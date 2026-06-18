"""WebSocket server: hello + ~15 Hz state stream out, cmd dispatch in (desktop/PROTOCOL.md).

One shared GuiEngine backs all connections (it's one app). Each connection gets a sender loop
(hello once, then state snapshots) and a receiver loop (cmd dispatch). A housekeeping task polls
devices and finalises stale transcript utterances.
"""
from __future__ import annotations

import asyncio
import json

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .engine import GuiEngine

APP_NAME = "DebbieDavidApp"
VERSION = "0.4"
STATE_HZ = 15
HOUSEKEEP_HZ = 4


class BridgeServer:
    def __init__(self, engine: GuiEngine) -> None:
        self.engine = engine
        self._clients: set = set()

    # -- per-connection loops ----------------------------------------------------------------
    async def _sender(self, ws) -> None:
        hello = {"type": "hello", "appName": APP_NAME, "version": VERSION,
                 **self.engine.list_devices()}
        await ws.send(json.dumps(hello))
        period = 1.0 / STATE_HZ
        while True:
            await ws.send(json.dumps(self.engine.state.snapshot()))
            await asyncio.sleep(period)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            await self._dispatch(data)

    async def handler(self, ws) -> None:
        self._clients.add(ws)
        try:
            await asyncio.gather(self._sender(ws), self._receiver(ws))
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)

    # -- command dispatch --------------------------------------------------------------------
    async def _dispatch(self, data: dict) -> None:
        cmd = data.get("cmd")
        e = self.engine
        try:
            if cmd == "gotoStep":
                await e.goto_step(data.get("step", "splash"))
            elif cmd == "setName":
                e.set_name(data["side"], data.get("name", ""))
            elif cmd == "setLanguage":
                await e.set_language(data["side"], data["lang"])
            elif cmd == "setGain":
                e.set_gain(data.get("value", 0.5))
            elif cmd == "selectDevice":
                await e.select_device(data["kind"], int(data["index"]))
            elif cmd == "testMic":
                await e.test_mic(data["side"], bool(data.get("on", True)))
            elif cmd == "testEar":
                e.test_ear(data["side"])
            elif cmd == "startChannelTest":
                await e.start_channel_test(data["side"])
            elif cmd == "stopChannelTest":
                await e.stop_channel_test()
            elif cmd == "channelTestAnswer":
                await e.channel_test_answer(data["side"], bool(data.get("ok", True)))
            elif cmd == "swapEarbuds":
                await e.swap_earbuds()
            elif cmd == "swapPeople":
                await e.swap_people()
            elif cmd == "rescan":
                e.rescan()
            elif cmd == "fixEarbuds":
                await e.fix_earbuds()
            elif cmd == "startLive":
                await e.start_live()
            elif cmd == "stopLive":
                await e.stop_live()
            elif cmd == "shutdown":
                await e.shutdown()
        except (KeyError, ValueError, TypeError):
            pass  # malformed cmd: ignore (renderer is the only client; log-free by design)

    # -- background housekeeping -------------------------------------------------------------
    async def _housekeeping(self) -> None:
        period = 1.0 / HOUSEKEEP_HZ
        ticks = 0
        while True:
            await asyncio.sleep(period)
            ticks += 1
            self.engine.state.finalize_stale_utterances()
            self.engine.state.clear_stale_error()
            # poll hardware ~1 Hz, but not mid-session (don't disturb the running device view)
            if ticks % HOUSEKEEP_HZ == 0 and not self.engine.state.session_running:
                self.engine.refresh_devices()

    # -- run ---------------------------------------------------------------------------------
    async def serve_forever(self, host: str = "127.0.0.1", port: int = 0) -> None:
        async with serve(self.handler, host, port) as server:
            bound = server.sockets[0].getsockname()[1]
            print(f"READY {bound}", flush=True)
            hk = asyncio.create_task(self._housekeeping())
            try:
                await asyncio.Future()  # run until cancelled
            finally:
                hk.cancel()
                await self.engine.shutdown()
