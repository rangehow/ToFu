//! CPython `json.dumps` spellings used at the MCP boundary.
//!
//! Digests (`schema_hash`, catalog fingerprints), relay frames and the
//! structured-result text are produced by CPython's `json` module in the
//! Python oracle; these writers reproduce its bytes: separators, `sort_keys`,
//! `ensure_ascii` and `repr(float)`.

use serde_json::Value;

#[derive(Clone, Copy)]
pub struct Style {
    pub spaced: bool,
    pub sorted: bool,
    pub ascii: bool,
}

/// `json.dumps(value, ensure_ascii=False)`.
pub const SPACED: Style = Style {
    spaced: true,
    sorted: false,
    ascii: false,
};
/// `json.dumps(value, ensure_ascii=False, sort_keys=True)`.
pub const SPACED_SORTED: Style = Style {
    spaced: true,
    sorted: true,
    ascii: false,
};
/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))`.
pub const COMPACT_SORTED: Style = Style {
    spaced: false,
    sorted: true,
    ascii: false,
};
/// `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))`.
pub const COMPACT_SORTED_ASCII: Style = Style {
    spaced: false,
    sorted: true,
    ascii: true,
};
/// `json.dumps(value, separators=(',', ':'))` (ASCII-escaped, insertion order).
pub const COMPACT_ASCII: Style = Style {
    spaced: false,
    sorted: false,
    ascii: true,
};

pub fn dumps(value: &Value, style: Style) -> String {
    let mut out = String::new();
    write_value(&mut out, value, style);
    out
}

fn write_escaped(out: &mut String, text: &str, ascii: bool) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && (c as u32) > 0x7f => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `repr(float)`: shortest round-trip digits, scientific below 1e-4 or from 1e16.
pub fn float_repr(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0".into()
        } else {
            "0.0".into()
        };
    }
    let sci = format!("{value:e}");
    let (mantissa, exponent) = sci.split_once('e').expect("{:e} always has an exponent");
    let exponent: i32 = exponent.parse().expect("integer exponent");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        let body = if point <= 0 {
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
        format!("{sign}{body}")
    } else {
        let tail = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        let esign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{}{tail}e{esign}{:02}", &digits[..1], exponent.abs())
    }
}

fn write_number(out: &mut String, number: &serde_json::Number) {
    if let Some(float) = number.as_f64().filter(|_| number.is_f64()) {
        out.push_str(&float_repr(float));
    } else {
        out.push_str(&number.to_string());
    }
}

fn write_value(out: &mut String, value: &Value, style: Style) {
    let (item_sep, key_sep) = if style.spaced {
        (", ", ": ")
    } else {
        (",", ":")
    };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => write_number(out, number),
        Value::String(text) => write_escaped(out, text, style.ascii),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(item_sep);
                }
                write_value(out, item, style);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            if style.sorted {
                keys.sort();
            }
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(item_sep);
                }
                write_escaped(out, key, style.ascii);
                out.push_str(key_sep);
                write_value(out, &map[key], style);
            }
            out.push('}');
        }
    }
}

/// `str.isspace()` for one character (CPython also counts U+001C..U+001F).
pub fn py_space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}

/// `str.strip()`.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_space)
}

/// Python `len(str)`.
pub fn py_len(text: &str) -> usize {
    text.chars().count()
}

/// Python `text[:n]`.
pub fn py_prefix(text: &str, n: usize) -> &str {
    match text.char_indices().nth(n) {
        Some((index, _)) => &text[..index],
        None => text,
    }
}

/// `f'{n:,}'`.
pub fn thousands(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

/// Python `repr(str)` for the common case (single quotes unless the text holds one).
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn forms_match_cpython() {
        let value = json!({"b": [1, "x\u{2019}", 1.5], "a": {"d": null, "c": true}});
        assert_eq!(
            dumps(&value, SPACED),
            r#"{"b": [1, "x’", 1.5], "a": {"d": null, "c": true}}"#
        );
        assert_eq!(
            dumps(&value, COMPACT_SORTED_ASCII),
            r#"{"a":{"c":true,"d":null},"b":[1,"x\u2019",1.5]}"#
        );
        assert_eq!(
            dumps(&json!("\u{1F600}"), COMPACT_SORTED_ASCII),
            "\"\\ud83d\\ude00\""
        );
    }

    #[test]
    fn float_repr_matches_cpython() {
        for (value, text) in [
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (123456789012345.6, "123456789012345.6"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (-2.5, "-2.5"),
            (1e100, "1e+100"),
        ] {
            assert_eq!(float_repr(value), text);
        }
    }

    #[test]
    fn helpers() {
        assert_eq!(thousands(1234567), "1,234,567");
        assert_eq!(thousands(12), "12");
        assert_eq!(py_repr("x"), "'x'");
        assert_eq!(py_repr("it's"), "\"it's\"");
        assert_eq!(py_prefix("añb", 2), "añ");
        assert_eq!(py_strip(" \u{1c}a\t"), "a");
    }
}
