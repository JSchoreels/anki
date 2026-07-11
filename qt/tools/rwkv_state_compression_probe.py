#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Probe low-rank/quantized RWKV state compression on a local state cache.

This is an experimental measurement tool. It reads an Anki desktop RWKV
`snapshot-v1.bin`, samples serialized module states, and reports WKV singular
spectra plus naive low-rank quantization reconstruction error. It does not
modify the cache or collection.
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import time
import zlib
from pathlib import Path

import numpy as np  # type: ignore[import-not-found]

SNAPSHOT_MAGIC = b"ARWKVSNAPSHOT7\0"
MODULE_MAGIC = b"ARWKVMODSTATE1"
D_MODEL = 128
HEADS = 4
HEAD_SIZE = 32
MODULE_LAYERS = {
    "card": 3,
    "note": 2,
    "deck": 4,
    "preset": 3,
    "global": 4,
}


class Reader:
    def __init__(self, path: Path) -> None:
        self.file = path.open("rb")
        self.offset = 0

    def close(self) -> None:
        self.file.close()

    def read(self, size: int) -> bytes:
        data = self.file.read(size)
        if len(data) != size:
            raise EOFError(f"expected {size} bytes at {self.offset}, got {len(data)}")
        self.offset += size
        return data

    def u8(self) -> int:
        return self.read(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def bytes(self) -> bytes:
        return self.read(self.u32())

    def skip_bytes(self) -> int:
        size = self.u32()
        self.file.seek(size, os.SEEK_CUR)
        self.offset += size
        return size


class Buffer:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        data = self.data[self.offset : self.offset + size]
        if len(data) != size:
            raise EOFError(f"expected {size} bytes at {self.offset}, got {len(data)}")
        self.offset += size
        return data

    def u32(self) -> int:
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def f32_vec(self) -> np.ndarray:
        size = self.u32()
        start = self.offset
        self.offset += size * 4
        if self.offset > len(self.data):
            raise EOFError("truncated f32 vector")
        return np.frombuffer(self.data, dtype="<f4", count=size, offset=start)


def quantized_dequantized(values: np.ndarray, bits: int) -> np.ndarray:
    qmax = (1 << (bits - 1)) - 1
    amax = float(np.max(np.abs(values))) if values.size else 0.0
    if amax == 0.0 or not math.isfinite(amax):
        return np.zeros_like(values, dtype=np.float32)
    scale = amax / qmax
    quantized = np.rint(values / scale).clip(-qmax, qmax).astype(np.int16)
    return quantized.astype(np.float32) * scale


def relative_error(original: np.ndarray, reconstructed: np.ndarray) -> float:
    denominator = float(np.linalg.norm(original))
    if denominator == 0.0:
        return 0.0 if float(np.linalg.norm(reconstructed)) == 0.0 else float("inf")
    return float(np.linalg.norm(original - reconstructed) / denominator)


def summarize(values: list[float]) -> str:
    array = np.asarray(values, dtype=np.float64)
    if not array.size:
        return "n/a"
    return "mean={:.4f} p50={:.4f} p90={:.4f} p99={:.4f}".format(
        float(array.mean()),
        float(np.quantile(array, 0.50)),
        float(np.quantile(array, 0.90)),
        float(np.quantile(array, 0.99)),
    )


class Probe:
    def __init__(self, sample_limit: int) -> None:
        self.sample_limit = sample_limit
        self.scope_counts: dict[str, int] = {}
        self.scope_bytes: dict[str, int] = {}
        self.sampled_by_scope: dict[str, int] = {}
        self.zlib_ratios: list[float] = []
        self.shift_int4_errors: list[float] = []
        self.rank_energy: dict[int, list[float]] = {1: [], 2: [], 4: [], 8: []}
        self.rank_errors: dict[int, list[float]] = {1: [], 2: [], 4: [], 8: []}
        self.factor_errors: dict[tuple[int, int], list[float]] = {
            (rank, bits): [] for rank in (1, 2, 4) for bits in (4, 8)
        }

    def process_state(self, scope: str, state: bytes) -> None:
        buffer = Buffer(state)
        if buffer.read(len(MODULE_MAGIC)) != MODULE_MAGIC:
            raise ValueError("invalid RWKV module state magic")
        layer_count = buffer.u32()
        expected_layers = MODULE_LAYERS[scope]
        if layer_count != expected_layers:
            raise ValueError(
                f"{scope} state has {layer_count} layers, expected {expected_layers}"
            )

        self.sampled_by_scope[scope] = self.sampled_by_scope.get(scope, 0) + 1
        self.zlib_ratios.append(len(zlib.compress(state, 1)) / len(state))

        for _ in range(layer_count):
            x_shift = buffer.f32_vec()
            matrix = buffer.f32_vec()
            channel_shift = buffer.f32_vec()

            for shift in (x_shift, channel_shift):
                self.shift_int4_errors.append(
                    relative_error(
                        shift.astype(np.float32), quantized_dequantized(shift, 4)
                    )
                )

            if matrix.size != HEADS * HEAD_SIZE * HEAD_SIZE:
                raise ValueError(f"unexpected matrix size {matrix.size}")
            for head in range(HEADS):
                start = head * HEAD_SIZE * HEAD_SIZE
                original = (
                    matrix[start : start + HEAD_SIZE * HEAD_SIZE]
                    .reshape(HEAD_SIZE, HEAD_SIZE)
                    .astype(np.float32)
                )
                self.process_matrix(original)

        if buffer.offset != len(state):
            raise ValueError(
                f"trailing module state bytes: {len(state) - buffer.offset}"
            )

    def process_matrix(self, matrix: np.ndarray) -> None:
        u, singular_values, vh = np.linalg.svd(matrix, full_matrices=False)
        energy = singular_values * singular_values
        total_energy = float(np.sum(energy))
        if total_energy == 0.0:
            return

        for rank in (1, 2, 4, 8):
            kept = float(np.sum(energy[:rank]) / total_energy)
            self.rank_energy[rank].append(kept)
            self.rank_errors[rank].append(math.sqrt(max(0.0, 1.0 - kept)))

        for rank in (1, 2, 4):
            for bits in (4, 8):
                reconstructed = np.zeros((HEAD_SIZE, HEAD_SIZE), dtype=np.float32)
                for index in range(rank):
                    root = math.sqrt(float(singular_values[index]))
                    left = quantized_dequantized(
                        u[:, index].astype(np.float32) * root, bits
                    )
                    right = quantized_dequantized(
                        vh[index, :].astype(np.float32) * root, bits
                    )
                    reconstructed += np.outer(left, right).astype(np.float32)
                self.factor_errors[(rank, bits)].append(
                    relative_error(matrix, reconstructed)
                )

    def process_state_map(self, reader: Reader, scope: str) -> None:
        count = reader.u32()
        self.scope_counts[scope] = count
        self.scope_bytes.setdefault(scope, 0)
        stride = max(1, count // self.sample_limit)
        sampled = 0
        for index in range(count):
            reader.i64()
            state = reader.bytes()
            self.scope_bytes[scope] += len(state)
            if sampled < self.sample_limit and index % stride == 0:
                self.process_state(scope, state)
                sampled += 1

    def report(
        self, snapshot_size: int, metadata_size: int, runtime_size: int, elapsed: float
    ) -> None:
        print(f"snapshot_size_bytes {snapshot_size}")
        print(f"metadata_bytes {metadata_size}")
        print(f"runtime_bytes {runtime_size}")
        print(f"elapsed_sec {elapsed:.3f}")
        print()
        print("Scope byte counts")
        for scope in ("card", "note", "deck", "preset", "global"):
            if scope in self.scope_counts:
                print(
                    scope,
                    "states",
                    self.scope_counts[scope],
                    "state_bytes",
                    self.scope_bytes.get(scope, 0),
                    "sampled",
                    self.sampled_by_scope.get(scope, 0),
                )

        print()
        print("Singular energy kept")
        for rank in (1, 2, 4, 8):
            print(f"rank {rank}", summarize(self.rank_energy[rank]))

        print()
        print("Rank truncation relative Frobenius error")
        for rank in (1, 2, 4, 8):
            print(f"rank {rank}", summarize(self.rank_errors[rank]))

        print()
        print("Naive low-rank factor quantization relative error")
        for rank in (1, 2, 4):
            for bits in (4, 8):
                print(
                    f"rank{rank}_int{bits}", summarize(self.factor_errors[(rank, bits)])
                )

        print()
        print("Shift int4 relative error")
        print(summarize(self.shift_int4_errors))

        print()
        print("Per-state zlib level1 ratio sample")
        print(summarize(self.zlib_ratios))

        self.report_byte_estimates(runtime_size)

    def report_byte_estimates(self, runtime_size: int) -> None:
        total_layers = (
            self.scope_counts.get("card", 0) * MODULE_LAYERS["card"]
            + self.scope_counts.get("note", 0) * MODULE_LAYERS["note"]
            + self.scope_counts.get("deck", 0) * MODULE_LAYERS["deck"]
            + self.scope_counts.get("preset", 0) * MODULE_LAYERS["preset"]
            + self.scope_counts.get("global", 0) * MODULE_LAYERS["global"]
        )
        raw_layer_bytes = 2 * D_MODEL * 4 + HEADS * HEAD_SIZE * HEAD_SIZE * 4
        shift_int4_layer_bytes = 2 * ((D_MODEL + 1) // 2 + 4)
        print()
        print("Naive int4 low-rank byte estimates")
        print("total_layers", total_layers)
        for rank in (1, 2, 4, 8):
            factor_values = 2 * HEAD_SIZE * rank
            matrix_bytes = HEADS * (((factor_values + 1) // 2) + 2 * rank * 4)
            compressed_layer_bytes = shift_int4_layer_bytes + matrix_bytes
            module_bytes = sum(
                count
                * (
                    len(MODULE_MAGIC)
                    + 4
                    + MODULE_LAYERS[scope] * (3 * 4 + compressed_layer_bytes)
                )
                for scope, count in self.scope_counts.items()
            )
            print(
                f"rank{rank}",
                "layer_bytes",
                compressed_layer_bytes,
                "module_MB",
                f"{module_bytes / 1_000_000:.1f}",
                "module_ratio",
                f"{sum(self.scope_bytes.values()) / module_bytes:.1f}x",
                "snapshot_plus_runtime_MB",
                f"{(module_bytes + runtime_size) / 1_000_000:.1f}",
                "per_layer_ratio",
                f"{raw_layer_bytes / compressed_layer_bytes:.1f}x",
            )


def run(snapshot: Path, sample_limit: int) -> None:
    start = time.monotonic()
    probe = Probe(sample_limit)
    reader = Reader(snapshot)
    try:
        if reader.read(len(SNAPSHOT_MAGIC)) != SNAPSHOT_MAGIC:
            raise ValueError("invalid RWKV state cache snapshot header")
        metadata_size = len(reader.bytes())
        for scope in ("card", "note", "deck", "preset"):
            probe.process_state_map(reader, scope)

        marker = reader.u8()
        if marker == 1:
            state = reader.bytes()
            probe.scope_counts["global"] = 1
            probe.scope_bytes["global"] = len(state)
            probe.process_state("global", state)
        elif marker != 0:
            raise ValueError("invalid global state marker")

        runtime_marker = reader.u8()
        if runtime_marker == 1:
            runtime_size = reader.skip_bytes()
        elif runtime_marker == 0:
            runtime_size = 0
        else:
            raise ValueError("invalid runtime state marker")
    finally:
        reader.close()

    probe.report(
        snapshot.stat().st_size, metadata_size, runtime_size, time.monotonic() - start
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--sample-limit", type=int, default=256)
    args = parser.parse_args()
    run(args.snapshot, args.sample_limit)


if __name__ == "__main__":
    main()
