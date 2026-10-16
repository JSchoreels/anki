# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: the Anki XP look on the desktop's web views while Anki XP is on, in light mode (it is a day theme, as on
the phone). pages.css is the phone's (assets/xp/pages.css: only Anki's CSS variables and a few generic elements, so
the Svelte pages - deck options, statistics, card info - follow it); look.css adds the desktop's own screens."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aqt import gui_hooks
from aqt.theme import theme_manager

if TYPE_CHECKING:
    from .review import XpReview

HERE = Path(__file__).parent


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
        """Anki's own screens (stdHtml): deck list, overview, toolbars, the reviewer's bottom bar."""
        from aqt.reviewer import Reviewer

        if not self.on or context is None or isinstance(context, Reviewer):
            return  # the card keeps its note type's styling
        web_content.head += f"<style>{self.css('pages.css', 'look.css')}</style>"

    def on_page(self, webview: Any) -> None:
        """The Svelte pages (deck options, statistics, card info, …): pages.css after Anki's own style."""
        if not self.on:
            return
        css = json.dumps(self.css("pages.css"))
        webview.eval(
            "(() => { const s = document.createElement('style'); s.id = 'kuma3-xp';"
            f" s.textContent = {css}; document.head.appendChild(s); }})();"
        )
