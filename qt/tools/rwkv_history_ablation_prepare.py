# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export offline RWKV replay inputs from an already-copied backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from anki._backend import RustBackend
from anki.collection import Collection
from aqt import rwkv_history, rwkv_scheduler


def reset_rows(
    rows: Sequence[Sequence[int]],
    resets: dict[int, int],
    *,
    keep_previous: bool,
) -> tuple[list[Sequence[int]], dict[int, int]]:
    """Retain every eligible answer, or only answers after each latest reset."""
    retained = [row for row in rows if keep_previous or row[0] > resets.get(row[1], 0)]
    # Keep later Learning answers append-only. Only the first retained Learning
    # answer is a start; later starts must not silently remove experiment rows.
    cutoffs: dict[int, int] = {}
    for row in retained:
        cutoffs.setdefault(row[1], row[0])
    return retained, cutoffs


def export_rows(
    history: rwkv_scheduler.RwkvHistoricalReviewInputs,
    selected: set[int],
) -> list[list[object]]:
    return [
        [
            review_id,
            review.identity.card_id,
            review.identity.note_id,
            review.identity.deck_id,
            review.identity.preset_id,
            review.ease,
            review.duration_millis,
            review.card_type,
            review.day_offset,
            review.current_elapsed_days,
            review.current_elapsed_seconds,
            review_id in selected,
        ]
        for review_id, review in zip(history.review_ids, history.reviews, strict=True)
    ]


def reset_comparison(
    col: Collection,
    first_review_elapsed_source: rwkv_scheduler.RwkvFirstReviewElapsedSource,
) -> dict[str, object]:
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    raw = col.db.all(
        """
select r.id, r.cid, c.nid,
       case when c.odid != 0 then c.odid else c.did end,
       r.ease, r.time, r.type, cast(r.ivl as integer), cast(r.factor as integer)
from revlog r join cards c on c.id = r.cid
where r.ease between 1 and 4 and r.type in (0, 1, 2, 3, 4, 5)
  and not (r.type = 3 and r.factor = 0)
order by r.id
"""
    )
    resets = {
        int(card_id): int(review_id)
        for card_id, review_id in col.db.all(
            """
select r.cid, max(r.id)
from revlog r join cards c on c.id = r.cid
where r.type = 4 and r.ease = 0 and r.factor = 0
group by r.cid
"""
        )
    }
    selected = {row[0] for row in raw if row[1] in resets and row[0] > resets[row[1]]}
    if not selected:
        raise ValueError("no eligible answers after a recorded Forget operation")
    variants = {}
    for mode, keep_previous in (("all_reviews", True), ("after_reset_only", False)):
        retained, cutoffs = reset_rows(raw, resets, keep_previous=keep_previous)
        # Use the production Python adapter for timing and preset routing, but
        # supply the experiment's membership instead of canonical retention.
        with (
            patch.object(rwkv_history, "full_review_inputs", return_value=None),
            patch.object(
                rwkv_scheduler, "_historical_rwkv_review_rows", return_value=retained
            ),
            patch.object(
                rwkv_scheduler,
                "_rwkv_preserved_learning_start_cutoffs",
                return_value=cutoffs,
            ),
        ):
            history = rwkv_scheduler._historical_rwkv_review_inputs(
                reviewer, first_review_elapsed_source=first_review_elapsed_source
            )
        assert history.review_ids == [row[0] for row in retained]
        variants[mode] = export_rows(history, selected)
    return {
        "reset_cards": len(resets),
        "reset_review_ids": resets,
        "scored_cards": len({row[1] for row in raw if row[0] in selected}),
        "scored_answers": len(selected),
        "first_review_elapsed_source": first_review_elapsed_source.value,
        "first_review_elapsed_config": (
            rwkv_scheduler._rwkv_first_review_elapsed_config_key(reviewer)
        ),
        "dynamic_preset_replay": (
            rwkv_scheduler._rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
        ),
        "variants": variants,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-copy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deck-match", default="Yomitan")
    parser.add_argument("--reset-comparison", action="store_true")
    parser.add_argument(
        "--first-review-elapsed",
        choices=("deck_config", "missing"),
        default="deck_config",
    )
    args = parser.parse_args()
    elapsed_source = rwkv_scheduler.RwkvFirstReviewElapsedSource(
        args.first_review_elapsed
    )
    backup = args.backup_copy.resolve(strict=True)
    output = args.output.resolve()
    workspace = Path(__file__).resolve().parents[2]
    for path in (backup, output):
        if not any(
            path.is_relative_to(root) for root in (Path("/private/tmp"), workspace)
        ):
            parser.error(
                "backup copy and output must be under /private/tmp or workspace"
            )
    if backup.suffix != ".colpkg" or output.suffix != ".json" or backup == output:
        parser.error("supply a copied .colpkg and a separate .json output")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rwkv-ablation-", dir=output.parent))
    collection = work / "collection.anki2"
    RustBackend(langs=["en"]).import_collection_package(
        col_path=str(collection),
        backup_path=str(backup),
        media_folder=str(work / "collection.media"),
        media_db=str(work / "collection.media.db2"),
    )
    col = Collection(str(collection))
    try:
        decks = [
            deck
            for deck in col.decks.all_names_and_ids()
            if args.deck_match.casefold() in deck.name.casefold()
        ]
        if not decks:
            parser.error(f"no decks match {args.deck_match!r}")
        deck_ids = {int(deck.id) for deck in decks}
        if args.reset_comparison:
            experiment = reset_comparison(col, elapsed_source)
        else:
            history = rwkv_scheduler._historical_rwkv_review_inputs(
                SimpleNamespace(mw=SimpleNamespace(col=col)),
                first_review_elapsed_source=elapsed_source,
            )
            selected = {
                review_id
                for review_id, review in zip(
                    history.review_ids, history.reviews, strict=True
                )
                if review.identity.deck_id in deck_ids
            }
            experiment = {
                "history_hash": history.history_hash,
                "replay_key": history.replay_key,
                "rows": export_rows(history, selected),
            }
        output.write_text(
            json.dumps(
                {
                    "backup": str(backup),
                    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "collection_copy": str(collection),
                    "decks": [deck.name for deck in decks],
                    "model_sha256": hashlib.sha256(
                        (
                            workspace
                            / "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"
                        ).read_bytes()
                    ).hexdigest(),
                    **experiment,
                }
            ),
            encoding="utf8",
        )
        if args.reset_comparison:
            print(
                f"Exported reset comparison: {experiment['scored_answers']} answers "
                f"on {experiment['scored_cards']} cards to {output}",
                flush=True,
            )
        else:
            print(
                f"Exported {len(history.reviews)} canonical reviews to {output}",
                flush=True,
            )
            print(f"Scoring decks: {[deck.name for deck in decks]}", flush=True)
    finally:
        col.close()


if __name__ == "__main__":
    main()
