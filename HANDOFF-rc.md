# Handoff: fork release candidate `rc-2026-11`

Date: Sat 2026-11-21. Worktree `C:/Users/jonat/workspace/wt/fork-rc`, branch `rc-2026-11` (pushed to origin; NOT
merged into fork `main`; no release build, tag, GitHub release or PR). Report: `docs/rwkv-rc-2026-11.MD`.

## What is in the branch

On top of fork `main` 4fe4ae9c8:

1. `rc-state-int8` (d6da559b3, 2dc21da67): compact int8 card rows + compact notes (`compact_cards_int8`) default for
   every size, cache version 14, no state-decode panics, `WEIGHTS-NOTICE.md`.
2. Merge of `rwkv-time-heads-family` (tip 1e3d91b2d): answer-time heads for the family (SM117, SM119), SM113 / SM115.
   Conflicts resolved in `RELEASE.md`, `legacy_tests.rs`, `runtime.rs`, `testdata/sm92-time-vectors.json.gz` (the
   time-heads branch's synthetic fixture for the re-trained SM92 head).
3. The release-candidate models (one commit):
   - Small two-head = the show-time two-head cool-down for Small (SM118), key/folder `sm118` (master `b4fe1175`,
     head from SM119). The show-time retrain (SM92) stays bundled and stays the preserved profile value `sm92`.
   - Tiny = the six-layer Tiny (SM130), two-head `sm130th` (`48a5e724`) + Instant-Only `sm130io` (`23f03728`), heads
     from SM138 (346 inputs). SM106 / SM115 (and SM98 / SM101) stay bundled, unused by any size.
   - Big unchanged (SM91s2 `67ee8e9b` + SM99 `c49e45d6`); Small Instant-Only unchanged (SM113 `e4574e2a`).

Native six-layer support (no change to compiled constants): `SIX_LAYER_PLAN` / `SMALL_LAYER_PLANS` in
`architecture.rs`; `layer_plan_for` reads every stream's layer count from the weights; the answer-time head width
follows the plan (`answer_time_inputs`: 474 / 346 / 1,306) and a head of another width is refused; `ModelKind`
`Sm118`, `Sm130Th`, `Sm130Io` with `module_layers()` (each key refuses weights with other layer counts, so SM106 and
SM130 weights never load under each other's key). Python: `RWKV_SMALL_TWO_HEAD_MODEL = "sm118"`,
`RWKV_TINY_TWO_HEAD_MODEL = "sm130th"`, `RWKV_TINY_INSTANT_MODEL = "sm130io"` (`qt/aqt/rwkv_model.py`). Build: new
folders copied, `WEIGHTS-NOTICE.md` copied into every `_aqt/rwkv_inference/<model>/` folder
(`build/configure/src/aqt.rs`). TS: Deck Options loss and replay estimates for the new Small / Tiny.

Weights were exported in a separate anki-rwkv worktree `C:/Users/jonat/workspace/wt/fork-rc-export` (detached at
SM138's `75266753`): `scripts/small_model_export.py package` + synthetic test vectors +
`artifacts/small-model/fork-rc/sm130_fork_package.py` (manifests) + `artifacts/small-model/fork-rc/time_heads_package.py`
(heads + time fixtures). Full packages (with `master.pth`, `parity.json`, `testvectors/`, `reference/`) are in
`wt/fork-rc-export/artifacts/models/{sm118,sm130th,sm130io}` (untracked there; nothing committed in anki-rwkv). Synthetic
users: `C:/Users/jonat/AppData/Local/Temp/claude/eefeeb6d2df69e2b/41a2947b-8da0-4ee6-a033-bc96eb42a697/scratchpad/syn`
(regenerate with `python scripts/synthetic_testvector_users.py <dir>` in anki-rwkv main).

## Test results (Windows, cargo 1.99, -j 8, 8 test threads)

- `cargo test -p anki --lib rwkv::` (default features): 402 passed, 0 failed, 35 ignored (386 after the merge
  alone; +16 new tests).
- `cargo test -p anki --lib --no-default-features rwkv::`: 402 passed, 0 failed, 34 ignored.
- `cargo clippy -p anki --lib --tests` (both feature sets), `cargo clippy -p anki --lib`, `cargo clippy -p
  configure`: clean; `cargo fmt -p anki --check`: clean.
- New tests: the six-layer Tiny models and SM118 match their packages' synthetic vectors (int8 max probability
  error 1.2e-6 / 7.2e-7 / 1.4e-6; tolerance 1e-5), ignore missing-ID flags on every inference path, build show-time
  intervals like the scorer; their answer-time heads (346 / 346 / 474 inputs) reproduce the PyTorch heads on the
  synthetic users (max bin probability error <= 3.5e-7, f32 and int8); `rwkv_answer_time_head_width_follows_the_layer_plan`
  (474 / 346 per plan, a head of the other width refused, a four-round head beside six-layer weights fails the
  load); the stored sizes (six-layer Tiny = Tiny: 1,315 B per card and per note at `compact_cards_int8`; pair
  5,298 B); the layer plan read from the weights and the architecture.json specs; every runtime test with the new
  keys (pairs bit-identical to each model alone at all five precisions, restore at each precision, leak-free
  inputs, cross-key weight refusal, layout switching never reuses state). The precision tests (bulk vs sequential
  bit identity, queries/undo, checkpoints/spills) now run the six-layer plan too.
- `rwkv_long_card_full` (release, 20,000 answers): 10 models (the family + SM118, SM113, both SM130) x 3 patterns
  x 2 compact policies x 2 paths = 120 runs, all finite, no failure; compact int8 cards never saturate.
- `rwkv_model_size_benchmark` (release, 4 threads, 32,000 synthetic answers, 4,000 cards, compact int8, machine
  shared with a CPU job): state replay Big pair 6,184 ms, Small pair (SM118 + SM113) 1,340 ms, six-layer Tiny pair
  705-724 ms vs the earlier Tiny pair (SM106 + SM115) 1,187 ms (-39%); alone 405-424 ms vs 663 ms (-36%); Big alone
  3,074 ms. Deck Options' Tiny replay savings vs Big become 88% (pair) / 87% (one model).

## What remains

- **Mac (Python / TS, not runnable here: no `just` build).** Edited Python compiles and passes `ruff check` +
  `ruff format --check`. Run:
  ```sh
  git fetch && git switch rc-2026-11
  just build
  just test-py-target qt/tests/test_rwkv_model.py qt/tests/test_rwkv_model_switch.py \
    qt/tests/test_rwkv_inference_process.py qt/tests/test_rwkv_precision.py qt/tests/test_rwkv_scheduler.py \
    qt/tests/test_rwkv_srs_benchmark.py qt/tests/test_rwkv_answer_time.py
  just test-ts            # rwkv-model-estimates.test.ts (new Small / Tiny losses and Tiny replay savings)
  just check-local        # then just check
  ```
  Things to look at: `test_rwkv_model_switch.py` expected Tiny's models were stale before this branch (they still
  named SM106 + SM101); they now name SM130. The deck-options e2e test's Small figures (7.8 KB, 6.4 MB) are unchanged
  (same state sizes).
- Optional app-level check on a collection copy: `just rwkv-app-speed-bench` (runtimes `tiny-pair` = SM130 pair,
  `tiny4-pair` = the earlier SM106 + SM115 pair).
- Packaging for the release (Hugging Face / release packages of SM118 and the two SM130 models, family README):
  not done; the packages in `wt/fork-rc-export/artifacts/models/` are ready inputs (manifests neutral, synthetic
  vectors, parity PASS).
- Release build / tag / CI matrix: only after the user's go.
