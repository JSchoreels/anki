# Handoff: answer-time heads for the longer Instant-Only models (SM113, SM115)

Branch `rwkv-time-heads-family` (not merged into main; no release build without the user's OK).
Done: heads for the show-time big model (SM91s2), the show-time instant-only big model (SM99), the show-time retrain (SM92), the Tiny two-head model (SM106); SM113/SM115 packaged and wired (Small/Tiny Instant-Only). Report: docs/rwkv-time-heads-2026-11-16.MD.

Remaining, after the time heads for the new candidates (SM119, log wt/sm119/artifacts/small-model/sm119/gpu/launch-sm119.log) end:

1. In C:/Users/jonat/workspace/wt/time-heads-package (PY=C:/Users/jonat/workspace/anki-rwkv/.venv/Scripts/python.exe, SYN = synthetic data folder; regenerate with `$PY -X utf8 scripts/synthetic_testvector_users.py $SYN`), CPU, ~15 s each. Confirm the SM119 head folder names against launch-sm119.sh first:
   $PY -X utf8 artifacts/small-model/time-heads-package/time_heads_package.py sm113 --data $SYN --head-dir C:/Users/jonat/workspace/wt/sm119/artifacts/small-model/sm119-sm113-head --master C:/Users/jonat/workspace/wt/sm113/artifacts/small-model/sm113-inst-e35st-st-dec2ep32k-t99-p2/expertium/E89-F65e-F77a/master.pth --vectors artifacts/models/sm113/testvectors
   $PY -X utf8 artifacts/small-model/time-heads-package/time_heads_package.py sm115 --data $SYN --head-dir C:/Users/jonat/workspace/wt/sm119/artifacts/small-model/sm119-sm115-head --master C:/Users/jonat/workspace/wt/sm115/artifacts/small-model/sm115-c1-e38st-st-dec2ep32k-t99-p2/expertium/E89-F65e-F77a/master.pth --vectors artifacts/models/sm115/testvectors
   Reword the time_head.json "model" text to name SM119.
2. Fork edits: add "sm113", "sm115" to the head loop in build/configure/src/aqt.rs; in rslib/src/rwkv/sm40_tests.rs add sm113_/sm115_answer_time_vectors + two tests copied from SM106's; update the "no head" assertions for SM113/SM115 in runtime.rs and qt/tests/test_rwkv_inference_process.py (family pair test); update "head pending" wording in docs/rwkv.MD, docs/rwkv-time-heads-2026-11-16.MD, RELEASE.md.
3. Rerun `cargo test -j 4 -p anki --lib rwkv::` (MSVC toolchain in ~/.cargo/bin, PROTOC from wt/anki-tiny/out/extracted, CARGO_TARGET_DIR=wt/anki-tiny/target) and the four qt/tests rwkv python files; commit, fetch + merge origin/main, push the branch.
4. If the show-time two-head cool-down for Small (SM118) is a go and SM119 trained its head: swapping Small's two-head model is a separate decision for the user.

Open issue for the user: rslib/src/rwkv/testdata/sm72e25-time-vectors.json.gz holds a real user's revlog (sm92's did until this branch) in the public fork history.
