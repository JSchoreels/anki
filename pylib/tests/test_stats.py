# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os
import tempfile

from tests.shared import getEmptyCol


def test_stats():
    col = getEmptyCol()
    note = col.newNote()
    note["Front"] = "foo"
    col.addNote(note)
    c = note.cards()[0]
    # card stats
    card_stats = col.card_stats_data(c.id)
    assert card_stats.note_id == note.id
    c = col.sched.getCard()
    col.sched.answerCard(c, 3)
    col.sched.answerCard(c, 2)
    card_stats = col.card_stats_data(c.id)
    assert len(card_stats.revlog) == 2


def test_graphs_empty():
    col = getEmptyCol()
    assert col.stats().report()


def test_graphs():
    dir = tempfile.gettempdir()
    col = getEmptyCol()
    g = col.stats()
    rep = g.report()
    with open(os.path.join(dir, "test.html"), "w", encoding="UTF-8") as note:
        note.write(rep)


def test_card_memory_metrics():
    col = getEmptyCol()
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"] = "metrics"
    col.add_note(note, 1)
    cid = note.cards()[0].id
    assert not col.card_memory_metrics([])
    metrics = col.card_memory_metrics([cid, 1, cid], include_retrievability=False)
    assert [item.card_id for item in metrics] == [cid, cid]
    assert not metrics[0].HasField("memory_state")
    assert not metrics[0].HasField("fsrs_retrievability")
    assert metrics[0] == metrics[1]


def test_card_details_optional_selection_and_metrics():
    col = getEmptyCol()
    try:
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "猫", "cat"
        col.add_note(note, 1)
        cid = note.cards()[0].id
        assert not col.card_details([])
        plain = col.card_details([cid])[0]
        assert not plain.HasField("metrics")
        assert not plain.HasField("note_fields")
        empty = col.card_details([cid], note_fields=[])[0]
        assert empty.HasField("note_fields")
        assert not empty.note_fields.fields
        entries = col.card_details(
            [cid, 1, cid],
            include_memory_state=True,
            note_fields=["Back", "Front", "Missing"],
        )
        assert [entry.card_id for entry in entries] == [cid, cid]
        assert entries[0] == entries[1]
        assert entries[0].HasField("metrics")
        assert not entries[0].metrics.HasField("fsrs_retrievability")
        assert [
            (field.name, field.value, field.order)
            for field in entries[0].note_fields.fields
        ] == [("Front", "猫", 0), ("Back", "cat", 1)]
    finally:
        col.close()
