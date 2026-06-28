# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import base64
import gzip
import hashlib
import inspect
import json
import logging
import math
import os
import struct
import tempfile
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, TypeVar, cast

from anki import cards_pb2, deck_config_pb2, scheduler_pb2
from anki.consts import (
    CARD_TYPE_LRN,
    CARD_TYPE_RELEARNING,
    CARD_TYPE_REV,
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_REV,
    QUEUE_TYPE_SUSPENDED,
)
from anki.scheduler.v3 import SchedulingState, SchedulingStates
from anki.utils import ids2str
from aqt.qt import QWidget

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

_REVIEWER_PREDICTION_ATTR = "_rwkv_review_prediction"
_REVIEW_ORDER_RETRIEVABILITY_ASCENDING = (
    deck_config_pb2.DeckConfig.Config.REVIEW_CARD_ORDER_RETRIEVABILITY_ASCENDING
)
_REVIEW_ORDER_RETRIEVABILITY_DESCENDING = (
    deck_config_pb2.DeckConfig.Config.REVIEW_CARD_ORDER_RETRIEVABILITY_DESCENDING
)
_DEFAULT_RWKV_REVIEW_BATCH_SIZE = 512
_DEFAULT_RWKV_REVIEW_REFRESH_INTERVAL = 1
_MIN_RWKV_REVIEW_BATCH_SIZE = 64
_MAX_RWKV_REVIEW_BATCH_SIZE = 2048
_MIN_RWKV_REVIEW_REFRESH_INTERVAL = 1
_MAX_RWKV_REVIEW_REFRESH_INTERVAL = 10_000
_RWKV_REVIEW_PREDICTION_CACHE_LIMIT = 32768
_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE = "search_stats_rwkv_review_retrievability"
_RWKV_REVIEW_UNDO_LIMIT = 30
_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS = 30.0
_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS = 0.05
_EMBEDDED_RWKV_MODEL_FILENAME = "RWKV_trained_on_5000_10000.bin"
_RWKV_MODEL_KEY_HASH_CHUNK_SIZE = 1024 * 1024
_RWKV_STATE_CACHE_VERSION = 5
_RWKV_STATE_CACHE_LEGACY_JSON_VERSION = 2
_RWKV_STATE_CACHE_DIR = "rwkv-state-cache"
_RWKV_STATE_CACHE_DATA_FILE = "state-v1.json.gz"
_RWKV_STATE_CACHE_SNAPSHOT_FILE = "snapshot-v1.bin"
_RWKV_STATE_CACHE_DELTAS_FILE = "deltas-v1.log"
_RWKV_STATE_CACHE_META_FILE = "state-v1.meta.json"
_RWKV_STATE_CACHE_SNAPSHOT_MAGIC = b"ARWKVSNAPSHOT5\0"
_RWKV_STATE_CACHE_DELTAS_MAGIC = b"ARWKVDELTAS5\0"
_FSRS_PRESET_OVERLAY_CONFIG_KEY = "fsrsPresetOverlay"
_RWKV_DEFAULT_TARGET_RETENTION = 0.9
_RWKV_RATING_FIELDS = ("again", "hard", "good", "easy")
_reviewer_backend: RwkvReviewerBackend | None = None
_reviewer_backend_warmup_keys: set[tuple[int, int]] = set()
_reviewer_backend_warmup_pending_keys: set[tuple[int, int]] = set()
_resolved_preset_id_cache: dict[tuple[int, str | None], dict[int, str]] = {}
_rwkv_review_queue_score_maps: dict[int, dict[int, float]] = {}
_rwkv_review_queue_score_generations: dict[int, int] = {}
_rwkv_stats_prepare_lock = threading.Lock()
_rwkv_stats_prepare_in_flight: dict[RwkvStatsPrepareKey, threading.Event] = {}
_rwkv_score_prewarm_lock = threading.Lock()
_rwkv_score_prewarm_in_flight: set[RwkvScorePrewarmKey] = set()
_rwkv_startup_prompt_shown = False


@dataclass(frozen=True)
class RwkvRecallPoint:
    elapsed_days: float
    retrievability: float


@dataclass(frozen=True)
class RwkvIntervalOverride:
    again: int | None = None
    hard: int | None = None
    good: int | None = None
    easy: int | None = None


@dataclass(frozen=True)
class RwkvReviewPrediction:
    retrievability: float | None = None
    current_interval: int | None = None
    current_s90: int | None = None
    interval_overrides: RwkvIntervalOverride = RwkvIntervalOverride()
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride()


@dataclass(frozen=True)
class RwkvReviewerPrediction:
    card_id: int
    retrievability: float | None
    review_enabled: bool = False
    interval_override_used: bool = False
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride()


@dataclass(frozen=True)
class RwkvReviewerDiagnostics:
    retrievability: float | None
    retrievability_source: str


@dataclass(frozen=True)
class RwkvReviewIdentity:
    card_id: int
    note_id: int | None = None
    deck_id: int | None = None
    preset_id: int | None = None


@dataclass(frozen=True)
class RwkvReviewInput:
    identity: RwkvReviewIdentity
    is_query: bool
    ease: int | None
    duration_millis: int | None
    card_type: int | None
    card_queue: int | None
    card_due: int | None
    interval_days: int | None
    ease_factor: int | None
    reps: int | None
    lapses: int | None
    day_offset: int | None
    current_state_kind: str | None
    current_normal_state_kind: str | None
    current_elapsed_days: int | None
    current_elapsed_seconds: int | None
    target_retentions: tuple[
        float | None,
        float | None,
        float | None,
        float | None,
    ] = (None, None, None, None)


@dataclass(frozen=True)
class RwkvReviewCandidate:
    reviewer: object
    card: object


@dataclass(frozen=True)
class RwkvStatsGraphCard:
    id: int
    nid: int
    did: int
    odid: int
    type: int
    queue: int
    due: int
    odue: int
    ivl: int
    factor: int
    reps: int
    lapses: int
    last_review_time: int | None

    def current_deck_id(self) -> int:
        return self.odid or self.did


class RwkvStatsGraphCardFields(NamedTuple):
    id: int
    nid: int
    did: int
    odid: int
    type: int
    queue: int
    due: int
    odue: int
    ivl: int
    factor: int
    reps: int
    lapses: int
    last_review_time: int | None

    def current_deck_id(self) -> int:
        return self.odid or self.did


@dataclass(frozen=True)
class RwkvReviewInputBatchBuild:
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]]
    loaded_rows: int
    parsed_cards: int
    cards_with_state: int
    disabled_config_cards: int
    eligible_cards: int
    deck_configs: int
    preset_elapsed_ms: float
    load_elapsed_ms: float
    candidate_elapsed_ms: float
    searched_rows: int = 0


@dataclass(frozen=True)
class RwkvReviewRescheduleItem:
    card_id: int
    interval_days: int
    elapsed_days: int
    s90: int


@dataclass(frozen=True)
class RwkvReviewRescheduleResult:
    built: bool
    changes: object | None
    predicted: int = 0
    updated: int = 0


@dataclass(frozen=True)
class RwkvReviewTransition:
    prediction: RwkvReviewPrediction | None = None
    card_state: object | None = None
    note_state: object | None = None
    deck_state: object | None = None
    preset_state: object | None = None
    global_state: object | None = None


@dataclass(frozen=True)
class RwkvReviewerStateSnapshot:
    card_state: object | None = None
    note_state: object | None = None
    deck_state: object | None = None
    preset_state: object | None = None
    global_state: object | None = None
    runtime_state: object | None = None


@dataclass(frozen=True)
class RwkvReviewRollbackFrame:
    counter: int
    identity: RwkvReviewIdentity
    before: RwkvReviewerStateSnapshot
    after: RwkvReviewerStateSnapshot


@dataclass(frozen=True)
class RwkvReviewPredictionRequest:
    review_input: RwkvReviewInput
    card_state: object | None = None
    note_state: object | None = None
    deck_state: object | None = None
    preset_state: object | None = None
    global_state: object | None = None


@dataclass(frozen=True)
class RwkvBackendCacheSnapshot:
    card_states: dict[int, bytes]
    note_states: dict[int, bytes]
    deck_states: dict[int, bytes]
    preset_states: dict[int, bytes]
    global_state: bytes | None
    runtime_state: bytes | None


@dataclass(frozen=True)
class RwkvHistoricalReviewInputs:
    reviews: list[RwkvReviewInput]
    review_ids: list[int]
    previous_review_id_by_card: dict[int, int]
    previous_interval_days_by_card: dict[int, int]
    review_count_by_card: dict[int, int]
    last_review_id: int
    review_count: int


@dataclass(frozen=True)
class RwkvStoredStateCache:
    metadata: dict[str, object]
    snapshot: RwkvBackendCacheSnapshot
    history: RwkvHistoricalReviewInputs


@dataclass(frozen=True)
class RwkvHistoricalPresetRule:
    preset_id: int
    search: str | None
    card_ids: frozenset[int] | None
    min_reps: int | None
    max_reps: int | None
    min_interval_days: float | None
    max_interval_days: float | None


@dataclass(frozen=True)
class RwkvWarmUpProgress:
    processed_reviews: int
    total_reviews: int


RwkvWarmUpProgressCallback = Callable[[RwkvWarmUpProgress], None]
RwkvStateCacheProgressCallback = Callable[[str, int | None, int | None], None]
RwkvReviewPredictionRequestByIndex = tuple[int, RwkvReviewPredictionRequest]
RwkvCachedReviewPredictions = tuple[
    list[RwkvReviewPrediction | None],
    list[RwkvReviewPredictionRequestByIndex],
    int,
]
RwkvStatsPrepareKey = tuple[int, int, int, int, str]
RwkvScorePrewarmKey = tuple[int, int, int, int, tuple[int, ...]]


class RwkvReviewerBackend(Protocol):
    def predict_review(
        self,
        *,
        reviewer: object,
        card: object,
    ) -> RwkvReviewPrediction | None: ...

    def predict_reviews(
        self,
        candidates: Sequence[RwkvReviewCandidate],
    ) -> Sequence[RwkvReviewPrediction | None]: ...

    def review_answered(
        self,
        *,
        reviewer: object,
        card: object,
        ease: int,
    ) -> None: ...


class RwkvReviewRuntime(Protocol):
    def review(
        self,
        *,
        review_input: RwkvReviewInput,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> RwkvReviewTransition: ...


class RwkvStatefulReviewerBackend:
    def __init__(self, runtime: RwkvReviewRuntime) -> None:
        self._runtime = runtime
        self._card_states: dict[int, object | None] = {}
        self._note_states: dict[int, object | None] = {}
        self._deck_states: dict[int, object | None] = {}
        self._preset_states: dict[int, object | None] = {}
        self._global_state: object | None = None
        self._state_generation = 0
        self._undo_frames: list[RwkvReviewRollbackFrame] = []
        self._redo_frames: list[RwkvReviewRollbackFrame] = []
        self._prediction_cache: OrderedDict[
            RwkvReviewInput,
            RwkvReviewPrediction | None,
        ] = OrderedDict()
        initial_runtime_cache_state = getattr(self._runtime, "cache_state", None)
        self._initial_runtime_state = (
            _cacheable_state_bytes(initial_runtime_cache_state())
            if callable(initial_runtime_cache_state)
            else None
        )

    def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
        runtime_cache_state = getattr(self._runtime, "cache_state", None)
        runtime_state = runtime_cache_state() if callable(runtime_cache_state) else None
        return RwkvBackendCacheSnapshot(
            card_states=_cacheable_state_map(self._card_states),
            note_states=_cacheable_state_map(self._note_states),
            deck_states=_cacheable_state_map(self._deck_states),
            preset_states=_cacheable_state_map(self._preset_states),
            global_state=_cacheable_state_bytes(self._global_state),
            runtime_state=_cacheable_state_bytes(runtime_state),
        )

    def restore_cache_snapshot(self, snapshot: RwkvBackendCacheSnapshot) -> None:
        self._card_states = dict(snapshot.card_states)
        self._note_states = dict(snapshot.note_states)
        self._deck_states = dict(snapshot.deck_states)
        self._preset_states = dict(snapshot.preset_states)
        self._global_state = snapshot.global_state
        self._advance_state_generation()
        self._undo_frames.clear()
        self._redo_frames.clear()
        self._clear_prediction_cache("state cache restored")

        if snapshot.runtime_state is not None:
            restore_cache_state = getattr(self._runtime, "restore_cache_state", None)
            if callable(restore_cache_state):
                restore_cache_state(snapshot.runtime_state)

    def reset_cache_snapshot(self) -> None:
        self._card_states.clear()
        self._note_states.clear()
        self._deck_states.clear()
        self._preset_states.clear()
        self._global_state = None
        self._advance_state_generation()
        self._undo_frames.clear()
        self._redo_frames.clear()
        self._clear_prediction_cache("state cache reset")

        if self._initial_runtime_state is not None:
            restore_cache_state = getattr(self._runtime, "restore_cache_state", None)
            if callable(restore_cache_state):
                restore_cache_state(self._initial_runtime_state)
        reset_warm_up_state = getattr(self._runtime, "reset_warm_up_state", None)
        if callable(reset_warm_up_state):
            reset_warm_up_state()

    def warm_up(
        self,
        reviews: Sequence[RwkvReviewInput],
        *,
        review_ids: Sequence[int] | None = None,
        prediction_recorder: Callable[[int, float], None] | None = None,
        progress: RwkvWarmUpProgressCallback | None = None,
    ) -> None:
        total = len(reviews)
        report_every = _rwkv_warmup_progress_interval(total)
        _report_rwkv_warmup_progress(progress, processed=0, total=total)
        bulk_warm_up = getattr(self._runtime, "warm_up_reviews", None)
        if callable(bulk_warm_up) and self._can_use_runtime_bulk_warm_up():
            self.reset_cache_snapshot()
            snapshot = bulk_warm_up(
                reviews,
                review_ids=review_ids,
                prediction_recorder=prediction_recorder,
                progress=progress,
            )
            self._install_cache_snapshot(snapshot)
            return

        for processed, review_input in enumerate(reviews, start=1):
            identity = review_input.identity
            if review_input.ease is not None:
                card_state = self._card_states.get(identity.card_id)
                note_state = _entity_state(self._note_states, identity.note_id)
                deck_state = _entity_state(self._deck_states, identity.deck_id)
                preset_state = _entity_state(
                    self._preset_states,
                    identity.preset_id,
                )
                if prediction_recorder is not None and review_ids is not None:
                    review_id = (
                        review_ids[processed - 1]
                        if processed - 1 < len(review_ids)
                        else None
                    )
                    if isinstance(review_id, int):
                        query_transition = self._runtime.review(
                            review_input=replace(
                                review_input,
                                is_query=True,
                                ease=None,
                                duration_millis=None,
                            ),
                            card_state=card_state,
                            note_state=note_state,
                            deck_state=deck_state,
                            preset_state=preset_state,
                            global_state=self._global_state,
                        )
                        prediction = getattr(query_transition, "prediction", None)
                        retrievability = getattr(
                            prediction,
                            "retrievability",
                            None,
                        )
                        if isinstance(retrievability, (int, float)) and math.isfinite(
                            retrievability
                        ):
                            prediction_recorder(review_id, retrievability)

                transition = self._runtime.review(
                    review_input=review_input,
                    card_state=card_state,
                    note_state=note_state,
                    deck_state=deck_state,
                    preset_state=preset_state,
                    global_state=self._global_state,
                )
                self._store_transition(identity, transition)

            if processed == total or processed % report_every == 0:
                _report_rwkv_warmup_progress(
                    progress,
                    processed=processed,
                    total=total,
                )

    def _can_use_runtime_bulk_warm_up(self) -> bool:
        return (
            not self._card_states
            and not self._note_states
            and not self._deck_states
            and not self._preset_states
            and self._global_state is None
        )

    def _install_cache_snapshot(self, snapshot: RwkvBackendCacheSnapshot) -> None:
        self._card_states = dict(snapshot.card_states)
        self._note_states = dict(snapshot.note_states)
        self._deck_states = dict(snapshot.deck_states)
        self._preset_states = dict(snapshot.preset_states)
        self._global_state = snapshot.global_state
        self._advance_state_generation()
        self._undo_frames.clear()
        self._redo_frames.clear()
        self._clear_prediction_cache("state cache built")

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
        cached, prediction = self._cached_prediction(review_input)
        if cached:
            logger.debug(
                "RWKV stateful prediction cache hit: card_id=%s runtime=%s",
                identity.card_id,
                type(self._runtime).__name__,
            )
            return prediction

        request = self._prediction_request(identity, review_input)
        prediction = self._runtime.review(
            review_input=request.review_input,
            card_state=request.card_state,
            note_state=request.note_state,
            deck_state=request.deck_state,
            preset_state=request.preset_state,
            global_state=request.global_state,
        ).prediction
        self._cache_prediction(review_input, prediction)
        return prediction

    def predict_reviews(
        self,
        candidates: Sequence[RwkvReviewCandidate],
    ) -> Sequence[RwkvReviewPrediction | None]:
        start = time.monotonic()
        predictions: list[RwkvReviewPrediction | None] = [None] * len(candidates)
        requests_by_index: list[tuple[int, RwkvReviewPredictionRequest]] = []
        cache_hits = 0

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
            cached, prediction = self._cached_prediction(review_input)
            if cached:
                cache_hits += 1
                predictions[index] = prediction
                continue

            requests_by_index.append(
                (index, self._prediction_request(identity, review_input))
            )

        if not requests_by_index:
            if cache_hits:
                logger.debug(
                    "RWKV stateful batch predicted from cache: candidates=%s "
                    "cache_hits=%s runtime=%s elapsed_ms=%.1f",
                    len(candidates),
                    cache_hits,
                    type(self._runtime).__name__,
                    (time.monotonic() - start) * 1000,
                )
            return predictions

        request_elapsed_ms = (time.monotonic() - start) * 1000
        predict_many = getattr(self._runtime, "predict_many", None)
        if callable(predict_many):
            predict_start = time.monotonic()
            logger.debug(
                "RWKV stateful batch predict_many started: candidates=%s requests=%s "
                "cache_hits=%s runtime=%s build_elapsed_ms=%.1f",
                len(candidates),
                len(requests_by_index),
                cache_hits,
                type(self._runtime).__name__,
                request_elapsed_ms,
            )
            batch_predictions = predict_many(
                [request for _, request in requests_by_index]
            )
            predict_elapsed_ms = (time.monotonic() - predict_start) * 1000
            if len(batch_predictions) != len(requests_by_index):
                raise ValueError("RWKV batch prediction count mismatch")

            for (index, request), prediction in zip(
                requests_by_index,
                batch_predictions,
                strict=True,
            ):
                predictions[index] = prediction
                self._cache_prediction(request.review_input, prediction)
            logger.debug(
                "RWKV stateful batch predicted: candidates=%s requests=%s "
                "cache_hits=%s runtime=%s build_elapsed_ms=%.1f "
                "predict_elapsed_ms=%.1f elapsed_ms=%.1f",
                len(candidates),
                len(requests_by_index),
                cache_hits,
                type(self._runtime).__name__,
                request_elapsed_ms,
                predict_elapsed_ms,
                (time.monotonic() - start) * 1000,
            )
            return predictions

        predict_start = time.monotonic()
        for index, request in requests_by_index:
            predictions[index] = self._runtime.review(
                review_input=request.review_input,
                card_state=request.card_state,
                note_state=request.note_state,
                deck_state=request.deck_state,
                preset_state=request.preset_state,
                global_state=request.global_state,
            ).prediction
            self._cache_prediction(request.review_input, predictions[index])
        logger.debug(
            "RWKV stateful batch predicted via per-card fallback: candidates=%s "
            "requests=%s cache_hits=%s runtime=%s build_elapsed_ms=%.1f "
            "predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(candidates),
            len(requests_by_index),
            cache_hits,
            type(self._runtime).__name__,
            request_elapsed_ms,
            (time.monotonic() - predict_start) * 1000,
            (time.monotonic() - start) * 1000,
        )

        return predictions

    def cached_review_predictions(
        self,
        candidates: Sequence[RwkvReviewCandidate],
    ) -> RwkvCachedReviewPredictions:
        start = time.monotonic()
        predictions: list[RwkvReviewPrediction | None] = [None] * len(candidates)
        requests_by_index: list[RwkvReviewPredictionRequestByIndex] = []
        cache_hits = 0

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
            cached, prediction = self._cached_prediction(review_input)
            if cached:
                cache_hits += 1
                predictions[index] = prediction
            else:
                requests_by_index.append(
                    (index, self._prediction_request(identity, review_input))
                )

        if cache_hits:
            logger.debug(
                "RWKV stateful prediction cache split: candidates=%s cache_hits=%s "
                "misses=%s runtime=%s elapsed_ms=%.1f",
                len(candidates),
                cache_hits,
                len(requests_by_index),
                type(self._runtime).__name__,
                (time.monotonic() - start) * 1000,
            )

        return predictions, requests_by_index, cache_hits

    def cached_review_input_predictions(
        self,
        inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    ) -> RwkvCachedReviewPredictions:
        start = time.monotonic()
        predictions: list[RwkvReviewPrediction | None] = [None] * len(inputs_by_index)
        requests_by_index: list[RwkvReviewPredictionRequestByIndex] = []
        cache_hits = 0

        for position, (index, review_input) in enumerate(inputs_by_index):
            cached, prediction = self._cached_prediction(review_input)
            if cached:
                cache_hits += 1
                predictions[position] = prediction
            else:
                requests_by_index.append(
                    (
                        index,
                        self._prediction_request(
                            review_input.identity,
                            review_input,
                        ),
                    )
                )

        if cache_hits:
            logger.debug(
                "RWKV stateful input prediction cache split: inputs=%s cache_hits=%s "
                "misses=%s runtime=%s elapsed_ms=%.1f",
                len(inputs_by_index),
                cache_hits,
                len(requests_by_index),
                type(self._runtime).__name__,
                (time.monotonic() - start) * 1000,
            )

        return predictions, requests_by_index, cache_hits

    def predict_review_requests(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        if not requests:
            return []

        start = time.monotonic()
        predict_many = getattr(self._runtime, "predict_many", None)
        if callable(predict_many):
            logger.debug(
                "RWKV stateful request batch predict_many started: requests=%s "
                "runtime=%s",
                len(requests),
                type(self._runtime).__name__,
            )
            predictions = predict_many(requests)
            if len(predictions) != len(requests):
                raise ValueError("RWKV batch prediction count mismatch")

            for request, prediction in zip(requests, predictions, strict=True):
                self._cache_prediction(request.review_input, prediction)
            logger.debug(
                "RWKV stateful request batch predicted: requests=%s runtime=%s "
                "elapsed_ms=%.1f",
                len(requests),
                type(self._runtime).__name__,
                (time.monotonic() - start) * 1000,
            )
            return predictions

        predictions = [
            self._runtime.review(
                review_input=request.review_input,
                card_state=request.card_state,
                note_state=request.note_state,
                deck_state=request.deck_state,
                preset_state=request.preset_state,
                global_state=request.global_state,
            ).prediction
            for request in requests
        ]
        for request, prediction in zip(requests, predictions, strict=True):
            self._cache_prediction(request.review_input, prediction)
        logger.debug(
            "RWKV stateful request batch predicted via per-card fallback: "
            "requests=%s runtime=%s elapsed_ms=%.1f",
            len(requests),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return predictions

    def predict_retrievability_requests(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        if not requests:
            return []

        predict_retrievability_many = getattr(
            self._runtime,
            "predict_retrievability_many",
            None,
        )
        if not callable(predict_retrievability_many):
            return self.predict_review_requests(requests)

        start = time.monotonic()
        retrievabilities = predict_retrievability_many(requests)
        if len(retrievabilities) != len(requests):
            raise ValueError("RWKV retrievability batch prediction count mismatch")

        logger.debug(
            "RWKV stateful request batch predict_retrievability_many predicted: "
            "requests=%s runtime=%s elapsed_ms=%.1f",
            len(requests),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        predictions = [
            RwkvReviewPrediction(retrievability=float(retrievability))
            for retrievability in retrievabilities
        ]
        for request, prediction in zip(requests, predictions, strict=True):
            self._cache_prediction(request.review_input, prediction)
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
        before = self._snapshot(identity, review_input)
        transition = self._runtime.review(
            review_input=review_input,
            card_state=before.card_state,
            note_state=before.note_state,
            deck_state=before.deck_state,
            preset_state=before.preset_state,
            global_state=before.global_state,
        )
        self._store_transition(identity, transition)
        self._save_rollback_frame(
            reviewer,
            RwkvReviewRollbackFrame(
                counter=0,
                identity=identity,
                before=before,
                after=self._snapshot(identity, review_input),
            ),
        )

    def answer_undone(self, counter: int, next_counter: int | None) -> bool:
        index = _rollback_frame_index(self._undo_frames, counter)
        if index is None:
            return False

        frame = self._undo_frames.pop(index)
        self._restore_snapshot(frame.identity, frame.before)
        _append_bounded(
            self._redo_frames,
            replace(
                frame, counter=next_counter if next_counter is not None else counter
            ),
        )
        return True

    def answer_redone(self, counter: int, next_counter: int | None) -> bool:
        index = _rollback_frame_index(self._redo_frames, counter)
        if index is None:
            return False

        frame = self._redo_frames.pop(index)
        self._restore_snapshot(frame.identity, frame.after)
        _append_bounded(
            self._undo_frames,
            replace(
                frame, counter=next_counter if next_counter is not None else counter
            ),
        )
        return True

    def _store_transition(
        self,
        identity: RwkvReviewIdentity,
        transition: RwkvReviewTransition,
    ) -> None:
        self._card_states[identity.card_id] = transition.card_state
        _set_entity_state(self._note_states, identity.note_id, transition.note_state)
        _set_entity_state(self._deck_states, identity.deck_id, transition.deck_state)
        _set_entity_state(
            self._preset_states,
            identity.preset_id,
            transition.preset_state,
        )
        self._global_state = transition.global_state
        self._advance_state_generation()
        self._clear_prediction_cache("review state advanced")

    def _save_rollback_frame(
        self,
        reviewer: object,
        frame: RwkvReviewRollbackFrame,
    ) -> None:
        self._redo_frames.clear()
        counter = _current_undo_counter(reviewer)
        if counter is None:
            return

        _append_bounded(self._undo_frames, replace(frame, counter=counter))

    def _snapshot(
        self,
        identity: RwkvReviewIdentity,
        review_input: RwkvReviewInput,
    ) -> RwkvReviewerStateSnapshot:
        return RwkvReviewerStateSnapshot(
            card_state=self._card_states.get(identity.card_id),
            note_state=_entity_state(self._note_states, identity.note_id),
            deck_state=_entity_state(self._deck_states, identity.deck_id),
            preset_state=_entity_state(self._preset_states, identity.preset_id),
            global_state=self._global_state,
            runtime_state=_runtime_state(self._runtime, review_input),
        )

    def _restore_snapshot(
        self,
        identity: RwkvReviewIdentity,
        snapshot: RwkvReviewerStateSnapshot,
    ) -> None:
        self._card_states[identity.card_id] = snapshot.card_state
        _set_entity_state(self._note_states, identity.note_id, snapshot.note_state)
        _set_entity_state(self._deck_states, identity.deck_id, snapshot.deck_state)
        _set_entity_state(
            self._preset_states,
            identity.preset_id,
            snapshot.preset_state,
        )
        self._global_state = snapshot.global_state
        _restore_runtime_state(self._runtime, snapshot.runtime_state)
        self._advance_state_generation()
        self._clear_prediction_cache("review state restored")

    def state_generation(self) -> int:
        return self._state_generation

    def _advance_state_generation(self) -> None:
        self._state_generation += 1

    def _prediction_request(
        self,
        identity: RwkvReviewIdentity,
        review_input: RwkvReviewInput,
    ) -> RwkvReviewPredictionRequest:
        return RwkvReviewPredictionRequest(
            review_input=review_input,
            card_state=self._card_states.get(identity.card_id),
            note_state=_entity_state(self._note_states, identity.note_id),
            deck_state=_entity_state(self._deck_states, identity.deck_id),
            preset_state=_entity_state(self._preset_states, identity.preset_id),
            global_state=self._global_state,
        )

    def _cached_prediction(
        self,
        review_input: RwkvReviewInput,
    ) -> tuple[bool, RwkvReviewPrediction | None]:
        try:
            prediction = self._prediction_cache[review_input]
        except KeyError:
            return False, None

        self._prediction_cache.move_to_end(review_input)
        return True, prediction

    def _cache_prediction(
        self,
        review_input: RwkvReviewInput,
        prediction: RwkvReviewPrediction | None,
    ) -> None:
        self._prediction_cache[review_input] = prediction
        self._prediction_cache.move_to_end(review_input)
        while len(self._prediction_cache) > _RWKV_REVIEW_PREDICTION_CACHE_LIMIT:
            self._prediction_cache.popitem(last=False)

    def _clear_prediction_cache(self, reason: str) -> None:
        cached_entries = len(self._prediction_cache)
        if cached_entries:
            logger.debug(
                "RWKV stateful prediction cache cleared: reason=%s entries=%s "
                "runtime=%s",
                reason,
                cached_entries,
                type(self._runtime).__name__,
            )
        self._prediction_cache.clear()


def record_collection_undo(changes: object) -> None:
    """Roll back RWKV state after Anki undoes an answered review."""

    _record_collection_undo_or_redo(changes, redo=False)


def record_collection_redo(changes: object) -> None:
    """Restore RWKV state after Anki redoes an answered review."""

    _record_collection_undo_or_redo(changes, redo=True)


def _record_collection_undo_or_redo(changes: object, *, redo: bool) -> None:
    backend = _reviewer_backend
    if backend is None:
        return

    counter = _undo_result_counter(changes)
    if counter is None:
        return

    next_counter = _undo_result_next_counter(changes)
    handler_name = "answer_redone" if redo else "answer_undone"
    handler = getattr(backend, handler_name, None)
    if callable(handler):
        handler(counter, next_counter)


def _current_undo_counter(reviewer: object) -> int | None:
    col = _collection(reviewer)
    undo_status = getattr(col, "undo_status", None)
    if not callable(undo_status):
        return None

    try:
        status = undo_status()
    except Exception:
        logger.debug("failed to read undo status for RWKV rollback")
        return None

    return _valid_counter(getattr(status, "last_step", None))


def _undo_result_counter(changes: object) -> int | None:
    return _valid_counter(getattr(changes, "counter", None))


def _undo_result_next_counter(changes: object) -> int | None:
    return _valid_counter(
        getattr(getattr(changes, "new_status", None), "last_step", None)
    )


def _valid_counter(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rollback_frame_index(
    frames: Sequence[RwkvReviewRollbackFrame],
    counter: int,
) -> int | None:
    for index in range(len(frames) - 1, -1, -1):
        if frames[index].counter == counter:
            return index
    return None


def _append_bounded(
    frames: list[RwkvReviewRollbackFrame],
    frame: RwkvReviewRollbackFrame,
) -> None:
    frames.append(frame)
    del frames[:-_RWKV_REVIEW_UNDO_LIMIT]


def _runtime_state(
    runtime: RwkvReviewRuntime,
    review_input: RwkvReviewInput,
) -> object | None:
    snapshot = getattr(runtime, "snapshot", None)
    if not callable(snapshot):
        return None
    return snapshot(review_input)


def _restore_runtime_state(runtime: RwkvReviewRuntime, state: object | None) -> None:
    restore = getattr(runtime, "restore", None)
    if callable(restore):
        restore(state)


def _cacheable_state_map(states: dict[int, object | None]) -> dict[int, bytes]:
    return {
        key: state_bytes
        for key, state in states.items()
        if (state_bytes := _cacheable_state_bytes(state)) is not None
    }


def _cacheable_state_bytes(state: object | None) -> bytes | None:
    if state is None:
        return None
    if isinstance(state, bytes):
        return state
    raise TypeError(f"RWKV state cache only supports bytes, got {type(state).__name__}")


def _rwkv_warmup_progress_interval(total: int) -> int:
    if total <= 0:
        return 1
    return max(1, min(1000, total // 100 or 1))


def _report_rwkv_warmup_progress(
    progress: RwkvWarmUpProgressCallback | None,
    *,
    processed: int,
    total: int,
) -> None:
    if progress is not None:
        progress(
            RwkvWarmUpProgress(
                processed_reviews=processed,
                total_reviews=total,
            )
        )


class _RwkvReviewRetrievabilityCacheWriter:
    def __init__(
        self,
        reviewer: object,
        *,
        source: str = "rwkv_state_cache_build",
    ) -> None:
        self._col: Any | None = _collection(reviewer)
        self._source = source
        self._rows: list[tuple[int, float, str, int]] = []

    def record(self, review_id: int, retrievability: float) -> None:
        if (
            self._col is None
            or review_id <= 0
            or not math.isfinite(retrievability)
            or retrievability < 0
            or retrievability > 1
        ):
            return

        self._rows.append(
            (
                review_id,
                float(retrievability),
                self._source,
                int(time.time() * 1000),
            )
        )
        if len(self._rows) >= 1000:
            self.flush()

    def flush(self) -> None:
        if self._col is None or not self._rows:
            return

        rows = self._rows
        self._rows = []
        try:
            _ensure_rwkv_review_retrievability_cache_table(self._col)
            self._col.db.executemany(
                f"""
                INSERT OR REPLACE INTO {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                    (revlog_id, prediction, source, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
        except Exception:
            logger.exception("failed to store RWKV review retrievability cache")


def _ensure_rwkv_review_retrievability_cache_table(col: object) -> None:
    db = getattr(col, "db", None)
    execute = getattr(db, "execute", None)
    if not callable(execute):
        return

    required_columns = {"revlog_id", "prediction", "source", "updated_at"}
    columns = _rwkv_review_retrievability_cache_columns(col)
    if columns and not required_columns.issubset(columns):
        execute(f"DROP TABLE IF EXISTS {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}")

    execute(f"""
    CREATE TABLE IF NOT EXISTS {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
        revlog_id INTEGER PRIMARY KEY,
        prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
        source TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """)


def _rwkv_review_retrievability_cache_columns(col: object) -> set[str]:
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return set()

    try:
        rows = all_rows(f"PRAGMA table_info({_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE})")
    except Exception:
        logger.debug("failed to inspect RWKV review retrievability cache table")
        return set()

    return {
        column_name
        for row in rows
        if len(row) > 1 and isinstance((column_name := row[1]), str)
    }


def set_reviewer_backend(
    backend: RwkvReviewerBackend | None,
) -> RwkvReviewerBackend | None:
    global _reviewer_backend

    previous = _reviewer_backend
    _reviewer_backend = backend
    _reviewer_backend_warmup_keys.clear()
    _reviewer_backend_warmup_pending_keys.clear()
    _resolved_preset_id_cache.clear()
    _rwkv_review_queue_score_maps.clear()
    _rwkv_review_queue_score_generations.clear()
    _rwkv_score_prewarm_in_flight.clear()
    return previous


def configure_reviewer_backend_from_environment() -> bool:
    if _reviewer_backend is not None:
        return True

    start = time.monotonic()
    benchmark_path = os.environ.get("ANKI_RWKV_BENCHMARK_PATH")
    model_path = os.environ.get("ANKI_RWKV_MODEL_PATH")
    device = os.environ.get("ANKI_RWKV_DEVICE", "cpu")
    dtype = os.environ.get("ANKI_RWKV_DTYPE", "float")
    logger.debug(
        "RWKV scheduler backend configure started: benchmark_path=%s model_path=%s "
        "device=%s dtype=%s",
        bool(benchmark_path),
        str(model_path or embedded_rwkv_model_path()),
        device,
        dtype,
    )

    if benchmark_path and not model_path:
        logger.warning(
            "RWKV scheduler requires ANKI_RWKV_MODEL_PATH when ANKI_RWKV_BENCHMARK_PATH is set"
        )
        return False

    try:
        if benchmark_path:
            from aqt.rwkv_srs_benchmark import SrsBenchmarkRwkvReviewerBackend

            set_reviewer_backend(
                SrsBenchmarkRwkvReviewerBackend(
                    benchmark_path=benchmark_path,
                    model_path=model_path,
                    device=device,
                    dtype=dtype,
                )
            )
            logger.debug(
                "RWKV scheduler backend configured: backend=%s elapsed_ms=%.1f",
                type(_reviewer_backend).__name__,
                (time.monotonic() - start) * 1000,
            )
            return True

        resolved_model_path = _current_embedded_rwkv_model_path()
        if resolved_model_path is None:
            return False

        from aqt.rwkv_srs_benchmark import EmbeddedRwkvReviewerBackend

        set_reviewer_backend(
            EmbeddedRwkvReviewerBackend(
                model_path=resolved_model_path,
                device=device,
                dtype=dtype,
            )
        )
        logger.debug(
            "RWKV scheduler backend configured: backend=%s elapsed_ms=%.1f",
            type(_reviewer_backend).__name__,
            (time.monotonic() - start) * 1000,
        )
        return True
    except ModuleNotFoundError as exc:
        if exc.name == "torch":
            logger.debug("RWKV scheduler backend unavailable: PyTorch is not installed")
            return False

        logger.exception("failed to configure RWKV scheduler backend")
        return False
    except Exception:
        logger.exception("failed to configure RWKV scheduler backend")
        return False


def embedded_rwkv_model_path() -> Path | None:
    path = Path(__file__).parent / "rwkv_inference" / _EMBEDDED_RWKV_MODEL_FILENAME
    return path if path.exists() else None


def update_reviewer_scheduling_states(
    states: SchedulingStates,
    reviewer: object,
    card: object,
) -> SchedulingStates:
    """Apply desktop RWKV predictions before answer buttons are rendered."""

    if _reviewer_backend is None:
        return states

    try:
        review_enabled = rwkv_review_enabled(reviewer, card)
        if review_enabled and not _prepare_reviewer_backend_for_review(reviewer):
            logger.debug("RWKV scheduling prediction skipped: warm-up pending")
            return states
        prediction = _reviewer_backend.predict_review(
            reviewer=reviewer,
            card=card,
        )
        if prediction is None:
            return states

        _validate_prediction(prediction)
        has_interval_overrides = _has_interval_overrides(prediction.interval_overrides)
        _store_reviewer_prediction(
            reviewer,
            card,
            prediction,
            review_enabled=review_enabled,
            interval_override_used=review_enabled and has_interval_overrides,
        )
        if review_enabled and has_interval_overrides:
            return apply_review_interval_overrides(
                states,
                prediction.interval_overrides,
                prediction.s90_overrides,
            )
    except Exception:
        logger.exception("RWKV scheduling prediction failed")

    return states


def record_reviewer_answer(
    reviewer: object,
    card: object,
    ease: int,
) -> None:
    """Update desktop RWKV state after a real review has been answered."""

    if _reviewer_backend is None:
        return

    try:
        if rwkv_review_enabled(
            reviewer, card
        ) and not _prepare_reviewer_backend_for_review(reviewer):
            logger.debug("RWKV answer update skipped: warm-up pending")
            return
        _reviewer_backend.review_answered(
            reviewer=reviewer,
            card=card,
            ease=ease,
        )
        _store_answered_card_rwkv_review_retrievability(reviewer, card)
        _store_answered_card_rwkv_s90(reviewer, card, ease)
        card_id = _card_id(card)
        if card_id is not None:
            _set_rwkv_card_info_score(reviewer, card_id, None)
            _invalidate_resolved_preset_id_cache(reviewer, card_ids=[card_id])
    except Exception:
        logger.exception("RWKV review state update failed")


def refresh_answered_card_queue_score(
    reviewer: object,
    card: object,
) -> None:
    """Refresh the answered card's installed RWKV queue score after state changes."""

    deck_id = _current_deck_id(reviewer)
    card_id = _card_id(card)
    if deck_id is None or card_id is None:
        return

    existing_scores = _rwkv_review_queue_score_maps.get(deck_id)
    if existing_scores is None:
        return

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_config_enabled(deck_config)
        and _rwkv_review_instant_order_enabled(deck_config)
        and _review_order_uses_retrievability(deck_config)
    ):
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return

    updated_scores = dict(existing_scores)
    updated_scores.pop(card_id, None)

    try:
        scores = _rwkv_review_queue_scores(
            reviewer=reviewer,
            card_ids=[card_id],
            batch_size=_rwkv_review_batch_size(deck_config),
        )
    except Exception:
        logger.exception("RWKV answered-card queue score refresh failed")
        scores = []

    for scored_card_id, retrievability in scores:
        if scored_card_id == card_id:
            updated_scores[scored_card_id] = retrievability

    _set_rwkv_review_queue_scores(
        reviewer,
        deck_id,
        sorted(updated_scores.items()),
        fresh_for_backend_state=False,
    )


def prepare_reviewer_queue_order(reviewer: object) -> None:
    """Prepare transient RWKV review ordering scores for the current deck."""

    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        _clear_rwkv_review_queue_scores(reviewer)
        return

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_config_enabled(deck_config)
        and _rwkv_review_instant_order_enabled(deck_config)
        and _review_order_uses_retrievability(deck_config)
    ):
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return

    _prepare_rwkv_review_scores_for_deck(
        reviewer=reviewer,
        deck_id=deck_id,
        deck_config=deck_config,
        reason="review queue",
    )


def reviewer_queue_order_enabled(reviewer: object) -> bool:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        return False

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return (
        isinstance(deck_config, dict)
        and _rwkv_review_config_enabled(deck_config)
        and _rwkv_review_instant_order_enabled(deck_config)
        and _review_order_uses_retrievability(deck_config)
    )


def reviewer_queue_order_refresh_due(reviewer: object) -> bool:
    card = getattr(reviewer, "card", None)
    deck_config = _rwkv_review_enabled_deck_config(reviewer, card)
    if deck_config is None or not (
        _rwkv_review_instant_order_enabled(deck_config)
        and _review_order_uses_retrievability(deck_config)
    ):
        return False

    answered_ids = getattr(reviewer, "_answeredIds", None)
    answered_count = len(answered_ids) if isinstance(answered_ids, list) else 0
    interval = _rwkv_review_refresh_interval(deck_config)
    return answered_count > 0 and answered_count % interval == 0


def reviewer_queue_order_refresh_on_exit_enabled(reviewer: object) -> bool:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        return False

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return (
        isinstance(deck_config, dict)
        and _rwkv_review_config_enabled(deck_config)
        and _rwkv_review_instant_order_enabled(deck_config)
        and _review_order_uses_retrievability(deck_config)
        and _rwkv_review_refresh_on_exit(deck_config)
    )


def prepare_stats_retrievability_scores(reviewer: object, search: str) -> None:
    """Prepare transient RWKV scores for cards matched by a stats graph search."""

    if _reviewer_backend is None:
        configure_start = time.monotonic()
        configure_reviewer_backend_from_environment()
        logger.debug(
            "RWKV stats backend configure finished: search=%r backend=%s elapsed_ms=%.1f",
            search,
            type(_reviewer_backend).__name__ if _reviewer_backend is not None else None,
            (time.monotonic() - configure_start) * 1000,
        )
    if _reviewer_backend is None:
        _set_rwkv_stats_graph_scores(reviewer, search, [])
        return

    start = time.monotonic()
    prepare_key: RwkvStatsPrepareKey | None = None
    prepare_event: threading.Event | None = None
    owns_prepare = False
    try:
        logger.debug("RWKV stats preparation started: search=%r", search)
        warmup_start = time.monotonic()
        warmed_up = _prepare_reviewer_backend_for_stats(reviewer)
        if not warmed_up and _reviewer_backend_warmup_pending(reviewer):
            warmed_up = _wait_for_reviewer_backend_warmup(
                reviewer,
                timeout_secs=_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS,
            )
            if warmed_up:
                warmed_up = _prepare_reviewer_backend_for_stats(reviewer)
        warmup_elapsed_ms = (time.monotonic() - warmup_start) * 1000
        logger.debug(
            "RWKV stats warm-up finished: search=%r warmed_up=%s elapsed_ms=%.1f",
            search,
            warmed_up,
            warmup_elapsed_ms,
        )
        if not warmed_up:
            _set_rwkv_stats_graph_scores(reviewer, search, [])
            logger.debug(
                "RWKV stats retrievability scoring skipped: warm-up pending search=%r",
                search,
            )
            return
        prepare_key = _rwkv_stats_prepare_key(reviewer, search)
        if prepare_key is not None:
            prepare_event, owns_prepare = _begin_rwkv_stats_prepare(prepare_key)
            if not owns_prepare:
                wait_start = time.monotonic()
                logger.debug(
                    "RWKV stats preparation waiting for in-flight result: search=%r",
                    search,
                )
                prepare_event.wait()
                logger.debug(
                    "RWKV stats preparation reused in-flight result: search=%r "
                    "elapsed_ms=%.1f",
                    search,
                    (time.monotonic() - wait_start) * 1000,
                )
                return
        search_score_start = time.monotonic()
        search_score_result = _rwkv_stats_graph_scores_for_search(
            reviewer=reviewer,
            search=search,
        )
        search_score_elapsed_ms = (time.monotonic() - search_score_start) * 1000
        if search_score_result is not None:
            scores, input_build = search_score_result
            set_start = time.monotonic()
            _set_rwkv_stats_graph_scores(reviewer, search, scores)
            set_elapsed_ms = (time.monotonic() - set_start) * 1000
            logger.debug(
                "prepared RWKV stats retrievability scores from backend search: "
                "search=%r loaded=%s scored=%s warmup_elapsed_ms=%.1f "
                "score_elapsed_ms=%.1f set_elapsed_ms=%.1f elapsed_ms=%.1f",
                search,
                input_build.parsed_cards,
                len(scores),
                warmup_elapsed_ms,
                search_score_elapsed_ms,
                set_elapsed_ms,
                (time.monotonic() - start) * 1000,
            )
            return
        card_ids_start = time.monotonic()
        card_ids = _stats_graph_card_ids(reviewer, search)
        card_ids_elapsed_ms = (time.monotonic() - card_ids_start) * 1000
        logger.debug(
            "RWKV stats card search finished: search=%r candidates=%s elapsed_ms=%.1f",
            search,
            len(card_ids),
            card_ids_elapsed_ms,
        )
        score_start = time.monotonic()
        scores = _rwkv_stats_graph_scores(reviewer=reviewer, card_ids=card_ids)
        score_elapsed_ms = (time.monotonic() - score_start) * 1000
        set_start = time.monotonic()
        _set_rwkv_stats_graph_scores(reviewer, search, scores)
        set_elapsed_ms = (time.monotonic() - set_start) * 1000
        logger.debug(
            "prepared RWKV stats retrievability scores: search=%r candidates=%s scored=%s "
            "warmup_elapsed_ms=%.1f card_ids_elapsed_ms=%.1f score_elapsed_ms=%.1f "
            "set_elapsed_ms=%.1f elapsed_ms=%.1f",
            search,
            len(card_ids),
            len(scores),
            warmup_elapsed_ms,
            card_ids_elapsed_ms,
            score_elapsed_ms,
            set_elapsed_ms,
            (time.monotonic() - start) * 1000,
        )
    except Exception:
        logger.exception("RWKV stats retrievability scoring failed")
        _set_rwkv_stats_graph_scores(reviewer, search, [])
    finally:
        if owns_prepare and prepare_key is not None and prepare_event is not None:
            _finish_rwkv_stats_prepare(prepare_key, prepare_event)


def _prepare_reviewer_backend_for_stats(reviewer: object) -> bool:
    """Use cached RWKV state for stats without building it inside /graphs."""

    if _reviewer_backend is None:
        return True
    if not _reviewer_backend_cacheable():
        return _warm_up_reviewer_backend(reviewer)

    return _prepare_reviewer_backend_from_cache(reviewer)


def _prepare_reviewer_backend_for_card_info(reviewer: object) -> bool:
    """Use an already-warmed or cached RWKV state for Card Info diagnostics."""

    if _reviewer_backend is None:
        return True
    if _reviewer_backend_warmed_up(reviewer):
        return True
    return _prepare_reviewer_backend_from_cache(reviewer)


def _prepare_reviewer_backend_for_review(reviewer: object) -> bool:
    """Use warmed or cached RWKV state for review-time intervals."""

    if _reviewer_backend is None:
        return True
    if _reviewer_backend_warmed_up(reviewer):
        return True
    if _reviewer_backend_warmup_pending(reviewer):
        return False
    return _prepare_reviewer_backend_from_cache(reviewer)


def _prepare_reviewer_backend_from_cache(
    reviewer: object,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Restore cached RWKV state without starting a full historical rebuild."""

    if not _reviewer_backend_cacheable():
        return False

    key = _reviewer_backend_warmup_key(reviewer)
    if key is None or key in _reviewer_backend_warmup_keys:
        return True
    if key in _reviewer_backend_warmup_pending_keys:
        return False

    _reviewer_backend_warmup_pending_keys.add(key)
    start = time.monotonic()
    try:
        restored = _restore_reviewer_backend_cache(reviewer, progress=progress)
        if restored:
            _reviewer_backend_warmup_keys.add(key)
            logger.debug(
                "restored RWKV reviewer state cache: elapsed_ms=%.1f",
                (time.monotonic() - start) * 1000,
            )
            return True

        logger.debug(
            "RWKV state cache unavailable: elapsed_ms=%.1f",
            (time.monotonic() - start) * 1000,
        )
        return False
    finally:
        _reviewer_backend_warmup_pending_keys.discard(key)


def _reviewer_backend_warmup_pending(reviewer: object) -> bool:
    key = _reviewer_backend_warmup_key(reviewer)
    return key is not None and key in _reviewer_backend_warmup_pending_keys


def _wait_for_reviewer_backend_warmup(
    reviewer: object,
    *,
    timeout_secs: float,
) -> bool:
    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        return True

    start = time.monotonic()
    deadline = start + timeout_secs
    while key in _reviewer_backend_warmup_pending_keys:
        remaining_secs = deadline - time.monotonic()
        if remaining_secs <= 0:
            logger.debug(
                "timed out waiting for RWKV warm-up before stats: elapsed_ms=%.1f",
                (time.monotonic() - start) * 1000,
            )
            return False
        time.sleep(min(_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS, remaining_secs))

    logger.debug(
        "waited for RWKV warm-up before stats: warmed_up=%s elapsed_ms=%.1f",
        key in _reviewer_backend_warmup_keys,
        (time.monotonic() - start) * 1000,
    )
    return key in _reviewer_backend_warmup_keys


def _rwkv_stats_prepare_key(
    reviewer: object,
    search: str,
) -> RwkvStatsPrepareKey | None:
    warmup_key = _reviewer_backend_warmup_key(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    if warmup_key is None or not isinstance(days_elapsed, int):
        return None

    backend_id, collection_id = warmup_key
    return (
        backend_id,
        collection_id,
        days_elapsed,
        _reviewer_backend_state_generation(),
        search,
    )


def _reviewer_backend_state_generation() -> int:
    backend = _reviewer_backend
    state_generation = getattr(backend, "state_generation", None)
    if not callable(state_generation):
        return 0

    try:
        value = state_generation()
    except Exception:
        logger.debug("failed to read RWKV backend state generation")
        return 0

    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _begin_rwkv_stats_prepare(
    key: RwkvStatsPrepareKey,
) -> tuple[threading.Event, bool]:
    with _rwkv_stats_prepare_lock:
        event = _rwkv_stats_prepare_in_flight.get(key)
        if event is not None:
            return event, False

        event = threading.Event()
        _rwkv_stats_prepare_in_flight[key] = event
        return event, True


def _finish_rwkv_stats_prepare(
    key: RwkvStatsPrepareKey,
    event: threading.Event,
) -> None:
    with _rwkv_stats_prepare_lock:
        if _rwkv_stats_prepare_in_flight.get(key) is event:
            del _rwkv_stats_prepare_in_flight[key]
        event.set()


def prewarm_reviewer_queue_score_cache(
    reviewer: object,
    *,
    reason: str = "reviewer",
) -> None:
    """Opportunistically pre-score likely queue scopes into the RWKV memo."""

    deck_ids = _rwkv_score_prewarm_deck_ids(reviewer)
    if not deck_ids:
        return

    key = _rwkv_score_prewarm_key(reviewer, deck_ids)
    if key is not None and not _begin_rwkv_score_prewarm(key):
        logger.debug(
            "RWKV score prewarm skipped: reason=%s deck_ids=%s already_in_flight=True",
            reason,
            deck_ids,
        )
        return

    start = time.monotonic()

    def prewarm() -> None:
        _prewarm_rwkv_review_scores_for_decks(
            reviewer,
            deck_ids,
            reason=reason,
        )

    def done(future: Future[None]) -> None:
        if key is not None:
            _finish_rwkv_score_prewarm(key)
        try:
            future.result()
        except Exception:
            logger.exception(
                "RWKV score prewarm failed: reason=%s deck_ids=%s",
                reason,
                deck_ids,
            )
            return

        logger.debug(
            "RWKV score prewarm finished: reason=%s deck_ids=%s elapsed_ms=%.1f",
            reason,
            deck_ids,
            (time.monotonic() - start) * 1000,
        )

    taskman = getattr(getattr(reviewer, "mw", None), "taskman", None)
    run_in_background = getattr(taskman, "run_in_background", None)
    if callable(run_in_background):
        run_in_background(prewarm, done, uses_collection=True)
        return

    try:
        prewarm()
    except Exception:
        if key is not None:
            _finish_rwkv_score_prewarm(key)
        raise
    done(_ImmediateFuture(None))


class _ImmediateFuture(Future[None]):
    def __init__(self, value: None) -> None:
        super().__init__()
        self.set_result(value)


def _begin_rwkv_score_prewarm(key: RwkvScorePrewarmKey) -> bool:
    with _rwkv_score_prewarm_lock:
        if key in _rwkv_score_prewarm_in_flight:
            return False
        _rwkv_score_prewarm_in_flight.add(key)
        return True


def _finish_rwkv_score_prewarm(key: RwkvScorePrewarmKey) -> None:
    with _rwkv_score_prewarm_lock:
        _rwkv_score_prewarm_in_flight.discard(key)


def _rwkv_score_prewarm_key(
    reviewer: object,
    deck_ids: Sequence[int],
) -> RwkvScorePrewarmKey | None:
    warmup_key = _reviewer_backend_warmup_key(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    if warmup_key is None or not isinstance(days_elapsed, int):
        return None

    backend_id, collection_id = warmup_key
    return (
        backend_id,
        collection_id,
        days_elapsed,
        _reviewer_backend_state_generation(),
        tuple(deck_ids),
    )


def _rwkv_score_prewarm_deck_ids(reviewer: object) -> list[int]:
    current_deck_id = _current_deck_id(reviewer)
    if current_deck_id is None:
        return []

    deck_ids = [current_deck_id]
    parent_deck_id = _immediate_parent_deck_id(reviewer, current_deck_id)
    if parent_deck_id is not None and parent_deck_id != current_deck_id:
        deck_ids.append(parent_deck_id)
    return deck_ids


def _immediate_parent_deck_id(reviewer: object, deck_id: int) -> int | None:
    decks = getattr(_collection(reviewer), "decks", None)
    get_deck = getattr(decks, "get", None)
    id_for_name = getattr(decks, "id_for_name", None)
    if not callable(get_deck) or not callable(id_for_name):
        return None

    try:
        deck = get_deck(deck_id)
    except Exception:
        logger.debug("failed to read deck for RWKV score prewarm")
        return None

    if not isinstance(deck, dict):
        return None
    name = deck.get("name")
    if not isinstance(name, str) or "::" not in name:
        return None

    parent_name = name.rsplit("::", 1)[0]
    try:
        parent_deck_id = id_for_name(parent_name, create=False)
    except TypeError:
        try:
            parent_deck_id = id_for_name(parent_name)
        except Exception:
            logger.debug("failed to resolve parent deck for RWKV score prewarm")
            return None
    except Exception:
        logger.debug("failed to resolve parent deck for RWKV score prewarm")
        return None

    return (
        parent_deck_id
        if isinstance(parent_deck_id, int) and not isinstance(parent_deck_id, bool)
        else None
    )


def _prewarm_rwkv_review_scores_for_decks(
    reviewer: object,
    deck_ids: Sequence[int],
    *,
    reason: str,
) -> None:
    if _reviewer_backend is None:
        configure_reviewer_backend_from_environment()
    if _reviewer_backend is None:
        return

    if not _prepare_reviewer_backend_for_stats(reviewer):
        logger.debug(
            "RWKV score prewarm skipped: reason=%s deck_ids=%s warmed_up=False",
            reason,
            list(deck_ids),
        )
        return

    total_candidates = 0
    total_scored = 0
    start = time.monotonic()
    for deck_id in deck_ids:
        deck_config = _deck_config_for_deck_id(reviewer, deck_id)
        if not (
            isinstance(deck_config, dict)
            and _rwkv_review_config_enabled(deck_config)
            and _rwkv_review_instant_order_enabled(deck_config)
            and _review_order_uses_retrievability(deck_config)
        ):
            continue

        deck_scores = _rwkv_review_queue_scores_for_deck(
            reviewer=reviewer,
            deck_id=deck_id,
            batch_size=_rwkv_review_batch_size(deck_config),
        )
        if deck_scores is not None:
            scores, input_build = deck_scores
            total_candidates += input_build.searched_rows
            total_scored += len(scores)
            continue

        card_ids = _review_card_ids_in_deck_tree(reviewer, deck_id)
        if not card_ids:
            continue

        scores = _rwkv_review_queue_scores(
            reviewer=reviewer,
            card_ids=card_ids,
            batch_size=_rwkv_review_batch_size(deck_config),
        )
        total_candidates += len(card_ids)
        total_scored += len(scores)

    logger.debug(
        "RWKV score prewarm scored: reason=%s deck_ids=%s candidates=%s scored=%s "
        "elapsed_ms=%.1f",
        reason,
        list(deck_ids),
        total_candidates,
        total_scored,
        (time.monotonic() - start) * 1000,
    )


def _reviewer_backend_cacheable() -> bool:
    backend = _reviewer_backend
    return callable(getattr(backend, "cache_snapshot", None)) and callable(
        getattr(backend, "restore_cache_snapshot", None)
    )


def _prepare_rwkv_review_scores_for_deck(
    *,
    reviewer: object,
    deck_id: int,
    deck_config: dict[str, object],
    reason: str,
) -> None:
    start = time.monotonic()
    if _reviewer_backend is None:
        configure_start = time.monotonic()
        configure_reviewer_backend_from_environment()
        logger.debug(
            "RWKV %s backend configure finished: deck_id=%s backend=%s elapsed_ms=%.1f",
            reason,
            deck_id,
            type(_reviewer_backend).__name__ if _reviewer_backend is not None else None,
            (time.monotonic() - configure_start) * 1000,
        )
    if _reviewer_backend is None:
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return

    try:
        warmup_start = time.monotonic()
        warmed_up = _warm_up_reviewer_backend(reviewer)
        warmup_elapsed_ms = (time.monotonic() - warmup_start) * 1000
        if not warmed_up:
            _clear_rwkv_review_queue_scores(reviewer, deck_id)
            logger.debug(
                "RWKV %s scoring skipped: deck_id=%s warmup_elapsed_ms=%.1f",
                reason,
                deck_id,
                warmup_elapsed_ms,
            )
            return
        deck_scores_start = time.monotonic()
        deck_scores = _rwkv_review_queue_scores_for_deck(
            reviewer=reviewer,
            deck_id=deck_id,
            batch_size=_rwkv_review_batch_size(deck_config),
        )
        deck_scores_elapsed_ms = (time.monotonic() - deck_scores_start) * 1000
        if deck_scores is not None:
            scores, input_build = deck_scores
            set_start = time.monotonic()
            _set_rwkv_review_queue_scores(reviewer, deck_id, scores)
            set_elapsed_ms = (time.monotonic() - set_start) * 1000
            logger.debug(
                "prepared RWKV %s scores from backend deck queue: deck_id=%s "
                "candidates=%s scored=%s warmup_elapsed_ms=%.1f "
                "scores_elapsed_ms=%.1f set_elapsed_ms=%.1f elapsed_ms=%.1f",
                reason,
                deck_id,
                input_build.searched_rows,
                len(scores),
                warmup_elapsed_ms,
                deck_scores_elapsed_ms,
                set_elapsed_ms,
                (time.monotonic() - start) * 1000,
            )
            return
        card_ids_start = time.monotonic()
        card_ids = _review_card_ids_in_deck_tree(reviewer, deck_id)
        card_ids_elapsed_ms = (time.monotonic() - card_ids_start) * 1000
        scores_start = time.monotonic()
        scores = _rwkv_review_queue_scores(
            reviewer=reviewer,
            card_ids=card_ids,
            batch_size=_rwkv_review_batch_size(deck_config),
        )
        scores_elapsed_ms = (time.monotonic() - scores_start) * 1000

        set_start = time.monotonic()
        _set_rwkv_review_queue_scores(reviewer, deck_id, scores)
        set_elapsed_ms = (time.monotonic() - set_start) * 1000
        logger.debug(
            "prepared RWKV %s scores: deck_id=%s candidates=%s scored=%s "
            "warmup_elapsed_ms=%.1f card_ids_elapsed_ms=%.1f "
            "scores_elapsed_ms=%.1f set_elapsed_ms=%.1f elapsed_ms=%.1f",
            reason,
            deck_id,
            len(card_ids),
            len(scores),
            warmup_elapsed_ms,
            card_ids_elapsed_ms,
            scores_elapsed_ms,
            set_elapsed_ms,
            (time.monotonic() - start) * 1000,
        )
    except Exception:
        logger.exception("RWKV %s scoring failed", reason)
        _clear_rwkv_review_queue_scores(reviewer, deck_id)


def current_reviewer_retrievability(
    reviewer: object,
    card: object,
) -> float | None:
    prediction = _current_reviewer_prediction(reviewer, card)
    return prediction.retrievability if prediction else None


def _active_rwkv_card_info_diagnostics(
    reviewer: object,
    card: object,
) -> RwkvReviewerDiagnostics | None:
    card_id = _card_id(card)
    if card_id is None or not rwkv_review_enabled(reviewer, card):
        return None

    retrievability = _active_rwkv_retrievability_score(reviewer, card_id)
    if retrievability is None:
        return None

    return RwkvReviewerDiagnostics(
        retrievability=retrievability,
        retrievability_source="RWKV",
    )


def current_reviewer_diagnostics(
    reviewer: object,
    card: object,
    *,
    fallback_source: str,
) -> RwkvReviewerDiagnostics | None:
    prediction = _current_reviewer_prediction(reviewer, card)
    if prediction is None:
        return None

    return RwkvReviewerDiagnostics(
        retrievability=prediction.retrievability,
        retrievability_source=_retrievability_source(prediction, fallback_source),
    )


def has_reviewer_prediction(reviewer: object) -> bool:
    return isinstance(
        getattr(reviewer, _REVIEWER_PREDICTION_ATTR, None), RwkvReviewerPrediction
    )


def has_reviewer_backend() -> bool:
    return configure_reviewer_backend_from_environment()


def rwkv_card_info_rows(
    *,
    reviewer: object,
    card: object,
    fallback_source: str,
) -> list[tuple[str, str]]:
    card_id = _card_id(card)
    store_card_info_score = True
    diagnostics = _active_rwkv_card_info_diagnostics(reviewer, card)
    if diagnostics is not None:
        store_card_info_score = False
    else:
        diagnostics = current_reviewer_diagnostics(
            reviewer,
            card,
            fallback_source=fallback_source,
        )
    if diagnostics is None:
        if _reviewer_backend is None and rwkv_review_enabled(reviewer, card):
            configure_reviewer_backend_from_environment()
        diagnostics = _queried_card_info_diagnostics(
            reviewer,
            card,
            fallback_source=fallback_source,
        )
    if diagnostics is None:
        if not rwkv_review_enabled(reviewer, card):
            card_id = _card_id(card)
            if card_id is not None:
                _set_rwkv_card_info_score(reviewer, card_id, None)
            return []
        diagnostics = RwkvReviewerDiagnostics(
            retrievability=None,
            retrievability_source=_unavailable_retrievability_source(fallback_source),
        )

    if card_id is not None and store_card_info_score:
        retrievability = (
            diagnostics.retrievability
            if diagnostics.retrievability is not None
            and rwkv_review_enabled(reviewer, card)
            else None
        )
        _set_rwkv_card_info_score(reviewer, card_id, retrievability)

    return [
        ("RWKV computed R", _format_retrievability(diagnostics.retrievability)),
        ("Retrievability source", diagnostics.retrievability_source),
    ]


def rwkv_review_enabled(
    reviewer: object,
    card: object,
) -> bool:
    return _rwkv_review_enabled_deck_config(reviewer, card) is not None


def _collection_has_rwkv_review_enabled(mw: object) -> bool:
    col = getattr(mw, "col", None)
    decks = getattr(col, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if not callable(all_config):
        return False

    try:
        configs = all_config()
    except Exception:
        logger.debug("failed to read deck configs for RWKV cache prompt")
        return False

    return any(
        isinstance(config, dict) and _rwkv_review_config_enabled(config)
        for config in configs
    )


def _rwkv_review_enabled_deck_config(
    reviewer: object,
    card: object,
) -> dict[str, object] | None:
    deck_id = _deck_id(card)
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not isinstance(deck_config, dict):
        return None

    return deck_config if _rwkv_review_config_enabled(deck_config) else None


def rwkv_review_identity(
    reviewer: object,
    card: object,
) -> RwkvReviewIdentity | None:
    card_id = _int_attr(card, "id")
    if card_id is None:
        return None
    deck_id = _deck_id(card)

    return RwkvReviewIdentity(
        card_id=card_id,
        note_id=_int_attr(card, "nid"),
        deck_id=deck_id,
        preset_id=_preset_id(reviewer, card_id, deck_id),
    )


def rwkv_review_input(
    *,
    reviewer: object,
    card: object,
    identity: RwkvReviewIdentity,
    ease: int | None,
) -> RwkvReviewInput:
    current_state = _current_scheduling_state(reviewer)
    state_kind, normal_state_kind = _scheduling_state_kinds(current_state)
    elapsed_days, elapsed_seconds = _scheduling_state_elapsed(current_state)

    return RwkvReviewInput(
        identity=identity,
        is_query=ease is None,
        ease=ease,
        duration_millis=_duration_millis(card, ease),
        card_type=_int_attr(card, "type"),
        card_queue=_int_attr(card, "queue"),
        card_due=_int_attr(card, "due"),
        interval_days=_int_attr(card, "ivl"),
        ease_factor=_int_attr(card, "factor"),
        reps=_int_attr(card, "reps"),
        lapses=_int_attr(card, "lapses"),
        day_offset=_day_offset(reviewer),
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
        target_retentions=_rwkv_target_retentions(
            reviewer=reviewer,
            card=card,
            states=_scheduling_states(reviewer),
        ),
    )


def _rwkv_target_retentions(
    *,
    reviewer: object,
    card: object,
    states: SchedulingStates | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    if states is not None and getattr(
        states, "dynamic_desired_retention_enabled", False
    ):
        retentions = tuple(
            value
            for value in getattr(states, "dynamic_desired_retentions", [])
            if _valid_probability(value)
        )
        if len(retentions) == 4:
            return cast(tuple[float, float, float, float], retentions)

    desired_retention = _reviewer_desired_retention_override(reviewer)
    if desired_retention is None:
        desired_retention = _desired_retention_for_card(reviewer, card)
    if desired_retention is None:
        desired_retention = _RWKV_DEFAULT_TARGET_RETENTION

    return (desired_retention, desired_retention, desired_retention, desired_retention)


def _reviewer_desired_retention_override(reviewer: object) -> float | None:
    value = getattr(reviewer, "_desired_retention_override", None)
    return value if _valid_probability(value) else None


def _desired_retention_for_card(reviewer: object, card: object) -> float | None:
    card_id = _card_id(card)
    if card_id is not None:
        mw = getattr(reviewer, "mw", None)
        col = getattr(mw, "col", None)
        fsrs_preset_for_card = getattr(col, "fsrs_preset_for_card", None)
        if callable(fsrs_preset_for_card):
            try:
                value = getattr(
                    fsrs_preset_for_card(card_id), "desired_retention", None
                )
            except Exception:
                logger.debug("failed to read FSRS preset desired retention for RWKV")
            else:
                if _valid_probability(value):
                    return cast(float, value)

    deck_config = _deck_config_for_deck_id(reviewer, _deck_id(card))
    if isinstance(deck_config, dict):
        value = deck_config.get(
            "desiredRetention", deck_config.get("desired_retention")
        )
        if _valid_probability(value):
            return cast(float, value)

    return None


def interval_from_recall_curve(
    points: Sequence[RwkvRecallPoint],
    target_retention: float,
    *,
    max_interval_days: int,
    nonmonotonic_tolerance: float = 1e-4,
) -> int | None:
    """Return the first interval where projected recall reaches the target."""

    if not _valid_probability(target_retention):
        raise ValueError("target_retention must be between 0 and 1")
    if max_interval_days < 1:
        raise ValueError("max_interval_days must be at least 1")
    if not math.isfinite(nonmonotonic_tolerance) or nonmonotonic_tolerance < 0:
        raise ValueError("nonmonotonic_tolerance must be finite and non-negative")

    ordered_points = sorted(points, key=lambda point: point.elapsed_days)
    _validate_recall_points(ordered_points)
    if not ordered_points:
        return None
    if not _recall_curve_is_monotonic(
        ordered_points,
        tolerance=nonmonotonic_tolerance,
    ):
        return None

    previous = ordered_points[0]
    if previous.retrievability <= target_retention:
        return _clamped_interval(previous.elapsed_days, max_interval_days)

    for point in ordered_points[1:]:
        if point.retrievability <= target_retention:
            return _clamped_interval(
                _interpolated_elapsed_days(previous, point, target_retention),
                max_interval_days,
            )

        previous = point

    return None


def apply_review_interval_overrides(
    states: SchedulingStates,
    overrides: RwkvIntervalOverride,
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride(),
) -> SchedulingStates:
    """Apply RWKV day intervals to review answers without mutating input states."""

    updated_states = SchedulingStates()
    updated_states.CopyFrom(states)

    for rating, interval in (
        ("again", overrides.again),
        ("hard", overrides.hard),
        ("good", overrides.good),
        ("easy", overrides.easy),
    ):
        if interval is None:
            continue
        _set_review_interval_if_present(
            getattr(updated_states, rating),
            _validated_interval(interval),
        )
        s90 = getattr(s90_overrides, rating)
        if s90 is not None:
            _set_review_s90_if_present(
                getattr(updated_states, rating),
                _validated_interval(s90),
            )

    return updated_states


def _validate_prediction(prediction: RwkvReviewPrediction) -> None:
    if prediction.retrievability is not None and not _valid_probability(
        prediction.retrievability
    ):
        raise ValueError("retrievability must be between 0 and 1")
    if prediction.current_interval is not None:
        _validated_interval(prediction.current_interval)
    if prediction.current_s90 is not None:
        _validated_interval(prediction.current_s90)
    for rating in _RWKV_RATING_FIELDS:
        interval = getattr(prediction.interval_overrides, rating)
        if interval is not None:
            _validated_interval(interval)
        s90 = getattr(prediction.s90_overrides, rating)
        if s90 is not None:
            _validated_interval(s90)


def _store_reviewer_prediction(
    reviewer: object,
    card: object,
    prediction: RwkvReviewPrediction,
    *,
    review_enabled: bool,
    interval_override_used: bool,
) -> None:
    card_id = _card_id(card)
    if card_id is None:
        return

    setattr(
        reviewer,
        _REVIEWER_PREDICTION_ATTR,
        RwkvReviewerPrediction(
            card_id=card_id,
            retrievability=prediction.retrievability,
            review_enabled=review_enabled,
            interval_override_used=interval_override_used,
            s90_overrides=prediction.s90_overrides,
        ),
    )


def _current_reviewer_prediction(
    reviewer: object,
    card: object,
) -> RwkvReviewerPrediction | None:
    prediction = getattr(reviewer, _REVIEWER_PREDICTION_ATTR, None)
    if not isinstance(prediction, RwkvReviewerPrediction):
        return None
    if prediction.card_id != _card_id(card):
        return None

    return prediction


def _store_answered_card_rwkv_s90(reviewer: object, card: object, ease: int) -> None:
    prediction = _current_reviewer_prediction(reviewer, card)
    if prediction is None or not prediction.review_enabled:
        return

    s90 = _s90_for_ease(prediction.s90_overrides, ease)
    if s90 is None:
        return

    load = getattr(card, "load", None)
    if callable(load):
        try:
            load()
        except Exception:
            logger.debug("failed to reload answered card before storing RWKV S90")
            return

    memory_state = getattr(card, "memory_state", None)
    if memory_state is None:
        memory_state = cards_pb2.FsrsMemoryState(difficulty=5.0)
        setattr(card, "memory_state", memory_state)
    elif getattr(memory_state, "difficulty", 0) <= 0:
        memory_state.difficulty = 5.0

    memory_state.stability = float(s90)

    col = getattr(card, "col", None)
    if col is None:
        mw = getattr(reviewer, "mw", None)
        col = getattr(mw, "col", None)
    update_card = getattr(col, "update_card", None)
    if not callable(update_card):
        return

    try:
        update_card(card, skip_undo_entry=True)
    except Exception:
        logger.exception("failed to store RWKV S90 on answered card")


def _store_answered_card_rwkv_review_retrievability(
    reviewer: object,
    card: object,
) -> None:
    prediction = _current_reviewer_prediction(reviewer, card)
    if (
        prediction is None
        or not prediction.review_enabled
        or not _valid_probability(prediction.retrievability)
    ):
        return

    card_id = _card_id(card)
    if card_id is None:
        return

    review_id = _latest_rwkv_review_id_for_card(reviewer, card_id)
    if review_id is None:
        logger.debug(
            "RWKV review retrievability cache skipped: no review row for card %s",
            card_id,
        )
        return

    writer = _RwkvReviewRetrievabilityCacheWriter(reviewer, source="rwkv_review")
    writer.record(review_id, prediction.retrievability)
    writer.flush()


def _latest_rwkv_review_id_for_card(reviewer: object, card_id: int) -> int | None:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    if not callable(scalar):
        return None

    try:
        review_id = scalar(
            """
            select id
            from revlog
            where cid = ?
                and ease between 1 and 4
                and type in (0, 1, 2, 3)
            order by id desc
            limit 1
            """,
            card_id,
        )
    except Exception:
        logger.debug("failed to find latest RWKV review id for card %s", card_id)
        return None

    if isinstance(review_id, int) and not isinstance(review_id, bool) and review_id > 0:
        return review_id
    return None


def _s90_for_ease(overrides: RwkvIntervalOverride, ease: int) -> int | None:
    if 1 <= ease <= len(_RWKV_RATING_FIELDS):
        return cast(int | None, getattr(overrides, _RWKV_RATING_FIELDS[ease - 1]))
    return None


def _queried_card_info_diagnostics(
    reviewer: object,
    card: object,
    *,
    fallback_source: str,
) -> RwkvReviewerDiagnostics | None:
    if _reviewer_backend is None:
        return None

    card_id = _card_id(card)
    if card_id is None:
        return None

    try:
        candidate = _card_info_review_candidate(reviewer, card)
        review_enabled = rwkv_review_enabled(candidate.reviewer, candidate.card)
        if review_enabled and not _prepare_reviewer_backend_for_card_info(reviewer):
            logger.debug("RWKV card info prediction skipped: warm-up pending")
            return None
        predictions = _predict_review_batch([candidate])
        prediction = predictions[0] if predictions else None
        if prediction is None:
            return None

        _validate_prediction(prediction)
        reviewer_prediction = RwkvReviewerPrediction(
            card_id=card_id,
            retrievability=prediction.retrievability,
            review_enabled=review_enabled,
            interval_override_used=(
                review_enabled
                and _has_interval_overrides(prediction.interval_overrides)
            ),
        )
        return RwkvReviewerDiagnostics(
            retrievability=reviewer_prediction.retrievability,
            retrievability_source=_retrievability_source(
                reviewer_prediction,
                fallback_source,
            ),
        )
    except Exception:
        logger.exception("RWKV card info prediction failed")
        return None


def _card_info_review_candidate(reviewer: object, card: object) -> RwkvReviewCandidate:
    candidate = _shared_card_info_review_candidate(reviewer, card)
    if candidate is not None:
        return candidate

    states = _scheduling_states_for_card(reviewer, card)
    if states is None:
        return RwkvReviewCandidate(reviewer=reviewer, card=card)

    context = SimpleNamespace(
        mw=getattr(reviewer, "mw", None),
        _v3=SimpleNamespace(states=states),
    )
    return RwkvReviewCandidate(reviewer=context, card=card)


def _shared_card_info_review_candidate(
    reviewer: object,
    card: object,
) -> RwkvReviewCandidate | None:
    card_id = _card_id(card)
    if card_id is None:
        return None

    timing = _timing_today(reviewer)
    if timing is None:
        return None

    loaded_cards = _rwkv_cards_for_ids(reviewer, [card_id], reason="card info")
    if len(loaded_cards) != 1:
        return None

    loaded_card = loaded_cards[0]
    states = _stats_graph_scheduling_states(
        loaded_card,
        timing,
        include_suspended_review=True,
    )
    if states is None:
        return None

    deck_config = _deck_config_for_deck_id(reviewer, loaded_card.current_deck_id())
    if not isinstance(deck_config, dict):
        return None

    context = _stats_graph_reviewer_context(
        deck_config=deck_config,
        states=states,
        timing=timing,
        resolved_preset_id=_resolved_fsrs_preset_ids(reviewer, [card_id]).get(card_id),
    )
    return RwkvReviewCandidate(reviewer=context, card=loaded_card)


def _scheduling_states_for_card(
    reviewer: object,
    card: object,
) -> SchedulingStates | None:
    card_id = _card_id(card)
    if card_id is None:
        return None

    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    sched = getattr(col, "sched", None)
    get_scheduling_states = getattr(sched, "get_scheduling_states", None)
    if not callable(get_scheduling_states):
        return None

    try:
        states = get_scheduling_states(card_id)
    except Exception:
        logger.debug("failed to read scheduling states for RWKV card info")
        return None

    return states if isinstance(states, SchedulingStates) else None


def _retrievability_source(
    prediction: RwkvReviewerPrediction,
    fallback_source: str,
) -> str:
    if prediction.review_enabled and prediction.interval_override_used:
        return "RWKV"
    if prediction.review_enabled:
        return f"{fallback_source} (RWKV interval unavailable)"
    return f"{fallback_source} (RWKV disabled)"


def _unavailable_retrievability_source(fallback_source: str) -> str:
    if _reviewer_backend is None:
        return f"{fallback_source} (RWKV backend unavailable)"
    return f"{fallback_source} (RWKV unavailable)"


def _format_retrievability(retrievability: float | None) -> str:
    if retrievability is None:
        return "Unavailable"

    return f"{retrievability * 100:.0f}%"


def _card_id(card: object) -> int | None:
    return _int_attr(card, "id")


def _deck_id(card: object) -> int | None:
    current_deck_id = getattr(card, "current_deck_id", None)
    if callable(current_deck_id):
        try:
            value = current_deck_id()
            if isinstance(value, int):
                return value
        except Exception:
            logger.debug("failed to read current deck id for RWKV review input")

    return _int_attr(card, "did")


def _preset_id(
    reviewer: object,
    card_id: int,
    deck_id: int | None,
) -> int | None:
    resolved_preset_id = _resolved_fsrs_preset_id(reviewer, card_id)
    if resolved_preset_id is not None:
        return _stable_preset_id(resolved_preset_id)

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if isinstance(deck_config, dict):
        value = deck_config.get("id")
        if isinstance(value, int):
            return value

    return None


def _preset_ids_for_card_ids(
    reviewer: object,
    card_ids: Sequence[int],
) -> dict[int, int]:
    return {
        card_id: _stable_preset_id(preset_id)
        for card_id, preset_id in _resolved_fsrs_preset_ids(
            reviewer,
            card_ids,
        ).items()
    }


def _resolved_fsrs_preset_id(reviewer: object, card_id: int) -> str | None:
    resolved_preset_id = getattr(reviewer, "_rwkv_resolved_preset_id", None)
    if isinstance(resolved_preset_id, str) and resolved_preset_id:
        return resolved_preset_id

    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    fsrs_preset_for_card = getattr(col, "fsrs_preset_for_card", None)
    if not callable(fsrs_preset_for_card):
        return None

    try:
        preset_id = getattr(fsrs_preset_for_card(card_id), "id", None)
    except Exception:
        logger.debug("failed to resolve FSRS preset for RWKV review input")
        return None

    return preset_id if isinstance(preset_id, str) and preset_id else None


def _resolved_fsrs_preset_ids(
    reviewer: object,
    card_ids: Sequence[int],
) -> dict[int, str]:
    if not card_ids:
        return {}

    collection_key = _preset_id_cache_key(reviewer)
    cache = _resolved_preset_id_cache.setdefault(collection_key, {})
    resolved = {card_id: cache[card_id] for card_id in card_ids if card_id in cache}
    missing_card_ids = [card_id for card_id in card_ids if card_id not in resolved]
    if not missing_card_ids:
        logger.debug(
            "RWKV FSRS preset ids resolved from cache: cards=%s",
            len(card_ids),
        )
        return resolved

    start = time.monotonic()
    col = _collection(reviewer)
    backend = getattr(col, "_backend", None)
    get_preset_ids = getattr(backend, "get_fsrs_preset_ids_for_cards", None)
    if callable(get_preset_ids):
        try:
            logger.debug(
                "RWKV FSRS preset batch resolve started: cards=%s cached=%s missing=%s",
                len(card_ids),
                len(resolved),
                len(missing_card_ids),
            )
            response = get_preset_ids(missing_card_ids)
            batch_resolved = _fsrs_preset_ids_response_items(response)
            cache.update(batch_resolved)
            resolved.update(batch_resolved)
            logger.debug(
                "RWKV FSRS preset batch resolve finished: cards=%s cached=%s "
                "missing=%s resolved=%s elapsed_ms=%.1f",
                len(card_ids),
                len(card_ids) - len(missing_card_ids),
                len(missing_card_ids),
                len(resolved),
                (time.monotonic() - start) * 1000,
            )
            return resolved
        except Exception:
            logger.debug("failed to batch-resolve FSRS presets for RWKV review input")

    for card_id in missing_card_ids:
        preset_id = _resolved_fsrs_preset_id(reviewer, card_id)
        if preset_id is not None:
            cache[card_id] = preset_id
            resolved[card_id] = preset_id
    logger.debug(
        "RWKV FSRS preset per-card resolve finished: cards=%s cached=%s missing=%s "
        "resolved=%s elapsed_ms=%.1f",
        len(card_ids),
        len(card_ids) - len(missing_card_ids),
        len(missing_card_ids),
        len(resolved),
        (time.monotonic() - start) * 1000,
    )
    return resolved


def _preset_id_cache_key(reviewer: object) -> tuple[int, str | None]:
    col = _collection(reviewer)
    path = getattr(col, "path", None)
    return (id(col), path if isinstance(path, str) else None)


def _invalidate_resolved_preset_id_cache(
    reviewer: object,
    *,
    card_ids: Sequence[int] | None = None,
) -> None:
    cache = _resolved_preset_id_cache.get(_preset_id_cache_key(reviewer))
    if cache is None:
        return
    if card_ids is None:
        cache.clear()
    else:
        for card_id in card_ids:
            cache.pop(card_id, None)


def _fsrs_preset_ids_response_items(response: object) -> dict[int, str]:
    resolved: dict[int, str] = {}
    items = getattr(response, "items", None)
    if items is None or callable(items):
        items = response

    try:
        iterator = iter(items)
    except TypeError:
        return resolved

    for item in iterator:
        card_id = getattr(item, "card_id", None)
        preset_id = getattr(item, "preset_id", None)
        if isinstance(card_id, int) and isinstance(preset_id, str) and preset_id:
            resolved[card_id] = preset_id
    return resolved


def _stable_preset_id(preset_id: str) -> int:
    if preset_id.isdecimal():
        return int(preset_id)

    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _deck_config_for_deck_id(
    reviewer: object,
    deck_id: int | None,
) -> object | None:
    if deck_id is None:
        return None

    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    decks = getattr(col, "decks", None)
    config_dict_for_deck_id = getattr(decks, "config_dict_for_deck_id", None)
    if not callable(config_dict_for_deck_id):
        return None

    try:
        return config_dict_for_deck_id(deck_id)
    except Exception:
        logger.debug("failed to read deck config for RWKV review input")
        return None


def _reviewer_backend_warmed_up(reviewer: object) -> bool:
    key = _reviewer_backend_warmup_key(reviewer)
    return key is None or key in _reviewer_backend_warmup_keys


def _warm_up_reviewer_backend(
    reviewer: object,
    *,
    force_rebuild: bool = False,
    require_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    backend = _reviewer_backend
    if backend is None:
        return True

    warm_up = getattr(backend, "warm_up", None)
    if not callable(warm_up):
        return True

    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        return True

    if force_rebuild:
        _reviewer_backend_warmup_keys.discard(key)
    elif key in _reviewer_backend_warmup_keys:
        if (
            not require_retrievability_cache
            or _existing_rwkv_review_retrievability_cache_complete(reviewer)
        ):
            return True
        _reviewer_backend_warmup_keys.discard(key)
    if key in _reviewer_backend_warmup_pending_keys:
        return False

    _reviewer_backend_warmup_pending_keys.add(key)
    start = time.monotonic()
    try:
        logger.debug("RWKV historical warm-up started")
        _report_rwkv_state_cache_progress(
            progress,
            "Checking RWKV state cache...",
        )
        restore_start = time.monotonic()
        if force_rebuild:
            reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
            if callable(reset_cache_snapshot):
                reset_cache_snapshot()
            _report_rwkv_state_cache_progress(
                progress,
                "Forcing RWKV state cache rebuild...",
            )
            restore_elapsed_ms = 0.0
        else:
            restored = _restore_reviewer_backend_cache(
                reviewer,
                require_retrievability_cache=require_retrievability_cache,
                progress=progress,
            )
            restore_elapsed_ms = (time.monotonic() - restore_start) * 1000
            if restored:
                _reviewer_backend_warmup_keys.add(key)
                logger.debug(
                    "restored RWKV reviewer state cache: elapsed_ms=%.1f",
                    restore_elapsed_ms,
                )
                return True
            reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
            if callable(reset_cache_snapshot):
                reset_cache_snapshot()

        _report_rwkv_state_cache_progress(
            progress,
            "Loading RWKV review history...",
        )
        history_start = time.monotonic()
        history = _historical_rwkv_review_inputs(reviewer)
        history_elapsed_ms = (time.monotonic() - history_start) * 1000
        logger.debug(
            "RWKV historical warm-up inputs prepared: reviews=%s review_count=%s "
            "last_review_id=%s elapsed_ms=%.1f",
            len(history.reviews),
            history.review_count,
            history.last_review_id,
            history_elapsed_ms,
        )
        warm_up_start = time.monotonic()
        _warm_up_rwkv_reviews(
            reviewer,
            backend,
            warm_up,
            history.reviews,
            review_ids=history.review_ids,
            progress=progress,
            label="Building RWKV state cache",
        )
        warm_up_elapsed_ms = (time.monotonic() - warm_up_start) * 1000
        _report_rwkv_state_cache_progress(
            progress,
            "Saving RWKV state cache...",
        )
        save_start = time.monotonic()
        _save_reviewer_backend_cache(reviewer, history)
        save_elapsed_ms = (time.monotonic() - save_start) * 1000
        _reviewer_backend_warmup_keys.add(key)
        logger.debug(
            "warmed RWKV reviewer state: reviews=%s restore_elapsed_ms=%.1f "
            "history_elapsed_ms=%.1f warm_up_elapsed_ms=%.1f "
            "save_elapsed_ms=%.1f elapsed_ms=%.1f",
            len(history.reviews),
            restore_elapsed_ms,
            history_elapsed_ms,
            warm_up_elapsed_ms,
            save_elapsed_ms,
            (time.monotonic() - start) * 1000,
        )
        return True
    except Exception:
        logger.exception("RWKV historical warm-up failed")
        return False
    finally:
        _reviewer_backend_warmup_pending_keys.discard(key)


def _reviewer_backend_warmup_key(reviewer: object) -> tuple[int, int] | None:
    backend = _reviewer_backend
    col = _collection(reviewer)
    if backend is None or col is None or getattr(col, "db", None) is None:
        return None

    return (id(backend), id(col))


def _warm_up_rwkv_reviews(
    reviewer: object,
    backend: object,
    warm_up: object,
    reviews: Sequence[RwkvReviewInput],
    *,
    review_ids: Sequence[int] | None = None,
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
) -> None:
    if isinstance(backend, RwkvStatefulReviewerBackend):
        writer = _RwkvReviewRetrievabilityCacheWriter(reviewer)
        started_at = time.monotonic()
        try:
            backend.warm_up(
                reviews,
                review_ids=review_ids,
                prediction_recorder=writer.record,
                progress=lambda replay_progress: _report_rwkv_review_replay_progress(
                    progress,
                    label=label,
                    replay_progress=replay_progress,
                    elapsed_seconds=time.monotonic() - started_at,
                ),
            )
        finally:
            writer.flush()
        return

    if callable(warm_up):
        warm_up_callable = cast(Callable[..., Any], warm_up)
        warm_up_parameters = _callable_parameters(warm_up_callable)
        if _supports_rwkv_warm_up_prediction_recorder(warm_up_parameters):
            writer = _RwkvReviewRetrievabilityCacheWriter(reviewer)
            started_at = time.monotonic()
            kwargs: dict[str, object] = {
                "review_ids": review_ids,
                "prediction_recorder": writer.record,
            }
            if _callable_accepts_keyword(warm_up_parameters, "progress"):
                kwargs["progress"] = (
                    lambda replay_progress: _report_rwkv_review_replay_progress(
                        progress,
                        label=label,
                        replay_progress=replay_progress,
                        elapsed_seconds=time.monotonic() - started_at,
                    )
                )
            try:
                warm_up_callable(reviews, **kwargs)
            finally:
                writer.flush()
            return

        warm_up_callable(reviews)


def _callable_parameters(
    callable_object: Callable[..., Any],
) -> dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(callable_object).parameters)
    except (TypeError, ValueError):
        return {}


def _supports_rwkv_warm_up_prediction_recorder(
    parameters: dict[str, inspect.Parameter],
) -> bool:
    return _callable_accepts_keyword(
        parameters,
        "review_ids",
    ) and _callable_accepts_keyword(parameters, "prediction_recorder")


def _callable_accepts_keyword(
    parameters: dict[str, inspect.Parameter],
    keyword: str,
) -> bool:
    return keyword in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _report_rwkv_review_replay_progress(
    progress_callback: RwkvStateCacheProgressCallback | None,
    *,
    label: str,
    replay_progress: RwkvWarmUpProgress,
    elapsed_seconds: float,
) -> None:
    total = replay_progress.total_reviews
    processed = min(replay_progress.processed_reviews, total)
    _report_rwkv_state_cache_progress(
        progress=progress_callback,
        label=_rwkv_replay_progress_label(
            label,
            replay_progress,
            elapsed_seconds=elapsed_seconds,
        ),
        value=processed,
        maximum=total,
    )


def _rwkv_replay_progress_label(
    label: str,
    replay_progress: RwkvWarmUpProgress,
    *,
    elapsed_seconds: float,
) -> str:
    total = max(replay_progress.total_reviews, 0)
    processed = min(max(replay_progress.processed_reviews, 0), total)
    parts = [
        f"{label}: {processed:,}/{total:,} reviews",
        f"elapsed: {_format_rwkv_progress_time(elapsed_seconds)}",
    ]
    if processed > 0:
        remaining = (
            0
            if processed >= total
            else elapsed_seconds * (total - processed) / processed
        )
        parts.append(f"remaining: {_format_rwkv_progress_time(remaining)}")
    return " | ".join(parts)


def _format_rwkv_progress_time(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _report_rwkv_state_cache_progress(
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
    value: int | None = None,
    maximum: int | None = None,
) -> None:
    if progress is not None:
        progress(label, value, maximum)


def _run_on_main(mw: object, callback: Callable[[], None]) -> None:
    in_main_thread = getattr(mw, "inMainThread", None)
    if callable(in_main_thread) and in_main_thread():
        callback()
        return

    taskman = getattr(mw, "taskman", None)
    run_on_main = getattr(taskman, "run_on_main", None)
    if callable(run_on_main):
        run_on_main(callback)
    else:
        callback()


def warm_up_rwkv_state(
    mw: object,
    *,
    force_rebuild: bool = False,
    require_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Warm and persist RWKV state for the current desktop collection."""

    configure_reviewer_backend_from_environment()
    if _reviewer_backend is None:
        return False

    return _warm_up_reviewer_backend(
        SimpleNamespace(mw=mw),
        force_rebuild=force_rebuild,
        require_retrievability_cache=require_retrievability_cache,
        progress=progress,
    )


def load_rwkv_state_cache(
    mw: object,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Restore a usable RWKV state cache without starting a full rebuild."""

    configure_reviewer_backend_from_environment()
    if _reviewer_backend is None:
        return False

    return _prepare_reviewer_backend_from_cache(
        SimpleNamespace(mw=mw),
        progress=progress,
    )


def rwkv_state_cache_usable(mw: object) -> bool:
    """Return true when the current collection has a usable local RWKV cache."""

    context = SimpleNamespace(mw=mw)
    metadata = _read_rwkv_state_cache_metadata(context)
    if metadata is None or not _rwkv_state_cache_metadata_usable(context, metadata):
        return False

    if metadata.get("version") != _RWKV_STATE_CACHE_VERSION:
        return True

    cache_dir = _rwkv_state_cache_dir(context)
    return (
        cache_dir is not None and (cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE).exists()
    )


def prepare_rwkv_state_cache_on_startup(mw: object) -> None:
    """Restore or prompt for RWKV state cache preparation after profile open."""

    if not _collection_has_rwkv_review_enabled(mw):
        return

    if rwkv_state_cache_usable(mw):
        load_rwkv_state_cache_with_progress(mw)
    else:
        maybe_prompt_for_rwkv_state_cache(mw)


def maybe_prompt_for_rwkv_state_cache(mw: object) -> None:
    """Prompt once per session to build the local RWKV state cache if needed."""

    global _rwkv_startup_prompt_shown
    parent = cast(QWidget | None, mw)

    if _rwkv_startup_prompt_shown:
        return
    if not _collection_has_rwkv_review_enabled(mw):
        return
    if rwkv_state_cache_usable(mw):
        return
    if not configure_reviewer_backend_from_environment():
        return

    _rwkv_startup_prompt_shown = True

    def prompt() -> None:
        from aqt.utils import askUser

        if askUser(
            "RWKV review is enabled, but the local RWKV state cache is not ready. "
            "Build it now? Anki will show progress until it finishes.",
            parent=parent,
        ):
            build_rwkv_state_cache_with_progress(mw)

    taskman = getattr(mw, "taskman", None)
    run_on_main = getattr(taskman, "run_on_main", None)
    if callable(run_on_main):
        run_on_main(prompt)
    else:
        prompt()


def load_rwkv_state_cache_with_progress(mw: object) -> None:
    """Restore the local RWKV state cache with a lightweight progress dialog."""

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        load_rwkv_state_cache(mw)
        return

    def start_load() -> None:
        parent = cast(QWidget | None, mw)
        start = time.monotonic()

        def progress(
            label: str,
            value: int | None,
            maximum: int | None,
        ) -> None:
            def update() -> None:
                progress_manager = getattr(mw, "progress", None)
                update_progress = getattr(progress_manager, "update", None)
                if callable(update_progress):
                    update_progress(label=label, value=value, max=maximum)

            _run_on_main(mw, update)

        def load() -> bool:
            return load_rwkv_state_cache(mw, progress=progress)

        def done(future: Future[bool]) -> None:
            try:
                loaded = future.result()
            except Exception:
                logger.exception("RWKV state cache startup load failed")
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            if loaded:
                logger.debug(
                    "RWKV state cache startup load finished: elapsed_ms=%.1f",
                    elapsed_ms,
                )
                prewarm_reviewer_queue_score_cache(
                    SimpleNamespace(mw=mw),
                    reason="startup cache load",
                )
            else:
                logger.debug(
                    "RWKV state cache startup load skipped: elapsed_ms=%.1f",
                    elapsed_ms,
                )

        with_progress(
            load,
            done,
            parent=parent,
            label="Loading RWKV state cache...",
            immediate=True,
            uses_collection=True,
            title="RWKV State Cache",
        )

    _run_on_main(mw, start_load)


def build_rwkv_state_cache_with_progress(
    mw: object,
    *,
    force_rebuild: bool = False,
) -> None:
    """Build the local RWKV state cache with a modal progress dialog."""

    from aqt.utils import tooltip

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        warm_up_rwkv_state(
            mw,
            force_rebuild=force_rebuild,
            require_retrievability_cache=True,
        )
        return

    def start_build() -> None:
        parent = cast(QWidget | None, mw)
        start = time.monotonic()

        def progress(
            label: str,
            value: int | None,
            maximum: int | None,
        ) -> None:
            def update() -> None:
                progress_manager = getattr(mw, "progress", None)
                update_progress = getattr(progress_manager, "update", None)
                if callable(update_progress):
                    update_progress(label=label, value=value, max=maximum)

            _run_on_main(mw, update)

        def build() -> bool:
            return warm_up_rwkv_state(
                mw,
                force_rebuild=force_rebuild,
                require_retrievability_cache=True,
                progress=progress,
            )

        def done(future: Future[bool]) -> None:
            try:
                built = future.result()
            except Exception:
                logger.exception("RWKV state cache build failed")
                tooltip("RWKV state cache build failed.", parent=parent)
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            if built:
                tooltip("RWKV state cache ready.", parent=parent)
                logger.debug(
                    "RWKV state cache build finished: elapsed_ms=%.1f",
                    elapsed_ms,
                )
                prewarm_reviewer_queue_score_cache(
                    SimpleNamespace(mw=mw),
                    reason="state cache build",
                )
            else:
                tooltip("RWKV state cache could not be built.", parent=parent)

        with_progress(
            build,
            done,
            parent=parent,
            label="Building RWKV state cache...",
            immediate=True,
            uses_collection=True,
            title="RWKV State Cache",
        )

    _run_on_main(mw, start_build)


def reschedule_rwkv_review_cards_with_progress(mw: object) -> None:
    """Reschedule RWKV-enabled review cards and persist current RWKV S90."""

    from aqt.operations import on_op_finished
    from aqt.utils import tooltip

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        result = reschedule_rwkv_review_cards(mw)
        if result.changes is not None:
            on_op_finished(cast(Any, mw), cast(Any, result.changes), None)
        return

    def start_reschedule() -> None:
        parent = cast(QWidget | None, mw)
        start = time.monotonic()

        def progress(
            label: str,
            value: int | None,
            maximum: int | None,
        ) -> None:
            def update() -> None:
                progress_manager = getattr(mw, "progress", None)
                update_progress = getattr(progress_manager, "update", None)
                if callable(update_progress):
                    update_progress(label=label, value=value, max=maximum)

            _run_on_main(mw, update)

        def reschedule() -> RwkvReviewRescheduleResult:
            return reschedule_rwkv_review_cards(mw, progress=progress)

        def done(future: Future[RwkvReviewRescheduleResult]) -> None:
            try:
                result = future.result()
            except Exception:
                logger.exception("RWKV review reschedule failed")
                tooltip("RWKV reschedule failed.", parent=parent)
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            if result.changes is not None:
                on_op_finished(cast(Any, mw), cast(Any, result.changes), None)
            if result.built:
                tooltip(
                    f"RWKV rescheduled {result.updated} cards.",
                    parent=parent,
                )
                logger.debug(
                    "RWKV review reschedule finished: predicted=%s updated=%s "
                    "elapsed_ms=%.1f",
                    result.predicted,
                    result.updated,
                    elapsed_ms,
                )
            else:
                tooltip("RWKV reschedule could not be started.", parent=parent)

        with_progress(
            reschedule,
            done,
            parent=parent,
            label="Preparing RWKV reschedule...",
            immediate=True,
            uses_collection=True,
            title="RWKV Reschedule",
        )

    _run_on_main(mw, start_reschedule)


def reschedule_rwkv_review_cards(
    mw: object,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> RwkvReviewRescheduleResult:
    """Compute current RWKV intervals/S90s and apply them to review cards."""

    _report_rwkv_state_cache_progress(
        progress,
        "Preparing RWKV state...",
    )
    if not warm_up_rwkv_state(mw, progress=progress):
        return RwkvReviewRescheduleResult(built=False, changes=None)

    reviewer = SimpleNamespace(mw=mw)
    _report_rwkv_state_cache_progress(
        progress,
        "Finding RWKV review cards...",
    )
    card_ids = _rwkv_review_reschedule_card_ids(mw)
    if not card_ids:
        return RwkvReviewRescheduleResult(built=True, changes=None)

    items = _rwkv_review_reschedule_items(
        reviewer,
        card_ids,
        progress=progress,
    )
    if not items:
        return RwkvReviewRescheduleResult(built=True, changes=None)

    _report_rwkv_state_cache_progress(
        progress,
        "Saving RWKV reschedule...",
        len(items),
        len(items),
    )
    changes = _apply_rwkv_review_reschedule(mw, items)
    updated = getattr(changes, "count", 0)
    return RwkvReviewRescheduleResult(
        built=True,
        changes=getattr(changes, "changes", None),
        predicted=len(items),
        updated=updated if isinstance(updated, int) else 0,
    )


def _restore_reviewer_backend_cache(
    reviewer: object,
    *,
    require_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    backend = _reviewer_backend
    restore_snapshot = getattr(backend, "restore_cache_snapshot", None)
    warm_up = getattr(backend, "warm_up", None)
    if not callable(restore_snapshot) or not callable(warm_up):
        return False

    stored = _read_rwkv_state_cache(reviewer)
    if stored is None:
        return False
    if require_retrievability_cache and not _rwkv_review_retrievability_cache_complete(
        reviewer,
        last_review_id=stored.history.last_review_id,
        review_count=stored.history.review_count,
    ):
        logger.debug(
            "RWKV state cache restore skipped: retrievability cache incomplete"
        )
        return False

    try:
        restore_snapshot(stored.snapshot)
        if stored.history.reviews:
            _report_rwkv_state_cache_progress(
                progress,
                "Loading RWKV cache deltas...",
            )
            _replay_rwkv_cache_reviews(
                backend,
                warm_up,
                stored.history.reviews,
                progress=progress,
                label="Loading RWKV cache deltas",
            )

        _report_rwkv_state_cache_progress(
            progress,
            "Loading new RWKV reviews...",
        )
        history = _historical_rwkv_review_inputs(
            reviewer,
            after_review_id=stored.history.last_review_id,
            previous_review_id_by_card=stored.history.previous_review_id_by_card,
            previous_interval_days_by_card=stored.history.previous_interval_days_by_card,
            review_count_by_card=stored.history.review_count_by_card,
        )
        if history.reviews:
            _warm_up_rwkv_reviews(
                reviewer,
                backend,
                warm_up,
                history.reviews,
                review_ids=history.review_ids,
                progress=progress,
                label="Updating RWKV state cache",
            )
            _report_rwkv_state_cache_progress(
                progress,
                "Saving RWKV state cache...",
            )
            if _rwkv_state_cache_uses_current_model_key(stored.metadata):
                _append_rwkv_state_cache_deltas(
                    reviewer,
                    history,
                    snapshot_review_id=_int_value(
                        stored.metadata.get("snapshotReviewId")
                    )
                    or stored.history.last_review_id,
                )
            else:
                _save_reviewer_backend_cache(reviewer, history)
        elif not _rwkv_state_cache_uses_current_model_key(stored.metadata):
            _report_rwkv_state_cache_progress(
                progress,
                "Saving RWKV state cache...",
            )
            _save_reviewer_backend_cache(reviewer, stored.history)
        logger.debug(
            "loaded RWKV state cache: cached_delta_reviews=%s "
            "incremental_reviews=%s last_review_id=%s",
            len(stored.history.reviews),
            len(history.reviews),
            history.last_review_id,
        )
        return True
    except Exception:
        logger.exception("failed to restore RWKV state cache")
        return False


def _existing_rwkv_review_retrievability_cache_complete(reviewer: object) -> bool:
    metadata = _read_rwkv_state_cache_metadata(reviewer)
    if metadata is None or not _rwkv_state_cache_metadata_usable(reviewer, metadata):
        return False

    last_review_id = _int_value(metadata.get("lastReviewId"))
    review_count = _int_value(metadata.get("reviewCount"))
    if last_review_id is None or review_count is None:
        return False

    return _rwkv_review_retrievability_cache_complete(
        reviewer,
        last_review_id=last_review_id,
        review_count=review_count,
    )


def _rwkv_review_retrievability_cache_complete(
    reviewer: object,
    *,
    last_review_id: int,
    review_count: int,
) -> bool:
    if review_count <= 0:
        return True

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    if not callable(scalar):
        return False

    try:
        cached = scalar(
            f"""
select count()
from revlog r
join {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} cache
  on cache.revlog_id = r.id
where r.ease between 1 and 4
  and r.type in (0, 1, 2, 3)
  and r.id <= ?
  and cache.prediction between 0 and 1
""",
            last_review_id,
        )
    except Exception:
        logger.debug("failed to check RWKV review retrievability cache completeness")
        return False

    return isinstance(cached, int) and cached >= review_count


def _save_reviewer_backend_cache(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
) -> None:
    backend = _reviewer_backend
    cache_snapshot = getattr(backend, "cache_snapshot", None)
    if not callable(cache_snapshot):
        return

    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return

    try:
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
        )
        snapshot = cache_snapshot()
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = _encode_rwkv_state_cache_snapshot_file(
            metadata=metadata,
            snapshot=snapshot,
            history=history,
        )
        _atomic_write(cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE, data)
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE, _rwkv_empty_deltas_log()
        )
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_META_FILE,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf8"),
        )
        logger.debug(
            "saved RWKV state cache snapshot: reviews=%s last_review_id=%s bytes=%s",
            len(history.reviews),
            history.last_review_id,
            len(data),
        )
    except Exception:
        logger.exception("failed to save RWKV state cache")


def _append_rwkv_state_cache_deltas(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    snapshot_review_id: int,
) -> None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        delta_path = cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE
        _append_rwkv_delta_records(delta_path, history.review_ids, history.reviews)
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=snapshot_review_id,
        )
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_META_FILE,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf8"),
        )
        logger.debug(
            "appended RWKV state cache deltas: reviews=%s last_review_id=%s",
            len(history.reviews),
            history.last_review_id,
        )
    except Exception:
        logger.exception("failed to append RWKV state cache deltas")


def _read_rwkv_state_cache(reviewer: object) -> RwkvStoredStateCache | None:
    stored = _read_rwkv_state_cache_binary(reviewer)
    if stored is not None:
        return stored

    return _read_rwkv_state_cache_legacy_json(reviewer)


def _read_rwkv_state_cache_binary(reviewer: object) -> RwkvStoredStateCache | None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return None

    metadata = _read_rwkv_state_cache_metadata(reviewer)
    if (
        not isinstance(metadata, dict)
        or metadata.get("version") != _RWKV_STATE_CACHE_VERSION
    ):
        return None
    if not _rwkv_state_cache_metadata_usable(reviewer, metadata):
        return None

    snapshot_path = cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE
    if not snapshot_path.exists():
        return None

    try:
        snapshot_metadata, snapshot, snapshot_history = (
            _decode_rwkv_state_cache_snapshot_file(snapshot_path.read_bytes())
        )
        if not _rwkv_state_cache_metadata_matches_manifest(
            snapshot_metadata,
            metadata,
        ):
            return None

        delta_reviews = _read_rwkv_delta_records(
            cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE,
            after_review_id=snapshot_history.last_review_id,
            until_review_id=_int_value(metadata.get("lastReviewId")) or 0,
        )
        history = _rwkv_history_after_delta_reviews(snapshot_history, delta_reviews)
        if history.last_review_id != (_int_value(metadata.get("lastReviewId")) or 0):
            return None
        if history.review_count != (_int_value(metadata.get("reviewCount")) or 0):
            return None

        return RwkvStoredStateCache(
            metadata=metadata,
            snapshot=snapshot,
            history=history,
        )
    except Exception:
        logger.exception("failed to read binary RWKV state cache")
        return None


def _read_rwkv_state_cache_legacy_json(
    reviewer: object,
) -> RwkvStoredStateCache | None:
    payload = _read_rwkv_state_cache_payload(reviewer)
    if payload is None:
        return None

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not _rwkv_state_cache_metadata_usable(
        reviewer,
        metadata,
    ):
        return None

    try:
        return RwkvStoredStateCache(
            metadata={
                **metadata,
                "snapshotReviewId": metadata.get("lastReviewId"),
            },
            snapshot=_decode_rwkv_cache_snapshot(payload),
            history=RwkvHistoricalReviewInputs(
                reviews=[],
                review_ids=[],
                previous_review_id_by_card=_decode_int_map(
                    payload.get("previousReviewIdByCard")
                ),
                previous_interval_days_by_card=_decode_int_map(
                    payload.get("previousIntervalDaysByCard")
                ),
                review_count_by_card=_decode_int_map(payload.get("reviewCountByCard")),
                last_review_id=_int_value(metadata.get("lastReviewId")) or 0,
                review_count=_int_value(metadata.get("reviewCount")) or 0,
            ),
        )
    except Exception:
        logger.exception("failed to read legacy RWKV state cache")
        return None


def _read_rwkv_state_cache_payload(reviewer: object) -> dict[str, object] | None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return None

    path = cache_dir / _RWKV_STATE_CACHE_DATA_FILE
    try:
        return json.loads(gzip.decompress(path.read_bytes()).decode("utf8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("failed to read RWKV state cache")
        return None


def _read_rwkv_state_cache_metadata(reviewer: object) -> dict[str, object] | None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return None

    path = cache_dir / _RWKV_STATE_CACHE_META_FILE
    try:
        value = json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("failed to read RWKV state cache metadata")
        return None

    return value if isinstance(value, dict) else None


def _rwkv_state_cache_metadata(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    snapshot_review_id: int,
) -> dict[str, object]:
    return {
        "version": _RWKV_STATE_CACHE_VERSION,
        "collection": _rwkv_collection_cache_key(reviewer),
        "model": _rwkv_model_cache_key(),
        "dynamicPresetReplay": _rwkv_dynamic_preset_replay_enabled_for_collection(
            reviewer
        ),
        "snapshotReviewId": snapshot_review_id,
        "lastReviewId": history.last_review_id,
        "reviewCount": history.review_count,
    }


def _rwkv_state_cache_metadata_usable(
    reviewer: object,
    metadata: dict[str, object],
) -> bool:
    if metadata.get("version") not in (
        _RWKV_STATE_CACHE_VERSION,
        _RWKV_STATE_CACHE_LEGACY_JSON_VERSION,
    ):
        return False
    if metadata.get("collection") != _rwkv_collection_cache_key(reviewer):
        return False
    if not _rwkv_state_cache_model_usable(metadata.get("model")):
        return False
    if metadata.get("version") == _RWKV_STATE_CACHE_VERSION and metadata.get(
        "dynamicPresetReplay"
    ) != _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer):
        return False

    last_review_id = _int_value(metadata.get("lastReviewId"))
    review_count = _int_value(metadata.get("reviewCount"))
    if last_review_id is None or review_count is None:
        return False

    return (
        _historical_rwkv_review_count_through(reviewer, last_review_id) == review_count
    )


def _rwkv_state_cache_dir(reviewer: object) -> Path | None:
    mw = getattr(reviewer, "mw", None)
    pm = getattr(mw, "pm", None)
    profile_folder = getattr(pm, "profileFolder", None)
    if not callable(profile_folder):
        return None

    return Path(profile_folder()) / _RWKV_STATE_CACHE_DIR


def _rwkv_collection_cache_key(reviewer: object) -> dict[str, object]:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    collection_created = scalar("select crt from col") if callable(scalar) else None
    collection_path = getattr(col, "path", "")
    return {
        "created": collection_created if isinstance(collection_created, int) else None,
        "path": hashlib.sha256(str(collection_path).encode("utf8")).hexdigest(),
    }


def _rwkv_model_cache_key() -> dict[str, object] | None:
    model_path = _current_embedded_rwkv_model_path()
    if model_path is None:
        return None

    try:
        stat = model_path.stat()
        digest = _sha256_file(model_path)
    except OSError:
        return None

    return {
        "source": "custom" if os.environ.get("ANKI_RWKV_MODEL_PATH") else "embedded",
        "name": model_path.name,
        "size": stat.st_size,
        "sha256": digest,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_RWKV_MODEL_KEY_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _rwkv_state_cache_uses_current_model_key(metadata: dict[str, object]) -> bool:
    return (
        metadata.get("version") == _RWKV_STATE_CACHE_VERSION
        and metadata.get("model") == _rwkv_model_cache_key()
    )


def _rwkv_state_cache_model_usable(stored_model: object) -> bool:
    current_model = _rwkv_model_cache_key()
    return stored_model == current_model or _rwkv_legacy_embedded_model_key_matches(
        stored_model,
        current_model,
    )


def _rwkv_legacy_embedded_model_key_matches(
    stored_model: object,
    current_model: object,
) -> bool:
    if not isinstance(stored_model, dict) or not isinstance(current_model, dict):
        return False
    if current_model.get("source") != "embedded":
        return False

    stored_path = stored_model.get("path")
    if not isinstance(stored_path, str):
        return False
    if Path(stored_path).name != _EMBEDDED_RWKV_MODEL_FILENAME:
        return False
    if current_model.get("name") != _EMBEDDED_RWKV_MODEL_FILENAME:
        return False
    if _int_value(stored_model.get("mtimeNs")) is None:
        return False

    stored_size = _int_value(stored_model.get("size"))
    current_size = _int_value(current_model.get("size"))
    return stored_size is not None and stored_size == current_size


def _current_embedded_rwkv_model_path() -> Path | None:
    if os.environ.get("ANKI_RWKV_BENCHMARK_PATH"):
        return None

    model_path = os.environ.get("ANKI_RWKV_MODEL_PATH")
    return Path(model_path) if model_path else embedded_rwkv_model_path()


def _encode_rwkv_cache_snapshot(
    snapshot: RwkvBackendCacheSnapshot,
) -> dict[str, object]:
    return {
        "cardStates": _encode_state_map(snapshot.card_states),
        "noteStates": _encode_state_map(snapshot.note_states),
        "deckStates": _encode_state_map(snapshot.deck_states),
        "presetStates": _encode_state_map(snapshot.preset_states),
        "globalState": _encode_bytes(snapshot.global_state),
        "runtimeState": _encode_bytes(snapshot.runtime_state),
    }


def _decode_rwkv_cache_snapshot(
    payload: dict[str, object],
) -> RwkvBackendCacheSnapshot:
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("missing RWKV cache snapshot")

    return RwkvBackendCacheSnapshot(
        card_states=_decode_state_map(snapshot.get("cardStates")),
        note_states=_decode_state_map(snapshot.get("noteStates")),
        deck_states=_decode_state_map(snapshot.get("deckStates")),
        preset_states=_decode_state_map(snapshot.get("presetStates")),
        global_state=_decode_optional_bytes(snapshot.get("globalState")),
        runtime_state=_decode_optional_bytes(snapshot.get("runtimeState")),
    )


def _encode_state_map(states: dict[int, bytes]) -> dict[str, str]:
    return {str(key): _encode_bytes(value) or "" for key, value in states.items()}


def _decode_state_map(value: object) -> dict[int, bytes]:
    if not isinstance(value, dict):
        return {}

    states: dict[int, bytes] = {}
    for key, state in value.items():
        if isinstance(key, str) and isinstance(state, str):
            states[int(key)] = base64.b64decode(state.encode("ascii"))
    return states


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _decode_optional_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid RWKV cache byte value")
    return base64.b64decode(value.encode("ascii"))


def _decode_int_map(value: object) -> dict[int, int]:
    if not isinstance(value, dict):
        return {}
    return {
        int(key): int(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, int)
    }


def _int_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        file.write(data)
        temporary_path = Path(file.name)
    os.replace(temporary_path, path)


class _RwkvBinaryReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def remaining(self) -> int:
        return len(self._data) - self._offset

    def bytes(self, size: int) -> bytes:
        if size < 0 or self._offset + size > len(self._data):
            raise ValueError("truncated RWKV cache binary data")
        value = self._data[self._offset : self._offset + size]
        self._offset += size
        return value

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.bytes(8))[0]

    def expect_end(self) -> None:
        if self.remaining():
            raise ValueError("trailing RWKV cache binary data")


def _write_u8(out: bytearray, value: int) -> None:
    out.append(value & 0xFF)


def _write_u32(out: bytearray, value: int) -> None:
    out.extend(struct.pack("<I", value))


def _write_i64(out: bytearray, value: int) -> None:
    out.extend(struct.pack("<q", value))


def _write_bytes(out: bytearray, value: bytes) -> None:
    _write_u32(out, len(value))
    out.extend(value)


def _read_bytes(reader: _RwkvBinaryReader) -> bytes:
    return reader.bytes(reader.u32())


def _write_optional_bytes(out: bytearray, value: bytes | None) -> None:
    if value is None:
        _write_u8(out, 0)
    else:
        _write_u8(out, 1)
        _write_bytes(out, value)


def _read_optional_bytes(reader: _RwkvBinaryReader) -> bytes | None:
    marker = reader.u8()
    if marker == 0:
        return None
    if marker != 1:
        raise ValueError("invalid optional bytes marker")
    return _read_bytes(reader)


def _write_optional_i64(out: bytearray, value: int | None) -> None:
    if value is None:
        _write_u8(out, 0)
    else:
        _write_u8(out, 1)
        _write_i64(out, value)


def _read_optional_i64(reader: _RwkvBinaryReader) -> int | None:
    marker = reader.u8()
    if marker == 0:
        return None
    if marker != 1:
        raise ValueError("invalid optional integer marker")
    return reader.i64()


def _write_optional_string(out: bytearray, value: str | None) -> None:
    if value is None:
        _write_u8(out, 0)
    else:
        _write_u8(out, 1)
        _write_bytes(out, value.encode("utf8"))


def _read_optional_string(reader: _RwkvBinaryReader) -> str | None:
    marker = reader.u8()
    if marker == 0:
        return None
    if marker != 1:
        raise ValueError("invalid optional string marker")
    return _read_bytes(reader).decode("utf8")


def _write_json(out: bytearray, value: dict[str, object]) -> None:
    _write_bytes(
        out,
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf8"),
    )


def _read_json(reader: _RwkvBinaryReader) -> dict[str, object]:
    value = json.loads(_read_bytes(reader).decode("utf8"))
    if not isinstance(value, dict):
        raise ValueError("invalid RWKV cache JSON payload")
    return value


def _write_state_map(out: bytearray, states: dict[int, bytes]) -> None:
    _write_u32(out, len(states))
    for key, state in sorted(states.items()):
        _write_i64(out, key)
        _write_bytes(out, state)


def _read_state_map(reader: _RwkvBinaryReader) -> dict[int, bytes]:
    states: dict[int, bytes] = {}
    for _ in range(reader.u32()):
        key = reader.i64()
        states[key] = _read_bytes(reader)
    return states


def _write_int_map(out: bytearray, values: dict[int, int]) -> None:
    _write_u32(out, len(values))
    for key, value in sorted(values.items()):
        _write_i64(out, key)
        _write_i64(out, value)


def _read_int_map_binary(reader: _RwkvBinaryReader) -> dict[int, int]:
    values: dict[int, int] = {}
    for _ in range(reader.u32()):
        key = reader.i64()
        values[key] = reader.i64()
    return values


def _write_cache_snapshot_binary(
    out: bytearray,
    snapshot: RwkvBackendCacheSnapshot,
) -> None:
    _write_state_map(out, snapshot.card_states)
    _write_state_map(out, snapshot.note_states)
    _write_state_map(out, snapshot.deck_states)
    _write_state_map(out, snapshot.preset_states)
    _write_optional_bytes(out, snapshot.global_state)
    _write_optional_bytes(out, snapshot.runtime_state)


def _read_cache_snapshot_binary(
    reader: _RwkvBinaryReader,
) -> RwkvBackendCacheSnapshot:
    return RwkvBackendCacheSnapshot(
        card_states=_read_state_map(reader),
        note_states=_read_state_map(reader),
        deck_states=_read_state_map(reader),
        preset_states=_read_state_map(reader),
        global_state=_read_optional_bytes(reader),
        runtime_state=_read_optional_bytes(reader),
    )


def _encode_rwkv_state_cache_snapshot_file(
    *,
    metadata: dict[str, object],
    snapshot: RwkvBackendCacheSnapshot,
    history: RwkvHistoricalReviewInputs,
) -> bytes:
    out = bytearray(_RWKV_STATE_CACHE_SNAPSHOT_MAGIC)
    _write_json(out, metadata)
    _write_cache_snapshot_binary(out, snapshot)
    _write_int_map(out, history.previous_review_id_by_card)
    _write_int_map(out, history.previous_interval_days_by_card)
    _write_int_map(out, history.review_count_by_card)
    return bytes(out)


def _decode_rwkv_state_cache_snapshot_file(
    data: bytes,
) -> tuple[dict[str, object], RwkvBackendCacheSnapshot, RwkvHistoricalReviewInputs]:
    reader = _RwkvBinaryReader(data)
    if (
        reader.bytes(len(_RWKV_STATE_CACHE_SNAPSHOT_MAGIC))
        != _RWKV_STATE_CACHE_SNAPSHOT_MAGIC
    ):
        raise ValueError("invalid RWKV state cache snapshot header")
    metadata = _read_json(reader)
    snapshot = _read_cache_snapshot_binary(reader)
    previous_ids = _read_int_map_binary(reader)
    previous_intervals = _read_int_map_binary(reader)
    review_counts = _read_int_map_binary(reader)
    reader.expect_end()
    history = RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=_int_value(metadata.get("lastReviewId")) or 0,
        review_count=_int_value(metadata.get("reviewCount")) or 0,
    )
    return metadata, snapshot, history


def _rwkv_empty_deltas_log() -> bytes:
    return _RWKV_STATE_CACHE_DELTAS_MAGIC


def _write_review_input(out: bytearray, review_input: RwkvReviewInput) -> None:
    identity = review_input.identity
    _write_i64(out, identity.card_id)
    _write_optional_i64(out, identity.note_id)
    _write_optional_i64(out, identity.deck_id)
    _write_optional_i64(out, identity.preset_id)
    _write_u8(out, 1 if review_input.is_query else 0)
    _write_optional_i64(out, review_input.ease)
    _write_optional_i64(out, review_input.duration_millis)
    _write_optional_i64(out, review_input.card_type)
    _write_optional_i64(out, review_input.card_queue)
    _write_optional_i64(out, review_input.card_due)
    _write_optional_i64(out, review_input.interval_days)
    _write_optional_i64(out, review_input.ease_factor)
    _write_optional_i64(out, review_input.reps)
    _write_optional_i64(out, review_input.lapses)
    _write_optional_i64(out, review_input.day_offset)
    _write_optional_string(out, review_input.current_state_kind)
    _write_optional_string(out, review_input.current_normal_state_kind)
    _write_optional_i64(out, review_input.current_elapsed_days)
    _write_optional_i64(out, review_input.current_elapsed_seconds)


def _read_review_input(reader: _RwkvBinaryReader) -> RwkvReviewInput:
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=reader.i64(),
            note_id=_read_optional_i64(reader),
            deck_id=_read_optional_i64(reader),
            preset_id=_read_optional_i64(reader),
        ),
        is_query=bool(reader.u8()),
        ease=_read_optional_i64(reader),
        duration_millis=_read_optional_i64(reader),
        card_type=_read_optional_i64(reader),
        card_queue=_read_optional_i64(reader),
        card_due=_read_optional_i64(reader),
        interval_days=_read_optional_i64(reader),
        ease_factor=_read_optional_i64(reader),
        reps=_read_optional_i64(reader),
        lapses=_read_optional_i64(reader),
        day_offset=_read_optional_i64(reader),
        current_state_kind=_read_optional_string(reader),
        current_normal_state_kind=_read_optional_string(reader),
        current_elapsed_days=_read_optional_i64(reader),
        current_elapsed_seconds=_read_optional_i64(reader),
    )


def _encode_rwkv_delta_record(review_id: int, review_input: RwkvReviewInput) -> bytes:
    out = bytearray()
    _write_i64(out, review_id)
    _write_review_input(out, review_input)
    return bytes(out)


def _decode_rwkv_delta_record(data: bytes) -> tuple[int, RwkvReviewInput]:
    reader = _RwkvBinaryReader(data)
    review_id = reader.i64()
    review_input = _read_review_input(reader)
    reader.expect_end()
    return review_id, review_input


def _append_rwkv_delta_records(
    path: Path,
    review_ids: Sequence[int],
    reviews: Sequence[RwkvReviewInput],
) -> None:
    if len(review_ids) != len(reviews):
        raise ValueError("RWKV delta review id count mismatch")

    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("ab") as file:
        if needs_header:
            file.write(_RWKV_STATE_CACHE_DELTAS_MAGIC)
        for review_id, review in zip(review_ids, reviews):
            payload = _encode_rwkv_delta_record(review_id, review)
            file.write(struct.pack("<I", len(payload)))
            file.write(payload)
            file.write(struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF))
        file.flush()
        os.fsync(file.fileno())


def _read_rwkv_delta_records(
    path: Path,
    *,
    after_review_id: int,
    until_review_id: int,
) -> list[tuple[int, RwkvReviewInput]]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []

    reader = _RwkvBinaryReader(data)
    if (
        reader.bytes(len(_RWKV_STATE_CACHE_DELTAS_MAGIC))
        != _RWKV_STATE_CACHE_DELTAS_MAGIC
    ):
        raise ValueError("invalid RWKV state cache delta header")

    records: list[tuple[int, RwkvReviewInput]] = []
    while reader.remaining():
        if reader.remaining() < 8:
            break
        length = reader.u32()
        if reader.remaining() < length + 4:
            break
        payload = reader.bytes(length)
        checksum = reader.u32()
        if checksum != (zlib.crc32(payload) & 0xFFFFFFFF):
            raise ValueError("invalid RWKV state cache delta checksum")
        review_id, review_input = _decode_rwkv_delta_record(payload)
        if after_review_id < review_id <= until_review_id:
            records.append((review_id, review_input))
    return records


def _rwkv_history_after_delta_reviews(
    base: RwkvHistoricalReviewInputs,
    delta_reviews: Sequence[tuple[int, RwkvReviewInput]],
) -> RwkvHistoricalReviewInputs:
    previous_ids = dict(base.previous_review_id_by_card)
    previous_intervals = dict(base.previous_interval_days_by_card)
    review_counts = dict(base.review_count_by_card)
    reviews: list[RwkvReviewInput] = []
    review_ids: list[int] = []
    last_review_id = base.last_review_id
    review_count = base.review_count

    for review_id, review in delta_reviews:
        if review_id <= last_review_id:
            continue
        reviews.append(review)
        review_ids.append(review_id)
        card_id = review.identity.card_id
        previous_ids[card_id] = review_id
        if review.interval_days is not None:
            previous_intervals[card_id] = review.interval_days
        review_counts[card_id] = review_counts.get(card_id, 0) + 1
        last_review_id = max(last_review_id, review_id)
        review_count += 1

    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=last_review_id,
        review_count=review_count,
    )


def _rwkv_state_cache_metadata_matches_manifest(
    snapshot_metadata: dict[str, object],
    manifest_metadata: dict[str, object],
) -> bool:
    snapshot_review_id = _int_value(manifest_metadata.get("snapshotReviewId"))
    return (
        snapshot_metadata.get("version") == _RWKV_STATE_CACHE_VERSION
        and snapshot_metadata.get("collection") == manifest_metadata.get("collection")
        and snapshot_metadata.get("model") == manifest_metadata.get("model")
        and snapshot_metadata.get("dynamicPresetReplay")
        == manifest_metadata.get("dynamicPresetReplay")
        and _int_value(snapshot_metadata.get("lastReviewId")) == snapshot_review_id
    )


def _replay_rwkv_cache_reviews(
    backend: object,
    warm_up: object,
    reviews: Sequence[RwkvReviewInput],
    *,
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
) -> None:
    if isinstance(backend, RwkvStatefulReviewerBackend):
        started_at = time.monotonic()
        backend.warm_up(
            reviews,
            progress=lambda replay_progress: _report_rwkv_review_replay_progress(
                progress,
                label=label,
                replay_progress=replay_progress,
                elapsed_seconds=time.monotonic() - started_at,
            ),
        )
        return

    if callable(warm_up):
        warm_up(reviews)


def _historical_rwkv_review_inputs(
    reviewer: object,
    *,
    after_review_id: int | None = None,
    previous_review_id_by_card: dict[int, int] | None = None,
    previous_interval_days_by_card: dict[int, int] | None = None,
    review_count_by_card: dict[int, int] | None = None,
) -> RwkvHistoricalReviewInputs:
    start = time.monotonic()
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not isinstance(days_elapsed, int) or not isinstance(next_day_at, int):
        return RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            previous_review_id_by_card=dict(previous_review_id_by_card or {}),
            previous_interval_days_by_card=dict(previous_interval_days_by_card or {}),
            review_count_by_card=dict(review_count_by_card or {}),
            last_review_id=after_review_id or 0,
            review_count=0,
        )

    rows_start = time.monotonic()
    rows = _historical_rwkv_review_rows(reviewer, after_review_id=after_review_id)
    rows_elapsed_ms = (time.monotonic() - rows_start) * 1000
    previous_ids = dict(previous_review_id_by_card or {})
    previous_intervals = dict(previous_interval_days_by_card or {})
    review_counts = dict(review_count_by_card or {})
    dynamic_preset_replay = _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
    historical_preset_rules_start = time.monotonic()
    historical_preset_rules = (
        _historical_preset_rules(reviewer) if dynamic_preset_replay else []
    )
    historical_preset_rules_elapsed_ms = (
        time.monotonic() - historical_preset_rules_start
    ) * 1000
    preset_start = time.monotonic()
    preset_id_by_card: dict[int, int | None] = (
        _preset_ids_for_card_ids(
            reviewer,
            _historical_rwkv_review_card_ids(rows),
        )
        if dynamic_preset_replay
        else _historical_deck_config_ids_by_card(reviewer, rows)
    )
    preset_elapsed_ms = (time.monotonic() - preset_start) * 1000
    reviews: list[RwkvReviewInput] = []
    review_ids: list[int] = []
    last_review_id = after_review_id or 0
    historical_preset_rule_matches = 0

    for row in rows:
        (
            review_id,
            card_id,
            note_id,
            deck_id,
            ease,
            duration_millis,
            review_kind,
            interval_days,
            ease_factor,
        ) = row
        if not (
            isinstance(review_id, int)
            and isinstance(card_id, int)
            and isinstance(note_id, int)
            and isinstance(deck_id, int)
            and isinstance(ease, int)
            and isinstance(duration_millis, int)
            and isinstance(review_kind, int)
            and isinstance(interval_days, int)
            and isinstance(ease_factor, int)
        ):
            continue

        previous_review_id = previous_ids.get(card_id)
        elapsed_seconds = (
            max(0, (review_id - previous_review_id) // 1000)
            if previous_review_id is not None
            else -1
        )
        elapsed_days = elapsed_seconds // 86_400 if elapsed_seconds >= 0 else -1
        review_count_so_far = review_counts.get(card_id, 0)
        historical_interval_days = previous_intervals.get(card_id, 0)
        historical_preset_id = _historical_preset_id_for_review(
            historical_preset_rules,
            card_id=card_id,
            interval_days=historical_interval_days,
            review_count=review_count_so_far,
        )
        if historical_preset_id is not None:
            preset_id = historical_preset_id
            historical_preset_rule_matches += 1
        else:
            preset_id = preset_id_by_card[card_id]
        previous_ids[card_id] = review_id
        previous_intervals[card_id] = interval_days
        review_counts[card_id] = review_count_so_far + 1
        last_review_id = max(last_review_id, review_id)

        state_kind, normal_state_kind = _historical_review_state_kinds(review_kind)
        review_ids.append(review_id)
        reviews.append(
            RwkvReviewInput(
                identity=RwkvReviewIdentity(
                    card_id=card_id,
                    note_id=note_id,
                    deck_id=deck_id,
                    preset_id=preset_id,
                ),
                is_query=False,
                ease=ease,
                duration_millis=duration_millis,
                card_type=_historical_review_card_type(review_kind),
                card_queue=_historical_review_queue(review_kind),
                card_due=None,
                interval_days=interval_days,
                ease_factor=ease_factor,
                reps=None,
                lapses=None,
                day_offset=_historical_review_day_offset(
                    review_id,
                    days_elapsed=days_elapsed,
                    next_day_at=next_day_at,
                ),
                current_state_kind=state_kind,
                current_normal_state_kind=normal_state_kind,
                current_elapsed_days=elapsed_days,
                current_elapsed_seconds=elapsed_seconds,
            )
        )

    count_start = time.monotonic()
    review_count = _historical_rwkv_review_count_through(reviewer, last_review_id)
    count_elapsed_ms = (time.monotonic() - count_start) * 1000
    logger.debug(
        "RWKV historical review inputs built: rows=%s reviews=%s "
        "dynamic_preset_replay=%s historical_preset_rules=%s "
        "historical_preset_rule_matches=%s "
        "rows_elapsed_ms=%.1f historical_preset_rules_elapsed_ms=%.1f "
        "preset_elapsed_ms=%.1f count_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(rows),
        len(reviews),
        dynamic_preset_replay,
        len(historical_preset_rules),
        historical_preset_rule_matches,
        rows_elapsed_ms,
        historical_preset_rules_elapsed_ms,
        preset_elapsed_ms,
        count_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=last_review_id,
        review_count=review_count,
    )


def _historical_rwkv_review_card_ids(rows: Sequence[Sequence[object]]) -> list[int]:
    card_ids: list[int] = []
    seen: set[int] = set()
    for row in rows:
        if len(row) < 2:
            continue
        card_id = row[1]
        if isinstance(card_id, int) and card_id not in seen:
            seen.add(card_id)
            card_ids.append(card_id)
    return card_ids


def _historical_deck_config_ids_by_card(
    reviewer: object,
    rows: Sequence[Sequence[object]],
) -> dict[int, int | None]:
    preset_ids: dict[int, int | None] = {}
    deck_config_ids: dict[int, int | None] = {}
    for row in rows:
        if len(row) < 4:
            continue
        card_id = row[1]
        deck_id = row[3]
        if not isinstance(card_id, int) or not isinstance(deck_id, int):
            continue
        if deck_id not in deck_config_ids:
            deck_config = _deck_config_for_deck_id(reviewer, deck_id)
            config_id = deck_config.get("id") if isinstance(deck_config, dict) else None
            deck_config_ids[deck_id] = config_id if isinstance(config_id, int) else None
        preset_ids.setdefault(card_id, deck_config_ids[deck_id])
    return preset_ids


def _historical_preset_rules(reviewer: object) -> list[RwkvHistoricalPresetRule]:
    overlay = _fsrs_preset_overlay_config(reviewer)
    if overlay is None:
        return []

    simulator_rules = overlay.get("simulator_rules")
    if not isinstance(simulator_rules, list):
        return []

    rules: list[RwkvHistoricalPresetRule] = []
    for raw_rule in simulator_rules:
        if not isinstance(raw_rule, dict):
            continue
        preset_id = raw_rule.get("preset_id")
        if not isinstance(preset_id, str) or not preset_id:
            continue

        search = raw_rule.get("search")
        search_text = (
            search.strip() if isinstance(search, str) and search.strip() else None
        )
        card_ids = _historical_preset_rule_card_ids(reviewer, search_text)
        if search_text is not None and card_ids is None:
            continue

        min_reps = _int_value(raw_rule.get("min_reps"))
        max_reps = _int_value(raw_rule.get("max_reps"))
        min_interval_days = _float_value(raw_rule.get("min_interval_days"))
        max_interval_days = _float_value(raw_rule.get("max_interval_days"))
        if (
            min_reps is None
            and max_reps is None
            and min_interval_days is None
            and max_interval_days is None
        ):
            continue

        rules.append(
            RwkvHistoricalPresetRule(
                preset_id=_stable_preset_id(preset_id),
                search=search_text,
                card_ids=card_ids,
                min_reps=min_reps,
                max_reps=max_reps,
                min_interval_days=min_interval_days,
                max_interval_days=max_interval_days,
            )
        )

    return rules


def _fsrs_preset_overlay_config(reviewer: object) -> dict[str, object] | None:
    col = _collection(reviewer)
    get_config = getattr(col, "get_config", None)
    if not callable(get_config):
        return None

    try:
        overlay = get_config(_FSRS_PRESET_OVERLAY_CONFIG_KEY)
    except Exception:
        logger.debug("failed to read FSRS preset overlay for RWKV historical replay")
        return None

    return overlay if isinstance(overlay, dict) else None


def _historical_preset_rule_card_ids(
    reviewer: object,
    search: str | None,
) -> frozenset[int] | None:
    if search is None:
        return None

    col = _collection(reviewer)
    find_cards = getattr(col, "find_cards", None)
    if not callable(find_cards):
        return None

    try:
        card_ids = find_cards(search, order=False)
    except Exception:
        logger.debug(
            "failed to evaluate RWKV historical preset rule search: search=%r",
            search,
        )
        return None

    return frozenset(card_id for card_id in card_ids if isinstance(card_id, int))


def _historical_preset_id_for_review(
    rules: Sequence[RwkvHistoricalPresetRule],
    *,
    card_id: int,
    interval_days: int,
    review_count: int,
) -> int | None:
    for rule in rules:
        if rule.card_ids is not None and card_id not in rule.card_ids:
            continue
        if rule.min_reps is not None and review_count < rule.min_reps:
            continue
        if rule.max_reps is not None and review_count > rule.max_reps:
            continue
        if (
            rule.min_interval_days is not None
            and interval_days < rule.min_interval_days
        ):
            continue
        if (
            rule.max_interval_days is not None
            and interval_days > rule.max_interval_days
        ):
            continue

        return rule.preset_id

    return None


def _historical_rwkv_review_rows(
    reviewer: object,
    *,
    after_review_id: int | None = None,
) -> list[Sequence[object]]:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return []

    after_clause = "and r.id > ?" if after_review_id is not None else ""
    sql = f"""
select
  r.id,
  r.cid,
  c.nid,
  c.did,
  r.ease,
  r.time,
  r.type,
  cast(r.ivl as integer),
  cast(r.factor as integer)
from revlog r
join cards c on c.id = r.cid
where r.ease between 1 and 4
  and r.type in (0, 1, 2, 3)
  {after_clause}
order by r.id, r.cid
"""
    start = time.monotonic()
    logger.debug(
        "RWKV historical review rows query started: after_review_id=%s",
        after_review_id,
    )
    if after_review_id is not None:
        rows = all_rows(sql, after_review_id)
    else:
        rows = all_rows(sql)

    logger.debug(
        "RWKV historical review rows query finished: rows=%s elapsed_ms=%.1f",
        len(rows),
        (time.monotonic() - start) * 1000,
    )
    return rows


def _historical_rwkv_review_count_through(
    reviewer: object,
    last_review_id: int,
) -> int:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    if not callable(scalar):
        return 0

    value = scalar(
        """
select count()
from revlog
where ease between 1 and 4
  and type in (0, 1, 2, 3)
  and id <= ?
""",
        last_review_id,
    )
    return value if isinstance(value, int) else 0


def _historical_review_day_offset(
    review_id: int,
    *,
    days_elapsed: int,
    next_day_at: int,
) -> int:
    review_secs = review_id // 1000
    days_before_today = max(0, next_day_at - 1 - review_secs) // 86_400
    return max(0, days_elapsed - days_before_today)


def _historical_review_card_type(review_kind: int) -> int:
    if review_kind == 0:
        return int(CARD_TYPE_LRN)
    if review_kind == 2:
        return int(CARD_TYPE_RELEARNING)
    return int(CARD_TYPE_REV)


def _historical_review_queue(review_kind: int) -> int:
    if review_kind == 0:
        return int(QUEUE_TYPE_LRN)
    if review_kind == 2:
        return int(QUEUE_TYPE_DAY_LEARN_RELEARN)
    return int(QUEUE_TYPE_REV)


def _historical_review_state_kinds(review_kind: int) -> tuple[str | None, str | None]:
    if review_kind == 0:
        return "normal", "learning"
    if review_kind == 2:
        return "normal", "relearning"
    if review_kind == 3:
        return "filtered", None
    return "normal", "review"


def _rwkv_review_config_enabled(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_enabled")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config, "rwkvReviewEnabled", "rwkv_review_enabled"
    )
    if isinstance(value, bool):
        return value

    return False


def _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer: object) -> bool:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if callable(all_config):
        try:
            configs = all_config()
        except Exception:
            logger.debug("failed to read deck configs for RWKV preset replay mode")
        else:
            return any(
                isinstance(config, dict)
                and _rwkv_review_config_enabled(config)
                and _rwkv_review_dynamic_preset_replay(config)
                for config in configs
            )

    return False


def _rwkv_review_dynamic_preset_replay(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_dynamic_preset_replay")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewDynamicPresetReplay",
        "rwkv_review_dynamic_preset_replay",
    )
    return value if isinstance(value, bool) else False


def _review_order_uses_retrievability(deck_config: dict[str, object]) -> bool:
    value = deck_config.get("reviewOrder", deck_config.get("review_order"))
    return value in (
        _REVIEW_ORDER_RETRIEVABILITY_ASCENDING,
        _REVIEW_ORDER_RETRIEVABILITY_DESCENDING,
    )


def _rwkv_review_batch_size(deck_config: dict[str, object]) -> int:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_batch_size")
        if _valid_rwkv_review_batch_size(value):
            return cast(int, value)

    value = _rwkv_config_direct_value(
        deck_config, "rwkvReviewBatchSize", "rwkv_review_batch_size"
    )
    if _valid_rwkv_review_batch_size(value):
        return cast(int, value)

    return _DEFAULT_RWKV_REVIEW_BATCH_SIZE


def _rwkv_review_refresh_interval(deck_config: dict[str, object]) -> int:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_refresh_interval")
        if _valid_rwkv_review_refresh_interval(value):
            return cast(int, value)

    value = _rwkv_config_direct_value(
        deck_config, "rwkvReviewRefreshInterval", "rwkv_review_refresh_interval"
    )
    if _valid_rwkv_review_refresh_interval(value):
        return cast(int, value)

    return _DEFAULT_RWKV_REVIEW_REFRESH_INTERVAL


def _rwkv_review_refresh_on_exit(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_refresh_on_exit")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config, "rwkvReviewRefreshOnExit", "rwkv_review_refresh_on_exit"
    )
    return value if isinstance(value, bool) else False


def _rwkv_review_instant_order_enabled(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_instant_order_enabled")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewInstantOrderEnabled",
        "rwkv_review_instant_order_enabled",
    )
    return value if isinstance(value, bool) else False


def _rwkv_config_direct_value(
    deck_config: dict[str, object],
    camel_key: str,
    snake_key: str,
) -> object | None:
    return deck_config.get(camel_key, deck_config.get(snake_key))


def _valid_rwkv_review_batch_size(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _MIN_RWKV_REVIEW_BATCH_SIZE <= value <= _MAX_RWKV_REVIEW_BATCH_SIZE
    )


def _valid_rwkv_review_refresh_interval(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _MIN_RWKV_REVIEW_REFRESH_INTERVAL
        <= value
        <= _MAX_RWKV_REVIEW_REFRESH_INTERVAL
    )


def _rwkv_other_config(deck_config: dict[str, object]) -> dict[str, object] | None:
    direct = deck_config.get("jschoreels.rwkv", deck_config.get("jschoreels.fsrs"))
    if isinstance(direct, dict):
        return direct

    other = deck_config.get("other")
    if isinstance(other, dict):
        root = other
    elif isinstance(other, (bytes, bytearray)):
        root = _json_object_from_text(other.decode("utf8", errors="ignore"))
    elif isinstance(other, str):
        root = _json_object_from_text(other)
    else:
        return None

    value = root.get("jschoreels.rwkv", root.get("jschoreels.fsrs"))
    return value if isinstance(value, dict) else None


def _json_object_from_text(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text)
    except Exception:
        return None

    return value if isinstance(value, dict) else None


def _current_deck_id(reviewer: object) -> int | None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    decks = getattr(col, "decks", None)
    get_current_id = getattr(decks, "get_current_id", None)
    if not callable(get_current_id):
        return None

    try:
        deck_id = get_current_id()
    except Exception:
        logger.debug("failed to read current deck for RWKV queue ordering")
        return None

    return deck_id if isinstance(deck_id, int) else None


def _collection(reviewer: object) -> object | None:
    mw = getattr(reviewer, "mw", None)
    return getattr(mw, "col", None)


def _review_card_ids_in_deck_tree(reviewer: object, deck_id: int) -> list[int]:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    decks = getattr(col, "decks", None)
    deck_and_child_ids = getattr(decks, "deck_and_child_ids", None)
    db = getattr(col, "db", None)
    db_list = getattr(db, "list", None)
    if not callable(deck_and_child_ids) or not callable(db_list):
        return []

    deck_ids = deck_and_child_ids(deck_id)
    if not deck_ids:
        return []

    return [
        int(card_id)
        for card_id in db_list(
            f"select id from cards where did in {ids2str(deck_ids)} and queue = ?",
            int(QUEUE_TYPE_REV),
        )
        if isinstance(card_id, int)
    ]


def _rwkv_review_queue_scores_for_deck(
    *,
    reviewer: object,
    deck_id: int,
    batch_size: int,
) -> tuple[list[tuple[int, float]], RwkvReviewInputBatchBuild] | None:
    if not _reviewer_backend_accepts_review_inputs():
        return None

    start = time.monotonic()
    input_build = _rwkv_review_input_batches_for_deck_review_queue(
        reviewer=reviewer,
        deck_id=deck_id,
        batch_size_override=batch_size,
    )
    if input_build is None:
        return None

    score_start = time.monotonic()
    scores: list[tuple[int, float]] = []
    for input_batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items():
        input_scores = _rwkv_review_scores_for_inputs(
            inputs_by_card_id,
            batch_size=input_batch_size,
        )
        if input_scores is None:
            return None
        scores.extend(input_scores)

    logger.debug(
        "RWKV review queue deck inputs scored: deck_id=%s searched=%s loaded=%s "
        "with_state=%s enabled=%s inputs=%s scored=%s deck_configs=%s "
        "batch_size=%s load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f "
        "prediction_elapsed_ms=%.1f elapsed_ms=%.1f",
        deck_id,
        input_build.searched_rows,
        input_build.parsed_cards,
        input_build.cards_with_state,
        input_build.eligible_cards,
        sum(len(inputs) for inputs in input_build.inputs_by_batch_size.values()),
        len(scores),
        input_build.deck_configs,
        batch_size,
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        (time.monotonic() - score_start) * 1000,
        (time.monotonic() - start) * 1000,
    )
    return scores, input_build


def _rwkv_review_queue_scores(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    batch_size: int,
) -> list[tuple[int, float]]:
    start = time.monotonic()
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return []

    use_input_scoring = _reviewer_backend_accepts_review_inputs()
    if use_input_scoring:
        input_build = _rwkv_review_input_batches_for_ids(
            reviewer=reviewer,
            card_ids=card_ids,
            timing=timing,
            reason="review queue",
            include_suspended_review=False,
            supported_state_filter=True,
            batch_size_override=batch_size,
        )
        if input_build is not None:
            score_start = time.monotonic()
            scores: list[tuple[int, float]] = []
            for (
                input_batch_size,
                batch_inputs_by_card_id,
            ) in input_build.inputs_by_batch_size.items():
                input_scores = _rwkv_review_scores_for_inputs(
                    batch_inputs_by_card_id,
                    batch_size=input_batch_size,
                )
                if input_scores is None:
                    scores = []
                    use_input_scoring = False
                    break
                scores.extend(input_scores)
            if use_input_scoring:
                logger.debug(
                    "RWKV review queue inputs scored: card_ids=%s loaded=%s "
                    "with_state=%s enabled=%s inputs=%s scored=%s deck_configs=%s "
                    "batch_size=%s preset_elapsed_ms=%.1f load_elapsed_ms=%.1f "
                    "candidate_elapsed_ms=%.1f prediction_elapsed_ms=%.1f "
                    "elapsed_ms=%.1f",
                    len(card_ids),
                    input_build.parsed_cards,
                    input_build.cards_with_state,
                    input_build.eligible_cards,
                    sum(
                        len(inputs)
                        for inputs in input_build.inputs_by_batch_size.values()
                    ),
                    len(scores),
                    input_build.deck_configs,
                    batch_size,
                    input_build.preset_elapsed_ms,
                    input_build.load_elapsed_ms,
                    input_build.candidate_elapsed_ms,
                    (time.monotonic() - score_start) * 1000,
                    (time.monotonic() - start) * 1000,
                )
                return scores

    inputs_by_card_id: list[tuple[int, RwkvReviewInput]] = []
    candidates: list[RwkvReviewCandidate] = []
    deck_configs: dict[int, dict[str, object] | None] = {}
    loaded_cards = _rwkv_cards_for_ids(
        reviewer,
        card_ids,
        reason="review queue",
        use_enabled_deck_filter=True,
    )
    cards_with_state = 0
    eligible_cards: list[
        tuple[
            RwkvStatsGraphCard,
            dict[str, object],
            tuple[object, str | None, int | None, int | None],
        ]
    ] = []
    for card in loaded_cards:
        state_fields = _rwkv_state_fields_for_stats_graph_card(
            card,
            timing,
            include_suspended_review=False,
        )
        if state_fields[0] is _UNSUPPORTED_RWKV_STATE:
            continue
        cards_with_state += 1

        deck_id = card.current_deck_id()
        if deck_id not in deck_configs:
            deck_config = _deck_config_for_deck_id(reviewer, deck_id)
            deck_configs[deck_id] = (
                deck_config
                if isinstance(deck_config, dict)
                and _rwkv_review_config_enabled(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            continue
        eligible_cards.append((card, deck_config, state_fields))

    preset_ids_by_card = _resolved_fsrs_preset_ids(
        reviewer,
        [card.id for card, _, _ in eligible_cards],
    )
    for card, deck_config, state_fields in eligible_cards:
        if use_input_scoring:
            review_input = _rwkv_review_input_for_stats_graph_card(
                card=card,
                deck_config=deck_config,
                timing=timing,
                resolved_preset_id=preset_ids_by_card.get(card.id),
                state_fields=state_fields,
            )
            if review_input is not None:
                inputs_by_card_id.append((card.id, review_input))
            continue

        states = _stats_graph_scheduling_states(card, timing)
        if states is None:
            continue
        candidates.append(
            RwkvReviewCandidate(
                reviewer=_stats_graph_reviewer_context(
                    deck_config=deck_config,
                    states=states,
                    timing=timing,
                    resolved_preset_id=preset_ids_by_card.get(card.id),
                ),
                card=card,
            )
        )

    candidate_elapsed_ms = (time.monotonic() - start) * 1000
    score_start = time.monotonic()
    scores = (
        _rwkv_review_scores_for_inputs(inputs_by_card_id, batch_size=batch_size)
        if use_input_scoring
        else None
    )
    if scores is None:
        scores = _rwkv_review_scores_for_candidates(candidates, batch_size=batch_size)
    logger.debug(
        "RWKV review queue candidates scored: card_ids=%s loaded=%s "
        "with_state=%s enabled=%s inputs=%s scored=%s deck_configs=%s batch_size=%s "
        "candidate_elapsed_ms=%.1f prediction_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(card_ids),
        len(loaded_cards),
        cards_with_state,
        len(inputs_by_card_id) if use_input_scoring else len(candidates),
        len(inputs_by_card_id),
        len(scores),
        len(deck_configs),
        batch_size,
        candidate_elapsed_ms,
        (time.monotonic() - score_start) * 1000,
        (time.monotonic() - start) * 1000,
    )
    return scores


def _rwkv_stats_graph_scores(
    *,
    reviewer: object,
    card_ids: Sequence[int],
) -> list[tuple[int, float]]:
    start = time.monotonic()
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return []

    use_input_scoring = _reviewer_backend_accepts_review_inputs()
    if use_input_scoring:
        input_build = _rwkv_review_input_batches_for_ids(
            reviewer=reviewer,
            card_ids=card_ids,
            timing=timing,
            reason="stats graph",
            include_suspended_review=True,
            supported_state_filter=True,
        )
        if input_build is not None:
            input_scores_accum: list[tuple[int, float]] = []
            queue_score_cache = _fresh_rwkv_review_queue_score_map()
            queue_score_hits = 0
            score_start = time.monotonic()
            for (
                batch_size,
                inputs_by_card_id,
            ) in input_build.inputs_by_batch_size.items():
                cached_scores, inputs_by_card_id = _split_rwkv_queue_score_hits(
                    inputs_by_card_id,
                    queue_score_cache,
                )
                queue_score_hits += len(cached_scores)
                input_scores_accum.extend(cached_scores)
                if not inputs_by_card_id:
                    continue

                input_scores = _rwkv_review_scores_for_inputs(
                    inputs_by_card_id,
                    batch_size=batch_size,
                )
                if input_scores is None:
                    input_scores_accum = []
                    use_input_scoring = False
                    break
                input_scores_accum.extend(input_scores)
            if use_input_scoring:
                score_elapsed_ms = (time.monotonic() - score_start) * 1000
                logger.debug(
                    "RWKV stats graph inputs scored: card_ids=%s loaded=%s "
                    "unsupported_state=%s with_state=%s disabled_config=%s "
                    "enabled=%s scored=%s queue_score_hits=%s deck_configs=%s batches=%s "
                    "preset_elapsed_ms=%.1f load_elapsed_ms=%.1f "
                    "candidate_elapsed_ms=%.1f score_elapsed_ms=%.1f "
                    "elapsed_ms=%.1f",
                    len(card_ids),
                    input_build.parsed_cards,
                    input_build.parsed_cards - input_build.cards_with_state,
                    input_build.cards_with_state,
                    input_build.disabled_config_cards,
                    input_build.eligible_cards,
                    len(input_scores_accum),
                    queue_score_hits,
                    input_build.deck_configs,
                    {
                        batch_size: len(inputs_by_card_id)
                        for batch_size, inputs_by_card_id in (
                            input_build.inputs_by_batch_size.items()
                        )
                    },
                    input_build.preset_elapsed_ms,
                    input_build.load_elapsed_ms,
                    input_build.candidate_elapsed_ms,
                    score_elapsed_ms,
                    (time.monotonic() - start) * 1000,
                )
                return input_scores_accum

    deck_configs: dict[int, dict[str, object] | None] = {}
    candidates_by_batch_size: dict[int, list[RwkvReviewCandidate]] = {}
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    preset_elapsed_ms = 0.0
    load_start = time.monotonic()
    loaded_cards = _stats_graph_cards_for_ids(reviewer, card_ids)
    load_elapsed_ms = (time.monotonic() - load_start) * 1000
    candidate_start = time.monotonic()
    unsupported_state_cards = 0
    disabled_config_cards = 0
    cards_with_state = 0
    eligible_cards: list[
        tuple[
            RwkvStatsGraphCard,
            dict[str, object],
            tuple[object, str | None, int | None, int | None],
            int,
        ]
    ] = []
    for card in loaded_cards:
        state_fields = _rwkv_state_fields_for_stats_graph_card(
            card,
            timing,
            include_suspended_review=True,
        )
        if state_fields[0] is _UNSUPPORTED_RWKV_STATE:
            unsupported_state_cards += 1
            continue
        cards_with_state += 1

        deck_id = card.current_deck_id()
        if deck_id not in deck_configs:
            deck_config = _deck_config_for_deck_id(reviewer, deck_id)
            deck_configs[deck_id] = (
                deck_config
                if isinstance(deck_config, dict)
                and _rwkv_review_config_enabled(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            disabled_config_cards += 1
            continue

        batch_size = _rwkv_review_batch_size(deck_config)
        eligible_cards.append((card, deck_config, state_fields, batch_size))

    preset_start = time.monotonic()
    preset_ids_by_card = _resolved_fsrs_preset_ids(
        reviewer,
        [card.id for card, _, _, _ in eligible_cards],
    )
    preset_elapsed_ms = (time.monotonic() - preset_start) * 1000
    for card, deck_config, state_fields, batch_size in eligible_cards:
        if use_input_scoring:
            review_input = _rwkv_review_input_for_stats_graph_card(
                card=card,
                deck_config=deck_config,
                timing=timing,
                resolved_preset_id=preset_ids_by_card.get(card.id),
                include_suspended_review=True,
                state_fields=state_fields,
            )
            if review_input is not None:
                inputs_by_batch_size.setdefault(batch_size, []).append(
                    (card.id, review_input)
                )
            continue

        states = _stats_graph_scheduling_states(
            card,
            timing,
            include_suspended_review=True,
        )
        if states is None:
            unsupported_state_cards += 1
            continue

        context = _stats_graph_reviewer_context(
            deck_config=deck_config,
            states=states,
            timing=timing,
            resolved_preset_id=preset_ids_by_card.get(card.id),
        )
        candidates_by_batch_size.setdefault(batch_size, []).append(
            RwkvReviewCandidate(reviewer=context, card=card)
        )

    scores: list[tuple[int, float]] = []
    score_start = time.monotonic()
    if use_input_scoring:
        for batch_size, inputs_by_card_id in inputs_by_batch_size.items():
            input_scores = _rwkv_review_scores_for_inputs(
                inputs_by_card_id,
                batch_size=batch_size,
            )
            if input_scores is None:
                use_input_scoring = False
                break
            scores.extend(input_scores)

    if not use_input_scoring:
        for batch_size, candidates in candidates_by_batch_size.items():
            scores.extend(
                _rwkv_review_scores_for_candidates(candidates, batch_size=batch_size)
            )
    score_elapsed_ms = (time.monotonic() - score_start) * 1000
    candidate_elapsed_ms = (time.monotonic() - candidate_start) * 1000
    logger.debug(
        "RWKV stats graph candidates scored: card_ids=%s loaded=%s "
        "unsupported_state=%s with_state=%s disabled_config=%s enabled=%s "
        "scored=%s deck_configs=%s batches=%s "
        "preset_elapsed_ms=%.1f load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f "
        "score_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(card_ids),
        len(loaded_cards),
        unsupported_state_cards,
        cards_with_state,
        disabled_config_cards,
        len(eligible_cards),
        len(scores),
        len(deck_configs),
        (
            {
                batch_size: len(inputs_by_card_id)
                for batch_size, inputs_by_card_id in inputs_by_batch_size.items()
            }
            if use_input_scoring
            else {
                batch_size: len(candidates)
                for batch_size, candidates in candidates_by_batch_size.items()
            }
        ),
        preset_elapsed_ms,
        load_elapsed_ms,
        candidate_elapsed_ms,
        score_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return scores


def _rwkv_stats_graph_scores_for_search(
    *,
    reviewer: object,
    search: str,
) -> tuple[list[tuple[int, float]], RwkvReviewInputBatchBuild] | None:
    start = time.monotonic()
    if not _reviewer_backend_accepts_review_inputs():
        return None

    input_build = _rwkv_review_input_batches_for_search(
        reviewer=reviewer,
        search=search,
        include_suspended_review=True,
    )
    if input_build is None:
        return None

    scores: list[tuple[int, float]] = []
    queue_score_cache = _fresh_rwkv_review_queue_score_map()
    queue_score_hits = 0
    score_start = time.monotonic()
    for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items():
        cached_scores, inputs_by_card_id = _split_rwkv_queue_score_hits(
            inputs_by_card_id,
            queue_score_cache,
        )
        queue_score_hits += len(cached_scores)
        scores.extend(cached_scores)
        if not inputs_by_card_id:
            continue

        input_scores = _rwkv_review_scores_for_inputs(
            inputs_by_card_id,
            batch_size=batch_size,
        )
        if input_scores is None:
            return None
        scores.extend(input_scores)

    score_elapsed_ms = (time.monotonic() - score_start) * 1000
    logger.debug(
        "RWKV stats graph search inputs scored: search=%r loaded=%s "
        "unsupported_state=%s with_state=%s disabled_config=%s enabled=%s "
        "scored=%s queue_score_hits=%s deck_configs=%s batches=%s "
        "load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f "
        "score_elapsed_ms=%.1f elapsed_ms=%.1f",
        search,
        input_build.parsed_cards,
        input_build.parsed_cards - input_build.cards_with_state,
        input_build.cards_with_state,
        input_build.disabled_config_cards,
        input_build.eligible_cards,
        len(scores),
        queue_score_hits,
        input_build.deck_configs,
        {
            batch_size: len(inputs_by_card_id)
            for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items()
        },
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        score_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return scores, input_build


def _split_rwkv_queue_score_hits(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    queue_score_cache: dict[int, float],
) -> tuple[list[tuple[int, float]], list[tuple[int, RwkvReviewInput]]]:
    if not queue_score_cache:
        return [], list(inputs_by_card_id)

    cached_scores: list[tuple[int, float]] = []
    missing_inputs: list[tuple[int, RwkvReviewInput]] = []
    for card_id, review_input in inputs_by_card_id:
        score = queue_score_cache.get(card_id)
        if score is None:
            missing_inputs.append((card_id, review_input))
        else:
            cached_scores.append((card_id, score))

    return cached_scores, missing_inputs


def _fresh_rwkv_review_queue_score_map() -> dict[int, float]:
    state_generation = _reviewer_backend_state_generation()
    scores: dict[int, float] = {}
    for deck_id, deck_scores in _rwkv_review_queue_score_maps.items():
        if _rwkv_review_queue_score_generations.get(deck_id) == state_generation:
            scores.update(deck_scores)
    return scores


def _rwkv_review_reschedule_card_ids(mw: object) -> list[int]:
    col = getattr(mw, "col", None)
    db = getattr(col, "db", None)
    list_rows = getattr(db, "list", None)
    if not callable(list_rows):
        return []

    try:
        rows = list_rows(
            """
select id
from cards
where type = ?
  and queue = ?
""",
            int(CARD_TYPE_REV),
            int(QUEUE_TYPE_REV),
        )
    except Exception:
        logger.debug("failed to load RWKV reschedule card ids")
        return []

    return [card_id for card_id in rows if isinstance(card_id, int)]


def _rwkv_review_reschedule_items(
    reviewer: object,
    card_ids: Sequence[int],
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> list[RwkvReviewRescheduleItem]:
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return []

    items: list[RwkvReviewRescheduleItem] = []
    processed_cards = 0
    total_cards = len(card_ids)
    for chunk in _chunks(list(card_ids), 5000):
        deck_configs: dict[int, dict[str, object] | None] = {}
        candidates_by_batch_size: dict[int, list[RwkvReviewCandidate]] = {}
        elapsed_days_by_card_id: dict[int, int] = {}
        preset_ids_by_card = _resolved_fsrs_preset_ids(reviewer, chunk)
        loaded_cards = _rwkv_cards_for_ids(
            reviewer,
            chunk,
            reason="RWKV reschedule",
        )

        for card in loaded_cards:
            states = _stats_graph_scheduling_states(card, timing)
            if states is None:
                continue
            current = states.current
            if (
                current.WhichOneof("kind") != "normal"
                or current.normal.WhichOneof("kind") != "review"
            ):
                continue

            deck_id = card.current_deck_id()
            if deck_id not in deck_configs:
                deck_config = _deck_config_for_deck_id(reviewer, deck_id)
                deck_configs[deck_id] = (
                    deck_config
                    if isinstance(deck_config, dict)
                    and _rwkv_review_config_enabled(deck_config)
                    else None
                )

            deck_config = deck_configs[deck_id]
            if deck_config is None:
                continue

            elapsed_days_by_card_id[card.id] = current.normal.review.elapsed_days
            candidates_by_batch_size.setdefault(
                _rwkv_review_batch_size(deck_config),
                [],
            ).append(
                RwkvReviewCandidate(
                    reviewer=_stats_graph_reviewer_context(
                        deck_config=deck_config,
                        states=states,
                        timing=timing,
                        resolved_preset_id=preset_ids_by_card.get(card.id),
                    ),
                    card=card,
                )
            )

        for batch_size, candidates in candidates_by_batch_size.items():
            for batch in _chunks(candidates, batch_size):
                predictions = _predict_review_batch(batch)
                for candidate, prediction in zip(batch, predictions, strict=True):
                    item = _rwkv_review_reschedule_item(
                        candidate,
                        prediction,
                        elapsed_days_by_card_id,
                    )
                    if item is not None:
                        items.append(item)

        processed_cards += len(chunk)
        _report_rwkv_state_cache_progress(
            progress,
            "Predicting RWKV reschedule intervals...",
            processed_cards,
            total_cards,
        )

    logger.debug(
        "RWKV review reschedule items built: cards=%s items=%s",
        len(card_ids),
        len(items),
    )
    return items


def _rwkv_review_reschedule_item(
    candidate: RwkvReviewCandidate,
    prediction: RwkvReviewPrediction | None,
    elapsed_days_by_card_id: dict[int, int],
) -> RwkvReviewRescheduleItem | None:
    if prediction is None:
        return None

    try:
        _validate_prediction(prediction)
    except ValueError:
        logger.debug("invalid RWKV reschedule prediction", exc_info=True)
        return None

    card_id = _card_id(candidate.card)
    if card_id is None:
        return None
    elapsed_days = elapsed_days_by_card_id.get(card_id)
    if elapsed_days is None:
        return None
    if prediction.current_interval is None or prediction.current_s90 is None:
        return None

    return RwkvReviewRescheduleItem(
        card_id=card_id,
        interval_days=prediction.current_interval,
        elapsed_days=elapsed_days,
        s90=prediction.current_s90,
    )


def _apply_rwkv_review_reschedule(
    mw: object,
    items: Sequence[RwkvReviewRescheduleItem],
) -> object:
    from anki.collection import OpChangesWithCount

    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    apply_raw = getattr(backend, "apply_rwkv_review_reschedule_raw", None)
    if not callable(apply_raw):
        raise ValueError("RWKV reschedule backend API is unavailable")

    request = scheduler_pb2.RwkvReviewRescheduleRequest()
    for item in items:
        request.items.add(
            card_id=item.card_id,
            interval_days=item.interval_days,
            elapsed_days=item.elapsed_days,
            s90=float(item.s90),
        )

    response = OpChangesWithCount()
    response.ParseFromString(apply_raw(request.SerializeToString()))
    return response


def _rwkv_review_scores_for_candidates(
    candidates: Sequence[RwkvReviewCandidate],
    *,
    batch_size: int,
) -> list[tuple[int, float]]:
    start = time.monotonic()
    cached = _cached_review_predictions_for_candidates(candidates)
    if cached is None:
        return _rwkv_review_scores_for_candidates_without_cache_split(
            candidates,
            batch_size=batch_size,
            start=start,
        )
    else:
        predictions, requests_by_index, cache_hits = cached

    if not requests_by_index:
        scores = _scores_from_review_predictions(candidates, predictions)
        logger.debug(
            "RWKV review prediction candidates scored from cache: candidates=%s "
            "cache_hits=%s scored=%s elapsed_ms=%.1f",
            len(candidates),
            cache_hits,
            len(scores),
            (time.monotonic() - start) * 1000,
        )
        return scores

    predict_start = time.monotonic()
    runtime_batch_size = _rwkv_retrievability_batch_size(batch_size)
    for missing_offset in range(0, len(requests_by_index), runtime_batch_size):
        batch_requests_by_index = requests_by_index[
            missing_offset : missing_offset + runtime_batch_size
        ]
        batch_start = time.monotonic()
        logger.debug(
            "RWKV review prediction runtime batch started: missing_offset=%s "
            "size=%s batch_size=%s configured_batch_size=%s cache_hits=%s",
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            batch_size,
            cache_hits,
        )
        batch_predictions = _predict_retrievability_requests(
            [request for _, request in batch_requests_by_index]
        )
        batch_predict_elapsed_ms = (time.monotonic() - batch_start) * 1000
        if len(batch_predictions) != len(batch_requests_by_index):
            raise ValueError("RWKV batch prediction count mismatch")

        for (index, _), prediction in zip(
            batch_requests_by_index,
            batch_predictions,
            strict=True,
        ):
            predictions[index] = prediction
        logger.debug(
            "RWKV review prediction runtime batch processed: missing_offset=%s "
            "size=%s batch_size=%s configured_batch_size=%s "
            "predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            batch_size,
            batch_predict_elapsed_ms,
            (time.monotonic() - batch_start) * 1000,
        )

    scores = _scores_from_review_predictions(candidates, predictions)
    logger.debug(
        "RWKV review prediction candidates scored: candidates=%s cache_hits=%s "
        "runtime_requests=%s scored=%s batch_size=%s predict_elapsed_ms=%.1f "
        "elapsed_ms=%.1f",
        len(candidates),
        cache_hits,
        len(requests_by_index),
        len(scores),
        runtime_batch_size,
        (time.monotonic() - predict_start) * 1000,
        (time.monotonic() - start) * 1000,
    )
    return scores


def _rwkv_review_scores_for_inputs(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    *,
    batch_size: int,
) -> list[tuple[int, float]] | None:
    start = time.monotonic()
    cached = _cached_review_input_predictions_for_inputs(
        [
            (index, review_input)
            for index, (_, review_input) in enumerate(inputs_by_card_id)
        ]
    )
    if cached is None:
        return None

    predictions, requests_by_index, cache_hits = cached
    if not requests_by_index:
        scores = _scores_from_input_predictions(inputs_by_card_id, predictions)
        logger.debug(
            "RWKV review inputs scored from cache: inputs=%s cache_hits=%s "
            "scored=%s elapsed_ms=%.1f",
            len(inputs_by_card_id),
            cache_hits,
            len(scores),
            (time.monotonic() - start) * 1000,
        )
        return scores

    predict_start = time.monotonic()
    runtime_batch_size = _rwkv_retrievability_batch_size(batch_size)
    for missing_offset in range(0, len(requests_by_index), runtime_batch_size):
        batch_requests_by_index = requests_by_index[
            missing_offset : missing_offset + runtime_batch_size
        ]
        batch_start = time.monotonic()
        logger.debug(
            "RWKV review input runtime batch started: missing_offset=%s "
            "size=%s batch_size=%s configured_batch_size=%s cache_hits=%s",
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            batch_size,
            cache_hits,
        )
        batch_predictions = _predict_retrievability_requests(
            [request for _, request in batch_requests_by_index]
        )
        batch_predict_elapsed_ms = (time.monotonic() - batch_start) * 1000
        if len(batch_predictions) != len(batch_requests_by_index):
            raise ValueError("RWKV batch prediction count mismatch")

        for (index, _), prediction in zip(
            batch_requests_by_index,
            batch_predictions,
            strict=True,
        ):
            predictions[index] = prediction
        logger.debug(
            "RWKV review input runtime batch processed: missing_offset=%s "
            "size=%s batch_size=%s configured_batch_size=%s "
            "predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            batch_size,
            batch_predict_elapsed_ms,
            (time.monotonic() - batch_start) * 1000,
        )

    scores = _scores_from_input_predictions(inputs_by_card_id, predictions)
    logger.debug(
        "RWKV review inputs scored: inputs=%s cache_hits=%s runtime_requests=%s "
        "scored=%s batch_size=%s predict_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(inputs_by_card_id),
        cache_hits,
        len(requests_by_index),
        len(scores),
        runtime_batch_size,
        (time.monotonic() - predict_start) * 1000,
        (time.monotonic() - start) * 1000,
    )
    return scores


def _cached_review_input_predictions_for_inputs(
    inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
) -> RwkvCachedReviewPredictions | None:
    backend = _reviewer_backend
    cached_review_input_predictions = getattr(
        backend,
        "cached_review_input_predictions",
        None,
    )
    if not callable(cached_review_input_predictions):
        return None

    return cast(
        RwkvCachedReviewPredictions,
        cached_review_input_predictions(inputs_by_index),
    )


def _reviewer_backend_accepts_review_inputs() -> bool:
    return callable(
        getattr(
            _reviewer_backend,
            "cached_review_input_predictions",
            None,
        )
    )


def _scores_from_input_predictions(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    predictions: Sequence[RwkvReviewPrediction | None],
) -> list[tuple[int, float]]:
    if len(predictions) != len(inputs_by_card_id):
        raise ValueError("RWKV batch prediction count mismatch")

    scores: list[tuple[int, float]] = []
    for (card_id, _), prediction in zip(inputs_by_card_id, predictions, strict=True):
        if prediction is None or prediction.retrievability is None:
            continue

        _validate_prediction(prediction)
        scores.append((card_id, prediction.retrievability))

    return scores


def _rwkv_retrievability_batch_size(batch_size: int) -> int:
    if batch_size == _DEFAULT_RWKV_REVIEW_BATCH_SIZE:
        return _MAX_RWKV_REVIEW_BATCH_SIZE
    return batch_size


def _rwkv_review_scores_for_candidates_without_cache_split(
    candidates: Sequence[RwkvReviewCandidate],
    *,
    batch_size: int,
    start: float,
) -> list[tuple[int, float]]:
    scores: list[tuple[int, float]] = []
    for batch_offset in range(0, len(candidates), batch_size):
        batch = candidates[batch_offset : batch_offset + batch_size]
        batch_start = time.monotonic()
        logger.debug(
            "RWKV review prediction batch started: offset=%s size=%s batch_size=%s",
            batch_offset,
            len(batch),
            batch_size,
        )
        predictions = _predict_review_batch(batch)
        predict_elapsed_ms = (time.monotonic() - batch_start) * 1000
        scored_before = len(scores)
        scores.extend(_scores_from_review_predictions(batch, predictions))
        logger.debug(
            "RWKV review prediction batch processed: offset=%s size=%s scored=%s "
            "batch_size=%s predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            batch_offset,
            len(batch),
            len(scores) - scored_before,
            batch_size,
            predict_elapsed_ms,
            (time.monotonic() - batch_start) * 1000,
        )

    logger.debug(
        "RWKV review prediction candidates scored without cache split: "
        "candidates=%s scored=%s batch_size=%s elapsed_ms=%.1f",
        len(candidates),
        len(scores),
        batch_size,
        (time.monotonic() - start) * 1000,
    )
    return scores


def _cached_review_predictions_for_candidates(
    candidates: Sequence[RwkvReviewCandidate],
) -> RwkvCachedReviewPredictions | None:
    backend = _reviewer_backend
    cached_review_predictions = getattr(backend, "cached_review_predictions", None)
    if not callable(cached_review_predictions):
        return None

    return cast(RwkvCachedReviewPredictions, cached_review_predictions(candidates))


def _predict_review_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
) -> Sequence[RwkvReviewPrediction | None]:
    backend = _reviewer_backend
    predict_review_requests = getattr(backend, "predict_review_requests", None)
    if not callable(predict_review_requests):
        raise ValueError("RWKV backend does not support request batch prediction")

    return cast(
        Sequence[RwkvReviewPrediction | None], predict_review_requests(requests)
    )


def _predict_retrievability_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
) -> Sequence[RwkvReviewPrediction | None]:
    backend = _reviewer_backend
    predict_retrievability_requests = getattr(
        backend,
        "predict_retrievability_requests",
        None,
    )
    if callable(predict_retrievability_requests):
        return cast(
            Sequence[RwkvReviewPrediction | None],
            predict_retrievability_requests(requests),
        )

    return _predict_review_requests(requests)


def _scores_from_review_predictions(
    candidates: Sequence[RwkvReviewCandidate],
    predictions: Sequence[RwkvReviewPrediction | None],
) -> list[tuple[int, float]]:
    if len(predictions) != len(candidates):
        raise ValueError("RWKV batch prediction count mismatch")

    scores: list[tuple[int, float]] = []
    for candidate, prediction in zip(candidates, predictions, strict=True):
        if prediction is None or prediction.retrievability is None:
            continue

        _validate_prediction(prediction)
        card_id = _card_id(candidate.card)
        if card_id is not None:
            scores.append((card_id, prediction.retrievability))

    return scores


def _rwkv_review_input_batches_for_ids(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    timing: object,
    reason: str,
    include_suspended_review: bool,
    supported_state_filter: bool,
    batch_size_override: int | None = None,
    use_enabled_deck_filter: bool = True,
) -> RwkvReviewInputBatchBuild | None:
    backend_build = _rwkv_review_input_batches_from_backend_for_ids(
        reviewer=reviewer,
        card_ids=card_ids,
        include_suspended_review=include_suspended_review,
        batch_size_override=batch_size_override,
        use_enabled_deck_filter=use_enabled_deck_filter,
    )
    if backend_build is not None:
        return backend_build

    candidate_start = time.monotonic()
    load_start = time.monotonic()
    rows = _rwkv_card_rows_for_ids(
        reviewer,
        card_ids,
        reason=reason,
        supported_state_filter=supported_state_filter,
        enabled_deck_ids=(
            _rwkv_enabled_deck_id_filter(reviewer) if use_enabled_deck_filter else None
        ),
    )
    if rows is None:
        return None
    load_elapsed_ms = (time.monotonic() - load_start) * 1000

    parsed_cards = [
        fields for row in rows if (fields := _stats_graph_card_fields_from_row(row))
    ]
    missing_review_time_ids = [
        fields.id for fields in parsed_cards if fields.last_review_time is None
    ]
    latest_review_times = _latest_eligible_review_times_for_cards(
        reviewer,
        missing_review_time_ids,
        reason=reason,
    )
    if latest_review_times:
        parsed_cards = [
            fields._replace(last_review_time=latest_review_times[fields.id])
            if fields.last_review_time is None and fields.id in latest_review_times
            else fields
            for fields in parsed_cards
        ]

    deck_configs: dict[int, dict[str, object] | None] = {}
    eligible_fields: list[
        tuple[
            RwkvStatsGraphCardFields,
            dict[str, object],
            tuple[object, str | None, int | None, int | None],
            int,
        ]
    ] = []
    cards_with_state = 0
    disabled_config_cards = 0
    for fields in parsed_cards:
        state_fields = _rwkv_state_fields_for_stats_graph_fields(
            fields,
            timing,
            include_suspended_review=include_suspended_review,
        )
        if state_fields[0] is _UNSUPPORTED_RWKV_STATE:
            continue
        cards_with_state += 1

        deck_id = fields.current_deck_id()
        if deck_id not in deck_configs:
            deck_config = _deck_config_for_deck_id(reviewer, deck_id)
            deck_configs[deck_id] = (
                deck_config
                if isinstance(deck_config, dict)
                and _rwkv_review_config_enabled(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            disabled_config_cards += 1
            continue
        batch_size = (
            batch_size_override
            if batch_size_override is not None
            else _rwkv_review_batch_size(deck_config)
        )
        eligible_fields.append((fields, deck_config, state_fields, batch_size))

    preset_start = time.monotonic()
    preset_ids_by_card = _resolved_fsrs_preset_ids(
        reviewer,
        [fields.id for fields, _, _, _ in eligible_fields],
    )
    preset_elapsed_ms = (time.monotonic() - preset_start) * 1000

    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    for fields, deck_config, state_fields, batch_size in eligible_fields:
        review_input = _rwkv_review_input_for_stats_graph_fields(
            fields=fields,
            deck_config=deck_config,
            timing=timing,
            resolved_preset_id=preset_ids_by_card.get(fields.id),
            state_fields=state_fields,
        )
        if review_input is not None:
            inputs_by_batch_size.setdefault(batch_size, []).append(
                (fields.id, review_input)
            )

    return RwkvReviewInputBatchBuild(
        inputs_by_batch_size=inputs_by_batch_size,
        loaded_rows=len(rows),
        parsed_cards=len(parsed_cards),
        cards_with_state=cards_with_state,
        disabled_config_cards=disabled_config_cards,
        eligible_cards=len(eligible_fields),
        deck_configs=len(deck_configs),
        preset_elapsed_ms=preset_elapsed_ms,
        load_elapsed_ms=load_elapsed_ms,
        candidate_elapsed_ms=(time.monotonic() - candidate_start) * 1000,
    )


def _rwkv_review_input_batches_from_backend_for_ids(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    include_suspended_review: bool,
    batch_size_override: int | None,
    use_enabled_deck_filter: bool,
) -> RwkvReviewInputBatchBuild | None:
    if not card_ids:
        return RwkvReviewInputBatchBuild(
            inputs_by_batch_size={},
            loaded_rows=0,
            parsed_cards=0,
            cards_with_state=0,
            disabled_config_cards=0,
            eligible_cards=0,
            deck_configs=0,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )
    if not use_enabled_deck_filter:
        return None

    col = _collection(reviewer)
    backend = getattr(col, "_backend", None)
    if backend is None:
        return None

    load_start = time.monotonic()
    response = _rwkv_review_input_rows_backend_response(
        backend,
        card_ids=card_ids,
        include_suspended_review=include_suspended_review,
    )
    if response is None:
        return None

    return _rwkv_review_input_batch_build_from_backend_response(
        response=response,
        batch_size_override=batch_size_override,
        load_start=load_start,
        source_label="cards",
        source_size=len(card_ids),
    )


def _rwkv_review_input_batches_for_search(
    *,
    reviewer: object,
    search: str,
    include_suspended_review: bool,
    batch_size_override: int | None = None,
    use_enabled_deck_filter: bool = True,
) -> RwkvReviewInputBatchBuild | None:
    if not use_enabled_deck_filter:
        return None

    col = _collection(reviewer)
    backend = getattr(col, "_backend", None)
    if backend is None:
        return None

    load_start = time.monotonic()
    response = _rwkv_review_input_rows_for_search_backend_response(
        backend,
        search=search,
        include_suspended_review=include_suspended_review,
    )
    if response is None:
        return None

    return _rwkv_review_input_batch_build_from_backend_response(
        response=response,
        batch_size_override=batch_size_override,
        load_start=load_start,
        source_label="search_cards",
        source_size=_rwkv_backend_uint(response, "searched_cards"),
    )


def _rwkv_review_input_batches_for_deck_review_queue(
    *,
    reviewer: object,
    deck_id: int,
    batch_size_override: int,
) -> RwkvReviewInputBatchBuild | None:
    col = _collection(reviewer)
    backend = getattr(col, "_backend", None)
    if backend is None:
        return None

    load_start = time.monotonic()
    response = _rwkv_review_input_rows_for_deck_review_queue_backend_response(
        backend,
        deck_id=deck_id,
    )
    if response is None:
        return None

    return _rwkv_review_input_batch_build_from_backend_response(
        response=response,
        batch_size_override=batch_size_override,
        load_start=load_start,
        source_label="deck_review_queue_cards",
        source_size=_rwkv_backend_uint(response, "searched_cards"),
    )


def _rwkv_review_input_batch_build_from_backend_response(
    *,
    response: object,
    batch_size_override: int | None,
    load_start: float,
    source_label: str,
    source_size: int,
) -> RwkvReviewInputBatchBuild:
    if isinstance(response, scheduler_pb2.RwkvReviewInputRowsForCardsResponse):
        return _rwkv_review_input_batch_build_from_backend_proto_response(
            response=response,
            batch_size_override=batch_size_override,
            load_start=load_start,
            source_label=source_label,
            source_size=source_size,
        )

    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    parsed_cards = 0
    eligible_cards = 0
    rows = getattr(response, "rows", ())
    for row in rows:
        parsed_cards += 1
        review_input = _rwkv_review_input_from_backend_row(row)
        if review_input is None:
            continue

        card_id = review_input.identity.card_id
        if card_id is None:
            continue
        batch_size = (
            batch_size_override
            if batch_size_override is not None
            else _rwkv_backend_row_batch_size(row)
        )
        inputs_by_batch_size.setdefault(batch_size, []).append((card_id, review_input))
        eligible_cards += 1

    elapsed_ms = (time.monotonic() - load_start) * 1000
    logger.debug(
        "RWKV review input backend rows loaded: %s=%s rows=%s eligible=%s "
        "elapsed_ms=%.1f",
        source_label,
        source_size,
        _rwkv_backend_uint(response, "loaded_cards"),
        eligible_cards,
        elapsed_ms,
    )
    return RwkvReviewInputBatchBuild(
        inputs_by_batch_size=inputs_by_batch_size,
        loaded_rows=_rwkv_backend_uint(response, "loaded_cards"),
        parsed_cards=parsed_cards,
        cards_with_state=_rwkv_backend_uint(response, "cards_with_supported_state"),
        disabled_config_cards=_rwkv_backend_uint(response, "disabled_config_cards"),
        eligible_cards=eligible_cards,
        deck_configs=_rwkv_backend_uint(response, "deck_configs"),
        preset_elapsed_ms=0.0,
        load_elapsed_ms=elapsed_ms,
        candidate_elapsed_ms=elapsed_ms,
        searched_rows=source_size,
    )


def _rwkv_review_input_batch_build_from_backend_proto_response(
    *,
    response: scheduler_pb2.RwkvReviewInputRowsForCardsResponse,
    batch_size_override: int | None,
    load_start: float,
    source_label: str,
    source_size: int,
) -> RwkvReviewInputBatchBuild:
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    parsed_cards = 0
    eligible_cards = 0
    for row in response.rows:
        parsed_cards += 1
        review_input = _rwkv_review_input_from_backend_proto_row(row)
        card_id = review_input.identity.card_id
        batch_size = (
            batch_size_override
            if batch_size_override is not None
            else (
                row.batch_size
                if _valid_rwkv_review_batch_size(row.batch_size)
                else _DEFAULT_RWKV_REVIEW_BATCH_SIZE
            )
        )
        inputs_by_batch_size.setdefault(batch_size, []).append((card_id, review_input))
        eligible_cards += 1

    elapsed_ms = (time.monotonic() - load_start) * 1000
    logger.debug(
        "RWKV review input backend rows loaded: %s=%s rows=%s eligible=%s "
        "elapsed_ms=%.1f",
        source_label,
        source_size,
        response.loaded_cards,
        eligible_cards,
        elapsed_ms,
    )
    return RwkvReviewInputBatchBuild(
        inputs_by_batch_size=inputs_by_batch_size,
        loaded_rows=response.loaded_cards,
        parsed_cards=parsed_cards,
        cards_with_state=response.cards_with_supported_state,
        disabled_config_cards=response.disabled_config_cards,
        eligible_cards=eligible_cards,
        deck_configs=response.deck_configs,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=elapsed_ms,
        candidate_elapsed_ms=elapsed_ms,
        searched_rows=source_size,
    )


def _rwkv_review_input_rows_backend_response(
    backend: object,
    *,
    card_ids: Sequence[int],
    include_suspended_review: bool,
) -> object | None:
    get_rows_raw = getattr(backend, "rwkv_review_input_rows_for_cards_raw", None)
    if callable(get_rows_raw) and hasattr(
        scheduler_pb2,
        "RwkvReviewInputRowsForCardsRequest",
    ):
        try:
            request = scheduler_pb2.RwkvReviewInputRowsForCardsRequest(
                card_ids=card_ids,
                include_suspended_review=include_suspended_review,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug("failed to load RWKV review input rows from backend")
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_cards", None)
    if not callable(get_rows):
        return None

    try:
        return get_rows(
            card_ids=card_ids,
            include_suspended_review=include_suspended_review,
            include_disabled_decks=False,
        )
    except Exception:
        logger.debug("failed to load RWKV review input rows from backend")
        return None


def _rwkv_review_input_rows_for_search_backend_response(
    backend: object,
    *,
    search: str,
    include_suspended_review: bool,
) -> object | None:
    get_rows_raw = getattr(backend, "rwkv_review_input_rows_for_search_raw", None)
    if callable(get_rows_raw) and hasattr(
        scheduler_pb2,
        "RwkvReviewInputRowsForSearchRequest",
    ):
        try:
            request = scheduler_pb2.RwkvReviewInputRowsForSearchRequest(
                search=search,
                include_suspended_review=include_suspended_review,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug(
                "failed to load RWKV review input rows for search from backend"
            )
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_search", None)
    if not callable(get_rows):
        return None

    try:
        return get_rows(
            search=search,
            include_suspended_review=include_suspended_review,
            include_disabled_decks=False,
        )
    except Exception:
        logger.debug("failed to load RWKV review input rows for search from backend")
        return None


def _rwkv_review_input_rows_for_deck_review_queue_backend_response(
    backend: object,
    *,
    deck_id: int,
) -> object | None:
    get_rows_raw = getattr(
        backend,
        "rwkv_review_input_rows_for_deck_review_queue_raw",
        None,
    )
    if callable(get_rows_raw) and hasattr(
        scheduler_pb2,
        "RwkvReviewInputRowsForDeckReviewQueueRequest",
    ):
        try:
            request = scheduler_pb2.RwkvReviewInputRowsForDeckReviewQueueRequest(
                deck_id=deck_id,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug(
                "failed to load RWKV review input rows for deck review queue from backend"
            )
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_deck_review_queue", None)
    if not callable(get_rows):
        return None

    try:
        return get_rows(
            deck_id=deck_id,
            include_disabled_decks=False,
        )
    except Exception:
        logger.debug(
            "failed to load RWKV review input rows for deck review queue from backend"
        )
        return None


def _rwkv_review_input_from_backend_row(row: object) -> RwkvReviewInput | None:
    card_id = _rwkv_backend_int(row, "card_id")
    if card_id is None:
        return None
    note_id = _rwkv_backend_int(row, "note_id")
    deck_id = _rwkv_backend_int(row, "deck_id")
    preset_id = _rwkv_backend_preset_id(row)
    target_retention = _rwkv_backend_probability(
        row,
        "target_retention",
        _RWKV_DEFAULT_TARGET_RETENTION,
    )

    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card_id,
            note_id=note_id,
            deck_id=deck_id,
            preset_id=preset_id,
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=_rwkv_backend_int(row, "card_type"),
        card_queue=_rwkv_backend_int(row, "card_queue"),
        card_due=_rwkv_backend_int(row, "card_due"),
        interval_days=_rwkv_backend_int(row, "interval_days"),
        ease_factor=_rwkv_backend_int(row, "ease_factor"),
        reps=_rwkv_backend_int(row, "reps"),
        lapses=_rwkv_backend_int(row, "lapses"),
        day_offset=_rwkv_backend_int(row, "day_offset"),
        current_state_kind=_rwkv_backend_non_empty_str(row, "current_state_kind"),
        current_normal_state_kind=_rwkv_backend_non_empty_str(
            row,
            "current_normal_state_kind",
        ),
        current_elapsed_days=_rwkv_backend_optional_int(row, "current_elapsed_days"),
        current_elapsed_seconds=_rwkv_backend_optional_int(
            row,
            "current_elapsed_seconds",
        ),
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _rwkv_review_input_from_backend_proto_row(
    row: scheduler_pb2.RwkvReviewInputRowsForCardsResponse.Row,
) -> RwkvReviewInput:
    preset_id = _stable_preset_id(row.preset_id) if row.preset_id else None
    target_retention = (
        row.target_retention
        if _valid_probability(row.target_retention)
        else _RWKV_DEFAULT_TARGET_RETENTION
    )

    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=row.card_id,
            note_id=row.note_id,
            deck_id=row.deck_id,
            preset_id=preset_id,
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=row.card_type,
        card_queue=row.card_queue,
        card_due=row.card_due,
        interval_days=row.interval_days,
        ease_factor=row.ease_factor,
        reps=row.reps,
        lapses=row.lapses,
        day_offset=row.day_offset,
        current_state_kind=row.current_state_kind or None,
        current_normal_state_kind=row.current_normal_state_kind or None,
        current_elapsed_days=(
            row.current_elapsed_days if row.HasField("current_elapsed_days") else None
        ),
        current_elapsed_seconds=(
            row.current_elapsed_seconds
            if row.HasField("current_elapsed_seconds")
            else None
        ),
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _rwkv_backend_preset_id(row: object) -> int | None:
    preset_id = _rwkv_backend_non_empty_str(row, "preset_id")
    return _stable_preset_id(preset_id) if preset_id is not None else None


def _rwkv_backend_row_batch_size(row: object) -> int:
    batch_size = _rwkv_backend_int(row, "batch_size")
    return (
        batch_size
        if batch_size is not None and _valid_rwkv_review_batch_size(batch_size)
        else _DEFAULT_RWKV_REVIEW_BATCH_SIZE
    )


def _rwkv_backend_probability(row: object, name: str, default: float) -> float:
    value = getattr(row, name, None)
    return cast(float, value) if _valid_probability(value) else default


def _rwkv_backend_uint(row: object, name: str) -> int:
    value = _rwkv_backend_int(row, name)
    return value if value is not None and value >= 0 else 0


def _rwkv_backend_int(row: object, name: str) -> int | None:
    value = getattr(row, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rwkv_backend_optional_int(row: object, name: str) -> int | None:
    has_field = getattr(row, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(name):
                return None
        except ValueError:
            pass
    return _rwkv_backend_int(row, name)


def _rwkv_backend_non_empty_str(row: object, name: str) -> str | None:
    value = getattr(row, name, None)
    return value if isinstance(value, str) and value else None


def _stats_graph_cards_for_ids(
    reviewer: object,
    card_ids: Sequence[int],
) -> list[RwkvStatsGraphCard]:
    return _rwkv_cards_for_ids(
        reviewer,
        card_ids,
        reason="stats graph",
        supported_state_filter=True,
        use_enabled_deck_filter=True,
    )


def _rwkv_cards_for_ids(
    reviewer: object,
    card_ids: Sequence[int],
    *,
    reason: str,
    supported_state_filter: bool = False,
    use_enabled_deck_filter: bool = False,
) -> list[RwkvStatsGraphCard]:
    rows = _rwkv_card_rows_for_ids(
        reviewer,
        card_ids,
        reason=reason,
        supported_state_filter=supported_state_filter,
        enabled_deck_ids=(
            _rwkv_enabled_deck_id_filter(reviewer) if use_enabled_deck_filter else None
        ),
    )
    if rows is None:
        return []

    card_order = {card_id: index for index, card_id in enumerate(card_ids)}
    cards = [card for row in rows if (card := _stats_graph_card_from_row(row))]
    missing_review_time_ids = [
        card.id for card in cards if card.last_review_time is None
    ]
    latest_review_times = _latest_eligible_review_times_for_cards(
        reviewer,
        missing_review_time_ids,
        reason=reason,
    )
    if latest_review_times:
        cards = [
            replace(card, last_review_time=latest_review_times[card.id])
            if card.last_review_time is None and card.id in latest_review_times
            else card
            for card in cards
        ]

    return sorted(
        cards,
        key=lambda card: card_order.get(card.id, len(card_order)),
    )


def _rwkv_card_rows_for_ids(
    reviewer: object,
    card_ids: Sequence[int],
    *,
    reason: str,
    supported_state_filter: bool = False,
    enabled_deck_ids: set[int] | None = None,
) -> list[Sequence[object]] | None:
    if not card_ids:
        return []
    if enabled_deck_ids is not None and not enabled_deck_ids:
        return []

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return []

    try:
        start = time.monotonic()
        logger.debug(
            "RWKV %s card bulk load started: cards=%s",
            reason,
            len(card_ids),
        )
        rows = all_rows(
            f"""
select id, nid, did, odid, type, queue, due, odue, ivl, factor, reps, lapses, data
from cards
where id in {ids2str(card_ids)}
{_rwkv_supported_state_sql_filter() if supported_state_filter else ""}
{_rwkv_enabled_deck_sql_filter(enabled_deck_ids)}
"""
        )
        logger.debug(
            "RWKV %s card bulk load finished: cards=%s rows=%s elapsed_ms=%.1f",
            reason,
            len(card_ids),
            len(rows),
            (time.monotonic() - start) * 1000,
        )
    except Exception:
        logger.debug("failed to bulk-load cards for RWKV %s", reason)
        return None

    return cast(list[Sequence[object]], rows)


def _rwkv_supported_state_sql_filter() -> str:
    return f"""
  and (
    (type = {int(CARD_TYPE_REV)} and queue in ({int(QUEUE_TYPE_REV)}, {int(QUEUE_TYPE_SUSPENDED)}))
    or (type = {int(CARD_TYPE_LRN)} and queue in ({int(QUEUE_TYPE_LRN)}, {int(QUEUE_TYPE_DAY_LEARN_RELEARN)}))
    or (type = {int(CARD_TYPE_RELEARNING)} and queue in ({int(QUEUE_TYPE_LRN)}, {int(QUEUE_TYPE_DAY_LEARN_RELEARN)}))
  )
"""


def _rwkv_enabled_deck_sql_filter(enabled_deck_ids: set[int] | None) -> str:
    if enabled_deck_ids is None:
        return ""

    return (
        "\n  and (case when odid != 0 then odid else did end) "
        f"in {ids2str(sorted(enabled_deck_ids))}"
    )


def _rwkv_enabled_deck_id_filter(reviewer: object) -> set[int] | None:
    all_deck_ids = _all_deck_ids(reviewer)
    if all_deck_ids is None:
        return None

    enabled_deck_ids = {
        deck_id for deck_id in all_deck_ids if _rwkv_deck_id_enabled(reviewer, deck_id)
    }
    if len(enabled_deck_ids) == len(all_deck_ids):
        return None

    return enabled_deck_ids


def _all_deck_ids(reviewer: object) -> set[int] | None:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_names_and_ids = getattr(decks, "all_names_and_ids", None)
    if callable(all_names_and_ids):
        try:
            values = all_names_and_ids()
        except Exception:
            logger.debug("failed to read deck ids for RWKV deck SQL filter")
        else:
            deck_ids = {
                deck_id
                for value in values
                if isinstance((deck_id := getattr(value, "id", None)), int)
                and not isinstance(deck_id, bool)
            }
            if deck_ids:
                return deck_ids

    all_decks = getattr(decks, "all", None)
    if not callable(all_decks):
        return None

    try:
        values = all_decks()
    except Exception:
        logger.debug("failed to read decks for RWKV deck SQL filter")
        return None

    deck_ids = {
        deck_id
        for value in values
        if isinstance(value, dict)
        and isinstance((deck_id := value.get("id")), int)
        and not isinstance(deck_id, bool)
    }
    return deck_ids if deck_ids else None


def _rwkv_deck_id_enabled(reviewer: object, deck_id: int) -> bool:
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return isinstance(deck_config, dict) and _rwkv_review_config_enabled(deck_config)


def _latest_eligible_review_times_for_cards(
    reviewer: object,
    card_ids: Sequence[int],
    *,
    reason: str,
) -> dict[int, int]:
    if not card_ids:
        return {}

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return {}

    try:
        start = time.monotonic()
        logger.debug(
            "RWKV %s latest eligible revlog load started: cards=%s",
            reason,
            len(card_ids),
        )
        rows = all_rows(
            f"""
select cid, max(id)
from revlog
where cid in {ids2str(card_ids)}
  and ease between 1 and 4
  and type in (0, 1, 2, 3)
group by cid
"""
        )
        logger.debug(
            "RWKV %s latest eligible revlog load finished: cards=%s rows=%s "
            "elapsed_ms=%.1f",
            reason,
            len(card_ids),
            len(rows),
            (time.monotonic() - start) * 1000,
        )
    except Exception:
        logger.debug("failed to load latest eligible revlogs for RWKV %s", reason)
        return {}

    review_times: dict[int, int] = {}
    for row in rows:
        if len(row) != 2:
            continue
        card_id, revlog_id = row
        if (
            isinstance(card_id, int)
            and not isinstance(card_id, bool)
            and isinstance(revlog_id, int)
            and not isinstance(revlog_id, bool)
        ):
            review_times[card_id] = max(0, revlog_id // 1000)

    return review_times


def _stats_graph_card_fields_from_row(
    row: Sequence[object],
) -> RwkvStatsGraphCardFields | None:
    if len(row) != 13:
        return None

    (
        card_id,
        note_id,
        deck_id,
        original_deck_id,
        card_type,
        queue,
        due,
        original_due,
        interval_days,
        ease_factor,
        reps,
        lapses,
        data,
    ) = row
    int_values = (
        card_id,
        note_id,
        deck_id,
        original_deck_id,
        card_type,
        queue,
        due,
        original_due,
        interval_days,
        ease_factor,
        reps,
        lapses,
    )
    if not all(isinstance(value, int) for value in int_values):
        return None

    return RwkvStatsGraphCardFields(
        id=cast(int, card_id),
        nid=cast(int, note_id),
        did=cast(int, deck_id),
        odid=cast(int, original_deck_id),
        type=cast(int, card_type),
        queue=cast(int, queue),
        due=cast(int, due),
        odue=cast(int, original_due),
        ivl=cast(int, interval_days),
        factor=cast(int, ease_factor),
        reps=cast(int, reps),
        lapses=cast(int, lapses),
        last_review_time=_stats_graph_last_review_time(data),
    )


def _stats_graph_card_from_row(row: Sequence[object]) -> RwkvStatsGraphCard | None:
    fields = _stats_graph_card_fields_from_row(row)
    if fields is None:
        return None

    return RwkvStatsGraphCard(
        id=fields.id,
        nid=fields.nid,
        did=fields.did,
        odid=fields.odid,
        type=fields.type,
        queue=fields.queue,
        due=fields.due,
        odue=fields.odue,
        ivl=fields.ivl,
        factor=fields.factor,
        reps=fields.reps,
        lapses=fields.lapses,
        last_review_time=fields.last_review_time,
    )


def _stats_graph_last_review_time(data: object) -> int | None:
    if not isinstance(data, str) or not data:
        return None

    try:
        value = json.loads(data).get("lrt")
    except (AttributeError, json.JSONDecodeError, TypeError):
        return None

    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stats_graph_scheduling_states(
    card: RwkvStatsGraphCard,
    timing: object,
    *,
    include_suspended_review: bool = False,
) -> SchedulingStates | None:
    states = SchedulingStates()

    if card.type == int(CARD_TYPE_REV) and card.queue in (
        int(QUEUE_TYPE_REV),
        int(QUEUE_TYPE_SUSPENDED),
    ):
        if card.queue == int(QUEUE_TYPE_SUSPENDED) and not include_suspended_review:
            return None
        elapsed_days = _stats_graph_elapsed_days(card, timing)
        if elapsed_days is None:
            return states
        review = states.current.normal.review
        review.scheduled_days = max(0, card.ivl)
        review.elapsed_days = elapsed_days
        review.ease_factor = card.factor / 1000
        review.lapses = max(0, card.lapses)
        return states

    if card.type == int(CARD_TYPE_LRN) and card.queue in (
        int(QUEUE_TYPE_LRN),
        int(QUEUE_TYPE_DAY_LEARN_RELEARN),
    ):
        elapsed_seconds = _stats_graph_elapsed_seconds(card, timing)
        if elapsed_seconds is None:
            return states
        learning = states.current.normal.learning
        learning.elapsed_secs = elapsed_seconds
        return states

    if card.type == int(CARD_TYPE_RELEARNING) and card.queue in (
        int(QUEUE_TYPE_LRN),
        int(QUEUE_TYPE_DAY_LEARN_RELEARN),
    ):
        elapsed_days = _stats_graph_elapsed_days(card, timing)
        elapsed_seconds = _stats_graph_elapsed_seconds(card, timing)
        if elapsed_days is None or elapsed_seconds is None:
            return states
        relearning = states.current.normal.relearning
        relearning.review.scheduled_days = max(0, card.ivl)
        relearning.review.elapsed_days = elapsed_days
        relearning.review.ease_factor = card.factor / 1000
        relearning.review.lapses = max(0, card.lapses)
        relearning.learning.elapsed_secs = elapsed_seconds
        return states

    return None


def _rwkv_review_input_for_stats_graph_card(
    *,
    card: RwkvStatsGraphCard,
    deck_config: dict[str, object],
    timing: object,
    resolved_preset_id: str | None = None,
    include_suspended_review: bool = False,
    state_fields: tuple[object, str | None, int | None, int | None] | None = None,
) -> RwkvReviewInput | None:
    state_kind, normal_state_kind, elapsed_days, elapsed_seconds = (
        state_fields
        or _rwkv_state_fields_for_stats_graph_card(
            card,
            timing,
            include_suspended_review=include_suspended_review,
        )
    )
    if state_kind is _UNSUPPORTED_RWKV_STATE:
        return None

    deck_id = card.current_deck_id()
    target_retention = _rwkv_target_retention_for_deck_config(deck_config)
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card.id,
            note_id=card.nid,
            deck_id=deck_id,
            preset_id=_rwkv_preset_id_for_stats_graph_card(
                deck_config,
                resolved_preset_id,
            ),
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=card.type,
        card_queue=card.queue,
        card_due=card.due,
        interval_days=card.ivl,
        ease_factor=card.factor,
        reps=card.reps,
        lapses=card.lapses,
        day_offset=_day_offset_from_timing(timing),
        current_state_kind=cast(str | None, state_kind),
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _rwkv_review_input_for_stats_graph_fields(
    *,
    fields: RwkvStatsGraphCardFields,
    deck_config: dict[str, object],
    timing: object,
    resolved_preset_id: str | None = None,
    state_fields: tuple[object, str | None, int | None, int | None] | None = None,
) -> RwkvReviewInput | None:
    state_kind, normal_state_kind, elapsed_days, elapsed_seconds = (
        state_fields
        or _rwkv_state_fields_for_stats_graph_fields(
            fields,
            timing,
            include_suspended_review=True,
        )
    )
    if state_kind is _UNSUPPORTED_RWKV_STATE:
        return None

    deck_id = fields.current_deck_id()
    target_retention = _rwkv_target_retention_for_deck_config(deck_config)
    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=fields.id,
            note_id=fields.nid,
            deck_id=deck_id,
            preset_id=_rwkv_preset_id_for_stats_graph_card(
                deck_config,
                resolved_preset_id,
            ),
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=fields.type,
        card_queue=fields.queue,
        card_due=fields.due,
        interval_days=fields.ivl,
        ease_factor=fields.factor,
        reps=fields.reps,
        lapses=fields.lapses,
        day_offset=_day_offset_from_timing(timing),
        current_state_kind=cast(str | None, state_kind),
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


_UNSUPPORTED_RWKV_STATE = object()


def _rwkv_state_fields_for_stats_graph_card(
    card: RwkvStatsGraphCard,
    timing: object,
    *,
    include_suspended_review: bool,
) -> tuple[object, str | None, int | None, int | None]:
    return _rwkv_state_fields_for_stats_graph_values(
        card_type=card.type,
        queue=card.queue,
        last_review_time=card.last_review_time,
        timing=timing,
        include_suspended_review=include_suspended_review,
    )


def _rwkv_state_fields_for_stats_graph_fields(
    fields: RwkvStatsGraphCardFields,
    timing: object,
    *,
    include_suspended_review: bool,
) -> tuple[object, str | None, int | None, int | None]:
    return _rwkv_state_fields_for_stats_graph_values(
        card_type=fields.type,
        queue=fields.queue,
        last_review_time=fields.last_review_time,
        timing=timing,
        include_suspended_review=include_suspended_review,
    )


def _rwkv_state_fields_for_stats_graph_values(
    *,
    card_type: int,
    queue: int,
    last_review_time: int | None,
    timing: object,
    include_suspended_review: bool,
) -> tuple[object, str | None, int | None, int | None]:
    if card_type == int(CARD_TYPE_REV) and queue in (
        int(QUEUE_TYPE_REV),
        int(QUEUE_TYPE_SUSPENDED),
    ):
        if queue == int(QUEUE_TYPE_SUSPENDED) and not include_suspended_review:
            return _UNSUPPORTED_RWKV_STATE, None, None, None
        elapsed_days = _stats_graph_elapsed_days_for_review_time(
            last_review_time,
            timing,
        )
        if elapsed_days is None:
            return None, None, None, None
        return "normal", "review", elapsed_days, None

    if card_type == int(CARD_TYPE_LRN) and queue in (
        int(QUEUE_TYPE_LRN),
        int(QUEUE_TYPE_DAY_LEARN_RELEARN),
    ):
        elapsed_seconds = _stats_graph_elapsed_seconds_for_review_time(last_review_time)
        if elapsed_seconds is None:
            return None, None, None, None
        return "normal", "learning", None, elapsed_seconds

    if card_type == int(CARD_TYPE_RELEARNING) and queue in (
        int(QUEUE_TYPE_LRN),
        int(QUEUE_TYPE_DAY_LEARN_RELEARN),
    ):
        elapsed_days = _stats_graph_elapsed_days_for_review_time(
            last_review_time,
            timing,
        )
        elapsed_seconds = _stats_graph_elapsed_seconds_for_review_time(last_review_time)
        if elapsed_days is None or elapsed_seconds is None:
            return None, None, None, None
        return "normal", "relearning", elapsed_days, elapsed_seconds

    return _UNSUPPORTED_RWKV_STATE, None, None, None


def _rwkv_target_retention_for_deck_config(deck_config: dict[str, object]) -> float:
    value = deck_config.get("desiredRetention", deck_config.get("desired_retention"))
    return (
        cast(float, value)
        if _valid_probability(value)
        else _RWKV_DEFAULT_TARGET_RETENTION
    )


def _rwkv_preset_id_for_stats_graph_card(
    deck_config: dict[str, object],
    resolved_preset_id: str | None,
) -> int | None:
    if resolved_preset_id is not None:
        return _stable_preset_id(resolved_preset_id)

    value = deck_config.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _day_offset_from_timing(timing: object) -> int | None:
    days_elapsed = getattr(timing, "days_elapsed", None)
    return days_elapsed if isinstance(days_elapsed, int) else None


def _stats_graph_elapsed_days(card: RwkvStatsGraphCard, timing: object) -> int | None:
    return _stats_graph_elapsed_days_for_review_time(card.last_review_time, timing)


def _stats_graph_elapsed_days_for_review_time(
    last_review_time: int | None,
    timing: object,
) -> int | None:
    next_day_at = getattr(timing, "next_day_at", None)
    if isinstance(last_review_time, int) and isinstance(next_day_at, int):
        return max(0, next_day_at - last_review_time) // 86_400

    return None


def _stats_graph_elapsed_seconds(
    card: RwkvStatsGraphCard, timing: object
) -> int | None:
    return _stats_graph_elapsed_seconds_for_review_time(card.last_review_time)


def _stats_graph_elapsed_seconds_for_review_time(
    last_review_time: int | None,
) -> int | None:
    if isinstance(last_review_time, int):
        now = int(time.time())
        return max(0, now - last_review_time)

    return None


def _stats_graph_reviewer_context(
    *,
    deck_config: dict[str, object],
    states: SchedulingStates,
    timing: object,
    resolved_preset_id: str | None = None,
) -> object:
    return SimpleNamespace(
        _rwkv_resolved_preset_id=resolved_preset_id,
        _v3=SimpleNamespace(states=states),
        mw=SimpleNamespace(
            col=SimpleNamespace(
                decks=SimpleNamespace(
                    config_dict_for_deck_id=lambda deck_id: deck_config
                ),
                sched=SimpleNamespace(_timing_today=lambda: timing),
            )
        ),
    )


def _stats_graph_card_ids(reviewer: object, search: str) -> list[int]:
    col = _collection(reviewer)
    find_cards = getattr(col, "find_cards", None)
    if not callable(find_cards):
        return []

    try:
        start = time.monotonic()
        logger.debug("RWKV stats card search started: search=%r", search)
        card_ids = [
            int(card_id)
            for card_id in find_cards(search, order=False)
            if isinstance(card_id, int)
        ]
        logger.debug(
            "RWKV stats card search finished: search=%r cards=%s elapsed_ms=%.1f",
            search,
            len(card_ids),
            (time.monotonic() - start) * 1000,
        )
        return card_ids
    except Exception:
        logger.debug("failed to search cards for RWKV stats graph")
        return []


def _predict_review_batch(
    candidates: Sequence[RwkvReviewCandidate],
) -> Sequence[RwkvReviewPrediction | None]:
    backend = _reviewer_backend
    if backend is None:
        return []

    predict_reviews = getattr(backend, "predict_reviews", None)
    if callable(predict_reviews):
        start = time.monotonic()
        predictions = predict_reviews(candidates)
        logger.debug(
            "RWKV review batch predicted: size=%s backend=%s path=batch "
            "elapsed_ms=%.1f",
            len(candidates),
            type(backend).__name__,
            (time.monotonic() - start) * 1000,
        )
        return predictions

    start = time.monotonic()
    predictions = [
        backend.predict_review(reviewer=candidate.reviewer, card=candidate.card)
        for candidate in candidates
    ]
    logger.debug(
        "RWKV review batch predicted: size=%s backend=%s path=per-card elapsed_ms=%.1f",
        len(candidates),
        type(backend).__name__,
        (time.monotonic() - start) * 1000,
    )
    return predictions


def _card_for_id(reviewer: object, card_id: int) -> object | None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    get_card = getattr(col, "get_card", None)
    if not callable(get_card):
        return None

    try:
        return get_card(card_id)
    except Exception:
        logger.debug("failed to load card for RWKV queue ordering: card_id=%s", card_id)
        return None


def _set_rwkv_review_queue_scores(
    reviewer: object,
    deck_id: int,
    scores: Sequence[tuple[int, float]],
    *,
    fresh_for_backend_state: bool = True,
) -> None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    set_scores = getattr(backend, "set_rwkv_review_queue_scores", None)
    if not callable(set_scores):
        return

    set_scores(
        deck_id=deck_id,
        scores=[
            scheduler_pb2.RwkvReviewQueueScoresRequest.Score(
                card_id=card_id,
                retrievability=retrievability,
            )
            for card_id, retrievability in scores
        ],
    )
    if scores:
        _rwkv_review_queue_score_maps[deck_id] = {
            card_id: retrievability for card_id, retrievability in scores
        }
        if fresh_for_backend_state:
            _rwkv_review_queue_score_generations[deck_id] = (
                _reviewer_backend_state_generation()
            )
        else:
            _rwkv_review_queue_score_generations.pop(deck_id, None)
    else:
        _rwkv_review_queue_score_maps.pop(deck_id, None)
        _rwkv_review_queue_score_generations.pop(deck_id, None)


def _set_rwkv_stats_graph_scores(
    reviewer: object,
    search: str,
    scores: Sequence[tuple[int, float]],
) -> None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    set_scores = getattr(backend, "set_rwkv_stats_graph_scores", None)
    if not callable(set_scores):
        return

    set_scores(
        search=search,
        scores=[
            scheduler_pb2.RwkvStatsGraphScoresRequest.Score(
                card_id=card_id,
                retrievability=retrievability,
            )
            for card_id, retrievability in scores
        ],
    )


def _set_rwkv_card_info_score(
    reviewer: object,
    card_id: int,
    retrievability: float | None,
) -> None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    set_score = getattr(backend, "set_rwkv_card_info_score", None)
    if not callable(set_score):
        return

    request = scheduler_pb2.RwkvCardInfoScoreRequest(card_id=card_id)
    if retrievability is not None:
        request.retrievability = retrievability
    set_score(request)


def _active_rwkv_retrievability_score(
    reviewer: object,
    card_id: int,
) -> float | None:
    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    get_score_raw = getattr(backend, "get_rwkv_retrievability_score_raw", None)
    get_score = getattr(backend, "get_rwkv_retrievability_score", None)
    if not callable(get_score_raw) and not callable(get_score):
        return None

    try:
        if callable(get_score_raw):
            request = cards_pb2.CardId(cid=card_id)
            response = scheduler_pb2.RwkvRetrievabilityScoreResponse()
            response.ParseFromString(get_score_raw(request.SerializeToString()))
        else:
            response = get_score(card_id)
    except Exception:
        logger.debug(
            "failed to read active RWKV retrievability score: card_id=%s",
            card_id,
        )
        return None

    if isinstance(response, float):
        return response if math.isfinite(response) and 0 <= response <= 1 else None

    has_field = getattr(response, "HasField", None)
    if callable(has_field) and not has_field("retrievability"):
        return None

    retrievability = getattr(response, "retrievability", None)
    if not isinstance(retrievability, float) or not math.isfinite(retrievability):
        return None
    if not 0 <= retrievability <= 1:
        return None

    return retrievability


def _clear_rwkv_review_queue_scores(
    reviewer: object,
    deck_id: int | None = None,
) -> None:
    if deck_id is None:
        deck_id = _current_deck_id(reviewer) or 0
    _set_rwkv_review_queue_scores(reviewer, deck_id, [])


def _duration_millis(card: object, ease: int | None) -> int | None:
    if ease is None:
        return None

    time_taken = getattr(card, "time_taken", None)
    if not callable(time_taken):
        return None

    try:
        value = time_taken(capped=False)
    except TypeError:
        value = time_taken(False)
    except Exception:
        logger.debug("failed to read answer duration for RWKV review input")
        return None

    return value if isinstance(value, int) else None


def _day_offset(reviewer: object) -> int | None:
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    return days_elapsed if isinstance(days_elapsed, int) else None


def _timing_today(reviewer: object) -> object | None:
    col = _collection(reviewer)
    sched = getattr(col, "sched", None)
    timing_today = getattr(sched, "_timing_today", None)
    if not callable(timing_today):
        return None

    try:
        return timing_today()
    except Exception:
        logger.debug("failed to read scheduler timing for RWKV review input")
        return None


def _current_scheduling_state(reviewer: object) -> SchedulingState | None:
    states = _scheduling_states(reviewer)
    current = getattr(states, "current", None)
    return current if isinstance(current, SchedulingState) else None


def _scheduling_states(reviewer: object) -> SchedulingStates | None:
    v3 = getattr(reviewer, "_v3", None)
    states = getattr(v3, "states", None)
    return states if isinstance(states, SchedulingStates) else None


def _scheduling_state_kinds(
    state: SchedulingState | None,
) -> tuple[str | None, str | None]:
    if state is None:
        return None, None

    state_kind = state.WhichOneof("kind")
    normal_state_kind = (
        state.normal.WhichOneof("kind") if state_kind == "normal" else None
    )
    return state_kind, normal_state_kind


def _scheduling_state_elapsed(
    state: SchedulingState | None,
) -> tuple[int | None, int | None]:
    if state is None or state.WhichOneof("kind") != "normal":
        return None, None

    normal_kind = state.normal.WhichOneof("kind")
    if normal_kind == "review":
        return state.normal.review.elapsed_days, None
    if normal_kind == "learning":
        return None, state.normal.learning.elapsed_secs
    if normal_kind == "relearning":
        return (
            state.normal.relearning.review.elapsed_days,
            state.normal.relearning.learning.elapsed_secs,
        )

    return None, None


def _int_attr(instance: object, attr: str) -> int | None:
    value = getattr(instance, attr, None)
    return value if isinstance(value, int) else None


def _entity_state(states: dict[int, object | None], key: int | None) -> object | None:
    return states.get(key) if key is not None else None


def _set_entity_state(
    states: dict[int, object | None],
    key: int | None,
    state: object | None,
) -> None:
    if key is not None:
        states[key] = state


def _has_interval_overrides(overrides: RwkvIntervalOverride) -> bool:
    return all(
        interval is not None
        for interval in (
            overrides.again,
            overrides.hard,
            overrides.good,
            overrides.easy,
        )
    )


def _validate_recall_points(points: Sequence[RwkvRecallPoint]) -> None:
    previous_elapsed_days: float | None = None

    for point in points:
        if not math.isfinite(point.elapsed_days) or point.elapsed_days < 0:
            raise ValueError("elapsed_days must be finite and non-negative")
        if not _valid_probability(point.retrievability):
            raise ValueError("retrievability must be between 0 and 1")
        if (
            previous_elapsed_days is not None
            and point.elapsed_days <= previous_elapsed_days
        ):
            raise ValueError("elapsed_days must be unique")
        previous_elapsed_days = point.elapsed_days


def _recall_curve_is_monotonic(
    points: Sequence[RwkvRecallPoint],
    *,
    tolerance: float,
) -> bool:
    previous = points[0]
    for point in points[1:]:
        if point.retrievability > previous.retrievability + tolerance:
            return False
        previous = point

    return True


def _valid_probability(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _interpolated_elapsed_days(
    previous: RwkvRecallPoint,
    point: RwkvRecallPoint,
    target_retention: float,
) -> float:
    recall_delta = previous.retrievability - point.retrievability
    if recall_delta <= 0:
        return point.elapsed_days

    elapsed_delta = point.elapsed_days - previous.elapsed_days
    target_fraction = (previous.retrievability - target_retention) / recall_delta
    return previous.elapsed_days + elapsed_delta * target_fraction


def _clamped_interval(elapsed_days: float, max_interval_days: int) -> int:
    return min(max(1, math.ceil(elapsed_days)), max_interval_days)


def _validated_interval(interval: int) -> int:
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("interval overrides must be positive day counts")
    return interval


def _chunks(items: Sequence[_T], size: int) -> Iterator[Sequence[_T]]:
    if size < 1:
        raise ValueError("chunk size must be positive")

    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _set_review_interval_if_present(
    state: SchedulingState,
    interval: int,
) -> None:
    if state.WhichOneof("kind") != "normal":
        return
    normal_kind = state.normal.WhichOneof("kind")
    if normal_kind == "review":
        state.normal.review.scheduled_days = interval
        state.normal.review.fuzz_delta_days = 0
    elif normal_kind == "relearning":
        state.normal.relearning.review.scheduled_days = interval
        state.normal.relearning.review.fuzz_delta_days = 0


def _set_review_s90_if_present(
    state: SchedulingState,
    s90: int,
) -> None:
    review = _review_state_for_interval_override(state)
    if review is None:
        return

    memory_state = review.memory_state
    if memory_state.difficulty <= 0:
        memory_state.difficulty = 5.0
    memory_state.stability = float(s90)


def _review_state_for_interval_override(state: SchedulingState) -> Any | None:
    if state.WhichOneof("kind") != "normal":
        return None
    normal_kind = state.normal.WhichOneof("kind")
    if normal_kind == "review":
        return state.normal.review
    if normal_kind == "relearning":
        return state.normal.relearning.review
    return None
