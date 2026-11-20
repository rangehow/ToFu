//! `operations_pkg/_raw_archives.py`: owner-scoped durable provider
//! request/response archives (`raw_archive.*`) and the stored-bytes counter
//! that turn deletion debits.

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use serde_json::{json, Value};

use super::core::{now_ms, text, Obj};
use crate::codec::{dump, load};
use crate::error::{fail, OpResult};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const MAX_ARCHIVE_STORED_BYTES: usize = 16 * 1024 * 1024;
const MAX_ARCHIVE_DECODED_PART_BYTES: usize = 64 * 1024 * 1024;
const MAX_GLOBAL_BUDGET_BYTES: i64 = 4 * 1024 * 1024 * 1024;
const MAX_READ_CHUNK_BYTES: i64 = 1024 * 1024;
/// `lib.raw_archive_contract.RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES`.
const RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES: i64 = 1_000_000_000_000_000;
const USAGE_META_KEY: &str = "raw_archive_stored_bytes";
const METADATA_COLUMNS: &str = "archive_id,user_id,conversation_id,turn_id,attempt_id,task_id,round_num,transport_attempt,request_bytes,response_bytes,stored_bytes,request_sha256,response_sha256,integrity,truncation_reason,summary_json,created_at_ms,LENGTH(request_blob)>0 AS request_blob,LENGTH(response_blob)>0 AS response_blob";
const TRUNCATION_REASONS: &[&str] =
    &["", "attempt_limit", "quota_exhausted", "secret_scrubbed", "transport_interrupted"];

fn compressed_part(payload: &Payload, key: &str) -> OpResult<Vec<u8>> {
    let Some(Value::String(encoded)) = payload.get(key) else {
        return fail("database_protocol_error", format!("Invalid {key} in raw archive request"));
    };
    let engine = GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_allow_trailing_bits(true)
            .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
    );
    let Ok(value) = engine.decode(encoded) else {
        return fail("database_protocol_error", format!("Invalid {key} in raw archive request"));
    };
    if value.len() > MAX_ARCHIVE_STORED_BYTES {
        return fail("database_protocol_error", "Raw archive part exceeds its byte budget");
    }
    Ok(value)
}

fn digest(payload: &Payload, key: &str) -> OpResult<String> {
    let value = required_text(payload, key, 64)?;
    if value.len() != 64 || !value.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')) {
        return fail("database_protocol_error", format!("Invalid {key} in raw archive request"));
    }
    Ok(value)
}

fn summary(payload: &Payload) -> OpResult<Obj> {
    let value = get(payload, "summary");
    let result = match value {
        _ if !crate::json::truthy(value) => Obj::new(),
        Value::Object(map) => map.clone(),
        _ => return fail("database_protocol_error", "Invalid raw archive summary"),
    };
    if result.len() > 32 || dump(&Value::Object(result.clone()))?.len() > 64 * 1024 {
        return fail("database_protocol_error", "Raw archive summary exceeds its budget");
    }
    Ok(result)
}

fn stored_bytes_usage(session: &Session<'_>) -> OpResult<i64> {
    if let Some(row) =
        session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key=?", &params![USAGE_META_KEY])?
    {
        return row.int("meta_value");
    }
    let total = session
        .fetch_one("SELECT COALESCE(SUM(stored_bytes),0) AS total FROM storage_raw_archives", &[])?
        .map(|row| row.opt_int("total").unwrap_or(0))
        .unwrap_or(0);
    session.execute(
        "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)",
        &params![USAGE_META_KEY, total.to_string()],
    )?;
    Ok(total)
}

fn charge_stored_bytes_usage(session: &Session<'_>, delta: i64) -> OpResult<()> {
    let value = (stored_bytes_usage(session)? + delta).to_string();
    session.execute("UPDATE storage_meta SET meta_value=? WHERE meta_key=?", &params![value, USAGE_META_KEY])?;
    Ok(())
}

/// `_debit_stored_bytes_for_delete(session, where, params)`.
pub fn debit_stored_bytes_for_delete(session: &Session<'_>, where_clause: &str, args: &[SqlValue]) -> OpResult<()> {
    let total = session
        .fetch_one(
            &format!("SELECT COALESCE(SUM(stored_bytes),0) AS total FROM storage_raw_archives WHERE {where_clause}"),
            args,
        )?
        .map(|row| row.opt_int("total").unwrap_or(0))
        .unwrap_or(0);
    if total != 0 {
        charge_stored_bytes_usage(session, -total)?;
    }
    Ok(())
}

fn public_metadata(row: &Row) -> OpResult<Value> {
    let loaded = load(row.get("summary_json"))?;
    let summary = if crate::json::truthy(&loaded) { loaded } else { Value::Object(Obj::new()) };
    let Value::Object(summary) = summary else {
        return fail("database_integrity", "Stored raw archive summary is malformed");
    };
    let request_bytes = row.opt_int("request_bytes").unwrap_or(0);
    let response_bytes = row.opt_int("response_bytes").unwrap_or(0);
    let combined = get(&summary, "combinedSha256");
    Ok(json!({
        "archiveId": row.get("archive_id").text_or_empty(),
        "attemptId": row.get("attempt_id").text_or_empty(),
        "taskId": row.get("task_id").text_or_empty(),
        "turnId": row.get("turn_id").text_or_empty(),
        "roundNum": row.int("round_num")?,
        "transportAttempt": row.int("transport_attempt")?,
        "summary": if crate::json::truthy(get(&summary, "text")) { text(get(&summary, "text")) } else { "Provider request/response".to_owned() },
        "byteCount": request_bytes + response_bytes,
        "requestBytes": request_bytes,
        "responseBytes": response_bytes,
        "storedBytes": row.opt_int("stored_bytes").unwrap_or(0),
        "requestSha256": row.get("request_sha256").text_or_empty(),
        "responseSha256": row.get("response_sha256").text_or_empty(),
        "sha256": if crate::json::truthy(combined) { text(combined) } else { row.get("response_sha256").text_or_empty() },
        "integrity": row.get("integrity").text_or_empty(),
        "truncationReason": row.text("truncation_reason"),
        "requestAvailable": row.get("request_blob").truthy(),
        "responseAvailable": row.get("response_blob").truthy(),
        "createdAt": row.int("created_at_ms")?,
        "details": Value::Object(summary),
    }))
}

fn with_replay(mut metadata: Value, replay: bool) -> Value {
    metadata["idempotentReplay"] = Value::Bool(replay);
    metadata
}

/// `_raw_archive_put`.
pub fn raw_archive_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let archive_id = required_text(payload, "archive_id", 160)?;
    let conversation_id = required_text(payload, "conversation_id", 128)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let round_num = integer(payload, "round_num", None, Some(1), Some(1_000_000))?;
    let transport_attempt = integer(payload, "transport_attempt", Some(0), Some(0), Some(10_000))?;
    let request_bytes = integer(payload, "request_bytes", None, Some(0), Some(1_000_000_000))?;
    let response_bytes = integer(payload, "response_bytes", None, Some(0), Some(1_000_000_000))?;
    let request_sha256 = digest(payload, "request_sha256")?;
    let response_sha256 = digest(payload, "response_sha256")?;
    let mut integrity = required_text(payload, "integrity", 16)?;
    if integrity != "complete" && integrity != "partial" {
        return fail("database_protocol_error", "Invalid raw archive integrity");
    }
    let mut truncation_reason = text(get(payload, "truncation_reason"));
    if !TRUNCATION_REASONS.contains(&truncation_reason.as_str()) {
        return fail("database_protocol_error", "Invalid raw archive truncation reason");
    }
    let summary = summary(payload)?;
    let mut request_blob = compressed_part(payload, "request_blob_b64")?;
    let mut response_blob = compressed_part(payload, "response_blob_b64")?;
    let mut requested = (request_blob.len() + response_blob.len()) as i64;
    if requested > MAX_ARCHIVE_STORED_BYTES as i64 {
        return fail("database_protocol_error", "Raw archive exceeds its per-attempt budget");
    }
    let budget = integer(payload, "budget_bytes", None, Some(1), Some(MAX_GLOBAL_BUDGET_BYTES))?;
    let min_free = integer(payload, "min_free_bytes", Some(0), Some(0), Some(RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES))?;
    let available_free =
        integer(payload, "available_free_bytes", Some(0), Some(0), Some(RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES))?;
    let existing = session.fetch_one(
        &format!("SELECT {METADATA_COLUMNS} FROM storage_raw_archives WHERE archive_id=?"),
        &params![archive_id.clone()],
    )?;
    if let Some(existing) = existing {
        if existing.get("attempt_id").text_or_empty() != attempt_id
            || existing.get("request_sha256").text_or_empty() != request_sha256
            || existing.get("response_sha256").text_or_empty() != response_sha256
        {
            return fail("database_conflict", "Raw archive identity has conflicting content");
        }
        return Ok(with_replay(public_metadata(&existing)?, true));
    }
    let owner = session.fetch_one(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND a.task_id=? AND a.conversation_id=? AND a.turn_id=? AND t.user_id=?",
        &params![attempt_id.clone(), task_id.clone(), conversation_id.clone(), turn_id.clone(), user_id],
    )?;
    if owner.is_none() {
        return fail("database_not_found", "Raw archive owner not found");
    }
    let current_usage = stored_bytes_usage(session)?;
    let attempt_usage = session
        .fetch_one(
            "SELECT COALESCE(SUM(stored_bytes),0) AS total FROM storage_raw_archives WHERE attempt_id=?",
            &params![attempt_id.clone()],
        )?
        .map(|row| row.opt_int("total").unwrap_or(0))
        .unwrap_or(0);
    let attempt_limit_exhausted = attempt_usage + requested > MAX_ARCHIVE_STORED_BYTES as i64;
    let budget_exhausted = current_usage + requested > budget;
    let space_exhausted = available_free > 0 && available_free - requested < min_free;
    let scrub = if space_exhausted || requested > budget {
        "quota_exhausted"
    } else if attempt_limit_exhausted {
        "attempt_limit"
    } else if budget_exhausted {
        "quota_exhausted"
    } else {
        ""
    };
    if !scrub.is_empty() {
        request_blob.clear();
        response_blob.clear();
        requested = 0;
        integrity = "partial".into();
        truncation_reason = scrub.into();
    }
    let created_at = now_ms();
    let summary_bytes = dump(&Value::Object(summary))?;
    session.execute(
        "INSERT INTO storage_raw_archives(archive_id,user_id,conversation_id,turn_id,attempt_id,task_id,round_num,transport_attempt,request_blob,response_blob,request_bytes,response_bytes,stored_bytes,request_sha256,response_sha256,integrity,truncation_reason,summary_json,created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        &params![
            archive_id.clone(),
            user_id,
            conversation_id.clone(),
            turn_id.clone(),
            attempt_id.clone(),
            task_id.clone(),
            round_num,
            transport_attempt,
            request_blob.clone(),
            response_blob.clone(),
            request_bytes,
            response_bytes,
            requested,
            request_sha256.clone(),
            response_sha256.clone(),
            integrity.clone(),
            truncation_reason.clone(),
            summary_bytes.clone(),
            created_at
        ],
    )?;
    charge_stored_bytes_usage(session, requested)?;
    let mut row = Row::default();
    for (column, value) in [
        ("archive_id", SqlValue::Text(archive_id)),
        ("user_id", SqlValue::Integer(user_id)),
        ("conversation_id", SqlValue::Text(conversation_id)),
        ("turn_id", SqlValue::Text(turn_id)),
        ("attempt_id", SqlValue::Text(attempt_id)),
        ("task_id", SqlValue::Text(task_id)),
        ("round_num", SqlValue::Integer(round_num)),
        ("transport_attempt", SqlValue::Integer(transport_attempt)),
        ("request_blob", SqlValue::Blob(request_blob)),
        ("response_blob", SqlValue::Blob(response_blob)),
        ("request_bytes", SqlValue::Integer(request_bytes)),
        ("response_bytes", SqlValue::Integer(response_bytes)),
        ("stored_bytes", SqlValue::Integer(requested)),
        ("request_sha256", SqlValue::Text(request_sha256)),
        ("response_sha256", SqlValue::Text(response_sha256)),
        ("integrity", SqlValue::Text(integrity)),
        ("truncation_reason", SqlValue::Text(truncation_reason)),
        ("summary_json", SqlValue::Blob(summary_bytes)),
        ("created_at_ms", SqlValue::Integer(created_at)),
    ] {
        row.set(column, value);
    }
    Ok(with_replay(public_metadata(&row)?, false))
}

/// `_raw_archive_list`.
pub fn raw_archive_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let limit = integer(payload, "limit", Some(16), Some(1), Some(64))?;
    let mut sql = String::from("SELECT * FROM storage_raw_archives WHERE user_id=? AND task_id=?");
    let mut args = vec![SqlValue::Integer(user_id), SqlValue::Text(task_id)];
    if payload.contains_key("round_num") {
        let round_num = integer(payload, "round_num", None, Some(1), Some(1_000_000))?;
        sql.push_str(" AND round_num=?");
        args.push(SqlValue::Integer(round_num));
    }
    sql.push_str(" ORDER BY created_at_ms,archive_id LIMIT ?");
    args.push(SqlValue::Integer(limit));
    let rows = session.fetch_all(&sql, &args)?;
    Ok(json!({"archives": rows.iter().map(public_metadata).collect::<OpResult<Vec<_>>>()?}))
}

fn decode_part(value: &SqlValue) -> OpResult<Vec<u8>> {
    let bytes: Vec<u8> = match value {
        SqlValue::Blob(bytes) => bytes.clone(),
        SqlValue::Text(text) => {
            let mut latin1 = Vec::with_capacity(text.len());
            for character in text.chars() {
                if (character as u32) > 0xFF {
                    return crate::error::internal("UnicodeEncodeError: latin-1");
                }
                latin1.push(character as u32 as u8);
            }
            latin1
        }
        _ => return fail("database_integrity", "Stored raw archive body has invalid type"),
    };
    if bytes.is_empty() {
        return Ok(vec![]);
    }
    let mut decoder = flate2::Decompress::new(true);
    let mut out: Vec<u8> = Vec::new();
    let limit = MAX_ARCHIVE_DECODED_PART_BYTES + 1;
    let mut eof = false;
    loop {
        if out.len() >= limit {
            break;
        }
        let room = (limit - out.len()).min(1 << 20);
        out.reserve(room);
        let consumed = decoder.total_in() as usize;
        let produced = out.len();
        let status = match decoder.decompress_vec(&bytes[consumed..], &mut out, flate2::FlushDecompress::None) {
            Ok(status) => status,
            Err(_) => return fail("database_integrity", "Stored raw archive body is corrupt"),
        };
        if out.len() > limit {
            out.truncate(limit);
        }
        if status == flate2::Status::StreamEnd {
            eof = true;
            break;
        }
        if decoder.total_in() as usize == consumed && out.len() == produced {
            break;
        }
    }
    let unused = (decoder.total_in() as usize) < bytes.len();
    if out.len() > MAX_ARCHIVE_DECODED_PART_BYTES || !eof || unused {
        return fail("database_integrity", "Stored raw archive body is malformed");
    }
    Ok(out)
}

/// `_raw_archive_read`.
pub fn raw_archive_read(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let archive_id = required_text(payload, "archive_id", 160)?;
    let part = required_text(payload, "part", 16)?;
    if part != "request" && part != "response" {
        return fail("database_protocol_error", "Invalid raw archive part");
    }
    let offset = integer(payload, "offset", Some(0), Some(0), Some(MAX_ARCHIVE_DECODED_PART_BYTES as i64))?;
    let limit = integer(payload, "limit", Some(256 * 1024), Some(1), Some(MAX_READ_CHUNK_BYTES))?;
    let Some(row) = session.fetch_one(
        "SELECT * FROM storage_raw_archives WHERE archive_id=? AND user_id=? AND task_id=?",
        &params![archive_id, user_id, task_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let raw = decode_part(row.require(&format!("{part}_blob"))?)?;
    let end = (raw.len() as i64).min(offset + limit);
    let chunk: &[u8] = if (offset as usize) < raw.len() { &raw[offset as usize..end as usize] } else { &[] };
    Ok(json!({
        "archive": public_metadata(&row)?,
        "part": part,
        "offset": offset,
        "nextOffset": end,
        "hasMore": end < raw.len() as i64,
        "availableBytes": raw.len(),
        "dataBase64": base64::engine::general_purpose::STANDARD.encode(chunk),
    }))
}
