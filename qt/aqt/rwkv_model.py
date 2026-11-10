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
# and no longer used by default; it stays bundled so a constant below can
# switch back to it in one line.
RWKV_INSTANT_ONLY_MODEL = "sm57"
# The show-time instant-only model, stage 2 (SM98): trained from the release
# with the curve loss off, on show-time inputs. Its curve head is untrained,
# so it never runs alone: by default it runs beside the release for
# RWKV-Instant, see runtime_models(). Never a profile's model choice.
RWKV_SPECIALIZED_INSTANT_MODEL = "sm98"
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (RWKV_INSTANT_ONLY_MODEL, RWKV_SPECIALIZED_INSTANT_MODEL)
# The model that serves RWKV-Instant alone with the release when RWKV-Curve is
# not used and the profile opts out of the pair: the show-time retrain of the
# release (SM92), a full model (curve, intervals, S90 and answer time).
RWKV_INSTANT_MODEL = "sm92"
RWKV_INSTANT_MODEL_RELEASES = ("sm72e25",)
# Profile opt-out (default off): one model for both schedulers instead of the
# pair, about half the memory with slightly worse RWKV-Instant predictions.
RWKV_SINGLE_MODEL_KEY = "rwkvSingleModel"
# The former opt-in toggle (until 2026-11-10), whose values meant the
# opposite; ignored, and dropped from the profile on the next save.
RWKV_OLD_SPECIALIZED_INSTANT_MODEL_KEY = "rwkvSpecializedInstantModel"
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
    RWKV_SPECIALIZED_INSTANT_MODEL: Path("sm98/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    # Profiles without an explicit choice use the leak-free small model.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return value if isinstance(value, str) and value in RWKV_MODELS else "sm72e25"


def single_model_for_profile(profile: Mapping[str, object] | None) -> bool:
    # Off by default: RWKV-Instant runs on its own model beside the release.
    value = profile.get(RWKV_SINGLE_MODEL_KEY) if profile else None
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
    single_model: bool,
) -> RwkvRuntimeModels:
    """Pick the runtime models for a profile model and the schedulers in use.

    Only the release (sm72e25) gets a separate RWKV-Instant model; every
    other choice (SM92, Legacy, older small models) serves both schedulers
    alone. With the release and RWKV-Instant in use:
      - by default, with or without RWKV-Curve: the show-time instant-only
        model (SM98, RWKV_SPECIALIZED_INSTANT_MODEL) beside the release (two
        model states). SM98 gives RWKV-Instant's recall and button
        probabilities; the release keeps the curve, intervals, S90 and the
        answer time. SM98's curve head is untrained, so it never runs alone.
      - with the `single_model` opt-out and no RWKV-Curve: the show-time
        retrain (SM92, RWKV_INSTANT_MODEL) alone, exactly as for a profile
        that picks `sm92`.
      - with the opt-out and both: the release alone.
    RWKV-Curve only, or neither: the release alone.
    """
    if model not in RWKV_INSTANT_MODEL_RELEASES or not instant_used:
        return RwkvRuntimeModels(model)
    if not single_model:
        return RwkvRuntimeModels(model, RWKV_SPECIALIZED_INSTANT_MODEL)
    if not curve_used:
        return RwkvRuntimeModels(RWKV_INSTANT_MODEL)
    return RwkvRuntimeModels(model)
