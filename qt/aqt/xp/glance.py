# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the statistics page's "At a glance" and "Player" boxes (the phone's XpGlance): due today, due tomorrow,
studied today and the 30-day retention against the deck's target, for the page's deck; then level, XP today,
streak and daily goal for all decks and devices. Inserted above the page's first section; styled by pages.css."""

from __future__ import annotations

import json
from html import escape

from anki.collection import Collection
from anki.decks import DeckId

from .game import Game, level, level_start

DAY = 86_400


def _cell(value: str, label: str, note: str = "", state: str = "") -> str:
    small = f"<small>{escape(note)}</small>" if note else ""
    return f'<div class="xp-glance-cell {state}"><b>{escape(value)}</b><span>{escape(label)}</span>{small}</div>'


def glance_html(col: Collection, deck_id: int) -> str:
    did = DeckId(deck_id)
    ids = ",".join(str(i) for i in col.decks.deck_and_child_ids(did))
    node = col.sched.deck_due_tree(did)
    due = (node.new_count + node.learn_count + node.review_count) if node else 0
    cutoff = col.sched.day_cutoff
    today = col.sched.today
    tomorrow = col.db.scalar(
        f"select count() from cards where did in ({ids}) and ((queue in (2, 3) and due = ?) or (queue = 1 and due >= ? and due < ?))",
        today + 1,
        cutoff,
        cutoff + DAY,
    )
    reps, ms = col.db.first(
        f"select count(), coalesce(sum(time), 0) from revlog where id > ? and type not in (4, 5) and cid in (select id from cards where did in ({ids}))",
        (cutoff - DAY) * 1000,
    )
    # Anki's own retention: each card's first review of the day with an interval of a day or more
    eases = col.db.list(
        f"""select ease from revlog where id in (select min(id) from revlog where id > ? and ease > 0
        and (lastIvl >= 1 or lastIvl <= -86400) and cid in (select id from cards where did in ({ids}))
        group by cid, (id / 1000 - ?) / 86400)""",
        (cutoff - 30 * DAY) * 1000,
        cutoff,
    )
    target = float(col.decks.config_dict_for_deck_id(did).get("desiredRetention", 0.9))
    goal = round(target * 100)
    if not eases:
        retention = _cell("–", "retention, last 30 days", "no reviews yet")
    else:
        pct = round(100 * sum(1 for e in eases if e > 1) / len(eases))
        below = pct < goal
        retention = _cell(
            f"{pct}%",
            "retention, last 30 days",
            f"target {goal}% · {'below' if below else 'on target'}",
            "below" if below else "met",
        )
    name = col.decks.name(did)
    return (
        '<div class="position-relative"><h1>At a glance</h1></div><div class="xp-glance">'
        + _cell(str(due), "due today")
        + _cell(str(tomorrow), "due tomorrow")
        + _cell(str(reps), "studied today", f"{ms // 60_000} min" if reps else "")
        + retention
        + f'</div><p class="xp-glance-deck">{escape(name)}</p>'
    )


def player_html(game: Game) -> str:
    p = game.g
    lv = level(p.xp)
    longest = game.longest_streak
    streak_note = (
        f"longest {longest}"
        if longest > p.streak
        else ("your longest" if p.streak > 0 else "")
    )
    return (
        '<div class="position-relative"><h1>Player</h1></div><div class="xp-glance">'
        + _cell(
            f"Lv {lv + 1}", "level", f"{p.xp - level_start(lv):,}/{100 * (lv + 1):,} XP"
        )
        + _cell(
            f"+{p.today.xp:,}",
            "XP today",
            f"best combo {p.today.best}" if p.today.best else "",
        )
        + _cell(f"🔥 {p.streak}", "day streak", streak_note)
        + _cell(
            f"{p.reps}/{p.goal}",
            "daily goal",
            "done today" if p.goal_hit else f"{p.goal - p.reps} reps to go",
            "met" if p.goal_hit else "",
        )
        + '</div><p class="xp-glance-deck">All decks, every device</p>'
    )


def script(glance: str, player: str) -> str:
    """The graphs render after the page loads: wait for the first section, put both boxes above it, in order."""
    boxes = json.dumps(
        [["xp-glance", glance], ["xp-player", player]], ensure_ascii=False
    )
    return f"""(function () {{
    var boxes = {boxes}, tries = 0;
    (function place() {{ boxes.forEach(function (b) {{
        var box = document.getElementById(b[0]);
        if (!box) {{
            var first = document.querySelector(".container:not([id^=xp-]) > .position-relative > h1");
            if (!first) {{ if (b[0] === "xp-glance" && tries++ < 50) setTimeout(place, 200); return; }}
            var sec = first.closest(".container");
            box = document.createElement("div");
            box.id = b[0];
            box.className = "container";
            sec.parentNode.insertBefore(box, sec);
        }}
        box.innerHTML = b[1];
    }}); }})();
}})();"""
