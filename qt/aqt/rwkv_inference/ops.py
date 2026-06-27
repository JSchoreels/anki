# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from torch import Tensor  # type: ignore[import-not-found]


def single_timestep(
    r_bhk: Tensor,
    k_bhk: Tensor,
    v_bhk: Tensor,
    w_bhk: Tensor,
    a_bhk: Tensor,
    k_deformed_bhk: Tensor,
    state_bhkk: Tensor,
) -> tuple[Tensor, Tensor]:
    r_bhk1 = r_bhk.unsqueeze(-1)
    k_bhk1 = k_bhk.unsqueeze(-1)
    v_bhk1 = v_bhk.unsqueeze(-1)
    w_bhk1 = w_bhk.unsqueeze(-1)
    a_bhk1 = a_bhk.unsqueeze(-1)
    k_deformed_bhk1 = k_deformed_bhk.unsqueeze(-1)

    state_bhkk = (
        state_bhkk * w_bhk1.mT
        - state_bhkk @ k_deformed_bhk1 @ (a_bhk1 * k_deformed_bhk1).mT
    )
    state_bhkk = state_bhkk + (v_bhk1 @ k_bhk1.mT)

    out_bhk1 = state_bhkk @ r_bhk1
    return out_bhk1.squeeze(-1), state_bhkk
