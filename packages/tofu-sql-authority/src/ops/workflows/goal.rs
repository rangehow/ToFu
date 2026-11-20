//! Chat Goal Mode runs over the orchestration run store
//! (`operations_pkg/_runs.py`, `lib/goal_runs/contract.py`).

use serde_json::{json, Map, Value};

use super::{
    get_or, is_terminal_column, json_text, now_ms, object_or_empty, optional_text, or_empty, protocol, py_strip,
    run_owner, sql_str, terminal_params, TERMINAL_STATUS_SQL,
};
use crate::codec::{self, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{required_text, Payload};
use crate::session::Session;

const GOAL_RUN_FORMAT: &str = "tofu.goal-run/v1";
const GOAL_RUN_CREATED_BY: &str = "chat_goal_mode";
const GOAL_RUN_ORCHESTRATION_PREFIX: &str = "chat-goal:";
const MAX_GOAL_OBJECTIVE_CHARS: usize = 48_000;
/// `DEFAULT_GOAL_MAX_ITERATIONS`.
const DEFAULT_GOAL_MAX_ITERATIONS: i64 = 40;
/// `lib/orchestration/loop_policy.py::MAX_EXECUTOR_MAX_ITERATIONS`.
const MAX_GOAL_MAX_ITERATIONS: i64 = 64;
/// `_GOAL_STOP_REPORT_MAX_CHARS`.
const GOAL_STOP_REPORT_MAX_CHARS: usize = 200_000;
const GOAL_RUN_TERMINAL_STATUSES: [&str; 4] = ["completed", "blocked", "failed", "cancelled"];

const GOAL_POLICY_DIRECTIVE: &str = concat!(
    "Pursue the stated objective for durable long-term benefit. Diagnose and ",
    "fix root causes, require concrete verification evidence, and do not ",
    "substitute a temporary patch when a robust maintainable solution is ",
    "within the delegated scope. Never shrink, narrow, or redefine the ",
    "objective to fit what is easy to finish now: work is aligned only when ",
    "it makes the full requested end state more true, and a smaller, safer, ",
    "or easier-to-verify substitute that preserves a different end state is ",
    "misaligned. Completion means the complete original scope is true and ",
    "verified, not the largest subset that fits the moment."
);

/// `goal_run_policy()`.
fn goal_run_policy() -> Value {
    json!({
        "solutionHorizon": "long_term",
        "rootCauseRequired": true,
        "verificationEvidenceRequired": true,
        "temporaryPatchPolicy": "reject_when_robust_solution_is_in_scope",
        "objectiveFidelity": "never_shrink_original_scope",
        "iterationBudget": {
            "windowDefault": DEFAULT_GOAL_MAX_ITERATIONS,
            "windowHardCeiling": MAX_GOAL_MAX_ITERATIONS,
            "policy": "checkpoint_extend",
            "stallBreaker": "zero_state_changes_and_zero_verified_progress_in_window",
        },
        "workerContext": {
            "policy": "conversation",
            "history": "model_messages_with_tool_results",
            "compaction": "ordinary_chat",
            "reviewer": "isolated_latest_work",
        },
        "directive": GOAL_POLICY_DIRECTIVE,
    })
}

/// `is_valid_goal_transition(status, reason)`.
fn is_valid_goal_transition(status: &Value, reason: &Value) -> bool {
    let (Some(status), Some(reason)) = (status.as_str(), reason.as_str()) else { return false };
    let reasons: &[&str] = match status {
        "completed" => &["objective_verified"],
        "blocked" => &[
            "iteration_budget_exhausted",
            "execution_budget_exhausted",
            "no_verified_progress",
            "verifier_declared_blocked",
        ],
        "failed" => &["worker_lost", "execution_unavailable", "runtime_failure"],
        "cancelled" => {
            &["human_stop", "superseded_by_human", "superseded_by_new_goal", "conversation_deleted", "runtime_shutdown"]
        }
        _ => &[],
    };
    reasons.contains(&reason)
}

/// `goal_orchestration_id(conversation_id)`.
fn goal_orchestration_id(conversation_id: &str) -> OpResult<String> {
    let conversation_id = py_strip(conversation_id);
    if conversation_id.is_empty() {
        return internal("ValueError: GoalRun requires a conversation id");
    }
    Ok(format!("{GOAL_RUN_ORCHESTRATION_PREFIX}{conversation_id}"))
}

/// `storage_status_for_goal_status(status)`.
fn storage_status_for_goal_status(status: &str) -> OpResult<&'static str> {
    Ok(match status {
        "active" => "running",
        "completed" => "done",
        "blocked" | "failed" => "error",
        "cancelled" => "aborted",
        _ => return internal("KeyError: goal status"),
    })
}

/// `goal_status_from_storage(str(status or ''))`.
fn goal_status_from_storage(status: &str) -> &'static str {
    match status {
        "pending" | "running" => "active",
        "paused" => "blocked",
        "done" => "completed",
        "error" => "failed",
        "aborted" => "cancelled",
        _ => "failed",
    }
}

/// `str(value or '')` of a decoded JSON value.
fn str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if truthy(value) => python_str(value),
        _ => String::new(),
    }
}

/// `_goal_event_append`: the next GoalRun event inside the caller's transaction.
pub(super) fn goal_event_append(
    session: &Session<'_>,
    run_id: &str,
    user_id: i64,
    tenant_id: &str,
    mut document: Map<String, Value>,
) -> OpResult<i64> {
    // The run row fences MAX(seq), ordinary event appends and native claims.
    if session
        .fetch_one_for_update(
            "SELECT id FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
            &params![run_id, user_id, tenant_id],
        )?
        .is_none()
    {
        return fail("database_not_found", "Orchestration run does not exist");
    }
    let row = session.fetch_one(
        concat!(
            "SELECT COALESCE(MAX(seq) + 1, 0) AS next_sequence ",
            "FROM orchestration_run_events WHERE run_id = ? AND user_id = ? ",
            "AND tenant_id = ?"
        ),
        &params![run_id, user_id, tenant_id],
    )?;
    let sequence = match row {
        Some(row) => super::int_or_zero(row.get("next_sequence"))?,
        None => 0,
    };
    if !document.contains_key("format") {
        document.insert("format".into(), Value::String(GOAL_RUN_FORMAT.into()));
    }
    let kind = str_or_empty(document.get("type"));
    let encoded = json_text(&Value::Object(document))?;
    session.execute(
        concat!(
            "INSERT INTO orchestration_run_events(",
            "run_id, user_id, tenant_id, seq, type, node_id, payload, ts) ",
            "VALUES (?, ?, ?, ?, ?, '', ?, ?)"
        ),
        &params![run_id, user_id, tenant_id, sequence, kind, encoded, now_ms()],
    )?;
    Ok(sequence)
}

fn goal_last_event(session: &Session<'_>, run_id: &str, user_id: i64, tenant_id: &str) -> OpResult<Map<String, Value>> {
    let row = session.fetch_one(
        concat!(
            "SELECT payload FROM orchestration_run_events ",
            "WHERE run_id = ? AND user_id = ? AND tenant_id = ? ",
            "AND type IN ('goal_run_started', 'goal_run_transition') ",
            "ORDER BY seq DESC LIMIT 1"
        ),
        &params![run_id, user_id, tenant_id],
    )?;
    match row {
        Some(row) => Ok(object_or_empty(codec::load(row.get("payload"))?)),
        None => Ok(Map::new()),
    }
}

fn goal_started_event(
    session: &Session<'_>,
    run_id: &str,
    user_id: i64,
    tenant_id: &str,
) -> OpResult<Map<String, Value>> {
    let row = session.fetch_one(
        concat!(
            "SELECT payload FROM orchestration_run_events ",
            "WHERE run_id = ? AND user_id = ? AND tenant_id = ? ",
            "AND type = 'goal_run_started' ORDER BY seq LIMIT 1"
        ),
        &params![run_id, user_id, tenant_id],
    )?;
    match row {
        Some(row) => Ok(object_or_empty(codec::load(row.get("payload"))?)),
        None => Ok(Map::new()),
    }
}

fn mapping_or_empty(value: Option<&Value>) -> Value {
    match value {
        Some(Value::Object(map)) => Value::Object(map.clone()),
        _ => Value::Object(Map::new()),
    }
}

/// `_goal_run_projection`.
fn goal_run_projection(session: &Session<'_>, run_id: &str, user_id: i64, tenant_id: &str) -> OpResult<Value> {
    let row = session.fetch_one(
        concat!(
            "SELECT id, orch_id, name, definition, input, status, final, error, ",
            "created_by, created_at, updated_at, finished_at ",
            "FROM orchestration_runs WHERE id = ? AND user_id = ? AND tenant_id = ? ",
            "AND created_by = ?"
        ),
        &params![run_id, user_id, tenant_id, GOAL_RUN_CREATED_BY],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let event = goal_last_event(session, run_id, user_id, tenant_id)?;
    let started = goal_started_event(session, run_id, user_id, tenant_id)?;
    let mut physical_status = row.text("status");
    if physical_status.is_empty() {
        physical_status = "pending".into();
    }
    let status = match event.get("status") {
        Some(value) if truthy(value) => python_str(value),
        _ => goal_status_from_storage(&physical_status).to_owned(),
    };
    let orch_id = row.text("orch_id");
    let conversation_id = orch_id.strip_prefix(GOAL_RUN_ORCHESTRATION_PREFIX).unwrap_or_default().to_owned();
    let terminal = GOAL_RUN_TERMINAL_STATUSES.contains(&status.as_str());
    Ok(json!({
        "format": GOAL_RUN_FORMAT,
        "runId": row.require("id")?.to_json()?,
        "conversationId": conversation_id,
        "objective": or_empty(row.get("input"))?,
        "status": status,
        "reason": str_or_empty(event.get("reason")),
        "policy": mapping_or_empty(started.get("policy")),
        "final": row.text("final"),
        "outcome": mapping_or_empty(event.get("outcome")),
        // Full terminal stop report (excerpt lives in conversation settings).
        "stopReport": str_or_empty(event.get("stopReport")),
        "storageStatus": physical_status,
        "terminal": terminal,
        "createdAt": super::int_or_zero(row.get("created_at"))?,
        "updatedAt": super::int_or_zero(row.get("updated_at"))?,
        "finishedAt": super::int_or_zero(row.get("finished_at"))?,
    }))
}

pub(super) fn get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    goal_run_projection(session, &run_id, user_id, &tenant_id)
}

pub(super) fn latest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conversation_id = required_text(payload, "conversation_id", 256)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let row = session.fetch_one(
        concat!(
            "SELECT id FROM orchestration_runs ",
            "WHERE user_id = ? AND tenant_id = ? AND orch_id = ? ",
            "AND created_by = ? ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        &params![user_id, &tenant_id, goal_orchestration_id(&conversation_id)?, GOAL_RUN_CREATED_BY],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    goal_run_projection(session, &sql_str(row.get("id")), user_id, &tenant_id)
}

/// `_goal_run_start`: atomically supersede any prior active goal and start one run.
pub(super) fn start(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let conversation_id = required_text(payload, "conversation_id", 256)?;
    let objective = required_text(payload, "objective", MAX_GOAL_OBJECTIVE_CHARS)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let (Some(Value::Object(definition)), Some(Value::Object(policy))) =
        (payload.get("definition"), payload.get("policy"))
    else {
        return protocol("Invalid GoalRun definition or policy");
    };
    let definition = Value::Object(definition.clone());
    let policy = Value::Object(policy.clone());
    if !py_eq(&policy, &goal_run_policy()) {
        return protocol("GoalRun policy does not match the canonical contract");
    }
    let orch_id = goal_orchestration_id(&conversation_id)?;
    let existing = session.fetch_one(
        concat!(
            "SELECT id, orch_id, input, definition, created_by ",
            "FROM orchestration_runs ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![&run_id, user_id, &tenant_id],
    )?;
    if let Some(existing) = existing {
        if existing.get("created_by").as_str() != Some(GOAL_RUN_CREATED_BY)
            || existing.get("orch_id").as_str() != Some(orch_id.as_str())
            || existing.get("input").as_str() != Some(objective.as_str())
            || !py_eq(&codec::load(existing.get("definition"))?, &definition)
        {
            return fail("database_conflict", "GoalRun id belongs to another launch");
        }
        let started = goal_started_event(session, &run_id, user_id, &tenant_id)?;
        if !py_eq(started.get("policy").unwrap_or(&Value::Null), &policy) {
            return fail("database_conflict", "GoalRun id has a different policy");
        }
        return Ok(json!({
            "created": false,
            "supersededRunIds": [],
            "run": goal_run_projection(session, &run_id, user_id, &tenant_id)?,
        }));
    }

    let [done, error, aborted] = terminal_params();
    let active_rows = session.fetch_all(
        &format!(
            "SELECT id FROM orchestration_runs WHERE user_id = ? AND tenant_id = ? AND orch_id = ? AND created_by = ? AND status NOT IN ({TERMINAL_STATUS_SQL}) ORDER BY created_at, id"
        ),
        &params![user_id, &tenant_id, &orch_id, GOAL_RUN_CREATED_BY, &done, &error, &aborted],
    )?;
    let now = now_ms();
    let mut superseded: Vec<Value> = Vec::new();
    for active in &active_rows {
        let prior_run_id = sql_str(active.get("id"));
        let cancellation = json!({
            "format": GOAL_RUN_FORMAT,
            "kind": "goal_run_cancelled",
            "reason": "superseded_by_new_goal",
            "supersededByRunId": &run_id,
        });
        let changed = session.execute(
            &format!(
                "UPDATE orchestration_runs SET status = 'aborted', error = ?, updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? ELSE finished_at END WHERE id = ? AND user_id = ? AND tenant_id = ? AND status NOT IN ({TERMINAL_STATUS_SQL})"
            ),
            &params![json_text(&cancellation)?, now, now, &prior_run_id, user_id, &tenant_id, &done, &error, &aborted],
        )?;
        if changed == 0 {
            continue;
        }
        let mut event = Map::new();
        event.insert("type".into(), json!("goal_run_transition"));
        event.insert("status".into(), json!("cancelled"));
        event.insert("reason".into(), json!("superseded_by_new_goal"));
        event.insert("supersededByRunId".into(), json!(&run_id));
        goal_event_append(session, &prior_run_id, user_id, &tenant_id, event)?;
        superseded.push(Value::String(prior_run_id));
    }

    session.execute(
        concat!(
            "INSERT INTO orchestration_runs(",
            "id, user_id, tenant_id, orch_id, name, definition, input, status, ",
            "created_by, created_at, updated_at) ",
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)"
        ),
        &params![
            &run_id,
            user_id,
            &tenant_id,
            &orch_id,
            "Goal Mode",
            json_text(&definition)?,
            &objective,
            GOAL_RUN_CREATED_BY,
            now,
            now,
        ],
    )?;
    let mut event = Map::new();
    event.insert("type".into(), json!("goal_run_started"));
    event.insert("status".into(), json!("active"));
    event.insert("reason".into(), json!("started"));
    event.insert("conversationId".into(), json!(&conversation_id));
    event.insert("objective".into(), json!(&objective));
    event.insert("policy".into(), policy);
    goal_event_append(session, &run_id, user_id, &tenant_id, event)?;
    Ok(json!({
        "created": true,
        "supersededRunIds": superseded,
        "run": goal_run_projection(session, &run_id, user_id, &tenant_id)?,
    }))
}

/// `_goal_run_transition`: append one typed terminal transition and settle its header.
pub(super) fn transition(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let run_id = required_text(payload, "run_id", 200)?;
    let (user_id, tenant_id) = run_owner(payload)?;
    let status_value = payload.get("status").unwrap_or(&Value::Null);
    let reason_value = payload.get("reason").unwrap_or(&Value::Null);
    if !is_valid_goal_transition(status_value, reason_value) {
        return protocol("Invalid GoalRun terminal status/reason pair");
    }
    let status = status_value.as_str().unwrap_or_default().to_owned();
    let reason = reason_value.as_str().unwrap_or_default().to_owned();
    let empty_text = Value::String(String::new());
    let empty_map = Value::Object(Map::new());
    let final_value = get_or(payload, "final", &empty_text);
    let outcome_value = get_or(payload, "outcome", &empty_map);
    // The full (machine-token-stripped) terminal VU verdict, persisted on the
    // transition event so the conversation fold can stay a bounded excerpt.
    let stop_report = optional_text(payload, "stop_report", "", GOAL_STOP_REPORT_MAX_CHARS, "goal run")?;
    let (Some(final_text), Some(outcome)) = (final_value.as_str(), outcome_value.as_object()) else {
        return protocol("Invalid GoalRun transition payload");
    };
    let row = session.fetch_one_for_update(
        concat!(
            "SELECT status, final, created_by FROM orchestration_runs ",
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        ),
        &params![&run_id, user_id, &tenant_id],
    )?;
    let Some(row) = row.filter(|row| row.get("created_by").as_str() == Some(GOAL_RUN_CREATED_BY)) else {
        return Ok(json!({"transitioned": false, "run": null}));
    };
    let current_event = goal_last_event(session, &run_id, user_id, &tenant_id)?;
    let current_goal_status = match current_event.get("status") {
        Some(value) if truthy(value) => python_str(value),
        _ => goal_status_from_storage(&row.text("status")).to_owned(),
    };
    if is_terminal_column(row.get("status")) {
        let current_outcome = current_event.get("outcome").cloned().unwrap_or_else(|| Value::Object(Map::new()));
        if current_goal_status != status
            || !py_eq(current_event.get("reason").unwrap_or(&Value::Null), reason_value)
            || row.text("final") != final_text
            || !py_eq(&current_outcome, &Value::Object(outcome.clone()))
            || str_or_empty(current_event.get("stopReport")) != stop_report
        {
            return fail("database_conflict", "GoalRun terminal meaning is immutable");
        }
        return Ok(json!({
            "transitioned": false,
            "run": goal_run_projection(session, &run_id, user_id, &tenant_id)?,
        }));
    }

    let physical_status = storage_status_for_goal_status(&status)?;
    let now = now_ms();
    let mut error_value = String::new();
    if status != "completed" {
        error_value = json_text(&json!({
            "format": GOAL_RUN_FORMAT,
            "kind": format!("goal_run_{status}"),
            "reason": &reason,
        }))?;
    }
    let [done, error, aborted] = terminal_params();
    let changed = session.execute(
        &format!(
            "UPDATE orchestration_runs SET status = ?, final = ?, error = ?, updated_at = ?, finished_at = CASE WHEN finished_at = 0 THEN ? ELSE finished_at END WHERE id = ? AND user_id = ? AND tenant_id = ? AND status NOT IN ({TERMINAL_STATUS_SQL})"
        ),
        &params![physical_status, final_text, error_value, now, now, &run_id, user_id, &tenant_id, done, error, aborted],
    )?;
    if changed == 0 {
        return fail("database_conflict", "GoalRun transition lost its active fence");
    }
    let mut event = Map::new();
    event.insert("type".into(), json!("goal_run_transition"));
    event.insert("status".into(), json!(&status));
    event.insert("reason".into(), json!(&reason));
    event.insert("outcome".into(), Value::Object(outcome.clone()));
    if !stop_report.is_empty() {
        event.insert("stopReport".into(), json!(&stop_report));
    }
    goal_event_append(session, &run_id, user_id, &tenant_id, event)?;
    Ok(json!({
        "transitioned": true,
        "run": goal_run_projection(session, &run_id, user_id, &tenant_id)?,
    }))
}
