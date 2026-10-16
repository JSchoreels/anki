# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP game's window (the phone's XpWindows): today's quests, trophies, the player's numbers and the
game's settings (speed clock, sound, daily goal). Opened from the reviewer's game bar."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

from aqt.qt import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    qconnect,
)

from .game import LIMITS, QUEST_XP, TROPHIES, Game, level, level_start

if TYPE_CHECKING:
    from aqt.main import AnkiQt

CSS = """
body { font-family: Tahoma, 'Segoe UI', sans-serif; font-size: 13px; color: #000; background: #ece9d8; }
h3 { margin: 10px 0 4px; color: #003399; font-size: 14px; }
table { border-collapse: collapse; }
td { padding: 2px 8px 2px 0; vertical-align: top; }
.bar { background: #686868; width: 160px; height: 10px; }
.done { color: #006b00; font-weight: bold; }
.locked { color: #6d6d6d; }
.big { font-size: 16px; font-weight: bold; }
"""


def _bar(frac: float) -> str:
    w = int(160 * max(0.0, min(1.0, frac)))
    return (
        '<table cellspacing=0 cellpadding=0 style="border:1px solid #686868"><tr>'
        f'<td style="background:#3a7ce8;width:{w}px;height:9px;padding:0"></td>'
        f'<td style="background:#fff;width:{160 - w}px;height:9px;padding:0"></td></tr></table>'
    )


def page(game: Game) -> str:
    g = game.g
    lv = level(g.xp)
    lo, size = level_start(lv), 100 * (lv + 1)
    out = [f"<style>{CSS}</style>"]
    out.append(
        f"<div class=big>Level {lv + 1} · {g.xp:,} XP</div>{_bar((g.xp - lo) / size)}"
        f"<div>{lo + size - g.xp:,} XP to level {lv + 2}</div>"
    )
    out.append("<h3>Today</h3><table>")
    out.append(f"<tr><td>Reps</td><td><b>{g.reps}</b> of {g.goal}</td></tr>")
    out.append(
        f"<tr><td>XP today</td><td><b>{g.today.xp:,}</b> (best combo {g.today.best})</td></tr>"
    )
    out.append(f"<tr><td>Speed bonuses</td><td><b>{g.today.fast}</b></td></tr>")
    out.append(
        f"<tr><td>Streak</td><td><b>🔥 {g.streak} days</b> (longest {game.longest_streak})</td></tr>"
    )
    out.append(f"<tr><td>Best combo ever</td><td><b>{g.record}</b></td></tr>")
    out.append("</table><h3>Quests</h3><table>")
    for q in game.quests():
        got = q.id in g.today.got
        out.append(
            f"<tr><td>{'✔' if got else '☐'}</td><td class={'done' if got else ''}>{escape(q.text)}</td>"
            f"<td>{min(q.have, q.need)}/{q.need}</td><td>+{QUEST_XP} XP</td></tr>"
        )
    out.append("</table>")
    have = sum(1 for t in TROPHIES if t.id in g.ach)
    out.append(f"<h3>Trophies {have}/{len(TROPHIES)}</h3><table>")
    for t in TROPHIES:
        got = t.id in g.ach
        out.append(
            f"<tr class={'' if got else 'locked'}><td>{t.icon if got else '🔒'}</td>"
            f"<td><b>{escape(t.name)}</b></td><td>{escape(t.desc)}</td></tr>"
        )
    out.append("</table>")
    return "".join(out)


def show_quests(mw: AnkiQt, game: Game) -> None:
    d = QDialog(mw)
    d.setWindowTitle("Anki XP")
    d.resize(460, 620)
    lay = QVBoxLayout(d)
    view = QTextBrowser(d)
    view.setHtml(page(game))
    lay.addWidget(view)

    row = QHBoxLayout()
    row.addWidget(QLabel("Speed clock"))
    clock = QComboBox(d)
    for s in LIMITS:
        clock.addItem(f"{s} s", s)
    clock.setCurrentIndex(LIMITS.index(game.limit) if game.limit in LIMITS else 2)
    row.addWidget(clock)
    row.addWidget(QLabel("Daily goal"))
    goal = QSpinBox(d)
    goal.setRange(10, 1000)
    goal.setSingleStep(10)
    goal.setValue(game.g.goal)
    row.addWidget(goal)
    sound = QCheckBox("Sounds", d)
    sound.setChecked(game.sound)
    row.addWidget(sound)
    row.addStretch()
    lay.addLayout(row)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, d)
    qconnect(buttons.rejected, d.reject)
    lay.addWidget(buttons)

    def apply() -> None:
        game.store["limit"] = clock.currentData()
        game.store["sound"] = sound.isChecked()
        if goal.value() != game.g.goal:
            game.set_goal(goal.value())
        if mw.xp:
            mw.xp.push_bar()
            mw.xp.js(
                f"kxp.init({{sound: {str(game.sound).lower()}, limit: {game.limit}}})"
            )

    qconnect(d.finished, lambda _: apply())
    d.show()
