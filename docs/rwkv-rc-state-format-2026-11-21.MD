# Release-candidate state format: int8 card rows and compact notes — 2026-11-21

**Decision (user, 2026-11-21):** the family release candidate (Sat 2026-11-28) stores every size's card and note
state as **int8 rows with float16 row scales and int8 token shifts** (the training repository's `w8+s8hx8` card
layout, with the `hx8` notes of `w8+s4c8nhx8`). Profile value `compact_cards_int8`, the new default for Big, Small
and Tiny. The reason: the previous default (`cards_int4_notes_int8`, int4 cards with f32 row scales) breaks on long
cards (non-finite state, a crash in the bulk warm-up), now that Small and Tiny ship too. Compact int4 card rows stay
available as the non-default `compact_cards_int4`; the old f32-scale int4 path stays readable and selectable only in
Rust (benchmarks, comparisons), not from a profile.

The model files and the family mapping (`RWKV_SIZE_MODELS`, release `rwkv-family-2026-11-13`) are unchanged.

## Why

- The compact state format (SM129 / SM131) was free on the 732-user benchmark, but benchmark users never exceed 127
  prior answers per card. The fork re-quantizes the state after every answer; on long cards the int4 rounding error
  compounds (anki-rwkv `artifacts/small-model/sm133-fork-compact/check-and-scope.md`).
- The long-card measurement on real users (SM136, anki-rwkv branch `sm136`) found int4 card rows, compact or not,
  above the +0.25% ceiling for every size on cards with 128+ prior answers (up to +4.55% curve), while int8 card rows
  with float16 scales and int8 shifts stayed at most +0.12%.

## Format

Both compact layouts share the magic `ARWKVMODSTATE2` with the plain ones; a new precision byte selects them, so a
reader of this build decodes all four.

| Precision byte | Name | Matrix rows (`HEAD_SIZE` values) | Shift vectors (`D_MODEL` values) |
| --- | --- | --- | --- |
| `0x18` | `Int8Compact` | float16 scale + int8 codes | float16 scale + int8 codes |
| `0x14` | `Int4Compact` | float16 scale + int4 codes, 2 per byte | float16 scale + int8 codes |
| `8` | `Int8` (plain) | f32 scale + int8 codes | float16 values |
| `4` | `Int4` (plain) | f32 scale + int4 codes | float16 values |

- Scale rule (`round_scale_f16` in the training repository): abs-max / limit (127 or 7), clamped to 65,504, rounded
  to float16 (nearest even, subnormals included); 1.0 when the result is 0 (all-zero vectors and underflow). Codes:
  `clamp(round_half_even(x / scale), ±limit)`.
- An empty shift block is its u32 count 0 only; a non-empty one is the count, the 2-byte scale and the codes.
- The float16 clamp keeps every stored value finite (at most 127 × 65,504), so a long, drifting card saturates instead
  of overflowing. The decoder still rejects non-finite or non-positive scales and the codes −128 / −8.

Policies (`StatePrecision::module_precisions`): `compact_cards_int8` = Int8Compact cards and notes;
`compact_cards_int4` = Int4Compact cards, Int8Compact notes; `cards_int4_notes_int8` = Int4 cards, Int8 notes;
deck, preset and global stay f32.

### Bytes per card / note (one model; a pair stores two states plus a 19-byte wrapper)

Asserted by `rwkv_state_precision_stored_sizes_of_the_small_and_tiny_models` and
`rwkv_state_precision_stored_sizes_of_the_big_model`, equal to SM136's `sm136-bytes.json`:

| Size | `compact_cards_int8` (default) | `compact_cards_int4` | `cards_int4_notes_int8` (previous default) | `int8` |
| --- | ---: | ---: | ---: | ---: |
| Big | 13,891 / 9,267 | 7,747 / 9,267 | 9,271 / 10,283 | 15,415 / 10,283 |
| Small | 2,545 / 1,315 | 1,521 / 1,315 | 1,963 / 1,567 | 2,987 / 1,567 |
| Tiny | 1,315 / 1,315 | 803 / 1,315 | 1,055 / 1,567 | 1,567 / 1,567 |

Per card + note with the default pair (two models): Big 46,354 B (was 39,146), Small 7,758 B (was 7,098), Tiny
5,298 B (was 5,282).

## Upgrade: one rebuild, no conversion

- `qt/aqt/rwkv_precision.py`: `RWKV_STATE_PRECISION_DEFAULT = "compact_cards_int8"` is the single switch for the
  default; `RWKV_STATE_PRECISIONS = ("f32", "int8", "compact_cards_int8", "compact_cards_int4")`. A saved
  `cards_int4_notes_int8` (or the removed `int4`) is no longer accepted and maps to the default, so no profile keeps
  the overflowing writer. The TypeScript fallback `RWKV_DEFAULT_STATE_PRECISION` (`ts/routes/deck-options/lib.ts`)
  mirrors it.
- The replay key hashes the precision string, so every state cache rebuilds once on the first start (a full replay
  from the revlog). `_RWKV_STATE_CACHE_VERSION` is also bumped 13 → 14.
- Stored states are not converted: re-quantizing is not bit-identical to a fresh replay, a drifted card would keep its
  drift and an overflowed one cannot be decoded. The decoder still reads the plain bytes 4 / 8, so undo states, spills
  and old checkpoints stay readable until the rebuild replaces them.

## Panic fix: a broken state is an error, and the cache rebuilds

- `ModuleState::into_decoded` / `decoded` (both `expect("packed state was validated on load")`) are gone. Bulk replay
  (`bulk.rs`) decodes every stored state it will read before it changes anything (`validate_stream_states`), and the
  in-replay decodes (`layer_states`, `run_module`, `run_interleaved`, the state-only wavefront, the workload
  simulation's background replay) return `io::Error`. A state that overflowed in the plain int4 layout now fails the
  call with `invalid RWKV compact state: …` (naming the module and id) instead of panicking.
- On the desktop, a failed cache restore is logged and the backend falls back to a full replay
  (`_restore_reviewer_backend_cache` → `reset_cache_snapshot`); a panic was a `BaseException` that escaped that path.
  A full replay in the compact layouts cannot write an undecodable state.
- Model passes that cannot report an error (workload simulation and forecast) use `decoded_or_fresh`: an undecodable
  state, which only the plain layouts can write, is read as fresh state instead of panicking.

## Long cards (synthetic, 20,000 answers)

`rslib/src/rwkv/long_card_tests.rs`: one synthetic card answered 20,000 times, every stored module state fed back as
the app does (`RwkvRuntime::review`), and the bulk warm-up in calls of 250 answers; all six release models; patterns
`mixed` (15% Again, 10% Hard, 65% Good, 10% Easy, 40-Again streaks every 250 answers, mostly 1-3-day intervals),
`again1d` and `good1d`; the float32-state run of the same model as the reference. `rwkv_long_card_full` (ignored,
release, about 100 s with 8 threads) asserts no error, no panic and no non-finite output or stored value for both
compact policies; the quick `rwkv_long_card_quick_compact_states_stay_finite` runs by default.

Result: **both compact policies answer all 20,000 answers on every model and pattern, on both paths, with every
output and stored value finite.** The previous default fails exactly where the SM133 check found it, now with an
error instead of a panic on the bulk path.

| Model | Previous default fails at answer (mixed / again1d / good1d) | `compact_cards_int8`: mean \|Δr\| vs float32, answers 5,001-20,000 (mixed / again1d / good1d) | `compact_cards_int4`: same, and the first answer with a card row at the float16 cap |
| --- | --- | --- | --- |
| the show-time big model (SM91s2) | 11,201 / 2,722 / 2,242 | 0.0185 / 0.0051 / 0.0035 | 0.0402 / 0.0019 / 0.0018; capped from 1,622 / 1,140 / 646 |
| the show-time instant-only big model (SM99) | 6,135 / 4,761 / 2,226 | 0.0068 / 0.0127 / 0.0001 (capped from 19,835 / 19,215 on again1d / good1d) | 0.0519 / 0.0073 / 0.0001; capped from 1,438 / 1,285 / 556 |
| the show-time retrain (SM92) | finite / 7,321 / 10,566 | 0.0019 / 0.0012 / 0.0009 | 0.0190 / 0.0023 / 0.0073; capped on again1d / good1d from 1,396 / 3,141 |
| the Small Instant-Only model (SM98) | finite / 5,590 / 9,267 | 0.0034 / 0.0019 / 0.0001 | 0.0172 / 0.0004 / 0.0000; capped from 770 / 6,406 |
| the Tiny two-head model (SM106) | finite / 10,371 / finite (row scale 1.5e9) | 0.0031 / 0.0005 / 0.0002 | 0.0148 / 0.0015 / 0.0004; capped on again1d from 6,820 |
| the Tiny Instant-Only model (SM101) | finite / 4,618 / 6,898 | 0.0053 / 0.0007 / 0.0000 | 0.0394 / 0.0071 / 0.0008; capped from 819 / 6,832 |

- Compact int4 cards drift as expected: on `mixed` the mean |Δr| vs float32 is 0.015-0.052 after answer 5,000 on
  every model (int8 cards: 0.002-0.019), and on 1-day patterns most card rows run into the float16 cap within a few
  hundred to a few thousand answers (they stay finite).
- Int8 cards stay below the cap everywhere except the show-time instant-only big model (SM99) on the two extreme
  1-day patterns, which reach it only near answer 19,000-20,000 (finite, drift ≤ 0.013).
- Full table and JSON rows: `RWKV_LONG_CARD_OUTPUT=<file>` writes one JSON line per run.

## Parity with the training repository's scorer (synthetic data only)

- **Codec, bit for bit:** `qt/tools/rwkv_compact_state_codec_vectors.py` feeds seeded random states and edge rows
  (zero, 1e-9 to 3e38, exact ties, float16 subnormal scales, infinity) through the scorer's
  `int_row_quant(bits | F16_SCALE)` and the big model's `_quantize_rows` / `_round_shift` (they agree), and writes
  `rslib/src/rwkv/testdata/compact-state-codec.json.gz` (86 KB). `rwkv_state_precision_compact_codec_matches_the_scorer`
  reproduces every int8 and int4 row and every int8 shift bit for bit on both engines (head size 16 / 32, `D_MODEL`
  64 / 128); the only allowed difference is the sign of a zero (the scorer keeps −0 where an integer code cannot). The
  row orientation is the last axis of the scorer's `[B, H, K, K]` state, the fork's contiguous row.
- **Model, the show-time big model (SM91s2):** `qt/tools/rwkv_compact_state_big_vectors.py` runs the scorer's fork-model
  path (`install_state_quant_model` + `set_state_streams`) on the two synthetic users of
  `synthetic_testvector_users.py` (show-time inputs, deleted cards: neighbour, float32 weights) at float32,
  `compact_cards_int8` and `compact_cards_int4` state. One correction: the scorer also rounds the shift input of a
  stream's first row, which is that row's own input and not a stored vector; the fork does not, so the script rounds
  only rows that read an earlier row. Fixture: `testdata/compact-state-big-vectors.json.gz` (first 500 rows per user);
  `rwkv_legacy_compact_state_matches_the_scorer`; `ANKI_RWKV_COMPACT_BIG_VECTORS` points it at the full vectors.
  A rounded state is chaotic: a one-ulp float difference (summation order) flips an occasional code and the flip
  carries forward, so a strict per-output bound does not hold over long histories even for the plain `int8` format.
  The gate is measured against that floor instead: the same Rust model on inputs moved by one ulp. Results (mean
  absolute probability difference over all 12 outputs per row):

  | State | Rows | Rust vs scorer: mean / maximum | Rust, inputs moved by one ulp: mean | Mean distance from float32 state: Rust / scorer |
  | --- | ---: | --- | ---: | --- |
  | float32 | 6,309 (full) | 9.2e-8 / 8.3e-7 (gate: every output ≤ 1e-5) | | |
  | `compact_cards_int8` | 1,000 (fixture) | 2.2e-5 / 6.1e-4 | 1.5e-5 | 2.007e-4 / 1.995e-4 |
  | `compact_cards_int8` | 6,309 (full) | 3.7e-5 / 8.6e-4 | 3.0e-5 | 2.007e-4 / 2.008e-4 |
  | `compact_cards_int4` | 1,000 (fixture) | 1.6e-5 / 5.5e-4 | 8.8e-6 | 7.50e-4 / 7.49e-4 |
  | `compact_cards_int4` | 6,309 (full) | 3.6e-5 / 1.2e-3 | 3.6e-5 | 8.14e-4 / 8.12e-4 |
  | plain `int8` (reference, not gated) | 6,309 (full) | 1.5e-5 / 4.2e-4 | | |

  The gate: the mean difference from the scorer at most 3× the one-ulp floor, and the rounding's effect (mean
  distance from the float32 state) within 5% of the scorer's; a missing or extra rounding step moves that effect
  (before the first-row correction the scorer's rows already differed at row 0). Rows 0-98 of the first user agree
  within 1e-4 on every output.
- Small and Tiny have no model-level scorer vectors yet (their scorer path is the eager training model with
  `state_vq` hooks); they are covered by the codec parity above, the bulk / per-answer bit identity and the long-card
  test.

## Measured on a synthetic collection

`rwkv_model_size_benchmark` (`rslib/src/rwkv/model_size_bench.rs`, release, `ANKI_RWKV_SIZE_BENCH_PRECISION`): a
synthetic history of 4,000 cards, 2,000 notes and 32,000 answers; median of 5 samples after one warm-up sample;
this Windows desktop (16 logical CPUs), other work idle. Warm-up = the bulk replay of all answers.

| Runtime (default pair per size) | Precision | Warm-up ms (reviews/s) | Stored card + note state, whole collection | Per card + note |
| --- | --- | ---: | ---: | ---: |
| Big pair (SM91s2 + SM99) | `compact_cards_int8` | 2,715 (11,785) | 148.3 MB | 46,354 B |
| | `cards_int4_notes_int8` | 2,528 (12,659) | 115.4 MB | 39,146 B |
| | `compact_cards_int4` | 2,613 (12,245) | 99.2 MB | 34,066 B |
| Small pair (SM92 + SM98) | `compact_cards_int8` | 694 (46,118) | 25.7 MB | 7,758 B |
| | `cards_int4_notes_int8` | 697 (45,938) | 22.1 MB | 7,098 B |
| | `compact_cards_int4` | 793 (40,377) | 17.5 MB | 5,710 B |
| Tiny pair (SM106 + SM101) | `compact_cards_int8` | 644 (49,707) | 15.9 MB | 5,298 B |
| | `cards_int4_notes_int8` | 624 (51,307) | 14.8 MB | 5,282 B |
| | `compact_cards_int4` | 604 (53,014) | 11.8 MB | 4,274 B |

The warm-up time is unchanged within this machine's sample noise (the single-model runs move by ±10% in both
directions between precisions); queue scoring of 4,000 cards took 153 / 44 / 39 ms (Big / Small / Tiny pair) at the
new default against 150 / 43 / 38 ms before. On this collection (two cards per note) the stored state grows by 28% for Big, 17% for Small and 7% for Tiny
against the previous default: note states shrink (Big 10%, Small and Tiny 16%), card states grow (Big 50%, Small 30%,
Tiny 25%).

## Tests and commands

Run on Windows with the system Rust 1.99 toolchain (the fork pins 1.97.1), debug unless noted:

- `cargo test -p anki --lib rwkv::` (default features): 371 passed, 0 failed, 35 ignored.
- `cargo test -p anki --lib --no-default-features rwkv::`: 371 passed, 0 failed, 34 ignored (the long-card tests
  skip the small Instant-Only models, which this build cannot load alone).
- `cargo clippy -p anki --lib --tests` with and without default features: no warnings.
- `cargo test --release -p anki --lib rwkv_long_card_full -- --ignored --nocapture` with
  `RWKV_LONG_CARD_PLAIN=1 RWKV_LONG_CARD_THREADS=8`: passed in 100 s (108 runs).
- `ANKI_RWKV_COMPACT_BIG_VECTORS=<full vectors> cargo test --release -p anki --lib rwkv_legacy_compact_state_matches_the_scorer`:
  passed on all 6,309 rows.
- Not run here: the Python suites (`qt/tests/test_rwkv_precision.py`, `test_rwkv_model.py`,
  `test_rwkv_inference_process.py`, `test_rwkv_scheduler.py`, `test_rwkv_srs_benchmark.py`), the TypeScript unit test
  (`rwkv-model-estimates.test.ts`) and the deck-options end-to-end test; they need a full `just build`.

## Not changed

- The Deck Options page has no storage-precision selector; the `deck-config-rwkv-*` precision strings stay unused
  and untranslated as before. The per-card estimate uses the new bytes.
- `runtime_precision_bench.rs` (Unix only) still compares the plain formats.
