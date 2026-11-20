//! `lib/paper/builds/contract.py` (`limit`, `validate`) and
//! `document_identity.py::source_binding_key`, plus the CPython codecs the
//! paper build operations rely on (`json.dumps`, strict `b64decode`).
//!
//! `validate` interprets the subset of JSON Schema draft 2020-12 used by
//! `contracts/paper_build_v1.schema.json` with python-jsonschema's semantics:
//! keywords run in schema order and the FIRST error's instance path names
//! the invalid field.

use std::sync::OnceLock;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::json::python_float_repr;

const SCHEMA: &str = include_str!("../../../../../contracts/paper_build_v1.schema.json");

fn schema() -> &'static Value {
    static PARSED: OnceLock<Value> = OnceLock::new();
    PARSED.get_or_init(|| serde_json::from_str(SCHEMA).expect("paper build schema JSON"))
}

/// `limit(name)`.
pub fn limit(name: &str) -> i64 {
    let value = &schema()["x-resource-limits"][name];
    value.as_i64().unwrap_or_else(|| value.as_f64().map(|v| v as i64).expect("paper build limit"))
}

/// `BuildError('build_contract', message)`; `str(exc)` is the message.
#[derive(Debug, Clone, PartialEq)]
pub struct BuildError(pub String);

fn is_integer(value: &Value) -> bool {
    match value {
        Value::Number(number) => {
            number.is_i64() || number.is_u64() || number.as_f64().is_some_and(|v| v.fract() == 0.0)
        }
        _ => false,
    }
}

fn type_matches(name: &str, value: &Value) -> bool {
    match name {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "boolean" => value.is_boolean(),
        "null" => value.is_null(),
        "number" => value.is_number(),
        "integer" => is_integer(value),
        _ => false,
    }
}

/// `jsonschema._utils.equal`: booleans never equal numbers; `1 == 1.0`.
fn schema_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::String(a), Value::String(b)) => a == b,
        (Value::String(_), _) | (_, Value::String(_)) => false,
        (Value::Array(a), Value::Array(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| schema_equal(x, y)),
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter().all(|(key, value)| b.get(key).is_some_and(|other| schema_equal(value, other)))
        }
        (Value::Bool(a), Value::Bool(b)) => a == b,
        (Value::Bool(_), _) | (_, Value::Bool(_)) => false,
        (Value::Null, Value::Null) => true,
        (Value::Number(a), Value::Number(b)) => match (a.as_i64(), b.as_i64()) {
            (Some(x), Some(y)) => x == y,
            _ => a.as_f64() == b.as_f64(),
        },
        _ => false,
    }
}

/// Python `re.search(pattern, text)`: a trailing `$` also matches before a
/// final newline.
fn pattern_matches(pattern: &str, text: &str) -> bool {
    let translated = match pattern.strip_suffix('$') {
        Some(head) if !head.ends_with('\\') => format!("{head}\\n?\\z"),
        _ => pattern.to_owned(),
    };
    regex::Regex::new(&translated).map(|regex| regex.is_match(text)).unwrap_or(false)
}

fn compare_number(value: &Value, bound: &Value) -> std::cmp::Ordering {
    match (value.as_i64(), bound.as_i64()) {
        (Some(a), Some(b)) => a.cmp(&b),
        _ => {
            let a = value.as_f64().unwrap_or(f64::NAN);
            let b = bound.as_f64().unwrap_or(f64::NAN);
            a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
        }
    }
}

fn resolve(reference: &str) -> &'static Value {
    let name = reference.rsplit('/').next().unwrap_or_default();
    &schema()["$defs"][name]
}

/// The instance path (innermost last) of the first validation error.
fn first_error(subschema: &Value, instance: &Value) -> Option<Vec<String>> {
    let Value::Object(keywords) = subschema else {
        return if subschema == &Value::Bool(false) { Some(Vec::new()) } else { None };
    };
    for (keyword, argument) in keywords {
        let error = match keyword.as_str() {
            "$ref" => first_error(resolve(argument.as_str().unwrap_or_default()), instance),
            "type" => {
                let matches = match argument {
                    Value::String(name) => type_matches(name, instance),
                    Value::Array(names) => {
                        names.iter().any(|name| type_matches(name.as_str().unwrap_or_default(), instance))
                    }
                    _ => true,
                };
                (!matches).then(Vec::new)
            }
            "const" => (!schema_equal(instance, argument)).then(Vec::new),
            "enum" => {
                let options = argument.as_array().map(Vec::as_slice).unwrap_or_default();
                (!options.iter().any(|option| schema_equal(option, instance))).then(Vec::new)
            }
            "pattern" => match instance {
                Value::String(text) => (!pattern_matches(argument.as_str().unwrap_or_default(), text)).then(Vec::new),
                _ => None,
            },
            "minLength" => match instance {
                Value::String(text) => ((text.chars().count() as i64) < argument.as_i64().unwrap_or(0)).then(Vec::new),
                _ => None,
            },
            "maxLength" => match instance {
                Value::String(text) => {
                    ((text.chars().count() as i64) > argument.as_i64().unwrap_or(i64::MAX)).then(Vec::new)
                }
                _ => None,
            },
            "minimum" => match instance {
                Value::Number(_) => compare_number(instance, argument).is_lt().then(Vec::new),
                _ => None,
            },
            "maximum" => match instance {
                Value::Number(_) => compare_number(instance, argument).is_gt().then(Vec::new),
                _ => None,
            },
            "maxItems" => match instance {
                Value::Array(items) => ((items.len() as i64) > argument.as_i64().unwrap_or(i64::MAX)).then(Vec::new),
                _ => None,
            },
            "minItems" => match instance {
                Value::Array(items) => ((items.len() as i64) < argument.as_i64().unwrap_or(0)).then(Vec::new),
                _ => None,
            },
            "required" => match instance {
                Value::Object(map) => {
                    let names = argument.as_array().map(Vec::as_slice).unwrap_or_default();
                    names.iter().any(|name| !map.contains_key(name.as_str().unwrap_or_default())).then(Vec::new)
                }
                _ => None,
            },
            "properties" => match (instance, argument) {
                (Value::Object(map), Value::Object(properties)) => properties.iter().find_map(|(name, property)| {
                    let value = map.get(name)?;
                    first_error(property, value).map(|mut path| {
                        path.push(name.clone());
                        path
                    })
                }),
                _ => None,
            },
            "additionalProperties" => match instance {
                Value::Object(map) => {
                    let declared = keywords.get("properties").and_then(Value::as_object);
                    let mut extras = map
                        .iter()
                        .filter(|(name, _)| !declared.is_some_and(|properties| properties.contains_key(*name)));
                    match argument {
                        Value::Bool(false) => extras.next().map(|_| Vec::new()),
                        Value::Object(_) => extras.find_map(|(name, value)| {
                            first_error(argument, value).map(|mut path| {
                                path.push(name.clone());
                                path
                            })
                        }),
                        _ => None,
                    }
                }
                _ => None,
            },
            "items" => match instance {
                Value::Array(items) => items.iter().enumerate().find_map(|(index, item)| {
                    first_error(argument, item).map(|mut path| {
                        path.push(index.to_string());
                        path
                    })
                }),
                _ => None,
            },
            "anyOf" => {
                let options = argument.as_array().map(Vec::as_slice).unwrap_or_default();
                (!options.iter().any(|option| first_error(option, instance).is_none())).then(Vec::new)
            }
            "allOf" => {
                let options = argument.as_array().map(Vec::as_slice).unwrap_or_default();
                options.iter().find_map(|option| first_error(option, instance))
            }
            "if" => {
                if first_error(argument, instance).is_none() {
                    keywords.get("then").and_then(|then| first_error(then, instance))
                } else {
                    keywords.get("else").and_then(|otherwise| first_error(otherwise, instance))
                }
            }
            _ => None,
        };
        if error.is_some() {
            return error;
        }
    }
    None
}

/// `validate(value, definition)`.
pub fn validate(value: &Value, definition: &str) -> Result<(), BuildError> {
    if let Value::Object(map) = value {
        if let Some(version) = map.get("schema_version") {
            if !(version.is_i64() || version.is_u64()) {
                return Err(BuildError("Invalid paper build field: schema_version".into()));
            }
        }
    }
    if let Some(mut path) = first_error(resolve(definition), value) {
        path.reverse();
        let field = if path.is_empty() { definition.to_owned() } else { path.join(".") };
        return Err(BuildError(format!("Invalid paper build field: {field}")));
    }
    if definition == "PaperBuildReceiptV1" {
        let artifacts = value["artifacts"].as_array().map(Vec::as_slice).unwrap_or_default();
        let names: Vec<&str> = artifacts.iter().map(|row| row["name"].as_str().unwrap_or_default()).collect();
        let mut unique = names.clone();
        unique.sort_unstable();
        unique.dedup();
        if unique.len() != names.len() {
            return Err(BuildError("Duplicate build artifact".into()));
        }
        let diagnostics = value["diagnostics"].as_array().map(Vec::as_slice).unwrap_or_default();
        if value["status"] == "success"
            && (!names.contains(&"document.pdf") || diagnostics.iter().any(|row| row["severity"] == "error"))
        {
            return Err(BuildError("Successful build requires a PDF and no errors".into()));
        }
    }
    Ok(())
}

fn write_python_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(character),
            other => {
                let mut units = [0u16; 2];
                for unit in other.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write_python_json(out: &mut String, value: &Value, sort_keys: bool) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0)));
            }
        }
        Value::String(text) => write_python_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_python_json(out, item, sort_keys);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if sort_keys {
                entries.sort_by(|left, right| left.0.cmp(right.0));
            }
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_python_string(out, key);
                out.push(':');
                write_python_json(out, item, sort_keys);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(value, separators=(',', ':'), sort_keys=...)` (ASCII-escaped).
pub fn python_json_dumps(value: &Value, sort_keys: bool) -> String {
    let mut out = String::new();
    write_python_json(&mut out, value, sort_keys);
    out
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

/// `document_identity.source_binding_key(source)`.
pub fn source_binding_key(source: &Value) -> Result<String, BuildError> {
    validate(source, "PaperStoredSourceV1")?;
    let names: &[&str] = match source["source"].as_str().unwrap_or_default() {
        "local" => &["source", "project_root", "main_file"],
        "overleaf" => &["source", "mcp_server", "project_id", "main_file"],
        _ => &["source", "paper_hash", "lang"],
    };
    let mut binding = Map::new();
    for name in names {
        binding.insert((*name).to_owned(), source[*name].clone());
    }
    Ok(sha256_hex(python_json_dumps(&Value::Object(binding), true).as_bytes()))
}

fn base64_value(byte: u8) -> Option<u8> {
    match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// CPython 3.12 `base64.b64decode(text, validate=True)` (`a2b_base64` strict
/// mode): `None` wherever Python raises `binascii.Error`/`ValueError`.
pub fn b64decode_strict(text: &str) -> Option<Vec<u8>> {
    if !text.is_ascii() {
        return None;
    }
    let data = text.as_bytes();
    if data.first() == Some(&b'=') {
        return None;
    }
    let mut out = Vec::with_capacity(data.len() / 4 * 3);
    let mut quad_pos = 0u8;
    let mut left = 0u8;
    let mut pads = 0u8;
    let mut padding_started = false;
    for (index, &byte) in data.iter().enumerate() {
        if byte == b'=' {
            padding_started = true;
            if quad_pos == 0 {
                return None;
            }
            if quad_pos >= 2 {
                pads += 1;
                if quad_pos + pads >= 4 {
                    if index + 1 < data.len() {
                        return None;
                    }
                    return Some(out);
                }
            }
            continue;
        }
        let value = base64_value(byte)?;
        if padding_started {
            return None;
        }
        pads = 0;
        match quad_pos {
            0 => {
                quad_pos = 1;
                left = value;
            }
            1 => {
                quad_pos = 2;
                out.push((left << 2) | (value >> 4));
                left = value & 0x0f;
            }
            2 => {
                quad_pos = 3;
                out.push((left << 4) | (value >> 2));
                left = value & 0x03;
            }
            _ => {
                quad_pos = 0;
                out.push((left << 6) | value);
                left = 0;
            }
        }
    }
    if quad_pos != 0 {
        return None;
    }
    Some(out)
}

pub fn b64encode(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn strict_base64_matches_cpython() {
        assert_eq!(b64decode_strict("cGRm"), Some(b"pdf".to_vec()));
        for bad in ["cGRm=", "cGRm==", "cGRm====", "cGRm\n", "=cGRm", "cGR", "cGRm*", "eB=", "cG=Rm", "cGRmeB"] {
            assert_eq!(b64decode_strict(bad), None, "{bad:?}");
        }
        assert_eq!(b64decode_strict("eB=="), Some(b"x".to_vec()));
        assert_eq!(b64decode_strict("eA=="), Some(b"x".to_vec()));
        assert_eq!(b64decode_strict(""), Some(Vec::new()));
    }

    #[test]
    fn python_json_escapes_non_ascii() {
        assert_eq!(
            python_json_dumps(&json!({"b": 1.0, "a": "界😀\u{7f}"}), true),
            r#"{"a":"\u754c\ud83d\ude00\u007f","b":1.0}"#
        );
    }

    #[test]
    fn digests_match_python_json_dumps() {
        let receipt = json!({"schema_version": 1, "owner_user_id": 11, "document_id": "d".repeat(64),
            "build_id": "b".repeat(32), "toolchain": "fixture界😀", "size": 3.0, "x": null, "t": true, "f": 1e-7});
        assert_eq!(
            sha256_hex(python_json_dumps(&receipt, true).as_bytes()),
            "5c5a91f12e784e1513b755d51c8564e63e79c10d7600adacd19cd8651298f503"
        );
        let source = json!({"source": "local", "project_root": "/saved/界😀", "path": "chapter.tex",
            "main_file": "m.tex", "label": "L"});
        assert_eq!(
            source_binding_key(&source).unwrap(),
            "76381cf246250eb08549175feba47b599423e6015c2e8266c8121fa4e6969e74"
        );
    }

    #[test]
    fn receipt_errors_name_the_first_invalid_field() {
        let receipt = json!({"schema_version": 1, "owner_user_id": 1, "document_id": "d".repeat(64),
            "build_id": "b".repeat(32), "target": "host", "source_digest": "e".repeat(64), "version_verified": true,
            "main_file": "main.tex", "engine": "pdflatex", "toolchain": "t", "status": "success", "started_at": 1,
            "completed_at": 2, "diagnostics": [], "artifacts": [{"name": "document.pdf", "sha256": "a".repeat(64), "size_bytes": 3.0}]});
        assert_eq!(validate(&receipt, "PaperBuildReceiptV1"), Ok(()));
        let mut bad = receipt.clone();
        bad["source_digest"] = Value::Null;
        assert_eq!(
            validate(&bad, "PaperBuildReceiptV1"),
            Err(BuildError("Invalid paper build field: source_digest".into()))
        );
        let mut bad = receipt.clone();
        bad["artifacts"][0]["name"] = json!("x");
        assert_eq!(
            validate(&bad, "PaperBuildReceiptV1"),
            Err(BuildError("Invalid paper build field: artifacts.0.name".into()))
        );
        assert!(pattern_matches("^[0-9a-f]{2}$", "ab\n"));
        assert!(!pattern_matches("^[0-9a-f]{2}$", "ab\n\n"));
    }
}
