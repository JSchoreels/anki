# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Wait for the CI workflow run attached to a commit to pass."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

RELEVANT_EVENTS = {"push", "workflow_dispatch"}


def select_ci_run(runs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    for run in runs:
        if run.get("event") in RELEVANT_EVENTS:
            return run

    return None


def run_has_passed(run: dict[str, Any] | None) -> bool:
    return (
        run is not None
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def run_has_failed(run: dict[str, Any] | None) -> bool:
    return (
        run is not None
        and run.get("status") == "completed"
        and run.get("conclusion") != "success"
    )


def fetch_ci_runs(repo: str | None, workflow: str, commit: str) -> list[dict[str, Any]]:
    command = [
        "gh",
        "run",
        "list",
        "--workflow",
        workflow,
        "--commit",
        commit,
        "--limit",
        "10",
        "--json",
        "conclusion,databaseId,event,status,url",
    ]
    if repo:
        command.extend(["--repo", repo])

    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return json.loads(result.stdout)


def describe_run(run: dict[str, Any] | None) -> str:
    if run is None:
        return "no matching CI run found yet"

    status = run.get("status") or "unknown"
    conclusion = run.get("conclusion") or "pending"
    url = run.get("url") or "unknown URL"
    return f"CI run {run.get('databaseId')} is {status}/{conclusion}: {url}"


def wait_for_ci(
    repo: str | None,
    workflow: str,
    commit: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        run = select_ci_run(fetch_ci_runs(repo, workflow, commit))

        if run_has_passed(run):
            print(f"::notice::{describe_run(run)}")
            return True

        if run_has_failed(run):
            print(f"::error::{describe_run(run)}", file=sys.stderr)
            return False

        if time.monotonic() >= deadline:
            print(
                f"::error::Timed out waiting for CI for commit {commit}: "
                f"{describe_run(run)}",
                file=sys.stderr,
            )
            return False

        print(f"::notice::{describe_run(run)}; waiting {poll_seconds}s")
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--repo")
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()

    if not wait_for_ci(
        repo=args.repo,
        workflow=args.workflow,
        commit=args.commit,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
