# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export review-order experiment inputs from an already-copied backup.

Writes the full replay history (same rows as the history ablation export) and
every non-suspended review card as a workload-simulation input row, built by
the production Python adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# The script directory is on sys.path when run as a script.
from rwkv_history_ablation_prepare import export_rows  # type: ignore[import-not-found]

from anki._backend import RustBackend
from anki.collection import Collection
from anki.consts import CARD_TYPE_REV
from aqt import rwkv_scheduler, rwkv_srs_benchmark


def export_review_inputs(reviewer: SimpleNamespace) -> list[list[Any]]:
    """Every non-suspended review card as a 20-field workload input row."""
    input_build = rwkv_scheduler._rwkv_review_input_batches_for_search(
        reviewer=reviewer,
        search="",
        include_suspended_review=False,
        include_new_cards=False,
        batch_size_override=None,
    )
    if input_build is None:
        raise ValueError("unable to load RWKV review inputs")
    inputs = rwkv_scheduler._rwkv_simulation_inputs(input_build)
    rows = [
        list(rwkv_srs_benchmark._workload_input_row(review_input))
        for _, review_input, _ in sorted(inputs, key=lambda item: item[0])
        if review_input.card_type == CARD_TYPE_REV
    ]
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-copy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
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
    work = Path(tempfile.mkdtemp(prefix="rwkv-review-order-", dir=output.parent))
    collection = work / "collection.anki2"
    RustBackend(langs=["en"]).import_collection_package(
        col_path=str(collection),
        backup_path=str(backup),
        media_folder=str(work / "collection.media"),
        media_db=str(work / "collection.media.db2"),
    )
    col = Collection(str(collection))
    try:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
        history = rwkv_scheduler._historical_rwkv_review_inputs(
            reviewer, first_review_elapsed_source=elapsed_source
        )
        rows = export_rows(history, set(history.review_ids))
        inputs = export_review_inputs(reviewer)
        output.write_text(
            json.dumps(
                {
                    "backup": str(backup),
                    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "collection_copy": str(collection),
                    "today": col.sched.today,
                    "history_hash": history.history_hash,
                    "replay_key": history.replay_key,
                    "rows": rows,
                    "inputs": inputs,
                }
            ),
            encoding="utf8",
        )
        print(
            f"Exported {len(rows)} history rows and {len(inputs)} review cards "
            f"to {output}",
            flush=True,
        )
    finally:
        col.close()


if __name__ == "__main__":
    main()
