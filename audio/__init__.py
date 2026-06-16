"""Audio I/O package (PortAudio capture + stereo output).

Public API: the PortAudioRuntime (an AudioRuntime), device resolution, and the pure DSP
primitives that are unit-tested without hardware.
"""
from audio.capture import (
    Chunker,
    RingBuffer,
    StreamingResampler,
    deinterleave,
)
from audio.devices import DeviceError, list_devices, resolve_device
from audio.output import ChannelJitterBuffer, OutputEngine
from audio.runtime import PortAudioRuntime

__all__ = [
    "PortAudioRuntime",
    "resolve_device",
    "list_devices",
    "DeviceError",
    "deinterleave",
    "RingBuffer",
    "StreamingResampler",
    "Chunker",
    "ChannelJitterBuffer",
    "OutputEngine",
]
