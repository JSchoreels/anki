# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

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


def test_historical_card_type_matches_existing_benchmark_mapping() -> None:
    assert probe._historical_card_type(0) == 1
    assert probe._historical_card_type(1) == 2
    assert probe._historical_card_type(2) == 3
    assert probe._historical_card_type(3) == 2
    assert probe._historical_card_type(4) == 2


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
