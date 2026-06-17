"""Bridge entry point: `python -m gui_bridge`.

Loads .env (for GEMINI_API_KEY), builds the real GuiEngine, and serves the WebSocket forever,
printing `READY <port>` once bound so Electron main knows where to connect. SIGINT/SIGTERM shut
the engine down cleanly.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from gui_bridge.engine import GuiEngine  # noqa: E402
from gui_bridge.server import BridgeServer  # noqa: E402


async def _main() -> None:
    engine = GuiEngine()
    server = BridgeServer(engine)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    serve_task = asyncio.create_task(server.serve_forever())
    await stop.wait()
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(_main())
