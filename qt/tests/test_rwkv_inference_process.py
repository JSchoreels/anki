# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import importlib
import importlib.util
import math
import struct
from pathlib import Path

import pytest

_RWKV_MODEL_FILENAME = "RWKV_trained_on_5000_10000.bin"
_RWKV_CURVE_COUNT = 128
_RWKV_GOLDEN_CARD_ID = 1549775725979
_RWKV_GOLDEN_ROW0_IMMEDIATE = 0.89073008298873901
_RWKV_GOLDEN_ROW34_IMMEDIATE = 0.70197033882141113
_RWKV_GOLDEN_ROW34_AHEAD = 0.63239266440118969
_RWKV_ABS_TOL = 1e-6

_RWKV_GOLDEN_REVIEWS = [
    {
        "card_id": 1549775725972,
        "note_id": 1549775724800,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 26482,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725972,
        "note_id": 1549775724800,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 35675,
        "elapsed_days": 0,
        "elapsed_seconds": 137,
    },
    {
        "card_id": 1549775725973,
        "note_id": 1549775724801,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 25048,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725972,
        "note_id": 1549775724800,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 10872,
        "elapsed_days": 0,
        "elapsed_seconds": 80,
    },
    {
        "card_id": 1549775725974,
        "note_id": 1549775724802,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 22485,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725973,
        "note_id": 1549775724801,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 32352,
        "elapsed_days": 0,
        "elapsed_seconds": 110,
    },
    {
        "card_id": 1549775725975,
        "note_id": 1549775724803,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 21382,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725974,
        "note_id": 1549775724802,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 11606,
        "elapsed_days": 0,
        "elapsed_seconds": 88,
    },
    {
        "card_id": 1549775725973,
        "note_id": 1549775724801,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 26787,
        "elapsed_days": 0,
        "elapsed_seconds": 103,
    },
    {
        "card_id": 1549775725975,
        "note_id": 1549775724803,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 57171,
        "elapsed_days": 0,
        "elapsed_seconds": 149,
    },
    {
        "card_id": 1549775725974,
        "note_id": 1549775724802,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 5987,
        "elapsed_days": 0,
        "elapsed_seconds": 143,
    },
    {
        "card_id": 1549775725973,
        "note_id": 1549775724801,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 5574,
        "elapsed_days": 0,
        "elapsed_seconds": 201,
    },
    {
        "card_id": 1549775725975,
        "note_id": 1549775724803,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 21440,
        "elapsed_days": 0,
        "elapsed_seconds": 162,
    },
    {
        "card_id": 1549775725976,
        "note_id": 1549775724804,
        "deck_id": 1707669702331,
        "preset_id": 3366177995109454929,
        "day_offset": 0,
        "rating": 3,
        "state": 0,
        "duration": 24599,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725977,
        "note_id": 1549775724805,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 23181,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725978,
        "note_id": 1549775724806,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 19847,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725977,
        "note_id": 1549775724805,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 4618,
        "elapsed_days": 0,
        "elapsed_seconds": 77,
    },
    {
        "card_id": 1549775725978,
        "note_id": 1549775724806,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 4532,
        "elapsed_days": 0,
        "elapsed_seconds": 72,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 0,
        "duration": 19777,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    },
    {
        "card_id": 1549775725972,
        "note_id": 1549775724800,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 15541,
        "elapsed_days": 0,
        "elapsed_seconds": 769,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 11288,
        "elapsed_days": 0,
        "elapsed_seconds": 116,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 20926,
        "elapsed_days": 0,
        "elapsed_seconds": 162,
    },
    {
        "card_id": 1549775725974,
        "note_id": 1549775724802,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 8272,
        "elapsed_days": 0,
        "elapsed_seconds": 761,
    },
    {
        "card_id": 1549775725975,
        "note_id": 1549775724803,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 38156,
        "elapsed_days": 0,
        "elapsed_seconds": 662,
    },
    {
        "card_id": 1549775725973,
        "note_id": 1549775724801,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 12531,
        "elapsed_days": 0,
        "elapsed_seconds": 744,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 7253,
        "elapsed_days": 0,
        "elapsed_seconds": 104,
    },
    {
        "card_id": 1549775725976,
        "note_id": 1549775724804,
        "deck_id": 1707669702331,
        "preset_id": 3366177995109454929,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 27585,
        "elapsed_days": 0,
        "elapsed_seconds": 688,
    },
    {
        "card_id": 1549775725976,
        "note_id": 1549775724804,
        "deck_id": 1707669702331,
        "preset_id": 3366177995109454929,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 10236,
        "elapsed_days": 0,
        "elapsed_seconds": 44,
    },
    {
        "card_id": 1549775725977,
        "note_id": 1549775724805,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 12253,
        "elapsed_days": 0,
        "elapsed_seconds": 592,
    },
    {
        "card_id": 1549775725978,
        "note_id": 1549775724806,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 1,
        "state": 5,
        "duration": 42842,
        "elapsed_days": 0,
        "elapsed_seconds": 600,
    },
    {
        "card_id": 1549775725978,
        "note_id": 1549775724806,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 5,
        "duration": 4968,
        "elapsed_days": 0,
        "elapsed_seconds": 11,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 6986,
        "elapsed_days": 0,
        "elapsed_seconds": 333,
    },
    {
        "card_id": 1549775725976,
        "note_id": 1549775724804,
        "deck_id": 1707669702331,
        "preset_id": 3366177995109454929,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 8396,
        "elapsed_days": 0,
        "elapsed_seconds": 253,
    },
    {
        "card_id": 1549775725978,
        "note_id": 1549775724806,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 0,
        "rating": 3,
        "state": 4,
        "duration": 14451,
        "elapsed_days": 0,
        "elapsed_seconds": 199,
    },
    {
        "card_id": 1549775725979,
        "note_id": 1549775724807,
        "deck_id": 1707669702331,
        "preset_id": 8169090998413644153,
        "day_offset": 1,
        "rating": 1,
        "state": 2,
        "duration": 25616,
        "elapsed_days": 1,
        "elapsed_seconds": 76039,
    },
]


class _RwkvCacheCursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def expect_magic(self, magic: bytes) -> None:
        found = self.bytes(len(magic))
        assert found == magic

    def expect_end(self) -> None:
        assert self.offset == len(self.data)

    def bytes(self, length: int) -> bytes:
        end = self.offset + length
        assert end <= len(self.data)
        value = self.data[self.offset : end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.bytes(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.bytes(4))[0]

    def f32_vec(self) -> list[float]:
        return [self.f32() for _ in range(self.u32())]

    def skip_option_i64(self) -> None:
        tag = self.u8()
        assert tag in (0, 1)
        if tag:
            self.i64()

    def skip_i64_set(self) -> None:
        for _ in range(self.u32()):
            self.i64()

    def skip_i64_map(self) -> None:
        for _ in range(self.u32()):
            self.i64()
            self.i64()


def _skip_rwkv_feature_state(cursor: _RwkvCacheCursor) -> None:
    cursor.skip_option_i64()
    cursor.skip_option_i64()
    cursor.skip_i64_set()
    cursor.skip_i64_map()
    cursor.skip_i64_map()
    cursor.i64()
    cursor.i64()
    cursor.i64()
    cursor.skip_i64_map()
    cursor.skip_i64_map()
    cursor.skip_i64_map()
    cursor.i64()

    for _ in range(cursor.u32()):
        cursor.u8()
        cursor.i64()
        cursor.f32_vec()

    cursor.u32()
    for _ in range(624):
        cursor.u32()


def _rwkv_curves_from_cache(cache: bytes) -> dict[int, tuple[list[float], list[float]]]:
    cursor = _RwkvCacheCursor(cache)
    cursor.expect_magic(b"ARWKVPROCSTATE2")
    _skip_rwkv_feature_state(cursor)
    curves = {}
    for _ in range(cursor.u32()):
        card_id = cursor.i64()
        curves[card_id] = (cursor.f32_vec(), cursor.f32_vec())
    cursor.expect_end()
    return curves


def _rwkv_review_args(
    review: dict[str, int],
    *,
    is_query: bool,
    card_state: bytes | None,
    note_state: bytes | None,
    deck_state: bytes | None,
    preset_state: bytes | None,
    global_state: bytes | None,
) -> tuple[object, ...]:
    return (
        review["card_id"],
        review["note_id"],
        review["deck_id"],
        review["preset_id"],
        is_query,
        None if is_query else review["rating"],
        None if is_query else review["duration"],
        review["state"],
        review["day_offset"],
        review["elapsed_days"],
        review["elapsed_seconds"],
        None,
        None,
        None,
        None,
        card_state,
        note_state,
        deck_state,
        preset_state,
        global_state,
        True,
    )


def _rwkv_linspace_exp(index: int, count: int, point_spread: float) -> float:
    value = 0.0 if count <= 1 else point_spread * index / (count - 1)
    return math.exp(value)


def _rwkv_interp_ahead_logits(
    ahead_logits: list[float], elapsed_seconds: float
) -> float:
    point_count = len(ahead_logits)
    if point_count < 2:
        return ahead_logits[0] if ahead_logits else 0.0

    def point(index: int) -> float:
        raw = _rwkv_linspace_exp(index, point_count, 18.5)
        return 0.5 + (raw - 1.0) * math.exp(21.0 - 18.5)

    right = 0
    while right + 1 < point_count and point(right) < elapsed_seconds:
        right += 1
    right = max(1, min(right, point_count - 1))
    left = right - 1
    xl = point(left)
    xr = point(right)
    yl = ahead_logits[left]
    yr = ahead_logits[right]
    return 1e-5 + (1.0 - 2e-5) * (yl + (yr - yl) * (elapsed_seconds - xl) / (xr - xl))


def _rwkv_sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _rwkv_predict_curve(
    curve: tuple[list[float], list[float]], elapsed_seconds: float
) -> float:
    ahead_logits, weights = curve
    elapsed_seconds = max(elapsed_seconds, 1.0)
    raw_probability = 0.0
    for index, weight in enumerate(weights):
        s_space_raw = _rwkv_linspace_exp(index, _RWKV_CURVE_COUNT, 18.5)
        s_space = 0.1 + (s_space_raw - 1.0) * math.exp(22.0 - 18.5)
        raw_probability += weight * 0.9 ** (elapsed_seconds / s_space)

    curve_probability = 1e-5 + (1.0 - 2e-5) * raw_probability
    curve_logits = math.log(curve_probability / (1.0 - curve_probability))
    return _rwkv_sigmoid(
        curve_logits + _rwkv_interp_ahead_logits(ahead_logits, elapsed_seconds)
    )


def _import_rsbridge(root: Path) -> object:
    extension_path = root / "out/pylib/anki/_rsbridge.so"
    if not extension_path.exists():
        pytest.skip("built _rsbridge extension is unavailable; run `just build` first")

    spec = importlib.util.spec_from_file_location("_rsbridge", extension_path)
    if spec is None or spec.loader is None:
        pytest.skip(f"unable to load _rsbridge from {extension_path}")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"unable to import built _rsbridge: {exc}")
    return module


def test_rsbridge_rwkv_golden_predictions_cover_rwkv_and_rwkv_p() -> None:
    root = Path(__file__).resolve().parents[2]
    model_path = root / "qt/aqt/rwkv_inference" / _RWKV_MODEL_FILENAME
    if not model_path.exists():
        pytest.skip(f"RWKV model is unavailable: {model_path}")

    rsbridge = _import_rsbridge(root)
    runtime = rsbridge.RwkvInference(str(model_path), 0.9, 36500)
    card_states: dict[int, bytes] = {}
    note_states: dict[int, bytes] = {}
    deck_states: dict[int, bytes] = {}
    preset_states: dict[int, bytes] = {}
    global_state: bytes | None = None
    curves: dict[int, tuple[list[float], list[float]]] = {}

    for index, review in enumerate(_RWKV_GOLDEN_REVIEWS):
        card_state = card_states.get(review["card_id"])
        note_state = note_states.get(review["note_id"])
        deck_state = deck_states.get(review["deck_id"])
        preset_state = preset_states.get(review["preset_id"])

        if index == 34:
            assert _RWKV_GOLDEN_CARD_ID in curves
            ahead = _rwkv_predict_curve(
                curves[_RWKV_GOLDEN_CARD_ID], review["elapsed_seconds"]
            )
            assert math.isclose(ahead, _RWKV_GOLDEN_ROW34_AHEAD, abs_tol=_RWKV_ABS_TOL)

        query_output = runtime.review(
            *_rwkv_review_args(
                review,
                is_query=True,
                card_state=card_state,
                note_state=note_state,
                deck_state=deck_state,
                preset_state=preset_state,
                global_state=global_state,
            )
        )
        query_input = (
            *_rwkv_review_args(
                review,
                is_query=True,
                card_state=None,
                note_state=None,
                deck_state=None,
                preset_state=None,
                global_state=None,
            )[:15],
            True,
        )
        resident_output = runtime.predict_retrievability_many_from_warm_up(
            [query_input]
        )
        assert resident_output == pytest.approx([query_output[0]], abs=_RWKV_ABS_TOL)
        if index == 0:
            assert math.isclose(
                query_output[0], _RWKV_GOLDEN_ROW0_IMMEDIATE, abs_tol=_RWKV_ABS_TOL
            )
        elif index == 34:
            assert math.isclose(
                query_output[0], _RWKV_GOLDEN_ROW34_IMMEDIATE, abs_tol=_RWKV_ABS_TOL
            )
        button_probabilities = query_output[5]
        assert math.isclose(sum(button_probabilities), 1.0, abs_tol=_RWKV_ABS_TOL)
        assert math.isclose(
            button_probabilities[0],
            1.0 - query_output[0],
            abs_tol=_RWKV_ABS_TOL,
        )

        update_output = runtime.review(
            *_rwkv_review_args(
                review,
                is_query=False,
                card_state=card_state,
                note_state=note_state,
                deck_state=deck_state,
                preset_state=preset_state,
                global_state=global_state,
            )
        )
        card_states[review["card_id"]] = update_output[6]
        note_states[review["note_id"]] = update_output[7]
        deck_states[review["deck_id"]] = update_output[8]
        preset_states[review["preset_id"]] = update_output[9]
        global_state = update_output[10]
        curves = _rwkv_curves_from_cache(runtime.cache_state())

    snapshot = runtime.warm_up_snapshot()
    batch_inputs: list[tuple[object, ...]] = []
    batch_expected: list[float] = []
    for review in _RWKV_GOLDEN_REVIEWS[-9:]:
        batch_expected.append(
            runtime.review(
                *_rwkv_review_args(
                    review,
                    is_query=True,
                    card_state=card_states.get(review["card_id"]),
                    note_state=note_states.get(review["note_id"]),
                    deck_state=deck_states.get(review["deck_id"]),
                    preset_state=preset_states.get(review["preset_id"]),
                    global_state=global_state,
                )
            )[0]
        )
        batch_inputs.append(
            (
                *_rwkv_review_args(
                    review,
                    is_query=True,
                    card_state=None,
                    note_state=None,
                    deck_state=None,
                    preset_state=None,
                    global_state=None,
                )[:15],
                True,
            )
        )
    assert runtime.predict_retrievability_many_from_warm_up(
        batch_inputs
    ) == pytest.approx(batch_expected, abs=_RWKV_ABS_TOL)

    restored = rsbridge.RwkvInference(str(model_path), 0.9, 36500)
    restored.restore_warm_up_snapshot(snapshot)
    restored.restore_cache_state(snapshot[5])
    assert restored.predict_retrievability_many_from_warm_up(
        [query_input]
    ) == pytest.approx(
        runtime.predict_retrievability_many_from_warm_up([query_input]),
        abs=_RWKV_ABS_TOL,
    )


def test_rwkv_inference_process_uses_eval_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = pytest.importorskip("torch")

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "aqt"))
    process_module = importlib.import_module("rwkv_inference.process")

    created: list[FakeRnn] = []

    class FakeRnn:
        def __init__(self, config: object) -> None:
            self.config = config
            self.eval_called = False
            created.append(self)

        def to(self, device: object) -> FakeRnn:
            self.device = device
            return self

        def load_state_dict(self, state_dict: object) -> None:
            self.state_dict = state_dict

        def selective_cast(self, dtype: object) -> FakeRnn:
            self.dtype = dtype
            return self

        def eval(self) -> FakeRnn:
            self.eval_called = True
            return self

    monkeypatch.setattr(process_module, "SrsRWKVRnn", FakeRnn)
    monkeypatch.setattr(process_module.torch, "load", lambda *args, **kwargs: {})

    process = process_module.RwkvInferenceProcess(
        model_path=Path("unused.pth"),
        device=torch.device("cpu"),
        dtype=torch.float32,
    )

    assert process.rnn is created[0]
    assert created[0].eval_called
