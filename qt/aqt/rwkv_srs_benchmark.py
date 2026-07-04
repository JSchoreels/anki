# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import importlib.util
import logging
import struct
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from aqt.rwkv_scheduler import (
    RwkvBackendCacheSnapshot,
    RwkvIntervalOverride,
    RwkvRecallPoint,
    RwkvReviewCandidate,
    RwkvReviewerBackend,
    RwkvReviewInput,
    RwkvReviewPrediction,
    RwkvReviewPredictionRequest,
    RwkvReviewTransition,
    RwkvStatefulReviewerBackend,
    RwkvWarmUpProgress,
    RwkvWarmUpProgressCallback,
    interval_from_recall_curve,
    rwkv_review_identity,
    rwkv_review_input,
)

logger = logging.getLogger(__name__)

_PACKED_PREDICTION_REQUEST_MAGIC = b"ARWKVPR1"
_PACKED_WARM_UP_REVIEW_MAGIC = b"ARWKVWU1"
_PACKED_PREDICTION_REQUEST_HEADER = struct.Struct("<8sI")
_PACKED_PREDICTION_REQUEST_ROW = struct.Struct("<IqqqqBBqqqqqffff")
_RUST_WARMUP_CHUNK_SIZE = 4096


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

    def warm_up(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        progress: RwkvWarmUpProgressCallback | None = None,
    ) -> None:
        total = len(reviews)
        report_every = _warmup_progress_interval(total)
        _report_warmup_progress(progress, processed=0, total=total)

        for processed, review_input in enumerate(reviews, start=1):
            if review_input.ease is None:
                if processed == total or processed % report_every == 0:
                    _report_warmup_progress(
                        progress,
                        processed=processed,
                        total=total,
                    )
                continue

            if prediction_recorder is not None and review_ids is not None:
                review_id = (
                    review_ids[processed - 1]
                    if processed - 1 < len(review_ids)
                    else None
                )
                if isinstance(review_id, int):
                    probability = self._process.imm_predict(
                        self._row_builder.row_for(
                            replace(
                                review_input,
                                is_query=True,
                                ease=None,
                                duration_millis=None,
                            )
                        )
                    )
                    prediction_recorder(review_id, _probability_as_float(probability))

            curve = self._process.process_row(self._row_builder.row_for(review_input))
            if curve is not None:
                self._curves[review_input.identity.card_id] = curve

            if processed == total or processed % report_every == 0:
                _report_warmup_progress(progress, processed=processed, total=total)

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
        intervals = self._interval_overrides(review_input)
        s90s = self._s90_overrides(review_input)
        return RwkvReviewPrediction(
            retrievability=_probability_as_float(probability),
            current_interval=intervals.good,
            current_s90=s90s.good,
            interval_overrides=intervals,
            s90_overrides=s90s,
        )

    def predict_review_retrievability(
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
        return RwkvReviewPrediction(retrievability=_probability_as_float(probability))

    def predict_reviews(
        self,
        candidates: Sequence[RwkvReviewCandidate],
    ) -> Sequence[RwkvReviewPrediction | None]:
        inputs_by_index: list[tuple[int, RwkvReviewInput]] = []
        rows = []
        predictions: list[RwkvReviewPrediction | None] = [None] * len(candidates)

        for index, candidate in enumerate(candidates):
            identity = rwkv_review_identity(candidate.reviewer, candidate.card)
            if identity is None:
                continue

            review_input = rwkv_review_input(
                reviewer=candidate.reviewer,
                card=candidate.card,
                identity=identity,
                ease=None,
            )
            inputs_by_index.append((index, review_input))
            rows.append(self._row_builder.row_for(review_input))

        if not rows:
            return predictions

        imm_predict_many = getattr(self._process, "imm_predict_many", None)
        probabilities = (
            imm_predict_many(rows)
            if callable(imm_predict_many)
            else [self._process.imm_predict(row) for row in rows]
        )

        for (index, review_input), probability in zip(
            inputs_by_index, probabilities, strict=True
        ):
            intervals = self._interval_overrides(review_input)
            s90s = self._s90_overrides(review_input)
            predictions[index] = RwkvReviewPrediction(
                retrievability=_probability_as_float(probability),
                current_interval=intervals.good,
                current_s90=s90s.good,
                interval_overrides=intervals,
                s90_overrides=s90s,
            )

        return predictions

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

    def _interval_overrides(
        self, review_input: RwkvReviewInput
    ) -> RwkvIntervalOverride:
        return self._curve_interval_overrides(
            review_input,
            review_input.target_retentions,
        )

    def _s90_overrides(self, review_input: RwkvReviewInput) -> RwkvIntervalOverride:
        return self._curve_interval_overrides(
            review_input,
            (0.9, 0.9, 0.9, 0.9),
        )

    def _curve_interval_overrides(
        self,
        review_input: RwkvReviewInput,
        target_retentions: tuple[float | None, ...],
    ) -> RwkvIntervalOverride:
        curve = self._curves.get(review_input.identity.card_id)
        if curve is None:
            return RwkvIntervalOverride()

        points = [
            RwkvRecallPoint(
                elapsed_days=day,
                retrievability=_probability_as_float(
                    self._process.predict_func(curve, day * 86_400)
                ),
            )
            for day in _interval_search_days(self._max_interval_days)
        ]

        intervals = [
            interval_from_recall_curve(
                points,
                target_retention=_valid_target_retention(
                    target_retention,
                    fallback=self._target_retention,
                ),
                max_interval_days=self._max_interval_days,
            )
            for target_retention in target_retentions
        ]
        return RwkvIntervalOverride(
            again=intervals[0],
            hard=intervals[1],
            good=intervals[2],
            easy=intervals[3],
        )


def _valid_target_retention(value: object, *, fallback: float) -> float:
    if (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= 1
    ):
        return float(value)
    return fallback


class EmbeddedRwkvReviewerBackend(RwkvStatefulReviewerBackend):
    """RWKV backend using Anki's embedded Rust inference-only runner."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "cpu",
        dtype: str = "float",
        target_retention: float = 0.9,
        max_interval_days: int = 36500,
    ) -> None:
        del device, dtype
        super().__init__(
            _RustRwkvRuntime(
                model_path=Path(model_path),
                target_retention=target_retention,
                max_interval_days=max_interval_days,
            ),
        )


class _RustRwkvRuntime:
    def __init__(
        self,
        *,
        model_path: Path,
        target_retention: float,
        max_interval_days: int,
    ) -> None:
        from anki import _rsbridge

        rwkv_inference = getattr(_rsbridge, "RwkvInference")
        self._process = rwkv_inference(
            str(model_path),
            target_retention,
            max_interval_days,
        )
        self._process_lock = threading.RLock()

    def _locked_process(self) -> Any:
        lock = getattr(self, "_process_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._process_lock = lock
        return lock

    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition:
        identity = review_input.identity
        with self._locked_process():
            (
                retrievability,
                current_interval,
                current_s90,
                intervals,
                s90s,
                next_card_state,
                next_note_state,
                next_deck_state,
                next_preset_state,
                next_global_state,
            ) = self._process.review(
                identity.card_id,
                identity.note_id,
                identity.deck_id,
                identity.preset_id,
                review_input.is_query,
                review_input.ease,
                review_input.duration_millis,
                review_input.card_type,
                review_input.day_offset,
                review_input.current_elapsed_days,
                review_input.current_elapsed_seconds,
                *review_input.target_retentions,
                _state_bytes(card_state),
                _state_bytes(note_state),
                _state_bytes(deck_state),
                _state_bytes(preset_state),
                _state_bytes(global_state),
            )

        return RwkvReviewTransition(
            prediction=RwkvReviewPrediction(
                retrievability=float(retrievability),
                current_interval=_optional_interval(current_interval),
                current_s90=_optional_interval(current_s90),
                interval_overrides=_interval_override_from_tuple(intervals),
                s90_overrides=_interval_override_from_tuple(s90s),
            ),
            card_state=next_card_state,
            note_state=next_note_state,
            deck_state=next_deck_state,
            preset_state=next_preset_state,
            global_state=next_global_state,
        )

    def warm_up_reviews(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        progress: RwkvWarmUpProgressCallback | None = None,
    ) -> RwkvBackendCacheSnapshot:
        total = len(reviews)
        backend_chunk_size = _rust_warmup_chunk_size(total)
        _report_warmup_progress(progress, processed=0, total=total)
        record_predictions = prediction_recorder is not None and review_ids is not None
        processed = 0
        warm_up_packed = getattr(self._process, "warm_up_reviews_packed", None)

        with self._locked_process():
            while processed < total:
                chunk = reviews[processed : processed + backend_chunk_size]
                if callable(warm_up_packed):
                    predictions = warm_up_packed(
                        _packed_warm_up_reviews(chunk),
                        record_predictions,
                    )
                else:
                    predictions = self._process.warm_up_reviews(
                        [_review_input_row(review_input) for review_input in chunk],
                        record_predictions,
                    )
                if record_predictions:
                    for index, retrievability in predictions:
                        review_index = processed + int(index)
                        if 0 <= review_index < len(review_ids):
                            prediction_recorder(
                                review_ids[review_index],
                                retrievability,
                            )

                processed += len(chunk)
                _report_warmup_progress(progress, processed=processed, total=total)

            (
                card_states,
                note_states,
                deck_states,
                preset_states,
                global_state,
                runtime_state,
            ) = self._process.warm_up_snapshot()
        return RwkvBackendCacheSnapshot(
            card_states=dict(card_states),
            note_states=dict(note_states),
            deck_states=dict(deck_states),
            preset_states=dict(preset_states),
            global_state=global_state,
            runtime_state=runtime_state,
        )

    def reset_warm_up_state(self) -> None:
        reset = getattr(self._process, "reset_warm_up_state", None)
        if callable(reset):
            with self._locked_process():
                reset()

    def predict_many(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        predict_many = getattr(self._process, "predict_many", None)
        if not callable(predict_many):
            return [
                self.review(
                    review_input=request.review_input,
                    card_state=request.card_state,
                    note_state=request.note_state,
                    deck_state=request.deck_state,
                    preset_state=request.preset_state,
                    global_state=request.global_state,
                ).prediction
                for request in requests
            ]

        build_start = time.monotonic()
        rows = [
            (
                request.review_input.identity.card_id,
                request.review_input.identity.note_id,
                request.review_input.identity.deck_id,
                request.review_input.identity.preset_id,
                request.review_input.is_query,
                request.review_input.ease,
                request.review_input.duration_millis,
                request.review_input.card_type,
                request.review_input.day_offset,
                request.review_input.current_elapsed_days,
                request.review_input.current_elapsed_seconds,
                *request.review_input.target_retentions,
                _state_bytes(request.card_state),
                _state_bytes(request.note_state),
                _state_bytes(request.deck_state),
                _state_bytes(request.preset_state),
                _state_bytes(request.global_state),
            )
            for request in requests
        ]
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust batch bridge started: requests=%s build_elapsed_ms=%.1f",
            len(requests),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(rows)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(requests):
            raise ValueError("RWKV Rust batch prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust batch predicted: requests=%s "
            "build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(requests),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )

        return [
            RwkvReviewPrediction(
                retrievability=float(retrievability),
                current_interval=_optional_interval(current_interval),
                current_s90=_optional_interval(current_s90),
                interval_overrides=_interval_override_from_tuple(intervals),
                s90_overrides=_interval_override_from_tuple(s90s),
            )
            for retrievability, current_interval, current_s90, intervals, s90s in outputs
        ]

    def predict_retrievability_many(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[float]:
        predict_many_packed = getattr(
            self._process,
            "predict_retrievability_many_packed",
            None,
        )
        predict_many_tuple = getattr(self._process, "predict_retrievability_many", None)
        if not callable(predict_many_packed) and not callable(predict_many_tuple):
            return [
                float(prediction.retrievability)
                if prediction is not None and prediction.retrievability is not None
                else float("nan")
                for prediction in self.predict_many(requests)
            ]

        build_start = time.monotonic()
        use_packed = not callable(predict_many_tuple) and callable(predict_many_packed)
        payload = (
            _packed_prediction_requests(requests)
            if use_packed
            else [_prediction_request_row(request) for request in requests]
        )
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust retrievability batch bridge started: "
            "requests=%s packed=%s build_elapsed_ms=%.1f",
            len(requests),
            use_packed,
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = (
                predict_many_packed(*payload)
                if use_packed
                else predict_many_tuple(payload)
            )
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(requests):
            raise ValueError("RWKV Rust retrievability prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust retrievability batch predicted: requests=%s "
            "packed=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(requests),
            use_packed,
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )

        return [float(retrievability) for retrievability in outputs]

    def predict_retrievability_many_after_review(
        self,
        *,
        answer: RwkvReviewInput,
        query_inputs: Sequence[RwkvReviewInput],
        snapshot: RwkvBackendCacheSnapshot,
    ) -> Sequence[float]:
        predict_many = getattr(
            self._process,
            "predict_retrievability_many_after_review",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV future retrievability prediction is unavailable")

        build_start = time.monotonic()
        answer_row = _review_input_row(answer)
        query_rows = [_review_input_row(review_input) for review_input in query_inputs]
        snapshot_row = _workload_snapshot(snapshot)
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust future retrievability batch bridge started: "
            "requests=%s build_elapsed_ms=%.1f",
            len(query_rows),
            build_elapsed_ms,
        )
        with self._locked_process():
            outputs = predict_many(answer_row, query_rows, snapshot_row)
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        if len(outputs) != len(query_inputs):
            raise ValueError("RWKV future retrievability prediction count mismatch")

        logger.debug(
            "RWKV embedded Rust future retrievability batch predicted: "
            "requests=%s build_elapsed_ms=%.1f bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(query_rows),
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return [float(retrievability) for retrievability in outputs]

    def simulate_workload(
        self,
        *,
        inputs: Sequence[tuple[int, RwkvReviewInput, int]],
        snapshot: RwkvBackendCacheSnapshot,
        min_dr: int,
        max_dr: int,
        target_dr_step: int,
        days_to_simulate: int,
        review_limit: int,
        state_update_interval: int,
        review_model: object,
        progress: Callable[[int, int], None] | None = None,
    ) -> object:
        simulate_workload = getattr(self._process, "simulate_workload", None)
        if not callable(simulate_workload):
            raise ValueError("RWKV Rust runtime does not support workload simulation")

        build_start = time.monotonic()
        rows = [_workload_input_row(review_input) for _, review_input, _ in inputs]
        grade_seconds = tuple(getattr(review_model, "grade_seconds"))
        bucket_probabilities = _workload_bucket_probabilities(review_model)
        build_elapsed_ms = (time.monotonic() - build_start) * 1000
        predict_start = time.monotonic()
        logger.debug(
            "RWKV embedded Rust workload simulation bridge started: "
            "inputs=%s dr=%s..%s step=%s days=%s state_update_interval=%s "
            "build_elapsed_ms=%.1f",
            len(rows),
            min_dr,
            max_dr,
            target_dr_step,
            days_to_simulate,
            state_update_interval,
            build_elapsed_ms,
        )
        with self._locked_process():
            output = simulate_workload(
                rows,
                _workload_snapshot(snapshot),
                int(min_dr),
                int(max_dr),
                int(target_dr_step),
                int(days_to_simulate),
                int(review_limit),
                int(state_update_interval),
                grade_seconds,
                bucket_probabilities,
                progress,
            )
        predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
        logger.debug(
            "RWKV embedded Rust workload simulation bridge finished: "
            "inputs=%s dr=%s..%s step=%s days=%s state_update_interval=%s "
            "build_elapsed_ms=%.1f "
            "bridge_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(rows),
            min_dr,
            max_dr,
            target_dr_step,
            days_to_simulate,
            state_update_interval,
            build_elapsed_ms,
            predict_elapsed_ms,
            build_elapsed_ms + predict_elapsed_ms,
        )
        return output

    def snapshot(self, review_input: RwkvReviewInput) -> object:
        with self._locked_process():
            return self._process.state_for_card(review_input.identity.card_id)

    def restore(self, state: object | None) -> None:
        if state is not None:
            with self._locked_process():
                self._process.restore_state(state)

    def cache_state(self) -> bytes:
        with self._locked_process():
            return bytes(self._process.cache_state())

    def restore_cache_state(self, state: bytes) -> None:
        with self._locked_process():
            self._process.restore_cache_state(state)


def _review_input_row(
    review_input: RwkvReviewInput,
) -> tuple[
    int,
    int | None,
    int | None,
    int | None,
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    identity = review_input.identity
    return (
        identity.card_id,
        identity.note_id,
        identity.deck_id,
        identity.preset_id,
        review_input.is_query,
        review_input.ease,
        review_input.duration_millis,
        review_input.card_type,
        review_input.day_offset,
        review_input.current_elapsed_days,
        review_input.current_elapsed_seconds,
        *review_input.target_retentions,
    )


def _workload_input_row(
    review_input: RwkvReviewInput,
) -> tuple[
    int,
    int | None,
    int | None,
    int | None,
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    float | None,
    float | None,
    float | None,
    float | None,
    int | None,
    int | None,
    int | None,
]:
    return (
        *_review_input_row(review_input),
        review_input.interval_days,
        review_input.reps,
        review_input.lapses,
    )


def _workload_snapshot(
    snapshot: RwkvBackendCacheSnapshot,
) -> tuple[
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    list[tuple[int, bytes]],
    bytes | None,
    bytes | None,
]:
    return (
        sorted(snapshot.card_states.items()),
        sorted(snapshot.note_states.items()),
        sorted(snapshot.deck_states.items()),
        sorted(snapshot.preset_states.items()),
        snapshot.global_state,
        snapshot.runtime_state,
    )


def _workload_bucket_probabilities(
    review_model: object,
) -> list[tuple[int, float, float, float, float]]:
    probabilities = getattr(review_model, "bucket_probabilities")
    return [
        (int(bucket), float(again), float(hard), float(good), float(easy))
        for bucket, (again, hard, good, easy) in sorted(probabilities.items())
    ]


def _prediction_request_row(
    request: RwkvReviewPredictionRequest,
) -> tuple[
    int,
    int | None,
    int | None,
    int | None,
    bool,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    float | None,
    float | None,
    float | None,
    float | None,
    bytes | None,
    bytes | None,
    bytes | None,
    bytes | None,
    bytes | None,
]:
    return (
        *_review_input_row(request.review_input),
        _state_bytes(request.card_state),
        _state_bytes(request.note_state),
        _state_bytes(request.deck_state),
        _state_bytes(request.preset_state),
        _state_bytes(request.global_state),
    )


def _packed_prediction_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
) -> tuple[
    bytes,
    tuple[
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
        list[bytes | None],
    ],
]:
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_PREDICTION_REQUEST_MAGIC,
            len(requests),
        )
    )
    card_states: list[bytes | None] = []
    note_states: list[bytes | None] = []
    deck_states: list[bytes | None] = []
    preset_states: list[bytes | None] = []
    global_states: list[bytes | None] = []

    for request in requests:
        payload.extend(_packed_review_input_row(request.review_input))

        card_states.append(_state_bytes(request.card_state))
        note_states.append(_state_bytes(request.note_state))
        deck_states.append(_state_bytes(request.deck_state))
        preset_states.append(_state_bytes(request.preset_state))
        global_states.append(_state_bytes(request.global_state))

    return (
        bytes(payload),
        (card_states, note_states, deck_states, preset_states, global_states),
    )


def _packed_review_input_row(review_input: RwkvReviewInput) -> bytes:
    identity = review_input.identity
    presence = 0

    def optional_i64(value: int | None, bit: int) -> int:
        nonlocal presence
        if value is None:
            return 0
        presence |= 1 << bit
        return int(value)

    def optional_f32(value: float | None, bit: int) -> float:
        nonlocal presence
        if value is None:
            return 0.0
        presence |= 1 << bit
        return float(value)

    note_id = optional_i64(identity.note_id, 0)
    deck_id = optional_i64(identity.deck_id, 1)
    preset_id = optional_i64(identity.preset_id, 2)
    ease = optional_i64(review_input.ease, 3)
    duration_millis = optional_i64(review_input.duration_millis, 4)
    card_type = optional_i64(review_input.card_type, 5)
    day_offset = optional_i64(review_input.day_offset, 6)
    current_elapsed_days = optional_i64(review_input.current_elapsed_days, 7)
    current_elapsed_seconds = optional_i64(review_input.current_elapsed_seconds, 8)
    target_retention_again = optional_f32(review_input.target_retentions[0], 9)
    target_retention_hard = optional_f32(review_input.target_retentions[1], 10)
    target_retention_good = optional_f32(review_input.target_retentions[2], 11)
    target_retention_easy = optional_f32(review_input.target_retentions[3], 12)

    return _PACKED_PREDICTION_REQUEST_ROW.pack(
        presence,
        identity.card_id,
        note_id,
        deck_id,
        preset_id,
        1 if review_input.is_query else 0,
        ease,
        duration_millis,
        card_type,
        day_offset,
        current_elapsed_days,
        current_elapsed_seconds,
        target_retention_again,
        target_retention_hard,
        target_retention_good,
        target_retention_easy,
    )


def _packed_warm_up_reviews(reviews: Sequence[RwkvReviewInput]) -> bytes:
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_WARM_UP_REVIEW_MAGIC,
            len(reviews),
        )
    )
    for review_input in reviews:
        payload.extend(_packed_review_input_row(review_input))
    return bytes(payload)


def _warmup_progress_interval(total: int) -> int:
    if total <= 0:
        return 1
    return max(1, min(1000, total // 100 or 1))


def _rust_warmup_chunk_size(total: int) -> int:
    progress_interval = _warmup_progress_interval(total)
    if total <= _RUST_WARMUP_CHUNK_SIZE:
        return progress_interval
    return max(progress_interval, _RUST_WARMUP_CHUNK_SIZE)


def _report_warmup_progress(
    progress: RwkvWarmUpProgressCallback | None,
    *,
    processed: int,
    total: int,
) -> None:
    if progress is not None:
        progress(RwkvWarmUpProgress(processed_reviews=processed, total_reviews=total))


def _state_bytes(state: object | None) -> bytes | None:
    if state is None:
        return None
    if isinstance(state, bytes):
        return state
    raise TypeError("RWKV Rust state must be bytes")


def _interval_override_from_tuple(values: object) -> RwkvIntervalOverride:
    if not isinstance(values, tuple) or len(values) != 4:
        return RwkvIntervalOverride()

    return RwkvIntervalOverride(
        again=_optional_interval(values[0]),
        hard=_optional_interval(values[1]),
        good=_optional_interval(values[2]),
        easy=_optional_interval(values[3]),
    )


def _optional_interval(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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
                "duration": _duration_millis(review_input),
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

    torch_dtype = _torch_dtype(torch, dtype)
    return (
        RNNProcess(
            path=model_path,
            device=torch.device(device),
            dtype=torch_dtype,
        ),
        lambda row: pd.Series(row, dtype="float64"),
    )


def _torch_dtype(torch: Any, dtype: str) -> Any:
    return {
        "float": torch.float32,
        "float32": torch.float32,
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
    }[dtype]


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


def _duration_millis(review_input: RwkvReviewInput) -> float:
    if review_input.duration_millis is None:
        return 0.0
    return float(review_input.duration_millis)


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
