# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

# The three model sizes Deck Options offers, stored in the profile under
# RWKV_MODEL_KEY:
#   - Big: "legacy", the show-time big model (SM91s2) alone for both
#     schedulers (the big model (1 leak-free epoch) until 2026-11-11; same
#     key and file, so the state rebuilds once).
#   - Small (the default): "sm72e25", the default small model (SM63), with the
#     show-time instant-only model, stage 2 (SM98) for RWKV-Instant.
#   - Tiny: "tiny", the same with the smaller instant-only model (SM101, one
#     card layer) in SM98's place. It only shrinks RWKV-Instant's state: the
#     curve still comes from the default small model (SM63).
# The values of Big and Small are the keys earlier builds stored, so existing
# profiles keep their choice; an earlier build reads "tiny" as unknown and
# falls back to Small.
RWKV_MODEL_BIG = "legacy"
RWKV_MODEL_SMALL = "sm72e25"
RWKV_MODEL_TINY = "tiny"
RWKV_MODEL_SIZES = (RWKV_MODEL_BIG, RWKV_MODEL_SMALL, RWKV_MODEL_TINY)
# Every profile value the runtime accepts. Besides the three sizes: the older
# small models (SM40, SM36, SM55, SM56, SM72 cool-downs from epoch 15 and 20)
# keep running until the next Deck Options save, which stores Small instead
# (ts/routes/deck-options/lib.ts), and the show-time retrain of the release
# (SM92) stays a preserved advanced value: Deck Options shows it only to a
# profile that already holds it, and keeps it on save.
RWKV_MODELS = (
    RWKV_MODEL_BIG,
    "sm40",
    "sm36",
    "sm55",
    "sm56",
    "sm72",
    "sm72e20",
    RWKV_MODEL_SMALL,
    "sm92",
    RWKV_MODEL_TINY,
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
# The smaller instant-only model (SM101): SM98 with one card layer instead of
# two (3,134 instead of 4,554 bytes of int8 state per card and note), trained
# the same way. Tiny's RWKV-Instant model, used exactly where Small uses SM98.
# Never a profile's model choice (the profile stores "tiny").
RWKV_TINY_INSTANT_MODEL = "sm101"
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_SPECIALIZED_INSTANT_MODEL,
    RWKV_TINY_INSTANT_MODEL,
)
# The profile choices with their own RWKV-Instant model, and the model that
# keeps RWKV-Curve for each. Every other choice is one model for both. Big
# has no instant model yet: the show-time instant-only big model (SM99) is
# planned as its partner, an entry here plus a big-engine pair in the runtime
# (RwkvRuntime::load_paired accepts only small models today).
RWKV_CHOICE_INSTANT_MODELS = {
    RWKV_MODEL_SMALL: RWKV_SPECIALIZED_INSTANT_MODEL,
    RWKV_MODEL_TINY: RWKV_TINY_INSTANT_MODEL,
}
RWKV_CHOICE_CURVE_MODELS = {RWKV_MODEL_TINY: RWKV_MODEL_SMALL}
# The show-time retrain of the release (SM92), a full model (curve, intervals,
# S90 and answer time). It stays bundled and selectable as a profile model,
# and runtime_models() picks it for Small and Tiny with the one-model opt-out
# when presets use both RWKV-Instant and RWKV-Curve.
RWKV_INSTANT_MODEL = "sm92"
RWKV_INSTANT_MODEL_RELEASES = tuple(RWKV_CHOICE_INSTANT_MODELS)
# Profile opt-out (default off): one model for both schedulers instead of the
# pair, about half the memory with slightly worse RWKV-Instant predictions
# (Small and Tiny then run the show-time retrain (SM92) alone).
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
    RWKV_TINY_INSTANT_MODEL: Path("sm101/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    """The profile's model choice (a value of RWKV_MODELS, not always a model
    key: "tiny" is resolved by runtime_models())."""
    # Profiles without an explicit choice, or with a value this build does
    # not know, use Small.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return (
        value if isinstance(value, str) and value in RWKV_MODELS else RWKV_MODEL_SMALL
    )


def curve_model_for_choice(model: str) -> str:
    """The one full model behind a profile choice: the model a custom
    checkpoint path replaces and the model that keeps RWKV-Curve."""
    return RWKV_CHOICE_CURVE_MODELS.get(model, model)


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
    instant-only `model` alone (SM98 or SM101) has no curve: `curve_model`
    is None. Both keys are part of the replay key (`instantModel` only for a
    pair), so a switch between a pair, the release alone, SM98 alone, SM101
    alone or the other size's pair rebuilds state once and never reuses
    another layout's state.
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

    Only Small (the release, sm72e25) and Tiny get a separate RWKV-Instant
    model: the show-time instant-only model, stage 2 (SM98,
    RWKV_SPECIALIZED_INSTANT_MODEL) for Small, the smaller instant-only
    model (SM101, RWKV_TINY_INSTANT_MODEL) for Tiny. Both keep the release
    for RWKV-Curve. Every other choice (Big, SM92, older small models) serves
    both schedulers alone. With Small or Tiny and RWKV-Instant in use:
      - RWKV-Instant without RWKV-Curve, with or without the `single_model`
        opt-out: the instant model alone, an instant-only runtime (one model
        state). It gives RWKV-Instant's recall and button probabilities
        only: no curve recall, intervals, S90 or answer time, and no RWKV
        workload simulation. Due dates and memory state come from the
        presets' FSRS fallback.
      - RWKV-Instant and RWKV-Curve, by default: the instant model beside the
        release (two model states). The instant model gives RWKV-Instant's
        recall and button probabilities; the release keeps the curve,
        intervals, S90 and the answer time.
      - RWKV-Instant and RWKV-Curve with the opt-out: the show-time retrain of
        the release (SM92, RWKV_INSTANT_MODEL) alone, a full model serving
        both schedulers (one model state). Against the release (SM63) on
        show-time inputs it ties on the curve and predicts RWKV-Instant
        better; it differs from the release, so the state is rebuilt once.
    RWKV-Curve only, or neither: the release alone (for Small and Tiny
    alike, so that switch keeps the same state).
    """
    curve_model = curve_model_for_choice(model)
    instant_model = RWKV_CHOICE_INSTANT_MODELS.get(model)
    if instant_model is None or not instant_used:
        return RwkvRuntimeModels(curve_model)
    if not curve_used:
        return RwkvRuntimeModels(instant_model)
    if not single_model:
        return RwkvRuntimeModels(curve_model, instant_model)
    return RwkvRuntimeModels(RWKV_INSTANT_MODEL)
