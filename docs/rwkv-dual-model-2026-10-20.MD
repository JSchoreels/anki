# Dual RWKV model integration — 2026-10-20

Both Legacy and SM40 are available in the same desktop build. Preferences → Review → RWKV selects the prediction model for the entire local profile, independently of f32, INT8 or INT4 state storage.

## Profile behavior

The policy below describes the initial integration. The current default is SM40 for profiles without a supported saved choice; explicitly saved Legacy selections are preserved. See [current profile behavior](rwkv.MD#bundled-rwkv-models).

- Existing profiles without `rwkvModel` keep Legacy. Supported saved choices are preserved; unknown values also resolve to Legacy.
- New profile creation explicitly saves `rwkvModel = sm40`. State precision continues to default to INT8, and existing precision choices are preserved.
- Preferences saves the next profile-open choice. The current backend, background jobs and cache identity continue using the active model until the profile reopens.
- Model changes rebuild state from retained review history. Both weight files are bundled, and only the selected model is loaded. Model-tagged runtime caches and undo states reject cross-model restoration.

The native implementation compiles one shared engine source twice with fixed dimensions, retaining the existing tensor loops. The desktop bridge selects the engine when loading the profile. The `rwkv-sm40` Cargo feature selects the primary engine for standalone benchmarks; both runtime models are present with and without it.

## Matched full-history verification

The existing copied-history input contains 224,022 retained answers. Three optimized fresh-process reconstructions per variant alternate forward/reverse order, followed by final cache restoration and separate causal quality scoring. Input preparation, model loading, state reconstruction and final cache writing are included in total reconstruction time. Resource measurements use the standalone native engine harness; desktop selection and dispatch are covered separately by Python/native bridge and scheduler tests.

| Variant | Total reconstruction | Final cache | Ready RSS after restore |
| --- | ---: | ---: | ---: |
| Legacy, f32 state | 9.96 s | 1211.6 MB | 1402.8 MB |
| Legacy, INT8 state | 10.91 s | 371.6 MB | 677.1 MB |
| SM40, f32 state | 3.09 s | 192.0 MB | 412.8 MB |
| SM40, INT8 state | 3.42 s | 67.4 MB | 291.6 MB |

MB means 1,000,000 bytes. These are fresh measurements on the same M1 host; run-to-run conditions also affect the difference from the initial report.

All eight Curve/Instant prediction files are **bit-identical** to the frozen [initial four-variant comparison](rwkv-sm40-2026-10-20.MD). All log-loss, calibration and paired UM+ results match exactly, including the original quality ranking. Each variant also has identical state hashes across its three reconstructions and fresh-process cache restoration. State hashes use the new model-tagged runtime format.

The [aggregate JSON](rwkv-dual-model-2026-10-20.json) records source, model, input and executable hashes, run phases, resource samples, quality metrics and baseline parity. It identifies the pre-commit HEAD alongside exact source hashes; the measured source snapshot precedes only final lint annotations on the deliberate duplicate modules and checkpoint forwarding signature. The exact snapshot is archived with the private benchmark outputs. Private input, prediction arrays, source archive, executable copies and logs remain under `/private/tmp/anki-rwkv-sm40-2026-10-20/comparison-dual/`.

## Regression coverage

Both exact optimized benchmark executables pass 177 native RWKV tests, with 32 offline benchmarks ignored in each. Coverage includes both specialized engines, published SM40 fixtures, model-specific cache/undo rejection and same-model restoration at every supported state precision.

The focused desktop suite passes **708 tests**, with four optional Torch tests skipped because Torch is unavailable. It covers existing/new profile defaults, both models at all three precisions, profile reopen/reuse, active cache identity, real Qt selector keyboard interaction and saving, scheduler behavior, and native bridge state/undo/workload roundtrips for both models. The final **`just check` passes**, including repository Rust, Python, TypeScript/Svelte, formatting and lint checks. The installer test passed after retrying a transient stub-download network reset.

## Reproduction

```sh
just rwkv-model-bench --input /private/tmp/anki-rwkv-sm40-2026-10-20/input.json --output /private/tmp/anki-rwkv-sm40-2026-10-20/comparison-dual --rounds 3
just test-py-target qt/tests/test_rwkv_model.py qt/tests/test_rwkv_precision.py qt/tests/test_rwkv_inference_process.py qt/tests/test_rwkv_scheduler.py
just check
```

Use a fresh output directory for a new measurement. The comparison builds and tests both primary-engine configurations automatically.
