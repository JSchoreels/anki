# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Replay an ascending-R review pool from a copied collection backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from collections import deque
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace
from typing import Any

from anki import _rsbridge, config_pb2, scheduler_pb2
from anki._backend import RustBackend
from anki.collection import Collection
from anki.consts import CARD_TYPE_REV
from aqt import rwkv_scheduler as scheduler
from aqt.rwkv_history import NativeReviewInputs
from aqt.rwkv_model import RWKV_SMALL_INSTANT_MODEL
from aqt.rwkv_precision import RWKV_STATE_PRECISION_DEFAULT
from aqt.rwkv_srs_benchmark import _packed_warm_up_reviews

Review = scheduler_pb2.RwkvHistoricalReviewInputsResponse.Review


@dataclass
class Accuracy:
    no_eligible_pick: int = 0
    ranks: list[int] = field(default_factory=list)
    gaps: list[float] = field(default_factory=list)

    def observe(
        self,
        pick: int | None,
        ranks: dict[int, int],
        exact: dict[int, float],
    ) -> None:
        if not ranks:
            return
        if pick is None or pick not in ranks:
            self.no_eligible_pick += 1
            return
        self.ranks.append(ranks[pick])
        best = next(iter(ranks))
        self.gaps.append(100 * (exact[pick] - exact[best]))

    def summary(self) -> dict:
        ranks = sorted(self.ranks)
        decisions = len(ranks) + self.no_eligible_pick
        top_k = {}
        for k in range(1, 6):
            matches = sum(rank <= k for rank in ranks)
            top_k[k] = {
                "matches": matches,
                "percent": round(100 * matches / decisions, 2) if decisions else None,
            }
        return dict(
            decisions=decisions,
            top_k=top_k,
            no_eligible_pick=self.no_eligible_pick,
            p95_rank=ranks[math.ceil(0.95 * len(ranks)) - 1] if ranks else None,
            worst_rank=max(ranks, default=None),
            mean_gap_pp=fmean(self.gaps) if self.gaps else None,
            max_gap_pp=max(self.gaps, default=None),
        )


@dataclass
class PartialQueue:
    window: int
    baseline: bool
    scores: dict[int, float] = field(default_factory=dict)
    before_refresh: Accuracy = field(default_factory=Accuracy)
    after_refresh: Accuracy = field(default_factory=Accuracy)
    before_refresh_vs_full_precomputed: Accuracy = field(default_factory=Accuracy)
    best_in_window: int = 0
    queries: int = 0

    def update(
        self,
        exact: dict[int, float],
        refresh_round: int,
        blocked: set[int],
        target: float,
        ranks: dict[int, int],
    ) -> int | None:
        def next_review() -> int | None:
            return min(
                (
                    c
                    for c, r in self.scores.items()
                    if c in exact and c not in blocked and r <= target
                ),
                key=lambda c: (self.scores[c], c),
                default=None,
            )

        visible = next_review()
        if self.baseline:
            batch = sorted(self.scores, key=lambda c: (self.scores[c], c))[
                : self.window
            ]
        else:
            batch = scheduler._rwkv_review_candidate_refresh_card_ids(
                {"reviewOrder": scheduler._REVIEW_ORDER_RETRIEVABILITY_ASCENDING},
                self.scores,
                None,
                limit=self.window,
                refresh_round=refresh_round,
            )
        if visible is not None and visible not in batch:
            batch.append(visible)
        for card in batch:
            if card in exact:
                self.scores[card] = exact[card]
            else:
                self.scores.pop(card, None)
        self.queries += len(batch)
        if ranks:
            self.best_in_window += next(iter(ranks)) in batch
            self.before_refresh.observe(visible, ranks, exact)
            self.after_refresh.observe(next_review(), ranks, exact)
        return visible


@dataclass
class ReviewPool:
    inference: Any
    deck_id: int
    active: set[int]
    target: float
    allow_same_day: bool
    min_seconds: int
    recent: deque[int]
    last: dict[int, Review] = field(default_factory=dict)

    def apply(self, rows: NativeReviewInputs) -> None:
        self.inference.warm_up_reviews_packed(_packed_warm_up_reviews(rows), False)
        for row in rows.rows:
            if row.deck_id == self.deck_id:
                self.recent.append(row.card_id)
                if row.card_id in self.active:
                    if row.interval_days > 0:
                        self.last[row.card_id] = row
                    else:
                        self.last.pop(row.card_id, None)

    def scores(self, now: int, day: int) -> dict[int, float]:
        def query(row: Review) -> tuple:
            seconds = max(0, now // 1000 - row.review_id // 1000)
            return (
                row.card_id,
                row.note_id,
                row.deck_id,
                row.preset_id,
                True,
                None,
                None,
                CARD_TYPE_REV,
                day,
                day - row.day_offset,
                seconds,
                *([self.target] * 4),
                True,
            )

        values = self.inference.predict_retrievability_many_from_warm_up(
            [query(row) for row in self.last.values()]
        )
        if not all(0 <= value <= 1 for value in values):
            raise ValueError("invalid RWKV probability")
        return dict(zip(self.last, values, strict=True))

    def blocked(self, now: int, day: int) -> set[int]:
        return set(self.recent) | {
            card
            for card, prev in self.last.items()
            if now // 1000 - prev.review_id // 1000 < self.min_seconds
            or (not self.allow_same_day and prev.day_offset == day)
        }

    def rank(self, scores: dict[int, float], blocked: set[int]) -> dict[int, int]:
        ordered = sorted(
            (
                card
                for card, score in scores.items()
                if card in self.last and card not in blocked and score <= self.target
            ),
            key=lambda card: (scores[card], card),
        )
        return {card: rank for rank, card in enumerate(ordered, 1)}

    def simulated_answer(self, card: int, recorded: Review) -> Review:
        # Use the recorded grade, duration and answer time on the oracle's pick.
        # Again leaves the ordinary review pool for the rest of this session.
        previous = self.last[card]
        elapsed = recorded.day_offset - previous.day_offset
        return Review(
            review_id=recorded.review_id,
            card_id=card,
            note_id=previous.note_id,
            deck_id=previous.deck_id,
            preset_id=previous.preset_id,
            ease=recorded.ease,
            duration_millis=recorded.duration_millis,
            card_type=(
                scheduler.RwkvReviewState.REVIEW
                if elapsed
                else scheduler.RwkvReviewState.FILTERED
            ),
            day_offset=recorded.day_offset,
            elapsed_days=elapsed,
            elapsed_seconds=recorded.review_id // 1000 - previous.review_id // 1000,
            interval_days=0 if recorded.ease == 1 else previous.interval_days,
        )


def measure_day(
    pool: ReviewPool,
    rows: list[Review],
    queues: list[PartialQueue],
    ascending: bool,
    sizes: list[tuple[int, int]],
) -> int:
    last_answer = max(
        (r.review_id for r in rows if r.deck_id == pool.deck_id), default=None
    )
    refresh_round = 0
    winners = set()
    ranks: dict[int, int] | None = None
    last_full_update: dict[int, float] = {}
    for row in rows:
        if row.deck_id != pool.deck_id:
            pool.apply(NativeReviewInputs([row]))
            ranks = None
            continue
        if ranks is None:
            now = row.review_id - row.duration_millis
            scores = pool.scores(now, row.day_offset)
            ranks = pool.rank(scores, pool.blocked(now, row.day_offset))
            if not refresh_round:
                last_full_update = scores
                for queue in queues:
                    queue.scores = scores.copy()
        if ascending:
            if not ranks:
                break
            row = pool.simulated_answer(next(iter(ranks)), row)
        pool.apply(NativeReviewInputs([row]))
        if row.review_id == last_answer:
            continue
        refresh_round += 1
        exact = pool.scores(row.review_id, row.day_offset)
        blocked = pool.blocked(row.review_id, row.day_offset)
        ranks = pool.rank(exact, blocked)
        # With precomputation on in both cases, the full queue also selects
        # from its last completed update, using the same eligibility rules.
        full_precomputed_ranks = pool.rank(last_full_update, blocked)
        if ranks:
            winners.add(next(iter(ranks)))
        sizes.append((len(exact), len(ranks)))
        for queue in queues:
            visible = queue.update(exact, refresh_round, blocked, pool.target, ranks)
            queue.before_refresh_vs_full_precomputed.observe(
                visible, full_precomputed_ranks, last_full_update
            )
        last_full_update = exact
    return len(winners)


def replay(col: Collection, args: argparse.Namespace) -> dict:
    deck_id = col.decks.id_for_name(args.deck)
    if deck_id is None:
        raise ValueError(f"unknown deck {args.deck!r}")
    deck = col._backend.get_deck(deck_id).normal
    config = col._backend.get_deck_config(deck.config_id).config
    target = (
        deck.desired_retention
        if deck.HasField("desired_retention")
        else config.desired_retention
    )
    active = set(col.db.list("select id from cards where did=? and queue>=0", deck_id))
    history = scheduler._historical_rwkv_review_inputs(
        SimpleNamespace(mw=SimpleNamespace(col=col)), native_replay=True
    )
    if not isinstance(history.reviews, NativeReviewInputs) or not history.reviews:
        raise ValueError("backtest requires canonical native review history")
    rows = history.reviews.rows
    end = rows[-1].day_offset - args.skip_last_days
    start = end - args.days + 1
    model = scheduler._embedded_rwkv_model_file(args.model)
    if model is None:
        raise ValueError(f"missing bundled model {args.model!r}")
    inference = getattr(_rsbridge, "RwkvInference")(
        str(model), target, 36500, RWKV_STATE_PRECISION_DEFAULT, args.model
    )
    pool = ReviewPool(
        inference,
        deck_id,
        active,
        target,
        col._backend.get_config_bool(
            config_pb2.ConfigKey.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED
        ),
        config.rwkv_review_min_elapsed_secs,
        deque(maxlen=config.rwkv_review_min_intervening_reviews),
    )
    split = next(i for i, r in enumerate(rows) if r.day_offset >= start)
    for i in range(0, split, 20_000):
        pool.apply(history.reviews[i : min(i + 20_000, split)])
    queues = [
        PartialQueue(k, baseline) for k in args.windows for baseline in (True, False)
    ]
    sizes: list[tuple[int, int]] = []
    unique_best = []
    ascending = args.trajectory == "ascending"
    for day, group in groupby(rows[split:], lambda r: r.day_offset):
        if day > end:
            break
        daily = list(group)
        if ascending:
            snapshot = inference.warm_up_snapshot()
            last, recent = pool.last.copy(), pool.recent.copy()
        unique_best.append(measure_day(pool, daily, queues, ascending, sizes))
        if ascending:
            # Each day starts from the actual prior history, including shared model state.
            inference.restore_warm_up_snapshot(snapshot)
            inference.restore_cache_state(snapshot[-1])
            pool.last, pool.recent = last, recent
            pool.apply(NativeReviewInputs(daily))
    if not sizes:
        raise ValueError("no review decisions in this deck and day range")
    pools, eligible = zip(*sizes, strict=True)
    return dict(
        backup_sha256=hashlib.sha256(args.backup_copy.read_bytes()).hexdigest(),
        deck=args.deck,
        model=args.model,
        state_precision=RWKV_STATE_PRECISION_DEFAULT,
        days=[start, end],
        prefix_answers=split,
        target=target,
        min_intervening=pool.recent.maxlen,
        min_seconds=pool.min_seconds,
        same_day=pool.allow_same_day,
        trajectory=args.trajectory,
        distinct_best_per_day=unique_best,
        checkpoints=len(sizes),
        empty_exact=eligible.count(0),
        pool_range=[min(pools), max(pools)],
        eligible_range=[min(eligible), max(eligible)],
        results=[
            dict(
                window=q.window,
                policy="stale" if q.baseline else "current",
                best_in_window=q.best_in_window,
                mean_queries=q.queries / len(sizes),
                before_refresh=q.before_refresh.summary(),
                after_refresh=q.after_refresh.summary(),
                before_refresh_vs_full_precomputed=q.before_refresh_vs_full_precomputed.summary(),
            )
            for q in queues
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-copy", type=Path, required=True)
    parser.add_argument("--deck", required=True, help="full name of the review deck")
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--skip-last-days", type=int, default=0)
    parser.add_argument("--windows", type=int, nargs="+", default=[64, 512, 1024])
    parser.add_argument(
        "--trajectory", choices=["recorded", "ascending"], default="ascending"
    )
    parser.add_argument("--model", default=RWKV_SMALL_INSTANT_MODEL)
    args = parser.parse_args()
    if args.days < 1 or args.skip_last_days < 0 or min(args.windows) < 1:
        parser.error("use positive days and window sizes and a nonnegative skip")
    if args.backup_copy.suffix != ".colpkg":
        parser.error("supply a copied .colpkg backup")
    with tempfile.TemporaryDirectory(prefix="rwkv-candidate-backtest-") as directory:
        root = Path(directory)
        collection = root / "collection.anki2"
        RustBackend(langs=["en"]).import_collection_package(
            col_path=str(collection),
            backup_path=str(args.backup_copy.resolve(strict=True)),
            media_folder=str(root / "collection.media"),
            media_db=str(root / "collection.media.db2"),
        )
        col = Collection(str(collection))
        try:
            print(json.dumps(replay(col, args), indent=2))
        finally:
            col.close()
