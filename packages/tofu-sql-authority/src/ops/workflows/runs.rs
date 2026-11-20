//! Orchestration run headers and events (`operations_pkg/_runs.py`).

use serde_json::{json, Map, Value};

use super::goal::goal_event_append;
use super::{
    get_or, int_of, int_or_zero, is_run_status, is_terminal_column, is_terminal_status, json_text, now_ms, or_empty,
    protocol, run_owner, sql_str, terminal_params, TERMINAL_STATUS_SQL,
};
use crate::codec;
use crate::error::{fail, internal, OpResult};
use crate::json::{self as pyjson, truthy};
use crate::params;
use crate::payload::{integer, required_text, text_or_empty, Payload};
use crate::session::{Row, Session, SqlValue};

/// `lib/goal_runs/contract.py::GOAL_RUN_CREATED_BY`.
const GOAL_RUN_CREATED_BY: &str = "chat_goal_mode";
/// `CONTINUITY_ELECTION_CLOSED_EVENT_TYPES`.
const CONTINUITY_ELECTION_CLOSED_EVENT_TYPES: [&str; 4] =
    ["execution_recovery_claim", "continuity_resumed", "continuity_refused", "continuity_exhausted"];
/// `MAX_FRAME_BYTES // 8`.
const ORCHESTRATION_EVENT_PAGE_BYTES: i64 = 64 * 1024 * 1024 / 8;

/// `_run_status(payload, optional=)`.
fn run_status(payload: &Payload, optional: bool) -> OpResult<String> {
    let empty = Value::String(String::new());
    let value = get_or(payload, "status", &empty);
    if optional && value.as_str() == Some("") {
        return Ok(String::new());
    }
    if !is_run_status(value) {
        return protocol("Invalid orchestration run status");
    }
    Ok(value.as_str().unwrap_or_default().to_owned())
}

/// `_decode_run_error`: undecodable text is preserved as `str(value)`.
fn decode_run_error(value: &SqlValue) -> OpResult<Value> {
    let bytes = match value {
        SqlValue::Null => return Ok(Value::Null),
        SqlValue::Text(text) if text.is_empty() => return Ok(Value::Null),
        SqlValue::Text(text) => text.as_bytes(),
        SqlValue::Blob(bytes) => bytes.as_slice(),
        other => return codec::load(other),
    };
    match pyjson::loads(bytes) {
        Ok(decoded) => codec::load_value(decoded),
        Err(_) => Ok(Value::String(sql_str(value))),
    }
}

/// `_run_row(row, detail=)`.
fn run_row(row: &Row, detail: bool) -> OpResult<Value> {
    let mut status = row.text("status");
    if status.is_empty() {
        status = "pending".into();
    }
    let terminal = is_terminal_status(&status);
    let mut result = Map::new();
    result.insert("id".into(), row.require("id")?.to_json()?);
    result.insert("orch_id".into(), or_empty(row.get("orch_id"))?);
    result.insert("name".into(), or_empty(row.get("name"))?);
    result.insert("status".into(), Value::String(status));
    result.insert("terminal".into(), Value::Bool(terminal));
    result.insert("final".into(), or_empty(row.get("final"))?);
    result.insert("error".into(), decode_run_error(row.get("error"))?);
    result.insert("created_by".into(), or_empty(row.get("created_by"))?);
    result.insert("created_at".into(), Value::from(int_or_zero(row.get("created_at"))?));
    result.insert("updated_at".into(), Value::from(int_or_zero(row.get("updated_at"))?));
    result.insert("finished_at".into(), Value::from(int_or_zero(row.get("finished_at"))?));
    if detail {
        let definition = codec::load(row.get("definition"))?;
        if !definition.is_object() {
            return fail("database_integrity", "Durable orchestration definition is not an object");
        }
        result.insert("definition".into(), definition);
        result.insert("input".into(), or_empty(row.get("input"))?);
    }
    Ok(Value::Object(result))
}

pub(super) fn run_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let Some(Value::Object(definition)) = payload.get("definition") else {
        return protocol("Invalid orchestration definition");
    };
    let now = now_ms();
    session.execute(
        concat!(
            "INSERT INTO orchestration_runs(",
            "id, user_id, tenant_id, orch_id, name, definition, input, status, ",
            "created_by, created_at, updated_at) ",
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        ),
        &params![
            run_id,
            user_id,
            tenant_id,
            text_or_empty(payload, "orch_id"),
            text_or_empty(payload, "name"),
            json_text(&Value::Object(definition.clone()))?,
            text_or_empty(payload, "input"),
            "pending",
            text_or_empty(payload, "created_by"),
            now,
            now,
        ],
    )?;
    Ok(json!({"created": true}))
}

pub(super) fn run_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let row = session.fetch_one(
        concat!(
            "SELECT id, orch_id, name, definition, input, status, final, error, ",
            "created_by, created_at, updated_at, finished_at ",
            "FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![run_id, user_id, tenant_id],
    )?;
    match row {
        Some(row) => run_row(&row, true),
        None => Ok(Value::Null),
    }
}

pub(super) fn run_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, tenant_id) = run_owner(payload)?;
    let empty = Value::String(String::new());
    let status = get_or(payload, "status", &empty);
    let orch_id = get_or(payload, "orch_id", &empty);
    if truthy(status) && !is_run_status(status) {
        return protocol("Invalid orchestration run status");
    }
    let (Some(status), Some(orch_id)) = (status.as_str(), orch_id.as_str()) else {
        return protocol("Invalid orchestration run filter");
    };
    let limit = integer(payload, "limit", Some(50), Some(1), Some(200))?;
    let rows = session.fetch_all(
        concat!(
            "SELECT id, orch_id, name, status, final, error, created_by, ",
            "created_at, updated_at, finished_at FROM orchestration_runs ",
            "WHERE user_id = ? AND tenant_id = ? ",
            "AND (? = '' OR status = ?) AND (? = '' OR orch_id = ?) ",
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        ),
        &params![user_id, tenant_id, status, status, orch_id, orch_id, limit],
    )?;
    Ok(Value::Array(rows.iter().map(|row| run_row(row, false)).collect::<OpResult<_>>()?))
}

pub(super) fn run_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let status = run_status(payload, false)?;
    let now = now_ms();
    let final_value = payload.get("final").filter(|value| !value.is_null());
    let error_present = payload.contains_key("error");
    let error = payload.get("error").unwrap_or(&Value::Null);
    let row = session.fetch_one_for_update(
        concat!(
            "SELECT status, final, error, finished_at FROM orchestration_runs ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![&run_id, user_id, &tenant_id],
    )?;
    let Some(row) = row else { return Ok(json!({"changed": false})) };
    if is_terminal_column(row.get("status")) && row.get("status").as_str() != Some(status.as_str()) {
        return Ok(json!({"changed": false}));
    }
    let next_final = match final_value {
        None => row.get("final").clone(),
        Some(value) => SqlValue::Text(pyjson::python_str(value)),
    };
    let next_error = if !error_present {
        row.get("error").clone()
    } else if let Value::String(text) = error {
        SqlValue::Text(text.clone())
    } else {
        SqlValue::Text(json_text(error)?)
    };
    let mut finished = int_or_zero(row.get("finished_at"))?;
    if is_terminal_status(&status) {
        if finished == 0 {
            finished = now;
        }
    } else {
        finished = 0;
    }
    let count = session.execute(
        concat!(
            "UPDATE orchestration_runs SET status = ?, final = ?, error = ?, ",
            "updated_at = ?, finished_at = ? ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![&status, next_final, next_error, now, finished, &run_id, user_id, &tenant_id],
    )?;
    if count > 0 && error_present {
        session.execute(
            concat!(
                "UPDATE orchestration_runs SET continuity_pending = 0 ",
                "WHERE id = ? AND user_id = ? AND tenant_id = ?"
            ),
            &params![&run_id, user_id, &tenant_id],
        )?;
    }
    Ok(json!({"changed": count > 0}))
}

/// `_retire_orchestration_runs(session, payload, owner=)`.
fn retire_runs(session: &Session<'_>, payload: &Payload, owner: Option<(i64, String)>) -> OpResult<Value> {
    let mut clause = format!("status NOT IN ({TERMINAL_STATUS_SQL})");
    let mut args: Vec<SqlValue> = terminal_params().to_vec();
    if let Some((user_id, tenant_id)) = &owner {
        clause.push_str(" AND user_id = ? AND tenant_id = ?");
        args.push(SqlValue::from(*user_id));
        args.push(SqlValue::from(tenant_id));
    }
    let rows = session.fetch_all(
        &format!("SELECT id, user_id, tenant_id, status, created_by FROM orchestration_runs WHERE {clause} LIMIT 1001"),
        &args,
    )?;
    if rows.len() > 1000 {
        return fail("database_resource_exhausted", "Run retirement exceeds its bound");
    }
    let error = payload.get("error").unwrap_or(&Value::Null);
    let encoded = match error {
        Value::String(text) => text.clone(),
        other => json_text(other)?,
    };
    let now = now_ms();
    let mut retired = 0usize;
    let mut goals: Vec<&Row> = Vec::new();
    let mut candidates: Vec<Value> = Vec::new();
    for row in &rows {
        let boundary = [row.get("id").clone(), row.get("user_id").clone(), row.get("tenant_id").clone()];
        if row.get("status").as_str() == Some("paused") {
            let last = session.fetch_one(
                concat!(
                    "SELECT payload FROM orchestration_run_events ",
                    "WHERE run_id = ? AND user_id = ? AND tenant_id = ? ORDER BY seq DESC LIMIT 1"
                ),
                &boundary,
            )?;
            let event = match last {
                Some(last) => codec::load(last.get("payload"))?,
                None => Value::Object(Map::new()),
            };
            let Value::Object(event) = event else { return internal("AttributeError: get") };
            if (event.get("type").and_then(Value::as_str) == Some("human_request")
                && matches!(event.get("continuation"), Some(Value::Object(_))))
                || event.get("continuation_expired") == Some(&Value::Bool(true))
            {
                continue;
            }
        }
        let changed = session.execute(
            concat!(
                "UPDATE orchestration_runs SET status = 'error', final = '', error = ?, ",
                "updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? ELSE finished_at END ",
                "WHERE id = ? AND user_id = ? AND tenant_id = ?"
            ),
            &params![&encoded, now, now, &boundary[0], &boundary[1], &boundary[2]],
        )?;
        retired += changed;
        let is_goal = row.get("created_by").as_str() == Some(GOAL_RUN_CREATED_BY);
        if changed > 0
            && owner.is_none()
            && is_goal
            && matches!(error, Value::Object(map)
                if map.get("kind").and_then(Value::as_str) == Some("worker_lost")
                && map.get("source").and_then(Value::as_str) == Some("orchestration.startup_recovery"))
        {
            session.execute(
                concat!(
                    "UPDATE orchestration_runs SET continuity_pending = 1 ",
                    "WHERE id = ? AND user_id = ? AND tenant_id = ?"
                ),
                &boundary,
            )?;
        }
        if changed > 0 {
            // Boot-continuity election (docs/RUN_CONTINUITY.md D2): the pass
            // resumes exactly the runs settled in this boot generation.
            candidates.push(json!({
                "run_id": row.get("id").to_json()?,
                "user_id": row.get("user_id").to_json()?,
                "tenant_id": row.get("tenant_id").to_json()?,
                "created_by": or_empty(row.get("created_by"))?,
            }));
        }
        if is_goal {
            goals.push(row);
        }
    }
    append_interrupted_goal_transitions(session, &goals, error)?;
    if owner.is_none() {
        // Indexed durable election survives loss of the process-local boot slot.
        let pending = session.fetch_all(
            concat!(
                "SELECT id, user_id, tenant_id, created_by FROM orchestration_runs ",
                "WHERE continuity_pending = 1 ORDER BY finished_at, id LIMIT 1000"
            ),
            &[],
        )?;
        let mut descriptors: Vec<(Value, Value)> = Vec::new();
        for row in &pending {
            let key = row.get("id").to_json()?;
            let descriptor = json!({
                "run_id": key.clone(),
                "user_id": row.get("user_id").to_json()?,
                "tenant_id": row.get("tenant_id").to_json()?,
                "created_by": row.get("created_by").to_json()?,
            });
            match descriptors.iter_mut().find(|(existing, _)| *existing == key) {
                Some(slot) => slot.1 = descriptor,
                None => descriptors.push((key, descriptor)),
            }
        }
        // Fresh non-Goal retirements keep their explicit-resume descriptors.
        for candidate in candidates {
            if descriptors.len() >= 1000 {
                break;
            }
            let key = candidate["run_id"].clone();
            if !descriptors.iter().any(|(existing, _)| *existing == key) {
                descriptors.push((key, candidate));
            }
        }
        candidates = descriptors.into_iter().map(|(_, descriptor)| descriptor).collect();
    }
    Ok(json!({"retired": retired, "candidates": candidates}))
}

/// `_append_interrupted_goal_transitions`.
fn append_interrupted_goal_transitions(session: &Session<'_>, rows: &[&Row], error: &Value) -> OpResult<()> {
    let error_projection = match error {
        Value::Object(map) => Value::Object(map.clone()),
        other if truthy(other) => Value::String(pyjson::python_str(other)),
        _ => Value::String(String::new()),
    };
    for row in rows {
        let mut event = Map::new();
        event.insert("type".into(), json!("goal_run_transition"));
        event.insert("status".into(), json!("failed"));
        event.insert("reason".into(), json!("worker_lost"));
        event.insert("outcome".into(), json!({"error": error_projection.clone()}));
        goal_event_append(
            session,
            &sql_str(row.get("id")),
            int_of(row.get("user_id"))?,
            &row.text("tenant_id"),
            event,
        )?;
    }
    Ok(())
}

pub(super) fn run_retire(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = run_owner(payload)?;
    retire_runs(session, payload, Some(owner))
}

pub(super) fn run_retire_all(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    retire_runs(session, payload, None)
}

/// `_orchestration_event_append`; returns whether the row was inserted.
fn append_event(session: &Session<'_>, payload: &Payload) -> OpResult<bool> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let sequence = integer(payload, "sequence", None, Some(0), None)?;
    let Some(Value::Object(event)) = payload.get("event") else {
        return protocol("Invalid orchestration event");
    };
    let encoded = json_text(&Value::Object(event.clone()))?;
    if session
        .fetch_one_for_update(
            concat!("SELECT 1 AS present FROM orchestration_runs ", "WHERE id = ? AND user_id = ? AND tenant_id = ?"),
            &params![&run_id, user_id, &tenant_id],
        )?
        .is_none()
    {
        return fail("database_not_found", "Orchestration run does not exist");
    }
    let empty = Value::Null;
    let field = |key: &str| {
        let value = event.get(key).unwrap_or(&empty);
        if truthy(value) {
            pyjson::python_str(value)
        } else {
            String::new()
        }
    };
    let inserted = session.execute(
        concat!(
            "INSERT INTO orchestration_run_events(",
            "run_id, user_id, tenant_id, seq, type, node_id, payload, ts) ",
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ",
            "ON CONFLICT(run_id, user_id, tenant_id, seq) DO NOTHING"
        ),
        &params![&run_id, user_id, &tenant_id, sequence, field("type"), field("node_id"), &encoded, now_ms()],
    )?;
    if inserted == 0 {
        let row = session.fetch_one(
            concat!(
                "SELECT payload FROM orchestration_run_events ",
                "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq = ?"
            ),
            &params![&run_id, user_id, &tenant_id, sequence],
        )?;
        let existing = match row {
            Some(row) => Some(json_text(&codec::load(row.get("payload"))?)?),
            None => None,
        };
        if existing.as_deref() != Some(encoded.as_str()) {
            return fail("database_conflict", "Orchestration event sequence has a conflicting payload");
        }
    }
    if event
        .get("type")
        .and_then(Value::as_str)
        .is_some_and(|kind| CONTINUITY_ELECTION_CLOSED_EVENT_TYPES.contains(&kind))
    {
        session.execute(
            concat!(
                "UPDATE orchestration_runs SET continuity_pending = 0 ",
                "WHERE id = ? AND user_id = ? AND tenant_id = ?"
            ),
            &params![&run_id, user_id, &tenant_id],
        )?;
    }
    Ok(inserted > 0)
}

pub(super) fn event_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let inserted = append_event(session, payload)?;
    Ok(json!({"inserted": inserted, "accepted": true}))
}

pub(super) fn event_project(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, tenant_id) = run_owner(payload)?;
    let status = run_status(payload, true)?;
    if is_terminal_status(&status) {
        return protocol("Terminal orchestration status requires an explicit transition");
    }
    if !append_event(session, payload)? {
        return Ok(json!({"projected": true, "inserted": false}));
    }
    let run_id = pyjson::python_str(payload.get("run_id").unwrap_or(&Value::Null));
    let now = now_ms();
    let [done, error, aborted] = terminal_params();
    let count = if !status.is_empty() {
        session.execute(
            &format!(
                "UPDATE orchestration_runs SET status = ?, updated_at = ?, finished_at = 0 WHERE id = ? AND user_id = ? AND tenant_id = ? AND status NOT IN ({TERMINAL_STATUS_SQL})"
            ),
            &params![&status, now, &run_id, user_id, &tenant_id, done, error, aborted],
        )?
    } else {
        session.execute(
            &format!(
                "UPDATE orchestration_runs SET updated_at = ? WHERE id = ? AND user_id = ? AND tenant_id = ? AND status NOT IN ({TERMINAL_STATUS_SQL})"
            ),
            &params![now, &run_id, user_id, &tenant_id, done, error, aborted],
        )?
    };
    if count == 0 {
        return fail("database_conflict", "Orchestration run header rejected event projection");
    }
    Ok(json!({"projected": true, "inserted": true}))
}

pub(super) fn event_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let requested = integer(payload, "cursor", Some(0), Some(0), None)?;
    let boundary_row = session.fetch_one(
        concat!(
            "SELECT COALESCE(MAX(seq) + 1, 0) AS next_cursor ",
            "FROM orchestration_run_events ",
            "WHERE run_id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![&run_id, user_id, &tenant_id],
    )?;
    let boundary = match boundary_row {
        Some(row) => int_or_zero(row.get("next_cursor"))?,
        None => 0,
    };
    if requested > boundary {
        return Ok(json!({"events": [], "next_cursor": boundary, "cursor_reset": true, "caught_up": true}));
    }
    let lengths = session.fetch_all(
        concat!(
            "SELECT seq, LENGTH(CAST(payload AS BLOB)) AS payload_bytes ",
            "FROM orchestration_run_events ",
            "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq >= ? ",
            "ORDER BY seq LIMIT 2000"
        ),
        &params![&run_id, user_id, &tenant_id, requested],
    )?;
    let (mut count, mut total_bytes) = (0i64, 1024i64);
    for row in &lengths {
        // Account for the sequence field added to historical event payloads.
        let size = int_of(row.get("payload_bytes"))? + 32;
        if total_bytes + size > ORCHESTRATION_EVENT_PAGE_BYTES {
            if count == 0 {
                return protocol("Orchestration event exceeds the replay page byte boundary");
            }
            break;
        }
        count += 1;
        total_bytes += size;
    }
    let rows = if count > 0 {
        session.fetch_all(
            concat!(
                "SELECT seq, payload FROM orchestration_run_events ",
                "WHERE run_id = ? AND user_id = ? AND tenant_id = ? AND seq >= ? ",
                "ORDER BY seq LIMIT ?"
            ),
            &params![&run_id, user_id, &tenant_id, requested, count],
        )?
    } else {
        Vec::new()
    };
    let mut events = Vec::with_capacity(rows.len());
    for row in &rows {
        let Value::Object(mut event) = codec::load(row.get("payload"))? else {
            return fail("database_integrity", "Durable orchestration event is not an object");
        };
        if !event.contains_key("seq") {
            event.insert("seq".into(), Value::from(int_of(row.get("seq"))?));
        }
        events.push(Value::Object(event));
    }
    let next_cursor = match rows.last() {
        Some(last) => boundary.min(int_of(last.get("seq"))? + 1),
        None => boundary,
    };
    Ok(json!({
        "events": events,
        "next_cursor": next_cursor,
        "cursor_reset": false,
        "caught_up": next_cursor >= boundary,
    }))
}

pub(super) fn run_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    // Serialize event deletion with transaction-scoped recovery observations.
    session.fetch_one_for_update(
        "SELECT id FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        &params![&run_id, user_id, &tenant_id],
    )?;
    session.execute(
        concat!("DELETE FROM orchestration_run_events ", "WHERE run_id = ? AND user_id = ? AND tenant_id = ?"),
        &params![&run_id, user_id, &tenant_id],
    )?;
    let count = session.execute(
        concat!("DELETE FROM orchestration_runs ", "WHERE id = ? AND user_id = ? AND tenant_id = ?"),
        &params![&run_id, user_id, &tenant_id],
    )?;
    Ok(json!({"deleted": count > 0}))
}
