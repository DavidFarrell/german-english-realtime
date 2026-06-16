"""PortAudioRuntime: the real AudioRuntime, composing capture + output.

Implements contracts.AudioRuntime (input_source, enqueue_output) and manages the underlying
PortAudio streams via start()/stop() and a context-manager lifecycle.
"""
from __future__ import annotations

from typing import AsyncIterator, Literal

import config
from contracts import InputAudioChunk, OutputAudioChunk, OutputChannel
from audio.capture import CaptureEngine
from audio.output import OutputEngine


class PortAudioRuntime:
    """Real AudioRuntime backed by PortAudio (sounddevice)."""

    def __init__(self, device_config: config.DeviceConfig | None = None,
                 input_index: int | None = None, output_index: int | None = None) -> None:
        self.cfg = device_config or config.DeviceConfig()
        self.capture = CaptureEngine(self.cfg, device_index=input_index)
        self.output = OutputEngine(self.cfg, device_index=output_index)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.output.start()
        self.capture.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self.capture.stop()
        self.output.stop()
        self._started = False

    def __enter__(self) -> "PortAudioRuntime":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- AudioRuntime protocol --
    def input_source(self, source: Literal["left", "right"]) -> AsyncIterator[InputAudioChunk]:
        return self.capture.capture_channel(source)

    def enqueue_output(self, channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        return self.output.enqueue(channel, audio)
