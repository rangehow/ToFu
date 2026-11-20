//! Python `json.dumps(value, separators=(',', ':'))` byte for byte: ASCII
//! output with `\uXXXX` escapes and `repr(float)` numbers, keys in the
//! map's (insertion) order. The Home digests and stores these texts.

use serde_json::Value;

pub fn dumps(value: &Value) -> String {
    let mut out = String::new();
    write(value, &mut out);
    out
}

fn write(value: &Value, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => {
            if number.is_f64() {
                out.push_str(&float_repr(number.as_f64().unwrap_or(0.0)));
            } else {
                out.push_str(&number.to_string());
            }
        }
        Value::String(text) => string(text, out),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write(item, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                string(key, out);
                out.push(':');
                write(item, out);
            }
            out.push('}');
        }
    }
}

fn string(text: &str, out: &mut String) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// `repr(float)`: shortest round-trip digits; positional for decimal
/// exponents in [-4, 16), else `d.ddde+XX`.
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "NaN".into();
    }
    if value.is_infinite() {
        return if value > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        let text = if point <= 0 {
            format!("0.{}{}", "0".repeat((-point) as usize), digits)
        } else if point as usize >= digits.len() {
            format!("{}{}.0", digits, "0".repeat(point as usize - digits.len()))
        } else {
            format!(
                "{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        };
        format!("{sign}{text}")
    } else {
        let tail = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits.clone()
        };
        let exponent_sign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{tail}e{exponent_sign}{:02}", exponent.abs())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_json_dumps() {
        assert_eq!(float_repr(72.0), "72.0");
        assert_eq!(float_repr(0.0), "0.0");
        assert_eq!(float_repr(1e-5), "1e-05");
        assert_eq!(float_repr(0.0001), "0.0001");
        assert_eq!(float_repr(1e16), "1e+16");
        assert_eq!(float_repr(123.456), "123.456");
        assert_eq!(float_repr(-2.5e-7), "-2.5e-07");
        assert_eq!(float_repr(1234567890123456.0), "1234567890123456.0");
        let value = json!({"a": "é\u{1F600}\"\\\n\u{7f}", "b": [1, 2.5, null, true]});
        assert_eq!(
            dumps(&value),
            r#"{"a":"\u00e9\ud83d\ude00\"\\\n\u007f","b":[1,2.5,null,true]}"#
        );
    }
}
