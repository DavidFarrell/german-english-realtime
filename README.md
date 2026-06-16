# German-English-realtime

Exploring a real-time **German <-> English** speech translation tool, built on Google's
**Gemini 3.5 Live Translate** (announced 9 June 2026; runs on the Gemini Live API).

## Layout

- `live_translate_starter.py` - the AI Studio starter David pasted in: the Live API audio
  loop wired to the Live-Translate preview model. The runnable starting point. (Has a couple
  of known issues flagged in its docstring - e.g. an undefined `debug`, and it defaults to
  camera mode; use `--mode none` for audio-only.)
- `notes/`
  - `gemini-live-3-5-translate.md` - facts from the Google launch blog + confirmed API
    details (model id, config, audio formats, deps).
  - `thursdai-insights.md` - the deep technical source: ThursdAI's on-air test + interview
    with Thor Shafe (DeepMind). Architecture, latency, pricing, one-session-per-language, the
    LiveKit reference build. **Read this one.**
  - `video-demos-transcripts.md` - transcripts of the 4 videos embedded in the blog.
- `reference/` - saved external reference material (e.g. the cookbook quickstart).

## The one-paragraph state of play

The capability we want now exists as a single hosted speech-to-speech model
(`models/gemini-3.5-live-translate-preview`) on the Gemini Live API - no need to stitch
STT+MT+TTS. It auto-detects language, translates into a configured target, streams audio back
in <500ms, and returns input+output transcripts alongside. **A two-way DE<->EN tool = two
concurrent Live API sessions** (target `en` + target `de`) fed the shared mic, relying on
auto-detection. Start one-directional, then add the second session. German's verb-final word
order is the model's stated worst-case for latency, so measure DE->EN explicitly.

## Next steps (rough)

1. Get the starter running audio-only (`--mode none`, fix the `debug` ref, set
   `GEMINI_API_KEY`); confirm EN-target translation works.
2. Find Thor's "Gemini Live Translate" LiveKit example - crib the multi-session orchestration.
3. Add the second (de-target) session for two-way; test DE->EN and EN->DE latency.
4. Pin down pricing (continuous token burn, no turns) before anything long-running.
5. Decide the product shape with David (two-way conversation vs "listen to a talk in your
   language"); likely tied to the Luxembourg family / German context.
