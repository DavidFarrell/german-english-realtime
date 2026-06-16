"""Thin, plain, swappable terminal status view.

PROVISIONAL UI (see notes/ui-is-provisional.md). The real visuals are designed separately in
Claude Design; this is disposable scaffolding. Keep ALL logic out of the view: the app owns the
engine and merely pushes facts into `UiState`; the view only reads `UiState` and prints.

Two pieces:
  - `UiState`: a tiny mutable state object the app updates (per-direction levels, last transcript
    lines, session status). No business logic, no I/O.
  - `TerminalUi`: a no-op-safe renderer that periodically prints a plain snapshot of `UiState`.
    `NullUi` is a drop-in that prints nothing, so the app runs fine with no UI at all.

The app talks to a UI only through `update_*` / `set_status` and an async `run()` loop, so the
whole view can be replaced (Electron, curses, web) without touching the engine.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from contracts import Direction


def _rms_dbfs(pcm_s16le: bytes) -> float:
    """Rough RMS level of an s16le buffer in dBFS (-inf floored to -90). View-side only."""
    n = len(pcm_s16le) // 2
    if n == 0:
        return -90.0
    import array

    a = array.array("h")
    a.frombytes(pcm_s16le[: n * 2])
    mean_sq = sum(v * v for v in a) / n
    if mean_sq <= 0:
        return -90.0
    lin = math.sqrt(mean_sq) / 32768.0
    return max(-90.0, 20.0 * math.log10(lin)) if lin > 0 else -90.0


@dataclass
class DirectionState:
    """Per-direction running view state. Plain data; the app fills it in."""

    label: str
    in_level_dbfs: float = -90.0
    out_level_dbfs: float = -90.0
    in_lines: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    out_lines: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    out_bytes: int = 0
    status: str = "idle"
    last_update_ns: int = 0


class UiState:
    """The seam between engine and view. The app mutates this; the view reads it. No logic."""

    def __init__(self, names: dict[Direction, str] | None = None) -> None:
        names = names or {
            Direction.DE_TO_EN: "DE->EN (left in / left out)",
            Direction.EN_TO_DE: "EN->DE (right in / right out)",
        }
        self.directions: dict[Direction, DirectionState] = {
            d: DirectionState(label=names[d]) for d in (Direction.DE_TO_EN, Direction.EN_TO_DE)
        }
        self.started_ns = time.monotonic_ns()

    # -- update API the app calls (cheap, non-blocking) --
    def on_input_chunk(self, direction: Direction, pcm_s16le: bytes) -> None:
        st = self.directions[direction]
        st.in_level_dbfs = _rms_dbfs(pcm_s16le)
        st.last_update_ns = time.monotonic_ns()

    def on_output_chunk(self, direction: Direction, pcm_s16le: bytes) -> None:
        st = self.directions[direction]
        st.out_level_dbfs = _rms_dbfs(pcm_s16le)
        st.out_bytes += len(pcm_s16le)
        st.last_update_ns = time.monotonic_ns()

    def on_input_transcript(self, direction: Direction, text: str) -> None:
        self.directions[direction].in_lines.append(text.strip())

    def on_output_transcript(self, direction: Direction, text: str) -> None:
        self.directions[direction].out_lines.append(text.strip())

    def set_status(self, direction: Direction, status: str) -> None:
        self.directions[direction].status = status


def _bar(dbfs: float, width: int = 20) -> str:
    """A plain ASCII level meter from a dBFS value (-60..0 mapped across `width`)."""
    frac = max(0.0, min(1.0, (dbfs + 60.0) / 60.0))
    filled = int(frac * width)
    return "#" * filled + "-" * (width - filled)


class TerminalUi:
    """A minimal periodic-print status view. Plain text, no colour, no branding."""

    def __init__(self, state: UiState, interval_s: float = 0.5, stream=None) -> None:
        self.state = state
        self.interval_s = interval_s
        self.stream = stream or sys.stderr
        self._running = False

    def render_once(self) -> str:
        lines = ["", "=== two-way translator ==="]
        for direction in (Direction.DE_TO_EN, Direction.EN_TO_DE):
            st = self.state.directions[direction]
            lines.append(f"[{st.label}]  status={st.status}  out={st.out_bytes:,}B")
            lines.append(f"  in  {_bar(st.in_level_dbfs)}  {st.in_level_dbfs:6.1f} dBFS")
            lines.append(f"  out {_bar(st.out_level_dbfs)}  {st.out_level_dbfs:6.1f} dBFS")
            if st.in_lines:
                lines.append(f"  heard : {' '.join(st.in_lines)}")
            if st.out_lines:
                lines.append(f"  spoke : {' '.join(st.out_lines)}")
        return "\n".join(lines)

    async def run(self) -> None:
        """Render periodically until cancelled. Safe to cancel at shutdown."""
        self._running = True
        try:
            while self._running:
                print(self.render_once(), file=self.stream, flush=True)
                await asyncio.sleep(self.interval_s)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False


class NullUi:
    """No-op UI. Proves the app runs fine without any view (the UI must be optional)."""

    def __init__(self, state: UiState | None = None) -> None:
        self.state = state

    def render_once(self) -> str:
        return ""

    async def run(self) -> None:
        return
