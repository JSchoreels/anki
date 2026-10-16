# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP look on the desktop's web views while Anki XP is on, in light mode (it is a day theme, as on
the phone). pages.css is the phone's (assets/xp/pages.css: only Anki's CSS variables and a few generic elements, so
the Svelte pages - deck options, statistics, card info - follow it); look.css adds the desktop's own screens."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aqt import gui_hooks
from aqt.theme import theme_manager

if TYPE_CHECKING:
    from .review import XpReview

HERE = Path(__file__).parent
logger = logging.getLogger(__name__)


class XpLook:
    def __init__(self, review: XpReview) -> None:
        self.review = review
        gui_hooks.webview_will_set_content.append(self.on_content)
        gui_hooks.webview_did_inject_style_into_page.append(self.on_page)

    @property
    def on(self) -> bool:
        return self.review.on and not theme_manager.night_mode

    def css(self, *names: str) -> str:
        return "\n".join((HERE / n).read_text(encoding="utf-8") for n in names)

    def on_content(self, web_content: Any, context: object | None) -> None:
        """Anki's own screens (stdHtml): deck list, overview, the toolbars, the reviewer's answer bar. Not the card
        (its note type's styling) nor the editor or add-on pages (untested with these rules)."""
        from aqt.deckbrowser import DeckBrowser, DeckBrowserBottomBar
        from aqt.overview import Overview, OverviewBottomBar
        from aqt.reviewer import ReviewerBottomBar
        from aqt.toolbar import BottomToolbar, TopToolbar

        screens = (
            DeckBrowser,
            DeckBrowserBottomBar,
            Overview,
            OverviewBottomBar,
            ReviewerBottomBar,
            TopToolbar,
            BottomToolbar,
        )
        if not self.on or not isinstance(context, screens):
            return
        web_content.head += f"<style>{self.css('pages.css', 'look.css')}</style>"

    def on_page(self, webview: Any) -> None:
        """The Svelte pages (deck options, statistics, card info, …): pages.css after Anki's own style."""
        if not self.on:
            return
        congrats = "congrats" in webview.page().url().path()
        css = json.dumps(self.css("pages.css", *(["congrats.css"] if congrats else [])))
        webview.eval(
            "(() => { const s = document.createElement('style'); s.id = 'kuma3-xp';"
            f" s.textContent = {css}; document.head.appendChild(s); }})();"
        )
        if congrats:  # the phone's Easter egg: the window's close box, 5 clicks
            webview.eval(self.css("congrats.js"))
        if "graphs" in webview.page().url().path():
            self.glance(webview)

    def glance(self, webview: Any) -> None:
        """Statistics: "At a glance" for the page's deck and the "Player" box (the phone's XpGlance)."""
        from urllib.parse import parse_qs

        from aqt import mw

        from . import glance, progress

        if not mw or not mw.col:
            return
        try:
            query = parse_qs(webview.page().url().query())
            did = int((query.get("currentDeckId") or [mw.col.decks.current()["id"]])[0])
            game = self.review.load()
            progress.read_notes(mw.col, game)
            webview.eval(
                glance.script(glance.glance_html(mw.col, did), glance.player_html(game))
            )
        except Exception:
            logger.exception("Anki XP: statistics boxes")
