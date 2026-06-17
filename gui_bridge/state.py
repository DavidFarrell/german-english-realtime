"""ViewState - the seam between the GUI engine and the renderer.

Plain mutable data with a `snapshot()` that returns exactly the protocol's `state` dict
(desktop/PROTOCOL.md). No engine logic, no I/O. The engine mutates this; the server serialises it.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

WAVEFORM_BARS = 48          # how many envelope bars the renderer draws per side
SUBPEAKS_PER_CHUNK = 16     # peaks extracted from each 100 ms input chunk (lively scroll)
SPEAKING_LEVEL = 0.06       # normalised level above which a side counts as "speaking"
SPEAKING_HOLD_MS = 350      # keep "speaking" lit this long after the last loud chunk
UTTERANCE_CAP = 60          # rolling transcript length


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def level_from_pcm(pcm_s16le: bytes) -> float:
    """Normalised 0..1 meter from an s16le buffer (dBFS -60..0 mapped across the bar)."""
    a = np.frombuffer(pcm_s16le, dtype=np.int16)
    if a.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    if rms <= 0:
        return 0.0
    dbfs = 20.0 * math.log10(rms / 32768.0)
    return max(0.0, min(1.0, (dbfs + 60.0) / 60.0))


def _subpeaks(pcm_s16le: bytes, n: int) -> list[float]:
    """Split a chunk into `n` windows and return each window's peak magnitude 0..1."""
    a = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32)
    if a.size == 0:
        return [0.0] * n
    # pad to a multiple of n, then reduce by window max-abs
    pad = (-a.size) % n
    if pad:
        a = np.concatenate([a, np.zeros(pad, np.float32)])
    windows = a.reshape(n, -1)
    peaks = np.max(np.abs(windows), axis=1) / 32768.0
    return [float(min(1.0, p)) for p in peaks]


@dataclass
class SideState:
    name: str
    lang: Literal["en", "de"]
    level: float = 0.0
    out_level: float = 0.0
    waveform: deque = field(default_factory=lambda: deque([0.0] * WAVEFORM_BARS, maxlen=WAVEFORM_BARS))
    muted: bool = False
    testing: bool = False
    _last_loud_ms: int = 0

    def push_input(self, pcm_s16le: bytes) -> None:
        self.level = level_from_pcm(pcm_s16le)
        for p in _subpeaks(pcm_s16le, SUBPEAKS_PER_CHUNK):
            self.waveform.append(p)
        if self.level >= SPEAKING_LEVEL:
            self._last_loud_ms = _now_ms()

    def push_output_level(self, pcm_s16le: bytes) -> None:
        self.out_level = level_from_pcm(pcm_s16le)

    @property
    def speaking(self) -> bool:
        return (_now_ms() - self._last_loud_ms) <= SPEAKING_HOLD_MS

    def snapshot(self) -> dict:
        return {
            "name": self.name, "lang": self.lang,
            "level": round(self.level, 3), "outLevel": round(self.out_level, 3),
            "waveform": [round(v, 3) for v in self.waveform],
            "muted": self.muted, "testing": self.testing, "speaking": self.speaking,
        }


@dataclass
class Utterance:
    id: int
    side: Literal["left", "right"]
    speaker: str
    src_lang: str
    dst_lang: str
    t_start_ms: int
    source: str = ""
    translation: str = ""
    live: bool = True
    last_fragment_ms: int = 0

    def snapshot(self) -> dict:
        return {
            "id": self.id, "side": self.side, "speaker": self.speaker,
            "srcLang": self.src_lang, "dstLang": self.dst_lang,
            "tStartMs": self.t_start_ms, "source": self.source.strip(),
            "translation": self.translation.strip(), "live": self.live,
        }


@dataclass
class DeviceSlot:
    found: bool = False
    name: str = ""
    index: int | None = None

    def snapshot(self) -> dict:
        return {"found": self.found, "deviceName": self.name, "deviceIndex": self.index}


@dataclass
class ChannelTestState:
    active: bool = False
    side: Literal["left", "right"] | None = None
    phase: str = "idle"           # idle|listening|playing|awaiting|ok|crossed
    crossed: bool = False
    confirmed: dict = field(default_factory=lambda: {"left": False, "right": False})

    def snapshot(self) -> dict:
        return {
            "active": self.active, "side": self.side, "phase": self.phase,
            "crossed": self.crossed, "confirmed": dict(self.confirmed),
        }


class ViewState:
    """Everything the renderer needs, in one mutable object. snapshot() -> protocol `state` dict."""

    def __init__(self) -> None:
        self.wizard_step = "splash"
        self.input = DeviceSlot()
        self.output = DeviceSlot()
        self.gain = 0.72
        self.sides: dict[str, SideState] = {
            "left": SideState(name="David", lang="en"),
            "right": SideState(name="Debbie", lang="de"),
        }
        self.channel_test = ChannelTestState()
        self.session_running = False
        self.session_started_ms = 0
        self.utterances: deque[Utterance] = deque(maxlen=UTTERANCE_CAP)
        self._next_id = 1
        # one live utterance per side - the two speakers interleave, so they must NOT finalise
        # each other (a single global "last utterance" cursor would).
        self._live: dict[str, Utterance | None] = {"left": None, "right": None}
        self.error: dict | None = None

    # -- transcript helpers (the engine calls these from the pump) --
    def add_fragment(self, side: str, kind: Literal["src", "dst"], text: str,
                     src_lang: str, dst_lang: str) -> None:
        now = _now_ms()
        cur = self._live.get(side)
        if cur is None or not cur.live:
            cur = Utterance(
                id=self._next_id, side=side, speaker=self.sides[side].name,
                src_lang=src_lang, dst_lang=dst_lang, t_start_ms=now,
            )
            self._next_id += 1
            self.utterances.append(cur)
            self._live[side] = cur
        if kind == "src":
            cur.source += text
        else:
            cur.translation += text
        cur.last_fragment_ms = now

    def finalize_stale_utterances(self, soft_gap_ms: int = 1100, hard_gap_ms: int = 2200) -> None:
        """End a live utterance when its speaker pauses. Since the model emits no turn_complete,
        finalise on a SHORT gap if the text ends on terminal punctuation, else on a longer gap."""
        now = _now_ms()
        for side, cur in self._live.items():
            if cur is None or not cur.live:
                continue
            gap = now - cur.last_fragment_ms
            text = (cur.source or cur.translation).rstrip()
            ends_clause = text.endswith((".", "?", "!", "…", ":"))
            if gap > hard_gap_ms or (ends_clause and gap > soft_gap_ms):
                cur.live = False
                self._live[side] = None

    def reset_transcript(self) -> None:
        self.utterances.clear()
        self._live = {"left": None, "right": None}

    def elapsed_ms(self) -> int:
        return (_now_ms() - self.session_started_ms) if self.session_running else 0

    def snapshot(self) -> dict:
        return {
            "type": "state",
            "wizardStep": self.wizard_step,
            "input": self.input.snapshot(),
            "output": self.output.snapshot(),
            "gain": round(self.gain, 3),
            "sides": {k: v.snapshot() for k, v in self.sides.items()},
            "channelTest": self.channel_test.snapshot(),
            "session": {
                "running": self.session_running,
                "elapsedMs": self.elapsed_ms(),
                "utterances": [u.snapshot() for u in self.utterances],
            },
            "error": self.error,
        }
