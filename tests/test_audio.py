"""Audio-layer unit tests. NO hardware, NO API.

Pure DSP is tested directly on synthetic audio. The thin sounddevice wiring is exercised with
InputStream/OutputStream monkeypatched to fakes so no real device ever opens.
"""
from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest
import soxr

import config
import contracts
from audio.capture import (
    CaptureEngine,
    Chunker,
    RingBuffer,
    StreamingResampler,
    deinterleave,
)
from audio.devices import DeviceError, resolve_device
from audio.output import ChannelJitterBuffer, OutputEngine
from audio.runtime import PortAudioRuntime
from contracts import OutputAudioChunk, OutputChannel, Direction


# --- deinterleave ----------------------------------------------------------------------------

def test_deinterleave_splits_known_buffer():
    # interleaved L,R,L,R,...  L = 0,1,2,3   R = 100,101,102,103
    inter = np.array([0, 100, 1, 101, 2, 102, 3, 103], dtype=np.int16)
    left, right = deinterleave(inter)
    assert left.tolist() == [0, 1, 2, 3]
    assert right.tolist() == [100, 101, 102, 103]
    assert left.dtype == np.int16 and right.dtype == np.int16


def test_deinterleave_accepts_2d():
    a = np.array([[1, -1], [2, -2], [3, -3]], dtype=np.int16)
    left, right = deinterleave(a)
    assert left.tolist() == [1, 2, 3]
    assert right.tolist() == [-1, -2, -3]


# --- streaming resample ----------------------------------------------------------------------

def _sine(n, freq, rate):
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * freq * t) * 20000).astype(np.int16)


def test_resample_4800_yields_about_1600():
    """48k -> 16k of 4800 input frames yields ~1600 output frames (one 100 ms model chunk)."""
    rs = StreamingResampler(48_000, 16_000)
    sig = _sine(4800, 440, 48_000)
    out = rs.feed(sig, last=True)
    assert abs(out.size - 1600) <= 32  # filter delay tolerance


def test_streaming_resample_preserves_state():
    """Resampling in 100 ms pieces equals resampling the whole signal (within tolerance)."""
    rate_in, rate_out = 48_000, 16_000
    whole_sig = _sine(48_000, 440, rate_in)  # 1 second

    whole = soxr.resample(whole_sig.astype(np.float32), rate_in, rate_out)
    whole = whole.astype(np.int16)

    rs = StreamingResampler(rate_in, rate_out)
    pieces = []
    piece = 4800  # 100 ms at 48k
    for off in range(0, whole_sig.size, piece):
        chunk = whole_sig[off:off + piece]
        last = off + piece >= whole_sig.size
        pieces.append(rs.feed(chunk, last=last))
    streamed = np.concatenate(pieces)

    # Lengths must match closely and the overlapping body must be near-identical.
    assert abs(streamed.size - whole.size) <= 4
    n = min(streamed.size, whole.size)
    diff = np.abs(streamed[:n].astype(np.int32) - whole[:n].astype(np.int32))
    assert diff.mean() < 50  # streaming == whole within small numerical tolerance


def test_streaming_resample_no_boundary_discontinuity():
    """Feeding a continuous tone in many small pieces through ONE streaming resampler produces a
    smooth waveform with no spikes at piece boundaries - the continuity that persistent filter
    state buys us (a fresh resampler per piece would glitch at the seams)."""
    rs = StreamingResampler(48_000, 16_000)
    sig = _sine(48_000, 200, 48_000)  # low freq -> small sample-to-sample steps
    piece = 480  # tiny 10 ms pieces, many boundaries
    out = np.concatenate([
        rs.feed(sig[o:o + piece], last=o + piece >= sig.size)
        for o in range(0, sig.size, piece)
    ])
    # No single-sample jump should exceed what a clean 200 Hz @ 16k tone can produce.
    max_step = np.max(np.abs(np.diff(out.astype(np.int32))))
    assert max_step < 2000  # smooth; a boundary glitch would spike far higher


# --- chunking --------------------------------------------------------------------------------

def test_chunker_emits_exact_3200_byte_frames():
    ch = Chunker(contracts.INPUT_FRAMES)
    frames = ch.add(np.zeros(contracts.INPUT_FRAMES * 2 + 500, dtype=np.int16))
    assert len(frames) == 2
    for f in frames:
        assert len(f) == contracts.INPUT_CHUNK_BYTES == 3200


def test_chunker_partial_tail_flush_padded():
    ch = Chunker(contracts.INPUT_FRAMES)
    assert ch.add(np.zeros(500, dtype=np.int16)) == []  # not enough yet
    tail = ch.flush(pad=True)
    assert tail is not None and len(tail) == contracts.INPUT_CHUNK_BYTES
    assert ch.flush() is None  # nothing left


def test_chunker_accumulates_across_adds():
    ch = Chunker(contracts.INPUT_FRAMES)
    assert ch.add(np.zeros(1000, dtype=np.int16)) == []
    out = ch.add(np.zeros(1000, dtype=np.int16))  # now 2000 >= 1600
    assert len(out) == 1 and len(out[0]) == 3200


# --- ring buffer -----------------------------------------------------------------------------

def test_ring_drop_oldest_keeps_newest_and_counts_overflow():
    rb = RingBuffer(capacity=8)
    rb.push(np.arange(1, 9, dtype=np.int16))     # fills exactly: 1..8
    assert len(rb) == 8 and rb.overflow == 0
    rb.push(np.arange(100, 104, dtype=np.int16))  # 4 more -> drop oldest 4
    assert rb.overflow == 4
    got = rb.pop(8)
    # Oldest 1,2,3,4 dropped; newest = 5,6,7,8,100,101,102,103
    assert got.tolist() == [5, 6, 7, 8, 100, 101, 102, 103]


def test_ring_push_larger_than_capacity_keeps_tail():
    rb = RingBuffer(capacity=4)
    rb.push(np.arange(10, dtype=np.int16))  # 0..9
    assert rb.overflow == 6
    assert rb.pop(4).tolist() == [6, 7, 8, 9]


def test_ring_fifo_order_no_overflow():
    rb = RingBuffer(capacity=10)
    rb.push(np.array([1, 2, 3], dtype=np.int16))
    rb.push(np.array([4, 5], dtype=np.int16))
    assert rb.pop(3).tolist() == [1, 2, 3]
    assert rb.pop(10).tolist() == [4, 5]
    assert rb.overflow == 0


def test_ring_concurrent_push_pop_no_loss_or_dup():
    """Drive push() from a real background thread (the PortAudio-callback role) while pop()
    runs concurrently on this thread (the drain role). The ring is sized large enough that no
    overflow can occur, so every pushed sample must come out exactly once and in order. This
    is the scenario the hand-called-callback tests never exercise; without a lock the coupled
    _head/_size/_buf updates race and would lose, duplicate, or mis-order samples.
    """
    # Unique, non-wrapping int16 values so order/duplication is unambiguous.
    total = 30_000
    src = np.arange(total, dtype=np.int16)
    rb = RingBuffer(capacity=total + 1)  # +1 so it never overflows even at full depth

    def producer() -> None:
        i = 0
        while i < total:
            batch = src[i:i + 37]  # awkward batch size to force wrap-around writes
            rb.push(batch)
            i += batch.size

    popped: list[int] = []
    prod = threading.Thread(target=producer)
    prod.start()
    while len(popped) < total:
        chunk = rb.pop(101)  # awkward read size to force wrap-around reads
        if chunk.size:
            popped.extend(int(x) for x in chunk)
        elif not prod.is_alive():
            # producer done and ring empty -> drain whatever (if anything) is left
            chunk = rb.pop(total)
            popped.extend(int(x) for x in chunk)
            break
    prod.join()
    # Drain any tail the loop missed.
    tail = rb.pop(total)
    popped.extend(int(x) for x in tail)

    assert rb.overflow == 0
    assert len(popped) == total, "lost or duplicated samples across the thread boundary"
    assert popped == src.tolist(), "samples came out re-ordered/corrupted"


# --- output jitter buffer --------------------------------------------------------------------

def test_jitter_zero_fill_on_underrun():
    jb = ChannelJitterBuffer(device_rate=44_100, max_ms=400)
    jb.append(np.full(100, 7, dtype=np.int16))
    out = jb.pull(160)   # ask for more than we have
    assert out[:100].tolist() == [7] * 100
    assert out[100:].tolist() == [0] * 60  # zero-filled
    assert jb.underruns == 60


def test_jitter_drop_oldest_beyond_max():
    jb = ChannelJitterBuffer(device_rate=1000, max_ms=100)  # max_samples = 100
    jb.append(np.arange(80, dtype=np.int16))
    jb.append(np.arange(80, 160, dtype=np.int16))  # total 160 -> drop oldest 60
    assert jb.dropped == 60
    out = jb.pull(100)
    assert out[0] == 60  # first surviving sample is the 61st we appended


def test_output_never_mixes_left_into_right():
    """Feed ONLY the LEFT direction; RIGHT must stay perfectly silent."""
    eng = OutputEngine(config.DeviceConfig())
    # 24k model audio, all loud on LEFT only.
    pcm = np.full(2400, 15000, dtype=np.int16).tobytes()
    eng.enqueue(OutputChannel.LEFT,
                OutputAudioChunk(pcm_s16le=pcm, direction=Direction.DE_TO_EN, seq=0,
                                 t_received_ns=0))
    block = eng.render_block(1024)  # (1024, 2)
    left, right = block[:, 0], block[:, 1]
    assert np.any(left != 0)            # left has signal
    assert np.all(right == 0)           # right is dead silent - no cross-mixing


def test_streaming_resample_24k_to_44100_steady_state_rate():
    """Upsample 24k -> 44100: over a 1 s stream the total output approaches the steady-state
    rate (~44100), confirming persistent state amortises the filter delay (not per-chunk loss)."""
    rs = StreamingResampler(24_000, 44_100)
    total = 0
    for _ in range(10):  # ten 100 ms chunks = 1 s
        total += rs.feed(_sine(2400, 440, 24_000)).size
    total += rs.feed(np.zeros(0, dtype=np.int16), last=True).size
    assert abs(total - 44_100) <= 200


def test_output_enqueue_appends_to_correct_channel_only():
    eng = OutputEngine(config.DeviceConfig())  # 44100 out
    pcm = _sine(2400, 440, 24_000).tobytes()  # 100 ms @ 24k
    eng.enqueue(OutputChannel.RIGHT,
                OutputAudioChunk(pcm_s16le=pcm, direction=Direction.EN_TO_DE, seq=0,
                                 t_received_ns=0))
    assert eng.depth(OutputChannel.RIGHT) > 0
    assert eng.depth(OutputChannel.LEFT) == 0  # other direction untouched


# --- devices (query_devices monkeypatched; no real hardware) ---------------------------------

_FAKE_DEVICES = [
    {"name": "Wireless Mic Rx", "max_input_channels": 2, "max_output_channels": 0,
     "default_samplerate": 48000.0},
    {"name": "Bose QC35 II", "max_input_channels": 0, "max_output_channels": 2,
     "default_samplerate": 44100.0},
    {"name": "Bose QC35 II", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 16000.0},  # the HFP mic entry
    {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0,
     "default_samplerate": 48000.0},
]


@pytest.fixture
def fake_query(monkeypatch):
    monkeypatch.setattr("audio.devices.sd.query_devices", lambda *a, **k: _FAKE_DEVICES)


def test_resolve_input_by_name(fake_query):
    assert resolve_device("Wireless Mic Rx", "input", min_channels=2) == 0


def test_resolve_output_by_name(fake_query):
    assert resolve_device("Bose QC35 II", "output", min_channels=2) == 1


def test_resolve_refuses_bose_as_input(fake_query):
    with pytest.raises(DeviceError, match="Bose|HFP|input"):
        resolve_device("Bose QC35 II", "input", min_channels=2)


def test_resolve_too_few_channels(fake_query):
    with pytest.raises(DeviceError, match="channel"):
        resolve_device("MacBook Pro Microphone", "input", min_channels=2)


def test_resolve_not_found(fake_query):
    with pytest.raises(DeviceError, match="no input device"):
        resolve_device("Nonexistent Device", "input")


# --- thin wiring: streams are mocked, no device opens ----------------------------------------

class _FakeStream:
    """Stand-in for sd.InputStream / sd.OutputStream. Records that it was constructed and
    never touches hardware."""
    instances: list["_FakeStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        _FakeStream.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


@pytest.fixture
def mock_streams(monkeypatch):
    _FakeStream.instances = []
    monkeypatch.setattr("audio.capture.sd.InputStream", _FakeStream)
    monkeypatch.setattr("audio.output.sd.OutputStream", _FakeStream)
    monkeypatch.setattr("audio.capture.devices.resolve_device", lambda *a, **k: 0)
    monkeypatch.setattr("audio.output.devices.resolve_device", lambda *a, **k: 1)
    return _FakeStream


def test_capture_engine_produces_chunks_via_fake_callback(mock_streams):
    """Drive the engine's callback by hand (simulating PortAudio) and assert it yields
    correctly-framed InputAudioChunks - no device required."""
    async def go():
        eng = CaptureEngine(config.DeviceConfig())
        eng.start()
        # Simulate 1 s of interleaved 48k stereo: LEFT=440Hz, RIGHT silent.
        left = _sine(48_000, 440, 48_000)
        right = np.zeros(48_000, dtype=np.int16)
        inter = np.empty(48_000 * 2, dtype=np.int16)
        inter[0::2] = left
        inter[1::2] = right
        # feed in callback-sized blocks
        block = 1024
        for off in range(0, inter.size, block * 2):
            eng._callback(inter[off:off + block * 2], block, None, None)
        # let the drain task run
        chunks = []
        async def collect():
            async for c in eng.capture_channel("left"):
                chunks.append(c)
                if len(chunks) >= 8:
                    break
        await asyncio.wait_for(collect(), timeout=3)
        eng.stop()
        return chunks

    chunks = asyncio.run(go())
    assert len(chunks) >= 8
    for c in chunks:
        assert len(c.pcm_s16le) == contracts.INPUT_CHUNK_BYTES
        assert c.source == "left"
    # sequence numbers are monotonic
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_capture_ring_overflow_counted(mock_streams):
    """Flooding the ring (no drain) bumps the overflow counter via the callback path."""
    eng = CaptureEngine(config.DeviceConfig())
    # Don't start the drain; just push directly past capacity through the callback.
    big = np.zeros((eng._native_rate + 5000) * 2, dtype=np.int16)
    eng._callback(big, big.size // 2, None, None)
    assert eng._rings["left"].overflow > 0
    assert eng._rings["right"].overflow > 0


def test_runtime_lifecycle_opens_and_closes_mock_streams(mock_streams):
    async def go():
        rt = PortAudioRuntime(config.DeviceConfig())
        with rt:
            assert rt._started
            # both an input and an output stream were constructed and started
            assert any(s.started for s in _FakeStream.instances)
        assert not rt._started
        assert all(s.closed for s in _FakeStream.instances)

    asyncio.run(go())


def test_runtime_enqueue_output_routes_to_channel(mock_streams):
    rt = PortAudioRuntime(config.DeviceConfig())
    pcm = np.full(2400, 9000, dtype=np.int16).tobytes()
    ok = rt.enqueue_output(
        OutputChannel.LEFT,
        OutputAudioChunk(pcm_s16le=pcm, direction=Direction.DE_TO_EN, seq=0, t_received_ns=0))
    assert ok is True
    assert rt.output.depth(OutputChannel.LEFT) > 0
    assert rt.output.depth(OutputChannel.RIGHT) == 0  # nothing leaked to the other direction
