# Insights from ThursdAI (Thor Shafe, Google DeepMind) - 11 June 2026

The ThursdAI episode of 11 June 2026 tested Gemini 3.5 Live Translate live on air and
interviewed **Thor Shafe** (Google DeepMind, Developer Experience), the person who built the
public demo. This is the richest technical source we have - it goes well beyond the blog.

Full segment write-up (with the complete transcript) lives in the vault:
`Resources/Podcast Transcripts/ThursdAI/2026_06_11 - ThursdAI - Gemini 3.5 Live Translate.md`
Host: Alex Volkov. Segment timecodes 1:21:42-1:38:08.

## The technically important bits (not in the blog)

- **It's a new class of model, not a turn-taking conversational model.** It continuously
  streams as you speak - no "your turn / my turn". Consequence: **pricing has no input/output
  turn distinction** - you "continuously burn tokens" as audio flows. (Worth pinning down the
  exact billing unit before we build anything that runs for hours.)
- **True speech-to-speech, sound-token to sound-token - it never routes through text.** The
  model ingests source speech, understands it natively, and re-speaks it in the target
  language. The on-screen transcript is produced by **separate transcribers running in
  parallel** over BOTH the input and output audio - they are NOT in the translation path. So
  the API gives you: input transcript + output transcript + translated audio chunks.
- **Latency: under 500ms, on device.** Most of the delay is the model **waiting to understand
  sentence context** - and how much it waits depends on the language and **where the verb
  falls in the sentence.** This is the single most relevant line for us: **German is
  verb-final / verb-second**, so DE->EN latency behaviour is exactly the case Thor calls out.
  Worth measuring DE->EN vs EN->DE separately.
- **70 languages** (those meeting Google's internal benchmark) -> ~**2,000 combinations**. DE
  and EN both in.
- **Voice replication is attempted but not there yet** - it sometimes gets gender wrong or
  drifts tone. **This is NOT voice cloning.** Live Translate trades voice fidelity for
  real-time speed; ElevenLabs dub v2 is the opposite trade (great cloning, much slower).
- Handled **technical terms / proper nouns** well on a day-old model (recognised "Anthropic",
  "Fable 5"). One miss: didn't recognise "Yam" as a person's name and dropped it. So expect
  occasional name/entity drops.

## Architecture - the part that matters for a DE<->EN tool

- Runs on the **Gemini Live API** - a **stateful WebSocket** API. Stream audio in; get back
  transcript chunks + translated audio chunks; play them back near real time.
- **One session per target language.** For **bi-directional** (two speakers, two languages)
  or multi-party, you spin up a **unique session per target language**. That is exactly how
  Google Meet does it.
  - So our DE<->EN two-way tool = **two concurrent sessions**: one with
    `target_language_code="en"` (translates the German speaker into English) and one with
    `target_language_code="de"` (translates the English speaker into German). Auto language
    detection means we don't have to tell each session which side is speaking.
- Thor's reference build: **"Gemini Live Translate"** in the **Gemini Live examples**. One
  **LiveKit** room ingests the incoming audio; as each audience member picks a language, a new
  agent (= a Live API session) is spun up in the room to translate into that language.
  **LiveKit** is a Google real-time-media partner - worth using for scaling/transport rather
  than hand-rolling WebRTC. (Find this example to crib the multi-session orchestration.)

## How Thor demoed it (matches the AI Studio link David sent)

1. Google **AI Studio -> `/live`** (link: <https://aistudio.google.com/live?model=gemini-3.5-live-translate-preview>)
2. Select model: **Gemini 3.5 Live Translate (preview)**.
3. Share the **audio of a browser tab** (e.g. a live stream) as the source.
4. Pick the **target language**.
5. **"Echo target language"** toggle: model auto-detects spoken language and translates to
   target; echo lets you also hear target output when spoken == target (handy for confirming
   on air). He'd listened to the show's bio-threat segment in German this way.

## Use cases they raised (sanity-check our directions against these)

- **Grab** (SE-Asia ride-hailing) testing it for passenger<->driver calls across languages.
- **Live events / podcasts** - Alex: stream ThursdAI in every language; his mum listens in
  Russian. "Tune in and listen in your language" with no pre-selection (he called it a
  weekend project - the exact shape of an interesting build for us).
- **UN-style interpretation** is the famous case, but many live-translate needs don't require
  a human-interpreter level.
- The level they *couldn't* test on air: **each speaker hearing everyone else in their own
  language in their headphones** simultaneously - true flowing multi-language conversation.
  That's the aspiration for a polished DE<->EN tool.

## Direct implications for German-English-realtime

1. **Two-way DE<->EN = two Live API sessions** (en target + de target), both fed the shared
   mic(s), relying on auto language detection. Start one-directional (one session, like the
   starter script's `target_language_code="en"`), then add the second session.
2. **Measure DE->EN latency specifically** - verb-final German is the model's stated
   worst-case for context-wait latency.
3. **Don't expect voice cloning** - output is a generic-ish synthetic voice that may drift
   gender/tone. Fine for utility; not for "sounds like me".
4. **Look at the LiveKit "Gemini Live Translate" example** before building transport - it
   already does multi-session orchestration we'd otherwise reinvent.
5. **Pin down pricing** (continuous token burn, no turns) before anything long-running.
