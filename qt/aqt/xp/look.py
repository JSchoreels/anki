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
from aqt.qt import QEvent, QMainWindow, QObject, Qt, QWidget
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
        if not hasattr(self, "_windows"):
            self._windows = _Windows(self)
            mw.app.installEventFilter(self._windows)
        for w in mw.app.topLevelWidgets():
            if w.isVisible():
                self._windows.paint(w)

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
        from aqt import mw

        if not self.on:
            return
        if isinstance(context, Editor):
            web_content.head += f"<style>{self.css('pages.css')}</style>"
            return
        if not isinstance(context, screens):
            return
        web_content.head += f"<style>{self.css('pages.css', 'look.css')}</style>"
        if mw.bottomWeb:  # the white strip under the deck list: the page behind the HTML is beige too
            from aqt.qt import QColor

            mw.bottomWeb.page().setBackgroundColor(QColor("#ece9d8"))

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



class _Windows(QObject):
    """Anki's own windows only (nothing outside Anki changes): Luna blue title bars and square corners (Windows 11
    lets an app set its caption colours and corner style), and the phone's CRT on top. A flat Luna blue, not the
    gradient: that and the round caption buttons would need frameless windows with home-made bars (not worth it)."""

    def __init__(self, look: XpLook) -> None:
        super().__init__()
        self.look = look

    def eventFilter(self, obj: QObject | None, event: QEvent | None) -> bool:
        if isinstance(obj, QWidget) and obj.isWindow() and event is not None:
            if event.type() == QEvent.Type.Show:
                self.paint(obj)
            elif event.type() == QEvent.Type.Resize and (crt := obj.findChild(_Crt)):
                crt.cover()
        return False

    def paint(self, w: QWidget) -> None:
        if w.windowType() not in (Qt.WindowType.Window, Qt.WindowType.Dialog):
            return
        on = self.look.on
        crt = w.findChild(_Crt)
        if on and self.look.review.load().store.get("crt", True):
            (crt or _Crt(w)).cover()
        elif crt:
            crt.hide()
        if not is_win:
            return
        import ctypes

        default = 0xFFFFFFFF  # DWMWA_COLOR_DEFAULT
        # COLORREF is 0x00BBGGRR: border #0831d9, caption #0054e3, text white; corners 1 = square, 0 = Windows' own
        for attr, color, off in (
            (33, 1, 0),
            (34, 0x00D93108, default),
            (35, 0x00E35400, default),
            (36, 0x00FFFFFF, default),
        ):
            value = ctypes.c_uint(color if on else off)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(w.winId()), attr, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                return  # Windows 10: no caption colours or corners


class _Crt(QWidget):
    """The phone's CRT (XpChrome.kt Crt): a 1 px line at 30/255 black every 3 px, and on the main window the dark
    rim (clear to 60 % of the half diagonal, then 80/255 black at the corners). Clicks go through it."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rim = isinstance(window, QMainWindow)

    def cover(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()

    def paintEvent(self, event: Any) -> None:
        from aqt.qt import QBrush, QColor, QPainter, QPixmap, QRadialGradient

        if not hasattr(self, "lines"):
            tile = QPixmap(1, 3)
            tile.fill(QColor(0, 0, 0, 0))
            tp = QPainter(tile)
            tp.fillRect(0, 0, 1, 1, QColor(0, 0, 0, 30))
            tp.end()
            self.lines = QBrush(tile)
        p = QPainter(self)
        p.fillRect(self.rect(), self.lines)
        if self.rim:
            r = (self.width() ** 2 + self.height() ** 2) ** 0.5 / 2
            g = QRadialGradient(self.width() / 2, self.height() / 2, r)
            g.setColorAt(0, QColor(0, 0, 0, 0))
            g.setColorAt(0.6, QColor(0, 0, 0, 0))
            g.setColorAt(1, QColor(0, 0, 0, 80))
            p.fillRect(self.rect(), g)
