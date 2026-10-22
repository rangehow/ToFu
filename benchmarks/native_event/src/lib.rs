//! Experimental bounded replay storage; no production lifecycle or durability authority.
use std::collections::VecDeque;
use pyo3::prelude::*;
use pyo3::gc::PyVisit;
use pyo3::PyTraverseError;
use pyo3::types::PyList;
use pyo3::exceptions::{PyValueError, PyIndexError};

#[pyclass(weakref)]
struct ReplayWindow {
    entries: VecDeque<Py<PyAny>>,
    sizes: VecDeque<usize>,
    bytes: usize,
    maximum_count: usize,
    maximum_bytes: usize,
}

#[pymethods]
impl ReplayWindow {
    #[new]
    fn new(maximum_count: usize, maximum_bytes: usize) -> PyResult<Self> {
        if maximum_count == 0 || maximum_bytes == 0 {
            return Err(PyValueError::new_err("positive retention bounds required"));
        }
        Ok(Self { entries: VecDeque::new(), sizes: VecDeque::new(), bytes: 0, maximum_count, maximum_bytes })
    }

    fn append(mut slf: PyRefMut<'_, Self>, event: Py<PyAny>, size: usize) -> PyResult<usize> {
        let next_bytes = slf.bytes.checked_add(size)
            .ok_or_else(|| PyValueError::new_err("retained byte total overflow"))?;
        slf.entries.push_back(event);
        slf.sizes.push_back(size);
        slf.bytes = next_bytes;
        let mut removed = 0;
        let mut retired = Vec::new();
        // The latest valid event survives the ordinary tail budget, as in TaskRuntime.
        while slf.entries.len() > 1 &&
            (slf.entries.len() > slf.maximum_count || slf.bytes > slf.maximum_bytes) {
            let event = slf.entries.pop_front().unwrap();
            let bytes = slf.sizes.pop_front().unwrap();
            retired.push(event);
            slf.bytes -= bytes;
            removed += 1;
        }
        // Publish coherent state and release the mutable borrow before Python
        // destructors can reenter a read or append operation.
        drop(slf);
        drop(retired);
        Ok(removed)
    }

    fn tail<'py>(&self, py: Python<'py>, offset: usize) -> PyResult<Bound<'py, PyList>> {
        PyList::new(py, self.entries.iter().skip(offset).map(|event| event.bind(py)))
    }

    // One coherent read under the Python/task read lock. The first event is
    // owned by the returned tuple; no mutable native borrow survives return.
    fn bounds(&self, py: Python<'_>) -> (usize, Option<Py<PyAny>>) {
        (self.entries.len(), self.entries.front().map(|event| event.clone_ref(py)))
    }

    fn __len__(&self) -> usize { self.entries.len() }

    fn __iter__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // Public replay reads hold the task lock. Return an owned snapshot
        // iterator rather than crossing PyO3 once per __getitem__ call.
        // Later append/clear cannot mutate this iterator's membership.
        self.tail(py, 0)?.call_method0("__iter__")
    }

    fn __getitem__(&self, py: Python<'_>, index: isize) -> PyResult<Py<PyAny>> {
        let normalized = if index < 0 { self.entries.len() as isize + index } else { index };
        if normalized < 0 { return Err(PyIndexError::new_err("event index out of range")); }
        self.entries.get(normalized as usize).map(|event| event.clone_ref(py))
            .ok_or_else(|| PyIndexError::new_err("event index out of range"))
    }

    fn clear(slf: PyRefMut<'_, Self>) { Self::__clear__(slf); }

    #[getter]
    fn retained_bytes(&self) -> usize { self.bytes }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for event in &self.entries { visit.call(event)?; }
        Ok(())
    }

    fn __clear__(mut slf: PyRefMut<'_, Self>) {
        let retired = std::mem::take(&mut slf.entries);
        slf.sizes.clear();
        slf.bytes = 0;
        drop(slf);
        drop(retired);
    }
}

#[pymodule]
fn tofu_event_prototype(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ReplayWindow>()
}
