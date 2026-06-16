"""HW acoustic end-to-end loopback (Gate-T2; autonomous, run by a human with rig + key).

The full mouth-to-ear path over the rig:
  1. play a German fixture out the LEFT earcup (one output channel),
  2. capture the coupled DJI ch0 via the real PortAudioRuntime,
  3. translate it (target=en) through translate_session.live_events,
  4. save out_loopback_en.wav + print transcripts;
then the English side: English fixture out the RIGHT earcup -> DJI ch1 -> translate(de) ->
out_loopback_de.wav.

This is the autonomous end-to-end hardware test: it exercises capture + Bluetooth playout + the
real model, which the API-only --once and latency `component` modes deliberately exclude.

    python tools/hw_loopback_test.py

Needs the Bose (A2DP 2ch out), the DJI ("Wireless Mic Rx" 2ch in), and GEMINI_API_KEY in .env.
NB: the Bose A2DP playout latency (~150-300 ms) is part of what this measures - see RUNBOOK.md.
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import config  # noqa: E402
from audio import PortAudioRuntime  # noqa: E402
from contracts import (  # noqa: E402
    Direction,
    LiveSessionConfig,
    MODEL_OUTPUT_RATE,
    OutputChannel,
)
from latency import align_latency  # noqa: E402
from translate_session import live_events  # noqa: E402

CAP_SECONDS_PAD = 2.0  # extra capture beyond the fixture length, for tail + model flush


def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "expected mono s16le"
        return w.readframes(w.getnframes()), w.getframerate()


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _play_out_earcup(pcm16k: bytes, channel: OutputChannel, device_index: int, rate: int) -> None:
    """Play the (16k mono) fixture out ONLY one earcup, resampled to the device rate. Blocking,
    intended to be run in a thread so it overlaps with live capture."""
    import soxr

    mono = np.frombuffer(pcm16k, dtype=np.int16)
    res = np.asarray(soxr.resample(mono, 16_000, rate), dtype=np.int16)
    stereo = np.zeros((res.size, 2), dtype=np.int16)
    stereo[:, 0 if channel is OutputChannel.LEFT else 1] = res
    sd.play(stereo, samplerate=rate, device=device_index, blocking=True)


async def run_side(fixture: Path, direction: str, runtime: PortAudioRuntime,
                   out_device_index: int, out_rate: int) -> int:
    d = Direction.DE_TO_EN if direction == "en" else Direction.EN_TO_DE
    out_channel = config.OUTPUT_FOR_DIRECTION[d]            # which earcup we play into
    cap_channel = config.SOURCE_FOR_DIRECTION[d]            # which DJI channel couples back
    src_pcm16k, src_rate = _read_wav(fixture)
    fixture_secs = len(src_pcm16k) / 2 / src_rate

    print(f"\n[{d.value}] play {fixture.name} out {out_channel.value} earcup -> "
          f"capture DJI {cap_channel} -> translate({direction})")

    # Start playing the fixture out the earcup in the background; capture + translate live.
    play_task = asyncio.create_task(asyncio.to_thread(
        _play_out_earcup, src_pcm16k, out_channel, out_device_index, out_rate))

    out_audio = bytearray()
    in_txt: list[str] = []
    out_txt: list[str] = []
    cfg = LiveSessionConfig(direction=d)
    src_chunks = runtime.input_source(cap_channel)

    deadline = time.monotonic() + fixture_secs + CAP_SECONDS_PAD

    async def translate() -> None:
        async for ev in live_events(cfg, src_chunks):
            if ev.kind == "audio" and ev.audio is not None and ev.audio.pcm_s16le:
                out_audio.extend(ev.audio.pcm_s16le)
            elif ev.kind == "input_transcript" and ev.text:
                in_txt.append(ev.text)
            elif ev.kind == "output_transcript" and ev.text:
                out_txt.append(ev.text)
            elif ev.kind == "status" and ev.detail in ("turn_complete", "generation_complete"):
                break
            elif ev.kind == "error":
                print(f"  ERROR: {ev.detail}", file=sys.stderr)
                break
            if time.monotonic() > deadline:
                break

    try:
        await asyncio.wait_for(translate(), timeout=fixture_secs + CAP_SECONDS_PAD + 15)
    except asyncio.TimeoutError:
        print("  (timeout waiting for translation)", file=sys.stderr)
    await play_task

    out_path = ROOT / f"out_loopback_{direction}.wav"
    _write_wav(out_path, bytes(out_audio), MODEL_OUTPUT_RATE)
    # NB: no shared-clock offset is recorded here (fixture-file zero != model-output zero), so
    # this is (model_preroll - fixture_leading_silence), NOT mouth-to-ear and NOT incl. Bluetooth.
    # See align_latency's ZERO-POINT WARNING; pass extra_offset_ns once the rig records the
    # capture-start-vs-playout-start offset on one monotonic clock.
    align = align_latency(src_pcm16k, src_rate, bytes(out_audio), MODEL_OUTPUT_RATE)
    print(f"  heard : {''.join(in_txt)!r}")
    print(f"  spoke : {''.join(out_txt)!r}")
    print(f"  out   : {len(out_audio):,} bytes 24k -> {out_path} "
          f"({len(out_audio)/2/MODEL_OUTPUT_RATE:.1f}s)")
    print(f"  align : {('n/a' if align is None else f'{align:.0f} ms')} "
          f"(fixture-onset->output-onset; NOT mouth-to-ear, excludes capture+BT)")
    return 0 if out_audio else 1


async def main_async() -> int:
    de_fixture = ROOT / "tests/fixtures/de_morgen.16k.wav"
    en_fixture = ROOT / "tests/fixtures/en_short.16k.wav"
    cfg = config.DeviceConfig()

    from audio import devices

    out_index = devices.resolve_device(cfg.output_name, "output", min_channels=2)
    out_rate = cfg.output_native_rate

    runtime = PortAudioRuntime()
    runtime.start()
    print("HW acoustic loopback - run with the rig assembled, room quiet, earcups sealed.")
    try:
        rc1 = await run_side(de_fixture, "en", runtime, out_index, out_rate)
        rc2 = await run_side(en_fixture, "de", runtime, out_index, out_rate)
    finally:
        runtime.stop()
    ok = rc1 == 0 and rc2 == 0
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
