# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import os
from collections.abc import MutableMapping
from pathlib import Path

PORTABLE_MARKER = "anki-portable"
PORTABLE_DATA_DIR = "Anki Portable Data"


def configure_portable_environment(
    module_file: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Point a marked macOS bundle at data stored beside the app."""
    module_file = (module_file or Path(__file__)).resolve()
    environ = environ if environ is not None else os.environ

    if len(module_file.parents) < 5:
        return None

    resources = module_file.parents[2]
    if resources.name != "Resources" or resources.parent.name != "Contents":
        return None

    bundle = resources.parent.parent
    if not (resources / PORTABLE_MARKER).is_file():
        return None

    portable_root = bundle.parent
    data_dir = portable_root / PORTABLE_DATA_DIR
    temp_dir = data_dir / ".tmp"
    data_dir.mkdir(exist_ok=True)
    temp_dir.mkdir(exist_ok=True)

    root_digest = hashlib.sha256(os.fsencode(portable_root)).hexdigest()[:16]
    environ["ANKI_BASE"] = str(data_dir)
    environ["ANKI_PORTABLE"] = "1"
    environ["ANKI_PORTABLE_ROOT"] = str(portable_root)
    environ["ANKI_SINGLE_INSTANCE_KEY"] = f"anki-portable-{root_digest}"
    environ["TMPDIR"] = str(temp_dir)
    return data_dir


def main():
    configure_portable_environment()

    import aqt

    aqt.run()
