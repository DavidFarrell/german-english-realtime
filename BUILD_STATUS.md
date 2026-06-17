# Build status - two-way DE<->EN live translator

Built overnight 16 Jun 2026 (ultracode). **The engine works end-to-end on real hardware.** One
hardware setting needs your hand before simultaneous two-way will work (see "One thing for you").

## TL;DR

- ✅ **Translation engine**: both directions translate correctly, live, on your key.
- ✅ **Real-hardware acoustic path**: German played into the rig was captured by the DJI and
  translated to English (and EN->DE). The whole mic->model->audio path is proven.
- ✅ **Channel isolation FIXED + SIMULTANEOUS two-way PROVEN** (17 Jun): after setting the DJI
  receiver out of Mono (it now exposes separate channels), `hw_channel_check` passes at 22-27 dB
  isolation, and `hw_twoway_test` runs both directions AT ONCE with no cross-contamination -
  German(left) and English(right) spoken together, each translated correctly. The two-way
  translator works end-to-end on hardware.
- ⚠️ Note on the desk rig: a live `--run` (which *plays* translated audio into the earcups) will
  acoustically feed back, because each direction's output earcup holds that direction's input mic.
  That's a quirk of the mics-in-earcups *test* rig, not real use (two people, separate buds). The
  `hw_twoway_test` avoids it by saving output to files instead of playing it.

## What got built

| File | What it is |
|------|-----------|
| `contracts.py` / `config.py` | Frozen shared types + device/routing config (DJI ch0=L=German, ch1=R=English; Bose L/R out) |
| `translate_session.py` | `live_events()` - one Gemini Live-Translate session per direction; pure event parser; feed-decoupled go_away reconnect |
| `audio/` | PortAudio capture (48k->16k, bounded thread-safe ring), jitter-buffered stereo output (24k->44.1k, L/R never mixed), device resolve with **Bose-HFP guard** |
| `app.py` | asyncio TaskGroup, routing, SIGINT shutdown. Modes: `--list-devices` `--check` `--once` `--run` |
| `ui.py` | Thin terminal status (level meters + transcripts). Deliberately plain/swappable - real design comes from Claude Design (per `notes/ui-is-provisional.md`) |
| `latency.py` | Component + acoustic latency harness (energy-onset based; limits documented) |
| `tools/hw_channel_check.py` | Per-earcup isolation check (the test that currently FAILs - see below) |
| `tools/hw_loopback_test.py` | Autonomous acoustic end-to-end: play fixture into earcup -> capture DJI -> translate |
| tests/ | 74 passing, 1 skipped (the skipped one is a live round-trip, run with `LIVE=1`) |

## How to run

```bash
cd ~/projects/German-English-realtime && source .venv/bin/activate
python app.py --list-devices        # see DJI + Bose
python app.py --check               # fake end-to-end, no API/hardware (proves it composes)
python app.py --once tests/fixtures/de_morgen.16k.wav --target en   # one real translation -> out_en.wav
python tools/hw_loopback_test.py    # the real acoustic test on the rig
python app.py --run                 # the real two-way app (needs Stereo mode - see below)
```

## Verified results (live, on your key)

- `--once` DE->EN: *"Das Wetter ist heute sehr schön."* -> *"The weather is very nice today."*
- `--once` EN->DE: *"...finish the whole project together."* -> *"...das ganze Projekt zusammen beenden werden."*
- `hw_loopback_test`: German **played into the earcup**, captured by the DJI, came back as correct
  English (and the reverse). out_loopback_en.wav / out_loopback_de.wav.

## Two findings worth knowing

1. **The model never sends an end-of-turn signal.** It's a continuous streamer (matches the
   ThursdAI note) - after a clause it just emits silence, no `turn_complete`/`generation_complete`.
   So `--once` now ends the turn by **silence detection** (clean 5s clips instead of 27s of padding).
   `--run` streams continuously, which is the correct behaviour for live conversation.
2. **First-byte latency ~2.9s** on these short clips (the model gathering sentence context, as the
   notes predicted for verb-final German). Worth measuring properly on longer, natural speech.

## One thing for you (the only hardware blocker)

The two-way design needs each DJI transmitter on its own channel. Right now `hw_channel_check.py`
shows **0 dB isolation** - a tone played into the left earcup shows up equally on *both* DJI
channels (perfect correlation). That means the receiver is summing to mono. To fix:

1. Set the **DJI Mic 3 receiver to Stereo mode** (not Mono) - so TX1->left, TX2->right.
2. Make sure **both transmitters are powered on**.
3. Re-seat each transmitter sealed inside its earcup.

Then: `python tools/hw_channel_check.py` should PASS (>=12 dB isolation), and `python app.py --run`
gives real simultaneous two-way. (Note: with the mics-in-earcups desk rig, `--run` will acoustically
feed back because each direction's output plays into the earcup holding that direction's input mic -
that's a quirk of the *test* rig, not real use where the two people wear separate buds. For a clean
two-way test, clip the mics to two people / two separated spots.)

## Not done (deferred, by design)

- Input VAD / cross-channel gating (Slice 5) - stops mic bleed + idle token burn.
- Real mouth-to-ear latency with a shared-clock offset (the harness is built; needs the rig timing).
- The Riso Zine Funky UI - waiting on your Claude Design wireframes.
- Pricing/quota measurement over long sessions (undocumented; needs a deliberate run).
