# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import math
from dataclasses import dataclass

import torch  # type: ignore[import-not-found]


@dataclass(frozen=True)
class RWKV7Config:
    d_model: int
    n_heads: int
    n_layers: int
    channel_mixer_factor: float
    layer_offset: int
    total_layers: int
    decay_lora: int
    a_lora: int
    v0_mix_amt_lora: int
    gate_lora: int
    dropout: float
    dropout_layer: float


def ortho_init(x: torch.Tensor, scale: float) -> torch.Tensor:
    with torch.no_grad():
        shape = x.shape
        if len(shape) == 2:
            gain = math.sqrt(shape[0] / shape[1]) if shape[0] > shape[1] else 1
            torch.nn.init.orthogonal_(x, gain=gain * scale)
        elif len(shape) == 3:
            gain = math.sqrt(shape[1] / shape[2]) if shape[1] > shape[2] else 1
            for i in range(shape[0]):
                torch.nn.init.orthogonal_(x[i], gain=gain * scale)
        else:
            raise ValueError("unsupported tensor rank for orthogonal init")
        return x


class LoraSimple(torch.nn.Module):
    def __init__(self, name: str, d_model: int, d_lora: int, layer_id: int) -> None:
        super().__init__()
        del layer_id
        with torch.no_grad():
            self.A = torch.nn.Linear(d_model, d_lora, bias=False)
            torch.nn.init.zeros_(self.A.weight)
            self.B_and_lamb = torch.nn.Linear(d_lora, d_model, bias=True)
            ortho_init(self.B_and_lamb.weight, scale=0.1)
            if name == "v":
                torch.nn.init.ones_(self.B_and_lamb.bias)
            else:
                torch.nn.init.zeros_(self.B_and_lamb.bias)

    def forward(self, in_btc: torch.Tensor) -> torch.Tensor:
        return self.B_and_lamb(self.A(in_btc))


class LoraMLP(torch.nn.Module):
    def __init__(
        self,
        name: str,
        config: RWKV7Config,
        d_lora: int,
        out_dim: int,
        layer_id: int,
    ) -> None:
        super().__init__()
        c = out_dim
        ratio_0_to_1 = layer_id / (config.total_layers - 1)

        with torch.no_grad():
            self.A = torch.nn.Linear(config.d_model, d_lora, bias=False)
            torch.nn.init.zeros_(self.A.weight)
            self.B_and_lamb = torch.nn.Linear(d_lora, out_dim, bias=True)
            ortho_init(self.B_and_lamb.weight, scale=0.1)
            if name == "d":
                decay_speed = torch.ones(c)
                for i in range(c):
                    decay_speed[i] = -7 + 5 * (i / (c - 1)) ** (
                        0.85 + 1.0 * ratio_0_to_1**0.5
                    )
                self.B_and_lamb.bias.copy_(decay_speed + 0.5)
            else:
                torch.nn.init.zeros_(self.B_and_lamb.bias)

    def forward(self, in_btc: torch.Tensor) -> torch.Tensor:
        return self.B_and_lamb(torch.nn.functional.tanh(self.A(in_btc)))
