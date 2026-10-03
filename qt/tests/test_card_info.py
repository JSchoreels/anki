# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from anki.collection import OpChanges
from aqt import gui_hooks
from aqt.browser import card_info


@pytest.fixture
def open_card_info(monkeypatch):
    updates: list[int] = []

    def create_dialog(_parent, _mw, _card, on_close, *_args):
        return SimpleNamespace(
            update_card=updates.append,
            reject=on_close,
            activateWindow=lambda: None,
            raise_=lambda: None,
        )

    monkeypatch.setattr(card_info, "CardInfoDialog", create_dialog)
    manager = card_info.CardInfoManager(cast(Any, object()), "test", "Card Info")
    manager.set_card(cast(Any, SimpleNamespace(id=123)))
    manager.show()
    try:
        yield manager, updates
    finally:
        manager.close()


@pytest.mark.parametrize("changed", ["deck", "deck_config", "config", "card"])
def test_card_info_refreshes_when_deck_or_preset_settings_change(
    open_card_info, changed: str
) -> None:
    _manager, updates = open_card_info
    changes = OpChanges()
    setattr(changes, changed, True)

    gui_hooks.operation_did_execute(changes, None)

    assert updates == ([] if changed == "card" else [123])


def test_card_info_refreshes_when_its_rwkv_state_becomes_ready(open_card_info) -> None:
    manager, updates = open_card_info
    manager.show()

    gui_hooks.rwkv_state_did_prepare(object())
    assert updates == []

    gui_hooks.rwkv_state_did_prepare(manager.mw)
    assert updates == [123]


def test_closed_card_info_does_not_refresh_after_rwkv_recovery(open_card_info) -> None:
    manager, updates = open_card_info
    manager.close()

    gui_hooks.rwkv_state_did_prepare(manager.mw)
    gui_hooks.operation_did_execute(OpChanges(deck_config=True), None)

    assert updates == []
