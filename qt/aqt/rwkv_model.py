# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

# The three model sizes Deck Options offers, stored in the profile under
# RWKV_MODEL_KEY:
#   - Big: "legacy", the show-time big model (SM91s2) (the big model (1
#     leak-free epoch) until 2026-11-11; same key and file, so the state
#     rebuilds once), with the show-time instant-only big model (SM99) for
#     RWKV-Instant since 2026-11-12, the way Small pairs its models.
#   - Small (the default): "sm72e25", the default small model (SM63), with the
#     show-time instant-only model, stage 2 (SM98) for RWKV-Instant.
#   - Tiny: "tiny", the same with the smaller instant-only model (SM101, one
#     card layer) in SM98's place; the pair's curve still comes from the
#     default small model (SM63), which is more accurate on the curve than the
#     Tiny two-head model (SM106). With the one-model opt-out Tiny runs SM106
#     alone (since 2026-11-13, SM112).
# RWKV-Instant without RWKV-Curve runs SM101 alone for Small and Tiny alike
# (since 2026-11-13, SM112; SM98 before for Small) and SM99 alone for Big:
# see runtime_models().
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
# so it never serves a curve: Small's RWKV-Instant model beside the release
# when presets also use RWKV-Curve (alone without RWKV-Curve until 2026-11-13,
# now RWKV_INSTANT_ALONE_MODEL), see runtime_models(). Never a profile's model
# choice.
RWKV_SPECIALIZED_INSTANT_MODEL = "sm98"
# The smaller instant-only model (SM101, the Tiny Instant-Only model): SM98
# with one card layer instead of two (3,134 instead of 4,554 bytes of int8
# state per card and note), trained the same way. Tiny's RWKV-Instant model in
# the pair, and RWKV-Instant's model alone for Small and Tiny. Never a
# profile's model choice (the profile stores "tiny").
RWKV_TINY_INSTANT_MODEL = "sm101"
# The Tiny two-head model (SM106): the show-time retrain (SM92) cut to one
# card layer and trained on with both heads on show-time inputs, a full model
# (curve, intervals, S90, instant recall and buttons) without an answer-time
# head. Tiny's one model under the opt-out. Never a profile's model choice.
RWKV_TINY_SINGLE_MODEL = "sm106"
# The show-time instant-only big model (SM99): the show-time big model
# (SM91s2) trained with the curve loss off on show-time inputs, on the big
# engine. Big's RWKV-Instant model, used exactly where Small uses SM98. Never
# a profile's model choice (the profile stores "legacy").
RWKV_BIG_INSTANT_MODEL = "sm99"
# The model RWKV-Instant runs alone (no preset uses RWKV-Curve) for Small and
# Tiny alike, whatever the size setting: the Tiny Instant-Only model (SM101).
# The user's decision of 2026-11-12: "if the user only wants instant
# predictions, we can use Tiny directly". Small ran SM98 here until then;
# SM101 keeps the smallest state and is the fastest.
RWKV_INSTANT_ALONE_MODEL = RWKV_TINY_INSTANT_MODEL
# The choices whose RWKV-Instant alone keeps a model of their own: Big, an
# explicit choice, keeps the show-time instant-only big model (SM99).
RWKV_CHOICE_INSTANT_ALONE_MODELS = {RWKV_MODEL_BIG: RWKV_BIG_INSTANT_MODEL}
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_SPECIALIZED_INSTANT_MODEL,
    RWKV_TINY_INSTANT_MODEL,
    RWKV_BIG_INSTANT_MODEL,
)
# The profile choices with their own RWKV-Instant model, and the model that
# keeps RWKV-Curve for each. Every other choice is one model for both.
RWKV_CHOICE_INSTANT_MODELS = {
    RWKV_MODEL_BIG: RWKV_BIG_INSTANT_MODEL,
    RWKV_MODEL_SMALL: RWKV_SPECIALIZED_INSTANT_MODEL,
    RWKV_MODEL_TINY: RWKV_TINY_INSTANT_MODEL,
}
RWKV_CHOICE_CURVE_MODELS = {RWKV_MODEL_TINY: RWKV_MODEL_SMALL}
# The show-time retrain of the release (SM92), a full model (curve, intervals,
# S90 and answer time). It stays bundled and selectable as a profile model,
# and runtime_models() picks it for Small with the one-model opt-out when
# presets use both RWKV-Instant and RWKV-Curve (Tiny until 2026-11-13).
RWKV_INSTANT_MODEL = "sm92"
RWKV_INSTANT_MODEL_RELEASES = tuple(RWKV_CHOICE_INSTANT_MODELS)
# The one model each choice with an RWKV-Instant model runs alone under the
# opt-out with both schedulers: Big its show-time big model (SM91s2), which
# is already a show-time model; Small the show-time retrain (SM92); Tiny the
# Tiny two-head model (SM106).
RWKV_CHOICE_SINGLE_MODELS = {
    RWKV_MODEL_BIG: RWKV_MODEL_BIG,
    RWKV_MODEL_TINY: RWKV_TINY_SINGLE_MODEL,
}
# Profile opt-out (default off): one model for both schedulers instead of the
# pair, about half the memory with slightly worse RWKV-Instant predictions
# (Big then runs the show-time big model (SM91s2) alone, Small the show-time
# retrain (SM92) alone, Tiny the Tiny two-head model (SM106) alone).
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
    RWKV_BIG_INSTANT_MODEL: Path("sm99/weights.f32.bin"),
    RWKV_TINY_SINGLE_MODEL: Path("sm106/weights.f32.bin"),
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
    instant-only `model` alone (SM101 or SM99) has no curve:
    `curve_model` is None. Both keys are part of the replay key
    (`instantModel` only for a pair), so a switch between a pair, the release
    alone, an instant model alone or another size's pair rebuilds state once
    and never reuses another layout's state.
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

    The three sizes get a separate RWKV-Instant model in the pair: the
    show-time instant-only big model (SM99, RWKV_BIG_INSTANT_MODEL) for Big,
    the show-time instant-only model, stage 2 (SM98,
    RWKV_SPECIALIZED_INSTANT_MODEL) for Small, the Tiny Instant-Only model
    (SM101, RWKV_TINY_INSTANT_MODEL) for Tiny. Big keeps the show-time big
    model (SM91s2) for RWKV-Curve, Small and Tiny the release. Every other
    choice (SM92, older small models) serves both schedulers alone.
    With a size and RWKV-Instant in use:
      - RWKV-Instant without RWKV-Curve, with or without the `single_model`
        opt-out: an instant-only model alone, an instant-only runtime (one
        model state): the Tiny Instant-Only model (SM101,
        RWKV_INSTANT_ALONE_MODEL) for Small and Tiny alike, SM99 for Big
        (RWKV_CHOICE_INSTANT_ALONE_MODELS). It gives RWKV-Instant's recall and
        button probabilities only: no curve recall, intervals, S90 or answer
        time, and no RWKV workload simulation. Due dates and memory state
        come from the presets' FSRS fallback.
      - RWKV-Instant and RWKV-Curve, by default: the instant model beside the
        release (two model states). The instant model gives RWKV-Instant's
        recall and button probabilities; the release keeps the curve,
        intervals, S90 and the answer time.
      - RWKV-Instant and RWKV-Curve with the opt-out: one full model serving
        both schedulers (one model state, RWKV_CHOICE_SINGLE_MODELS). Big:
        its show-time big model (SM91s2) alone, the pair's curve model.
        Small: the show-time retrain of the release (SM92,
        RWKV_INSTANT_MODEL); against the release (SM63) on show-time inputs
        it ties on the curve and predicts RWKV-Instant better. Tiny: the Tiny
        two-head model (SM106, RWKV_TINY_SINGLE_MODEL), a one-card-layer
        model with both heads (no answer-time head). Either differs from the
        release, so the state is rebuilt once.
    RWKV-Curve only, or neither: the curve model alone (for Small and Tiny
    alike, so that switch keeps the same state; for Big the same model as
    the opt-out).
    """
    curve_model = curve_model_for_choice(model)
    instant_model = RWKV_CHOICE_INSTANT_MODELS.get(model)
    if instant_model is None or not instant_used:
        return RwkvRuntimeModels(curve_model)
    if not curve_used:
        return RwkvRuntimeModels(
            RWKV_CHOICE_INSTANT_ALONE_MODELS.get(model, RWKV_INSTANT_ALONE_MODEL)
        )
    if not single_model:
        return RwkvRuntimeModels(curve_model, instant_model)
    return RwkvRuntimeModels(RWKV_CHOICE_SINGLE_MODELS.get(model, RWKV_INSTANT_MODEL))
