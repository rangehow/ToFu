//! Private ports of `turn_projection_head.py` and `turn_projection_write.py`:
//! bounded durable patch heads, checkpoint bases and revision writes.
//!
//! `projection_from_turn_row` consults the backend's reconstructible
//! revision-exact cache (`crate::turn_projection_cache`) exactly as Python.

use serde_json::{Map, Value};

use super::patch::apply_projection_patch;
use crate::codec::{dump, dump_param, load, py_eq};
use crate::error::{fail, OpResult};
use crate::params;
use crate::session::{Row, Session, SqlValue};
use crate::turn_projection_cache::{projection_cache_key, ProjectionCacheKey};
use std::sync::Arc;

pub const PROJECTION_HEAD_MAX_PATCHES: i64 = 64;
pub const PROJECTION_HEAD_MAX_PATCH_BYTES: i64 = 1024 * 1024;
const PROJECTION_HEAD_QUERY_ROW_LIMIT: i64 = PROJECTION_HEAD_MAX_PATCHES * 4 + 1;
pub const PROJECTION_INLINE_LIVE_MAX_BYTES: i64 = 64 * 1024;
pub use crate::turn_projection_cache::PROJECTION_CACHE_CHARGE_MULTIPLIER;

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct HeadState {
    pub materialized_revision: Option<i64>,
    pub patch_count: i64,
    pub patch_bytes: i64,
    pub checkpoint_revision: Option<i64>,
}

impl HeadState {
    pub fn active(&self) -> bool {
        self.materialized_revision.is_some()
    }

    pub fn checkpoint_active(&self) -> bool {
        self.checkpoint_revision.is_some()
    }
}

pub struct ProjectionBase {
    pub projection: Value,
    pub payload_bytes: i64,
}

fn nonnegative(value: &SqlValue, field: &str) -> OpResult<i64> {
    match value {
        SqlValue::Integer(value) if *value >= 0 => Ok(*value),
        _ => fail("database_integrity", format!("Turn {field} must be a nonnegative integer")),
    }
}

/// `_row_value(row, field)` without a default.
fn row_value<'r>(row: &'r Row, field: &str) -> OpResult<&'r SqlValue> {
    if !row.contains(field) {
        return fail("database_integrity", format!("Turn row is missing {field}"));
    }
    Ok(row.get(field))
}

/// `plan_projection_head_append`.
pub fn plan_head_append(
    state: &HeadState,
    current_revision: i64,
    patch_bytes: i64,
    exact_patch: bool,
    projection_changed: bool,
) -> OpResult<Option<HeadState>> {
    if current_revision < 0 {
        return fail("database_integrity", "Turn projection revision must be a nonnegative integer");
    }
    if patch_bytes < 0 {
        return fail("database_integrity", "Turn projection patch bytes must be a nonnegative integer");
    }
    if !exact_patch || patch_bytes == 0 {
        return Ok(None);
    }
    if !state.active() && !state.checkpoint_active() && !projection_changed {
        return Ok(None);
    }
    let (materialized, count, bytes) = if let Some(materialized) = state.materialized_revision {
        if current_revision - materialized != state.patch_count {
            return fail("database_integrity", "Turn projection patch head is not revision-contiguous");
        }
        (materialized, state.patch_count + 1, state.patch_bytes + patch_bytes)
    } else {
        (current_revision, 1, patch_bytes)
    };
    if count > PROJECTION_HEAD_MAX_PATCHES || bytes > PROJECTION_HEAD_MAX_PATCH_BYTES {
        return Ok(None);
    }
    Ok(Some(HeadState {
        materialized_revision: Some(materialized),
        patch_count: count,
        patch_bytes: bytes,
        checkpoint_revision: state.checkpoint_revision,
    }))
}

/// `projection_head_state(row)`.
pub fn head_state(row: &Row) -> OpResult<HeadState> {
    let checkpoint = match row.get("projection_checkpoint_revision") {
        SqlValue::Null => None,
        value => Some(nonnegative(value, "projection checkpoint revision")?),
    };
    let materialized = match row.get("projection_materialized_revision") {
        SqlValue::Null => None,
        value => Some(nonnegative(value, "projection materialized revision")?),
    };
    let count = if row.contains("projection_patch_count") {
        nonnegative(row.get("projection_patch_count"), "projection patch count")?
    } else {
        0
    };
    let bytes = if row.contains("projection_patch_bytes") {
        nonnegative(row.get("projection_patch_bytes"), "projection patch bytes")?
    } else {
        0
    };
    if materialized.is_none() {
        if count != 0 || bytes != 0 {
            return fail("database_integrity", "Materialized Turn has residual projection patch metadata");
        }
    } else if !(1..=PROJECTION_HEAD_MAX_PATCHES).contains(&count)
        || !(1..=PROJECTION_HEAD_MAX_PATCH_BYTES).contains(&bytes)
    {
        return fail("database_integrity", "Turn projection patch head exceeds its budget");
    }
    if let Some(checkpoint) = checkpoint {
        let current = nonnegative(row_value(row, "projection_revision")?, "projection revision")?;
        if checkpoint != materialized.unwrap_or(current) {
            return fail("database_integrity", "Turn projection checkpoint revision is inconsistent");
        }
    }
    Ok(HeadState {
        materialized_revision: materialized,
        patch_count: count,
        patch_bytes: bytes,
        checkpoint_revision: checkpoint,
    })
}

fn stored_payload_bytes(raw: &SqlValue, decoded: &Value) -> OpResult<i64> {
    Ok(match raw {
        SqlValue::Blob(bytes) => bytes.len() as i64,
        SqlValue::Text(text) => text.len() as i64,
        _ => dump(decoded)?.len() as i64,
    })
}

fn attempt_text(row: &Row) -> String {
    row.text("current_attempt_id")
}

/// `load_turn_projection_base`.
pub fn load_base(
    session: &Session<'_>,
    row: &Row,
    head: &HeadState,
    require_mapping: bool,
) -> OpResult<ProjectionBase> {
    if let Some(checkpoint_revision) = head.checkpoint_revision {
        let checkpoint = session.fetch_one(
            "SELECT projection_json,projection_bytes FROM storage_turn_projection_checkpoints WHERE turn_id=? AND conversation_id=? AND user_id=? AND attempt_id=? AND projection_revision=?",
            &params![
                row_value(row, "turn_id")?.text_or_empty(),
                row_value(row, "conversation_id")?.text_or_empty(),
                nonnegative(row_value(row, "user_id")?, "owner")?,
                attempt_text(row),
                checkpoint_revision,
            ],
        )?;
        let Some(checkpoint) = checkpoint else {
            return fail("database_integrity", "Turn projection checkpoint is missing");
        };
        let raw = checkpoint.get("projection_json");
        let declared = nonnegative(checkpoint.get("projection_bytes"), "projection checkpoint bytes")?;
        if declared < 1 {
            return fail("database_integrity", "Turn projection checkpoint is empty");
        }
        let decoded = load(raw)?;
        if !decoded.is_object() {
            return fail("database_integrity", "Stored turn projection checkpoint must be an object");
        }
        let actual = match raw {
            SqlValue::Blob(bytes) => bytes.len() as i64,
            SqlValue::Text(text) => text.len() as i64,
            _ => declared,
        };
        if actual != declared {
            return fail("database_integrity", "Turn projection checkpoint byte evidence is inconsistent");
        }
        return Ok(ProjectionBase { projection: decoded, payload_bytes: declared });
    }
    let raw = if row.contains("projection_json") {
        row.get("projection_json").clone()
    } else {
        let attempt_id = attempt_text(row);
        let mut sql = String::from(
            "SELECT projection_json FROM storage_conversation_turns WHERE turn_id=? AND conversation_id=? AND user_id=? AND projection_revision=?",
        );
        let mut args = vec![
            SqlValue::Text(row_value(row, "turn_id")?.text_or_empty()),
            SqlValue::Text(row_value(row, "conversation_id")?.text_or_empty()),
            SqlValue::Integer(nonnegative(row_value(row, "user_id")?, "owner")?),
            SqlValue::Integer(nonnegative(row_value(row, "projection_revision")?, "projection revision")?),
        ];
        if !attempt_id.is_empty() {
            sql.push_str(" AND current_attempt_id=?");
            args.push(SqlValue::Text(attempt_id));
        }
        match session.fetch_one(&sql, &args)? {
            Some(inline) => inline.get("projection_json").clone(),
            None => return fail("database_conflict", "Turn projection changed during hydration"),
        }
    };
    let decoded = load(&raw)?;
    if require_mapping && !decoded.is_object() {
        return fail("database_integrity", "Stored turn projection must be an object");
    }
    let payload_bytes = stored_payload_bytes(&raw, &decoded)?;
    Ok(ProjectionBase { projection: decoded, payload_bytes })
}

fn patch_from_event(row: &Row) -> OpResult<Option<Value>> {
    let envelope = load(row.get("payload_json"))?;
    let Value::Object(envelope) = envelope else {
        return fail("database_integrity", "Turn projection event envelope is malformed");
    };
    let Some(Value::Object(payload)) = envelope.get("payload") else { return Ok(None) };
    match payload.get("projectionPatch") {
        Some(patch @ Value::Object(_)) => Ok(Some(patch.clone())),
        _ => Ok(None),
    }
}

/// `fold_turn_projection_head`.
pub fn fold_head(session: &Session<'_>, row: &Row, base: &Value, head: &HeadState) -> OpResult<Value> {
    let Some(materialized) = head.materialized_revision else { return Ok(base.clone()) };
    let current = nonnegative(row_value(row, "projection_revision")?, "projection revision")?;
    if materialized >= current || current - materialized != head.patch_count {
        return fail("database_integrity", "Turn projection patch revisions are inconsistent");
    }
    let attempt_id = attempt_text(row);
    if attempt_id.is_empty() {
        return fail("database_integrity", "Turn projection patch head has no attempt");
    }
    let rows = session.fetch_all(
        "SELECT projection_revision,payload_json FROM storage_attempt_events WHERE attempt_id=? AND projection_revision>? AND projection_revision<=? ORDER BY sequence LIMIT ?",
        &params![attempt_id, materialized, current, PROJECTION_HEAD_QUERY_ROW_LIMIT],
    )?;
    if rows.len() as i64 >= PROJECTION_HEAD_QUERY_ROW_LIMIT {
        return fail("database_integrity", "Turn projection patch event range is ambiguous");
    }
    let mut patches: std::collections::HashMap<i64, Value> = std::collections::HashMap::new();
    for event in &rows {
        let revision = nonnegative(event.get("projection_revision"), "event projection revision")?;
        let Some(patch) = patch_from_event(event)? else { continue };
        if patches.contains_key(&revision) {
            return fail("database_integrity", "Turn projection revision has duplicate patches");
        }
        patches.insert(revision, patch);
    }
    let mut projection = base.clone();
    for revision in materialized + 1..=current {
        let Some(patch) = patches.get(&revision) else {
            return fail("database_integrity", "Turn projection patch chain has a gap");
        };
        let base_ok = py_eq(patch.get("baseRevision").unwrap_or(&Value::Null), &Value::from(revision - 1));
        let target_ok = py_eq(patch.get("targetRevision").unwrap_or(&Value::Null), &Value::from(revision));
        if !base_ok || !target_ok {
            return fail("database_integrity", "Turn projection patch chain is misbased");
        }
        projection = match apply_projection_patch(&projection, patch) {
            Ok(next) => next,
            Err(_) => return fail("database_integrity", "Turn projection patch chain is invalid"),
        };
    }
    Ok(projection)
}

/// The cache identity of `row`'s current attempt (`projection_cache_key`).
fn row_cache_key(session: &Session<'_>, row: &Row, attempt_id: &str) -> OpResult<ProjectionCacheKey> {
    Ok(projection_cache_key(
        session.backend,
        nonnegative(row_value(row, "user_id")?, "owner")?,
        &row_value(row, "conversation_id")?.text_or_empty(),
        &row_value(row, "turn_id")?.text_or_empty(),
        attempt_id,
    ))
}

/// `projection_from_turn_row(session, row)`: always an object, using a
/// revision-exact cache entry when one is live.
pub fn projection_from_turn_row(session: &Session<'_>, row: &Row) -> OpResult<Value> {
    let head = head_state(row)?;
    let revision = nonnegative(row_value(row, "projection_revision")?, "projection revision")?;
    let attempt_id = attempt_text(row);
    // Settled turns are eligible too: the key's exact revision evicts a
    // stale entry once any patch/checkpoint advances it.
    let cache_eligible = head.active()
        || head.checkpoint_active()
        || matches!(
            row.text("status").as_str(),
            "pending" | "running" | "completed" | "failed" | "interrupted" | "truncated" | "superseded"
        );
    let mut cache_key = None;
    if let Some(cache) = session.turn_projection_cache {
        if !attempt_id.is_empty() && cache_eligible {
            let key = row_cache_key(session, row, &attempt_id)?;
            if let Some(entry) = cache.get(&key, revision) {
                return Ok((*entry.projection).clone());
            }
            cache_key = Some(key);
        }
    }
    let base = load_base(session, row, &head, true)?;
    let projection = fold_head(session, row, &base.projection, &head)?;
    if let (Some(cache), Some(key)) = (session.turn_projection_cache, cache_key) {
        cache.remember(
            &key,
            revision,
            Arc::new(projection.clone()),
            ((base.payload_bytes + head.patch_bytes) * PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1),
            Some(base.payload_bytes),
            !head.active(),
            false,
        );
    }
    Ok(projection)
}

/// `discard_projection_cache_for_row`: release one row's reconstructible
/// live baseline after materialization.
pub fn discard_projection_cache_for_row(session: &Session<'_>, row: &Row) -> OpResult<bool> {
    let attempt_id = attempt_text(row);
    let Some(cache) = session.turn_projection_cache else { return Ok(false) };
    if attempt_id.is_empty() {
        return Ok(false);
    }
    Ok(cache.discard(&row_cache_key(session, row, &attempt_id)?))
}

/// `projection_from_turn_row` as a map.
pub fn projection_map(session: &Session<'_>, row: &Row) -> OpResult<Map<String, Value>> {
    match projection_from_turn_row(session, row)? {
        Value::Object(map) => Ok(map),
        _ => Ok(Map::new()),
    }
}

/// `delete_turn_projection_checkpoint`.
pub fn delete_checkpoint(session: &Session<'_>, turn_id: &str) -> OpResult<usize> {
    session.execute("DELETE FROM storage_turn_projection_checkpoints WHERE turn_id=?", &params![turn_id])
}

#[allow(clippy::too_many_arguments)]
pub fn update_projection_row(
    session: &Session<'_>,
    turn_id: &str,
    attempt_id: &str,
    status: &str,
    old_revision: i64,
    new_revision: i64,
    now: i64,
    head: &HeadState,
    projection_assignment: &str,
    projection_args: Vec<SqlValue>,
    settlement: Option<&Value>,
) -> OpResult<usize> {
    let settlement_assignment = if settlement.is_some() { ",settlement_json=?" } else { "" };
    let sql = format!(
        "UPDATE storage_conversation_turns SET status=?,{projection_assignment}projection_revision=?,projection_checkpoint_revision=?,projection_materialized_revision=?,projection_patch_count=?,projection_patch_bytes=?{settlement_assignment},updated_at=? WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?"
    );
    let mut args = vec![SqlValue::Text(status.to_owned())];
    args.extend(projection_args);
    args.push(SqlValue::Integer(new_revision));
    args.push(head.checkpoint_revision.into());
    args.push(head.materialized_revision.into());
    args.push(SqlValue::Integer(head.patch_count));
    args.push(SqlValue::Integer(head.patch_bytes));
    if let Some(settlement) = settlement {
        args.push(dump_param(settlement)?);
    }
    args.extend([
        SqlValue::Integer(now),
        SqlValue::Text(turn_id.to_owned()),
        SqlValue::Text(attempt_id.to_owned()),
        SqlValue::Integer(old_revision),
    ]);
    session.execute(&sql, &args)
}

#[allow(clippy::too_many_arguments)]
pub fn upsert_checkpoint(
    session: &Session<'_>,
    turn_id: &str,
    conversation_id: &str,
    user_id: i64,
    attempt_id: &str,
    revision: i64,
    projection_json: Vec<u8>,
    now: i64,
) -> OpResult<()> {
    let length = projection_json.len() as i64;
    let changed = session.execute(
        "INSERT INTO storage_turn_projection_checkpoints(turn_id,conversation_id,user_id,attempt_id,projection_revision,projection_json,projection_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET conversation_id=excluded.conversation_id,user_id=excluded.user_id,attempt_id=excluded.attempt_id,projection_revision=excluded.projection_revision,projection_json=excluded.projection_json,projection_bytes=excluded.projection_bytes,updated_at=excluded.updated_at",
        &params![turn_id, conversation_id, user_id, attempt_id, revision, projection_json, length, now],
    )?;
    if changed != 1 {
        return fail("database_integrity", "Turn projection checkpoint upsert changed no row");
    }
    Ok(())
}

/// `advance_unchanged_projection_revision`.
pub fn advance_unchanged_revision(
    session: &Session<'_>,
    row: &Row,
    projection: &Value,
    bridge_patch: &Value,
    now: i64,
) -> OpResult<i64> {
    let head = head_state(row)?;
    let old_revision = row.opt_int("projection_revision").unwrap_or(0);
    let new_revision = old_revision + 1;
    let turn_id = row.text("turn_id");
    let attempt_id = row.text("current_attempt_id");
    let status = row.text("status");
    let next = plan_head_append(&head, old_revision, dump(bridge_patch)?.len() as i64, true, false)?;
    let changed = if let Some(next) = next {
        update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &next,
            "",
            vec![],
            None,
        )?
    } else if head.active() || head.checkpoint_active() {
        let checkpoint_projection = dump(projection)?;
        let (assignment, args) = if head.checkpoint_active() {
            ("", vec![])
        } else {
            ("projection_json=?,", vec![dump_param(&Value::Object(Map::new()))?])
        };
        let state = HeadState { checkpoint_revision: Some(new_revision), ..HeadState::default() };
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &state,
            assignment,
            args,
            None,
        )?;
        if changed == 1 {
            upsert_checkpoint(
                session,
                &turn_id,
                &row.text("conversation_id"),
                row.int("user_id")?,
                &attempt_id,
                new_revision,
                checkpoint_projection,
                now,
            )?;
        }
        changed
    } else {
        update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &head,
            "",
            vec![],
            None,
        )?
    };
    if changed != 1 {
        return fail("database_conflict", "The turn changed during projection revision advance");
    }
    Ok(new_revision)
}
