# TODO (Slice 3, deferred): CoreAudio property listeners instead of 5 Hz armed polling

Slices 1-2 of the AudioGuardian shipped with the GPT-5-accepted v1 compromise: the guard reconciles
on a **5 Hz poll while armed/live** plus immediate reconciles on every arm/live transition, output
selection, manual fix, and session start (see `notes/AUTOFIX_PLAN.md`, "Listeners vs polling").

The Slice-3 fast-follow is to make it event-driven:

- Register CoreAudio property listeners (`AudioObjectAddPropertyListenerBlock` + a dispatch queue) on:
  - `kAudioHardwarePropertyDefaultInputDevice` (someone grabbed/changed the default mic),
  - `kAudioHardwarePropertyDefaultOutputDevice`,
  - `kAudioHardwarePropertyDevices` (device list churn / connect / disconnect),
  - ideally the earbud A2DP node's `kAudioDevicePropertyStreamConfiguration` (channel-count flips).
- The listener callbacks must do NO work: only `loop.call_soon_threadsafe(guardian.post, Event.DIRTY)`.
  The owner re-enumerates and decides. NEVER call `set_default` from a callback.
- `AudioGuardian.post()` is already thread-safe (captures the loop in `run()`, routes cross-thread
  posts via `call_soon_threadsafe`), and `Event.DIRTY` is already handled (reconcile while leased).
  So Slice 3 is mostly the ctypes block/dispatch plumbing in `macaudio.py` + a small registration shim
  the guardian/engine starts on arm and tears down on disarm/shutdown.
- Keep the 5 Hz poll as a belt-and-braces backstop (or drop it once listeners are proven); the polling
  path stays correct either way.

Risk note (why it was deferred): wiring `AudioObjectAddPropertyListenerBlock` from pure ctypes needs a
block trampoline + a `dispatch_queue_t`, which is more ctypes risk than value for v1. The polling
compromise is acknowledged in the plan's sign-off, not a silent shortcut.
