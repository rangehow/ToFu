//! `operations_pkg/_turns_lifecycle.py`: compaction, deletion and cleanup.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::archives::debit_stored_bytes_for_delete;
use super::core::{delete_turn_search_rows, now_ms, text, turn_identity, turn_public, upsert_turn_search_row, Obj};
use super::head::{delete_checkpoint, projection_from_turn_row};
use super::normalize::normalize_projection_document;
use super::read::prune_turn_tombstones;
use crate::codec::dump_param;
use crate::error::{fail, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

/// Turn rows keyed by id (`dict` with deterministic iteration is not
/// required: every consumer sorts or treats it as a set).
pub type RowSet = BTreeMap<String, Row>;

/// `_turn_row_is_live`.
pub fn turn_row_is_live(session: &Session<'_>, row: &Row) -> OpResult<bool> {
    if matches!(row.text("status").as_str(), "pending" | "running") {
        return Ok(true);
    }
    let attempt_id = row.get("current_attempt_id");
    if !attempt_id.truthy() {
        return Ok(false);
    }
    let attempt =
        session.fetch_one("SELECT status FROM storage_generation_attempts WHERE attempt_id=?", &params![attempt_id])?;
    Ok(attempt.is_some_and(|attempt| matches!(attempt.text("status").as_str(), "pending" | "running")))
}

/// `list(dict.fromkeys(str(item) for item in values if item))`.
pub fn unique_ids(value: &Value) -> Vec<String> {
    let mut seen = BTreeSet::new();
    let mut ids = Vec::new();
    if let Value::Array(items) = value {
        for item in items {
            if !truthy(item) {
                continue;
            }
            let id = python_str(item);
            if seen.insert(id.clone()) {
                ids.push(id);
            }
        }
    }
    ids
}

/// `_turn_deletion_closure`.
pub fn turn_deletion_closure(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    wanted: &[String],
) -> OpResult<RowSet> {
    let mut rows = RowSet::new();
    let mut pending_turns: Vec<String> = {
        let mut seen = BTreeSet::new();
        wanted.iter().filter(|id| !id.is_empty() && seen.insert((*id).clone())).cloned().collect()
    };
    let mut pending_lanes: Vec<String> = Vec::new();
    let mut seen_lanes = BTreeSet::new();
    while !pending_turns.is_empty() || !pending_lanes.is_empty() {
        while let Some(turn_id) = pending_turns.pop() {
            if rows.contains_key(&turn_id) {
                continue;
            }
            let Some(row) = session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
                &params![conv_id, user_id, turn_id.clone()],
            )?
            else {
                return fail("database_not_found", "Turn not found");
            };
            let projection = projection_from_turn_row(session, &row)?;
            rows.insert(turn_id, row);
            if let Some(lanes) = projection.get("_branchLanes") {
                if truthy(lanes) {
                    let lanes: &[Value] = match lanes {
                        Value::Array(lanes) => lanes,
                        Value::Object(_) | Value::String(_) => &[],
                        _ => return crate::error::internal("_branchLanes is not iterable"),
                    };
                    for descriptor in lanes {
                        if let Value::Object(descriptor) = descriptor {
                            if truthy(get(descriptor, "laneId")) {
                                pending_lanes.push(python_str(get(descriptor, "laneId")));
                            }
                        }
                    }
                }
            }
        }
        if let Some(lane_id) = pending_lanes.pop() {
            if !seen_lanes.insert(lane_id.clone()) {
                continue;
            }
            let children = session.fetch_all(
                "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=?",
                &params![conv_id, user_id, lane_id],
            )?;
            pending_turns
                .extend(children.iter().map(|child| child.text("turn_id")).filter(|id| !rows.contains_key(id)));
        }
    }
    for row in rows.values() {
        if turn_row_is_live(session, row)? {
            return fail("database_conflict", "A running turn or child branch cannot be compacted/deleted.");
        }
    }
    Ok(rows)
}

fn expire_change_prefix_for_deleted_turns(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    turn_ids: &[String],
) -> OpResult<()> {
    let marks = vec!["?"; turn_ids.len()].join(",");
    let mut args = vec![SqlValue::Text(conv_id.into()), SqlValue::Integer(user_id)];
    args.extend(turn_ids.iter().map(|id| SqlValue::Text(id.clone())));
    let row = session.fetch_one(
        &format!("SELECT MAX(c.sync_sequence) AS replay_floor FROM storage_conversation_changes AS c JOIN storage_generation_attempts AS a ON a.attempt_id=c.attempt_id WHERE c.conversation_id=? AND c.user_id=? AND c.attempt_sequence IS NOT NULL AND a.turn_id IN ({marks})"),
        &args,
    )?;
    let Some(floor) = row.and_then(|row| row.opt_int("replay_floor")) else { return Ok(()) };
    session.execute(
        "DELETE FROM storage_conversation_changes WHERE conversation_id=? AND user_id=? AND sync_sequence<=?",
        &params![conv_id, user_id, floor],
    )?;
    Ok(())
}

/// `_model_continuation._prefix`.
fn continuation_prefix(user_id: i64, conversation_id: &str) -> String {
    format!("{user_id}:{}:", hex::encode(Sha256::digest(conversation_id.as_bytes())))
}

/// Private port of `_model_continuation.delete_conversation_continuations`;
/// shared owner: model continuation operations.
fn delete_conversation_continuations(session: &Session<'_>, conversation_id: &str, user_id: i64) -> OpResult<()> {
    session.execute(
        "DELETE FROM storage_records WHERE namespace=? AND record_key LIKE ?",
        &params!["model.continuation.v1", continuation_prefix(user_id, conversation_id) + "%"],
    )?;
    Ok(())
}

/// `_delete_turn_row_set`.
pub fn delete_turn_row_set(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    rows: &RowSet,
    now: i64,
) -> OpResult<Vec<String>> {
    if !rows.is_empty() {
        delete_conversation_continuations(session, conv_id, user_id)?;
    }
    let delete_ids: Vec<String> = rows.keys().cloned().collect();
    delete_turn_search_rows(session, rows.values())?;
    for chunk in delete_ids.chunks(256) {
        let marks = vec!["?"; chunk.len()].join(",");
        let ids: Vec<SqlValue> = chunk.iter().map(|id| SqlValue::Text(id.clone())).collect();
        let mut owned = vec![SqlValue::Integer(user_id)];
        owned.extend(ids.iter().cloned());
        expire_change_prefix_for_deleted_turns(session, conv_id, user_id, chunk)?;
        debit_stored_bytes_for_delete(session, &format!("user_id=? AND turn_id IN ({marks})"), &owned)?;
        session
            .execute(&format!("DELETE FROM storage_raw_archives WHERE user_id=? AND turn_id IN ({marks})"), &owned)?;
        session.execute(
            &format!("DELETE FROM storage_attempt_events WHERE attempt_id IN (SELECT attempt_id FROM storage_generation_attempts WHERE turn_id IN ({marks}))"),
            &ids,
        )?;
        session.execute(&format!("DELETE FROM storage_generation_attempts WHERE turn_id IN ({marks})"), &ids)?;
        session
            .execute(&format!("DELETE FROM storage_turn_projection_checkpoints WHERE turn_id IN ({marks})"), &ids)?;
        session.execute(&format!("DELETE FROM storage_conversation_turns WHERE turn_id IN ({marks})"), &ids)?;
    }
    for turn_id in &delete_ids {
        session.execute(
            "INSERT INTO storage_turn_tombstones (conversation_id,user_id,turn_id,deleted_at) VALUES (?,?,?,?) ON CONFLICT(conversation_id,turn_id) DO NOTHING",
            &params![conv_id, user_id, turn_id.clone(), now],
        )?;
    }
    Ok(delete_ids)
}

/// `_turn_compact`.
pub fn turn_compact(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let expected_revision = integer(payload, "expected_conversation_revision", None, Some(0), None)?;
    let summary_turn_id = required_text(payload, "summary_turn_id", 128)?;
    let summary_projection = normalize_projection_document(get(payload, "summary_projection"))?;
    let marker_ok = matches!(summary_projection.get("compaction"), Some(Value::Object(marker)) if marker.get("blockId") == Some(&json!("compaction")));
    if !marker_ok {
        return fail("database_protocol_error", "Turn compaction summary marker is required.");
    }
    let wanted = unique_ids(get(payload, "delete_turn_ids"));
    let raw_updates = get(payload, "projection_updates");
    let raw_updates: Vec<Value> = match raw_updates {
        _ if !truthy(raw_updates) => vec![],
        Value::Array(items) => items.clone(),
        _ => return fail("database_protocol_error", "projection_updates must be a list"),
    };
    let insert_after = text(get(payload, "insert_after_turn_id"));
    let insert_before = text(get(payload, "insert_before_turn_id"));
    if insert_after.is_empty() && insert_before.is_empty() {
        return fail("database_protocol_error", "Summary insertion anchor is required");
    }
    let Some(conversation) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return fail("database_not_found", "Conversation not found");
    };
    let actual_revision = conversation.opt_int("rev").unwrap_or(0);
    if actual_revision != expected_revision {
        return Ok(json!({"applied": false, "conversationRevision": actual_revision}));
    }
    if session
        .fetch_one("SELECT turn_id FROM storage_conversation_turns WHERE turn_id=?", &params![summary_turn_id.clone()])?
        .is_some()
    {
        return fail("database_conflict", "Summary turn already exists");
    }
    let main_rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id='main' ORDER BY ordinal",
        &params![conv_id.clone(), user_id],
    )?;
    if main_rows.is_empty() {
        return fail("database_not_found", "Turn transcript not found");
    }
    let main_ids: BTreeSet<String> = main_rows.iter().map(|row| row.text("turn_id")).collect();
    if wanted.iter().any(|id| !main_ids.contains(id)) {
        return fail("database_not_found", "Compaction turn not found");
    }
    let delete_rows = turn_deletion_closure(session, &conv_id, user_id, &wanted)?;
    let retained: Vec<&Row> = main_rows.iter().filter(|row| !delete_rows.contains_key(&row.text("turn_id"))).collect();
    if retained.is_empty() {
        return fail("database_protocol_error", "Compaction must preserve a live tail");
    }
    for row in &retained {
        if turn_row_is_live(session, row)? {
            return fail("database_conflict", "A running turn cannot be compacted.");
        }
    }
    let retained_ids: Vec<String> = retained.iter().map(|row| row.text("turn_id")).collect();
    let find = |id: &str| retained.iter().find(|row| row.text("turn_id") == id).copied();
    if !insert_after.is_empty() && find(&insert_after).is_none() {
        return fail("database_protocol_error", "Invalid summary predecessor");
    }
    if !insert_before.is_empty() && find(&insert_before).is_none() {
        return fail("database_protocol_error", "Invalid summary successor");
    }
    let after_index = if insert_after.is_empty() {
        -1
    } else {
        retained_ids.iter().position(|id| *id == insert_after).unwrap_or(0) as i64
    };
    let before_index = if insert_before.is_empty() {
        retained_ids.len() as i64
    } else {
        retained_ids.iter().position(|id| *id == insert_before).unwrap_or(0) as i64
    };
    if before_index != after_index + 1 {
        return fail("database_protocol_error", "Summary anchors must be adjacent after folded turns are removed");
    }
    let summary_ordinal = if !insert_after.is_empty() {
        find(&insert_after).expect("anchor").int("ordinal")? + 1
    } else {
        find(&insert_before).expect("anchor").int("ordinal")? - 1
    };
    if !insert_before.is_empty() && summary_ordinal >= find(&insert_before).expect("anchor").int("ordinal")? {
        return fail("database_conflict", "No ordinal gap for compaction summary");
    }
    let mut updates: Vec<(&Row, Obj, i64)> = Vec::new();
    let mut seen = BTreeSet::new();
    for update in &raw_updates {
        let Value::Object(update) = update else {
            return fail("database_protocol_error", "Invalid projection update");
        };
        let turn_id = text(get(update, "turn_id"));
        if turn_id.is_empty() || seen.contains(&turn_id) || find(&turn_id).is_none() {
            return fail("database_protocol_error", "Invalid retained turn update");
        }
        seen.insert(turn_id.clone());
        let row = find(&turn_id).expect("retained");
        let expected = match get(update, "expected_projection_revision") {
            Value::Number(number) => number.as_i64().filter(|value| *value >= 0),
            _ => None,
        };
        let Some(expected) = expected.filter(|expected| row.opt_int("projection_revision") == Some(*expected)) else {
            return fail("database_conflict", "A retained turn changed before compaction");
        };
        updates.push((row, normalize_projection_document(get(update, "projection"))?, expected));
    }
    let now = now_ms();
    let advanced = session.execute(
        "UPDATE storage_conversations SET rev=rev+1,updated_at_ms=? WHERE id=? AND user_id=? AND rev=?",
        &params![now, conv_id.clone(), user_id, expected_revision],
    )?;
    if advanced != 1 {
        return Ok(json!({"applied": false, "conversationRevision": actual_revision}));
    }
    let deleted = delete_turn_row_set(session, &conv_id, user_id, &delete_rows, now)?;
    for (row, projection, revision) in &updates {
        let changed = session.execute(
            "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND projection_revision=?",
            &params![dump_param(&Value::Object(projection.clone()))?, revision + 1, now, row.get("turn_id"), *revision],
        )?;
        if changed != 1 {
            return fail("database_conflict", "A retained turn changed during compaction");
        }
        delete_checkpoint(session, &row.text("turn_id"))?;
        upsert_turn_search_row(session, row, None)?;
    }
    let settlement = json!({"outcome": "completed", "cause": "manual_compaction", "resumeOptions": []});
    session.execute(
        "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        &params![
            summary_turn_id.clone(),
            conv_id.clone(),
            user_id,
            "main",
            if insert_after.is_empty() { None } else { Some(insert_after.clone()) },
            summary_ordinal,
            "assistant",
            "compaction",
            "",
            "completed",
            SqlValue::Null,
            dump_param(&Value::Object(summary_projection))?,
            1i64,
            dump_param(&settlement)?,
            now,
            now
        ],
    )?;
    for row in &retained {
        let turn_id = row.text("turn_id");
        let parent = row.text("parent_turn_id");
        if turn_id == insert_before || delete_rows.contains_key(&parent) {
            session.execute(
                "UPDATE storage_conversation_turns SET parent_turn_id=?,updated_at=? WHERE turn_id=?",
                &params![summary_turn_id.clone(), now, turn_id],
            )?;
        }
    }
    prune_turn_tombstones(session, now)?;
    let summary_row = session
        .fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", &params![summary_turn_id])?
        .expect("inserted summary");
    upsert_turn_search_row(session, &summary_row, None)?;
    Ok(json!({
        "applied": true,
        "turn": turn_public(session, &summary_row)?,
        "deletedTurnIds": deleted,
        "conversationRevision": expected_revision + 1,
    }))
}

/// `_turn_delete`.
pub fn turn_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let wanted = unique_ids(get(payload, "turn_ids"));
    if wanted.is_empty() {
        return fail("database_protocol_error", "turnIds required");
    }
    let rows = turn_deletion_closure(session, &conv_id, user_id, &wanted)?;
    let now = now_ms();
    let deleted = delete_turn_row_set(session, &conv_id, user_id, &rows, now)?;
    prune_turn_tombstones(session, now)?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, conv_id.clone(), user_id],
    )?;
    let conversation = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?;
    let Some(conversation) = conversation else { return crate::error::internal("TypeError: NoneType") };
    Ok(json!({"deletedTurnIds": deleted, "conversationRevision": conversation.int("rev")?}))
}

/// `_turn_cleanup`.
pub fn turn_cleanup(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let retention = integer(payload, "retention_ms", Some(6 * 60 * 60 * 1000), Some(0), None)?.max(0);
    let limit = integer(payload, "limit", Some(500), Some(1), Some(5000))?;
    let cutoff = now_ms() - retention;
    let rows = session.fetch_all(
        "SELECT a.attempt_id FROM storage_generation_attempts a WHERE a.status='superseded' AND a.superseded_at IS NOT NULL AND a.superseded_at<? AND NOT EXISTS (SELECT 1 FROM storage_conversation_turns t WHERE t.current_attempt_id=a.attempt_id) AND NOT EXISTS (SELECT 1 FROM storage_conversation_changes c WHERE c.attempt_id=a.attempt_id AND c.attempt_sequence IS NOT NULL) ORDER BY a.superseded_at LIMIT ?",
        &params![cutoff, limit],
    )?;
    for row in &rows {
        let attempt_id = row.get("attempt_id").clone();
        debit_stored_bytes_for_delete(session, "attempt_id=?", std::slice::from_ref(&attempt_id))?;
        session.execute("DELETE FROM storage_raw_archives WHERE attempt_id=?", std::slice::from_ref(&attempt_id))?;
        session.execute("DELETE FROM storage_attempt_events WHERE attempt_id=?", std::slice::from_ref(&attempt_id))?;
        session.execute("DELETE FROM storage_generation_attempts WHERE attempt_id=?", &[attempt_id])?;
    }
    Ok(Value::from(rows.len()))
}
