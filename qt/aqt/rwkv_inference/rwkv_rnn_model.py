# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import copy
from typing import Optional, cast

import torch  # type: ignore[import-not-found]

from .ops import single_timestep
from .rwkv_model import LoraMLP, LoraSimple, RWKV7Config


class RWKV7RNN(torch.nn.Module):
    def __init__(self, config: RWKV7Config) -> None:
        super().__init__()
        self.blocks = torch.nn.ModuleList(
            [RWKV7RNNLayer(config, layer_id) for layer_id in range(config.n_layers)]
        )

    def forward(
        self, in_bc: torch.Tensor, state: object | None
    ) -> tuple[torch.Tensor, dict[int, object | None]]:
        if state is None:
            state_dict: dict[int, object | None] = {
                i: None for i in range(len(self.blocks))
            }
        else:
            state_dict = cast(dict[int, object | None], copy.deepcopy(state))

        x_bc, v0_bc = in_bc, torch.empty_like(in_bc)
        for i, block in enumerate(self.blocks):
            x_bc, v0_bc, block_state = block(
                in_bc=x_bc, v0_bc=v0_bc, state=state_dict[i]
            )
            state_dict[i] = block_state
        return x_bc, state_dict

    def run(
        self, in_bc: torch.Tensor, state: object | None
    ) -> tuple[torch.Tensor, dict[int, object | None]]:
        return self.forward(in_bc, state)


class RWKV7RNNLayer(torch.nn.Module):
    def __init__(self, config: RWKV7Config, layer_id: int) -> None:
        super().__init__()
        self.time_mixer = RWKV7RNNTimeMixer(config, layer_id)
        self.channel_mixer = RWKV7RNNChannelMixer(config, layer_id)

    def forward(
        self,
        in_bc: torch.Tensor,
        v0_bc: torch.Tensor,
        state: Optional[
            tuple[Optional[tuple[torch.Tensor, torch.Tensor]], Optional[torch.Tensor]]
        ],
    ) -> tuple[torch.Tensor, torch.Tensor, tuple[object, object]]:
        if state is None:
            state = None, None
        time_state, channel_state = state
        x_bc, v0_bc, time_state = self.time_mixer(
            in_bc=in_bc, v0_bc=v0_bc, state=time_state
        )
        x_bc, channel_state = self.channel_mixer(x_bc, state=channel_state)
        return x_bc, v0_bc, (time_state, channel_state)


class RWKV7RNNChannelMixer(torch.nn.Module):
    def __init__(self, config: RWKV7Config, layer_id: int) -> None:
        super().__init__()
        ratio_1_to_almost_0 = 1.0 - (layer_id / config.n_layers)
        self.layer_norm = torch.nn.LayerNorm(config.d_model)

        channel_ratio = torch.ones(1, 1, config.d_model)
        for i in range(config.d_model):
            channel_ratio[0, 0, i] = i / config.d_model

        self.lerp_k = torch.nn.Parameter(
            1 - torch.pow(channel_ratio, ratio_1_to_almost_0**4)
        )

        k_dim = int(config.channel_mixer_factor * config.d_model)
        self.W_k = torch.nn.Linear(config.d_model, k_dim, bias=False)
        self.W_v = torch.nn.Linear(k_dim, config.d_model, bias=False)

    def forward(
        self, in_bc: torch.Tensor, state: Optional[torch.Tensor]
    ) -> tuple[torch.Tensor, torch.Tensor]:
        x_shift_b1c = state
        in_b1c = in_bc.unsqueeze(1)
        x_b1c = self.layer_norm(in_b1c)
        if x_shift_b1c is None:
            x_shift_b1c = x_b1c

        x_layer_norm_b1c = x_b1c
        k_b1k = self.W_k(torch.lerp(x_b1c, x_shift_b1c, self.lerp_k))
        o_b1c = self.W_v(torch.square(torch.nn.functional.relu(k_b1k)))

        return (in_b1c + o_b1c).squeeze(1), x_layer_norm_b1c


class RWKV7RNNTimeMixer(torch.nn.Module):
    def __init__(self, config: RWKV7Config, layer_id: int) -> None:
        super().__init__()
        self.layer_id = layer_id
        c = config.d_model
        self.H = config.n_heads
        self.K = c // config.n_heads

        self.layer_norm = torch.nn.LayerNorm(config.d_model)
        self.rkvdag_lerp = torch.nn.Parameter(torch.empty(8, 1, 1, config.d_model))
        self.bonus = torch.nn.Parameter(
            torch.zeros(1, 1, config.n_heads, config.d_model // config.n_heads)
        )

        self.W_r = torch.nn.Linear(config.d_model, config.d_model, bias=False)
        self.W_k = torch.nn.Linear(config.d_model, config.d_model, bias=False)
        self.W_v = torch.nn.Linear(config.d_model, config.d_model, bias=False)
        self.W_o = torch.nn.Linear(config.d_model, config.d_model, bias=False)

        self.k_scale_linear = torch.nn.Linear(config.d_model, self.H, bias=True)
        self.v_scale_linear = torch.nn.Linear(config.d_model, self.H, bias=True)
        self.v_lora_simple = LoraSimple(
            name="v",
            d_model=config.d_model,
            d_lora=config.v0_mix_amt_lora,
            layer_id=layer_id,
        )
        self.a_lora_simple = LoraSimple(
            name="a", d_model=config.d_model, d_lora=config.a_lora, layer_id=layer_id
        )
        self.d_lora_mlp = LoraMLP(
            name="d",
            config=config,
            d_lora=config.decay_lora,
            out_dim=config.d_model,
            layer_id=layer_id,
        )

        self.lora_A_g = torch.nn.Linear(config.d_model, config.gate_lora, bias=False)
        self.lora_B_g = torch.nn.Linear(config.gate_lora, config.d_model, bias=False)

        self.out_group_norm = torch.nn.GroupNorm(
            config.n_heads, config.d_model, eps=64e-5
        )

    def forward(
        self,
        in_bc: torch.Tensor,
        v0_bc: torch.Tensor,
        state: Optional[tuple[torch.Tensor, torch.Tensor]],
    ) -> tuple[torch.Tensor, torch.Tensor, tuple[torch.Tensor, torch.Tensor]]:
        b, c = in_bc.shape
        h, k = self.H, self.K

        in_b1c = in_bc.unsqueeze(1)

        x_b1c = self.layer_norm(in_b1c)
        x_layer_norm_b1c = x_b1c
        if state is None:
            x_shift_b1c = x_b1c
            state_b1hkk = torch.zeros(
                b, 1, h, k, k, dtype=torch.float32, device=in_bc.device
            )
        else:
            x_shift_b1c, state_b1hkk = state

        rkvdag_6b1c = torch.lerp(
            x_b1c.unsqueeze(0), x_shift_b1c.unsqueeze(0), self.rkvdag_lerp
        )
        r_b1c, k_b1c, v_b1c, d_b1c, a_b1c, g_b1c, k_scale_b1c, v_scale_b1c = (
            rkvdag_6b1c.unbind(dim=0)
        )
        r_b1c = self.W_r(r_b1c)
        k_b1c = self.W_k(k_b1c)
        k_scale_b1h = torch.nn.functional.sigmoid(self.k_scale_linear(k_scale_b1c))
        v_scale_b1h = torch.nn.functional.sigmoid(self.v_scale_linear(v_scale_b1c))

        if self.layer_id == 0:
            v_b1c = self.W_v(v_b1c)
            v0_bc = v_b1c.squeeze(1)
        else:
            v_lerp_b1c = torch.nn.functional.sigmoid(self.v_lora_simple(v_b1c))
            v_b1c = torch.lerp(self.W_v(v_b1c), v0_bc.unsqueeze(1), v_lerp_b1c)

        a_b1c = torch.nn.functional.sigmoid(self.a_lora_simple(a_b1c))
        g_b1c = self.lora_B_g(torch.nn.functional.sigmoid(self.lora_A_g(g_b1c)))

        d_b1c = -0.5 - torch.nn.functional.softplus(-self.d_lora_mlp(d_b1c))
        w_b1c = torch.exp(-torch.exp(d_b1c.float()))

        k_b1hk = k_scale_b1h.unsqueeze(-1) * torch.nn.functional.normalize(
            k_b1c.view(b, 1, h, k), dim=-1, p=2.0
        )
        r_b1hk = r_b1c.view(b, 1, h, k)
        v_b1hk = v_scale_b1h.unsqueeze(-1) * torch.nn.functional.normalize(
            v_b1c.view(b, 1, h, k), dim=-1, p=2.0
        )
        w_b1hk = w_b1c.view(b, 1, h, k)
        a_b1hk = a_b1c.view(b, 1, h, k)
        k_deformed_b1hk = k_b1hk
        k_b1hk = k_b1hk * a_b1hk

        out_bhk, next_state_bhkk = single_timestep(
            r_b1hk.float().squeeze(1),
            k_b1hk.float().squeeze(1),
            v_b1hk.float().squeeze(1),
            w_b1hk.float().squeeze(1),
            a_b1hk.float().squeeze(1),
            k_deformed_b1hk.float().squeeze(1),
            state_b1hkk.float().squeeze(1),
        )

        out_b1hk = out_bhk.to(in_b1c.dtype).unsqueeze(1)

        out_b1c = self.out_group_norm(out_b1hk.view(b, c)).view(b, 1, c)
        bonus_b1c = (
            (r_b1hk * self.bonus * k_b1hk).sum(dim=-1, keepdim=True) * v_b1hk
        ).view(b, 1, c)
        out_b1c = self.W_o(g_b1c * (out_b1c + bonus_b1c))
        return (
            (in_b1c + out_b1c).squeeze(1),
            v0_bc,
            (x_layer_norm_b1c, next_state_bhkk.unsqueeze(1)),
        )
