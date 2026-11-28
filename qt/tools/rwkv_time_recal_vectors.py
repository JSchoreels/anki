# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Parity vectors for the rolling recalibration of the timed answer-time
probabilities (SM175).

The rolling recalibration on the 732 set (SM125) validated a per-user, causal,
rolling Platt scaling of every "recalled / Good or Easy / Easy AND answered
within S s" probability (reading the back included); the Big models (SM128)
and the six-layer Tiny (SM130, checked in SM140) use the same code. This
script runs that reference code, `recal_user` of the training repository's
`scripts/sm125_recal.py` (branch sm125 or later), unchanged, on SYNTHETIC
users: seeded random grade probabilities, time-bin distributions, grades,
durations and days, no review data. Each user's raw event probabilities are
built with the reference's own `cdf_at` (P(set) x P(t <= S | grade), linear in
ln time inside a bin) and stored rounded to 4 decimals (float32, as the
reference's dump stores them). The vectors hold, per user, the inputs, the
reference's recalibrated and repaired arm (`cal`) and which rows were
recalibrated (`active`).

Users:
- `standard`: the shipped scheme (7-day blocks, latest 30,000 earlier reviews,
  1,000 earlier reviews needed, 20 positives and 20 negatives per event);
- `low_cap`: the same with a user cap of 20 s (S = 20 s and 30 s stay raw);
- `window`: a smaller window (700 rows, 250 needed) so that the
  latest-rows cut and the per-event 20-sample rule (rare Easy answers) are
  exercised on a small file;
- `short`: fewer than 1,000 earlier reviews: every row stays raw.

`rwkv_time_recal_matches_the_reference` replays every user in Rust and must
reproduce the `cal` arm within 1e-6; it also checks that the deployment fit
(the current block only, cold start) gives the reference's values for each
user's last row.

Run from a checkout of the training repository with its environment:

    python qt/tools/rwkv_time_recal_vectors.py <training repo> \
        rslib/src/rwkv/testdata/time-recal-vectors.json.gz
"""

import gzip
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

SEED = 175


def synthetic_user(np, r, cdf_at, rng, rows, days, easy_scale, cap):
    """Raw events from random grade / time distributions; labels drawn from a
    miscalibrated truth (grades sharpened, times shifted) so the fit moves."""
    alpha = np.array([1.0, 0.6, 4.0, 0.8 * easy_scale])
    pg = rng.dirichlet(alpha, size=rows)
    centre = rng.normal(5.5, 1.6, size=(rows, 4))
    bins = np.arange(16)[None, None, :]
    logits = -0.5 * ((bins - centre[:, :, None]) / 1.8) ** 2
    pt = np.exp(logits)
    pt /= pt.sum(-1, keepdims=True)
    raw = np.empty((rows, len(r.COLS)))
    cdfs = {s: cdf_at(pt, s) for s in r.S_GRID}
    for j, (sn, s) in enumerate(r.COLS):
        gs = list(r.SETS[sn])
        raw[:, j] = pg[:, gs].sum(1) if s == 0 else (pg[:, gs] * cdfs[s][:, gs]).sum(1)
    raw = np.round(raw, 4).astype(np.float32)
    # Truth: grades from a sharper softmax, times one bin faster for Good.
    z = np.log(pg) * 1.3 + np.array([0.0, -0.2, 0.25, 0.1])
    truth = np.exp(z - z.max(1, keepdims=True))
    truth /= truth.sum(1, keepdims=True)
    grade = np.array([rng.choice(4, p=p) for p in truth])
    shift = np.where(grade == 2, -1.0, 0.4)
    centre_g = centre[np.arange(rows), grade] + shift
    bin_index = np.clip(np.rint(rng.normal(centre_g, 1.8)), 0, 15).astype(int)
    edges = [500.0 * 120.0 ** (k / 14) for k in range(15)]
    lower = np.array([250.0] + edges)
    upper = np.array(edges + [60000.0])
    duration = np.exp(rng.uniform(np.log(lower[bin_index]), np.log(upper[bin_index])))
    duration = np.minimum(np.rint(duration), cap).astype(np.int64)
    duration = np.maximum(duration, 1)
    day = np.sort(rng.integers(0, days, size=rows)).astype(np.int64) + 400
    return raw, grade.astype(np.int64), duration, day


def main() -> None:
    repo, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    sys.path.insert(0, str(repo))
    import numpy as np  # type: ignore[import-not-found]
    from scripts import sm125_recal as r
    from scripts.sm117_head import cdf_at

    rng = np.random.default_rng(SEED)
    standard = {
        "roll_days": r.ROLL_DAYS,
        "roll_rows": r.ROLL_ROWS,
        "min_rows": r.N_MIN,
        "min_class": r.MIN_CLASS,
    }
    assert standard == {
        "roll_days": 7,
        "roll_rows": 30000,
        "min_rows": 1000,
        "min_class": 20,
    }, standard
    small_window = dict(standard, roll_rows=700, min_rows=250)
    users = [
        ("standard", 1900, 120, 1.0, 60000, standard),
        ("low_cap", 1500, 90, 1.0, 20000, standard),
        ("window", 2400, 200, 0.05, 60000, small_window),
        ("short", 600, 60, 1.0, 60000, standard),
    ]
    out_users = []
    for name, rows, days, easy_scale, cap, config in users:
        raw, grade, duration, day = synthetic_user(
            np, r, cdf_at, rng, rows, days, easy_scale, cap
        )
        saved = (r.ROLL_DAYS, r.ROLL_ROWS, r.MIN_CLASS)
        r.ROLL_DAYS, r.ROLL_ROWS, r.MIN_CLASS = (
            config["roll_days"],
            config["roll_rows"],
            config["min_class"],
        )
        try:
            arms, _y, _v, active, stats = r.recal_user(
                raw,
                grade,
                duration,
                day,
                cap,
                n_min=config["min_rows"],
                n_min_sens=10**9,
            )
        finally:
            r.ROLL_DAYS, r.ROLL_ROWS, r.MIN_CLASS = saved
        cal = arms["cal"]
        assert np.isfinite(cal).all()
        print(
            f"{name}: {rows} rows, {int(active.sum())} recalibrated, "
            f"{stats['identity_fits']} identity fits, {stats['clamped']} clamped"
        )
        out_users.append(
            {
                "name": name,
                "config": config,
                "cap_millis": int(cap),
                "day": day.tolist(),
                "grade": grade.tolist(),
                "duration_millis": duration.tolist(),
                "raw": [[float(f"{float(v):.4f}") for v in row] for row in raw],
                "cal": [[float(f"{v:.10f}") for v in row] for row in cal],
                "active": active.astype(int).tolist(),
            }
        )
    document = {
        "source": "training repository scripts/sm125_recal.py recal_user, synthetic",
        "seed": SEED,
        "s_grid": list(r.S_GRID),
        "cols": [[sn, s] for sn, s in r.COLS],
        "users": out_users,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(document, f, separators=(",", ":"))
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
