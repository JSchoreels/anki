# Upstream performance backports

Reviewed on 2026-10-07 against refreshed official Anki `main` at
`a13d8a6e9e91d87eb046bf7b26ee64880a4eb544`. The fork baseline is
`7d3e1c310`, which already includes the Clanki port `91979c359`.

## Changes integrated

| Official Anki PR | Upstream status at review | Behavior |
| --- | --- | --- |
| [#5768](https://github.com/ankitects/anki/pull/5768) | Merged | Give the Browser column header an empty selection model so repainting does not scan every selected row. The table keeps its actual selection. |
| [#5771](https://github.com/ankitects/anki/pull/5771) | Merged | Count changed selection ranges for menu actions as well as Ctrl/Shift selection. Preserve the fork's fallback for add-ons overriding model flags. |
| [#5717](https://github.com/ankitects/anki/pull/5717) | Merged | Stream full collection downloads through a buffered temporary file, flush it, check integrity, and only then atomically replace the local collection. |
| [#5654](https://github.com/ankitects/anki/pull/5654) | Open | Use the same millisecond mtime helper for media scans, local additions, and downloads, avoiding unnecessary hashing of unchanged files. |

The media change does not change the database schema. Entries written in seconds
by older scans are hashed once, retain the existing checksum-based pending-sync
behavior, and are recorded in milliseconds for subsequent scans. Deleted entries
still use zero. Subsecond changes can now be compared in the same units as file
additions.

## Already included through Clanki

- [#5755](https://github.com/ankitects/anki/pull/5755): set-based selection
  restoration after searches.
- [#5756](https://github.com/ankitects/anki/pull/5756): selection range counting.
- [#5766](https://github.com/ankitects/anki/pull/5766): reduced per-cell enum and
  delegate overhead during table painting.
- [#5767](https://github.com/ankitects/anki/pull/5767): row lookups using
  `compress`/`map` and faster disabled-row counting.
- [#5757](https://github.com/ankitects/anki/pull/5757): cached sidebar section
  roots and direct tree traversal during expansion/filtering.

[Editor MathJax lazy loading (#5620)](https://github.com/ankitects/anki/pull/5620)
was reviewed but not included: it changes editor typesetting to asynchronous
loading and add-on availability of MathJax, and its author reports no established
startup-time improvement. It needs separate editor interaction verification.

## Browser measurements

Five samples on the local Apple Silicon development machine with offscreen Qt,
50,000 synthetic rows, four columns, no add-ons, and no collection access.
The before implementation uses the fork baseline's view setup and selection
callback; other Clanki improvements are present in both versions. Values are
medians and measure individual operations, not complete Browser responsiveness.

| Operation | Before | After |
| --- | ---: | ---: |
| Select All without modifiers | 434.23 ms | 0.91 ms |
| Invert a single-row selection | 415.85 ms | 0.85 ms |
| Paint the column header with every row selected | 90.32 ms | 0.59 ms |
| Row flags checked by that header paint | 50,001 | 0 |

The permanent Qt regression tests exercise selection, inversion, clearing,
disabled rows, and actual header rendering. Both new Browser tests failed before
the backports and pass after them. Existing tests retain add-on flag overrides
and search/card-note selection restoration.

Rust regression coverage checks unchanged media, one-time conversion of old
timestamps, subsecond file changes, decompression and flushing of a streamed
response, and rejection of an invalid download without replacing the local
collection. Existing sync roundtrip and timeout tests cover the surrounding
protocol. Tests use temporary synthetic data and localhost mock servers.

`just check` passed after the backports, including build, formatting, lint,
type checks, and all required test targets. The targeted Browser run passed
212 permanent tests plus the temporary benchmark comparison. No user collection
or live sync service was used.

Run the Browser regressions and complete checks with:

```sh
just test-py-target qt/tests/test_browser_table.py qt/tests/test_browser_sidebar.py
just check
```
