# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Benchmark actual Browser classes in an explicitly selected built checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast


def checksum(result: Any) -> str:
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def measure(
    name: str, workload: str, operation: Callable[[], Any], samples: int
) -> dict[str, Any]:
    expected = checksum(operation())
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        result = operation()
        timings.append((time.perf_counter() - started) * 1000)
        if checksum(result) != expected:
            raise RuntimeError(f"Non-deterministic output for {name}")
    quartiles = statistics.quantiles(timings, n=4, method="inclusive")
    return {
        "name": name,
        "workload": workload,
        "samples": samples,
        "median_ms": statistics.median(timings),
        "p25_ms": quartiles[0],
        "p75_ms": quartiles[2],
        "min_ms": min(timings),
        "max_ms": max(timings),
        "result_checksum": expected,
    }


def benchmark(source_root: Path, samples: int) -> dict[str, Any]:
    sys.path[:0] = [
        str(source_root / path)
        for path in (
            "pylib",
            "out/pylib",
            "qt",
            "out/qt",
        )
    ]
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

    import aqt.browser.table.table as table_module
    from anki.collection import BrowserColumns as Columns
    from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
    from aqt.browser.sidebar.model import SidebarModel
    from aqt.browser.sidebar.tree import SidebarTreeView
    from aqt.browser.table import CellRow
    from aqt.browser.table.model import DataModel
    from aqt.browser.table.table import StatusDelegate, Table
    from aqt.qt import (
        QAbstractTableModel,
        QApplication,
        QImage,
        QItemDelegate,
        QItemSelection,
        QModelIndex,
        QPainter,
        QRect,
        QStyleOptionViewItem,
        Qt,
        QTreeView,
    )

    app = QApplication.instance() or QApplication([])
    assert app
    model = cast(Any, DataModel.__new__(DataModel))
    QAbstractTableModel.__init__(model)
    keys = [f"column{i}" for i in range(8)]
    model._state = SimpleNamespace(
        active_columns=keys,
        column_key_at=lambda section: keys[section],
    )
    model._items = list(range(1000, 21000))
    model._rows = {}
    model._block_updates = True
    model._stale_cutoff = 0.0
    model._want_tooltips = True
    model.columns = {
        key: SimpleNamespace(uses_cell_font=True, alignment=Columns.ALIGNMENT_CENTER)
        for key in keys
    }
    for item in model._items[:41]:
        model._rows[item] = CellRow.generic(8, "Representative Browser text")
    model._rows[model._items[49]] = CellRow.disabled(8, "deleted")
    model._rows[model._items[4999]] = CellRow.disabled(8, "deleted")
    selected = model._items[::4]
    table = cast(Any, Table.__new__(Table))
    table._model = model
    table._selected_items = selected
    table._current_item = selected[-1]
    table.browser = SimpleNamespace(on_all_or_selected_rows_changed=lambda: None)
    rows = []
    rows.append(
        measure(
            "browser_selected_row_lookup",
            "20,000 list-backed rows; 5,000 selected IDs",
            lambda: model.get_item_rows(selected),
            samples,
        )
    )
    rows.append(
        measure(
            "browser_selection_restoration",
            "20,000 rows; 5,000 selected; current ID selected",
            table._intersected_selection,
            samples,
        )
    )

    selection = QItemSelection(model.index(0, 0), model.index(4999, 7))
    original_modifiers = table_module.KeyboardModifiersPressed
    setattr(
        table_module,
        "KeyboardModifiersPressed",
        lambda: SimpleNamespace(shift=False, control=True),
    )

    def count_selection() -> int:
        table._len_selection = 0
        table._on_selection_changed(selection, QItemSelection())
        return cast(int, table._len_selection)

    try:
        rows.append(
            measure(
                "browser_ctrl_selection_count",
                "5,000 rows x 8 columns; two cached deleted rows",
                count_selection,
                samples,
            )
        )
    finally:
        setattr(table_module, "KeyboardModifiersPressed", original_modifiers)

    indices = [model.index(row, column) for row in range(41) for column in range(8)]

    def metadata() -> list[int]:
        text_length = alignment = font_size = flags = 0
        for index in indices:
            text_length += len(model.data(index, Qt.ItemDataRole.DisplayRole))
            text_length += len(model.data(index, Qt.ItemDataRole.ToolTipRole))
            alignment += model.data(index, Qt.ItemDataRole.TextAlignmentRole)
            font_size += model.data(index, Qt.ItemDataRole.FontRole).pixelSize()
            model.data(index, Qt.ItemDataRole.BackgroundRole)
            flags += model.flags(index).value
        return [text_length, alignment, font_size, flags]

    rows.append(
        measure(
            "browser_table_metadata",
            "41 cached rows x 8 columns; 5 roles and flags per cell; no painting",
            metadata,
            samples,
        )
    )
    delegate = cast(Any, StatusDelegate.__new__(StatusDelegate))
    QItemDelegate.__init__(delegate)
    delegate._model = model
    image = QImage(960, 1025, QImage.Format.Format_ARGB32)
    option = QStyleOptionViewItem()

    def paint_table() -> str:
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        try:
            for index in indices:
                option.rect = QRect(index.column() * 120, index.row() * 25, 120, 25)
                delegate.paint(painter, option, index)
        finally:
            painter.end()
        # Hashing is included equally in both versions and reported explicitly.
        return hashlib.sha256(
            image.constBits().asstring(image.sizeInBytes())
        ).hexdigest()

    rows.append(
        measure(
            "browser_table_delegate_paint",
            "41 cached rows x 8 columns; QImage offscreen paint plus pixel SHA256",
            paint_table,
            samples,
        )
    )

    tree = cast(Any, SidebarTreeView.__new__(SidebarTreeView))
    QTreeView.__init__(tree)
    tree.valid_drop_types = (SidebarItemType.DECK,)
    root = SidebarItem("root", "")
    items = []
    for section in range(7):
        section_item = SidebarItem(
            f"section {section}", "", item_type=SidebarItemType.DECK_ROOT
        )
        root.add_child(section_item)
        items.append(section_item)
        for child in range(199):
            item = SidebarItem(
                f"deck {section} {child}", "", item_type=SidebarItemType.DECK
            )
            section_item.add_child(item)
            items.append(item)
    sidebar_model = SidebarModel(tree, root)
    tree.setModel(sidebar_model)
    sidebar_indices = [sidebar_model.index_for_item(item) for item in items]
    rows.append(
        measure(
            "browser_sidebar_flags",
            "1,400 sidebar items; editable/droppable decks and section roots",
            lambda: sum(sidebar_model.flags(index).value for index in sidebar_indices),
            samples,
        )
    )
    rows.append(
        measure(
            "browser_sidebar_section_roots",
            "1,400 item type section-root checks",
            lambda: sum(item.item_type.is_section_root() for item in items),
            samples,
        )
    )

    def filter_sidebar() -> list[Any]:
        tree.collapseAll()
        sidebar_model.search("deck 0")
        tree._expand_where_necessary(sidebar_model, searching=True)
        current = sidebar_model.item_for_index(tree.currentIndex())
        return [
            current.name,
            [
                item.name
                for item, index in zip(items, sidebar_indices)
                if tree.isExpanded(index)
            ],
            sum(item.is_highlighted() for item in items),
        ]

    rows.append(
        measure(
            "browser_sidebar_filter_expansion",
            "1,400 items; filter, collapse, expand and select first match in real hidden QTreeView",
            filter_sidebar,
            samples,
        )
    )
    tree.deleteLater()
    app.processEvents()
    return {
        "source_root": str(source_root),
        "python": sys.version,
        "platform": sys.platform,
        "qt_platform": "offscreen",
        "limitations": [
            "Synthetic IDs/cached rows; no database, profile, search backend or add-ons.",
            "Microbenchmarks isolate Browser work; they are not whole-window latency.",
            "Delegate benchmark includes identical image reset and pixel-hash overhead.",
            "Sidebar uses a hidden real Qt tree; visible layout and event scheduling are excluded.",
        ],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=21)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    result = benchmark(args.source_root.resolve(), args.samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
