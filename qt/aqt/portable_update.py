# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Portable update staging and the standalone, stdlib-only update helper.

The installer also bundles this source outside app_packages so a staged app can
run the current updater without importing Qt or depending on bytecode versions.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

DATA_DIR = "Anki Portable Data"
MANIFEST = "anki-portable-update.json"
HELPER = "portable-update-helper.py"
PLAN_ENV = "ANKI_PORTABLE_UPDATE_PLAN"
ACK_ENV = "ANKI_PORTABLE_UPDATE_ACK"
RESTART_ENV = "ANKI_PORTABLE_UPDATE_RESTART"


def launcher_path(system: str) -> str:
    return {
        "darwin": "Anki Portable.app/Contents/MacOS/Anki Portable",
        "win32": "Anki Portable.exe",
        "linux": "anki",
    }[system]


def resources_path(root: Path, system: str) -> Path:
    if system == "darwin":
        return root / "Anki Portable.app/Contents/Resources"
    return root


def runtime_machine() -> str:
    # Windows may report the native CPU while running an emulated x64 build.
    if sys.platform == "win32":
        return {"win-amd64": "AMD64", "win-arm64": "ARM64"}.get(
            sysconfig.get_platform(), platform.machine()
        )
    return platform.machine()


def write_manifest(root: Path, system: str, machine: str) -> None:
    entries = sorted(
        {p.name for p in root.iterdir() if p.name != DATA_DIR} | {MANIFEST}
    )
    (root / MANIFEST).write_text(
        json.dumps(
            {"protocol": 1, "system": system, "machine": machine, "entries": entries}
        ),
        encoding="utf-8",
    )


def read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST
    if path.is_symlink():
        raise ValueError(
            "The portable application manifest must not be a symbolic link."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid portable application manifest.")
    entries = data.get("entries")
    if data.get("protocol") != 1 or not isinstance(entries, list) or not entries:
        raise ValueError("This portable build does not support automatic replacement.")
    if any(
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or ":" in name
        or any(char in name for char in "\0<>|?*")
        or name.endswith((".", " "))
        or name.casefold() == DATA_DIR.casefold()
        for name in entries
    ) or len({name.casefold() for name in entries}) != len(entries):
        raise ValueError("Invalid portable application manifest.")
    if MANIFEST not in entries or data.get("system") not in (
        "darwin",
        "win32",
        "linux",
    ):
        raise ValueError("Invalid portable application manifest.")
    return data


def validate_payload(root: Path, manifest: dict[str, Any]) -> None:
    data = root / DATA_DIR
    if data.is_symlink() or (
        data.exists() and (not data.is_dir() or any(data.iterdir()))
    ):
        raise ValueError("The update archive contains portable user data.")
    expected = set(manifest["entries"]) | {DATA_DIR}
    if any(p.name not in expected for p in root.iterdir()):
        raise ValueError("The update archive contains unlisted application files.")
    for name in manifest["entries"]:
        entry = root / name
        if entry.is_symlink() or (not entry.is_file() and not entry.is_dir()):
            raise ValueError(f"Invalid application entry: {name}")
        for path in entry.rglob("*") if entry.is_dir() else ():
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError(f"Application link leaves the archive: {path}")
            if not path.is_file() and not path.is_dir():
                raise ValueError(f"Unsupported application file: {path}")
    resources = resources_path(root, manifest["system"])
    if (
        not (resources / "anki-portable").is_file()
        or not (resources / HELPER).is_file()
    ):
        raise ValueError(
            "The update is not an automatic-update-capable portable build."
        )
    launcher = root / launcher_path(manifest["system"])
    if not launcher.is_file() or (
        manifest["system"] != "win32" and not os.access(launcher, os.X_OK)
    ):
        raise ValueError("The portable update launcher is missing or not executable.")


def save_state(job: Path, state: str) -> None:
    temporary = job / "state.tmp"
    temporary.write_text(state, encoding="utf-8")
    temporary.replace(job / "state")


def validate_installation(
    root: Path, current: dict[str, Any], incoming: dict[str, Any]
) -> None:
    if (incoming["system"], incoming["machine"]) != (
        current["system"],
        current["machine"],
    ):
        raise ValueError("The portable update is for a different platform or CPU.")
    for name in current["entries"]:
        if (root / name).is_symlink():
            raise ValueError(
                f"The installed application entry is a symbolic link: {name}"
            )
    for name in incoming["entries"]:
        path = root / name
        if name not in current["entries"] and (path.exists() or path.is_symlink()):
            raise ValueError(f"The update would overwrite a user-added file: {name}")


def prepare_update(
    backend: Any,
    archive: Path,
    root: Path,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> str:
    root = root.resolve(strict=True)
    current = read_manifest(root)
    if (current["system"], current["machine"]) != (sys.platform, runtime_machine()):
        raise ValueError(
            "The portable application manifest does not match this runtime."
        )
    updates = root / DATA_DIR / "updates"
    updates.mkdir(parents=True, exist_ok=True)
    job = Path(tempfile.mkdtemp(prefix="portable-update-", dir=updates))
    process = None
    try:
        backend.extract_portable_update(
            archive_path=str(archive), destination=str(job / "payload")
        )

        def check_cancelled() -> None:
            if cancelled():
                from anki.errors import Interrupted

                raise Interrupted("Portable update cancelled", None, None, None)

        check_cancelled()
        staged = job / "payload" / "Anki Portable"
        incoming = read_manifest(staged)
        validate_installation(root, current, incoming)
        validate_payload(staged, incoming)
        required = sum(
            p.stat().st_size
            for p in staged.rglob("*")
            if p.is_file() and not p.is_symlink()
        )
        if shutil.disk_usage(root).free < required + 8 * 1024 * 1024:
            raise OSError("Not enough free space to install the portable update.")
        shutil.copy2(
            resources_path(root, current["system"]) / HELPER, job / "helper.py"
        )
        plan = {
            "protocol": 1,
            "root": str(root),
            "pid": os.getpid(),
            "system": current["system"],
            "old_entries": current["entries"],
            "new_entries": incoming["entries"],
        }
        plan_path = job / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        save_state(job, "prepared")
        env = os.environ.copy()
        env.pop(ACK_ENV, None)
        env[PLAN_ENV] = str(plan_path)
        with (job / "helper.log").open("ab") as log:
            process = subprocess.Popen(
                [str(staged / launcher_path(plan["system"]))],
                cwd=staged,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                creationflags=detached_flags(),
            )
        deadline = time.monotonic() + 30
        while not (job / "ready").exists():
            check_cancelled()
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"The portable updater could not start. See {job / 'helper.log'}"
                )
            time.sleep(0.05)
        check_cancelled()
        return str(plan_path)
    except Exception:
        if process is not None:
            (job / "cancel").touch()
        else:
            shutil.rmtree(job)
        raise


def detached_flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return 0


@contextlib.contextmanager
def installation_lock(updates: Path) -> Iterator[None]:
    with (updates / "portable-update.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def can_start_application() -> bool:
    if os.environ.pop(RESTART_ENV, None) == "1":
        return True
    updates = Path(os.environ["ANKI_PORTABLE_ROOT"]) / DATA_DIR / "updates"
    if not (updates / "portable-update.lock").exists():
        return True
    try:
        with installation_lock(updates):
            return True
    except (BlockingIOError, PermissionError):
        return False


def wait_for_exit(
    pid: int,
    cancel: Path,
    timeout: float = 600,
    *,
    ready: Callable[[], None] = lambda: None,
) -> bool:
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: already exited
                ready()
                return True
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ready()
            while not cancel.exists() and time.monotonic() < deadline:
                result = kernel.WaitForSingleObject(handle, 100)
                if result == 0:  # WAIT_OBJECT_0
                    return True
                if result != 258:  # WAIT_TIMEOUT
                    raise ctypes.WinError(ctypes.get_last_error())
            return False
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        ready()
        return True
    ready()
    while not cancel.exists() and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def move_entry(source: Path, target: Path) -> None:
    # Windows may release the old process's executable/DLL handles slightly later.
    deadline = time.monotonic() + 10
    while True:
        try:
            source.rename(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def copy_entry(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def apply_update(job: Path, plan: dict[str, Any]) -> None:
    root = Path(plan["root"])
    staged = job / "payload" / "Anki Portable"
    backup = job / "previous"
    backup.mkdir()
    moved = []
    installed = []
    save_state(job, "applying")
    try:
        for name in plan["old_entries"]:
            if (root / name).exists():
                move_entry(root / name, backup / name)
                moved.append(name)
        for name in plan["new_entries"]:
            installed.append(name)
            copy_entry(staged / name, root / name)
        save_state(job, "installed")
    except Exception:
        restore_previous(job, root, moved, installed)
        raise


def restore_previous(
    job: Path, root: Path, moved: list[str], installed: list[str]
) -> None:
    for name in installed:
        remove_entry(root / name)
    for name in moved:
        move_entry(job / "previous" / name, root / name)
    save_state(job, "rolled-back")


def start_application(
    root: Path, system: str, ack: Path | None = None
) -> subprocess.Popen:
    env = os.environ.copy()
    env.pop(PLAN_ENV, None)
    env.pop(ACK_ENV, None)
    env[RESTART_ENV] = "1"
    if ack is not None:
        env[ACK_ENV] = str(ack)
    return subprocess.Popen(
        [str(root / launcher_path(system))],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        creationflags=detached_flags(),
    )


def cleanup_old_jobs(job: Path, root: Path) -> None:
    # Called under the installation lock, after the new app acknowledged startup.
    for previous in job.parent.glob("portable-update-*"):
        if previous == job or previous.is_symlink() or not previous.is_dir():
            continue
        try:
            plan = json.loads((previous / "plan.json").read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                continue
            state = (previous / "state").read_text()
            if (
                plan.get("protocol") == 1
                and plan.get("root") == str(root)
                and state in ("complete", "unconfirmed", "rolled-back", "cancelled")
            ):
                shutil.rmtree(previous)
        except (OSError, ValueError):
            continue  # Retain incomplete or unrecognized jobs for recovery.


def run_helper(plan_path: Path) -> int:
    job = plan_path.resolve(strict=True).parent
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root = Path(plan["root"]).resolve(strict=True)
    if plan.get("protocol") != 1 or job.parent != root / DATA_DIR / "updates":
        raise ValueError("Invalid portable update plan location.")
    current = read_manifest(root)
    incoming = read_manifest(job / "payload" / "Anki Portable")
    if (
        plan.get("system") != current["system"]
        or type(plan.get("pid")) is not int
        or plan["pid"] <= 0
    ):
        raise ValueError("Invalid portable update process identity.")
    if (
        plan["old_entries"] != current["entries"]
        or plan["new_entries"] != incoming["entries"]
    ):
        raise ValueError("The application changed while preparing the update.")
    validate_payload(job / "payload" / "Anki Portable", incoming)
    with installation_lock(job.parent):
        save_state(job, "waiting")
        if not wait_for_exit(plan["pid"], job / "cancel", ready=(job / "ready").touch):
            save_state(job, "cancelled")
            return 1
        child = None
        try:
            if read_manifest(root) != current:
                raise ValueError("The application changed while waiting for shutdown.")
            validate_installation(root, current, incoming)
            apply_update(job, plan)
            child = start_application(root, plan["system"], job / "started")
            deadline = time.monotonic() + 120
            while not (job / "started").exists():
                if child.poll() is not None:
                    raise RuntimeError(
                        "The updated application exited before completing bootstrap."
                    )
                if time.monotonic() >= deadline:
                    save_state(job, "unconfirmed")
                    return 0  # Never replace files belonging to a still-running application.
                time.sleep(0.1)
            save_state(job, "complete")
            cleanup_old_jobs(job, root)
            return 0
        except Exception as error:
            # After acknowledgement, even a process that has exited may have
            # migrated profile data. Never restart the older app in that case.
            if child is None or (
                child.poll() is not None and not (job / "started").exists()
            ):
                if (job / "state").read_text() == "installed":
                    try:
                        moved = [
                            name
                            for name in plan["old_entries"]
                            if (job / "previous" / name).exists()
                        ]
                        restore_previous(job, root, moved, plan["new_entries"])
                    except Exception as rollback_error:
                        error = RuntimeError(
                            f"{error}\nRollback could not finish: {rollback_error}"
                        )
            else:
                save_state(job, "unconfirmed")
            (job.parent / "last-update-error.txt").write_text(
                f"{error}\n\nUpdate files and previous application: {job}",
                encoding="utf-8",
            )
            if (job / "state").read_text() in ("waiting", "rolled-back"):
                start_application(root, plan["system"])
            return 1


def acknowledge_startup() -> None:
    """Commit application startup before a new version can open/migrate profile data."""
    ack = os.environ.pop(ACK_ENV, None)
    root = Path(os.environ["ANKI_PORTABLE_ROOT"]).resolve()
    updates = root / DATA_DIR / "updates"
    if ack:
        path = Path(ack).resolve()
        if path.name == "started" and path.parent.parent == updates:
            path.touch()


def finish_startup() -> None:
    root = Path(os.environ["ANKI_PORTABLE_ROOT"]).resolve()
    updates = root / DATA_DIR / "updates"
    error = updates / "last-update-error.txt"
    if error.is_file():
        from aqt.utils import show_warning, tr

        detail = error.read_text(encoding="utf-8")
        error.unlink()
        show_warning(tr.qt_misc_portable_update_failed(details=detail))


if __name__ == "__main__":
    raise SystemExit(run_helper(Path(globals()["update_plan_path"])))
