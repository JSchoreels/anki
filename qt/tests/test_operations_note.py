# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable, Sequence
from types import SimpleNamespace

from aqt.operations import note as note_ops


def test_remove_notes_preserves_rwkv_state(monkeypatch) -> None:
    note_ids = [10, 20]
    outcome = SimpleNamespace()
    removed_note_ids: list[list[int]] = []
    reconciled_note_ids: list[list[int]] = []

    def remove_notes(ids: Sequence[int]) -> object:
        removed_note_ids.append(list(ids))
        return outcome

    col = SimpleNamespace(remove_notes=remove_notes)

    def run_preserving_rwkv_state(
        col_arg: object,
        mutation: Callable[[], object],
        *,
        note_ids: Sequence[int] = (),
    ) -> object:
        assert col_arg is col
        reconciled_note_ids.append(list(note_ids))
        return mutation()

    monkeypatch.setattr(
        note_ops,
        "_run_preserving_rwkv_state",
        run_preserving_rwkv_state,
    )

    operation = note_ops.remove_notes(parent=SimpleNamespace(), note_ids=note_ids)
    result = operation._op(col)

    assert result is outcome
    assert removed_note_ids == [note_ids]
    assert reconciled_note_ids == [note_ids]
