# Lossless RWKV checkpoint compression — 2026-10-22

On a copy of Main Profile's latest backup, independently compressing unchanged SM40 checkpoint bytes with zstd levels 1–3 saves **12.4–12.8%** against an otherwise identical uncompressed block container. It adds **71–150 ms to saving** and **44–46 ms to restoring** at the median, with approximately **1–2 MB more peak process RAM**. All decoded checkpoint bytes and restored native recurrent/runtime hashes match exactly. The experiment does not enable compression in desktop Anki or change its model, precision, scheduler, or production cache format.

## Measurements

Five rounds rotate and reverse codec order. Every save and restore uses a fresh optimized native process. MB means 1,000,000 bytes; times and whole-process phase peak RSS are medians. The matching raw block container isolates compression cost. Native SQLite is an additional production-format control; its storage and reader differ from the block experiment.

| Checkpoint | File size | Save | Restore | Peak RSS: save / restore |
| --- | ---: | ---: | ---: | ---: |
| Native SQLite, uncompressed | 53.084 MB | 91.7 ms | 87.6 ms | 90.3 / 86.5 MB |
| Raw 1 MiB blocks | 52.803 MB | 60.6 ms | 79.7 ms | 88.9 / 80.0 MB |
| zstd-1, 1 MiB blocks | 46.251 MB | 131.7 ms | 123.7 ms | 90.5 / 81.3 MB |
| zstd-2, 1 MiB blocks | 46.148 MB | 135.4 ms | 124.8 ms | 90.5 / 81.4 MB |
| zstd-3, 1 MiB blocks | 46.029 MB | 210.7 ms | 125.6 ms | 90.7 / 81.1 MB |

Level 1 saves **6.55 MB** against raw blocks, or **6.83 MB** against native SQLite. Moving from level 1 to level 3 saves only another **0.22 MB** and adds about **79 ms** to saving. Restored resident state is unchanged: the lower restore RSS of the block variants relative to SQLite comes from different container/reader buffers, while zstd itself uses slightly more RAM than raw blocks.

This would affect checkpoint writes during state-cache construction/recovery and restores during profile startup. The measured startup difference against native SQLite is about **36–38 ms**, which is small in normal desktop use; the disk saving is also modest on this collection. These results do not support enabling zstd as a performance or resident-memory optimization. If disk size becomes a priority, level 1 is the strongest measured candidate for a separate production integration.

## Input and verification

The input is an untouched temporary copy of `backup-2026-10-21-23.46.14.colpkg`, imported with the existing backup-preparation recipe into a separate temporary collection. No live profile database was opened or queried. Canonical production preprocessing retains **224,074 answers**, **13,977 cards**, **13,202 notes**, **22 decks** and **13 presets**, plus global state. The model is the bundled SM40 f32-weight export, SHA-256 `a8d13b82b4e534e16bc5b3e5986c04cc52fc80ac36ddb9695049f5fa41d38a74`. Precision remains `cards_int4_notes_int8`: INT4 card matrices, INT8 note matrices, f32 row scales, f16 shift vectors, and f32 shared scopes.

Production replay first creates a full native SQLite checkpoint. Each save case restores that source outside the timed region. Block cases stream the same native runtime bytes and full state-delta serialization through independent 1 MiB frames without constructing another complete state snapshot. Each restore decodes through the production native state/runtime deserializers. Raw and compressed variants use identical framing except for the codec; no column rearrangement, dictionary, re-quantization, or tensor conversion is performed.

All **50 timed save/restore cases** match the prepared recurrent-state and runtime-cache hashes. All **20 block checkpoint outputs** also pass a separate streaming, byte-for-byte comparison of their decoded payload against the raw reference, including end-of-file checks. Unit coverage exercises empty and multi-block data, forward seeking across compressed frames, truncation and trailing data. The zstd frames have no internal checksum in this experiment; explicit comparisons establish the measured round trips, while a production format would need its own corruption-handling design.

The restored recurrent-state BLAKE3 is `0956e4d32f9fa84502b06d27657b676dbe22503f812a408c26431c356fa6aad5`; the restored runtime-cache BLAKE3 is `74505f599274cb31d901a5aeb63cd03ec856ec6dc3ec3cc2f924f2bb8374bc28`. Restoring identical state with the same model and computation preserves subsequent predictions, so this lossless storage transformation introduces no LogLoss change. This is a consequence of exact restoration, not a newly measured many-user quality result.

## Quality evidence and scope

Choosing a different quantizer or model requires its own matched quality evidence. The upstream [SM47 comparison](https://github.com/JSchoreels/anki-rwkv/blob/977db90025c37c8e5ef7ac59f490c79d2b3288d1/artifacts/small-model/sm47/sm47-summary.md) reports 723 users for SM36 with w8 weights and experimental state formats; it does not directly validate this bundled SM40 f32-weight encoding. [SM49 variant C](https://github.com/JSchoreels/anki-rwkv/blob/977db90025c37c8e5ef7ac59f490c79d2b3288d1/artifacts/small-model/sm49/sm49-summary.md) has much stronger compression but a separate lossy-quantization tradeoff. Neither change is included here. The subsequently requested SM36 model integration is a separate comparison and must not be attributed to zstd.

The benchmark measures a full root checkpoint, without the desktop's history-map metadata, recovery-root/delta chain, incremental overwrites, stashes, undo, GUI progress, or Python process. It does not benchmark skipping superseded entries in a real checkpoint chain. Filesystem cache is warm; fresh processes do not imply cold disk reads. Save time includes serialization, compression, writes, flush and file synchronization, while replay, model loading, source restoration and verification are excluded. Native SQLite retains its normal transactional writer; the experimental block file does not reproduce SQLite transactions or atomic promotion.

Phase RSS is sampled every 2 ms with start/end handshakes; the aggregate JSON separately retains OS process high-water readings, which can include setup. macOS 26.7.1 arm64 was under substantial background load (load averages at launch approximately 34/16/12), so small timing differences should not be treated as isolated-machine guarantees. Raw save samples span 49–102 ms and zstd-3 spans 196–370 ms; all samples are retained.

## Reproduction and provenance

Copy an existing backup before preparing inputs, and choose a new output directory. The benchmark refuses existing output directories/checkpoints and leaves production cache files unchanged.

```sh
rtk proxy just rwkv-history-ablation-prepare --backup-copy /private/tmp/rwkv-checkpoint-zstd-2026-10-22/backup.colpkg --output /private/tmp/rwkv-checkpoint-zstd-2026-10-22/input.json --deck-match=
rtk proxy just rwkv-checkpoint-compression-native -- --nocapture
rtk proxy just rwkv-checkpoint-compression-bench --input /private/tmp/rwkv-checkpoint-zstd-2026-10-22/input.json --output /private/tmp/rwkv-checkpoint-zstd-2026-10-22/new-benchmark --rounds 5
rtk proxy just check
```

The [aggregate JSON](rwkv-checkpoint-zstd-2026-10-22.json) records exact source/input/model/executable hashes, per-run timings and RSS, byte checks and medians. The experiment began at HEAD `e3599ef5c3cbbea8b3c3ccbb6dc6fb01a3fd58aa` with uncommitted benchmark tooling and an unrelated existing edit in `review_order_bench.rs`; exact source hashes and the preserved source archive identify the tested code. Private backup/input files, native SQLite and block checkpoints, executable, requests and full logs remain under `/private/tmp/rwkv-checkpoint-zstd-2026-10-22/`. No per-card data is committed.

After measurement, the Python driver's SHA-256 helper was changed to an equivalent bounded streaming implementation for compatibility with the repository's typing target. Both hashing methods produce identical digests for all 31 source/input/model files checked. Native sources and the measured executable are unchanged; the archived driver is the exact version used during measurement.

Verification passed: both focused native block tests, targeted Python formatting/lint checks, and the complete `just check` build, lint and test workflow.
