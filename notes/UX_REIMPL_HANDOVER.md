# UX re-implementation handover (pre-compaction note, 17 Jun 2026)

David is about to hand me a **design to re-implement the UX**. This note captures everything I need
to pick that up cleanly in a fresh context. The ENGINE is done and hardware-proven; only the UI
changes. Read `BUILD_STATUS.md` first, then this.

## Where things stand (don't redo)

- Engine works end-to-end, verified LIVE on the key and on real hardware:
  - both directions translate correctly (`app.py --once`),
  - channel isolation PASSES 22-27 dB (`tools/hw_channel_check.py`),
  - SIMULTANEOUS two-way PASSES with no cross-bleed (`tools/hw_twoway_test.py`).
- 74 unit tests green, `app.py --check` (fake e2e, no API/hardware) passes.
- 5 commits on the repo, NOT pushed. Branch = whatever's checked out (no feature branch made).
- DJI receiver is now in multi-channel mode (exposes 4 USB channels; TX1=ch0, TX2=ch1). We open
  `channels=2` so we read TX1/TX2. Bose output is A2DP 2ch @44100 (idx 1); the HFP 1ch entry
  (idx 0) is the trap - `devices.resolve_device` refuses Bose as input.

## The UI seam - THIS is what any new UI must drive (don't break it)

`ui.py` is deliberately thin and swappable (`notes/ui-is-provisional.md`). The contract:

- `UiState` (in `ui.py`) is the ONLY thing the engine touches. The app mutates it; the view reads it.
  Per-direction (`Direction.DE_TO_EN`, `Direction.EN_TO_DE`) it holds: `in_level_dbfs`,
  `out_level_dbfs`, `in_lines`/`out_lines` (deque of last 3 transcript lines), `out_bytes`,
  `status`, `last_update_ns`. Update API the app calls: `on_input_chunk`, `on_output_chunk`,
  `on_input_transcript`, `on_output_transcript`, `set_status`.
- A UI object just needs `__init__(state)`, `render_once() -> str`, and `async run()`. `NullUi` is
  the no-op proof the app runs with NO view. `TerminalUi` is the current plain renderer.
- `app.py` `_run()` builds `UiState`, wires per-direction level taps (`_level_taps`/`_tapped`),
  passes `output_sink`, and runs the UI as one task in the TaskGroup. To swap the UI, replace the
  `ui` object - do NOT move engine logic into the view.

**Rule (from David, in ui-is-provisional.md): keep the UI thin/plain/decoupled. The engine must stay
replaceable-UI. If the new design is Electron/web, the renderer stays dumb; the Python core drives
it over a clean interface (e.g. a small JSON/event bridge), all heavy lifting (Gemini sessions,
PortAudio, resample) stays in the core.**

## The design that's coming

- David is producing wireframes/an interactive prototype in **Claude Design**, using the
  **Paidia "Riso Zine Funky"** design system
  (`~/git/ai-sandbox/projects/DesignSystems/PaidiaRhisoZineFunky/`).
- The intended flow is already spec'd in `notes/claude-design-ux-prompt.md`: a splash screen, then a
  single-canvas **accordion setup wizard** - Row 1 INPUTS (two mics, live waveforms, default
  "Wireless Mac RX"/our "Wireless Mic Rx"), Row 2 OUTPUTS (two earbuds, default AirPods/our Bose),
  Row 3 CHANNEL TEST (per-person "hear yourself" loopback with ~0.5s delay into their OWN bud),
  final LIVE TEST. The new design David hands me may differ - map whatever he gives onto the seam.

## Engine hooks the wizard UX will likely need (probably NOT built yet - build as needed)

- **Structured device list for pickers** - `audio.devices.list_devices()` returns a pretty string;
  a picker needs a list of `{index, name, in_ch, out_ch, rate}`. Add a structured variant.
- **Live waveforms** - `UiState` currently exposes dBFS levels only. Real waveforms need raw sample
  windows streamed to the view. Add a small ring of recent samples per channel if the design wants
  scrolling waveforms.
- **Per-mic test (Row 1)** - open capture on one channel and stream its level/waveform without
  starting translation. (Capture layer already supports per-channel sources.)
- **Channel-test loopback (Row 3)** - play a person's own mic back into their OWN earcup with ~0.5s
  delay. NB on the desk rig this is exactly the feedback path; fine as a brief, gated self-test.
  `hw_channel_check.py` already does play->capture per side and can seed this.
- **Per-person naming + language target** - `config.SOURCE_FOR_DIRECTION` / `OUTPUT_FOR_DIRECTION`
  and `UiState(names=...)` already allow custom labels; a wizard sets these.
- **Run lifecycle from the UI** - start/stop the two sessions on a button, not just CLI `--run`.

## Gotchas to carry forward (hard-won)

1. **The model sends NO turn_complete / generation_complete** - it's a continuous streamer. End of
   turn = SILENCE (see `_once` silence detection, ~1.2 s gap). `--run` streams continuously.
2. **Feedback on the desk rig**: each direction's OUTPUT earcup holds that direction's INPUT mic, so
   a live `--run` (which plays output) self-feeds-back. Tests that play SOURCE only and save output
   to files avoid it. Real use (two people, separate buds) has no feedback.
3. **Bose HFP trap**: if anything opens the Bose MIC, macOS drops it to 1ch 16k HFP and the stereo
   output dies. Recovery = power-cycle/reconnect the Bose; keep Sound Input off the Bose. Never open
   Bose as input (the guard enforces this).
4. **First-byte latency ~2.9 s** on short clips (model gathering sentence context; verb-final German
   is the worst case). Real conversation latency should be measured on longer natural speech.
5. **Input VAD not built** (Slice 5) - two live mics burn tokens continuously and may translate
   bleed. Add per-channel/cross-channel gating for a real deployment.

## Run / test quickref

```bash
cd ~/projects/German-English-realtime && source .venv/bin/activate   # google-genai==2.8.0 pinned
python app.py --list-devices
python app.py --check                                   # fake e2e (no API/hardware)
python -m pytest -q                                     # 74 pass / 1 skip
python app.py --once tests/fixtures/de_morgen.16k.wav --target en
python tools/hw_channel_check.py                        # isolation (PASS 22-27 dB)
python tools/hw_twoway_test.py                          # simultaneous two-way (PASS)
```

File map: `contracts.py`/`config.py` (frozen seam), `translate_session.py` (live_events),
`audio/` (capture/output/devices/runtime), `app.py` (loop+router+CLI), `ui.py` (the swappable view),
`latency.py`, `tools/` (fixtures + hw tests), `tests/`.
