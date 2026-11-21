# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping

# The stored card and note state (deck, preset and global stay f32):
#   - "compact_cards_int8" (default): int8 card and note rows with float16 row
#     scales and int8 shift vectors (the training repository's w8+s8hx8 card
#     layout and hx8 notes).
#   - "compact_cards_int4": the same notes, int4 card rows (w8+s4c8nhx8). It
#     stays finite on long cards but drifts from the float32 state there.
#   - "int8": int8 rows with f32 scales and float16 shifts; "f32": no rounding.
# The earlier default "cards_int4_notes_int8" (int4 cards with f32 row scales)
# overflowed on cards with thousands of answers; saved profiles holding it move
# to the default, and the new value changes the replay key, so the state cache
# rebuilds once. docs/rwkv-rc-state-format-2026-11-21.MD has the measurements.
RWKV_STATE_PRECISION_DEFAULT = "compact_cards_int8"
RWKV_STATE_PRECISIONS = ("f32", "int8", "compact_cards_int8", "compact_cards_int4")
RWKV_STATE_PRECISION_KEY = "rwkvStatePrecision"


def state_precision_for_profile(profile: Mapping[str, object] | None) -> str:
    value = profile.get(RWKV_STATE_PRECISION_KEY) if profile else None
    return (
        value
        if isinstance(value, str) and value in RWKV_STATE_PRECISIONS
        else RWKV_STATE_PRECISION_DEFAULT
    )
