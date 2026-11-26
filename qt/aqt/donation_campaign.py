# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import re
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
from aqt.qt import QDateTime, QMessageBox, Qt
from aqt.utils import openFolder, openLink, show_info, tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt

CAMPAIGN_PAGE = "https://jschoreels.github.io/anki-rwkv/donations.html"
STATUS_URL = "https://jschoreels.github.io/anki-rwkv/data/donations.json"
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


def check_campaign(mw: AnkiQt) -> None:
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
                _invite(mw, campaign)

        mw.progress.single_shot(0, prompt)

    # A network check must not block the reviewer or collection operations.
    mw.taskman.run_in_background(_load_campaign, loaded, uses_collection=False)


def _ask(mw: AnkiQt, body: str, action: str) -> bool:
    dialog = QMessageBox(mw)
    dialog.setWindowTitle(tr.qt_misc_campaign_title())
    dialog.setTextFormat(Qt.TextFormat.RichText)
    dialog.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    dialog.setText(body)
    proceed = dialog.addButton(action, QMessageBox.ButtonRole.AcceptRole)
    stop = dialog.addButton(
        tr.qt_misc_campaign_stop(), QMessageBox.ButtonRole.ActionRole
    )
    later = dialog.addButton(
        tr.qt_misc_campaign_later(), QMessageBox.ButtonRole.RejectRole
    )
    dialog.setDefaultButton(later)
    dialog.setEscapeButton(later)
    dialog.exec()
    clicked = dialog.clickedButton()
    if clicked == proceed:
        return True
    mw.pm.profile[REMINDER] = None if clicked == stop else int_time() + 86_400
    mw.pm.save()
    return False


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
    if not _ask(
        mw, "<p>" + "</p><p>".join(paragraphs) + "</p>", tr.qt_misc_campaign_upload()
    ):
        return

    # The callback keeps the private directory alive until the donor finishes in Dropbox.
    directory = TemporaryDirectory(prefix="anki-donation-")
    path = Path(directory.name) / "collection.colpkg"

    def on_exported() -> None:
        with directory:
            openFolder(directory.name)
            openLink(campaign.request_url)
            body = (
                f"<p>{escape(tr.qt_misc_campaign_handoff())}</p>"
                f"<p>{escape(tr.qt_misc_campaign_file())}<br>{escape(str(path))}</p>"
                + _link(campaign.request_url, tr.qt_misc_campaign_open_dropbox())
            )
            if _ask(mw, body, tr.qt_misc_campaign_uploaded()):
                mw.pm.profile[REMINDER] = (
                    QDateTime.currentDateTime().addMonths(1).toSecsSinceEpoch()
                )
                mw.pm.save()
                show_info(tr.qt_misc_campaign_thanks(), parent=mw)

    export_collection_package(
        mw, mw, str(path), include_media=False, legacy=False, on_success=on_exported
    )
