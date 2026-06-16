# RUNBOOK - two-way DE<->EN live translator

How to run the app and the hardware test harness, plus the human-gated checklist for the
acoustic gate (Gate-T2). The pieces that need NO key and NO hardware are at the top; the
hardware gates are at the bottom.

All commands assume the venv:

```bash
cd /Users/david/projects/German-English-realtime
source .venv/bin/activate
```

---

## 1. Offline, no key, no hardware

```bash
python app.py --check          # fake end-to-end, both directions through real routing; exit 0
python app.py --list-devices   # enumerate audio devices (find the DJI + Bose indices)
python -m pytest -q            # full unit suite (LIVE test skipped unless LIVE=1)
```

`--check` is the composition proof: it runs BOTH directions concurrently through the same
`pump_direction` routing the real app uses, with `FakeAudioRuntime` + `fake_live_events`. Green =
the modules compose.

In `--list-devices` you should see (indices vary):
- `Wireless Mic Rx` - 2 in / 0 out @ 48000 Hz - the **DJI** (ch0=TX1=LEFT=German, ch1=TX2=RIGHT=English)
- `Bose QC35 II` - 0 in / **2 out** @ 44100 Hz - the **Bose A2DP output** (use this one)
- `Bose QC35 II` - **1 in** / 0 out @ 16000 Hz - the **Bose HFP mic** - **NEVER open as input**
  (it collapses the Bose output to mono). `devices.resolve_device` refuses it automatically.

---

## 2. One real session from a fixture (Gate-T1, needs the key, no hardware)

Needs `GEMINI_API_KEY` in `.env`. Streams a 16k fixture through ONE real translate session and
writes the translated 24k audio.

```bash
python app.py --once tests/fixtures/de_morgen.16k.wav --target en   # -> out_en.wav
python app.py --once tests/fixtures/en_short.16k.wav  --target de   # -> out_de.wav
```

Prints the input/output transcripts and first-byte / output-span timing. This is model + network
only - it excludes capture and Bluetooth playout.

Component latency across both directions:

```bash
python latency.py component tests/fixtures/de_morgen.16k.wav both
```

---

## 3. The real two-way app (needs key + hardware)

```bash
python app.py --run            # plain terminal status view
python app.py --run --no-ui    # no-op UI (engine only)
```

Two concurrent sessions under one TaskGroup: DE->EN (DJI ch0 -> LEFT earcup) and EN->DE (DJI ch1
-> RIGHT earcup). Ctrl-C (SIGINT) shuts down cleanly: sessions stop pumping, then the audio
runtime is torn down (in that order, so no chunk is enqueued into a closed stream).

---

## 4. Hardware harness (Gate-T2) - per-earcup isolation rig

**The rig:** each DJI transmitter's mic sits **inside one Bose earcup**, so audio played out one
earcup couples acoustically into exactly one DJI channel:

```
  LEFT  Bose earcup  <--couples-->  DJI ch0 (TX1, LEFT, German side)
  RIGHT Bose earcup  <--couples-->  DJI ch1 (TX2, RIGHT, English side)
```

This hardware channel isolation is what keeps the two directions from bleeding into each other.
Seal each DJI mic inside its earcup; keep the room quiet; set a safe, moderate volume.

### 4a. Isolation check

Plays a tone out the LEFT earcup only and asserts energy on DJI ch0 and NOT ch1, then repeats
for RIGHT -> ch1. Needs no key.

```bash
python tools/hw_channel_check.py            # 1.0s 440 Hz tone per side
python tools/hw_channel_check.py 1.5 660    # custom seconds + tone Hz
```

PASS per side requires the intended channel to exceed the other by >= 12 dB. A FAIL means the
mics are cross-coupling (earcups not sealed, wrong device, or mics swapped).

### 4b. Acoustic end-to-end loopback

Plays a German fixture out the LEFT earcup -> captures DJI ch0 -> real translate(en) ->
`out_loopback_en.wav`; then English out RIGHT -> ch1 -> translate(de) -> `out_loopback_de.wav`.
Prints transcripts and an onset->onset align latency. **Needs the key.**

```bash
python tools/hw_loopback_test.py
```

This is the only test that includes capture + Bluetooth A2DP playout. A2DP adds ~150-300 ms that
wired output would not - it can eat the <500 ms budget on its own. To quantify the Bluetooth tax,
re-run wired (or via AirPods) and compare the align numbers.

---

## 5. Human-gated checklist (do this BEFORE Gate-T2)

These are physical/OS preconditions a human must confirm; the harness cannot check them.

- [ ] **macOS Mono Audio is OFF.** System Settings -> Accessibility -> Audio -> "Play stereo
      audio as mono" **unchecked**. (Mono audio destroys the per-earcup isolation - both earcups
      get both channels.)
- [ ] **Audio balance centred.** System Settings -> Sound -> Output -> Balance slider centred. An
      off-centre balance attenuates one earcup and skews the isolation check.
- [ ] **Bose connected as A2DP, not HFP.** Pick the **2ch/44100** Bose output. Never select the
      Bose mic (the 1ch/16k entry) as an input - it forces HFP and collapses output to mono.
- [ ] **Safe volume.** Start low. The mics are *inside* the earcups, so output couples straight
      back in - too loud risks feedback and is unpleasant.
- [ ] **Feedback check.** With both sessions live (`--run`), confirm no runaway feedback howl
      before speaking. If it builds, lower volume / improve the earcup seal.
- [ ] **Privacy / consent.** Live translation streams audio to Google. Tell anyone being
      translated that their speech is sent to a cloud model, and get consent before recording or
      running a session in a meeting.
- [ ] **DJI channels confirmed.** TX1 = ch0 = LEFT = German speaker; TX2 = ch1 = RIGHT = English
      speaker. If the wrong person is on the wrong channel, swap the transmitters, not the code.

---

## Files

- `app.py` - composition root (event loop, TaskGroup, SIGINT, routing). Modes above.
- `ui.py` - thin terminal status view (provisional; see `notes/ui-is-provisional.md`).
- `tools/hw_channel_check.py` - per-earcup isolation rig.
- `tools/hw_loopback_test.py` - autonomous acoustic end-to-end test.
- `latency.py` - `component` (API only) and `acoustic` (hardware) latency modes.
