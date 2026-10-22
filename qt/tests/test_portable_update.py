# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aqt import portable_update as update


def make_app(root: Path, version: str, *, system: str | None = None) -> None:
    system = system or sys.platform
    resources = update.resources_path(root, system)
    resources.mkdir(parents=True, exist_ok=True)
    (root / update.DATA_DIR).mkdir(exist_ok=True)
    (resources / "anki-portable").touch()
    shutil.copy2(Path(update.__file__), resources / update.HELPER)
    launcher = root / update.launcher_path(system)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, runpy, sys\nfrom pathlib import Path\n"
        "plan = os.environ.pop('ANKI_PORTABLE_UPDATE_PLAN', None)\n"
        "if plan:\n"
        "    runpy.run_path(str(Path(plan).parent / 'helper.py'), run_name='__main__', "
        "init_globals={'update_plan_path': plan})\n"
        "else:\n"
        f"    data = Path(os.environ['ANKI_PORTABLE_ROOT']) / {update.DATA_DIR!r}\n"
        f"    (data / 'launched.txt').write_text({version!r})\n"
        f"    if {version!r} == 'crash': sys.exit(2)\n"
        "    if ack := os.environ.get('ANKI_PORTABLE_UPDATE_ACK'): Path(ack).touch()\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (root / "version.txt").write_text(version)
    (root / ("old-runtime.dll" if version == "old" else "new-runtime.dll")).write_text(
        version
    )
    update.write_manifest(root, system, platform.machine())


def make_job(tmp_path: Path, version="new", *, system: str | None = None):
    root = tmp_path / "Anki Portable"
    make_app(root, "old", system=system)
    data = root / update.DATA_DIR
    (data / "profiles").mkdir()
    (data / "profiles" / "keep.bin").write_bytes(b"opaque user collection and media")
    (root / "user-extra.txt").write_text("not application-owned")
    job = data / "updates" / "portable-update-test"
    staged = job / "payload" / "Anki Portable"
    make_app(staged, version, system=system)
    plan = {
        "protocol": 1,
        "root": str(root),
        "pid": os.getpid(),
        "system": system or sys.platform,
        "old_entries": update.read_manifest(root)["entries"],
        "new_entries": update.read_manifest(staged)["entries"],
    }
    (job / "plan.json").write_text(json.dumps(plan))
    return root, job, plan


def assert_user_files(root: Path) -> None:
    assert (
        root / update.DATA_DIR / "profiles" / "keep.bin"
    ).read_bytes() == b"opaque user collection and media"
    assert (root / "user-extra.txt").read_text() == "not application-owned"


def test_replacement_preserves_data_and_unowned_files_and_keeps_previous_application(
    tmp_path,
):
    root, job, plan = make_job(tmp_path)

    update.apply_update(job, plan)

    assert (root / "version.txt").read_text() == "new"
    assert not (root / "old-runtime.dll").exists()
    assert (root / "new-runtime.dll").read_text() == "new"
    assert (job / "previous" / "version.txt").read_text() == "old"
    assert (job / "state").read_text() == "installed"
    assert_user_files(root)


def test_copy_failure_restores_previous_application_and_partial_files(
    tmp_path, monkeypatch
):
    root, job, plan = make_job(tmp_path)
    copy = update.copy_entry
    calls = 0

    def fail_after_first_entry(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_bytes(b"partial")
            raise OSError("disk full")
        copy(source, target)

    monkeypatch.setattr(update, "copy_entry", fail_after_first_entry)
    with pytest.raises(OSError, match="disk full"):
        update.apply_update(job, plan)

    assert (root / "version.txt").read_text() == "old"
    assert (root / "old-runtime.dll").read_text() == "old"
    assert not (root / "new-runtime.dll").exists()
    assert update.read_manifest(root)["entries"] == plan["old_entries"]
    assert (job / "state").read_text() == "rolled-back"
    assert_user_files(root)


@pytest.mark.parametrize(
    "name",
    [
        update.DATA_DIR,
        update.DATA_DIR.upper(),
        update.DATA_DIR + ".",
        "../outside",
        "a/b",
        "a\\b",
        "C:escape",
    ],
)
def test_manifest_cannot_manage_data_or_paths_outside_application(tmp_path, name):
    tmp_path.joinpath(update.MANIFEST).write_text(
        json.dumps(
            {"protocol": 1, "system": sys.platform, "entries": [update.MANIFEST, name]}
        )
    )
    with pytest.raises(ValueError):
        update.read_manifest(tmp_path)


def test_update_rejects_user_added_name_collision_before_installing(tmp_path):
    root, job, plan = make_job(tmp_path)
    (root / "new-runtime.dll").write_text("user file")

    with pytest.raises(ValueError, match="user-added file"):
        update.validate_installation(
            root,
            update.read_manifest(root),
            update.read_manifest(job / "payload" / "Anki Portable"),
        )

    assert (root / "new-runtime.dll").read_text() == "user file"
    assert_user_files(root)


def test_update_archive_cannot_contain_user_data(tmp_path):
    root, job, _plan = make_job(tmp_path)
    staged = job / "payload" / "Anki Portable"
    (staged / update.DATA_DIR / "unexpected.txt").write_text("not application files")

    with pytest.raises(ValueError, match="contains portable user data"):
        update.validate_payload(staged, update.read_manifest(staged))
    assert_user_files(root)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink fixture")
@pytest.mark.parametrize("system", ["darwin", "linux"])
def test_application_symlink_cannot_point_into_existing_user_data(tmp_path, system):
    root, job, _plan = make_job(tmp_path, system=system)
    staged = job / "payload" / "Anki Portable"
    resources = update.resources_path(staged, system)
    application_files = resources / "app_packages"
    application_files.mkdir()
    update.write_manifest(staged, system, platform.machine())
    (application_files / "outside-link").symlink_to(root / update.DATA_DIR)

    with pytest.raises(ValueError, match="link leaves the archive"):
        update.validate_payload(staged, update.read_manifest(staged))
    assert_user_files(root)


def test_cancelled_shutdown_never_replaces_application(tmp_path, monkeypatch):
    root, job, _plan = make_job(tmp_path)
    monkeypatch.setattr(update, "wait_for_exit", lambda *args, **kwargs: False)

    assert update.run_helper(job / "plan.json") == 1

    assert (root / "version.txt").read_text() == "old"
    assert not (job / "previous").exists()
    assert (job / "state").read_text() == "cancelled"
    assert_user_files(root)


def test_cancelled_preparation_does_not_launch_helper_or_change_application(tmp_path):
    from anki.errors import Interrupted

    root, job, _plan = make_job(tmp_path)
    staged = job / "payload" / "Anki Portable"

    def extract(*, archive_path, destination):
        shutil.copytree(staged.parent, destination)

    backend = SimpleNamespace(extract_portable_update=extract)
    with pytest.raises(Interrupted):
        update.prepare_update(
            backend, tmp_path / "unused.zip", root, cancelled=lambda: True
        )

    assert (root / "version.txt").read_text() == "old"
    assert_user_files(root)


def test_windows_wait_observes_process_handle_without_sending_a_signal(
    tmp_path, monkeypatch
):
    kernel = SimpleNamespace(
        OpenProcess=MagicMock(return_value=123),
        WaitForSingleObject=MagicMock(return_value=0),
        CloseHandle=MagicMock(),
    )
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(
        update.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False
    )
    signal = MagicMock(side_effect=AssertionError("must never signal the running app"))
    monkeypatch.setattr(update.os, "kill", signal)

    assert update.wait_for_exit(456, tmp_path / "cancel", timeout=1)

    kernel.OpenProcess.assert_called_once_with(0x00100000, False, 456)
    kernel.CloseHandle.assert_called_once_with(123)
    signal.assert_not_called()


def test_windows_x64_update_uses_runtime_architecture_under_arm_emulation(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(update.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(update.sysconfig, "get_platform", lambda: "win-amd64")

    assert update.runtime_machine() == "AMD64"


def test_startup_acknowledgement_only_writes_the_current_update_job(
    tmp_path, monkeypatch
):
    job = tmp_path / update.DATA_DIR / "updates" / "portable-update-test"
    job.mkdir(parents=True)
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))
    monkeypatch.setenv(update.ACK_ENV, str(job / "started"))

    update.acknowledge_startup()

    assert (job / "started").is_file()
    assert update.ACK_ENV not in os.environ
    monkeypatch.setenv(update.ACK_ENV, str(tmp_path / "started"))
    update.acknowledge_startup()
    assert not (tmp_path / "started").exists()


def test_manual_start_is_blocked_during_installation_but_updater_restart_is_allowed(
    tmp_path, monkeypatch
):
    updates = tmp_path / update.DATA_DIR / "updates"
    updates.mkdir(parents=True)
    monkeypatch.setenv("ANKI_PORTABLE_ROOT", str(tmp_path))

    with update.installation_lock(updates):
        assert not update.can_start_application()
        monkeypatch.setenv(update.RESTART_ENV, "1")
        assert update.can_start_application()
        assert update.RESTART_ENV not in os.environ

    assert update.can_start_application()


def test_successful_job_cleanup_retains_current_backup_and_unrecognized_jobs(tmp_path):
    root, job, plan = make_job(tmp_path)
    old = job.parent / "portable-update-old"
    old.mkdir()
    (old / "plan.json").write_text(json.dumps(plan))
    (old / "state").write_text("complete")
    unknown = job.parent / "portable-update-user-extra"
    unknown.mkdir()
    (unknown / "plan.json").write_text("[]")
    (unknown / "state").write_text("complete")

    with update.installation_lock(job.parent):
        update.cleanup_old_jobs(job, root)

    assert not old.exists()
    assert job.exists()
    assert unknown.exists()
    assert_user_files(root)


@pytest.mark.parametrize("exit_code", [None, 1])
def test_acknowledged_application_is_not_rolled_back_if_status_write_fails(
    tmp_path, monkeypatch, exit_code
):
    root, job, _plan = make_job(tmp_path)
    monkeypatch.setattr(update, "wait_for_exit", lambda *args, **kwargs: True)
    child = MagicMock()
    child.poll.return_value = exit_code

    def start(*args):
        (job / "started").touch()
        return child

    monkeypatch.setattr(update, "start_application", start)
    save = update.save_state

    def fail_complete(path, state):
        if state == "complete":
            raise OSError("status write failed")
        save(path, state)

    monkeypatch.setattr(update, "save_state", fail_complete)

    assert update.run_helper(job / "plan.json") == 1

    assert (root / "version.txt").read_text() == "new"
    assert (job / "previous" / "version.txt").read_text() == "old"
    assert (job / "state").read_text() == "unconfirmed"
    assert_user_files(root)


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("portable update subprocess did not finish before deadline")
        time.sleep(0.05)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX executable fixture; Windows uses the packaged .exe",
)
@pytest.mark.parametrize(
    "version, expected", [("new", "complete"), ("crash", "rolled-back")]
)
def test_native_archive_helper_waits_replaces_and_restarts_or_rolls_back(
    tmp_path, version, expected
):
    root, unused_job, _plan = make_job(tmp_path, version)
    archive = tmp_path / "portable.zip"
    staged = unused_job / "payload" / "Anki Portable"
    with zipfile.ZipFile(archive, "w") as zip_file:
        for path in staged.rglob("*"):
            zip_file.write(path, "Anki Portable/" + path.relative_to(staged).as_posix())
    shutil.rmtree(unused_job)
    prepared = tmp_path / "prepared.txt"
    exit_requested = tmp_path / "exit.txt"
    script = (
        "import runpy, time\nfrom pathlib import Path\nfrom anki._backend import RustBackend\n"
        f"module = runpy.run_path({str(Path(update.__file__))!r})\n"
        f"plan = module['prepare_update'](RustBackend(), Path({str(archive)!r}), Path({str(root)!r}))\n"
        f"Path({str(prepared)!r}).write_text(plan)\n"
        f"while not Path({str(exit_requested)!r}).exists(): time.sleep(0.05)\n"
    )
    env = os.environ.copy()
    env["ANKI_PORTABLE_ROOT"] = str(root)
    with (tmp_path / "driver.log").open("w") as log:
        driver = subprocess.Popen(
            [sys.executable, "-c", script], env=env, stdout=log, stderr=log
        )
    try:
        wait_until(lambda: prepared.exists() or driver.poll() is not None)
        assert prepared.exists(), (tmp_path / "driver.log").read_text()
        job = Path(prepared.read_text()).parent
        assert (root / "version.txt").read_text() == "old"
        assert not (job / "previous").exists()
        assert_user_files(root)

        exit_requested.touch()
        assert driver.wait(timeout=10) == 0
        wait_until(lambda: (job / "state").read_text() == expected)
        if expected == "complete":
            assert (root / "version.txt").read_text() == "new"
            assert (root / update.DATA_DIR / "launched.txt").read_text() == "new"
            assert (job / "previous" / "version.txt").read_text() == "old"
        else:
            wait_until(
                lambda: (root / update.DATA_DIR / "launched.txt").read_text() == "old"
            )
            assert (root / "version.txt").read_text() == "old"
            assert not (root / "new-runtime.dll").exists()
            assert (
                "exited before completing bootstrap"
                in (job.parent / "last-update-error.txt").read_text()
            )
        assert_user_files(root)
    finally:
        if driver.poll() is None:
            exit_requested.touch()
            driver.wait(timeout=10)
