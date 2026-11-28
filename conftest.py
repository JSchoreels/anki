# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os

os.environ.setdefault("ANKI_TEST_MODE", "1")


def pytest_configure(config) -> None:
    # Tests that start a fresh Python interpreter need the same import paths as
    # pytest's `pythonpath` setting, which only affects the current process.
    paths = [str(path) for path in config.getini("pythonpath")]
    if existing := os.environ.get("PYTHONPATH"):
        paths.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
