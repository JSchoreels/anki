# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from zipfile import ZipFile

import pytest
from requests.exceptions import ConnectionError

import aqt
from anki.collection import Collection
from aqt import donation_campaign as campaign
from aqt.qt import QApplication, QDateTime, QMessageBox, QTimer, QWidget

REQUEST_URL = "https://www.dropbox.com/request/Test123"


def test_campaign_requires_a_dropbox_request_but_totals_are_optional(monkeypatch):
    page = Mock()
    client = Mock()
    http = MagicMock()
    http.__enter__.return_value = client
    monkeypatch.setattr(campaign, "HttpClient", lambda: http)
    response = MagicMock()
    response.__enter__.return_value = page
    page.text = '<a href="https://www.dropbox.com.example.org/request/x">Upload</a>'
    client.get.return_value = response
    assert campaign._load_campaign() is None

    page.text = f'<a href="{REQUEST_URL}">Upload</a>'
    client.get.side_effect = [response, ConnectionError()]
    assert campaign._load_campaign() == campaign.Campaign(REQUEST_URL, None)

    page.json.return_value = {
        "donations": {"donors_with_reviews": 5, "reviews_unique": 42}
    }
    client.get.side_effect = [response, response]
    assert campaign._load_campaign() == campaign.Campaign(REQUEST_URL, (5, 42))
    client.get.side_effect = ConnectionError()
    assert campaign._load_campaign() is None


@pytest.mark.parametrize(
    "choices",
    [
        ["Later"],
        ["Don’t remind me"],
        ["Upload my collection", "Later"],
        ["Upload my collection", "I’ve finished uploading"],
    ],
)
def test_campaign_buttons_and_dropbox_handoff(tmp_path, monkeypatch, choices):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mw = QWidget()
    mw.col = Collection(str(tmp_path / "collection.anki2"))
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    mw.state = "deckBrowser"
    mw._unload_profile_and_exit_pending = False
    mw.progress = SimpleNamespace(single_shot=lambda _, callback: callback())
    mw.reopen = mw.col.reopen
    mw._increase_background_ops = mw._decrease_background_ops = lambda: None

    def run(task, *args, on_done=None, **kwargs):
        future = Future()
        future.set_result(task())
        (on_done or args[0])(future)

    mw.taskman = SimpleNamespace(run_in_background=run, with_backend_progress=run)
    monkeypatch.setattr(aqt, "mw", mw)
    monkeypatch.setattr(campaign, "tr", mw.col.tr)
    monkeypatch.setattr(campaign, "int_time", lambda: 12345)
    lookup = Mock(return_value=campaign.Campaign(REQUEST_URL, (5, 42)))
    monkeypatch.setattr(campaign, "_load_campaign", lookup)
    received = []
    opened = []
    thanks = Mock()
    monkeypatch.setattr(campaign, "show_info", thanks)
    monkeypatch.setattr(campaign, "openLink", opened.append)

    def inspect_export(folder):
        path = Path(folder) / "collection.colpkg"
        with ZipFile(path) as package:
            assert "collection.anki21b" in package.namelist()
            assert not any(name.isdigit() for name in package.namelist())
        received.append(path)

    monkeypatch.setattr(campaign, "openFolder", inspect_export)
    exported = choices[0] == "Upload my collection"
    confirmed = choices[-1] == "I’ve finished uploading"
    prompts = []

    def click():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, QMessageBox)
        prompts.append(dialog.text())
        button_text = choices[len(prompts) - 1]
        if button_text == "Upload my collection":
            QTimer.singleShot(0, click)
        next(
            button for button in dialog.buttons() if button.text() == button_text
        ).click()

    try:
        mw.col.media.write_data("sample.mp3", b"media")
        note = mw.col.new_note(mw.col.models.current())
        note.fields[0] = "Synthetic note [sound:sample.mp3]"
        mw.col.add_note(note, 1)
        QTimer.singleShot(0, click)
        before = QDateTime.currentDateTime().addMonths(1).toSecsSinceEpoch()
        campaign.check_campaign(mw)
        assert mw.col.tr.qt_misc_campaign_status(count=5, reviews=42) in prompts[0]
        assert opened == ([REQUEST_URL] if exported else [])
        assert thanks.call_count == int(confirmed)
        if exported:
            assert len(received) == 1 and not received[0].exists()
            assert mw.col.get_note(note.id).fields == note.fields
        due = mw.pm.profile[campaign.REMINDER]
        if confirmed:
            assert (
                before
                <= due
                <= QDateTime.currentDateTime().addMonths(1).toSecsSinceEpoch()
            )
        elif choices[-1] == "Later":
            assert due == 12345 + 86_400
        else:
            assert due is None
        campaign.check_campaign(mw)
        lookup.assert_called_once()
    finally:
        mw.col.close()
        mw.close()
