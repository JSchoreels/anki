# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Codec vectors for the low-rank card / note state (precision bytes 0x28, 0x26).

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

With `q6` as the third argument: the same recurrences with int6 factors
(`q6`, codes in [-31, 31]; the smaller shift vectors and factors experiment
(SM152), branch sm152 or later), with their own seed, plus `quant_vectors`:
synthetic vectors (random, exact rounding ties, zero, float16 underflow and
overflow) with the scorer's int6 rounding (`quant_vectors(x, -1, 31)`), its float16
scale, codes and bit-packed bytes (little-endian two's complement, as the
training repository's byte accounting packs them), which
`rwkv_state_lowrank_int6_rounding_matches_the_scorer` must reproduce bit for
bit.

Run from a checkout of the training repository that has the low-rank codec
(branch sm148 or later; sm152 or later for `q6`), with its environment:

    python qt/tools/rwkv_lowrank_state_codec_vectors.py <training repo> \
        rslib/src/rwkv/testdata/lowrank-state-codec.json.gz
    python qt/tools/rwkv_lowrank_state_codec_vectors.py <training repo> \
        rslib/src/rwkv/testdata/lowrank-state-codec-int6.json.gz q6
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
    int6 = sys.argv[3:] == ["q6"]
    sys.path.insert(0, str(repo))
    import torch  # type: ignore[import-not-found]
    from scripts import state_lowrank as sl  # type: ignore[import-not-found]

    torch.set_num_threads(1)
    g = torch.Generator().manual_seed(20261124 if int6 else 20261123)

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
        elif codec.quant == "q6":
            lf = sl.quant_vectors(lf, -2, 31)
            rf = sl.quant_vectors(rf, -1, 31)
        stored, left_new = codec.encode(nxt, left_prev, v, n, None)
        assert torch.equal(stored, lf @ rf), "the copy must equal LowRankCodec.encode"
        assert torch.equal(left_new, lf)
        return stored, lf, rf, r_eff

    cases = []
    for K in (16, 32):
        variants = (
            (("2.16.4", "q6"), ("2", "q6"))
            if int6
            else (("2.16.4", "q8"), ("2", "q8"), ("2.16.4", "f"))
        )
        for spec, quant in variants:
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
                w = torch.exp(
                    -torch.exp(torch.rand(R, HEADS, K, generator=g) * 3.5 - 3.0)
                )
                kd = torch.randn(R, HEADS, K, generator=g)
                kd = kd / kd.norm(dim=-1, keepdim=True)
                a = torch.rand(R, HEADS, K, generator=g)
                k = torch.randn(R, HEADS, K, generator=g) / K**0.5
                v = (
                    torch.randn(R, HEADS, K, generator=g)
                    / K**0.5
                    * scale[idx][:, None, None]
                )
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
                stored, lf, rf, r_eff = encode_with_right(
                    codec, nxt, left[idx], v, count[idx]
                )
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
    payload = {"cases": cases}
    if int6:
        payload["quant_vectors"] = int6_quant_vectors(torch, sl, g, b64)
    blob = json.dumps(payload, separators=(",", ":")).encode()
    out.write_bytes(gzip.compress(blob, mtime=0))
    print(f"wrote {out}: {out.stat().st_size:,} bytes")


def pack(codes, bits):
    """Little-endian bit packing of signed codes (two's complement in `bits`
    bits), as the training repository's byte accounting."""
    out = bytearray((len(codes) * bits + 7) // 8)
    acc = nacc = pos = 0
    for code in codes:
        acc |= (int(code) & ((1 << bits) - 1)) << nacc
        nacc += bits
        while nacc >= 8:
            out[pos] = acc & 255
            acc >>= 8
            nacc -= 8
            pos += 1
    if nacc:
        out[pos] = acc & 255
    return bytes(out)


def int6_quant_vectors(torch, sl, g, b64):
    """Synthetic vectors and the scorer's int6 rounding of each."""
    from scripts.small_model_wkv import (  # type: ignore[import-not-found]
        round_scale_f16,
    )

    vectors = []
    for K in (16, 32):
        rows = [torch.randn(K, generator=g) * s for s in (1.0, 0.03, 7.0, 1e-3, 250.0)]
        rows += [torch.rand(K, generator=g) - 0.5 for _ in range(20)]
        # exact rounding ties: k + 1/2 code steps of an exact float16 scale
        step = 2.0**-6
        tie = torch.tensor([31.0] + [(k % 31) + 0.5 for k in range(1, K)]) * step
        tie[1::2] *= -1
        rows += [tie, -tie]
        rows.append(torch.zeros(K))
        rows.append(torch.full((K,), 1e-12))  # the scale underflows float16: 1.0
        big = torch.randn(K, generator=g) * 1e7
        big[3] = 3e9  # abs-max / 31 above 65,504: the scale clamps, codes clamp
        rows.append(big)
        for x in rows:
            x = x.to(torch.float32)
            q = sl.quant_vectors(x.unsqueeze(0), -1, 31)[0]
            scale = round_scale_f16(x.abs().amax().reshape(1) / 31.0)
            scale = torch.where(scale > 0, scale, torch.ones_like(scale))
            codes = torch.clamp(torch.round(x / scale), -31, 31).to(torch.int64)
            assert torch.equal(codes.to(torch.float32) * scale, q)
            vectors.append(
                {
                    "head_size": K,
                    "values": b64(x),
                    "rounded": b64(q),
                    "scale_bits": int(scale.half().view(torch.int16).item()) & 0xFFFF,
                    "codes": codes.tolist(),
                    "packed": base64.b64encode(pack(codes.tolist(), 6)).decode(),
                }
            )
    return vectors


if __name__ == "__main__":
    main()
