# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import atexit
from pathlib import Path
from tempfile import TemporaryDirectory

from aqt.log import ExecutionLog
from aqt.log_redaction import redact_log_text
from aqt.qt import QFileDialog, QInputDialog, QIODevice, QSaveFile, QWidget
from aqt.utils import openFolder, openLink, showWarning, tr

_ISSUE_URL = "https://github.com/JSchoreels/anki/issues/new"


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


def post_logs(parent: QWidget, log: ExecutionLog) -> None:
    try:
        data = log.snapshot()
    except OSError as exc:
        showWarning(tr.qt_misc_log_export_failed(reason=str(exc)), parent=parent)
        return

    dialog = QInputDialog(parent)
    dialog.setWindowTitle(tr.qt_misc_post_logs_on_github_issues())
    dialog.setLabelText(tr.qt_misc_review_log_attachment())
    dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput)
    dialog.setTextValue(data.decode("utf-8", errors="replace"))
    # Qt normalizes line endings and some Unicode whitespace in the editor.
    original = dialog.textValue()
    accepted = dialog.exec()
    edited = dialog.textValue()
    dialog.deleteLater()
    if not accepted:
        return
    if edited != original:
        data = redact_log_text(edited).encode("utf-8")

    try:
        directory = TemporaryDirectory(prefix="anki-github-issue-")
        # The browser may still be uploading after this action returns.
        atexit.register(directory.cleanup)
        (Path(directory.name) / "anki-execution.log").write_bytes(data)
    except OSError as exc:
        showWarning(tr.qt_misc_log_export_failed(reason=str(exc)), parent=parent)
        return
    openFolder(directory.name)
    openLink(_ISSUE_URL)
