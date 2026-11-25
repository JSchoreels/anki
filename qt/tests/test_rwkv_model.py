# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json
import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import rwkv_scheduler
from aqt.profiles import ProfileManager, profileConf
from aqt.rwkv_model import (
    RWKV_BIG_INSTANT_MODEL,
    RWKV_CHOICE_MODEL_ALIASES,
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_INSTANT_ONLY_MODELS,
    RWKV_MODEL_BIG,
    RWKV_MODEL_FILES,
    RWKV_MODEL_SIZES,
    RWKV_MODEL_SM63,
    RWKV_MODEL_SMALL,
    RWKV_MODEL_TINY,
    RWKV_MODELS,
    RWKV_SIZE_MODELS,
    RWKV_SMALL_INSTANT_MODEL,
    RWKV_SMALL_TWO_HEAD_MODEL,
    RWKV_SMALL_TWO_HEAD_MODEL_SM92,
    RWKV_TINY_INSTANT_MODEL,
    RWKV_TINY_TWO_HEAD_MODEL,
    RwkvRuntimeModels,
    RwkvSizeModels,
    curve_model_for_choice,
    model_for_profile,
    runtime_models,
    single_model_for_profile,
)

# Profile values that run under their own model key when no preset uses
# RWKV-Instant: every choice but Small, Tiny and SM63, which resolve to the
# show-time two-head cool-down for Small (SM118), the six-layer Tiny two-head
# model (SM130) and SM63's key.
RUNTIME_MODEL_KEYS = tuple(
    model
    for model in RWKV_MODELS
    if model not in (RWKV_MODEL_SMALL, RWKV_MODEL_TINY, RWKV_MODEL_SM63)
)
# Model keys a resident backend may run alone: those, plus the default small
# model (SM63, "sm72e25"), the sizes' two-head models (SM118, SM130) and the
# Tiny two-head model (SM106) of the earlier builds.
RESIDENT_MODEL_KEYS = (*RUNTIME_MODEL_KEYS, "sm72e25", "sm118", "sm130th", "sm106")
# The release candidate's family (2026-11): each size's two-head and
# Instant-Only model. A size never runs another size's model.
FAMILY = {
    "legacy": ("legacy", "sm99"),
    "sm72e25": ("sm118", "sm113"),
    "tiny": ("sm130th", "sm130io"),
}


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, "sm72e25"),
        ({}, "sm72e25"),
        ({"rwkvModel": "unknown"}, "sm72e25"),
        ({"rwkvModel": None}, "sm72e25"),
        ({"rwkvModel": 36}, "sm72e25"),
        ({"rwkvModel": "legacy"}, "legacy"),
        ({"rwkvModel": "sm40"}, "sm40"),
        ({"rwkvModel": "sm36"}, "sm36"),
        ({"rwkvModel": "sm55"}, "sm55"),
        ({"rwkvModel": "sm56"}, "sm56"),
        ({"rwkvModel": "sm72"}, "sm72"),
        ({"rwkvModel": "sm72e20"}, "sm72e20"),
        ({"rwkvModel": "sm72e25"}, "sm72e25"),
        ({"rwkvModel": "sm63"}, "sm63"),
        ({"rwkvModel": "sm92"}, "sm92"),
        ({"rwkvModel": "tiny"}, "tiny"),
        # Model keys that are never a profile choice fall back to Small.
        ({"rwkvModel": "sm98"}, "sm72e25"),
        ({"rwkvModel": "sm101"}, "sm72e25"),
        ({"rwkvModel": "sm106"}, "sm72e25"),
        ({"rwkvModel": "sm118"}, "sm72e25"),
        ({"rwkvModel": "sm130th"}, "sm72e25"),
        ({"rwkvModel": "sm130io"}, "sm72e25"),
    ],
)
def test_model_for_profile_defaults_to_sm72e25_and_preserves_explicit_choices(
    profile, expected
):
    assert model_for_profile(profile) == expected


@pytest.mark.parametrize("model", [None, *RWKV_MODELS])
@pytest.mark.parametrize(
    "precision",
    [
        None,
        "f32",
        "int8",
        "int4",
        "cards_int4_notes_int8",
        "compact_cards_int8",
        "compact_cards_int4",
        "compact_lowrank_int8",
        "compact_lowrank",
    ],
)
def test_deck_options_loads_existing_profile_model_and_precision(
    monkeypatch, model, precision
):
    from flask import Flask

    from anki.deck_config_pb2 import DeckConfigsForUpdate
    from aqt import mediasrv

    backend = MagicMock()
    backend.get_deck_configs_for_update_raw.return_value = DeckConfigsForUpdate(
        fsrs=True
    ).SerializeToString()
    profile = {"rwkvModel": model, "rwkvStatePrecision": precision}
    monkeypatch.setattr(
        "aqt.mw",
        SimpleNamespace(
            pm=SimpleNamespace(profile=profile),
            col=SimpleNamespace(
                _backend=backend, card_count=lambda: 123, note_count=lambda: 87
            ),
        ),
    )
    with Flask(__name__).test_request_context(data=b""):
        result = DeckConfigsForUpdate.FromString(mediasrv.get_deck_configs_for_update())
    assert result.fsrs
    assert result.rwkv_collection_counts.cards == 123
    assert result.rwkv_collection_counts.notes == 87
    assert result.rwkv_profile_settings.model == (model or "sm72e25")
    # Full int4 was removed, the int4 default overflowed on long cards, the
    # int8 rows gave way to the low-rank state and its int8 factors to the
    # size-dependent factor width: all fall back to the default.
    assert result.rwkv_profile_settings.state_precision == (
        precision
        if precision
        not in (
            None,
            "int4",
            "cards_int4_notes_int8",
            "compact_cards_int8",
            "compact_lowrank_int8",
        )
        else "compact_lowrank"
    )


@pytest.mark.parametrize(
    "mode,has_settings,should_save",
    [(0, True, True), (1, True, True), (2, True, False), (0, False, False)],
)
def test_deck_options_applies_local_choices_only_after_a_successful_save(
    monkeypatch, mode, has_settings, should_save
):
    from anki.deck_config_pb2 import UpdateDeckConfigsRequest
    from aqt import mediasrv

    profile = {
        "rwkvModel": "sm36",
        "rwkvStatePrecision": "int8",
        "rwkvSpecializedInstantModel": True,
        "other": 42,
    }
    save = MagicMock()
    owner = SimpleNamespace(
        pm=SimpleNamespace(profile=profile, save=save),
        app=SimpleNamespace(activeModalWidget=lambda: None),
    )
    monkeypatch.setattr("aqt.mw", owner)
    active_backend = object()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", active_backend)
    request = UpdateDeckConfigsRequest(mode=mode)
    if has_settings:
        request.rwkv_profile_settings.model = "legacy"
        request.rwkv_profile_settings.state_precision = "compact_cards_int4"
        request.rwkv_profile_settings.single_model = True
    mediasrv._on_update_deck_configs_success(request, close_on_success=False)
    assert profile == {
        "rwkvModel": "legacy" if should_save else "sm36",
        "rwkvStatePrecision": "compact_cards_int4" if should_save else "int8",
        # A save stores the opt-out and drops the former opt-in toggle, whose
        # values meant the opposite.
        **(
            {"rwkvSingleModel": True}
            if should_save
            else {"rwkvSpecializedInstantModel": True}
        ),
        "other": 42,
    }
    assert save.call_count == int(should_save)
    assert rwkv_scheduler._reviewer_backend is active_backend


@pytest.mark.parametrize(
    "model,precision",
    [("unknown", "int8"), ("legacy", "unknown"), ("legacy", "int4"), ("", "")],
)
def test_deck_options_rejects_invalid_runtime_settings_before_starting_save(
    monkeypatch, model, precision
):
    from flask import Flask

    from anki.deck_config_pb2 import UpdateDeckConfigsRequest
    from aqt import mediasrv

    start_save = MagicMock()
    monkeypatch.setattr(mediasrv, "update_deck_configs_op", start_save)
    request = UpdateDeckConfigsRequest()
    request.rwkv_profile_settings.model = model
    request.rwkv_profile_settings.state_precision = precision
    with Flask(__name__).test_request_context(data=request.SerializeToString()):
        with pytest.raises(ValueError, match="Unsupported RWKV"):
            mediasrv._update_deck_configs(close_on_success=False)
    start_save.assert_not_called()


def test_deck_options_pending_or_failed_save_does_not_change_profile(monkeypatch):
    from flask import Flask

    from anki.deck_config_pb2 import UpdateDeckConfigsRequest
    from aqt import mediasrv

    profile = {"rwkvModel": "sm36", "rwkvStatePrecision": "int8"}
    save = MagicMock()
    owner = SimpleNamespace(
        pm=SimpleNamespace(profile=profile, save=save),
        taskman=SimpleNamespace(run_on_main=lambda callback: callback()),
    )
    monkeypatch.setattr("aqt.mw", owner)
    operation = MagicMock()
    monkeypatch.setattr(mediasrv, "update_deck_configs_op", lambda **kwargs: operation)
    request = UpdateDeckConfigsRequest()
    request.rwkv_profile_settings.model = "legacy"
    request.rwkv_profile_settings.state_precision = "f32"
    with Flask(__name__).test_request_context(data=request.SerializeToString()):
        assert mediasrv._update_deck_configs(close_on_success=False) == b""
    operation.success.assert_called_once()
    assert profile == {"rwkvModel": "sm36", "rwkvStatePrecision": "int8"}
    save.assert_not_called()


def test_new_profiles_save_sm72e25_without_rewriting_existing_profiles():
    manager = ProfileManager.__new__(ProfileManager)
    manager.db = MagicMock()
    manager.db.scalar.return_value = False
    manager.create("New profile")
    settings = pickle.loads(manager.db.execute.call_args.args[2])
    assert model_for_profile(settings) == "sm72e25"
    assert settings["rwkvModel"] == "sm72e25"
    assert "rwkvModel" not in profileConf
    manager.db.reset_mock()
    manager.db.scalar.return_value = True
    manager.create("Existing profile")
    manager.db.execute.assert_not_called()


@pytest.mark.parametrize("active_model", RESIDENT_MODEL_KEYS)
@pytest.mark.parametrize(
    "saved_model,filename",
    [
        ("legacy", "RWKV_trained_on_5000_10000.bin"),
        ("sm40", "sm40v2/weights.f32.bin"),
        ("sm36", "sm36/weights.f32.bin"),
        ("sm55", "sm55/weights.f32.bin"),
        ("sm56", "sm56/weights.f32.bin"),
        ("sm72", "sm72/weights.f32.bin"),
        ("sm72e20", "sm72e20/weights.f32.bin"),
        # Without RWKV-Instant in use each size runs its two-head model alone:
        # Small the show-time two-head cool-down for Small (SM118), Tiny the
        # six-layer Tiny two-head model (SM130). The default small model
        # (SM63) only by its own value, the show-time retrain (SM92) too.
        ("sm72e25", "sm118/weights.f32.bin"),
        ("sm63", "sm72e25/weights.f32.bin"),
        ("sm92", "sm92/weights.f32.bin"),
        ("tiny", "sm130th/weights.f32.bin"),
    ],
)
def test_model_choice_waits_for_profile_reopen_and_keeps_precision_independent(
    monkeypatch, active_model, saved_model, filename
):
    import aqt

    profile = {"rwkvModel": saved_model, "rwkvStatePrecision": "int8"}
    monkeypatch.setattr(aqt, "mw", SimpleNamespace(pm=SimpleNamespace(profile=profile)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(model_name=active_model, state_precision="f32"),
    )
    assert rwkv_scheduler._rwkv_model() == active_model
    assert rwkv_scheduler._rwkv_state_precision() == "f32"
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert rwkv_scheduler._rwkv_model() == curve_model_for_choice(saved_model)
    assert rwkv_scheduler._rwkv_state_precision() == "int8"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    assert rwkv_scheduler.embedded_rwkv_model_path() == root / filename


@pytest.mark.parametrize(
    "saved_model,expected_model",
    [
        (None, "sm118"),
        ("legacy", "legacy"),
        ("sm40", "sm40"),
        ("sm36", "sm36"),
        ("sm55", "sm55"),
        ("sm56", "sm56"),
        ("sm72", "sm72"),
        ("sm72e20", "sm72e20"),
        ("sm72e25", "sm118"),
        ("sm63", "sm72e25"),
        ("sm92", "sm92"),
        ("tiny", "sm130th"),
    ],
)
@pytest.mark.parametrize("active_model", RESIDENT_MODEL_KEYS)
@pytest.mark.parametrize(
    "saved_precision, replace_for_precision",
    [
        (None, True),
        ("f32", True),
        ("int8", False),
        ("int4", True),
        ("cards_int4_notes_int8", True),
        ("compact_cards_int8", True),
        ("compact_cards_int4", True),
        ("compact_lowrank_int8", True),
        ("compact_lowrank", True),
    ],
)
def test_profile_open_reuses_only_matching_model_and_precision(
    monkeypatch,
    saved_model,
    active_model,
    expected_model,
    saved_precision,
    replace_for_precision,
):
    from aqt.rwkv_srs_benchmark import EmbeddedRwkvReviewerBackend

    backend = EmbeddedRwkvReviewerBackend.__new__(EmbeddedRwkvReviewerBackend)
    backend.model_name = active_model
    backend.state_precision = "int8"
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_invalidate_reviewer_backend_runtime_state_for_profile_open",
        lambda: None,
    )
    replace = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "set_reviewer_backend", replace)
    owner = SimpleNamespace(
        pm=SimpleNamespace(
            profile={
                **({"rwkvStatePrecision": saved_precision} if saved_precision else {}),
                **({"rwkvModel": saved_model} if saved_model else {}),
            }
        )
    )
    rwkv_scheduler.begin_rwkv_state_cache_startup(owner)
    if expected_model != active_model or replace_for_precision:
        replace.assert_called_once_with(None)
    else:
        replace.assert_not_called()


@pytest.mark.parametrize("model", [None, *RWKV_MODELS])
@pytest.mark.parametrize(
    "precision",
    [
        None,
        "f32",
        "int8",
        "int4",
        "cards_int4_notes_int8",
        "compact_cards_int8",
        "compact_cards_int4",
        "compact_lowrank_int8",
        "compact_lowrank",
    ],
)
def test_profile_model_and_precision_reach_the_native_backend(
    monkeypatch, model, precision
):
    import aqt

    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            pm=SimpleNamespace(
                profile={
                    **({"rwkvStatePrecision": precision} if precision else {}),
                    **({"rwkvModel": model} if model else {}),
                }
            )
        ),
    )
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "set_reviewer_backend",
        lambda backend: setattr(rwkv_scheduler, "_reviewer_backend", backend),
    )
    assert rwkv_scheduler.configure_reviewer_backend_from_environment()
    backend = rwkv_scheduler._reviewer_backend
    # No preset uses RWKV-Instant here: each size runs its two-head model.
    assert backend.model_name == curve_model_for_choice(model or "sm72e25")
    assert backend.state_precision == (
        precision
        if precision
        not in (
            None,
            "int4",
            "cards_int4_notes_int8",
            "compact_cards_int8",
            "compact_lowrank_int8",
        )
        else "compact_lowrank"
    )


@pytest.mark.parametrize("active_model", RUNTIME_MODEL_KEYS)
@pytest.mark.parametrize("saved_model", RUNTIME_MODEL_KEYS)
def test_cache_replay_identity_tracks_active_model_until_profile_reopen(
    monkeypatch, active_model, saved_model
):
    import aqt

    profile = {"rwkvModel": active_model, "rwkvStatePrecision": "int8"}
    monkeypatch.setattr(aqt, "mw", SimpleNamespace(pm=SimpleNamespace(profile=profile)))
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda _: False,
    )
    source = rwkv_scheduler.RwkvFirstReviewElapsedSource.CARD_CREATION

    def key():
        return rwkv_scheduler._rwkv_replay_semantics_key(
            object(), first_review_elapsed_source=source
        )

    active_key = key()
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(model_name=active_model, state_precision="int8"),
    )
    profile["rwkvModel"] = saved_model
    assert key() == active_key
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert (key() == active_key) == (saved_model == active_model)


@pytest.mark.parametrize(
    "instant_used,curve_used,single,expected",
    [
        # RWKV-Instant and no RWKV-Curve (as scheduler or as RWKV-Instant's
        # fallback): the longer Small Instant-Only model (SM113) alone, an
        # instant-only runtime, whatever the opt-out (the Tiny Instant-Only
        # model (SM101) in SM112, the Small Instant-Only model (SM98) until
        # 2026-11-16).
        (True, False, False, RwkvRuntimeModels("sm113")),
        (True, False, True, RwkvRuntimeModels("sm113")),
        # Both: SM113 runs beside SM118, which keeps the curve, intervals and
        # S90. The opt-out uses SM118 alone.
        (True, True, False, RwkvRuntimeModels("sm118", "sm113")),
        (True, True, True, RwkvRuntimeModels("sm118")),
        # Only RWKV-Curve, or neither: SM118 alone.
        (False, True, False, RwkvRuntimeModels("sm118")),
        (False, True, True, RwkvRuntimeModels("sm118")),
        (False, False, False, RwkvRuntimeModels("sm118")),
        (False, False, True, RwkvRuntimeModels("sm118")),
    ],
)
def test_runtime_models_pick_the_instant_model_per_scheduler(
    instant_used, curve_used, single, expected
):
    models = runtime_models(
        "sm72e25",
        instant_used=instant_used,
        curve_used=curve_used,
        single_model=single,
    )
    assert models == expected
    if instant_used:
        assert models.instant_scheduler_model == (
            "sm118" if curve_used and single else "sm113"
        )
    # A curve exists exactly when a preset uses RWKV-Curve or nothing uses
    # RWKV-Instant, and it always comes from SM118. Neither the default small
    # model (SM63) nor the instant-only small model (SM57) is ever picked.
    if instant_used and not curve_used:
        assert models.curve_model is None
        assert models.model in RWKV_INSTANT_ONLY_MODELS
    else:
        assert models.curve_model == models.model == "sm118"
    assert "sm57" not in models
    assert "sm72e25" not in models


def test_instant_only_model_stays_selectable():
    # The instant-only small model (SM57) stays bundled but unused by default;
    # alone it never serves a curve. The show-time retrain (SM92) stays
    # bundled and selectable as a profile model.
    assert RWKV_SMALL_TWO_HEAD_MODEL_SM92 == "sm92"
    assert RWKV_SMALL_TWO_HEAD_MODEL_SM92 in RWKV_MODELS
    assert RWKV_MODEL_FILES[RWKV_SMALL_TWO_HEAD_MODEL_SM92] == Path(
        "sm92/weights.f32.bin"
    )
    assert RWKV_INSTANT_ONLY_MODEL == "sm57"
    assert RWKV_INSTANT_ONLY_MODEL in RWKV_MODEL_FILES
    assert RwkvRuntimeModels("sm57").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm57").curve_model == "sm72e25"


@pytest.mark.parametrize("instant_used", [False, True])
@pytest.mark.parametrize("curve_used", [False, True])
@pytest.mark.parametrize("single", [False, True])
def test_default_small_model_stays_bundled_and_selectable(
    instant_used, curve_used, single
):
    # No size picks the default small model (SM63) since the family wiring
    # (SM114); it stays bundled under its model key "sm72e25" and runs alone
    # for both schedulers when a profile names it as "sm63".
    assert RWKV_MODEL_SM63 == "sm63" and RWKV_MODEL_SM63 in RWKV_MODELS
    assert RWKV_CHOICE_MODEL_ALIASES == {"sm63": "sm72e25"}
    assert model_for_profile({"rwkvModel": "sm63"}) == "sm63"
    assert curve_model_for_choice("sm63") == "sm72e25"
    assert runtime_models(
        "sm63", instant_used=instant_used, curve_used=curve_used, single_model=single
    ) == RwkvRuntimeModels("sm72e25")
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for name in ("weights.f32.bin", "time_head.f32.bin", "architecture.json"):
        assert (root / "sm72e25" / name).exists()
    for size in RWKV_MODEL_SIZES:
        assert "sm72e25" not in runtime_models(
            size, instant_used=instant_used, curve_used=curve_used, single_model=single
        )


def test_tiny_instant_model_is_sm130io_and_instant_only():
    # The six-layer Tiny Instant-Only model (SM130), Tiny's Instant-Only
    # model since the 2026-11 release candidate: bundled, never a profile's
    # model key (the profile stores "tiny"), alone it serves no curve. The
    # longer cool-down for Tiny (SM115) and the Tiny Instant-Only model
    # (SM101) it replaces stay bundled and instant-only.
    assert RWKV_TINY_INSTANT_MODEL == "sm130io"
    assert RWKV_SIZE_MODELS[RWKV_MODEL_TINY].instant == "sm130io"
    for model in ("sm130io", "sm115", "sm101"):
        assert model in RWKV_INSTANT_ONLY_MODELS
        assert RWKV_MODEL_FILES[model] == Path(f"{model}/weights.f32.bin")
        assert model not in RWKV_MODELS
        assert RwkvRuntimeModels(model).curve_model is None
    assert RwkvRuntimeModels("sm130th", "sm130io").curve_model == "sm130th"
    assert RwkvRuntimeModels("sm130th", "sm130io").instant_scheduler_model == "sm130io"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for model in ("sm130io", "sm115", "sm101"):
        assert (root / RWKV_MODEL_FILES[model]).exists()
    # The six-layer plan (w64-c1-d2p1u1) and its 346-input answer-time head.
    spec = json.loads((root / "sm130io" / "architecture.json").read_text())
    assert spec["architecture"] == "w64-c1-d2p1u1"
    assert [s["n_layers"] for s in spec["streams"]] == [1, 2, 1, 1, 1]
    head = json.loads((root / "sm130io" / "time_head.json").read_text())
    assert head["weights"]["tensors"]["time_mlps.0.hidden.weight"] == [128, 346]
    # The tiny plan: SM101's container and sizes (one card layer).
    for name in ("weights.f32.bin", "weights.int8.bin"):
        assert (root / "sm115" / name).stat().st_size == (
            root / "sm101" / name
        ).stat().st_size
    assert (root / "sm115" / "architecture.json").exists()


def test_tiny_two_head_model_is_sm130th_and_a_full_model():
    # The six-layer Tiny two-head model (SM130): bundled, never a profile's
    # model key, a full model (it serves the curve) on the six-layer plan,
    # Tiny's curve model in the pair and alone, with a 346-input answer-time
    # head (SM138).
    assert RWKV_TINY_TWO_HEAD_MODEL == "sm130th"
    assert RWKV_SIZE_MODELS[RWKV_MODEL_TINY].two_head == "sm130th"
    assert RWKV_TINY_TWO_HEAD_MODEL not in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm130th"] == Path("sm130th/weights.f32.bin")
    assert "sm130th" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm130th").curve_model == "sm130th"
    assert RwkvRuntimeModels("sm130th").instant_scheduler_model == "sm130th"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for name in (
        "weights.f32.bin",
        "weights.int8.bin",
        "architecture.json",
        "time_head.f32.bin",
        "time_head.int8.bin",
        "time_head.json",
    ):
        assert (root / "sm130th" / name).exists()
    # The same architecture as the Instant-Only model, smaller than Tiny's.
    assert (root / "sm130th" / "architecture.json").read_bytes() == (
        root / "sm130io" / "architecture.json"
    ).read_bytes()
    assert (root / "sm130th" / "weights.f32.bin").stat().st_size < (
        root / "sm106" / "weights.f32.bin"
    ).stat().st_size
    with open(root / RWKV_MODEL_FILES["sm130th"], "rb") as file:
        assert file.read(13) == b"ARWKVWEIGHTS1"


def test_small_two_head_model_is_sm118_and_sm92_stays_a_profile_value():
    # The show-time two-head cool-down for Small (SM118): Small's two-head
    # model since the 2026-11 release candidate, never a profile's model key.
    # The show-time retrain (SM92) it replaces stays a preserved profile
    # value that runs SM92 alone.
    assert RWKV_SMALL_TWO_HEAD_MODEL == "sm118"
    assert RWKV_SIZE_MODELS[RWKV_MODEL_SMALL].two_head == "sm118"
    assert RWKV_MODEL_FILES["sm118"] == Path("sm118/weights.f32.bin")
    assert "sm118" not in RWKV_MODELS and "sm92" in RWKV_MODELS
    assert curve_model_for_choice("sm92") == "sm92"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for name in ("weights.f32.bin", "architecture.json", "time_head.f32.bin"):
        assert (root / "sm118" / name).exists()
    for name in ("weights.f32.bin", "weights.int8.bin"):
        assert (root / "sm118" / name).stat().st_size == (
            root / "sm92" / name
        ).stat().st_size


def test_tiny_two_head_model_sm106_stays_bundled():
    # The Tiny two-head model (SM106): Tiny's two-head model until the
    # 2026-11 release candidate; bundled, never a profile's model key, a full
    # model on the tiny plan.
    assert "sm106" not in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm106"] == Path("sm106/weights.f32.bin")
    assert "sm106" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm106").curve_model == "sm106"
    assert RwkvRuntimeModels("sm106").instant_scheduler_model == "sm106"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for name in ("weights.f32.bin", "weights.int8.bin", "architecture.json"):
        assert (root / "sm106" / name).exists()
    # The tiny architecture and file sizes of the Tiny Instant-Only model
    # (SM101), the same ARWKVWEIGHTS1 container; an answer-time head since
    # the time heads for the family (SM117).
    assert (root / "sm106" / "architecture.json").read_bytes() == (
        root / "sm101" / "architecture.json"
    ).read_bytes()
    for name in ("weights.f32.bin", "weights.int8.bin"):
        assert (root / "sm106" / name).stat().st_size == (
            root / "sm101" / name
        ).stat().st_size
    with open(root / RWKV_MODEL_FILES["sm106"], "rb") as file:
        assert file.read(13) == b"ARWKVWEIGHTS1"
    for name in ("time_head.f32.bin", "time_head.int8.bin", "time_head.json"):
        assert (root / "sm106" / name).exists()


def test_instant_alone_runs_the_size_own_instant_model():
    # The user's decision of 2026-11-13 (family wiring (SM114)): RWKV-Instant
    # without RWKV-Curve runs the size's own Instant-Only model: since
    # 2026-11-16 the longer Small Instant-Only model (SM113) for Small, since
    # the 2026-11 release candidate the six-layer Tiny Instant-Only model
    # (SM130) for Tiny.
    for size, expected in (
        ("legacy", "sm99"),
        ("sm72e25", "sm113"),
        ("tiny", "sm130io"),
    ):
        assert RWKV_SIZE_MODELS[size].instant == expected
        for single in (False, True):
            assert runtime_models(
                size, instant_used=True, curve_used=False, single_model=single
            ) == RwkvRuntimeModels(expected)


def test_big_instant_model_is_sm99_and_instant_only():
    # The show-time instant-only big model (SM99): bundled, never a profile's
    # model key (the profile stores "legacy"), alone it serves no curve.
    assert RWKV_BIG_INSTANT_MODEL == "sm99"
    assert RWKV_BIG_INSTANT_MODEL in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_SIZE_MODELS[RWKV_MODEL_BIG].instant == "sm99"
    assert RWKV_MODEL_FILES["sm99"] == Path("sm99/weights.f32.bin")
    assert "sm99" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm99").curve_model is None
    assert RwkvRuntimeModels("legacy", "sm99").curve_model == "legacy"
    assert RwkvRuntimeModels("legacy", "sm99").instant_scheduler_model == "sm99"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    assert (root / RWKV_MODEL_FILES["sm99"]).exists()
    assert (root / "sm99" / "architecture.json").exists()
    assert (root / "sm99" / "manifest.json").exists()
    # The same format as the Big file: the fork's ARWKVWEIGHTS1 container.
    for name in ("legacy", "sm99"):
        with open(root / RWKV_MODEL_FILES[name], "rb") as file:
            assert file.read(13) == b"ARWKVWEIGHTS1"
    assert (root / RWKV_MODEL_FILES["sm99"]).stat().st_size == (
        root / RWKV_MODEL_FILES["legacy"]
    ).stat().st_size


def test_model_sizes_are_big_small_tiny():
    # Big and Small keep the values earlier builds stored.
    assert RWKV_MODEL_SIZES == ("legacy", "sm72e25", "tiny")
    assert (RWKV_MODEL_BIG, RWKV_MODEL_SMALL, RWKV_MODEL_TINY) == RWKV_MODEL_SIZES
    assert set(RWKV_MODEL_SIZES) <= set(RWKV_MODELS)
    # Each size's two-head model keeps RWKV-Curve; Big's has the size's key.
    assert [curve_model_for_choice(m) for m in RWKV_MODEL_SIZES] == [
        "legacy",
        "sm118",
        "sm130th",
    ]
    assert all(curve_model_for_choice(m) == m for m in RUNTIME_MODEL_KEYS)
    # The family release, model for model.
    assert RWKV_SIZE_MODELS == {
        size: RwkvSizeModels(*models) for size, models in FAMILY.items()
    }
    # No model belongs to two sizes.
    family_models = [m for models in FAMILY.values() for m in models]
    assert len(set(family_models)) == len(family_models) == 6


# The three sizes x the schedulers in use x the one-model opt-out: each size
# runs only its own two models of the family release (SM114).
_SIZE_RUNTIMES = {
    # Big: the show-time instant-only big model (SM99) alone for
    # RWKV-Instant without RWKV-Curve, the pair for both, the show-time big
    # model (SM91s2) alone otherwise and with the opt-out.
    "legacy": {
        "instant only": ("sm99", None),
        "both": ("legacy", "sm99"),
        "curve only": ("legacy", None),
        "neither": ("legacy", None),
        "both, opt-out": ("legacy", None),
        "instant only, opt-out": ("sm99", None),
    },
    # Small: the longer Small Instant-Only model (SM113) alone for
    # RWKV-Instant without RWKV-Curve, the show-time two-head cool-down for
    # Small (SM118) + SM113 pair for both, SM118 alone otherwise and with the
    # opt-out (the show-time retrain (SM92) until the 2026-11 release
    # candidate).
    "sm72e25": {
        "instant only": ("sm113", None),
        "both": ("sm118", "sm113"),
        "curve only": ("sm118", None),
        "neither": ("sm118", None),
        "both, opt-out": ("sm118", None),
        "instant only, opt-out": ("sm113", None),
    },
    # Tiny: the six-layer Tiny Instant-Only model (SM130) alone, the six-layer
    # Tiny two-head model (SM130) + its Instant-Only model for both, the
    # two-head model alone otherwise and with the opt-out (the Tiny two-head
    # model (SM106) and the longer cool-down for Tiny (SM115) until the
    # 2026-11 release candidate).
    "tiny": {
        "instant only": ("sm130io", None),
        "both": ("sm130th", "sm130io"),
        "curve only": ("sm130th", None),
        "neither": ("sm130th", None),
        "both, opt-out": ("sm130th", None),
        "instant only, opt-out": ("sm130io", None),
    },
}
# The previous build's mapping (the family wiring (SM114), with the Small
# Instant-Only model (SM98) and the Tiny Instant-Only model (SM101)), for
# the one-time rebuilds.
_PREVIOUS_RUNTIMES = {
    "legacy": {
        "instant only": ("sm99", None),
        "both": ("legacy", "sm99"),
        "curve only": ("legacy", None),
        "neither": ("legacy", None),
        "both, opt-out": ("legacy", None),
        "instant only, opt-out": ("sm99", None),
    },
    "sm72e25": {
        "instant only": ("sm98", None),
        "both": ("sm92", "sm98"),
        "curve only": ("sm92", None),
        "neither": ("sm92", None),
        "both, opt-out": ("sm92", None),
        "instant only, opt-out": ("sm98", None),
    },
    "tiny": {
        "instant only": ("sm101", None),
        "both": ("sm106", "sm101"),
        "curve only": ("sm106", None),
        "neither": ("sm106", None),
        "both, opt-out": ("sm106", None),
        "instant only, opt-out": ("sm101", None),
    },
}
_USE_CASES = {
    "instant only": (True, False, False),
    "both": (True, True, False),
    "curve only": (False, True, False),
    "neither": (False, False, False),
    "both, opt-out": (True, True, True),
    "instant only, opt-out": (True, False, True),
}


@pytest.mark.parametrize("size", RWKV_MODEL_SIZES)
@pytest.mark.parametrize("case", list(_USE_CASES))
def test_model_sizes_pick_their_runtime_models(size, case):
    instant_used, curve_used, single = _USE_CASES[case]
    models = runtime_models(
        size, instant_used=instant_used, curve_used=curve_used, single_model=single
    )
    assert models == RwkvRuntimeModels(*_SIZE_RUNTIMES[size][case])
    two_head, instant = FAMILY[size]
    # The curve never comes from an instant-only model, and there is one
    # exactly when a preset uses RWKV-Curve or nothing uses RWKV-Instant; it
    # is always the size's two-head model.
    if instant_used and not curve_used:
        assert models.curve_model is None
        assert models.model == instant
    else:
        assert models.curve_model == models.model == two_head
        assert models.curve_model == curve_model_for_choice(size)
    if instant_used:
        assert models.instant_scheduler_model == (
            two_head if curve_used and single else instant
        )
    # No size ever loads another size's weights.
    own_files = {RWKV_MODEL_FILES[m] for m in FAMILY[size]}
    other_files = {
        RWKV_MODEL_FILES[m]
        for other, other_models in FAMILY.items()
        if other != size
        for m in other_models
    }
    loaded = {RWKV_MODEL_FILES[m] for m in models if m is not None}
    assert loaded <= own_files and not loaded & other_files
    assert RWKV_MODEL_FILES["sm72e25"] not in loaded


@pytest.mark.parametrize(
    "stored,choice,runtime_both",
    [
        # Big pairs the show-time big model (SM91s2) with the show-time
        # instant-only big model (SM99).
        ("legacy", "legacy", ("legacy", "sm99")),
        # Unset, the old default key and unknown values: Small, now the
        # show-time two-head cool-down for Small (SM118) with the longer
        # Small Instant-Only model (SM113).
        (None, "sm72e25", ("sm118", "sm113")),
        ("sm72e25", "sm72e25", ("sm118", "sm113")),
        ("unknown", "sm72e25", ("sm118", "sm113")),
        ("tiny", "tiny", ("sm130th", "sm130io")),
        # Older small models (and the default small model (SM63) by its own
        # value) keep running alone until a Deck Options save stores Small
        # (ts/routes/deck-options/lib.ts); SM92 is preserved.
        ("sm63", "sm63", ("sm72e25", None)),
        ("sm40", "sm40", ("sm40", None)),
        ("sm36", "sm36", ("sm36", None)),
        ("sm55", "sm55", ("sm55", None)),
        ("sm56", "sm56", ("sm56", None)),
        ("sm72", "sm72", ("sm72", None)),
        ("sm72e20", "sm72e20", ("sm72e20", None)),
        ("sm92", "sm92", ("sm92", None)),
    ],
)
def test_existing_profiles_migrate_to_the_model_sizes(stored, choice, runtime_both):
    profile = {} if stored is None else {"rwkvModel": stored}
    assert model_for_profile(profile) == choice
    owner = _config_owner(_deck_configs(curve=True, instant=True), profile)
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(owner) == (
        RwkvRuntimeModels(*runtime_both)
    )


@pytest.mark.parametrize("size", RWKV_MODEL_SIZES)
def test_custom_model_path_replaces_the_choice_full_model(monkeypatch, size):
    monkeypatch.setenv("ANKI_RWKV_MODEL_PATH", "custom.bin")
    owner = _config_owner(_deck_configs(curve=True, instant=True), {"rwkvModel": size})
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(owner) == (
        RwkvRuntimeModels(curve_model_for_choice(size))
    )


def test_specialized_instant_model_is_sm113_and_instant_only():
    # The longer Small Instant-Only model (SM113), Small's Instant-Only model
    # since 2026-11-16: bundled, never a profile's model choice, and alone it
    # serves no curve. The Small Instant-Only model (SM98) it replaces stays
    # bundled and instant-only.
    assert RWKV_SMALL_INSTANT_MODEL == "sm113"
    assert RWKV_SIZE_MODELS[RWKV_MODEL_SMALL].instant == "sm113"
    for model in ("sm113", "sm98"):
        assert model in RWKV_INSTANT_ONLY_MODELS
        assert RWKV_MODEL_FILES[model] == Path(f"{model}/weights.f32.bin")
        assert model not in RWKV_MODELS
        assert RwkvRuntimeModels(model).curve_model is None
    assert RwkvRuntimeModels("sm92", "sm113").curve_model == "sm92"


@pytest.mark.parametrize("model", [m for m in RWKV_MODELS if m not in RWKV_MODEL_SIZES])
@pytest.mark.parametrize("instant_used", [False, True])
@pytest.mark.parametrize("curve_used", [False, True])
def test_runtime_models_keep_other_models_shared(model, instant_used, curve_used):
    # SM92, SM63 and the older small models serve both schedulers alone.
    assert runtime_models(
        model,
        instant_used=instant_used,
        curve_used=curve_used,
        single_model=False,
    ) == RwkvRuntimeModels(RWKV_CHOICE_MODEL_ALIASES.get(model, model))
    assert "sm57" not in RWKV_MODELS


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, False),
        ({}, False),
        ({"rwkvSingleModel": False}, False),
        ({"rwkvSingleModel": "yes"}, False),
        ({"rwkvSingleModel": True}, True),
        # The former opt-in toggle is ignored either way.
        ({"rwkvSpecializedInstantModel": True}, False),
        ({"rwkvSpecializedInstantModel": False}, False),
    ],
)
def test_single_model_opt_out_defaults_off(profile, expected):
    assert single_model_for_profile(profile) is expected


def _deck_configs(*, curve, instant, fallback_curve=False):
    configs = [
        {"id": 1, "rwkvReviewEnabled": curve, "rwkvReviewInstantOrderEnabled": False},
        {"id": 2, "rwkvReviewEnabled": False, "rwkvReviewInstantOrderEnabled": instant},
    ]
    if fallback_curve:
        # RWKV-Instant with "Fallback : Due-Date Calculator" set to RWKV-Curve.
        configs.append(
            {"id": 3, "rwkvReviewEnabled": True, "rwkvReviewInstantOrderEnabled": True}
        )
    return configs


def _config_owner(configs, profile=None):
    return SimpleNamespace(
        pm=SimpleNamespace(profile=profile if profile is not None else {}),
        col=SimpleNamespace(
            decks=SimpleNamespace(all_config=lambda: configs),
            get_config=lambda key: None,
        ),
    )


@pytest.mark.parametrize(
    "configs,curve_enabled",
    [
        (_deck_configs(curve=False, instant=False, fallback_curve=True), True),
        (_deck_configs(curve=False, instant=True, fallback_curve=True), True),
        (_deck_configs(curve=False, instant=True), False),
        # The nested form saved by Deck Options.
        (
            [
                {
                    "id": 4,
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": True,
                        "rwkv_review_instant_order_enabled": True,
                    },
                }
            ],
            True,
        ),
        (
            [
                {
                    "id": 5,
                    "jschoreels.rwkv": {
                        "rwkv_review_enabled": False,
                        "rwkv_review_instant_order_enabled": True,
                    },
                }
            ],
            False,
        ),
    ],
)
def test_curve_as_instant_fallback_counts_as_curve_used(configs, curve_enabled):
    reviewer = SimpleNamespace(mw=_config_owner(configs))
    state = rwkv_scheduler._rwkv_collection_config_state(reviewer)
    assert state.curve_enabled is curve_enabled
    assert state.instant_enabled
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(reviewer.mw) == (
        RwkvRuntimeModels("sm118", "sm113")
        if curve_enabled
        else RwkvRuntimeModels("sm113")
    )


def test_instant_with_curve_fallback_counts_as_curve_used_but_fsrs_fallback_does_not():
    # RWKV-Instant with RWKV-Curve as its due-date fallback: curve used.
    curve_fallback = SimpleNamespace(
        mw=_config_owner(_deck_configs(curve=False, instant=False, fallback_curve=True))
    )
    state = rwkv_scheduler._rwkv_collection_config_state(curve_fallback)
    assert state.instant_enabled and state.curve_enabled
    # RWKV-Instant with the FSRS fallback (rwkv_review_enabled off): not used.
    fsrs_fallback = SimpleNamespace(
        mw=_config_owner(_deck_configs(curve=False, instant=True))
    )
    state = rwkv_scheduler._rwkv_collection_config_state(fsrs_fallback)
    assert state.instant_enabled and not state.curve_enabled
    for size, (two_head, instant_model) in (
        ("sm72e25", FAMILY["sm72e25"]),
        ("tiny", FAMILY["tiny"]),
    ):
        profile = {"rwkvModel": size}
        curve_fallback.mw.pm.profile = profile
        fsrs_fallback.mw.pm.profile = profile
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            curve_fallback.mw
        ) == RwkvRuntimeModels(two_head, instant_model)
        # RWKV-Instant alone: the size's own Instant-Only model.
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            fsrs_fallback.mw
        ) == RwkvRuntimeModels(instant_model)
        profile["rwkvSingleModel"] = True
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            curve_fallback.mw
        ) == RwkvRuntimeModels(two_head)
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            fsrs_fallback.mw
        ) == RwkvRuntimeModels(instant_model)
    # Big: the show-time big model (SM91s2) keeps the curve beside SM99, and
    # runs alone with the opt-out.
    profile = {"rwkvModel": "legacy"}
    curve_fallback.mw.pm.profile = profile
    fsrs_fallback.mw.pm.profile = profile
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(
        curve_fallback.mw
    ) == RwkvRuntimeModels("legacy", "sm99")
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(
        fsrs_fallback.mw
    ) == RwkvRuntimeModels("sm99")
    profile["rwkvSingleModel"] = True
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(
        curve_fallback.mw
    ) == RwkvRuntimeModels("legacy")
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(
        fsrs_fallback.mw
    ) == RwkvRuntimeModels("sm99")


@pytest.mark.parametrize(
    "curve,instant,fallback_curve,single,expected",
    [
        # RWKV-Instant without RWKV-Curve: the Small Instant-Only model
        # (SM113) alone, opt-out ignored.
        (False, True, False, False, ("sm113", None)),
        (False, True, False, True, ("sm113", None)),
        # RWKV-Curve as a preset's scheduler, or as RWKV-Instant's fallback.
        (True, True, False, False, ("sm118", "sm113")),
        (True, True, False, True, ("sm118", None)),
        (False, False, True, False, ("sm118", "sm113")),
        (False, False, True, True, ("sm118", None)),
        (False, True, True, False, ("sm118", "sm113")),
        # RWKV-Curve only, or neither.
        (True, False, False, False, ("sm118", None)),
        (True, False, False, True, ("sm118", None)),
        (False, False, False, False, ("sm118", None)),
    ],
)
@pytest.mark.parametrize(
    "size,instant_model",
    [("sm72e25", "sm113"), ("tiny", "sm130io"), ("legacy", "sm99")],
)
def test_profile_presets_and_opt_out_reach_the_native_backend(
    monkeypatch, curve, instant, fallback_curve, single, expected, size, instant_model
):
    # The rows are Small's; every size runs its own two models in the same
    # places: its Instant-Only model where Small runs the Small Instant-Only
    # model (SM113), its two-head model where Small runs the show-time
    # two-head cool-down for Small (SM118).
    own = dict(zip(FAMILY["sm72e25"], FAMILY[size]))
    expected = tuple(own.get(m, m) for m in expected)
    import aqt
    from aqt import rwkv_srs_benchmark

    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    configs = _deck_configs(curve=curve, instant=instant, fallback_curve=fallback_curve)
    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            pm=SimpleNamespace(
                profile={
                    "rwkvModel": size,
                    "rwkvSingleModel": single,
                }
            ),
            col=SimpleNamespace(
                decks=SimpleNamespace(all_config=lambda: configs),
                get_config=lambda key: None,
            ),
        ),
    )
    created = {}

    class Runtime:
        def __init__(self, **kwargs):
            created.update(kwargs)

    monkeypatch.setattr(rwkv_srs_benchmark, "_RustRwkvRuntime", Runtime)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.setattr(
        rwkv_scheduler,
        "set_reviewer_backend",
        lambda backend: setattr(rwkv_scheduler, "_reviewer_backend", backend),
    )
    assert rwkv_scheduler.configure_reviewer_backend_from_environment()
    backend = rwkv_scheduler._reviewer_backend
    assert (backend.model_name, backend.instant_model_name) == expected
    assert (created["model"], created["instant_model"]) == expected
    if expected[0] == "legacy":
        assert Path(created["model_path"]).name == "RWKV_trained_on_5000_10000.bin"
    else:
        assert Path(created["model_path"]).parent.name == expected[0]
    if expected[1] is None:
        assert created["instant_model_path"] is None
    else:
        assert Path(created["instant_model_path"]).parent.name == instant_model
    # No size ever loads another size's weights: the native runtime gets
    # only the size's own files.
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    own_files = {root / RWKV_MODEL_FILES[m] for m in FAMILY[size]}
    loaded = {Path(created["model_path"])}
    if created["instant_model_path"] is not None:
        loaded.add(Path(created["instant_model_path"]))
    assert loaded <= own_files
    # The cache identity follows the runtime models until the next profile open.
    assert rwkv_scheduler._rwkv_runtime_models() == RwkvRuntimeModels(*expected)
    # Workload simulation needs a curve model: not with an Instant-Only model
    # alone.
    assert rwkv_scheduler.rwkv_workload_simulation_available() == (
        expected[0] not in ("sm113", "sm130io", "sm99")
    )


def test_paired_runtime_gets_its_own_replay_identity(monkeypatch):
    import aqt

    monkeypatch.setattr(
        aqt, "mw", SimpleNamespace(pm=SimpleNamespace(profile={"rwkvModel": "sm72e25"}))
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    keys = []
    for model, instant_model in (
        ("sm72e25", None),
        ("sm72e25", "sm57"),
        ("sm72e25", "sm92"),
        ("sm72e25", "sm98"),
        # Tiny's pair until SM114: the Tiny Instant-Only model (SM101) beside
        # the release.
        ("sm72e25", "sm101"),
        # The family pairs (SM114): Small's show-time retrain (SM92) with the
        # Small Instant-Only model (SM98), Tiny's two-head model (SM106) with
        # SM101.
        ("sm92", "sm98"),
        ("sm106", "sm101"),
        # The show-time instant-only model, stage 2 (SM98) alone, SM101
        # alone, and the show-time retrain (SM92) alone.
        ("sm98", None),
        ("sm101", None),
        # The pairs and Instant-Only models since 2026-11-16: the longer
        # Small Instant-Only model (SM113) and the longer cool-down for Tiny
        # (SM115), beside their two-head models and alone.
        ("sm92", "sm113"),
        ("sm106", "sm115"),
        ("sm113", None),
        ("sm115", None),
        ("sm92", None),
        # The Tiny two-head model (SM106) alone, Tiny's one model.
        ("sm106", None),
        # The release candidate's Small and Tiny (2026-11): the show-time
        # two-head cool-down for Small (SM118) and the six-layer Tiny
        # (SM130), in pairs and alone.
        ("sm118", "sm113"),
        ("sm118", None),
        ("sm130th", "sm130io"),
        ("sm130th", None),
        ("sm130io", None),
        # Big alone, Big's pair (the show-time instant-only big model (SM99)
        # beside the show-time big model (SM91s2)) and SM99 alone.
        ("legacy", None),
        ("legacy", "sm99"),
        ("sm99", None),
    ):
        monkeypatch.setattr(
            rwkv_scheduler,
            "_reviewer_backend",
            SimpleNamespace(
                model_name=model,
                instant_model_name=instant_model,
                state_precision="int8",
            ),
        )
        keys.append(
            rwkv_scheduler._rwkv_replay_semantics_key(
                SimpleNamespace(),
                first_review_elapsed_source=(
                    rwkv_scheduler.RwkvFirstReviewElapsedSource.DECK_CONFIG
                ),
            )
        )
    # Single, SM98 alone, SM101 alone, SM106 alone, SM99 alone and every
    # pair's states never share a key: a switch between the sizes, or
    # between a pair and its instant model alone, rebuilds once.
    assert len(set(keys)) == len(keys)


def _resident_identity(monkeypatch, models):
    """The replay key and the model cache key of a backend running `models`."""
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(
            model_name=models.model,
            instant_model_name=models.instant_model,
            state_precision="int8",
        ),
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_signature", None)
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_model_cache_value", None)
    replay_key = rwkv_scheduler._rwkv_replay_semantics_key(
        SimpleNamespace(),
        first_review_elapsed_source=(
            rwkv_scheduler.RwkvFirstReviewElapsedSource.DECK_CONFIG
        ),
    )
    return replay_key, rwkv_scheduler._rwkv_model_cache_key()


def test_switching_sizes_and_schedulers_rebuilds_or_reuses_state(monkeypatch):
    # Every size x schedulers x opt-out combination (SM112, updated for the
    # family wiring (SM114)). Two combinations keep the same saved state
    # exactly when they run the same models; otherwise the replay key
    # differs and the state rebuilds once.
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    identities = {}
    for size in RWKV_MODEL_SIZES:
        for case, (instant_used, curve_used, single) in _USE_CASES.items():
            models = runtime_models(
                size,
                instant_used=instant_used,
                curve_used=curve_used,
                single_model=single,
            )
            identities[(size, case)] = (
                models,
                *_resident_identity(monkeypatch, models),
            )
    assert len(identities) == 18
    for first, (models, replay_key, model_key) in identities.items():
        assert model_key is not None, first
        for second, (other, other_replay_key, other_model_key) in identities.items():
            assert (replay_key == other_replay_key) == (models == other), (
                first,
                second,
            )
            if first[0] != second[0]:
                # No two sizes share a model, so never a state either.
                assert not set(models) & set(other) - {None}, (first, second)
                assert replay_key != other_replay_key, (first, second)
                assert model_key["sha256"] != other_model_key["sha256"]
    # Within a size, the two-head model alone keeps one state for RWKV-Curve
    # only, neither and the opt-out with both; the Instant-Only model alone
    # one state with or without the opt-out.
    for size in RWKV_MODEL_SIZES:
        assert (
            identities[(size, "curve only")][1]
            == identities[(size, "neither")][1]
            == identities[(size, "both, opt-out")][1]
        )
        assert (
            identities[(size, "instant only")][1]
            == identities[(size, "instant only, opt-out")][1]
        )


def test_family_wiring_rebuilds_each_changed_combination_once(monkeypatch):
    # Of the 18 combinations, exactly those whose models changed since the
    # previous build (the family wiring (SM114)) get a new replay key and
    # rebuild once: every Small and Tiny combination (new two-head and
    # Instant-Only models: SM118 + SM113, the six-layer Tiny (SM130)). Big's
    # keep their state.
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    changed = set()
    for size in RWKV_MODEL_SIZES:
        for case in _USE_CASES:
            before = RwkvRuntimeModels(*_PREVIOUS_RUNTIMES[size][case])
            after = RwkvRuntimeModels(*_SIZE_RUNTIMES[size][case])
            old_key, _ = _resident_identity(monkeypatch, before)
            new_key, _ = _resident_identity(monkeypatch, after)
            assert (old_key != new_key) == (before != after), (size, case)
            if before != after:
                changed.add((size, case))
    assert changed == {
        (size, case) for size in ("sm72e25", "tiny") for case in _USE_CASES
    }


@pytest.mark.parametrize(
    "profile,configs,before,after",
    [
        # Small with RWKV-Instant alone: the Tiny Instant-Only model (SM101)
        # in SM112, the longer Small Instant-Only model (SM113) now.
        (
            {"rwkvModel": "sm72e25"},
            _deck_configs(curve=False, instant=True),
            RwkvRuntimeModels("sm101"),
            RwkvRuntimeModels("sm113"),
        ),
        (
            {"rwkvModel": "sm72e25", "rwkvSingleModel": True},
            _deck_configs(curve=False, instant=True),
            RwkvRuntimeModels("sm101"),
            RwkvRuntimeModels("sm113"),
        ),
        # Small with both schedulers: the default small model (SM63) beside
        # SM98 until SM114, now the show-time two-head cool-down for Small
        # (SM118) beside SM113.
        (
            {"rwkvModel": "sm72e25"},
            _deck_configs(curve=True, instant=True),
            RwkvRuntimeModels("sm72e25", "sm98"),
            RwkvRuntimeModels("sm118", "sm113"),
        ),
        # Small with RWKV-Curve only: SM63 alone until SM114, now SM118 alone.
        (
            {"rwkvModel": "sm72e25"},
            _deck_configs(curve=True, instant=False),
            RwkvRuntimeModels("sm72e25"),
            RwkvRuntimeModels("sm118"),
        ),
        # Small with RWKV-Curve only: the show-time retrain (SM92) alone in
        # the previous build, now SM118 alone.
        (
            {"rwkvModel": "sm72e25"},
            _deck_configs(curve=True, instant=False),
            RwkvRuntimeModels("sm92"),
            RwkvRuntimeModels("sm118"),
        ),
        # Tiny with both schedulers: SM63 beside the Tiny Instant-Only model
        # (SM101) until SM114, now the six-layer Tiny pair (SM130).
        (
            {"rwkvModel": "tiny"},
            _deck_configs(curve=True, instant=True),
            RwkvRuntimeModels("sm72e25", "sm101"),
            RwkvRuntimeModels("sm130th", "sm130io"),
        ),
        # Tiny with both schedulers: the Tiny two-head model (SM106) beside
        # the longer cool-down for Tiny (SM115) in the previous build.
        (
            {"rwkvModel": "tiny"},
            _deck_configs(curve=True, instant=True),
            RwkvRuntimeModels("sm106", "sm115"),
            RwkvRuntimeModels("sm130th", "sm130io"),
        ),
        # Tiny with RWKV-Curve only: SM63 alone until SM114, SM106 alone in
        # the previous build, now the six-layer Tiny two-head model alone.
        (
            {"rwkvModel": "tiny"},
            _deck_configs(curve=True, instant=False),
            RwkvRuntimeModels("sm106"),
            RwkvRuntimeModels("sm130th"),
        ),
        # Tiny with both schedulers and the opt-out: the show-time retrain
        # (SM92) until SM112, now the six-layer Tiny two-head model (SM130).
        (
            {"rwkvModel": "tiny", "rwkvSingleModel": True},
            _deck_configs(curve=True, instant=True),
            RwkvRuntimeModels("sm92"),
            RwkvRuntimeModels("sm130th"),
        ),
    ],
)
def test_states_built_on_the_previous_model_rebuild_once(
    monkeypatch, profile, configs, before, after
):
    # A state cache saved by the previous build carries the previous model's
    # replay key and weights hash: the new build refuses it, rebuilds once
    # from the review history, and then reuses its own state.
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: False,
    )
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_collection_modified", lambda r: 7)
    assert (
        rwkv_scheduler._rwkv_runtime_models_for_profile(_config_owner(configs, profile))
        == after
    )
    old_replay_key, old_model_key = _resident_identity(monkeypatch, before)
    new_replay_key, new_model_key = _resident_identity(monkeypatch, after)
    assert old_replay_key != new_replay_key
    assert old_model_key["sha256"] != new_model_key["sha256"]
    assert not rwkv_scheduler._rwkv_state_cache_model_usable(old_model_key)
    reviewer = SimpleNamespace()
    old_metadata = {"collectionMod": 7, "replayKey": old_replay_key}
    new_metadata = {"collectionMod": 7, "replayKey": new_replay_key}
    assert not rwkv_scheduler._rwkv_state_cache_collection_unchanged(
        reviewer, old_metadata
    )
    # The rebuilt state is reused at the next open: one rebuild only.
    assert rwkv_scheduler._rwkv_state_cache_collection_unchanged(reviewer, new_metadata)
    assert rwkv_scheduler._rwkv_state_cache_model_usable(new_model_key)
    assert _resident_identity(monkeypatch, after) == (new_replay_key, new_model_key)


def test_tiny_models_of_equal_size_never_share_a_model_key(monkeypatch):
    # The Tiny Instant-Only model (SM101) and the Tiny two-head model (SM106)
    # have the same file name and byte size: only the weights hash tells
    # their states apart, and it does.
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    _, sm101 = _resident_identity(monkeypatch, RwkvRuntimeModels("sm101"))
    _, sm106 = _resident_identity(monkeypatch, RwkvRuntimeModels("sm106"))
    assert (sm101["name"], sm101["size"]) == (sm106["name"], sm106["size"])
    assert sm101["sha256"] != sm106["sha256"]
    assert not rwkv_scheduler._rwkv_state_cache_model_usable(sm101)


def _embedded_backend(model, instant_model=None):
    from aqt.rwkv_srs_benchmark import EmbeddedRwkvReviewerBackend

    backend = EmbeddedRwkvReviewerBackend.__new__(EmbeddedRwkvReviewerBackend)
    backend.model_name = model
    backend.instant_model_name = instant_model
    backend.state_precision = "int8"
    return backend


@pytest.mark.parametrize(
    "resident,configs,single,dropped",
    [
        # SM113 alone, and a preset now uses RWKV-Curve (scheduler or fallback).
        (("sm113", None), _deck_configs(curve=True, instant=True), False, True),
        (("sm113", None), _deck_configs(curve=True, instant=True), True, True),
        (
            ("sm113", None),
            _deck_configs(curve=False, instant=True, fallback_curve=True),
            False,
            True,
        ),
        # Still no RWKV-Curve: keep SM113 alone.
        (("sm113", None), _deck_configs(curve=False, instant=True), False, False),
        (("sm113", None), _deck_configs(curve=False, instant=False), False, False),
        # Runtimes with a curve wait for the next profile open as before.
        (("sm118", "sm113"), _deck_configs(curve=False, instant=True), False, False),
        (("sm118", None), _deck_configs(curve=True, instant=True), False, False),
        # Tiny: the six-layer Tiny Instant-Only model (SM130) alone, the same
        # rule.
        (("sm130io", None), _deck_configs(curve=True, instant=True), False, True),
        (("sm130io", None), _deck_configs(curve=True, instant=True), True, True),
        (("sm130io", None), _deck_configs(curve=False, instant=True), False, False),
        (
            ("sm130th", "sm130io"),
            _deck_configs(curve=False, instant=True),
            False,
            False,
        ),
        # Big: SM99 alone, the same rule.
        (("sm99", None), _deck_configs(curve=True, instant=True), False, True),
        (("sm99", None), _deck_configs(curve=True, instant=True), True, True),
        (("sm99", None), _deck_configs(curve=False, instant=True), False, False),
        (("legacy", "sm99"), _deck_configs(curve=False, instant=True), False, False),
    ],
)
def test_instant_only_runtime_reopens_once_a_preset_uses_curve(
    monkeypatch, resident, configs, single, dropped
):
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    backend = _embedded_backend(*resident)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    replace = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "set_reviewer_backend", replace)
    size = next(size for size, models in FAMILY.items() if resident[0] in models)
    owner = _config_owner(configs, {"rwkvModel": size, "rwkvSingleModel": single})
    assert (
        rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner) is dropped
    )
    if dropped:
        replace.assert_called_once_with(None)
    else:
        replace.assert_not_called()


@pytest.mark.parametrize("size", RWKV_MODEL_SIZES)
@pytest.mark.parametrize("single", [False, True])
def test_instant_alone_reopens_once_a_preset_uses_curve(monkeypatch, size, single):
    # Each size runs its own Instant-Only model alone for RWKV-Instant
    # without RWKV-Curve; an RWKV-Curve preset swaps in the size's own
    # runtime (its pair, or its two-head model alone with the opt-out).
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    two_head, instant = FAMILY[size]
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _embedded_backend(instant))
    replace = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "set_reviewer_backend", replace)
    profile = {"rwkvModel": size, "rwkvSingleModel": single}
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(
        _config_owner(_deck_configs(curve=False, instant=True), profile)
    ) == RwkvRuntimeModels(instant)
    assert not rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(
        _config_owner(_deck_configs(curve=False, instant=True), profile)
    )
    owner = _config_owner(_deck_configs(curve=True, instant=True), profile)
    assert rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner)
    replace.assert_called_once_with(None)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert rwkv_scheduler._rwkv_runtime_models_for_profile(owner) == (
        RwkvRuntimeModels(two_head) if single else RwkvRuntimeModels(two_head, instant)
    )


def test_runtime_refresh_ignores_other_backends(monkeypatch):
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(model_name="sm98", instant_model_name=None),
    )
    replace = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "set_reviewer_backend", replace)
    owner = _config_owner(_deck_configs(curve=True, instant=True))
    assert not rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert not rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner)
    replace.assert_not_called()


def _set_english():
    import anki.lang

    anki.lang.set_lang("en")


@pytest.mark.parametrize("compare", [False, True])
def test_workload_simulation_without_curve_model_explains_itself(monkeypatch, compare):
    from anki import scheduler_pb2

    _set_english()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _embedded_backend("sm98"))
    load = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "load_rwkv_state_cache", load)
    request = scheduler_pb2.SimulateFsrsReviewRequest()
    if compare:
        request.rwkv_comparison.SetInParent()
    simulate = (
        rwkv_scheduler.compare_rwkv_and_fsrs_workload
        if compare
        else rwkv_scheduler.simulate_rwkv_workload
    )
    with pytest.raises(rwkv_scheduler.RwkvWorkloadUnavailableError) as raised:
        simulate(request, mw=SimpleNamespace())
    assert str(raised.value) == (
        "RWKV workload simulation needs RWKV-Curve. Turn on RWKV-Curve in a "
        "preset (as its scheduler or as RWKV-Instant's fallback) and save to "
        "simulate RWKV workload."
    )
    # Checked before any state is loaded or built.
    load.assert_not_called()
    with pytest.raises(rwkv_scheduler.RwkvWorkloadUnavailableError):
        rwkv_scheduler.simulate_rwkv_workload_bytes(request.SerializeToString())


def test_workload_start_reports_missing_curve_model_before_starting(monkeypatch):
    _set_english()
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _embedded_backend("sm98"))
    started = MagicMock()
    monkeypatch.setattr(rwkv_scheduler.threading, "Thread", started)
    with pytest.raises(
        rwkv_scheduler.RwkvWorkloadUnavailableError, match="needs RWKV-Curve"
    ):
        rwkv_scheduler.start_rwkv_workload_bytes(b"")
    started.assert_not_called()


@pytest.mark.parametrize(
    "resident,available",
    [
        (("sm98", None), False),
        (("sm101", None), False),
        (("sm106", None), True),
        (("sm72e25", "sm98"), True),
        (("sm72e25", "sm101"), True),
        (("sm92", "sm98"), True),
        (("sm106", "sm101"), True),
        (("sm118", "sm113"), True),
        (("sm130th", "sm130io"), True),
        (("sm130th", None), True),
        (("sm130io", None), False),
        (("sm72e25", None), True),
        (("sm92", None), True),
        (("legacy", None), True),
        (("sm99", None), False),
        (("legacy", "sm99"), True),
    ],
)
def test_deck_options_learns_workload_availability(monkeypatch, resident, available):
    from flask import Flask

    from anki.generic_pb2 import Bool
    from aqt import mediasrv

    monkeypatch.setattr(
        rwkv_scheduler, "_reviewer_backend", _embedded_backend(*resident)
    )
    assert "rwkvWorkloadAvailable" in mediasrv.post_handlers
    with Flask(__name__).test_request_context(data=b""):
        result = Bool.FromString(mediasrv.rwkv_workload_available())
    assert result.val is available
