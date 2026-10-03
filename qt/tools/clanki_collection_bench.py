# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Benchmark backend operations on a supplied backup copy, never a live profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def checksum(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def serialize(value: Any) -> Any:
    from google.protobuf.json_format import MessageToDict

    if hasattr(value, "DESCRIPTOR"):
        return MessageToDict(value, preserving_proto_field_name=True)
    return value


def safe_location(path: Path, workspace: Path) -> bool:
    return (
        path.is_relative_to(Path("/private/tmp")) or path.is_relative_to(workspace)
    ) and "Anki2" not in path.parts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=7)
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[2]
    root = args.source_root.resolve()
    backup = args.backup.resolve(strict=True)
    work = args.work_dir.resolve()
    output = args.output.resolve()
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    if backup.suffix != ".colpkg" or not safe_location(backup, workspace):
        parser.error("--backup must be an already-copied .colpkg under tmp/workspace")
    if not safe_location(work, workspace) or not safe_location(output, workspace):
        parser.error(
            "work directory and output must be under /private/tmp or workspace"
        )
    if output == backup or output.suffix != ".json":
        parser.error("output must be a JSON file separate from the backup")
    args.source_root, args.backup, args.work_dir, args.output = (
        root,
        backup,
        work,
        output,
    )
    return args


def main() -> None:
    args = parse_args()
    root, backup, work, output = (
        args.source_root,
        args.backup,
        args.work_dir,
        args.output,
    )

    # Load both the generated wrappers and native library from the requested build.
    sys.path.insert(0, str(root / "out" / "pylib"))
    sys.path.insert(0, str(root / "pylib"))
    from anki import _rsbridge
    from anki._backend import RustBackend
    from anki.collection import Collection
    from anki.deck_config_pb2 import UpdateDeckConfigsRequest
    from anki.decks import DeckId

    if not Path(sys.modules["anki.collection"].__file__).resolve().is_relative_to(root):
        raise RuntimeError("Anki was not imported from the requested source build")
    if not Path(_rsbridge.__file__).resolve().is_relative_to(root / "out" / "rust"):
        raise RuntimeError(
            "Native backend was not loaded from the requested source build"
        )

    work.mkdir(parents=True, exist_ok=True)
    # A fresh directory ensures import/copies cannot overwrite a prior collection.
    run_dir = Path(tempfile.mkdtemp(prefix="clanki-collection-", dir=work))
    fixture_dir = run_dir / "fixture"
    fixture_dir.mkdir()
    fixture = fixture_dir / "collection.anki2"
    backend = RustBackend(langs=["en"])
    backend.import_collection_package(
        col_path=str(fixture),
        backup_path=str(backup),
        media_folder=str(fixture_dir / "collection.media"),
        media_db=str(fixture_dir / "collection.media.db2"),
    )
    now = int(os.environ.get("ANKI_BENCH_NOW", str(int(time.time()))))

    def counts(col: Collection) -> dict[str, int]:
        return {"cards": col.card_count(), "notes": col.note_count()}

    def scheduling_snapshot(col: Collection, card_ids: list[int]) -> dict[str, Any]:
        digest = hashlib.sha256()
        intervals = hashlib.sha256()
        with_memory = 0
        for cid in card_ids:
            card = col._backend.get_card(cid)
            card.ClearField("mtime_secs")
            card.ClearField("usn")
            with_memory += int(card.HasField("memory_state"))
            digest.update(card.SerializeToString(deterministic=True))
            intervals.update(f"{cid}:{card.interval}:{card.due}:{card.queue};".encode())
        return {
            "checksum": digest.hexdigest(),
            "interval_checksum": intervals.hexdigest(),
            "cards": len(card_ids),
            "with_memory": with_memory,
        }

    col = Collection(str(fixture))
    try:
        workload = counts(col)
        candidates = []
        for deck in col.decks.all_names_and_ids(include_filtered=False):
            options = col.decks.get_deck_configs_for_update(DeckId(deck.id))
            preset = next(
                item.config
                for item in options.all_config
                if item.config.id == options.current_deck.config_id
            )
            if not options.fsrs or preset.config.fsrs_version != 0:
                continue
            card_ids = list(col.find_cards(f"did:{deck.id} is:review"))
            if card_ids:
                preferred = int("yomitan" in deck.name.lower()) * 2 + int(
                    "japan" in deck.name.lower()
                )
                candidates.append((preferred, len(card_ids), deck.id, deck.name))
        if not candidates:
            raise RuntimeError("Backup has no populated FSRS7 review deck")
        _, _, deck_id, deck_name = max(candidates)
        col.decks.set_current(DeckId(deck_id))
        options = col.decks.get_deck_configs_for_update(DeckId(deck_id))
        preset_id = options.current_deck.config_id
        preset = next(
            item.config for item in options.all_config if item.config.id == preset_id
        )
        affected_ids: list[int] = []
        for deck in col.decks.all_names_and_ids(include_filtered=False):
            deck_options = col.decks.get_deck_configs_for_update(DeckId(deck.id))
            if deck_options.current_deck.config_id == preset_id:
                affected_ids.extend(col.find_cards(f"did:{deck.id}"))
        affected_ids = sorted(set(affected_ids))
        before_state = scheduling_snapshot(col, affected_ids)
        day = col.sched.today
        selected: dict[str, Any] = {
            "deck": deck_name,
            "deck_id": deck_id,
            "preset": preset.name,
            "preset_id": preset_id,
            "affected_cards": len(affected_ids),
            "review_cards": len(col.find_cards(f"did:{deck_id} is:review")),
            "desired_retention_before": preset.config.desired_retention,
            "desired_retention_after": 0.91
            if abs(preset.config.desired_retention - 0.91) > 0.001
            else 0.9,
        }
    finally:
        col.close()

    def save_request(col: Collection) -> Any:
        options = col.decks.get_deck_configs_for_update(DeckId(deck_id))
        preset = next(
            item.config for item in options.all_config if item.config.id == preset_id
        )
        request = UpdateDeckConfigsRequest(target_deck_id=deck_id, configs=[preset])
        request.configs[0].config.desired_retention = selected[
            "desired_retention_after"
        ]
        request.limits.CopyFrom(options.current_deck.limits)
        for field in (
            "card_state_customizer",
            "new_cards_ignore_review_limit",
            "fsrs",
            "apply_all_parent_limits",
            "fsrs_health_check",
            "load_balancer_enabled",
            "fsrs_short_term_with_steps_enabled",
            "fsrs_learning_queues_disabled",
            "review_fuzz_enabled",
            "review_fuzz_base",
            "review_fuzz_factor_short",
            "review_fuzz_factor_mid",
            "review_fuzz_factor_long",
        ):
            setattr(request, field, getattr(options, field))
        request.fsrs_reschedule = False
        return request

    results: dict[str, Any] = {}
    for name in (
        "deck_due_tree",
        "congratulations_info",
        "empty_cards",
        "check_database",
        "preset_save",
    ):
        durations, invariants, clock_dependent_outputs = [], [], []
        for sample in range(args.samples):
            sample_dir = run_dir / f"{name}-{sample}"
            sample_dir.mkdir()
            sample_path = sample_dir / "collection.anki2"
            shutil.copy2(fixture, sample_path)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(fixture) + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(str(sample_path) + suffix))
            col = Collection(str(sample_path))
            try:
                if col.sched.today != day:
                    raise RuntimeError("Scheduler day changed; rerun both versions")
                request = save_request(col) if name == "preset_save" else None
                value: Any
                start = time.perf_counter()
                if name == "deck_due_tree":
                    value = col._backend.deck_tree(now=now)
                elif name == "congratulations_info":
                    value = col.sched.congratulations_info()
                elif name == "empty_cards":
                    value = col.get_empty_cards()
                elif name == "check_database":
                    value = list(col._backend.check_database())
                else:
                    value = col.decks.update_deck_configs(request)
                durations.append((time.perf_counter() - start) * 1000)
                normalized = serialize(value)
                if name == "congratulations_info":
                    clock_dependent_outputs.append(value.secs_until_next_learn)
                    normalized.pop("secs_until_next_learn", None)
                invariant: dict[str, Any] = {
                    "output_checksum": checksum(normalized),
                    "counts": counts(col),
                    "scheduler_day": col.sched.today,
                }
                if name == "check_database":
                    invariant["report"] = value
                if name == "preset_save":
                    invariant["scheduling"] = scheduling_snapshot(col, affected_ids)
                    if (
                        invariant["scheduling"]["interval_checksum"]
                        != before_state["interval_checksum"]
                    ):
                        raise RuntimeError(
                            "Preset save changed intervals despite disabled rescheduling"
                        )
                if invariant["counts"] != workload or invariant["scheduler_day"] != day:
                    raise RuntimeError(f"{name} changed counts or crossed day rollover")
                invariants.append(invariant)
            finally:
                col.close()
                shutil.rmtree(sample_dir)
        if any(item != invariants[0] for item in invariants):
            raise RuntimeError(f"{name} produced inconsistent sample outputs")
        quartiles = statistics.quantiles(durations, n=4, method="inclusive")
        results[name] = {
            "samples_ms": durations,
            "median_ms": statistics.median(durations),
            "q1_ms": quartiles[0],
            "q3_ms": quartiles[2],
            "invariants": invariants[0],
        }
        if clock_dependent_outputs:
            results[name]["secs_until_next_learn_per_sample"] = clock_dependent_outputs

    report = {
        "source_root": str(root),
        "backup": str(backup),
        "run_dir": str(run_dir),
        "samples": args.samples,
        "deck_tree_now": now,
        "scheduler_day": day,
        "workload": workload,
        "selected_preset": selected,
        "preset_scheduling_before": before_state,
        "results": results,
        "caveats": [
            "Set identical ANKI_BENCH_NOW for both runs; only deck_tree accepts an explicit clock.",
            "Rust wall clock cannot be frozen through supported APIs; scheduler-day rollover is rejected.",
            "Congratulations checksum excludes clock-dependent secs_until_next_learn; its actual values are reported separately.",
            "Preset save recomputes memory states with rescheduling disabled; intervals should remain unchanged.",
            "Fixture import, cloning, opening, request construction and checksums are outside measured time.",
            "Fresh collection connections do not clear the operating system filesystem cache.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(output), "results": results}, indent=2))


if __name__ == "__main__":
    main()
