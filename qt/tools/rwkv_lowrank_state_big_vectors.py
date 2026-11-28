# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Model-level parity vectors for the low-rank card / note state on the big model.

The low-rank twin of `rwkv_compact_state_big_vectors.py`: the same patched
fork model of the training repository (card and note state stored after every
answer, the first row of a stream keeps its own shift input, as the fork does)
on the show-time big model (SM91s2) with float32 weights, with the low-rank
codec on the fork-model path (`scripts/state_lowrank.py`: `install_fork` +
`set_fork`). It writes, per row, the inputs of the fork's vector format
(`rslib/src/rwkv/legacy_tests.rs`), the row's label and the outputs at three
state formats:

- `fp32`: float32 state;
- `t24`: `compact_lowrank_int8` (`w8+lr2.16.4q8x8`: rank 2 below 16 answers,
  rank 4 from 16, int8 factors, int8 shift vectors);
- `t24_q6`: `compact_lowrank_int6` (`w8+lr2.16.4q6x8`: the same with int6
  factors; Big's and Small's profile default since the 6-bit factors (SM162)).

Only SYNTHETIC users: the training repository's
`scripts/synthetic_testvector_users.py` writes users 900001 and 900002 (fixed
seed, no dataset rows) into a scratch folder. Run with a checkout of the
training repository that has the int6 factors (branch sm152 or later) as the
working directory, on the CPU:

    python <anki-rwkv main>/scripts/synthetic_testvector_users.py <scratch>
    RWKV_DELETED_CARDS=neighbour CUDA_VISIBLE_DEVICES= RWKV_NO_JIT=1 PYTORCH_JIT=0 \\
        python -X utf8 <fork>/qt/tools/rwkv_lowrank_state_big_vectors.py \\
        <fork>/qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.pth <scratch> \\
        <fork>/rslib/src/rwkv/testdata/lowrank-state-big-vectors.json.gz 300 900001 900002
"""

import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("RWKV_NO_JIT", "1")
os.environ["PYTORCH_JIT"] = "0"  # install_fork needs eager modules
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
sys.path.insert(0, os.getcwd())
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rwkv_compact_state_big_vectors import (  # type: ignore[import-not-found]  # noqa: E402
    COMPACT8,
    SWEEP,
    cq,
    install_state_quant_model,
)

del install_state_quant_model  # installed by the import above

import numpy as np  # type: ignore[import-not-found]  # noqa: E402
import torch  # type: ignore[import-not-found]  # noqa: E402
from rwkv import data_processing as dp  # type: ignore[import-not-found]  # noqa: E402
from rwkv.data_processing import (  # type: ignore[import-not-found]  # noqa: E402
    add_segment_features,
    get_rwkv_data,
)
from rwkv.prepare_batch import prepare  # type: ignore[import-not-found]  # noqa: E402
from scripts import (  # noqa: E402
    compress_common as cc,
)
from scripts import (  # noqa: E402
    sm31_inputs,
    state_lowrank,
)

# (label, low-rank codec label or None for the float32 state)
FORMATS = [("fp32", None), ("t24", "lr2.16.4q8x8"), ("t24_q6", "lr2.16.4q6x8")]


def codec(spec):
    c = state_lowrank.parse_sm148(spec)
    assert c is not None and c.quant in ("q8", "q6"), spec
    c.rows_bits = c.shift_bits = COMPACT8
    return c


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
        state={
            k: v.float()
            for k, v in torch.load(
                weights, map_location="cpu", weights_only=True
            ).items()
        }
    )
    for module in model.rwkv_modules:
        for block in module.blocks:
            block.time_mixer.use_reference_wkv = True
    state_lowrank.install_fork(model)

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
        for label, spec in FORMATS:
            if spec is None:
                state_lowrank.set_fork(None)
                cq.set_state_streams(model, (0, 0), (0, 0))
            else:
                state_lowrank.set_fork(codec(spec))
                cq.set_state_streams(model, (COMPACT8, COMPACT8), (COMPACT8, COMPACT8))
            values[label] = outputs(batch, rows)
        state_lowrank.set_fork(None)
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
                    # (label_y, has_label, is_query), for the LogLoss gate
                    "label": [
                        float(labels[i, 2]),
                        float(labels[i, 4]),
                        float(labels[i, 6]),
                    ],
                    **{label: values[label][i] for label, _ in FORMATS},
                }
            )
        for label, _ in FORMATS[1:]:
            difference = max(
                abs(a - b) for row in out_rows for a, b in zip(row["fp32"], row[label])
            )
            print(
                f"user {user}: {T} rows, kept {count}; max |{label} - fp32| {difference:.3g}",
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
