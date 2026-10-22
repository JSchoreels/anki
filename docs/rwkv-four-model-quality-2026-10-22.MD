# Four-model RWKV LogLoss comparison — 2026-10-22

This comparison adds the bundled big model (Legacy, 2,762,884 parameters) to the same backup and native replay used for SM40, SM36 and SM55. All four use **f32 compute weights and `cards_int4_notes_int8` state storage**: INT4 card matrices, INT8 note matrices, f32 row scales, f16 shifts and f32 shared scopes. This is the user's current quantization, rather than upstream's w8+s8 evaluation mode.

On this collection, **SM36 has the lowest Curve LogLoss and the big model has the lowest Instant LogLoss**. SM36's interday Instant loss is only **0.0033% above the big model's**. These are descriptive results for one collection; they do not replace the upstream many-user comparisons or establish a prospective scheduling benefit. No model selection or quantization setting is changed by this benchmark.

## Matched results

LogLoss is lower-is-better. All models replay **224,074 canonical retained answers**. The interday slice scores **176,943 matched repeated reviews** with elapsed days ≥ 1 and the existing eligibility flag.

| Model | Curve LogLoss | Instant LogLoss | Curve relative to big model | Instant relative to big model |
| --- | ---: | ---: | ---: | ---: |
| Big model (Legacy) | 0.46095616 | **0.45598502** | reference | reference |
| SM40 | 0.46100351 | 0.45627509 | +0.01027% | +0.06361% |
| SM36 Muon | **0.46035114** | 0.45599996 | −0.13125% | +0.00328% |
| SM55 | 0.46045784 | 0.45604222 | −0.10811% | +0.01254% |

Including same-day repeated reviews gives **210,097 matched reviews**:

| Model | Curve LogLoss | Instant LogLoss | Curve relative to big model | Instant relative to big model |
| --- | ---: | ---: | ---: | ---: |
| Big model (Legacy) | 0.44683328 | **0.44131036** | reference | reference |
| SM40 | 0.44680476 | 0.44229566 | −0.00638% | +0.22327% |
| SM36 Muon | **0.44622928** | 0.44202287 | −0.13517% | +0.16145% |
| SM55 | 0.44628332 | 0.44199691 | −0.12308% | +0.15557% |

The same models lead each head in both slices. SM55 is the best small model for Instant on the all-repeated slice, while SM36 is the best small model for both heads on the interday slice. The big model's Instant advantage is larger when same-day reviews are included. No independent-user confidence interval is inferred from this single collection.

## Data, models and scoring

The source is the previously copied `backup-2026-10-21-23.46.14.colpkg`, SHA-256 `915c25948b232a5673e65c3d0cf7928e8332b85fd3325ce16b5e1abcdcc7d57a`. Its existing canonical input remains `/private/tmp/rwkv-checkpoint-zstd-2026-10-22/input.json`, SHA-256 `20d87d631758258cb5779c55d4a2e9484d0218d3b33ae8f291c6a02be2cef34c`. The live profile database and the original backup are never opened or modified by this run.

The big model runs the existing `rwkv::model_bench::rwkv_model_quality_case` in an optimized **`--no-default-features`** build, selecting the Legacy architecture. The full test name and `--exact` prevent accidentally running the alternate architecture's copy of the same test. Small-model predictions are reused from the [SM36](rwkv-sm36-2026-10-22.MD) and [SM55](rwkv-sm55-2026-10-22.MD) checks after verifying their input and prediction hashes and reproducing their aggregate losses. The same native production replay and scoring convention apply to all four models; each architecture retains its own native head equations and feature handling.

Probabilities are causal: Instant is the current pre-answer head, while Curve evaluates the previous answer's curve at the elapsed time of the current answer. The first review of every card is excluded from scoring. Both slices require the same existing per-row eligibility flag; the all-repeated slice also includes elapsed days < 1. Scoring clips probabilities to [1e−6, 1−1e−6], matching the existing comparison tool. Both NumPy aggregation and an independent scalar `math.fsum` calculation agree to less than `1e−14` LogLoss. All eight prediction arrays have exactly 224,074 finite probabilities in [0, 1].

| Compute weight file | SHA-256 |
| --- | --- |
| Legacy `RWKV_trained_on_5000_10000.bin` | `749ad1ad18d5b6ef8c4185802fa0c1e4a14e67cd7874f3efeaae355fffb1b9ec` |
| SM40 `sm40v2/weights.f32.bin` | `a8d13b82b4e534e16bc5b3e5986c04cc52fc80ac36ddb9695049f5fa41d38a74` |
| SM36 `sm36/weights.f32.bin` | `38089bf527281c9d40d0175e580e630d970cd22e7cb852d7b325e2583e6f6141` |
| SM55 `sm55/weights.f32.bin` | `dc8ef4e36d9df3e221c61f9c79238ad33f2c01e4db6e1a833d132f2715e90b38` |

The comparison uses the native source at `49d92c9a8af7748c9ef1ae77d0d7dd2aa8d3df2c`, with the pre-existing unrelated `review_order_bench.rs` work left unchanged. The source snapshot and per-file hashes record that working-tree state. This is an accuracy replay; its one-run elapsed time is not a matched performance benchmark.

## Reproduction and evidence

The equivalent replay using the repository recipe is:

```sh
rtk proxy env ANKI_RWKV_MODEL_INPUT=/private/tmp/rwkv-checkpoint-zstd-2026-10-22/input.json ANKI_RWKV_MODEL_OUTPUT=/private/tmp/rwkv-four-model-quality-2026-10-22/quality-legacy ANKI_RWKV_MODEL_WEIGHTS=/Users/jschoreels/workspace/anki/qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin ANKI_RWKV_MODEL_PRECISION=cards_int4_notes_int8 just rwkv-model-tests --no-default-features --release -- --ignored --exact rwkv::model_bench::rwkv_model_quality_case --nocapture
```

The output directory must exist. The native quality case completed all retained answers and passed. Raw Legacy predictions, its log, the aggregation script, source archive and [aggregate JSON](rwkv-four-model-quality-2026-10-22.json) are preserved under `/private/tmp/rwkv-four-model-quality-2026-10-22/`. The small-model controls remain in their original private evidence directories. The JSON records all model/input/output hashes, full-precision losses and relative differences, and verification results; no private review rows or prediction arrays are committed.

The required `TZ=UTC just check` passed; unchanged code checks used the existing build cache. This follow-up changes only the report and documentation links. The Legacy replay and independent four-model aggregation were executed for this comparison.
