# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Adapt native full-history preparation to the desktop replay interface.

Rust owns filtering, routing, elapsed times, and history identity. Keep the
desktop dataclasses here so replay and cache callers retain their existing API.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Set
from dataclasses import replace

from anki import scheduler_pb2
from anki.errors import Interrupted
from aqt import rwkv_scheduler as scheduler


def full_review_inputs(
    reviewer: object,
    build: Callable[
        [scheduler_pb2.RwkvHistoricalReviewInputsRequest],
        scheduler_pb2.RwkvHistoricalReviewInputsResponse,
    ],
    *,
    replay_key: str,
    first_review_elapsed_source: scheduler.RwkvFirstReviewElapsedSource,
    ignored_review_ids: Set[int],
    prepare_recovery_checkpoint: bool,
    progress: scheduler.RwkvStateCacheProgressCallback | None,
) -> scheduler.RwkvHistoricalReviewInputs | None:
    started = time.monotonic()
    scheduler._report_rwkv_review_input_prepare_progress(
        progress, processed=0, total=0, started_at=started
    )
    elapsed_source = scheduler.RwkvFirstReviewElapsedSource
    request = scheduler_pb2.RwkvHistoricalReviewInputsRequest(
        history=scheduler_pb2.RwkvHistoricalReviewFingerprintRequest(
            ignored_review_ids=sorted(ignored_review_ids),
            dynamic_preset_replay=(
                scheduler._rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
            ),
            stable_preset_ids=scheduler._rwkv_history_stable_preset_ids(reviewer),
            first_review_uses_creation_by_config_id=(
                scheduler._rwkv_first_review_uses_creation_by_config_id(reviewer)
            ),
        ),
        preserved_learning_start_cutoffs=scheduler._rwkv_preserved_learning_start_cutoffs(
            reviewer
        ),
        # An absent override selects each deck's policy; False explicitly
        # selects missing elapsed time for every first review.
        first_review_uses_creation=(
            None
            if first_review_elapsed_source == elapsed_source.DECK_CONFIG
            else first_review_elapsed_source == elapsed_source.CARD_CREATION
        ),
        recovery_checkpoint_max_age_millis=(
            scheduler._RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS
            if prepare_recovery_checkpoint
            else None
        ),
    )
    try:
        response = build(request)
    except Interrupted:
        raise
    except Exception:
        # Preparation is read-only, so retrying through the established Python
        # builder is safe when native preset resolution cannot handle the input.
        scheduler.logger.warning(
            "Rust RWKV full history preparation failed; using Python replay builder",
            exc_info=True,
        )
        return None
    native_elapsed_ms = (time.monotonic() - started) * 1000
    reviews = []
    review_ids = []
    for row in response.reviews:
        state_kind, normal_state_kind = scheduler._historical_review_state_kinds(
            row.review_kind
        )
        reviews.append(
            scheduler.RwkvReviewInput(
                identity=scheduler.RwkvReviewIdentity(
                    card_id=row.card_id,
                    note_id=row.note_id,
                    deck_id=row.deck_id,
                    preset_id=row.preset_id,
                ),
                is_query=False,
                ease=row.ease,
                duration_millis=row.duration_millis,
                card_type=row.card_type,
                card_queue=scheduler._historical_review_queue(row.review_kind),
                card_due=None,
                interval_days=row.interval_days,
                ease_factor=row.ease_factor,
                reps=None,
                lapses=None,
                day_offset=row.day_offset,
                current_state_kind=state_kind,
                current_normal_state_kind=normal_state_kind,
                current_elapsed_days=row.elapsed_days,
                current_elapsed_seconds=row.elapsed_seconds,
            )
        )
        review_ids.append(row.review_id)

    def history(
        metadata: scheduler_pb2.RwkvHistoricalReviewMetadata,
    ) -> scheduler.RwkvHistoricalReviewInputs:
        return scheduler.RwkvHistoricalReviewInputs(
            reviews=[],
            review_ids=[],
            previous_review_id_by_card=dict(metadata.previous_review_id_by_card),
            previous_interval_days_by_card=dict(
                metadata.previous_interval_days_by_card
            ),
            review_count_by_card=dict(metadata.review_count_by_card),
            last_review_id=metadata.identity.last_review_id,
            review_count=metadata.identity.review_count,
            history_hash=metadata.identity.history_hash,
            replay_key=replay_key,
            ignored_review_ids=tuple(response.active_ignored_review_ids),
        )

    result = replace(history(response.metadata), reviews=reviews, review_ids=review_ids)
    if response.HasField("recovery_checkpoint"):
        # A checkpoint stores prefix metadata; its review rows are already in
        # result.reviews and must not be copied into a second replay history.
        checkpoint = history(response.recovery_checkpoint)
        result.prepared_checkpoint_histories[checkpoint.review_count] = checkpoint
    scheduler._report_rwkv_review_input_prepare_progress(
        progress, processed=len(reviews), total=len(reviews), started_at=started
    )
    scheduler.logger.debug(
        "RWKV full historical review inputs built in Rust: reviews=%s "
        "native_elapsed_ms=%.1f elapsed_ms=%.1f",
        len(reviews),
        native_elapsed_ms,
        (time.monotonic() - started) * 1000,
    )
    return result
