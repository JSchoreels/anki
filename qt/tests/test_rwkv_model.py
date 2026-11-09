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
    RWKV_MODEL_FILES,
    RWKV_MODELS,
    RwkvRuntimeModels,
    model_for_profile,
    runtime_models,
    specialized_instant_model_for_profile,
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

    profile = {"rwkvModel": "sm36", "rwkvStatePrecision": "int8", "other": 42}
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
        request.rwkv_profile_settings.specialized_instant_model = True
    mediasrv._on_update_deck_configs_success(request, close_on_success=False)
    assert profile == {
        "rwkvModel": "legacy" if should_save else "sm36",
        "rwkvStatePrecision": "cards_int4_notes_int8" if should_save else "int8",
        **({"rwkvSpecializedInstantModel": True} if should_save else {}),
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
    "instant_used,curve_used,specialized,expected",
    [
        # Only RWKV-Instant: the show-time retrain (SM92) alone, no toggle needed.
        (True, False, False, RwkvRuntimeModels("sm92")),
        (True, False, True, RwkvRuntimeModels("sm92")),
        # Both schedulers: the shared release (SM63) unless the toggle is on.
        (True, True, False, RwkvRuntimeModels("sm72e25")),
        (True, True, True, RwkvRuntimeModels("sm72e25", "sm92")),
        # Only RWKV-Curve, or neither: the release alone.
        (False, True, False, RwkvRuntimeModels("sm72e25")),
        (False, True, True, RwkvRuntimeModels("sm72e25")),
        (False, False, True, RwkvRuntimeModels("sm72e25")),
    ],
)
def test_runtime_models_pick_the_instant_model_per_scheduler(
    instant_used, curve_used, specialized, expected
):
    models = runtime_models(
        "sm72e25",
        instant_used=instant_used,
        curve_used=curve_used,
        specialized_instant_model=specialized,
    )
    assert models == expected
    if instant_used:
        assert models.instant_scheduler_model == (
            "sm92" if specialized or not curve_used else "sm72e25"
        )
    # RWKV-Curve keeps the release state whenever it is used; the show-time
    # retrain (SM92) alone has a trained curve, as for a profile picking it.
    assert models.curve_model == models.model
    assert "sm57" not in models


def test_instant_only_model_stays_selectable():
    # The instant-only small model (SM57) stays bundled but unused by default;
    # alone it never serves a curve.
    assert RWKV_INSTANT_MODEL == "sm92"
    assert RWKV_INSTANT_ONLY_MODEL == "sm57"
    assert RWKV_INSTANT_ONLY_MODEL in RWKV_MODEL_FILES
    assert RwkvRuntimeModels("sm57").curve_model is None
    assert RwkvRuntimeModels("sm72e25", "sm57").curve_model == "sm72e25"


@pytest.mark.parametrize("model", [m for m in RWKV_MODELS if m != "sm72e25"])
@pytest.mark.parametrize("instant_used", [False, True])
@pytest.mark.parametrize("curve_used", [False, True])
def test_runtime_models_keep_other_models_shared(model, instant_used, curve_used):
    # SM92, Legacy and the older small models serve both schedulers alone.
    assert runtime_models(
        model,
        instant_used=instant_used,
        curve_used=curve_used,
        specialized_instant_model=True,
    ) == RwkvRuntimeModels(model)
    assert "sm57" not in RWKV_MODELS


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, False),
        ({}, False),
        ({"rwkvSpecializedInstantModel": False}, False),
        ({"rwkvSpecializedInstantModel": "yes"}, False),
        ({"rwkvSpecializedInstantModel": True}, True),
    ],
)
def test_specialized_instant_model_toggle_defaults_off(profile, expected):
    assert specialized_instant_model_for_profile(profile) is expected


def _deck_configs(*, curve, instant):
    return [
        {"id": 1, "rwkvReviewEnabled": curve, "rwkvReviewInstantOrderEnabled": False},
        {"id": 2, "rwkvReviewEnabled": False, "rwkvReviewInstantOrderEnabled": instant},
    ]


@pytest.mark.parametrize(
    "curve,instant,specialized,expected",
    [
        (False, True, False, ("sm92", None)),
        (True, True, False, ("sm72e25", None)),
        (True, True, True, ("sm72e25", "sm92")),
        (True, False, True, ("sm72e25", None)),
    ],
)
def test_profile_presets_and_toggle_reach_the_native_backend(
    monkeypatch, curve, instant, specialized, expected
):
    import aqt
    from aqt import rwkv_srs_benchmark

    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    configs = _deck_configs(curve=curve, instant=instant)
    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            pm=SimpleNamespace(
                profile={
                    "rwkvModel": "sm72e25",
                    "rwkvSpecializedInstantModel": specialized,
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
        assert Path(created["instant_model_path"]).parent.name == "sm92"
    # The cache identity follows the runtime models until the next profile open.
    assert rwkv_scheduler._rwkv_runtime_models() == RwkvRuntimeModels(*expected)


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
    for instant_model in (None, "sm57", "sm92"):
        monkeypatch.setattr(
            rwkv_scheduler,
            "_reviewer_backend",
            SimpleNamespace(
                model_name="sm72e25",
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
    # Single, paired with SM57 and paired with SM92 states never share a key.
    assert len(set(keys)) == 3
