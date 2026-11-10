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
    RWKV_INSTANT_MODEL,
    RWKV_INSTANT_ONLY_MODEL,
    RWKV_INSTANT_ONLY_MODELS,
    RWKV_MODEL_FILES,
    RWKV_MODELS,
    RWKV_SPECIALIZED_INSTANT_MODEL,
    RwkvRuntimeModels,
    model_for_profile,
    runtime_models,
    single_model_for_profile,
)


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


@pytest.mark.parametrize("active_model", RWKV_MODELS)
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
    assert rwkv_scheduler._rwkv_model() == saved_model
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
    ],
)
@pytest.mark.parametrize("active_model", RWKV_MODELS)
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
    assert backend.model_name == (model or "sm72e25")
    assert backend.state_precision == (
        precision if precision not in (None, "int4") else "cards_int4_notes_int8"
    )


@pytest.mark.parametrize("active_model", RWKV_MODELS)
@pytest.mark.parametrize("saved_model", RWKV_MODELS)
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
        # fallback): the show-time instant-only model, stage 2 (SM98) alone,
        # an instant-only runtime, whatever the opt-out.
        (True, False, False, RwkvRuntimeModels("sm98")),
        (True, False, True, RwkvRuntimeModels("sm98")),
        # Both: by default SM98 beside the release, which keeps curve,
        # intervals, S90 and answer time; the opt-out shares the release.
        (True, True, False, RwkvRuntimeModels("sm72e25", "sm98")),
        (True, True, True, RwkvRuntimeModels("sm72e25")),
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
            "sm72e25" if curve_used and single else "sm98"
        )
    # A curve exists exactly when a preset uses RWKV-Curve or nothing uses
    # RWKV-Instant; the show-time retrain (SM92) and the instant-only small
    # model (SM57) are never picked automatically.
    if instant_used and not curve_used:
        assert models.curve_model is None
        assert models.model in RWKV_INSTANT_ONLY_MODELS
    else:
        assert models.curve_model == models.model == "sm72e25"
    assert "sm57" not in models
    assert "sm92" not in models


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


def test_specialized_instant_model_is_sm98_and_instant_only():
    # The show-time instant-only model, stage 2 (SM98): bundled, never a
    # profile's model choice, and alone it serves no curve.
    assert RWKV_SPECIALIZED_INSTANT_MODEL == "sm98"
    assert RWKV_SPECIALIZED_INSTANT_MODEL in RWKV_INSTANT_ONLY_MODELS
    assert RWKV_MODEL_FILES["sm98"] == Path("sm98/weights.f32.bin")
    assert "sm98" not in RWKV_MODELS
    assert RwkvRuntimeModels("sm98").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm98").curve_model == "sm72e25"


@pytest.mark.parametrize("model", [m for m in RWKV_MODELS if m != "sm72e25"])
@pytest.mark.parametrize("instant_used", [False, True])
@pytest.mark.parametrize("curve_used", [False, True])
def test_runtime_models_keep_other_models_shared(model, instant_used, curve_used):
    # SM92, Legacy and the older small models serve both schedulers alone.
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
        else RwkvRuntimeModels("sm98")
    )


@pytest.mark.parametrize(
    "curve,instant,fallback_curve,single,expected",
    [
        # RWKV-Instant without RWKV-Curve: SM98 alone, opt-out ignored.
        (False, True, False, False, ("sm98", None)),
        (False, True, False, True, ("sm98", None)),
        # RWKV-Curve as a preset's scheduler, or as RWKV-Instant's fallback.
        (True, True, False, False, ("sm72e25", "sm98")),
        (True, True, False, True, ("sm72e25", None)),
        (False, False, True, False, ("sm72e25", "sm98")),
        (False, False, True, True, ("sm72e25", None)),
        (False, True, True, False, ("sm72e25", "sm98")),
        # RWKV-Curve only, or neither.
        (True, False, False, False, ("sm72e25", None)),
        (True, False, False, True, ("sm72e25", None)),
        (False, False, False, False, ("sm72e25", None)),
    ],
)
def test_profile_presets_and_opt_out_reach_the_native_backend(
    monkeypatch, curve, instant, fallback_curve, single, expected
):
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
                    "rwkvModel": "sm72e25",
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
    assert Path(created["model_path"]).parent.name == expected[0]
    if expected[1] is None:
        assert created["instant_model_path"] is None
    else:
        assert Path(created["instant_model_path"]).parent.name == "sm98"
    # The cache identity follows the runtime models until the next profile open.
    assert rwkv_scheduler._rwkv_runtime_models() == RwkvRuntimeModels(*expected)
    # Workload simulation needs a curve model: not with SM98 alone.
    assert rwkv_scheduler.rwkv_workload_simulation_available() == (
        expected[0] != "sm98"
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
        # The show-time instant-only model, stage 2 (SM98) alone, and the
        # show-time retrain (SM92) alone.
        ("sm98", None),
        ("sm92", None),
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
    # Single, SM98 alone and paired with SM57, SM92 or SM98 states never
    # share a key: a switch between the pair and SM98 alone rebuilds once.
    assert len(set(keys)) == 6


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
    owner = _config_owner(configs, {"rwkvModel": "sm72e25", "rwkvSingleModel": single})
    assert (
        rwkv_scheduler.refresh_rwkv_runtime_models_after_preset_change(owner) is dropped
    )
    if dropped:
        replace.assert_called_once_with(None)
    else:
        replace.assert_not_called()


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
        (("sm72e25", "sm98"), True),
        (("sm72e25", None), True),
        (("sm92", None), True),
        (("legacy", None), True),
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
