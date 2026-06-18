"""GuiEngine - the GUI's composition root over the frozen translation core.

Mirrors app.py's routing (audio -> output channel, transcripts -> view) but makes the routing
runtime-swappable and adds the controls the wizard needs: device selection + presence polling,
input gain, per-mic test, the channel-test self-loopback (own mic -> own ear, ~0.5 s delay),
crossed-wiring swaps, and start/stop of the two live sessions.

The translation core (`translate_session.live_events`, `audio.PortAudioRuntime`) is untouched and
injected, so this whole engine runs headless with fakes (no API, no hardware).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import replace
from typing import Callable

import numpy as np

import config
import contracts
from contracts import Direction, InputAudioChunk, LiveSessionConfig, OutputChannel
from audio import devices

from . import macaudio
from .state import ViewState

# Process names that hold a mic but aren't worth naming to the user (system plumbing / ourselves).
_HOLDER_LABELS: dict[str, str | None] = {
    "Sound": "the Sound settings window",
    "replayd": "a screen / audio recording",
    "coreaudiod": None,
    "VTDecoderXPCService": None,
}


def _friendly_holder(names: list[str]) -> str | None:
    """Turn raw mic-holding process names into a short human phrase, or None if only noise/us."""
    out: list[str] = []
    for n in names:
        if n in _HOLDER_LABELS:
            label = _HOLDER_LABELS[n]
            if label:
                out.append(label)
        elif n.lower().startswith("python") or "DebbieDavid" in n or n.startswith("pid "):
            continue  # ourselves / unnameable
        else:
            out.append(n)
    seen: set[str] = set()
    uniq = [x for x in out if not (x in seen or seen.add(x))]
    return ", ".join(uniq) if uniq else None

# Design asked for ~0.5 s; we keep it just under the 400 ms output jitter cap so the channel-test
# echo can't pile up in the buffer, and to limit feedback cascade on the mics-in-earcups desk rig.
LOOPBACK_DELAY_MS = 300
SRC_LANG = {Direction.DE_TO_EN: "de", Direction.EN_TO_DE: "en"}
DST_LANG = {Direction.DE_TO_EN: "en", Direction.EN_TO_DE: "de"}


def _ear(side: str) -> OutputChannel:
    return OutputChannel.LEFT if side == "left" else OutputChannel.RIGHT


def _other(side: str) -> str:
    return "right" if side == "left" else "left"


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class GuiEngine:
    def __init__(
        self,
        live_events_fn: Callable | None = None,
        runtime_factory: Callable | None = None,
        device_config: config.DeviceConfig | None = None,
    ) -> None:
        self.cfg = device_config or config.DeviceConfig()
        self._live_events_fn = live_events_fn
        self._runtime_factory = runtime_factory  # (in_idx, out_idx) -> runtime
        self.state = ViewState()

        # runtime-swappable routing: direction -> (source mic side, output ear channel).
        # REAL TWO-PERSON PRODUCT routing is CROSSED: each person hears the OTHER, translated, in
        # their own ear. With left = English speaker, right = German speaker (the design's default):
        #   DE_TO_EN reads the German (right) mic and outputs English to the English (left) ear;
        #   EN_TO_DE reads the English (left) mic and outputs German to the German (right) ear.
        # (NB this differs from the frozen engine's same-side config map, which is the desk-rig
        # loopback wiring - see notes/UX_REIMPL_HANDOVER.md and David's flag.)
        self.routing: dict[Direction, tuple[str, OutputChannel]] = {}
        self._rebuild_routing_from_langs()
        # device selection (None index = resolve by name)
        self._input_name = self.cfg.input_name
        self._output_name = self.cfg.output_name
        self._input_index: int | None = None
        self._output_index: int | None = None

        self.runtime = None
        self._session_tasks: list[asyncio.Task] = []
        self._mic_tests: dict[str, asyncio.Task] = {}
        self._input_monitors: dict[str, asyncio.Task] = {}
        self._channel_test_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.refresh_devices()

    # -- gain --------------------------------------------------------------------------------
    def _gain_factor(self) -> float:
        # slider 0..1 -> linear multiplier 0..2 (0.5 = unity); default 0.72 ~= +3 dB
        return max(0.0, min(2.0, self.state.gain * 2.0))

    def _apply_gain(self, pcm: bytes) -> bytes:
        g = self._gain_factor()
        if abs(g - 1.0) < 1e-3:
            return pcm
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * g
        return np.clip(a, -32768, 32767).astype(np.int16).tobytes()

    async def _gain_source(self, raw, side: str):
        """Wrap a raw input source: apply gain, meter the side, yield gained chunks."""
        async for chunk in raw:
            pcm = self._apply_gain(chunk.pcm_s16le)
            self.state.sides[side].push_input(pcm)
            yield replace(chunk, pcm_s16le=pcm)

    # -- devices -----------------------------------------------------------------------------
    def refresh_devices(self) -> None:
        in_idx = self._input_index if self._input_index is not None \
            else devices.find(self._input_name, "input", min_channels=2)
        out_idx = devices.find(self._output_name, "output", min_channels=2) \
            if self._output_index is None else self._output_index
        self.state.input.found = in_idx is not None
        self.state.input.index = in_idx
        self.state.input.name = self._input_name
        self.state.output.found = out_idx is not None
        self.state.output.index = out_idx
        self.state.output.name = self._output_name
        # keep the live device lists fresh for the pickers (incl. the not-found screen)
        self.state.input_devices = devices.list_devices_structured("input")
        self.state.output_devices = devices.list_devices_structured("output")

    def list_devices(self) -> dict:
        return {
            "inputDevices": devices.list_devices_structured("input"),
            "outputDevices": devices.list_devices_structured("output"),
        }

    async def select_device(self, kind: str, index: int) -> None:
        structured = devices.list_devices_structured(kind)
        match = next((d for d in structured if d["index"] == index), None)
        if match is None:
            return
        # Changing a device requires tearing down the open runtime so the change actually takes
        # effect (otherwise the UI would show a device the engine isn't really using).
        was_live = self.state.session_running
        if self.runtime is not None or was_live:
            await self.stop_live()
            await self._cancel_mic_tests()
            await self._cancel_channel_test()
            await self._cancel_input_monitor()
            self._stop_runtime()
        if kind == "input":
            self._input_name, self._input_index = match["name"], index
        else:
            self._output_name, self._output_index = match["name"], index
        self.refresh_devices()
        if was_live:
            await self.start_live()
        elif self.state.wizard_step == "inputs":
            await self.start_input_monitor()   # keep the mic waveforms live after a mic swap

    # -- runtime lifecycle -------------------------------------------------------------------
    def _make_runtime(self):
        if self._runtime_factory is not None:
            return self._runtime_factory(self.state.input.index, self.state.output.index)
        from audio import PortAudioRuntime
        return PortAudioRuntime(
            self.cfg, input_index=self.state.input.index, output_index=self.state.output.index)

    def _ensure_runtime(self) -> bool:
        if self.runtime is not None:
            return True
        # Only the MICROPHONE is required to start the runtime - output (earbuds) is optional, so
        # the mic check works before the earbuds are connected (capture-only). Output-needing
        # actions check _output_ready() separately.
        if self._runtime_factory is None:
            self.refresh_devices()
            if not self.state.input.found:
                self.state.set_error("mic_missing", "Microphone not found.")
                return False
        try:
            self.runtime = self._make_runtime()
            self.runtime.start()
            self.state.clear_error()
            return True
        except Exception as exc:  # device opened by something else, HFP collapse, etc.
            self.state.set_error("runtime_error", str(exc))
            self.runtime = None
            return False

    def _output_ready(self) -> bool:
        """True if the runtime can actually play audio (earbuds present)."""
        return self.runtime is not None and getattr(self.runtime, "output_available", True)

    def _output_mono_device(self) -> dict | None:
        """The selected output device dict if it's stuck in mono call mode (outCh < 2), else None."""
        idx = self.state.output.index
        dev = next((d for d in self.state.output_devices if d["index"] == idx), None)
        if dev is not None and dev.get("outCh", 2) < 2:
            return dev
        return None

    def _set_output_error(self) -> None:
        """Set the toast for an unavailable output. If the earbuds are in mono call mode, name the
        process holding the mic and mark it fixable so the renderer shows a one-tap Fix button."""
        dev = self._output_mono_device()
        if dev is not None:
            holder = None
            try:
                holder = _friendly_holder(macaudio.running_input_process_names())
            except Exception:
                holder = None
            held = f" - held by {holder}" if holder else ""
            msg = (f"“{dev['name']}” is in mono call mode{held}. "
                   f"Tap Fix to switch the earbuds back to stereo.")
            self.state.set_error("output_mono", msg, fixable=True, holder=holder)
            return
        err = getattr(self.runtime, "output_error", None)
        if err:
            self.state.set_error("no_output", f"Couldn't open the earbuds for stereo: {err}")
        else:
            self.state.set_error("no_output", "Connect earbuds to hear the translation.")

    async def fix_earbuds(self) -> None:
        """Un-stick the earbuds from HFP mono back to A2DP stereo (see macaudio.force_earbuds_stereo),
        then re-resolve devices. Runs the blocking recipe off the event loop so state keeps flowing."""
        if self.state.fixing_output:
            return
        self.state.fixing_output = True
        self.state.set_error("fixing", "Fixing the earbuds - this takes a few seconds…")
        # free the audio devices so the recipe can re-route them without us holding a stream
        await self.stop_live()
        await self._cancel_mic_tests()
        await self._cancel_channel_test()
        await self._cancel_input_monitor()
        self._stop_runtime()
        try:
            result = await asyncio.to_thread(
                macaudio.force_earbuds_stereo, self._output_name, self._input_name)
        except Exception as exc:
            result = {"ok": False, "channels": 0, "steps": [str(exc)]}
        finally:
            self.state.fixing_output = False
        self.refresh_devices()
        if result.get("ok"):
            self.state.clear_error()
        else:
            self._set_output_error()

    def _stop_runtime(self) -> None:
        if self.runtime is not None:
            try:
                self.runtime.stop()
            finally:
                self.runtime = None

    # -- live sessions -----------------------------------------------------------------------
    def _live_events(self):
        if self._live_events_fn is not None:
            return self._live_events_fn
        from translate_session import live_events
        return live_events

    async def _pump(self, direction: Direction) -> None:
        source_side, out_channel = self.routing[direction]
        cfg = LiveSessionConfig(direction=direction)
        raw = self.runtime.input_source(source_side)
        src = self._gain_source(raw, source_side)
        ear = out_channel.value
        try:
            async for ev in self._live_events()(cfg, src):
                if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
                    self.runtime.enqueue_output(out_channel, ev.audio)
                    self.state.sides[ear].push_output_level(ev.audio.pcm_s16le)
                elif ev.kind == "input_transcript" and ev.text:
                    self.state.add_fragment(source_side, "src", ev.text,
                                            SRC_LANG[direction], DST_LANG[direction])
                elif ev.kind == "output_transcript" and ev.text:
                    self.state.add_fragment(source_side, "dst", ev.text,
                                            SRC_LANG[direction], DST_LANG[direction])
                elif ev.kind == "error":
                    self.state.set_error("session_error", ev.detail or "error")
        except asyncio.CancelledError:
            raise

    async def start_live(self) -> None:
        async with self._lock:
            if self.state.session_running:
                return
            await self._cancel_mic_tests()
            await self._cancel_channel_test()
            await self._cancel_input_monitor()
            if not self._ensure_runtime():
                return
            if not self._output_ready():
                self._set_output_error()
                return
            self.state.session_running = True
            self.state.session_started_ms = _now_ms()
            self._session_tasks = [
                asyncio.create_task(self._pump(d), name=f"pump-{d.value}") for d in Direction
            ]

    async def stop_live(self) -> None:
        async with self._lock:
            await self._cancel_tasks(self._session_tasks)
            self._session_tasks = []
            self.state.session_running = False

    # -- mic test (stream one channel's level/waveform, no translation) ----------------------
    async def _mic_test_loop(self, side: str) -> None:
        raw = self.runtime.input_source(side)
        try:
            async for chunk in raw:
                self.state.sides[side].push_input(self._apply_gain(chunk.pcm_s16le))
        except asyncio.CancelledError:
            raise

    async def test_mic(self, side: str, on: bool) -> None:
        async with self._lock:  # same lock as start/stop live: one consumer per capture channel
            if not on:
                t = self._mic_tests.pop(side, None)
                if t:
                    await self._cancel_tasks([t])
                self.state.sides[side].testing = False
                return
            if self.state.session_running:
                self.state.set_error("busy", "Stop live before testing a mic.")
                return
            if side in self._mic_tests:
                return
            if not self._ensure_runtime():
                return
            self.state.sides[side].testing = True
            self._mic_tests[side] = asyncio.create_task(
                self._mic_test_loop(side), name=f"mictest-{side}")

    # -- input monitor (live mic waveforms on the inputs screen; no earbuds required) ----------
    async def _monitor_loop(self, side: str) -> None:
        raw = self.runtime.input_source(side)
        try:
            async for chunk in raw:
                self.state.sides[side].push_input(self._apply_gain(chunk.pcm_s16le))
        except asyncio.CancelledError:
            raise

    async def start_input_monitor(self) -> None:
        async with self._lock:
            if self.state.session_running or self._input_monitors:
                return
            if not self._ensure_runtime():
                return
            for side in ("left", "right"):
                self._input_monitors[side] = asyncio.create_task(
                    self._monitor_loop(side), name=f"monitor-{side}")

    async def _cancel_input_monitor(self) -> None:
        tasks = list(self._input_monitors.values())
        self._input_monitors.clear()
        await self._cancel_tasks(tasks)

    # -- channel test: own mic -> own ear, ~0.3 s delay --------------------------------------
    def _ear_for_side(self, side: str) -> OutputChannel:
        # The channel test is a PHYSICAL self-check: play this mic back into the SAME physical
        # side's earbud, independent of live (crossed) translation routing. If the person hears
        # themselves, their mic + bud are on the same side; if not, the buds are crossed. (Deriving
        # this from live routing would play into the OTHER ear and falsely flag correct hardware.)
        return _ear(side)

    async def _channel_test_loop(self, side: str) -> None:
        from audio.capture import StreamingResampler
        ear = self._ear_for_side(side)
        rate = getattr(self.runtime, "output", None)
        device_rate = getattr(rate, "device_rate", contracts.MODEL_OUTPUT_RATE)
        rs = StreamingResampler(contracts.INPUT_RATE, device_rate)
        pending: deque[tuple[int, np.ndarray]] = deque()
        raw = self.runtime.input_source(side)
        ct = self.state.channel_test
        ct.phase = "listening"
        try:
            async for chunk in raw:
                pcm = self._apply_gain(chunk.pcm_s16le)
                self.state.sides[side].push_input(pcm)
                res = rs.feed(np.frombuffer(pcm, dtype=np.int16))
                if res.size:
                    pending.append((_now_ms() + LOOPBACK_DELAY_MS, res))
                now = _now_ms()
                while pending and pending[0][0] <= now:
                    _, s = pending.popleft()
                    out = getattr(self.runtime, "output", None)
                    if out is not None and hasattr(out, "enqueue_device_samples"):
                        out.enqueue_device_samples(ear, s)
                    ct.phase = "playing"
            # Source ended naturally (capture stopped / fixture exhausted): flush the delay line so
            # the tail still reaches the ear. On cancel we skip this (the test is being torn down).
            out = getattr(self.runtime, "output", None)
            if out is not None and hasattr(out, "enqueue_device_samples"):
                while pending:
                    out.enqueue_device_samples(ear, pending.popleft()[1])
        except asyncio.CancelledError:
            raise

    async def start_channel_test(self, side: str) -> None:
        async with self._lock:
            if self.state.session_running:
                self.state.set_error("busy", "Stop live before the channel test.")
                return
            await self._cancel_channel_test()
            await self._cancel_mic_tests()
            await self._cancel_input_monitor()
            if not self._ensure_runtime():
                return
            if not self._output_ready():
                self._set_output_error()
                return
            ct = self.state.channel_test
            ct.active = True
            ct.side = side
            ct.crossed = False
            ct.phase = "listening"
            self._channel_test_task = asyncio.create_task(
                self._channel_test_loop(side), name=f"chantest-{side}")

    async def stop_channel_test(self) -> None:
        await self._cancel_channel_test()
        ct = self.state.channel_test
        ct.active = False
        if ct.phase in ("listening", "playing"):
            ct.phase = "idle"

    async def channel_test_answer(self, side: str, ok: bool) -> None:
        await self._cancel_channel_test()
        ct = self.state.channel_test
        ct.active = False
        if ok:
            ct.confirmed[side] = True
            ct.crossed = False
            ct.phase = "ok"
        else:
            ct.crossed = True
            ct.phase = "crossed"

    def test_ear(self, side: str) -> None:
        """Play a short 440 Hz tone into one earbud only (the outputs-row 'Test ear' button)."""
        if not self._ensure_runtime():
            return
        if not self._output_ready():
            self._set_output_error()
            return
        out = getattr(self.runtime, "output", None)
        if out is None or not hasattr(out, "enqueue_device_samples"):
            return
        ear = OutputChannel.LEFT if side == "left" else OutputChannel.RIGHT
        rate = out.device_rate
        t = np.arange(int(rate * 0.6)) / rate
        env = np.minimum(1.0, np.minimum(t * 20, (t[-1] - t) * 20))  # tiny fade in/out
        tone = (np.sin(2 * np.pi * 440 * t) * env * 0.25 * 32767).astype(np.int16)
        out.enqueue_device_samples(ear, tone)

    # -- routing derivation + crossed-wiring fixes -------------------------------------------
    def _rebuild_routing_from_langs(self) -> None:
        """Canonical CROSSED routing from each side's language. The person on a side speaks (and
        hears) that side's language; the direction translating FROM it reads that mic and outputs
        to the OTHER person's ear. Resets any earbud swap to canonical."""
        de_side = "left" if self.state.sides["left"].lang == "de" else "right"
        en_side = _other(de_side)
        self.routing = {
            Direction.DE_TO_EN: (de_side, _ear(en_side)),   # German mic  -> English listener's ear
            Direction.EN_TO_DE: (en_side, _ear(de_side)),   # English mic -> German listener's ear
        }

    async def _restart_live_if_running(self, mutate) -> None:
        """Apply a routing/state mutation safely: if a session is live, stop it, mutate, restart."""
        was_live = self.state.session_running
        if was_live:
            await self.stop_live()
        await self._cancel_mic_tests()
        await self._cancel_channel_test()
        mutate()
        self._reset_channel_test()
        if was_live:
            await self.start_live()

    async def swap_earbuds(self) -> None:
        def _mut() -> None:
            for d, (src, out) in self.routing.items():
                flipped = OutputChannel.RIGHT if out is OutputChannel.LEFT else OutputChannel.LEFT
                self.routing[d] = (src, flipped)
        await self._restart_live_if_running(_mut)

    async def swap_people(self) -> None:
        def _mut() -> None:
            for d, (src, out) in self.routing.items():
                self.routing[d] = (_other(src), out)
            left, right = self.state.sides["left"], self.state.sides["right"]
            left.name, right.name = right.name, left.name
            left.lang, right.lang = right.lang, left.lang
        await self._restart_live_if_running(_mut)

    def _reset_channel_test(self) -> None:
        ct = self.state.channel_test
        ct.crossed = False
        ct.phase = "idle"
        ct.confirmed = {"left": False, "right": False}

    # -- simple setters ----------------------------------------------------------------------
    def set_gain(self, value: float) -> None:
        self.state.gain = max(0.0, min(1.0, float(value)))

    def set_name(self, side: str, name: str) -> None:
        if side in self.state.sides and name.strip():
            self.state.sides[side].name = name.strip()[:24]

    async def set_language(self, side: str, lang: str) -> None:
        if side not in self.state.sides or lang not in ("en", "de"):
            return

        def _mut() -> None:
            self.state.sides[side].lang = lang
            self.state.sides[_other(side)].lang = "de" if lang == "en" else "en"
            self._rebuild_routing_from_langs()
        await self._restart_live_if_running(_mut)

    async def goto_step(self, step: str) -> None:
        if step not in ("splash", "inputs", "outputs", "channel", "live"):
            return
        self.state.clear_error()        # navigating dismisses any stale toast
        self.state.wizard_step = step
        # Live mic waveforms only while on the inputs screen; free the channels everywhere else.
        if step == "inputs":
            await self.start_input_monitor()
        else:
            await self._cancel_input_monitor()

    def rescan(self) -> None:
        self.refresh_devices()

    # -- cancellation helpers ----------------------------------------------------------------
    async def _cancel_tasks(self, tasks: list[asyncio.Task]) -> None:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _cancel_mic_tests(self) -> None:
        tasks = list(self._mic_tests.values())
        self._mic_tests.clear()
        for s in self.state.sides.values():
            s.testing = False
        await self._cancel_tasks(tasks)

    async def _cancel_channel_test(self) -> None:
        if self._channel_test_task is not None:
            await self._cancel_tasks([self._channel_test_task])
            self._channel_test_task = None

    async def shutdown(self) -> None:
        await self.stop_live()
        await self._cancel_mic_tests()
        await self._cancel_channel_test()
        await self._cancel_input_monitor()
        self._stop_runtime()
