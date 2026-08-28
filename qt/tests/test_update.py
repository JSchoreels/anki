# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.collection import GithubRelease
from aqt.update import _release_is_newer


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
