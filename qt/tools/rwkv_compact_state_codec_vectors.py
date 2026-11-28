# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Codec vectors for the compact card / note state (precision bytes 0x18 / 0x14).

The training repository's scorer rounds the stored RWKV state with
`scripts.state_vq.int_row_quant(bits | F16_SCALE)` (small models) and the
`_quantize_rows` / `_round_shift` helpers that `scripts.compress_quant`
patches into the big model. This script feeds both the same synthetic float32
states, checks that they agree bit for bit, and writes the inputs with their
reconstructions. `rwkv_state_precision_compact_codec_matches_the_scorer`
re-quantizes the inputs in Rust and must reproduce every reconstruction bit
for bit (row = the last axis of the scorer's [B, H, K, K] state, which is the
fork's contiguous row of HEAD_SIZE values).

Synthetic values only (seeded random states plus hand-made edge rows); no
review data. Run from a checkout of the training repository that has the
SM47 / SM131 state formats (branch sm131 or later), with its environment:

    python qt/tools/rwkv_compact_state_codec_vectors.py <training repo> \
        rslib/src/rwkv/testdata/compact-state-codec.json.gz
"""

import base64
import gzip
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")


def main() -> None:
    repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(repo))
    import numpy as np  # type: ignore[import-not-found]
    import torch  # type: ignore[import-not-found]
    from scripts import compress_quant
    from scripts.small_model_wkv import F16_SCALE
    from scripts.state_vq import int_row_quant

    # The helpers the scorer patches into the big model.
    helpers: dict[str, object] = {"torch": torch}
    exec(compress_quant._PATCH_HELPERS.split("def reference_rwkv7_q")[0], helpers)
    quantize_rows = cast(
        Callable[[torch.Tensor, int], torch.Tensor], helpers["_quantize_rows"]
    )
    round_shift = cast(
        Callable[[torch.Tensor, int], torch.Tensor], helpers["_round_shift"]
    )

    rng = np.random.default_rng(20261121)

    def encode(values: np.ndarray) -> str:
        return base64.b64encode(
            np.ascontiguousarray(values, dtype="<f4").tobytes()
        ).decode()

    def edge_rows(width: int) -> np.ndarray:
        rows = []
        for maximum in [
            0.0,
            1e-9,
            1e-6,
            1e-5,
            1e-4,
            3e-3,
            0.5,
            63.5,
            1e4,
            65504.0 * 7,
            1e6,
            1e9,
            3e38,
        ]:
            row = rng.uniform(-1, 1, width).astype(np.float32) * np.float32(maximum)
            if maximum:
                row[rng.integers(width)] = np.float32(maximum) * (
                    1 if rng.random() < 0.5 else -1
                )
            rows.append(row)
        # Exact ties: scale 0.5 (int8: max 63.5) and 2.0 (int4: max 14) with
        # values at k + 0.5 times the scale.
        for scale, limit in [(0.5, 127), (2.0, 7)]:
            row = np.zeros(width, np.float32)
            row[0] = scale * limit
            row[1:9] = (
                np.array([0.5, 1.5, 2.5, -0.5, -1.5, -2.5, 3.5, -3.5], np.float32)
                * scale
            )
            rows.append(row)
        # Infinity saturates at the float16 cap.
        row = rng.normal(size=width).astype(np.float32)
        row[3] = np.inf
        rows.append(row)
        return np.stack(rows)

    cases = []
    for head_size, d_model in [(16, 64), (32, 128)]:
        heads = 4
        # A non-symmetric state: a transposed row/column layout would not
        # reproduce the reconstruction.
        state = rng.normal(size=(1, heads, head_size, head_size)).astype(np.float32)
        state *= np.float32(10.0) ** rng.uniform(
            -3, 3, size=(1, heads, head_size, 1)
        ).astype(np.float32)
        state[0, 0] += np.arange(head_size, dtype=np.float32)[None, :] * 0.37
        matrix = np.concatenate([state.reshape(-1, head_size), edge_rows(head_size)])
        expected = {}
        for name, bits in [("int8", 8 | F16_SCALE), ("int4", 4 | F16_SCALE)]:
            tensor = torch.from_numpy(matrix.copy())
            a = int_row_quant(bits)(tensor).numpy()
            b = quantize_rows(tensor, bits).numpy()
            # The same rounding from the [B, H, K, K] layout of the state.
            c = (
                int_row_quant(bits)(torch.from_numpy(state.copy()))
                .numpy()
                .reshape(-1, head_size)
            )
            assert a.tobytes() == b.tobytes(), name
            assert a[: len(c)].tobytes() == c.tobytes(), name
            assert np.isfinite(a).all(), name
            expected[name] = a
        shifts = np.concatenate(
            [
                rng.normal(size=(8, d_model)).astype(np.float32)
                * np.float32(10.0)
                ** rng.uniform(-3, 3, size=(8, 1)).astype(np.float32),
                edge_rows(d_model),
            ]
        )
        tensor = torch.from_numpy(shifts.copy())
        shift_expected = round_shift(tensor, 8 | F16_SCALE).numpy()
        assert (
            shift_expected.tobytes()
            == int_row_quant(8 | F16_SCALE)(tensor).numpy().tobytes()
        )
        cases.append(
            {
                "head_size": head_size,
                "d_model": d_model,
                "matrix": encode(matrix),
                "int8": encode(expected["int8"]),
                "int4": encode(expected["int4"]),
                "shift": encode(shifts),
                "shift_int8": encode(shift_expected),
            }
        )
    payload = {
        "note": "SYNTHETIC float32 states (seeded random and edge rows), no review data",
        "generator": "qt/tools/rwkv_compact_state_codec_vectors.py",
        "scorer": "scripts.state_vq.int_row_quant(bits | F16_SCALE) == compress_quant._quantize_rows / _round_shift",
        "cases": cases,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(out, "wb", mtime=0) as file:
        file.write(json.dumps(payload, indent=0).encode())
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
