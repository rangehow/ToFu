//! `operations_pkg/_turns_write.py`: turn/attempt creation, queue binding,
//! steer, settled append, attempt lifecycle and projection edits.

use serde_json::{json, Value};

use super::automation::{
    enqueue_task_started, execution_witness, start_context_is_valid, TaskStart, WORKER_EXECUTION_CONFIG_KEY,
};
use super::background::retire_background_command_receipts;
use super::core::{
    as_obj, attempt_public, now_ms, payload_now, projection_change, py_int, text, text_or, turn_event_append,
    turn_identity, turn_public, upsert_turn_search_row, value_in, Obj,
};
use super::head::{advance_unchanged_revision, delete_checkpoint, projection_map};
use super::lifecycle::{delete_turn_row_set, turn_deletion_closure, RowSet};
use super::normalize::{background_command_completion, normalize_projection_document};
use super::patch::build_projection_patch;
use super::read::{conversation_revision, prune_turn_tombstones};
use super::worker::require_conversation_publication_claim;
use crate::codec::{dump, dump_param, load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{json_param, Row, Session, SqlValue};

const KIND_REAL: &str = "real";
const KIND_GOAL_CONTINUATION: &str = "goal_continuation";
const CTX_SNAPSHOT_MAX_BYTES: usize = 16 * 1024;

fn uuid4() -> String {
    uuid::Uuid::new_v4().to_string()
}

fn fetch_turn(session: &Session<'_>, turn_id: &SqlValue) -> OpResult<Option<Row>> {
    session.fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", std::slice::from_ref(turn_id))
}

fn fetch_attempt(session: &Session<'_>, attempt_id: &SqlValue) -> OpResult<Option<Row>> {
    session.fetch_one("SELECT * FROM storage_generation_attempts WHERE attempt_id=?", std::slice::from_ref(attempt_id))
}

fn loaded_obj_or_empty(value: &SqlValue) -> OpResult<Obj> {
    let loaded = load(value)?;
    match loaded {
        _ if !truthy(&loaded) => Ok(Obj::new()),
        Value::Object(map) => Ok(map),
        _ => internal("stored document has no get"),
    }
}

fn attempt_dispatch_mode(payload: &Payload) -> OpResult<String> {
    let mode = text(get(payload, "dispatch_mode"));
    if mode.is_empty() || mode == "conversation_executor" {
        return Ok(mode);
    }
    fail("database_protocol_error", "Invalid conversation attempt dispatch mode")
}

/// Private port of `_queue_shared.renumber_queue_positions`; shared owner: queue.
fn renumber_queue_positions(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<()> {
    let rows = session.fetch_all(
        "SELECT id FROM storage_queue_items WHERE conv_id = ? AND user_id = ? ORDER BY priority, position, id",
        &params![conv_id, user_id],
    )?;
    for (index, row) in rows.iter().enumerate() {
        session.execute(
            "UPDATE storage_queue_items SET position = ? WHERE id = ? AND user_id = ?",
            &params![index as i64 + 1, row.get("id"), user_id],
        )?;
    }
    Ok(())
}

/// Private port of `_queue._queue_item`; shared owner: queue.
fn queue_item(row: &Row) -> OpResult<Value> {
    let payload = loaded_obj_or_empty(row.get("payload_json"))?;
    let config_loaded = load(row.get("config_json"))?;
    let config = if truthy(&config_loaded) { config_loaded } else { Value::Object(Obj::new()) };
    let preview = if truthy(get(&payload, "_peerMessage")) {
        get(&payload, "_peerText").clone()
    } else {
        let value = payload.get("text").cloned().unwrap_or(Value::String(String::new()));
        if truthy(&value) {
            value
        } else {
            Value::String(String::new())
        }
    };
    let kind = row.text("kind");
    let mut result = Obj::new();
    result.insert("queueId".into(), Value::String(row.get("id").text_or_empty()));
    result.insert("userId".into(), Value::from(row.int("user_id")?));
    result.insert("position".into(), Value::from(row.int("position")?));
    result.insert("kind".into(), Value::String(if kind.is_empty() { KIND_REAL.into() } else { kind }));
    result.insert("priority".into(), Value::from(row.int("priority")?));
    result.insert("timestamp".into(), Value::from(row.int("created_at_ms")?));
    result.insert("text".into(), Value::String(python_str(&preview).chars().take(2000).collect()));
    for (key, field) in [
        ("hasImages", "images"),
        ("hasPdfs", "pdfTexts"),
        ("hasAttachments", "attachments"),
        ("hasRefs", "convRefs"),
        ("hasQuotes", "replyQuotes"),
    ] {
        result.insert(key.into(), Value::Bool(truthy(get(&payload, field))));
    }
    result.insert("payload".into(), Value::Object(payload.clone()));
    result.insert("config".into(), config);
    for (column, public) in
        [("input_turn_id", "inputTurnId"), ("output_turn_id", "outputTurnId"), ("attempt_id", "attemptId")]
    {
        let value = row.text(column);
        if !value.is_empty() {
            result.insert(public.into(), Value::String(value));
        }
    }
    let embedded = match get(&payload, "_user_msg") {
        Value::Object(user) => get(user, "_msgId").clone(),
        _ => Value::String(String::new()),
    };
    let source = if truthy(&embedded) { embedded } else { get(&payload, "_msgId").clone() };
    let source = text(&source);
    if !source.is_empty() {
        result.insert("sourceMessageId".into(), Value::String(source));
    }
    if truthy(get(&payload, "_peerMessage")) {
        result.insert("isPeerMessage".into(), Value::Bool(true));
        result.insert("fromConv".into(), payload.get("_fromConv").cloned().unwrap_or(Value::String(String::new())));
        result.insert("isPeerHuman".into(), Value::Bool(truthy(get(&payload, "_peerHuman"))));
    }
    if let Some(completion) = background_command_completion(&payload) {
        result.insert("backgroundCommand".into(), completion);
    }
    if truthy(get(&payload, "_steerFallback")) {
        result.insert("steerFallback".into(), Value::Bool(true));
    }
    Ok(Value::Object(result))
}

/// `_ensure_turn_conversation_header`: returns `(rev, now)`.
fn ensure_turn_conversation_header(session: &Session<'_>, payload: &Payload) -> OpResult<(i64, i64)> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let conversation = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?;
    let raw_defaults = get(payload, "conversation_defaults");
    let defaults = if truthy(raw_defaults) { as_obj(raw_defaults) } else { Obj::new() };
    let now = payload_now(payload)?;
    let rev = match conversation {
        Some(row) => row.int("rev")?,
        None => {
            if !truthy(get(&defaults, "allowCreate")) {
                return fail("database_not_found", "Conversation not found");
            }
            let mut settings = match get(&defaults, "settings") {
                Value::Object(map) => map.clone(),
                _ => Obj::new(),
            };
            settings.shift_remove("activeTaskId");
            let created = if truthy(get(&defaults, "createdAt")) { py_int(get(&defaults, "createdAt"))? } else { now };
            session.execute(
                "INSERT INTO storage_conversations (id,user_id,title,messages_json,created_at_ms,updated_at_ms,settings_json,msg_count,search_text,rev) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0)",
                &params![
                    conv_id.clone(),
                    user_id,
                    text_or(get(&defaults, "title"), "New Chat").chars().take(500).collect::<String>(),
                    dump_param(&json!([]))?,
                    created,
                    now,
                    dump_param(&Value::Object(settings))?,
                    ""
                ],
            )?;
            0
        }
    };
    if let Value::Object(incoming) = get(&defaults, "settings") {
        let Some(current) = session.fetch_one(
            "SELECT settings_json FROM storage_conversations WHERE id=? AND user_id=?",
            &params![conv_id.clone(), user_id],
        )?
        else {
            return fail("database_not_found", "Conversation not found");
        };
        let mut settings = loaded_obj_or_empty(current.get("settings_json"))?;
        let mut incoming = incoming.clone();
        incoming.shift_remove("activeTaskId");
        for (key, value) in incoming {
            settings.insert(key, value);
        }
        session.execute(
            "UPDATE storage_conversations SET settings_json=?,updated_at_ms=? WHERE id=? AND user_id=?",
            &params![dump_param(&Value::Object(settings))?, now, conv_id, user_id],
        )?;
    }
    Ok((rev, now))
}

/// `_turn_create_pair`.
pub fn turn_create_pair(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let dispatch_mode = attempt_dispatch_mode(payload)?;
    let command_id = required_text(payload, "command_id", 256)?;
    let lane_id = text_or(get(payload, "lane_id"), "main");
    let input_actor = text_or(get(payload, "input_actor"), "human");
    let output_actor = text_or(get(payload, "output_actor"), "assistant");
    if !matches!(input_actor.as_str(), "human" | "virtual_user" | "critic")
        || !matches!(output_actor.as_str(), "assistant" | "planner" | "critic" | "virtual_user")
    {
        return fail("database_protocol_error", "Invalid turn actor");
    }
    let raw_binding = get(payload, "queue_binding");
    let binding = match raw_binding {
        _ if !truthy(raw_binding) => Obj::new(),
        Value::Object(map) => map.clone(),
        _ => return fail("database_protocol_error", "Invalid queue binding"),
    };
    let queue_id = super::normalize::py_strip(&text(get(&binding, "queueId"))).to_owned();
    if !queue_id.is_empty() && (queue_id.chars().count() > 256 || input_actor != "human") {
        return fail("database_protocol_error", "Invalid queue binding");
    }
    let existing = session.fetch_one(
        "SELECT a.attempt_id, a.turn_id FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.conversation_id=? AND a.command_id=? AND t.user_id=?",
        &params![conv_id.clone(), command_id.clone(), user_id],
    )?;
    if let Some(existing) = existing {
        let turn = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND user_id=?",
            &params![existing.get("turn_id"), user_id],
        )?;
        let attempt = fetch_attempt(session, existing.get("attempt_id"))?;
        let submitted = match &turn {
            Some(turn) if turn.get("parent_turn_id").truthy() => fetch_turn(session, turn.get("parent_turn_id"))?,
            _ => None,
        };
        let (Some(turn), Some(attempt)) = (turn, attempt) else {
            return fail("database_integrity", "Turn command replay references missing rows");
        };
        let queue_row = if attempt.text("queue_state") == "pending" {
            session.fetch_one(
                "SELECT * FROM storage_queue_items WHERE id=? AND user_id=?",
                &params![attempt.text("queue_id"), user_id],
            )?
        } else {
            None
        };
        let mut result = Obj::new();
        result.insert(
            "submittedTurn".into(),
            match &submitted {
                Some(row) => turn_public(session, row)?,
                None => Value::Null,
            },
        );
        result.insert("turn".into(), turn_public(session, &turn)?);
        result.insert("attempt".into(), attempt_public(&attempt)?);
        result.insert("conversationRevision".into(), Value::from(conversation_revision(session, &conv_id, user_id)?));
        result.insert("streamCursor".into(), json!(1));
        result.insert("idempotentReplay".into(), json!(true));
        result.insert(
            "_needsStart".into(),
            Value::Bool(
                attempt.text("status") == "pending"
                    && !attempt.get("task_id").truthy()
                    && attempt.text("queue_state") != "pending",
            ),
        );
        if let Some(queue_row) = queue_row {
            result.insert("queued".into(), json!(true));
            result.insert("queueId".into(), Value::String(queue_row.get("id").text_or_empty()));
            result.insert("position".into(), Value::from(queue_row.int("position")?));
            result.insert("queueItem".into(), queue_item(&queue_row)?);
        }
        return Ok(Value::Object(result));
    }
    let (conversation_rev, now) = ensure_turn_conversation_header(session, payload)?;
    let parent_turn_id = get(payload, "parent_turn_id").clone();
    let mut parent: Option<Row> = None;
    if truthy(&parent_turn_id) {
        parent = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND conversation_id=? AND user_id=?",
            &[json_param(&parent_turn_id)?, SqlValue::Text(conv_id.clone()), SqlValue::Integer(user_id)],
        )?;
        if parent.is_none() {
            return fail("turn_parent_invalid", "Parent turn does not exist");
        }
    }
    let expected_conversation = get(payload, "expected_conversation_revision");
    if !expected_conversation.is_null() {
        let valid = matches!(expected_conversation, Value::Number(number) if number.as_u64().is_some());
        if !valid {
            return fail("database_protocol_error", "Invalid expected conversation revision");
        }
        if !py_eq(&json!(conversation_revision(session, &conv_id, user_id)?), expected_conversation) {
            return fail("turn_lane_advanced", "The conversation changed before admission.");
        }
    }
    let expected_parent = get(payload, "expected_parent_projection_revision");
    if !expected_parent.is_null() {
        let valid = matches!(expected_parent, Value::Number(number) if number.as_i64().is_some_and(|value| value >= 0));
        if !valid || !truthy(&parent_turn_id) {
            return fail("database_protocol_error", "Invalid parent projection revision");
        }
        let parent = parent.as_ref().expect("validated parent");
        if Some(parent.int("projection_revision")?) != expected_parent.as_i64() {
            return fail("turn_lane_advanced", "The recovery source changed.");
        }
    }
    if (input_actor == "human" && queue_id.is_empty()) || truthy(get(payload, "require_lane_idle")) {
        let live = session.fetch_one(
            "SELECT t.* FROM storage_conversation_turns AS t JOIN storage_generation_attempts AS a ON a.attempt_id=t.current_attempt_id WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? AND a.status IN ('pending','running') AND a.queue_state='' ORDER BY t.ordinal DESC LIMIT 1",
            &params![conv_id.clone(), user_id, lane_id.clone()],
        )?;
        if live.is_some() {
            return fail("turn_in_progress", "This lane already has a live generation attempt.");
        }
    }
    let require_parent_not_stopped = truthy(get(payload, "require_parent_not_stopped"));
    let mut tail: Option<Row> = None;
    if truthy(get(payload, "require_parent_is_lane_tail")) || require_parent_not_stopped {
        tail = session.fetch_one(
            "SELECT turn_id,status,settlement_json FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? ORDER BY ordinal DESC LIMIT 1",
            &params![conv_id.clone(), user_id, lane_id.clone()],
        )?;
        let matches_parent = match &tail {
            Some(tail) => py_eq(&tail.get("turn_id").to_json()?, &parent_turn_id),
            None => false,
        };
        if !matches_parent {
            return fail("turn_lane_advanced", "The lane advanced while the continuation was prepared.");
        }
    }
    if require_parent_not_stopped {
        let tail = tail.as_ref().expect("tail");
        let status = tail.text("status");
        let restart_interrupted = status == "interrupted"
            && text(get(&loaded_obj_or_empty(tail.get("settlement_json"))?, "cause")) == "server_restart"
            && get(&loaded_obj_or_empty(tail.get("settlement_json"))?, "cause").is_string();
        if status != "completed" && status != "failed" && !restart_interrupted {
            return fail("turn_lane_advanced", "The parent stopped or is not settled for continuation.");
        }
    }
    if truthy(get(payload, "reject_if_human_queued")) {
        let waiting = session.fetch_one(
            "SELECT id FROM storage_queue_items WHERE conv_id = ? AND user_id = ? AND kind = ? LIMIT 1",
            &params![conv_id.clone(), user_id, KIND_REAL],
        )?;
        if waiting.is_some() {
            return fail("turn_superseded_by_human", "A newer human turn superseded this Goal continuation.");
        }
    }
    if input_actor == "human" {
        let deleted = session.execute(
            "DELETE FROM storage_queue_items WHERE conv_id = ? AND user_id = ? AND kind = ?",
            &params![conv_id.clone(), user_id, KIND_GOAL_CONTINUATION],
        )?;
        if deleted != 0 {
            renumber_queue_positions(session, &conv_id, user_id)?;
        }
    }
    let input_projection = match get(payload, "input_projection") {
        Value::String(content) => json!({"content": content}),
        value @ Value::Object(_) => value.clone(),
        _ => json!({"content": ""}),
    };
    let input_projection = normalize_projection_document(&input_projection)?;
    session.fetch_one(
        "SELECT 1 AS present FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? LIMIT 1",
        &params![conv_id.clone(), user_id, lane_id.clone()],
    )?;
    let ordinal_row = session.fetch_one(
        "SELECT COALESCE(MAX(ordinal), -1) AS ordinal FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=?",
        &params![conv_id.clone(), user_id, lane_id.clone()],
    )?;
    let base_ordinal = ordinal_row.map(|row| row.int("ordinal")).transpose()?.unwrap_or(-1);
    let input_turn_id = uuid4();
    let output_turn_id = uuid4();
    let attempt_id = uuid4();
    let input_attempt_id = if input_actor != "human" { Some(uuid4()) } else { None };
    let input_presentation = text_or(get(payload, "input_presentation_id"), &format!("{command_id}:input"));
    let output_presentation = text_or(get(payload, "output_presentation_id"), &format!("{command_id}:output"));
    if input_presentation.chars().count() > 512 || output_presentation.chars().count() > 512 {
        return fail("database_protocol_error", "Invalid presentation identity");
    }
    let submitted_settlement = json!({
        "outcome": "completed",
        "cause": if input_actor == "human" { "submitted" } else { "orchestration_generated" },
        "resumeOptions": [],
    });
    let run_id = text(get(payload, "run_id"));
    session.execute(
        "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &[
            SqlValue::Text(input_turn_id.clone()),
            SqlValue::Text(conv_id.clone()),
            SqlValue::Integer(user_id),
            SqlValue::Text(input_presentation),
            SqlValue::Text(lane_id.clone()),
            json_param(&parent_turn_id)?,
            SqlValue::Integer(base_ordinal + 1),
            SqlValue::Text(input_actor.clone()),
            SqlValue::Text(text_or(get(payload, "input_kind"), "input")),
            SqlValue::Text(run_id.clone()),
            SqlValue::Text("completed".into()),
            input_attempt_id.clone().into(),
            dump_param(&Value::Object(input_projection.clone()))?,
            SqlValue::Integer(1),
            dump_param(&submitted_settlement)?,
            SqlValue::Integer(now),
            SqlValue::Integer(now),
        ],
    )?;
    if let Some(input_attempt_id) = &input_attempt_id {
        session.execute(
            "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at,started_at,settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            &params![
                input_attempt_id.clone(),
                conv_id.clone(),
                input_turn_id.clone(),
                format!("{command_id}:input"),
                "",
                "generate",
                "completed",
                0i64,
                dump_param(&json!({}))?,
                dump_param(&json!({"runId": run_id.clone()}))?,
                dump_param(&json!({}))?,
                now,
                now,
                now
            ],
        )?;
        let event = json!({
            "conversationId": conv_id,
            "turnId": input_turn_id,
            "attemptId": input_attempt_id,
            "seq": 1,
            "projectionRevision": 1,
            "type": "terminal_settlement",
            "payload": {
                "status": "completed",
                "settlement": submitted_settlement,
                "projection": Value::Object(input_projection.clone()),
            },
        });
        super::core::insert_attempt_event(
            session,
            input_attempt_id,
            1,
            &conv_id,
            &input_turn_id,
            1,
            "terminal_settlement",
            &event,
            now,
            false,
        )?;
    }
    session.execute(
        "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            output_turn_id.clone(),
            conv_id.clone(),
            user_id,
            output_presentation,
            lane_id.clone(),
            input_turn_id.clone(),
            base_ordinal + 2,
            output_actor,
            text_or(get(payload, "kind"), "reply"),
            run_id,
            "pending",
            attempt_id.clone(),
            dump_param(&json!({"content": "", "thinking": "", "segments": [], "toolRounds": []}))?,
            1i64,
            dump_param(&json!({}))?,
            now,
            now
        ],
    )?;
    let config = get(payload, "config");
    let config = if truthy(config) { config.clone() } else { json!({}) };
    session.execute(
        "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,dispatch_mode,queue_id,queue_state,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            attempt_id.clone(),
            conv_id.clone(),
            output_turn_id.clone(),
            command_id.clone(),
            "",
            "generate",
            dispatch_mode,
            queue_id.clone(),
            if queue_id.is_empty() { "" } else { "pending" },
            "pending",
            0i64,
            dump_param(&json!({}))?,
            dump_param(&config)?,
            dump_param(&json!({}))?,
            now
        ],
    )?;
    let mut queue_row = None;
    if !queue_id.is_empty() {
        if session.fetch_one("SELECT id FROM storage_queue_items WHERE id=?", &params![queue_id.clone()])?.is_some() {
            return fail("database_conflict", "Queue identity already exists");
        }
        let queue_rows = session.fetch_all(
            "SELECT id FROM storage_queue_items WHERE conv_id=? AND user_id=? ORDER BY priority,position",
            &params![conv_id.clone(), user_id],
        )?;
        let position = queue_rows.len() as i64 + 1;
        let message = match get(&binding, "message") {
            Value::Object(message) => Value::Object(message.clone()),
            _ => json!({
                "text": text(get(&input_projection, "content")),
                "_user_msg": Value::Object(input_projection.clone()),
            }),
        };
        let kind = text_or(get(&binding, "kind"), KIND_REAL);
        if kind != KIND_REAL && kind != KIND_GOAL_CONTINUATION {
            return fail("database_protocol_error", "Invalid queued turn kind");
        }
        let priority = integer(&binding, "priority", Some(100), Some(0), Some(1000))?;
        let created_at = integer(&binding, "createdAt", Some(now), Some(0), None)?;
        session.execute(
            "INSERT INTO storage_queue_items (id,user_id,conv_id,payload_json,config_json,position,kind,priority,created_at_ms,input_turn_id,output_turn_id,attempt_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            &params![
                queue_id.clone(),
                user_id,
                conv_id.clone(),
                dump_param(&message)?,
                dump_param(&config)?,
                position,
                kind,
                priority,
                created_at,
                input_turn_id.clone(),
                output_turn_id.clone(),
                attempt_id.clone()
            ],
        )?;
        queue_row = session.fetch_one(
            "SELECT * FROM storage_queue_items WHERE id=? AND user_id=?",
            &params![queue_id.clone(), user_id],
        )?;
    }
    let event = json!({
        "conversationId": conv_id,
        "turnId": output_turn_id,
        "attemptId": attempt_id,
        "seq": 1,
        "projectionRevision": 1,
        "type": "status_changed",
        "payload": {"status": "pending"},
    });
    super::core::insert_attempt_event(
        session,
        &attempt_id,
        1,
        &conv_id,
        &output_turn_id,
        1,
        "status_changed",
        &event,
        now,
        false,
    )?;
    let revision = conversation_rev + 1;
    session.execute(
        "UPDATE storage_conversations SET rev=?, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![revision, now, conv_id.clone(), user_id],
    )?;
    let input_row = fetch_turn(session, &SqlValue::Text(input_turn_id))?;
    let output_row = fetch_turn(session, &SqlValue::Text(output_turn_id))?;
    let attempt_row = fetch_attempt(session, &SqlValue::Text(attempt_id))?;
    let (Some(input_row), Some(output_row), Some(attempt_row)) = (input_row, output_row, attempt_row) else {
        return fail("database_integrity", "Created turn pair is incomplete");
    };
    upsert_turn_search_row(session, &input_row, None)?;
    upsert_turn_search_row(session, &output_row, None)?;
    let mut result = Obj::new();
    result.insert("submittedTurn".into(), turn_public(session, &input_row)?);
    result.insert("turn".into(), turn_public(session, &output_row)?);
    result.insert("attempt".into(), attempt_public(&attempt_row)?);
    result.insert("conversationRevision".into(), Value::from(revision));
    result.insert("streamCursor".into(), json!(1));
    result.insert("idempotentReplay".into(), json!(false));
    result.insert("_needsStart".into(), Value::Bool(queue_id.is_empty()));
    if let Some(queue_row) = queue_row {
        result.insert("queued".into(), json!(true));
        result.insert("queueId".into(), Value::String(queue_id));
        result.insert("position".into(), Value::from(queue_row.int("position")?));
        result.insert("queueItem".into(), queue_item(&queue_row)?);
    }
    Ok(Value::Object(result))
}

/// `_turn_queue_activate`.
pub fn turn_queue_activate(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    let queue_row = session.fetch_one(
        "SELECT * FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        &params![queue_id.clone(), conv_id.clone(), user_id],
    )?;
    let Some(queue_row) = queue_row.filter(|row| row.get("attempt_id").truthy()) else {
        return fail("database_not_found", "Queued turn not found");
    };
    let attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=? AND conversation_id=? AND queue_id=? AND queue_state='pending' AND status='pending' AND task_id=''",
        &params![queue_row.get("attempt_id"), conv_id.clone(), queue_id.clone()],
    )?;
    let Some(attempt) = attempt else {
        return fail("database_conflict", "Queued attempt is no longer pending");
    };
    let by_id = |column: &str| {
        session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND conversation_id=? AND user_id=?",
            &params![queue_row.get(column), conv_id.clone(), user_id],
        )
    };
    let output_row = by_id("output_turn_id")?;
    let input_row = by_id("input_turn_id")?;
    let (Some(output_row), Some(input_row)) = (output_row, input_row) else {
        return fail("database_integrity", "Queued turn pair is incomplete");
    };
    let live = session.fetch_one(
        "SELECT a.attempt_id FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.current_attempt_id=a.attempt_id WHERE t.conversation_id=? AND t.user_id=? AND t.lane_id=? AND a.status IN ('pending','running') AND a.queue_state='' LIMIT 1",
        &params![conv_id.clone(), user_id, output_row.get("lane_id")],
    )?;
    if live.is_some() {
        return fail("turn_in_progress", "This lane already has a live generation attempt.");
    }
    session.execute(
        "UPDATE storage_generation_attempts SET queue_id='',queue_state='' WHERE attempt_id=?",
        &[attempt.get("attempt_id").clone()],
    )?;
    session.execute(
        "DELETE FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        &params![queue_id.clone(), conv_id.clone(), user_id],
    )?;
    renumber_queue_positions(session, &conv_id, user_id)?;
    let now = now_ms();
    let Some(conversation) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return fail("database_integrity", "Queued conversation is missing");
    };
    let revision = conversation.opt_int("rev").unwrap_or(0) + 1;
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? WHERE id=? AND user_id=?",
        &params![revision, now, conv_id, user_id],
    )?;
    let Some(attempt) = fetch_attempt(session, attempt.get("attempt_id"))? else {
        return fail("database_integrity", "Queued attempt is missing");
    };
    Ok(json!({
        "submittedTurn": turn_public(session, &input_row)?,
        "turn": turn_public(session, &output_row)?,
        "attempt": attempt_public(&attempt)?,
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": false,
        "queued": false,
        "queueId": queue_id,
        "_needsStart": true,
    }))
}

/// `_turn_queue_cancel`.
pub fn turn_queue_cancel(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let queue_id = required_text(payload, "queue_id", 256)?;
    let Some(conversation) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return fail("database_not_found", "Conversation not found");
    };
    let current_rev = conversation.opt_int("rev").unwrap_or(0);
    let queue_row = session.fetch_one(
        "SELECT * FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        &params![queue_id.clone(), conv_id.clone(), user_id],
    )?;
    let Some(queue_row) = queue_row else {
        return Ok(json!({
            "conversationId": conv_id, "conversationRevision": current_rev, "queueId": queue_id,
            "cancelled": false, "inputTurn": null, "deletedTurnIds": [],
        }));
    };
    let input_turn_id = queue_row.text("input_turn_id");
    let output_turn_id = queue_row.text("output_turn_id");
    let attempt_id = queue_row.text("attempt_id");
    if input_turn_id.is_empty() && output_turn_id.is_empty() && attempt_id.is_empty() {
        let message = loaded_obj_or_empty(queue_row.get("payload_json"))?;
        let completion = get(&message, "_backgroundCommand");
        let kind = queue_row.text("kind");
        if (kind == "workflow_step" || kind == "background_receipt")
            && matches!(completion, Value::String(id) if !id.is_empty())
        {
            let now = now_ms();
            if queue_row.get("lease_task_id").truthy() || queue_row.opt_int("leased_until_ms").unwrap_or(0) >= now {
                return fail("database_conflict", "Queue delivery is already being dispatched");
            }
            session.execute(
                "DELETE FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
                &params![queue_id.clone(), conv_id.clone(), user_id],
            )?;
            renumber_queue_positions(session, &conv_id, user_id)?;
            let revision = current_rev + 1;
            session.execute(
                "UPDATE storage_conversations SET rev=?,updated_at_ms=? WHERE id=? AND user_id=?",
                &params![revision, now, conv_id.clone(), user_id],
            )?;
            return Ok(json!({
                "conversationId": conv_id, "conversationRevision": revision, "queueId": queue_id,
                "cancelled": true, "inputTurn": null, "deletedTurnIds": [],
            }));
        }
    }
    if input_turn_id.is_empty() || output_turn_id.is_empty() || attempt_id.is_empty() {
        return fail("database_conflict", "Legacy queue rows use the compatibility cancel adapter");
    }
    let attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=? AND conversation_id=? AND queue_id=? AND queue_state='pending' AND status='pending' AND task_id=''",
        &params![attempt_id, conv_id.clone(), queue_id.clone()],
    )?;
    if attempt.is_none() {
        return fail("database_conflict", "Queued attempt is no longer cancellable");
    }
    let rows = session.fetch_all(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id IN (?,?)",
        &params![conv_id.clone(), user_id, input_turn_id.clone(), output_turn_id.clone()],
    )?;
    let mut by_id = RowSet::new();
    for row in rows {
        by_id.insert(row.text("turn_id"), row);
    }
    if by_id.len() != 2 || !by_id.contains_key(&input_turn_id) || !by_id.contains_key(&output_turn_id) {
        return fail("database_integrity", "Queued turn pair is incomplete");
    }
    let input_turn = turn_public(session, &by_id[&input_turn_id])?;
    session.execute(
        "DELETE FROM storage_queue_items WHERE id=? AND conv_id=? AND user_id=?",
        &params![queue_id.clone(), conv_id.clone(), user_id],
    )?;
    renumber_queue_positions(session, &conv_id, user_id)?;
    let now = now_ms();
    let deleted = delete_turn_row_set(session, &conv_id, user_id, &by_id, now)?;
    let revision = current_rev + 1;
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? WHERE id=? AND user_id=?",
        &params![revision, now, conv_id.clone(), user_id],
    )?;
    Ok(json!({
        "conversationId": conv_id, "conversationRevision": revision, "queueId": queue_id,
        "cancelled": true, "inputTurn": input_turn, "deletedTurnIds": deleted,
    }))
}

/// `_turn_steer_commit`.
pub fn turn_steer_commit(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let command_id = required_text(payload, "command_id", 256)?;
    let steer_text = required_text(payload, "text", 1024 * 1024)?;
    let images = match get(payload, "images") {
        Value::Array(images) => images.clone(),
        _ => vec![],
    };
    let author = get(payload, "author");
    if !author.is_null() && !author.is_object() {
        return fail("database_protocol_error", "Invalid author in storage request");
    }
    let row = session.fetch_one(
        "SELECT t.* FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id WHERE a.attempt_id=? AND a.conversation_id=? AND t.user_id=? AND a.status IN ('pending','running') AND a.queue_state='' AND a.task_id<>'' AND t.current_attempt_id=a.attempt_id AND t.status IN ('pending','running')",
        &params![attempt_id.clone(), conv_id.clone(), user_id],
    )?;
    let Some(row) = row else {
        return fail("database_conflict", "The steer execution window is already closed");
    };
    let mut next = projection_map(session, &row)?;
    let mut records: Vec<Value> = match next.get("_userSteerInjects") {
        Some(Value::Array(items)) => items.clone(),
        _ => vec![],
    };
    let block_id = format!("injection:user-steer:{command_id}");
    let exists = records.iter().any(
        |item| matches!(item, Value::Object(record) if record.get("blockId") == Some(&Value::String(block_id.clone()))),
    );
    if !exists {
        let mut preview = Obj::new();
        preview.insert("text".into(), Value::String(steer_text.chars().take(1200).collect()));
        if !images.is_empty() {
            preview.insert("images".into(), Value::Array(images));
        }
        let mut record = Obj::new();
        record.insert("blockId".into(), Value::String(block_id.clone()));
        record.insert("commandId".into(), Value::String(command_id.clone()));
        record.insert("count".into(), json!(1));
        record.insert("previews".into(), Value::Array(vec![Value::Object(preview)]));
        record.insert("deliveryState".into(), json!("pending"));
        if author.is_object() {
            record.insert("author".into(), author.clone());
        }
        records.push(Value::Object(record));
    }
    next.insert("_userSteerInjects".into(), Value::Array(records));
    let base = row.opt_int("projection_revision").unwrap_or(0);
    let now = now_ms();
    let changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND projection_revision=? AND current_attempt_id=?",
        &params![dump_param(&Value::Object(next))?, base + 1, now, row.get("turn_id"), base, attempt_id],
    )?;
    if changed == 0 {
        return fail("database_conflict", "The steer execution window changed while committing");
    }
    delete_checkpoint(session, &row.text("turn_id"))?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1,updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, conv_id.clone(), user_id],
    )?;
    let Some(updated) = fetch_turn(session, row.get("turn_id"))? else {
        return fail("database_integrity", "Steered turn is missing");
    };
    upsert_turn_search_row(session, &updated, None)?;
    let Some(conversation) = session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )?
    else {
        return fail("database_integrity", "Steered conversation is missing");
    };
    Ok(json!({
        "steered": true,
        "injectionId": command_id,
        "blockId": block_id,
        "conversationId": conv_id,
        "turnId": row.text("turn_id"),
        "conversationRevision": conversation.opt_int("rev").unwrap_or(0),
    }))
}

/// `_turn_append_settled`.
pub fn turn_append_settled(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let actor = required_text(payload, "actor", 32)?;
    if !matches!(actor.as_str(), "human" | "assistant" | "planner" | "critic" | "virtual_user") {
        return fail("database_protocol_error", "Invalid settled turn actor");
    }
    let status = text_or(get(payload, "status"), "completed");
    if !matches!(status.as_str(), "completed" | "failed" | "interrupted" | "truncated" | "superseded") {
        return fail("database_protocol_error", "Invalid settled turn status");
    }
    let projection = get(payload, "projection");
    let projection = match projection {
        _ if !truthy(projection) => Value::Object(Obj::new()),
        Value::Object(_) => projection.clone(),
        _ => return fail("database_protocol_error", "Invalid turn projection"),
    };
    let projection = normalize_projection_document(&projection)?;
    let settlement = get(payload, "settlement");
    let settlement = match settlement {
        _ if !truthy(settlement) => json!({"outcome": status, "cause": "ingested", "resumeOptions": []}),
        Value::Object(_) => settlement.clone(),
        _ => return fail("database_protocol_error", "Invalid turn settlement"),
    };
    let lane_id = text_or(get(payload, "lane_id"), "main");
    let command_id = required_text(payload, "command_id", 256)?;
    let (rev, command_now) = ensure_turn_conversation_header(session, payload)?;
    let tail = session.fetch_one(
        "SELECT turn_id,ordinal FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? ORDER BY ordinal DESC LIMIT 1",
        &params![conv_id.clone(), user_id, lane_id.clone()],
    )?;
    let parent = tail.as_ref().map(|tail| tail.text("turn_id"));
    let ordinal = match &tail {
        Some(tail) => tail.int("ordinal")? + 1,
        None => 0,
    };
    let now = integer(payload, "created_at", Some(command_now), Some(0), None)?;
    let turn_id = if truthy(get(payload, "turn_id")) { python_str(get(payload, "turn_id")) } else { uuid4() };
    let attempt_id = if actor != "human" { Some(uuid4()) } else { None };
    if session
        .fetch_one("SELECT turn_id FROM storage_conversation_turns WHERE turn_id=?", &params![turn_id.clone()])?
        .is_some()
    {
        return fail("database_conflict", "Turn already exists");
    }
    session.execute(
        "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        &params![
            turn_id.clone(),
            conv_id.clone(),
            user_id,
            lane_id,
            parent,
            ordinal,
            actor,
            text_or(get(payload, "kind"), "ingested"),
            text(get(payload, "run_id")),
            status.clone(),
            attempt_id.clone(),
            dump_param(&Value::Object(projection))?,
            dump_param(&settlement)?,
            now,
            now
        ],
    )?;
    let mut attempt_value = Value::Null;
    if let Some(attempt_id) = &attempt_id {
        let error = get(payload, "error");
        let error = if truthy(error) { error.clone() } else { json!({}) };
        let config = get(payload, "config");
        let config = if truthy(config) { config.clone() } else { json!({}) };
        session.execute(
            "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at,started_at,settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            &params![
                attempt_id.clone(),
                conv_id.clone(),
                turn_id.clone(),
                command_id,
                "",
                "ingest",
                status,
                dump_param(&json!({}))?,
                dump_param(&config)?,
                dump_param(&error)?,
                now,
                now,
                now
            ],
        )?;
        let Some(row) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else {
            return fail("database_integrity", "Settled attempt is missing");
        };
        attempt_value = attempt_public(&row)?;
    }
    let revision = rev + 1;
    session.execute(
        "UPDATE storage_conversations SET rev=?,updated_at_ms=? WHERE id=? AND user_id=?",
        &params![revision, now, conv_id, user_id],
    )?;
    let Some(turn_row) = fetch_turn(session, &SqlValue::Text(turn_id))? else {
        return fail("database_integrity", "Settled turn is missing");
    };
    upsert_turn_search_row(session, &turn_row, None)?;
    Ok(json!({"turn": turn_public(session, &turn_row)?, "attempt": attempt_value, "conversationRevision": revision}))
}

fn dispatch_claims(payload: &Payload, attempt_id: &str) -> OpResult<(String, String, bool)> {
    let mut owner = text(get(payload, "dispatch_owner_id"));
    if !owner.is_empty() {
        owner = required_text(payload, "dispatch_owner_id", 64)?;
    }
    let legacy = format!("@dispatching:{attempt_id}");
    let claim = if owner.is_empty() { legacy.clone() } else { format!("{legacy}:{owner}") };
    Ok((legacy, claim, !owner.is_empty()))
}

/// `_turn_attempt_claim`.
pub fn turn_attempt_claim(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let (_, claim, has_owner) = dispatch_claims(payload, &attempt_id)?;
    let attempt = session.fetch_one(
        "SELECT a.status, a.task_id, a.queue_state FROM storage_generation_attempts AS a JOIN storage_conversation_turns AS t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=?",
        &params![attempt_id.clone(), user_id],
    )?;
    let Some(attempt) = attempt else { return Ok(json!(false)) };
    if attempt.text("status") != "pending" || attempt.get("queue_state").truthy() {
        return Ok(json!(false));
    }
    let existing = attempt.text("task_id");
    if existing == claim {
        return Ok(Value::Bool(has_owner));
    }
    if !existing.is_empty() {
        return Ok(json!(false));
    }
    let changed = session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=? AND status='pending' AND task_id='' AND queue_state=''",
        &params![claim, attempt_id],
    )?;
    Ok(Value::Bool(changed != 0))
}

type ResumeOptions = (Vec<String>, Vec<(String, Obj)>);

fn validated_resume_option_anchors(settlement: &Obj) -> OpResult<ResumeOptions> {
    let raw =
        if settlement.contains_key("resumeOptions") { get(settlement, "resumeOptions").clone() } else { json!([]) };
    let Value::Array(raw) = raw else { return fail("database_protocol_error", "Invalid stored resume options") };
    let mut options = Vec::new();
    let mut anchors = Vec::new();
    for item in raw {
        let (operation, anchor) = match &item {
            Value::String(_) => (item.clone(), json!({})),
            Value::Object(map) => (get(map, "operation").clone(), map.get("anchor").cloned().unwrap_or(json!({}))),
            _ => return fail("database_protocol_error", "Invalid stored resume option"),
        };
        let Value::String(operation) = operation.clone() else {
            return fail("database_protocol_error", "Invalid stored resume operation");
        };
        if operation.is_empty() {
            return fail("database_protocol_error", "Invalid stored resume operation");
        }
        let Value::Object(anchor) = anchor else {
            return fail("database_protocol_error", "Invalid stored resume anchor");
        };
        if options.contains(&operation) {
            return fail("database_protocol_error", "Duplicate stored resume operation");
        }
        options.push(operation.clone());
        anchors.push((operation, anchor));
    }
    Ok((options, anchors))
}

/// Private port of `tool_round_replay.canonical_resume_anchor`.
fn canonical_resume_anchor(operation: &str, requested: &Value, stored: &Obj) -> Option<Obj> {
    let canonical = stored.clone();
    if requested.is_null() {
        return Some(canonical);
    }
    let Value::Object(requested) = requested else { return None };
    if py_eq(&Value::Object(requested.clone()), &Value::Object(canonical.clone())) {
        return Some(canonical);
    }
    if operation != "checkpoint_resume" || requested.contains_key("retainedToolRoundPositions") {
        return None;
    }
    let positions = canonical.get("retainedToolRoundPositions")?;
    let mut widened = requested.clone();
    widened.insert("retainedToolRoundPositions".into(), positions.clone());
    if !py_eq(&Value::Object(widened), &Value::Object(canonical.clone())) {
        return None;
    }
    Some(canonical)
}

/// `tool_round_identity._identity_text`.
fn identity_text(value: &Value) -> String {
    let raw = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) if number.is_i64() || number.is_u64() => number.to_string(),
        _ => return String::new(),
    };
    let text: String = super::normalize::py_strip(&raw).chars().take(256).collect();
    if text.starts_with("@dispatching:") {
        String::new()
    } else {
        text
    }
}

fn stamped_round(value: &Obj, attempt: &str, task: &str) -> Obj {
    let mut result = value.clone();
    let pick = |a: &str, b: &str| {
        let first = get(value, a);
        identity_text(if truthy(first) { first } else { get(value, b) })
    };
    let existing_attempt = pick("attemptId", "_attemptId");
    let existing_task = pick("taskId", "_taskId");
    if (!existing_task.is_empty() && existing_task != task)
        || (!existing_attempt.is_empty() && existing_attempt != attempt)
    {
        return result;
    }
    if !attempt.is_empty() && !truthy(get(&result, "attemptId")) {
        result.insert("attemptId".into(), Value::String(attempt.into()));
    }
    if !task.is_empty() && !truthy(get(&result, "taskId")) {
        result.insert("taskId".into(), Value::String(task.into()));
    }
    result
}

/// Private port of `tool_round_identity.projection_history_with_execution_identity`.
fn projection_history_with_execution_identity(projection: &Obj, attempt_id: &Value, task_id: &Value) -> Obj {
    let attempt = identity_text(attempt_id);
    let task = identity_text(task_id);
    let mut projection = projection.clone();
    if let Some(Value::Array(rounds)) = projection.get("toolRounds") {
        let stamped = rounds
            .iter()
            .map(|round| match round {
                Value::Object(round) => Value::Object(stamped_round(round, &attempt, &task)),
                other => other.clone(),
            })
            .collect();
        projection.insert("toolRounds".into(), Value::Array(stamped));
    }
    let Some(Value::Array(segments)) = projection.get("segments") else { return projection };
    let scoped = segments
        .iter()
        .map(|value| {
            let Value::Object(segment) = value else { return value.clone() };
            let mut segment = segment.clone();
            if segment.get("type") == Some(&json!("tool_use")) || !get(&segment, "llmRound").is_null() {
                segment = stamped_round(&segment, &attempt, &task);
                if let Some(Value::Object(round)) = segment.get("_round") {
                    let stamped = stamped_round(round, &attempt, &task);
                    segment.insert("_round".into(), Value::Object(stamped));
                }
            }
            Value::Object(segment)
        })
        .collect();
    projection.insert("segments".into(), Value::Array(scoped));
    projection
}

fn resume_ctx_rebound_projection(previous: &Obj, input_update: &Value) -> OpResult<Obj> {
    let Value::Object(update) = input_update else {
        return fail("database_conflict", "Only regenerate may edit its submitted turn.");
    };
    if update.keys().any(|key| key != "contextSnapshot") {
        return fail("database_conflict", "Only regenerate may edit its submitted turn.");
    }
    let mut next = previous.clone();
    next.shift_remove("_ctx");
    let ctx = get(update, "contextSnapshot");
    if ctx.is_null() {
        next.shift_remove("contextSnapshot");
        return Ok(next);
    }
    let snapshot = match ctx {
        Value::Object(ctx) => match ctx.get("snapshot") {
            Some(snapshot @ Value::Object(_)) => snapshot.clone(),
            _ => return fail("database_protocol_error", "contextSnapshot must carry a snapshot object."),
        },
        _ => return fail("database_protocol_error", "contextSnapshot must carry a snapshot object."),
    };
    if dump(&snapshot)?.len() > CTX_SNAPSHOT_MAX_BYTES {
        return fail("database_protocol_error", "contextSnapshot exceeds its size budget.");
    }
    let block: String =
        text_or(get(ctx.as_object().expect("object"), "blockId"), "turn-context").chars().take(80).collect();
    next.insert("contextSnapshot".into(), json!({"blockId": block, "snapshot": snapshot}));
    Ok(next)
}

fn later_turn_discard_acknowledgement(payload: &Payload) -> OpResult<Option<Vec<String>>> {
    let raw = get(payload, "discard_later_turn_ids");
    if raw.is_null() {
        return Ok(None);
    }
    let invalid = || fail("database_protocol_error", "Invalid later-Turn discard acknowledgement");
    let Value::Array(items) = raw else { return invalid() };
    let mut ids: Vec<String> = Vec::new();
    for item in items {
        match item {
            Value::String(id) if !id.is_empty() && id.chars().count() <= 128 && !ids.contains(id) => {
                ids.push(id.clone())
            }
            _ => return invalid(),
        }
    }
    Ok(Some(ids))
}

pub(super) fn attempt_source(
    session: &Session<'_>,
    turn: &Row,
    public: &Value,
    expected_revision: i64,
) -> OpResult<(Value, Option<Row>)> {
    if Some(expected_revision) != public["projectionRevision"].as_i64() {
        return fail("turn_projection_stale", "The turn changed since this command was prepared.");
    }
    let current_id = public["currentAttemptId"].clone();
    let current = if truthy(&current_id) {
        session.fetch_one(
            "SELECT status, task_id FROM storage_generation_attempts WHERE attempt_id=? AND conversation_id=? AND turn_id=?",
            &params![json_param(&current_id)?, turn.get("conversation_id"), turn.get("turn_id")],
        )?
    } else {
        None
    };
    if current.as_ref().is_some_and(|row| matches!(row.text("status").as_str(), "pending" | "running")) {
        return fail("database_conflict", "This turn already has a live attempt.");
    }
    Ok((current_id, current))
}

pub(super) fn attempt_resume_anchor(public: &Value, operation: &str, requested_anchor: &Value) -> OpResult<Obj> {
    let Value::Object(settlement) = &public["settlement"] else {
        return fail("database_protocol_error", "Invalid stored turn settlement");
    };
    let (options, anchors) = validated_resume_option_anchors(settlement)?;
    if operation != "regenerate" && !options.iter().any(|option| option == operation) {
        return fail("database_conflict", format!("{operation} is not available for the current settlement."));
    }
    if !requested_anchor.is_null() && !requested_anchor.is_object() {
        return fail("database_protocol_error", "Requested resume anchor must be an object");
    }
    let stored_anchor =
        anchors.iter().find(|(name, _)| name == operation).map(|(_, anchor)| anchor.clone()).unwrap_or_default();
    let Some(anchor) = canonical_resume_anchor(operation, requested_anchor, &stored_anchor) else {
        return fail("database_conflict", "The requested resume anchor is not current.");
    };
    Ok(anchor)
}

pub(super) fn attempt_later_turns(session: &Session<'_>, turn: &Row) -> OpResult<RowSet> {
    let conv_id = turn.text("conversation_id");
    let user_id = turn.int("user_id")?;
    let tail_ids: Vec<String> = session.fetch_all(
        "SELECT turn_id FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal>?",
        &params![conv_id.clone(), user_id, turn.get("lane_id"), turn.get("ordinal")],
    )?.iter().map(|row| row.text("turn_id")).collect();
    turn_deletion_closure(session, &conv_id, user_id, &tail_ids)
}

pub(super) fn attempt_resume_projection(
    session: &Session<'_>,
    turn: &Row,
    public: &Value,
    current_id: &Value,
    current: Option<&Row>,
) -> OpResult<Obj> {
    let projection = as_obj(&public["projection"]);
    if !truthy(current_id) {
        return Ok(projection);
    }
    let count = session
        .fetch_one(
            "SELECT COUNT(*) AS attempt_count FROM storage_generation_attempts WHERE conversation_id=? AND turn_id=?",
            &params![turn.get("conversation_id"), turn.get("turn_id")],
        )?
        .map(|row| row.opt_int("attempt_count").unwrap_or(0));
    let Some(count) = count else { return fail("database_integrity", "Attempt count is unavailable") };
    if count <= 1 {
        let task = match current {
            Some(row) => row.get("task_id").to_json()?,
            None => json!(""),
        };
        return Ok(projection_history_with_execution_identity(&projection, current_id, &task));
    }
    Ok(projection)
}

/// `_turn_attempt_create`.
pub fn turn_attempt_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let dispatch_mode = attempt_dispatch_mode(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let command_id = required_text(payload, "command_id", 256)?;
    let operation = required_text(payload, "operation", 64)?;
    let expected_revision = integer(payload, "expected_projection_revision", None, Some(0), None)?;
    if !matches!(operation.as_str(), "continue" | "checkpoint_resume" | "regenerate" | "answer_guidance") {
        return fail("database_protocol_error", "Invalid attempt operation");
    }
    let acknowledged = later_turn_discard_acknowledgement(payload)?;
    let raw_config = get(payload, "config");
    let raw_answer = match raw_config {
        Value::Object(config) => get(config, "_humanGuidanceAnswer").clone(),
        _ => Value::Null,
    };
    if operation == "answer_guidance" {
        let valid = match &raw_answer {
            Value::Object(answer) => {
                matches!(answer.get("guidanceId"), Some(Value::String(id)) if !id.is_empty() && id.chars().count() <= 128)
                    && matches!(answer.get("response"), Some(Value::String(text)) if !text.is_empty() && text.chars().count() <= 32768)
            }
            _ => false,
        };
        if !valid {
            return fail("database_protocol_error", "answer_guidance requires a bounded _humanGuidanceAnswer config");
        }
    } else if !raw_answer.is_null() {
        return fail("database_protocol_error", "_humanGuidanceAnswer is only valid for answer_guidance");
    }
    let target_actor = get(payload, "target_actor");
    let target_kind = get(payload, "target_kind");
    if operation == "regenerate" {
        if !value_in(target_actor, &["assistant", "planner"])? {
            return fail("database_protocol_error", "Invalid regenerate target actor");
        }
        if !value_in(target_kind, &["reply", "plan", "flow_node"])? {
            return fail("database_protocol_error", "Invalid regenerate target kind");
        }
        if (target_actor == &json!("planner")) != (target_kind == &json!("plan")) {
            return fail("database_protocol_error", "Inconsistent regenerate target identity");
        }
    } else if !target_actor.is_null() || !target_kind.is_null() {
        return fail("database_protocol_error", "Only regenerate may migrate generated Turn identity");
    }
    let existing = session.fetch_one(
        "SELECT a.attempt_id, a.turn_id FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id AND t.conversation_id=a.conversation_id WHERE a.conversation_id=? AND t.user_id=? AND a.command_id=?",
        &params![conv_id.clone(), user_id, command_id.clone()],
    )?;
    if let Some(existing) = existing {
        let Some(attempt_row) = fetch_attempt(session, existing.get("attempt_id"))? else {
            return fail("database_integrity", "Attempt command replay references a missing attempt");
        };
        let Some(turn_row) = fetch_turn(session, existing.get("turn_id"))? else {
            return fail("database_integrity", "Attempt command replay references a missing turn");
        };
        let deleted: Vec<Value> = session
            .fetch_all(
                "SELECT turn_id FROM storage_turn_tombstones WHERE conversation_id=? AND user_id=? AND deleted_at=?",
                &params![conv_id.clone(), user_id, attempt_row.int("created_at")?],
            )?
            .iter()
            .map(|row| Value::String(row.text("turn_id")))
            .collect();
        return Ok(json!({
            "turn": turn_public(session, &turn_row)?,
            "attempt": attempt_public(&attempt_row)?,
            "conversationRevision": conversation_revision(session, &conv_id, user_id)?,
            "streamCursor": 1,
            "idempotentReplay": true,
            "_needsStart": attempt_row.text("status") == "pending" && !attempt_row.get("task_id").truthy(),
            "deletedTurnIds": deleted,
        }));
    }
    let Some(turn) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id.clone(), user_id, turn_id.clone()],
    )?
    else {
        return fail("database_not_found", "Turn not found");
    };
    let public = turn_public(session, &turn)?;
    let public_revision = public["projectionRevision"].as_i64().unwrap_or(0);
    let (current_id, current) = attempt_source(session, &turn, &public, expected_revision)?;
    let anchor = attempt_resume_anchor(&public, &operation, get(payload, "resume_anchor"))?;
    let later_rows = attempt_later_turns(session, &turn)?;
    let discarded = if operation == "regenerate" { later_rows } else { RowSet::new() };
    match &acknowledged {
        None if !discarded.is_empty() => {
            return fail(
                "turn_later_turns_unacknowledged",
                "Later Turns would be discarded; acknowledge exactly which ones.",
            )
        }
        Some(ids) => {
            let mut sorted = ids.clone();
            sorted.sort();
            let closure: Vec<String> = discarded.keys().cloned().collect();
            if sorted != closure {
                return fail(
                    "turn_later_turns_changed",
                    "The later Turns changed since their discard was acknowledged.",
                );
            }
        }
        None => {}
    }
    let now = now_ms();
    let mut projection = if operation == "regenerate" {
        as_obj(&public["projection"])
    } else {
        attempt_resume_projection(session, &turn, &public, &current_id, current.as_ref())?
    };
    let settlement = as_obj(&public["settlement"]);
    let mut rolled_back = Obj::new();
    if operation != "regenerate" {
        let lane_continues =
            matches!(operation.as_str(), "continue" | "answer_guidance") && truthy(get(&projection, "content"));
        if operation == "checkpoint_resume" {
            if let Some(Value::String(content)) = projection.get("content") {
                if !content.is_empty() {
                    rolled_back.insert("content".into(), Value::String(content.clone()));
                }
            }
        }
        if !lane_continues {
            if let Some(Value::String(thinking)) = projection.get("thinking") {
                if !thinking.is_empty() {
                    rolled_back.insert("thinking".into(), Value::String(thinking.clone()));
                }
            }
        }
        if !rolled_back.is_empty() {
            let owner = if truthy(&current_id) { python_str(&current_id) } else { turn_id.clone() };
            let mut entry = Obj::new();
            entry.insert("blockId".into(), Value::String(format!("rolled-back:{owner}")));
            entry.insert("at".into(), Value::from(now));
            for (key, value) in &rolled_back {
                entry.insert(key.clone(), value.clone());
            }
            if truthy(&current_id) {
                entry.insert("attemptId".into(), current_id.clone());
            }
            let mut lane: Vec<Value> = match projection.get("rolledBack") {
                Some(Value::Array(items)) => items.iter().filter(|item| item.is_object()).cloned().collect(),
                _ => vec![],
            };
            lane.push(Value::Object(entry));
            let start = lane.len().saturating_sub(4);
            projection.insert("rolledBack".into(), Value::Array(lane[start..].to_vec()));
        }
    }
    let mut submitted = None;
    let mut submitted_changes = Vec::new();
    let input_update = get(payload, "input_update");
    if !input_update.is_null() {
        let parent_id = turn.get("parent_turn_id").clone();
        let parent = if parent_id.truthy() {
            session.fetch_one(
                "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
                &[SqlValue::Text(conv_id.clone()), SqlValue::Integer(user_id), parent_id.clone()],
            )?
        } else {
            None
        };
        let Some(parent) =
            parent.filter(|parent| matches!(parent.text("actor").as_str(), "human" | "virtual_user" | "critic"))
        else {
            return fail("database_conflict", "The generated turn has no editable submitted parent.");
        };
        let previous_input = projection_map(session, &parent)?;
        let next_input = if operation == "regenerate" {
            let expected = get(payload, "expected_input_projection_revision");
            if expected.is_null() || py_int(expected)? != parent.int("projection_revision")? {
                return fail("database_conflict", "The submitted turn changed since editing began.");
            }
            match input_update {
                Value::Object(update) => update.clone(),
                other => as_obj(&json!({"content": python_str(other)})),
            }
        } else {
            resume_ctx_rebound_projection(&previous_input, input_update)?
        };
        let parent_base = parent.int("projection_revision")?;
        let changed = session.execute(
            "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND projection_revision=?",
            &params![dump_param(&Value::Object(next_input.clone()))?, parent_base + 1, now, parent_id.clone(), parent_base],
        )?;
        if changed != 1 {
            return fail("database_conflict", "The submitted turn changed while regeneration was applied.");
        }
        let parent_turn = parent_id.text_or_empty();
        delete_checkpoint(session, &parent_turn)?;
        submitted_changes.push(projection_change(
            &parent_turn,
            &Value::Object(previous_input),
            &Value::Object(next_input),
            parent_base,
            parent_base + 1,
            now,
        ));
        let Some(parent) = fetch_turn(session, &parent_id)? else {
            return fail("database_integrity", "Submitted turn is missing");
        };
        upsert_turn_search_row(session, &parent, None)?;
        submitted = Some(turn_public(session, &parent)?);
    }
    let mut discarded_ids = Vec::new();
    if !discarded.is_empty() {
        discarded_ids = delete_turn_row_set(session, &conv_id, user_id, &discarded, now)?;
        prune_turn_tombstones(session, now)?;
    }
    if operation == "regenerate" {
        projection = as_obj(&json!({"content": "", "thinking": "", "segments": [], "toolRounds": []}));
    } else if operation == "checkpoint_resume" {
        let content = anchor.get("content").cloned().unwrap_or(json!(""));
        let thinking = anchor.get("thinking").cloned().unwrap_or(json!(""));
        let segments = anchor.get("segments").cloned().unwrap_or(json!([]));
        let Value::Array(segments) = segments else {
            return fail("database_protocol_error", "Invalid checkpoint projection anchor");
        };
        if !content.is_string() || !thinking.is_string() {
            return fail("database_protocol_error", "Invalid checkpoint projection anchor");
        }
        projection.insert("content".into(), json!(""));
        projection.insert("thinking".into(), json!(""));
        let kept = match anchor.get("keptToolRounds").cloned().unwrap_or(json!(0)) {
            Value::Number(number) => number.as_i64().filter(|value| *value >= 0),
            _ => None,
        };
        let Some(kept) = kept else {
            return fail("database_protocol_error", "Invalid checkpoint tool-round boundary");
        };
        let rounds = match projection.get("toolRounds").cloned().unwrap_or(json!([])) {
            Value::Array(rounds) => rounds,
            _ => return fail("database_protocol_error", "Invalid checkpoint tool-round projection"),
        };
        if kept as usize > rounds.len() {
            return fail("database_protocol_error", "Checkpoint tool-round boundary exceeds the projection");
        }
        match anchor.get("retainedToolRoundPositions").cloned().unwrap_or(Value::Null) {
            Value::Null => {
                projection.insert("toolRounds".into(), Value::Array(rounds[..kept as usize].to_vec()));
            }
            Value::Array(positions) => {
                let mut previous = -1i64;
                let mut selected = Vec::new();
                for position in positions {
                    let valid = match &position {
                        Value::Number(number) => number.as_i64().filter(|value| {
                            *value > previous && *value >= 0 && *value < kept && (*value as usize) < rounds.len()
                        }),
                        _ => None,
                    };
                    let Some(position) = valid else {
                        return fail("database_protocol_error", "Invalid checkpoint tool-round position");
                    };
                    selected.push(rounds[position as usize].clone());
                    previous = position;
                }
                let replayable = match anchor.get("replayableToolRounds").cloned().unwrap_or(json!(0)) {
                    Value::Number(number) => {
                        number.as_i64().filter(|value| *value >= 0 && (*value as usize) <= selected.len())
                    }
                    _ => None,
                };
                if replayable.is_none() {
                    return fail("database_protocol_error", "Checkpoint replayable tool-round count is inconsistent");
                }
                projection.insert("toolRounds".into(), Value::Array(selected));
            }
            _ => return fail("database_protocol_error", "Invalid checkpoint tool-round positions"),
        }
        projection.insert("segments".into(), Value::Array(segments));
    } else if truthy(get(&rolled_back, "thinking")) {
        projection.insert("thinking".into(), json!(""));
    }
    let mut accepted_config = if truthy(raw_config) { as_obj(raw_config) } else { Obj::new() };
    let cause = if truthy(get(&settlement, "cause")) { get(&settlement, "cause") } else { get(&settlement, "reason") };
    accepted_config.insert(
        "_resumeSource".into(),
        json!({
            "attemptId": if truthy(&current_id) { current_id.clone() } else { json!("") },
            "taskId": match &current { Some(row) => row.text("task_id"), None => String::new() },
            "status": match &current { Some(row) => row.get("status").text_or_empty(), None => String::new() },
            "cause": text(cause),
        }),
    );
    let new_revision = public_revision + 1;
    let attempt_id = uuid4();
    session.execute(
        "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,dispatch_mode,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            attempt_id.clone(),
            conv_id.clone(),
            turn_id.clone(),
            command_id,
            "",
            operation.clone(),
            dispatch_mode,
            "pending",
            public_revision,
            dump_param(&Value::Object(anchor))?,
            dump_param(&Value::Object(accepted_config))?,
            dump_param(&json!({}))?,
            now
        ],
    )?;
    let next_actor = if operation == "regenerate" { python_str(target_actor) } else { turn.text("actor") };
    let next_kind = if operation == "regenerate" { python_str(target_kind) } else { turn.text("kind") };
    session.execute(
        "UPDATE storage_conversation_turns SET status=?,current_attempt_id=?,projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,settlement_json=?,actor=?,kind=?,updated_at=? WHERE turn_id=? AND projection_revision=?",
        &params![
            "pending",
            attempt_id.clone(),
            dump_param(&Value::Object(projection.clone()))?,
            new_revision,
            dump_param(&json!({}))?,
            next_actor.clone(),
            next_kind.clone(),
            now,
            turn_id.clone(),
            public_revision
        ],
    )?;
    delete_checkpoint(session, &turn_id)?;
    let Some(attempt) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else {
        return fail("database_integrity", "Created attempt is missing");
    };
    let attempt_value = attempt_public(&attempt)?;
    let event = json!({
        "conversationId": conv_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": 1,
        "projectionRevision": new_revision,
        "type": "status_changed",
        "payload": {
            "status": "pending",
            "operation": operation,
            "projectionPatch": build_projection_patch(&public["projection"], &Value::Object(projection), public_revision, new_revision),
            "turnState": {
                "turnId": turn_id,
                "status": "pending",
                "actor": next_actor,
                "kind": next_kind,
                "currentAttemptId": attempt_id,
                "settlement": {},
                "updatedAt": now,
            },
            "attempts": [attempt_value.clone()],
        },
    });
    super::core::insert_attempt_event(
        session,
        &attempt_id,
        1,
        &conv_id,
        &turn_id,
        new_revision,
        "status_changed",
        &event,
        now,
        true,
    )?;
    let revision = match session.fetch_one(
        "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conv_id.clone(), user_id],
    )? {
        Some(row) => row.int("rev")? + 1,
        None => 1,
    };
    session.execute(
        "UPDATE storage_conversations SET rev=?, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![revision, now, conv_id, user_id],
    )?;
    let Some(turn) = fetch_turn(session, &SqlValue::Text(turn_id))? else {
        return fail("database_integrity", "Created attempt turn is missing");
    };
    let mut result = Obj::new();
    result.insert("turn".into(), turn_public(session, &turn)?);
    result.insert("attempt".into(), attempt_value);
    result.insert("conversationRevision".into(), Value::from(revision));
    result.insert("streamCursor".into(), json!(1));
    result.insert("idempotentReplay".into(), json!(false));
    result.insert("_needsStart".into(), json!(true));
    result.insert("_conversationSyncAttemptEvents".into(), Value::Array(vec![event]));
    result.insert("_conversationSyncTurnPatches".into(), Value::Array(submitted_changes));
    if let Some(submitted) = submitted {
        result.insert("submittedTurn".into(), submitted);
    }
    result.insert("deletedTurnIds".into(), json!(discarded_ids));
    Ok(Value::Object(result))
}

// ── storage/model_continuation.py::model_projections_equal ──────────────

const DISPLAY_ONLY_FIELDS: &[&str] = &[
    "_showingTranslation",
    "_translateDone",
    "_translateFailed",
    "_translateModel",
    "activityTimeline",
    "apiRounds",
    "cost",
    "fileChanges",
    "lastRoundUsage",
    "modifiedFileList",
    "modifiedFiles",
    "provenance",
    "timingTrace",
    "translatedContent",
    "translation",
    "usage",
];
const ROUND_DISPLAY_ONLY_FIELDS: &[&str] = &["_swarm", "_swarmSnapshot", "timingTrace", "translation", "usage"];

fn model_round(value: &Value) -> Value {
    match value {
        Value::Object(round) => Value::Object(
            round
                .iter()
                .filter(|(key, _)| !ROUND_DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// Private port of `model_continuation.model_projection_for_context`.
pub fn model_projection_for_context(projection: &Obj) -> Obj {
    let mut result: Obj = projection
        .iter()
        .filter(|(key, _)| !DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    if let Some(Value::Array(rounds)) = result.get("toolRounds") {
        let rounds = rounds.iter().map(model_round).collect();
        result.insert("toolRounds".into(), Value::Array(rounds));
    }
    if let Some(Value::Array(segments)) = result.get("segments") {
        let segments = segments
            .iter()
            .map(|segment| {
                let Value::Object(segment) = segment else { return segment.clone() };
                let translated =
                    matches!(segment.get("type"), Some(Value::String(kind)) if kind == "text" || kind == "thinking");
                let mut projected: Obj = segment
                    .iter()
                    .filter(|(key, _)| !(translated && key.as_str() == "translatedText"))
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();
                if let Some(round) = projected.get("_round").cloned() {
                    projected.insert("_round".into(), model_round(&round));
                }
                Value::Object(projected)
            })
            .collect();
        result.insert("segments".into(), Value::Array(segments));
    }
    result
}

/// `_same_json`: type-aware JSON equality.
pub fn same_json(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len() && a.iter().all(|(key, value)| b.get(key).is_some_and(|other| same_json(value, other)))
        }
        (Value::Array(a), Value::Array(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| same_json(x, y)),
        (Value::Number(a), Value::Number(b)) => match (a.is_f64(), b.is_f64()) {
            (false, false) => a == b,
            (true, true) => a.as_f64() == b.as_f64(),
            _ => false,
        },
        _ => left == right,
    }
}

/// `_turn_projection_update`.
pub fn turn_projection_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let mut expected = integer(payload, "expected_projection_revision", None, Some(0), None)?;
    let preserve_activity = match payload.get("preserve_activity") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "preserve_activity must be boolean"),
    };
    let select = "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?";
    let Some(mut row) = session.fetch_one(select, &params![conv_id.clone(), user_id, turn_id.clone()])? else {
        return fail("database_not_found", "Turn not found");
    };
    let answer = get(payload, "human_guidance_answer");
    let retirement = get(payload, "background_command_retirement");
    if !retirement.is_null() && !answer.is_null() {
        return fail("database_protocol_error", "A guidance answer cannot retire background receipts");
    }
    let mut projection = get(payload, "projection").clone();
    if !answer.is_null() {
        let Value::Object(answer) = answer else {
            return fail("database_invalid_payload", "Invalid human guidance answer");
        };
        let Some(reloaded) = session.fetch_one(select, &params![conv_id.clone(), user_id, turn_id.clone()])? else {
            return fail("database_integrity", "Guidance turn is missing");
        };
        row = reloaded;
        let previous = projection_map(session, &row)?;
        let rounds: Vec<Value> = match previous.get("toolRounds") {
            Some(Value::Array(rounds)) => rounds.clone(),
            Some(_) => return internal("toolRounds is not iterable"),
            None => vec![],
        };
        let guidance_id = get(answer, "guidanceId");
        let matches: Vec<usize> = rounds
            .iter()
            .enumerate()
            .filter(|(_, round)| matches!(round, Value::Object(r) if py_eq(get(r, "guidanceId"), guidance_id)))
            .map(|(index, _)| index)
            .collect();
        if matches.len() != 1 || rounds[matches[0]].get("toolName") != Some(&json!("ask_human")) {
            return fail("database_not_found", "Human guidance not ready");
        }
        let question = rounds[matches[0]].as_object().cloned().unwrap_or_default();
        let response = get(answer, "response");
        let command_id = get(answer, "commandId");
        let valid_response = matches!(response, Value::String(text) if !super::normalize::py_strip(text).is_empty() && text.chars().count() <= 8000);
        let valid_command =
            matches!(command_id, Value::String(text) if !text.is_empty() && text.chars().count() <= 256);
        if !valid_response || !valid_command {
            return fail("database_invalid_payload", "Invalid human guidance answer");
        }
        for accepted in &rounds {
            if let Value::Object(accepted) = accepted {
                if py_eq(get(accepted, "guidanceAnswerCommandId"), command_id)
                    && (!py_eq(get(accepted, "guidanceId"), guidance_id)
                        || !py_eq(get(accepted, "guidanceResponse"), response))
                {
                    return fail("database_conflict", "Human guidance command already accepted differently");
                }
            }
        }
        if truthy(get(&question, "guidanceAnswerCommandId")) {
            if !py_eq(get(&question, "guidanceResponse"), response) {
                return fail("database_conflict", "Human guidance already answered differently");
            }
            let Some(conversation) = session.fetch_one(
                "SELECT rev FROM storage_conversations WHERE id=? AND user_id=?",
                &params![conv_id.clone(), user_id],
            )?
            else {
                return fail("database_integrity", "Guidance conversation is missing");
            };
            return Ok(json!({
                "turn": turn_public(session, &row)?,
                "conversationRevision": conversation.int("rev")?,
                "idempotentReplay": true,
            }));
        }
        if !matches!(row.text("status").as_str(), "pending" | "running")
            || !value_in(get(&question, "status"), &["pending", "awaiting_human"])?
        {
            return fail("database_not_found", "Human guidance not ready");
        }
        let response = response.as_str().unwrap_or("").to_owned();
        require_declared_choice_response(&question)?;
        let mut next = previous.clone();
        let answered: Vec<Value> = rounds
            .iter()
            .enumerate()
            .map(|(index, round)| {
                if index == matches[0] {
                    Value::Object(answered_guidance_round(&question, &response, command_id.as_str().unwrap_or("")))
                } else {
                    round.clone()
                }
            })
            .collect();
        next.insert("toolRounds".into(), Value::Array(answered));
        projection = Value::Object(next);
        expected = row.int("projection_revision")?;
    }
    if answer.is_null() && matches!(row.text("status").as_str(), "pending" | "running") {
        return fail("turn_in_progress", "A running turn cannot be edited.");
    }
    if row.int("projection_revision")? != expected {
        return fail("turn_projection_stale", "The turn changed since editing began.");
    }
    let projection = match projection {
        Value::Object(map) => map,
        other => as_obj(&json!({"content": text(&other)})),
    };
    let previous = projection_map(session, &row)?;
    let mut display_offset = row.opt_int("model_context_display_offset").unwrap_or(0);
    if same_json(
        &Value::Object(model_projection_for_context(&previous)),
        &Value::Object(model_projection_for_context(&projection)),
    ) {
        display_offset += 1;
    }
    let now = now_ms();
    let revision = expected + 1;
    let changed = session.execute(
        "UPDATE storage_conversation_turns SET projection_json=?,projection_revision=?,model_context_display_offset=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND projection_revision=?",
        &params![dump_param(&Value::Object(projection.clone()))?, revision, display_offset, now, turn_id.clone(), expected],
    )?;
    if changed == 0 {
        return fail("turn_projection_stale", "The turn changed while the edit was applied.");
    }
    delete_checkpoint(session, &turn_id)?;
    let removed_queue_ids = if retirement.is_null() {
        None
    } else {
        Some(retire_background_command_receipts(session, &conv_id, user_id, retirement, &projection)?)
    };
    if preserve_activity {
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1 WHERE id=? AND user_id=?",
            &params![conv_id.clone(), user_id],
        )?;
    } else {
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            &params![now, conv_id.clone(), user_id],
        )?;
    }
    let Some(updated) = fetch_turn(session, &SqlValue::Text(turn_id.clone()))? else {
        return fail("database_integrity", "Updated turn is missing");
    };
    upsert_turn_search_row(session, &updated, None)?;
    let Some(conversation) = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?
    else {
        return fail("database_integrity", "Updated conversation is missing");
    };
    let mut result = Obj::new();
    result.insert("turn".into(), turn_public(session, &updated)?);
    result.insert("conversationRevision".into(), Value::from(conversation.int("rev")?));
    if let Some(removed_queue_ids) = removed_queue_ids {
        result.insert("removedQueueIds".into(), Value::Array(removed_queue_ids));
    }
    result.insert(
        "_conversationSyncTurnPatch".into(),
        projection_change(&turn_id, &Value::Object(previous), &Value::Object(projection), expected, revision, now),
    );
    Ok(Value::Object(result))
}

/// `human_gate_contract.require_declared_choice_response` (the empty
/// response branch is unreachable: callers validate it first).
fn require_declared_choice_response(question: &Obj) -> OpResult<()> {
    if question.get("guidanceType") != Some(&json!("choice")) {
        return Ok(());
    }
    let options: Option<Vec<Value>> = match question.get("guidanceOptions") {
        Some(Value::Array(declared)) => Some(
            declared
                .iter()
                .map(|option| match option {
                    Value::Object(option) => get(option, "label").clone(),
                    other => other.clone(),
                })
                .collect(),
        ),
        _ => None,
    };
    let valid = options.is_some_and(|options| {
        !options.is_empty() && options.iter().all(|option| matches!(option, Value::String(label) if !label.is_empty()))
    });
    if !valid {
        return fail("database_integrity", "Human guidance has no valid declared choices");
    }
    Ok(())
}

/// `human_gate_contract.answered_guidance_round`.
pub fn answered_guidance_round(question: &Obj, response: &str, command_id: &str) -> Obj {
    let mut result = question.clone();
    result.insert("guidanceResponse".into(), json!(response));
    result.insert("guidanceAnswerCommandId".into(), json!(command_id));
    result.insert("status".into(), json!("completed"));
    result.insert("resultSummary".into(), json!(response));
    result.insert("resultText".into(), json!(format!("Human response: {response}")));
    result.insert("toolContent".into(), json!(format!("Human response: {response}")));
    for key in ["results", "toolMeta"] {
        if let Some(Value::Array(entries)) = result.get_mut(key) {
            for entry in entries.iter_mut() {
                if let Value::Object(entry) = entry {
                    if py_eq(get(entry, "guidanceId"), get(question, "guidanceId")) {
                        entry.insert("userResponse".into(), json!(response));
                        entry.insert("waiting".into(), json!(false));
                        entry.insert("received".into(), json!(true));
                    }
                }
            }
        }
    }
    result
}

/// `human_gate_contract.preserve_guidance_answers`.
pub fn preserve_guidance_answers(previous: &Obj, candidate: &Obj) -> OpResult<Obj> {
    let rounds: Vec<Value> = match previous.get("toolRounds") {
        Some(Value::Array(rounds)) => rounds.clone(),
        None => vec![],
        Some(_) => return internal("toolRounds is not iterable"),
    };
    let mut accepted: Vec<(Value, Obj)> = Vec::new();
    for round in rounds {
        if let Value::Object(round) = round {
            if truthy(get(&round, "guidanceAnswerCommandId")) {
                let id = get(&round, "guidanceId").clone();
                if let Some(slot) = accepted.iter_mut().find(|(key, _)| py_eq(key, &id)) {
                    slot.1 = round;
                } else {
                    accepted.push((id, round));
                }
            }
        }
    }
    if accepted.is_empty() {
        return Ok(candidate.clone());
    }
    let mut result = candidate.clone();
    if !result.contains_key("toolRounds") {
        result.insert("toolRounds".into(), json!([]));
    }
    let Some(Value::Array(rounds)) = result.get_mut("toolRounds") else { return internal("toolRounds is not a list") };
    let mut present: Vec<Value> = Vec::new();
    for round in rounds.iter_mut() {
        let Value::Object(question) = round else { continue };
        let id = get(question, "guidanceId").clone();
        if let Some((_, receipt)) = accepted.iter().find(|(key, _)| py_eq(key, &id)) {
            let response = text(get(receipt, "guidanceResponse"));
            let command = text(get(receipt, "guidanceAnswerCommandId"));
            let answered = answered_guidance_round(question, &response, &command);
            *round = Value::Object(answered);
            present.push(id);
        }
    }
    for (id, receipt) in &accepted {
        if !present.iter().any(|value| py_eq(value, id)) {
            rounds.push(Value::Object(receipt.clone()));
        }
    }
    Ok(result)
}

/// `_turn_related_announce`.
pub fn turn_related_announce(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let ids = get(payload, "turn_ids");
    let turn_ids: Vec<String> = match ids {
        _ if !truthy(ids) => vec![],
        Value::Array(items) => items.iter().filter(|item| truthy(item)).map(python_str).collect(),
        _ => return internal("turn_ids is not iterable"),
    };
    if turn_ids.is_empty() {
        return Ok(json!(false));
    }
    let Some(attempt) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else { return Ok(json!(false)) };
    if !matches!(attempt.text("status").as_str(), "pending" | "running") {
        return Ok(json!(false));
    }
    let Some(root) = fetch_turn(session, attempt.get("turn_id"))? else { return Ok(json!(false)) };
    if root.int("user_id")? != user_id || root.get("current_attempt_id").as_str() != Some(attempt_id.as_str()) {
        return Ok(json!(false));
    }
    let mut related = Vec::new();
    let mut related_attempts = Vec::new();
    for turn_id in turn_ids {
        let row = session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND conversation_id=?",
            &params![turn_id, attempt.get("conversation_id")],
        )?;
        let Some(row) = row else { continue };
        related.push(turn_public(session, &row)?);
        if row.get("current_attempt_id").truthy() {
            if let Some(child) = fetch_attempt(session, row.get("current_attempt_id"))? {
                related_attempts.push(attempt_public(&child)?);
            }
        }
    }
    if related.is_empty() {
        return Ok(json!(false));
    }
    let now = now_ms();
    let root_projection = Value::Object(projection_map(session, &root)?);
    let base = root.int("projection_revision")?;
    let bridge = build_projection_patch(&root_projection, &root_projection, base, base + 1);
    let revision = advance_unchanged_revision(session, &root, &root_projection, &bridge, now)?;
    let root_id = root.text("turn_id");
    let mut event = Obj::new();
    event.insert("projectionPatch".into(), bridge);
    event.insert(
        "turns".into(),
        Value::Array(
            related.into_iter().filter(|item| item.get("turnId") != Some(&Value::String(root_id.clone()))).collect(),
        ),
    );
    event.insert("attempts".into(), Value::Array(related_attempts));
    event.insert("updateKind".into(), json!("related_turns_created"));
    let conversation_id = attempt.text("conversation_id");
    let appended =
        turn_event_append(session, &attempt_id, &conversation_id, &root_id, revision, "projection_updated", event)?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, conversation_id, root.get("user_id")],
    )?;
    Ok(json!({"changed": true, "_conversationSyncAttemptEvents": [appended["event"].clone()]}))
}

/// `_turn_attempt_bind`.
pub fn turn_attempt_bind(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let (legacy, claim, _) = dispatch_claims(payload, &attempt_id)?;
    let Some(attempt) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else { return Ok(Value::Null) };
    let Some(turn) = fetch_turn(session, attempt.get("turn_id"))? else { return Ok(Value::Null) };
    if turn.int("user_id")? != user_id || turn.get("current_attempt_id").as_str() != Some(attempt_id.as_str()) {
        return Ok(Value::Null);
    }
    let status = attempt.text("status");
    if status != "pending" && status != "running" {
        return Ok(Value::Null);
    }
    let existing = attempt.text("task_id");
    if !existing.is_empty() && existing != legacy && existing != claim && existing != task_id {
        return fail("database_conflict", "Generation attempt is already bound to a task");
    }
    if existing == task_id {
        return attempt_public(&attempt);
    }
    let now = now_ms();
    session.execute(
        "UPDATE storage_generation_attempts SET task_id=? WHERE attempt_id=? AND status IN ('pending','running') AND task_id=?",
        &params![task_id, attempt_id.clone(), existing],
    )?;
    let base = turn.int("projection_revision")?;
    let revision = base + 1;
    if status == "pending" {
        let changed = session.execute(
            "UPDATE storage_conversation_turns SET projection_revision=?, updated_at=? WHERE turn_id=? AND current_attempt_id=? AND status='pending' AND projection_revision=?",
            &params![revision, now, turn.get("turn_id"), attempt_id.clone(), base],
        )?;
        if changed != 1 {
            return fail("database_conflict", "Pending turn changed during task binding");
        }
    }
    let Some(row) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else {
        return fail("database_integrity", "Bound attempt is missing");
    };
    let mut result = attempt_public(&row)?;
    let mut events = Vec::new();
    if status == "pending" {
        let projection = Value::Object(projection_map(session, &turn)?);
        let mut event = Obj::new();
        event.insert("status".into(), json!("pending"));
        event.insert("dispatchState".into(), json!("queued"));
        event.insert("attempts".into(), Value::Array(vec![result.clone()]));
        event.insert("projectionPatch".into(), build_projection_patch(&projection, &projection, base, revision));
        let appended = turn_event_append(
            session,
            &attempt_id,
            &attempt.text("conversation_id"),
            &turn.text("turn_id"),
            revision,
            "status_changed",
            event,
        )?;
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            &params![now, turn.get("conversation_id"), turn.get("user_id")],
        )?;
        events.push(appended["event"].clone());
    }
    result["_conversationSyncAttemptEvents"] = Value::Array(events);
    Ok(result)
}

/// `_turn_attempt_start`.
pub fn turn_attempt_start(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let context = get(payload, "automation_context");
    if !context.is_null() && !start_context_is_valid(context) {
        return fail("database_protocol_error", "Invalid automation start context");
    }
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let Some(attempt) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else { return Ok(Value::Null) };
    let Some(turn) = fetch_turn(session, attempt.get("turn_id"))? else { return Ok(Value::Null) };
    if turn.int("user_id")? != user_id
        || turn.get("current_attempt_id").as_str() != Some(attempt_id.as_str())
        || attempt.text("task_id") != task_id
    {
        return Ok(Value::Null);
    }
    require_conversation_publication_claim(
        session,
        &attempt_id,
        &task_id,
        user_id,
        get(payload, "worker_claim"),
        false,
        false,
    )?;
    let witness = match payload.get("worker_process") {
        Some(process) => Some(execution_witness(process, get(payload, "worker_claim"))?),
        None => None,
    };
    let mut config = loaded_obj_or_empty(attempt.get("config_json"))?;
    if attempt.text("status") == "running" {
        if let Some(witness) = &witness {
            if !py_eq(config.get(WORKER_EXECUTION_CONFIG_KEY).unwrap_or(&Value::Null), witness) {
                return fail("database_conflict", "Worker entry identity cannot change after entry");
            }
        }
        return attempt_public(&attempt);
    }
    if attempt.text("status") != "pending" || turn.text("status") != "pending" {
        return Ok(Value::Null);
    }
    let now = now_ms();
    if let Some(witness) = witness {
        config.insert(WORKER_EXECUTION_CONFIG_KEY.into(), witness);
    }
    let attempt_changed = session.execute(
        "UPDATE storage_generation_attempts SET status='running', started_at=COALESCE(started_at,?),config_json=? WHERE attempt_id=? AND task_id=? AND status='pending'",
        &params![now, dump_param(&Value::Object(config))?, attempt_id.clone(), task_id.clone()],
    )?;
    let base = turn.int("projection_revision")?;
    let revision = base + 1;
    let turn_changed = session.execute(
        "UPDATE storage_conversation_turns SET status='running', projection_revision=?, updated_at=? WHERE turn_id=? AND current_attempt_id=? AND status='pending' AND projection_revision=?",
        &params![revision, now, turn.get("turn_id"), attempt_id.clone(), base],
    )?;
    if attempt_changed != 1 || turn_changed != 1 {
        return fail("database_conflict", "Pending attempt changed during worker entry");
    }
    let Some(row) = fetch_attempt(session, &SqlValue::Text(attempt_id.clone()))? else {
        return fail("database_integrity", "Started attempt is missing");
    };
    let mut result = attempt_public(&row)?;
    let projection = Value::Object(projection_map(session, &turn)?);
    let mut event = Obj::new();
    event.insert("status".into(), json!("running"));
    event.insert("dispatchState".into(), json!("running"));
    event.insert("attempts".into(), Value::Array(vec![result.clone()]));
    event.insert("projectionPatch".into(), build_projection_patch(&projection, &projection, base, revision));
    let appended = turn_event_append(
        session,
        &attempt_id,
        &attempt.text("conversation_id"),
        &turn.text("turn_id"),
        revision,
        "status_changed",
        event,
    )?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, turn.get("conversation_id"), turn.get("user_id")],
    )?;
    if !context.is_null() {
        let start = TaskStart {
            attempt_id: &attempt_id,
            task_id: &task_id,
            conversation_id: &attempt.text("conversation_id"),
            turn_id: &turn.text("turn_id"),
            started_at_ms: row.int("started_at")?,
        };
        enqueue_task_started(session, user_id, context, &start, now)?;
    }
    result["_conversationSyncAttemptEvents"] = json!([appended["event"].clone()]);
    Ok(result)
}
