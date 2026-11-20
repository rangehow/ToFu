//! `operations_pkg/_project.py`: owner-scoped recent projects and the
//! cross-plane `project.relink` re-key.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};

use super::py::{get, int_or_zero_strict, map_get, str_or_empty};
use super::records::{column_str, load_record_value, now_ms, record_delete, record_get_value, record_put};
use crate::codec::{dump, dump_param, load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json;
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const RECENT_PROJECT_NAMESPACE: &str = "recent_projects";
/// `lib/project_recent_contract.py::RECENT_PROJECT_PATH_MAX_CHARS`.
const RECENT_PROJECT_PATH_MAX_CHARS: usize = 4096;
/// `lib/project_recent_contract.py::PROJECT_RELINK_STORAGE_DEADLINE_SECONDS`.
pub const PROJECT_RELINK_STORAGE_DEADLINE_SECONDS: f64 = 120.0;

fn recent_project_prefix(payload: &Payload) -> OpResult<String> {
    Ok(format!("{}:", integer(payload, "user_id", None, Some(1), None)?))
}

fn recent_project_key(payload: &Payload) -> OpResult<String> {
    let prefix = recent_project_prefix(payload)?;
    Ok(prefix + &required_text(payload, "project_path", 4096)?)
}

fn record_payload(namespace: &str, key: &str, value: Option<Value>) -> Payload {
    let mut payload = Map::new();
    payload.insert("namespace".into(), Value::from(namespace));
    payload.insert("key".into(), Value::from(key));
    if let Some(value) = value {
        payload.insert("value".into(), value);
    }
    payload
}

/// `dict(value)` of a decoded record value (falsy values read as `{}`).
fn as_dict(value: &Value) -> OpResult<Map<String, Value>> {
    if !json::truthy(value) {
        return Ok(Map::new());
    }
    match value {
        Value::Object(map) => Ok(map.clone()),
        _ => internal("TypeError: cannot convert to dict"),
    }
}

pub fn project_recent_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let prefix = recent_project_prefix(payload)?;
    let rows = session.fetch_all(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key LIKE ? ORDER BY record_key",
        &params![RECENT_PROJECT_NAMESPACE, prefix + "%"],
    )?;
    let mut projects = Vec::new();
    for row in rows {
        if let Value::Object(map) = load_record_value(RECENT_PROJECT_NAMESPACE, row.get("value_json"))? {
            projects.push(map);
        }
    }
    let mut keyed = Vec::with_capacity(projects.len());
    for project in projects {
        keyed.push((int_or_zero_strict(map_get(&project, "last_used"))?, project));
    }
    keyed.sort_by_key(|entry| std::cmp::Reverse(entry.0));
    Ok(Value::Array(keyed.into_iter().map(|(_, project)| Value::Object(project)).collect()))
}

pub fn project_recent_touch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let key = recent_project_key(payload)?;
    session.lock_key(RECENT_PROJECT_NAMESPACE, &key);
    let current = record_get_value(session, RECENT_PROJECT_NAMESPACE, &key)?;
    let mut value = as_dict(get(&current, "value"))?;
    let path = required_text(payload, "project_path", 4096)?;
    let count = int_or_zero_strict(map_get(&value, "count"))? + 1;
    let last_used = integer(payload, "last_used", None, Some(0), None)?;
    value.insert("path".into(), Value::from(path));
    value.insert("count".into(), Value::from(count));
    value.insert("last_used".into(), Value::from(last_used));
    let value = Value::Object(value);
    record_put(session, &record_payload(RECENT_PROJECT_NAMESPACE, &key, Some(value.clone())))?;
    Ok(value)
}

pub fn project_recent_touch_many(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let raw_paths = match payload.get("project_paths") {
        Some(Value::Array(items)) if !items.is_empty() => items,
        _ => return fail("database_protocol_error", "Invalid project_paths in storage request"),
    };
    let mut paths: Vec<String> = Vec::new();
    let mut seen = BTreeSet::new();
    for candidate in raw_paths {
        let mut single = Map::new();
        single.insert("project_path".into(), candidate.clone());
        let path = required_text(&single, "project_path", RECENT_PROJECT_PATH_MAX_CHARS)?;
        if seen.insert(path.clone()) {
            paths.push(path);
        }
    }
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let last_used = integer(payload, "last_used", None, Some(0), None)?;
    let mut sorted = paths.clone();
    sorted.sort();
    for path in sorted {
        let mut touch = Map::new();
        touch.insert("user_id".into(), Value::from(user_id));
        touch.insert("last_used".into(), Value::from(last_used));
        touch.insert("project_path".into(), Value::from(path));
        project_recent_touch(session, &touch)?;
    }
    Ok(json!({"touched": paths.len()}))
}

pub fn project_recent_clear(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner_prefix = recent_project_prefix(payload)?;
    session.lock_key(RECENT_PROJECT_NAMESPACE, &owner_prefix);
    let deleted = session.execute(
        "DELETE FROM storage_records WHERE namespace = ? AND record_key LIKE ?",
        &params![RECENT_PROJECT_NAMESPACE, owner_prefix + "%"],
    )?;
    Ok(json!({"deleted": deleted}))
}

/// `_replace_project_path_references`; `None` when unchanged.
fn replace_project_path_references(
    settings: &Map<String, Value>,
    old_path: &str,
    new_path: &str,
) -> Option<Map<String, Value>> {
    let mut rewritten = settings.clone();
    let mut changed = false;
    let old_value = Value::from(old_path);
    let new_value = Value::from(new_path);
    if rewritten.get("projectPath").is_some_and(|value| py_eq(value, &old_value)) {
        rewritten.insert("projectPath".into(), new_value.clone());
        changed = true;
    }
    for key in ["projectPaths", "readOnlyPaths"] {
        let Some(Value::Array(items)) = rewritten.get(key) else { continue };
        if !items.iter().any(|item| py_eq(item, &old_value)) {
            continue;
        }
        let mut deduplicated: Vec<Value> = Vec::new();
        for item in items {
            let candidate = if py_eq(item, &old_value) { new_value.clone() } else { item.clone() };
            if !deduplicated.iter().any(|existing| py_eq(existing, &candidate)) {
                deduplicated.push(candidate);
            }
        }
        rewritten.insert(key.into(), Value::Array(deduplicated));
        changed = true;
    }
    changed.then_some(rewritten)
}

/// `_conversation_project_relink` (SQLite path).
fn conversation_project_relink(
    session: &Session<'_>,
    user_id: i64,
    old_path: &str,
    new_path: &str,
) -> OpResult<(usize, usize)> {
    let json_literal = String::from_utf8(dump(&Value::from(old_path))?).expect("orjson emits UTF-8");
    let escaped_literal = json_literal.replace('!', "!!").replace('%', "!%").replace('_', "!_");
    let tables = [("storage_conversations", "id"), ("storage_conversation_trash", "conversation_id")];
    let mut moved = Vec::new();
    for (table, id_column) in tables {
        let mut candidates: Vec<(String, SqlValue)> = Vec::new();
        let mut after_id = String::new();
        loop {
            let rows = session.fetch_all(
                &format!(
                    "SELECT {id_column} AS conversation_id,settings_json FROM {table} \
                     WHERE user_id=? AND CAST(settings_json AS TEXT) LIKE ? ESCAPE '!' \
                     AND {id_column}>? ORDER BY {id_column} LIMIT ?"
                ),
                &params![user_id, format!("%{escaped_literal}%"), &after_id, 512i64],
            )?;
            if rows.is_empty() {
                break;
            }
            after_id = column_str(rows.last().expect("page").get("conversation_id"));
            candidates.extend(
                rows.into_iter().map(|row| (column_str(row.get("conversation_id")), row.get("settings_json").clone())),
            );
        }
        moved.push((table, id_column, candidates));
    }
    let mut updates: Vec<Vec<Vec<SqlValue>>> = Vec::new();
    for (_, _, candidates) in &moved {
        let mut parameters = Vec::new();
        for (conv_id, stored) in candidates {
            let settings = load(stored)?;
            let settings = if json::truthy(&settings) { settings } else { Value::Object(Map::new()) };
            let Value::Object(settings) = settings else {
                return fail("database_integrity", "Conversation settings are malformed");
            };
            let Some(rewritten) = replace_project_path_references(&settings, old_path, new_path) else { continue };
            parameters.push(vec![
                dump_param(&Value::Object(rewritten))?,
                SqlValue::from(conv_id),
                SqlValue::from(user_id),
            ]);
        }
        updates.push(parameters);
    }
    let mut counts = Vec::new();
    for ((table, id_column, _), parameters) in moved.iter().zip(updates) {
        if parameters.is_empty() {
            counts.push(0);
            continue;
        }
        counts.push(session.execute_many_exact(
            &format!("UPDATE {table} SET settings_json=? WHERE {id_column}=? AND user_id=?"),
            &parameters,
        )?);
    }
    Ok((counts[0], counts[1]))
}

pub fn project_relink(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let old_path = required_text(payload, "old_path", 4096)?;
    let new_path = required_text(payload, "new_path", 4096)?;
    if old_path == new_path {
        return fail("database_protocol_error", "old_path and new_path must differ");
    }
    let old_key = format!("{user_id}:{old_path}");
    let new_key = format!("{user_id}:{new_path}");
    let locks: BTreeSet<&String> = [&old_key, &new_key].into_iter().collect();
    for key in locks {
        session.lock_key(RECENT_PROJECT_NAMESPACE, key);
    }
    let current = record_get_value(session, RECENT_PROJECT_NAMESPACE, &old_key)?;
    let Value::Object(value) = get(&current, "value") else {
        return fail("database_not_found", "Old path is not in recent projects");
    };
    let mut new_value = value.clone();
    new_value.insert("path".into(), Value::from(new_path.as_str()));
    let existing = record_get_value(session, RECENT_PROJECT_NAMESPACE, &new_key)?;
    if let Value::Object(existing_value) = get(&existing, "value") {
        let count =
            int_or_zero_strict(map_get(&new_value, "count"))? + int_or_zero_strict(map_get(existing_value, "count"))?;
        let last_used = int_or_zero_strict(map_get(&new_value, "last_used"))?
            .max(int_or_zero_strict(map_get(existing_value, "last_used"))?);
        new_value.insert("count".into(), Value::from(count));
        new_value.insert("last_used".into(), Value::from(last_used));
    }
    record_put(session, &record_payload(RECENT_PROJECT_NAMESPACE, &new_key, Some(Value::Object(new_value.clone()))))?;
    record_delete(session, &record_payload(RECENT_PROJECT_NAMESPACE, &old_key, None))?;
    let brain_moved = project_brain_relink_scope(session, user_id, &old_path, &new_path)?;
    let (conversations_moved, trashed_moved) = conversation_project_relink(session, user_id, &old_path, &new_path)?;
    Ok(json!({
        "project": Value::Object(new_value),
        "projectBrainMoved": brain_moved,
        "conversationsMoved": conversations_moved,
        "trashedConversationsMoved": trashed_moved,
    }))
}

// ── private port of operations_pkg/_project_brain.py relink; shared owner: project_brain ──

const PROJECT_BRAIN_VERSION: i64 = 1;
const PROJECT_BRAIN_STREAM_KIND: &str = "project_brain";
const EVENT_CHECKPOINT_TAIL: i64 = 600;
const WORK_TERMINAL: [&str; 4] = ["completed", "failed", "cancelled", "interrupted"];

/// `_TRAILING_SEPARATORS.sub('', key)` with Python `$` (end, or before a
/// final newline).
fn strip_trailing_separators(key: &str) -> String {
    let is_separator = |c: char| c == '/' || c == '\\';
    if let Some(body) = key.strip_suffix('\n') {
        if body.ends_with(is_separator) {
            return format!("{}\n", body.trim_end_matches(is_separator));
        }
        return key.to_owned();
    }
    key.trim_end_matches(is_separator).to_owned()
}

fn int_field(value: &Map<String, Value>, field: &str) -> OpResult<i64> {
    int_or_zero_strict(map_get(value, field))
}

fn is_terminal(status: &Value) -> bool {
    WORK_TERMINAL.iter().any(|candidate| matches!(status, Value::String(text) if text == candidate))
}

/// private port of `_project_brain.py::_save_projection`; shared owner: project_brain
fn save_projection(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    projection: &Map<String, Value>,
    now_ms: i64,
) -> OpResult<()> {
    let head = int_field(projection, "headSequence")?;
    let checkpoint = int_field(projection, "checkpointSequence")?;
    session.execute(
        "INSERT INTO storage_project_brain_projects(\
         owner_user_id,project_key,head_sequence,checkpoint_sequence,\
         projection_json,updated_at_ms) VALUES (?,?,?,?,?,?) \
         ON CONFLICT(owner_user_id,project_key) DO UPDATE SET \
         head_sequence=excluded.head_sequence,\
         checkpoint_sequence=excluded.checkpoint_sequence,\
         projection_json=excluded.projection_json,\
         updated_at_ms=excluded.updated_at_ms",
        &params![owner_user_id, project_key, head, checkpoint, dump_param(&Value::Object(projection.clone()))?, now_ms],
    )?;
    Ok(())
}

/// private port of `_project_brain.py::_write_projection_checkpoint`; shared owner: project_brain
fn write_projection_checkpoint(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    projection: &Map<String, Value>,
    timestamp: i64,
) -> OpResult<()> {
    let sequence = int_field(projection, "headSequence")? + 1;
    let mut snapshot = projection.clone();
    snapshot.insert("headSequence".into(), Value::from(sequence));
    snapshot.insert("checkpointSequence".into(), Value::from(sequence));
    let event = json!({
        "ownerUserId": owner_user_id,
        "projectKey": project_key,
        "projectSequence": sequence,
        "kind": "projection_checkpoint",
        "timestamp": timestamp,
        "payload": {"snapshot": Value::Object(snapshot.clone())},
    });
    let task_id = format!("project-brain:{owner_user_id}:{project_key}");
    session.execute(
        "INSERT INTO storage_events(\
         task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,\
         project_key,project_sequence,event_json,created_at_ms) \
         VALUES (?,?,?,?,?,?,?,?,?,?)",
        &params![
            &task_id,
            sequence,
            PROJECT_BRAIN_STREAM_KIND,
            "project_brain",
            "projection_checkpoint",
            owner_user_id,
            project_key,
            sequence,
            dump(&event)?,
            timestamp
        ],
    )?;
    save_projection(session, owner_user_id, project_key, &snapshot, timestamp)?;
    session.execute(
        "DELETE FROM storage_events WHERE owner_user_id=? AND project_key=? \
         AND project_sequence>0 AND project_sequence<=?",
        &params![owner_user_id, project_key, sequence - EVENT_CHECKPOINT_TAIL],
    )?;
    Ok(())
}

/// private port of `_project_brain.py::_ordered_work_items`; shared owner: project_brain
fn ordered_work_items(items: Vec<Map<String, Value>>) -> OpResult<Vec<Value>> {
    let mut active = Vec::new();
    let mut terminal = Vec::new();
    for item in items {
        let status = map_get(&item, "status");
        if matches!(status, Value::String(text) if text == "active") {
            active.push((int_field(&item, "startedAt")?, item));
        } else if is_terminal(status) {
            terminal.push((int_field(&item, "finishedAt")?, item));
        }
    }
    active.sort_by_key(|entry| entry.0);
    terminal.sort_by_key(|entry| entry.0);
    Ok(active.into_iter().chain(terminal).map(|(_, item)| Value::Object(item)).collect())
}

fn rows_of(value: &Value) -> Vec<Value> {
    if !json::truthy(value) {
        return Vec::new();
    }
    match value {
        Value::Array(items) => items.clone(),
        Value::Object(map) => map.keys().map(|key| Value::from(key.as_str())).collect(),
        Value::String(text) => text.chars().map(|c| Value::from(c.to_string())).collect(),
        _ => Vec::new(),
    }
}

/// `_merge_relinked_immutable_rows`.
fn merge_immutable_rows(
    destination: &Value,
    source: &Value,
    identity: &dyn Fn(&Map<String, Value>) -> OpResult<String>,
    field: &str,
) -> OpResult<Vec<Value>> {
    let mut rows: Vec<(String, Map<String, Value>)> = Vec::new();
    for values in [destination, source] {
        for raw in rows_of(values) {
            let Value::Object(item) = raw else { continue };
            let key = identity(&item)?;
            match rows.iter_mut().find(|(known, _)| *known == key) {
                Some(entry) => {
                    if !py_eq(&Value::Object(entry.1.clone()), &Value::Object(item.clone())) {
                        return fail(
                            "database_conflict",
                            format!("Project Brain {field} identity conflict during relink"),
                        );
                    }
                    entry.1 = item;
                }
                None => rows.push((key, item)),
            }
        }
    }
    Ok(rows.into_iter().map(|(_, item)| Value::Object(item)).collect())
}

/// private port of `_project_brain.py::_merge_relinked_projections`; shared owner: project_brain
fn merge_relinked_projections(
    owner_user_id: i64,
    project_key: &str,
    destination: &Map<String, Value>,
    source: &Map<String, Value>,
) -> OpResult<Map<String, Value>> {
    let mut merged = source.clone();
    for (key, value) in destination {
        merged.insert(key.clone(), value.clone());
    }
    merged.insert("version".into(), Value::from(PROJECT_BRAIN_VERSION));
    merged.insert("ownerUserId".into(), Value::from(owner_user_id));
    merged.insert("projectKey".into(), Value::from(project_key));
    merged.insert("checkpointSequence".into(), Value::from(0));

    let mut work_by_id: Vec<(String, Map<String, Value>)> = Vec::new();
    for projection in [destination, source] {
        for raw in rows_of(map_get(projection, "workItems")) {
            let Value::Object(item) = raw else { continue };
            let work_id = str_or_empty(map_get(&item, "id"));
            if work_id.is_empty() {
                continue;
            }
            let Some(slot) = work_by_id.iter_mut().find(|(known, _)| *known == work_id) else {
                work_by_id.push((work_id, item));
                continue;
            };
            let previous = slot.1.clone();
            let rank = |value: &Map<String, Value>| -> OpResult<(i64, i64, bool)> {
                Ok((
                    int_field(value, "finishedAt")?,
                    int_field(value, "startedAt")?,
                    is_terminal(map_get(value, "status")),
                ))
            };
            let authoritative = if rank(&item)? > rank(&previous)? { &item } else { &previous };
            let mut combined = authoritative.clone();
            let mut paths: Vec<Value> = Vec::new();
            for path in
                rows_of(map_get(&previous, "changedPaths")).into_iter().chain(rows_of(map_get(&item, "changedPaths")))
            {
                super::py::require_hashable(&path)?;
                if !paths.iter().any(|known| py_eq(known, &path)) {
                    paths.push(path);
                }
            }
            combined.insert("changedPaths".into(), Value::Array(paths));
            let mut artifacts: Vec<(String, Map<String, Value>)> = Vec::new();
            for artifact in
                rows_of(map_get(&previous, "artifacts")).into_iter().chain(rows_of(map_get(&item, "artifacts")))
            {
                let Value::Object(artifact) = artifact else { continue };
                let id = map_get(&artifact, "id");
                let key = str_or_empty(if json::truthy(id) { id } else { map_get(&artifact, "path") });
                if key.is_empty() {
                    continue;
                }
                match artifacts.iter_mut().find(|(known, _)| *known == key) {
                    Some(entry) => entry.1 = artifact,
                    None => artifacts.push((key, artifact)),
                }
            }
            combined.insert(
                "artifacts".into(),
                Value::Array(artifacts.into_iter().map(|(_, a)| Value::Object(a)).collect()),
            );
            slot.1 = combined;
        }
    }
    merged.insert(
        "workItems".into(),
        Value::Array(ordered_work_items(work_by_id.into_iter().map(|(_, i)| i).collect())?),
    );

    type NarrativeKey = (String, String, i64, String, String);
    let mut narratives: Vec<(NarrativeKey, Map<String, Value>)> = Vec::new();
    for projection in [destination, source] {
        for raw in rows_of(map_get(projection, "narratives")) {
            let Value::Object(item) = raw else { continue };
            let key = (
                str_or_empty(map_get(&item, "kind")),
                str_or_empty(map_get(&item, "text")),
                int_field(&item, "timestamp")?,
                str_or_empty(map_get(&item, "workId")),
                str_or_empty(map_get(&item, "conversationId")),
            );
            match narratives.iter_mut().find(|(known, _)| *known == key) {
                Some(entry) => entry.1 = item,
                None => narratives.push((key, item)),
            }
        }
    }
    let mut narrative_rows = Vec::new();
    for (_, item) in narratives {
        narrative_rows.push(((int_field(&item, "timestamp")?, int_field(&item, "sequence")?), item));
    }
    narrative_rows.sort_by_key(|entry| entry.0);
    let combined_head =
        (int_field(destination, "headSequence")? + int_field(source, "headSequence")?).max(narrative_rows.len() as i64);
    let first_sequence = combined_head - narrative_rows.len() as i64 + 1;
    let narrative_rows: Vec<Value> = narrative_rows
        .into_iter()
        .enumerate()
        .map(|(offset, (_, mut item))| {
            item.insert("sequence".into(), Value::from(first_sequence + offset as i64));
            Value::Object(item)
        })
        .collect();
    merged.insert("narratives".into(), Value::Array(narrative_rows));
    merged.insert("headSequence".into(), Value::from(combined_head));

    let checker_identity = |item: &Map<String, Value>| -> OpResult<String> {
        Ok(format!("{}\u{0}{}", str_or_empty(map_get(item, "checkerId")), int_field(item, "version")?))
    };
    merged.insert(
        "checkers".into(),
        Value::Array(merge_immutable_rows(
            map_get(destination, "checkers"),
            map_get(source, "checkers"),
            &checker_identity,
            "checkers",
        )?),
    );
    let charter = |projection: &Map<String, Value>| -> OpResult<Map<String, Value>> {
        let value = map_get(projection, "charter");
        if !json::truthy(value) {
            return Ok(Map::new());
        }
        match value {
            Value::Object(map) => Ok(map.clone()),
            _ => internal("TypeError: charter is not a mapping"),
        }
    };
    let destination_charter = charter(destination)?;
    let source_charter = charter(source)?;
    let decision_identity =
        |item: &Map<String, Value>| -> OpResult<String> { Ok(str_or_empty(map_get(item, "decisionId"))) };
    let decisions = merge_immutable_rows(
        map_get(&destination_charter, "decisions"),
        map_get(&source_charter, "decisions"),
        &decision_identity,
        "charter decisions",
    )?;
    let mut merged_charter = source_charter.clone();
    for (key, value) in &destination_charter {
        merged_charter.insert(key.clone(), value.clone());
    }
    merged_charter.insert("decisions".into(), Value::Array(decisions));
    merged.insert("charter".into(), Value::Object(merged_charter));

    let mut watch_by_id: Vec<(String, Map<String, Value>)> = Vec::new();
    for projection in [destination, source] {
        for raw in rows_of(map_get(projection, "watch")) {
            let Value::Object(item) = raw else { continue };
            let item_id = str_or_empty(map_get(&item, "id"));
            match watch_by_id.iter_mut().find(|(known, _)| *known == item_id) {
                Some(entry) => {
                    if int_field(&item, "updatedAt")? >= int_field(&entry.1, "updatedAt")? {
                        entry.1 = item;
                    }
                }
                None => watch_by_id.push((item_id, item)),
            }
        }
    }
    let mut watch = Vec::new();
    for (_, item) in watch_by_id {
        watch.push((int_field(&item, "updatedAt")?, item));
    }
    watch.sort_by_key(|entry| entry.0);
    merged.insert("watch".into(), Value::Array(watch.into_iter().map(|(_, item)| Value::Object(item)).collect()));
    merged.insert("cursors".into(), Value::Object(Map::new()));
    Ok(merged)
}

/// private port of `_project_brain.py::_project_brain_relink_scope`; shared owner: project_brain
fn project_brain_relink_scope(
    session: &Session<'_>,
    owner_user_id: i64,
    old_project_key: &str,
    new_project_key: &str,
) -> OpResult<bool> {
    let old_project_key = strip_trailing_separators(old_project_key);
    let new_project_key = strip_trailing_separators(new_project_key);
    let mut keys = [old_project_key.clone(), new_project_key.clone()];
    keys.sort();
    for key in &keys {
        session.lock_key("project_brain.project", &format!("{owner_user_id}:{key}"));
    }
    let select = "SELECT projection_json,updated_at_ms \
                  FROM storage_project_brain_projects \
                  WHERE owner_user_id=? AND project_key=?";
    let Some(old_row) = session.fetch_one(select, &params![owner_user_id, &old_project_key])? else {
        return Ok(false);
    };
    let new_row = session.fetch_one(select, &params![owner_user_id, &new_project_key])?;
    let Value::Object(mut document) = load(old_row.get("projection_json"))? else {
        return fail("database_integrity", "Project Brain projection is invalid");
    };
    document.insert("projectKey".into(), Value::from(new_project_key.as_str()));
    if let Some(new_row) = new_row {
        let Value::Object(destination) = load(new_row.get("projection_json"))? else {
            return fail("database_integrity", "Project Brain projection is invalid");
        };
        let merged = merge_relinked_projections(owner_user_id, &new_project_key, &destination, &document)?;
        let timestamp = old_row.int("updated_at_ms")?.max(new_row.int("updated_at_ms")?).max(now_ms());
        write_projection_checkpoint(session, owner_user_id, &new_project_key, &merged, timestamp)?;
        session.execute(
            "DELETE FROM storage_events WHERE owner_user_id=? AND project_key=?",
            &params![owner_user_id, &old_project_key],
        )?;
        session.execute(
            "DELETE FROM storage_project_brain_projects WHERE owner_user_id=? AND project_key=?",
            &params![owner_user_id, &old_project_key],
        )?;
        return Ok(true);
    }
    let new_task_id = format!("project-brain:{owner_user_id}:{new_project_key}");
    let rows = session.fetch_all(
        "SELECT sequence,stream_kind,event_type,event_kind,event_json,created_at_ms \
         FROM storage_events WHERE owner_user_id=? AND project_key=? \
         ORDER BY project_sequence",
        &params![owner_user_id, &old_project_key],
    )?;
    session.execute(
        "DELETE FROM storage_events WHERE owner_user_id=? AND project_key=?",
        &params![owner_user_id, &old_project_key],
    )?;
    for row in rows {
        let Value::Object(mut event) = load(row.get("event_json"))? else {
            return fail("database_integrity", "Project Brain event is invalid");
        };
        event.insert("projectKey".into(), Value::from(new_project_key.as_str()));
        if super::py::eq_str(map_get(&event, "kind"), "projection_checkpoint") {
            let payload = map_get(&event, "payload").clone();
            let snapshot = match &payload {
                Value::Object(map) => map_get(map, "snapshot").clone(),
                _ => Value::Null,
            };
            let Value::Object(mut snapshot) = snapshot else {
                return fail("database_integrity", "Project Brain checkpoint is invalid");
            };
            if int_field(&snapshot, "ownerUserId")? != owner_user_id
                || str_or_empty(map_get(&snapshot, "projectKey")) != old_project_key
            {
                return fail("database_integrity", "Project Brain checkpoint ownership mismatch");
            }
            snapshot.insert("projectKey".into(), Value::from(new_project_key.as_str()));
            let mut payload = payload.as_object().expect("checkpoint payload").clone();
            payload.insert("snapshot".into(), Value::Object(snapshot));
            event.insert("payload".into(), Value::Object(payload));
        }
        let project_sequence = map_get(&event, "projectSequence");
        let sequence =
            if json::truthy(project_sequence) { int_or_zero_strict(project_sequence)? } else { row.int("sequence")? };
        session.execute(
            "INSERT INTO storage_events(\
             task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,\
             project_key,project_sequence,event_json,created_at_ms) \
             VALUES (?,?,?,?,?,?,?,?,?,?)",
            &params![
                &new_task_id,
                row.int("sequence")?,
                row.get("stream_kind"),
                row.get("event_type"),
                row.get("event_kind"),
                owner_user_id,
                &new_project_key,
                sequence,
                dump(&Value::Object(event))?,
                row.int("created_at_ms")?
            ],
        )?;
    }
    session.execute(
        "DELETE FROM storage_project_brain_projects WHERE owner_user_id=? AND project_key=?",
        &params![owner_user_id, &old_project_key],
    )?;
    save_projection(session, owner_user_id, &new_project_key, &document, old_row.int("updated_at_ms")?)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trailing_separators_follow_python_dollar() {
        assert_eq!(strip_trailing_separators("/a/b//"), "/a/b");
        assert_eq!(strip_trailing_separators("/a\\"), "/a");
        assert_eq!(strip_trailing_separators("/a/\n"), "/a\n");
        assert_eq!(strip_trailing_separators("/a"), "/a");
    }
}
