# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import logging
import time

from anki.collection import Collection, OpChanges, OpChangesAfterUndo, Preferences
from anki.errors import UndoEmpty
from aqt import gui_hooks
from aqt.operations import CollectionOp
from aqt.qt import QWidget
from aqt.utils import showWarning, tooltip, tr

logger = logging.getLogger(__name__)


def undo(*, parent: QWidget) -> None:
    "Undo the last operation, and refresh the UI."

    reviewer = getattr(parent, "reviewer", None)
    requested_at = time.monotonic()
    restored_card_ids: list[int] = []
    set_review_actions_blocked = getattr(reviewer, "set_review_actions_blocked", None)
    begin_undo = getattr(reviewer, "begin_undo", None)
    finish_undo = getattr(reviewer, "finish_undo", None)
    if callable(begin_undo):
        begin_undo()
    elif callable(set_review_actions_blocked):
        set_review_actions_blocked(True)

    def unblock_review_actions() -> None:
        if callable(finish_undo):
            finish_undo(None)
        elif callable(set_review_actions_blocked):
            set_review_actions_blocked(False)

    def perform_undo(col: Collection) -> OpChangesAfterUndo:
        started_at = time.monotonic()
        out = col.undo()
        collection_finished_at = time.monotonic()
        from aqt import rwkv_scheduler

        restored_card_ids.extend(rwkv_scheduler.record_collection_undo(out))
        logger.debug(
            "undo completed: wait_ms=%.1f collection_ms=%.1f rwkv_ms=%.1f restored_card_ids=%s",
            (started_at - requested_at) * 1000,
            (collection_finished_at - started_at) * 1000,
            (time.monotonic() - collection_finished_at) * 1000,
            restored_card_ids,
        )
        return out

    def on_success(out: OpChangesAfterUndo) -> None:
        from aqt import rwkv_scheduler

        unblock_after_success = True
        try:
            queued_restored_card = False
            if reviewer is not None:
                rwkv_scheduler.queue_reviewer_undo_card_ids(reviewer, restored_card_ids)
                if restored_card_ids:
                    queued_restored_card = True
                    out.changes.study_queues = True
            unblock_after_success = not queued_restored_card
            gui_hooks.state_did_undo(out)
            tooltip(tr.undo_action_undone(action=out.operation), parent=parent)
            if callable(finish_undo):
                finish_undo(out.changes)
                unblock_after_success = False
        finally:
            if unblock_after_success:
                unblock_review_actions()

    def on_failure(exc: Exception) -> None:
        try:
            if not isinstance(exc, UndoEmpty):
                showWarning(str(exc), parent=parent)
        finally:
            unblock_review_actions()

    CollectionOp(parent, perform_undo).success(on_success).failure(
        on_failure
    ).run_in_background()


def redo(*, parent: QWidget) -> None:
    "Redo the last operation, and refresh the UI."

    reviewer = getattr(parent, "reviewer", None)
    restored_card_ids: list[int] = []

    def perform_redo(col: Collection) -> OpChangesAfterUndo:
        out = col.redo()
        from aqt import rwkv_scheduler

        restored_card_ids.extend(rwkv_scheduler.record_collection_redo(out))
        return out

    def on_success(out: OpChangesAfterUndo) -> None:
        if reviewer is not None and restored_card_ids and out.changes.study_queues:
            from aqt import rwkv_scheduler

            rwkv_scheduler.apply_reviewer_redo_card_ids(
                reviewer,
                restored_card_ids,
            )
        tooltip(tr.undo_action_redone(action=out.operation), parent=parent)

    CollectionOp(parent, perform_redo).success(on_success).run_in_background()


def set_preferences(
    *, parent: QWidget, preferences: Preferences
) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.set_preferences(preferences))
