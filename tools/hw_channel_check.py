"""HW per-earcup isolation check (Gate-T2; run by a human with the rig assembled).

The rig: each DJI transmitter mic sits inside one Bose earcup. Playing a tone out ONLY the LEFT
output channel should couple into ONLY DJI ch0 (left); out ONLY the RIGHT channel should couple
into ONLY DJI ch1 (right). This verifies the hardware channel isolation the whole two-way design
depends on (no cross-talk between the German and English sides).

For each side it: plays a short tone out that one earcup, captures both DJI channels, and asserts
the energy appears on the intended channel and NOT (much) on the other. Prints PASS/FAIL per side.

    python tools/hw_channel_check.py [seconds] [tone_hz]

Needs the Bose (A2DP, 2ch) and the DJI ("Wireless Mic Rx", 2ch) connected. NEVER selects the
Bose HFP mic (devices.resolve_device refuses Bose as input).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from audio import devices  # noqa: E402
from contracts import OutputChannel  # noqa: E402

# dB ratio the intended channel must exceed the other by for a clean PASS.
ISOLATION_DB = 12.0


def _tone(seconds: float, hz: float, rate: int) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)


def _rms(x: np.ndarray) -> float:
    x = x.astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def _db(num: float, den: float) -> float:
    if den <= 0:
        return float("inf") if num > 0 else 0.0
    return 20.0 * np.log10(max(num, 1e-9) / den)


def play_and_capture(
    out_channel: OutputChannel,
    seconds: float,
    hz: float,
    out_index: int,
    in_index: int,
    out_rate: int,
    in_rate: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Play `hz` out ONLY `out_channel`; capture both DJI channels for the same span."""
    tone = _tone(seconds, hz, out_rate)
    stereo = np.zeros((tone.size, 2), dtype=np.int16)
    col = 0 if out_channel is OutputChannel.LEFT else 1
    stereo[:, col] = tone

    captured: list[np.ndarray] = []

    def in_cb(indata, frames, time_info, status):  # pragma: no cover (device)
        captured.append(indata.copy())

    with sd.InputStream(device=in_index, channels=2, samplerate=in_rate, dtype="int16",
                        callback=in_cb):
        # Settle, then play the tone (blocking) so capture brackets the playback.
        time.sleep(0.2)
        sd.play(stereo, samplerate=out_rate, device=out_index, blocking=True)
        time.sleep(0.2)

    rec = np.concatenate(captured) if captured else np.zeros((0, 2), dtype=np.int16)
    return rec[:, 0], rec[:, 1]


def check_side(out_channel: OutputChannel, intended_ch: int, seconds: float, hz: float,
               out_index: int, in_index: int, out_rate: int, in_rate: int) -> bool:
    left, right = play_and_capture(
        out_channel, seconds, hz, out_index, in_index, out_rate, in_rate)
    rms = (_rms(left), _rms(right))
    intended = rms[intended_ch]
    other = rms[1 - intended_ch]
    isolation = _db(intended, other)
    side = "LEFT->ch0" if out_channel is OutputChannel.LEFT else "RIGHT->ch1"
    ok = intended > 0 and isolation >= ISOLATION_DB
    print(f"  {side:11s}  ch0_rms={rms[0]:8.1f}  ch1_rms={rms[1]:8.1f}  "
          f"isolation={isolation:6.1f} dB  -> {'PASS' if ok else 'FAIL'}")
    return ok


def main(argv: list[str]) -> int:
    seconds = float(argv[0]) if len(argv) > 0 else 1.0
    hz = float(argv[1]) if len(argv) > 1 else 440.0

    cfg = config.DeviceConfig()
    out_index = devices.resolve_device(cfg.output_name, "output", min_channels=2)
    in_index = devices.resolve_device(cfg.input_name, "input", min_channels=2)
    out_rate = cfg.output_native_rate
    in_rate = cfg.input_native_rate

    print("HW per-earcup isolation check")
    print(f"  output: {cfg.output_name} (idx {out_index}, {out_rate} Hz)")
    print(f"  input : {cfg.input_name} (idx {in_index}, {in_rate} Hz)")
    print(f"  tone  : {hz:.0f} Hz, {seconds:.1f}s; need >= {ISOLATION_DB:.0f} dB isolation\n")

    left_ok = check_side(OutputChannel.LEFT, 0, seconds, hz, out_index, in_index, out_rate, in_rate)
    right_ok = check_side(OutputChannel.RIGHT, 1, seconds, hz, out_index, in_index, out_rate, in_rate)

    ok = left_ok and right_ok
    print("\n  RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
