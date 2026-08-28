# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Callable

import aqt
from anki.buildinfo import buildhash
from anki.buildinfo import version as version_str
from anki.collection import GithubRelease
from aqt.operations import QueryOp
from aqt.package import (
    download_github_update_and_install as _download_github_update_and_install,
)
from aqt.qt import *
from aqt.utils import show_warning, tooltip, tr


def _release_is_newer(
    release: GithubRelease,
    *,
    current_version: str = version_str,
    current_buildhash: str = buildhash,
) -> bool:
    from packaging.version import Version

    release_version = Version(release.tag_name)
    installed_version = Version(current_version)
    release_base = Version(release_version.public.split("+", maxsplit=1)[0])
    installed_base = Version(installed_version.public.split("+", maxsplit=1)[0])

    if release_base != installed_base:
        return release_base > installed_base

    target = release.target_commitish.lower()
    installed_hash = current_buildhash.lower()
    if target and installed_hash and target.startswith(installed_hash):
        return False

    return release_version > installed_version or len(target) >= 8


def check_for_update(*, parent: aqt.AnkiQt, manual: bool) -> None:
    from packaging.version import Version

    version = Version(version_str)

    def on_success(release: GithubRelease) -> None:
        if _release_is_newer(release):
            prompt_and_install_github_update(parent, release)
        elif manual:
            tooltip(tr.addons_no_updates_available(), parent=parent)

    def on_failure(exc: Exception) -> None:
        if manual:
            show_warning(str(exc), parent=parent)
        else:
            print(f"update check failed: {exc}")

    op = get_latest_release_op(
        parent=parent,
        include_prerelease=version.is_prerelease,
        on_success=on_success,
    ).failure(on_failure)
    if manual:
        op = op.with_progress()
    op.run_in_background()


def prompt_and_install_github_update(mw: aqt.AnkiQt, release: GithubRelease) -> None:
    msg = (
        tr.qt_misc_anki_updatedanki_has_been_released(val=release.tag_name)
        + tr.qt_misc_would_you_like_to_download_it()
    )

    msgbox = QMessageBox(mw)
    msgbox.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    msgbox.setIcon(QMessageBox.Icon.Information)
    msgbox.setText(msg)

    msgbox.setDefaultButton(QMessageBox.StandardButton.Yes)
    ret = msgbox.exec()

    if ret == QMessageBox.StandardButton.Yes:
        _download_github_update_and_install(release)


def get_latest_release_op(
    parent: QWidget,
    include_prerelease: bool,
    on_success: Callable[[GithubRelease], None],
) -> QueryOp:
    return QueryOp(
        parent=parent,
        op=lambda col: col._backend.get_latest_release(
            include_prerelease=include_prerelease
        ),
        success=on_success,
    )
