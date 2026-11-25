# RWKV state building on Apple M1 Max

On a 10-core Apple M1 Max, one card per low-rank solve gave the fastest median full rebuild for Big, Small and Tiny. Grouping four cards, as the AVX2 path does, lowered replay throughput by 4.9% to 7.3%. The macOS runtime now groups two cards only for Small and Tiny in a single-worker replay pool, where long-history replay gained 2.9% and 4.0% respectively.

## Full rebuild

Median reviews per second during replay with all 10 threads and the shipped model pairs. The first three columns use the shipped low-rank format (`compact_lowrank`, int6 factors for Big and Small, int8 for Tiny). Percentages compare throughput with one card per solve. The last column uses compact int8 rows as a separate format comparison.

| Model pair | 1 card | 2 cards | 4 cards | Int8 rows |
|---|---:|---:|---:|---:|
| Big | 11,550 | 10,978 (−5.0%) | 10,983 (−4.9%) | 10,784 |
| Small | 39,229 | 35,352 (−9.9%) | 36,785 (−6.2%) | 40,041 |
| Tiny | 64,691 | 60,087 (−7.1%) | 59,944 (−7.3%) | 72,075 |

Including model loading, replay, and the full and delta checkpoints, one-card rebuilds took **18.07 s** for Big, **5.40 s** for Small and **3.33 s** for Tiny. Four-card batches were slower than one card in all nine same-pass rebuild comparisons. Two-card results varied more, especially on Tiny.

## Long card histories

Median reviews per second for a standalone model on one thread. Each of 1,000 cards received 24 reviews, taking every card through the switch to rank 4 at 16 answers.

| Model | 1 card | 2 cards | 4 cards | Int8 rows |
|---|---:|---:|---:|---:|
| Big | 3,774 | 3,762 (−0.3%) | 3,639 (−3.6%) | 3,920 |
| Small | 14,520 | 14,770 (+1.7%) | 14,118 (−2.8%) | 18,329 |
| Tiny | 25,817 | 26,766 (+3.7%) | 24,596 (−4.7%) | 36,050 |

## Two-card batches on Small and Tiny

Two-card batches beat one card in all three single-thread long-history passes on both sizes. For a full mixed-history rebuild limited to one thread, throughput went from 7,030 to 7,056 reviews/s on Small and from 12,721 to 13,070 on Tiny. The difference varied between passes. With ten threads on the long histories, each size also had passes favoring each width.

The isolated codec benchmark explains the model-size difference. In 30 alternating blocks timed with the macOS thread CPU clock, pairing writes with 16-value heads saved 8–10% CPU time per layer write at rank 2 and 12–13% at rank 4, across int6 and int8 factors. For Big’s 32-value heads, the two-card cost was within 1% at rank 2 and about 5% lower at rank 4. Four-card batches took more CPU time per write in all eight cases.

## Verification on the final implementation

The table compares release builds of `517dc4473` with and without the macOS dispatch change. Before and after runs alternated order for each model. Values are median reviews per second across five long-history passes and three full rebuilds.

| Workload | Small, before → after | Tiny, before → after |
|---|---:|---:|
| Long histories, 1 worker | 14,630 → 15,047 (+2.9%) | 27,090 → 28,170 (+4.0%) |
| Full rebuild, 1 worker | 7,175 → 7,238 (+0.9%) | 13,195 → 13,329 (+1.0%) |
| Full rebuild, 10 workers | 39,832 → 39,970 | 66,484 → 67,923 |

Both builds used one card per solve in the ten-worker control. Tiny’s individual ten-worker comparisons varied by up to 14.9%. In the single-worker long-history test, the final build was faster in four of five Small comparisons and all five Tiny comparisons. Card and note byte counts and prediction checksums matched across all before and after runs of that test.

## Measurement and correctness

- Built native `aarch64-apple-darwin` release binaries with Rust 1.97.1 on an M1 Max with eight performance cores and two efficiency cores. The baseline was `42ab96f1f`, which has the engine and weights from `6cb164606`. The two experimental builds grouped the replay streams and compiled the solver for the matching width, eight heads for two cards and sixteen heads for four.
- Used the SM163 synthetic history of 205,785 reviews on 14,000 cards over 730 days. The full rebuild replays up to 16,384 reviews per chunk and writes both checkpoints to a temporary store. The long-history benchmark uses the same model weights and takes the median of three replay timings after one warm-up. [SM163 rebuild harness](https://github.com/JSchoreels/anki-rwkv/blob/97128eb8cf14f5ddcc21e45a82d7926bc11f92a8/artifacts/small-model/sm163/sm163_speed.rs) · [Long-history benchmark](../rslib/src/rwkv/model_size_bench.rs)
- Repeated all four configurations in three different orders after other builds and tests had finished. The full-rebuild and long-history width comparisons give the median of those three passes. Idle CPU sampled just before each run had a median of 81% and ranged from 44% to 87%. Earlier trials under competing load were excluded.
- Both experimental builds passed 24 codec and replay checks covering both engine head sizes, int6 and int8 factors, byte-for-byte agreement with the frozen codec, reconstructed matrices, partial batches and replay across chunk boundaries. Across all three low-rank widths, each long-history workload also produced the same card and note byte counts and prediction checksum. The complete Rust library suite passed with one Rayon worker in both primary-engine configurations (1,607 tests in each). [Codec tests](../rslib/src/rwkv/lowrank.rs) · [Replay tests](../rslib/src/rwkv/precision_tests.rs)
- The one-card build already vectorizes across a card’s four heads. ARM64 disassembly of the encoder and Jacobi solver showed 128-bit NEON vector arithmetic with separate double-precision multiply and add instructions. The inspected functions contained no fused multiply-add operations. [Original SM168 handoff and PC measurements](https://github.com/JSchoreels/anki/commit/42ab96f1ff50aeb29bfdaec9793df4189b167d95)
