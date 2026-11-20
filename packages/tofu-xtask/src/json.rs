//! Python-style reads of contract JSON values, so a renderer states the same
//! conditions its contract mapping documents (`truthy` is Python truthiness).

use serde_json::{Map, Value};

pub type Object = Map<String, Value>;

pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|n| n != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

pub fn object<'a>(value: &'a Value, what: &str) -> Result<&'a Object, String> {
    value
        .as_object()
        .ok_or_else(|| format!("{what}: expected an object"))
}

pub fn array<'a>(value: &'a Value, what: &str) -> Result<&'a Vec<Value>, String> {
    value
        .as_array()
        .ok_or_else(|| format!("{what}: expected an array"))
}

pub fn string<'a>(value: &'a Value, what: &str) -> Result<&'a str, String> {
    value
        .as_str()
        .ok_or_else(|| format!("{what}: expected a string"))
}

pub fn boolean(value: &Value, what: &str) -> Result<bool, String> {
    value
        .as_bool()
        .ok_or_else(|| format!("{what}: expected a boolean"))
}

pub fn field<'a>(map: &'a Object, key: &str, what: &str) -> Result<&'a Value, String> {
    map.get(key)
        .ok_or_else(|| format!("{what}: missing {key:?}"))
}

/// An integer as Python's `str(int)` prints it.
pub fn integer(value: &Value, what: &str) -> Result<String, String> {
    if value.is_i64() || value.is_u64() {
        Ok(value.to_string())
    } else {
        Err(format!("{what}: expected an integer"))
    }
}
