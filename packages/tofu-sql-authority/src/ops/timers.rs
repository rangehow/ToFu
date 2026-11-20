//! `operation_domains/timers.py`: durable scheduler timer operations
//! (`operations_pkg/_timers.py`, timer half).

pub(crate) mod py;

use serde_json::{json, Value};

use self::py::{bind, bind_get, column_str, document, int_get, int_or, py_strip, text_or};
use super::{register as add, OperationSpec, Registry};
use crate::codec::dump_param;
use crate::error::{fail, OpResult};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "timer.create", OperationSpec::command(true, timer_create));
    add(registry, "timer.cancel", OperationSpec::command(true, timer_cancel));
    add(registry, "timer.get", OperationSpec::query(timer_get));
    add(registry, "timer.list", OperationSpec::query(timer_list));
    add(registry, "timer.active.list_all", OperationSpec::query(timer_active_list_all));
    add(registry, "timer.active.count", OperationSpec::query(timer_active_count));
    add(registry, "timer.history", OperationSpec::query(timer_history));
    add(registry, "timer.update", OperationSpec::command(true, timer_update));
    add(registry, "timer.poll.append", OperationSpec::command(false, timer_poll_append));
    add(registry, "timer.poll.commit", OperationSpec::command(true, timer_poll_commit));
    add(registry, "timer.progress", OperationSpec::command(true, timer_progress));
    add(registry, "timer.poll.log", OperationSpec::query(timer_poll_log));
}

const TIMER_COLUMNS: &[&str] = &[
    "id",
    "user_id",
    "conv_id",
    "source_task_id",
    "check_instruction",
    "check_command",
    "continuation_message",
    "poll_interval",
    "max_polls",
    "poll_count",
    "status",
    "tools_config",
    "created_at",
    "updated_at",
    "triggered_at",
    "cancelled_at",
    "execution_task_id",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promotion_streak",
    "fallback_streak",
    "promoted_at",
    "origin",
];

const TIMER_NUMERIC: &[&str] =
    &["poll_interval", "max_polls", "poll_count", "promotion_streak", "fallback_streak", "user_id"];

fn timer_id(payload: &Payload) -> OpResult<String> {
    required_text(payload, "timer_id", 128)
}

fn timer_owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn timer_document(row: &crate::session::Row) -> OpResult<Value> {
    document(row, TIMER_COLUMNS, TIMER_NUMERIC)
}

fn columns() -> String {
    TIMER_COLUMNS.join(", ")
}

fn timer_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let sql = format!("SELECT {} FROM storage_timers WHERE id = ? AND user_id = ?", columns());
    let row = session.fetch_one(&sql, &params![timer_id(payload)?, timer_owner(payload)?])?;
    match row {
        None => Ok(Value::Null),
        Some(row) => timer_document(&row),
    }
}

fn timer_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(50), Some(1), Some(200))?;
    let user_id = timer_owner(payload)?;
    let sql = format!(
        "SELECT {} FROM storage_timers WHERE user_id=? \
         ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC LIMIT ?",
        columns()
    );
    let rows = session.fetch_all(&sql, &params![user_id, limit])?;
    Ok(Value::Array(rows.iter().map(timer_document).collect::<OpResult<_>>()?))
}

fn timer_history(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let row = session.fetch_one(
        "SELECT 1 AS present FROM storage_timers WHERE user_id=? LIMIT 1",
        &params![timer_owner(payload)?],
    )?;
    Ok(Value::Bool(row.is_some()))
}

fn timer_active_list_all(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(200), Some(0), Some(2000))?;
    let sql = format!(
        "SELECT {} FROM storage_timers WHERE status='active' ORDER BY created_at ASC{}",
        columns(),
        if limit != 0 { " LIMIT ?" } else { "" }
    );
    let rows = if limit != 0 { session.fetch_all(&sql, &params![limit])? } else { session.fetch_all(&sql, &[])? };
    Ok(Value::Array(rows.iter().map(timer_document).collect::<OpResult<_>>()?))
}

fn timer_active_count(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let row = session.fetch_one(
        "SELECT COUNT(*) AS n FROM storage_timers WHERE user_id=? AND status='active'",
        &params![timer_owner(payload)?],
    )?;
    let count = match row {
        Some(row) => py::column_int_or_zero(row.require("n")?)?,
        None => return crate::error::internal("'NoneType' object is not subscriptable"),
    };
    Ok(Value::from(count))
}

fn timer_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    for key in ["conv_id", "continuation_message"] {
        required_text(payload, key, if key != "conv_id" { 20000 } else { 256 })?;
    }
    let instruction = match payload.get("check_instruction") {
        None => String::new(),
        Some(Value::String(text)) if crate::payload::char_len(text) <= 20000 => text.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid check_instruction"),
    };
    if py_strip(&instruction).is_empty() && py_strip(&required_text(payload, "condition_command", 20000)?).is_empty() {
        return fail("database_protocol_error", "Missing readiness condition");
    }
    session.lock_key("timer", &timer_id);
    if session.fetch_one("SELECT 1 FROM storage_timers WHERE id = ?", &params![&timer_id])?.is_some() {
        return fail("database_conflict", "Timer already exists");
    }
    let config = match payload.get("tools_config") {
        None => Value::Object(Default::default()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid timer tools_config"),
    };
    let mut fields: Vec<SqlValue> = Vec::with_capacity(TIMER_COLUMNS.len());
    for column in TIMER_COLUMNS {
        fields.push(match *column {
            "poll_count" | "promotion_streak" | "fallback_streak" => SqlValue::Integer(int_or(payload, column, 0)?),
            _ => SqlValue::Null,
        });
    }
    let poll_interval = integer(payload, "poll_interval", Some(60), Some(10), None)?;
    let max_polls = integer(payload, "max_polls", Some(120), Some(0), None)?;
    let tools_config = dump_param(&config)?;
    for (index, column) in TIMER_COLUMNS.iter().enumerate() {
        fields[index] = match *column {
            "poll_count" | "promotion_streak" | "fallback_streak" => fields[index].clone(),
            "id" => SqlValue::Text(timer_id.clone()),
            "user_id" => SqlValue::Integer(user_id),
            "poll_interval" => SqlValue::Integer(poll_interval),
            "max_polls" => SqlValue::Integer(max_polls),
            "tools_config" => tools_config.clone(),
            "status" => SqlValue::Text("active".into()),
            other => bind_get(payload, other, "")?,
        };
    }
    let columns = columns();
    let placeholders = vec!["?"; TIMER_COLUMNS.len()].join(", ");
    session.execute(&format!("INSERT INTO storage_timers ({columns}) VALUES ({placeholders})"), &fields)?;
    let row = session.fetch_one(&format!("SELECT {columns} FROM storage_timers WHERE id = ?"), &params![&timer_id])?;
    let Some(row) = row else { return crate::error::internal("created timer is missing") };
    Ok(json!({"applied": true, "timer": timer_document(&row)?}))
}

fn timer_cancel(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    session.lock_key("timer", &timer_id);
    let now = match payload.get("now") {
        Some(value) if crate::json::truthy(value) => bind(value)?,
        _ => SqlValue::Text(String::new()),
    };
    let changed = session.execute(
        "UPDATE storage_timers SET status='cancelled', cancelled_at=?, updated_at=? \
         WHERE id=? AND user_id=? AND status='active'",
        &params![&now, &now, &timer_id, user_id],
    )?;
    Ok(json!({"changed": changed != 0}))
}

const TIMER_UPDATE_ALLOWED: &[&str] = &[
    "poll_count",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "status",
    "updated_at",
    "triggered_at",
    "execution_task_id",
    "cancelled_at",
    "promotion_streak",
    "fallback_streak",
    "promoted_at",
    "condition_kind",
];

fn timer_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    let values: Vec<(&str, &Value)> =
        TIMER_UPDATE_ALLOWED.iter().filter_map(|key| payload.get(*key).map(|value| (*key, value))).collect();
    if values.is_empty() {
        return fail("database_protocol_error", "Empty timer update");
    }
    session.lock_key("timer", &timer_id);
    let assignments: Vec<String> = values.iter().map(|(key, _)| format!("{key} = ?")).collect();
    let expected_status = match payload.get("expected_status") {
        None | Some(Value::Null) => None,
        Some(_) => Some(required_text(payload, "expected_status", 32)?),
    };
    let mut params: Vec<SqlValue> = values.iter().map(|(_, value)| bind(value)).collect::<OpResult<_>>()?;
    params.push(SqlValue::Text(timer_id));
    params.push(SqlValue::Integer(user_id));
    let mut where_clause = String::from(" WHERE id = ? AND user_id = ?");
    if let Some(status) = expected_status {
        where_clause.push_str(" AND status = ?");
        params.push(SqlValue::Text(status));
    }
    let changed =
        session.execute(&format!("UPDATE storage_timers SET {}{where_clause}", assignments.join(", ")), &params)?;
    Ok(json!({"changed": changed != 0}))
}

fn poll_append(session: &Session<'_>, payload: &Payload, require_active: bool) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    let poll_id = text_or(payload, "poll_id", "", Some(80));
    session.lock_key("timer", &timer_id);
    let timer = session
        .fetch_one("SELECT status FROM storage_timers WHERE id=? AND user_id=?", &params![&timer_id, user_id])?;
    let Some(timer) = timer else { return fail("database_not_found", "Timer not found") };
    session.lock_key("timer_poll_log", "global");
    if !poll_id.is_empty() {
        let existing = session.fetch_one(
            "SELECT id FROM storage_timer_poll_log WHERE timer_id=? AND poll_id=?",
            &params![&timer_id, &poll_id],
        )?;
        if let Some(existing) = existing {
            return Ok(json!({"inserted": false, "id": existing.int("id")?}));
        }
    }
    let status = timer.require("status")?;
    if require_active && status.as_str() != Some("active") {
        return Ok(json!({"inserted": false, "id": null, "status": column_str(status)}));
    }
    let row = session.fetch_one("SELECT COALESCE(MAX(id), 0) AS max_id FROM storage_timer_poll_log", &[])?;
    let Some(row) = row else { return crate::error::internal("max id row is missing") };
    let next_id = row.int("max_id")? + 1;
    session.execute(
        "INSERT INTO storage_timer_poll_log \
         (id, timer_id, poll_time, decision, reason, check_output, tokens_used, model, poll_id, raw_output, tier, predicate_matched, llm_agreed) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            next_id,
            &timer_id,
            text_or(payload, "poll_time", "", None),
            text_or(payload, "decision", "wait", None),
            text_or(payload, "reason", "", Some(500)),
            text_or(payload, "check_output", "", Some(5000)),
            int_or(payload, "tokens_used", 0)?,
            text_or(payload, "model", "", Some(120)),
            &poll_id,
            text_or(payload, "raw_output", "", Some(5000)),
            text_or(payload, "tier", "llm", None),
            int_get(payload, "predicate_matched", -1)?,
            int_get(payload, "llm_agreed", -1)?,
        ],
    )?;
    Ok(json!({"inserted": true, "id": next_id}))
}

fn timer_poll_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    poll_append(session, payload, false)
}

fn timer_poll_commit(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let mut result = poll_append(session, payload, true)?;
    if result["inserted"] != Value::Bool(true) {
        result["advanced"] = Value::Bool(false);
        return Ok(result);
    }
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    let poll_time = required_text(payload, "poll_time", 64)?;
    let changed = session.execute(
        "UPDATE storage_timers SET poll_count=poll_count+1, \
         last_poll_at=?, last_poll_decision=?, last_poll_reason=?, updated_at=? \
         WHERE id=? AND user_id=?",
        &params![
            &poll_time,
            text_or(payload, "decision", "wait", None),
            text_or(payload, "reason", "", Some(500)),
            &poll_time,
            &timer_id,
            user_id,
        ],
    )?;
    if changed == 0 {
        return fail("database_not_found", "Timer not found");
    }
    result["advanced"] = Value::Bool(true);
    Ok(result)
}

fn timer_progress(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    let now = required_text(payload, "poll_time", 64)?;
    session.lock_key("timer", &timer_id);
    let changed = session.execute(
        "UPDATE storage_timers SET poll_count=poll_count+1, \
         last_poll_at=?, last_poll_decision=?, last_poll_reason=?, updated_at=? \
         WHERE id=? AND user_id=? AND status='active'",
        &params![
            &now,
            text_or(payload, "decision", "skipped", None),
            text_or(payload, "reason", "", Some(500)),
            &now,
            &timer_id,
            user_id,
        ],
    )?;
    Ok(json!({"changed": changed != 0}))
}

fn timer_poll_log(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let timer_id = timer_id(payload)?;
    let user_id = timer_owner(payload)?;
    let limit = integer(payload, "limit", Some(30), Some(1), Some(200))?;
    let rows = session.fetch_all(
        "SELECT l.id, l.timer_id, l.poll_time, l.decision, l.reason, \
         l.check_output, l.tokens_used, l.model, l.poll_id, l.raw_output, \
         l.tier, l.predicate_matched, l.llm_agreed \
         FROM storage_timer_poll_log AS l JOIN storage_timers AS t \
         ON t.id=l.timer_id WHERE l.timer_id=? AND t.user_id=? \
         ORDER BY l.poll_time DESC LIMIT ?",
        &params![&timer_id, user_id, limit],
    )?;
    Ok(Value::Array(rows.iter().map(|row| row.to_json()).collect::<OpResult<_>>()?))
}
