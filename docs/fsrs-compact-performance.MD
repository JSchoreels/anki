# Compact FSRS history benchmark

Measured on 2026-11-03 on macOS arm64 with release-LTO builds. The pre-change
checkout was `77f62489a`; its preserved release binary's optimization,
evaluation and calibration outputs matched a fresh pre-change debug build.
The updated binary integrates [fsrs-rs #469](https://github.com/open-spaced-repetition/fsrs-rs/pull/469).
The same copied backup was imported once into a temporary collection and copied
again for each sample. No live profile database was opened or modified.

## Yomitan vocabulary deck

The Yomitan vocabulary deck contained 11,744 cards and 188,183 rated reviews;
the search produced 173,683 FSRS-7 training targets. The runs used the deck's
34-parameter FSRS-7 preset, included same-day reviews, disabled scheduling
penalties and Dynamic DR, and used one relearning step. The separate Yomitan
Kanji deck was not included. Each value is the median of three fresh-process
samples per build, run in alternating order. [Raw samples](fsrs-yomitan-compact-benchmark-2026-11-03.json)
contain the individual timings, peak RSS and output digests.

The benchmark applies the Yomitan preset to the entire deck. In the copied
profile, 9,390 cards resolve to that preset and 2,354 to other FSRS presets, so
these timings do not include the work of routing cards among presets or running
separate optimizations for them.

| Operation | Time before | Time after | Peak RSS before | Peak RSS after |
| --- | ---: | ---: | ---: | ---: |
| Optimize FSRS-7 parameters | 1,568 ms | 364 ms | 259 MiB | 165 MiB |
| Evaluate current parameters | 767 ms | 178 ms | 135 MiB | 107 MiB |
| Generate final-fit predictions | 740 ms | 742 ms | 173 MiB | 111 MiB |
| Generate final-fit and five validation-fold predictions | 1,590 ms | 1,462 ms | 236 MiB | 125 MiB |
| Optimize with the optional health check | 1,821 ms | 626 ms | 412 MiB | 359 MiB |

Optimization responses and evaluation metrics were byte-identical across
builds. All 173,683 final-fit rows and all 318,419 final-fit plus validation
rows had matching ordered checksums, including review IDs, predictions, roles,
folds and sources.

A [follow-up profile](fsrs-compact-prediction-profile-2026-11-03.MD) compares
the current per-review adapter with a proposed compact fsrs-rs prediction API.

## Whole collection

The collection contained 31,673 cards and 270,985 rated reviews. The FSRS-7
search produced 248,668 targets. These runs used another fixed set of 34 parameters, included
same-day reviews, disabled scheduling penalties and Dynamic DR, and used one
relearning step. Each table value is the median of three fresh-process samples
per build, run in alternating order. [Raw samples](fsrs-compact-benchmark-2026-11-03.json)
include individual timings, peak RSS and output digests.

| Operation | Time before | Time after | Peak RSS before | Peak RSS after |
| --- | ---: | ---: | ---: | ---: |
| Optimize FSRS-7 parameters | 2,231 ms | 511 ms | 337 MiB | 196 MiB |
| Evaluate current parameters | 909 ms | 212 ms | 148 MiB | 111 MiB |
| Generate final-fit predictions | 1,077 ms | 938 ms | 217 MiB | 125 MiB |
| Generate final-fit and five validation-fold predictions | 2,196 ms | 1,941 ms | 307 MiB | 143 MiB |
| Optimize with the optional health check | 2,594 ms | 878 ms | 549 MiB | 474 MiB |

Timing covers the backend operation, including input preparation and calibration
cache writes, but excludes copying and opening the temporary collection. Peak
RSS is for the whole fresh process, including collection opening; it is not an
allocation count for FSRS alone. Cache-row checksums are computed after timing
and RSS capture. Storage performance and review-history distribution affect the
results. The health check still uses the expanded-item cross-validation API,
which explains its higher peak memory.

The optimization response and evaluation metrics were byte-identical across
builds. All 248,668 final-fit rows and all 455,892 final-fit plus validation rows
had matching ordered checksums, including review IDs, predictions, roles, folds
and sources. The prediction path uses the existing historical-memory-state API
once per selected card and preserves target order; validation-fold training uses
only the selected chronological prefixes.

## Reproducing

Use a collection imported from an already copied `.colpkg` under `/private/tmp`
or the workspace. Preserve both complete builds, including their native bridge,
before rebuilding. Supply a JSON file containing the same 34 FSRS-7 parameters
to each run; keep the collection and parameters unchanged across samples.

```sh
rtk just fsrs-compact-bench \
  --source-root /path/to/preserved/build \
  --collection /private/tmp/fsrs-bench/source/collection.anki2 \
  --parameters /private/tmp/fsrs-bench/parameters.json \
  --work-dir /private/tmp/fsrs-bench/runs \
  --output /private/tmp/fsrs-bench/result.json \
  --scenario optimize \
  --search "'deck:\"My Deck\"'"
```

Repeat in fresh processes for `evaluate`, `predict`, `folds` and
`optimize-health`, alternating baseline and updated builds. The tool copies the
collection into a new temporary directory for each invocation and rejects paths
inside `Anki2`. Omit `--search` for the whole collection. The additional shell
quotes around the search survive the `just` recipe's argument expansion. Compare
output digests before interpreting the timings.
