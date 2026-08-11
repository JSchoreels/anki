# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import aqt.rwkv_scheduler
from anki.collection import OpChanges
from aqt.operations import collection as collection_ops


def test_undo_with_rwkv_restored_card_marks_study_queues(monkeypatch) -> None:
    out = SimpleNamespace(changes=OpChanges(), operation="Answer Card")
    parent = SimpleNamespace(reviewer=SimpleNamespace(_answeredIds=[123]))
    phase = "idle"

    class CollectionOp:
        def __init__(self, parent: object, op: Callable[[object], object]) -> None:
            self._op = op
            self._success: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._success = callback
            return self

        def failure(self, callback: Callable[[Exception], None]) -> object:
            return self

        def run_in_background(self) -> None:
            nonlocal phase
            assert self._success is not None
            phase = "operation"
            result = self._op(SimpleNamespace(undo=lambda: out))
            phase = "success"
            self._success(result)
            phase = "done"

    monkeypatch.setattr(collection_ops, "CollectionOp", CollectionOp)

    def record_collection_undo(undo_out: object) -> list[int]:
        assert undo_out is out
        assert phase == "operation"
        return [123]

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_collection_undo", record_collection_undo
    )
    monkeypatch.setattr(
        collection_ops.gui_hooks, "state_did_undo", lambda undo_out: None
    )
    monkeypatch.setattr(
        collection_ops.tr,
        "undo_action_undone",
        lambda *, action: f"undone {action}",
    )
    monkeypatch.setattr(collection_ops, "tooltip", lambda *args, **kwargs: None)

    collection_ops.undo(parent=parent)

    assert out.changes.study_queues
    assert aqt.rwkv_scheduler.pop_reviewer_undo_card_id(parent.reviewer) == 123


def test_redo_preserves_rwkv_state_from_generic_queue_invalidation(monkeypatch) -> None:
    out = SimpleNamespace(
        changes=OpChanges(study_queues=True),
        operation="Answer Card",
    )
    reviewer = SimpleNamespace()
    parent = SimpleNamespace(reviewer=reviewer)
    phase = "idle"
    marked: list[object] = []

    class CollectionOp:
        def __init__(self, parent: object, op: Callable[[object], object]) -> None:
            self._op = op
            self._success: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._success = callback
            return self

        def run_in_background(self) -> None:
            nonlocal phase
            assert self._success is not None
            phase = "operation"
            result = self._op(SimpleNamespace(redo=lambda: out))
            phase = "success"
            self._success(result)
            phase = "done"

    monkeypatch.setattr(collection_ops, "CollectionOp", CollectionOp)

    def record_collection_redo(redo_out: object) -> list[int]:
        assert redo_out is out
        assert phase == "operation"
        return [123]

    def apply_redo(arg: object, card_ids: list[int]) -> None:
        assert phase == "success"
        assert card_ids == [123]
        marked.append(arg)

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "record_collection_redo",
        record_collection_redo,
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "apply_reviewer_redo_card_ids",
        apply_redo,
    )
    monkeypatch.setattr(
        collection_ops.tr,
        "undo_action_redone",
        lambda *, action: f"redone {action}",
    )
    monkeypatch.setattr(collection_ops, "tooltip", lambda *args, **kwargs: None)

    collection_ops.redo(parent=parent)

    assert marked == [reviewer]


def test_undo_blocks_reviewer_actions_until_operation_finishes(monkeypatch) -> None:
    out = SimpleNamespace(changes=OpChanges(), operation="Answer Card")

    class Reviewer:
        def __init__(self) -> None:
            self.block_calls: list[bool] = []

        def set_review_actions_blocked(self, blocked: bool) -> None:
            self.block_calls.append(blocked)

    reviewer = Reviewer()
    parent = SimpleNamespace(reviewer=reviewer)

    class CollectionOp:
        def __init__(self, parent: object, op: Callable[[object], object]) -> None:
            self._op = op
            self._success: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._success = callback
            return self

        def failure(self, callback: Callable[[Exception], None]) -> object:
            return self

        def run_in_background(self) -> None:
            assert reviewer.block_calls == [True]
            assert self._success is not None
            result = self._op(SimpleNamespace(undo=lambda: out))
            self._success(result)
            assert reviewer.block_calls == [True, False]

    monkeypatch.setattr(collection_ops, "CollectionOp", CollectionOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_collection_undo", lambda undo_out: []
    )
    monkeypatch.setattr(
        collection_ops.gui_hooks, "state_did_undo", lambda undo_out: None
    )
    monkeypatch.setattr(
        collection_ops.tr,
        "undo_action_undone",
        lambda *, action: f"undone {action}",
    )
    monkeypatch.setattr(collection_ops, "tooltip", lambda *args, **kwargs: None)

    collection_ops.undo(parent=parent)

    assert reviewer.block_calls == [True, False]


def test_undo_keeps_reviewer_actions_blocked_for_restored_card_refresh(
    monkeypatch,
) -> None:
    out = SimpleNamespace(changes=OpChanges(), operation="Answer Card")
    out.changes.study_queues = True

    class Reviewer:
        def __init__(self) -> None:
            self.block_calls: list[bool] = []

        def set_review_actions_blocked(self, blocked: bool) -> None:
            self.block_calls.append(blocked)

    reviewer = Reviewer()
    parent = SimpleNamespace(reviewer=reviewer)

    class CollectionOp:
        def __init__(self, parent: object, op: Callable[[object], object]) -> None:
            self._op = op
            self._success: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._success = callback
            return self

        def failure(self, callback: Callable[[Exception], None]) -> object:
            return self

        def run_in_background(self) -> None:
            assert reviewer.block_calls == [True]
            assert self._success is not None
            result = self._op(SimpleNamespace(undo=lambda: out))
            self._success(result)
            assert reviewer.block_calls == [True]

    monkeypatch.setattr(collection_ops, "CollectionOp", CollectionOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_collection_undo", lambda undo_out: [123]
    )
    monkeypatch.setattr(
        collection_ops.gui_hooks, "state_did_undo", lambda undo_out: None
    )
    monkeypatch.setattr(
        collection_ops.tr,
        "undo_action_undone",
        lambda *, action: f"undone {action}",
    )
    monkeypatch.setattr(collection_ops, "tooltip", lambda *args, **kwargs: None)

    collection_ops.undo(parent=parent)

    assert reviewer.block_calls == [True]


def test_undo_unblocks_reviewer_actions_without_restored_card(
    monkeypatch,
) -> None:
    out = SimpleNamespace(changes=OpChanges(), operation="Answer Card")
    out.changes.study_queues = True

    class Reviewer:
        def __init__(self) -> None:
            self.block_calls: list[bool] = []

        def set_review_actions_blocked(self, blocked: bool) -> None:
            self.block_calls.append(blocked)

    reviewer = Reviewer()
    parent = SimpleNamespace(reviewer=reviewer)

    class CollectionOp:
        def __init__(self, parent: object, op: Callable[[object], object]) -> None:
            self._op = op
            self._success: Callable[[object], None] | None = None

        def success(self, callback: Callable[[object], None]) -> object:
            self._success = callback
            return self

        def failure(self, callback: Callable[[Exception], None]) -> object:
            return self

        def run_in_background(self) -> None:
            assert reviewer.block_calls == [True]
            assert self._success is not None
            result = self._op(SimpleNamespace(undo=lambda: out))
            self._success(result)
            assert reviewer.block_calls == [True, False]

    monkeypatch.setattr(collection_ops, "CollectionOp", CollectionOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler, "record_collection_undo", lambda undo_out: []
    )
    monkeypatch.setattr(
        collection_ops.gui_hooks, "state_did_undo", lambda undo_out: None
    )
    monkeypatch.setattr(
        collection_ops.tr,
        "undo_action_undone",
        lambda *, action: f"undone {action}",
    )
    monkeypatch.setattr(collection_ops, "tooltip", lambda *args, **kwargs: None)

    collection_ops.undo(parent=parent)

    assert reviewer.block_calls == [True, False]
