# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping

# The stored card and note state (deck, preset and global stay f32):
#   - "compact_lowrank" (default): every card and note head stored as a
#     rank-2 product (rank 4 from the 16th answer) of factor vectors with
#     float16 scales and int8 shift vectors. The factor width follows the
#     model's size: int6 factors for Big and Small (the training repository's
#     w8+lr2.16.4q6x8, the 6-bit factors (SM162)), int8 factors for Tiny
#     (w8+lr2.16.4q8x8). About -85% (Big), -71% (Small) and -65% (Tiny) state
#     bytes against the int8 rows.
#   - "compact_cards_int4": the same notes, int4 card rows (w8+s4c8nhx8). It
#     stays finite on long cards but drifts from the float32 state there.
#   - "int8": int8 rows with f32 scales and float16 shifts; "f32": no rounding.
# The earlier default "cards_int4_notes_int8" (int4 cards with f32 row scales)
# overflowed on cards with thousands of answers; saved profiles holding it move
# to the default, and the new value changes the replay key, so the state cache
# rebuilds once. docs/rwkv-rc-state-format-2026-11-21.MD has the measurements.
# The release candidate's int8 rows ("compact_cards_int8", the default until
# the low-rank state) are no longer a profile value either: the Deck Options
# page saves the value it loaded, so profiles that saved it move to the
# default (one rebuild). The native code still reads and writes every layout.
# docs/rwkv-lowrank-state-2026-11-23.MD has the low-rank measurements.
# The low-rank state with int8 factors on every size ("compact_lowrank_int8",
# the default until the 6-bit factors) is no longer a profile value for the
# same reason: profiles that saved it move to "compact_lowrank" (one rebuild;
# Tiny's bytes stay the same). The native code still accepts
# "compact_lowrank_int8" and "compact_lowrank_int6" (int6 on every size).
RWKV_STATE_PRECISION_DEFAULT = "compact_lowrank"
RWKV_STATE_PRECISIONS = ("f32", "int8", "compact_lowrank", "compact_cards_int4")
RWKV_STATE_PRECISION_KEY = "rwkvStatePrecision"


def state_precision_for_profile(profile: Mapping[str, object] | None) -> str:
    value = profile.get(RWKV_STATE_PRECISION_KEY) if profile else None
    return (
        value
        if isinstance(value, str) and value in RWKV_STATE_PRECISIONS
        else RWKV_STATE_PRECISION_DEFAULT
    )
