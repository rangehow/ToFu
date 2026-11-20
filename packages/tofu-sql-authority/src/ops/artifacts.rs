//! `operation_domains/artifacts.py`: conversation artifacts and retained
//! tool-result artifacts (`operations_pkg/_artifacts.py`,
//! `operations_pkg/_tool_result_query.py`).

mod generated_unicode_casefold;
pub mod pyjson;
mod tool_result_query;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};
use tool_result_query::{limit, media_type, query_result, validate_manifest};

pub fn register(registry: &mut Registry) {
    add(registry, "artifact.create", OperationSpec::command(true, artifact_create));
    add(registry, "artifact.get", OperationSpec::query(artifact_get));
    add(registry, "artifact.list", OperationSpec::query(artifact_list));
    add(registry, "artifact.delete", OperationSpec::command(true, artifact_delete));
    add(registry, "artifact.versions", OperationSpec::query(artifact_versions));
    add(registry, "artifact.library", OperationSpec::query(artifact_library));
    add(registry, "artifact.pin", OperationSpec::command(true, artifact_pin));
    add(registry, "tool_result_artifact.put", OperationSpec::command(true, tool_result_artifact_put));
    add(registry, "tool_result_artifact.read", OperationSpec::query(tool_result_artifact_read));
    add(registry, "tool_result_artifact.search", OperationSpec::query(tool_result_artifact_search));
    add(registry, "tool_result_artifact.prune", OperationSpec::maintenance(tool_result_artifact_prune));
}

// private port of lib/storage_authority_sql/operations_pkg/_runs.py::_optional_text; shared owner: workflows
fn optional_text(payload: &Payload, field: &str, maximum: usize, scope: &str) -> OpResult<String> {
    match payload.get(field) {
        None => Ok(String::new()),
        Some(Value::String(value)) if char_len(value) <= maximum => Ok(value.clone()),
        Some(_) => Err(StorageError::protocol(format!("Invalid {scope} {field}")).into()),
    }
}

// private port of lib/storage_authority_sql/operations_pkg/_runs.py::_json_text; shared owner: workflows
fn json_text(value: &Value) -> OpResult<String> {
    Ok(String::from_utf8(codec::dump(value)?).expect("orjson output is UTF-8"))
}

/// Python `str.strip()` (whitespace per `str.isspace`).
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

const TOOL_RESULT_TTL_MAX_MS: i64 = 7 * 24 * 60 * 60 * 1000;

const ARTIFACT_COLUMNS: &str = "id, conv_id, task_id, msg_id, source, source_ref, format, title, \
content, content_sha256, size_bytes, version, parent_id, pinned, meta, created_at";

/// `tool_result_query_contract.retention_limits`:
/// `runtime_guards.resolve_resource_budget('TOFU_BROWSER_STAGING_MAX_MIB')`.
/// The launcher materializes the adaptive value in the environment; without
/// it the personal default is 1% of the data volume's free space (64..2048
/// MiB), the 256 MiB fallback when unobservable, 4096 MiB distributed.
fn retention_limits() -> (i64, i64) {
    let global_cap = limit("globalBytes");
    let maximum_mib = global_cap / (1024 * 1024);
    let configured = std::env::var("TOFU_BROWSER_STAGING_MAX_MIB")
        .ok()
        .and_then(|raw| if raw.trim().is_empty() { None } else { raw.trim().parse::<i64>().ok() })
        .unwrap_or(0);
    let mib = if configured > 0 {
        configured
    } else {
        let mode = std::env::var("TOFU_DEPLOYMENT_MODE").unwrap_or_default().trim().to_lowercase();
        if mode == "distributed" {
            4096
        } else {
            disk_free_mib().map(|free| ((free as f64 * 0.01) as i64).clamp(64, 2048)).unwrap_or(256)
        }
    };
    let mib = mib.clamp(1, maximum_mib.max(1));
    let global_bytes = global_cap.min(mib * 1024 * 1024);
    (limit("ownerBytes").min(global_bytes), global_bytes)
}

fn disk_free_mib() -> Option<i64> {
    let path = std::env::var("TOFU_DATA_DIR").ok().filter(|path| !path.is_empty()).unwrap_or_else(|| ".".into());
    let path = std::ffi::CString::new(path).ok()?;
    let mut stats: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: `path` is NUL-terminated and `stats` is a valid out pointer.
    if unsafe { libc::statvfs(path.as_ptr(), &mut stats) } != 0 {
        return None;
    }
    Some((stats.f_bavail as u128 * stats.f_frsize as u128 / (1024 * 1024)) as i64)
}

/// `_canonical_manifest_digest_input`.
fn canonical_manifest_digest_input(content: &str) -> Option<String> {
    let Ok(Value::Object(mut value)) = serde_json::from_str::<Value>(content) else { return None };
    value.shift_remove("resultId")?;
    Some(pyjson::dumps(&Value::Object(value), true))
}

/// `_tool_result_content_digest`.
fn tool_result_content_digest(content: &str, media: &str) -> String {
    if media == media_type() {
        if let Some(canonical) = canonical_manifest_digest_input(content) {
            return hex::encode(Sha256::digest(canonical.as_bytes()));
        }
    }
    hex::encode(Sha256::digest(content.as_bytes()))
}

fn random_cursor_key() -> String {
    // `secrets.token_hex(32)`: 32 bytes from the OS CSPRNG.
    let mut bytes = [0u8; 32];
    let filled = std::fs::File::open("/dev/urandom")
        .and_then(|mut file| std::io::Read::read_exact(&mut file, &mut bytes))
        .is_ok();
    if !filled {
        bytes[..16].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
        bytes[16..].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
    }
    hex::encode(bytes)
}

/// `_tool_result_artifact_put`.
fn tool_result_artifact_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let max_bytes = limit("artifactBytes");
    let content = optional_text(payload, "content", max_bytes as usize, "tool_result_artifact")?;
    let size = content.len() as i64;
    if size > max_bytes {
        return fail("database_protocol_error", "Tool artifact is too large");
    }
    let created = integer(payload, "created_at_ms", None, Some(1), None)?;
    let expires = integer(payload, "expires_at_ms", None, Some(created + 1), None)?;
    if expires - created > TOOL_RESULT_TTL_MAX_MS {
        return fail("database_protocol_error", "Tool artifact TTL is too long");
    }
    let raw_media_type = optional_text(payload, "media_type", 128, "tool_result_artifact")?;
    let media = match py_strip(&raw_media_type) {
        "" => "text/plain".to_owned(),
        stripped => stripped.to_owned(),
    };
    let digest = tool_result_content_digest(&content, &media);
    if media == media_type() {
        validate_manifest(&content)?;
    }
    session.lock_key("tool_result_artifact", &format!("{user_id}:{digest}"));
    session.lock_key("tool_result_artifact_quota", "global");
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO NOTHING",
        &params!["tool_result_private", "cursor-key-v1", format!("\"{}\"", random_cursor_key()), 1i64, created],
    )?;
    prune(session, created, 128)?;
    let existing = session.fetch_one(
        "SELECT media_type FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?",
        &params![user_id, &digest],
    )?;
    if let Some(existing) = &existing {
        if existing.get("media_type").as_str() != Some(media.as_str()) {
            return fail("database_protocol_error", "Artifact media type conflict");
        }
    }
    if existing.is_none() {
        let (owner_bytes, global_bytes) = retention_limits();
        let caps: [(&str, Vec<SqlValue>, i64, i64); 2] = [
            ("WHERE user_id=?", vec![user_id.into()], owner_bytes, limit("ownerRows")),
            ("", vec![], global_bytes, limit("globalRows")),
        ];
        for (predicate, values, byte_cap, row_cap) in caps {
            let usage = session.fetch_one(
                &format!(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes FROM tool_result_artifacts {predicate}"
                ),
                &values,
            )?;
            let Some(usage) = usage else { return crate::error::internal("aggregate returned no row") };
            if usage.int("n")? >= row_cap || usage.int("bytes")? + size > byte_cap {
                return fail("storage_payload_too_large", "Tool result persistence quota exceeded");
            }
        }
    }
    session.execute(
        "INSERT INTO tool_result_artifacts(\
user_id, content_sha256, content, media_type, size_bytes, \
created_at_ms, expires_at_ms, last_accessed_at_ms) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?) \
ON CONFLICT(user_id, content_sha256) DO UPDATE SET \
expires_at_ms=CASE WHEN tool_result_artifacts.expires_at_ms \
> excluded.expires_at_ms THEN tool_result_artifacts.expires_at_ms \
ELSE excluded.expires_at_ms END, \
last_accessed_at_ms=CASE WHEN \
tool_result_artifacts.last_accessed_at_ms \
> excluded.last_accessed_at_ms THEN \
tool_result_artifacts.last_accessed_at_ms \
ELSE excluded.last_accessed_at_ms END",
        &params![user_id, &digest, &content, &media, size, created, expires, created],
    )?;
    let effective = session.fetch_one(
        "SELECT expires_at_ms FROM tool_result_artifacts WHERE user_id = ? AND content_sha256 = ?",
        &params![user_id, &digest],
    )?;
    let Some(effective) = effective else {
        return fail("database_integrity", "Tool artifact insert was not visible");
    };
    Ok(json!({
        "artifactRef": format!("tool-result:{digest}"),
        "contentSha256": digest,
        "sizeBytes": size,
        "expiresAtMs": effective.int("expires_at_ms")?,
    }))
}

fn tool_result_artifact_read(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    query_result(session, payload, "read")
}

fn tool_result_artifact_search(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    query_result(session, payload, "search")
}

fn prune(session: &Session<'_>, now: i64, limit: i64) -> OpResult<Value> {
    let rows = session.fetch_all(
        "SELECT user_id, content_sha256 FROM tool_result_artifacts WHERE expires_at_ms <= ? ORDER BY expires_at_ms LIMIT ?",
        &params![now, limit],
    )?;
    let mut deleted = 0usize;
    for row in &rows {
        deleted += session.execute(
            "DELETE FROM tool_result_artifacts WHERE user_id = ? AND content_sha256 = ? AND expires_at_ms <= ?",
            &params![row.get("user_id"), row.get("content_sha256"), now],
        )?;
    }
    Ok(json!({"deleted": deleted, "hasMore": rows.len() as i64 == limit}))
}

/// `_tool_result_artifact_prune`.
fn tool_result_artifact_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let now = integer(payload, "now_ms", None, Some(1), None)?;
    let limit = integer(payload, "limit", Some(1000), Some(1), Some(5000))?;
    prune(session, now, limit)
}

/// `row[column] or ''`.
fn or_empty(value: &SqlValue) -> OpResult<Value> {
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(String::new()))
    }
}

/// `int(row[column] or default)`.
fn int_or(row: &Row, column: &str, default: i64) -> OpResult<i64> {
    if row.require(column)?.truthy() {
        row.int(column)
    } else {
        Ok(default)
    }
}

/// `_artifact_document`.
fn artifact_document(row: &Row, content: bool) -> OpResult<Value> {
    let source_ref = codec::load(row.require("source_ref")?)?;
    let meta = codec::load(row.require("meta")?)?;
    if !source_ref.is_object() || !meta.is_object() {
        return fail("database_integrity", "Artifact JSON is invalid");
    }
    let mut result = Map::new();
    result.insert("id".into(), row.require("id")?.to_json()?);
    result.insert("conv_id".into(), row.require("conv_id")?.to_json()?);
    result.insert("task_id".into(), or_empty(row.require("task_id")?)?);
    result.insert("msg_id".into(), or_empty(row.require("msg_id")?)?);
    result.insert("source".into(), row.require("source")?.to_json()?);
    result.insert("source_ref".into(), source_ref);
    result.insert("format".into(), row.require("format")?.to_json()?);
    result.insert("title".into(), or_empty(row.require("title")?)?);
    result.insert("content_sha256".into(), row.require("content_sha256")?.to_json()?);
    result.insert("size_bytes".into(), Value::from(int_or(row, "size_bytes", 0)?));
    result.insert("version".into(), Value::from(int_or(row, "version", 1)?));
    result.insert("parent_id".into(), or_empty(row.require("parent_id")?)?);
    result.insert("pinned".into(), Value::Bool(row.require("pinned")?.truthy()));
    result.insert("meta".into(), meta);
    result.insert("created_at".into(), Value::from(int_or(row, "created_at", 0)?));
    if content {
        result.insert("content".into(), or_empty(row.require("content")?)?);
    }
    Ok(Value::Object(result))
}

/// `_artifact_create`.
fn artifact_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let artifact_id = required_text(payload, "artifact_id", 256)?;
    let conv_id = required_text(payload, "conv_id", 512)?;
    let source = required_text(payload, "source", 256)?;
    let artifact_format = required_text(payload, "format", 32)?;
    if !matches!(artifact_format.as_str(), "markdown" | "html" | "svg") {
        return fail("database_protocol_error", "Invalid artifact format");
    }
    let content = match payload.get("content") {
        None => String::new(),
        Some(Value::String(content)) => content.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid artifact content"),
    };
    let size = content.len() as i64;
    let source_ref = payload.get("source_ref").cloned().unwrap_or_else(|| json!({}));
    let meta = payload.get("meta").cloned().unwrap_or_else(|| json!({}));
    if !source_ref.is_object() || !meta.is_object() {
        return fail("database_protocol_error", "Invalid artifact metadata");
    }
    let sha = hex::encode(Sha256::digest(content.as_bytes()));
    session.lock_key("artifact.conv", &conv_id);
    let existing = session.fetch_one(
        &format!(
            "SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts WHERE conv_id = ? AND content_sha256 = ? AND deleted_at = 0 \
             ORDER BY created_at DESC LIMIT 1"
        ),
        &params![&conv_id, &sha],
    )?;
    if let Some(existing) = existing {
        return Ok(json!({"created": false, "artifact": artifact_document(&existing, false)?}));
    }
    let mut parent_id = optional_text(payload, "parent_id", 256, "artifact")?;
    let mut version = 1i64;
    if let Some(Value::String(path)) = source_ref.get("path") {
        if parent_id.is_empty() && !path.is_empty() {
            let candidate = session.fetch_one(
                "SELECT id, version FROM chat_artifacts \
                 WHERE conv_id = ? AND deleted_at = 0 AND json_extract(source_ref, '$.path') = ? \
                 ORDER BY version DESC, created_at DESC LIMIT 1",
                &params![&conv_id, path],
            )?;
            if let Some(candidate) = candidate {
                parent_id = candidate.text("id");
                version = int_or(&candidate, "version", 1)? + 1;
            }
        }
    }
    let task_id = optional_text(payload, "task_id", 512, "artifact")?;
    let msg_id = optional_text(payload, "msg_id", 512, "artifact")?;
    let source_ref_text = json_text(&source_ref)?;
    let title = py_strip(&optional_text(payload, "title", 300, "artifact")?).to_owned();
    let meta_text = json_text(&meta)?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.execute(
        "INSERT INTO chat_artifacts(\
id, conv_id, task_id, msg_id, source, source_ref, format, title, \
content, content_sha256, size_bytes, version, parent_id, pinned, \
meta, created_at, deleted_at) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            &artifact_id,
            &conv_id,
            task_id,
            msg_id,
            source,
            source_ref_text,
            artifact_format,
            title,
            content,
            sha,
            size,
            version,
            parent_id,
            false,
            meta_text,
            created_at,
            0i64,
        ],
    )?;
    let row = session
        .fetch_one(&format!("SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts WHERE id = ?"), &params![&artifact_id])?;
    let Some(row) = row else { return fail("database_integrity", "Artifact insert was not visible") };
    Ok(json!({"created": true, "artifact": artifact_document(&row, false)?}))
}

fn select_live_by_id(session: &Session<'_>, id: &SqlValue) -> OpResult<Option<Row>> {
    session.fetch_one(
        &format!("SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts WHERE id = ? AND deleted_at = 0"),
        std::slice::from_ref(id),
    )
}

/// `_artifact_get`.
fn artifact_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let artifact_id = required_text(payload, "artifact_id", 256)?;
    let include_content = match payload.get("include_content") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid artifact content selector"),
    };
    match select_live_by_id(session, &SqlValue::Text(artifact_id))? {
        Some(row) => artifact_document(&row, include_content),
        None => Ok(Value::Null),
    }
}

fn documents(rows: &[Row]) -> OpResult<Value> {
    Ok(Value::Array(rows.iter().map(|row| artifact_document(row, false)).collect::<OpResult<Vec<_>>>()?))
}

/// `_artifact_list`.
fn artifact_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = required_text(payload, "conv_id", 512)?;
    let include_deleted = match payload.get("include_deleted") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid artifact deleted selector"),
    };
    let where_clause = if include_deleted { "WHERE conv_id = ?" } else { "WHERE conv_id = ? AND deleted_at = 0" };
    let rows = session.fetch_all(
        &format!("SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts {where_clause} ORDER BY created_at DESC"),
        &params![conv_id],
    )?;
    documents(&rows)
}

/// `_artifact_delete`.
fn artifact_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let artifact_id = required_text(payload, "artifact_id", 256)?;
    let deleted_at = integer(payload, "deleted_at", None, Some(1), None)?;
    let count = session.execute(
        "UPDATE chat_artifacts SET deleted_at = ? WHERE id = ? AND deleted_at = 0",
        &params![deleted_at, artifact_id],
    )?;
    Ok(json!({"deleted": count != 0}))
}

/// `_artifact_versions`.
fn artifact_versions(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let artifact_id = required_text(payload, "artifact_id", 256)?;
    let Some(mut row) = select_live_by_id(session, &SqlValue::Text(artifact_id))? else {
        return Ok(Value::Array(Vec::new()));
    };
    let mut seen_up = std::collections::HashSet::new();
    seen_up.insert(row.text("id"));
    loop {
        let parent = row.get("parent_id").clone();
        if !parent.truthy() || seen_up.contains(&parent.text_or_empty()) {
            break;
        }
        seen_up.insert(parent.text_or_empty());
        let Some(found) = select_live_by_id(session, &parent)? else { break };
        row = found;
    }
    let mut chain = vec![artifact_document(&row, false)?];
    let mut current_id = row.text("id");
    let mut seen_forward = std::collections::HashSet::new();
    seen_forward.insert(current_id.clone());
    loop {
        let child = session.fetch_one(
            &format!(
                "SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts WHERE parent_id = ? AND deleted_at = 0 \
                 ORDER BY version ASC, created_at ASC LIMIT 1"
            ),
            &params![&current_id],
        )?;
        let Some(child) = child else { break };
        let child_id = child.text("id");
        if seen_forward.contains(&child_id) {
            break;
        }
        seen_forward.insert(child_id.clone());
        chain.push(artifact_document(&child, false)?);
        current_id = child_id;
    }
    Ok(Value::Array(chain))
}

/// `_artifact_library`.
fn artifact_library(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(50), Some(1), Some(200))?;
    let rows = session.fetch_all(
        &format!("SELECT {ARTIFACT_COLUMNS} FROM chat_artifacts WHERE deleted_at = 0 ORDER BY pinned DESC, created_at DESC LIMIT ?"),
        &params![limit],
    )?;
    documents(&rows)
}

/// `_artifact_pin`.
fn artifact_pin(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let artifact_id = required_text(payload, "artifact_id", 256)?;
    let Some(Value::Bool(pinned)) = payload.get("pinned") else {
        return fail("database_protocol_error", "Invalid artifact pin flag");
    };
    let count = session.execute(
        "UPDATE chat_artifacts SET pinned = ? WHERE id = ? AND deleted_at = 0",
        &params![*pinned, artifact_id],
    )?;
    Ok(json!({"changed": count != 0}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tool_result_query::tool_result_digest;

    #[test]
    fn manifest_digest_ignores_result_id() {
        let a = r#"{"resultId":"x","b":1.0,"a":"é"}"#;
        assert_eq!(canonical_manifest_digest_input(a).unwrap(), r#"{"a":"é","b":1.0}"#);
        assert!(canonical_manifest_digest_input("[1]").is_none());
        assert_eq!(tool_result_digest(&json!("tool-result:".to_owned() + &"a".repeat(64))).unwrap(), "a".repeat(64));
    }
}
