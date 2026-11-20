//! Project Brain maintenance and listing operations: active list, restart
//! recovery snapshot, rebuild, cutover status and legacy cutover.

use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use super::fold::{
    append_event_and_fold, empty_projection, fold_event, integrity, load_projection, protocol, public_projection,
    public_work_item, save_projection, strip_trailing_separators,
};
use super::project_identity;
use super::py::{self, field, get, int0, sql_int0, text};
use crate::codec;
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{char_len, integer, Payload};
use crate::session::{Session, SqlValue};

fn wall_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `[_public_work_item(item) for item in projection.get('workItems') or ()
///   if isinstance(item, Mapping) and item.get('status') == status]`.
fn work_items_with_status(projection: &Map<String, Value>, status: &str) -> OpResult<Vec<Value>> {
    Ok(py::items(field(projection, "workItems"))?
        .iter()
        .filter(|item| item.is_object() && py::is_str(get(item, "status"), status))
        .map(public_work_item)
        .collect())
}

/// `_project_brain_list_active`.
pub fn list_active(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner_user_id = integer(payload, "owner_user_id", None, Some(1), None)?;
    let mut items = Vec::new();
    let mut cursor: Option<(i64, String)> = None;
    loop {
        let (where_clause, parameters) = match &cursor {
            None => ("", params![owner_user_id].to_vec()),
            Some((updated, key)) => (
                " AND (updated_at_ms < ? OR (updated_at_ms = ? AND project_key > ?))",
                params![owner_user_id, *updated, *updated, key].to_vec(),
            ),
        };
        let sql = format!(
            "SELECT projection_json,updated_at_ms,project_key FROM storage_project_brain_projects WHERE owner_user_id=?{where_clause} ORDER BY updated_at_ms DESC,project_key LIMIT 512"
        );
        let rows = session.fetch_all(&sql, &parameters)?;
        let Some(last) = rows.last() else { break };
        cursor = Some((last.int("updated_at_ms")?, last.text("project_key")));
        for row in &rows {
            let Value::Object(projection) = codec::load(row.require("projection_json")?)? else { continue };
            let active = work_items_with_status(&projection, "active")?;
            if !active.is_empty() {
                items.push(json!({
                    "projectKey": text(field(&projection, "projectKey")),
                    "workItems": active,
                }));
            }
        }
    }
    Ok(json!({"projects": items}))
}

/// `_project_brain_recovery_snapshot`.
pub fn recovery_snapshot(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let cursor = field(payload, "cursor");
    let include_cancelled = match field(payload, "include_cancelled") {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        _ => return protocol("Invalid include_cancelled"),
    };
    let mut where_clause = "";
    let mut parameters: Vec<SqlValue> = Vec::new();
    if !cursor.is_null() {
        let valid = match cursor {
            Value::Object(map) => {
                map.len() == 2
                    && map.contains_key("ownerUserId")
                    && map.contains_key("projectKey")
                    && match &map["ownerUserId"] {
                        Value::Number(number) => number.as_u64().is_some_and(|owner| owner > 0),
                        _ => false,
                    }
                    && matches!(&map["projectKey"], Value::String(key) if (1..=4096).contains(&char_len(key)))
            }
            _ => false,
        };
        if !valid {
            return protocol("Invalid recovery cursor");
        }
        let Some(owner) = cursor["ownerUserId"].as_i64() else {
            return internal("OverflowError: Python int too large to convert to SQLite INTEGER");
        };
        where_clause = "WHERE owner_user_id > ? OR (owner_user_id = ? AND project_key > ?) ";
        parameters = params![owner, owner, cursor["projectKey"].as_str().unwrap_or_default()].to_vec();
    }
    let sql = format!(
        "SELECT owner_user_id,project_key,projection_json FROM storage_project_brain_projects {where_clause}ORDER BY owner_user_id,project_key LIMIT 1001"
    );
    let mut rows = session.fetch_all(&sql, &parameters)?;
    let capped = rows.len() > 1000;
    rows.truncate(1000);
    let mut projects = Vec::new();
    for row in &rows {
        let Value::Object(projection) = codec::load(row.require("projection_json")?)? else { continue };
        let active = work_items_with_status(&projection, "active")?;
        let cancelled = if include_cancelled { work_items_with_status(&projection, "cancelled")? } else { Vec::new() };
        if !active.is_empty() || !cancelled.is_empty() {
            let mut project = Map::new();
            project.insert("ownerUserId".into(), Value::from(row.int("owner_user_id")?));
            project.insert("projectKey".into(), Value::String(row.text("project_key")));
            project.insert("workItems".into(), Value::Array(active));
            if include_cancelled {
                project.insert("cancelledWorkItems".into(), Value::Array(cancelled));
            }
            projects.push(Value::Object(project));
        }
    }
    let mut result = Map::new();
    result.insert("projects".into(), Value::Array(projects));
    result.insert("capped".into(), Value::Bool(capped));
    if include_cancelled {
        result.insert("includesCancelled".into(), Value::Bool(true));
    }
    if capped {
        let last = rows.last().expect("capped rows");
        result.insert(
            "nextCursor".into(),
            json!({"ownerUserId": last.int("owner_user_id")?, "projectKey": last.text("project_key")}),
        );
    }
    Ok(Value::Object(result))
}

/// `_project_brain_rebuild`.
pub fn rebuild(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, project_key) = project_identity(payload)?;
    session.lock_key("project_brain.project", &format!("{owner_user_id}:{project_key}"));
    let rows = session.fetch_all(
        "SELECT project_sequence,event_json FROM storage_events WHERE owner_user_id=? AND project_key=? AND project_sequence>0 ORDER BY project_sequence",
        &params![owner_user_id, project_key.as_str()],
    )?;
    if rows.is_empty() {
        return fail("database_not_found", "Project Brain event stream not found");
    }

    let mut parsed: Vec<Map<String, Value>> = Vec::new();
    let mut checkpoint_index: Option<usize> = None;
    for (index, row) in rows.iter().enumerate() {
        let Value::Object(document) = codec::load(row.require("event_json")?)? else {
            return integrity("Project Brain event is invalid");
        };
        let sequence = int0(field(&document, "projectSequence"))?;
        if sequence != sql_int0(row.require("project_sequence")?)? {
            return integrity("Project Brain event sequence mismatch");
        }
        if int0(field(&document, "ownerUserId"))? != owner_user_id
            || text(field(&document, "projectKey")) != project_key
        {
            return integrity("Project Brain event ownership mismatch");
        }
        if py::is_str(field(&document, "kind"), "projection_checkpoint") {
            checkpoint_index = Some(index);
        }
        parsed.push(document);
    }

    let (mut projection, replay_from) = match checkpoint_index {
        Some(index) => {
            let checkpoint_payload = field(&parsed[index], "payload");
            let checkpoint_payload = if truthy(checkpoint_payload) { checkpoint_payload.clone() } else { json!({}) };
            let Value::Object(snapshot) = py::attr(&checkpoint_payload, "snapshot")? else {
                return integrity("Project Brain checkpoint is invalid");
            };
            let projection = snapshot.clone();
            if int0(field(&projection, "ownerUserId"))? != owner_user_id
                || text(field(&projection, "projectKey")) != project_key
            {
                return integrity("Project Brain checkpoint ownership mismatch");
            }
            (projection, index + 1)
        }
        None => {
            if int0(field(&parsed[0], "projectSequence"))? != 1 {
                return integrity("Project Brain event prefix lacks checkpoint");
            }
            (empty_projection(owner_user_id, &project_key), 0)
        }
    };

    let mut previous = int0(field(&projection, "headSequence"))?;
    for event in &parsed[replay_from..] {
        let sequence = int0(field(event, "projectSequence"))?;
        if sequence != previous + 1 {
            return integrity("Project Brain event stream has a gap");
        }
        fold_event(&mut projection, event)?;
        previous = sequence;
    }

    save_projection(session, owner_user_id, &project_key, &projection, wall_ms())?;
    Ok(json!({
        "ok": true,
        "projectKey": project_key,
        "headSequence": int0(field(&projection, "headSequence"))?,
        "checkpointSequence": int0(field(&projection, "checkpointSequence"))?,
        "replayedEvents": parsed.len() - replay_from,
        "projection": public_projection(&projection)?,
    }))
}

/// `_table_exists` (SQLite).
fn table_exists(session: &Session<'_>, table_name: &str) -> OpResult<bool> {
    Ok(session
        .fetch_one("SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=?", &params![table_name])?
        .is_some())
}

fn cutover_complete(session: &Session<'_>) -> OpResult<bool> {
    let row = session
        .fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key=?", &params!["project_brain_cutover_v1"])?;
    Ok(row.is_some_and(|row| row.get("meta_value").as_str() == Some("complete")))
}

/// `_project_brain_cutover_status`.
pub fn cutover_status(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    Ok(json!({"complete": cutover_complete(session)?}))
}

/// `_project_brain_cutover`: atomically migrate durable legacy intent and
/// remove old authorities.
pub fn cutover(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if cutover_complete(session)? {
        return Ok(json!({"ok": true, "alreadyComplete": true, "projects": 0}));
    }
    let now_ms = integer(payload, "timestamp", Some(wall_ms()), Some(0), None)?;

    if session.fetch_one("SELECT 1 AS present FROM storage_project_brain_projects LIMIT 1", &[])?.is_some() {
        return integrity("Project Brain projection exists before cutover receipt");
    }

    let mut projects: BTreeMap<(i64, String), Vec<Value>> = BTreeMap::new();
    if table_exists(session, "storage_watch_items")? {
        let watch_rows = session
            .fetch_all("SELECT * FROM storage_watch_items ORDER BY user_id,project_path,updated_at,item_id", &[])?;
        for row in &watch_rows {
            let owner_user_id = row.int("user_id")?;
            let normalized_project = strip_trailing_separators(&row.text("project_path"));
            if normalized_project.is_empty() {
                continue;
            }
            let mut latest = Value::Null;
            if table_exists(session, "storage_watch_responses")? {
                let response = session.fetch_one(
                    "SELECT response,trigger,ts FROM storage_watch_responses WHERE item_id=? ORDER BY sequence DESC LIMIT 1",
                    &[row.require("item_id")?.clone()],
                )?;
                if let Some(response) = response {
                    let response_text = response.text("response");
                    if !py::strip(&response_text).is_empty() {
                        latest = json!({
                            "text": py::prefix(&response_text, 4000),
                            "trigger": py::prefix(&response.text("trigger"), 64),
                            "timestamp": sql_int0(response.require("ts")?)?,
                        });
                    }
                }
            }
            let status = row.text("status");
            let status = if status.is_empty() || status == "open" { "active" } else { "resolved" };
            let kind = row.text("kind");
            let kind = if kind.is_empty() { "concern".to_owned() } else { kind };
            projects.entry((owner_user_id, normalized_project)).or_default().push(json!({
                "id": py::prefix(&row.text("item_id"), 128),
                "kind": py::prefix(&kind, 64),
                "text": py::prefix(&row.text("text"), 4000),
                "status": status,
                "sourceConversationId": py::prefix(&row.text("created_by_conv"), 256),
                "createdAt": sql_int0(row.require("created_at")?)?,
                "updatedAt": sql_int0(row.require("updated_at")?)?,
                "latestResult": latest,
            }));
        }
    }

    let mut migrated = 0i64;
    for ((owner_user_id, project_key), watch) in projects {
        let mut projection = empty_projection(owner_user_id, &project_key);
        let expected = watch.len();
        let mut event_payload = Map::new();
        event_payload.insert("watch".into(), Value::Array(watch));
        append_event_and_fold(
            session,
            owner_user_id,
            &project_key,
            &mut projection,
            "legacy_migrated",
            event_payload,
            now_ms,
        )?;
        let verified = load_projection(session, owner_user_id, &project_key)?;
        if py::items(field(&verified, "watch"))?.len() != expected {
            return integrity("Project Brain cutover verification failed");
        }
        migrated += 1;
    }

    // Board/Feed/Status execution history is intentionally not imported.
    session.execute("DELETE FROM storage_events WHERE stream_kind IN ('project_feed','project_status')", &[])?;
    session.execute("DELETE FROM storage_records WHERE namespace='project_charter'", &[])?;
    let mut retired_queued_turns = 0i64;
    if table_exists(session, "storage_queue_items")? {
        for row in session.fetch_all("SELECT id,kind,payload_json FROM storage_queue_items", &[])? {
            let queued_payload = codec::load(row.require("payload_json")?)?;
            let is_board_kickoff = queued_payload.is_object() && truthy(get(&queued_payload, "boardTaskId"));
            if row.text("kind") == "peer_msg" || is_board_kickoff {
                retired_queued_turns += session
                    .execute("DELETE FROM storage_queue_items WHERE id=?", &[row.require("id")?.clone()])?
                    as i64;
            }
        }
    }
    for table_name in [
        "storage_watch_responses",
        "storage_watch_runs",
        "storage_watch_items",
        "storage_board_tasks",
        "project_events",
        "project_tasks",
    ] {
        session.execute(&format!("DROP TABLE IF EXISTS {table_name}"), &[])?;
    }
    session.execute(
        "INSERT INTO storage_meta(meta_key,meta_value) VALUES (?,?) ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
        &params!["project_brain_cutover_v1", "complete"],
    )?;
    Ok(json!({
        "ok": true,
        "alreadyComplete": false,
        "projects": migrated,
        "verified": true,
        "retiredQueuedTurns": retired_queued_turns,
    }))
}
