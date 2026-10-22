# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pickle
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import rwkv_scheduler
from aqt.profiles import ProfileManager, profileConf
from aqt.rwkv_model import model_for_profile


@pytest.mark.parametrize(
    "profile,expected",
    [
        (None, "sm55"),
        ({}, "sm55"),
        ({"rwkvModel": "unknown"}, "sm55"),
        ({"rwkvModel": None}, "sm55"),
        ({"rwkvModel": 36}, "sm55"),
        ({"rwkvModel": "legacy"}, "legacy"),
        ({"rwkvModel": "sm40"}, "sm40"),
        ({"rwkvModel": "sm36"}, "sm36"),
        ({"rwkvModel": "sm55"}, "sm55"),
    ],
)
def test_model_for_profile_defaults_to_sm55_and_preserves_explicit_choices(
    profile, expected
):
    assert model_for_profile(profile) == expected


def test_new_profiles_save_sm55_without_rewriting_existing_profiles():
    manager = ProfileManager.__new__(ProfileManager)
    manager.db = MagicMock()
    manager.db.scalar.return_value = False
    manager.create("New profile")
    settings = pickle.loads(manager.db.execute.call_args.args[2])
    assert model_for_profile(settings) == "sm55"
    assert settings["rwkvModel"] == "sm55"
    assert "rwkvModel" not in profileConf
    manager.db.reset_mock()
    manager.db.scalar.return_value = True
    manager.create("Existing profile")
    manager.db.execute.assert_not_called()


@pytest.mark.parametrize("active_model", ["legacy", "sm40", "sm36", "sm55"])
@pytest.mark.parametrize(
    "saved_model,filename",
    [
        ("legacy", "RWKV_trained_on_5000_10000.bin"),
        ("sm40", "sm40v2/weights.f32.bin"),
        ("sm36", "sm36/weights.f32.bin"),
        ("sm55", "sm55/weights.f32.bin"),
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
        (None, "sm55"),
        ("legacy", "legacy"),
        ("sm40", "sm40"),
        ("sm36", "sm36"),
        ("sm55", "sm55"),
    ],
)
@pytest.mark.parametrize("active_model", ["legacy", "sm40", "sm36", "sm55"])
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


@pytest.mark.parametrize("model", [None, "legacy", "sm40", "sm36", "sm55"])
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
    assert backend.model_name == (model or "sm55")
    assert backend.state_precision == (precision or "cards_int4_notes_int8")


@pytest.mark.parametrize("active_model", ["legacy", "sm40", "sm36", "sm55"])
@pytest.mark.parametrize("saved_model", ["legacy", "sm40", "sm36", "sm55"])
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
