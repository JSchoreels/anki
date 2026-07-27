# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from aqt import rwkv_srs_benchmark


def test_srs_benchmark_loader_enables_eval_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeProcess] = []

    class FakeRnn:
        def __init__(self) -> None:
            self.training = True

        def eval(self) -> FakeRnn:
            self.training = False
            return self

    class FakeProcess:
        def __init__(self, *, path: Path, device: object, dtype: object) -> None:
            self.path = path
            self.device = device
            self.dtype = dtype
            self.rnn = FakeRnn()
            created.append(self)

    fake_pandas = types.ModuleType("pandas")
    setattr(fake_pandas, "Series", lambda row, dtype: (row, dtype))
    fake_torch = types.ModuleType("torch")
    for name in ("float32", "bfloat16", "float16"):
        setattr(fake_torch, name, object())
    setattr(fake_torch, "device", lambda value: value)
    fake_rwkv = types.ModuleType("rwkv")
    setattr(fake_rwkv, "__path__", [])
    fake_runner = types.ModuleType("rwkv.run_as_rnn")
    setattr(fake_runner, "RNNProcess", FakeProcess)

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "pandas", fake_pandas)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "rwkv", fake_rwkv)
    monkeypatch.setitem(sys.modules, "rwkv.run_as_rnn", fake_runner)
    monkeypatch.setattr(
        rwkv_srs_benchmark,
        "_install_srs_benchmark_import_shims",
        lambda: None,
    )

    process, row_factory = rwkv_srs_benchmark._load_srs_benchmark_process(
        benchmark_path=Path("benchmark"),
        model_path=Path("model.pth"),
        device="cpu",
        dtype="float",
    )

    assert len(created) == 1
    assert process.rnn is created[0].rnn
    assert not process.rnn.training
    assert row_factory({"card_id": 1}) == ({"card_id": 1}, "float64")


def test_srs_benchmark_adapter_supports_anki_max_interval() -> None:
    torch = pytest.importorskip("torch")
    elapsed_inputs: list[float] = []
    elapsed_dtypes = []

    class FakeRnn:
        point_spread = 18.5
        max_e = 21
        num_points = 128

        def forgetting_curve(self, weights: Any, elapsed_seconds: Any) -> Any:
            elapsed_inputs.append(float(elapsed_seconds.item()))
            elapsed_dtypes.append(elapsed_seconds.dtype)
            return torch.tensor([0.8])

    class FakeProcess:
        device = torch.device("cpu")
        dtype = torch.bfloat16
        rnn = FakeRnn()

    process = rwkv_srs_benchmark._SrsBenchmarkProcessAdapter(
        FakeProcess(),
        torch,
    )
    curve = (
        torch.linspace(-0.25, 0.25, FakeRnn.num_points).to(torch.bfloat16).view(1, -1),
        torch.full(
            (1, FakeRnn.num_points),
            1 / FakeRnn.num_points,
            dtype=torch.bfloat16,
        ),
    )
    first_second_after_grid = math.ceil(
        0.5
        + (math.exp(FakeRnn.point_spread) - 1)
        * math.exp(FakeRnn.max_e - FakeRnn.point_spread)
    )
    at_grid_limit = process.predict_func(curve, first_second_after_grid)
    at_anki_max = process.predict_func(curve, 36_500 * 86_400)

    assert torch.isfinite(at_anki_max).all()
    assert 0 < at_anki_max.item() < 1
    assert at_anki_max.item() == pytest.approx(at_grid_limit.item(), abs=1e-7)
    assert elapsed_inputs[-1] == 36_500 * 86_400
    assert elapsed_dtypes[-1] == torch.int64
