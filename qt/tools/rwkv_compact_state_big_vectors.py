# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Model-level parity vectors for the compact card / note state on the big model.

Runs the training repository's scorer arithmetic for the fork (big) model,
`scripts.compress_quant.install_state_quant_model` + `set_state_streams`,
which stores the card and note state rounded after every answer exactly as
the fork does, on the show-time big model (SM91s2) with float32 weights. It
writes, per row, the inputs of the fork's vector format
(`rslib/src/rwkv/legacy_tests.rs`) and the outputs at three state formats:

- `fp32`: float32 state;
- `compact_int8`: `compact_cards_int8` (`w8+s8hx8` cards, `hx8` notes);
- `compact_int4`: `compact_cards_int4` (`w8+s4c8nhx8`).

Only SYNTHETIC users: the training repository's
`scripts/synthetic_testvector_users.py` writes users 900001 and 900002 (fixed
seed, no dataset rows) into a scratch folder. Run with the training
repository worktree as the working directory (it needs `scripts/sm31_inputs.py`
and the SM131 state formats), on the CPU:

    python <anki-rwkv main>/scripts/synthetic_testvector_users.py <scratch>
    RWKV_DELETED_CARDS=neighbour CUDA_VISIBLE_DEVICES= RWKV_NO_JIT=1 \\
        python -X utf8 <fork>/qt/tools/rwkv_compact_state_big_vectors.py \\
        <fork>/qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.pth <scratch> \\
        <fork>/rslib/src/rwkv/testdata/compact-state-big-vectors.json.gz 500 900001 900002
"""

import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("RWKV_NO_JIT", "1")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
sys.path.insert(0, os.getcwd())

from scripts import compress_quant as cq  # type: ignore[import-not-found]  # noqa: E402

# The scorer's patched model rounds every row's shift input, including the
# first row of a stream, whose shift is its own input: the fork reads no
# stored vector there. This copy rounds only rows that read an earlier row
# (rows that read a stored vector are unchanged). It is written to a scratch
# file, so the training repository's generated module is not touched.
_STORED_ONLY = """

def _round_shift_stored(x: torch.Tensor, select: torch.Tensor, bits: int) -> torch.Tensor:
    rounded = _round_shift(x, bits)
    own = select == torch.arange(select.shape[1], device=select.device).unsqueeze(0)
    return torch.where(own.unsqueeze(-1), x, rounded)
"""


def install_state_quant_model():
    import importlib.util
    import tempfile

    import rwkv.model  # type: ignore[import-not-found]  # noqa: F401  (parent package)

    name = "rwkv.model.rwkv_model"
    assert name not in sys.modules, "rwkv.model.rwkv_model is already imported"
    path = cq.ROOT / "rwkv" / "model" / "rwkv_model.py"
    source = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    for old, new in cq._PATCHES:
        assert source.count(old) == 1, old[:60]
        if "def _quantize_rows" in new:
            new += _STORED_ONLY
        new = new.replace(
            "_round_shift(x_shift_BTC, self.shift_bits)",
            "_round_shift_stored(x_shift_BTC, time_shift_select_BT, self.shift_bits)",
        )
        source = source.replace(old, new)
    assert source.count("_round_shift_stored(") == 3, "patch anchors changed"
    source += "\nSTATE_QUANT_PATCHED = True\n"
    generated = Path(tempfile.mkdtemp()) / "rwkv_model_state_quant_stored_shifts.py"
    generated.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, generated)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


from scripts import compress_wkv_cuda  # noqa: E402

compress_wkv_cuda.define()
install_state_quant_model()

import numpy as np  # type: ignore[import-not-found]  # noqa: E402
import torch  # type: ignore[import-not-found]  # noqa: E402
from rwkv import data_processing as dp  # noqa: E402
from rwkv.data_processing import (  # type: ignore[import-not-found]  # noqa: E402
    add_segment_features,
    get_rwkv_data,
)
from rwkv.prepare_batch import prepare  # type: ignore[import-not-found]  # noqa: E402
from scripts import compress_common as cc  # noqa: E402
from scripts import sm31_inputs  # noqa: E402
from scripts.small_model_wkv import (  # type: ignore[import-not-found]  # noqa: E402
    F16_SCALE,
)

COMPACT8 = 8 | F16_SCALE
COMPACT4 = 4 | F16_SCALE
# (label, card (WKV bits, shift bits), note (WKV bits, shift bits))
FORMATS = [
    ("fp32", (0, 0), (0, 0)),
    ("compact_int8", (COMPACT8, COMPACT8), (COMPACT8, COMPACT8)),
    ("compact_int4", (COMPACT4, COMPACT8), (COMPACT8, COMPACT8)),
]
SWEEP = [60.0, 3600.0, 86400.0, 7 * 86400.0, 30 * 86400.0, 365 * 86400.0]


def main() -> None:
    weights, data, out_path, limit = (
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Path(sys.argv[3]),
        int(sys.argv[4]),
    )
    users = [int(user) for user in sys.argv[5:]]
    assert all(user >= 900_000 for user in users), "synthetic users only"
    assert (data / "SYNTHETIC_README.txt").exists(), "synthetic data folder only"
    assert os.environ.get("RWKV_DELETED_CARDS") == "neighbour"
    torch.set_num_threads(4)
    model = cq.load_model(
        state=torch.load(weights, map_location="cpu", weights_only=True)
    )

    def outputs(batch, rows):
        T = len(rows)
        with torch.inference_mode():
            stats = model.get_loss(batch)
            p_imm = stats.p_imm.float().numpy().reshape(-1)
            p_rating = stats.p_imm_all.float().numpy().reshape(T, 4)
            elapsed = batch.labels[0, :, 0].float()
            _, out_w, _, _ = model.forward_batch(
                batch.start,
                batch.sub_gather,
                batch.sub_gather_lens,
                batch.time_shift_selects,
                batch.skips,
                batch.num_data,
            )
            curves = [
                model.forgetting_curve(out_w, t.unsqueeze(-1))
                .float()
                .numpy()
                .reshape(-1)
                for t in [elapsed] + [torch.full_like(elapsed, s) for s in SWEEP]
            ]
        return [
            [
                float(p_imm[i]),
                *map(float, p_rating[i]),
                float(curves[0][i]),
                *(float(c[i]) for c in curves[1:]),
            ]
            for i in range(T)
        ]

    out = []
    for user in users:
        frame = sm31_inputs.apply_frame(get_rwkv_data(data, user), "e2s-query")
        with sm31_inputs.query_patch("e2s-query"):
            sample = cc.make_samples(frame, user, len(frame))
            assert len(sample) == 1
            batch = prepare(sample, seed=2025 + user)
            rows = dp.add_queries(
                add_segment_features(frame.copy()).reset_index(drop=True), []
            )
        features = batch.start.cpu().numpy()
        labels = batch.labels.cpu().numpy()[0]
        T = len(rows)
        assert features.shape == (T, 92), features.shape
        values = {}
        for label, card, note in FORMATS:
            cq.set_state_streams(model, card, note)
            values[label] = outputs(batch, rows)
        cq.set_state_streams(model, (0, 0), (0, 0))
        count = T if limit == 0 else min(limit, T)
        out_rows = []
        for i in range(count):
            out_rows.append(
                {
                    "features": features[i].astype(np.float32).tolist(),
                    "ids": [
                        int(rows[s].iloc[i])
                        for s in (
                            "card_id",
                            "deck_id",
                            "note_id",
                            "preset_id",
                            "user_id",
                        )
                    ],
                    "skip": bool(rows["skip"].iloc[i]),
                    "elapsed": float(labels[i, 0]),
                    "interval": [
                        float(rows["elapsed_seconds"].iloc[i]),
                        float(rows["elapsed_days"].iloc[i]),
                    ],
                    **{label: values[label][i] for label, _, _ in FORMATS},
                }
            )
        difference = max(
            abs(a - b)
            for row in out_rows
            for a, b in zip(row["fp32"], row["compact_int8"])
        )
        print(
            f"user {user}: {T} rows, kept {count}; max |compact_int8 - fp32| {difference:.3g}",
            flush=True,
        )
        out.append({"name": f"synthetic_user_{user}", "sweep": SWEEP, "rows": out_rows})
    blob = json.dumps(out, separators=(",", ":")).encode()
    out_path.write_bytes(
        gzip.compress(blob, mtime=0) if out_path.suffix == ".gz" else blob
    )
    print(f"wrote {out_path}: {sum(len(u['rows']) for u in out)} rows", flush=True)


if __name__ == "__main__":
    main()
