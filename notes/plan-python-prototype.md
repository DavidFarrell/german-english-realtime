# Implementation plan - Python prototype (v2, post-GPT-5 review)

**Goal:** a single-machine Python prototype that turns a DJI Mic 3 (USB-C, Stereo Mode =>
TX1=left, TX2=right) into a two-way German<->English live translator on Gemini 3.5 Live
Translate, and **measures real latency** (German verb-final = stated worst case) BEFORE any
Electron UI. Concept: [[concept-dji-mic3-two-way]]. API facts: [[gemini-live-3-5-translate]],
[[thursdai-insights]].

> This is v2. It folds in a GPT-5 adversarial review of v1 (and facts I verified locally).
> The changelog at the bottom records what changed and why.

## Verified facts (checked locally, 16 Jun 2026)

- **The installed `google-genai` is 1.56.0 and has NO `TranslationConfig` and no
  `translation_config` field on `LiveConnectConfig`.** The AI Studio starter
  (`../live_translate_starter.py`) therefore **cannot even construct its CONFIG** in this env.
  Latest on PyPI is **2.8.0**. => SDK upgrade (or raw-WebSocket fallback) is step one.
- **`send_realtime_input` DOES exist** on the async live session even in 1.56.0 - so the
  realtime-audio send path is `session.send_realtime_input(...)`, NOT the deprecated
  `session.send(...)` the starter uses.
- DJI Mic 3 Stereo Mode => TX1 left / TX2 right over USB-C (confirmed earlier from DJI docs).

## Definition of done (prototype)

1. Two people on the two DJI transmitters hold a DE<->EN conversation, each heard in their own
   output device, near real time.
2. **Measured latency** for DE->EN and EN->DE (component latency at minimum; physical
   mouth-to-ear if the rig is feasible), with the verb-final hypothesis tested with numbers.
3. The riskiest unknowns (SDK surface / preview model / transcripts; DJI channel split; two
   concurrent sessions + quota) are proven, not assumed.
4. No secrets in the repo; `.env` (gitignored) holds the key.

## Correct SDK surface (the bits v1 got wrong)

- **Config:** after upgrading, build `LiveConnectConfig(response_modalities=["AUDIO"],
  translation_config=types.TranslationConfig(target_language_code="en"))`. If the upgraded
  *python* SDK still lacks `TranslationConfig`, fall back to a **raw WebSocket** client setting
  `generationConfig.translationConfig` (the REST/WS schema has it). Decide this in Slice 0.
- **Send (realtime audio):** `await session.send_realtime_input(audio=types.Blob(data=pcm,
  mime_type="audio/pcm;rate=16000"))`. Raw **16-bit little-endian PCM, 16 kHz mono**, in
  **~100 ms chunks** (Google's recommendation), NOT one big blob and NOT faster than realtime.
- **Receive:** iterate `session.receive()`; for each event handle **all** fields (do not
  `continue` after audio):
  - audio out: `response.server_content.model_turn.parts[].inline_data.data` (24 kHz PCM).
  - text: `input_transcription` / `output_transcription` (must be enabled in config; treat as
    **diagnostic only**, not the translation path).
  - control: handle `go_away`, turn/session-resumption events.
- **Model id:** normalise - try both `gemini-3.5-live-translate-preview` and the
  `models/...`-prefixed form against `client.aio.live.connect()` after upgrade; keep whichever
  the SDK accepts.
- **Key:** standardise on `GEMINI_API_KEY` (note the SDK also reads `GOOGLE_API_KEY`; set only
  one to avoid precedence confusion).
- **Transcripts decision:** use the Live API's **built-in input/output transcription** config
  for the prototype (diagnostic text). Do NOT spin up separate transcriber sessions (that's
  what the consumer product does; unnecessary cost/complexity here).

## Architecture (hardened)

```
DJI receiver (USB, 2ch @48k)  ──InputStream callback (PortAudio thread)──┐
  callback does NOTHING heavy: just copies the (frames,2) int16 block    │
  into two per-channel bounded ring buffers (drop-oldest + overflow ctr) │
        ┌──────────────────────────────────────────────────────────────┘
        ▼  (worker coroutine per channel, woken via loop.call_soon_threadsafe)
  streaming resampler 48k→16k  (PERSISTENT filter state across blocks; soxr stream
        ▼                       or scipy with zi) → 100ms int16 mono chunks
  session_A.send_realtime_input (target=en)        session_B.send_realtime_input (target=de)
        ▼ receive: model_turn audio (24k) + transcription
  per-session output jitter buffer  → OutputStream CALLBACK (zero-fill on underrun,
        ▼                              resample 24k→device-rate if device won't open at 24k)
  device 1 (English listener)                      device 2 (German listener)
```

- asyncio `TaskGroup`. PortAudio callbacks run on their own threads and must never block,
  allocate heavily, or resample - they only move bytes into/out of bounded buffers. Bridge to
  asyncio via `loop.call_soon_threadsafe`.
- **Backpressure/drop policy explicit:** bounded ring buffers; when input is late, drop oldest
  and bump an overflow counter (logged). Output uses a jitter buffer + zero-fill on underrun;
  log queue depth.
- **Resampling is streaming** (filter state preserved across callbacks) - not independent
  per-buffer resample (which clicks at boundaries).
- **Output rate:** many devices won't open at 24 kHz; query device preferred rate and resample
  the model output to it. Independent output devices have independent clocks => no
  sample-perfect sync (fine: two different listeners).
- **Resilience:** handle dropped WS / `go_away` / session renewal with reconnect-and-resume on
  each session.

## Tech choices

- **Audio I/O: `sounddevice`** (PortAudio) for device enumeration-by-name, numpy buffers, and
  output callbacks. Persist device **names + host API**, not raw indices (indices shift).
- **Resampling: `soxr`** in streaming mode (preferred) or `scipy.signal` with retained `zi`.
- **SDK: `google-genai` upgraded** to a `TranslationConfig`-capable version (verify; pin it).
  Raw-WS fallback only if needed.
- **Secrets:** `python-dotenv` + gitignored `.env`; commit `.env.example`.
- **Env:** `uv` + `pyproject.toml` with a pinned google-genai.

## File layout

```
app.py                  # CLI + asyncio orchestration
src/config.py           # device names+hostapi, channel↔lang↔output map, rates, model id
src/audio_io.py         # enumerate; input callback+ring buffers; streaming resamplers; output sinks
src/translate_session.py# one Live session: connect, send_realtime_input, receive parse, reconnect
src/latency.py          # latency harness (paced canned clips; optional loopback-rig analysis)
clips/                  # canned DE/EN sentences (verb-final ones included); gitignored if large
.env.example
pyproject.toml
```

## Build slices (re-ordered per GPT-5)

**Slice 0 - SDK / API schema spike (NO hardware).**
- Fresh venv; `pip install -U google-genai`; assert `types.TranslationConfig` exists and
  `translation_config` is a `LiveConnectConfig` field. If not, switch to the raw-WS fallback
  here and prove it.
- Connect ONE session to the preview model; enable input/output transcription; feed a canned
  **German wav** as **paced 100 ms 16k-mono chunks** via `send_realtime_input`; parse
  `server_content.model_turn.parts[].inline_data.data`; save `out_en.wav`; print transcripts.
- **Acceptance:** out_en.wav is intelligible English of the German input. Proves SDK version,
  preview model id, config schema, send API, receive event shape, audio formats - hardware-free.

**Slice 1 - Audio hardware spike (NO model).**
- `--list-devices` (names + host API). Plug in DJI; confirm in **Audio MIDI Setup** it's a
  true **2-ch 48 kHz** input (not safety-track/mono/mixed). Record 5 s of both channels to a
  stereo wav; verify by ear/scope that **TX1=ch0=left, TX2=ch1=right**. Open each intended
  **output** device (prefer **wired USB DACs / an interface**, not two Bluetooth earbuds).
- **Acceptance:** confirmed 2-ch capture with known channel→speaker mapping, and two output
  devices that open and play a test tone.

**Slice 2 - One-direction live path (wired output).**
- Left channel (German) → streaming resample → session(en) → jitter buffer → wired output.
- **Acceptance:** speak German into TX1, hear English in near real time. Proves the full
  real-time loop + thread↔asyncio bridge + streaming resampler + output callback.

**Slice 3 - Latency harness for ONE direction (before concurrency).**
- Paced canned clips (100 ms, real-time): log send-start, first-output-byte, last-output-byte
  => component latency (label clearly: EXCLUDES mic-capture + playout hardware). Then, if
  feasible, a **physical loopback rig**: play source clip through an earbud coupled to a DJI
  TX, record the translated listener output and the source on one clock, forced-align, and
  measure **"disambiguating word spoken" -> "target equivalent audible"** (the meaningful
  metric for verb-final German), not first syllable.
- **Acceptance:** DE->EN vs EN->DE latency table with the methodology's limits stated.

**Slice 4 - Two concurrent sessions, then full two-way.**
- First prove **two canned real-time streams concurrently** (quota + WS stability). Then wire
  the real DJI right channel → session(de) → second output. Channel↔lang↔output in config/CLI;
  single-output fallback (A→L, B→R of one device) with a warning.
- **Acceptance:** real two-way DE<->EN exchange, each heard in their own device; no quota/WS
  failures over a multi-minute session.

**Slice 5 - Robustness polish.**
- Per-channel energy gate / simple VAD (cut mic bleed + idle token burn); live transcript
  print; reconnect on `go_away`/WS drop with session resumption; config persistence; graceful
  Ctrl-C closing streams + sessions.

## Risks / unknowns (status)

1. **SDK surface (HIGH):** installed 1.56.0 lacks `TranslationConfig`. Mitigation: upgrade +
   verify, else raw-WS fallback. RETIRED by Slice 0.
2. **Preview model gating (HIGH):** is `gemini-3.5-live-translate-preview` reachable on
   David's key? RETIRED by Slice 0. (Needs a key with access - the one ask of David.)
3. **DJI channel reality on macOS (MED):** true 2-ch 48k? channel order? RETIRED by Slice 1.
4. **Two concurrent sessions + quota (MED):** proven early in Slice 4 with canned streams.
5. **Latency definition (MED):** component proxy + optional physical rig; confounds (VAD
   edges, BT codec latency, underruns, resampler group delay, warm vs cold connection, model
   leading silence, semantic delay on long German clauses) documented in results.
6. **Realtime audio plumbing (MED):** bounded buffers, drop policy, streaming resampler,
   output jitter buffer, no work in callbacks - designed in above.
7. **Echo/feedback (LOW):** mandate headphones/earbuds; out of scope to cancel.
8. **macOS perms/devices:** terminal mic permission; persist device names+host API; avoid BT
   HFP mic downgrade; prefer wired DACs for measurement.

## Not in the prototype
Electron/GUI; voice cloning (model doesn't); LiveKit transport (but read Thor's "Gemini Live
Translate" LiveKit example for multi-session orchestration shape).

## First action
Slice 0. Need from David: a `GEMINI_API_KEY` with access to
`gemini-3.5-live-translate-preview`. Everything in Slice 0/1 is otherwise buildable now.

---

## Changelog: v1 -> v2 (what the GPT-5 review changed)

1. **SDK reality check up front.** v1 assumed the starter's `translation_config` + `session.send`
   + `response.data/.text` worked. Verified locally: installed `google-genai` 1.56.0 has no
   `TranslationConfig`; `send_realtime_input` is the real send API. v2 makes "upgrade/verify SDK
   or raw-WS fallback" the first step and corrects the send/receive surface
   (`send_realtime_input` + `server_content.model_turn.parts` + separate transcription fields).
2. **100 ms real-time-paced chunks** for both live audio and the latency harness (never send
   faster than realtime).
3. **Architecture hardened:** bounded ring buffers + explicit drop policy + overflow counters;
   streaming resampler with persistent filter state; output via callback with jitter buffer +
   zero-fill; resample model 24k output to device rate; no heavy work in PortAudio callbacks;
   reconnect/`go_away` handling.
4. **Latency methodology fixed:** the send->first-output metric is labelled component-only;
   added a physical loopback-rig method and the "disambiguating word -> target audible" metric
   for verb-final German; confounds listed.
5. **Slices re-ordered:** Slice 0 split into (0) SDK/API spike and (1) audio-hardware spike;
   one-direction latency harness moved BEFORE two-way concurrency; two-session quota/stability
   proven with canned streams before full hardware.
6. **macOS specifics:** persist device names + host API (not indices); prefer wired USB DACs
   over two Bluetooth earbuds (HFP downgrade, latency, clock drift); confirm DJI true 2ch 48k
   in Audio MIDI Setup; independent output clocks => no sample-perfect sync.
7. **Transcript ownership decided:** use the Live API's built-in input/output transcription
   (diagnostic), not separate transcriber sessions, for the prototype.
