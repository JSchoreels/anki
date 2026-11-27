# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import re
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from requests.exceptions import RequestException

from anki.httpclient import HttpClient
from anki.utils import int_time
from aqt.import_export.exporting import export_collection_package
from aqt.qt import (
    QCheckBox,
    QDateTime,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    Qt,
    QUrl,
    QVBoxLayout,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineView,
    QWidget,
    pyqtSignal,
    qconnect,
)
from aqt.utils import show_info, tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt

CAMPAIGN_PAGE = "https://jschoreels.github.io/anki-rwkv/donations.html"
STATUS_URL = "https://jschoreels.github.io/anki-rwkv/data/donations.json"
UPLOAD_BROKER = (
    "https://anki-rwkv-donations.jonathan-schoreels.workers.dev/v1/upload-link"
)
AUTO_UPLOAD = "donationCampaignAutomaticUpload"
MAX_UPLOAD_BYTES = 150_000_000
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


def check_campaign(mw: AnkiQt, *, profile_ready: Future[None] | None = None) -> None:
    profile = mw.pm.profile
    due = profile.get(REMINDER, 0)
    if due is None or due > int_time():
        return

    def loaded(result: Future[Campaign | None]) -> None:
        campaign = result.result()
        if campaign is None:
            return

        def prompt() -> None:
            if (
                mw.pm.profile is profile
                and mw.state == "deckBrowser"
                and mw.isEnabled()
                and not mw._unload_profile_and_exit_pending
            ):
                if profile.get(REMINDER, 0) != due:
                    return
                if profile.get(AUTO_UPLOAD) is True:
                    _prepare(mw, campaign, automatic=True)
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
        profile[REMINDER] = (
            None if result == self.STOP_REMINDERS else int_time() + 86_400
        )
        self.mw.pm.save()
        return False


class DropboxRequestPage(QWebEnginePage):
    file_selected = pyqtSignal()

    def __init__(
        self, profile: QWebEngineProfile, request_url: QUrl, path: Path, parent: QWidget
    ) -> None:
        super().__init__(profile, parent)
        self.request_url = request_url
        self.path = path

    def _is_request(self, url: QUrl) -> bool:
        return (
            url.scheme() == "https"
            and url.authority() == "www.dropbox.com"
            and url.path().rstrip("/") == self.request_url.path().rstrip("/")
        )

    def acceptNavigationRequest(
        self, url: QUrl, kind: QWebEnginePage.NavigationType, is_main_frame: bool
    ) -> bool:
        # Qt's file chooser callback does not identify the requesting frame.
        return is_main_frame and self._is_request(url)

    def chooseFiles(
        self,
        mode: QWebEnginePage.FileSelectionMode,
        old_files: Iterable[str | None],
        accepted_mime_types: Iterable[str | None],
    ) -> list[str]:
        if self._is_request(self.url()) and mode in (
            self.FileSelectionMode.FileSelectOpen,
            self.FileSelectionMode.FileSelectOpenMultiple,
        ):
            self.file_selected.emit()
            return [str(self.path)]
        return []


def _show_upload_form(mw: AnkiQt, request_url: str, path: Path) -> bool:
    dialog = CampaignDialog(
        mw, tr.qt_misc_campaign_handoff(), tr.qt_misc_campaign_uploaded()
    )
    view = QWebEngineView(dialog)
    # The view owns the page and is destroyed before this private profile.
    profile = QWebEngineProfile(dialog)
    page = DropboxRequestPage(profile, QUrl(request_url), path, view)
    view.setPage(page)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    dialog.content.addWidget(view, 1)
    dialog.proceed.setEnabled(False)
    qconnect(page.file_selected, lambda: dialog.proceed.setEnabled(True))
    dialog.resize(850, 700)
    view.load(page.request_url)
    return dialog.ask()


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def _invite(mw: AnkiQt, campaign: Campaign) -> None:
    paragraphs = [escape(tr.qt_misc_campaign_invitation())]
    if campaign.totals is not None:
        donors, reviews = campaign.totals
        paragraphs.append(
            escape(tr.qt_misc_campaign_status(count=donors, reviews=reviews))
        )
    paragraphs.extend(
        (
            _link(CAMPAIGN_PAGE, tr.qt_misc_campaign_details()),
            escape(tr.qt_misc_campaign_consent()),
        )
    )
    dialog = CampaignDialog(
        mw, "<p>" + "</p><p>".join(paragraphs) + "</p>", tr.qt_misc_campaign_upload()
    )
    monthly = QCheckBox(tr.qt_misc_campaign_monthly())
    dialog.content.addWidget(monthly)
    # The dialog deletes its children on close.
    automatic = False

    def choose(checked: bool) -> None:
        nonlocal automatic
        automatic = checked

    qconnect(monthly.toggled, choose)
    if dialog.ask():
        _prepare(mw, campaign, automatic=automatic)


def _prepare(mw: AnkiQt, campaign: Campaign, *, automatic: bool) -> None:
    profile = mw.pm.profile
    if automatic:
        # Defer another attempt if export fails or the app quits mid-transfer.
        profile[REMINDER] = int_time() + 86_400
        mw.pm.save()
    # The callback keeps the private directory alive until the donor finishes in Dropbox.
    directory = TemporaryDirectory(prefix="anki-donation-")
    path = Path(directory.name) / "collection.colpkg"

    def on_exported() -> None:
        with directory:
            if mw.pm.profile is not profile:
                return
            native = automatic and path.stat().st_size <= MAX_UPLOAD_BYTES
            if automatic and not native:
                profile.pop(AUTO_UPLOAD, None)
                mw.pm.save()
                show_info(tr.qt_misc_campaign_too_large(), parent=mw)
            if native:
                success = _upload(mw, path)
            else:
                success = _show_upload_form(mw, campaign.request_url, path)
            if success and mw.pm.profile is profile:
                if native:
                    profile[AUTO_UPLOAD] = True
                profile[REMINDER] = (
                    QDateTime.currentDateTime().addMonths(1).toSecsSinceEpoch()
                )
                mw.pm.save()
                show_info(tr.qt_misc_campaign_thanks(), parent=mw)

    export_collection_package(
        mw, mw, str(path), include_media=False, legacy=False, on_success=on_exported
    )


def _upload(mw: AnkiQt, path: Path) -> bool:
    from aqt.donation_upload import CampaignUpload

    dialog = CampaignDialog(mw, tr.qt_misc_campaign_uploading(), "")
    dialog.proceed.hide()
    bar = QProgressBar(dialog)
    bar.setRange(0, 0)
    dialog.content.addWidget(bar)
    upload = CampaignUpload(path, UPLOAD_BROKER, {}, MAX_UPLOAD_BYTES)

    def show_progress(percent: int) -> None:
        bar.setRange(0, 100)
        bar.setValue(percent)

    qconnect(upload.progress, show_progress)
    qconnect(upload.done, dialog.done)
    upload.start()
    try:
        accepted = dialog.ask()
    finally:
        upload.stop()
    if upload.failed:
        show_info(tr.qt_misc_campaign_upload_failed(), parent=mw)
    return accepted
