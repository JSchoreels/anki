# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from anki.collection import Collection
from anki.consts import (
    CARD_TYPE_LRN,
    CARD_TYPE_RELEARNING,
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    CardQueue,
    CardType,
)
from anki.deck_config_pb2 import DeckConfig, UpdateDeckConfigsRequest
from anki.decks import DeckId
from anki.scheduler.v3 import Scheduler
from anki.scheduler_pb2 import CardAnswer, SchedulingStatesWithIntervalsRequest
from anki.utils import int_time

if TYPE_CHECKING:
    from anki.cards import Card


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Collection]:
    collection = Collection(str(tmp_path / "collection.anki2"))
    configure_repeat_guards(collection)
    collection.set_config("collapseTime", 86_400)
    try:
        yield collection
    finally:
        collection.close()


def configure_repeat_guards(
    col: Collection,
    enabled: bool = True,
    minimum_reviews: int = 3,
    minimum_secs: int = 90,
) -> None:
    options = col.decks.get_deck_configs_for_update(DeckId(1))
    preset = next(
        entry.config
        for entry in options.all_config
        if entry.config.id == options.current_deck.config_id
    )
    preset.config.rwkv_review_instant_order_enabled = enabled
    preset.config.rwkv_review_min_intervening_reviews = minimum_reviews
    preset.config.rwkv_review_min_elapsed_secs = minimum_secs
    request = UpdateDeckConfigsRequest(target_deck_id=1, configs=[preset])
    request.limits.CopyFrom(options.current_deck.limits)
    col.decks.update_deck_configs(request)


def add_card(col: Collection, deck_id: DeckId = DeckId(1)) -> Card:
    note = col.new_note(col.models.current())
    note["Front"] = "Repeat spacing"
    col.add_note(note, deck_id)
    return note.cards()[0]


def scheduler(col: Collection) -> Scheduler:
    assert isinstance(col.sched, Scheduler)
    return col.sched


def answer(col: Collection, rating: CardAnswer.Rating.V) -> None:
    queued = scheduler(col).get_queued_cards().cards[0]
    state = {
        CardAnswer.AGAIN: queued.states.again,
        CardAnswer.GOOD: queued.states.good,
        CardAnswer.EASY: queued.states.easy,
    }[rating]
    scheduler(col).answer_card(
        CardAnswer(
            card_id=queued.card.id,
            current_state=queued.states.current,
            new_state=state,
            rating=rating,
            answered_at_millis=int_time(1000),
            milliseconds_taken=1000,
        )
    )


def offered_ids(col: Collection) -> list[int]:
    return [
        entry.card.id
        for entry in scheduler(col).get_queued_cards_without_states().cards
    ]


def test_rwkv_learning_repeat_is_withheld_after_answer_and_redo(
    col: Collection,
) -> None:
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)

    queued = scheduler(col).get_queued_cards_without_states()
    assert list(queued.cards) == []
    assert queued.learning_count == 0
    assert (
        list(
            scheduler(col)
            .get_queued_cards_without_states(intraday_learning_only=True)
            .cards
        )
        == []
    )

    col.undo()
    assert offered_ids(col) == [card.id]
    col.redo()
    assert offered_ids(col) == []


@pytest.mark.parametrize(
    ("queue", "card_type", "learn_ahead"),
    [
        (QUEUE_TYPE_LRN, CARD_TYPE_LRN, False),
        (QUEUE_TYPE_LRN, CARD_TYPE_LRN, True),
        (QUEUE_TYPE_LRN, CARD_TYPE_RELEARNING, False),
        (QUEUE_TYPE_LRN, CARD_TYPE_RELEARNING, True),
        (QUEUE_TYPE_DAY_LEARN_RELEARN, CARD_TYPE_RELEARNING, False),
    ],
)
def test_rwkv_learning_repeat_requires_both_minimums(
    col: Collection, queue: CardQueue, card_type: CardType, learn_ahead: bool
) -> None:
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.queue = queue
    card.type = card_type
    card.ivl = 1
    card.due = (
        scheduler(col).today
        if queue == QUEUE_TYPE_DAY_LEARN_RELEARN
        else scheduler(col).day_cutoff - 1
        if learn_ahead
        else int_time() - 1
    )
    # Keep the time guard unmet regardless of how long the test runner takes.
    card.last_review_time = int_time() + 3600
    col.update_card(card)
    assert offered_ids(col) == []

    for _ in range(3):
        add_card(col)
        answer(col, CardAnswer.EASY)
    assert offered_ids(col) == []
    assert scheduler(col).get_queued_cards_without_states().learning_count == 0

    card.last_review_time = int_time() - 3600
    col.update_card(card)
    assert offered_ids(col) == [card.id]
    assert scheduler(col).get_queued_cards_without_states().learning_count == 1


def test_rwkv_learning_repeat_counts_only_answers_in_selected_deck_tree(
    col: Collection,
) -> None:
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.last_review_time = int_time() - 3600
    col.update_card(card)

    outside = col.decks.add_normal_deck_with_name("Outside").id
    col.decks.set_current(DeckId(outside))
    for _ in range(3):
        add_card(col, DeckId(outside))
        answer(col, CardAnswer.EASY)
    col.decks.set_current(DeckId(1))
    assert offered_ids(col) == []

    child = col.decks.add_normal_deck_with_name("Default::Child").id
    for _ in range(3):
        add_card(col, DeckId(child))
        answer(col, CardAnswer.EASY)
    assert offered_ids(col) == [card.id]


@pytest.mark.parametrize("instant_enabled", [False, True])
def test_learning_repeat_is_available_when_repeat_guards_do_not_apply(
    col: Collection, instant_enabled: bool
) -> None:
    if instant_enabled:
        configure_repeat_guards(col, minimum_reviews=0, minimum_secs=0)
    else:
        configure_repeat_guards(col, enabled=False)

    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    assert offered_ids(col) == [card.id]


@pytest.mark.parametrize(("minimum_reviews", "minimum_secs"), [(3, 0), (0, 90)])
def test_rwkv_learning_repeat_can_disable_each_minimum(
    col: Collection, minimum_reviews: int, minimum_secs: int
) -> None:
    configure_repeat_guards(
        col, minimum_reviews=minimum_reviews, minimum_secs=minimum_secs
    )
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.last_review_time = int_time() + 3600
    col.update_card(card)
    assert offered_ids(col) == []

    if minimum_reviews:
        for _ in range(minimum_reviews):
            add_card(col)
            answer(col, CardAnswer.EASY)
    else:
        card.last_review_time = int_time() - 3600
        col.update_card(card)
    assert offered_ids(col) == [card.id]


def test_rwkv_learning_repeat_recovers_missing_last_answer_time(
    col: Collection,
) -> None:
    configure_repeat_guards(col, minimum_reviews=0)
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.last_review_time = None
    col.update_card(card)
    assert offered_ids(col) == []


def test_rwkv_learning_guards_preserve_an_already_displayed_card(
    col: Collection,
) -> None:
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.queue = QUEUE_TYPE_DAY_LEARN_RELEARN
    card.due = scheduler(col).today
    col.update_card(card)
    other = add_card(col)

    scheduler(col).rebuild_queued_cards_preserving_current_card(card.id)
    assert offered_ids(col) == [card.id]
    answer(col, CardAnswer.GOOD)
    assert offered_ids(col) == [other.id]
    col.undo()
    assert offered_ids(col) == [card.id]
    col.redo()
    assert offered_ids(col) == [other.id]
    answer(col, CardAnswer.EASY)
    assert offered_ids(col) == []


def test_rwkv_learning_guards_follow_undo_and_redo_of_other_answers(
    col: Collection,
) -> None:
    card = add_card(col)
    answer(col, CardAnswer.AGAIN)
    card.load()
    card.queue = QUEUE_TYPE_DAY_LEARN_RELEARN
    card.due = scheduler(col).today
    card.last_review_time = int_time() - 3600
    col.update_card(card)
    for _ in range(3):
        add_card(col)
        answer(col, CardAnswer.EASY)
    assert offered_ids(col) == [card.id]

    col.undo()
    queued = scheduler(col).get_queued_cards_without_states()
    assert queued.cards[0].card.id != card.id
    assert queued.learning_count == 0
    col.redo()
    assert offered_ids(col) == [card.id]
    assert scheduler(col).get_queued_cards_without_states().learning_count == 1


def test_rwkv_curve_generated_learning_interval_keeps_repeat_spacing(
    col: Collection,
) -> None:
    configure_repeat_guards(col, minimum_reviews=0)
    options = col.decks.get_deck_configs_for_update(DeckId(1))
    preset = options.all_config[0].config
    preset.config.fsrs_version = DeckConfig.Config.FSRS_VERSION_SEVEN
    del preset.config.learn_steps[:]
    del preset.config.relearn_steps[:]
    request = UpdateDeckConfigsRequest(
        target_deck_id=1,
        configs=[preset],
        fsrs=True,
        fsrs_short_term_with_steps_enabled=True,
    )
    request.limits.CopyFrom(options.current_deck.limits)
    col.decks.update_deck_configs(request)
    col.set_config("collapseTime", 7200)

    card = add_card(col)
    queued = scheduler(col).get_queued_cards().cards[0]
    # Curve supplies its fractional-day intervals through this native API.
    states = col._backend.scheduling_states_with_intervals(
        SchedulingStatesWithIntervalsRequest(card_id=card.id, good=70 / 1440)
    )
    assert states.good.normal.learning.scheduled_secs == 4200
    scheduler(col).answer_card(
        CardAnswer(
            card_id=card.id,
            current_state=queued.states.current,
            new_state=states.good,
            rating=CardAnswer.GOOD,
            answered_at_millis=int_time(1000),
            milliseconds_taken=1000,
        )
    )
    card.load()
    assert card.queue == QUEUE_TYPE_LRN
    due = card.due
    assert offered_ids(col) == []

    card.last_review_time = int_time() - 3600
    col.update_card(card)
    assert offered_ids(col) == [card.id]
    assert col.get_card(card.id).due == due
