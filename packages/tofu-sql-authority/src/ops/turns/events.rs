//! `operations_pkg/_turns_events.py::_turn_event_record` plus the private
//! model-continuation settlement it performs in the terminal transaction.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::background::acknowledge_background_command_receipt;
use super::core::{
    as_obj, attempt_public, now_ms, payload_now, py_int, text, text_or, turn_event_append, turn_public,
    upsert_turn_search_row, Obj,
};
use super::head::{
    delete_checkpoint, fold_head, head_state, load_base, plan_head_append, update_projection_row, upsert_checkpoint,
    HeadState, PROJECTION_CACHE_CHARGE_MULTIPLIER, PROJECTION_INLINE_LIVE_MAX_BYTES,
};
use super::normalize::py_json_dumps;
use super::patch::{apply_projection_patch, build_projection_patch, is_text_projection_patch};
use super::segments::projection_with_stable_segments;
use super::taskevents::append_event_row;
use super::trace::merge_client_trace_evidence;
use super::worker::{require_conversation_publication_claim, starts_tool_effect};
use super::write::{model_projection_for_context, preserve_guidance_answers};
use crate::attempt_event_metrics;
use crate::codec::{dump, dump_param, load, py_eq};
use crate::error::{fail, Failure, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};
use crate::turn_projection_cache::{projection_cache_key, projection_text_bytes};
use std::sync::Arc;

/// `TurnProjectionWriteResult`: storage/cache evidence of one revision write.
struct ProjectionWrite {
    changed: usize,
    projection_bytes: i64,
    cache_charge_bytes: i64,
    stored_payload_bytes: Option<i64>,
    stored_matches_projection: bool,
    /// `Some(bytes)` when the projection BLOB assignment was skipped.
    blob_write_skipped_bytes: Option<i64>,
    blob_write_deferred: bool,
    /// `Some((checkpoint_bytes, inline_released_bytes))` once materialized.
    checkpoint_materialized: Option<(i64, i64)>,
}

fn not_applied() -> OpResult<Value> {
    Ok(json!({"applied": false}))
}

fn loaded_obj(value: &SqlValue) -> OpResult<Obj> {
    let loaded = load(value)?;
    match loaded {
        _ if !truthy(&loaded) => Ok(Obj::new()),
        Value::Object(map) => Ok(map),
        _ => crate::error::internal("stored document has no get"),
    }
}

/// `turn.event.record` handler.
pub fn turn_event_record(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    event_record(session, payload, false)
}

#[allow(clippy::too_many_lines)]
pub(super) fn event_record(session: &Session<'_>, payload: &Payload, settling_flow_parent: bool) -> OpResult<Value> {
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let background_ack = get(payload, "background_command_ack");
    let task_event = get(payload, "task_event");
    if truthy(get(payload, "terminal")) || !background_ack.is_null() {
        let identity = session.fetch_one(
            "SELECT a.conversation_id FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=?",
            &params![attempt_id.clone(), user_id],
        )?;
        if let Some(identity) = identity {
            let key = format!("{user_id}:{}", identity.text("conversation_id"));
            session.lock_key("conversation", &key);
            session.lock_key("turn_conversation", &key);
            if !background_ack.is_null() {
                session.lock_key("queue-conversation", &key);
            }
        }
    }
    let event_payload_hint = get(payload, "event_payload");
    let require_unbound =
        event_payload_hint.as_object().and_then(|map| map.get("_requireUnboundAttempt")).unwrap_or(&Value::Bool(false));
    if require_unbound != &Value::Bool(false) {
        if require_unbound != &Value::Bool(true)
            || !truthy(get(payload, "terminal"))
            || get(payload, "status") != &json!("failed")
        {
            return fail("database_protocol_error", "Unbound fence requires a startup failure");
        }
        session.lock_key("attempt_dispatch", &attempt_id);
        session.lock_key("turn_attempt", &attempt_id);
    }
    let flow_root_hint = text(get(payload, "flow_root_attempt_id"));
    let Some(attempt) = session
        .fetch_one("SELECT * FROM storage_generation_attempts WHERE attempt_id=?", &params![attempt_id.clone()])?
    else {
        return not_applied();
    };
    if !matches!(attempt.text("status").as_str(), "pending" | "running") {
        return not_applied();
    }
    if truthy(require_unbound)
        && (attempt.text("status") != "pending"
            || attempt.get("task_id").truthy()
            || attempt.get("queue_state").truthy())
    {
        return not_applied();
    }
    let flow_root = text(get(&loaded_obj(attempt.get("config_json"))?, "flowRootAttemptId"));
    if flow_root != flow_root_hint {
        return not_applied();
    }
    let mut parent: Option<Row> = None;
    if !flow_root.is_empty() {
        parent = session.fetch_one(
            "SELECT a.status,a.task_id,t.current_attempt_id,t.user_id,a.conversation_id FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id WHERE a.attempt_id=?",
            &params![flow_root.clone()],
        )?;
        let valid = match &parent {
            Some(parent) => {
                parent.int("user_id")? == user_id
                    && parent.get("conversation_id") == attempt.get("conversation_id")
                    && parent.get("current_attempt_id").as_str() == Some(flow_root.as_str())
                    && (matches!(parent.text("status").as_str(), "pending" | "running") || settling_flow_parent)
            }
            None => false,
        };
        if !valid {
            return not_applied();
        }
    }
    let Some(turn) = session.fetch_one(
        "SELECT turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_revision,projection_checkpoint_revision,projection_materialized_revision,projection_patch_count,projection_patch_bytes,settlement_json,created_at,updated_at FROM storage_conversation_turns WHERE turn_id=?",
        &[attempt.get("turn_id").clone()],
    )?
    else {
        return not_applied();
    };
    if turn.int("user_id")? != user_id || turn.get("current_attempt_id").as_str() != Some(attempt_id.as_str()) {
        return not_applied();
    }
    let starts_effect = match task_event {
        Value::Object(carrier) => {
            let mut starts = starts_tool_effect(get(carrier, "event"));
            if !starts {
                for row in iterable(get(carrier, "preceding_events"))? {
                    if let Value::Object(row) = row {
                        if starts_tool_effect(get(&row, "event")) {
                            starts = true;
                            break;
                        }
                    }
                }
            }
            starts
        }
        _ => false,
    };
    let claim_task = match &parent {
        Some(parent) if !flow_root.is_empty() => parent.text("task_id"),
        _ => attempt.text("task_id"),
    };
    let terminal = truthy(get(payload, "terminal"));
    require_conversation_publication_claim(
        session,
        if flow_root.is_empty() { &attempt_id } else { &flow_root },
        &claim_task,
        user_id,
        get(payload, "worker_claim"),
        terminal && !starts_effect,
        !starts_effect,
    )?;
    let now = payload_now(payload)?;
    let old_revision = turn.int("projection_revision")?;
    let new_revision = old_revision + 1;
    let head = head_state(&turn)?;
    let status = text_or(get(payload, "status"), if terminal { "failed" } else { "running" });
    let mut started_by_event = false;
    if !terminal && attempt.text("status") == "pending" {
        let task_id = required_text(payload, "task_id", 256)?;
        let bound = attempt.text("task_id");
        if bound.is_empty() || bound != task_id {
            return not_applied();
        }
        let started = session.execute(
            "UPDATE storage_generation_attempts SET status='running', started_at=COALESCE(started_at,?) WHERE attempt_id=? AND task_id=? AND status='pending'",
            &params![now, attempt_id.clone(), task_id],
        )?;
        if started != 1 {
            return not_applied();
        }
        started_by_event = true;
    }
    let cache = session.turn_projection_cache;
    let cache_key = projection_cache_key(
        session.backend,
        user_id,
        &attempt.text("conversation_id"),
        &turn.text("turn_id"),
        &attempt_id,
    );
    let cache_entry = cache.and_then(|cache| cache.get(&cache_key, old_revision));
    let stored: Value;
    let stored_is_mapping: bool;
    let mut stored_matches_previous: bool;
    let stored_payload_bytes: Option<i64>;
    let mut previous_has_stable_segments: bool;
    let cache_charge_bytes: i64;
    if let Some(entry) = cache_entry {
        stored = (*entry.projection).clone();
        stored_is_mapping = true;
        stored_matches_previous = entry.stored_matches_projection;
        stored_payload_bytes = entry.stored_payload_bytes;
        previous_has_stable_segments = entry.stable_segments;
        cache_charge_bytes = entry.charge_bytes;
    } else {
        let base = match load_base(session, &turn, &head, false) {
            Ok(base) => base,
            Err(Failure::Storage(error)) if error.code == "database_conflict" => {
                if started_by_event {
                    return fail("database_conflict", "Turn changed while its first worker event was recorded");
                }
                return not_applied();
            }
            Err(other) => return Err(other),
        };
        if head.active() && !base.projection.is_object() {
            return fail("database_integrity", "Turn projection patch head has a non-object base");
        }
        stored = if head.active() { fold_head(session, &turn, &base.projection, &head)? } else { base.projection };
        stored_is_mapping = stored.is_object();
        stored_matches_previous = stored_is_mapping && !head.active();
        stored_payload_bytes = Some(base.payload_bytes);
        previous_has_stable_segments = false;
        cache_charge_bytes = ((base.payload_bytes + head.patch_bytes) * PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1);
    }
    // `current_stored_payload_bytes()`: reload the base only when a cache
    // entry carried no physical byte evidence.
    let current_stored_payload_bytes = || -> OpResult<i64> {
        match stored_payload_bytes {
            Some(bytes) => Ok(bytes),
            None => Ok(load_base(session, &turn, &head, false)?.payload_bytes),
        }
    };
    let mut previous = if stored_is_mapping { stored.clone() } else { json!({}) };
    let wire_previous = previous.clone();
    let wire_matches_stored = stored_matches_previous;
    let mut normalization_changed = false;
    let projection_patch = get(payload, "projection_patch");
    let segments_stable = match payload.get("projection_segments_stable") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Projection stable-segment evidence must be a boolean"),
    };
    if segments_stable && projection_patch.is_null() {
        return fail("database_protocol_error", "Projection stable-segment evidence requires a projection patch");
    }
    let mut replay: Option<Value> = None;
    let mut text_patch = false;
    let mut projection: Value;
    if !projection_patch.is_null() {
        if truthy(get(payload, "slim")) {
            return fail("database_protocol_error", "Slim turn events cannot carry a projection patch");
        }
        text_patch = !terminal
            && status == "running"
            && !segments_stable
            && background_ack.is_null()
            && is_text_projection_patch(projection_patch);
        if !text_patch && !previous_has_stable_segments {
            let actor = text_or(&turn.get("actor").to_json()?, "assistant");
            let turn_status = text(&turn.get("status").to_json()?);
            let normalized = Value::Object(projection_with_stable_segments(&previous, &actor, &turn_status)?);
            stored_matches_previous = stored_matches_previous && py_eq(&normalized, &previous);
            normalization_changed = !py_eq(&normalized, &previous);
            previous = normalized;
        }
        if !text_patch {
            previous_has_stable_segments = true;
        }
        let Value::Object(patch) = projection_patch else {
            return fail("database_protocol_error", "Projection patch must be an object");
        };
        let base_revision = match patch.get("baseRevision") {
            Some(Value::Number(number)) => number.as_i64(),
            _ => None,
        };
        let target_revision = match patch.get("targetRevision") {
            Some(Value::Number(number)) => number.as_i64(),
            _ => None,
        };
        let (Some(base_revision), Some(target_revision)) = (base_revision, target_revision) else {
            return fail("database_protocol_error", "Projection patch revisions must be integers");
        };
        if base_revision != old_revision {
            return fail("turn_projection_stale", "Projection patch base revision is stale");
        }
        if target_revision != new_revision {
            return fail("database_protocol_error", "Projection patch must advance exactly one revision");
        }
        projection = match apply_projection_patch(&previous, projection_patch) {
            Ok(applied) => applied,
            Err(_) => return fail("database_protocol_error", "Projection patch is invalid"),
        };
        replay = Some(projection_patch.clone());
    } else if settling_flow_parent {
        projection = previous.clone();
    } else {
        let supplied = get(payload, "projection");
        projection = if truthy(supplied) { supplied.clone() } else { json!({}) };
    }
    let settlement = get(payload, "settlement");
    let settlement = if truthy(settlement) { settlement.clone() } else { json!({}) };
    let event_type = if terminal {
        "terminal_settlement".to_owned()
    } else {
        text_or(get(payload, "event_type"), "projection_updated")
    };
    if terminal {
        if let Value::Object(map) = &projection {
            let attempt_trace = load(attempt.get("timing_trace_json"))?;
            let attempt_trace = if truthy(&attempt_trace) { attempt_trace } else { json!({}) };
            let before = map.get("timingTrace").cloned();
            let task =
                if attempt.get("task_id").truthy() { attempt.text("task_id") } else { text(get(payload, "task_id")) };
            let merged = merge_client_trace_evidence(get(map, "timingTrace"), &attempt_trace, &task)?;
            let mut map = map.clone();
            if !merged.is_empty() {
                map.insert("timingTrace".into(), Value::Object(merged));
            } else {
                map.shift_remove("timingTrace");
            }
            let after = map.get("timingTrace").cloned();
            let same = match (&before, &after) {
                (None, None) => true,
                (Some(a), Some(b)) => py_eq(a, b),
                _ => false,
            };
            if !same {
                replay = None;
            }
            projection = Value::Object(map);
        }
    }
    let slim = truthy(get(payload, "slim")) || text_patch;
    let projection_is_mapping = projection.is_object();
    let mut next: Obj;
    let next_has_stable_segments: bool;
    if slim {
        let values = if !projection_patch.is_null() { as_obj(&projection) } else { payload.clone() };
        let content = text(get(&values, "content"));
        let thinking = text(get(&values, "thinking"));
        if !projection_patch.is_null() {
            next = as_obj(&projection);
        } else {
            next = as_obj(&previous);
            next.insert("content".into(), Value::String(content.clone()));
            next.insert("thinking".into(), Value::String(thinking.clone()));
            let mut previous_text = Obj::new();
            for field in ["content", "thinking"] {
                if let Some(value) = previous.get(field) {
                    previous_text.insert(field.into(), value.clone());
                }
            }
            replay = Some(build_projection_patch(
                &Value::Object(previous_text),
                &json!({"content": content, "thinking": thinking}),
                old_revision,
                new_revision,
            ));
        }
        let text_changed = content != text(previous.get("content").unwrap_or(&Value::Null))
            || thinking != text(previous.get("thinking").unwrap_or(&Value::Null));
        next_has_stable_segments = previous_has_stable_segments && !text_changed && status == turn.text("status");
    } else {
        next = as_obj(&projection);
        // Only the authenticated producer can attest that its patch target
        // already crossed `projection_with_stable_segments`.
        next_has_stable_segments = !projection_patch.is_null() && segments_stable;
    }
    let preserved = preserve_guidance_answers(&as_obj(&wire_previous), &next)?;
    if !py_eq(&Value::Object(preserved.clone()), &Value::Object(next.clone())) {
        next = preserved;
        projection = Value::Object(next.clone());
        replay = None;
    }
    let replay = match replay {
        Some(patch) if !normalization_changed => patch,
        _ => build_projection_patch(&wire_previous, &Value::Object(next.clone()), old_revision, new_revision),
    };
    let replay_is_exact = head.active() || wire_matches_stored;
    let next_value = Value::Object(next.clone());
    let projection_changed = !projection_is_mapping || !py_eq(&next_value, &wire_previous);
    let patch_bytes = dump(&replay)?.len() as i64;
    let next_head = plan_head_append(
        &head,
        old_revision,
        patch_bytes,
        replay_is_exact && !terminal && projection_is_mapping,
        projection_changed,
    )?;
    let settlement_value = if terminal || !slim { Some(&settlement) } else { None };
    let turn_id = turn.text("turn_id");
    let conversation_id = turn.text("conversation_id");
    let materialized = HeadState { checkpoint_revision: head.checkpoint_revision, ..HeadState::default() };
    let inline_payload_bytes = stored_payload_bytes.unwrap_or(0);
    let externalize = !head.checkpoint_active() && inline_payload_bytes > PROJECTION_INLINE_LIVE_MAX_BYTES;
    let checkpoint_required = !terminal
        && projection_is_mapping
        && (externalize || (next_head.is_none() && (projection_changed || head.active())));
    let empty_projection = || -> OpResult<(&'static str, Vec<SqlValue>)> {
        Ok(if head.checkpoint_active() { ("", vec![]) } else { ("projection_json=?,", vec![dump_param(&json!({}))?]) })
    };
    let write = if checkpoint_required {
        let encoded = dump(&next_value)?;
        let bytes = encoded.len() as i64;
        let state = HeadState { checkpoint_revision: Some(new_revision), ..HeadState::default() };
        let (assignment, args) = empty_projection()?;
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &state,
            assignment,
            args,
            settlement_value,
        )?;
        if changed == 1 {
            upsert_checkpoint(session, &turn_id, &conversation_id, user_id, &attempt_id, new_revision, encoded, now)?;
        }
        ProjectionWrite {
            changed,
            projection_bytes: bytes,
            cache_charge_bytes: (bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1),
            stored_payload_bytes: Some(bytes),
            stored_matches_projection: true,
            blob_write_skipped_bytes: None,
            blob_write_deferred: false,
            checkpoint_materialized: (changed == 1)
                .then_some((bytes, if head.checkpoint_active() { 0 } else { inline_payload_bytes })),
        }
    } else if let Some(next_head) = next_head {
        let logical_unchanged = projection_is_mapping && !projection_changed;
        let next_charge = (cache_charge_bytes + patch_bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1);
        let bytes = if slim {
            dump(&json!({"content": text(get(&next, "content")), "thinking": text(get(&next, "thinking"))}))?.len()
                as i64
        } else {
            (next_charge / PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1)
        };
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &next_head,
            "",
            vec![],
            settlement_value,
        )?;
        ProjectionWrite {
            changed,
            projection_bytes: bytes,
            cache_charge_bytes: next_charge,
            stored_payload_bytes,
            stored_matches_projection: false,
            blob_write_skipped_bytes: logical_unchanged.then_some(stored_payload_bytes.unwrap_or(0)),
            blob_write_deferred: !logical_unchanged,
            checkpoint_materialized: None,
        }
    } else if !head.active() && stored_matches_previous && projection_is_mapping && !projection_changed {
        let physical_bytes = current_stored_payload_bytes()?;
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &materialized,
            "",
            vec![],
            settlement_value,
        )?;
        ProjectionWrite {
            changed,
            projection_bytes: physical_bytes,
            cache_charge_bytes,
            stored_payload_bytes: Some(physical_bytes),
            stored_matches_projection: true,
            blob_write_skipped_bytes: Some(physical_bytes),
            blob_write_deferred: false,
            checkpoint_materialized: None,
        }
    } else if slim && !head.active() && !head.checkpoint_active() && stored_matches_previous {
        let content = text(get(&next, "content"));
        let thinking = text(get(&next, "thinking"));
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &materialized,
            "projection_json=json_set(projection_json, '$.content', ?, '$.thinking', ?),",
            vec![SqlValue::Text(content.clone()), SqlValue::Text(thinking.clone())],
            settlement_value,
        )?;
        let next_charge = (cache_charge_bytes
            + PROJECTION_CACHE_CHARGE_MULTIPLIER
                * (projection_text_bytes(&next_value) - projection_text_bytes(&wire_previous)))
        .max(1);
        ProjectionWrite {
            changed,
            projection_bytes: dump(&json!({"content": content, "thinking": thinking}))?.len() as i64,
            cache_charge_bytes: next_charge,
            stored_payload_bytes: None,
            stored_matches_projection: true,
            blob_write_skipped_bytes: None,
            blob_write_deferred: false,
            checkpoint_materialized: None,
        }
    } else {
        let materialized_projection = if slim { next_value.clone() } else { projection.clone() };
        let encoded = dump(&materialized_projection)?;
        let bytes = encoded.len() as i64;
        let changed = update_projection_row(
            session,
            &turn_id,
            &attempt_id,
            &status,
            old_revision,
            new_revision,
            now,
            &HeadState::default(),
            "projection_json=?,",
            vec![SqlValue::Blob(encoded)],
            settlement_value,
        )?;
        if changed == 1 && head.checkpoint_active() {
            delete_checkpoint(session, &turn_id)?;
        }
        ProjectionWrite {
            changed,
            projection_bytes: bytes,
            cache_charge_bytes: (bytes * PROJECTION_CACHE_CHARGE_MULTIPLIER).max(1),
            stored_payload_bytes: Some(bytes),
            stored_matches_projection: projection_is_mapping,
            blob_write_skipped_bytes: None,
            blob_write_deferred: false,
            checkpoint_materialized: None,
        }
    };
    let projection_bytes = write.projection_bytes;
    if write.changed == 0 {
        if let Some(cache) = cache {
            cache.discard(&cache_key);
        }
        if started_by_event {
            return fail("database_conflict", "Turn changed while its first worker event was recorded");
        }
        return not_applied();
    }
    if terminal {
        let trace = match next.get("timingTrace") {
            Some(trace @ Value::Object(_)) => trace.clone(),
            _ => json!({}),
        };
        let error = get(payload, "error");
        let error = if truthy(error) { error.clone() } else { json!({}) };
        session.execute(
            "UPDATE storage_generation_attempts SET status=?, error_json=?, timing_trace_json=?, settled_at=? WHERE attempt_id=? AND status IN ('pending','running')",
            &params![status.clone(), dump_param(&error)?, dump_param(&trace)?, now, attempt_id.clone()],
        )?;
    }
    let continuation_status = if terminal {
        Value::String(settle_continuation(session, &turn, get(payload, "model_continuation"), &status)?)
    } else {
        Value::Null
    };
    let raw_event_payload = get(payload, "event_payload");
    let mut event_payload = match raw_event_payload {
        _ if !truthy(raw_event_payload) => Obj::new(),
        Value::Object(map) => map.clone(),
        _ => return crate::error::internal("dict() of a non-mapping event payload"),
    };
    event_payload.shift_remove("_requireUnboundAttempt");
    event_payload.shift_remove("removedQueueIds");
    if !background_ack.is_null() {
        if slim {
            return fail("database_protocol_error", "Background acknowledgement requires a full receipt projection");
        }
        let removed_ids = acknowledge_background_command_receipt(
            session,
            &attempt.text("conversation_id"),
            user_id,
            background_ack,
            &next,
        )?;
        event_payload.insert("removedQueueIds".into(), Value::Array(removed_ids));
    }
    if terminal {
        event_payload.insert("modelContinuationStatus".into(), continuation_status.clone());
    }
    event_payload.shift_remove("projection");
    event_payload.insert("projectionPatch".into(), replay);
    event_payload.insert("status".into(), Value::String(status.clone()));
    event_payload.insert("projectionBytes".into(), Value::from(projection_bytes));
    if started_by_event {
        let Some(started) = session
            .fetch_one("SELECT * FROM storage_generation_attempts WHERE attempt_id=?", &params![attempt_id.clone()])?
        else {
            return crate::error::internal("started attempt is missing");
        };
        event_payload.insert("attempts".into(), json!([attempt_public(&started)?]));
    }
    let appended = turn_event_append(
        session,
        &attempt_id,
        &attempt.text("conversation_id"),
        &turn_id,
        new_revision,
        &event_type,
        event_payload,
    )?;
    session.execute(
        "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
        &params![now, attempt.get("conversation_id"), turn.get("user_id")],
    )?;
    if terminal {
        upsert_turn_search_row(session, &turn, Some(&status))?;
    }
    let mut task_event_result = Value::Null;
    let mut task_event_count = 0usize;
    if !task_event.is_null() {
        let Value::Object(carrier) = task_event else {
            return fail("database_protocol_error", "Invalid task event carrier");
        };
        let preceding = match carrier.get("preceding_events") {
            None => vec![],
            Some(Value::Array(rows)) if rows.len() < PROGRESS_BATCH_MAX_FRAMES => rows.clone(),
            Some(_) => return fail("database_protocol_error", "Invalid progress event batch size"),
        };
        if !preceding.is_empty() {
            let last = match carrier.get("sequence") {
                Some(Value::Number(number)) => number.as_i64(),
                _ => None,
            };
            let Some(last) = last.filter(|last| *last >= preceding.len() as i64) else {
                return fail("database_protocol_error", "Invalid progress event batch sequence");
            };
            let first = last - preceding.len() as i64;
            let mut wire_bytes = 0usize;
            for (expected, row) in (first..).zip(preceding.iter().chain(std::iter::once(task_event))) {
                let valid = match row {
                    Value::Object(row) => {
                        let sequence = match row.get("sequence") {
                            Some(Value::Number(number)) => number.as_i64(),
                            _ => None,
                        };
                        py_eq(get(row, "task_id"), get(carrier, "task_id"))
                            && py_eq(get(row, "task_id"), get(payload, "task_id"))
                            && sequence == Some(expected)
                            && expected >= 0
                            && matches!(row.get("event"), Some(Value::Object(event))
                                if matches!(event.get("type").and_then(Value::as_str), Some("delta" | "program_output" | "tool_progress")))
                            && !terminal
                    }
                    _ => false,
                };
                if !valid {
                    return fail("database_protocol_error", "Invalid progress event batch scope/order");
                }
            }
            for row in preceding.iter().chain(std::iter::once(task_event)) {
                wire_bytes += dump(&row["event"])?.len();
            }
            if wire_bytes > PROGRESS_BATCH_MAX_WIRE_BYTES {
                return fail("database_protocol_error", "Progress event batch exceeds wire budget");
            }
            for row in &preceding {
                append_event_row(session, &as_obj(row))?;
            }
        }
        task_event_result = append_event_row(session, carrier)?;
        task_event_count = preceding.len() + 1;
    }
    if let Some(cache) = cache {
        if terminal || (!slim && !projection_is_mapping) {
            cache.discard(&cache_key);
        } else {
            cache.remember(
                &cache_key,
                new_revision,
                Arc::new(next_value),
                write.cache_charge_bytes,
                write.stored_payload_bytes,
                write.stored_matches_projection,
                next_has_stable_segments,
            );
        }
    }
    if let Some(skipped_bytes) = write.blob_write_skipped_bytes {
        attempt_event_metrics::observe_blob_write_skip(&event_type, skipped_bytes);
    }
    if write.blob_write_deferred {
        attempt_event_metrics::observe_blob_write_deferral(&event_type, write.stored_payload_bytes.unwrap_or(0));
    }
    if let Some((checkpoint_bytes, inline_released_bytes)) = write.checkpoint_materialized {
        attempt_event_metrics::observe_checkpoint_materialization(&event_type, checkpoint_bytes, inline_released_bytes);
    }
    let mut events = vec![appended["event"].clone()];
    if terminal && flow_root.is_empty() && attempt.get("task_id").truthy() {
        let children = session.fetch_all(
            "SELECT attempt_id,config_json FROM storage_generation_attempts WHERE conversation_id=? AND task_id=? AND status IN ('pending','running') LIMIT 4096",
            &[attempt.get("conversation_id").clone(), attempt.get("task_id").clone()],
        )?;
        for child in &children {
            if get(&loaded_obj(child.get("config_json"))?, "flowRootAttemptId") != &Value::String(attempt_id.clone()) {
                continue;
            }
            let mut child_settlement = as_obj(&settlement);
            child_settlement.insert("resumeOptions".into(), json!([]));
            let error = get(payload, "error");
            let mut child_payload = Payload::new();
            child_payload.insert("worker_claim".into(), get(payload, "worker_claim").clone());
            child_payload.insert("attempt_id".into(), child.get("attempt_id").to_json()?);
            child_payload.insert("user_id".into(), Value::from(user_id));
            child_payload.insert("flow_root_attempt_id".into(), Value::String(attempt_id.clone()));
            child_payload.insert("terminal".into(), json!(true));
            child_payload.insert("status".into(), Value::String(status.clone()));
            child_payload.insert("settlement".into(), Value::Object(child_settlement));
            child_payload.insert("error".into(), if truthy(error) { error.clone() } else { json!({}) });
            child_payload.insert("event_type".into(), json!("terminal_settlement"));
            child_payload.insert("event_payload".into(), json!({"phase": null}));
            let child_result = event_record(session, &child_payload, true)?;
            if let Some(Value::Array(child_events)) = child_result.get("_conversationSyncAttemptEvents") {
                events.extend(child_events.iter().cloned());
            }
        }
    }
    Ok(json!({
        "applied": true,
        "status": status,
        "projection_revision": new_revision,
        "task_event": task_event_result,
        "task_event_count": task_event_count,
        "model_continuation_status": continuation_status,
        "_conversationSyncAttemptEvents": events,
    }))
}

// ── private ports of `_model_continuation.py` / `storage/model_continuation.py`;
//    shared owner: model continuation operations ─────────────────────────────

const CONTINUATION_NAMESPACE: &str = "model.continuation.v1";
const MAX_ENTRY_BYTES: usize = 8_388_608;
const MAX_JSON_SAFE_INTEGER: i64 = 9_007_199_254_740_991;
const MAX_MESSAGES: usize = 16_384;
const MAX_TURN_WITNESSES: usize = 4096;

fn is_hex64(value: &Value) -> bool {
    matches!(value, Value::String(text) if text.len() == 64 && text.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
}

/// `encode_projection(projection)`.
pub(super) fn encode_continuation(projection: &Value) -> Option<String> {
    let Value::Object(map) = projection else { return None };
    let messages = map.get("messages");
    let turns = map.get("turns");
    let allowed = if map.contains_key("historyProof") { 1 } else { MAX_TURN_WITNESSES };
    let Some(Value::Array(messages)) = messages else { return None };
    if messages.is_empty() || messages.len() > MAX_MESSAGES || !messages.iter().all(Value::is_object) {
        return None;
    }
    if messages.last().and_then(|message| message.get("role")) != Some(&json!("assistant")) {
        return None;
    }
    let Some(Value::Array(turns)) = turns else { return None };
    if turns.is_empty() || turns.len() > allowed {
        return None;
    }
    if let Some(proof) = map.get("historyProof") {
        let Value::Object(proof) = proof else { return None };
        let keys_ok = proof.len() == 3 && ["version", "count", "digest"].iter().all(|key| proof.contains_key(*key));
        let version_ok = matches!(proof.get("version"), Some(Value::Number(n)) if matches!(n.as_i64(), Some(1 | 2)));
        let count_ok = matches!(proof.get("count"), Some(Value::Number(n)) if n.as_i64().is_some_and(|c| (0..=MAX_JSON_SAFE_INTEGER).contains(&c)));
        if !keys_ok || !version_ok || !count_ok || !is_hex64(proof.get("digest").unwrap_or(&Value::Null)) {
            return None;
        }
    }
    for witness in turns {
        let valid = matches!(witness, Value::Array(pair) if pair.len() == 2
            && matches!(&pair[0], Value::String(id) if !id.is_empty() && id.chars().count() <= 256)
            && is_hex64(&pair[1]));
        if !valid {
            return None;
        }
    }
    let encoded = py_json_dumps(projection, false);
    if encoded.len() > MAX_ENTRY_BYTES {
        return None;
    }
    Some(encoded)
}

/// `settled_witness(row)` for a public Turn.
pub(super) fn settled_witness(turn: &Obj) -> Value {
    let mut source = Obj::new();
    for key in ["actor", "laneId", "parentTurnId", "status", "currentAttemptId"] {
        source.insert(key.into(), get(turn, key).clone());
    }
    let revision = if turn.contains_key("modelContextRevision") {
        get(turn, "modelContextRevision").clone()
    } else {
        let projection = py_int(get(turn, "projectionRevision")).unwrap_or(0);
        Value::from(projection)
    };
    source.insert("modelContextRevision".into(), revision);
    let projection = get(turn, "projection");
    let projection = if truthy(projection) { as_obj(projection) } else { Obj::new() };
    source.insert("projection".into(), Value::Object(model_projection_for_context(&projection)));
    let digest = hex::encode(Sha256::digest(py_json_dumps(&Value::Object(source), true).as_bytes()));
    json!([get(turn, "turnId").clone(), digest])
}

pub(super) fn continuation_key(user_id: i64, conversation_id: &str, lane_id: &str) -> String {
    format!(
        "{user_id}:{}:{}",
        hex::encode(Sha256::digest(conversation_id.as_bytes())),
        hex::encode(Sha256::digest(lane_id.as_bytes()))
    )
}

fn continuation_put(
    session: &Session<'_>,
    user_id: i64,
    conversation_id: &str,
    lane_id: &str,
    projection: &Value,
) -> OpResult<bool> {
    let mut identity = Payload::new();
    identity.insert("user_id".into(), Value::from(user_id));
    identity.insert("conversation_id".into(), Value::String(conversation_id.into()));
    identity.insert("lane_id".into(), Value::String(lane_id.into()));
    let user_id = integer(&identity, "user_id", None, Some(1), None)?;
    let conversation_id = required_text(&identity, "conversation_id", 256)?;
    let lane_id = required_text(&identity, "lane_id", 256)?;
    let owned = session
        .fetch_one(
            "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
            &params![conversation_id.clone(), user_id],
        )?
        .is_some();
    let key = continuation_key(user_id, &conversation_id, &lane_id);
    if !owned {
        return Ok(false);
    }
    let Some(encoded) = encode_continuation(projection) else { return Ok(false) };
    let witness = projection["turns"].as_array().and_then(|turns| turns.last()).cloned().unwrap_or(Value::Null);
    let row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND turn_id=?",
        &[
            SqlValue::Text(conversation_id),
            SqlValue::Integer(user_id),
            SqlValue::Text(lane_id),
            crate::session::json_param(&witness[0])?,
        ],
    )?;
    let Some(row) = row else { return Ok(false) };
    if !matches!(row.text("status").as_str(), "completed" | "truncated") {
        return Ok(false);
    }
    let public = as_obj(&turn_public(session, &row)?);
    if settled_witness(&public) != witness {
        return Ok(false);
    }
    let frontier = row.int("ordinal")? + 1;
    let current = session.fetch_one(
        "SELECT value_json,version FROM storage_records WHERE namespace=? AND record_key=?",
        &params![CONTINUATION_NAMESPACE, key.clone()],
    )?;
    if let Some(current) = current {
        let actual = current.int("version")?;
        if frontier < actual {
            return Ok(false);
        }
        if frontier == actual {
            return Ok(py_eq(&load(current.get("value_json"))?, projection));
        }
        let changed = session.execute(
            "UPDATE storage_records SET value_json=?,version=?,updated_at_ms=? WHERE namespace=? AND record_key=? AND version=?",
            &params![encoded, frontier, now_ms(), CONTINUATION_NAMESPACE, key, actual],
        )?;
        return Ok(changed == 1);
    }
    session.execute(
        "INSERT INTO storage_records(namespace,record_key,value_json,version,updated_at_ms) VALUES (?,?,?,?,?)",
        &params![CONTINUATION_NAMESPACE, key, encoded, frontier, now_ms()],
    )?;
    Ok(true)
}

/// `settle_continuation(session, turn, candidate, status=)`.
fn settle_continuation(session: &Session<'_>, turn: &Row, candidate: &Value, status: &str) -> OpResult<String> {
    if status != "completed" && status != "truncated" {
        return Ok("not_completed".into());
    }
    let Value::Object(candidate) = candidate else { return Ok("unavailable".into()) };
    let Some(projection) = candidate.get("projection") else {
        return Ok(match candidate.get("status") {
            Some(Value::String(reason)) if reason.chars().count() <= 64 => reason.clone(),
            _ => "invalid".into(),
        });
    };
    if encode_continuation(projection).is_none() {
        return Ok("invalid_or_over_budget".into());
    }
    let Some(row) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE turn_id=? AND user_id=?",
        &[turn.get("turn_id").clone(), turn.get("user_id").clone()],
    )?
    else {
        return crate::error::internal("TypeError: NoneType");
    };
    let target = as_obj(&turn_public(session, &row)?);
    let last = projection["turns"].as_array().and_then(|turns| turns.last()).cloned().unwrap_or(Value::Null);
    let target_content =
        get(target.get("projection").and_then(Value::as_object).unwrap_or(&Obj::new()), "content").clone();
    if !py_eq(&last[0], get(&target, "turnId")) || !py_eq(get(candidate, "content"), &target_content) {
        return Ok("authority_changed".into());
    }
    let mut projection = as_obj(projection);
    let mut turns = projection["turns"].as_array().cloned().unwrap_or_default();
    turns.pop();
    turns.push(settled_witness(&target));
    projection.insert("turns".into(), Value::Array(turns));
    let stored = continuation_put(
        session,
        turn.int("user_id")?,
        &turn.text("conversation_id"),
        &turn.text("lane_id"),
        &Value::Object(projection),
    )?;
    Ok(if stored { "stored" } else { "rejected" }.into())
}

/// `_turn_perception_record`.
pub fn turn_perception_record(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = super::core::turn_identity(payload)?;
    let turn_id = required_text(payload, "turn_id", 128)?;
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let Value::Object(observation) = get(payload, "observation") else {
        return fail("database_protocol_error", "Perception observation must be an object");
    };
    let row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv_id.clone(), user_id, turn_id.clone()],
    )?;
    let attempt = session.fetch_one(
        "SELECT * FROM storage_generation_attempts WHERE attempt_id=? AND conversation_id=? AND turn_id=?",
        &params![attempt_id.clone(), conv_id.clone(), turn_id],
    )?;
    let (Some(row), Some(attempt)) = (row, attempt) else {
        return fail("database_not_found", "Turn attempt not found");
    };
    let task_id = attempt.text("task_id");
    if task_id.is_empty() {
        return fail("database_conflict", "Turn attempt has no executor task identity");
    }
    let loaded = load(attempt.get("timing_trace_json"))?;
    let mut timing_trace = match loaded {
        Value::Object(map) => map,
        _ => Obj::new(),
    };
    if timing_trace.is_empty() {
        let projection = super::head::projection_from_turn_row(session, &row)?;
        if let Some(Value::Object(trace)) = projection.get("timingTrace") {
            if text(get(trace, "taskId")) == task_id && trace.get("clientObservations").is_some_and(Value::is_array) {
                timing_trace = Obj::new();
                timing_trace.insert("version".into(), json!(super::trace::TRACE_CONTRACT_VERSION));
                timing_trace.insert("taskId".into(), Value::String(task_id.clone()));
                timing_trace.insert("clientObservations".into(), trace["clientObservations"].clone());
                if truthy(get(trace, "clientObservationDroppedCount")) {
                    let dropped = super::core::py_int_or_zero(get(trace, "clientObservationDroppedCount"))?;
                    timing_trace.insert("clientObservationDroppedCount".into(), Value::from(dropped));
                }
            }
        }
    }
    let appended = super::trace::append_client_trace_observation(
        &Value::Object(timing_trace),
        observation,
        &task_id,
        &attempt_id,
        now_ms(),
    )?;
    let (trace, applied) = match appended {
        Ok(result) => result,
        Err(message) => return fail("database_protocol_error", message),
    };
    let conversation = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?;
    if applied {
        let changed = session.execute(
            "UPDATE storage_generation_attempts SET timing_trace_json=? WHERE attempt_id=?",
            &params![dump_param(&Value::Object(trace))?, attempt_id],
        )?;
        if changed != 1 {
            return fail("database_conflict", "Attempt changed while perception was recorded");
        }
    }
    let Some(conversation) = conversation else {
        return fail("database_integrity", "Perception conversation is missing");
    };
    let mut result = Obj::new();
    result.insert("applied".into(), Value::Bool(applied));
    if !applied {
        result.insert("idempotentReplay".into(), json!(true));
    }
    result.insert("conversationRevision".into(), Value::from(conversation.int("rev")?));
    result.insert("projectionRevision".into(), Value::from(row.opt_int("projection_revision").unwrap_or(0)));
    Ok(Value::Object(result))
}

/// `lib.storage.generated_task_event_delivery_contract`.
const PROGRESS_BATCH_MAX_FRAMES: usize = 500;
const PROGRESS_BATCH_MAX_WIRE_BYTES: usize = 1_048_576;
const STRUCTURAL_BATCH_MAX_EVENTS: usize = 500;
const STRUCTURAL_BATCH_MAX_WIRE_BYTES: usize = 62_914_560;

/// Python iteration of `value or ()`.
fn iterable(value: &Value) -> OpResult<Vec<Value>> {
    Ok(match value {
        _ if !truthy(value) => vec![],
        Value::Array(items) => items.clone(),
        Value::Object(map) => map.keys().map(|key| Value::String(key.clone())).collect(),
        Value::String(text) => text.chars().map(|c| Value::String(c.to_string())).collect(),
        _ => return crate::error::internal("object is not iterable"),
    })
}

/// `_validate_turn_event_batch`.
fn validate_turn_event_batch(payload: &Payload) -> OpResult<Vec<Value>> {
    let entries = match payload.get("entries") {
        Some(Value::Array(entries)) if !entries.is_empty() => entries.clone(),
        _ => return fail("database_protocol_error", "Invalid turn event batch"),
    };
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let mut identity: Option<(Value, Value, Value)> = None;
    let mut previous_sequence = -1i64;
    let mut rows = 0usize;
    let mut wire_bytes = 0usize;
    for entry in &entries {
        let Value::Object(entry) = entry else {
            return fail("database_protocol_error", "Invalid turn event batch entry");
        };
        let task_event = get(entry, "task_event");
        let event_payload = get(entry, "event_payload");
        let owner_ok = matches!(entry.get("user_id"), Some(Value::Number(number)) if number.as_i64() == Some(user_id) && !number.is_f64());
        let event_type = get(entry, "event_type");
        let event_type_ok = if truthy(event_type) { event_type == &json!("projection_updated") } else { true };
        let tool_effect =
            matches!(event_payload, Value::Object(map) if map.get("updateKind") == Some(&json!("tool_effect")));
        if !owner_ok
            || truthy(get(entry, "terminal"))
            || !get(entry, "background_command_ack").is_null()
            || !event_type_ok
            || tool_effect
            || !task_event.is_object()
        {
            return fail("database_protocol_error", "Turn event batch entries must be carried structural events");
        }
        let flow_root = get(entry, "flow_root_attempt_id");
        let entry_identity = (
            get(entry, "attempt_id").clone(),
            get(entry, "task_id").clone(),
            if truthy(flow_root) { flow_root.clone() } else { json!("") },
        );
        if let Some(current) = &identity {
            let same = py_eq(&current.0, &entry_identity.0)
                && py_eq(&current.1, &entry_identity.1)
                && py_eq(&current.2, &entry_identity.2);
            if !same {
                return fail("database_protocol_error", "Turn event batch spans tasks or attempts");
            }
        }
        identity = Some(entry_identity);
        let carrier = task_event.as_object().expect("carrier");
        let preceding = match carrier.get("preceding_events") {
            None => vec![],
            Some(Value::Array(rows)) => rows.clone(),
            Some(_) => return fail("database_protocol_error", "Invalid progress event batch size"),
        };
        for row in preceding.iter().chain(std::iter::once(task_event)) {
            let Value::Object(row) = row else {
                return fail("database_protocol_error", "Invalid turn event batch row");
            };
            let sequence = match row.get("sequence") {
                Some(Value::Number(number)) if !number.is_f64() => number.as_i64(),
                _ => None,
            };
            let event = get(row, "event");
            let effect = matches!(event, Value::Object(map) if map.get("type") == Some(&json!("tool_effect")));
            let ordered = sequence.is_some_and(|sequence| sequence > previous_sequence);
            if !py_eq(get(row, "task_id"), get(entry, "task_id")) || !ordered || effect {
                return fail("database_protocol_error", "Turn event batch rows must form one ascending task stream");
            }
            previous_sequence = sequence.expect("ordered sequence");
            rows += 1;
            wire_bytes += dump(event)?.len();
        }
    }
    if rows > STRUCTURAL_BATCH_MAX_EVENTS || wire_bytes > STRUCTURAL_BATCH_MAX_WIRE_BYTES {
        return fail("database_protocol_error", "Turn event batch exceeds the storage event RPC bound");
    }
    Ok(entries)
}

/// `_turn_event_record_batch`.
pub fn turn_event_record_batch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let entries = validate_turn_event_batch(payload)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let attempt_id = text(get(&as_obj(&entries[entries.len() - 1]), "attempt_id"));
    let mut results: Vec<Value> = Vec::new();
    let outcome = (|| -> OpResult<()> {
        for entry in &entries {
            let result = event_record(session, &as_obj(entry), false)?;
            if let Some(first) = results.first() {
                if truthy(get(&as_obj(&result), "applied")) != truthy(get(&as_obj(first), "applied")) {
                    return fail("database_conflict", "Turn event batch entries diverged inside one transaction");
                }
            }
            results.push(result);
        }
        Ok(())
    })();
    if let Err(failure) = outcome {
        // Applied entries already advanced this attempt's reconstructible
        // baseline; the rollback must not leave it ahead of the durable row.
        let applied = results.first().filter(|first| truthy(&first["applied"]));
        let event = applied.and_then(|first| first["_conversationSyncAttemptEvents"].as_array()?.first().cloned());
        if let (Some(cache), Some(event)) = (session.turn_projection_cache, event) {
            cache.discard(&projection_cache_key(
                session.backend,
                user_id,
                &text(&event["conversationId"]),
                &text(&event["turnId"]),
                &attempt_id,
            ));
        }
        return Err(failure);
    }
    Ok(Value::Array(results))
}

#[cfg(test)]
mod unbound_fence_tests {
    use super::*;

    #[test]
    fn startup_failure_fence_rejects_invalid_markers_and_bound_attempts() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE storage_conversation_turns(turn_id TEXT,user_id INTEGER); CREATE TABLE storage_generation_attempts(attempt_id TEXT,turn_id TEXT,conversation_id TEXT,status TEXT,task_id TEXT,queue_state TEXT); INSERT INTO storage_conversation_turns VALUES('t',1); INSERT INTO storage_generation_attempts VALUES('a','t','c','pending','task','');").unwrap();
        let session = Session::new(&connection, "test", None, true);
        let payload = json!({"attempt_id":"a","user_id":1,"terminal":true,"status":"failed","event_payload":{"_requireUnboundAttempt":true}});
        let payload = payload.as_object().unwrap();
        for sql in [
            "UPDATE storage_generation_attempts SET task_id='task'",
            "UPDATE storage_generation_attempts SET task_id='',queue_state='pending'",
            "UPDATE storage_generation_attempts SET queue_state='',status='running'",
        ] {
            connection.execute_batch(sql).unwrap();
            assert_eq!(event_record(&session, payload, false).unwrap(), json!({"applied":false}));
        }
        for marker in [Value::Null, json!(0), json!("true")] {
            let mut invalid = payload.clone();
            invalid["event_payload"]["_requireUnboundAttempt"] = marker;
            let error = event_record(&session, &invalid, false).unwrap_err();
            assert!(
                matches!(error, crate::error::Failure::Storage(error) if error.code == "database_protocol_error" && error.message == "Unbound fence requires a startup failure")
            );
        }
        for (field, value) in [("terminal", json!(false)), ("status", json!("interrupted"))] {
            let mut invalid = payload.clone();
            invalid.insert(field.into(), value);
            let error = event_record(&session, &invalid, false).unwrap_err();
            assert!(
                matches!(error, crate::error::Failure::Storage(error) if error.code == "database_protocol_error" && error.message == "Unbound fence requires a startup failure")
            );
        }
    }
}
