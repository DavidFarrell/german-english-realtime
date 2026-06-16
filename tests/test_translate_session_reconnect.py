"""Offline test for the go_away -> reconnect path in translate_session.live_events.

No API, no hardware. We monkeypatch genai.Client with a fake whose live.connect hands out
scripted fake sessions. The first session emits a resumption handle then go_away; the second
session then runs to normal completion. We assert that:
  - audio is NOT lost across the reconnect (every input chunk reaches a session),
  - audio_stream_end is sent exactly once, only after the source genuinely finishes,
  - the reconnected session uses the resumption handle.

This is the case the bug report flagged: a single-pass `source` was re-consumed across
reconnects, so the second session got only a stream-end and zero audio.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

import translate_session
from contracts import Direction, InputAudioChunk, LiveSessionConfig

pytestmark = pytest.mark.asyncio


def _chunk(seq: int) -> InputAudioChunk:
    return InputAudioChunk(pcm_s16le=bytes([seq % 256]) * 4, seq=seq, source="fixture")


async def _source(n: int) -> AsyncIterator[InputAudioChunk]:
    for i in range(n):
        yield _chunk(i)
        await asyncio.sleep(0)


class _FakeSession:
    """A scripted live session. Records sends; yields a fixed list of messages on receive()."""

    def __init__(self, messages: list[Any], log: dict[str, Any]) -> None:
        self._messages = messages
        self._log = log
        self.sent_audio: list[bytes] = []
        self.stream_ended = 0
        log["sessions"].append(self)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def send_realtime_input(self, audio: Any = None, audio_stream_end: bool = False) -> None:
        if audio_stream_end:
            self.stream_ended += 1
        elif audio is not None:
            self.sent_audio.append(audio.data)

    async def receive(self) -> AsyncIterator[Any]:
        for m in self._messages:
            await asyncio.sleep(0)
            yield m


class _Msg:
    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _Handle:
    def __init__(self, new_handle: str) -> None:
        self.new_handle = new_handle


class _FakeLive:
    def __init__(self, sessions: list[_FakeSession], log: dict[str, Any]) -> None:
        self._sessions = sessions
        self._log = log

    def connect(self, model: str, config: Any) -> _FakeSession:
        # Record the resumption handle the SDK was asked to resume with (None on first connect).
        sr = getattr(config, "session_resumption", None)
        self._log["handles"].append(getattr(sr, "handle", None))
        return self._sessions.pop(0)


class _FakeAio:
    def __init__(self, live: _FakeLive) -> None:
        self.live = live


class _FakeClient:
    def __init__(self, sessions: list[_FakeSession], log: dict[str, Any]) -> None:
        self.aio = _FakeAio(_FakeLive(sessions, log))


async def test_go_away_reconnect_preserves_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    log: dict[str, Any] = {"sessions": [], "handles": []}

    # First session: hand out a resumption handle, then go_away (forces a reconnect).
    s1_messages = [
        _Msg(session_resumption_update=_Handle("h-1")),
        _Msg(go_away=object()),
    ]
    # Second (reconnected) session: a few no-op messages give the sender scheduler turns to
    # drain the remaining feed, then complete normally so the generator finishes.
    s2_messages = [
        _Msg(server_content=_Msg()) for _ in range(20)
    ] + [_Msg(server_content=_Msg(turn_complete=True))]
    sessions = [
        _FakeSession(s1_messages, log),
        _FakeSession(s2_messages, log),
    ]

    def fake_client_factory(*a: Any, **k: Any) -> _FakeClient:
        return _FakeClient(sessions, log)

    monkeypatch.setattr(translate_session.genai, "Client", fake_client_factory)

    cfg = LiveSessionConfig(direction=Direction.DE_TO_EN)
    n = 6
    saw_go_away = False
    statuses: list[str | None] = []

    async for ev in translate_session.live_events(cfg, _source(n)):
        if ev.kind == "status":
            statuses.append(ev.detail)
            if ev.detail == "go_away":
                saw_go_away = True
        elif ev.kind == "error":
            pytest.fail(f"unexpected error event: {ev.detail}")

    assert saw_go_away, "expected a go_away status event"

    s1, s2 = log["sessions"]
    # Every input chunk must have been delivered to *some* session - nothing lost on reconnect.
    total_sent = len(s1.sent_audio) + len(s2.sent_audio)
    assert total_sent == n, f"expected all {n} chunks sent across sessions, got {total_sent}"
    # The reconnected session must have received real audio, not just a stream-end.
    assert s2.sent_audio, "reconnected session received zero audio (the bug)"
    # audio_stream_end only on true source completion: never on the go_away session, once total.
    assert s1.stream_ended == 0, "stream-end must not be sent on a go_away reconnect"
    assert s2.stream_ended == 1, "stream-end should be sent exactly once on true completion"
    # The reconnect used the handle from the first session's resumption update.
    assert log["handles"] == [None, "h-1"], log["handles"]


async def test_normal_completion_sends_all_audio_and_one_stream_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: dict[str, Any] = {"sessions": [], "handles": []}
    messages = [_Msg(server_content=_Msg()) for _ in range(20)] + [
        _Msg(server_content=_Msg(turn_complete=True))
    ]
    sessions = [_FakeSession(messages, log)]

    monkeypatch.setattr(
        translate_session.genai, "Client",
        lambda *a, **k: _FakeClient(sessions, log),
    )

    cfg = LiveSessionConfig(direction=Direction.EN_TO_DE)
    n = 4
    async for ev in translate_session.live_events(cfg, _source(n)):
        if ev.kind == "error":
            pytest.fail(f"unexpected error event: {ev.detail}")

    (s1,) = log["sessions"]
    assert len(s1.sent_audio) == n
    assert s1.stream_ended == 1
    assert log["handles"] == [None]
