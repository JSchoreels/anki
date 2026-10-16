# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3, from our RWKV forecast add-on (anki-addons/rwkv_forecast/engine.py): projected RWKV recall for every
review card at future times.

All calls into the fork's private RWKV code live here (aqt.rwkv_scheduler of the JSchoreels
FSRS-7 fork, 26.09.3+fsrs7 build 100).
The fork scores a card with a *query* input that carries the time since its last review
(current_elapsed_seconds / _days) and today's day number (day_offset). A forecast is the same
query with the clock moved forward: the model's state is not touched, so every number means
"if you stop reviewing now". Read-only: nothing here writes cards.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import aqt.rwkv_scheduler as rs

# same search as the experiment (queue = review): learning cards follow their steps, not RWKV
REVIEW_SEARCH = "is:review -is:learn -is:suspended -is:buried"
WARMUP_TIMEOUT_SECS = 120


class Busy(Exception):
    """The RWKV backend is warming up or busy (e.g. right after an answer); try again soon."""


@dataclass
class Forecast:
    taken: float  # time.time() when computed
    offsets: list[int]  # seconds from `taken`; offsets[0] == 0
    labels: list[str]
    cards: dict[int, dict] = field(default_factory=dict)  # cid -> {did, target, r: [R per offset]}
    elapsed_ms: float = 0.0
    phases: dict = field(default_factory=dict)  # ms per step, for tuning

    def index(self, label: str) -> int:
        return self.labels.index(label)


def _reviewer(mw):
    return getattr(mw, "reviewer", None) or SimpleNamespace(mw=mw)


def _shift(inp, secs: int, days: int):
    """The query `secs` later; `days` = day rollovers crossed (elapsed days and day number move)."""
    if not secs:
        return inp
    es, ed, do = inp.current_elapsed_seconds, inp.current_elapsed_days, inp.day_offset
    return replace(
        inp,
        current_elapsed_seconds=None if es is None else es + secs,
        current_elapsed_days=None if ed is None else ed + days,
        day_offset=None if do is None else do + days,
    )


def compute(mw, offsets: list[tuple[str, int | None]], search: str = REVIEW_SEARCH) -> Forecast:
    """Run in a background thread. `offsets` = [(label, seconds from now)], first one ("now", 0);
    seconds None = the next day rollover (Anki's "tomorrow")."""
    start = time.monotonic()
    mark = [start]
    phases = {}

    def lap(name):
        now = time.monotonic()
        phases[name] = phases.get(name, 0) + (now - mark[0]) * 1000
        mark[0] = now

    reviewer = _reviewer(mw)
    if rs._reviewer_backend is None:
        rs.configure_reviewer_backend_from_environment()
    if rs._reviewer_backend is None:
        raise RuntimeError("RWKV is not available in this Anki")

    # same as a filtered-deck rebuild: use the warm state, or warm up (this thread) if nobody has
    warmed = rs._prepare_reviewer_backend_for_filtered_deck(reviewer)
    if not warmed and rs._reviewer_backend_warmup_pending(reviewer):
        if rs._wait_for_reviewer_backend_warmup(reviewer, timeout_secs=WARMUP_TIMEOUT_SECS):
            warmed = rs._prepare_reviewer_backend_for_filtered_deck(reviewer)
    if not warmed:
        raise Busy("RWKV warm-up")
    token = rs._capture_reviewer_backend_prediction_state_token(reviewer)
    if token is None:
        raise Busy("RWKV state")
    lap("warm")

    build = rs._rwkv_review_input_batches_for_search(
        reviewer=reviewer, search=search, include_suspended_review=False
    )
    if build is None:
        raise RuntimeError("RWKV inputs unavailable")
    # presets with dynamic desired retention get their real target here (same as is:rwkv:due)
    build = rs._resolve_dynamic_desired_retentions_for_input_build(reviewer, build)
    lap("inputs")

    taken = time.time()
    cutoff = mw.col.sched.day_cutoff  # next rollover, epoch seconds
    offsets = [(l, max(1, int(cutoff - taken)) if s is None else s) for l, s in offsets]
    fc = Forecast(taken=taken, offsets=[s for _, s in offsets], labels=[l for l, _ in offsets])
    for _, inp in rs._rwkv_review_input_build_inputs(build):
        cid = inp.identity.card_id
        fc.cards[cid] = dict(
            did=inp.identity.deck_id,
            target=rs._rwkv_review_input_target_retention(inp),
            r=[None] * len(offsets),
        )

    for i, (_, secs) in enumerate(offsets):
        at = taken + secs
        days = 0 if at < cutoff - 1 else 1 + int(max(0, at - cutoff) // 86400)
        for batch_size, items in build.inputs_by_batch_size.items():
            shifted = [(cid, _shift(inp, secs, days)) for cid, inp in items]
            for chunk in _chunks(shifted, rs._RWKV_REVIEW_RESCHEDULE_BATCH_SIZE):
                try:
                    scores = rs._rwkv_review_scores_for_inputs(
                        chunk, batch_size=batch_size, state_token=token
                    )
                except rs._ReviewerBackendPredictionAborted as e:  # incl. busy
                    raise Busy("RWKV state changed") from e
                if scores is None:
                    raise Busy("RWKV busy")
                for cid, r in scores:
                    fc.cards[cid]["r"][i] = float(r)
        lap("score")

    # cards the model could not score are left out (Anki doesn't count them either)
    fc.cards = {c: v for c, v in fc.cards.items() if v["r"][0] is not None and v["target"]}
    fc.elapsed_ms = (time.monotonic() - start) * 1000
    fc.phases = phases
    return fc


def _chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def offsets_for() -> list[tuple[str, int | None]]:
    """now, 1 h, 3 h and tomorrow (next rollover)."""
    return [("now", 0), ("1h", 3600), ("3h", 3 * 3600), ("tomorrow", None)]
