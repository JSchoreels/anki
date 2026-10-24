# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Restore scratch RWKV checkpoints in fresh processes to measure steady RAM."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from tools.rwkv_runtime_precision_bench import (
    MODES,
    build,
    provenance,
    run_case,
    sha256,
)


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    med = statistics.median
    for mode in MODES:
        selected = [run for run in runs if run["mode"] == mode]
        restores = [run["phases"]["cache_restore"] for run in selected]
        queues = [
            run["phases"][f"queue_score_{i}"] for run in selected for i in range(3)
        ]
        answers = [
            sample for run in selected for sample in run["summary"]["answer_samples"]
        ]
        output[mode] = {
            "rounds": len(selected),
            "queries": selected[0]["summary"]["queries"],
            "cache_restore_seconds": med(phase["wall_seconds"] for phase in restores),
            "cache_restore_cpu_seconds": med(
                phase["cpu_seconds"] for phase in restores
            ),
            "ready_rss_bytes": med(phase["end_rss_bytes"] for phase in restores),
            "restore_sampled_peak_rss_bytes": med(
                phase["sampled_peak_rss_bytes"] for phase in restores
            ),
            "queue_seconds": med(phase["wall_seconds"] for phase in queues),
            "queue_cpu_seconds": med(phase["cpu_seconds"] for phase in queues),
            "queue_sampled_peak_rss_bytes": med(
                phase["sampled_peak_rss_bytes"] for phase in queues
            ),
            "answer_seconds": med(sample["wall_seconds"] for sample in answers),
            "answer_cpu_seconds": med(sample["cpu_seconds"] for sample in answers),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be >= 1")
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob("*.sqlite")):
        raise ValueError("Use a fresh output directory; scratch caches must be new")
    input_path = args.input.resolve(strict=True)
    reference_path = args.reference.resolve(strict=True)
    reference = json.loads(reference_path.read_text())
    # A fused comparison may reuse earlier precision predictions and runtime
    # results. Retain both exact source snapshots, rather than relabeling them.
    reference_runs = list(reference["runs"])
    if earlier := reference.get("reference_result"):
        reference_runs = json.loads(Path(earlier).read_text())["runs"] + reference_runs
    model = root / "qt/aqt/rwkv_inference/sm40v2/weights.f32.bin"
    metadata: dict[str, Any] = {
        "provenance": provenance(root, output),
        "input_sha256": sha256(input_path),
        "model_sha256": sha256(model),
        "reference_result": str(reference_path),
        "reference_result_sha256": sha256(reference_path),
        "sampling_interval_seconds": 0.02,
    }
    for key in ("input_sha256", "model_sha256"):
        if reference[key] != metadata[key]:
            raise ValueError(f"Reference {key} does not match")
    source = json.loads(input_path.read_text())
    seen = set()
    query_rows = []
    for row in reversed(source["rows"]):
        if row[1] not in seen:
            seen.add(row[1])
            query_rows.append(row)
            if len(query_rows) == 4096:
                break
    query_rows.reverse()
    if max(row[8] for row in query_rows) != max(row[8] for row in source["rows"]):
        raise ValueError("Query rows must preserve the full-history end day")
    query_input = output / "queries.json"
    query_input.write_text(json.dumps({"rows": query_rows}))
    metadata["query_input_sha256"] = sha256(query_input)
    del source
    executable = build(root, output)
    metadata["executable_sha256"] = sha256(executable)
    packed = reference_path.parent / "model.int4.bin"
    metadata["packed_model_sha256"] = sha256(packed)
    caches = {mode: output / f"{mode}.sqlite" for mode in MODES}
    caches["weights_int4_decoded"] = caches["weights_int4_resident"]
    caches["weights_int4_fused"] = caches["weights_int4_resident"]
    builders = []
    for mode in MODES:
        if mode in ("weights_int4_decoded", "weights_int4_fused"):
            continue
        builders.append(
            run_case(
                executable,
                root,
                input_path,
                output,
                packed,
                mode,
                -1,
                False,
                "write",
                caches[mode],
            )
        )
    runs = []
    for round_index in range(args.rounds):
        order = MODES[round_index % len(MODES) :] + MODES[: round_index % len(MODES)]
        for mode in order:
            run = run_case(
                executable,
                root,
                query_input,
                output,
                packed,
                mode,
                round_index,
                False,
                "restore",
                caches[mode],
            )
            expected = next(
                previous["summary"]["queue_checksums"]
                for previous in reversed(reference_runs)
                if previous["mode"] == mode
            )
            if run["summary"]["queue_checksums"] != expected:
                raise ValueError(
                    f"Restored {mode} query results differ from full replay"
                )
            builder_mode = (
                "weights_int4_resident"
                if mode in ("weights_int4_decoded", "weights_int4_fused")
                else mode
            )
            expected_state = next(
                builder["summary"]["state_blake3"]
                for builder in builders
                if builder["mode"] == builder_mode
            )
            if run["summary"]["state_blake3"] != expected_state:
                raise ValueError(f"Restored {mode} state differs from its saved cache")
            for previous in reference_runs:
                if (
                    previous["mode"] == mode
                    and "state_blake3" in previous["summary"]
                    and previous["summary"]["state_blake3"] != expected_state
                ):
                    raise ValueError(f"{mode} saved state differs from full replay")
            runs.append(run)
            (output / "results.json").write_text(
                json.dumps({**metadata, "builders": builders, "runs": runs}, indent=2)
            )
    for name, expected in metadata["provenance"]["source_sha256"].items():
        if sha256(root / name) != expected:
            raise ValueError(f"Source changed during measurement: {name}")
    for path, key in ((input_path, "input_sha256"), (model, "model_sha256")):
        if sha256(path) != metadata[key]:
            raise ValueError(f"{key} changed during measurement")
    result = {
        **metadata,
        "builders": builders,
        "runs": runs,
        "aggregate": aggregate(runs),
        "cache_file_bytes": {
            mode: path.stat().st_size for mode, path in caches.items()
        },
        "restored_queries_match_full_replay": True,
    }
    (output / "results.json").write_text(json.dumps(result, indent=2))
    print(
        f"Cold-cache resource comparison complete: {output / 'results.json'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
