"""Latency harness for the two-way translator.

Two CLI modes plus a pure, hardware-free analysis core:

  - PURE CORE (unit-tested on synthetic signals): energy-onset detection, component-metric
    math from timestamps, and a source-onset -> output-onset alignment.
  - MODE A `component` (API only, no hardware): stream a 16k fixture through
    translate_session.live_events in real-time-paced 100 ms chunks and time
    send-start / first-output-byte / last-output-byte. EXCLUDES capture + playout.
  - MODE B `acoustic`: the genuine mouth-to-ear test (play a clip out ONE Bose earcup, capture the
    coupled DJI channel, translate, report align_latency incl. capture + Bluetooth playout) lives
    in tools/hw_loopback_test.py. The `acoustic` CLI here only prints the methodology and points
    there; run_acoustic in this module is just the injectable runtime->translate wiring core (it
    does not drive stimulus playout - see its docstring).

The pure core never imports the SDK or sounddevice, so the tests run with no key and no rig.
"""
from __future__ import annotations

import array
import asyncio
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from contracts import (
    MODEL_OUTPUT_RATE,
    Direction,
    LiveSessionConfig,
)

# Full-scale reference for s16le energy in dBFS (a sine at +/-32767 ~= 0 dBFS).
_S16_FULL_SCALE = 32768.0
NS_PER_S = 1_000_000_000


# --------------------------------------------------------------------------------------------
# Pure analysis core (no API, no hardware)
# --------------------------------------------------------------------------------------------
def _samples(pcm_s16le: bytes) -> array.array:
    """Decode s16le bytes to a signed-16 array (truncating any trailing odd byte)."""
    a = array.array("h")
    n = len(pcm_s16le) - (len(pcm_s16le) % 2)
    a.frombytes(pcm_s16le[:n])
    return a


def onset_ns(
    pcm_s16le: bytes,
    rate: int,
    threshold_dbfs: float = -35.0,
    window_ms: float = 20.0,
) -> int | None:
    """First sample index (as nanoseconds) where short-window RMS energy crosses a threshold.

    Slides a `window_ms` window over the signal and returns the start of the first window whose
    RMS level is at or above `threshold_dbfs` (relative to s16 full scale). Returns None if the
    signal never crosses the threshold (e.g. pure silence). Used to locate speech onset.
    """
    s = _samples(pcm_s16le)
    if not s:
        return None
    win = max(1, int(rate * window_ms / 1000.0))
    thresh_lin = _S16_FULL_SCALE * (10.0 ** (threshold_dbfs / 20.0))
    thresh_sq = thresh_lin * thresh_lin
    # Hop by a fraction of the window for resolution without scanning every sample.
    hop = max(1, win // 4)
    for start in range(0, len(s), hop):
        frame = s[start:start + win]
        if len(frame) < win:
            break
        mean_sq = sum(v * v for v in frame) / len(frame)
        if mean_sq >= thresh_sq:
            return int(start * NS_PER_S / rate)
    return None


@dataclass(frozen=True)
class LatencyResult:
    """Component latency for one translate session, in milliseconds.

    All fields measure from send-start (the moment the first input chunk is sent). These EXCLUDE
    audio capture and playout - they are model + network only (see Mode A).
    """
    direction: Direction
    first_byte_ms: float | None        # send-start -> first output byte
    last_byte_ms: float | None         # send-start -> last output byte
    output_span_ms: float | None       # first byte -> last byte (output stream duration)
    output_bytes: int                  # total translated audio bytes received
    turn_complete: bool                # did the session signal end-of-output

    def as_dict(self) -> dict:
        return {
            "direction": self.direction.value,
            "first_byte_ms": self.first_byte_ms,
            "last_byte_ms": self.last_byte_ms,
            "output_span_ms": self.output_span_ms,
            "output_bytes": self.output_bytes,
            "turn_complete": self.turn_complete,
        }


def component_metrics(
    direction: Direction,
    send_start_ns: int,
    first_byte_ns: int | None,
    last_byte_ns: int | None,
    output_bytes: int,
    turn_complete: bool,
) -> LatencyResult:
    """Compute component latency (ms) from monotonic-ns timestamps.

    `first_byte_ns` / `last_byte_ns` are None when no output audio was received.
    """
    def ms(a: int | None, b: int | None) -> float | None:
        if a is None or b is None:
            return None
        return (a - b) / 1_000_000.0

    return LatencyResult(
        direction=direction,
        first_byte_ms=ms(first_byte_ns, send_start_ns),
        last_byte_ms=ms(last_byte_ns, send_start_ns),
        output_span_ms=ms(last_byte_ns, first_byte_ns),
        output_bytes=output_bytes,
        turn_complete=turn_complete,
    )


def align_latency(
    source_pcm: bytes,
    source_rate: int,
    out_pcm: bytes,
    out_rate: int,
    extra_offset_ns: int = 0,
    threshold_dbfs: float = -35.0,
) -> float | None:
    """Source speech onset -> translated-output onset, in milliseconds.

    ZERO-POINT WARNING: `source_pcm` and `out_pcm` onsets are measured from each buffer's OWN
    first sample. The result is only mouth-to-ear latency if those two zeros are the same instant
    on one clock. They usually are NOT: a fixture file's zero is its own start (its leading
    silence), while a translated stream's zero is the model's first emitted sample - which already
    excludes the capture/network/Bluetooth round-trip. Subtracting them then yields roughly
    (model_preroll - fixture_leading_silence), which is independent of the real path delay and can
    even be negative. To get a true figure the caller MUST either (a) measure both onsets against
    one shared monotonic clock and pass the source-zero-minus-output-zero difference as
    `extra_offset_ns`, or (b) pass `source_pcm` as the CAPTURED source channel (which shares the
    output stream's clock), not the fixture file. With `extra_offset_ns=0` and a fixture source,
    do NOT label the number 'mouth-to-ear' or 'incl. Bluetooth' - it is not that quantity.

    `extra_offset_ns` is added to the result; it is the offset (ns) between the source buffer's
    zero and the output buffer's zero on a shared clock. If either signal has no detectable onset,
    returns None.

    METHODOLOGY LIMIT: this is *energy-onset to energy-onset*, only an approximation of the real
    quantity of interest - "disambiguating word spoken -> its translation first audible". It is
    sensitive to leading silence, to VAD/endpointing behaviour, and to variable translation
    wording (the target clause can start on a different word than the source, and German
    verb-final order shifts where the disambiguating word falls). Treat the number as indicative,
    not exact; pair it with the input/output transcripts when interpreting.
    """
    src = onset_ns(source_pcm, source_rate, threshold_dbfs=threshold_dbfs)
    out = onset_ns(out_pcm, out_rate, threshold_dbfs=threshold_dbfs)
    if src is None or out is None:
        return None
    return (out - src + extra_offset_ns) / 1_000_000.0


# --------------------------------------------------------------------------------------------
# Mode A - component latency (API only, no hardware)
# --------------------------------------------------------------------------------------------
def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "expected mono s16le"
        return w.readframes(w.getnframes()), w.getframerate()


async def measure_component(
    fixture: Path,
    direction: Direction,
    live_events_fn=None,
) -> LatencyResult:
    """Stream `fixture` through a live session and time first/last output byte.

    `live_events_fn` defaults to the real translate_session.live_events; tests pass
    fakes.fake_live_events. The fixture is paced in real time by fakes.fixture_source, so the
    timings reflect the model + network only - capture and playout are EXCLUDED.
    """
    if live_events_fn is None:
        from translate_session import live_events as live_events_fn  # lazy: keeps core SDK-free

    from fakes import fixture_source

    source = fixture_source(fixture, source="fixture", realtime=True)
    cfg = LiveSessionConfig(direction=direction)

    send_start_ns: int | None = None
    first_byte_ns: int | None = None
    last_byte_ns: int | None = None
    out_bytes = 0
    turn_complete = False

    send_start_ns = time.monotonic_ns()
    async for ev in live_events_fn(cfg, source):
        if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
            if first_byte_ns is None:
                first_byte_ns = ev.t_ns
            last_byte_ns = ev.t_ns
            out_bytes += len(ev.audio.pcm_s16le)
        elif ev.kind == "status" and ev.detail in ("turn_complete", "generation_complete"):
            turn_complete = True

    return component_metrics(
        direction, send_start_ns, first_byte_ns, last_byte_ns, out_bytes, turn_complete,
    )


def _print_component_row(label: str, r: LatencyResult) -> None:
    fb = "n/a" if r.first_byte_ms is None else f"{r.first_byte_ms:7.0f}"
    lb = "n/a" if r.last_byte_ms is None else f"{r.last_byte_ms:7.0f}"
    sp = "n/a" if r.output_span_ms is None else f"{r.output_span_ms:7.0f}"
    secs = r.output_bytes / 2 / MODEL_OUTPUT_RATE
    print(f"  {label:10s} {fb:>9s} {lb:>9s} {sp:>9s}   {r.output_bytes:>8,}  ({secs:4.1f}s)"
          f"  {'done' if r.turn_complete else '...':>4s}")


async def run_component(fixture: Path, direction: Direction, both: bool) -> int:
    print(f"\nMode A - component latency (model + network only; EXCLUDES capture + playout)")
    print(f"  fixture: {fixture.name}")
    print(f"  {'dir':10s} {'first-ms':>9s} {'last-ms':>9s} {'span-ms':>9s}   "
          f"{'out-bytes':>8s}")
    results = []
    if both:
        for d in (Direction.DE_TO_EN, Direction.EN_TO_DE):
            r = await measure_component(fixture, d)
            _print_component_row(d.value, r)
            results.append(r)
    else:
        r = await measure_component(fixture, direction)
        _print_component_row(direction.value, r)
        results.append(r)
    ok = any(r.output_bytes > 0 for r in results)
    print("  RESULT:", "PASS" if ok else "FAIL (no output audio)")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------------
# Mode B - acoustic loopback (hardware; run later by a human)
# --------------------------------------------------------------------------------------------
ACOUSTIC_METHODOLOGY = """\
Mode B - acoustic loopback (mouth-to-ear, INCLUDES capture + Bluetooth playout)

The genuine acoustic test lives in tools/hw_loopback_test.py - run THAT, not this module. It is
the only path wired to actually play a stimulus out the earcup. latency.run_acoustic here is just
the injectable runtime->translate wiring core (and its fake-driven test); it does NOT drive
playout, so with the real rig it would translate ambient mic with no source signal.

Rig: the DJI transmitters' mics sit inside the Bose earcups, so playing a clip out ONE earcup
(one output channel only) couples acoustically into exactly ONE DJI channel. tools/hw_loopback_test:
  1. plays the source clip out one Bose earcup (LEFT or RIGHT only),
  2. simultaneously captures the coupled DJI channel,
  3. runs the captured audio through the translate path (translate_session.live_events),
  4. records the translated output,
  5. reports align_latency(source_onset -> output_onset).

METHODOLOGY LIMITS:
  - align_latency is energy-onset to energy-onset: an approximation of "disambiguating word ->
    target audible", sensitive to leading silence, VAD, and variable translation wording.
  - Bluetooth A2DP playout adds ~150-300 ms that wired output would not; run wired vs AirPods to
    quantify the Bluetooth tax (it can eat the <500 ms budget on its own).
  - Acoustic coupling adds room/speaker colouration; keep level high and the earcup sealed.
"""


@dataclass(frozen=True)
class AcousticResult:
    direction: Direction
    align_ms: float | None
    source_secs: float
    output_secs: float


async def run_acoustic(
    fixture: Path,
    direction: Direction,
    *,
    runtime=None,
    live_events_fn=None,
    extra_offset_ns: int = 0,
) -> AcousticResult:
    """Wire a runtime's captured `direction` channel through the translate path, for one direction.

    Importable and injectable: `runtime` and `live_events_fn` default to the real PortAudioRuntime
    + live SDK (imported lazily so this module loads with neither present), but a fake may be
    passed for dry-runs.

    NOTE - this does NOT play `source_pcm` out the earcup. The frozen AudioRuntime protocol exposes
    only input_source + enqueue_output (no source-playout method), so with the real
    PortAudioRuntime nothing drives the stimulus and the captured channel is just whatever the DJI
    mic happens to hear - the align number would then be meaningless. The genuine mouth-to-ear
    acoustic path, which spawns playout of the fixture out one earcup concurrently with capture,
    is tools/hw_loopback_test.py; use that for a real acoustic measurement. Here `source_pcm` is
    only the reference onset for align_latency, and enqueue_output just routes the translation back
    to the listener's earcup. Retained as the injectable runtime->translate wiring core (and its
    fake-driven test); the CLI deliberately points at tools/hw_loopback_test.py instead.
    """
    from config import OUTPUT_FOR_DIRECTION, SOURCE_FOR_DIRECTION

    if runtime is None:  # pragma: no cover - requires hardware
        from audio import PortAudioRuntime  # type: ignore
        runtime = PortAudioRuntime()
    if live_events_fn is None:  # pragma: no cover - requires the SDK + key
        from translate_session import live_events as live_events_fn

    source_pcm, source_rate = _read_wav(fixture)
    out_channel = OUTPUT_FOR_DIRECTION[direction]
    capture_channel = SOURCE_FOR_DIRECTION[direction]

    # Drive the runtime's captured `capture_channel` through translate, routing each translated
    # chunk back out `out_channel`. We do NOT play `source_pcm` here (the AudioRuntime protocol has
    # no source-playout method) - a fake runtime replays a fixture for the channel; the real one
    # captures whatever the DJI mic hears. For genuine acoustic playout see tools/hw_loopback_test.
    cfg = LiveSessionConfig(direction=direction)
    src_chunks = runtime.input_source(capture_channel)

    out_audio = bytearray()
    async for ev in live_events_fn(cfg, src_chunks):
        if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
            out_audio.extend(ev.audio.pcm_s16le)
            runtime.enqueue_output(out_channel, ev.audio)

    align_ms = align_latency(
        source_pcm, source_rate, bytes(out_audio), MODEL_OUTPUT_RATE,
        extra_offset_ns=extra_offset_ns,
    )
    return AcousticResult(
        direction=direction,
        align_ms=align_ms,
        source_secs=len(source_pcm) / 2 / source_rate,
        output_secs=len(out_audio) / 2 / MODEL_OUTPUT_RATE,
    )


def _print_acoustic(r: AcousticResult) -> None:
    a = "n/a" if r.align_ms is None else f"{r.align_ms:.0f} ms"
    print(f"  {r.direction.value:10s}  align(onset->onset) = {a:>10s}"
          f"   src {r.source_secs:.1f}s  out {r.output_secs:.1f}s")


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _direction(arg: str) -> Direction:
    a = arg.lower()
    if a in ("en", "de_to_en", "de->en"):
        return Direction.DE_TO_EN
    if a in ("de", "en_to_de", "en->de"):
        return Direction.EN_TO_DE
    raise SystemExit(f"unknown target/direction {arg!r} (use en | de | de_to_en | en_to_de)")


def _usage() -> int:
    print(__doc__)
    print("usage:")
    print("  python latency.py component <fixture.16k.wav> [en|de|both]")
    print("  python latency.py acoustic  -> prints methodology, points to tools/hw_loopback_test.py")
    return 2


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return _usage()
    mode = argv[0]
    if mode == "component":
        if len(argv) < 2:
            return _usage()
        fixture = Path(argv[1])
        tgt = argv[2] if len(argv) > 2 else "en"
        if tgt.lower() == "both":
            return asyncio.run(run_component(fixture, Direction.DE_TO_EN, both=True))
        return asyncio.run(run_component(fixture, _direction(tgt), both=False))
    if mode == "acoustic":
        # The genuine acoustic path is tools/hw_loopback_test.py (it actually plays the stimulus
        # out the earcup). Running run_acoustic against the real rig would translate ambient mic
        # with no source signal, so this CLI no longer does that - it points you at the real tool.
        print(ACOUSTIC_METHODOLOGY)
        print("Run the genuine acoustic test instead:\n  python tools/hw_loopback_test.py")
        return 0
    return _usage()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
