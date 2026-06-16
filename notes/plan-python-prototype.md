# Implementation plan - Python prototype (v4, + parallel/ultracode build strategy)

**Goal:** a single-machine Python prototype that turns a DJI Mic 3 (USB-C, Stereo Mode =>
TX1=left, TX2=right) into a two-way German<->English live translator on Gemini 3.5 Live
Translate, and **measures real latency** (German verb-final = stated worst case) BEFORE any
Electron UI. Concept: [[concept-dji-mic3-two-way]]. API facts: [[gemini-live-3-5-translate]],
[[thursdai-insights]].

> v4 history: v1 (my plan) -> GPT-5 review -> v2 -> web research + AirPods -> v3 -> parallel
> multi-agent ("ultracode") build strategy -> v4. Changelog at the bottom.
>
> **For the ultracode instance:** read "## Building with ultracode / parallel subagents" near
> the end first - it tells you what to fan out, what is a hard barrier, and what CANNOT be
> done by agents (hardware/human gates). The contracts you need to fan out against are already
> verified and in this doc (see "Correct SDK surface" + "Architecture").

## Verified facts (checked locally + against Google docs, 16 Jun 2026)

- **SDK fix is concrete: pin `google-genai>=2.8.0`.** The installed 1.56.0 had NO
  `TranslationConfig` (so the AI Studio starter can't construct its config). I installed 2.8.0
  in a clean venv and confirmed it HAS: `types.TranslationConfig` (fields
  `target_language_code`, `echo_target_language`), and `LiveConnectConfig` fields
  `translation_config`, `input_audio_transcription`, `output_audio_transcription`, plus
  `types.AudioTranscriptionConfig`. **=> No raw-WebSocket fallback needed; the Python SDK
  supports everything.** (Raw-WS demoted to contingency.)
- **`send_realtime_input` is the real send API** (exists even in 1.56). The starter's
  `session.send(...)` is deprecated.
- **Per Google docs (authoritative):**
  - model `gemini-3.5-live-translate-preview`; **API-key only - NOT on Vertex yet.**
  - input **16-bit PCM, 16 kHz, mono, little-endian**; output **24 kHz mono PCM**; send in
    **100 ms chunks**.
  - `translationConfig`: `targetLanguageCode` (BCP-47, default `en`), `echoTargetLanguage`
    (bool, default false - keep false: only emit when translation is actually needed).
  - transcripts: enable `inputAudioTranscription` / `outputAudioTranscription`; read
    `serverContent.inputTranscription.text` / `outputTranscription.text`; audio at
    `serverContent.modelTurn.parts[].inlineData.data`.
  - **translation is unidirectional per session** ("the model acts as an interpreter") =>
    two-way needs **two sessions** (confirms the design).
  - documented limits: **audio-only input** (no text); **voice replication inconsistent**;
    **language detection struggles with heavy accents and similar languages**; **background
    audio not fully filtered**; **all output carries a non-removable SynthID watermark**.
  - **public preview**: no SLA / stable pricing / GA; expect the API to shift.
- DJI Mic 3 Stereo Mode => TX1 left / TX2 right over USB-C (DJI docs).

## Output design (UPDATED - David's AirPods decision)

**One AirPod per person.** This actually SIMPLIFIES the output side: the AirPods are a single
**stereo Bluetooth output device**, and the two buds are physically separate (no shared
band/cable). So:

- Route **session A output (German->English) -> LEFT channel -> David's left AirPod**.
- Route **session B output (English->German) -> RIGHT channel -> sister-in-law's right AirPod**.
- Each person wears one bud and hears only their own language. One output device, stereo,
  L/R split in code. No need for two separate output devices after all.

**But two Bluetooth-specific gotchas (baked into the plan):**

1. **Keep the AirPods OUTPUT-ONLY (A2DP).** The microphones are the DJI transmitters. If macOS
   selects an AirPod as the *input* device, it flips the AirPods into **HFP** mode -> the
   output collapses to **mono + low quality** (both buds get the same muddy stream), killing
   the L/R split. So: explicitly select the DJI as input, AirPods as output only, and never
   let the app open the AirPod mic.
2. **Bluetooth output latency (~150-300 ms, AAC) eats the <500 ms budget.** This directly
   undercuts the headline latency. The latency harness MUST compare **wired output vs AirPods**
   so we know the real end-to-end number David will experience. (Wired USB-C earbuds / a splitter
   are the low-latency fallback if AirPods latency is unacceptable.)

## Correct SDK surface (concrete, verified)

```python
# pip install "google-genai>=2.8.0"  (verified to expose the types below)
from google import genai
from google.genai import types

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  # API key only; not Vertex yet
MODEL = "gemini-3.5-live-translate-preview"                   # confirm prefix vs models/ at runtime

def cfg(target):  # one per direction
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target, echo_target_language=False),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

# send: 100ms, 16k mono LE PCM
await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

# receive: handle ALL fields per event (don't `continue` after audio)
async for r in session.receive():
    sc = r.server_content
    if sc and sc.model_turn:
        for p in sc.model_turn.parts:
            if p.inline_data: out_audio(p.inline_data.data)   # 24k PCM
    if sc and sc.input_transcription:  log_in(sc.input_transcription.text)
    if sc and sc.output_transcription: log_out(sc.output_transcription.text)
    # also handle go_away / session resumption
```

## Architecture (hardened; unchanged from v2 except output is now one stereo device)

```
DJI receiver (USB, 2ch @48k) ──InputStream callback (PortAudio thread; copies only)──┐
   ch0 (L=TX1=German)  → bounded ring buffer A → streaming resample 48k→16k → 100ms chunks → session_A(target=en)
   ch1 (R=TX2=English) → bounded ring buffer B → streaming resample 48k→16k → 100ms chunks → session_B(target=de)
   each session.receive → 24k audio + transcripts
   session_A audio → jitter buffer → LEFT  of the AirPods stereo OutputStream  (David)
   session_B audio → jitter buffer → RIGHT of the AirPods stereo OutputStream  (sister-in-law)
```

- asyncio `TaskGroup`; PortAudio callbacks never block/allocate/resample (copy bytes only),
  bridge via `loop.call_soon_threadsafe`.
- bounded ring buffers + drop-oldest + overflow counters; **streaming** resampler (persistent
  filter state via `soxr` stream or `scipy` `zi`); output callback with jitter buffer +
  zero-fill on underrun + queue-depth logging; resample 24k->device rate if needed.
- reconnect/`go_away`/session-resumption per session.

## Tech choices
- **`sounddevice`** (PortAudio): enumerate by name+host API (persist those, not indices);
  one stereo output stream for the AirPods; explicit DJI input selection.
- **`soxr`** streaming resampler (or scipy with retained `zi`).
- **`google-genai>=2.8.0`** pinned (verified). Raw-WS = contingency only.
- secrets: `python-dotenv` + gitignored `.env` (`GEMINI_API_KEY`; set only this, not also
  `GOOGLE_API_KEY`). `uv` + `pyproject.toml`.

## Web research findings (others building on this API)
- **Availability:** API-key only; **Vertex AI not supported yet** (dev-forum request; Google
  redirected to sales). Plan around AI Studio / Gemini API keys.
- **Transport SDKs** for scaling/real apps: LiveKit, Pipecat, Agora, Fishjam, Vision Agents -
  "your code becomes session management + UX, not signal processing." Not needed for a
  single-machine prototype, but the route for the eventual product (Thor used LiveKit).
- **SynthID watermark** is embedded in ALL output audio and non-removable - note for any
  recordings we keep.
- **Accents / similar languages:** docs explicitly flag detection struggles with heavy accents
  and similar languages, and that background audio isn't fully filtered. David's sister-in-law
  is a real German speaker with a real accent, and two lavs share a room -> **validate against
  her actual voice early**, not just clean canned clips.
- **Ephemeral tokens (future Electron client):** must use **v1alpha** endpoint and put
  `translationConfig` in the token-creation constraints server-side.
- **Preview maturity:** no SLA, pricing not final, API surface will shift; Grab piloted at
  ~10M calls/month, but Google says validate latency/quality against your own conditions.
- **Operational limits (session length caps, concurrency/quota, pricing) are NOT publicly
  documented** - so we must measure them ourselves (two concurrent sessions + a multi-minute
  run are explicit acceptance gates below).

## Build slices

**Slice 0 - SDK / API schema spike (NO hardware).** Fresh venv; `pip install "google-genai>=2.8.0"`;
connect ONE session to the preview model with translation + transcription config; feed a canned
**German wav** as paced 100 ms 16k-mono chunks via `send_realtime_input`; parse
`server_content.model_turn.parts[].inline_data.data` + transcripts; save `out_en.wav`.
*Acceptance:* intelligible English out + transcripts. (SDK risk already largely retired by the
2.8.0 check; this proves the live round-trip + that David's key can reach the preview model.)

**Slice 1 - Audio hardware spike (NO model).** `--list-devices` (names+host API). Confirm DJI is
true **2-ch 48 kHz** in Audio MIDI Setup; record both channels; verify TX1=ch0=L / TX2=ch1=R.
Open the **AirPods as a stereo output** and prove independent L vs R tones reach the two buds.
Explicitly confirm macOS is NOT using an AirPod as input (no HFP collapse). *Acceptance:*
2-ch capture with known mapping + verified independent L/R playout on the two AirPods.

**Slice 2 - One-direction live path.** Left channel (German) → resample → session(en) → LEFT
AirPod. *Acceptance:* speak German into TX1, hear English in the left bud, near real time.

**Slice 3 - Latency harness for ONE direction, WIRED vs AIRPODS.** Paced canned clips (100 ms,
real-time): log send-start / first-output-byte / last-output-byte = component latency (label:
excludes capture + playout). Then a **physical loopback rig**: source clip through an earbud
coupled to a DJI TX; record translated output + source on one clock; forced-align; measure
**"disambiguating word spoken -> target audible"** (the meaningful metric for verb-final
German). **Run it twice: wired output and AirPods**, to quantify the Bluetooth latency tax.
*Acceptance:* DE->EN vs EN->DE latency table, wired vs AirPods, methodology limits stated.

**Slice 4 - Two concurrent sessions, then full two-way.** First prove **two canned real-time
streams concurrently** (quota + WS stability + session-length). Then wire the real DJI right
channel → session(de) → RIGHT AirPod. *Acceptance:* real two-way DE<->EN over a multi-minute
session, each in their own bud, no quota/WS failures; **test with the sister-in-law's real
German accent**, not just canned clips.

**Slice 5 - Robustness polish.** Per-channel energy gate / VAD (cut mic bleed + idle token
burn); live transcript print; reconnect on `go_away`/WS drop with resumption; config persist;
graceful shutdown.

## Risks / unknowns (status)
1. SDK surface (HIGH) - RETIRED: 2.8.0 verified to have everything; pin it.
2. Preview model gating on David's key (HIGH) - Slice 0. Needs a key with preview access.
3. **Bluetooth latency tax (HIGH, new)** - AirPods may blow the <500 ms feel; Slice 3 measures
   wired vs AirPods; wired earbuds are the fallback.
4. **AirPods forced into HFP/mono (MED, new)** - keep output-only, never open AirPod mic; Slice 1.
5. DJI channel reality on macOS (MED) - Slice 1.
6. Two concurrent sessions + quota + session-length (MED) - Slice 4 (undocumented; we measure).
7. Accent/bleed degrading detection (MED) - validate with the real speaker early (Slice 4).
8. Realtime plumbing (MED) - bounded buffers, streaming resampler, jitter buffers (designed in).
9. SynthID watermark on recordings (LOW) - note it; fine for a prototype.

## Not in the prototype
Electron/GUI; voice cloning (model can't); LiveKit/Pipecat transport (single machine) - but
that's the route for the product, and ephemeral-token + v1alpha is the client-auth path.

## First action
Slice 0, on a fresh venv with `google-genai>=2.8.0`. Only blocker: a `GEMINI_API_KEY` with
access to `gemini-3.5-live-translate-preview` (API-key, not Vertex).

---

## Building with ultracode / parallel subagents

This section is for the fresh Claude Code instance that will implement this with the dynamic
multi-agent workflow approach. The honest summary: **the code is very parallelizable, but the
proof is not** - validation is hardware- and human-gated and stays sequential.

### The key enabler: contracts are already known, so fan out immediately

Normally you'd have to run a sequential spike before you know the API shape. Here the SDK
surface is **already verified** (`google-genai>=2.8.0`: `send_realtime_input`,
`server_content.model_turn.parts[].inline_data`, `TranslationConfig`, transcription configs;
16k-in/24k-out/100 ms - see "Correct SDK surface"). So you can write a **skeleton commit that
fixes the module interfaces first**, then fan out implementation against those frozen
contracts. Contract-first is what makes the parallelism safe (agents don't diverge).

### Dependency graph (what's parallel vs sequential vs human-only)

```
[Phase 1: SKELETON]  one agent, BARRIER
   define interfaces + dataclasses (config), module APIs, the in-memory PCM contract
   (bytes, 16k mono LE in / 24k mono LE out), and a fakeable boundary for the Live session
        │
        ▼
[Phase 2: PARALLEL IMPLEMENT]  fan out, worktree-isolated, ~4 independent agents
   A) translate_session.py  - Live API wrapper; testable vs the API with canned wav, no audio HW
   B) audio_io.py           - device enum, input callback+ring buffers, streaming resampler,
                              output jitter buffer; testable with synthetic/loopback audio, no API
   C) latency.py            - paced-clip harness + forced-alignment analysis; against the contracts
   D) fixtures/scaffolding  - canned DE/EN clips (incl. verb-final sentences), .env.example,
                              pyproject pin, --list-devices, CLI skeleton in app.py
        │   (A and B are the two big ones and are fully independent - different files, different
        │    test rigs: A needs a key but no hardware; B needs neither)
        ▼
[Phase 3: INTEGRATE + ADVERSARIAL VERIFY]  one integrator, then a verify panel
   integrate app.py wiring (join/barrier); then parallel verifiers, each a distinct lens:
     - SDK-contract verifier: does send/receive match the LIVE docs TODAY (preview = shifting)?
     - audio-correctness verifier: streaming resampler keeps filter state? buffers bounded +
       drop policy? output zero-fills on underrun? no work in PortAudio callbacks?
     - concurrency verifier: two sessions, backpressure, go_away/reconnect, no deadlock
        │
        ▼
[Phase 4: HARDWARE + HUMAN GATES]  *** NOT agent work - sequential checkpoints for David ***
   Slice 1 (real DJI 2ch + AirPods A2DP/no-HFP), Slice 3 (real latency rig, wired vs AirPods),
   Slice 4 (real two-way + sister-in-law's German accent). Agents PREPARE these (harness, runbook,
   logging); a human RUNS them on the physical machine.
```

### Concrete fan-out for Phase 2 (low conflict by construction)
- Each agent owns **one file** -> use **worktree isolation** so each can run its own checks
  without stepping on the others, then merge. (app.py is the only shared file; it belongs to
  the Phase-3 integrator, not Phase 2.)
- Give each agent: the frozen interface from Phase 1, the relevant section of this plan, and a
  self-contained acceptance test it must pass (A: canned-wav round-trip; B: synthetic-audio
  resample+route with assertions on rate/channels/buffer bounds; C: deterministic timestamp
  math on a fake stream).

### What ultracode buys here, and what it does NOT
- **Buys:** A and B (the bulk of the code) built concurrently; a real adversarial verify pass
  on the two genuinely risky areas (the preview SDK surface, and the realtime audio plumbing)
  using multiple lenses instead of one generate-and-hope; research/fixtures in parallel.
- **Does NOT buy:** any shortcut through the hardware/human gates. Mic capture, AirPods
  behaviour, real latency, and accent robustness are physical and must be measured by David on
  the device. Don't let a workflow "declare success" on those - they're checkpoints, not tasks.
- **Caution:** don't over-fan-out the trivial parts (config/CLI) - the win is concentrating
  agent effort on (1) the audio plumbing correctness and (2) verifying the preview API against
  live docs, because those are where this breaks. Spend the parallel budget on verification,
  not on splitting a 40-line config file four ways.

### Suggested workflow phases (maps to the graph)
1. **Skeleton** (1 agent, barrier) -> 2. **Implement** (parallel, worktree, ~4 agents) ->
   3. **Integrate + verify** (1 integrator + a 3-lens verify panel) -> stop and hand the
   hardware runbook to David for the Phase-4 gates. Re-enter a small workflow afterwards for
   Slice 5 polish if wanted.

---

## Changelog
**v1 -> v2 (GPT-5 review):** corrected the deprecated `session.send`/`response.data` to
`send_realtime_input` + `server_content.model_turn.parts`; 100 ms real-time-paced chunks;
hardened audio plumbing (bounded buffers, streaming resampler, jitter buffers); real
mouth-to-ear latency rig + verb-final metric; re-ordered slices (SDK spike vs hardware spike
split; one-direction latency before two-way; two-session quota proven with canned streams);
macOS device/clock specifics. Flagged installed `google-genai` 1.56.0 lacks `TranslationConfig`.

**v3 -> v4 (parallel / ultracode build strategy):** added "Building with ultracode / parallel
subagents" - a contract-first fan-out (skeleton barrier -> ~4 worktree-isolated implementers,
the two big independent ones being the Live-session wrapper and the audio I/O -> integrate +
a 3-lens adversarial verify panel), with an explicit, honest line that the hardware/human gates
(real DJI+AirPods, latency rig, accent test) CANNOT be done by agents and stay sequential
checkpoints. Steer the parallel budget at audio-plumbing correctness + verifying the preview
API against live docs, not at splitting trivial files.

**v2 -> v3 (web research + AirPods):**
- **SDK fix made concrete + verified:** `google-genai>=2.8.0` exposes `TranslationConfig`
  (`target_language_code`, `echo_target_language`) + `input/output_audio_transcription` +
  `AudioTranscriptionConfig`. Raw-WS fallback demoted to contingency; added a concrete code
  block with the real field names.
- **Output redesigned for AirPods (David's call):** one stereo BT device, English->left bud /
  German->right bud, one per person. Simpler than two devices. Added the two BT gotchas:
  keep A2DP output-only (don't trigger HFP mono collapse), and the ~150-300 ms BT latency tax
  -> Slice 3 now measures wired vs AirPods.
- **Web research folded in:** API-key only (no Vertex yet); transport-SDK options for scaling;
  non-removable SynthID watermark; documented limits (audio-only, inconsistent voice,
  accent/similar-language detection issues, imperfect background filtering); ephemeral
  tokens/v1alpha for the future client; preview = shifting API, undocumented session/quota
  limits we must measure ourselves; validate against the real German speaker's accent early.
