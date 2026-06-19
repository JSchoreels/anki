# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any, cast

from anki.decks import DeckId
from aqt.stats import NewDeckStats


class DeckChooser:
    selected_deck_id: DeckId

    def __init__(self, deck_id: int) -> None:
        self.selected_deck_id = DeckId(deck_id)


class Web:
    def __init__(self) -> None:
        self.loaded_paths: list[str] = []

    def load_sveltekit_page(self, path: str) -> None:
        self.loaded_paths.append(path)


class Form:
    def __init__(self) -> None:
        self.web = Web()


def test_new_stats_refresh_url_changes_with_selected_deck() -> None:
    stats = cast(Any, NewDeckStats.__new__(NewDeckStats))
    stats.deck_chooser = DeckChooser(123)
    stats.form = Form()

    stats.refresh()
    stats.deck_chooser.selected_deck_id = DeckId(456)
    stats.refresh()

    assert stats.form.web.loaded_paths == [
        "graphs?currentDeckId=123",
        "graphs?currentDeckId=456",
    ]
