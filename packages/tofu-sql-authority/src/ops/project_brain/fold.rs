//! Project Brain projection load/save, event append and fold
//! (`operations_pkg/_project_brain.py`: `_load_projection` .. `_command_result`).

use serde_json::{json, Map, Value};

use super::py::{self, field, get, int0, text};
use crate::codec;
use crate::error::{fail, internal, OpResult, StorageError};
use crate::json::{python_str, truthy};
use crate::params;
use crate::session::Session;

pub const PROJECT_BRAIN_VERSION: i64 = 1;
pub const NARRATIVE_TEXT_LIMIT_BYTES: usize = 720;
const EVENT_CHECKPOINT_THRESHOLD: i64 = 1200;
const EVENT_CHECKPOINT_TAIL: i64 = 600;
/// `lib/task_replay/task_event_contract.py::PROJECT_BRAIN_STREAM_KIND`.
const PROJECT_BRAIN_STREAM_KIND: &str = "project_brain";
pub const WORK_TERMINAL: [&str; 4] = ["completed", "failed", "cancelled", "interrupted"];

const INSERT_EVENT: &str = "INSERT INTO storage_events(task_id,sequence,stream_kind,event_type,event_kind,owner_user_id,project_key,project_sequence,event_json,created_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)";

pub type Projection = Map<String, Value>;

pub fn protocol<T>(message: &str) -> OpResult<T> {
    fail("database_protocol_error", message)
}

pub fn integrity<T>(message: &str) -> OpResult<T> {
    fail("database_integrity", message)
}

pub fn is_terminal(value: &Value) -> bool {
    value.as_str().is_some_and(|status| WORK_TERMINAL.contains(&status))
}

/// `_TRAILING_SEPARATORS.sub('', text)` for `[/\\]+$` (`$` also matches
/// before one final newline).
pub fn strip_trailing_separators(text: &str) -> String {
    let separators: &[char] = &['/', '\\'];
    if let Some(body) = text.strip_suffix('\n') {
        let trimmed = body.trim_end_matches(separators);
        if trimmed.len() != body.len() {
            return format!("{trimmed}\n");
        }
        return text.to_owned();
    }
    text.trim_end_matches(separators).to_owned()
}

/// `_bounded_utf8(text, max_bytes)` for a str argument.
pub fn bounded_utf8(value: &str, max_bytes: usize) -> String {
    let stripped = py::strip(value);
    if stripped.len() <= max_bytes {
        return stripped.to_owned();
    }
    let mut end = max_bytes;
    while !stripped.is_char_boundary(end) {
        end -= 1;
    }
    py::rstrip(&stripped[..end]).to_owned()
}

/// `str(a or b or ... or default)`.
pub fn first_text(values: &[&Value], default: &str) -> String {
    values.iter().find(|value| truthy(value)).map(|value| python_str(value)).unwrap_or_else(|| default.to_owned())
}

pub fn empty_projection(owner_user_id: i64, project_key: &str) -> Projection {
    let Value::Object(map) = json!({
        "version": PROJECT_BRAIN_VERSION,
        "ownerUserId": owner_user_id,
        "projectKey": project_key,
        "headSequence": 0,
        "checkpointSequence": 0,
        "workItems": [],
        "narratives": [],
        "charter": {"decisions": []},
        "checkers": [],
        "watch": [],
        "cursors": {},
    }) else {
        unreachable!()
    };
    map
}

/// `_load_projection`.
pub fn load_projection(session: &Session<'_>, owner_user_id: i64, project_key: &str) -> OpResult<Projection> {
    let row = session.fetch_one(
        "SELECT projection_json FROM storage_project_brain_projects WHERE owner_user_id=? AND project_key=?",
        &params![owner_user_id, project_key],
    )?;
    let Some(row) = row else { return Ok(empty_projection(owner_user_id, project_key)) };
    let Value::Object(mut projection) = codec::load(row.require("projection_json")?)? else {
        return integrity("Project Brain projection is invalid");
    };
    if int0(field(&projection, "version"))? != PROJECT_BRAIN_VERSION {
        return integrity("Unsupported Project Brain projection version");
    }
    if int0(field(&projection, "ownerUserId"))? != owner_user_id
        || text(field(&projection, "projectKey")) != project_key
    {
        return integrity("Project Brain projection ownership mismatch");
    }
    projection.shift_remove("attention");
    Ok(projection)
}

/// `_save_projection`.
pub fn save_projection(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    projection: &Projection,
    now_ms: i64,
) -> OpResult<()> {
    let head = int0(field(projection, "headSequence"))?;
    let checkpoint = int0(field(projection, "checkpointSequence"))?;
    session.execute(
        "INSERT INTO storage_project_brain_projects(owner_user_id,project_key,head_sequence,checkpoint_sequence,projection_json,updated_at_ms) VALUES (?,?,?,?,?,?) ON CONFLICT(owner_user_id,project_key) DO UPDATE SET head_sequence=excluded.head_sequence,checkpoint_sequence=excluded.checkpoint_sequence,projection_json=excluded.projection_json,updated_at_ms=excluded.updated_at_ms",
        &params![
            owner_user_id,
            project_key,
            head,
            checkpoint,
            codec::dump_param(&Value::Object(projection.clone()))?,
            now_ms
        ],
    )?;
    Ok(())
}

const PUBLIC_WORK_FIELDS: [&str; 11] = [
    "id",
    "taskId",
    "conversationId",
    "title",
    "trigger",
    "status",
    "changedPaths",
    "artifacts",
    "resultSummary",
    "startedAt",
    "finishedAt",
];

/// `_public_work_item`.
pub fn public_work_item(item: &Value) -> Value {
    let mut map = Map::new();
    for key in PUBLIC_WORK_FIELDS {
        map.insert(key.to_owned(), get(item, key).clone());
    }
    Value::Object(map)
}

/// `_public_projection`.
pub fn public_projection(projection: &Projection) -> OpResult<Value> {
    let charter = field(projection, "charter");
    let charter = if truthy(charter) { Value::Object(py::dict(charter)?) } else { json!({"decisions": []}) };
    Ok(json!({
        "version": py::int_or(field(projection, "version"), PROJECT_BRAIN_VERSION)?,
        "ownerUserId": int0(field(projection, "ownerUserId"))?,
        "projectKey": text(field(projection, "projectKey")),
        "headSequence": int0(field(projection, "headSequence"))?,
        "checkpointSequence": int0(field(projection, "checkpointSequence"))?,
        "workItems": py::mappings(field(projection, "workItems"))?.iter().map(public_work_item).collect::<Vec<_>>(),
        "narratives": py::mappings(field(projection, "narratives"))?,
        "charter": charter,
        "checkers": py::mappings(field(projection, "checkers"))?,
        "watch": py::mappings(field(projection, "watch"))?,
    }))
}

/// `_interruption_supersedes`.
pub fn interruption_supersedes(current: &Value, status: &str) -> bool {
    py::is_str(current, "cancelled") && status == "interrupted"
}

fn sorted_by(items: Vec<Value>, key: &str) -> OpResult<Vec<Value>> {
    let mut keyed = Vec::with_capacity(items.len());
    for item in items {
        let sort_key = int0(py::attr(&item, key)?)?;
        keyed.push((sort_key, item));
    }
    keyed.sort_by_key(|(sort_key, _)| *sort_key);
    Ok(keyed.into_iter().map(|(_, item)| item).collect())
}

/// `_ordered_work_items`.
pub fn ordered_work_items(items: Vec<Value>) -> OpResult<Vec<Value>> {
    let mut active = Vec::new();
    for item in &items {
        if py::is_str(py::attr(item, "status")?, "active") {
            active.push(item.clone());
        }
    }
    let mut terminal = Vec::new();
    for item in &items {
        if is_terminal(py::attr(item, "status")?) {
            terminal.push(item.clone());
        }
    }
    let mut ordered = sorted_by(active, "startedAt")?;
    ordered.extend(sorted_by(terminal, "finishedAt")?);
    Ok(ordered)
}

/// `_work_index`; `None` is -1.
pub fn work_index(projection: &Projection, work_id: &str) -> OpResult<Option<usize>> {
    for (index, item) in py::items(field(projection, "workItems"))?.iter().enumerate() {
        if item.is_object() && text(get(item, "id")) == work_id {
            return Ok(Some(index));
        }
    }
    Ok(None)
}

/// `projection['workItems'][index]` (an index from `work_index` names a list item).
pub fn work_item(projection: &Projection, index: usize) -> Value {
    projection["workItems"][index].clone()
}

fn work_item_map(projection: &Projection, index: usize) -> Map<String, Value> {
    work_item(projection, index).as_object().cloned().unwrap_or_default()
}

fn set_work_item(projection: &mut Projection, index: usize, item: Map<String, Value>) {
    if let Some(Value::Array(items)) = projection.get_mut("workItems") {
        items[index] = Value::Object(item);
    }
}

/// `_append_narrative`.
#[allow(clippy::too_many_arguments)]
pub fn append_narrative(
    projection: &mut Projection,
    sequence: i64,
    kind: &str,
    text_value: &str,
    timestamp: i64,
    work_id: &str,
    conversation_id: &str,
) -> OpResult<()> {
    let kind = py::prefix(kind, 64);
    let mut entry = Map::new();
    entry.insert("sequence".into(), Value::from(sequence));
    entry.insert("kind".into(), Value::String(if kind.is_empty() { "note".into() } else { kind }));
    entry.insert("text".into(), Value::String(bounded_utf8(text_value, NARRATIVE_TEXT_LIMIT_BYTES)));
    entry.insert("timestamp".into(), Value::from(timestamp));
    if !work_id.is_empty() {
        entry.insert("workId".into(), Value::String(work_id.to_owned()));
    }
    if !conversation_id.is_empty() {
        entry.insert("conversationId".into(), Value::String(conversation_id.to_owned()));
    }
    let mut narratives = py::mappings(field(projection, "narratives"))?;
    narratives.push(Value::Object(entry));
    projection.insert("narratives".into(), Value::Array(narratives));
    Ok(())
}

/// `list(dict.fromkeys(values))`.
fn unique(values: Vec<Value>) -> OpResult<Vec<Value>> {
    let mut result: Vec<Value> = Vec::new();
    for value in values {
        if value.is_array() || value.is_object() {
            return internal("TypeError: unhashable type");
        }
        if !result.iter().any(|seen| codec::py_eq(seen, &value)) {
            result.push(value);
        }
    }
    Ok(result)
}

/// `_fold_event`.
pub fn fold_event(projection: &mut Projection, event: &Map<String, Value>) -> OpResult<()> {
    let kind = text(field(event, "kind"));
    let payload = match field(event, "payload") {
        Value::Object(map) => map.clone(),
        _ => Map::new(),
    };
    let sequence = int0(field(event, "projectSequence"))?;
    let timestamp = int0(field(event, "timestamp"))?;

    if kind == "work_started" {
        let item = py::dict(field(&payload, "workItem"))?;
        if text(field(&item, "status")) != "active" {
            return protocol("New Project work must be active");
        }
        let work_id = text(field(&item, "id"));
        if work_id.is_empty() {
            return protocol("Project work id is required");
        }
        if work_index(projection, &work_id)?.is_none() {
            let mut items = py::unpack(projection.get("workItems"))?;
            items.push(Value::Object(item));
            let ordered = ordered_work_items(items)?;
            projection.insert("workItems".into(), Value::Array(ordered));
        }
    } else if kind == "work_title_refined" {
        let work_id = text(field(&payload, "workId"));
        if let Some(index) = work_index(projection, &work_id)? {
            let mut item = work_item_map(projection, index);
            if py::is_str(field(&item, "status"), "active") && !truthy(field(&item, "_titleRefined")) {
                let priority = int0(field(&payload, "titlePriority"))?;
                if priority > int0(field(&item, "_titlePriority"))? {
                    let title = text(field(&payload, "title"));
                    item.insert("title".into(), Value::String(py::prefix(py::strip(&title), 500)));
                    item.insert("_titlePriority".into(), Value::from(priority));
                    item.insert("_titleRefined".into(), Value::Bool(true));
                    set_work_item(projection, index, item);
                }
            }
        }
    } else if kind == "work_changed" {
        let work_id = text(field(&payload, "workId"));
        if let Some(index) = work_index(projection, &work_id)? {
            let mut item = work_item_map(projection, index);
            if py::is_str(field(&item, "status"), "active") {
                let mut paths = Vec::new();
                for path in py::items(field(&payload, "changedPaths"))? {
                    let rendered = python_str(&path);
                    let stripped = py::strip(&rendered);
                    if !stripped.is_empty() {
                        paths.push(Value::String(py::prefix(stripped, 4096)));
                    }
                }
                let artifacts = py::mappings(field(&payload, "artifacts"))?;
                let mut changed = py::items(field(&item, "changedPaths"))?;
                changed.extend(paths);
                item.insert("changedPaths".into(), Value::Array(unique(changed)?));
                let mut artifact_by_id = Map::new();
                for (position, value) in py::items(field(&item, "artifacts"))?.into_iter().enumerate() {
                    if !value.is_object() {
                        continue;
                    }
                    let key = first_text(&[get(&value, "id"), get(&value, "path")], &position.to_string());
                    artifact_by_id.insert(key, value);
                }
                for artifact in artifacts {
                    let key = first_text(&[get(&artifact, "id"), get(&artifact, "path")], "");
                    if !key.is_empty() {
                        artifact_by_id.insert(key, artifact);
                    }
                }
                item.insert("artifacts".into(), Value::Array(artifact_by_id.into_iter().map(|(_, v)| v).collect()));
                set_work_item(projection, index, item);
            }
        }
    } else if kind == "work_finished" {
        let work_id = text(field(&payload, "workId"));
        if let Some(index) = work_index(projection, &work_id)? {
            let mut item = work_item_map(projection, index);
            let status = text(field(&payload, "status"));
            if interruption_supersedes(field(&item, "status"), &status) {
                item.insert("status".into(), Value::String(status));
                set_work_item(projection, index, item);
            } else if py::is_str(field(&item, "status"), "active") && WORK_TERMINAL.contains(&status.as_str()) {
                let summary = text(field(&payload, "resultSummary"));
                let summary = py::prefix(py::strip(&summary), 4000);
                item.insert("status".into(), Value::String(status.clone()));
                item.insert("resultSummary".into(), Value::String(summary.clone()));
                item.insert("finishedAt".into(), Value::from(timestamp));
                let has_output = truthy(field(&item, "changedPaths")) || truthy(field(&item, "artifacts"));
                let title = field(&item, "title").clone();
                let conversation_id = text(field(&item, "conversationId"));
                set_work_item(projection, index, item);
                let items = py::unpack(projection.get("workItems"))?;
                let ordered = ordered_work_items(items)?;
                projection.insert("workItems".into(), Value::Array(ordered));
                if has_output || matches!(status.as_str(), "failed" | "cancelled" | "interrupted") {
                    let summary = if summary.is_empty() {
                        format!("{}: {status}", first_text(&[&title], &work_id))
                    } else {
                        summary
                    };
                    append_narrative(
                        projection,
                        sequence,
                        "work_result",
                        &summary,
                        timestamp,
                        &work_id,
                        &conversation_id,
                    )?;
                }
            }
        }
    } else if kind == "narrative_added" {
        append_narrative(
            projection,
            sequence,
            &py::text_or(field(&payload, "narrativeKind"), "note"),
            &text(field(&payload, "text")),
            timestamp,
            &text(field(&payload, "workId")),
            &text(field(&payload, "conversationId")),
        )?;
    } else if kind == "checker_registered" {
        let definition = py::dict(field(&payload, "definition"))?;
        let mut checkers = py::mappings(field(projection, "checkers"))?;
        let key = (text(field(&definition, "checkerId")), int0(field(&definition, "version"))?);
        let mut found = false;
        for item in &checkers {
            let candidate = (text(get(item, "checkerId")), int0(get(item, "version"))?);
            if candidate == key {
                found = true;
                break;
            }
        }
        if !found {
            checkers.push(Value::Object(definition));
        }
        projection.insert("checkers".into(), Value::Array(checkers));
    } else if kind == "decision_promoted" {
        let decision = py::dict(field(&payload, "decision"))?;
        let mut charter = py::dict(field(projection, "charter"))?;
        let mut decisions = py::mappings(field(&charter, "decisions"))?;
        let decision_id = text(field(&decision, "decisionId"));
        if !decisions.iter().any(|item| text(get(item, "decisionId")) == decision_id) {
            decisions.push(Value::Object(decision.clone()));
        }
        charter.insert("decisions".into(), Value::Array(decisions));
        projection.insert("charter".into(), Value::Object(charter));
        append_narrative(
            projection,
            sequence,
            "decision",
            &text(field(&decision, "text")),
            timestamp,
            "",
            &text(field(&decision, "sourceConversationId")),
        )?;
    } else if kind == "checker_result" {
        let result = py::dict(field(&payload, "result"))?;
        let checker_ref = py::dict(field(&result, "checkerRef"))?;
        let decision_id = text(field(&payload, "decisionId"));
        let mut charter = py::dict(field(projection, "charter"))?;
        let mut decisions = py::mappings(field(&charter, "decisions"))?;
        for decision in decisions.iter_mut() {
            let reference = get(decision, "checkerRef");
            let reference = if truthy(reference) { reference.clone() } else { json!({}) };
            let same_checker = text(py::attr(&reference, "id")?) == text(field(&checker_ref, "id"))
                && int0(py::attr(&reference, "version")?)? == int0(field(&checker_ref, "version"))?;
            if same_checker && (decision_id.is_empty() || text(get(decision, "decisionId")) == decision_id) {
                if let Value::Object(map) = decision {
                    map.insert("latestVerification".into(), Value::Object(result.clone()));
                }
            }
        }
        charter.insert("decisions".into(), Value::Array(decisions));
        projection.insert("charter".into(), Value::Object(charter));
        if !truthy(field(&result, "ok")) {
            let label = first_text(&[field(&result, "label"), field(&checker_ref, "id")], "Checker");
            let reason = py::text_or(field(&result, "summary"), "checker failed");
            let message = format!("{label}: {reason}");
            let work_id = text(field(&result, "workId"));
            append_narrative(projection, sequence, "checker_failed", &message, timestamp, &work_id, "")?;
        }
    } else if kind.starts_with("watch_") {
        let mut watch = py::mappings(field(projection, "watch"))?;
        let item = py::dict(field(&payload, "item"))?;
        let item_id = first_text(&[field(&item, "id"), field(&payload, "itemId")], "");
        let previous = watch.iter().find(|row| text(get(row, "id")) == item_id).cloned().unwrap_or_else(|| json!({}));
        watch.retain(|row| text(get(row, "id")) != item_id);
        let (narrative_text, source_conversation_id) = if kind == "watch_deleted" {
            (
                format!("Watch removed: {}", first_text(&[get(&previous, "text")], &item_id)),
                text(get(&previous, "sourceConversationId")),
            )
        } else {
            let action = if py::is_str(field(&item, "status"), "resolved") {
                "resolved"
            } else if kind == "watch_added" {
                "added"
            } else {
                "updated"
            };
            let rendered = format!("Watch {action}: {}", first_text(&[field(&item, "text")], &item_id));
            let source = text(field(&item, "sourceConversationId"));
            watch.push(Value::Object(item));
            (rendered, source)
        };
        projection.insert("watch".into(), Value::Array(watch));
        append_narrative(projection, sequence, &kind, &narrative_text, timestamp, "", &source_conversation_id)?;
    } else if kind == "cursor_initialized" || kind == "cursor_confirmed" {
        let conversation_id = text(field(&payload, "conversationId"));
        if !conversation_id.is_empty() {
            let mut cursors = py::dict(field(projection, "cursors"))?;
            let mut current = py::dict(field(&cursors, &conversation_id))?;
            let requested = py::int_or(field(&payload, "deliveredSequence"), sequence)?;
            let delivered = int0(field(&current, "deliveredSequence"))?.max(requested);
            current.insert("deliveredSequence".into(), Value::from(delivered));
            current.insert("updatedAt".into(), Value::from(timestamp));
            cursors.insert(conversation_id, Value::Object(current));
            projection.insert("cursors".into(), Value::Object(cursors));
        }
    } else if kind == "legacy_migrated" {
        projection.insert("watch".into(), Value::Array(py::mappings(field(&payload, "watch"))?));
    } else if kind == "projection_checkpoint" {
        projection.insert("checkpointSequence".into(), Value::from(sequence));
    }

    projection.insert("headSequence".into(), Value::from(sequence));
    Ok(())
}

fn insert_event(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    sequence: i64,
    kind: &str,
    event: &Map<String, Value>,
    timestamp: i64,
) -> OpResult<usize> {
    let task_id = format!("project-brain:{owner_user_id}:{project_key}");
    session.execute(
        INSERT_EVENT,
        &params![
            task_id,
            sequence,
            PROJECT_BRAIN_STREAM_KIND,
            "project_brain",
            kind,
            owner_user_id,
            project_key,
            sequence,
            codec::dump_param(&Value::Object(event.clone()))?,
            timestamp
        ],
    )
}

fn event_document(
    owner_user_id: i64,
    project_key: &str,
    sequence: i64,
    kind: &str,
    timestamp: i64,
    payload: Map<String, Value>,
) -> Map<String, Value> {
    let mut event = Map::new();
    event.insert("ownerUserId".into(), Value::from(owner_user_id));
    event.insert("projectKey".into(), Value::String(project_key.to_owned()));
    event.insert("projectSequence".into(), Value::from(sequence));
    event.insert("kind".into(), Value::String(kind.to_owned()));
    event.insert("timestamp".into(), Value::from(timestamp));
    event.insert("payload".into(), Value::Object(payload));
    event
}

/// `_append_event_and_fold`.
pub fn append_event_and_fold(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    projection: &mut Projection,
    kind: &str,
    payload: Map<String, Value>,
    timestamp: i64,
) -> OpResult<Value> {
    let sequence = int0(field(projection, "headSequence"))? + 1;
    let event = event_document(owner_user_id, project_key, sequence, kind, timestamp, payload);
    let count = insert_event(session, owner_user_id, project_key, sequence, kind, &event, timestamp)?;
    if count != 1 {
        return Err(StorageError::new("database_conflict", "Project Brain sequence allocation conflict").into());
    }
    fold_event(projection, &event)?;
    save_projection(session, owner_user_id, project_key, projection, timestamp)?;
    if sequence - int0(field(projection, "checkpointSequence"))? >= EVENT_CHECKPOINT_THRESHOLD {
        write_projection_checkpoint(session, owner_user_id, project_key, projection, timestamp)?;
    }
    Ok(Value::Object(event))
}

/// `_write_projection_checkpoint`.
pub fn write_projection_checkpoint(
    session: &Session<'_>,
    owner_user_id: i64,
    project_key: &str,
    projection: &mut Projection,
    timestamp: i64,
) -> OpResult<()> {
    let sequence = int0(field(projection, "headSequence"))? + 1;
    let mut snapshot = projection.clone();
    snapshot.insert("headSequence".into(), Value::from(sequence));
    snapshot.insert("checkpointSequence".into(), Value::from(sequence));
    let mut payload = Map::new();
    payload.insert("snapshot".into(), Value::Object(snapshot.clone()));
    let event = event_document(owner_user_id, project_key, sequence, "projection_checkpoint", timestamp, payload);
    insert_event(session, owner_user_id, project_key, sequence, "projection_checkpoint", &event, timestamp)?;
    *projection = snapshot;
    save_projection(session, owner_user_id, project_key, projection, timestamp)?;
    session.execute(
        "DELETE FROM storage_events WHERE owner_user_id=? AND project_key=? AND project_sequence>0 AND project_sequence<=?",
        &params![owner_user_id, project_key, sequence - EVENT_CHECKPOINT_TAIL],
    )?;
    Ok(())
}

/// `_command_result`.
pub fn command_result(projection: &Projection, event: Option<Value>) -> OpResult<Value> {
    Ok(json!({
        "ok": true,
        "event": event.unwrap_or(Value::Null),
        "projection": public_projection(projection)?,
        "pushHint": {
            "type": "project_brain_changed",
            "projectSequence": int0(field(projection, "headSequence"))?,
        },
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trailing_separators_follow_python_regex() {
        assert_eq!(strip_trailing_separators("/a/b//"), "/a/b");
        assert_eq!(strip_trailing_separators("/a\\/"), "/a");
        assert_eq!(strip_trailing_separators("/a/\n"), "/a\n");
        assert_eq!(strip_trailing_separators("/a\n"), "/a\n");
        assert_eq!(strip_trailing_separators("///"), "");
    }

    #[test]
    fn bounded_utf8_drops_partial_characters() {
        let text = "验".repeat(400);
        let bounded = bounded_utf8(&text, 720);
        assert_eq!(bounded.len(), 720);
        assert_eq!(bounded_utf8(" a验", 3), "a");
        assert_eq!(bounded_utf8("  ok  ", 720), "ok");
    }
}
