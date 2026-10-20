// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Shape of JSchoreels' 331k-parameter "w64" RWKV (anki-rwkv release
//! small-model-sm40-2026-10-19, its INTEGRATION.md), for the engine compiled
//! as `crate::rwkv_small`: d_model 64, heads of 16, fewer layers per stream
//! run interleaved, some layers without a channel mixer, no value LoRA in
//! layer 0, input 22 ignored, and a 3-power-curve head.

pub(crate) const SMALL: bool = true;
pub(crate) const D_MODEL: usize = 64;
pub(crate) const HEAD_DIM: usize = 128;
/// Only the big model's curve head has these.
pub(crate) const NUM_CURVES: usize = 128;
/// A store of the other model is never read.
pub(crate) const STATE_CACHE_STORE_SCHEMA_VERSION: i64 = 1_004;
pub(crate) const MODULE_LAYERS: [usize; 5] = [2, 4, 1, 3, 3];
pub(crate) const CHANNEL_MIXER_DIMS: [usize; 5] = [64; 5];
#[rustfmt::skip]
pub(crate) const LAYER_STEPS: &[(usize, usize)] = &[
    (0, 0), (1, 0), (2, 0), (3, 0), (4, 0),
    (0, 1), (1, 1), (3, 1), (4, 1),
    (1, 2), (3, 2), (4, 2),
    (1, 3),
];
pub(crate) const LORA_RANKS: [usize; 4] = [2, 4, 4, 4];
/// The small model's states are ~6x smaller and its layer steps run one after
/// another, chunk by chunk, so the per-chunk work dominated at 2,048 rows: a
/// 203k-review rebuild on a Pixel 8a took 130 s, at 16,384 rows 50 s (peak
/// 231 -> 349 MB).
pub(crate) const BULK_CHUNK_ROWS: usize = 16_384;
