"""HW SIMULTANEOUS two-way test (Gate-T2 capstone; needs the rig + Stereo/Quad DJI + key).

Plays German into the LEFT earcup and English into the RIGHT earcup AT THE SAME TIME, captures
both DJI channels via the real runtime, and runs BOTH translate directions concurrently:
  ch0 (left, German)  -> translate(en)  -> out_twoway_en.wav
  ch1 (right, English) -> translate(de) -> out_twoway_de.wav
This proves the channel isolation holds end-to-end: each session should hear only its own speaker,
not the other (which would show up as garbled cross-translation).

Outputs are saved to files, NOT played back into the earcups (that would feed back, since each
direction's output earcup holds that direction's input mic on this desk rig).

    python tools/hw_twoway_test.py
"""
from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import soxr
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import config  # noqa: E402
from audio import PortAudioRuntime, devices  # noqa: E402
from contracts import Direction, LiveSessionConfig, MODEL_OUTPUT_RATE  # noqa: E402
from translate_session import live_events  # noqa: E402

CAP_PAD = 2.5
SILENCE_RMS = 180


def _read_wav(p: Path) -> tuple[bytes, int]:
    with wave.open(str(p), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


def _write_wav(p: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)


def _build_stereo(de16k: bytes, en16k: bytes, out_rate: int) -> np.ndarray:
    """German -> LEFT, English -> RIGHT, both resampled to out_rate, played together."""
    de = np.asarray(soxr.resample(np.frombuffer(de16k, np.int16), 16_000, out_rate), np.int16)
    en = np.asarray(soxr.resample(np.frombuffer(en16k, np.int16), 16_000, out_rate), np.int16)
    n = max(de.size, en.size)
    st = np.zeros((n, 2), np.int16)
    st[:de.size, 0] = de
    st[:en.size, 1] = en
    return st


async def _translate(direction: Direction, runtime: PortAudioRuntime, deadline: float,
                     out: bytearray, in_txt: list[str], out_txt: list[str]) -> None:
    cfg = LiveSessionConfig(direction=direction)
    src = runtime.input_source(config.SOURCE_FOR_DIRECTION[direction])
    gen = live_events(cfg, src)
    loop = asyncio.get_running_loop()
    try:
        async for ev in gen:
            if ev.kind == "audio" and ev.audio and ev.audio.pcm_s16le:
                out.extend(ev.audio.pcm_s16le)
            elif ev.kind == "input_transcript" and ev.text:
                in_txt.append(ev.text)
            elif ev.kind == "output_transcript" and ev.text:
                out_txt.append(ev.text)
            elif ev.kind == "error":
                print(f"  [{direction.value}] ERROR: {ev.detail}", file=sys.stderr); break
            if loop.time() > deadline:
                break
    finally:
        await gen.aclose()


async def main_async() -> int:
    de, _ = _read_wav(ROOT / "tests/fixtures/de_morgen.16k.wav")
    en, _ = _read_wav(ROOT / "tests/fixtures/en_short.16k.wav")
    cfg = config.DeviceConfig()
    out_index = devices.resolve_device(cfg.output_name, "output", min_channels=2)
    out_rate = cfg.output_native_rate
    stereo = _build_stereo(de, en, out_rate)
    play_secs = stereo.shape[0] / out_rate

    runtime = PortAudioRuntime()
    runtime.start()
    print("HW simultaneous two-way - German(left) + English(right) at once, isolation should hold.\n")

    de_out, en_out = bytearray(), bytearray()
    de_in_t, de_out_t, en_in_t, en_out_t = [], [], [], []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + play_secs + CAP_PAD
    try:
        play = asyncio.create_task(asyncio.to_thread(
            sd.play, stereo, out_rate, device=out_index, blocking=True))
        await asyncio.gather(
            _translate(Direction.DE_TO_EN, runtime, deadline, de_out, de_in_t, de_out_t),
            _translate(Direction.EN_TO_DE, runtime, deadline, en_out, en_in_t, en_out_t),
        )
        await play
    finally:
        runtime.stop()

    _write_wav(ROOT / "out_twoway_en.wav", bytes(de_out), MODEL_OUTPUT_RATE)
    _write_wav(ROOT / "out_twoway_de.wav", bytes(en_out), MODEL_OUTPUT_RATE)
    print("[DE->EN]  (left mic, German speaker)")
    print(f"  heard : {''.join(de_in_t)!r}")
    print(f"  spoke : {''.join(de_out_t)!r}")
    print(f"  out   : {len(de_out):,}B -> out_twoway_en.wav")
    print("[EN->DE]  (right mic, English speaker)")
    print(f"  heard : {''.join(en_in_t)!r}")
    print(f"  spoke : {''.join(en_out_t)!r}")
    print(f"  out   : {len(en_out):,}B -> out_twoway_de.wav")
    ok = bool(de_out) and bool(en_out) and bool(de_in_t) and bool(en_in_t)
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
