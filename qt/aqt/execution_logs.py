# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from pathlib import Path
from textwrap import indent
from urllib.parse import urlencode

from aqt.log import ExecutionLog
from aqt.qt import QFileDialog, QInputDialog, QIODevice, QSaveFile, QWidget
from aqt.utils import openLink, showWarning, tr

_ISSUE_URL = "https://github.com/JSchoreels/anki/issues/new"
# Leave room for the browser and OS to pass the URL to GitHub.
_MAX_ISSUE_URL = 2000


def export_log(parent: QWidget, log: ExecutionLog) -> None:
    path, _ = QFileDialog.getSaveFileName(
        parent, tr.qt_misc_export_execution_log(), "anki-execution.log", "*.log"
    )
    if not path:
        return
    try:
        target = Path(path).resolve()
        if any(
            target == source.resolve()
            or (target.exists() and source.exists() and target.samefile(source))
            for source in log.paths()
        ):
            showWarning(tr.qt_misc_choose_another_log_file(), parent=parent)
            return
        data = log.snapshot()
        destination = QSaveFile(path)
        if (
            not destination.open(QIODevice.OpenModeFlag.WriteOnly)
            or destination.write(data) != len(data)
            or not destination.commit()
        ):
            raise OSError(destination.errorString())
    except OSError as exc:
        showWarning(tr.qt_misc_log_export_failed(reason=str(exc)), parent=parent)


def _issue_url(excerpt: str) -> str:
    body = "### Execution log\n\n" + indent(excerpt, "    ")
    return _ISSUE_URL + "?" + urlencode({"body": body})


def _recent_excerpt(data: bytes) -> str:
    # Prefer whole recent lines. If even the last line is too long, keep its tail.
    tail = data[-_MAX_ISSUE_URL:].decode("utf-8", errors="replace").rstrip()
    while len(_issue_url(tail)) > _MAX_ISSUE_URL:
        if "\n" in tail:
            tail = tail.split("\n", 1)[1]
        else:
            tail = tail[max(1, len(tail) // 8) :]
    return tail


def post_logs(parent: QWidget, log: ExecutionLog) -> None:
    try:
        excerpt = _recent_excerpt(log.snapshot())
    except OSError as exc:
        showWarning(tr.qt_misc_log_export_failed(reason=str(exc)), parent=parent)
        return

    prompt = tr.qt_misc_review_log_excerpt()
    while True:
        excerpt, accepted = QInputDialog.getMultiLineText(
            parent, tr.qt_misc_post_logs_on_github_issues(), prompt, excerpt
        )
        if not accepted:
            return
        url = _issue_url(excerpt)
        if len(url) <= _MAX_ISSUE_URL:
            openLink(url)
            return
        prompt = tr.qt_misc_log_excerpt_too_long()
