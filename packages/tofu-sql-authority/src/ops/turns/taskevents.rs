//! Private ports of the task-event row writer a turn frame carries:
//! `operations_pkg/_records.py::_append_event_row` (with
//! `turn_publication_checked=True`), `task_event_codec.py`,
//! `lib/task_replay/persistence_projection.py` usage projection and
//! `lib/event_envelope.py` stamp/clock. Copied from `ops/core/{codecs,events}.rs`;
//! shared owner: core.

use flate2::{Decompress, FlushDecompress, Status};
use serde_json::{json, Map, Value};

use crate::codec::{dump, load};
use crate::error::{fail, OpResult, StorageError};
use crate::json;
use crate::params;
use crate::payload::{required_text_512, Payload};
use crate::session::{Session, SqlValue};

/// `lib/storage/protocol.py::MAX_FRAME_BYTES`.
const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
const TASK_STREAM_KIND: &str = "task";

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

// ── task_event_codec.py ──────────────────────────────────────────────────

const TASK_EVENT_COMPRESSION_MIN_BYTES: usize = 64 * 1024;
const MAX_DECODED_TASK_EVENT_BYTES: usize = MAX_FRAME_BYTES;
const MAX_STORED_TASK_EVENT_BYTES: usize = MAX_FRAME_BYTES;
const COMPRESSED_TASK_EVENT_MAGIC: &[u8] = b"tofu.task-event.zlib.v1\x00";
const TASK_EVENT_MAGIC_PREFIX: &[u8] = b"tofu.task-event.";

fn integrity(message: &str) -> StorageError {
    StorageError::new("database_integrity", message)
}

fn encode_task_event_payload(raw: Vec<u8>) -> Result<Vec<u8>, StorageError> {
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
fn payload_bytes(value: &crate::session::SqlValue) -> Result<&[u8], StorageError> {
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

fn decode_task_event_payload(value: &crate::session::SqlValue) -> Result<Vec<u8>, StorageError> {
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
fn project_usage_container(container: &Value) -> Option<Value> {
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
fn stamp_event_format_envelope(payload: Value) -> Result<Value, String> {
    let Value::Object(mut map) = payload else { return Ok(payload) };
    if map.contains_key("v") || map.contains_key("ignorable") {
        return Err("event payload uses the reserved format-envelope namespace ('v'/'ignorable')".into());
    }
    map.insert("v".into(), Value::from(EVENT_FORMAT_VERSION));
    Ok(Value::Object(map))
}

/// `execution_event_time_ms`.
fn execution_event_time_ms(payload: &Value, fallback_ms: i64) -> Result<i64, String> {
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

/// `_advance_event_read_guard`.
fn advance_event_read_guard(session: &Session<'_>, task_id: &str, appends: i64, prunes: i64) -> OpResult<()> {
    session.execute(
        "INSERT INTO storage_event_read_guards (task_id, source_id, append_version, prune_version) VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET append_version = storage_event_read_guards.append_version + excluded.append_version, prune_version = storage_event_read_guards.prune_version + excluded.prune_version",
        &params![task_id, uuid::Uuid::new_v4().simple().to_string(), appends, prunes],
    )?;
    Ok(())
}

fn event_string_field(event: &Value, field: &str) -> String {
    match event {
        Value::Object(map) => super::core::text(crate::payload::get(map, field)).chars().take(128).collect(),
        _ => String::new(),
    }
}

/// `_append_event_row(session, payload, turn_publication_checked=True)`.
pub fn append_event_row(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text_512(payload, "task_id")?;
    let sequence = match payload.get("sequence").and_then(json::as_strict_int) {
        Some(sequence) if sequence >= 0 => sequence,
        _ => return fail("database_protocol_error", "Invalid event sequence"),
    };
    let raw_event = payload.get("event").cloned().unwrap_or(Value::Null);
    let event = project_usage_container(&raw_event).unwrap_or(raw_event);
    let event = stamp_event_format_envelope(event).map_err(StorageError::protocol)?;
    let event_timestamp = execution_event_time_ms(&event, super::core::now_ms()).map_err(StorageError::protocol)?;
    let raw_encoded = dump(&event)?;
    let encoded = encode_task_event_payload(raw_encoded.clone())?;
    let stream_value = payload.get("stream_kind").unwrap_or(&Value::Null);
    let stream_kind =
        if json::truthy(stream_value) { json::python_str(stream_value) } else { TASK_STREAM_KIND.to_owned() };
    if stream_kind != TASK_STREAM_KIND {
        return fail(
            "database_protocol_error",
            "event.append only accepts task streams; Project Brain uses its transactional semantic command boundary",
        );
    }
    let event_type = event_string_field(&event, "type");
    let event_kind = event_string_field(&event, "kind");
    if let Some(require_live) = payload.get("require_attempts_live").filter(|value| !value.is_null()) {
        let attempts = match require_live {
            Value::Array(items)
                if (1..=2).contains(&items.len())
                    && items
                        .iter()
                        .all(|item| matches!(item, Value::String(id) if (1..=128).contains(&id.chars().count()))) =>
            {
                items.iter().map(|item| item.as_str().expect("attempt id").to_owned()).collect::<Vec<_>>()
            }
            _ => return fail("database_protocol_error", "Invalid require_attempts_live fence"),
        };
        let placeholders = vec!["?"; attempts.len()].join(", ");
        let parameters: Vec<SqlValue> = attempts.iter().map(SqlValue::from).collect();
        let rows = session.fetch_all(
            &format!("SELECT attempt_id, status FROM storage_generation_attempts WHERE attempt_id IN ({placeholders})"),
            &parameters,
        )?;
        let live: Vec<String> = rows
            .iter()
            .filter(
                |row| matches!(row.get("status"), SqlValue::Text(status) if status == "pending" || status == "running"),
            )
            .filter_map(|row| row.get("attempt_id").as_str().map(str::to_owned))
            .collect();
        if attempts.iter().any(|attempt| !live.contains(attempt)) {
            return Ok(
                json!({"inserted": false, "rejected": "attempt_not_live", "task_id": task_id, "sequence": sequence}),
            );
        }
    }
    let count = session.execute(
        "INSERT INTO storage_events(task_id, sequence, stream_kind, event_type, event_kind, event_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, sequence) DO NOTHING",
        &params![task_id.clone(), sequence, stream_kind, event_type, event_kind, encoded, event_timestamp],
    )?;
    if count == 0 {
        let row = session.fetch_one(
            "SELECT event_json FROM storage_events WHERE task_id = ? AND sequence = ?",
            &params![task_id.clone(), sequence],
        )?;
        let existing = match row {
            None => None,
            Some(row) => {
                let decoded = decode_task_event_payload(row.get("event_json"))?;
                Some(dump(&load(&SqlValue::Blob(decoded))?)?)
            }
        };
        if existing.as_deref() != Some(raw_encoded.as_slice()) {
            return fail("database_conflict", "Event sequence has a conflicting payload");
        }
    }
    if count > 0 {
        advance_event_read_guard(session, &task_id, 1, 0)?;
    }
    Ok(json!({"inserted": count > 0, "task_id": task_id, "sequence": sequence}))
}
