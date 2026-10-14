# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Fresh-process native RWKV resource measurements and pairwise UM+."""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import queue
import statistics
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-not-found]
from tools.rwkv_um_plus import um_plus_pair

MODES = (
    "f32",
    "state_int8",
    "state_int4",
    "weights_int4_decoded",
    "weights_int4_resident",
    "weights_int4_fused",
    "state_int8_all",
)
SOURCE_FILES = (
    "rslib/src/rwkv/mod.rs",
    "rslib/src/rwkv/bulk.rs",
    "rslib/src/rwkv/precision.rs",
    "rslib/src/rwkv/packed_weights.rs",
    "rslib/src/rwkv/fused_packed.rs",
    "rslib/src/rwkv/runtime_precision_bench.rs",
    "rslib/src/rwkv/weight_precision_bench.rs",
    "rslib/src/rwkv/state_scope_bench.rs",
    "qt/tools/rwkv_runtime_precision_bench.py",
    "qt/tools/rwkv_runtime_cold_cache_bench.py",
    "qt/tools/rwkv_um_plus.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(root: Path, output: Path) -> dict[str, Any]:
    archive = output / "experiment-source.tar.gz"
    with tarfile.open(archive, "w:gz") as saved:
        for name in SOURCE_FILES:
            saved.add(root / name, arcname=name)
    return {
        "git_commit": subprocess.check_output(
            ["rtk", "git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "working_tree_status": subprocess.check_output(
            ["rtk", "git", "status", "--short"], cwd=root, text=True
        ).strip(),
        "source_sha256": {name: sha256(root / name) for name in SOURCE_FILES},
        "source_archive": str(archive),
        "source_archive_sha256": sha256(archive),
    }


def build(root: Path, output: Path) -> Path:
    with (
        (output / "build.jsonl").open("w") as stdout,
        (output / "build.log").open("w") as stderr,
    ):
        subprocess.run(
            [
                "cargo",
                "test",
                "-p",
                "anki",
                "--release",
                "--lib",
                "rwkv_runtime_precision_case",
                "--no-run",
                "--message-format=json",
            ],
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            check=True,
        )
    artifacts = [
        row["executable"]
        for line in (output / "build.jsonl").read_text().splitlines()
        if (row := json.loads(line)).get("reason") == "compiler-artifact"
        and row.get("executable")
        and row["target"]["name"] == "anki"
    ]
    if len(artifacts) != 1:
        raise ValueError("Expected one native Anki library test executable")
    return Path(artifacts[0])


def run_case(
    executable: Path,
    root: Path,
    input_path: Path,
    output: Path,
    packed: Path,
    mode: str,
    round_index: int,
    record_quality: bool,
    cache_action: str = "",
    cache_path: Path | None = None,
) -> dict[str, Any]:
    name = f"{round_index}-{mode}"
    env = {
        **os.environ,
        "ANKI_RWKV_RUNTIME_INPUT": str(input_path),
        "ANKI_RWKV_RUNTIME_MODE": mode,
        "ANKI_RWKV_RUNTIME_PACKED_MODEL": str(packed),
        "ANKI_RWKV_RUNTIME_HANDSHAKE": "1",
    }
    for key in ("ANKI_RWKV_RUNTIME_CACHE", "ANKI_RWKV_RUNTIME_CACHE_ACTION"):
        env.pop(key, None)
    if cache_action:
        if cache_path is None:
            raise ValueError("A scratch cache path is required")
        env["ANKI_RWKV_RUNTIME_CACHE"] = str(cache_path)
        env["ANKI_RWKV_RUNTIME_CACHE_ACTION"] = cache_action
    prediction_path = output / f"{mode}.f32"
    if record_quality:
        env["ANKI_RWKV_RUNTIME_PREDICTIONS"] = str(prediction_path)
    else:
        env.pop("ANKI_RWKV_RUNTIME_PREDICTIONS", None)
    background_cpu = psutil.cpu_percent(interval=0.2)
    process = subprocess.Popen(
        [
            str(executable),
            "rwkv::runtime_precision_bench::rwkv_runtime_precision_case",
            "--ignored",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None and process.stdin is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    monitored = psutil.Process(process.pid)
    phases: dict[str, Any] = {}
    result: dict[str, Any] = {
        "mode": mode,
        "round": round_index,
        "background_cpu_percent": background_cpu,
        "phases": phases,
    }
    active: str | None = None
    last_progress = time.monotonic()
    with (output / f"{name}.log").open("w") as log:
        while True:
            try:
                rss = monitored.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                rss = 0
            if active is not None:
                phases[active]["sampled_peak_rss_bytes"] = max(
                    phases[active]["sampled_peak_rss_bytes"], rss
                )
            if time.monotonic() - last_progress >= 60:
                print(f"Running {name}: {active or 'quality replay'}...", flush=True)
                last_progress = time.monotonic()
            try:
                line = lines.get(timeout=0.02)
            except queue.Empty:
                continue
            if line is None:
                break
            log.write(line)
            log.flush()
            if "RWKV_BENCH_EVENT=" not in line:
                continue
            event = json.loads(line.split("RWKV_BENCH_EVENT=", 1)[1])
            kind = event["kind"]
            rss = monitored.memory_info().rss
            if kind == "start":
                active = event["phase"]
                phases[active] = {"start_rss_bytes": rss, "sampled_peak_rss_bytes": rss}
            elif kind == "end":
                phase = phases[event["phase"]]
                phase.update(event)
                phase["end_rss_bytes"] = rss
                phase["sampled_peak_rss_bytes"] = max(
                    phase["sampled_peak_rss_bytes"], rss
                )
                phase["cpu_seconds"] = (
                    event["user_cpu_seconds"] + event["system_cpu_seconds"]
                )
                active = None
            elif kind == "summary":
                result["summary"] = event
            elif kind == "quality":
                result["quality"] = event["metrics"]
                result["predictions_sha256"] = sha256(prediction_path)
            process.stdin.write("ack\n")
            process.stdin.flush()
    process.wait()
    reader.join()
    if process.returncode:
        tail = (output / f"{name}.log").read_text().splitlines()[-25:]
        raise RuntimeError(f"Native case {name} failed:\n" + "\n".join(tail))
    if mode != "prepare" and "summary" not in result:
        raise RuntimeError(f"Native case {name} produced no summary")
    if mode != "prepare":
        primary = "cache_restore" if cache_action == "restore" else "state_only_rebuild"
        print(
            f"Finished {name}: {primary}={phases[primary]['wall_seconds']:.2f}s ready_RSS={phases[primary]['end_rss_bytes'] / 1e6:.1f}MB",
            flush=True,
        )
    return result


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for mode in MODES:
        selected = [run for run in runs if run["mode"] == mode]
        if not selected:
            continue
        rebuilds = [run["phases"]["state_only_rebuild"] for run in selected]
        queues = [
            run["phases"][f"queue_score_{i}"] for run in selected for i in range(3)
        ]
        answers = [
            sample for run in selected for sample in run["summary"]["answer_samples"]
        ]
        med = statistics.median
        output[mode] = {
            "rounds": len(selected),
            "queries": selected[0]["summary"]["queries"],
            "rayon_threads": selected[0]["summary"]["rayon_threads"],
            "model_load_seconds": med(
                run["phases"]["model_load"]["wall_seconds"] for run in selected
            ),
            "model_ready_rss_bytes": med(
                run["phases"]["model_load"]["end_rss_bytes"] for run in selected
            ),
            "rebuild_seconds": med(run["wall_seconds"] for run in rebuilds),
            "rebuild_cpu_seconds": med(run["cpu_seconds"] for run in rebuilds),
            "ready_rss_bytes": med(run["end_rss_bytes"] for run in rebuilds),
            "rebuild_sampled_peak_rss_bytes": med(
                run["sampled_peak_rss_bytes"] for run in rebuilds
            ),
            "queue_seconds": med(run["wall_seconds"] for run in queues),
            "queue_cpu_seconds": med(run["cpu_seconds"] for run in queues),
            "queue_sampled_peak_rss_bytes": med(
                run["sampled_peak_rss_bytes"] for run in queues
            ),
            "answer_seconds": med(run["wall_seconds"] for run in answers),
            "answer_cpu_seconds": med(run["cpu_seconds"] for run in answers),
        }
    return output


def quality(
    input_path: Path,
    output: Path,
    runs: list[dict[str, Any]],
    reference: Path | None = None,
) -> dict[str, Any]:
    source = json.loads(input_path.read_text())
    indices = []
    actual = []
    seen = set()
    for index, row in enumerate(source["rows"]):
        previous = row[1] in seen
        seen.add(row[1])
        if previous and row[9] >= 1 and row[11]:
            indices.append(index)
            actual.append(float(row[5] != 1))
    probabilities = {}
    files = {}
    for mode in MODES:
        path = output / f"{mode}.f32"
        if not path.exists() and reference is not None:
            path = reference / path.name
        if not path.exists():
            continue
        files[mode] = path
        values = array.array("f")
        values.frombytes(path.read_bytes())
        if sys.byteorder != "little":
            values.byteswap()
        if len(values) != len(source["rows"]):
            raise ValueError(f"Incomplete predictions for {mode}")
        probabilities[mode] = [float(values[index]) for index in indices]
    if (
        files["weights_int4_decoded"].read_bytes()
        != files["weights_int4_resident"].read_bytes()
    ):
        raise ValueError("Resident int4 changed predictions from decoded int4")
    excluded = {"weights_int4_decoded"}
    if "weights_int4_fused" in probabilities:
        excluded.add("weights_int4_resident")
    models = [mode for mode in MODES if mode in probabilities and mode not in excluded]
    matrix = []
    pairs = {}
    for first in models:
        scores: list[float | None] = []
        for second in models:
            if first == second:
                scores.append(None)
                continue
            pair = um_plus_pair(actual, probabilities[first], probabilities[second])
            if pair["count"] != len(indices):
                raise ValueError("UM+ models must have exactly the same valid outcomes")
            scores.append(pair["first_score"])
            pairs[f"{first} vs {second}"] = pair
        matrix.append(scores)
    rankings: list[dict[str, Any]] = []
    for mode, scores in zip(models, matrix, strict=True):
        valid = [score for score in scores if score is not None]
        rankings.append(
            {"model": mode, "max": max(valid), "mean": statistics.mean(valid)}
        )
    result = {
        "count": len(indices),
        "models": models,
        "matrix": matrix,
        "rankings": sorted(rankings, key=lambda row: row["max"]),
        "pairs": pairs,
        "metrics": {run["mode"]: run["quality"] for run in runs if "quality" in run},
        "resident_matches_decoded_exactly": True,
    }
    if "weights_int4_fused" in probabilities:
        differences = [
            abs(first - second)
            for first, second in zip(
                probabilities["weights_int4_fused"],
                probabilities["weights_int4_decoded"],
                strict=True,
            )
        ]
        result["fused_vs_decoded"] = {
            "max_probability_difference": max(differences),
            "mean_probability_difference": statistics.mean(differences),
            "full_replay_byte_identical": files["weights_int4_fused"].read_bytes()
            == files["weights_int4_decoded"].read_bytes(),
        }
    return result


def plot(result_path: Path, output: Path) -> None:
    import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    result = json.loads(result_path.read_text())
    labels = {
        "f32": "W32 / S32",
        "state_int8": "W32 / S8",
        "state_int4": "W32 / S4",
        "weights_int4_resident": "W4 / S8",
        "weights_int4_fused": "W4 fused / S8",
        "state_int8_all": "W32 / S8 all",
    }
    um = result["um_plus"]
    values = np.array(
        [[math.nan if v is None else v for v in row] for row in um["matrix"]]
    )
    fig, axis = plt.subplots(figsize=(7.5, 6), layout="constrained")
    colors = axis.imshow(values, cmap="YlOrRd", vmin=0)
    names = [labels[mode] for mode in um["models"]]
    axis.set_xticks(range(len(names)), names, rotation=25, ha="right")
    axis.set_yticks(range(len(names)), names)
    axis.set_xlabel("Opponent used to form disagreement bins")
    axis.set_ylabel("Model being scored")
    axis.set_title(
        f"RWKV UM+ · {um['count']:,} matched interday reviews\nLower is better"
    )
    for row in range(len(names)):
        for column in range(len(names)):
            value = values[row, column]
            axis.text(
                column,
                row,
                "—" if math.isnan(value) else f"{value:.5f}",
                ha="center",
                va="center",
                color="white"
                if not math.isnan(value) and value > np.nanmax(values) * 0.6
                else "black",
            )
    fig.colorbar(colors, ax=axis, label="UM+")
    fig.savefig(output / "um-plus.png", dpi=160)
    fig.savefig(output / "um-plus.svg")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--plot-only", type=Path)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    if args.plot_only:
        plot(args.plot_only, output)
        return
    if args.input is None or args.rounds < 1:
        parser.error("supply --input and --rounds >= 1")
    input_path = args.input.resolve(strict=True)
    root = Path(__file__).resolve().parents[2]
    original_model = root / "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"
    metadata: dict[str, Any] = {
        "provenance": provenance(root, output),
        "input_sha256": sha256(input_path),
        "model_sha256": sha256(original_model),
        "logical_cpus": os.cpu_count(),
        "platform": sys.platform,
        "sampling_interval_seconds": 0.02,
        "threads_environment": {
            key: os.environ.get(key)
            for key in ("RAYON_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
        },
    }
    if args.reference:
        reference_result = json.loads((args.reference / "results.json").read_text())
        for key in ("input_sha256", "model_sha256"):
            if reference_result[key] != metadata[key]:
                raise ValueError(f"Reference {key} does not match")
        metadata["reference_result"] = str(args.reference / "results.json")
        metadata["reference_result_sha256"] = sha256(args.reference / "results.json")
    print("Building native runtime harness...", flush=True)
    executable = build(root, output)
    metadata["executable_sha256"] = sha256(executable)
    packed = output / "model.int4.bin"
    run_case(executable, root, input_path, output, packed, "prepare", -1, False)
    metadata["packed_model_sha256"] = sha256(packed)
    runs: list[dict[str, Any]] = []
    modes = tuple(args.modes)
    for round_index in range(args.rounds):
        order = modes[round_index % len(modes) :] + modes[: round_index % len(modes)]
        for mode in order:
            run = run_case(
                executable,
                root,
                input_path,
                output,
                packed,
                mode,
                round_index,
                round_index == 0,
            )
            runs.append(run)
            (output / "results.json").write_text(
                json.dumps(
                    {**metadata, "runs": runs, "aggregate": aggregate(runs)}, indent=2
                )
            )
    result = {
        **metadata,
        "runs": runs,
        "aggregate": aggregate(runs),
        "um_plus": quality(input_path, output, runs, args.reference),
    }
    for name, expected in metadata["provenance"]["source_sha256"].items():
        if sha256(root / name) != expected:
            raise ValueError(f"Experiment source changed during measurement: {name}")
    for path, key in ((input_path, "input_sha256"), (original_model, "model_sha256")):
        if sha256(path) != metadata[key]:
            raise ValueError(f"{key} changed during measurement")
    int4_states = {
        run["summary"]["state_blake3"]
        for run in runs
        if run["mode"].startswith("weights_int4_")
    }
    if len(int4_states) > 1:
        raise ValueError("Int4 computation paths changed the saved replay state")
    (output / "results.json").write_text(json.dumps(result, indent=2))
    print(f"Runtime comparison and UM+ complete: {output / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
