# FSRS-6 compact-history benchmark

## Follow-up against upstream main on November 6

After upstream merged the separate SIMD training change in #474, the compact
FSRS-6 implementation was updated to reuse that trainer. Seven fresh-process
samples per variant were run on the same copied Yomitan collection described
below, with a warm-up and alternating order. Both variants ran in the same
instrumented Anki binary and used fsrs-rs based on upstream commit `a466537`.

| Workload | Expanded time / peak RSS | Compact time / peak RSS |
| --- | ---: | ---: |
| Optimize parameters | 219.05 ms / 136.28 MiB | 218.97 ms / 103.91 MiB |
| Final-fit predictions and five validation folds | 1,101.31 ms / 146.56 MiB | 1,105.87 ms / 119.31 MiB |
| Optimize with health check | 302.80 ms / 284.73 MiB | 296.23 ms / 261.73 MiB |

The compact optimizer saved 23.8% of whole-process peak RSS for ordinary
optimization and 18.6% for the validation-fold workload. The timings do not
show a material speedup for these workloads. All optimization responses and
269,994 prediction rows had matching hashes across the paired variants.

On a separate synthetic workload of 1,000 cards with 256 reviews each,
complete processes using unmodified upstream expanded inputs and the new
compact path took 288.31 ms / 331.81 MiB and 230.96 ms / 32.20 MiB,
respectively. Seven samples per variant were rotated, with one warm-up. The
candidate's expanded path was also tested; all three printed identical
optimized parameters. These synthetic results depend on the unusually long
histories and should not be interpreted as Yomitan's speedup.

The [follow-up samples](fsrs6-compact-pr-benchmark-2026-11-06.json) contain
timings, peak RSS, counts and hashes, without review IDs or parameters. The
remaining sections document the earlier prototype against the older trainer.

On 2026-11-05, a copied November 3 Anki backup was imported into a temporary
collection. The benchmark selected the Yomitan vocabulary deck, containing
11,744 cards, and used its saved 21-parameter FSRS-6 vector. One vector was
applied to the whole deck; separate preset routing was not measured. No live
profile database was opened or modified.

The fork excludes same-day targets for FSRS-6 even when the benchmark request
enables them. Same-day reviews may still contribute to histories. There were
147,269 final-fit targets and 269,994 final-fit plus five-validation-fold rows;
these counts differ from the earlier FSRS-7 experiment.

Measurements use one instrumented macOS arm64 release-LTO build, one warm-up
per variant and seven fresh-process samples per variant. Orders were alternated
for paired comparisons and rotated for the three-way comparison. Each process
opened its own copy of the imported collection. Wall time includes input
preparation and cache writes but excludes copying and opening the collection.
Peak RSS covers the whole process, including opening the collection. Scheduling
penalties and Dynamic DR were disabled; the request specified one relearning
step. [Raw samples](fsrs6-compact-benchmark-2026-11-05.json) contain timing,
peak RSS, counts and output hashes, without individual reviews or parameters.

## Per-review predictions

Both paths start from Anki's current compact input preparation. The expanded
variant then materializes the selected histories and uses the existing
expanded-item prediction API; the compact variant uses the new card-prefix
prediction API directly. This isolates the prediction paths in the same binary;
it is not a reproduction of the complete Anki pipeline from before #469.

| Workload | Expanded time | Compact time | Expanded peak RSS | Compact peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Final-fit prediction rebuild | 632.53 ms | 637.45 ms | 141.28 MiB | 118.14 MiB |
| Final fit plus five validation folds | 1,572.29 ms | 1,578.45 ms | 204.52 MiB | 178.88 MiB |

The prediction step in the final-fit rebuild, including materializing expanded
inputs, took a median of 35.67 ms expanded and 11.88 ms compact, about three
times faster. Peak process memory fell by 16.4%, but the whole rebuild did not
become measurably faster.
For both workloads all ordered output-row hashes matched across samples.

### Why the calculation saving is not visible end to end

A separate five-pair phase profile recorded these medians:

| Phase | Expanded | Compact |
| --- | ---: | ---: |
| Create prediction rows, including the prediction call | 37.03 ms | 12.79 ms |
| Wait for the progress thread to finish | 73.24 ms | 97.61 ms |
| Complete prediction-row phase, including that wait | 110.42 ms | 110.44 ms |
| Save the cache | 411.45 ms | 411.80 ms |

The progress thread sleeps in 100 ms intervals before checking completion.
In these runs its additional wait absorbed the faster calculation, leaving
the complete row phase unchanged. Cache writes were the largest measured
phase. The [phase samples](fsrs6-compact-phase-profile-2026-11-05.json)
also matched the main experiment's prediction-row hash.

## Experimental compact FSRS-6 optimizer

An isolated proof of concept keeps the histories compact through FSRS-6
normalization, outlier selection and batch creation. It expands the smaller
initialization subset, retains the existing FSRS-6 optimizer and its batch
ordering, and builds the training batches directly from selected prefixes.
This is experimental code, not a production integration or an upstream change.

| Workload | Current optimizer time | Prototype time | Current peak RSS | Prototype peak RSS |
| --- | ---: | ---: | ---: | ---: |
| Optimize parameters | 440.62 ms | 438.37 ms | 156.88 MiB | 118.25 MiB |
| Optimize with health check | 654.46 ms | 631.26 ms | 289.48 MiB | 282.86 MiB |
| Final fit plus five validation folds | 1,578.45 ms | 1,586.75 ms | 178.88 MiB | 133.95 MiB |

The last row uses the compact prediction API in both columns and changes only
the optimizer used to train the five folds. Peak memory fell by 24.6% for normal
optimization and 25.1% for the fold workload; these samples do not establish a
material speedup for either. The health-check peak fell only 2.3%, because that
path still expands histories for its separate time-series evaluation. Its
median elapsed time was 3.5% lower in these samples.

Each optimization response, including the health-check result, and every
prediction-row hash matched between variants. The prototype also passed
bit-for-bit parity tests on generated card histories with fractional and
same-day intervals, two sequence limits and both short-term settings; the
tests passed with the normal outlier filter and with `FSRS_NO_OUTLIER=1`.
These checks support the benchmark comparison, but do not establish that the
prototype covers every input or is ready for production.

Verification used the isolated Anki checkout: `just check` passed with the
macOS packaging test excluded because the sandbox prevented its temporary disk
image from being created. That test then passed separately outside the sandbox.
The patched fsrs-rs checkout passed 137 library tests and eight doctests with
`SKIP_TRAINING=1`, plus the prototype parity tests with
`FSRS_NO_OUTLIER=1`.

## Reproduction

The Anki source was commit `b56021a77` and the fsrs-rs source was commit
`49fe84eaaced63bc5382e2d19a6ec5160b9df772`, with local-only changes recorded
in the [optimizer prototype](fsrs6-compact-optimizer-prototype.patch) and
[benchmark instrumentation](fsrs6-compact-benchmark-instrumentation.patch).
The instrumentation also records the additional progress-thread phase profile.
For a fresh isolated checkout, apply the patches, point the Anki fsrs workspace
dependency to the patched fsrs-rs checkout, update the temporary lockfile and
build with `RELEASE=2 just build`. Supply an imported collection copy and a
separate 21-value FSRS-6 parameter file to `just fsrs-compact-bench`.

Set `ANKI_FSRS6_EXPANDED_PREDICTION=1` to select the expanded prediction
comparison, `ANKI_FSRS6_COMPACT_OPTIMIZER=1` to select the prototype, and
`ANKI_FSRS6_PROFILE=1` to record phase timings. Compare response or ordered-row
hashes before interpreting timing or memory differences. These switches and
the local path dependency belong only to the experimental checkout.
