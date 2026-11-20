//! Python value semantics used by the Project Brain port: `int(x or 0)`,
//! `str(x or '')`, `x or ()` iteration, `dict(x)`, `.strip()` and slicing
//! over JSON values decoded from payloads and stored projections.

use serde_json::{Map, Value};

use crate::error::{internal, OpResult};
use crate::json::{python_str, truthy};
use crate::session::SqlValue;

static NULL: Value = Value::Null;

/// Python `int(value)` of a JSON value (TypeError/ValueError are internal).
pub fn int(value: &Value) -> OpResult<i64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                Ok(integer)
            } else if number.is_u64() {
                internal("OverflowError: Python int too large")
            } else {
                Ok(number.as_f64().unwrap_or(0.0).trunc() as i64)
            }
        }
        Value::String(text) => match strip(text).parse::<i64>() {
            Ok(integer) => Ok(integer),
            Err(_) => internal(format!("ValueError: invalid literal for int(): {text:?}")),
        },
        _ => internal("TypeError: int() argument must be a string or a number"),
    }
}

/// `int(value or 0)`.
pub fn int0(value: &Value) -> OpResult<i64> {
    int_or(value, 0)
}

/// `int(value or default)`.
pub fn int_or(value: &Value, default: i64) -> OpResult<i64> {
    if truthy(value) {
        int(value)
    } else {
        Ok(default)
    }
}

/// `str(value or '')`.
pub fn text(value: &Value) -> String {
    text_or(value, "")
}

/// `str(value or default)`.
pub fn text_or(value: &Value, default: &str) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        default.to_owned()
    }
}

/// Python `str.isspace()` characters.
fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `str.rstrip()`.
pub fn rstrip(text: &str) -> &str {
    text.trim_end_matches(is_space)
}

/// `text[:limit]` (code points).
pub fn prefix(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// `mapping.get(key)` on a value already known to be a Mapping (or not:
/// a non-object answers None, as a filtered comprehension would skip it).
pub fn get<'v>(value: &'v Value, key: &str) -> &'v Value {
    value.as_object().and_then(|map| map.get(key)).unwrap_or(&NULL)
}

/// `value.get(key)` where a non-Mapping raises AttributeError.
pub fn attr<'v>(value: &'v Value, key: &str) -> OpResult<&'v Value> {
    match value {
        Value::Object(map) => Ok(map.get(key).unwrap_or(&NULL)),
        _ => internal(format!("AttributeError: object has no attribute 'get' ({key})")),
    }
}

/// `map.get(key)` with a missing key as None.
pub fn field<'v>(map: &'v Map<String, Value>, key: &str) -> &'v Value {
    map.get(key).unwrap_or(&NULL)
}

/// Iterate a present iterable like Python (`for x in value`).
fn iterate(value: &Value) -> OpResult<Vec<Value>> {
    match value {
        Value::Array(items) => Ok(items.clone()),
        Value::Object(map) => Ok(map.keys().map(|key| Value::String(key.clone())).collect()),
        Value::String(text) => Ok(text.chars().map(|c| Value::String(c.to_string())).collect()),
        _ => internal("TypeError: object is not iterable"),
    }
}

/// `value or ()` iteration.
pub fn items(value: &Value) -> OpResult<Vec<Value>> {
    if truthy(value) {
        iterate(value)
    } else {
        Ok(Vec::new())
    }
}

/// `[*mapping.get(key, [])]`: a missing key is empty, a present one must iterate.
pub fn unpack(value: Option<&Value>) -> OpResult<Vec<Value>> {
    match value {
        None => Ok(Vec::new()),
        Some(value) => iterate(value),
    }
}

/// `dict(value or {})`.
pub fn dict(value: &Value) -> OpResult<Map<String, Value>> {
    if !truthy(value) {
        return Ok(Map::new());
    }
    match value {
        Value::Object(map) => Ok(map.clone()),
        _ => internal("TypeError: cannot convert to dict"),
    }
}

/// `[dict(item) for item in value or () if isinstance(item, Mapping)]`.
pub fn mappings(value: &Value) -> OpResult<Vec<Value>> {
    Ok(items(value)?.into_iter().filter(Value::is_object).collect())
}

/// `value == literal` for a str literal.
pub fn is_str(value: &Value, literal: &str) -> bool {
    value.as_str() == Some(literal)
}

/// `value in container` for a str needle.
pub fn contains(container: &Value, needle: &str) -> OpResult<bool> {
    match container {
        Value::Object(map) => Ok(map.contains_key(needle)),
        Value::Array(items) => Ok(items.iter().any(|item| is_str(item, needle))),
        Value::String(text) => Ok(text.contains(needle)),
        _ => internal("TypeError: argument is not iterable"),
    }
}

/// `int(column or 0)` of a SQLite value.
pub fn sql_int0(value: &SqlValue) -> OpResult<i64> {
    if !value.truthy() {
        return Ok(0);
    }
    match value.as_int() {
        Some(integer) => Ok(integer),
        None => internal("ValueError: invalid literal for int()"),
    }
}
