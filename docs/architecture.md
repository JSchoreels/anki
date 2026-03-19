# Anki Architecture

Very brief notes for now.

## Backend/GUI

At the highest level, Anki is logically separated into two parts.

A neat visualization of the file layout is available here:
<https://mango-dune-07a8b7110.1.azurestaticapps.net/?repo=ankitects%2Fanki>
(or go to <https://githubnext.com/projects/repo-visualization#explore-for-yourself> and enter `ankitects/anki`).

### Library (rslib & pylib)

The Python library (pylib) exports "backend" methods - opening collections,
fetching and answering cards, and so on. It is used by Anki’s GUI, and can also
be included in command line programs to access Anki decks without the GUI.

The library is accessible in Python with "import anki". Its code lives in
the `pylib/anki/` folder.

These days, the majority of backend logic lives in a Rust library (rslib, located in `rslib/`). Calls to pylib proxy requests to rslib, and return the results.

pylib contains a private Python module called rsbridge (`pylib/rsbridge/`) that wraps the Rust code, making it accessible in Python.

### GUI (aqt & ts)

Anki's _GUI_ is a mix of Qt (via the PyQt Python bindings for Qt), and
TypeScript/HTML/CSS. The Qt code lives in `qt/aqt/`, and is importable in Python
with "import aqt". The web code is split between `qt/aqt/data/web/` and `ts/`,
with the majority of new code being placed in the latter, and copied into the
former at build time.

## Protobuf

Anki uses Protocol Buffers to define backend methods, and the storage format of
some items in a collection file. The definitions live in `proto/anki/`.

The Python/Rust bridge uses them to pass data back and forth, and some of the
TypeScript code also makes use of them, allowing data to be communicated in a
type-safe manner between the different languages.

At the moment, the protobuf is not considered public API. Some pylib methods
expose a protobuf object directly to callers, but when they do so, they use a
type alias, so callers outside pylib should never need to import a generated
\_pb2.py file.

## FSRS Help Me Decide Review-Time Buckets

`Help Me Decide (Experimental)` now estimates review time from bucketed history
instead of a single fixed review cost table.

Data flow:

1. Read revlog entries for the searched cards.
2. Reconstruct review histories and infer pre-review memory state using FSRS.
3. Build samples `(R, Grade, taken_millis)` for review-kind entries.
4. Aggregate samples into buckets:
   - `R`: 5% bands (`95-100`, `90-95`, ...).
   - `Grade`: Again/Hard/Good/Easy.
5. Resolve missing buckets by averaging nearby `R` buckets; if still empty,
   fall back to grade-level mean.
6. During `simulate_workload`, each simulated review uses
   `time(R_bucket, Grade)` to accumulate `daily_time_cost`.
7. The workload response includes a flattened matrix for UI inspection:
   - `review_time_fail_seconds`
   - `review_time_pass_seconds`
   - `review_time_sample_counts` (raw per-cell sample counts)
   with bucket dimensions:
   - `review_time_r_bucket_count`
   - `review_time_s_bucket_count` (fixed to `1`, UI compatibility)

Scope:

- This is currently applied only to the Help Me Decide workload simulation path.
- Normal review simulation and scheduler behavior are unchanged.
