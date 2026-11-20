//! Pure codecs and projections the core handlers apply at the storage
//! boundary: `task_result_field_codec.py`, `task_event_codec.py`,
//! `lib/task_replay/persistence_projection.py` (usage projection),
//! `lib/event_envelope.py` (stamp/clock) and
//! `lib/task_replay/request_inspector_projection.py`.

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use flate2::{Decompress, FlushDecompress, Status};
use serde_json::{Map, Value};

use crate::codec;
use crate::error::{OpResult, StorageError};
use crate::json;

/// `lib/storage/protocol.py::MAX_FRAME_BYTES`.
pub const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;

fn zlib_level1(source: &[u8]) -> Vec<u8> {
    crate::zlib::compress(source, 1)
}

/// `zlib.decompressobj().decompress(data, expected + 1)` followed by the
/// callers' exact-length, `eof`, `unconsumed_tail` and `unused_data` checks.
/// `Err(true)` is `zlib.error`; `Err(false)` a length/trailer mismatch.
fn zlib_exact(data: &[u8], expected: usize) -> Result<Vec<u8>, bool> {
    let mut decompressor = Decompress::new(true);
    let mut output = Vec::with_capacity(expected + 1);
    let status = decompressor.decompress_vec(data, &mut output, FlushDecompress::None).map_err(|_| true)?;
    let consumed = decompressor.total_in() as usize;
    if output.len() != expected || status != Status::StreamEnd || consumed != data.len() {
        return Err(false);
    }
    Ok(output)
}

// ── task_result_field_codec.py ───────────────────────────────────────────

pub const TASK_RESULT_FIELD_CODEC_KEY: &str = "_tofuTaskResultFieldCodec";
const TASK_RESULT_FIELD_CODEC_VERSION: i64 = 1;
const TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES: usize = 32 * 1024;
const MAX_DECODED_TASK_RESULT_FIELD_BYTES: usize = MAX_FRAME_BYTES;
const MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES: usize = MAX_FRAME_BYTES;
pub const TASK_RESULT_COMPRESSIBLE_FIELDS: [&str; 6] =
    ["segments", "metadata", "tool_rounds", "content", "thinking", "error"];
const TASK_RESULT_FIELD_ENCODING: &str = "zlib-base64";

/// `ProjectionCodecError` (and its `TaskResultFieldCodecError` subclass).
#[derive(Debug)]
pub struct CodecError;

fn serialized_document(value: &Value) -> Result<Vec<u8>, CodecError> {
    match codec::encode_projection(value) {
        Ok(Some(encoded)) => Ok(json::dumps_sorted(&encoded)),
        Ok(None) => Ok(json::dumps_sorted(value)),
        Err(codec::ReservedCodecKey) => Err(CodecError),
    }
}

fn encoded_text_field(text: &str) -> Result<Option<Value>, CodecError> {
    let source = text.as_bytes();
    if source.len() > MAX_DECODED_TASK_RESULT_FIELD_BYTES {
        return Err(CodecError);
    }
    if source.len() < TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES {
        return Ok(None);
    }
    let payload = base64::engine::general_purpose::STANDARD.encode(zlib_level1(source));
    if payload.len() > MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES {
        return Ok(None);
    }
    let envelope = json::object(vec![(
        TASK_RESULT_FIELD_CODEC_KEY,
        json::object(vec![
            ("version", Value::from(TASK_RESULT_FIELD_CODEC_VERSION)),
            ("encoding", Value::from(TASK_RESULT_FIELD_ENCODING)),
            ("decodedBytes", Value::from(source.len() as i64)),
            ("payload", Value::String(payload)),
        ]),
    )]);
    let encoded = json::dumps_sorted(&envelope);
    Ok(if encoded.len() < source.len() { Some(envelope) } else { None })
}

/// `encode_task_result_fields_for_storage(value).stored_document`.
pub fn encode_task_result_document(value: &Value) -> Result<Vec<u8>, CodecError> {
    let Value::Object(public) = value else { return serialized_document(value) };
    let mut stored: Option<Map<String, Value>> = None;
    for field in TASK_RESULT_COMPRESSIBLE_FIELDS {
        let candidate = public.get(field);
        if let Some(Value::Object(map)) = candidate {
            if map.contains_key(TASK_RESULT_FIELD_CODEC_KEY) {
                return Err(CodecError);
            }
        }
        let Some(Value::String(text)) = candidate else { continue };
        let Some(encoded) = encoded_text_field(text)? else { continue };
        stored.get_or_insert_with(|| public.clone()).insert(field.to_owned(), encoded);
    }
    match stored {
        Some(stored) => serialized_document(&Value::Object(stored)),
        None => serialized_document(value),
    }
}

fn decoded_text_field(
    candidate: &Map<String, Value>,
    decoded_budget: usize,
    payload_budget: usize,
) -> Result<(String, usize, usize), CodecError> {
    if candidate.len() != 1 {
        return Err(CodecError);
    }
    let Some(Value::Object(envelope)) = candidate.get(TASK_RESULT_FIELD_CODEC_KEY) else { return Err(CodecError) };
    if envelope.len() != 4
        || !["version", "encoding", "decodedBytes", "payload"].iter().all(|k| envelope.contains_key(*k))
    {
        return Err(CodecError);
    }
    if json::as_strict_int(&envelope["version"]) != Some(TASK_RESULT_FIELD_CODEC_VERSION) {
        return Err(CodecError);
    }
    if envelope["encoding"].as_str() != Some(TASK_RESULT_FIELD_ENCODING) {
        return Err(CodecError);
    }
    let decoded_bytes = match json::as_strict_int(&envelope["decodedBytes"]) {
        Some(value)
            if value >= TASK_RESULT_FIELD_COMPRESSION_MIN_BYTES as i64
                && value <= MAX_DECODED_TASK_RESULT_FIELD_BYTES as i64 =>
        {
            value as usize
        }
        _ => return Err(CodecError),
    };
    if decoded_bytes > decoded_budget {
        return Err(CodecError);
    }
    let payload = match &envelope["payload"] {
        Value::String(text)
            if !text.is_empty() && text.chars().count() <= MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES =>
        {
            text
        }
        _ => return Err(CodecError),
    };
    if !payload.is_ascii() {
        return Err(CodecError);
    }
    if payload.len() > payload_budget {
        return Err(CodecError);
    }
    let engine = GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
    );
    let compressed = engine.decode(payload).map_err(|_| CodecError)?;
    if compressed.is_empty() {
        return Err(CodecError);
    }
    let source = zlib_exact(&compressed, decoded_bytes).map_err(|_| CodecError)?;
    let text = String::from_utf8(source).map_err(|_| CodecError)?;
    Ok((text, decoded_bytes, payload.len()))
}

/// `decode_task_result_fields_from_storage(value, fields=fields)`.
pub fn decode_task_result_fields(value: Value, fields: Option<&[&str]>) -> Result<Value, CodecError> {
    let Value::Object(map) = &value else { return Ok(value) };
    let mut decoded: Option<Map<String, Value>> = None;
    let mut decoded_bytes = 0usize;
    let mut payload_bytes = 0usize;
    for field in TASK_RESULT_COMPRESSIBLE_FIELDS {
        if fields.is_some_and(|selected| !selected.contains(&field)) {
            continue;
        }
        let Some(Value::Object(candidate)) = map.get(field) else { continue };
        if !candidate.contains_key(TASK_RESULT_FIELD_CODEC_KEY) {
            continue;
        }
        let (text, decoded_size, payload_size) = decoded_text_field(
            candidate,
            MAX_DECODED_TASK_RESULT_FIELD_BYTES - decoded_bytes,
            MAX_STORED_TASK_RESULT_FIELD_PAYLOAD_BYTES - payload_bytes,
        )?;
        decoded.get_or_insert_with(|| map.clone()).insert(field.to_owned(), Value::String(text));
        decoded_bytes += decoded_size;
        payload_bytes += payload_size;
    }
    Ok(match decoded {
        Some(decoded) => Value::Object(decoded),
        None => value,
    })
}

/// `_records.py::_encoded_task_result_document`.
pub fn encoded_task_result_document(value: &Value) -> OpResult<Vec<u8>> {
    encode_task_result_document(value)
        .map_err(|_| StorageError::protocol("Task result storage encoding is invalid").into())
}

/// `_records.py::_hydrated_task_result_value`.
pub fn hydrated_task_result_value(value: Value, fields: Option<&[&str]>) -> OpResult<Value> {
    decode_task_result_fields(value, fields)
        .map_err(|_| StorageError::new("database_integrity", "Stored task result field encoding is invalid").into())
}

// ── task_event_codec.py ──────────────────────────────────────────────────

const TASK_EVENT_COMPRESSION_MIN_BYTES: usize = 64 * 1024;
const MAX_DECODED_TASK_EVENT_BYTES: usize = MAX_FRAME_BYTES;
const MAX_STORED_TASK_EVENT_BYTES: usize = MAX_FRAME_BYTES;
const COMPRESSED_TASK_EVENT_MAGIC: &[u8] = b"tofu.task-event.zlib.v1\x00";
const TASK_EVENT_MAGIC_PREFIX: &[u8] = b"tofu.task-event.";

fn integrity(message: &str) -> StorageError {
    StorageError::new("database_integrity", message)
}

pub fn encode_task_event_payload(raw: Vec<u8>) -> Result<Vec<u8>, StorageError> {
    if raw.len() > MAX_DECODED_TASK_EVENT_BYTES {
        return Err(StorageError::protocol("Task event exceeds its decoded byte budget"));
    }
    if raw.len() < TASK_EVENT_COMPRESSION_MIN_BYTES {
        return Ok(raw);
    }
    let compressed = zlib_level1(&raw);
    let mut encoded = Vec::with_capacity(COMPRESSED_TASK_EVENT_MAGIC.len() + 4 + compressed.len());
    encoded.extend_from_slice(COMPRESSED_TASK_EVENT_MAGIC);
    encoded.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    encoded.extend_from_slice(&compressed);
    Ok(if encoded.len() < raw.len() { encoded } else { raw })
}

/// `_payload_bytes`: TEXT/BLOB columns; anything else is an invalid type.
pub fn payload_bytes(value: &crate::session::SqlValue) -> Result<&[u8], StorageError> {
    value.bytes().ok_or_else(|| integrity("Stored task event has an invalid payload type"))
}

fn decoded_size_from_encoded(encoded: &[u8]) -> Result<usize, StorageError> {
    if encoded.len() > MAX_STORED_TASK_EVENT_BYTES {
        return Err(integrity("Stored task event exceeds its byte budget"));
    }
    if !encoded.starts_with(COMPRESSED_TASK_EVENT_MAGIC) {
        if encoded.starts_with(TASK_EVENT_MAGIC_PREFIX) {
            return Err(integrity("Stored task event uses an unsupported codec"));
        }
        return Ok(encoded.len());
    }
    let header_start = COMPRESSED_TASK_EVENT_MAGIC.len();
    let payload_start = header_start + 4;
    if encoded.len() <= payload_start {
        return Err(integrity("Stored compressed task event is truncated"));
    }
    let length = u32::from_be_bytes(encoded[header_start..payload_start].try_into().expect("four bytes")) as usize;
    if !(TASK_EVENT_COMPRESSION_MIN_BYTES..=MAX_DECODED_TASK_EVENT_BYTES).contains(&length) {
        return Err(integrity("Stored compressed task-event length is invalid"));
    }
    Ok(length)
}

pub fn decode_task_event_payload(value: &crate::session::SqlValue) -> Result<Vec<u8>, StorageError> {
    let encoded = payload_bytes(value)?;
    let length = decoded_size_from_encoded(encoded)?;
    if !encoded.starts_with(COMPRESSED_TASK_EVENT_MAGIC) {
        return Ok(encoded.to_vec());
    }
    let payload_start = COMPRESSED_TASK_EVENT_MAGIC.len() + 4;
    zlib_exact(&encoded[payload_start..], length).map_err(|corrupt| {
        if corrupt {
            integrity("Stored compressed task event is corrupt")
        } else {
            integrity("Stored compressed task-event length mismatched")
        }
    })
}

// ── persistence_projection.py (usage projection) ─────────────────────────

const USAGE_TRANSIENT_KEYS: [&str; 3] = ["_wire_fp", "_wire_static", "_wire_routing"];
const API_ROUND_FIELDS: [&str; 2] = ["apiRounds", "_continueApiRounds"];

fn is_usage_transient_key(key: &str) -> bool {
    USAGE_TRANSIENT_KEYS.contains(&key) || key.starts_with("_wire_")
}

/// `sanitize_usage_for_persist`; `None` means "unchanged".
fn sanitize_usage(usage: &Value) -> Option<Value> {
    let Value::Object(map) = usage else { return None };
    if !map.keys().any(|key| is_usage_transient_key(key)) {
        return None;
    }
    Some(Value::Object(
        map.iter().filter(|(key, _)| !is_usage_transient_key(key)).map(|(k, v)| (k.clone(), v.clone())).collect(),
    ))
}

fn sanitize_api_rounds(rounds: &Value) -> Option<Value> {
    let Value::Array(items) = rounds else { return None };
    let mut out: Option<Vec<Value>> = None;
    for (position, item) in items.iter().enumerate() {
        let clean = match item {
            Value::Object(round) => match round.get("usage").and_then(sanitize_usage) {
                Some(usage) => {
                    let mut copy = round.clone();
                    copy.insert("usage".into(), usage);
                    Some(Value::Object(copy))
                }
                None => None,
            },
            _ => None,
        };
        match (&mut out, clean) {
            (Some(out), Some(clean)) => out.push(clean),
            (Some(out), None) => out.push(item.clone()),
            (None, Some(clean)) => {
                let mut list = items[..position].to_vec();
                list.push(clean);
                out = Some(list);
            }
            (None, None) => {}
        }
    }
    out.map(Value::Array)
}

/// `project_usage_container_for_storage` (copy-on-change).
pub fn project_usage_container(container: &Value) -> Option<Value> {
    let Value::Object(map) = container else { return None };
    let mut projected: Option<Map<String, Value>> = None;
    if let Some(usage) = map.get("usage").and_then(sanitize_usage) {
        projected.get_or_insert_with(|| map.clone()).insert("usage".into(), usage);
    }
    for field in API_ROUND_FIELDS {
        if let Some(rounds) = map.get(field).and_then(sanitize_api_rounds) {
            projected.get_or_insert_with(|| map.clone()).insert(field.into(), rounds);
        }
    }
    projected.map(Value::Object)
}

// ── event_envelope.py ────────────────────────────────────────────────────

const EVENT_FORMAT_VERSION: i64 = 1;

/// `stamp_event_format_envelope`; `Err` is the `EventFormatError` text.
pub fn stamp_event_format_envelope(payload: Value) -> Result<Value, String> {
    let Value::Object(mut map) = payload else { return Ok(payload) };
    if map.contains_key("v") || map.contains_key("ignorable") {
        return Err("event payload uses the reserved format-envelope namespace ('v'/'ignorable')".into());
    }
    map.insert("v".into(), Value::from(EVENT_FORMAT_VERSION));
    Ok(Value::Object(map))
}

/// `execution_event_time_ms`.
pub fn execution_event_time_ms(payload: &Value, fallback_ms: i64) -> Result<i64, String> {
    let invalid = || "invalid server execution clock".to_owned();
    let Value::Object(map) = payload else { return Ok(fallback_ms) };
    if !["executionAtMs", "executionOriginAtMs", "executionClockId"].iter().any(|key| map.contains_key(*key)) {
        return Ok(fallback_ms);
    }
    let timestamp = match map.get("executionAtMs").and_then(json::as_strict_int) {
        Some(value) if 0 < value && value <= 9_007_199_254_740_991 => value,
        _ => return Err(invalid()),
    };
    let origin = map.get("executionOriginAtMs");
    let clock_id = map.get("executionClockId");
    if map.get("type") == Some(&Value::from("delta")) && origin.is_none() && clock_id.is_none() {
        return Ok(timestamp);
    }
    let origin_ok = matches!(origin.and_then(json::as_strict_int), Some(value) if 0 < value && value <= timestamp);
    let clock_ok = matches!(clock_id, Some(Value::String(text)) if (1..=128).contains(&text.chars().count()));
    if !origin_ok || !clock_ok {
        return Err(invalid());
    }
    Ok(timestamp)
}

// ── request_inspector_projection.py ──────────────────────────────────────

/// `generated_request_inspector_projection.PROJECTION`.
pub const REQUEST_INSPECTOR_PROJECTION: &str = "request-inspector/v1";
const SNAPSHOT_FIELDS: [&str; 14] = [
    "type",
    "roundNum",
    "kind",
    "turn",
    "model",
    "params",
    "label",
    "agentId",
    "agentRole",
    "degraded",
    "degradedReason",
    "messageCount",
    "toolsCount",
    "prefixLen",
];

fn none_or_false(value: Option<&Value>) -> bool {
    matches!(value, None | Some(Value::Null) | Some(Value::Bool(false)))
}

pub fn project_request_inspector_event(event: Value) -> Value {
    let Value::Object(map) = &event else { return event };
    if map.get("type") != Some(&Value::from("messages_snapshot"))
        || !map.contains_key("prefixLen")
        || map.get("messageCount").and_then(json::as_strict_int).is_none()
        || map.get("toolsCount").and_then(json::as_strict_int).is_none()
    {
        return event;
    }
    let empty = Vec::new();
    let messages = match map.get("newMessages") {
        value if none_or_false(value) => &empty,
        Some(Value::Array(items)) => items,
        _ => return event,
    };
    let mut projected = Map::new();
    for key in SNAPSHOT_FIELDS {
        if let Some(value) = map.get(key) {
            projected.insert(key.to_owned(), value.clone());
        }
    }
    let mut tail = Vec::new();
    for message in messages {
        let Value::Object(message) = message else { continue };
        let calls = match message.get("tool_calls") {
            value if none_or_false(value) => &empty,
            Some(Value::Array(items)) => items,
            _ => return event,
        };
        let mut names = Vec::new();
        for call in calls {
            let Value::Object(call) = call else { continue };
            let name = match call.get("function") {
                Some(Value::Object(function)) => function.get("name").cloned().unwrap_or(Value::Null),
                _ => call.get("name").cloned().unwrap_or(Value::Null),
            };
            names.push(json::object(vec![("name", name)]));
        }
        let blocks: Vec<Value> = match message.get("content") {
            Some(Value::Array(content)) => content
                .iter()
                .filter_map(|block| match block {
                    Value::Object(block) if block.get("type") == Some(&Value::from("tool_use")) => {
                        Some(json::object(vec![
                            ("type", Value::from("tool_use")),
                            ("name", block.get("name").cloned().unwrap_or(Value::Null)),
                        ]))
                    }
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        };
        if !names.is_empty() || !blocks.is_empty() {
            tail.push(json::object(vec![("tool_calls", Value::Array(names)), ("content", Value::Array(blocks))]));
        }
    }
    projected.insert("newMessages".into(), Value::Array(tail));
    Value::Object(projected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn task_result_fields_round_trip_through_compression() {
        let content = "x".repeat(40_000);
        let value = json!({"task_id": "t", "content": content, "status": "done"});
        let stored = encode_task_result_document(&value).unwrap();
        let text = String::from_utf8(stored.clone()).unwrap();
        assert!(text.contains(TASK_RESULT_FIELD_CODEC_KEY));
        let loaded: Value = serde_json::from_slice(&stored).unwrap();
        let hydrated = decode_task_result_fields(loaded, None).unwrap();
        assert_eq!(hydrated["content"], Value::String(content));
    }

    #[test]
    fn task_events_compress_only_when_large() {
        let small = b"{\"type\":\"x\"}".to_vec();
        assert_eq!(encode_task_event_payload(small.clone()).unwrap(), small);
        let large = format!("{{\"text\":\"{}\"}}", "y".repeat(70_000)).into_bytes();
        let encoded = encode_task_event_payload(large.clone()).unwrap();
        assert!(encoded.starts_with(COMPRESSED_TASK_EVENT_MAGIC));
        let decoded = decode_task_event_payload(&crate::session::SqlValue::Blob(encoded)).unwrap();
        assert_eq!(decoded, large);
    }

    #[test]
    fn usage_projection_strips_wire_diagnostics() {
        let event = json!({"usage": {"a": 1, "_wire_fp": 2}, "apiRounds": [{"usage": {"_wire_x": 1}}, 3]});
        let projected = project_usage_container(&event).unwrap();
        assert_eq!(projected, json!({"usage": {"a": 1}, "apiRounds": [{"usage": {}}, 3]}));
        assert!(project_usage_container(&json!({"usage": {"a": 1}})).is_none());
    }
}
