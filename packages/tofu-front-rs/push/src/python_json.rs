//! Python `json.dumps` byte compatibility for the frames Rust itself builds.
//!
//! Python's `/api/push` sends control-lane frames (`ping`, `pong`) with
//! Quart's `websocket.send_json`, i.e. Quart's `DefaultJSONProvider`:
//! `json.dumps(obj, ensure_ascii=True, sort_keys=True)` with the default
//! `', '` / `': '` separators. Data-lane frames arrive from Python already
//! encoded and are never re-encoded here.
//!
//! Numbers keep their JSON literal (`serde_json/arbitrary_precision`) and are
//! rendered the way Python's `json.loads` then `json.dumps` renders them: an
//! integer literal becomes a Python `int`, anything else a `float` printed
//! with `repr` (`1e+16`, `1e-05`, `100.0`, `Infinity` on overflow).

use serde_json::Value;

/// `json.dumps(value, ensure_ascii=True, sort_keys=True)`.
pub fn dumps_sorted_ascii(value: &Value) -> String {
    let mut out = String::new();
    write_value(value, &mut out);
    out
}

fn write_value(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => write_number(&number.to_string(), out),
        Value::String(text) => write_string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_value(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (index, key) in keys.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                write_string(key, out);
                out.push_str(": ");
                write_value(&map[key.as_str()], out);
            }
            out.push('}');
        }
    }
}

fn write_number(literal: &str, out: &mut String) {
    let is_integer = !literal.contains(['.', 'e', 'E']);
    if is_integer {
        let (negative, digits) = match literal.strip_prefix('-') {
            Some(rest) => (true, rest),
            None => (false, literal),
        };
        let digits = digits.trim_start_matches('0');
        if digits.is_empty() {
            out.push('0');
        } else {
            if negative {
                out.push('-');
            }
            out.push_str(digits);
        }
        return;
    }
    match literal.parse::<f64>() {
        Ok(value) => out.push_str(&python_float_repr(value)),
        Err(_) => out.push_str("NaN"),
    }
}

/// `repr(float)` as `json.dumps` prints it (`allow_nan=True`).
pub fn python_float_repr(value: f64) -> String {
    if value.is_nan() {
        return "NaN".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "Infinity" } else { "-Infinity" }.to_string();
    }
    // `{:e}` prints the shortest round-trip digits, as Python's repr does.
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("LowerExp always prints an exponent");
    let exponent: i32 = exponent.parse().expect("LowerExp exponent is an integer");
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let mut out = String::from(sign);
    if !(-4..16).contains(&exponent) {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        out.push('e');
        out.push(if exponent < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exponent.unsigned_abs()));
        return out;
    }
    if exponent < 0 {
        out.push_str("0.");
        for _ in 0..(-exponent - 1) {
            out.push('0');
        }
        out.push_str(&digits);
        return out;
    }
    let point = exponent as usize + 1;
    if digits.len() <= point {
        out.push_str(&digits);
        for _ in digits.len()..point {
            out.push('0');
        }
        out.push_str(".0");
    } else {
        out.push_str(&digits[..point]);
        out.push('.');
        out.push_str(&digits[point..]);
    }
    out
}

fn write_string(text: &str, out: &mut String) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            _ => {
                let mut units = [0u16; 2];
                for unit in character.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dumps(text: &str) -> String {
        dumps_sorted_ascii(&serde_json::from_str(text).unwrap())
    }

    // Expected strings are python3 -c 'import json;print(json.dumps(
    //   json.loads(S), ensure_ascii=True, sort_keys=True))' outputs.
    #[test]
    fn objects_sort_keys_and_use_python_separators() {
        assert_eq!(
            dumps(r#"{"type":"pong","channel":"system","t":1}"#),
            r#"{"channel": "system", "t": 1, "type": "pong"}"#
        );
        assert_eq!(dumps("{}"), "{}");
        assert_eq!(dumps("[1,[2,{}],[]]"), "[1, [2, {}], []]");
    }

    #[test]
    fn numbers_round_trip_like_python() {
        assert_eq!(dumps("1731234567890"), "1731234567890");
        assert_eq!(dumps("-0"), "0");
        assert_eq!(
            dumps("123456789012345678901234567890"),
            "123456789012345678901234567890"
        );
        assert_eq!(dumps("1.0"), "1.0");
        assert_eq!(dumps("1.50"), "1.5");
        assert_eq!(dumps("100e0"), "100.0");
        assert_eq!(dumps("1e16"), "1e+16");
        assert_eq!(dumps("1.5e16"), "1.5e+16");
        assert_eq!(dumps("9999999999999998.0"), "9999999999999998.0");
        assert_eq!(dumps("0.0001"), "0.0001");
        assert_eq!(dumps("0.00001"), "1e-05");
        assert_eq!(dumps("1234.5678"), "1234.5678");
        assert_eq!(dumps("-0.0"), "-0.0");
        assert_eq!(dumps("1e400"), "Infinity");
        assert_eq!(dumps("-1e400"), "-Infinity");
        assert_eq!(dumps("5e-324"), "5e-324");
        assert_eq!(dumps("12345.678e100"), "1.2345678e+104");
        assert_eq!(dumps("4123.25"), "4123.25");
    }

    #[test]
    fn strings_escape_like_ensure_ascii() {
        assert_eq!(dumps(r#""a\"b\\c/d""#), r#""a\"b\\c/d""#);
        assert_eq!(
            dumps(r#""\n\r\t\b\f\u0001\u007f""#),
            r#""\n\r\t\b\f\u0001\u007f""#
        );
        assert_eq!(dumps(r#""é中😀""#), r#""\u00e9\u4e2d\ud83d\ude00""#);
    }
}
