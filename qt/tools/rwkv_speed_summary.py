# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Summarize the JSON lines of `qt/tools/rwkv_speed_test.ps1` into medians.

    python qt/tools/rwkv_speed_summary.py RESULTS.jsonl [more.jsonl ...]

Groups runs by runtime, operation and cold/warm (cold: iteration 0 of a
fresh process; warm: later iterations of one process, after an untimed
first run), and prints a Markdown table of the
median wall time, its range, replay reviews/s, this process's CPU cores,
the other load on the machine, peak RAM, the state store size and, for
builds, an app estimate (the median plus the measured Python side). A
`state` run adds the stored bytes per card and per note.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

ORDER = [
    "big-pair",
    "big-one",
    "big-instant",
    "small-pair",
    "small-one",
    "small-curve",
    "small-instant",
    "tiny-pair",
    "tiny-instant",
]


def main() -> None:
    rows = []
    for path in sys.argv[1:]:
        with open(path, encoding="utf8") as file:
            rows.extend(json.loads(line) for line in file if line.strip())
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    states: dict[str, dict] = {}
    python = [row for row in rows if row.get("op") == "python-overhead"]
    for row in rows:
        if "runtime" not in row:
            continue
        if row["op"] == "state":
            states[row["runtime"]] = row
            continue
        label = row.get("label", "")
        if label == "dump" or (label.startswith("warm") and row["iteration"] == 0):
            # The history dump, and the untimed first run of a warm process.
            continue
        warmth = "cold" if row["iteration"] == 0 else "warm"
        op = row["op"]
        if op == "score":
            op = "score after " + ("load" if label.endswith("load") else "build")
        groups[(row["runtime"], op, warmth)].append(row)

    def key(item: tuple[str, str, str]) -> tuple[int, str, str, str]:
        runtime, op, warmth = item
        rank = ORDER.index(runtime) if runtime in ORDER else len(ORDER)
        return (rank, runtime, op, warmth)

    print(
        "| Runtime | Operation | Cold / warm | Runs | Median s | Min–max s "
        "| Replay reviews/s | Process cores | Other load % | Peak RAM MB | Store MB "
        "| App estimate s |"
    )
    print("|" + " --- |" * 12)
    med = statistics.median
    # A build in the app adds the Python side (serial with the replay).
    python_s = med(row["total_ms"] for row in python) / 1000 if python else None
    for group in sorted(groups, key=key):
        runs = groups[group]
        wall = [run["total_ms"] / 1000 for run in runs]
        replay = [run["replay_reviews_per_s"] for run in runs if "replay_reviews_per_s" in run]
        store = [run["store_bytes"] for run in runs if "store_bytes" in run]
        print(
            f"| {group[0]} | {group[1]} | {group[2]} | {len(runs)} "
            f"| {med(wall):.2f} | {min(wall):.2f}–{max(wall):.2f} "
            f"| {f'{med(replay):,.0f}' if replay else ''} "
            f"| {med(run['process_cores'] for run in runs):.1f} "
            f"| {med(run['other_busy_pct'] for run in runs):.1f} "
            f"| {max(run['peak_working_set_mb'] for run in runs):,.0f} "
            f"| {f'{med(store) / 1048576:,.0f}' if store else ''} "
            f"| {f'{med(wall) + python_s:.2f}' if python_s and group[1] == 'build' else ''} |"
        )
    if states:
        print()
        print("| Runtime | Cards | Notes | Bytes per card | Bytes per note | Card + note MB |")
        print("|" + " --- |" * 6)
        for runtime in sorted(states, key=lambda r: ORDER.index(r) if r in ORDER else 99):
            state = states[runtime]
            total = (state["card_state_bytes"] + state["note_state_bytes"]) / 1048576
            print(
                f"| {runtime} | {state['cards']:,} | {state['notes']:,} "
                f"| {state['bytes_per_card']:,} | {state['bytes_per_note']:,} | {total:,.0f} |"
            )
    if python:
        print()
        print(
            "Python side of a build (median of "
            f"{len(python)}): {med(row['total_ms'] for row in python) / 1000:.2f} s "
            f"(parse {med(row['parse_ms'] for row in python):.0f} ms, convert "
            f"{med(row['convert_ms'] for row in python):.0f} ms, pack "
            f"{med(row['pack_ms'] for row in python):.0f} ms) for "
            f"{python[0]['reviews']:,} reviews."
        )


if __name__ == "__main__":
    main()
