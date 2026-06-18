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

# Verified on David's Mac 16-18 Jun 2026 (sounddevice enumeration):
#   "Wireless Mic Rx" = DJI, 2ch @ 48000 (TX1=ch0=LEFT, TX2=ch1=RIGHT)
#   "David's Eggies"  = BT earbuds, A2DP stereo output (out 2). NB a *separate* 1ch entry is the
#                       HFP mic - opening it (or making the buds the default INPUT) collapses
#                       output to mono. Never open the earbud input; keep the DJI as the mic.
# Matched as a substring (case-insensitive), so "Eggies" avoids the curly-apostrophe in the name.
DEFAULT_INPUT_NAME = "Wireless Mic Rx"
DEFAULT_OUTPUT_NAME = "Eggies"

DJI_NATIVE_RATE = 48_000      # confirm at runtime; resample 48k -> 16k for the model
# Fallback output rate; the real device rate is detected at runtime (output resamples 24k -> device).
DEFAULT_OUTPUT_RATE = 48_000

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
