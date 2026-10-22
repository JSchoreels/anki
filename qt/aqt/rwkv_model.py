# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path

RWKV_MODELS = ("legacy", "sm40", "sm36", "sm55")
RWKV_MODEL_KEY = "rwkvModel"
RWKV_MODEL_FILES = {
    "legacy": Path("RWKV_trained_on_5000_10000.bin"),
    "sm40": Path("sm40v2/weights.f32.bin"),
    "sm36": Path("sm36/weights.f32.bin"),
    "sm55": Path("sm55/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    # Profiles without an explicit choice use the bundled Muon model.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return value if isinstance(value, str) and value in RWKV_MODELS else "sm55"
