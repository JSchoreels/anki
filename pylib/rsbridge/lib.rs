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
        card_state: Option<&Bound<'_, PyBytes>>,
        note_state: Option<&Bound<'_, PyBytes>>,
        deck_state: Option<&Bound<'_, PyBytes>>,
        preset_state: Option<&Bound<'_, PyBytes>>,
        global_state: Option<&Bound<'_, PyBytes>>,
    ) -> PyResult<(
        f32,
        Option<u32>,
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
            output.good_interval,
            PyBytes::new(py, &output.card_state).unbind(),
            PyBytes::new(py, &output.note_state).unbind(),
            PyBytes::new(py, &output.deck_state).unbind(),
            PyBytes::new(py, &output.preset_state).unbind(),
            PyBytes::new(py, &output.global_state).unbind(),
        ))
    }

    fn predict_many(&mut self, requests: &Bound<'_, PyAny>) -> PyResult<Vec<(f32, Option<u32>)>> {
        let mut parsed_requests = Vec::new();
        for request in requests.try_iter()? {
            parsed_requests.push(parse_rwkv_prediction_request(&request?)?);
        }

        self.inner
            .predict_many(parsed_requests)
            .map(|outputs| {
                outputs
                    .into_iter()
                    .map(|output| (output.retrievability, output.good_interval))
                    .collect()
            })
            .map_err(|err| PyException::new_err(err.to_string()))
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
    if tuple.len() != 16 {
        return Err(PyException::new_err(
            "RWKV prediction request must contain 16 fields",
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
        },
        state: rwkv::ReviewStateOwned {
            card: optional_bytes(&tuple.get_item(11)?)?,
            note: optional_bytes(&tuple.get_item(12)?)?,
            deck: optional_bytes(&tuple.get_item(13)?)?,
            preset: optional_bytes(&tuple.get_item(14)?)?,
            global: optional_bytes(&tuple.get_item(15)?)?,
        },
    })
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
