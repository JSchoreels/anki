# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any, cast

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
