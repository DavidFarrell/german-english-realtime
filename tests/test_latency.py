"""Tests for latency.py - pure core on synthetic signals, plus Mode A/B with fakes.

No API, no hardware: tones are built in-memory, the live path uses fakes.fake_live_events, and
the acoustic path uses fakes.FakeAudioRuntime replaying a synthetic WAV.
"""
from __future__ import annotations

import array
import math
import wave

import pytest

import latency
from contracts import Direction, MODEL_OUTPUT_RATE, OutputAudioChunk, SessionEvent
from fakes import FakeAudioRuntime, fake_live_events


def tone(rate: int, secs: float, amp: int = 12000, freq: float = 440.0) -> bytes:
    """A mono s16le sine at `amp` (well above the -35 dBFS onset threshold)."""
    a = array.array("h")
    n = int(rate * secs)
    for i in range(n):
        a.append(int(amp * math.sin(2 * math.pi * freq * i / rate)))
    return a.tobytes()


def silence(rate: int, secs: float) -> bytes:
    return b"\x00\x00" * int(rate * secs)


def write_wav(path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# --- onset_ns -------------------------------------------------------------------------------
def test_onset_pure_silence_is_none():
    assert latency.onset_ns(silence(16000, 0.5), 16000) is None


def test_onset_finds_tone_after_delay():
    rate = 16000
    delay_s = 0.30
    pcm = silence(rate, delay_s) + tone(rate, 0.5)
    on = latency.onset_ns(pcm, rate)
    assert on is not None
    # Onset should land at ~300 ms, within one detection window (~20 ms) of the constructed delay.
    measured_ms = on / 1_000_000
    assert abs(measured_ms - delay_s * 1000) <= 25, measured_ms


def test_onset_at_start_is_near_zero():
    on = latency.onset_ns(tone(16000, 0.3), 16000)
    assert on is not None and on < 25 * 1_000_000  # within ~25 ms of zero


def test_onset_threshold_rejects_quiet_signal():
    # A tone ~ -50 dBFS sits below the default -35 dBFS threshold -> no onset.
    quiet = tone(16000, 0.4, amp=100)
    assert latency.onset_ns(quiet, 16000) is None
    # ...but a generous threshold finds it.
    assert latency.onset_ns(quiet, 16000, threshold_dbfs=-60) is not None


# --- align_latency --------------------------------------------------------------------------
def test_align_latency_matches_constructed_delay():
    src_rate, out_rate = 16000, MODEL_OUTPUT_RATE
    # Source speaks at 100 ms; output (different rate) speaks at 600 ms -> 500 ms align.
    src = silence(src_rate, 0.10) + tone(src_rate, 0.4)
    out = silence(out_rate, 0.60) + tone(out_rate, 0.4)
    ms = latency.align_latency(src, src_rate, out, out_rate)
    assert ms is not None
    assert abs(ms - 500) <= 30, ms


def test_align_latency_extra_offset_applied():
    rate = 16000
    src = silence(rate, 0.10) + tone(rate, 0.3)
    out = silence(rate, 0.10) + tone(rate, 0.3)  # same onset -> ~0 before offset
    base = latency.align_latency(src, rate, out, rate)
    shifted = latency.align_latency(src, rate, out, rate, extra_offset_ns=50_000_000)
    assert base is not None and shifted is not None
    assert abs((shifted - base) - 50) <= 1, (base, shifted)


def test_align_latency_none_when_no_output_onset():
    rate = 16000
    src = silence(rate, 0.1) + tone(rate, 0.3)
    assert latency.align_latency(src, rate, silence(rate, 0.5), rate) is None


# --- component_metrics math -----------------------------------------------------------------
def test_component_metrics_math():
    send = 1_000_000_000
    first = send + 200_000_000      # +200 ms
    last = send + 1_700_000_000     # +1700 ms
    r = latency.component_metrics(
        Direction.DE_TO_EN, send, first, last, output_bytes=48000, turn_complete=True)
    assert r.first_byte_ms == pytest.approx(200.0)
    assert r.last_byte_ms == pytest.approx(1700.0)
    assert r.output_span_ms == pytest.approx(1500.0)
    assert r.output_bytes == 48000
    assert r.turn_complete is True
    assert r.as_dict()["direction"] == "de_to_en"


def test_component_metrics_no_output():
    r = latency.component_metrics(
        Direction.EN_TO_DE, 0, None, None, output_bytes=0, turn_complete=False)
    assert r.first_byte_ms is None and r.last_byte_ms is None and r.output_span_ms is None


# --- Mode A with the fake live session ------------------------------------------------------
@pytest.mark.asyncio
async def test_measure_component_with_fake(tmp_path):
    rate = 16000
    wav = tmp_path / "src.16k.wav"
    write_wav(wav, silence(rate, 0.1) + tone(rate, 0.5), rate)
    r = await latency.measure_component(wav, Direction.DE_TO_EN, live_events_fn=fake_live_events)
    # The fake echoes every input chunk as output audio and ends with turn_complete.
    assert r.output_bytes > 0
    assert r.first_byte_ms is not None and r.first_byte_ms >= 0
    assert r.last_byte_ms >= r.first_byte_ms
    assert r.turn_complete is True


# --- Mode B with the fake runtime + fake session --------------------------------------------
@pytest.mark.asyncio
async def test_run_acoustic_with_fakes(tmp_path):
    rate = 16000
    wav = tmp_path / "left.16k.wav"
    write_wav(wav, silence(rate, 0.1) + tone(rate, 0.5), rate)
    runtime = FakeAudioRuntime(left_wav=str(wav), realtime=False)
    r = await latency.run_acoustic(
        wav, Direction.DE_TO_EN, runtime=runtime, live_events_fn=fake_live_events)
    # FakeAudioRuntime replays the left fixture; fake session echoes it back as 24k output.
    # The echo copies the source bytes verbatim, so source and output share the SAME leading
    # silence and their onsets coincide by construction -> align ~ 0. This proves the plumbing
    # runs but says nothing about the onset-zero-point semantics (see the test below).
    assert r.output_secs > 0
    assert r.align_ms is not None  # both source and (echoed) output have a clear onset
    # Near-zero by construction (same bytes, same leading silence). Not exactly zero: the source
    # onset is timed at 16k but the identical output bytes are timed at MODEL_OUTPUT_RATE (24k),
    # so the shared sample index maps to a slightly earlier output time - itself a small taste of
    # the onset-zero-point mismatch the test below makes explicit.
    assert abs(r.align_ms) <= 60, r.align_ms


async def _silence_stripping_live_events(config, source):
    """A fake session that emits the echoed audio only AFTER the input's leading silence.

    The real model output does not preserve the source clip's leading silence: its stream begins
    when the translation starts speaking, on its own timeline. This fake mimics that - it drops
    silent input chunks and starts the output stream at the first non-silent chunk - so the
    output PCM has ~no leading silence even though the source fixture does.
    """
    seq = 0
    started = False
    async for chunk in source:
        if not started:
            # Skip leading silence: wait for the first chunk with any real energy.
            if max(abs(v) for v in array.array("h", chunk.pcm_s16le)) < 1000:
                continue
            started = True
        yield SessionEvent(
            direction=config.direction, kind="audio", t_ns=0,
            audio=OutputAudioChunk(pcm_s16le=chunk.pcm_s16le, direction=config.direction,
                                   seq=seq, t_received_ns=0))
        seq += 1
    yield SessionEvent(direction=config.direction, kind="status", t_ns=0,
                       detail="turn_complete")


@pytest.mark.asyncio
async def test_align_ms_reflects_file_internal_silence_not_wall_clock(tmp_path):
    """Document (and pin) that align_ms is onset-within-source vs onset-within-output.

    The fixture has 100 ms of leading silence; the fake session strips it, so the OUTPUT pcm
    starts at the tone with ~no leading silence. align_latency then reports out_onset - src_onset
    ~= 0 - 100 = -100 ms: a NEGATIVE 'latency' where the translation appears to arrive before the
    source was spoken. That is physically impossible for a true mouth-to-ear delay - it can only
    happen because align_ms compares each clip's INTERNAL onset position, with no shared
    wall-clock zero. This is the onset-zero-point assumption the live path violates; the test
    locks the current behaviour so the defect is visible rather than masked by the echo fake.
    """
    rate = 16000
    lead_ms = 100.0
    wav = tmp_path / "left.16k.wav"
    write_wav(wav, silence(rate, lead_ms / 1000) + tone(rate, 0.5), rate)
    runtime = FakeAudioRuntime(left_wav=str(wav), realtime=False)
    r = await latency.run_acoustic(
        wav, Direction.DE_TO_EN, runtime=runtime,
        live_events_fn=_silence_stripping_live_events)
    assert r.align_ms is not None
    # Source onset ~ +100 ms; output onset ~ 0 -> align ~ -100 ms (file-internal, not wall-clock).
    assert r.align_ms < -50, r.align_ms
    assert abs(r.align_ms - (-lead_ms)) <= 30, r.align_ms
