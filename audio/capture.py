"""DJI 2ch@48k -> two async streams of 100 ms 16k mono InputAudioChunks.

The PortAudio callback does NOTHING heavy: it only deinterleaves int16 frames into two bounded
ring buffers (drop-OLDEST on overflow). Per channel an async task pulls 48k samples, streaming-
resamples to 16k (persistent soxr filter state), accumulates exactly INPUT_FRAMES samples, and
yields InputAudioChunks.

All signal processing is in pure functions (deinterleave, RingBuffer, StreamingResampler,
Chunker) so it can be unit-tested with synthetic audio and no device.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import AsyncIterator, Literal

import numpy as np
import sounddevice as sd
import soxr

import config
import contracts
from contracts import InputAudioChunk
from audio import devices


# --- pure DSP --------------------------------------------------------------------------------

def deinterleave(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split interleaved 2-channel int16 (shape (n*2,) or (n,2)) into (left=ch0, right=ch1)."""
    a = np.ascontiguousarray(frames).reshape(-1, 2)
    return a[:, 0].copy(), a[:, 1].copy()


class RingBuffer:
    """Bounded int16 ring buffer with drop-OLDEST on overflow and an overflow counter.

    Capacity is in samples. push() never blocks; if there is not enough room it discards the
    oldest samples to make space and bumps `overflow` by the number of dropped samples.

    Thread-safe (push runs on the PortAudio callback thread; pop/__len__ on the app/event-loop
    thread). A plain Lock guards the coupled _head/_size/_buf/overflow mutations; the callback
    push is short and holds no await, so it cannot stall the event loop.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = int(capacity)
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._size = 0          # valid samples currently stored
        self._head = 0          # index of oldest sample
        self.overflow = 0       # total samples dropped due to overflow
        self._lock = threading.Lock()

    def push(self, samples: np.ndarray) -> None:
        s = np.asarray(samples, dtype=np.int16).ravel()
        n = s.size
        if n == 0:
            return
        with self._lock:
            if n >= self.capacity:
                # Only the newest `capacity` samples survive.
                dropped = self._size + (n - self.capacity)
                self.overflow += dropped
                self._buf[:] = s[-self.capacity:]
                self._head = 0
                self._size = self.capacity
                return
            free = self.capacity - self._size
            if n > free:
                drop = n - free
                self.overflow += drop
                self._head = (self._head + drop) % self.capacity
                self._size -= drop
            tail = (self._head + self._size) % self.capacity
            first = min(n, self.capacity - tail)
            self._buf[tail:tail + first] = s[:first]
            if first < n:
                self._buf[:n - first] = s[first:]
            self._size += n

    def pop(self, n: int) -> np.ndarray:
        """Pop up to n oldest samples (fewer if not available)."""
        with self._lock:
            n = min(n, self._size)
            out = np.empty(n, dtype=np.int16)
            first = min(n, self.capacity - self._head)
            out[:first] = self._buf[self._head:self._head + first]
            if first < n:
                out[first:] = self._buf[:n - first]
            self._head = (self._head + n) % self.capacity
            self._size -= n
            return out

    def __len__(self) -> int:
        with self._lock:
            return self._size


class StreamingResampler:
    """Persistent-state resampler: feed(piece) -> resampled int16, filter state preserved."""

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._rs = soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="int16")

    def feed(self, samples: np.ndarray, last: bool = False) -> np.ndarray:
        s = np.asarray(samples, dtype=np.int16).ravel()
        out = self._rs.resample_chunk(s, last=last)
        return np.asarray(out, dtype=np.int16).ravel()


class Chunker:
    """Accumulate int16 samples and emit fixed-size frames (bytes)."""

    def __init__(self, frames: int) -> None:
        self.frames = int(frames)
        self._acc = np.empty(0, dtype=np.int16)

    def add(self, samples: np.ndarray) -> list[bytes]:
        self._acc = np.concatenate([self._acc, np.asarray(samples, dtype=np.int16).ravel()])
        out: list[bytes] = []
        while self._acc.size >= self.frames:
            frame = self._acc[:self.frames]
            out.append(frame.tobytes())
            self._acc = self._acc[self.frames:]
        return out

    def flush(self, pad: bool = True) -> bytes | None:
        """Return the remaining tail as one frame (zero-padded), or None if empty."""
        if self._acc.size == 0:
            return None
        tail = self._acc
        self._acc = np.empty(0, dtype=np.int16)
        if pad and tail.size < self.frames:
            tail = np.concatenate([tail, np.zeros(self.frames - tail.size, dtype=np.int16)])
        return tail.tobytes()


# --- live capture wiring (thin) --------------------------------------------------------------

def _now() -> int:
    return time.monotonic_ns()


class CaptureEngine:
    """Owns the DJI InputStream and two per-channel ring buffers + resamplers.

    The sounddevice callback only deinterleaves into the ring buffers. Per-channel async tasks
    drain the buffers, resample, chunk, and feed bounded asyncio.Queues that capture_channel()
    drains as InputAudioChunks.
    """

    def __init__(self, device_config: config.DeviceConfig | None = None,
                 device_index: int | None = None) -> None:
        self.cfg = device_config or config.DeviceConfig()
        self._device_index = device_index
        self._native_rate = self.cfg.input_native_rate
        cap = self._native_rate  # ~1 s of headroom per channel
        self._rings = {"left": RingBuffer(cap), "right": RingBuffer(cap)}
        self._queues: dict[str, asyncio.Queue[InputAudioChunk]] = {}
        self._resamplers: dict[str, StreamingResampler] = {}
        self._chunkers: dict[str, Chunker] = {}
        self._seq = {"left": 0, "right": 0}
        self._frames_in = {"left": 0, "right": 0}
        self._stream: sd.InputStream | None = None
        self._drain_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    # -- callback (no alloc/await/print; just deinterleave + ring push) --
    def _callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover (device)
        left, right = deinterleave(np.frombuffer(memoryview(indata), dtype=np.int16))
        self._rings["left"].push(left)
        self._rings["right"].push(right)

    async def _drain(self) -> None:
        # Poll the rings, resample + chunk, push to queues. Tight loop, no device touching.
        while self._running:
            produced = False
            for ch in ("left", "right"):
                samples = self._rings[ch].pop(self._native_rate)  # up to ~1s
                if samples.size:
                    produced = True
                    res = self._resamplers[ch].feed(samples)
                    for frame in self._chunkers[ch].add(res):
                        self._emit(ch, frame)
            if not produced:
                await asyncio.sleep(0.005)
            else:
                await asyncio.sleep(0)

    def _emit(self, ch: str, frame: bytes) -> None:
        seq = self._seq[ch]
        self._seq[ch] += 1
        chunk = InputAudioChunk(
            pcm_s16le=frame, seq=seq, source=ch,  # type: ignore[arg-type]
            t_capture_ns=_now(), source_frame0=seq * contracts.INPUT_FRAMES)
        q = self._queues[ch]
        if q.full():
            try:
                q.get_nowait()  # drop-oldest
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(chunk)

    def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_event_loop()
        for ch in ("left", "right"):
            self._queues[ch] = asyncio.Queue(maxsize=contracts.INPUT_QUEUE_MAX_CHUNKS)
            self._resamplers[ch] = StreamingResampler(self._native_rate, contracts.INPUT_RATE)
            self._chunkers[ch] = Chunker(contracts.INPUT_FRAMES)
        idx = self._device_index
        if idx is None:
            idx = devices.resolve_device(self.cfg.input_name, "input", min_channels=2)
        self._stream = sd.InputStream(
            device=idx, channels=2, samplerate=self._native_rate,
            dtype="int16", callback=self._callback)
        self._stream.start()
        self._running = True
        self._drain_task = self._loop.create_task(self._drain())

    def stop(self) -> None:
        self._running = False
        if self._drain_task is not None:
            self._drain_task.cancel()
            self._drain_task = None
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def capture_channel(
        self, source: Literal["left", "right"]
    ) -> AsyncIterator[InputAudioChunk]:
        q = self._queues[source]
        while self._running or not q.empty():
            try:
                yield await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if not self._running:
                    break
