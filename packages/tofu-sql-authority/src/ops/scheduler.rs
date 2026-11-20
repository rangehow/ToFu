//! `operation_domains/scheduler.py`: scheduled task and proactive poll
//! operations (`operations_pkg/_timers.py`, scheduler half).

mod isoformat;

use serde_json::{json, Map, Value};

use super::timers::py::{self, bind, column_text, document, int_get, int_or, text_or};
use super::{register as add, OperationSpec, Registry};
use crate::codec::{dump_param, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "scheduler.task.create", OperationSpec::command(true, scheduler_create));
    add(registry, "scheduler.task.ensure", OperationSpec::command(true, scheduler_ensure));
    add(registry, "scheduler.task.get", OperationSpec::query(scheduler_get));
    add(registry, "scheduler.task.list", OperationSpec::query(scheduler_list));
    add(registry, "scheduler.task.list_all", OperationSpec::query(scheduler_list_all));
    add(registry, "scheduler.task.execution_lookup", OperationSpec::query(scheduler_execution_lookup));
    add(registry, "scheduler.task.settle_execution", OperationSpec::command(true, scheduler_settle_execution));
    add(registry, "scheduler.task.update", OperationSpec::command(true, scheduler_update));
    add(registry, "scheduler.task.delete", OperationSpec::command(true, scheduler_delete));
    add(registry, "scheduler.task.record_result", OperationSpec::command(true, scheduler_record_result));
    add(registry, "scheduler.task.claim_due", OperationSpec::command(true, scheduler_claim_due));
    add(registry, "scheduler.poll.append", OperationSpec::command(false, scheduler_poll_append));
    add(registry, "scheduler.poll.log", OperationSpec::query(scheduler_poll_log));
}

/// `lib/scheduler/contract.py::DUE_CLAIM_INTERVAL_SECONDS`.
const DUE_CLAIM_INTERVAL_SECONDS: i64 = 55;

const SCHEDULER_COLUMNS: &[&str] = &[
    "id",
    "user_id",
    "system_key",
    "name",
    "schedule",
    "task_type",
    "command",
    "description",
    "enabled",
    "notify_on_failure",
    "notify_on_success",
    "max_runtime",
    "last_run",
    "last_result",
    "last_status",
    "run_count",
    "fail_count",
    "created_at",
    "updated_at",
    "target_conv_id",
    "source_conv_id",
    "tools_config",
    "poll_count",
    "last_poll_at",
    "last_poll_decision",
    "last_poll_reason",
    "last_execution_at",
    "last_execution_task_id",
    "last_execution_status",
    "execution_count",
    "max_executions",
    "expires_at",
    "condition_kind",
    "condition_command",
    "condition_regex",
    "promotion_streak",
    "fallback_streak",
    "promoted_at",
];

const SCHEDULER_NUMERIC: &[&str] = &[
    "enabled",
    "notify_on_failure",
    "notify_on_success",
    "max_runtime",
    "run_count",
    "fail_count",
    "poll_count",
    "execution_count",
    "max_executions",
    "promotion_streak",
    "fallback_streak",
    "user_id",
];

fn columns() -> String {
    SCHEDULER_COLUMNS.join(", ")
}

fn task_id(payload: &Payload) -> OpResult<String> {
    if payload.contains_key("task_id") {
        return required_text(payload, "task_id", 128);
    }
    required_text(payload, "id", 128)
}

fn owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn scheduler_document(row: &Row) -> OpResult<Value> {
    document(row, SCHEDULER_COLUMNS, SCHEDULER_NUMERIC)
}

fn documents(rows: &[Row]) -> OpResult<Value> {
    Ok(Value::Array(rows.iter().map(scheduler_document).collect::<OpResult<_>>()?))
}

/// `bool(payload.get(key, default))`.
fn flag(payload: &Payload, key: &str, default: bool) -> bool {
    payload.get(key).map(truthy).unwrap_or(default)
}

fn tools_config(payload: &Payload) -> OpResult<Value> {
    match payload.get("tools_config") {
        None => Ok(Value::Object(Map::new())),
        Some(value @ Value::Object(_)) => Ok(value.clone()),
        Some(_) => fail("database_protocol_error", "Invalid scheduler tools_config"),
    }
}

fn enabled_only(payload: &Payload) -> OpResult<bool> {
    match payload.get("enabled_only") {
        None => Ok(false),
        Some(Value::Bool(flag)) => Ok(*flag),
        Some(_) => fail("database_protocol_error", "Invalid enabled_only"),
    }
}

fn scheduler_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let sql = format!("SELECT {} FROM storage_scheduled_tasks WHERE id = ? AND user_id = ?", columns());
    match session.fetch_one(&sql, &params![task_id(payload)?, owner(payload)?])? {
        None => Ok(Value::Null),
        Some(row) => scheduler_document(&row),
    }
}

fn scheduler_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(1000), Some(0), None)?;
    let enabled_only = enabled_only(payload)?;
    let user_id = owner(payload)?;
    let sql = format!(
        "SELECT {} FROM storage_scheduled_tasks WHERE user_id=?{} ORDER BY created_at DESC{}",
        columns(),
        if enabled_only { " AND enabled = 1" } else { "" },
        if limit != 0 { " LIMIT ?" } else { "" }
    );
    let rows = if limit != 0 {
        session.fetch_all(&sql, &params![user_id, limit])?
    } else {
        session.fetch_all(&sql, &params![user_id])?
    };
    documents(&rows)
}

fn scheduler_list_all(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(1000), Some(0), None)?;
    let enabled_only = enabled_only(payload)?;
    let sql = format!(
        "SELECT {} FROM storage_scheduled_tasks{} ORDER BY created_at DESC{}",
        columns(),
        if enabled_only { " WHERE enabled=1" } else { "" },
        if limit != 0 { " LIMIT ?" } else { "" }
    );
    let rows = if limit != 0 { session.fetch_all(&sql, &params![limit])? } else { session.fetch_all(&sql, &[])? };
    documents(&rows)
}

fn create(session: &Session<'_>, payload: &Payload, system_key: &str) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    owner(payload)?;
    for key in ["name", "schedule", "command"] {
        required_text(payload, key, if key == "command" { 20000 } else { 512 })?;
    }
    let config = tools_config(payload)?;
    session.lock_key("scheduler_task", &task_id);
    if session.fetch_one("SELECT 1 FROM storage_scheduled_tasks WHERE id=?", &params![&task_id])?.is_some() {
        return fail("database_conflict", "Scheduled task already exists");
    }
    let tools_config = dump_param(&config)?;
    let mut numeric: Vec<(&str, i64)> = Vec::new();
    for key in SCHEDULER_NUMERIC {
        if !matches!(*key, "enabled" | "notify_on_failure" | "notify_on_success") {
            numeric.push((key, int_or(payload, key, 0)?));
        }
    }
    let max_runtime = int_or(payload, "max_runtime", 300)?;
    let mut values = Vec::with_capacity(SCHEDULER_COLUMNS.len());
    for column in SCHEDULER_COLUMNS {
        values.push(match *column {
            "id" => SqlValue::Text(task_id.clone()),
            "system_key" => SqlValue::Text(system_key.to_owned()),
            "tools_config" => tools_config.clone(),
            "enabled" => SqlValue::Integer(flag(payload, "enabled", true) as i64),
            "notify_on_failure" => SqlValue::Integer(flag(payload, "notify_on_failure", true) as i64),
            "notify_on_success" => SqlValue::Integer(flag(payload, "notify_on_success", false) as i64),
            "max_runtime" => SqlValue::Integer(max_runtime),
            other => match numeric.iter().find(|(key, _)| *key == other) {
                Some((_, value)) => SqlValue::Integer(*value),
                None => match payload.get(other) {
                    Some(value) => bind(value)?,
                    None => SqlValue::Text(String::new()),
                },
            },
        });
    }
    let columns = columns();
    let placeholders = vec!["?"; SCHEDULER_COLUMNS.len()].join(", ");
    session.execute(&format!("INSERT INTO storage_scheduled_tasks ({columns}) VALUES ({placeholders})"), &values)?;
    let row =
        session.fetch_one(&format!("SELECT {columns} FROM storage_scheduled_tasks WHERE id=?"), &params![&task_id])?;
    let Some(row) = row else { return internal("'NoneType' object is not subscriptable") };
    Ok(json!({"applied": true, "task": scheduler_document(&row)?}))
}

fn scheduler_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    create(session, payload, "")
}

fn scheduler_ensure(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner(payload)?;
    let system_key = required_text(payload, "system_key", 128)?;
    let name = required_text(payload, "name", 512)?;
    let schedule = required_text(payload, "schedule", 512)?;
    let command = required_text(payload, "command", 20000)?;
    let task_type = required_text(payload, "task_type", 128)?;
    let config = tools_config(payload)?;
    let mut definition: Vec<(&str, Value)> = vec![
        ("name", Value::String(name.clone())),
        ("schedule", Value::String(schedule)),
        ("task_type", Value::String(task_type.clone())),
        ("command", Value::String(command)),
        ("description", Value::String(text_or(payload, "description", "", Some(20000)))),
        ("notify_on_failure", Value::from(flag(payload, "notify_on_failure", true) as i64)),
        ("notify_on_success", Value::from(flag(payload, "notify_on_success", false) as i64)),
        ("max_runtime", Value::from(integer(payload, "max_runtime", Some(300), Some(1), Some(86400))?)),
        ("tools_config", config),
        ("condition_kind", Value::String(text_or(payload, "condition_kind", "llm", Some(64)))),
        ("condition_command", Value::String(text_or(payload, "condition_command", "", Some(20000)))),
        ("condition_regex", Value::String(text_or(payload, "condition_regex", "", Some(20000)))),
    ];
    let reconcile_enabled = match payload.get("reconcile_enabled") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid reconcile_enabled"),
    };
    if reconcile_enabled {
        definition.push(("enabled", Value::from(flag(payload, "enabled", true) as i64)));
    }
    session.lock_key("scheduler_system_task", &format!("{user_id}:{system_key}"));
    let columns = columns();
    let mut row = session.fetch_one(
        &format!("SELECT {columns} FROM storage_scheduled_tasks WHERE user_id=? AND system_key=?"),
        &params![user_id, &system_key],
    )?;
    let legacy_rows = session.fetch_all(
        &format!(
            "SELECT {columns} FROM storage_scheduled_tasks \
             WHERE user_id=? AND system_key='' AND name=? AND task_type=? \
             ORDER BY created_at, id"
        ),
        &params![user_id, &name, &task_type],
    )?;
    let updated_at = text_or(payload, "updated_at", "", None);
    if let Some(keeper) = legacy_rows.first() {
        let mut duplicates: Vec<&Row> = legacy_rows[1..].iter().collect();
        if let Some(current) = row.as_ref() {
            duplicates.push(current);
        }
        for duplicate in duplicates {
            let duplicate_id = py::column_str(duplicate.require("id")?);
            session.execute("DELETE FROM storage_proactive_poll_log WHERE task_id=?", &params![&duplicate_id])?;
            session.execute(
                "DELETE FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
                &params![&duplicate_id, user_id],
            )?;
        }
        let keeper_id = py::column_str(keeper.require("id")?);
        session.execute(
            "UPDATE storage_scheduled_tasks SET system_key=?, updated_at=? \
             WHERE id=? AND user_id=? AND system_key=''",
            &params![&system_key, &updated_at, &keeper_id, user_id],
        )?;
        row = session.fetch_one(
            &format!("SELECT {columns} FROM storage_scheduled_tasks WHERE id=? AND user_id=?"),
            &params![&keeper_id, user_id],
        )?;
    }
    let Some(found) = row else {
        let created = create(session, payload, &system_key)?;
        return Ok(json!({"created": true, "task": created["task"]}));
    };
    let current = scheduler_document(&found)?;
    let changed: Vec<(&str, Value)> =
        definition.into_iter().filter(|(key, value)| !py_eq(&current[*key], value)).collect();
    let mut result_row = found;
    if !changed.is_empty() {
        let mut assignments = Vec::new();
        let mut parameters = Vec::new();
        for (key, value) in &changed {
            assignments.push(format!("{key}=?"));
            parameters.push(if *key == "tools_config" { dump_param(value)? } else { bind(value)? });
        }
        assignments.push("updated_at=?".to_owned());
        parameters.push(SqlValue::Text(updated_at));
        let current_id = crate::session::json_param(&current["id"])?;
        parameters.push(current_id.clone());
        parameters.push(SqlValue::Integer(user_id));
        parameters.push(SqlValue::Text(system_key));
        session.execute(
            &format!(
                "UPDATE storage_scheduled_tasks SET {} WHERE id=? AND user_id=? AND system_key=?",
                assignments.join(", ")
            ),
            &parameters,
        )?;
        let refreshed = session.fetch_one(
            &format!("SELECT {columns} FROM storage_scheduled_tasks WHERE id=? AND user_id=?"),
            &[current_id, SqlValue::Integer(user_id)],
        )?;
        let Some(refreshed) = refreshed else { return internal("'NoneType' object is not subscriptable") };
        result_row = refreshed;
    }
    Ok(json!({
        "created": false,
        "updated": !changed.is_empty(),
        "task": scheduler_document(&result_row)?,
    }))
}

fn scheduler_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    let mut updates: Vec<(&str, SqlValue)> = Vec::new();
    let mut raw: Vec<(&str, &Value)> = Vec::new();
    for column in SCHEDULER_COLUMNS {
        if matches!(*column, "id" | "user_id" | "system_key" | "created_at" | "tools_config") {
            continue;
        }
        if let Some(value) = payload.get(*column) {
            raw.push((column, value));
        }
    }
    let mut tools: Option<SqlValue> = None;
    if let Some(config) = payload.get("tools_config") {
        if !config.is_object() {
            return fail("database_protocol_error", "Invalid scheduler tools_config");
        }
        tools = Some(dump_param(config)?);
    }
    if raw.is_empty() && tools.is_none() {
        return fail("database_protocol_error", "Empty scheduler update");
    }
    session.lock_key("scheduler_task", &task_id);
    let has_updated_at = raw.iter().any(|(key, _)| *key == "updated_at");
    let mut integers = Vec::with_capacity(raw.len());
    for (key, value) in &raw {
        integers.push(if SCHEDULER_NUMERIC.contains(key) { Some(py::py_int(value)?) } else { None });
    }
    for ((key, value), integer) in raw.into_iter().zip(integers) {
        updates.push((
            key,
            match integer {
                Some(integer) => SqlValue::Integer(integer),
                None => bind(value)?,
            },
        ));
    }
    if let Some(tools) = tools {
        updates.push(("tools_config", tools));
    }
    if !has_updated_at {
        updates.push(("updated_at", SqlValue::Text(text_or(payload, "now", "", None))));
    }
    let assignments: Vec<String> = updates.iter().map(|(key, _)| format!("{key}=?")).collect();
    let mut parameters: Vec<SqlValue> = updates.into_iter().map(|(_, value)| value).collect();
    parameters.push(SqlValue::Text(task_id));
    parameters.push(SqlValue::Integer(user_id));
    let changed = session.execute(
        &format!("UPDATE storage_scheduled_tasks SET {} WHERE id=? AND user_id=?", assignments.join(", ")),
        &parameters,
    )?;
    Ok(json!({"changed": changed != 0}))
}

fn scheduler_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    session.lock_key("scheduler_task", &task_id);
    let present = session.fetch_one(
        "SELECT 1 AS present FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
        &params![&task_id, user_id],
    )?;
    if present.is_none() {
        return Ok(json!({"deleted": false}));
    }
    session.execute("DELETE FROM storage_proactive_poll_log WHERE task_id=?", &params![&task_id])?;
    session.execute("DELETE FROM storage_scheduled_tasks WHERE id=? AND user_id=?", &params![&task_id, user_id])?;
    Ok(json!({"deleted": true}))
}

fn scheduler_record_result(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    session.lock_key("scheduler_task", &task_id);
    let now = required_text(payload, "now", 64)?;
    let success = flag(payload, "success", false);
    let changed = session.execute(
        "UPDATE storage_scheduled_tasks SET last_run=?, last_result=?, last_status=?, \
         run_count=run_count+1, fail_count=fail_count+?, updated_at=? \
         WHERE id=? AND user_id=?",
        &params![
            &now,
            text_or(payload, "result", "", Some(10000)),
            if success { "ok" } else { "failed" },
            if success { 0 } else { 1 },
            &now,
            &task_id,
            user_id,
        ],
    )?;
    Ok(json!({"changed": changed != 0}))
}

fn scheduler_claim_due(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    let lane = required_text(payload, "lane", 16)?;
    if lane != "run" && lane != "poll" {
        return fail("database_protocol_error", "Invalid scheduler claim lane");
    }
    let now_text = required_text(payload, "now", 64)?;
    let Some(now_value) = isoformat::fromisoformat(&now_text) else {
        return fail("database_protocol_error", "Invalid scheduler claim timestamp");
    };
    let minimum_interval =
        integer(payload, "minimum_interval_seconds", Some(DUE_CLAIM_INTERVAL_SECONDS), Some(1), Some(3600))?;
    let last_column = if lane == "poll" { "last_poll_at" } else { "last_run" };
    session.lock_key("scheduler_task", &task_id);
    let row = session.fetch_one(
        &format!("SELECT enabled, {last_column} AS last_claim FROM storage_scheduled_tasks WHERE id=? AND user_id=?"),
        &params![&task_id, user_id],
    )?;
    let Some(row) = row.filter(|row| row.get("enabled").truthy()) else {
        return Ok(json!({"claimed": false}));
    };
    let last_claim = column_text(row.require("last_claim")?);
    if !last_claim.is_empty() {
        let Some(last_value) = isoformat::fromisoformat(&last_claim) else {
            return fail("database_integrity", "Stored scheduler claim timestamp is invalid");
        };
        let Some(elapsed) = isoformat::difference_micros(&now_value, &last_value) else {
            return internal("can't subtract offset-naive and offset-aware datetimes");
        };
        if elapsed < i128::from(minimum_interval) * 1_000_000 {
            return Ok(json!({"claimed": false}));
        }
    }
    let changed = session.execute(
        &format!(
            "UPDATE storage_scheduled_tasks SET {last_column}=?, updated_at=? WHERE id=? AND user_id=? AND enabled=1"
        ),
        &params![&now_text, &now_text, &task_id, user_id],
    )?;
    Ok(json!({"claimed": changed != 0}))
}

fn scheduler_poll_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    if session
        .fetch_one(
            "SELECT 1 AS present FROM storage_scheduled_tasks WHERE id=? AND user_id=?",
            &params![&task_id, user_id],
        )?
        .is_none()
    {
        return fail("database_not_found", "Scheduled task not found");
    }
    session.lock_key("scheduler_poll_log", "global");
    let row = session.fetch_one("SELECT COALESCE(MAX(id),0) AS max_id FROM storage_proactive_poll_log", &[])?;
    let Some(row) = row else { return internal("'NoneType' object is not subscriptable") };
    let ident = row.int("max_id")? + 1;
    session.execute(
        "INSERT INTO storage_proactive_poll_log \
         (id, task_id, poll_time, decision, reason, status_snapshot, model, tokens_used, execution_task_id, tier, predicate_matched, llm_agreed) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            ident,
            &task_id,
            text_or(payload, "poll_time", "", None),
            text_or(payload, "decision", "skip", None),
            text_or(payload, "reason", "", Some(500)),
            text_or(payload, "status_snapshot", "", Some(5000)),
            text_or(payload, "model", "", Some(120)),
            int_or(payload, "tokens_used", 0)?,
            text_or(payload, "execution_task_id", "", None),
            text_or(payload, "tier", "llm", None),
            int_get(payload, "predicate_matched", -1)?,
            int_get(payload, "llm_agreed", -1)?,
        ],
    )?;
    Ok(json!({"inserted": true, "id": ident}))
}

fn scheduler_poll_log(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = task_id(payload)?;
    let user_id = owner(payload)?;
    let limit = integer(payload, "limit", Some(30), Some(1), Some(200))?;
    let rows = session.fetch_all(
        "SELECT l.id, l.task_id, l.poll_time, l.decision, l.reason, \
         l.status_snapshot, l.model, l.tokens_used, l.execution_task_id, \
         l.tier, l.predicate_matched, l.llm_agreed \
         FROM storage_proactive_poll_log AS l \
         JOIN storage_scheduled_tasks AS t ON t.id=l.task_id \
         WHERE l.task_id=? AND t.user_id=? ORDER BY l.poll_time DESC LIMIT ?",
        &params![&task_id, user_id, limit],
    )?;
    Ok(Value::Array(rows.iter().map(|row| row.to_json()).collect::<OpResult<_>>()?))
}

fn scheduler_settle_execution(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let execution_id = required_text(payload, "execution_task_id", 128)?;
    let status = required_text(payload, "status", 16)?;
    let now = required_text(payload, "now", 64)?;
    if status != "ok" && status != "error" {
        return fail("database_protocol_error", "Invalid execution settlement status");
    }
    let changed = session.execute(
        "UPDATE storage_scheduled_tasks SET last_execution_status=?, updated_at=? \
         WHERE user_id=? AND task_type='agent' AND last_execution_task_id=?",
        &params![&status, &now, owner, &execution_id],
    )?;
    Ok(json!({"changed": changed}))
}

fn scheduler_execution_lookup(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let execution_id = required_text(payload, "execution_task_id", 128)?;
    let present = session.fetch_one(
        "SELECT 1 AS present FROM storage_scheduled_tasks \
         WHERE user_id=? AND task_type='agent' AND last_execution_task_id=? LIMIT 1",
        &params![owner, &execution_id],
    )?;
    Ok(json!({"indexed": true, "matched": present.is_some()}))
}
