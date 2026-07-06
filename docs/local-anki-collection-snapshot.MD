# Local Anki Collection Snapshot

Use this workflow when benchmarks or experiments need a realistic Anki
collection without touching the live profile database.

## Current Snapshot

The current local snapshot is:

```text
/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2
```

It was copied from:

```text
/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2
```

The `.local/` directory is gitignored and must not be committed.

## Safety Rules

Do not run SQLite, Anki backend code, benchmarks, migrations, or ad hoc database
tools against the live profile path.

Use the snapshot path above for read-only analysis and benchmark inputs. If a
benchmark mutates the database, copy this snapshot to `/private/tmp` first and
mutate that temporary copy.

When refreshing the snapshot from the live profile:

1. Quit Anki.
2. Confirm no process has the database open:

```sh
lsof "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2" \
     "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2-wal" \
     "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2-shm"
```

3. Copy `collection.anki2` and any existing sidecars together:

```sh
mkdir -p /Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current
cp -p "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2" \
      "/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2"
cp -p "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2-wal" \
      "/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2-wal"
cp -p "/Users/jschoreels/Library/Application Support/Anki2/Main Profile/collection.anki2-shm" \
      "/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2-shm"
```

If a sidecar does not exist, skip that `cp`.

## Benchmark Variable

For local commands, prefer passing:

```sh
ANKI_BENCH_COLLECTION=/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2
```

For the retrievability-cache write benchmark in the
`codex/db-write-batching` worktree, use:

```sh
env PROTOC=/opt/homebrew/bin/protoc \
  ANKI_RETRIEVABILITY_CACHE_BENCH_COLLECTION=/Users/jschoreels/workspace/anki/.local/collection-snapshots/main-profile-current/collection.anki2 \
  ANKI_RETRIEVABILITY_CACHE_BENCH_ROWS=10000 \
  cargo test -p anki retrievability_cache_insert_benchmark -- --ignored --nocapture
```
