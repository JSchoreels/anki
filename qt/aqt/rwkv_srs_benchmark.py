# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from aqt.rwkv_scheduler import (
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewerBackend,
    RwkvReviewInput,
    RwkvReviewPrediction,
    interval_from_recall_curve,
    rwkv_review_identity,
    rwkv_review_input,
)


class SrsBenchmarkRwkvReviewerBackend(RwkvReviewerBackend):
    """Optional bridge to the RWKV RNN runner from srs-benchmark."""

    def __init__(
        self,
        *,
        benchmark_path: str | Path | None = None,
        model_path: str | Path | None = None,
        device: str = "cpu",
        dtype: str = "float",
        target_retention: float = 0.9,
        max_interval_days: int = 36500,
        process: object | None = None,
        row_factory: Callable[[dict[str, object]], object] | None = None,
    ) -> None:
        if process is None:
            if benchmark_path is None or model_path is None:
                raise ValueError("benchmark_path and model_path are required")
            process, row_factory = _load_srs_benchmark_process(
                benchmark_path=Path(benchmark_path),
                model_path=Path(model_path),
                device=device,
                dtype=dtype,
            )

        self._process: Any = process
        self._row_builder = SrsBenchmarkReviewRowBuilder(row_factory or dict)
        self._target_retention = target_retention
        self._max_interval_days = max_interval_days
        self._curves: dict[int, object] = {}

    def predict_review(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return None

        review_input = rwkv_review_input(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=None,
        )
        probability = self._process.imm_predict(self._row_builder.row_for(review_input))
        return RwkvReviewPrediction(
            retrievability=_probability_as_float(probability),
            interval_overrides=RwkvIntervalOverride(
                good=self._good_interval_override(review_input)
            ),
        )

    def review_answered(
        self,
        *,
        reviewer: object,
        card: object,
        ease: int,
    ) -> None:
        identity = rwkv_review_identity(reviewer, card)
        if identity is None:
            return

        review_input = rwkv_review_input(
            reviewer=reviewer,
            card=card,
            identity=identity,
            ease=ease,
        )
        curve = self._process.process_row(self._row_builder.row_for(review_input))
        if curve is not None:
            self._curves[identity.card_id] = curve

    def _good_interval_override(self, review_input: RwkvReviewInput) -> int | None:
        curve = self._curves.get(review_input.identity.card_id)
        if curve is None:
            return None

        return interval_from_recall_curve(
            [
                RwkvRecallPoint(
                    elapsed_days=day,
                    retrievability=_probability_as_float(
                        self._process.predict_func(curve, day * 86_400)
                    ),
                )
                for day in _interval_search_days(self._max_interval_days)
            ],
            target_retention=self._target_retention,
            max_interval_days=self._max_interval_days,
        )


class SrsBenchmarkReviewRowBuilder:
    def __init__(self, row_factory: Callable[[dict[str, object]], object]) -> None:
        self._row_factory = row_factory

    def row_for(self, review_input: RwkvReviewInput) -> object:
        identity = review_input.identity
        elapsed_seconds = _elapsed_seconds(review_input)
        elapsed_days = _elapsed_days(review_input, elapsed_seconds)

        return self._row_factory(
            {
                "card_id": identity.card_id,
                "note_id": identity.note_id,
                "deck_id": identity.deck_id,
                "preset_id": identity.preset_id,
                "elapsed_days": elapsed_days,
                "elapsed_seconds": elapsed_seconds,
                "day_offset": review_input.day_offset or 0,
                "duration": _duration_seconds(review_input),
                "state": review_input.card_type or 0,
                "rating": review_input.ease or 1,
            }
        )


def _load_srs_benchmark_process(
    *,
    benchmark_path: Path,
    model_path: Path,
    device: str,
    dtype: str,
) -> tuple[object, Callable[[dict[str, object]], object]]:
    sys.path.insert(0, str(benchmark_path))
    _install_srs_benchmark_import_shims()
    import pandas as pd  # type: ignore[import-untyped, import-not-found]
    import torch  # type: ignore[import-not-found]
    from rwkv.run_as_rnn import RNNProcess  # type: ignore[import-not-found]

    torch_dtype = {
        "float": torch.float32,
        "float32": torch.float32,
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
    }[dtype]
    return (
        RNNProcess(
            path=model_path,
            device=torch.device(device),
            dtype=torch_dtype,
        ),
        lambda row: pd.Series(row, dtype="float64"),
    )


def _install_srs_benchmark_import_shims() -> None:
    if not _module_available("tomli"):
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            pass
        else:
            sys.modules["tomli"] = tomllib
    if not _module_available("lmdb"):
        sys.modules["lmdb"] = types.ModuleType("lmdb")


def _module_available(module_name: str) -> bool:
    return (
        module_name in sys.modules or importlib.util.find_spec(module_name) is not None
    )


def _elapsed_seconds(review_input: RwkvReviewInput) -> int:
    if review_input.current_elapsed_seconds is not None:
        return review_input.current_elapsed_seconds
    if review_input.current_elapsed_days is not None:
        return review_input.current_elapsed_days * 86_400
    return -1


def _elapsed_days(review_input: RwkvReviewInput, elapsed_seconds: int) -> int:
    if review_input.current_elapsed_days is not None:
        return review_input.current_elapsed_days
    if elapsed_seconds >= 0:
        return elapsed_seconds // 86_400
    return -1


def _duration_seconds(review_input: RwkvReviewInput) -> float:
    if review_input.duration_millis is None:
        return 0.0
    return review_input.duration_millis / 1000


def _interval_search_days(max_interval_days: int) -> list[int]:
    if max_interval_days <= 30:
        return list(range(1, max_interval_days + 1))

    days = list(range(1, 31))
    day = 45
    while day < max_interval_days:
        days.append(day)
        day = int(day * 1.5)
    days.append(max_interval_days)
    return days


def _probability_as_float(probability: object) -> float:
    detach = getattr(probability, "detach", None)
    if callable(detach):
        probability = detach()

    cpu = getattr(probability, "cpu", None)
    if callable(cpu):
        probability = cpu()

    item = getattr(probability, "item", None)
    if callable(item):
        return float(item())

    return float(cast(Any, probability))
