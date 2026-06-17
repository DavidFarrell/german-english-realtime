# DebbieDavidApp - desktop UI (Electron + Python bridge)

The faithful implementation of `DebbieDavidApp.dc.html` (Claude Design, Riso Zine Funky) as a real
macOS app. The Electron shell is a **dumb renderer**; all the work (audio, Gemini Live Translate,
routing) stays in the untouched Python core. They talk over a local WebSocket - see `PROTOCOL.md`.

```
desktop/
  main.js         Electron main: spawns `python -m gui_bridge`, waits for `READY <port>`, opens the window
  preload.js      exposes only the ws port to the renderer (contextIsolation on)
  renderer/       the view: index.html + styles.css + app.js + the copied _ds/ design system
  PROTOCOL.md     the WebSocket contract both sides code to
../gui_bridge/     the Python bridge (a 2nd composition root over the frozen engine)
```

## Run it

```bash
# 1. the Python venv must exist with the engine deps (google-genai==2.8.0 etc.)
cd ~/projects/German-English-realtime && source .venv/bin/activate && python -c "import websockets"

# 2. install the Electron dep once
cd desktop && npm install

# 3. launch (this spawns the bridge for you)
npm start
```

The window opens on the splash screen. Walk the wizard: Inputs (name the two people, pick the DJI,
set gain, watch the live waveforms) -> Outputs (choose what each ear hears, test each ear) ->
Channel test (each person hears themselves, ~0.3 s delay; "No" -> the crossed-wiring fixer) ->
Start live translation (two-column live transcript).

## Headless checks (no Electron, no hardware, no API)

```bash
cd ~/projects/German-English-realtime && source .venv/bin/activate
python tools/bridge_smoke.py     # drives the whole bridge protocol over a real ws with fakes
python -m pytest -q              # the engine unit tests (still green)
```

## How the bridge maps to the engine

`gui_bridge/engine.py` reuses `translate_session.live_events` + `audio.PortAudioRuntime` exactly as
`app.py` does, but makes the routing runtime-swappable and adds the wizard's controls (device
pickers, gain, per-mic test, the channel-test loopback, crossed-wiring swaps, start/stop). The
frozen files (`contracts.py`, `config.py`, `translate_session.py`, `app.py`) are untouched.

**One routing note worth knowing:** the GUI defaults to the **real two-person product** wiring,
which is *crossed* - each person hears the OTHER, translated, in their own ear (left person speaks
& hears English, right speaks & hears German, per the design's live screen). That differs from the
frozen engine's `config.py` *same-side* map, which is the mics-in-earcups **desk-rig loopback**
wiring. The routing is data (`engine.routing`) and the channel-test swaps adjust it live.
