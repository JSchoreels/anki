# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The RWKV-Instant "(≤N)" label shown beside review counts."""

from __future__ import annotations

from aqt.utils import tr


def rwkv_review_potential_label(
    review_count: int, headroom: int | None, waiting: int = 0
) -> tuple[str, str] | None:
    """Return the label and its tooltip, or None when the daily review limit
    leaves no headroom beyond `review_count`.

    `waiting` counts due reviews held back for spacing."""
    if not headroom:
        return None
    total = review_count + headroom
    title = tr.decks_rwkv_review_potential_tooltip(count=review_count, total=total)
    if waiting:
        title += " " + tr.decks_rwkv_review_waiting_tooltip(count=waiting)
    return f" (≤{total})", title
