# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import rwkv_scheduler
from aqt.profiles import ProfileManager, profileConf
from aqt.rwkv_model import (
    RWKV_BIG_INSTANT_MODEL,
    RWKV_CHOICE_INSTANT_ALONE_MODELS,
    RWKV_CHOICE_INSTANT_MODELS,
    RWKV_CHOICE_SINGLE_MODELS,
    RWKV_INSTANT_ALONE_MODEL,
    RWKV_INSTANT_MODEL,
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_INSTANT_ONLY_MODELS,
    RWKV_MODEL_BIG,
    RWKV_MODEL_FILES,
    RWKV_MODEL_SIZES,
    RWKV_MODEL_SMALL,
    RWKV_MODEL_TINY,
    RWKV_MODELS,
    RWKV_SPECIALIZED_INSTANT_MODEL,
    RWKV_TINY_INSTANT_MODEL,
    RWKV_TINY_SINGLE_MODEL,
    RwkvRuntimeModels,
    curve_model_for_choice,
    model_for_profile,
    runtime_models,
    single_model_for_profile,
)

# Profile values that are also one runtime model key (every choice but Tiny,
# which runtime_models() resolves to the default small model and SM101).
RUNTIME_MODEL_KEYS = tuple(model for model in RWKV_MODELS if model != RWKV_MODEL_TINY)


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
        ({"rwkvModel": "sm92"}, "sm92"),
        ({"rwkvModel": "tiny"}, "tiny"),
        # Model keys that are never a profile choice fall back to Small.
        ({"rwkvModel": "sm98"}, "sm72e25"),
        ({"rwkvModel": "sm101"}, "sm72e25"),
        ({"rwkvModel": "sm106"}, "sm72e25"),
    ],
)
def test_model_for_profile_defaults_to_sm72e25_and_preserves_explicit_choices(
    profile, expected
):
    assert model_for_profile(profile) == expected


@pytest.mark.parametrize("model", [None, *RWKV_MODELS])
@pytest.mark.parametrize(
    "precision", [None, "f32", "int8", "int4", "cards_int4_notes_int8"]
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
            pm=SimpleNamespace(profile=profile), col=SimpleNamespace(_backend=backend)
        ),
    )
    with Flask(__name__).test_request_context(data=b""):
        result = DeckConfigsForUpdate.FromString(mediasrv.get_deck_configs_for_update())
    assert result.fsrs
    assert result.rwkv_profile_settings.model == (model or "sm72e25")
    # Full int4 was removed; a stored "int4" falls back to the default.
    assert result.rwkv_profile_settings.state_precision == (
        precision if precision not in (None, "int4") else "cards_int4_notes_int8"
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
        request.rwkv_profile_settings.state_precision = "cards_int4_notes_int8"
        request.rwkv_profile_settings.single_model = True
    mediasrv._on_update_deck_configs_success(request, close_on_success=False)
    assert profile == {
        "rwkvModel": "legacy" if should_save else "sm36",
        "rwkvStatePrecision": "cards_int4_notes_int8" if should_save else "int8",
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


@pytest.mark.parametrize("active_model", RUNTIME_MODEL_KEYS)
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
        ("sm72e25", "sm72e25/weights.f32.bin"),
        ("sm92", "sm92/weights.f32.bin"),
        # Tiny without RWKV-Instant in use: the default small model alone.
        ("tiny", "sm72e25/weights.f32.bin"),
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
        (None, "sm72e25"),
        ("legacy", "legacy"),
        ("sm40", "sm40"),
        ("sm36", "sm36"),
        ("sm55", "sm55"),
        ("sm56", "sm56"),
        ("sm72", "sm72"),
        ("sm72e20", "sm72e20"),
        ("sm72e25", "sm72e25"),
        ("sm92", "sm92"),
        ("tiny", "sm72e25"),
    ],
)
@pytest.mark.parametrize("active_model", RUNTIME_MODEL_KEYS)
@pytest.mark.parametrize(
    "saved_precision, replace_for_precision",
    [
        (None, True),
        ("f32", True),
        ("int8", False),
        ("int4", True),
        ("cards_int4_notes_int8", True),
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
    "precision", [None, "f32", "int8", "int4", "cards_int4_notes_int8"]
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
    # No preset uses RWKV-Instant here: Tiny runs the default small model.
    assert backend.model_name == curve_model_for_choice(model or "sm72e25")
    assert backend.state_precision == (
        precision if precision not in (None, "int4") else "cards_int4_notes_int8"
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
        # fallback): the Tiny Instant-Only model (SM101) alone, an
        # instant-only runtime, whatever the opt-out (SM98 until SM112).
        (True, False, False, RwkvRuntimeModels("sm101")),
        (True, False, True, RwkvRuntimeModels("sm101")),
        # Both: by default SM98 beside the release, which keeps curve,
        # intervals, S90 and answer time; the opt-out uses SM92 alone.
        (True, True, False, RwkvRuntimeModels("sm72e25", "sm98")),
        (True, True, True, RwkvRuntimeModels("sm92")),
        # Only RWKV-Curve, or neither: the release alone.
        (False, True, False, RwkvRuntimeModels("sm72e25")),
        (False, True, True, RwkvRuntimeModels("sm72e25")),
        (False, False, False, RwkvRuntimeModels("sm72e25")),
        (False, False, True, RwkvRuntimeModels("sm72e25")),
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
            ("sm92" if single else "sm98") if curve_used else "sm101"
        )
    # A curve exists exactly when a preset uses RWKV-Curve or nothing uses
    # RWKV-Instant. The show-time retrain (SM92) is picked only by the
    # one-model opt-out with both schedulers; the instant-only small model
    # (SM57) is never picked automatically.
    if instant_used and not curve_used:
        assert models.curve_model is None
        assert models.model in RWKV_INSTANT_ONLY_MODELS
    elif instant_used and single:
        assert models.curve_model == models.model == "sm92"
    else:
        assert models.curve_model == models.model == "sm72e25"
    assert "sm57" not in models
    assert ("sm92" in models) == (instant_used and curve_used and single)


def test_instant_only_model_stays_selectable():
    # The instant-only small model (SM57) stays bundled but unused by default;
    # alone it never serves a curve. The show-time retrain (SM92) stays
    # bundled and selectable as a profile model.
    assert RWKV_INSTANT_MODEL == "sm92"
    assert RWKV_INSTANT_MODEL in RWKV_MODELS
    assert RWKV_MODEL_FILES[RWKV_INSTANT_MODEL] == Path("sm92/weights.f32.bin")
    assert RWKV_INSTANT_ONLY_MODEL == "sm57"
    assert RWKV_INSTANT_ONLY_MODEL in RWKV_MODEL_FILES
    assert RwkvRuntimeModels("sm57").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm57").curve_model == "sm72e25"


def test_tiny_instant_model_is_sm101_and_instant_only():
    # The smaller instant-only model (SM101): bundled, never a profile's model
    # key (the profile stores "tiny"), alone it serves no curve.
    assert RWKV_TINY_INSTANT_MODEL == "sm101"
    assert RWKV_TINY_INSTANT_MODEL in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm101"] == Path("sm101/weights.f32.bin")
    assert "sm101" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm101").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm101").curve_model == "sm72e25"
    assert RwkvRuntimeModels("sm72e25", "sm101").instant_scheduler_model == "sm101"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    assert (root / RWKV_MODEL_FILES["sm101"]).exists()
    assert (root / "sm101" / "architecture.json").exists()


def test_tiny_single_model_is_sm106_and_a_full_model():
    # The Tiny two-head model (SM106): bundled, never a profile's model key,
    # a full model (it serves the curve) on the tiny plan, Tiny's one model
    # under the opt-out.
    assert RWKV_TINY_SINGLE_MODEL == "sm106"
    assert RWKV_CHOICE_SINGLE_MODELS[RWKV_MODEL_TINY] == "sm106"
    assert RWKV_TINY_SINGLE_MODEL not in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm106"] == Path("sm106/weights.f32.bin")
    assert "sm106" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm106").curve_model == "sm106"
    assert RwkvRuntimeModels("sm106").instant_scheduler_model == "sm106"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    for name in ("weights.f32.bin", "weights.int8.bin", "architecture.json"):
        assert (root / "sm106" / name).exists()
    # The tiny architecture and file sizes of the Tiny Instant-Only model
    # (SM101), the same ARWKVWEIGHTS1 container; no answer-time head.
    assert (root / "sm106" / "architecture.json").read_bytes() == (
        root / "sm101" / "architecture.json"
    ).read_bytes()
    for name in ("weights.f32.bin", "weights.int8.bin"):
        assert (root / "sm106" / name).stat().st_size == (
            root / "sm101" / name
        ).stat().st_size
    with open(root / RWKV_MODEL_FILES["sm106"], "rb") as file:
        assert file.read(13) == b"ARWKVWEIGHTS1"
    assert not (root / "sm106" / "time_head.f32.bin").exists()


def test_instant_alone_model_is_tiny_except_for_big():
    # The user's decision of 2026-11-12: RWKV-Instant without RWKV-Curve runs
    # the Tiny Instant-Only model (SM101) whatever the size, except Big,
    # which keeps the show-time instant-only big model (SM99).
    assert RWKV_INSTANT_ALONE_MODEL == RWKV_TINY_INSTANT_MODEL == "sm101"
    assert RWKV_CHOICE_INSTANT_ALONE_MODELS == {RWKV_MODEL_BIG: "sm99"}
    for size, expected in (
        ("legacy", "sm99"),
        ("sm72e25", "sm101"),
        ("tiny", "sm101"),
    ):
        for single in (False, True):
            assert runtime_models(
                size, instant_used=True, curve_used=False, single_model=single
            ) == RwkvRuntimeModels(expected)
    # The pairs keep their own RWKV-Instant model: Small stays on SM98.
    assert RWKV_CHOICE_INSTANT_MODELS[RWKV_MODEL_SMALL] == "sm98"


def test_big_instant_model_is_sm99_and_instant_only():
    # The show-time instant-only big model (SM99): bundled, never a profile's
    # model key (the profile stores "legacy"), alone it serves no curve.
    assert RWKV_BIG_INSTANT_MODEL == "sm99"
    assert RWKV_BIG_INSTANT_MODEL in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_CHOICE_INSTANT_MODELS[RWKV_MODEL_BIG] == "sm99"
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
    # Only Tiny is not itself a model key.
    assert [curve_model_for_choice(m) for m in RWKV_MODEL_SIZES] == [
        "legacy",
        "sm72e25",
        "sm72e25",
    ]
    assert all(curve_model_for_choice(m) == m for m in RUNTIME_MODEL_KEYS)


# The three sizes x the schedulers in use x the one-model opt-out.
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
    # Small: the Tiny Instant-Only model (SM101) alone for RWKV-Instant
    # without RWKV-Curve (SM112; SM98 before), the default small model (SM63)
    # + SM98 pair for both, the show-time retrain (SM92) alone with the
    # opt-out, SM63 alone otherwise.
    "sm72e25": {
        "instant only": ("sm101", None),
        "both": ("sm72e25", "sm98"),
        "curve only": ("sm72e25", None),
        "neither": ("sm72e25", None),
        "both, opt-out": ("sm92", None),
        "instant only, opt-out": ("sm101", None),
    },
    # Tiny: SM101 in the pair beside SM63 (SM63 is better on the curve than
    # the Tiny two-head model (SM106)), SM106 alone with the opt-out (SM112;
    # SM92 before), SM101 alone and SM63 alone as for Small.
    "tiny": {
        "instant only": ("sm101", None),
        "both": ("sm72e25", "sm101"),
        "curve only": ("sm72e25", None),
        "neither": ("sm72e25", None),
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
    # The curve never comes from an instant-only model, and there is one
    # exactly when a preset uses RWKV-Curve or nothing uses RWKV-Instant.
    if instant_used and not curve_used:
        assert models.curve_model is None
    elif size != "legacy" and instant_used and single:
        # The opt-out with both schedulers: the show-time retrain (SM92) for
        # Small, the Tiny two-head model (SM106) for Tiny.
        expected = "sm106" if size == "tiny" else "sm92"
        assert models.curve_model == models.model == expected
    else:
        assert models.curve_model == curve_model_for_choice(size)
    small = runtime_models(
        "sm72e25",
        instant_used=instant_used,
        curve_used=curve_used,
        single_model=single,
    )
    if size == "tiny":
        # Tiny and Small run the same runtime (and keep the same state)
        # without RWKV-Instant and with RWKV-Instant alone; with both
        # schedulers Tiny has its own pair and its own one model.
        assert (models == small) == (not instant_used or not curve_used)


@pytest.mark.parametrize(
    "stored,choice,runtime_both",
    [
        # Big pairs the show-time big model (SM91s2) with the show-time
        # instant-only big model (SM99).
        ("legacy", "legacy", ("legacy", "sm99")),
        # Unset, the old default key and unknown values: Small.
        (None, "sm72e25", ("sm72e25", "sm98")),
        ("sm72e25", "sm72e25", ("sm72e25", "sm98")),
        ("unknown", "sm72e25", ("sm72e25", "sm98")),
        ("tiny", "tiny", ("sm72e25", "sm101")),
        # Older small models keep running alone until a Deck Options save
        # stores Small (ts/routes/deck-options/lib.ts); SM92 is preserved.
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


def test_specialized_instant_model_is_sm98_and_instant_only():
    # The show-time instant-only model, stage 2 (SM98): bundled, never a
    # profile's model choice, and alone it serves no curve.
    assert RWKV_SPECIALIZED_INSTANT_MODEL == "sm98"
    assert RWKV_SPECIALIZED_INSTANT_MODEL in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm98"] == Path("sm98/weights.f32.bin")
    assert "sm98" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm98").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm98").curve_model == "sm72e25"


@pytest.mark.parametrize("model", [m for m in RWKV_MODELS if m not in RWKV_MODEL_SIZES])
@pytest.mark.parametrize("instant_used", [False, True])
@pytest.mark.parametrize("curve_used", [False, True])
def test_runtime_models_keep_other_models_shared(model, instant_used, curve_used):
    # SM92 and the older small models serve both schedulers alone.
    assert runtime_models(
        model,
        instant_used=instant_used,
        curve_used=curve_used,
        single_model=False,
    ) == RwkvRuntimeModels(model)
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
        RwkvRuntimeModels("sm72e25", "sm98")
        if curve_enabled
        else RwkvRuntimeModels("sm101")
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
    for size, instant_model, single_model in (
        ("sm72e25", "sm98", "sm92"),
        ("tiny", "sm101", "sm106"),
    ):
        profile = {"rwkvModel": size}
        curve_fallback.mw.pm.profile = profile
        fsrs_fallback.mw.pm.profile = profile
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            curve_fallback.mw
        ) == RwkvRuntimeModels("sm72e25", instant_model)
        # RWKV-Instant alone: the Tiny Instant-Only model (SM101) for both.
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            fsrs_fallback.mw
        ) == RwkvRuntimeModels("sm101")
        profile["rwkvSingleModel"] = True
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            curve_fallback.mw
        ) == RwkvRuntimeModels(single_model)
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            fsrs_fallback.mw
        ) == RwkvRuntimeModels("sm101")
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
        # RWKV-Instant without RWKV-Curve: the Tiny Instant-Only model
        # (SM101) alone, opt-out ignored.
        (False, True, False, False, ("sm101", None)),
        (False, True, False, True, ("sm101", None)),
        # RWKV-Curve as a preset's scheduler, or as RWKV-Instant's fallback.
        (True, True, False, False, ("sm72e25", "sm98")),
        (True, True, False, True, ("sm92", None)),
        (False, False, True, False, ("sm72e25", "sm98")),
        (False, False, True, True, ("sm92", None)),
        (False, True, True, False, ("sm72e25", "sm98")),
        # RWKV-Curve only, or neither.
        (True, False, False, False, ("sm72e25", None)),
        (True, False, False, True, ("sm72e25", None)),
        (False, False, False, False, ("sm72e25", None)),
    ],
)
@pytest.mark.parametrize(
    "size,instant_model", [("sm72e25", "sm98"), ("tiny", "sm101"), ("legacy", "sm99")]
)
def test_profile_presets_and_opt_out_reach_the_native_backend(
    monkeypatch, curve, instant, fallback_curve, single, expected, size, instant_model
):
    # The rows are Small's; Tiny runs SM101 wherever Small runs SM98 and the
    # Tiny two-head model (SM106) where Small runs the show-time retrain
    # (SM92). Big runs SM99 in place of SM98 and SM101, and the show-time big
    # model (SM91s2) where Small runs the release (SM63) or SM92.
    if size == "legacy":
        big = {"sm98": "sm99", "sm101": "sm99", "sm72e25": "legacy", "sm92": "legacy"}
        expected = tuple(big.get(m, m) for m in expected)
    elif size == "tiny":
        tiny = {"sm98": "sm101", "sm92": "sm106"}
        expected = tuple(tiny.get(m, m) for m in expected)
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
    # The cache identity follows the runtime models until the next profile open.
    assert rwkv_scheduler._rwkv_runtime_models() == RwkvRuntimeModels(*expected)
    # Workload simulation needs a curve model: not with SM101 or SM99 alone.
    assert rwkv_scheduler.rwkv_workload_simulation_available() == (
        expected[0] not in ("sm101", "sm99")
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
        # Tiny's pair: the smaller instant-only model (SM101) beside the
        # release.
        ("sm72e25", "sm101"),
        # The show-time instant-only model, stage 2 (SM98) alone, SM101
        # alone, and the show-time retrain (SM92) alone.
        ("sm98", None),
        ("sm101", None),
        ("sm92", None),
        # The Tiny two-head model (SM106) alone, Tiny's one model.
        ("sm106", None),
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
    # Single, SM98 alone, SM101 alone, SM106 alone, SM99 alone and paired with
    # SM57, SM92, SM98, SM101 or SM99 states never share a key: a switch
    # between the sizes, or between a pair and its instant model alone,
    # rebuilds once.
    assert len(set(keys)) == 12


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
    # SM112: every size x schedulers x opt-out combination. Two combinations
    # keep the same saved state exactly when they run the same models;
    # otherwise the replay key differs and the state rebuilds once.
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
    for first, (models, replay_key, model_key) in identities.items():
        assert model_key is not None, first
        for second, (other, other_replay_key, _) in identities.items():
            assert (replay_key == other_replay_key) == (models == other), (
                first,
                second,
            )
    # Small and Tiny share RWKV-Instant alone (SM101) and RWKV-Curve alone
    # (SM63): switching the size there keeps the state.
    for case in ("instant only", "instant only, opt-out", "curve only", "neither"):
        assert identities[("sm72e25", case)][1] == identities[("tiny", case)][1]
    for case in ("both", "both, opt-out"):
        assert identities[("sm72e25", case)][1] != identities[("tiny", case)][1]
    # Big keeps its own RWKV-Instant model alone.
    assert (
        identities[("legacy", "instant only")][1]
        != (identities[("sm72e25", "instant only")][1])
    )


@pytest.mark.parametrize(
    "profile,configs,before,after",
    [
        # Small with RWKV-Instant alone: the Small Instant-Only model (SM98)
        # until SM112, now the Tiny Instant-Only model (SM101).
        (
            {"rwkvModel": "sm72e25"},
            _deck_configs(curve=False, instant=True),
            RwkvRuntimeModels("sm98"),
            RwkvRuntimeModels("sm101"),
        ),
        (
            {"rwkvModel": "sm72e25", "rwkvSingleModel": True},
            _deck_configs(curve=False, instant=True),
            RwkvRuntimeModels("sm98"),
            RwkvRuntimeModels("sm101"),
        ),
        # Tiny with both schedulers and the opt-out: the show-time retrain
        # (SM92) until SM112, now the Tiny two-head model (SM106).
        (
            {"rwkvModel": "tiny", "rwkvSingleModel": True},
            _deck_configs(curve=True, instant=True),
            RwkvRuntimeModels("sm92"),
            RwkvRuntimeModels("sm106"),
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
        # SM98 alone, and a preset now uses RWKV-Curve (scheduler or fallback).
        (("sm98", None), _deck_configs(curve=True, instant=True), False, True),
        (("sm98", None), _deck_configs(curve=True, instant=True), True, True),
        (
            ("sm98", None),
            _deck_configs(curve=False, instant=True, fallback_curve=True),
            False,
            True,
        ),
        # Still no RWKV-Curve: keep SM98 alone.
        (("sm98", None), _deck_configs(curve=False, instant=True), False, False),
        (("sm98", None), _deck_configs(curve=False, instant=False), False, False),
        # Runtimes with a curve wait for the next profile open as before.
        (("sm72e25", "sm98"), _deck_configs(curve=False, instant=True), False, False),
        (("sm72e25", None), _deck_configs(curve=True, instant=True), False, False),
        # Tiny: SM101 alone, the same rule.
        (("sm101", None), _deck_configs(curve=True, instant=True), False, True),
        (("sm101", None), _deck_configs(curve=True, instant=True), True, True),
        (("sm101", None), _deck_configs(curve=False, instant=True), False, False),
        (("sm72e25", "sm101"), _deck_configs(curve=False, instant=True), False, False),
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
    size = (
        "tiny" if "sm101" in resident else "legacy" if "sm99" in resident else "sm72e25"
    )
    owner = _config_owner(configs, {"rwkvModel": size, "rwkvSingleModel": single})
    assert (
        rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner) is dropped
    )
    if dropped:
        replace.assert_called_once_with(None)
    else:
        replace.assert_not_called()


@pytest.mark.parametrize("size", ["sm72e25", "tiny"])
@pytest.mark.parametrize("single", [False, True])
def test_small_and_tiny_instant_alone_reopen_once_a_preset_uses_curve(
    monkeypatch, size, single
):
    # Small and Tiny both run the Tiny Instant-Only model (SM101) alone for
    # RWKV-Instant without RWKV-Curve; an RWKV-Curve preset swaps in the
    # size's own runtime (its pair, or its one model with the opt-out).
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _embedded_backend("sm101"))
    replace = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "set_reviewer_backend", replace)
    profile = {"rwkvModel": size, "rwkvSingleModel": single}
    assert not rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(
        _config_owner(_deck_configs(curve=False, instant=True), profile)
    )
    owner = _config_owner(_deck_configs(curve=True, instant=True), profile)
    assert rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner)
    replace.assert_called_once_with(None)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    expected = {
        ("sm72e25", False): RwkvRuntimeModels("sm72e25", "sm98"),
        ("sm72e25", True): RwkvRuntimeModels("sm92"),
        ("tiny", False): RwkvRuntimeModels("sm72e25", "sm101"),
        ("tiny", True): RwkvRuntimeModels("sm106"),
    }
    assert (
        rwkv_scheduler._rwkv_runtime_models_for_profile(owner)
        == (expected[(size, single)])
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
