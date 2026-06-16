"""Offline tests for the thin UI: state updates, rendering, and no-op safety. No API/hardware."""
from __future__ import annotations

import asyncio

import pytest

from contracts import Direction
from ui import NullUi, TerminalUi, UiState, _rms_dbfs


def _loud_pcm(n: int = 1600) -> bytes:
    import array

    return array.array("h", [20000, -20000] * (n // 2)).tobytes()


def test_rms_dbfs_silence_is_floor():
    assert _rms_dbfs(b"\x00\x00" * 100) == -90.0


def test_rms_dbfs_empty_is_floor():
    assert _rms_dbfs(b"") == -90.0


def test_rms_dbfs_loud_is_high():
    db = _rms_dbfs(_loud_pcm())
    assert -10.0 < db <= 0.0


def test_state_starts_with_both_directions():
    st = UiState()
    assert set(st.directions) == {Direction.DE_TO_EN, Direction.EN_TO_DE}
    for d in st.directions.values():
        assert d.status == "idle"
        assert d.out_bytes == 0


def test_input_chunk_updates_level_only_for_that_direction():
    st = UiState()
    st.on_input_chunk(Direction.DE_TO_EN, _loud_pcm())
    assert st.directions[Direction.DE_TO_EN].in_level_dbfs > -90.0
    assert st.directions[Direction.EN_TO_DE].in_level_dbfs == -90.0


def test_output_chunk_accumulates_bytes():
    st = UiState()
    st.on_output_chunk(Direction.EN_TO_DE, b"\x01\x02" * 10)
    st.on_output_chunk(Direction.EN_TO_DE, b"\x03\x04" * 10)
    assert st.directions[Direction.EN_TO_DE].out_bytes == 40


def test_transcript_lines_are_capped_at_three():
    st = UiState()
    for i in range(5):
        st.on_input_transcript(Direction.DE_TO_EN, f"line{i}")
    lines = list(st.directions[Direction.DE_TO_EN].in_lines)
    assert lines == ["line2", "line3", "line4"]


def test_set_status():
    st = UiState()
    st.set_status(Direction.DE_TO_EN, "translating")
    assert st.directions[Direction.DE_TO_EN].status == "translating"


def test_terminal_render_includes_both_labels_and_status():
    st = UiState()
    st.set_status(Direction.DE_TO_EN, "translating")
    st.on_output_transcript(Direction.DE_TO_EN, "Good morning")
    out = TerminalUi(st).render_once()
    assert "DE->EN" in out
    assert "EN->DE" in out
    assert "translating" in out
    assert "Good morning" in out


@pytest.mark.asyncio
async def test_terminal_run_is_cancellable():
    st = UiState()
    ui = TerminalUi(st, interval_s=0.01)
    task = asyncio.create_task(ui.run())
    await asyncio.sleep(0.03)
    task.cancel()
    await task  # must not raise
    assert not ui._running


@pytest.mark.asyncio
async def test_null_ui_run_is_noop():
    ui = NullUi(UiState())
    await ui.run()  # returns immediately, no output, no error
    assert ui.render_once() == ""
