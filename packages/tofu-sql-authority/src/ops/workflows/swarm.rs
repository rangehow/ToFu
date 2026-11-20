//! Durable swarm sessions, agent checkpoints and compact markers
//! (`operations_pkg/_runs.py`, `lib/swarm/persistence_contract.py`).

use serde_json::{json, Map, Value};

use super::{int_of, int_or_zero, json_text, optional_text, or_empty, protocol, py_strip};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const SWARM_SESSION_STATUS_RUNNING: &str = "running";
const SWARM_SESSION_STATUS_TERMINATED: &str = "terminated";
const SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS: &str = "quarantined:ownerless";
/// `SWARM_SESSION_WRITABLE_STATUSES`, sorted as `_swarm_resumable_list` binds them.
const SWARM_SESSION_WRITABLE_STATUSES: [&str; 2] = ["running", "terminated"];
/// `_SWARM_NONTERMINAL`, sorted.
const SWARM_NONTERMINAL: [&str; 3] = ["pending", "retrying", "running"];
const SWARM_CHECKPOINT_MARKER_RESULT_FIELDS: [&str; 8] = [
    "actuals_complete",
    "checkpoint_phase",
    "completion_tokens",
    "cost_usd",
    "elapsed_seconds",
    "prompt_tokens",
    "rounds_used",
    "total_tokens",
];
const SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS: usize = 64;
/// `MAX_FRAME_BYTES - 4096 - 4096`.
const SWARM_AGENT_PAGE_BYTES: i64 = 64 * 1024 * 1024 - 4096 - 4096;
const SWARM_AGENT_FIELD_OVERHEAD_BYTES: i64 = 256;

const SWARM_MARKER_COLUMNS: &str = concat!(
    "m.result_json AS marker_result_json, m.rounds_used AS marker_rounds_used, ",
    "m.updated_at AS marker_updated_at"
);
const SWARM_MARKER_JOIN: &str = concat!(
    "LEFT JOIN swarm_agent_checkpoint_markers AS m ",
    "ON m.swarm_key = a.swarm_key AND m.agent_id = a.agent_id"
);
/// `_SWARM_AGENT_SELECT`.
fn swarm_agent_select() -> String {
    format!(
        "SELECT a.agent_id AS agent_id, a.role AS role, a.objective AS objective, a.status AS status, a.messages_json AS messages_json, a.result_json AS result_json, a.rounds_used AS rounds_used, a.delivered AS delivered, a.updated_at AS updated_at, {SWARM_MARKER_COLUMNS} FROM swarm_agents AS a {SWARM_MARKER_JOIN}"
    )
}

/// `_swarm_json(value, list, field)`.
fn swarm_list_json(value: Option<&Value>, field: &str) -> OpResult<String> {
    match value {
        Some(value @ Value::Array(_)) => json_text(value),
        _ => protocol(&format!("Invalid swarm {field}")),
    }
}

/// `row[column] or default` for TEXT status columns.
fn or_default(value: &SqlValue, default: &str) -> OpResult<Value> {
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(default.into()))
    }
}

/// `require_user_id(value)` succeeds (`lib/identity.py`).
// private port of lib/identity.py::require_user_id; shared owner: identity
fn is_valid_user_id(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Number(number)) => {
            if let Some(integer) = number.as_i64() {
                integer > 0
            } else {
                number.as_u64().is_some()
            }
        }
        Some(Value::String(text)) => {
            // int(text, 10): optional sign, digits with single underscores between digits.
            let text = py_strip(text);
            let (negative, digits) = match text.as_bytes().first() {
                Some(b'-') => (true, &text[1..]),
                Some(b'+') => (false, &text[1..]),
                _ => (false, text),
            };
            let valid = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
            valid && !negative && digits.chars().any(|c| c.is_ascii_digit() && c != '0')
        }
        _ => false,
    }
}

pub(super) fn session_save(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let specs_json = swarm_list_json(payload.get("specs"), "specs")?;
    let Some(Value::Object(config)) = payload.get("config") else {
        return protocol("Invalid swarm config");
    };
    let config_json = json_text(&Value::Object(config.clone()))?;
    let now = integer(payload, "now_ms", None, Some(0), None)?;
    let status = optional_text(payload, "status", "running", 64, "swarm")?;
    if !SWARM_SESSION_WRITABLE_STATUSES.contains(&status.as_str()) {
        return protocol("Invalid swarm session status");
    }
    session.lock_key("swarm.session", &swarm_key);
    let conv_id = optional_text(payload, "conv_id", "", 4096, "swarm")?;
    let task_id = optional_text(payload, "task_id", "", 4096, "swarm")?;
    session.execute(
        concat!(
            "INSERT INTO swarm_sessions(",
            "swarm_key, conv_id, task_id, status, specs_json, config_json, ",
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ",
            "ON CONFLICT(swarm_key) DO UPDATE SET ",
            "conv_id = excluded.conv_id, task_id = excluded.task_id, ",
            "status = excluded.status, specs_json = excluded.specs_json, ",
            "config_json = excluded.config_json, updated_at = excluded.updated_at"
        ),
        &params![swarm_key, conv_id, task_id, status, specs_json, config_json, now, now],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn session_terminate(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let now = integer(payload, "now_ms", None, Some(0), None)?;
    let count = session.execute(
        concat!("UPDATE swarm_sessions SET status = ?, updated_at = ? ", "WHERE swarm_key = ? AND status = ?"),
        &params![SWARM_SESSION_STATUS_TERMINATED, now, swarm_key, SWARM_SESSION_STATUS_RUNNING],
    )?;
    Ok(json!({"changed": count > 0}))
}

/// Quarantine one invalid legacy session without deleting child evidence;
/// ownership is re-checked inside this transaction.
pub(super) fn session_quarantine_ownerless(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    session.lock_key("swarm.session", &swarm_key);
    let item = session
        .fetch_one("SELECT status, config_json FROM swarm_sessions WHERE swarm_key = ?", &params![&swarm_key])?;
    let Some(item) = item else { return Ok(json!({"changed": false})) };
    let Value::Object(config) = codec::load(item.get("config_json"))? else {
        return fail("database_integrity", "Durable swarm session config is invalid");
    };
    if is_valid_user_id(config.get("user_id")) {
        return Ok(json!({"changed": false}));
    }
    let mut current_status = item.text("status");
    if current_status.is_empty() {
        current_status = "running".into();
    }
    if current_status == SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS {
        return Ok(json!({"changed": false}));
    }
    let count = session.execute(
        concat!(
            "UPDATE swarm_sessions SET status = ?, updated_at = ? ",
            "WHERE swarm_key = ? AND status = ? AND config_json = ?"
        ),
        &params![
            SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
            now_ms,
            &swarm_key,
            current_status,
            item.get("config_json")
        ],
    )?;
    Ok(json!({"changed": count > 0}))
}

pub(super) fn session_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    session.execute("DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ?", &params![&swarm_key])?;
    session.execute("DELETE FROM swarm_agents WHERE swarm_key = ?", &params![&swarm_key])?;
    let count = session.execute("DELETE FROM swarm_sessions WHERE swarm_key = ?", &params![&swarm_key])?;
    Ok(json!({"deleted": count > 0}))
}

fn agent_lock_key(swarm_key: &str, agent_id: &str) -> String {
    format!("{}:{swarm_key}{agent_id}", char_len(swarm_key))
}

pub(super) fn agent_save(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let agent_id = required_text(payload, "agent_id", 512)?;
    let messages_json = swarm_list_json(payload.get("messages"), "messages")?;
    let Some(Value::Object(result)) = payload.get("result") else {
        return protocol("Invalid swarm result");
    };
    let result_json = json_text(&Value::Object(result.clone()))?;
    let rounds_used = integer(payload, "rounds_used", Some(0), Some(0), Some(1_000_000))?;
    let now = integer(payload, "now_ms", None, Some(0), None)?;
    let delivered = match payload.get("delivered") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(flag)) => Some(*flag),
        Some(_) => return protocol("Invalid swarm delivered flag"),
    };
    let checkpoint_id = optional_text(payload, "checkpoint_id", "", 512, "swarm")?;
    // PostgreSQL TEXT rejects NUL bytes; a length-prefixed composite key is
    // unambiguous on both backends and safe for advisory-lock hashing.
    session.lock_key("swarm.agent", &agent_lock_key(&swarm_key, &agent_id));
    let values = params![
        &swarm_key,
        &agent_id,
        optional_text(payload, "role", "", 4096, "swarm")?,
        optional_text(payload, "objective", "", 100_000, "swarm")?,
        optional_text(payload, "status", "pending", 64, "swarm")?,
        messages_json,
        result_json,
        rounds_used,
        delivered.unwrap_or(false),
        now,
        &checkpoint_id,
    ];
    if delivered.is_none() {
        session.execute(
            concat!(
                "INSERT INTO swarm_agents(",
                "swarm_key, agent_id, role, objective, status, messages_json, ",
                "result_json, rounds_used, delivered, updated_at, checkpoint_id) ",
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
                "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET ",
                "role = excluded.role, objective = excluded.objective, ",
                "status = excluded.status, messages_json = excluded.messages_json, ",
                "result_json = excluded.result_json, ",
                "rounds_used = excluded.rounds_used, updated_at = excluded.updated_at, ",
                "checkpoint_id = excluded.checkpoint_id"
            ),
            &values,
        )?;
    } else {
        session.execute(
            concat!(
                "INSERT INTO swarm_agents(",
                "swarm_key, agent_id, role, objective, status, messages_json, ",
                "result_json, rounds_used, delivered, updated_at, checkpoint_id) ",
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
                "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET ",
                "role = excluded.role, objective = excluded.objective, ",
                "status = excluded.status, messages_json = excluded.messages_json, ",
                "result_json = excluded.result_json, rounds_used = excluded.rounds_used, ",
                "delivered = excluded.delivered, updated_at = excluded.updated_at, ",
                "checkpoint_id = excluded.checkpoint_id"
            ),
            &values,
        )?;
    }
    // A full checkpoint supersedes every earlier compact marker.
    session.execute(
        "DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id = ?",
        &params![&swarm_key, &agent_id],
    )?;
    // Echoing the id acknowledges it as the base of a later swarm.agent.append.
    if checkpoint_id.is_empty() {
        Ok(json!({"saved": true}))
    } else {
        Ok(json!({"saved": true, "checkpoint_id": checkpoint_id}))
    }
}

/// Append new messages to the exact checkpoint this writer last wrote; any
/// other base answers `appended: false` and the writer saves in full.
pub(super) fn agent_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let agent_id = required_text(payload, "agent_id", 512)?;
    let base_checkpoint_id = required_text(payload, "base_checkpoint_id", 512)?;
    let checkpoint_id = required_text(payload, "checkpoint_id", 512)?;
    let base_message_count = integer(payload, "base_message_count", None, Some(0), None)?;
    let Some(Value::Array(appended)) = payload.get("messages") else {
        return protocol("Invalid swarm messages");
    };
    let Some(Value::Object(result)) = payload.get("result") else {
        return protocol("Invalid swarm result");
    };
    let result_json = json_text(&Value::Object(result.clone()))?;
    let rounds_used = integer(payload, "rounds_used", Some(0), Some(0), Some(1_000_000))?;
    let now = integer(payload, "now_ms", None, Some(0), None)?;
    let status = optional_text(payload, "status", "pending", 64, "swarm")?;
    session.lock_key("swarm.agent", &agent_lock_key(&swarm_key, &agent_id));
    let row = session.fetch_one(
        concat!(
            "SELECT role, objective, messages_json, checkpoint_id FROM swarm_agents ",
            "WHERE swarm_key = ? AND agent_id = ?"
        ),
        &params![&swarm_key, &agent_id],
    )?;
    let Some(row) = row.filter(|row| row.get("checkpoint_id").as_str() == Some(base_checkpoint_id.as_str())) else {
        return Ok(json!({"appended": false}));
    };
    let Value::Array(mut messages) = codec::load(row.get("messages_json"))? else {
        return fail("database_integrity", "Durable swarm agent JSON is invalid");
    };
    if messages.len() as i64 != base_message_count {
        return Ok(json!({"appended": false}));
    }
    messages.extend(appended.iter().cloned());
    let messages_json = json_text(&Value::Array(messages))?;
    // A full save of this transcript must fit one storage frame and every
    // agent page must still carry it, so appends stop at the same page bound.
    let row_bytes =
        [agent_id.clone(), row.text("role"), row.text("objective"), messages_json.clone(), result_json.clone()]
            .iter()
            .map(|text| text.len() as i64)
            .sum::<i64>();
    if row_bytes + SWARM_AGENT_FIELD_OVERHEAD_BYTES > SWARM_AGENT_PAGE_BYTES {
        return protocol("Storage frame exceeds the size limit");
    }
    session.execute(
        concat!(
            "UPDATE swarm_agents SET status = ?, messages_json = ?, result_json = ?, ",
            "rounds_used = ?, updated_at = ?, checkpoint_id = ? ",
            "WHERE swarm_key = ? AND agent_id = ?"
        ),
        &params![status, messages_json, result_json, rounds_used, now, checkpoint_id, &swarm_key, &agent_id],
    )?;
    session.execute(
        "DELETE FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id = ?",
        &params![&swarm_key, &agent_id],
    )?;
    Ok(json!({"appended": true}))
}

/// `_swarm_checkpoint_marker(value)`.
fn checkpoint_marker(value: Option<&Value>) -> OpResult<Map<String, Value>> {
    let invalid = || protocol("Invalid swarm checkpoint marker");
    let Some(Value::Object(marker)) = value else { return invalid() };
    if marker.is_empty() || !marker.keys().all(|key| SWARM_CHECKPOINT_MARKER_RESULT_FIELDS.contains(&key.as_str())) {
        return invalid();
    }
    for item in marker.values() {
        match item {
            Value::String(text) => {
                if char_len(text) > SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS {
                    return invalid();
                }
            }
            Value::Null | Value::Bool(_) | Value::Number(_) => {}
            _ => return invalid(),
        }
    }
    Ok(marker.clone())
}

/// Overlay compact checkpoint fields on one agent's last full checkpoint.
pub(super) fn agent_checkpoint_mark(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let agent_id = required_text(payload, "agent_id", 512)?;
    let mut marker = checkpoint_marker(payload.get("result"))?;
    let rounds_used = integer(payload, "rounds_used", None, Some(0), Some(1_000_000))?;
    let now = integer(payload, "now_ms", None, Some(0), None)?;
    session.lock_key("swarm.agent", &agent_lock_key(&swarm_key, &agent_id));
    if session
        .fetch_one(
            "SELECT 1 AS present FROM swarm_agents WHERE swarm_key = ? AND agent_id = ?",
            &params![&swarm_key, &agent_id],
        )?
        .is_none()
    {
        return Ok(json!({"marked": false}));
    }
    let current = session.fetch_one(
        concat!("SELECT result_json FROM swarm_agent_checkpoint_markers ", "WHERE swarm_key = ? AND agent_id = ?"),
        &params![&swarm_key, &agent_id],
    )?;
    if let Some(current) = current {
        let Value::Object(mut previous) = codec::load(current.get("result_json"))? else {
            return fail("database_integrity", "Durable swarm checkpoint marker is invalid");
        };
        for (key, value) in marker {
            previous.insert(key, value);
        }
        marker = previous;
    }
    session.execute(
        concat!(
            "INSERT INTO swarm_agent_checkpoint_markers(",
            "swarm_key, agent_id, result_json, rounds_used, updated_at) ",
            "VALUES (?, ?, ?, ?, ?) ",
            "ON CONFLICT(swarm_key, agent_id) DO UPDATE SET ",
            "result_json = excluded.result_json, rounds_used = excluded.rounds_used, ",
            "updated_at = excluded.updated_at"
        ),
        &params![&swarm_key, &agent_id, json_text(&Value::Object(marker))?, rounds_used, now],
    )?;
    Ok(json!({"marked": true}))
}

/// `_swarm_delivery_ids(payload)`: the key and order-preserving unique ids.
fn delivery_ids(payload: &Payload) -> OpResult<(String, Vec<String>)> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let agent_ids = match payload.get("agent_ids") {
        Some(Value::Array(items))
            if items.len() <= 1000
                && items
                    .iter()
                    .all(|item| matches!(item, Value::String(text) if !text.is_empty() && char_len(text) <= 512)) =>
        {
            items
        }
        _ => return protocol("Invalid swarm agent_ids"),
    };
    let mut unique: Vec<String> = Vec::with_capacity(agent_ids.len());
    for item in agent_ids {
        let text = item.as_str().unwrap_or_default();
        if !unique.iter().any(|seen| seen == text) {
            unique.push(text.to_owned());
        }
    }
    Ok((swarm_key, unique))
}

fn mark_delivered(session: &Session<'_>, payload: &Payload) -> OpResult<usize> {
    let (swarm_key, agent_ids) = delivery_ids(payload)?;
    let mut changed = 0usize;
    for agent_id in &agent_ids {
        changed += session.execute(
            concat!("UPDATE swarm_agents SET delivered = 1 ", "WHERE swarm_key = ? AND agent_id = ?"),
            &params![&swarm_key, agent_id],
        )?;
    }
    Ok(changed)
}

pub(super) fn agents_acknowledge_results(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (swarm_key, _) = delivery_ids(payload)?;
    let task_id = required_text(payload, "task_id", 4096)?;
    // Share the replacement writer's lock, including PostgreSQL's read/ACK
    // window. A process-local session check cannot fence another writer.
    session.lock_key("swarm.session", &swarm_key);
    let row = session.fetch_one("SELECT task_id FROM swarm_sessions WHERE swarm_key = ?", &params![&swarm_key])?;
    if row.is_none_or(|row| row.get("task_id").as_str() != Some(task_id.as_str())) {
        return Ok(json!({"acknowledged": false, "changed": 0}));
    }
    Ok(json!({"acknowledged": true, "changed": mark_delivered(session, payload)?}))
}

pub(super) fn agents_mark_delivered(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    Ok(json!({"changed": mark_delivered(session, payload)?}))
}

/// `_swarm_marked_state(agent)`: marker overlay, rounds and updated_at.
fn marked_state(agent: &Row) -> OpResult<(Map<String, Value>, i64, i64)> {
    if agent.get("marker_result_json").is_null() {
        return Ok((Map::new(), int_or_zero(agent.get("rounds_used"))?, int_or_zero(agent.get("updated_at"))?));
    }
    let Value::Object(marker) = codec::load(agent.get("marker_result_json"))? else {
        return fail("database_integrity", "Durable swarm checkpoint marker is invalid");
    };
    Ok((marker, int_or_zero(agent.get("marker_rounds_used"))?, int_or_zero(agent.get("marker_updated_at"))?))
}

/// `_swarm_agent_row(agent)`.
fn agent_row(agent: &Row) -> OpResult<Value> {
    let messages = codec::load(agent.get("messages_json"))?;
    let result = codec::load(agent.get("result_json"))?;
    let (Value::Array(_), Value::Object(mut result)) = (&messages, result) else {
        return fail("database_integrity", "Durable swarm agent JSON is invalid");
    };
    let (marker, rounds_used, updated_at) = marked_state(agent)?;
    for (key, value) in marker {
        result.insert(key, value);
    }
    Ok(json!({
        "agent_id": agent.require("agent_id")?.to_json()?,
        "role": or_empty(agent.get("role"))?,
        "objective": or_empty(agent.get("objective"))?,
        "status": or_default(agent.get("status"), "pending")?,
        "messages": messages,
        "result": result,
        "rounds_used": rounds_used,
        "delivered": agent.get("delivered").truthy(),
        "updated_at": updated_at,
    }))
}

pub(super) fn session_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let include_agents = match payload.get("include_agents") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return protocol("Invalid swarm include_agents flag"),
    };
    let item = session.fetch_one(
        concat!(
            "SELECT swarm_key, conv_id, task_id, status, specs_json, config_json, ",
            "created_at, updated_at FROM swarm_sessions WHERE swarm_key = ?"
        ),
        &params![&swarm_key],
    )?;
    let Some(item) = item else { return Ok(Value::Null) };
    let specs = codec::load(item.get("specs_json"))?;
    let config = codec::load(item.get("config_json"))?;
    if !specs.is_array() || !config.is_object() {
        return fail("database_integrity", "Durable swarm session JSON is invalid");
    }
    let mut detail = Map::new();
    detail.insert("swarm_key".into(), item.require("swarm_key")?.to_json()?);
    detail.insert("conv_id".into(), or_empty(item.get("conv_id"))?);
    detail.insert("task_id".into(), or_empty(item.get("task_id"))?);
    detail.insert("status".into(), or_default(item.get("status"), "running")?);
    detail.insert("specs".into(), specs);
    detail.insert("config".into(), config);
    detail.insert("created_at".into(), Value::from(int_or_zero(item.get("created_at"))?));
    detail.insert("updated_at".into(), Value::from(int_or_zero(item.get("updated_at"))?));
    if include_agents {
        let rows = session.fetch_all(
            &format!("{} WHERE a.swarm_key = ? ORDER BY a.agent_id", swarm_agent_select()),
            &params![&swarm_key],
        )?;
        detail.insert("agents".into(), Value::Array(rows.iter().map(agent_row).collect::<OpResult<_>>()?));
    }
    Ok(Value::Object(detail))
}

/// Session header without specs/config plus one agent's compact state.
pub(super) fn agent_state_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let agent_id = required_text(payload, "agent_id", 512)?;
    let item = session.fetch_one(
        concat!(
            "SELECT swarm_key, conv_id, task_id, status, created_at, updated_at ",
            "FROM swarm_sessions WHERE swarm_key = ?"
        ),
        &params![&swarm_key],
    )?;
    let Some(item) = item else { return Ok(Value::Null) };
    let agent = session.fetch_one(
        &format!(
            "SELECT a.agent_id AS agent_id, a.status AS status, a.rounds_used AS rounds_used, a.delivered AS delivered, a.updated_at AS updated_at, {SWARM_MARKER_COLUMNS} FROM swarm_agents AS a {SWARM_MARKER_JOIN} WHERE a.swarm_key = ? AND a.agent_id = ?"
        ),
        &params![&swarm_key, &agent_id],
    )?;
    let agent_value = match &agent {
        None => Value::Null,
        Some(agent) => {
            let (_, rounds_used, updated_at) = marked_state(agent)?;
            json!({
                "agent_id": agent.require("agent_id")?.to_json()?,
                "status": or_default(agent.get("status"), "pending")?,
                "rounds_used": rounds_used,
                "delivered": agent.get("delivered").truthy(),
                "updated_at": updated_at,
            })
        }
    };
    Ok(json!({
        "swarm_key": item.require("swarm_key")?.to_json()?,
        "conv_id": or_empty(item.get("conv_id"))?,
        "task_id": or_empty(item.get("task_id"))?,
        "status": or_default(item.get("status"), "running")?,
        "created_at": int_or_zero(item.get("created_at"))?,
        "updated_at": int_or_zero(item.get("updated_at"))?,
        "agent": agent_value,
    }))
}

pub(super) fn agents_page(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let swarm_key = required_text(payload, "swarm_key", 512)?;
    let after = optional_text(payload, "after_agent_id", "", 512, "swarm")?;
    let remaining = session.fetch_all(
        concat!(
            "SELECT agent_id, LENGTH(CAST(agent_id AS BLOB)) + LENGTH(CAST(role AS BLOB)) + ",
            "LENGTH(CAST(objective AS BLOB)) + LENGTH(CAST(messages_json AS BLOB)) + ",
            "LENGTH(CAST(result_json AS BLOB)) AS row_bytes FROM swarm_agents ",
            "WHERE swarm_key = ? AND agent_id > ? ORDER BY agent_id"
        ),
        &params![&swarm_key, &after],
    )?;
    let marker_rows = session.fetch_all(
        concat!(
            "SELECT agent_id, LENGTH(CAST(result_json AS BLOB)) AS marker_bytes ",
            "FROM swarm_agent_checkpoint_markers WHERE swarm_key = ? AND agent_id > ?"
        ),
        &params![&swarm_key, &after],
    )?;
    let mut marker_bytes: Vec<(SqlValue, i64)> = Vec::with_capacity(marker_rows.len());
    for row in &marker_rows {
        let bytes = int_of(row.get("marker_bytes"))?;
        let key = row.get("agent_id").clone();
        match marker_bytes.iter_mut().find(|(existing, _)| *existing == key) {
            Some(slot) => slot.1 = bytes,
            None => marker_bytes.push((key, bytes)),
        }
    }
    let (mut count, mut page_bytes) = (0usize, 0i64);
    for row in &remaining {
        let marker = marker_bytes.iter().find(|(key, _)| key == row.get("agent_id")).map_or(0, |(_, bytes)| *bytes);
        let row_bytes = int_of(row.get("row_bytes"))? + marker + SWARM_AGENT_FIELD_OVERHEAD_BYTES;
        if count > 0 && page_bytes + row_bytes > SWARM_AGENT_PAGE_BYTES {
            break;
        }
        count += 1;
        page_bytes += row_bytes;
    }
    let agents: Vec<Value> = if count > 0 {
        session
            .fetch_all(
                &format!(
                    "{} WHERE a.swarm_key = ? AND a.agent_id > ? ORDER BY a.agent_id LIMIT ?",
                    swarm_agent_select()
                ),
                &params![&swarm_key, &after, count],
            )?
            .iter()
            .map(agent_row)
            .collect::<OpResult<_>>()?
    } else {
        Vec::new()
    };
    let next_after = if count < remaining.len() {
        agents.last().map(|agent| agent["agent_id"].clone()).unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    Ok(json!({"agents": agents, "next_after_agent_id": next_after}))
}

/// Keys only: each session is read separately, so history never overflows.
pub(super) fn resumable_list(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    let [normal_a, normal_b] = SWARM_SESSION_WRITABLE_STATUSES;
    let [pending, retrying, running] = SWARM_NONTERMINAL;
    let rows = session.fetch_all(
        concat!(
            "SELECT s.swarm_key AS swarm_key, s.updated_at AS updated_at ",
            "FROM swarm_sessions AS s WHERE s.status IN (?, ?) AND EXISTS (",
            "SELECT 1 FROM swarm_agents AS candidate ",
            "WHERE candidate.swarm_key = s.swarm_key AND (",
            "candidate.status IN (?, ?, ?) OR ",
            "(candidate.status = ? AND candidate.delivered = 0))) ",
            "ORDER BY s.updated_at DESC, s.swarm_key"
        ),
        &params![normal_a, normal_b, pending, retrying, running, "completed"],
    )?;
    let mut sessions = Vec::with_capacity(rows.len());
    for row in &rows {
        sessions.push(json!({
            "swarm_key": row.require("swarm_key")?.to_json()?,
            "updated_at": int_or_zero(row.get("updated_at"))?,
        }));
    }
    Ok(Value::Array(sessions))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_validation_matches_require_user_id() {
        for (value, valid) in [
            (json!(1), true),
            (json!(0), false),
            (json!(-3), false),
            (json!(true), false),
            (json!(2.0), false),
            (json!(" 12 "), true),
            (json!("+7"), true),
            (json!("1_000"), true),
            (json!("1__0"), false),
            (json!("_1"), false),
            (json!("000"), false),
            (json!("-5"), false),
            (json!("abc"), false),
            (json!(""), false),
            (Value::Null, false),
        ] {
            assert_eq!(is_valid_user_id(Some(&value)), valid, "{value}");
        }
        assert!(!is_valid_user_id(None));
    }
}
