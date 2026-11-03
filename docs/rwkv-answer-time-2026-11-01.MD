# Answer-time head for the small model, leak-free, cool-down from epoch 25 (SM72) — 2026-11-01

The default small model (key `sm72e25`) now ships with an optional **answer-time head**: the answer-time head on richer frozen features (SM82c). It predicts how long the next answer will take, separately for Again, Hard, Good and Easy, and Card Info shows the result. Scheduling is unchanged: no interval, queue order, desired-retention rule, simulator or statistic reads the prediction.

## What ships

| File (`qt/aqt/rwkv_inference/sm72e25/`) | Bytes | SHA-256 |
| --- | ---: | --- |
| `time_head.f32.bin` (bundled into the desktop build) | 276,402 | `e958d749b94a64c4fed9a39b3dbff32c52144839956657857188eb13258d223b` |
| `time_head.int8.bin` (tests only) | 70,586 | `7c4e123819b8a05f7dc30ed4da566a200f2ad616fffe57b98335f6224c64244f` |
| `time_head.json` (specification, bundled) | 3,686 | `c5c7e2069911aae36a92cb76f5a65a43c9cddc262631cb483ff2be2b5c7dbb87` |

`weights.f32.bin` (SHA-256 `42b79e35…f25cf261`), `weights.int8.bin`, `architecture.json` and `manifest.json` are **unchanged, byte for byte**. The head is a separate file on purpose: the desktop's RWKV state cache is keyed by the model file's SHA-256, so a changed weights file would make every profile rebuild its state; with a separate file, cached state stays valid and every grade output is unchanged by construction (same file, same code path). The files are byte-identical copies of the anki-rwkv package `artifacts/models/sm72e25-time` (branch `sm83`).

**Format.** `time_head.f32.bin` uses the existing `ARWKVWEIGHTS1` container with five tensors: `time_mlps.0.hidden.weight` [128, 474], `time_mlps.0.hidden.bias` [128], `time_mlps.0.out.weight` [64, 128], `time_mlps.0.out.bias` [64] and `time_head.format_version` [1] = 1. The runtime reads only format version 1 and rejects others. A model folder without the file loads as before and reports no head (backward compatible); older desktops ignore the file (forward compatible). The head has 69,056 parameters; the w8 export of model plus head would be 480,859 weight bytes against 410,459 for the model alone. Cached state is unchanged.

**Head.** Linear 474 → 128, ReLU, Linear 128 → 64 = 4 grades × 16 log-time bins (bin 0 under 0.5 s, geometric bins up to 60 s, bin 15 = 60 s or more). Inputs at the query:

- 448 frozen-model values: the residual stream (width 64) after the input encoder and after each of the four interleaved layer rounds, each layer-normalised without affine parameters, and the Instant head's 128-wide hidden layer. `SrsModel::answer_time_inputs` runs the same trunk as the grade path; a test checks that its button probabilities equal the grade path's bit for bit.
- 26 causal history features (`rslib/src/rwkv/answer_time.rs`, `AnswerTimeHistory`): the card's last three recorded ln answer times with "none" flags, ln(1 + card review count), the card's last grade, the collection's running median ln time per grade (exact median; fallback the median over all grades, then 8 s) with ln(1 + count) per grade, the median over all grades, the previous recorded ln time with a flag, an exponential average (0.1) of recorded ln times, ln(1 + elapsed days) with a new-card flag, and ln(1 + review index). ln times are centred on ln 8 s. Only reviews with a recorded time enter the time statistics.

Predictions exist only for queries on a later day than the card's previous review (elapsed days ≥ 1): in training, a same-day or first review's interval input holds its own answer time, so the head was neither trained nor scored on them.

## Upstream quality evidence

From the decision of the answer-time head on richer frozen features (SM82c), anki-rwkv. Training: one pass over 80,988,171 labelled reviews of 1,991 users (first 400,000 reviews each), head only, every other weight frozen bit for bit. Evaluation: 722 users of the 732-user set, 21,585,938 test reviews with a recorded time on a later day than the card's previous review. Lower is better; brackets are 95% review-weighted paired user-bootstrap intervals.

| Predictor | Censored 16-bin time LogLoss | Mean abs ln error of the median |
| --- | ---: | ---: |
| This head | 1.9568 | 0.4677 |
| The card's last answer time | 2.4466 | 0.7222 |
| The collection's running median per grade | 2.3664 | 0.6428 |

Against the running median per grade: −0.4096 [−0.4388, −0.3842] LogLoss (−17.3%) and −0.1751 [−0.1899, −0.1613] abs ln error (−27.2%). Against the card's last time: −20.0% / −35.2%. A head trained jointly with the model reached 1.9300 / 0.4625; the frozen head is +1.4% / +1.1% behind it but leaves the grade predictions untouched. Grade outputs on five evaluation users were bitwise equal between the released model and the model with the head, and equal to the recorded release scores (Curve / Instant LogLoss 0.30003 / 0.27654 on the 732-user set, unchanged). Predicted vs actual share of answers under 3.9 s: Again 0.146 / 0.140, Hard 0.211 / 0.214, Good 0.324 / 0.330, Easy 0.389 / 0.399.

## Native parity

`rwkv_sm72e25_answer_time_head_matches_published_vectors` replays the published vectors of users 4676 and 1745 through the native model and, at every labelled query, compares the head's per-grade bin probabilities with the PyTorch reference (the frozen-feature path the head was trained and scored on) using the reference's own history features. `rwkv_sm72e25_answer_time_history_matches_published_features` recomputes the 26 history features from the users' revlogs.

| Vectors | Queries | f32 weights, max abs bin probability error | INT8 weights | History features, max abs error |
| --- | ---: | ---: | ---: | ---: |
| Full package vectors (`ANKI_RWKV_SM72E25_TIME_VECTORS`) | 6,543 | 1.0e−6 | 8.3e−7 | 0 |
| Bundled fixture, first 500 rows per user | 407 | 4.4e−7 | 6.2e−7 | 0 |

The tolerance is the existing 1e−5 (1e−6 for the history features). Max abs logit error: 1.8e−5 (full, both formats). The fixture `rslib/src/rwkv/testdata/sm72e25-time-vectors.json.gz` is made with `python qt/tools/rwkv_sm40_vectors.py <package> <output> --limit 500 --time-only` and reuses the rows of `sm72e25-vectors.json.gz`; the full vectors use `--limit 0` without `--time-only` and are not committed. The package's own NumPy checker reproduces its PyTorch logits within 5.7e−6 (probabilities 3.9e−7).

## Where it is computed and shown

- Native runtime: `RwkvInference::predict_answer_times(requests, history)` (`rslib/src/rwkv/engine.rs`), dispatched by `RwkvRuntime`; the bridge exposes `has_answer_time_head()` and `predict_answer_times(requests, history)`, which returns per query the median seconds per grade, P(under 3.9 s) per grade, the grade-mixed median and the Instant head's grade probabilities.
- History features: the collection RPC `RwkvAnswerTimeHistoryFeatures` visits the canonical RWKV history once (the traversal the full warm-up uses) and returns the features of the requested cards; the desktop caches the result until the review log or the ignored reviews change.
- Card Info (`qt/aqt/rwkv_scheduler.py`, `qt/aqt/rwkv_answer_time.py`): **RWKV : Answer Time by Button** and **RWKV : Expected Answer Time** after the RWKV retrievability rows, only when the head is loaded and the card's previous review was on an earlier day.

## Verification

- Rust, standalone Cargo harness on Windows x86-64 (GNU toolchain; the module's sources compiled in place with stand-ins for the protobuf and crate items it imports, the system SQLite, and an uncompressed stand-in for zstd, used only by a compression benchmark): with default features 214 passed, 4 failed, 30 ignored; without default features 214 passed, 4 failed, 29 ignored. The four failures are the bit-exactness checks of the experimental fused packed INT4 kernels (`fused_packed::tests`, in both engines), the same four that the [cool-down from epoch 25 integration report](rwkv-sm72e25-2026-10-30.MD#verification) lists as failing before it; this change does not touch them, and they are test-only benchmark code. New: the two vector tests above, `rwkv_runtime_answer_time_head_is_optional_and_leaves_grades_unchanged` (a copy of the weights without the head file loads without a head, predicts no answer time, and gives bitwise the same `predict_many` outputs; a later-day query gets a normalised prediction and a same-day query none; Legacy has no head) and two unit tests of the feature rule and the median.
- Not run on this machine (no protoc, ninja, C compiler, PyQt, pytest or Node): the collection RPC test `answer_time_history_features_follow_the_canonical_history`, the bridge test `test_rsbridge_rwkv_answer_time_head_is_optional`, `qt/tests/test_rwkv_answer_time.py` (its pure formatting tests were run directly with Python) and the type and format checks; the remote CI runs them.

## Not done

- The workload simulator and the RWKV/FSRS comparison keep their fixed per-grade answer times; using per-card predicted times there would change their results and is left for a later release.
- No own-collection evaluation of the answer-time head.
