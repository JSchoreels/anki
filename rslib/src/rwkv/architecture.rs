// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Dimensions are constant for each specialized engine.
use super::SMALL_MODEL;
pub(super) const D_MODEL: usize = if SMALL_MODEL { 64 } else { 128 };
pub(super) const HEAD_DIM: usize = if SMALL_MODEL { 128 } else { 512 };
pub(super) const NUM_CURVES: usize = if SMALL_MODEL { 3 } else { 128 };
pub(super) const MODULE_LAYERS: [usize; 5] = if SMALL_MODEL {
    [2, 4, 1, 3, 3]
} else {
    [3, 4, 2, 3, 4]
};
pub(super) const CHANNEL_MIXER_DIMS: [usize; 5] = if SMALL_MODEL {
    [64; 5]
} else {
    [192, 256, 192, 256, 256]
};
#[cfg(test)]
pub(super) const MODEL_PATH: &str = if SMALL_MODEL {
    "../qt/aqt/rwkv_inference/sm40v2/weights.f32.bin"
} else {
    "../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin"
};
pub(super) const LAYER_STEPS: &[(usize, usize)] = if SMALL_MODEL {
    &[
        (0, 0),
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
        (0, 1),
        (1, 1),
        (3, 1),
        (4, 1),
        (1, 2),
        (3, 2),
        (4, 2),
        (1, 3),
    ]
} else {
    &[
        (0, 0),
        (0, 1),
        (0, 2),
        (1, 0),
        (1, 1),
        (1, 2),
        (1, 3),
        (2, 0),
        (2, 1),
        (3, 0),
        (3, 1),
        (3, 2),
        (4, 0),
        (4, 1),
        (4, 2),
        (4, 3),
    ]
};
/// The tiny model (the smaller instant-only model, SM101): the small
/// architecture with one card layer instead of two. Only the small engine
/// loads it; every other dimension is the small model's.
pub(super) const TINY_MODULE_LAYERS: [usize; 5] = [1, 4, 1, 3, 3];
/// [`LAYER_STEPS`] of the small model without the card's second layer, the
/// interleaved schedule of [`TINY_MODULE_LAYERS`].
pub(super) const TINY_LAYER_STEPS: &[(usize, usize)] = &[
    (0, 0),
    (1, 0),
    (2, 0),
    (3, 0),
    (4, 0),
    (1, 1),
    (3, 1),
    (4, 1),
    (1, 2),
    (3, 2),
    (4, 2),
    (1, 3),
];
/// The six-layer Tiny (SM130, arch `w64-c1-d2p1u1`): the tiny model's card
/// and note layers with the deck, preset and user streams cut to 2 / 1 / 1
/// layers. Its schedule is the first two rounds of [`TINY_LAYER_STEPS`] (an
/// exact subsequence of it), so an answer-time head beside it reads 2 layer
/// rounds instead of 4 (346 inputs instead of 474). Only the small engine
/// loads it; every other dimension is the small model's, and the channel
/// mixers it keeps (card 0, deck 0, note 0) are the tiny model's.
pub(super) const SIX_LAYER_MODULE_LAYERS: [usize; 5] = [1, 2, 1, 1, 1];
/// The interleaved schedule of [`SIX_LAYER_MODULE_LAYERS`].
pub(super) const SIX_LAYER_STEPS: &[(usize, usize)] =
    &[(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (1, 1)];
/// The per-module layer counts and the layer schedule of one loaded model:
/// the engine's own, or the tiny or six-layer Tiny model's in the small
/// engine.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct LayerPlan {
    pub(super) module_layers: [usize; 5],
    pub(super) steps: &'static [(usize, usize)],
}
pub(super) const ENGINE_LAYER_PLAN: LayerPlan = LayerPlan {
    module_layers: MODULE_LAYERS,
    steps: LAYER_STEPS,
};
pub(super) const TINY_LAYER_PLAN: LayerPlan = LayerPlan {
    module_layers: TINY_MODULE_LAYERS,
    steps: TINY_LAYER_STEPS,
};
pub(super) const SIX_LAYER_PLAN: LayerPlan = LayerPlan {
    module_layers: SIX_LAYER_MODULE_LAYERS,
    steps: SIX_LAYER_STEPS,
};
/// The plans the small engine loads, told apart by the layer counts of the
/// weights; the big engine loads only its own.
pub(super) const SMALL_LAYER_PLANS: [LayerPlan; 3] =
    [ENGINE_LAYER_PLAN, TINY_LAYER_PLAN, SIX_LAYER_PLAN];
pub(super) fn channel_mixer_present(module: usize, layer: usize) -> bool {
    !SMALL_MODEL || matches!((module, layer), (0, 0) | (1, 0) | (1, 3) | (2, 0))
}
