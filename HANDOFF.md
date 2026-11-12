# Handoff: RWKV model-size speed test (branch `speed-test`)

State on 2026-11-12: harness built and calibrated (contended, GPU training running); the final timing run is **not done yet**. Background, calibration numbers and the plan: `docs/rwkv-speed-test-plan.MD`.

## Before the run

1. Quiet window: GPU queue idle (no training, scoring or export), no other CPU work, probably Friday after 16:00.
2. Worktree: `C:/Users/jonat/workspace/wt/speed-test` (branch `speed-test` of the fork). `git fetch && git merge --ff-only origin/speed-test`.
3. Collection copy: `.local/collection/collection.anki2` (git-ignored, from 2026-11-03). To refresh it, make a plain file copy of `C:/Users/jonat/AppData/Roaming/Anki2/User 1/collection.anki2` (plus `-wal` if present) into `.local/collection/`. Never open, lock or run anything against the AppData folder. Report only aggregate numbers.
4. Optional, once the Tiny two-head model (SM106) is in the fork with a `ModelKind` (or as a path to its weights): add it as a custom runtime, see below.

## The run (one command, about 25 minutes)

```powershell
cd C:/Users/jonat/workspace/wt/speed-test
powershell -NoProfile -ExecutionPolicy Bypass -File qt/tools/rwkv_speed_test.ps1 `
  -Collection C:/Users/jonat/workspace/wt/speed-test/.local/collection/collection.anki2 `
  -OutDir C:/Users/jonat/workspace/wt/speed-test/.local/speed-final
```

With the Tiny two-head model (SM106) as Tiny's one-model choice (kind key as the fork names it; `@path` only while the weights are not bundled):

```powershell
  -Runtimes big-pair,big-one,big-instant,small-pair,small-one,small-curve,small-instant,tiny-pair,tiny-instant,"tiny-one=sm106@C:/path/to/sm106/weights.f32.bin"
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
