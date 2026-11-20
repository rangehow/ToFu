//! `operation_domains/integration.py`: deterministic Git integration-control
//! storage operations (workspace registry, checkpoint queue, delivery
//! receipts and the bounded per-project event log).

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::error::{internal, OpResult, StorageError};
use crate::faults::inject_once;
use crate::json::{as_strict_int, float_value, python_float_repr};
use crate::params;
use crate::payload::{char_len, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "integration.workspace.register", OperationSpec::command(true, register_workspace));
    add(registry, "integration.workspace.get", OperationSpec::query(get_workspace));
    add(registry, "integration.workspace.save_checkpoint", OperationSpec::command(true, save_checkpoint));
    add(registry, "integration.workspace.submit", OperationSpec::command(true, submit_checkpoint));
    add(registry, "integration.workspace.retry", OperationSpec::command(true, retry_checkpoint));
    add(registry, "integration.workspace.discard", OperationSpec::command(true, discard));
    add(registry, "integration.workspace.set_meta", OperationSpec::command(true, set_meta));
    add(registry, "integration.workspace.claim_next", OperationSpec::command(true, claim_next));
    add(registry, "integration.workspace.peek_ready", OperationSpec::query(peek_ready));
    add(registry, "integration.workspace.delivery_page", OperationSpec::query(delivery_page));
    add(registry, "integration.workspace.delivery_action_page", OperationSpec::query(delivery_action_page));
    add(registry, "integration.workspace.get_integrating", OperationSpec::query(get_integrating));
    add(registry, "integration.workspace.quarantine", OperationSpec::command(true, quarantine));
    add(registry, "integration.workspace.requeue", OperationSpec::command(true, requeue));
    add(registry, "integration.workspace.mark_merged", OperationSpec::command(true, mark_merged));
    add(registry, "integration.workspace.mark_failed", OperationSpec::command(true, mark_failed));
    add(registry, "integration.event.record", OperationSpec::command(true, record_event));
    add(registry, "integration.status", OperationSpec::query(status));
}

fn protocol(message: impl Into<String>) -> crate::error::Failure {
    StorageError::protocol(message).into()
}

fn conflict(message: impl Into<String>) -> crate::error::Failure {
    StorageError::new("database_conflict", message).into()
}

/// Python `text[:count]` (code points).
fn prefix(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// `_text(payload, key, maximum, required=)`.
fn text(payload: &Payload, key: &str, maximum: usize, required: bool) -> OpResult<String> {
    let value = match payload.get(key) {
        None => "",
        Some(Value::String(value)) => value.as_str(),
        Some(_) => return Err(protocol(format!("Invalid {key} in integration storage request"))),
    };
    if char_len(value) > maximum {
        return Err(protocol(format!("Invalid {key} in integration storage request")));
    }
    if required && value.is_empty() {
        return Err(protocol(format!("Missing {key} in integration storage request")));
    }
    Ok(value.to_owned())
}

/// `_integer(payload, key, minimum=)`.
fn integer(payload: &Payload, key: &str, minimum: i64) -> OpResult<i64> {
    match payload.get(key).and_then(as_strict_int) {
        Some(value) if value >= minimum => Ok(value),
        _ => Err(protocol(format!("Invalid {key} in integration storage request"))),
    }
}

/// `_number(payload, key)`.
fn number(payload: &Payload, key: &str) -> OpResult<f64> {
    match payload.get(key) {
        Some(Value::Number(value)) => match value.as_f64() {
            Some(value) if value.is_finite() && value >= 0.0 => Ok(value),
            _ => Err(protocol(format!("Invalid {key} in integration storage request"))),
        },
        _ => Err(protocol(format!("Invalid {key} in integration storage request"))),
    }
}

/// Python `int(value)` for a JSON-decoded payload member (wide enough for
/// the later `max`/`min` clamps).
fn python_int(value: &Value) -> OpResult<i128> {
    match value {
        Value::Bool(flag) => Ok(*flag as i128),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                Ok(value as i128)
            } else if let Some(value) = number.as_u64() {
                Ok(value as i128)
            } else {
                match number.as_f64() {
                    Some(value) if value.is_finite() => Ok(value.trunc() as i128),
                    _ => internal("int() of a non-finite float"),
                }
            }
        }
        Value::String(text) => match text.trim().parse::<i128>() {
            Ok(value) => Ok(value),
            Err(_) => internal("invalid literal for int()"),
        },
        _ => internal("int() argument must be a string or a number"),
    }
}

/// Bind a Python int as a SQLite INTEGER (`OverflowError` beyond 64 bits).
fn bind_int(value: i128) -> OpResult<i64> {
    i64::try_from(value).or_else(|_| internal("Python int too large to convert to SQLite INTEGER"))
}

fn get<'m>(map: &'m Map<String, Value>, key: &str) -> &'m Value {
    static NULL: Value = Value::Null;
    map.get(key).unwrap_or(&NULL)
}

fn is_str(value: &Value, expected: &str) -> bool {
    value.as_str() == Some(expected)
}

/// Python `json.dumps(value, ensure_ascii=False, sort_keys=True)` (default
/// `', '` / `': '` separators, `float.__repr__` numbers).
fn python_json_dumps_sorted(value: &Value) -> String {
    fn write(out: &mut String, value: &Value) {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(true) => out.push_str("true"),
            Value::Bool(false) => out.push_str("false"),
            Value::Number(number) => match (number.as_i64(), number.as_u64()) {
                (Some(value), _) => out.push_str(&value.to_string()),
                (None, Some(value)) => out.push_str(&value.to_string()),
                _ => out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0))),
            },
            Value::String(text) => out.push_str(&serde_json::to_string(text).expect("string")),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push_str(", ");
                    }
                    write(out, item);
                }
                out.push(']');
            }
            Value::Object(map) => {
                out.push('{');
                let mut entries: Vec<(&String, &Value)> = map.iter().collect();
                entries.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
                for (index, (key, item)) in entries.into_iter().enumerate() {
                    if index > 0 {
                        out.push_str(", ");
                    }
                    out.push_str(&serde_json::to_string(key).expect("string"));
                    out.push_str(": ");
                    write(out, item);
                }
                out.push('}');
            }
        }
    }
    let mut out = String::new();
    write(&mut out, value);
    out
}

/// `_origin_document`: best-effort parse of the meta row's origin JSON.
fn origin_document(raw: &SqlValue) -> Map<String, Value> {
    let SqlValue::Text(raw) = raw else { return Map::new() };
    if raw.is_empty() {
        return Map::new();
    }
    match serde_json::from_str::<Value>(raw) {
        Ok(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

/// `_delivery_denied`.
fn delivery_denied(origin: &Map<String, Value>) -> bool {
    match origin.get("delivery") {
        Some(Value::Object(delivery)) => {
            matches!(get(delivery, "taskOutcome").as_str(), Some("failed" | "cancelled" | "stopped"))
        }
        _ => false,
    }
}

/// `_delivery_generation`.
fn delivery_generation(receipt: &Map<String, Value>) -> &str {
    get(receipt, "executorTaskId").as_str().unwrap_or("")
}

/// `_delivery_patch`: the receipt `proposed` may store over `stored`;
/// `None` keeps `stored`.
fn delivery_patch(stored: Option<&Value>, proposed: Map<String, Value>) -> Option<Map<String, Value>> {
    let Some(Value::Object(stored)) = stored else { return Some(proposed) };
    let generation = delivery_generation(&proposed);
    if generation != delivery_generation(stored) {
        let outcome = get(&proposed, "taskOutcome");
        if !generation.is_empty()
            && is_str(get(&proposed, "state"), "working")
            && (outcome.is_null() || is_str(outcome, ""))
        {
            return Some(proposed);
        }
        return None;
    }
    let mut wrapped = Map::new();
    wrapped.insert("delivery".into(), Value::Object(stored.clone()));
    if delivery_denied(&wrapped) {
        if !crate::codec::py_eq(get(&proposed, "taskOutcome"), get(stored, "taskOutcome")) {
            return None;
        }
        let mut merged = stored.clone();
        for (key, value) in proposed {
            merged.insert(key, value);
        }
        return Some(merged);
    }
    Some(proposed)
}

/// `_delivery_needs_action` over a `_row_document`.
fn delivery_needs_action(row: &Value, retained: bool) -> bool {
    let origin = row["origin"].as_object().expect("row document origin");
    let Some(Value::Object(receipt)) = origin.get("delivery") else { return false };
    let state = get(receipt, "state");
    if matches!(state.as_str(), Some("working" | "pending" | "blocked")) {
        return true;
    }
    if is_str(state, "failed") {
        return matches!(receipt.get("retry"), Some(Value::Object(retry))
            if is_str(get(retry, "condition"), "validation_inputs_changed"));
    }
    if !is_str(state, "delivered") || !is_str(get(origin, "executionMode"), "isolated-task") {
        return false;
    }
    if !origin.contains_key("deliveryCheckpoint")
        && is_str(get(receipt, "taskOutcome"), "completed")
        && is_str(get(receipt, "publicationMode"), "working_tree")
    {
        return true;
    }
    let retirement = get(origin, "sourceRetirement");
    row["managed"].as_i64().unwrap_or(0) != 0
        && !is_str(retirement, "reclaimed")
        && (retained || !is_str(retirement, "retained"))
}

/// `_row_document`.
fn row_document(row: &Row) -> OpResult<Value> {
    let created_at = match row.require("created_at")?.as_f64() {
        Some(value) => value,
        None => return internal("float() of created_at"),
    };
    let updated_at = match row.require("updated_at")?.as_f64() {
        Some(value) => value,
        None => return internal("float() of updated_at"),
    };
    Ok(json!({
        "id": row.int("id")?,
        "user_id": row.int("user_id")?,
        "project_root": row.text("project_root"),
        "task_id": row.text("task_id"),
        "title": row.text("title"),
        "workspace_path": row.text("workspace_path"),
        "managed": row.int("managed")?,
        "state": row.text("state"),
        "base_sha": row.text("base_sha"),
        "checkpoint_sha": row.text("checkpoint_sha"),
        "candidate_sha": row.text("candidate_sha"),
        "error": row.text("error"),
        "origin": Value::Object(origin_document(row.get("origin_json"))),
        "created_at": float_value(created_at),
        "updated_at": float_value(updated_at),
    }))
}

/// `_event_document`.
fn event_document(row: &Row) -> OpResult<Value> {
    let created_at = match row.require("created_at")?.as_f64() {
        Some(value) => value,
        None => return internal("float() of created_at"),
    };
    Ok(json!({
        "id": row.int("id")?,
        "user_id": row.int("user_id")?,
        "project_root": row.text("project_root"),
        "task_id": row.text("task_id"),
        "kind": row.text("kind"),
        "message": row.text("message"),
        "detail": row.text("detail"),
        "created_at": float_value(created_at),
    }))
}

fn workspace_row(session: &Session<'_>, user_id: i64, project_root: &str, task_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(
        "SELECT w.id, w.user_id, w.project_root, w.task_id, w.title, \
         w.workspace_path, \
         w.managed, w.state, w.base_sha, w.checkpoint_sha, w.candidate_sha, \
         w.error, w.created_at, w.updated_at, m.origin_json \
         FROM integration_workspaces w \
         LEFT JOIN integration_workspace_meta m \
         ON m.user_id = w.user_id AND m.project_root = w.project_root \
         AND m.task_id = w.task_id \
         WHERE w.user_id = ? AND w.project_root = ? AND w.task_id = ?",
        &params![user_id, project_root, task_id],
    )
}

fn required_workspace(session: &Session<'_>, user_id: i64, project_root: &str, task_id: &str) -> OpResult<Row> {
    match workspace_row(session, user_id, project_root, task_id)? {
        Some(row) => Ok(row),
        None => Err(conflict("Unknown integration task")),
    }
}

/// `_next_id`: the table is selected only by this module.
fn next_id(session: &Session<'_>, table: &str) -> OpResult<i64> {
    let row = session.fetch_one(&format!("SELECT COALESCE(MAX(id), 0) AS value FROM {table}"), &[])?;
    Ok(match row {
        Some(row) => row.int("value")?,
        None => 0,
    } + 1)
}

#[allow(clippy::too_many_arguments)]
fn append_event(
    session: &Session<'_>,
    user_id: i64,
    project_root: &str,
    task_id: &str,
    kind: &str,
    message: &str,
    detail: &str,
    now: f64,
) -> OpResult<()> {
    session.lock_key("integration.events", &format!("{user_id}|{project_root}"));
    let event_id = next_id(session, "integration_events")?;
    session.execute(
        "INSERT INTO integration_events(\
         id, user_id, project_root, task_id, kind, message, detail, created_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        &params![event_id, user_id, project_root, task_id, kind, prefix(message, 500), prefix(detail, 4000), now],
    )?;
    session.execute(
        "DELETE FROM integration_events WHERE user_id = ? AND project_root = ? \
         AND id NOT IN (SELECT id FROM integration_events \
         WHERE user_id = ? AND project_root = ? \
         ORDER BY id DESC LIMIT 300)",
        &params![user_id, project_root, user_id, project_root],
    )?;
    Ok(())
}

/// `_register_workspace`.
fn register_workspace(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let title = text(payload, "title", 1000, false)?;
    let workspace_path = text(payload, "workspace_path", 4096, true)?;
    let base_sha = text(payload, "base_sha", 200, true)?;
    let origin_json = text(payload, "origin_json", 4000, false)?;
    let Some(Value::Bool(managed)) = payload.get("managed") else {
        return Err(protocol("Invalid managed in integration storage request"));
    };
    let managed = *managed;
    let now = number(payload, "now")?;
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    session.lock_key("integration.checkout", &workspace_path);
    let claimed = session.fetch_one(
        "SELECT id FROM integration_workspaces WHERE workspace_path = ? \
         AND NOT (user_id = ? AND project_root = ? AND task_id = ?) \
         AND state != 'discarded' LIMIT 1",
        &params![&workspace_path, user_id, &project_root, &task_id],
    )?;
    if claimed.is_some() {
        return Err(conflict("Workspace already belongs to another work item"));
    }
    let existing = workspace_row(session, user_id, &project_root, &task_id)?;
    if let Some(existing) = &existing {
        if delivery_denied(&origin_document(existing.get("origin_json"))) {
            return Err(conflict("Task delivery has been revoked"));
        }
        if matches!(existing.text("state").as_str(), "ready" | "integrating" | "merged" | "discarded") {
            return Err(conflict("This task already has an immutable or terminal integration record"));
        }
    }
    match &existing {
        None => {
            session.lock_key("integration.workspace_ids", "global");
            let id = next_id(session, "integration_workspaces")?;
            session.execute(
                "INSERT INTO integration_workspaces(\
                 id, user_id, project_root, task_id, title, workspace_path, \
                 managed, state, base_sha, created_at, updated_at) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                &params![
                    id,
                    user_id,
                    &project_root,
                    &task_id,
                    &title,
                    &workspace_path,
                    managed as i64,
                    "running",
                    &base_sha,
                    now,
                    now
                ],
            )?;
        }
        Some(existing) => {
            session.execute(
                "UPDATE integration_workspaces SET title = ?, workspace_path = ?, \
                 managed = ?, base_sha = ?, checkpoint_sha = ?, candidate_sha = ?, \
                 state = ?, error = ?, updated_at = ? WHERE id = ?",
                &params![
                    &title,
                    &workspace_path,
                    managed as i64,
                    &base_sha,
                    "",
                    "",
                    "running",
                    "",
                    now,
                    existing.int("id")?
                ],
            )?;
        }
    }
    if !origin_json.is_empty() {
        upsert_meta(session, user_id, &project_root, &task_id, &origin_json, now)?;
    }
    inject_once("integration.after_workspace_mutation")?;
    append_event(
        session,
        user_id,
        &project_root,
        &task_id,
        "registered",
        "Writer workspace registered",
        &workspace_path,
        now,
    )?;
    Ok(json!({"ok": true}))
}

/// `_upsert_meta`.
fn upsert_meta(
    session: &Session<'_>,
    user_id: i64,
    project_root: &str,
    task_id: &str,
    origin_json: &str,
    now: f64,
) -> OpResult<()> {
    session.lock_key("integration.workspace_meta", &format!("{user_id}|{project_root}|{task_id}"));
    let changed = session.execute(
        "UPDATE integration_workspace_meta SET origin_json = ?, updated_at = ? \
         WHERE user_id = ? AND project_root = ? AND task_id = ?",
        &params![origin_json, now, user_id, project_root, task_id],
    )?;
    if changed == 0 {
        session.execute(
            "INSERT INTO integration_workspace_meta(\
             user_id, project_root, task_id, origin_json, updated_at) \
             VALUES (?, ?, ?, ?, ?)",
            &params![user_id, project_root, task_id, origin_json, now],
        )?;
    }
    Ok(())
}

/// `_get_workspace`.
fn get_workspace(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    match workspace_row(session, user_id, &project_root, &task_id)? {
        Some(row) => row_document(&row),
        None => Ok(Value::Null),
    }
}

/// `_save_checkpoint`.
fn save_checkpoint(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let checkpoint_sha = text(payload, "checkpoint_sha", 200, true)?;
    let base_sha = text(payload, "base_sha", 200, false)?;
    let now = number(payload, "now")?;
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    let row = required_workspace(session, user_id, &project_root, &task_id)?;
    let state = row.text("state");
    if matches!(state.as_str(), "ready" | "integrating") {
        return Err(conflict("The submitted checkpoint is immutable while it is in the integration queue"));
    }
    if matches!(state.as_str(), "discarded" | "merged") {
        return Err(conflict(format!(
            "The workspace is {state}; checkpointing it would resurrect a terminal integration record"
        )));
    }
    if !matches!(state.as_str(), "running" | "checkpointed" | "quarantined" | "failed") {
        return Err(conflict(format!("Workspace state {state} cannot be checkpointed")));
    }
    if !base_sha.is_empty() {
        session.execute(
            "UPDATE integration_workspaces SET checkpoint_sha = ?, base_sha = ?, \
             state = ?, error = ?, updated_at = ? WHERE id = ?",
            &params![&checkpoint_sha, &base_sha, "checkpointed", "", now, row.int("id")?],
        )?;
    } else {
        session.execute(
            "UPDATE integration_workspaces SET checkpoint_sha = ?, state = ?, \
             error = ?, updated_at = ? WHERE id = ?",
            &params![&checkpoint_sha, "checkpointed", "", now, row.int("id")?],
        )?;
    }
    append_event(
        session,
        user_id,
        &project_root,
        &task_id,
        "checkpointed",
        &format!("Checkpoint {} captured without staging the workspace", prefix(&checkpoint_sha, 12)),
        "",
        now,
    )?;
    Ok(json!({"ok": true}))
}

/// `_submit_checkpoint`.
fn submit_checkpoint(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let now = number(payload, "now")?;
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    let row = required_workspace(session, user_id, &project_root, &task_id)?;
    if delivery_denied(&origin_document(row.get("origin_json"))) {
        return Err(conflict("Task delivery has been revoked"));
    }
    let state = row.text("state");
    if state != "checkpointed" {
        return Err(conflict("Only a freshly checkpointed workspace can be submitted"));
    }
    if !row.get("checkpoint_sha").truthy() {
        return Err(conflict("Checkpoint the workspace before submitting"));
    }
    if state == "integrating" {
        return Err(conflict("The checkpoint is already integrating"));
    }
    session.execute(
        "UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? \
         WHERE id = ?",
        &params!["ready", "", now, row.int("id")?],
    )?;
    append_event(
        session,
        user_id,
        &project_root,
        &task_id,
        "submitted",
        "Checkpoint entered the deterministic integration queue",
        "",
        now,
    )?;
    Ok(json!({"ok": true}))
}

/// `_retry_checkpoint`.
fn retry_checkpoint(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let now = number(payload, "now")?;
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    let row = required_workspace(session, user_id, &project_root, &task_id)?;
    if delivery_denied(&origin_document(row.get("origin_json"))) {
        return Err(conflict("Task delivery has been revoked"));
    }
    if !matches!(row.text("state").as_str(), "quarantined" | "failed") {
        return Err(conflict("Only quarantined or failed checkpoints can be retried"));
    }
    if !row.get("checkpoint_sha").truthy() {
        return Err(conflict("Checkpoint the workspace before retrying"));
    }
    session.execute(
        "UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? \
         WHERE id = ?",
        &params!["ready", "", now, row.int("id")?],
    )?;
    append_event(
        session,
        user_id,
        &project_root,
        &task_id,
        "retried",
        "Quarantined checkpoint returned to the queue",
        "",
        now,
    )?;
    Ok(json!({"ok": true}))
}

macro_rules! workspace_columns {
    () => {
        "id, user_id, project_root, task_id, title, workspace_path, managed, \
         state, base_sha, checkpoint_sha, candidate_sha, error, created_at, \
         updated_at"
    };
}

/// Oldest ready row whose owner/project has no active integration — the
/// single eligibility predicate shared by the read-only peek and the claim.
const READY_SELECT: &str = concat!(
    "SELECT ",
    workspace_columns!(),
    " FROM integration_workspaces ",
    "WHERE state = ? ",
    "AND NOT EXISTS (SELECT 1 FROM integration_workspaces active ",
    "WHERE active.user_id = integration_workspaces.user_id ",
    "AND active.project_root = integration_workspaces.project_root ",
    "AND active.state = ?) ORDER BY updated_at ASC, id ASC LIMIT 1"
);

/// `_peek_ready`.
fn peek_ready(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let mut row = session.fetch_one(READY_SELECT, &params!["ready", "integrating"])?;
    if row.is_none() && payload.get("now").is_some_and(|value| !value.is_null()) {
        let now = number(payload, "now")?;
        row = session.fetch_one(
            concat!(
                "SELECT ",
                workspace_columns!(),
                " FROM integration_workspaces ",
                "WHERE state = ? AND updated_at < ? ",
                "ORDER BY updated_at ASC, id ASC LIMIT 1"
            ),
            &params!["integrating", now - 660.0],
        )?;
    }
    match row {
        Some(row) => row_document(&row),
        None => Ok(Value::Null),
    }
}

/// `_claim_next`.
fn claim_next(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let now = number(payload, "now")?;
    session.lock_key("integration.claim", "global");
    session.execute(
        "UPDATE integration_workspaces SET state = ?, error = ?, updated_at = ? \
         WHERE state = ? AND updated_at < ?",
        &params!["ready", "Recovered an interrupted integration", now, "integrating", now - 660.0],
    )?;
    let Some(row) = session.fetch_one(READY_SELECT, &params!["ready", "integrating"])? else {
        return Ok(Value::Null);
    };
    let current = required_workspace(session, row.int("user_id")?, &row.text("project_root"), &row.text("task_id"))?;
    if delivery_denied(&origin_document(current.get("origin_json"))) {
        session.execute(
            "UPDATE integration_workspaces SET state = ?, candidate_sha = ?, \
             updated_at = ? WHERE id = ? AND state = ?",
            &params!["checkpointed", "", now, row.int("id")?, "ready"],
        )?;
        return Ok(Value::Null);
    }
    let changed = session.execute(
        "UPDATE integration_workspaces SET state = ?, updated_at = ? \
         WHERE id = ? AND state = ?",
        &params!["integrating", now, row.int("id")?, "ready"],
    )?;
    if changed != 1 {
        return Ok(Value::Null);
    }
    let fresh = session.fetch_one(
        "SELECT id, user_id, project_root, task_id, title, workspace_path, managed, \
         state, base_sha, checkpoint_sha, candidate_sha, error, created_at, \
         updated_at FROM integration_workspaces WHERE id = ?",
        &params![row.int("id")?],
    )?;
    match fresh {
        Some(fresh) => row_document(&fresh),
        None => Ok(Value::Null),
    }
}

/// `max(0, int(payload.get('after_id', 0)))`, `max(1, min(128, int(payload.get('limit', 64))))`.
fn page_bounds(payload: &Payload) -> OpResult<(i64, i64)> {
    let after_id = python_int(payload.get("after_id").unwrap_or(&json!(0)))?.max(0);
    let limit = python_int(payload.get("limit").unwrap_or(&json!(64)))?.clamp(1, 128);
    Ok((bind_int(after_id)?, limit as i64))
}

const DELIVERY_PAGE_SELECT: &str = "SELECT w.*, m.origin_json FROM integration_workspaces w \
     LEFT JOIN integration_workspace_meta m ON m.user_id = w.user_id \
     AND m.project_root = w.project_root AND m.task_id = w.task_id \
     WHERE w.id > ? ORDER BY w.id LIMIT ?";

/// `_delivery_page`.
fn delivery_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (after_id, limit) = page_bounds(payload)?;
    let mut rows = session.fetch_all(DELIVERY_PAGE_SELECT, &params![after_id, limit + 1])?;
    let more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let next_after_id = if more { rows.last().expect("a full page").int("id")? } else { 0 };
    let documents = rows.iter().map(row_document).collect::<OpResult<Vec<_>>>()?;
    Ok(json!({"rows": documents, "hasMore": more, "nextAfterId": next_after_id}))
}

/// `_delivery_action_page`: windows are filtered here until a lookahead
/// match or the end.
fn delivery_action_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (after_id, limit) = page_bounds(payload)?;
    let retained = payload.get("retained") == Some(&Value::Bool(true));
    let mut rows: Vec<Value> = Vec::new();
    let mut cursor = after_id;
    while rows.len() as i64 <= limit {
        let window = session.fetch_all(DELIVERY_PAGE_SELECT, &params![cursor, limit + 1])?;
        for row in &window {
            let document = row_document(row)?;
            if delivery_needs_action(&document, retained) {
                rows.push(document);
                if rows.len() as i64 > limit {
                    break;
                }
            }
        }
        if window.len() as i64 <= limit {
            break;
        }
        cursor = window.last().expect("a full window").int("id")?;
    }
    let more = rows.len() as i64 > limit;
    rows.truncate(limit as usize);
    let next_after_id = if more { rows.last().expect("a full page")["id"].clone() } else { json!(0) };
    Ok(json!({"rows": rows, "hasMore": more, "nextAfterId": next_after_id}))
}

/// `_get_integrating`.
fn get_integrating(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let row_id = integer(payload, "row_id", 1)?;
    let row = session.fetch_one(
        "SELECT id, user_id, project_root, task_id, title, workspace_path, managed, \
         state, base_sha, checkpoint_sha, candidate_sha, error, created_at, \
         updated_at FROM integration_workspaces WHERE id = ? AND state = ?",
        &params![row_id, "integrating"],
    )?;
    match row {
        Some(row) => row_document(&row),
        None => Ok(Value::Null),
    }
}

/// `_cas_state`.
fn cas_state(
    session: &Session<'_>,
    payload: &Payload,
    state: &str,
    event_kind: Option<&str>,
    event_message: &str,
) -> OpResult<Value> {
    let row_id = integer(payload, "row_id", 1)?;
    let now = number(payload, "now")?;
    let error = text(payload, "error", 4000, false)?;
    let candidate_sha = text(payload, "candidate_sha", 200, false)?;
    let row = session.fetch_one(
        "SELECT user_id, project_root, task_id FROM integration_workspaces \
         WHERE id = ? AND state = ?",
        &params![row_id, "integrating"],
    )?;
    let Some(row) = row else { return Ok(json!({"changed": false})) };
    let mut assignments = String::from("state = ?, error = ?, updated_at = ?");
    let mut values: Vec<SqlValue> = vec![state.into(), prefix(&error, 4000).into(), now.into()];
    if state == "merged" {
        assignments.push_str(", candidate_sha = ?");
        values.push(candidate_sha.into());
    }
    values.push(row_id.into());
    values.push("integrating".into());
    let changed = session
        .execute(&format!("UPDATE integration_workspaces SET {assignments} WHERE id = ? AND state = ?"), &values)?;
    if changed == 1 {
        if let Some(event_kind) = event_kind.filter(|kind| !kind.is_empty()) {
            let detail = if matches!(state, "quarantined" | "failed") { error.as_str() } else { "" };
            append_event(
                session,
                row.int("user_id")?,
                &row.text("project_root"),
                &row.text("task_id"),
                event_kind,
                event_message,
                detail,
                now,
            )?;
        }
    }
    Ok(json!({"changed": changed == 1}))
}

fn quarantine(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    cas_state(session, payload, "quarantined", Some("quarantined"), "Checkpoint needs attention")
}

fn requeue(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    cas_state(session, payload, "ready", None, "")
}

fn mark_merged(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let candidate_sha = text(payload, "candidate_sha", 200, true)?;
    let mut enriched = payload.clone();
    enriched.insert("candidate_sha".into(), Value::String(candidate_sha.clone()));
    enriched.insert("error".into(), Value::String(String::new()));
    cas_state(
        session,
        &enriched,
        "merged",
        Some("merged"),
        &format!("Checkpoint integrated into candidate {}", prefix(&candidate_sha, 12)),
    )
}

fn mark_failed(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    cas_state(session, payload, "failed", Some("failed"), "Integration worker failed")
}

/// `_discard`: human discard parks the row terminally.
fn discard(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let now = number(payload, "now")?;
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    let row = required_workspace(session, user_id, &project_root, &task_id)?;
    match row.text("state").as_str() {
        "integrating" => return Err(conflict("Cannot discard while the checkpoint is integrating")),
        "merged" => return Err(conflict("A merged integration record is terminal and cannot be discarded")),
        "discarded" => return Ok(json!({"ok": true, "changed": false})),
        _ => {}
    }
    let changed = session.execute(
        "UPDATE integration_workspaces SET state = ?, updated_at = ? \
         WHERE id = ?",
        &params!["discarded", now, row.int("id")?],
    )?;
    append_event(
        session,
        user_id,
        &project_root,
        &task_id,
        "discarded",
        "Workspace discarded; refs and worktree kept for forensics",
        "",
        now,
    )?;
    Ok(json!({"ok": true, "changed": changed == 1}))
}

/// `_set_meta`: merge origin metadata; the delivery receipt changes only as
/// `_delivery_patch` admits.
fn set_meta(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, true)?;
    let patch_json = text(payload, "patch_json", 4000, true)?;
    let now = number(payload, "now")?;
    let mut patch = match serde_json::from_str::<Value>(&patch_json) {
        Ok(Value::Object(patch)) => patch,
        _ => return Err(protocol("Invalid patch_json in integration storage request")),
    };
    if patch.get("delivery").is_some_and(|delivery| !delivery.is_object()) {
        return Err(protocol("delivery must be a JSON object"));
    }
    session.lock_key("integration.workspace", &format!("{user_id}|{project_root}|{task_id}"));
    let row = required_workspace(session, user_id, &project_root, &task_id)?;
    let mut merged = origin_document(row.get("origin_json"));
    if let Some(Value::Object(proposed)) = patch.get("delivery").cloned() {
        match delivery_patch(merged.get("delivery"), proposed) {
            None => {
                patch.shift_remove("delivery");
            }
            Some(delivery) => {
                patch.insert("delivery".into(), Value::Object(delivery));
            }
        }
    }
    for (key, value) in patch {
        merged.insert(key, value);
    }
    let denied = delivery_denied(&merged);
    if denied {
        session.execute(
            "UPDATE integration_workspaces SET state = 'checkpointed', error = '', \
             candidate_sha = '', updated_at = ? \
             WHERE user_id = ? AND project_root = ? AND task_id = ? \
             AND state IN ('ready', 'integrating')",
            &params![now, user_id, &project_root, &task_id],
        )?;
    }
    let merged = Value::Object(merged);
    upsert_meta(session, user_id, &project_root, &task_id, &python_json_dumps_sorted(&merged), now)?;
    if denied {
        inject_once("integration.after_delivery_revocation")?;
    }
    Ok(json!({"ok": true, "origin": merged}))
}

/// `_record_event`.
fn record_event(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let task_id = text(payload, "task_id", 512, false)?;
    let kind = text(payload, "kind", 200, true)?;
    let message = text(payload, "message", 500, false)?;
    let detail = text(payload, "detail", 4000, false)?;
    let now = number(payload, "now")?;
    append_event(session, user_id, &project_root, &task_id, &kind, &message, &detail, now)?;
    Ok(json!({"ok": true}))
}

/// `_status`.
fn status(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", 1)?;
    let project_root = text(payload, "project_root", 4096, true)?;
    let rows = session.fetch_all(
        "SELECT w.id, w.user_id, w.project_root, w.task_id, w.title, \
         w.workspace_path, \
         w.managed, w.state, w.base_sha, w.checkpoint_sha, w.candidate_sha, \
         w.error, w.created_at, w.updated_at, m.origin_json \
         FROM integration_workspaces w \
         LEFT JOIN integration_workspace_meta m \
         ON m.user_id = w.user_id AND m.project_root = w.project_root \
         AND m.task_id = w.task_id \
         WHERE w.user_id = ? AND w.project_root = ? \
         ORDER BY w.updated_at DESC",
        &params![user_id, &project_root],
    )?;
    let events = session.fetch_all(
        "SELECT id, user_id, project_root, task_id, kind, message, detail, \
         created_at FROM integration_events \
         WHERE user_id = ? AND project_root = ? \
         ORDER BY id DESC LIMIT 30",
        &params![user_id, &project_root],
    )?;
    Ok(json!({
        "rows": rows.iter().map(row_document).collect::<OpResult<Vec<_>>>()?,
        "events": events.iter().map(event_document).collect::<OpResult<Vec<_>>>()?,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_json_matches_python_json_dumps() {
        let value = json!({"b": [1, 2.5, 1e-5, null], "a": {"é": "x\n\u{1}"}, "c": true});
        assert_eq!(
            python_json_dumps_sorted(&value),
            "{\"a\": {\"é\": \"x\\n\\u0001\"}, \"b\": [1, 2.5, 1e-05, null], \"c\": true}"
        );
    }

    #[test]
    fn delivery_patch_keeps_denials_and_admits_new_generations() {
        let stored = json!({"executorTaskId": "t1", "taskOutcome": "failed", "state": "failed"});
        let same = json!({"executorTaskId": "t1", "state": "delivered"}).as_object().unwrap().clone();
        assert!(delivery_patch(Some(&stored), same).is_none());
        let fresh = json!({"executorTaskId": "t2", "state": "working"}).as_object().unwrap().clone();
        assert!(delivery_patch(Some(&stored), fresh).is_some());
        let stale = json!({"executorTaskId": "t0", "state": "delivered"}).as_object().unwrap().clone();
        assert!(delivery_patch(Some(&stored), stale).is_none());
    }
}
