//! Python value semantics shared by the timer, scheduler and worker-job ports
//! (`int()`, `str(x or '')[:n]`, sqlite3 parameter binding, `str.strip()`).

use serde_json::{Map, Value};

use crate::error::{internal, OpResult, StorageError};
use crate::json;
use crate::payload::{self, Payload};
use crate::session::{json_param, Row, SqlValue};

/// Python `int(value)` of a JSON-decoded value; failures are the
/// `TypeError`/`ValueError` the adapter folds to `database_internal`.
pub fn py_int(value: &Value) -> OpResult<i64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                Ok(integer)
            } else if number.as_u64().is_some() {
                internal("Python int too large to convert to SQLite INTEGER")
            } else {
                let float = number.as_f64().unwrap_or(f64::NAN);
                if !float.is_finite() || float.trunc().abs() >= 9.223_372_036_854_776e18 {
                    return internal("int() of float out of range");
                }
                Ok(float.trunc() as i64)
            }
        }
        Value::String(text) => parse_python_int(text),
        _ => internal("int() argument must be a string or a number"),
    }
}

fn parse_python_int(text: &str) -> OpResult<i64> {
    let trimmed = py_strip(text);
    let (sign, digits) = match trimmed.strip_prefix('-') {
        Some(rest) => (-1i128, rest),
        None => (1, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    let bytes = digits.as_bytes();
    let well_formed = !bytes.is_empty()
        && bytes.first().is_some_and(u8::is_ascii_digit)
        && bytes.last().is_some_and(u8::is_ascii_digit)
        && bytes.iter().all(|byte| byte.is_ascii_digit() || *byte == b'_')
        && !digits.contains("__");
    if !well_formed {
        return internal("invalid literal for int()");
    }
    let cleaned: String = digits.chars().filter(|c| *c != '_').collect();
    match cleaned.parse::<i128>().ok().map(|value| value * sign).and_then(|value| i64::try_from(value).ok()) {
        Some(value) => Ok(value),
        None => internal("Python int too large to convert to SQLite INTEGER"),
    }
}

/// `int(payload.get(key, default) or default)`.
pub fn int_or(payload: &Payload, key: &str, default: i64) -> OpResult<i64> {
    match payload.get(key) {
        Some(value) if json::truthy(value) => py_int(value),
        _ => Ok(default),
    }
}

/// `int(payload.get(key, default))`.
pub fn int_get(payload: &Payload, key: &str, default: i64) -> OpResult<i64> {
    match payload.get(key) {
        Some(value) => py_int(value),
        None => Ok(default),
    }
}

/// `str(payload.get(key) or fallback)[:maximum]`.
pub fn text_or(payload: &Payload, key: &str, fallback: &str, maximum: Option<usize>) -> String {
    let value = payload::get(payload, key);
    let text = if json::truthy(value) { json::python_str(value) } else { fallback.to_owned() };
    match maximum {
        Some(maximum) => truncate(&text, maximum),
        None => text,
    }
}

/// Python `text[:maximum]` (code points).
pub fn truncate(text: &str, maximum: usize) -> String {
    match text.char_indices().nth(maximum) {
        Some((index, _)) => text[..index].to_owned(),
        None => text.to_owned(),
    }
}

fn py_is_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python `str.strip()`.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_is_space)
}

/// Bind a payload value as Python's sqlite3 would; containers are the
/// `sqlite3.ProgrammingError` that `_map_sqlite_error` reports generically.
pub fn bind(value: &Value) -> OpResult<SqlValue> {
    match value {
        Value::Array(_) | Value::Object(_) => {
            Err(StorageError::new("database_internal", "SQLite operation failed").into())
        }
        other => json_param(other),
    }
}

/// `payload.get(key, default)` bound as a parameter.
pub fn bind_get(payload: &Payload, key: &str, default: &str) -> OpResult<SqlValue> {
    match payload.get(key) {
        Some(value) => bind(value),
        None => Ok(SqlValue::Text(default.to_owned())),
    }
}

/// `int(value or 0)` of a stored column.
pub fn column_int_or_zero(value: &SqlValue) -> OpResult<i64> {
    if !value.truthy() {
        return Ok(0);
    }
    match value.as_int() {
        Some(integer) => Ok(integer),
        None => internal("int() of stored column"),
    }
}

/// Python `str(value)` of a stored column.
pub fn column_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(bytes) => format!("b{:?}", String::from_utf8_lossy(bytes)),
    }
}

/// Python `str(value or '')` of a stored column.
pub fn column_text(value: &SqlValue) -> String {
    if value.truthy() {
        column_str(value)
    } else {
        String::new()
    }
}

/// `_load(value) or {}`.
pub fn load_or_empty(value: &SqlValue) -> OpResult<Value> {
    let loaded = crate::codec::load(value)?;
    Ok(if json::truthy(&loaded) { loaded } else { Value::Object(Map::new()) })
}

/// `{column: row[column] for column in columns}` with the listed columns
/// replaced by `int(value or 0)` and `tools_config` by `_load(...) or {}`.
pub fn document(row: &Row, columns: &[&str], numeric: &[&str]) -> OpResult<Value> {
    let mut map = Map::new();
    for column in columns {
        let value = row.require(column)?;
        let item = if *column == "tools_config" {
            load_or_empty(value)?
        } else if numeric.contains(column) {
            Value::from(column_int_or_zero(value)?)
        } else {
            value.to_json()?
        };
        map.insert((*column).to_owned(), item);
    }
    Ok(Value::Object(map))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_int_conversion() {
        assert_eq!(py_int(&json!(" 1_000 ")).unwrap(), 1000);
        assert_eq!(py_int(&json!(-2.9)).unwrap(), -2);
        assert_eq!(py_int(&json!(true)).unwrap(), 1);
        assert!(py_int(&json!("1.5")).is_err());
        assert!(py_int(&json!("1__0")).is_err());
        assert!(py_int(&Value::Null).is_err());
    }

    #[test]
    fn truncation_counts_code_points() {
        assert_eq!(truncate("héllo", 2), "hé");
        assert_eq!(truncate("ab", 5), "ab");
        assert_eq!(py_strip("\u{1c} x \u{a0}"), "x");
    }
}
