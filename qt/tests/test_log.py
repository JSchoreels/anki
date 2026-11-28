# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("close_output", [False, True])
def test_console_logging_allows_cleanup_after_output_pipe_closes(
    tmp_path: Path, close_output: bool
) -> None:
    script = """
import logging
import sys
from aqt.log import setup_logging

setup_logging(sys.argv[1], level=logging.INFO)
logging.info("ready")
sys.stdin.readline()
logging.warning("closing server")
logging.warning("finishing cleanup")
logging.shutdown()
sys.stdout.flush()
sys.stderr.write("cleanup completed\\n")
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        assert "ready" in process.stdout.readline()
        if close_output:
            process.stdout.close()
        process.stdin.write("close\n")
        process.stdin.flush()
        process.wait(timeout=30)
        diagnostics = process.stderr.read()
        if not close_output:
            output = process.stdout.read()
            assert "closing server" in output
            assert "finishing cleanup" in output

    assert process.returncode == 0, diagnostics
    assert diagnostics == "cleanup completed\n"
    captured = (tmp_path / "execution.log").read_text(encoding="utf-8")
    assert "finishing cleanup" in captured
    assert captured.endswith("cleanup completed\n")


@pytest.mark.parametrize(
    "error_number",
    [
        errno.EIO,
        errno.EACCES,
        pytest.param(
            errno.EINVAL,
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="Windows uses EINVAL for a closed pipe"
            ),
        ),
    ],
)
def test_console_logging_reports_other_output_errors(
    tmp_path: Path, error_number: int
) -> None:
    script = """
import logging
import sys
from aqt.log import setup_logging

class FailingOutput:
    def write(self, message):
        raise OSError(int(sys.argv[2]), "unexpected output failure")

    def flush(self):
        pass

sys.stdout = FailingOutput()
setup_logging(sys.argv[1], level=logging.INFO)
logging.warning("closing server")
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(error_number)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert process.returncode == 0
    assert "unexpected output failure" in process.stderr
    assert "closing server" in process.stderr


def test_execution_log_keeps_console_output_across_restarts(tmp_path: Path) -> None:
    script = """
import logging, sys
from aqt.log import setup_logging
setup_logging(sys.argv[1], level=logging.INFO)
logger = logging.getLogger("aqt.rwkv_scheduler")
logger.info("review %s", sys.argv[2])
print("日本語")
print("traceback detail", file=sys.stderr)
"""
    for session in ("first", "second"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), session],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert f"review {session}" in result.stdout
        assert result.stderr == "traceback detail\n"

    text = (tmp_path / "execution.log").read_text(encoding="utf-8")
    assert text.index("review first") < text.index("review second")
    assert text.count("日本語\ntraceback detail\n") == 2


def test_execution_log_exports_rotated_parts_oldest_first(tmp_path: Path) -> None:
    import logging

    from aqt.log import ExecutionLog

    log = ExecutionLog(tmp_path / "execution.log")
    try:
        log.maxBytes = 6
        for line in ("oldest", "one", "two", "three"):
            log.handle(logging.makeLogRecord({"msg": line + "\n"}))
        assert log.snapshot() == os.linesep.join(("one", "two", "three", "")).encode()
    finally:
        log.close()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            '{"password": "two \\"quoted\\" words", "status": 401}',
            '{"password": "<redacted>", "status": 401}',
        ),
        ("Authorization: Bearer example-token", "Authorization: <redacted>"),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: <redacted>"),
        ("--password 'secret words'", "--password '<redacted>'"),
        ("syncKey=one&two more", "syncKey=<redacted>"),
        ("GET /?access_token=private&deck=12", "GET /?access_token=<redacted>&deck=12"),
        ("https://me:secret@example.org/path", "https://<redacted>@example.org/path"),
        ("user+anki%40example.org", "<redacted>"),
        ("tokenizer=ready token_count=12", "tokenizer=ready token_count=12"),
        ("found ghp_fake123", "found <redacted>"),
    ],
)
def test_redacts_recognizable_log_values(message: str, expected: str) -> None:
    from aqt.log_redaction import redact_log_text

    assert redact_log_text(message) == expected
    assert redact_log_text(expected) == expected


def test_secrets_are_redacted_before_log_files_are_written(tmp_path: Path) -> None:
    script = """
import logging, sys
from aqt.log import setup_logging
log = setup_logging(sys.argv[1], level=logging.INFO)
sys.stdout.write("pass")
sys.stderr.write("user+")
sys.stdout.write("word=synthetic-pass")
sys.stdout.flush()
assert b"synthetic-pass" not in log.snapshot()
sys.stderr.write("anki@example.invalid\\n")
print()
logging.getLogger("addon.example").warning("Authorization: Bearer %s", "synthetic-token")
sys.stderr.write("token=synthetic-unfinished")
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True
    )
    execution = (tmp_path / "execution.log").read_text()
    addon = (tmp_path / "addons/example/example.log").read_text()
    assert execution.count("<redacted>") == 4
    assert "Authorization: <redacted>" in addon
    for secret in ("synthetic-", "user+", "example.invalid"):
        assert secret not in execution
        assert secret not in addon


def test_github_attachment_keeps_full_rotated_log(tmp_path, monkeypatch):
    from aqt import execution_logs
    from aqt.log import ExecutionLog
    from aqt.qt import QApplication, QTimer

    app = QApplication.instance() or QApplication([])
    older = ("older 日本語 & # + %\r\n" * 6000).encode()
    latest = "session\u00a0finished\r\n".encode()
    log = ExecutionLog(tmp_path / "execution.log")
    Path(log.baseFilename + ".1").write_bytes(older + b"password=")
    Path(log.baseFilename).write_bytes(b"legacy password\r\n" + latest)
    opened = []
    folders = []
    reviews = []

    def accept():
        dialog = app.activeModalWidget()
        reviews.append(dialog.textValue())
        dialog.accept()

    monkeypatch.setattr(execution_logs.tr, "_translate", lambda *args, **kwargs: "")
    monkeypatch.setattr(execution_logs, "openLink", opened.append)
    monkeypatch.setattr(
        execution_logs, "openFolder", lambda path: folders.append(Path(path))
    )
    try:
        QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
        execution_logs.post_logs(None, log)
        assert not opened and not folders

        QTimer.singleShot(0, accept)
        execution_logs.post_logs(None, log)
        assert reviews[0].count("older 日本語") == 6000
        assert "finished" in reviews[0]
        assert "password=<redacted>" in reviews[0]
        assert "legacy password" not in reviews[0]
        assert opened == ["https://github.com/JSchoreels/anki/issues/new"]
        attachment = (folders[0] / "anki-execution.log").read_bytes()
        assert attachment == older + b"password=<redacted>\r\n" + latest
    finally:
        log.close()


def test_help_menu_exports_and_opens_a_reviewed_issue(tmp_path, monkeypatch):
    import logging
    from types import SimpleNamespace

    from aqt import execution_logs
    from aqt.forms.main import Ui_MainWindow
    from aqt.log import ExecutionLog
    from aqt.main import AnkiQt
    from aqt.qt import QApplication, QMainWindow, QTimer
    from aqt.utils import tr

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "")
    window = AnkiQt.__new__(AnkiQt)
    QMainWindow.__init__(window)
    window.form = Ui_MainWindow()
    window.form.setupUi(window)
    log = ExecutionLog(tmp_path / "execution.log")
    try:
        window.app = SimpleNamespace(execution_log=log)
        window.setupMenus()
        export = window.form.actionExportExecutionLog
        report = window.form.actionPostLogs
        assert {export, report} <= set(window.form.menuHelp.actions())
        log.handle(logging.makeLogRecord({"msg": "review complete\n"}))
        destination = tmp_path / "export.log"
        monkeypatch.setattr(
            execution_logs.QFileDialog,
            "getSaveFileName",
            lambda *args: (str(destination), ""),
        )
        export.trigger()
        original_log = f"review complete{os.linesep}".encode()
        assert destination.read_bytes() == original_log

        opened = []

        def edit_attachment():
            dialog = app.activeModalWidget()
            dialog.setTextValue("edited review by reviewer@example.invalid")
            dialog.accept()

        monkeypatch.setattr(execution_logs, "openLink", opened.append)
        folders = []
        monkeypatch.setattr(
            execution_logs, "openFolder", lambda path: folders.append(Path(path))
        )
        QTimer.singleShot(0, edit_attachment)
        report.trigger()
        assert opened == ["https://github.com/JSchoreels/anki/issues/new"]
        attachment = (folders[0] / "anki-execution.log").read_bytes()
        assert attachment == b"edited review by <redacted>"
        assert log.snapshot() == original_log
    finally:
        log.close()
        window.deleteLater()
        app.processEvents()
