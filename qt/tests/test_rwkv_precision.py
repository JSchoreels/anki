# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pytest

from aqt.rwkv_precision import state_precision_for_profile


@pytest.mark.parametrize(
    "profile, expected",
    [
        (None, "cards_int4_notes_int8"),
        ({}, "cards_int4_notes_int8"),
        ({"rwkvStatePrecision": "unknown"}, "cards_int4_notes_int8"),
        ({"rwkvStatePrecision": None}, "cards_int4_notes_int8"),
        ({"rwkvStatePrecision": 8}, "cards_int4_notes_int8"),
        ({"rwkvStatePrecision": "int8"}, "int8"),
        ({"rwkvStatePrecision": "f32"}, "f32"),
        # Full int4 was removed: stored profiles fall back to the default.
        ({"rwkvStatePrecision": "int4"}, "cards_int4_notes_int8"),
        ({"rwkvStatePrecision": "cards_int4_notes_int8"}, "cards_int4_notes_int8"),
    ],
)
def test_state_precision_for_profile_preserves_supported_choices(profile, expected):
    assert state_precision_for_profile(profile) == expected
