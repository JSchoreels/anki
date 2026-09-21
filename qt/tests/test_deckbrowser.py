# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anki.decks import DeckTreeNode


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch):
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        tr,
        "decks_review_limit_tooltip",
        lambda *, total, count: f'{total} due; limit allows {count} "reviews".',
    )
    from aqt.deckbrowser import DeckBrowser

    browser = DeckBrowser.__new__(DeckBrowser)
    browser._rwkv_pending_deck_ids = set()
    return browser


def test_review_limit_totals_include_collapsed_children(browser):
    child = DeckTreeNode(
        deck_id=2,
        review_count=40,
        review_uncapped=100,
        review_uncapped_including_children=100,
        level=2,
    )
    parent = DeckTreeNode(
        deck_id=1,
        review_count=65,
        review_uncapped=42,
        review_uncapped_including_children=142,
        children=[child],
        collapsed=True,
        level=1,
    )
    tree = DeckTreeNode(children=[parent])
    browser._render_data = SimpleNamespace(tree=tree, current_deck_id=1)

    labels = browser._review_limit_labels(tree)
    assert labels[1] == (" (/142)", '142 due; limit allows 65 "reviews".')
    assert labels[2][0] == " (/100)"
    rendered = browser._renderDeckTree(tree)
    assert 'class="review-count">65</span>' in rendered
    assert (
        'title="142 due; limit allows 65 &quot;reviews&quot;."> (/142)</span>'
        in rendered
    )
    assert 'id="deck-2-review-limit"' not in rendered


@pytest.mark.parametrize(
    "count,total,expected", [(0, 10, " (/10)"), (10, 10, ""), (0, 0, "")]
)
def test_review_limit_only_shown_when_count_reduced(browser, count, total, expected):
    tree = DeckTreeNode(
        children=[
            DeckTreeNode(
                deck_id=1, review_count=count, review_uncapped_including_children=total
            )
        ]
    )
    assert browser._review_limit_labels(tree)[1][0] == expected


def test_review_limit_refresh_clears_pending_and_unlimited_labels(browser):
    node = DeckTreeNode(
        deck_id=1, review_count=5, review_uncapped_including_children=10
    )
    tree = DeckTreeNode(children=[node])
    scripts = []
    browser.web = SimpleNamespace(eval=scripts.append)
    browser._render_data = SimpleNamespace(tree=tree)

    def refresh_labels():
        browser._render_rwkv_deck_counts()
        encoded = scripts[-1].split("const limitLabels = ", 1)[1].split(";\n", 1)[0]
        return json.loads(encoded)["1"]

    browser._rwkv_pending_deck_ids = {1}
    assert refresh_labels() == ["", ""]
    browser._rwkv_pending_deck_ids.clear()
    assert refresh_labels()[0] == " (/10)"
    tree.children[0].review_count = 10
    assert refresh_labels() == ["", ""]


@pytest.mark.parametrize(
    "reading_answers,sibling_answers,parent_count,reading_count",
    [(2, 2, 0, 1), (1, 5, 1, 0)],
)
def test_rwkv_subdeck_counts_match_opening_each_deck(
    browser,
    tmp_path: Path,
    reading_answers,
    sibling_answers,
    parent_count,
    reading_count,
):
    from anki.collection import Collection
    from aqt import rwkv_scheduler

    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        parent = col.decks.id("All")
        reading = col.decks.id("All::Reading")
        sibling = col.decks.id("All::Other")
        for deck_id, minimum in [(parent, 6), (reading, 2), (sibling, 2)]:
            config = col.decks.add_config(str(deck_id))
            config.update(
                rwkvReviewEnabled=True,
                rwkvReviewInstantOrderEnabled=True,
                rwkvReviewAllowSameDayReview=True,
                rwkvReviewMinInterveningReviews=minimum,
                rwkvReviewMinElapsedSecs=0,
                sameDayReviewsIgnoreReviewLimit=True,
            )
            config["rev"]["perDay"] = 0 if deck_id == parent else 9999
            col.decks.update_config(config)
            col.decks.set_config_id_for_deck_dict(col.decks.get(deck_id), config["id"])

        timing = col.sched._timing_today()
        today_start = timing.next_day_at - 86_400

        def add_review(deck_id):
            note = col.new_note(col.models.by_name("Basic"))
            note["Front"] = str(col.card_count())
            col.add_note(note, deck_id)
            card = note.cards()[0]
            card.type = card.queue = 2
            card.due = timing.days_elapsed + 1
            card.ivl = 1
            card.desired_retention = 0.75
            card.last_review_time = today_start
            col.update_card(card)
            return card.id

        repeat = add_review(reading)
        reading_answer = add_review(reading)
        sibling_answer = add_review(sibling)
        # Only Reading's answers advance Reading's repeat guard. The parent
        # also includes answers from Other, even when both share predictions.
        history = (
            [repeat]
            + [reading_answer] * reading_answers
            + [sibling_answer] * sibling_answers
        )
        for index, card_id in enumerate(history):
            col.db.execute(
                "insert into revlog values (?, ?, 0, 3, 1, 1, 2500, 1000, 1)",
                today_start * 1000 + index + 1,
                card_id,
            )

        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        tree = col.sched.deck_due_tree()
        browser._render_data = SimpleNamespace(tree=tree, current_deck_id=parent)
        browser._rwkv_pending_deck_ids = {parent, reading, sibling}
        scripts = []
        browser.web = SimpleNamespace(eval=scripts.append)
        browser._render_rwkv_deck_counts()
        assert "null" in scripts[-1]

        assert rwkv_scheduler._set_rwkv_deck_count_scores(
            reviewer, parent, [(repeat, 0.5)]
        )
        browser._update_rwkv_deck_counts(parent, col.sched.deck_due_tree())
        rows = json.loads(scripts[-1].split("const rows = ", 1)[1].split(";\n", 1)[0])
        displayed = {deck_id: review for deck_id, _new, _learn, review in rows}
        assert displayed[parent] == parent_count
        assert displayed[reading] == reading_count
        assert not browser._rwkv_pending_deck_ids
        # Different eligibility is not a daily-limit restriction.
        labels = browser._review_limit_labels(browser._render_data.tree)
        assert labels[parent] == labels[reading] == ("", "")

        assert rwkv_scheduler._set_rwkv_deck_count_scores(reviewer, parent, [])
        cleared = col.sched.deck_due_tree().children[0]
        assert cleared.review_count == 0
        assert all(child.review_count == 0 for child in cleared.children)

        for deck_id, expected in [(parent, parent_count), (reading, reading_count)]:
            col.decks.select(deck_id)
            request = rwkv_scheduler._rwkv_score_request(
                reviewer, deck_id, [(repeat, 0.5)]
            )
            col._backend.set_rwkv_review_queue_scores(
                deck_id=deck_id, scores=request.scores
            )
            assert col.sched.get_queued_cards_without_states().review_count == expected
    finally:
        col.close()
