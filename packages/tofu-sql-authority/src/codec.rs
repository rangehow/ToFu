//! `_dump` / `_load` and the lossless projection storage codec
//! (`operations_pkg/_common.py`, `projection_codec.py`).

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value};

use crate::error::{fail, OpResult, StorageError};
use crate::json;
use crate::session::SqlValue;

pub const STORAGE_PROJECTION_CODEC_KEY: &str = "_tofuStorageProjectionCodec";
pub const STORAGE_PROJECTION_CODEC_VERSION: i64 = 1;

/// Python `==` between two JSON-decoded values (`1 == 1.0 == True`).
pub fn py_eq(left: &Value, right: &Value) -> bool {
    fn numeric(value: &Value) -> Option<f64> {
        match value {
            Value::Bool(flag) => Some(*flag as i64 as f64),
            Value::Number(number) => number.as_f64(),
            _ => None,
        }
    }
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len() && a.iter().all(|(key, value)| b.get(key).is_some_and(|other| py_eq(value, other)))
        }
        (Value::Array(a), Value::Array(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| py_eq(x, y)),
        (Value::String(a), Value::String(b)) => a == b,
        (Value::Null, Value::Null) => true,
        _ => match (numeric(left), numeric(right)) {
            (Some(a), Some(b)) => match (left.as_i64(), right.as_i64()) {
                (Some(x), Some(y)) => x == y,
                _ => a == b,
            },
            _ => false,
        },
    }
}

fn codec_error(_reason: &str) -> StorageError {
    StorageError::new("database_integrity", "Stored turn projection encoding is invalid")
}

fn segment_tool_call_id(segment: &Map<String, Value>) -> String {
    let id = segment.get("id").filter(|value| json::truthy(value)).or_else(|| segment.get("toolCallId"));
    match id {
        Some(Value::String(text)) => text.clone(),
        _ => String::new(),
    }
}

fn unique_tool_rounds_by_id(tool_rounds: &[Value]) -> HashMap<String, &Map<String, Value>> {
    let mut indexed = HashMap::new();
    let mut duplicates = HashSet::new();
    for candidate in tool_rounds {
        let Value::Object(round) = candidate else { continue };
        let Some(Value::String(id)) = round.get("toolCallId") else { continue };
        if id.is_empty() {
            continue;
        }
        if indexed.contains_key(id) {
            duplicates.insert(id.clone());
            continue;
        }
        indexed.insert(id.clone(), round);
    }
    for id in duplicates {
        indexed.remove(&id);
    }
    indexed
}

fn is_projection_shape(value: &Value) -> bool {
    matches!(value, Value::Object(map)
        if map.get("segments").is_some_and(Value::is_array) && map.get("toolRounds").is_some_and(Value::is_array))
}

fn sorted(map: Map<String, Value>) -> Map<String, Value> {
    let mut entries: Vec<(String, Value)> = map.into_iter().collect();
    entries.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    entries.into_iter().collect()
}

/// The public projection used the reserved codec namespace.
#[derive(Debug)]
pub struct ReservedCodecKey;

/// `encode_projection_for_storage`; `Ok(None)` keeps the projection verbatim.
pub fn encode_projection(projection: &Value) -> Result<Option<Value>, ReservedCodecKey> {
    if !is_projection_shape(projection) {
        return Ok(None);
    }
    let map = projection.as_object().expect("projection shape");
    if map.contains_key(STORAGE_PROJECTION_CODEC_KEY) {
        return Err(ReservedCodecKey);
    }
    let segments = map["segments"].as_array().expect("segments");
    let tool_rounds = unique_tool_rounds_by_id(map["toolRounds"].as_array().expect("toolRounds"));
    let mut counts: HashMap<String, usize> = HashMap::new();
    for candidate in segments {
        if let Value::Object(segment) = candidate {
            let id = segment_tool_call_id(segment);
            if !id.is_empty() {
                *counts.entry(id).or_default() += 1;
            }
        }
    }
    let mut encoded_segments: Option<Vec<Value>> = None;
    let mut references = Vec::new();
    for (index, candidate) in segments.iter().enumerate() {
        let Value::Object(segment) = candidate else { continue };
        let id = segment_tool_call_id(segment);
        if counts.get(&id) != Some(&1) {
            continue;
        }
        let Some(round) = tool_rounds.get(&id) else { continue };
        let input_from_round = segment.contains_key("input")
            && round.contains_key("toolArgs")
            && py_eq(&segment["input"], &round["toolArgs"]);
        let result = segment.get("result");
        let content_from_round = matches!(result, Some(Value::Object(result))
            if result.contains_key("content") && round.contains_key("toolContent")
            && py_eq(&result["content"], &round["toolContent"]));
        if !input_from_round && !content_from_round {
            continue;
        }
        let encoded = encoded_segments.get_or_insert_with(|| segments.clone());
        let mut encoded_segment = segment.clone();
        let mut reference = Map::new();
        reference.insert("segmentIndex".into(), Value::from(index as i64));
        reference.insert("toolCallId".into(), Value::String(id.clone()));
        if input_from_round {
            encoded_segment.shift_remove("input");
            reference.insert("inputFromToolRound".into(), Value::Bool(true));
        }
        if content_from_round {
            let mut encoded_result = result.and_then(Value::as_object).cloned().unwrap_or_default();
            encoded_result.shift_remove("content");
            encoded_segment.insert("result".into(), Value::Object(encoded_result));
            reference.insert("resultContentFromToolRound".into(), Value::Bool(true));
        }
        encoded[index] = Value::Object(encoded_segment);
        references.push(Value::Object(reference));
    }
    let Some(encoded_segments) = encoded_segments else { return Ok(None) };
    if references.is_empty() {
        return Ok(None);
    }
    let mut encoded = map.clone();
    encoded.insert("segments".into(), Value::Array(encoded_segments));
    let mut codec = Map::new();
    codec.insert("version".into(), Value::from(STORAGE_PROJECTION_CODEC_VERSION));
    codec.insert("segmentToolRoundReferences".into(), Value::Array(references));
    encoded.insert(STORAGE_PROJECTION_CODEC_KEY.into(), Value::Object(codec));
    Ok(Some(Value::Object(encoded)))
}

/// `decode_projection_from_storage`.
pub fn decode_projection(projection: Value) -> Result<Value, StorageError> {
    let Value::Object(map) = &projection else { return Ok(projection) };
    let Some(codec) = map.get(STORAGE_PROJECTION_CODEC_KEY) else { return Ok(projection) };
    if !is_projection_shape(&projection) {
        return Err(codec_error("projection shape is missing"));
    }
    let Value::Object(codec) = codec else { return Err(codec_error("codec envelope")) };
    if json::as_strict_int(codec.get("version").unwrap_or(&Value::Null)) != Some(STORAGE_PROJECTION_CODEC_VERSION)
        || matches!(codec.get("version"), Some(Value::Bool(_)))
    {
        return Err(codec_error("unsupported codec version"));
    }
    let segments = map["segments"].as_array().expect("segments");
    let references = match codec.get("segmentToolRoundReferences") {
        Some(Value::Array(references)) if !references.is_empty() && references.len() <= segments.len() => references,
        _ => return Err(codec_error("reference list is invalid")),
    };
    let tool_rounds = unique_tool_rounds_by_id(map["toolRounds"].as_array().expect("toolRounds"));
    let mut decoded_segments = segments.clone();
    let mut seen_indexes = HashSet::new();
    let mut seen_ids = HashSet::new();
    for reference in references {
        let Value::Object(reference) = reference else { return Err(codec_error("reference")) };
        let index = match reference.get("segmentIndex") {
            Some(Value::Number(number)) => number.as_i64(),
            _ => None,
        };
        let Some(index) = index.filter(|i| *i >= 0 && (*i as usize) < segments.len()) else {
            return Err(codec_error("segment index"));
        };
        if !seen_indexes.insert(index) {
            return Err(codec_error("segment index"));
        }
        let id = match reference.get("toolCallId") {
            Some(Value::String(id)) if !id.is_empty() && !seen_ids.contains(id) => id.clone(),
            _ => return Err(codec_error("tool-call identity")),
        };
        seen_ids.insert(id.clone());
        let Value::Object(candidate) = &decoded_segments[index as usize] else {
            return Err(codec_error("segment identity"));
        };
        if segment_tool_call_id(candidate) != id {
            return Err(codec_error("segment identity"));
        }
        let Some(round) = tool_rounds.get(&id) else { return Err(codec_error("tool round")) };
        let restore_input = reference.get("inputFromToolRound") == Some(&Value::Bool(true));
        let restore_content = reference.get("resultContentFromToolRound") == Some(&Value::Bool(true));
        if !restore_input && !restore_content {
            return Err(codec_error("no restorable field"));
        }
        let mut segment = candidate.clone();
        if restore_input && !segment.contains_key("input") {
            let Some(args) = round.get("toolArgs") else { return Err(codec_error("tool args")) };
            segment.insert("input".into(), args.clone());
        }
        if restore_content {
            let Some(Value::Object(result)) = segment.get("result") else {
                return Err(codec_error("segment result"));
            };
            let mut result = result.clone();
            if !result.contains_key("content") {
                let Some(content) = round.get("toolContent") else { return Err(codec_error("tool result")) };
                result.insert("content".into(), content.clone());
            }
            segment.insert("result".into(), Value::Object(sorted(result)));
        }
        decoded_segments[index as usize] = Value::Object(sorted(segment));
    }
    let mut decoded = map.clone();
    decoded.shift_remove(STORAGE_PROJECTION_CODEC_KEY);
    decoded.insert("segments".into(), Value::Array(decoded_segments));
    Ok(Value::Object(sorted(decoded)))
}

/// `_dump(value)`: orjson sorted bytes of the storage-encoded projection,
/// bound as a BLOB like Python's `JsonBytes`.
pub fn dump(value: &Value) -> OpResult<Vec<u8>> {
    match encode_projection(value) {
        Ok(Some(encoded)) => Ok(json::dumps_sorted(&encoded)),
        Ok(None) => Ok(json::dumps_sorted(value)),
        Err(ReservedCodecKey) => fail("database_protocol_error", "Projection storage encoding is invalid"),
    }
}

pub fn dump_param(value: &Value) -> OpResult<SqlValue> {
    Ok(SqlValue::Blob(dump(value)?))
}

/// `_load(column)`: decode stored JSON text/bytes and hydrate projections.
pub fn load(value: &SqlValue) -> OpResult<Value> {
    let decoded = match value {
        SqlValue::Null => return Ok(Value::Null),
        SqlValue::Integer(integer) => return Ok(Value::from(*integer)),
        SqlValue::Real(real) => return Ok(json::float_value(*real)),
        SqlValue::Text(text) => json::loads(text.as_bytes()),
        SqlValue::Blob(bytes) => json::loads(bytes),
    };
    let decoded = match decoded {
        Ok(decoded) => decoded,
        Err(_) => return crate::error::internal("orjson.JSONDecodeError"),
    };
    Ok(decode_projection(decoded)?)
}

/// `_load` of an already decoded value (PostgreSQL/in-memory hand-off).
pub fn load_value(value: Value) -> OpResult<Value> {
    Ok(decode_projection(value)?)
}

/// `_wire_document` for one `bytes` value: `{"$bytes": base64}`.
pub fn wire_bytes(bytes: &[u8]) -> Value {
    use base64::Engine;
    let mut map = Map::new();
    map.insert("$bytes".into(), Value::String(base64::engine::general_purpose::STANDARD.encode(bytes)));
    Value::Object(map)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn projection_round_trip_interns_and_restores() {
        let projection = json!({
            "segments": [{"type": "tool", "id": "c1", "input": {"a": 1}, "result": {"content": "x", "ok": true}}],
            "toolRounds": [{"toolCallId": "c1", "toolArgs": {"a": 1}, "toolContent": "x"}],
        });
        let stored = dump(&projection).unwrap();
        let text = String::from_utf8(stored.clone()).unwrap();
        assert!(text.contains(STORAGE_PROJECTION_CODEC_KEY));
        assert!(!text.contains("\"input\""));
        let loaded = load(&SqlValue::Blob(stored)).unwrap();
        assert!(py_eq(&loaded, &projection));
    }

    #[test]
    fn python_equality_is_numeric() {
        assert!(py_eq(&json!(1), &json!(1.0)));
        assert!(py_eq(&json!(true), &json!(1)));
        assert!(!py_eq(&json!("1"), &json!(1)));
    }
}
