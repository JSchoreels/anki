# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


def test_screen_boundary_retry_skips_deleted_widget(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aqt
    from aqt.qt import QWidget, sip
    from aqt.utils import ensureWidgetInScreenBoundaries

    callbacks = []
    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            progress=SimpleNamespace(
                timer=lambda ms, callback, repeat, parent: callbacks.append(callback)
            )
        ),
        raising=False,
    )
    widget = QWidget()
    ensureWidgetInScreenBoundaries(widget)
    assert len(callbacks) == 1

    sip.delete(widget)
    callbacks.pop()()

    assert callbacks == []


@pytest.mark.parametrize("active_window", [False, True])
def test_show_info_uses_available_window_when_parent_deleted(
    app: Any, monkeypatch: pytest.MonkeyPatch, active_window: bool
) -> None:
    import aqt
    from aqt import utils
    from aqt.qt import QMessageBox, QWidget, sip

    mw = QWidget()
    active = QWidget() if active_window else None
    mw.app = SimpleNamespace(activeWindow=lambda: active)  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", mw, raising=False)
    parents = []

    def exec_dialog(dialog: QMessageBox) -> int:
        parents.append(dialog.parent())
        return 1

    monkeypatch.setattr(QMessageBox, "exec", exec_dialog)
    closed = QWidget()
    sip.delete(closed)

    assert utils.showInfo("Completed", parent=closed) == 1
    assert parents == [active or mw]


@pytest.mark.parametrize("active_window", [False, True])
def test_tooltip_uses_available_window_when_parent_deleted(
    app: Any, monkeypatch: pytest.MonkeyPatch, active_window: bool
) -> None:
    import aqt
    from aqt import utils
    from aqt.qt import QTimer, QWidget, sip

    mw = QWidget()
    active = QWidget() if active_window else None
    mw.app = SimpleNamespace(activeWindow=lambda: active)  # type: ignore[attr-defined]
    mw.progress = SimpleNamespace(  # type: ignore[attr-defined]
        timer=lambda *args, **kwargs: QTimer(kwargs["parent"])
    )
    monkeypatch.setattr(aqt, "mw", mw, raising=False)
    closed = QWidget()
    sip.delete(closed)

    try:
        utils.tooltip("Completed", parent=closed)

        assert utils._tooltipLabel is not None
        assert utils._tooltipLabel.parent() is (active or mw)
        assert utils._tooltipLabel.isVisible()
    finally:
        utils.closeTooltip()
        app.processEvents()
