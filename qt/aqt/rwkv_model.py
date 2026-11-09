# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

RWKV_MODELS = (
    "legacy",
    "sm40",
    "sm36",
    "sm55",
    "sm56",
    "sm72",
    "sm72e20",
    "sm72e25",
    "sm92",
)
RWKV_MODEL_KEY = "rwkvModel"
# The instant-only small model (SM57): trained from the release (sm72e25) with
# the curve loss off, on normal inputs. It is never a profile's model choice
# and no longer used by default; it stays bundled so RWKV_INSTANT_MODEL can
# switch back to it in one line.
RWKV_INSTANT_ONLY_MODEL = "sm57"
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (RWKV_INSTANT_ONLY_MODEL,)
# The model that serves RWKV-Instant with the release, see runtime_models():
# the show-time retrain of the release (SM92). RWKV-Instant queries the model
# at show time, the inputs SM92 was trained on.
RWKV_INSTANT_MODEL = "sm92"
RWKV_INSTANT_MODEL_RELEASES = ("sm72e25",)
RWKV_SPECIALIZED_INSTANT_MODEL_KEY = "rwkvSpecializedInstantModel"
RWKV_MODEL_FILES = {
    "legacy": Path("RWKV_trained_on_5000_10000.bin"),
    "sm40": Path("sm40v2/weights.f32.bin"),
    "sm36": Path("sm36/weights.f32.bin"),
    "sm55": Path("sm55/weights.f32.bin"),
    "sm56": Path("sm56/weights.f32.bin"),
    "sm72": Path("sm72/weights.f32.bin"),
    "sm72e20": Path("sm72e20/weights.f32.bin"),
    "sm72e25": Path("sm72e25/weights.f32.bin"),
    "sm92": Path("sm92/weights.f32.bin"),
    RWKV_INSTANT_ONLY_MODEL: Path("sm57/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    # Profiles without an explicit choice use the leak-free small model.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return value if isinstance(value, str) and value in RWKV_MODELS else "sm72e25"


def specialized_instant_model_for_profile(profile: Mapping[str, object] | None) -> bool:
    # Off by default: with both schedulers, one shared model state.
    value = profile.get(RWKV_SPECIALIZED_INSTANT_MODEL_KEY) if profile else None
    return value is True


class RwkvRuntimeModels(NamedTuple):
    """The models the native runtime loads for one profile.

    `model` is the runtime's main model. `instant_model`, when set, runs
    beside it and provides instant recall and button probabilities, while
    `model` keeps the curve, intervals, S90 and the answer-time head. Either
    model's key is part of the replay key, so a change rebuilds state.
    """

    model: str
    instant_model: str | None = None

    @property
    def curve_model(self) -> str | None:
        """The model whose state serves RWKV-Curve, or None without a curve."""
        return None if self.model in RWKV_INSTANT_ONLY_MODELS else self.model

    @property
    def instant_scheduler_model(self) -> str:
        """The model whose state serves RWKV-Instant."""
        return self.instant_model or self.model


def runtime_models(
    model: str,
    *,
    instant_used: bool,
    curve_used: bool,
    specialized_instant_model: bool,
) -> RwkvRuntimeModels:
    """Pick the runtime models for a profile model and the schedulers in use.

    Only the release (sm72e25) gets a separate RWKV-Instant model
    (RWKV_INSTANT_MODEL); every other choice (SM92, Legacy, older small
    models) serves both schedulers alone. With the release:
      - RWKV-Instant and no RWKV-Curve: RWKV_INSTANT_MODEL alone. The
        show-time retrain (SM92) runs exactly as for a profile that picks
        `sm92`; the instant-only model (SM57) would give no curve output, no
        intervals, no S90 and no answer time.
      - both: the release alone by default; with the profile toggle,
        RWKV_INSTANT_MODEL runs beside it (two model states).
      - RWKV-Curve only, or neither: the release alone.
    """
    if model not in RWKV_INSTANT_MODEL_RELEASES or not instant_used:
        return RwkvRuntimeModels(model)
    if not curve_used:
        return RwkvRuntimeModels(RWKV_INSTANT_MODEL)
    if specialized_instant_model:
        return RwkvRuntimeModels(model, RWKV_INSTANT_MODEL)
    return RwkvRuntimeModels(model)
