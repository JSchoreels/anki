// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Shape of the fork's 2.76M-parameter RWKV, for the engine in `crate::rwkv`
//! (`rwkv/mod.rs`, which `crate::rwkv_small` compiles a second time with
//! `shape_small.rs`).

/// Whether this is the small model (crate::rwkv_small).
pub(crate) const SMALL: bool = false;
pub(crate) const D_MODEL: usize = 128;
/// Hidden width of the feature MLP and the heads.
pub(crate) const HEAD_DIM: usize = 4 * D_MODEL;
pub(crate) const NUM_CURVES: usize = 128;
pub(crate) const STATE_CACHE_STORE_SCHEMA_VERSION: i64 = 4;
pub(crate) const MODULE_LAYERS: [usize; 5] = [3, 4, 2, 3, 4];
pub(crate) const CHANNEL_MIXER_DIMS: [usize; 5] = [192, 256, 192, 256, 256];
/// (module, layer) in the order one review runs them; each module keeps its
/// own value residual (v0) across its layers.
#[rustfmt::skip]
pub(crate) const LAYER_STEPS: &[(usize, usize)] = &[
    (0, 0), (0, 1), (0, 2),
    (1, 0), (1, 1), (1, 2), (1, 3),
    (2, 0), (2, 1),
    (3, 0), (3, 1), (3, 2),
    (4, 0), (4, 1), (4, 2), (4, 3),
];
/// LoRA ranks of the time mixer: value, a, decay, gate.
pub(crate) const LORA_RANKS: [usize; 4] = [8, 16, 16, 16];
/// Rows per bulk replay chunk. A chunk holds the f32 states of every card and
/// note it touches (kuma3 keeps them as halves otherwise), so kuma3 uses a
/// quarter of the desktop bridge's 8192: a 203k-review replay on a PC peaked
/// at 1.33 GB instead of 1.83 GB, and took 16.4 s instead of 14.7 s.
pub(crate) const BULK_CHUNK_ROWS: usize = 2_048;
