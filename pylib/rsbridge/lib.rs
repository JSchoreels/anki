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
    inner: rwkv::RwkvInference,
}

#[pyclass(module = "_rsbridge")]
#[derive(Clone)]
struct RwkvInferenceState {
    inner: rwkv::RwkvInferenceState,
}

type RwkvIntervalTuple = (Option<u32>, Option<u32>, Option<u32>, Option<u32>);
type RwkvSerializedStateMap = Vec<(i64, Py<PyBytes>)>;
type RwkvWarmUpSnapshot = (
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    RwkvSerializedStateMap,
    Option<Py<PyBytes>>,
    Py<PyBytes>,
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
fn open_backend(init_msg: &Bound<'_, PyBytes>) -> PyResult<Backend> {
    match init_backend(init_msg.as_bytes()) {
        Ok(backend) => Ok(Backend { backend }),
        Err(e) => Err(PyException::new_err(e)),
    }
}

#[pymethods]
impl Backend {
    fn command(
        &self,
        py: Python,
        service: u32,
        method: u32,
        input: &Bound<'_, PyBytes>,
    ) -> PyResult<Py<PyBytes>> {
        let in_bytes = input.as_bytes();
        py.detach(|| self.backend.run_service_method(service, method, in_bytes))
            .map(|out_bytes| {
                let out_obj = PyBytes::new(py, &out_bytes);
                out_obj.unbind()
            })
            .map_err(BackendError::new_err)
    }

    /// This takes and returns JSON, due to Python's slow protobuf
    /// encoding/decoding.
    fn db_command(&self, py: Python, input: &Bound<'_, PyBytes>) -> PyResult<Py<PyBytes>> {
        let in_bytes = input.as_bytes();
        let out_res = py.detach(|| {
            self.backend
                .run_db_command_bytes(in_bytes)
                .map_err(BackendError::new_err)
        });
        let out_bytes = out_res?;
        let out_obj = PyBytes::new(py, &out_bytes);
        Ok(out_obj.unbind())
    }
}

#[pymethods]
impl RwkvInference {
    #[new]
    #[pyo3(signature = (model_path, target_retention=0.9, max_interval_days=36500))]
    fn new(model_path: &str, target_retention: f32, max_interval_days: u32) -> PyResult<Self> {
        rwkv::RwkvInference::load(model_path.into(), target_retention, max_interval_days)
            .map(|inner| Self { inner })
            .map_err(|err| PyException::new_err(err.to_string()))
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
    ) -> PyResult<(
        f32,
        Option<u32>,
        Option<u32>,
        RwkvIntervalTuple,
        RwkvIntervalTuple,
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
            output.current_interval,
            output.current_s90,
            interval_tuple(output.intervals),
            interval_tuple(output.s90s),
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
            Option<u32>,
            Option<u32>,
            RwkvIntervalTuple,
            RwkvIntervalTuple,
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
                            output.current_interval,
                            output.current_s90,
                            interval_tuple(output.intervals),
                            interval_tuple(output.s90s),
                        )
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

    fn warm_up_reviews(
        &mut self,
        reviews: &Bound<'_, PyAny>,
        record_predictions: bool,
    ) -> PyResult<Vec<(usize, f32)>> {
        let mut parsed_reviews = Vec::new();
        for review in reviews.try_iter()? {
            parsed_reviews.push(parse_rwkv_review_input(&review?)?);
        }

        self.inner
            .warm_up_reviews(parsed_reviews, record_predictions)
            .map_err(|err| PyException::new_err(err.to_string()))
    }

    fn warm_up_snapshot(&self, py: Python<'_>) -> RwkvWarmUpSnapshot {
        let snapshot = self.inner.warm_up_snapshot();
        (
            py_state_map(py, snapshot.card_states),
            py_state_map(py, snapshot.note_states),
            py_state_map(py, snapshot.deck_states),
            py_state_map(py, snapshot.preset_states),
            snapshot
                .global_state
                .map(|state| PyBytes::new(py, &state).unbind()),
            PyBytes::new(py, &self.inner.cache_state()).unbind(),
        )
    }

    fn reset_warm_up_state(&mut self) {
        self.inner.reset_warm_up_state();
    }

    fn state_for_card(&self, card_id: i64) -> RwkvInferenceState {
        RwkvInferenceState {
            inner: self.inner.state_for_card(card_id),
        }
    }

    fn restore_state(&mut self, state: &RwkvInferenceState) {
        self.inner.restore_state(&state.inner)
    }

    fn cache_state(&self, py: Python<'_>) -> Py<PyBytes> {
        PyBytes::new(py, &self.inner.cache_state()).unbind()
    }

    fn restore_cache_state(&mut self, state: &Bound<'_, PyBytes>) -> PyResult<()> {
        self.inner
            .restore_cache_state(state.as_bytes())
            .map_err(|err| PyException::new_err(err.to_string()))
    }
}

fn parse_rwkv_prediction_request(
    request: &Bound<'_, PyAny>,
) -> PyResult<rwkv::ReviewPredictionRequest> {
    let tuple = request.cast::<PyTuple>()?;
    if tuple.len() != 20 {
        return Err(PyException::new_err(
            "RWKV prediction request must contain 20 fields",
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
        },
        state: rwkv::ReviewStateOwned {
            card: optional_bytes(&tuple.get_item(15)?)?,
            note: optional_bytes(&tuple.get_item(16)?)?,
            deck: optional_bytes(&tuple.get_item(17)?)?,
            preset: optional_bytes(&tuple.get_item(18)?)?,
            global: optional_bytes(&tuple.get_item(19)?)?,
        },
    })
}

const PACKED_PREDICTION_REQUEST_MAGIC: &[u8; 8] = b"ARWKVPR1";

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

        parsed_requests.push(rwkv::ReviewPredictionRequest {
            input: rwkv::ReviewInput {
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
            },
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
    if tuple.len() != 15 {
        return Err(PyException::new_err(
            "RWKV review input must contain 15 fields",
        ));
    }

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
    })
}

fn interval_tuple(intervals: [Option<u32>; 4]) -> RwkvIntervalTuple {
    (intervals[0], intervals[1], intervals[2], intervals[3])
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
    m.add_wrapped(wrap_pyfunction!(buildhash)).unwrap();
    m.add_wrapped(wrap_pyfunction!(open_backend)).unwrap();
    m.add_wrapped(wrap_pyfunction!(initialize_logging)).unwrap();
    m.add_wrapped(wrap_pyfunction!(syncserver)).unwrap();

    Ok(())
}
