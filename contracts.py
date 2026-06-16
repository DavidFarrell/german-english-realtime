"""Frozen realtime contracts. Phase-1 barrier.

Every implementation module imports from here and NEVER edits this file. These constants and
types are the shared seam between the audio layer, the Live-session layer, and the app/router.
If two modules disagree about chunk size, PCM layout, queue policy, or event shape, the whole
thing silently corrupts audio - so it all lives here, once.

Facts pinned 16 Jun 2026 against Google Live-Translate docs + a verified live round-trip:
  - model input  : 16-bit PCM, 16 kHz, mono, little-endian, sent in 100 ms chunks
  - model output : 24 kHz mono PCM
  - translation is unidirectional per session -> two-way = two sessions (en target + de target)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Literal, Protocol

# --- PCM / framing (model side; device rates are handled in the audio layer) ---
INPUT_RATE = 16_000            # model wants 16 kHz mono in
INPUT_FRAMES = 1_600          # exactly 100 ms @ 16 kHz -> 3200 bytes s16le
INPUT_CHUNK_BYTES = INPUT_FRAMES * 2
MODEL_OUTPUT_RATE = 24_000    # model emits 24 kHz mono
PCM_FORMAT = "s16le"          # mono, little-endian, 16-bit, throughout

# --- Backpressure (numeric so every module makes the SAME choice) ---
INPUT_QUEUE_MAX_CHUNKS = 10           # ~1 s; drop-OLDEST on overflow, bump a counter
OUTPUT_JITTER_TARGET_MS = 120
OUTPUT_JITTER_MAX_MS = 400            # beyond this, drop-oldest; zero-fill on underrun


class Direction(Enum):
    DE_TO_EN = "de_to_en"   # German speaker (TX1/left) heard as English
    EN_TO_DE = "en_to_de"   # English speaker (TX2/right) heard as German

    @property
    def target_language_code(self) -> str:
        return "en" if self is Direction.DE_TO_EN else "de"


class OutputChannel(Enum):
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class InputAudioChunk:
    """Exactly one 100 ms frame of mono 16 kHz s16le audio headed for a session."""
    pcm_s16le: bytes              # mono 16k, exactly INPUT_CHUNK_BYTES long
    seq: int
    source: Literal["left", "right", "fixture"]
    t_capture_ns: int | None = None      # monotonic ns when captured (for latency)
    source_frame0: int | None = None     # index of this chunk's first sample in the source


@dataclass(frozen=True)
class OutputAudioChunk:
    """A chunk of translated audio coming back from a session (mono 24k, variable length)."""
    pcm_s16le: bytes
    direction: Direction
    seq: int
    t_received_ns: int


@dataclass(frozen=True)
class SessionEvent:
    """The ONE event type a live session emits. status/error carry go_away / reconnect."""
    direction: Direction
    kind: Literal["audio", "input_transcript", "output_transcript", "status", "error"]
    t_ns: int
    audio: OutputAudioChunk | None = None
    text: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class LiveSessionConfig:
    """Everything needed to open one unidirectional translate session."""
    direction: Direction
    model: str = "gemini-3.5-live-translate-preview"
    api_version: str = "v1beta"
    echo_target_language: bool = False
    input_transcription: bool = True
    output_transcription: bool = True

    @property
    def target_language_code(self) -> str:
        return self.direction.target_language_code


async def live_events(
    config: LiveSessionConfig,
    source: AsyncIterator[InputAudioChunk],
) -> AsyncIterator[SessionEvent]:
    """Open a Gemini Live-Translate session for one direction, pump `source` chunks in, and
    yield SessionEvents (audio + transcripts + status/error). Implemented in translate_session.py.
    """
    raise NotImplementedError
    yield  # pragma: no cover  (makes this an async generator for typing)


class AudioRuntime(Protocol):
    """The audio I/O boundary. Implemented by the real PortAudio runtime and by a fake."""

    def input_source(self, source: Literal["left", "right"]) -> AsyncIterator[InputAudioChunk]:
        """An async stream of 100 ms 16 kHz mono chunks for one DJI channel."""
        ...

    def enqueue_output(self, channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        """Hand a translated chunk to a jitter-buffered output channel. False = dropped."""
        ...
