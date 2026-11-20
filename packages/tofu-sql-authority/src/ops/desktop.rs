//! `operation_domains/desktop.py`: owner-scoped durable desktop egress-agent
//! preference (`operations_pkg/_desktop.py`).

use serde_json::{json, Value};

use super::{register as add, OperationSpec, Registry};
use crate::error::{OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, integer, Payload};
use crate::session::Session;

const MAX_AGENT_ID_CHARS: usize = 128;

pub fn register(registry: &mut Registry) {
    add(registry, "desktop.egress_agent.get", OperationSpec::query(egress_agent_get));
    add(registry, "desktop.egress_agent.initialize", OperationSpec::command(true, egress_agent_initialize));
    add(registry, "desktop.egress_agent.set", OperationSpec::command(true, egress_agent_set));
}

fn owner_user_id(payload: &Payload) -> OpResult<i64> {
    integer(payload, "owner_user_id", None, Some(1), None)
}

fn agent_id(payload: &Payload) -> OpResult<String> {
    match payload.get("agent_id") {
        Some(Value::String(value)) if char_len(value) <= MAX_AGENT_ID_CHARS => Ok(value.clone()),
        _ => Err(StorageError::protocol("Invalid agent_id in storage request").into()),
    }
}

fn read_preference(session: &Session<'_>, owner_user_id: i64) -> OpResult<Value> {
    let row = session.fetch_one(
        "SELECT agent_id,updated_at_ms FROM storage_desktop_egress_preferences WHERE owner_user_id=?",
        &params![owner_user_id],
    )?;
    Ok(match row {
        None => json!({"present": false, "agent_id": "", "updated_at_ms": 0}),
        Some(row) => json!({
            "present": true,
            "agent_id": row.text("agent_id"),
            "updated_at_ms": if row.get("updated_at_ms").truthy() { row.int("updated_at_ms")? } else { 0 },
        }),
    })
}

/// `_desktop_egress_agent_get`.
fn egress_agent_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    read_preference(session, owner_user_id(payload)?)
}

/// `_write_preference`.
fn write_preference(session: &Session<'_>, owner_user_id: i64, agent_id: &str, initialize: bool) -> OpResult<Value> {
    session.lock_key("desktop.egress_agent", &owner_user_id.to_string());
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0);
    if initialize {
        session.execute(
            "INSERT INTO storage_desktop_egress_preferences(owner_user_id,agent_id,updated_at_ms) VALUES (?,?,?) \
             ON CONFLICT(owner_user_id) DO NOTHING",
            &params![owner_user_id, agent_id, now_ms],
        )?;
    } else {
        session.execute(
            "INSERT INTO storage_desktop_egress_preferences(owner_user_id,agent_id,updated_at_ms) VALUES (?,?,?) \
             ON CONFLICT(owner_user_id) DO UPDATE SET agent_id=excluded.agent_id,updated_at_ms=excluded.updated_at_ms",
            &params![owner_user_id, agent_id, now_ms],
        )?;
    }
    read_preference(session, owner_user_id)
}

fn egress_agent_initialize(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner_user_id(payload)?;
    let agent = agent_id(payload)?;
    write_preference(session, owner, &agent, true)
}

fn egress_agent_set(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner_user_id(payload)?;
    let agent = agent_id(payload)?;
    write_preference(session, owner, &agent, false)
}
