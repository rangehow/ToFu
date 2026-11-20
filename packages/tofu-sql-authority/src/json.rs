//! JSON bytes compatible with the Python authority's `orjson` encodings.
//!
//! Receipts, request digests and stored documents are orjson bytes, so the
//! port reproduces orjson's layout exactly: compact separators, raw UTF-8,
//! serde-identical string escapes and orjson's float notation (shortest
//! round-trip digits; fixed notation for decimal exponents -5..=15, otherwise
//! `d.ddde+N` / `d.ddde-N`). `canonical_json` is `OPT_SORT_KEYS`; the plain
//! encoder keeps object insertion order like `orjson.dumps(dict)`.

use serde_json::{Map, Number, Value};

use crate::error::StorageError;

/// Shortest round-trip decimal digits and exponent: `value = 0.d1d2.. * 10^(exponent+1)`
/// expressed as (digits, exponent) where the first digit has weight 10^exponent.
fn shortest_digits(value: f64) -> (String, i32) {
    // Rust's `{:e}` is shortest round-trip: "1.2345e-7", "1e16".
    let formatted = format!("{:e}", value.abs());
    let (mantissa, exponent) = formatted.split_once('e').expect("LowerExp has an exponent");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    (digits, exponent.parse().expect("LowerExp exponent is an integer"))
}

fn fixed_notation(digits: &str, exponent: i32) -> String {
    let count = digits.len() as i32;
    if exponent < 0 {
        let zeros = (-exponent - 1) as usize;
        format!("0.{}{}", "0".repeat(zeros), digits)
    } else if exponent + 1 >= count {
        format!("{}{}.0", digits, "0".repeat((exponent + 1 - count) as usize))
    } else {
        let split = (exponent + 1) as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    }
}

/// orjson's float text.
pub fn orjson_float(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sign = if value < 0.0 { "-" } else { "" };
    let (digits, exponent) = shortest_digits(value);
    if (-5..16).contains(&exponent) {
        return format!("{sign}{}", fixed_notation(&digits, exponent));
    }
    let mantissa = if digits.len() == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
    let exponent_sign = if exponent < 0 { '-' } else { '+' };
    format!("{sign}{mantissa}e{exponent_sign}{}", exponent.abs())
}

/// Python `repr(float)` (also `str(float)`).
pub fn python_float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".into();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if value == 0.0 {
        return if value.is_sign_negative() { "-0.0".into() } else { "0.0".into() };
    }
    let sign = if value < 0.0 { "-" } else { "" };
    let (digits, exponent) = shortest_digits(value);
    if (-4..16).contains(&exponent) {
        return format!("{sign}{}", fixed_notation(&digits, exponent));
    }
    let mantissa = if digits.len() == 1 { digits.clone() } else { format!("{}.{}", &digits[..1], &digits[1..]) };
    let exponent_sign = if exponent < 0 { '-' } else { '+' };
    format!("{sign}{mantissa}e{exponent_sign}{:02}", exponent.abs())
}

fn write_string(out: &mut Vec<u8>, text: &str) {
    serde_json::to_writer(&mut *out, text).expect("string serialization cannot fail");
}

fn write_number(out: &mut Vec<u8>, number: &Number) {
    if let Some(integer) = number.as_i64() {
        out.extend_from_slice(integer.to_string().as_bytes());
    } else if let Some(integer) = number.as_u64() {
        out.extend_from_slice(integer.to_string().as_bytes());
    } else {
        let value = number.as_f64().expect("a JSON number is integral or finite");
        out.extend_from_slice(orjson_float(value).as_bytes());
    }
}

fn write_value(out: &mut Vec<u8>, value: &Value, sort_keys: bool) {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(number) => write_number(out, number),
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_value(out, item, sort_keys);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            out.push(b'{');
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if sort_keys {
                // Python sorts str keys by code point; UTF-8 byte order agrees.
                entries.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
            }
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_string(out, key);
                out.push(b':');
                write_value(out, item, sort_keys);
            }
            out.push(b'}');
        }
    }
}

/// `orjson.dumps(value)`.
pub fn dumps(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    write_value(&mut out, value, false);
    out
}

/// `orjson.dumps(value, option=orjson.OPT_SORT_KEYS)`; also
/// `lib/storage/protocol.py::canonical_json`.
pub fn dumps_sorted(value: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    write_value(&mut out, value, true);
    out
}

pub fn canonical_json(value: &Value) -> Vec<u8> {
    dumps_sorted(value)
}

/// `orjson.loads(bytes)`; malformed input maps to the caller's error.
pub fn loads(bytes: &[u8]) -> Result<Value, serde_json::Error> {
    serde_json::from_slice(bytes)
}

/// Python `str(value)` for JSON-decoded payload values.
pub fn python_str(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => match (number.as_i64(), number.as_u64()) {
            (Some(integer), _) => integer.to_string(),
            (None, Some(integer)) => integer.to_string(),
            _ => python_float_repr(number.as_f64().unwrap_or(0.0)),
        },
        Value::String(text) => text.clone(),
        other => python_repr(other),
    }
}

/// Python `repr()` of JSON-decoded containers (dict/list/str literals).
pub fn python_repr(value: &Value) -> String {
    match value {
        Value::String(text) => python_str_repr(text),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(python_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> =
                map.iter().map(|(key, item)| format!("{}: {}", python_str_repr(key), python_repr(item))).collect();
            format!("{{{}}}", inner.join(", "))
        }
        other => python_str(other),
    }
}

fn python_str_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python truthiness of a JSON value (`if value:`).
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().map(|v| v != 0.0).unwrap_or(true),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// Python `isinstance(value, int)` (bool included, as in Python).
pub fn is_python_int(value: &Value) -> bool {
    matches!(value, Value::Bool(_)) || value.as_i64().is_some() || value.as_u64().is_some()
}

/// `isinstance(value, int) and not isinstance(value, bool)`.
pub fn as_strict_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        _ => None,
    }
}

/// A float JSON value (Python `float`).
pub fn float_value(value: f64) -> Value {
    Number::from_f64(value).map(Value::Number).unwrap_or(Value::Null)
}

pub fn object(entries: Vec<(&str, Value)>) -> Value {
    let mut map = Map::new();
    for (key, value) in entries {
        map.insert(key.to_owned(), value);
    }
    Value::Object(map)
}

/// `validate_finite_json_numbers`: serde_json cannot hold NaN/Inf, so decoded
/// values are finite by construction; kept as an explicit seam for callers
/// that build floats from arithmetic.
pub fn finite(value: f64) -> Result<f64, StorageError> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(StorageError::protocol("Storage values must be finite JSON numbers"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn floats_match_orjson_notation() {
        let cases = [
            (1e16, "1e+16"),
            (1e-7, "1e-7"),
            (0.1, "0.1"),
            (1.0, "1.0"),
            (1.5e300, "1.5e+300"),
            (-0.0, "-0.0"),
            (123456789012345678.0, "1.2345678901234568e+17"),
            (5e-324, "5e-324"),
            (1e15, "1000000000000000.0"),
            (12345.678, "12345.678"),
            (1e-5, "0.00001"),
            (1.5e-5, "0.000015"),
            (1.23e-8, "1.23e-8"),
            (9999999999999998.0, "9999999999999998.0"),
            (2.5e16, "2.5e+16"),
            (1e-6, "1e-6"),
            (100.0, "100.0"),
            (0.0003, "0.0003"),
            (1.7976931348623157e308, "1.7976931348623157e+308"),
        ];
        for (value, expected) in cases {
            assert_eq!(orjson_float(value), expected, "{value:e}");
        }
    }

    #[test]
    fn python_repr_matches_cpython() {
        assert_eq!(python_float_repr(1e16), "1e+16");
        assert_eq!(python_float_repr(1e-5), "1e-05");
        assert_eq!(python_float_repr(0.0001), "0.0001");
        assert_eq!(python_float_repr(1e15), "1000000000000000.0");
        assert_eq!(python_str(&json!(true)), "True");
        assert_eq!(python_repr(&json!({"a": [1, "b'"]})), "{'a': [1, \"b'\"]}");
    }

    #[test]
    fn sorted_and_insertion_order_encodings() {
        let value = json!({"b": 1, "a": {"é": "\u{0}\u{1f}\u{2028}\"\\/"}, "c": []});
        assert_eq!(
            String::from_utf8(dumps_sorted(&value)).unwrap(),
            "{\"a\":{\"é\":\"\\u0000\\u001f\u{2028}\\\"\\\\/\"},\"b\":1,\"c\":[]}"
        );
        assert_eq!(String::from_utf8(dumps(&value)).unwrap().chars().nth(2), Some('b'));
    }
}
