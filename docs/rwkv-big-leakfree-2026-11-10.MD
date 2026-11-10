# Legacy - Bigger Model is the big model (1 leak-free epoch): integration — 2026-11-10

The fork's **Legacy - Bigger Model** (profile key `legacy`) now ships the big model (1 leak-free epoch) instead of the original big model (fork RWKV). Same 2,762,884-parameter architecture and file format; the weights are the original big model trained one more epoch on leak-free inputs, where a deleted card's note, deck and preset flags (inputs 13-15) are never set. The original big model was trained with the deleted-card signal in its inputs, a leak the small models already lost with the leak-free small model (SM72). Requested by the user on 2026-11-10 ("the big model used by the fork should be the one that is leak free").

## Accuracy (upstream)

anki-rwkv worktree `sm72`, `artifacts/small-model/sm72/gpu-big/report-732.md`: 722 users of the 732-user held-out set, leak-free scoring (deleted cards: neighbour), INT8 weights and INT8 card / note state (w8+s8), review-weighted LogLoss, paired user bootstrap (2,000 resamples, seed 0). The % are of the big model (1 leak-free epoch)'s normal-input LogLoss of the same head; negative means the new weights are better.

| Head and inputs | The big model (1 leak-free epoch) | The original big model (fork RWKV) | Change |
| --- | --- | --- | --- |
| Curve, normal inputs | 0.29897 | 0.29966 | **−0.23%** |
| Instant, normal inputs | 0.27576 | 0.27620 | **−0.16%** |
| Instant, show-time inputs (the times RWKV-Instant gives the model) | 0.277157 | 0.277559 | **−0.15%** |

The report's 95% intervals exclude zero for both heads on normal inputs. This is the model behind the "big model (1 leak-free epoch)" reference that every small-model result is measured against.

## What changed

- **Weights:** `qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin` (ARWKVWEIGHTS1, f32, 504 tensors, 11,082,637 bytes, SHA-256 `0e07004d…`, was `749ad1ad…`) is the leak-free epoch's master weights (`sm72-big-leakfree-1ep`, `master.pth` SHA-256 `ac7f0d8a…`), exported on CPU with the same writer as the original. Re-exporting the original `RWKV_trained_on_5000_10000.pth` with it reproduces the original `.bin` byte for byte, so the format is unchanged. The PyTorch checkpoint next to it, `RWKV_trained_on_5000_10000.pth` (used only by developer tools such as the head fine-tune probe; not shipped by the build), is now the same master weights.
- **The old file:** replaced, not kept. No saved setting, build step or test needs the original weights: the profile key stays `legacy`, and the file name is kept because the build copy step, tools, tests and custom paths name it. The original weights stay in git history and in anki-rwkv.
- **Feature path:** `ModelKind::Legacy.leak_free_inputs()` is now true, so the runtime forces inputs 13-15 to zero on every row for Legacy too, in the per-review, query, batched-query and bulk-replay paths, as for the leak-free small models. The leak-input rule now applies only to the earlier small models (SM40, SM36, SM55, SM56). `RwkvRuntime::load_paired` still rejects Legacy on either side: it is not a small model.
- **States rebuild once:** the state cache's model key carries the weights file's SHA-256, which changed, so a Legacy profile rebuilds its RWKV state from the retained review history once at the first open. The fallback that matches older cache keys by file name and size only applies to `weights.f32.bin`, so it cannot reuse an original-weights state.
- **UI text:** the bigger-model description in Deck Options names the leak-free epoch.

## Verification

- Training code against the fork runtime: test vectors from the trainer's PyTorch model (normal inputs, deleted cards: neighbour, full histories of two users, 14,403 rows: instant recall, the four button probabilities, curve recall at the row's elapsed time and at six sweep times, from the raw forgetting curve, since the runtime ignores the big model's "ahead" logit residual as before), run through the fork's big engine with the leak-free rule: maximum absolute error 1.6e−6 on all 14,403 rows (bundled fixture: 9.5e−7). The same check on the original weights, with vectors made the same way, gives 1.3e−6, so the method reproduces the shipped model too.
- New tests (`rslib/src/rwkv/legacy_tests.rs`, compiled into whichever engine runs the big architecture): `rwkv_legacy_matches_published_vectors` (bundled fixture `rslib/src/rwkv/testdata/legacy-vectors.json.gz`, first 500 rows per user; `ANKI_RWKV_LEGACY_VECTORS` points it at the full vectors) and `rwkv_legacy_ignores_missing_id_flags`. `rwkv_runtime_feeds_leak_free_inputs_to_leak_free_models` (renamed from `rwkv_runtime_feeds_only_sm72_leak_free_inputs`) now covers Legacy: the runtime's predictions differ from the raw weights' only for histories with missing identities.
