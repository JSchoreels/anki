# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from zipfile import ZipFile

import pytest
from requests.exceptions import ConnectionError

import aqt
from anki.collection import Collection
from aqt import donation_campaign as campaign
from aqt.qt import QApplication, QDateTime, QTimer, QWidget

REQUEST_URL = "https://www.dropbox.com/request/Test123"


@pytest.mark.parametrize("profile_ready_first", [True, False])
def test_invitation_waits_for_both_campaign_and_profile(
    profile_ready_first, monkeypatch
):
    mw = Mock()
    mw.pm.profile = {}
    mw.state = "deckBrowser"
    mw._unload_profile_and_exit_pending = False
    mw.progress.single_shot.side_effect = lambda _, callback: callback()
    invite = Mock()
    monkeypatch.setattr(campaign, "_invite", invite)
    profile_ready = Future()
    result = Future()
    result.set_result(campaign.Campaign(REQUEST_URL, None))

    campaign.check_campaign(mw, profile_ready=profile_ready)
    loaded = mw.taskman.run_in_background.call_args.args[1]
    if profile_ready_first:
        profile_ready.set_result(None)
        invite.assert_not_called()
        loaded(result)
    else:
        loaded(result)
        invite.assert_not_called()
        profile_ready.set_result(None)
    invite.assert_called_once_with(mw, result.result())


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
    thanks = Mock()
    monkeypatch.setattr(campaign, "show_info", thanks)

    def inspect_export(parent, request_url, path):
        assert request_url == REQUEST_URL
        with ZipFile(path) as package:
            assert "collection.anki21b" in package.namelist()
            assert not any(name.isdigit() for name in package.namelist())
        received.append(path)
        return campaign.CampaignDialog(
            parent, "Dropbox", mw.col.tr.qt_misc_campaign_uploaded()
        ).ask()

    monkeypatch.setattr(campaign, "_show_upload_form", inspect_export)
    exported = choices[0] == "Upload my collection"
    confirmed = choices[-1] == "I’ve finished uploading"
    prompts = []

    def click():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, campaign.CampaignDialog)
        prompts.append(dialog.instructions.text())
        button_text = choices[len(prompts) - 1]
        if button_text == "Upload my collection":
            QTimer.singleShot(0, click)
        next(
            button
            for button in dialog.buttons.buttons()
            if button.text() == button_text
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
        assert bool(received) == exported
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


@pytest.mark.parametrize("valid_receipt", [True, False])
def test_native_upload_requires_dropbox_receipt(tmp_path, monkeypatch, valid_receipt):
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from aqt import donation_upload

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(["anki-upload-test"])
    uploaded = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/v1/upload-link":
                response = {"link": origin + "/collection"}
            else:
                uploaded.append((self.path, self.headers["Content-Type"], body))
                response = {"content-hash": "ab" * 32} if valid_receipt else {}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"

    path = tmp_path / "collection.colpkg"
    path.write_bytes(b"synthetic archive")
    mw = QWidget()
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    monkeypatch.setattr(campaign, "UPLOAD_BROKER", origin + "/v1/upload-link")
    monkeypatch.setattr(
        donation_upload, "DROPBOX_UPLOAD_PREFIX", origin + "/collection"
    )
    monkeypatch.setattr(campaign, "tr", Mock())
    campaign.tr.qt_misc_campaign_title.return_value = "Campaign"
    campaign.tr.qt_misc_campaign_stop.return_value = "Don’t remind me"
    campaign.tr.qt_misc_campaign_later.return_value = "Later"
    campaign.tr.qt_misc_campaign_uploading.return_value = "Uploading"
    warning = Mock()
    monkeypatch.setattr(campaign, "show_info", warning)
    try:
        assert campaign._upload(mw, path) is valid_receipt
        assert uploaded == [
            ("/collection", "application/octet-stream", path.read_bytes())
        ]
        assert warning.call_count == int(not valid_receipt)
    finally:
        server.shutdown()
        server.server_close()
        mw.close()
        app.processEvents()


def test_monthly_upload_enrollment_and_due_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(["anki-campaign-monthly"])
    mw = QWidget()
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    mw.state = "deckBrowser"
    mw._unload_profile_and_exit_pending = False
    mw.progress = SimpleNamespace(single_shot=lambda _, f: f())

    def run(task, done, **kwargs):
        result = Future()
        result.set_result(task())
        done(result)

    mw.taskman = SimpleNamespace(run_in_background=run)
    col = Collection(str(tmp_path / "test.anki2"))
    monkeypatch.setattr(campaign, "tr", col.tr)
    monkeypatch.setattr(
        campaign, "_load_campaign", lambda: campaign.Campaign(REQUEST_URL, None)
    )
    notify = Mock()
    monkeypatch.setattr(campaign, "show_info", notify)

    def export(parent, window, path, *, include_media, legacy, on_success):
        assert not include_media and not legacy
        col.export_collection_package(path, include_media, legacy)
        col.reopen()
        on_success()

    monkeypatch.setattr(campaign, "export_collection_package", export)
    upload = Mock(return_value=True)
    monkeypatch.setattr(campaign, "_upload", upload)

    def enroll():
        dialog = app.activeModalWidget()
        monthly = dialog.findChild(campaign.QCheckBox)
        assert not monthly.isChecked()
        monthly.click()
        dialog.proceed.click()

    try:
        QTimer.singleShot(0, enroll)
        campaign.check_campaign(mw)
        assert mw.pm.profile[campaign.AUTO_UPLOAD] is True
        campaign.check_campaign(mw)
        assert upload.call_count == 1
        mw.pm.profile[campaign.REMINDER] = 0
        campaign.check_campaign(mw)
        assert upload.call_count == notify.call_count == 2
        assert all(not call.args[1].exists() for call in upload.call_args_list)
    finally:
        col.close()
        mw.close()
