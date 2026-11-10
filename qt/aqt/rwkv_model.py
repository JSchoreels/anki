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
# so it never serves a curve: beside the release when presets also use
# RWKV-Curve, alone (instant-only runtime) when they use RWKV-Instant without
# RWKV-Curve, see runtime_models(). Never a profile's model choice.
RWKV_SPECIALIZED_INSTANT_MODEL = "sm98"
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (RWKV_INSTANT_ONLY_MODEL, RWKV_SPECIALIZED_INSTANT_MODEL)
# The show-time retrain of the release (SM92), a full model (curve, intervals,
# S90 and answer time). It stays bundled and selectable as a profile model,
# but is no longer picked automatically for RWKV-Instant (until 2026-11-10 it
# ran alone with the opt-out and no RWKV-Curve).
RWKV_INSTANT_MODEL = "sm92"
RWKV_INSTANT_MODEL_RELEASES = ("sm72e25",)
# Profile opt-out (default off): one model for both schedulers instead of the
# pair, about half the memory with slightly worse RWKV-Instant predictions.
# Only matters when presets use both RWKV-Instant and RWKV-Curve.
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
    # Off by default: with both schedulers, RWKV-Instant runs on its own
    # model beside the release.
    value = profile.get(RWKV_SINGLE_MODEL_KEY) if profile else None
    return value is True


class RwkvRuntimeModels(NamedTuple):
    """The models the native runtime loads for one profile.

    `model` is the runtime's main model. `instant_model`, when set, runs
    beside it and provides instant recall and button probabilities, while
    `model` keeps the curve, intervals, S90 and the answer-time head. An
    instant-only `model` alone (SM98) has no curve: `curve_model` is None.
    Both keys are part of the replay key (`instantModel` only for a pair), so
    a switch between a pair, the release alone and SM98 alone rebuilds state
    once and never reuses another layout's state.
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

    `curve_used` is true when any preset uses RWKV-Curve, as its scheduler or
    as RWKV-Instant's due-date fallback (both set `rwkv_review_enabled`).

    Only the release (sm72e25) gets a separate RWKV-Instant model; every
    other choice (SM92, Legacy, older small models) serves both schedulers
    alone. With the release and RWKV-Instant in use:
      - RWKV-Instant without RWKV-Curve, with or without the `single_model`
        opt-out: the show-time instant-only model, stage 2 (SM98,
        RWKV_SPECIALIZED_INSTANT_MODEL) alone, an instant-only runtime (one
        model state). It gives RWKV-Instant's recall and button
        probabilities only: no curve recall, intervals, S90 or answer time,
        and no RWKV workload simulation. Due dates and memory state come
        from the presets' FSRS fallback.
      - RWKV-Instant and RWKV-Curve, by default: SM98 beside the release (two
        model states). SM98 gives RWKV-Instant's recall and button
        probabilities; the release keeps the curve, intervals, S90 and the
        answer time.
      - RWKV-Instant and RWKV-Curve with the opt-out: the release alone.
    RWKV-Curve only, or neither: the release alone.
    """
    if model not in RWKV_INSTANT_MODEL_RELEASES or not instant_used:
        return RwkvRuntimeModels(model)
    if not curve_used:
        return RwkvRuntimeModels(RWKV_SPECIALIZED_INSTANT_MODEL)
    if not single_model:
        return RwkvRuntimeModels(model, RWKV_SPECIALIZED_INSTANT_MODEL)
    return RwkvRuntimeModels(model)
