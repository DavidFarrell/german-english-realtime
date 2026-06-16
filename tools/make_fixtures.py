"""Generate canned voice fixtures (German + English) as 16 kHz mono s16le WAV.

These are the deterministic inputs for Slice-0 round-trips, unit tests, and the latency harness.
Uses macOS `say` (offline) -> ffmpeg to 16k mono PCM. Run from the repo root:

    python tools/make_fixtures.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# (filename stem, macOS voice, text). Verb-final German lines included on purpose: the model's
# stated worst case for context-wait latency is where the verb falls late in the clause.
CLIPS = [
    ("de_morgen", "Anna", "Guten Morgen. Wie geht es Ihnen heute? Das Wetter ist heute sehr schoen."),
    ("de_verbfinal", "Anna", "Ich glaube, dass wir morgen das ganze Projekt zusammen fertig machen werden."),
    ("de_short", "Anna", "Hallo, ich heisse Debbie. Schoen dich kennenzulernen."),
    ("en_hello", "Daniel", "Hello, my name is David. It is very nice to meet you today."),
    ("en_verbfinal", "Daniel", "I think that tomorrow we will finish the whole project together."),
    ("en_short", "Daniel", "Good morning. How are you doing today? The weather is lovely."),
]


def make(stem: str, voice: str, text: str) -> Path:
    FIX.mkdir(parents=True, exist_ok=True)
    aiff = FIX / f"{stem}.aiff"
    wav = FIX / f"{stem}.16k.wav"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(wav)],
        check=True,
    )
    aiff.unlink(missing_ok=True)
    return wav


def main() -> int:
    for stem, voice, text in CLIPS:
        wav = make(stem, voice, text)
        size = wav.stat().st_size
        secs = (size - 44) / 2 / 16000
        print(f"  {wav.name:20s} {secs:4.1f}s  ({voice})")
    print(f"\n{len(CLIPS)} fixtures in {FIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
