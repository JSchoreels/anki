# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export canonical RWKV replay inputs from an already-copied backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from anki._backend import RustBackend
from anki.collection import Collection
from aqt import rwkv_scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-copy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deck-match", default="Yomitan")
    args = parser.parse_args()
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
        history = rwkv_scheduler._historical_rwkv_review_inputs(
            SimpleNamespace(mw=SimpleNamespace(col=col))
        )
        rows = [
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
                review.identity.deck_id in deck_ids,
            ]
            for review_id, review in zip(
                history.review_ids, history.reviews, strict=True
            )
        ]
        output.write_text(
            json.dumps(
                {
                    "backup": str(backup),
                    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "collection_copy": str(collection),
                    "decks": [deck.name for deck in decks],
                    "history_hash": history.history_hash,
                    "replay_key": history.replay_key,
                    "model_sha256": hashlib.sha256(
                        (
                            workspace
                            / "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"
                        ).read_bytes()
                    ).hexdigest(),
                    "rows": rows,
                }
            ),
            encoding="utf8",
        )
        print(f"Exported {len(rows)} canonical reviews to {output}", flush=True)
        print(f"Scoring decks: {[deck.name for deck in decks]}", flush=True)
    finally:
        col.close()


if __name__ == "__main__":
    main()
