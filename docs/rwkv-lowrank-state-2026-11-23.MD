# Low-rank card and note state — 2026-11-23

**Decision (user, 2026-11-23):** every size (Big, Small, Tiny) stores its card and note state as a **rank-2 product
below 16 answers and a rank-4 product from the 16th answer, with int8 factors** (the training repository's label
`w8+lr2.16.4q8x8`, scored in its low-rank int8 states experiment (SM148)). Profile value `compact_lowrank_int8`, the
new default. It passes the user's budgets at every size on the 732-user benchmark set (Big at most +0.15% against its
own int8 rows, Small at most +0.5% and Tiny at most +1.0% against Big at int8; 95% CI upper bound per head) and saves
about 82% (Big), 66% (Small) and 65% (Tiny) of the card and note state bytes against the int8 rows.

**Update (user, 2026-11-24): 6-bit factors for Big and Small (SM162).** The default profile value is now
`compact_lowrank`: Big and Small store the same schedule with int6 factor codes (precision byte `0x26`, the training
repository's `w8+lr2.16.4q6x8`, from the smaller shift vectors and factors experiment (SM152); codes in [-31, 31] with
a float16 scale of max/31 per factor vector, bit-packed four codes in three bytes), Tiny keeps the int8 factors below
(`0x28`). About 85% (Big) and 71% (Small) fewer state bytes than the int8 rows. `compact_lowrank_int8` and
`compact_lowrank_int6` remain native values (one width on every size); state-cache version 16; details in `RELEASE.md`.
The sections below describe the int8-factor layout, which `0x26` follows except for the code width.

## Format

Precision byte `0x28` (`LowRankInt8`) under the magic `ARWKVMODSTATE2`; the decoder still reads every older layout.

- Per layer: the x shift block (float16 scale + int8 codes, as `0x18`), the u32 value count of the matrix, the
  low-rank block, the channel shift block.
- Low-rank block: answer count n (u8, saturating at 255), rank c (u8), then per head c left vectors and c right
  vectors, each a float16 scale and `HEAD_SIZE` int8 codes (scale rule of the compact rows: abs-max / 127, clamped to
  65,504, 1.0 on zero; codes round-half-even in [-127, 127]). The stored matrix is sum_i L_i R_i^T of the decoded
  vectors.
- Rank: min(n, r) with r from the schedule (`RankSchedule`, default `2.16.4`: r = 2 below 16 answers, 4 from 16). At
  the 16th answer the basis holds 2 stored components and the new value vector, so the state is rank 3 with a zero
  fourth component; from the 17th answer it is a full rank 4. The stored bytes do not depend on the schedule.
- Write (`rslib/src/rwkv/lowrank.rs`): the answer's output uses the float update; the stored state is the best rank-r
  approximation of the float update within span(previous left factors, value vector) (normalised basis, Gram
  eigen-decomposition with a 1e-5 relative cut, then the eigen-decomposition of B B^T for B = Q^T S'), split evenly
  into left and right factors, each vector rounded to int8. f64 solve (the scorer: float32).
- A matrix without factors (held at float32 or stored in another layout) is factored on the full basis with the answer
  count unknown (255). Non-finite values are replaced before the solve, so every stored value is finite.

Policies: `compact_lowrank_int8` (default schedule) and `compact_lowrank_int8:<spec>` for benchmarks (for example
`compact_lowrank_int8:2` = rank 2 for every answer count, the scorer's `w8+lr2q8x8`).

### Bytes per card / note

The state grows with the answer count. At full rank (fork layout, one model), card / note:

| Size | rank 1 | rank 2 | rank 4 | mean over the answer counts of 723 benchmark users | int8 rows (`compact_cards_int8`) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Big | 1,657 / 1,111 | 2,473 / 1,655 | 4,105 / 2,743 | 2,524 / 1,720 | 13,891 / 9,267 |
| Small | 533 / 309 | 821 / 453 | 1,397 / 741 | 839 / 470 | 2,545 / 1,315 |
| Tiny | 309 / 309 | 453 / 453 | 741 / 741 | 462 / 470 | 1,315 / 1,315 |

The mean uses the answer-count shares of the low-rank experiment's byte estimate (`sm148-bytes.json`) with the fork's
2 bytes per layer for the count and the rank. Deck Options estimates use the means.

## Upgrade: one rebuild, no conversion

- `RWKV_STATE_PRECISION_DEFAULT = "compact_lowrank_int8"` in `qt/aqt/rwkv_precision.py` (the TypeScript fallback
  mirrors it). `compact_cards_int8` is no longer a profile value: the Deck Options page saves the value it loaded, so
  profiles that saved the release candidate's default move to the new one. The replay key hashes the precision string
  and `_RWKV_STATE_CACHE_VERSION` goes 14 -> 15, so every state cache rebuilds once.
- Stored states are not converted. Undo states, spills and checkpoints in older layouts stay readable until the
  rebuild replaces them; a write on top of such a state factors it on the full basis.

## Parity with the training repository's scorer (synthetic data only)

`qt/tools/rwkv_lowrank_state_codec_vectors.py` runs the scorer's `LowRankCodec.encode` on seeded synthetic recurrences
(3 entities x 2 heads x 24 writes, head size 16 and 32, schedules `2.16.4` and `2` with int8 factors, `2.16.4` with
float factors) and writes `rslib/src/rwkv/testdata/lowrank-state-codec.json.gz`.

- `rwkv_state_lowrank_codec_matches_the_scorer`, each write from the scorer's own previous state: int8 factors give
  the identical stored matrix on 625 of 630 head writes (K = 32: 252 of 252; K = 16: 249 of 252, the rest one code
  step apart: relative Frobenius error at most 3.5e-3); float factors agree to at most 2.2e-6.
- `rwkv_state_lowrank_chain_follows_the_scorer`, the Rust chain on its own: mean relative difference at most 1.3e-3
  (int8, K = 16), 0 (int8, K = 32), at most 7.3e-6 (float factors).

## End to end on benchmark users (2026-11-24)

`rwkv_big_lowrank_state_end_to_end` and `rwkv_small_lowrank_state_end_to_end` (ignored; the input file comes from an
environment variable and is never bundled, it holds benchmark rows) replay three benchmark users of the training
repository's low-rank int8 states experiment (SM148) (2388, 4676 and 1866, the last with a card of 128+ answers;
show-time inputs, deleted cards `neighbour`) through all six release models at float32, `compact_cards_int8`, the new
default and `compact_lowrank_int8:2`. The vectors and the comparison scripts are in the training repository
(`artifacts/small-model/sm151/`).

- Big models (show-time big model (SM91s2), show-time instant-only big model (SM99)) against the scorer's per-row
  outputs on the same rows: per-user LogLoss within 1.8e-5 at the new default (float32 state: 2e-8).
- All six models: the default's LogLoss change against the int8 rows matches the experiment's per user within 5.5e-5
  (curve and instant; rule: 3e-4 per user, 1e-4 mean).

## Speed

Release build, `rwkv_model_size_benchmark` (whole-model state replay, synthetic answers), int8 rows and the low-rank
default back to back, one thread, measured while a GPU job and other CPU work ran (absolute reviews/s under load,
ratios from back-to-back pairs; history sizes via `ANKI_RWKV_SIZE_BENCH_CARDS` / `ANKI_RWKV_SIZE_BENCH_ROUNDS`):

| History | Size | int8 rows: reviews/s (µs per review) | low-rank: reviews/s (µs per review) | extra µs per review |
| --- | --- | ---: | ---: | ---: |
| 4,000 cards x 8 answers | Big | 3,455 (289.5) | 3,614 (276.7) | within noise |
| 4,000 cards x 8 answers | Small | 16,436 (60.8) | 13,330 (75.0) | +14.2 |
| 4,000 cards x 8 answers | Tiny | 30,703 (32.6) | 23,438 (42.7) | +10.1 |
| 1,000 cards x 24 answers | Big | 3,653 (273.8) | 3,169 (315.5) | +41.8 |
| 1,000 cards x 24 answers | Small | 16,397 (61.0) | 10,285 (97.2) | +36.2 |
| 1,000 cards x 24 answers | Tiny | 30,644 (32.6) | 17,600 (56.8) | +24.2 |

The cost is in the codec's small solves (`rwkv_state_lowrank_codec_micro_benchmark`: one layer write 19 to 21 µs at
head size 16, 33 µs at 32; a read 0.7 µs and 6 µs). The Jacobi solver stops once a sweep no longer reduces the
off-diagonal mass: before, a third of the 5 x 5 solves ran all 60 sweeps and long-card replay took up to 2.6 times
the int8 time (Tiny); the results are unchanged (same codec parity). A later rewrite of the codec (faster low-rank codec in the fork
(SM156)) keeps every stored byte identical (checked against a frozen copy of the codec above: long chains of every
schedule shape, the scorer's vectors, random bases; `lowrank_reference.rs`) and halves the write: no per-head
allocation (fixed-size arrays), and the four heads' Jacobi solves run in lockstep so their serial chains of divisions
and square roots overlap (one layer write at rank 4: 21 to 22 -> 11 µs at head size 16, 33 -> 16 µs at 32; at
rank 2: 7.7 -> 3.4 µs and 17 -> 6.4 µs; reads 0.8 -> 0.5 µs and 6.9 -> 1.4 µs). Replay of 1,000 cards x 24 answers, one thread, medians under load: Small
90.3 -> 72.4 µs per review, Tiny 54.5 -> 39.2, Big 301.6 -> 237.0 (int8 rows 59.5, 31.1 and 250.8 in the same runs). At four threads (the Deck Options benchmark)
Small and Tiny save 72% and 84% of Big's replay time as pairs (70% and 84% alone), against 77% and 88% at the int8
rows; the Deck Options table uses the low-rank values. Its byte estimate keeps the mean per card and note, and the
tooltip shows the range from rank 1 (one answer) to rank 4 (16 answers or more).
