# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import aqt
import aqt.browser
from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
from aqt.qt import *
from aqt.theme import theme_manager

# Qt asks for the flags of every index it touches, so a sidebar refresh or a
# keystroke in the filter box runs flags() thousands of times. The four
# possible results are built once here instead of with enum ORs per call.
_FLAGS_INVALID = Qt.ItemFlag.ItemIsEnabled
_FLAGS_BASE = (
    Qt.ItemFlag.ItemIsEnabled
    | Qt.ItemFlag.ItemIsSelectable
    | Qt.ItemFlag.ItemIsDragEnabled
)
_FLAGS_EDITABLE = _FLAGS_BASE | Qt.ItemFlag.ItemIsEditable
_FLAGS_DROPPABLE = _FLAGS_BASE | Qt.ItemFlag.ItemIsDropEnabled
_FLAGS_DROPPABLE_EDITABLE = _FLAGS_DROPPABLE | Qt.ItemFlag.ItemIsEditable
_EDITABLE_TYPES = frozenset(
    item_type for item_type in SidebarItemType if item_type.is_editable()
)


class SidebarModel(QAbstractItemModel):
    def __init__(
        self, sidebar: aqt.browser.sidebar.SidebarTreeView, root: SidebarItem
    ) -> None:
        super().__init__(sidebar)
        self.sidebar = sidebar
        self.root = root
        self._cache_rows(root)

    def _cache_rows(self, node: SidebarItem) -> None:
        "Cache index of children in parent."
        for row, item in enumerate(node.children):
            item._row_in_parent = row
            self._cache_rows(item)

    def item_for_index(self, idx: QModelIndex) -> SidebarItem:
        return idx.internalPointer()

    def index_for_item(self, item: SidebarItem) -> QModelIndex:
        assert item._row_in_parent is not None
        return self.createIndex(item._row_in_parent, 0, item)

    def search(self, text: str) -> bool:
        return self.root.search(text.lower())

    # Qt API
    ######################################################################

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self.root.children)
        else:
            item: SidebarItem = parent.internalPointer()
            return len(item.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def index(
        self, row: int, column: int, parent: QModelIndex = QModelIndex()
    ) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parentItem: SidebarItem
        if not parent.isValid():
            parentItem = self.root
        else:
            parentItem = parent.internalPointer()

        item = parentItem.children[row]
        return self.createIndex(row, column, item)

    def parent(self, child: QModelIndex) -> QModelIndex:  # type: ignore
        if not child.isValid():
            return QModelIndex()

        childItem: SidebarItem = child.internalPointer()
        parentItem = childItem._parent_item

        if parentItem is None or parentItem == self.root:
            return QModelIndex()

        row = parentItem._row_in_parent
        assert row is not None

        return self.createIndex(row, 0, parentItem)

    def data(
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> QVariant:
        if not index.isValid():
            return QVariant()

        if role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.DecorationRole,
            Qt.ItemDataRole.ToolTipRole,
            Qt.ItemDataRole.EditRole,
        ):
            return QVariant()

        item: SidebarItem = index.internalPointer()

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return QVariant(item.name)
        if role == Qt.ItemDataRole.ToolTipRole:
            return QVariant(item.tooltip)
        return QVariant(theme_manager.icon_from_resources(item.icon))

    def setData(
        self, index: QModelIndex, text: str, _role: int = Qt.ItemDataRole.EditRole
    ) -> bool:
        return self.sidebar._on_rename(index.internalPointer(), text)

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return _FLAGS_INVALID
        item: SidebarItem = index.internalPointer()
        item_type = item.item_type
        editable = item_type in _EDITABLE_TYPES
        if item_type in self.sidebar.valid_drop_types:
            return _FLAGS_DROPPABLE_EDITABLE if editable else _FLAGS_DROPPABLE
        return _FLAGS_EDITABLE if editable else _FLAGS_BASE
