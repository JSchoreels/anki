# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import rwkv_scheduler
from aqt.profiles import ProfileManager, profileConf
from aqt.rwkv_model import RWKV_MODELS, model_for_profile


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, "sm56"),
        ({}, "sm56"),
        ({"rwkvModel": "unknown"}, "sm56"),
        ({"rwkvModel": None}, "sm56"),
        ({"rwkvModel": 36}, "sm56"),
        ({"rwkvModel": "legacy"}, "legacy"),
        ({"rwkvModel": "sm40"}, "sm40"),
        ({"rwkvModel": "sm36"}, "sm36"),
        ({"rwkvModel": "sm55"}, "sm55"),
        ({"rwkvModel": "sm56"}, "sm56"),
    ],
)
def test_model_for_profile_defaults_to_sm56_and_preserves_explicit_choices(
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
    assert result.rwkv_profile_settings.model == (model or "sm56")
    assert result.rwkv_profile_settings.state_precision == (
        precision or "cards_int4_notes_int8"
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
    mediasrv._on_update_deck_configs_success(request, close_on_success=False)
    assert profile == {
        "rwkvModel": "legacy" if should_save else "sm36",
        "rwkvStatePrecision": "cards_int4_notes_int8" if should_save else "int8",
        "other": 42,
    }
    assert save.call_count == int(should_save)
    assert rwkv_scheduler._reviewer_backend is active_backend


@pytest.mark.parametrize(
    "model,precision", [("unknown", "int8"), ("legacy", "unknown"), ("", "")]
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


def test_new_profiles_save_sm56_without_rewriting_existing_profiles():
    manager = ProfileManager.__new__(ProfileManager)
    manager.db = MagicMock()
    manager.db.scalar.return_value = False
    manager.create("New profile")
    settings = pickle.loads(manager.db.execute.call_args.args[2])
    assert model_for_profile(settings) == "sm56"
    assert settings["rwkvModel"] == "sm56"
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
    ],
)
def test_model_choice_waits_for_profile_reopen_and_keeps_precision_independent(
    monkeypatch, active_model, saved_model, filename
):
    import aqt

    profile = {"rwkvModel": saved_model, "rwkvStatePrecision": "int4"}
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
    assert rwkv_scheduler._rwkv_state_precision() == "int4"
    root = Path(rwkv_scheduler.__file__).parent / "rwkv_inference"
    assert rwkv_scheduler.embedded_rwkv_model_path() == root / filename


@pytest.mark.parametrize(
    "saved_model,expected_model",
    [
        (None, "sm56"),
        ("legacy", "legacy"),
        ("sm40", "sm40"),
        ("sm36", "sm36"),
        ("sm55", "sm55"),
        ("sm56", "sm56"),
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
    assert backend.model_name == (model or "sm56")
    assert backend.state_precision == (precision or "cards_int4_notes_int8")


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
