# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP home on the desktop deck list (the phone's XpHome): a Player group box above the decks (level,
XP bar, streak, daily goal, Quests and Trophies), the decks with nothing due folded into "N decks done for today",
and Today, RWKV recall and Activity boxes under them (today.py). Only while Anki XP is on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt import gui_hooks

from . import progress
from .today import Today
from .game import TROPHIES, level, level_start

if TYPE_CHECKING:
    from .review import XpReview

CSS = """
<style>
fieldset.kxp-box { border: 1px solid #d0d0bf; border-radius: 4px; margin: 0 auto 14px; padding: 6px 12px 10px;
  max-width: 600px; text-align: left; font-family: Tahoma, 'Segoe UI', sans-serif; font-size: 13px; color: #000;
  box-shadow: inset 1px 1px 0 #fff, 1px 1px 0 #fff; }
fieldset.kxp-box legend { color: #0046d5; padding: 0 4px; font-weight: bold; }
.kxp-lv { display: flex; align-items: center; gap: 10px; }
.kxp-lv b { font-size: 15px; white-space: nowrap; }
.kxp-xpbar { flex: 1; height: 12px; background: #686868; padding: 1px; }
.kxp-xpbar div { height: 100%; background: #fff; }
.kxp-xpbar i { display: block; height: 100%; background: linear-gradient(#a5c8ff, #3a7ce8, #2563d8); }
.kxp-row { margin-top: 6px; display: flex; gap: 18px; flex-wrap: wrap; align-items: center; }
.kxp-row a { color: #0033cc; text-decoration: none; cursor: pointer; }
.kxp-row a:hover { text-decoration: underline; }
.kxp-ok { color: #006b00; font-weight: bold; }
tr.kxp-done { display: none; }
body.kxp-open tr.kxp-done { display: table-row; }
#kxp-fold td { padding: 6px 0 2px; color: #595959; cursor: pointer; }
#kxp-fold td:hover { color: #000; }
</style>
"""

FOLD_JS = """
<script>
(function () {
  function due(row) {
    let any = false, known = true;
    for (const k of ["new", "learn", "review"]) {
      const e = document.getElementById("deck-" + row.id + "-" + k + "-count");
      if (!e) continue;
      const t = e.textContent.trim();
      if (!/^\\d+$/.test(t)) known = false; else if (+t > 0) any = true;
    }
    return any || !known;  // a count still coming ("…") keeps the deck shown
  }
  function fold() {
    const rows = [...document.querySelectorAll("tr.deck")];
    let hidden = 0;
    for (const r of rows) {
      const done = !due(r) && !r.classList.contains("current");
      r.classList.toggle("kxp-done", done);
      if (done) hidden++;
    }
    let f = document.getElementById("kxp-fold");
    if (!hidden) { if (f) f.remove(); return; }
    if (!f) {
      f = document.createElement("tr");
      f.id = "kxp-fold";
      f.innerHTML = "<td colspan=6></td>";
      f.onclick = () => { document.body.classList.toggle("kxp-open"); fold(); };
      const last = rows[rows.length - 1];
      last && last.parentNode.insertBefore(f, last.nextSibling);
    }
    const open = document.body.classList.contains("kxp-open");
    f.firstChild.textContent = open ? "▾ Hide " + hidden + " decks done for today"
                                    : "▸ " + hidden + " decks done for today";
  }
  fold();
  let t = 0;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(fold, 50); })
    .observe(document.body, { subtree: true, childList: true, characterData: true });
})();
</script>
"""


class XpHome:
    def __init__(self, review: XpReview) -> None:
        self.review = review
        self.today = Today(review.mw)
        gui_hooks.deck_browser_will_render_content.append(self.on_render)

    def on_render(self, deck_browser: Any, content: Any) -> None:
        if not self.review.on:
            return
        game = self.review.load()
        try:
            progress.read_notes(deck_browser.mw.col, game)
        except Exception:
            pass
        game.new_day()
        g = game.g
        lv = level(g.xp)
        lo, size = level_start(lv), 100 * (lv + 1)
        frac = max(0.0, min(1.0, (g.xp - lo) / size))
        quests = game.quests()
        got = sum(1 for q in quests if q.id in g.today.got)
        trophies = sum(1 for t in TROPHIES if t.id in g.ach)
        goal = (
            f"<span class=kxp-ok>Goal {g.reps}/{g.goal} ✓</span>"
            if g.goal_hit
            else f"Goal {g.reps}/{g.goal}"
        )
        days = "day" if g.streak == 1 else "days"
        player = f"""
<fieldset class=kxp-box><legend>Player</legend>
<div class=kxp-lv><b>Lv {lv + 1}</b><div class=kxp-xpbar title="{lo + size - g.xp:,} XP to level {lv + 2}">
<div><i style="width:{frac * 100:.0f}%"></i></div></div><span>{g.xp - lo:,}/{size:,} XP</span></div>
<div class=kxp-row><span>🔥 {g.streak} {days}</span><span>{goal}</span>
<a onclick="return pycmd('kxp:quests')">Quests {got}/{len(quests)} ▸</a>
<a onclick="return pycmd('kxp:quests')">Trophies {trophies}/{len(TROPHIES)} ▸</a></div>
</fieldset>"""
        content.tree = CSS + player + content.tree + FOLD_JS
        content.stats = self.today.html(content.stats, deck_browser)
