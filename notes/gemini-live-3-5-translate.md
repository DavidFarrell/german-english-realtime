# Gemini 3.5 Live Translate - notes

**Source:** Google blog - <https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-live-3-5-translate/>
**Announced:** 9 June 2026
**Captured:** 16 June 2026

These are working notes for the German-English-realtime project, based on Google's
announcement. The headline: Google now has a near-real-time speech-to-speech translation
model exposed via the **Gemini Live API** - which is the relevant building block if we want
to build a live German<->English translation tool.

## What it is

- **Gemini 3.5 Live Translate** - an audio-in / audio-out streaming model for **near
  real-time speech-to-speech translation**.
- Covers **70+ languages** with **automatic language detection** (no manual "I'm speaking X"
  config). 70+ languages => 2000+ pairwise combinations. German and English both supported.
- It generates **speech**, not just text: continuous, natural-sounding translated audio that
  preserves the speaker's **intonation, pacing and pitch**, without awkward pauses.

## Why it matters for this project

- The capability we'd want for a German<->English live tool now exists as a hosted model -
  we don't have to stitch together separate STT -> MT -> TTS components and eat their
  combined latency. It's a single streaming audio model.
- Access is via the **Gemini Live API** (public preview) - so it's buildable now, not just a
  consumer feature locked inside Google's own apps.

## How to access (three tiers)

| Audience | Where | Status |
|----------|-------|--------|
| **Developers** | **Gemini Live API** + Google AI Studio | Public preview (build now) |
| Enterprises | Google Meet (built-in live translation) | Private preview, "this month" |
| General users | Google Translate app (Android + iOS) | Rolling out |

For us the developer tier is the one that matters: **Gemini Live API** (streaming, bidi
audio) via Google AI Studio.

## Technical specifics from the post

- **Modality:** audio-in -> audio-out, streaming.
- **Latency:** "just a few seconds behind the speaker"; described as "near real-time" - NOT
  instantaneous. Streaming is tuned to balance translation quality against lag.
- **Noise robustness:** designed to hold up in "unpredictable environments" (background
  noise, real rooms).
- **Multilingual input:** handles mixed/auto-detected languages without manual setup.
- **SynthID watermarking** on generated audio (detectability / provenance).
- **No pricing** disclosed in the announcement.

## Confirmed API details (from the AI Studio starter David provided + ThursdAI)

See `../live_translate_starter.py` (the actual AI Studio code) and [[thursdai-insights]].

- **Model id:** `models/gemini-3.5-live-translate-preview`. Try it live at
  <https://aistudio.google.com/live?model=gemini-3.5-live-translate-preview>.
- **API:** Gemini **Live API** - stateful WebSocket. `client.aio.live.connect(model, config)`,
  `api_version="v1beta"`, `GEMINI_API_KEY`. SDK: `google-genai`.
- **Translation is a config, not a separate model mode:** `LiveConnectConfig(...,
  translation_config=types.TranslationConfig(target_language_code="en"), response_modalities=["AUDIO"])`.
  So DE<->EN two-way = **two sessions**, one `target_language_code="en"`, one `"de"` (auto
  language detection handles which side is speaking).
- **Audio formats:** mic in = 16-bit PCM, **16 kHz** mono, mime `audio/pcm`; model audio out =
  **24 kHz**. Chunk size 1024.
- **Deps:** `pip install google-genai opencv-python pyaudio pillow mss` (cv2/mss only needed
  if you stream video frames; a pure audio translator can drop them and use `--mode none`).
- **Transcripts + audio both returned:** response stream carries `response.data` (audio
  chunks) and `response.text` (transcript) - the text is from parallel transcribers, not the
  translation path.
- **Latency:** <500ms, on-device-capable; the wait is mostly the model gathering sentence
  context, which depends on **verb position** -> relevant for German (verb-final). Measure
  DE->EN separately.

## Related notes
- [[thursdai-insights]] - Thor Shafe (DeepMind) deep-dive: architecture, pricing, latency,
  one-session-per-language, the LiveKit reference build.
- [[video-demos-transcripts]] - transcripts of the 4 embedded blog videos (incl. the live
  German<->English switch in the main demo).
- `../live_translate_starter.py` - the runnable AI Studio starter (with known-issues notes).

## Open questions to chase down (some now answered above)

- Exact **Gemini Live API** parameters for the translate behaviour - is "Live Translate" a
  distinct mode/model id, or a config on the existing Gemini Live audio model? Need to check
  the AI Studio / Live API docs.
- **Pricing** and quota for streaming audio (per-minute? per-token? audio in+out both
  billed?).
- Real measured **latency** for DE<->EN specifically, and how it degrades with noise / two
  people talking over each other.
- Whether the API exposes the **source transcript + target transcript** alongside the audio
  (useful for a UI that shows text too, like the consumer Translate app does).
- Voice control: can we pick / clone the output voice, or is it a fixed synthetic voice?
- Turn-taking / barge-in handling for a two-way conversation (vs one-directional
  "listen to a talk" use).

## Possible directions for German-English-realtime

(Speculative - to refine with David. Likely tied to David's Luxembourg family / German
context, or live-talk translation like the ThursdAI Gemini-translate segment he blogified.)

1. **Two-way conversation tool** - phone/laptop mic in, translated speech out, for DE<->EN
   live chat (e.g. family).
2. **One-way "listen to a German talk/meeting in English"** - capture system audio, stream to
   the Live API, play/caption the English.
3. Compare against the **Google Translate app** consumer experience to see what the API buys
   us over just using the app (custom UI, captions, logging, integration into other tools).

## Next step

Read the Gemini Live API docs in AI Studio to pin down the exact model id / mode, streaming
setup, and pricing - then decide which of the directions above to prototype first.
