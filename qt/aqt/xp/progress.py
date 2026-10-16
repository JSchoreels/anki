# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP game's link to the collection (the phone's XpGame.readNotes / writeNote / pickBoss).

Progress is shared with the phone and kuma3 Skins: every device keeps its own part in its own `🐻 Anki XP progress`
note (fields Device, Data; card suspended in the kumapie deck); all parts add up to one XP. The streak is the review
streak from the revlog, as the heatmap counts it.
"""

from __future__ import annotations

import json
import logging

from anki.collection import Collection

from .game import Game

logger = logging.getLogger(__name__)

PROGRESS = "🐻 Anki XP progress"
DECK = "kumapie"
DAY_MS = 86_400_000


def review_days(col: Collection) -> set[int]:
    """Days with a review, as days ago (0 = today, Anki's day cutoff); manual reschedules don't count."""
    cutoff = int(col.sched.day_cutoff) * 1000
    return {
        (cutoff - 1 - int(i)) // DAY_MS
        for i in col.db.list("select id from revlog where ease > 0")
        if int(i) < cutoff
    }


def streaks(days: set[int]) -> tuple[int, int, bool]:
    """(current, longest, reviewed today): today counts once reviewed; until then the streak runs to yesterday."""
    today = 0 in days
    cur = 0
    d = 0 if today else 1
    while d in days:
        cur += 1
        d += 1
    longest = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev is not None and d == prev + 1 else 1
        longest = max(longest, run)
        prev = d
    return cur, longest, today


def read_notes(col: Collection, game: Game) -> None:
    """Every other device's part (the phone's, kuma3 Skins', other PCs') and the review streak."""
    try:
        game.review_streak, game.longest_streak, game.reviewed_today = streaks(
            review_days(col)
        )
    except Exception:
        logger.exception("Anki XP: reading the review streak")
    try:
        others = {}
        for nid in col.find_notes(f'"note:{PROGRESS}"'):
            f = col.get_note(nid).fields
            if len(f) >= 2 and f[0] and f[0] != game.device:
                try:
                    others[f[0]] = json.loads(f[1])
                except ValueError:
                    pass
        game.others = others
    except Exception:
        logger.exception("Anki XP: reading the progress notes")
    game.save()


def write_note(col: Collection, game: Game, create: bool = True) -> None:
    """This device's part into its note (made with its note type the first time, which is undoable: only when the
    reviewer closes, create=True). An update leaves no undo entry and keeps Anki's undo (kuma3's rslib change), so
    it is written after every answer."""
    try:
        data = json.dumps(game.mine, ensure_ascii=False, separators=(",", ":"))
        nt = col.models.by_name(PROGRESS)
        if nt:
            for nid in col.find_notes(f'"note:{PROGRESS}"'):
                note = col.get_note(nid)
                if note.fields[0] == game.device:
                    if note.fields[1] != data:
                        note.fields[1] = data
                        col.update_note(note, skip_undo_entry=True)
                    return
        if not create:
            return
        if not nt:
            m = col.models.new(PROGRESS)
            col.models.add_field(m, col.models.new_field("Device"))
            col.models.add_field(m, col.models.new_field("Data"))
            t = col.models.new_template("Card 1")
            t["qfmt"] = "{{Device}}"
            t["afmt"] = "{{FrontSide}}<hr id=answer>{{Data}}"
            col.models.add_template(m, t)
            col.models.add_dict(m)
            nt = col.models.by_name(PROGRESS)
        note = col.new_note(nt)
        note["Device"] = game.device
        note["Data"] = data
        note.tags = ["anki-xp"]
        col.add_note(note, col.decks.id(DECK))
        col.sched.suspend_cards(note.card_ids())
    except Exception:
        logger.exception("Anki XP: writing the progress note")


def pick_boss(col: Collection, game: Game, deck_id: int) -> None:
    """Today's boss for the deck: its due card with the most lapses (1+), or none."""
    if not game.needs_boss(deck_id):
        return
    try:
        ids = col.find_cards(
            f"did:{deck_id} is:due -is:suspended -is:buried prop:lapses>0",
            order="c.lapses desc",
        )
    except Exception:
        logger.exception("Anki XP: boss card")
        ids = []
    game.set_boss(deck_id, int(ids[0]) if ids else 0)


if __name__ == "__main__":
    assert streaks({0, 1, 2, 5}) == (3, 3, True)
    assert streaks({1, 2, 3, 4, 9, 10}) == (4, 4, False)
    assert streaks({2, 3}) == (0, 2, False)
    assert streaks(set()) == (0, 0, False)
    print("aqt.xp.progress self-check ok")
