//! CPython semantics the stream parser depends on: `str.strip`, `int()`,
//! truthiness, `json.loads` acceptance, `str()`/`repr()` and the Unicode
//! digit and case-folding rules of `re`. Unicode tables come from the
//! oracle's CPython 3.12 `unicodedata` (Unicode 15.0).

use serde_json::{Map, Value};

use super::errors::{unsupported, Flow};

pub use crate::pyjson::{py_prefix as prefix, truthy};

/// `str.isspace()` for one character.
pub fn space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}

/// `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(space)
}

pub fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// Code points of every Unicode `Nd` zero; each starts a run of ten digits.
const DECIMAL_ZEROS: &[u32] = &[
    0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66,
    0xDE6, 0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90,
    0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
    0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650,
    0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60, 0x16AC0,
    0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E950,
    0x1FBF0,
];

/// The value of a `\d` (Unicode `Nd`) character.
pub fn decimal(ch: char) -> Option<u32> {
    let code = u32::from(ch);
    DECIMAL_ZEROS
        .iter()
        .find(|zero| (**zero..**zero + 10).contains(&code))
        .map(|zero| code - zero)
}

/// `int(digits)` of a non-empty run of `\d` characters; `None` beyond `limit`.
pub fn decimal_value(digits: &[char], limit: i64) -> Option<i64> {
    let mut value: i64 = 0;
    for ch in digits {
        value = value
            .checked_mul(10)?
            .checked_add(i64::from(decimal(*ch)?))?;
        if value > limit {
            return None;
        }
    }
    Some(value)
}

/// `re.IGNORECASE` equality of a text character with an ASCII pattern
/// character (CPython folds U+0130/U+0131 to `i`, U+017F to `s`, U+212A to `k`).
pub fn ci_eq(ch: char, pattern: char) -> bool {
    let pattern = pattern.to_ascii_lowercase();
    ch.to_ascii_lowercase() == pattern
        || matches!(
            (pattern, ch),
            ('i', '\u{130}' | '\u{131}') | ('s', '\u{17f}') | ('k', '\u{212a}')
        )
}

/// Characters CPython's `repr()` prints literally beyond printable ASCII,
/// restricted to ranges that are wholly printable in Unicode 15.0.
fn repr_literal(ch: char) -> bool {
    matches!(u32::from(ch),
        0xA1..=0xAC | 0xAE..=0x2FF | 0x370..=0x377 | 0x391..=0x3A1 | 0x3A3..=0x3FF
        | 0x400..=0x482 | 0x660..=0x669 | 0x6F0..=0x6F9 | 0x2010..=0x2027 | 0x2030..=0x205E | 0x2070..=0x2071
        | 0x2074..=0x208E | 0x2460..=0x24FF | 0x2776..=0x2793 | 0x3001..=0x303F
        | 0x3041..=0x3096 | 0x30A1..=0x30FF | 0x4E00..=0x9FFF | 0xAC00..=0xD7A3
        | 0xFF01..=0xFF60)
}

/// `repr(text)`.
pub fn repr(text: &str) -> Flow<String> {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if ch == quote => {
                out.push('\\');
                out.push(ch);
            }
            ' '..='~' => out.push(ch),
            '\0'..='\u{1f}' | '\u{7f}'..='\u{a0}' | '\u{ad}' => {
                out.push_str(&format!("\\x{:02x}", u32::from(ch)));
            }
            _ if repr_literal(ch) => out.push(ch),
            _ => return unsupported("repr() of text outside the printable whitelist"),
        }
    }
    out.push(quote);
    Ok(out)
}

/// `repr(float)`: the shortest round-trip digits in CPython's layout.
pub fn float_repr(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific.split_once('e').expect("{:e} has an exponent");
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let exponent: i32 = exponent.parse().expect("{:e} exponent is an integer");
    let point = exponent + 1;
    let body = if (-3..=16).contains(&point) {
        let len = digits.len() as i32;
        if point <= 0 {
            format!("0.{}{digits}", "0".repeat((-point) as usize))
        } else if point >= len {
            format!("{digits}{}.0", "0".repeat((point - len) as usize))
        } else {
            format!(
                "{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        }
    } else {
        let tail = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}{tail}e{sign}{:02}", &digits[..1], exponent.abs())
    };
    if value < 0.0 {
        format!("-{body}")
    } else {
        body
    }
}

fn number_repr(number: &serde_json::Number) -> String {
    if number.is_f64() {
        float_repr(number.as_f64().unwrap_or(f64::NAN))
    } else {
        number.to_string()
    }
}

/// `repr()` of a decoded JSON value.
pub fn value_repr(value: &Value) -> Flow<String> {
    match value {
        Value::String(text) => repr(text),
        Value::Array(items) => {
            let parts: Flow<Vec<String>> = items.iter().map(value_repr).collect();
            Ok(format!("[{}]", parts?.join(", ")))
        }
        Value::Object(map) => Ok(format!("{{{}}}", map_repr(map)?)),
        other => str_of(other),
    }
}

fn map_repr(map: &Map<String, Value>) -> Flow<String> {
    let parts: Flow<Vec<String>> = map
        .iter()
        .map(|(key, value)| Ok(format!("{}: {}", repr(key)?, value_repr(value)?)))
        .collect();
    Ok(parts?.join(", "))
}

/// `str()` of a decoded JSON value.
pub fn str_of(value: &Value) -> Flow<String> {
    match value {
        Value::String(text) => Ok(text.clone()),
        Value::Null => Ok("None".into()),
        Value::Bool(true) => Ok("True".into()),
        Value::Bool(false) => Ok("False".into()),
        Value::Number(number) => Ok(number_repr(number)),
        container => value_repr(container),
    }
}

/// Python `==` of decoded JSON values (`True == 1 == 1.0`).
pub fn eq(left: &Value, right: &Value) -> bool {
    let numeric = |value: &Value| match value {
        Value::Bool(flag) => Some(f64::from(u8::from(*flag))),
        Value::Number(number) => number.as_f64(),
        _ => None,
    };
    match (left, right) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| eq(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, x)| b.get(key).is_some_and(|y| eq(x, y)))
        }
        (Value::Number(a), Value::Number(b)) if !a.is_f64() && !b.is_f64() => a == b,
        _ => match (numeric(left), numeric(right)) {
            (Some(a), Some(b)) => a == b,
            _ => left == right,
        },
    }
}

/// Why `int(value)` raised.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IntError {
    Type,
    Value,
    Overflow,
}

/// `int(value)` for a JSON value; inputs whose CPython result this port does
/// not reproduce (arbitrary precision, non-ASCII digits, `1_000`) are
/// `Unsupported`.
pub fn int(value: &Value) -> Flow<Result<i64, IntError>> {
    match value {
        Value::Bool(flag) => Ok(Ok(i64::from(*flag))),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                return Ok(Ok(integer));
            }
            if number.as_u64().is_some() {
                return unsupported("integer beyond i64");
            }
            let float = number.as_f64().unwrap_or(f64::NAN);
            if float.is_nan() {
                return Ok(Err(IntError::Value));
            }
            if float.is_infinite() {
                return Ok(Err(IntError::Overflow));
            }
            if float.abs() >= 9.2e18 {
                return unsupported("float beyond i64");
            }
            Ok(Ok(float.trunc() as i64))
        }
        Value::String(text) => {
            let trimmed = strip(text);
            if !trimmed.is_ascii() || trimmed.contains('_') {
                return unsupported("int() of a non-ASCII or underscored string");
            }
            let digits = trimmed
                .strip_prefix('-')
                .or_else(|| trimmed.strip_prefix('+'))
                .unwrap_or(trimmed);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return Ok(Err(IntError::Value));
            }
            match trimmed.trim_start_matches('+').parse::<i64>() {
                Ok(parsed) => Ok(Ok(parsed)),
                Err(_) => unsupported("integer string beyond i64"),
            }
        }
        Value::Null | Value::Array(_) | Value::Object(_) => Ok(Err(IntError::Type)),
    }
}

/// `int(value)` where TypeError/ValueError are caught by the caller and
/// OverflowError is not.
pub fn int_or_none(value: &Value) -> Flow<Option<i64>> {
    match int(value)? {
        Ok(number) => Ok(Some(number)),
        Err(IntError::Overflow) => unsupported("int() overflow escapes the oracle"),
        Err(_) => Ok(None),
    }
}

/// `json.loads(text)`: `Ok(None)` when CPython also rejects the text.
pub fn loads(text: &str) -> Flow<Option<Value>> {
    match serde_json::from_str::<Value>(text) {
        Ok(value) => Ok(Some(value)),
        Err(error) => {
            if text.contains("NaN")
                || text.contains("Infinity")
                || text.contains("\\ud")
                || text.contains("\\uD")
                || error.to_string().contains("recursion limit")
            {
                return unsupported("JSON accepted by CPython but not serde_json");
            }
            Ok(None)
        }
    }
}

/// Integer-valued JSON numbers only, so `pyjson` spells every number as
/// CPython would.
pub fn require_integral_numbers(value: &Value) -> Flow<()> {
    match value {
        Value::Number(number) if number.as_i64().is_none() && number.as_u64().is_none() => {
            unsupported("float in a re-serialized provider error")
        }
        Value::Array(items) => items.iter().try_for_each(require_integral_numbers),
        Value::Object(map) => map.values().try_for_each(require_integral_numbers),
        _ => Ok(()),
    }
}
