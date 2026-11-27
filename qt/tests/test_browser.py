# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any, cast

import pytest

import aqt.rwkv_scheduler
from anki.collection import SearchNode
from aqt.browser.browser import Browser


class SearchRecorder:
    def __init__(self) -> None:
        self.search: str | None = None
        self.prompt: str | None = None

    def __call__(self, search: str, prompt: str | None = None) -> None:
        self.search = search
        self.prompt = prompt


class Col:
    def __init__(self, default_search: str) -> None:
        self.default_search = default_search

    def get_config_string(self, _key: Any) -> str:
        return self.default_search

    def build_search_string(self, node: SearchNode) -> str:
        assert node.deck == "current"
        return "deck:current"


def test_default_browser_search_shows_current_deck_scope() -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.col = Col(default_search="")
    search_for = SearchRecorder()
    browser.search_for = search_for

    browser._default_search()

    assert search_for.search == "deck:current"
    assert search_for.prompt == "deck:current"


def test_configured_default_browser_search_is_shown_unchanged() -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.col = Col(default_search="is:due")
    search_for = SearchRecorder()
    browser.search_for = search_for

    browser._default_search()

    assert search_for.search == "is:due"
    assert search_for.prompt == "is:due"


class TableSearchRecorder:
    def __init__(self) -> None:
        self.searches: list[str] = []

    def search(self, search: str) -> None:
        self.searches.append(search)


class ImmediateQueryOp:
    def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
        self.op = op
        self.success = success

    def with_progress(self) -> ImmediateQueryOp:
        return self

    def run_in_background(self) -> None:
        self.success(self.op(None))


@pytest.mark.parametrize(
    "query",
    ["prop:rwkv:r<0.95", "prop:rwkv-curve:r<0.95"],
)
def test_rwkv_browser_search_prepares_scores_before_searching(
    monkeypatch: Any,
    query: str,
) -> None:
    from aqt.browser import browser as browser_module

    browser = cast(Any, Browser.__new__(Browser))
    browser.mw = object()
    browser.table = TableSearchRecorder()
    browser._lastSearchTxt = query
    browser._rwkv_search_generation = 0
    browser._closeEventHasCleanedUp = False
    prepared: list[str] = []

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search: prepared.append(search),
    )

    browser.search()

    assert prepared == [query]
    assert browser.table.searches == [query]


def test_is_due_browser_search_prepares_scores_with_instant_presets(
    monkeypatch: Any,
) -> None:
    from aqt.browser import browser as browser_module

    browser = cast(Any, Browser.__new__(Browser))
    browser.mw = object()
    browser.table = TableSearchRecorder()
    browser._lastSearchTxt = "deck:current is:due"
    browser._rwkv_search_generation = 0
    browser._closeEventHasCleanedUp = False
    prepared: list[str] = []

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(aqt.rwkv_scheduler, "rwkv_instant_due_enabled", lambda _r: True)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search: prepared.append(search),
    )

    browser.search()

    assert prepared == ["deck:current is:due"]
    # The search runs even when preparation did not finish.
    assert browser.table.searches == ["deck:current is:due"]


def test_non_rwkv_browser_search_runs_without_preparation(monkeypatch: Any) -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.mw = object()
    browser.table = TableSearchRecorder()
    browser._lastSearchTxt = "prop:r<0.95"
    browser._rwkv_search_generation = 0
    prepared: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search: prepared.append(search),
    )

    browser.search()

    assert prepared == []
    assert browser.table.searches == ["prop:r<0.95"]
