# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anki.decks import FilteredDeckConfig
from aqt import errors, rwkv_scheduler
from aqt.operations import CollectionOp
from aqt.operations.scheduling import FilteredDeckRwkvPolicy, RwkvFilteredDeckStateCold
from aqt.qt import QMessageBox, QWidget
from aqt.utils import ask_user_dialog


def run_filtered_deck_op_with_rwkv_check(
    *,
    mw: object,
    parent: QWidget,
    config: FilteredDeckConfig,
    make_op: Callable[[FilteredDeckRwkvPolicy], CollectionOp[Any]],
) -> None:
    """Ask before a desktop filtered-deck build needs a full historical replay."""

    need = rwkv_scheduler.filtered_deck_rwkv_need(config)
    logger = rwkv_scheduler.logger

    def failure(exc: Exception) -> None:
        if isinstance(exc, RwkvFilteredDeckStateCold):
            prompt()
        else:
            errors.show_exception(parent=parent, exception=exc)

    def run_with_state() -> None:
        make_op(FilteredDeckRwkvPolicy.REQUIRE_STATE).failure(
            failure
        ).run_in_background()

    def on_built(ready: bool) -> None:
        if ready:
            run_with_state()

    def on_choice(choice: int) -> None:
        if choice == 0:
            logger.info("RWKV filtered-deck choice: build state need=%s", need.value)
            rwkv_scheduler.build_rwkv_state_cache_with_progress(
                mw, record_retrievability_cache=False, on_done=on_built
            )
        elif choice == 1 and need is rwkv_scheduler.FilteredDeckRwkvNeed.ORDER:
            logger.info("RWKV filtered-deck choice: FSRS order need=%s", need.value)
            make_op(FilteredDeckRwkvPolicy.SKIP).run_in_background()
        else:
            logger.debug("RWKV filtered-deck choice: cancel need=%s", need.value)

    def prompt() -> None:
        logger.info("RWKV filtered-deck state prompt shown: need=%s", need.value)
        buttons: list[str | QMessageBox.StandardButton] = ["Build RWKV State"]
        if need is rwkv_scheduler.FilteredDeckRwkvNeed.ORDER:
            text = (
                "This filtered deck is sorted by retrievability, but the RWKV state "
                "is not ready. Building it replays your whole review history and "
                "can take several minutes.\n\n"
                "Build the RWKV state first (with progress) and then the deck, or "
                "build the deck now in FSRS retrievability order."
            )
            buttons.append("Build Deck Now in FSRS Order")
        else:
            text = (
                "This filtered deck's search uses RWKV values, so the RWKV state "
                "is needed to choose its cards, but it is not ready. Building it "
                "replays your whole review history and can take several minutes.\n\n"
                "Build the RWKV state first (with progress) and then the deck?"
            )
        buttons.append(QMessageBox.StandardButton.Cancel)
        ask_user_dialog(
            text,
            callback=on_choice,
            buttons=buttons,
            default_button=0,
            parent=parent,
            title="RWKV State",
        )

    if (
        need is rwkv_scheduler.FilteredDeckRwkvNeed.NONE
        or not rwkv_scheduler.filtered_deck_rwkv_state_cold(mw)
    ):
        run_with_state()
    else:
        prompt()
