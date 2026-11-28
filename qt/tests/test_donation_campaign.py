# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID
from zipfile import ZipFile

import pytest
from requests.exceptions import ConnectionError

import aqt
from anki.collection import Collection
from aqt import donation_campaign as campaign
from aqt import donation_transfer as transfer
from aqt.preferences import Preferences
from aqt.qt import (
    QApplication,
    QDateTime,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QObject,
    QTimer,
    QWidget,
    pyqtSignal,
)

REQUEST_URL = "https://www.dropbox.com/request/Test123"


class FakeUpload(QObject):
    """Stands in for CampaignUpload. `start` reports `result` at once unless it is None."""

    progress = pyqtSignal(int)
    done = pyqtSignal(int)

    def __init__(self, path, collection_id, inspect=None, result=True):
        super().__init__()
        self.path = path
        self.collection_id = collection_id
        self.inspect = inspect
        self.result = result
        self.started = self.stopped = False

    def start(self):
        self.started = True
        if self.inspect:
            self.inspect(self.path, self.collection_id)
        if self.result is not None:
            self.finish(self.result)

    def finish(self, success):
        code = QDialog.DialogCode.Accepted if success else QDialog.DialogCode.Rejected
        self.done.emit(code)

    def stop(self):
        self.stopped = True


def use_tr(monkeypatch, col):
    for module in (campaign, transfer):
        monkeypatch.setattr(module, "tr", col.tr)


def add_review(col):
    # A synthetic review, so the scheduled upload sees new history.
    newest = col.db.scalar("select max(id) from revlog") or 0
    col.db.execute(
        "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)"
        " values (?, 1, 0, 3, 1, 0, 2500, 1000, 1)",
        max(newest + 1, 1_700_000_000_000),
    )


def wait_until_idle(app, seconds=10):
    deadline = QDateTime.currentMSecsSinceEpoch() + seconds * 1000
    while transfer.busy():
        assert QDateTime.currentMSecsSinceEpoch() < deadline
        app.processEvents()


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
        ["Upload my collection"],
    ],
)
def test_campaign_invitation_and_media_free_export(tmp_path, monkeypatch, choices):
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

    mw.taskman = SimpleNamespace(run_in_background=run, with_progress=run)
    monkeypatch.setattr(aqt, "mw", mw)
    use_tr(monkeypatch, mw.col)
    monkeypatch.setattr(campaign, "int_time", lambda: 12345)
    lookup = Mock(return_value=campaign.Campaign(REQUEST_URL, (5, 42)))
    monkeypatch.setattr(campaign, "_load_campaign", lookup)
    received = []
    thanks = Mock()
    monkeypatch.setattr(transfer, "show_info", thanks)

    def inspect_export(path, collection_id):
        assert UUID(collection_id).version == 4
        assert collection_id == mw.col.get_config(campaign.COLLECTION_ID)
        with ZipFile(path) as package:
            assert "collection.anki21b" in package.namelist()
            assert not any(name.isdigit() for name in package.namelist())
        received.append(path)

    monkeypatch.setattr(
        transfer, "_upload", lambda *args: FakeUpload(*args, inspect=inspect_export)
    )
    confirmed = choices == ["Upload my collection"]
    prompts = []

    def click():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, campaign.CampaignDialog)
        prompts.append(dialog.instructions.text())
        button_text = choices[len(prompts) - 1]
        if len(prompts) < len(choices):
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
        assert (
            mw.col.tr.qt_misc_campaign_status(
                donors_formatted="5", reviews_formatted="42"
            )
            in prompts[0]
        )
        assert bool(received) == confirmed
        assert (mw.col.get_config(campaign.COLLECTION_ID) is not None) == confirmed
        assert thanks.call_count == int(confirmed)
        if confirmed:
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


@pytest.mark.parametrize("chunked", [True, False])
@pytest.mark.parametrize("valid_receipt", [True, False])
def test_one_time_upload_from_invitation(tmp_path, monkeypatch, valid_receipt, chunked):
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from aqt import donation_upload

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(["anki-upload-test"])
    synced_id = "f8098c7e-e2cd-461d-a57f-a9155a404e4d"
    broker_ids = []
    uploaded = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path in ("/v1/upload-link", "/v1/upload-session"):
                broker_ids.append(self.headers.get("X-Collection-ID"))
            if self.path == "/v1/upload-link":
                response = {"link": origin + "/collection"}
            elif self.path == "/v1/upload-session":
                assert int(self.headers["X-Upload-Size"]) == len(b"synthetic archive")
                response = {"offset": 0, "session": "cursor-0", "chunk_size": 8}
            else:
                uploaded.append((self.path, self.headers["Content-Type"], body))
                offset = sum(len(chunk[2]) for chunk in uploaded)
                if chunked:
                    assert (
                        self.headers["Authorization"]
                        == f"Bearer cursor-{offset - len(body)}"
                    )
                if chunked and offset < len(b"synthetic archive"):
                    response = {
                        "offset": offset,
                        "session": f"cursor-{offset}",
                        "chunk_size": 8,
                    }
                else:
                    response = {"content-hash": "ab" * 32} if valid_receipt else {}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    progress = []
    show_progress = transfer.TransferDialog.show_progress

    def record_progress(dialog, percent):
        show_progress(dialog, percent)
        progress.append((dialog.bar.maximum(), percent, dialog.label.text()))

    mw = QWidget()
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    col = Collection(str(tmp_path / "collection.anki2"))
    mw.col = col
    col.set_config(campaign.COLLECTION_ID, synced_id)
    use_tr(monkeypatch, col)
    if chunked:
        monkeypatch.setattr(transfer, "MAX_DIRECT_BYTES", 8)
    monkeypatch.setattr(transfer, "UPLOAD_BROKER", origin + "/v1/upload-link")
    monkeypatch.setattr(
        donation_upload, "DROPBOX_UPLOAD_PREFIX", origin + "/collection"
    )
    monkeypatch.setattr(transfer.TransferDialog, "show_progress", record_progress)

    def export(parent, path, choices, collection_id, on_success, on_failure):
        # One dialog shows the whole transfer, starting with the export.
        dialog = mw.findChild(transfer.TransferDialog)
        assert dialog.isVisible()
        assert dialog.label.text() == col.tr.qt_misc_campaign_preparing()
        assert (dialog.bar.minimum(), dialog.bar.maximum()) == (0, 0)
        # The transfer dialog offers only Cancel, not the reminder choices.
        assert [
            dialog.buttons.standardButton(button) for button in dialog.buttons.buttons()
        ] == [QDialogButtonBox.StandardButton.Cancel]
        path.write_bytes(b"synthetic archive")
        on_success()

    monkeypatch.setattr(transfer, "_export", export)
    notify = Mock()
    monkeypatch.setattr(transfer, "show_info", notify)

    def choose_one_time():
        dialog = app.activeModalWidget()
        assert dialog.proceed.text() == "Upload my collection"
        assert not dialog.findChild(campaign.QCheckBox).isChecked()
        dialog.proceed.click()

    try:
        QTimer.singleShot(0, choose_one_time)
        campaign._invite(mw, campaign.Campaign(REQUEST_URL, None))
        wait_until_idle(app)
        assert broker_ids == [synced_id]
        assert progress[0] == (100, 0, col.tr.qt_misc_campaign_uploading(percent=0))
        assert progress[-1] == (100, 100, col.tr.qt_misc_campaign_waiting_for_receipt())
        if chunked:
            assert [value for _, value, _ in progress] == [0, 47, 94, 100]
            assert progress[1][2] == col.tr.qt_misc_campaign_uploading(percent=47)
        endpoint = "/v1/upload-chunk" if chunked else "/collection"
        assert all(
            path == endpoint and mime == "application/octet-stream"
            for path, mime, _ in uploaded
        )
        assert [entry[2] for entry in uploaded] == (
            [b"syntheti", b"c archiv", b"e"] if chunked else [b"synthetic archive"]
        )
        assert not mw.pm.profile.get(campaign.AUTO_UPLOAD)
        message = (
            col.tr.qt_misc_campaign_thanks()
            if valid_receipt
            else col.tr.qt_misc_campaign_upload_failed()
        )
        notify.assert_called_once_with(message, parent=mw)
    finally:
        col.close()
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
    mw.col = col
    use_tr(monkeypatch, col)
    monkeypatch.setattr(
        campaign, "_load_campaign", lambda: campaign.Campaign(REQUEST_URL, None)
    )
    notify = Mock()
    monkeypatch.setattr(transfer, "show_info", notify)
    tip = Mock()
    monkeypatch.setattr(transfer, "tooltip", tip)

    note = col.new_note(col.models.by_name("Basic"))
    note.fields = ["excluded front", "included back"]
    col.add_note(note, 1)

    def export(parent, path, choices, collection_id, on_success, on_failure):
        transfer.export_campaign(col, str(path), choices, collection_id)
        on_success()

    monkeypatch.setattr(transfer, "_export", export)

    def inspect_upload(path, collection_id):
        from anki._backend import RustBackend

        # Read the actual outgoing package into a new, synthetic test collection.
        folder = tmp_path / str(upload.call_count)
        folder.mkdir()
        db = str(folder / "copy.anki2")
        RustBackend().import_collection_package(
            backup_path=str(path),
            col_path=db,
            media_folder=str(folder / "media"),
            media_db=str(folder / "media.db"),
        )
        copy = Collection(db)
        try:
            assert copy.get_note(note.id).fields == ["", "included back"]
            assert not copy.models.by_name("Basic")["tmpls"][0]["qfmt"]
            assert copy.get_config(campaign.COLLECTION_ID) == collection_id
        finally:
            copy.close()

    upload = Mock(side_effect=inspect_upload)
    monkeypatch.setattr(
        transfer, "_upload", lambda *args: FakeUpload(*args, inspect=upload)
    )

    def enroll():
        dialog = app.activeModalWidget()
        monthly = dialog.findChild(campaign.QCheckBox)
        assert not monthly.isChecked()
        from aqt.qt import QDialogButtonBox, QPushButton, Qt

        def customize():
            editor = app.activeModalWidget()
            assert isinstance(editor, campaign.SharingDialog)
            (basic,) = editor.types.findItems("Basic", Qt.MatchFlag.MatchExactly)
            editor.types.setCurrentItem(basic)
            editor.fields.item(0).setCheckState(Qt.CheckState.Unchecked)
            editor.templates.item(0).setCheckState(Qt.CheckState.Unchecked)
            buttons = editor.findChild(QDialogButtonBox).buttons()
            next(b for b in buttons if b.text() == "Save preferences").click()

        QTimer.singleShot(0, customize)
        next(
            b
            for b in dialog.findChildren(QPushButton)
            if b.text() == "Sharing preferences"
        ).click()
        monthly.click()
        dialog.proceed.click()

    try:
        QTimer.singleShot(0, enroll)
        campaign.check_campaign(mw)
        assert mw.pm.profile[campaign.AUTO_UPLOAD] is True
        campaign.check_campaign(mw)
        assert upload.call_count == notify.call_count == 1
        add_review(col)
        mw.pm.profile[campaign.REMINDER] = 0
        campaign.check_campaign(mw)
        assert upload.call_count == 2
        # The scheduled upload thanks with a tooltip instead of a dialog.
        assert notify.call_count == 1
        tip.assert_called_once_with(
            col.tr.qt_misc_campaign_thanks(), period=8000, parent=mw
        )
        assert col.get_note(note.id).fields == ["excluded front", "included back"]
        assert [call.args[1] for call in upload.call_args_list] == [
            col.get_config(campaign.COLLECTION_ID)
        ] * 2
        assert all(not call.args[0].exists() for call in upload.call_args_list)

        checkbox = campaign.QCheckBox()
        prefs = SimpleNamespace(
            prof=mw.pm.profile,
            form=SimpleNamespace(campaign_monthly=checkbox),
            setup_network=Mock(),
            update_network=Mock(),
        )
        Preferences.setup_profile(prefs)
        assert not checkbox.isHidden() and checkbox.isChecked()
        checkbox.click()
        Preferences.update_profile(prefs)
        assert not mw.pm.profile.get(campaign.AUTO_UPLOAD)
        invitation = Mock()
        monkeypatch.setattr(campaign, "_invite", invitation)
        mw.pm.profile[campaign.REMINDER] = 0
        campaign.check_campaign(mw)
        invitation.assert_called_once_with(mw, campaign.Campaign(REQUEST_URL, None))
        assert upload.call_count == 2
        checkbox.close()
    finally:
        col.close()
        mw.close()


def test_sharing_preferences_follow_note_types_and_survive_reopening(
    tmp_path, monkeypatch
):
    from aqt.donation_sharing import SHARING, SharingDialog
    from aqt.qt import QDialogButtonBox, Qt

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mw = QWidget()
    col = Collection(str(tmp_path / "preferences.anki2"))
    mw.col = col
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    checked = Qt.CheckState.Checked
    unchecked = Qt.CheckState.Unchecked

    def open_editor():
        dialog = SharingDialog(mw, mw)
        dialog.show()
        app.processEvents()
        return dialog

    def select_type(dialog, name):
        (item,) = dialog.types.findItems(name, Qt.MatchFlag.MatchExactly)
        dialog.types.setCurrentItem(item)

    def click(dialog, label):
        buttons = dialog.findChild(QDialogButtonBox).buttons()
        next(button for button in buttons if button.text() == label).click()

    try:
        dialog = open_editor()
        select_type(dialog, "Basic (and reversed card)")
        assert [dialog.fields.item(i).checkState() for i in range(2)] == [
            checked,
            checked,
        ]
        dialog.search.setText("front")
        assert dialog.fields.item(1).isHidden()
        dialog.fields.item(0).setCheckState(unchecked)
        dialog.templates.item(1).setCheckState(unchecked)
        select_type(dialog, "Basic")
        assert dialog.fields.item(0).checkState() == checked
        click(dialog, "Save preferences")
        assert SHARING in mw.pm.profile

        model = col.models.by_name("Basic (and reversed card)")
        col.models.add_field(model, col.models.new_field("Later addition"))
        col.models.update_dict(model)
        dialog = open_editor()
        select_type(dialog, model["name"])
        assert [dialog.fields.item(i).checkState() for i in range(3)] == [
            unchecked,
            checked,
            unchecked,
        ]
        assert [dialog.templates.item(i).checkState() for i in range(2)] == [
            checked,
            unchecked,
        ]
        select_type(dialog, "Basic")
        dialog.exclude_fields.click()
        assert dialog.fields.item(0).checkState() == unchecked
        click(dialog, "Cancel")
        dialog = open_editor()
        select_type(dialog, "Basic")
        assert dialog.fields.item(0).checkState() == checked
        dialog.reject()
    finally:
        col.close()
        mw.close()


def test_note_types_can_be_excluded_and_bulk_toggled(tmp_path, monkeypatch):
    from aqt.donation_sharing import SHARING, SharingDialog
    from aqt.qt import Qt

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mw = QWidget()
    col = Collection(str(tmp_path / "types.anki2"))
    mw.col = col
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    checked = Qt.CheckState.Checked
    unchecked = Qt.CheckState.Unchecked
    reversed_name = "Basic (and reversed card)"

    def open_editor():
        dialog = SharingDialog(mw, mw)
        dialog.show()
        app.processEvents()
        return dialog

    def type_item(dialog, name):
        (item,) = dialog.types.findItems(name, Qt.MatchFlag.MatchExactly)
        return item

    def select_type(dialog, name):
        dialog.types.setCurrentItem(type_item(dialog, name))

    def states(widget):
        return [widget.item(i).checkState() for i in range(widget.count())]

    def editable(dialog):
        controls = (
            dialog.fields,
            dialog.templates,
            dialog.search,
            dialog.include_fields,
            dialog.exclude_fields,
        )
        return {control.isEnabled() for control in controls}

    try:
        dialog = open_editor()
        assert set(states(dialog.types)) == {checked}
        select_type(dialog, reversed_name)
        dialog.fields.item(0).setCheckState(unchecked)
        type_item(dialog, reversed_name).setCheckState(unchecked)
        assert editable(dialog) == {False}
        assert states(dialog.fields) == [unchecked, checked]
        type_item(dialog, reversed_name).setCheckState(checked)
        assert editable(dialog) == {True}
        assert states(dialog.fields) == [unchecked, checked]
        assert states(dialog.templates) == [checked, checked]

        # Field buttons act only on the fields the search leaves visible.
        select_type(dialog, "Basic")
        dialog.search.setText("front")
        dialog.exclude_fields.click()
        assert states(dialog.fields) == [unchecked, checked]
        dialog.search.setText("")
        dialog.include_fields.click()
        assert states(dialog.fields) == [checked, checked]
        dialog.search.setText("back")
        dialog.exclude_fields.click()
        dialog.search.setText("")
        assert states(dialog.fields) == [checked, unchecked]

        # Including a type with nothing selected selects all of it.
        select_type(dialog, "Cloze")
        dialog.exclude_fields.click()
        dialog.templates.item(0).setCheckState(unchecked)
        type_item(dialog, "Cloze").setCheckState(unchecked)
        type_item(dialog, "Cloze").setCheckState(checked)
        assert set(states(dialog.fields)) == set(states(dialog.templates)) == {checked}

        dialog.exclude_types.click()
        assert set(states(dialog.types)) == {unchecked}
        assert editable(dialog) == {False}
        dialog.include_types.click()
        assert set(states(dialog.types)) == {checked}
        assert editable(dialog) == {True}
        select_type(dialog, reversed_name)
        assert states(dialog.fields) == [unchecked, checked]
        select_type(dialog, "Basic")
        assert states(dialog.fields) == [checked, unchecked]

        type_item(dialog, reversed_name).setCheckState(unchecked)
        next(
            b
            for b in dialog.findChildren(campaign.QPushButton)
            if b.text() == "Save preferences"
        ).click()
        reversed_id = str(col.models.by_name(reversed_name)["id"])
        assert mw.pm.profile[SHARING][reversed_id]["include"] is False

        added = col.models.copy(col.models.by_name("Basic"))
        dialog = open_editor()
        assert type_item(dialog, reversed_name).checkState() == unchecked
        assert type_item(dialog, added["name"]).checkState() == unchecked
        assert type_item(dialog, "Basic").checkState() == checked
        select_type(dialog, reversed_name)
        assert editable(dialog) == {False}
        assert states(dialog.fields) == [unchecked, checked]
        select_type(dialog, added["name"])
        assert editable(dialog) == {False}
        assert set(states(dialog.fields)) == {unchecked}
        dialog.reject()
    finally:
        col.close()
        mw.close()


def test_saved_note_types_without_include_stay_included():
    from aqt.donation_sharing import current_choices

    model = {
        "id": 7,
        "flds": [{"id": 1, "name": "Front"}, {"id": 2, "name": "Back"}],
        "tmpls": [{"id": 3, "name": "Card 1"}],
    }
    saved = {"7": {"flds": ["id:1:Front"], "tmpls": []}}
    assert current_choices([model], saved) == {
        "7": {"include": True, "flds": ["id:1:Front"], "tmpls": []}
    }
    assert current_choices([model], {})["7"]["include"] is False
    assert current_choices([model], None)["7"] == {
        "include": True,
        "flds": ["id:1:Front", "id:2:Back"],
        "tmpls": ["id:3:Card 1"],
    }


def test_excluded_note_types_share_no_text_or_layout(tmp_path):
    from anki._backend import RustBackend
    from aqt.donation_sharing import current_choices, export_campaign

    col = Collection(str(tmp_path / "source.anki2"))
    try:
        notes = {}
        for name, fields in (
            ("Basic", ["shared front", "shared back"]),
            ("Basic (and reversed card)", ["private front", "private back"]),
        ):
            note = col.new_note(col.models.by_name(name))
            note.fields = fields
            col.add_note(note, 1)
            notes[name] = note
        excluded = col.models.by_name("Basic (and reversed card)")
        choices = current_choices(col.models.all(), None)
        # Remembered selections of an excluded type must not leak.
        choices[str(excluded["id"])]["include"] = False
        path = str(tmp_path / "campaign.colpkg")
        export_campaign(col, path, choices, "test-id")
        cards = len(col.find_cards(""))
    finally:
        col.close()

    folder = tmp_path / "copy"
    folder.mkdir()
    db = str(folder / "copy.anki2")
    RustBackend().import_collection_package(
        backup_path=path,
        col_path=db,
        media_folder=str(folder / "media"),
        media_db=str(folder / "media.db"),
    )
    copy = Collection(db)
    try:
        assert copy.get_note(notes["Basic"].id).fields == [
            "shared front",
            "shared back",
        ]
        assert copy.get_note(notes["Basic (and reversed card)"].id).fields == [
            "",
            "",
        ]
        # Excluded note types are exported under a generic name.
        model = copy.models.get(excluded["id"])
        assert not any(t["qfmt"] or t["afmt"] for t in model["tmpls"])
        assert not model["css"]
        assert copy.models.by_name("Basic")["tmpls"][0]["qfmt"]
        assert len(copy.find_cards("")) == cards
    finally:
        copy.close()


def test_invitation_shows_the_current_sharing_state(tmp_path, monkeypatch):
    from aqt.donation_sharing import SHARING, current_choices
    from aqt.qt import QPushButton

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mw = QWidget()
    col = Collection(str(tmp_path / "sharing.anki2"))
    mw.col = col
    mw.pm = SimpleNamespace(profile={}, save=Mock())
    use_tr(monkeypatch, col)
    prepare = Mock()
    monkeypatch.setattr(transfer, "prepare", prepare)
    models = col.models.all()
    shown = []

    def read_state(then):
        def inspect():
            dialog = app.activeModalWidget()
            shown.append(dialog.findChild(QLabel, "campaignSharingState").text())
            then(dialog)

        return inspect

    def later(dialog):
        next(b for b in dialog.buttons.buttons() if b.text() == "Later").click()

    def invite(saved, then=later):
        if saved is None:
            mw.pm.profile.pop(SHARING, None)
        else:
            mw.pm.profile[SHARING] = saved
        QTimer.singleShot(0, read_state(then))
        campaign._invite(mw, campaign.Campaign(REQUEST_URL, None))
        return shown[-1]

    try:
        assert invite(None) == col.tr.qt_misc_campaign_sharing_all(
            notetype_count=len(models)
        )
        assert invite({}) == col.tr.qt_misc_campaign_sharing_nothing()
        # Fields of excluded note types do not count.
        choices = current_choices(models, None)
        basic = col.models.by_name("Basic")
        choices[str(basic["id"])]["flds"] = choices[str(basic["id"])]["flds"][:1]
        cloze = col.models.by_name("Cloze")
        choices[str(cloze["id"])]["include"] = False
        included = [m for m in models if m["id"] != cloze["id"]]
        assert invite(choices) == col.tr.qt_misc_campaign_sharing_some(
            selected=sum(len(m["flds"]) for m in included) - 1,
            total=sum(len(m["flds"]) for m in included),
            included_count=len(included),
            notetype_count=len(models),
        )

        def include_everything(dialog):
            def save():
                editor = app.activeModalWidget()
                assert isinstance(editor, campaign.SharingDialog)
                editor.include_types.click()
                buttons = editor.findChild(QDialogButtonBox).buttons()
                next(b for b in buttons if b.text() == "Save preferences").click()

            QTimer.singleShot(0, save)
            next(
                b
                for b in dialog.findChildren(QPushButton)
                if b.text() == "Sharing preferences"
            ).click()
            shown.append(dialog.findChild(QLabel, "campaignSharingState").text())
            later(dialog)

        assert invite({}, include_everything) == col.tr.qt_misc_campaign_sharing_all(
            notetype_count=len(models)
        )
        assert shown[-2] == col.tr.qt_misc_campaign_sharing_nothing()
        prepare.assert_not_called()
    finally:
        col.close()
        mw.close()


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    """A profile enrolled in monthly uploads whose reminder is due."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    mw = QWidget()
    col = Collection(str(tmp_path / "scheduled.anki2"))
    mw.col = col
    mw.pm = SimpleNamespace(
        profile={campaign.AUTO_UPLOAD: True, campaign.REMINDER: 0}, save=Mock()
    )
    mw.state = "deckBrowser"
    mw._unload_profile_and_exit_pending = False
    mw.progress = SimpleNamespace(single_shot=lambda _, f: f())

    def run(task, done, **kwargs):
        result = Future()
        result.set_result(task())
        done(result)

    mw.taskman = SimpleNamespace(run_in_background=run, with_progress=run)
    use_tr(monkeypatch, col)
    monkeypatch.setattr(
        campaign, "_load_campaign", lambda: campaign.Campaign(REQUEST_URL, None)
    )
    monkeypatch.setattr(transfer, "int_time", lambda: 12345)
    env = SimpleNamespace(
        app=app,
        mw=mw,
        col=col,
        profile=mw.pm.profile,
        notify=Mock(),
        tip=Mock(),
        campaign_tip=Mock(),
        exports=[],
        uploads=[],
        result=True,
        defer_export=False,
    )
    monkeypatch.setattr(transfer, "show_info", env.notify)
    monkeypatch.setattr(transfer, "tooltip", env.tip)
    monkeypatch.setattr(campaign, "tooltip", env.campaign_tip)

    def export(parent, path, choices, collection_id, on_success, on_failure):
        path.write_bytes(b"synthetic archive")
        env.exports.append((path, on_success))
        if not env.defer_export:
            on_success()

    def upload(path, collection_id):
        env.uploads.append(FakeUpload(path, collection_id, result=env.result))
        return env.uploads[-1]

    monkeypatch.setattr(transfer, "_export", export)
    monkeypatch.setattr(transfer, "_upload", upload)
    add_review(col)
    try:
        yield env
    finally:
        transfer._cancel_active()
        col.close()
        mw.close()


def test_scheduled_upload_runs_without_a_dialog(scheduled):
    scheduled.result = None
    campaign.check_campaign(scheduled.mw)
    (upload,) = scheduled.uploads
    assert upload.started and transfer.busy()
    assert scheduled.app.activeModalWidget() is None
    assert scheduled.mw.findChild(transfer.TransferDialog) is None
    upload.finish(True)
    assert not transfer.busy() and not upload.path.exists()
    scheduled.notify.assert_not_called()
    scheduled.tip.assert_called_once_with(
        scheduled.col.tr.qt_misc_campaign_thanks(), period=8000, parent=scheduled.mw
    )
    due = scheduled.profile[campaign.REMINDER]
    assert due >= QDateTime.currentDateTime().addDays(27).toSecsSinceEpoch()
    assert scheduled.profile[transfer.LAST_REVIEW] == scheduled.col.db.scalar(
        "select max(id) from revlog"
    )


def test_scheduled_failures_are_reported_on_the_third_in_a_row(scheduled):
    failed = scheduled.col.tr.qt_misc_campaign_upload_failed()

    def attempt(result):
        scheduled.result = result
        scheduled.profile[campaign.REMINDER] = 0
        campaign.check_campaign(scheduled.mw)
        assert not transfer.busy()

    for count in (1, 2):
        attempt(False)
        assert scheduled.profile[transfer.FAILED_UPLOADS] == count
        # The next attempt is tomorrow.
        assert scheduled.profile[campaign.REMINDER] == 12345 + 86_400
        scheduled.notify.assert_not_called()
    attempt(False)
    scheduled.notify.assert_called_once_with(failed, parent=scheduled.mw)
    assert transfer.FAILED_UPLOADS not in scheduled.profile
    assert scheduled.profile[campaign.REMINDER] == 12345 + 86_400

    attempt(False)
    assert scheduled.profile[transfer.FAILED_UPLOADS] == 1
    attempt(True)
    assert transfer.FAILED_UPLOADS not in scheduled.profile
    assert scheduled.notify.call_count == 1
    assert len(scheduled.uploads) == 5

    # Manual uploads report a failure at once.
    scheduled.result = False
    transfer.prepare(scheduled.mw, automatic=False)
    assert scheduled.notify.call_count == 2
    assert scheduled.notify.call_args.args == (failed,)
    assert transfer.FAILED_UPLOADS not in scheduled.profile


def test_unchanged_review_history_skips_the_monthly_upload(scheduled):
    newest = scheduled.col.db.scalar("select max(id) from revlog")
    scheduled.profile[transfer.LAST_REVIEW] = newest
    campaign.check_campaign(scheduled.mw)
    assert not scheduled.exports and not transfer.busy()
    due = scheduled.profile[campaign.REMINDER]
    assert due >= QDateTime.currentDateTime().addDays(27).toSecsSinceEpoch()
    scheduled.tip.assert_not_called()

    # Manual uploads always proceed.
    transfer.prepare(scheduled.mw, automatic=False)
    assert len(scheduled.exports) == 1

    add_review(scheduled.col)
    scheduled.profile[campaign.REMINDER] = 0
    campaign.check_campaign(scheduled.mw)
    assert len(scheduled.exports) == 2
    assert scheduled.profile[transfer.LAST_REVIEW] > newest


def test_manual_upload_shows_one_dialog_and_cancel_skips_the_upload(scheduled):
    tr = scheduled.col.tr
    scheduled.defer_export = True
    scheduled.result = None
    transfer.prepare(scheduled.mw, automatic=False)
    dialog = scheduled.mw.findChild(transfer.TransferDialog)
    assert dialog.isVisible() and dialog.isModal()
    assert dialog.label.text() == tr.qt_misc_campaign_preparing()
    assert (dialog.bar.minimum(), dialog.bar.maximum()) == (0, 0)
    assert [dialog.buttons.standardButton(b) for b in dialog.buttons.buttons()] == [
        QDialogButtonBox.StandardButton.Cancel
    ]

    path, exported = scheduled.exports[-1]
    exported()
    (upload,) = scheduled.uploads
    assert dialog.label.text() == tr.qt_misc_campaign_uploading(percent=0)
    upload.progress.emit(40)
    assert dialog.label.text() == tr.qt_misc_campaign_uploading(percent=40)
    assert (dialog.bar.maximum(), dialog.bar.value()) == (100, 40)
    upload.progress.emit(100)
    assert dialog.label.text() == tr.qt_misc_campaign_waiting_for_receipt()
    upload.finish(True)
    assert not dialog.isVisible() and not path.exists()
    scheduled.notify.assert_called_once_with(
        tr.qt_misc_campaign_thanks(), parent=scheduled.mw
    )

    # Cancelling while the export runs skips the upload once it finishes.
    scheduled.profile[campaign.REMINDER] = 0
    transfer.prepare(scheduled.mw, automatic=False)
    dialog = next(
        d for d in scheduled.mw.findChildren(transfer.TransferDialog) if d.isVisible()
    )
    dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
    assert not dialog.isVisible() and not transfer.busy()
    path, exported = scheduled.exports[-1]
    assert path.exists()
    exported()
    assert len(scheduled.uploads) == 1 and not path.exists()
    assert scheduled.profile[campaign.REMINDER] == 12345 + 86_400
    assert scheduled.notify.call_count == 1


def test_profile_close_stops_a_background_transfer(scheduled):
    from aqt import gui_hooks

    scheduled.result = None
    campaign.check_campaign(scheduled.mw)
    (upload,) = scheduled.uploads
    assert upload.started and transfer.busy()

    # The menu does not start a second transfer.
    campaign.check_campaign(scheduled.mw, manual=True)
    scheduled.campaign_tip.assert_called_once_with(
        scheduled.col.tr.qt_misc_campaign_upload_in_progress(), parent=scheduled.mw
    )
    assert len(scheduled.exports) == 1

    gui_hooks.profile_will_close()
    assert upload.stopped and not transfer.busy()
    assert not upload.path.parent.exists()
    # A late completion signal changes nothing.
    upload.finish(True)
    scheduled.tip.assert_not_called()
    assert transfer.LAST_REVIEW not in scheduled.profile

    # A profile closed mid-export removes the archive once the export ends.
    scheduled.defer_export = True
    scheduled.profile[campaign.REMINDER] = 0
    campaign.check_campaign(scheduled.mw)
    path, exported = scheduled.exports[-1]
    gui_hooks.profile_will_close()
    assert not transfer.busy() and path.exists()
    exported()
    assert len(scheduled.uploads) == 1 and not path.parent.exists()
