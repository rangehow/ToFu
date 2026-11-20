//! Python value semantics the core handlers rely on (`int(x or 0)`,
//! `str(x or '')`, `text[:n]`, `dict.get`) over JSON-decoded values.

use serde_json::{Map, Value};

use crate::error::{internal, OpResult};
use crate::json::{python_str, truthy};

static NULL: Value = Value::Null;

/// `mapping.get(key)` (missing and non-mapping receivers read as `None`
/// only where the caller has already proven a mapping).
pub fn get<'v>(value: &'v Value, key: &str) -> &'v Value {
    match value {
        Value::Object(map) => map.get(key).unwrap_or(&NULL),
        _ => &NULL,
    }
}

pub fn map_get<'v>(map: &'v Map<String, Value>, key: &str) -> &'v Value {
    map.get(key).unwrap_or(&NULL)
}

/// `value.get(key)` where `value` must be a mapping: anything else is the
/// Python `AttributeError` a handler lets escape as an internal failure.
pub fn attr_get<'v>(value: &'v Value, key: &str) -> OpResult<&'v Value> {
    match value {
        Value::Object(map) => Ok(map.get(key).unwrap_or(&NULL)),
        _ => internal("AttributeError: object has no attribute 'get'"),
    }
}

/// Python `int(text)` for a `str` argument.
fn parse_python_int(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    let (negative, digits) = match trimmed.as_bytes().first() {
        Some(b'-') => (true, &trimmed[1..]),
        Some(b'+') => (false, &trimmed[1..]),
        _ => (false, trimmed),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    if !digits.bytes().all(|byte| byte.is_ascii_digit() || byte == b'_') {
        return None;
    }
    let cleaned: String = digits.chars().filter(|c| *c != '_').collect();
    let magnitude: i128 = cleaned.parse().ok()?;
    let value = if negative { -magnitude } else { magnitude };
    i64::try_from(value).ok()
}

/// Python `int(value)`; `Err` is the TypeError/ValueError/OverflowError class.
pub fn int(value: &Value) -> Result<i64, ()> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                return Ok(integer);
            }
            if number.as_u64().is_some() {
                return Err(());
            }
            let float = number.as_f64().ok_or(())?;
            if !float.is_finite() {
                return Err(());
            }
            let truncated = float.trunc();
            if truncated < i64::MIN as f64 || truncated >= i64::MAX as f64 {
                return Err(());
            }
            Ok(truncated as i64)
        }
        Value::String(text) => parse_python_int(text).ok_or(()),
        _ => Err(()),
    }
}

/// Python `int(value or 0)`.
pub fn int_or_zero(value: &Value) -> Result<i64, ()> {
    if truthy(value) {
        int(value)
    } else {
        Ok(0)
    }
}

/// `int(value or 0)` whose failure escapes the handler.
pub fn int_or_zero_strict(value: &Value) -> OpResult<i64> {
    match int_or_zero(value) {
        Ok(value) => Ok(value),
        Err(()) => internal("ValueError: invalid literal for int()"),
    }
}

/// Python `str(value or '')`.
pub fn str_or_empty(value: &Value) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        String::new()
    }
}

/// Python `text[:limit]` (code points).
pub fn truncate(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// `isinstance(value, int) and not isinstance(value, bool)`.
pub fn strict_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        _ => None,
    }
}

/// `value == text` for a decoded value and a Python `str`.
pub fn eq_str(value: &Value, text: &str) -> bool {
    matches!(value, Value::String(candidate) if candidate == text)
}

/// `value in {...}` raises `TypeError: unhashable type` for list/dict.
pub fn require_hashable(value: &Value) -> OpResult<()> {
    match value {
        Value::Array(_) | Value::Object(_) => internal("TypeError: unhashable type"),
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_int_conversions() {
        assert_eq!(int(&json!(" 12 ")), Ok(12));
        assert_eq!(int(&json!("1_000")), Ok(1000));
        assert_eq!(int(&json!("1.5")), Err(()));
        assert_eq!(int(&json!(2.9)), Ok(2));
        assert_eq!(int(&json!(-2.9)), Ok(-2));
        assert_eq!(int(&json!(true)), Ok(1));
        assert_eq!(int(&json!([])), Err(()));
        assert_eq!(int_or_zero(&json!("")), Ok(0));
        assert_eq!(int_or_zero(&Value::Null), Ok(0));
    }
}
