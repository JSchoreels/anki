# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki import scheduler_pb2
from aqt import rwkv_srs_benchmark
from aqt.rwkv_scheduler import RwkvReviewIdentity, RwkvReviewInput


def test_review_performance_benchmark_starts_in_fresh_interpreter() -> None:
    script = Path(__file__).parents[1] / "tools" / "rwkv_review_performance_bench.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--collection-copy" in result.stdout


@pytest.mark.parametrize("learner", [0, 1])
def test_comparison_bridge_runs_both_schedulers_with_a_shared_learner(
    learner: int,
) -> None:
    model = (
        Path(__file__).parents[1]
        / "aqt"
        / "rwkv_inference"
        / "sm56"
        / "weights.f32.bin"
    )
    runtime = rwkv_srs_benchmark._RustRwkvRuntime(
        model_path=model,
        target_retention=0.9,
        max_interval_days=36500,
        state_precision="cards_int4_notes_int8",
        model="sm56",
    )
    answer = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=100, note_id=101, deck_id=1, preset_id=1),
        is_query=False,
        ease=3,
        duration_millis=8000,
        card_type=2,
        card_queue=2,
        card_due=100,
        interval_days=5,
        ease_factor=2500,
        reps=2,
        lapses=0,
        day_offset=100,
        current_state_kind="normal",
        current_normal_state_kind="review",
        current_elapsed_days=5,
        current_elapsed_seconds=432000,
    )
    runtime.warm_up_reviews([answer], return_snapshot=False)
    query = replace(
        answer,
        is_query=True,
        ease=None,
        duration_millis=None,
        fsrs_memory_state=(10.0, 5.0, 10.0),
        fsrs_historical_retention=0.9,
    )
    request = scheduler_pb2.SimulateFsrsReviewRequest(
        days_to_simulate=3,
        review_limit=10,
        max_interval=36500,
        rwkv_workload_target_step=70,
        rwkv_comparison=scheduler_pb2.RwkvComparisonOptions(
            learner=learner,
            scenario=1,
            repetitions=1,
        ),
    )

    fsrs, rwkv, estimated = runtime.compare_workloads(
        inputs=[(100, query, 64)],
        request=request,
        input_scale=1.0,
        review_model=SimpleNamespace(
            grade_seconds=(12.0, 9.0, 7.0, 5.0), bucket_probabilities={}
        ),
    )

    assert fsrs[0] == pytest.approx(rwkv[0])
    assert [point[0] for point in fsrs[2]] == [30, 99]
    assert [point[0] for point in rwkv[2]] == [30, 99]
    assert all(point[4] > 0 for point in fsrs[2] + rwkv[2])
    assert estimated == 0


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
