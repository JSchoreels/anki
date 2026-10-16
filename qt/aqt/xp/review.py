# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP game in the desktop reviewer (the phone's XpReview). Keeps the speed bonus clock, scores each
answer with game.Game and drives the bar, the chips, the balloons and the sounds in the reviewer's web view
(xp.js / xp.css). Turned on and off from Tools > Anki XP; the game's state lives in the profile between answers, the
shared progress note is written when the reviewer closes."""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anki.cards import Card
from aqt import gui_hooks
from aqt.utils import tr

from . import progress
from .game import FAST_POINTS, IDLE_MS, POINTS, Game, level, level_start

if TYPE_CHECKING:
    from aqt.main import AnkiQt

HERE = Path(__file__).parent
logger = logging.getLogger(__name__)
PROFILE_KEY = "kuma3_xp"


def _kind(title: str, level_up: bool) -> str:
    """Which jingle a win plays (XpReview.win)."""
    if level_up and title.startswith("Level"):
        return "level"
    if title.startswith("🏆") or title.startswith("👑 Boss"):
        return "big"
    if title.endswith("combo!"):
        return "combo"
    return ""


class XpReview:
    def __init__(self, mw: AnkiQt) -> None:
        self.mw = mw
        self.game: Game | None = None
        self.card_id = 0
        self.deck_id = 0
        self.shown = True
        self.clock_on = False
        self.clock_start = 0.0
        self.question_at = 0.0
        self.think_ms = 0
        self.token = 0
        self.in_review = False
        self.home: object | None = None  # aqt.xp.home.XpHome, set by setup()
        self.look: object | None = None  # aqt.xp.look.XpLook, set by setup()
        gui_hooks.webview_will_set_content.append(self.on_content)
        gui_hooks.state_did_change.append(self.on_state)
        gui_hooks.reviewer_did_show_question.append(self.on_question)
        gui_hooks.reviewer_did_show_answer.append(self.on_answer_shown)
        gui_hooks.reviewer_did_answer_card.append(self.on_answered)
        gui_hooks.reviewer_will_end.append(self.on_end)
        gui_hooks.state_did_undo.append(self.on_undo)
        gui_hooks.av_player_did_end_playing.append(self.on_audio_done)
        gui_hooks.webview_did_receive_js_message.append(self.on_js)
        gui_hooks.profile_will_close.append(self.on_profile_close)

    # ------------------------------------------------------------ on / off, state

    @property
    def on(self) -> bool:
        return bool(self.mw.pm.profile and self.mw.pm.profile.get("kuma3_xp_on", True))

    def set_on(self, on: bool) -> None:
        self.mw.pm.profile["kuma3_xp_on"] = on

    def load(self) -> Game:
        """The game for this profile (its part, settings and boss cards live in the profile)."""
        if self.game is None:
            store = self.mw.pm.profile.setdefault(PROFILE_KEY, {})
            # one part per PC, the same after a crash (the profile, and a random id in it, is saved only on close)
            store.setdefault("device", f"kuma3-desktop-{socket.gethostname()}")
            self.game = Game(store)
        return self.game

    def on_profile_close(self) -> None:
        self.game = None

    # ------------------------------------------------------------ the web view

    def on_content(self, web_content: Any, context: object | None) -> None:
        from aqt.reviewer import Reviewer

        if not isinstance(context, Reviewer) or not self.on:
            return
        web_content.head += (
            f"<style>{(HERE / 'xp.css').read_text(encoding='utf-8')}</style>"
        )
        web_content.body += (
            f"<script>{(HERE / 'xp.js').read_text(encoding='utf-8')}</script>"
        )

    def js(self, code: str) -> None:
        if self.in_review and self.mw.reviewer.web:
            self.mw.reviewer.web.eval(f"window.kxp && {code}")

    def push_bar(self) -> None:
        g = self.load()
        g.new_day()
        m = g.g
        lv = level(m.xp)
        lo, size = level_start(lv), 100 * (lv + 1)
        try:
            left = sum(self.mw.col.sched.counts())
        except Exception:
            left = -1
        mult = g.mult()
        state = {
            "toGoal": m.goal - m.reps,
            "left": left,
            "streak": m.streak,
            "lv": lv + 1,
            "frac": max(0.0, min(1.0, (m.xp - lo) / size)),
            "xp": m.xp,
            "next": lo + size - m.xp,
            "combo": g.combo,
            "mult": int(mult) if mult == int(mult) else round(mult, 1),
        }
        self.js(f"kxp.update({json.dumps(state)})")

    # ------------------------------------------------------------ the reviewer

    def on_state(self, new_state: str, old_state: str) -> None:
        if new_state != "review" or not self.on:
            return
        g = self.load()
        self.in_review = True
        g.start_session()

        def read(col: Any) -> None:
            progress.read_notes(col, g)
            progress.pick_boss(col, g, col.decks.selected())

        self.mw.taskman.run_in_background(
            lambda: read(self.mw.col), lambda fut: self.push_bar()
        )

    def on_question(self, card: Card) -> None:
        if not self.on or not self.in_review:
            return
        g = self.load()
        self.js(f"kxp.init({json.dumps({'sound': g.sound, 'limit': g.limit})})")
        self.card_id, self.deck_id = card.id, card.did
        self.shown = False
        self.think_ms = 0
        self.question_at = time.monotonic()
        has_audio = bool(card.question_av_tags()) and self.mw.reviewer.autoplay(card)
        self.clock_on = not has_audio
        self.clock_start = time.monotonic()
        self.js(f"kxp.clock({json.dumps('run' if self.clock_on else 'wait')})")
        self.token += 1
        if (
            has_audio
        ):  # the clock waits for the word audio, or 5 s if it never reports back
            t = self.token

            def late() -> None:
                if t == self.token:
                    self.on_audio_done(None)

            self.mw.progress.single_shot(5000, late, False)
        if g.first_clock():
            self.balloon(
                "⏱ Speed bonus",
                f"Show the answer before the green line runs out ({g.limit} s), then Good or Easy, for {FAST_POINTS} "
                f"points instead of {POINTS}. Again and Hard still count: grade honestly.",
            )
        if g.is_boss(self.deck_id, self.card_id):
            self.balloon(
                "👑 BOSS CARD",
                f"Failed {card.lapses}×: ×3 XP on this card, whatever you press.",
            )
        self.push_bar()

    def on_audio_done(self, player: Any) -> None:
        if self.shown or self.clock_on or not self.in_review:
            return
        self.clock_on = True
        self.clock_start = time.monotonic()
        self.js("kxp.clock('run')")

    def on_answer_shown(self, card: Card) -> None:
        if self.shown or not self.in_review:
            return
        self.shown = True
        self.think_ms = (
            int((time.monotonic() - self.clock_start) * 1000) if self.clock_on else 0
        )
        self.js("kxp.clock('hide')")

    def on_answered(self, reviewer: Any, card: Card, ease: int) -> None:
        if not self.on or not self.in_review:
            return
        g = self.load()
        idle = (time.monotonic() - self.question_at) * 1000 > IDLE_MS
        r = g.score(
            ease, self.think_ms if self.clock_on else 99_999, idle, card.did, card.id
        )
        mult = int(r.mult) if r.mult == int(r.mult) else round(r.mult, 1)
        payload = {
            "ease": ease,
            "points": r.points,
            "mult": mult,
            "xp": r.xp,
            "broke": r.broke_combo,
            "multUp": r.mult_up,
            "combo": g.combo,
            "wins": [[t, m, _kind(t, r.level_up)] for t, m in r.wins],
        }
        self.js(f"kxp.answered({json.dumps(payload, ensure_ascii=False)})")
        self.push_bar()
        progress.write_note(self.mw.col, g, create=False)  # the phone and Skins see it on the next sync

    def on_undo(self, changes: Any) -> None:
        """Undoing an answer gives back its rep, XP, quests and boss."""
        op = getattr(changes, "operation", "")
        logger.debug("Anki XP: undo of %r", op)
        if self.game and op == tr.actions_answer_card():
            if self.game.undo_answer():
                self.push_bar()
                progress.write_note(self.mw.col, self.game, create=False)

    def on_end(self) -> None:
        """The reviewer closes: the combo ends, a clean sitting may earn a trophy, the note is written."""
        if not self.in_review or not self.game:
            return
        self.in_review = False
        g = self.game
        rows, wins = g.finish_session()
        progress.write_note(self.mw.col, g)  # quick; before the collection may close
        if rows:
            from aqt.utils import tooltip

            table = "".join(
                f"<tr><td>{k}</td><td align=right><b>{v}</b></td></tr>" for k, v in rows
            )
            extra = "".join(f"<br><b>{t}</b> {m}" for t, m in wins)
            tooltip(
                f"<b>✔ Session report</b><table cellspacing=4>{table}</table>{extra}",
                period=7000,
            )

    def balloon(self, title: str, msg: str) -> None:
        self.js(
            f"kxp.balloon({json.dumps(title, ensure_ascii=False)}, {json.dumps(msg, ensure_ascii=False)})"
        )

    # ------------------------------------------------------------ the bar's click: quests and trophies

    def on_js(
        self, handled: tuple[bool, Any], message: str, context: Any
    ) -> tuple[bool, Any]:
        if message != "kxp:quests":
            return handled
        from .windows import show_quests

        show_quests(self.mw, self.load())
        return (True, None)
