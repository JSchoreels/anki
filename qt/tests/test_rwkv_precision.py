# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pytest

from aqt.rwkv_precision import state_precision_for_profile


@pytest.mark.parametrize(
    "profile, expected",
    [
        (None, "compact_lowrank"),
        ({}, "compact_lowrank"),
        ({"rwkvStatePrecision": "unknown"}, "compact_lowrank"),
        ({"rwkvStatePrecision": None}, "compact_lowrank"),
        ({"rwkvStatePrecision": 8}, "compact_lowrank"),
        ({"rwkvStatePrecision": "int8"}, "int8"),
        ({"rwkvStatePrecision": "f32"}, "f32"),
        ({"rwkvStatePrecision": "compact_lowrank"}, "compact_lowrank"),
        # The int8-factor low-rank default of the release candidate: the
        # Deck Options page saved the loaded value, so these profiles move to
        # the size-dependent default (int6 factors for Big and Small).
        ({"rwkvStatePrecision": "compact_lowrank_int8"}, "compact_lowrank"),
        ({"rwkvStatePrecision": "compact_lowrank_int6"}, "compact_lowrank"),
        # The release candidate's int8 rows: the Deck Options page saved the
        # loaded value, so these profiles move to the low-rank default.
        ({"rwkvStatePrecision": "compact_cards_int8"}, "compact_lowrank"),
        ({"rwkvStatePrecision": "compact_cards_int4"}, "compact_cards_int4"),
        # Full int4 was removed: stored profiles fall back to the default.
        ({"rwkvStatePrecision": "int4"}, "compact_lowrank"),
        # The former default overflowed on long cards: profiles move to the
        # new default, which also changes the replay key (one cache rebuild).
        ({"rwkvStatePrecision": "cards_int4_notes_int8"}, "compact_lowrank"),
    ],
)
def test_state_precision_for_profile_preserves_supported_choices(profile, expected):
    assert state_precision_for_profile(profile) == expected
