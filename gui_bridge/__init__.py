"""GUI bridge: a second composition root for the Electron desktop app.

This package reuses the frozen engine (`contracts`, `config`, `translate_session.live_events`,
`audio.PortAudioRuntime`) and adds the runtime control + rich view-state the GUI needs, exposed
over a local WebSocket (see desktop/PROTOCOL.md). The translation core is untouched: the bridge
mirrors app.py's routing but makes it runtime-swappable and pushes facts into a `ViewState` the
renderer reads.

Modules:
  state.py   - ViewState: plain mutable data + snapshot() to the protocol dict.
  engine.py  - GuiEngine: runtime lifecycle, routing, gain, taps, sessions, mic-test, loopback.
  server.py  - the asyncio websockets server (hello + state stream + cmd dispatch).
  __main__.py- entry point: pick a port, serve, print `READY <port>`.
"""
