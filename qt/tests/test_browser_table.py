# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import random
from types import SimpleNamespace
from typing import Any, cast

import pytest

import aqt.browser.table.table as table_module
from aqt.browser.table import CellRow
from aqt.browser.table.model import DataModel
from aqt.browser.table.table import Table
from aqt.qt import (
    QAbstractItemView,
    QAbstractTableModel,
    QApplication,
    QItemSelection,
    QItemSelectionModel,
    QItemSelectionRange,
    QModelIndex,
    QPixmap,
    Qt,
    QTableView,
    QWidget,
    sip,
)


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def make_table_view(model: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    model._state.column_label = lambda _column: "Column"
    model._state.column_key_at = lambda section: f"c{section}"
    model.columns = {key: SimpleNamespace() for key in model._state.active_columns}
    monkeypatch.setattr(
        table_module,
        "KeyboardModifiersPressed",
        lambda: SimpleNamespace(shift=False, control=False),
    )
    table = cast(Any, Table.__new__(Table))
    table.browser = cast(Any, QWidget())
    table.browser.on_all_or_selected_rows_changed = lambda: None
    table.browser.on_current_row_changed = lambda: None
    table._model = model
    table._len_selection = 0
    table._selected_rows = None
    table._view = QTableView(table.browser)
    table._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    # Font preferences are unrelated to selection and normally need a collection.
    monkeypatch.setattr(Table, "_update_font", lambda _self: None)
    table._setup_view()
    return table


def test_header_repaint_does_not_scan_selected_rows(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = make_model(2_000, 4, [])
    table = make_table_view(model, monkeypatch)
    try:
        table._view.selectAll()
        header = table._horizontal_header()
        header.setHighlightSections(False)
        header.resize(640, 30)
        visits = 0
        cached_row = model.get_cached_row

        def counted_cached_row(index: QModelIndex) -> Any:
            nonlocal visits
            visits += 1
            return cached_row(index)

        monkeypatch.setattr(model, "get_cached_row", counted_cached_row)
        header.render(QPixmap(header.size()))
        assert visits < model.rowCount(), "header paint scanned the selected rows"
        assert table.len_selection() == 2_000
        assert table._selection_model().isSelected(model.index(1_999, 0))
    finally:
        sip.delete(table.browser)


def test_menu_selection_counts_rows_without_enumerating_the_selection(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = make_model(50, 4, [7, 8])
    table = make_table_view(model, monkeypatch)
    try:
        enumerations = 0
        selected_rows = table._selection_model().selectedRows

        def counted_selected_rows() -> Any:
            nonlocal enumerations
            enumerations += 1
            return selected_rows()

        monkeypatch.setattr(
            table._selection_model(), "selectedRows", counted_selected_rows
        )
        # Menu actions run without Ctrl/Shift being held.
        table._view.selectAll()
        assert table.len_selection() == 48
        table._selection_model().select(
            QItemSelection(model.index(0, 0), model.index(49, 3)),
            QItemSelectionModel.SelectionFlag.Toggle,
        )
        assert table.len_selection() == 0
        table._selection_model().select(
            model.index(3, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        assert table.len_selection() == 1
        table._view.clearSelection()
        assert table.len_selection() == 0
        assert enumerations == 0, "selection counting enumerated the selected rows"
    finally:
        sip.delete(table.browser)


def make_model(rows: int, columns: int, disabled: list[int]) -> Any:
    """A browser table model with `rows` card ids, some of them deleted
    (disabled) and some cached as normal rows."""
    model = cast(Any, DataModel.__new__(DataModel))
    QAbstractTableModel.__init__(model)
    model._state = SimpleNamespace(active_columns=[f"c{i}" for i in range(columns)])
    model._items = list(range(1000, 1000 + rows))
    model._rows = {}
    model._block_updates = False
    for row in range(0, rows, 3):
        model._rows[model._items[row]] = CellRow.generic(columns, "text")
    for row in disabled:
        model._rows[model._items[row]] = CellRow.disabled(columns, "deleted")
    return model


def random_selection(rng: random.Random, model: Any) -> QItemSelection:
    rows, columns = model.rowCount(), model.columnCount()
    selection = QItemSelection()
    for _ in range(rng.randint(0, 6)):
        top = rng.randrange(rows)
        bottom = rng.randrange(top, rows)
        left = rng.randrange(columns)
        right = rng.randrange(left, columns)
        # append, not select, so ranges may overlap and cells may repeat
        selection.append(
            QItemSelectionRange(model.index(top, left), model.index(bottom, right))
        )
    return selection


@pytest.mark.parametrize("seed", range(40))
def test_enabled_cells_are_counted_as_qt_counts_them(seed: int) -> None:
    rng = random.Random(seed)
    rows, columns = rng.randint(1, 80), rng.randint(1, 7)
    disabled = rng.sample(range(rows), rng.randint(0, rows // 3))
    model = make_model(rows, columns, disabled)
    selection = random_selection(rng, model)
    assert model.count_enabled_cells(selection) == len(selection.indexes())
    # the ids as a search returns them
    model._items = search_ids(list(model._items))
    assert model.count_enabled_cells(selection) == len(selection.indexes())


def test_whole_table_and_empty_selection_are_counted_as_qt_counts_them() -> None:
    for disabled in ([], [0, 5, 49]):
        model = make_model(50, 6, disabled)
        everything = QItemSelection(model.index(0, 0), model.index(49, 5))
        assert model.count_enabled_cells(everything) == len(everything.indexes())
        assert model.count_enabled_cells(QItemSelection()) == 0


class ModifiersPressed:
    shift = False
    control = True


def test_ctrl_selection_change_counts_rows(monkeypatch: Any) -> None:
    monkeypatch.setattr(table_module, "KeyboardModifiersPressed", ModifiersPressed)
    model = make_model(50, 6, [7, 8])
    table = cast(Any, Table.__new__(Table))
    table._model = model
    table._len_selection = 1
    table._selected_rows = None
    table.browser = SimpleNamespace(on_all_or_selected_rows_changed=lambda: None)

    # Ctrl+A with only row 3 selected before: 49 rows are added, and the
    # deleted rows 7 and 8 do not count
    selected = QItemSelection(model.index(0, 0), model.index(2, 5))
    selected.merge(
        QItemSelection(model.index(4, 0), model.index(49, 5)),
        table_module.QItemSelectionModel.SelectionFlag.Select,
    )
    Table._on_selection_changed(table, selected, QItemSelection())
    assert table._len_selection == 48

    # Ctrl+click on row 3 again removes it
    deselected = QItemSelection(model.index(3, 0), model.index(3, 5))
    Table._on_selection_changed(table, QItemSelection(), deselected)
    assert table._len_selection == 47


def search_ids(ids: list[int]) -> Any:
    """Ids in the container a search returns (a protobuf repeated field)."""
    from anki import search_pb2

    return search_pb2.SearchResponse(ids=ids).ids


def rows_by_scan(table_items: Any, items: Any) -> list[int]:
    return [row for row, item in enumerate(table_items) if item in set(items)]


def row_by_scan(table_items: Any, item: int) -> int | None:
    return next((row for row, i in enumerate(table_items) if i == item), None)


@pytest.mark.parametrize("seed", range(60))
def test_item_rows_match_a_scan_of_the_table(seed: int) -> None:
    rng = random.Random(seed)
    rows = rng.randint(0, 60)
    # repeated ids too, which a hook may return
    ids = [rng.randint(1, 40) for _ in range(rows)]
    for table_items in (ids, search_ids(ids)):
        model = make_model(0, 1, [])
        model._items = table_items
        wanted = [rng.randint(1, 45) for _ in range(rng.randint(0, 8))]
        assert model.get_item_rows(wanted) == rows_by_scan(ids, wanted)
        for item in range(0, 46):
            assert model.get_item_row(item) == row_by_scan(ids, item)
            if item in wanted:
                rows_of_wanted = model.get_item_rows(wanted)
                assert model.get_item_row_among(item, rows_of_wanted) == row_by_scan(
                    ids, item
                )


@pytest.mark.parametrize("seed", range(60))
def test_restored_selection_matches_a_scan_of_the_table(seed: int) -> None:
    rng = random.Random(seed)
    ids = [rng.randint(1, 30) for _ in range(rng.randint(1, 50))]
    model = make_model(0, 1, [])
    model._items = search_ids(ids)
    table = cast(Any, Table.__new__(Table))
    table._model = model
    selected = [rng.randint(1, 35) for _ in range(rng.randint(0, 6))]
    # the current item selected, not selected, missing, or unset
    current = rng.choice([None, rng.randint(1, 35)] + selected)
    table._selected_items = selected
    table._current_item = current
    expected_current = current and row_by_scan(ids, current)
    assert Table._intersected_selection(table) == (
        rows_by_scan(ids, selected),
        expected_current,
    )
    # toggling cards/notes mode maps the items first
    table._state = SimpleNamespace(
        get_new_items=lambda items: [item * 7 % 31 for item in items]
    )
    new_ids = [rng.randint(0, 30) for _ in range(len(ids))]
    model._items = search_ids(new_ids)
    new_selected = [item * 7 % 31 for item in selected]
    expected_current = row_by_scan(new_ids, current * 7 % 31) if current else None
    assert Table._toggled_selection(table) == (
        rows_by_scan(new_ids, new_selected),
        expected_current,
    )


def test_enabled_cells_respect_addon_flags(monkeypatch: Any) -> None:
    model = make_model(4, 2, [])

    def addon_flags(self: Any, index: QModelIndex) -> Qt.ItemFlag:
        if index.row() == 1:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    monkeypatch.setattr(DataModel, "flags", addon_flags)
    selection = QItemSelection(model.index(0, 0), model.index(3, 1))
    assert model.count_enabled_cells(selection) == 6
    assert len(selection.indexes()) == 6


@pytest.mark.parametrize("centered", [False, True])
def test_table_display_and_alignment_preserve_qt_values(centered: bool) -> None:
    from anki.collection import BrowserColumns as Columns

    model = make_model(1, 1, [])
    model._stale_cutoff = 0.0
    model._want_tooltips = True
    model._state.column_key_at = lambda _section: "c0"
    model.columns = {
        "c0": SimpleNamespace(
            uses_cell_font=False,
            alignment=Columns.ALIGNMENT_CENTER if centered else Columns.ALIGNMENT_START,
        )
    }
    index = model.index(0, 0)
    alignment = Qt.AlignmentFlag.AlignVCenter
    if centered:
        alignment |= Qt.AlignmentFlag.AlignHCenter
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "text"
    assert model.data(index, Qt.ItemDataRole.ToolTipRole) == "text"
    actual_alignment = model.data(index, Qt.ItemDataRole.TextAlignmentRole)
    assert actual_alignment == alignment.value
    assert type(actual_alignment) is int
    flags = model.flags(index)
    assert flags == Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    assert isinstance(flags, Qt.ItemFlag)
