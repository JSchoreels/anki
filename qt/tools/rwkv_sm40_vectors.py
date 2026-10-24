# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Convert published SM40 vectors; requires NumPy only for this preparation."""

import argparse
import gzip
import json
from pathlib import Path

import numpy as np  # type: ignore[import-not-found]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    users = []
    for path in sorted((args.package / "testvectors").glob("user_*.npz")):
        data = np.load(path, allow_pickle=False)
        count = (
            len(data["features"])
            if args.limit == 0
            else min(args.limit, len(data["features"]))
        )
        rows = []
        for index in range(count):
            rows.append(
                {
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
            )
        users.append(
            {"name": path.name, "sweep": data["sweep_seconds"].tolist(), "rows": rows}
        )
    data = json.dumps(users, separators=(",", ":")).encode()
    args.output.write_bytes(
        gzip.compress(data, mtime=0) if args.output.suffix == ".gz" else data
    )


if __name__ == "__main__":
    main()
