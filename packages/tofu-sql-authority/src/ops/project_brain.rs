//! `operation_domains/project_brain.py`: the transactional Project Brain event
//! stream and complete projection (`operations_pkg/_project_brain.py`).
//!
//! Every command locks one `owner_user_id + normalized project_key` scope,
//! appends exactly one semantic event, folds the projection in the same
//! transaction and returns a push hint. `_project_brain_relink_scope` is not
//! an operation of this domain and is not ported here.

mod fold;
mod maintenance;
mod py;

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use self::fold::{
    append_event_and_fold, bounded_utf8, command_result, interruption_supersedes, is_terminal, load_projection,
    protocol, public_projection, strip_trailing_separators, work_index, work_item, Projection,
    NARRATIVE_TEXT_LIMIT_BYTES, WORK_TERMINAL,
};
use self::py::{field, get, int0, text};
use super::{register as add, OperationSpec, Registry};
use crate::codec::py_eq;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::Session;

pub fn register(registry: &mut Registry) {
    add(registry, "project_brain.get", OperationSpec::query(project_brain_get));
    add(registry, "project_brain.active.list", OperationSpec::query(maintenance::list_active));
    add(registry, "project_brain.work.start", OperationSpec::command(true, work_start));
    add(registry, "project_brain.work.refine", OperationSpec::command(true, work_refine));
    add(registry, "project_brain.work.change", OperationSpec::command(true, work_change));
    add(registry, "project_brain.work.finish", OperationSpec::command(true, work_finish));
    add(registry, "project_brain.narrative.add", OperationSpec::command(true, narrative_add));
    add(registry, "project_brain.checker.register", OperationSpec::command(true, checker_register));
    add(registry, "project_brain.checker.result", OperationSpec::command(true, checker_result));
    add(registry, "project_brain.decision.promote", OperationSpec::command(true, decision_promote));
    add(registry, "project_brain.watch.add", OperationSpec::command(true, watch_add));
    add(registry, "project_brain.watch.update", OperationSpec::command(true, watch_update));
    add(registry, "project_brain.watch.delete", OperationSpec::command(true, watch_delete));
    // Prepare mutates only once (cursor initialization) and must remain fresh
    // thereafter, so it intentionally has no permanent command receipt.
    add(registry, "project_brain.cursor.prepare", OperationSpec::command(false, cursor_prepare));
    add(registry, "project_brain.cursor.confirm", OperationSpec::command(true, cursor_confirm));
    add(registry, "project_brain.cutover.status", OperationSpec::query(maintenance::cutover_status));
    add(registry, "project_brain.cutover", OperationSpec::command(true, maintenance::cutover));
    add(registry, "project_brain.recovery.snapshot", OperationSpec::maintenance(maintenance::recovery_snapshot));
    add(registry, "project_brain.rebuild", OperationSpec::maintenance(maintenance::rebuild));
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `_project_identity`.
fn project_identity(payload: &Payload) -> OpResult<(i64, String)> {
    let owner_user_id = integer(payload, "owner_user_id", None, Some(1), None)?;
    let raw = required_text(payload, "project_key", 4096)?;
    let project_key = strip_trailing_separators(py::strip(&raw));
    if project_key.is_empty() {
        return protocol("Project Brain project_key is empty");
    }
    Ok((owner_user_id, project_key))
}

fn object(entries: Vec<(&str, Value)>) -> Map<String, Value> {
    entries.into_iter().map(|(key, value)| (key.to_owned(), value)).collect()
}

/// `_string_list`.
fn string_list(
    value: &Value,
    field_name: &str,
    limit: Option<usize>,
    item_limit: usize,
    allow_empty: bool,
) -> OpResult<Vec<Value>> {
    let message = format!("Invalid Project Brain {field_name}");
    let Value::Array(items) = value else { return protocol(&message) };
    if limit.is_some_and(|limit| items.len() > limit) {
        return protocol(&message);
    }
    for item in items {
        match item {
            Value::String(text) if char_len(text) <= item_limit && (allow_empty || !text.is_empty()) => {}
            _ => return protocol(&message),
        }
    }
    Ok(items.clone())
}

/// `_artifacts`.
fn artifacts(value: &Value) -> OpResult<Vec<Value>> {
    let Value::Array(items) = value else { return protocol("Invalid Project Brain artifacts") };
    let mut result = Vec::new();
    for raw in items {
        let Value::Object(raw) = raw else { return protocol("Invalid Project Brain artifact") };
        let id = required_text(raw, "id", 256)?;
        let title = text(field(raw, "title"));
        let format = text(field(raw, "format"));
        let path = text(field(raw, "path"));
        if char_len(&title) > 500 || char_len(&format) > 128 || char_len(&path) > 4096 {
            return protocol("Invalid Project Brain artifact");
        }
        result.push(Value::Object(object(vec![
            ("id", Value::String(id)),
            ("title", Value::String(title)),
            ("format", Value::String(format)),
            ("path", Value::String(path)),
        ])));
    }
    Ok(result)
}

/// `_validated_work_item`.
fn validated_work_item(value: &Map<String, Value>) -> OpResult<Map<String, Value>> {
    let task_id = required_text(value, "taskId", 256)?;
    let work_id = required_text(value, "id", 128)?;
    let digest = hex::encode(Sha256::digest(task_id.as_bytes()));
    if work_id != format!("pw_{}", &digest[..24]) {
        return protocol("Project work id is not deterministic");
    }
    let trigger = required_text(value, "trigger", 32)?;
    if !matches!(trigger.as_str(), "todo_write" | "file_write" | "isolated_workspace") {
        return protocol("Invalid Project work trigger");
    }
    let status = required_text(value, "status", 32)?;
    if status != "active" {
        return protocol("New Project work must be active");
    }
    let summary = match value.get("resultSummary") {
        Some(Value::String(summary)) if char_len(summary) <= 4000 => summary.clone(),
        _ => return protocol("Invalid Project work resultSummary"),
    };
    if !field(value, "finishedAt").is_null() {
        return protocol("New Project work cannot be terminal");
    }
    let priority = int0(field(value, "_titlePriority"))?;
    if !(1..=1000).contains(&priority) {
        return protocol("Invalid Project work title priority");
    }
    let conversation_id = required_text(value, "conversationId", 256)?;
    let title = required_text(value, "title", 500)?;
    let changed_paths = string_list(field(value, "changedPaths"), "changedPaths", None, 4096, false)?;
    let artifacts = artifacts(field(value, "artifacts"))?;
    let started_at = integer(value, "startedAt", None, Some(0), None)?;
    let refined = truthy(value.get("_titleRefined").unwrap_or(&Value::Bool(false)));
    Ok(object(vec![
        ("id", Value::String(work_id)),
        ("taskId", Value::String(task_id)),
        ("conversationId", Value::String(conversation_id)),
        ("title", Value::String(title)),
        ("trigger", Value::String(trigger)),
        ("status", Value::String(status)),
        ("changedPaths", Value::Array(changed_paths)),
        ("artifacts", Value::Array(artifacts)),
        ("resultSummary", Value::String(summary)),
        ("startedAt", Value::from(started_at)),
        ("finishedAt", Value::Null),
        ("_titlePriority", Value::from(priority)),
        ("_titleRefined", Value::Bool(refined)),
    ]))
}

/// `_validated_checker`.
fn validated_checker(value: &Map<String, Value>) -> OpResult<Map<String, Value>> {
    let argv = string_list(field(value, "argv"), "checker argv", Some(32), 4096, false)?;
    let globs = string_list(field(value, "pathGlobs"), "checker pathGlobs", Some(64), 4096, false)?;
    if argv.is_empty() || globs.is_empty() || !field(value, "enabled").is_boolean() {
        return protocol("Invalid Checker definition");
    }
    Ok(object(vec![
        ("checkerId", Value::String(required_text(value, "checkerId", 128)?)),
        ("version", Value::from(integer(value, "version", None, Some(1), None)?)),
        ("label", Value::String(required_text(value, "label", 256)?)),
        ("argv", Value::Array(argv)),
        ("cwd", Value::String(required_text(value, "cwd", 4096)?)),
        ("pathGlobs", Value::Array(globs)),
        ("timeoutMs", Value::from(integer(value, "timeoutMs", None, Some(100), Some(3_600_000))?)),
        ("enabled", value["enabled"].clone()),
    ]))
}

/// `_validated_watch_item`.
fn validated_watch_item(value: &Map<String, Value>) -> OpResult<Map<String, Value>> {
    let status = required_text(value, "status", 32)?;
    if status != "active" && status != "resolved" {
        return protocol("Invalid Project Watch status");
    }
    let latest = field(value, "latestResult");
    let latest_result = match latest {
        Value::Null => Value::Null,
        Value::Object(latest) => {
            let latest_text = text(field(latest, "text"));
            let latest_trigger = text(field(latest, "trigger"));
            if char_len(&latest_text) > 4000 || char_len(&latest_trigger) > 64 {
                return protocol("Invalid Project Watch latestResult");
            }
            Value::Object(object(vec![
                ("text", Value::String(latest_text)),
                ("trigger", Value::String(latest_trigger)),
                ("timestamp", Value::from(integer(latest, "timestamp", None, Some(0), None)?)),
            ]))
        }
        _ => return protocol("Invalid Project Watch latestResult"),
    };
    Ok(object(vec![
        ("id", Value::String(required_text(value, "id", 128)?)),
        ("kind", Value::String(required_text(value, "kind", 64)?)),
        ("text", Value::String(required_text(value, "text", 4000)?)),
        ("status", Value::String(status)),
        ("sourceConversationId", Value::String(py::prefix(&text(field(value, "sourceConversationId")), 256))),
        ("createdAt", Value::from(integer(value, "createdAt", None, Some(0), None)?)),
        ("updatedAt", Value::from(integer(value, "updatedAt", None, Some(0), None)?)),
        ("latestResult", latest_result),
    ]))
}

/// `_project_brain_get`.
fn project_brain_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, project_key) = project_identity(payload)?;
    public_projection(&load_projection(session, owner_user_id, project_key.as_str())?)
}

/// `any(str(item.get('checkerId') or '') == id and int(item.get('version') or 0) == version
///      for item in projection.get('checkers') or ())`.
fn checker_registered(projection: &Projection, checker_id: &str, version: i64) -> OpResult<Option<Value>> {
    for item in py::items(field(projection, "checkers"))? {
        if text(py::attr(&item, "checkerId")?) == checker_id && int0(py::attr(&item, "version")?)? == version {
            return Ok(Some(item));
        }
    }
    Ok(None)
}

/// `_project_brain_command`.
fn project_brain_command(session: &Session<'_>, payload: &Payload, action: &str) -> OpResult<Value> {
    let (owner_user_id, project_key) = project_identity(payload)?;
    session.lock_key("project_brain.project", &format!("{owner_user_id}:{project_key}"));
    let mut projection = load_projection(session, owner_user_id, &project_key)?;
    let now_ms = integer(payload, "timestamp", Some(now_ms()), Some(0), None)?;

    let (kind, event_payload): (&str, Map<String, Value>) = match action {
        "work.start" => {
            let Some(Value::Object(work_item_value)) = payload.get("work_item") else {
                return protocol("work_item must be an object");
            };
            let validated = validated_work_item(work_item_value)?;
            let work_id = text(field(&validated, "id"));
            if let Some(index) = work_index(&projection, &work_id)? {
                let existing = work_item(&projection, index);
                if text(get(&existing, "taskId")) != text(field(&validated, "taskId"))
                    || text(get(&existing, "conversationId")) != text(field(&validated, "conversationId"))
                {
                    return fail("database_conflict", "Project work ownership is immutable");
                }
                return command_result(&projection, None);
            }
            ("work_started", object(vec![("workItem", Value::Object(validated))]))
        }
        "work.refine" => {
            let work_id = required_text(payload, "work_id", 128)?;
            let Some(index) = work_index(&projection, &work_id)? else {
                return fail("database_not_found", "Project work item not found");
            };
            let item = work_item(&projection, index);
            let title = py::strip(&required_text(payload, "title", 500)?).to_owned();
            let priority = integer(payload, "title_priority", None, Some(1), Some(1000))?;
            if !py::is_str(get(&item, "status"), "active")
                || truthy(get(&item, "_titleRefined"))
                || priority <= int0(get(&item, "_titlePriority"))?
            {
                return command_result(&projection, None);
            }
            (
                "work_title_refined",
                object(vec![
                    ("workId", Value::String(work_id)),
                    ("title", Value::String(title)),
                    ("titlePriority", Value::from(priority)),
                ]),
            )
        }
        "work.change" => {
            let work_id = required_text(payload, "work_id", 128)?;
            if work_index(&projection, &work_id)?.is_none() {
                return fail("database_not_found", "Project work item not found");
            }
            let changed = field(payload, "changed_paths");
            let changed = if truthy(changed) { changed.clone() } else { json!([]) };
            let changed_paths = string_list(&changed, "changedPaths", None, 4096, false)?;
            let raw_artifacts = field(payload, "artifacts");
            let raw_artifacts = if truthy(raw_artifacts) { raw_artifacts.clone() } else { json!([]) };
            (
                "work_changed",
                object(vec![
                    ("workId", Value::String(work_id)),
                    ("changedPaths", Value::Array(changed_paths)),
                    ("artifacts", Value::Array(artifacts(&raw_artifacts)?)),
                ]),
            )
        }
        "work.finish" => {
            let work_id = required_text(payload, "work_id", 128)?;
            let Some(index) = work_index(&projection, &work_id)? else {
                return fail("database_not_found", "Project work item not found");
            };
            let status = required_text(payload, "status", 32)?;
            if !WORK_TERMINAL.contains(&status.as_str()) {
                return protocol("Invalid Project work terminal status");
            }
            let current = get(&work_item(&projection, index), "status").clone();
            if is_terminal(&current) && !interruption_supersedes(&current, &status) {
                return command_result(&projection, None);
            }
            (
                "work_finished",
                object(vec![
                    ("workId", Value::String(work_id)),
                    ("status", Value::String(status)),
                    ("resultSummary", Value::String(py::prefix(&text(field(payload, "result_summary")), 4000))),
                ]),
            )
        }
        "narrative.add" => {
            let narrative_text = bounded_utf8(&required_text(payload, "text", 720)?, NARRATIVE_TEXT_LIMIT_BYTES);
            (
                "narrative_added",
                object(vec![
                    ("narrativeKind", Value::String(required_text(payload, "kind", 64)?)),
                    ("text", Value::String(narrative_text)),
                    ("workId", Value::String(py::prefix(&text(field(payload, "work_id")), 128))),
                    ("conversationId", Value::String(py::prefix(&text(field(payload, "conversation_id")), 256))),
                ]),
            )
        }
        "checker.register" => {
            let Some(Value::Object(definition)) = payload.get("definition") else {
                return protocol("Checker definition must be an object");
            };
            let validated = validated_checker(definition)?;
            let checker_id = text(field(&validated, "checkerId"));
            let version = int0(field(&validated, "version"))?;
            if let Some(existing) = checker_registered(&projection, &checker_id, version)? {
                if !py_eq(&existing, &Value::Object(validated)) {
                    return fail("database_conflict", "Checker versions are immutable");
                }
                return command_result(&projection, None);
            }
            ("checker_registered", object(vec![("definition", Value::Object(validated))]))
        }
        "decision.promote" => {
            let Some(Value::Object(decision)) = payload.get("decision") else {
                return protocol("Decision must be an object");
            };
            let Some(Value::Object(checker_ref)) = decision.get("checkerRef") else {
                return protocol("Decision checkerRef is required");
            };
            let checker_id = required_text(checker_ref, "id", 128)?;
            let version = integer(checker_ref, "version", None, Some(1), None)?;
            if checker_registered(&projection, &checker_id, version)?.is_none() {
                return fail("database_not_found", "Decision references an unknown checker version");
            }
            let decision_id = required_text(decision, "decisionId", 128)?;
            required_text(decision, "text", 4000)?;
            required_text(decision, "sourceConversationId", 256)?;
            required_text(decision, "sourceTurnId", 256)?;
            if !matches!(decision.get("latestVerification"), Some(Value::Null | Value::Object(_))) {
                return protocol("Decision latestVerification must be null or an object");
            }
            let charter = field(&projection, "charter");
            let charter = if truthy(charter) { charter.clone() } else { json!({}) };
            let mut existing_decision = None;
            for item in py::items(py::attr(&charter, "decisions")?)? {
                if text(py::attr(&item, "decisionId")?) == decision_id {
                    existing_decision = Some(item);
                    break;
                }
            }
            let decision_value = Value::Object(decision.clone());
            if let Some(existing) = existing_decision {
                if !py_eq(&existing, &decision_value) {
                    return fail("database_conflict", "Charter decisions are immutable");
                }
                return command_result(&projection, None);
            }
            ("decision_promoted", object(vec![("decision", decision_value)]))
        }
        "checker.result" => {
            let Some(Value::Object(result)) = payload.get("result") else {
                return protocol("Checker result must be an object");
            };
            let Some(Value::Object(checker_ref)) = result.get("checkerRef") else {
                return protocol("Checker result ref is required");
            };
            let checker_id = required_text(checker_ref, "id", 128)?;
            let version = integer(checker_ref, "version", None, Some(1), None)?;
            if checker_registered(&projection, &checker_id, version)?.is_none() {
                return fail("database_not_found", "Checker result version is not registered");
            }
            let output = text(field(result, "output"));
            let summary = text(field(result, "summary"));
            if !field(result, "ok").is_boolean()
                || !field(result, "timedOut").is_boolean()
                || char_len(&output) > 4000
                || char_len(&summary) > 1000
            {
                return protocol("Invalid Checker result");
            }
            let exit_code = field(result, "exitCode");
            if !exit_code.is_null() && !(exit_code.is_i64() || exit_code.is_u64()) {
                return protocol("Invalid Checker exitCode");
            }
            let normalized = object(vec![
                (
                    "checkerRef",
                    Value::Object(object(vec![("id", Value::String(checker_id)), ("version", Value::from(version))])),
                ),
                ("label", Value::String(required_text(result, "label", 256)?)),
                ("ok", result["ok"].clone()),
                ("exitCode", exit_code.clone()),
                ("timedOut", result["timedOut"].clone()),
                ("durationMs", Value::from(integer(result, "durationMs", None, Some(0), None)?)),
                ("reason", Value::String(required_text(result, "reason", 64)?)),
                ("summary", Value::String(summary)),
                ("output", Value::String(output)),
                ("workId", Value::String(py::prefix(&text(field(result, "workId")), 128))),
                ("timestamp", Value::from(integer(result, "timestamp", None, Some(0), None)?)),
            ]);
            (
                "checker_result",
                object(vec![
                    ("result", Value::Object(normalized)),
                    ("decisionId", Value::String(py::prefix(&text(field(payload, "decision_id")), 128))),
                ]),
            )
        }
        "watch.add" | "watch.update" | "watch.delete" => {
            let item = field(payload, "item");
            if action != "watch.delete" && !item.is_object() {
                return protocol("Watch item must be an object");
            }
            if action == "watch.delete" {
                let item_id = required_text(payload, "item_id", 128)?;
                let mut found = false;
                for row in py::items(field(&projection, "watch"))? {
                    if text(py::attr(&row, "id")?) == item_id {
                        found = true;
                        break;
                    }
                }
                if !found {
                    return fail("database_not_found", "Project Watch item not found");
                }
                ("watch_deleted", object(vec![("itemId", Value::String(item_id))]))
            } else {
                let validated = validated_watch_item(item.as_object().expect("watch item object"))?;
                let candidate_id = text(field(&validated, "id"));
                let rows = py::items(field(&projection, "watch"))?;
                let exists = rows.iter().any(|row| row.is_object() && text(get(row, "id")) == candidate_id);
                if action == "watch.add" && exists {
                    let mut current = None;
                    for row in &rows {
                        if text(py::attr(row, "id")?) == candidate_id {
                            current = Some(row);
                            break;
                        }
                    }
                    let current = current.expect("an existing watch row");
                    if !py_eq(current, &Value::Object(validated)) {
                        return fail("database_conflict", "Project Watch item already exists");
                    }
                    return command_result(&projection, None);
                }
                if action == "watch.update" && !exists {
                    return fail("database_not_found", "Project Watch item not found");
                }
                let kind = if action == "watch.update" { "watch_updated" } else { "watch_added" };
                (kind, object(vec![("item", Value::Object(validated))]))
            }
        }
        "cursor.confirm" => {
            let conversation_id = required_text(payload, "conversation_id", 256)?;
            let delivered = integer(payload, "delivered_sequence", None, Some(0), None)?;
            let cursors = field(&projection, "cursors");
            let cursors = if truthy(cursors) { cursors.clone() } else { json!({}) };
            let current = py::dict(py::attr(&cursors, &conversation_id)?)?;
            if delivered <= int0(field(&current, "deliveredSequence"))? {
                return command_result(&projection, None);
            }
            let head = int0(field(&projection, "headSequence"))?;
            (
                "cursor_confirmed",
                object(vec![
                    ("conversationId", Value::String(conversation_id)),
                    ("deliveredSequence", Value::from(delivered.min(head))),
                ]),
            )
        }
        _ => return protocol(&format!("Unknown Project Brain action: {action}")),
    };

    let event =
        append_event_and_fold(session, owner_user_id, &project_key, &mut projection, kind, event_payload, now_ms)?;
    command_result(&projection, Some(event))
}

fn work_start(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "work.start")
}

fn work_refine(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "work.refine")
}

fn work_change(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "work.change")
}

fn work_finish(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "work.finish")
}

fn narrative_add(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "narrative.add")
}

fn checker_register(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "checker.register")
}

fn checker_result(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "checker.result")
}

fn decision_promote(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "decision.promote")
}

fn watch_add(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "watch.add")
}

fn watch_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "watch.update")
}

fn watch_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    project_brain_command(session, payload, "watch.delete")
}

fn delivery_token(owner_user_id: i64, project_key: &str, conversation_id: &str, from: i64, to: i64) -> String {
    hex::encode(Sha256::digest(format!("{owner_user_id}\0{project_key}\0{conversation_id}\0{from}\0{to}").as_bytes()))
}

/// `_project_brain_cursor_prepare`.
fn cursor_prepare(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, project_key) = project_identity(payload)?;
    let conversation_id = required_text(payload, "conversation_id", 256)?;
    session.lock_key("project_brain.project", &format!("{owner_user_id}:{project_key}"));
    let mut projection = load_projection(session, owner_user_id, &project_key)?;
    let cursors = field(&projection, "cursors");
    let cursors = if truthy(cursors) { cursors.clone() } else { json!({}) };
    if !py::contains(&cursors, &conversation_id)? {
        let now_ms = integer(payload, "timestamp", Some(now_ms()), Some(0), None)?;
        let delivered = int0(field(&projection, "headSequence"))? + 1;
        let event = append_event_and_fold(
            session,
            owner_user_id,
            &project_key,
            &mut projection,
            "cursor_initialized",
            object(vec![
                ("conversationId", Value::String(conversation_id)),
                ("deliveredSequence", Value::from(delivered)),
            ]),
            now_ms,
        )?;
        let sequence = py::int(get(&event, "projectSequence"))?;
        return Ok(json!({
            "initialized": true,
            "entries": [],
            "fromSequence": sequence,
            "toSequence": sequence,
            "headSequence": sequence,
            "deliveryToken": "",
        }));
    }

    let cursor = py::attr(&cursors, &conversation_id)?;
    let cursor = if truthy(cursor) { cursor.clone() } else { json!({}) };
    let delivered = int0(py::attr(&cursor, "deliveredSequence")?)?;
    let limit = integer(payload, "limit", Some(12), Some(1), Some(12))?;
    let token_budget = integer(payload, "token_budget", Some(900), Some(1), Some(900))?;
    let mut entries: Vec<Value> = Vec::new();
    let mut spent = 0i64;
    for item in py::items(field(&projection, "narratives"))? {
        if !item.is_object() {
            continue;
        }
        let sequence = int0(get(&item, "sequence"))?;
        if sequence <= delivered {
            continue;
        }
        // Provider-neutral upper bound: one token per UTF-8 byte plus the row
        // envelope. It stops before the first non-fitting row, preserving
        // strict sequence pagination.
        let cost = (text(get(&item, "text")).len() as i64).max(1) + 12;
        if !entries.is_empty() && spent + cost > token_budget {
            break;
        }
        if entries.is_empty() && cost > token_budget {
            return fail("database_integrity", "Stored Project narrative exceeds the delivery budget");
        }
        entries.push(item);
        spent += cost;
        if entries.len() as i64 >= limit {
            break;
        }
    }
    let to_sequence = match entries.last() {
        Some(last) => py::int(get(last, "sequence"))?,
        None => delivered,
    };
    let token = if entries.is_empty() {
        String::new()
    } else {
        delivery_token(owner_user_id, &project_key, &conversation_id, delivered, to_sequence)
    };
    Ok(json!({
        "initialized": false,
        "entries": entries,
        "fromSequence": delivered,
        "toSequence": to_sequence,
        "headSequence": int0(field(&projection, "headSequence"))?,
        "deliveryToken": token,
    }))
}

/// `_project_brain_cursor_confirm`.
fn cursor_confirm(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, project_key) = project_identity(payload)?;
    let conversation_id = required_text(payload, "conversation_id", 256)?;
    let delivered = integer(payload, "delivered_sequence", None, Some(0), None)?;
    let from_sequence = integer(payload, "from_sequence", None, Some(0), None)?;
    let expected = delivery_token(owner_user_id, &project_key, &conversation_id, from_sequence, delivered);
    if text(field(payload, "delivery_token")) != expected {
        return protocol("Invalid narrative delivery token");
    }
    project_brain_command(session, payload, "cursor.confirm")
}
