# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import re
from concurrent.futures import Future
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from requests.exceptions import RequestException

from anki.httpclient import HttpClient
from anki.utils import int_time
from aqt.donation_sharing import SHARING, SharingDialog, describe_sharing
from aqt.qt import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLocale,
    QPushButton,
    Qt,
    QToolButton,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import openLink, show_info, tooltip, tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt

CAMPAIGN_PAGE = "https://jschoreels.github.io/anki-rwkv/donations.html"
STATUS_URL = "https://jschoreels.github.io/anki-rwkv/data/donations.json"
AUTO_UPLOAD = "donationCampaignAutomaticUpload"
COLLECTION_ID = "donationCampaignCollectionId"
RELAY_CONSENT = "donationCampaignLargeUploadConsent"
REMINDER = "donationCampaignReminder"


class DropboxRequestParser(HTMLParser):
    url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (url := dict(attrs).get("href")):
            if re.fullmatch(r"https://www\.dropbox\.com/request/[A-Za-z0-9]+/?", url):
                self.url = url


@dataclass(frozen=True)
class Campaign:
    request_url: str
    totals: tuple[int, int] | None


def _load_campaign() -> Campaign | None:
    with HttpClient() as client:
        client.timeout = 5
        parser = DropboxRequestParser()
        try:
            with client.get(CAMPAIGN_PAGE) as page:
                page.raise_for_status()
                parser.feed(page.text)
        except RequestException:
            return None
        if not parser.url:
            return None
        totals = None
        try:
            with client.get(STATUS_URL) as status:
                status.raise_for_status()
                data = status.json()["donations"]
                donors, reviews = data["donors_with_reviews"], data["reviews_unique"]
                if type(donors) is int and type(reviews) is int:
                    totals = (donors, reviews)
        except (RequestException, ValueError, KeyError, TypeError):
            pass  # The file request still works when the optional totals are unavailable.
        return Campaign(parser.url, totals)


def setup_support_menu(mw: AnkiQt) -> None:
    menu = mw.form
    sharing = menu.menuSupport.addAction(tr.qt_misc_campaign_preferences())
    menu.menuSupport.insertAction(menu.actionCampaignDetails, sharing)
    qconnect(sharing.triggered, lambda: SharingDialog(mw, mw).exec())

    def update() -> None:
        profile = mw.pm.profile
        available = profile is not None and bool(mw.col)
        menu.actionCampaignUpload.setEnabled(available)
        sharing.setEnabled(available)
        menu.actionStopMonthlyUploads.setEnabled(
            profile is not None and profile.get(AUTO_UPLOAD) is True
        )

    def stop_monthly() -> None:
        if (profile := mw.pm.profile) is not None:
            profile.pop(AUTO_UPLOAD, None)
            profile.pop(RELAY_CONSENT, None)
            mw.pm.save()

    qconnect(menu.menuSupport.aboutToShow, update)
    qconnect(
        menu.actionCampaignUpload.triggered, lambda: check_campaign(mw, manual=True)
    )
    qconnect(menu.actionCampaignDetails.triggered, lambda: openLink(CAMPAIGN_PAGE))
    qconnect(menu.actionStopMonthlyUploads.triggered, stop_monthly)


def check_campaign(
    mw: AnkiQt, *, profile_ready: Future[None] | None = None, manual: bool = False
) -> None:
    # donation_transfer imports this module, so it is imported late.
    from aqt import donation_transfer

    profile = mw.pm.profile
    if profile is None:
        return
    if manual and donation_transfer.busy():
        tooltip(tr.qt_misc_campaign_upload_in_progress(), parent=mw)
        return
    due = profile.get(REMINDER, 0)
    if not manual and (due is None or due > int_time()):
        return

    def loaded(result: Future[Campaign | None]) -> None:
        campaign = result.result()
        if campaign is None:
            if manual and mw.pm.profile is profile:
                show_info(tr.qt_misc_campaign_unavailable(), parent=mw)
            return

        def prompt() -> None:
            if (
                mw.pm.profile is profile
                and (manual or mw.state == "deckBrowser")
                and mw.isEnabled()
                and not mw._unload_profile_and_exit_pending
            ):
                if profile.get(REMINDER, 0) != due:
                    return
                if not manual and profile.get(AUTO_UPLOAD) is True:
                    donation_transfer.prepare(mw, automatic=True, scheduled=True)
                else:
                    _invite(mw, campaign)

        if profile_ready is None:
            mw.progress.single_shot(0, prompt)
        else:
            # Both completions run on the UI thread.
            profile_ready.add_done_callback(
                lambda _: mw.progress.single_shot(0, prompt)
            )

    # A network check must not block the reviewer or collection operations.
    if manual:
        mw.taskman.with_progress(
            _load_campaign, loaded, parent=mw, immediate=True, uses_collection=False
        )
    else:
        mw.taskman.run_in_background(_load_campaign, loaded, uses_collection=False)


class CampaignDialog(QDialog):
    STOP_REMINDERS = 2

    def __init__(self, mw: AnkiQt, body: str, action: str) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle(tr.qt_misc_campaign_title())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.content = QVBoxLayout(self)
        self.instructions = QLabel(body)
        self.instructions.setWordWrap(True)
        self.instructions.setOpenExternalLinks(True)
        self.content.addWidget(self.instructions)
        self.buttons = QDialogButtonBox(self)
        role = QDialogButtonBox.ButtonRole
        self.proceed = self.buttons.addButton(action, role.AcceptRole)
        stop = self.buttons.addButton(tr.qt_misc_campaign_stop(), role.ActionRole)
        later = self.buttons.addButton(tr.qt_misc_campaign_later(), role.RejectRole)
        later.setDefault(True)
        qconnect(self.buttons.accepted, self.accept)
        qconnect(self.buttons.rejected, self.reject)
        qconnect(stop.clicked, lambda: self.done(self.STOP_REMINDERS))

    def ask(self) -> bool:
        self.content.addWidget(self.buttons)
        profile = self.mw.pm.profile
        result = self.exec()
        if self.mw.pm.profile is not profile:
            return False
        if result == QDialog.DialogCode.Accepted:
            return True
        if result == self.STOP_REMINDERS:
            profile.pop(AUTO_UPLOAD, None)
            profile[REMINDER] = None
        elif profile.get(REMINDER, 0) is not None:
            profile[REMINDER] = int_time() + 86_400
        self.mw.pm.save()
        return False


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def _invite(mw: AnkiQt, campaign: Campaign) -> None:
    paragraphs = [
        escape(tr.qt_misc_campaign_invitation()),
    ]
    if campaign.totals is not None:
        donors, reviews = campaign.totals
        paragraphs.append(
            escape(
                tr.qt_misc_campaign_status(
                    donors_formatted=QLocale().toString(donors),
                    reviews_formatted=QLocale().toString(reviews),
                )
            )
        )
    paragraphs.append(_link(CAMPAIGN_PAGE, tr.qt_misc_campaign_details()))
    dialog = CampaignDialog(
        mw,
        f"<h2>{escape(tr.qt_misc_campaign_heading())}</h2><p>"
        + "</p><p>".join(paragraphs)
        + "</p>",
        tr.qt_misc_campaign_upload(),
    )
    dialog.setMinimumWidth(540)
    dialog.content.setContentsMargins(22, 18, 22, 16)
    dialog.content.setSpacing(12)

    group = QFrame()
    group.setObjectName("campaignSharing")
    group.setStyleSheet(
        "QFrame#campaignSharing { background: palette(alternate-base); border: 1px solid palette(mid); border-radius: 8px; }"
    )
    details = QVBoxLayout(group)
    details.setContentsMargins(14, 12, 14, 12)
    details.setSpacing(8)
    heading = QLabel(f"<b>{escape(tr.qt_misc_campaign_sharing())}</b>")
    details.addWidget(heading)
    summary = QLabel(tr.qt_misc_campaign_summary())
    summary.setWordWrap(True)
    details.addWidget(summary)
    sharing = QLabel()
    sharing.setObjectName("campaignSharingState")
    sharing.setTextFormat(Qt.TextFormat.PlainText)
    sharing.setWordWrap(True)
    details.addWidget(sharing)

    def show_sharing() -> None:
        sharing.setText(describe_sharing(mw.col, mw.pm.profile.get(SHARING)))

    def edit_sharing() -> None:
        if SharingDialog(mw, dialog).exec() == QDialog.DialogCode.Accepted:
            show_sharing()

    show_sharing()
    preferences = QPushButton(tr.qt_misc_campaign_preferences())
    details.addWidget(preferences, 0, Qt.AlignmentFlag.AlignLeft)
    qconnect(preferences.clicked, edit_sharing)
    transfer = QToolButton()
    transfer.setText(tr.qt_misc_campaign_transfer())
    transfer.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    transfer.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    transfer.setStyleSheet(
        "QToolButton { background: transparent; border: 1px solid transparent; border-radius: 3px; }"
        "QToolButton:focus { border-color: palette(highlight); }"
    )
    transfer.setArrowType(Qt.ArrowType.RightArrow)
    transfer.setCheckable(True)
    details.addWidget(transfer, 0, Qt.AlignmentFlag.AlignLeft)
    disclosure = QLabel(tr.qt_misc_campaign_consent())
    disclosure.setWordWrap(True)
    disclosure.hide()

    def toggle_transfer(expanded: bool) -> None:
        transfer.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        disclosure.setVisible(expanded)
        dialog.adjustSize()

    qconnect(transfer.toggled, toggle_transfer)
    details.addWidget(disclosure)
    dialog.content.addWidget(group)
    monthly = QCheckBox(tr.qt_misc_campaign_monthly())
    dialog.content.addWidget(monthly)
    help_text = QLabel(tr.qt_misc_campaign_monthly_help())
    help_text.setWordWrap(True)
    help_text.setContentsMargins(22, 0, 0, 0)
    dialog.content.addWidget(help_text)
    automatic = mw.pm.profile.get(AUTO_UPLOAD) is True
    monthly.setChecked(automatic)

    def choose(checked: bool) -> None:
        nonlocal automatic
        automatic = checked

    qconnect(monthly.toggled, choose)
    if dialog.ask():
        if not automatic:
            mw.pm.profile.pop(AUTO_UPLOAD, None)
            mw.pm.profile.pop(RELAY_CONSENT, None)
        from aqt import donation_transfer

        donation_transfer.prepare(mw, automatic=automatic)
