//! Python-compatible JSON helpers.
//!
//! Tofu's wire bytes are produced by CPython's `json` module. Compact
//! insertion-order encoding with `ensure_ascii=False` is exactly what
//! `serde_json` (with `preserve_order`) emits for the values Tofu sends, so the
//! sealed payload uses `serde_json::to_writer`. The two other spellings Tofu
//! uses inside requests are reproduced here: `json.dumps(v,
//! ensure_ascii=False)` with the default `", "`/`": "` separators, and the
//! sorted, ASCII-escaped compact form of `decoded_result_schema`.

use serde_json::{Map, Value};

/// Python truthiness of a JSON value.
pub fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|n| n != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// `str.isspace()` for one character (CPython also counts U+001C..U+001F).
fn py_space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}

/// `not text.strip()`.
pub fn py_blank(text: &str) -> bool {
    text.chars().all(py_space)
}

/// `text.lstrip()`.
pub fn py_lstrip(text: &str) -> &str {
    text.trim_start_matches(py_space)
}

/// Python's `str` slicing of the first `n` characters.
pub fn py_prefix(text: &str, n: usize) -> &str {
    match text.char_indices().nth(n) {
        Some((index, _)) => &text[..index],
        None => text,
    }
}

pub fn compact(value: &Value) -> Vec<u8> {
    serde_json::to_vec(value).expect("JSON values always encode")
}

pub fn compact_string(value: &Value) -> String {
    String::from_utf8(compact(value)).expect("serde_json emits UTF-8")
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
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn number_text(value: &Value) -> String {
    compact_string(value)
}

fn write_value(out: &mut String, value: &Value, spaced: bool, sorted: bool, ascii: bool) {
    let (item_sep, key_sep) = if spaced { (", ", ": ") } else { (",", ":") };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(_) => out.push_str(&number_text(value)),
        Value::String(text) => write_escaped(out, text, ascii),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(item_sep);
                }
                write_value(out, item, spaced, sorted, ascii);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            if sorted {
                keys.sort();
            }
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push_str(item_sep);
                }
                write_escaped(out, key, ascii);
                out.push_str(key_sep);
                write_value(out, &map[key], spaced, sorted, ascii);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(value, ensure_ascii=False)`.
pub fn dumps_spaced(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, true, false, false);
    out
}

/// `json.dumps(value, sort_keys=True, ensure_ascii=False)` (default separators).
pub fn dumps_spaced_sorted(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, true, true, false);
    out
}

/// `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))`.
pub fn dumps_sorted_ascii(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, false, true, true);
    out
}

/// A copy of `map` without one key, keeping the remaining order (`dict.pop`).
pub fn without(map: &Map<String, Value>, key: &str) -> Map<String, Value> {
    map.iter()
        .filter(|(name, _)| name.as_str() != key)
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect()
}

/// Top-level keys that do not start with `_` (`strip_internal_request_fields`).
pub fn public_fields(map: &Map<String, Value>) -> Map<String, Value> {
    map.iter()
        .filter(|(name, _)| !name.starts_with('_'))
        .map(|(name, value)| (name.clone(), value.clone()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn spaced_and_sorted_forms_match_cpython() {
        let value = json!({"b": [1, "x\u{2019}"], "a": {"d": null, "c": true}});
        assert_eq!(
            dumps_spaced(&value),
            r#"{"b": [1, "x’"], "a": {"d": null, "c": true}}"#
        );
        assert_eq!(
            dumps_sorted_ascii(&value),
            r#"{"a":{"c":true,"d":null},"b":[1,"x\u2019"]}"#
        );
        assert_eq!(
            dumps_sorted_ascii(&json!("\u{1F600}")),
            "\"\\ud83d\\ude00\""
        );
    }

    #[test]
    fn python_whitespace_includes_information_separators() {
        assert!(py_blank(" \u{1c}\t\u{3000}"));
        assert!(!py_blank(" a "));
        assert_eq!(py_prefix("añb", 2), "añ");
    }
}
