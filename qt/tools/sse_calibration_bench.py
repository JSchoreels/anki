# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Measure calibration latency using a preserved build and an explicit backup copy.

Run each sample in a fresh process. `fold-start` cancels at the first training
iteration: it measures preparation/memory, not complete cross-validation time.
`sse-ui` uses a controlled slow SQL stub and a real Qt event loop.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def peak_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024**2 if sys.platform == "darwin" else 1024)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sse-module", type=Path)
    parser.add_argument(
        "--scenario",
        choices=["final-fit", "early-abort", "fold-start", "sse-ui"],
        required=True,
    )
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[2]
    for name in ("backup", "work_dir", "output"):
        path = getattr(args, name).resolve()
        if (
            not (
                path.is_relative_to(Path("/private/tmp"))
                or path.is_relative_to(workspace)
            )
            or "Anki2" in path.parts
        ):
            parser.error(
                f"--{name.replace('_', '-')} must be under tmp/workspace, outside any live profile"
            )
        setattr(args, name, path)
    if args.backup.suffix != ".colpkg" or not args.backup.is_file():
        parser.error("--backup must be an already-copied .colpkg")
    if args.output == args.backup or args.output.suffix != ".json":
        parser.error("--output must be a separate JSON file")
    if args.scenario == "sse-ui" and not args.sse_module:
        parser.error("--sse-module is required for sse-ui")
    args.source_root = args.source_root.resolve()
    return args


def ui_probe(module_path: Path, work: Path) -> dict[str, Any]:
    import aqt
    from aqt.qt import QApplication, QTimer, QWidget
    from aqt.taskman import TaskManager

    spec = importlib.util.spec_from_file_location("sse_bench_module", module_path)
    assert spec and spec.loader
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = QApplication.instance() or QApplication([])
    sql_on_main = []

    def first(_sql: str) -> tuple[int, int]:
        sql_on_main.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.35)
        return (1, 1)

    class Window(QWidget):
        def weakref(self) -> Any:
            return weakref.proxy(self)

        def _increase_background_ops(self) -> None:
            pass

        def _decrease_background_ops(self) -> None:
            pass

    mw: Any = Window()
    mw.col = SimpleNamespace(
        path=str(work / "synthetic.anki2"), mod=1, db=SimpleNamespace(first=first)
    )
    mw.taskman = TaskManager(mw)
    aqt.mw = mw
    module.ensure_calibration = lambda *args, **kwargs: {
        "generated": False,
        "warnings": [],
    }
    service = module.CalibrationService()
    ticks = [time.monotonic()]
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        service.start(mw, [1], work, "synthetic_cache")
        deadline = time.monotonic() + 5
        while not service.checked and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        for _ in range(4):
            app.processEvents()
            time.sleep(0.01)
        if not service.checked:
            raise RuntimeError("SSE did not complete")
        return {
            "max_heartbeat_gap_ms": max(b - a for a, b in zip(ticks, ticks[1:])) * 1000,
            "sql_on_main": sql_on_main,
        }
    finally:
        timer.stop()
        mw.taskman._collection_executor.shutdown()
        mw.taskman._no_collection_executor.shutdown()


def prediction_digest(col: Any, model: str) -> dict[str, Any]:
    rows = col.db.all(
        f"SELECT revlog_id, prediction, sample_role, fold_index, source FROM search_stats_{model}_review_retrievability ORDER BY revlog_id, sample_role, fold_index, source"
    )
    return {
        "rows": len(rows),
        "prediction_checksum": hashlib.sha256(
            json.dumps(rows, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def fsrs_probe(col: Any, scenario: str) -> dict[str, Any]:
    from anki import deck_config_pb2, scheduler_pb2

    cid = col.db.scalar(
        "SELECT cid FROM revlog GROUP BY cid ORDER BY count(*) DESC LIMIT 1"
    )
    preset = col.fsrs_preset_for_card(cid)
    request = scheduler_pb2.ComputeFsrsReviewRetrievabilityCalibrationRequest(
        search="",
        params=preset.params,
        fsrs_version=deck_config_pb2.DeckConfig.Config.FsrsVersion.Value(
            "FSRS_VERSION_" + preset.fsrs_version.upper()
        ),
        num_of_relearning_steps=1,
        include_same_day_reviews=True,
        include_validation_folds=scenario == "fold-start",
    )
    result: dict[str, Any] = {}
    start = time.monotonic()

    def compute() -> None:
        try:
            result["stored"] = (
                col._backend.compute_fsrs_review_retrievability_calibration(request)
            )
        except Exception as error:
            result["error"] = type(error).__name__ + ": " + str(error)
        result["elapsed_ms"] = (time.monotonic() - start) * 1000

    worker = threading.Thread(target=compute, daemon=True)
    worker.start()

    def abort() -> None:
        result["abort_at_ms"] = (time.monotonic() - start) * 1000
        col._backend.set_wants_abort()

    abort_timer = threading.Timer(0.02, abort) if scenario == "early-abort" else None
    if abort_timer:
        abort_timer.start()
    queries = []
    aborted = False
    while worker.is_alive():
        elapsed = time.monotonic() - start
        if elapsed > 180:
            raise RuntimeError("Calibration benchmark timed out")
        should_abort = False
        if scenario == "fold-start":
            value = col._backend.latest_progress()
            should_abort = (
                value.HasField("compute_params") and value.compute_params.current > 0
            )
        if should_abort and not aborted:
            abort()
            aborted = True
        query_start = time.monotonic()
        col.card_count()
        queries.append((time.monotonic() - query_start) * 1000)
        time.sleep(0.01)
    worker.join()
    if abort_timer:
        abort_timer.cancel()
        abort_timer.join()
    result["max_card_query_ms"] = max(queries, default=0)
    result["query_samples"] = len(queries)
    if scenario == "fold-start" and not aborted:
        raise RuntimeError("No fold training progress was observed")
    result.update(prediction_digest(col, "fsrs"))
    return result


def main() -> None:
    args = parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    for path in ("out/pylib", "pylib", "out/qt", "qt"):
        sys.path.insert(0, str(args.source_root / path))
    from anki import _rsbridge
    from anki._backend import RustBackend
    from anki.collection import Collection

    for name in ("anki.collection", "aqt"):
        if name in sys.modules and not Path(
            sys.modules[name].__file__
        ).resolve().is_relative_to(args.source_root):
            raise RuntimeError(f"{name} did not load from the requested build")
    if not Path(_rsbridge.__file__).resolve().is_relative_to(args.source_root):
        raise RuntimeError("Native backend did not load from the requested build")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sse-calibration-", dir=args.work_dir))
    if args.scenario == "sse-ui":
        result = ui_probe(args.sse_module, work)
    else:
        backend = RustBackend(langs=["en"])
        db_path = work / "collection.anki2"
        backend.import_collection_package(
            col_path=str(db_path),
            backup_path=str(args.backup),
            media_folder=str(work / "media"),
            media_db=str(work / "media.db2"),
        )
        col = Collection(str(db_path))
        try:
            workload = {
                "cards": col.card_count(),
                "rated_reviews": col.db.scalar(
                    "SELECT count(*) FROM revlog WHERE ease BETWEEN 1 AND 4 AND type IN (0,1,2,3)"
                ),
            }
            result = fsrs_probe(col, args.scenario)
            result.update(workload)
        finally:
            col.close()
    result.update(
        scenario=args.scenario,
        source_root=str(args.source_root),
        peak_rss_mib=peak_mib(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
