//! `operations_pkg/_turns_read.py::_turn_visible_sync` with private ports of
//! the pure owners it calls: `lib/orchestration/message_compat.py`,
//! `lib/orchestration/visible_identity.py`, `lib/agent_core/output_settlement.py`,
//! `lib/agent_verdict/_handoff.py::strip_machine_tokens` and
//! `lib/turn_verdict::derive_task_verdict`.

use std::collections::VecDeque;

use regex::Regex;
use serde_json::{json, Value};

use super::core::{
    as_obj, insert_attempt_event, now_ms, text, text_or, turn_event_append, turn_public, upsert_turn_search_row, Obj,
};
use super::events::event_record;
use super::head::{advance_unchanged_revision, delete_checkpoint, projection_map};
use super::normalize::{normalize_projection_document, py_strip};
use super::patch::build_projection_patch;
use super::segments::{execution_identity, execution_llm_round, projection_with_stable_segments};
use super::worker::require_conversation_publication_claim;
use crate::codec::{dump_param, load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

// ── visible_identity.py (uuid5 over NAMESPACE_URL) ────────────────────────

fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let mut message = data.to_vec();
    let bit_length = (data.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bit_length.to_be_bytes());
    for chunk in message.chunks(64) {
        let mut w = [0u32; 80];
        for (i, word) in chunk.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, word) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let temp = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*word);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = temp;
        }
        for (slot, value) in h.iter_mut().zip([a, b, c, d, e]) {
            *slot = slot.wrapping_add(value);
        }
    }
    let mut out = [0u8; 20];
    for (i, word) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&word.to_be_bytes());
    }
    out
}

/// `uuid.uuid5(uuid.NAMESPACE_URL, name)`.
fn uuid5_url(name: &str) -> String {
    let namespace = uuid::Uuid::parse_str("6ba7b811-9dad-11d1-80b4-00c04fd430c8").expect("namespace");
    let mut data = namespace.as_bytes().to_vec();
    data.extend_from_slice(name.as_bytes());
    let digest = sha1(&data);
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    bytes[6] = (bytes[6] & 0x0F) | 0x50;
    bytes[8] = (bytes[8] & 0x3F) | 0x80;
    uuid::Uuid::from_bytes(bytes).to_string()
}

fn visible_phase_identity(root_turn_id: &str, attempt_id: &str, index: i64) -> (String, String) {
    if index == 0 {
        return (root_turn_id.to_owned(), attempt_id.to_owned());
    }
    (
        uuid5_url(&format!("turn-attempt:{attempt_id}:visible:{index}")),
        uuid5_url(&format!("turn-attempt:{attempt_id}:visible-attempt:{index}")),
    )
}

// ── message_compat.py ────────────────────────────────────────────────────

fn normalize_flow_message(message: &Value) -> OpResult<Obj> {
    let Value::Object(message) = message else { return internal("dict() of a non-mapping message") };
    let mut normalized = message.clone();
    for (legacy, canonical) in [
        ("_isEndpointPlanner", "_isFlowPlanner"),
        ("_isEndpointReview", "_isFlowReview"),
        ("_epIteration", "_flowIteration"),
        ("_epPlannerIteration", "_flowPlannerIteration"),
        ("_epApproved", "_flowApproved"),
        ("_epNextPhase", "_flowNextPhase"),
        ("_epStateChangingCount", "_flowStateChangingCount"),
    ] {
        if !normalized.contains_key(canonical) && normalized.contains_key(legacy) {
            let value = normalized[legacy].clone();
            normalized.insert(canonical.into(), value);
        }
        normalized.shift_remove(legacy);
    }
    Ok(normalized)
}

fn is_flow_turn_kind(kind: &SqlValue) -> OpResult<bool> {
    let kind = text(&kind.to_json()?);
    Ok(["flow_", "autopilot_", "endpoint_"].iter().any(|prefix| kind.starts_with(prefix)))
}

// ── agent_verdict strip_machine_tokens ───────────────────────────────────

fn strip_machine_tokens(value: &Value) -> OpResult<Value> {
    if !truthy(value) {
        return Ok(value.clone());
    }
    let Value::String(text) = value else { return internal("expected string or bytes-like object") };
    static PATTERNS: std::sync::OnceLock<[Regex; 4]> = std::sync::OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        [
            Regex::new(&regex::escape("[VU: TASK_DONE]")).expect("sentinel"),
            Regex::new(r"(?i)\[PROGRESS:\s*resolved\s*=\s*(\d+)\s*(?:,|;|\s)\s*remaining\s*=\s*(\d+)\s*\]")
                .expect("progress"),
            Regex::new(r"(?i)\[VERDICT:\s*(STOP|CONTINUE_WORKER|CONTINUE_PLANNER|CONTINUE|BLOCKED)\s*\]")
                .expect("verdict"),
            Regex::new(r"\n{3,}").expect("husk"),
        ]
    });
    let mut out = text.clone();
    let mut changed = false;
    for pattern in &patterns[..3] {
        if pattern.is_match(&out) {
            changed = true;
            out = pattern.replace_all(&out, "").into_owned();
        }
    }
    if !changed {
        return Ok(value.clone());
    }
    let out = patterns[3].replace_all(&out, "\n\n").into_owned();
    Ok(Value::String(py_strip(&out).to_owned()))
}

// ── turn_verdict.derive_task_verdict(message) ────────────────────────────

fn abort_initiator(reason: &str) -> &'static str {
    match reason {
        "server_shutdown" | "restart_recovery" => "server_restart",
        "lead_hold" => "lead_hold",
        "storage_authority_integrity"
        | "storage_frame_overflow"
        | "turn_attempt_stale_fence"
        | "conversation_attempt_stale_fence"
        | "worker_lease_lost" => "storage_fence",
        "dispatch_grant_revoked" | "dispatch_authority_lost" => "dispatch_authority",
        "stuck_no_progress" => "stuck_task_reaper",
        "hallucination_loop" | "awaiting_user_action" | "legacy_autopilot_retired" => "tofu_guard",
        _ => "person",
    }
}

const PROVIDER_STREAM_STATES: &[&str] = &[
    "provider_finished",
    "client_aborted",
    "premature_close",
    "malformed_stream",
    "semantic_progress_timeout",
    "no_actionable_output",
    "empty_response",
    "tool_payload_missing",
    "gateway_limit_response",
    "unknown",
];

/// `(status, cause)` of `derive_task_verdict(task)` with no terminal event.
fn derive_task_verdict(task: &Obj) -> (String, String) {
    let finish_value = match task.get("finishReason") {
        Some(value) if !value.is_null() => value.clone(),
        _ => get(task, "finish_reason").clone(),
    };
    let finish = text(&finish_value);
    let raw_state = match task.get("streamState") {
        Some(value) if !value.is_null() => value.clone(),
        _ => get(task, "stream_state").clone(),
    };
    let stream_state = match &raw_state {
        Value::Null => None,
        Value::String(text) if text.is_empty() => None,
        other => {
            let rendered = python_str(other);
            Some(if PROVIDER_STREAM_STATES.contains(&rendered.as_str()) { rendered } else { "unknown".into() })
        }
    };
    let content_filter_cause = match task.get("_contentFilterSettlement") {
        Some(Value::Object(settlement)) => text(get(settlement, "cause")),
        _ => String::new(),
    };
    let terminal_error = get(task, "error");
    let durable_reason = match terminal_error {
        Value::Object(error)
            if error.get("context") == Some(&json!("invocation-guard"))
                && error.get("source") == Some(&json!("task-engine")) =>
        {
            text(get(error, "invocationStopReason"))
        }
        _ => String::new(),
    };
    let has_error = truthy(terminal_error);
    let aborted = truthy(get(task, "aborted"));
    let task_status = text(get(task, "status"));
    let abort_reason = text(get(task, "_abort_reason"));
    let invocation = {
        let stamped = text(get(task, "_invocationStopReason"));
        if stamped.is_empty() {
            durable_reason
        } else {
            stamped
        }
    };
    let state = stream_state.as_deref();
    let verdict = |status: &str, cause: &str| (status.to_owned(), cause.to_owned());
    let system = match abort_initiator(&abort_reason) {
        "storage_fence" => Some(("failed", "internal_fence_abort")),
        "dispatch_authority" => Some(("failed", "")),
        "server_restart" => Some(("interrupted", "server_restart")),
        "lead_hold" => Some(("interrupted", "lead_hold")),
        "stuck_task_reaper" => Some(("failed", "worker_lost")),
        "tofu_guard" => Some(("failed", "generation_error")),
        _ => None,
    };
    if let Some((status, cause)) = system {
        return verdict(status, if cause.is_empty() { &abort_reason } else { cause });
    }
    if invocation == "stalled" || invocation == "deadline" {
        return verdict("failed", &format!("invocation_{invocation}"));
    }
    if aborted
        || task_status == "aborted"
        || state == Some("client_aborted")
        || matches!(finish.as_str(), "aborted" | "interrupted")
    {
        return verdict("interrupted", "user_abort");
    }
    let failed_state = matches!(
        state,
        Some(
            "premature_close"
                | "malformed_stream"
                | "semantic_progress_timeout"
                | "no_actionable_output"
                | "empty_response"
                | "tool_payload_missing"
                | "gateway_limit_response"
        )
    );
    if failed_state || matches!(finish.as_str(), "premature_close" | "abnormal_stop") {
        return verdict("failed", "provider_stream_error");
    }
    if has_error
        || task_status == "error"
        || matches!(finish.as_str(), "error" | "timeout" | "premature_close" | "abnormal_stop")
    {
        return verdict("failed", "generation_error");
    }
    if matches!(
        finish.as_str(),
        "length" | "max_tokens" | "context_length" | "content_filter" | "budget_exceeded" | "incomplete" | "max_turns"
    ) {
        let cause = if finish == "content_filter"
            && matches!(content_filter_cause.as_str(), "model_refusal" | "content_filter")
        {
            content_filter_cause
        } else {
            finish
        };
        return verdict("truncated", &cause);
    }
    let success = matches!(
        finish.as_str(),
        "stop" | "done" | "completed" | "end_turn" | "tool_calls" | "tool_use" | "function_call"
    );
    if (state == Some("provider_finished") || state.is_none()) && success {
        return verdict("completed", "provider_finished");
    }
    verdict("failed", "completion_evidence_missing")
}

// ── output_settlement.py ─────────────────────────────────────────────────

fn bget<'v>(block: &'v Value, key: &str) -> OpResult<&'v Value> {
    match block {
        Value::Object(map) => Ok(get(map, key)),
        _ => internal("segment block has no attribute 'get'"),
    }
}

fn hashable(value: &Value) -> OpResult<()> {
    if value.is_array() || value.is_object() {
        return internal("unhashable type");
    }
    Ok(())
}

fn occurrence_key(row: &Obj) -> OpResult<Vec<Value>> {
    let reference = match row.get("inspectorRef") {
        Some(Value::Object(reference)) => get(reference, "turn").clone(),
        _ => Value::Null,
    };
    let (attempt, task) = execution_identity(row);
    let call = get(row, "toolCallId");
    let key = vec![
        Value::String(attempt),
        Value::String(task),
        reference,
        execution_llm_round(row).unwrap_or(Value::Null),
        get(row, "roundNum").clone(),
        if truthy(call) { call.clone() } else { json!("") },
        get(row, "toolName").clone(),
    ];
    for part in &key {
        hashable(part)?;
    }
    Ok(key)
}

fn keys_equal(left: &[Value], right: &[Value]) -> bool {
    left.len() == right.len() && left.iter().zip(right).all(|(a, b)| py_eq(a, b))
}

fn compatible_occurrence(left: &Obj, right: &Obj) -> OpResult<bool> {
    let empty = |value: &Value| value.is_null() || value == &json!("");
    Ok(occurrence_key(left)?
        .iter()
        .zip(occurrence_key(right)?.iter())
        .all(|(a, b)| empty(a) || empty(b) || py_eq(a, b)))
}

fn lookup<K>(table: &mut Vec<(K, VecDeque<usize>)>, key: K, eq: impl Fn(&K, &K) -> bool) -> &mut VecDeque<usize> {
    let position = match table.iter().position(|(candidate, _)| eq(candidate, &key)) {
        Some(position) => position,
        None => {
            table.push((key, VecDeque::new()));
            table.len() - 1
        }
    };
    &mut table[position].1
}

fn iterable_items(value: &Value) -> OpResult<Vec<Value>> {
    Ok(match value {
        _ if !truthy(value) => vec![],
        Value::Array(items) => items.clone(),
        Value::Object(map) => map.keys().map(|key| Value::String(key.clone())).collect(),
        Value::String(text) => text.chars().map(|c| Value::String(c.to_string())).collect(),
        _ => return internal("object is not iterable"),
    })
}

fn seal_rounds(mut rounds: Vec<Value>, additions: &Value) -> OpResult<Vec<Value>> {
    let mut exact: Vec<(Vec<Value>, VecDeque<usize>)> = Vec::new();
    let mut by_id: Vec<(String, Vec<usize>)> = Vec::new();
    let mut by_ordinal: Vec<(Vec<Value>, VecDeque<usize>)> = Vec::new();
    for (index, row) in rounds.iter().enumerate() {
        let Value::Object(row) = row else { continue };
        lookup(&mut exact, occurrence_key(row)?, |a, b| keys_equal(a, b)).push_back(index);
        if truthy(get(row, "toolCallId")) {
            let id = python_str(get(row, "toolCallId"));
            match by_id.iter_mut().find(|(candidate, _)| *candidate == id) {
                Some((_, list)) => list.push(index),
                None => by_id.push((id, vec![index])),
            }
        } else {
            let key = vec![get(row, "roundNum").clone(), get(row, "toolName").clone()];
            for part in &key {
                hashable(part)?;
            }
            lookup(&mut by_ordinal, key, |a, b| keys_equal(a, b)).push_back(index);
        }
    }
    let mut matched: Vec<usize> = Vec::new();
    for source in iterable_items(additions)? {
        let Value::Object(row) = source else { continue };
        let call_id = text(get(&row, "toolCallId"));
        let ordinal_is_int = matches!(row.get("roundNum"), Some(Value::Bool(_)))
            || matches!(row.get("roundNum"), Some(Value::Number(number)) if !number.is_f64());
        if call_id.is_empty() && !ordinal_is_int {
            return internal("Completion tool occurrence needs a call ID or ordinal");
        }
        let queue = lookup(&mut exact, occurrence_key(&row)?, |a, b| keys_equal(a, b));
        while queue.front().is_some_and(|front| matched.contains(front)) {
            queue.pop_front();
        }
        let mut index = queue.pop_front();
        let candidates: Vec<usize> = if call_id.is_empty() {
            vec![]
        } else {
            by_id.iter().find(|(candidate, _)| *candidate == call_id).map(|(_, list)| list.clone()).unwrap_or_default()
        };
        if index.is_none()
            && candidates.len() == 1
            && !matched.contains(&candidates[0])
            && compatible_occurrence(rounds[candidates[0]].as_object().expect("mapping"), &row)?
        {
            index = Some(candidates[0]);
        }
        if index.is_none() && call_id.is_empty() {
            let key = vec![get(&row, "roundNum").clone(), get(&row, "toolName").clone()];
            for part in &key {
                hashable(part)?;
            }
            let ordinal_queue = lookup(&mut by_ordinal, key, |a, b| keys_equal(a, b));
            while ordinal_queue.front().is_some_and(|front| matched.contains(front)) {
                ordinal_queue.pop_front();
            }
            if let Some(front) = ordinal_queue.front().copied() {
                if compatible_occurrence(rounds[front].as_object().expect("mapping"), &row)? {
                    index = ordinal_queue.pop_front();
                }
            }
        }
        match index {
            None => rounds.push(Value::Object(row)),
            Some(index) => {
                matched.push(index);
                let Value::Object(target) = &mut rounds[index] else { unreachable!() };
                for (key, value) in row {
                    if !target.contains_key(&key) {
                        target.insert(key, value);
                    }
                }
            }
        }
    }
    Ok(rounds)
}

fn append_completion_blocks(segments: &mut Vec<Value>, additions: &Value) -> OpResult<()> {
    let mut by_id: Vec<(Value, usize)> = Vec::new();
    for (index, block) in segments.iter().enumerate() {
        if let Value::Object(map) = block {
            let id = get(map, "blockId");
            if truthy(id) {
                hashable(id)?;
                match by_id.iter_mut().find(|(candidate, _)| py_eq(candidate, id)) {
                    Some(slot) => slot.1 = index,
                    None => by_id.push((id.clone(), index)),
                }
            }
        }
    }
    for (position, source) in iterable_items(additions)?.into_iter().enumerate() {
        let Value::Object(mut block) = source else { continue };
        let identity = if truthy(get(&block, "blockId")) {
            block["blockId"].clone()
        } else {
            Value::String(format!("{}:completion-block-{position}", python_str(get(&block, "type"))))
        };
        hashable(&identity)?;
        block.insert("blockId".into(), identity.clone());
        let previous = by_id.iter().find(|(candidate, _)| py_eq(candidate, &identity)).map(|(_, index)| *index);
        match previous {
            None => {
                segments.push(Value::Object(block));
                by_id.push((identity, segments.len() - 1));
            }
            Some(index) => {
                let kind = get(&block, "type");
                hashable(kind)?;
                let previous_block = segments[index].as_object().expect("mapping");
                let extends = matches!(kind.as_str(), Some("text" | "thinking"))
                    && py_eq(get(previous_block, "type"), kind)
                    && match get(&block, "text") {
                        Value::String(new_text) => new_text.starts_with(&text(get(previous_block, "text"))),
                        _ => false,
                    };
                if extends {
                    segments[index]["text"] = block["text"].clone();
                }
            }
        }
    }
    Ok(())
}

fn is_text_block(block: &Value, kind: &str) -> OpResult<bool> {
    Ok(bget(block, "type")? == &json!(kind))
}

/// `seal_output_projection(live, completion)`.
fn seal_output_projection(live: &Obj, completion: &Obj) -> OpResult<Obj> {
    let mut result = live.clone();
    for (key, value) in completion {
        if matches!(key.as_str(), "content" | "thinking" | "toolRounds" | "segments") {
            continue;
        }
        match (value, result.get(key)) {
            (Value::Object(update), Some(Value::Object(current))) => {
                let mut merged = current.clone();
                for (inner, item) in update {
                    merged.insert(inner.clone(), item.clone());
                }
                result.insert(key.clone(), Value::Object(merged));
            }
            _ => {
                result.insert(key.clone(), value.clone());
            }
        }
    }
    let segments_were_list = result.get("segments").is_some_and(Value::is_array);
    let mut segments: Vec<Value> = match result.get("segments") {
        Some(Value::Array(items)) => items.clone(),
        _ => vec![],
    };
    append_completion_blocks(&mut segments, get(completion, "segments"))?;
    let mut terminal_text = Value::Null;
    for block in segments.iter().rev() {
        if is_text_block(block, "text")? && truthy(bget(block, "terminal")?) {
            terminal_text = block.get("text").cloned().unwrap_or(Value::Null);
            break;
        }
    }
    let old_content = text(get(live, "content"));
    let completion_content = get(completion, "content");
    let final_content = text(if truthy(completion_content) { completion_content } else { &terminal_text });
    if !final_content.is_empty() && final_content != old_content {
        let mut recorded: Option<usize> = None;
        for (index, block) in segments.iter().enumerate().rev() {
            if is_text_block(block, "text")? && bget(block, "text")? == &Value::String(final_content.clone()) {
                recorded = Some(index);
                break;
            }
        }
        let extends = !old_content.is_empty() && final_content.starts_with(&old_content);
        let mut preserved = false;
        for (index, block) in segments.iter_mut().enumerate() {
            if is_text_block(block, "text")? && truthy(bget(block, "terminal")?) {
                if extends && bget(block, "text")? == &Value::String(old_content.clone()) && recorded.is_none() {
                    block["text"] = Value::String(final_content.clone());
                    recorded = Some(index);
                }
                if recorded != Some(index) {
                    let map = block.as_object_mut().expect("mapping");
                    map.shift_remove("terminal");
                    map.insert("deliverable".into(), json!(false));
                }
                preserved = preserved || block.get("text") == Some(&Value::String(old_content.clone()));
            }
        }
        if !old_content.is_empty() && !extends && !preserved {
            let mut present = false;
            for block in &segments {
                if is_text_block(block, "text")? && bget(block, "text")? == &Value::String(old_content.clone()) {
                    present = true;
                    break;
                }
            }
            if !present {
                segments.push(json!({"type": "text", "text": old_content, "deliverable": false}));
            }
        }
        let recorded = match recorded {
            Some(index) => index,
            None => {
                let mut used: Vec<Value> = Vec::new();
                for block in &segments {
                    let id = bget(block, "blockId")?;
                    hashable(id)?;
                    used.push(id.clone());
                }
                let mut suffix = segments.len();
                while used.iter().any(|id| id == &Value::String(format!("text:completion-{suffix}"))) {
                    suffix += 1;
                }
                segments.push(
                    json!({"type": "text", "text": final_content, "blockId": format!("text:completion-{suffix}")}),
                );
                segments.len() - 1
            }
        };
        let map = segments[recorded].as_object_mut().expect("mapping");
        map.insert("terminal".into(), json!(true));
        map.insert("deliverable".into(), json!(true));
        result.insert("content".into(), Value::String(final_content));
    } else if !result.contains_key("content") {
        result.insert("content".into(), Value::String(old_content));
    }
    let mut terminal_thinking = Value::Null;
    for block in segments.iter().rev() {
        if is_text_block(block, "thinking")? && truthy(bget(block, "terminal")?) {
            terminal_thinking = block.get("text").cloned().unwrap_or(Value::Null);
            break;
        }
    }
    let old_thinking = text(get(live, "thinking"));
    let completion_thinking = get(completion, "thinking");
    let final_thinking = text(if truthy(completion_thinking) { completion_thinking } else { &terminal_thinking });
    let thinking = if final_thinking.starts_with(&old_thinking) { final_thinking } else { old_thinking };
    result.insert("thinking".into(), Value::String(thinking));
    if segments_were_list || !segments.is_empty() {
        result.insert("segments".into(), Value::Array(segments));
    }
    let rounds = match result.get("toolRounds") {
        Some(Value::Array(items)) => items.clone(),
        _ => vec![],
    };
    let sealed = seal_rounds(rounds, get(completion, "toolRounds"))?;
    result.insert("toolRounds".into(), Value::Array(sealed));
    Ok(result)
}

// ── _turns_read.py visible helpers ───────────────────────────────────────

fn blank(value: &Value) -> bool {
    py_strip(&text(value)).is_empty()
}

/// `_partition_visible_messages(messages)[0]`.
fn kept_visible_messages(messages: &Value) -> Vec<Value> {
    let source: Vec<Value> = match messages {
        Value::Array(items) => items.clone(),
        _ => vec![],
    };
    let mut dropped: Vec<usize> = Vec::new();
    for (index, message) in source.iter().enumerate() {
        if let Value::Object(map) = message {
            if map.get("role") == Some(&json!("user"))
                && truthy(get(map, "_isVirtualUser"))
                && !truthy(get(map, "_flowLivePhase"))
                && blank(get(map, "content"))
            {
                dropped.push(index);
            }
        }
    }
    for (index, message) in source.iter().enumerate() {
        if dropped.contains(&index) {
            continue;
        }
        let Value::Object(map) = message else { continue };
        if map.get("role") == Some(&json!("assistant"))
            && blank(get(map, "content"))
            && blank(get(map, "thinking"))
            && map.get("finishReason") == Some(&json!("aborted"))
            && !truthy(get(map, "toolRounds"))
            && index > 0
            && dropped.contains(&(index - 1))
        {
            dropped.push(index);
        }
    }
    source.into_iter().enumerate().filter(|(index, _)| !dropped.contains(index)).map(|(_, message)| message).collect()
}

fn missing(value: Option<&Value>) -> bool {
    matches!(value, None | Some(Value::Null)) || value == Some(&json!(""))
}

/// `_inherit_live_round_fields`.
fn inherit_live_round_fields(projection: &mut Obj, live: &Obj) {
    let (Some(Value::Array(rounds)), Some(Value::Array(live_rounds))) =
        (projection.get("toolRounds"), live.get("toolRounds"))
    else {
        return;
    };
    let mut live_by_id: Vec<(String, Obj)> = Vec::new();
    for item in live_rounds {
        if let Value::Object(item) = item {
            if truthy(get(item, "toolCallId")) {
                let id = python_str(get(item, "toolCallId"));
                match live_by_id.iter_mut().find(|(candidate, _)| *candidate == id) {
                    Some(slot) => slot.1 = item.clone(),
                    None => live_by_id.push((id, item.clone())),
                }
            }
        }
    }
    if live_by_id.is_empty() {
        return;
    }
    let mut rounds = rounds.clone();
    for round in rounds.iter_mut() {
        let Value::Object(round) = round else { continue };
        let id = text(get(round, "toolCallId"));
        let Some((_, live)) = live_by_id.iter().find(|(candidate, _)| *candidate == id) else { continue };
        for field in ["toolArgs", "toolContent", "tStart", "tEnd", "attemptId", "taskId"] {
            if missing(round.get(field)) && !missing(live.get(field)) {
                round.insert(field.into(), live[field].clone());
            }
        }
    }
    projection.insert("toolRounds".into(), Value::Array(rounds));
}

/// `_visible_shape`.
fn visible_shape(
    message: &Value,
    default_kind: &str,
    live_projection: Option<&Obj>,
    seal_live: bool,
    status: &str,
) -> OpResult<(String, String, Obj)> {
    let message = normalize_flow_message(message)?;
    let role = get(&message, "role");
    let flag = |key: &str| truthy(get(&message, key));
    let critic_or_assistant = if role == &json!("user") { "critic" } else { "assistant" };
    let (actor, kind) = if flag("_isVirtualUser") {
        ("virtual_user".to_owned(), "autopilot_virtual_user".to_owned())
    } else if flag("_isFlowReview") {
        ("critic".into(), "flow_node".into())
    } else if flag("_isFlowPlanner") {
        ("planner".into(), "flow_node".into())
    } else if flag("_flowNodeId") || flag("_flowRunId") {
        (critic_or_assistant.into(), "flow_node".into())
    } else {
        (critic_or_assistant.into(), if default_kind.is_empty() { "flow_node".into() } else { default_kind.into() })
    };
    let mut projection = normalize_projection_document(&Value::Object(message.clone()))?;
    for (key, default) in [("content", json!("")), ("thinking", json!("")), ("toolRounds", json!([]))] {
        if !projection.contains_key(key) {
            projection.insert(key.into(), default);
        }
    }
    if let Some(live) = live_projection {
        if seal_live {
            let mut sealed_live = projection_with_stable_segments(&Value::Object(live.clone()), &actor, "running")?;
            if actor == "virtual_user" {
                let content = get(&sealed_live, "content");
                let content = if truthy(content) { content.clone() } else { json!("") };
                sealed_live.insert("content".into(), strip_machine_tokens(&content)?);
                let blocks = iterable_items(get(&sealed_live, "segments"))?;
                let mut stripped = Vec::with_capacity(blocks.len());
                for block in blocks {
                    if bget(&block, "type")? == &json!("text") {
                        let mut block = block.as_object().cloned().expect("mapping");
                        let Some(text_value) = block.get("text").cloned() else { return internal("KeyError: 'text'") };
                        block.insert("text".into(), strip_machine_tokens(&text_value)?);
                        stripped.push(Value::Object(block));
                    } else {
                        stripped.push(block);
                    }
                }
                sealed_live.insert("segments".into(), Value::Array(stripped));
            }
            projection = seal_output_projection(&sealed_live, &projection)?;
            projection = projection_with_stable_segments(&Value::Object(projection), &actor, "running")?;
        } else {
            inherit_live_round_fields(&mut projection, live);
        }
    }
    let mut projection = projection_with_stable_segments(&Value::Object(projection), &actor, status)?;
    let mut phase = match projection.get("orchestration") {
        Some(Value::Object(map)) => map.clone(),
        _ => Obj::new(),
    };
    let either = |a: &str, b: &str| {
        let first = get(&message, a);
        if truthy(first) {
            first.clone()
        } else {
            get(&message, b).clone()
        }
    };
    for (key, value) in [
        ("iteration", either("_flowIteration", "_flowPlannerIteration")),
        ("approved", get(&message, "_flowApproved").clone()),
        ("nextPhase", get(&message, "_flowNextPhase").clone()),
        ("stuck", get(&message, "_isStuck").clone()),
        ("flowNodeId", get(&message, "_flowNodeId").clone()),
        ("flowRunId", get(&message, "_flowRunId").clone()),
    ] {
        phase.insert(key.into(), value);
    }
    let phase: Obj = phase.into_iter().filter(|(_, value)| !value.is_null()).collect();
    projection.insert("orchestration".into(), Value::Object(phase));
    Ok((actor, kind, projection))
}

/// `_visible_settlement`.
fn visible_settlement(message: &Value) -> OpResult<(String, Value)> {
    let Value::Object(message) = message else { return internal("message has no attribute 'get'") };
    let (status, cause) = if message.contains_key("finishReason") || message.contains_key("streamState") {
        derive_task_verdict(message)
    } else if truthy(get(message, "error")) {
        ("failed".to_owned(), "generation_error".to_owned())
    } else {
        ("completed".to_owned(), "phase_completed".to_owned())
    };
    let settlement = json!({
        "outcome": status,
        "cause": cause,
        "providerFinishReason": get(message, "finishReason").clone(),
        "error": get(message, "error").clone(),
        "resumeOptions": [],
    });
    Ok((status, settlement))
}

fn fetch_turn(session: &Session<'_>, turn_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one("SELECT * FROM storage_conversation_turns WHERE turn_id=?", &params![turn_id])
}

/// `_turn_visible_sync`.
pub fn turn_visible_sync(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv_id = required_text(payload, "conversation_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let attempt_id = required_text(payload, "attempt_id", 128)?;
    let root_id = required_text(payload, "root_turn_id", 128)?;
    let live = truthy(get(payload, "live"));
    let offset = integer(payload, "start_index", Some(0), Some(0), None)?;
    let source = get(payload, "messages");
    let source = if truthy(source) { source.clone() } else { json!([]) };
    let messages = if live { iterable_items(&source)? } else { kept_visible_messages(&source) };
    if offset + messages.len() as i64 > 4096 || (live && messages.len() != 1) {
        return fail("database_protocol_error", "Invalid visible phase window");
    }
    if messages.is_empty() {
        return Ok(Value::Null);
    }
    session.lock_key("conversation", &format!("{user_id}:{conv_id}"));
    session.lock_key("turn_conversation", &format!("{user_id}:{conv_id}"));
    session.lock_key("attempt_events", &attempt_id);
    let attempt = session
        .fetch_one("SELECT * FROM storage_generation_attempts WHERE attempt_id=?", &params![attempt_id.clone()])?;
    let root = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND turn_id=?",
        &params![conv_id.clone(), root_id.clone()],
    )?;
    let (Some(attempt), Some(mut root)) = (attempt, root) else { return Ok(Value::Null) };
    if !matches!(attempt.text("status").as_str(), "pending" | "running")
        || root.int("user_id")? != user_id
        || root.get("current_attempt_id").as_str() != Some(attempt_id.as_str())
    {
        return Ok(Value::Null);
    }
    let root_base = root.opt_int("projection_revision").unwrap_or(0);
    require_conversation_publication_claim(
        session,
        &attempt_id,
        &text(&attempt.get("task_id").to_json()?),
        user_id,
        get(payload, "worker_claim"),
        !live,
        true,
    )?;
    let root_before = projection_map(session, &root)?;
    let now = now_ms();
    let run_id = {
        let stored = text(&root.get("run_id").to_json()?);
        if !stored.is_empty() {
            stored
        } else {
            text_or(get(payload, "run_id"), &attempt_id)
        }
    };
    let default_kind = text_or(get(payload, "default_kind"), "flow_node");
    let mut previous_id: Value = if offset != 0 {
        Value::String(visible_phase_identity(&root_id, &attempt_id, offset - 1).0)
    } else {
        root.get("parent_turn_id").to_json()?
    };
    let mut related: Vec<Value> = if offset != 0 { vec![turn_public(session, &root)?] } else { vec![] };
    let mut visible_ids: Vec<Value> =
        (0..offset).map(|index| Value::String(visible_phase_identity(&root_id, &attempt_id, index).0)).collect();
    let mut child_events: Vec<Value> = Vec::new();
    let mut changed = false;
    let mut sync_event: Option<Value> = None;
    let task_id = text(&attempt.get("task_id").to_json()?);
    for (position, message) in messages.iter().enumerate() {
        let index = offset + position as i64;
        let (phase_status, settlement) =
            if live { ("running".to_owned(), json!({})) } else { visible_settlement(message)? };
        let (actor, kind, projection) = visible_shape(
            message,
            &default_kind,
            if live { None } else { Some(&root_before) },
            index == 0 && !live,
            if index == 0 { "running" } else { &phase_status },
        )?;
        if index == 0 {
            visible_ids.push(Value::String(root_id.clone()));
            previous_id = Value::String(root_id.clone());
            if !is_flow_turn_kind(root.get("kind"))? {
                let updated = session.execute(
                    "UPDATE storage_conversation_turns SET actor=?,kind=?,run_id=?,projection_json=?,projection_revision=?,projection_checkpoint_revision=NULL,projection_materialized_revision=NULL,projection_patch_count=0,projection_patch_bytes=0,updated_at=? WHERE turn_id=? AND current_attempt_id=? AND projection_revision=?",
                    &params![
                        actor,
                        kind,
                        run_id.clone(),
                        dump_param(&Value::Object(projection))?,
                        root_base + 1,
                        now,
                        root_id.clone(),
                        attempt_id.clone(),
                        root_base
                    ],
                )?;
                if updated != 1 {
                    return fail("database_conflict", "The visible root turn changed during synchronization.");
                }
                delete_checkpoint(session, &root_id)?;
                root = fetch_turn(session, &root_id)?.expect("updated root");
                changed = true;
            }
            upsert_turn_search_row(session, &root, None)?;
            related.push(turn_public(session, &root)?);
            continue;
        }
        let (turn_id, child_attempt_id) = visible_phase_identity(&root_id, &attempt_id, index);
        visible_ids.push(Value::String(turn_id.clone()));
        let mut existing = fetch_turn(session, &turn_id)?;
        if let Some(row) = &existing {
            if row.get("current_attempt_id").as_str() != Some(child_attempt_id.as_str())
                || row.get("conversation_id").as_str() != Some(conv_id.as_str())
                || row.int("user_id")? != user_id
            {
                return fail("database_conflict", "Visible phase attempt was superseded");
            }
        }
        match &existing {
            None => {
                let ordinal_row = session.fetch_one(
                    "SELECT COALESCE(MAX(ordinal),-1) AS ordinal FROM storage_conversation_turns WHERE conversation_id=? AND lane_id=?",
                    &[SqlValue::Text(conv_id.clone()), root.get("lane_id").clone()],
                )?;
                let ordinal = ordinal_row.map(|row| row.int("ordinal")).transpose()?.unwrap_or(-1) + 1;
                session.execute(
                    "INSERT INTO storage_conversation_turns (turn_id,conversation_id,user_id,lane_id,parent_turn_id,ordinal,actor,kind,run_id,status,current_attempt_id,projection_json,projection_revision,settlement_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    &[
                        SqlValue::Text(turn_id.clone()),
                        SqlValue::Text(conv_id.clone()),
                        root.get("user_id").clone(),
                        root.get("lane_id").clone(),
                        crate::session::json_param(&previous_id)?,
                        SqlValue::Integer(ordinal),
                        SqlValue::Text(actor),
                        SqlValue::Text(kind),
                        SqlValue::Text(run_id.clone()),
                        SqlValue::Text(phase_status.clone()),
                        SqlValue::Text(child_attempt_id.clone()),
                        dump_param(&Value::Object(projection))?,
                        SqlValue::Integer(1),
                        dump_param(&settlement)?,
                        SqlValue::Integer(now),
                        SqlValue::Integer(now),
                    ],
                )?;
                session.execute(
                    "INSERT INTO storage_generation_attempts (attempt_id,conversation_id,turn_id,command_id,task_id,operation,status,base_projection_revision,resume_anchor_json,config_json,error_json,created_at,started_at,settled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    &params![
                        child_attempt_id.clone(),
                        conv_id.clone(),
                        turn_id.clone(),
                        format!("run:{attempt_id}:visible:{index}"),
                        task_id.clone(),
                        "generate",
                        phase_status.clone(),
                        0i64,
                        dump_param(&json!({}))?,
                        dump_param(&json!({"runId": run_id.clone(), "flowRootAttemptId": attempt_id.clone()}))?,
                        dump_param(&json!({}))?,
                        now,
                        now,
                        if live { None } else { Some(now) }
                    ],
                )?;
                let event_type = if live { "status_changed" } else { "terminal_settlement" };
                let event = json!({
                    "conversationId": conv_id,
                    "turnId": turn_id,
                    "attemptId": child_attempt_id,
                    "seq": 1,
                    "projectionRevision": 1,
                    "type": event_type,
                    "payload": {"status": phase_status, "settlement": settlement},
                });
                insert_attempt_event(
                    session,
                    &child_attempt_id,
                    1,
                    &conv_id,
                    &turn_id,
                    1,
                    event_type,
                    &event,
                    now,
                    false,
                )?;
                existing = fetch_turn(session, &turn_id)?;
                changed = true;
            }
            Some(row) if !live && matches!(row.text("status").as_str(), "pending" | "running") => {
                let live_projection = projection_map(session, row)?;
                let (_, _, projection) =
                    visible_shape(message, &default_kind, Some(&live_projection), true, &phase_status)?;
                let mut record = Payload::new();
                record.insert("worker_claim".into(), get(payload, "worker_claim").clone());
                record.insert("attempt_id".into(), Value::String(child_attempt_id.clone()));
                record.insert("user_id".into(), Value::from(user_id));
                record.insert("flow_root_attempt_id".into(), Value::String(attempt_id.clone()));
                record.insert("terminal".into(), json!(true));
                record.insert("status".into(), Value::String(phase_status.clone()));
                record.insert("projection".into(), Value::Object(projection));
                record.insert("settlement".into(), settlement.clone());
                record.insert("event_type".into(), json!("terminal_settlement"));
                record.insert("event_payload".into(), json!({}));
                let result = event_record(session, &record, false)?;
                if !truthy(get(&as_obj(&result), "applied")) {
                    return fail("database_conflict", "Visible phase settlement lost authority");
                }
                if let Some(Value::Array(events)) = result.get("_conversationSyncAttemptEvents") {
                    child_events.extend(events.iter().cloned());
                }
                existing = fetch_turn(session, &turn_id)?;
            }
            Some(row) if !matches!(row.text("status").as_str(), "pending" | "running") => {
                let previous = projection_map(session, row)?;
                let (_, _, replay) = visible_shape(message, &default_kind, Some(&previous), true, &phase_status)?;
                let stored = load(row.get("settlement_json"))?;
                let stored_cause = match &stored {
                    Value::Object(map) => get(map, "cause").clone(),
                    _ if !truthy(&stored) => Value::Null,
                    _ => return internal("settlement has no attribute 'get'"),
                };
                if live
                    || row.text("status") != phase_status
                    || !py_eq(&Value::Object(replay), &Value::Object(previous))
                    || !py_eq(&stored_cause, &settlement["cause"])
                {
                    return fail("database_conflict", "Visible phase already settled differently");
                }
            }
            Some(_) => {}
        }
        let existing = existing.expect("visible phase row");
        upsert_turn_search_row(session, &existing, None)?;
        related.push(turn_public(session, &existing)?);
        previous_id = Value::String(turn_id);
    }
    if changed {
        root = fetch_turn(session, &root_id)?.expect("root");
        let mut revision = root.opt_int("projection_revision").unwrap_or(0);
        let root_after: Obj = if revision == root_base {
            let mut root_public = turn_public(session, &root)?;
            let before = Value::Object(root_before.clone());
            revision = advance_unchanged_revision(
                session,
                &root,
                &before,
                &build_projection_patch(&before, &before, root_base, root_base + 1),
                now,
            )?;
            root_public["projectionRevision"] = Value::from(revision);
            root_public["updatedAt"] = Value::from(now);
            related[0] = root_public;
            root_before.clone()
        } else if revision != root_base + 1 {
            return fail("database_conflict", "The visible root turn revision advanced unexpectedly.");
        } else {
            related[0] = turn_public(session, &root)?;
            projection_map(session, &root)?
        };
        let mut event = Obj::new();
        event.insert(
            "projectionPatch".into(),
            build_projection_patch(
                &Value::Object(root_before.clone()),
                &Value::Object(root_after),
                root_base,
                revision,
            ),
        );
        event.insert(
            "turns".into(),
            Value::Array(
                related
                    .into_iter()
                    .filter(|item| item.get("turnId") != Some(&Value::String(root_id.clone())))
                    .collect(),
            ),
        );
        event.insert("updateKind".into(), json!("visible_turns_committed"));
        let appended =
            turn_event_append(session, &attempt_id, &conv_id, &root_id, revision, "projection_updated", event)?;
        session.execute(
            "UPDATE storage_conversations SET rev=rev+1, updated_at_ms=? WHERE id=? AND user_id=?",
            &[SqlValue::Integer(now), SqlValue::Text(conv_id), root.get("user_id").clone()],
        )?;
        sync_event = Some(appended["event"].clone());
    }
    let mut events = child_events;
    events.extend(sync_event);
    Ok(json!({"visibleTurnIds": visible_ids, "_conversationSyncAttemptEvents": events}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid5_matches_python() {
        // uuid.uuid5(uuid.NAMESPACE_URL, 'python.org')
        assert_eq!(uuid5_url("python.org"), "7af94e2b-4dd9-50f0-9c9a-8a48519bdef0");
    }
}
