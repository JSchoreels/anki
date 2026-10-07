# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import pytest

from release_notes import extract_release_notes


def test_draft_notes_exclude_maintenance_and_older_releases() -> None:
    changelog = """# Release notes

## Maintenance
- Maintain the changelog.

## Unreleased

### Changed

- Browser selection is faster.
  More details on the next line.

## [26.09.3+fsrs7.build.97](https://example.com) — 2026-10-06

- A previous fix.
"""
    assert extract_release_notes(changelog, "26.09.3+fsrs7.build.98") == (
        "### Changed\n\n- Browser selection is faster.\n"
        "  More details on the next line.\n"
    )


def test_archived_release_uses_its_own_notes() -> None:
    changelog = """## Unreleased

- A later change.

## [26.09.3+fsrs7](https://example.com) — 2026-10-07

### Fixed

- The release fix.
"""
    assert extract_release_notes(changelog, "26.09.3+fsrs7") == (
        "### Fixed\n\n- The release fix.\n"
    )


@pytest.mark.parametrize(
    "changelog",
    [
        "## Maintenance\n- Keep notes current.\n",
        "## Unreleased\n\n## Old release\n- Old fix.\n",
        "## Unreleased\n\n### Fixed\n",
    ],
)
def test_missing_curated_notes_stop_release(changelog: str) -> None:
    with pytest.raises(ValueError, match="No curated release notes"):
        extract_release_notes(changelog, "26.09.3+fsrs7.build.98")
