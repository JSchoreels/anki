# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# coding: utf-8

import os
import tempfile
import types
from typing import Any, cast

import anki.collection as collection
from anki.cards import CardId
from anki.collection import AddonFsrsPreset, FsrsPresetOverlay, FsrsPresetRule
from anki.collection import Collection as aopen
from anki.dbproxy import emulate_named_args
from anki.lang import TR, without_unicode_isolation
from anki.stdmodels import _legacy_add_basic_model, get_stock_notetypes
from anki.utils import is_win
from tests.shared import assertException, getEmptyCol


class FakeProto:
    def __init__(self, **fields: Any) -> None:
        self._fields = set(fields)
        for key, value in fields.items():
            setattr(self, key, value)

    def HasField(self, field: str) -> bool:
        return field in self._fields


def test_create_open():
    (fd, path) = tempfile.mkstemp(suffix=".anki2", prefix="test_attachNew")
    try:
        os.close(fd)
        os.unlink(path)
    except OSError:
        pass
    col = aopen(path)
    # for open()
    newPath = col.path
    newMod = col.mod
    col.close()
    del col

    # reopen
    col = aopen(newPath)
    assert col.mod == newMod
    col.close()

    # non-writeable dir
    if is_win:
        dir = "c:\root.anki2"
    else:
        dir = "/attachroot.anki2"
    assertException(Exception, lambda: aopen(dir))
    # reuse tmp file from before, test non-writeable file
    os.chmod(newPath, 0)
    assertException(Exception, lambda: aopen(newPath))
    os.chmod(newPath, 0o666)
    os.unlink(newPath)


def test_fsrs_preset_overlay_helper():
    col = getEmptyCol()
    overlay = FsrsPresetOverlay(
        presets=[
            AddonFsrsPreset(
                id="addon:test:medical",
                name="Medical",
                fsrs_version="six",
                params=[1.0] * 21,
                desired_retention=0.9,
                historical_retention=0.9,
            )
        ],
        rules=[
            FsrsPresetRule(
                search="tag:medical",
                preset_id="addon:test:medical",
            )
        ],
    )

    col.set_fsrs_preset_overlay(overlay)
    stored = col.get_fsrs_preset_overlay()

    assert stored == overlay


def test_fsrs_preset_overlay_helper_validates_rules():
    col = getEmptyCol()
    overlay = FsrsPresetOverlay(
        presets=[
            AddonFsrsPreset(
                id="addon:test:medical",
                name="Medical",
                fsrs_version="six",
                params=[1.0] * 21,
                desired_retention=0.9,
                historical_retention=0.9,
            )
        ],
        rules=[
            FsrsPresetRule(
                search="prop:d>0.5",
                preset_id="addon:test:medical",
            )
        ],
    )

    assertException(Exception, lambda: col.set_fsrs_preset_overlay(overlay))


def test_fsrs_preset_overlay_helper_validates_rule_preset_ids():
    col = getEmptyCol()
    overlay = FsrsPresetOverlay(
        presets=[
            AddonFsrsPreset(
                id="addon:test:medical",
                name="Medical",
                fsrs_version="six",
                params=[1.0] * 21,
                desired_retention=0.9,
                historical_retention=0.9,
            )
        ],
        rules=[
            FsrsPresetRule(
                search="tag:medical",
                preset_id="addon:test:missing",
            )
        ],
    )

    assertException(Exception, lambda: col.set_fsrs_preset_overlay(overlay))


def test_compute_memory_state_exposes_internal_stability():
    col = object.__new__(collection.Collection)
    col._backend = cast(
        Any,
        types.SimpleNamespace(
            compute_memory_state=lambda _card_id: FakeProto(
                desired_retention=0.9,
                decay=0.047,
                state=FakeProto(
                    stability=164.3861,
                    stability_internal=139.0,
                    difficulty=3.189,
                ),
            )
        ),
    )

    memory_state = col.compute_memory_state(CardId(123))

    assert memory_state.stability == 164.3861
    assert memory_state.stability_internal == 139.0
    assert memory_state.difficulty == 3.189
    assert memory_state.desired_retention == 0.9
    assert memory_state.decay == 0.047


def test_compute_memory_state_internal_stability_defaults_to_stability():
    col = object.__new__(collection.Collection)
    col._backend = cast(
        Any,
        types.SimpleNamespace(
            compute_memory_state=lambda _card_id: FakeProto(
                desired_retention=0.9,
                decay=0.047,
                state=FakeProto(
                    stability=164.3861,
                    difficulty=3.189,
                ),
            )
        ),
    )

    memory_state = col.compute_memory_state(CardId(123))

    assert memory_state.stability == 164.3861
    assert memory_state.stability_internal == 164.3861


def test_noteAddDelete():
    col = getEmptyCol()
    # add a note
    note = col.newNote()
    note["Front"] = "one"
    note["Back"] = "two"
    n = col.addNote(note)
    assert n == 1
    # test multiple cards - add another template
    m = col.models.current()
    mm = col.models
    t = mm.new_template("Reverse")
    t["qfmt"] = "{{Back}}"
    t["afmt"] = "{{Front}}"
    mm.add_template(m, t)
    mm.save(m)
    assert col.card_count() == 2
    # creating new notes should use both cards
    note = col.newNote()
    note["Front"] = "three"
    note["Back"] = "four"
    n = col.addNote(note)
    assert n == 2
    assert col.card_count() == 4
    # check q/a generation
    c0 = note.cards()[0]
    assert "three" in c0.question()
    # it should not be a duplicate
    assert not note.fields_check()
    # now let's make a duplicate
    note2 = col.newNote()
    note2["Front"] = "one"
    note2["Back"] = ""
    assert note2.fields_check()
    # empty first field should not be permitted either
    note2["Front"] = " "
    assert note2.fields_check()


def test_fieldChecksum():
    col = getEmptyCol()
    note = col.newNote()
    note["Front"] = "new"
    note["Back"] = "new2"
    col.addNote(note)
    assert col.db.scalar("select csum from notes") == int("c2a6b03f", 16)
    # changing the val should change the checksum
    note["Front"] = "newx"
    note.flush()
    assert col.db.scalar("select csum from notes") == int("302811ae", 16)


def test_addDelTags():
    col = getEmptyCol()
    note = col.newNote()
    note["Front"] = "1"
    col.addNote(note)
    note2 = col.newNote()
    note2["Front"] = "2"
    col.addNote(note2)
    # adding for a given id
    col.tags.bulk_add([note.id], "foo")
    note.load()
    note2.load()
    assert "foo" in note.tags
    assert "foo" not in note2.tags
    # should be canonified
    col.tags.bulk_add([note.id], "foo aaa")
    note.load()
    assert note.tags[0] == "aaa"
    assert len(note.tags) == 2


def test_timestamps():
    col = getEmptyCol()
    assert len(col.models.all_names_and_ids()) == len(get_stock_notetypes(col))
    for i in range(100):
        _legacy_add_basic_model(col)
    assert len(col.models.all_names_and_ids()) == 100 + len(get_stock_notetypes(col))


def test_furigana():
    col = getEmptyCol()
    mm = col.models
    m = mm.current()
    # filter should work
    m["tmpls"][0]["qfmt"] = "{{kana:Front}}"
    mm.save(m)
    n = col.newNote()
    n["Front"] = "foo[abc]"
    col.addNote(n)
    c = n.cards()[0]
    assert c.question().endswith("abc")
    # and should avoid sound
    n["Front"] = "foo[sound:abc.mp3]"
    n.flush()
    assert "anki:play" in c.question(reload=True)
    # it shouldn't throw an error while people are editing
    m["tmpls"][0]["qfmt"] = "{{kana:}}"
    mm.save(m)
    c.question(reload=True)


def test_translate():
    col = getEmptyCol()
    no_uni = without_unicode_isolation

    assert (
        col.tr.card_template_rendering_front_side_problem()
        == "Front template has a problem:"
    )
    assert no_uni(col.tr.statistics_reviews(reviews=1)) == "1 review"
    assert no_uni(col.tr.statistics_reviews(reviews=2)) == "2 reviews"


def test_db_named_args(capsys):
    sql = "select a, 2+:test5 from b where arg =:foo and x = :test5"
    args: tuple = tuple()
    kwargs = dict(test5=5, foo="blah")

    s, a = emulate_named_args(sql, args, kwargs)
    assert s == "select a, 2+?1 from b where arg =?2 and x = ?1"
    assert a == [5, "blah"]

    # swallow the warning
    _ = capsys.readouterr()


def test_fsrs_interval_at_retrievability_helpers():
    col = getEmptyCol()

    class BatchRequest:
        class Item:
            def __init__(self, *, card_id: int, stability: float) -> None:
                self.card_id = card_id
                self.stability = stability

    class BatchResponse:
        class Item:
            def __init__(self, *, card_id: int, interval: float) -> None:
                self.card_id = card_id
                self.interval = interval

    class SchedulerPb2Stub:
        FsrsIntervalAtRetrievabilityBatchRequest = BatchRequest
        FsrsIntervalAtRetrievabilityBatchResponse = BatchResponse

    class DummyBackend:
        def __init__(self) -> None:
            self.single_args: tuple[int, float, float] | None = None
            self.batch_args: tuple[list[Any], float] | None = None

        def fsrs_interval_at_retrievability(
            self, *, card_id: int, stability: float, target_retrievability: float
        ) -> float:
            self.single_args = (card_id, stability, target_retrievability)
            return 12.5

        def fsrs_interval_at_retrievability_batch(
            self,
            *,
            items: list[Any],
            target_retrievability: float,
        ) -> list[BatchResponse.Item]:
            self.batch_args = (items, target_retrievability)
            return [
                BatchResponse.Item(
                    card_id=items[0].card_id,
                    interval=items[0].stability + 1.0,
                ),
                BatchResponse.Item(
                    card_id=items[1].card_id,
                    interval=items[1].stability + 2.0,
                ),
            ]

    backend = DummyBackend()
    original_scheduler_pb2 = collection.scheduler_pb2
    try:
        collection.scheduler_pb2 = SchedulerPb2Stub  # type: ignore[assignment]
        col._backend = backend  # type: ignore[assignment]

        single = col.fsrs_interval_at_retrievability(1001, 9.0, 0.9)
        batch = col.fsrs_interval_at_retrievability_batch(
            [(1001, 9.0), (1002, 10.0)], 0.9
        )

        assert single == 12.5
        assert backend.single_args == (1001, 9.0, 0.9)
        assert backend.batch_args is not None
        assert backend.batch_args[1] == 0.9
        assert [item.card_id for item in backend.batch_args[0]] == [1001, 1002]
        assert [item.stability for item in backend.batch_args[0]] == [9.0, 10.0]
        assert batch == {1001: 10.0, 1002: 12.0}
    finally:
        collection.scheduler_pb2 = original_scheduler_pb2


def test_fsrs_interval_at_retrievability_by_config_batch_helper():
    col = getEmptyCol()

    class ByConfigBatchRequest:
        class Item:
            def __init__(
                self, *, request_index: int, config_id: int, stability: float
            ) -> None:
                self.request_index = request_index
                self.config_id = config_id
                self.stability = stability

    class SchedulerPb2Stub:
        FsrsIntervalAtRetrievabilityByConfigBatchRequest = ByConfigBatchRequest

    class ResponseItem:
        def __init__(self, *, request_index: int, interval: float) -> None:
            self.request_index = request_index
            self.interval = interval

    class DummyBackend:
        def __init__(self) -> None:
            self.batch_args: tuple[list[Any], float] | None = None

        def fsrs_interval_at_retrievability_by_config_batch(
            self,
            *,
            items: list[Any],
            target_retrievability: float,
        ) -> list[ResponseItem]:
            self.batch_args = (items, target_retrievability)
            # Return out of order to verify wrapper reorders by request_index.
            return [
                ResponseItem(request_index=2, interval=30.0),
                ResponseItem(request_index=0, interval=10.0),
                ResponseItem(request_index=1, interval=20.0),
            ]

    backend = DummyBackend()
    original_scheduler_pb2 = collection.scheduler_pb2
    try:
        collection.scheduler_pb2 = SchedulerPb2Stub  # type: ignore[assignment]
        col._backend = backend  # type: ignore[assignment]
        intervals = col.fsrs_interval_at_retrievability_by_config_batch(
            [(101, 7.0), (101, 7.0), (202, 9.0)],
            0.9,
        )

        assert backend.batch_args is not None
        assert backend.batch_args[1] == 0.9
        assert [item.request_index for item in backend.batch_args[0]] == [0, 1, 2]
        assert [item.config_id for item in backend.batch_args[0]] == [101, 101, 202]
        assert [item.stability for item in backend.batch_args[0]] == [7.0, 7.0, 9.0]
        assert intervals == [10.0, 20.0, 30.0]
    finally:
        collection.scheduler_pb2 = original_scheduler_pb2
