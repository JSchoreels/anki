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

from anki.utils import is_win
from aqt import gui_hooks
from aqt.qt import QEvent, QObject, Qt, QWidget
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
        gui_hooks.style_did_init.append(self.on_qss)

    @property
    def on(self) -> bool:
        return self.review.on and not theme_manager.night_mode

    def css(self, *names: str) -> str:
        return "\n".join((HERE / n).read_text(encoding="utf-8") for n in names)

    def on_qss(self, buf: str) -> str:
        """The Qt widgets (Browse, Add, Add-ons, every dialog): Fusion, so the stylesheet draws the same everywhere,
        the XP face in the palette, and look.qss after Anki's own rules."""
        if not self.on:
            return buf
        from aqt import mw
        from aqt.qt import QColor, QPalette, QStyleFactory

        app = mw.app
        app.setStyle(QStyleFactory.create("fusion"))
        palette = app.palette()
        for role, color in (
            (QPalette.ColorRole.Window, "#ece9d8"),
            (QPalette.ColorRole.Button, "#ece9d8"),
            (QPalette.ColorRole.AlternateBase, "#f4f3ee"),
            (QPalette.ColorRole.Base, "#ffffff"),
            (QPalette.ColorRole.Highlight, "#316ac5"),
            (QPalette.ColorRole.HighlightedText, "#ffffff"),
            (QPalette.ColorRole.ToolTipBase, "#ffffe1"),
        ):
            palette.setColor(role, QColor(color))
        app.setPalette(palette)
        return buf + self.css("look.qss")

    def restyle(self) -> None:
        """Anki XP switched, or the profile (and its switch) just opened: Anki's palette and stylesheet again,
        with or without on_qss."""
        from aqt import mw

        theme_manager._apply_palette(mw.app)
        theme_manager._apply_style(mw.app)
        if not hasattr(self, "_bars"):
            self._bars = _LunaBars(self)
            mw.app.installEventFilter(self._bars)
        for w in mw.app.topLevelWidgets():
            if w.isVisible():
                self._bars.paint(w)

    def on_content(self, web_content: Any, context: object | None) -> None:
        """Anki's own screens (stdHtml): deck list, overview, the toolbars, the reviewer's answer bar. Not the card
        (its note type's styling) nor add-on pages (untested with these rules). The editor gets pages.css only
        (Anki's colour variables), not look.css's button and toolbar rules."""
        from aqt.editor_legacy import Editor
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
        if not self.on:
            return
        if isinstance(context, Editor):
            web_content.head += f"<style>{self.css('pages.css')}</style>"
            return
        if not isinstance(context, screens):
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


class _LunaBars(QObject):
    """Luna blue title bars on Anki's own windows only: Windows 11 lets an app colour its caption (DWM caption,
    text and border colours), so nothing changes outside Anki. A flat Luna blue, not the gradient: the gradient and
    the round caption buttons would need frameless windows with home-made bars (not worth it)."""

    def __init__(self, look: XpLook) -> None:
        super().__init__()
        self.look = look

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if (
            event is not None
            and event.type() == QEvent.Type.Show
            and isinstance(obj, QWidget)
            and obj.isWindow()
        ):
            self.paint(obj)
        return False

    def paint(self, w: QWidget) -> None:
        if not is_win or w.windowType() not in (Qt.WindowType.Window, Qt.WindowType.Dialog):
            return
        import ctypes

        default = 0xFFFFFFFF  # DWMWA_COLOR_DEFAULT
        on = self.look.on
        # COLORREF is 0x00BBGGRR: border #0831d9, caption #0054e3, text white
        for attr, color in ((34, 0x00D93108), (35, 0x00E35400), (36, 0x00FFFFFF)):
            value = ctypes.c_uint(color if on else default)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(w.winId()), attr, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                return  # Windows 10: no caption colours
