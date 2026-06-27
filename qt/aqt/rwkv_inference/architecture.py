# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from dataclasses import dataclass

from .rwkv_model import RWKV7Config

_N_HEADS = 4
_DROPOUT = 0.02
_DROPOUT_LONG = 0.05
_DROPOUT_LAYER = 0.01


@dataclass(frozen=True)
class AnkiRWKVConfig:
    d_model: int
    modules: list[tuple[str, RWKV7Config]]
    dropout: float


DEFAULT_ANKI_RWKV_CONFIG = AnkiRWKVConfig(
    d_model=32 * _N_HEADS,
    modules=[
        (
            "card_id",
            RWKV7Config(
                d_model=32 * _N_HEADS,
                n_heads=_N_HEADS,
                n_layers=3,
                layer_offset=0,
                total_layers=3,
                channel_mixer_factor=1.5,
                decay_lora=16,
                a_lora=16,
                v0_mix_amt_lora=8,
                gate_lora=16,
                dropout=_DROPOUT,
                dropout_layer=_DROPOUT_LAYER,
            ),
        ),
        (
            "deck_id",
            RWKV7Config(
                d_model=32 * _N_HEADS,
                n_heads=_N_HEADS,
                n_layers=4,
                layer_offset=0,
                total_layers=4,
                channel_mixer_factor=2.0,
                decay_lora=16,
                a_lora=16,
                v0_mix_amt_lora=8,
                gate_lora=16,
                dropout=_DROPOUT_LONG,
                dropout_layer=_DROPOUT_LAYER,
            ),
        ),
        (
            "note_id",
            RWKV7Config(
                d_model=32 * _N_HEADS,
                n_heads=_N_HEADS,
                n_layers=2,
                layer_offset=0,
                total_layers=2,
                channel_mixer_factor=1.5,
                decay_lora=16,
                a_lora=16,
                v0_mix_amt_lora=8,
                gate_lora=16,
                dropout=_DROPOUT,
                dropout_layer=_DROPOUT_LAYER,
            ),
        ),
        (
            "preset_id",
            RWKV7Config(
                d_model=32 * _N_HEADS,
                n_heads=_N_HEADS,
                n_layers=3,
                layer_offset=0,
                total_layers=3,
                channel_mixer_factor=2.0,
                decay_lora=16,
                a_lora=16,
                v0_mix_amt_lora=8,
                gate_lora=16,
                dropout=_DROPOUT_LONG,
                dropout_layer=_DROPOUT_LAYER,
            ),
        ),
        (
            "user_id",
            RWKV7Config(
                d_model=32 * _N_HEADS,
                n_heads=_N_HEADS,
                n_layers=4,
                layer_offset=0,
                total_layers=4,
                channel_mixer_factor=2.0,
                decay_lora=16,
                a_lora=16,
                v0_mix_amt_lora=8,
                gate_lora=16,
                dropout=_DROPOUT_LONG,
                dropout_layer=_DROPOUT_LAYER,
            ),
        ),
    ],
    dropout=_DROPOUT,
)
