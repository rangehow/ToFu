//! `operations_pkg/_turns_branch.py`: branch lane create/delete.

use serde_json::{json, Value};

use super::core::{now_ms, projection_change, turn_identity, turn_public, upsert_turn_search_row};
use super::head::{delete_checkpoint, projection_map};
use super::lifecycle::{delete_turn_row_set, turn_deletion_closure, turn_row_is_live};
use super::read::prune_turn_tombstones;
use crate::codec::dump_param;
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::Session;

fn truncated(value: &Value, default: &str, limit: usize) -> Value {
    let rendered = if truthy(value) { crate::json::python_str(value) } else { default.to_owned() };
    Value::String(rendered.chars().take(limit).collect())
}

fn lanes_of(projection: &serde_json::Map<String, Value>) -> OpResult<Vec<Value>> {
    let value = get(projection, "_branchLanes");
    match value {
        _ if !truthy(value) => Ok(vec![]),
        Value::Array(items) => Ok(items.clone()),
        Value::Object(map) => Ok(map.keys().map(|key| Value::String(key.clone())).collect()),
        Value::String(text) => Ok(text.chars().map(|c| Value::String(c.to_string())).collect()),
        _ => internal("_branchLanes is not iterable"),
    }
}

/// `_turn_branch_create`.
pub fn turn_branch_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let parent_id = required_text(payload, "parent_turn_id", 128)?;
    let expected = integer(payload, "expected_projection_revision", None, Some(0), None)?;
    let Some(parent) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id.clone(), user_id, parent_id.clone()],
    )?
    else {
        return fail("database_not_found", "Parent turn not found");
    };
    let public = turn_public(session, &parent)?;
    if matches!(public["status"].as_str(), Some("pending" | "running")) {
        return fail("database_conflict", "A running parent turn cannot be branched.");
    }
    if Some(expected) != public["projectionRevision"].as_i64() {
        return fail("database_conflict", "The parent turn changed before branch creation.");
    }
    let now = now_ms();
    let lane = json!({
        "laneId": format!("lane_{}", uuid::Uuid::new_v4()),
        "parentTurnId": parent_id,
        "title": truncated(get(payload, "title"), "Branch", 200),
        "icon": "⑂",
        "kind": truncated(get(payload, "kind"), "branch", 80),
        "anchorText": truncated(get(payload, "anchor_text"), "", 1000),
        "parentSelection": truncated(get(payload, "parent_selection"), "", 10000),
        "createdAt": now,
    });
    let previous = public["projection"].as_object().cloned().unwrap_or_default();
    let mut projection = previous.clone();
    let mut lanes = lanes_of(&projection)?;
    lanes.push(lane.clone());
    projection.insert("_branchLanes".into(), Value::Array(lanes));
    let revision = expected + 1;
    let changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND projection_revision=?",
        &params![dump_param(&Value::Object(projection.clone()))?, revision, now, parent_id.clone(), expected],
    )?;
    if changed != 1 {
        return fail("database_conflict", "The parent turn changed during branch creation");
    }
    delete_checkpoint(session, &parent_id)?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, conv_id.clone(), user_id],
    )?;
    let updated = session
        .fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", &params![parent_id.clone()])?
        .expect("updated parent");
    upsert_turn_search_row(session, &updated, None)?;
    let conversation = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?;
    let Some(conversation) = conversation else { return internal("TypeError: NoneType") };
    Ok(json!({
        "turn": turn_public(session, &updated)?,
        "lane": lane,
        "conversationRevision": conversation.int("rev")?,
        "_conversationSyncTurnPatch": projection_change(&parent_id, &Value::Object(previous), &Value::Object(projection), expected, revision, now),
    }))
}

/// `_turn_branch_delete`.
pub fn turn_branch_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let parent_id = required_text(payload, "parent_turn_id", 128)?;
    let lane_id = required_text(payload, "lane_id", 128)?;
    let Some(parent) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id.clone(), user_id, parent_id.clone()],
    )?
    else {
        return fail("database_not_found", "Parent turn not found");
    };
    if turn_row_is_live(session, &parent)? {
        return fail("database_conflict", "A running parent turn cannot delete a branch.");
    }
    let previous = projection_map(session, &parent)?;
    let mut projection = previous.clone();
    let descriptors = lanes_of(&projection)?;
    let mut kept = Vec::new();
    for item in &descriptors {
        let Value::Object(descriptor) = item else { return internal("descriptor has no get") };
        if get(descriptor, "laneId") != &Value::String(lane_id.clone()) {
            kept.push(item.clone());
        }
    }
    if kept.len() == descriptors.len() {
        return fail("database_not_found", "Branch lane not found");
    }
    let children = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns WHERE conversation_id=? AND lane_id=?",
        &params![conv_id.clone(), lane_id.clone()],
    )?;
    let now = now_ms();
    let ids: Vec<String> = children.iter().map(|row| row.text("turn_id")).collect();
    let rows = turn_deletion_closure(session, &conv_id, user_id, &ids)?;
    let deleted = delete_turn_row_set(session, &conv_id, user_id, &rows, now)?;
    projection.insert("_branchLanes".into(), Value::Array(kept));
    prune_turn_tombstones(session, now)?;
    let base = parent.int("projection_revision")?;
    let revision = base + 1;
    session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=?",
        &params![dump_param(&Value::Object(projection.clone()))?, revision, now, parent_id.clone()],
    )?;
    delete_checkpoint(session, &parent_id)?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, conv_id.clone(), user_id],
    )?;
    let updated = session
        .fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", &params![parent_id.clone()])?
        .expect("updated parent");
    upsert_turn_search_row(session, &updated, None)?;
    let conversation = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?;
    let Some(conversation) = conversation else { return internal("TypeError: NoneType") };
    Ok(json!({
        "turn": turn_public(session, &updated)?,
        "deletedLaneId": lane_id,
        "deletedTurnIds": deleted,
        "conversationRevision": conversation.int("rev")?,
        "_conversationSyncTurnPatch": projection_change(&parent_id, &Value::Object(previous), &Value::Object(projection), base, revision, now),
    }))
}
