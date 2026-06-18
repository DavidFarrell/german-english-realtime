# Plan: seamless auto-fix for BT-earbud HFP-mono collapse

Status: **design, signed off** (Claude + GPT-5 xhigh, 2-round convergence, 18 Jun 2026). Implement
later. This supersedes the manual-Fix-button approach shipped 18 Jun (which becomes one entry point
into the guardian below).

## Problem (recap)

macOS auto-selects mic-advertising BT earbuds ("David's Eggies") as the system **default INPUT** on
connect; any process then reading that mic forces the buds from A2DP stereo (2 out ch, what the app
needs: German->left / English->right) into HFP mono "call mode" (1 out ch). Common forcers: the Sound
settings pane (`Sound.appex`), screen/audio capture (`replayd`/Granola), always-on STT (SuperWhisper).
The collapse is sticky.

## Architecture: one `AudioGuardian` owner

A single asyncio-owned component is the ONLY thing allowed to mutate CoreAudio defaults or run
recovery. **Manual Fix and auto-fix both route through it** - no separate `engine.fix_earbuds()` that
can race the guard.

**States:** `disabled` -> `passive` -> `armed` -> `live`; transient `recovering_light`,
`recovering_park`; sticky `blocked` (oscillation, or an external holder we can't evict).

**Lease - the guard may mutate global audio ONLY in `armed` or `live`:**
- `armed` = user is actively setting up/using earbuds in-app (wizard on outputs/channel steps, or the
  protect toggle is on).
- `live` = a translation session is running. (Collapse CAN happen mid-session, so the guard must run
  live; heavy/park recovery is the part that won't run with audio flowing.)
- `passive` = splash/inputs step or backgrounded with no session -> **observe only**, never touch
  system defaults (so we can't sabotage David picking the earbud mic for Zoom while the app idles in
  the background).
- User toggle "Keep earbuds in stereo while in use" (default ON) gates the whole guard; off ->
  `disabled`.

**Inputs (reconciled graph, all by UID):** device list + an **earbud-group-specific generation
counter** (not the global device-list generation - HDMI/USB churn must not reset recovery budgets);
our DJI input UID; our earbud A2DP output UID + resolved HFP input UID; system default input UID;
earbud output channel count; set of processes holding the **earbud input device specifically**;
foreground/live state; user toggle. **Debounce** holder/channel snapshots before acting (process-device
state flickers).

**Actions:** `correct_default_input` (light), `recover_park` (heavier, visible), `recover_reconnect`
(blueutil; manual/opt-in only, never silent), `surface_blocker`.

## Device identity: UIDs, not name substrings

BT buds expose, under ONE name, a 1ch HFP input node AND a 2ch A2DP output node; substring lookups
can read the wrong node (mis-report mono, or set default to the HFP node).
- Resolve + persist CoreAudio **device UIDs** (`kAudioDevicePropertyDeviceUID`) for the DJI and
  earbuds; persist UID, not name.
- **Earbud-group classifier (GPT-5 constraint - the one hard requirement for sign-off):**
  `kAudioDevicePropertyRelatedDevices` as PRIMARY but NOT authoritative. Validated fallback =
  Bluetooth transport (`kAudioDevicePropertyTransportType == ...Bluetooth`) + matching
  name/manufacturer/model + complementary input/output capability. UID-stem matching only as a
  last-resort heuristic (UIDs are documented black boxes). **Persist BOTH the A2DP output UID and the
  resolved HFP input UID once confirmed.** (Misidentifying the HFP sibling is the single biggest risk;
  this neutralises it.)
- Channel checks read the output scope of the A2DP node by UID. `set_default` targets a specific UID +
  scope.

## Holder detection: who holds the EARBUD INPUT, not "any mic"

For each process in `kAudioHardwarePropertyProcessObjectList`, read `kAudioProcessPropertyDevices`
(input scope) and check whether the earbud HFP input UID is in that set. **Cross-check with
`kAudioProcessPropertyIsRunningInput`** (Devices gives attribution; IsRunningInput filters
stale/non-active clients) and **exclude our own PID**. A confirmed non-app holder of the earbud input
drives the `blocked` decision (light/park will lose, so surface a blocker rather than thrash).

## Recovery: graduated

1. **Light** (`correct_default_input`): set default input -> DJI (by UID), wait ~1-2s, re-check.
   Cheap, no audio interruption; fixes the common "macOS just grabbed the buds as default mic" case.
   Auto-run freely while armed/live.
2. **Park** (`recover_park`): proven recipe minus reconnect - park default output -> built-in
   speakers + input -> DJI, wait, hand output back. ~4s audible blip (audio routes to laptop
   speakers). Auto-run ONLY while armed/live AND with the "Fixing..." state shown - never silent.
3. **Reconnect** (`recover_reconnect`): `blueutil` disconnect/connect. Disruptive (can drop other
   apps' audio/meetings). NEVER silent/auto - only on an explicit user tap from the blocker toast.

## Retry: episode-based

Episode signature = (earbud-group generation, default-input UID, earbud channel count, earbud-input
holder set). A recovery runs at most once per distinct signature: one light per mono episode, one park
per device-connection episode. Time cooldown only a secondary backstop. >3 default-input corrections
in 10s -> `blocked_oscillation`; leave `blocked` only when the signature changes (reconnect, holder
gone, or explicit user request).

## In-app prevention (do regardless)

The engine must open the DJI input and earbud A2DP output by **explicit UID** and never read/select
the default input device - so DebbieDavidApp itself can never trigger HFP. (Doesn't stop other apps;
that's the guard's job.) Cheap, high-value correctness fix.

## Listeners vs polling

Target: CoreAudio property listeners (default-input, default-output, device-list) that do NO work -
only `loop.call_soon_threadsafe(enqueue, "dirty")`; the owner re-enumerates and decides. Never
`set_default` in a callback.
**v1 compromise (GPT-5-accepted):** poll ~5 Hz **only while armed/live**, PLUS immediate reconciles on
every arm/live transition, output-device selection, manual fix, and session start. Native
`AudioObjectAddPropertyListenerBlock`+dispatch deferred to v2 (ctypes block/dispatch plumbing is more
risk than value for v1). It remains a compromise, not fully event-driven.

## Concurrency

One asyncio owner task. Everything else (4 Hz housekeeping tick, future listener callbacks via
`call_soon_threadsafe`, manual Fix cmd, live/wizard transitions) only **enqueues events**. The owner
is the sole mutator of defaults and sole launcher of the blocking recipe via `asyncio.to_thread`,
guarded by `recovering_*` state so re-entrancy is impossible. Housekeeping shrinks to "emit a tick."

## State / protocol

- Slim `guardian` status surfaced (generalises today's `fixingOutput`): idle / "Fixing earbuds..." /
  "blocked: <holder> is using the earbud mic - quit it, or tap Reconnect".
- `output_mono` error becomes guardian-emitted; manual `fixEarbuds` cmd enqueues a user-requested
  escalation (incl. the opt-in reconnect) rather than calling the recipe directly.
- Protect toggle persisted (settings file or `setGuardEnabled` cmd).

## Decisions for David (UX, not engineering)

1. Confirm the `armed` definition + protect-toggle default-ON (above).
2. Is the ~4s laptop-speaker blip acceptable as an automatic action while armed, or should even `park`
   require a tap (auto-light always; auto-park only if David says yes)?

## Phasing (implement later)

- **Slice 1 - correctness, no behaviour change:** UID identity + earbud-group classifier (validated
  fallback, persist both UIDs); engine opens devices by UID + stops touching default input; channel/
  holder checks by UID.
- **Slice 2 - the guardian:** `AudioGuardian` owner + lease + graduated light/park + episode retry +
  oscillation/blocked; manual Fix re-routed through it; 5 Hz armed polling + transition reconciles.
- **Slice 3 - event-driven + polish:** CoreAudio property listeners as dirty triggers; protect-toggle
  UI; blocker toast with opt-in Reconnect.

## Sign-off

GPT-5 (xhigh, round 2): "**YES**, if you amend grouping to RelatedDevices-primary + validated fallback
+ store both earbud output/input UIDs" - amended above. Remaining acknowledged compromise: v1 uses
polling, not listeners (fast-follow in Slice 3).
