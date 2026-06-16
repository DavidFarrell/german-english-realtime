"""Real Gemini Live-Translate session: one direction in, SessionEvents out.

Implements contracts.live_events. One unidirectional translate session per call (two-way
translation = two of these, one per Direction). The send/receive surface mirrors the verified
reference in tools/slice0_roundtrip.py.

Structure:
  - `_events_from_server_content` is a PURE parser: a LiveServerContent-shaped object in, a list
    of SessionEvents out. No network, fully unit-testable.
  - `live_events` owns all the network: it opens the session, spawns a sender task that drains
    `source`, runs a receive loop that pushes parsed events onto an asyncio.Queue, and yields
    them. On go_away it emits a status event and (if the SDK exposes resumption) reconnects;
    other exceptions become a single error event instead of crashing the generator.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load GEMINI_API_KEY from the repo .env so every live entrypoint (app --once/--run, latency
# component mode, the hw tools) gets the key without each wiring it up. Idempotent.
load_dotenv(Path(__file__).resolve().parent / ".env")

from contracts import (
    INPUT_QUEUE_MAX_CHUNKS,
    INPUT_RATE,
    Direction,
    InputAudioChunk,
    LiveSessionConfig,
    OutputAudioChunk,
    SessionEvent,
)

# Sentinel pushed onto the queue when the receive loop is finished for good.
_DONE = object()


def _build_connect_config(config: LiveSessionConfig) -> types.LiveConnectConfig:
    """Translate a LiveSessionConfig into the SDK's LiveConnectConfig (verified surface)."""
    kwargs: dict[str, Any] = dict(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=config.target_language_code,
            echo_target_language=config.echo_target_language,
        ),
    )
    if config.input_transcription:
        kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
    if config.output_transcription:
        kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
    # Ask for a resumable session so go_away -> reconnect can pick up where we left off.
    kwargs["session_resumption"] = types.SessionResumptionConfig()
    return types.LiveConnectConfig(**kwargs)


def _events_from_server_content(server_content: Any, direction: Direction) -> list[SessionEvent]:
    """PURE parser: turn one server_content into SessionEvents.

    Handles ALL fields on a single event (audio + transcripts + completion can co-occur), in a
    stable order: audio, input_transcript, output_transcript, then a turn_complete status. Never
    `continue`s past audio. No network, no clock dependency beyond time.monotonic_ns().
    """
    events: list[SessionEvent] = []
    if server_content is None:
        return events

    seq = 0
    model_turn = getattr(server_content, "model_turn", None)
    if model_turn is not None:
        for part in getattr(model_turn, "parts", None) or ():
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline is not None else None
            if data:
                t = time.monotonic_ns()
                events.append(
                    SessionEvent(
                        direction=direction,
                        kind="audio",
                        t_ns=t,
                        audio=OutputAudioChunk(
                            pcm_s16le=data,
                            direction=direction,
                            seq=seq,
                            t_received_ns=t,
                        ),
                    )
                )
                seq += 1

    in_tx = getattr(server_content, "input_transcription", None)
    in_text = getattr(in_tx, "text", None) if in_tx is not None else None
    if in_text:
        events.append(
            SessionEvent(direction=direction, kind="input_transcript",
                         t_ns=time.monotonic_ns(), text=in_text)
        )

    out_tx = getattr(server_content, "output_transcription", None)
    out_text = getattr(out_tx, "text", None) if out_tx is not None else None
    if out_text:
        events.append(
            SessionEvent(direction=direction, kind="output_transcript",
                         t_ns=time.monotonic_ns(), text=out_text)
        )

    if getattr(server_content, "turn_complete", None) or getattr(
        server_content, "generation_complete", None
    ):
        events.append(
            SessionEvent(direction=direction, kind="status",
                         t_ns=time.monotonic_ns(), detail="turn_complete")
        )

    return events


# Sentinel pushed onto the input feed when `source` is genuinely exhausted (not a reconnect).
_SOURCE_END = object()


async def _feed_source(source: AsyncIterator[InputAudioChunk], feed: asyncio.Queue[Any]) -> None:
    """Drain `source` ONCE into `feed`, then push _SOURCE_END so a sender can flush.

    Created once per live_events call. Decoupling the (single-pass) source from the sender means
    a go_away -> reconnect resumes from the live tail of the audio instead of re-consuming an
    already-exhausted iterator. Backpressure is bounded by `feed`'s maxsize.
    """
    try:
        async for chunk in source:
            await feed.put(chunk)
    finally:
        await feed.put(_SOURCE_END)


async def _drain_source(session: Any, feed: asyncio.Queue[Any]) -> bool:
    """Sender task: pump chunks from the shared `feed` in.

    Returns True if the source is genuinely finished (we hit _SOURCE_END and sent
    audio_stream_end), or False if the task was cancelled mid-stream (e.g. a go_away reconnect),
    leaving any unsent tail in `feed` for the next session. Only a True return means we told the
    model the speech is over.
    """
    mime = f"audio/pcm;rate={INPUT_RATE}"
    while True:
        item = await feed.get()
        if item is _SOURCE_END:
            await session.send_realtime_input(audio_stream_end=True)
            return True
        await session.send_realtime_input(
            audio=types.Blob(data=item.pcm_s16le, mime_type=mime)
        )


async def live_events(
    config: LiveSessionConfig,
    source: AsyncIterator[InputAudioChunk],
) -> AsyncIterator[SessionEvent]:
    """Open a Gemini Live-Translate session for `config.direction`, pump `source` in, yield events.

    Network lives here only. A queue decouples the receive loop from the consumer; closing the
    generator cancels the sender and closes the session.
    """
    direction = config.direction
    client = genai.Client(http_options=types.HttpOptions(api_version=config.api_version))
    queue: asyncio.Queue[Any] = asyncio.Queue()
    # One shared input feed, drained once from `source` by `feeder`. Each (re)connected session's
    # sender pulls from it, so a go_away reconnect continues from the live audio tail rather than
    # a consumed iterator. Bounded so the feeder applies backpressure to the source.
    feed: asyncio.Queue[Any] = asyncio.Queue(maxsize=INPUT_QUEUE_MAX_CHUNKS)
    feeder = asyncio.create_task(_feed_source(source, feed))

    async def run_session(resumption_handle: str | None) -> str | None:
        """Open one connection, run sender + receive loop, feed `queue`.

        Returns a resumption handle if the SDK handed one out and we should reconnect (go_away),
        or None when the stream completed normally or resumption is unavailable.
        """
        connect_config = _build_connect_config(config)
        if resumption_handle is not None:
            connect_config.session_resumption = types.SessionResumptionConfig(
                handle=resumption_handle
            )

        next_handle = resumption_handle
        async with client.aio.live.connect(model=config.model, config=connect_config) as session:
            # Sender pulls from the shared `feed`. On go_away it's cancelled mid-stream and any
            # unsent chunks remain in `feed` for the next session; only a clean _SOURCE_END makes
            # it send audio_stream_end. So a reconnect resumes from the live tail.
            sender = asyncio.create_task(_drain_source(session, feed))
            try:
                async for message in session.receive():
                    update = getattr(message, "session_resumption_update", None)
                    if update is not None and getattr(update, "new_handle", None):
                        next_handle = update.new_handle

                    go_away = getattr(message, "go_away", None)
                    if go_away is not None:
                        queue.put_nowait(
                            SessionEvent(direction=direction, kind="status",
                                         t_ns=time.monotonic_ns(), detail="go_away")
                        )
                        # Reconnect with the latest handle if we have one; else stop.
                        return next_handle

                    server_content = getattr(message, "server_content", None)
                    for event in _events_from_server_content(server_content, direction):
                        queue.put_nowait(event)
            finally:
                sender.cancel()
                try:
                    await sender
                except (asyncio.CancelledError, Exception):
                    pass
        return None

    async def pump() -> None:
        """Drive run_session, reconnecting on go_away; surface failures as one error event."""
        handle: str | None = None
        try:
            while True:
                handle = await run_session(handle)
                if handle is None:
                    break
                # go_away with a resumable handle: loop and reconnect.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never crash the generator; report it
            queue.put_nowait(
                SessionEvent(direction=direction, kind="error",
                             t_ns=time.monotonic_ns(), detail=repr(exc))
            )
        finally:
            queue.put_nowait(_DONE)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        # GeneratorExit / normal completion: stop the pump (which cancels the sender + closes
        # the session via the async-with on the way out) and the source feeder.
        pump_task.cancel()
        feeder.cancel()
        for task in (pump_task, feeder):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
