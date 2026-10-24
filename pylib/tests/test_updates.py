# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import hashlib
from pathlib import Path

from anki._backend import RustBackend
from anki.collection import GithubRelease


def test_download_release_reuses_verified_archive_inside_portable_data(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))
    updates = tmp_path / "Anki Portable Data" / "updates"
    updates.mkdir(parents=True)
    archive = updates / "anki-1.2.3-portable-mac-apple.zip"
    archive.write_bytes(b"verified portable archive")
    old_archive = updates / "old.zip"
    old_archive.write_bytes(b"old download")
    data = tmp_path / "Anki Portable Data" / "keep.txt"
    data.write_text("existing user data")
    release = GithubRelease(
        filename=archive.name,
        url="https://example.invalid/unused-cached-download",
        checksum=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )

    path = RustBackend().download_release(release)

    assert Path(path) == archive
    assert archive.read_bytes() == b"verified portable archive"
    assert not old_archive.exists()
    assert data.read_text() == "existing user data"
