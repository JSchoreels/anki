# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Compare hybrid RWKV state with INT8 and frozen configured FSRS7 presets."""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import statistics
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anki.collection
from anki.cards import CardId
from aqt import rwkv_scheduler
from tools.rwkv_runtime_precision_bench import build, provenance, sha256
from tools.rwkv_um_plus import um_plus_pair

MODES = ("state_int8", "cards_int4_notes_int8", "fsrs7")


def frozen_presets(source: dict) -> dict:
    collection = Path(source["collection_copy"]).resolve(strict=True)
    if not collection.is_relative_to(Path("/private/tmp")):
        raise ValueError("Use an extracted backup under /private/tmp")
    col = anki.collection.Collection(str(collection))
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        card_ids = list({row[1] for row in source["rows"]})
        resolved = rwkv_scheduler._resolved_fsrs_preset_ids(reviewer, card_ids)
        presets = {}
        for row in source["rows"]:
            preset_id = rwkv_scheduler._stable_preset_id(resolved[row[1]])
            if preset_id != row[4]:
                raise ValueError("Historical preset routing differs from current routing")
            if str(preset_id) not in presets:
                preset = col.fsrs_preset_for_card(CardId(row[1]))
                if preset.fsrs_version != "seven":
                    raise ValueError(f"Preset {preset.name} is not FSRS7")
                presets[str(preset_id)] = {
                    "id": preset.id,
                    "name": preset.name,
                    "params": list(preset.params),
                    "ignore_revlogs_before_date": preset.ignore_revlogs_before_date,
                }
        return presets
    finally:
        col.close()


def comparison(rows: list, output: Path) -> dict:
    seen = set()
    indices = []
    actual = []
    for index, row in enumerate(rows):
        previous = row[1] in seen
        seen.add(row[1])
        if previous and row[9] >= 1 and row[11]:
            indices.append(index)
            actual.append(float(row[5] != 1))
    if not indices:
        raise ValueError("No matched interday reviews")
    probabilities = {}
    metrics = {}
    for mode in MODES:
        values = array.array("f")
        values.frombytes((output / f"{mode}.f32").read_bytes())
        if sys.byteorder != "little":
            values.byteswap()
        if len(values) != len(rows):
            raise ValueError(f"Incomplete predictions for {mode}")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in values):
            raise ValueError(f"Invalid predictions for {mode}")
        selected = [float(values[index]) for index in indices]
        probabilities[mode] = selected
        loss = 0.0
        for y, p in zip(actual, selected, strict=True):
            p = max(1e-6, min(1 - 1e-6, p))
            loss -= math.log(p) if y else math.log1p(-p)
        metrics[mode] = {"log_loss": loss / len(indices)}
    pairs = {}
    for i, first in enumerate(MODES):
        for second in MODES[i + 1 :]:
            pair = um_plus_pair(actual, probabilities[first], probabilities[second])
            if pair["count"] != len(indices):
                raise ValueError("UM+ dropped matched outcomes")
            pairs[f"{first} vs {second}"] = pair
    differences = sorted(
        abs(a - b)
        for a, b in zip(
            probabilities["cards_int4_notes_int8"], probabilities["state_int8"], strict=True
        )
    )
    return {
        "reviews": len(indices),
        "outcome_recall_rate": statistics.mean(actual),
        "metrics": metrics,
        "um_plus_pairs": pairs,
        "hybrid_vs_int8": {
            "mean_abs_probability_difference": statistics.mean(differences),
            "p99_abs_probability_difference": differences[(len(differences) - 1) * 99 // 100],
            "max_abs_probability_difference": differences[-1],
            "fraction_over_5pp": sum(d > 0.05 for d in differences) / len(differences),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source_path = args.input.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = json.loads(source_path.read_text())
    source["fsrs_presets"] = frozen_presets(source)
    prepared = output / "input.json"
    prepared.write_text(json.dumps(source))
    root = Path(__file__).resolve().parents[2]
    metadata: dict[str, Any] = {
        "source_input_sha256": sha256(source_path),
        "input_sha256": sha256(prepared),
        "model_sha256": sha256(root / "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"),
        "driver_sha256": sha256(Path(__file__)),
        "provenance": provenance(root, output),
        "fsrs_baseline": "Frozen configured FSRS7 parameters; matched canonical history; no fitting",
    }
    if source["model_sha256"] != metadata["model_sha256"]:
        raise ValueError("Prepared history used a different RWKV model")
    print(f"Building benchmark for {len(source['rows']):,} retained answers...", flush=True)
    executable = build(root, output)
    metadata["executable_sha256"] = sha256(executable)
    with (output / "replay.log").open("w") as log:
        subprocess.run(
            [str(executable), "rwkv::runtime_precision_bench::rwkv_hybrid_precision_metrics",
             "--ignored", "--exact", "--nocapture", "--test-threads=1"],
            cwd=root,
            env={**os.environ, "ANKI_RWKV_HYBRID_INPUT": str(prepared),
                 "ANKI_RWKV_HYBRID_OUTPUT": str(output)},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    result = {
        **metadata,
        "native": json.loads((output / "native.json").read_text()),
        "comparison": comparison(source["rows"], output),
        "prediction_sha256": {mode: sha256(output / f"{mode}.f32") for mode in MODES},
    }
    for name, expected in metadata["provenance"]["source_sha256"].items():
        if sha256(root / name) != expected:
            raise ValueError(f"Source changed during benchmark: {name}")
    if sha256(prepared) != metadata["input_sha256"]:
        raise ValueError("Prepared input changed during benchmark")
    if sha256(Path(__file__)) != metadata["driver_sha256"]:
        raise ValueError("Benchmark driver changed during measurement")
    if sha256(root / "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin") != metadata["model_sha256"]:
        raise ValueError("RWKV model changed during measurement")
    (output / "results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["comparison"]["metrics"], indent=2), flush=True)
    print(f"Saved comparison: {output / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
