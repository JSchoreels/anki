# RWKV state building on the Mac: find the same speed-up (to do)

Written 2026-11-25 on the Windows PC, for the Mac. Fork main `6cb164606` and later.

## What the PC got, and why the Mac doesn't have it yet

The batched low-rank codec (SM168, merged in `86072018a`) encodes the card and note states of up to four cards in
one solve: the heads of four layer writes are the lanes of one set of small Jacobi eigen problems, stored lane by
lane (`a[i][j][lane]` in `rslib/src/rwkv/lowrank.rs`), so the rotations of all lanes run side by side in SIMD
registers. On x86-64 the solve is also compiled for AVX2 / AVX-512 and picked at run time.

Measured on the PC (quiet machine, median of 3, alternating order; anki-rwkv
`artifacts/small-model/sm168/RESULT.md`):

| Size | One-thread long-history replay, before -> after | Full rebuild, before -> after |
|---|---|---|
| Big | 4,940 -> 5,589 reviews/s (+13.1%) | 14,530 -> 15,320 reviews/s (+5.4%) |
| Small | 17,225 -> 20,475 reviews/s (+18.9%) | 47,227 -> 49,121 reviews/s (+4.0%) |
| Tiny | 30,165 -> 37,793 reviews/s (+25.3%) | 82,844 -> 86,959 reviews/s (+5.0%) |

**The Mac does not use the batching.** `lowrank::batch_writes()` returns 1 unless the CPU has 256-bit vectors
(AVX2 or AVX-512), so Apple Silicon (128-bit NEON) keeps one card per solve. On the PC, capping the codec at
128-bit vectors made more lanes only add work (codec benchmark at baseline width: -9% to +2% time per write vs
before). That was an x86 baseline build, not a Mac: **nobody has measured it on Apple Silicon.**

## What to try on the Mac

1. **Measure first.** Build release and run the speed harness of the clean re-measure (SM163):
   `artifacts/small-model/sm163/run.sh` and `sm163_speed.rs` in the anki-rwkv repo (one-thread replay with
   `RAYON_NUM_THREADS=1`, full rebuild with all threads, codec micro-benchmark). Record before numbers on fork main.
2. **Try batching on aarch64.** Let `batch_writes()` return 2 and 4 on `target_arch = "aarch64"` and benchmark
   each. Apple cores have several NEON pipes, so independent lanes can pay off through instruction-level
   parallelism even with 128-bit registers (2 f64 or 4 f32 per register), unlike the x86 baseline the PC tried.
3. **Check what the compiler emits.** Look at `encode_batch_impl` in the aarch64 assembly (`cargo asm` or
   `objdump`): the lane loops should become NEON `fmla`/`fmul` on vectors. If they stay scalar, make the lane loop
   shape vectorizer-friendly (fixed `L`, no early exits; the non-rotating lane already uses a select).
4. **Keep the bytes identical.** The batched-vs-single test (`encode_layers` at 1, 2 and 4 cards per solve, both head
   sizes, int8 and int6 factors) and the frozen-codec tests must pass unchanged. No fused multiply-add where the
   reference does a separate multiply and add, and no reordering of sums: NEON `fmla` is fused, so check that the
   compiler is not contracting `a * b + c` (Rust does not contract by default; keep it that way).
5. **No new dependencies** (no Accelerate / BLAS): the gain must come from our own code.

## Done when

- One-thread replay and full rebuild on the Mac are measured before and after, per size, with the default format
  (`compact_lowrank`: Big/Small int6 factors, Tiny int8).
- If a batch width helps on aarch64: same bytes (tests above), both `cargo test -p anki --lib` suites pass, merged
  into fork main with the numbers in RELEASE.md. If none helps: write the numbers here and close it.
