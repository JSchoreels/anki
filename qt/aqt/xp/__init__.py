# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""kuma3: Anki XP on the desktop (code\\kumapie_pc_plan.md, "kuma3 desktop"): the phone's XP game in the reviewer,
sharing one XP and streak with the phone and kuma3 Skins through the progress notes. Tools > Anki XP turns it off."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt import gui_hooks
from aqt.qt import QAction, qconnect

from .home import XpHome
from .look import XpLook
from .review import XpReview

if TYPE_CHECKING:
    from aqt.main import AnkiQt


def setup(mw: AnkiQt) -> XpReview:
    review = XpReview(mw)
    review.home = XpHome(review)
    review.look = XpLook(review)
    action = QAction("Anki XP", mw)
    action.setCheckable(True)
    action.setToolTip(
        "The XP game in reviews: points, combos, quests, boss cards, trophies"
    )

    def opened() -> None:
        action.setChecked(review.on)
        # the toolbar was drawn before the profile (and its Anki XP switch) was known
        mw.toolbar.draw()

    gui_hooks.profile_did_open.append(opened)

    def toggled(on: bool) -> None:
        if on == review.on:
            return  # the profile opening: nothing changed
        review.set_on(on)
        mw.toolbar.draw()
        mw.reset()  # the screen again, with or without the XP look

    qconnect(action.toggled, toggled)
    mw.form.menuTools.addSeparator()
    mw.form.menuTools.addAction(action)
    return review
