//! `operations_pkg/_turns_lifecycle.py::_turn_recover` with private ports of
//! what it computes inside the transaction: the restart verdict of
//! `lib/turn_lifecycle.py::_settlement` (resume options from the tool-round
//! replay scan) and `lib/tools/effect_interruption.py`.

use std::collections::{BTreeSet, HashMap};

use serde_json::{json, Map, Value};

use super::core::{insert_attempt_event, now_ms, upsert_turn_search_row, Obj};
use super::head::{
    delete_checkpoint, discard_projection_cache_for_row, head_state, projection_from_turn_row,
    PROJECTION_CACHE_CHARGE_MULTIPLIER,
};
use super::patch::build_projection_patch;
use super::replay::{checkpoint_retention_positions, scan_replayable_tool_round_prefix};
use crate::codec::{dump_param, load, STORAGE_PROJECTION_CODEC_KEY};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, Payload};
use crate::session::{Row, Session, SqlValue};

/// `projection_codec.STORAGE_PROJECTION_MAX_HYDRATION_RATIO`.
const STORAGE_PROJECTION_MAX_HYDRATION_RATIO: i64 = 2;
/// `result_envelope.tool_error_next_action` keeps at most this many characters.
const TOOL_ERROR_NEXT_ACTION_MAX_CHARS: usize = 300;
const INTERRUPTED_WRITE_MESSAGE: &str = "The producing executor was retired before a tool response was captured. File publication remains unknown. This is an interruption observation, not a handler response, publication proof, task completion or approval.";

/// `projection_hydration_byte_upper_bound(value)` for a SQLite column.
fn projection_hydration_byte_upper_bound(value: &SqlValue) -> i64 {
    let (length, has_codec) = match value {
        SqlValue::Null => return 0,
        SqlValue::Blob(bytes) => (
            bytes.len(),
            bytes.windows(STORAGE_PROJECTION_CODEC_KEY.len()).any(|w| w == STORAGE_PROJECTION_CODEC_KEY.as_bytes()),
        ),
        SqlValue::Text(text) => (text.len(), text.contains(STORAGE_PROJECTION_CODEC_KEY)),
        SqlValue::Integer(integer) => (integer.to_string().len(), false),
        SqlValue::Real(real) => (crate::json::orjson_float(*real).len(), false),
    };
    length as i64 * if has_codec { STORAGE_PROJECTION_MAX_HYDRATION_RATIO } else { 1 }
}

/// `_turn_recovery_projection_budget(row)`.
fn recovery_projection_budget(row: &Row) -> OpResult<i64> {
    let head = head_state(row)?;
    let base_bytes = if head.checkpoint_active() {
        match row.get("projection_checkpoint_bytes") {
            SqlValue::Integer(bytes) if *bytes >= 1 => bytes * STORAGE_PROJECTION_MAX_HYDRATION_RATIO,
            _ => return fail("database_integrity", "Turn projection checkpoint byte evidence is missing"),
        }
    } else {
        projection_hydration_byte_upper_bound(row.get("projection_json"))
    };
    Ok(base_bytes + head.patch_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER)
}

fn lower_hex(value: Option<&Value>, size: usize) -> bool {
    matches!(value, Some(Value::String(text))
        if text.chars().count() == size && text.chars().all(|c| matches!(c, '0'..='9' | 'a'..='f')))
}

/// `effect_interruption.record_interrupted_file_effects(projection, ...)`.
fn record_interrupted_file_effects(
    projection: &mut Obj,
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
    task_id: &str,
) {
    if [conversation_id, turn_id, attempt_id, task_id].iter().any(|value| value.is_empty()) {
        return;
    }
    let Some(Value::Array(rounds)) = projection.get_mut("toolRounds") else { return };
    let mut counts: HashMap<String, usize> = HashMap::new();
    for row in rounds.iter() {
        if let Some(Value::Object(effect)) = row.get("executionEffect") {
            if let Some(Value::String(operation)) = effect.get("operationId") {
                *counts.entry(operation.clone()).or_default() += 1;
            }
        }
    }
    let text_is =
        |value: Option<&Value>, expected: &str| matches!(value, Some(Value::String(text)) if text == expected);
    for raw in rounds.iter_mut() {
        let Value::Object(row) = raw else { continue };
        let model_content_ok = match row.get("toolModelContent") {
            None | Some(Value::Null) => true,
            Some(Value::Array(items)) => items.is_empty(),
            _ => false,
        };
        if !text_is(row.get("toolName"), "write_file")
            || !text_is(row.get("taskId"), task_id)
            || !text_is(row.get("attemptId"), attempt_id)
            || row.get("toolContent").is_some_and(|content| !content.is_null())
            || !model_content_ok
            || row.get("_providerAttemptDiscarded") == Some(&Value::Bool(true))
        {
            continue;
        }
        let Some(Value::Object(effect)) = row.get("executionEffect") else { continue };
        if !text_is(effect.get("scope"), "attempt")
            || !text_is(effect.get("recovery"), "reconcile")
            || !(text_is(effect.get("state"), "entered") || text_is(effect.get("state"), "unknown"))
        {
            continue;
        }
        let operation = effect.get("operationId");
        let (Some(Value::Object(reference)), Some(Value::Object(publication))) =
            (effect.get("inspection"), effect.get("reconciliation"))
        else {
            continue;
        };
        let operation_text = match operation {
            Some(Value::String(text)) => text.clone(),
            _ => continue,
        };
        if !lower_hex(operation, 32)
            || counts.get(&operation_text) != Some(&1)
            || !text_is(reference.get("operationId"), &operation_text)
            || !text_is(reference.get("conversationId"), conversation_id)
            || !text_is(reference.get("turnId"), turn_id)
            || !lower_hex(reference.get("ownerScope"), 24)
            || !text_is(publication.get("kind"), "project_file_write")
            || !text_is(publication.get("taskId"), task_id)
        {
            continue;
        }
        let arguments = format!(
            "{{\"operation_id\":{},\"turn_id\":{}}}",
            serde_json::to_string(&operation_text).expect("string"),
            serde_json::to_string(turn_id).expect("string")
        );
        let action = format!("Call inspect_tool_effect({arguments}). Do not repeat the write automatically.");
        if action.chars().count() > TOOL_ERROR_NEXT_ACTION_MAX_CHARS {
            continue;
        }
        let content = format!(
            "{{\"code\":\"tool_effect_unknown\",\"message\":{},\"nextAction\":{},\"retryable\":false,\"status\":\"error\"}}",
            serde_json::to_string(INTERRUPTED_WRITE_MESSAGE).expect("string"),
            serde_json::to_string(&action).expect("string")
        );
        let mut next_effect = effect.clone();
        next_effect.insert("state".into(), json!("unknown"));
        row.insert("toolContent".into(), Value::String(content));
        row.insert("status".into(), json!("aborted"));
        row.insert("executionEffect".into(), Value::Object(next_effect));
    }
}

/// `str(value or '')`.
fn str_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(value) if truthy(value) => python_str(value),
        _ => String::new(),
    }
}

/// `_settlement(task, {'type': 'aborted', 'finishReason': 'interrupted'},
/// projection)` for a task aborted by `server_shutdown` with no error: the
/// verdict is the constant restart interruption, only resume options vary.
fn restart_settlement(projection: &Obj) -> Value {
    let raw_rounds = match projection.get("toolRounds") {
        Some(value) if truthy(value) => value.clone(),
        _ => json!([]),
    };
    let prefix = scan_replayable_tool_round_prefix(&raw_rounds);
    let content_tail = projection.get("content").is_some_and(truthy);
    let restart_interrupted = prefix.blocked_position.is_none();
    let mut options = Vec::new();
    if !content_tail && (prefix.replayable > 0 || projection.get("thinking").is_some_and(truthy) || restart_interrupted)
    {
        options.push(json!({"operation": "continue", "anchor": {"type": "replay_only", "contentChars": 0}}));
    }
    if prefix.replayable > 0 || content_tail {
        let (kept, retained) = checkpoint_retention_positions(&raw_rounds, &prefix);
        options.push(json!({
            "operation": "checkpoint_resume",
            "anchor": {
                "type": "tool_checkpoint",
                "keptToolRounds": kept,
                "replayableToolRounds": prefix.replayable,
                "retainedToolRoundPositions": retained,
                "content": "", "thinking": "", "segments": [],
            },
        }));
    }
    if let Some(position) = prefix.blocked_position.filter(|_| prefix.blocked_reason == "missing_tool_result") {
        let gap = raw_rounds.as_array().and_then(|rounds| rounds.get(position));
        if let Some(Value::Object(gap)) = gap {
            let guidance_ok = matches!(gap.get("guidanceId"), Some(Value::String(id)) if !id.is_empty());
            if gap.get("toolName") == Some(&json!("ask_human"))
                && gap.get("status") == Some(&json!("awaiting_human"))
                && guidance_ok
            {
                let (kept, retained) = checkpoint_retention_positions(&raw_rounds, &prefix);
                let response_type = match gap.get("guidanceType") {
                    Some(value) if truthy(value) => python_str(value),
                    _ => "free_text".to_owned(),
                };
                options.push(json!({
                    "operation": "answer_guidance",
                    "anchor": {
                        "type": "human_guidance",
                        "guidanceId": gap["guidanceId"].clone(),
                        "toolCallId": str_or_empty(gap.get("toolCallId")),
                        "question": str_or_empty(gap.get("guidanceQuestion")),
                        "responseType": response_type,
                        "roundPosition": position,
                        "keptToolRounds": kept,
                        "retainedToolRoundPositions": retained,
                    },
                }));
            }
        }
    }
    options.push(json!({"operation": "regenerate", "anchor": {"type": "turn_start"}}));
    json!({
        "outcome": "interrupted",
        "cause": "server_restart",
        "evidence": "system_recovery",
        "streamState": null,
        "providerFinishReason": "interrupted",
        "error": null,
        "resumeOptions": options,
    })
}

/// `_turn_recover`.
#[allow(clippy::too_many_lines)]
pub fn turn_recover(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let now = now_ms();
    let max_rows = integer(payload, "max_rows", Some(8), Some(1), Some(500))?;
    let max_bytes = integer(payload, "max_bytes", Some(2_000_000), Some(1), Some(8 * 1024 * 1024))?;
    let created_before_ms = if get(payload, "created_before_ms").is_null() {
        None
    } else {
        Some(integer(payload, "created_before_ms", None, Some(0), None)?)
    };
    let exclude_task_ids: BTreeSet<String> = match get(payload, "exclude_task_ids") {
        Value::Array(items) => items.iter().filter(|item| truthy(item)).map(python_str).collect(),
        _ => BTreeSet::new(),
    };
    let mut sql = String::from(
        "SELECT a.*,t.user_id,t.current_attempt_id,t.projection_json,t.projection_revision,t.projection_checkpoint_revision,t.projection_materialized_revision,t.projection_patch_count,t.projection_patch_bytes,cp.projection_bytes AS projection_checkpoint_bytes FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id AND t.current_attempt_id=a.attempt_id LEFT JOIN storage_turn_projection_checkpoints cp ON cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id AND cp.user_id=t.user_id AND cp.attempt_id=t.current_attempt_id AND cp.projection_revision=t.projection_checkpoint_revision WHERE a.status IN ('pending','running') AND COALESCE(a.task_id,'') NOT LIKE 'conversation-attempt:%'",
    );
    let mut args = Vec::new();
    if let Some(created_before_ms) = created_before_ms {
        sql.push_str(" AND a.created_at < ?");
        args.push(SqlValue::Integer(created_before_ms));
    }
    let rows = session.fetch_all(&sql, &args)?;
    let mut recovered = 0i64;
    let mut remaining = 0i64;
    let mut chunk_rows = 0i64;
    let mut chunk_bytes = 0i64;
    let mut recovered_events: Vec<Value> = Vec::new();
    for row in &rows {
        if !exclude_task_ids.is_empty() && exclude_task_ids.contains(&row.text("task_id")) {
            continue;
        }
        let projection_len = recovery_projection_budget(row)?;
        if chunk_rows > 0 && (chunk_rows >= max_rows || chunk_bytes + projection_len > max_bytes) {
            remaining += 1;
            continue;
        }
        let mut projection = match projection_from_turn_row(session, row)? {
            Value::Object(map) => map,
            _ => return internal("projection is not a mapping"),
        };
        let identity = |column: &str| row.get(column).as_str().unwrap_or("").to_owned();
        record_interrupted_file_effects(
            &mut projection,
            &identity("conversation_id"),
            &identity("turn_id"),
            &identity("attempt_id"),
            &identity("task_id"),
        );
        let config = load(row.get("config_json"))?;
        if truthy(&config) && !config.is_object() {
            return internal("AttributeError: config has no get");
        }
        let settlement = restart_settlement(&projection);
        let old_revision = row.int("projection_revision")?;
        let new_revision = old_revision + 1;
        let attempt_id = row.get("attempt_id").clone();
        let changed = session.execute(
            "UPDATE storage_generation_attempts SET status='interrupted', settled_at=? WHERE attempt_id=? AND status IN ('pending','running')",
            &params![now, attempt_id.clone()],
        )?;
        if changed == 0 {
            continue;
        }
        let projection_value = Value::Object(projection);
        let turn_changed = session.execute(
            "UPDATE storage_conversation_turns SET status='interrupted',projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,settlement_json=?,updated_at=? WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
            &params![
                dump_param(&projection_value)?,
                new_revision,
                dump_param(&settlement)?,
                now,
                row.get("turn_id"),
                attempt_id.clone(),
                old_revision
            ],
        )?;
        if turn_changed != 1 {
            return fail("database_conflict", "Turn changed while restart recovery was materialized");
        }
        let turn_id = row.text("turn_id");
        delete_checkpoint(session, &turn_id)?;
        discard_projection_cache_for_row(session, row)?;
        let Some(sequence) = session.fetch_one(
            "SELECT COALESCE(MAX(sequence),0) AS n FROM storage_attempt_events WHERE attempt_id=?",
            std::slice::from_ref(&attempt_id),
        )?
        else {
            return internal("aggregate returned no row");
        };
        let seq = 1 + sequence.int("n")?;
        let mut event = Map::new();
        event.insert("conversationId".into(), row.get("conversation_id").to_json()?);
        event.insert("turnId".into(), row.get("turn_id").to_json()?);
        event.insert("attemptId".into(), attempt_id.to_json()?);
        event.insert("seq".into(), Value::from(seq));
        event.insert("projectionRevision".into(), Value::from(new_revision));
        event.insert("type".into(), json!("terminal_settlement"));
        event.insert(
            "payload".into(),
            json!({
                "status": "interrupted",
                "settlement": settlement,
                "projectionPatch": build_projection_patch(&projection_value, &projection_value, old_revision, new_revision),
            }),
        );
        let event = Value::Object(event);
        insert_attempt_event(
            session,
            &row.text("attempt_id"),
            seq,
            &row.text("conversation_id"),
            &turn_id,
            new_revision,
            "terminal_settlement",
            &event,
            now,
            false,
        )?;
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            &params![now, row.get("conversation_id"), row.get("user_id")],
        )?;
        let Some(recovered_turn) =
            session.fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", &params![turn_id])?
        else {
            return internal("TypeError: recovered turn is missing");
        };
        upsert_turn_search_row(session, &recovered_turn, None)?;
        recovered_events.push(event);
        recovered += 1;
        chunk_rows += 1;
        chunk_bytes += projection_len;
    }
    Ok(json!({
        "recovered": recovered,
        "remaining": remaining,
        "_conversationSyncAttemptEvents": recovered_events,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn effect_row(operation: &str) -> Value {
        json!({
            "toolName": "write_file", "taskId": "task", "attemptId": "att", "toolContent": null,
            "executionEffect": {"scope": "attempt", "recovery": "reconcile", "state": "entered",
                "operationId": operation,
                "inspection": {"operationId": operation, "conversationId": "conv", "turnId": "t\"1",
                    "ownerScope": "0123456789abcdef01234567"},
                "reconciliation": {"kind": "project_file_write", "taskId": "task"}},
        })
    }

    #[test]
    fn interrupted_writes_get_the_python_model_text() {
        let operation = "0123456789abcdef0123456789abcdef";
        let mut projection = json!({"toolRounds": [effect_row(operation)]}).as_object().cloned().unwrap();
        record_interrupted_file_effects(&mut projection, "conv", "t\"1", "att", "task");
        let row = &projection["toolRounds"][0];
        assert_eq!(row["status"], "aborted");
        assert_eq!(row["executionEffect"]["state"], "unknown");
        let expected = concat!(
            "{\"code\":\"tool_effect_unknown\",\"message\":\"The producing executor was retired before a tool ",
            "response was captured. File publication remains unknown. This is an interruption observation, not a ",
            "handler response, publication proof, task completion or approval.\",\"nextAction\":\"Call ",
            "inspect_tool_effect({\\\"operation_id\\\":\\\"0123456789abcdef0123456789abcdef\\\",\\\"turn_id\\\":",
            "\\\"t\\\\\\\"1\\\"}). Do not repeat the write automatically.\",\"retryable\":false,\"status\":\"error\"}"
        );
        assert_eq!(row["toolContent"], expected);
        let mut duplicated =
            json!({"toolRounds": [effect_row(operation), effect_row(operation)]}).as_object().cloned().unwrap();
        record_interrupted_file_effects(&mut duplicated, "conv", "t\"1", "att", "task");
        assert!(duplicated["toolRounds"][0]["toolContent"].is_null());
    }

    #[test]
    fn restart_settlement_matches_python_resume_options() {
        let empty = Obj::new();
        let options = restart_settlement(&empty)["resumeOptions"].clone();
        assert_eq!(options[0]["operation"], "continue");
        assert_eq!(options[1]["operation"], "regenerate");
        let guided = json!({"toolRounds": [
            {"toolCallId": "a", "toolName": "b", "toolContent": "r"},
            {"toolCallId": "q", "toolName": "ask_human", "status": "awaiting_human", "guidanceId": "g"},
        ]});
        let settlement = restart_settlement(guided.as_object().unwrap());
        let operations: Vec<_> =
            settlement["resumeOptions"].as_array().unwrap().iter().map(|o| o["operation"].clone()).collect();
        assert_eq!(
            operations,
            vec![json!("continue"), json!("checkpoint_resume"), json!("answer_guidance"), json!("regenerate")]
        );
        assert_eq!(settlement["resumeOptions"][2]["anchor"]["responseType"], "free_text");
    }
}
