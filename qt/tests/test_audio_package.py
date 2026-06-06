# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import importlib.util
from pathlib import Path


def _load_audio_hatch_build():
    module_path = Path(__file__).parents[1] / "audio" / "hatch_build.py"
    spec = importlib.util.spec_from_file_location("audio_hatch_build", module_path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_macos_audio_libraries_are_copied_to_mpv_libs_dir(tmp_path: Path) -> None:
    module = _load_audio_hatch_build()
    lib_file = tmp_path / "source" / "libass.9.dylib"
    lib_file.parent.mkdir()
    lib_file.write_bytes(b"fake dylib")

    dst_dir = tmp_path / "anki_audio"
    dst_dir.mkdir()
    module._copy_macos_library_files([lib_file], dst_dir)

    assert (dst_dir / "libs" / lib_file.name).read_bytes() == b"fake dylib"
    assert not (dst_dir / "lib").exists()
