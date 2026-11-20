//! Private port of `lib/storage_authority_sql/task_result_field_codec.py`
//! (`decode_task_result_fields_from_storage`) and of
//! `operations_pkg/_records.py::{_record_get, _encoded_task_result_document}`
//! for the `task_results` namespace; shared owner: core (records).

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use serde_json::{json, Map, Value};

use crate::codec;
use crate::error::{fail, OpResult};
use crate::json;
use crate::ops::conversations::legacy::{zlib_compress_level1, zlib_inflate_exact};
use crate::ops::conversations::support::{load, py_int};
use crate::params;
use crate::session::Session;

const CODEC_KEY: &str = "_tofuTaskResultFieldCodec";
const CODEC_VERSION: i64 = 1;
const COMPRESSION_MIN_BYTES: usize = 32 * 1024;
const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
const ENCODING: &str = "zlib-base64";
const COMPRESSIBLE_FIELDS: &[&str] = &["segments", "metadata", "tool_rounds", "content", "thinking", "error"];

const PYTHON_B64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_allow_trailing_bits(true)
        .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
);

fn decoded_text_field(
    candidate: &Map<String, Value>,
    decoded_budget: usize,
    payload_budget: usize,
) -> Result<(String, usize, usize), ()> {
    if candidate.len() != 1 {
        return Err(());
    }
    let Some(Value::Object(envelope)) = candidate.get(CODEC_KEY) else { return Err(()) };
    let fields = ["version", "encoding", "decodedBytes", "payload"];
    if envelope.len() != fields.len() || !fields.iter().all(|field| envelope.contains_key(*field)) {
        return Err(());
    }
    if json::as_strict_int(&envelope["version"]) != Some(CODEC_VERSION) {
        return Err(());
    }
    if envelope["encoding"] != Value::String(ENCODING.into()) {
        return Err(());
    }
    let Some(decoded_bytes) = json::as_strict_int(&envelope["decodedBytes"])
        .filter(|size| *size >= COMPRESSION_MIN_BYTES as i64 && *size <= MAX_FRAME_BYTES as i64)
        .map(|size| size as usize)
    else {
        return Err(());
    };
    if decoded_bytes > decoded_budget {
        return Err(());
    }
    let Value::String(payload) = &envelope["payload"] else { return Err(()) };
    if payload.is_empty() || payload.chars().count() > MAX_FRAME_BYTES || !payload.is_ascii() {
        return Err(());
    }
    if payload.len() > payload_budget {
        return Err(());
    }
    let compressed = PYTHON_B64.decode(payload.as_bytes()).map_err(|_| ())?;
    if compressed.is_empty() {
        return Err(());
    }
    let source = zlib_inflate_exact(&compressed, decoded_bytes)?;
    let text = String::from_utf8(source).map_err(|_| ())?;
    Ok((text, decoded_bytes, payload.len()))
}

/// `decode_task_result_fields_from_storage(value)` (all fields).
pub fn decode_task_result_fields(value: Value) -> Result<Value, ()> {
    let Value::Object(map) = value else { return Ok(value) };
    let mut decoded = map.clone();
    let mut decoded_bytes = 0usize;
    let mut payload_bytes = 0usize;
    for field in COMPRESSIBLE_FIELDS {
        let Some(Value::Object(candidate)) = map.get(*field) else { continue };
        if !candidate.contains_key(CODEC_KEY) {
            continue;
        }
        let (text, decoded_size, payload_size) =
            decoded_text_field(candidate, MAX_FRAME_BYTES - decoded_bytes, MAX_FRAME_BYTES - payload_bytes)?;
        decoded.insert((*field).into(), Value::String(text));
        decoded_bytes += decoded_size;
        payload_bytes += payload_size;
    }
    Ok(Value::Object(decoded))
}

fn encoded_text_field(text: &str) -> Result<Option<Value>, ()> {
    let source = text.as_bytes();
    if source.len() > MAX_FRAME_BYTES {
        return Err(());
    }
    if source.len() < COMPRESSION_MIN_BYTES {
        return Ok(None);
    }
    let payload = base64::engine::general_purpose::STANDARD.encode(zlib_compress_level1(source));
    if payload.len() > MAX_FRAME_BYTES {
        return Ok(None);
    }
    let envelope = json!({CODEC_KEY: {
        "version": CODEC_VERSION,
        "encoding": ENCODING,
        "decodedBytes": source.len(),
        "payload": payload,
    }});
    Ok(if json::dumps_sorted(&envelope).len() < source.len() { Some(envelope) } else { None })
}

/// `_encoded_task_result_document(value)`.
pub fn encoded_task_result_document(value: &Value) -> OpResult<Vec<u8>> {
    let invalid = || fail("database_protocol_error", "Task result storage encoding is invalid");
    let Value::Object(map) = value else { return codec::dump(value) };
    let mut stored = map.clone();
    for field in COMPRESSIBLE_FIELDS {
        match map.get(*field) {
            Some(Value::Object(candidate)) if candidate.contains_key(CODEC_KEY) => return invalid(),
            Some(Value::String(text)) => match encoded_text_field(text) {
                Ok(Some(envelope)) => {
                    stored.insert((*field).into(), envelope);
                }
                Ok(None) => {}
                Err(()) => return invalid(),
            },
            _ => {}
        }
    }
    match codec::encode_projection(&Value::Object(stored.clone())) {
        Ok(Some(encoded)) => Ok(json::dumps_sorted(&encoded)),
        Ok(None) => Ok(json::dumps_sorted(&Value::Object(stored))),
        Err(codec::ReservedCodecKey) => invalid(),
    }
}

/// `_record_get(session, {'namespace': 'task_results', 'key': key})`.
pub fn task_result_get(session: &Session<'_>, key: &str) -> OpResult<Option<Map<String, Value>>> {
    if key.is_empty() || key.chars().count() > 512 {
        return fail("database_protocol_error", "Invalid key in storage request");
    }
    let Some(row) = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records \
         WHERE namespace = ? AND record_key = ?",
        &params!["task_results", key],
    )?
    else {
        return Ok(None);
    };
    let value = match decode_task_result_fields(load(row.get("value_json"))?) {
        Ok(value) => value,
        Err(()) => return fail("database_integrity", "Stored task result field encoding is invalid"),
    };
    let mut record = Map::new();
    record.insert("value".into(), value);
    record.insert("version".into(), Value::from(py_int(row.get("version"))?));
    record.insert("updated_at_ms".into(), Value::from(py_int(row.get("updated_at_ms"))?));
    Ok(Some(record))
}
