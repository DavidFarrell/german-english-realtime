"""Fakes so the whole pipeline runs with NO API and NO hardware.

These let unit tests and a `--check` end-to-end run prove that the modules compose, before any
key spend or hardware. The real implementations live in translate_session.py (live_events) and
audio/ (AudioRuntime); these mirror their contracts exactly.
"""
from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path
from typing import AsyncIterator, Literal

from contracts import (
    INPUT_CHUNK_BYTES,
    Direction,
    InputAudioChunk,
    LiveSessionConfig,
    OutputAudioChunk,
    OutputChannel,
    SessionEvent,
)


def _now() -> int:
    return time.monotonic_ns()


async def fixture_source(
    wav_path: str | Path,
    source: Literal["left", "right", "fixture"] = "fixture",
    realtime: bool = True,
) -> AsyncIterator[InputAudioChunk]:
    """Yield a 16 kHz mono WAV as 100 ms InputAudioChunks, optionally real-time paced."""
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16_000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes())
    seq = 0
    for off in range(0, len(pcm), INPUT_CHUNK_BYTES):
        chunk = pcm[off:off + INPUT_CHUNK_BYTES]
        if len(chunk) < INPUT_CHUNK_BYTES:
            chunk = chunk + b"\x00" * (INPUT_CHUNK_BYTES - len(chunk))  # pad final frame
        yield InputAudioChunk(pcm_s16le=chunk, seq=seq, source=source,
                              t_capture_ns=_now(), source_frame0=seq * 1600)
        seq += 1
        if realtime:
            await asyncio.sleep(0.1)


async def silent_source(
    n_chunks: int, source: Literal["left", "right", "fixture"] = "fixture"
) -> AsyncIterator[InputAudioChunk]:
    for seq in range(n_chunks):
        yield InputAudioChunk(pcm_s16le=b"\x00" * INPUT_CHUNK_BYTES, seq=seq, source=source,
                              t_capture_ns=_now(), source_frame0=seq * 1600)
        await asyncio.sleep(0)


async def fake_live_events(
    config: LiveSessionConfig,
    source: AsyncIterator[InputAudioChunk],
) -> AsyncIterator[SessionEvent]:
    """Drop-in fake for live_events: echoes input as 'translated' audio + canned transcripts.

    No network. For each input chunk it emits one 24k 'audio' event (the same bytes, so output
    is audible nonsense but the plumbing is exercised) and periodic transcript events. Emits a
    final status='turn_complete' so consumers that wait for completion don't hang.
    """
    seq = 0
    n_in = 0
    async for chunk in source:
        n_in += 1
        # Pretend the model returned ~1.5x the audio (24k vs 16k) - reuse the bytes.
        yield SessionEvent(
            direction=config.direction, kind="audio", t_ns=_now(),
            audio=OutputAudioChunk(pcm_s16le=chunk.pcm_s16le, direction=config.direction,
                                   seq=seq, t_received_ns=_now()))
        seq += 1
        if n_in % 10 == 0:
            yield SessionEvent(direction=config.direction, kind="input_transcript",
                               t_ns=_now(), text="[fake input] ")
            yield SessionEvent(direction=config.direction, kind="output_transcript",
                               t_ns=_now(), text="[fake output] ")
    yield SessionEvent(direction=config.direction, kind="status", t_ns=_now(),
                       detail="turn_complete")


class FakeAudioRuntime:
    """In-memory AudioRuntime: input_source replays a fixture; enqueue_output records bytes."""

    def __init__(self, left_wav: str | Path | None = None,
                 right_wav: str | Path | None = None, realtime: bool = False) -> None:
        self._wavs = {"left": left_wav, "right": right_wav}
        self._realtime = realtime
        self.captured: dict[OutputChannel, bytearray] = {
            OutputChannel.LEFT: bytearray(), OutputChannel.RIGHT: bytearray()}
        self.dropped = 0

    def input_source(self, source: Literal["left", "right"]) -> AsyncIterator[InputAudioChunk]:
        wav = self._wavs.get(source)
        if wav is None:
            return silent_source(0, source=source)
        return fixture_source(wav, source=source, realtime=self._realtime)

    def enqueue_output(self, channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        self.captured[channel].extend(audio.pcm_s16le)
        return True
