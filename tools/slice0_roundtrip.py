"""Slice 0: prove the live round-trip on the real key (no hardware).

Streams a canned German WAV to gemini-3.5-live-translate-preview in real-time-paced 100 ms
chunks via send_realtime_input, parses the receive events (audio + input/output transcripts),
and writes the translated English audio to out_en.wav.

    python tools/slice0_roundtrip.py [fixture.16k.wav] [target_lang]

This is the canonical reference for translate_session.py's send/receive surface.
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL = "gemini-3.5-live-translate-preview"
INPUT_RATE = 16_000
CHUNK_FRAMES = 1_600            # 100 ms @ 16 kHz
CHUNK_BYTES = CHUNK_FRAMES * 2
OUTPUT_RATE = 24_000


def read_wav_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == INPUT_RATE, f"expected 16k, got {w.getframerate()}"
        assert w.getnchannels() == 1, "expected mono"
        assert w.getsampwidth() == 2, "expected 16-bit"
        return w.readframes(w.getnframes())


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def run(fixture: Path, target: str) -> int:
    pcm = read_wav_pcm(fixture)
    client = genai.Client(http_options=types.HttpOptions(api_version="v1beta"))
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target, echo_target_language=False),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    out_audio = bytearray()
    in_txt: list[str] = []
    out_txt: list[str] = []
    t_send0 = t_first = t_last = None

    async with client.aio.live.connect(model=MODEL, config=cfg) as session:

        async def sender():
            nonlocal t_send0
            t_send0 = time.monotonic()
            for off in range(0, len(pcm), CHUNK_BYTES):
                chunk = pcm[off:off + CHUNK_BYTES]
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}"))
                await asyncio.sleep(CHUNK_FRAMES / INPUT_RATE)  # real-time pace
            # signal end of speech so the model flushes the final clause
            await session.send_realtime_input(audio_stream_end=True)

        async def receiver():
            nonlocal t_first, t_last
            async for r in session.receive():
                sc = r.server_content
                if not sc:
                    continue
                if sc.model_turn:
                    for p in sc.model_turn.parts:
                        if p.inline_data and p.inline_data.data:
                            if t_first is None:
                                t_first = time.monotonic()
                            t_last = time.monotonic()
                            out_audio.extend(p.inline_data.data)
                if sc.input_transcription and sc.input_transcription.text:
                    in_txt.append(sc.input_transcription.text)
                if sc.output_transcription and sc.output_transcription.text:
                    out_txt.append(sc.output_transcription.text)

        send_task = asyncio.create_task(sender())
        try:
            await asyncio.wait_for(receiver(), timeout=30)
        except asyncio.TimeoutError:
            pass
        send_task.cancel()

    out = ROOT / "out_en.wav"
    write_wav(out, bytes(out_audio), OUTPUT_RATE)
    print(f"  fixture     : {fixture.name}  (target={target})")
    print(f"  input txt   : {''.join(in_txt)!r}")
    print(f"  output txt  : {''.join(out_txt)!r}")
    print(f"  out audio   : {len(out_audio):,} bytes 24k -> {out}  ({len(out_audio)/2/OUTPUT_RATE:.1f}s)")
    if t_send0 and t_first:
        print(f"  first-byte  : {(t_first - t_send0)*1000:.0f} ms after send-start (component latency)")
    ok = bool(out_audio) and bool(out_txt)
    print("  RESULT      :", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    fx = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tests/fixtures/de_morgen.16k.wav"
    tgt = sys.argv[2] if len(sys.argv) > 2 else "en"
    raise SystemExit(asyncio.run(run(fx, tgt)))
