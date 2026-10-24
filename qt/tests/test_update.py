# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest
from mock import MagicMock

import aqt
import aqt.operations
import aqt.package
import aqt.portable_update
import aqt.preferences
import aqt.update
from anki._backend import RustBackend, Translations
from anki.collection import GithubRelease
from aqt.preferences import Preferences
from aqt.profiles import ProfileManager
from aqt.progress import ProgressUpdate
from aqt.qt import QApplication, QCheckBox, QMessageBox, QTimer, QWidget
from aqt.update import _release_is_newer


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def query_op(monkeypatch):
    operation = MagicMock()
    operation.with_backend_progress.return_value = operation
    factory = MagicMock(return_value=operation)
    monkeypatch.setattr(aqt.operations, "QueryOp", factory)
    monkeypatch.setattr(aqt.update, "QueryOp", factory)
    return factory


@pytest.fixture
def translations(monkeypatch):
    backend = RustBackend(langs=["en"])
    tr = Translations(weakref.ref(backend))
    monkeypatch.setattr(aqt.package, "tr", tr)
    monkeypatch.setattr(aqt.preferences, "tr", tr)
    monkeypatch.setattr(aqt.update, "tr", tr)
    yield


def release(tag: str, target: str) -> GithubRelease:
    return GithubRelease(tag_name=tag, target_commitish=target)


def test_release_is_newer_uses_version_and_release_commit() -> None:
    assert _release_is_newer(
        release("26.09b2+fsrs7.build.90", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert _release_is_newer(
        release("26.09b1+fsrs7.build.90", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert not _release_is_newer(
        release("26.09b1+fsrs7.build.90", "aaaaaaaa1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert not _release_is_newer(
        release("26.08+fsrs7.build.89", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )


@pytest.mark.parametrize(
    "tag, target, expected",
    [
        ("26.09.3+fsrs7.build.102", "bbbbbbbb1234", False),
        ("26.09.3+fsrs7.build.107", "bbbbbbbb1234", False),
        ("26.09.3+fsrs7.build.108", "bbbbbbbb1234", True),
        ("26.09.3+fsrs7.build.107.2", "bbbbbbbb1234", True),
        ("26.09.3+fsrs7.build.108", "aaaaaaaa1234", False),
        ("26.09.3+fsrs7", "bbbbbbbb1234", False),
        ("26.09.4+fsrs7.build.108", "bbbbbbbb1234", True),
        ("26.09.2+fsrs7.build.108", "bbbbbbbb1234", False),
    ],
)
def test_fork_update_requires_a_newer_version_or_build(tag, target, expected):
    assert (
        _release_is_newer(
            release(tag, target),
            current_version="26.09.3+fsrs7.build.107",
            current_buildhash="aaaaaaaa",
        )
        is expected
    )


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("build", [102, 107, 108])
def test_update_check_compares_the_packaged_build(
    manual, build, monkeypatch, query_op, translations
):
    monkeypatch.setattr(aqt.update, "artifact_version", "26.09.3+fsrs7.build.107")
    monkeypatch.setattr(aqt.update, "version_str", "26.09.3+fsrs7")
    prompt = MagicMock()
    monkeypatch.setattr(aqt.update, "prompt_and_install_github_update", prompt)
    tooltip = MagicMock()
    monkeypatch.setattr(aqt.update, "tooltip", tooltip)
    update = release(f"26.09.3+fsrs7.build.{build}", "bbbbbbbb1234")

    aqt.update.check_for_update(parent=None, manual=manual)
    query_op.call_args.kwargs["success"](update)

    if build > 107:
        prompt.assert_called_once_with(None, update)
    else:
        prompt.assert_not_called()
    assert tooltip.call_count == int(manual and build <= 107)


@pytest.mark.parametrize("portable", [False, True])
def test_update_lookup_requests_current_edition(portable, monkeypatch, query_op):
    monkeypatch.setattr(aqt, "is_portable", lambda: portable)
    backend = MagicMock()
    aqt.update.get_latest_release_op(
        parent=None, include_prerelease=True, on_success=lambda _release: None
    )

    query_op.call_args.kwargs["op"](SimpleNamespace(_backend=backend))

    backend.get_latest_release.assert_called_once_with(
        include_prerelease=True, portable=portable
    )


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("enabled", [None, False, True])
def test_update_preference_applies_to_both_editions(portable, enabled, monkeypatch):
    monkeypatch.setattr(aqt, "is_portable", lambda: portable)
    pm = ProfileManager.__new__(ProfileManager)
    pm.meta = {} if enabled is None else {"check_for_updates": enabled}

    assert pm.check_for_updates() is (enabled is not False)


def test_portable_update_checkbox_can_disable_checks(monkeypatch, app, translations):
    monkeypatch.setattr(aqt, "is_portable", lambda: True)
    profile = ProfileManager.__new__(ProfileManager)
    profile.meta = {}
    dialog = MagicMock()
    checkbox = QCheckBox()
    dialog.form.check_for_updates = checkbox
    dialog.mw.pm.check_for_updates = profile.check_for_updates
    dialog.mw.pm.set_update_check = profile.set_update_check

    Preferences.setup_network(dialog)
    assert checkbox.isChecked()
    assert checkbox.isEnabled()
    checkbox.click()

    assert not profile.check_for_updates()


@pytest.mark.parametrize("filename", ["portable.zip", "portable.tar.zst"])
def test_portable_download_prepares_update_before_normal_shutdown(
    filename, tmp_path, monkeypatch, query_op, translations
):
    monkeypatch.setattr(aqt, "is_portable", lambda: True)
    window = MagicMock()
    monkeypatch.setattr(aqt, "mw", window)
    installer = MagicMock()
    monkeypatch.setattr(aqt.package.subprocess, "Popen", installer)
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))
    plan = str(tmp_path / "plan.json")
    prepare = MagicMock(return_value=plan)
    monkeypatch.setattr(aqt.portable_update, "prepare_update", prepare)
    output_path = str(tmp_path / filename)
    update = GithubRelease(filename=filename)
    window.col._backend.download_release.return_value = output_path

    aqt.package.download_github_update_and_install(update)
    callbacks = query_op.call_args.kwargs
    assert callbacks["op"](window.col) == plan
    window.close.assert_not_called()
    callbacks["success"](plan)

    window.col._backend.download_release.assert_called_once_with(update)
    assert prepare.call_args.args == (window.col._backend, Path(output_path), tmp_path)
    installer.assert_not_called()
    window.app.quit.assert_not_called()
    window.close.assert_called_once_with()


def test_failed_preparation_leaves_anki_running(tmp_path, monkeypatch, query_op):
    monkeypatch.setattr(aqt, "is_portable", lambda: True)
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))
    window = MagicMock()
    monkeypatch.setattr(aqt, "mw", window)
    window.col._backend.download_release.return_value = str(tmp_path / "portable.zip")
    monkeypatch.setattr(
        aqt.portable_update,
        "prepare_update",
        MagicMock(side_effect=ValueError("bad payload")),
    )

    aqt.package.download_github_update_and_install(
        GithubRelease(filename="portable.zip")
    )
    with pytest.raises(ValueError, match="bad payload"):
        query_op.call_args.kwargs["op"](window.col)

    window.close.assert_not_called()
    window.app.quit.assert_not_called()


def test_cancel_after_staging_cancels_helper_without_closing_anki(
    tmp_path, monkeypatch, query_op, translations
):
    monkeypatch.setattr(aqt, "is_portable", lambda: True)
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))
    window = MagicMock()
    monkeypatch.setattr(aqt, "mw", window)
    plan = str(tmp_path / "plan.json")
    window.col._backend.download_release.return_value = str(tmp_path / "portable.zip")
    monkeypatch.setattr(
        aqt.portable_update, "prepare_update", MagicMock(return_value=plan)
    )

    aqt.package.download_github_update_and_install(
        GithubRelease(filename="portable.zip")
    )
    callbacks = query_op.call_args.kwargs
    assert callbacks["op"](window.col) == plan
    progress = SimpleNamespace(
        HasField=lambda _name: True, download_update=SimpleNamespace(total_bytes=0)
    )
    query_op.return_value.with_backend_progress.call_args.args[0](
        progress, ProgressUpdate(user_wants_abort=True)
    )
    callbacks["success"](plan)

    window.close.assert_not_called()
    assert (tmp_path / "cancel").is_file()


@pytest.mark.parametrize(
    "filename, command",
    [("update.msi", ["msiexec", "/i"]), ("update.dmg", ["open"])],
)
def test_installed_update_still_launches_installer_and_quits(
    filename, command, tmp_path, monkeypatch, query_op
):
    monkeypatch.setattr(aqt, "is_portable", lambda: False)
    window = MagicMock()
    monkeypatch.setattr(aqt, "mw", window)
    installer = MagicMock()
    monkeypatch.setattr(aqt.package.subprocess, "Popen", installer)
    output_path = str(tmp_path / filename)

    aqt.package.download_github_update_and_install(GithubRelease(filename=filename))
    query_op.call_args.kwargs["success"](output_path)

    assert installer.call_args.args[0] == command + [output_path]
    window.app.quit.assert_called_once_with()


@pytest.mark.parametrize("accept", [False, True])
def test_portable_update_prompt_downloads_only_after_yes(
    accept, monkeypatch, app, translations
):
    monkeypatch.setattr(aqt, "is_portable", lambda: True)
    download = MagicMock()
    monkeypatch.setattr(aqt.update, "_download_github_update_and_install", download)
    window = QWidget()
    update = release("26.09.3+fsrs7.build.110", "bbbbbbbb1234")
    prompt_text = []

    def answer():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        prompt_text.append(dialog.text())
        button = (
            QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No
        )
        dialog.button(button).click()

    QTimer.singleShot(0, answer)
    try:
        aqt.update.prompt_and_install_github_update(window, update)
        assert "restart automatically" in prompt_text[0]
        assert "Anki Portable Data" in prompt_text[0]
        if accept:
            download.assert_called_once_with(update)
        else:
            download.assert_not_called()
    finally:
        window.close()
