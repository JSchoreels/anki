# Handoff: RWKV model-size speed test (branch `speed-test`)

State on 2026-11-12: harness built and calibrated (contended, GPU training running); the final timing run is **not done yet**. Background, calibration numbers and the plan: `docs/rwkv-speed-test-plan.MD`.

## Before the run

1. Quiet window: GPU queue idle (no training, scoring or export), no other CPU work, probably Friday after 16:00.
2. Worktree: `C:/Users/jonat/workspace/wt/speed-test` (branch `speed-test` of the fork). `git fetch && git merge --ff-only origin/speed-test`.
3. Collection copy: `.local/collection/collection.anki2` (git-ignored, from 2026-11-03). To refresh it, make a plain file copy of `C:/Users/jonat/AppData/Roaming/Anki2/User 1/collection.anki2` (plus `-wal` if present) into `.local/collection/`. Never open, lock or run anything against the AppData folder. Report only aggregate numbers.
4. Updated 2026-11-14 (family wiring (SM114) merged): runtimes follow the per-size models. `tiny-pair` = Tiny two-head model (SM106) + Tiny Instant-Only model (SM101), `tiny-one` = SM106 alone, `tiny-instant` = SM101 alone; Big = show-time big model (SM91s2) + show-time instant-only big model (SM99); Small = show-time retrain (SM92) + Small Instant-Only model (SM98). Extra names: `tiny-curve`, `small-curve` (same as the one-model runs), `sm63-alone` (default small model (SM63), comparison only). Smoke 2026-11-14 passed (tiny pair 2,129 B per card + 3,153 B per note = 5,282 B).

## The run (one command, about 25 minutes)

```powershell
cd C:/Users/jonat/workspace/wt/speed-test
powershell -NoProfile -ExecutionPolicy Bypass -File qt/tools/rwkv_speed_test.ps1 `
  -Collection C:/Users/jonat/workspace/wt/speed-test/.local/collection/collection.anki2 `
  -OutDir C:/Users/jonat/workspace/wt/speed-test/.local/speed-final
```

The default runtime list is already: big-pair, big-one, big-instant, small-pair, small-one, small-instant, tiny-pair, tiny-one, tiny-instant (no custom `@path` needed). Final-run command (after the GPU training ends, quiet machine):

```powershell
cd C:/Users/jonat/workspace/wt/speed-test
powershell -NoProfile -ExecutionPolicy Bypass -File qt/tools/rwkv_speed_test.ps1 `
  -Collection C:/Users/jonat/workspace/wt/speed-test/.local/collection/collection.anki2 `
  -OutDir C:/Users/jonat/workspace/wt/speed-test/.local/speed-final `
  -Runtimes big-pair,big-one,big-instant,small-pair,small-one,small-instant,tiny-pair,tiny-one,tiny-instant
```

The script builds the release test binary (`PROTOC` from `wt/anki-tiny/out/extracted`), copies the collection into the output folder, refuses to start if the GPU is above 5% or the CPU load above 10% (`-AllowBusy` overrides, for smoke tests only), and writes:

- `.local/speed-final/results.jsonl`: one JSON line per run;
- `.local/speed-final/summary.md`: medians per runtime × operation × cold/warm, peak RAM, store size, app estimate (build + Python side), state bytes per card and note;
- `.local/speed-final/run.log`: CPU load before and after, GPU state, every step.

Re-summarize any time: `python qt/tools/rwkv_speed_summary.py .local/speed-final/results.jsonl`.

## Single runs (debugging)

```bash
cd C:/Users/jonat/workspace/wt/speed-test
export PATH="$HOME/.cargo/bin:$PATH" PROTOC=C:/Users/jonat/workspace/wt/anki-tiny/out/extracted/protoc/bin/protoc.exe
cargo test --release -p anki --lib --no-run      # prints target/release/deps/anki-<hash>.exe
# the harness opens its collection read-write: give it a working copy, never the master copy
mkdir -p .local/cal && cp .local/collection/collection.anki2 .local/cal/
ANKI_SPEED_COLLECTION=$PWD/.local/cal/collection.anki2 ANKI_SPEED_RUNTIMES=tiny-pair \
ANKI_SPEED_OPS=build,score ANKI_SPEED_ITERATIONS=1 \
  target/release/deps/anki-<hash>.exe rwkv_app_speed_benchmark --ignored --nocapture --test-threads 1
```

Notes: use `~/.cargo/bin` (the MSVC 1.97.1 toolchain of `rust-toolchain.toml`); the `Program Files` GNU cargo fails on `dlltool`. The worktree's `ftl/core-repo` and `ftl/qt-repo` submodules were filled from the main checkout (`git -c protocol.file.allow=always -c submodule.ftl/core-repo.url=C:/Users/jonat/workspace/anki/ftl/core-repo ... submodule update --init`); a fresh worktree needs the same.

## After the run

Write the results into `docs/rwkv-speed-test-plan.MD` (or a dated `docs/rwkv-speed-test-<date>.MD`): the build table per size and mode (median s, app estimate, replay reviews/s, peak RAM, state bytes per card + note, store size), the load and score tables, CPU load during the run. Every model as "plain name (ID)". Commit as Jonathan Schoreels on `speed-test`, fetch + merge `origin/speed-test`, push that branch only.
