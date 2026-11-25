# RWKV state building on Apple M1 Max

## Multicore rebuilds with the ARM64 kernels

The SM163 synthetic collection contains 205,785 reviews on 14,000 cards spanning 730 days. The baseline for these measurements is `2d929e0d3`, which already includes the Apple Silicon single-worker codec tuning. Both builds run natively on the same 10-core M1 Max with the shipped Big, Small and Tiny model pairs and their default compressed state formats.

### Full native rebuild

The times include model loading, history replay and the full and delta checkpoints. Time saved compares the two median durations across five passes. Replay throughput covers the history replay phase alone.

| Model pair | Before | Final | Time saved | Replay before → final, reviews/s | Faster pairs (paired time savings) |
|---|---:|---:|---:|---:|---:|
| Big | 17.67 s | 13.87 s | 21.5% | 11,802 → 15,097 | 5/5 (18.2% to 33.5%) |
| Small | 5.24 s | 4.50 s | 14.2% | 40,417 → 47,312 | 5/5 (12.0% to 29.0%) |
| Tiny | 3.06 s | 2.73 s | 10.8% | 70,751 → 79,602 | 5/5 (9.6% to 14.6%) |

Compared directly with the initial sixteen-output NEON prototype, the final build reduced rebuild time by 8.3% on Big, 10.2% on Small and 9.4% on Tiny, using the median of the three paired changes for each size.

### Comparison with int8 rows

Each format used the final kernels and the production chunk limit for its model. The table gives median replay throughput over three passes. Both formats matched their own baseline’s serialized-state and prediction digests.

| Model pair | Shipped low-rank, reviews/s | Int8 rows, reviews/s | Low-rank throughput difference |
|---|---:|---:|---:|
| Big | 14,692 | 13,710 | +7.2% |
| Small | 47,846 | 51,074 | −6.3% |
| Tiny | 79,150 | 87,783 | −9.8% |

### Five optimization passes

1. **Dense projections.** Four input rows share a block of weights and keep up to sixteen output values per row in NEON registers. One scan per block determines whether the inner loop can skip testing each scale for zero. On the synthetic history, 93.50% of Big blocks, 97.78% of Small blocks and 96.25% of Tiny blocks were dense. Blocks containing either sign of zero still skip those products, including when a weight is infinite.
2. **Narrow projections.** Four-output tiles also keep their accumulators in registers. In the isolated projection benchmark, the two projection changes together cut a dense 64-input, 4-output block from 1,805 to 550 ns and a 128-input, 8-output block from 4,783 to 2,415 ns relative to the first sixteen-output prototype. Scalar tails retain their original multiply and add order.
3. **Low-rank compression.** The Jacobi solver visits only matrix sizes present in the batch, returns a one-dimensional eigenproblem directly and uses two ARM64 double-precision lanes for groups of one or two heads. When every head needs the current rotation, the solver skips the per-element selection. In the final source, median paired thread CPU time per complete layer write fell by 0.38% to 4.35% across twelve isolated cases covering both head sizes, both factor widths and three history lengths. The benchmark checked the encoded bytes and the reconstructed matrix. In full rebuilds, the solver-only difference fell within the variation between runs for each model.
4. **Recurrent state update.** Each row of the new state feeds its output dot product while the row is still in registers. In isolation, time to update a head and calculate its output fell from 206 to 72 ns for a 16-value head and from 526 to 246 ns for a 32-value head. The state update retains its separate multiplications and additions, and both dot products retain their original fused operations and reduction order.
5. **Replay chunk size.** The desktop passes up to 16,384 reviews at a time. The experiment varied the engine’s internal limit across 2,048, 4,096, 8,192 and 16,384 rows. Doubling it from 8,192 to 16,384 raised macOS’s reported peak memory footprint from 973 to 1,159 MiB on Big, from 316 to 439 MiB on Small and from 267 to 343 MiB on Tiny. Each footprint comes from a separate full rebuild without the post-timing snapshot used for the bitwise checks. On Big the larger limit won all three initial comparisons. Three back-to-back pairs on the optimized kernels confirmed time savings of 2.75%, 3.01% and 5.62%. The larger limit gave mixed wall-time results on Small and Tiny. Two further runs per limit on the final kernels, in opposite orders, measured 774–823 MiB at 8,192 and 1,134–1,167 MiB at 16,384 on Big. The macOS ARM64 runtime now uses 16,384 for long state-only Big rebuilds with multiple workers. Other paths use the 8,192 default.

### Rebuild and equivalence checks

Each timed run starts a fresh process, loads the model pair, replays the history and writes both the full and delta checkpoints. Synthetic history generation, snapshot serialization for comparison and subsequent prediction checks run outside the timer. The reported replay throughput excludes loading and checkpoints. The benchmark uses the same weights, review order and precision in both builds. [SM163 synthetic workload](https://github.com/JSchoreels/anki-rwkv/blob/97128eb8cf14f5ddcc21e45a82d7926bc11f92a8/artifacts/small-model/sm163/sm163_speed.rs)

The final full-rebuild comparison runs the baseline and selected build back-to-back in each of five passes, alternating which binary runs first for each model. The first three passes also time the initial sixteen-output NEON prototype and int8-row control. A lock serializes benchmarking, compilation and repository checks.

After each rebuild, the harness sorts the card, note, deck and preset states by ID, then computes a BLAKE3 digest of the IDs, lengths, encoded state bytes and global state. A second digest covers every prediction field for 128 card queries, including the per-button probabilities, intervals and S90 values. All 14,000 card states, 7,000 note states, three decks, one preset and the global state matched the baseline in every full rebuild, as did the sampled predictions. A second seeded history with 50,988 reviews on 3,500 cards also produced identical digests with one, eight and ten workers in every model size. Regression tests also compare individual projection outputs and recurrent matrix elements against the previous arithmetic and the low-rank codec against its frozen reference. [Projection and recurrence tests](../rslib/src/rwkv/engine.rs) · [Codec tests](../rslib/src/rwkv/lowrank.rs)

## Earlier codec batch measurements

On a 10-core Apple M1 Max, one card per low-rank solve gave the fastest median full rebuild for Big, Small and Tiny. Grouping four cards, as the AVX2 path does, lowered replay throughput by 4.9% to 7.3%. The macOS runtime now groups two cards only for Small and Tiny in a single-worker replay pool, where long-history replay gained 2.9% and 4.0% respectively.

### Full rebuild

Median reviews per second during replay with all 10 threads and the shipped model pairs. The first three columns use the shipped low-rank format (`compact_lowrank`, int6 factors for Big and Small, int8 for Tiny). Percentages compare throughput with one card per solve. The last column uses compact int8 rows as a separate format comparison.

| Model pair | 1 card | 2 cards | 4 cards | Int8 rows |
|---|---:|---:|---:|---:|
| Big | 11,550 | 10,978 (−5.0%) | 10,983 (−4.9%) | 10,784 |
| Small | 39,229 | 35,352 (−9.9%) | 36,785 (−6.2%) | 40,041 |
| Tiny | 64,691 | 60,087 (−7.1%) | 59,944 (−7.3%) | 72,075 |

Including model loading, replay, and the full and delta checkpoints, one-card rebuilds took **18.07 s** for Big, **5.40 s** for Small and **3.33 s** for Tiny. Four-card batches were slower than one card in all nine same-pass rebuild comparisons. Two-card results varied more, especially on Tiny.

### Long card histories

Median reviews per second for a standalone model on one thread. Each of 1,000 cards received 24 reviews, taking every card through the switch to rank 4 at 16 answers.

| Model | 1 card | 2 cards | 4 cards | Int8 rows |
|---|---:|---:|---:|---:|
| Big | 3,774 | 3,762 (−0.3%) | 3,639 (−3.6%) | 3,920 |
| Small | 14,520 | 14,770 (+1.7%) | 14,118 (−2.8%) | 18,329 |
| Tiny | 25,817 | 26,766 (+3.7%) | 24,596 (−4.7%) | 36,050 |

### Two-card batches on Small and Tiny

Two-card batches beat one card in all three single-thread long-history passes on both sizes. For a full mixed-history rebuild limited to one thread, throughput went from 7,030 to 7,056 reviews/s on Small and from 12,721 to 13,070 on Tiny. The difference varied between passes. With ten threads on the long histories, each size also had passes favoring each width.

The isolated codec benchmark explains the model-size difference. In 30 alternating blocks timed with the macOS thread CPU clock, pairing writes with 16-value heads saved 8–10% CPU time per layer write at rank 2 and 12–13% at rank 4, across int6 and int8 factors. For Big’s 32-value heads, the two-card cost was within 1% at rank 2 and about 5% lower at rank 4. Four-card batches took more CPU time per write in all eight cases.

### Verification on the final implementation

The table compares release builds of `517dc4473` with and without the macOS dispatch change. Before and after runs alternated order for each model. Values are median reviews per second across five long-history passes and three full rebuilds.

| Workload | Small, before → after | Tiny, before → after |
|---|---:|---:|
| Long histories, 1 worker | 14,630 → 15,047 (+2.9%) | 27,090 → 28,170 (+4.0%) |
| Full rebuild, 1 worker | 7,175 → 7,238 (+0.9%) | 13,195 → 13,329 (+1.0%) |
| Full rebuild, 10 workers | 39,832 → 39,970 | 66,484 → 67,923 |

Both builds used one card per solve in the ten-worker control. Tiny’s individual ten-worker comparisons varied by up to 14.9%. In the single-worker long-history test, the final build was faster in four of five Small comparisons and all five Tiny comparisons. Card and note byte counts and prediction checksums matched across all before and after runs of that test.

### Measurement and correctness

- Built native `aarch64-apple-darwin` release binaries with Rust 1.97.1 on an M1 Max with eight performance cores and two efficiency cores. The baseline was `42ab96f1f`, which has the engine and weights from `6cb164606`. The two experimental builds grouped the replay streams and compiled the solver for the matching width, eight heads for two cards and sixteen heads for four.
- Used the SM163 synthetic history of 205,785 reviews on 14,000 cards over 730 days. The full rebuild replays up to 16,384 reviews per chunk and writes both checkpoints to a temporary store. The long-history benchmark uses the same model weights and takes the median of three replay timings after one warm-up. [SM163 rebuild harness](https://github.com/JSchoreels/anki-rwkv/blob/97128eb8cf14f5ddcc21e45a82d7926bc11f92a8/artifacts/small-model/sm163/sm163_speed.rs) · [Long-history benchmark](../rslib/src/rwkv/model_size_bench.rs)
- Repeated all four configurations in three different orders after other builds and tests had finished. The full-rebuild and long-history width comparisons give the median of those three passes. Idle CPU sampled just before each run had a median of 81% and ranged from 44% to 87%. Earlier trials under competing load were excluded.
- Both experimental builds passed 24 codec and replay checks covering both engine head sizes, int6 and int8 factors, byte-for-byte agreement with the frozen codec, reconstructed matrices, partial batches and replay across chunk boundaries. Across all three low-rank widths, each long-history workload also produced the same card and note byte counts and prediction checksum. The complete Rust library suite passed with one Rayon worker in both primary-engine configurations (1,607 tests in each). [Codec tests](../rslib/src/rwkv/lowrank.rs) · [Replay tests](../rslib/src/rwkv/precision_tests.rs)
- The one-card build already vectorizes across a card’s four heads. ARM64 disassembly of the encoder and Jacobi solver showed 128-bit NEON vector arithmetic with separate double-precision multiply and add instructions. The inspected functions contained no fused multiply-add operations. [Original SM168 handoff and PC measurements](https://github.com/JSchoreels/anki/commit/42ab96f1ff50aeb29bfdaec9793df4189b167d95)
