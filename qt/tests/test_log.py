# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import errno
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
