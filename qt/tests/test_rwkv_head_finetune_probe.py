# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")  # type: ignore[assignment]

from tools import rwkv_head_finetune_probe as probe


def test_split_samples_uses_chronological_order() -> None:
    samples = _samples(10)

    split = probe._split_samples(
        samples,
        train_fraction=0.60,
        validation_fraction=0.20,
    )

    assert split.train.review_ids == [100, 101, 102, 103, 104, 105]
    assert split.validation.review_ids == [106, 107]
    assert split.test.review_ids == [108, 109]


def test_with_raw_logit_feature_appends_standardized_logit() -> None:
    original = probe.SplitData(
        train=_samples(3, raw_logits=[1.0, 3.0, 5.0]),
        validation=_samples(1, start=10, raw_logits=[7.0]),
        test=_samples(1, start=20, raw_logits=[-1.0]),
    )
    standardized = probe.SplitData(
        train=probe._replace_features(original.train, torch.zeros(3, 2)),
        validation=probe._replace_features(original.validation, torch.zeros(1, 2)),
        test=probe._replace_features(original.test, torch.zeros(1, 2)),
    )

    result = probe._with_raw_logit_feature(standardized, original)

    assert result.train.features.shape == (3, 3)
    assert result.validation.features.shape == (1, 3)
    assert torch.allclose(
        result.train.features[:, 2],
        torch.tensor([-1.0, 0.0, 1.0]),
    )
    assert result.validation.features[0, 2].item() == 2.0
    assert result.test.features[0, 2].item() == -2.0


def test_fit_binary_head_can_learn_simple_signal() -> None:
    torch.manual_seed(1)
    features = torch.linspace(-3.0, 3.0, 80).unsqueeze(1)
    labels = (features.squeeze(1) > 0).float()
    samples = probe.ExtractedSamples(
        features=features,
        raw_logits=torch.zeros(80, 1),
        raw_predictions=torch.full((80,), 0.5),
        labels=labels,
        recall_bins=[(0, 0, 0) for _ in range(80)],
        review_ids=list(range(80)),
    )
    split = probe._split_samples(
        samples,
        train_fraction=0.60,
        validation_fraction=0.20,
    )

    result = probe._fit_binary_head(
        split,
        learning_rate=0.1,
        max_epochs=100,
        patience=20,
        l2=0.0,
    )

    assert result["test"]["log_loss"] < 0.30
    assert result["test"]["brier"] < 0.10


def test_historical_state_matches_training_dataset_mapping() -> None:
    assert probe._historical_state(0, is_learning_start=True) == 0
    assert probe._historical_state(0) == 1
    assert probe._historical_state(1) == 2
    assert probe._historical_state(2) == 3
    assert probe._historical_state(3) == 4
    assert probe._historical_state(4) == 5


def test_preset_resolution_batches_backend_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_PRESET_ID_RESOLUTION_BATCH_SIZE", 2)
    preset_ids = {card_id: f"preset:{card_id}" for card_id in range(1, 6)}
    backend = _PresetBackend(preset_ids)

    resolved = probe._resolved_preset_ids_for_cards(
        SimpleNamespace(_backend=backend),
        [1, 2, 3, 4, 5],
    )

    assert resolved == preset_ids
    assert backend.calls == [[1, 2], [3, 4], [5]]


def test_preset_resolution_fails_closed_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_PRESET_ID_RESOLUTION_BATCH_SIZE", 2)
    backend = _PresetBackend(
        {
            1: "preset:1",
            2: "preset:2",
            4: "preset:4",
            5: "preset:5",
        }
    )

    with pytest.raises(probe.ProbeError, match=r"card ids: 3"):
        probe._resolved_preset_ids_for_cards(
            SimpleNamespace(_backend=backend),
            [1, 2, 3, 4, 5],
        )

    assert backend.calls == [[1, 2], [3, 4], [5]]


def test_review_rows_use_effective_deck_and_backend_addon_preset() -> None:
    review_id = (40 * 86_400 + 100) * 1000
    card_id = review_id - 3 * 86_400 * 1000
    collection = _collection(
        cards=[(card_id, 10, 900, 100)],
        reviews=[(review_id, card_id, 3, 1234, 0, 2500, 20)],
        preset_ids={card_id: "addon:test:medical"},
        config={
            "id": 1000,
            "rwkvReviewEnabled": True,
            "rwkvReviewFirstReviewElapsedFromCardCreation": True,
        },
    )
    try:
        rows = probe._load_review_rows_from_collection(
            collection,
            deck_match="Home",
            limit=0,
        )
    finally:
        collection.db.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.deck_id == 100
    assert row.preset_id == _expected_preset_hash("addon:test:medical")
    assert row.elapsed_seconds == 3 * 86_400
    assert row.elapsed_days == 3
    assert row.day_offset == 40
    assert collection._backend.calls == [[card_id]]
    assert probe._model_row(row)["preset_id"] == row.preset_id


def test_review_rows_fail_when_backend_does_not_resolve_preset() -> None:
    review_id = (40 * 86_400 + 100) * 1000
    collection = _collection(
        cards=[(1, 10, 100, 0)],
        reviews=[(review_id, 1, 3, 1234, 0, 2500, 20)],
        preset_ids={},
        config={"id": 1000, "rwkvReviewEnabled": True},
    )
    try:
        with pytest.raises(probe.ProbeError, match="did not resolve FSRS presets"):
            probe._load_review_rows_from_collection(
                collection,
                deck_match=None,
                limit=0,
            )
    finally:
        collection.db.close()


def test_dynamic_preset_replay_uses_previous_interval_and_search() -> None:
    first_review = (39 * 86_400 + 100) * 1000
    second_review = (40 * 86_400 + 100) * 1000
    third_review = (41 * 86_400 + 100) * 1000
    card_id = 1
    search = 'deck:"Home"'
    collection = _collection(
        cards=[(card_id, 10, 100, 0)],
        reviews=[
            (first_review, card_id, 2, 1000, 0, 2500, 20),
            (second_review, card_id, 3, 1000, 1, 2500, 30),
            (third_review, card_id, 4, 1000, 1, 2500, 40),
        ],
        preset_ids={card_id: "addon:test:current"},
        config={
            "id": 1000,
            "rwkvReviewEnabled": True,
            "rwkvReviewDynamicPresetReplay": True,
            "rwkvReviewFirstReviewElapsedFromCardCreation": False,
        },
        overlay={
            "simulator_rules": [
                {
                    "preset_id": "addon:test:young",
                    "search": search,
                    "max_interval_days": 20.0,
                },
                {
                    "preset_id": "addon:test:mature",
                    "search": search,
                    "min_interval_days": 21.0,
                },
            ]
        },
        search_results={search: [card_id]},
    )
    try:
        rows = probe._load_review_rows_from_collection(
            collection,
            deck_match=None,
            limit=0,
        )
    finally:
        collection.db.close()

    assert [row.preset_id for row in rows] == [
        _expected_preset_hash("addon:test:young"),
        _expected_preset_hash("addon:test:young"),
        _expected_preset_hash("addon:test:mature"),
    ]
    assert rows[0].elapsed_seconds == -1
    assert collection.find_calls == [search]


def test_historical_preset_rules_deduplicate_and_intersect_searches() -> None:
    search = 'deck:"Home"'
    overlay = {
        "simulator_rules": [
            {
                "preset_id": "addon:test:young",
                "search": search,
                "max_interval_days": 20.0,
            },
            {
                "preset_id": "addon:test:mature",
                "search": search,
                "min_interval_days": 21.0,
            },
        ]
    }
    find_calls: list[str] = []

    def find_cards(search_text: str, *, order: bool) -> list[int]:
        assert order is False
        find_calls.append(search_text)
        return [1, 2, 3]

    collection = SimpleNamespace(
        get_config=lambda key: overlay if key == "fsrsPresetOverlay" else None,
        find_cards=find_cards,
    )

    rules = probe._historical_preset_rules(collection, [2, 4])

    assert [rule.card_ids for rule in rules] == [
        frozenset({2}),
        frozenset({2}),
    ]
    assert find_calls == [search]


def test_historical_preset_rules_skip_search_without_candidates() -> None:
    collection = SimpleNamespace(
        get_config=lambda _key: pytest.fail("config should not be read"),
        find_cards=lambda _search, *, order: pytest.fail(
            f"search should not run: {order=}"
        ),
    )

    assert probe._historical_preset_rules(collection, []) == []


def test_analysis_backend_opens_only_a_disposable_second_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "collection-copy.anki2"
    source.write_bytes(b"stable source")
    source.with_name(f"{source.name}-wal").write_bytes(b"stable wal")
    opened_paths: list[Path] = []
    closed: list[bool] = []

    class FakeCollection:
        def __init__(self, path: str) -> None:
            opened = Path(path)
            opened_paths.append(opened)
            assert opened != source
            assert opened.read_bytes() == b"stable source"
            assert opened.with_name(f"{opened.name}-wal").read_bytes() == b"stable wal"

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(probe, "_anki_collection_type", lambda: FakeCollection)

    with probe._open_analysis_collection_copy(source):
        assert opened_paths[0].exists()

    assert closed == [True]
    assert not opened_paths[0].exists()
    assert source.read_bytes() == b"stable source"
    assert source.with_name(f"{source.name}-wal").read_bytes() == b"stable wal"


def _samples(
    count: int,
    *,
    start: int = 0,
    raw_logits: list[float] | None = None,
) -> probe.ExtractedSamples:
    raw_logit_values = raw_logits or [float(index) for index in range(count)]
    return probe.ExtractedSamples(
        features=torch.arange(count * 2, dtype=torch.float32).reshape(count, 2),
        raw_logits=torch.tensor(raw_logit_values, dtype=torch.float32).unsqueeze(1),
        raw_predictions=torch.full((count,), 0.5),
        labels=torch.tensor([index % 2 for index in range(count)], dtype=torch.float32),
        recall_bins=[(0, 0, 0) for _ in range(count)],
        review_ids=list(range(100 + start, 100 + start + count)),
    )


class _PresetBackend:
    def __init__(self, preset_ids: dict[int, str]) -> None:
        self.preset_ids = preset_ids
        self.calls: list[list[int]] = []

    def get_fsrs_preset_ids_for_cards(self, card_ids: list[int]) -> SimpleNamespace:
        self.calls.append(card_ids)
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    card_id=card_id,
                    preset_id=self.preset_ids[card_id],
                )
                for card_id in card_ids
                if card_id in self.preset_ids
            ]
        )


def _collection(
    *,
    cards: list[tuple[int, int, int, int]],
    reviews: list[tuple[int, int, int, int, int, int, int]],
    preset_ids: dict[int, str],
    config: dict[str, object],
    overlay: dict[str, object] | None = None,
    search_results: dict[str, list[int]] | None = None,
) -> SimpleNamespace:
    db = sqlite3.connect(":memory:")
    db.execute("create table decks(id integer primary key, name text not null)")
    db.execute(
        "create table cards(id integer primary key, nid integer, did integer, odid integer)"
    )
    db.execute(
        "create table revlog(id integer primary key, cid integer, ease integer, "
        "time integer, type integer, factor integer, ivl integer)"
    )
    db.executemany(
        "insert into decks values (?, ?)", [(100, "Home"), (900, "Filtered")]
    )
    db.executemany("insert into cards values (?, ?, ?, ?)", cards)
    db.executemany("insert into revlog values (?, ?, ?, ?, ?, ?, ?)", reviews)

    class Decks:
        def config_dict_for_deck_id(self, deck_id: int) -> dict[str, object]:
            assert deck_id == 100
            return config

        def all_config(self) -> list[dict[str, object]]:
            return [config]

    backend = _PresetBackend(preset_ids)
    find_calls: list[str] = []

    def find_cards(search: str, *, order: bool) -> list[int]:
        assert order is False
        find_calls.append(search)
        return (search_results or {}).get(search, [])

    return SimpleNamespace(
        db=db,
        decks=Decks(),
        sched=SimpleNamespace(
            _timing_today=lambda: SimpleNamespace(
                days_elapsed=42,
                next_day_at=43 * 86_400,
            )
        ),
        _backend=backend,
        get_config=lambda key: overlay if key == "fsrsPresetOverlay" else None,
        find_cards=find_cards,
        find_calls=find_calls,
    )


def _expected_preset_hash(preset_id: str) -> int:
    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)
