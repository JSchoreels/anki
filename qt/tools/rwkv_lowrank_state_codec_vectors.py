# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Codec vectors for the low-rank card / note state (precision byte 0x2a).

The training repository's scorer stores the card and note WKV matrix of every
head as a rank-r product L R after each write (`scripts/state_lowrank.py`,
`LowRankCodec.encode`, the low-rank int8 state experiment). This script runs
that codec on SYNTHETIC recurrences (seeded random decays, keys and values; no
review data) and writes, per write, the recurrence inputs and the codec's
factors. `rwkv_state_lowrank_codec_matches_the_scorer` rebuilds each float
update in Rust from the scorer's previous stored state and must reproduce the
scorer's factors within the stated tolerance; a second test runs the Rust
chain on its own and compares the trajectories.

Cases: head size 16 and 32; schedules `2.16.4` (rank 2 below 16 answers, rank
4 from 16, the default) and `2` (rank 2 everywhere), int8 factors (`q8`) and
float32 factors (`f`, the solve without the factor rounding).

Run from a checkout of the training repository that has the low-rank codec
(branch sm148 or later), with its environment:

    python qt/tools/rwkv_lowrank_state_codec_vectors.py <training repo> \
        rslib/src/rwkv/testdata/lowrank-state-codec.json.gz
"""

import base64
import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

ENTITIES = 3
HEADS = 2
STEPS = 24


def main() -> None:
    repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(repo))
    import torch
    from scripts import state_lowrank as sl

    torch.set_num_threads(1)
    g = torch.Generator().manual_seed(20261123)

    def b64(x: torch.Tensor) -> str:
        return base64.b64encode(
            x.detach().contiguous().to(torch.float32).numpy().astype("<f4").tobytes()
        ).decode()

    def encode_with_right(codec, nxt, left_prev, v, n):
        """LowRankCodec.encode's rank path, also returning the right factors."""
        kind = codec.kind_of(n)
        r_eff = torch.minimum(n, kind.clamp(min=0))
        P = torch.cat([left_prev, v.unsqueeze(-1)], -1)
        lf, rf = sl.lowrank_factors(nxt, P, codec.rmax)
        comp = (torch.arange(codec.rmax)[None, :] < r_eff[:, None]).to(lf.dtype)
        lf = lf * comp[:, None, None, :]
        rf = rf * comp[:, None, :, None]
        if codec.quant == "q8":
            lf = sl.quant_vectors(lf, -2)
            rf = sl.quant_vectors(rf, -1)
        stored, left_new = codec.encode(nxt, left_prev, v, n, None)
        assert torch.equal(stored, lf @ rf), "the copy must equal LowRankCodec.encode"
        assert torch.equal(left_new, lf)
        return stored, lf, rf, r_eff

    cases = []
    for K in (16, 32):
        for spec, quant in (("2.16.4", "q8"), ("2", "q8"), ("2.16.4", "f")):
            codec = sl.LowRankCodec(spec, quant)
            rmax = codec.rmax
            state = torch.zeros(ENTITIES, HEADS, K, K)
            left = torch.zeros(ENTITIES, HEADS, K, rmax)
            count = torch.zeros(ENTITIES, dtype=torch.int64)
            # per-entity value scale: ordinary, small, large
            scale = torch.tensor([1.0, 0.05, 20.0])
            steps = []
            for t in range(STEPS):
                rows = [e for e in range(ENTITIES) if t == 0 or (t + e) % 7 != 6]
                idx = torch.tensor(rows)
                R = len(rows)
                w = torch.exp(-torch.exp(torch.rand(R, HEADS, K, generator=g) * 3.5 - 3.0))
                kd = torch.randn(R, HEADS, K, generator=g)
                kd = kd / kd.norm(dim=-1, keepdim=True)
                a = torch.rand(R, HEADS, K, generator=g)
                k = torch.randn(R, HEADS, K, generator=g) / K**0.5
                v = torch.randn(R, HEADS, K, generator=g) / K**0.5 * scale[idx][:, None, None]
                if t == 7:
                    v[0] = 0.0  # a zero value: its direction is dropped from the basis
                S = state[idx]
                Skd = (S @ kd.unsqueeze(-1)).squeeze(-1)
                nxt = (
                    S * w.unsqueeze(-2)
                    - Skd.unsqueeze(-1) * (a * kd).unsqueeze(-2)
                    + v.unsqueeze(-1) * k.unsqueeze(-2)
                )
                count[idx] += 1
                stored, lf, rf, r_eff = encode_with_right(codec, nxt, left[idx], v, count[idx])
                state[idx] = stored
                left[idx] = lf
                entries = []
                for j, e in enumerate(rows):
                    c = int(r_eff[j])
                    entries.append(
                        {
                            "entity": e,
                            "n": int(count[e]),
                            "rank": c,
                            # [5, H, K]: w, kd, a, k, v
                            "inputs": b64(torch.stack([w[j], kd[j], a[j], k[j], v[j]])),
                            # [H, c, K]: left vectors (columns of L), right vectors (rows of R)
                            "left": b64(lf[j, :, :, :c].transpose(-1, -2)),
                            "right": b64(rf[j, :, :c, :]),
                        }
                    )
                steps.append(entries)
            assert torch.isfinite(state).all()
            cases.append(
                {
                    "head_size": K,
                    "heads": HEADS,
                    "entities": ENTITIES,
                    "spec": spec,
                    "quant": quant,
                    "steps": steps,
                }
            )
            print(f"K={K} {spec} {quant}: {STEPS} steps, final counts {count.tolist()}")
    blob = json.dumps({"cases": cases}, separators=(",", ":")).encode()
    out.write_bytes(gzip.compress(blob, mtime=0))
    print(f"wrote {out}: {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
