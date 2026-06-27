#!/usr/bin/env python
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""
Standalone launcher for Playwright TS e2e tests.

Seeds a throwaway ANKI_BASE so Anki skips the language picker and the
profile chooser, then spawns Anki with mediasrv pinned to a known local
port. Playwright's webServer config invokes this script and polls an HTTP
page served by mediasrv before letting tests run.

This script duplicates _seed_prefs from qt/tests/conftest.py on purpose so
the pytest harness and the TS harness stay independent. Keep the two copies
in sync if you change the seed schema.
"""

from __future__ import annotations

import os
import pickle
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MEDIASRV_PORT = int(os.environ.get("ANKI_API_PORT", "40000"))
TEST_PROFILE = "test"
LOCAL_PYTHON_PATHS = ["pylib", "qt", "out/pylib", "out/qt"]
SEED_REVIEW_CARD_BATCH_SIZE = 1000
SEED_REVLOG_BATCH_SIZE = 5000


def _seed_prefs(base: Path) -> None:
    meta = {
        "ver": 0,
        "updates": False,
        "created": int(time.time()),
        "id": random.randrange(0, 2**63),
        "lastMsg": 0,
        "suppressUpdate": True,
        "firstRun": False,
        "defaultLang": "en_US",
        # The real switch for setup_auto_update — checked in
        # qt/aqt/main.py:setup_auto_update via pm.check_for_updates().
        # "suppressUpdate" only suppresses a single dismissed version string.
        "check_for_updates": False,
    }
    profile = {
        "mainWindowGeom": None,
        "mainWindowState": None,
        "numBackups": 50,
        "lastOptimize": int(time.time()),
        "searchHistory": [],
        "syncKey": None,
        "syncMedia": True,
        "autoSync": False,
        "allowHTML": False,
        "importMode": 1,
        "lastColour": "#00f",
        "stripHTML": True,
        "deleteMedia": False,
    }
    db_path = base / "prefs21.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "create table profiles (name text primary key collate nocase, data blob not null)"
    )
    conn.execute(
        "insert into profiles values ('_global', ?)",
        (pickle.dumps(meta, protocol=4),),
    )
    conn.execute(
        "insert into profiles values (?, ?)",
        (TEST_PROFILE, pickle.dumps(profile, protocol=4)),
    )
    conn.commit()
    conn.close()


def _local_python_path_code() -> str:
    return f"sys.path.extend({LOCAL_PYTHON_PATHS!r})"


def _nonnegative_int_from_env(name: str) -> int:
    value = os.environ.get(name)
    if not value:
        return 0

    try:
        count = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None

    if count < 0:
        raise ValueError(f"{name} must not be negative")

    return count


def _seed_review_card_count() -> int:
    return _nonnegative_int_from_env("ANKI_E2E_SEED_REVIEW_CARDS")


def _seed_reviews_per_card() -> int:
    return _nonnegative_int_from_env("ANKI_E2E_SEED_REVIEWS_PER_CARD")


def _seed_review_cards(base: Path, count: int, reviews_per_card: int) -> None:
    if count <= 0:
        return

    sys.path.extend(LOCAL_PYTHON_PATHS)

    from anki.collection import AddNoteRequest, Collection
    from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV
    from anki.decks import DEFAULT_DECK_CONF_ID, DeckId

    profile_dir = base / TEST_PROFILE
    profile_dir.mkdir()
    collection_path = profile_dir / "collection.anki2"
    col = Collection(str(collection_path))
    try:
        col.set_config("fsrs", True)
        config = col.decks.get_config(DEFAULT_DECK_CONF_ID)
        assert config is not None
        config["rwkvReviewEnabled"] = True
        config["rwkvReviewBatchSize"] = 512
        col.decks.update_config(config)

        notetype = col.models.by_name("Basic")
        assert notetype is not None

        deck_id = DeckId(1)
        for start in range(0, count, SEED_REVIEW_CARD_BATCH_SIZE):
            requests = []
            for index in range(start, min(start + SEED_REVIEW_CARD_BATCH_SIZE, count)):
                note = col.new_note(notetype)
                note.fields[0] = f"front {index}"
                note.fields[1] = f"back {index}"
                requests.append(AddNoteRequest(note=note, deck_id=deck_id))
            col.add_notes(requests)

        review_data = f'{{"lrt":{int(time.time()) - 3 * 86_400}}}'
        col.db.execute(
            """
update cards
set type = ?, queue = ?, due = ?, ivl = ?, factor = ?, reps = ?, lapses = ?, data = ?
""",
            int(CARD_TYPE_REV),
            int(QUEUE_TYPE_REV),
            col.sched.today,
            7,
            2500,
            3,
            0,
            review_data,
        )
        _seed_review_history(col, reviews_per_card)
    finally:
        col.close()

    print(
        f"Seeded e2e collection with {count} RWKV-enabled review cards"
        f" and {count * reviews_per_card} synthetic revlog rows",
        flush=True,
    )


def _seed_review_history(col: object, reviews_per_card: int) -> None:
    if reviews_per_card <= 0:
        return

    card_ids = [
        int(card_id) for card_id in col.db.list("select id from cards order by id")
    ]
    now_millis = int(time.time() * 1000)
    day_millis = 86_400 * 1000
    rows = []
    entry_index = 0

    for card_index, card_id in enumerate(card_ids):
        for review_index in range(reviews_per_card):
            days_ago = ((card_index + review_index * 17) % 365) + 1
            interval = min(365, max(1, review_index + 1))
            rows.append(
                (
                    now_millis - days_ago * day_millis + entry_index,
                    card_id,
                    -1,
                    ((card_index + review_index) % 4) + 1,
                    interval,
                    max(1, interval - 1),
                    2500,
                    3000 + (card_index % 120) * 100,
                    1,
                )
            )
            entry_index += 1

            if len(rows) >= SEED_REVLOG_BATCH_SIZE:
                _insert_revlog_rows(col, rows)
                rows.clear()

    if rows:
        _insert_revlog_rows(col, rows)


def _insert_revlog_rows(
    col: object, rows: list[tuple[int, int, int, int, int, int, int, int, int]]
) -> None:
    col.db.executemany(
        "insert or ignore into revlog values (?,?,?,?,?,?,?,?,?)",
        rows,
    )


def _run_command() -> list[str]:
    if os.environ.get("ANKI_E2E_FAKE_RWKV_BACKEND") == "1":
        code = f"""
import sys
{_local_python_path_code()}

import aqt
from aqt import rwkv_scheduler


class E2eRwkvBackend:
    def warm_up(self, reviews):
        pass

    def predict_review(self, *, reviewer, card):
        return rwkv_scheduler.RwkvReviewPrediction(retrievability=0.5)

    def predict_reviews(self, candidates):
        predictions = []
        for candidate in candidates:
            card_id = getattr(candidate.card, "id", 0)
            predictions.append(
                rwkv_scheduler.RwkvReviewPrediction(
                    retrievability=0.3 + (card_id % 60) / 100,
                )
            )
        return predictions

    def review_answered(self, *, reviewer, card, ease):
        pass


rwkv_scheduler.set_reviewer_backend(E2eRwkvBackend())
aqt.run()
"""
        return [sys.executable, "-c", code, "-p", TEST_PROFILE]

    return [sys.executable, str(REPO_ROOT / "tools" / "run.py"), "-p", TEST_PROFILE]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="anki-e2e-") as base_str:
        base = Path(base_str)
        _seed_prefs(base)
        _seed_review_cards(base, _seed_review_card_count(), _seed_reviews_per_card())

        env = {
            **os.environ,
            "ANKI_BASE": str(base),
            "ANKI_API_PORT": str(MEDIASRV_PORT),
            "ANKI_SINGLE_INSTANCE_KEY": f"anki-e2e-{base.name}",
            # Documented testing escape: makes _have_api_access() return True
            # for all /_anki/* requests so external clients (Playwright's own
            # Chromium) can hit the API without injecting Authorization
            # headers. Side effect: mediasrv binds to all interfaces. Tolerable
            # on a dev machine; do not enable in shared environments.
            "ANKI_API_HOST": "0.0.0.0",
            "ANKIDEV": "1",
            "PYTHONPYCACHEPREFIX": str(REPO_ROOT / "out" / "pycache"),
            "RUST_BACKTRACE": "1",
            # Headless Qt: the e2e harness only needs mediasrv's HTTP stack,
            # not a visible window. The offscreen platform plugin renders to
            # memory and requires no display server.
            "QT_QPA_PLATFORM": "offscreen",
            # Flush Python output immediately so Playwright captures it.
            "PYTHONUNBUFFERED": "1",
        }
        env.pop("QTWEBENGINE_REMOTE_DEBUGGING", None)
        env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
        proc = subprocess.Popen(_run_command(), env=env)

        def _forward(signum: int, _frame: object) -> None:
            proc.terminate()

        signal.signal(signal.SIGTERM, _forward)
        signal.signal(signal.SIGINT, _forward)

        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
