# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Measure an FSRS operation in a fresh process against a copied collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def copied_path(value: str) -> Path:
    path = Path(value).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if "Anki2" in path.parts or not (
        path.is_relative_to(Path("/private/tmp")) or path.is_relative_to(workspace)
    ):
        raise argparse.ArgumentTypeError(
            "path must be a temporary/workspace copy outside Anki2"
        )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--collection", required=True, type=copied_path)
    parser.add_argument("--parameters", required=True, type=copied_path)
    parser.add_argument("--work-dir", required=True, type=copied_path)
    parser.add_argument("--output", required=True, type=copied_path)
    parser.add_argument("--search", default="", help="Anki card search to benchmark")
    parser.add_argument(
        "--scenario",
        required=True,
        choices=["optimize", "optimize-health", "evaluate", "predict", "folds"],
    )
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    if not args.collection.is_file() or args.collection.name != "collection.anki2":
        parser.error("--collection must be a copied or imported collection.anki2")
    if (
        args.output in (args.collection, args.parameters)
        or args.output.suffix != ".json"
    ):
        parser.error("--output must be a separate JSON file")
    params = json.loads(args.parameters.read_text())
    if len(params) != 34:
        parser.error("--parameters must contain 34 FSRS-7 parameters")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    for relative_path in ("out/pylib", "pylib", "out/qt", "qt"):
        sys.path.insert(0, str(source_root / relative_path))
    from anki import _rsbridge, deck_config_pb2, scheduler_pb2
    from anki.collection import Collection

    if not Path(_rsbridge.__file__).resolve().is_relative_to(source_root):
        raise RuntimeError("native backend did not load from the requested build")
    if (
        not Path(sys.modules["anki.collection"].__file__)
        .resolve()
        .is_relative_to(source_root)
    ):
        raise RuntimeError("Python library did not load from the requested build")
    if sys.platform == "win32":
        raise RuntimeError("peak RSS measurement requires a POSIX platform")
    import resource

    version = deck_config_pb2.DeckConfig.Config.FsrsVersion.Value("FSRS_VERSION_SEVEN")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="fsrs-compact-", dir=args.work_dir
    ) as directory:
        collection_path = Path(directory) / "collection.anki2"
        shutil.copy2(args.collection, collection_path)
        col = Collection(str(collection_path))
        try:
            start = time.monotonic()
            result: dict[str, object]
            if args.scenario in ("optimize", "optimize-health"):
                optimize_response = col._backend.compute_fsrs_params(
                    scheduler_pb2.ComputeFsrsParamsRequest(
                        search=args.search,
                        current_params=params,
                        num_of_relearning_steps=1,
                        include_same_day_reviews=True,
                        fsrs_version=version,
                        health_check=args.scenario == "optimize-health",
                    )
                )
                result = {"targets": optimize_response.fsrs_items}
                digest = hashlib.sha256(
                    optimize_response.SerializeToString()
                ).hexdigest()
            elif args.scenario == "evaluate":
                evaluation_response = col._backend.evaluate_params_legacy(
                    scheduler_pb2.EvaluateParamsLegacyRequest(
                        search=args.search, params=params, include_same_day_reviews=True
                    )
                )
                result = {
                    "log_loss": evaluation_response.log_loss,
                    "rmse_bins": evaluation_response.rmse_bins,
                }
                digest = hashlib.sha256(
                    evaluation_response.SerializeToString()
                ).hexdigest()
            else:
                stored = col._backend.compute_fsrs_review_retrievability_calibration(
                    scheduler_pb2.ComputeFsrsReviewRetrievabilityCalibrationRequest(
                        search=args.search,
                        params=params,
                        num_of_relearning_steps=1,
                        include_same_day_reviews=True,
                        fsrs_version=version,
                        include_validation_folds=args.scenario == "folds",
                    )
                )
                result = {"stored": stored}
                digest = ""
            elapsed_ms = (time.monotonic() - start) * 1000
            peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (
                1024**2 if sys.platform == "darwin" else 1024
            )
            if args.scenario in ("predict", "folds"):
                rows = col.db.all(
                    "SELECT revlog_id, prediction, sample_role, fold_index, source "
                    "FROM search_stats_fsrs_review_retrievability "
                    "ORDER BY revlog_id, sample_role, fold_index, source"
                )
                result["rows"] = len(rows)
                digest = hashlib.sha256(
                    json.dumps(rows, separators=(",", ":")).encode()
                ).hexdigest()
            result.update(
                scenario=args.scenario,
                search=args.search,
                elapsed_ms=elapsed_ms,
                peak_rss_mib=peak_rss_mib,
                digest=digest,
            )
        finally:
            col.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
