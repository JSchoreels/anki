# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Measure lossless checkpoint codecs in isolated native processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import statistics
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-not-found]

CODECS = ("raw", "zstd1", "zstd2", "zstd3", "sqlite")
TEST = "rwkv::checkpoint_compression_bench::rwkv_checkpoint_compression_case"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build(root: Path, output: Path) -> Path:
    with (
        (output / "build.jsonl").open("w") as stdout,
        (output / "build.log").open("w") as stderr,
    ):
        subprocess.run(
            [
                "rtk",
                "proxy",
                "just",
                "rwkv-checkpoint-compression-native",
                "--no-run",
                "--message-format=json",
            ],
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            check=True,
        )
    executables = [
        row["executable"]
        for line in (output / "build.jsonl").read_text().splitlines()
        if (row := json.loads(line)).get("reason") == "compiler-artifact"
        and row.get("executable")
        and row["target"]["name"] == "anki"
    ]
    if len(executables) != 1:
        raise ValueError("Expected one native Anki test executable")
    executable = output / "native"
    shutil.copy2(executables[0], executable)
    return executable


def run_case(
    executable: Path, root: Path, output: Path, name: str, request: dict[str, Any]
) -> dict[str, Any]:
    request_path = output / f"{name}.request.json"
    request_path.write_text(json.dumps(request, indent=2))
    environment = {
        **os.environ,
        "ANKI_RWKV_CHECKPOINT_REQUEST": str(request_path),
        "ANKI_RWKV_CHECKPOINT_HANDSHAKE": "1",
    }
    process = subprocess.Popen(
        [
            str(executable),
            TEST,
            "--ignored",
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ],
        cwd=root,
        env=environment,
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
    result: dict[str, Any] = {"name": name, "request": request, "phases": {}}
    active: dict[str, Any] | None = None
    progress = time.monotonic()
    with (output / f"{name}.log").open("w") as log:
        while True:
            try:
                rss = monitored.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                rss = 0
            if active is not None:
                active["sampled_peak_rss_bytes"] = max(
                    active["sampled_peak_rss_bytes"], rss
                )
            if time.monotonic() - progress >= 45:
                print(f"Running {name}...", flush=True)
                progress = time.monotonic()
            try:
                line = lines.get(timeout=0.002)
            except queue.Empty:
                continue
            if line is None:
                break
            log.write(line)
            log.flush()
            if "RWKV_CHECKPOINT_EVENT=" not in line:
                continue
            event = json.loads(line.split("RWKV_CHECKPOINT_EVENT=", 1)[1])
            rss = monitored.memory_info().rss
            if event["kind"] == "start":
                active = {"start_rss_bytes": rss, "sampled_peak_rss_bytes": rss}
                result["phases"][event["phase"]] = active
            elif event["kind"] == "end":
                active = result["phases"][event["phase"]]
                active.update(event)
                active["end_rss_bytes"] = rss
                active["sampled_peak_rss_bytes"] = max(
                    active["sampled_peak_rss_bytes"], rss
                )
                active["sampled_peak_increase_bytes"] = (
                    active["sampled_peak_rss_bytes"] - active["start_rss_bytes"]
                )
                active = None
            elif event["kind"] == "summary":
                result["summary"] = event
            process.stdin.write("ack\n")
            process.stdin.flush()
    process.wait()
    reader.join()
    process.stdin.close()
    process.stdout.close()
    if process.returncode or "summary" not in result:
        tail = (output / f"{name}.log").read_text().splitlines()[-25:]
        raise RuntimeError(f"Native case failed: {name}\n" + "\n".join(tail))
    (output / f"{name}.json").write_text(json.dumps(result, indent=2))
    return result


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for codec in CODECS:
        selected = [run for run in runs if run["request"]["codec"] == codec]
        saves = [run for run in selected if run["request"]["action"] == "save"]
        sizes = {run["summary"]["file_bytes"] for run in saves}
        if len(sizes) != 1:
            raise ValueError(f"Checkpoint size changed across rounds for {codec}")
        result[codec] = {"file_bytes": sizes.pop(), "rounds": len(saves)}
        for action in ("save", "restore"):
            phases = [
                run["phases"][action]
                for run in selected
                if run["request"]["action"] == action
            ]
            result[codec][action] = {
                field: statistics.median(phase[field] for phase in phases)
                for field in (
                    "wall_seconds",
                    "start_rss_bytes",
                    "sampled_peak_rss_bytes",
                    "end_rss_bytes",
                    "sampled_peak_increase_bytes",
                    "process_peak_rss_bytes",
                )
            }
            result[codec][action]["wall_seconds_samples"] = [
                p["wall_seconds"] for p in phases
            ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rounds", default=5, type=int)
    parser.add_argument("--block-bytes", default=1024 * 1024, type=int)
    parser.add_argument(
        "--precision",
        default="cards_int4_notes_int8",
        choices=("cards_int4_notes_int8", "int8", "int4", "f32"),
    )
    args = parser.parse_args()
    if args.rounds < 1 or not 4096 <= args.block_bytes <= 16 * 1024 * 1024:
        parser.error(
            "positive rounds and a block size between 4 KiB and 16 MiB required"
        )
    root = Path(__file__).resolve().parents[2]
    source = args.input.resolve(strict=True)
    output = args.output.resolve()
    if not output.is_relative_to(Path("/private/tmp")) and not output.is_relative_to(
        root / ".local"
    ):
        parser.error("private benchmark output must be under /private/tmp or .local")
    output.mkdir(parents=True, exist_ok=False)
    weights = root / "qt/aqt/rwkv_inference/sm40v2/weights.f32.bin"
    sources = [
        *root.glob("rslib/src/rwkv/*.rs"),
        Path(__file__),
        root / "justfile",
        root / "Cargo.toml",
        root / "Cargo.lock",
        root / "rslib/Cargo.toml",
    ]
    with tarfile.open(output / "sources.tar.gz", "w:gz") as archive:
        for path in sources:
            archive.add(path, arcname=str(path.relative_to(root)))
    metadata = {
        "input_sha256": sha256(source),
        "weights_sha256": sha256(weights),
        "git_commit": subprocess.check_output(
            ["rtk", "proxy", "git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "working_tree_status": subprocess.check_output(
            ["rtk", "proxy", "git", "status", "--short"], cwd=root, text=True
        ).strip(),
        "source_sha256": {str(p.relative_to(root)): sha256(p) for p in sources},
        "source_archive_sha256": sha256(output / "sources.tar.gz"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "precision": args.precision,
        "model": "sm40",
        "weights_precision": "f32",
        "block_bytes": args.block_bytes,
        "rounds": args.rounds,
        "cache_policy": "Fresh process per operation; filesystem cache is warm; no cache eviction.",
        "memory_policy": "Whole child-process RSS sampled every 2 ms with boundary handshakes; parent/build excluded.",
        "timing_policy": "Target save/restore only, including file flush/sync on save; input preparation/model loading/hash verification excluded.",
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2))
    print("Building native checkpoint benchmark...", flush=True)
    executable = build(root, output)
    metadata["executable_sha256"] = sha256(executable)
    baseline = output / "source.sqlite3"
    request: dict[str, Any] = {
        "source": str(baseline),
        "weights": str(weights),
        "precision": args.precision,
        "block_bytes": args.block_bytes,
    }
    prepared = run_case(
        executable,
        root,
        output,
        "prepare",
        {
            **request,
            "action": "prepare",
            "codec": "sqlite",
            "input": str(source),
            "checkpoint": str(baseline),
        },
    )
    expected = {k: prepared["summary"][k] for k in ("state_blake3", "runtime_blake3")}
    reference = output / "reference.blocks"
    reference_run = run_case(
        executable,
        root,
        output,
        "reference",
        {
            **request,
            "action": "save",
            "codec": "raw",
            "checkpoint": str(reference),
        },
    )
    if any(reference_run["summary"][k] != value for k, value in expected.items()):
        raise ValueError("Raw reference differs from the source checkpoint")
    runs = []
    for round_index in range(args.rounds):
        order = (
            CODECS[round_index % len(CODECS) :] + CODECS[: round_index % len(CODECS)]
        )
        if round_index % 2:
            order = tuple(reversed(order))
        for codec in order:
            checkpoint = output / f"round-{round_index}-{codec}.checkpoint"
            for action in ("save", "restore"):
                run = run_case(
                    executable,
                    root,
                    output,
                    f"{round_index}-{codec}-{action}",
                    {
                        **request,
                        "action": action,
                        "codec": codec,
                        "checkpoint": str(checkpoint),
                    },
                )
                if any(run["summary"][k] != value for k, value in expected.items()):
                    raise ValueError(f"Restored bytes changed: {codec}, {action}")
                runs.append(run)
            if codec != "sqlite":
                checked = run_case(
                    executable,
                    root,
                    output,
                    f"{round_index}-{codec}-verify",
                    {
                        **request,
                        "action": "verify",
                        "codec": codec,
                        "checkpoint": str(checkpoint),
                        "reference": str(reference),
                    },
                )
                if not checked["summary"]["byte_identical"]:
                    raise ValueError(f"Decoded checkpoint differs: {codec}")
            print(f"Round {round_index + 1}/{args.rounds}: {codec} passed", flush=True)
    if any(
        sha256(root / name) != digest
        for name, digest in metadata["source_sha256"].items()
    ):
        raise ValueError("Benchmark sources changed during the run")
    metadata["all_byte_checks_passed"] = True
    result = {
        "metadata": metadata,
        "prepared": prepared["summary"],
        "hashes": expected,
        "summary": aggregate(runs),
        "runs": runs,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2))
    for codec, row in result["summary"].items():
        print(
            f"{codec}: {row['file_bytes'] / 1e6:.3f} MB, save {row['save']['wall_seconds'] * 1000:.1f} ms, restore {row['restore']['wall_seconds'] * 1000:.1f} ms",
            flush=True,
        )
    print(f"Results: {output / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()
