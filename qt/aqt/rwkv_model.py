# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

# The three model sizes Deck Options offers, stored in the profile under
# RWKV_MODEL_KEY. Each size runs only its own models, those of the RWKV family
# release (anki-rwkv rwkv-family-2026-11-13), RWKV_SIZE_MODELS: a two-head
# model (curve, intervals, S90, instant recall and buttons) and an
# Instant-Only model (instant recall and buttons only):
#   - Big: "legacy", the show-time big model (SM91s2) with the show-time
#     instant-only big model (SM99).
#   - Small (the default): "sm72e25", the show-time retrain (SM92) with the
#     Small Instant-Only model (SM98). Until 2026-11-13 Small ran the default
#     small model (SM63) for RWKV-Curve and the Tiny Instant-Only model
#     (SM101) for RWKV-Instant alone.
#   - Tiny: "tiny", the Tiny two-head model (SM106) with the Tiny Instant-Only
#     model (SM101). Until 2026-11-13 Tiny ran the default small model (SM63)
#     for RWKV-Curve.
# The schedulers the presets use and the one-model opt-out choose between the
# size's two models (see runtime_models()); a size never runs another size's
# model (the family wiring (SM114)).
# The values of Big and Small are the keys earlier builds stored, so existing
# profiles keep their choice; "sm72e25" is also the default small model
# (SM63)'s model key, but as a profile value it means Small. An earlier build
# reads "tiny" as unknown and falls back to Small.
RWKV_MODEL_BIG = "legacy"
RWKV_MODEL_SMALL = "sm72e25"
RWKV_MODEL_TINY = "tiny"
RWKV_MODEL_SIZES = (RWKV_MODEL_BIG, RWKV_MODEL_SMALL, RWKV_MODEL_TINY)
# The default small model (SM63) as an explicit profile value: it runs SM63
# alone (model key "sm72e25", see RWKV_CHOICE_MODEL_ALIASES), like the older
# small models below. No size picks SM63 since the family wiring (SM114); it
# stays bundled.
RWKV_MODEL_SM63 = "sm63"
# Every profile value the runtime accepts. Besides the three sizes: the older
# small models (SM40, SM36, SM55, SM56, SM72 cool-downs from epoch 15 and 20,
# and the default small model (SM63)) keep running alone until the next Deck
# Options save, which stores Small instead (ts/routes/deck-options/lib.ts),
# and the show-time retrain (SM92) stays a preserved advanced value: Deck
# Options shows it only to a profile that already holds it, and keeps it on
# save; it runs SM92 alone for both schedulers.
RWKV_MODELS = (
    RWKV_MODEL_BIG,
    "sm40",
    "sm36",
    "sm55",
    "sm56",
    "sm72",
    "sm72e20",
    RWKV_MODEL_SM63,
    RWKV_MODEL_SMALL,
    "sm92",
    RWKV_MODEL_TINY,
)
RWKV_MODEL_KEY = "rwkvModel"
# Profile values whose model key differs from the value.
RWKV_CHOICE_MODEL_ALIASES = {RWKV_MODEL_SM63: "sm72e25"}
# The instant-only small model (SM57): trained from the release (sm72e25) with
# the curve loss off, on normal inputs. Never a profile's model choice and no
# longer used; it stays bundled.
RWKV_INSTANT_ONLY_MODEL = "sm57"
# The show-time retrain (SM92): the default small model (SM63) retrained on
# show-time inputs, a full model (curve, intervals, S90, instant recall and
# buttons, answer time). Small's two-head model, and a preserved profile
# value of its own.
RWKV_SMALL_TWO_HEAD_MODEL = "sm92"
# The Small Instant-Only model (SM98, the show-time instant-only model, stage
# 2): trained from the release with the curve loss off, on show-time inputs.
# Its curve head is untrained, so it never serves a curve. Small's
# Instant-Only model. Never a profile's model choice.
RWKV_SMALL_INSTANT_MODEL = "sm98"
# The Tiny two-head model (SM106): the show-time retrain (SM92) cut to one
# card layer and trained on with both heads on show-time inputs, a full model
# (curve, intervals, S90, instant recall and buttons) without an answer-time
# head. Tiny's two-head model. Never a profile's model choice.
RWKV_TINY_TWO_HEAD_MODEL = "sm106"
# The Tiny Instant-Only model (SM101): SM98 with one card layer instead of two
# (3,134 instead of 4,554 bytes of int8 state per card and note), trained the
# same way. Tiny's Instant-Only model. Never a profile's model choice (the
# profile stores "tiny").
RWKV_TINY_INSTANT_MODEL = "sm101"
# The show-time instant-only big model (SM99): the show-time big model
# (SM91s2) trained with the curve loss off on show-time inputs, on the big
# engine. Big's Instant-Only model. Never a profile's model choice (the
# profile stores "legacy", which is also the show-time big model (SM91s2)'s
# model key).
RWKV_BIG_INSTANT_MODEL = "sm99"
# Models whose curve head is untrained: they serve RWKV-Instant only.
RWKV_INSTANT_ONLY_MODELS = (
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_SMALL_INSTANT_MODEL,
    RWKV_TINY_INSTANT_MODEL,
    RWKV_BIG_INSTANT_MODEL,
)


class RwkvSizeModels(NamedTuple):
    """The two models of one size of the RWKV family release."""

    two_head: str
    instant: str


# Each size's models, exactly the family release's: nothing crosses sizes.
RWKV_SIZE_MODELS = {
    RWKV_MODEL_BIG: RwkvSizeModels(RWKV_MODEL_BIG, RWKV_BIG_INSTANT_MODEL),
    RWKV_MODEL_SMALL: RwkvSizeModels(
        RWKV_SMALL_TWO_HEAD_MODEL, RWKV_SMALL_INSTANT_MODEL
    ),
    RWKV_MODEL_TINY: RwkvSizeModels(RWKV_TINY_TWO_HEAD_MODEL, RWKV_TINY_INSTANT_MODEL),
}
# Profile opt-out (default off): with both schedulers, the size's two-head
# model alone for both instead of the pair, about half the memory with
# slightly worse RWKV-Instant predictions. Only matters when presets use both
# RWKV-Instant and RWKV-Curve.
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
    RWKV_SMALL_TWO_HEAD_MODEL: Path("sm92/weights.f32.bin"),
    RWKV_INSTANT_ONLY_MODEL: Path("sm57/weights.f32.bin"),
    RWKV_SMALL_INSTANT_MODEL: Path("sm98/weights.f32.bin"),
    RWKV_TINY_INSTANT_MODEL: Path("sm101/weights.f32.bin"),
    RWKV_BIG_INSTANT_MODEL: Path("sm99/weights.f32.bin"),
    RWKV_TINY_TWO_HEAD_MODEL: Path("sm106/weights.f32.bin"),
}


def model_for_profile(profile: Mapping[str, object] | None) -> str:
    """The profile's model choice (a value of RWKV_MODELS, not always a model
    key: the sizes and "sm63" are resolved by runtime_models())."""
    # Profiles without an explicit choice, or with a value this build does
    # not know, use Small.
    value = profile.get(RWKV_MODEL_KEY) if profile else None
    return (
        value if isinstance(value, str) and value in RWKV_MODELS else RWKV_MODEL_SMALL
    )


def curve_model_for_choice(model: str) -> str:
    """The one full model behind a profile choice: a size's two-head model,
    or the model the choice names. It keeps RWKV-Curve, and a custom
    checkpoint path replaces it."""
    size_models = RWKV_SIZE_MODELS.get(model)
    if size_models is not None:
        return size_models.two_head
    return RWKV_CHOICE_MODEL_ALIASES.get(model, model)


def single_model_for_profile(profile: Mapping[str, object] | None) -> bool:
    # Off by default: with both schedulers, RWKV-Instant runs on the size's
    # Instant-Only model beside its two-head model.
    value = profile.get(RWKV_SINGLE_MODEL_KEY) if profile else None
    return value is True


class RwkvRuntimeModels(NamedTuple):
    """The models the native runtime loads for one profile.

    `model` is the runtime's main model. `instant_model`, when set, runs
    beside it and provides instant recall and button probabilities, while
    `model` keeps the curve, intervals, S90 and the answer-time head (when it
    has one: the Tiny two-head model (SM106) and the show-time big model
    (SM91s2) have none). An instant-only `model` alone (SM98, SM101 or SM99)
    has no curve: `curve_model` is None. Both keys are part of the replay key
    (`instantModel` only for a pair), so a switch between a pair, a two-head
    model alone, an instant model alone or another size's models rebuilds
    state once and never reuses another layout's state.
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

    A size (RWKV_SIZE_MODELS) runs only its own two models, the family
    release's: the two-head model (Big: the show-time big model (SM91s2);
    Small: the show-time retrain (SM92); Tiny: the Tiny two-head model
    (SM106)) and the Instant-Only model (Big: the show-time instant-only big
    model (SM99); Small: the Small Instant-Only model (SM98); Tiny: the Tiny
    Instant-Only model (SM101)):
      - RWKV-Instant and RWKV-Curve, by default: the Instant-Only model beside
        the two-head model (two model states). The Instant-Only model gives
        RWKV-Instant's recall and button probabilities; the two-head model
        keeps the curve, intervals, S90 and the answer time (only Small's
        SM92 has an answer-time head).
      - RWKV-Instant and RWKV-Curve with the `single_model` opt-out: the
        two-head model alone for both (one model state).
      - RWKV-Instant without RWKV-Curve, with or without the opt-out: the
        Instant-Only model alone, an instant-only runtime. It gives
        RWKV-Instant's recall and button probabilities only: no curve recall,
        intervals, S90 or answer time, and no RWKV workload simulation. Due
        dates and memory states come from the presets' FSRS fallback.
      - RWKV-Curve only, or neither: the two-head model alone, the same
        runtime (and state) as the opt-out.
    Every other choice (SM92, SM63, the older small models) runs its one model
    alone for both schedulers.
    """
    size_models = RWKV_SIZE_MODELS.get(model)
    if size_models is None:
        return RwkvRuntimeModels(curve_model_for_choice(model))
    if not instant_used:
        return RwkvRuntimeModels(size_models.two_head)
    if not curve_used:
        return RwkvRuntimeModels(size_models.instant)
    if single_model:
        return RwkvRuntimeModels(size_models.two_head)
    return RwkvRuntimeModels(size_models.two_head, size_models.instant)
