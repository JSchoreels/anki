# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from anki.collection import Collection, OpChanges, OpChangesWithId
from anki.models import ChangeNotetypeRequest, NotetypeDict, NotetypeId
from anki.stdmodels import StockNotetypeKind
from aqt.operations import CollectionOp
from aqt.qt import QWidget


def add_notetype_legacy(
    *,
    parent: QWidget,
    notetype: NotetypeDict,
) -> CollectionOp[OpChangesWithId]:
    def add_notetype_preserving_rwkv_state(col: Collection) -> OpChangesWithId:
        from aqt import rwkv_scheduler

        return rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
            col,
            lambda: col.models.add_dict(notetype),
        )

    return CollectionOp(parent, add_notetype_preserving_rwkv_state)


def update_notetype_legacy(
    *,
    parent: QWidget,
    notetype: NotetypeDict,
    skip_checks: bool = False,
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent, lambda col: col.models.update_dict(notetype, skip_checks)
    )


def remove_notetype(
    *,
    parent: QWidget,
    notetype_id: NotetypeId,
) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.models.remove(notetype_id))


def change_notetype_of_notes(
    *, parent: QWidget, input: ChangeNotetypeRequest
) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.models.change_notetype_of_notes(input))


def restore_notetype_to_stock(
    *, parent: QWidget, notetype_id: NotetypeId, force_kind: StockNotetypeKind.V | None
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: col.models.restore_notetype_to_stock(notetype_id, force_kind),
    )
