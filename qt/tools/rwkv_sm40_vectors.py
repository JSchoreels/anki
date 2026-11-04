# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Convert published SM40 vectors (and answer-time head vectors when present);
requires NumPy only for this preparation."""

import argparse
import gzip
import json
from pathlib import Path

import numpy as np  # type: ignore[import-not-found]


def answer_time_vectors(data: dict, count: int) -> dict:
    """Answer-time head vectors (package sm72e25-time) for the first `count` rows:
    the query rows' history features and head logits, and the revlog prefix the
    history features are computed from."""
    keep = data["time_row"] < count
    review_th = data["time_review_th"][keep]
    reviews = int(review_th.max()) if len(review_th) else 0
    return {
        "rows": data["time_row"][keep].tolist(),
        "review_th": review_th.tolist(),
        "explicit": data["time_explicit"][keep].tolist(),
        "fp32": data["fp32_time_logits"][keep].tolist(),
        "int8": data["int8_time_logits"][keep].tolist(),
        "revlog": [
            data[f"rev_{name}"][:reviews].tolist()
            for name in ("card_id", "rating", "duration", "elapsed_days")
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--intervals",
        action="store_true",
        help="add each row's interval inputs (raw elapsed seconds and days)",
    )
    parser.add_argument(
        "--time-only",
        action="store_true",
        help="keep only the answer-time vectors (the rows are the model's own vectors)",
    )
    args = parser.parse_args()
    users = []
    for path in sorted((args.package / "testvectors").glob("user_*.npz")):
        # Decompress each array once: NpzFile reads a member on every access.
        data = dict(np.load(path, allow_pickle=False))
        count = (
            len(data["features"])
            if args.limit == 0
            else min(args.limit, len(data["features"]))
        )
        rows = []
        for index in range(count):
            row = {
                "features": data["features"][index].tolist(),
                "ids": [
                    int(data[f"id_{stream}"][index])
                    for stream in (
                        "card_id",
                        "deck_id",
                        "note_id",
                        "preset_id",
                        "user_id",
                    )
                ],
                "skip": bool(data["skip"][index]),
                "elapsed": float(data["label_elapsed_seconds"][index]),
                "fp32": [
                    float(data["fp32_p_imm"][index]),
                    *data["fp32_p_rating"][index].tolist(),
                    float(data["fp32_p_curve"][index]),
                    *data["fp32_p_curve_sweep"][index].tolist(),
                ],
                "int8": [
                    float(data["int8_p_imm"][index]),
                    *data["int8_p_rating"][index].tolist(),
                    float(data["int8_p_curve"][index]),
                    *data["int8_p_curve_sweep"][index].tolist(),
                ],
            }
            if args.intervals:
                # The row's own interval inputs (seconds, days), as the
                # trainer built them (show-time query rows in package sm92).
                row["interval"] = [
                    float(data["raw_elapsed_seconds"][index]),
                    float(data["raw_elapsed_days"][index]),
                ]
            rows.append(row)
        user = {
            "name": path.name,
            "sweep": data["sweep_seconds"].tolist(),
            "rows": rows,
        }
        if "time_row" in data:
            user["time"] = answer_time_vectors(data, count)
        if args.time_only:
            user["rows"] = []
        users.append(user)
    output = json.dumps(users, separators=(",", ":")).encode()
    args.output.write_bytes(
        gzip.compress(output, mtime=0) if args.output.suffix == ".gz" else output
    )


if __name__ == "__main__":
    main()
