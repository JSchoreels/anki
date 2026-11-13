# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtTest import QTest

import aqt
from anki.deck_config_pb2 import UpdateDeckConfigsRequest
from aqt import mediasrv, rwkv_scheduler
from aqt.qt import QApplication, QEventLoop, QMessageBox, Qt, QTimer, QWidget
from aqt.rwkv_model import RwkvRuntimeModels
from aqt.rwkv_srs_benchmark import EmbeddedRwkvReviewerBackend


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _owner(app: QApplication, *, curve: bool, instant: bool) -> QWidget:
    owner = QWidget()
    owner.app = app  # type: ignore[attr-defined]
    owner.state = "deckBrowser"  # type: ignore[attr-defined]
    owner.pm = SimpleNamespace(  # type: ignore[attr-defined]
        profile={"rwkvModel": "sm72e25", "rwkvStatePrecision": "int8"},
        save=MagicMock(),
    )
    owner.col = SimpleNamespace(  # type: ignore[attr-defined]
        decks=SimpleNamespace(
            all_config=lambda: [
                {
                    "id": 1,
                    "rwkvReviewEnabled": curve,
                    "rwkvReviewInstantOrderEnabled": instant,
                }
            ]
        ),
        get_config=lambda _key: None,
    )
    return owner


def _backend(model: str, instant_model: str | None) -> EmbeddedRwkvReviewerBackend:
    backend = EmbeddedRwkvReviewerBackend.__new__(EmbeddedRwkvReviewerBackend)
    backend.model_name = model
    backend.instant_model_name = instant_model
    backend.state_precision = "int8"
    return backend


def _save_tiny() -> None:
    request = UpdateDeckConfigsRequest()
    request.rwkv_profile_settings.model = "tiny"
    request.rwkv_profile_settings.state_precision = "int8"
    mediasrv._on_update_deck_configs_success(request, close_on_success=True)


@pytest.mark.parametrize(
    "button,calibration",
    [("Build State Only", False), ("Build State + Calibration Data", True)],
)
@pytest.mark.parametrize("previous_operation", [False, True])
def test_saved_model_change_prompts_and_builds_without_reopening(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    button: str,
    calibration: bool,
    previous_operation: bool,
) -> None:
    owner = _owner(app, curve=True, instant=True)
    owner._rwkv_state_cache_loading = previous_operation  # type: ignore[attr-defined]
    owner.show()
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    builds: list[tuple[RwkvRuntimeModels, bool, bool]] = []

    def build(_owner: object, **kwargs: object) -> None:
        builds.append(
            (
                rwkv_scheduler._rwkv_runtime_models_for_profile(owner),
                bool(kwargs.get("force_rebuild")),
                bool(kwargs.get("record_retrievability_cache")),
            )
        )

    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    clicked: list[str] = []

    def click_prompt() -> None:
        for widget in QApplication.topLevelWidgets():
            if not isinstance(widget, QMessageBox) or not widget.isVisible():
                continue
            for candidate in widget.buttons():
                if candidate.text() == button:
                    clicked.append(button)
                    candidate.click()
                    loop.quit()
                    return

    timer.timeout.connect(click_prompt)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    try:
        _save_tiny()
        timer.start()
        timeout.start(2_000)
        loop.exec()
        assert clicked == [button]
        assert owner.pm.profile["rwkvModel"] == "tiny"  # type: ignore[attr-defined]
        if previous_operation:
            app.processEvents()
            assert not rwkv_scheduler.rwkv_state_declined(owner)
            assert builds == []
            rwkv_scheduler._finish_rwkv_state_cache_operation(
                owner, ready=False, prewarm_reason="previous operation"
            )
        assert builds == [(RwkvRuntimeModels("sm106", "sm101"), True, calibration)]
    finally:
        timer.stop()
        timeout.stop()
        for message in owner.findChildren(QMessageBox):
            message.close()
        owner.close()


@pytest.mark.parametrize(
    "curve,instant,models",
    [
        (False, True, ("sm98", None)),
        (True, False, ("sm92", None)),
    ],
)
def test_one_model_setting_keeps_the_same_state_with_one_scheduler(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    curve: bool,
    instant: bool,
    models: tuple[str, str | None],
) -> None:
    owner = _owner(app, curve=curve, instant=instant)
    monkeypatch.setattr(aqt, "mw", owner)
    backend = _backend(*models)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    build = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    try:
        request = UpdateDeckConfigsRequest()
        request.rwkv_profile_settings.model = "sm72e25"
        request.rwkv_profile_settings.state_precision = "int8"
        request.rwkv_profile_settings.single_model = True
        mediasrv._on_update_deck_configs_success(request, close_on_success=True)
        app.processEvents()
        assert owner.pm.profile["rwkvSingleModel"] is True  # type: ignore[attr-defined]
        assert rwkv_scheduler._reviewer_backend is backend
        assert not rwkv_scheduler.rwkv_state_cache_loading(owner)
        assert not [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        build.assert_not_called()
    finally:
        owner.close()


@pytest.mark.parametrize(
    "curve,instant,old_models,new_models",
    [
        (False, True, ("sm98", None), RwkvRuntimeModels("sm101")),
        (True, False, ("sm92", None), RwkvRuntimeModels("sm106")),
    ],
)
def test_saved_size_change_prompts_with_one_scheduler(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    curve: bool,
    instant: bool,
    old_models: tuple[str, str | None],
    new_models: RwkvRuntimeModels,
) -> None:
    owner = _owner(app, curve=curve, instant=instant)
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend(*old_models))
    prompt = MagicMock()
    build = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    try:
        _save_tiny()
        app.processEvents()
        prompt.assert_called_once()
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(owner) == new_models
        prompt.call_args.kwargs["callback"](0)
        build.assert_called_once()
    finally:
        owner.close()


def test_postponed_model_rebuild_does_not_start_automatic_recovery(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    prompt = MagicMock()
    build = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    try:
        _save_tiny()
        assert rwkv_scheduler.rwkv_state_cache_loading(owner)
        assert not rwkv_scheduler.request_rwkv_state_cache_recovery(
            owner, reason="deck or preset change"
        )
        app.processEvents()
        prompt.assert_called_once()
        prompt.call_args.kwargs["callback"](2)
        assert rwkv_scheduler.rwkv_state_declined(owner)
        assert not rwkv_scheduler.rwkv_state_cache_loading(owner)
        assert not rwkv_scheduler.request_rwkv_state_cache_recovery(
            owner, reason="deck or preset change"
        )
        build.assert_not_called()
    finally:
        owner.close()


def test_escape_postpones_the_model_rebuild(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    owner.show()
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    build = MagicMock()
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(10)
    dismissed: list[bool] = []

    def dismiss_prompt() -> None:
        if dismissed:
            if rwkv_scheduler.rwkv_state_declined(owner):
                loop.quit()
            return
        for widget in owner.findChildren(QMessageBox):
            if widget.isVisible():
                QTest.keyClick(widget, Qt.Key.Key_Escape)
                dismissed.append(True)
                return

    timer.timeout.connect(dismiss_prompt)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    try:
        _save_tiny()
        timer.start()
        timeout.start(2_000)
        loop.exec()
        assert dismissed == [True]
        assert rwkv_scheduler.rwkv_state_declined(owner)
        assert not rwkv_scheduler.rwkv_state_cache_loading(owner)
        build.assert_not_called()
    finally:
        timer.stop()
        timeout.stop()
        for message in owner.findChildren(QMessageBox):
            message.close()
        owner.close()


def test_scheduled_recovery_waits_for_model_rebuild_choice(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    scheduled = []
    owner.progress = SimpleNamespace(  # type: ignore[attr-defined]
        single_shot=lambda _delay, callback: scheduled.append(callback)
    )
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    monkeypatch.setattr(rwkv_scheduler, "_rwkv_resident_state_ready", lambda _mw: False)
    prompt = MagicMock()
    build = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    try:
        assert rwkv_scheduler.request_rwkv_state_cache_recovery(owner, reason="test")
        assert len(scheduled) == 1
        _save_tiny()
        scheduled[0]()
        build.assert_not_called()
        app.processEvents()
        prompt.call_args.kwargs["callback"](0)
        build.assert_called_once()
        assert build.call_args.kwargs["force_rebuild"] is True
    finally:
        owner.close()


def test_model_rebuild_waits_for_an_existing_cache_operation(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    owner._rwkv_state_cache_loading = True  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    prompt = MagicMock()
    build = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    try:
        _save_tiny()
        app.processEvents()
        prompt.call_args.kwargs["callback"](0)
        build.assert_not_called()
        rwkv_scheduler._finish_rwkv_state_cache_operation(
            owner, ready=False, prewarm_reason="previous operation"
        )
        build.assert_called_once()
        assert build.call_args.kwargs["force_rebuild"] is True
    finally:
        owner.close()


def test_postponing_model_rebuild_keeps_existing_operation_loading(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    owner._rwkv_state_cache_loading = True  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    prompt = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    try:
        _save_tiny()
        app.processEvents()
        prompt.call_args.kwargs["callback"](2)
        assert rwkv_scheduler.rwkv_state_declined(owner)
        assert rwkv_scheduler.rwkv_state_cache_loading(owner)
        rwkv_scheduler._finish_rwkv_state_cache_operation(
            owner, ready=False, prewarm_reason="previous operation"
        )
        assert not rwkv_scheduler.rwkv_state_cache_loading(owner)
    finally:
        owner.close()


@pytest.mark.parametrize("choice_made", [False, True])
def test_model_change_discards_backend_started_before_save(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, choice_made: bool
) -> None:
    from aqt import rwkv_srs_benchmark

    owner = _owner(app, curve=True, instant=True)
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
    monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
    started = threading.Event()
    release = threading.Event()
    result: list[bool] = []

    class SlowBackend:
        def __init__(self, **_kwargs: object) -> None:
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(rwkv_srs_benchmark, "EmbeddedRwkvReviewerBackend", SlowBackend)
    prompt = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(
        rwkv_scheduler, "build_rwkv_state_cache_with_progress", MagicMock()
    )
    worker = threading.Thread(
        target=lambda: result.append(
            rwkv_scheduler.configure_reviewer_backend_from_environment()
        )
    )
    try:
        worker.start()
        assert started.wait(5)
        _save_tiny()
        if choice_made:
            app.processEvents()
            prompt.call_args.kwargs["callback"](0)
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result == [False]
        assert rwkv_scheduler._reviewer_backend is None
    finally:
        release.set()
        worker.join(timeout=5)
        owner._rwkv_model_change_pending = None  # type: ignore[attr-defined]
        app.processEvents()
        owner.close()


def test_one_model_setting_prompts_when_it_changes_the_active_models(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(app, curve=True, instant=True)
    monkeypatch.setattr(aqt, "mw", owner)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", _backend("sm92", "sm98"))
    prompt = MagicMock()
    build = MagicMock()
    monkeypatch.setattr("aqt.utils.ask_user_dialog", prompt)
    monkeypatch.setattr(rwkv_scheduler, "build_rwkv_state_cache_with_progress", build)
    request = UpdateDeckConfigsRequest()
    request.rwkv_profile_settings.model = "sm72e25"
    request.rwkv_profile_settings.state_precision = "int8"
    request.rwkv_profile_settings.single_model = True
    try:
        mediasrv._on_update_deck_configs_success(request, close_on_success=False)
        app.processEvents()
        prompt.call_args.kwargs["callback"](0)
        assert rwkv_scheduler._rwkv_runtime_models_for_profile(
            owner
        ) == RwkvRuntimeModels("sm92")
        build.assert_called_once()
    finally:
        owner.close()
