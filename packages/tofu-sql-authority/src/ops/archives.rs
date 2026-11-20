//! `operation_domains/archives.py`: compaction transcript archives
//! (`operations_pkg/_archives.py`), durable model continuations
//! (`_model_continuation.py`) and child invocation birth evidence
//! (`_invocations.py`).

use serde_json::{json, Map, Value};

use super::conversations::legacy::{decode_archived_sequence, encode_archived_sequence};
use super::conversations::support::{
    load, mark_conversation_search_projection_dirty, now_ms, prefix_chars, py_int, py_str, str_or,
};
use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

mod continuation;
mod invocations;
mod task_result_codec;
mod worker_process;

const MAX_RECEIPT_BYTES: usize = 32 * 1024;
const MAX_MESSAGES_BYTES: usize = 8 * 1024 * 1024;

pub fn register(registry: &mut Registry) {
    add(registry, "invocation.register_child", OperationSpec::command(false, invocations::register_child));
    add(registry, "invocation.resolve_child", OperationSpec::query(invocations::resolve_child));
    add(registry, "model_continuation.get", OperationSpec::query(continuation::continuation_get));
    add(registry, "model_continuation.put", OperationSpec::command(false, continuation::continuation_put));
    add(registry, "model_continuation.delete", OperationSpec::command(false, continuation::continuation_delete));
    add(registry, "compaction_archive.create", OperationSpec::command(true, archive_create));
    add(registry, "compaction_archive.list", OperationSpec::query(archive_list));
    add(registry, "compaction_archive.get", OperationSpec::query(archive_get));
    add(registry, "compaction_archive.update_summary", OperationSpec::command(true, archive_update_summary));
    add(registry, "compaction_archive.delete_conversation", OperationSpec::command(true, archive_delete_conversation));
    add(registry, "compaction_archive.prune", OperationSpec::command(true, archive_prune));
}

fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

/// `_identity(payload)`.
fn identity(payload: &Payload) -> OpResult<(String, i64)> {
    Ok((required_text(payload, "conversation_id", 512)?, integer(payload, "user_id", None, Some(1), None)?))
}

/// `_require_owned_conversation`.
fn require_owned_conversation(session: &Session<'_>, conversation_id: &str, user_id: i64) -> OpResult<()> {
    let row = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations \
         WHERE id=? AND user_id=?",
        &params![conversation_id, user_id],
    )?;
    if row.is_none() {
        return fail("database_not_found", "Conversation not found");
    }
    Ok(())
}

/// `_text(payload, key, maximum=)`.
fn text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= maximum => Ok(value.clone()),
        _ => protocol(&format!("Invalid {key} in storage request")),
    }
}

/// `_archive_scope(session, payload, conversation_id, user_id)`.
fn archive_scope(session: &Session<'_>, payload: &Payload, conversation_id: &str, user_id: i64) -> OpResult<String> {
    let scope = text(payload, "archive_scope", 1024)?;
    require_owned_conversation(session, conversation_id, user_id)?;
    if !scope.is_empty() {
        invocations::require_child(session, &scope, conversation_id, user_id)?;
    }
    Ok(scope)
}

/// `_encoded_receipt(payload)`.
fn encoded_receipt(payload: &Payload) -> OpResult<Vec<u8>> {
    let value = match payload.get("receipt") {
        None => Value::Object(Map::new()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return protocol("Archive receipt must be an object"),
    };
    let encoded = codec::dump(&value)?;
    if encoded.len() > MAX_RECEIPT_BYTES {
        return protocol("Archive receipt exceeds 32 KiB");
    }
    Ok(encoded)
}

/// `_decoded_receipt(row)`.
fn decoded_receipt(row: &Row) -> OpResult<Map<String, Value>> {
    match load(row.get("receipt_json"))? {
        Value::Object(map) => Ok(map),
        _ => fail("database_integrity", "Archive receipt is malformed"),
    }
}

/// `_encoded_messages(messages)`.
fn encoded_messages(messages: &Value) -> OpResult<Vec<u8>> {
    match encode_archived_sequence(messages) {
        Ok(encoded) => Ok(encoded),
        Err(()) => protocol("Archive messages are invalid"),
    }
}

/// `_decoded_messages(value)`.
fn decoded_messages(value: &SqlValue) -> OpResult<Vec<Value>> {
    let stored = load(value)?;
    match decode_archived_sequence(&stored) {
        Ok(messages) => Ok(messages),
        Err(()) => fail("database_integrity", "Archive messages are malformed"),
    }
}

/// `_metadata(row)`.
fn metadata(row: &Row) -> OpResult<Map<String, Value>> {
    let summary = str_or(row.get("summary"), "");
    let task_model = str_or(row.get("model"), "");
    let receipt = decoded_receipt(row)?;
    let int_or_zero = |column: &str| super::conversations::support::int_or_zero(row.get(column));
    let value = json!({
        "schemaVersion": "tofu.compaction-archive/v3",
        "id": py_str(row.get("archive_id")),
        "convId": py_str(row.get("conversation_id")),
        "createdAt": py_int(row.get("created_at_ms"))?,
        "snapshotKind": "pre_compaction_transcript",
        "trigger": str_or(row.get("trigger"), "force"),
        "taskId": str_or(row.get("task_id"), ""),
        "roundNum": int_or_zero("round_num")?,
        "model": task_model,
        "taskModel": task_model,
        "tokensBefore": int_or_zero("tokens_before")?,
        "tokensAfter": int_or_zero("tokens_after")?,
        "tokenCountKind": "estimated",
        "msgsBefore": int_or_zero("msgs_before")?,
        "msgsAfter": int_or_zero("msgs_after")?,
        "reason": str_or(row.get("reason"), ""),
        "payloadSize": int_or_zero("payload_size")?,
        "payloadSizeUnit": "bytes",
        "summaryPreview": prefix_chars(&summary, 240),
        "hasSummary": !summary.is_empty(),
        "hasReceipt": !receipt.is_empty(),
        "resultStatus": receipt.get("status").filter(|value| truthy(value)).map(crate::json::python_str).unwrap_or_else(|| "legacy".into()),
        "resultStrategy": receipt.get("strategy").filter(|value| truthy(value)).map(crate::json::python_str).unwrap_or_default(),
    });
    let Value::Object(map) = value else { unreachable!("metadata is an object") };
    Ok(map)
}

fn archive_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conversation_id, user_id) = identity(payload)?;
    let scope = archive_scope(session, payload, &conversation_id, user_id)?;
    let archive_id = required_text(payload, "archive_id", 128)?;
    let messages = get(payload, "messages");
    if !messages.is_array() {
        return protocol("Archive messages must be a list");
    }
    require_owned_conversation(session, &conversation_id, user_id)?;
    let stored_messages = encoded_messages(messages)?;
    let stored_receipt = encoded_receipt(payload)?;
    let created_at_ms = integer(payload, "created_at_ms", Some(now_ms()), Some(0), None)?;
    let payload_size = stored_messages.len() as i64;
    let summary = text(payload, "summary", 200_000)?;
    let trigger = text(payload, "trigger", 32)?;
    let trigger = if trigger.is_empty() { "force".to_owned() } else { trigger };
    let task_id = text(payload, "task_id", 512)?;
    let round_num = integer(payload, "round_num", Some(0), Some(0), None)?;
    let model = text(payload, "model", 256)?;
    let tokens_before = integer(payload, "tokens_before", Some(0), Some(0), None)?;
    let tokens_after = integer(payload, "tokens_after", Some(0), Some(0), None)?;
    let msgs_before = integer(payload, "msgs_before", Some(0), Some(0), None)?;
    let msgs_after = integer(payload, "msgs_after", Some(0), Some(0), None)?;
    let reason = text(payload, "reason", 500)?;
    let inserted = session.execute(
        "INSERT INTO storage_compaction_archives(\
         archive_id,archive_scope,conversation_id,user_id,messages_json,summary,receipt_json,trigger,\
         task_id,round_num,model,tokens_before,tokens_after,msgs_before,\
         msgs_after,reason,payload_size,created_at_ms) \
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) \
         ON CONFLICT(archive_id) DO NOTHING",
        &params![
            archive_id.as_str(),
            scope.as_str(),
            conversation_id.as_str(),
            user_id,
            stored_messages,
            summary,
            stored_receipt,
            trigger,
            task_id,
            round_num,
            model,
            tokens_before,
            tokens_after,
            msgs_before,
            msgs_after,
            reason,
            payload_size,
            created_at_ms,
        ],
    )?;
    if inserted == 0 {
        let existing = session.fetch_one(
            "SELECT conversation_id,user_id,archive_scope,messages_json FROM \
             storage_compaction_archives WHERE archive_id=?",
            &params![archive_id.as_str()],
        )?;
        let conflicting = match &existing {
            None => true,
            Some(existing) => {
                py_str(existing.get("conversation_id")) != conversation_id
                    || py_int(existing.get("user_id"))? != user_id
                    || *existing.get("archive_scope") != SqlValue::Text(scope.clone())
                    || codec::dump(&Value::Array(decoded_messages(existing.get("messages_json"))?))?
                        != codec::dump(messages)?
            }
        };
        if conflicting {
            return fail("database_conflict", "Archive id has a conflicting payload");
        }
    }
    if inserted != 0 {
        mark_conversation_search_projection_dirty(session, &conversation_id, user_id)?;
    }
    Ok(json!({"created": inserted != 0, "archiveId": archive_id}))
}

fn archive_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conversation_id, user_id) = identity(payload)?;
    let scope = archive_scope(session, payload, &conversation_id, user_id)?;
    require_owned_conversation(session, &conversation_id, user_id)?;
    let limit = integer(payload, "limit", Some(200), Some(1), Some(1000))?;
    let rows = session.fetch_all(
        "SELECT archive_id,conversation_id,created_at_ms,trigger,task_id,\
         round_num,model,tokens_before,tokens_after,msgs_before,msgs_after,\
         reason,payload_size,SUBSTR(summary,1,240) AS summary,receipt_json \
         FROM storage_compaction_archives \
         WHERE conversation_id=? AND user_id=? AND archive_scope=? \
         ORDER BY created_at_ms,archive_id LIMIT ?",
        &params![conversation_id.as_str(), user_id, scope.as_str(), limit],
    )?;
    let archives = rows.iter().map(|row| metadata(row).map(Value::Object)).collect::<OpResult<Vec<_>>>()?;
    Ok(json!({"archives": archives}))
}

fn archive_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conversation_id, user_id) = identity(payload)?;
    let scope = archive_scope(session, payload, &conversation_id, user_id)?;
    let archive_id = required_text(payload, "archive_id", 128)?;
    let include_messages = match payload.get("include_messages") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid include_messages in storage request"),
    };
    let include_post_messages = match payload.get("include_post_messages") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid include_post_messages in storage request"),
    };
    require_owned_conversation(session, &conversation_id, user_id)?;
    let selected_columns = if include_messages {
        "*".to_owned()
    } else {
        let mut columns = String::from(
            "archive_id,conversation_id,user_id,summary,trigger,task_id,\
             round_num,model,tokens_before,tokens_after,msgs_before,\
             msgs_after,reason,payload_size,created_at_ms,receipt_json",
        );
        if include_post_messages {
            columns.push_str(",post_messages_json");
        }
        columns
    };
    let Some(row) = session.fetch_one(
        &format!(
            "SELECT {selected_columns} FROM storage_compaction_archives WHERE archive_id=? \
             AND conversation_id=? AND user_id=? AND archive_scope=?"
        ),
        &params![archive_id.as_str(), conversation_id.as_str(), user_id, scope.as_str()],
    )?
    else {
        return Ok(Value::Null);
    };
    let mut archive = metadata(&row)?;
    archive.insert("summary".into(), Value::String(str_or(row.get("summary"), "")));
    archive.insert("receipt".into(), Value::Object(decoded_receipt(&row)?));
    archive.insert(
        "messagesCount".into(),
        Value::from(super::conversations::support::int_or_zero(row.get("msgs_before"))?),
    );
    let mut messages = None;
    if include_messages {
        let decoded = decoded_messages(row.get("messages_json"))?;
        archive.insert("messagesCount".into(), Value::from(decoded.len()));
        messages = Some(decoded);
    }
    let mut result = Map::new();
    result.insert("archive".into(), Value::Object(archive));
    if let Some(messages) = messages {
        result.insert("messages".into(), Value::Array(messages));
    }
    if include_post_messages {
        let stored_post = row.get("post_messages_json");
        let post = if stored_post.is_null() { Value::Null } else { Value::Array(decoded_messages(stored_post)?) };
        result.insert("postMessages".into(), post);
    }
    Ok(Value::Object(result))
}

fn archive_update_summary(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let archive_id = required_text(payload, "archive_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let scope = text(payload, "archive_scope", 1024)?;
    let Some(active) = session.fetch_one(
        "SELECT a.conversation_id FROM storage_compaction_archives AS a \
         JOIN storage_conversations AS c \
         ON c.id=a.conversation_id AND c.user_id=a.user_id \
         WHERE a.archive_id=? AND a.user_id=? AND a.archive_scope=?",
        &params![archive_id.as_str(), user_id, scope.as_str()],
    )?
    else {
        return Ok(json!({"updated": false}));
    };
    archive_scope(session, payload, &py_str(active.get("conversation_id")), user_id)?;
    let mut assignments = vec!["summary=?", "tokens_after=?", "msgs_after=?"];
    let mut values = vec![
        SqlValue::from(text(payload, "summary", 200_000)?),
        SqlValue::from(integer(payload, "tokens_after", None, Some(0), None)?),
        SqlValue::from(integer(payload, "msgs_after", None, Some(0), None)?),
    ];
    if payload.contains_key("receipt") {
        assignments.push("receipt_json=?");
        values.push(SqlValue::Blob(encoded_receipt(payload)?));
    }
    let post_messages = get(payload, "post_messages");
    if !post_messages.is_null() {
        if !post_messages.is_array() {
            return protocol("Archive post messages must be a list");
        }
        let encoded_post = encoded_messages(post_messages)?;
        if encoded_post.len() > MAX_MESSAGES_BYTES {
            return protocol("Archive post messages exceed 8 MiB");
        }
        assignments.push("post_messages_json=?");
        values.push(SqlValue::Blob(encoded_post));
    }
    values.extend([SqlValue::from(archive_id.as_str()), SqlValue::from(user_id), SqlValue::from(scope.as_str())]);
    let updated = session.execute(
        &format!(
            "UPDATE storage_compaction_archives SET {} WHERE archive_id=? AND user_id=? AND archive_scope=?",
            assignments.join(",")
        ),
        &values,
    )?;
    Ok(json!({"updated": updated != 0}))
}

fn archive_delete_conversation(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conversation_id, user_id) = identity(payload)?;
    let scope = archive_scope(session, payload, &conversation_id, user_id)?;
    let deleted = session.execute(
        "DELETE FROM storage_compaction_archives \
         WHERE conversation_id=? AND user_id=? AND archive_scope=?",
        &params![conversation_id.as_str(), user_id, scope.as_str()],
    )?;
    if deleted != 0 {
        mark_conversation_search_projection_dirty(session, &conversation_id, user_id)?;
    }
    Ok(json!({"deleted": deleted}))
}

fn archive_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conversation_id, user_id) = identity(payload)?;
    let scope = archive_scope(session, payload, &conversation_id, user_id)?;
    let keep = integer(payload, "keep", None, Some(1), Some(1000))?;
    let rows = session.fetch_all(
        "SELECT archive_id FROM storage_compaction_archives \
         WHERE conversation_id=? AND user_id=? AND archive_scope=? \
         ORDER BY created_at_ms DESC,archive_id DESC",
        &params![conversation_id.as_str(), user_id, scope.as_str()],
    )?;
    let stale_ids: Vec<String> = rows.iter().skip(keep as usize).map(|row| py_str(row.get("archive_id"))).collect();
    for archive_id in &stale_ids {
        session.execute(
            "DELETE FROM storage_compaction_archives WHERE archive_id=? \
             AND conversation_id=? AND user_id=? AND archive_scope=?",
            &params![archive_id.as_str(), conversation_id.as_str(), user_id, scope.as_str()],
        )?;
    }
    if !stale_ids.is_empty() {
        mark_conversation_search_projection_dirty(session, &conversation_id, user_id)?;
    }
    Ok(json!({"deleted": stale_ids.len()}))
}
