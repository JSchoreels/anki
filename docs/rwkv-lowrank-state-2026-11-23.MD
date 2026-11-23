# Low-rank card and note state — 2026-11-23

**Decision (user, 2026-11-23):** every size (Big, Small, Tiny) stores its card and note state as a **rank-2 product
below 16 answers and a rank-4 product from the 16th answer, with int8 factors** (the training repository's label
`w8+lr2.16.4q8x8`, scored in its low-rank int8 states experiment (SM148)). Profile value `compact_lowrank_int8`, the
new default. It passes the user's budgets at every size on the 732-user benchmark set (Big at most +0.15% against its
own int8 rows, Small at most +0.5% and Tiny at most +1.0% against Big at int8; 95% CI upper bound per head) and saves
about 82% (Big), 66% (Small) and 65% (Tiny) of the card and note state bytes against the int8 rows.

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
