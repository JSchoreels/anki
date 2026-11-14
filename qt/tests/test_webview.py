# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from anki.collection import OpChanges
from aqt.qt import QWebEngineUrlRequestInfo
from aqt.webview import AnkiWebView, AuthInterceptor

ResourceType = QWebEngineUrlRequestInfo.ResourceType


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


@pytest.mark.parametrize(
    ("enabled", "internal", "resource_type", "method", "authorized"),
    [
        (True, True, ResourceType.ResourceTypeXhr, b"POST", True),
        (True, True, ResourceType.ResourceTypeXhr, b"GET", False),
        (True, True, ResourceType.ResourceTypeSubFrame, b"POST", False),
        (True, True, ResourceType.ResourceTypeSubFrame, b"GET", False),
        (True, False, ResourceType.ResourceTypeXhr, b"POST", False),
        (False, True, ResourceType.ResourceTypeXhr, b"POST", False),
    ],
)
def test_auth_interceptor_only_authorizes_internal_post_requests_from_xhr(
    monkeypatch, enabled, internal, resource_type, method, authorized
) -> None:
    from aqt import mediasrv, webview

    monkeypatch.setattr(webview, "_is_internal_url", lambda _: internal)
    request = MagicMock()
    request.resourceType.return_value = resource_type
    request.requestMethod.return_value = method

    AuthInterceptor(api_enabled=enabled).interceptRequest(request)

    if authorized:
        request.setHttpHeader.assert_called_once_with(
            b"Authorization", f"Bearer {mediasrv._APIKEY}".encode("utf-8")
        )
    else:
        request.setHttpHeader.assert_not_called()
