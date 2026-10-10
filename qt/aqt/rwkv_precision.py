# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping

RWKV_STATE_PRECISIONS = ("f32", "int8", "int4")
RWKV_STATE_PRECISION_KEY = "rwkvStatePrecision"


def state_precision_for_profile(profile: Mapping[str, object] | None) -> str:
    value = profile.get(RWKV_STATE_PRECISION_KEY) if profile else None
    return (
        value if isinstance(value, str) and value in RWKV_STATE_PRECISIONS else "int8"
    )
