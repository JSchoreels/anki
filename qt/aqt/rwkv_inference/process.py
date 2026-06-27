# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import torch  # type: ignore[import-not-found]

from .architecture import DEFAULT_ANKI_RWKV_CONFIG
from .config import DAY_OFFSET_ENCODE_PERIODS, ID_ENCODE_DIMS, ID_SPLIT, RWKV_SUBMODULES
from .features import (
    CARD_FEATURE_COLUMNS,
    ID_PLACEHOLDER,
    is_missing_id,
    scale_cum_new_cards_today,
    scale_cum_reviews_today,
    scale_day_offset_diff,
    scale_diff_new_cards,
    scale_diff_reviews,
    scale_duration,
    scale_elapsed_days,
    scale_elapsed_days_cumulative,
    scale_elapsed_seconds,
    scale_elapsed_seconds_cumulative,
    scale_state,
)
from .srs_model_rnn import SrsRWKVRnn


class RwkvInferenceProcess:
    """Inference-only RWKV process compatible with the srs-benchmark runner."""

    def __init__(
        self,
        *,
        model_path: Path,
        device: torch.device,
        dtype: torch.dtype,
    ) -> None:
        # Match the upstream runner's deterministic initialization before weights
        # are loaded, as later ID encodings draw from the same torch RNG stream.
        torch.manual_seed(2025)
        self.rnn = SrsRWKVRnn(DEFAULT_ANKI_RWKV_CONFIG).to(device)
        state_dict = torch.load(model_path, map_location=device, weights_only=True)
        self.rnn.load_state_dict(state_dict)
        self.rnn = self.rnn.selective_cast(dtype)
        self.device = device
        self.dtype = dtype

        self.card_states: dict[int, object | None] = {}
        self.note_states: dict[int, object | None] = {}
        self.deck_states: dict[int, object | None] = {}
        self.preset_states: dict[int, object | None] = {}
        self.global_state: object | None = None
        self.first_day_offset: int | None = None
        self.prev_row: dict[str, Any] | None = None
        self.card_set: set[int] = set()
        self.last_new_cards: dict[int, int] = {}
        self.i = 0
        self.last_i: dict[int, int] = {}
        self.today = -1
        self.today_reviews = 0
        self.today_new_cards = 0
        self.card2first_day_offset: dict[int, int] = {}
        self.card2elapsed_days_cumulative: dict[int, int] = {}
        self.card2elapsed_seconds_cumulative: dict[int, int] = {}
        self.id_encodings: dict[str, dict[object, torch.Tensor]] = {
            submodule: {} for submodule in RWKV_SUBMODULES
        }

    def imm_predict(self, row: Mapping[str, object]) -> torch.Tensor:
        prepared = self._query_row(row)
        return self._run_query(prepared)

    def imm_predict_many(
        self, rows: Sequence[Mapping[str, object]]
    ) -> Sequence[torch.Tensor]:
        prepared_rows = [self._query_row(row) for row in rows]
        if len(prepared_rows) < 2:
            return [self._run_query(row) for row in prepared_rows]

        try:
            return list(self._run_many_queries(prepared_rows).unbind(dim=0))
        except ValueError:
            return [self._run_query(row) for row in prepared_rows]

    def _query_row(self, row: Mapping[str, object]) -> dict[str, Any]:
        prepared = self._add_same(dict(row))
        prepared["is_query"] = 1.0
        prepared["skip"] = True
        prepared["scaled_duration"] = 0
        prepared["scaled_state"] = 0
        for i in range(1, 5):
            prepared[f"rating_{i}"] = 0

        return prepared

    def _run_query(self, prepared: Mapping[str, object]) -> torch.Tensor:
        _, imm_probs = self._run(prepared, skip=True)
        return imm_probs

    def process_row(
        self, row: Mapping[str, object]
    ) -> tuple[torch.Tensor, torch.Tensor]:
        prepared = self._add_same(dict(row))
        prepared["is_query"] = 0.0
        prepared["skip"] = False
        prepared["scaled_duration"] = scale_duration(_float_value(prepared, "duration"))
        prepared["scaled_state"] = scale_state(_float_value(prepared, "state"))
        rating = _int_value(prepared, "rating")
        for i in range(1, 5):
            prepared[f"rating_{i}"] = 1.0 if rating == i else 0.0

        curve, _ = self._run(prepared, skip=False)

        card_id = _int_value(prepared, "card_id")
        self.card2elapsed_days_cumulative[card_id] = (
            self.card2elapsed_days_cumulative.get(card_id, 0)
            + _int_value(prepared, "elapsed_days")
        )
        self.card2elapsed_seconds_cumulative[card_id] = (
            self.card2elapsed_seconds_cumulative.get(card_id, 0)
            + _int_value(prepared, "elapsed_seconds")
        )

        if self.first_day_offset is None:
            self.first_day_offset = _int_value(prepared, "day_offset")

        if prepared["day_offset"] != self.today:
            self.today = _int_value(prepared, "day_offset")
            self.today_new_cards = 0
            self.today_reviews = -1
        self.today_reviews += 1
        if card_id not in self.card_set:
            self.today_new_cards += 1
            self.card_set.add(card_id)
            self.card2first_day_offset[card_id] = (
                _int_value(prepared, "day_offset") - self.first_day_offset
            )

        self.prev_row = dict(prepared)
        self.last_i[card_id] = self.i
        self.last_new_cards[card_id] = len(self.card_set)
        self.i += 1
        return curve

    def predict_func(
        self, curve: tuple[torch.Tensor, torch.Tensor], elapsed_seconds: int
    ) -> torch.Tensor:
        elapsed_seconds_tensor = torch.tensor(
            elapsed_seconds, device=self.device, dtype=self.dtype
        ).view(1, 1)
        out_ahead_logits, out_w = curve
        curve_probs_raw = self.rnn.forgetting_curve(out_w, elapsed_seconds_tensor)
        curve_logits_raw = torch.log(curve_probs_raw / (1 - curve_probs_raw))
        ahead_logit_residual = self.rnn.interp(out_ahead_logits, elapsed_seconds_tensor)
        curve_logits = curve_logits_raw + ahead_logit_residual
        return torch.sigmoid(curve_logits)

    def _get_tensor(self, row: Mapping[str, object]) -> torch.Tensor:
        features = torch.tensor(
            [row[column] for column in CARD_FEATURE_COLUMNS],
            dtype=self.dtype,
            device=self.device,
            requires_grad=False,
        )
        features = self._add_id_encoding(features, row)
        features = self._add_day_offset_encoding(features, row)
        return features.unsqueeze(0)

    def _add_id_encoding(
        self, features: torch.Tensor, row: Mapping[str, object]
    ) -> torch.Tensor:
        gather = [features]
        for submodule in RWKV_SUBMODULES:
            if submodule == "user_id":
                continue
            value = row[submodule]
            if value not in self.id_encodings[submodule]:
                self.id_encodings[submodule][value] = self._generate_id_encoding(
                    submodule
                )

            gather.append(self.id_encodings[submodule][value])

        return torch.cat(gather, dim=-1)

    def _generate_id_encoding(self, submodule: str) -> torch.Tensor:
        encode_dim = ID_ENCODE_DIMS[submodule]
        return torch.randint(
            low=0,
            high=ID_SPLIT,
            size=(encode_dim,),
            device=self.device,
            requires_grad=False,
        ).to(self.dtype) - ((ID_SPLIT - 1) / 2)

    def _add_day_offset_encoding(
        self, features: torch.Tensor, row: Mapping[str, object]
    ) -> torch.Tensor:
        day_offset = torch.full(
            (1,),
            _float_value(row, "day_offset"),
            dtype=self.dtype,
            device=self.device,
        )
        day_offset_first = torch.full(
            (1,),
            _float_value(row, "day_offset_first"),
            dtype=self.dtype,
            device=self.device,
        )
        gather = [features]
        for period in DAY_OFFSET_ENCODE_PERIODS:
            f = 2 * math.pi / period
            encodings = torch.cat(
                (
                    torch.sin(f * (day_offset % period)),
                    torch.cos(f * (day_offset % period)),
                ),
                dim=-1,
            )
            gather.append(encodings)
            encodings_first = torch.cat(
                (
                    torch.sin(f * (day_offset_first % period)),
                    torch.cos(f * (day_offset_first % period)),
                ),
                dim=-1,
            )
            gather.append(encodings_first)

        return torch.cat(gather, dim=-1)

    def _run(
        self,
        row: Mapping[str, object],
        *,
        skip: bool,
    ) -> tuple[tuple[torch.Tensor, torch.Tensor], torch.Tensor]:
        features = self._get_tensor(row)

        with torch.inference_mode():
            card_id = _int_value(row, "card_id")
            note_id = _int_value(row, "note_id")
            deck_id = _int_value(row, "deck_id")
            preset_id = _int_value(row, "preset_id")

            self.card_states.setdefault(card_id, None)
            self.note_states.setdefault(note_id, None)
            self.deck_states.setdefault(deck_id, None)
            self.preset_states.setdefault(preset_id, None)

            (
                out_ahead_logits,
                out_w,
                out_p_logits,
                next_card_state,
                next_note_state,
                next_deck_state,
                next_preset_state,
                next_global_state,
            ) = self.rnn.review(
                features,
                self.card_states[card_id],
                self.note_states[note_id],
                self.deck_states[deck_id],
                self.preset_states[preset_id],
                self.global_state,
            )
            if not skip:
                self.card_states[card_id] = next_card_state
                self.note_states[note_id] = next_note_state
                self.deck_states[deck_id] = next_deck_state
                self.preset_states[preset_id] = next_preset_state
                self.global_state = next_global_state

            out_p_probs = torch.softmax(out_p_logits, dim=-1)
            out_p_again, _, _, _ = out_p_probs.unbind(dim=-1)
            return (out_ahead_logits, out_w), 1.0 - out_p_again

    def _run_many_queries(
        self,
        rows: Sequence[Mapping[str, object]],
    ) -> torch.Tensor:
        features = torch.cat([self._get_tensor(row) for row in rows], dim=0)

        with torch.inference_mode():
            card_ids = [_int_value(row, "card_id") for row in rows]
            note_ids = [_int_value(row, "note_id") for row in rows]
            deck_ids = [_int_value(row, "deck_id") for row in rows]
            preset_ids = [_int_value(row, "preset_id") for row in rows]

            for card_id in card_ids:
                self.card_states.setdefault(card_id, None)
            for note_id in note_ids:
                self.note_states.setdefault(note_id, None)
            for deck_id in deck_ids:
                self.deck_states.setdefault(deck_id, None)
            for preset_id in preset_ids:
                self.preset_states.setdefault(preset_id, None)

            (
                _out_ahead_logits,
                _out_w,
                out_p_logits,
                _next_card_state,
                _next_note_state,
                _next_deck_state,
                _next_preset_state,
                _next_global_state,
            ) = self.rnn.review(
                features,
                _stack_state_batch([self.card_states[card_id] for card_id in card_ids]),
                _stack_state_batch([self.note_states[note_id] for note_id in note_ids]),
                _stack_state_batch([self.deck_states[deck_id] for deck_id in deck_ids]),
                _stack_state_batch(
                    [self.preset_states[preset_id] for preset_id in preset_ids]
                ),
                self.global_state,
            )

            out_p_probs = torch.softmax(out_p_logits, dim=-1)
            out_p_again, _, _, _ = out_p_probs.unbind(dim=-1)
            return 1.0 - out_p_again

    def _add_same(self, row: dict[str, Any]) -> dict[str, Any]:
        card_id = _int_value(row, "card_id")
        elapsed_days = _int_value(row, "elapsed_days")
        elapsed_seconds = _int_value(row, "elapsed_seconds")

        row["elapsed_days_cumulative"] = (
            self.card2elapsed_days_cumulative.get(card_id, 0) + elapsed_days
        )
        row["scaled_elapsed_days_cumulative"] = scale_elapsed_days_cumulative(
            row["elapsed_days_cumulative"]
        )
        row["elapsed_seconds_cumulative"] = (
            self.card2elapsed_seconds_cumulative.get(card_id, 0) + elapsed_seconds
        )
        row["scaled_elapsed_seconds_cumulative"] = scale_elapsed_seconds_cumulative(
            row["elapsed_seconds_cumulative"]
        )
        seconds_per_day = 86_400
        row["elapsed_seconds_sin"] = math.sin(
            (elapsed_seconds % seconds_per_day) * 2 * math.pi / seconds_per_day
        )
        row["elapsed_seconds_cos"] = math.cos(
            (elapsed_seconds % seconds_per_day) * 2 * math.pi / seconds_per_day
        )
        row["elapsed_seconds_cumulative_sin"] = math.sin(
            (row["elapsed_seconds_cumulative"] % seconds_per_day)
            * 2
            * math.pi
            / seconds_per_day
        )
        row["elapsed_seconds_cumulative_cos"] = math.cos(
            (row["elapsed_seconds_cumulative"] % seconds_per_day)
            * 2
            * math.pi
            / seconds_per_day
        )

        if self.first_day_offset is None:
            row["day_offset"] = 0
        else:
            row["day_offset"] = _int_value(row, "day_offset") - self.first_day_offset

        if card_id in self.card2first_day_offset:
            row["day_offset_first"] = self.card2first_day_offset[card_id]
        else:
            row["day_offset_first"] = row["day_offset"]

        row["day_of_week"] = ((row["day_offset"] % 7) - 3) / 3

        for name in ("note_id", "deck_id", "preset_id"):
            if is_missing_id(row.get(name)):
                row[name] = ID_PLACEHOLDER
                row[f"{name}_is_nan"] = 1.0
            else:
                row[f"{name}_is_nan"] = 0.0

        previous_day_offset = (
            0 if self.prev_row is None else _int_value(self.prev_row, "day_offset")
        )
        row["day_offset_diff"] = scale_day_offset_diff(
            _int_value(row, "day_offset") - previous_day_offset
        )
        unscaled_diff_new_cards = (
            (len(self.card_set) - self.last_new_cards[card_id])
            if card_id in self.last_new_cards
            else 0
        )
        row["diff_new_cards"] = scale_diff_new_cards(unscaled_diff_new_cards)
        unscaled_diff_reviews = (
            max(0, self.i - self.last_i[card_id] - 1) if card_id in self.last_i else 0
        )
        row["diff_reviews"] = scale_diff_reviews(unscaled_diff_reviews)

        row_today_reviews = self.today_reviews
        row_today_new_cards = self.today_new_cards
        if row["day_offset"] != self.today:
            row_today_new_cards = 0
            row_today_reviews = -1

        row_today_reviews += 1
        if card_id not in self.card_set:
            row_today_new_cards += 1
        row["cum_reviews_today"] = scale_cum_reviews_today(row_today_reviews)
        row["cum_new_cards_today"] = scale_cum_new_cards_today(row_today_new_cards)

        row["scaled_elapsed_days"] = scale_elapsed_days(elapsed_days)
        row["scaled_elapsed_seconds"] = scale_elapsed_seconds(elapsed_seconds)
        return row


def _int_value(row: Mapping[str, object], key: str) -> int:
    return int(cast(Any, row[key]))


def _float_value(row: Mapping[str, object], key: str) -> float:
    return float(cast(Any, row[key]))


def _stack_state_batch(states: Sequence[object | None]) -> object | None:
    if not states or all(state is None for state in states):
        return None
    if any(state is None for state in states):
        raise ValueError("cannot batch mixed empty and non-empty RWKV states")

    first = states[0]
    if isinstance(first, torch.Tensor):
        tensors = []
        for state in states:
            if not isinstance(state, torch.Tensor):
                raise ValueError("cannot batch heterogeneous RWKV tensor states")
            tensors.append(state)
        return torch.cat(tensors, dim=0)

    if isinstance(first, dict):
        keys = set(first.keys())
        batches = []
        for state in states:
            if not isinstance(state, dict) or set(state.keys()) != keys:
                raise ValueError("cannot batch heterogeneous RWKV dict states")
            batches.append(state)
        return {
            key: _stack_state_batch([state[key] for state in batches])
            for key in sorted(keys)
        }

    if isinstance(first, tuple):
        width = len(first)
        tuples = []
        for state in states:
            if not isinstance(state, tuple) or len(state) != width:
                raise ValueError("cannot batch heterogeneous RWKV tuple states")
            tuples.append(state)
        return tuple(
            _stack_state_batch([state[index] for state in tuples])
            for index in range(width)
        )

    raise ValueError("cannot batch unsupported RWKV state")
