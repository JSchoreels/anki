# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

RWKV_SUBMODULES = ["card_id", "note_id", "deck_id", "preset_id", "user_id"]

ID_ENCODE_DIMS = {
    "card_id": 12,
    "note_id": 12,
    "deck_id": 8,
    "preset_id": 8,
}
ID_SPLIT = 4
DAY_OFFSET_ENCODE_PERIODS = [3, 7, 30, 100, 365, 3650, 36500]
