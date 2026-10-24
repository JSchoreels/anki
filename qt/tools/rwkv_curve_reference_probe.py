# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Replay exported native inputs with the unmodified SM40 NumPy reference."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import sys
import time
from pathlib import Path

import numpy as np  # type: ignore[import-not-found]

NAMES = ("card_id", "deck_id", "note_id", "preset_id", "user_id")
SWEEP = (1.0, 3.0, 600.0, 86400.0, 31536000.0, 3153600000.0)
TARGETS = (0.75, 0.6, 0.5)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pava(values):
    blocks: list[tuple[float, int]] = []
    for value in values:
        blocks.append((float(value), 1))
        while (
            len(blocks) > 1
            and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]
        ):
            total, count = blocks.pop()
            previous, previous_count = blocks.pop()
            blocks.append((previous + total, previous_count + count))
    return [total / count for total, count in blocks for _ in range(count)]


def crossing(model, heads, target, ordered):
    def at(day):
        values = [model.p_curve(h, float(np.float32(day * 86400))) for h in heads]
        return pava(values) if ordered else values

    initial = at(0.0)
    maximum = at(36500.0)
    result = []
    for grade in range(4):
        if initial[grade] <= target:
            result.append(1 / 1440)
            continue
        if maximum[grade] > target:
            result.append(36500.0)
            continue
        lo, hi = 0.0, 36500.0
        for _ in range(48):
            if hi - lo <= max(1 / 86400, hi * 2e-7):
                break
            mid = (lo + hi) / 2
            if at(mid)[grade] > target:
                lo = mid
            else:
                hi = mid
        result.append((lo + hi) / 2)
    return result


def pack(model, heads):
    w = np.exp(heads["w_logits"] - heads["w_logits"].max())
    w /= w.sum()
    s = np.exp(np.clip(heads["s_raw"], -25, 25))
    d = np.exp(np.clip(heads["d_raw"], -25, 25))
    instant, ratings = model.instant(heads)
    return {
        "weights": w.tolist(),
        "scales_raw": heads["s_raw"].tolist(),
        "decays_raw": heads["d_raw"].tolist(),
        "scales_days": (s / 86400).tolist(),
        "decays": d.tolist(),
        "instant": instant,
        "ratings": ratings.tolist(),
        "sweep": [model.p_curve(heads, t) for t in SWEEP],
    }


def snapshot(store, ids):
    return [copy.deepcopy(store[i].get(ids[name])) for i, name in enumerate(NAMES)]


def restore(store, ids, saved):
    for i, name in enumerate(NAMES):
        if saved[i] is None:
            store[i].pop(ids[name], None)
        else:
            store[i][ids[name]] = copy.deepcopy(saved[i])


def evaluate(model, store, ids, features):
    heads = [model.step(f, ids, True, store) for f in features]
    return heads, {
        "heads": [pack(model, h) for h in heads],
        "crossings": {
            str(target): {
                "raw": crossing(model, heads[1:], target, False),
                "pava": crossing(model, heads[1:], target, True),
            }
            for target in TARGETS
        },
    }


def run(args):
    sys.path.insert(0, str(args.package / "reference"))
    reference = importlib.import_module("small_model_recurrent")
    weights, spec = importlib.import_module("weights_io").load_package(args.package)
    model = reference.RecurrentModel(weights, spec)
    store = model.new_store()
    trace = json.loads((args.trace / "trace.json").read_text())
    features = np.memmap(args.trace / "history.f32", dtype="<f4", mode="r").reshape(
        -1, 92
    )
    ids_array = np.memmap(args.trace / "ids.i64", dtype="<i8", mode="r").reshape(-1, 5)
    assert len(features) == len(ids_array) == trace["history_rows"]
    report = {
        "history_rows": len(features),
        "reference_files_sha256": {
            p.name: sha(p) for p in (args.package / "reference").glob("*.py")
        },
        "weights_sha256": sha(args.package / "weights.f32.bin"),
        "trace_sha256": sha(args.trace / "trace.json"),
        "numpy": np.__version__,
        "max_probability_error": 0.0,
        "max_parameter_error": 0.0,
        "cases": [],
    }
    cursor = 0
    start = time.monotonic()
    for case_index, case in enumerate(trace["traces"]):
        while cursor < case["history_index"]:
            ids = dict(zip(NAMES, map(int, ids_array[cursor])))
            model.step(features[cursor], ids, False, store)
            cursor += 1
            if cursor % 10000 == 0:
                print(
                    f"Reference replay {cursor}/{len(features)} rows; {time.monotonic() - start:.1f}s",
                    flush=True,
                )
        ids = dict(zip(NAMES, case["ids"]))
        before = snapshot(store, ids)
        outputs = []
        for branch in case["branches"]:
            restore(store, ids, before)
            samples = []
            after_original = None
            before_tenth = None
            for step, sample in enumerate(branch["steps"]):
                if step == 1:
                    after_original = snapshot(store, ids)
                if step == 10:
                    before_tenth = snapshot(store, ids)
                _, result = evaluate(model, store, ids, sample["features"])
                for native, ref in zip(sample["heads"], result["heads"], strict=True):
                    for key in ("instant", "ratings", "sweep"):
                        error = float(
                            np.max(
                                np.abs(np.asarray(native[key]) - np.asarray(ref[key]))
                            )
                        )
                        report["max_probability_error"] = max(
                            report["max_probability_error"], error
                        )
                        assert error < 1e-5, (case_index, step, key, error)
                    for key in ("weights", "scales_raw", "decays_raw"):
                        error = float(
                            np.max(
                                np.abs(np.asarray(native[key]) - np.asarray(ref[key]))
                            )
                        )
                        report["max_parameter_error"] = max(
                            report["max_parameter_error"], error
                        )
                result["step"] = step
                result["native_crossings"] = sample["crossings"]
                samples.append(result)
                model.step(sample["actual_features"], ids, False, store)
            # Separate changing input features from changing recurrent state.
            ablations = {}
            for label, state, feature_index in (
                ("original", before, 0),
                ("features_after_original_only", before, 1),
                ("state_after_original_only", after_original, 0),
                ("first_extra", after_original, 1),
                ("features_after_repeats_only", after_original, 10),
                ("state_after_repeats_only", before_tenth, 1),
                ("tenth_extra", before_tenth, 10),
            ):
                restore(store, ids, state)
                _, ablations[label] = evaluate(
                    model, store, ids, branch["steps"][feature_index]["features"]
                )
            for label, updated in (
                ("only_card_note_updated", (0, 2)),
                ("only_shared_updated", (1, 3, 4)),
            ):
                mixed = [
                    before_tenth[i] if i in updated else after_original[i]
                    for i in range(5)
                ]
                restore(store, ids, mixed)
                _, ablations[label] = evaluate(
                    model, store, ids, branch["steps"][10]["features"]
                )
            restore(store, ids, before_tenth)
            f = np.array(branch["steps"][10]["features"][3], dtype=np.float32)
            type_heads = []
            for value in (-2.0, -1.0, 0.0, 1.0, 2.0):
                f[22] = value
                type_heads.append(model.step(f, ids, True, store))
            assert all(
                np.array_equal(type_heads[0][k], h[k]) for h in type_heads for k in h
            )
            outputs.append(
                {
                    "gap_seconds": branch["gap_seconds"],
                    "steps": samples,
                    "ablations": ablations,
                    "state_feature_mask_bit_identical": True,
                }
            )
        restore(store, ids, before)
        report["cases"].append(
            {
                "case": case_index,
                "history_index": case["history_index"],
                "branches": outputs,
            }
        )
        args.output.write_text(json.dumps(report, allow_nan=False))
        print(
            f"Checked reference case {case_index + 1}/{len(trace['traces'])}; max probability error={report['max_probability_error']:.3g}",
            flush=True,
        )
    report["elapsed_seconds"] = time.monotonic() - start
    args.output.write_text(json.dumps(report, allow_nan=False))
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
