"""Headless end-to-end smoke test of the GUI bridge - NO API, NO hardware.

Spins up the real BridgeServer backed by a GuiEngine wired to fakes (fake_live_events + a fake
runtime replaying fixtures), connects a WebSocket client, drives the protocol, and asserts the
state stream reflects the commands. Proves the bridge composes and the protocol round-trips before
Electron is in the picture.

    python tools/bridge_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fakes  # noqa: E402
from contracts import OutputChannel  # noqa: E402
from gui_bridge.engine import GuiEngine  # noqa: E402
from gui_bridge.server import BridgeServer  # noqa: E402


class SmokeRuntime(fakes.FakeAudioRuntime):
    """FakeAudioRuntime + start/stop + an `output` shim so loopback/test-ear paths don't crash."""

    class _Out:
        device_rate = 44_100

        def __init__(self) -> None:
            self.device_samples: dict[OutputChannel, int] = {c: 0 for c in OutputChannel}

        def enqueue_device_samples(self, channel: OutputChannel, samples: np.ndarray) -> bool:
            self.device_samples[channel] += int(np.asarray(samples).size)
            return True

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.output = SmokeRuntime._Out()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _runtime_factory(_in_idx, _out_idx):
    return SmokeRuntime(
        left_wav=str(ROOT / "tests/fixtures/de_morgen.16k.wav"),
        right_wav=str(ROOT / "tests/fixtures/en_short.16k.wav"),
        realtime=False,
    )


async def _recv_until(ws, pred, timeout=5.0):
    """Read state frames until pred(state) is true; return that state."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time())
        msg = json.loads(raw)
        if msg.get("type") == "state":
            last = msg
            if pred(msg):
                return msg
    raise AssertionError(f"condition not met within {timeout}s; last={json.dumps(last)[:400]}")


async def main() -> int:
    engine = GuiEngine(live_events_fn=fakes.fake_live_events, runtime_factory=_runtime_factory)
    server = BridgeServer(engine)

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("  PASS " if cond else "  FAIL ") + label)
        if not cond:
            failures.append(label)

    async with websockets.serve(server.handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        hk = asyncio.create_task(server._housekeeping())
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            hello = json.loads(await ws.recv())
            check(hello.get("type") == "hello", "hello received")
            check("inputDevices" in hello and "outputDevices" in hello, "hello carries device lists")

            s0 = json.loads(await ws.recv())
            check(s0.get("type") == "state" and s0["wizardStep"] == "splash", "initial state = splash")

            await ws.send(json.dumps({"cmd": "gotoStep", "step": "inputs"}))
            await _recv_until(ws, lambda s: s["wizardStep"] == "inputs")
            check(True, "gotoStep -> inputs")

            await ws.send(json.dumps({"cmd": "setName", "side": "left", "name": "Davey"}))
            await ws.send(json.dumps({"cmd": "setGain", "value": 0.9}))
            await _recv_until(ws, lambda s: s["sides"]["left"]["name"] == "Davey" and s["gain"] == 0.9)
            check(True, "setName + setGain reflected")

            await ws.send(json.dumps({"cmd": "setLanguage", "side": "left", "lang": "de"}))
            st = await _recv_until(ws, lambda s: s["sides"]["left"]["lang"] == "de")
            check(st["sides"]["right"]["lang"] == "en", "setLanguage enforces opposite side")

            # mic test streams a level without translation
            await ws.send(json.dumps({"cmd": "testMic", "side": "left", "on": True}))
            await _recv_until(ws, lambda s: s["sides"]["left"]["testing"] is True, timeout=4)
            check(True, "testMic on -> testing flag")
            await ws.send(json.dumps({"cmd": "testMic", "side": "left", "on": False}))
            await _recv_until(ws, lambda s: s["sides"]["left"]["testing"] is False, timeout=4)
            check(True, "testMic off")

            # channel test: own mic -> SAME physical side's ear (independent of crossed live route)
            await ws.send(json.dumps({"cmd": "gotoStep", "step": "channel"}))
            await ws.send(json.dumps({"cmd": "startChannelTest", "side": "left"}))
            await _recv_until(ws, lambda s: s["channelTest"]["active"]
                              and s["channelTest"]["side"] == "left", timeout=4)
            await asyncio.sleep(0.5)  # let the ~0.3 s delay line release into the buffer
            ds = engine.runtime.output.device_samples
            check(ds[OutputChannel.LEFT] > 0 and ds[OutputChannel.RIGHT] == 0,
                  "channel test plays into the SAME side's ear (left mic -> left ear)")
            await ws.send(json.dumps({"cmd": "channelTestAnswer", "side": "left", "ok": True}))
            await _recv_until(ws, lambda s: s["channelTest"]["confirmed"]["left"] is True, timeout=3)
            check(True, "channelTestAnswer confirms the side")

            # go live: fake sessions should produce transcript utterances
            await ws.send(json.dumps({"cmd": "startLive"}))
            st = await _recv_until(ws, lambda s: s["session"]["running"] is True
                                   and len(s["session"]["utterances"]) > 0, timeout=8)
            utts = st["session"]["utterances"]
            check(any(u["source"] for u in utts), "live produced transcript utterances")
            check(any(u["translation"] for u in utts), "utterances carry translations")

            await ws.send(json.dumps({"cmd": "swapEarbuds"}))
            await asyncio.sleep(0.2)
            check(engine.routing  # left direction now plays RIGHT
                  and engine.routing.__class__ is dict, "swapEarbuds dispatched")

            await ws.send(json.dumps({"cmd": "stopLive"}))
            await _recv_until(ws, lambda s: s["session"]["running"] is False, timeout=5)
            check(True, "stopLive")

            await ws.send(json.dumps({"cmd": "shutdown"}))
            await asyncio.sleep(0.1)

        hk.cancel()

    print("\nRESULT:", "PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
