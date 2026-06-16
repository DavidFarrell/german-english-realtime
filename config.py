"""Device + routing configuration. Small, shared, frozen alongside contracts.

Owns the mapping between physical reality and the abstract contracts:
  - which DJI channel is which speaker/direction
  - which output channel each direction goes to
  - default device names (resolved to indices at runtime by name + host API, never raw index;
    indices reshuffle when devices come and go - see notes/plan slice 1).
"""
from __future__ import annotations

from dataclasses import dataclass

from contracts import Direction, OutputChannel

# Verified on David's Mac 16 Jun 2026 (sounddevice enumeration):
#   "Wireless Mic Rx" = DJI, 2ch @ 48000 (TX1=ch0=LEFT, TX2=ch1=RIGHT)
#   "Bose QC35 II"    = 2ch @ 44100 A2DP output. NB a *separate* 1ch/16k Bose entry is the HFP
#                       mic - opening it collapses output to mono. Never open the Bose input.
DEFAULT_INPUT_NAME = "Wireless Mic Rx"
DEFAULT_OUTPUT_NAME = "Bose QC35 II"

DJI_NATIVE_RATE = 48_000      # confirm at runtime; resample 48k -> 16k for the model
# Bose A2DP native rate (resample 24k model output -> device rate). Confirmed 44100.
DEFAULT_OUTPUT_RATE = 44_100

# Physical wiring (the DJI stereo split gives hardware channel isolation per speaker).
#   LEFT mic channel  (ch0 / TX1) = the German speaker  -> translate to EN
#   RIGHT mic channel (ch1 / TX2) = the English speaker -> translate to DE
SOURCE_FOR_DIRECTION: dict[Direction, str] = {
    Direction.DE_TO_EN: "left",
    Direction.EN_TO_DE: "right",
}

# Where each translated stream is played. German->English is for the English listener's bud,
# English->German for the German listener's bud. With the rig (mics inside the earcups) this
# also defines which earcup couples to which mic.
OUTPUT_FOR_DIRECTION: dict[Direction, OutputChannel] = {
    Direction.DE_TO_EN: OutputChannel.LEFT,
    Direction.EN_TO_DE: OutputChannel.RIGHT,
}


@dataclass(frozen=True)
class DeviceConfig:
    input_name: str = DEFAULT_INPUT_NAME
    output_name: str = DEFAULT_OUTPUT_NAME
    input_native_rate: int = DJI_NATIVE_RATE
    output_native_rate: int = DEFAULT_OUTPUT_RATE
    # Per-person display names (optional; UI may override). Left = German, Right = English.
    left_name: str = "Left (German)"
    right_name: str = "Right (English)"
