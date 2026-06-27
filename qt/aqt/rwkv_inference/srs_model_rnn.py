# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math

import torch  # type: ignore[import-not-found]

from .architecture import AnkiRWKVConfig
from .rwkv_rnn_model import RWKV7RNN

_DTYPE_EXCLUDE = [
    "w_linear",
    "s_linear",
    "d_linear",
    "d_softplus",
    "k_linear",
    "p_linear",
    "ahead_linear",
]


class SrsRWKVRnn(torch.nn.Module):
    def __init__(self, anki_rwkv_config: AnkiRWKVConfig) -> None:
        super().__init__()
        self.card_features_dim = 92
        self.d_model = anki_rwkv_config.d_model
        self.features_fc_dim = 4 * anki_rwkv_config.d_model
        self.ahead_head_dim = 4 * self.d_model
        self.p_head_dim = 4 * self.d_model
        self.w_head_dim = 4 * self.d_model
        self.num_curves = 128

        self.features2card = torch.nn.Sequential(
            torch.nn.Linear(self.card_features_dim, self.features_fc_dim),
            torch.nn.SiLU(),
            torch.nn.LayerNorm(self.features_fc_dim),
            torch.nn.Linear(self.features_fc_dim, self.d_model),
            torch.nn.SiLU(),
        )
        self.rwkv_modules = torch.nn.ModuleList(
            [RWKV7RNN(config=config) for _, config in anki_rwkv_config.modules]
        )
        self.prehead_norm = torch.nn.LayerNorm(self.d_model)
        self.prehead_dropout = torch.nn.Dropout(p=anki_rwkv_config.dropout)
        self.head_ahead_logits = torch.nn.Sequential(
            torch.nn.Linear(self.d_model, self.ahead_head_dim),
            torch.nn.ReLU(),
        )
        self.head_w = torch.nn.Sequential(
            torch.nn.Linear(self.d_model, 1 * self.d_model),
            torch.nn.ReLU(),
            torch.nn.LayerNorm(1 * self.d_model),
            torch.nn.Dropout(p=0.1),
            torch.nn.Linear(1 * self.d_model, self.w_head_dim),
        )
        self.head_p = torch.nn.Sequential(
            torch.nn.Linear(self.d_model, self.p_head_dim),
            torch.nn.ReLU(),
        )

        self.max_e = 21
        self.point_spread = 18.5
        self.num_points = 128
        self.ahead_linear = torch.nn.Linear(self.ahead_head_dim, self.num_points)

        self.w_linear = torch.nn.Linear(self.w_head_dim, self.num_curves)

        self.s_point_spread = 18.5
        self.s_max = 22

        self.p_linear = torch.nn.Linear(self.p_head_dim, 4)

    def forgetting_curve(
        self, w: torch.Tensor, label_elapsed_seconds: torch.Tensor
    ) -> torch.Tensor:
        s_space_raw = torch.exp(
            torch.linspace(0, self.s_point_spread, self.num_curves, device=w.device)
        )
        s_space = 0.1 + (s_space_raw - 1) * (
            math.e ** (self.s_max - self.s_point_spread)
        )
        minimum_elapsed = torch.tensor(
            1.0,
            dtype=label_elapsed_seconds.dtype,
            device=label_elapsed_seconds.device,
        )
        label_elapsed_seconds = torch.maximum(minimum_elapsed, label_elapsed_seconds)
        return 1e-5 + (1 - 2 * 1e-5) * torch.sum(
            w * 0.9 ** (label_elapsed_seconds / s_space), dim=-1
        )

    def interp(
        self, out_ahead_logits: torch.Tensor, label_elapsed_seconds: torch.Tensor
    ) -> torch.Tensor:
        label_elapsed_seconds = torch.clamp(label_elapsed_seconds.contiguous(), min=1)
        point_space_raw = torch.exp(
            torch.linspace(
                0, self.point_spread, self.num_points, device=out_ahead_logits.device
            )
        )
        point_space = 0.5 + (point_space_raw - 1) * (
            math.e ** (self.max_e - self.point_spread)
        )
        right_idx = torch.searchsorted(point_space, label_elapsed_seconds)
        left_idx = torch.clamp(right_idx - 1, min=0)
        xl, xr = point_space[left_idx], point_space[right_idx]
        yl = torch.gather(out_ahead_logits, dim=-1, index=left_idx)
        yr = torch.gather(out_ahead_logits, dim=-1, index=right_idx)
        res = 1e-5 + (1 - 2 * 1e-5) * (
            yl + (yr - yl) * (label_elapsed_seconds - xl) / (xr - xl)
        )
        return res.squeeze(-1)

    def review(
        self,
        card_features: torch.Tensor,
        card_state: object | None,
        note_state: object | None,
        deck_state: object | None,
        preset_state: object | None,
        global_state: object | None,
    ) -> tuple[
        torch.Tensor, torch.Tensor, torch.Tensor, object, object, object, object, object
    ]:
        card_rwkv_input = self.features2card(card_features)
        card_encoding, next_card_state = self.rwkv_modules[0].run(
            card_rwkv_input, card_state
        )
        deck_encoding, next_deck_state = self.rwkv_modules[1].run(
            card_encoding, deck_state
        )
        note_encoding, next_note_state = self.rwkv_modules[2].run(
            deck_encoding, note_state
        )
        preset_encoding, next_preset_state = self.rwkv_modules[3].run(
            note_encoding, preset_state
        )
        global_encoding, next_global_state = self.rwkv_modules[4].run(
            preset_encoding, global_state
        )

        x = self.prehead_dropout(self.prehead_norm(global_encoding))
        out_w_logits = self.w_linear(self.head_w(x).float())
        out_w = torch.nn.functional.softmax(out_w_logits, dim=-1)
        out_ahead_logits = self.ahead_linear(self.head_ahead_logits(x).float())

        x_p = self.head_p(x).float()
        out_p_logits = self.p_linear(x_p)
        return (
            out_ahead_logits,
            out_w,
            out_p_logits,
            next_card_state,
            next_note_state,
            next_deck_state,
            next_preset_state,
            next_global_state,
        )

    def selective_cast(self, dtype: torch.dtype) -> "SrsRWKVRnn":
        for name, module in self.named_modules():
            if not name or _is_excluded(name):
                continue
            if dtype == torch.bfloat16:
                module.to(dtype)
            elif dtype == torch.half:
                raise ValueError("float16 is not tested for RWKV inference")
            elif dtype == torch.float32:
                pass
        return self


def _is_excluded(name: str) -> bool:
    return any(query in name for query in _DTYPE_EXCLUDE)
