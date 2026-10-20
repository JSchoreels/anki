// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The RWKV engine of `crate::rwkv` compiled a second time for the small
//! model's shape (`rwkv/shape_small.rs`): kuma3 lets the user pick the model,
//! and the offline runtime picks the engine by the model file.

#[path = "rwkv/shape_small.rs"]
mod rwkv_shape;

#[path = "rwkv/mod.rs"]
mod engine;

pub use engine::*;
