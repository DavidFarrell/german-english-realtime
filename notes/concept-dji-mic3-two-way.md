# Concept: two-way DE<->EN with a DJI Mic 3 (David's idea)

David's product concept (16 Jun 2026), captured + assessed. The idea is his; the assessment
and flagged gotchas below are mine.

## The idea

- Hardware already owned: **DJI Mic 3** - 1 receiver + 2 transmitters (clip-on lavs).
- Plug the receiver into the laptop over **USB-C**. In **Stereo Mode** the receiver presents
  the two transmitters as separate channels: **TX1 -> left, TX2 -> right** (confirmed - DJI's
  own docs; Mono Mode would mix both into one channel, which we do NOT want here).
- One person wears each transmitter. e.g. **David (English) on the right, his German-speaking
  sister-in-law on the left.**
- An app splits the stereo input into two mono streams and spins up **two Gemini Live
  Translate sessions**, one per direction:
  - **Left channel (German speech) -> session with `target_language_code="en"`** -> English out.
  - **Right channel (English speech) -> session with `target_language_code="de"`** -> German out.
- Each person hears the other in their own language, in near real time, so they can just talk.
- UI: an Electron app where you plug in the receiver, it verifies it sees both channels, you
  assign which channel is German and which is English (i.e. which target each gets), and hit go.

## Honest assessment

**This is a good fit for the tech and very buildable.** It maps almost exactly onto the
documented "one session per target language" pattern (the same approach Google Meet uses, per
[[thursdai-insights]]). The genuinely elegant bit is using the DJI's stereo split as a clean
**two-speaker capture with hardware channel isolation** - that side-steps the "whose voice is
this" problem because each speaker is already on a known channel. Good instinct.

### What works straightforwardly
- **Input:** read the DJI receiver as a 2-channel USB audio device, deinterleave L/R into two
  16 kHz mono PCM streams (the model wants 16 kHz in). One stream per session.
- **Two sessions:** the starter already shows the single-session shape; this is just two of
  them, differing only in `target_language_code`. Auto language detection means we don't even
  have to assert "left is German" - though we can, to be safe.

### The real wrinkle to solve: the OUTPUT side (two listeners, not L/R of one headset)
- The input is elegant; the **listening** side is the underspecified part. Each person needs
  to hear ONLY the other's translation:
  - David (EN) needs the **German->English** session output.
  - Sister-in-law (DE) needs the **English->German** session output.
- Those are two outputs for two different people - you can't serve them as left/right of a
  single pair of headphones unless one person takes each earcup (fine for a quick demo, awkward
  for real use). For a real two-person setup you need **two output sinks** (e.g. a wired pair +
  a Bluetooth pair, or two BT earbuds), and the app routes each session's audio to its own
  device. Worth designing for from the start - it's the bit most likely to feel clunky.
- (If the goal is actually just David monitoring/learning - one person hearing both directions
  - then L/R of one headset is fine and this wrinkle disappears.)

### Other gotchas to plan for
- **Mic bleed / cross-talk:** two lavs in the same room each pick up both voices a bit. Lavs
  are close-mic so isolation is decent, but the model may occasionally translate the bleed.
  Mitigate with per-channel VAD/gating (only send a channel when its wearer is actually
  speaking) if it's a problem.
- **Acoustic feedback:** if translations play out of speakers, the mics re-capture them ->
  loop. Headphones/earbuds avoid this - which we need anyway for the two-listener design.
- **Latency:** German is verb-final, the model's stated worst case for context-wait latency
  ([[thursdai-insights]]). Measure DE->EN specifically; it may lag EN->DE.
- **Cost:** two sessions running continuously = double the token burn, and billing is
  continuous (no turns). Fine for short chats; pin down the rate before long sessions.
- **Voice:** output is a synthetic voice that can drift gender/tone - not voice cloning. Fine
  for utility.

## Suggested build path

1. **Prototype in Python first** (matches `../live_translate_starter.py` + the cookbook). Use
   `sounddevice`/`pyaudio` to open the DJI 2-channel input, deinterleave, run two asyncio Live
   sessions, and play each output to a chosen device. Fastest way to prove the core loop +
   measure real latency before investing in UI.
2. **Then wrap in Electron** for the nice UX (device picker, "which channel = which language",
   go button). Electron would use the JS SDK `@google/genai`; audio device routing is the
   fiddly part there, so proving it in Python first de-risks it.
3. Crib Thor's **LiveKit "Gemini Live Translate"** example for the multi-session orchestration
   even if we don't use LiveKit transport.

## Verdict
Concept is sound and well-matched to the tech. Build the Python core-loop prototype first to
prove latency + the DJI channel split; the one real design decision to make up front is
**how the two translated outputs reach two people** (two output devices vs sharing one headset
vs single-listener monitoring).
