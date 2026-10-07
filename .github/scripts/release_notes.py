# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Extract the curated changelog for a release from RELEASE.md."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def extract_release_notes(changelog: str, version: str) -> str:
    sections = re.split(r"^## +(.+)$", changelog, flags=re.MULTILINE)
    by_title = {
        re.sub(r"^\[([^]]+)\].*", r"\1", title).split(" — ")[0].strip(): body.strip()
        for title, body in zip(sections[1::2], sections[2::2])
    }
    notes = by_title.get(version, by_title.get("Unreleased", ""))
    if not re.search(r"^[-*] +\S", notes, flags=re.MULTILINE):
        raise ValueError(f"No curated release notes for {version} in RELEASE.md")
    return notes + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        notes = extract_release_notes(
            Path("RELEASE.md").read_text(encoding="utf-8"), args.version
        )
    except ValueError as error:
        parser.error(str(error))
    args.output.write_text(notes, encoding="utf-8")


if __name__ == "__main__":
    main()
