# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path

RWKV_MODELS = ("legacy", "sm40")
RWKV_MODEL_KEY = "rwkvModel"
RWKV_MODEL_FILES = {
    "legacy": Path("RWKV_trained_on_5000_10000.bin"),
    "sm40": Path("sm40v2/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    # Missing keys belong to profiles created before model selection existed.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return value if isinstance(value, str) and value in RWKV_MODELS else "legacy"
