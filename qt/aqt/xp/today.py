# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP home's boxes under the decks (the phone's XpHome footers): Today ("Studied … today" and the
RWKV forecast, RwkvForecast.kt's lines), RWKV recall (a dot per review card at its recall now, red line at the
desired retention) and Activity (HeatmapFooter.kt: 18 weeks back, 8 ahead, the stats line). The forecast runs in
the background once the deck counts are in and is swapped into the page; it means "if you stop reviewing now"."""

from __future__ import annotations

import datetime
import html
import json
import logging
import time
from typing import Any

from aqt import gui_hooks
from aqt.qt import QTimer

from . import forecast as engine

logger = logging.getLogger(__name__)

NEAR = 0.03  # "close" = recall within this of the card's target
AXIS_MIN = 0.6
MAX_AGE_SECS = 300
RETRY_MS, MAX_RETRIES = 2000, 15
INK = {"due": "#c62828", "near": "#e65100", "safe": "#2e7d32"}  # 4.5:1 on the face
DOT = {"due": "#e53935", "near": "#e65100", "safe": "#43a047"}
GREENS = ["#d6e685", "#8cc665", "#44a340", "#1e6823"]
GREYS = ["#d4d4d4", "#b4b4b4", "#8c8c8c", "#666666"]
EMPTY = "rgba(128,128,128,.13)"

CSS = """
<style>
.kxp-today { line-height: 1.5; }
.kxp-today a.n { cursor: pointer; color: inherit; text-decoration: none; }
.kxp-today a.n:hover { text-decoration: underline; }
.kxp-fc { margin-top: 4px; }
.kxp-recall { position: relative; margin: 6px 4px 0; border-bottom: 1px solid #999; }
.kxp-recall i { position: absolute; width: 6px; height: 6px; border-radius: 50%; margin-left: -3px; }
.kxp-recall b { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: #e53935; }
.kxp-recall s { position: absolute; top: 0; bottom: 0; background: rgba(230, 81, 0, .16); }
.kxp-axis { position: relative; height: 14px; margin: 0 4px; font-size: 10px; color: #444; }
.kxp-axis span { position: absolute; transform: translateX(-50%); }
.kxp-axis span:first-child { transform: none; } .kxp-axis span:last-child { transform: translateX(-100%); }
.kxp-heat { display: grid; grid-auto-flow: column; grid-template-rows: repeat(7, 11px); grid-auto-columns: 11px;
  gap: 2px; justify-content: center; margin-top: 4px; }
.kxp-heat i { display: block; }
.kxp-heat i.now { outline: 1.5px solid #333; outline-offset: -1px; }
.kxp-cap { text-align: center; color: #444; margin-top: 4px; }
.kxp-stats { text-align: center; margin-top: 2px; }
.kxp-stats b { color: #1e6823; }
</style>
"""


class Today:
    def __init__(self, mw: Any) -> None:
        self.mw = mw
        self.fc: engine.Forecast | None = None
        self.key: tuple | None = None
        self.running = self.again = False
        self.retries = 0
        self.off = False  # this Anki has no RWKV: no forecast lines, no recall box
        gui_hooks.webview_did_receive_js_message.append(self.on_js)
        gui_hooks.profile_will_close.append(lambda: setattr(self, "fc", None))

    # ------------------------------------------------------------ the boxes

    def html(self, studied: str, deck_browser: Any) -> str:
        if not self.fresh():
            self.after_counts(deck_browser)
        return (
            CSS
            + f'<fieldset class="kxp-box kxp-today"><legend>Today</legend><div>{studied}</div>'
            + f'<div class="kxp-fc" id="kxp-fc">{self.forecast_html()}</div></fieldset>'
            + f'<div id="kxp-recall">{self.recall_html()}</div>'
            + self.activity_html()
        )

    def forecast_html(self) -> str:
        fc = self.fc
        if self.off:
            return ""
        if fc is None:
            return '<span style="color:#595959">RWKV forecast: computing…</span>'
        cids = list(fc.cards)
        if not cids:
            return ""
        now = split(fc, cids, 0)
        due0 = set(now["due"])
        in1h = [c for c in split(fc, cids, fc.index("1h"))["due"] if c not in due0]
        in3h = [c for c in split(fc, cids, fc.index("3h"))["due"] if c not in due0]
        tomorrow = split(fc, cids, fc.index("tomorrow"))["due"]
        due, near, safe = now["due"], now["near"], now["safe"]
        return (
            f"<div>{link(due, f'{len(due)} below target', 'due')}</div>"
            f"<div>{link(near, f'{len(near)} close', 'near')} · {link(safe, f'{len(safe)} safe', 'safe')}</div>"
            f"<div>If you stop now: {link(in1h, f'+{len(in1h)} in 1 h')} · {link(in3h, f'+{len(in3h)} in 3 h')} · "
            f"{link(tomorrow, f'{len(tomorrow)} by tomorrow')}</div>"
        )

    def recall_html(self) -> str:
        """One dot per card at its recall now, stacked where they meet, a red line at each desired retention."""
        fc = self.fc
        if self.off or fc is None or not fc.cards:
            return ""
        pct = lambda r: max(0.0, min(1.0, (r - AXIS_MIN) / (1 - AXIS_MIN))) * 100
        order = sorted(fc.cards, key=lambda c: fc.cards[c]["r"][0])
        bins = [int(pct(fc.cards[c]["r"][0]) / 0.9) for c in order]  # about a dot wide
        tallest = max((bins.count(b) for b in set(bins)), default=1)
        step = min(7.0, 60 / max(1, tallest))
        stacks: dict[int, int] = {}
        dots = []
        for cid, b in zip(order, bins):
            card = fc.cards[cid]
            r, t = card["r"][0], card["target"]
            n = stacks[b] = stacks.get(b, 0) + 1
            kind = "due" if r < t else "near" if r < t + NEAR else "safe"
            dots.append(
                f'<i style="left:{pct(r):.2f}%;bottom:{(n - 1) * step + 1:.1f}px;background:{DOT[kind]}"'
                f' title="R {r * 100:.1f}%"></i>'
            )
        targets = sorted({fc.cards[c]["target"] for c in order})
        lines = "".join(
            f'<s style="left:{pct(t):.2f}%;width:{pct(t + NEAR) - pct(t):.2f}%"></s><b style="left:{pct(t):.2f}%"></b>'
            for t in targets
        )
        ticks = "".join(
            f"<span style=\"left:{k * 25}%\">{round((AXIS_MIN + (1 - AXIS_MIN) * k / 4) * 100)}%</span>"
            for k in range(5)
        )
        height = (tallest - 1) * step + 10
        return (
            '<fieldset class="kxp-box"><legend>RWKV recall</legend>'
            f'<div class="kxp-recall" style="height:{height:.0f}px">{lines}{"".join(dots)}</div>'
            f'<div class="kxp-axis">{ticks}</div></fieldset>'
        )

    def activity_html(self) -> str:
        try:
            today, past, future = heat_data(self.mw.col)
        except Exception:
            logger.exception("Anki XP: heatmap")
            return ""
        first = today - datetime.timedelta(weeks=18)
        last = today + datetime.timedelta(weeks=8)
        past_avg = avg(v for k, v in past.items() if k <= 0 and v > 0)
        future_avg = avg(v for k, v in future.items() if k > 0 and v > 0)
        cells = ["<i></i>"] * first.weekday()  # weeks start on Monday
        day = first
        while day <= last:
            off = (day - today).days
            n = (past if off <= 0 else future).get(off, 0)
            shade = level(n, past_avg if off <= 0 else future_avg)
            color = EMPTY if shade is None else (GREENS if off <= 0 else GREYS)[shade]
            date = day.strftime("%A %B %d, %Y").replace(" 0", " ")
            tip = (
                (f"{n:,} review{'s' * (n != 1)} on {date}" if n else f"No reviews on {date}")
                if off <= 0
                else (f"{n:,} card{'s' * (n != 1)} due on {date}" if n else f"Nothing due on {date}")
            )
            now = ' class="now"' if off == 0 else ""
            cells.append(f'<i{now} style="background:{color}" title="{tip}"></i>')
            day += datetime.timedelta(days=1)
        studied = {k: v for k, v in past.items() if k <= 0 and v > 0}
        longest = run = 0
        if studied:
            for k in range(min(studied), 1):
                run = run + 1 if k in studied else 0
                longest = max(longest, run)
        current, k = 0, 0 if 0 in studied else -1
        while k in studied:
            current, k = current + 1, k - 1
        daily = round(sum(studied.values()) / len(studied)) if studied else 0
        learned = round(100 * len(studied) / (1 - min(studied))) if studied else 0
        days = lambda n: f"{n} day{'s' * (n != 1)}"
        cap = f"{first.strftime('%b %Y')} – {last.strftime('%b %Y')}"
        stats = "&nbsp;&nbsp;&nbsp; ".join(
            f"{label}:&nbsp;<b>{value.replace(' ', '&nbsp;')}</b>"
            for label, value in (
                ("Daily average", f"{daily:,} reviews"),
                ("Days learned", f"{learned}%"),
                ("Longest streak", days(longest)),
                ("Current streak", days(current)),
            )
        )
        return (
            '<fieldset class="kxp-box"><legend>Activity</legend>'
            f'<div class="kxp-heat">{"".join(cells)}</div><div class="kxp-cap">{cap}</div>'
            f'<div class="kxp-stats">{stats}</div></fieldset>'
        )

    # ------------------------------------------------------------ the forecast in the background

    def key_now(self) -> tuple:
        col = self.mw.col
        return (col.db.scalar("select max(id) from revlog") or 0, col.sched.day_cutoff)

    def fresh(self) -> bool:
        if self.off:
            return True
        return (
            self.fc is not None
            and self.key == self.key_now()
            and time.time() - self.fc.taken < MAX_AGE_SECS
        )

    def after_counts(self, deck_browser: Any, waited: int = 0) -> None:
        """Start once the fork's own RWKV deck counts are in: both use the RWKV backend, and together the counts
        were ~3x slower (the add-on's finding)."""
        if getattr(deck_browser, "_rwkv_pending_deck_ids", None) and waited < 5000:
            QTimer.singleShot(30, lambda: self.after_counts(deck_browser, waited + 30))
            return
        self.refresh()

    def refresh(self) -> None:
        if self.mw.col is None:
            return
        if self.running:
            self.again = True
            return
        self.running, self.again = True, False
        key = self.key_now()

        def done(fut: Any) -> None:
            self.running = False
            try:
                self.fc, self.key, self.retries = fut.result(), key, 0
            except engine.Busy:
                self.retries += 1
                if self.retries <= MAX_RETRIES:
                    QTimer.singleShot(RETRY_MS, self.refresh)
                    return
                self.retries = 0
            except RuntimeError:
                self.off = True  # no RWKV in this Anki
            except Exception:
                logger.exception("Anki XP: RWKV forecast")
            self.swap()
            if self.again:
                QTimer.singleShot(0, self.refresh)

        self.mw.taskman.run_in_background(
            lambda: engine.compute(self.mw, engine.offsets_for()), done, uses_collection=True
        )

    def swap(self) -> None:
        if self.mw.state != "deckBrowser":
            return
        self.mw.deckBrowser.web.eval(
            "(() => { for (const [id, h] of "
            + json.dumps([["kxp-fc", self.forecast_html()], ["kxp-recall", self.recall_html()]])
            + ") { const e = document.getElementById(id); if (e) e.innerHTML = h; } })();"
        )

    def on_js(self, handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
        if not message.startswith("kxp:browse:"):
            return handled
        import aqt

        aqt.dialogs.open("Browser", self.mw, search=(f"cid:{message.split(':', 2)[2]}",))
        return (True, None)


def split(fc: engine.Forecast, cids: list[int], i: int) -> dict[str, list[int]]:
    g: dict[str, list[int]] = {"due": [], "near": [], "safe": []}
    for cid in cids:
        r, t = fc.cards[cid]["r"][i], fc.cards[cid]["target"]
        g["due" if r < t else "near" if r < t + NEAR else "safe"].append(cid)
    return g


def link(cids: list[int], text: str, kind: str = "") -> str:
    style = f' style="color:{INK[kind]}"' if kind else ""
    if not cids:
        return f"<span{style}>{text}</span>"
    ids = ",".join(str(c) for c in cids)
    return f"<a class=n{style} onclick=\"return pycmd('kxp:browse:{ids}')\">{html.escape(text)}</a>"


def avg(values: Any) -> float:
    v = list(values)
    return sum(v) / len(v) if v else 1.0


def level(n: int, average: float) -> int | None:
    """Four shades relative to the average day; None for an empty day."""
    if n <= 0:
        return None
    return 0 if n < average * 0.5 else 1 if n < average else 2 if n < average * 1.5 else 3


def heat_data(col: Any) -> tuple[datetime.date, dict[int, int], dict[int, int]]:
    """HeatmapData.load: reviews per past Anki day and cards due per future day, as offsets from today."""
    day_start = col.sched.day_cutoff - 86_400
    shift = 86_400 * 100_000  # keeps the division flooring for past days
    past = {
        int(d) - 100_000: n
        for d, n in col.db.all(
            "select (id / 1000 - ? + ?) / 86400, count() from revlog where ease > 0 group by 1",
            day_start,
            shift,
        )
    }
    today_num = col.sched.today
    future: dict[int, int] = {}
    for due, n in col.db.all(
        "select due, count() from cards where queue in (2, 3) and due <= ? group by due", today_num + 400
    ):
        off = max(0, due - today_num)
        future[off] = future.get(off, 0) + n
    learning = col.db.scalar("select count() from cards where queue = 1") or 0
    if learning:
        future[0] = future.get(0, 0) + learning
    return datetime.date.fromtimestamp(day_start), past, future
