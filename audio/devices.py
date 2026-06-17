"""Device resolution by name + host API (never raw index; indices reshuffle).

Pure-ish: resolve_device queries sounddevice but does no streaming. The HFP guard refuses to
return the Bose as an input - opening its 1ch/16k mic entry collapses output to mono.
"""
from __future__ import annotations

from typing import Literal

import sounddevice as sd


class DeviceError(RuntimeError):
    """No matching device, too few channels, or a forbidden (HFP) input."""


def _channels(dev: dict, kind: Literal["input", "output"]) -> int:
    return int(dev["max_input_channels"] if kind == "input" else dev["max_output_channels"])


def resolve_device(name: str, kind: Literal["input", "output"], min_channels: int = 2) -> int:
    """Return the PortAudio device index whose name contains `name` and supports `min_channels`
    of the given kind. Match by name substring across all host APIs; refuse the Bose as input.
    """
    if kind == "input" and "bose" in name.lower():
        raise DeviceError(
            f"refusing to open {name!r} as an input: the Bose HFP mic collapses output to mono")

    needle = name.lower()
    candidates: list[tuple[int, dict]] = []
    for idx, dev in enumerate(sd.query_devices()):
        if needle in str(dev["name"]).lower() and _channels(dev, kind) >= 1:
            candidates.append((idx, dev))

    if not candidates:
        raise DeviceError(f"no {kind} device matching {name!r} found")

    # Prefer a candidate that actually has enough channels of this kind.
    good = [(idx, dev) for idx, dev in candidates if _channels(dev, kind) >= min_channels]
    if not good:
        idx, dev = candidates[0]
        raise DeviceError(
            f"{kind} device {dev['name']!r} has {_channels(dev, kind)} {kind} channel(s), "
            f"need >= {min_channels}")
    return good[0][0]


def list_devices() -> str:
    """A plain table (idx | in/out ch | default_sr | name) for the CLI."""
    rows = ["idx |  in | out |   default_sr | name", "----+-----+-----+--------------+" + "-" * 24]
    for idx, dev in enumerate(sd.query_devices()):
        rows.append(
            f"{idx:>3} | {int(dev['max_input_channels']):>3} | "
            f"{int(dev['max_output_channels']):>3} | "
            f"{float(dev['default_samplerate']):>10.0f} Hz | {dev['name']}")
    return "\n".join(rows)


def list_devices_structured(kind: Literal["input", "output"] | None = None) -> list[dict]:
    """Structured device list for GUI pickers: [{index, name, inCh, outCh, rate}].

    With `kind`, only devices that can do that kind (>= 1 channel) are returned. The Bose HFP mic
    entry is still listed (the picker may show it) - the HFP guard only bites at resolve time.
    """
    out: list[dict] = []
    for idx, dev in enumerate(sd.query_devices()):
        in_ch = int(dev["max_input_channels"])
        out_ch = int(dev["max_output_channels"])
        if kind == "input" and in_ch < 1:
            continue
        if kind == "output" and out_ch < 1:
            continue
        out.append({
            "index": idx, "name": str(dev["name"]),
            "inCh": in_ch, "outCh": out_ch,
            "rate": int(float(dev["default_samplerate"])),
        })
    return out


def find(name: str, kind: Literal["input", "output"], min_channels: int = 2) -> int | None:
    """Non-raising presence check: return a usable device index for `name`/`kind`, or None.

    A thin wrapper over resolve_device that swallows DeviceError so the GUI can poll for hardware
    (the device-not-found screen + Rescan) without exceptions.
    """
    try:
        return resolve_device(name, kind, min_channels=min_channels)
    except DeviceError:
        return None
