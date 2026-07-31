# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from subprocess import CompletedProcess

import pytest

from wait_for_ci import resolve_commit, run_has_failed, run_has_passed, select_ci_run


def test_full_commit_sha_does_not_require_resolution() -> None:
    commit = "a" * 40

    assert resolve_commit(None, commit.upper()) == commit


def test_resolves_remote_ref_to_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "b" * 40

    def run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert command == [
            "gh",
            "api",
            "repos/JSchoreels/anki/commits/main",
            "--jq",
            ".sha",
        ]
        assert kwargs == {"check": True, "stdout": -1, "text": True}
        return CompletedProcess(command, 0, stdout=f"{commit}\n")

    monkeypatch.setattr("wait_for_ci.subprocess.run", run)

    assert resolve_commit("JSchoreels/anki", "main") == commit


def test_encodes_remote_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "c" * 40

    def run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        assert command[2] == "repos/JSchoreels/anki/commits/release%2F26.05"
        return CompletedProcess(command, 0, stdout=f"{commit}\n")

    monkeypatch.setattr("wait_for_ci.subprocess.run", run)

    assert resolve_commit("JSchoreels/anki", "release/26.05") == commit


def test_remote_ref_requires_repo() -> None:
    with pytest.raises(ValueError, match="--repo is required"):
        resolve_commit(None, "main")


def test_selects_push_or_dispatch_run() -> None:
    run = select_ci_run(
        [
            {"event": "pull_request", "status": "completed", "conclusion": "success"},
            {
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    )

    assert run == {
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
    }


def test_ignores_missing_ci_run() -> None:
    assert select_ci_run([{"event": "pull_request"}]) is None


def test_success_requires_completed_successful_run() -> None:
    assert run_has_passed(
        {"event": "push", "status": "completed", "conclusion": "success"}
    )
    assert not run_has_passed(
        {"event": "push", "status": "in_progress", "conclusion": ""}
    )


def test_failure_requires_completed_unsuccessful_run() -> None:
    assert run_has_failed(
        {"event": "push", "status": "completed", "conclusion": "failure"}
    )
    assert not run_has_failed(
        {"event": "push", "status": "in_progress", "conclusion": ""}
    )
    assert not run_has_failed(None)
