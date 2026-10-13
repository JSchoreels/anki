# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import io
import os.path
from collections.abc import Callable
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import pytest
from mock import MagicMock

import aqt.addons
from anki.collection import AddonInfo
from aqt import gui_hooks
from aqt.addons import (
    AddonManager,
    ChooseAddonsToUpdateDialog,
    DownloadError,
    DownloadOk,
    InstallOk,
    check_and_prompt_for_updates,
    download_addon,
    download_and_install_addon,
    package_name_valid,
)
from aqt.main import AnkiQt
from aqt.progress import ProgressManager
from aqt.qt import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QEventLoop,
    Qt,
    QTimer,
    QWidget,
)
from aqt.taskman import TaskManager


@pytest.fixture
def update_check(monkeypatch, addon_manager, app):
    callbacks = []
    timers = []
    finished = []
    addon_manager.mw = SimpleNamespace(
        col=object(),
        progress=SimpleNamespace(
            single_shot=lambda _ms, callback, **_kwargs: timers.append(callback)
        ),
    )
    monkeypatch.setattr(
        aqt.addons,
        "check_for_updates",
        lambda _mgr, callback: callbacks.append(callback),
    )
    monkeypatch.setattr(QApplication, "activeModalWidget", lambda: None)
    try:
        yield addon_manager, callbacks, timers, finished
    finally:
        gui_hooks.profile_will_close()
        app.processEvents()


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_update_check_does_not_repeat_while_waiting_for_a_prompt(
    update_check, monkeypatch
) -> None:
    mgr, callbacks, timers, finished = update_check
    prompts = []
    monkeypatch.setattr(
        aqt.addons,
        "handle_update_info",
        lambda _parent, _mgr, _items, on_done, _requested: prompts.append(on_done),
    )
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    assert len(callbacks) == 1

    callbacks.pop()([])
    assert prompts == []
    timers.pop()()
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    assert len(prompts) == 1
    assert callbacks == []

    prompts.pop()([])
    assert finished == [[]]
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    assert len(callbacks) == 1


@pytest.mark.parametrize("close_profile", [False, True])
def test_update_check_discards_results_from_a_previous_collection(
    update_check, monkeypatch, close_profile: bool
) -> None:
    mgr, callbacks, timers, finished = update_check
    prompts = []
    monkeypatch.setattr(
        aqt.addons, "handle_update_info", lambda *args: prompts.append(args)
    )
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    callbacks.pop()([])
    if close_profile:
        gui_hooks.profile_will_close()
    mgr.mw.col = object()
    timers.pop()()

    assert prompts == []
    assert finished == []
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    assert len(callbacks) == 1


def test_update_check_cancellation_preserves_other_profile_close_hooks(
    update_check,
) -> None:
    mgr, _callbacks, _timers, finished = update_check
    closed = []
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    on_close = lambda: closed.append(True)
    gui_hooks.profile_will_close.append(on_close)
    try:
        gui_hooks.profile_will_close()
        assert closed == [True]
        assert not mgr._update_check_in_progress
    finally:
        gui_hooks.profile_will_close.remove(on_close)


def test_update_check_started_before_profile_open_uses_the_opened_collection(
    update_check, monkeypatch
) -> None:
    mgr, callbacks, timers, finished = update_check
    mgr.mw.col = None
    monkeypatch.setattr(
        aqt.addons,
        "handle_update_info",
        lambda _parent, _mgr, _items, on_done, _requested: on_done([]),
    )
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append)
    mgr.mw.col = object()
    callbacks.pop()([])
    timers.pop()()

    assert finished == [[]]
    assert not mgr._update_check_in_progress


def test_automatic_update_check_finishes_when_updates_are_disabled(
    update_check,
) -> None:
    mgr, callbacks, timers, finished = update_check
    mgr.get_updated_addons = lambda _items: [AddonInfo(id=123)]
    mgr.addon_meta = lambda _id: SimpleNamespace(update_enabled=False)
    check_and_prompt_for_updates(mgr.mw, mgr, finished.append, requested_by_user=False)
    callbacks.pop()([])
    timers.pop()()

    assert finished == [[]]
    assert not mgr._update_check_in_progress


def _wait_for_ui(condition: Callable[[], bool]) -> None:
    loop = QEventLoop()
    poll = QTimer()
    poll.timeout.connect(lambda: loop.quit() if condition() else None)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    poll.start(10)
    timeout.start(5_000)
    try:
        if not condition():
            loop.exec()
        assert condition(), "Qt did not reach the expected UI state"
    finally:
        poll.stop()
        timeout.stop()


@pytest.mark.parametrize("choice", ["accept", "cancel", "close"])
def test_update_prompt_waits_for_rwkv_modals_and_closes_before_completion(
    monkeypatch, addon_manager, app, choice: str
) -> None:
    from PyQt6.QtTest import QTest

    mw = cast(Any, QWidget())
    mw.app = app
    mw.col = object()
    mw.weakref = lambda: mw
    mw.inMainThread = lambda: True
    mw.progress = ProgressManager(mw)
    mw.taskman = TaskManager(mw)
    mw.show()
    addon_manager.mw = mw
    addon = AddonInfo(id=123, modified=1)
    addon_manager.get_updated_addons = lambda _items: [addon]
    addon_manager.update_supported_versions = lambda _items: None
    addon_manager.addon_meta = lambda _id: SimpleNamespace(
        update_enabled=True, human_name=lambda: "Test add-on"
    )
    addon_manager.write_addon_meta = lambda _meta: None
    monkeypatch.setattr(aqt.addons, "restoreGeom", lambda *_args: None)
    monkeypatch.setattr(aqt.addons, "saveGeom", lambda *_args: None)
    monkeypatch.setattr(
        aqt.addons,
        "tr",
        SimpleNamespace(
            addons_choose_update_window_title=lambda: "Add-on Updates",
            addons_the_following_addons_have_updates_available=lambda: (
                "Updates available"
            ),
            addons_choose_update_update_all=lambda: "Update all",
        ),
    )
    monkeypatch.setattr(
        aqt.addons, "check_for_updates", lambda _mgr, callback: callback([addon])
    )
    finished = []

    def visible_prompts() -> list[ChooseAddonsToUpdateDialog]:
        return [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, ChooseAddonsToUpdateDialog) and widget.isVisible()
        ]

    def download(_parent, _mgr, ids, on_done, _client) -> None:
        assert ids == [123]
        assert visible_prompts() == []
        on_done([])

    monkeypatch.setattr(aqt.addons, "download_addons", download)
    scheduled = []
    single_shot = mw.progress.single_shot

    def observe_timer(ms, callback, requires_collection=True) -> None:
        scheduled.append(callback)
        single_shot(ms, callback, requires_collection)

    monkeypatch.setattr(mw.progress, "single_shot", observe_timer)
    rwkv_prompt = QDialog(mw)
    rwkv_prompt.setWindowTitle("RWKV State Cache")
    rwkv_prompt.setWindowModality(Qt.WindowModality.ApplicationModal)
    rwkv_prompt.show()
    try:
        _wait_for_ui(lambda: app.activeModalWidget() is rwkv_prompt)
        check_and_prompt_for_updates(mw, addon_manager, finished.append)
        _wait_for_ui(lambda: len(scheduled) >= 2)
        assert visible_prompts() == []

        # Choosing a rebuild replaces the question with modal progress.
        rwkv_prompt.reject()
        progress = mw.progress.start(
            parent=mw,
            label="Building RWKV state cache...",
            title="RWKV State Cache",
            immediate=True,
        )
        # The offscreen platform has no native focus window for the show timer.
        mw.progress._showWin()
        _wait_for_ui(lambda: app.activeModalWidget() is progress)
        assert visible_prompts() == []
        mw.progress.finish()
        _wait_for_ui(lambda: len(visible_prompts()) == 1)
        dialog = visible_prompts()[0]
        assert app.activeModalWidget() is None
        if choice == "close":
            dialog.close()
        else:
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            button = buttons.button(
                QDialogButtonBox.StandardButton.Ok
                if choice == "accept"
                else QDialogButtonBox.StandardButton.Cancel
            )
            assert button is not None
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert finished == [[]]
        assert visible_prompts() == []
        assert not addon_manager._update_check_in_progress

        # A profile reload closes an unanswered prompt before another can open.
        check_and_prompt_for_updates(mw, addon_manager, finished.append)
        _wait_for_ui(lambda: len(visible_prompts()) == 1)
        gui_hooks.profile_will_close()
        mw.hide()
        AnkiQt._checkForUnclosedWidgets(mw)
        assert visible_prompts() == []
        assert finished == [[]]
        mw.col = object()
        check_and_prompt_for_updates(mw, addon_manager, finished.append)
        _wait_for_ui(lambda: len(visible_prompts()) == 1)
    finally:
        gui_hooks.profile_will_close()
        for dialog in visible_prompts():
            dialog.close()
        rwkv_prompt.close()
        if mw.progress.busy():
            mw.progress.clear()
            _wait_for_ui(lambda: not mw.progress.busy())
        mw.taskman._collection_executor.shutdown()
        mw.taskman._no_collection_executor.shutdown()
        mw.close()
        app.processEvents()


def test_readMinimalManifest():
    assertReadManifest(
        '{"package": "yes", "name": "no"}', {"package": "yes", "name": "no"}
    )


def test_readExtraKeys():
    assertReadManifest(
        '{"package": "a", "name": "b", "mod": 3, "conflicts": ["d", "e"]}',
        {"package": "a", "name": "b", "mod": 3, "conflicts": ["d", "e"]},
    )


def test_invalidManifest():
    assertReadManifest('{"one": 1}', {})


def test_mustHaveName():
    assertReadManifest('{"package": "something"}', {})


def test_mustHavePackage():
    assertReadManifest('{"name": "something"}', {})


def test_invalidJson():
    assertReadManifest("this is not a JSON dictionary", {})


def test_missingManifest():
    assertReadManifest(
        '{"package": "what", "name": "ever"}', {}, nameInZip="not-manifest.bin"
    )


def test_ignoreExtraKeys():
    assertReadManifest(
        '{"package": "a", "name": "b", "game": "c"}', {"package": "a", "name": "b"}
    )


def test_conflictsMustBeStrings():
    assertReadManifest(
        '{"package": "a", "name": "b", "conflicts": ["c", 4, {"d": "e"}]}', {}
    )


def assertReadManifest(contents, expectedManifest, nameInZip="manifest.json"):
    with TemporaryDirectory() as td:
        zfn = os.path.join(td, "addon.zip")
        with ZipFile(zfn, "w") as zfile:
            zfile.writestr(nameInZip, contents)

        adm = AddonManager(MagicMock())

        with ZipFile(zfn, "r") as zfile:
            assert adm.readManifestFile(zfile) == expectedManifest


def test_package_name_validation():
    assert not package_name_valid("")
    assert not package_name_valid("/")
    assert not package_name_valid("a/b")
    assert not package_name_valid("..")
    assert package_name_valid("ab")


@pytest.fixture
def addon_manager(tmp_path) -> AddonManager:
    adm = AddonManager(MagicMock())
    adm.mw.pm.addonFolder.return_value = tmp_path
    return adm


def test_install_prefers_packaged_name(addon_manager):
    data = io.BytesIO()
    with ZipFile(data, "w") as zfile:
        zfile.writestr(
            "manifest.json",
            '{"package": "12345", "name": "Packaged & Name"}',
        )

    result = addon_manager.install(
        data, manifest={"package": "12345", "name": "Fallback Name"}
    )

    assert isinstance(result, InstallOk)
    assert result.name == "Packaged & Name"


def _addon_download(data: bytes, filename: str = "Fallback_Name.zip") -> DownloadOk:
    return DownloadOk(
        data=data,
        filename=filename,
        mod_time=0,
        min_point_version=0,
        max_point_version=0,
        branch_index=0,
    )


def test_download_prefers_packaged_name(monkeypatch, addon_manager):
    data = io.BytesIO()
    with ZipFile(data, "w") as zfile:
        zfile.writestr(
            "manifest.json",
            '{"package": "12345", "name": "Packaged & Name"}',
        )

    monkeypatch.setattr(
        "aqt.addons.download_addon",
        lambda _client, _id: _addon_download(data.getvalue()),
    )

    _, result = download_and_install_addon(addon_manager, MagicMock(), 12345)

    assert isinstance(result, InstallOk)
    assert result.name == "Packaged & Name"


def test_download_falls_back_to_filename_name(monkeypatch, addon_manager):
    data = io.BytesIO()
    with ZipFile(data, "w") as zfile:
        zfile.writestr("__init__.py", "")

    monkeypatch.setattr(
        "aqt.addons.download_addon",
        lambda _client, _id: _addon_download(data.getvalue()),
    )

    _, result = download_and_install_addon(addon_manager, MagicMock(), 12345)

    assert isinstance(result, InstallOk)
    assert result.name == "Fallback Name"


def test_install_extracts_safe_files(tmp_path, addon_manager):
    zfn = os.path.join(tmp_path, "addon.zip")
    with ZipFile(zfn, "w") as zfile:
        zfile.writestr("main.py", "content")
        zfile.writestr("../unsafe.txt", "content")
        zfile.writestr("subdir/helper.py", "content")
    with ZipFile(zfn) as zfile:
        addon_manager._install("12345", zfile)
    addon_dir = os.path.join(tmp_path, "12345")
    assert os.path.exists(os.path.join(addon_dir, "main.py"))
    assert os.path.exists(os.path.join(addon_dir, "subdir", "helper.py"))
    assert not os.path.exists(os.path.join(tmp_path, "unsafe.txt"))


def test_download_addon_rejects_bad_content_disposition():
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-disposition": "inline; not-a-filename"}
    client.get.return_value = resp
    client.stream_content.return_value = b"data"

    result = download_addon(client, 123)

    assert isinstance(result, DownloadError)
    assert isinstance(result.exception, ValueError)
    assert "content-disposition" in str(result.exception)
