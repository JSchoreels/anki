# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "artifact_version, expected",
    [
        (None, "26.09.3+fsrs7"),
        ("", "26.09.3+fsrs7"),
        ("26.09.3+fsrs7.build.107", "26.09.3+fsrs7.build.107"),
    ],
)
def test_buildinfo_preserves_artifact_version_separately(
    artifact_version, expected, tmp_path, monkeypatch
):
    version_file = tmp_path / "version"
    version_file.write_text("26.09.3+fsrs7\n", encoding="utf8")
    buildhash_file = tmp_path / "buildhash"
    buildhash_file.write_text("aaaaaaaa\n", encoding="utf8")
    output = tmp_path / "buildinfo.py"
    script = Path(__file__).parents[1] / "tools/genbuildinfo.py"
    monkeypatch.setattr(
        sys, "argv", [str(script), str(version_file), str(buildhash_file), str(output)]
    )
    if artifact_version is None:
        monkeypatch.delenv("ANKI_ARTIFACT_VERSION", raising=False)
    else:
        monkeypatch.setenv("ANKI_ARTIFACT_VERSION", artifact_version)

    runpy.run_path(str(script))
    metadata = runpy.run_path(str(output))

    assert metadata["version"] == "26.09.3+fsrs7"
    assert metadata["artifact_version"] == expected
    assert metadata["buildhash"] == "aaaaaaaa"
