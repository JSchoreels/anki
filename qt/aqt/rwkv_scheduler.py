# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import base64
import bisect
import enum
import gzip
import hashlib
import importlib
import inspect
import json
import logging
import math
import mmap
import os
import re
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, Protocol, TypedDict, TypeVar, cast

from typing_extensions import NotRequired

from anki import collection_pb2, deck_config_pb2, scheduler_pb2
from anki.consts import (
    CARD_TYPE_LRN,
    CARD_TYPE_NEW,
    CARD_TYPE_RELEARNING,
    CARD_TYPE_REV,
    QUEUE_TYPE_DAY_LEARN_RELEARN,
    QUEUE_TYPE_LRN,
    QUEUE_TYPE_NEW,
    QUEUE_TYPE_REV,
    QUEUE_TYPE_SUSPENDED,
)
from anki.decks import DeckTreeNode, FilteredDeckConfig
from anki.scheduler.v3 import SchedulingState, SchedulingStates
from anki.utils import ids2str
from aqt.qt import QMessageBox, QWidget

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class RwkvStatsPreparationStatus(enum.Enum):
    READY = "ready"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class RwkvFirstReviewElapsedSource(enum.Enum):
    DECK_CONFIG = "deck_config"
    MISSING = "missing"
    CARD_CREATION = "card_creation"


class RwkvReviewState(enum.IntEnum):
    """State values stored in the training dataset before `scaled_state`."""

    LEARN_START = 0
    LEARNING = 1
    REVIEW = 2
    RELEARNING = 3
    FILTERED = 4
    MANUAL = 5
    RESCHEDULED = 6


class _RwkvCollectionConfigState(NamedTuple):
    review_enabled: bool
    dynamic_preset_replay_enabled: bool


class _RwkvStateCacheCheckpointEntry(TypedDict):
    lastReviewId: int
    reviewCount: int
    historyHash: str
    segmentId: NotRequired[int]


class _RwkvHistoryPrefixIdentity(NamedTuple):
    last_review_id: int
    review_count: int
    history_hash: str


_REVIEWER_PREDICTION_ATTR = "_rwkv_review_prediction"
_REVIEWER_PENDING_ANSWER_STATE_ATTR = "_rwkv_pending_answer_state"
_REVIEWER_SYNTHETIC_ANSWER_STATES_ATTR = "_rwkv_synthetic_answer_states"
_REVIEW_ORDER_RETRIEVABILITY_DESCENDING = (
    deck_config_pb2.DeckConfig.Config.REVIEW_CARD_ORDER_RETRIEVABILITY_DESCENDING
)
_REVIEW_ORDER_RELATIVE_OVERDUENESS = (
    deck_config_pb2.DeckConfig.Config.REVIEW_CARD_ORDER_RELATIVE_OVERDUENESS
)
_NEW_GATHER_PRIORITY_DESCENDING_RETRIEVABILITY = getattr(
    deck_config_pb2.DeckConfig.Config,
    "NEW_CARD_GATHER_PRIORITY_DESCENDING_RETRIEVABILITY",
    6,
)
_NEW_GATHER_PRIORITY_ASCENDING_RETRIEVABILITY = getattr(
    deck_config_pb2.DeckConfig.Config,
    "NEW_CARD_GATHER_PRIORITY_ASCENDING_RETRIEVABILITY",
    7,
)
_DEFAULT_RWKV_REVIEW_BATCH_SIZE = 512
_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE = 128
_DEFAULT_RWKV_REVIEW_REFRESH_INTERVAL = 1
_DEFAULT_RWKV_REVIEW_MIN_INTERVENING_REVIEWS = 5
_DEFAULT_RWKV_REVIEW_FIRST_REVIEW_ELAPSED_FROM_CARD_CREATION = True
_MIN_RWKV_REVIEW_BATCH_SIZE = 64
_MAX_RWKV_REVIEW_BATCH_SIZE = 8192
_AUTO_RWKV_RETRIEVABILITY_BATCH_SIZE = 2048
_MIN_RWKV_REVIEW_REFRESH_INTERVAL = 1
_MAX_RWKV_REVIEW_REFRESH_INTERVAL = 10_000
_RWKV_REVIEW_PREDICTION_CACHE_LIMIT = 32768
_RWKV_REVIEW_INPUT_BATCH_CACHE_ATTR = "_rwkv_review_input_batch_cache"
_RWKV_REVIEW_INPUT_BATCH_CACHE_LIMIT = 4
_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE = "search_stats_rwkv_review_retrievability"
_RWKV_REVIEW_UNDO_LIMIT = 30
_RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS = 120.0
_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS = 0.05
_RWKV_INSTANT_R_SEARCH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])prop:rwkv:r(?=[<>=!])",
    re.IGNORECASE,
)
_RWKV_CURVE_R_SEARCH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])prop:rwkv-curve:r(?=[<>=!])",
    re.IGNORECASE,
)
_RWKV_INSTANT_DUE_SEARCH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:-])-?is:rwkv:due(?![A-Za-z0-9_:-])",
    re.IGNORECASE,
)
_RWKV_CURVE_DUE_SEARCH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:-])-?is:rwkv-curve:due(?![A-Za-z0-9_:-])",
    re.IGNORECASE,
)
_FILTERED_DECK_RETRIEVABILITY_ORDERS = frozenset(
    (
        FilteredDeckConfig.SearchTerm.RETRIEVABILITY_ASCENDING,
        FilteredDeckConfig.SearchTerm.RETRIEVABILITY_DESCENDING,
    )
)
_RWKV_WORKLOAD_MIN_DR = 30
_RWKV_WORKLOAD_MAX_DR = 99
_RWKV_SIMULATOR_BUCKET_COUNT = 20
_RWKV_SIMULATOR_PRIOR_WEIGHT = 4.0
_RWKV_SIMULATOR_DEFAULT_GRADE_SECONDS = (8.0, 8.0, 8.0, 8.0)
_RWKV_SIMULATOR_MAX_TAKEN_MILLIS = 600_000
_RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT = "final_fit"
_RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD = "test_fold"
_RWKV_RETRIEVABILITY_SAMPLE_ROLE_POST_OPTIMIZATION = "post_optimization"
_RWKV_CALIBRATION_METRIC_EPSILON = 1e-6
_RWKV_CALIBRATION_TRAIN_FRACTION = 0.70
_EMBEDDED_RWKV_MODEL_FILENAME = "RWKV_trained_on_5000_10000.bin"
_RWKV_MODEL_KEY_HASH_CHUNK_SIZE = 1024 * 1024
_RWKV_STATE_CACHE_VERSION = 12
_RWKV_STATE_CACHE_LEGACY_JSON_VERSION = 2
_RWKV_PRESET_REPLAY_SEMANTICS_VERSION = 2
_RWKV_STATE_CACHE_DIR = "rwkv-state-cache"
_RWKV_STATE_CACHE_DATA_FILE = "state-v1.json.gz"
_RWKV_STATE_CACHE_LEGACY_DATA_FILES = (
    "state-v1.json",
    _RWKV_STATE_CACHE_DATA_FILE,
)
_RWKV_STATE_CACHE_SNAPSHOT_FILE = "snapshot-v1.bin"
_RWKV_STATE_CACHE_STORE_FILE = "state-v12.sqlite3"
_RWKV_STATE_CACHE_STORE_TEMP_FILE = ".state-v12-building.sqlite3"
_RWKV_STATE_CACHE_STORE_KIND = "delta-sqlite-v1"
_RWKV_STATE_CACHE_STORE_SCHEMA_VERSION = 4
_RWKV_STATE_CACHE_DELTAS_FILE = "deltas-v1.log"
_RWKV_STATE_CACHE_META_FILE = "state-v1.meta.json"
_RWKV_STATE_CACHE_CHECKPOINT_PREFIX = "checkpoint-v1-"
_RWKV_STATE_CACHE_CHECKPOINT_SUFFIX = ".bin"
_RWKV_STATE_CACHE_SNAPSHOT_MAGIC = b"ARWKVSNAPSHOT12\0"
_RWKV_STATE_CACHE_DELTAS_MAGIC = b"ARWKVDELTAS12\0"
_RWKV_STATE_CACHE_DELTA_WRITE_BUFFER_SIZE = 1024 * 1024
_RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS = 8 * 86_400_000
_RWKV_STATE_CACHE_IGNORED_REVIEW_IDS_KEY = "ignoredReviewIds"
_RWKV_STATE_CACHE_COLLECTION_MOD_KEY = "collectionMod"
_RWKV_STATE_CACHE_HISTORY_HASH_DOMAIN = b"anki-rwkv-state-cache-history-v1\0"
_RWKV_STATE_CACHE_EMPTY_HISTORY_HASH = hashlib.sha256(
    _RWKV_STATE_CACHE_HISTORY_HASH_DOMAIN
).hexdigest()
_FSRS_PRESET_OVERLAY_CONFIG_KEY = "fsrsPresetOverlay"
_RWKV_DEFAULT_TARGET_RETENTION = 0.9
_RWKV_RATING_FIELDS = ("again", "hard", "good", "easy")
_RWKV_AFTER_REVIEW_HORIZONS = (
    ("RWKV : R After Review", 0),
    ("RWKV : R After 10min", 600),
)
_RWKV_MEMORISED_CHECKPOINT_INTERVAL_SECONDS = 30.0


def _rwkv_historical_answer_sql_condition(alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}ease between 1 and 4 "
        f"and {prefix}type in (0, 1, 2, 3, 4, 5) "
        f"and not ({prefix}type = 3 and {prefix}factor = 0)"
    )


_reviewer_backend_state_lock = threading.RLock()
_reviewer_backend_execution_lock = threading.RLock()
_reviewer_backend_prediction_local = threading.local()
_reviewer_backend: RwkvReviewerBackend | None = None
_reviewer_backend_assignment_generation = 0
_reviewer_backend_warmup_states: dict[
    tuple[int, int],
    RwkvResidentStateIdentity | None,
] = {}
_reviewer_backend_warmup_generations: dict[tuple[int, int], int] = {}
_reviewer_backend_warmup_pending_generations: dict[tuple[int, int], int] = {}
_rwkv_memorised_history_identity_cache: dict[
    tuple[int, int],
    tuple[int, RwkvResidentStateIdentity],
] = {}
_resolved_preset_id_cache: dict[tuple[int, str | None], dict[int, str]] = {}
_rwkv_review_queue_score_maps: dict[int, dict[int, float]] = {}
_rwkv_review_queue_target_maps: dict[int, dict[int, float]] = {}
_rwkv_review_queue_score_generations: dict[int, int] = {}
_rwkv_review_queue_score_config_keys: dict[int, RwkvReviewQueueScoreConfigKey] = {}
_rwkv_review_queue_collection_key: RwkvReviewQueueCollectionKey | None = None
_dynamic_desired_retention_generation = 0
_rwkv_study_queue_generation = 0
_RWKV_REVIEW_UNDO_CARD_IDS_ATTR = "_rwkv_review_undo_card_ids"
_RWKV_REVIEW_UNDO_QUEUE_CHANGE_PENDING_ATTR = "_rwkv_review_undo_queue_change_pending"
_rwkv_stats_prepare_lock = threading.Lock()
_rwkv_stats_prepare_in_flight: dict[
    RwkvStatsPrepareKey,
    Future[RwkvStatsPreparationStatus],
] = {}
_rwkv_score_prewarm_lock = threading.Lock()
_rwkv_score_prewarm_in_flight: set[RwkvScorePrewarmKey] = set()
_rwkv_startup_prompt_shown = False
_rwkv_model_cache_lock = threading.Lock()
_rwkv_model_cache_signature: tuple[str, str, int, int, int, int, int] | None = None
_rwkv_model_cache_value: dict[str, object] | None = None


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


RwkvButtonProbabilities = tuple[float, float, float, float]


@dataclass(frozen=True)
class RwkvReviewPrediction:
    retrievability: float | None = None
    curve_retrievability: float | None = None
    current_interval: int | None = None
    current_s90: int | None = None
    interval_overrides: RwkvIntervalOverride = RwkvIntervalOverride()
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride()
    button_probabilities: RwkvButtonProbabilities | None = None


@dataclass(frozen=True)
class RwkvReviewerPrediction:
    card_id: int
    retrievability: float | None
    review_enabled: bool = False
    interval_override_used: bool = False
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride()
    button_probabilities: RwkvButtonProbabilities | None = None


@dataclass(frozen=True)
class RwkvReviewerDiagnostics:
    retrievability: float | None
    retrievability_source: str
    button_probabilities: RwkvButtonProbabilities | None = None
    s90_overrides: RwkvIntervalOverride = RwkvIntervalOverride()


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
    # Legacy field name: answered inputs carry RwkvReviewState, not Anki CardType.
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
    enforce_grade_order: bool = True


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
    dynamic_desired_retentions_resolved: bool = False
    session_answered_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RwkvReviewQueueScoreResult:
    scores: list[tuple[int, float]]
    target_retentions_by_card_id: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RwkvStatsSearchScoreResult:
    scores: list[tuple[int, float]]
    curve_scores: list[tuple[int, float]]
    input_build: RwkvReviewInputBatchBuild
    target_retentions_by_card_id: dict[int, float] = field(default_factory=dict)
    intervening_reviews_by_card_id: dict[int, int] = field(default_factory=dict)
    curve_due_card_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class RwkvReviewQueueContext:
    collection_key: RwkvReviewQueueCollectionKey
    selected_deck_id: int
    deck_id: int
    deck_scope: tuple[int, ...]
    days_elapsed: int
    next_day_at: int
    config_key: str
    dynamic_desired_retention_generation: int
    study_queue_generation: int


@dataclass(frozen=True)
class RwkvReviewQueuePartialRefresh:
    existing_scores: tuple[tuple[int, float], ...]
    existing_target_retentions: tuple[tuple[int, float], ...] | None
    candidate_card_ids: tuple[int, ...]


@dataclass(frozen=True)
class RwkvReviewQueueOrderAsyncWork:
    context: RwkvReviewQueueContext
    deck_id: int
    reason: str
    batch_size: int
    state_generation: int
    input_build: RwkvReviewInputBatchBuild
    inputs_by_card_id: tuple[tuple[int, RwkvReviewInput], ...]
    predictions: tuple[RwkvReviewPrediction | None, ...]
    requests_by_index: tuple[RwkvReviewPredictionRequestByIndex, ...]
    resident_inputs_by_index: tuple[tuple[int, RwkvReviewInput], ...]
    cache_hits: int
    warmup_elapsed_ms: float
    build_elapsed_ms: float
    existing_scores: tuple[tuple[int, float], ...] | None = None
    existing_target_retentions: tuple[tuple[int, float], ...] | None = None
    candidate_card_ids: tuple[int, ...] = ()
    fresh_for_backend_state: bool = True
    backend: RwkvReviewerBackend | None = None
    backend_assignment_generation: int | None = None
    collection_owner: object | None = None
    collection: object | None = None
    collection_backend: object | None = None
    resident_state_key: tuple[int, int] | None = None
    resident_state_generation: int | None = None


@dataclass(frozen=True)
class RwkvReviewQueueOrderAsyncResult:
    context: RwkvReviewQueueContext
    deck_id: int
    reason: str
    state_generation: int
    scores: tuple[tuple[int, float], ...]
    input_build: RwkvReviewInputBatchBuild
    cache_hits: int
    runtime_requests: int
    warmup_elapsed_ms: float
    build_elapsed_ms: float
    score_elapsed_ms: float
    prediction_cache_entries: tuple[
        tuple[RwkvReviewInput, RwkvReviewPrediction | None], ...
    ] = ()
    target_retentions_by_card_id: dict[int, float] = field(default_factory=dict)
    existing_scores: tuple[tuple[int, float], ...] | None = None
    existing_target_retentions: tuple[tuple[int, float], ...] | None = None
    candidate_card_ids: tuple[int, ...] = ()
    fresh_for_backend_state: bool = True
    backend: RwkvReviewerBackend | None = None
    backend_assignment_generation: int | None = None
    collection_owner: object | None = None
    collection: object | None = None
    collection_backend: object | None = None
    resident_state_key: tuple[int, int] | None = None
    resident_state_generation: int | None = None


@dataclass(frozen=True)
class RwkvReviewRescheduleItem:
    card_id: int
    interval_days: int
    elapsed_days: int
    s90: float
    target_retention: float | None = None


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
    before_curve_prediction: (
        tuple[RwkvReviewInput, RwkvReviewPrediction | None] | None
    ) = None


@dataclass(frozen=True)
class RwkvReviewPredictionRequest:
    review_input: RwkvReviewInput
    card_state: object | None = None
    note_state: object | None = None
    deck_state: object | None = None
    preset_state: object | None = None
    global_state: object | None = None
    state_generation: int | None = field(default=None, compare=False)
    resident_state_digest: bytes | None = field(
        default=None,
        compare=False,
        repr=False,
    )


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
    deck_id: int | None = None
    history_hash: str = _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    replay_key: str = ""
    ignored_review_ids: tuple[int, ...] = ()
    prepared_checkpoint_histories: dict[int, RwkvHistoricalReviewInputs] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class RwkvResidentStateIdentity:
    last_review_id: int
    review_count: int
    history_hash: str
    replay_key: str


@dataclass(frozen=True)
class _ReviewerBackendPredictionStateToken:
    backend: RwkvReviewerBackend
    backend_assignment_generation: int
    collection_owner: object | None
    collection: object | None
    collection_backend: object | None
    state_generation: int
    resident_state_key: tuple[int, int] | None
    resident_state_generation: int | None
    resident_state_ready: bool
    dynamic_desired_retention_generation: int
    study_queue_generation: int


class _RwkvPendingAnswerState(NamedTuple):
    card_id: int
    ease: int
    review_state: int
    base_review_state: int | None
    answered_at_millis: int
    review_input: RwkvReviewInput


class _ReviewerBackendWarmupStart(NamedTuple):
    generation: int | None
    ready: bool


@dataclass(frozen=True)
class _ReviewerBackendTemporaryOperation:
    reviewer: object
    backend: RwkvReviewerBackend
    key: tuple[int, int]
    generation: int
    previous_state_present: bool
    previous_identity: RwkvResidentStateIdentity | None

    def is_current(self) -> bool:
        return _reviewer_backend_warmup_is_current(
            self.reviewer,
            self.backend,
            self.key,
            self.generation,
        )

    def require_current(self) -> None:
        _require_reviewer_backend_warmup_current(self.is_current)


class _ReviewerBackendWarmupInvalidated(Exception):
    pass


class _ReviewerBackendPredictionAborted(Exception):
    pass


class _ReviewerBackendPredictionBusy(_ReviewerBackendPredictionAborted):
    pass


class _RwkvSyntheticAnswerState(NamedTuple):
    ease: int
    review_state: int
    answered_at_millis: int


@dataclass(frozen=True)
class RwkvStoredStateCache:
    metadata: dict[str, object]
    snapshot: RwkvBackendCacheSnapshot | None
    history: RwkvHistoricalReviewInputs
    pending_history: RwkvHistoricalReviewInputs | None = None
    reusable_checkpoint_entries: tuple[_RwkvStateCacheCheckpointEntry, ...] = ()
    recovered_from_checkpoint: bool = False
    desired_checkpoint_review_counts: tuple[int, ...] = ()
    state_store_path: Path | None = None
    state_store_generation: str | None = None
    state_store_segment_id: int | None = None
    ignored_review_ids_changed: bool = False


@dataclass(frozen=True)
class RwkvStoredStateCheckpoint:
    snapshot: RwkvBackendCacheSnapshot
    history: RwkvHistoricalReviewInputs


@dataclass
class _RwkvStateCacheWriteContext:
    cache_dir: Path
    metadata_base: dict[str, object]
    state_store_path: Path | None = None
    state_store_generation: str | None = None
    state_store_temporary: bool = False
    state_store_head_segment_id: int | None = None


@dataclass(frozen=True)
class RwkvHistoricalPresetRule:
    preset_id: str
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


@dataclass(frozen=True)
class RwkvWorkloadProgress:
    current: int
    total: int


@dataclass
class RwkvWorkloadJob:
    cancel_event: threading.Event
    done: bool = False
    result: bytes | None = None
    error: str | None = None


@dataclass(frozen=True)
class RwkvMemorisedCardSeries:
    card_id: int
    note_id: int | None
    start_day: int
    values: bytes


@dataclass(frozen=True)
class RwkvMemorisedHistoryResult:
    identity: str
    first_day: int
    last_day: int
    cards: tuple[RwkvMemorisedCardSeries, ...]
    completed_through_day: int | None = None
    total: int = 0
    complete: bool = True


@dataclass
class RwkvMemorisedHistoryJob:
    cancel_event: threading.Event
    display_card_ids: frozenset[int]
    lock: threading.Lock = field(default_factory=threading.Lock)
    phase: str = "loading"
    current: int = 0
    total: int = 0
    first_day: int | None = None
    completed_through_day: int | None = None
    retrievability_by_day: list[float] = field(default_factory=list)
    note_retrievability_by_day: list[float] = field(default_factory=list)
    card_count_by_day: list[int] = field(default_factory=list)
    result: RwkvMemorisedHistoryResult | None = None
    checkpoint: RwkvMemorisedHistoryResult | None = None
    done: bool = False
    error: str | None = None


_rwkv_workload_progress_lock = threading.Lock()
_rwkv_workload_progress = RwkvWorkloadProgress(current=0, total=0)
_rwkv_workload_job_lock = threading.Lock()
_rwkv_workload_job: RwkvWorkloadJob | None = None
_rwkv_memorised_history_job_lock = threading.Lock()
_rwkv_memorised_history_job: RwkvMemorisedHistoryJob | None = None


@dataclass
class _RwkvSimulationCard:
    review_input: RwkvReviewInput
    due_day: int
    last_review_day: int
    interval_days: int
    reps: int
    lapses: int
    is_new: bool
    suspended: bool = False


@dataclass(frozen=True)
class _RwkvSimulationPoint:
    memorized: float
    weighted_memorized: float
    cost: float
    review_count: int


@dataclass(frozen=True)
class _RwkvSimulatorReviewModel:
    grade_seconds: tuple[float, float, float, float]
    bucket_probabilities: dict[int, tuple[float, float, float, float]]
    review_time_r_bucket_count: int = 0
    review_time_s_bucket_count: int = 0
    review_time_again_seconds: tuple[float, ...] = ()
    review_time_hard_seconds: tuple[float, ...] = ()
    review_time_good_seconds: tuple[float, ...] = ()
    review_time_easy_seconds: tuple[float, ...] = ()
    review_time_sample_counts: tuple[int, ...] = ()
    review_time_again_coeffs: tuple[float, ...] = ()
    review_time_hard_coeffs: tuple[float, ...] = ()
    review_time_good_coeffs: tuple[float, ...] = ()
    review_time_easy_coeffs: tuple[float, ...] = ()
    review_time_grade_weights: tuple[float, ...] = ()
    review_time_transition_probs: tuple[float, ...] = ()
    review_time_transition_counts: tuple[int, ...] = ()
    review_time_success_grade_probs: tuple[float, ...] = ()
    review_time_success_grade_counts: tuple[int, ...] = ()

    def probabilities_for(
        self, retrievability: float
    ) -> tuple[float, float, float, float]:
        bucket = _rwkv_simulator_bucket(retrievability)
        return self.bucket_probabilities.get(
            bucket,
            _fallback_rwkv_grade_probabilities(retrievability),
        )


@dataclass(frozen=True)
class _RwkvWorkloadScheduling:
    review_limit: int
    new_limit: int
    new_cards_ignore_review_limit: bool
    max_interval: int
    review_order: int
    suspend_after_lapses: int | None


class _RwkvSimulatorReviewTimeFields(NamedTuple):
    r_bucket_count: int
    s_bucket_count: int
    again_seconds: tuple[float, ...]
    hard_seconds: tuple[float, ...]
    good_seconds: tuple[float, ...]
    easy_seconds: tuple[float, ...]
    sample_counts: tuple[int, ...]
    again_coeffs: tuple[float, ...]
    hard_coeffs: tuple[float, ...]
    good_coeffs: tuple[float, ...]
    easy_coeffs: tuple[float, ...]
    grade_weights: tuple[float, ...]
    transition_probs: tuple[float, ...]
    transition_counts: tuple[int, ...]
    success_grade_probs: tuple[float, ...]
    success_grade_counts: tuple[int, ...]


RwkvWarmUpProgressCallback = Callable[[RwkvWarmUpProgress], None]
RwkvStateCacheProgressCallback = Callable[[str, int | None, int | None], None]
RwkvStateCacheSnapshotCallback = Callable[[int, RwkvBackendCacheSnapshot], None]
RwkvWorkloadProgressCallback = Callable[[int, int], None]
RwkvReviewPredictionRequestByIndex = tuple[int, RwkvReviewPredictionRequest]
RwkvCachedReviewPredictions = tuple[
    list[RwkvReviewPrediction | None],
    list[RwkvReviewPredictionRequestByIndex],
    int,
]
RwkvWorkloadOutput = tuple[
    float,
    float,
    Sequence[tuple[int, float, float, float, int]],
]
RwkvStatsPrepareKey = tuple[
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    bool,
    int,
    int,
    str,
    bool,
    bool,
    bool,
]
RwkvScorePrewarmKey = tuple[int, int, int, int, tuple[int, ...]]
RwkvFirstReviewElapsedStateCacheKey = tuple[tuple[object, bool], ...]
RwkvReviewQueueCollectionKey = tuple[int, int]
RwkvReviewQueueScoreConfigKey = tuple[
    RwkvReviewQueueCollectionKey,
    int,
    int,
    str,
    tuple[int, ...],
    int,
    int,
]
RwkvCalibrationMetricBin = tuple[int, int, int]
RwkvCalibrationMetricPair = tuple[float, int, RwkvCalibrationMetricBin]
RwkvReviewInputBatchCacheKey = tuple[
    int,
    int | None,
    bool,
    int,
    int,
    RwkvReviewQueueCollectionKey,
    str,
    tuple[int, ...],
    int,
    int,
]


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
        self._resident_state_populated = False
        self._state_generation = 0
        self._undo_frames: list[RwkvReviewRollbackFrame] = []
        self._redo_frames: list[RwkvReviewRollbackFrame] = []
        self._prediction_cache: OrderedDict[
            RwkvReviewInput,
            RwkvReviewPrediction | None,
        ] = OrderedDict()
        self._curve_prediction_cache: OrderedDict[
            RwkvReviewInput,
            RwkvReviewPrediction | None,
        ] = OrderedDict()
        self._restored_curve_predictions: dict[
            int,
            RwkvReviewPrediction | None,
        ] = {}
        initial_runtime_cache_state = getattr(self._runtime, "cache_state", None)
        self._initial_runtime_state = (
            _cacheable_state_bytes(initial_runtime_cache_state())
            if callable(initial_runtime_cache_state)
            else None
        )

    def cache_snapshot(self) -> RwkvBackendCacheSnapshot:
        if self._runtime_owns_warm_up_state():
            runtime_snapshot = getattr(self._runtime, "warm_up_snapshot", None)
            if not callable(runtime_snapshot):
                raise TypeError("RWKV resident runtime snapshot is unavailable")
            return cast(RwkvBackendCacheSnapshot, runtime_snapshot())

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

    def append_cache_snapshot_binary(self, path: Path) -> None:
        append_snapshot = getattr(
            self._runtime,
            "append_warm_up_snapshot_binary",
            None,
        )
        if not self._runtime_owns_warm_up_state() or not callable(append_snapshot):
            raise TypeError("RWKV resident runtime snapshot writer is unavailable")
        append_snapshot(path)

    def supports_streaming_cache_snapshot(self) -> bool:
        return self._runtime_owns_warm_up_state() and callable(
            getattr(self._runtime, "append_warm_up_snapshot_binary", None)
        )

    def supports_delta_state_store(self) -> bool:
        return self._runtime_owns_warm_up_state() and all(
            callable(getattr(self._runtime, method, None))
            for method in (
                "write_warm_up_state_checkpoint",
                "restore_warm_up_state_checkpoint",
            )
        )

    def write_state_cache_checkpoint(
        self,
        path: Path,
        store_generation: str,
        parent_segment_id: int | None,
        history: RwkvHistoricalReviewInputs,
        *,
        full: bool,
        durable: bool,
    ) -> int:
        writer = getattr(self._runtime, "write_warm_up_state_checkpoint", None)
        if not self.supports_delta_state_store() or not callable(writer):
            raise TypeError("RWKV resident runtime state-store writer is unavailable")
        previous_ids, previous_intervals, review_counts = (
            _encode_rwkv_state_cache_history_maps(history)
        )
        return cast(
            int,
            writer(
                path,
                store_generation,
                parent_segment_id,
                history.last_review_id,
                history.review_count,
                history.history_hash,
                history.replay_key,
                previous_ids,
                previous_intervals,
                review_counts,
                full,
                durable,
            ),
        )

    def finish_state_cache_checkpoints(self) -> None:
        finish = getattr(self._runtime, "finish_warm_up_state_checkpoints", None)
        if callable(finish):
            finish()

    def restore_state_cache_checkpoint(
        self,
        path: Path,
        store_generation: str,
        segment_id: int,
    ) -> None:
        restore = getattr(self._runtime, "restore_warm_up_state_checkpoint", None)
        if not self.supports_delta_state_store() or not callable(restore):
            raise TypeError("RWKV resident runtime state-store reader is unavailable")
        restore(path, store_generation, segment_id)
        self._clear_python_state_cache()
        self._resident_state_populated = True
        self._advance_state_generation()
        self._undo_frames.clear()
        self._redo_frames.clear()
        self._clear_prediction_cache("state cache restored")

    def restore_cache_snapshot(self, snapshot: RwkvBackendCacheSnapshot) -> None:
        _restore_runtime_warm_up_snapshot(self._runtime, snapshot)
        if self._runtime_owns_warm_up_state():
            self._clear_python_state_cache()
            self._resident_state_populated = bool(
                snapshot.card_states
                or snapshot.note_states
                or snapshot.deck_states
                or snapshot.preset_states
                or snapshot.global_state is not None
            )
        else:
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
        self._clear_python_state_cache()
        self._resident_state_populated = False
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
        snapshot_after_reviews: Sequence[int] = (),
        snapshot_recorder: RwkvStateCacheSnapshotCallback | None = None,
    ) -> None:
        total = len(reviews)
        snapshot_endpoints = {
            endpoint for endpoint in snapshot_after_reviews if 0 < endpoint <= total
        }
        report_every = _rwkv_warmup_progress_interval(total)
        _report_rwkv_warmup_progress(progress, processed=0, total=total)
        bulk_warm_up = getattr(self._runtime, "warm_up_reviews", None)
        bulk_parameters = (
            _callable_parameters(bulk_warm_up) if callable(bulk_warm_up) else {}
        )
        bulk_supports_snapshots = (
            snapshot_recorder is not None
            and _callable_accepts_keyword(
                bulk_parameters,
                "snapshot_after_reviews",
            )
            and _callable_accepts_keyword(bulk_parameters, "snapshot_recorder")
        )
        if (
            callable(bulk_warm_up)
            and self._can_use_runtime_bulk_warm_up()
            and (not snapshot_endpoints or bulk_supports_snapshots)
        ):
            continuing_resident_state = (
                self._runtime_owns_warm_up_state() and self._resident_state_populated
            )
            if not continuing_resident_state:
                self.reset_cache_snapshot()
            kwargs: dict[str, object] = {
                "review_ids": review_ids,
                "prediction_recorder": prediction_recorder,
                "progress": progress,
            }
            if bulk_supports_snapshots:
                kwargs["snapshot_after_reviews"] = sorted(snapshot_endpoints)
                kwargs["snapshot_recorder"] = snapshot_recorder
            if self._runtime_owns_warm_up_state() and _callable_accepts_keyword(
                bulk_parameters,
                "return_snapshot",
            ):
                kwargs["return_snapshot"] = False
            snapshot = bulk_warm_up(reviews, **kwargs)
            self._install_cache_snapshot(
                snapshot,
                resident_state_populated=continuing_resident_state or bool(reviews),
            )
            return

        for processed, review_input in enumerate(reviews, start=1):
            identity = review_input.identity
            if review_input.ease is not None:
                state = self._review_state_snapshot(
                    identity,
                    review_input,
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
                            card_state=state.card_state,
                            note_state=state.note_state,
                            deck_state=state.deck_state,
                            preset_state=state.preset_state,
                            global_state=state.global_state,
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
                    card_state=state.card_state,
                    note_state=state.note_state,
                    deck_state=state.deck_state,
                    preset_state=state.preset_state,
                    global_state=state.global_state,
                )
                self._store_transition(identity, transition)

            if processed in snapshot_endpoints and snapshot_recorder is not None:
                try:
                    snapshot = self.cache_snapshot()
                except TypeError:
                    logger.debug(
                        "RWKV historical checkpoints skipped: "
                        "runtime state is not cacheable",
                    )
                    snapshot_endpoints.clear()
                else:
                    snapshot_recorder(processed, snapshot)

            if processed == total or processed % report_every == 0:
                _report_rwkv_warmup_progress(
                    progress,
                    processed=processed,
                    total=total,
                )

    def _can_use_runtime_bulk_warm_up(self) -> bool:
        if self._runtime_owns_warm_up_state():
            return True
        return (
            not self._card_states
            and not self._note_states
            and not self._deck_states
            and not self._preset_states
            and self._global_state is None
        )

    def _install_cache_snapshot(
        self,
        snapshot: RwkvBackendCacheSnapshot | None,
        *,
        resident_state_populated: bool,
    ) -> None:
        if self._runtime_owns_warm_up_state():
            self._clear_python_state_cache()
            self._resident_state_populated = resident_state_populated
        elif snapshot is None:
            raise ValueError("RWKV bulk warm-up did not return a state snapshot")
        else:
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
        self._cache_curve_prediction(review_input, prediction)
        return prediction

    def predict_review_curve(
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
        cached, prediction = self._cached_curve_prediction(review_input)
        if cached:
            self._restored_curve_predictions.pop(identity.card_id, None)
            logger.debug(
                "RWKV stateful curve prediction cache hit: card_id=%s runtime=%s",
                identity.card_id,
                type(self._runtime).__name__,
            )
            return prediction

        if identity.card_id in self._restored_curve_predictions:
            prediction = self._restored_curve_predictions.pop(identity.card_id)
            self._cache_prediction(review_input, prediction)
            self._cache_curve_prediction(review_input, prediction)
            logger.debug(
                "RWKV stateful rollback curve prediction reused: card_id=%s runtime=%s",
                identity.card_id,
                type(self._runtime).__name__,
            )
            return prediction

        return self.predict_review_uncached(reviewer=reviewer, card=card)

    def predict_review_uncached(
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
        request = self._prediction_request(identity, review_input)
        prediction = self.predict_review_requests_uncached([request])[0]
        self._cache_prediction(review_input, prediction)
        self._cache_curve_prediction(review_input, prediction)
        return prediction

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
        request = self._prediction_request(identity, review_input)
        return self.predict_retrievability_requests_uncached([request])[0]

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
                self._cache_curve_prediction(request.review_input, prediction)
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
            self._cache_curve_prediction(request.review_input, predictions[index])
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

    def cache_review_input_predictions(
        self,
        entries: Sequence[tuple[RwkvReviewInput, RwkvReviewPrediction | None]],
    ) -> None:
        for review_input, prediction in entries:
            self._cache_prediction(review_input, prediction)

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
                self._cache_curve_prediction(request.review_input, prediction)
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
            self._cache_curve_prediction(request.review_input, prediction)
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

        start = time.monotonic()
        resident_predictions = self._resident_retrievability_predictions_for_requests(
            requests,
            cache_predictions=True,
        )
        if resident_predictions is not None:
            logger.debug(
                "RWKV stateful request batch predicted from resident state: "
                "requests=%s runtime=%s elapsed_ms=%.1f",
                len(requests),
                type(self._runtime).__name__,
                (time.monotonic() - start) * 1000,
            )
            return resident_predictions

        predict_retrievability_many = getattr(
            self._runtime,
            "predict_retrievability_many",
            None,
        )
        if not callable(predict_retrievability_many):
            return self.predict_review_requests(requests)

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
        return predictions

    def predict_retrievability_inputs_from_warm_up(
        self,
        inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    ) -> Sequence[RwkvReviewPrediction | None] | None:
        cached = self.cached_retrievability_inputs_from_warm_up(inputs_by_index)
        if cached is None:
            return None

        start = time.monotonic()
        predictions, misses, cache_hits = cached
        if misses:
            batch_predictions = (
                self.predict_retrievability_inputs_from_warm_up_uncached(
                    [review_input for _, review_input in misses]
                )
            )
            if len(batch_predictions) != len(misses):
                raise ValueError(
                    "RWKV resident retrievability prediction count mismatch"
                )
            for (index, review_input), prediction in zip(
                misses,
                batch_predictions,
                strict=True,
            ):
                predictions[index] = prediction
                self._cache_prediction(review_input, prediction)

        logger.debug(
            "RWKV stateful resident retrievability inputs predicted: inputs=%s "
            "cache_hits=%s runtime_requests=%s runtime=%s elapsed_ms=%.1f",
            len(inputs_by_index),
            cache_hits,
            len(misses),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return predictions

    def cached_retrievability_inputs_from_warm_up(
        self,
        inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    ) -> (
        tuple[
            list[RwkvReviewPrediction | None],
            list[tuple[int, RwkvReviewInput]],
            int,
        ]
        | None
    ):
        predict_many = getattr(
            self._runtime,
            "predict_retrievability_many_from_warm_up",
            None,
        )
        if not callable(predict_many):
            return None

        predictions: list[RwkvReviewPrediction | None] = [None] * len(inputs_by_index)
        misses: list[tuple[int, RwkvReviewInput]] = []
        cache_hits = 0
        for position, (index, review_input) in enumerate(inputs_by_index):
            cached, prediction = self._cached_prediction(review_input)
            if cached:
                cache_hits += 1
                predictions[position] = prediction
            else:
                misses.append((index, review_input))

        return predictions, misses, cache_hits

    def predict_retrievability_inputs_from_warm_up_uncached(
        self,
        review_inputs: Sequence[RwkvReviewInput],
    ) -> Sequence[RwkvReviewPrediction | None]:
        if not review_inputs:
            return []

        predict_many = getattr(
            self._runtime,
            "predict_retrievability_many_from_warm_up",
            None,
        )
        if not callable(predict_many):
            raise ValueError("RWKV resident retrievability prediction is unavailable")
        retrievabilities = predict_many(review_inputs)
        return [
            RwkvReviewPrediction(retrievability=float(retrievability))
            for retrievability in retrievabilities
        ]

    def predict_review_requests_uncached(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        if not requests:
            return []

        start = time.monotonic()
        predict_many = getattr(self._runtime, "predict_many", None)
        if callable(predict_many):
            logger.debug(
                "RWKV stateful uncached request batch predict_many started: "
                "requests=%s runtime=%s",
                len(requests),
                type(self._runtime).__name__,
            )
            predictions = predict_many(requests)
            if len(predictions) != len(requests):
                raise ValueError("RWKV batch prediction count mismatch")

            logger.debug(
                "RWKV stateful uncached request batch predicted: requests=%s "
                "runtime=%s elapsed_ms=%.1f",
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
        logger.debug(
            "RWKV stateful uncached request batch predicted via per-card fallback: "
            "requests=%s runtime=%s elapsed_ms=%.1f",
            len(requests),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return predictions

    def predict_retrievability_requests_uncached(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
    ) -> Sequence[RwkvReviewPrediction | None]:
        if not requests:
            return []

        start = time.monotonic()
        resident_predictions = self._resident_retrievability_predictions_for_requests(
            requests,
            cache_predictions=False,
        )
        if resident_predictions is not None:
            logger.debug(
                "RWKV stateful uncached request batch predicted from resident state: "
                "requests=%s runtime=%s elapsed_ms=%.1f",
                len(requests),
                type(self._runtime).__name__,
                (time.monotonic() - start) * 1000,
            )
            return resident_predictions

        predict_retrievability_many = getattr(
            self._runtime,
            "predict_retrievability_many",
            None,
        )
        if not callable(predict_retrievability_many):
            return self.predict_review_requests_uncached(requests)

        retrievabilities = predict_retrievability_many(requests)
        if len(retrievabilities) != len(requests):
            raise ValueError("RWKV retrievability batch prediction count mismatch")

        logger.debug(
            "RWKV stateful uncached request batch predict_retrievability_many "
            "predicted: requests=%s runtime=%s elapsed_ms=%.1f",
            len(requests),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return [
            RwkvReviewPrediction(retrievability=float(retrievability))
            for retrievability in retrievabilities
        ]

    def _resident_retrievability_predictions_for_requests(
        self,
        requests: Sequence[RwkvReviewPredictionRequest],
        *,
        cache_predictions: bool,
    ) -> Sequence[RwkvReviewPrediction | None] | None:
        if not callable(
            getattr(self._runtime, "predict_retrievability_many_from_warm_up", None)
        ):
            return None
        if any(not request.review_input.is_query for request in requests):
            return None

        state_generation = self._state_generation
        for request in requests:
            if request.state_generation is not None:
                if (
                    request.state_generation != state_generation
                    or request.resident_state_digest
                    != _prediction_request_state_digest(request)
                ):
                    return None
                continue
            current_request = self._prediction_request(
                request.review_input.identity,
                request.review_input,
            )
            if request != current_request:
                return None

        predictions = self.predict_retrievability_inputs_from_warm_up_uncached(
            [request.review_input for request in requests]
        )
        if self._state_generation != state_generation:
            return None
        if cache_predictions:
            for request, prediction in zip(requests, predictions, strict=True):
                self._cache_prediction(request.review_input, prediction)
        return predictions

    def predict_retrievability_after_review(
        self,
        *,
        answer: RwkvReviewInput,
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        snapshot: RwkvBackendCacheSnapshot,
    ) -> list[tuple[int, float]] | None:
        predict_future = getattr(
            self._runtime,
            "predict_retrievability_many_after_review",
            None,
        )
        if not callable(predict_future):
            return None
        if not inputs_by_card_id:
            return []

        start = time.monotonic()
        retrievabilities = predict_future(
            answer=answer,
            query_inputs=[review_input for _, review_input in inputs_by_card_id],
            snapshot=snapshot,
        )
        if len(retrievabilities) != len(inputs_by_card_id):
            raise ValueError("RWKV future retrievability prediction count mismatch")

        scores: list[tuple[int, float]] = []
        for (card_id, _), retrievability in zip(
            inputs_by_card_id,
            retrievabilities,
            strict=True,
        ):
            value = float(retrievability)
            if not math.isfinite(value) or not 0 <= value <= 1:
                continue
            scores.append((card_id, value))

        logger.debug(
            "RWKV stateful future retrievability predicted: inputs=%s scored=%s "
            "runtime=%s elapsed_ms=%.1f",
            len(inputs_by_card_id),
            len(scores),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return scores

    def predict_retrievability_after_reviews(
        self,
        *,
        answers: Sequence[RwkvReviewInput],
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
        snapshot: RwkvBackendCacheSnapshot,
    ) -> list[list[tuple[int, float]]] | None:
        predict_future = getattr(
            self._runtime,
            "predict_retrievability_many_after_reviews",
            None,
        )
        if not callable(predict_future):
            return None
        if not answers:
            return []
        if not inputs_by_card_id:
            return [[] for _ in answers]

        start = time.monotonic()
        retrievability_batches = predict_future(
            answers=answers,
            query_inputs=[review_input for _, review_input in inputs_by_card_id],
            snapshot=snapshot,
        )
        score_batches = self._future_retrievability_score_batches(
            retrievability_batches,
            answer_count=len(answers),
            inputs_by_card_id=inputs_by_card_id,
        )

        logger.debug(
            "RWKV stateful future retrievability multi-answer predicted: "
            "answers=%s inputs=%s scored=%s runtime=%s elapsed_ms=%.1f",
            len(answers),
            len(inputs_by_card_id),
            sum(len(scores) for scores in score_batches),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return score_batches

    def predict_retrievability_after_reviews_from_warm_up(
        self,
        *,
        answers: Sequence[RwkvReviewInput],
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    ) -> list[list[tuple[int, float]]] | None:
        predict_future = getattr(
            self._runtime,
            "predict_retrievability_many_after_reviews_from_warm_up",
            None,
        )
        if not callable(predict_future):
            return None
        if not answers:
            return []
        if not inputs_by_card_id:
            return [[] for _ in answers]

        start = time.monotonic()
        retrievability_batches = predict_future(
            answers=answers,
            query_inputs=[review_input for _, review_input in inputs_by_card_id],
        )
        score_batches = self._future_retrievability_score_batches(
            retrievability_batches,
            answer_count=len(answers),
            inputs_by_card_id=inputs_by_card_id,
        )
        logger.debug(
            "RWKV stateful resident future retrievability multi-answer predicted: "
            "answers=%s inputs=%s scored=%s runtime=%s elapsed_ms=%.1f",
            len(answers),
            len(inputs_by_card_id),
            sum(len(scores) for scores in score_batches),
            type(self._runtime).__name__,
            (time.monotonic() - start) * 1000,
        )
        return score_batches

    @staticmethod
    def _future_retrievability_score_batches(
        retrievability_batches: Sequence[Sequence[float]],
        *,
        answer_count: int,
        inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    ) -> list[list[tuple[int, float]]]:
        if len(retrievability_batches) != answer_count:
            raise ValueError("RWKV future retrievability answer count mismatch")
        if any(
            len(retrievabilities) != len(inputs_by_card_id)
            for retrievabilities in retrievability_batches
        ):
            raise ValueError("RWKV future retrievability prediction count mismatch")

        score_batches: list[list[tuple[int, float]]] = []
        for retrievabilities in retrievability_batches:
            scores: list[tuple[int, float]] = []
            for (card_id, _), retrievability in zip(
                inputs_by_card_id,
                retrievabilities,
                strict=True,
            ):
                value = float(retrievability)
                if math.isfinite(value) and 0 <= value <= 1:
                    scores.append((card_id, value))
            score_batches.append(scores)
        return score_batches

    def simulate_workload(
        self,
        *,
        inputs: Sequence[tuple[int, RwkvReviewInput, int]],
        snapshot: RwkvBackendCacheSnapshot,
        min_dr: int,
        max_dr: int,
        target_dr_step: int,
        days_to_simulate: int,
        scheduling: _RwkvWorkloadScheduling,
        state_update_interval: int,
        review_model: _RwkvSimulatorReviewModel,
        progress: RwkvWorkloadProgressCallback | None = None,
    ) -> object | None:
        simulate_workload = getattr(self._runtime, "simulate_workload", None)
        if not callable(simulate_workload):
            return None
        return simulate_workload(
            inputs=inputs,
            snapshot=snapshot,
            min_dr=min_dr,
            max_dr=max_dr,
            target_dr_step=target_dr_step,
            days_to_simulate=days_to_simulate,
            scheduling=scheduling,
            state_update_interval=state_update_interval,
            review_model=review_model,
            progress=progress,
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
        before = self._snapshot(identity, review_input)
        before_curve_prediction = self._curve_prediction_for_card(identity.card_id)
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
                before_curve_prediction=before_curve_prediction,
            ),
        )

    def review_input_answered(self, review_input: RwkvReviewInput) -> None:
        if review_input.ease is None:
            return

        identity = review_input.identity
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

    def answer_undone(self, counter: int, next_counter: int | None) -> int | None:
        index = _rollback_frame_index(self._undo_frames, counter)
        if index is None:
            return None

        frame = self._undo_frames.pop(index)
        self._restore_snapshot(frame.identity, frame.before)
        self._restore_curve_prediction(frame.before_curve_prediction)
        _append_bounded(
            self._redo_frames,
            replace(
                frame, counter=next_counter if next_counter is not None else counter
            ),
        )
        return frame.identity.card_id

    def answer_redone(self, counter: int, next_counter: int | None) -> int | None:
        index = _rollback_frame_index(self._redo_frames, counter)
        if index is None:
            return None

        frame = self._redo_frames.pop(index)
        self._restore_snapshot(frame.identity, frame.after)
        _append_bounded(
            self._undo_frames,
            replace(
                frame, counter=next_counter if next_counter is not None else counter
            ),
        )
        return frame.identity.card_id

    def _store_transition(
        self,
        identity: RwkvReviewIdentity,
        transition: RwkvReviewTransition,
    ) -> None:
        if self._runtime_owns_warm_up_state():
            self._resident_state_populated = True
        else:
            self._card_states[identity.card_id] = transition.card_state
            _set_entity_state(
                self._note_states, identity.note_id, transition.note_state
            )
            _set_entity_state(
                self._deck_states, identity.deck_id, transition.deck_state
            )
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
        return replace(
            self._review_state_snapshot(identity, review_input),
            runtime_state=_runtime_state(self._runtime, review_input),
        )

    def _restore_snapshot(
        self,
        identity: RwkvReviewIdentity,
        snapshot: RwkvReviewerStateSnapshot,
    ) -> None:
        if self._runtime_owns_warm_up_state():
            self._resident_state_populated = True
        else:
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
        _restore_runtime_warm_up_state(self._runtime, identity, snapshot)
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
        state = self._review_state_snapshot(identity, review_input)
        resident_state = self._runtime_owns_warm_up_state()
        request = RwkvReviewPredictionRequest(
            review_input=review_input,
            card_state=state.card_state,
            note_state=state.note_state,
            deck_state=state.deck_state,
            preset_state=state.preset_state,
            global_state=state.global_state,
            state_generation=self._state_generation if resident_state else None,
        )
        if not resident_state:
            return request
        return replace(
            request,
            resident_state_digest=_prediction_request_state_digest(request),
        )

    def _review_state_snapshot(
        self,
        identity: RwkvReviewIdentity,
        review_input: RwkvReviewInput,
    ) -> RwkvReviewerStateSnapshot:
        if self._runtime_owns_warm_up_state():
            runtime_state = getattr(self._runtime, "warm_up_state", None)
            if not callable(runtime_state):
                raise TypeError("RWKV resident runtime state is unavailable")
            return cast(RwkvReviewerStateSnapshot, runtime_state(review_input))

        return RwkvReviewerStateSnapshot(
            card_state=self._card_states.get(identity.card_id),
            note_state=_entity_state(self._note_states, identity.note_id),
            deck_state=_entity_state(self._deck_states, identity.deck_id),
            preset_state=_entity_state(self._preset_states, identity.preset_id),
            global_state=self._global_state,
        )

    def _runtime_owns_warm_up_state(self) -> bool:
        return getattr(self._runtime, "resident_warm_up_state", False) is True

    def _clear_python_state_cache(self) -> None:
        self._card_states.clear()
        self._note_states.clear()
        self._deck_states.clear()
        self._preset_states.clear()
        self._global_state = None

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

    def _cached_curve_prediction(
        self,
        review_input: RwkvReviewInput,
    ) -> tuple[bool, RwkvReviewPrediction | None]:
        try:
            prediction = self._curve_prediction_cache[review_input]
        except KeyError:
            return False, None

        self._curve_prediction_cache.move_to_end(review_input)
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

    def _cache_curve_prediction(
        self,
        review_input: RwkvReviewInput,
        prediction: RwkvReviewPrediction | None,
    ) -> None:
        self._curve_prediction_cache[review_input] = prediction
        self._curve_prediction_cache.move_to_end(review_input)
        while len(self._curve_prediction_cache) > _RWKV_REVIEW_PREDICTION_CACHE_LIMIT:
            self._curve_prediction_cache.popitem(last=False)

    def _curve_prediction_for_card(
        self,
        card_id: int,
    ) -> tuple[RwkvReviewInput, RwkvReviewPrediction | None] | None:
        for review_input, prediction in reversed(self._curve_prediction_cache.items()):
            if review_input.identity.card_id == card_id:
                return review_input, prediction
        return None

    def _restore_curve_prediction(
        self,
        entry: tuple[RwkvReviewInput, RwkvReviewPrediction | None] | None,
    ) -> None:
        if entry is None:
            return
        review_input, prediction = entry
        self._cache_prediction(review_input, prediction)
        self._cache_curve_prediction(review_input, prediction)
        self._restored_curve_predictions[review_input.identity.card_id] = prediction

    def _clear_prediction_cache(self, reason: str) -> None:
        cached_entries = (
            len(self._prediction_cache)
            + len(self._curve_prediction_cache)
            + len(self._restored_curve_predictions)
        )
        if cached_entries:
            logger.debug(
                "RWKV stateful prediction cache cleared: reason=%s entries=%s "
                "runtime=%s",
                reason,
                cached_entries,
                type(self._runtime).__name__,
            )
        self._prediction_cache.clear()
        self._curve_prediction_cache.clear()
        self._restored_curve_predictions.clear()


def record_collection_undo(changes: object) -> list[int]:
    """Roll back RWKV state after Anki undoes an answered review."""

    return _record_collection_undo_or_redo(changes, redo=False)


def record_collection_redo(changes: object) -> list[int]:
    """Restore RWKV state after Anki redoes an answered review."""

    return _record_collection_undo_or_redo(changes, redo=True)


def _record_collection_undo_or_redo(changes: object, *, redo: bool) -> list[int]:
    backend = _reviewer_backend
    if backend is None:
        return []

    counter = _undo_result_counter(changes)
    if counter is None:
        return []

    operation = "review redo" if redo else "review undo"
    if not _reviewer_backend_execution_lock.acquire(blocking=False):
        _invalidate_reviewer_backend_states(
            backend,
            reason=f"{operation} while RWKV backend was busy",
        )
        logger.debug("RWKV %s skipped: backend busy", operation)
        return []

    next_counter = _undo_result_next_counter(changes)
    try:
        with _reviewer_backend_state_lock:
            pending = any(
                key[0] == id(backend)
                for key in _reviewer_backend_warmup_pending_generations
            )
            backend_changed = _reviewer_backend is not backend
        if pending or backend_changed:
            _invalidate_reviewer_backend_states(
                backend,
                reason=f"{operation} while RWKV state was pending",
            )
            logger.debug(
                "RWKV %s skipped: pending=%s backend_changed=%s",
                operation,
                pending,
                backend_changed,
            )
            return []

        handler_name = "answer_redone" if redo else "answer_undone"
        handler = getattr(backend, handler_name, None)
        if callable(handler):
            restored = handler(counter, next_counter)
            if card_id := _valid_card_id(restored):
                restored_card_ids = [card_id]
            elif isinstance(restored, Sequence) and not isinstance(restored, str):
                restored_card_ids = [
                    card_id
                    for value in restored
                    if (card_id := _valid_card_id(value)) is not None
                ]
            else:
                restored_card_ids = []
            if restored_card_ids:
                _mark_reviewer_backend_identities_unknown(
                    backend,
                    reason="review redone" if redo else "review undone",
                )
            return restored_card_ids
    finally:
        _reviewer_backend_execution_lock.release()

    return []


def queue_reviewer_undo_card_ids(reviewer: object, card_ids: Sequence[int]) -> None:
    valid_card_ids = [
        card_id for value in card_ids if (card_id := _valid_card_id(value)) is not None
    ]
    if not valid_card_ids:
        return

    _invalidate_reviewer_transient_scores_after_undo(reviewer, valid_card_ids)
    synthetic_states = getattr(
        reviewer,
        _REVIEWER_SYNTHETIC_ANSWER_STATES_ATTR,
        None,
    )
    if isinstance(synthetic_states, dict):
        for card_id in valid_card_ids:
            synthetic_states.pop(card_id, None)

    queue = getattr(reviewer, _RWKV_REVIEW_UNDO_CARD_IDS_ATTR, None)
    if not isinstance(queue, list):
        queue = []
        setattr(reviewer, _RWKV_REVIEW_UNDO_CARD_IDS_ATTR, queue)
    queue.extend(valid_card_ids)
    setattr(reviewer, _RWKV_REVIEW_UNDO_QUEUE_CHANGE_PENDING_ATTR, True)

    answered_ids = getattr(reviewer, "_answeredIds", None)
    if not isinstance(answered_ids, list):
        return

    for card_id in valid_card_ids:
        for index in range(len(answered_ids) - 1, -1, -1):
            if answered_ids[index] == card_id:
                del answered_ids[index]
                break


def _invalidate_reviewer_transient_scores_after_undo(
    reviewer: object,
    card_ids: Sequence[int],
) -> None:
    _rwkv_review_queue_score_generations.clear()
    _clear_rwkv_review_input_batch_cache(reviewer)
    _invalidate_resolved_preset_id_cache(reviewer, card_ids=card_ids)
    for card_id in card_ids:
        _set_rwkv_card_info_score(reviewer, card_id, None)


def pop_reviewer_undo_card_id(reviewer: object) -> int | None:
    queue = getattr(reviewer, _RWKV_REVIEW_UNDO_CARD_IDS_ATTR, None)
    if not isinstance(queue, list):
        return None

    while queue:
        if card_id := _valid_card_id(queue.pop(0)):
            return card_id
    return None


def reviewer_has_undo_card_ids(reviewer: object) -> bool:
    queue = getattr(reviewer, _RWKV_REVIEW_UNDO_CARD_IDS_ATTR, None)
    return isinstance(queue, list) and any(
        _valid_card_id(value) is not None for value in queue
    )


def _consume_reviewer_undo_queue_change(reviewer: object) -> bool:
    if not bool(getattr(reviewer, _RWKV_REVIEW_UNDO_QUEUE_CHANGE_PENDING_ATTR, False)):
        return False
    setattr(reviewer, _RWKV_REVIEW_UNDO_QUEUE_CHANGE_PENDING_ATTR, False)
    return True


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


def _valid_card_id(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


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


def _restore_runtime_warm_up_snapshot(
    runtime: RwkvReviewRuntime,
    snapshot: RwkvBackendCacheSnapshot,
) -> None:
    restore = getattr(runtime, "restore_warm_up_snapshot", None)
    if callable(restore):
        restore(snapshot)


def _restore_runtime_warm_up_state(
    runtime: RwkvReviewRuntime,
    identity: RwkvReviewIdentity,
    snapshot: RwkvReviewerStateSnapshot,
) -> None:
    restore = getattr(runtime, "restore_warm_up_state", None)
    if callable(restore):
        restore(identity, snapshot)


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


def _prediction_request_state_digest(
    request: RwkvReviewPredictionRequest,
) -> bytes | None:
    digest = hashlib.blake2b(digest_size=16)
    for state in (
        request.card_state,
        request.note_state,
        request.deck_state,
        request.preset_state,
        request.global_state,
    ):
        if state is None:
            digest.update(b"\0")
        elif isinstance(state, bytes):
            digest.update(b"\1")
            digest.update(len(state).to_bytes(8, "little"))
            digest.update(state)
        else:
            return None
    return digest.digest()


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
        default_sample_role: str = _RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT,
        default_fold_index: int = -1,
        sample_role_by_review_id: Mapping[int, str] | None = None,
        fold_index_by_review_id: Mapping[int, int] | None = None,
    ) -> None:
        self._col: Any | None = _collection(reviewer)
        self._source = source
        self._default_sample_role = default_sample_role
        self._default_fold_index = default_fold_index
        self._sample_role_by_review_id = sample_role_by_review_id or {}
        self._fold_index_by_review_id = fold_index_by_review_id or {}
        self._rows: list[tuple[int, float, str, int]] = []

    def __call__(self, review_id: int, retrievability: float) -> None:
        self.record(review_id, retrievability)

    def record(self, review_id: int, retrievability: float) -> None:
        self.record_many([(review_id, retrievability)])

    def record_many(self, rows: Sequence[tuple[int, float]]) -> None:
        if self._col is None:
            return

        for review_id, retrievability in rows:
            if (
                review_id <= 0
                or not math.isfinite(retrievability)
                or retrievability < 0
                or retrievability > 1
            ):
                continue

            self._rows.append(
                (
                    review_id,
                    retrievability,
                    self._sample_role_by_review_id.get(
                        review_id,
                        self._default_sample_role,
                    ),
                    self._fold_index_by_review_id.get(
                        review_id,
                        self._default_fold_index,
                    ),
                )
            )
            if len(self._rows) >= 1000:
                self.flush()

    def flush(self) -> None:
        if self._col is None or not self._rows:
            return

        rows = self._rows
        self._rows = []
        backend = getattr(self._col, "_backend", None)
        store_rows = getattr(backend, "set_rwkv_review_retrievability_cache_rows", None)
        if not callable(store_rows):
            logger.debug(
                "RWKV review retrievability cache skipped: backend unavailable"
            )
            return

        try:
            store_rows(
                source=self._source,
                rows=[
                    scheduler_pb2.RwkvReviewRetrievabilityCacheRowsRequest.Row(
                        revlog_id=review_id,
                        prediction=prediction,
                        sample_role=sample_role,
                        fold_index=fold_index,
                    )
                    for review_id, prediction, sample_role, fold_index in rows
                ],
            )
        except Exception:
            logger.exception("failed to store RWKV review retrievability cache")


def set_reviewer_backend(
    backend: RwkvReviewerBackend | None,
) -> RwkvReviewerBackend | None:
    global _reviewer_backend, _rwkv_review_queue_collection_key
    global _reviewer_backend_assignment_generation

    with _reviewer_backend_state_lock:
        previous = _reviewer_backend
        _invalidate_all_reviewer_backend_runtime_state_locked()
        _resolved_preset_id_cache.clear()
        _rwkv_review_queue_score_maps.clear()
        _rwkv_review_queue_target_maps.clear()
        _rwkv_review_queue_score_generations.clear()
        _rwkv_review_queue_score_config_keys.clear()
        _rwkv_review_input_batch_module_cache.clear()
        _rwkv_review_queue_collection_key = None
        _reviewer_backend = backend
        _reviewer_backend_assignment_generation += 1
    _rwkv_score_prewarm_in_flight.clear()
    return previous


def _invalidate_all_reviewer_backend_runtime_state_locked() -> None:
    keys = (
        _reviewer_backend_warmup_states.keys()
        | _reviewer_backend_warmup_generations.keys()
        | _reviewer_backend_warmup_pending_generations.keys()
        | _rwkv_memorised_history_identity_cache.keys()
    )
    for key in keys:
        _reviewer_backend_warmup_generations[key] = (
            _reviewer_backend_warmup_generations.get(key, 0) + 1
        )
    _reviewer_backend_warmup_states.clear()
    _reviewer_backend_warmup_pending_generations.clear()
    _rwkv_memorised_history_identity_cache.clear()


def _invalidate_reviewer_backend_runtime_state_for_profile_open() -> None:
    global _rwkv_startup_prompt_shown

    with _reviewer_backend_state_lock:
        _invalidate_all_reviewer_backend_runtime_state_locked()
        _rwkv_startup_prompt_shown = False


def _finish_reviewer_backend_warmup(
    key: tuple[int, int],
    generation: int,
) -> None:
    with _reviewer_backend_state_lock:
        if _reviewer_backend_warmup_pending_generations.get(key) == generation:
            _reviewer_backend_warmup_pending_generations.pop(key, None)


def _reviewer_backend_warmup_is_current(
    reviewer: object,
    backend: RwkvReviewerBackend,
    key: tuple[int, int],
    generation: int,
) -> bool:
    with _reviewer_backend_state_lock:
        col = _collection(reviewer)
        return (
            _reviewer_backend is backend
            and col is not None
            and getattr(col, "db", None) is not None
            and key == (id(backend), id(col))
            and _reviewer_backend_warmup_generations.get(key, 0) == generation
            and _reviewer_backend_warmup_pending_generations.get(key) == generation
        )


def _require_reviewer_backend_warmup_current(
    is_current: Callable[[], bool],
) -> None:
    if not is_current():
        raise _ReviewerBackendWarmupInvalidated


def _acquire_reviewer_backend_execution(
    reviewer: object,
    backend: RwkvReviewerBackend,
    key: tuple[int, int],
    generation: int,
) -> bool:
    _reviewer_backend_execution_lock.acquire()
    try:
        if _reviewer_backend_warmup_is_current(
            reviewer,
            backend,
            key,
            generation,
        ):
            return True
    except Exception:
        _reviewer_backend_execution_lock.release()
        raise
    _reviewer_backend_execution_lock.release()
    return False


@contextmanager
def _try_reviewer_backend_prediction_access(
    *,
    expected_backend: RwkvReviewerBackend | None = None,
    expected_backend_assignment_generation: int | None = None,
    expected_state_generation: int | None = None,
    expected_resident_state_key: tuple[int, int] | None = None,
    expected_resident_state_generation: int | None = None,
    expected_state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> Iterator[RwkvReviewerBackend | None]:
    """Claim non-blocking access to resident backend state for prediction."""

    inherited_backend = getattr(
        _reviewer_backend_prediction_local,
        "backend",
        None,
    )
    if expected_state_token is not None:
        expected_backend = expected_state_token.backend
    requested_backend = (
        expected_backend if expected_backend is not None else inherited_backend
    )
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
    if backend is None or (
        requested_backend is not None and backend is not requested_backend
    ):
        yield None
        return
    if not _reviewer_backend_execution_lock.acquire(blocking=False):
        yield None
        return

    try:
        if not _reviewer_backend_prediction_access_is_current(
            backend,
            expected_backend_assignment_generation=(
                expected_backend_assignment_generation
            ),
            expected_state_generation=expected_state_generation,
            expected_resident_state_key=expected_resident_state_key,
            expected_resident_state_generation=expected_resident_state_generation,
            expected_state_token=expected_state_token,
        ):
            yield None
            return
        previous_backend = inherited_backend
        _reviewer_backend_prediction_local.backend = backend
        try:
            yield backend
        finally:
            if previous_backend is None:
                delattr(_reviewer_backend_prediction_local, "backend")
            else:
                _reviewer_backend_prediction_local.backend = previous_backend
    finally:
        _reviewer_backend_execution_lock.release()


def _reviewer_backend_prediction_access_is_current(
    backend: RwkvReviewerBackend,
    *,
    expected_backend_assignment_generation: int | None = None,
    expected_state_generation: int | None = None,
    expected_resident_state_key: tuple[int, int] | None = None,
    expected_resident_state_generation: int | None = None,
    expected_state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> bool:
    if expected_state_token is not None:
        if backend is not expected_state_token.backend:
            return False
        expected_state_generation = expected_state_token.state_generation
        expected_backend_assignment_generation = (
            expected_state_token.backend_assignment_generation
        )
        expected_resident_state_key = expected_state_token.resident_state_key
        expected_resident_state_generation = (
            expected_state_token.resident_state_generation
        )

    with _reviewer_backend_state_lock:
        backend_is_current = _reviewer_backend is backend and (
            expected_state_generation is None
            or _reviewer_backend_state_generation(backend) == expected_state_generation
        )
        if not backend_is_current:
            return False
        if (
            expected_backend_assignment_generation is not None
            and _reviewer_backend_assignment_generation
            != expected_backend_assignment_generation
        ):
            return False
        if expected_state_token is not None and (
            _reviewer_backend_assignment_generation
            != expected_state_token.backend_assignment_generation
            or (
                expected_state_token.collection_owner is not None
                and getattr(
                    expected_state_token.collection_owner,
                    "col",
                    None,
                )
                is not expected_state_token.collection
                or (
                    expected_state_token.collection is not None
                    and getattr(
                        expected_state_token.collection,
                        "_backend",
                        None,
                    )
                    is not expected_state_token.collection_backend
                )
            )
        ):
            return False
        if (
            expected_state_token is not None
            and not _rwkv_review_queue_context_epochs_are_current(expected_state_token)
        ):
            return False
        if (
            expected_resident_state_key is None
            or expected_resident_state_generation is None
        ):
            return True
        resident_state_ready = (
            expected_resident_state_key in _reviewer_backend_warmup_states
            and expected_resident_state_key
            not in _reviewer_backend_warmup_pending_generations
        )
        expected_resident_state_ready = (
            expected_state_token.resident_state_ready
            if expected_state_token is not None
            else True
        )
        return (
            resident_state_ready == expected_resident_state_ready
            and _reviewer_backend_warmup_generations.get(
                expected_resident_state_key,
                0,
            )
            == expected_resident_state_generation
        )


def _capture_reviewer_backend_prediction_state_token(
    reviewer: object,
    *,
    expected_backend: RwkvReviewerBackend | None = None,
) -> _ReviewerBackendPredictionStateToken | None:
    """Capture the complete resident state identity used by multi-batch work."""

    collection_owner = getattr(reviewer, "mw", None)
    col = _collection(reviewer)
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
        if backend is None or (
            expected_backend is not None and backend is not expected_backend
        ):
            return None

        resident_state_key = (
            (id(backend), id(col))
            if (
                callable(getattr(backend, "warm_up", None))
                and col is not None
                and getattr(col, "db", None) is not None
            )
            else None
        )
        resident_state_generation = (
            _reviewer_backend_warmup_generations.get(
                resident_state_key,
                0,
            )
            if resident_state_key is not None
            else None
        )
        resident_state_ready = resident_state_key is None or (
            resident_state_key in _reviewer_backend_warmup_states
            and resident_state_key not in _reviewer_backend_warmup_pending_generations
        )
        if not resident_state_ready:
            return None
        return _ReviewerBackendPredictionStateToken(
            backend=backend,
            backend_assignment_generation=_reviewer_backend_assignment_generation,
            collection_owner=collection_owner,
            collection=col,
            collection_backend=getattr(col, "_backend", None),
            state_generation=_reviewer_backend_state_generation(backend),
            resident_state_key=resident_state_key,
            resident_state_generation=resident_state_generation,
            resident_state_ready=resident_state_ready,
            dynamic_desired_retention_generation=(
                _dynamic_desired_retention_generation
            ),
            study_queue_generation=_rwkv_study_queue_generation,
        )


def _reviewer_backend_prediction_state_token_is_current(
    state_token: _ReviewerBackendPredictionStateToken,
) -> bool:
    return _reviewer_backend_prediction_access_is_current(
        state_token.backend,
        expected_state_token=state_token,
    )


def _raise_reviewer_backend_prediction_unavailable(
    state_token: _ReviewerBackendPredictionStateToken,
) -> None:
    if _reviewer_backend_prediction_state_token_is_current(state_token):
        raise _ReviewerBackendPredictionBusy
    raise _ReviewerBackendPredictionAborted


def _reviewer_backend_resident_state_token(
    reviewer: object,
    backend: RwkvReviewerBackend,
) -> tuple[tuple[int, int], int] | None:
    col = _collection(reviewer)
    if col is None or getattr(col, "db", None) is None:
        return None
    key = (id(backend), id(col))
    with _reviewer_backend_state_lock:
        if (
            _reviewer_backend is not backend
            or key not in _reviewer_backend_warmup_states
            or key in _reviewer_backend_warmup_pending_generations
        ):
            return None
        return key, _reviewer_backend_warmup_generations.get(key, 0)


def _claim_reviewer_backend_temporary_operation(
    reviewer: object,
    backend: RwkvReviewerBackend,
) -> _ReviewerBackendTemporaryOperation | None:
    context = _reviewer_backend_warmup_context(reviewer)
    if context is None or context[0] is not backend:
        return None
    key = context[1]

    with _reviewer_backend_state_lock:
        if (
            _reviewer_backend is not backend
            or key in _reviewer_backend_warmup_pending_generations
        ):
            return None
        generation = _reviewer_backend_warmup_generations.get(key, 0)
        previous_state_present = key in _reviewer_backend_warmup_states
        previous_identity = _reviewer_backend_warmup_states.pop(key, None)
        _rwkv_memorised_history_identity_cache.pop(key, None)
        _reviewer_backend_warmup_pending_generations[key] = generation

    operation = _ReviewerBackendTemporaryOperation(
        reviewer=reviewer,
        backend=backend,
        key=key,
        generation=generation,
        previous_state_present=previous_state_present,
        previous_identity=previous_identity,
    )
    if _acquire_reviewer_backend_execution(
        reviewer,
        backend,
        key,
        generation,
    ):
        return operation

    _finish_reviewer_backend_warmup(key, generation)
    return None


def _finish_reviewer_backend_temporary_operation(
    operation: _ReviewerBackendTemporaryOperation,
    *,
    restored: bool,
) -> None:
    discard_queue_scores = False
    with _reviewer_backend_state_lock:
        col = _collection(operation.reviewer)
        current = (
            _reviewer_backend is operation.backend
            and col is not None
            and getattr(col, "db", None) is not None
            and operation.key == (id(operation.backend), id(col))
            and _reviewer_backend_warmup_generations.get(operation.key, 0)
            == operation.generation
            and _reviewer_backend_warmup_pending_generations.get(operation.key)
            == operation.generation
        )
        if current and restored and operation.previous_state_present:
            _reviewer_backend_warmup_states[operation.key] = operation.previous_identity
            if operation.previous_identity is not None:
                _rwkv_memorised_history_identity_cache[operation.key] = (
                    operation.generation,
                    operation.previous_identity,
                )
            else:
                _rwkv_memorised_history_identity_cache.pop(operation.key, None)
        elif current:
            _reviewer_backend_warmup_states.pop(operation.key, None)
            _rwkv_memorised_history_identity_cache.pop(operation.key, None)
            _clear_rwkv_review_queue_score_cache()
            discard_queue_scores = True
        if (
            _reviewer_backend_warmup_pending_generations.get(operation.key)
            == operation.generation
        ):
            _reviewer_backend_warmup_pending_generations.pop(operation.key, None)
    if discard_queue_scores:
        try:
            _clear_rwkv_review_queue_scores(operation.reviewer)
        except Exception:
            logger.exception(
                "failed to clear RWKV queue scores after temporary state loss"
            )


@contextmanager
def _temporary_reviewer_backend_operation(
    reviewer: object,
    backend: RwkvReviewerBackend,
    *,
    cache_snapshot: Callable[[], _T],
    restore_cache_snapshot: Callable[[_T], object],
    restore_required: Callable[[], bool] | None = None,
) -> Iterator[tuple[_ReviewerBackendTemporaryOperation, _T] | None]:
    operation = _claim_reviewer_backend_temporary_operation(reviewer, backend)
    if operation is None:
        yield None
        return

    snapshots: list[_T] = []
    state_safe = False
    previous_prediction_backend = getattr(
        _reviewer_backend_prediction_local,
        "backend",
        None,
    )
    _reviewer_backend_prediction_local.backend = backend
    try:
        operation.require_current()
        snapshots.append(cache_snapshot())
        operation.require_current()
        yield operation, snapshots[0]
    finally:
        try:
            should_restore = restore_required is None or restore_required()
            if snapshots and should_restore:
                restore_cache_snapshot(snapshots[0])
                state_safe = True
            elif snapshots:
                state_safe = True
        finally:
            try:
                _reviewer_backend_execution_lock.release()
            finally:
                try:
                    _finish_reviewer_backend_temporary_operation(
                        operation,
                        restored=state_safe,
                    )
                finally:
                    if previous_prediction_backend is None:
                        delattr(_reviewer_backend_prediction_local, "backend")
                    else:
                        _reviewer_backend_prediction_local.backend = (
                            previous_prediction_backend
                        )


def configure_reviewer_backend_from_environment() -> bool:
    with _reviewer_backend_state_lock:
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

    backend = _reviewer_backend
    if backend is None:
        return states

    try:
        curve_enabled = rwkv_review_enabled(reviewer, card)
        review_active = rwkv_review_active(reviewer, card)
        if review_active and not _prepare_reviewer_backend_for_review(reviewer):
            logger.debug("RWKV scheduling prediction skipped: warm-up pending")
            return states

        with _try_reviewer_backend_prediction_access(
            expected_backend=backend,
        ) as current_backend:
            if current_backend is None:
                logger.debug("RWKV scheduling prediction skipped: backend busy")
                return states
            if review_active and not _reviewer_backend_warmed_up(reviewer):
                logger.debug(
                    "RWKV scheduling prediction skipped: state changed before access"
                )
                return states
            state_generation = _reviewer_backend_state_generation(current_backend)
            if curve_enabled:
                predict_curve = getattr(
                    current_backend,
                    "predict_review_curve",
                    None,
                )
                predict_curve_uncached = getattr(
                    current_backend,
                    "predict_review_uncached",
                    None,
                )
                prediction = (
                    predict_curve(reviewer=reviewer, card=card)
                    if callable(predict_curve)
                    else (
                        predict_curve_uncached(reviewer=reviewer, card=card)
                        if callable(predict_curve_uncached)
                        else current_backend.predict_review(
                            reviewer=reviewer,
                            card=card,
                        )
                    )
                )
            else:
                predict_retrievability = getattr(
                    current_backend,
                    "predict_review_retrievability",
                    None,
                )
                prediction = (
                    predict_retrievability(reviewer=reviewer, card=card)
                    if callable(predict_retrievability)
                    else current_backend.predict_review(reviewer=reviewer, card=card)
                )
            if prediction is None:
                return states

            with _reviewer_backend_state_lock:
                if (
                    _reviewer_backend is not current_backend
                    or _reviewer_backend_state_generation(current_backend)
                    != state_generation
                    or review_active
                    and not _reviewer_backend_warmed_up(reviewer)
                ):
                    logger.debug(
                        "RWKV scheduling prediction discarded: backend state changed"
                    )
                    return states
                _validate_prediction(prediction)
                has_interval_overrides = _has_interval_overrides(
                    prediction.interval_overrides
                )
                _store_reviewer_prediction(
                    reviewer,
                    card,
                    prediction,
                    review_enabled=review_active,
                    interval_override_used=curve_enabled and has_interval_overrides,
                )
                if curve_enabled and has_interval_overrides:
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

    card_id = _card_id(card)
    collection_backend = getattr(_collection(reviewer), "_backend", None)
    try:
        backend = _reviewer_backend
        if backend is None:
            return

        if not _reviewer_backend_execution_lock.acquire(blocking=False):
            _invalidate_reviewer_backend_state(
                reviewer,
                reason="review answered while RWKV backend was busy",
            )
            logger.debug("RWKV answer update skipped: backend busy")
            return
        try:
            if _reviewer_backend is not backend or _reviewer_backend_warmup_pending(
                reviewer
            ):
                _invalidate_reviewer_backend_state(
                    reviewer,
                    reason="review answered while RWKV state was pending",
                )
                logger.debug("RWKV answer update skipped: state pending")
                return
            if rwkv_review_active(
                reviewer, card
            ) and not _prepare_reviewer_backend_for_review(reviewer):
                _invalidate_reviewer_backend_state(
                    reviewer,
                    reason="review answered before warm-up completed",
                )
                logger.debug("RWKV answer update skipped: warm-up pending")
                return
            backend.review_answered(
                reviewer=reviewer,
                card=card,
                ease=ease,
            )
            _mark_reviewer_backend_identity_unknown(
                reviewer,
                reason="review answered",
            )
            if card_id is not None:
                _invalidate_resolved_preset_id_cache(reviewer, card_ids=[card_id])
        finally:
            _reviewer_backend_execution_lock.release()
    except Exception:
        _invalidate_reviewer_backend_state(
            reviewer,
            reason="review answer update failed",
        )
        logger.exception("RWKV review state update failed")
    finally:
        if card_id is not None:
            try:
                _set_rwkv_card_info_score(
                    reviewer,
                    card_id,
                    None,
                    collection_backend=collection_backend,
                )
            except Exception:
                logger.exception("failed to clear answered card RWKV info score")
        pending = getattr(reviewer, _REVIEWER_PENDING_ANSWER_STATE_ATTR, None)
        if isinstance(pending, _RwkvPendingAnswerState) and pending.card_id == _card_id(
            card
        ):
            if pending.ease == ease:
                synthetic_states = getattr(
                    reviewer,
                    _REVIEWER_SYNTHETIC_ANSWER_STATES_ATTR,
                    None,
                )
                if not isinstance(synthetic_states, dict):
                    synthetic_states = {}
                    setattr(
                        reviewer,
                        _REVIEWER_SYNTHETIC_ANSWER_STATES_ATTR,
                        synthetic_states,
                    )
                if pending.review_state != pending.base_review_state:
                    synthetic_states[pending.card_id] = _RwkvSyntheticAnswerState(
                        ease=pending.ease,
                        review_state=pending.review_state,
                        answered_at_millis=pending.answered_at_millis,
                    )
                else:
                    synthetic_states.pop(pending.card_id, None)
            delattr(reviewer, _REVIEWER_PENDING_ANSWER_STATE_ATTR)


def refresh_answered_card_queue_score(
    reviewer: object,
    card: object,
) -> None:
    """Refresh the answered card's installed RWKV queue score after state changes."""

    deck_id = _current_deck_id(reviewer)
    card_id = _card_id(card)
    if deck_id is None or card_id is None:
        return

    existing_scores = _rwkv_review_queue_score_map_for_deck(reviewer, deck_id)
    if existing_scores is None:
        return

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_instant_order_enabled(deck_config)
    ):
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return

    try:
        score_result = _rwkv_review_queue_score_result(
            reviewer=reviewer,
            card_ids=[card_id],
            batch_size=_rwkv_review_batch_size(deck_config),
        )
    except Exception:
        logger.exception("RWKV answered-card queue score refresh failed")
        score_result = RwkvReviewQueueScoreResult(scores=[])

    updated_score: float | None = None
    updated_target_retention: float | None = None
    for scored_card_id, retrievability in score_result.scores:
        if scored_card_id == card_id:
            updated_score = retrievability
            updated_target_retention = score_result.target_retentions_by_card_id.get(
                card_id
            )
            break

    if _patch_answered_card_rwkv_review_queue_score(
        reviewer,
        deck_id,
        card_id,
        updated_score,
        target_retention=updated_target_retention,
    ):
        return

    updated_scores = dict(existing_scores)
    if updated_score is None:
        updated_scores.pop(card_id, None)
    else:
        updated_scores[card_id] = updated_score
    existing_target_retentions = _rwkv_review_queue_target_map_for_deck(
        reviewer,
        deck_id,
    )
    updated_target_retentions = dict(existing_target_retentions or {})
    if updated_target_retention is None:
        updated_target_retentions.pop(card_id, None)
    else:
        updated_target_retentions[card_id] = updated_target_retention
    _set_rwkv_review_queue_scores(
        reviewer,
        deck_id,
        sorted(updated_scores.items()),
        target_retentions_by_card_id=updated_target_retentions,
        fresh_for_backend_state=False,
    )


def invalidate_reviewer_queue_for_card_answer(
    reviewer: object,
    card: object,
) -> None:
    """Drop the in-memory study queue before answering a non-queued reviewer card."""

    deck_id = _deck_id(card)
    current_deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        deck_id = current_deck_id
    if deck_id is None:
        deck_id = 0

    existing_scores = _rwkv_review_queue_score_map_for_deck(reviewer, deck_id)
    if (
        existing_scores is None
        and current_deck_id is not None
        and current_deck_id != deck_id
    ):
        current_deck_scores = _rwkv_review_queue_score_map_for_deck(
            reviewer,
            current_deck_id,
        )
        if current_deck_scores is not None:
            deck_id = current_deck_id
            existing_scores = current_deck_scores
    scores = sorted(existing_scores.items()) if existing_scores is not None else []
    _set_rwkv_review_queue_scores(
        reviewer,
        deck_id,
        scores,
        target_retentions_by_card_id=_rwkv_review_queue_target_map_for_deck(
            reviewer,
            deck_id,
        ),
    )


def _prepare_current_deck_review_queue_scores(
    reviewer: object,
    *,
    reason: str,
) -> None:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        _clear_rwkv_review_queue_scores(reviewer)
        return

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_instant_order_enabled(deck_config)
    ):
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return

    _prepare_rwkv_review_scores_for_deck(
        reviewer=reviewer,
        deck_id=deck_id,
        deck_config=deck_config,
        reason=reason,
    )


def prepare_current_deck_review_queue_scores(
    mw: object,
    *,
    reason: str = "deck counts",
) -> None:
    """Prepare transient RWKV review scores before deck counts are queried."""

    _prepare_current_deck_review_queue_scores(SimpleNamespace(mw=mw), reason=reason)


def prepare_reviewer_queue_order(reviewer: object) -> None:
    """Prepare transient RWKV review ordering scores for the current deck."""

    _prepare_current_deck_review_queue_scores(reviewer, reason="review queue")


def prepare_reviewer_queue_order_async_work(
    reviewer: object,
    *,
    reason: str = "review queue",
) -> RwkvReviewQueueOrderAsyncWork | None:
    """Build immutable RWKV queue scoring work while holding collection access.

    The returned work can be scored on a non-collection worker. Installation must
    still validate the backend state generation before replacing queue scores.
    """

    start = time.monotonic()
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        _clear_rwkv_review_queue_scores(reviewer)
        return None

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_instant_order_enabled(deck_config)
    ):
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return None

    if _reviewer_backend is None:
        configure_start = time.monotonic()
        configure_reviewer_backend_from_environment()
        logger.debug(
            "RWKV async %s backend configure finished: deck_id=%s backend=%s "
            "elapsed_ms=%.1f",
            reason,
            deck_id,
            type(_reviewer_backend).__name__ if _reviewer_backend is not None else None,
            (time.monotonic() - configure_start) * 1000,
        )
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
        backend_assignment_generation = _reviewer_backend_assignment_generation
    if backend is None:
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return None

    if not callable(getattr(backend, "cached_review_input_predictions", None)):
        logger.debug(
            "RWKV async %s scoring skipped: deck_id=%s input_scoring=False",
            reason,
            deck_id,
        )
        return None

    try:
        warmup_start = time.monotonic()
        warmed_up = _prepare_reviewer_backend_for_review(reviewer)
        warmup_elapsed_ms = (time.monotonic() - warmup_start) * 1000
        with _reviewer_backend_state_lock:
            backend_is_current = (
                _reviewer_backend is backend
                and _reviewer_backend_assignment_generation
                == backend_assignment_generation
            )
        if not warmed_up or not backend_is_current:
            _clear_rwkv_review_queue_scores(reviewer, deck_id)
            logger.debug(
                "RWKV async %s scoring skipped: deck_id=%s warmed_up=%s "
                "backend_current=%s warmup_elapsed_ms=%.1f",
                reason,
                deck_id,
                warmed_up,
                backend_is_current,
                warmup_elapsed_ms,
            )
            return None

        batch_size = _rwkv_review_batch_size(deck_config)
        state_generation = _reviewer_backend_state_generation(backend)
        context = _rwkv_review_queue_context(reviewer, deck_id)
        if context is None:
            logger.debug(
                "RWKV async %s scoring skipped: deck_id=%s queue_context=False",
                reason,
                deck_id,
            )
            return None
        candidate_work = _candidate_refreshed_rwkv_review_queue_async_work(
            reviewer=reviewer,
            deck_id=deck_id,
            deck_config=deck_config,
            reason=reason,
            batch_size=batch_size,
            state_generation=state_generation,
            backend_assignment_generation=backend_assignment_generation,
            context=context,
            warmup_elapsed_ms=warmup_elapsed_ms,
            start=start,
            expected_backend=backend,
        )
        if candidate_work is not None:
            return candidate_work

        input_build = _rwkv_review_input_batches_for_deck_review_queue(
            reviewer=reviewer,
            deck_id=deck_id,
            batch_size_override=batch_size,
            include_new_cards=_new_gather_uses_retrievability(deck_config),
        )
        if input_build is None:
            logger.debug(
                "RWKV async %s scoring skipped: deck_id=%s deck_input_build=False",
                reason,
                deck_id,
            )
            return None

        return _rwkv_review_queue_async_work_from_input_build(
            reviewer=reviewer,
            deck_id=deck_id,
            reason=reason,
            batch_size=batch_size,
            state_generation=state_generation,
            backend_assignment_generation=backend_assignment_generation,
            context=context,
            input_build=input_build,
            warmup_elapsed_ms=warmup_elapsed_ms,
            build_start=start,
            fresh_for_backend_state=True,
            expected_backend=backend,
        )
    except Exception:
        logger.exception("RWKV async %s scoring preparation failed", reason)
        _clear_rwkv_review_queue_scores(reviewer, deck_id)
        return None


def score_reviewer_queue_order_async_work(
    work: RwkvReviewQueueOrderAsyncWork,
) -> RwkvReviewQueueOrderAsyncResult:
    if not _rwkv_review_queue_async_collection_is_current(work):
        return _aborted_reviewer_queue_order_async_result(work)
    with _try_reviewer_backend_prediction_access(
        expected_backend=work.backend,
        expected_backend_assignment_generation=(work.backend_assignment_generation),
        expected_state_generation=work.state_generation,
        expected_resident_state_key=work.resident_state_key,
        expected_resident_state_generation=work.resident_state_generation,
    ) as backend:
        if backend is None:
            logger.debug(
                "RWKV async %s scoring skipped: deck_id=%s backend busy or stale",
                work.reason,
                work.deck_id,
            )
            return _aborted_reviewer_queue_order_async_result(work)
        result = _score_reviewer_queue_order_async_work_with_backend(work, backend)
        if not _reviewer_backend_prediction_access_is_current(
            backend,
            expected_backend_assignment_generation=(work.backend_assignment_generation),
            expected_state_generation=work.state_generation,
            expected_resident_state_key=work.resident_state_key,
            expected_resident_state_generation=work.resident_state_generation,
        ) or not _rwkv_review_queue_async_collection_is_current(work):
            return _aborted_reviewer_queue_order_async_result(work)
        return result


def _rwkv_review_queue_async_collection_is_current(
    work: RwkvReviewQueueOrderAsyncWork | RwkvReviewQueueOrderAsyncResult,
) -> bool:
    collection_owner = getattr(work, "collection_owner", None)
    collection = getattr(work, "collection", None)
    collection_backend = getattr(work, "collection_backend", None)
    if collection_owner is None and collection is None and collection_backend is None:
        return True
    return (
        collection_owner is not None
        and getattr(collection_owner, "col", None) is collection
        and getattr(collection, "_backend", None) is collection_backend
    )


def _score_reviewer_queue_order_async_work_with_backend(
    work: RwkvReviewQueueOrderAsyncWork,
    backend: RwkvReviewerBackend,
) -> RwkvReviewQueueOrderAsyncResult:
    def work_is_current() -> bool:
        return _reviewer_backend_prediction_access_is_current(
            backend,
            expected_backend_assignment_generation=(work.backend_assignment_generation),
            expected_state_generation=work.state_generation,
            expected_resident_state_key=work.resident_state_key,
            expected_resident_state_generation=work.resident_state_generation,
        ) and _rwkv_review_queue_async_collection_is_current(work)

    start = time.monotonic()
    predictions = list(work.predictions)
    requests_by_index = list(work.requests_by_index)
    resident_inputs_by_index = list(work.resident_inputs_by_index)
    if resident_inputs_by_index:
        if _reviewer_backend_state_generation(backend) == work.state_generation:
            resident_predictions = _predict_retrievability_inputs_from_warm_up_uncached(
                [review_input for _, review_input in resident_inputs_by_index],
                backend=backend,
            )
            if not work_is_current():
                return _aborted_reviewer_queue_order_async_result(work)
            if len(resident_predictions) != len(resident_inputs_by_index):
                raise ValueError("RWKV resident prediction count mismatch")
            for (index, _), prediction in zip(
                resident_inputs_by_index,
                resident_predictions,
                strict=True,
            ):
                predictions[index] = prediction
        else:
            logger.debug(
                "RWKV async %s resident scoring aborted after state advance: "
                "deck_id=%s state_generation=%s current_generation=%s",
                work.reason,
                work.deck_id,
                work.state_generation,
                _reviewer_backend_state_generation(backend),
            )

    runtime_batch_size = min(
        _rwkv_retrievability_batch_size(work.batch_size),
        _MIN_RWKV_REVIEW_BATCH_SIZE,
    )
    for missing_offset in range(0, len(requests_by_index), runtime_batch_size):
        if _reviewer_backend_state_generation(backend) != work.state_generation:
            logger.debug(
                "RWKV async %s scoring aborted after state advance: deck_id=%s "
                "missing_offset=%s state_generation=%s current_generation=%s",
                work.reason,
                work.deck_id,
                missing_offset,
                work.state_generation,
                _reviewer_backend_state_generation(backend),
            )
            break

        batch_requests_by_index = requests_by_index[
            missing_offset : missing_offset + runtime_batch_size
        ]
        batch_start = time.monotonic()
        logger.debug(
            "RWKV async review input runtime batch started: deck_id=%s "
            "missing_offset=%s size=%s batch_size=%s configured_batch_size=%s "
            "cache_hits=%s",
            work.deck_id,
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            work.batch_size,
            work.cache_hits,
        )
        batch_predictions = _predict_retrievability_requests_uncached(
            [request for _, request in batch_requests_by_index],
            backend=backend,
        )
        if not work_is_current():
            return _aborted_reviewer_queue_order_async_result(work)
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
            "RWKV async review input runtime batch processed: deck_id=%s "
            "missing_offset=%s size=%s batch_size=%s configured_batch_size=%s "
            "predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            work.deck_id,
            missing_offset,
            len(batch_requests_by_index),
            runtime_batch_size,
            work.batch_size,
            batch_predict_elapsed_ms,
            (time.monotonic() - batch_start) * 1000,
        )

    scores = _scores_from_input_predictions(
        work.inputs_by_card_id,
        predictions,
    )
    target_retentions_by_card_id = (
        _rwkv_review_input_build_target_retentions_by_card_id(work.input_build)
    )
    if work.existing_scores is not None:
        fresh_scores_by_card_id = dict(scores)
        merged_scores = dict(work.existing_scores)
        for card_id in work.candidate_card_ids:
            if card_id in fresh_scores_by_card_id:
                merged_scores[card_id] = fresh_scores_by_card_id[card_id]
            else:
                merged_scores.pop(card_id, None)
        scores = sorted(merged_scores.items())

        merged_target_retentions = (
            dict(work.existing_target_retentions)
            if work.existing_target_retentions is not None
            else {}
        )
        for card_id in work.candidate_card_ids:
            target_retention = target_retentions_by_card_id.get(card_id)
            if target_retention is not None and card_id in fresh_scores_by_card_id:
                merged_target_retentions[card_id] = target_retention
            else:
                merged_target_retentions.pop(card_id, None)
        target_retentions_by_card_id = merged_target_retentions

    score_elapsed_ms = (time.monotonic() - start) * 1000
    logger.debug(
        "RWKV async %s inputs scored: deck_id=%s searched=%s loaded=%s "
        "with_state=%s enabled=%s inputs=%s scored=%s deck_configs=%s "
        "batch_size=%s cache_hits=%s runtime_requests=%s load_elapsed_ms=%.1f "
        "candidate_elapsed_ms=%.1f prediction_elapsed_ms=%.1f",
        work.reason,
        work.deck_id,
        work.input_build.searched_rows,
        work.input_build.parsed_cards,
        work.input_build.cards_with_state,
        work.input_build.eligible_cards,
        len(work.inputs_by_card_id),
        len(scores),
        work.input_build.deck_configs,
        work.batch_size,
        work.cache_hits,
        len(work.requests_by_index) + len(work.resident_inputs_by_index),
        work.input_build.load_elapsed_ms,
        work.input_build.candidate_elapsed_ms,
        score_elapsed_ms,
    )
    return RwkvReviewQueueOrderAsyncResult(
        context=work.context,
        deck_id=work.deck_id,
        reason=work.reason,
        state_generation=work.state_generation,
        scores=tuple(scores),
        input_build=work.input_build,
        cache_hits=work.cache_hits,
        runtime_requests=len(work.requests_by_index)
        + len(work.resident_inputs_by_index),
        warmup_elapsed_ms=work.warmup_elapsed_ms,
        build_elapsed_ms=work.build_elapsed_ms,
        score_elapsed_ms=score_elapsed_ms,
        prediction_cache_entries=tuple(
            (review_input, prediction)
            for (_, review_input), prediction in zip(
                work.inputs_by_card_id,
                predictions,
                strict=True,
            )
        ),
        target_retentions_by_card_id=target_retentions_by_card_id,
        existing_scores=work.existing_scores,
        existing_target_retentions=work.existing_target_retentions,
        candidate_card_ids=work.candidate_card_ids,
        fresh_for_backend_state=work.fresh_for_backend_state,
        backend=backend,
        backend_assignment_generation=work.backend_assignment_generation,
        collection_owner=work.collection_owner,
        collection=work.collection,
        collection_backend=work.collection_backend,
        resident_state_key=work.resident_state_key,
        resident_state_generation=work.resident_state_generation,
    )


def _aborted_reviewer_queue_order_async_result(
    work: RwkvReviewQueueOrderAsyncWork,
) -> RwkvReviewQueueOrderAsyncResult:
    return RwkvReviewQueueOrderAsyncResult(
        context=work.context,
        deck_id=work.deck_id,
        reason=work.reason,
        state_generation=-1,
        scores=(),
        input_build=work.input_build,
        cache_hits=work.cache_hits,
        runtime_requests=0,
        warmup_elapsed_ms=work.warmup_elapsed_ms,
        build_elapsed_ms=work.build_elapsed_ms,
        score_elapsed_ms=0.0,
        existing_scores=work.existing_scores,
        existing_target_retentions=work.existing_target_retentions,
        candidate_card_ids=work.candidate_card_ids,
        fresh_for_backend_state=work.fresh_for_backend_state,
        backend=work.backend,
        backend_assignment_generation=work.backend_assignment_generation,
        collection_owner=work.collection_owner,
        collection=work.collection,
        collection_backend=work.collection_backend,
        resident_state_key=work.resident_state_key,
        resident_state_generation=work.resident_state_generation,
    )


def install_reviewer_queue_order_async_result(
    reviewer: object,
    result: RwkvReviewQueueOrderAsyncResult,
) -> bool:
    if not _rwkv_review_queue_async_collection_is_current(result):
        return False
    with _try_reviewer_backend_prediction_access(
        expected_backend=result.backend,
        expected_backend_assignment_generation=(result.backend_assignment_generation),
        expected_state_generation=result.state_generation,
        expected_resident_state_key=result.resident_state_key,
        expected_resident_state_generation=result.resident_state_generation,
    ) as backend:
        if backend is None or not _rwkv_review_queue_order_async_result_is_current(
            reviewer,
            result,
        ):
            return False

        if not _cache_reviewer_queue_order_async_result_predictions(result):
            return False
        if _rwkv_review_queue_context(reviewer, result.deck_id) != result.context:
            logger.debug(
                "RWKV async %s scores discarded after queue context changed "
                "during cache install: deck_id=%s",
                result.reason,
                result.deck_id,
            )
            return False

        def result_is_current() -> bool:
            return (
                _reviewer_backend_prediction_access_is_current(
                    backend,
                    expected_backend_assignment_generation=(
                        result.backend_assignment_generation
                    ),
                    expected_state_generation=result.state_generation,
                    expected_resident_state_key=result.resident_state_key,
                    expected_resident_state_generation=(
                        result.resident_state_generation
                    ),
                )
                and _rwkv_review_queue_context_epochs_are_current(result.context)
                and _rwkv_review_queue_async_collection_is_current(result)
            )

        set_start = time.monotonic()
        if not result_is_current():
            return False
        if not _set_rwkv_review_queue_scores(
            reviewer,
            result.deck_id,
            result.scores,
            target_retentions_by_card_id=result.target_retentions_by_card_id,
            fresh_for_backend_state=result.fresh_for_backend_state,
            collection_backend=result.collection_backend,
            collection=result.collection,
            collection_owner=result.collection_owner,
            score_config_key=(
                _rwkv_review_queue_score_config_key_from_context(result.context)
            ),
            is_current=result_is_current,
        ):
            return False
        logger.debug(
            "installed RWKV async %s scores: deck_id=%s scored=%s "
            "warmup_elapsed_ms=%.1f build_elapsed_ms=%.1f score_elapsed_ms=%.1f "
            "set_elapsed_ms=%.1f total_elapsed_ms=%.1f",
            result.reason,
            result.deck_id,
            len(result.scores),
            result.warmup_elapsed_ms,
            result.build_elapsed_ms,
            result.score_elapsed_ms,
            (time.monotonic() - set_start) * 1000,
            result.build_elapsed_ms + result.score_elapsed_ms,
        )
        return True


def cache_reviewer_queue_order_async_result_predictions(
    reviewer: object,
    result: RwkvReviewQueueOrderAsyncResult,
) -> bool:
    if not _rwkv_review_queue_order_async_result_is_current(reviewer, result):
        return False

    return _cache_reviewer_queue_order_async_result_predictions(result)


def _rwkv_review_queue_order_async_result_is_current(
    reviewer: object,
    result: RwkvReviewQueueOrderAsyncResult,
) -> bool:
    if not _rwkv_review_queue_async_collection_is_current(result):
        logger.debug(
            "RWKV async %s scores discarded after collection changed: deck_id=%s",
            result.reason,
            result.deck_id,
        )
        return False
    result_backend = getattr(result, "backend", None)
    if result_backend is not None and _reviewer_backend is not result_backend:
        logger.debug(
            "RWKV async %s scores discarded after backend changed: deck_id=%s",
            result.reason,
            result.deck_id,
        )
        return False

    result_backend_assignment_generation = getattr(
        result,
        "backend_assignment_generation",
        None,
    )
    if (
        result_backend_assignment_generation is not None
        and result_backend_assignment_generation
        != _reviewer_backend_assignment_generation
    ):
        logger.debug(
            "RWKV async %s scores discarded after backend reset: deck_id=%s",
            result.reason,
            result.deck_id,
        )
        return False

    current_context = _rwkv_review_queue_context(reviewer, result.deck_id)
    if current_context != result.context:
        logger.debug(
            "RWKV async %s scores discarded after queue context changed: "
            "deck_id=%s prepared_context=%s current_context=%s scored=%s",
            result.reason,
            result.deck_id,
            result.context,
            current_context,
            len(result.scores),
        )
        return False

    current_generation = _reviewer_backend_state_generation()
    if current_generation != result.state_generation:
        logger.debug(
            "RWKV async %s scores discarded after state advance: deck_id=%s "
            "state_generation=%s current_generation=%s scored=%s",
            result.reason,
            result.deck_id,
            result.state_generation,
            current_generation,
            len(result.scores),
        )
        return False

    return True


def _cache_reviewer_queue_order_async_result_predictions(
    result: RwkvReviewQueueOrderAsyncResult,
) -> bool:
    result_backend = getattr(result, "backend", None)
    prediction_cache_entries = getattr(result, "prediction_cache_entries", ())
    if result_backend is None and _reviewer_backend is None:
        return True
    with _try_reviewer_backend_prediction_access(
        expected_backend=result_backend,
        expected_backend_assignment_generation=getattr(
            result,
            "backend_assignment_generation",
            None,
        ),
        expected_state_generation=result.state_generation,
        expected_resident_state_key=getattr(result, "resident_state_key", None),
        expected_resident_state_generation=getattr(
            result,
            "resident_state_generation",
            None,
        ),
    ) as backend:
        if backend is None:
            logger.debug(
                "RWKV async %s prediction cache skipped: deck_id=%s "
                "backend busy or stale",
                result.reason,
                result.deck_id,
            )
            return False
        cache_predictions = getattr(
            backend,
            "cache_review_input_predictions",
            None,
        )
        if not callable(cache_predictions) or not prediction_cache_entries:
            return True

        cache_predictions(prediction_cache_entries)
        if not _reviewer_backend_prediction_access_is_current(
            backend,
            expected_backend_assignment_generation=getattr(
                result,
                "backend_assignment_generation",
                None,
            ),
            expected_state_generation=result.state_generation,
            expected_resident_state_key=getattr(result, "resident_state_key", None),
            expected_resident_state_generation=getattr(
                result,
                "resident_state_generation",
                None,
            ),
        ):
            return False
        logger.debug(
            "cached RWKV async %s predictions: deck_id=%s entries=%s",
            result.reason,
            result.deck_id,
            len(prediction_cache_entries),
        )
        return True


def reviewer_queue_order_enabled(reviewer: object) -> bool:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        return False

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return isinstance(deck_config, dict) and _rwkv_review_instant_order_enabled(
        deck_config
    )


def reviewer_queue_order_refresh_due(reviewer: object) -> bool:
    card = getattr(reviewer, "card", None)
    deck_config = _rwkv_review_active_deck_config(reviewer, card)
    if deck_config is None or not _rwkv_review_instant_order_enabled(deck_config):
        return False

    answered_count = len(_session_answered_ids(reviewer))
    interval = _rwkv_review_refresh_interval(deck_config)
    return answered_count > 0 and answered_count % interval == 0


def reviewer_queue_order_refresh_before_next_card(reviewer: object) -> bool:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        return False

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return isinstance(deck_config, dict) and _new_gather_uses_retrievability(
        deck_config
    )


def reviewer_queue_order_refresh_on_exit_enabled(reviewer: object) -> bool:
    deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        return False

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    return (
        isinstance(deck_config, dict)
        and _rwkv_review_instant_order_enabled(deck_config)
        and _rwkv_review_refresh_on_exit(deck_config)
    )


def reviewer_queue_order_exit_refresh_needed(reviewer: object) -> bool:
    """Return whether an enabled exit refresh lacks a current queue-score map."""

    if not reviewer_queue_order_refresh_on_exit_enabled(reviewer):
        return False

    deck_id = _current_deck_id(reviewer)
    return (
        deck_id is not None
        and _rwkv_review_queue_score_generations.get(deck_id)
        != _reviewer_backend_state_generation()
    )


def _search_uses_rwkv_instant_due(search: str) -> bool:
    return _RWKV_INSTANT_DUE_SEARCH_PATTERN.search(search) is not None


def _search_uses_rwkv_curve_due(search: str) -> bool:
    return _RWKV_CURVE_DUE_SEARCH_PATTERN.search(search) is not None


def _search_uses_rwkv_curve_retrievability(search: str) -> bool:
    return _RWKV_CURVE_R_SEARCH_PATTERN.search(search) is not None


def prepare_stats_retrievability_scores(  # noqa: PLR0911
    reviewer: object,
    search: str,
    *,
    warm_up_if_needed: bool = False,
    prepare_instant_due: bool = False,
    prepare_curve_due: bool = False,
    prepare_curve_retrievability: bool = False,
) -> RwkvStatsPreparationStatus:
    """Prepare transient RWKV scores for cards matched by a stats graph search."""

    prepare_instant_due = prepare_instant_due or _search_uses_rwkv_instant_due(search)
    prepare_curve_due = prepare_curve_due or _search_uses_rwkv_curve_due(search)
    prepare_curve_retrievability = (
        prepare_curve_retrievability or _search_uses_rwkv_curve_retrievability(search)
    )

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
        return RwkvStatsPreparationStatus.UNAVAILABLE

    start = time.monotonic()
    prepare_key: RwkvStatsPrepareKey | None = None
    prepare_future: Future[RwkvStatsPreparationStatus] | None = None
    prepare_generation: int | None = None
    state_token: _ReviewerBackendPredictionStateToken | None = None
    owns_prepare = False
    prepare_status = RwkvStatsPreparationStatus.FAILED
    try:
        logger.debug("RWKV stats preparation started: search=%r", search)
        warmup_start = time.monotonic()
        prepare_backend = (
            _prepare_reviewer_backend_for_filtered_deck
            if warm_up_if_needed
            else _prepare_reviewer_backend_for_stats
        )
        warmed_up = prepare_backend(reviewer)
        if not warmed_up and _reviewer_backend_warmup_pending(reviewer):
            warmed_up = _wait_for_reviewer_backend_warmup(
                reviewer,
                timeout_secs=(
                    None if warm_up_if_needed else _RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS
                ),
            )
            if warmed_up:
                warmed_up = prepare_backend(reviewer)
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
            if warm_up_if_needed and _reviewer_backend is not None:
                return RwkvStatsPreparationStatus.FAILED
            return (
                RwkvStatsPreparationStatus.PENDING
                if _reviewer_backend_warmup_pending(reviewer)
                else RwkvStatsPreparationStatus.UNAVAILABLE
            )
        state_token = _capture_reviewer_backend_prediction_state_token(reviewer)
        if state_token is None:
            logger.debug(
                "RWKV stats retrievability scoring skipped: "
                "backend state unavailable search=%r",
                search,
            )
            return RwkvStatsPreparationStatus.FAILED
        prepare_key = _rwkv_stats_prepare_key(
            reviewer,
            search,
            state_token=state_token,
            prepare_instant_due=prepare_instant_due,
            prepare_curve_due=prepare_curve_due,
            prepare_curve_retrievability=prepare_curve_retrievability,
        )
        prepare_generation = state_token.state_generation
        if prepare_key is not None:
            prepare_future, owns_prepare = _begin_rwkv_stats_prepare(prepare_key)
            if not owns_prepare:
                wait_start = time.monotonic()
                logger.debug(
                    "RWKV stats preparation waiting for in-flight result: search=%r",
                    search,
                )
                prepare_status = prepare_future.result()
                logger.debug(
                    "RWKV stats preparation reused in-flight result: search=%r "
                    "status=%s elapsed_ms=%.1f",
                    search,
                    prepare_status.value,
                    (time.monotonic() - wait_start) * 1000,
                )
                return (
                    prepare_status
                    if _reviewer_backend_prediction_state_token_is_current(state_token)
                    else RwkvStatsPreparationStatus.FAILED
                )
        search_score_start = time.monotonic()
        search_score_result = _rwkv_stats_graph_scores_for_search(
            reviewer=reviewer,
            search=search,
            state_token=state_token,
            prepare_instant_due=prepare_instant_due,
            prepare_curve_due=prepare_curve_due,
            prepare_curve_retrievability=prepare_curve_retrievability,
        )
        search_score_elapsed_ms = (time.monotonic() - search_score_start) * 1000
        if search_score_result is not None:
            scores = search_score_result.scores
            input_build = search_score_result.input_build
            set_start = time.monotonic()
            if not _set_rwkv_stats_graph_scores_if_current(
                reviewer,
                search,
                scores,
                state_token=state_token,
                target_retentions_by_card_id=(
                    search_score_result.target_retentions_by_card_id
                ),
                intervening_reviews_by_card_id=(
                    search_score_result.intervening_reviews_by_card_id
                ),
                curve_due_card_ids=search_score_result.curve_due_card_ids,
                curve_retrievabilities_by_card_id=dict(
                    search_score_result.curve_scores
                ),
            ):
                if _reviewer_backend_prediction_state_token_is_current(state_token):
                    prepare_status = RwkvStatsPreparationStatus.PENDING
                    logger.debug(
                        "deferred RWKV stats scores while backend was busy: search=%r",
                        search,
                    )
                    return prepare_status
                logger.debug(
                    "discarded RWKV stats scores after state change: search=%r",
                    search,
                )
                return RwkvStatsPreparationStatus.FAILED
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
            prepare_status = RwkvStatsPreparationStatus.READY
            return prepare_status
        if not _reviewer_backend_prediction_state_token_is_current(state_token):
            logger.debug(
                "discarded RWKV stats search fallback after state change: search=%r",
                search,
            )
            return RwkvStatsPreparationStatus.FAILED
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
        scores = _rwkv_stats_graph_scores(
            reviewer=reviewer,
            card_ids=card_ids,
            include_new_cards=_search_text_explicitly_includes_new_cards(search),
            state_token=state_token,
        )
        score_elapsed_ms = (time.monotonic() - score_start) * 1000
        set_start = time.monotonic()
        if not _set_rwkv_stats_graph_scores_if_current(
            reviewer,
            search,
            scores,
            state_token=state_token,
        ):
            if _reviewer_backend_prediction_state_token_is_current(state_token):
                prepare_status = RwkvStatsPreparationStatus.PENDING
                logger.debug(
                    "deferred RWKV stats scores while backend was busy: search=%r",
                    search,
                )
                return prepare_status
            logger.debug(
                "discarded RWKV stats scores after state change: search=%r",
                search,
            )
            return RwkvStatsPreparationStatus.FAILED
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
        prepare_status = RwkvStatsPreparationStatus.READY
        return prepare_status
    except _ReviewerBackendPredictionBusy:
        prepare_status = RwkvStatsPreparationStatus.PENDING
        logger.debug("RWKV stats retrievability scoring deferred: backend busy")
        return prepare_status
    except _ReviewerBackendPredictionAborted:
        logger.debug("RWKV stats retrievability scoring aborted: backend stale")
        return RwkvStatsPreparationStatus.FAILED
    except Exception:
        logger.exception("RWKV stats retrievability scoring failed")
        if state_token is None:
            if _rwkv_stats_prepare_generation_is_current(prepare_generation):
                _set_rwkv_stats_graph_scores(reviewer, search, [])
        else:
            _set_rwkv_stats_graph_scores_if_current(
                reviewer,
                search,
                [],
                state_token=state_token,
            )
        return RwkvStatsPreparationStatus.FAILED
    finally:
        if owns_prepare and prepare_key is not None and prepare_future is not None:
            _finish_rwkv_stats_prepare(
                prepare_key,
                prepare_future,
                prepare_status,
            )


def prepare_filtered_deck_retrievability_scores(
    reviewer: object,
    config: FilteredDeckConfig,
) -> RwkvStatsPreparationStatus | None:
    """Prepare RWKV scores needed by a filtered deck's filters and ordering."""

    terms = [term for term in config.search_terms[:2] if term.limit > 0]
    prepare_instant_due = any(
        _search_uses_rwkv_instant_due(term.search) for term in terms
    )
    prepare_curve_due = any(_search_uses_rwkv_curve_due(term.search) for term in terms)
    prepare_curve_retrievability = any(
        _search_uses_rwkv_curve_retrievability(term.search) for term in terms
    )
    needs_scores = (
        any(
            term.order in _FILTERED_DECK_RETRIEVABILITY_ORDERS
            or _RWKV_INSTANT_R_SEARCH_PATTERN.search(term.search)
            for term in terms
        )
        or prepare_instant_due
        or prepare_curve_due
        or prepare_curve_retrievability
    )
    if not needs_scores:
        return None

    col = _collection(reviewer)
    build_search_string = getattr(col, "build_search_string", None)
    if not callable(build_search_string):
        return RwkvStatsPreparationStatus.UNAVAILABLE

    try:
        search = build_search_string(
            *(term.search for term in terms),
            joiner="OR",
        )
    except Exception:
        logger.debug(
            "failed to build RWKV filtered-deck candidate search", exc_info=True
        )
        return RwkvStatsPreparationStatus.FAILED

    return prepare_stats_retrievability_scores(
        reviewer,
        search,
        warm_up_if_needed=True,
        prepare_instant_due=prepare_instant_due,
        prepare_curve_due=prepare_curve_due,
        prepare_curve_retrievability=prepare_curve_retrievability,
    )


def _prepare_reviewer_backend_for_stats(reviewer: object) -> bool:
    """Use cached RWKV state for stats without building it inside /graphs."""

    if _reviewer_backend is None:
        return True
    if not _reviewer_backend_cacheable():
        return _warm_up_reviewer_backend(reviewer)

    return _prepare_reviewer_backend_from_cache(reviewer)


def _prepare_reviewer_backend_for_filtered_deck(reviewer: object) -> bool:
    """Ensure a filtered-deck rebuild does not race a cold RWKV backend."""

    if _reviewer_backend is None or _reviewer_backend_warmed_up(reviewer):
        return True
    if _reviewer_backend_warmup_pending(reviewer):
        return False
    return _warm_up_reviewer_backend(reviewer)


def _prepare_reviewer_backend_for_card_info(reviewer: object) -> bool:
    """Use an already-warmed or cached RWKV state for Card Info diagnostics."""

    if _reviewer_backend is None:
        return True
    if _reviewer_backend_warmed_up(reviewer):
        return True
    return _prepare_reviewer_backend_from_cache(reviewer)


def _prepare_reviewer_backend_for_review(reviewer: object) -> bool:
    """Use warmed or cached RWKV state for review-time intervals."""

    if _reviewer_backend is None or not callable(
        getattr(_reviewer_backend, "warm_up", None)
    ):
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

    context = _reviewer_backend_warmup_context(reviewer)
    if context is None:
        return True
    backend, key = context
    if not _reviewer_backend_cacheable(backend):
        return False

    with _reviewer_backend_state_lock:
        if _reviewer_backend is not backend:
            return False
        if key in _reviewer_backend_warmup_pending_generations:
            return False
        if key in _reviewer_backend_warmup_states:
            return True
        warmup_generation = _reviewer_backend_warmup_generations.get(key, 0)
        _reviewer_backend_warmup_pending_generations[key] = warmup_generation
    if not _acquire_reviewer_backend_execution(
        reviewer,
        backend,
        key,
        warmup_generation,
    ):
        _finish_reviewer_backend_warmup(key, warmup_generation)
        return False

    def is_current() -> bool:
        return _reviewer_backend_warmup_is_current(
            reviewer,
            backend,
            key,
            warmup_generation,
        )

    start = time.monotonic()
    try:
        restored_identity = _restore_reviewer_backend_cache(
            reviewer,
            backend=backend,
            is_current=is_current,
            progress=progress,
        )
        if not is_current():
            return False
        if restored_identity is not None and _publish_reviewer_backend_state(
            key,
            restored_identity,
            expected_generation=warmup_generation,
        ):
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
    except _ReviewerBackendWarmupInvalidated:
        return False
    finally:
        try:
            _finish_rwkv_state_cache_checkpoint_writes_safely(backend)
        finally:
            try:
                _reviewer_backend_execution_lock.release()
            finally:
                _finish_reviewer_backend_warmup(key, warmup_generation)


def _reviewer_backend_warmup_pending(reviewer: object) -> bool:
    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        return False
    with _reviewer_backend_state_lock:
        return key in _reviewer_backend_warmup_pending_generations


def _wait_for_reviewer_backend_warmup(
    reviewer: object,
    *,
    timeout_secs: float | None,
) -> bool:
    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        return True

    start = time.monotonic()
    deadline = start + timeout_secs if timeout_secs is not None else None
    while True:
        with _reviewer_backend_state_lock:
            pending = key in _reviewer_backend_warmup_pending_generations
        if not pending:
            break
        if deadline is None:
            time.sleep(_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS)
        else:
            remaining_secs = deadline - time.monotonic()
            if remaining_secs <= 0:
                logger.debug(
                    "timed out waiting for RWKV warm-up before stats: elapsed_ms=%.1f",
                    (time.monotonic() - start) * 1000,
                )
                return False
            time.sleep(min(_RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS, remaining_secs))

    with _reviewer_backend_state_lock:
        warmed_up = key in _reviewer_backend_warmup_states
    logger.debug(
        "waited for RWKV warm-up before stats: warmed_up=%s elapsed_ms=%.1f",
        warmed_up,
        (time.monotonic() - start) * 1000,
    )
    return warmed_up


def _rwkv_stats_prepare_key(
    reviewer: object,
    search: str,
    *,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
    prepare_instant_due: bool = False,
    prepare_curve_due: bool = False,
    prepare_curve_retrievability: bool = False,
) -> RwkvStatsPrepareKey | None:
    warmup_key = _reviewer_backend_warmup_key(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    if warmup_key is None or not isinstance(days_elapsed, int):
        return None

    backend_id, collection_id = warmup_key
    if state_token is not None:
        if backend_id != id(state_token.backend):
            return None
        if _collection(reviewer) is not state_token.collection:
            return None
        if (
            state_token.resident_state_key is not None
            and warmup_key != state_token.resident_state_key
        ):
            return None
    return (
        backend_id,
        collection_id,
        (
            id(state_token.collection_backend)
            if state_token is not None
            else id(getattr(_collection(reviewer), "_backend", None))
        ),
        days_elapsed,
        (
            state_token.backend_assignment_generation
            if state_token is not None
            else _reviewer_backend_assignment_generation
        ),
        (
            state_token.state_generation
            if state_token is not None
            else _reviewer_backend_state_generation()
        ),
        (
            state_token.resident_state_generation
            if state_token is not None
            and state_token.resident_state_generation is not None
            else -1
        ),
        state_token.resident_state_ready if state_token is not None else False,
        (
            state_token.dynamic_desired_retention_generation
            if state_token is not None
            else _dynamic_desired_retention_generation
        ),
        (
            state_token.study_queue_generation
            if state_token is not None
            else _rwkv_study_queue_generation
        ),
        search,
        prepare_instant_due,
        prepare_curve_due,
        prepare_curve_retrievability,
    )


def _reviewer_backend_state_generation(backend: object | None = None) -> int:
    if backend is None:
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
) -> tuple[Future[RwkvStatsPreparationStatus], bool]:
    with _rwkv_stats_prepare_lock:
        future = _rwkv_stats_prepare_in_flight.get(key)
        if future is not None:
            return future, False

        future = Future()
        _rwkv_stats_prepare_in_flight[key] = future
        return future, True


def _finish_rwkv_stats_prepare(
    key: RwkvStatsPrepareKey,
    future: Future[RwkvStatsPreparationStatus],
    status: RwkvStatsPreparationStatus,
) -> None:
    if not future.done():
        future.set_result(status)
    with _rwkv_stats_prepare_lock:
        if _rwkv_stats_prepare_in_flight.get(key) is future:
            del _rwkv_stats_prepare_in_flight[key]


def _rwkv_stats_prepare_generation_is_current(
    expected_generation: int | None,
) -> bool:
    return (
        expected_generation is None
        or expected_generation == _reviewer_backend_state_generation()
    )


def prewarm_reviewer_queue_score_cache(
    reviewer: object,
    *,
    reason: str = "reviewer",
    include_parent_scope: bool = True,
) -> None:
    """Opportunistically pre-score likely queue scopes into the RWKV memo."""

    deck_ids = _rwkv_score_prewarm_deck_ids(
        reviewer,
        include_parent_scope=include_parent_scope,
    )
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

    def finish() -> None:
        if key is not None:
            _finish_rwkv_score_prewarm(key)
        logger.debug(
            "RWKV score prewarm finished: reason=%s deck_ids=%s elapsed_ms=%.1f",
            reason,
            deck_ids,
            (time.monotonic() - start) * 1000,
        )

    taskman = getattr(getattr(reviewer, "mw", None), "taskman", None)
    run_in_background = getattr(taskman, "run_in_background", None)
    if callable(run_in_background):
        _prewarm_rwkv_review_scores_for_decks_async(
            reviewer,
            deck_ids,
            reason=reason,
            taskman=taskman,
            on_done=finish,
        )
        return

    try:
        _prewarm_rwkv_review_scores_for_decks(
            reviewer,
            deck_ids,
            reason=reason,
        )
    except Exception:
        if key is not None:
            _finish_rwkv_score_prewarm(key)
        raise
    finish()


def deck_browser_rwkv_count_scope_ids(
    mw: object,
    tree: DeckTreeNode,
) -> tuple[int, ...]:
    """Return disjoint RWKV-enabled deck scopes, prioritizing the current one."""

    reviewer = SimpleNamespace(mw=mw)
    current_deck_id = _current_deck_id(reviewer)

    def contains_deck(node: DeckTreeNode, deck_id: int | None) -> bool:
        return deck_id is not None and (
            getattr(node, "deck_id", None) == deck_id
            or any(
                contains_deck(child, deck_id) for child in getattr(node, "children", ())
            )
        )

    scopes: list[tuple[int, bool]] = []

    def collect(node: DeckTreeNode) -> None:
        deck_id = getattr(node, "deck_id", None)
        if not isinstance(deck_id, int):
            return
        deck_config = _deck_config_for_deck_id(reviewer, deck_id)
        enabled = isinstance(deck_config, dict) and _rwkv_review_instant_order_enabled(
            deck_config
        )
        if enabled:
            scopes.append((deck_id, contains_deck(node, current_deck_id)))
            return
        for child in getattr(node, "children", ()):
            collect(child)

    for child in getattr(tree, "children", ()):
        collect(child)
    scopes.sort(key=lambda scope: not scope[1])
    return tuple(deck_id for deck_id, _ in scopes)


def clear_deck_browser_rwkv_count_scores(mw: object) -> None:
    backend = getattr(getattr(mw, "col", None), "_backend", None)
    clear_scores = getattr(backend, "clear_rwkv_deck_count_scores", None)
    if callable(clear_scores):
        clear_scores()


def prepare_deck_browser_rwkv_counts_incrementally(
    mw: object,
    deck_ids: Sequence[int],
    *,
    should_continue: Callable[[], bool],
    on_update: Callable[[int, DeckTreeNode | None], None],
    on_done: Callable[[bool], None] | None = None,
) -> None:
    """Score deck scopes, reporting if unresolved loading state may be cleared."""

    reviewer = SimpleNamespace(mw=mw)
    taskman = getattr(mw, "taskman", None)
    run_in_background = getattr(taskman, "run_in_background", None)
    if not callable(run_in_background) or not deck_ids:
        if on_done is not None:
            on_done(True)
        return

    remaining_deck_ids = iter(deck_ids)
    start = time.monotonic()
    first_update_elapsed_ms: float | None = None
    updated_scopes = 0
    deferred_scopes = 0

    def finish(*, clear_pending: bool | None = None) -> None:
        if clear_pending is None:
            clear_pending = deferred_scopes == 0
        logger.debug(
            "RWKV deck browser incremental counts finished: scopes=%s updated=%s "
            "deferred=%s clear_pending=%s first_update_elapsed_ms=%s elapsed_ms=%.1f",
            len(deck_ids),
            updated_scopes,
            deferred_scopes,
            clear_pending,
            (
                f"{first_update_elapsed_ms:.1f}"
                if first_update_elapsed_ms is not None
                else None
            ),
            (time.monotonic() - start) * 1000,
        )
        if on_done is not None:
            on_done(clear_pending)

    def fail(stage: str) -> None:
        logger.exception("RWKV deck browser count %s failed", stage)
        finish(clear_pending=True)

    def prepare_next_deck() -> None:
        if not should_continue():
            finish()
            return
        deck_id = next(remaining_deck_ids, None)
        if deck_id is None:
            finish()
            return

        def prepare() -> tuple[RwkvReviewQueueOrderAsyncWork | None, bool]:
            if not should_continue():
                return None, False
            if rwkv_state_cache_loading(mw):
                return None, True
            work = _rwkv_score_prewarm_work_for_deck(
                reviewer,
                deck_id=deck_id,
                reason="deck browser counts",
            )
            return work, rwkv_state_cache_loading(mw)

        def prepared(
            future: Future[tuple[RwkvReviewQueueOrderAsyncWork | None, bool]],
        ) -> None:
            nonlocal deferred_scopes
            try:
                work, loading = future.result()
            except Exception:
                fail("preparation")
                return
            if work is None:
                if loading:
                    deferred_scopes += 1
                    logger.debug(
                        "RWKV deck browser count deferred during state cache load: "
                        "deck_id=%s",
                        deck_id,
                    )
                elif should_continue():
                    on_update(deck_id, None)
                prepare_next_deck()
                return
            if not should_continue():
                prepare_next_deck()
                return

            def score() -> RwkvReviewQueueOrderAsyncResult:
                return score_reviewer_queue_order_async_work(work)

            def scored(future: Future[RwkvReviewQueueOrderAsyncResult]) -> None:
                try:
                    result = future.result()
                except Exception:
                    fail("scoring")
                    return
                if not should_continue():
                    finish()
                    return

                def install() -> DeckTreeNode | None:
                    if not _rwkv_review_queue_async_collection_is_current(result):
                        return None
                    result_backend = getattr(result, "backend", None)
                    resident_state_key = getattr(
                        result,
                        "resident_state_key",
                        None,
                    )
                    resident_state_generation = getattr(
                        result,
                        "resident_state_generation",
                        None,
                    )
                    install_start = time.monotonic()
                    if result_backend is None and _reviewer_backend is None:
                        current_context = _rwkv_review_queue_context(
                            reviewer,
                            result.deck_id,
                        )

                        def stateless_result_is_current() -> bool:
                            return (
                                _rwkv_review_queue_async_collection_is_current(result)
                                and result.state_generation
                                == _reviewer_backend_state_generation()
                                and _rwkv_review_queue_context_epochs_are_current(
                                    result.context
                                )
                                and _rwkv_review_queue_context(
                                    reviewer,
                                    result.deck_id,
                                )
                                == result.context
                            )

                        if (
                            result.state_generation
                            != _reviewer_backend_state_generation()
                            or current_context != result.context
                            or not _rwkv_review_queue_context_epochs_are_current(
                                result.context
                            )
                            or not _rwkv_review_queue_async_collection_is_current(
                                result
                            )
                        ):
                            return None
                        if not _set_rwkv_deck_count_scores(
                            reviewer,
                            result.deck_id,
                            result.scores,
                            target_retentions_by_card_id=(
                                result.target_retentions_by_card_id
                            ),
                            collection_backend=getattr(
                                result,
                                "collection_backend",
                                None,
                            ),
                            collection=getattr(result, "collection", None),
                            collection_owner=getattr(
                                result,
                                "collection_owner",
                                None,
                            ),
                            is_current=stateless_result_is_current,
                        ):
                            return None
                    else:
                        with _try_reviewer_backend_prediction_access(
                            expected_backend=result_backend,
                            expected_backend_assignment_generation=getattr(
                                result,
                                "backend_assignment_generation",
                                None,
                            ),
                            expected_state_generation=result.state_generation,
                            expected_resident_state_key=resident_state_key,
                            expected_resident_state_generation=(
                                resident_state_generation
                            ),
                        ) as backend:
                            if (
                                backend is None
                                or _rwkv_review_queue_context(
                                    reviewer,
                                    result.deck_id,
                                )
                                != result.context
                                or not _cache_reviewer_queue_order_async_result_predictions(
                                    result
                                )
                                or _rwkv_review_queue_context(
                                    reviewer,
                                    result.deck_id,
                                )
                                != result.context
                            ):
                                return None

                            def stateful_result_is_current() -> bool:
                                return (
                                    _reviewer_backend_prediction_access_is_current(
                                        backend,
                                        expected_backend_assignment_generation=getattr(
                                            result,
                                            "backend_assignment_generation",
                                            None,
                                        ),
                                        expected_state_generation=(
                                            result.state_generation
                                        ),
                                        expected_resident_state_key=(
                                            resident_state_key
                                        ),
                                        expected_resident_state_generation=(
                                            resident_state_generation
                                        ),
                                    )
                                    and _rwkv_review_queue_context_epochs_are_current(
                                        result.context
                                    )
                                    and _rwkv_review_queue_async_collection_is_current(
                                        result
                                    )
                                    and _rwkv_review_queue_context(
                                        reviewer,
                                        result.deck_id,
                                    )
                                    == result.context
                                )

                            if not stateful_result_is_current():
                                return None
                            if not _set_rwkv_deck_count_scores(
                                reviewer,
                                result.deck_id,
                                result.scores,
                                target_retentions_by_card_id=(
                                    result.target_retentions_by_card_id
                                ),
                                collection_backend=getattr(
                                    result,
                                    "collection_backend",
                                    None,
                                ),
                                collection=getattr(result, "collection", None),
                                collection_owner=getattr(
                                    result,
                                    "collection_owner",
                                    None,
                                ),
                                is_current=stateful_result_is_current,
                            ):
                                return None
                    set_elapsed_ms = (time.monotonic() - install_start) * 1000
                    if not _rwkv_review_queue_async_collection_is_current(result):
                        return None
                    result_collection_value = getattr(result, "collection", None)
                    result_collection = (
                        result_collection_value
                        if result_collection_value is not None
                        else getattr(mw, "col")
                    )
                    tree = getattr(result_collection, "sched").deck_due_tree()
                    logger.debug(
                        "RWKV deck browser count scope installed: deck_id=%s "
                        "scored=%s set_elapsed_ms=%.1f tree_elapsed_ms=%.1f",
                        result.deck_id,
                        len(result.scores),
                        set_elapsed_ms,
                        (time.monotonic() - install_start) * 1000 - set_elapsed_ms,
                    )
                    return tree

                def installed(future: Future[DeckTreeNode | None]) -> None:
                    nonlocal first_update_elapsed_ms, updated_scopes
                    try:
                        tree = future.result()
                    except Exception:
                        fail("installation")
                        return
                    if should_continue():
                        if tree is not None:
                            updated_scopes += 1
                            if first_update_elapsed_ms is None:
                                first_update_elapsed_ms = (
                                    time.monotonic() - start
                                ) * 1000
                        on_update(result.deck_id, tree)
                    prepare_next_deck()

                run_in_background(install, installed, uses_collection=True)

            run_in_background(score, scored, uses_collection=False)

        run_in_background(prepare, prepared, uses_collection=True)

    prepare_next_deck()


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


def _rwkv_score_prewarm_deck_ids(
    reviewer: object,
    *,
    include_parent_scope: bool = True,
) -> list[int]:
    current_deck_id = _current_deck_id(reviewer)
    if current_deck_id is None:
        return []

    deck_ids = [current_deck_id]
    if not include_parent_scope:
        return deck_ids

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
            and _rwkv_review_instant_order_enabled(deck_config)
        ):
            continue

        deck_scores = _rwkv_review_queue_scores_for_deck(
            reviewer=reviewer,
            deck_id=deck_id,
            batch_size=_rwkv_review_batch_size(deck_config),
            include_new_cards=_new_gather_uses_retrievability(deck_config),
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


def _prewarm_rwkv_review_scores_for_decks_async(
    reviewer: object,
    deck_ids: Sequence[int],
    *,
    reason: str,
    taskman: object,
    on_done: Callable[[], None],
) -> None:
    """Prewarm one deck scope at a time without holding collection access while scoring."""

    run_in_background = getattr(taskman, "run_in_background")
    remaining_deck_ids = iter(deck_ids)
    total_candidates = 0
    total_scored = 0
    start = time.monotonic()

    def fail(stage: str) -> None:
        logger.exception(
            "RWKV score prewarm %s failed: reason=%s deck_ids=%s",
            stage,
            reason,
            list(deck_ids),
        )
        on_done()

    def prepare_next_deck() -> None:
        nonlocal total_candidates, total_scored
        deck_id = next(remaining_deck_ids, None)
        if deck_id is None:
            logger.debug(
                "RWKV score prewarm scored: reason=%s deck_ids=%s "
                "candidates=%s scored=%s elapsed_ms=%.1f",
                reason,
                list(deck_ids),
                total_candidates,
                total_scored,
                (time.monotonic() - start) * 1000,
            )
            on_done()
            return

        def prepare() -> RwkvReviewQueueOrderAsyncWork | None:
            return _rwkv_score_prewarm_work_for_deck(
                reviewer,
                deck_id=deck_id,
                reason=reason,
            )

        def prepared(future: Future[RwkvReviewQueueOrderAsyncWork | None]) -> None:
            nonlocal total_candidates
            try:
                work = future.result()
            except Exception:
                fail("preparation")
                return
            if work is None:
                prepare_next_deck()
                return

            total_candidates += work.input_build.searched_rows

            def score() -> RwkvReviewQueueOrderAsyncResult:
                return score_reviewer_queue_order_async_work(work)

            def scored(future: Future[RwkvReviewQueueOrderAsyncResult]) -> None:
                nonlocal total_scored
                try:
                    result = future.result()
                except Exception:
                    fail("scoring")
                    return

                def cache_predictions() -> bool:
                    return cache_reviewer_queue_order_async_result_predictions(
                        reviewer,
                        result,
                    )

                def predictions_cached(future: Future[bool]) -> None:
                    nonlocal total_scored
                    try:
                        future.result()
                    except Exception:
                        fail("prediction caching")
                        return
                    total_scored += len(result.scores)
                    prepare_next_deck()

                run_in_background(
                    cache_predictions,
                    predictions_cached,
                    uses_collection=True,
                )

            run_in_background(score, scored, uses_collection=False)

        run_in_background(prepare, prepared, uses_collection=True)

    prepare_next_deck()


def _rwkv_score_prewarm_work_for_deck(
    reviewer: object,
    *,
    deck_id: int,
    reason: str,
) -> RwkvReviewQueueOrderAsyncWork | None:
    """Snapshot optional prewarm work; backend failures intentionally skip fallback."""

    if _reviewer_backend is None:
        configure_reviewer_backend_from_environment()
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
        backend_assignment_generation = _reviewer_backend_assignment_generation
    if backend is None or not callable(
        getattr(backend, "cached_review_input_predictions", None)
    ):
        return None
    if not _prepare_reviewer_backend_for_stats(reviewer):
        logger.debug(
            "RWKV score prewarm skipped: reason=%s deck_id=%s warmed_up=False",
            reason,
            deck_id,
        )
        return None
    with _reviewer_backend_state_lock:
        if (
            _reviewer_backend is not backend
            or _reviewer_backend_assignment_generation != backend_assignment_generation
        ):
            return None
        state_generation = _reviewer_backend_state_generation(backend)

    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not (
        isinstance(deck_config, dict)
        and _rwkv_review_instant_order_enabled(deck_config)
    ):
        return None

    start = time.monotonic()
    batch_size = _rwkv_review_batch_size(deck_config)
    input_build = _rwkv_review_input_batches_for_deck_review_queue(
        reviewer=reviewer,
        deck_id=deck_id,
        batch_size_override=batch_size,
        include_new_cards=_new_gather_uses_retrievability(deck_config),
    )
    if input_build is None:
        logger.debug(
            "RWKV score prewarm skipped after backend input failure: "
            "reason=%s deck_id=%s",
            reason,
            deck_id,
        )
        return None

    context = _rwkv_review_queue_context(reviewer, deck_id)
    if context is None:
        return None

    return _rwkv_review_queue_async_work_from_input_build(
        reviewer=reviewer,
        deck_id=deck_id,
        reason=f"score prewarm ({reason})",
        batch_size=batch_size,
        state_generation=state_generation,
        backend_assignment_generation=backend_assignment_generation,
        context=context,
        input_build=input_build,
        warmup_elapsed_ms=0.0,
        build_start=start,
        fresh_for_backend_state=False,
        expected_backend=backend,
    )


def _reviewer_backend_cacheable(
    backend: RwkvReviewerBackend | None = None,
) -> bool:
    if backend is None:
        with _reviewer_backend_state_lock:
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
        batch_size = _rwkv_review_batch_size(deck_config)
        candidate_scores = _candidate_refreshed_rwkv_review_queue_scores_for_deck(
            reviewer=reviewer,
            deck_id=deck_id,
            deck_config=deck_config,
            batch_size=batch_size,
        )
        deck_scores_elapsed_ms = (time.monotonic() - deck_scores_start) * 1000
        if candidate_scores is not None:
            scores, target_retentions_by_card_id, candidates, scored = candidate_scores
            set_start = time.monotonic()
            _set_rwkv_review_queue_scores(
                reviewer,
                deck_id,
                scores,
                target_retentions_by_card_id=target_retentions_by_card_id,
                fresh_for_backend_state=False,
            )
            set_elapsed_ms = (time.monotonic() - set_start) * 1000
            logger.debug(
                "prepared RWKV %s scores from candidate refresh: deck_id=%s "
                "candidates=%s scored=%s retained=%s warmup_elapsed_ms=%.1f "
                "scores_elapsed_ms=%.1f set_elapsed_ms=%.1f elapsed_ms=%.1f",
                reason,
                deck_id,
                candidates,
                scored,
                len(scores),
                warmup_elapsed_ms,
                deck_scores_elapsed_ms,
                set_elapsed_ms,
                (time.monotonic() - start) * 1000,
            )
            return

        deck_scores_start = time.monotonic()
        deck_scores = _rwkv_review_queue_scores_for_deck(
            reviewer=reviewer,
            deck_id=deck_id,
            batch_size=batch_size,
            include_new_cards=_new_gather_uses_retrievability(deck_config),
        )
        deck_scores_elapsed_ms = (time.monotonic() - deck_scores_start) * 1000
        if deck_scores is not None:
            scores, input_build = deck_scores
            set_start = time.monotonic()
            _set_rwkv_review_queue_scores(
                reviewer,
                deck_id,
                scores,
                target_retentions_by_card_id=_rwkv_review_input_build_target_retentions_by_card_id(
                    input_build
                ),
            )
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
        score_result = _rwkv_review_queue_score_result(
            reviewer=reviewer,
            card_ids=card_ids,
            batch_size=batch_size,
        )
        scores_elapsed_ms = (time.monotonic() - scores_start) * 1000

        set_start = time.monotonic()
        _set_rwkv_review_queue_scores(
            reviewer,
            deck_id,
            score_result.scores,
            target_retentions_by_card_id=score_result.target_retentions_by_card_id,
        )
        set_elapsed_ms = (time.monotonic() - set_start) * 1000
        logger.debug(
            "prepared RWKV %s scores: deck_id=%s candidates=%s scored=%s "
            "warmup_elapsed_ms=%.1f card_ids_elapsed_ms=%.1f "
            "scores_elapsed_ms=%.1f set_elapsed_ms=%.1f elapsed_ms=%.1f",
            reason,
            deck_id,
            len(card_ids),
            len(score_result.scores),
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
        button_probabilities=prediction.button_probabilities,
        s90_overrides=prediction.s90_overrides,
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
    include_after_review: bool = True,
) -> list[tuple[str, str]]:
    card_id = _card_id(card)
    if not rwkv_review_active(reviewer, card):
        diagnostics = current_reviewer_diagnostics(
            reviewer,
            card,
            fallback_source=fallback_source,
        )
        if diagnostics is not None:
            if card_id is not None:
                _set_rwkv_card_info_score(reviewer, card_id, None)
            return _card_info_diagnostic_rows(diagnostics)
        if card_id is not None:
            _set_rwkv_card_info_score(reviewer, card_id, None)
        return []

    if _reviewer_backend is None:
        configure_reviewer_backend_from_environment()
    candidate = _card_info_review_candidate(reviewer, card)
    queried_diagnostics = _queried_card_info_diagnostics(
        reviewer,
        card,
        fallback_source=fallback_source,
        _candidate=candidate,
    )
    if queried_diagnostics is None:
        diagnostics = RwkvReviewerDiagnostics(
            retrievability=None,
            retrievability_source=_unavailable_retrievability_source(fallback_source),
        )
        if card_id is not None:
            _set_rwkv_card_info_score(reviewer, card_id, None)
    else:
        diagnostics = queried_diagnostics

    diagnostics = _with_card_info_prediction_details(diagnostics)

    rows = _card_info_diagnostic_rows(diagnostics)
    if rwkv_review_enabled(reviewer, card):
        rows.extend(
            _rwkv_card_info_next_s90_rows(
                states=_scheduling_states(candidate.reviewer),
                rwkv_s90_overrides=diagnostics.s90_overrides,
            )
        )
    if include_after_review and rwkv_review_active(reviewer, card):
        rows.extend(
            rwkv_card_info_after_review_rows(
                reviewer,
                card,
                _candidate=candidate,
            )
        )
    return rows


def _card_info_diagnostic_rows(
    diagnostics: RwkvReviewerDiagnostics,
) -> list[tuple[str, str]]:
    rows = [
        ("RWKV computed R", _format_retrievability(diagnostics.retrievability)),
    ]
    if diagnostics.button_probabilities is not None:
        rows.append(
            (
                "RWKV : Answer Button Probability",
                _format_button_probabilities(diagnostics.button_probabilities),
            )
        )
    rows.append(("Retrievability source", diagnostics.retrievability_source))
    return rows


def _with_card_info_prediction_details(
    diagnostics: RwkvReviewerDiagnostics,
) -> RwkvReviewerDiagnostics:
    button_probabilities = diagnostics.button_probabilities
    if button_probabilities is not None and diagnostics.retrievability is not None:
        button_probabilities = _rwkv_button_probabilities_with_retrievability(
            button_probabilities,
            diagnostics.retrievability,
        )

    return RwkvReviewerDiagnostics(
        retrievability=diagnostics.retrievability,
        retrievability_source=diagnostics.retrievability_source,
        button_probabilities=button_probabilities,
        s90_overrides=diagnostics.s90_overrides,
    )


def _rwkv_card_info_next_s90_rows(
    *,
    states: SchedulingStates | None,
    rwkv_s90_overrides: RwkvIntervalOverride,
) -> list[tuple[str, str]]:
    rwkv_values = tuple(
        getattr(rwkv_s90_overrides, rating) for rating in _RWKV_RATING_FIELDS
    )
    fsrs_values = (
        tuple(
            _s90_for_scheduling_state(getattr(states, rating))
            for rating in _RWKV_RATING_FIELDS
        )
        if states is not None
        else (None, None, None, None)
    )
    return [
        ("RWKV Curve Next S90", _format_next_s90_values(rwkv_values)),
        ("FSRS Next S90", _format_next_s90_values(fsrs_values)),
    ]


def _s90_for_scheduling_state(state: SchedulingState) -> float | None:
    state_kind = state.WhichOneof("kind")
    if state_kind == "normal":
        return _s90_for_normal_scheduling_state(state.normal)
    if state_kind == "filtered" and state.filtered.WhichOneof("kind") == "rescheduling":
        return _s90_for_normal_scheduling_state(
            state.filtered.rescheduling.original_state
        )
    return None


def _s90_for_normal_scheduling_state(normal: Any) -> float | None:
    normal_kind = normal.WhichOneof("kind")
    if normal_kind == "learning":
        return _s90_from_memory_state(normal.learning.memory_state)
    if normal_kind == "review":
        return _s90_from_memory_state(normal.review.memory_state)
    if normal_kind == "relearning":
        return _s90_from_memory_state(
            normal.relearning.learning.memory_state
        ) or _s90_from_memory_state(normal.relearning.review.memory_state)
    return None


def _s90_from_memory_state(memory_state: object) -> float | None:
    value = getattr(memory_state, "stability", None)
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    ):
        return float(value)
    return None


def _format_next_s90_values(values: Sequence[float | int | None]) -> str:
    return " ".join(
        f"{label}:{_format_s90_days(value)}"
        for label, value in zip(
            ("Again", "Hard", "Good", "Easy"),
            values,
            strict=True,
        )
    )


def _format_s90_days(value: float | int | None) -> str:
    if value is None or not math.isfinite(value) or value <= 0:
        return "Unavailable"
    amount = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{amount}d"


def rwkv_card_info_after_review_row(
    reviewer: object,
    card: object,
) -> tuple[str, str]:
    """Return the immediate post-review row for compatibility with callers."""

    return rwkv_card_info_after_review_rows(reviewer, card)[0]


def rwkv_card_info_after_review_rows(
    reviewer: object,
    card: object,
    *,
    _candidate: RwkvReviewCandidate | None = None,
) -> list[tuple[str, str]]:
    try:
        backend = _reviewer_backend
        if backend is None:
            return _unavailable_rwkv_card_info_after_review_rows()

        candidate = _candidate or _card_info_review_candidate(reviewer, card)
        if not rwkv_review_active(candidate.reviewer, candidate.card):
            return _unavailable_rwkv_card_info_after_review_rows()
        if not _prepare_reviewer_backend_for_card_info(reviewer):
            logger.debug(
                "RWKV card info after-review prediction skipped: warm-up pending"
            )
            return _unavailable_rwkv_card_info_after_review_rows()

        card_id = _card_id(candidate.card)
        if card_id is None:
            return _unavailable_rwkv_card_info_after_review_rows()

        identity = rwkv_review_identity(candidate.reviewer, candidate.card)
        if identity is None:
            return _unavailable_rwkv_card_info_after_review_rows()

        query_input = rwkv_review_input(
            reviewer=candidate.reviewer,
            card=candidate.card,
            identity=identity,
            ease=None,
        )
        labeled_eases = (
            ("Again", 1),
            ("Hard", 2),
            ("Good", 3),
            ("Easy", 4),
        )
        answer_inputs = [
            replace(
                query_input,
                is_query=False,
                ease=ease,
                duration_millis=None,
            )
            for _, ease in labeled_eases
        ]
        horizon_inputs = [
            (
                horizon_index,
                replace(
                    query_input,
                    current_elapsed_days=0,
                    current_elapsed_seconds=elapsed_seconds,
                ),
            )
            for horizon_index, (_, elapsed_seconds) in enumerate(
                _RWKV_AFTER_REVIEW_HORIZONS
            )
        ]
        score_batches = _rwkv_card_info_after_review_score_batches(
            reviewer=reviewer,
            backend=backend,
            answer_inputs=answer_inputs,
            horizon_inputs=horizon_inputs,
        )
        if score_batches is None or len(score_batches) != len(labeled_eases):
            return _unavailable_rwkv_card_info_after_review_rows()

        rows: list[tuple[str, str]] = []
        for horizon_index, (row_label, _) in enumerate(_RWKV_AFTER_REVIEW_HORIZONS):
            values: list[str] = []
            have_prediction = False
            for (rating_label, _), scores in zip(
                labeled_eases,
                score_batches,
                strict=True,
            ):
                score_by_horizon = dict(scores or [])
                retrievability = score_by_horizon.get(horizon_index)
                have_prediction = have_prediction or retrievability is not None
                values.append(
                    f"{rating_label}:{_format_retrievability(retrievability)}"
                )

            rows.append(
                (
                    row_label,
                    " ".join(values)
                    if have_prediction
                    else _unavailable_rwkv_card_info_after_review_value(),
                )
            )

        return rows
    except Exception:
        logger.exception("RWKV card info after-review prediction failed")
        return _unavailable_rwkv_card_info_after_review_rows()


def _rwkv_card_info_after_review_score_batches(
    *,
    reviewer: object,
    backend: RwkvReviewerBackend,
    answer_inputs: Sequence[RwkvReviewInput],
    horizon_inputs: Sequence[tuple[int, RwkvReviewInput]],
) -> Sequence[Sequence[tuple[int, float]]] | None:
    with _try_reviewer_backend_prediction_access(
        expected_backend=backend,
    ) as current_backend:
        if current_backend is None or not _reviewer_backend_warmed_up(reviewer):
            logger.debug(
                "RWKV card info after-review prediction skipped: backend busy "
                "or state changed"
            )
            return None
        predict_from_warm_up = getattr(
            current_backend,
            "predict_retrievability_after_reviews_from_warm_up",
            None,
        )
        state_generation = _reviewer_backend_state_generation(current_backend)
        if callable(predict_from_warm_up):
            score_batches = cast(
                Sequence[Sequence[tuple[int, float]]] | None,
                predict_from_warm_up(
                    answers=answer_inputs,
                    inputs_by_card_id=horizon_inputs,
                ),
            )
            if score_batches is not None:
                if not _reviewer_backend_prediction_access_is_current(
                    current_backend,
                    expected_state_generation=state_generation,
                ) or not _reviewer_backend_warmed_up(reviewer):
                    return None
                return score_batches

        cache_snapshot = getattr(current_backend, "cache_snapshot", None)
        predict_after_reviews = getattr(
            current_backend,
            "predict_retrievability_after_reviews",
            None,
        )
        if not callable(cache_snapshot) or not callable(predict_after_reviews):
            return None

        snapshot = cache_snapshot()
        score_batches = cast(
            Sequence[Sequence[tuple[int, float]]] | None,
            predict_after_reviews(
                answers=answer_inputs,
                inputs_by_card_id=horizon_inputs,
                snapshot=snapshot,
            ),
        )
        if not _reviewer_backend_prediction_access_is_current(
            current_backend,
            expected_state_generation=state_generation,
        ) or not _reviewer_backend_warmed_up(reviewer):
            return None
        return score_batches


def _unavailable_rwkv_card_info_after_review_value() -> str:
    return "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable"


def _unavailable_rwkv_card_info_after_review_rows() -> list[tuple[str, str]]:
    return [
        (label, _unavailable_rwkv_card_info_after_review_value())
        for label, _ in _RWKV_AFTER_REVIEW_HORIZONS
    ]


def _unavailable_rwkv_card_info_after_review_row() -> tuple[str, str]:
    return _unavailable_rwkv_card_info_after_review_rows()[0]


def rwkv_review_enabled(
    reviewer: object,
    card: object,
) -> bool:
    deck_config = _deck_config_for_deck_id(reviewer, _deck_id(card))
    return isinstance(deck_config, dict) and _rwkv_review_config_enabled(deck_config)


def rwkv_review_active(
    reviewer: object,
    card: object,
) -> bool:
    return _rwkv_review_active_deck_config(reviewer, card) is not None


def _rwkv_collection_config_state(
    reviewer: object,
) -> _RwkvCollectionConfigState:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if not callable(all_config):
        return _RwkvCollectionConfigState(False, False)

    try:
        configs = all_config()
    except Exception:
        logger.debug("failed to read deck configs for RWKV collection state")
        return _RwkvCollectionConfigState(False, False)

    review_enabled = False
    dynamic_preset_replay_enabled = False
    for config in configs:
        if not isinstance(config, dict) or not _rwkv_review_config_active(config):
            continue
        review_enabled = True
        if _rwkv_review_dynamic_preset_replay(config):
            dynamic_preset_replay_enabled = True

    return _RwkvCollectionConfigState(
        review_enabled=review_enabled,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
    )


def _rwkv_review_active_deck_config(
    reviewer: object,
    card: object,
) -> dict[str, object] | None:
    deck_id = _deck_id(card)
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not isinstance(deck_config, dict):
        return None

    return deck_config if _rwkv_review_config_active(deck_config) else None


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
    _use_pending_answer: bool = True,
) -> RwkvReviewInput:
    pending = getattr(reviewer, _REVIEWER_PENDING_ANSWER_STATE_ATTR, None)
    if (
        _use_pending_answer
        and ease is not None
        and isinstance(pending, _RwkvPendingAnswerState)
        and pending.card_id == identity.card_id
        and pending.ease == ease
    ):
        return pending.review_input

    current_state = _current_scheduling_state(reviewer)
    state_kind, normal_state_kind = _scheduling_state_kinds(current_state)
    elapsed_days, elapsed_seconds = _scheduling_state_elapsed(current_state)
    deck_config = _rwkv_review_active_deck_config(reviewer, card)
    if (
        normal_state_kind == "new"
        and elapsed_seconds is None
        and isinstance(deck_config, dict)
        and _rwkv_review_first_review_elapsed_from_card_creation(deck_config)
    ):
        elapsed_seconds = _elapsed_seconds_since_card_created(reviewer, card)
        elapsed_days = (
            elapsed_seconds // 86_400 if elapsed_seconds is not None else None
        )

    base_review_state = _rwkv_review_state_for_scheduling_state(
        state_kind=state_kind,
        normal_state_kind=normal_state_kind,
        card_type=_int_attr(card, "type"),
    )
    review_state = base_review_state
    if isinstance(deck_config, dict):
        review_state = _rwkv_review_state_for_live_context(
            reviewer,
            card,
            base_review_state=base_review_state,
        )

    if review_state in (
        int(RwkvReviewState.REVIEW),
        int(RwkvReviewState.RELEARNING),
        int(RwkvReviewState.FILTERED),
    ):
        elapsed_days, elapsed_seconds = _elapsed_since_card_last_review(
            reviewer,
            card,
        )

    if review_state != base_review_state:
        state_kind, normal_state_kind = _rwkv_review_state_kinds(review_state)
    card_queue = _int_attr(card, "queue")
    if ease is not None or review_state != base_review_state:
        card_queue = _rwkv_review_queue_for_state(review_state, card_queue)

    return RwkvReviewInput(
        identity=identity,
        is_query=ease is None,
        ease=ease,
        duration_millis=_duration_millis(card, ease),
        card_type=review_state,
        card_queue=card_queue,
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
        enforce_grade_order=(
            not isinstance(deck_config, dict)
            or _rwkv_review_enforce_grade_order_config(deck_config)
        ),
    )


def _rwkv_review_state_for_scheduling_state(
    *,
    state_kind: str | None,
    normal_state_kind: str | None,
    card_type: int | None,
) -> int | None:
    if state_kind == "filtered":
        return int(RwkvReviewState.FILTERED)

    normal_states = {
        "new": RwkvReviewState.LEARN_START,
        "learning": RwkvReviewState.LEARNING,
        "review": RwkvReviewState.REVIEW,
        "relearning": RwkvReviewState.RELEARNING,
    }
    if (state := normal_states.get(normal_state_kind)) is not None:
        return int(state)

    # Anki's persistent card types happen to match dataset states 0 through 3.
    return card_type


def _rwkv_review_state_for_live_context(
    reviewer: object,
    card: object,
    *,
    base_review_state: int | None,
    answered_at_millis: int | None = None,
) -> int | None:
    if base_review_state != int(RwkvReviewState.REVIEW):
        return base_review_state

    card_id = _card_id(card)
    if card_id is None:
        return base_review_state
    previous = _latest_eligible_review_for_card(reviewer, card_id)
    if previous is None:
        return base_review_state
    previous_id, previous_ease, previous_kind = previous
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not isinstance(days_elapsed, int) or not isinstance(next_day_at, int):
        return base_review_state

    if answered_at_millis is None or answered_at_millis <= 0:
        now = getattr(timing, "now", None)
        answered_at_millis = (
            now * 1000 if isinstance(now, int) else int(time.time() * 1000)
        )
    if _historical_review_day_offset(
        previous_id,
        days_elapsed=days_elapsed,
        next_day_at=next_day_at,
    ) != _historical_review_day_offset(
        answered_at_millis,
        days_elapsed=days_elapsed,
        next_day_at=next_day_at,
    ):
        return base_review_state

    if previous_kind == 1:
        return int(
            RwkvReviewState.RELEARNING
            if previous_ease == 1
            else RwkvReviewState.FILTERED
        )
    if previous_kind == 2:
        return int(
            RwkvReviewState.RELEARNING
            if previous_ease in (1, 2)
            else RwkvReviewState.FILTERED
        )
    if previous_kind == 0:
        return int(RwkvReviewState.FILTERED)
    if previous_kind == 3:
        synthetic_states = getattr(
            reviewer,
            _REVIEWER_SYNTHETIC_ANSWER_STATES_ATTR,
            None,
        )
        synthetic = (
            synthetic_states.get(card_id)
            if isinstance(synthetic_states, dict)
            else None
        )
        if (
            isinstance(synthetic, _RwkvSyntheticAnswerState)
            and synthetic.review_state == int(RwkvReviewState.FILTERED)
            and abs(synthetic.answered_at_millis - previous_id) <= 1_000
        ):
            return int(RwkvReviewState.FILTERED)
    return base_review_state


def _latest_eligible_review_for_card(
    reviewer: object,
    card_id: int,
) -> tuple[int, int, int] | None:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    first = getattr(db, "first", None)
    if not callable(first):
        return None

    try:
        row = first(
            f"""
select id, ease, type
from revlog
where cid = ?
  and {_rwkv_historical_answer_sql_condition()}
order by id desc
limit 1
""",
            card_id,
        )
    except Exception:
        logger.debug("failed to load latest eligible RWKV review for card %s", card_id)
        return None
    if not isinstance(row, Sequence) or len(row) != 3:
        return None
    review_id, ease, review_kind = row
    if not all(isinstance(value, int) for value in row):
        return None
    return review_id, ease, review_kind


def _rwkv_review_state_kinds(review_state: int | None) -> tuple[str | None, str | None]:
    if review_state == int(RwkvReviewState.LEARN_START):
        return "normal", "new"
    if review_state == int(RwkvReviewState.LEARNING):
        return "normal", "learning"
    if review_state == int(RwkvReviewState.REVIEW):
        return "normal", "review"
    if review_state == int(RwkvReviewState.RELEARNING):
        return "normal", "relearning"
    if review_state == int(RwkvReviewState.FILTERED):
        return "filtered", None
    return None, None


def _rwkv_review_queue_for_state(
    review_state: int | None,
    fallback: int | None,
) -> int | None:
    if review_state in (
        int(RwkvReviewState.LEARN_START),
        int(RwkvReviewState.LEARNING),
    ):
        return int(QUEUE_TYPE_LRN)
    if review_state == int(RwkvReviewState.RELEARNING):
        return int(QUEUE_TYPE_DAY_LEARN_RELEARN)
    if review_state in (
        int(RwkvReviewState.REVIEW),
        int(RwkvReviewState.FILTERED),
    ):
        return int(QUEUE_TYPE_REV)
    return fallback


def _rwkv_raw_review_kind(review_state: int | None) -> int | None:
    if review_state in (
        int(RwkvReviewState.LEARN_START),
        int(RwkvReviewState.LEARNING),
    ):
        return 0
    if review_state == int(RwkvReviewState.REVIEW):
        return 1
    if review_state == int(RwkvReviewState.RELEARNING):
        return 2
    if review_state == int(RwkvReviewState.FILTERED):
        return 3
    return None


def _rwkv_answer_input(
    reviewer: object,
    card: object,
    ease: int,
) -> RwkvReviewInput | None:
    identity = rwkv_review_identity(reviewer, card)
    if identity is None:
        return None
    return rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=identity,
        ease=ease,
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

    return max_interval_days


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
    if prediction.curve_retrievability is not None and not _valid_probability(
        prediction.curve_retrievability
    ):
        raise ValueError("curve retrievability must be between 0 and 1")
    if prediction.button_probabilities is not None and not _valid_button_probabilities(
        prediction.button_probabilities
    ):
        raise ValueError("button probabilities must be four values between 0 and 1")
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
            button_probabilities=prediction.button_probabilities,
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


def _rwkv_pending_answer_review_input(
    *,
    answer: object,
    reviewer: object,
    card: object,
    ease: int,
    deck_config: dict[str, object],
    review_state: int,
    answered_at_millis: int,
) -> RwkvReviewInput | None:
    identity = rwkv_review_identity(reviewer, card)
    if identity is None:
        return None

    review_input = rwkv_review_input(
        reviewer=reviewer,
        card=card,
        identity=identity,
        ease=ease,
        _use_pending_answer=False,
    )
    day_offset, elapsed_days, elapsed_seconds = _rwkv_answer_elapsed(
        reviewer=reviewer,
        card=card,
        deck_config=deck_config,
        review_state=review_state,
        answered_at_millis=answered_at_millis,
    )
    state_kind, normal_state_kind = _rwkv_review_state_kinds(review_state)
    return replace(
        review_input,
        duration_millis=_rwkv_answer_duration_millis(answer, card, ease),
        card_type=review_state,
        card_queue=_rwkv_review_queue_for_state(
            review_state,
            _int_attr(card, "queue"),
        ),
        day_offset=day_offset,
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=elapsed_seconds,
    )


def _rwkv_answer_elapsed(
    *,
    reviewer: object,
    card: object,
    deck_config: dict[str, object],
    review_state: int,
    answered_at_millis: int,
) -> tuple[int | None, int | None, int | None]:
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    have_scheduler_days = isinstance(days_elapsed, int) and isinstance(next_day_at, int)
    day_offset = (
        _historical_review_day_offset(
            answered_at_millis,
            days_elapsed=days_elapsed,
            next_day_at=next_day_at,
        )
        if have_scheduler_days
        else _day_offset(reviewer)
    )

    card_id = _card_id(card)
    previous = (
        _latest_eligible_review_for_card(reviewer, card_id)
        if card_id is not None and review_state != int(RwkvReviewState.LEARN_START)
        else None
    )
    if previous is not None:
        previous_review_id = previous[0]
        elapsed_seconds = max(
            0,
            (answered_at_millis - previous_review_id) // 1000,
        )
        elapsed_days = (
            max(
                0,
                day_offset
                - _historical_review_day_offset(
                    previous_review_id,
                    days_elapsed=days_elapsed,
                    next_day_at=next_day_at,
                ),
            )
            if have_scheduler_days and day_offset is not None
            else None
        )
        return day_offset, elapsed_days, elapsed_seconds

    if card_id is not None and _rwkv_review_first_review_elapsed_from_card_creation(
        deck_config
    ):
        elapsed_seconds = max(0, (answered_at_millis - card_id) // 1000)
        return day_offset, elapsed_seconds // 86_400, elapsed_seconds

    return day_offset, -1, -1


def _rwkv_answer_duration_millis(
    answer: object,
    card: object,
    ease: int,
) -> int | None:
    duration_millis = _int_value(getattr(answer, "milliseconds_taken", None))
    if duration_millis is None:
        return _duration_millis(card, ease)

    duration_millis = max(0, duration_millis)
    time_limit = getattr(card, "time_limit", None)
    if not callable(time_limit):
        return duration_millis

    try:
        limit_millis = _int_value(time_limit())
    except Exception:
        logger.debug("failed to read answer time limit for RWKV review input")
        return duration_millis

    return (
        min(duration_millis, max(0, limit_millis))
        if limit_millis is not None
        else duration_millis
    )


def set_answer_rwkv_metadata(
    answer: object,
    reviewer: object,
    card: object,
    ease: int,
) -> None:
    pending = getattr(reviewer, _REVIEWER_PENDING_ANSWER_STATE_ATTR, None)
    if isinstance(pending, _RwkvPendingAnswerState):
        delattr(reviewer, _REVIEWER_PENDING_ANSWER_STATE_ATTR)

    deck_config = _rwkv_review_active_deck_config(reviewer, card)
    if deck_config is not None:
        current_state = _current_scheduling_state(reviewer)
        state_kind, normal_state_kind = _scheduling_state_kinds(current_state)
        base_review_state = _rwkv_review_state_for_scheduling_state(
            state_kind=state_kind,
            normal_state_kind=normal_state_kind,
            card_type=_int_attr(card, "type"),
        )
        answered_at_millis = _int_attr(answer, "answered_at_millis")
        if answered_at_millis is None or answered_at_millis <= 0:
            timing = _timing_today(reviewer)
            now = getattr(timing, "now", None)
            answered_at_millis = (
                now * 1000 if isinstance(now, int) else int(time.time() * 1000)
            )
        review_state = _rwkv_review_state_for_live_context(
            reviewer,
            card,
            base_review_state=base_review_state,
            answered_at_millis=answered_at_millis,
        )
        review_kind = _rwkv_raw_review_kind(review_state)
        card_id = _card_id(card)
        if (
            review_kind is not None
            and card_id is not None
            and isinstance(review_state, int)
        ):
            review_input = _rwkv_pending_answer_review_input(
                answer=answer,
                reviewer=reviewer,
                card=card,
                ease=ease,
                deck_config=deck_config,
                review_state=review_state,
                answered_at_millis=answered_at_millis,
            )
            setattr(answer, "rwkv_review_kind", review_kind)
            if review_input is not None:
                setattr(
                    reviewer,
                    _REVIEWER_PENDING_ANSWER_STATE_ATTR,
                    _RwkvPendingAnswerState(
                        card_id,
                        ease,
                        review_state,
                        base_review_state,
                        answered_at_millis,
                        review_input,
                    ),
                )

    prediction = _current_reviewer_prediction(reviewer, card)
    if prediction is None or not prediction.review_enabled:
        return

    if _valid_probability(prediction.retrievability):
        setattr(answer, "rwkv_retrievability", float(prediction.retrievability))

    if not prediction.interval_override_used:
        return

    s90 = _s90_for_ease(prediction.s90_overrides, ease)
    if s90 is None:
        return
    if not math.isfinite(s90) or s90 <= 0:
        logger.debug("invalid RWKV S90 ignored for answer: %s", s90)
        return

    setattr(answer, "rwkv_s90", float(s90))


def set_answer_rwkv_s90(
    answer: object,
    reviewer: object,
    card: object,
    ease: int,
) -> None:
    set_answer_rwkv_metadata(answer, reviewer, card, ease)


def _s90_for_ease(overrides: RwkvIntervalOverride, ease: int) -> int | None:
    if 1 <= ease <= len(_RWKV_RATING_FIELDS):
        return cast(int | None, getattr(overrides, _RWKV_RATING_FIELDS[ease - 1]))
    return None


def _queried_card_info_diagnostics(
    reviewer: object,
    card: object,
    *,
    fallback_source: str,
    _candidate: RwkvReviewCandidate | None = None,
) -> RwkvReviewerDiagnostics | None:
    backend = _reviewer_backend
    if backend is None:
        return None

    card_id = _card_id(card)
    if card_id is None:
        return None

    try:
        review_enabled = rwkv_review_active(reviewer, card)
        if review_enabled and not _prepare_reviewer_backend_for_card_info(reviewer):
            logger.debug("RWKV card info prediction skipped: warm-up pending")
            return None
        state_token = _capture_reviewer_backend_prediction_state_token(
            reviewer,
            expected_backend=backend,
        )
        if state_token is None:
            return None
        with _try_reviewer_backend_prediction_access(
            expected_state_token=state_token,
        ) as current_backend:
            if current_backend is None:
                return None
            candidate = _candidate or _card_info_review_candidate(reviewer, card)
            review_enabled = rwkv_review_active(candidate.reviewer, candidate.card)
            predictions = _predict_review_batch_with_backend(
                [candidate],
                current_backend,
            )
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
                s90_overrides=prediction.s90_overrides,
                button_probabilities=prediction.button_probabilities,
            )
            diagnostics = RwkvReviewerDiagnostics(
                retrievability=reviewer_prediction.retrievability,
                retrievability_source=_retrievability_source(
                    reviewer_prediction,
                    fallback_source,
                ),
                button_probabilities=reviewer_prediction.button_probabilities,
                s90_overrides=reviewer_prediction.s90_overrides,
            )
            with _reviewer_backend_state_lock:
                if not _reviewer_backend_prediction_access_is_current(
                    current_backend,
                    expected_state_token=state_token,
                ):
                    return None
                _set_rwkv_card_info_score(
                    reviewer,
                    card_id,
                    diagnostics.retrievability,
                    prediction.curve_retrievability,
                    collection_backend=state_token.collection_backend,
                )
            return diagnostics
    except Exception:
        logger.exception("RWKV card info prediction failed")
        return None


def _card_info_review_candidate(reviewer: object, card: object) -> RwkvReviewCandidate:
    states = _scheduling_states_for_card(reviewer, card)
    if states is not None:
        context = SimpleNamespace(
            mw=getattr(reviewer, "mw", None),
            _v3=SimpleNamespace(states=states),
        )
        return RwkvReviewCandidate(reviewer=context, card=card)

    candidate = _shared_card_info_review_candidate(reviewer, card)
    if candidate is not None:
        return candidate

    return RwkvReviewCandidate(reviewer=reviewer, card=card)


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
    if prediction.review_enabled and _valid_probability(prediction.retrievability):
        return "RWKV"
    if prediction.review_enabled:
        return f"{fallback_source} (RWKV unavailable)"
    return f"{fallback_source} (RWKV disabled)"


def _unavailable_retrievability_source(fallback_source: str) -> str:
    if _reviewer_backend is None:
        return f"{fallback_source} (RWKV backend unavailable)"
    return f"{fallback_source} (RWKV unavailable)"


def _format_retrievability(retrievability: float | None) -> str:
    if retrievability is None:
        return "Unavailable"

    return f"{retrievability * 100:.0f}%"


def _format_button_probabilities(probabilities: RwkvButtonProbabilities) -> str:
    return " ".join(
        f"{label}:{_format_retrievability(probability)}"
        for label, probability in zip(
            ("Again", "Hard", "Good", "Easy"),
            probabilities,
            strict=True,
        )
    )


def _rwkv_button_probabilities_with_retrievability(
    probabilities: RwkvButtonProbabilities | None,
    retrievability: float,
) -> RwkvButtonProbabilities | None:
    if probabilities is None or not _valid_probability(retrievability):
        return probabilities

    successful = probabilities[1] + probabilities[2] + probabilities[3]
    if successful <= 0:
        return probabilities

    scale = retrievability / successful
    return (
        1.0 - retrievability,
        probabilities[1] * scale,
        probabilities[2] * scale,
        probabilities[3] * scale,
    )


def _valid_button_probabilities(value: object) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return False
    if len(value) != 4:
        return False

    total = 0.0
    for probability in value:
        if not _valid_probability(probability):
            return False
        total += float(probability)

    return math.isclose(total, 1.0, abs_tol=0.01)


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
    if key is None:
        return True
    with _reviewer_backend_state_lock:
        return (
            key in _reviewer_backend_warmup_states
            and key not in _reviewer_backend_warmup_pending_generations
        )


def _resident_state_identity(
    history: RwkvHistoricalReviewInputs,
) -> RwkvResidentStateIdentity:
    if not _rwkv_history_hash_is_valid(history.history_hash):
        raise ValueError("missing resident RWKV history identity")
    if not history.replay_key:
        raise ValueError("missing resident RWKV replay identity")
    return RwkvResidentStateIdentity(
        last_review_id=history.last_review_id,
        review_count=history.review_count,
        history_hash=history.history_hash,
        replay_key=history.replay_key,
    )


def _invalidate_reviewer_backend_state(
    reviewer: object,
    *,
    reason: str,
) -> None:
    key = _reviewer_backend_warmup_key(reviewer)
    invalidated = False
    was_warm = False
    generation = 0
    with _reviewer_backend_state_lock:
        _clear_rwkv_review_queue_score_cache()
        if (
            key is not None
            and _reviewer_backend is not None
            and id(_reviewer_backend) == key[0]
        ):
            invalidated = True
            was_warm = key in _reviewer_backend_warmup_states
            _reviewer_backend_warmup_states.pop(key, None)
            _rwkv_memorised_history_identity_cache.pop(key, None)
            generation = _reviewer_backend_warmup_generations.get(key, 0) + 1
            _reviewer_backend_warmup_generations[key] = generation
    try:
        _clear_rwkv_review_queue_scores(reviewer)
    except Exception:
        logger.exception(
            "failed to clear RWKV queue scores after resident state invalidation"
        )
    if not invalidated:
        return
    logger.debug(
        "RWKV resident state invalidated: reason=%s was_warm=%s generation=%s",
        reason,
        was_warm,
        generation,
    )


def _publish_reviewer_backend_state(
    key: tuple[int, int],
    identity: RwkvResidentStateIdentity,
    *,
    expected_generation: int,
) -> bool:
    with _reviewer_backend_state_lock:
        current_generation = _reviewer_backend_warmup_generations.get(key, 0)
        current_backend_id = (
            id(_reviewer_backend) if _reviewer_backend is not None else None
        )
        backend_changed = current_backend_id != key[0]
        if current_generation == expected_generation and not backend_changed:
            _reviewer_backend_warmup_states[key] = identity
            _rwkv_memorised_history_identity_cache[key] = (
                current_generation,
                identity,
            )
            return True
    logger.debug(
        "discarding invalidated RWKV warm-up result: "
        "expected_generation=%s current_generation=%s backend_changed=%s",
        expected_generation,
        current_generation,
        backend_changed,
    )
    return False


def _mark_reviewer_backend_identity_unknown(
    reviewer: object,
    *,
    reason: str,
) -> None:
    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        return
    with _reviewer_backend_state_lock:
        if _reviewer_backend is None or id(_reviewer_backend) != key[0]:
            return
        if key in _reviewer_backend_warmup_states:
            _reviewer_backend_warmup_states[key] = None
        _rwkv_memorised_history_identity_cache.pop(key, None)
        generation = _reviewer_backend_warmup_generations.get(key, 0) + 1
        _reviewer_backend_warmup_generations[key] = generation
        _rwkv_review_queue_score_generations.clear()
    logger.debug(
        "RWKV resident identity cleared: reason=%s generation=%s",
        reason,
        generation,
    )


def _invalidate_reviewer_backend_states(
    backend: object,
    *,
    reason: str,
) -> None:
    backend_id = id(backend)
    with _reviewer_backend_state_lock:
        matching_keys = [
            key
            for key in (
                _reviewer_backend_warmup_states.keys()
                | _reviewer_backend_warmup_generations.keys()
                | _reviewer_backend_warmup_pending_generations.keys()
                | _rwkv_memorised_history_identity_cache.keys()
            )
            if key[0] == backend_id
        ]
        for key in matching_keys:
            _reviewer_backend_warmup_states.pop(key, None)
            _rwkv_memorised_history_identity_cache.pop(key, None)
            _reviewer_backend_warmup_generations[key] = (
                _reviewer_backend_warmup_generations.get(key, 0) + 1
            )
        if matching_keys:
            _clear_rwkv_review_queue_score_cache()
    if matching_keys:
        logger.debug(
            "RWKV resident states invalidated: reason=%s collections=%s",
            reason,
            len(matching_keys),
        )


def _mark_reviewer_backend_identities_unknown(
    backend: object,
    *,
    reason: str,
) -> None:
    backend_id = id(backend)
    with _reviewer_backend_state_lock:
        matching_keys = [
            key
            for key in (
                _reviewer_backend_warmup_states.keys()
                | _reviewer_backend_warmup_generations.keys()
                | _reviewer_backend_warmup_pending_generations.keys()
                | _rwkv_memorised_history_identity_cache.keys()
            )
            if key[0] == backend_id
        ]
        for key in matching_keys:
            if key in _reviewer_backend_warmup_states:
                _reviewer_backend_warmup_states[key] = None
            _rwkv_memorised_history_identity_cache.pop(key, None)
            _reviewer_backend_warmup_generations[key] = (
                _reviewer_backend_warmup_generations.get(key, 0) + 1
            )
        if matching_keys:
            _rwkv_review_queue_score_generations.clear()
    if matching_keys:
        logger.debug(
            "RWKV resident identities cleared: reason=%s collections=%s",
            reason,
            len(matching_keys),
        )


def _begin_forced_reviewer_backend_warmup(
    backend: RwkvReviewerBackend,
    key: tuple[int, int],
) -> _ReviewerBackendWarmupStart:
    with _reviewer_backend_state_lock:
        if _reviewer_backend is not backend:
            return _ReviewerBackendWarmupStart(None, False)
        _reviewer_backend_warmup_states.pop(key, None)
        _rwkv_memorised_history_identity_cache.pop(key, None)
        generation = _reviewer_backend_warmup_generations.get(key, 0) + 1
        _reviewer_backend_warmup_generations[key] = generation
        if key in _reviewer_backend_warmup_pending_generations:
            return _ReviewerBackendWarmupStart(None, False)
        _reviewer_backend_warmup_pending_generations[key] = generation
        return _ReviewerBackendWarmupStart(generation, False)


def _begin_reviewer_backend_warmup(
    reviewer: object,
    backend: RwkvReviewerBackend,
    key: tuple[int, int],
    *,
    force_rebuild: bool,
    require_retrievability_cache: bool,
) -> _ReviewerBackendWarmupStart:
    if force_rebuild:
        return _begin_forced_reviewer_backend_warmup(backend, key)

    while True:
        with _reviewer_backend_state_lock:
            if _reviewer_backend is not backend:
                return _ReviewerBackendWarmupStart(None, False)
            if key in _reviewer_backend_warmup_pending_generations:
                return _ReviewerBackendWarmupStart(None, False)
            if key not in _reviewer_backend_warmup_states:
                generation = _reviewer_backend_warmup_generations.get(key, 0)
                _reviewer_backend_warmup_pending_generations[key] = generation
                return _ReviewerBackendWarmupStart(generation, False)
            if not require_retrievability_cache:
                return _ReviewerBackendWarmupStart(None, True)
            observed_generation = _reviewer_backend_warmup_generations.get(key, 0)

        retrievability_cache_complete = (
            _existing_rwkv_review_retrievability_cache_complete(reviewer)
        )
        with _reviewer_backend_state_lock:
            if _reviewer_backend is not backend:
                return _ReviewerBackendWarmupStart(None, False)
            if (
                _reviewer_backend_warmup_generations.get(key, 0) != observed_generation
                or key not in _reviewer_backend_warmup_states
            ):
                continue
            if retrievability_cache_complete:
                return _ReviewerBackendWarmupStart(None, True)
            _reviewer_backend_warmup_states.pop(key, None)
            if key in _reviewer_backend_warmup_pending_generations:
                return _ReviewerBackendWarmupStart(None, False)
            _reviewer_backend_warmup_pending_generations[key] = observed_generation
            return _ReviewerBackendWarmupStart(observed_generation, False)


def _warm_up_reviewer_backend(
    reviewer: object,
    *,
    force_rebuild: bool = False,
    require_retrievability_cache: bool = False,
    record_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
    additional_ignored_review_ids: Sequence[int] = (),
) -> bool:
    context = _reviewer_backend_warmup_context(reviewer)
    if context is None:
        return True
    backend, key = context

    warm_up = getattr(backend, "warm_up", None)
    if not callable(warm_up):
        return True

    warmup_start = _begin_reviewer_backend_warmup(
        reviewer,
        backend,
        key,
        force_rebuild=force_rebuild,
        require_retrievability_cache=require_retrievability_cache,
    )
    if warmup_start.generation is None:
        return warmup_start.ready
    warmup_generation = warmup_start.generation
    _clear_rwkv_review_queue_score_cache()
    try:
        _clear_rwkv_review_queue_scores(reviewer)
    except Exception:
        logger.exception("failed to clear RWKV queue scores before warm-up")
    if not _acquire_reviewer_backend_execution(
        reviewer,
        backend,
        key,
        warmup_generation,
    ):
        _finish_reviewer_backend_warmup(key, warmup_generation)
        return False

    def is_current() -> bool:
        return _reviewer_backend_warmup_is_current(
            reviewer,
            backend,
            key,
            warmup_generation,
        )

    start = time.monotonic()
    try:
        logger.debug("RWKV historical warm-up started")
        _report_rwkv_state_cache_progress(
            progress,
            "Checking RWKV state cache...",
        )
        restore_start = time.monotonic()
        if force_rebuild:
            _require_reviewer_backend_warmup_current(is_current)
            reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
            if callable(reset_cache_snapshot):
                reset_cache_snapshot()
            _report_rwkv_state_cache_progress(
                progress,
                "Forcing RWKV state cache rebuild...",
            )
            restore_elapsed_ms = 0.0
        else:
            restored_identity = _restore_reviewer_backend_cache(
                reviewer,
                backend=backend,
                is_current=is_current,
                require_retrievability_cache=require_retrievability_cache,
                record_retrievability_cache=record_retrievability_cache,
                progress=progress,
                additional_ignored_review_ids=additional_ignored_review_ids,
            )
            restore_elapsed_ms = (time.monotonic() - restore_start) * 1000
            _require_reviewer_backend_warmup_current(is_current)
            if restored_identity is not None:
                if _publish_reviewer_backend_state(
                    key,
                    restored_identity,
                    expected_generation=warmup_generation,
                ):
                    logger.debug(
                        "restored RWKV reviewer state cache: elapsed_ms=%.1f",
                        restore_elapsed_ms,
                    )
                    return True
                return False
            reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
            if callable(reset_cache_snapshot):
                reset_cache_snapshot()

        _report_rwkv_state_cache_progress(
            progress,
            "Loading RWKV review history...",
        )
        state_cache_available = _rwkv_state_cache_dir(reviewer) is not None
        history_start = time.monotonic()
        history = _historical_rwkv_review_inputs(
            reviewer,
            progress=progress,
            prepare_recovery_checkpoint=state_cache_available,
        )
        history_elapsed_ms = (time.monotonic() - history_start) * 1000
        _require_reviewer_backend_warmup_current(is_current)
        logger.debug(
            "RWKV historical warm-up inputs prepared: reviews=%s review_count=%s "
            "last_review_id=%s elapsed_ms=%.1f",
            len(history.reviews),
            history.review_count,
            history.last_review_id,
            history_elapsed_ms,
        )
        checkpoint_review_counts = (
            _rwkv_recovery_checkpoint_review_counts(history.review_ids)
            if state_cache_available
            else []
        )
        supports_delta_state_store_fn = getattr(
            backend,
            "supports_delta_state_store",
            None,
        )
        supports_delta_state_store = (
            callable(supports_delta_state_store_fn) and supports_delta_state_store_fn()
        )
        snapshot_review_counts = list(checkpoint_review_counts)
        if supports_delta_state_store and history.reviews:
            snapshot_review_counts.append(len(history.reviews))
        checkpoint_writer = _RwkvStateCacheCheckpointWriter(
            reviewer,
            history,
            snapshot_review_counts,
            full_review_counts=checkpoint_review_counts,
        )
        warm_up_start = time.monotonic()
        _require_reviewer_backend_warmup_current(is_current)
        _warm_up_rwkv_reviews(
            reviewer,
            backend,
            warm_up,
            history.reviews,
            review_ids=history.review_ids,
            progress=progress,
            label="Building RWKV state cache",
            record_retrievability_cache=record_retrievability_cache,
            snapshot_after_reviews=snapshot_review_counts,
            snapshot_recorder=checkpoint_writer,
            is_current=is_current,
        )
        warm_up_elapsed_ms = (time.monotonic() - warm_up_start) * 1000
        _require_reviewer_backend_warmup_current(is_current)
        _report_rwkv_state_cache_progress(
            progress,
            "Saving RWKV state cache...",
        )
        save_start = time.monotonic()
        _require_reviewer_backend_warmup_current(is_current)
        _save_reviewer_backend_cache(
            reviewer,
            history,
            backend=backend,
            checkpoint_entries=checkpoint_writer.entries,
            write_context=checkpoint_writer.context,
        )
        save_elapsed_ms = (time.monotonic() - save_start) * 1000
        _require_reviewer_backend_warmup_current(is_current)
        if not _publish_reviewer_backend_state(
            key,
            _resident_state_identity(history),
            expected_generation=warmup_generation,
        ):
            return False
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
    except _ReviewerBackendWarmupInvalidated:
        return False
    except Exception:
        logger.exception("RWKV historical warm-up failed")
        return False
    finally:
        try:
            _finish_rwkv_state_cache_checkpoint_writes_safely(backend)
        finally:
            try:
                _reviewer_backend_execution_lock.release()
            finally:
                _finish_reviewer_backend_warmup(key, warmup_generation)


def _reviewer_backend_warmup_context(
    reviewer: object,
) -> tuple[RwkvReviewerBackend, tuple[int, int]] | None:
    col = _collection(reviewer)
    if col is None or getattr(col, "db", None) is None:
        return None
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
        if backend is None:
            return None
        return backend, (id(backend), id(col))


def _reviewer_backend_warmup_key(reviewer: object) -> tuple[int, int] | None:
    context = _reviewer_backend_warmup_context(reviewer)
    return context[1] if context is not None else None


def _rwkv_recovery_checkpoint_review_counts(
    review_ids: Sequence[int],
) -> list[int]:
    if len(review_ids) < 2:
        return []

    first_review_id = review_ids[0]
    last_review_id = review_ids[-1]
    cutoff = last_review_id - _RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS
    if cutoff < first_review_id:
        return []
    review_count = bisect.bisect_right(review_ids, cutoff)
    return [review_count] if 0 < review_count < len(review_ids) else []


def _rwkv_history_prefix(
    history: RwkvHistoricalReviewInputs,
    review_count: int,
) -> RwkvHistoricalReviewInputs:
    return _rwkv_history_prefixes(history, [review_count])[review_count]


def _rwkv_history_prefixes(
    history: RwkvHistoricalReviewInputs,
    review_counts: Sequence[int],
) -> dict[int, RwkvHistoricalReviewInputs]:
    requested_counts = sorted(set(review_counts))
    if any(
        not 0 < review_count <= len(history.reviews)
        for review_count in requested_counts
    ):
        raise ValueError("invalid RWKV history checkpoint review count")
    if not requested_counts:
        return {}

    results: dict[int, RwkvHistoricalReviewInputs] = {}
    previous_ids: dict[int, int] = {}
    previous_intervals: dict[int, int] = {}
    counts_by_card: dict[int, int] = {}
    history_hash = _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    next_requested_index = 0
    for review_count, (review_id, review) in enumerate(
        zip(history.review_ids, history.reviews, strict=True),
        start=1,
    ):
        card_id = review.identity.card_id
        previous_ids[card_id] = review_id
        if review.interval_days is not None:
            previous_intervals[card_id] = review.interval_days
        counts_by_card[card_id] = counts_by_card.get(card_id, 0) + 1
        history_hash = _rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )

        if review_count != requested_counts[next_requested_index]:
            continue
        results[review_count] = RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            previous_review_id_by_card=dict(previous_ids),
            previous_interval_days_by_card=dict(previous_intervals),
            review_count_by_card=dict(counts_by_card),
            last_review_id=review_id,
            review_count=review_count,
            history_hash=history_hash,
            replay_key=history.replay_key,
            ignored_review_ids=history.ignored_review_ids,
        )
        next_requested_index += 1
        if next_requested_index == len(requested_counts):
            break

    return results


def _rwkv_history_with_suffix_prefix(
    base: RwkvHistoricalReviewInputs,
    suffix: RwkvHistoricalReviewInputs,
    suffix_review_count: int,
) -> RwkvHistoricalReviewInputs:
    return _rwkv_history_with_suffix_prefixes(
        base,
        suffix,
        [suffix_review_count],
    )[suffix_review_count]


def _rwkv_history_with_suffix_prefixes(
    base: RwkvHistoricalReviewInputs,
    suffix: RwkvHistoricalReviewInputs,
    suffix_review_counts: Sequence[int],
) -> dict[int, RwkvHistoricalReviewInputs]:
    requested_counts = sorted(set(suffix_review_counts))
    if any(
        not 0 < review_count <= len(suffix.reviews) for review_count in requested_counts
    ):
        raise ValueError("invalid RWKV checkpoint suffix review count")
    if not requested_counts:
        return {}
    previous_ids = dict(base.previous_review_id_by_card)
    previous_intervals = dict(base.previous_interval_days_by_card)
    review_counts = dict(base.review_count_by_card)
    if base.replay_key != suffix.replay_key:
        raise ValueError("RWKV checkpoint histories use different replay semantics")
    if base.ignored_review_ids != suffix.ignored_review_ids:
        raise ValueError("RWKV checkpoint histories ignore different reviews")

    results: dict[int, RwkvHistoricalReviewInputs] = {}
    history_hash = base.history_hash
    next_requested_index = 0
    for suffix_review_count, (review_id, review) in enumerate(
        zip(suffix.review_ids, suffix.reviews, strict=True),
        start=1,
    ):
        card_id = review.identity.card_id
        previous_ids[card_id] = review_id
        if review.interval_days is not None:
            previous_intervals[card_id] = review.interval_days
        review_counts[card_id] = review_counts.get(card_id, 0) + 1
        history_hash = _rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )

        if suffix_review_count != requested_counts[next_requested_index]:
            continue
        results[suffix_review_count] = RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            previous_review_id_by_card=dict(previous_ids),
            previous_interval_days_by_card=dict(previous_intervals),
            review_count_by_card=dict(review_counts),
            last_review_id=review_id,
            review_count=base.review_count + suffix_review_count,
            history_hash=history_hash,
            replay_key=base.replay_key,
            ignored_review_ids=base.ignored_review_ids,
        )
        next_requested_index += 1
        if next_requested_index == len(requested_counts):
            break

    return results


def _rwkv_validated_history_suffix(
    current_history: RwkvHistoricalReviewInputs,
    base_history: RwkvHistoricalReviewInputs,
) -> RwkvHistoricalReviewInputs:
    suffix_start = bisect.bisect_right(
        current_history.review_ids,
        base_history.last_review_id,
    )
    if base_history.last_review_id and (
        suffix_start == 0
        or current_history.review_ids[suffix_start - 1] != base_history.last_review_id
    ):
        raise ValueError("RWKV cache base is not a current history prefix")

    return RwkvHistoricalReviewInputs(
        reviews=current_history.reviews[suffix_start:],
        review_ids=current_history.review_ids[suffix_start:],
        previous_review_id_by_card=dict(current_history.previous_review_id_by_card),
        previous_interval_days_by_card=dict(
            current_history.previous_interval_days_by_card
        ),
        review_count_by_card=dict(current_history.review_count_by_card),
        last_review_id=current_history.last_review_id,
        review_count=current_history.review_count,
        deck_id=current_history.deck_id,
        history_hash=current_history.history_hash,
        replay_key=current_history.replay_key,
        ignored_review_ids=current_history.ignored_review_ids,
    )


def _warm_up_rwkv_reviews(
    reviewer: object,
    backend: object,
    warm_up: object,
    reviews: Sequence[RwkvReviewInput],
    *,
    review_ids: Sequence[int] | None = None,
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
    record_retrievability_cache: bool = True,
    snapshot_after_reviews: Sequence[int] = (),
    snapshot_recorder: RwkvStateCacheSnapshotCallback | None = None,
    is_current: Callable[[], bool] | None = None,
) -> None:
    started_at = time.monotonic()

    def progress_reporter(replay_progress: RwkvWarmUpProgress) -> None:
        if is_current is not None:
            _require_reviewer_backend_warmup_current(is_current)
        _report_rwkv_review_replay_progress(
            progress,
            label=label,
            replay_progress=replay_progress,
            elapsed_seconds=time.monotonic() - started_at,
        )

    if isinstance(backend, RwkvStatefulReviewerBackend):
        if not record_retrievability_cache:
            backend.warm_up(
                reviews,
                review_ids=review_ids,
                progress=progress_reporter,
                snapshot_after_reviews=snapshot_after_reviews,
                snapshot_recorder=snapshot_recorder,
            )
        else:
            writer = _RwkvReviewRetrievabilityCacheWriter(reviewer)
            try:
                backend.warm_up(
                    reviews,
                    review_ids=review_ids,
                    prediction_recorder=writer,
                    progress=progress_reporter,
                    snapshot_after_reviews=snapshot_after_reviews,
                    snapshot_recorder=snapshot_recorder,
                )
            finally:
                writer.flush()
        return

    if callable(warm_up):
        warm_up_callable = cast(Callable[..., Any], warm_up)
        warm_up_parameters = _callable_parameters(warm_up_callable)
        kwargs: dict[str, object] = {}
        if _callable_accepts_keyword(warm_up_parameters, "review_ids"):
            kwargs["review_ids"] = review_ids
        if _callable_accepts_keyword(warm_up_parameters, "progress"):
            kwargs["progress"] = progress_reporter
        if record_retrievability_cache and _supports_rwkv_warm_up_prediction_recorder(
            warm_up_parameters
        ):
            writer = _RwkvReviewRetrievabilityCacheWriter(reviewer)
            kwargs["prediction_recorder"] = writer
            try:
                warm_up_callable(reviews, **kwargs)
            finally:
                writer.flush()
            return

        warm_up_callable(reviews, **kwargs)


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
    record_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Warm and persist RWKV state for the current desktop collection."""

    configure_reviewer_backend_from_environment()
    if _reviewer_backend is None:
        return False

    record_retrievability_cache = (
        record_retrievability_cache or require_retrievability_cache
    )
    return _warm_up_reviewer_backend(
        SimpleNamespace(mw=mw),
        force_rebuild=force_rebuild,
        require_retrievability_cache=require_retrievability_cache,
        record_retrievability_cache=record_retrievability_cache,
        progress=progress,
    )


def recompute_rwkv_calibration_data(
    mw: object,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Rewrite historical RWKV calibration rows without replacing active state."""

    configure_reviewer_backend_from_environment()
    backend = _reviewer_backend
    if backend is None:
        return False

    cache_snapshot = getattr(backend, "cache_snapshot", None)
    restore_cache_snapshot = getattr(backend, "restore_cache_snapshot", None)
    reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
    warm_up = getattr(backend, "warm_up", None)
    if not (
        callable(cache_snapshot)
        and callable(restore_cache_snapshot)
        and callable(reset_cache_snapshot)
        and callable(warm_up)
    ):
        logger.debug(
            "RWKV calibration data recompute skipped: backend does not support snapshots"
        )
        return False

    reviewer = SimpleNamespace(mw=mw)
    start = time.monotonic()
    try:
        with _temporary_reviewer_backend_operation(
            reviewer,
            backend,
            cache_snapshot=cache_snapshot,
            restore_cache_snapshot=restore_cache_snapshot,
        ) as temporary:
            if temporary is None:
                logger.debug(
                    "RWKV calibration data recompute skipped: backend state busy"
                )
                return False
            operation, _original_snapshot = temporary

            operation.require_current()

            def current_progress(
                label: str,
                value: int | None = None,
                maximum: int | None = None,
            ) -> None:
                operation.require_current()
                _report_rwkv_state_cache_progress(
                    progress,
                    label,
                    value,
                    maximum,
                )

            current_progress(
                "Loading RWKV review history...",
            )
            history = _historical_rwkv_review_inputs(
                reviewer,
                progress=current_progress,
            )
            operation.require_current()
            logger.debug(
                "RWKV calibration recompute inputs prepared: reviews=%s",
                len(history.reviews),
            )
            reset_cache_snapshot()
            sample_role_by_review_id, fold_index_by_review_id = (
                _rwkv_calibration_fold_role_maps(history)
            )
            writer = _RwkvReviewRetrievabilityCacheWriter(
                reviewer,
                source="rwkv_calibration_recompute",
                sample_role_by_review_id=sample_role_by_review_id,
                fold_index_by_review_id=fold_index_by_review_id,
            )
            started_at = time.monotonic()

            def replay_progress(replay_progress: RwkvWarmUpProgress) -> None:
                operation.require_current()
                _report_rwkv_review_replay_progress(
                    progress,
                    label="Recomputing RWKV calibration data",
                    replay_progress=replay_progress,
                    elapsed_seconds=time.monotonic() - started_at,
                )

            try:
                cast(Callable[..., object], warm_up)(
                    history.reviews,
                    review_ids=history.review_ids,
                    prediction_recorder=writer.record,
                    progress=replay_progress,
                )
                operation.require_current()
            finally:
                writer.flush()
            logger.debug(
                "RWKV calibration data recomputed: reviews=%s elapsed_ms=%.1f",
                len(history.reviews),
                (time.monotonic() - start) * 1000,
            )
            return True
    except _ReviewerBackendWarmupInvalidated:
        logger.debug("RWKV calibration data recompute invalidated")
        return False
    except Exception:
        logger.exception("RWKV calibration data recompute failed")
        return False


def rwkv_calibration_data_available(mw: object) -> bool:
    """Return whether current, role-aware historical RWKV predictions exist."""

    reviewer = SimpleNamespace(mw=mw)
    metadata = _read_rwkv_state_cache_metadata(reviewer)
    if metadata is None or not _rwkv_state_cache_metadata_usable(reviewer, metadata):
        return False

    last_review_id = _int_value(metadata.get("lastReviewId"))
    review_count = _int_value(metadata.get("reviewCount"))
    if last_review_id is None or review_count is None:
        return False
    if not _rwkv_review_retrievability_cache_complete(
        reviewer,
        last_review_id=last_review_id,
        review_count=review_count,
    ):
        return False
    if review_count < 2:
        return True

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    if not callable(scalar):
        return False
    try:
        available = scalar(
            f"""
select 1
from {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
where revlog_id <= ?
  and prediction between 0 and 1
  and sample_role = ?
limit 1
""",
            last_review_id,
            _RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD,
        )
    except Exception:
        logger.debug("failed to check RWKV calibration-data availability")
        return False
    return available == 1


def ensure_rwkv_calibration_data(
    mw: object,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> bool:
    """Generate role-aware historical RWKV predictions when they are missing.

    This synchronous API is intended for add-ons running in a background task.
    Existing complete data is a fast no-op. The active reviewer state is
    snapshotted and restored by `recompute_rwkv_calibration_data()`.
    """

    if rwkv_calibration_data_available(mw):
        return True
    return recompute_rwkv_calibration_data(mw, progress=progress)


def recompute_rwkv_calibration_data_with_progress(mw: object) -> None:
    """Recompute RWKV calibration rows with a modal progress dialog."""

    from aqt.utils import tooltip

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        recompute_rwkv_calibration_data(mw)
        return

    def start_recompute() -> None:
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

        def recompute() -> bool:
            return recompute_rwkv_calibration_data(mw, progress=progress)

        def done(future: Future[bool]) -> None:
            try:
                recomputed = future.result()
            except Exception:
                logger.exception("RWKV calibration data recompute failed")
                tooltip("RWKV calibration data recompute failed.", parent=parent)
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            if recomputed:
                tooltip("RWKV calibration data recomputed.", parent=parent)
                logger.debug(
                    "RWKV calibration data recompute finished: elapsed_ms=%.1f",
                    elapsed_ms,
                )
            else:
                tooltip(
                    "RWKV calibration data could not be recomputed.",
                    parent=parent,
                )

        with_progress(
            recompute,
            done,
            parent=parent,
            label="Recomputing RWKV calibration data...",
            immediate=True,
            uses_collection=True,
            title="RWKV Calibration Data",
        )

    _run_on_main(mw, start_recompute)


def compare_rwkv_first_review_elapsed_metrics(
    mw: object,
    *,
    deck_id: int | None = None,
    progress: RwkvStateCacheProgressCallback | None = None,
) -> dict[str, object]:
    """Compare RWKV logloss with first-review elapsed time missing vs card creation."""

    configure_reviewer_backend_from_environment()
    backend = _reviewer_backend
    if backend is None:
        return _rwkv_unavailable_metric_comparison("RWKV backend is not available.")

    cache_snapshot = getattr(backend, "cache_snapshot", None)
    restore_cache_snapshot = getattr(backend, "restore_cache_snapshot", None)
    reset_cache_snapshot = getattr(backend, "reset_cache_snapshot", None)
    warm_up = getattr(backend, "warm_up", None)
    if not (
        callable(cache_snapshot)
        and callable(restore_cache_snapshot)
        and callable(reset_cache_snapshot)
        and callable(warm_up)
    ):
        logger.debug(
            "RWKV first-review elapsed comparison skipped: backend does not support snapshots"
        )
        return _rwkv_unavailable_metric_comparison(
            "RWKV backend does not support comparison replay."
        )

    reviewer = SimpleNamespace(mw=mw)
    start = time.monotonic()
    try:
        with _temporary_reviewer_backend_operation(
            reviewer,
            backend,
            cache_snapshot=cache_snapshot,
            restore_cache_snapshot=restore_cache_snapshot,
        ) as temporary:
            if temporary is None:
                return _rwkv_unavailable_metric_comparison(
                    "RWKV backend state is busy."
                )
            operation, _original_snapshot = temporary

            def current_progress(
                label: str,
                value: int | None = None,
                maximum: int | None = None,
            ) -> None:
                operation.require_current()
                _report_rwkv_state_cache_progress(
                    progress,
                    label,
                    value,
                    maximum,
                )

            current_progress(
                "Loading RWKV review history with missing first-review elapsed time...",
            )
            missing_history = _historical_rwkv_review_inputs(
                reviewer,
                deck_id=deck_id,
                first_review_elapsed_source=RwkvFirstReviewElapsedSource.MISSING,
                progress=current_progress,
            )
            operation.require_current()
            missing_predictions = _rwkv_calibration_predictions_for_history(
                backend,
                warm_up,
                reset_cache_snapshot,
                missing_history,
                progress=progress,
                label="Replaying RWKV history with missing first-review elapsed time",
                is_current=operation.is_current,
            )
            current_progress(
                "Loading RWKV review history with creation-based first-review elapsed time...",
            )
            card_creation_history = _historical_rwkv_review_inputs(
                reviewer,
                deck_id=deck_id,
                first_review_elapsed_source=RwkvFirstReviewElapsedSource.CARD_CREATION,
                progress=current_progress,
            )
            operation.require_current()
            card_creation_predictions = _rwkv_calibration_predictions_for_history(
                backend,
                warm_up,
                reset_cache_snapshot,
                card_creation_history,
                progress=progress,
                label=(
                    "Replaying RWKV history with creation-based first-review elapsed time"
                ),
                is_current=operation.is_current,
            )
            current_progress(
                "Computing RWKV first-review elapsed comparison metrics...",
            )
            comparison = {
                "available": True,
                "deckId": deck_id or 0,
                "missing": _rwkv_calibration_metrics_for_history(
                    missing_history,
                    missing_predictions,
                ),
                "cardCreation": _rwkv_calibration_metrics_for_history(
                    card_creation_history,
                    card_creation_predictions,
                ),
            }
            operation.require_current()
            logger.debug(
                "RWKV first-review elapsed comparison finished: deck_id=%s "
                "missing_reviews=%s card_creation_reviews=%s missing_predictions=%s "
                "card_creation_predictions=%s elapsed_ms=%.1f",
                deck_id,
                len(missing_history.reviews),
                len(card_creation_history.reviews),
                len(missing_predictions),
                len(card_creation_predictions),
                (time.monotonic() - start) * 1000,
            )
            return comparison
    except _ReviewerBackendWarmupInvalidated:
        logger.debug("RWKV first-review elapsed comparison invalidated")
        return _rwkv_unavailable_metric_comparison(
            "RWKV backend state changed during comparison."
        )
    except Exception:
        logger.exception("RWKV first-review elapsed comparison failed")
        return _rwkv_unavailable_metric_comparison(
            "RWKV first-review elapsed comparison failed."
        )


def _rwkv_unavailable_metric_comparison(reason: str) -> dict[str, object]:
    return {
        "available": False,
        "reason": reason,
        "baseline": _rwkv_empty_calibration_metrics(),
        "features": {},
        "split": {},
    }


def _rwkv_calibration_fold_role_maps(
    history: RwkvHistoricalReviewInputs,
) -> tuple[dict[int, str], dict[int, int]]:
    review_ids = [
        review_id
        for review_id, review_input in zip(
            history.review_ids,
            history.reviews,
            strict=True,
        )
        if review_input.ease is not None
    ]
    if len(review_ids) < 2:
        return {}, {}

    train_end = max(1, int(len(review_ids) * _RWKV_CALIBRATION_TRAIN_FRACTION))
    train_end = min(train_end, len(review_ids) - 1)
    sample_role_by_review_id = {
        review_id: _RWKV_RETRIEVABILITY_SAMPLE_ROLE_FINAL_FIT
        for review_id in review_ids[:train_end]
    }
    fold_index_by_review_id = {review_id: -1 for review_id in review_ids[:train_end]}
    sample_role_by_review_id.update(
        {
            review_id: _RWKV_RETRIEVABILITY_SAMPLE_ROLE_TEST_FOLD
            for review_id in review_ids[train_end:]
        }
    )
    fold_index_by_review_id.update(
        {review_id: 0 for review_id in review_ids[train_end:]}
    )
    return sample_role_by_review_id, fold_index_by_review_id


def _rwkv_calibration_predictions_for_history(
    backend: object,
    warm_up: object,
    reset_cache_snapshot: Callable[[], object],
    history: RwkvHistoricalReviewInputs,
    *,
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
    is_current: Callable[[], bool] | None = None,
) -> dict[int, float]:
    predictions: dict[int, float] = {}

    def require_current() -> None:
        if is_current is not None:
            _require_reviewer_backend_warmup_current(is_current)

    def record_prediction(review_id: int, retrievability: float) -> None:
        if review_id > 0 and _valid_probability(retrievability):
            predictions[review_id] = float(retrievability)

    require_current()
    reset_cache_snapshot()
    started_at = time.monotonic()

    def progress_callback(replay_progress: RwkvWarmUpProgress) -> None:
        require_current()
        _report_rwkv_review_replay_progress(
            progress,
            label=label,
            replay_progress=replay_progress,
            elapsed_seconds=time.monotonic() - started_at,
        )

    if isinstance(backend, RwkvStatefulReviewerBackend):
        backend.warm_up(
            history.reviews,
            review_ids=history.review_ids,
            prediction_recorder=record_prediction,
            progress=progress_callback,
        )
        require_current()
        return predictions

    if not callable(warm_up):
        return predictions

    warm_up_callable = cast(Callable[..., Any], warm_up)
    warm_up_parameters = _callable_parameters(warm_up_callable)
    if not _supports_rwkv_warm_up_prediction_recorder(warm_up_parameters):
        return predictions

    kwargs: dict[str, object] = {
        "review_ids": history.review_ids,
        "prediction_recorder": record_prediction,
    }
    if _callable_accepts_keyword(warm_up_parameters, "progress"):
        kwargs["progress"] = progress_callback
    warm_up_callable(history.reviews, **kwargs)
    require_current()
    return predictions


def _rwkv_calibration_metrics_for_history(
    history: RwkvHistoricalReviewInputs,
    predictions_by_review_id: Mapping[int, float],
) -> dict[str, float | int]:
    prior_long_term_reviews_by_card: dict[int, int] = {}
    prior_lapses_by_card: dict[int, int] = {}
    pairs: list[RwkvCalibrationMetricPair] = []

    for review_id, review_input in zip(
        history.review_ids,
        history.reviews,
        strict=True,
    ):
        prediction = predictions_by_review_id.get(review_id)
        if prediction is None or review_input.ease is None:
            continue

        card_id = review_input.identity.card_id
        elapsed_days = review_input.current_elapsed_days
        is_long_term_review = (
            isinstance(elapsed_days, int)
            and not isinstance(elapsed_days, bool)
            and elapsed_days >= 1
        )
        prior_long_term_reviews = prior_long_term_reviews_by_card.get(card_id, 0)
        prior_lapses = prior_lapses_by_card.get(card_id, 0)
        long_term_reviews = prior_long_term_reviews + int(is_long_term_review)

        pairs.append(
            (
                prediction,
                0 if review_input.ease == 1 else 1,
                (
                    _rwkv_calibration_metric_delta_t_bin(
                        elapsed_days if isinstance(elapsed_days, int) else -1
                    ),
                    _rwkv_calibration_metric_count_bin(
                        long_term_reviews + 1.0,
                        1.99,
                        1.89,
                    ),
                    (
                        0
                        if prior_lapses == 0
                        else _rwkv_calibration_metric_count_bin(
                            prior_lapses,
                            1.65,
                            1.73,
                        )
                    ),
                ),
            )
        )

        prior_long_term_reviews_by_card[card_id] = long_term_reviews
        if review_input.ease == 1:
            prior_lapses_by_card[card_id] = prior_lapses + 1

    return _rwkv_calibration_metrics(pairs)


def _rwkv_empty_calibration_metrics() -> dict[str, float | int]:
    return {
        "count": 0,
        "positives": 0,
        "recallRate": 0.0,
        "logLoss": 0.0,
        "brier": 0.0,
        "rmse": 0.0,
        "bins": 0,
        "rmseBins": 0.0,
    }


def _rwkv_calibration_metrics(
    pairs: Sequence[RwkvCalibrationMetricPair],
) -> dict[str, float | int]:
    count = 0
    positives = 0
    log_loss = 0.0
    brier = 0.0
    bin_totals: dict[RwkvCalibrationMetricBin, list[float]] = {}
    for prediction, outcome, recall_bin in pairs:
        prediction = _rwkv_calibration_metric_probability(prediction)
        count += 1
        positives += outcome
        log_loss -= outcome * math.log(prediction) + (1 - outcome) * math.log(
            1 - prediction
        )
        brier += (prediction - outcome) ** 2
        value = bin_totals.setdefault(recall_bin, [0.0, 0.0, 0.0])
        value[0] += prediction
        value[1] += outcome
        value[2] += 1.0

    if count == 0:
        return _rwkv_empty_calibration_metrics()

    return {
        "count": count,
        "positives": positives,
        "recallRate": positives / count,
        "logLoss": log_loss / count,
        "brier": brier / count,
        "rmse": math.sqrt(brier / count),
        "bins": len(bin_totals),
        "rmseBins": _rwkv_calibration_metric_rmse_bins(bin_totals),
    }


def _rwkv_calibration_metric_probability(value: float) -> float:
    return min(
        max(float(value), _RWKV_CALIBRATION_METRIC_EPSILON),
        1.0 - _RWKV_CALIBRATION_METRIC_EPSILON,
    )


def _rwkv_calibration_metric_rmse_bins(
    bin_totals: Mapping[RwkvCalibrationMetricBin, Sequence[float]],
) -> float:
    weight_sum = sum(value[2] for value in bin_totals.values())
    if weight_sum == 0:
        return 0.0

    squared_error_sum = 0.0
    for predicted_sum, actual_sum, count in bin_totals.values():
        predicted = predicted_sum / count
        actual = actual_sum / count
        squared_error_sum += (predicted - actual) ** 2 * count
    return math.sqrt(squared_error_sum / weight_sum)


def _rwkv_calibration_metric_delta_t_bin(delta_t: int) -> int:
    if delta_t <= 0:
        return 0
    return _rwkv_calibration_metric_count_bin(delta_t, 248.0, 3.62)


def _rwkv_calibration_metric_count_bin(
    value: float,
    multiplier: float,
    base: float,
) -> int:
    if value <= 0:
        return 0
    binned = multiplier * base ** math.floor(math.log(value, base))
    return round(binned) if math.isfinite(binned) and binned >= 0 else 0


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


def rwkv_state_cache_usable(
    mw: object,
    *,
    dynamic_preset_replay_enabled: bool | None = None,
) -> bool:
    """Return true when the current collection has a usable local RWKV cache."""

    context = SimpleNamespace(mw=mw)
    metadata = _read_rwkv_state_cache_metadata(context)
    if metadata is None:
        return False

    if metadata.get("version") == _RWKV_STATE_CACHE_VERSION:
        with _reviewer_backend_state_lock:
            backend = _reviewer_backend
        return (
            _read_rwkv_state_cache_binary(
                context,
                backend=backend,
                dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
            )
            is not None
        )
    return _rwkv_state_cache_metadata_usable(
        context,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
    )


def prepare_rwkv_state_cache_on_startup(mw: object) -> None:
    """Restore or prompt for RWKV state cache preparation after profile open."""

    begin_rwkv_state_cache_startup(mw)
    finish_rwkv_state_cache_startup(mw)


def begin_rwkv_state_cache_startup(mw: object) -> None:
    """Invalidate resident state before startup sync can change the collection."""

    _invalidate_reviewer_backend_runtime_state_for_profile_open()
    _set_rwkv_state_cache_loading(mw, True)


def finish_rwkv_state_cache_startup(mw: object) -> None:
    """Restore or prompt after any automatic startup sync has completed."""

    config_state = _rwkv_collection_config_state(SimpleNamespace(mw=mw))
    if not config_state.review_enabled:
        _set_rwkv_state_cache_loading(mw, False)
        return

    load_rwkv_state_cache_with_progress(
        mw,
        prompt_if_unavailable=True,
    )


def maybe_prompt_for_rwkv_state_cache(mw: object) -> None:
    """Prompt once per session to build the local RWKV state cache if needed."""

    if _rwkv_startup_prompt_shown:
        return
    config_state = _rwkv_collection_config_state(SimpleNamespace(mw=mw))
    if not config_state.review_enabled:
        return
    if rwkv_state_cache_usable(
        mw,
        dynamic_preset_replay_enabled=config_state.dynamic_preset_replay_enabled,
    ):
        return

    _show_rwkv_state_cache_prompt(mw)


def _rwkv_resident_state_ready(mw: object) -> bool:
    reviewer = SimpleNamespace(mw=mw)
    return _reviewer_backend_warmup_key(
        reviewer
    ) is not None and _reviewer_backend_warmed_up(reviewer)


def _show_rwkv_state_cache_prompt(mw: object) -> None:
    global _rwkv_startup_prompt_shown

    if _rwkv_startup_prompt_shown:
        return
    if not configure_reviewer_backend_from_environment():
        return
    if _rwkv_resident_state_ready(mw):
        return

    _rwkv_startup_prompt_shown = True
    parent = cast(QWidget | None, mw)

    def prompt() -> None:
        if _rwkv_resident_state_ready(mw):
            return

        from aqt.utils import ask_user_dialog

        def on_choice(choice: int) -> None:
            if choice not in (0, 1) or _rwkv_resident_state_ready(mw):
                return
            if choice == 0:
                build_rwkv_state_cache_with_progress(
                    mw,
                    record_retrievability_cache=False,
                )
            elif choice == 1:
                build_rwkv_state_cache_with_progress(
                    mw,
                    record_retrievability_cache=True,
                )

        ask_user_dialog(
            "RWKV review is enabled, but the local RWKV state cache is not ready.\n\n"
            "Build the state cache only to start reviewing sooner. Build with "
            "calibration data if you also want historical RWKV predictions prepared "
            "for calibration/stat features.",
            callback=on_choice,
            buttons=[
                "Build State Only",
                "Build State + Calibration Data",
                QMessageBox.StandardButton.Cancel,
            ],
            default_button=1,
            parent=parent,
            title="RWKV State Cache",
        )

    taskman = getattr(mw, "taskman", None)
    run_on_main = getattr(taskman, "run_on_main", None)
    if callable(run_on_main):
        run_on_main(prompt)
    else:
        prompt()


def rwkv_state_cache_loading(mw: object) -> bool:
    """Return whether count preparation must wait for RWKV state restoration."""

    if getattr(mw, "_rwkv_state_cache_loading", False):
        return True
    return _reviewer_backend_warmup_pending(SimpleNamespace(mw=mw))


def _set_rwkv_state_cache_loading(mw: object, loading: bool) -> None:
    setattr(mw, "_rwkv_state_cache_loading", loading)


def _refresh_active_rwkv_count_view(mw: object) -> bool:
    if getattr(mw, "state", None) not in ("deckBrowser", "overview"):
        return False
    refresh = getattr(mw, "onRefreshTimer", None)
    if not callable(refresh):
        return False
    logger.debug("refreshing active count view after RWKV state cache operation")
    refresh()
    return True


def _finish_rwkv_state_cache_operation(
    mw: object,
    *,
    ready: bool,
    prewarm_reason: str,
) -> None:
    _set_rwkv_state_cache_loading(mw, False)
    if _refresh_active_rwkv_count_view(mw) or not ready:
        return
    prewarm_reviewer_queue_score_cache(
        SimpleNamespace(mw=mw),
        reason=prewarm_reason,
        include_parent_scope=False,
    )


def load_rwkv_state_cache_with_progress(
    mw: object,
    *,
    prompt_if_unavailable: bool = False,
) -> None:
    """Restore the local RWKV state cache with a lightweight progress dialog."""

    def finish(loaded: bool) -> None:
        if prompt_if_unavailable and not loaded:
            _set_rwkv_state_cache_loading(mw, False)
            _show_rwkv_state_cache_prompt(mw)
            return
        _finish_rwkv_state_cache_operation(
            mw,
            ready=loaded,
            prewarm_reason="startup cache load",
        )

    _set_rwkv_state_cache_loading(mw, True)
    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        try:
            loaded = load_rwkv_state_cache(mw)
        except Exception:
            finish(False)
            if prompt_if_unavailable:
                logger.exception("RWKV state cache startup load failed")
                return
            raise
        finish(loaded)
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
                finish(False)
                logger.exception("RWKV state cache startup load failed")
                return

            elapsed_ms = (time.monotonic() - start) * 1000
            if loaded:
                logger.debug(
                    "RWKV state cache startup load finished: elapsed_ms=%.1f",
                    elapsed_ms,
                )
            else:
                logger.debug(
                    "RWKV state cache startup load skipped: elapsed_ms=%.1f",
                    elapsed_ms,
                )
            finish(loaded)

        try:
            with_progress(
                load,
                done,
                parent=parent,
                label="Loading RWKV state cache...",
                immediate=True,
                uses_collection=True,
                title="RWKV State Cache",
            )
        except Exception:
            finish(False)
            if prompt_if_unavailable:
                logger.exception("failed to start RWKV state cache load")
                return
            raise

    _run_on_main(mw, start_load)


def refresh_rwkv_state_after_sync(
    mw: object,
    on_done: Callable[[], None],
    *,
    remote_review_ids: Sequence[int] = (),
) -> None:
    """Reconcile resident RWKV state with the merged review history."""

    reviewer = SimpleNamespace(mw=mw)
    ignored_review_count_before = len(
        _rwkv_state_cache_ignored_review_ids(_read_rwkv_state_cache_metadata(reviewer))
    )
    if not _rwkv_collection_config_state(reviewer).review_enabled:
        on_done()
        return
    if not configure_reviewer_backend_from_environment():
        on_done()
        return

    key = _reviewer_backend_warmup_key(reviewer)
    if key is None:
        on_done()
        return

    _invalidate_reviewer_backend_state(
        reviewer,
        reason="post-sync refresh",
    )
    _set_rwkv_state_cache_loading(mw, True)

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

    def refresh() -> bool:
        return _warm_up_reviewer_backend(
            reviewer,
            progress=progress,
            additional_ignored_review_ids=remote_review_ids,
        )

    completion_lock = threading.Lock()
    completed = False

    def done(future: Future[bool]) -> None:
        nonlocal completed
        with completion_lock:
            if completed:
                return
            completed = True
        try:
            ready = future.result()
        except Exception:
            logger.exception("RWKV post-sync state refresh failed")
            ready = False

        _set_rwkv_state_cache_loading(mw, False)

        logger.info(
            "RWKV post-sync state refresh finished: ready=%s elapsed_ms=%.1f",
            ready,
            (time.monotonic() - start) * 1000,
        )
        ignored_review_count = len(
            _rwkv_state_cache_ignored_review_ids(
                _read_rwkv_state_cache_metadata(reviewer)
            )
        )
        if (
            ready
            and remote_review_ids
            and ignored_review_count > ignored_review_count_before
        ):
            from aqt.utils import show_warning

            logger.warning(
                "RWKV state ignored synchronized historical reviews: new=%s total=%s",
                ignored_review_count - ignored_review_count_before,
                ignored_review_count,
            )
            show_warning(
                f"RWKV kept its previous state and did not incorporate "
                f"{ignored_review_count} synchronized review"
                f"{'' if ignored_review_count == 1 else 's'} older than 8 days.\n\n"
                "Rebuild the RWKV state from deck options if you want "
                "those reviews included.",
                parent=cast(QWidget | None, mw),
            )
        on_done()

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if callable(with_progress):
        try:
            with_progress(
                refresh,
                done,
                parent=cast(QWidget | None, mw),
                label="Updating RWKV state after sync...",
                immediate=True,
                uses_collection=True,
                title="RWKV State Cache",
            )
        except Exception as exc:
            launch_future: Future[bool] = Future()
            launch_future.set_exception(exc)
            done(launch_future)
        return

    direct_future: Future[bool] = Future()
    try:
        direct_future.set_result(refresh())
    except Exception as exc:
        direct_future.set_exception(exc)
    done(direct_future)


def build_rwkv_state_cache_with_progress(
    mw: object,
    *,
    force_rebuild: bool = False,
    record_retrievability_cache: bool = False,
) -> None:
    """Build the local RWKV state cache with a modal progress dialog."""

    from aqt.utils import tooltip

    _set_rwkv_state_cache_loading(mw, True)
    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        try:
            built = warm_up_rwkv_state(
                mw,
                force_rebuild=force_rebuild,
                require_retrievability_cache=record_retrievability_cache,
                record_retrievability_cache=record_retrievability_cache,
            )
        except Exception:
            _finish_rwkv_state_cache_operation(
                mw,
                ready=False,
                prewarm_reason="state cache build",
            )
            raise
        _finish_rwkv_state_cache_operation(
            mw,
            ready=built,
            prewarm_reason="state cache build",
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
                require_retrievability_cache=record_retrievability_cache,
                record_retrievability_cache=record_retrievability_cache,
                progress=progress,
            )

        def done(future: Future[bool]) -> None:
            try:
                built = future.result()
            except Exception:
                _finish_rwkv_state_cache_operation(
                    mw,
                    ready=False,
                    prewarm_reason="state cache build",
                )
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
            else:
                tooltip("RWKV state cache could not be built.", parent=parent)
            _finish_rwkv_state_cache_operation(
                mw,
                ready=built,
                prewarm_reason="state cache build",
            )

        try:
            with_progress(
                build,
                done,
                parent=parent,
                label="Building RWKV state cache...",
                immediate=True,
                uses_collection=True,
                title="RWKV State Cache",
            )
        except Exception:
            _finish_rwkv_state_cache_operation(
                mw,
                ready=False,
                prewarm_reason="state cache build",
            )
            raise

    _run_on_main(mw, start_build)


def simulate_rwkv_workload_bytes(
    data: bytes,
    *,
    cancel_event: threading.Event | None = None,
) -> bytes:
    request = scheduler_pb2.SimulateFsrsReviewRequest()
    request.ParseFromString(data)
    response = simulate_rwkv_workload(request, cancel_event=cancel_event)
    return response.SerializeToString()


def rwkv_memorised_history_identity(mw: object) -> str:
    """Return the stable producer identity used by the local daily-R cache."""

    reviewer = SimpleNamespace(mw=mw)
    ready_identity = _rwkv_ready_state_cache_history_identity(
        reviewer,
    )
    warmup_key: tuple[int, int] | None = None
    observed_generation: int | None = None
    if ready_identity is None:
        warmup_key = _reviewer_backend_warmup_key(reviewer)
        if warmup_key is not None:
            with _reviewer_backend_state_lock:
                if _reviewer_backend_warmup_key(reviewer) == warmup_key:
                    observed_generation = (
                        _reviewer_backend_warmup_generations.setdefault(
                            warmup_key,
                            0,
                        )
                    )
                    cached = _rwkv_memorised_history_identity_cache.get(warmup_key)
                    if cached is not None:
                        cached_generation, cached_identity = cached
                        if cached_generation == observed_generation:
                            ready_identity = cached_identity
                        else:
                            _rwkv_memorised_history_identity_cache.pop(
                                warmup_key,
                                None,
                            )
    if ready_identity is None:
        history = _historical_rwkv_review_inputs(reviewer)
        ready_identity = _resident_state_identity(history)
        if warmup_key is not None and observed_generation is not None:
            with _reviewer_backend_state_lock:
                if (
                    _reviewer_backend_warmup_key(reviewer) == warmup_key
                    and _reviewer_backend_warmup_generations.get(warmup_key, 0)
                    == observed_generation
                ):
                    _rwkv_memorised_history_identity_cache[warmup_key] = (
                        observed_generation,
                        ready_identity,
                    )
    return _rwkv_memorised_history_identity(
        reviewer,
        last_review_id=ready_identity.last_review_id,
        review_count=ready_identity.review_count,
        history_hash=ready_identity.history_hash,
        replay_key=ready_identity.replay_key,
    )


def _rwkv_ready_state_cache_history_identity(
    reviewer: object,
) -> RwkvResidentStateIdentity | None:
    warmup_key = _reviewer_backend_warmup_key(reviewer)
    if warmup_key is None:
        return None

    with _reviewer_backend_state_lock:
        if warmup_key in _reviewer_backend_warmup_pending_generations:
            return None
        return _reviewer_backend_warmup_states.get(warmup_key)


def _rwkv_memorised_history_identity(
    reviewer: object,
    *,
    last_review_id: int,
    review_count: int,
    history_hash: str,
    replay_key: str,
) -> str:
    if not _rwkv_history_hash_is_valid(history_hash) or not replay_key:
        raise ValueError("invalid RWKV Memorised history identity")
    value = {
        "version": 3,
        "collection": _rwkv_collection_cache_key(reviewer),
        "model": _rwkv_model_cache_key(),
        "dynamicPresetReplay": _rwkv_dynamic_preset_replay_enabled_for_collection(
            reviewer
        ),
        "firstReviewElapsed": _rwkv_first_review_elapsed_config_key(reviewer),
        "lastReviewId": last_review_id,
        "reviewCount": review_count,
        "historyHash": history_hash,
        "replayKey": replay_key,
        "dayOffset": _day_offset(reviewer),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def start_rwkv_memorised_history(
    mw: object,
    display_card_ids: Sequence[int],
    resume: RwkvMemorisedHistoryResult | None = None,
) -> None:
    """Start an isolated, progressively readable daily RWKV history build."""

    global _rwkv_memorised_history_job

    cancel_rwkv_memorised_history()
    selected = frozenset(
        card_id
        for card_id in display_card_ids
        if isinstance(card_id, int) and not isinstance(card_id, bool) and card_id > 0
    )
    job = RwkvMemorisedHistoryJob(
        cancel_event=threading.Event(),
        display_card_ids=selected,
    )
    if resume is not None:
        job.phase = "resuming"
        job.checkpoint = resume
        if not resume.complete:
            (
                job.retrievability_by_day,
                job.note_retrievability_by_day,
                job.card_count_by_day,
            ) = _rwkv_memorised_aggregate_series(resume, selected)
            job.current = sum(len(card.values) // 2 for card in resume.cards)
            job.total = resume.total
            job.first_day = resume.first_day
            job.completed_through_day = resume.completed_through_day
    with _rwkv_memorised_history_job_lock:
        _rwkv_memorised_history_job = job

    threading.Thread(
        target=_run_rwkv_memorised_history_job,
        args=(mw, job),
        name="rwkv-memorised-history",
        daemon=True,
    ).start()


def rwkv_memorised_history_progress() -> dict[str, object]:
    with _rwkv_memorised_history_job_lock:
        job = _rwkv_memorised_history_job
    if job is None:
        return {"phase": "idle", "current": 0, "total": 0, "done": False}

    with job.lock:
        return {
            "phase": job.phase,
            "current": job.current,
            "total": job.total,
            "firstDay": job.first_day,
            "completedThroughDay": job.completed_through_day,
            "retrievabilityByDay": list(job.retrievability_by_day),
            "noteRetrievabilityByDay": list(job.note_retrievability_by_day),
            "cardCountByDay": list(job.card_count_by_day),
            "done": job.done,
            "error": job.error,
        }


def rwkv_memorised_history_result() -> RwkvMemorisedHistoryResult | None:
    with _rwkv_memorised_history_job_lock:
        job = _rwkv_memorised_history_job
    if job is None:
        return None
    with job.lock:
        if not job.done:
            return None
        if job.error is not None:
            raise ValueError(job.error)
        return job.result


def rwkv_memorised_history_checkpoint() -> RwkvMemorisedHistoryResult | None:
    with _rwkv_memorised_history_job_lock:
        job = _rwkv_memorised_history_job
    if job is None:
        return None
    with job.lock:
        return job.checkpoint


def cancel_rwkv_memorised_history() -> None:
    with _rwkv_memorised_history_job_lock:
        job = _rwkv_memorised_history_job
    if job is not None and not job.done:
        job.cancel_event.set()


def _run_rwkv_memorised_history_job(
    mw: object,
    job: RwkvMemorisedHistoryJob,
) -> None:
    try:
        _compute_rwkv_memorised_history(mw, job)
    except InterruptedError:
        with job.lock:
            job.phase = "cancelled"
    except Exception as exc:
        logger.exception("RWKV Memorised history build failed")
        with job.lock:
            job.phase = "failed"
            job.error = str(exc)
    finally:
        with job.lock:
            job.done = True


def _compute_rwkv_memorised_history(
    mw: object,
    job: RwkvMemorisedHistoryJob,
) -> None:
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    reviewer = SimpleNamespace(mw=mw)
    timing = _timing_today(reviewer)
    last_day = getattr(timing, "days_elapsed", None)
    if not isinstance(last_day, int):
        raise ValueError("RWKV scheduler timing is unavailable")

    history = _historical_rwkv_review_inputs(reviewer)
    review_pairs = [
        (review_id, review)
        for review_id, review in zip(history.review_ids, history.reviews, strict=True)
        if isinstance(review.day_offset, int)
    ]
    review_ids = [review_id for review_id, _review in review_pairs]
    reviews = [review for _review_id, review in review_pairs]
    identity = _rwkv_memorised_history_identity(
        reviewer,
        last_review_id=history.last_review_id,
        review_count=history.review_count,
        history_hash=history.history_hash,
        replay_key=history.replay_key,
    )
    if not reviews:
        with job.lock:
            job.phase = "complete"
            job.current = 0
            job.total = 0
            job.completed_through_day = last_day
            job.result = RwkvMemorisedHistoryResult(
                identity=identity,
                first_day=last_day,
                last_day=last_day,
                cards=(),
                completed_through_day=last_day,
            )
        return

    model_path = _current_embedded_rwkv_model_path()
    if model_path is None:
        raise ValueError("RWKV model is unavailable")

    first_day_by_card: dict[int, int] = {}
    note_id_by_card: dict[int, int | None] = {}
    for review in reviews:
        day = review.day_offset
        card_id = review.identity.card_id
        first_day_by_card.setdefault(card_id, day)
        note_id_by_card[card_id] = review.identity.note_id
    first_day = min(first_day_by_card.values())
    total = sum(last_day - day + 1 for day in first_day_by_card.values())

    selected_note_counts: dict[int, int] = {}
    for card_id in job.display_card_ids:
        note_id = note_id_by_card.get(card_id)
        if note_id is not None:
            selected_note_counts[note_id] = selected_note_counts.get(note_id, 0) + 1

    runtime = _RustRwkvRuntime(
        model_path=model_path,
        target_retention=_RWKV_DEFAULT_TARGET_RETENTION,
        max_interval_days=36_500,
    )
    (
        review_index,
        active_inputs,
        reps_by_card,
        lapses_by_card,
        start_day_by_card,
        values_by_card,
        display_retrievability,
        display_note_retrievability,
        display_card_count,
        current,
        loop_first_day,
        resumed,
    ) = _initial_rwkv_memorised_computation_state(
        job,
        identity=identity,
        first_day=first_day,
        last_day=last_day,
        total=total,
        reviews=reviews,
        review_ids=review_ids,
        runtime=runtime,
    )
    last_checkpoint_at = time.monotonic()

    with job.lock:
        job.phase = "computing"
        job.current = current
        job.total = total
        job.first_day = first_day
        job.completed_through_day = loop_first_day - 1 if resumed else None
        job.retrievability_by_day = list(display_retrievability)
        job.note_retrievability_by_day = list(display_note_retrievability)
        job.card_count_by_day = list(display_card_count)

    for day in range(loop_first_day, last_day + 1):
        if job.cancel_event.is_set():
            _finish_cancelled_rwkv_memorised_job(
                job,
                identity=identity,
                first_day=first_day,
                last_day=last_day,
                total=total,
                note_id_by_card=note_id_by_card,
                start_day_by_card=start_day_by_card,
                values_by_card=values_by_card,
            )
            return

        day_start = review_index
        while review_index < len(reviews) and reviews[review_index].day_offset == day:
            review_index += 1
        day_reviews = reviews[day_start:review_index]
        runtime.warm_up_reviews_in_place(day_reviews)

        for review in day_reviews:
            card_id = review.identity.card_id
            active_inputs[card_id] = review
            reps_by_card[card_id] = reps_by_card.get(card_id, 0) + 1
            if review.ease == 1 and review.current_normal_state_kind in (
                "review",
                "relearning",
            ):
                lapses_by_card[card_id] = lapses_by_card.get(card_id, 0) + 1
            start_day_by_card.setdefault(card_id, day)
            values_by_card.setdefault(card_id, array("H"))

        card_ids = sorted(active_inputs)
        query_inputs = []
        for card_id in card_ids:
            previous = active_inputs[card_id]
            elapsed_days = max(0, day - previous.day_offset)
            query_inputs.append(
                replace(
                    previous,
                    is_query=True,
                    ease=None,
                    duration_millis=None,
                    day_offset=day,
                    current_elapsed_days=elapsed_days,
                    current_elapsed_seconds=elapsed_days * 86_400,
                    reps=reps_by_card.get(card_id, 0),
                    lapses=lapses_by_card.get(card_id, 0),
                )
            )

        predictions = runtime.predict_retrievability_many_from_warm_up(query_inputs)
        selected_sum = 0.0
        selected_note_sum = 0.0
        selected_count = 0
        for card_id, query_input, raw_prediction in zip(
            card_ids,
            query_inputs,
            predictions,
            strict=True,
        ):
            prediction = float(raw_prediction)
            prediction = min(max(prediction, 0.0), 1.0)
            values_by_card[card_id].append(round(prediction * 65_535))

            if card_id in job.display_card_ids:
                selected_sum += prediction
                selected_count += 1
                note_id = note_id_by_card.get(card_id)
                note_count = (
                    selected_note_counts.get(note_id, 0) if note_id is not None else 0
                )
                if note_count:
                    selected_note_sum += prediction / note_count

        display_retrievability.append(selected_sum)
        display_note_retrievability.append(selected_note_sum)
        display_card_count.append(selected_count)
        current += len(card_ids)
        with job.lock:
            job.current = current
            job.completed_through_day = day
            job.retrievability_by_day = list(display_retrievability)
            job.note_retrievability_by_day = list(display_note_retrievability)
            job.card_count_by_day = list(display_card_count)

        now = time.monotonic()
        if now - last_checkpoint_at >= _RWKV_MEMORISED_CHECKPOINT_INTERVAL_SECONDS:
            checkpoint = _rwkv_memorised_result_from_values(
                identity=identity,
                first_day=first_day,
                last_day=last_day,
                completed_through_day=day,
                total=total,
                note_id_by_card=note_id_by_card,
                start_day_by_card=start_day_by_card,
                values_by_card=values_by_card,
                complete=False,
            )
            with job.lock:
                job.checkpoint = checkpoint
            last_checkpoint_at = now

    result = _rwkv_memorised_result_from_values(
        identity=identity,
        first_day=first_day,
        last_day=last_day,
        completed_through_day=last_day,
        total=total,
        note_id_by_card=note_id_by_card,
        start_day_by_card=start_day_by_card,
        values_by_card=values_by_card,
        complete=True,
    )
    with job.lock:
        job.phase = "complete"
        job.current = total
        job.completed_through_day = last_day
        job.result = result
        job.checkpoint = None


def _initial_rwkv_memorised_computation_state(
    job: RwkvMemorisedHistoryJob,
    *,
    identity: str,
    first_day: int,
    last_day: int,
    total: int,
    reviews: Sequence[RwkvReviewInput],
    review_ids: Sequence[int],
    runtime: object,
) -> tuple[
    int,
    dict[int, RwkvReviewInput],
    dict[int, int],
    dict[int, int],
    dict[int, int],
    dict[int, array[int]],
    list[float],
    list[float],
    list[int],
    int,
    int,
    bool,
]:
    resume = job.checkpoint
    if resume is not None and resume.complete:
        resume = _rwkv_memorised_completed_prefix_checkpoint(
            resume,
            identity=identity,
            first_day=first_day,
            last_day=last_day,
            total=total,
            reviews=reviews,
            review_ids=review_ids,
        )
    resumed = _restore_rwkv_memorised_checkpoint(
        resume,
        identity=identity,
        first_day=first_day,
        last_day=last_day,
        reviews=reviews,
        runtime=runtime,
    )
    if resumed is None:
        if job.checkpoint is not None:
            logger.warning("ignored stale or invalid RWKV Memorised checkpoint")
            with job.lock:
                job.checkpoint = None
        return 0, {}, {}, {}, {}, {}, [], [], [], 0, first_day, False

    (
        review_index,
        active_inputs,
        reps_by_card,
        lapses_by_card,
        start_day_by_card,
        values_by_card,
    ) = resumed
    assert resume is not None
    completed_day = resume.completed_through_day
    assert completed_day is not None
    retrievability, note_retrievability, card_count = _rwkv_memorised_aggregate_series(
        resume, job.display_card_ids
    )
    current = sum(len(values) for values in values_by_card.values())
    logger.debug(
        "resumed RWKV Memorised history: completed_day=%s current=%s total=%s",
        completed_day,
        current,
        total,
    )
    return (
        review_index,
        active_inputs,
        reps_by_card,
        lapses_by_card,
        start_day_by_card,
        values_by_card,
        retrievability,
        note_retrievability,
        card_count,
        current,
        completed_day + 1,
        True,
    )


def _rwkv_memorised_completed_prefix_checkpoint(
    completed: RwkvMemorisedHistoryResult,
    *,
    identity: str,
    first_day: int,
    last_day: int,
    total: int,
    reviews: Sequence[RwkvReviewInput],
    review_ids: Sequence[int],
) -> RwkvMemorisedHistoryResult | None:
    """Reuse the unaffected day prefix of a completed Memorised cache."""

    if (
        not completed.complete
        or completed.completed_through_day != completed.last_day
        or completed.first_day != first_day
        or completed.last_day > last_day
        or len(reviews) != len(review_ids)
    ):
        return None

    try:
        cached_identity = json.loads(completed.identity)
        current_identity = json.loads(identity)
    except (TypeError, ValueError):
        return None
    if not isinstance(cached_identity, dict) or not isinstance(current_identity, dict):
        return None

    cached_last_review_id = cached_identity.pop("lastReviewId", None)
    cached_review_count = cached_identity.pop("reviewCount", None)
    cached_history_hash = cached_identity.pop("historyHash", None)
    cached_day = cached_identity.pop("dayOffset", None)
    current_last_review_id = current_identity.pop("lastReviewId", None)
    current_review_count = current_identity.pop("reviewCount", None)
    current_history_hash = current_identity.pop("historyHash", None)
    current_day = current_identity.pop("dayOffset", None)
    if (
        cached_identity != current_identity
        or not isinstance(cached_last_review_id, int)
        or not isinstance(cached_review_count, int)
        or not _rwkv_history_hash_is_valid(cached_history_hash)
        or not isinstance(cached_day, int)
        or not isinstance(current_last_review_id, int)
        or not isinstance(current_review_count, int)
        or not _rwkv_history_hash_is_valid(current_history_hash)
        or not isinstance(current_day, int)
        or cached_day != completed.last_day
        or current_day != last_day
        or cached_day > current_day
        or cached_last_review_id > current_last_review_id
        or cached_review_count > current_review_count
    ):
        return None

    unchanged_prefix_count = 0
    prefix_hash = _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    full_hash = _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    for review_id, review in zip(review_ids, reviews, strict=True):
        full_hash = _rwkv_history_hash_after_review(full_hash, review_id, review)
        if review_id <= cached_last_review_id:
            unchanged_prefix_count += 1
            prefix_hash = _rwkv_history_hash_after_review(
                prefix_hash,
                review_id,
                review,
            )
    if (
        unchanged_prefix_count != cached_review_count
        or prefix_hash != cached_history_hash
        or full_hash != current_history_hash
    ):
        return None

    new_review_days = [
        review.day_offset
        for review_id, review in zip(review_ids, reviews, strict=True)
        if review_id > cached_last_review_id and isinstance(review.day_offset, int)
    ]
    if current_last_review_id > cached_last_review_id and not new_review_days:
        return None

    affected_day = min(new_review_days) if new_review_days else completed.last_day + 1
    reusable_through_day = min(completed.last_day, affected_day - 1)
    if reusable_through_day < first_day or reusable_through_day >= last_day:
        return None

    cards: list[RwkvMemorisedCardSeries] = []
    for card in completed.cards:
        if card.start_day > reusable_through_day:
            continue
        value_count = reusable_through_day - card.start_day + 1
        byte_count = value_count * 2
        if len(card.values) < byte_count or len(card.values) % 2:
            return None
        cards.append(replace(card, values=card.values[:byte_count]))

    logger.debug(
        "reusing completed RWKV Memorised cache prefix: cached_day=%s "
        "affected_day=%s reusable_through_day=%s cached_reviews=%s "
        "current_reviews=%s",
        completed.last_day,
        affected_day,
        reusable_through_day,
        cached_review_count,
        current_review_count,
    )
    return RwkvMemorisedHistoryResult(
        identity=identity,
        first_day=first_day,
        last_day=last_day,
        cards=tuple(cards),
        completed_through_day=reusable_through_day,
        total=total,
        complete=False,
    )


def _restore_rwkv_memorised_checkpoint(
    checkpoint: RwkvMemorisedHistoryResult | None,
    *,
    identity: str,
    first_day: int,
    last_day: int,
    reviews: Sequence[RwkvReviewInput],
    runtime: object,
) -> (
    tuple[
        int,
        dict[int, RwkvReviewInput],
        dict[int, int],
        dict[int, int],
        dict[int, int],
        dict[int, array[int]],
    ]
    | None
):
    if (
        checkpoint is None
        or checkpoint.complete
        or checkpoint.identity != identity
        or checkpoint.first_day != first_day
        or checkpoint.last_day != last_day
        or checkpoint.completed_through_day is None
        or not first_day <= checkpoint.completed_through_day < last_day
    ):
        return None

    completed_day = checkpoint.completed_through_day
    review_index = 0
    active_inputs: dict[int, RwkvReviewInput] = {}
    reps_by_card: dict[int, int] = {}
    lapses_by_card: dict[int, int] = {}
    start_day_by_card: dict[int, int] = {}
    while (
        review_index < len(reviews)
        and reviews[review_index].day_offset is not None
        and reviews[review_index].day_offset <= completed_day
    ):
        review = reviews[review_index]
        card_id = review.identity.card_id
        active_inputs[card_id] = review
        reps_by_card[card_id] = reps_by_card.get(card_id, 0) + 1
        if review.ease == 1 and review.current_normal_state_kind in (
            "review",
            "relearning",
        ):
            lapses_by_card[card_id] = lapses_by_card.get(card_id, 0) + 1
        assert review.day_offset is not None
        start_day_by_card.setdefault(card_id, review.day_offset)
        review_index += 1

    checkpoint_cards = {card.card_id: card for card in checkpoint.cards}
    values_by_card = {
        card_id: _u16_array_from_little_endian_bytes(card.values)
        for card_id, card in checkpoint_cards.items()
    }
    if set(values_by_card) != set(active_inputs):
        return None
    for card_id, values in values_by_card.items():
        expected_start = start_day_by_card[card_id]
        card = checkpoint_cards[card_id]
        if (
            card.start_day != expected_start
            or len(values) != completed_day - expected_start + 1
        ):
            return None

    warm_up = getattr(runtime, "warm_up_reviews_in_place", None)
    if not callable(warm_up):
        return None
    warm_up(reviews[:review_index])
    return (
        review_index,
        active_inputs,
        reps_by_card,
        lapses_by_card,
        start_day_by_card,
        values_by_card,
    )


def _finish_cancelled_rwkv_memorised_job(
    job: RwkvMemorisedHistoryJob,
    *,
    identity: str,
    first_day: int,
    last_day: int,
    total: int,
    note_id_by_card: dict[int, int | None],
    start_day_by_card: dict[int, int],
    values_by_card: dict[int, array[int]],
) -> None:
    completed_day = job.completed_through_day
    checkpoint = (
        _rwkv_memorised_result_from_values(
            identity=identity,
            first_day=first_day,
            last_day=last_day,
            completed_through_day=completed_day,
            total=total,
            note_id_by_card=note_id_by_card,
            start_day_by_card=start_day_by_card,
            values_by_card=values_by_card,
            complete=False,
        )
        if completed_day is not None
        else None
    )
    with job.lock:
        job.phase = "cancelled"
        job.checkpoint = checkpoint
        job.result = checkpoint


def _rwkv_memorised_result_from_values(
    *,
    identity: str,
    first_day: int,
    last_day: int,
    completed_through_day: int,
    total: int,
    note_id_by_card: dict[int, int | None],
    start_day_by_card: dict[int, int],
    values_by_card: dict[int, array[int]],
    complete: bool,
) -> RwkvMemorisedHistoryResult:
    return RwkvMemorisedHistoryResult(
        identity=identity,
        first_day=first_day,
        last_day=last_day,
        cards=tuple(
            RwkvMemorisedCardSeries(
                card_id=card_id,
                note_id=note_id_by_card.get(card_id),
                start_day=start_day_by_card[card_id],
                values=_little_endian_u16_bytes(values),
            )
            for card_id, values in sorted(values_by_card.items())
        ),
        completed_through_day=completed_through_day,
        total=total,
        complete=complete,
    )


def _rwkv_memorised_aggregate_series(
    result: RwkvMemorisedHistoryResult,
    display_card_ids: frozenset[int],
) -> tuple[list[float], list[float], list[int]]:
    completed_day = result.completed_through_day
    if completed_day is None:
        return [], [], []
    length = max(0, completed_day - result.first_day + 1)
    retrievability = [0.0] * length
    note_retrievability = [0.0] * length
    card_count = [0] * length
    note_counts: dict[int, int] = {}
    for card in result.cards:
        if card.card_id in display_card_ids and card.note_id is not None:
            note_counts[card.note_id] = note_counts.get(card.note_id, 0) + 1
    for card in result.cards:
        if card.card_id not in display_card_ids:
            continue
        values = _u16_array_from_little_endian_bytes(card.values)
        offset = card.start_day - result.first_day
        note_count = note_counts.get(card.note_id, 0) if card.note_id is not None else 0
        for index, encoded in enumerate(values):
            day_index = offset + index
            if not 0 <= day_index < length:
                continue
            prediction = encoded / 65_535
            retrievability[day_index] += prediction
            card_count[day_index] += 1
            if note_count:
                note_retrievability[day_index] += prediction / note_count
    return retrievability, note_retrievability, card_count


def _u16_array_from_little_endian_bytes(raw: bytes) -> array[int]:
    if len(raw) % 2:
        raise ValueError("invalid RWKV Memorised UInt16 series")
    values = array("H")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _little_endian_u16_bytes(values: array[int]) -> bytes:
    if sys.byteorder == "little":
        return values.tobytes()
    copied = array("H", values)
    copied.byteswap()
    return copied.tobytes()


def start_rwkv_workload_bytes(data: bytes) -> bytes:
    global _rwkv_workload_job

    cancel_rwkv_workload()
    job = RwkvWorkloadJob(cancel_event=threading.Event())
    with _rwkv_workload_job_lock:
        _rwkv_workload_job = job
    _set_rwkv_workload_progress(0, 0)

    def run() -> None:
        try:
            job.result = simulate_rwkv_workload_bytes(
                bytes(data),
                cancel_event=job.cancel_event,
            )
        except Exception as exc:
            job.error = str(exc)
        finally:
            job.done = True

    threading.Thread(
        target=run,
        name="rwkv-workload-simulation",
        daemon=True,
    ).start()
    return b""


def rwkv_workload_result_bytes() -> bytes | None:
    with _rwkv_workload_job_lock:
        job = _rwkv_workload_job
    if job is None:
        raise ValueError("RWKV workload simulation has not been started")
    if not job.done:
        return None
    if job.error is not None:
        raise ValueError(job.error)
    return job.result or b""


def cancel_rwkv_workload() -> None:
    with _rwkv_workload_job_lock:
        job = _rwkv_workload_job
    if job is not None and not job.done:
        job.cancel_event.set()


def rwkv_workload_progress_bytes() -> bytes:
    with _rwkv_workload_progress_lock:
        progress = _rwkv_workload_progress
    return collection_pb2.ComputeRetentionProgress(
        current=max(0, int(progress.current)),
        total=max(0, int(progress.total)),
    ).SerializeToString()


def _set_rwkv_workload_progress(current: int, total: int) -> None:
    global _rwkv_workload_progress
    with _rwkv_workload_progress_lock:
        _rwkv_workload_progress = RwkvWorkloadProgress(
            current=max(0, int(current)),
            total=max(0, int(total)),
        )


def _check_rwkv_workload_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("RWKV workload simulation interrupted")


def _rwkv_workload_progress_total(min_dr: int, max_dr: int) -> int:
    return _rwkv_workload_progress_total_for_step(min_dr, max_dr, 1)


def _rwkv_workload_progress_total_for_step(
    min_dr: int,
    max_dr: int,
    target_dr_step: int,
) -> int:
    target_drs = _rwkv_workload_target_drs(min_dr, max_dr, target_dr_step)
    return len(target_drs) + 1 if target_drs else 0


def _rwkv_workload_target_drs(
    min_dr: int,
    max_dr: int,
    target_dr_step: int,
) -> list[int]:
    if min_dr > max_dr:
        return []
    step = max(1, int(target_dr_step))
    values = list(range(min_dr, max_dr + 1, step))
    if values[-1] != max_dr:
        values.append(max_dr)
    return values


def _rwkv_workload_target_dr_step(
    request: scheduler_pb2.SimulateFsrsReviewRequest,
) -> int:
    return max(1, int(request.rwkv_workload_target_step or 1))


def _rwkv_workload_state_update_interval(
    request: scheduler_pb2.SimulateFsrsReviewRequest,
) -> int:
    return max(1, int(request.rwkv_workload_state_update_interval or 1))


def _rwkv_sampled_review_limit(review_limit: int, input_scale: float) -> int:
    if review_limit <= 0:
        return 0
    if input_scale <= 1.0:
        return review_limit
    return max(1, int(round(review_limit / input_scale)))


def _rwkv_workload_review_count_cap(
    review_limit: int,
    additional_new_limit: int,
    days_to_simulate: int,
) -> int:
    return min(
        2**32 - 1,
        (max(0, int(review_limit)) + max(0, int(additional_new_limit)))
        * max(0, int(days_to_simulate)),
    )


def _sample_rwkv_simulation_inputs(
    inputs: Sequence[tuple[int, RwkvReviewInput, int]],
    sample_limit: int,
) -> list[tuple[int, RwkvReviewInput, int]]:
    if sample_limit <= 0 or len(inputs) <= sample_limit:
        return list(inputs)

    sorted_inputs = sorted(inputs, key=lambda item: item[0])
    total = len(sorted_inputs)
    if sample_limit == 1:
        return [sorted_inputs[total // 2]]
    return [
        sorted_inputs[round(index * (total - 1) / (sample_limit - 1))]
        for index in range(sample_limit)
    ]


def simulate_rwkv_workload(
    request: scheduler_pb2.SimulateFsrsReviewRequest,
    mw: object | None = None,
    cancel_event: threading.Event | None = None,
) -> scheduler_pb2.SimulateFsrsWorkloadResponse:
    """Simulate a fixed-DR workload using the current desktop RWKV state cache."""

    if mw is None:
        import aqt

        mw = aqt.mw

    reviewer = SimpleNamespace(mw=mw)
    if not configure_reviewer_backend_from_environment() or _reviewer_backend is None:
        raise ValueError("RWKV backend is not available")
    if not _reviewer_backend_warmed_up(reviewer) and not load_rwkv_state_cache(mw):
        raise ValueError("RWKV state cache is not ready. Build it first.")
    if not _reviewer_backend_accepts_review_inputs():
        raise ValueError("RWKV backend does not support workload simulation")

    input_build = _rwkv_review_input_batches_for_search(
        reviewer=reviewer,
        search=request.search,
        include_suspended_review=False,
        include_new_cards=request.new_limit > 0,
        batch_size_override=None,
    )
    if input_build is None:
        raise ValueError("Unable to load RWKV review inputs for the requested search")

    all_simulation_inputs = _rwkv_simulation_inputs(input_build)
    sample_limit = max(0, int(request.rwkv_workload_sample_limit or 0))
    simulation_inputs = _sample_rwkv_simulation_inputs(
        all_simulation_inputs,
        sample_limit,
    )
    input_scale = (
        len(all_simulation_inputs) / len(simulation_inputs)
        if simulation_inputs
        else 1.0
    )
    response = scheduler_pb2.SimulateFsrsWorkloadResponse()
    response.reviewless_end_memorized = 0.0
    response.reviewless_end_weighted_memorized = 0.0
    _set_rwkv_workload_progress(0, 0)
    if not simulation_inputs:
        return response

    backend = _reviewer_backend
    cache_snapshot = getattr(backend, "cache_snapshot", None)
    restore_cache_snapshot = getattr(backend, "restore_cache_snapshot", None)
    if not callable(cache_snapshot) or not callable(restore_cache_snapshot):
        raise ValueError("RWKV backend does not support simulator snapshots")

    review_model = _rwkv_simulator_review_model(reviewer)
    _apply_rwkv_review_time_model(response, review_model)
    days_to_simulate = max(0, int(request.days_to_simulate))
    review_limit = max(0, int(request.review_limit))
    new_limit = max(0, int(request.new_limit))
    sampled_review_limit = _rwkv_sampled_review_limit(review_limit, input_scale)
    sampled_new_limit = _rwkv_sampled_review_limit(new_limit, input_scale)
    scheduling = _RwkvWorkloadScheduling(
        review_limit=sampled_review_limit,
        new_limit=sampled_new_limit,
        new_cards_ignore_review_limit=request.new_cards_ignore_review_limit,
        max_interval=max(1, int(request.max_interval)),
        review_order=int(request.review_order),
        suspend_after_lapses=request.suspend_after_lapse_count,
    )
    review_count_cap = _rwkv_workload_review_count_cap(
        review_limit,
        new_limit if request.new_cards_ignore_review_limit else 0,
        days_to_simulate,
    )
    target_dr_step = _rwkv_workload_target_dr_step(request)
    state_update_interval = _rwkv_workload_state_update_interval(request)
    progress_total = _rwkv_workload_progress_total_for_step(
        _RWKV_WORKLOAD_MIN_DR,
        _RWKV_WORKLOAD_MAX_DR,
        target_dr_step,
    )
    _set_rwkv_workload_progress(0, progress_total)
    restore_required = False

    try:
        _check_rwkv_workload_cancel(cancel_event)
        with _temporary_reviewer_backend_operation(
            reviewer,
            backend,
            cache_snapshot=cache_snapshot,
            restore_cache_snapshot=restore_cache_snapshot,
            restore_required=lambda: restore_required,
        ) as temporary:
            if temporary is None:
                raise ValueError("RWKV backend state is busy")
            operation, original_snapshot = temporary

            def report_progress(current: int, total: int) -> None:
                _check_rwkv_workload_cancel(cancel_event)
                operation.require_current()
                _set_rwkv_workload_progress(current, total)

            operation.require_current()
            start = time.monotonic()
            fast_output = _simulate_rwkv_workload_with_embedded_runtime(
                backend=backend,
                simulation_inputs=simulation_inputs,
                snapshot=original_snapshot,
                days_to_simulate=days_to_simulate,
                scheduling=scheduling,
                target_dr_step=target_dr_step,
                state_update_interval=state_update_interval,
                review_model=review_model,
                progress=report_progress,
            )
            operation.require_current()
            if fast_output is not None:
                _check_rwkv_workload_cancel(cancel_event)
                _apply_rwkv_workload_output(response, fast_output)
                _scale_rwkv_workload_response(
                    response,
                    input_scale,
                    review_count_cap=review_count_cap,
                )
                _enforce_monotonic_rwkv_workload_review_counts(response)
                operation.require_current()
                logger.debug(
                    "RWKV workload simulation finished: search=%r inputs=%s days=%s "
                    "sampled_inputs=%s sample_limit=%s input_scale=%.3f dr_step=%s "
                    "review_limit=%s sampled_review_limit=%s "
                    "state_update_interval=%s path=embedded elapsed_ms=%.1f",
                    request.search,
                    len(all_simulation_inputs),
                    days_to_simulate,
                    len(simulation_inputs),
                    sample_limit,
                    input_scale,
                    target_dr_step,
                    review_limit,
                    sampled_review_limit,
                    state_update_interval,
                    (time.monotonic() - start) * 1000,
                )
                return response

            restore_required = True
            restore_cache_snapshot(original_snapshot)
            operation.require_current()
            _check_rwkv_workload_cancel(cancel_event)
            reviewless_memorized, reviewless_weighted = _rwkv_simulation_memorized(
                simulation_inputs,
                days_to_simulate,
            )
            operation.require_current()
            response.reviewless_end_memorized = reviewless_memorized
            response.reviewless_end_weighted_memorized = reviewless_weighted
            report_progress(1, progress_total)

            start = time.monotonic()
            for offset, dr in enumerate(
                _rwkv_workload_target_drs(
                    _RWKV_WORKLOAD_MIN_DR,
                    _RWKV_WORKLOAD_MAX_DR,
                    target_dr_step,
                ),
                start=2,
            ):
                _check_rwkv_workload_cancel(cancel_event)
                operation.require_current()
                target_retention = dr / 100.0
                restore_cache_snapshot(original_snapshot)
                operation.require_current()
                point = _simulate_rwkv_workload_for_target(
                    simulation_inputs,
                    target_retention=target_retention,
                    days_to_simulate=days_to_simulate,
                    scheduling=scheduling,
                    state_update_interval=state_update_interval,
                    review_model=review_model,
                )
                operation.require_current()
                response.memorized[dr] = point.memorized
                response.weighted_memorized[dr] = point.weighted_memorized
                response.cost[dr] = point.cost
                response.review_count[dr] = point.review_count
                report_progress(offset, progress_total)

            _check_rwkv_workload_cancel(cancel_event)
            operation.require_current()
            _scale_rwkv_workload_response(
                response,
                input_scale,
                review_count_cap=review_count_cap,
            )
            _enforce_monotonic_rwkv_workload_review_counts(response)
            logger.debug(
                "RWKV workload simulation finished: search=%r inputs=%s days=%s "
                "sampled_inputs=%s sample_limit=%s input_scale=%.3f dr_step=%s "
                "review_limit=%s sampled_review_limit=%s state_update_interval=%s "
                "path=python elapsed_ms=%.1f",
                request.search,
                len(all_simulation_inputs),
                days_to_simulate,
                len(simulation_inputs),
                sample_limit,
                input_scale,
                target_dr_step,
                review_limit,
                sampled_review_limit,
                state_update_interval,
                (time.monotonic() - start) * 1000,
            )
    except _ReviewerBackendWarmupInvalidated as exc:
        raise InterruptedError(
            "RWKV workload simulation invalidated by state change"
        ) from exc
    finally:
        if cancel_event is None or not cancel_event.is_set():
            _set_rwkv_workload_progress(progress_total, progress_total)

    return response


def _rwkv_simulation_inputs(
    input_build: RwkvReviewInputBatchBuild,
) -> list[tuple[int, RwkvReviewInput, int]]:
    inputs: list[tuple[int, RwkvReviewInput, int]] = []
    for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items():
        for card_id, review_input in inputs_by_card_id:
            if review_input.card_type not in (CARD_TYPE_NEW, CARD_TYPE_REV):
                continue
            if (
                review_input.card_type == CARD_TYPE_REV
                and review_input.current_elapsed_days is None
            ):
                continue
            inputs.append((card_id, review_input, batch_size))
    return inputs


def _simulate_rwkv_workload_with_embedded_runtime(
    *,
    backend: object,
    simulation_inputs: Sequence[tuple[int, RwkvReviewInput, int]],
    snapshot: RwkvBackendCacheSnapshot,
    days_to_simulate: int,
    scheduling: _RwkvWorkloadScheduling,
    target_dr_step: int,
    state_update_interval: int,
    review_model: _RwkvSimulatorReviewModel,
    progress: RwkvWorkloadProgressCallback | None,
) -> object | None:
    simulate_workload = getattr(backend, "simulate_workload", None)
    if not callable(simulate_workload):
        return None
    return simulate_workload(
        inputs=simulation_inputs,
        snapshot=snapshot,
        min_dr=_RWKV_WORKLOAD_MIN_DR,
        max_dr=_RWKV_WORKLOAD_MAX_DR,
        target_dr_step=target_dr_step,
        days_to_simulate=days_to_simulate,
        scheduling=scheduling,
        state_update_interval=state_update_interval,
        review_model=review_model,
        progress=progress,
    )


def _apply_rwkv_workload_output(
    response: scheduler_pb2.SimulateFsrsWorkloadResponse,
    output: object,
) -> None:
    reviewless_memorized, reviewless_weighted, points = cast(
        RwkvWorkloadOutput,
        output,
    )
    response.reviewless_end_memorized = reviewless_memorized
    response.reviewless_end_weighted_memorized = reviewless_weighted
    for dr, memorized, weighted_memorized, cost, review_count in points:
        response.memorized[dr] = memorized
        response.weighted_memorized[dr] = weighted_memorized
        response.cost[dr] = cost
        response.review_count[dr] = review_count


def _apply_rwkv_review_time_model(
    response: scheduler_pb2.SimulateFsrsWorkloadResponse,
    model: _RwkvSimulatorReviewModel,
) -> None:
    response.review_time_r_bucket_count = model.review_time_r_bucket_count
    response.review_time_s_bucket_count = model.review_time_s_bucket_count
    _replace_repeated(
        response.review_time_again_seconds, model.review_time_again_seconds
    )
    _replace_repeated(response.review_time_hard_seconds, model.review_time_hard_seconds)
    _replace_repeated(response.review_time_good_seconds, model.review_time_good_seconds)
    _replace_repeated(response.review_time_easy_seconds, model.review_time_easy_seconds)
    _replace_repeated(
        response.review_time_sample_counts, model.review_time_sample_counts
    )
    _replace_repeated(response.review_time_again_coeffs, model.review_time_again_coeffs)
    _replace_repeated(response.review_time_hard_coeffs, model.review_time_hard_coeffs)
    _replace_repeated(response.review_time_good_coeffs, model.review_time_good_coeffs)
    _replace_repeated(response.review_time_easy_coeffs, model.review_time_easy_coeffs)
    _replace_repeated(
        response.review_time_grade_weights, model.review_time_grade_weights
    )
    _replace_repeated(
        response.review_time_transition_probs, model.review_time_transition_probs
    )
    _replace_repeated(
        response.review_time_transition_counts, model.review_time_transition_counts
    )
    _replace_repeated(
        response.review_time_success_grade_probs,
        model.review_time_success_grade_probs,
    )
    _replace_repeated(
        response.review_time_success_grade_counts,
        model.review_time_success_grade_counts,
    )


def _replace_repeated(field: object, values: Sequence[object]) -> None:
    repeated = cast(Any, field)
    del repeated[:]
    repeated.extend(values)


def _scale_rwkv_workload_response(
    response: scheduler_pb2.SimulateFsrsWorkloadResponse,
    scale: float,
    *,
    review_count_cap: int | None = None,
) -> None:
    if scale != 1.0:
        response.reviewless_end_memorized *= scale
        response.reviewless_end_weighted_memorized *= scale
        for mapping in (
            response.memorized,
            response.weighted_memorized,
            response.cost,
        ):
            for key, value in list(mapping.items()):
                mapping[key] = value * scale
    for key, value in list(response.review_count.items()):
        review_count = max(0, int(round(value * scale)))
        if review_count_cap is not None:
            review_count = min(review_count, review_count_cap)
        response.review_count[key] = min(
            2**32 - 1,
            review_count,
        )


def _enforce_monotonic_rwkv_workload_review_counts(
    response: scheduler_pb2.SimulateFsrsWorkloadResponse,
) -> None:
    _enforce_monotonic_uint_map(response.review_count)
    for preset in response.preset_workload:
        _enforce_monotonic_uint_map(preset.review_count)


def _enforce_monotonic_uint_map(mapping: Any) -> None:
    running = 0
    for key in sorted(mapping):
        running = max(running, int(mapping[key]))
        mapping[key] = running


def _simulate_rwkv_workload_for_target(
    simulation_inputs: Sequence[tuple[int, RwkvReviewInput, int]],
    *,
    target_retention: float,
    days_to_simulate: int,
    scheduling: _RwkvWorkloadScheduling,
    state_update_interval: int,
    review_model: _RwkvSimulatorReviewModel,
) -> _RwkvSimulationPoint:
    predictions = _rwkv_simulation_predictions(
        [
            (
                card_id,
                _rwkv_simulation_query_input(review_input, target_retention, 0),
                batch_size,
            )
            for card_id, review_input, batch_size in simulation_inputs
        ]
    )
    cards = [
        _rwkv_simulation_card(
            review_input,
            prediction,
            target_retention=target_retention,
        )
        for (_, review_input, _), prediction in zip(
            simulation_inputs,
            predictions,
            strict=True,
        )
        if prediction is not None
    ]

    total_cost = 0.0
    review_count = 0
    for day in range(days_to_simulate):
        due_reviews = [
            card
            for card in cards
            if not card.is_new and not card.suspended and card.due_day <= day
        ]
        review_predictions = [
            _rwkv_simulation_prediction_for_card(
                card,
                target_retention=target_retention,
                day=day,
            )
            for card in due_reviews
        ]
        due_reviews = [
            card
            for card, _ in sorted(
                zip(due_reviews, review_predictions, strict=True),
                key=lambda item: _rwkv_simulation_review_sort_key(
                    item[0], item[1], scheduling.review_order, target_retention
                ),
            )
        ]
        due_reviews = (
            due_reviews[: scheduling.review_limit] if scheduling.review_limit else []
        )

        due_new = sorted(
            (
                card
                for card in cards
                if card.is_new and not card.suspended and card.due_day <= day
            ),
            key=lambda card: card.review_input.identity.card_id,
        )[: scheduling.new_limit]
        if not scheduling.new_cards_ignore_review_limit:
            due_new = due_new[: max(0, scheduling.review_limit - len(due_reviews))]
        due_cards = [*due_reviews, *due_new]

        for card in due_cards:
            prediction = _rwkv_simulation_prediction_for_card(
                card,
                target_retention=target_retention,
                day=day,
            )
            if prediction is None:
                card.due_day = day + 1
                continue

            retrievability = _valid_retrievability_or_default(
                prediction.retrievability,
                target_retention,
            )
            ease = _rwkv_simulation_grade(
                review_model.probabilities_for(retrievability),
                card.review_input.identity.card_id,
                day,
                card.reps,
            )
            grade_seconds = review_model.grade_seconds[ease - 1]
            total_cost += grade_seconds
            review_count += 1

            answer_input = _rwkv_simulation_answer_input(
                card,
                target_retention=target_retention,
                day=day,
                ease=ease,
                duration_millis=max(1, round(grade_seconds * 1000)),
            )
            if review_count % state_update_interval == 0:
                _rwkv_simulation_store_answer(answer_input)

            interval = _s90_for_ease(prediction.interval_overrides, ease)
            interval = min(scheduling.max_interval, max(1, interval or 1))
            card.interval_days = interval
            card.last_review_day = day
            card.due_day = day + interval
            card.reps += 1
            if card.is_new:
                card.is_new = False
                card.review_input = replace(card.review_input, card_type=CARD_TYPE_REV)
            if ease == 1:
                card.lapses += 1
                if (
                    scheduling.suspend_after_lapses is not None
                    and card.lapses >= scheduling.suspend_after_lapses
                ):
                    card.suspended = True

    memorized, weighted_memorized = _rwkv_simulation_memorized_from_cards(
        cards,
        target_retention=target_retention,
        day=days_to_simulate,
    )
    return _RwkvSimulationPoint(
        memorized=memorized,
        weighted_memorized=weighted_memorized,
        cost=total_cost,
        review_count=review_count,
    )


def _rwkv_simulation_card(
    review_input: RwkvReviewInput,
    prediction: RwkvReviewPrediction | None,
    *,
    target_retention: float,
) -> _RwkvSimulationCard:
    elapsed_days = _rwkv_input_elapsed_days(review_input)
    interval_days = review_input.interval_days or 1
    due_day = 0
    if prediction is not None and prediction.retrievability is not None:
        if prediction.retrievability > target_retention and prediction.current_interval:
            due_day = max(1, prediction.current_interval - elapsed_days)
    return _RwkvSimulationCard(
        review_input=review_input,
        due_day=due_day,
        last_review_day=-elapsed_days,
        interval_days=interval_days,
        reps=review_input.reps or 0,
        lapses=review_input.lapses or 0,
        is_new=review_input.card_type == CARD_TYPE_NEW,
    )


def _rwkv_simulation_review_sort_key(
    card: _RwkvSimulationCard,
    prediction: RwkvReviewPrediction | None,
    review_order: int,
    target_retention: float,
) -> tuple[float, int]:
    card_id = card.review_input.identity.card_id
    retrievability = _valid_retrievability_or_default(
        prediction.retrievability if prediction is not None else None,
        target_retention,
    )
    priority: float
    if review_order == 3:
        priority = float(card.interval_days)
    elif review_order == 4:
        priority = -card.interval_days
    elif review_order == 5:
        priority = card.review_input.ease_factor or 0
    elif review_order == 6:
        priority = -(card.review_input.ease_factor or 0)
    elif review_order == 7:
        priority = retrievability
    elif review_order == 11:
        priority = -retrievability
    elif review_order == 12:
        priority = _rwkv_relative_overdueness(retrievability, target_retention)
    elif review_order == 8:
        priority = _rwkv_simulation_unit_hash(card_id, 0, 0)
    elif review_order == 9:
        priority = card_id
    elif review_order == 10:
        priority = -card_id
    else:
        priority = card.due_day
    return float(priority), card_id


def _rwkv_relative_overdueness(
    retrievability: float,
    target_retention: float | None,
) -> float:
    effective_target = (
        float(target_retention) if _valid_probability(target_retention) else 1.0
    )
    return retrievability / max(0.0001, effective_target)


def _rwkv_simulation_memorized(
    simulation_inputs: Sequence[tuple[int, RwkvReviewInput, int]],
    day: int,
) -> tuple[float, float]:
    introduced_inputs = [
        item for item in simulation_inputs if item[1].card_type != CARD_TYPE_NEW
    ]
    predictions = _rwkv_simulation_predictions(
        [
            (card_id, _rwkv_simulation_query_input(review_input, 0.9, day), batch_size)
            for card_id, review_input, batch_size in introduced_inputs
        ]
    )
    return _rwkv_memorized_from_predictions(predictions)


def _rwkv_simulation_memorized_from_cards(
    cards: Sequence[_RwkvSimulationCard],
    *,
    target_retention: float,
    day: int,
) -> tuple[float, float]:
    predictions = [
        _rwkv_simulation_prediction_for_card(
            card,
            target_retention=target_retention,
            day=day,
        )
        for card in cards
        if not card.is_new
    ]
    return _rwkv_memorized_from_predictions(predictions)


def _rwkv_memorized_from_predictions(
    predictions: Sequence[RwkvReviewPrediction | None],
) -> tuple[float, float]:
    memorized = 0.0
    weighted = 0.0
    for prediction in predictions:
        if prediction is None or prediction.retrievability is None:
            continue
        retrievability = prediction.retrievability
        if not _valid_probability(retrievability):
            continue
        memorized += retrievability
        weighted += retrievability * _rwkv_s90_weight(prediction.current_s90)
    return memorized, weighted


def _rwkv_simulation_prediction_for_card(
    card: _RwkvSimulationCard,
    *,
    target_retention: float,
    day: int,
) -> RwkvReviewPrediction | None:
    predictions = _rwkv_simulation_predictions(
        [
            (
                card.review_input.identity.card_id,
                _rwkv_simulation_query_input_for_card(
                    card,
                    target_retention=target_retention,
                    day=day,
                ),
                _DEFAULT_RWKV_REVIEW_BATCH_SIZE,
            )
        ]
    )
    return predictions[0] if predictions else None


def _rwkv_simulation_predictions(
    inputs: Sequence[tuple[int, RwkvReviewInput, int]],
) -> list[RwkvReviewPrediction | None]:
    predictions: list[RwkvReviewPrediction | None] = [None] * len(inputs)
    indexes_by_batch_size: dict[int, list[int]] = {}
    for index, (_, _, batch_size) in enumerate(inputs):
        indexes_by_batch_size.setdefault(batch_size, []).append(index)

    for batch_size, indexes in indexes_by_batch_size.items():
        batch = [(inputs[index][0], inputs[index][1]) for index in indexes]
        batch_predictions = _rwkv_review_predictions_for_inputs(
            batch,
            batch_size=batch_size,
        )
        if batch_predictions is None:
            continue
        for index, prediction in zip(indexes, batch_predictions, strict=True):
            predictions[index] = prediction

    return predictions


def _rwkv_simulation_query_input_for_card(
    card: _RwkvSimulationCard,
    *,
    target_retention: float,
    day: int,
) -> RwkvReviewInput:
    elapsed_days = max(0, day - card.last_review_day)
    return _rwkv_simulation_input(
        card.review_input,
        target_retention=target_retention,
        day=day,
        elapsed_days=elapsed_days,
        interval_days=card.interval_days,
        reps=card.reps,
        lapses=card.lapses,
        is_query=True,
        ease=None,
        duration_millis=None,
    )


def _rwkv_simulation_query_input(
    review_input: RwkvReviewInput,
    target_retention: float,
    day: int,
) -> RwkvReviewInput:
    elapsed_days = _rwkv_input_elapsed_days(review_input) + day
    return _rwkv_simulation_input(
        review_input,
        target_retention=target_retention,
        day=day,
        elapsed_days=elapsed_days,
        interval_days=review_input.interval_days or 1,
        reps=review_input.reps or 0,
        lapses=review_input.lapses or 0,
        is_query=True,
        ease=None,
        duration_millis=None,
    )


def _rwkv_simulation_answer_input(
    card: _RwkvSimulationCard,
    *,
    target_retention: float,
    day: int,
    ease: int,
    duration_millis: int,
) -> RwkvReviewInput:
    elapsed_days = max(0, day - card.last_review_day)
    return _rwkv_simulation_input(
        card.review_input,
        target_retention=target_retention,
        day=day,
        elapsed_days=elapsed_days,
        interval_days=card.interval_days,
        reps=card.reps,
        lapses=card.lapses,
        is_query=False,
        ease=ease,
        duration_millis=duration_millis,
    )


def _rwkv_simulation_input(
    review_input: RwkvReviewInput,
    *,
    target_retention: float,
    day: int,
    elapsed_days: int,
    interval_days: int,
    reps: int,
    lapses: int,
    is_query: bool,
    ease: int | None,
    duration_millis: int | None,
) -> RwkvReviewInput:
    day_offset = (
        review_input.day_offset + day
        if isinstance(review_input.day_offset, int)
        else None
    )
    return replace(
        review_input,
        is_query=is_query,
        ease=ease,
        duration_millis=duration_millis,
        day_offset=day_offset,
        current_elapsed_days=elapsed_days,
        current_elapsed_seconds=None,
        interval_days=interval_days,
        reps=reps,
        lapses=lapses,
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _rwkv_simulation_store_answer(review_input: RwkvReviewInput) -> None:
    backend = getattr(
        _reviewer_backend_prediction_local,
        "backend",
        None,
    )
    review_input_answered = getattr(backend, "review_input_answered", None)
    if not callable(review_input_answered):
        raise ValueError("RWKV backend does not support simulator state updates")
    review_input_answered(review_input)


def _rwkv_simulator_review_model(reviewer: object) -> _RwkvSimulatorReviewModel:
    rows = _rwkv_simulator_review_model_rows(reviewer)
    bucket_counts: dict[int, list[float]] = {}
    grade_seconds_sum = [0.0, 0.0, 0.0, 0.0]
    grade_seconds_count = [0, 0, 0, 0]
    timed_samples: list[tuple[float, int, float]] = []
    for retrievability_value, ease_value, taken_millis in rows:
        if (
            not isinstance(retrievability_value, (int, float))
            or isinstance(retrievability_value, bool)
            or not isinstance(ease_value, int)
            or isinstance(ease_value, bool)
            or ease_value not in (1, 2, 3, 4)
        ):
            continue
        retrievability = float(retrievability_value)
        ease = ease_value
        if not _valid_probability(retrievability):
            continue
        bucket = _rwkv_simulator_bucket(retrievability)
        bucket_counts.setdefault(bucket, [0.0, 0.0, 0.0, 0.0])[ease - 1] += 1.0
        if (
            isinstance(taken_millis, int)
            and not isinstance(taken_millis, bool)
            and 0 < taken_millis < _RWKV_SIMULATOR_MAX_TAKEN_MILLIS
        ):
            seconds = taken_millis / 1000.0
            grade_seconds_sum[ease - 1] += seconds
            grade_seconds_count[ease - 1] += 1
            timed_samples.append((retrievability, ease, seconds))

    bucket_probabilities: dict[int, tuple[float, float, float, float]] = {}
    for bucket, counts in bucket_counts.items():
        center = (bucket + 0.5) / _RWKV_SIMULATOR_BUCKET_COUNT
        fallback = _fallback_rwkv_grade_probabilities(center)
        smoothed = [
            count + fallback[index] * _RWKV_SIMULATOR_PRIOR_WEIGHT
            for index, count in enumerate(counts)
        ]
        total = sum(smoothed)
        if total > 0:
            bucket_probabilities[bucket] = cast(
                tuple[float, float, float, float],
                tuple(value / total for value in smoothed),
            )

    grade_seconds = tuple(
        grade_seconds_sum[index] / grade_seconds_count[index]
        if grade_seconds_count[index]
        else _RWKV_SIMULATOR_DEFAULT_GRADE_SECONDS[index]
        for index in range(4)
    )
    review_time = _rwkv_simulator_review_time_fields(
        timed_samples,
        bucket_counts,
        cast(tuple[float, float, float, float], grade_seconds),
    )
    return _RwkvSimulatorReviewModel(
        grade_seconds=cast(tuple[float, float, float, float], grade_seconds),
        bucket_probabilities=bucket_probabilities,
        review_time_r_bucket_count=review_time.r_bucket_count,
        review_time_s_bucket_count=review_time.s_bucket_count,
        review_time_again_seconds=review_time.again_seconds,
        review_time_hard_seconds=review_time.hard_seconds,
        review_time_good_seconds=review_time.good_seconds,
        review_time_easy_seconds=review_time.easy_seconds,
        review_time_sample_counts=review_time.sample_counts,
        review_time_again_coeffs=review_time.again_coeffs,
        review_time_hard_coeffs=review_time.hard_coeffs,
        review_time_good_coeffs=review_time.good_coeffs,
        review_time_easy_coeffs=review_time.easy_coeffs,
        review_time_grade_weights=review_time.grade_weights,
        review_time_transition_probs=review_time.transition_probs,
        review_time_transition_counts=review_time.transition_counts,
        review_time_success_grade_probs=review_time.success_grade_probs,
        review_time_success_grade_counts=review_time.success_grade_counts,
    )


def _rwkv_simulator_review_time_fields(
    timed_samples: Sequence[tuple[float, int, float]],
    bucket_counts: dict[int, list[float]],
    grade_seconds: tuple[float, float, float, float],
) -> _RwkvSimulatorReviewTimeFields:
    samples_by_grade: list[list[tuple[float, float]]] = [[], [], [], []]
    sample_counts = [0 for _ in range(_RWKV_SIMULATOR_BUCKET_COUNT)]
    success_grade_counts = [
        [0.0, 0.0, 0.0] for _ in range(_RWKV_SIMULATOR_BUCKET_COUNT)
    ]

    for retrievability, ease, seconds in timed_samples:
        if ease not in (1, 2, 3, 4):
            continue
        bucket = _rwkv_simulator_ui_bucket(retrievability)
        sample_counts[bucket] += 1
        samples_by_grade[ease - 1].append((retrievability, seconds))
        if ease > 1:
            success_grade_counts[bucket][ease - 2] += 1.0

    coeffs = tuple(
        _rwkv_simulator_review_time_coeffs(
            samples_by_grade[index], grade_seconds[index]
        )
        for index in range(4)
    )
    per_grade_seconds = tuple(
        tuple(
            _rwkv_simulator_review_time_for_bucket(
                bucket, grade_coeffs, grade_seconds[index]
            )
            for bucket in range(_RWKV_SIMULATOR_BUCKET_COUNT)
        )
        for index, grade_coeffs in enumerate(coeffs)
    )
    grade_weights = _rwkv_simulator_grade_weights(bucket_counts)
    success_probs = tuple(
        probability
        for bucket in range(_RWKV_SIMULATOR_BUCKET_COUNT)
        for probability in _rwkv_simulator_success_grade_probabilities(
            bucket,
            success_grade_counts[bucket],
        )
    )
    transition_probs = tuple(
        probability for _ in range(4) for probability in grade_weights
    )

    return _RwkvSimulatorReviewTimeFields(
        r_bucket_count=_RWKV_SIMULATOR_BUCKET_COUNT,
        s_bucket_count=1,
        again_seconds=per_grade_seconds[0],
        hard_seconds=per_grade_seconds[1],
        good_seconds=per_grade_seconds[2],
        easy_seconds=per_grade_seconds[3],
        sample_counts=tuple(sample_counts),
        again_coeffs=coeffs[0],
        hard_coeffs=coeffs[1],
        good_coeffs=coeffs[2],
        easy_coeffs=coeffs[3],
        grade_weights=grade_weights,
        transition_probs=transition_probs,
        transition_counts=tuple(0 for _ in range(16)),
        success_grade_probs=success_probs,
        success_grade_counts=tuple(int(sum(counts)) for counts in success_grade_counts),
    )


def _rwkv_simulator_review_time_coeffs(
    samples: Sequence[tuple[float, float]],
    fallback_seconds: float,
) -> tuple[float, float, float, float, float]:
    if len(samples) < 2:
        return (fallback_seconds, 0.0, 0.0, 0.0, 0.0)

    xs = [1.0 - max(0.0, min(1.0, retrievability)) for retrievability, _ in samples]
    ys = [seconds for _, seconds in samples]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 1e-9:
        return (mean_y, 0.0, 0.0, 0.0, 0.0)

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope /= denominator
    intercept = mean_y - slope * mean_x
    if math.isfinite(intercept) and math.isfinite(slope):
        return (intercept, slope, 0.0, 0.0, 0.0)
    return (fallback_seconds, 0.0, 0.0, 0.0, 0.0)


def _rwkv_simulator_review_time_for_bucket(
    bucket: int,
    coeffs: tuple[float, float, float, float, float],
    fallback_seconds: float,
) -> float:
    retrievability = max(0.0, min(1.0, 1.0 - (bucket + 0.5) * 0.05))
    predicted = coeffs[0] + coeffs[1] * (1.0 - retrievability)
    return (
        predicted if math.isfinite(predicted) and predicted > 0.0 else fallback_seconds
    )


def _rwkv_simulator_grade_weights(
    bucket_counts: dict[int, list[float]],
) -> tuple[float, float, float, float]:
    counts = [0.0, 0.0, 0.0, 0.0]
    for bucket in bucket_counts.values():
        for index, count in enumerate(bucket):
            counts[index] += count
    total = sum(counts)
    if total <= 0:
        return (0.25, 0.25, 0.25, 0.25)
    return cast(
        tuple[float, float, float, float], tuple(count / total for count in counts)
    )


def _rwkv_simulator_success_grade_probabilities(
    bucket: int,
    counts: Sequence[float],
) -> tuple[float, float, float]:
    retrievability = max(0.0, min(1.0, 1.0 - (bucket + 0.5) * 0.05))
    fallback = _fallback_rwkv_grade_probabilities(retrievability)
    fallback_success = fallback[1:]
    fallback_total = sum(fallback_success)
    prior = (
        tuple(value / fallback_total for value in fallback_success)
        if fallback_total > 0
        else (1 / 3, 1 / 3, 1 / 3)
    )
    smoothed = [
        counts[index] + prior[index] * _RWKV_SIMULATOR_PRIOR_WEIGHT
        for index in range(3)
    ]
    total = sum(smoothed)
    if total <= 0:
        return cast(tuple[float, float, float], prior)
    return cast(tuple[float, float, float], tuple(value / total for value in smoothed))


def _rwkv_simulator_review_model_rows(
    reviewer: object,
) -> list[tuple[object, object, object]]:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return []

    try:
        return cast(
            list[tuple[object, object, object]],
            all_rows(
                f"""
select cache.prediction, r.ease, r.time
from {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} cache
join revlog r on r.id = cache.revlog_id
where cache.prediction between 0 and 1
  and {_rwkv_historical_answer_sql_condition("r")}
"""
            ),
        )
    except Exception:
        logger.debug("failed to load RWKV simulator review-time samples")
        return []


def _fallback_rwkv_grade_probabilities(
    retrievability: float,
) -> tuple[float, float, float, float]:
    r = min(max(retrievability, 0.0), 1.0) if math.isfinite(retrievability) else 0.9
    again = min(max(1.0 - r, 0.02), 0.85)
    success = 1.0 - again
    hard_share = min(max((0.95 - r) / 0.45, 0.10), 0.45)
    easy_share = min(max((r - 0.75) / 0.25, 0.05), 0.35)
    if hard_share + easy_share > 0.90:
        scale = 0.90 / (hard_share + easy_share)
        hard_share *= scale
        easy_share *= scale
    hard = success * hard_share
    easy = success * easy_share
    good = max(0.0, success - hard - easy)
    total = again + hard + good + easy
    return again / total, hard / total, good / total, easy / total


def _rwkv_simulation_grade(
    probabilities: tuple[float, float, float, float],
    card_id: int,
    day: int,
    reps: int,
) -> int:
    threshold = _rwkv_simulation_unit_hash(card_id, day, reps)
    cumulative = 0.0
    for index, probability in enumerate(probabilities, start=1):
        cumulative += probability
        if threshold <= cumulative:
            return index
    return 4


def _rwkv_simulation_unit_hash(card_id: int, day: int, reps: int) -> float:
    value = (card_id & 0xFFFFFFFFFFFFFFFF) ^ ((day + 1) * 0x9E3779B185EBCA87)
    value ^= (reps + 1) * 0x165667B19E3779F9
    value &= 0xFFFFFFFFFFFFFFFF
    value ^= value >> 33
    value = (value * 0xFF51AFD7ED558CCD) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 33
    value = (value * 0xC4CEB9FE1A85EC53) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 33
    return value / 0xFFFFFFFFFFFFFFFF


def _rwkv_simulator_bucket(retrievability: float) -> int:
    if not math.isfinite(retrievability):
        return _RWKV_SIMULATOR_BUCKET_COUNT - 1
    return min(
        _RWKV_SIMULATOR_BUCKET_COUNT - 1,
        max(0, int(retrievability * _RWKV_SIMULATOR_BUCKET_COUNT)),
    )


def _rwkv_simulator_ui_bucket(retrievability: float) -> int:
    if not math.isfinite(retrievability):
        return _RWKV_SIMULATOR_BUCKET_COUNT - 1
    clamped = max(0.0, min(1.0, retrievability))
    base_index = int(min(clamped * 100.0, 99.9999) / 5.0)
    return _RWKV_SIMULATOR_BUCKET_COUNT - 1 - base_index


def _rwkv_input_elapsed_days(review_input: RwkvReviewInput) -> int:
    elapsed_days = review_input.current_elapsed_days
    if isinstance(elapsed_days, int) and not isinstance(elapsed_days, bool):
        return max(0, elapsed_days)
    return 0


def _valid_retrievability_or_default(value: object, default: float) -> float:
    return cast(float, value) if _valid_probability(value) else default


def _rwkv_s90_weight(current_s90: int | None) -> float:
    if current_s90 is None or current_s90 <= 0:
        return 1.0
    return 1.0 - math.exp((-8.0 / 365.0) * current_s90)


def reschedule_rwkv_review_cards_with_progress(
    mw: object,
    *,
    deck_id: int | None = None,
) -> None:
    """Reschedule RWKV-enabled review cards and persist current RWKV S90."""

    from aqt.operations import on_op_finished
    from aqt.utils import tooltip

    taskman = getattr(mw, "taskman", None)
    with_progress = getattr(taskman, "with_progress", None)
    if not callable(with_progress):
        result = reschedule_rwkv_review_cards(mw, deck_id=deck_id)
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
            return reschedule_rwkv_review_cards(
                mw,
                deck_id=deck_id,
                progress=progress,
            )

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
    deck_id: int | None = None,
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
    with _reviewer_backend_state_lock:
        backend = _reviewer_backend
    state_token = _capture_reviewer_backend_prediction_state_token(
        reviewer,
        expected_backend=backend,
    )
    if state_token is None:
        return RwkvReviewRescheduleResult(built=False, changes=None)

    items: list[RwkvReviewRescheduleItem] | None = None
    if deck_id is not None:
        try:
            items = _rwkv_review_reschedule_items_for_deck(
                reviewer,
                deck_id,
                progress=progress,
                state_token=state_token,
            )
        except _ReviewerBackendPredictionAborted:
            return RwkvReviewRescheduleResult(built=False, changes=None)

    if items is None:
        if not _reviewer_backend_prediction_state_token_is_current(state_token):
            return RwkvReviewRescheduleResult(built=False, changes=None)
        _report_rwkv_state_cache_progress(
            progress,
            "Finding RWKV review cards...",
        )
        card_ids = _rwkv_review_reschedule_card_ids(mw, deck_id=deck_id)
        if not card_ids:
            return RwkvReviewRescheduleResult(built=True, changes=None)

        try:
            items = _rwkv_review_reschedule_items(
                reviewer,
                card_ids,
                progress=progress,
                state_token=state_token,
            )
        except _ReviewerBackendPredictionAborted:
            return RwkvReviewRescheduleResult(built=False, changes=None)
        if items is None:
            return RwkvReviewRescheduleResult(built=False, changes=None)
    if not items:
        return RwkvReviewRescheduleResult(
            built=_reviewer_backend_prediction_state_token_is_current(state_token),
            changes=None,
        )

    _report_rwkv_state_cache_progress(
        progress,
        "Saving RWKV reschedule...",
        len(items),
        len(items),
    )
    changes = _apply_rwkv_review_reschedule_if_current(
        mw,
        items,
        state_token=state_token,
    )
    if changes is None:
        logger.debug("RWKV review reschedule discarded after backend state change")
        return RwkvReviewRescheduleResult(built=False, changes=None)
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
    backend: RwkvReviewerBackend,
    is_current: Callable[[], bool],
    require_retrievability_cache: bool = False,
    record_retrievability_cache: bool = False,
    progress: RwkvStateCacheProgressCallback | None = None,
    additional_ignored_review_ids: Sequence[int] = (),
) -> RwkvResidentStateIdentity | None:
    restore_snapshot = getattr(backend, "restore_cache_snapshot", None)
    warm_up = getattr(backend, "warm_up", None)
    if not callable(restore_snapshot) or not callable(warm_up):
        return None

    stored = _read_rwkv_state_cache(
        reviewer,
        backend=backend,
        additional_ignored_review_ids=additional_ignored_review_ids,
    )
    if stored is None:
        return None
    stored_history = stored.history
    if require_retrievability_cache and not _rwkv_review_retrievability_cache_complete(
        reviewer,
        last_review_id=stored_history.last_review_id,
        review_count=stored_history.review_count,
    ):
        logger.debug(
            "RWKV state cache restore skipped: retrievability cache incomplete"
        )
        return None

    stored_snapshot = stored.snapshot
    stored_metadata = stored.metadata
    pending_history = stored.pending_history
    reusable_checkpoint_entries = stored.reusable_checkpoint_entries
    recovered_from_checkpoint = stored.recovered_from_checkpoint
    desired_checkpoint_review_counts = stored.desired_checkpoint_review_counts
    state_store_path = stored.state_store_path
    state_store_generation = stored.state_store_generation
    state_store_segment_id = stored.state_store_segment_id
    ignored_review_ids_changed = stored.ignored_review_ids_changed
    del stored
    try:
        _require_reviewer_backend_warmup_current(is_current)
        if (
            state_store_path is not None
            and state_store_generation is not None
            and state_store_segment_id is not None
        ):
            restore_state_store = getattr(
                backend, "restore_state_cache_checkpoint", None
            )
            if not callable(restore_state_store):
                return None
            restore_state_store(
                state_store_path,
                state_store_generation,
                state_store_segment_id,
            )
        elif stored_snapshot is not None:
            restore_snapshot(stored_snapshot)
        else:
            return None
        del stored_snapshot
        if stored_history.reviews:
            _require_reviewer_backend_warmup_current(is_current)
            _report_rwkv_state_cache_progress(
                progress,
                "Loading RWKV cache deltas...",
            )
            _replay_rwkv_cache_reviews(
                backend,
                warm_up,
                stored_history.reviews,
                progress=progress,
                label="Loading RWKV cache deltas",
                is_current=is_current,
            )
            _require_reviewer_backend_warmup_current(is_current)

        _report_rwkv_state_cache_progress(
            progress,
            "Loading new RWKV reviews...",
        )
        history = pending_history
        if history is None:
            history = _historical_rwkv_review_inputs(
                reviewer,
                after_review_id=stored_history.last_review_id,
                progress=progress,
                previous_review_id_by_card=stored_history.previous_review_id_by_card,
                previous_interval_days_by_card=(
                    stored_history.previous_interval_days_by_card
                ),
                review_count_by_card=stored_history.review_count_by_card,
                previous_history_hash=stored_history.history_hash,
                previous_replay_key=stored_history.replay_key,
            )
        _require_reviewer_backend_warmup_current(is_current)
        if history.reviews:
            checkpoint_review_counts = []
            if recovered_from_checkpoint:
                checkpoint_review_counts = [
                    suffix_review_count
                    for target_review_count in desired_checkpoint_review_counts
                    if 0
                    < (
                        suffix_review_count := target_review_count
                        - stored_history.review_count
                    )
                    <= len(history.reviews)
                ]
            checkpoint_writer = _RwkvStateCacheCheckpointWriter(
                reviewer,
                history,
                [
                    *checkpoint_review_counts,
                    *(
                        [len(history.reviews)]
                        if state_store_path is not None and history.reviews
                        else []
                    ),
                ],
                full_review_counts=checkpoint_review_counts,
                base_history=stored_history,
                state_store_path=state_store_path,
                state_store_generation=state_store_generation,
                parent_segment_id=state_store_segment_id,
            )
            _require_reviewer_backend_warmup_current(is_current)
            _warm_up_rwkv_reviews(
                reviewer,
                backend,
                warm_up,
                history.reviews,
                review_ids=history.review_ids,
                progress=progress,
                label="Updating RWKV state cache",
                record_retrievability_cache=record_retrievability_cache,
                snapshot_after_reviews=[
                    *checkpoint_review_counts,
                    *(
                        [len(history.reviews)]
                        if state_store_path is not None and history.reviews
                        else []
                    ),
                ],
                snapshot_recorder=checkpoint_writer,
                is_current=is_current,
            )
            _require_reviewer_backend_warmup_current(is_current)
            _report_rwkv_state_cache_progress(
                progress,
                "Saving RWKV state cache...",
            )
            if recovered_from_checkpoint:
                desired_checkpoint_review_count_set = set(
                    desired_checkpoint_review_counts
                )
                _require_reviewer_backend_warmup_current(is_current)
                _save_reviewer_backend_cache(
                    reviewer,
                    history,
                    backend=backend,
                    checkpoint_entries=[
                        *[
                            entry
                            for entry in reusable_checkpoint_entries
                            if entry["reviewCount"]
                            in desired_checkpoint_review_count_set
                        ],
                        *checkpoint_writer.entries,
                    ],
                    write_context=checkpoint_writer.context,
                )
            elif _rwkv_state_cache_uses_current_model_key(stored_metadata):
                _require_reviewer_backend_warmup_current(is_current)
                _append_rwkv_state_cache_deltas(
                    reviewer,
                    history,
                    snapshot_review_id=_int_value(
                        stored_metadata.get("snapshotReviewId")
                    )
                    or stored_history.last_review_id,
                )
            else:
                _require_reviewer_backend_warmup_current(is_current)
                _save_reviewer_backend_cache(
                    reviewer,
                    history,
                    backend=backend,
                )
        elif recovered_from_checkpoint or ignored_review_ids_changed:
            _report_rwkv_state_cache_progress(
                progress,
                "Saving RWKV state cache...",
            )
            _require_reviewer_backend_warmup_current(is_current)
            existing_store_context = (
                _rwkv_state_cache_write_context(
                    reviewer,
                    state_store_path=state_store_path,
                    state_store_generation=state_store_generation,
                    parent_segment_id=state_store_segment_id,
                )
                if state_store_path is not None
                else None
            )
            if (
                ignored_review_ids_changed
                and stored_history.reviews
                and existing_store_context is not None
            ):
                write_checkpoint = getattr(
                    backend,
                    "write_state_cache_checkpoint",
                    None,
                )
                if not callable(write_checkpoint):
                    raise TypeError("RWKV state-store writer is unavailable")
                _write_rwkv_state_cache_store_checkpoint(
                    existing_store_context,
                    stored_history,
                    write_checkpoint,
                    full=False,
                )
            _save_reviewer_backend_cache(
                reviewer,
                stored_history,
                backend=backend,
                checkpoint_entries=[
                    entry
                    for entry in (
                        reusable_checkpoint_entries
                        if recovered_from_checkpoint
                        else _rwkv_state_cache_checkpoint_entries(stored_metadata)
                    )
                    if entry["reviewCount"] in set(desired_checkpoint_review_counts)
                ],
                write_context=existing_store_context,
            )
        elif not _rwkv_state_cache_uses_current_model_key(stored_metadata):
            _report_rwkv_state_cache_progress(
                progress,
                "Saving RWKV state cache...",
            )
            _require_reviewer_backend_warmup_current(is_current)
            _save_reviewer_backend_cache(
                reviewer,
                stored_history,
                backend=backend,
            )
        _require_reviewer_backend_warmup_current(is_current)
        _refresh_rwkv_state_cache_collection_mod(reviewer, history)
        logger.debug(
            "loaded RWKV state cache: cached_delta_reviews=%s "
            "incremental_reviews=%s last_review_id=%s",
            len(stored_history.reviews),
            len(history.reviews),
            history.last_review_id,
        )
        return _resident_state_identity(history)
    except _ReviewerBackendWarmupInvalidated:
        raise
    except Exception:
        logger.exception("failed to restore RWKV state cache")
        return None


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
select count(distinct r.id)
from revlog r
join {_RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} cache
  on cache.revlog_id = r.id
where {_rwkv_historical_answer_sql_condition("r")}
  and r.id <= ?
  and cache.prediction between 0 and 1
""",
            last_review_id,
        )
    except Exception:
        logger.debug("failed to check RWKV review retrievability cache completeness")
        return False

    return isinstance(cached, int) and cached >= review_count


class _RwkvCheckpointHistoryCursor:
    def __init__(
        self,
        history: RwkvHistoricalReviewInputs,
        *,
        base_history: RwkvHistoricalReviewInputs | None = None,
    ) -> None:
        if base_history is not None and base_history.replay_key != history.replay_key:
            raise ValueError("RWKV checkpoint histories use different replay semantics")
        if (
            base_history is not None
            and base_history.ignored_review_ids != history.ignored_review_ids
        ):
            raise ValueError("RWKV checkpoint histories ignore different reviews")
        self._history = history
        self._processed_reviews = 0
        self._has_base_history = base_history is not None
        self._base_review_count = base_history.review_count if base_history else 0
        self._previous_ids = (
            dict(base_history.previous_review_id_by_card) if base_history else {}
        )
        self._previous_intervals = (
            dict(base_history.previous_interval_days_by_card) if base_history else {}
        )
        self._review_counts = (
            dict(base_history.review_count_by_card) if base_history else {}
        )
        self._history_hash = (
            base_history.history_hash
            if base_history
            else _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
        )

    def advance(self, review_count: int) -> RwkvHistoricalReviewInputs:
        if not self._processed_reviews < review_count <= len(self._history.reviews):
            raise ValueError("invalid RWKV history checkpoint review count")

        if not self._has_base_history:
            if prepared := self._history.prepared_checkpoint_histories.get(
                review_count
            ):
                self._processed_reviews = review_count
                self._previous_ids = dict(prepared.previous_review_id_by_card)
                self._previous_intervals = dict(prepared.previous_interval_days_by_card)
                self._review_counts = dict(prepared.review_count_by_card)
                self._history_hash = prepared.history_hash
                return prepared
            if review_count == len(self._history.reviews):
                self._processed_reviews = review_count
                return replace(
                    self._history,
                    reviews=[],
                    review_ids=[],
                    prepared_checkpoint_histories={},
                )

        for index in range(self._processed_reviews, review_count):
            review_id = self._history.review_ids[index]
            review = self._history.reviews[index]
            card_id = review.identity.card_id
            self._previous_ids[card_id] = review_id
            if review.interval_days is not None:
                self._previous_intervals[card_id] = review.interval_days
            self._review_counts[card_id] = self._review_counts.get(card_id, 0) + 1
            self._history_hash = _rwkv_history_hash_after_review(
                self._history_hash,
                review_id,
                review,
            )

        self._processed_reviews = review_count
        return RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            # The checkpoint writer consumes these mappings synchronously before
            # the cursor advances again, so retaining copies is unnecessary.
            previous_review_id_by_card=self._previous_ids,
            previous_interval_days_by_card=self._previous_intervals,
            review_count_by_card=self._review_counts,
            last_review_id=self._history.review_ids[review_count - 1],
            review_count=self._base_review_count + review_count,
            deck_id=self._history.deck_id,
            history_hash=self._history_hash,
            replay_key=self._history.replay_key,
            ignored_review_ids=self._history.ignored_review_ids,
        )


class _RwkvStateCacheCheckpointWriter:
    def __init__(
        self,
        reviewer: object,
        history: RwkvHistoricalReviewInputs,
        review_counts: Sequence[int],
        *,
        full_review_counts: Sequence[int] = (),
        base_history: RwkvHistoricalReviewInputs | None = None,
        state_store_path: Path | None = None,
        state_store_generation: str | None = None,
        parent_segment_id: int | None = None,
    ) -> None:
        self._reviewer = reviewer
        self._remaining_review_counts = set(review_counts)
        self._full_review_counts = set(full_review_counts)
        if not self._full_review_counts <= self._remaining_review_counts:
            raise ValueError("full RWKV checkpoints must be snapshot endpoints")
        self._history_cursor = (
            _RwkvCheckpointHistoryCursor(
                history,
                base_history=base_history,
            )
            if self._remaining_review_counts
            else None
        )
        self._entries: dict[int, _RwkvStateCacheCheckpointEntry] = {}
        if not self._remaining_review_counts:
            self.context = None
        elif (
            state_store_path is None
            and state_store_generation is None
            and parent_segment_id is None
        ):
            self.context = _rwkv_state_cache_write_context(reviewer)
        else:
            self.context = _rwkv_state_cache_write_context(
                reviewer,
                state_store_path=state_store_path,
                state_store_generation=state_store_generation,
                parent_segment_id=parent_segment_id,
            )

    @property
    def entries(self) -> list[_RwkvStateCacheCheckpointEntry]:
        return [self._entries[review_id] for review_id in sorted(self._entries)]

    def __call__(
        self,
        review_count: int,
        snapshot: RwkvBackendCacheSnapshot,
    ) -> None:
        history = self._history_at(review_count)
        if self.context is None:
            return
        entry = _write_rwkv_state_cache_checkpoint(
            self._reviewer,
            self.context,
            history,
            snapshot,
        )
        self._store_entry(entry)

    def write_runtime_snapshot(
        self,
        review_count: int,
        append_snapshot: Callable[[Path], None],
    ) -> None:
        history = self._history_at(review_count)
        if self.context is None:
            return
        entry = _write_rwkv_state_cache_checkpoint_from_runtime(
            self._reviewer,
            self.context,
            history,
            append_snapshot,
        )
        self._store_entry(entry)

    def write_runtime_checkpoint(
        self,
        review_count: int,
        write_checkpoint: Callable[..., int],
    ) -> None:
        history = self._history_at(review_count)
        if self.context is None:
            return
        entry = _write_rwkv_state_cache_store_checkpoint(
            self.context,
            history,
            write_checkpoint,
            full=review_count in self._full_review_counts,
        )
        self._store_entry(entry)

    def _history_at(self, review_count: int) -> RwkvHistoricalReviewInputs:
        if review_count not in self._remaining_review_counts:
            raise ValueError("unexpected RWKV state checkpoint review count")
        self._remaining_review_counts.remove(review_count)
        if self._history_cursor is None:
            raise ValueError("RWKV state checkpoint history is unavailable")
        return self._history_cursor.advance(review_count)

    def _store_entry(
        self,
        entry: _RwkvStateCacheCheckpointEntry | None,
    ) -> None:
        if entry is not None:
            self._entries[entry["lastReviewId"]] = entry


def _rwkv_state_cache_write_context(
    reviewer: object,
    *,
    state_store_path: Path | None = None,
    state_store_generation: str | None = None,
    parent_segment_id: int | None = None,
) -> _RwkvStateCacheWriteContext | None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    return _RwkvStateCacheWriteContext(
        cache_dir=cache_dir,
        metadata_base=_rwkv_state_cache_metadata_base(reviewer),
        state_store_path=state_store_path,
        state_store_generation=state_store_generation,
        state_store_head_segment_id=parent_segment_id,
    )


def _rwkv_state_cache_checkpoint_entry(
    history: RwkvHistoricalReviewInputs,
    *,
    segment_id: int | None = None,
) -> _RwkvStateCacheCheckpointEntry:
    entry: _RwkvStateCacheCheckpointEntry = {
        "lastReviewId": history.last_review_id,
        "reviewCount": history.review_count,
        "historyHash": history.history_hash,
    }
    if segment_id is not None:
        entry["segmentId"] = segment_id
    return entry


def _write_rwkv_state_cache_store_checkpoint(
    context: _RwkvStateCacheWriteContext,
    history: RwkvHistoricalReviewInputs,
    write_checkpoint: Callable[..., int],
    *,
    full: bool,
) -> _RwkvStateCacheCheckpointEntry:
    _prepare_rwkv_state_cache_store_context(context)
    if context.state_store_path is None or context.state_store_generation is None:
        raise ValueError("RWKV state-cache store context is unavailable")
    started_at = time.monotonic()
    size_before = (
        context.state_store_path.stat().st_size
        if context.state_store_path.exists()
        else 0
    )
    parent_segment_id = None if full else context.state_store_head_segment_id
    segment_id = write_checkpoint(
        context.state_store_path,
        context.state_store_generation,
        parent_segment_id,
        history.last_review_id,
        history.review_count,
        history.history_hash,
        history.replay_key,
        *_encode_rwkv_state_cache_history_maps(history),
        full,
        not context.state_store_temporary,
    )
    context.state_store_head_segment_id = segment_id
    size_after = context.state_store_path.stat().st_size
    logger.debug(
        "saved RWKV delta state checkpoint: reviews=%s last_review_id=%s "
        "segment=%s parent=%s bytes_delta=%s elapsed_ms=%.1f",
        history.review_count,
        history.last_review_id,
        segment_id,
        parent_segment_id,
        max(0, size_after - size_before),
        (time.monotonic() - started_at) * 1000,
    )
    return _rwkv_state_cache_checkpoint_entry(history, segment_id=segment_id)


def _prepare_rwkv_state_cache_store_context(
    context: _RwkvStateCacheWriteContext,
) -> None:
    if context.state_store_path is not None:
        if not context.state_store_generation:
            raise ValueError("missing RWKV state-cache store generation")
        return
    store_path = context.cache_dir / _RWKV_STATE_CACHE_STORE_TEMP_FILE
    _remove_rwkv_state_cache_store_files(store_path)
    context.state_store_path = store_path
    context.state_store_generation = os.urandom(16).hex()
    context.state_store_temporary = True


def _write_rwkv_state_cache_checkpoint(
    reviewer: object,
    context: _RwkvStateCacheWriteContext,
    history: RwkvHistoricalReviewInputs,
    snapshot: RwkvBackendCacheSnapshot,
) -> _RwkvStateCacheCheckpointEntry | None:
    try:
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
            base_metadata=context.metadata_base,
        )
        checkpoint_path = _rwkv_state_cache_checkpoint_path(
            context.cache_dir,
            history.last_review_id,
        )
        _atomic_write_rwkv_state_cache_snapshot(
            checkpoint_path,
            metadata=metadata,
            snapshot=snapshot,
            history=history,
        )
        return _rwkv_state_cache_checkpoint_entry(history)
    except Exception:
        logger.exception(
            "failed to save RWKV state checkpoint: last_review_id=%s",
            history.last_review_id,
        )
        return None


def _write_rwkv_state_cache_checkpoint_from_runtime(
    reviewer: object,
    context: _RwkvStateCacheWriteContext,
    history: RwkvHistoricalReviewInputs,
    append_snapshot: Callable[[Path], None],
) -> _RwkvStateCacheCheckpointEntry | None:
    try:
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
            base_metadata=context.metadata_base,
        )
        checkpoint_path = _rwkv_state_cache_checkpoint_path(
            context.cache_dir,
            history.last_review_id,
        )
        _atomic_write_rwkv_state_cache_snapshot_from_runtime(
            checkpoint_path,
            metadata=metadata,
            append_snapshot=append_snapshot,
            history=history,
        )
        return _rwkv_state_cache_checkpoint_entry(history)
    except Exception:
        logger.exception(
            "failed to save RWKV state checkpoint from resident runtime: "
            "last_review_id=%s",
            history.last_review_id,
        )
        return None


def _remove_legacy_rwkv_state_cache_files(cache_dir: Path) -> None:
    for filename in _RWKV_STATE_CACHE_LEGACY_DATA_FILES:
        try:
            (cache_dir / filename).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "failed to remove legacy RWKV state cache: path=%s",
                cache_dir / filename,
                exc_info=True,
            )


def _remove_legacy_rwkv_state_cache_checkpoint_files(cache_dir: Path) -> None:
    for checkpoint_path in cache_dir.glob(
        f"{_RWKV_STATE_CACHE_CHECKPOINT_PREFIX}*{_RWKV_STATE_CACHE_CHECKPOINT_SUFFIX}"
    ):
        try:
            checkpoint_path.unlink()
        except OSError:
            logger.warning(
                "failed to remove legacy RWKV state checkpoint: path=%s",
                checkpoint_path,
                exc_info=True,
            )


def _save_reviewer_backend_cache(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    backend: RwkvReviewerBackend | None = None,
    checkpoint_entries: Sequence[_RwkvStateCacheCheckpointEntry] = (),
    write_context: _RwkvStateCacheWriteContext | None = None,
) -> None:
    if history.deck_id is not None:
        _log_scoped_rwkv_state_cache_write_skip("save", history)
        return

    if backend is None:
        with _reviewer_backend_state_lock:
            backend = _reviewer_backend
    cache_snapshot = getattr(backend, "cache_snapshot", None)
    append_snapshot = getattr(backend, "append_cache_snapshot_binary", None)
    supports_streaming = getattr(backend, "supports_streaming_cache_snapshot", None)
    stream_snapshot = (
        callable(append_snapshot)
        and callable(supports_streaming)
        and supports_streaming()
    )
    if not stream_snapshot and not callable(cache_snapshot):
        return

    context = write_context or _rwkv_state_cache_write_context(reviewer)
    if context is None:
        return
    cache_dir = context.cache_dir

    try:
        supports_delta_state_store_fn = getattr(
            backend,
            "supports_delta_state_store",
            None,
        )
        supports_delta_state_store = (
            callable(supports_delta_state_store_fn) and supports_delta_state_store_fn()
        )
        if context.state_store_head_segment_id is not None or (
            supports_delta_state_store
            and callable(getattr(backend, "write_state_cache_checkpoint", None))
        ):
            _save_reviewer_backend_state_store(
                reviewer,
                history,
                backend=backend,
                checkpoint_entries=checkpoint_entries,
                context=context,
            )
            return

        entries_by_review_id = {
            entry["lastReviewId"]: entry
            for entry in checkpoint_entries
            if 0 < entry["lastReviewId"] < history.last_review_id
        }
        retained_checkpoint_entries = [
            entries_by_review_id[review_id]
            for review_id in sorted(entries_by_review_id)
        ]
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=history.last_review_id,
            checkpoint_entries=retained_checkpoint_entries,
            base_metadata=context.metadata_base,
        )
        snapshot_path = cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE
        if stream_snapshot:
            _atomic_write_rwkv_state_cache_snapshot_from_runtime(
                snapshot_path,
                metadata=metadata,
                append_snapshot=cast(Callable[[Path], None], append_snapshot),
                history=history,
            )
        else:
            snapshot = cast(Callable[[], RwkvBackendCacheSnapshot], cache_snapshot)()
            _atomic_write_rwkv_state_cache_snapshot(
                snapshot_path,
                metadata=metadata,
                snapshot=snapshot,
                history=history,
            )
        retained_checkpoint_paths = {
            _rwkv_state_cache_checkpoint_path(
                cache_dir,
                entry["lastReviewId"],
            )
            for entry in retained_checkpoint_entries
        }
        for checkpoint_path in cache_dir.glob(
            f"{_RWKV_STATE_CACHE_CHECKPOINT_PREFIX}*"
            f"{_RWKV_STATE_CACHE_CHECKPOINT_SUFFIX}"
        ):
            if checkpoint_path not in retained_checkpoint_paths:
                checkpoint_path.unlink()
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE, _rwkv_empty_deltas_log()
        )
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_META_FILE,
            json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf8"),
        )
        _remove_legacy_rwkv_state_cache_files(cache_dir)
        logger.debug(
            "saved RWKV state cache snapshot: reviews=%s last_review_id=%s "
            "checkpoints=%s bytes=%s",
            history.review_count,
            history.last_review_id,
            len(retained_checkpoint_entries),
            snapshot_path.stat().st_size,
        )
    except Exception:
        logger.exception("failed to save RWKV state cache")


def _save_reviewer_backend_state_store(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    backend: RwkvReviewerBackend,
    checkpoint_entries: Sequence[_RwkvStateCacheCheckpointEntry],
    context: _RwkvStateCacheWriteContext,
) -> None:
    write_checkpoint = getattr(backend, "write_state_cache_checkpoint", None)
    if context.state_store_head_segment_id is None:
        if not callable(write_checkpoint):
            raise TypeError("RWKV state-cache store writer is unavailable")
        _prepare_rwkv_state_cache_store_context(context)
        if context.state_store_path is None or context.state_store_generation is None:
            raise ValueError("RWKV state-cache store context is unavailable")
        context.state_store_head_segment_id = write_checkpoint(
            context.state_store_path,
            context.state_store_generation,
            None,
            history,
            full=True,
            durable=not context.state_store_temporary,
        )

    _finish_rwkv_state_cache_checkpoint_writes(backend)
    snapshot_segment_id = context.state_store_head_segment_id
    if snapshot_segment_id is None or context.state_store_generation is None:
        raise ValueError("missing RWKV state-cache head segment")
    entries_by_review_id = {
        entry["lastReviewId"]: entry
        for entry in checkpoint_entries
        if 0 < entry["lastReviewId"] < history.last_review_id
        and _int_value(entry.get("segmentId")) is not None
    }
    retained_checkpoint_entries = [
        entries_by_review_id[review_id] for review_id in sorted(entries_by_review_id)
    ]
    metadata = _rwkv_state_cache_metadata(
        reviewer,
        history,
        snapshot_review_id=history.last_review_id,
        checkpoint_entries=retained_checkpoint_entries,
        base_metadata=context.metadata_base,
        state_store_generation=context.state_store_generation,
        snapshot_segment_id=snapshot_segment_id,
    )
    store_path = context.state_store_path
    if store_path is None:
        raise ValueError("missing RWKV state-cache store path")
    live_store_path = context.cache_dir / _RWKV_STATE_CACHE_STORE_FILE
    if context.state_store_temporary:
        os.replace(store_path, live_store_path)
        context.state_store_path = live_store_path
        context.state_store_temporary = False
    elif store_path != live_store_path:
        raise ValueError("unexpected RWKV state-cache store path")
    _atomic_write(
        context.cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE,
        _rwkv_empty_deltas_log(),
    )
    _atomic_write(
        context.cache_dir / _RWKV_STATE_CACHE_META_FILE,
        json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("utf8"),
    )
    (context.cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE).unlink(missing_ok=True)
    _remove_legacy_rwkv_state_cache_files(context.cache_dir)
    _remove_legacy_rwkv_state_cache_checkpoint_files(context.cache_dir)
    try:
        _prune_rwkv_state_cache_store(
            live_store_path,
            context.state_store_generation,
            snapshot_segment_id,
        )
    except Exception:
        logger.warning(
            "failed to prune obsolete RWKV state-cache branches",
            exc_info=True,
        )
    logger.debug(
        "saved RWKV delta state store: reviews=%s last_review_id=%s "
        "checkpoints=%s segments_head=%s bytes=%s",
        history.review_count,
        history.last_review_id,
        len(retained_checkpoint_entries),
        snapshot_segment_id,
        live_store_path.stat().st_size,
    )


def _finish_rwkv_state_cache_checkpoint_writes(backend: object) -> None:
    finish = getattr(backend, "finish_state_cache_checkpoints", None)
    if callable(finish):
        finish()


def _finish_rwkv_state_cache_checkpoint_writes_safely(backend: object) -> None:
    try:
        _finish_rwkv_state_cache_checkpoint_writes(backend)
    except Exception:
        logger.warning(
            "failed to close RWKV state-cache checkpoint writer",
            exc_info=True,
        )


def _prune_rwkv_state_cache_store(
    path: Path,
    store_generation: str,
    head_segment_id: int,
) -> None:
    reachable_segment_ids = _rwkv_state_cache_store_segment_chain(
        path,
        store_generation,
        head_segment_id,
    )
    placeholders = ",".join("?" for _ in reachable_segment_ids)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"delete from segment_state_chunks where segment_id not in ({placeholders})",
            reachable_segment_ids,
        )
        connection.execute(
            f"delete from segments where id not in ({placeholders})",
            reachable_segment_ids,
        )


def _append_rwkv_state_cache_deltas(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    snapshot_review_id: int,
) -> None:
    if history.deck_id is not None:
        _log_scoped_rwkv_state_cache_write_skip("append deltas", history)
        return

    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        existing_metadata = _read_rwkv_state_cache_metadata(reviewer)
        snapshot_history_hash = (
            existing_metadata.get("snapshotHistoryHash")
            if isinstance(existing_metadata, dict)
            else None
        )
        if not _rwkv_history_hash_is_valid(snapshot_history_hash):
            raise ValueError("missing RWKV snapshot history identity")
        delta_path = cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE
        _append_rwkv_delta_records(delta_path, history.review_ids, history.reviews)
        metadata = _rwkv_state_cache_metadata(
            reviewer,
            history,
            snapshot_review_id=snapshot_review_id,
            snapshot_history_hash=cast(str, snapshot_history_hash),
            checkpoint_entries=_rwkv_state_cache_checkpoint_entries(
                existing_metadata,
            ),
            state_store_generation=(
                cast(str, existing_metadata.get("storeGeneration"))
                if existing_metadata.get("storage") == _RWKV_STATE_CACHE_STORE_KIND
                and isinstance(existing_metadata.get("storeGeneration"), str)
                else None
            ),
            snapshot_segment_id=(
                _int_value(existing_metadata.get("snapshotSegmentId"))
                if existing_metadata.get("storage") == _RWKV_STATE_CACHE_STORE_KIND
                else None
            ),
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


def _log_scoped_rwkv_state_cache_write_skip(
    action: str,
    history: RwkvHistoricalReviewInputs,
) -> None:
    logger.warning(
        "refusing to %s scoped RWKV state cache history: deck_id=%s reviews=%s "
        "last_review_id=%s review_count=%s",
        action,
        history.deck_id,
        len(history.reviews),
        history.last_review_id,
        history.review_count,
    )


def _read_rwkv_state_cache(
    reviewer: object,
    *,
    backend: RwkvReviewerBackend | None = None,
    additional_ignored_review_ids: Sequence[int] = (),
) -> RwkvStoredStateCache | None:
    stored = _read_rwkv_state_cache_binary(
        reviewer,
        backend=backend,
        additional_ignored_review_ids=additional_ignored_review_ids,
    )
    if stored is not None:
        return stored

    return _read_rwkv_state_cache_legacy_json(reviewer)


def _read_rwkv_state_cache_binary(  # noqa: PLR0911
    reviewer: object,
    *,
    backend: RwkvReviewerBackend | None = None,
    dynamic_preset_replay_enabled: bool | None = None,
    additional_ignored_review_ids: Sequence[int] = (),
) -> RwkvStoredStateCache | None:
    location = _rwkv_state_cache_binary_location(reviewer)
    if location is None:
        return None
    cache_dir, metadata = location
    if not _rwkv_state_cache_metadata_compatible(
        reviewer,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
    ):
        return None
    if not additional_ignored_review_ids and _rwkv_state_cache_collection_unchanged(
        reviewer,
        metadata,
    ):
        stored = _read_unchanged_rwkv_state_cache_binary(
            reviewer,
            backend=backend,
            cache_dir=cache_dir,
            metadata=metadata,
        )
        if stored is not None:
            logger.debug("validated RWKV state cache from unchanged collection marker")
            return stored
    existing_ignored_review_ids = _rwkv_state_cache_ignored_review_ids(metadata)
    metadata_last_review_id = _int_value(metadata.get("lastReviewId")) or 0
    newest_known_review_id = max(
        (metadata_last_review_id, *additional_ignored_review_ids)
    )
    ignore_cutoff = newest_known_review_id - _RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS
    newly_ignored_review_ids = {
        review_id
        for review_id in additional_ignored_review_ids
        if review_id > 0 and review_id <= ignore_cutoff
    }
    try:
        current_history = _historical_rwkv_review_inputs(
            reviewer,
            ignored_review_ids=frozenset(
                (*existing_ignored_review_ids, *newly_ignored_review_ids)
            ),
        )
    except Exception:
        logger.exception("failed to validate current RWKV replay history")
        return None
    ignored_review_ids_changed = (
        current_history.ignored_review_ids != existing_ignored_review_ids
    )
    desired_checkpoint_review_counts = tuple(
        _rwkv_recovery_checkpoint_review_counts(current_history.review_ids)
    )
    if not _rwkv_state_cache_metadata_compatible(
        reviewer,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        replay_key=current_history.replay_key,
    ):
        return None
    if metadata.get("storage") == _RWKV_STATE_CACHE_STORE_KIND:
        stored = _read_rwkv_state_cache_store(
            reviewer,
            backend=backend,
            cache_dir=cache_dir,
            metadata=metadata,
            current_history=current_history,
            desired_checkpoint_review_counts=desired_checkpoint_review_counts,
            dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        )
        return (
            replace(
                stored,
                ignored_review_ids_changed=ignored_review_ids_changed,
            )
            if stored is not None
            else None
        )

    snapshot_path = cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE
    if not snapshot_path.exists():
        return None

    try:
        snapshot_metadata = _validate_rwkv_state_cache_snapshot_file(snapshot_path)
    except Exception:
        logger.exception("failed to validate binary RWKV state cache")
        return None
    if not _rwkv_state_cache_metadata_matches_manifest(
        snapshot_metadata,
        metadata,
    ):
        return None

    checkpoint_entries = _rwkv_state_cache_checkpoint_entries(metadata)
    prefix_review_ids = [
        _int_value(snapshot_metadata.get("lastReviewId")) or 0,
        *[entry["lastReviewId"] for entry in checkpoint_entries],
    ]
    metadata_last_review_id = _int_value(metadata.get("lastReviewId"))
    if metadata_last_review_id is not None:
        prefix_review_ids.append(metadata_last_review_id)
    current_prefix_identities = _rwkv_history_prefix_identities(
        current_history,
        prefix_review_ids,
    )

    snapshot: RwkvBackendCacheSnapshot | None = None
    snapshot_history: RwkvHistoricalReviewInputs | None = None
    snapshot_decode_failed = False
    if _rwkv_state_cache_metadata_usable(
        reviewer,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        current_history=current_history,
        current_prefix_identities=current_prefix_identities,
    ):
        try:
            decoded_metadata, snapshot, snapshot_history = (
                _read_rwkv_state_cache_snapshot_file(snapshot_path)
            )
            if not _rwkv_state_cache_metadata_matches_manifest(
                decoded_metadata,
                metadata,
            ):
                raise ValueError("RWKV snapshot metadata changed while reading")
        except Exception:
            snapshot = None
            snapshot_history = None
            snapshot_decode_failed = True
            logger.warning(
                "failed to decode effective RWKV state snapshot; "
                "trying an earlier checkpoint",
                exc_info=True,
            )
        else:
            try:
                snapshot_history = replace(
                    snapshot_history,
                    ignored_review_ids=current_history.ignored_review_ids,
                )
                delta_reviews = _read_rwkv_delta_records(
                    cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE,
                    after_review_id=snapshot_history.last_review_id,
                    until_review_id=_int_value(metadata.get("lastReviewId")) or 0,
                )
                history = _rwkv_history_after_delta_reviews(
                    snapshot_history,
                    delta_reviews,
                )
                if (
                    history.last_review_id
                    == (_int_value(metadata.get("lastReviewId")) or 0)
                    and history.review_count
                    == (_int_value(metadata.get("reviewCount")) or 0)
                    and history.history_hash == metadata.get("historyHash")
                ):
                    return RwkvStoredStateCache(
                        metadata=metadata,
                        snapshot=snapshot,
                        history=history,
                        pending_history=_rwkv_validated_history_suffix(
                            current_history,
                            history,
                        ),
                        desired_checkpoint_review_counts=(
                            desired_checkpoint_review_counts
                        ),
                        ignored_review_ids_changed=ignored_review_ids_changed,
                    )
            except Exception:
                logger.warning(
                    "failed to restore effective RWKV delta state; "
                    "trying an earlier checkpoint",
                    exc_info=True,
                )

    base_snapshot_usable = _rwkv_state_cache_metadata_usable(
        reviewer,
        snapshot_metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        current_history=current_history,
        current_prefix_identities=current_prefix_identities,
    )
    if snapshot_decode_failed:
        base_snapshot_usable = False
    if base_snapshot_usable and snapshot is None and snapshot_history is None:
        try:
            decoded_metadata, snapshot, snapshot_history = (
                _read_rwkv_state_cache_snapshot_file(snapshot_path)
            )
            if not _rwkv_state_cache_metadata_matches_manifest(
                decoded_metadata,
                metadata,
            ):
                raise ValueError("RWKV snapshot metadata changed while reading")
            snapshot_history = replace(
                snapshot_history,
                ignored_review_ids=current_history.ignored_review_ids,
            )
        except Exception:
            logger.warning(
                "failed to read base RWKV state snapshot; trying an earlier checkpoint",
                exc_info=True,
            )
            snapshot = None
            snapshot_history = None
            base_snapshot_usable = False

    reusable_checkpoint_entries: list[_RwkvStateCacheCheckpointEntry] = []
    for entry in checkpoint_entries:
        identity = current_prefix_identities.get(entry["lastReviewId"])
        if identity != _RwkvHistoryPrefixIdentity(
            last_review_id=entry["lastReviewId"],
            review_count=entry["reviewCount"],
            history_hash=entry["historyHash"],
        ):
            continue
        checkpoint_path = _rwkv_state_cache_checkpoint_path(
            cache_dir,
            entry["lastReviewId"],
        )
        try:
            checkpoint_metadata = _validate_rwkv_state_cache_snapshot_file(
                checkpoint_path
            )
        except FileNotFoundError:
            continue
        except Exception:
            logger.warning(
                "failed to validate RWKV state checkpoint: path=%s",
                checkpoint_path,
                exc_info=True,
            )
            continue
        if _rwkv_state_cache_checkpoint_metadata_matches_manifest(
            checkpoint_metadata,
            metadata,
            entry,
        ):
            reusable_checkpoint_entries.append(entry)

    selected_snapshot = snapshot if base_snapshot_usable else None
    selected_history = snapshot_history if base_snapshot_usable else None
    if not base_snapshot_usable:
        while reusable_checkpoint_entries:
            selected_entry = reusable_checkpoint_entries[-1]
            checkpoint_path = _rwkv_state_cache_checkpoint_path(
                cache_dir,
                selected_entry["lastReviewId"],
            )
            try:
                (
                    checkpoint_metadata,
                    checkpoint_snapshot,
                    checkpoint_history,
                ) = _read_rwkv_state_cache_snapshot_file(checkpoint_path)
            except Exception:
                logger.warning(
                    "failed to read RWKV state checkpoint: path=%s",
                    checkpoint_path,
                    exc_info=True,
                )
                reusable_checkpoint_entries.pop()
                continue
            checkpoint_history = replace(
                checkpoint_history,
                ignored_review_ids=current_history.ignored_review_ids,
            )
            if not _rwkv_state_cache_checkpoint_metadata_matches_manifest(
                checkpoint_metadata,
                metadata,
                selected_entry,
            ) or not _rwkv_state_cache_history_prefix_matches(
                reviewer,
                checkpoint_history,
                current_history=current_history,
                current_prefix_identities=current_prefix_identities,
            ):
                reusable_checkpoint_entries.pop()
                continue
            selected_snapshot = checkpoint_snapshot
            selected_history = checkpoint_history
            break

    if selected_snapshot is None or selected_history is None:
        return None

    logger.info(
        "recovering RWKV state from historical checkpoint: last_review_id=%s "
        "review_count=%s available_checkpoints=%s",
        selected_history.last_review_id,
        selected_history.review_count,
        len(reusable_checkpoint_entries) + int(base_snapshot_usable),
    )
    return RwkvStoredStateCache(
        metadata=metadata,
        snapshot=selected_snapshot,
        history=selected_history,
        pending_history=_rwkv_validated_history_suffix(
            current_history,
            selected_history,
        ),
        reusable_checkpoint_entries=tuple(reusable_checkpoint_entries),
        recovered_from_checkpoint=True,
        desired_checkpoint_review_counts=desired_checkpoint_review_counts,
        ignored_review_ids_changed=ignored_review_ids_changed,
    )


def _read_rwkv_state_cache_store(
    reviewer: object,
    *,
    backend: RwkvReviewerBackend | None,
    cache_dir: Path,
    metadata: dict[str, object],
    current_history: RwkvHistoricalReviewInputs,
    desired_checkpoint_review_counts: tuple[int, ...],
    dynamic_preset_replay_enabled: bool | None,
) -> RwkvStoredStateCache | None:
    if not callable(getattr(backend, "restore_state_cache_checkpoint", None)):
        return None
    store_generation = metadata.get("storeGeneration")
    snapshot_segment_id = _int_value(metadata.get("snapshotSegmentId"))
    if (
        not isinstance(store_generation, str)
        or not store_generation
        or snapshot_segment_id is None
        or snapshot_segment_id <= 0
    ):
        return None
    store_path = cache_dir / _RWKV_STATE_CACHE_STORE_FILE
    try:
        snapshot_history = _read_rwkv_state_cache_store_segment_history(
            store_path,
            store_generation,
            snapshot_segment_id,
        )
        state_store_segment_chain = set(
            _rwkv_state_cache_store_segment_chain(
                store_path,
                store_generation,
                snapshot_segment_id,
            )
        )
    except Exception:
        logger.exception("failed to validate RWKV delta state store")
        return None
    snapshot_history = replace(
        snapshot_history,
        ignored_review_ids=current_history.ignored_review_ids,
    )
    if snapshot_history.replay_key != current_history.replay_key:
        return None
    manifest_matches_snapshot = snapshot_history.last_review_id == (
        _int_value(metadata.get("snapshotReviewId")) or 0
    ) and snapshot_history.history_hash == metadata.get("snapshotHistoryHash")

    checkpoint_entries = _rwkv_state_cache_checkpoint_entries(metadata)
    prefix_review_ids = [
        snapshot_history.last_review_id,
        *[entry["lastReviewId"] for entry in checkpoint_entries],
    ]
    metadata_last_review_id = _int_value(metadata.get("lastReviewId"))
    if metadata_last_review_id is not None:
        prefix_review_ids.append(metadata_last_review_id)
    current_prefix_identities = _rwkv_history_prefix_identities(
        current_history,
        prefix_review_ids,
    )

    if manifest_matches_snapshot and _rwkv_state_cache_metadata_usable(
        reviewer,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        current_history=current_history,
        current_prefix_identities=current_prefix_identities,
    ):
        try:
            delta_reviews = _read_rwkv_delta_records(
                cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE,
                after_review_id=snapshot_history.last_review_id,
                until_review_id=_int_value(metadata.get("lastReviewId")) or 0,
            )
            history = _rwkv_history_after_delta_reviews(
                snapshot_history,
                delta_reviews,
            )
            if (
                history.last_review_id
                == (_int_value(metadata.get("lastReviewId")) or 0)
                and history.review_count
                == (_int_value(metadata.get("reviewCount")) or 0)
                and history.history_hash == metadata.get("historyHash")
            ):
                return RwkvStoredStateCache(
                    metadata=metadata,
                    snapshot=None,
                    history=history,
                    pending_history=_rwkv_validated_history_suffix(
                        current_history,
                        history,
                    ),
                    desired_checkpoint_review_counts=(desired_checkpoint_review_counts),
                    state_store_path=store_path,
                    state_store_generation=store_generation,
                    state_store_segment_id=snapshot_segment_id,
                )
        except Exception:
            logger.warning(
                "failed to restore effective RWKV state-store deltas; "
                "trying an earlier segment",
                exc_info=True,
            )

    valid_checkpoints: list[
        tuple[_RwkvStateCacheCheckpointEntry, RwkvHistoricalReviewInputs, int]
    ] = []
    for entry in checkpoint_entries:
        segment_id = _int_value(entry.get("segmentId"))
        identity = current_prefix_identities.get(entry["lastReviewId"])
        if (
            segment_id is None
            or segment_id <= 0
            or segment_id not in state_store_segment_chain
            or identity
            != _RwkvHistoryPrefixIdentity(
                last_review_id=entry["lastReviewId"],
                review_count=entry["reviewCount"],
                history_hash=entry["historyHash"],
            )
        ):
            continue
        try:
            checkpoint_history = _read_rwkv_state_cache_store_segment_history(
                store_path,
                store_generation,
                segment_id,
            )
            checkpoint_history = replace(
                checkpoint_history,
                ignored_review_ids=current_history.ignored_review_ids,
            )
        except Exception:
            logger.warning(
                "failed to validate RWKV state-store checkpoint: segment=%s",
                segment_id,
                exc_info=True,
            )
            continue
        if (
            checkpoint_history.last_review_id == entry["lastReviewId"]
            and checkpoint_history.review_count == entry["reviewCount"]
            and checkpoint_history.history_hash == entry["historyHash"]
            and checkpoint_history.replay_key == current_history.replay_key
        ):
            valid_checkpoints.append((entry, checkpoint_history, segment_id))

    candidates = list(valid_checkpoints)
    snapshot_identity = current_prefix_identities.get(snapshot_history.last_review_id)
    if snapshot_identity == _RwkvHistoryPrefixIdentity(
        last_review_id=snapshot_history.last_review_id,
        review_count=snapshot_history.review_count,
        history_hash=snapshot_history.history_hash,
    ):
        candidates.append(
            (
                _rwkv_state_cache_checkpoint_entry(
                    snapshot_history,
                    segment_id=snapshot_segment_id,
                ),
                snapshot_history,
                snapshot_segment_id,
            )
        )
    if not candidates:
        return None
    _selected_entry, selected_history, selected_segment_id = max(
        candidates,
        key=lambda candidate: candidate[1].review_count,
    )
    reusable_checkpoint_entries = tuple(
        entry
        for entry, checkpoint_history, _segment_id in valid_checkpoints
        if checkpoint_history.review_count <= selected_history.review_count
    )
    logger.info(
        "recovering RWKV state from delta-store checkpoint: last_review_id=%s "
        "review_count=%s available_checkpoints=%s",
        selected_history.last_review_id,
        selected_history.review_count,
        len(reusable_checkpoint_entries),
    )
    return RwkvStoredStateCache(
        metadata=metadata,
        snapshot=None,
        history=selected_history,
        pending_history=_rwkv_validated_history_suffix(
            current_history,
            selected_history,
        ),
        reusable_checkpoint_entries=reusable_checkpoint_entries,
        recovered_from_checkpoint=True,
        desired_checkpoint_review_counts=desired_checkpoint_review_counts,
        state_store_path=store_path,
        state_store_generation=store_generation,
        state_store_segment_id=selected_segment_id,
    )


def _read_unchanged_rwkv_state_cache_binary(
    reviewer: object,
    *,
    backend: RwkvReviewerBackend | None,
    cache_dir: Path,
    metadata: dict[str, object],
) -> RwkvStoredStateCache | None:
    """Read the effective cache state after a collection marker match.

    The marker proves the collection inputs have not changed since this
    manifest was written. The persisted files are still checked against the
    manifest before their state is accepted.
    """

    try:
        if metadata.get("storage") == _RWKV_STATE_CACHE_STORE_KIND:
            return _read_unchanged_rwkv_state_cache_store(
                backend=backend,
                cache_dir=cache_dir,
                metadata=metadata,
            )

        snapshot_path = cache_dir / _RWKV_STATE_CACHE_SNAPSHOT_FILE
        snapshot_metadata = _validate_rwkv_state_cache_snapshot_file(snapshot_path)
        if not _rwkv_state_cache_metadata_matches_manifest(
            snapshot_metadata,
            metadata,
        ):
            return None
        decoded_metadata, snapshot, snapshot_history = (
            _read_rwkv_state_cache_snapshot_file(snapshot_path)
        )
        if not _rwkv_state_cache_metadata_matches_manifest(
            decoded_metadata,
            metadata,
        ):
            return None
        snapshot_history = replace(
            snapshot_history,
            ignored_review_ids=_rwkv_state_cache_ignored_review_ids(metadata),
        )
        history = _rwkv_effective_cached_history(
            cache_dir,
            metadata,
            snapshot_history,
        )
        if history is None:
            return None
        return RwkvStoredStateCache(
            metadata=metadata,
            snapshot=snapshot,
            history=history,
            pending_history=_rwkv_empty_history_suffix(history),
        )
    except Exception:
        logger.warning(
            "failed to read RWKV cache through unchanged-collection fast path; "
            "falling back to canonical validation",
            exc_info=True,
        )
        return None


def _read_unchanged_rwkv_state_cache_store(
    *,
    backend: RwkvReviewerBackend | None,
    cache_dir: Path,
    metadata: dict[str, object],
) -> RwkvStoredStateCache | None:
    if not callable(getattr(backend, "restore_state_cache_checkpoint", None)):
        return None
    store_generation = metadata.get("storeGeneration")
    snapshot_segment_id = _int_value(metadata.get("snapshotSegmentId"))
    if (
        not isinstance(store_generation, str)
        or not store_generation
        or snapshot_segment_id is None
        or snapshot_segment_id <= 0
    ):
        return None

    store_path = cache_dir / _RWKV_STATE_CACHE_STORE_FILE
    snapshot_history = _read_rwkv_state_cache_store_segment_history(
        store_path,
        store_generation,
        snapshot_segment_id,
    )
    _rwkv_state_cache_store_segment_chain(
        store_path,
        store_generation,
        snapshot_segment_id,
    )
    snapshot_history = replace(
        snapshot_history,
        ignored_review_ids=_rwkv_state_cache_ignored_review_ids(metadata),
    )
    history = _rwkv_effective_cached_history(
        cache_dir,
        metadata,
        snapshot_history,
    )
    if history is None:
        return None
    return RwkvStoredStateCache(
        metadata=metadata,
        snapshot=None,
        history=history,
        pending_history=_rwkv_empty_history_suffix(history),
        state_store_path=store_path,
        state_store_generation=store_generation,
        state_store_segment_id=snapshot_segment_id,
    )


def _rwkv_effective_cached_history(
    cache_dir: Path,
    metadata: Mapping[str, object],
    snapshot_history: RwkvHistoricalReviewInputs,
) -> RwkvHistoricalReviewInputs | None:
    if (
        snapshot_history.last_review_id
        != (_int_value(metadata.get("snapshotReviewId")) or 0)
        or snapshot_history.history_hash != metadata.get("snapshotHistoryHash")
        or snapshot_history.replay_key != metadata.get("replayKey")
    ):
        return None
    history = _rwkv_history_after_delta_reviews(
        snapshot_history,
        _read_rwkv_delta_records(
            cache_dir / _RWKV_STATE_CACHE_DELTAS_FILE,
            after_review_id=snapshot_history.last_review_id,
            until_review_id=_int_value(metadata.get("lastReviewId")) or 0,
        ),
    )
    if (
        history.last_review_id != (_int_value(metadata.get("lastReviewId")) or 0)
        or history.review_count != (_int_value(metadata.get("reviewCount")) or 0)
        or history.history_hash != metadata.get("historyHash")
    ):
        return None
    return history


def _rwkv_empty_history_suffix(
    history: RwkvHistoricalReviewInputs,
) -> RwkvHistoricalReviewInputs:
    return replace(history, reviews=[], review_ids=[])


def _read_rwkv_state_cache_store_segment_history(
    path: Path,
    store_generation: str,
    segment_id: int,
) -> RwkvHistoricalReviewInputs:
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(path) as connection:
        schema_version = connection.execute("pragma user_version").fetchone()
        if schema_version != (_RWKV_STATE_CACHE_STORE_SCHEMA_VERSION,):
            raise ValueError("unsupported RWKV state-cache store schema")
        generation_row = connection.execute(
            "select value from store_metadata where key = 'generation'"
        ).fetchone()
        if generation_row != (store_generation,):
            raise ValueError("RWKV state-cache store generation mismatch")
        row = connection.execute(
            """
select
  last_review_id,
  review_count,
  history_hash,
  replay_key,
  previous_review_ids,
  previous_intervals,
  review_counts
from segments
where id = ?
""",
            (segment_id,),
        ).fetchone()
    if row is None:
        raise ValueError("missing RWKV state-cache segment")
    (
        last_review_id,
        review_count,
        history_hash,
        replay_key,
        previous_review_ids,
        previous_intervals,
        review_counts,
    ) = row
    if (
        not isinstance(last_review_id, int)
        or not isinstance(review_count, int)
        or not _rwkv_history_hash_is_valid(history_hash)
        or not isinstance(replay_key, str)
        or not replay_key
        or not all(
            isinstance(value, bytes)
            for value in (
                previous_review_ids,
                previous_intervals,
                review_counts,
            )
        )
    ):
        raise ValueError("invalid RWKV state-cache segment metadata")
    return RwkvHistoricalReviewInputs(
        reviews=[],
        review_ids=[],
        previous_review_id_by_card=_decode_int_map_binary(previous_review_ids),
        previous_interval_days_by_card=_decode_int_map_binary(previous_intervals),
        review_count_by_card=_decode_int_map_binary(review_counts),
        last_review_id=last_review_id,
        review_count=review_count,
        history_hash=cast(str, history_hash),
        replay_key=replay_key,
    )


def _rwkv_state_cache_store_segment_chain(
    path: Path,
    store_generation: str,
    segment_id: int,
) -> list[int]:
    if not path.is_file():
        raise FileNotFoundError(path)
    chain: list[int] = []
    seen: set[int] = set()
    with sqlite3.connect(path) as connection:
        schema_version = connection.execute("pragma user_version").fetchone()
        generation_row = connection.execute(
            "select value from store_metadata where key = 'generation'"
        ).fetchone()
        if schema_version != (
            _RWKV_STATE_CACHE_STORE_SCHEMA_VERSION,
        ) or generation_row != (store_generation,):
            raise ValueError("RWKV state-cache store identity mismatch")
        current_segment_id: int | None = segment_id
        while current_segment_id is not None:
            if current_segment_id in seen:
                raise ValueError("cyclic RWKV state-cache segment chain")
            seen.add(current_segment_id)
            row = connection.execute(
                "select parent_id from segments where id = ?",
                (current_segment_id,),
            ).fetchone()
            if row is None:
                raise ValueError("missing RWKV state-cache segment")
            parent_segment_id = row[0]
            if parent_segment_id is not None and not isinstance(
                parent_segment_id,
                int,
            ):
                raise ValueError("invalid RWKV state-cache parent segment")
            chain.append(current_segment_id)
            current_segment_id = parent_segment_id
    return chain


def _rwkv_state_cache_binary_location(
    reviewer: object,
) -> tuple[Path, dict[str, object]] | None:
    cache_dir = _rwkv_state_cache_dir(reviewer)
    if cache_dir is None:
        return None

    metadata = _read_rwkv_state_cache_metadata(reviewer)
    if metadata is None or metadata.get("version") != _RWKV_STATE_CACHE_VERSION:
        return None
    return cache_dir, metadata


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


def _rwkv_state_cache_ignored_review_ids(
    metadata: Mapping[str, object] | None,
) -> tuple[int, ...]:
    if metadata is None:
        return ()
    raw_review_ids = metadata.get(_RWKV_STATE_CACHE_IGNORED_REVIEW_IDS_KEY)
    if not isinstance(raw_review_ids, list):
        return ()
    return tuple(
        sorted(
            {
                review_id
                for review_id in raw_review_ids
                if isinstance(review_id, int)
                and not isinstance(review_id, bool)
                and review_id > 0
            }
        )
    )


def _rwkv_state_cache_metadata(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    snapshot_review_id: int,
    snapshot_history_hash: str | None = None,
    checkpoints: Sequence[RwkvStoredStateCheckpoint] = (),
    checkpoint_entries: Sequence[_RwkvStateCacheCheckpointEntry] = (),
    base_metadata: Mapping[str, object] | None = None,
    state_store_generation: str | None = None,
    snapshot_segment_id: int | None = None,
) -> dict[str, object]:
    if checkpoints and checkpoint_entries:
        raise ValueError("RWKV checkpoint snapshots and entries are mutually exclusive")
    if not _rwkv_history_hash_is_valid(history.history_hash):
        raise ValueError("missing RWKV history identity")
    if not history.replay_key:
        raise ValueError("missing RWKV replay semantics identity")
    if snapshot_history_hash is None:
        if snapshot_review_id != history.last_review_id:
            raise ValueError("missing RWKV snapshot prefix identity")
        snapshot_history_hash = history.history_hash
    if not _rwkv_history_hash_is_valid(snapshot_history_hash):
        raise ValueError("invalid RWKV snapshot prefix identity")
    entries: list[_RwkvStateCacheCheckpointEntry]
    if checkpoints:
        entries = [
            _RwkvStateCacheCheckpointEntry(
                lastReviewId=checkpoint.history.last_review_id,
                reviewCount=checkpoint.history.review_count,
                historyHash=checkpoint.history.history_hash,
            )
            for checkpoint in checkpoints
        ]
    else:
        entries = list(checkpoint_entries)
    metadata = dict(
        base_metadata
        if base_metadata is not None
        else _rwkv_state_cache_metadata_base(reviewer)
    )
    metadata.update(
        {
            "snapshotReviewId": snapshot_review_id,
            "snapshotHistoryHash": snapshot_history_hash,
            "lastReviewId": history.last_review_id,
            "reviewCount": history.review_count,
            "historyHash": history.history_hash,
            "replayKey": history.replay_key,
        }
    )
    if state_store_generation is not None or snapshot_segment_id is not None:
        if not state_store_generation or snapshot_segment_id is None:
            raise ValueError("incomplete RWKV state-cache store identity")
        metadata.update(
            {
                "storage": _RWKV_STATE_CACHE_STORE_KIND,
                "storeGeneration": state_store_generation,
                "snapshotSegmentId": snapshot_segment_id,
            }
        )
    if entries:
        metadata["checkpoints"] = entries
    if history.ignored_review_ids:
        metadata[_RWKV_STATE_CACHE_IGNORED_REVIEW_IDS_KEY] = list(
            history.ignored_review_ids
        )
    else:
        metadata.pop(_RWKV_STATE_CACHE_IGNORED_REVIEW_IDS_KEY, None)
    return metadata


def _rwkv_state_cache_metadata_base(
    reviewer: object,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "version": _RWKV_STATE_CACHE_VERSION,
        "presetReplaySemantics": _RWKV_PRESET_REPLAY_SEMANTICS_VERSION,
        "collection": _rwkv_collection_cache_key(reviewer),
        "model": _rwkv_model_cache_key(),
        "dynamicPresetReplay": _rwkv_dynamic_preset_replay_enabled_for_collection(
            reviewer
        ),
    }
    if (collection_mod := _rwkv_collection_modified(reviewer)) is not None:
        metadata[_RWKV_STATE_CACHE_COLLECTION_MOD_KEY] = collection_mod
    return metadata


def _refresh_rwkv_state_cache_collection_mod(
    reviewer: object,
    history: RwkvHistoricalReviewInputs | RwkvResidentStateIdentity,
) -> None:
    """Record the collection marker after confirming the cache history identity."""

    try:
        collection_mod = _rwkv_collection_modified(reviewer)
        if collection_mod is None:
            return
        metadata = _read_rwkv_state_cache_metadata(reviewer)
        if (
            metadata is None
            or metadata.get("version") != _RWKV_STATE_CACHE_VERSION
            or (_int_value(metadata.get("lastReviewId")) or 0) != history.last_review_id
            or (_int_value(metadata.get("reviewCount")) or 0) != history.review_count
            or metadata.get("historyHash") != history.history_hash
            or metadata.get("replayKey") != history.replay_key
            or metadata.get(_RWKV_STATE_CACHE_COLLECTION_MOD_KEY) == collection_mod
        ):
            return
        cache_dir = _rwkv_state_cache_dir(reviewer)
        if cache_dir is None:
            return
        updated = {
            **metadata,
            _RWKV_STATE_CACHE_COLLECTION_MOD_KEY: collection_mod,
        }
        _atomic_write(
            cache_dir / _RWKV_STATE_CACHE_META_FILE,
            json.dumps(updated, separators=(",", ":"), sort_keys=True).encode("utf8"),
        )
        logger.debug("updated RWKV state cache collection marker")
    except Exception:
        logger.warning(
            "failed to update RWKV state cache collection marker",
            exc_info=True,
        )


def _rwkv_state_cache_checkpoint_entries(
    metadata: dict[str, object] | None,
) -> list[_RwkvStateCacheCheckpointEntry]:
    if not isinstance(metadata, dict):
        return []
    raw_entries = metadata.get("checkpoints")
    if not isinstance(raw_entries, list):
        return []

    entries: dict[int, _RwkvStateCacheCheckpointEntry] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            continue
        last_review_id = _int_value(raw_entry.get("lastReviewId"))
        review_count = _int_value(raw_entry.get("reviewCount"))
        history_hash = raw_entry.get("historyHash")
        segment_id = _int_value(raw_entry.get("segmentId"))
        if (
            last_review_id is None
            or last_review_id <= 0
            or review_count is None
            or review_count <= 0
            or not _rwkv_history_hash_is_valid(history_hash)
        ):
            continue
        entry: _RwkvStateCacheCheckpointEntry = {
            "lastReviewId": last_review_id,
            "reviewCount": review_count,
            "historyHash": history_hash,
        }
        if segment_id is not None and segment_id > 0:
            entry["segmentId"] = segment_id
        entries[last_review_id] = entry
    return [entries[review_id] for review_id in sorted(entries)]


def _rwkv_state_cache_checkpoint_path(
    cache_dir: Path,
    last_review_id: int,
) -> Path:
    return cache_dir / (
        f"{_RWKV_STATE_CACHE_CHECKPOINT_PREFIX}{last_review_id}"
        f"{_RWKV_STATE_CACHE_CHECKPOINT_SUFFIX}"
    )


def _rwkv_state_cache_metadata_compatible(
    reviewer: object,
    metadata: dict[str, object],
    *,
    dynamic_preset_replay_enabled: bool | None = None,
    replay_key: str | None = None,
) -> bool:
    if metadata.get("version") not in (
        _RWKV_STATE_CACHE_VERSION,
        _RWKV_STATE_CACHE_LEGACY_JSON_VERSION,
    ):
        return False
    if metadata.get("presetReplaySemantics") != _RWKV_PRESET_REPLAY_SEMANTICS_VERSION:
        return False
    if metadata.get("collection") != _rwkv_collection_cache_key(reviewer):
        return False
    if not _rwkv_state_cache_model_usable(metadata.get("model")):
        return False
    if dynamic_preset_replay_enabled is None:
        dynamic_preset_replay_enabled = (
            _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
        )
    if (
        metadata.get("version") == _RWKV_STATE_CACHE_VERSION
        and metadata.get("dynamicPresetReplay") != dynamic_preset_replay_enabled
    ):
        return False
    if metadata.get("version") == _RWKV_STATE_CACHE_VERSION:
        if metadata.get("storage") not in (None, _RWKV_STATE_CACHE_STORE_KIND):
            return False
        if (
            not _rwkv_history_hash_is_valid(metadata.get("historyHash"))
            or not _rwkv_history_hash_is_valid(metadata.get("snapshotHistoryHash"))
            or not isinstance(metadata.get("replayKey"), str)
            or not metadata.get("replayKey")
        ):
            return False
        if replay_key is not None and metadata.get("replayKey") != replay_key:
            return False
        if metadata.get("storage") == _RWKV_STATE_CACHE_STORE_KIND and (
            not isinstance(metadata.get("storeGeneration"), str)
            or not metadata.get("storeGeneration")
            or (_int_value(metadata.get("snapshotSegmentId")) or 0) <= 0
        ):
            return False
    return True


def _rwkv_state_cache_metadata_usable(
    reviewer: object,
    metadata: dict[str, object],
    *,
    dynamic_preset_replay_enabled: bool | None = None,
    current_history: RwkvHistoricalReviewInputs | None = None,
    current_prefix_identities: Mapping[
        int,
        _RwkvHistoryPrefixIdentity,
    ]
    | None = None,
) -> bool:
    if current_history is None:
        try:
            current_history = _historical_rwkv_review_inputs(reviewer)
        except Exception:
            logger.debug(
                "failed to build current history for RWKV cache validation",
                exc_info=True,
            )
            return False
    if not current_history.ignored_review_ids and (
        ignored_review_ids := _rwkv_state_cache_ignored_review_ids(metadata)
    ):
        try:
            current_history = _historical_rwkv_review_inputs(
                reviewer,
                ignored_review_ids=frozenset(ignored_review_ids),
            )
        except Exception:
            return False
    if not _rwkv_state_cache_metadata_compatible(
        reviewer,
        metadata,
        dynamic_preset_replay_enabled=dynamic_preset_replay_enabled,
        replay_key=current_history.replay_key,
    ):
        return False
    last_review_id = _int_value(metadata.get("lastReviewId"))
    review_count = _int_value(metadata.get("reviewCount"))
    history_hash = metadata.get("historyHash")
    if (
        last_review_id is None
        or review_count is None
        or not _rwkv_history_hash_is_valid(history_hash)
    ):
        return False

    identity = (
        current_prefix_identities.get(last_review_id)
        if current_prefix_identities is not None
        else _rwkv_history_prefix_identity(current_history, last_review_id)
    )
    return identity == _RwkvHistoryPrefixIdentity(
        last_review_id=last_review_id,
        review_count=review_count,
        history_hash=cast(str, history_hash),
    )


def _rwkv_state_cache_history_prefix_matches(
    reviewer: object,
    history: RwkvHistoricalReviewInputs,
    *,
    current_history: RwkvHistoricalReviewInputs | None = None,
    current_prefix_identities: Mapping[
        int,
        _RwkvHistoryPrefixIdentity,
    ]
    | None = None,
) -> bool:
    if not _rwkv_history_hash_is_valid(history.history_hash) or not history.replay_key:
        return False
    if current_history is None:
        try:
            current_history = _historical_rwkv_review_inputs(reviewer)
        except Exception:
            logger.debug(
                "failed to build current history for RWKV prefix validation",
                exc_info=True,
            )
            return False
    if current_history.ignored_review_ids != history.ignored_review_ids:
        try:
            current_history = _historical_rwkv_review_inputs(
                reviewer,
                ignored_review_ids=frozenset(history.ignored_review_ids),
            )
        except Exception:
            return False
    if history.replay_key != current_history.replay_key:
        return False

    identity = (
        current_prefix_identities.get(history.last_review_id)
        if current_prefix_identities is not None
        else _rwkv_history_prefix_identity(
            current_history,
            history.last_review_id,
        )
    )
    return identity == _RwkvHistoryPrefixIdentity(
        last_review_id=history.last_review_id,
        review_count=history.review_count,
        history_hash=history.history_hash,
    )


def _rwkv_history_prefix_identity(
    history: RwkvHistoricalReviewInputs,
    last_review_id: int,
) -> _RwkvHistoryPrefixIdentity:
    return _rwkv_history_prefix_identities(history, [last_review_id])[last_review_id]


def _rwkv_history_prefix_identities(
    history: RwkvHistoricalReviewInputs,
    last_review_ids: Sequence[int],
) -> dict[int, _RwkvHistoryPrefixIdentity]:
    requested_ids = sorted(set(last_review_ids))
    if not requested_ids:
        return {}

    results: dict[int, _RwkvHistoryPrefixIdentity] = {}
    if history.last_review_id in requested_ids:
        results[history.last_review_id] = _RwkvHistoryPrefixIdentity(
            last_review_id=history.last_review_id,
            review_count=history.review_count,
            history_hash=history.history_hash,
        )
        requested_ids.remove(history.last_review_id)
    if not requested_ids:
        return results

    history_hash = _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    review_count = 0
    actual_last_review_id = 0
    next_requested_index = 0
    for review_id, review in zip(history.review_ids, history.reviews, strict=True):
        while (
            next_requested_index < len(requested_ids)
            and requested_ids[next_requested_index] < review_id
        ):
            results[requested_ids[next_requested_index]] = _RwkvHistoryPrefixIdentity(
                last_review_id=actual_last_review_id,
                review_count=review_count,
                history_hash=history_hash,
            )
            next_requested_index += 1
        if next_requested_index == len(requested_ids):
            break
        history_hash = _rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )
        review_count += 1
        actual_last_review_id = review_id

    while next_requested_index < len(requested_ids):
        results[requested_ids[next_requested_index]] = _RwkvHistoryPrefixIdentity(
            last_review_id=actual_last_review_id,
            review_count=review_count,
            history_hash=history_hash,
        )
        next_requested_index += 1

    return results


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


def _rwkv_collection_modified(reviewer: object) -> int | None:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    try:
        value = scalar("select mod from col") if callable(scalar) else None
    except Exception:
        logger.debug("failed to read RWKV collection modification marker")
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rwkv_state_cache_collection_unchanged(
    reviewer: object,
    metadata: Mapping[str, object],
) -> bool:
    cached_mod = _int_value(metadata.get(_RWKV_STATE_CACHE_COLLECTION_MOD_KEY))
    if cached_mod is None or cached_mod != _rwkv_collection_modified(reviewer):
        return False
    replay_key = metadata.get("replayKey")
    try:
        current_replay_key = _rwkv_replay_semantics_key(
            reviewer,
            first_review_elapsed_source=RwkvFirstReviewElapsedSource.DECK_CONFIG,
        )
    except Exception:
        logger.debug("failed to build RWKV replay key for collection marker")
        return False
    return isinstance(replay_key, str) and replay_key == current_replay_key


def _rwkv_model_cache_key() -> dict[str, object] | None:
    global _rwkv_model_cache_signature
    global _rwkv_model_cache_value

    model_path = _current_embedded_rwkv_model_path()
    if model_path is None:
        return None

    source = "custom" if os.environ.get("ANKI_RWKV_MODEL_PATH") else "embedded"
    with _rwkv_model_cache_lock:
        for _attempt in range(2):
            try:
                stat = model_path.stat()
            except OSError:
                return None
            signature = (
                source,
                str(model_path),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            if (
                signature == _rwkv_model_cache_signature
                and _rwkv_model_cache_value is not None
            ):
                return dict(_rwkv_model_cache_value)

            try:
                digest = _sha256_file(model_path)
                final_stat = model_path.stat()
            except OSError:
                return None
            final_signature = (
                source,
                str(model_path),
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mtime_ns,
                final_stat.st_ctime_ns,
            )
            if final_signature != signature:
                continue

            value: dict[str, object] = {
                "source": source,
                "name": model_path.name,
                "size": stat.st_size,
                "sha256": digest,
            }
            _rwkv_model_cache_signature = signature
            _rwkv_model_cache_value = value
            return dict(value)

    return None


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


def _atomic_write_stream(
    path: Path,
    write: Callable[[_RwkvBinaryStream], None],
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary_path = Path(file.name)
            write(file)
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, data: bytes) -> None:
    def write(file: _RwkvBinaryStream) -> None:
        file.write(data)

    _atomic_write_stream(path, write)


def _remove_rwkv_state_cache_store_files(path: Path) -> None:
    for candidate in (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ):
        candidate.unlink(missing_ok=True)


class _RwkvBinaryReader:
    def __init__(self, data: bytes | mmap.mmap) -> None:
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

    def skip(self, size: int) -> None:
        if size < 0 or self._offset + size > len(self._data):
            raise ValueError("truncated RWKV cache binary data")
        self._offset += size

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.bytes(8))[0]

    def expect_end(self) -> None:
        if self.remaining():
            raise ValueError("trailing RWKV cache binary data")


class _RwkvBinaryStream(Protocol):
    def write(self, data: bytes, /) -> int: ...


_RwkvBinaryOutput = bytearray | _RwkvBinaryStream


def _write_raw(out: _RwkvBinaryOutput, value: bytes) -> None:
    if isinstance(out, bytearray):
        out.extend(value)
        return
    written = out.write(value)
    if written != len(value):
        raise OSError(f"short RWKV cache write: {written} of {len(value)} bytes")


def _write_u8(out: _RwkvBinaryOutput, value: int) -> None:
    _write_raw(out, bytes((value & 0xFF,)))


def _write_u32(out: _RwkvBinaryOutput, value: int) -> None:
    _write_raw(out, struct.pack("<I", value))


def _write_i64(out: _RwkvBinaryOutput, value: int) -> None:
    _write_raw(out, struct.pack("<q", value))


def _write_bytes(out: _RwkvBinaryOutput, value: bytes) -> None:
    _write_u32(out, len(value))
    _write_raw(out, value)


def _read_bytes(reader: _RwkvBinaryReader) -> bytes:
    return reader.bytes(reader.u32())


def _write_optional_bytes(out: _RwkvBinaryOutput, value: bytes | None) -> None:
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


def _write_optional_i64(out: _RwkvBinaryOutput, value: int | None) -> None:
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


def _write_optional_string(out: _RwkvBinaryOutput, value: str | None) -> None:
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


def _write_json(out: _RwkvBinaryOutput, value: dict[str, object]) -> None:
    _write_bytes(
        out,
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf8"),
    )


def _read_json(reader: _RwkvBinaryReader) -> dict[str, object]:
    value = json.loads(_read_bytes(reader).decode("utf8"))
    if not isinstance(value, dict):
        raise ValueError("invalid RWKV cache JSON payload")
    return value


def _write_state_map(
    out: _RwkvBinaryOutput,
    states: dict[int, bytes],
) -> None:
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


def _skip_state_map(reader: _RwkvBinaryReader) -> None:
    for _ in range(reader.u32()):
        reader.i64()
        reader.skip(reader.u32())


def _write_int_map(out: _RwkvBinaryOutput, values: dict[int, int]) -> None:
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


def _skip_int_map_binary(reader: _RwkvBinaryReader) -> None:
    reader.skip(reader.u32() * 16)


def _encode_int_map_binary(values: dict[int, int]) -> bytes:
    out = bytearray()
    _write_int_map(out, values)
    return bytes(out)


def _decode_int_map_binary(data: bytes) -> dict[int, int]:
    reader = _RwkvBinaryReader(data)
    values = _read_int_map_binary(reader)
    reader.expect_end()
    return values


def _encode_rwkv_state_cache_history_maps(
    history: RwkvHistoricalReviewInputs,
) -> tuple[bytes, bytes, bytes]:
    return (
        _encode_int_map_binary(history.previous_review_id_by_card),
        _encode_int_map_binary(history.previous_interval_days_by_card),
        _encode_int_map_binary(history.review_count_by_card),
    )


def _write_cache_snapshot_binary(
    out: _RwkvBinaryOutput,
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


def _skip_optional_bytes(reader: _RwkvBinaryReader) -> None:
    marker = reader.u8()
    if marker == 0:
        return
    if marker != 1:
        raise ValueError("invalid optional bytes marker")
    reader.skip(reader.u32())


def _skip_cache_snapshot_binary(reader: _RwkvBinaryReader) -> None:
    _skip_state_map(reader)
    _skip_state_map(reader)
    _skip_state_map(reader)
    _skip_state_map(reader)
    _skip_optional_bytes(reader)
    _skip_optional_bytes(reader)


def _encode_rwkv_state_cache_snapshot_file(
    *,
    metadata: dict[str, object],
    snapshot: RwkvBackendCacheSnapshot,
    history: RwkvHistoricalReviewInputs,
) -> bytes:
    out = bytearray()
    _write_rwkv_state_cache_snapshot_file(
        out,
        metadata=metadata,
        snapshot=snapshot,
        history=history,
    )
    return bytes(out)


def _write_rwkv_state_cache_snapshot_file(
    out: _RwkvBinaryOutput,
    *,
    metadata: dict[str, object],
    snapshot: RwkvBackendCacheSnapshot,
    history: RwkvHistoricalReviewInputs,
) -> None:
    _write_raw(out, _RWKV_STATE_CACHE_SNAPSHOT_MAGIC)
    _write_json(out, metadata)
    _write_cache_snapshot_binary(out, snapshot)
    _write_int_map(out, history.previous_review_id_by_card)
    _write_int_map(out, history.previous_interval_days_by_card)
    _write_int_map(out, history.review_count_by_card)


def _atomic_write_rwkv_state_cache_snapshot(
    path: Path,
    *,
    metadata: dict[str, object],
    snapshot: RwkvBackendCacheSnapshot,
    history: RwkvHistoricalReviewInputs,
) -> None:
    def write(file: _RwkvBinaryStream) -> None:
        _write_rwkv_state_cache_snapshot_file(
            file,
            metadata=metadata,
            snapshot=snapshot,
            history=history,
        )

    _atomic_write_stream(path, write)


def _atomic_write_rwkv_state_cache_snapshot_from_runtime(
    path: Path,
    *,
    metadata: dict[str, object],
    append_snapshot: Callable[[Path], None],
    history: RwkvHistoricalReviewInputs,
) -> None:
    def write(file: _RwkvBinaryStream) -> None:
        _write_raw(file, _RWKV_STATE_CACHE_SNAPSHOT_MAGIC)
        _write_json(file, metadata)
        concrete_file = cast(Any, file)
        concrete_file.flush()
        append_snapshot(Path(concrete_file.name))
        concrete_file.seek(0, os.SEEK_END)
        _write_int_map(file, history.previous_review_id_by_card)
        _write_int_map(file, history.previous_interval_days_by_card)
        _write_int_map(file, history.review_count_by_card)

    _atomic_write_stream(path, write)


def _decode_rwkv_state_cache_snapshot_file(
    data: bytes | mmap.mmap,
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
        history_hash=str(
            metadata.get("historyHash", _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH)
        ),
        replay_key=str(metadata.get("replayKey", "")),
    )
    return metadata, snapshot, history


@contextmanager
def _mmap_rwkv_state_cache_file(path: Path) -> Iterator[mmap.mmap]:
    with path.open("rb") as file:
        with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def _read_rwkv_state_cache_snapshot_file(
    path: Path,
) -> tuple[dict[str, object], RwkvBackendCacheSnapshot, RwkvHistoricalReviewInputs]:
    with _mmap_rwkv_state_cache_file(path) as data:
        return _decode_rwkv_state_cache_snapshot_file(data)


def _validate_rwkv_state_cache_snapshot_file(path: Path) -> dict[str, object]:
    with _mmap_rwkv_state_cache_file(path) as data:
        reader = _RwkvBinaryReader(data)
        if (
            reader.bytes(len(_RWKV_STATE_CACHE_SNAPSHOT_MAGIC))
            != _RWKV_STATE_CACHE_SNAPSHOT_MAGIC
        ):
            raise ValueError("invalid RWKV state cache snapshot header")
        metadata = _read_json(reader)
        _skip_cache_snapshot_binary(reader)
        _skip_int_map_binary(reader)
        _skip_int_map_binary(reader)
        _skip_int_map_binary(reader)
        reader.expect_end()
        return metadata


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


def _rwkv_history_hash_is_valid(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _rwkv_history_hash_after_review(
    previous_hash: str,
    review_id: int,
    review_input: RwkvReviewInput,
) -> str:
    if not _rwkv_history_hash_is_valid(previous_hash):
        raise ValueError("invalid RWKV history hash")

    digest = hashlib.sha256()
    digest.update(_RWKV_STATE_CACHE_HISTORY_HASH_DOMAIN)
    digest.update(bytes.fromhex(previous_hash))
    digest.update(_encode_rwkv_delta_record(review_id, review_input))
    return digest.hexdigest()


def _write_rwkv_delta_record_frame(
    out: bytearray,
    review_id: int,
    review_input: RwkvReviewInput,
) -> None:
    payload = _encode_rwkv_delta_record(review_id, review_input)
    _write_u32(out, len(payload))
    out.extend(payload)
    _write_u32(out, zlib.crc32(payload) & 0xFFFFFFFF)


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
        pending = bytearray()
        if needs_header:
            pending.extend(_RWKV_STATE_CACHE_DELTAS_MAGIC)
        for review_id, review in zip(review_ids, reviews):
            _write_rwkv_delta_record_frame(pending, review_id, review)
            if len(pending) >= _RWKV_STATE_CACHE_DELTA_WRITE_BUFFER_SIZE:
                file.write(pending)
                pending.clear()
        if pending:
            file.write(pending)
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
    history_hash = base.history_hash

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
        history_hash = _rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review,
        )

    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=last_review_id,
        review_count=review_count,
        deck_id=base.deck_id,
        history_hash=history_hash,
        replay_key=base.replay_key,
        ignored_review_ids=base.ignored_review_ids,
    )


def _rwkv_state_cache_metadata_matches_manifest(
    snapshot_metadata: dict[str, object],
    manifest_metadata: dict[str, object],
) -> bool:
    snapshot_review_id = _int_value(manifest_metadata.get("snapshotReviewId"))
    return (
        _rwkv_state_cache_snapshot_metadata_matches_manifest(
            snapshot_metadata,
            manifest_metadata,
        )
        and _int_value(snapshot_metadata.get("lastReviewId")) == snapshot_review_id
        and snapshot_metadata.get("historyHash")
        == manifest_metadata.get("snapshotHistoryHash")
    )


def _rwkv_state_cache_checkpoint_metadata_matches_manifest(
    checkpoint_metadata: dict[str, object],
    manifest_metadata: dict[str, object],
    checkpoint_entry: _RwkvStateCacheCheckpointEntry,
) -> bool:
    return (
        _rwkv_state_cache_snapshot_metadata_matches_manifest(
            checkpoint_metadata,
            manifest_metadata,
        )
        and _int_value(checkpoint_metadata.get("lastReviewId"))
        == checkpoint_entry["lastReviewId"]
        and _int_value(checkpoint_metadata.get("reviewCount"))
        == checkpoint_entry["reviewCount"]
        and checkpoint_metadata.get("historyHash") == checkpoint_entry["historyHash"]
    )


def _rwkv_state_cache_snapshot_metadata_matches_manifest(
    snapshot_metadata: dict[str, object],
    manifest_metadata: dict[str, object],
) -> bool:
    return (
        snapshot_metadata.get("version") == _RWKV_STATE_CACHE_VERSION
        and snapshot_metadata.get("presetReplaySemantics")
        == manifest_metadata.get("presetReplaySemantics")
        and snapshot_metadata.get("collection") == manifest_metadata.get("collection")
        and snapshot_metadata.get("model") == manifest_metadata.get("model")
        and snapshot_metadata.get("dynamicPresetReplay")
        == manifest_metadata.get("dynamicPresetReplay")
        and snapshot_metadata.get("replayKey") == manifest_metadata.get("replayKey")
    )


def _replay_rwkv_cache_reviews(
    backend: object,
    warm_up: object,
    reviews: Sequence[RwkvReviewInput],
    *,
    progress: RwkvStateCacheProgressCallback | None,
    label: str,
    is_current: Callable[[], bool],
) -> None:
    if isinstance(backend, RwkvStatefulReviewerBackend):
        started_at = time.monotonic()

        def progress_reporter(replay_progress: RwkvWarmUpProgress) -> None:
            _require_reviewer_backend_warmup_current(is_current)
            _report_rwkv_review_replay_progress(
                progress,
                label=label,
                replay_progress=replay_progress,
                elapsed_seconds=time.monotonic() - started_at,
            )

        backend.warm_up(
            reviews,
            progress=progress_reporter,
        )
        return

    if callable(warm_up):
        _require_reviewer_backend_warmup_current(is_current)
        warm_up(reviews)


def _historical_rwkv_review_inputs(
    reviewer: object,
    *,
    after_review_id: int | None = None,
    deck_id: int | None = None,
    progress: RwkvStateCacheProgressCallback | None = None,
    previous_review_id_by_card: dict[int, int] | None = None,
    previous_interval_days_by_card: dict[int, int] | None = None,
    review_count_by_card: dict[int, int] | None = None,
    previous_history_hash: str | None = None,
    previous_replay_key: str | None = None,
    first_review_elapsed_source: RwkvFirstReviewElapsedSource = RwkvFirstReviewElapsedSource.DECK_CONFIG,
    ignored_review_ids: AbstractSet[int] = frozenset(),
    prepare_recovery_checkpoint: bool = False,
) -> RwkvHistoricalReviewInputs:
    start = time.monotonic()
    requested_deck_id = deck_id
    previous_ids = dict(previous_review_id_by_card or {})
    previous_intervals = dict(previous_interval_days_by_card or {})
    review_counts = dict(review_count_by_card or {})
    replay_key = _rwkv_replay_semantics_key(
        reviewer,
        first_review_elapsed_source=first_review_elapsed_source,
    )
    if previous_replay_key and previous_replay_key != replay_key:
        raise ValueError("RWKV replay semantics changed during incremental replay")
    if previous_history_hash is not None and not _rwkv_history_hash_is_valid(
        previous_history_hash
    ):
        raise ValueError("invalid previous RWKV history identity")
    history_hash = (
        previous_history_hash
        if previous_history_hash is not None
        else _RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    )

    have_previous_state = all(
        value is not None
        for value in (
            previous_review_id_by_card,
            previous_interval_days_by_card,
            review_count_by_card,
        )
    )
    if after_review_id is not None and have_previous_state:
        incremental_rows = _historical_rwkv_review_rows(
            reviewer,
            after_review_id=after_review_id,
            deck_id=deck_id,
            limit=1,
        )
        if not incremental_rows:
            review_count = sum(review_counts.values())
            logger.debug(
                "RWKV historical review inputs unchanged: "
                "last_review_id=%s review_count=%s deck_id=%s elapsed_ms=%.1f",
                after_review_id,
                review_count,
                requested_deck_id,
                (time.monotonic() - start) * 1000,
            )
            return RwkvHistoricalReviewInputs(
                reviews=[],
                review_ids=[],
                previous_review_id_by_card=previous_ids,
                previous_interval_days_by_card=previous_intervals,
                review_count_by_card=review_counts,
                last_review_id=after_review_id,
                review_count=review_count,
                deck_id=requested_deck_id,
                history_hash=history_hash,
                replay_key=replay_key,
            )

    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not isinstance(days_elapsed, int) or not isinstance(next_day_at, int):
        return RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            previous_review_id_by_card=previous_ids,
            previous_interval_days_by_card=previous_intervals,
            review_count_by_card=review_counts,
            last_review_id=after_review_id or 0,
            review_count=sum(review_counts.values()),
            deck_id=requested_deck_id,
            history_hash=history_hash,
            replay_key=replay_key,
        )

    rows_start = time.monotonic()
    raw_rows = list(
        _historical_rwkv_review_rows(
            reviewer,
            deck_id=deck_id,
        )
    )
    active_ignored_review_ids = tuple(
        sorted(
            {
                review_id
                for row in raw_rows
                if row
                and isinstance((review_id := row[0]), int)
                and review_id in ignored_review_ids
            }
        )
    )
    if active_ignored_review_ids:
        active_ignored_review_id_set = set(active_ignored_review_ids)
        raw_rows = [
            row
            for row in raw_rows
            if not (
                row
                and isinstance(row[0], int)
                and row[0] in active_ignored_review_id_set
            )
        ]
    retained_start_by_card = _benchmark_retained_historical_review_starts(raw_rows)
    recovery_cutoff_review_id: int | None = None
    if prepare_recovery_checkpoint:
        for raw_row_index in range(len(raw_rows) - 1, -1, -1):
            row = raw_rows[raw_row_index]
            if (
                _benchmark_retained_historical_review_state(
                    raw_row_index,
                    row,
                    retained_start_by_card,
                )
                is None
                or len(row) < 9
                or not isinstance(row[0], int)
                or (after_review_id is not None and row[0] <= after_review_id)
            ):
                continue
            recovery_cutoff_review_id = (
                row[0] - _RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS
            )
            break

    def retained_rows() -> Iterator[tuple[int, Sequence[object], int]]:
        for index, row in enumerate(raw_rows):
            historical_state = _benchmark_retained_historical_review_state(
                index,
                row,
                retained_start_by_card,
            )
            if historical_state is None:
                continue
            if after_review_id is not None and (
                not isinstance(row[0], int) or row[0] <= after_review_id
            ):
                continue
            yield index, row, historical_state

    row_count = sum(1 for _ in retained_rows())
    rows_elapsed_ms = (time.monotonic() - rows_start) * 1000
    dynamic_preset_replay = _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
    historical_preset_rules_start = time.monotonic()
    historical_preset_rules = (
        _historical_preset_rules(reviewer) if dynamic_preset_replay else []
    )
    historical_preset_rules_elapsed_ms = (
        time.monotonic() - historical_preset_rules_start
    ) * 1000
    deck_config_start = time.monotonic()
    deck_configs_by_deck_id = _historical_deck_configs_by_deck_id(
        reviewer,
        (row for _index, row, _state in retained_rows()),
    )
    deck_config_elapsed_ms = (time.monotonic() - deck_config_start) * 1000
    preset_start = time.monotonic()
    preset_id_by_card = _historical_deck_config_ids_by_card(
        reviewer,
        (row for _index, row, _state in retained_rows()),
        deck_configs_by_deck_id=deck_configs_by_deck_id,
    )
    preset_id_by_card.update(
        _resolved_fsrs_preset_ids(
            reviewer,
            _historical_rwkv_review_card_ids(
                row for _index, row, _state in retained_rows()
            ),
        )
    )
    preset_elapsed_ms = (time.monotonic() - preset_start) * 1000
    reviews: list[RwkvReviewInput] = []
    review_ids: list[int] = []
    last_review_id = after_review_id or 0
    retained_review_count = 0
    review_count = 0
    historical_preset_rule_matches = 0
    prepared_checkpoint_histories: dict[int, RwkvHistoricalReviewInputs] = {}
    prepare_started_at = time.monotonic()
    prepare_report_every = _rwkv_warmup_progress_interval(row_count)
    _report_rwkv_review_input_prepare_progress(
        progress,
        processed=0,
        total=row_count,
        started_at=prepare_started_at,
    )

    prepared_row_count = 0
    for raw_row_index, row in enumerate(raw_rows):
        historical_state = _benchmark_retained_historical_review_state(
            raw_row_index,
            row,
            retained_start_by_card,
        )
        if historical_state is None:
            raw_rows[raw_row_index] = ()
            continue
        review_id_value = row[0] if row else None
        if isinstance(review_id_value, int):
            retained_review_count += 1
            if review_id_value <= last_review_id:
                review_count = retained_review_count
        if after_review_id is not None and (
            not isinstance(review_id_value, int) or review_id_value <= after_review_id
        ):
            raw_rows[raw_row_index] = ()
            continue
        prepared_row_count += 1
        raw_rows[raw_row_index] = ()
        if len(row) < 9:
            continue
        (
            review_id,
            card_id,
            note_id,
            row_deck_id,
            ease,
            duration_millis,
            review_kind,
            interval_days,
            ease_factor,
        ) = row[:9]
        if not (
            isinstance(review_id, int)
            and isinstance(card_id, int)
            and isinstance(note_id, int)
            and isinstance(row_deck_id, int)
            and isinstance(ease, int)
            and isinstance(duration_millis, int)
            and isinstance(review_kind, int)
            and isinstance(interval_days, int)
            and isinstance(ease_factor, int)
        ):
            continue

        if (
            recovery_cutoff_review_id is not None
            and review_id > recovery_cutoff_review_id
            and reviews
            and not prepared_checkpoint_histories
        ):
            checkpoint_review_count = len(reviews)
            prepared_checkpoint_histories[checkpoint_review_count] = (
                RwkvHistoricalReviewInputs(
                    reviews=[],
                    review_ids=[],
                    previous_review_id_by_card=dict(previous_ids),
                    previous_interval_days_by_card=dict(previous_intervals),
                    review_count_by_card=dict(review_counts),
                    last_review_id=review_ids[-1],
                    review_count=checkpoint_review_count,
                    deck_id=requested_deck_id,
                    history_hash=history_hash,
                    replay_key=replay_key,
                    ignored_review_ids=active_ignored_review_ids,
                )
            )

        day_offset = _historical_review_day_offset(
            review_id,
            days_elapsed=days_elapsed,
            next_day_at=next_day_at,
        )
        previous_review_id = previous_ids.get(card_id)
        if previous_review_id is not None:
            elapsed_seconds = max(0, (review_id - previous_review_id) // 1000)
            elapsed_days = max(
                0,
                day_offset
                - _historical_review_day_offset(
                    previous_review_id,
                    days_elapsed=days_elapsed,
                    next_day_at=next_day_at,
                ),
            )
        else:
            deck_config = deck_configs_by_deck_id.get(row_deck_id)
            elapsed_source = first_review_elapsed_source
            if elapsed_source == RwkvFirstReviewElapsedSource.DECK_CONFIG:
                elapsed_source = (
                    RwkvFirstReviewElapsedSource.CARD_CREATION
                    if isinstance(deck_config, dict)
                    and _rwkv_review_first_review_elapsed_from_card_creation(
                        deck_config
                    )
                    else RwkvFirstReviewElapsedSource.MISSING
                )
            elapsed_seconds = (
                max(0, (review_id - card_id) // 1000)
                if elapsed_source == RwkvFirstReviewElapsedSource.CARD_CREATION
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
            base_preset_id: int | str | None = historical_preset_id
            historical_preset_rule_matches += 1
        else:
            base_preset_id = preset_id_by_card[card_id]
        preset_id = (
            _stable_preset_id(str(base_preset_id))
            if base_preset_id is not None
            else None
        )
        previous_ids[card_id] = review_id
        previous_intervals[card_id] = interval_days
        review_counts[card_id] = review_count_so_far + 1
        last_review_id = max(last_review_id, review_id)
        review_count = retained_review_count

        state_kind, normal_state_kind = _historical_review_state_kinds(review_kind)
        review_input = RwkvReviewInput(
            identity=RwkvReviewIdentity(
                card_id=card_id,
                note_id=note_id,
                deck_id=row_deck_id,
                preset_id=preset_id,
            ),
            is_query=False,
            ease=ease,
            duration_millis=duration_millis,
            card_type=historical_state,
            card_queue=_historical_review_queue(review_kind),
            card_due=None,
            interval_days=interval_days,
            ease_factor=ease_factor,
            reps=None,
            lapses=None,
            day_offset=day_offset,
            current_state_kind=state_kind,
            current_normal_state_kind=normal_state_kind,
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_seconds,
        )
        review_ids.append(review_id)
        reviews.append(review_input)
        history_hash = _rwkv_history_hash_after_review(
            history_hash,
            review_id,
            review_input,
        )
        if (
            prepared_row_count == row_count
            or prepared_row_count % prepare_report_every == 0
        ):
            _report_rwkv_review_input_prepare_progress(
                progress,
                processed=prepared_row_count,
                total=row_count,
                started_at=prepare_started_at,
            )
    logger.debug(
        "RWKV historical review inputs built: rows=%s reviews=%s "
        "dynamic_preset_replay=%s historical_preset_rules=%s "
        "historical_preset_rule_matches=%s "
        "rows_elapsed_ms=%.1f historical_preset_rules_elapsed_ms=%.1f "
        "deck_config_elapsed_ms=%.1f "
        "preset_elapsed_ms=%.1f elapsed_ms=%.1f "
        "deck_id=%s",
        row_count,
        len(reviews),
        dynamic_preset_replay,
        len(historical_preset_rules),
        historical_preset_rule_matches,
        rows_elapsed_ms,
        historical_preset_rules_elapsed_ms,
        deck_config_elapsed_ms,
        preset_elapsed_ms,
        (time.monotonic() - start) * 1000,
        requested_deck_id,
    )
    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=last_review_id,
        review_count=review_count,
        deck_id=requested_deck_id,
        history_hash=history_hash,
        replay_key=replay_key,
        ignored_review_ids=active_ignored_review_ids,
        prepared_checkpoint_histories=prepared_checkpoint_histories,
    )


def _report_rwkv_review_input_prepare_progress(
    progress: RwkvStateCacheProgressCallback | None,
    *,
    processed: int,
    total: int,
    started_at: float,
) -> None:
    if progress is None:
        return

    replay_progress = RwkvWarmUpProgress(
        processed_reviews=processed,
        total_reviews=total,
    )
    _report_rwkv_state_cache_progress(
        progress,
        _rwkv_replay_progress_label(
            "Preparing RWKV review inputs",
            replay_progress,
            elapsed_seconds=time.monotonic() - started_at,
        ),
        min(processed, total),
        total,
    )


def _historical_rwkv_review_card_ids(rows: Iterable[Sequence[object]]) -> list[int]:
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


def _historical_deck_configs_by_deck_id(
    reviewer: object,
    rows: Iterable[Sequence[object]],
) -> dict[int, object | None]:
    deck_ids = sorted(
        {
            deck_id
            for row in rows
            if len(row) >= 4
            and isinstance((deck_id := row[3]), int)
            and not isinstance(deck_id, bool)
        }
    )
    return {
        deck_id: _deck_config_for_deck_id(reviewer, deck_id) for deck_id in deck_ids
    }


def _historical_deck_config_ids_by_card(
    reviewer: object,
    rows: Iterable[Sequence[object]],
    *,
    deck_configs_by_deck_id: Mapping[int, object | None] | None = None,
) -> dict[int, int | str | None]:
    preset_ids: dict[int, int | str | None] = {}
    deck_config_ids: dict[int, int | None] = {}
    for row in rows:
        if len(row) < 4:
            continue
        card_id = row[1]
        deck_id = row[3]
        if not isinstance(card_id, int) or not isinstance(deck_id, int):
            continue
        if deck_id not in deck_config_ids:
            deck_config = (
                deck_configs_by_deck_id[deck_id]
                if deck_configs_by_deck_id is not None
                and deck_id in deck_configs_by_deck_id
                else _deck_config_for_deck_id(reviewer, deck_id)
            )
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
                preset_id=preset_id,
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
) -> str | None:
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
    deck_id: int | None = None,
    limit: int | None = None,
) -> list[Sequence[object]]:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows):
        return []

    after_clause = "and r.id > ?" if after_review_id is not None else ""
    deck_ids = _deck_tree_ids(reviewer, deck_id)
    effective_deck_sql = "(case when c.odid != 0 then c.odid else c.did end)"
    deck_clause = f"and {effective_deck_sql} in {ids2str(deck_ids)}" if deck_ids else ""
    limit_clause = f"limit {max(0, limit)}" if limit is not None else ""
    sql = f"""
select
  r.id,
  r.cid,
  c.nid,
  {effective_deck_sql},
  r.ease,
  r.time,
  r.type,
  cast(r.ivl as integer),
  cast(r.factor as integer)
from revlog r
join cards c on c.id = r.cid
where {_rwkv_historical_answer_sql_condition("r")}
  {after_clause}
  {deck_clause}
order by r.id, r.cid
{limit_clause}
"""
    start = time.monotonic()
    logger.debug(
        "RWKV historical review rows query started: after_review_id=%s deck_id=%s",
        after_review_id,
        deck_id,
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
    *,
    deck_id: int | None = None,
) -> int:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    scalar = getattr(db, "scalar", None)
    if not callable(scalar):
        return 0

    deck_ids = _deck_tree_ids(reviewer, deck_id)
    effective_deck_sql = "(case when c.odid != 0 then c.odid else c.did end)"
    deck_clause = f"and {effective_deck_sql} in {ids2str(deck_ids)}" if deck_ids else ""
    value = scalar(
        f"""
with eligible as (
  select
    r.id,
    r.cid,
    r.type,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where {_rwkv_historical_answer_sql_condition("r")}
    {deck_clause}
), retained_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
)
select count()
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
  and e.id <= ?
""",
        last_review_id,
    )
    return value if isinstance(value, int) else 0


def _deck_tree_ids(reviewer: object, deck_id: int | None) -> list[int]:
    if deck_id is None:
        return []

    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    deck_and_child_ids = getattr(decks, "deck_and_child_ids", None)
    if not callable(deck_and_child_ids):
        return [deck_id]

    try:
        deck_ids = deck_and_child_ids(deck_id)
    except Exception:
        logger.debug("failed to read deck tree for RWKV historical replay")
        return [deck_id]

    valid_ids = [
        int(value)
        for value in deck_ids
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return valid_ids or [deck_id]


def _historical_review_day_offset(
    review_id: int,
    *,
    days_elapsed: int,
    next_day_at: int,
) -> int:
    review_secs = review_id // 1000
    days_before_today = max(0, next_day_at - 1 - review_secs) // 86_400
    return max(0, days_elapsed - days_before_today)


def _benchmark_retained_historical_review_rows(
    rows: Sequence[Sequence[object]],
) -> list[tuple[Sequence[object], int]]:
    retained_start_by_card = _benchmark_retained_historical_review_starts(rows)
    return [
        (row, historical_state)
        for index, row in enumerate(rows)
        if (
            historical_state := _benchmark_retained_historical_review_state(
                index,
                row,
                retained_start_by_card,
            )
        )
        is not None
    ]


def _benchmark_retained_historical_review_starts(
    rows: Sequence[Sequence[object]],
) -> dict[int, tuple[int, bool]]:
    retained_start_by_card: dict[int, tuple[int, bool]] = {}
    previous_kind_by_card: dict[int, int] = {}

    for index, row in enumerate(rows):
        if len(row) < 7:
            continue
        card_id = row[1]
        review_kind = row[6]
        if not isinstance(card_id, int) or not isinstance(review_kind, int):
            continue
        retained_start_by_card.setdefault(card_id, (index, False))
        previous_kind = previous_kind_by_card.get(card_id)
        if review_kind == 0 and previous_kind != 0:
            retained_start_by_card[card_id] = (index, True)
        previous_kind_by_card[card_id] = review_kind

    return retained_start_by_card


def _benchmark_retained_historical_review_state(
    index: int,
    row: Sequence[object],
    retained_start_by_card: Mapping[int, tuple[int, bool]],
) -> int | None:
    if len(row) < 7:
        return None
    card_id = row[1]
    review_kind = row[6]
    if not isinstance(card_id, int) or not isinstance(review_kind, int):
        return None
    start_index, starts_with_learning = retained_start_by_card[card_id]
    if index < start_index:
        return None
    return _historical_review_state(
        review_kind,
        is_learning_start=starts_with_learning and index == start_index,
    )


def _historical_review_state(
    review_kind: int,
    *,
    is_learning_start: bool = False,
) -> int:
    # The dataset builder increments raw revlog kinds before RWKV scales them.
    return 0 if is_learning_start else review_kind + 1


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


def _rwkv_review_enforce_grade_order_config(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_enforce_grade_order")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewEnforceGradeOrder",
        "rwkv_review_enforce_grade_order",
    )
    return value if isinstance(value, bool) else True


def _rwkv_review_config_active(deck_config: dict[str, object]) -> bool:
    return _rwkv_review_config_enabled(
        deck_config
    ) or _rwkv_review_instant_order_enabled(deck_config)


def _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer: object) -> bool:
    return _rwkv_collection_config_state(reviewer).dynamic_preset_replay_enabled


def _rwkv_first_review_elapsed_config_key(
    reviewer: object,
) -> list[list[object]]:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if not callable(all_config):
        return []

    try:
        configs = all_config()
    except Exception:
        logger.debug("failed to read deck configs for RWKV first review elapsed")
        return []

    key: list[list[object]] = []
    for index, config in enumerate(configs):
        if not isinstance(config, dict):
            continue
        config_id = config.get("id")
        key.append(
            [
                config_id if isinstance(config_id, int) else f"index:{index}",
                _rwkv_review_first_review_elapsed_from_card_creation(config),
            ]
        )

    return sorted(key, key=lambda item: str(item[0]))


def _rwkv_preset_routing_config_key(
    reviewer: object,
) -> list[list[object]]:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_names_and_ids = getattr(decks, "all_names_and_ids", None)
    if not callable(all_names_and_ids):
        return []

    try:
        raw_decks = all_names_and_ids()
    except Exception:
        logger.debug("failed to read deck routing for RWKV replay identity")
        return []

    routing: list[list[object]] = []
    for raw_deck in raw_decks:
        raw_deck_id = (
            raw_deck.get("id")
            if isinstance(raw_deck, dict)
            else getattr(raw_deck, "id", None)
        )
        if not isinstance(raw_deck_id, int) or isinstance(raw_deck_id, bool):
            continue
        config = _deck_config_for_deck_id(reviewer, raw_deck_id)
        config_id = config.get("id") if isinstance(config, dict) else None
        routing.append(
            [
                raw_deck_id,
                config_id if isinstance(config_id, int) else None,
            ]
        )

    return sorted(routing, key=lambda item: cast(int, item[0]))


def _rwkv_replay_semantics_key(
    reviewer: object,
    *,
    first_review_elapsed_source: RwkvFirstReviewElapsedSource,
) -> str:
    dynamic_preset_replay = _rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
    payload: dict[str, object] = {
        "version": _RWKV_PRESET_REPLAY_SEMANTICS_VERSION,
        "firstReviewElapsedSource": first_review_elapsed_source.value,
        "firstReviewElapsedConfig": (
            _rwkv_first_review_elapsed_config_key(reviewer)
            if first_review_elapsed_source == RwkvFirstReviewElapsedSource.DECK_CONFIG
            else []
        ),
        "dynamicPresetReplay": dynamic_preset_replay,
        "presetRouting": _rwkv_preset_routing_config_key(reviewer),
        "presetOverlay": (
            _fsrs_preset_overlay_config(reviewer) if dynamic_preset_replay else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf8")
    return hashlib.sha256(encoded).hexdigest()


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


def _rwkv_review_first_review_elapsed_from_card_creation(
    deck_config: dict[str, object],
) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_first_review_elapsed_from_card_creation")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewFirstReviewElapsedFromCardCreation",
        "rwkv_review_first_review_elapsed_from_card_creation",
    )
    return (
        value
        if isinstance(value, bool)
        else _DEFAULT_RWKV_REVIEW_FIRST_REVIEW_ELAPSED_FROM_CARD_CREATION
    )


def _new_gather_uses_retrievability(deck_config: dict[str, object]) -> bool:
    value = deck_config.get(
        "newCardGatherPriority",
        deck_config.get("new_card_gather_priority"),
    )
    return value in (
        _NEW_GATHER_PRIORITY_ASCENDING_RETRIEVABILITY,
        _NEW_GATHER_PRIORITY_DESCENDING_RETRIEVABILITY,
    )


def _search_text_explicitly_includes_new_cards(search: str) -> bool:
    return re.search(r"(?<![-\w:])is:new(?![\w:])", search, re.IGNORECASE) is not None


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


def _rwkv_review_candidate_refresh_enabled(deck_config: dict[str, object]) -> bool:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_candidate_refresh_enabled")
        if isinstance(value, bool):
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewCandidateRefreshEnabled",
        "rwkv_review_candidate_refresh_enabled",
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


def _rwkv_existing_other_config_key(root: Mapping[str, object]) -> str | None:
    if isinstance(root.get("jschoreels.rwkv"), dict):
        return "jschoreels.rwkv"
    if isinstance(root.get("jschoreels.fsrs"), dict):
        return "jschoreels.fsrs"
    return None


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


def _reviewer_scoped_to_collection(
    reviewer: object,
    collection: object | None,
) -> object:
    """Keep request construction pinned to the collection captured by async work."""

    if collection is None or _collection(reviewer) is collection:
        return reviewer

    original_mw = getattr(reviewer, "mw", None)
    scoped_reviewer = SimpleNamespace(
        mw=SimpleNamespace(
            col=collection,
            reviewer=getattr(original_mw, "reviewer", None),
        )
    )
    answered_ids = getattr(reviewer, "_answeredIds", None)
    if isinstance(answered_ids, list):
        scoped_reviewer._answeredIds = answered_ids
    return scoped_reviewer


def _rwkv_collection_identity_is_current(
    *,
    collection_owner: object | None,
    collection: object | None,
    collection_backend: object | None,
) -> bool:
    if collection_owner is None:
        return True
    if getattr(collection_owner, "col", None) is not collection:
        return False
    if collection is None:
        return collection_backend is None
    return getattr(collection, "_backend", None) is collection_backend


def _rwkv_review_collection_key(
    reviewer: object,
) -> RwkvReviewQueueCollectionKey | None:
    col = _collection(reviewer)
    if col is None:
        return None

    return (id(col), id(getattr(col, "_backend", None)))


def _clear_rwkv_review_queue_score_cache() -> None:
    _rwkv_review_queue_score_maps.clear()
    _rwkv_review_queue_target_maps.clear()
    _rwkv_review_queue_score_generations.clear()
    _rwkv_review_queue_score_config_keys.clear()


def _ensure_rwkv_review_collection_scope(
    reviewer: object,
) -> RwkvReviewQueueCollectionKey | None:
    global _rwkv_review_queue_collection_key

    collection_key = _rwkv_review_collection_key(reviewer)
    if collection_key is None:
        return None

    if _rwkv_review_queue_collection_key is None:
        _rwkv_review_queue_collection_key = collection_key
    elif _rwkv_review_queue_collection_key != collection_key:
        logger.debug(
            "RWKV queue collection changed; clearing transient caches: "
            "previous=%s current=%s",
            _rwkv_review_queue_collection_key,
            collection_key,
        )
        _clear_rwkv_review_queue_score_cache()
        _rwkv_review_input_batch_module_cache.clear()
        _rwkv_review_queue_collection_key = collection_key

    return collection_key


def _rwkv_review_deck_scope_key(
    reviewer: object,
    deck_id: int,
) -> tuple[int, ...]:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    deck_and_child_ids = getattr(decks, "deck_and_child_ids", None)
    if callable(deck_and_child_ids):
        try:
            deck_ids = deck_and_child_ids(deck_id)
        except Exception:
            logger.debug(
                "failed to read deck scope for RWKV queue context: deck_id=%s",
                deck_id,
            )
        else:
            return tuple(
                sorted(
                    value
                    for value in deck_ids
                    if isinstance(value, int) and not isinstance(value, bool)
                )
            )

    return (deck_id,)


def _rwkv_review_queue_configuration_key(reviewer: object) -> str:
    col = _collection(reviewer)
    decks = getattr(col, "decks", None)
    all_config = getattr(decks, "all_config", None)
    if not callable(all_config):
        return ""

    try:
        configs = all_config()
        payload = json.dumps(
            configs,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
    except Exception:
        logger.debug("failed to fingerprint deck configs for RWKV queue context")
        return ""

    return hashlib.sha256(payload.encode("utf8")).hexdigest()


def _rwkv_review_queue_context(
    reviewer: object,
    deck_id: int,
) -> RwkvReviewQueueContext | None:
    selected_deck_id = _current_deck_id(reviewer)
    if selected_deck_id is None:
        return None

    collection_key = _ensure_rwkv_review_collection_scope(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if (
        collection_key is None
        or not isinstance(days_elapsed, int)
        or not isinstance(next_day_at, int)
    ):
        return None

    with _reviewer_backend_state_lock:
        dynamic_desired_retention_generation = _dynamic_desired_retention_generation
        study_queue_generation = _rwkv_study_queue_generation

    return RwkvReviewQueueContext(
        collection_key=collection_key,
        selected_deck_id=selected_deck_id,
        deck_id=deck_id,
        deck_scope=_rwkv_review_deck_scope_key(reviewer, deck_id),
        days_elapsed=days_elapsed,
        next_day_at=next_day_at,
        config_key=_rwkv_review_queue_configuration_key(reviewer),
        dynamic_desired_retention_generation=(dynamic_desired_retention_generation),
        study_queue_generation=study_queue_generation,
    )


def _rwkv_review_queue_context_epochs_are_current(
    context: RwkvReviewQueueContext | _ReviewerBackendPredictionStateToken,
) -> bool:
    """Validate queue-wide epochs while the caller holds the state lock."""

    return (
        context.dynamic_desired_retention_generation
        == _dynamic_desired_retention_generation
        and context.study_queue_generation == _rwkv_study_queue_generation
    )


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


def _rwkv_review_input_build_inputs(
    input_build: RwkvReviewInputBatchBuild,
) -> list[tuple[int, RwkvReviewInput]]:
    return [
        item
        for inputs_by_card_id in input_build.inputs_by_batch_size.values()
        for item in inputs_by_card_id
    ]


def _resolve_dynamic_desired_retentions_for_input_build(
    reviewer: object,
    input_build: RwkvReviewInputBatchBuild,
) -> RwkvReviewInputBatchBuild:
    if input_build.dynamic_desired_retentions_resolved:
        return input_build

    inputs_by_card_id = _rwkv_review_input_build_inputs(input_build)
    resolved_inputs_by_card_id = _resolve_dynamic_desired_retentions_for_inputs(
        reviewer,
        inputs_by_card_id,
    )
    if resolved_inputs_by_card_id is inputs_by_card_id:
        return replace(input_build, dynamic_desired_retentions_resolved=True)

    remaining_by_card_id = dict(resolved_inputs_by_card_id)
    resolved_inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    for batch_size, batch_inputs in input_build.inputs_by_batch_size.items():
        resolved_batch = [
            (card_id, remaining_by_card_id.pop(card_id, review_input))
            for card_id, review_input in batch_inputs
        ]
        if resolved_batch:
            resolved_inputs_by_batch_size[batch_size] = resolved_batch

    return replace(
        input_build,
        inputs_by_batch_size=resolved_inputs_by_batch_size,
        dynamic_desired_retentions_resolved=True,
    )


def _invalidate_rwkv_review_input_caches(mw: object) -> int:
    global _dynamic_desired_retention_generation

    with _reviewer_backend_state_lock:
        _dynamic_desired_retention_generation += 1
        generation = _dynamic_desired_retention_generation
    _clear_rwkv_review_queue_score_cache()
    _rwkv_review_input_batch_module_cache.clear()
    with _rwkv_score_prewarm_lock:
        _rwkv_score_prewarm_in_flight.clear()

    reviewer = SimpleNamespace(mw=mw)
    _clear_rwkv_review_queue_scores(reviewer)
    clear_deck_browser_rwkv_count_scores(mw)
    return generation


def dynamic_desired_retention_did_change(mw: object) -> None:
    """Invalidate RWKV targets and refresh study queues after provider changes."""

    generation = _invalidate_rwkv_review_input_caches(mw)

    reset = getattr(mw, "reset", None)
    if callable(reset):
        reset()

    logger.debug(
        "RWKV Dynamic DR caches invalidated: generation=%s",
        generation,
    )


def collection_content_did_change(mw: object, initiator: object | None) -> None:
    """Refresh content-dependent inputs without discarding unchanged RWKV state."""

    reviewer = SimpleNamespace(mw=mw)
    changed_card_ids = _collection_content_change_card_ids(reviewer, initiator)
    if _fsrs_preset_overlay_has_routing_rules(reviewer) and not (
        changed_card_ids
        and _changed_cards_keep_rwkv_preset_routing(reviewer, changed_card_ids)
    ):
        fsrs_preset_resolution_did_change(mw)
        _invalidate_rwkv_review_input_caches(mw)
        return

    generation = _invalidate_rwkv_review_input_caches(mw)
    logger.debug(
        "RWKV resident state retained after collection content mutation: "
        "cards=%s generation=%s",
        len(changed_card_ids),
        generation,
    )


def _fsrs_preset_overlay_has_routing_rules(reviewer: object) -> bool:
    col = _collection(reviewer)
    get_config = getattr(col, "get_config", None)
    if not callable(get_config):
        return True

    try:
        overlay = get_config(_FSRS_PRESET_OVERLAY_CONFIG_KEY)
    except Exception:
        logger.debug(
            "failed to inspect FSRS preset overlay after collection content mutation",
            exc_info=True,
        )
        return True

    if not isinstance(overlay, dict):
        return False
    return any(
        isinstance(rules, list) and bool(rules)
        for rules in (
            overlay.get("rules"),
            overlay.get("simulator_rules"),
        )
    )


def _collection_content_change_card_ids(
    reviewer: object,
    initiator: object | None,
) -> tuple[int, ...]:
    card = getattr(initiator, "card", None)
    card_id = _valid_card_id(getattr(card, "id", None))
    note_id = _valid_card_id(getattr(initiator, "nid", None))
    if note_id is None:
        note = getattr(initiator, "note", None)
        note_id = _valid_card_id(getattr(note, "id", None))

    if note_id is None:
        return (card_id,) if card_id is not None else ()

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    list_rows = getattr(db, "list", None)
    if not callable(list_rows):
        return ()

    try:
        card_ids = [
            valid_card_id
            for value in list_rows("select id from cards where nid = ?", note_id)
            if (valid_card_id := _valid_card_id(value)) is not None
        ]
    except Exception:
        logger.debug(
            "failed to resolve changed note cards for RWKV preset comparison",
            exc_info=True,
        )
        return ()

    return tuple(dict.fromkeys(card_ids))


def _changed_cards_keep_rwkv_preset_routing(
    reviewer: object,
    card_ids: Sequence[int],
) -> bool:
    historical_card_ids = _historical_rwkv_card_ids(reviewer, card_ids)
    if historical_card_ids is None:
        return False

    cache = _resolved_preset_id_cache.get(_preset_id_cache_key(reviewer), {})
    if any(card_id not in cache for card_id in historical_card_ids):
        return False
    previous_preset_ids = {card_id: cache[card_id] for card_id in historical_card_ids}

    _invalidate_resolved_preset_id_cache(reviewer, card_ids=card_ids)
    current_preset_ids = _resolved_fsrs_preset_ids(reviewer, card_ids)
    return all(
        current_preset_ids.get(card_id) == previous_preset_id
        for card_id, previous_preset_id in previous_preset_ids.items()
    )


def _historical_rwkv_card_ids(
    reviewer: object,
    card_ids: Sequence[int],
) -> set[int] | None:
    if not card_ids:
        return set()

    col = _collection(reviewer)
    db = getattr(col, "db", None)
    list_rows = getattr(db, "list", None)
    if not callable(list_rows):
        return None

    try:
        values = list_rows(
            f"""
select distinct cid
from revlog
where cid in {ids2str(card_ids)}
  and {_rwkv_historical_answer_sql_condition()}
"""
        )
    except Exception:
        logger.debug(
            "failed to resolve changed cards with RWKV history",
            exc_info=True,
        )
        return None

    return {
        card_id for value in values if (card_id := _valid_card_id(value)) is not None
    }


def fsrs_preset_resolution_did_change(mw: object) -> None:
    """Discard resident state and preset assignments after collection changes."""

    reviewer = SimpleNamespace(mw=mw)
    _invalidate_reviewer_backend_state(
        reviewer,
        reason="collection routing mutation",
    )
    _invalidate_resolved_preset_id_cache(reviewer)


def study_queues_did_change(
    mw: object,
    initiator: object | None,
    changes: collection_pb2.OpChanges | None = None,
) -> None:
    """Discard RWKV queue work after a non-answer study-queue mutation."""

    global _rwkv_study_queue_generation

    reviewer = getattr(mw, "reviewer", None)
    if reviewer is not None and (
        initiator is reviewer or _consume_reviewer_undo_queue_change(reviewer)
    ):
        return

    transient_reviewer = SimpleNamespace(mw=mw)
    deck_browser = getattr(mw, "deckBrowser", None)
    resident_state_preserved = (
        changes is not None
        and changes.config
        and deck_browser is not None
        and initiator is deck_browser
        and not any(
            (
                changes.card,
                changes.note,
                changes.deck,
                changes.tag,
                changes.notetype,
                changes.deck_config,
            )
        )
    )
    if resident_state_preserved:
        try:
            _clear_rwkv_review_queue_scores(transient_reviewer)
        except Exception:
            logger.exception(
                "failed to clear RWKV queue scores after queue-only mutation"
            )
        resident_identity = _rwkv_ready_state_cache_history_identity(transient_reviewer)
        if resident_identity is not None:
            _refresh_rwkv_state_cache_collection_mod(
                transient_reviewer,
                resident_identity,
            )
    else:
        _invalidate_reviewer_backend_state(
            transient_reviewer,
            reason="study queue mutation",
        )
    with _reviewer_backend_state_lock:
        _rwkv_study_queue_generation += 1
        generation = _rwkv_study_queue_generation
    _clear_rwkv_review_queue_score_cache()
    _rwkv_review_input_batch_module_cache.clear()
    with _rwkv_score_prewarm_lock:
        _rwkv_score_prewarm_in_flight.clear()

    _invalidate_resolved_preset_id_cache(transient_reviewer)
    clear_deck_browser_rwkv_count_scores(mw)
    logger.debug(
        "RWKV study queue caches invalidated: generation=%s initiator=%s "
        "resident_state_preserved=%s",
        generation,
        type(initiator).__name__ if initiator is not None else None,
        resident_state_preserved,
    )


def _resolve_dynamic_desired_retentions_for_inputs(
    reviewer: object,
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
) -> Sequence[tuple[int, RwkvReviewInput]]:
    start = time.monotonic()
    target_retentions_by_card_id = _dynamic_desired_retention_targets_for_inputs(
        reviewer,
        inputs_by_card_id,
    )
    if not target_retentions_by_card_id:
        return inputs_by_card_id

    resolved_inputs: list[tuple[int, RwkvReviewInput]] = []
    updated = 0
    for card_id, review_input in inputs_by_card_id:
        target_retention = target_retentions_by_card_id.get(card_id)
        if target_retention is None:
            resolved_inputs.append((card_id, review_input))
            continue

        resolved_input = _rwkv_review_input_with_target_retention(
            review_input,
            target_retention,
        )
        if resolved_input is not review_input:
            updated += 1
        resolved_inputs.append((card_id, resolved_input))

    if not updated:
        return inputs_by_card_id

    logger.debug(
        "RWKV Dynamic DR targets resolved: inputs=%s targets=%s updated=%s "
        "elapsed_ms=%.1f",
        len(inputs_by_card_id),
        len(target_retentions_by_card_id),
        updated,
        (time.monotonic() - start) * 1000,
    )
    return resolved_inputs


def _dynamic_desired_retention_targets_for_inputs(
    reviewer: object,
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
) -> dict[int, float]:
    info_for_cards = _dynamic_desired_retention_info_for_cards_resolver()
    if info_for_cards is None:
        return {}

    current_desired_retentions = {
        card_id: target_retention
        for card_id, review_input in inputs_by_card_id
        if (target_retention := _rwkv_review_input_target_retention(review_input))
        is not None
    }
    if not current_desired_retentions:
        return {}

    col = _collection(reviewer)
    get_card = getattr(col, "get_card", None)
    if not callable(get_card):
        return {}

    cards = []
    for card_id in current_desired_retentions:
        try:
            card = get_card(card_id)
        except Exception:
            logger.debug("failed to load card for RWKV Dynamic DR: card_id=%s", card_id)
            continue
        if _card_id(card) == card_id:
            cards.append(card)

    if not cards:
        return {}

    info_by_card_id = _dynamic_desired_retention_info_for_cards(
        collection=col,
        cards=cards,
        current_desired_retentions=current_desired_retentions,
        info_for_cards=info_for_cards,
    )
    if not isinstance(info_by_card_id, Mapping):
        return {}

    target_retentions_by_card_id: dict[int, float] = {}
    for card in cards:
        card_id = _card_id(card)
        if card_id is None:
            continue
        info = info_by_card_id.get(card_id)
        target_retention = getattr(info, "desired_retention", None)
        if _valid_probability(target_retention):
            target_retentions_by_card_id[card_id] = float(target_retention)

    return target_retentions_by_card_id


def _dynamic_desired_retention_info_for_cards_resolver() -> (
    Callable[..., object] | None
):
    try:
        dynamic_desired_retention = importlib.import_module("dynamic_desired_retention")
    except ImportError:
        return None
    except Exception:
        logger.debug("failed to import Dynamic DR provider for RWKV", exc_info=True)
        return None

    effective_desired_retention_info_for_cards = getattr(
        dynamic_desired_retention,
        "effective_desired_retention_info_for_cards",
        None,
    )
    if not callable(effective_desired_retention_info_for_cards):
        return None

    return effective_desired_retention_info_for_cards


def _dynamic_desired_retention_info_for_cards(
    *,
    collection: object,
    cards: Sequence[object],
    current_desired_retentions: Mapping[int, float | None],
    info_for_cards: Callable[..., object],
) -> object | None:
    try:
        return info_for_cards(
            collection=collection,
            cards=cards,
            current_desired_retentions=current_desired_retentions,
        )
    except Exception:
        logger.debug("failed to resolve Dynamic DR targets for RWKV", exc_info=True)
        return None


def _rwkv_review_input_with_target_retention(
    review_input: RwkvReviewInput,
    target_retention: float,
) -> RwkvReviewInput:
    current_target_retention = _rwkv_review_input_target_retention(review_input)
    if current_target_retention is not None and math.isclose(
        current_target_retention,
        target_retention,
        rel_tol=0.0,
        abs_tol=1e-6,
    ):
        return review_input

    return replace(
        review_input,
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
    )


def _rwkv_review_input_build_target_retentions_by_card_id(
    input_build: RwkvReviewInputBatchBuild,
) -> dict[int, float]:
    return _rwkv_review_input_target_retentions_by_card_id(
        _rwkv_review_input_build_inputs(input_build)
    )


def _rwkv_review_input_target_retentions_by_card_id(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
) -> dict[int, float]:
    return {
        card_id: target_retention
        for card_id, review_input in inputs_by_card_id
        if (target_retention := _rwkv_review_input_target_retention(review_input))
        is not None
    }


def _rwkv_review_input_target_retention(
    review_input: RwkvReviewInput,
) -> float | None:
    if len(review_input.target_retentions) < 3:
        return None
    target_retention = review_input.target_retentions[2]
    return target_retention if _valid_probability(target_retention) else None


def _candidate_refreshed_rwkv_review_queue_scores_for_deck(
    *,
    reviewer: object,
    deck_id: int,
    deck_config: dict[str, object],
    batch_size: int,
) -> tuple[list[tuple[int, float]], dict[int, float], int, int] | None:
    if not _rwkv_review_candidate_refresh_enabled(deck_config):
        return None

    existing_scores = _rwkv_review_queue_score_map_for_deck(reviewer, deck_id)
    if not existing_scores:
        return None
    existing_target_retentions = _rwkv_review_queue_target_map_for_deck(
        reviewer,
        deck_id,
    )

    candidate_ids = _rwkv_review_candidate_refresh_card_ids(
        deck_config,
        existing_scores,
        existing_target_retentions,
        limit=batch_size,
    )
    current_card_id = _card_id(getattr(reviewer, "card", None))
    if current_card_id is not None:
        candidate_ids.append(current_card_id)
    candidate_ids = _unique_rwkv_candidate_card_ids(candidate_ids)
    if not candidate_ids:
        return None

    fresh_score_result = _rwkv_review_queue_score_result(
        reviewer=reviewer,
        card_ids=candidate_ids,
        batch_size=batch_size,
    )
    fresh_scores_by_card_id = dict(fresh_score_result.scores)
    merged_scores = dict(existing_scores)
    merged_target_retentions = (
        dict(existing_target_retentions)
        if existing_target_retentions is not None
        else {}
    )
    for card_id in candidate_ids:
        if card_id in fresh_scores_by_card_id:
            merged_scores[card_id] = fresh_scores_by_card_id[card_id]
            target_retention = fresh_score_result.target_retentions_by_card_id.get(
                card_id
            )
            if target_retention is not None:
                merged_target_retentions[card_id] = target_retention
        else:
            merged_scores.pop(card_id, None)
            merged_target_retentions.pop(card_id, None)

    return (
        sorted(merged_scores.items()),
        merged_target_retentions,
        len(candidate_ids),
        len(fresh_score_result.scores),
    )


def _rwkv_review_candidate_refresh_card_ids(
    deck_config: dict[str, object],
    scores: dict[int, float],
    target_retentions_by_card_id: dict[int, float] | None,
    *,
    limit: int,
) -> list[int]:
    if limit <= 0:
        return []

    review_order = deck_config.get("reviewOrder", deck_config.get("review_order"))
    if review_order == _REVIEW_ORDER_RETRIEVABILITY_DESCENDING:
        ordered_scores = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    elif review_order == _REVIEW_ORDER_RELATIVE_OVERDUENESS:
        targets = target_retentions_by_card_id or {}
        ordered_scores = sorted(
            scores.items(),
            key=lambda item: (
                _rwkv_relative_overdueness(item[1], targets.get(item[0])),
                item[0],
            ),
        )
    else:
        ordered_scores = sorted(scores.items(), key=lambda item: (item[1], item[0]))

    return [card_id for card_id, _ in ordered_scores[:limit]]


def _unique_rwkv_candidate_card_ids(card_ids: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    unique: list[int] = []
    for value in card_ids:
        card_id = _valid_card_id(value)
        if card_id is None or card_id in seen:
            continue
        seen.add(card_id)
        unique.append(card_id)
    return unique


def _candidate_refreshed_rwkv_review_queue_async_work(
    *,
    reviewer: object,
    deck_id: int,
    deck_config: dict[str, object],
    reason: str,
    batch_size: int,
    state_generation: int,
    backend_assignment_generation: int | None = None,
    context: RwkvReviewQueueContext,
    warmup_elapsed_ms: float,
    start: float,
    expected_backend: RwkvReviewerBackend,
) -> RwkvReviewQueueOrderAsyncWork | None:
    if not _rwkv_review_candidate_refresh_enabled(deck_config):
        return None

    existing_scores = _rwkv_review_queue_score_map_for_deck(reviewer, deck_id)
    if not existing_scores:
        return None
    existing_target_retentions = _rwkv_review_queue_target_map_for_deck(
        reviewer,
        deck_id,
    )

    candidate_ids = _rwkv_review_candidate_refresh_card_ids(
        deck_config,
        existing_scores,
        existing_target_retentions,
        limit=batch_size,
    )
    current_card_id = _card_id(getattr(reviewer, "card", None))
    if current_card_id is not None:
        candidate_ids.append(current_card_id)
    candidate_ids = _unique_rwkv_candidate_card_ids(candidate_ids)
    if not candidate_ids:
        return None

    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return None

    input_build = _rwkv_review_input_batches_for_ids(
        reviewer=reviewer,
        card_ids=candidate_ids,
        timing=timing,
        reason=reason,
        include_suspended_review=False,
        supported_state_filter=True,
        batch_size_override=batch_size,
    )
    if input_build is None:
        return None

    return _rwkv_review_queue_async_work_from_input_build(
        reviewer=reviewer,
        deck_id=deck_id,
        reason=reason,
        batch_size=batch_size,
        state_generation=state_generation,
        backend_assignment_generation=backend_assignment_generation,
        context=context,
        input_build=input_build,
        warmup_elapsed_ms=warmup_elapsed_ms,
        build_start=start,
        partial_refresh=RwkvReviewQueuePartialRefresh(
            existing_scores=tuple(sorted(existing_scores.items())),
            existing_target_retentions=(
                tuple(sorted(existing_target_retentions.items()))
                if existing_target_retentions is not None
                else None
            ),
            candidate_card_ids=tuple(candidate_ids),
        ),
        fresh_for_backend_state=False,
        expected_backend=expected_backend,
    )


def _rwkv_review_queue_async_work_from_input_build(  # noqa: PLR0913
    *,
    reviewer: object,
    deck_id: int,
    reason: str,
    batch_size: int,
    state_generation: int,
    backend_assignment_generation: int | None = None,
    context: RwkvReviewQueueContext,
    input_build: RwkvReviewInputBatchBuild,
    warmup_elapsed_ms: float,
    build_start: float,
    partial_refresh: RwkvReviewQueuePartialRefresh | None = None,
    fresh_for_backend_state: bool,
    expected_backend: RwkvReviewerBackend | None = None,
) -> RwkvReviewQueueOrderAsyncWork | None:
    collection_owner = getattr(reviewer, "mw", None)
    collection = _collection(reviewer)
    collection_backend = getattr(collection, "_backend", None)
    if context.collection_key != (id(collection), id(collection_backend)):
        return None

    input_build = _resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        input_build,
    )
    inputs_by_card_id = tuple(_rwkv_review_input_build_inputs(input_build))
    indexed_inputs = [
        (index, review_input)
        for index, (_, review_input) in enumerate(inputs_by_card_id)
    ]
    with _try_reviewer_backend_prediction_access(
        expected_backend=expected_backend,
        expected_backend_assignment_generation=backend_assignment_generation,
        expected_state_generation=state_generation,
    ) as backend:
        if backend is None:
            logger.debug(
                "RWKV async %s work preparation skipped: backend busy or stale",
                reason,
            )
            return None
        resident_state_token = _reviewer_backend_resident_state_token(
            reviewer,
            backend,
        )
        if (
            callable(getattr(backend, "warm_up", None))
            and _reviewer_backend_warmup_key(reviewer) is not None
            and resident_state_token is None
        ):
            logger.debug(
                "RWKV async %s work preparation skipped: resident state changed",
                reason,
            )
            return None
        resident_cached = _cached_retrievability_inputs_from_warm_up(
            indexed_inputs,
            backend=backend,
        )
        if resident_cached is not None:
            predictions, resident_inputs_by_index, cache_hits = resident_cached
            requests_by_index: Sequence[RwkvReviewPredictionRequestByIndex] = []
        else:
            cached = _cached_review_input_predictions_for_inputs(
                indexed_inputs,
                backend=backend,
            )
            if cached is None:
                return None
            predictions, requests_by_index, cache_hits = cached
            resident_inputs_by_index = []
        if not _reviewer_backend_prediction_access_is_current(
            backend,
            expected_backend_assignment_generation=(backend_assignment_generation),
            expected_state_generation=state_generation,
            expected_resident_state_key=(
                resident_state_token[0] if resident_state_token is not None else None
            ),
            expected_resident_state_generation=(
                resident_state_token[1] if resident_state_token is not None else None
            ),
        ):
            return None
        if (
            getattr(reviewer, "mw", None) is not collection_owner
            or getattr(collection_owner, "col", None) is not collection
            or getattr(collection, "_backend", None) is not collection_backend
        ):
            return None
    build_elapsed_ms = (time.monotonic() - build_start) * 1000
    logger.debug(
        "RWKV async %s work prepared: deck_id=%s searched=%s loaded=%s "
        "with_state=%s enabled=%s inputs=%s cache_hits=%s runtime_requests=%s "
        "deck_configs=%s batch_size=%s warmup_elapsed_ms=%.1f "
        "load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f build_elapsed_ms=%.1f",
        reason,
        deck_id,
        input_build.searched_rows,
        input_build.parsed_cards,
        input_build.cards_with_state,
        input_build.eligible_cards,
        len(inputs_by_card_id),
        cache_hits,
        len(requests_by_index) + len(resident_inputs_by_index),
        input_build.deck_configs,
        batch_size,
        warmup_elapsed_ms,
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        build_elapsed_ms,
    )
    return RwkvReviewQueueOrderAsyncWork(
        context=context,
        deck_id=deck_id,
        reason=reason,
        batch_size=batch_size,
        state_generation=state_generation,
        input_build=input_build,
        inputs_by_card_id=inputs_by_card_id,
        predictions=tuple(predictions),
        requests_by_index=tuple(requests_by_index),
        resident_inputs_by_index=tuple(resident_inputs_by_index),
        cache_hits=cache_hits,
        warmup_elapsed_ms=warmup_elapsed_ms,
        build_elapsed_ms=build_elapsed_ms,
        existing_scores=(
            partial_refresh.existing_scores if partial_refresh is not None else None
        ),
        existing_target_retentions=(
            partial_refresh.existing_target_retentions
            if partial_refresh is not None
            else None
        ),
        candidate_card_ids=(
            partial_refresh.candidate_card_ids if partial_refresh is not None else ()
        ),
        fresh_for_backend_state=fresh_for_backend_state,
        backend=backend,
        backend_assignment_generation=backend_assignment_generation,
        collection_owner=collection_owner,
        collection=collection,
        collection_backend=collection_backend,
        resident_state_key=(
            resident_state_token[0] if resident_state_token is not None else None
        ),
        resident_state_generation=(
            resident_state_token[1] if resident_state_token is not None else None
        ),
    )


def _rwkv_review_queue_scores_for_deck(
    *,
    reviewer: object,
    deck_id: int,
    batch_size: int,
    include_new_cards: bool,
) -> tuple[list[tuple[int, float]], RwkvReviewInputBatchBuild] | None:
    if not _reviewer_backend_accepts_review_inputs():
        return None

    start = time.monotonic()
    input_build = _rwkv_review_input_batches_for_deck_review_queue(
        reviewer=reviewer,
        deck_id=deck_id,
        batch_size_override=batch_size,
        include_new_cards=include_new_cards,
    )
    if input_build is None:
        return None

    input_build = _resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        input_build,
    )
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
    return _rwkv_review_queue_score_result(
        reviewer=reviewer,
        card_ids=card_ids,
        batch_size=batch_size,
    ).scores


def _rwkv_review_queue_score_result(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    batch_size: int,
) -> RwkvReviewQueueScoreResult:
    start = time.monotonic()
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return RwkvReviewQueueScoreResult(scores=[])

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
            input_build = _resolve_dynamic_desired_retentions_for_input_build(
                reviewer,
                input_build,
            )
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
                return RwkvReviewQueueScoreResult(
                    scores=scores,
                    target_retentions_by_card_id=_rwkv_review_input_build_target_retentions_by_card_id(
                        input_build
                    ),
                )

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
                and _rwkv_review_config_active(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            continue
        if _rwkv_review_first_review_elapsed_from_card_creation(deck_config):
            state_fields = _rwkv_state_fields_for_stats_graph_card(
                card,
                timing,
                include_suspended_review=False,
                first_review_elapsed_from_card_creation=True,
            )
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
    inputs_by_card_id = list(
        _resolve_dynamic_desired_retentions_for_inputs(
            reviewer,
            inputs_by_card_id,
        )
    )
    scores = (
        _rwkv_review_scores_for_inputs(
            inputs_by_card_id,
            batch_size=batch_size,
        )
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
    return RwkvReviewQueueScoreResult(
        scores=scores,
        target_retentions_by_card_id=_rwkv_review_input_target_retentions_by_card_id(
            inputs_by_card_id
        ),
    )


def _rwkv_stats_graph_prebuilt_input_scores(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    include_new_cards: bool,
    timing: object,
    start: float,
    use_input_scoring: bool,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> tuple[list[tuple[int, float]] | None, bool]:
    if not use_input_scoring:
        return None, False

    input_build = _rwkv_review_input_batches_for_ids(
        reviewer=reviewer,
        card_ids=card_ids,
        timing=timing,
        reason="stats graph",
        include_suspended_review=True,
        supported_state_filter=True,
        include_new_cards=include_new_cards,
    )
    if input_build is None:
        return None, True

    input_scores_accum: list[tuple[int, float]] = []
    queue_score_cache = _fresh_rwkv_review_queue_score_map(reviewer)
    queue_score_hits = 0
    score_start = time.monotonic()
    for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items():
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
            state_token=state_token,
        )
        if input_scores is None:
            return None, False
        input_scores_accum.extend(input_scores)

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
            for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items()
        },
        input_build.preset_elapsed_ms,
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        score_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return input_scores_accum, True


def _rwkv_stats_graph_scores(
    *,
    reviewer: object,
    card_ids: Sequence[int],
    include_new_cards: bool = False,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[tuple[int, float]]:
    start = time.monotonic()
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return []

    use_input_scoring = _reviewer_backend_accepts_review_inputs(
        state_token.backend if state_token is not None else None
    )
    input_scores, use_input_scoring = _rwkv_stats_graph_prebuilt_input_scores(
        reviewer=reviewer,
        card_ids=card_ids,
        include_new_cards=include_new_cards,
        timing=timing,
        start=start,
        use_input_scoring=use_input_scoring,
        state_token=state_token,
    )
    if input_scores is not None:
        return input_scores

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
                and _rwkv_review_config_active(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            disabled_config_cards += 1
            continue

        if _rwkv_review_first_review_elapsed_from_card_creation(deck_config):
            state_fields = _rwkv_state_fields_for_stats_graph_card(
                card,
                timing,
                include_suspended_review=True,
                first_review_elapsed_from_card_creation=True,
            )
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
                state_token=state_token,
            )
            if input_scores is None:
                use_input_scoring = False
                break
            scores.extend(input_scores)

    if not use_input_scoring:
        for batch_size, candidates in candidates_by_batch_size.items():
            scores.extend(
                _rwkv_review_scores_for_candidates(
                    candidates,
                    batch_size=batch_size,
                    state_token=state_token,
                )
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


def update_reviewer_queue_intervening_reviews(
    reviewer: object,
    card: object,
) -> None:
    """Update session-local RWKV repeat-spacing metadata without replacing scores."""

    min_intervening_reviews = reviewer_queue_order_min_intervening_reviews(
        reviewer,
        card,
    )
    if min_intervening_reviews <= 0:
        return

    deck_id = _deck_id(card)
    current_deck_id = _current_deck_id(reviewer)
    if deck_id is None:
        deck_id = current_deck_id
    if deck_id is None:
        return

    existing_scores = _rwkv_review_queue_score_map_for_deck(reviewer, deck_id)
    if (
        existing_scores is None
        and current_deck_id is not None
        and current_deck_id != deck_id
    ):
        current_deck_scores = _rwkv_review_queue_score_map_for_deck(
            reviewer,
            current_deck_id,
        )
        if current_deck_scores is not None:
            deck_id = current_deck_id
            existing_scores = current_deck_scores
    if existing_scores is None:
        return

    intervening_reviews_by_card_id = {
        card_id: min(intervening_reviews, min_intervening_reviews)
        for card_id, intervening_reviews in _session_intervening_reviews_by_card_id(
            reviewer,
            max_intervening_reviews=min_intervening_reviews,
        ).items()
        if card_id in existing_scores and intervening_reviews <= min_intervening_reviews
    }
    if not intervening_reviews_by_card_id:
        return

    mw = getattr(reviewer, "mw", None)
    col = getattr(mw, "col", None)
    backend = getattr(col, "_backend", None)
    request_type = getattr(
        scheduler_pb2,
        "RwkvReviewQueueInterveningReviewsRequest",
        None,
    )
    if request_type is None:
        invalidate_reviewer_queue_for_card_answer(reviewer, card)
        return

    request = request_type(deck_id=deck_id)
    for card_id, intervening_reviews in sorted(intervening_reviews_by_card_id.items()):
        request.items.add(
            card_id=card_id,
            intervening_reviews=intervening_reviews,
        )

    update_raw = getattr(
        backend,
        "update_rwkv_review_queue_intervening_reviews_raw",
        None,
    )
    if callable(update_raw):
        update_raw(request.SerializeToString())
        return

    update = getattr(backend, "update_rwkv_review_queue_intervening_reviews", None)
    if callable(update):
        update(deck_id=deck_id, items=list(request.items))
        return

    invalidate_reviewer_queue_for_card_answer(reviewer, card)


def reviewer_queue_order_needs_intervening_review_refresh(reviewer: object) -> bool:
    return reviewer_queue_order_min_intervening_reviews(reviewer) > 0


def reviewer_queue_order_min_intervening_reviews(
    reviewer: object,
    card: object | None = None,
) -> int:
    if card is None:
        card = getattr(reviewer, "card", None)
    deck_config = _rwkv_review_active_deck_config(reviewer, card)
    return _rwkv_review_min_intervening_reviews(deck_config) if deck_config else 0


def _rwkv_review_min_intervening_reviews(deck_config: dict[str, object]) -> int:
    nested = _rwkv_other_config(deck_config)
    if nested is not None:
        value = nested.get("rwkv_review_min_intervening_reviews")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value

    value = _rwkv_config_direct_value(
        deck_config,
        "rwkvReviewMinInterveningReviews",
        "rwkv_review_min_intervening_reviews",
    )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return _DEFAULT_RWKV_REVIEW_MIN_INTERVENING_REVIEWS


def _rwkv_stats_graph_scores_for_search(
    *,
    reviewer: object,
    search: str,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
    prepare_instant_due: bool = False,
    prepare_curve_due: bool = False,
    prepare_curve_retrievability: bool = False,
) -> RwkvStatsSearchScoreResult | None:
    start = time.monotonic()
    if not _reviewer_backend_accepts_review_inputs(
        state_token.backend if state_token is not None else None
    ):
        return None

    input_build = _rwkv_review_input_batches_for_search(
        reviewer=reviewer,
        search=search,
        include_suspended_review=True,
    )
    if input_build is None:
        return None
    if prepare_instant_due or prepare_curve_due:
        input_build = _resolve_dynamic_desired_retentions_for_input_build(
            reviewer,
            input_build,
        )

    scores: list[tuple[int, float]] = []
    curve_scores: list[tuple[int, float]] = []
    fully_predicted_card_ids: set[int] = set()
    curve_due_card_ids: set[int] = set()
    queue_score_cache = _fresh_rwkv_review_queue_score_map(reviewer)
    queue_score_hits = 0
    score_start = time.monotonic()

    if prepare_curve_due or prepare_curve_retrievability:
        curve_input_build = _rwkv_curve_enabled_input_build(reviewer, input_build)
        for inputs_by_card_id in curve_input_build.inputs_by_batch_size.values():
            for batch in _chunks(
                inputs_by_card_id,
                _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
            ):
                predictions = _rwkv_review_predictions_for_inputs(
                    batch,
                    batch_size=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
                    state_token=state_token,
                )
                if predictions is None:
                    return None
                for (card_id, review_input), prediction in zip(
                    batch,
                    predictions,
                    strict=True,
                ):
                    retrievability = (
                        prediction.retrievability if prediction is not None else None
                    )
                    if not _valid_probability(retrievability):
                        continue
                    scores.append((card_id, retrievability))
                    fully_predicted_card_ids.add(card_id)
                    curve_retrievability = (
                        prediction.curve_retrievability
                        if prediction is not None
                        else None
                    )
                    if _valid_probability(curve_retrievability):
                        curve_scores.append((card_id, curve_retrievability))
                    elapsed_days = review_input.current_elapsed_days
                    current_interval = prediction.current_interval
                    if (
                        review_input.card_type == CARD_TYPE_REV
                        and review_input.card_queue == QUEUE_TYPE_REV
                        and isinstance(elapsed_days, int)
                        and isinstance(current_interval, int)
                        and elapsed_days >= current_interval
                    ):
                        curve_due_card_ids.add(card_id)

    for batch_size, inputs_by_card_id in input_build.inputs_by_batch_size.items():
        inputs_by_card_id = [
            item
            for item in inputs_by_card_id
            if item[0] not in fully_predicted_card_ids
        ]
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
            state_token=state_token,
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
    prepare_due = prepare_instant_due or prepare_curve_due
    return RwkvStatsSearchScoreResult(
        scores=scores,
        curve_scores=curve_scores,
        input_build=input_build,
        target_retentions_by_card_id=(
            _rwkv_review_input_build_target_retentions_by_card_id(input_build)
            if prepare_due
            else {}
        ),
        intervening_reviews_by_card_id=(
            _rwkv_filtered_deck_intervening_reviews_by_card_id(
                reviewer,
                _rwkv_review_input_build_inputs(input_build),
            )
            if prepare_instant_due
            else {}
        ),
        curve_due_card_ids=frozenset(curve_due_card_ids),
    )


def _rwkv_filtered_deck_intervening_reviews_by_card_id(
    reviewer: object,
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
) -> dict[int, int]:
    card_ids_by_deck_id: dict[int, set[int]] = {}
    for card_id, review_input in inputs_by_card_id:
        deck_id = review_input.identity.deck_id
        if isinstance(deck_id, int):
            card_ids_by_deck_id.setdefault(deck_id, set()).add(card_id)

    intervening_reviews_by_card_id: dict[int, int] = {}
    for deck_id, card_ids in card_ids_by_deck_id.items():
        intervening_reviews_by_card_id.update(
            (card_id, intervening_reviews)
            for card_id, intervening_reviews in _queue_intervening_reviews_by_card_id(
                reviewer,
                deck_id,
            ).items()
            if card_id in card_ids
        )
    return intervening_reviews_by_card_id


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


def _rwkv_review_queue_score_map_for_deck(
    reviewer: object,
    deck_id: int,
) -> dict[int, float] | None:
    _ensure_rwkv_review_collection_scope(reviewer)
    scores = _rwkv_review_queue_score_maps.get(deck_id)
    if scores is None:
        return None

    if _rwkv_review_queue_score_config_keys.get(
        deck_id
    ) != _rwkv_review_queue_score_config_key(reviewer, deck_id):
        return None

    return scores


def _rwkv_review_queue_target_map_for_deck(
    reviewer: object,
    deck_id: int,
) -> dict[int, float] | None:
    _ensure_rwkv_review_collection_scope(reviewer)
    targets = _rwkv_review_queue_target_maps.get(deck_id)
    if targets is None:
        return None

    if _rwkv_review_queue_score_config_keys.get(
        deck_id
    ) != _rwkv_review_queue_score_config_key(reviewer, deck_id):
        return None

    return targets


def _fresh_rwkv_review_queue_score_map(reviewer: object) -> dict[int, float]:
    with _reviewer_backend_state_lock:
        _ensure_rwkv_review_collection_scope(reviewer)
        state_generation = _reviewer_backend_state_generation()
        scores: dict[int, float] = {}
        for deck_id, deck_scores in _rwkv_review_queue_score_maps.items():
            if _rwkv_review_queue_score_generations.get(
                deck_id
            ) == state_generation and _rwkv_review_queue_score_config_keys.get(
                deck_id
            ) == _rwkv_review_queue_score_config_key(reviewer, deck_id):
                scores.update(deck_scores)
        return scores


def _rwkv_review_reschedule_card_ids(
    mw: object,
    *,
    deck_id: int | None = None,
) -> list[int]:
    if deck_id is not None:
        return _review_card_ids_in_deck_tree(SimpleNamespace(mw=mw), deck_id)

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


def _rwkv_review_reschedule_items_for_deck(
    reviewer: object,
    deck_id: int,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[RwkvReviewRescheduleItem] | None:
    if not _reviewer_backend_accepts_review_inputs(
        state_token.backend if state_token is not None else None
    ):
        return None

    _report_rwkv_state_cache_progress(
        progress,
        "Finding RWKV review cards...",
    )
    input_build = _rwkv_review_input_batches_for_deck_review_queue(
        reviewer=reviewer,
        deck_id=deck_id,
        batch_size_override=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
        include_new_cards=False,
    )
    if input_build is None:
        return None

    input_build = _rwkv_curve_enabled_input_build(reviewer, input_build)
    input_build = _resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        input_build,
    )
    total_inputs = sum(
        len(inputs) for inputs in input_build.inputs_by_batch_size.values()
    )
    if not total_inputs:
        logger.debug(
            "RWKV review reschedule deck inputs loaded: deck_id=%s searched=%s "
            "loaded=%s with_state=%s enabled=%s items=0",
            deck_id,
            input_build.searched_rows,
            input_build.parsed_cards,
            input_build.cards_with_state,
            input_build.eligible_cards,
        )
        return []

    items: list[RwkvReviewRescheduleItem] = []
    processed_inputs = 0
    start = time.monotonic()
    for inputs_by_card_id in input_build.inputs_by_batch_size.values():
        for batch in _chunks(
            inputs_by_card_id,
            _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
        ):
            if state_token is None:
                predictions = _rwkv_review_predictions_for_inputs(
                    batch,
                    batch_size=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
                )
            else:
                predictions = _rwkv_review_predictions_for_inputs(
                    batch,
                    batch_size=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
                    state_token=state_token,
                )
            if predictions is None:
                return None

            items.extend(
                _rwkv_review_reschedule_items_from_input_predictions(
                    batch,
                    predictions,
                )
            )
            processed_inputs += len(batch)
            _report_rwkv_state_cache_progress(
                progress,
                "Predicting RWKV reschedule intervals...",
                processed_inputs,
                total_inputs,
            )

    logger.debug(
        "RWKV review reschedule deck inputs predicted: deck_id=%s searched=%s "
        "loaded=%s with_state=%s enabled=%s inputs=%s items=%s deck_configs=%s "
        "batch_size=%s load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f "
        "elapsed_ms=%.1f",
        deck_id,
        input_build.searched_rows,
        input_build.parsed_cards,
        input_build.cards_with_state,
        input_build.eligible_cards,
        total_inputs,
        len(items),
        input_build.deck_configs,
        _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return items


def _rwkv_review_reschedule_items(
    reviewer: object,
    card_ids: Sequence[int],
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[RwkvReviewRescheduleItem] | None:
    timing = _timing_today(reviewer)
    if not isinstance(getattr(timing, "days_elapsed", None), int):
        return []

    if _reviewer_backend_accepts_review_inputs(
        state_token.backend if state_token is not None else None
    ):
        input_build = _rwkv_review_input_batches_for_ids(
            reviewer=reviewer,
            card_ids=card_ids,
            timing=timing,
            reason="RWKV reschedule",
            include_suspended_review=False,
            supported_state_filter=True,
            batch_size_override=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
            use_enabled_deck_filter=True,
        )
        if input_build is not None:
            input_build = _rwkv_curve_enabled_input_build(reviewer, input_build)
            input_build = _resolve_dynamic_desired_retentions_for_input_build(
                reviewer,
                input_build,
            )
            return _rwkv_review_reschedule_items_from_input_build(
                input_build,
                progress=progress,
                state_token=state_token,
            )

    items: list[RwkvReviewRescheduleItem] = []
    processed_cards = 0
    total_cards = len(card_ids)
    for chunk in _chunks(list(card_ids), 5000):
        deck_configs: dict[int, dict[str, object] | None] = {}
        candidates: list[RwkvReviewCandidate] = []
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

        for batch in _chunks(candidates, _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE):
            predictions = _predict_review_batch(
                batch,
                state_token=state_token,
            )
            if (
                state_token is not None
                and not _reviewer_backend_prediction_state_token_is_current(state_token)
            ):
                return None
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
        "RWKV review reschedule items built: cards=%s items=%s batch_size=%s",
        len(card_ids),
        len(items),
        _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
    )
    return items


def _rwkv_curve_enabled_input_build(
    reviewer: object,
    input_build: RwkvReviewInputBatchBuild,
) -> RwkvReviewInputBatchBuild:
    curve_enabled_by_deck_id: dict[int | None, bool] = {}
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}

    for batch_size, inputs in input_build.inputs_by_batch_size.items():
        enabled_inputs: list[tuple[int, RwkvReviewInput]] = []
        for card_id, review_input in inputs:
            deck_id = review_input.identity.deck_id
            if deck_id not in curve_enabled_by_deck_id:
                deck_config = _deck_config_for_deck_id(reviewer, deck_id)
                curve_enabled_by_deck_id[deck_id] = isinstance(
                    deck_config, dict
                ) and _rwkv_review_config_enabled(deck_config)
            if curve_enabled_by_deck_id[deck_id]:
                enabled_inputs.append((card_id, review_input))
        if enabled_inputs:
            inputs_by_batch_size[batch_size] = enabled_inputs

    eligible_cards = sum(len(inputs) for inputs in inputs_by_batch_size.values())
    removed_cards = max(0, input_build.eligible_cards - eligible_cards)
    return replace(
        input_build,
        inputs_by_batch_size=inputs_by_batch_size,
        disabled_config_cards=input_build.disabled_config_cards + removed_cards,
        eligible_cards=eligible_cards,
    )


def _rwkv_review_reschedule_items_from_input_predictions(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    predictions: Sequence[RwkvReviewPrediction | None],
) -> list[RwkvReviewRescheduleItem]:
    if len(predictions) != len(inputs_by_card_id):
        raise ValueError("RWKV batch prediction count mismatch")

    items: list[RwkvReviewRescheduleItem] = []
    for (card_id, review_input), prediction in zip(
        inputs_by_card_id,
        predictions,
        strict=True,
    ):
        if prediction is None:
            continue

        try:
            _validate_prediction(prediction)
        except ValueError:
            logger.debug("invalid RWKV reschedule prediction", exc_info=True)
            continue

        elapsed_days = review_input.current_elapsed_days
        if not isinstance(elapsed_days, int) or isinstance(elapsed_days, bool):
            continue
        if prediction.current_interval is None or prediction.current_s90 is None:
            continue

        items.append(
            RwkvReviewRescheduleItem(
                card_id=card_id,
                interval_days=prediction.current_interval,
                elapsed_days=elapsed_days,
                s90=prediction.current_s90,
                target_retention=_rwkv_review_input_target_retention(review_input),
            )
        )

    return items


def _rwkv_review_reschedule_items_from_input_build(
    input_build: RwkvReviewInputBatchBuild,
    *,
    progress: RwkvStateCacheProgressCallback | None = None,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[RwkvReviewRescheduleItem] | None:
    total_inputs = sum(
        len(inputs) for inputs in input_build.inputs_by_batch_size.values()
    )
    if not total_inputs:
        return []

    items: list[RwkvReviewRescheduleItem] = []
    processed_inputs = 0
    start = time.monotonic()
    for inputs_by_card_id in input_build.inputs_by_batch_size.values():
        for batch in _chunks(
            inputs_by_card_id,
            _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
        ):
            if state_token is None:
                predictions = _rwkv_review_predictions_for_inputs(
                    batch,
                    batch_size=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
                )
            else:
                predictions = _rwkv_review_predictions_for_inputs(
                    batch,
                    batch_size=_RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
                    state_token=state_token,
                )
            if predictions is None:
                return None if state_token is not None else []

            items.extend(
                _rwkv_review_reschedule_items_from_input_predictions(
                    batch,
                    predictions,
                )
            )
            processed_inputs += len(batch)
            _report_rwkv_state_cache_progress(
                progress,
                "Predicting RWKV reschedule intervals...",
                processed_inputs,
                total_inputs,
            )

    logger.debug(
        "RWKV review reschedule inputs predicted: loaded=%s with_state=%s "
        "enabled=%s inputs=%s items=%s deck_configs=%s batch_size=%s "
        "load_elapsed_ms=%.1f candidate_elapsed_ms=%.1f elapsed_ms=%.1f",
        input_build.parsed_cards,
        input_build.cards_with_state,
        input_build.eligible_cards,
        total_inputs,
        len(items),
        input_build.deck_configs,
        _RWKV_REVIEW_RESCHEDULE_BATCH_SIZE,
        input_build.load_elapsed_ms,
        input_build.candidate_elapsed_ms,
        (time.monotonic() - start) * 1000,
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
    *,
    collection_backend: object | None = None,
) -> object:
    from anki.collection import OpChangesWithCount

    if collection_backend is None:
        col = getattr(mw, "col", None)
        collection_backend = getattr(col, "_backend", None)
    apply_raw = getattr(
        collection_backend,
        "apply_rwkv_review_reschedule_raw",
        None,
    )
    if not callable(apply_raw):
        raise ValueError("RWKV reschedule backend API is unavailable")

    request = scheduler_pb2.RwkvReviewRescheduleRequest()
    for item in items:
        request_item = request.items.add(
            card_id=item.card_id,
            interval_days=item.interval_days,
            elapsed_days=item.elapsed_days,
            s90=float(item.s90),
        )
        if _valid_probability(item.target_retention):
            request_item.target_retention = item.target_retention

    response = OpChangesWithCount()
    response.ParseFromString(apply_raw(request.SerializeToString()))
    return response


def _apply_rwkv_review_reschedule_if_current(
    mw: object,
    items: Sequence[RwkvReviewRescheduleItem],
    *,
    state_token: _ReviewerBackendPredictionStateToken,
) -> object | None:
    """Atomically validate prediction state before applying reschedule output."""

    if mw is not state_token.collection_owner or state_token.collection_backend is None:
        return None
    with _try_reviewer_backend_prediction_access(
        expected_state_token=state_token,
    ) as backend:
        if backend is None:
            return None
        with _reviewer_backend_state_lock:
            if not _reviewer_backend_prediction_access_is_current(
                backend,
                expected_state_token=state_token,
            ):
                return None
            return _apply_rwkv_review_reschedule(
                mw,
                items,
                collection_backend=state_token.collection_backend,
            )


def _rwkv_review_scores_for_candidates(
    candidates: Sequence[RwkvReviewCandidate],
    *,
    batch_size: int,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[tuple[int, float]]:
    with _try_reviewer_backend_prediction_access(
        expected_state_token=state_token,
    ) as backend:
        if backend is None:
            logger.debug("RWKV candidate scoring skipped: backend busy")
            if state_token is not None:
                _raise_reviewer_backend_prediction_unavailable(state_token)
            return []
        state_generation = _reviewer_backend_state_generation(backend)
        scores = _rwkv_review_scores_for_candidates_with_backend(
            candidates,
            batch_size=batch_size,
            backend=backend,
        )
        if _reviewer_backend_prediction_access_is_current(
            backend,
            expected_state_generation=state_generation,
            expected_state_token=state_token,
        ):
            return scores
        if state_token is not None:
            raise _ReviewerBackendPredictionAborted
        return []


def _rwkv_review_scores_for_candidates_with_backend(
    candidates: Sequence[RwkvReviewCandidate],
    *,
    batch_size: int,
    backend: RwkvReviewerBackend,
) -> list[tuple[int, float]]:
    start = time.monotonic()
    cached = _cached_review_predictions_for_candidates(candidates, backend=backend)
    if cached is None:
        return _rwkv_review_scores_for_candidates_without_cache_split(
            candidates,
            batch_size=batch_size,
            start=start,
            backend=backend,
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
            [request for _, request in batch_requests_by_index],
            backend=backend,
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
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[tuple[int, float]] | None:
    with _try_reviewer_backend_prediction_access(
        expected_state_token=state_token,
    ) as backend:
        if backend is None:
            logger.debug("RWKV input scoring skipped: backend busy")
            if state_token is not None:
                _raise_reviewer_backend_prediction_unavailable(state_token)
            return None
        state_generation = _reviewer_backend_state_generation(backend)
        scores = _rwkv_review_scores_for_inputs_with_backend(
            inputs_by_card_id,
            batch_size=batch_size,
            backend=backend,
        )
        if _reviewer_backend_prediction_access_is_current(
            backend,
            expected_state_generation=state_generation,
            expected_state_token=state_token,
        ):
            return scores
        if state_token is not None:
            raise _ReviewerBackendPredictionAborted
        return None


def _rwkv_review_scores_for_inputs_with_backend(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    *,
    batch_size: int,
    backend: RwkvReviewerBackend,
) -> list[tuple[int, float]] | None:
    start = time.monotonic()
    resident_predictions = _resident_retrievability_predictions_for_inputs(
        inputs_by_card_id,
        backend=backend,
    )
    if resident_predictions is not None:
        scores = _scores_from_input_predictions(
            inputs_by_card_id,
            resident_predictions,
        )
        logger.debug(
            "RWKV review inputs scored from resident state: inputs=%s scored=%s "
            "elapsed_ms=%.1f",
            len(inputs_by_card_id),
            len(scores),
            (time.monotonic() - start) * 1000,
        )
        return scores

    cached = _cached_review_input_predictions_for_inputs(
        [
            (index, review_input)
            for index, (_, review_input) in enumerate(inputs_by_card_id)
        ],
        backend=backend,
    )
    if cached is None:
        return None

    predictions, requests_by_index, cache_hits = cached
    if not requests_by_index:
        scores = _scores_from_input_predictions(
            inputs_by_card_id,
            predictions,
        )
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
            [request for _, request in batch_requests_by_index],
            backend=backend,
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

    scores = _scores_from_input_predictions(
        inputs_by_card_id,
        predictions,
    )
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


def _resident_retrievability_predictions_for_inputs(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> Sequence[RwkvReviewPrediction | None] | None:
    if backend is None:
        backend = _reviewer_backend
    predict = getattr(
        backend,
        "predict_retrievability_inputs_from_warm_up",
        None,
    )
    if not callable(predict):
        return None
    return cast(
        Sequence[RwkvReviewPrediction | None] | None,
        predict(
            [
                (index, review_input)
                for index, (_, review_input) in enumerate(inputs_by_card_id)
            ]
        ),
    )


def _cached_retrievability_inputs_from_warm_up(
    inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> (
    tuple[
        list[RwkvReviewPrediction | None],
        list[tuple[int, RwkvReviewInput]],
        int,
    ]
    | None
):
    if backend is None:
        backend = _reviewer_backend
    cached = getattr(
        backend,
        "cached_retrievability_inputs_from_warm_up",
        None,
    )
    if not callable(cached):
        return None
    return cast(
        tuple[
            list[RwkvReviewPrediction | None],
            list[tuple[int, RwkvReviewInput]],
            int,
        ]
        | None,
        cached(inputs_by_index),
    )


def _predict_retrievability_inputs_from_warm_up_uncached(
    review_inputs: Sequence[RwkvReviewInput],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> Sequence[RwkvReviewPrediction | None]:
    if backend is None:
        backend = _reviewer_backend
    predict = getattr(
        backend,
        "predict_retrievability_inputs_from_warm_up_uncached",
        None,
    )
    if not callable(predict):
        raise ValueError("RWKV resident retrievability prediction is unavailable")
    return cast(Sequence[RwkvReviewPrediction | None], predict(review_inputs))


def _rwkv_review_predictions_for_inputs(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    *,
    batch_size: int,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> list[RwkvReviewPrediction | None] | None:
    with _try_reviewer_backend_prediction_access(
        expected_state_token=state_token,
    ) as backend:
        if backend is None:
            logger.debug("RWKV input prediction skipped: backend busy")
            if state_token is not None:
                _raise_reviewer_backend_prediction_unavailable(state_token)
            return None
        state_generation = _reviewer_backend_state_generation(backend)
        predictions = _rwkv_review_predictions_for_inputs_with_backend(
            inputs_by_card_id,
            batch_size=batch_size,
            backend=backend,
        )
        if _reviewer_backend_prediction_access_is_current(
            backend,
            expected_state_generation=state_generation,
            expected_state_token=state_token,
        ):
            return predictions
        if state_token is not None:
            raise _ReviewerBackendPredictionAborted
        return None


def _rwkv_review_predictions_for_inputs_with_backend(
    inputs_by_card_id: Sequence[tuple[int, RwkvReviewInput]],
    *,
    batch_size: int,
    backend: RwkvReviewerBackend,
) -> list[RwkvReviewPrediction | None] | None:
    start = time.monotonic()
    cached = _cached_review_input_predictions_for_inputs(
        [
            (index, review_input)
            for index, (_, review_input) in enumerate(inputs_by_card_id)
        ],
        backend=backend,
    )
    if cached is None:
        return None

    predictions, requests_by_index, cache_hits = cached
    if not requests_by_index:
        logger.debug(
            "RWKV review inputs predicted from cache: inputs=%s cache_hits=%s "
            "elapsed_ms=%.1f",
            len(inputs_by_card_id),
            cache_hits,
            (time.monotonic() - start) * 1000,
        )
        return predictions

    predict_start = time.monotonic()
    for missing_offset in range(0, len(requests_by_index), batch_size):
        batch_requests_by_index = requests_by_index[
            missing_offset : missing_offset + batch_size
        ]
        batch_start = time.monotonic()
        logger.debug(
            "RWKV review input full prediction batch started: missing_offset=%s "
            "size=%s batch_size=%s cache_hits=%s",
            missing_offset,
            len(batch_requests_by_index),
            batch_size,
            cache_hits,
        )
        batch_predictions = _predict_review_requests(
            [request for _, request in batch_requests_by_index],
            backend=backend,
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
            "RWKV review input full prediction batch processed: missing_offset=%s "
            "size=%s batch_size=%s predict_elapsed_ms=%.1f elapsed_ms=%.1f",
            missing_offset,
            len(batch_requests_by_index),
            batch_size,
            batch_predict_elapsed_ms,
            (time.monotonic() - batch_start) * 1000,
        )

    logger.debug(
        "RWKV review inputs predicted: inputs=%s cache_hits=%s runtime_requests=%s "
        "batch_size=%s predict_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(inputs_by_card_id),
        cache_hits,
        len(requests_by_index),
        batch_size,
        (time.monotonic() - predict_start) * 1000,
        (time.monotonic() - start) * 1000,
    )
    return predictions


def _cached_review_input_predictions_for_inputs(
    inputs_by_index: Sequence[tuple[int, RwkvReviewInput]],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> RwkvCachedReviewPredictions | None:
    if backend is None:
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


def _reviewer_backend_accepts_review_inputs(
    backend: RwkvReviewerBackend | None = None,
) -> bool:
    if backend is None:
        backend = _reviewer_backend
    return callable(
        getattr(
            backend,
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
        return _AUTO_RWKV_RETRIEVABILITY_BATCH_SIZE
    return batch_size


def _rwkv_review_scores_for_candidates_without_cache_split(
    candidates: Sequence[RwkvReviewCandidate],
    *,
    batch_size: int,
    start: float,
    backend: RwkvReviewerBackend,
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
        predictions = _predict_review_batch_with_backend(batch, backend)
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
    *,
    backend: RwkvReviewerBackend | None = None,
) -> RwkvCachedReviewPredictions | None:
    if backend is None:
        backend = _reviewer_backend
    cached_review_predictions = getattr(backend, "cached_review_predictions", None)
    if not callable(cached_review_predictions):
        return None

    return cast(RwkvCachedReviewPredictions, cached_review_predictions(candidates))


def _predict_review_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> Sequence[RwkvReviewPrediction | None]:
    if backend is None:
        backend = _reviewer_backend
    predict_review_requests = getattr(backend, "predict_review_requests", None)
    if not callable(predict_review_requests):
        raise ValueError("RWKV backend does not support request batch prediction")

    return cast(
        Sequence[RwkvReviewPrediction | None], predict_review_requests(requests)
    )


def _predict_retrievability_requests(
    requests: Sequence[RwkvReviewPredictionRequest],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> Sequence[RwkvReviewPrediction | None]:
    if backend is None:
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

    return _predict_review_requests(requests, backend=backend)


def _predict_retrievability_requests_uncached(
    requests: Sequence[RwkvReviewPredictionRequest],
    *,
    backend: RwkvReviewerBackend | None = None,
) -> Sequence[RwkvReviewPrediction | None]:
    if backend is None:
        backend = _reviewer_backend
    predict_retrievability_requests_uncached = getattr(
        backend,
        "predict_retrievability_requests_uncached",
        None,
    )
    if callable(predict_retrievability_requests_uncached):
        return cast(
            Sequence[RwkvReviewPrediction | None],
            predict_retrievability_requests_uncached(requests),
        )

    return _predict_retrievability_requests(requests, backend=backend)


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
    include_new_cards: bool = False,
    batch_size_override: int | None = None,
    use_enabled_deck_filter: bool = True,
) -> RwkvReviewInputBatchBuild | None:
    backend_build = _rwkv_review_input_batches_from_backend_for_ids(
        reviewer=reviewer,
        card_ids=card_ids,
        include_suspended_review=include_suspended_review,
        include_new_cards=include_new_cards,
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
                and _rwkv_review_config_active(deck_config)
                else None
            )

        deck_config = deck_configs[deck_id]
        if deck_config is None:
            disabled_config_cards += 1
            continue
        if _rwkv_review_first_review_elapsed_from_card_creation(deck_config):
            state_fields = _rwkv_state_fields_for_stats_graph_fields(
                fields,
                timing,
                include_suspended_review=include_suspended_review,
                first_review_elapsed_from_card_creation=True,
            )
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
    include_new_cards: bool,
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
        include_new_cards=include_new_cards,
    )
    if response is None:
        return None

    return _rwkv_review_input_batch_build_from_backend_response(
        reviewer=reviewer,
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
    include_new_cards: bool = False,
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
        include_new_cards=include_new_cards,
    )
    if response is None:
        return None

    return _rwkv_review_input_batch_build_from_backend_response(
        reviewer=reviewer,
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
    batch_size_override: int | None,
    include_new_cards: bool,
) -> RwkvReviewInputBatchBuild | None:
    cache_key = _rwkv_review_input_batch_cache_key(
        reviewer=reviewer,
        deck_id=deck_id,
        batch_size_override=batch_size_override,
        include_new_cards=include_new_cards,
    )
    if cache_key is not None:
        cached = _cached_rwkv_review_input_batch_build(reviewer, cache_key)
        if cached is not None:
            return cached
        cache = _rwkv_review_input_batch_cache(reviewer)
        if cache:
            logger.debug(
                "RWKV review input batch cache miss: deck_id=%s "
                "batch_size=%s cache_size=%s stored_keys=%s",
                deck_id,
                batch_size_override,
                len(cache),
                [(k[0], k[1]) for k in cache],
            )

    col = _collection(reviewer)
    backend = getattr(col, "_backend", None)
    if backend is None:
        return None

    load_start = time.monotonic()
    response = _rwkv_review_input_rows_for_deck_review_queue_backend_response(
        backend,
        deck_id=deck_id,
        include_new_cards=include_new_cards,
    )
    if response is None:
        return None

    input_build = _rwkv_review_input_batch_build_from_backend_response(
        reviewer=reviewer,
        response=response,
        batch_size_override=batch_size_override,
        load_start=load_start,
        source_label="deck_review_queue_cards",
        source_size=_rwkv_backend_uint(response, "searched_cards"),
    )
    input_build = _resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        input_build,
    )
    if cache_key is not None:
        _cache_rwkv_review_input_batch_build(reviewer, cache_key, input_build)
    return input_build


def _rwkv_review_input_batch_cache_key(
    *,
    reviewer: object,
    deck_id: int,
    batch_size_override: int | None,
    include_new_cards: bool,
) -> RwkvReviewInputBatchCacheKey | None:
    collection_key = _ensure_rwkv_review_collection_scope(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if (
        collection_key is None
        or not isinstance(days_elapsed, int)
        or not isinstance(next_day_at, int)
    ):
        return None

    return (
        deck_id,
        batch_size_override,
        include_new_cards,
        days_elapsed,
        next_day_at,
        collection_key,
        _rwkv_review_queue_configuration_key(reviewer),
        _rwkv_review_deck_scope_key(reviewer, deck_id),
        _dynamic_desired_retention_generation,
        _rwkv_study_queue_generation,
    )


def _rwkv_first_review_elapsed_state_cache_key(
    reviewer: object,
) -> RwkvFirstReviewElapsedStateCacheKey:
    return tuple(
        (item[0], bool(item[1]))
        for item in _rwkv_first_review_elapsed_config_key(reviewer)
    )


def _rwkv_review_queue_score_config_key(
    reviewer: object,
    deck_id: int,
) -> RwkvReviewQueueScoreConfigKey:
    collection_key = _ensure_rwkv_review_collection_scope(reviewer)
    timing = _timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", -1)
    next_day_at = getattr(timing, "next_day_at", -1)
    return (
        collection_key or (0, 0),
        days_elapsed if isinstance(days_elapsed, int) else -1,
        next_day_at if isinstance(next_day_at, int) else -1,
        _rwkv_review_queue_configuration_key(reviewer),
        _rwkv_review_deck_scope_key(reviewer, deck_id),
        _dynamic_desired_retention_generation,
        _rwkv_study_queue_generation,
    )


def _rwkv_review_queue_score_config_key_from_context(
    context: RwkvReviewQueueContext,
) -> RwkvReviewQueueScoreConfigKey:
    return (
        context.collection_key,
        context.days_elapsed,
        context.next_day_at,
        context.config_key,
        context.deck_scope,
        context.dynamic_desired_retention_generation,
        context.study_queue_generation,
    )


_rwkv_review_input_batch_module_cache: OrderedDict[
    RwkvReviewInputBatchCacheKey, RwkvReviewInputBatchBuild
] = OrderedDict()


def _rwkv_review_input_batch_cache(
    reviewer: object,
) -> OrderedDict[RwkvReviewInputBatchCacheKey, RwkvReviewInputBatchBuild]:
    _ensure_rwkv_review_collection_scope(reviewer)
    return _rwkv_review_input_batch_module_cache


def _rwkv_review_card_ids_in_deck_scope(
    reviewer: object,
    card_ids: Sequence[int],
    deck_ids: Sequence[int],
) -> set[int] | None:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    list_rows = getattr(db, "list", None)
    if not callable(list_rows):
        return None

    try:
        rows = list_rows(
            f"select id from cards where id in {ids2str(card_ids)} "
            f"and did in {ids2str(deck_ids)}"
        )
    except Exception:
        logger.debug(
            "failed to filter answered RWKV cards by deck scope",
            exc_info=True,
        )
        return None

    return {card_id for value in rows if (card_id := _valid_card_id(value)) is not None}


def _cached_rwkv_review_input_batch_build(
    reviewer: object,
    cache_key: RwkvReviewInputBatchCacheKey,
) -> RwkvReviewInputBatchBuild | None:
    cache = _rwkv_review_input_batch_cache(reviewer)
    cached = cache.get(cache_key)
    if cached is None:
        return None

    cache.move_to_end(cache_key)

    session_answered_ids = tuple(_session_answered_ids(reviewer))
    processed_answered_ids = cached.session_answered_ids
    if session_answered_ids[: len(processed_answered_ids)] == processed_answered_ids:
        newly_answered_ids = session_answered_ids[len(processed_answered_ids) :]
    else:
        newly_answered_ids = session_answered_ids

    refresh_ids = list(dict.fromkeys(newly_answered_ids))
    if not refresh_ids:
        if session_answered_ids != processed_answered_ids:
            cached = replace(
                cached,
                session_answered_ids=session_answered_ids,
            )
            cache[cache_key] = cached
            cache.move_to_end(cache_key)
        input_count = sum(
            len(inputs) for inputs in cached.inputs_by_batch_size.values()
        )
        logger.debug(
            "RWKV review input backend rows reused from cache: deck_id=%s rows=%s "
            "eligible=%s inputs=%s refreshed=0 excluded=0",
            cache_key[0],
            cached.loaded_rows,
            input_count,
            input_count,
        )
        return replace(
            cached,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )

    refreshed_build = _rwkv_review_input_batches_from_backend_for_ids(
        reviewer=reviewer,
        card_ids=refresh_ids,
        include_suspended_review=False,
        include_new_cards=cache_key[2],
        batch_size_override=cache_key[1],
        use_enabled_deck_filter=True,
    )
    if refreshed_build is None:
        logger.debug(
            "RWKV review input cache refresh deferred after backend failure: "
            "deck_id=%s refresh_ids=%s",
            cache_key[0],
            len(refresh_ids),
        )
        return replace(
            cached,
            preset_elapsed_ms=0.0,
            load_elapsed_ms=0.0,
            candidate_elapsed_ms=0.0,
        )

    cached_input_card_ids = {
        card_id
        for inputs in cached.inputs_by_batch_size.values()
        for card_id, _ in inputs
    }
    deck_scope = set(cache_key[7])
    scoped_refresh_ids = _rwkv_review_card_ids_in_deck_scope(
        reviewer,
        refresh_ids,
        cache_key[7],
    )
    refreshed_inputs_by_batch_size: dict[
        int,
        list[tuple[int, RwkvReviewInput]],
    ] = {}
    for batch_size, inputs in refreshed_build.inputs_by_batch_size.items():
        scoped_inputs = [
            (card_id, review_input)
            for card_id, review_input in inputs
            if (
                card_id in scoped_refresh_ids
                if scoped_refresh_ids is not None
                else card_id in cached_input_card_ids
                or review_input.identity.deck_id in deck_scope
            )
        ]
        if scoped_inputs:
            refreshed_inputs_by_batch_size[batch_size] = scoped_inputs
    refreshed_build = replace(
        refreshed_build,
        inputs_by_batch_size=refreshed_inputs_by_batch_size,
        eligible_cards=sum(
            len(inputs) for inputs in refreshed_inputs_by_batch_size.values()
        ),
    )
    refreshed_build = _resolve_dynamic_desired_retentions_for_input_build(
        reviewer,
        refreshed_build,
    )
    refresh_id_set = set(refresh_ids)
    merged_inputs_by_batch_size = {
        batch_size: [
            (card_id, review_input)
            for card_id, review_input in inputs
            if card_id not in refresh_id_set
        ]
        for batch_size, inputs in cached.inputs_by_batch_size.items()
    }
    merged_inputs_by_batch_size = {
        key: inputs for key, inputs in merged_inputs_by_batch_size.items() if inputs
    }
    refreshed_input_count = 0
    for batch_size, inputs in refreshed_build.inputs_by_batch_size.items():
        if not inputs:
            continue
        merged_inputs_by_batch_size.setdefault(batch_size, []).extend(inputs)
        refreshed_input_count += len(inputs)

    input_count = sum(len(inputs) for inputs in merged_inputs_by_batch_size.values())
    updated = replace(
        cached,
        inputs_by_batch_size=merged_inputs_by_batch_size,
        eligible_cards=input_count,
        preset_elapsed_ms=0.0,
        load_elapsed_ms=0.0,
        candidate_elapsed_ms=0.0,
        session_answered_ids=session_answered_ids,
    )
    cache[cache_key] = updated
    cache.move_to_end(cache_key)
    logger.debug(
        "RWKV review input backend rows reused from cache: deck_id=%s rows=%s "
        "eligible=%s inputs=%s refreshed=%s excluded=%s",
        cache_key[0],
        cached.loaded_rows,
        input_count,
        input_count,
        refreshed_input_count,
        len(refresh_ids) - refreshed_input_count,
    )
    return updated


def _cache_rwkv_review_input_batch_build(
    reviewer: object,
    cache_key: RwkvReviewInputBatchCacheKey,
    input_build: RwkvReviewInputBatchBuild,
) -> None:
    cache = _rwkv_review_input_batch_cache(reviewer)
    cache[cache_key] = replace(
        input_build,
        session_answered_ids=(),
    )
    cache.move_to_end(cache_key)
    while len(cache) > _RWKV_REVIEW_INPUT_BATCH_CACHE_LIMIT:
        cache.popitem(last=False)


def _clear_rwkv_review_input_batch_cache(reviewer: object) -> None:
    if _rwkv_review_input_batch_module_cache:
        _rwkv_review_input_batch_module_cache.clear()


def _rwkv_review_input_batch_build_from_backend_response(
    *,
    reviewer: object,
    response: object,
    batch_size_override: int | None,
    load_start: float,
    source_label: str,
    source_size: int,
) -> RwkvReviewInputBatchBuild:
    if isinstance(response, scheduler_pb2.RwkvReviewInputRowsForCardsResponse):
        return _rwkv_review_input_batch_build_from_backend_proto_response(
            reviewer=reviewer,
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
    reviewer: object,
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
    include_new_cards: bool,
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
                include_new_cards=include_new_cards,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug(
                "failed to load RWKV review input rows from backend",
                exc_info=True,
            )
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_cards", None)
    if not callable(get_rows):
        return None

    try:
        return get_rows(
            card_ids=card_ids,
            include_suspended_review=include_suspended_review,
            include_disabled_decks=False,
            include_new_cards=include_new_cards,
        )
    except Exception:
        logger.debug(
            "failed to load RWKV review input rows from backend",
            exc_info=True,
        )
        return None


def _rwkv_review_input_rows_for_search_backend_response(
    backend: object,
    *,
    search: str,
    include_suspended_review: bool,
    include_new_cards: bool = False,
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
                include_new_cards=include_new_cards,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug(
                "failed to load RWKV review input rows for search from backend",
                exc_info=True,
            )
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_search", None)
    if not callable(get_rows):
        return None

    try:
        kwargs: dict[str, object] = dict(
            search=search,
            include_suspended_review=include_suspended_review,
            include_disabled_decks=False,
        )
        if include_new_cards:
            kwargs["include_new_cards"] = True
        return get_rows(**kwargs)
    except Exception:
        logger.debug(
            "failed to load RWKV review input rows for search from backend",
            exc_info=True,
        )
        return None


def _rwkv_review_input_rows_for_deck_review_queue_backend_response(
    backend: object,
    *,
    deck_id: int,
    include_new_cards: bool,
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
                include_new_cards=include_new_cards,
            )
            raw = get_rows_raw(request.SerializeToString())
            response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse()
            response.ParseFromString(raw)
            return response
        except Exception:
            logger.debug(
                "failed to load RWKV review input rows for deck review queue from backend",
                exc_info=True,
            )
            return None

    get_rows = getattr(backend, "rwkv_review_input_rows_for_deck_review_queue", None)
    if not callable(get_rows):
        return None

    try:
        return get_rows(
            deck_id=deck_id,
            include_disabled_decks=False,
            include_new_cards=include_new_cards,
        )
    except Exception:
        logger.debug(
            "failed to load RWKV review input rows for deck review queue from backend",
            exc_info=True,
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
    state_kind = _rwkv_backend_non_empty_str(row, "current_state_kind")
    normal_state_kind = _rwkv_backend_non_empty_str(
        row,
        "current_normal_state_kind",
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
        card_type=_rwkv_review_state_for_scheduling_state(
            state_kind=state_kind,
            normal_state_kind=normal_state_kind,
            card_type=_rwkv_backend_int(row, "card_type"),
        ),
        card_queue=_rwkv_backend_int(row, "card_queue"),
        card_due=_rwkv_backend_int(row, "card_due"),
        interval_days=_rwkv_backend_int(row, "interval_days"),
        ease_factor=_rwkv_backend_int(row, "ease_factor"),
        reps=_rwkv_backend_int(row, "reps"),
        lapses=_rwkv_backend_int(row, "lapses"),
        day_offset=_rwkv_backend_int(row, "day_offset"),
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
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
        enforce_grade_order=_rwkv_backend_bool(
            row,
            "enforce_grade_order",
            True,
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
    state_kind = row.current_state_kind or None
    normal_state_kind = row.current_normal_state_kind or None

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
        card_type=_rwkv_review_state_for_scheduling_state(
            state_kind=state_kind,
            normal_state_kind=normal_state_kind,
            card_type=row.card_type,
        ),
        card_queue=row.card_queue,
        card_due=row.card_due,
        interval_days=row.interval_days,
        ease_factor=row.ease_factor,
        reps=row.reps,
        lapses=row.lapses,
        day_offset=row.day_offset,
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
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
        enforce_grade_order=_rwkv_backend_bool(
            row,
            "enforce_grade_order",
            True,
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


def _rwkv_backend_bool(row: object, name: str, default: bool) -> bool:
    has_field = getattr(row, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(name):
                return default
        except ValueError:
            pass
    value = getattr(row, name, None)
    return value if isinstance(value, bool) else default


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
select cards.id, cards.nid, did, odid, type, queue, due, odue, ivl, factor, reps, lapses, cards.data
from cards
where cards.id in {ids2str(card_ids)}
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
    (type = {int(CARD_TYPE_NEW)} and queue = {int(QUEUE_TYPE_NEW)})
    or
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
    return isinstance(deck_config, dict) and _rwkv_review_config_active(deck_config)


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
  and {_rwkv_historical_answer_sql_condition()}
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

    if card.type == int(CARD_TYPE_NEW) and card.queue == int(QUEUE_TYPE_NEW):
        states.current.normal.new.SetInParent()
        return states

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
            first_review_elapsed_from_card_creation=(
                _rwkv_review_first_review_elapsed_from_card_creation(deck_config)
            ),
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
        card_type=_rwkv_review_state_for_scheduling_state(
            state_kind=cast(str | None, state_kind),
            normal_state_kind=normal_state_kind,
            card_type=card.type,
        ),
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
        enforce_grade_order=_rwkv_review_enforce_grade_order_config(deck_config),
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
            first_review_elapsed_from_card_creation=(
                _rwkv_review_first_review_elapsed_from_card_creation(deck_config)
            ),
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
        card_type=_rwkv_review_state_for_scheduling_state(
            state_kind=cast(str | None, state_kind),
            normal_state_kind=normal_state_kind,
            card_type=fields.type,
        ),
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
        enforce_grade_order=_rwkv_review_enforce_grade_order_config(deck_config),
    )


_UNSUPPORTED_RWKV_STATE = object()


def _rwkv_state_fields_for_stats_graph_card(
    card: RwkvStatsGraphCard,
    timing: object,
    *,
    include_suspended_review: bool,
    first_review_elapsed_from_card_creation: bool = False,
) -> tuple[object, str | None, int | None, int | None]:
    return _rwkv_state_fields_for_stats_graph_values(
        card_id=card.id,
        card_type=card.type,
        queue=card.queue,
        last_review_time=card.last_review_time,
        timing=timing,
        include_suspended_review=include_suspended_review,
        first_review_elapsed_from_card_creation=(
            first_review_elapsed_from_card_creation
        ),
    )


def _rwkv_state_fields_for_stats_graph_fields(
    fields: RwkvStatsGraphCardFields,
    timing: object,
    *,
    include_suspended_review: bool,
    first_review_elapsed_from_card_creation: bool = False,
) -> tuple[object, str | None, int | None, int | None]:
    return _rwkv_state_fields_for_stats_graph_values(
        card_id=fields.id,
        card_type=fields.type,
        queue=fields.queue,
        last_review_time=fields.last_review_time,
        timing=timing,
        include_suspended_review=include_suspended_review,
        first_review_elapsed_from_card_creation=(
            first_review_elapsed_from_card_creation
        ),
    )


def _rwkv_state_fields_for_stats_graph_values(
    *,
    card_id: int,
    card_type: int,
    queue: int,
    last_review_time: int | None,
    timing: object,
    include_suspended_review: bool,
    first_review_elapsed_from_card_creation: bool,
) -> tuple[object, str | None, int | None, int | None]:
    if card_type == int(CARD_TYPE_NEW) and queue == int(QUEUE_TYPE_NEW):
        elapsed_seconds = (
            _elapsed_seconds_since_card_created_for_timing(
                timing,
                card_id,
            )
            if first_review_elapsed_from_card_creation
            else None
        )
        if first_review_elapsed_from_card_creation and elapsed_seconds is None:
            return None, None, None, None
        elapsed_days = (
            elapsed_seconds // 86_400 if elapsed_seconds is not None else None
        )
        return "normal", "new", elapsed_days, elapsed_seconds

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
        elapsed_seconds = _stats_graph_elapsed_seconds_for_review_time(last_review_time)
        if elapsed_days is None or elapsed_seconds is None:
            return None, None, None, None
        return "normal", "review", elapsed_days, elapsed_seconds

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
    return _elapsed_seconds_since_review_time(last_review_time)


def _elapsed_since_card_last_review(
    reviewer: object,
    card: object,
) -> tuple[int | None, int | None]:
    last_review_time = getattr(card, "last_review_time", None)
    if not isinstance(last_review_time, int) or isinstance(last_review_time, bool):
        card_id = _card_id(card)
        previous = (
            _latest_eligible_review_for_card(reviewer, card_id)
            if card_id is not None
            else None
        )
        last_review_time = previous[0] // 1000 if previous is not None else None

    return (
        _stats_graph_elapsed_days_for_review_time(
            last_review_time,
            _timing_today(reviewer),
        ),
        _elapsed_seconds_since_review_time(last_review_time),
    )


def _elapsed_seconds_since_review_time(last_review_time: object) -> int | None:
    if not isinstance(last_review_time, int) or isinstance(last_review_time, bool):
        return None

    now = int(time.time())
    return max(0, now - last_review_time)


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
    *,
    state_token: _ReviewerBackendPredictionStateToken | None = None,
) -> Sequence[RwkvReviewPrediction | None]:
    backend = state_token.backend if state_token is not None else _reviewer_backend
    if backend is None:
        if state_token is not None:
            raise _ReviewerBackendPredictionAborted
        return [None] * len(candidates)

    with _try_reviewer_backend_prediction_access(
        expected_backend=backend,
        expected_state_token=state_token,
    ) as current_backend:
        if current_backend is None:
            logger.debug("RWKV review batch prediction skipped: backend busy")
            if state_token is not None:
                _raise_reviewer_backend_prediction_unavailable(state_token)
            return [None] * len(candidates)
        state_generation = _reviewer_backend_state_generation(current_backend)
        predictions = _predict_review_batch_with_backend(candidates, current_backend)
        if _reviewer_backend_prediction_access_is_current(
            current_backend,
            expected_state_generation=state_generation,
            expected_state_token=state_token,
        ):
            return predictions
        if state_token is not None:
            raise _ReviewerBackendPredictionAborted
        return [None] * len(candidates)


def _predict_review_batch_with_backend(
    candidates: Sequence[RwkvReviewCandidate],
    backend: RwkvReviewerBackend,
) -> Sequence[RwkvReviewPrediction | None]:
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
        backend.predict_review(
            reviewer=candidate.reviewer,
            card=candidate.card,
        )
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
    target_retentions_by_card_id: Mapping[int, float] | None = None,
    fresh_for_backend_state: bool = True,
    collection_backend: object | None = None,
    collection: object | None = None,
    collection_owner: object | None = None,
    score_config_key: RwkvReviewQueueScoreConfigKey | None = None,
    is_current: Callable[[], bool] | None = None,
) -> bool:
    if collection is None:
        collection = _collection(reviewer)
    scoped_reviewer = _reviewer_scoped_to_collection(reviewer, collection)
    backend = (
        collection_backend
        if collection_backend is not None
        else getattr(collection, "_backend", None)
    )
    request = _rwkv_score_request(
        scoped_reviewer,
        deck_id,
        scores,
        target_retentions_by_card_id=target_retentions_by_card_id,
    )
    if scores and score_config_key is None:
        score_config_key = _rwkv_review_queue_score_config_key(
            scoped_reviewer,
            deck_id,
        )
    if not _rwkv_collection_identity_is_current(
        collection_owner=collection_owner,
        collection=collection,
        collection_backend=backend,
    ):
        return False

    target_retentions_by_card_id = target_retentions_by_card_id or {}
    with _reviewer_backend_state_lock:
        if (
            not _rwkv_collection_identity_is_current(
                collection_owner=collection_owner,
                collection=collection,
                collection_backend=backend,
            )
            or is_current is not None
            and not is_current()
        ):
            return False
        set_scores_raw = getattr(backend, "set_rwkv_review_queue_scores_raw", None)
        if callable(set_scores_raw):
            set_scores_raw(request.SerializeToString())
        else:
            set_scores = getattr(backend, "set_rwkv_review_queue_scores", None)
            if not callable(set_scores):
                return False

            set_scores(
                deck_id=deck_id,
                scores=list(request.scores),
            )
        if (
            not _rwkv_collection_identity_is_current(
                collection_owner=collection_owner,
                collection=collection,
                collection_backend=backend,
            )
            or is_current is not None
            and not is_current()
        ):
            return False
        _ensure_rwkv_review_collection_scope(scoped_reviewer)
        _clear_rwkv_review_queue_score_cache()
        if scores:
            assert score_config_key is not None
            _rwkv_review_queue_score_maps[deck_id] = {
                card_id: retrievability for card_id, retrievability in scores
            }
            targets_for_scores = {
                card_id: target_retention
                for card_id, _ in scores
                if (target_retention := target_retentions_by_card_id.get(card_id))
                is not None
                and _valid_probability(target_retention)
            }
            if targets_for_scores:
                _rwkv_review_queue_target_maps[deck_id] = targets_for_scores
            _rwkv_review_queue_score_config_keys[deck_id] = score_config_key
            if fresh_for_backend_state:
                _rwkv_review_queue_score_generations[deck_id] = (
                    _reviewer_backend_state_generation()
                )
    return True


def _patch_answered_card_rwkv_review_queue_score(
    reviewer: object,
    deck_id: int,
    card_id: int,
    retrievability: float | None,
    *,
    target_retention: float | None,
) -> bool:
    backend = getattr(_collection(reviewer), "_backend", None)
    request_type = getattr(
        scheduler_pb2,
        "RwkvAnsweredCardQueueScorePatchRequest",
        None,
    )
    if request_type is None:
        return False

    request = request_type(deck_id=deck_id, card_id=card_id)
    if retrievability is not None:
        score_request = _rwkv_score_request(
            reviewer,
            deck_id,
            [(card_id, retrievability)],
            target_retentions_by_card_id=(
                {card_id: target_retention} if target_retention is not None else None
            ),
        )
        request.score.CopyFrom(score_request.scores[0])

    patch_raw = getattr(
        backend,
        "patch_answered_card_rwkv_review_queue_score_raw",
        None,
    )
    if callable(patch_raw):
        patch_raw(request.SerializeToString())
    else:
        patch = getattr(
            backend,
            "patch_answered_card_rwkv_review_queue_score",
            None,
        )
        if not callable(patch):
            return False
        patch(
            deck_id=deck_id,
            card_id=card_id,
            score=request.score if request.HasField("score") else None,
        )

    scores = _rwkv_review_queue_score_maps.get(deck_id)
    if scores is not None:
        if retrievability is None:
            scores.pop(card_id, None)
        else:
            scores[card_id] = retrievability
        if not scores:
            _rwkv_review_queue_score_maps.pop(deck_id, None)
            _rwkv_review_queue_score_config_keys.pop(deck_id, None)

    targets = _rwkv_review_queue_target_maps.get(deck_id)
    if target_retention is None:
        if targets is not None:
            targets.pop(card_id, None)
            if not targets:
                _rwkv_review_queue_target_maps.pop(deck_id, None)
    else:
        _rwkv_review_queue_target_maps.setdefault(deck_id, {})[card_id] = (
            target_retention
        )
    _rwkv_review_queue_score_generations.pop(deck_id, None)
    return True


def _rwkv_score_request(
    reviewer: object,
    deck_id: int,
    scores: Sequence[tuple[int, float]],
    *,
    target_retentions_by_card_id: Mapping[int, float] | None = None,
) -> scheduler_pb2.RwkvReviewQueueScoresRequest:
    request = scheduler_pb2.RwkvReviewQueueScoresRequest(deck_id=deck_id)
    intervening_reviews_by_card_id = (
        _queue_intervening_reviews_by_card_id(reviewer, deck_id) if scores else {}
    )
    target_retentions_by_card_id = target_retentions_by_card_id or {}
    for card_id, retrievability in scores:
        score = request.scores.add(card_id=card_id, retrievability=retrievability)
        intervening_reviews = intervening_reviews_by_card_id.get(card_id)
        if intervening_reviews is not None:
            score.intervening_reviews = intervening_reviews
        target_retention = target_retentions_by_card_id.get(card_id)
        if _valid_probability(target_retention):
            score.target_retention = target_retention
    return request


def _set_rwkv_deck_count_scores(
    reviewer: object,
    deck_id: int,
    scores: Sequence[tuple[int, float]],
    *,
    target_retentions_by_card_id: Mapping[int, float] | None = None,
    collection_backend: object | None = None,
    collection: object | None = None,
    collection_owner: object | None = None,
    is_current: Callable[[], bool] | None = None,
) -> bool:
    if collection is None:
        collection = _collection(reviewer)
    scoped_reviewer = _reviewer_scoped_to_collection(reviewer, collection)
    backend = (
        collection_backend
        if collection_backend is not None
        else getattr(collection, "_backend", None)
    )
    request = _rwkv_score_request(
        scoped_reviewer,
        deck_id,
        scores,
        target_retentions_by_card_id=target_retentions_by_card_id,
    )
    if not _rwkv_collection_identity_is_current(
        collection_owner=collection_owner,
        collection=collection,
        collection_backend=backend,
    ):
        return False

    with _reviewer_backend_state_lock:
        if (
            not _rwkv_collection_identity_is_current(
                collection_owner=collection_owner,
                collection=collection,
                collection_backend=backend,
            )
            or is_current is not None
            and not is_current()
        ):
            return False
        set_scores_raw = getattr(backend, "set_rwkv_deck_count_scores_raw", None)
        if callable(set_scores_raw):
            set_scores_raw(request.SerializeToString())
        else:
            set_scores = getattr(backend, "set_rwkv_deck_count_scores", None)
            if not callable(set_scores):
                return False
            set_scores(
                deck_id=deck_id,
                scores=list(request.scores),
            )
        return _rwkv_collection_identity_is_current(
            collection_owner=collection_owner,
            collection=collection,
            collection_backend=backend,
        ) and (is_current is None or is_current())


def _queue_intervening_reviews_by_card_id(
    reviewer: object,
    deck_id: int,
) -> dict[int, int]:
    deck_config = _deck_config_for_deck_id(reviewer, deck_id)
    if not isinstance(deck_config, dict):
        return {}

    min_intervening_reviews = _rwkv_review_min_intervening_reviews(deck_config)
    if min_intervening_reviews <= 0:
        return {}

    intervening_reviews = {
        card_id: reviews
        for card_id, reviews in _session_intervening_reviews_by_card_id(
            reviewer,
            max_intervening_reviews=min_intervening_reviews - 1,
        ).items()
        if reviews < min_intervening_reviews
    }
    intervening_reviews.update(
        _revlog_intervening_reviews_by_card_id(
            reviewer,
            deck_id,
            min_intervening_reviews,
        )
    )
    return intervening_reviews


def _revlog_intervening_reviews_by_card_id(
    reviewer: object,
    deck_id: int,
    review_count: int,
) -> dict[int, int]:
    col = _collection(reviewer)
    db = getattr(col, "db", None)
    all_rows = getattr(db, "all", None)
    if not callable(all_rows) or review_count <= 0:
        return {}

    deck_ids = _deck_tree_ids(reviewer, deck_id)
    effective_deck_sql = "(case when c.odid != 0 then c.odid else c.did end)"
    deck_clause = f"and {effective_deck_sql} in {ids2str(deck_ids)}" if deck_ids else ""
    try:
        rows = all_rows(
            f"""
select r.cid
from revlog r
join cards c on c.id = r.cid
where {_rwkv_historical_answer_sql_condition("r")}
  {deck_clause}
order by r.id desc, r.cid desc
limit ?
""",
            review_count,
        )
    except Exception:
        logger.debug(
            "failed to read recent RWKV review cards: deck_id=%s review_count=%s",
            deck_id,
            review_count,
        )
        return {}

    intervening_reviews: dict[int, int] = {}
    for index, row in enumerate(rows):
        card_id = _valid_card_id(row[0] if isinstance(row, Sequence) else row)
        if card_id is not None and card_id not in intervening_reviews:
            intervening_reviews[card_id] = index
    return intervening_reviews


def _session_intervening_reviews_by_card_id(
    reviewer: object,
    *,
    max_intervening_reviews: int | None = None,
) -> dict[int, int]:
    answered_ids = _session_answered_ids(
        reviewer,
        max_items=(
            max_intervening_reviews + 1 if max_intervening_reviews is not None else None
        ),
    )
    if not answered_ids:
        return {}

    last_answer_index_by_card_id: dict[int, int] = {}
    for index, card_id in enumerate(answered_ids):
        last_answer_index_by_card_id[card_id] = index

    answered_count = len(answered_ids)
    return {
        card_id: max(0, answered_count - answer_index - 1)
        for card_id, answer_index in last_answer_index_by_card_id.items()
    }


def _session_answered_ids(
    reviewer: object,
    *,
    max_items: int | None = None,
) -> list[int]:
    answered_ids = getattr(reviewer, "_answeredIds", None)
    if not isinstance(answered_ids, list):
        mw = getattr(reviewer, "mw", None)
        active_reviewer = getattr(mw, "reviewer", None)
        answered_ids = getattr(active_reviewer, "_answeredIds", None)

    if not isinstance(answered_ids, list):
        return []

    if max_items is not None:
        if max_items <= 0:
            return []
        answered_ids = answered_ids[-max_items:]

    return [
        card_id
        for value in answered_ids
        if (card_id := _valid_card_id(value)) is not None
    ]


def _set_rwkv_stats_graph_scores(
    reviewer: object,
    search: str,
    scores: Sequence[tuple[int, float]],
    *,
    target_retentions_by_card_id: Mapping[int, float] | None = None,
    intervening_reviews_by_card_id: Mapping[int, int] | None = None,
    curve_due_card_ids: AbstractSet[int] = frozenset(),
    curve_retrievabilities_by_card_id: Mapping[int, float] | None = None,
    collection_backend: object | None = None,
) -> None:
    if collection_backend is None:
        mw = getattr(reviewer, "mw", None)
        col = getattr(mw, "col", None)
        collection_backend = getattr(col, "_backend", None)
    set_scores = getattr(
        collection_backend,
        "set_rwkv_stats_graph_scores",
        None,
    )
    if not callable(set_scores):
        return

    target_retentions_by_card_id = target_retentions_by_card_id or {}
    intervening_reviews_by_card_id = intervening_reviews_by_card_id or {}
    curve_retrievabilities_by_card_id = curve_retrievabilities_by_card_id or {}
    score_messages: list[scheduler_pb2.RwkvStatsGraphScoresRequest.Score] = []
    for card_id, retrievability in scores:
        score = scheduler_pb2.RwkvStatsGraphScoresRequest.Score(
            card_id=card_id,
            retrievability=retrievability,
        )
        target_retention = target_retentions_by_card_id.get(card_id)
        if _valid_probability(target_retention):
            score.target_retention = target_retention
        intervening_reviews = intervening_reviews_by_card_id.get(card_id)
        if isinstance(intervening_reviews, int) and intervening_reviews >= 0:
            score.intervening_reviews = intervening_reviews
        if card_id in curve_due_card_ids:
            score.curve_due = True
        curve_retrievability = curve_retrievabilities_by_card_id.get(card_id)
        if _valid_probability(curve_retrievability):
            score.curve_retrievability = curve_retrievability
        score_messages.append(score)

    set_scores(
        search=search,
        scores=score_messages,
    )


def _set_rwkv_stats_graph_scores_if_current(
    reviewer: object,
    search: str,
    scores: Sequence[tuple[int, float]],
    *,
    state_token: _ReviewerBackendPredictionStateToken,
    target_retentions_by_card_id: Mapping[int, float] | None = None,
    intervening_reviews_by_card_id: Mapping[int, int] | None = None,
    curve_due_card_ids: AbstractSet[int] = frozenset(),
    curve_retrievabilities_by_card_id: Mapping[int, float] | None = None,
) -> bool:
    """Publish stats only while the complete prediction state is unchanged."""

    if (
        getattr(reviewer, "mw", None) is not state_token.collection_owner
        or state_token.collection_backend is None
    ):
        return False
    with _try_reviewer_backend_prediction_access(
        expected_state_token=state_token,
    ) as backend:
        if backend is None:
            return False
        with _reviewer_backend_state_lock:
            if not _reviewer_backend_prediction_access_is_current(
                backend,
                expected_state_token=state_token,
            ) or not _rwkv_review_queue_context_epochs_are_current(state_token):
                return False
            _set_rwkv_stats_graph_scores(
                reviewer,
                search,
                scores,
                target_retentions_by_card_id=target_retentions_by_card_id,
                intervening_reviews_by_card_id=intervening_reviews_by_card_id,
                curve_due_card_ids=curve_due_card_ids,
                curve_retrievabilities_by_card_id=(curve_retrievabilities_by_card_id),
                collection_backend=state_token.collection_backend,
            )
        return True


def _set_rwkv_card_info_score(
    reviewer: object,
    card_id: int,
    retrievability: float | None,
    curve_retrievability: float | None = None,
    *,
    collection_backend: object | None = None,
) -> None:
    backend = (
        collection_backend
        if collection_backend is not None
        else getattr(_collection(reviewer), "_backend", None)
    )
    set_score = getattr(backend, "set_rwkv_card_info_score", None)
    if not callable(set_score):
        return

    request = scheduler_pb2.RwkvCardInfoScoreRequest(card_id=card_id)
    if retrievability is not None:
        request.retrievability = retrievability
    if curve_retrievability is not None:
        request.curve_retrievability = curve_retrievability
    set_score(request)


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


def _elapsed_seconds_since_card_created(
    reviewer: object,
    card: object,
) -> int | None:
    card_id = _int_attr(card, "id")
    if card_id is None:
        return None

    timing = _timing_today(reviewer)
    return _elapsed_seconds_since_card_created_for_timing(timing, card_id)


def _elapsed_seconds_since_card_created_for_timing(
    timing: object,
    card_id: int,
) -> int | None:
    now = getattr(timing, "now", None)
    now_secs = now if isinstance(now, int) else int(time.time())
    return max(0, now_secs - card_id // 1000)


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
