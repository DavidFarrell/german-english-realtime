# DebbieDavidApp bridge protocol (v1)

The Electron renderer (dumb view) and the Python bridge (owns the engine) talk over a single
local WebSocket: `ws://127.0.0.1:<port>`. The port is chosen by the bridge, printed to stdout as
`READY <port>` and passed by Electron main to the renderer. All frames are JSON text.

The engine core (`contracts.py`, `config.py`, `translate_session.py`, `app.py`) is untouched.
The bridge is a second composition root for the GUI; the renderer holds NO engine logic - it only
renders `state` and sends `cmd`s.

## Coordinates

- `side`: `"left"` | `"right"`. Left = transmitter 1 / left earbud; Right = transmitter 2 / right.
- Each side has a `lang` target: `"en"` | `"de"` - the language that side HEARS in its earbud.
  Defaults: left hears `en` (left speaker is German -> translated to English... see note), right
  hears `de`. In our engine the LEFT mic is the German speaker translated to EN played on LEFT out,
  the RIGHT mic is the English speaker translated to DE played on RIGHT out. So `side.lang` is "what
  this person hears". `swapPeople` / `swapEarbuds` change the routing at runtime.
- `wizardStep`: `"splash"` | `"inputs"` | `"outputs"` | `"channel"` | `"live"`.

## Server -> client

Every message is `{ "type": ..., ... }`.

### `hello` (once, on connect)
```json
{
  "type": "hello",
  "appName": "DebbieDavidApp",
  "version": "0.4",
  "inputDevices":  [{"index":6,"name":"Wireless Mic Rx","inCh":2,"outCh":0,"rate":48000}, ...],
  "outputDevices": [{"index":1,"name":"Bose QC35 II","inCh":0,"outCh":2,"rate":44100}, ...]
}
```

### `state` (continuous, ~15 Hz; full snapshot, small)
```json
{
  "type": "state",
  "wizardStep": "inputs",
  "input":  { "found": true,  "deviceName": "Wireless Mic Rx", "deviceIndex": 6 },
  "output": { "found": true,  "deviceName": "Bose QC35 II",    "deviceIndex": 1 },
  "gain": 0.72,
  "sides": {
    "left":  { "name": "David",  "lang": "en", "level": 0.61, "waveform": [0.1,0.8,...],
               "speaking": true,  "muted": false, "testing": false },
    "right": { "name": "Debbie", "lang": "de", "level": 0.12, "waveform": [...],
               "speaking": false, "muted": false, "testing": false }
  },
  "channelTest": {
    "active": false, "side": null, "phase": "idle",
    "crossed": false, "confirmed": {"left": false, "right": false}
  },
  "session": {
    "running": false, "elapsedMs": 0,
    "utterances": [
      { "id": 7, "side": "left", "speaker": "David", "srcLang": "en", "dstLang": "de",
        "tStartMs": 242000, "source": "Hi, I'm David.", "translation": "Hallo, ich bin David.",
        "live": false }
    ]
  },
  "error": null
}
```
Notes:
- `level` is 0..1 (a normalised meter, derived from dBFS on the bridge side).
- `waveform` is an array of ~48 values 0..1 (recent peak envelope) for the animated bars.
- `utterances` is the rolling transcript (cap ~50). The newest may have `"live": true` while the
  speaker is mid-utterance; the bridge updates its `source`/`translation` in place and flips
  `live` to false when the clause ends.
- `channelTest.phase`: `"idle"` | `"listening"` | `"playing"` | `"awaiting"` | `"ok"` | `"crossed"`.
- `error`: `null` or `{ "code": "device_missing"|"session_error"|"output_mono"|..., "message": "..." }`.
  An `output_mono` error also carries `"fixable": true` and `"holder"` (the process holding the
  earbud mic, or null) - the renderer shows a **Fix** button that sends `fixEarbuds`. Fixable errors
  do not auto-expire.
- `fixingOutput`: `true` while the `fixEarbuds` recipe is running (renderer shows a "Fixing…" toast).

### `event` (discrete one-offs; also reflected in the next `state`)
```json
{ "type": "event", "event": "utterance", "utterance": { ... } }
{ "type": "event", "event": "channelTestResult", "side": "left", "crossed": false }
{ "type": "event", "event": "deviceChanged", "input": {...}, "output": {...} }
{ "type": "event", "event": "log", "message": "..." }
```

## Client -> server

Every message is `{ "cmd": ..., ... }`. Unknown cmds are ignored (logged).

| cmd | fields | effect |
|---|---|---|
| `gotoStep` | `step` | move the wizard to a step (also drives engine start/stop where needed) |
| `setName` | `side`, `name` | rename a person |
| `setLanguage` | `side`, `lang` | set what this side hears (`en`/`de`); enforces the other side gets the opposite |
| `setGain` | `value` (0..1) | input gain applied to both mics before the model |
| `selectDevice` | `kind` (`input`/`output`), `index` | choose a device |
| `testMic` | `side`, `on` (bool) | start/stop streaming one mic's level+waveform (no translation) |
| `testEar` | `side` | play a short tone into that earbud only |
| `startChannelTest` | `side` | begin the half-second self-loopback for that person |
| `stopChannelTest` | - | end any running loopback |
| `channelTestAnswer` | `side`, `ok` (bool) | user says "yes that's me" / "no" |
| `swapEarbuds` | - | flip which earbud each direction plays into (fix crossed wiring) |
| `swapPeople` | - | flip which mic is which person (names + source channel) |
| `rescan` | - | re-enumerate devices, refresh `found` flags |
| `fixEarbuds` | - | un-stick BT earbuds from HFP mono back to A2DP stereo (parks both default routes off them so the SCO link drops, then hands output back). Sent by the Fix button on a `fixable` toast. |
| `startLive` | - | start the two live translate sessions |
| `stopLive` | - | stop the sessions, keep the runtime |
| `shutdown` | - | stop everything (Electron sends on quit) |

## Lifecycle

1. Electron main spawns `python -m gui_bridge` (cwd = project root, venv python). The bridge picks
   a free port, starts the WS server, prints `READY <port>`.
2. Electron main waits for that line, then creates the window and passes the port to the renderer.
3. Renderer connects, gets `hello`, then a stream of `state`. It sends `cmd`s on user actions.
4. On quit, Electron sends `shutdown` and SIGTERMs the child.

The bridge tolerates missing hardware: if the input/output device is absent, `found:false` and the
renderer shows the device-not-found screen. `rescan` re-checks. No command requires hardware to be
present except those that actually stream audio (which no-op with an `error` event if absent).
