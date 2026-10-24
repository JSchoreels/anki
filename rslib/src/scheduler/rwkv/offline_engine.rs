// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The offline runtime's RWKV: the fork's 2.76M-parameter model
//! (`crate::rwkv`) or JSchoreels' 331k-parameter one (`crate::rwkv::alternate`,
//! the fork's second compile of the engine), picked by the model file the app
//! passes (kuma3's Settings > RWKV model). Both engines take the same inputs;
//! the small one gets copies of its own types.

use std::fs;
use std::io;
use std::path::PathBuf;

use crate::rwkv;
use crate::rwkv::alternate as rwkv_small;
use crate::rwkv::ReviewInput;
use crate::rwkv::StatePrecision;

pub(super) enum Engine {
    Big(rwkv::RwkvInference),
    Small(rwkv_small::RwkvInference),
}

/// See [rwkv::RwkvInference::state_snapshot].
pub(super) enum Snapshot {
    Big(rwkv::RwkvStateSnapshot),
    Small(rwkv_small::RwkvStateSnapshot),
}

/// The small model's weights carry its 3-power-curve head (`gru_w_weight`);
/// the big model's file has no such tensor.
fn is_small_model(weights: &[u8]) -> bool {
    weights.windows(12).any(|name| name == b"gru_w_weight")
}

fn small_precision(precision: StatePrecision) -> rwkv_small::StatePrecision {
    match precision {
        StatePrecision::F32 => rwkv_small::StatePrecision::F32,
        StatePrecision::Int8 => rwkv_small::StatePrecision::Int8,
        StatePrecision::Int4 => rwkv_small::StatePrecision::Int4,
        StatePrecision::Int4CardsInt8Notes => rwkv_small::StatePrecision::Int4CardsInt8Notes,
    }
}

fn small_inputs(inputs: Vec<ReviewInput>) -> Vec<rwkv_small::ReviewInput> {
    inputs
        .into_iter()
        .map(|input| {
            let ReviewInput {
                card_id,
                note_id,
                deck_id,
                preset_id,
                is_query,
                ease,
                duration_millis,
                card_type,
                day_offset,
                current_elapsed_days,
                current_elapsed_seconds,
                target_retentions,
                enforce_grade_order,
            } = input;
            rwkv_small::ReviewInput {
                card_id,
                note_id,
                deck_id,
                preset_id,
                is_query,
                ease,
                duration_millis,
                card_type,
                day_offset,
                current_elapsed_days,
                current_elapsed_seconds,
                target_retentions,
                enforce_grade_order,
            }
        })
        .collect()
}

impl Engine {
    pub(super) fn load_with_precision(
        path: PathBuf,
        target_retention: f32,
        max_interval_days: u32,
        precision: StatePrecision,
    ) -> io::Result<Self> {
        Ok(if is_small_model(&fs::read(&path)?) {
            Self::Small(rwkv_small::RwkvInference::load_with_precision(
                path,
                target_retention,
                max_interval_days,
                small_precision(precision),
            )?)
        } else {
            Self::Big(rwkv::RwkvInference::load_with_precision(
                path,
                target_retention,
                max_interval_days,
                precision,
            )?)
        })
    }

    pub(super) fn cache_state(&self) -> Vec<u8> {
        match self {
            Self::Big(engine) => engine.cache_state(),
            Self::Small(engine) => engine.cache_state(),
        }
    }

    pub(super) fn restore_cache_state(&mut self, bytes: &[u8]) -> io::Result<()> {
        match self {
            Self::Big(engine) => engine.restore_cache_state(bytes),
            Self::Small(engine) => engine.restore_cache_state(bytes),
        }
    }

    pub(super) fn reset_warm_up_state(&mut self) {
        match self {
            Self::Big(engine) => engine.reset_warm_up_state(),
            Self::Small(engine) => engine.reset_warm_up_state(),
        }
    }

    pub(super) fn read_warm_up_states(&mut self, input: &mut impl io::Read) -> io::Result<()> {
        match self {
            Self::Big(engine) => engine.read_warm_up_states(input),
            Self::Small(engine) => engine.read_warm_up_states(input),
        }
    }

    pub(super) fn state_snapshot(&self) -> Snapshot {
        match self {
            Self::Big(engine) => Snapshot::Big(engine.state_snapshot()),
            Self::Small(engine) => Snapshot::Small(engine.state_snapshot()),
        }
    }

    pub(super) fn warm_up_reviews(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<(usize, f32)>> {
        match self {
            Self::Big(engine) => engine.warm_up_reviews(reviews, record_predictions),
            Self::Small(engine) => {
                engine.warm_up_reviews(small_inputs(reviews), record_predictions)
            }
        }
    }

    /// RWKV-Curve for one card from its warm-up state: the four answers'
    /// unrounded intervals in days at the input's target retentions, and
    /// their S90s (the desktop's `predict_many`: a query pass plus one per
    /// simulated answer).
    pub(super) fn curve_from_warm_up(
        &mut self,
        input: ReviewInput,
    ) -> io::Result<([Option<f32>; 4], [Option<f32>; 4])> {
        Ok(match self {
            Self::Big(engine) => {
                let state = engine.warm_up_state(&input);
                let out =
                    engine.predict_many(vec![rwkv::ReviewPredictionRequest { input, state }])?;
                out.first().map(|o| (o.intervals, o.s90s))
            }
            Self::Small(engine) => {
                let input = small_inputs(vec![input]).remove(0);
                let state = engine.warm_up_state(&input);
                let out = engine
                    .predict_many(vec![rwkv_small::ReviewPredictionRequest { input, state }])?;
                out.first().map(|o| (o.intervals, o.s90s))
            }
        }
        .unwrap_or_default())
    }

    /// RWKV-Curve's current curve of each card from its warm-up state: the
    /// whole-day interval where it meets the input's Good target, and its
    /// S90 (what "Reschedule Cards with RWKV-Curve Intervals" applies).
    pub(super) fn curve_current_many(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<(Option<u32>, Option<f32>)>> {
        // ponytail: predict_many runs the 4 answer passes too; a query-only
        // variant if a big deck is slow to reschedule
        Ok(match self {
            Self::Big(engine) => {
                let requests = inputs
                    .into_iter()
                    .map(|input| {
                        let state = engine.warm_up_state(&input);
                        rwkv::ReviewPredictionRequest { input, state }
                    })
                    .collect();
                engine
                    .predict_many(requests)?
                    .into_iter()
                    .map(|o| (o.current_interval, o.current_s90))
                    .collect()
            }
            Self::Small(engine) => {
                let requests = small_inputs(inputs)
                    .into_iter()
                    .map(|input| {
                        let state = engine.warm_up_state(&input);
                        rwkv_small::ReviewPredictionRequest { input, state }
                    })
                    .collect();
                engine
                    .predict_many(requests)?
                    .into_iter()
                    .map(|o| (o.current_interval, o.current_s90))
                    .collect()
            }
        })
    }

    pub(super) fn predict_retrievability_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<f32>> {
        match self {
            Self::Big(engine) => engine.predict_retrievability_many_from_warm_up(inputs),
            Self::Small(engine) => {
                engine.predict_retrievability_many_from_warm_up(small_inputs(inputs))
            }
        }
    }
}

impl Snapshot {
    pub(super) fn write_warm_up_states(&self, out: &mut impl io::Write) -> io::Result<()> {
        match self {
            Self::Big(state) => state.write_warm_up_states(out),
            Self::Small(state) => state.write_warm_up_states(out),
        }
    }

    pub(super) fn cache_state(&self) -> &[u8] {
        match self {
            Self::Big(state) => state.cache_state(),
            Self::Small(state) => state.cache_state(),
        }
    }
}
