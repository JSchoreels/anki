// Copyright: Anki Software, LLC and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Both verified RWKV architectures, with one specialized engine selected per
//! profile.

// Both engines intentionally compile the same sources with different dimensions.
#![allow(clippy::duplicate_mod)]
// Rust 1.99 suggests `as_chunks` for constant-size `chunks_exact`. The RWKV
// kernels keep their verified, bit-exact loops until they are migrated and
// re-benchmarked separately.
#![allow(clippy::chunks_exact_to_as_chunks)]

// The feature controls only the primary engine used by the offline benchmark
// API.
const SMALL_MODEL: bool = cfg!(feature = "rwkv-sm40");
mod alternate;
mod answer_time;
mod answer_time_recal;
mod api;
pub mod paired;
mod runtime;
pub use answer_time::AnswerTimeHistory;
pub use answer_time::AnswerTimePrediction;
pub use answer_time::ANSWER_TIME_BINS;
pub use answer_time::ANSWER_TIME_FEATURES;
pub use answer_time_recal::AnswerTimeRecalibration;
pub use answer_time_recal::TimedEvents;
pub use answer_time_recal::ANSWER_TIME_RECAL_SECONDS;
pub use runtime::AnswerTimeRecalibrator;
pub use runtime::ModelKind;
pub use runtime::RwkvRuntime;
pub use runtime::RwkvRuntimeState;
include!("engine.rs");
#[cfg(test)]
mod app_speed_bench;
#[cfg(test)]
mod checkpoint_compression_bench;
#[cfg(all(test, not(feature = "rwkv-sm40")))]
#[path = "legacy_tests.rs"]
mod legacy_tests;
#[cfg(test)]
mod long_card_tests;
#[cfg(test)]
mod model_size_bench;
#[cfg(all(test, feature = "rwkv-sm40"))]
#[path = "sm40_tests.rs"]
mod sm40_tests;

#[cfg(all(test, feature = "rwkv-sm40"))]
mod curve_reference_probe;
