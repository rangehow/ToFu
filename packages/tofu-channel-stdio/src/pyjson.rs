//! Python's `json` semantics where the binding depends on them: `json.loads` of
//! one line (strict UTF-8, integers of any size, float overflow to infinity,
//! Python 3.12's 4300-digit integer limit), `json.dumps(ensure_ascii=False)`
//! bytes (`", "` and `": "` separators, `float.__repr__`), and `str()`/`repr()`
//! of decoded values for the messages Python builds from them.

use serde_json::{Map, Number, Value};

/// `sys.int_info.default_max_str_digits`: `int()` of a longer decimal raises ValueError.
const PYTHON_INT_MAX_STR_DIGITS: usize = 4300;

/// `json.loads(raw.decode('utf-8'))`, or `None` where Python raises
/// `UnicodeDecodeError` or `ValueError`.
pub fn loads(raw: &[u8]) -> Option<Value> {
    let text = std::str::from_utf8(raw).ok()?;
    let value: Value = serde_json::from_str(text).ok()?;
    if exceeds_int_digits(&value) {
        return None;
    }
    Some(value)
}

fn exceeds_int_digits(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            let token = number.to_string();
            is_integer_token(&token)
                && token.trim_start_matches('-').len() > PYTHON_INT_MAX_STR_DIGITS
        }
        Value::Array(items) => items.iter().any(exceeds_int_digits),
        Value::Object(map) => map.values().any(exceeds_int_digits),
        _ => false,
    }
}

fn is_integer_token(token: &str) -> bool {
    !token.contains(['.', 'e', 'E'])
}

/// `json.dumps(value, ensure_ascii=False)`.
pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value);
    out
}

fn write_value(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&json_number(number)),
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_string(out, key);
                out.push_str(": ");
                write_value(out, item);
            }
            out.push('}');
        }
    }
}

fn write_string(out: &mut String, text: &str) {
    // serde_json escapes exactly what ensure_ascii=False escapes: '"', '\\', \b \f \n \r \t
    // and other controls below 0x20 as lowercase \u00xx.
    out.push_str(&serde_json::to_string(text).expect("a str always serializes"));
}

enum PyNumber {
    Int(String),
    Float(f64),
}

fn py_number(number: &Number) -> PyNumber {
    let token = number.to_string();
    if is_integer_token(&token) {
        if token
            .trim_start_matches('-')
            .bytes()
            .all(|byte| byte == b'0')
        {
            return PyNumber::Int("0".to_string());
        }
        return PyNumber::Int(token);
    }
    PyNumber::Float(
        token
            .parse::<f64>()
            .expect("a JSON number token parses as f64"),
    )
}

fn json_number(number: &Number) -> String {
    match py_number(number) {
        PyNumber::Int(digits) => digits,
        PyNumber::Float(value) if value.is_nan() => "NaN".to_string(),
        PyNumber::Float(value) if value.is_infinite() => {
            if value > 0.0 { "Infinity" } else { "-Infinity" }.to_string()
        }
        PyNumber::Float(value) => float_repr(value),
    }
}

/// `float.__repr__` of a finite value: the shortest round-trip digits, fixed
/// notation for decimal exponents -4..=15, else `d.ddde±XX`.
pub fn float_repr(value: f64) -> String {
    let scientific = format!("{value:e}");
    let (negative, body) = match scientific.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, scientific.as_str()),
    };
    let (mantissa, exponent) = body.split_once('e').expect("{:e} has an exponent");
    let exponent: i32 = exponent.parse().expect("{:e} exponent is an integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let mut out = String::new();
    if negative {
        out.push('-');
    }
    if !(-4..16).contains(&exponent) {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if exponent < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exponent.abs()));
        return out;
    }
    let point = exponent + 1;
    if point <= 0 {
        out.push_str("0.");
        out.push_str(&"0".repeat((-point) as usize));
        out.push_str(&digits);
    } else if digits.len() <= point as usize {
        out.push_str(&digits);
        out.push_str(&"0".repeat(point as usize - digits.len()));
        out.push_str(".0");
    } else {
        out.push_str(&digits[..point as usize]);
        out.push('.');
        out.push_str(&digits[point as usize..]);
    }
    out
}

/// Python `str(value)` of a decoded JSON value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => py_repr(other),
    }
}

/// Python `repr(value)` of a decoded JSON value.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => match py_number(number) {
            PyNumber::Int(digits) => digits,
            PyNumber::Float(value) if value.is_nan() => "nan".to_string(),
            PyNumber::Float(value) if value.is_infinite() => {
                if value > 0.0 { "inf" } else { "-inf" }.to_string()
            }
            PyNumber::Float(value) => float_repr(value),
        },
        Value::String(text) => py_repr_str(text),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!("{{{}}}", repr_items(map)),
    }
}

fn repr_items(map: &Map<String, Value>) -> String {
    map.iter()
        .map(|(key, item)| format!("{}: {}", py_repr_str(key), py_repr(item)))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Python `repr(text)`: single quotes unless the text holds `'` and no `"`.
/// Printability follows Python for ASCII and Latin-1; beyond that only
/// controls and Unicode whitespace are escaped (Python also escapes the
/// unassigned, private-use and format categories).
pub fn py_repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
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
            ' ' => out.push(' '),
            c if (c as u32) < 0x20 || (0x7f..=0xa0).contains(&(c as u32)) || c == '\u{ad}' => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c if c.is_control() || c.is_whitespace() => {
                let code = c as u32;
                if code <= 0xffff {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `str.isspace()` of one character, for `str.strip()`.
pub fn py_isspace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python truthiness of a decoded JSON value.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => match py_number(number) {
            PyNumber::Int(digits) => digits != "0",
            PyNumber::Float(value) => value != 0.0,
        },
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// Python `int(value)` for a truthy decoded value, or `None` where Python
/// raises (or where the result leaves i64).
pub fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(number) => match py_number(number) {
            PyNumber::Int(digits) => digits.parse().ok(),
            PyNumber::Float(value) if value.is_finite() => {
                let truncated = value.trunc();
                if truncated >= i64::MIN as f64 && truncated < i64::MAX as f64 {
                    Some(truncated as i64)
                } else {
                    None
                }
            }
            PyNumber::Float(_) => None,
        },
        Value::String(text) => parse_int_literal(text.trim_matches(py_isspace)),
        _ => None,
    }
}

/// `int(text)` in base 10: optional sign, ASCII digits, single underscores between digits.
fn parse_int_literal(text: &str) -> Option<i64> {
    let (sign, digits) = match text.as_bytes().first() {
        Some(b'-') => (-1, &text[1..]),
        Some(b'+') => (1, &text[1..]),
        _ => (1, text),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return None;
    }
    if !digits
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'_')
    {
        return None;
    }
    let cleaned: String = digits.chars().filter(|c| *c != '_').collect();
    cleaned
        .parse::<i64>()
        .ok()
        .map(|magnitude| sign * magnitude)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn floats_print_like_python_repr() {
        let cases = [
            (1.0, "1.0"),
            (1.5, "1.5"),
            (-0.0, "-0.0"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.2345e20, "1.2345e+20"),
            (123.456, "123.456"),
            (0.1, "0.1"),
            (1e300, "1e+300"),
            (5e-324, "5e-324"),
        ];
        for (value, expected) in cases {
            assert_eq!(float_repr(value), expected, "{value}");
        }
    }

    #[test]
    fn dumps_uses_python_separators_and_keeps_non_ascii() {
        let value = loads(
            "{\"b\": [1, 2.50, -0, 1e400, {}], \"a\": \"é\\n\\u0001\", \"c\": []}".as_bytes(),
        )
        .unwrap();
        assert_eq!(
            dumps(&value),
            "{\"b\": [1, 2.5, 0, Infinity, {}], \"a\": \"é\\n\\u0001\", \"c\": []}"
        );
        let big = "1".repeat(40);
        assert_eq!(dumps(&loads(big.as_bytes()).unwrap()), big);
    }

    #[test]
    fn loads_refuses_what_python_refuses() {
        assert!(loads(b"not json").is_none());
        assert!(loads(b"").is_none());
        assert!(loads(b"\xff{}").is_none());
        assert!(loads("1".repeat(4301).as_bytes()).is_none());
        assert!(loads("1".repeat(4300).as_bytes()).is_some());
        assert!(loads(b" {\"a\": 1}\n").is_some());
    }

    #[test]
    fn repr_and_int_follow_python() {
        assert_eq!(py_repr_str("nope"), "'nope'");
        assert_eq!(py_repr_str("it's"), "\"it's\"");
        assert_eq!(py_repr_str("a'\"b"), "'a\\'\"b'");
        assert_eq!(py_repr_str("t\u{7}\u{a0}é"), "'t\\x07\\xa0é'");
        assert_eq!(py_str(&json!(null)), "None");
        assert_eq!(py_str(&json!({"a": [true, 1.0]})), "{'a': [True, 1.0]}");
        assert_eq!(py_int(&json!(" -1_000 ")), Some(-1000));
        assert_eq!(py_int(&json!("1__0")), None);
        assert_eq!(py_int(&json!(3.9)), Some(3));
        assert_eq!(py_int(&json!(true)), Some(1));
        assert_eq!(py_int(&json!([1])), None);
        assert!(!truthy(&json!(0.0)) && !truthy(&json!("")) && truthy(&json!([0])));
    }
}
