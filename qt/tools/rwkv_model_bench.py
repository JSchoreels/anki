# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Compare legacy/SM40 native reconstruction, storage, calibration and paired UM+."""

from __future__ import annotations

import argparse
import array
import itertools
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
from pathlib import Path

from tools.rwkv_runtime_precision_bench import run_case, sha256
from tools.rwkv_um_plus import um_plus_pair

MODELS = {
    "legacy": "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin",
    "sm40": "qt/aqt/rwkv_inference/sm40v2/weights.f32.bin",
}
VARIANTS = {
    "legacy_int8": ("legacy", "state_int8"),
    "sm40_f32": ("sm40", "f32"),
    "sm40_int8": ("sm40", "state_int8"),
}


def build_native(root: Path, output: Path, model: str) -> Path:
    command = [
        "rtk",
        "proxy",
        "cargo",
        "test",
        "-p",
        "anki",
        "--release",
        "--lib",
        "--no-run",
        "--message-format=json",
    ]
    if model == "legacy":
        command.append("--no-default-features")
    with (
        (output / "build.jsonl").open("w") as out,
        (output / "build.log").open("w") as err,
    ):
        subprocess.run(command, cwd=root, stdout=out, stderr=err, check=True)
    artifacts = [
        row["executable"]
        for line in (output / "build.jsonl").read_text().splitlines()
        if (row := json.loads(line)).get("reason") == "compiler-artifact"
        and row.get("executable")
        and row["target"]["name"] == "anki"
    ]
    if len(artifacts) != 1:
        raise ValueError("Expected one native test executable")
    executable = output / "native"
    shutil.copy2(artifacts[0], executable)
    return executable


def read_predictions(path: Path, count: int) -> list[float]:
    values = array.array("f")
    values.frombytes(path.read_bytes())
    if sys.byteorder != "little":
        values.byteswap()
    if len(values) != count or any(
        not math.isfinite(p) or not 0 <= p <= 1 for p in values
    ):
        raise ValueError(f"Incomplete or invalid predictions: {path}")
    return list(values)


def calibration(actual: list[float], predictions: list[float]) -> dict:
    bins = [[0, 0.0, 0.0] for _ in range(20)]
    loss = brier = 0.0
    for y, p in zip(actual, predictions, strict=True):
        bucket = bins[min(19, int(p * 20))]
        bucket[0] += 1
        bucket[1] += p
        bucket[2] += y
        brier += (p - y) ** 2
        clipped = min(1 - 1e-6, max(1e-6, p))
        loss -= math.log(clipped) if y else math.log1p(-clipped)
    count = len(actual)
    if not count:
        raise ValueError("No matched outcomes")
    populated = [
        {"index": i, "count": int(n), "prediction": p / n, "observed": y / n}
        for i, (n, p, y) in enumerate(bins)
        if n
    ]
    return {
        "log_loss": loss / count,
        "brier": brier / count,
        "mean_prediction": statistics.mean(predictions),
        "observed_recall": statistics.mean(actual),
        "bias": statistics.mean(predictions) - statistics.mean(actual),
        "ece_20": sum(
            b["count"] * abs(b["prediction"] - b["observed"]) for b in populated
        )
        / count,
        "rmse_bins_20": math.sqrt(
            sum(b["count"] * (b["prediction"] - b["observed"]) ** 2 for b in populated)
            / count
        ),
        "bins": populated,
    }


def compare(rows: list, output: Path) -> dict:
    seen = set()
    selections: dict[str, list[int]] = {"interday": [], "all_repeated": []}
    for index, row in enumerate(rows):
        if row[1] in seen and row[11]:
            selections["all_repeated"].append(index)
            if row[9] >= 1:
                selections["interday"].append(index)
        seen.add(row[1])
    predictions = {
        (model, head): read_predictions(output / model / f"{head}.f32", len(rows))
        for model, head in itertools.product(VARIANTS, ("curve", "instant"))
    }
    result = {}
    for selection, indices in selections.items():
        actual = [float(rows[i][5] != 1) for i in indices]
        metrics: dict[str, dict] = {}
        pairs: dict[str, dict] = {}
        for head in ("curve", "instant"):
            probabilities = {
                model: [predictions[model, head][i] for i in indices]
                for model in VARIANTS
            }
            metrics[head] = {
                model: calibration(actual, values)
                for model, values in probabilities.items()
            }
            pairs[head] = {}
            for first, second in itertools.combinations(VARIANTS, 2):
                pair = um_plus_pair(actual, probabilities[first], probabilities[second])
                if pair["count"] != len(indices):
                    raise ValueError("UM+ dropped reviews")
                pairs[head][f"{first} vs {second}"] = pair
        result[selection] = {
            "reviews": len(indices),
            "metrics": metrics,
            "um_plus": pairs,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    source_path = args.input.resolve(strict=True)
    source = json.loads(source_path.read_text())
    sources = [
        *root.glob("rslib/src/rwkv/*.rs"),
        root / "qt/tools/rwkv_model_bench.py",
        root / "qt/tools/rwkv_um_plus.py",
        root / "qt/tools/rwkv_runtime_precision_bench.py",
        root / "Cargo.toml",
        root / "Cargo.lock",
        root / "rslib/Cargo.toml",
    ]
    metadata = {
        "input_sha256": sha256(source_path),
        "backup_sha256": source["backup_sha256"],
        "models": {
            name: {
                "sha256": sha256(root / path),
                "file_bytes": (root / path).stat().st_size,
            }
            for name, path in MODELS.items()
        },
        "source_sha256": {str(p.relative_to(root)): sha256(p) for p in sources},
        "git_commit": subprocess.check_output(
            ["rtk", "proxy", "git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "variants": {name: list(value) for name, value in VARIANTS.items()},
        "state_precision": "SM40 f32 primary; legacy and SM40 INT8 card/note comparisons; shared scopes f32",
        "weights_precision": "f32",
        "retained_answers": len(source["rows"]),
        "rounds": args.rounds,
    }
    if args.resume:
        previous = json.loads((output / "metadata.json").read_text())
        if previous != metadata:
            raise ValueError("Source/model/input changed; use a new output directory")
    else:
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
        with tarfile.open(output / "sources.tar.gz", "w:gz") as archive:
            for path in sources:
                archive.add(path, arcname=str(path.relative_to(root)))
    executables = {}
    for model in MODELS:
        directory = output / model
        directory.mkdir(exist_ok=args.resume)
        print(f"Building {model} native benchmark...", flush=True)
        executables[model] = (
            directory / "native"
            if args.resume and (directory / "native").exists()
            else build_native(root, directory, model)
        )
        with (directory / "regression.log").open("w") as log:
            subprocess.run(
                [str(executables[model]), "rwkv::", "--test-threads=1"],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
    for variant in VARIANTS:
        (output / variant).mkdir(exist_ok=args.resume)
    runs: dict[str, list[dict]] = {variant: [] for variant in VARIANTS}
    for round_index in range(args.rounds):
        for variant in (
            list(VARIANTS) if round_index % 2 == 0 else list(reversed(VARIANTS))
        ):
            model, mode = VARIANTS[variant]
            directory = output / variant
            saved = directory / f"round-{round_index}.json"
            cache = directory / f"state-{round_index}.sqlite"
            if args.resume and saved.exists():
                run = json.loads(saved.read_text())
            else:
                run = run_case(
                    executables[model],
                    root,
                    source_path,
                    directory,
                    directory / "unused.bin",
                    mode,
                    round_index,
                    False,
                    cache_action="write",
                    cache_path=cache,
                )
                run["cache_file_bytes"] = cache.stat().st_size
                run["reconstruction_total_seconds"] = sum(
                    run["phases"][phase]["wall_seconds"]
                    for phase in (
                        "input_preparation",
                        "model_load",
                        "state_only_rebuild",
                        "cache_save",
                    )
                )
                saved.write_text(json.dumps(run, indent=2))
            runs[variant].append(run)
    restores = {}
    for variant, (model, mode) in VARIANTS.items():
        directory = output / variant
        saved = directory / "restore.json"
        if args.resume and saved.exists():
            restores[variant] = json.loads(saved.read_text())
        else:
            restores[variant] = run_case(
                executables[model],
                root,
                source_path,
                directory,
                directory / "unused.bin",
                mode,
                args.rounds,
                False,
                cache_action="restore",
                cache_path=directory / "state-0.sqlite",
            )
            saved.write_text(json.dumps(restores[variant], indent=2))
        state_hashes = {run["summary"]["state_blake3"] for run in runs[variant]}
        if (
            len(state_hashes) != 1
            or restores[variant]["summary"]["state_blake3"] not in state_hashes
        ):
            raise ValueError(f"Rebuild/restore state differs across runs for {variant}")
        if not (args.resume and (directory / "quality.done").exists()):
            print(f"Scoring {variant} curve and instant...", flush=True)
            with (directory / "quality.log").open("w") as log:
                subprocess.run(
                    [
                        str(executables[model]),
                        "rwkv::model_bench::rwkv_model_quality_case",
                        "--exact",
                        "--ignored",
                        "--nocapture",
                        "--test-threads=1",
                    ],
                    cwd=root,
                    env={
                        **os.environ,
                        "ANKI_RWKV_MODEL_INPUT": str(source_path),
                        "ANKI_RWKV_MODEL_OUTPUT": str(directory),
                        "ANKI_RWKV_MODEL_PRECISION": "f32" if mode == "f32" else "int8",
                    },
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            (directory / "quality.done").touch()
    result = {
        "metadata": metadata,
        "executable_sha256": {model: sha256(exe) for model, exe in executables.items()},
        "runs": runs,
        "restore": restores,
        "quality": compare(source["rows"], output),
        "prediction_sha256": {
            model: {
                head: sha256(output / model / f"{head}.f32")
                for head in ("curve", "instant")
            }
            for model in VARIANTS
        },
    }
    result["resource_medians"] = {
        model: {
            "rebuild_seconds": statistics.median(
                r["phases"]["state_only_rebuild"]["wall_seconds"] for r in values
            ),
            "total_seconds": statistics.median(
                r["reconstruction_total_seconds"] for r in values
            ),
            "cache_file_bytes": statistics.median(
                r["cache_file_bytes"] for r in values
            ),
            "peak_rebuild_rss_bytes": statistics.median(
                r["phases"]["state_only_rebuild"]["sampled_peak_rss_bytes"]
                for r in values
            ),
            "ready_rss_bytes": restores[model]["phases"]["cache_restore"][
                "end_rss_bytes"
            ],
        }
        for model, values in runs.items()
    }
    for name, digest in metadata["source_sha256"].items():
        if sha256(root / name) != digest:
            raise ValueError(f"Source changed during measurement: {name}")
    if sha256(source_path) != metadata["input_sha256"] or any(
        sha256(root / MODELS[model]) != metadata["models"][model]["sha256"]
        for model in MODELS
    ):
        raise ValueError("Input or model changed during measurement")
    (output / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["resource_medians"], indent=2), flush=True)
    print(f"Saved comparison: {output / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
