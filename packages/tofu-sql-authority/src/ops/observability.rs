//! `operation_domains/observability.py`: optimizer proposal/action and
//! aggregate-log operations (`operations_pkg/_optimizer.py`).

mod generated_unicode_simple_fold;

use rusqlite::functions::FunctionFlags;
use serde_json::{json, Value};

use super::{register as add, OperationSpec, Registry};
use crate::error::{OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "optimizer.proposal.create", OperationSpec::command(true, proposal_create));
    add(registry, "optimizer.proposal.update", OperationSpec::command(true, proposal_update));
    add(registry, "optimizer.proposal.get", OperationSpec::query(proposal_get));
    add(registry, "optimizer.proposal.list", OperationSpec::query(proposal_list));
    add(registry, "optimizer.action.record", OperationSpec::command(true, action_record));
    add(registry, "optimizer.action.outcome", OperationSpec::command(true, action_outcome));
    add(registry, "optimizer.action.revert", OperationSpec::command(true, action_revert));
    add(registry, "optimizer.action.list", OperationSpec::query(action_list));
    add(registry, "optimizer.action.expired", OperationSpec::query(action_expired));
    add(registry, "optimizer.action.for_proposal", OperationSpec::query(action_for_proposal));
    add(registry, "log_aggregate.flush", OperationSpec::command(false, log_aggregate_flush));
    add(registry, "log_aggregate.query", OperationSpec::query(log_aggregate_query));
}

// private port of lib/storage_authority_sql/operations_pkg/_runs.py::_optional_text; shared owner: workflows
fn optional_text(payload: &Payload, field: &str, default: &str, maximum: usize, scope: &str) -> OpResult<String> {
    match payload.get(field) {
        None => {
            if char_len(default) > maximum {
                return Err(StorageError::protocol(format!("Invalid {scope} {field}")).into());
            }
            Ok(default.to_owned())
        }
        Some(Value::String(value)) if char_len(value) <= maximum => Ok(value.clone()),
        Some(_) => Err(StorageError::protocol(format!("Invalid {scope} {field}")).into()),
    }
}

const OPT_PROPOSAL_COLUMNS: &str = "user_id, id, created_at, title, rationale, action_type, action_args, severity, \
confidence, evidence, status, status_reason";

const OPT_ACTION_COLUMNS: &str = "user_id, id, proposal_id, applied_at, expires_at, pre_metric, outcome_metric, \
outcome_recorded_at, reverted_at, revert_reason";

fn prefixed_action_columns() -> String {
    format!("a.{}", OPT_ACTION_COLUMNS.replace(", ", ", a."))
}

fn user_id(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn rows_json(rows: &[Row]) -> OpResult<Value> {
    Ok(Value::Array(rows.iter().map(Row::to_json).collect::<OpResult<Vec<_>>>()?))
}

fn row_json(row: Option<Row>) -> OpResult<Value> {
    match row {
        Some(row) => row.to_json(),
        None => Ok(Value::Null),
    }
}

fn proposal_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let proposal_id = required_text(payload, "proposal_id", 128)?;
    let values: Vec<SqlValue> = vec![
        user_id.into(),
        (&proposal_id).into(),
        required_text(payload, "created_at", 64)?.into(),
        optional_text(payload, "title", "", 500, "optimizer")?.into(),
        optional_text(payload, "rationale", "", 4000, "optimizer")?.into(),
        required_text(payload, "action_type", 256)?.into(),
        required_text(payload, "action_args", 2_000_000)?.into(),
        optional_text(payload, "severity", "low", 64, "optimizer")?.into(),
        number(payload, "confidence", 0.0, 1.0)?.into(),
        required_text(payload, "evidence", 2_000_000)?.into(),
        optional_text(payload, "status", "pending_review", 64, "optimizer")?.into(),
        optional_text(payload, "status_reason", "", 500, "optimizer")?.into(),
    ];
    session.execute(
        &format!("INSERT INTO optimizer_proposals({OPT_PROPOSAL_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
        &values,
    )?;
    Ok(json!({"proposal_id": proposal_id}))
}

fn proposal_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let status = required_text(payload, "status", 64)?;
    let reason = optional_text(payload, "reason", "", 500, "optimizer")?;
    let proposal_id = required_text(payload, "proposal_id", 128)?;
    let count = session.execute(
        "UPDATE optimizer_proposals SET status = ?, status_reason = ? WHERE user_id = ? AND id = ?",
        &params![status, reason, user_id, proposal_id],
    )?;
    Ok(json!({"changed": count != 0}))
}

fn proposal_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let proposal_id = required_text(payload, "proposal_id", 128)?;
    row_json(session.fetch_one(
        &format!("SELECT {OPT_PROPOSAL_COLUMNS} FROM optimizer_proposals WHERE user_id = ? AND id = ?"),
        &params![user_id, proposal_id],
    )?)
}

fn proposal_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let status = optional_text(payload, "status", "", 64, "optimizer")?;
    let limit = integer(payload, "limit", Some(50), Some(1), Some(500))?;
    rows_json(&session.fetch_all(
        &format!(
            "SELECT {OPT_PROPOSAL_COLUMNS} FROM optimizer_proposals WHERE user_id = ? AND (? = ? OR status = ?) \
             ORDER BY created_at DESC LIMIT ?"
        ),
        &params![user_id, &status, "", &status, limit],
    )?)
}

fn action_record(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let log_id = required_text(payload, "log_id", 128)?;
    let proposal_id = required_text(payload, "proposal_id", 128)?;
    if session
        .fetch_one("SELECT id FROM optimizer_proposals WHERE user_id = ? AND id = ?", &params![user_id, &proposal_id])?
        .is_none()
    {
        return Err(StorageError::new("database_integrity", "Optimizer proposal does not exist").into());
    }
    let applied_at = required_text(payload, "applied_at", 64)?;
    let expires_at = required_text(payload, "expires_at", 64)?;
    let pre_metric = required_text(payload, "pre_metric", 2_000_000)?;
    session.execute(
        "INSERT INTO optimizer_action_log(user_id, id, proposal_id, applied_at, expires_at, pre_metric) \
         VALUES (?, ?, ?, ?, ?, ?)",
        &params![user_id, &log_id, proposal_id, applied_at, expires_at, pre_metric],
    )?;
    Ok(json!({"log_id": log_id}))
}

fn action_outcome(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let outcome_metric = required_text(payload, "outcome_metric", 2_000_000)?;
    let recorded_at = required_text(payload, "recorded_at", 64)?;
    let log_id = required_text(payload, "log_id", 128)?;
    let count = session.execute(
        "UPDATE optimizer_action_log SET outcome_metric = ?, outcome_recorded_at = ? WHERE user_id = ? AND id = ?",
        &params![outcome_metric, recorded_at, user_id, log_id],
    )?;
    Ok(json!({"changed": count != 0}))
}

fn action_revert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let reverted_at = required_text(payload, "reverted_at", 64)?;
    let reason = optional_text(payload, "reason", "", 500, "optimizer")?;
    let log_id = required_text(payload, "log_id", 128)?;
    let count = session.execute(
        "UPDATE optimizer_action_log SET reverted_at = ?, revert_reason = ? WHERE user_id = ? AND id = ?",
        &params![reverted_at, reason, user_id, log_id],
    )?;
    Ok(json!({"changed": count != 0}))
}

fn action_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let include_reverted = match payload.get("include_reverted") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return Err(StorageError::protocol("Invalid include_reverted in storage request").into()),
    };
    let limit = integer(payload, "limit", Some(50), Some(1), Some(500))?;
    rows_json(&session.fetch_all(
        &format!(
            "SELECT {}, p.title AS p_title, p.action_type AS p_action_type, \
             p.action_args AS p_action_args, p.status AS p_status \
             FROM optimizer_action_log a JOIN optimizer_proposals p \
             ON p.user_id = a.user_id AND p.id = a.proposal_id \
             WHERE a.user_id = ? AND (? = 1 OR a.reverted_at = ?) \
             ORDER BY a.applied_at DESC LIMIT ?",
            prefixed_action_columns()
        ),
        &params![user_id, include_reverted as i64, "", limit],
    )?)
}

fn action_expired(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let now_iso = required_text(payload, "now_iso", 64)?;
    rows_json(&session.fetch_all(
        &format!(
            "SELECT {}, p.action_type AS p_action_type, p.action_args AS p_action_args, \
             p.status AS p_status FROM optimizer_action_log a \
             JOIN optimizer_proposals p \
             ON p.user_id = a.user_id AND p.id = a.proposal_id \
             WHERE a.user_id = ? AND a.reverted_at = ? AND p.status = ? \
             AND a.expires_at != ? AND a.expires_at <= ?",
            prefixed_action_columns()
        ),
        &params![user_id, "", "applied", "", now_iso],
    )?)
}

fn action_for_proposal(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = user_id(payload)?;
    let proposal_id = required_text(payload, "proposal_id", 128)?;
    row_json(session.fetch_one(
        &format!(
            "SELECT {OPT_ACTION_COLUMNS} FROM optimizer_action_log WHERE user_id = ? AND proposal_id = ? \
             ORDER BY applied_at DESC LIMIT 1"
        ),
        &params![user_id, proposal_id],
    )?)
}

fn log_aggregate_flush(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let rows = match payload.get("rows") {
        Some(Value::Array(rows)) if rows.len() <= 500 => rows,
        _ => return Err(StorageError::protocol("Invalid log aggregate batch").into()),
    };
    for item in rows {
        let Value::Object(item) = item else {
            return Err(StorageError::protocol("Invalid log aggregate row").into());
        };
        let values: Vec<SqlValue> = vec![
            required_text(item, "fingerprint", 64)?.into(),
            required_text(item, "level", 32)?.into(),
            optional_text(item, "logger", "", 256, "log aggregate")?.into(),
            optional_text(item, "template", "", 200, "log aggregate")?.into(),
            optional_text(item, "sample", "", 2000, "log aggregate")?.into(),
            integer(item, "count", None, Some(1), Some(1_000_000_000))?.into(),
            integer(item, "first_seen", None, Some(0), None)?.into(),
            integer(item, "last_seen", None, Some(0), None)?.into(),
        ];
        session.execute(
            "INSERT INTO log_aggregates(fingerprint, level, logger, template, sample, count, \
             first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?) \
             ON CONFLICT(fingerprint) DO UPDATE SET \
             count = log_aggregates.count + excluded.count, \
             last_seen = excluded.last_seen, sample = excluded.sample",
            &values,
        )?;
    }
    let mut swept = 0usize;
    match payload.get("cutoff_ms") {
        None | Some(Value::Null) => {}
        Some(cutoff) => {
            let Some(cutoff) = cutoff.as_i64().filter(|value| *value >= 0 && cutoff.is_number()) else {
                return Err(StorageError::protocol("Invalid log aggregate cutoff").into());
            };
            swept = session.execute(
                "DELETE FROM log_aggregates WHERE fingerprint IN (\
                 SELECT fingerprint FROM log_aggregates \
                 WHERE last_seen < ? ORDER BY last_seen LIMIT 500)",
                &params![cutoff],
            )?;
        }
    }
    Ok(json!({"flushed": rows.len(), "swept": swept}))
}

/// `simple_case_fold` (`generated_unicode_simple_fold.py`).
fn simple_case_fold(text: &str) -> String {
    text.chars()
        .map(|c| char::from_u32(generated_unicode_simple_fold::simple_case_fold(c as u32)).unwrap_or(c))
        .collect()
}

/// `adapters/search_support.py::simple_fold_column` for SQLite.
fn simple_fold_column(session: &Session<'_>, column: &str) -> OpResult<String> {
    session.connection().create_scalar_function(
        "tofu_simple_fold",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |context| {
            let value = SqlValue::from(context.get_raw(0));
            Ok(match value {
                SqlValue::Null => None,
                SqlValue::Text(text) => Some(simple_case_fold(&text)),
                SqlValue::Integer(integer) => Some(integer.to_string()),
                SqlValue::Real(real) => Some(simple_case_fold(&crate::json::python_float_repr(real))),
                SqlValue::Blob(bytes) => Some(simple_case_fold(&format!("b{:?}", String::from_utf8_lossy(&bytes)))),
            })
        },
    )?;
    Ok(format!("tofu_simple_fold({column})"))
}

fn log_aggregate_query(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let level = optional_text(payload, "level", "", 32, "log aggregate")?;
    let sort = optional_text(payload, "sort", "count", 32, "log aggregate")?;
    let order = match sort.as_str() {
        "count" => "count DESC, last_seen DESC",
        "last_seen" => "last_seen DESC",
        "level" => "level ASC, count DESC",
        _ => return Err(StorageError::protocol("Invalid log aggregate sort").into()),
    };
    let limit = integer(payload, "limit", Some(100), Some(1), Some(500))?;
    let q = optional_text(payload, "q", "", 200, "log aggregate")?;
    let template = simple_fold_column(session, "template")?;
    let escaped = simple_case_fold(&q).replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_");
    let pattern = format!("%{escaped}%");
    let where_clause = format!("WHERE (? = ? OR level = ?) AND (? = ? OR {template} LIKE ? ESCAPE '\\') ");
    let filter = params![&level, "", &level, &q, "", &pattern];
    let mut item_params = filter.to_vec();
    item_params.push(limit.into());
    let rows = session.fetch_all(
        &format!(
            "SELECT fingerprint, level, logger, template, sample, count, first_seen, last_seen FROM log_aggregates \
             {where_clause}ORDER BY {order} LIMIT ?"
        ),
        &item_params,
    )?;
    let totals = session.fetch_one(
        &format!("SELECT COUNT(*) AS n, COALESCE(SUM(count), 0) AS events FROM log_aggregates {where_clause}"),
        &filter,
    )?;
    let (total_rows, total_events) = match &totals {
        Some(row) => (row.int("n")?, row.int("events")?),
        None => (0, 0),
    };
    Ok(json!({"items": rows_json(&rows)?, "total_rows": total_rows, "total_events": total_events}))
}
