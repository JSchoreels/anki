# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from anki.collection import OpChanges, OpChangesAfterUndo, Preferences
from anki.errors import UndoEmpty
from aqt import gui_hooks
from aqt.operations import CollectionOp
from aqt.qt import QWidget
from aqt.utils import showWarning, tooltip, tr


def undo(*, parent: QWidget) -> None:
    "Undo the last operation, and refresh the UI."

    reviewer = getattr(parent, "reviewer", None)
    set_review_actions_blocked = getattr(reviewer, "set_review_actions_blocked", None)
    if callable(set_review_actions_blocked):
        set_review_actions_blocked(True)

    def unblock_review_actions() -> None:
        if callable(set_review_actions_blocked):
            set_review_actions_blocked(False)

    def on_success(out: OpChangesAfterUndo) -> None:
        from aqt import rwkv_scheduler

        unblock_after_success = True
        try:
            restored_card_ids = rwkv_scheduler.record_collection_undo(out)
            queued_restored_card = False
            if reviewer is not None:
                rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, restored_card_ids)
                if restored_card_ids:
                    queued_restored_card = True
                    out.changes.study_queues = True
            unblock_after_success = not queued_restored_card
            gui_hooks.state_did_undo(out)
            tooltip(tr.undo_action_undone(action=out.operation), parent=parent)
        finally:
            if unblock_after_success:
                unblock_review_actions()

    def on_failure(exc: Exception) -> None:
        try:
            if not isinstance(exc, UndoEmpty):
                showWarning(str(exc), parent=parent)
        finally:
            unblock_review_actions()

    CollectionOp(parent, lambda col: col.undo()).success(on_success).failure(
        on_failure
    ).run_in_background()


def redo(*, parent: QWidget) -> None:
    "Redo the last operation, and refresh the UI."

    def on_success(out: OpChangesAfterUndo) -> None:
        from aqt import rwkv_scheduler

        rwkv_scheduler.record_collection_redo(out)
        tooltip(tr.undo_action_redone(action=out.operation), parent=parent)

    CollectionOp(parent, lambda col: col.redo()).success(on_success).run_in_background()


def set_preferences(
    *, parent: QWidget, preferences: Preferences
) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.set_preferences(preferences))
