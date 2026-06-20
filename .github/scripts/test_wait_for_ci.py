# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from wait_for_ci import run_has_failed, run_has_passed, select_ci_run


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
