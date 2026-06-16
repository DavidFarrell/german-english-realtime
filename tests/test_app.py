"""Offline composition tests for app.py: routing, the --check path, CLI parsing. No API/hardware.

These prove the integrator's plumbing with fakes only: pump_direction routes events correctly,
both directions run concurrently through the same routing, and the CLI dispatches as documented.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app
import config
import fakes
from contracts import (
    Direction,
    InputAudioChunk,
    LiveSessionConfig,
    OutputAudioChunk,
    OutputChannel,
    SessionEvent,
)
from ui import UiState

ROOT = Path(__file__).resolve().parent.parent


async def _empty_source():
    return
    yield  # pragma: no cover - make this an async generator


async def _scripted_events(events: list[SessionEvent]):
    async def gen(cfg: LiveSessionConfig, source):
        # Drain the source so the (fake) sender doesn't dangle.
        async for _ in source:
            pass
        for ev in events:
            yield ev

    return gen


@pytest.mark.asyncio
async def test_pump_routes_audio_to_resolved_channel_and_counts():
    direction = Direction.DE_TO_EN
    audio = OutputAudioChunk(pcm_s16le=b"\x01\x02" * 5, direction=direction, seq=0, t_received_ns=1)
    events = [
        SessionEvent(direction=direction, kind="input_transcript", t_ns=1, text="Guten Morgen"),
        SessionEvent(direction=direction, kind="output_transcript", t_ns=2, text="Good morning"),
        SessionEvent(direction=direction, kind="audio", t_ns=3, audio=audio),
        SessionEvent(direction=direction, kind="status", t_ns=4, detail="turn_complete"),
    ]
    sink_calls: list[tuple[OutputChannel, int]] = []

    def sink(channel: OutputChannel, a: OutputAudioChunk) -> bool:
        sink_calls.append((channel, len(a.pcm_s16le)))
        return True

    state = UiState()
    gen = await _scripted_events(events)
    stats = await app.pump_direction(direction, _empty_source(), gen, sink, state)

    assert stats.audio_chunks == 1
    assert stats.output_bytes == 10
    assert stats.input_transcript == "Guten Morgen"
    assert stats.output_transcript == "Good morning"
    assert stats.turn_complete is True
    assert stats.error is None
    # DE_TO_EN must route to LEFT (per config.OUTPUT_FOR_DIRECTION).
    assert sink_calls == [(OutputChannel.LEFT, 10)]
    assert state.directions[direction].status == "done"


@pytest.mark.asyncio
async def test_pump_en_to_de_routes_to_right():
    direction = Direction.EN_TO_DE
    audio = OutputAudioChunk(pcm_s16le=b"\xaa\xbb", direction=direction, seq=0, t_received_ns=1)
    events = [SessionEvent(direction=direction, kind="audio", t_ns=1, audio=audio)]
    seen: list[OutputChannel] = []

    def sink(channel: OutputChannel, a: OutputAudioChunk) -> bool:
        seen.append(channel)
        return True

    gen = await _scripted_events(events)
    await app.pump_direction(direction, _empty_source(), gen, sink, UiState())
    assert seen == [OutputChannel.RIGHT]


@pytest.mark.asyncio
async def test_pump_records_error_event():
    direction = Direction.DE_TO_EN
    events = [SessionEvent(direction=direction, kind="error", t_ns=1, detail="boom")]
    gen = await _scripted_events(events)
    state = UiState()
    stats = await app.pump_direction(
        direction, _empty_source(), gen, lambda c, a: True, state)
    assert stats.error == "boom"
    assert "error" in state.directions[direction].status


@pytest.mark.asyncio
async def test_pump_with_fake_live_events_and_fixture_source():
    # End-to-end through the real fake_live_events using a real fixture.
    direction = Direction.DE_TO_EN
    source = fakes.fixture_source(
        ROOT / "tests/fixtures/de_morgen.16k.wav", source="fixture", realtime=False)
    captured: list[int] = []
    stats = await app.pump_direction(
        direction, source, fakes.fake_live_events,
        lambda c, a: (captured.append(len(a.pcm_s16le)) or True), UiState())
    assert stats.audio_chunks > 0
    assert stats.output_bytes == sum(captured)
    assert stats.turn_complete is True


def test_check_returns_zero_and_fills_both_channels():
    rc = app.main(["--check"])
    assert rc == 0


def test_list_devices_runs():
    # Real sounddevice enumeration is fine offline (no stream opened).
    rc = app.main(["--list-devices"])
    assert rc == 0


def test_default_usage_returns_zero():
    assert app.main([]) == 0


def test_parser_modes_mutually_exclusive():
    parser = app._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--check", "--run"])


def test_once_parser_defaults_target_en():
    args = app._build_parser().parse_args(["--once", "x.wav"])
    assert args.once == "x.wav"
    assert args.target == "en"


def test_routing_map_is_not_crossed():
    assert config.OUTPUT_FOR_DIRECTION[Direction.DE_TO_EN] is OutputChannel.LEFT
    assert config.OUTPUT_FOR_DIRECTION[Direction.EN_TO_DE] is OutputChannel.RIGHT
    assert config.SOURCE_FOR_DIRECTION[Direction.DE_TO_EN] == "left"
    assert config.SOURCE_FOR_DIRECTION[Direction.EN_TO_DE] == "right"
