# Implementation plan - Python prototype (v5, ultracode strategy hardened by GPT-5)

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
- **`google-genai==2.8.0`** pinned EXACTLY (verified working; a loose `>=` lets a fresh agent
  pull a future breaking preview SDK). Raw-WS = contingency only.
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
multi-agent workflow approach. The honest summary: **the code is parallelizable AFTER the
shared realtime contracts are frozen, but the proof is not** - validation is hardware- and
human-gated and stays sequential. (This section was itself adversarially reviewed by GPT-5;
the corrections below are baked in.)

### The real risk is NOT git conflicts - it's parallel agents inventing incompatible realtime
### contracts. So Phase 1 freezes contracts in CODE, not prose.

The SDK surface is verified (see "Correct SDK surface"), but A and B are **NOT "fully
independent"** - they share hidden contracts (chunk size, timestamp meaning, queue/drop policy,
output routing, shutdown, and whether boundaries are `bytes` vs `np.ndarray` vs async iterators
vs queues). Freeze all of that in a **`contracts.py` that no fan-out agent may edit**:

```python
# contracts.py  — frozen in Phase 1; fan-out agents import, never edit.
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Literal, Protocol

INPUT_RATE = 16_000
INPUT_FRAMES = 1_600          # exactly 100 ms @16k mono  -> chunk = 3200 bytes s16le
MODEL_OUTPUT_RATE = 24_000
PCM_FORMAT = "s16le"          # mono, little-endian, 16-bit

# Backpressure (numeric, so every agent makes the SAME choice):
INPUT_QUEUE_MAX_CHUNKS = 10           # ~1 s; drop-OLDEST on overflow, bump a counter
OUTPUT_JITTER_TARGET_MS = 120
OUTPUT_JITTER_MAX_MS = 400            # beyond this, drop-oldest; zero-fill on underrun

class Direction(Enum):  DE_TO_EN = "de_to_en"; EN_TO_DE = "en_to_de"
class OutputChannel(Enum):  LEFT = "left"; RIGHT = "right"

@dataclass(frozen=True)
class InputAudioChunk:
    pcm_s16le: bytes          # mono 16k, exactly INPUT_FRAMES
    seq: int
    source: Literal["left", "right", "fixture"]
    t_capture_ns: int | None
    source_frame0: int | None

@dataclass(frozen=True)
class OutputAudioChunk:
    pcm_s16le: bytes          # mono 24k, variable length
    direction: Direction
    seq: int
    t_received_ns: int

@dataclass(frozen=True)
class SessionEvent:           # the ONE event type the session emits
    direction: Direction
    kind: Literal["audio", "input_transcript", "output_transcript", "status", "error"]
    t_ns: int
    audio: "OutputAudioChunk | None" = None
    text: str | None = None
    detail: str | None = None  # status/error payload (covers go_away / reconnect)

async def live_events(direction: Direction,
                      source: AsyncIterator[InputAudioChunk],
                      config: "LiveSessionConfig") -> AsyncIterator[SessionEvent]: ...

class AudioRuntime(Protocol):
    def input_source(self, source: Literal["left","right"]) -> AsyncIterator[InputAudioChunk]: ...
    def enqueue_output(self, channel: OutputChannel, audio: OutputAudioChunk) -> bool: ...
```

**Event-loop / lifecycle ownership (also frozen in Phase 1):** `app.py` owns `asyncio.run`,
the `TaskGroup`, signal handling, routing (Direction->OutputChannel), and shutdown ordering.
**No worker module creates or closes the event loop.** PortAudio callbacks NEVER call async
APIs directly - they write into a bounded thread-safe bridge; the async side turns that into
`InputAudioChunk`. Because `SessionEvent` already carries `status`/`error`, reconnect/go_away
is part of the contract from day one (not "polish") - the concurrency verifier depends on it.

### Dependency graph (corrected per GPT-5)

```
[Phase 1: SKELETON]  one agent, HARD BARRIER
   contracts.py (above) + LiveSessionConfig + dependency pins (google-genai==2.8.0, exact) +
   CLI skeleton & --list-devices in app.py + module stubs + a FAKE AudioRuntime and a FAKE
   live_events (so end-to-end can run with no API + no hardware).
        │
        ▼
[Phase 2: PARALLEL IMPLEMENT]  fan out, worktree-isolated, by SEAM (not "one file")
   A) translate_session.py (+tests)  - real live_events(); API-key, no hardware
   B) audio/ package (+tests)        - capture, bridge, streaming resample, chunking, device
                                        probe, output resample, stereo L/R mix, jitter buffer.
                                        (Too big for one file -> a package. Needs neither API
                                        nor hardware: test with synthetic audio + the fakes.)
   C) latency.py (+deterministic fixtures) - paced-clip harness + analysis, against contracts
        ▼
[Phase 3: INTEGRATE + 5-LENS ADVERSARIAL VERIFY]  integrator, then verify panel
   integrator wires app.py against contracts.py; then parallel verifiers:
     1 SDK-contract: send/receive match LIVE docs TODAY (preview shifts)
     2 audio-correctness: resampler keeps filter state; buffers bounded + drop policy honoured;
       output zero-fills on underrun; nothing heavy in PortAudio callbacks
     3 concurrency: two sessions, backpressure numbers, go_away/reconnect, clean shutdown, no deadlock
     4 fake end-to-end integration: fake-source -> fake-session -> fake-output runs with NO API,
       NO hardware (proves the modules actually compose)
     5 latency-methodology: is "disambiguating word -> target audible" actually measurable given
       variable translation wording, possibly-missing word timestamps, cross-language alignment?
        ▼
[GATE T1: API-key-gated Live tests]  agent-runnable (needs key, NO hardware)
   single-session canned-wav round-trip; TWO concurrent canned sessions (quota/stability/session-length).
        ▼
[GATE T2: HARDWARE + HUMAN]  *** NOT agent work - sequential, David runs on the machine ***
   real DJI 2ch + AirPods (A2DP/no-HFP); latency rig wired-vs-AirPods; real two-way with the
   sister-in-law's German accent. Agents PREPARE harness + runbook + logging; a human RUNS it.
```

### Seams (revised - one-file-per-agent was too rigid)
- Skeleton owns `contracts.py`, dependency pins, CLI skeleton, stubs, and the FAKES.
- A owns `translate_session.py` + its tests. B owns the `audio/` package + its tests. C owns
  `latency.py` + deterministic fixtures. D (docs/runbooks/example clips) may NOT touch `app.py`.
- `app.py` belongs to the Phase-3 integrator only. Worktree isolation per agent; merge at integrate.

### What ultracode buys / does NOT
- **Buys:** A and B (the bulk) built concurrently once contracts are frozen; a real 5-lens
  adversarial verify on the genuinely risky areas (preview SDK surface, audio plumbing,
  composition, latency methodology); fakes let the whole thing be proven with no API/hardware.
- **Does NOT buy:** any shortcut through GATE T2. Mic capture, AirPods behaviour, real latency,
  accent robustness are physical - measured by David. A workflow must NOT "declare success" there.
- **Caution:** spend the parallel budget on audio-plumbing correctness + verifying the preview
  API against live docs - NOT on splitting trivial files. Pin `google-genai==2.8.0` exactly (a
  loose `>=` lets a fresh agent pull a future breaking preview SDK).

### Hardware/human runbook must check (so the human gate is reproducible)
macOS mic permission for the terminal; **Mono Audio OFF** and **audio balance centred** (else
the L/R split dies); **AirPods automatic-switching + AirPod-mic input DISABLED** (keep A2DP);
safe test volume; network conditions; **does translated audio leak back into the DJI mics**
(feedback) - use earbuds, check; **privacy/consent** for recording bilingual audio + transcripts.

### Cross-channel VAD (Slice 5)
Per-channel energy gating is not enough - two lavs in one room bleed. Use **cross-channel
dominance gating** (only stream the louder channel) so bleed doesn't wake the wrong direction.

---

## Changelog
**v4 -> v5 (GPT-5 review of the parallel-build strategy):** the Phase-1 barrier was too weak
(prose, not code). Added a concrete frozen `contracts.py` (PCM constants incl. 1600-frame /
100 ms chunk, numeric backpressure, `Direction`/`OutputChannel` enums, `InputAudioChunk` /
`OutputAudioChunk` / `SessionEvent` dataclasses, `live_events()` + `AudioRuntime` Protocol) and
froze event-loop/lifecycle ownership in `app.py`. Reclassified: "A and B fully independent" ->
"parallelizable after contracts frozen"; `audio_io.py` -> an `audio/` package (too big for one
agent); dep pins + CLI skeleton moved into Phase 1; reconnect/go_away is contract not polish
(via `SessionEvent` status/error); split API-key-gated Live tests (agent-runnable, gate T1)
from hardware/human gates (T2). Verify panel 3 -> 5 lenses (added fake end-to-end integration
+ latency-methodology). Pinned `google-genai==2.8.0` exactly. Added cross-channel dominance VAD
and an expanded human runbook (Mono Audio off, balance centred, AirPods auto-switch/mic off,
feedback check, privacy/consent).

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
