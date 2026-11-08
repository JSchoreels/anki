// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki::backend::init_backend;
use anki::backend::Backend as RustBackend;
use anki::log::set_global_logger;
use anki::rwkv;
use anki::sync::http_server::SimpleServer;
use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::types::PyBytes;
use pyo3::types::PyList;
use pyo3::types::PyTuple;
use pyo3::wrap_pyfunction;

#[pyclass(module = "_rsbridge")]
struct Backend {
    backend: RustBackend,
}

#[pyclass(module = "_rsbridge")]
struct RwkvInference {
    inner: rwkv::RwkvRuntime,
}

#[pyclass(module = "_rsbridge")]
#[derive(Clone)]
struct RwkvInferenceState {
    inner: rwkv::RwkvRuntimeState,
}

type RwkvUnroundedIntervalTuple = (Option<f32>, Option<f32>, Option<f32>, Option<f32>);
type RwkvProbabilityTuple = (f32, f32, f32, f32);
type RwkvAnswerTimeTuple = (
    RwkvProbabilityTuple,
    RwkvProbabilityTuple,
    f32,
    RwkvProbabilityTuple,
);
type RwkvSerializedStateMap = Vec<(i64, Py<PyBytes>)>;
type RwkvSerializedState = (
    Option<Py<PyBytes>>,
    Option<Py<PyBytes>>,
    Option<Py<PyBytes>>,
    Option<Py<PyBytes>>,
    Option<Py<PyBytes>>,
);
type RwkvWarmUpSnapshot = (
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    Option<Py<PyBytes>>,
    Py<PyBytes>,
);
type RwkvWorkloadPointTuple = (u32, f32, f32, f32, u32);
type RwkvWorkloadOutput = (f32, f32, Vec<RwkvWorkloadPointTuple>);
type RwkvForecastBandTuple = (f32, f32, f32, f32, f32, f32);
type RwkvForecastDayTuple = (
    u32,
    RwkvForecastBandTuple,
    RwkvForecastBandTuple,
    RwkvForecastBandTuple,
    RwkvForecastBandTuple,
    RwkvForecastBandTuple,
);
type RwkvForecastGroupTuple = (i64, Vec<f32>, Vec<f32>, Vec<f32>);
type RwkvForecastOutputTuple = (
    u32,
    Vec<RwkvForecastDayTuple>,
    Vec<RwkvForecastGroupTuple>,
    u32,
);

create_exception!(_rsbridge, BackendError, PyException);

#[pyfunction]
fn buildhash() -> &'static str {
    anki::version::buildhash()
}

#[pyfunction]
#[pyo3(signature = (path=None))]
fn initialize_logging(path: Option<&str>) -> PyResult<()> {
    set_global_logger(path).map_err(|e| PyException::new_err(e.to_string()))
}

#[pyfunction]
fn syncserver() -> PyResult<()> {
    set_global_logger(None).unwrap();
    let err = SimpleServer::run();
    Err(PyException::new_err(err.to_string()))
}

#[pyfunction]
fn rwkv_release_free_memory(py: Python<'_>) {
    py.detach(rwkv::release_free_memory_to_os);
}

#[pyfunction]
fn open_backend(init_msg: &Bound<'_, PyBytes>) -> PyResult<Backend> {
    match init_backend(init_msg.as_bytes()) {
        Ok(backend) => Ok(Backend { backend }),
        Err(e) => Err(PyException::new_err(e)),
    }
}

#[pymethods]
impl Backend {
    fn command<'a>(
        &self,
        py: Python<'a>,
        service: u32,
        method: u32,
        input: &Bound<'a, PyBytes>,
    ) -> PyResult<Bound<'a, PyBytes>> {
        let in_bytes = input.as_bytes();
        py.detach(|| self.backend.run_service_method(service, method, in_bytes))
            .map(|out_bytes| {
                let out_obj = PyBytes::new(py, &out_bytes);
                out_obj
            })
            .map_err(BackendError::new_err)
    }

    /// This takes and returns JSON, due to Python's slow protobuf
    /// encoding/decoding.
    fn db_command<'a>(
        &self,
        py: Python<'a>,
        input: &Bound<'a, PyBytes>,
    ) -> PyResult<Bound<'a, PyBytes>> {
        let in_bytes = input.as_bytes();
        let out_res = py.detach(|| {
            self.backend
                .run_db_command_bytes(in_bytes)
                .map_err(BackendError::new_err)
        });
        let out_bytes = out_res?;
        let out_obj = PyBytes::new(py, &out_bytes);
        Ok(out_obj)
    }
}

#[pymethods]
impl RwkvInference {
    #[new]
    /// With `instant_model_path` and `instant_model`, an instant-only model
    /// runs beside `model`: it provides instant recall and button
    /// probabilities, `model` the curve, intervals, S90 and answer time.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (model_path, target_retention=0.9, max_interval_days=36500, state_precision="f32", model="sm40", instant_model_path=None, instant_model=None))]
    fn new(
        model_path: &str,
        target_retention: f32,
        max_interval_days: u32,
        state_precision: &str,
        model: &str,
        instant_model_path: Option<&str>,
        instant_model: Option<&str>,
    ) -> PyResult<Self> {
        let precision = rwkv::StatePrecision::parse(state_precision)
            .map_err(|err| PyException::new_err(err.to_string()))?;
        let model =
            rwkv::ModelKind::parse(model).map_err(|err| PyException::new_err(err.to_string()))?;
        match (instant_model_path, instant_model) {
            (Some(instant_model_path), Some(instant_model)) => {
                let instant_model = rwkv::ModelKind::parse(instant_model)
                    .map_err(|err| PyException::new_err(err.to_string()))?;
                rwkv::RwkvRuntime::load_paired(
                    model_path.into(),
                    instant_model_path.into(),
                    target_retention,
                    max_interval_days,
                    precision,
                    model,
                    instant_model,
                )
            }
            (None, None) => rwkv::RwkvRuntime::load_with_precision(
                model_path.into(),
                target_retention,
                max_interval_days,
                precision,
                model,
            ),
            _ => {
                return Err(PyException::new_err(
                    "the instant RWKV model needs both a path and a name",
                ))
            }
        }
        .map(|inner| Self { inner })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    /// Whether an instant-only model runs beside the release model.
    fn is_paired(&self) -> bool {
        self.inner.is_paired()
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
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
        target_retention_again=None,
        target_retention_hard=None,
        target_retention_good=None,
        target_retention_easy=None,
        card_state=None,
        note_state=None,
        deck_state=None,
        preset_state=None,
        global_state=None,
        enforce_grade_order=true,
    ))]
    fn review(
        &mut self,
        py: Python<'_>,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        is_query: bool,
        ease: Option<u8>,
        duration_millis: Option<i64>,
        card_type: Option<i64>,
        day_offset: Option<i64>,
        current_elapsed_days: Option<i64>,
        current_elapsed_seconds: Option<i64>,
        target_retention_again: Option<f32>,
        target_retention_hard: Option<f32>,
        target_retention_good: Option<f32>,
        target_retention_easy: Option<f32>,
        card_state: Option<&Bound<'_, PyBytes>>,
        note_state: Option<&Bound<'_, PyBytes>>,
        deck_state: Option<&Bound<'_, PyBytes>>,
        preset_state: Option<&Bound<'_, PyBytes>>,
        global_state: Option<&Bound<'_, PyBytes>>,
        enforce_grade_order: bool,
    ) -> PyResult<(
        f32,
        Option<f32>,
        Option<u32>,
        Option<f32>,
        RwkvUnroundedIntervalTuple,
        RwkvUnroundedIntervalTuple,
        RwkvProbabilityTuple,
        Py<PyBytes>,
        Py<PyBytes>,
        Py<PyBytes>,
        Py<PyBytes>,
        Py<PyBytes>,
    )> {
        let output = self
            .inner
            .review(
                rwkv::ReviewInput {
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
                    target_retentions: [
                        target_retention_again,
                        target_retention_hard,
                        target_retention_good,
                        target_retention_easy,
                    ],
                    enforce_grade_order,
                },
                rwkv::ReviewState {
                    card: card_state.map(|state| state.as_bytes()),
                    note: note_state.map(|state| state.as_bytes()),
                    deck: deck_state.map(|state| state.as_bytes()),
                    preset: preset_state.map(|state| state.as_bytes()),
                    global: global_state.map(|state| state.as_bytes()),
                },
            )
            .map_err(|err| PyException::new_err(err.to_string()))?;

        Ok((
            output.retrievability,
            output.curve_retrievability,
            output.current_interval,
            output.current_s90,
            unrounded_interval_tuple(output.intervals),
            unrounded_interval_tuple(output.s90s),
            probability_tuple(output.button_probabilities),
            PyBytes::new(py, &output.card_state).unbind(),
            PyBytes::new(py, &output.note_state).unbind(),
            PyBytes::new(py, &output.deck_state).unbind(),
            PyBytes::new(py, &output.preset_state).unbind(),
            PyBytes::new(py, &output.global_state).unbind(),
        ))
    }

    fn predict_many(
        &mut self,
        requests: &Bound<'_, PyAny>,
    ) -> PyResult<
        Vec<(
            f32,
            Option<f32>,
            Option<u32>,
            Option<f32>,
            RwkvUnroundedIntervalTuple,
            RwkvUnroundedIntervalTuple,
            RwkvProbabilityTuple,
        )>,
    > {
        let mut parsed_requests = Vec::new();
        for request in requests.try_iter()? {
            parsed_requests.push(parse_rwkv_prediction_request(&request?)?);
        }

        self.inner
            .predict_many(parsed_requests)
            .map(|outputs| {
                outputs
                    .into_iter()
                    .map(|output| {
                        (
                            output.retrievability,
                            output.curve_retrievability,
                            output.current_interval,
                            output.current_s90,
                            unrounded_interval_tuple(output.intervals),
                            unrounded_interval_tuple(output.s90s),
                            probability_tuple(output.button_probabilities),
                        )
                    })
                    .collect()
            })
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    /// Whether the model folder had the optional answer-time head.
    fn has_answer_time_head(&self) -> bool {
        self.inner.has_answer_time_head()
    }

    /// Per prediction request (the `predict_many` tuple) and its 26 history
    /// features: `None`, or (median seconds per grade, P(under 3.9 s) per
    /// grade, grade-mixed median seconds, the instant head's grade
    /// probabilities).
    fn predict_answer_times(
        &mut self,
        requests: &Bound<'_, PyAny>,
        history: Vec<Vec<f32>>,
    ) -> PyResult<Vec<Option<RwkvAnswerTimeTuple>>> {
        let mut parsed_requests = Vec::new();
        for request in requests.try_iter()? {
            parsed_requests.push(parse_rwkv_prediction_request(&request?)?);
        }
        let history = history
            .into_iter()
            .map(|row| {
                <[f32; rwkv::ANSWER_TIME_FEATURES]>::try_from(row.as_slice()).map_err(|_| {
                    PyException::new_err(format!(
                        "RWKV answer-time history must contain {} values",
                        rwkv::ANSWER_TIME_FEATURES
                    ))
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        self.inner
            .predict_answer_times(parsed_requests, history)
            .map(|outputs| {
                outputs
                    .into_iter()
                    .map(|output| {
                        output.map(|prediction| {
                            (
                                probability_tuple(std::array::from_fn(|grade| {
                                    prediction.median_seconds(grade)
                                })),
                                probability_tuple(std::array::from_fn(|grade| {
                                    prediction.fast_probability(grade)
                                })),
                                prediction.mixed_median_seconds(),
                                probability_tuple(prediction.button_probabilities),
                            )
                        })
                    })
                    .collect()
            })
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many(&mut self, requests: &Bound<'_, PyAny>) -> PyResult<Vec<f32>> {
        let mut parsed_requests = Vec::new();
        for request in requests.try_iter()? {
            parsed_requests.push(parse_rwkv_prediction_request(&request?)?);
        }

        self.inner
            .predict_retrievability_many(parsed_requests)
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many_from_warm_up(
        &mut self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<f32>> {
        let mut parsed_inputs = Vec::new();
        for input in inputs.try_iter()? {
            parsed_inputs.push(parse_rwkv_review_input(&input?)?);
        }

        py.detach(|| {
            self.inner
                .predict_retrievability_many_from_warm_up(parsed_inputs)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many_from_warm_up_packed(
        &mut self,
        py: Python<'_>,
        inputs: &Bound<'_, PyBytes>,
    ) -> PyResult<Py<PyBytes>> {
        let parsed_inputs = parse_packed_rwkv_review_inputs(inputs.as_bytes())?;
        let outputs = py
            .detach(|| {
                self.inner
                    .predict_retrievability_many_from_warm_up(parsed_inputs)
            })
            .map_err(|err| PyException::new_err(err.to_string()))?;
        let mut packed = Vec::with_capacity(outputs.len() * std::mem::size_of::<f32>());
        for output in outputs {
            packed.extend_from_slice(&output.to_le_bytes());
        }
        Ok(PyBytes::new(py, &packed).unbind())
    }

    fn predict_retrievability_many_packed(
        &mut self,
        requests: &Bound<'_, PyBytes>,
        state_columns: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<f32>> {
        self.inner
            .predict_retrievability_many(parse_packed_rwkv_prediction_requests(
                requests.as_bytes(),
                state_columns,
            )?)
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many_after_review(
        &self,
        answer: &Bound<'_, PyAny>,
        query_inputs: &Bound<'_, PyAny>,
        snapshot: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<f32>> {
        let answer = parse_rwkv_review_input(answer)?;
        let mut parsed_query_inputs = Vec::new();
        for query_input in query_inputs.try_iter()? {
            parsed_query_inputs.push(parse_rwkv_review_input(&query_input?)?);
        }
        let snapshot = parse_rwkv_workload_snapshot(snapshot)?;

        self.inner
            .predict_retrievability_many_after_review(answer, parsed_query_inputs, snapshot)
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many_after_reviews(
        &self,
        answers: &Bound<'_, PyAny>,
        query_inputs: &Bound<'_, PyAny>,
        snapshot: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<Vec<f32>>> {
        let mut parsed_answers = Vec::new();
        for answer in answers.try_iter()? {
            parsed_answers.push(parse_rwkv_review_input(&answer?)?);
        }
        let mut parsed_query_inputs = Vec::new();
        for query_input in query_inputs.try_iter()? {
            parsed_query_inputs.push(parse_rwkv_review_input(&query_input?)?);
        }
        let snapshot = parse_rwkv_workload_snapshot(snapshot)?;

        self.inner
            .predict_retrievability_many_after_reviews(
                parsed_answers,
                parsed_query_inputs,
                snapshot,
            )
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn predict_retrievability_many_after_reviews_from_warm_up(
        &mut self,
        py: Python<'_>,
        answers: &Bound<'_, PyAny>,
        query_inputs: &Bound<'_, PyAny>,
    ) -> PyResult<Vec<Vec<f32>>> {
        let mut parsed_answers = Vec::new();
        for answer in answers.try_iter()? {
            parsed_answers.push(parse_rwkv_review_input(&answer?)?);
        }
        let mut parsed_query_inputs = Vec::new();
        for query_input in query_inputs.try_iter()? {
            parsed_query_inputs.push(parse_rwkv_review_input(&query_input?)?);
        }

        py.detach(|| {
            self.inner
                .predict_retrievability_many_after_reviews_from_warm_up(
                    parsed_answers,
                    parsed_query_inputs,
                )
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_reviews(
        &mut self,
        py: Python<'_>,
        reviews: &Bound<'_, PyAny>,
        record_predictions: bool,
    ) -> PyResult<Vec<(usize, f32)>> {
        let mut parsed_reviews = Vec::new();
        for review in reviews.try_iter()? {
            parsed_reviews.push(parse_rwkv_review_input(&review?)?);
        }

        py.detach(|| {
            self.inner
                .warm_up_reviews(parsed_reviews, record_predictions)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_reviews_packed(
        &mut self,
        py: Python<'_>,
        reviews: &Bound<'_, PyBytes>,
        record_predictions: bool,
    ) -> PyResult<Vec<(usize, f32)>> {
        let parsed_reviews = parse_packed_rwkv_review_inputs(reviews.as_bytes())?;
        py.detach(|| {
            self.inner
                .warm_up_reviews(parsed_reviews, record_predictions)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn forecast_history_predictions_packed(
        &self,
        py: Python<'_>,
        reviews: &Bound<'_, PyBytes>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Vec<(usize, f32)>> {
        let parsed_reviews = parse_packed_rwkv_review_inputs(reviews.as_bytes())?;
        let progress = progress.map(|callback| callback.clone().unbind());
        let mut progress_callback =
            |current, total| report_rwkv_workload_progress(progress.as_ref(), current, total);
        py.detach(|| {
            self.inner
                .forecast_history_predictions(parsed_reviews, &mut progress_callback)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_reviews_post_packed(
        &mut self,
        py: Python<'_>,
        reviews: &Bound<'_, PyBytes>,
    ) -> PyResult<Vec<(usize, f32)>> {
        let parsed_reviews = parse_packed_rwkv_review_inputs(reviews.as_bytes())?;
        py.detach(|| self.inner.warm_up_reviews_post(parsed_reviews))
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_reviews_query_packed(
        &mut self,
        py: Python<'_>,
        reviews: &Bound<'_, PyBytes>,
        queries: &Bound<'_, PyBytes>,
        after: bool,
    ) -> PyResult<Vec<(usize, f32)>> {
        let parsed_reviews = parse_packed_rwkv_review_inputs(reviews.as_bytes())?;
        let parsed_queries = parse_packed_rwkv_review_inputs(queries.as_bytes())?;
        py.detach(|| {
            self.inner
                .warm_up_reviews_query(parsed_reviews, parsed_queries, after)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_snapshot(&self, py: Python<'_>) -> PyResult<RwkvWarmUpSnapshot> {
        let (snapshot, cache_state) =
            py.detach(|| (self.inner.warm_up_snapshot(), self.inner.cache_state()));
        let snapshot = snapshot.map_err(|err| PyException::new_err(err.to_string()))?;
        Ok((
            py_state_map(py, snapshot.card_states),
            py_state_map(py, snapshot.note_states),
            py_state_map(py, snapshot.deck_states),
            py_state_map(py, snapshot.preset_states),
            snapshot
                .global_state
                .map(|state| PyBytes::new(py, &state).unbind()),
            PyBytes::new(py, &cache_state).unbind(),
        ))
    }

    fn warm_up_state(
        &self,
        py: Python<'_>,
        review: &Bound<'_, PyAny>,
    ) -> PyResult<RwkvSerializedState> {
        let input = parse_rwkv_review_input(review)?;
        let state = py
            .detach(|| self.inner.warm_up_state(&input))
            .map_err(|err| PyException::new_err(err.to_string()))?;
        Ok((
            state.card.map(|state| PyBytes::new(py, &state).unbind()),
            state.note.map(|state| PyBytes::new(py, &state).unbind()),
            state.deck.map(|state| PyBytes::new(py, &state).unbind()),
            state.preset.map(|state| PyBytes::new(py, &state).unbind()),
            state.global.map(|state| PyBytes::new(py, &state).unbind()),
        ))
    }

    fn append_warm_up_snapshot_binary(&self, py: Python<'_>, path: &str) -> PyResult<()> {
        py.detach(|| self.inner.append_warm_up_snapshot_binary(path.into()))
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    #[allow(clippy::too_many_arguments)]
    fn write_warm_up_state_checkpoint(
        &mut self,
        py: Python<'_>,
        path: String,
        store_generation: String,
        parent_segment_id: Option<i64>,
        last_review_id: i64,
        review_count: i64,
        history_hash: String,
        replay_key: String,
        previous_review_ids: Vec<u8>,
        previous_intervals: Vec<u8>,
        review_counts: Vec<u8>,
        full: bool,
        durable: bool,
    ) -> PyResult<i64> {
        py.detach(|| {
            self.inner.write_warm_up_state_checkpoint(
                path.into(),
                &store_generation,
                parent_segment_id,
                last_review_id,
                review_count,
                &history_hash,
                &replay_key,
                &previous_review_ids,
                &previous_intervals,
                &review_counts,
                full,
                durable,
            )
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn finish_warm_up_state_checkpoints(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.inner.finish_warm_up_state_checkpoints())
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn restore_warm_up_state_checkpoint(
        &mut self,
        py: Python<'_>,
        path: String,
        store_generation: String,
        segment_id: i64,
    ) -> PyResult<()> {
        py.detach(|| {
            self.inner
                .restore_warm_up_state_checkpoint(path.into(), &store_generation, segment_id)
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn restore_warm_up_snapshot(
        &mut self,
        py: Python<'_>,
        snapshot: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        // Copying the bytes out needs the GIL; decoding them does not.
        let snapshot = parse_rwkv_warm_up_snapshot(snapshot)?;
        py.detach(|| self.inner.restore_warm_up_snapshot(snapshot))
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    #[allow(clippy::too_many_arguments)]
    fn restore_warm_up_state(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        card_state: Option<&Bound<'_, PyBytes>>,
        note_state: Option<&Bound<'_, PyBytes>>,
        deck_state: Option<&Bound<'_, PyBytes>>,
        preset_state: Option<&Bound<'_, PyBytes>>,
        global_state: Option<&Bound<'_, PyBytes>>,
    ) -> PyResult<()> {
        self.inner
            .restore_warm_up_state(
                card_id,
                note_id,
                deck_id,
                preset_id,
                rwkv::ReviewStateOwned {
                    card: card_state.map(|state| state.as_bytes().to_vec()),
                    note: note_state.map(|state| state.as_bytes().to_vec()),
                    deck: deck_state.map(|state| state.as_bytes().to_vec()),
                    preset: preset_state.map(|state| state.as_bytes().to_vec()),
                    global: global_state.map(|state| state.as_bytes().to_vec()),
                },
            )
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn simulate_workload(
        &mut self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
        snapshot: &Bound<'_, PyAny>,
        min_dr: u32,
        max_dr: u32,
        target_dr_step: u32,
        days_to_simulate: u32,
        review_limit: u32,
        new_limit: u32,
        new_cards_ignore_review_limit: bool,
        max_interval: u32,
        review_order: i32,
        suspend_after_lapses: Option<u32>,
        state_update_interval: u32,
        grade_seconds: &Bound<'_, PyAny>,
        bucket_probabilities: &Bound<'_, PyAny>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<RwkvWorkloadOutput> {
        let parsed_inputs = parse_rwkv_workload_simulation_inputs(inputs)?;
        let config = rwkv_workload_simulation_config(
            min_dr,
            max_dr,
            target_dr_step,
            days_to_simulate,
            review_limit,
            new_limit,
            new_cards_ignore_review_limit,
            max_interval,
            review_order,
            suspend_after_lapses,
            state_update_interval,
            grade_seconds,
            bucket_probabilities,
        )?;
        let snapshot = parse_rwkv_workload_snapshot(snapshot)?;
        let progress = progress.map(|callback| callback.clone().unbind());
        let mut progress_callback = |current: u32, total: u32| {
            report_rwkv_workload_progress(progress.as_ref(), current, total)
        };

        py.detach(|| {
            self.inner
                .simulate_workload(parsed_inputs, snapshot, config, &mut progress_callback)
        })
        .map(rwkv_workload_output)
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    /// Like `simulate_workload`, but reads the resident warmed state instead of
    /// a serialized snapshot. The resident state is left unchanged.
    #[allow(clippy::too_many_arguments)]
    fn simulate_workload_from_warm_up(
        &self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
        min_dr: u32,
        max_dr: u32,
        target_dr_step: u32,
        days_to_simulate: u32,
        review_limit: u32,
        new_limit: u32,
        new_cards_ignore_review_limit: bool,
        max_interval: u32,
        review_order: i32,
        suspend_after_lapses: Option<u32>,
        state_update_interval: u32,
        grade_seconds: &Bound<'_, PyAny>,
        bucket_probabilities: &Bound<'_, PyAny>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<RwkvWorkloadOutput> {
        let parsed_inputs = parse_rwkv_workload_simulation_inputs(inputs)?;
        let config = rwkv_workload_simulation_config(
            min_dr,
            max_dr,
            target_dr_step,
            days_to_simulate,
            review_limit,
            new_limit,
            new_cards_ignore_review_limit,
            max_interval,
            review_order,
            suspend_after_lapses,
            state_update_interval,
            grade_seconds,
            bucket_probabilities,
        )?;
        let progress = progress.map(|callback| callback.clone().unbind());
        let mut progress_callback = |current: u32, total: u32| {
            report_rwkv_workload_progress(progress.as_ref(), current, total)
        };

        py.detach(|| {
            self.inner
                .simulate_workload_from_warm_up(parsed_inputs, config, &mut progress_callback)
        })
        .map(rwkv_workload_output)
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    #[allow(clippy::too_many_arguments)]
    fn compare_workloads_from_warm_up(
        &self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
        min_dr: u32,
        max_dr: u32,
        target_dr_step: u32,
        days_to_simulate: u32,
        review_limit: f32,
        new_limit: f32,
        new_cards_ignore_review_limit: bool,
        max_interval: u32,
        review_order: i32,
        suspend_after_lapses: Option<u32>,
        learner: i32,
        scenario: i32,
        repetitions: u32,
        output_scale: f32,
        grade_seconds: &Bound<'_, PyAny>,
        bucket_probabilities: &Bound<'_, PyAny>,
        background: &Bound<'_, PyAny>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<(
        RwkvWorkloadOutput,
        RwkvWorkloadOutput,
        RwkvWorkloadOutput,
        RwkvWorkloadOutput,
        u32,
    )> {
        let mut parsed_inputs = Vec::new();
        for value in inputs.try_iter()? {
            let value = value?;
            let tuple = value.cast::<PyTuple>()?;
            if tuple.len() != 5 {
                return Err(PyException::new_err(
                    "RWKV comparison input must contain 5 fields",
                ));
            }
            parsed_inputs.push(rwkv::RwkvComparisonCard {
                input: parse_rwkv_workload_simulation_input(&tuple.get_item(0)?)?,
                due_day: tuple.get_item(1)?.extract()?,
                fsrs_memory: tuple.get_item(2)?.extract()?,
                fsrs_parameters: tuple.get_item(3)?.extract()?,
                historical_retention: tuple.get_item(4)?.extract()?,
            });
        }
        let learner = match learner {
            0 => rwkv::RwkvComparisonLearner::Fsrs,
            1 => rwkv::RwkvComparisonLearner::Rwkv,
            _ => return Err(PyException::new_err("invalid comparison learner")),
        };
        let scenario = match scenario {
            0 => rwkv::RwkvPerformanceScenario::Steady,
            1 => rwkv::RwkvPerformanceScenario::TemporaryDrop,
            2 => rwkv::RwkvPerformanceScenario::PersistentDrop,
            3 => rwkv::RwkvPerformanceScenario::TemporaryImprovement,
            _ => return Err(PyException::new_err("invalid performance scenario")),
        };
        let mut parsed_background = Vec::new();
        for value in background.try_iter()? {
            let value = value?;
            let tuple = value.cast::<PyTuple>()?;
            if tuple.len() != 5 {
                return Err(PyException::new_err(
                    "RWKV background review must contain 5 fields",
                ));
            }
            parsed_background.push(rwkv::RwkvComparisonBackgroundReview {
                input: parse_rwkv_review_input(&tuple.get_item(0)?)?,
                pattern_day: tuple.get_item(1)?.extract()?,
                second_of_day: tuple.get_item(2)?.extract()?,
                last_review_day: tuple.get_item(3)?.extract()?,
                last_review_second: tuple.get_item(4)?.extract()?,
            });
        }
        let config = rwkv::RwkvComparisonConfig {
            min_dr,
            max_dr,
            target_dr_step,
            days_to_simulate,
            review_limit,
            new_limit,
            new_cards_ignore_review_limit,
            max_interval,
            review_order: review_order
                .try_into()
                .unwrap_or(anki::deckconfig::ReviewCardOrder::Day),
            suspend_after_lapses,
            learner,
            scenario,
            repetitions,
            output_scale,
            background: parsed_background,
            review_model: rwkv::RwkvWorkloadReviewModel {
                grade_seconds: parse_f32_quad(grade_seconds, "grade seconds")?,
                bucket_probabilities: parse_rwkv_workload_bucket_probabilities(
                    bucket_probabilities,
                )?,
            },
        };
        let progress = progress.map(|callback| callback.clone().unbind());
        let mut progress_callback = |current: u32, total: u32| {
            report_rwkv_workload_progress(progress.as_ref(), current, total)
        };
        py.detach(|| {
            self.inner
                .compare_workloads_from_warm_up(parsed_inputs, config, &mut progress_callback)
        })
        .map(|output| {
            (
                rwkv_workload_output(output.fsrs_scored_by_fsrs),
                rwkv_workload_output(output.rwkv_scored_by_fsrs),
                rwkv_workload_output(output.fsrs_scored_by_rwkv),
                rwkv_workload_output(output.rwkv_scored_by_rwkv),
                output.estimated_fsrs_states,
            )
        })
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    /// Day-by-day workload forecast over `paths` seeded answer paths, from the
    /// resident warmed state (left unchanged). Returns
    /// `(paths, days, groups, estimated_fsrs_states)` where each day is
    /// `(day, due, done, new, lapses, seconds)` and each of those a
    /// `(mean, p1, p5, p50, p95, p99)` band; each group is
    /// `(group, due_mean, done_mean, new_mean)` with one value per day.
    #[allow(clippy::too_many_arguments)]
    fn forecast_workload_from_warm_up(
        &self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
        groups: &Bound<'_, PyAny>,
        days: u32,
        paths: u32,
        seed: u32,
        default_review_limit: u32,
        default_new_limit: u32,
        new_cards_ignore_review_limit: bool,
        max_interval: u32,
        review_order: i32,
        suspend_after_lapses: Option<u32>,
        state_update_interval: u32,
        single_step_answers: bool,
        grade_seconds: &Bound<'_, PyAny>,
        bucket_probabilities: &Bound<'_, PyAny>,
        progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<RwkvForecastOutputTuple> {
        let mut parsed_inputs = Vec::new();
        for value in inputs.try_iter()? {
            let value = value?;
            let tuple = value.cast::<PyTuple>()?;
            if tuple.len() != 8 {
                return Err(PyException::new_err(
                    "RWKV forecast input must contain 8 fields",
                ));
            }
            let scheduler = match tuple.get_item(4)?.extract::<i32>()? {
                0 => rwkv::RwkvForecastScheduler::Rwkv,
                1 => rwkv::RwkvForecastScheduler::Fsrs,
                2 => rwkv::RwkvForecastScheduler::Sm2,
                _ => return Err(PyException::new_err("invalid forecast scheduler")),
            };
            parsed_inputs.push(rwkv::RwkvForecastCard {
                input: parse_rwkv_workload_simulation_input(&tuple.get_item(0)?)?,
                due_day: tuple.get_item(1)?.extract()?,
                group: tuple.get_item(2)?.extract()?,
                desired_retention: tuple.get_item(3)?.extract()?,
                scheduler,
                fsrs_memory: tuple.get_item(5)?.extract()?,
                fsrs_parameters: tuple.get_item(6)?.extract()?,
                historical_retention: tuple.get_item(7)?.extract()?,
            });
        }
        let mut parsed_groups = Vec::new();
        for value in groups.try_iter()? {
            let (group, review_limit, new_limit, reviews_done_today, new_done_today) =
                value?.extract::<(i64, u32, u32, u32, u32)>()?;
            parsed_groups.push(rwkv::RwkvForecastGroup {
                group,
                review_limit,
                new_limit,
                reviews_done_today,
                new_done_today,
            });
        }
        let config = rwkv::RwkvWorkloadForecastConfig {
            days,
            paths,
            seed,
            groups: parsed_groups,
            default_review_limit,
            default_new_limit,
            new_cards_ignore_review_limit,
            max_interval,
            review_order: review_order
                .try_into()
                .unwrap_or(anki::deckconfig::ReviewCardOrder::Day),
            suspend_after_lapses,
            state_update_interval,
            single_step_answers,
            review_model: rwkv::RwkvWorkloadReviewModel {
                grade_seconds: parse_f32_quad(grade_seconds, "grade seconds")?,
                bucket_probabilities: parse_rwkv_workload_bucket_probabilities(
                    bucket_probabilities,
                )?,
            },
        };
        let progress = progress.map(|callback| callback.clone().unbind());
        let mut progress_callback = |current: u32, total: u32| {
            report_rwkv_workload_progress(progress.as_ref(), current, total)
        };
        py.detach(|| {
            self.inner
                .forecast_workload_from_warm_up(parsed_inputs, config, &mut progress_callback)
        })
        .map(rwkv_forecast_output)
        .map_err(|err| PyException::new_err(err.to_string()))
    }

    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        inputs, decks, deck_configs, study_deck_id, timing_patterns,
        timing_pattern_success_shares, days, paths, seed, today,
        new_cards_ignore_review_limit, fsrs_learning_queues_disabled,
        fsrs_short_term_with_steps_enabled, learn_ahead_secs, now_millis,
        same_day_allowed, min_intervening_reviews, min_elapsed_secs,
        refresh_interval, candidate_refresh_enabled, candidate_refresh_limit,
        precompute_next_card, review_order, grade_seconds, bucket_probabilities,
        progress=None, scenario_sigma=None, day_progress=None
    ))]
    fn forecast_instant_workload_from_warm_up(
        &self,
        py: Python<'_>,
        inputs: &Bound<'_, PyAny>,
        decks: Vec<Vec<u8>>,
        deck_configs: Vec<Vec<u8>>,
        study_deck_id: i64,
        timing_patterns: Vec<Vec<(u32, u32)>>,
        timing_pattern_success_shares: Vec<(f32, f32, f32)>,
        days: u32,
        paths: u32,
        seed: u32,
        today: u32,
        new_cards_ignore_review_limit: bool,
        fsrs_learning_queues_disabled: bool,
        fsrs_short_term_with_steps_enabled: bool,
        learn_ahead_secs: u32,
        now_millis: u32,
        same_day_allowed: bool,
        min_intervening_reviews: u32,
        min_elapsed_secs: u32,
        refresh_interval: u32,
        candidate_refresh_enabled: bool,
        candidate_refresh_limit: usize,
        precompute_next_card: bool,
        review_order: i32,
        grade_seconds: &Bound<'_, PyAny>,
        bucket_probabilities: &Bound<'_, PyAny>,
        progress: Option<&Bound<'_, PyAny>>,
        scenario_sigma: Option<f32>,
        day_progress: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let mut parsed_inputs = Vec::new();
        for value in inputs.try_iter()? {
            let value = value?;
            let tuple = value.cast::<PyTuple>()?;
            if tuple.len() != 15 {
                return Err(PyException::new_err(
                    "Instant forecast input must contain 15 fields",
                ));
            }
            parsed_inputs.push(rwkv::RwkvInstantForecastCard {
                input: parse_rwkv_workload_simulation_input(&tuple.get_item(0)?)?,
                due_day: tuple.get_item(1)?.extract()?,
                group: tuple.get_item(2)?.extract()?,
                desired_retention: tuple.get_item(3)?.extract()?,
                intervening_reviews: tuple.get_item(4)?.extract()?,
                queue: tuple.get_item(5)?.extract()?,
                learning_due_millis: tuple.get_item(6)?.extract()?,
                remaining_steps: tuple.get_item(7)?.extract()?,
                new_position: tuple.get_item(8)?.extract()?,
                fsrs_memory: tuple.get_item(9)?.extract()?,
                fsrs_parameters: tuple.get_item(10)?.extract()?,
                fsrs_enabled: tuple.get_item(11)?.extract()?,
                instant_enabled: tuple.get_item(12)?.extract()?,
                last_kind_and_grade: tuple.get_item(13)?.extract()?,
                fsrs_historical_retention: tuple.get_item(14)?.extract()?,
            });
        }
        let (decks, deck_configs) = rwkv::decode_instant_forecast_decks(decks, deck_configs)
            .map_err(|error| PyException::new_err(error.to_string()))?;
        let config = rwkv::RwkvInstantForecastConfig {
            days,
            paths,
            scenario_sigma,
            report_days: day_progress.is_some(),
            seed,
            decks,
            deck_configs,
            study_deck_id,
            today,
            new_cards_ignore_review_limit,
            fsrs_learning_queues_disabled,
            fsrs_short_term_with_steps_enabled,
            learn_ahead_secs,
            timing_patterns,
            timing_pattern_success_shares: timing_pattern_success_shares
                .into_iter()
                .map(|(hard, good, easy)| [0.0, hard, good, easy])
                .collect(),
            now_millis,
            same_day_allowed,
            min_intervening_reviews,
            min_elapsed_secs,
            refresh_interval,
            candidate_refresh_enabled,
            candidate_refresh_limit,
            precompute_next_card,
            review_order: review_order
                .try_into()
                .unwrap_or(anki::deckconfig::ReviewCardOrder::Day),
            review_model: rwkv::RwkvWorkloadReviewModel {
                grade_seconds: parse_f32_quad(grade_seconds, "grade seconds")?,
                bucket_probabilities: parse_rwkv_workload_bucket_probabilities(
                    bucket_probabilities,
                )?,
            },
        };
        let progress = progress.map(|callback| callback.clone().unbind());
        let day_progress = day_progress.map(|callback| callback.clone().unbind());
        let mut progress_callback = |current: u32, total: u32| {
            report_rwkv_workload_progress(progress.as_ref(), current, total)
        };
        let mut day_callback = |path: u32, day: u32, values: [f32; 5]| {
            if let Some(callback) = &day_progress {
                Python::attach(|py| callback.call1(py, (path, day, values))).map_err(|err| {
                    std::io::Error::new(std::io::ErrorKind::Other, err.to_string())
                })?;
            }
            Ok(())
        };
        let mut output = py
            .detach(|| {
                self.inner.forecast_instant_workload_from_warm_up(
                    parsed_inputs,
                    config,
                    &mut progress_callback,
                    &mut day_callback,
                )
            })
            .map_err(|err| PyException::new_err(err.to_string()))?;
        let scenarios = output.scenarios.take();
        let (paths, days, groups, estimated) = rwkv_forecast_output(output);
        if let Some(scenarios) = scenarios {
            Ok((paths, days, groups, estimated, scenarios)
                .into_pyobject(py)?
                .into_any()
                .unbind())
        } else {
            Ok((paths, days, groups, estimated)
                .into_pyobject(py)?
                .into_any()
                .unbind())
        }
    }

    fn reset_warm_up_state(&mut self) {
        self.inner.reset_warm_up_state();
    }

    fn stash_warm_up_state(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.inner.stash_warm_up_state())
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn supports_warm_up_state_spill(&self) -> bool {
        true
    }

    fn stash_warm_up_state_to_file(&mut self, py: Python<'_>, path: String) -> PyResult<()> {
        py.detach(|| self.inner.stash_warm_up_state_to_file(path.as_ref()))
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn restore_stashed_warm_up_state(&mut self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| self.inner.restore_stashed_warm_up_state())
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn discard_stashed_warm_up_state(&mut self, py: Python<'_>) {
        py.detach(|| self.inner.discard_stashed_warm_up_state());
    }

    fn state_for_card(&self, card_id: i64) -> RwkvInferenceState {
        RwkvInferenceState {
            inner: self.inner.state_for_card(card_id),
        }
    }

    #[pyo3(signature = (card_id, note_id=None, deck_id=None, preset_id=None))]
    fn state_for_review(
        &self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
    ) -> RwkvInferenceState {
        RwkvInferenceState {
            inner: self
                .inner
                .state_for_review(card_id, note_id, deck_id, preset_id),
        }
    }

    fn restore_state(&mut self, state: &RwkvInferenceState) -> PyResult<()> {
        self.inner
            .restore_state(&state.inner)
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn cache_state(&self, py: Python<'_>) -> Py<PyBytes> {
        let state = py.detach(|| self.inner.cache_state());
        PyBytes::new(py, &state).unbind()
    }

    fn restore_cache_state(&mut self, py: Python<'_>, state: &Bound<'_, PyBytes>) -> PyResult<()> {
        let state = state.as_bytes().to_vec();
        py.detach(|| self.inner.restore_cache_state(&state))
            .map_err(|err| PyException::new_err(err.to_string()))
    }
}

fn parse_rwkv_prediction_request(
    request: &Bound<'_, PyAny>,
) -> PyResult<rwkv::ReviewPredictionRequest> {
    let tuple = request.cast::<PyTuple>()?;
    if tuple.len() != 21 {
        return Err(PyException::new_err(
            "RWKV prediction request must contain 21 fields",
        ));
    }

    Ok(rwkv::ReviewPredictionRequest {
        input: rwkv::ReviewInput {
            card_id: tuple.get_item(0)?.extract()?,
            note_id: tuple.get_item(1)?.extract()?,
            deck_id: tuple.get_item(2)?.extract()?,
            preset_id: tuple.get_item(3)?.extract()?,
            is_query: tuple.get_item(4)?.extract()?,
            ease: tuple.get_item(5)?.extract()?,
            duration_millis: tuple.get_item(6)?.extract()?,
            card_type: tuple.get_item(7)?.extract()?,
            day_offset: tuple.get_item(8)?.extract()?,
            current_elapsed_days: tuple.get_item(9)?.extract()?,
            current_elapsed_seconds: tuple.get_item(10)?.extract()?,
            target_retentions: [
                tuple.get_item(11)?.extract()?,
                tuple.get_item(12)?.extract()?,
                tuple.get_item(13)?.extract()?,
                tuple.get_item(14)?.extract()?,
            ],
            enforce_grade_order: tuple.get_item(15)?.extract()?,
        },
        state: rwkv::ReviewStateOwned {
            card: optional_bytes(&tuple.get_item(16)?)?,
            note: optional_bytes(&tuple.get_item(17)?)?,
            deck: optional_bytes(&tuple.get_item(18)?)?,
            preset: optional_bytes(&tuple.get_item(19)?)?,
            global: optional_bytes(&tuple.get_item(20)?)?,
        },
    })
}

const PACKED_PREDICTION_REQUEST_MAGIC: &[u8; 8] = b"ARWKVPR2";

fn parse_packed_rwkv_prediction_requests(
    requests: &[u8],
    state_columns: &Bound<'_, PyAny>,
) -> PyResult<Vec<rwkv::ReviewPredictionRequest>> {
    let mut cursor = PackedPredictionRequestCursor::new(requests);
    let magic = cursor.read_array::<8>()?;
    if &magic != PACKED_PREDICTION_REQUEST_MAGIC {
        return Err(PyException::new_err("invalid RWKV packed request header"));
    }

    let request_count = cursor.read_u32()? as usize;
    let states = parse_packed_prediction_request_states(state_columns, request_count)?;
    let mut parsed_requests = Vec::with_capacity(request_count);
    for state in states {
        parsed_requests.push(rwkv::ReviewPredictionRequest {
            input: read_packed_rwkv_review_input(&mut cursor)?,
            state,
        });
    }

    if !cursor.is_finished() {
        return Err(PyException::new_err(
            "trailing bytes in RWKV packed prediction request",
        ));
    }

    Ok(parsed_requests)
}

const PACKED_WARM_UP_REVIEW_MAGIC: &[u8; 8] = b"ARWKVWU2";

fn parse_packed_rwkv_review_inputs(reviews: &[u8]) -> PyResult<Vec<rwkv::ReviewInput>> {
    let mut cursor = PackedPredictionRequestCursor::new(reviews);
    let magic = cursor.read_array::<8>()?;
    if &magic != PACKED_WARM_UP_REVIEW_MAGIC {
        return Err(PyException::new_err(
            "invalid RWKV packed warm-up review header",
        ));
    }

    let review_count = cursor.read_u32()? as usize;
    let mut parsed_reviews = Vec::with_capacity(review_count);
    for _ in 0..review_count {
        parsed_reviews.push(read_packed_rwkv_review_input(&mut cursor)?);
    }

    if !cursor.is_finished() {
        return Err(PyException::new_err(
            "trailing bytes in RWKV packed warm-up reviews",
        ));
    }

    Ok(parsed_reviews)
}

fn read_packed_rwkv_review_input(
    cursor: &mut PackedPredictionRequestCursor<'_>,
) -> PyResult<rwkv::ReviewInput> {
    let presence = cursor.read_u32()?;
    let card_id = cursor.read_i64()?;
    let note_id = cursor.read_i64()?;
    let deck_id = cursor.read_i64()?;
    let preset_id = cursor.read_i64()?;
    let is_query = cursor.read_bool()?;
    let ease = cursor.read_u8()?;
    let duration_millis = cursor.read_i64()?;
    let card_type = cursor.read_i64()?;
    let day_offset = cursor.read_i64()?;
    let current_elapsed_days = cursor.read_i64()?;
    let current_elapsed_seconds = cursor.read_i64()?;
    let target_retention_again = cursor.read_f32()?;
    let target_retention_hard = cursor.read_f32()?;
    let target_retention_good = cursor.read_f32()?;
    let target_retention_easy = cursor.read_f32()?;
    let enforce_grade_order = cursor.read_bool()?;

    Ok(rwkv::ReviewInput {
        card_id,
        note_id: optional_i64(presence, 0, note_id),
        deck_id: optional_i64(presence, 1, deck_id),
        preset_id: optional_i64(presence, 2, preset_id),
        is_query,
        ease: optional_u8(presence, 3, ease),
        duration_millis: optional_i64(presence, 4, duration_millis),
        card_type: optional_i64(presence, 5, card_type),
        day_offset: optional_i64(presence, 6, day_offset),
        current_elapsed_days: optional_i64(presence, 7, current_elapsed_days),
        current_elapsed_seconds: optional_i64(presence, 8, current_elapsed_seconds),
        target_retentions: [
            optional_f32(presence, 9, target_retention_again),
            optional_f32(presence, 10, target_retention_hard),
            optional_f32(presence, 11, target_retention_good),
            optional_f32(presence, 12, target_retention_easy),
        ],
        enforce_grade_order,
    })
}

fn parse_packed_prediction_request_states(
    state_columns: &Bound<'_, PyAny>,
    request_count: usize,
) -> PyResult<Vec<rwkv::ReviewStateOwned>> {
    let columns = state_columns.cast::<PyTuple>()?;
    if columns.len() != 5 {
        return Err(PyException::new_err(
            "RWKV packed state columns must contain 5 fields",
        ));
    }

    let card_states_any = columns.get_item(0)?;
    let note_states_any = columns.get_item(1)?;
    let deck_states_any = columns.get_item(2)?;
    let preset_states_any = columns.get_item(3)?;
    let global_states_any = columns.get_item(4)?;

    let card_states = card_states_any.cast::<PyList>()?;
    let note_states = note_states_any.cast::<PyList>()?;
    let deck_states = deck_states_any.cast::<PyList>()?;
    let preset_states = preset_states_any.cast::<PyList>()?;
    let global_states = global_states_any.cast::<PyList>()?;

    for column in [
        card_states,
        note_states,
        deck_states,
        preset_states,
        global_states,
    ] {
        if column.len() != request_count {
            return Err(PyException::new_err(
                "RWKV packed state column count mismatch",
            ));
        }
    }

    let mut states = Vec::with_capacity(request_count);
    for index in 0..request_count {
        states.push(rwkv::ReviewStateOwned {
            card: optional_bytes(&card_states.get_item(index)?)?,
            note: optional_bytes(&note_states.get_item(index)?)?,
            deck: optional_bytes(&deck_states.get_item(index)?)?,
            preset: optional_bytes(&preset_states.get_item(index)?)?,
            global: optional_bytes(&global_states.get_item(index)?)?,
        });
    }

    Ok(states)
}

fn optional_i64(presence: u32, bit: u32, value: i64) -> Option<i64> {
    ((presence & (1_u32 << bit)) != 0).then_some(value)
}

fn optional_u8(presence: u32, bit: u32, value: u8) -> Option<u8> {
    ((presence & (1_u32 << bit)) != 0).then_some(value)
}

fn optional_f32(presence: u32, bit: u32, value: f32) -> Option<f32> {
    ((presence & (1_u32 << bit)) != 0).then_some(value)
}

struct PackedPredictionRequestCursor<'a> {
    bytes: &'a [u8],
    offset: usize,
}

impl<'a> PackedPredictionRequestCursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, offset: 0 }
    }

    fn is_finished(&self) -> bool {
        self.offset == self.bytes.len()
    }

    fn read_array<const N: usize>(&mut self) -> PyResult<[u8; N]> {
        let bytes = self.read_bytes(N)?;
        Ok(bytes.try_into().expect("slice length is checked"))
    }

    fn read_bool(&mut self) -> PyResult<bool> {
        match self.read_u8()? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(PyException::new_err(
                "invalid bool in RWKV packed prediction request",
            )),
        }
    }

    fn read_u8(&mut self) -> PyResult<u8> {
        Ok(self.read_array::<1>()?[0])
    }

    fn read_u32(&mut self) -> PyResult<u32> {
        Ok(u32::from_le_bytes(self.read_array()?))
    }

    fn read_i64(&mut self) -> PyResult<i64> {
        Ok(i64::from_le_bytes(self.read_array()?))
    }

    fn read_f32(&mut self) -> PyResult<f32> {
        Ok(f32::from_le_bytes(self.read_array()?))
    }

    fn read_bytes(&mut self, length: usize) -> PyResult<&'a [u8]> {
        let end = self
            .offset
            .checked_add(length)
            .ok_or_else(|| PyException::new_err("RWKV packed prediction request is too large"))?;
        if end > self.bytes.len() {
            return Err(PyException::new_err(
                "truncated RWKV packed prediction request",
            ));
        }

        let bytes = &self.bytes[self.offset..end];
        self.offset = end;
        Ok(bytes)
    }
}

fn parse_rwkv_review_input(request: &Bound<'_, PyAny>) -> PyResult<rwkv::ReviewInput> {
    let tuple = request.cast::<PyTuple>()?;
    if tuple.len() != 16 {
        return Err(PyException::new_err(
            "RWKV review input must contain 16 fields",
        ));
    }

    parse_rwkv_review_input_tuple(tuple)
}

fn parse_rwkv_review_input_tuple(tuple: &Bound<'_, PyTuple>) -> PyResult<rwkv::ReviewInput> {
    Ok(rwkv::ReviewInput {
        card_id: tuple.get_item(0)?.extract()?,
        note_id: tuple.get_item(1)?.extract()?,
        deck_id: tuple.get_item(2)?.extract()?,
        preset_id: tuple.get_item(3)?.extract()?,
        is_query: tuple.get_item(4)?.extract()?,
        ease: tuple.get_item(5)?.extract()?,
        duration_millis: tuple.get_item(6)?.extract()?,
        card_type: tuple.get_item(7)?.extract()?,
        day_offset: tuple.get_item(8)?.extract()?,
        current_elapsed_days: tuple.get_item(9)?.extract()?,
        current_elapsed_seconds: tuple.get_item(10)?.extract()?,
        target_retentions: [
            tuple.get_item(11)?.extract()?,
            tuple.get_item(12)?.extract()?,
            tuple.get_item(13)?.extract()?,
            tuple.get_item(14)?.extract()?,
        ],
        enforce_grade_order: tuple.get_item(15)?.extract()?,
    })
}

fn parse_rwkv_workload_simulation_input(
    request: &Bound<'_, PyAny>,
) -> PyResult<rwkv::RwkvWorkloadSimulationInput> {
    let tuple = request.cast::<PyTuple>()?;
    if tuple.len() != 20 {
        return Err(PyException::new_err(
            "RWKV workload simulation input must contain 20 fields",
        ));
    }

    Ok(rwkv::RwkvWorkloadSimulationInput {
        review_input: parse_rwkv_review_input_tuple(tuple)?,
        interval_days: tuple.get_item(16)?.extract()?,
        ease_factor: tuple.get_item(17)?.extract()?,
        reps: tuple.get_item(18)?.extract()?,
        lapses: tuple.get_item(19)?.extract()?,
    })
}

fn parse_rwkv_workload_simulation_inputs(
    inputs: &Bound<'_, PyAny>,
) -> PyResult<Vec<rwkv::RwkvWorkloadSimulationInput>> {
    let mut parsed_inputs = Vec::new();
    for input in inputs.try_iter()? {
        parsed_inputs.push(parse_rwkv_workload_simulation_input(&input?)?);
    }
    Ok(parsed_inputs)
}

#[allow(clippy::too_many_arguments)]
fn rwkv_workload_simulation_config(
    min_dr: u32,
    max_dr: u32,
    target_dr_step: u32,
    days_to_simulate: u32,
    review_limit: u32,
    new_limit: u32,
    new_cards_ignore_review_limit: bool,
    max_interval: u32,
    review_order: i32,
    suspend_after_lapses: Option<u32>,
    state_update_interval: u32,
    grade_seconds: &Bound<'_, PyAny>,
    bucket_probabilities: &Bound<'_, PyAny>,
) -> PyResult<rwkv::RwkvWorkloadSimulationConfig> {
    Ok(rwkv::RwkvWorkloadSimulationConfig {
        min_dr,
        max_dr,
        target_dr_step,
        days_to_simulate,
        review_limit,
        new_limit,
        new_cards_ignore_review_limit,
        max_interval,
        review_order: review_order
            .try_into()
            .unwrap_or(anki::deckconfig::ReviewCardOrder::Day),
        suspend_after_lapses,
        state_update_interval,
        review_model: rwkv::RwkvWorkloadReviewModel {
            grade_seconds: parse_f32_quad(grade_seconds, "grade seconds")?,
            bucket_probabilities: parse_rwkv_workload_bucket_probabilities(bucket_probabilities)?,
        },
    })
}

fn report_rwkv_workload_progress(
    progress: Option<&Py<PyAny>>,
    current: u32,
    total: u32,
) -> std::io::Result<()> {
    if let Some(callback) = progress {
        Python::attach(|py| callback.call1(py, (current, total)))
            .map(|_| ())
            .map_err(|err| std::io::Error::new(std::io::ErrorKind::Other, err.to_string()))?;
    }
    Ok(())
}

fn rwkv_forecast_band(band: rwkv::RwkvForecastBand) -> RwkvForecastBandTuple {
    (band.mean, band.p1, band.p5, band.p50, band.p95, band.p99)
}

fn rwkv_forecast_output(output: rwkv::RwkvWorkloadForecastOutput) -> RwkvForecastOutputTuple {
    (
        output.paths,
        output
            .days
            .into_iter()
            .map(|day| {
                (
                    day.day,
                    rwkv_forecast_band(day.due),
                    rwkv_forecast_band(day.done),
                    rwkv_forecast_band(day.new),
                    rwkv_forecast_band(day.lapses),
                    rwkv_forecast_band(day.seconds),
                )
            })
            .collect(),
        output
            .groups
            .into_iter()
            .map(|group| (group.group, group.due_mean, group.done_mean, group.new_mean))
            .collect(),
        output.estimated_fsrs_states,
    )
}

fn rwkv_workload_output(output: rwkv::RwkvWorkloadSimulationOutput) -> RwkvWorkloadOutput {
    (
        output.reviewless_end_memorized,
        output.reviewless_end_weighted_memorized,
        output
            .points
            .into_iter()
            .map(|(dr, point)| {
                (
                    dr,
                    point.memorized,
                    point.weighted_memorized,
                    point.cost,
                    point.review_count,
                )
            })
            .collect(),
    )
}

fn parse_rwkv_workload_snapshot(
    snapshot: &Bound<'_, PyAny>,
) -> PyResult<rwkv::RwkvWorkloadSimulationSnapshot> {
    let tuple = snapshot.cast::<PyTuple>()?;
    if tuple.len() != 6 {
        return Err(PyException::new_err(
            "RWKV workload snapshot must contain 6 fields",
        ));
    }

    Ok(rwkv::RwkvWorkloadSimulationSnapshot {
        card_states: parse_serialized_state_map(&tuple.get_item(0)?)?,
        note_states: parse_serialized_state_map(&tuple.get_item(1)?)?,
        deck_states: parse_serialized_state_map(&tuple.get_item(2)?)?,
        preset_states: parse_serialized_state_map(&tuple.get_item(3)?)?,
        global_state: optional_bytes(&tuple.get_item(4)?)?,
        runtime_state: optional_bytes(&tuple.get_item(5)?)?,
    })
}

/// Like `parse_rwkv_workload_snapshot()`, without copying the runtime state
/// that a warm-up snapshot restore ignores.
fn parse_rwkv_warm_up_snapshot(snapshot: &Bound<'_, PyAny>) -> PyResult<rwkv::RwkvWarmUpSnapshot> {
    let tuple = snapshot.cast::<PyTuple>()?;
    if tuple.len() != 6 {
        return Err(PyException::new_err(
            "RWKV workload snapshot must contain 6 fields",
        ));
    }

    Ok(rwkv::RwkvWarmUpSnapshot {
        card_states: parse_serialized_state_map(&tuple.get_item(0)?)?,
        note_states: parse_serialized_state_map(&tuple.get_item(1)?)?,
        deck_states: parse_serialized_state_map(&tuple.get_item(2)?)?,
        preset_states: parse_serialized_state_map(&tuple.get_item(3)?)?,
        global_state: optional_bytes(&tuple.get_item(4)?)?,
    })
}

fn parse_serialized_state_map(value: &Bound<'_, PyAny>) -> PyResult<Vec<(i64, Vec<u8>)>> {
    let mut states = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        let tuple = item.cast::<PyTuple>()?;
        if tuple.len() != 2 {
            return Err(PyException::new_err(
                "RWKV serialized state map entry must contain 2 fields",
            ));
        }
        states.push((
            tuple.get_item(0)?.extract()?,
            tuple.get_item(1)?.cast::<PyBytes>()?.as_bytes().to_vec(),
        ));
    }
    Ok(states)
}

fn parse_f32_quad(value: &Bound<'_, PyAny>, name: &str) -> PyResult<[f32; 4]> {
    let tuple = value.cast::<PyTuple>()?;
    if tuple.len() != 4 {
        return Err(PyException::new_err(format!(
            "RWKV workload {name} must contain 4 fields"
        )));
    }
    Ok([
        tuple.get_item(0)?.extract()?,
        tuple.get_item(1)?.extract()?,
        tuple.get_item(2)?.extract()?,
        tuple.get_item(3)?.extract()?,
    ])
}

fn parse_rwkv_workload_bucket_probabilities(
    value: &Bound<'_, PyAny>,
) -> PyResult<Vec<(u32, [f32; 4])>> {
    let mut probabilities = Vec::new();
    for item in value.try_iter()? {
        let item = item?;
        let tuple = item.cast::<PyTuple>()?;
        if tuple.len() != 5 {
            return Err(PyException::new_err(
                "RWKV workload bucket probability entry must contain 5 fields",
            ));
        }
        probabilities.push((
            tuple.get_item(0)?.extract()?,
            [
                tuple.get_item(1)?.extract()?,
                tuple.get_item(2)?.extract()?,
                tuple.get_item(3)?.extract()?,
                tuple.get_item(4)?.extract()?,
            ],
        ));
    }
    Ok(probabilities)
}

fn unrounded_interval_tuple(intervals: [Option<f32>; 4]) -> RwkvUnroundedIntervalTuple {
    (intervals[0], intervals[1], intervals[2], intervals[3])
}

fn probability_tuple(probabilities: [f32; 4]) -> RwkvProbabilityTuple {
    (
        probabilities[0],
        probabilities[1],
        probabilities[2],
        probabilities[3],
    )
}

fn py_state_map(py: Python<'_>, states: Vec<(i64, Vec<u8>)>) -> RwkvSerializedStateMap {
    states
        .into_iter()
        .map(|(key, state)| (key, PyBytes::new(py, &state).unbind()))
        .collect()
}

fn optional_bytes(value: &Bound<'_, PyAny>) -> PyResult<Option<Vec<u8>>> {
    if value.is_none() {
        return Ok(None);
    }

    Ok(Some(value.cast::<PyBytes>()?.as_bytes().to_vec()))
}

// Module definition
//////////////////////////////////

#[pymodule]
fn _rsbridge(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Backend>()?;
    m.add_class::<RwkvInference>()?;
    m.add_class::<RwkvInferenceState>()?;
    m.add_wrapped(wrap_pyfunction!(rwkv_release_free_memory))
        .unwrap();
    m.add_wrapped(wrap_pyfunction!(buildhash)).unwrap();
    m.add_wrapped(wrap_pyfunction!(open_backend)).unwrap();
    m.add_wrapped(wrap_pyfunction!(initialize_logging)).unwrap();
    m.add_wrapped(wrap_pyfunction!(syncserver)).unwrap();

    Ok(())
}
