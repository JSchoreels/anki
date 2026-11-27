# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Wait for the CI workflow run attached to a commit to pass."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

RELEVANT_EVENTS = {"push", "workflow_dispatch"}
FULL_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}")
PLATFORM_CHECKS = {
    "check (linux)",
    "check (linux-arm)",
    "check (macos)",
    "check (macos-intel)",
    "check (windows)",
    "check (windows-arm)",
}


def resolve_commit(repo: str | None, commit: str) -> str:
    if FULL_COMMIT_SHA.fullmatch(commit):
        return commit.lower()
    if not repo:
        raise ValueError(
            "--repo is required when --commit is a branch, tag, or short SHA"
        )

    encoded_commit = quote(commit, safe="")
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{encoded_commit}", "--jq", ".sha"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    resolved = result.stdout.strip()
    if not FULL_COMMIT_SHA.fullmatch(resolved):
        raise RuntimeError(f"GitHub returned an invalid commit SHA for {commit!r}")
    return resolved.lower()


def select_ci_run(
    runs: Sequence[dict[str, Any]],
    *,
    repo: str | None = None,
    full_matrix: bool = False,
) -> dict[str, Any] | None:
    for run in runs:
        if run.get("event") not in RELEVANT_EVENTS:
            continue
        if full_matrix and run_has_passed(run):
            jobs = fetch_ci_jobs(repo, run["databaseId"])
            passed = {job["name"] for job in jobs if job.get("conclusion") == "success"}
            if not PLATFORM_CHECKS <= passed:
                continue
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


def fetch_ci_jobs(repo: str | None, run_id: int) -> list[dict[str, Any]]:
    command = ["gh", "run", "view", str(run_id), "--json", "jobs"]
    if repo:
        command.extend(["--repo", repo])
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)
    return json.loads(result.stdout)["jobs"]


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
    *,
    full_matrix: bool = False,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        runs = fetch_ci_runs(repo, workflow, commit)
        run = select_ci_run(runs, repo=repo, full_matrix=full_matrix)

        if full_matrix and run is None and select_ci_run(runs) is not None:
            print(
                f"::error::No successful full CI matrix for {commit}. "
                "Dispatch the CI workflow for the release ref and try again.",
                file=sys.stderr,
            )
            return False

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
    parser.add_argument("--full-matrix", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    commit = resolve_commit(args.repo, args.commit)

    if not wait_for_ci(
        repo=args.repo,
        workflow=args.workflow,
        commit=commit,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        full_matrix=args.full_matrix,
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
