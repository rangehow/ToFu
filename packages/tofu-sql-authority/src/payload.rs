//! Shared payload validators (`operations_pkg/_common.py`).

use serde_json::{Map, Value};

use crate::error::{OpResult, StorageError};

pub type Payload = Map<String, Value>;

fn invalid(key: &str) -> StorageError {
    StorageError::protocol(format!("Invalid {key} in storage request"))
}

/// `payload.get(key)` with Python's missing-is-None semantics.
pub fn get<'p>(payload: &'p Payload, key: &str) -> &'p Value {
    static NULL: Value = Value::Null;
    payload.get(key).unwrap_or(&NULL)
}

/// Python `len(str)` counts code points.
pub fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// `_required_text(payload, key, maximum=512)`.
pub fn required_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        Some(Value::String(text)) if !text.is_empty() && char_len(text) <= maximum => Ok(text.clone()),
        _ => Err(invalid(key).into()),
    }
}

pub fn required_text_512(payload: &Payload, key: &str) -> OpResult<String> {
    required_text(payload, key, 512)
}

/// `_integer(payload, key, default=, minimum=, maximum=)`.
pub fn integer(
    payload: &Payload,
    key: &str,
    default: Option<i64>,
    minimum: Option<i64>,
    maximum: Option<i64>,
) -> OpResult<i64> {
    let value = match payload.get(key) {
        Some(Value::Number(number)) => number.as_i64(),
        Some(_) => None,
        None => default,
    };
    let Some(value) = value else { return Err(invalid(key).into()) };
    if minimum.is_some_and(|minimum| value < minimum) || maximum.is_some_and(|maximum| value > maximum) {
        return Err(invalid(key).into());
    }
    Ok(value)
}

/// `_number(payload, key, minimum=, maximum=)`.
pub fn number(payload: &Payload, key: &str, minimum: f64, maximum: f64) -> OpResult<f64> {
    match payload.get(key) {
        Some(Value::Number(number)) => {
            let value = number.as_f64().unwrap_or(f64::NAN);
            if minimum <= value && value <= maximum {
                Ok(value)
            } else {
                Err(invalid(key).into())
            }
        }
        _ => Err(invalid(key).into()),
    }
}

/// `_expected_version(payload)`.
pub fn expected_version(payload: &Payload) -> OpResult<Option<i64>> {
    if !payload.contains_key("expected_version") {
        return Ok(None);
    }
    integer(payload, "expected_version", None, Some(0), None).map(Some)
}

/// `payload.get(key) is True`.
pub fn is_true(payload: &Payload, key: &str) -> bool {
    payload.get(key) == Some(&Value::Bool(true))
}

/// `str(payload.get(key) or '')`.
pub fn text_or_empty(payload: &Payload, key: &str) -> String {
    let value = get(payload, key);
    if crate::json::truthy(value) {
        crate::json::python_str(value)
    } else {
        String::new()
    }
}

/// `payload.get(key)` when it is a `str`.
pub fn opt_str<'p>(payload: &'p Payload, key: &str) -> Option<&'p str> {
    payload.get(key).and_then(Value::as_str)
}

/// The payload must be a JSON object (`isinstance(payload, Mapping)`).
pub fn object(value: &Value) -> Option<&Payload> {
    value.as_object()
}
