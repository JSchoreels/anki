# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import sys
from types import ModuleType

import pytest

from anki.utils import int_version, int_version_to_str


@pytest.fixture
def buildinfo(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    buildinfo = ModuleType("anki.buildinfo")
    setattr(buildinfo, "buildhash", "test")
    monkeypatch.setitem(sys.modules, "anki.buildinfo", buildinfo)
    return buildinfo


def set_buildinfo_version(buildinfo: ModuleType, version: str) -> None:
    setattr(buildinfo, "version", version)


def test_int_version(buildinfo: ModuleType):
    set_buildinfo_version(buildinfo, "25.09")
    assert int_version() == 250900

    set_buildinfo_version(buildinfo, "25.09.4")
    assert int_version() == 250904

    set_buildinfo_version(buildinfo, "25.09.4+fsrs7")
    assert int_version() == 250904

    set_buildinfo_version(buildinfo, "25.09.4+fsrs7.build.7")
    assert int_version() == 250904

    set_buildinfo_version(buildinfo, "25.09b1")
    assert int_version() == 250900

    set_buildinfo_version(buildinfo, "2.1.23")
    assert int_version() == 23


def test_int_version_rejects_invalid_version(buildinfo: ModuleType):
    set_buildinfo_version(buildinfo, "invalid")

    with pytest.raises(ValueError, match="invalid version: invalid"):
        int_version()

    set_buildinfo_version(buildinfo, "25.09.4.2")

    with pytest.raises(ValueError, match=r"invalid version: 25\.09\.4\.2"):
        int_version()


def test_int_version_to_str():
    assert int_version_to_str(23) == "2.1.23"
    assert int_version_to_str(230900) == "23.09"
    assert int_version_to_str(230901) == "23.09.1"
