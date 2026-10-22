//! Read-only finite JSON-carrier checks for an owned CPython graph.
//!
//! Entry: all_finite. Dependencies: CPython stable ABI via PyO3. Holds the GIL
//! throughout borrowed-pointer traversal; calls no Python hooks and retains no
//! Python references or global cache. Stack and active set grow with depth,
//! not array width. This experimental module does not select a JSON decoder.
use pyo3::{ffi, prelude::*};
use std::collections::{HashSet, TryReserveError};
use pyo3::exceptions::PyMemoryError;

enum Frame {
    Value(*mut ffi::PyObject),
    List { object: *mut ffi::PyObject, index: isize, length: isize },
    Dict { object: *mut ffi::PyObject, position: isize },
}

/// Safety: root and descendants stay alive under the caller's root reference.
/// Exact builtins, the held GIL, and absence of Python callbacks prevent graph
/// mutation while traversing. CPython accessors return borrowed references;
/// every pointer remains call-local and is never dereferenced after return.
unsafe fn finite_graph(root: *mut ffi::PyObject) -> Result<bool, TryReserveError> {
    let mut frames = Vec::new();
    frames.try_reserve(16)?;
    let mut active = HashSet::new();
    frames.push(Frame::Value(root));
    while let Some(frame) = frames.pop() {
        match frame {
            Frame::Value(object) => {
                if ffi::PyFloat_CheckExact(object) != 0 {
                    if !ffi::PyFloat_AsDouble(object).is_finite() { return Ok(false); }
                } else if ffi::PyLong_CheckExact(object) != 0
                    || ffi::PyUnicode_CheckExact(object) != 0
                    || object == ffi::Py_None()
                    || object == ffi::Py_True()
                    || object == ffi::Py_False() {
                    // Arbitrary-size integers stay untouched; no conversion.
                } else if ffi::PyList_CheckExact(object) != 0 {
                    active.try_reserve(1)?;
                    if !active.insert(object as usize) { return Ok(false); }
                    frames.push(Frame::List { object, index: 0, length: ffi::PyList_Size(object) });
                } else if ffi::PyDict_CheckExact(object) != 0 {
                    active.try_reserve(1)?;
                    if !active.insert(object as usize) { return Ok(false); }
                    frames.push(Frame::Dict { object, position: 0 });
                } else {
                    // Subclasses/opaque carriers remain on the canonical port.
                    return Ok(false);
                }
            }
            Frame::List { object, index, length } => {
                if index < length {
                    frames.try_reserve(2)?;
                    let child = ffi::PyList_GetItem(object, index);
                    frames.push(Frame::List { object, index: index + 1, length });
                    frames.push(Frame::Value(child));
                } else {
                    active.remove(&(object as usize));
                }
            }
            Frame::Dict { object, mut position } => {
                let mut key = std::ptr::null_mut();
                let mut child = std::ptr::null_mut();
                if ffi::PyDict_Next(object, &mut position, &mut key, &mut child) != 0 {
                    if ffi::PyUnicode_CheckExact(key) == 0 { return Ok(false); }
                    frames.try_reserve(2)?;
                    frames.push(Frame::Dict { object, position });
                    frames.push(Frame::Value(child));
                } else {
                    active.remove(&(object as usize));
                }
            }
        }
    }
    Ok(true)
}

#[pyfunction]
fn all_finite(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    // A Bound argument owns the root's lifetime for the whole held-GIL call.
    unsafe { finite_graph(value.as_ptr()) }
        .map_err(|_| PyMemoryError::new_err("native finite traversal allocation failed"))
}


/// Check numeric float tokens in a previously syntax-validated JSON string.
/// Checking the source preserves overflow evidence discarded by duplicate keys.
/// String payloads are skipped, including escaped quotes and backslashes.
#[pyfunction]
fn float_tokens_finite(value: &str) -> bool {
    let bytes = value.as_bytes();
    let mut position = 0;
    while position < bytes.len() {
        if bytes[position] == b'"' {
            position += 1;
            loop {
                let Some(offset) = value[position..].find('"') else { return false; };
                let quote = position + offset;
                let mut before = quote;
                while before > 0 && bytes[before - 1] == b'\\' { before -= 1; }
                position = quote + 1;
                if (quote - before) % 2 == 0 { break; }
            }
        } else if bytes[position] == b'-' || bytes[position].is_ascii_digit() {
            let start = position;
            let mut floating = false;
            while position < bytes.len() {
                let byte = bytes[position];
                if byte.is_ascii_digit() || byte == b'-' || byte == b'+' {
                    position += 1;
                } else if byte == b'.' || byte == b'e' || byte == b'E' {
                    floating = true;
                    position += 1;
                } else { break; }
            }
            if floating {
                match value[start..position].parse::<f64>() {
                    Ok(number) if number.is_finite() => (),
                    _ => return false,
                }
            }
        } else { position += 1; }
    }
    true
}

#[pymodule]
fn tofu_json_finite(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(all_finite, module)?)?;
    module.add_function(wrap_pyfunction!(float_tokens_finite, module)?)?;
    Ok(())
}
