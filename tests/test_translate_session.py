"""Offline unit tests for translate_session._events_from_server_content + a guarded LIVE test.

The offline tests need NO API and NO hardware: they hand simple stub objects (shaped like the
SDK's LiveServerContent) to the pure parser and assert the SessionEvents that come back. The
LIVE test does one real DE->EN round-trip and is skipped unless LIVE is set in the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from contracts import Direction, SessionEvent
from translate_session import _events_from_server_content


# --- minimal stubs that mimic the SDK's server_content shape -------------------------------

@dataclass
class _InlineData:
    data: bytes | None


@dataclass
class _Part:
    inline_data: _InlineData | None = None


@dataclass
class _ModelTurn:
    parts: list[_Part] = field(default_factory=list)


@dataclass
class _Transcription:
    text: str | None = None


@dataclass
class _ServerContent:
    model_turn: _ModelTurn | None = None
    input_transcription: _Transcription | None = None
    output_transcription: _Transcription | None = None
    turn_complete: bool | None = None
    generation_complete: bool | None = None


def _audio_part(data: bytes) -> _Part:
    return _Part(inline_data=_InlineData(data=data))


# --- pure parser tests ---------------------------------------------------------------------

def test_audio_only():
    sc = _ServerContent(model_turn=_ModelTurn(parts=[_audio_part(b"\x01\x02\x03\x04")]))
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert [e.kind for e in events] == ["audio"]
    e = events[0]
    assert e.audio is not None
    assert e.audio.pcm_s16le == b"\x01\x02\x03\x04"
    assert e.audio.direction is Direction.DE_TO_EN
    assert e.audio.seq == 0
    assert e.audio.t_received_ns > 0
    assert e.t_ns == e.audio.t_received_ns


def test_audio_only_multiple_parts_get_sequential_seq():
    sc = _ServerContent(model_turn=_ModelTurn(parts=[
        _audio_part(b"aa"), _audio_part(b"bb"), _audio_part(b"cc"),
    ]))
    events = _events_from_server_content(sc, Direction.EN_TO_DE)
    assert [e.kind for e in events] == ["audio", "audio", "audio"]
    assert [e.audio.seq for e in events] == [0, 1, 2]
    assert [e.audio.pcm_s16le for e in events] == [b"aa", b"bb", b"cc"]
    assert all(e.audio.direction is Direction.EN_TO_DE for e in events)


def test_transcript_only():
    sc = _ServerContent(
        input_transcription=_Transcription(text="Guten Morgen"),
        output_transcription=_Transcription(text="Good morning"),
    )
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert [(e.kind, e.text) for e in events] == [
        ("input_transcript", "Guten Morgen"),
        ("output_transcript", "Good morning"),
    ]
    assert all(e.audio is None for e in events)


def test_empty_transcription_text_is_ignored():
    sc = _ServerContent(
        input_transcription=_Transcription(text=""),
        output_transcription=_Transcription(text=None),
    )
    assert _events_from_server_content(sc, Direction.DE_TO_EN) == []


def test_combined_audio_and_transcript_handles_all_fields():
    # One event carrying audio AND both transcripts must not 'continue' past the audio.
    sc = _ServerContent(
        model_turn=_ModelTurn(parts=[_audio_part(b"\xff\xfe")]),
        input_transcription=_Transcription(text="Hallo"),
        output_transcription=_Transcription(text="Hello"),
    )
    events = _events_from_server_content(sc, Direction.EN_TO_DE)
    assert [e.kind for e in events] == ["audio", "input_transcript", "output_transcript"]
    assert events[0].audio.pcm_s16le == b"\xff\xfe"
    assert events[1].text == "Hallo"
    assert events[2].text == "Hello"


def test_turn_complete_emits_status():
    sc = _ServerContent(turn_complete=True)
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert [e.kind for e in events] == ["status"]
    assert events[0].detail == "turn_complete"


def test_generation_complete_also_emits_status():
    sc = _ServerContent(generation_complete=True)
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert [(e.kind, e.detail) for e in events] == [("status", "turn_complete")]


def test_audio_plus_turn_complete_in_one_event():
    sc = _ServerContent(
        model_turn=_ModelTurn(parts=[_audio_part(b"zz")]),
        turn_complete=True,
    )
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert [e.kind for e in events] == ["audio", "status"]
    assert events[-1].detail == "turn_complete"


def test_none_server_content_yields_nothing():
    assert _events_from_server_content(None, Direction.DE_TO_EN) == []


def test_empty_audio_part_is_skipped():
    sc = _ServerContent(model_turn=_ModelTurn(parts=[_Part(inline_data=_InlineData(data=None))]))
    assert _events_from_server_content(sc, Direction.DE_TO_EN) == []


def test_events_are_session_event_instances():
    sc = _ServerContent(
        model_turn=_ModelTurn(parts=[_audio_part(b"qq")]),
        output_transcription=_Transcription(text="x"),
        turn_complete=True,
    )
    events = _events_from_server_content(sc, Direction.DE_TO_EN)
    assert all(isinstance(e, SessionEvent) for e in events)
    assert all(e.direction is Direction.DE_TO_EN for e in events)


# --- guarded LIVE test (skipped unless LIVE is set) ----------------------------------------

@pytest.mark.asyncio
async def test_live_de_to_en_roundtrip():
    if not os.environ.get("LIVE"):
        pytest.skip("LIVE not set; skipping real API round-trip")

    from pathlib import Path

    import fakes
    from contracts import LiveSessionConfig
    from translate_session import live_events

    fixture = Path(__file__).resolve().parent / "fixtures" / "de_morgen.16k.wav"
    cfg = LiveSessionConfig(direction=Direction.DE_TO_EN)
    source = fakes.fixture_source(fixture, source="fixture", realtime=True)

    audio_bytes = bytearray()
    out_text: list[str] = []
    saw_turn_complete = False

    async for ev in live_events(cfg, source):
        if ev.kind == "audio" and ev.audio is not None:
            audio_bytes.extend(ev.audio.pcm_s16le)
        elif ev.kind == "output_transcript" and ev.text:
            out_text.append(ev.text)
        elif ev.kind == "status" and ev.detail == "turn_complete":
            saw_turn_complete = True
            break
        elif ev.kind == "error":
            pytest.fail(f"live session error: {ev.detail}")

    assert audio_bytes, "expected non-empty translated audio"
    assert "".join(out_text).strip(), "expected a non-empty output transcript"
    assert saw_turn_complete, "expected a turn_complete status event"
