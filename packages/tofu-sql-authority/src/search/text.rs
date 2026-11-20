//! Python text semantics the turn-search port relies on: `str.lower()`,
//! `str.split()`/`strip()`, code-point slicing and `str.find`, the
//! `json.dumps(..., separators=(',', ':'))` ASCII encoder of search cursors,
//! `base64.b64decode(altchars=b'-_', validate=True)`, and
//! `lib/conversations/search_index.py::build_search_text`.

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use serde_json::Value;

use crate::ops::conversations::support::py_isspace;

/// Python `str.lower()` (full lowercase mapping with Final_Sigma).
pub fn lower(text: &str) -> String {
    text.to_lowercase()
}

/// `len(char.lower())` for one code point.
pub fn lower_len(character: char) -> usize {
    character.to_lowercase().count()
}

pub fn is_space(character: char) -> bool {
    py_isspace(character as u32)
}

/// Python `str.split()` with no separator.
pub fn split(text: &str) -> Vec<&str> {
    text.split(is_space).filter(|part| !part.is_empty()).collect()
}

/// Python `str.strip()` with no argument.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `list(dict.fromkeys(items))`.
pub fn unique<'a>(items: &[&'a str]) -> Vec<&'a str> {
    let mut seen: Vec<&'a str> = Vec::new();
    for item in items {
        if !seen.contains(item) {
            seen.push(item);
        }
    }
    seen
}

/// Python `haystack.find(needle)` in code points.
pub fn find(haystack: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() {
        return Some(0);
    }
    if needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|start| &haystack[*start..*start + needle.len()] == needle)
}

/// `json.dumps(value, separators=(',', ':'))` (`ensure_ascii=True`) for the
/// int/str/bool/null/list/dict values of search cursors.
pub fn json_dumps_ascii(value: &Value) -> String {
    let mut out = String::new();
    write_ascii(&mut out, value);
    out
}

fn write_ascii(out: &mut String, value: &Value) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&crate::json::python_str(&Value::Number(number.clone()))),
        Value::String(text) => write_ascii_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_ascii(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_ascii_string(out, key);
                out.push(':');
                write_ascii(out, item);
            }
            out.push('}');
        }
    }
}

fn write_ascii_string(out: &mut String, text: &str) {
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
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
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

/// `base64.b64decode(value, validate=True)`: canonical padding, any trailing bits.
const PYTHON_B64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_allow_trailing_bits(true)
        .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
);

/// `base64.b64decode(value.encode('ascii'), altchars=b'-_', validate=True)`.
pub fn b64decode_urlsafe(value: &str) -> Result<Vec<u8>, ()> {
    if !value.is_ascii() {
        return Err(());
    }
    let translated: String = value
        .chars()
        .map(|c| match c {
            '-' => '+',
            '_' => '/',
            other => other,
        })
        .collect();
    PYTHON_B64.decode(translated.as_bytes()).map_err(|_| ())
}

/// `base64.urlsafe_b64encode(raw).decode('ascii')`.
pub fn b64encode_urlsafe(raw: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE.encode(raw)
}

/// `build_search_text([message])` for one message mapping.
pub fn build_search_text(message: &serde_json::Map<String, Value>) -> Result<String, ()> {
    let mut parts: Vec<String> = Vec::new();
    let role = message.get("role").cloned().unwrap_or(Value::String(String::new()));
    if !matches!(role.as_str(), Some("user" | "assistant")) {
        return Ok(String::new());
    }
    match message.get("content") {
        Some(Value::Array(items)) => {
            for item in items {
                match item {
                    Value::String(text) => parts.push(text.clone()),
                    Value::Object(map) => match map.get("text") {
                        None => parts.push(String::new()),
                        Some(Value::String(text)) => parts.push(text.clone()),
                        // `'\n'.join` raises TypeError on a non-str part.
                        Some(_) => return Err(()),
                    },
                    _ => {}
                }
            }
        }
        Some(Value::String(content)) if !content.is_empty() => parts.push(content.clone()),
        _ => {}
    }
    for key in ["thinking", "translatedContent", "originalContent"] {
        if let Some(Value::String(text)) = message.get(key) {
            if !text.is_empty() {
                parts.push(text.clone());
            }
        }
    }
    Ok(parts.join("\n"))
}

/// `_bounded_turn_search_text`: at most 10 000 UTF-8 bytes, partial
/// trailing scalar dropped (`decode('utf-8', errors='ignore')`).
pub fn bounded_turn_search_text(value: String) -> String {
    const TURN_SEARCH_TEXT_MAX_BYTES: usize = 10_000;
    if value.len() <= TURN_SEARCH_TEXT_MAX_BYTES {
        return value;
    }
    let mut end = TURN_SEARCH_TEXT_MAX_BYTES;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn ascii_dumps_matches_python_json() {
        assert_eq!(
            json_dumps_ascii(&json!([2, "ab", 0, 5, "\u{ffff}é😀"])),
            r#"[2,"ab",0,5,"\uffff\u00e9\ud83d\ude00"]"#
        );
        assert_eq!(json_dumps_ascii(&json!({"scan": 1, "pending": null})), r#"{"scan":1,"pending":null}"#);
    }

    #[test]
    fn lower_matches_python_special_cases() {
        assert_eq!(lower("İ").chars().count(), 2);
        assert_eq!(lower("ΟΔΟΣ"), "\u{3bf}\u{3b4}\u{3bf}\u{3c2}");
        assert_eq!(lower_len('İ'), 2);
    }
}
