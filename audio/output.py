"""Stereo jitter-buffered output to the Bose 2ch@44100.

Two INDEPENDENT direction buffers (LEFT, RIGHT) - never mixed. enqueue() resamples 24k model
output -> device rate (persistent soxr state per channel) and appends to that channel's jitter
buffer. The OutputStream callback pulls from BOTH buffers and writes interleaved L/R int16,
zero-filling a channel on underrun and dropping oldest beyond OUTPUT_JITTER_MAX_MS.

DSP is in pure pieces (StreamingResampler reused from capture; ChannelJitterBuffer) so output
behaviour is unit-testable with no device.
"""
from __future__ import annotations

import threading
import time
from typing import Literal

import numpy as np
import sounddevice as sd

import config
import contracts
from contracts import OutputAudioChunk, OutputChannel
from audio import devices
from audio.capture import StreamingResampler


def _now() -> int:
    return time.monotonic_ns()


class ChannelJitterBuffer:
    """A single-direction FIFO of int16 samples at device rate, bounded by max_ms.

    Thread-safe (the audio callback runs on PortAudio's thread; enqueue on an app thread).
    pull(n) always returns exactly n samples, zero-filling on underrun and counting it.
    """

    def __init__(self, device_rate: int, max_ms: int) -> None:
        self.device_rate = int(device_rate)
        self.max_samples = int(device_rate * max_ms / 1000)
        self._buf = np.empty(0, dtype=np.int16)
        self._lock = threading.Lock()
        self.underruns = 0          # callback frames that had to be zero-filled
        self.dropped = 0            # samples dropped for exceeding max

    def append(self, samples: np.ndarray) -> None:
        s = np.asarray(samples, dtype=np.int16).ravel()
        with self._lock:
            self._buf = np.concatenate([self._buf, s])
            if self._buf.size > self.max_samples:
                drop = self._buf.size - self.max_samples
                self.dropped += drop
                self._buf = self._buf[drop:]   # drop oldest

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.int16)
        with self._lock:
            have = min(n, self._buf.size)
            if have:
                out[:have] = self._buf[:have]
                self._buf = self._buf[have:]
            if have < n:
                self.underruns += (n - have)
        return out

    def depth(self) -> int:
        with self._lock:
            return self._buf.size


class OutputEngine:
    """Owns the Bose OutputStream and the two direction jitter buffers + resamplers."""

    def __init__(self, device_config: config.DeviceConfig | None = None,
                 device_index: int | None = None) -> None:
        self.cfg = device_config or config.DeviceConfig()
        self._device_index = device_index
        self._device_rate = self.cfg.output_native_rate
        self._buffers: dict[OutputChannel, ChannelJitterBuffer] = {
            ch: ChannelJitterBuffer(self._device_rate, contracts.OUTPUT_JITTER_MAX_MS)
            for ch in OutputChannel
        }
        self._resamplers: dict[OutputChannel, StreamingResampler] = {
            ch: StreamingResampler(contracts.MODEL_OUTPUT_RATE, self._device_rate)
            for ch in OutputChannel
        }
        self._stream: sd.OutputStream | None = None
        self._running = False
        self.available = False     # True once an output stream is actually open

    def enqueue(self, channel: OutputChannel, audio: OutputAudioChunk) -> bool:
        pcm = np.frombuffer(audio.pcm_s16le, dtype=np.int16)
        res = self._resamplers[channel].feed(pcm)
        self._buffers[channel].append(res)
        return True

    def enqueue_device_samples(self, channel: OutputChannel, samples: np.ndarray) -> bool:
        """Append samples that are ALREADY at the device rate, bypassing the 24k->device resampler.

        For non-model audio that the GUI plays directly into one earbud (channel-test loopback,
        test tones). The caller is responsible for resampling to `self._device_rate`. L/R are still
        never mixed - this only touches the one channel's jitter buffer.
        """
        self._buffers[channel].append(np.asarray(samples, dtype=np.int16).ravel())
        return True

    @property
    def device_rate(self) -> int:
        return self._device_rate

    # -- callback: pull from both buffers, interleave L/R; no mixing, no alloc-heavy work --
    def _callback(self, outdata, frames, time_info, status) -> None:  # pragma: no cover (device)
        left = self._buffers[OutputChannel.LEFT].pull(frames)
        right = self._buffers[OutputChannel.RIGHT].pull(frames)
        buf = np.frombuffer(memoryview(outdata), dtype=np.int16).reshape(-1, 2)
        buf[:, 0] = left
        buf[:, 1] = right

    def start(self) -> None:
        """Open the output stream. If the device is absent (e.g. earbuds offline) this raises
        DeviceError; PortAudioRuntime catches that and runs capture-only, leaving `available`
        False so enqueue/enqueue_device_samples become no-ops until an output is connected."""
        if self._running:
            return
        idx = self._device_index
        if idx is None:
            idx = devices.resolve_device(self.cfg.output_name, "output", min_channels=2)
        # Use the device's OWN native sample rate (Bose 44.1k, AirPods/speakers 48k, ...) instead
        # of a hard-coded rate, so the stream actually opens. Rebuild buffers/resamplers to match.
        dev_rate = int(sd.query_devices(idx)["default_samplerate"]) or self._device_rate
        if dev_rate != self._device_rate:
            self._device_rate = dev_rate
            self._buffers = {ch: ChannelJitterBuffer(dev_rate, contracts.OUTPUT_JITTER_MAX_MS)
                             for ch in OutputChannel}
            self._resamplers = {ch: StreamingResampler(contracts.MODEL_OUTPUT_RATE, dev_rate)
                                for ch in OutputChannel}
        self._stream = sd.OutputStream(
            device=idx, channels=2, samplerate=self._device_rate,
            dtype="int16", callback=self._callback)
        self._stream.start()
        self._running = True
        self.available = True

    def stop(self) -> None:
        self._running = False
        self.available = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def depth(self, channel: OutputChannel) -> int:
        return self._buffers[channel].depth()

    def underruns(self, channel: OutputChannel) -> int:
        return self._buffers[channel].underruns

    def render_block(self, frames: int) -> np.ndarray:
        """Test/headless helper: produce one interleaved (frames, 2) int16 block, as the
        callback would, without a device."""
        left = self._buffers[OutputChannel.LEFT].pull(frames)
        right = self._buffers[OutputChannel.RIGHT].pull(frames)
        return np.stack([left, right], axis=1)
