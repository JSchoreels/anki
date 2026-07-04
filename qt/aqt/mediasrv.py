# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import enum
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from errno import EPROTOTYPE
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flask
import stringcase
import waitress.wasyncore
from flask import Response, abort, request
from waitress.server import create_server

import aqt
import aqt.main
import aqt.operations
import aqt.rwkv_scheduler
from anki import decks_pb2, generic_pb2, hooks
from anki.cards import CardId
from anki.collection import OpChanges, OpChangesOnly, Progress, SearchNode
from anki.decks import UpdateDeckConfigs
from anki.scheduler.v3 import SchedulingStatesWithContext, SetSchedulingStatesRequest
from anki.stats_pb2 import CardStatsResponse, GraphsRequest
from anki.utils import dev_mode
from aqt import gui_hooks
from aqt.changenotetype import ChangeNotetypeDialog
from aqt.deckoptions import DeckOptionsDialog
from aqt.operations import on_op_finished
from aqt.operations.deck import update_deck_configs as update_deck_configs_op
from aqt.progress import ProgressBarUpdate, ProgressUpdate
from aqt.qt import *
from aqt.utils import aqt_data_path, show_warning, tr

# https://forums.ankiweb.net/t/anki-crash-when-using-a-specific-deck/22266
waitress.wasyncore._DISCONNECTED = waitress.wasyncore._DISCONNECTED.union({EPROTOTYPE})  # type: ignore

logger = logging.getLogger(__name__)
app = flask.Flask(__name__, root_path="/fake")
RWKV_STATS_PENDING_HEADER = "X-Anki-Rwkv-Stats-Pending"
_QUIET_DEBUG_REQUEST_PATHS = frozenset(
    {
        "_anki/latestProgress",
    }
)


@dataclass
class LocalFileRequest:
    # base folder, eg media folder
    root: str
    # path to file relative to root folder
    path: str
    # collection media is untrusted user content; add-on web exports are not
    untrusted: bool = True


UNTRUSTED_MEDIA_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'none'",
        "connect-src 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "sandbox",
    )
)


def _editor_content_security_policy(port: int) -> str:
    # Only script-src is restricted here. Adding frame-src, object-src, img-src
    # etc. would break existing user content: YouTube embeds, dictionary iframes,
    # remote images, and SVG object tags.
    csp_paths = (
        f"http://127.0.0.1:{port}/_anki/",
        f"http://127.0.0.1:{port}/_addons/",
    )
    return "; ".join((f"script-src {' '.join(csp_paths)}",))


@dataclass
class BundledFileRequest:
    # path relative to aqt data folder
    path: str


@dataclass
class NotFound:
    message: str


DynamicRequest = Callable[[], Response]


class PageContext(enum.IntEnum):
    UNKNOWN = enum.auto()
    EDITOR = enum.auto()
    REVIEWER = enum.auto()
    PREVIEWER = enum.auto()
    CARD_LAYOUT = enum.auto()
    DECK_OPTIONS = enum.auto()
    # something in /_anki/pages/
    NON_LEGACY_PAGE = enum.auto()
    # Do not use this if you present user content (e.g. content from cards), as it's a
    # security issue.
    ADDON_PAGE = enum.auto()


@dataclass
class LegacyPage:
    html: str
    context: PageContext


class MediaServer(threading.Thread):
    _ready = threading.Event()
    daemon = True

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        super().__init__()
        self.is_shutdown = False
        # map of webview ids to pages
        self._legacy_pages: dict[int, LegacyPage] = {}

    def run(self) -> None:
        try:
            desired_host = os.getenv("ANKI_API_HOST", "127.0.0.1")
            desired_port = int(os.getenv("ANKI_API_PORT") or 0)
            self.server = create_server(
                app,
                host=desired_host,
                port=desired_port,
                clear_untrusted_proxy_headers=True,
            )
            logger.info(
                "Serving on http://%s:%s",
                self.server.effective_host,  # type: ignore[union-attr]
                self.server.effective_port,  # type: ignore[union-attr]
            )

            self._ready.set()
            self.server.run()

        except Exception:
            if not self.is_shutdown:
                raise

    def shutdown(self) -> None:
        self.is_shutdown = True
        sockets = list(self.server._map.values())  # type: ignore
        for socket in sockets:
            socket.handle_close()
        # https://github.com/Pylons/webtest/blob/4b8a3ebf984185ff4fefb31b4d0cf82682e1fcf7/webtest/http.py#L93-L104
        self.server.task_dispatcher.shutdown()

    def getPort(self) -> int:
        self._ready.wait()
        return int(self.server.effective_port)  # type: ignore

    def set_page_html(
        self, id: int, html: str, context: PageContext = PageContext.UNKNOWN
    ) -> None:
        self._legacy_pages[id] = LegacyPage(html, context)

    def get_page(self, id: int) -> LegacyPage | None:
        return self._legacy_pages.get(id)

    def get_page_html(self, id: int) -> str | None:
        if page := self.get_page(id):
            return page.html
        else:
            return None

    def get_page_context(self, id: int) -> PageContext | None:
        if page := self.get_page(id):
            return page.context
        else:
            return None

    def clear_page_html(self, id: int) -> None:
        try:
            del self._legacy_pages[id]
        except KeyError:
            pass


@app.route("/favicon.ico")
def favicon() -> Response:
    request = BundledFileRequest(os.path.join("imgs", "favicon.ico"))
    return _handle_builtin_file_request(request)


def _mime_for_path(path: str) -> str:
    "Mime type for provided path/filename."

    _, ext = os.path.splitext(path)
    ext = ext.lower()

    # Badly-behaved apps on Windows can alter the standard mime types in the registry, which can completely
    # break Anki's UI. So we hard-code the most common extensions.
    mime_types = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".html": "text/html",
        ".htm": "text/html",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".json": "application/json",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".mp3": "audio/mpeg",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".ogg": "audio/ogg",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }

    if mime := mime_types.get(ext):
        return mime
    else:
        # fallback to mimetypes, which may consult the registry
        mime, _encoding = mimetypes.guess_type(path)
        return mime or "application/octet-stream"


def _text_response(code: HTTPStatus, text: str) -> Response:
    """Return an error message.

    Response is returned as text/plain, so no escaping of untrusted
    input is required."""
    resp = flask.make_response(text, code)
    resp.headers["Content-type"] = "text/plain"
    return resp


class UnsafePathException(Exception):
    def __init__(self, path: str):
        super().__init__(f"Invalid path: {path}")


def ensure_safe_path(base_dir: str | Path, path: str | Path) -> str:
    base_dir = os.path.realpath(base_dir)
    path = os.path.normpath(path)
    fullpath = os.path.abspath(os.path.join(base_dir, path))

    # protect against directory traversal: https://security.openstack.org/guidelines/dg_using-file-paths.html
    if not fullpath.startswith(base_dir + os.sep):
        raise UnsafePathException(path)
    return fullpath


_LOCALHOST_HOSTS = ("127.0.0.1", "localhost", "[::1]")

_ALLOWED_ORIGIN_PREFIXES = tuple(
    f"{scheme}{host}" for scheme in ("http://", "https://") for host in _LOCALHOST_HOSTS
)


def is_localhost_origin(origin: str) -> bool:
    for prefix in _ALLOWED_ORIGIN_PREFIXES:
        if (
            origin == prefix
            or origin.startswith(prefix + ":")
            or origin.startswith(prefix + "/")
        ):
            return True
    return False


def _handle_local_file_request(request: LocalFileRequest) -> Response:
    directory = request.root
    path = request.path
    try:
        isdir = os.path.isdir(os.path.join(directory, path))
    except ValueError:
        return _text_response(
            HTTPStatus.BAD_REQUEST, f"Path for '{directory} - {path}' is too long!"
        )

    fullpath = ensure_safe_path(directory, path)

    if isdir:
        return _text_response(
            HTTPStatus.FORBIDDEN,
            f"Path for '{directory} - {path}' is a directory (not supported)!",
        )

    try:
        mimetype = _mime_for_path(fullpath)
        if os.path.exists(fullpath):
            if fullpath.endswith(".css"):
                # caching css files prevents flicker in the webview, but we want
                # a short cache
                max_age = 10
            elif fullpath.endswith(".js"):
                # don't cache js files
                max_age = 0
            else:
                max_age = 60 * 60
            response = flask.send_file(
                fullpath,
                mimetype=mimetype,
                conditional=True,
                max_age=max_age,
                download_name="foo",  # type: ignore[call-arg]
            )
            if request.untrusted:
                # Prevent user-provided HTML/SVG from running as an active document.
                response.headers["Content-Security-Policy"] = UNTRUSTED_MEDIA_CSP
            return response
        else:
            print(f"Not found: {path}")
            return _text_response(HTTPStatus.NOT_FOUND, f"Invalid path: {path}")

    except Exception as error:
        if dev_mode:
            print(
                "Caught HTTP server exception,\n%s"
                % "".join(traceback.format_exception(*sys.exc_info())),
            )

        # swallow it - user likely surfed away from
        # review screen before an image had finished
        # downloading
        return _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))


def _builtin_data(path: str) -> bytes:
    """Return data from file in aqt/data folder."""
    full_path = ensure_safe_path(aqt_data_path().parent, path)
    with open(full_path, "rb") as f:
        return f.read()


def _handle_builtin_file_request(request: BundledFileRequest) -> Response:
    path = request.path
    # do we need to serve the fallback page?
    immutable = "immutable" in path
    if path.startswith("sveltekit/") and not immutable:
        path = "sveltekit/index.html"
    mimetype = _mime_for_path(path)
    data_path = f"data/web/{path}"
    try:
        data = _builtin_data(data_path)
        response = Response(data, mimetype=mimetype)
        if immutable:
            response.headers["Cache-Control"] = "max-age=31536000"
        return response
    except FileNotFoundError:
        if dev_mode:
            print(f"404: {data_path}")
        resp = _text_response(HTTPStatus.NOT_FOUND, f"Invalid path: {path}")
        # we're including the path verbatim in our response, so we need to either use
        # plain text, or escape HTML characters to avoid reflecting untrusted input
        resp.headers["Content-type"] = "text/plain"
        return resp
    except Exception as error:
        if dev_mode:
            print(
                "Caught HTTP server exception,\n%s"
                % "".join(traceback.format_exception(*sys.exc_info())),
            )

        # swallow it - user likely surfed away from
        # review screen before an image had finished
        # downloading
        return _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))


@app.route("/<path:pathin>", methods=["GET", "POST"])
def handle_request(pathin: str) -> Response:
    if os.environ.get("ANKI_API_HOST") != "0.0.0.0":
        host = request.headers.get("Host", "").lower()
        origin = request.headers.get("Origin", "").lower()
        allowed_hosts = tuple(f"{h}:" for h in _LOCALHOST_HOSTS)
        if not any(host.startswith(h) for h in allowed_hosts):
            logger.warning("denied non-local host: %s", host)
            abort(403)
        if origin and not is_localhost_origin(origin):
            logger.warning("denied non-local origin: %s", origin)
            abort(403)

    req = _extract_request(pathin)
    if _should_log_request(pathin):
        logger.debug("%s /%s", flask.request.method, pathin)

    try:
        if isinstance(req, NotFound):
            print(req.message)
            return _text_response(HTTPStatus.NOT_FOUND, f"Invalid path: {pathin}")
        elif callable(req):
            return _handle_dynamic_request(req)
        elif isinstance(req, BundledFileRequest):
            return _handle_builtin_file_request(req)
        elif isinstance(req, LocalFileRequest):
            return _handle_local_file_request(req)
        else:
            return _text_response(HTTPStatus.FORBIDDEN, f"unexpected request: {pathin}")
    except UnsafePathException as exc:
        return _text_response(HTTPStatus.FORBIDDEN, str(exc))


def _should_log_request(pathin: str) -> bool:
    return pathin not in _QUIET_DEBUG_REQUEST_PATHS


def is_sveltekit_page(path: str) -> bool:
    page_name = path.split("/")[0]
    return page_name in [
        "graphs",
        "congrats",
        "card-info",
        "change-notetype",
        "deck-options",
        "dynamic-desired-retention-plot",
        "import-anki-package",
        "import-csv",
        "import-page",
        "image-occlusion",
    ]


def _extract_internal_request(
    path: str,
) -> BundledFileRequest | DynamicRequest | NotFound | None:
    "Catch /_anki references and rewrite them to web export folder."
    if is_sveltekit_page(path):
        path = f"_anki/sveltekit/_app/{path}"
    if path.startswith("_app/"):
        path = path.replace("_app", "_anki/sveltekit/_app")

    prefix = "_anki/"
    if not path.startswith(prefix):
        return None

    dirname = os.path.dirname(path)
    filename = os.path.basename(path)
    additional_prefix = None

    if dirname == "_anki":
        if flask.request.method == "POST":
            return _extract_collection_post_request(filename)
        elif get_handler := _extract_dynamic_get_request(filename):
            return get_handler

        # remap legacy top-level references
        base, ext = os.path.splitext(filename)
        if ext == ".css":
            additional_prefix = "css/"
        elif ext == ".js":
            if base in ("jquery-ui", "jquery", "plot"):
                additional_prefix = "js/vendor/"
            else:
                additional_prefix = "js/"
    # handle requests for vendored libraries
    elif dirname == "_anki/js/vendor":
        base, ext = os.path.splitext(filename)

        if base == "jquery":
            base = "jquery.min"
            additional_prefix = "js/vendor/"

        elif base == "jquery-ui":
            base = "jquery-ui.min"
            additional_prefix = "js/vendor/"

    if additional_prefix:
        oldpath = path
        path = f"{prefix}{additional_prefix}{base}{ext}"
        print(f"legacy {oldpath} remapped to {path}")

    return BundledFileRequest(path=path[len(prefix) :])


def _extract_addon_request(path: str) -> LocalFileRequest | NotFound | None:
    "Catch /_addons references and rewrite them to addons folder."
    prefix = "_addons/"
    if not path.startswith(prefix):
        return None

    addon_path = path[len(prefix) :]

    try:
        manager = aqt.mw.addonManager
    except AttributeError as error:
        if dev_mode:
            print(f"_redirectWebExports: {error}")
        return None

    try:
        addon, sub_path = addon_path.split("/", 1)
    except ValueError:
        return None
    if not addon:
        return None

    pattern = manager.getWebExports(addon)
    if not pattern:
        return None

    if re.fullmatch(pattern, sub_path):
        return LocalFileRequest(
            root=manager.addonsFolder(), path=addon_path, untrusted=False
        )

    return NotFound(message=f"couldn't locate item in add-on folder {path}")


def _extract_request(
    path: str,
) -> LocalFileRequest | BundledFileRequest | DynamicRequest | NotFound:
    if internal := _extract_internal_request(path):
        return internal
    elif addon := _extract_addon_request(path):
        return addon

    if not aqt.mw.col:
        return NotFound(message=f"collection not open, ignore request for {path}")

    path = hooks.media_file_filter(path)
    return LocalFileRequest(root=aqt.mw.col.media.dir(), path=path)


def congrats_info() -> bytes:
    if not aqt.mw.col.sched._is_finished():
        aqt.mw.taskman.run_on_main(lambda: aqt.mw.moveToState("overview"))
    return raw_backend_request("congrats_info")()


def get_deck_configs_for_update() -> bytes:
    return aqt.mw.col._backend.get_deck_configs_for_update_raw(request.data)


def _update_deck_configs(*, close_on_success: bool) -> bytes:  # complexipy: ignore
    # the regular change tracking machinery expects to be started on the main
    # thread and uses a callback on success, so we need to run this op on
    # main, and return immediately from the web request

    input = UpdateDeckConfigs()
    input.ParseFromString(request.data)
    completed_preset_names: set[str] = set()
    preset_log: list[str] = []
    zero_review_skip_logged = False
    first_progress_at: float | None = None
    smoothed_remaining: float | None = None
    preset_started_at: dict[str, float] = {}
    saved_preset_names: set[str] = set()
    updated_preset_names: set[str] = set()

    def format_elapsed_time(seconds: float) -> str:
        seconds = int(max(seconds, 0))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"

    def review_weighted_progress(progress: Any) -> tuple[int, float]:
        total_reviews = sum(
            preset.reviews for preset in progress.presets if not preset.skipped
        )
        completed_reviews = 0.0
        for preset in progress.presets:
            if preset.skipped:
                continue
            if preset.finished:
                completed_reviews += preset.reviews
            elif preset.total:
                completed_reviews += preset.reviews * preset.current / preset.total
        return total_reviews, completed_reviews

    def update_all_params_progress(val: Any, update: ProgressUpdate) -> None:
        nonlocal first_progress_at, smoothed_remaining, zero_review_skip_logged
        now = time.monotonic()
        if first_progress_at is None:
            first_progress_at = now
        update.max = max(val.total, 1)
        update.value = val.current
        pct = str(int(val.current / val.total * 100) if val.total > 0 else 0)
        total_reviews, completed_reviews = review_weighted_progress(val)
        elapsed = now - first_progress_at
        label_parts = [
            f"Optimizing presets: {val.current}/{val.total} ({pct}%)",
            f"elapsed: {format_elapsed_time(elapsed)}",
        ]
        if 0 < completed_reviews < total_reviews:
            remaining = (
                elapsed * (total_reviews - completed_reviews) / completed_reviews
            )
            if smoothed_remaining is None:
                smoothed_remaining = remaining
            else:
                smoothed_remaining = smoothed_remaining * 0.85 + remaining * 0.15
            label_parts.append(f"remaining: {format_elapsed_time(smoothed_remaining)}")
        update.label = " | ".join(label_parts)
        skipped_count = sum(1 for param_preset in val.presets if param_preset.skipped)
        if skipped_count and not zero_review_skip_logged:
            preset_log.append(f"[SKIP] {skipped_count} Presets with 0 reviews")
            zero_review_skip_logged = True
        for param_preset in val.presets:
            if (
                not param_preset.finished
                and not param_preset.skipped
                and param_preset.total > 0
            ):
                preset_started_at.setdefault(param_preset.name, now)
            if param_preset.name in completed_preset_names:
                continue
            if param_preset.skipped:
                completed_preset_names.add(param_preset.name)
            elif param_preset.finished:
                started_at = preset_started_at.get(param_preset.name, first_progress_at)
                duration = format_elapsed_time(now - started_at)
                preset_log.append(
                    f"[DONE] {param_preset.name} - {duration} "
                    f"({param_preset.reviews} reviews, "
                    f"long-term: {param_preset.long_term_reviews}, "
                    f"same-day: {param_preset.short_term_reviews})"
                )
                completed_preset_names.add(param_preset.name)
        update.details = "\n".join(preset_log[-12:]) if preset_log else None
        update.bars = [
            ProgressBarUpdate(
                label=f"{param_preset.name} ({param_preset.reviews} reviews)",
                value=param_preset.current
                if param_preset.total
                else int(param_preset.finished),
                max=max(param_preset.total, 1),
            )
            for param_preset in sorted(
                (
                    param_preset
                    for param_preset in val.presets
                    if not param_preset.finished and param_preset.total > 0
                ),
                key=lambda param_preset: param_preset.reviews,
                reverse=True,
            )
        ]

    def update_memory_progress(val: Any, update: ProgressUpdate) -> None:
        total_presets = max(val.total_presets, 1)
        current_preset = min(max(val.current_preset, 1), total_presets)
        update.max = total_presets
        update.value = current_preset
        preset_name = val.preset_name or tr.deck_config_shared_preset()
        if val.saving:
            update.label = val.label
            for memory_preset in val.presets:
                if (
                    not memory_preset.finished
                    or memory_preset.name in saved_preset_names
                ):
                    continue
                saved_preset_names.add(memory_preset.name)
                preset_log.append(f"[SAVE] {memory_preset.name}")
        else:
            action = "Rescheduling" if val.rescheduling else "Updating"
            update.label = (
                f"Saved optimized presets | {action} preset "
                f"{current_preset}/{total_presets}: {preset_name} | {val.label}"
            )
            for memory_preset in val.presets:
                if (
                    not memory_preset.finished
                    or memory_preset.name in updated_preset_names
                ):
                    continue
                updated_preset_names.add(memory_preset.name)
                done_action = "rescheduled" if memory_preset.rescheduling else "updated"
                preset_log.append(
                    f"[DONE] {memory_preset.name} - {done_action} "
                    f"{memory_preset.total_cards} cards"
                )
        update.details = "\n".join(preset_log[-12:]) if preset_log else None
        update.bars = [
            ProgressBarUpdate(
                label=(
                    memory_preset.name
                    if val.saving
                    else (
                        f"{memory_preset.name} ({memory_preset.total_cards} cards)"
                        if memory_preset.total_cards
                        else f"{memory_preset.name} (waiting)"
                    )
                ),
                value=memory_preset.current_cards,
                max=max(memory_preset.total_cards, 1),
            )
            for memory_preset in sorted(
                (
                    memory_preset
                    for memory_preset in val.presets
                    if not memory_preset.finished
                ),
                key=lambda memory_preset: memory_preset.total_cards,
                reverse=True,
            )
        ]

    def on_progress(progress: Progress, update: ProgressUpdate) -> None:
        if progress.HasField("compute_memory"):
            update_memory_progress(progress.compute_memory, update)
        elif progress.HasField("compute_params"):
            val2 = progress.compute_params
            # prevent an indeterminate progress bar from appearing at the start of each preset
            update.max = max(val2.total, 1)
            update.value = val2.current
            pct = str(int(val2.current / val2.total * 100) if val2.total > 0 else 0)
            label = tr.deck_config_optimizing_preset(
                current_count=val2.current_preset, total_count=val2.total_presets
            )
            if val2.reviews:
                reviews = tr.deck_config_percent_of_reviews(
                    pct=pct, reviews=val2.reviews
                )
                reviews += (
                    f" (long-term: {val2.long_term_reviews}, "
                    f"same-day: {val2.short_term_reviews})"
                )
            else:
                reviews = tr.qt_misc_processing()

            update.label = label + "\n" + reviews
        elif progress.HasField("compute_all_params"):
            update_all_params_progress(progress.compute_all_params, update)
        else:
            return
        if update.user_wants_abort:
            update.abort = True

    def on_success(changes: OpChanges) -> None:
        if isinstance(window := aqt.mw.app.activeModalWidget(), DeckOptionsDialog):
            if close_on_success:
                window.reject()
            else:
                window.web.eval("anki.deckOptionsSaved();")

    def handle_on_main() -> None:
        update_deck_configs_op(parent=aqt.mw, input=input).success(
            on_success
        ).with_backend_progress(on_progress).run_in_background()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def update_deck_configs() -> bytes:
    return _update_deck_configs(close_on_success=False)


def update_deck_configs_and_close() -> bytes:
    return _update_deck_configs(close_on_success=True)


def get_scheduling_states_with_context() -> bytes:
    return SchedulingStatesWithContext(
        states=aqt.mw.reviewer.get_scheduling_states(),
        context=aqt.mw.reviewer.get_scheduling_context(),
    ).SerializeToString()


def set_scheduling_states() -> bytes:
    states = SetSchedulingStatesRequest()
    states.ParseFromString(request.data)
    aqt.mw.reviewer.set_scheduling_states(states)
    return b""


def import_done() -> bytes:
    def update_window_modality() -> None:
        if window := aqt.mw.app.activeModalWidget():
            from aqt.import_export.import_dialog import ImportDialog

            if isinstance(window, ImportDialog):
                window.hide()
                window.setWindowModality(Qt.WindowModality.NonModal)
                window.show()

    aqt.mw.taskman.run_on_main(update_window_modality)
    return b""


def import_request(endpoint: str) -> bytes:
    output = raw_backend_request(endpoint)()
    response = OpChangesOnly()
    response.ParseFromString(output)

    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        on_op_finished(aqt.mw, response, window)

    aqt.mw.taskman.run_on_main(handle_on_main)

    return output


def import_csv() -> bytes:
    return import_request("import_csv")


def import_anki_package() -> bytes:
    return import_request("import_anki_package")


def import_json_file() -> bytes:
    return import_request("import_json_file")


def import_json_string() -> bytes:
    return import_request("import_json_string")


def search_in_browser() -> bytes:
    node = SearchNode()
    node.ParseFromString(request.data)

    def handle_on_main() -> None:
        aqt.dialogs.open("Browser", aqt.mw, search=(node,))

    aqt.mw.taskman.run_on_main(handle_on_main)

    return b""


def change_notetype() -> bytes:
    data = request.data

    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, ChangeNotetypeDialog):
            window.save(data)

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def deck_options_require_close() -> bytes:
    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, DeckOptionsDialog):
            window.require_close()

    # on certain linux systems, askUser's QMessageBox.question unsets the active window
    # so we wait for the next event loop before querying the next current active window
    aqt.mw.taskman.run_on_main(lambda: QTimer.singleShot(0, handle_on_main))
    return b""


def deck_options_ready() -> bytes:
    def handle_on_main() -> None:
        window = aqt.mw.app.activeModalWidget()
        if isinstance(window, DeckOptionsDialog):
            window.set_ready()

    aqt.mw.taskman.run_on_main(handle_on_main)
    return b""


def build_rwkv_state_cache() -> bytes:
    aqt.rwkv_scheduler.build_rwkv_state_cache_with_progress(aqt.mw)
    return b""


def force_build_rwkv_state_cache() -> bytes:
    aqt.rwkv_scheduler.build_rwkv_state_cache_with_progress(
        aqt.mw,
        force_rebuild=True,
    )
    return b""


def recompute_rwkv_calibration_data() -> bytes:
    aqt.rwkv_scheduler.recompute_rwkv_calibration_data_with_progress(aqt.mw)
    return b""


def compare_rwkv_extra_feature_metrics() -> bytes:
    payload_request = generic_pb2.Json()
    payload_request.ParseFromString(request.data)
    payload: dict[str, object] = {}
    if payload_request.json:
        try:
            value = json.loads(payload_request.json.decode("utf8"))
        except Exception:
            logger.debug("failed to decode RWKV extra feature comparison payload")
        else:
            if isinstance(value, dict):
                payload = value
    (
        deck_id,
        extra_feature_override,
    ) = aqt.rwkv_scheduler.rwkv_extra_feature_comparison_request_from_payload(
        payload,
    )
    aqt.rwkv_scheduler.compare_rwkv_extra_feature_metrics_with_progress(
        aqt.mw,
        deck_id=deck_id,
        extra_feature_override=extra_feature_override,
    )
    return b""


def train_rwkv_self_correction_calibration() -> bytes:
    payload_request = generic_pb2.Json()
    payload_request.ParseFromString(request.data)
    payload: dict[str, object] = {}
    if payload_request.json:
        try:
            value = json.loads(payload_request.json.decode("utf8"))
        except Exception:
            logger.debug("failed to decode RWKV calibration training payload")
        else:
            if isinstance(value, dict):
                payload = value
    (
        deck_id,
        config_id,
        extra_feature_override,
    ) = aqt.rwkv_scheduler.rwkv_self_correction_training_request_from_payload(
        payload,
    )
    aqt.rwkv_scheduler.train_rwkv_self_correction_calibration_with_progress(
        aqt.mw,
        deck_id=deck_id,
        config_id=config_id,
        extra_feature_override=extra_feature_override,
    )
    return b""


def reschedule_rwkv_review_cards() -> bytes:
    deck_id_request = decks_pb2.DeckId()
    deck_id_request.ParseFromString(request.data)
    deck_id = deck_id_request.did or None
    aqt.rwkv_scheduler.reschedule_rwkv_review_cards_with_progress(
        aqt.mw,
        deck_id=deck_id,
    )
    return b""


def simulate_rwkv_workload() -> bytes:
    return aqt.rwkv_scheduler.simulate_rwkv_workload_bytes(request.data)


def start_rwkv_workload() -> bytes:
    return aqt.rwkv_scheduler.start_rwkv_workload_bytes(request.data)


def rwkv_workload_result() -> Response | bytes:
    result = aqt.rwkv_scheduler.rwkv_workload_result_bytes()
    if result is None:
        return _text_response(HTTPStatus.ACCEPTED, "")
    return result


def cancel_rwkv_workload() -> bytes:
    aqt.rwkv_scheduler.cancel_rwkv_workload()
    return b""


def rwkv_workload_progress() -> bytes:
    return aqt.rwkv_scheduler.rwkv_workload_progress_bytes()


def save_custom_colours() -> bytes:
    colors = [
        QColorDialog.customColor(i).name(QColor.NameFormat.HexRgb)
        for i in range(QColorDialog.customCount())
    ]
    aqt.mw.col.set_config("customColorPickerPalette", colors)
    return b""


def card_stats() -> bytes:
    start = time.monotonic()
    hook_count = gui_hooks.card_info_will_add_rows.count()
    reviewer = getattr(aqt.mw, "reviewer", None) or SimpleNamespace(mw=aqt.mw)
    backend_start = time.monotonic()
    raw_output = aqt.mw.col._backend.card_stats_raw(request.data)
    backend_elapsed_ms = (time.monotonic() - backend_start) * 1000
    response: CardStatsResponse | None = None
    card: Any | None = None
    rwkv_review_enabled = False
    if hook_count == 0 and not aqt.rwkv_scheduler.has_reviewer_prediction(reviewer):
        response = CardStatsResponse()
        response.ParseFromString(raw_output)
        card = aqt.mw.col.get_card(CardId(response.card_id))
        rwkv_review_enabled = aqt.rwkv_scheduler.rwkv_review_enabled(reviewer, card)
        if not rwkv_review_enabled:
            logger.debug(
                "card stats served: hook_count=%s backend_elapsed_ms=%.1f elapsed_ms=%.1f",
                hook_count,
                backend_elapsed_ms,
                (time.monotonic() - start) * 1000,
            )
            return raw_output
        aqt.rwkv_scheduler.has_reviewer_backend()

    if response is None:
        response = CardStatsResponse()
        response.ParseFromString(raw_output)

    from aqt.browser.card_info import CardInfoRow

    rows: list[CardInfoRow] = []
    if card is None:
        card = aqt.mw.col.get_card(CardId(response.card_id))
    for label, value in aqt.rwkv_scheduler.rwkv_card_info_rows(
        reviewer=reviewer,
        card=card,
        fallback_source=_card_stats_fallback_retrievability_source(response),
    ):
        rows.append(CardInfoRow(label=label, value=value))

    hook_start = time.monotonic()
    gui_hooks.card_info_will_add_rows(rows, card)
    hook_elapsed_ms = (time.monotonic() - hook_start) * 1000

    for row in rows:
        response.extra_rows.add(label=row.label, value=row.value)

    logger.debug(
        "card stats served: card_id=%s hook_count=%s extra_rows=%s backend_elapsed_ms=%.1f "
        "hook_elapsed_ms=%.1f elapsed_ms=%.1f",
        response.card_id,
        hook_count,
        len(rows),
        backend_elapsed_ms,
        hook_elapsed_ms,
        (time.monotonic() - start) * 1000,
    )
    return response.SerializeToString()


def _card_stats_fallback_retrievability_source(response: CardStatsResponse) -> str:
    return "FSRS" if response.HasField("memory_state") else "SM2"


def graphs() -> Response:
    start = time.monotonic()
    request_proto = GraphsRequest()
    request_proto.ParseFromString(request.data)
    reviewer = getattr(aqt.mw, "reviewer", None) or SimpleNamespace(mw=aqt.mw)
    prepare_start = time.monotonic()
    prepare_status = aqt.rwkv_scheduler.prepare_stats_retrievability_scores(
        reviewer,
        request_proto.search,
    )
    prepare_elapsed_ms = (time.monotonic() - prepare_start) * 1000
    backend_start = time.monotonic()
    output = raw_backend_request("graphs")()
    backend_elapsed_ms = (time.monotonic() - backend_start) * 1000
    response = flask.make_response(output)
    response.headers["Content-Type"] = "application/binary"
    if prepare_status == aqt.rwkv_scheduler.RwkvStatsPreparationStatus.PENDING:
        response.headers[RWKV_STATS_PENDING_HEADER] = "1"
    logger.debug(
        "graphs served: search=%r days=%s rwkv_prepare_status=%s prepare_elapsed_ms=%.1f "
        "backend_elapsed_ms=%.1f response_bytes=%s elapsed_ms=%.1f",
        request_proto.search,
        request_proto.days,
        prepare_status.value,
        prepare_elapsed_ms,
        backend_elapsed_ms,
        len(output),
        (time.monotonic() - start) * 1000,
    )
    return response


post_handler_list = [
    congrats_info,
    get_deck_configs_for_update,
    update_deck_configs,
    update_deck_configs_and_close,
    get_scheduling_states_with_context,
    set_scheduling_states,
    change_notetype,
    import_done,
    import_csv,
    import_anki_package,
    import_json_file,
    import_json_string,
    search_in_browser,
    deck_options_require_close,
    deck_options_ready,
    build_rwkv_state_cache,
    force_build_rwkv_state_cache,
    recompute_rwkv_calibration_data,
    compare_rwkv_extra_feature_metrics,
    train_rwkv_self_correction_calibration,
    reschedule_rwkv_review_cards,
    simulate_rwkv_workload,
    start_rwkv_workload,
    rwkv_workload_result,
    cancel_rwkv_workload,
    rwkv_workload_progress,
    save_custom_colours,
    card_stats,
    graphs,
]


exposed_backend_list = [
    # CollectionService
    "latest_progress",
    "get_custom_colours",
    # DeckService
    "get_deck_names",
    # I18nService
    "i18n_resources",
    # ImportExportService
    "get_csv_metadata",
    "get_import_anki_package_presets",
    # NotesService
    "get_field_names",
    "get_note",
    # NotetypesService
    "get_notetype_names",
    "get_change_notetype_info",
    # StatsService
    "get_review_logs",
    "get_graph_preferences",
    "set_graph_preferences",
    # TagsService
    "complete_tag",
    # ImageOcclusionService
    "get_image_for_occlusion",
    "add_image_occlusion_note",
    "get_image_occlusion_note",
    "update_image_occlusion_note",
    "get_image_occlusion_fields",
    # SchedulerService
    "compute_fsrs_params",
    "compute_optimal_retention",
    "get_fsrs_new_card_intervals",
    "set_wants_abort",
    "evaluate_params",
    "evaluate_params_legacy",
    "get_optimal_retention_parameters",
    "simulate_fsrs_review",
    "simulate_fsrs_workload",
    "fsrs_next_interval",
    "fsrs_interval_at_retrievability",
    "fsrs_interval_at_retrievability_batch",
    "fsrs_interval_at_retrievability_variable_batch",
    "fsrs_interval_at_retrievability_by_config_batch",
    # DeckConfigService
    "get_ignored_before_count",
    "get_retention_workload",
]


def raw_backend_request(endpoint: str) -> Callable[[], bytes]:
    # check for key at startup
    from anki._backend import RustBackend

    assert hasattr(RustBackend, f"{endpoint}_raw")

    return lambda: getattr(aqt.mw.col._backend, f"{endpoint}_raw")(request.data)


# all methods in here require a collection
post_handlers = {
    stringcase.camelcase(handler.__name__): handler for handler in post_handler_list
} | {
    stringcase.camelcase(handler): raw_backend_request(handler)
    for handler in exposed_backend_list
}


def _extract_collection_post_request(path: str) -> DynamicRequest | NotFound:
    if not aqt.mw.col:
        return NotFound(message=f"collection not open, ignore request for {path}")
    if handler := post_handlers.get(path):
        # convert bytes/None into response
        def wrapped() -> Response:
            try:
                data = handler()
                if isinstance(data, Response):
                    response = data
                elif data:
                    response = flask.make_response(data)
                    response.headers["Content-Type"] = "application/binary"
                else:
                    response = _text_response(HTTPStatus.NO_CONTENT, "")
            except Exception as exc:
                print(traceback.format_exc())
                response = _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return response

        return wrapped
    else:
        return NotFound(message=f"{path} not found")


def _check_dynamic_request_permissions():
    if request.method == "GET":
        return

    def warn() -> None:
        show_warning(
            "Unexpected API access. Please report this message on the Anki forums."
        )

    # check content type header to ensure this isn't an opaque request from another origin
    if request.headers["Content-type"] != "application/binary":
        aqt.mw.taskman.run_on_main(warn)
        abort(403)

    # does page have access to entire API?
    if _have_api_access():
        return

    # whitelisted API endpoints for reviewer/previewer
    if request.path in (
        "/_anki/getSchedulingStatesWithContext",
        "/_anki/setSchedulingStates",
        "/_anki/i18nResources",
        "/_anki/congratsInfo",
    ):
        pass
    else:
        # other legacy pages may contain third-party JS, so we do not
        # allow them to access our API
        aqt.mw.taskman.run_on_main(warn)
        abort(403)


def _handle_dynamic_request(req: DynamicRequest) -> Response:
    _check_dynamic_request_permissions()
    try:
        return req()
    except Exception as e:
        return _text_response(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))


def legacy_page_data() -> Response:
    id = int(request.args["id"])
    page = aqt.mw.mediaServer.get_page(id)
    if page:
        response = Response(page.html, mimetype="text/html")
        # Prevent JS in field content from being executed in the editor, as it would
        # have access to our internal API, and is a security risk.
        if page.context == PageContext.EDITOR:
            response.headers["Content-Security-Policy"] = (
                _editor_content_security_policy(aqt.mw.mediaServer.getPort())
            )
        return response
    else:
        return _text_response(HTTPStatus.NOT_FOUND, "page not found")


_APIKEY = secrets.token_urlsafe(32)


def _have_api_access() -> bool:
    return (
        request.headers.get("Authorization") == f"Bearer {_APIKEY}"
        or os.environ.get("ANKI_API_HOST") == "0.0.0.0"
    )


# this currently only handles a single method; in the future, idempotent
# requests like i18nResources should probably be moved here
def _extract_dynamic_get_request(path: str) -> DynamicRequest | None:
    if path == "legacyPageData":
        return legacy_page_data
    else:
        return None
