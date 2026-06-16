"""Two-way DE<->EN live translator: composition root.

This module OWNS the event loop, the TaskGroup, SIGINT graceful shutdown, and the routing of
SessionEvents to audio output + UI. No worker module creates or closes the loop.

The core seam is `pump_direction`: it consumes one direction's `live_events(...)` stream and
routes every event the SAME way regardless of whether the events come from the real session or a
fake, and whether output goes to real hardware or a fake runtime. `--check` and `--run` both go
through it, so a green `--check` proves the real composition.

Modes (argparse):
  --list-devices   print audio.devices.list_devices() and exit.
  --check          FAKE end-to-end, NO API + NO hardware (FakeAudioRuntime + fake_live_events),
                   both directions concurrently through the real routing; asserts non-empty.
  --once FIX --target en|de   ONE real session from a fixture; writes out_<target>.wav.
  --run            the REAL two-way app: PortAudioRuntime + real live_events, two sessions.
  (no args)        short usage.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
import wave
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import numpy as np

import config
from contracts import (
    Direction,
    InputAudioChunk,
    LiveSessionConfig,
    MODEL_OUTPUT_RATE,
    OutputAudioChunk,
    OutputChannel,
    SessionEvent,
)
from ui import NullUi, TerminalUi, UiState

ROOT = Path(__file__).resolve().parent

# A live_events-shaped callable: (config, source) -> async iterator of SessionEvents.
LiveEventsFn = Callable[[LiveSessionConfig, AsyncIterator[InputAudioChunk]], AsyncIterator[SessionEvent]]

# An enqueue-output sink: (OutputChannel, OutputAudioChunk) -> bool (False = dropped).
# pump_direction resolves OUTPUT_FOR_DIRECTION once and hands the sink the concrete channel, so
# the sink is a thin wrapper around runtime.enqueue_output and never re-does the routing.
OutputSink = Callable[[OutputChannel, OutputAudioChunk], bool]


# --------------------------------------------------------------------------------------------
# Shared plumbing - identical for fake (--check) and real (--run)
# --------------------------------------------------------------------------------------------
class DirectionStats:
    """Per-direction counters collected as events are routed. Used by --check assertions."""

    def __init__(self, direction: Direction) -> None:
        self.direction = direction
        self.events = 0
        self.audio_chunks = 0
        self.output_bytes = 0
        self.input_transcript = ""
        self.output_transcript = ""
        self.turn_complete = False
        self.error: str | None = None


async def pump_direction(
    direction: Direction,
    source: AsyncIterator[InputAudioChunk],
    live_events_fn: LiveEventsFn,
    output_sink: OutputSink,
    state: UiState,
) -> DirectionStats:
    """Run ONE direction end to end: open the session, route every event.

    Routing (the contract the whole app rests on):
      - audio              -> output_sink(OUTPUT_FOR_DIRECTION[direction], chunk) + UI level
      - input_transcript   -> UI heard-line
      - output_transcript  -> UI spoke-line
      - status / error     -> UI status

    Returns DirectionStats so callers (notably --check) can assert non-empty output.
    """
    out_channel = config.OUTPUT_FOR_DIRECTION[direction]
    cfg = LiveSessionConfig(direction=direction)
    stats = DirectionStats(direction)
    state.set_status(direction, "connecting")

    async for ev in live_events_fn(cfg, source):
        stats.events += 1
        if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
            stats.audio_chunks += 1
            stats.output_bytes += len(ev.audio.pcm_s16le)
            output_sink(out_channel, ev.audio)
            state.on_output_chunk(direction, ev.audio.pcm_s16le)
            state.set_status(direction, "translating")
        elif ev.kind == "input_transcript" and ev.text:
            stats.input_transcript += ev.text
            state.on_input_transcript(direction, ev.text)
        elif ev.kind == "output_transcript" and ev.text:
            stats.output_transcript += ev.text
            state.on_output_transcript(direction, ev.text)
        elif ev.kind == "status":
            if ev.detail in ("turn_complete", "generation_complete"):
                stats.turn_complete = True
            state.set_status(direction, ev.detail or "status")
        elif ev.kind == "error":
            stats.error = ev.detail
            state.set_status(direction, f"error: {ev.detail}")

    if stats.error is None:
        state.set_status(direction, "done")
    return stats


def _level_taps(state: UiState) -> dict[Direction, Callable[[InputAudioChunk], None]]:
    """Make per-direction input-level taps the source-wrapper can call (UI input meters)."""
    return {d: (lambda ch, _d=d: state.on_input_chunk(_d, ch.pcm_s16le)) for d in Direction}


async def _tapped(
    source: AsyncIterator[InputAudioChunk], tap: Callable[[InputAudioChunk], None]
) -> AsyncIterator[InputAudioChunk]:
    """Pass-through wrapper that feeds each chunk to a tap (for input-level metering)."""
    async for chunk in source:
        tap(chunk)
        yield chunk


# --------------------------------------------------------------------------------------------
# --list-devices
# --------------------------------------------------------------------------------------------
def cmd_list_devices() -> int:
    from audio import list_devices

    print(list_devices())
    return 0


# --------------------------------------------------------------------------------------------
# --check : FAKE end-to-end, no API, no hardware
# --------------------------------------------------------------------------------------------
async def _check() -> int:
    import fakes

    runtime = fakes.FakeAudioRuntime(
        left_wav=str(ROOT / "tests/fixtures/de_morgen.16k.wav"),
        right_wav=str(ROOT / "tests/fixtures/en_short.16k.wav"),
        realtime=False,
    )
    state = UiState()
    taps = _level_taps(state)

    def output_sink(channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        return runtime.enqueue_output(channel, audio)

    async def run_one(direction: Direction) -> DirectionStats:
        raw = runtime.input_source(config.SOURCE_FOR_DIRECTION[direction])
        source = _tapped(raw, taps[direction])
        return await pump_direction(
            direction, source, fakes.fake_live_events, output_sink, state
        )

    # Both directions concurrently, through the SAME routing the real app uses.
    results: dict[Direction, DirectionStats] = {}
    async with asyncio.TaskGroup() as tg:
        tasks = {d: tg.create_task(run_one(d)) for d in Direction}
    for d, t in tasks.items():
        results[d] = t.result()

    print("--check: FAKE end-to-end (no API, no hardware), both directions through real routing\n")
    ok = True
    for direction in (Direction.DE_TO_EN, Direction.EN_TO_DE):
        st = results[direction]
        out_ch = config.OUTPUT_FOR_DIRECTION[direction]
        captured = len(runtime.captured[out_ch])
        print(f"  {direction.value:9s} -> out {out_ch.value:5s}: "
              f"events={st.events:3d}  audio={st.audio_chunks:3d}  "
              f"out_bytes={st.output_bytes:6d}  captured={captured:6d}  "
              f"turn_complete={st.turn_complete}")
        print(f"    heard : {st.input_transcript!r}")
        print(f"    spoke : {st.output_transcript!r}")
        if st.error is not None:
            print(f"    ERROR : {st.error}")
            ok = False
        if st.events == 0 or st.audio_chunks == 0 or st.output_bytes == 0:
            print("    FAIL  : empty pipeline")
            ok = False
        if captured == 0:
            print("    FAIL  : nothing reached the output channel")
            ok = False
        if not st.turn_complete:
            print("    FAIL  : no turn_complete")
            ok = False

    # Cross-routing sanity: DE->EN must land on LEFT, EN->DE on RIGHT, never crossed.
    if (config.OUTPUT_FOR_DIRECTION[Direction.DE_TO_EN] is not OutputChannel.LEFT
            or config.OUTPUT_FOR_DIRECTION[Direction.EN_TO_DE] is not OutputChannel.RIGHT):
        print("    FAIL  : routing map is wrong")
        ok = False

    print("\n  RESULT:", "PASS" if ok else "FAIL")
    print(TerminalUi(state).render_once())
    return 0 if ok else 1


# --------------------------------------------------------------------------------------------
# --once : ONE real session from a fixture (Gate-T1, real API)
# --------------------------------------------------------------------------------------------
def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _trim_trailing_silence(pcm: bytes, rms_thresh: float, win_ms: int = 50,
                           rate: int = MODEL_OUTPUT_RATE, keep_ms: int = 200) -> bytes:
    """Drop trailing near-silence (the model pads silence after the clause), keeping a short tail."""
    a = np.frombuffer(pcm, dtype=np.int16)
    if a.size == 0:
        return pcm
    win = max(1, win_ms * rate // 1000)
    last_voiced = 0
    for start in range(0, a.size, win):
        seg = a[start:start + win].astype(np.float64)
        if seg.size and np.sqrt(np.mean(seg * seg)) >= rms_thresh:
            last_voiced = start + seg.size
    end = min(a.size, last_voiced + keep_ms * rate // 1000)
    return a[:end].tobytes()


async def _once(fixture: Path, target: str) -> int:
    import fakes
    from translate_session import live_events

    direction = Direction.DE_TO_EN if target == "en" else Direction.EN_TO_DE
    cfg = LiveSessionConfig(direction=direction)
    source = fakes.fixture_source(fixture, source="fixture", realtime=True)

    out_audio = bytearray()
    in_txt: list[str] = []
    out_txt: list[str] = []
    t_send0 = time.monotonic_ns()
    t_first: int | None = None
    t_last: int | None = None
    turn_complete = False
    err: str | None = None

    # NB: this Live-Translate model is a CONTINUOUS streamer - it sends NO turn_complete /
    # generation_complete (verified 16 Jun). After the clause it just emits silence. So single-shot
    # --once detects end-of-turn by SILENCE: once real output audio has arrived and then stays quiet
    # for END_SILENCE_MS, the turn is done. wait_for is a final backstop.
    END_SILENCE_MS = 1200
    SILENCE_RMS = 180  # int16 RMS below this == silence (ambient floor measured ~16)

    def _rms(pcm: bytes) -> float:
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

    last_nonsilent_ns: int | None = None

    async def consume() -> None:
        nonlocal t_first, t_last, turn_complete, err, last_nonsilent_ns
        gen = live_events(cfg, source)
        try:
            async for ev in gen:
                if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
                    if t_first is None:
                        t_first = ev.t_ns
                    t_last = ev.t_ns
                    out_audio.extend(ev.audio.pcm_s16le)
                    if _rms(ev.audio.pcm_s16le) >= SILENCE_RMS:
                        last_nonsilent_ns = ev.t_ns
                    elif (last_nonsilent_ns is not None
                          and ev.t_ns - last_nonsilent_ns > END_SILENCE_MS * 1_000_000):
                        turn_complete = True  # silence-based end of turn
                        break
                elif ev.kind == "input_transcript" and ev.text:
                    in_txt.append(ev.text)
                elif ev.kind == "output_transcript" and ev.text:
                    out_txt.append(ev.text)
                elif ev.kind == "status" and ev.detail in ("turn_complete", "generation_complete"):
                    turn_complete = True
                    break
                elif ev.kind == "error":
                    err = ev.detail
                    break
        finally:
            await gen.aclose()

    try:
        await asyncio.wait_for(consume(), timeout=35)
    except asyncio.TimeoutError:
        print("  (no end-of-turn within 35s - stopped on timeout)", file=sys.stderr)
    if err is not None:
        print(f"  ERROR: {err}", file=sys.stderr)
        return 1

    # Trim trailing silence from the saved clip (the model pads silence after the clause).
    trimmed = _trim_trailing_silence(bytes(out_audio), SILENCE_RMS)
    out_path = ROOT / f"out_{target}.wav"
    _write_wav(out_path, trimmed, MODEL_OUTPUT_RATE)
    out_audio = bytearray(trimmed)
    print(f"  fixture    : {fixture.name}  (target={target}, {direction.value})")
    print(f"  heard      : {''.join(in_txt)!r}")
    print(f"  spoke      : {''.join(out_txt)!r}")
    secs = len(out_audio) / 2 / MODEL_OUTPUT_RATE
    print(f"  out audio  : {len(out_audio):,} bytes 24k -> {out_path}  ({secs:.1f}s)")
    if t_first is not None:
        print(f"  first-byte : {(t_first - t_send0) / 1e6:.0f} ms after send-start")
    if t_first is not None and t_last is not None:
        print(f"  output-span: {(t_last - t_first) / 1e6:.0f} ms")
    ok = bool(out_audio) and bool(out_txt)
    print(f"  turn_complete: {turn_complete}")
    print("  RESULT     :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------------
# --run : the REAL two-way app
# --------------------------------------------------------------------------------------------
async def _run(no_ui: bool = False) -> int:
    from audio import PortAudioRuntime
    from translate_session import live_events

    state = UiState()
    taps = _level_taps(state)
    ui = NullUi(state) if no_ui else TerminalUi(state)

    def output_sink(channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        return runtime.enqueue_output(channel, audio)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        print("\n[app] SIGINT - shutting down...", file=sys.stderr)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass  # platform without signal handlers (e.g. Windows) - Ctrl-C still raises

    runtime = PortAudioRuntime()
    runtime.start()
    print("[app] audio runtime started; two sessions live. Ctrl-C to stop.", file=sys.stderr)

    async def run_one(direction: Direction) -> None:
        raw = runtime.input_source(config.SOURCE_FOR_DIRECTION[direction])
        source = _tapped(raw, taps[direction])
        try:
            await pump_direction(direction, source, live_events, output_sink, state)
        except asyncio.CancelledError:
            raise

    try:
        async with asyncio.TaskGroup() as tg:
            session_tasks = [tg.create_task(run_one(d)) for d in Direction]
            ui_task = tg.create_task(ui.run())
            # Wait for SIGINT, then unwind the group cleanly.
            await stop.wait()
            for t in session_tasks:
                t.cancel()
            ui_task.cancel()
    except* asyncio.CancelledError:
        pass
    finally:
        # Shutdown ordering: stop capture/output AFTER the sessions stop pumping, so no chunk is
        # enqueued into a torn-down stream.
        runtime.stop()
        print("[app] stopped.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.py", description="Two-way DE<->EN live translator (Gemini Live-Translate).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    g.add_argument("--check", action="store_true",
                   help="fake end-to-end, no API + no hardware (proves composition)")
    g.add_argument("--once", metavar="FIXTURE.16k.wav",
                   help="one real session from a fixture (writes out_<target>.wav)")
    g.add_argument("--run", action="store_true", help="the real two-way app (needs hardware+key)")
    p.add_argument("--target", choices=("en", "de"), default="en",
                   help="target language for --once (default: en)")
    p.add_argument("--no-ui", action="store_true", help="run with a no-op UI (--run only)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_devices:
        return cmd_list_devices()
    if args.check:
        return asyncio.run(_check())
    if args.once:
        return asyncio.run(_once(Path(args.once), args.target))
    if args.run:
        return asyncio.run(_run(no_ui=args.no_ui))

    print("Two-way DE<->EN live translator.\n")
    print("usage:")
    print("  python app.py --list-devices              list audio devices")
    print("  python app.py --check                     fake end-to-end (no API, no hardware)")
    print("  python app.py --once FIX.16k.wav --target en|de   one real session from a fixture")
    print("  python app.py --run [--no-ui]             the real two-way app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
