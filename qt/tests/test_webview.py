# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from unittest.mock import MagicMock

from anki.collection import OpChanges
from aqt.webview import AnkiWebView


def test_operation_from_own_window_is_not_forwarded_to_webview() -> None:
    webview = MagicMock()
    webview.parentWidget.return_value = object()
    own_window = object()
    webview.window.return_value = own_window

    AnkiWebView.on_operation_did_execute(webview, OpChanges(), own_window)

    webview.eval.assert_not_called()


def test_operation_from_other_window_is_forwarded_to_webview() -> None:
    webview = MagicMock()
    webview.parentWidget.return_value = object()
    webview.window.return_value = object()
    changes = OpChanges()
    changes.note_text = True

    AnkiWebView.on_operation_did_execute(webview, changes, object())

    webview.eval.assert_called_once()
