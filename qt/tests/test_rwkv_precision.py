# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import weakref
from types import SimpleNamespace

import pytest

from anki._backend import RustBackend
from aqt.preferences import Preferences
from aqt.qt import QApplication, QDialog, Qt, QVBoxLayout
from aqt.rwkv_precision import state_precision_for_profile
from aqt.utils import tr


@pytest.mark.parametrize(
    "profile, expected",
    [
        (None, "int8"),
        ({}, "int8"),
        ({"rwkvStatePrecision": "unknown"}, "int8"),
        ({"rwkvStatePrecision": "f32"}, "f32"),
        ({"rwkvStatePrecision": "int4"}, "int4"),
    ],
)
def test_state_precision_for_profile_preserves_supported_choices(profile, expected):
    assert state_precision_for_profile(profile) == expected


def test_rwkv_preferences_selector_saves_choice_for_next_restart(monkeypatch):
    from PyQt6.QtTest import QTest

    # Exercise real widgets and the production save handler without an Anki
    # window or a user's collection/profile.
    application = QApplication.instance() or QApplication([])
    translations = RustBackend(langs=["en"])
    monkeypatch.setattr(tr, "backend", weakref.ref(translations))
    dialog = Preferences.__new__(Preferences)
    QDialog.__init__(dialog)
    dialog.prof = {"rwkvStatePrecision": "int8"}
    dialog.form = SimpleNamespace(review_options_layout=QVBoxLayout(dialog))
    monkeypatch.setattr(dialog, "update_network", lambda: None)
    dialog.setup_rwkv_precision()
    assert dialog.rwkv_precision.currentData() == "int8"
    assert dialog.rwkv_model.currentData() == "legacy"
    dialog.show()
    application.processEvents()
    QTest.keyClick(dialog.rwkv_model, Qt.Key.Key_End)
    assert dialog.rwkv_precision.itemText(2)
    QTest.keyClick(dialog.rwkv_precision, Qt.Key.Key_Home)
    dialog.update_profile()
    assert dialog.prof["rwkvStatePrecision"] == "f32"
    assert dialog.prof["rwkvModel"] == "sm40"
    QTest.keyClick(dialog.rwkv_model, Qt.Key.Key_Home)
    QTest.keyClick(dialog.rwkv_precision, Qt.Key.Key_End)
    dialog.update_profile()
    assert dialog.prof["rwkvStatePrecision"] == "int4"
    assert dialog.prof["rwkvModel"] == "legacy"
    dialog.deleteLater()
    application.processEvents()
