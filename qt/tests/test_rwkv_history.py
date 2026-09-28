# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from pathlib import Path
from types import SimpleNamespace

import pytest

from anki.collection import Collection
from anki.decks import DeckId, UpdateDeckConfigs
from anki.errors import Interrupted
from aqt import rwkv_scheduler as scheduler


@pytest.mark.parametrize("elapsed_source", list(scheduler.RwkvFirstReviewElapsedSource))
@pytest.mark.parametrize("preserve_history", [False, True])
def test_native_full_history_preserves_replay_and_recovery_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    elapsed_source: scheduler.RwkvFirstReviewElapsedSource,
    preserve_history: bool,
) -> None:
    col = Collection(str(tmp_path / "collection.anki2"))
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    try:
        notetype = col.models.current()
        assert notetype is not None
        note = col.new_note(notetype)
        note.fields[0] = "replay history"
        col.add_note(note, DeckId(1))
        card = note.cards()[0]
        card.start_timer()
        col.sched.answerCard(card, 4)
        col.sched.answerCard(card, 3)
        first_history = scheduler._historical_rwkv_review_inputs(reviewer)
        col.sched.reset_cards([card.id])
        card = col.get_card(card.id)
        card.start_timer()
        col.sched.answerCard(card, 1)
        col.sched.answerCard(card, 3)
        col.sched.answerCard(card, 4)
        if preserve_history:
            monkeypatch.setattr(
                scheduler,
                "_rwkv_preserved_learning_start_cutoffs",
                lambda _: {card.id: first_history.last_review_id},
            )
        options = dict(
            first_review_elapsed_source=elapsed_source,
            prepare_recovery_checkpoint=True,
        )
        native = scheduler._historical_rwkv_review_inputs(reviewer, **options)
        assert native.review_count == (5 if preserve_history else 3)
        assert native.reviews[0].card_type == scheduler.RwkvReviewState.LEARN_START
        assert native.prepared_checkpoint_histories[1].review_count == 1

        # Supplying prefix maps selects the existing Python replay builder.
        python = scheduler._historical_rwkv_review_inputs(
            reviewer,
            previous_review_id_by_card={},
            previous_interval_days_by_card={},
            review_count_by_card={},
            **options,
        )
        assert native == python
        assert (
            native.prepared_checkpoint_histories == python.prepared_checkpoint_histories
        )

        ignored = frozenset([native.review_ids[1]])
        native_ignored = scheduler._historical_rwkv_review_inputs(
            reviewer, ignored_review_ids=ignored, **options
        )
        python_ignored = scheduler._historical_rwkv_review_inputs(
            reviewer,
            previous_review_id_by_card={},
            previous_interval_days_by_card={},
            review_count_by_card={},
            ignored_review_ids=ignored,
            **options,
        )
        assert native_ignored == python_ignored
        assert (
            native_ignored.prepared_checkpoint_histories
            == python_ignored.prepared_checkpoint_histories
        )
        assert native_ignored.review_count == native.review_count - 1
        assert "using Python replay builder" not in caplog.text
    finally:
        col.close()


def test_native_full_history_handles_empty_collection(tmp_path: Path) -> None:
    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        history = scheduler._historical_rwkv_review_inputs(
            SimpleNamespace(mw=SimpleNamespace(col=col)),
            prepare_recovery_checkpoint=True,
        )
        assert history.reviews == []
        assert history.review_count == 0
        assert history.history_hash == scheduler._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
        assert history.prepared_checkpoint_histories == {}
    finally:
        col.close()


@pytest.mark.parametrize("missing_stable_id", [False, True])
def test_full_history_preserves_dynamic_presets_and_legacy_elapsed_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    missing_stable_id: bool,
) -> None:
    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        config = col.decks.get_deck_configs_for_update(DeckId(1)).all_config[0].config
        config.config.rwkv_review_enabled = True
        config.config.rwkv_review_dynamic_preset_replay = True
        col.decks.update_deck_configs(
            UpdateDeckConfigs(target_deck_id=1, configs=[config])
        )
        # The desktop also understands legacy policy values that the native
        # deck-config field alone cannot represent. Pass its effective value.
        legacy_config = col.decks.config_dict_for_deck_id(DeckId(1))
        legacy_config["rwkvReviewFirstReviewElapsedFromCardCreation"] = True
        monkeypatch.setattr(col.decks, "all_config", lambda: [legacy_config])
        monkeypatch.setattr(
            col.decks, "config_dict_for_deck_id", lambda _: legacy_config
        )
        col.set_config(
            "fsrsPresetOverlay",
            {
                "presets": [
                    {
                        "id": name,
                        "name": name,
                        "fsrs_version": "seven",
                        "params": [],
                        "desired_retention": 0.9,
                        "historical_retention": 0.9,
                        "ignore_revlogs_before_date": "",
                    }
                    for name in ["addon:test:young", "addon:test:mature"]
                ],
                "rules": [{"preset_id": "addon:test:mature", "search": ""}],
                "simulator_rules": [
                    {"preset_id": "addon:test:young", "max_reps": 0},
                    {"preset_id": "addon:test:mature", "min_reps": 1},
                ],
            },
        )
        notetype = col.models.current()
        assert notetype is not None
        note = col.new_note(notetype)
        note.fields[0] = "dynamic replay"
        col.add_note(note, DeckId(1))
        card = note.cards()[0]
        card.start_timer()
        col.sched.answerCard(card, 4)
        col.sched.answerCard(card, 3)
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        if missing_stable_id:
            # Exercise the real Rust error when an add-on preset has no supplied
            # stable id. Python can still derive the id from its string value.
            monkeypatch.setattr(
                scheduler, "_rwkv_history_stable_preset_ids", lambda _: {}
            )
        native = scheduler._historical_rwkv_review_inputs(reviewer)
        assert [review.identity.preset_id for review in native.reviews] == [
            scheduler._stable_preset_id("addon:test:young"),
            scheduler._stable_preset_id("addon:test:mature"),
        ]
        assert native.reviews[0].current_elapsed_seconds >= 0
        python = scheduler._historical_rwkv_review_inputs(
            reviewer,
            previous_review_id_by_card={},
            previous_interval_days_by_card={},
            review_count_by_card={},
        )
        assert native == python
        if missing_stable_id:
            assert "using Python replay builder" in caplog.text
        else:
            assert "using Python replay builder" not in caplog.text
            fingerprint = scheduler._rwkv_historical_review_fingerprint(reviewer)
            assert fingerprint is not None
            assert fingerprint.identity.history_hash == native.history_hash
    finally:
        col.close()


def test_full_history_does_not_retry_cancelled_native_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    col = Collection(str(tmp_path / "collection.anki2"))
    try:

        def interrupted(_request: object) -> None:
            raise Interrupted("cancelled", None, None, None)

        monkeypatch.setattr(col._backend, "rwkv_historical_review_inputs", interrupted)
        with pytest.raises(Interrupted, match="cancelled"):
            scheduler._historical_rwkv_review_inputs(
                SimpleNamespace(mw=SimpleNamespace(col=col))
            )
    finally:
        col.close()
