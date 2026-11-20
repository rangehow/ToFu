//! Private port of `lib/turn_projection_patch.py::normalize_projection_document`
//! and the pure normalizers it calls (`background_command_projection.py`,
//! `plan_contract.py`, `model_routing/route_snapshot.py::legacy_route_snapshot`,
//! `turn_activity_timeline.py::normalize_activity_timeline`).

use regex::Regex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::core::{text, text_or, value_in, Obj};
use crate::error::OpResult;
use crate::json::{python_float_repr, python_str, truthy};
use crate::payload::get;

/// Python `json.dumps(value, ensure_ascii=False, separators=(',', ':'))`.
pub fn py_json_dumps(value: &Value, sort_keys: bool) -> String {
    fn write(out: &mut String, value: &Value, sort_keys: bool) {
        match value {
            Value::Null => out.push_str("null"),
            Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
            Value::Number(number) => {
                if let Some(integer) = number.as_i64() {
                    out.push_str(&integer.to_string());
                } else if let Some(integer) = number.as_u64() {
                    out.push_str(&integer.to_string());
                } else {
                    out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0)));
                }
            }
            Value::String(text) => out.push_str(&serde_json::to_string(text).unwrap_or_default()),
            Value::Array(items) => {
                out.push('[');
                for (index, item) in items.iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    write(out, item, sort_keys);
                }
                out.push(']');
            }
            Value::Object(map) => {
                out.push('{');
                let mut entries: Vec<(&String, &Value)> = map.iter().collect();
                if sort_keys {
                    entries.sort_by(|a, b| a.0.cmp(b.0));
                }
                for (index, (key, item)) in entries.into_iter().enumerate() {
                    if index > 0 {
                        out.push(',');
                    }
                    out.push_str(&serde_json::to_string(key).unwrap_or_default());
                    out.push(':');
                    write(out, item, sort_keys);
                }
                out.push('}');
            }
        }
    }
    let mut out = String::new();
    write(&mut out, value, sort_keys);
    out
}

/// Python `str.isspace` for one character.
pub fn py_isspace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python `str.strip()`.
pub fn py_strip(value: &str) -> &str {
    value.trim_matches(py_isspace)
}

fn chars_prefix(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn copy_named(value: &Value, fields: &[&str]) -> Obj {
    match value {
        Value::Object(map) => {
            map.iter().filter(|(key, _)| fields.contains(&key.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect()
        }
        _ => Obj::new(),
    }
}

/// `_positive_int`.
fn positive_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => match number.as_i64() {
            Some(integer) => Some(integer.max(0)),
            None => number.as_f64().map(|float| (float.trunc() as i64).max(0)),
        },
        _ => None,
    }
}

const VALID_INITIATORS: &[&str] =
    &["autopilot", "brain", "channel", "human", "operator", "peer", "proactive", "swarm", "timer"];
const CHANNEL_ORIGIN_FIELDS: &[&str] = &["channel", "thread", "eventIds", "title"];
const BRAIN_FIELDS: &[&str] =
    &["epicId", "epicTitle", "originatorConv", "originatorTitle", "route", "method", "answered"];
const TRANSLATION_FIELDS: &[&str] = &[
    "status",
    "model",
    "error",
    "taskId",
    "statusMessage",
    "statusKind",
    "partial",
    "partialByRound",
    "sendFailure",
    "skippedReason",
];
const ORIGIN_FIELDS: &[&str] = &[
    "blockId",
    "initiator",
    "sourceConversationId",
    "peerHuman",
    "brain",
    "autopilotRunId",
    "boardTaskId",
    "scheduledTaskId",
    "channel",
    "forwardedFrom",
];
const FORWARDED_FROM_FIELDS: &[&str] = &["conversationId", "turnId", "steerId", "instructionId"];
const COMPACTION_FIELDS: &[&str] = &[
    "blockId",
    "archiveId",
    "conversationId",
    "trigger",
    "timestamp",
    "tokensBefore",
    "tokensAfter",
    "messagesBefore",
    "messagesAfter",
    "reductionPercent",
    "foldedToolRounds",
    "estimatedPromptTokens",
];
const IMAGE_GENERATION_FIELDS: &[&str] = &["blockId", "mode", "status", "results", "error"];
const IMAGE_RESULT_FIELDS: &[&str] = &[
    "ok",
    "prompt",
    "model",
    "providerId",
    "aspectRatio",
    "resolution",
    "imageUrl",
    "remoteImageUrl",
    "fileSize",
    "elapsedSeconds",
    "responseText",
    "error",
    "errorType",
];
const ROLLED_BACK_FIELDS: &[&str] = &["blockId", "attemptId", "at", "content", "thinking"];
const PROJECTION_IDENTITY_KEYS: &[&str] = &[
    "turnId",
    "attemptId",
    "_turnId",
    "_attemptId",
    "_msgId",
    "_taskId",
    "activeTaskId",
    "role",
    "_turnActor",
    "_turnKind",
    "_turnLaneId",
    "_turnStatus",
    "_turnSettlement",
    "_projectionRevision",
    "_commandPending",
    "branches",
];
pub const PUBLIC_PROJECTION_FIELDS: &[&str] = &[
    "content",
    "thinking",
    "toolRounds",
    "segments",
    "usage",
    "usageComplete",
    "apiRounds",
    "cost",
    "lastRoundUsage",
    "model",
    "preset",
    "providerId",
    "routeSnapshot",
    "thinkingDepth",
    "modifiedFiles",
    "modifiedFileList",
    "modifiedFilesTruncated",
    "fileChanges",
    "todoState",
    "waitingOn",
    "fallbackModel",
    "fallbackFrom",
    "fallbackReason",
    "fallbackKind",
    "translatedContent",
    "originalContent",
    "translation",
    "timestamp",
    "images",
    "attachments",
    "videos",
    "pdfTexts",
    "convRefs",
    "replyQuotes",
    "_branchLanes",
    "orchestration",
    "provenance",
    "_inboxInjects",
    "_peerInjects",
    "_userSteerInjects",
    "_bgCommandInjects",
    "backgroundCommand",
    "_stallNudges",
    "origin",
    "contextSnapshot",
    "compaction",
    "imageGeneration",
    "proposedPlan",
    "planExecution",
    "usageRecovery",
    "activityTimeline",
    "timingTrace",
    "rolledBack",
];

fn legacy_initiator(projection: &Obj) -> OpResult<String> {
    let declared = get(projection, "_initiator");
    if value_in(declared, VALID_INITIATORS)? {
        return Ok(python_str(declared));
    }
    let flag = |key: &str| truthy(get(projection, key));
    Ok(if flag("_peerMessage") {
        if flag("_peerHuman") {
            "operator"
        } else {
            "peer"
        }
    } else if flag("_isVirtualUser") || flag("_autopilotRunId") {
        "autopilot"
    } else if flag("_proactive") {
        "proactive"
    } else if flag("_timer") {
        "timer"
    } else if flag("_brainDispatch") {
        "brain"
    } else if flag("_swarmAutoContinue") {
        "swarm"
    } else {
        "human"
    }
    .to_owned())
}

fn normalize_origin(projection: &Obj) -> OpResult<Option<Obj>> {
    let mut origin = copy_named(get(projection, "origin"), ORIGIN_FIELDS);
    let mut brain = copy_named(get(&origin, "brain"), BRAIN_FIELDS);
    let legacy_brain = copy_named(get(projection, "_brainEpic"), BRAIN_FIELDS);
    for (key, value) in legacy_brain {
        brain.insert(key, value);
    }
    if !brain.is_empty() {
        origin.insert("brain".into(), Value::Object(brain));
    } else {
        origin.shift_remove("brain");
    }
    let channel = copy_named(get(&origin, "channel"), CHANNEL_ORIGIN_FIELDS);
    if truthy(get(&channel, "channel")) && truthy(get(&channel, "thread")) {
        let normalized: Obj = channel
            .iter()
            .map(|(key, value)| {
                let item = if key == "eventIds" {
                    match value {
                        Value::Array(items) => {
                            Value::Array(items.iter().map(|i| Value::String(python_str(i))).collect())
                        }
                        _ => Value::Array(vec![]),
                    }
                } else {
                    Value::String(python_str(value))
                };
                (key.clone(), item)
            })
            .collect();
        origin.insert("channel".into(), Value::Object(normalized));
    } else {
        origin.shift_remove("channel");
    }
    let forwarded = copy_named(&origin.shift_remove("forwardedFrom").unwrap_or(Value::Null), FORWARDED_FROM_FIELDS);
    if forwarded.values().all(|value| matches!(value, Value::String(text) if !text.is_empty()))
        && forwarded.contains_key("conversationId")
        && forwarded.contains_key("turnId")
    {
        origin.insert("forwardedFrom".into(), Value::Object(forwarded));
    }
    let declared = get(&origin, "initiator").clone();
    let initiator =
        if value_in(&declared, VALID_INITIATORS)? { python_str(&declared) } else { legacy_initiator(projection)? };
    let has_legacy_origin = [
        "_initiator",
        "_peerMessage",
        "_peerHuman",
        "_fromConv",
        "_brainDispatch",
        "_brainEpic",
        "_isVirtualUser",
        "_autopilotRunId",
        "_proactive",
        "_proactiveTaskId",
        "_timer",
        "_swarmAutoContinue",
        "_boardTaskId",
        "boardTaskId",
    ]
    .iter()
    .any(|key| !get(projection, key).is_null());
    if initiator == "human" && origin.is_empty() && !has_legacy_origin {
        return Ok(None);
    }
    let block = text_or(get(&origin, "blockId"), "origin");
    origin.insert("blockId".into(), Value::String(block));
    origin.insert("initiator".into(), Value::String(initiator));
    let source = if truthy(get(&origin, "sourceConversationId")) {
        get(&origin, "sourceConversationId").clone()
    } else {
        get(projection, "_fromConv").clone()
    };
    if truthy(&source) {
        origin.insert("sourceConversationId".into(), Value::String(python_str(&source)));
    }
    if !get(projection, "_peerHuman").is_null() {
        origin.insert("peerHuman".into(), Value::Bool(truthy(get(projection, "_peerHuman"))));
    }
    for (target, legacy) in [
        ("autopilotRunId", "_autopilotRunId"),
        ("boardTaskId", "_boardTaskId"),
        ("scheduledTaskId", "_proactiveTaskId"),
    ] {
        let mut candidates = vec![get(&origin, target).clone(), get(projection, legacy).clone()];
        if target == "boardTaskId" {
            candidates.push(get(projection, "boardTaskId").clone());
        }
        if let Some(value) = candidates.into_iter().find(truthy) {
            origin.insert(target.into(), Value::String(python_str(&value)));
        }
    }
    Ok(Some(origin))
}

fn normalize_translation(projection: &Obj) -> OpResult<Option<Obj>> {
    let mut translation = copy_named(get(projection, "translation"), TRANSLATION_FIELDS);
    for (key, legacy) in [
        ("model", "_translateModel"),
        ("error", "_translateError"),
        ("taskId", "_translateTaskId"),
        ("statusMessage", "_translateStatus"),
        ("statusKind", "_translateStatusKind"),
        ("partial", "_translatePartial"),
        ("partialByRound", "_translatePartialByRound"),
        ("sendFailure", "_translateFailed"),
        ("skippedReason", "_translateSkippedReason"),
    ] {
        let value = get(projection, legacy);
        if !value.is_null() {
            translation.insert(key.into(), value.clone());
        }
    }
    let done_present = projection.contains_key("_translateDone");
    let done = get(projection, "_translateDone");
    if !get(&translation, "error").is_null() {
        translation.insert("status".into(), json!("failed"));
    } else if truthy(get(&translation, "skippedReason")) {
        translation.insert("status".into(), json!("skipped"));
    } else if done == &Value::Bool(true) {
        translation.insert("status".into(), json!("completed"));
    } else if done == &Value::Bool(false) || truthy(get(&translation, "taskId")) || truthy(get(&translation, "partial"))
    {
        translation.insert("status".into(), json!("pending"));
    } else if (!get(projection, "translatedContent").is_null() || !get(projection, "originalContent").is_null())
        && !translation.contains_key("status")
    {
        translation.insert("status".into(), json!("completed"));
    }
    if translation.is_empty() && !done_present {
        return Ok(None);
    }
    if !value_in(get(&translation, "status"), &["pending", "completed", "skipped", "failed"])? {
        translation.shift_remove("status");
    }
    Ok(if translation.is_empty() { None } else { Some(translation) })
}

fn normalize_context(projection: &Obj) -> Option<Value> {
    if let Some(Value::Object(current)) = projection.get("contextSnapshot") {
        if let Some(snapshot @ Value::Object(_)) = current.get("snapshot") {
            return Some(json!({"blockId": text_or(get(current, "blockId"), "turn-context"), "snapshot": snapshot}));
        }
    }
    if let Some(legacy @ Value::Object(_)) = projection.get("_ctx") {
        return Some(json!({"blockId": "turn-context", "snapshot": legacy}));
    }
    None
}

fn normalize_compaction(projection: &Obj) -> Option<Obj> {
    let mut compaction = copy_named(get(projection, "compaction"), COMPACTION_FIELDS);
    let marker = match get(projection, "_compactions") {
        Value::Array(markers) if !markers.is_empty() => markers[0].as_object().cloned().unwrap_or_default(),
        _ => Obj::new(),
    };
    let is_legacy = get(projection, "_isCompactionSummary") == &Value::Bool(true);
    if compaction.is_empty() && !is_legacy {
        return None;
    }
    let block = text_or(get(&compaction, "blockId"), "compaction");
    compaction.insert("blockId".into(), Value::String(block));
    let or = |left: &Value, right: &Value| if truthy(left) { left.clone() } else { right.clone() };
    let text_fields = [
        ("archiveId", or(get(projection, "_compactionArchiveId"), get(&marker, "archiveId"))),
        ("conversationId", get(&marker, "convId").clone()),
        ("trigger", get(&marker, "trigger").clone()),
    ];
    for (key, value) in text_fields {
        if !value.is_null() && value != Value::String(String::new()) {
            compaction.insert(key.into(), Value::String(python_str(&value)));
        }
    }
    let number_fields = [
        ("timestamp", get(&marker, "ts").clone()),
        ("tokensBefore", get(&marker, "tokensBefore").clone()),
        ("tokensAfter", get(&marker, "tokensAfter").clone()),
        ("messagesBefore", get(&marker, "msgsBefore").clone()),
        ("messagesAfter", get(&marker, "msgsAfter").clone()),
        ("reductionPercent", get(&marker, "reductionPct").clone()),
        ("foldedToolRounds", or(get(&marker, "foldedToolRounds"), get(projection, "_foldedToolRounds"))),
        ("estimatedPromptTokens", get(projection, "_estimatedPromptTokens").clone()),
    ];
    for (key, value) in number_fields {
        if let Some(normalized) = positive_int(&value) {
            compaction.insert(key.into(), Value::from(normalized));
        }
    }
    Some(compaction)
}

/// Python `float(value)` for a decoded JSON value.
fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(flag) => Some(*flag as i64 as f64),
        Value::Number(number) => number.as_f64(),
        Value::String(text) => {
            let cleaned = py_strip(text).replace('_', "");
            match cleaned.to_ascii_lowercase().as_str() {
                "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
                "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
                "nan" | "+nan" | "-nan" => Some(f64::NAN),
                other => other.parse::<f64>().ok(),
            }
        }
        _ => None,
    }
}

fn normalize_image_result(value: &Value) -> Option<Obj> {
    let Value::Object(source) = value else { return None };
    let mut result = copy_named(value, IMAGE_RESULT_FIELDS);
    for (target, legacy) in [
        ("providerId", "provider_id"),
        ("aspectRatio", "aspect_ratio"),
        ("imageUrl", "image_url"),
        ("remoteImageUrl", "remote_image_url"),
        ("fileSize", "file_size"),
        ("elapsedSeconds", "elapsed"),
        ("responseText", "response_text"),
        ("errorType", "error_type"),
    ] {
        if !result.contains_key(target) && !get(source, legacy).is_null() {
            result.insert(target.into(), get(source, legacy).clone());
        }
    }
    let ok = match result.get("ok") {
        Some(value) => truthy(value),
        None => truthy(get(source, "image_url")) || truthy(get(source, "imageUrl")),
    };
    result.insert("ok".into(), Value::Bool(ok));
    let prompt = text(get(&result, "prompt"));
    result.insert("prompt".into(), Value::String(prompt));
    let model = text(get(&result, "model"));
    result.insert("model".into(), Value::String(model));
    if !get(&result, "elapsedSeconds").is_null() {
        match py_float(get(&result, "elapsedSeconds")) {
            Some(seconds) => {
                let bounded = if seconds > 0.0 { seconds } else { 0.0 };
                result.insert("elapsedSeconds".into(), crate::json::float_value(bounded));
            }
            None => {
                result.shift_remove("elapsedSeconds");
            }
        }
    }
    if !get(&result, "fileSize").is_null() {
        match positive_int(get(&result, "fileSize")) {
            Some(size) => {
                result.insert("fileSize".into(), Value::from(size));
            }
            None => {
                result.shift_remove("fileSize");
            }
        }
    }
    Some(result)
}

fn normalize_image_generation(projection: &Obj) -> OpResult<Option<Value>> {
    let current = copy_named(get(projection, "imageGeneration"), IMAGE_GENERATION_FIELDS);
    let legacy_single = get(projection, "_igResult");
    let legacy_batch = get(projection, "_igResults");
    let raw_results: Vec<Value> = match get(&current, "results") {
        Value::Array(items) => items.clone(),
        _ => match (legacy_batch, legacy_single) {
            (Value::Array(items), _) => items.clone(),
            (_, single @ Value::Object(_)) => vec![single.clone()],
            _ => vec![],
        },
    };
    let results: Vec<Obj> = raw_results.iter().filter_map(normalize_image_result).collect();
    let legacy_error = get(projection, "_igError");
    let flag = |key: &str| truthy(get(projection, key));
    let has_legacy = !results.is_empty()
        || flag("_isImageGen")
        || flag("_isImageEdit")
        || !legacy_error.is_null()
        || flag("_igBatchPending");
    if current.is_empty() && !has_legacy {
        return Ok(None);
    }
    let mut mode = get(&current, "mode").clone();
    if !value_in(&mode, &["generate", "edit", "batch"])? {
        mode = json!(if legacy_batch.is_array() {
            "batch"
        } else if flag("_isImageEdit") {
            "edit"
        } else {
            "generate"
        });
    }
    let mut status = get(&current, "status").clone();
    if !value_in(&status, &["running", "completed", "failed", "cancelled"])? {
        status = json!(if flag("_igBatchPending") {
            "running"
        } else if !legacy_error.is_null()
            || (!results.is_empty() && !results.iter().any(|result| truthy(get(result, "ok"))))
        {
            "failed"
        } else {
            "completed"
        });
    }
    let mut document = Obj::new();
    document.insert("blockId".into(), Value::String(text_or(get(&current, "blockId"), "image-generation")));
    document.insert("mode".into(), mode);
    document.insert("status".into(), status);
    document.insert("results".into(), Value::Array(results.into_iter().map(Value::Object).collect()));
    if !get(&current, "error").is_null() {
        document.insert("error".into(), get(&current, "error").clone());
    }
    if !legacy_error.is_null() {
        document.insert("error".into(), legacy_error.clone());
    }
    Ok(Some(Value::Object(document)))
}

/// `background_command_projection.normalize_handoff_round`.
fn normalize_handoff_round(round: Obj) -> OpResult<Obj> {
    if !value_in(get(&round, "toolName"), &["run_command", "code_exec"])? {
        return Ok(round);
    }
    let Value::Array(results) = get(&round, "results").clone() else { return Ok(round) };
    static HANDOFF: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let pattern = HANDOFF.get_or_init(|| {
        Regex::new(r"\n\[Command moved to background: (bg_[a-f0-9]{12})\. The command is still running;[^\n]*\]\s*$")
            .expect("handoff regex")
    });
    let mut normalized = Vec::new();
    for meta in results {
        let eligible = match &meta {
            Value::Object(fields) => {
                !truthy(get(fields, "backgroundCommandId"))
                    && (truthy(get(fields, "backgrounded")) || get(fields, "exitCode") == &json!("background"))
            }
            _ => false,
        };
        if !eligible {
            normalized.push(meta);
            continue;
        }
        let fields = meta.as_object().cloned().unwrap_or_default();
        let source = if truthy(get(&round, "content")) {
            get(&round, "content").clone()
        } else {
            get(&fields, "output").clone()
        };
        let body = text(&source);
        let matched = pattern.captures(&body).map(|captures| Value::String(captures[1].to_owned()));
        let command_id = if truthy(get(&round, "backgroundCommandId")) {
            get(&round, "backgroundCommandId").clone()
        } else {
            matched.unwrap_or(Value::Null)
        };
        if truthy(&command_id) {
            let mut updated = fields;
            updated.insert("backgroundCommandId".into(), command_id);
            normalized.push(Value::Object(updated));
        } else {
            normalized.push(Value::Object(fields));
        }
    }
    let mut round = round;
    round.insert("results".into(), Value::Array(normalized));
    Ok(round)
}

fn public_tool_round(item: &Obj) -> OpResult<Value> {
    let mut round = item.clone();
    if get(&round, "results").is_null() {
        round.shift_remove("results");
    }
    Ok(Value::Object(normalize_handoff_round(round)?))
}

/// `_normalize_tool_rounds`.
pub fn normalize_tool_rounds(value: &Value) -> OpResult<Value> {
    let Value::Array(items) = value else { return Ok(value.clone()) };
    let mut normalized = Vec::with_capacity(items.len());
    for item in items {
        match item {
            Value::Object(round) => normalized.push(public_tool_round(round)?),
            other => normalized.push(other.clone()),
        }
    }
    Ok(Value::Array(normalized))
}

fn normalize_rolled_back(value: &Value) -> Option<Vec<Value>> {
    let Value::Array(items) = value else { return None };
    let mut normalized = Vec::new();
    for item in items {
        if !item.is_object() {
            continue;
        }
        let mut entry = copy_named(item, ROLLED_BACK_FIELDS);
        if !matches!(entry.get("content"), Some(Value::String(text)) if !text.is_empty()) {
            entry.shift_remove("content");
        }
        if !matches!(entry.get("thinking"), Some(Value::String(text)) if !text.is_empty()) {
            entry.shift_remove("thinking");
        }
        if !entry.contains_key("content") && !entry.contains_key("thinking") {
            continue;
        }
        let block = text_or(get(&entry, "blockId"), "rolled-back");
        entry.insert("blockId".into(), Value::String(block));
        if !get(&entry, "attemptId").is_null() {
            let attempt = python_str(get(&entry, "attemptId"));
            entry.insert("attemptId".into(), Value::String(attempt));
        } else {
            entry.shift_remove("attemptId");
        }
        let valid_at =
            matches!(entry.get("at"), Some(Value::Number(number)) if number.as_i64().is_some_and(|at| at > 0));
        if !valid_at {
            entry.shift_remove("at");
        }
        normalized.push(Value::Object(entry));
    }
    if normalized.is_empty() {
        return None;
    }
    let start = normalized.len().saturating_sub(4);
    Some(normalized[start..].to_vec())
}

// ── plan_contract.py ──────────────────────────────────────────────────────

const MAX_PROPOSED_PLAN_CHARS: usize = 64_000;

fn normalized_plan_text(value: &Value) -> Option<String> {
    let Value::String(raw) = value else { return None };
    let text = py_strip(raw);
    let count = text.chars().count();
    if text.is_empty() || count > MAX_PROPOSED_PLAN_CHARS || text.len() > 4 * MAX_PROPOSED_PLAN_CHARS {
        return None;
    }
    Some(text.to_owned())
}

fn extract_proposed_plan(content: &Value) -> Option<String> {
    let Value::String(text) = content else { return None };
    if !text.to_lowercase().contains("<proposed_plan>") {
        return None;
    }
    static PLAN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let pattern =
        PLAN.get_or_init(|| Regex::new(r"(?is)<proposed_plan>\s*\n?(.*?)\n?\s*</proposed_plan>").expect("plan regex"));
    let last = pattern.captures_iter(text).last()?;
    normalized_plan_text(&Value::String(last[1].to_owned()))
}

fn proposed_plan_id(text: &str) -> String {
    format!("plan_{}", &hex::encode(Sha256::digest(text.as_bytes()))[..24])
}

fn proposed_plan_document(value: &Obj, content: &Value) -> Option<Value> {
    let text = extract_proposed_plan(content)?;
    if normalized_plan_text(get(value, "text")).as_deref() != Some(text.as_str()) {
        return None;
    }
    let plan_id = get(value, "planId");
    if !plan_id.is_null() && plan_id != &Value::String(proposed_plan_id(&text)) {
        return None;
    }
    let block = get(value, "blockId");
    if !block.is_null() && block != &json!("proposed-plan") {
        return None;
    }
    let revision = get(value, "revision");
    if !revision.is_null() && !crate::codec::py_eq(revision, &json!(1)) {
        return None;
    }
    let format = get(value, "format");
    if !format.is_null() && format != &json!("markdown") {
        return None;
    }
    Some(json!({
        "blockId": "proposed-plan",
        "planId": proposed_plan_id(&text),
        "revision": 1,
        "format": "markdown",
        "text": text,
    }))
}

fn plan_execution_document(value: &Value) -> Option<Value> {
    let Value::Object(value) = value else { return None };
    let plan_text = normalized_plan_text(get(value, "planText"))?;
    let source_turn_id = match get(value, "sourceTurnId") {
        Value::String(text) if !py_strip(text).is_empty() => py_strip(text).to_owned(),
        _ => return None,
    };
    let source_revision = match get(value, "sourceProjectionRevision") {
        Value::Number(number) => number.as_i64().filter(|revision| *revision >= 0)?,
        _ => return None,
    };
    let context_mode = text(get(value, "contextMode"));
    if context_mode != "current" && context_mode != "fresh" {
        return None;
    }
    let plan_id = proposed_plan_id(&plan_text);
    let declared = get(value, "planId");
    if !declared.is_null() && declared != &Value::String(plan_id.clone()) {
        return None;
    }
    let block = get(value, "blockId");
    if !block.is_null() && block != &json!("plan-execution") {
        return None;
    }
    Some(json!({
        "blockId": "plan-execution",
        "planId": plan_id,
        "sourceTurnId": source_turn_id,
        "sourceProjectionRevision": source_revision,
        "contextMode": context_mode,
        "planText": plan_text,
    }))
}

// ── background_command_projection.py ─────────────────────────────────────

/// `bounded_completion_preview` (both fields are bounded at 2000 chars).
pub fn bounded_completion_preview(metadata: &Obj) -> Obj {
    let mut result = metadata.clone();
    for field in ["command", "resultPreview"] {
        if let Some(Value::String(value)) = result.get(field) {
            if value.chars().count() > 2000 {
                let truncated = chars_prefix(value, 2000);
                result.insert(field.into(), Value::String(truncated));
                if field == "resultPreview" {
                    result.insert("resultTruncated".into(), Value::Bool(true));
                }
            }
        }
    }
    result
}

fn bound_projection_completion_previews(projection: &mut Obj) {
    if let Some(Value::Object(completion)) = projection.get("backgroundCommand") {
        let bounded = bounded_completion_preview(completion);
        projection.insert("backgroundCommand".into(), Value::Object(bounded));
    }
    let Some(Value::Array(injections)) = projection.get("_bgCommandInjects") else { return };
    let mut normalized = Vec::new();
    for injection in injections {
        let previews = match injection {
            Value::Object(fields) => match fields.get("previews") {
                Some(Value::Array(previews)) => previews,
                _ => {
                    normalized.push(injection.clone());
                    continue;
                }
            },
            _ => {
                normalized.push(injection.clone());
                continue;
            }
        };
        let mut bounded = Vec::new();
        for preview in previews {
            match preview {
                Value::Object(fields) if fields.get("backgroundCommand").is_some_and(Value::is_object) => {
                    let mut fields = fields.clone();
                    let completion =
                        bounded_completion_preview(fields["backgroundCommand"].as_object().expect("object"));
                    fields.insert("backgroundCommand".into(), Value::Object(completion));
                    bounded.push(Value::Object(fields));
                }
                other => bounded.push(other.clone()),
            }
        }
        let mut injection = injection.as_object().cloned().unwrap_or_default();
        injection.insert("previews".into(), Value::Array(bounded));
        normalized.push(Value::Object(injection));
    }
    projection.insert("_bgCommandInjects".into(), Value::Array(normalized));
}

/// `background_command_completion(payload)`.
pub fn background_command_completion(payload: &Obj) -> Option<Value> {
    let Some(Value::String(command_id)) = payload.get("_backgroundCommand") else { return None };
    if command_id.is_empty() {
        return None;
    }
    let metadata = match payload.get("_backgroundCommandCompletion") {
        Some(Value::Object(map)) => map.clone(),
        _ => Obj::new(),
    };
    let mut document = Obj::new();
    document.insert("commandId".into(), Value::String(chars_prefix(command_id, 128)));
    document.insert("status".into(), json!("completed"));
    document.insert("command".into(), Value::String(text(get(&metadata, "command"))));
    document.insert("resultPreview".into(), Value::String(text(get(&metadata, "resultPreview"))));
    document.insert("resultTruncated".into(), Value::Bool(truthy(get(&metadata, "resultTruncated"))));
    if let Some(Value::Number(number)) = metadata.get("exitCode") {
        if number.is_i64() || number.is_u64() {
            document.insert("exitCode".into(), Value::Number(number.clone()));
        }
    }
    for key in ["timedOut", "interrupted"] {
        if let Some(flag @ Value::Bool(_)) = metadata.get(key) {
            document.insert(key.into(), flag.clone());
        }
    }
    Some(Value::Object(bounded_completion_preview(&document)))
}

fn legacy_route_snapshot(model_id: &str, provider_id: &str) -> Value {
    let bounded = |value: &str| chars_prefix(value, 256);
    json!({
        "contract_version": "tofu.route-snapshot/v2",
        "legacy": true,
        "selected_model": null,
        "provider_scoped_selection": null,
        "preferred_provider_id": bounded(provider_id),
        "actual_model": if model_id.is_empty() { Value::Null } else { json!({"creator_id": "legacy", "model_id": bounded(model_id)}) },
        "provider_id": bounded(provider_id),
        "offering_id": "",
        "deployment_id": "",
        "connection_id": "",
        "credential": null,
        "wire_model_id": bounded(model_id),
        "transitions": [],
        "degradation_reasons": ["legacy_turn_projection"],
        "recorded_at": 0.0,
    })
}

/// `normalize_projection_document(raw)`.
pub fn normalize_projection_document(raw: &Value) -> OpResult<Obj> {
    let source = match raw {
        Value::String(content) => {
            let mut document = Obj::new();
            document.insert("content".into(), Value::String(content.clone()));
            return Ok(document);
        }
        Value::Object(map) => map.clone(),
        _ => {
            let mut document = Obj::new();
            document.insert("content".into(), Value::String(String::new()));
            return Ok(document);
        }
    };
    let mut result = source.clone();
    bound_projection_completion_previews(&mut result);
    for key in PROJECTION_IDENTITY_KEYS {
        result.shift_remove(*key);
    }
    if get(&result, "providerId").is_null() && !get(&result, "provider_id").is_null() {
        let provider = get(&result, "provider_id").clone();
        result.insert("providerId".into(), provider);
    }
    result.shift_remove("provider_id");
    if !result.contains_key("content") && result.contains_key("text") {
        let content = if truthy(get(&result, "text")) { get(&result, "text").clone() } else { json!("") };
        result.insert("content".into(), content);
    }
    if !result.contains_key("content") {
        result.insert("content".into(), json!(""));
    }
    if get(&result, "usage").is_null() {
        result.shift_remove("usage");
    }
    if !get(&result, "routeSnapshot").is_object() {
        let model_id = py_strip(&text(get(&result, "model"))).to_owned();
        let provider_id = py_strip(&text(get(&result, "providerId"))).to_owned();
        if !model_id.is_empty() || !provider_id.is_empty() {
            result.insert("routeSnapshot".into(), legacy_route_snapshot(&model_id, &provider_id));
        }
    }
    if result.contains_key("toolRounds") {
        let rounds = normalize_tool_rounds(&result["toolRounds"])?;
        result.insert("toolRounds".into(), rounds);
    }
    if let Some(Value::Array(segments)) = result.get("segments") {
        let mut normalized = Vec::with_capacity(segments.len());
        for segment in segments {
            match segment {
                Value::Object(fields) if fields.get("_round").is_some_and(Value::is_object) => {
                    let round = normalize_tool_rounds(&Value::Array(vec![fields["_round"].clone()]))?;
                    let mut fields = fields.clone();
                    fields.insert("_round".into(), round[0].clone());
                    normalized.push(Value::Object(fields));
                }
                other => normalized.push(other.clone()),
            }
        }
        result.insert("segments".into(), Value::Array(normalized));
    }
    let origin = normalize_origin(&source)?;
    let translation = normalize_translation(&source)?;
    let context_snapshot = normalize_context(&source);
    let compaction = normalize_compaction(&source);
    let image_generation = normalize_image_generation(&source)?;
    let activity_timeline = normalize_activity_timeline(get(&source, "activityTimeline"));
    let proposed_plan = match source.get("proposedPlan") {
        Some(Value::Object(plan)) => proposed_plan_document(plan, get(&source, "content")),
        _ => None,
    };
    let plan_execution = plan_execution_document(get(&source, "planExecution"));
    if let Some(origin) = origin {
        result.insert("origin".into(), Value::Object(origin));
    }
    if let Some(translation) = translation {
        result.insert("translation".into(), Value::Object(translation));
    }
    if let Some(context) = context_snapshot {
        result.insert("contextSnapshot".into(), context);
    }
    if let Some(compaction) = compaction {
        result.insert("compaction".into(), Value::Object(compaction));
    }
    if let Some(image_generation) = image_generation {
        result.insert("imageGeneration".into(), image_generation);
    }
    match activity_timeline {
        Some(timeline) => {
            result.insert("activityTimeline".into(), timeline);
        }
        None => {
            result.shift_remove("activityTimeline");
        }
    }
    match proposed_plan {
        Some(plan) => {
            result.insert("proposedPlan".into(), plan);
        }
        None => {
            result.shift_remove("proposedPlan");
        }
    }
    match plan_execution {
        Some(plan) => {
            result.insert("planExecution".into(), plan);
        }
        None => {
            result.shift_remove("planExecution");
        }
    }
    match normalize_rolled_back(get(&source, "rolledBack")) {
        Some(rolled) => {
            result.insert("rolledBack".into(), Value::Array(rolled));
        }
        None => {
            result.shift_remove("rolledBack");
        }
    }
    Ok(result.into_iter().filter(|(key, _)| PUBLIC_PROJECTION_FIELDS.contains(&key.as_str())).collect())
}

// ── turn_activity_timeline.py::normalize_activity_timeline ───────────────

const ACTIVITY_TIMELINE_MAX_ENTRIES: usize = 128;
const ACTIVITY_TIMELINE_MAX_JSON_BYTES: usize = 96 * 1024;
const INTEGER_MAX: i64 = 9_007_199_254_740_991;
const COUNT_MAX: i64 = 2_147_483_647;

fn string_limit(key: &str) -> Option<usize> {
    Some(match key {
        "id" | "spanId" | "parentSpanId" | "summaryKey" | "detailKey" | "model" | "providerId" | "toolName"
        | "toolCallId" | "reasonCode" | "fromModel" | "toModel" | "archiveId" | "routeId" | "envelopeKind" => 160,
        "kind" | "status" | "tokenCountKind" | "timingMode" | "routeMode" | "anchorPosition" | "requestLevel"
        | "attemptScope" => 24,
        "severity" => 16,
        "summary" => 180,
        "detail" => 400,
        "keyHint" => 64,
        "action" => 48,
        "requestTag" | "phase" | "trigger" | "routeDecision" | "failureStage" => 80,
        "taskId" | "attemptId" => 256,
        "anchorBlockId" => 320,
        "taskModel" | "summaryModel" | "summarySelectedModel" | "summaryProviderId" => 160,
        _ => return None,
    })
}

const INTEGER_FIELDS: &[&str] = &[
    "seq",
    "occurredAt",
    "startedAt",
    "endedAt",
    "roundNum",
    "llmRound",
    "anchorToolRoundNum",
    "count",
    "durationMs",
    "statusCode",
    "tokensBefore",
    "tokensAfter",
    "messagesBefore",
    "messagesAfter",
    "reductionPercent",
    "attempt",
];

/// `_text(value, limit)`.
fn activity_text(value: &Value, limit: usize) -> String {
    let rendered = match value {
        Value::Null => return String::new(),
        Value::String(text) => text.clone(),
        Value::Object(map) => {
            let chosen =
                ["message", "detail", "content", "error", "reason"].iter().find_map(|key| match map.get(*key) {
                    Some(Value::String(candidate)) if !py_strip(candidate).is_empty() => Some(candidate.clone()),
                    _ => None,
                });
            chosen.unwrap_or_else(|| py_json_dumps(value, false))
        }
        Value::Array(_) => py_json_dumps(value, false),
        other => python_str(other),
    };
    let joined = rendered.split(py_isspace).filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" ");
    chars_prefix(&joined, limit)
}

/// `bound_text(value, max_chars)` (`log_record_values.py`).
fn bound_text(text: &str, max_chars: usize) -> String {
    let limit = max_chars.max(128);
    let length = text.chars().count();
    if length <= limit {
        return text.to_owned();
    }
    let reserve = format!("\n… <log policy omitted {length} chars> …\n").chars().count();
    let available = (limit as i64 - reserve as i64).max(32) as usize;
    let head = (available as f64 * 0.62) as usize;
    let tail = available - head;
    let chars: Vec<char> = text.chars().collect();
    let head_text: String = chars[..head].iter().collect();
    let tail_text: String = chars[length - tail..].iter().collect();
    let omitted = length - head - tail;
    format!("{head_text}\n… <log policy omitted {omitted} chars> …\n{tail_text}")
}

fn safe_arg(value: &Value) -> Value {
    match value {
        Value::Null | Value::Bool(_) => value.clone(),
        Value::Number(_) => value.clone(),
        Value::String(text) => Value::String(chars_prefix(&bound_text(text, 160), 160)),
        Value::Array(items) => Value::Array(
            items.iter().take(8).filter(|item| !item.is_array() && !item.is_object()).map(safe_arg).collect(),
        ),
        Value::Object(_) => Value::String(chars_prefix(&bound_text(&activity_text(value, 160), 160), 160)),
    }
}

fn safe_args(value: &Value) -> Obj {
    let Value::Object(map) = value else { return Obj::new() };
    let mut result = Obj::new();
    for (key, item) in map.iter().take(12) {
        result.insert(chars_prefix(key, 80), safe_arg(item));
    }
    result
}

/// Python `int(value)` that answers `None` on TypeError/ValueError.
fn lenient_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(_) => None,
        Value::Number(number) => number.as_i64().or_else(|| number.as_f64().map(|float| float.trunc() as i64)),
        Value::String(text) => {
            let cleaned = py_strip(text);
            let digits = cleaned.trim_start_matches(['+', '-']);
            if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
                return None;
            }
            cleaned.replace('_', "").parse().ok()
        }
        _ => None,
    }
}

fn nonnegative_int(value: &Value, maximum: i64) -> Option<i64> {
    let parsed = lenient_int(value)?;
    if parsed >= 0 {
        Some(parsed.min(maximum))
    } else {
        None
    }
}

fn identity_int(value: &Value) -> Option<i64> {
    let parsed = match value {
        Value::Number(number) => number.as_i64()?,
        Value::String(text) => {
            if text.is_empty() || !text.chars().all(|c| c.is_ascii_digit()) || text.chars().count() > 10 {
                return None;
            }
            text.parse().ok()?
        }
        _ => return None,
    };
    if (0..=INTEGER_MAX).contains(&parsed) {
        Some(parsed)
    } else {
        None
    }
}

fn compaction_fact_text(value: &Value, limit: usize) -> String {
    let Value::String(text) = value else { return String::new() };
    static SECRET: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let pattern = SECRET.get_or_init(|| {
        Regex::new(concat!(
            r#"(?i)(?:authorization(?:\\?["'])?\s*:|bearer\s+|"#,
            r#"(?:api[_-]?(?:key|secret)|(?:access|refresh)[_-]?token|token|password|secret)"#,
            r#"(?:\\?["'])?\s*[:=]|https?://[^/\s:@]+:[^/\s@]+@)"#
        ))
        .expect("secret regex")
    });
    if pattern.is_match(text) {
        return "<redacted>".into();
    }
    activity_text(value, limit)
}

fn summary_calls(value: &Value) -> Vec<Value> {
    const ALLOWED: &[&str] = &[
        "model",
        "providerId",
        "status",
        "costUsd",
        "known",
        "reason",
        "costBasis",
        "pricingSource",
        "pricingSnapshot",
        "source",
        "currency",
        "provider",
        "selectedPromptTokens",
        "tierId",
        "maxPromptTokens",
        "usdToCny",
        "rates",
        "input",
        "output",
        "cacheReadMul",
        "cacheWriteMul",
        "cacheWrite1hMul",
        "cacheWrite1hTokens",
    ];
    fn clean(row: &Obj, depth: usize) -> Obj {
        let mut result = Obj::new();
        for (key, item) in row {
            if !ALLOWED.contains(&key.as_str()) {
                continue;
            }
            if matches!(key.as_str(), "model" | "providerId" | "provider") && !item.is_string() {
                continue;
            }
            match item {
                Value::Object(nested) if matches!(key.as_str(), "pricingSnapshot" | "rates") && depth < 2 => {
                    result.insert(key.clone(), Value::Object(clean(nested, depth + 1)));
                }
                Value::String(_) => {
                    result.insert(key.clone(), Value::String(compaction_fact_text(item, 160)));
                }
                Value::Null | Value::Bool(_) => {
                    result.insert(key.clone(), item.clone());
                }
                Value::Number(number) => {
                    let magnitude = number.as_f64().map(f64::abs).unwrap_or(f64::INFINITY);
                    let within = match number.as_i64() {
                        Some(integer) => integer.unsigned_abs() <= INTEGER_MAX as u64,
                        None => magnitude <= INTEGER_MAX as f64,
                    };
                    if within && magnitude.is_finite() {
                        result.insert(key.clone(), item.clone());
                    }
                }
                _ => {}
            }
        }
        result
    }
    let Value::Array(rows) = value else { return vec![] };
    rows.iter().take(16).filter_map(Value::as_object).map(|row| Value::Object(clean(row, 0))).collect()
}

fn is_failure_archive_bridge(value: &str) -> bool {
    ["summary-failed:", "summary-declined:", "summary-aborted:", "summary-cancelled:", "summary-rejected:", "failed:"]
        .iter()
        .any(|prefix| value.starts_with(prefix))
}

fn normalize_entry(raw: &Value) -> Option<Obj> {
    let Value::Object(raw) = raw else { return None };
    for key in ["taskId", "attemptId", "anchorBlockId"] {
        match raw.get(key) {
            None | Some(Value::Null) => {}
            Some(Value::String(value))
                if !py_strip(value).is_empty() && value.chars().count() <= string_limit(key)? => {}
            Some(_) => return None,
        }
    }
    if !get(raw, "anchorToolRoundNum").is_null() && identity_int(get(raw, "anchorToolRoundNum")).is_none() {
        return None;
    }
    match get(raw, "anchorPosition") {
        Value::Null => {}
        Value::String(position) if matches!(position.as_str(), "before" | "after" | "producer-start") => {}
        _ => return None,
    }
    let mut entry = Obj::new();
    for (key, value) in raw {
        if value.is_null() {
            continue;
        }
        if let Some(limit) = string_limit(key) {
            if key == "archiveId" && is_failure_archive_bridge(&activity_text(value, limit)) {
                continue;
            }
            let rendered = match key.as_str() {
                "taskId" | "attemptId" | "anchorBlockId" => text(value),
                "taskModel" | "summaryModel" | "summarySelectedModel" | "summaryProviderId" => {
                    compaction_fact_text(value, limit)
                }
                _ => activity_text(value, limit),
            };
            if !rendered.is_empty() {
                entry.insert(key.clone(), Value::String(rendered));
            }
        } else if INTEGER_FIELDS.contains(&key.as_str()) {
            let parsed = if key == "llmRound" || key == "anchorToolRoundNum" {
                identity_int(value)
            } else {
                nonnegative_int(value, if key == "reductionPercent" { 100 } else { INTEGER_MAX })
            };
            if let Some(parsed) = parsed {
                entry.insert(key.clone(), Value::from(parsed));
            }
        } else if key == "summaryCalls" {
            let calls = summary_calls(value);
            if !calls.is_empty() {
                entry.insert(key.clone(), Value::Array(calls));
            }
        } else if key == "summaryArgs" || key == "detailArgs" {
            let args = safe_args(value);
            if !args.is_empty() {
                entry.insert(key.clone(), Value::Object(args));
            }
        }
    }
    if !truthy(get(&entry, "id")) || !truthy(get(&entry, "spanId")) {
        return None;
    }
    if !matches!(get(&entry, "kind").as_str(), Some("model" | "tool" | "status" | "error" | "system")) {
        return None;
    }
    if !matches!(
        get(&entry, "status").as_str(),
        Some(
            "started"
                | "running"
                | "waiting"
                | "succeeded"
                | "failed"
                | "skipped"
                | "switched"
                | "aborted"
                | "superseded"
        )
    ) {
        return None;
    }
    if !matches!(get(&entry, "severity").as_str(), Some("info" | "warning" | "error")) {
        return None;
    }
    if !entry.contains_key("occurredAt") {
        return None;
    }
    let count = get(&entry, "count").as_i64().filter(|count| *count != 0).unwrap_or(1);
    entry.insert("count".into(), Value::from(count.clamp(1, COUNT_MAX)));
    Some(entry)
}

fn eviction_rank(entry: &Obj) -> u8 {
    if matches!(get(entry, "reasonCode").as_str(), Some("context_compaction" | "compacting")) {
        return 4;
    }
    if get(entry, "severity").as_str() == Some("error")
        || matches!(get(entry, "status").as_str(), Some("failed" | "switched"))
    {
        return 3;
    }
    if get(entry, "severity").as_str() != Some("info") {
        return 2;
    }
    if matches!(get(entry, "kind").as_str(), Some("status" | "model")) {
        0
    } else {
        1
    }
}

fn eviction_index(entries: &[Obj]) -> usize {
    let mut best: Option<(usize, u8)> = None;
    for (index, entry) in entries.iter().enumerate() {
        let rank = eviction_rank(entry);
        if rank == 0 {
            return index;
        }
        if best.is_none_or(|(_, best_rank)| rank < best_rank) {
            best = Some((index, rank));
        }
    }
    best.map(|(index, _)| index).unwrap_or(0)
}

fn timeline_document(entries: &[Obj], dropped: i64) -> Value {
    let mut document = Obj::new();
    document.insert("blockId".into(), json!("activity-timeline"));
    document.insert("version".into(), json!(1));
    document.insert("entries".into(), Value::Array(entries.iter().cloned().map(Value::Object).collect()));
    if dropped != 0 {
        document.insert("droppedCount".into(), Value::from(dropped));
    }
    Value::Object(document)
}

fn trim_entries(mut entries: Vec<Obj>, mut dropped: i64) -> (Vec<Obj>, i64) {
    while entries.len() > ACTIVITY_TIMELINE_MAX_ENTRIES {
        let index = eviction_index(&entries);
        entries.remove(index);
        dropped = (dropped + 1).min(COUNT_MAX);
    }
    if entries.is_empty() {
        return (entries, dropped);
    }
    let mut size = py_json_dumps(&timeline_document(&entries, dropped), false).len() as i64;
    while !entries.is_empty() && size > ACTIVITY_TIMELINE_MAX_JSON_BYTES as i64 {
        let index = eviction_index(&entries);
        let row_bytes = py_json_dumps(&Value::Object(entries[index].clone()), false).len() as i64;
        entries.remove(index);
        let previous = dropped;
        dropped = (dropped + 1).min(COUNT_MAX);
        size -= row_bytes + 1;
        if previous != 0 {
            size += dropped.to_string().len() as i64 - previous.to_string().len() as i64;
        } else if dropped != 0 {
            size += ",\"droppedCount\":".len() as i64 + dropped.to_string().len() as i64;
        }
    }
    (entries, dropped)
}

/// `normalize_activity_timeline(raw)`.
pub fn normalize_activity_timeline(raw: &Value) -> Option<Value> {
    let Value::Object(raw) = raw else { return None };
    let entries: Vec<Obj> = match raw.get("entries") {
        Some(Value::Array(items)) => items.iter().filter_map(normalize_entry).collect(),
        _ => vec![],
    };
    if entries.is_empty() {
        return None;
    }
    let dropped = nonnegative_int(get(raw, "droppedCount"), COUNT_MAX).unwrap_or(0);
    let (entries, dropped) = trim_entries(entries, dropped);
    Some(timeline_document(&entries, dropped))
}
