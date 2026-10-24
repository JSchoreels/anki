// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::io;
use std::path::Path;
use std::path::PathBuf;

use super::api::*;
use super::StatePrecision;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ModelKind {
    Legacy,
    Sm40,
    Sm36,
    Sm55,
    Sm56,
}

impl ModelKind {
    pub fn parse(value: &str) -> io::Result<Self> {
        match value {
            "legacy" => Ok(Self::Legacy),
            "sm40" => Ok(Self::Sm40),
            "sm36" => Ok(Self::Sm36),
            "sm55" => Ok(Self::Sm55),
            "sm56" => Ok(Self::Sm56),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unknown RWKV model",
            )),
        }
    }
}

pub enum RwkvRuntime {
    Primary(Box<super::RwkvInference>),
    Alternate(Box<super::alternate::RwkvInference>),
}

#[derive(Clone)]
pub enum RwkvRuntimeState {
    Primary(super::RwkvInferenceState),
    Alternate(super::alternate::RwkvInferenceState),
}

macro_rules! dispatch {
    ($runtime:expr, $engine:ident, $call:expr) => {
        match $runtime {
            RwkvRuntime::Primary($engine) => $call,
            RwkvRuntime::Alternate($engine) => $call,
        }
    };
}

impl RwkvRuntime {
    pub fn load_with_precision(
        path: PathBuf,
        target_retention: f32,
        max_interval_days: u32,
        precision: StatePrecision,
        model: ModelKind,
    ) -> io::Result<Self> {
        if matches!(
            model,
            ModelKind::Sm40 | ModelKind::Sm36 | ModelKind::Sm55 | ModelKind::Sm56
        ) == super::SMALL_MODEL
        {
            super::RwkvInference::load_with_precision(
                path,
                target_retention,
                max_interval_days,
                precision,
            )
            .map(|engine| Self::Primary(Box::new(engine)))
        } else {
            let precision = match precision {
                StatePrecision::F32 => super::alternate::StatePrecision::F32,
                StatePrecision::Int8 => super::alternate::StatePrecision::Int8,
                StatePrecision::Int4 => super::alternate::StatePrecision::Int4,
                StatePrecision::Int4CardsInt8Notes => {
                    super::alternate::StatePrecision::Int4CardsInt8Notes
                }
            };
            super::alternate::RwkvInference::load_with_precision(
                path,
                target_retention,
                max_interval_days,
                precision,
            )
            .map(|engine| Self::Alternate(Box::new(engine)))
        }
    }

    pub fn state_for_card(&self, card_id: i64) -> RwkvRuntimeState {
        match self {
            Self::Primary(engine) => RwkvRuntimeState::Primary(engine.state_for_card(card_id)),
            Self::Alternate(engine) => RwkvRuntimeState::Alternate(engine.state_for_card(card_id)),
        }
    }

    pub fn state_for_review(
        &self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
    ) -> RwkvRuntimeState {
        match self {
            Self::Primary(engine) => RwkvRuntimeState::Primary(
                engine.state_for_review(card_id, note_id, deck_id, preset_id),
            ),
            Self::Alternate(engine) => RwkvRuntimeState::Alternate(
                engine.state_for_review(card_id, note_id, deck_id, preset_id),
            ),
        }
    }

    pub fn restore_state(&mut self, state: &RwkvRuntimeState) -> io::Result<()> {
        match (self, state) {
            (Self::Primary(engine), RwkvRuntimeState::Primary(state)) => {
                engine.restore_state(state)
            }
            (Self::Alternate(engine), RwkvRuntimeState::Alternate(state)) => {
                engine.restore_state(state)
            }
            _ => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV undo state belongs to a different model",
                ))
            }
        }
        Ok(())
    }

    pub fn review(
        &mut self,
        input: ReviewInput,
        state: ReviewState<'_>,
    ) -> io::Result<ReviewOutput> {
        dispatch!(self, engine, engine.review(input, state))
    }

    pub fn predict_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<ReviewPredictionOutput>> {
        dispatch!(self, engine, engine.predict_many(requests))
    }

    pub fn predict_retrievability_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<f32>> {
        dispatch!(self, engine, engine.predict_retrievability_many(requests))
    }

    pub fn predict_retrievability_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<f32>> {
        dispatch!(
            self,
            engine,
            engine.predict_retrievability_many_from_warm_up(inputs)
        )
    }

    pub fn predict_retrievability_many_after_review(
        &self,
        answer: ReviewInput,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<f32>> {
        dispatch!(
            self,
            engine,
            engine.predict_retrievability_many_after_review(answer, query_inputs, snapshot)
        )
    }

    pub fn predict_retrievability_many_after_reviews(
        &self,
        answers: Vec<ReviewInput>,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<Vec<f32>>> {
        dispatch!(
            self,
            engine,
            engine.predict_retrievability_many_after_reviews(answers, query_inputs, snapshot)
        )
    }

    pub fn predict_retrievability_many_after_reviews_from_warm_up(
        &mut self,
        answers: Vec<ReviewInput>,
        query_inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<Vec<f32>>> {
        dispatch!(
            self,
            engine,
            engine.predict_retrievability_many_after_reviews_from_warm_up(answers, query_inputs)
        )
    }

    pub fn warm_up_reviews(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<(usize, f32)>> {
        dispatch!(
            self,
            engine,
            engine.warm_up_reviews(reviews, record_predictions)
        )
    }

    pub fn warm_up_snapshot(&self) -> RwkvWarmUpSnapshot {
        dispatch!(self, engine, engine.warm_up_snapshot())
    }

    pub fn warm_up_state(&self, input: &ReviewInput) -> ReviewStateOwned {
        dispatch!(self, engine, engine.warm_up_state(input))
    }

    pub fn append_warm_up_snapshot_binary(&self, path: PathBuf) -> io::Result<()> {
        dispatch!(self, engine, engine.append_warm_up_snapshot_binary(path))
    }

    // Keep the engine checkpoint API unchanged at the dispatch boundary.
    #[allow(clippy::too_many_arguments)]
    pub fn write_warm_up_state_checkpoint(
        &mut self,
        path: PathBuf,
        store_generation: &str,
        parent_segment_id: Option<i64>,
        last_review_id: i64,
        review_count: i64,
        history_hash: &str,
        replay_key: &str,
        previous_review_ids: &[u8],
        previous_intervals: &[u8],
        review_counts: &[u8],
        full: bool,
        durable: bool,
    ) -> io::Result<i64> {
        dispatch!(
            self,
            engine,
            engine.write_warm_up_state_checkpoint(
                path,
                store_generation,
                parent_segment_id,
                last_review_id,
                review_count,
                history_hash,
                replay_key,
                previous_review_ids,
                previous_intervals,
                review_counts,
                full,
                durable
            )
        )
    }

    pub fn finish_warm_up_state_checkpoints(&mut self) -> io::Result<()> {
        dispatch!(self, engine, engine.finish_warm_up_state_checkpoints())
    }

    pub fn restore_warm_up_state_checkpoint(
        &mut self,
        path: PathBuf,
        store_generation: &str,
        segment_id: i64,
    ) -> io::Result<()> {
        dispatch!(
            self,
            engine,
            engine.restore_warm_up_state_checkpoint(path, store_generation, segment_id)
        )
    }

    pub fn restore_warm_up_snapshot(&mut self, snapshot: RwkvWarmUpSnapshot) -> io::Result<()> {
        dispatch!(self, engine, engine.restore_warm_up_snapshot(snapshot))
    }

    pub fn restore_warm_up_state(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        state: ReviewStateOwned,
    ) -> io::Result<()> {
        dispatch!(
            self,
            engine,
            engine.restore_warm_up_state(card_id, note_id, deck_id, preset_id, state)
        )
    }

    pub fn stash_warm_up_state(&mut self) -> io::Result<()> {
        dispatch!(self, engine, engine.stash_warm_up_state())
    }

    pub fn stash_warm_up_state_to_file(&mut self, path: &Path) -> io::Result<()> {
        dispatch!(self, engine, engine.stash_warm_up_state_to_file(path))
    }

    pub fn restore_stashed_warm_up_state(&mut self) -> io::Result<()> {
        dispatch!(self, engine, engine.restore_stashed_warm_up_state())
    }

    pub fn discard_stashed_warm_up_state(&mut self) {
        dispatch!(self, engine, engine.discard_stashed_warm_up_state())
    }

    pub fn reset_warm_up_state(&mut self) {
        dispatch!(self, engine, engine.reset_warm_up_state())
    }

    pub fn cache_state(&self) -> Vec<u8> {
        dispatch!(self, engine, engine.cache_state())
    }

    pub fn restore_cache_state(&mut self, bytes: &[u8]) -> io::Result<()> {
        dispatch!(self, engine, engine.restore_cache_state(bytes))
    }

    pub fn simulate_workload(
        &self,
        inputs: Vec<RwkvWorkloadSimulationInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
        config: RwkvWorkloadSimulationConfig,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<RwkvWorkloadSimulationOutput> {
        dispatch!(
            self,
            engine,
            engine.simulate_workload(inputs, snapshot, config, progress)
        )
    }

    pub fn simulate_workload_from_warm_up(
        &self,
        inputs: Vec<RwkvWorkloadSimulationInput>,
        config: RwkvWorkloadSimulationConfig,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<RwkvWorkloadSimulationOutput> {
        dispatch!(
            self,
            engine,
            engine.simulate_workload_from_warm_up(inputs, config, progress)
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load(model: ModelKind, precision: StatePrecision) -> RwkvRuntime {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(match model {
            ModelKind::Legacy => "../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin",
            ModelKind::Sm40 => "../qt/aqt/rwkv_inference/sm40v2/weights.f32.bin",
            ModelKind::Sm36 => "../qt/aqt/rwkv_inference/sm36/weights.f32.bin",
            ModelKind::Sm55 => "../qt/aqt/rwkv_inference/sm55/weights.f32.bin",
            ModelKind::Sm56 => "../qt/aqt/rwkv_inference/sm56/weights.f32.bin",
        });
        RwkvRuntime::load_with_precision(path, 0.9, 36_500, precision, model).unwrap()
    }

    #[test]
    fn rwkv_runtime_restores_each_model_at_each_precision() {
        for precision in [
            StatePrecision::F32,
            StatePrecision::Int8,
            StatePrecision::Int4,
            StatePrecision::Int4CardsInt8Notes,
        ] {
            let mut legacy = load(ModelKind::Legacy, precision);
            let mut small = load(ModelKind::Sm40, precision);
            // Even an empty runtime snapshot must carry the model identity.
            assert!(legacy.restore_cache_state(&small.cache_state()).is_err());
            assert!(small.restore_cache_state(&legacy.cache_state()).is_err());
            assert!(legacy.restore_state(&small.state_for_card(100)).is_err());
            assert!(small.restore_state(&legacy.state_for_card(100)).is_err());
            for (model, mut engine) in [
                (ModelKind::Legacy, legacy),
                (ModelKind::Sm40, small),
                (ModelKind::Sm36, load(ModelKind::Sm36, precision)),
                (ModelKind::Sm55, load(ModelKind::Sm55, precision)),
                (ModelKind::Sm56, load(ModelKind::Sm56, precision)),
            ] {
                let answers = crate::rwkv::precision_tests::reviews(14);
                engine.warm_up_reviews(answers.clone(), false).unwrap();
                let mut query = answers[0].clone();
                query.is_query = true;
                query.ease = None;
                query.duration_millis = None;
                let before = engine
                    .predict_retrievability_many_from_warm_up(vec![query.clone()])
                    .unwrap();
                let mut restored = load(model, precision);
                restored
                    .restore_warm_up_snapshot(engine.warm_up_snapshot())
                    .unwrap();
                restored.restore_cache_state(&engine.cache_state()).unwrap();
                let after = restored
                    .predict_retrievability_many_from_warm_up(vec![query])
                    .unwrap();
                assert_eq!(before, after, "{model:?} {precision:?}");
            }
        }
        assert!(ModelKind::parse("unknown").is_err());
    }
}
