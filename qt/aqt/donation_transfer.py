# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Export, upload and bookkeeping for campaign donations.

Manual uploads show one TransferDialog from export to receipt. Scheduled
monthly uploads run without any dialog and fail quietly until several attempts
in a row have failed."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from uuid import uuid4

from anki.utils import int_time
from aqt import gui_hooks
from aqt.donation_campaign import (
    AUTO_UPLOAD,
    COLLECTION_ID,
    RELAY_CONSENT,
    REMINDER,
    CampaignDialog,
)
from aqt.donation_sharing import SHARING, Sharing, current_choices, export_campaign
from aqt.errors import show_exception
from aqt.operations import QueryOp
from aqt.qt import (
    QDateTime,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import show_info, tooltip, tr

if TYPE_CHECKING:
    from anki.collection import Collection
    from aqt.donation_upload import CampaignUpload
    from aqt.main import AnkiQt

UPLOAD_BROKER = (
    "https://anki-rwkv-donations.jonathan-schoreels.workers.dev/v1/upload-link"
)
MAX_DIRECT_BYTES = 150_000_000
FAILED_UPLOADS = "donationCampaignFailedUploads"
LAST_REVIEW = "donationCampaignLastReview"
# Scheduled uploads report a failure only once this many fail in a row.
FAILURES_BEFORE_NOTICE = 3

logger = logging.getLogger(__name__)
_active: Transfer | None = None


def busy() -> bool:
    return _active is not None


def _cancel_active() -> None:
    if _active is not None:
        _active.cancel()


# Closing the profile or quitting must not leave an upload running.
gui_hooks.profile_will_close.append(_cancel_active)


def _next_month() -> int:
    return QDateTime.currentDateTime().addMonths(1).toSecsSinceEpoch()


def _collection_id(col: Collection) -> str:
    value = col.get_config(COLLECTION_ID)
    if isinstance(value, str) and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
    ):
        return value
    value = str(uuid4())
    col.set_config(COLLECTION_ID, value)
    return value


def prepare(mw: AnkiQt, *, automatic: bool, scheduled: bool = False) -> None:
    if _active is not None:
        if not scheduled:
            tooltip(tr.qt_misc_campaign_upload_in_progress(), parent=mw)
        return
    profile = mw.pm.profile
    newest = mw.col.db.scalar("select max(id) from revlog") or 0
    if scheduled and profile.get(LAST_REVIEW) == newest:
        # Nothing new to learn from since the last donation.
        logger.info("Skipping the monthly campaign upload: no new reviews")
        profile[REMINDER] = _next_month()
        mw.pm.save()
        return
    Transfer(mw, newest, automatic=automatic, scheduled=scheduled).start()


def _export(
    mw: AnkiQt,
    path: Path,
    choices: Sharing,
    collection_id: str,
    on_success: Callable[[], None],
    on_failure: Callable[[Exception], None],
) -> None:
    # No progress window: manual uploads show their own dialog, and scheduled
    # uploads stay in the background. The export still holds the collection.
    QueryOp(
        parent=mw,
        op=lambda col: export_campaign(col, str(path), choices, collection_id),
        success=lambda _: on_success(),
    ).failure(on_failure).run_in_background()


def _upload(path: Path, collection_id: str) -> CampaignUpload:
    from aqt.donation_upload import CampaignUpload

    return CampaignUpload(
        path, UPLOAD_BROKER, {"X-Collection-ID": collection_id}, MAX_DIRECT_BYTES
    )


class TransferDialog(QDialog):
    """Progress of a manual upload, from export to receipt."""

    def __init__(self, mw: AnkiQt) -> None:
        super().__init__(mw)
        self.setWindowTitle(tr.qt_misc_campaign_title())
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self.label = QLabel(tr.qt_misc_campaign_preparing())
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        # Some native styles omit the progress bar's text. Show it in the label.
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        qconnect(self.buttons.rejected, self.reject)
        layout.addWidget(self.buttons)

    def show_progress(self, percent: int) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(percent)
        self.label.setText(
            tr.qt_misc_campaign_waiting_for_receipt()
            if percent == 100
            else tr.qt_misc_campaign_uploading(percent=percent)
        )


class Transfer:
    def __init__(
        self, mw: AnkiQt, newest_review: int, *, automatic: bool, scheduled: bool
    ) -> None:
        self.mw = mw
        self.profile = mw.pm.profile
        self.newest_review = newest_review
        self.automatic = automatic
        self.scheduled = scheduled
        self.directory = TemporaryDirectory(prefix="anki-donation-")
        self.path = Path(self.directory.name) / "collection.colpkg"
        self.exporting = False
        self.needs_consent = False
        self.upload: CampaignUpload | None = None
        self.dialog: TransferDialog | None = None

    def start(self) -> None:
        global _active
        _active = self
        profile = self.profile
        # Defer another attempt if export fails or the app quits mid-transfer.
        if profile.get(REMINDER, 0) is not None:
            profile[REMINDER] = int_time() + 86_400
        self.choices = current_choices(self.mw.col.models.all(), profile.get(SHARING))
        profile[SHARING] = self.choices
        self.mw.pm.save()
        self.collection_id = _collection_id(self.mw.col)
        if not self.scheduled:
            self.dialog = TransferDialog(self.mw)
            # Cancelling keeps the one-day reminder set above, like "Later".
            qconnect(self.dialog.rejected, self.cancel)
            self.dialog.open()
        self.exporting = True
        _export(
            self.mw,
            self.path,
            self.choices,
            self.collection_id,
            self._exported,
            self._export_failed,
        )

    def _close(self) -> None:
        global _active
        if _active is self:
            _active = None
        if self.upload is not None:
            # Release the archive before removing its directory.
            self.upload.stop()
        if self.dialog is not None:
            dialog, self.dialog = self.dialog, None
            if dialog.isVisible():
                dialog.accept()
            dialog.deleteLater()
        if not self.exporting:
            self.directory.cleanup()

    def cancel(self) -> None:
        if _active is self:
            # A running export cannot be interrupted. Its callback removes the
            # directory once the export ends.
            self._close()

    def _export_failed(self, exc: Exception) -> None:
        self.exporting = False
        if _active is not self:
            self.directory.cleanup()
            return
        logger.warning("Campaign export failed (%s)", type(exc).__name__)
        self._finish(False)
        if not self.scheduled and self.mw.pm.profile is self.profile:
            show_exception(parent=self.mw, exception=exc)

    def _exported(self) -> None:
        self.exporting = False
        profile = self.profile
        if (
            _active is not self
            or self.mw.pm.profile is not profile
            or profile.get(SHARING) != self.choices
            or (self.scheduled and profile.get(AUTO_UPLOAD) is not True)
        ):
            self._close()
            return
        self.needs_consent = (
            self.scheduled
            and self.path.stat().st_size > MAX_DIRECT_BYTES
            and not profile.get(RELAY_CONSENT)
        )
        if self.needs_consent:
            # Asked once, so this one dialog may interrupt a background upload.
            consent = CampaignDialog(
                self.mw,
                tr.qt_misc_campaign_large_monthly_consent(),
                tr.qt_misc_campaign_upload(),
            ).ask()
            if not consent or _active is not self:
                self._close()
                return
        self.upload = _upload(self.path, self.collection_id)
        if self.dialog is not None:
            self.dialog.show_progress(0)
            qconnect(self.upload.progress, self.dialog.show_progress)
        qconnect(
            self.upload.done,
            lambda code: self._finish(code == QDialog.DialogCode.Accepted),
        )
        self.upload.start()

    def _finish(self, success: bool) -> None:
        if _active is not self:
            return
        self._close()
        profile = self.profile
        if self.mw.pm.profile is not profile:
            return
        if success:
            if self.automatic and not self.scheduled:
                profile[AUTO_UPLOAD] = True
                profile[RELAY_CONSENT] = True
            elif self.needs_consent:
                profile[RELAY_CONSENT] = True
            if self.automatic or profile.get(REMINDER, 0) is not None:
                profile[REMINDER] = _next_month()
            profile[LAST_REVIEW] = self.newest_review
            profile.pop(FAILED_UPLOADS, None)
            self.mw.pm.save()
            if self.scheduled:
                tooltip(tr.qt_misc_campaign_thanks(), period=8000, parent=self.mw)
            else:
                show_info(tr.qt_misc_campaign_thanks(), parent=self.mw)
            return
        if not self.scheduled:
            if self.upload is not None:
                show_info(tr.qt_misc_campaign_upload_failed(), parent=self.mw)
            return
        # The reminder set in start() retries tomorrow.
        failures = profile.get(FAILED_UPLOADS, 0) + 1
        logger.warning("Scheduled campaign upload failed (%d in a row)", failures)
        if failures < FAILURES_BEFORE_NOTICE:
            profile[FAILED_UPLOADS] = failures
            self.mw.pm.save()
            return
        profile.pop(FAILED_UPLOADS, None)
        self.mw.pm.save()
        show_info(tr.qt_misc_campaign_upload_failed(), parent=self.mw)
