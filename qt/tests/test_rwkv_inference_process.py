# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import importlib
import importlib.util
import math
import struct
from pathlib import Path

import pytest

_RWKV_MODEL_FILENAME = "sm40v2/weights.f32.bin"
# The resident and per-review paths round differently on aarch64 (2.6e-6 seen on macOS CI).
_RWKV_ABS_TOL = 1e-5

_RWKV_REVIEWS = [
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


@pytest.mark.parametrize(
    "model_name,filename",
    [
        ("sm40", "sm40v2/weights.f32.bin"),
        ("legacy", "RWKV_trained_on_5000_10000.bin"),
        ("sm36", "sm36/weights.f32.bin"),
        ("sm55", "sm55/weights.f32.bin"),
        ("sm56", "sm56/weights.f32.bin"),
        ("sm72", "sm72/weights.f32.bin"),
        ("sm72e20", "sm72e20/weights.f32.bin"),
        ("sm72e25", "sm72e25/weights.f32.bin"),
    ],
)
@pytest.mark.parametrize("state_precision", ["f32", "int8", "cards_int4_notes_int8"])
def test_rsbridge_rwkv_checkpoint_is_readable_and_prunable_by_desktop(
    tmp_path, model_name, filename, state_precision
) -> None:
    from aqt import rwkv_scheduler

    root = Path(__file__).resolve().parents[2]
    rsbridge = _import_rsbridge(root)
    model_path = root / "qt/aqt/rwkv_inference" / filename
    runtime = rsbridge.RwkvInference(
        str(model_path), 0.9, 36500, state_precision=state_precision, model=model_name
    )
    reviews = [
        (
            *_rwkv_review_args(
                review,
                is_query=False,
                card_state=None,
                note_state=None,
                deck_state=None,
                preset_state=None,
                global_state=None,
            )[:15],
            True,
        )
        for review in _RWKV_REVIEWS[:3]
    ]
    runtime.warm_up_reviews(reviews, False)
    query = (
        *_rwkv_review_args(
            _RWKV_REVIEWS[3],
            is_query=True,
            card_state=None,
            note_state=None,
            deck_state=None,
            preset_state=None,
            global_state=None,
        )[:15],
        True,
    )
    expected = runtime.predict_retrievability_many_from_warm_up([query])
    path = tmp_path / "state.sqlite3"
    encoded_map = rwkv_scheduler._encode_int_map_binary({})

    def checkpoint(parent, full):
        return runtime.write_warm_up_state_checkpoint(
            str(path),
            "generation",
            parent,
            3000,
            len(reviews),
            "a" * 64,
            "replay",
            encoded_map,
            encoded_map,
            encoded_map,
            full,
            True,
        )

    base = checkpoint(None, True)
    orphan = checkpoint(None, True)
    head = checkpoint(base, False)
    runtime.finish_warm_up_state_checkpoints()

    history = rwkv_scheduler._read_rwkv_state_cache_store_segment_history(
        path, "generation", head
    )
    assert history.review_count == len(reviews)
    assert history.last_review_id == 3000
    assert history.history_hash == "a" * 64
    assert history.replay_key == "replay"
    assert rwkv_scheduler._rwkv_state_cache_store_segment_chain(
        path, "generation", head
    ) == [head, base]
    assert rwkv_scheduler._prune_rwkv_state_cache_store(path, "generation", head) == 1
    with pytest.raises(ValueError, match="missing RWKV state-cache segment"):
        rwkv_scheduler._read_rwkv_state_cache_store_segment_history(
            path, "generation", orphan
        )
    restored = rsbridge.RwkvInference(
        str(model_path), 0.9, 36500, state_precision=state_precision, model=model_name
    )
    restored.restore_warm_up_state_checkpoint(str(path), "generation", head)
    assert restored.predict_retrievability_many_from_warm_up([query]) == expected


@pytest.mark.parametrize(
    "model_name,filename",
    [
        ("sm40", "sm40v2/weights.f32.bin"),
        ("legacy", "RWKV_trained_on_5000_10000.bin"),
        ("sm36", "sm36/weights.f32.bin"),
        ("sm55", "sm55/weights.f32.bin"),
        ("sm56", "sm56/weights.f32.bin"),
        ("sm72", "sm72/weights.f32.bin"),
        ("sm72e20", "sm72e20/weights.f32.bin"),
        ("sm72e25", "sm72e25/weights.f32.bin"),
    ],
)
@pytest.mark.parametrize("state_precision", ["f32", "int8", "cards_int4_notes_int8"])
def test_rsbridge_rwkv_predictions_survive_state_and_workload_roundtrips(
    model_name, filename, state_precision
) -> None:
    root = Path(__file__).resolve().parents[2]
    model_path = root / "qt/aqt/rwkv_inference" / filename
    if not model_path.exists():
        pytest.skip(f"RWKV model is unavailable: {model_path}")

    rsbridge = _import_rsbridge(root)
    runtime = rsbridge.RwkvInference(
        str(model_path), 0.9, 36500, state_precision=state_precision, model=model_name
    )
    card_states: dict[int, bytes] = {}
    note_states: dict[int, bytes] = {}
    deck_states: dict[int, bytes] = {}
    preset_states: dict[int, bytes] = {}
    global_state: bytes | None = None

    for index, review in enumerate(_RWKV_REVIEWS):
        card_state = card_states.get(review["card_id"])
        note_state = note_states.get(review["note_id"])
        deck_state = deck_states.get(review["deck_id"])
        preset_state = preset_states.get(review["preset_id"])

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
            packed_query = struct.pack("<8sI", b"ARWKVWU2", 1) + struct.pack(
                "<IqqqqBBqqqqqffffB",
                0b1_1110_0111,
                *query_input[:5],
                0,
                0,
                query_input[7],
                query_input[8],
                query_input[9],
                query_input[10],
                0.0,
                0.0,
                0.0,
                0.0,
                query_input[15],
            )
            packed_resident_output = (
                runtime.predict_retrievability_many_from_warm_up_packed(packed_query)
            )
            assert struct.unpack("<f", packed_resident_output) == pytest.approx(
                [query_output[0]], abs=_RWKV_ABS_TOL
            )
        curve_retrievability = query_output[1]
        if review["elapsed_seconds"] >= 0 or review["elapsed_days"] >= 0:
            assert curve_retrievability is not None
            assert 0.0 <= curve_retrievability <= 1.0
        else:
            assert curve_retrievability is None
        button_probabilities = query_output[6]
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
        card_states[review["card_id"]] = update_output[7]
        note_states[review["note_id"]] = update_output[8]
        deck_states[review["deck_id"]] = update_output[9]
        preset_states[review["preset_id"]] = update_output[10]
        global_state = update_output[11]

    snapshot = runtime.warm_up_snapshot()
    workload_inputs = [(*query_input, 4, 2500, 5, 1)]
    workload_settings = (
        70,
        90,
        10,
        5,
        10,
        0,
        False,
        36500,
        0,
        None,
        1,
        (8.0, 8.0, 8.0, 8.0),
        [],
        None,
    )
    assert runtime.simulate_workload_from_warm_up(
        workload_inputs, *workload_settings
    ) == runtime.simulate_workload(workload_inputs, snapshot, *workload_settings)
    answer_inputs = []
    for ease in (1, 2, 3, 4):
        answer_input = list(query_input)
        answer_input[4] = False
        answer_input[5] = ease
        answer_input[6] = None
        answer_inputs.append(tuple(answer_input))
    snapshot_future = runtime.predict_retrievability_many_after_reviews(
        answer_inputs,
        [query_input],
        snapshot,
    )
    resident_state_before = runtime.cache_state()
    resident_future = runtime.predict_retrievability_many_after_reviews_from_warm_up(
        answer_inputs,
        [query_input],
    )
    assert len(resident_future) == len(snapshot_future)
    for resident_batch, snapshot_batch in zip(
        resident_future,
        snapshot_future,
        strict=True,
    ):
        assert resident_batch == pytest.approx(snapshot_batch, abs=_RWKV_ABS_TOL)
    assert runtime.cache_state() == resident_state_before

    undo_context = runtime.state_for_review(*query_input[:4])
    undo_modules = runtime.warm_up_state(query_input)
    runtime.warm_up_reviews([answer_inputs[2]], False)
    assert runtime.cache_state() != resident_state_before
    runtime.restore_state(undo_context)
    runtime.restore_warm_up_state(*query_input[:4], *undo_modules)
    assert runtime.warm_up_snapshot() == snapshot

    batch_inputs: list[tuple[object, ...]] = []
    batch_expected: list[float] = []
    for review in _RWKV_REVIEWS[-9:]:
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

    restored = rsbridge.RwkvInference(
        str(model_path), 0.9, 36500, state_precision=state_precision, model=model_name
    )
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


def test_rwkv_inference_process_keeps_creation_elapsed_out_of_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("torch")

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "aqt"))
    process_module = importlib.import_module("rwkv_inference.process")
    initial_learning = {
        "state": 0,
        "elapsed_days": 3,
        "elapsed_seconds": 259_200,
    }
    later_learning = {
        "state": 1,
        "elapsed_days": 3,
        "elapsed_seconds": 259_200,
    }

    assert process_module._state_update_row(initial_learning) == {
        "state": 0,
        "elapsed_days": -1,
        "elapsed_seconds": -1,
    }
    assert process_module._state_update_row(later_learning) == later_learning


def test_rwkv_inference_process_encodes_day_offsets_before_bfloat16_cast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "aqt"))
    process_module = importlib.import_module("rwkv_inference.process")
    process = object.__new__(process_module.RwkvInferenceProcess)
    process.device = torch.device("cpu")
    process.dtype = torch.bfloat16
    features = torch.zeros(1, dtype=torch.bfloat16)
    row = {
        "day_offset": 7_301,
        "day_offset_first": 7_299,
    }

    encoded = process._add_day_offset_encoding(features, row)

    expected_parts = [features]
    for period in process_module.DAY_OFFSET_ENCODE_PERIODS:
        frequency = 2 * math.pi / period
        for day_offset in (7_301, 7_299):
            day = torch.tensor([day_offset], dtype=torch.float32)
            expected_parts.append(
                torch.cat(
                    (
                        torch.sin(frequency * (day % period)),
                        torch.cos(frequency * (day % period)),
                    )
                ).to(torch.bfloat16)
            )
    assert torch.equal(encoded, torch.cat(expected_parts))


def test_rwkv_inference_process_preserves_large_elapsed_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "aqt"))
    process_module = importlib.import_module("rwkv_inference.process")
    elapsed_tensors = []

    class FakeRnn:
        def forgetting_curve(self, weights: object, elapsed_seconds: object) -> object:
            elapsed_tensors.append(elapsed_seconds)
            return torch.tensor([0.8])

        def interp(self, ahead_logits: object, elapsed_seconds: object) -> object:
            return torch.zeros(1)

    process = object.__new__(process_module.RwkvInferenceProcess)
    process.device = torch.device("cpu")
    process.dtype = torch.bfloat16
    process.rnn = FakeRnn()
    elapsed_seconds = 15_000 * 86_400
    curve = (
        torch.zeros((1, 128), dtype=torch.bfloat16),
        torch.zeros((1, 128), dtype=torch.bfloat16),
    )

    prediction = process.predict_func(curve, elapsed_seconds)

    assert torch.isfinite(prediction).all()
    assert elapsed_tensors[0].dtype == torch.int64
    assert elapsed_tensors[0].item() == elapsed_seconds
