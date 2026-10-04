# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
from aqt.browser.sidebar.model import SidebarModel
from aqt.browser.sidebar.tree import SidebarTreeView
from aqt.qt import QAbstractItemModel, QModelIndex, Qt


def make_model(root: SidebarItem, drop_types: tuple[SidebarItemType, ...] = ()) -> Any:
    model = cast(Any, SidebarModel.__new__(SidebarModel))
    QAbstractItemModel.__init__(model)
    model.root = root
    model.sidebar = SimpleNamespace(valid_drop_types=drop_types)
    model._cache_rows(root)
    return model


@pytest.mark.parametrize("item_type", list(SidebarItemType))
@pytest.mark.parametrize("droppable", [False, True])
def test_sidebar_flags_preserve_edit_and_drop_permissions(
    item_type: SidebarItemType, droppable: bool
) -> None:
    root = SidebarItem("root", "")
    item = SidebarItem("item", "", item_type=item_type)
    root.add_child(item)
    model = make_model(root, (item_type,) if droppable else ())
    flags = model.flags(model.index_for_item(item))
    assert isinstance(flags, Qt.ItemFlag)
    assert bool(flags & Qt.ItemFlag.ItemIsEnabled)
    assert bool(flags & Qt.ItemFlag.ItemIsSelectable)
    assert bool(flags & Qt.ItemFlag.ItemIsDragEnabled)
    assert bool(flags & Qt.ItemFlag.ItemIsEditable) == (
        item_type
        in (
            SidebarItemType.FLAG,
            SidebarItemType.SAVED_SEARCH,
            SidebarItemType.DECK,
            SidebarItemType.TAG,
        )
    )
    assert bool(flags & Qt.ItemFlag.ItemIsDropEnabled) == droppable
    assert model.flags(QModelIndex()) == Qt.ItemFlag.ItemIsEnabled


def test_sidebar_sections_keep_declaration_order() -> None:
    assert list(SidebarItemType.section_roots()) == [
        SidebarItemType.SAVED_SEARCH_ROOT,
        SidebarItemType.TODAY_ROOT,
        SidebarItemType.FLAG_ROOT,
        SidebarItemType.CARD_STATE_ROOT,
        SidebarItemType.DECK_ROOT,
        SidebarItemType.NOTETYPE_ROOT,
        SidebarItemType.TAG_ROOT,
    ]
    for item_type in SidebarItemType:
        assert item_type.is_section_root() == item_type.name.endswith("_ROOT")


@pytest.mark.parametrize("subtree_only", [False, True])
def test_sidebar_search_expands_matches_and_selects_first_descendant(
    subtree_only: bool,
) -> None:
    root = SidebarItem("root", "")
    decks = SidebarItem("match decks", "", item_type=SidebarItemType.DECK_ROOT)
    first = SidebarItem("match first", "", item_type=SidebarItemType.DECK)
    second = SidebarItem("match second", "", item_type=SidebarItemType.DECK)
    decks.add_child(first)
    decks.add_child(second)
    root.add_child(decks)
    model = make_model(root)
    model.search("match")
    expanded: list[str] = []
    selected: list[str] = []
    scrolled: list[str] = []
    selection = SimpleNamespace(
        setCurrentIndex=lambda index, _flags: selected.append(
            index.internalPointer().name
        )
    )
    view = SimpleNamespace(
        setExpanded=lambda index, _expanded: expanded.append(
            index.internalPointer().name
        ),
        _selection_model=lambda: selection,
        scrollTo=lambda index, _hint: scrolled.append(index.internalPointer().name),
    )

    SidebarTreeView._expand_where_necessary(
        cast(Any, view),
        model,
        parent=model.index_for_item(decks) if subtree_only else None,
        searching=True,
    )

    assert expanded == ([] if subtree_only else ["match decks"])
    assert selected == ["match first"]
    assert scrolled == ["match first"]
    assert first.is_highlighted() and second.is_highlighted()
