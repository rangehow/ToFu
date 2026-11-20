//! `operation_domains/core.py`: system, record, project-recent,
//! task-results, event and rate-limit operations.
//!
//! System operations live here (`operations_pkg/_common.py`); records and
//! task results in `core/records.rs`, events and rate limits in
//! `core/events.rs`, recent projects and relink in `core/project.rs`.

mod codecs;
mod events;
mod invocations;
mod project;
mod py;
mod records;

use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::{register as add, OperationSpec, Registry};
use crate::error::{fail, OpResult};
use crate::json::float_value;
use crate::mount::requires_offline_compaction;
use crate::params;
use crate::payload::{integer, Payload};
use crate::session::Session;

pub fn register(registry: &mut Registry) {
    add(registry, "system.schema_version", OperationSpec::query(schema_version));
    add(registry, "tofusql.query", OperationSpec::query(tofusql_query));
    add(registry, "system.reclaim", OperationSpec::command(false, system_reclaim));
    add(registry, "record.get", OperationSpec::query(records::record_get));
    add(registry, "record.list", OperationSpec::query(records::record_list));
    add(registry, "record.put", OperationSpec::command(true, records::record_put));
    add(registry, "record.delete", OperationSpec::command(true, records::record_delete));
    add(registry, "project.recent.list", OperationSpec::query(project::project_recent_list));
    add(registry, "project.recent.touch", OperationSpec::command(true, project::project_recent_touch));
    add(registry, "project.recent.touch_many", OperationSpec::command(true, project::project_recent_touch_many));
    add(registry, "project.recent.clear", OperationSpec::command(true, project::project_recent_clear));
    add(
        registry,
        "project.relink",
        OperationSpec::command(true, project::project_relink)
            .with_timeout(project::PROJECT_RELINK_STORAGE_DEADLINE_SECONDS),
    );
    add(registry, "task_results.replay_get", OperationSpec::query(records::task_results_replay_get));
    add(registry, "task_results.summary_list", OperationSpec::query(records::task_results_summary_list));
    add(
        registry,
        "task_results.cost_experiment_scan",
        OperationSpec::query(records::task_results_cost_experiment_scan),
    );
    add(registry, "task_results.checkpoint", OperationSpec::command(false, records::task_results_checkpoint));
    add(registry, "task_results.abort", OperationSpec::command(false, records::task_results_abort));
    add(registry, "task_results.abort_requested", OperationSpec::query(records::task_results_abort_requested));
    add(registry, "task_results.recovery_pending", OperationSpec::query(records::task_results_recovery_pending));
    add(registry, "task_results.recover_running", OperationSpec::command(false, records::task_results_recover_running));
    add(registry, "event.append", OperationSpec::command(false, events::event_append));
    add(registry, "event.append_batch", OperationSpec::command(false, events::event_append_batch));
    add(registry, "event.list", OperationSpec::query(events::event_list));
    add(registry, "event.latest", OperationSpec::query(events::event_latest));
    add(registry, "event.bounds", OperationSpec::query(events::event_bounds));
    add(registry, "event.inspector_summary", OperationSpec::query(events::event_inspector_summary));
    add(registry, "event.prune", OperationSpec::command(false, events::event_prune));
    add(registry, "rate_limit.record_and_check", OperationSpec::command(true, events::rate_limit_record_and_check));
}

/// `_schema_version`.
fn schema_version(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    let row =
        session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params!["schema_version"])?;
    let version = match row {
        Some(row) => match row.get("meta_value").as_int() {
            Some(version) => version,
            None => return crate::error::internal("invalid schema_version"),
        },
        None => 0,
    };
    Ok(json!({"version": version}))
}

/// `_tofusql_query`: served only by the Tofu-DB engine.
fn tofusql_query(_session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    fail("database_unavailable", "tofusql.query is served only by the Tofu-DB engine")
}

fn pragma_int(session: &Session<'_>, pragma: &str) -> OpResult<Option<i64>> {
    Ok(session.fetch_one(pragma, &[])?.and_then(|row| row.first().as_int()))
}

/// `_system_reclaim`: one bounded incremental-vacuum slice.
fn system_reclaim(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let max_pages = integer(payload, "max_pages", Some(8192), Some(1), Some(1_048_576))?;
    let min_free_pages = integer(payload, "min_free_pages", Some(1024), Some(0), None)?;
    let budget_ms = integer(payload, "budget_ms", Some(250), Some(10), Some(60_000))?;
    let mode = pragma_int(session, "PRAGMA auto_vacuum")?.unwrap_or(0);
    if mode != 2 {
        return Ok(json!({"reclaimed": 0, "auto_vacuum": mode, "skipped": "auto_vacuum is not INCREMENTAL"}));
    }
    let before = pragma_int(session, "PRAGMA freelist_count")?.unwrap_or(0);
    if before < min_free_pages {
        return Ok(json!({"reclaimed": 0, "freelist": before, "auto_vacuum": mode}));
    }
    let page_count = pragma_int(session, "PRAGMA page_count")?.unwrap_or(0);
    let page_size = pragma_int(session, "PRAGMA page_size")?.unwrap_or(0);
    let ratio = before as f64 / page_count.max(1) as f64;
    if requires_offline_compaction(before, page_count) {
        return Ok(json!({
            "reclaimed": 0,
            "freelist": before,
            "auto_vacuum": mode,
            "offline_required": true,
            "page_count": page_count,
            "page_size": page_size,
            "file_bytes": page_count * page_size,
            "freelist_bytes": before * page_size,
            "freelist_ratio": float_value(python_round(ratio, 6)),
            "reason": "bulk freelist exceeds the bounded online reclaim envelope",
        }));
    }
    let target = before.min(max_pages);
    let deadline = Instant::now() + Duration::from_millis(budget_ms as u64);
    let mut after = before;
    while before - after < target && Instant::now() < deadline {
        session.execute("PRAGMA incremental_vacuum(1)", &[])?;
        let new_after = pragma_int(session, "PRAGMA freelist_count")?.unwrap_or(after);
        if new_after >= after {
            break;
        }
        after = new_after;
    }
    Ok(json!({"reclaimed": (before - after).max(0), "freelist": after, "auto_vacuum": mode}))
}

/// Python `round(value, digits)` (round-half-even on the decimal value).
pub fn python_round(value: f64, digits: i32) -> f64 {
    let text = format!("{:.*}", digits as usize, value);
    text.parse().unwrap_or(value)
}
