# Compact FSRS per-review prediction profile

On 2026-11-03, an instrumented macOS arm64 release-LTO Anki build compared the
existing compact-history adapter with a new fsrs-rs compact prediction API. Both
paths ran in the same binary, using the same copied Yomitan vocabulary deck and
the same FSRS-7 parameter vector. The deck contained 11,744 cards, 188,183 rated
reviews and 173,683 eligible final-fit targets. The input and configuration
match [the earlier compact-history benchmark](fsrs-compact-performance.MD),
including the qualification that other presets apply to some cards in the deck.

Each result is the median of seven fresh-process samples per path, in alternating
order. Elapsed time includes backend input preparation and cache writes but not
copying or opening the collection. Peak RSS includes the complete process and
collection open. The prediction step includes mapping predictions back to review
IDs. [Raw samples](fsrs-compact-prediction-profile-2026-11-03.json) include the
individual phases and output checksums; they contain no review IDs or parameters.

| Operation | Existing adapter | fsrs-rs API | Peak RSS before | Peak RSS after |
| --- | ---: | ---: | ---: | ---: |
| Final-fit rebuild | 697.95 ms | 687.51 ms | 112.30 MiB | 112.41 MiB |
| Final-fit prediction step | 43.54 ms | 41.72 ms | — | — |
| Final fit plus five validation folds | 1,418.48 ms | 1,410.26 ms | 124.27 MiB | 125.30 MiB |
| Prediction steps across all six fits | 149.16 ms | 141.06 ms | — | — |

The new API produced the same ordered checksum in every pair: 173,683 final-fit
rows and 318,419 rows including validation folds. The small end-to-end differences
are not sufficient to establish a reliable speedup. The API would centralize the
prediction walk in fsrs-rs; this experiment does not show a material memory
improvement over the existing compact adapter.

The [integration patch](fsrs-compact-prediction-integration.patch) replaces the
Anki adapter with the new API. It passed Anki's Rust test suite in an isolated
checkout using the local fsrs-rs branch; it is not applied to Anki's production
dependency. Once the fsrs-rs commit is available from a suitable Git remote,
Anki can update its pin and apply the patch without introducing a machine-local
dependency.

The median phase breakdown for the existing adapter's final-fit rebuild was
107.83 ms fetching reviews, 8.86 ms preparing histories, 105.72 ms generating
cache rows, 0.18 ms sorting, and 469.87 ms saving. The prediction step above is
part of generating cache rows, not an additional phase. These are separate
medians, so their sum need not equal the median total. Saving represented roughly
67% of the median total; this identifies a candidate for further investigation,
not proof that the write path can be accelerated without changing its behavior.
The current save path deliberately uses bounded batches so cancellation and
collection reads can run between writes.

Verification included the fsrs-rs `.github/workflows/check.sh` workflow
(formatting, Clippy, release documentation, 135 tests and eight doctests, with
the workflow's `SKIP_TRAINING=1` setting) and Anki's `just test-rust` with the
integration patch and local fsrs-rs branch.

## Reproducing the profile

The fsrs-rs implementation is commit `49fe84eaaced63bc5382e2d19a6ec5160b9df772`
on the local `codex/compact-review-predictions` branch. The measured library code
is the same as that branch's implementation; the build used the existing Anki
fsrs-rs revision plus those source changes. The
branch was subsequently based on the refreshed upstream main, which added only a
development dependency update.

The [profiling patch](fsrs-compact-prediction-profile.patch) was applied only to
an isolated Anki checkout based on `3c4a61048`. For a profiling build, point that
checkout's fsrs workspace dependency to the local fsrs-rs branch, apply the patch,
and use the existing `just fsrs-compact-bench` recipe with a copied collection
and parameter file. Set `ANKI_FSRS_PROFILE=1`; additionally set
`ANKI_FSRS_UPSTREAM_PREDICTION=1` for the new API. Leave that second variable
unset for the existing adapter. Repeat `predict` and `folds` in fresh processes,
alternating the paths, and compare the recorded digests before timings. The
instrumentation and local path dependency are for profiling only.
