//! `operation_domains/workflows.py`: orchestration definitions, runs and
//! events, GoalRuns and swarm sessions (`operations_pkg/_runs.py`,
//! `operations_pkg/_orchestration_definitions.py`).

mod definitions;
mod goal;
mod runs;
mod swarm;

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{internal, OpResult, StorageError};
use crate::json;
use crate::payload::{char_len, integer, Payload};
use crate::session::SqlValue;

pub fn register(registry: &mut Registry) {
    add(registry, "orchestration.definition.list", OperationSpec::query(definitions::list));
    add(registry, "orchestration.definition.get", OperationSpec::query(definitions::get));
    add(registry, "orchestration.definition.create", OperationSpec::command(true, definitions::create));
    add(registry, "orchestration.definition.update", OperationSpec::command(true, definitions::update));
    add(registry, "orchestration.definition.delete", OperationSpec::command(true, definitions::delete));
    add(registry, "orchestration.run.create", OperationSpec::command(true, runs::run_create));
    add(registry, "orchestration.run.get", OperationSpec::query(runs::run_get));
    add(registry, "orchestration.run.list", OperationSpec::query(runs::run_list));
    add(registry, "orchestration.run.update_status", OperationSpec::command(true, runs::run_update));
    add(registry, "orchestration.run.retire_interrupted", OperationSpec::command(true, runs::run_retire));
    add(registry, "orchestration.run.retire_interrupted_all", OperationSpec::maintenance(runs::run_retire_all));
    add(registry, "orchestration.event.append", OperationSpec::command(false, runs::event_append));
    add(registry, "orchestration.event.project", OperationSpec::command(false, runs::event_project));
    add(registry, "orchestration.event.page", OperationSpec::query(runs::event_page));
    add(registry, "orchestration.run.delete", OperationSpec::command(true, runs::run_delete));
    add(registry, "goal.run.start", OperationSpec::command(true, goal::start));
    add(registry, "goal.run.transition", OperationSpec::command(true, goal::transition));
    add(registry, "goal.run.get", OperationSpec::query(goal::get));
    add(registry, "goal.run.latest", OperationSpec::query(goal::latest));
    add(registry, "swarm.session.save", OperationSpec::command(true, swarm::session_save));
    add(registry, "swarm.session.terminate", OperationSpec::command(true, swarm::session_terminate));
    add(
        registry,
        "swarm.session.quarantine_ownerless",
        OperationSpec::command(true, swarm::session_quarantine_ownerless),
    );
    add(registry, "swarm.session.delete", OperationSpec::command(true, swarm::session_delete));
    add(registry, "swarm.agent.save", OperationSpec::command(true, swarm::agent_save));
    add(registry, "swarm.agent.append", OperationSpec::command(true, swarm::agent_append));
    add(registry, "swarm.agent.checkpoint.mark", OperationSpec::command(true, swarm::agent_checkpoint_mark));
    add(registry, "swarm.agents.mark_delivered", OperationSpec::command(true, swarm::agents_mark_delivered));
    add(registry, "swarm.agents.acknowledge_results", OperationSpec::command(true, swarm::agents_acknowledge_results));
    add(registry, "swarm.session.get", OperationSpec::query(swarm::session_get));
    add(registry, "swarm.agents.page", OperationSpec::query(swarm::agents_page));
    add(registry, "swarm.agent.state.get", OperationSpec::query(swarm::agent_state_get));
    add(registry, "swarm.resumable.list", OperationSpec::query(swarm::resumable_list));
}

/// `lib/orchestration/run_status.py::VALID_RUN_STATUSES`.
const RUN_STATUSES: [&str; 6] = ["pending", "running", "paused", "done", "error", "aborted"];
/// `TERMINAL_RUN_STATUSES`; Python binds them in frozenset order, which only
/// feeds `NOT IN (...)`.
const TERMINAL_RUN_STATUSES: [&str; 3] = ["done", "error", "aborted"];
/// `_TERMINAL_STATUS_SQL`.
const TERMINAL_STATUS_SQL: &str = "?, ?, ?";

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn protocol<T>(message: &str) -> OpResult<T> {
    Err(StorageError::protocol(message).into())
}

fn terminal_params() -> [SqlValue; 3] {
    TERMINAL_RUN_STATUSES.map(SqlValue::from)
}

/// `_json_text(value)`: `_dump(value).decode()` bound as TEXT.
fn json_text(value: &Value) -> OpResult<String> {
    String::from_utf8(codec::dump(value)?).or_else(|_| internal("UnicodeDecodeError"))
}

/// Python `str.strip()` whitespace (`str.isspace`).
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `_run_owner` / `_workflow_owner`.
fn run_owner(payload: &Payload) -> OpResult<(i64, String)> {
    let tenant_id = match payload.get("tenant_id") {
        None => String::new(),
        Some(Value::String(text)) if char_len(text) <= 256 => text.clone(),
        Some(_) => return protocol("Invalid tenant_id in orchestration request"),
    };
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    Ok((user_id, py_strip(&tenant_id).to_owned()))
}

/// `is_run_status(value)`.
fn is_run_status(value: &Value) -> bool {
    value.as_str().is_some_and(|status| RUN_STATUSES.contains(&status))
}

/// `is_terminal_run_status(str(status or ''))` for a stored column.
fn is_terminal_column(value: &SqlValue) -> bool {
    TERMINAL_RUN_STATUSES.contains(&value.text_or_empty().as_str())
}

fn is_terminal_status(status: &str) -> bool {
    TERMINAL_RUN_STATUSES.contains(&status)
}

/// `row[column] or ''` as the JSON value Python would return.
fn or_empty(value: &SqlValue) -> OpResult<Value> {
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(String::new()))
    }
}

/// `int(value or 0)`.
fn int_or_zero(value: &SqlValue) -> OpResult<i64> {
    if !value.truthy() {
        return Ok(0);
    }
    match value.as_int() {
        Some(integer) => Ok(integer),
        None => internal("int() of a stored value"),
    }
}

/// `int(value)` (None is a TypeError).
fn int_of(value: &SqlValue) -> OpResult<i64> {
    match value.as_int() {
        Some(integer) => Ok(integer),
        None => internal("int() of a stored value"),
    }
}

/// Python `str(value)` of a column value.
fn sql_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(_) => {
            let text = value.text_or_empty();
            if text.is_empty() {
                "b''".into()
            } else {
                text
            }
        }
    }
}

/// `payload.get(key, default)` keeping an explicit JSON null.
fn get_or<'p>(payload: &'p Payload, key: &str, default: &'p Value) -> &'p Value {
    payload.get(key).unwrap_or(default)
}

/// `_optional_text(payload, field, default=, maximum=, scope=)`.
fn optional_text(payload: &Payload, field: &str, default: &str, maximum: usize, scope: &str) -> OpResult<String> {
    match payload.get(field) {
        None => {
            if char_len(default) > maximum {
                return protocol(&format!("Invalid {scope} {field}"));
            }
            Ok(default.to_owned())
        }
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(text.clone()),
        Some(_) => protocol(&format!("Invalid {scope} {field}")),
    }
}

/// `value if isinstance(value, dict) else {}` for decoded documents.
fn object_or_empty(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(map) => map,
        _ => Map::new(),
    }
}
