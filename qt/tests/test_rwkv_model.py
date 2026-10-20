# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pickle
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import rwkv_scheduler
from aqt.profiles import ProfileManager, profileConf
from aqt.rwkv_model import model_for_profile


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, "sm40"),
        ({}, "sm40"),
        ({"rwkvModel": "unknown"}, "sm40"),
        ({"rwkvModel": None}, "sm40"),
        ({"rwkvModel": "legacy"}, "legacy"),
        ({"rwkvModel": "sm40"}, "sm40"),
    ],
)
def test_model_for_profile_defaults_to_sm40_and_preserves_explicit_choices(
    profile, expected
):
    assert model_for_profile(profile) == expected


def test_new_profiles_save_sm40_without_rewriting_existing_profiles():
    manager = ProfileManager.__new__(ProfileManager)
    manager.db = MagicMock()
    manager.db.scalar.return_value = False
    manager.create("New profile")
    settings = pickle.loads(manager.db.execute.call_args.args[2])
    assert model_for_profile(settings) == "sm40"
    assert "rwkvModel" not in profileConf
    manager.db.reset_mock()
    manager.db.scalar.return_value = True
    manager.create("Existing profile")
    manager.db.execute.assert_not_called()


def test_model_choice_waits_for_profile_reopen_and_keeps_precision_independent(
    monkeypatch,
):
    import aqt

    profile = {"rwkvModel": "sm40", "rwkvStatePrecision": "int4"}
    monkeypatch.setattr(aqt, "mw", SimpleNamespace(pm=SimpleNamespace(profile=profile)))
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(model_name="legacy", state_precision="f32"),
    )
    assert rwkv_scheduler._rwkv_model() == "legacy"
    assert rwkv_scheduler._rwkv_state_precision() == "f32"
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert rwkv_scheduler._rwkv_model() == "sm40"
    assert rwkv_scheduler._rwkv_state_precision() == "int4"
    assert rwkv_scheduler.embedded_rwkv_model_path().name == "weights.f32.bin"
    profile["rwkvModel"] = "legacy"
    assert (
        rwkv_scheduler.embedded_rwkv_model_path().name
        == "RWKV_trained_on_5000_10000.bin"
    )


@pytest.mark.parametrize(
    "saved_model,active_model,replace_expected",
    [
        ("legacy", "sm40", True),
        ("sm40", "legacy", True),
        ("legacy", "legacy", False),
        ("sm40", "sm40", False),
        (None, "legacy", True),
        (None, "sm40", False),
    ],
)
def test_profile_open_reuses_only_the_saved_model(
    monkeypatch, saved_model, active_model, replace_expected
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
                "rwkvStatePrecision": "int8",
                **({"rwkvModel": saved_model} if saved_model else {}),
            }
        )
    )
    rwkv_scheduler.begin_rwkv_state_cache_startup(owner)
    if replace_expected:
        replace.assert_called_once_with(None)
    else:
        replace.assert_not_called()


@pytest.mark.parametrize("model", [None, "legacy", "sm40"])
@pytest.mark.parametrize("precision", ["f32", "int8", "int4"])
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
                    "rwkvStatePrecision": precision,
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
    assert backend.model_name == (model or "sm40")
    assert backend.state_precision == precision


def test_cache_replay_identity_tracks_active_model_until_profile_reopen(monkeypatch):
    import aqt

    profile = {"rwkvModel": "legacy", "rwkvStatePrecision": "int8"}
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

    legacy_key = key()
    monkeypatch.setattr(
        rwkv_scheduler,
        "_reviewer_backend",
        SimpleNamespace(model_name="legacy", state_precision="int8"),
    )
    profile["rwkvModel"] = "sm40"
    assert key() == legacy_key
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    assert key() != legacy_key
