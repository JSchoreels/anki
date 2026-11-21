# Answer-time heads for every size, and the longer Instant-Only models — 2026-11-16

The user's request of 2026-11-16: "embed the new models in anki so I can have the answer time head for all models and the tiny instant only too". Two changes ship together:

1. **Every size has an answer-time estimate.** The heads of the time heads for the family (SM117) are bundled for the two-head model of every size (the show-time big model (SM91s2), the show-time retrain (SM92), the Tiny two-head model (SM106)) and for the show-time instant-only big model (SM99). The show-time retrain (SM92)'s earlier head (the answer-time head release (SM83)) is replaced by its head from the time heads for the family (SM117). The native runtime now reads a head on the big engine too (1,306 inputs instead of 474).
2. **Small and Tiny get their longer Instant-Only models.** The longer Small Instant-Only model (SM113) replaces the Small Instant-Only model (SM98), and the longer cool-down for Tiny (SM115) replaces the Tiny Instant-Only model (SM101). Both had a "go" verdict. Their own answer-time heads, trained in the time heads for the new candidates (SM119) run, are bundled, so Small and Tiny answer the time from their Instant-Only model's head (see "Which head answers").

**What the answer time is.** The answer time is the whole time on the card, front and back together: from the moment the question is shown to the moment a grade button is pressed, capped at the preset's maximum answer seconds (the revlog `time` column). The moment "Show answer" is pressed is not stored, so front-only and back-only times cannot be computed. "Recall within S seconds" in Card Info therefore means recalled (Hard, Good or Easy) **and answered within S s, reading the back included**; it is not a pure recall latency. Scheduling never reads the estimate.

## What each size runs

Changes against the previous build in bold. "Answer time from" names the model whose head runs.

| Presets use | Big (`legacy`) | Small (`sm72e25`, default) | Tiny (`tiny`) |
| --- | --- | --- | --- |
| RWKV-Instant and RWKV-Curve (default) | the show-time big model (SM91s2) beside the show-time instant-only big model (SM99); **answer time from the show-time instant-only big model (SM99)** (was: none) | the show-time retrain (SM92) beside **the longer Small Instant-Only model (SM113)** (was the Small Instant-Only model (SM98)); answer time from the show-time retrain (SM92) until the longer Small Instant-Only model (SM113) has a head | the Tiny two-head model (SM106) beside **the longer cool-down for Tiny (SM115)** (was the Tiny Instant-Only model (SM101)); **answer time from the Tiny two-head model (SM106)** (was: none) until the longer cool-down for Tiny (SM115) has a head |
| both, with the one-model opt-out | the show-time big model (SM91s2) alone; **answer time from it** (was: none) | the show-time retrain (SM92) alone; answer time from it | the Tiny two-head model (SM106) alone; **answer time from it** (was: none) |
| RWKV-Instant without RWKV-Curve (opt-out ignored) | the show-time instant-only big model (SM99) alone; **answer time from it** (was: none) | **the longer Small Instant-Only model (SM113) alone**; no answer time until its head lands | **the longer cool-down for Tiny (SM115) alone**; no answer time until its head lands |
| RWKV-Curve only, or neither | the show-time big model (SM91s2) alone; **answer time from it** | the show-time retrain (SM92) alone; answer time from it | the Tiny two-head model (SM106) alone; **answer time from it** |

The rule of the family wiring (SM114) is unchanged: each size runs only its own two models, the one-model option runs the size's two-head model alone, and RWKV-Instant alone runs the size's Instant-Only model. The Small Instant-Only model (SM98) and the Tiny Instant-Only model (SM101) stay bundled (model kinds `sm98`, `sm101`, as the instant-only small model (SM57) did after it was replaced) but no size runs them; they were never a profile's model choice. Their heads from the time heads for the family (SM117) are not bundled, because nothing runs those models.

**State rebuild.** The replay key names the runtime's models, so every Small and Tiny profile that runs an Instant-Only model (RWKV-Instant alone, or the pair) rebuilds its RWKV state once after the update, as after any model change. Profiles running only a two-head model keep their state: the two-head weights files are unchanged byte for byte (the heads are separate files).

## Which head answers (decision)

Each head of the time heads for the family (SM117) was trained alone on one frozen model and reads that model's trunk (its residual stream and the instant head's hidden layer). The runtime therefore always runs a head on the state of the model it was trained on:

- **One model alone** (a two-head model, or an Instant-Only model alone): its own head, if it has one.
- **A pair:** the **Instant-Only model's head when it has one**, else the two-head model's head. Reason: in a pair, RWKV-Instant's grade probabilities (the Card Info buttons and the recall curve) come from the Instant-Only model, and the head's per-grade time distributions are mixed with the grade probabilities of its own trunk, which is exactly how the time heads for the family (SM117) trained and scored every head (P(grade) from the same model's instant head). Using the two-head model's head beside the Instant-Only model's buttons would mix two models. The Instant-Only model's head is also slightly better on every size (time LogLoss, 722 users, 732 set: the show-time instant-only big model (SM99) 1.9523 vs the show-time big model (SM91s2) 1.9558; the Small Instant-Only model (SM98) 1.9625 vs the show-time retrain (SM92) 1.9716; the Tiny Instant-Only model (SM101) 1.9656 vs the Tiny two-head model (SM106) 1.9751).

Before this change, a pair always used the two-head model's head (only the show-time retrain (SM92) had one) and an Instant-Only model alone never showed an answer time. Today the pair rule picks the show-time instant-only big model (SM99)'s head for Big and the longer Small Instant-Only model (SM113)'s and the longer cool-down for Tiny (SM115)'s own heads for Small and Tiny (since the time heads for the new candidates (SM119); the two-head model's head is used only with an Instant-Only model that has none, the Small Instant-Only model (SM98) and the Tiny Instant-Only model (SM101)). `RwkvRuntime::answer_time_from_instant_model()` reports which head a pair uses.

## Same-day reviews

The heads of the time heads for the family (SM117) were trained on show-time inputs with same-day reviews included (on show-time inputs the query row's interval runs to the moment the card is shown, so it does not hold the review's own answer time; CPU proof C4a of the time heads for the family (SM117)). They answer every query except a card's first review, including cards last reviewed today (learning and relearning cards). They carry `time_head.format_version` = 2. The default small model (SM63, folder `sm72e25`) keeps its format 1 head (normal inputs, later-day reviews only), which still answers only cards last reviewed on an earlier day. The runtime reads both formats.

## What ships

| File (`qt/aqt/rwkv_inference/`) | Bytes | SHA-256 (first 16) | Head of |
| --- | ---: | --- | --- |
| `RWKV_trained_on_5000_10000.time_head.f32.bin` (bundled) | 702,386 | `33d1e3c616a07a13` | the show-time big model (SM91s2) |
| `RWKV_trained_on_5000_10000.time_head.int8.bin` (tests only) | 177,082 | `6d413837e6b8aab0` | the show-time big model (SM91s2) |
| `RWKV_trained_on_5000_10000.time_head.json` (bundled) | 4,416 | `8fa5060c1cf2d54c` | the show-time big model (SM91s2) |
| `sm99/time_head.f32.bin` (bundled) | 702,386 | `8bd4f05b426e2025` | the show-time instant-only big model (SM99) |
| `sm99/time_head.int8.bin` (tests only) | 177,082 | `f61dd07e51d6cc1e` | the show-time instant-only big model (SM99) |
| `sm99/time_head.json` (bundled) | 4,373 | `310ac6c58bd6d2b0` | the show-time instant-only big model (SM99) |
| `sm92/time_head.f32.bin` (bundled, replaced) | 276,402 | `58cbf5af102fa6d7` | the show-time retrain (SM92) |
| `sm92/time_head.int8.bin` (tests only, replaced) | 70,586 | `43dc342bfd80a7d3` | the show-time retrain (SM92) |
| `sm92/time_head.json` (bundled, replaced) | 4,321 | `1eb3060038b3d0f8` | the show-time retrain (SM92) |
| `sm106/time_head.f32.bin` (bundled) | 276,402 | `449a5827829077b8` | the Tiny two-head model (SM106) |
| `sm106/time_head.int8.bin` (tests only) | 70,586 | `4797221b57442f79` | the Tiny two-head model (SM106) |
| `sm106/time_head.json` (bundled) | 4,366 | `1c680eb9200e44b3` | the Tiny two-head model (SM106) |
| `sm113/weights.f32.bin` (bundled), `weights.int8.bin` (tests only), `architecture.json`, `manifest.json` | 1,351,553 / 436,391 | `cc87f080eb5f2540` / `87bce8dbb48a11dd` | the longer Small Instant-Only model (SM113) |
| `sm115/weights.f32.bin` (bundled), `weights.int8.bin` (tests only), `architecture.json`, `manifest.json` | 1,270,749 / 409,483 | `b7c7a10aa271cae7` / `d0b64c636afbfcf3` | the longer cool-down for Tiny (SM115) |

Every model weights file that existed before is unchanged byte for byte. The head files use the `ARWKVWEIGHTS1` (f32) and `ARWKVWEIGHTS8PC1` (int8 per output row) containers with five tensors: `time_mlps.0.hidden.weight` [128, inputs], `time_mlps.0.hidden.bias` [128], `time_mlps.0.out.weight` [64, 128], `time_mlps.0.out.bias` [64], `time_head.format_version` [1] = 2. Before packaging, each head's frozen trunk was checked equal, tensor for tensor and bit for bit, to the fork's shipped weights file (420 tensors for the show-time retrain (SM92), 390 for the Tiny two-head model (SM106), 504 for the show-time instant-only big model (SM99) and for the show-time big model (SM91s2)'s `RWKV_trained_on_5000_10000.bin`). The packaging script is `artifacts/small-model/time-heads-package/time_heads_package.py` on the anki-rwkv branch `time-heads-package`; the packages of the longer Small Instant-Only model (SM113) and the longer cool-down for Tiny (SM115) follow the Tiny Instant-Only model (SM101)'s packaging (`small_model_export.py package`, leak-free inputs, CPU).

**Where the runtime finds a head.** `AnswerTimeHead::load_beside` looks for `<weights file name without .bin>.time_head.f32.bin` beside the weights first (the show-time big model (SM91s2)'s weights file has no folder of its own), then for `time_head.f32.bin` in the folder of the weights (`.int8.bin` beside INT8 weights). A model without either file loads as before and has no head. The desktop build copies the f32 head and its JSON (`build/configure/src/aqt.rs`).

## Inputs on the big engine

| Engine | Frozen inputs | History features | Head inputs | Head parameters |
| --- | --- | --- | --- | --- |
| small (Small and Tiny) | layer norm of the residual (width 64) after the input encoder and after each of the 4 interleaved layer rounds (5 x 64), then the instant head's hidden layer (128) | 26 | 474 | 69,056 |
| big | layer norm of the residual (width 128) after the input encoder and after each of the 5 sequential streams card, deck, note, preset, user (6 x 128), then the instant head's hidden layer (512) | 26 | 1,306 | 175,552 |

The schedule unit is a layer round in the small engine's interleaved schedule (the next step starts another layer index) and a stream in the big engine's sequential schedule (the next step starts another module); `answer_time_unit_ends` in `rslib/src/rwkv/engine.rs` encodes both, and compile-time checks pin 4 units for the small engine (the tiny plan drops one step of round 2, not a round) and 5 for the big one. This is the layout the time heads for the family (SM117) captured from the PyTorch big model (`fork_forward_capture`, every output bitwise the stock forward's). The 26 history features are unchanged (`AnswerTimeHistory`).

## Upstream quality (the time heads for the family (SM117), 722 users of the 732 set, show-time inputs, all time-labelled reviews)

Censored 16-bin time LogLoss given the true grade (lower is better), against the user's running median per grade; brackets are 95% review-weighted paired user-bootstrap intervals. Every head passed its gate: grade outputs bitwise unchanged on 5 users, and better than both time baselines with the CI below zero.

| Head of | Users | Time LogLoss | vs the user's running median |
| --- | ---: | ---: | --- |
| the show-time big model (SM91s2) | 719 | 1.9558 | -0.4345 [-0.4608, -0.4108] (-18.2%) |
| the show-time instant-only big model (SM99) | 719 | 1.9523 | -0.4380 [-0.4646, -0.4138] (-18.3%) |
| the show-time retrain (SM92) | 722 | 1.9716 | -0.4134 [-0.4402, -0.3889] (-17.3%) |
| the Tiny two-head model (SM106) | 722 | 1.9751 | -0.4098 [-0.4368, -0.3856] (-17.2%) |

On the later-day subset (the rows the earlier head was gated on), the new head of the show-time retrain (SM92) scores 1.9583 against 1.9580 for the head it replaces, so the replacement costs nothing there and adds same-day coverage on the inputs the fork actually feeds the model. "Recall within S seconds" (P(recall within S s) = sum over Hard, Good, Easy of P(grade) x P(time <= S | grade)) beats both running baselines at every S from 3 s to 30 s for every head; its calibration error is below 0.01 from 8 s up for every head and above it at 3 s (0.013 to 0.016), so the 3 s and 5 s points of the Card Info curve are the least reliable.

## The longer Instant-Only models

Instant LogLoss, 722 users of the 732 set, show-time inputs, w8+s8, paired user bootstrap (2,000 resamples, seed 0, 95% CI):

| Model | Instant LogLoss | vs the model it replaces |
| --- | ---: | --- |
| the longer Small Instant-Only model (SM113) | 0.275933 | -0.094% [-0.103, -0.085] vs the Small Instant-Only model (SM98) (0.276191), 628 users better / 94 worse |
| the longer cool-down for Tiny (SM115) | 0.276446 | -0.099% [-0.108, -0.091] vs the Tiny Instant-Only model (SM101) (0.27672), 621 users better / 101 worse |

The longer Small Instant-Only model (SM113) is the Small Instant-Only model (SM98)'s recipe with a two-epoch cool-down distilled from the show-time instant-only big model (SM99) (it closes 0.87 of the gap to it); the longer cool-down for Tiny (SM115) is the same recipe on the tiny plan (one card layer). Architectures, state sizes and runtimes are those of the Small Instant-Only model (SM98) and the Tiny Instant-Only model (SM101): the same weight file sizes, the same card layers (`ModelKind::Sm113` two, `ModelKind::Sm115` one, checked against the weights), the same stored state per card and note in a pair (7,098 B for Small and 5,282 B for Tiny at int4 cards + int8 notes). Their curve heads are untrained and never served (`instant_only()`).

## Verification

- **Answer-time parity, synthetic histories.** For each bundled head, fixtures of two synthetic users (900001 and 900002 from anki-rwkv `scripts/synthetic_testvector_users.py`; no dataset rows), first 500 rows each, show-time inputs, hold the PyTorch head's logits for the f32 and the int8 head (Small and Tiny: on `weights.int8.bin`; Big: on the f32 trunk, because the fork has no int8 big weights), the 26 history features and the synthetic revlog. 233 + 228 time rows per fixture, 186 of them same-day. The native trunk + head reproduce them; tolerance 1e-5 on every bin probability (1e-6 on the history features), and the head's trunk pass gives the grade path's button probabilities bit for bit:

| Head of | Queries | f32: max bin probability error / logit error | int8: max bin probability error / logit error | Test |
| --- | ---: | --- | --- | --- |
| the show-time big model (SM91s2) | 461 | 7.9e-7 / 7.2e-6 | 6.8e-7 / 6.7e-6 | `rwkv_legacy_answer_time_head_matches_published_vectors` |
| the show-time instant-only big model (SM99) | 461 | 7.8e-7 / 8.6e-6 | 6.7e-7 / 8.6e-6 | `rwkv_sm99_answer_time_head_matches_published_vectors` |
| the show-time retrain (SM92) | 461 | 3.5e-7 / 7.2e-6 | 2.9e-7 / 8.1e-6 | `rwkv_sm92_answer_time_head_matches_published_vectors` |
| the Tiny two-head model (SM106) | 461 | 3.7e-7 / 6.7e-6 | 3.4e-7 / 7.6e-6 | `rwkv_sm106_answer_time_head_matches_published_vectors` |
| the longer Small Instant-Only model (SM113) | 461 | within 1e-5 (test passes) | within 1e-5 (test passes) | `rwkv_sm113_answer_time_head_matches_published_vectors` |
| the longer cool-down for Tiny (SM115) | 461 | within 1e-5 (test passes) | within 1e-5 (test passes) | `rwkv_sm115_answer_time_head_matches_published_vectors` |

  The history features match exactly (maximum error 0; `rwkv_sm92_`, `rwkv_sm106_` and `rwkv_big_answer_time_history_matches_published_features`). On the packaging side, the NumPy recomputation of the logits from the written head files differs from PyTorch by at most 3.8e-6.

- **Recall parity of the new models, synthetic histories.** `rwkv_sm113_matches_published_vectors` and `rwkv_sm115_matches_published_vectors` (fixtures `sm113-vectors.json.gz` and `sm115-vectors.json.gz`, the same synthetic users, 500 rows each, with each row's interval) check instant recall, the four button probabilities and the curve at f32 and INT8 weights within 1e-5:

| Model | f32 weights | INT8 weights | Show-time interval features |
| --- | --- | --- | --- |
| the longer Small Instant-Only model (SM113) | 1.3e-6 | 8.9e-7 | 7.3e-7 (461 queries) |
| the longer cool-down for Tiny (SM115) | 8.9e-7 | 8.3e-7 | 7.3e-7 (461 queries) |

  The package-side parity of PyTorch against the NumPy reference on the package's test vectors: the longer Small Instant-Only model (SM113) fp32 p_imm 7.2e-7, p_rating 1.8e-6, p_curve 2.4e-7 (int8 6.0e-7 / 8.9e-7 / 3.0e-7); the longer cool-down for Tiny (SM115) fp32 6.0e-7 / 8.3e-7 / 3.0e-7 (int8 8.3e-7 / 8.3e-7 / 3.6e-7); `check_testvectors.py` PASS for both.

- **Grade outputs unchanged.** Loading a head changes no grade output (`rwkv_runtime_answer_time_head_is_optional_and_leaves_grades_unchanged`, now also for the Tiny two-head model (SM106) and the show-time big model (SM91s2), bitwise); the pair tests check that every recall, button, curve, interval and S90 output of each pair equals its models alone, bit for bit, at every state precision, and that the pair's answer time equals the answering model's alone.

- **Tests run (Windows, CPU):** `cargo test -j 4 -p anki --lib rwkv::` with default features 366 passed (362 before the heads of the longer Instant-Only models (SM113, SM115) were added, plus their four new tests), 0 failed, 34 ignored; before that change `--no-default-features` (the big engine as the primary one) gave 362 passed, 0 failed, 33 ignored (not rerun for the added heads). The Python figures below were measured before the SM113 / SM115 heads were added and have not been rerun since (no built bridge in this checkout). Python `qt/tests/test_rwkv_model.py`, `test_rwkv_scheduler.py`, `test_rwkv_inference_process.py` and `test_rwkv_answer_time.py`: 2,066 passed, 4 skipped, run against a freshly built release `rsbridge` of this branch, so the native-bridge cases ran (among them the answer-time head of every size, a same-day estimate of the show-time retrain (SM92)'s new head, the Big pair answering from the show-time instant-only big model (SM99), and the Small and Tiny pairs with the longer Instant-Only models at f32, int8 and int4 cards + int8 notes); `test_mediasrv.py -k rwkv`: 13 passed. `cargo clippy -p anki --tests`: clean. The TypeScript and end-to-end tests were not run (Node is not installed).

## Heads of the longer Instant-Only models (added 2026-11-17)

The time heads for the new candidates (SM119) trained and gated the heads of the longer Small Instant-Only model (SM113) (time LogLoss 1.9605) and the longer cool-down for Tiny (SM115) (1.9621); both passed the gate. They are packaged and wired as follows (the steps, kept for the next model):

1. In the anki-rwkv worktree of branch `time-heads-package`, run `time_heads_package.py sm113` and `time_heads_package.py sm115` (one command each, CPU, about 15 s; exact commands in that branch's `artifacts/small-model/time-heads-package/`). Each checks the trunk against the fork's weights bit for bit and writes `qt/aqt/rwkv_inference/<key>/time_head.{f32,int8}.bin`, `time_head.json` and `rslib/src/rwkv/testdata/<key>-time-vectors.json.gz`.
2. In the fork: add `"sm113", "sm115"` to the head list in `build/configure/src/aqt.rs`; add `sm113_answer_time_vectors` / `sm115_answer_time_vectors` and their two tests to `rslib/src/rwkv/sm40_tests.rs` (copies of the Tiny two-head model (SM106)'s ones); in `rslib/src/rwkv/runtime.rs` the family pair test then sees the pair answer from the Instant-Only model (`answer_time_from_instant_model()`), which it already accepts. The Python side needs no change.
