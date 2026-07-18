# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for mediasrv security utilities."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aqt.mediasrv import (
    UNTRUSTED_MEDIA_CSP,
    LocalFileRequest,
    UnsafePathException,
    _editor_content_security_policy,
    _handle_local_file_request,
    _should_log_request,
    ensure_safe_path,
    is_localhost_origin,
    is_sveltekit_page,
)

RWKV_AFTER_REVIEW_UNAVAILABLE_ROW = (
    "RWKV : R After Review",
    "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
)
RWKV_AFTER_TEN_MINUTES_UNAVAILABLE_ROW = (
    "RWKV : R After 10min",
    "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
)
RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS = [
    RWKV_AFTER_REVIEW_UNAVAILABLE_ROW,
    RWKV_AFTER_TEN_MINUTES_UNAVAILABLE_ROW,
]
NEXT_S90_UNAVAILABLE_ROWS = [
    (
        "RWKV Curve Next S90",
        "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
    ),
    (
        "FSRS Next S90",
        "Again:Unavailable Hard:Unavailable Good:Unavailable Easy:Unavailable",
    ),
]


class TestEnsureSafePath:
    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        subdir = Path(self.tmpdir) / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("ok")

    def test_valid_subpath(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub/file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_parent_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "../etc/passwd")

    def test_rejects_double_traversal(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../etc/passwd")

    def test_rejects_absolute_path_escape(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "/etc/passwd")

    def test_rejects_base_dir_itself(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, ".")

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "")

    def test_accepts_pathlib_args(self) -> None:
        result = ensure_safe_path(Path(self.tmpdir), Path("sub/file.txt"))
        assert result.endswith(os.path.join("sub", "file.txt"))

    def test_normalizes_redundant_separators(self) -> None:
        result = ensure_safe_path(self.tmpdir, "sub///file.txt")
        assert result == os.path.join(os.path.realpath(self.tmpdir), "sub", "file.txt")

    def test_rejects_traversal_after_normalization(self) -> None:
        with pytest.raises(UnsafePathException):
            ensure_safe_path(self.tmpdir, "sub/../../../etc/passwd")


class TestIsLocalhostOrigin:
    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:40000",
            "http://localhost:40000",
            "http://[::1]:40000",
            "https://127.0.0.1:40000",
            "https://localhost:40000",
            "https://[::1]:40000",
            "http://127.0.0.1",
            "http://localhost",
            "http://[::1]",
            "http://127.0.0.1/",
            "http://localhost/path",
        ],
    )
    def test_allowed_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.com",
            "http://127.0.0.1.evil.com",
            "http://localhost.evil.com",
            "http://evil.com:127.0.0.1",
            "http://notlocalhost:40000",
            "https://evil.com",
            "",
        ],
    )
    def test_rejected_origins(self, origin: str) -> None:
        assert is_localhost_origin(origin) is False


class TestIsSveltekitPage:
    def test_dynamic_desired_retention_plot_is_internal_page(self) -> None:
        assert is_sveltekit_page("dynamic-desired-retention-plot")
        assert is_sveltekit_page("dynamic-desired-retention-plot/_app/start.js")


class TestRequestLogging:
    def test_latest_progress_polling_is_not_logged(self) -> None:
        assert not _should_log_request("_anki/latestProgress")

    def test_regular_requests_are_logged(self) -> None:
        assert _should_log_request("_anki/evaluateParamsLegacy")


class TestGraphs:
    @pytest.mark.parametrize(
        ("status", "expected_header"),
        [
            ("PENDING", "1"),
            ("READY", None),
        ],
    )
    def test_graphs_sets_rwkv_pending_header(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
        expected_header: str | None,
    ) -> None:
        import aqt
        from anki.stats_pb2 import GraphsRequest
        from aqt.mediasrv import RWKV_STATS_PENDING_HEADER, app, graphs
        from aqt.rwkv_scheduler import RwkvStatsPreparationStatus

        calls: list[str] = []

        def prepare(reviewer: object, search: str) -> RwkvStatsPreparationStatus:
            calls.append(search)
            return getattr(RwkvStatsPreparationStatus, status)

        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=object()), raising=False)
        monkeypatch.setattr(
            "aqt.rwkv_scheduler.prepare_stats_retrievability_scores",
            prepare,
        )
        monkeypatch.setattr(
            "aqt.mediasrv.raw_backend_request",
            lambda endpoint: lambda: b"graph-data",
        )

        data = GraphsRequest(search="rated:7", days=365).SerializeToString()
        with app.test_request_context(data=data):
            response = graphs()

        assert calls == ["rated:7"]
        assert response.get_data() == b"graph-data"
        assert response.headers.get("Content-Type") == "application/binary"
        assert response.headers.get(RWKV_STATS_PENDING_HEADER) == expected_header


def _make_media_file(tmpdir: str, filename: str, content: bytes = b"test") -> str:
    path = os.path.join(tmpdir, filename)
    with open(path, "wb") as f:
        f.write(content)
    return filename


def _get_csp(response) -> str | None:
    return response.headers.get("Content-Security-Policy")


def _csp_directives(csp: str) -> dict[str, str]:
    directives = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        directives[name] = value
    return directives


class TestMediaFileCSP:
    """CSP headers on media file responses should block script execution."""

    @pytest.mark.parametrize("doctype", ["html", "svg"])
    def test_doc_has_csp_header(self, doctype: str) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(
                tmpdir, f"test.{doctype}", f"<{doctype}></{doctype}>".encode()
            )
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp is not None, f"{doctype} response must have CSP header"

    def test_csp_blocks_connect_to_local_api(self) -> None:
        """Scripts must not be able to fetch() the local /_anki/ API.

        Even if script-src somehow gets relaxed in the future, connect-src
        should not allow http: (which includes http://127.0.0.1).
        """
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "test.svg", b"<svg></svg>")
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp is not None

            # default-src 'none' implies connect-src 'none', which is sufficient
            if "default-src 'none'" in csp:
                return

            # Otherwise connect-src must not include http: or 'self'
            assert "http:" not in csp, (
                f"CSP must not allow http: connections (enables local API access): {csp}"
            )
            assert "'self'" not in csp, (
                f"CSP must not allow 'self' connections (enables local API access): {csp}"
            )

    def test_untrusted_media_is_sandboxed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "test.svg", b"<svg></svg>")
            req = LocalFileRequest(root=tmpdir, path=fname)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            csp = _get_csp(resp)
            assert csp == UNTRUSTED_MEDIA_CSP

            directives = _csp_directives(csp)
            assert directives["default-src"] == "'none'"
            assert directives["script-src"] == "'none'"
            assert directives["connect-src"] == "'none'"
            assert directives["object-src"] == "'none'"
            assert directives["frame-src"] == "'none'"
            assert directives["child-src"] == "'none'"
            assert directives["base-uri"] == "'none'"
            assert directives["form-action"] == "'none'"
            assert directives["sandbox"] == ""
            assert "frame-ancestors" not in directives

    def test_trusted_local_file_does_not_get_untrusted_media_csp(self) -> None:
        """Add-on exports use LocalFileRequest too, but should not be sandboxed."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            fname = _make_media_file(tmpdir, "addon.html", b"<html></html>")
            req = LocalFileRequest(root=tmpdir, path=fname, untrusted=False)
            from aqt.mediasrv import app

            with app.test_request_context():
                resp = _handle_local_file_request(req)
            assert _get_csp(resp) is None


class TestRwkvReschedule:
    @pytest.mark.parametrize("deck_id", [None, 100])
    def test_reschedule_forwards_requested_deck_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deck_id: int | None,
    ) -> None:
        import aqt
        from anki import decks_pb2
        from aqt.mediasrv import app, reschedule_rwkv_review_cards

        mw = object()
        calls: list[tuple[object, int | None]] = []

        def reschedule(mw_arg: object, *, deck_id: int | None = None) -> None:
            calls.append((mw_arg, deck_id))

        monkeypatch.setattr(aqt, "mw", mw, raising=False)
        monkeypatch.setattr(
            "aqt.rwkv_scheduler.reschedule_rwkv_review_cards_with_progress",
            reschedule,
        )

        data = (
            decks_pb2.DeckId(did=deck_id).SerializeToString()
            if deck_id is not None
            else b""
        )
        with app.test_request_context(data=data):
            assert reschedule_rwkv_review_cards() == b""

        assert calls == [(mw, deck_id)]


class TestEditorPageCSP:
    def test_editor_csp_does_not_block_user_embeds(self) -> None:
        csp = _editor_content_security_policy(port=12345)
        directives = _csp_directives(csp)

        assert directives["script-src"] == (
            "http://127.0.0.1:12345/_anki/ http://127.0.0.1:12345/_addons/"
        )
        assert "object-src" not in directives
        assert "frame-src" not in directives
        assert "child-src" not in directives
        assert "img-src" not in directives


class TestCardStats:
    @pytest.mark.parametrize(
        "deck_config",
        [
            {"id": 1, "rwkvReviewEnabled": True},
            {
                "id": 1,
                "other": {
                    "jschoreels.fsrs": {
                        "rwkv_review_enabled": True,
                    },
                },
            },
        ],
    )
    def test_card_info_includes_rwkv_diagnostics_without_reviewer_cache(
        self, monkeypatch: pytest.MonkeyPatch, deck_config: dict[str, object]
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt.mediasrv import app, card_stats
        from aqt.rwkv_scheduler import (
            RwkvIntervalOverride,
            RwkvReviewPrediction,
            set_reviewer_backend,
        )

        card = SimpleNamespace(id=123, did=10)
        response = CardStatsResponse(card_id=123)

        class Backend:
            def predict_review(
                self,
                *,
                reviewer: object,
                card: object,
            ) -> RwkvReviewPrediction:
                return RwkvReviewPrediction(
                    retrievability=0.61,
                    interval_overrides=RwkvIntervalOverride(
                        again=1,
                        hard=2,
                        good=4,
                        easy=8,
                    ),
                )

            def review_answered(
                self,
                *,
                reviewer: object,
                card: object,
                ease: int,
            ) -> None:
                pass

        class RawBackend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Decks:
            def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
                assert deck_id == 10
                return deck_config

        class Collection:
            _backend = RawBackend()
            decks = Decks()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        collection = Collection()
        mw = SimpleNamespace(col=collection)
        reviewer = SimpleNamespace(mw=mw)
        mw.reviewer = reviewer

        previous = set_reviewer_backend(Backend())
        try:
            monkeypatch.setattr(aqt, "mw", mw)
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            set_reviewer_backend(previous)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert [(row.label, row.value) for row in output.extra_rows] == [
            ("RWKV computed R", "61%"),
            ("Retrievability source", "RWKV"),
            *NEXT_S90_UNAVAILABLE_ROWS,
            *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
        ]

    def test_card_info_reports_rwkv_unavailable_when_backend_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt.mediasrv import app, card_stats
        from aqt.rwkv_scheduler import set_reviewer_backend

        card = SimpleNamespace(id=123, did=10)
        response = CardStatsResponse(card_id=123)
        response.memory_state.stability = 1.0

        class RawBackend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Decks:
            def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
                assert deck_id == 10
                return {"id": 1, "rwkvReviewEnabled": True}

        class Collection:
            _backend = RawBackend()
            decks = Decks()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        monkeypatch.delenv("ANKI_RWKV_BENCHMARK_PATH", raising=False)
        monkeypatch.delenv("ANKI_RWKV_MODEL_PATH", raising=False)
        monkeypatch.setattr("aqt.rwkv_scheduler.embedded_rwkv_model_path", lambda: None)
        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=Collection()))
        previous = set_reviewer_backend(None)
        try:
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            set_reviewer_backend(previous)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert [(row.label, row.value) for row in output.extra_rows] == [
            ("RWKV computed R", "Unavailable"),
            ("Retrievability source", "FSRS (RWKV backend unavailable)"),
            *NEXT_S90_UNAVAILABLE_ROWS,
            *RWKV_AFTER_REVIEW_UNAVAILABLE_ROWS,
        ]

    def test_card_info_hook_can_append_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aqt
        from anki.stats_pb2 import CardStatsResponse
        from aqt import gui_hooks
        from aqt.browser.card_info import CardInfoRow
        from aqt.mediasrv import app, card_stats

        card = object()
        response = CardStatsResponse(card_id=123)

        class Backend:
            def card_stats_raw(self, data: bytes) -> bytes:
                return response.SerializeToString()

        class Collection:
            _backend = Backend()

            def get_card(self, card_id: int) -> object:
                assert card_id == 123
                return card

        def add_row(rows: list[CardInfoRow], hook_card: object) -> None:
            assert hook_card is card
            rows.append(CardInfoRow(label="Dynamic DR", value="0.71"))

        monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=Collection()))
        gui_hooks.card_info_will_add_rows.append(add_row)
        try:
            with app.test_request_context(data=b""):
                raw_output = card_stats()
        finally:
            gui_hooks.card_info_will_add_rows.remove(add_row)

        output = CardStatsResponse()
        output.ParseFromString(raw_output)

        assert len(output.extra_rows) == 1
        assert output.extra_rows[0].label == "Dynamic DR"
        assert output.extra_rows[0].value == "0.71"
