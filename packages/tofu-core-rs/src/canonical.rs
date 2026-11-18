//! Canonical request body from stored history: the `build_body` subset the
//! prototype ports (`lib/llm/body/_build.py`, `lib/llm_sanitize/`).
//!
//! Ported stages, in Python's order: API-field strip with same-role seam
//! hints, blank text-block normalization, tool-call wire-shape healing,
//! occurrence-local orphan repair, ghost-assistant drop, same-role merge,
//! empty user/tool placeholders and canonical key order. Stages the prototype
//! does not port refuse with [`Unsupported`] instead of guessing: gateway
//! blocked-term sanitizing, image projection/validation, Claude/Gemini
//! reasoning replay and tool-call ID respelling.

use std::collections::{HashMap, HashSet, VecDeque};

use serde_json::{json, Map, Value};

use crate::pyjson::{dumps_spaced, dumps_spaced_sorted, py_blank, py_prefix, truthy};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unsupported(pub String);

impl std::fmt::Display for Unsupported {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "unsupported by the prototype: {}", self.0)
    }
}

impl std::error::Error for Unsupported {}

pub type Result<T> = std::result::Result<T, Unsupported>;

fn unsupported<T>(what: &str) -> Result<T> {
    Err(Unsupported(what.to_owned()))
}

const API_MESSAGE_FIELDS: [&str; 9] = [
    "role",
    "content",
    "name",
    "tool_calls",
    "tool_call_id",
    "reasoning_content",
    "thinking_signature",
    "reasoning_details",
    "cache_control",
];
const SEAM_SOURCE_FIELDS: [&str; 5] = [
    "_contextComposer",
    "_isMeta",
    "_isVuDirective",
    "_isObjectiveAnchor",
    "_replyDropped",
];
pub const SEAM_HINT: &str = "_tofuSameRoleSeam";
const SIDECARS: [&str; 3] = [
    "_responses_items",
    "_anthropic_content_blocks",
    "_reasoning_continuity",
];
const ASSISTANT_REPLAY_FIELDS: [&str; 6] = [
    "reasoning_content",
    "thinking_signature",
    "reasoning_details",
    "_responses_items",
    "_anthropic_content_blocks",
    "_reasoning_continuity",
];

/// Inputs of one canonical body (the `build_body` arguments a round uses).
pub struct BodySpec<'a> {
    pub model: &'a str,
    pub max_tokens: u64,
    pub temperature: Option<f64>,
    pub tools: Option<&'a [Value]>,
}

type Msg = Map<String, Value>;

fn content_has_images(content: Option<&Value>) -> bool {
    matches!(content, Some(Value::Array(blocks)) if blocks.iter().any(|block| {
        block.get("type").and_then(Value::as_str).is_some_and(|kind| kind == "image_url" || kind == "image")
    }))
}

fn strip_non_api_fields(messages: &[Value]) -> Result<Vec<Msg>> {
    let mut cleaned = Vec::with_capacity(messages.len());
    for message in messages {
        let Some(source) = message.as_object() else {
            continue;
        };
        if content_has_images(source.get("content")) {
            return unsupported("image blocks (vision validation/projection)");
        }
        if SIDECARS.iter().any(|key| truthy(source.get(*key)))
            || truthy(source.get("reasoning_content"))
            || truthy(source.get("reasoning_details"))
            || truthy(source.get("thinking_signature"))
        {
            return unsupported("reasoning or native replay sidecars");
        }
        let seam = SEAM_SOURCE_FIELDS
            .iter()
            .any(|key| truthy(source.get(*key)));
        let mut clean: Msg = source
            .iter()
            .filter(|(key, _)| API_MESSAGE_FIELDS.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        if seam {
            clean.insert(SEAM_HINT.to_owned(), Value::Bool(true));
        }
        cleaned.push(clean);
    }
    Ok(cleaned)
}

fn is_blank_text_block(block: &Value) -> bool {
    block.get("type").and_then(Value::as_str) == Some("text")
        && match block.get("text") {
            None | Some(Value::Null) => true,
            Some(Value::String(text)) => py_blank(text),
            Some(other) => !truthy(Some(other)),
        }
}

fn is_plain_text_block(block: &Value) -> bool {
    match block.as_object() {
        Some(map) => {
            map.len() == 2
                && map.get("type").and_then(Value::as_str) == Some("text")
                && map.get("text").is_some_and(Value::is_string)
        }
        None => false,
    }
}

/// `lib.llm_sanitize.text_blocks.normalize_text_blocks` (no positions).
pub fn normalize_text_blocks(blocks: &[Value]) -> Vec<Value> {
    let mut out: Vec<Value> = Vec::with_capacity(blocks.len());
    let mut pending: Vec<String> = Vec::new();
    let mut previous_plain = false;
    let fold_left = |out: &mut Vec<Value>, pending: &mut Vec<String>, previous_plain: bool| {
        if !pending.is_empty() && previous_plain {
            if let Some(Value::Object(last)) = out.last_mut() {
                let text = last.get("text").and_then(Value::as_str).unwrap_or("");
                let joined = format!("{text}{}", pending.concat());
                last.insert("text".to_owned(), Value::String(joined));
            }
        }
        pending.clear();
    };
    for block in blocks {
        if !is_plain_text_block(block) {
            fold_left(&mut out, &mut pending, previous_plain);
            previous_plain = false;
            if !is_blank_text_block(block) {
                out.push(block.clone());
            }
            continue;
        }
        let text = block["text"].as_str().unwrap_or("");
        if py_blank(text) {
            if !text.is_empty() {
                pending.push(text.to_owned());
            }
            continue;
        }
        let mut block = block.clone();
        if previous_plain {
            fold_left(&mut out, &mut pending, previous_plain);
        } else if !pending.is_empty() {
            let prefix = pending.concat();
            block["text"] = Value::String(format!("{prefix}{text}"));
            pending.clear();
        }
        out.push(block);
        previous_plain = true;
    }
    fold_left(&mut out, &mut pending, previous_plain);
    out
}

fn blank_tool_call_content(content: Option<&Value>) -> bool {
    match content {
        None | Some(Value::Null) => true,
        Some(Value::String(text)) => py_blank(text),
        Some(Value::Array(items)) => items.is_empty(),
        _ => false,
    }
}

fn strip_empty_text_blocks(messages: &mut [Msg]) {
    for message in messages.iter_mut() {
        if truthy(message.get("tool_calls"))
            && message.contains_key("content")
            && blank_tool_call_content(message.get("content"))
        {
            message.shift_remove("content");
            continue;
        }
        let Some(Value::Array(content)) = message.get("content") else {
            continue;
        };
        if content.is_empty() {
            continue;
        }
        let kept = normalize_text_blocks(content);
        if kept.len() == content.len() {
            continue;
        }
        if !kept.is_empty() {
            message.insert("content".to_owned(), Value::Array(kept));
        } else if truthy(message.get("tool_calls")) {
            message.shift_remove("content");
        } else {
            message.insert("content".to_owned(), Value::String(String::new()));
        }
    }
}

fn valid_tool_name(name: &str) -> bool {
    !name.is_empty()
        && name.chars().count() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

fn mint_tool_call_id(msg_index: usize, call_index: usize, call: &Msg) -> String {
    let without_id: Map<String, Value> = call
        .iter()
        .filter(|(key, _)| key.as_str() != "id")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    let payload = dumps_spaced_sorted(&Value::Object(without_id));
    let digest = sha1_smol::Sha1::from(format!("{msg_index}:{call_index}:{payload}"))
        .digest()
        .to_string();
    format!("call_{}", &digest[..12])
}

fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => crate::pyjson::compact_string(other),
    }
}

/// `_fix_tool_call_wire_shape`.
fn fix_tool_call_wire_shape(messages: Vec<Msg>) -> Result<Vec<Msg>> {
    let mut out = Vec::with_capacity(messages.len());
    let mut unclaimed: Vec<(String, String)> = Vec::new();
    let mut claimed: HashSet<String> = HashSet::new();
    for (index, mut message) in messages.into_iter().enumerate() {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        if role == "assistant" && message.contains_key("tool_calls") {
            let calls = match message.get("tool_calls") {
                Some(Value::Array(calls)) if !calls.is_empty() => calls.clone(),
                _ => {
                    message.shift_remove("tool_calls");
                    unclaimed.clear();
                    out.push(message);
                    continue;
                }
            };
            let mut kept = Vec::new();
            let mut kept_claims = Vec::new();
            for (call_index, call) in calls.into_iter().enumerate() {
                let Value::Object(mut call) = call else {
                    continue;
                };
                if let Some(id) = call.get("id") {
                    if !id.is_null() && !id.is_string() {
                        let text = py_str(id);
                        call.insert("id".to_owned(), Value::String(text));
                    }
                }
                let original = match call.get("id") {
                    Some(value) if truthy(Some(value)) => py_str(value),
                    _ => String::new(),
                };
                if original.is_empty() || claimed.contains(&original) {
                    let base = mint_tool_call_id(index, call_index, &call);
                    let mut candidate = base.clone();
                    let mut occurrence = 1;
                    while claimed.contains(&candidate) {
                        occurrence += 1;
                        candidate = format!("{base}_{occurrence}");
                    }
                    call.insert("id".to_owned(), Value::String(candidate));
                }
                let effective = py_str(&call["id"]);
                claimed.insert(effective.clone());
                if call.get("type").and_then(Value::as_str) != Some("function") {
                    call.insert("type".to_owned(), json!("function"));
                }
                if !call.get("function").is_some_and(Value::is_object) {
                    call.insert("function".to_owned(), json!({}));
                }
                let function = call["function"].as_object_mut().expect("function object");
                match function.get("name") {
                    Some(Value::String(name)) if !py_blank(name) => {
                        if !valid_tool_name(name) {
                            let fixed: String = name
                                .chars()
                                .map(|c| {
                                    if c.is_ascii_alphanumeric() || c == '_' || c == '-' {
                                        c
                                    } else {
                                        '_'
                                    }
                                })
                                .collect();
                            let fixed = py_prefix(&fixed, 64).to_owned();
                            function.insert("name".to_owned(), Value::String(fixed));
                        }
                    }
                    _ => {
                        function.insert("name".to_owned(), json!("unnamed_tool_call"));
                    }
                }
                match function.get("arguments") {
                    Some(Value::String(_)) => {}
                    Some(value @ (Value::Object(_) | Value::Array(_))) => {
                        let text = dumps_spaced(value);
                        function.insert("arguments".to_owned(), Value::String(text));
                    }
                    _ => {
                        function.insert("arguments".to_owned(), json!("{}"));
                    }
                }
                kept.push(Value::Object(call));
                kept_claims.push((original, effective));
            }
            if kept.is_empty() {
                message.shift_remove("tool_calls");
                unclaimed.clear();
            } else {
                message.insert("tool_calls".to_owned(), Value::Array(kept));
                unclaimed = kept_claims;
            }
            out.push(message);
            continue;
        }
        if role == "tool" {
            let mut tool_call_id = match message.get("tool_call_id") {
                None | Some(Value::Null) => None,
                Some(value) => Some(py_str(value)),
            };
            if let Some(id) = &tool_call_id {
                if !message["tool_call_id"].is_string() {
                    message.insert("tool_call_id".to_owned(), Value::String(id.clone()));
                }
            }
            if tool_call_id.as_deref().is_none_or(str::is_empty) {
                if unclaimed.is_empty() {
                    continue;
                }
                let (_, effective) = unclaimed.remove(0);
                message.insert("tool_call_id".to_owned(), Value::String(effective));
            } else {
                let id = tool_call_id.take().expect("checked above");
                let Some(position) = unclaimed
                    .iter()
                    .position(|(original, effective)| *original == id || *effective == id)
                else {
                    continue;
                };
                let (_, effective) = unclaimed.remove(position);
                if id != effective {
                    message.insert("tool_call_id".to_owned(), Value::String(effective));
                }
            }
            out.push(message);
            continue;
        }
        unclaimed.clear();
        out.push(message);
    }
    Ok(out)
}

fn survives_tool_call_strip(message: &Msg) -> bool {
    truthy(message.get("content"))
        || [
            "function_call",
            "reasoning_content",
            "reasoning_details",
            "thinking_signature",
            "_responses_items",
            "_anthropic_content_blocks",
        ]
        .iter()
        .any(|key| truthy(message.get(*key)))
}

fn str_field(value: Option<&Value>) -> String {
    match value {
        Some(value) if truthy(Some(value)) => py_str(value).trim().to_owned(),
        _ => String::new(),
    }
}

/// `_repair_adjacent_tool_runs`.
fn fix_orphaned_tool_calls(messages: Vec<Msg>) -> Vec<Msg> {
    let mut repaired = Vec::with_capacity(messages.len());
    let mut position = 0;
    while position < messages.len() {
        let message = &messages[position];
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        if role == "tool" {
            position += 1;
            continue;
        }
        let calls = match message.get("tool_calls") {
            Some(Value::Array(calls)) if role == "assistant" && !calls.is_empty() => calls,
            Some(value) if role == "assistant" && truthy(Some(value)) => {
                let stripped = crate::pyjson::without(message, "tool_calls");
                if survives_tool_call_strip(&stripped) {
                    repaired.push(stripped);
                }
                position += 1;
                continue;
            }
            _ => {
                repaired.push(message.clone());
                position += 1;
                continue;
            }
        };
        let mut end = position + 1;
        while end < messages.len()
            && messages[end].get("role").and_then(Value::as_str) == Some("tool")
        {
            end += 1;
        }
        let mut positions_by_id: HashMap<String, VecDeque<usize>> = HashMap::new();
        for (call_position, call) in calls.iter().enumerate() {
            let id = str_field(call.get("id"));
            if !id.is_empty() {
                positions_by_id
                    .entry(id)
                    .or_default()
                    .push_back(call_position);
            }
        }
        let mut matched_calls = HashSet::new();
        let mut matched_results = Vec::new();
        for result in &messages[position + 1..end] {
            let id = str_field(result.get("tool_call_id"));
            if let Some(queue) = positions_by_id.get_mut(&id).filter(|_| !id.is_empty()) {
                if let Some(call_position) = queue.pop_front() {
                    matched_calls.insert(call_position);
                    matched_results.push(result.clone());
                }
            }
        }
        let kept: Vec<Value> = calls
            .iter()
            .enumerate()
            .filter(|(call_position, _)| matched_calls.contains(call_position))
            .map(|(_, call)| call.clone())
            .collect();
        if !kept.is_empty() {
            if kept.len() == calls.len() {
                repaired.push(message.clone());
            } else {
                let mut copy = message.clone();
                copy.insert("tool_calls".to_owned(), Value::Array(kept));
                repaired.push(copy);
            }
            repaired.extend(matched_results);
        } else {
            let stripped = crate::pyjson::without(message, "tool_calls");
            if survives_tool_call_strip(&stripped) {
                repaired.push(stripped);
            }
        }
        position = end;
    }
    repaired
}

fn drop_empty_assistant_messages(messages: Vec<Msg>) -> Vec<Msg> {
    let ghost = |message: &Msg| {
        if message.get("role").and_then(Value::as_str) != Some("assistant") {
            return false;
        }
        if [
            "tool_calls",
            "function_call",
            "reasoning_content",
            "reasoning_details",
            "thinking_signature",
            "_responses_items",
            "_anthropic_content_blocks",
        ]
        .iter()
        .any(|key| truthy(message.get(*key)))
        {
            return false;
        }
        match message.get("content") {
            None | Some(Value::Null) => true,
            Some(Value::String(text)) => py_blank(text),
            Some(Value::Array(blocks)) => blocks.iter().all(|block| {
                block.get("type").and_then(Value::as_str) == Some("text")
                    && !block
                        .get("text")
                        .and_then(Value::as_str)
                        .is_some_and(|text| !py_blank(text))
            }),
            _ => false,
        }
    };
    messages
        .into_iter()
        .filter(|message| !ghost(message))
        .collect()
}

fn without_hint(message: &Msg) -> Msg {
    if message.contains_key(SEAM_HINT) {
        crate::pyjson::without(message, SEAM_HINT)
    } else {
        message.clone()
    }
}

fn content_or_empty(message: &Msg) -> Value {
    match message.get("content") {
        Some(value) if truthy(Some(value)) => value.clone(),
        _ => Value::String(String::new()),
    }
}

fn as_blocks(content: Value) -> Vec<Value> {
    match content {
        Value::Array(blocks) => blocks,
        Value::String(text) if text.is_empty() => Vec::new(),
        Value::String(text) => vec![json!({"type": "text", "text": text})],
        other => vec![other],
    }
}

/// `_merge_consecutive_same_role` (merge observability is not ported).
fn merge_consecutive_same_role(messages: Vec<Msg>) -> Vec<Msg> {
    let mut merged: Vec<Msg> = Vec::with_capacity(messages.len());
    for message in messages {
        let Some(previous) = merged.last_mut() else {
            merged.push(without_hint(&message));
            continue;
        };
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        let previous_role = previous.get("role").and_then(Value::as_str).unwrap_or("");
        let has_replay = |m: &Msg| ASSISTANT_REPLAY_FIELDS.iter().any(|k| m.contains_key(*k));
        let mergeable = role == previous_role
            && (role == "user" || role == "assistant")
            && message.get("name") == previous.get("name")
            && !truthy(message.get("tool_calls"))
            && !truthy(previous.get("tool_calls"))
            && !(role == "assistant" && (has_replay(&message) || has_replay(previous)));
        if !mergeable {
            merged.push(without_hint(&message));
            continue;
        }
        let left = content_or_empty(previous);
        let right = content_or_empty(&message);
        let combined = if left.is_array() || right.is_array() {
            let mut blocks = as_blocks(left);
            blocks.extend(as_blocks(right));
            Value::Array(blocks)
        } else {
            let left = left.as_str().unwrap_or("").to_owned();
            let right = right.as_str().unwrap_or("");
            let separator = if !left.is_empty() && !right.is_empty() {
                "\n\n"
            } else {
                ""
            };
            Value::String(format!("{left}{separator}{right}"))
        };
        previous.insert("content".to_owned(), combined);
    }
    merged
}

fn fix_empty_user_messages(messages: &mut [Msg]) {
    for message in messages.iter_mut() {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        if role != "user" && role != "tool" {
            continue;
        }
        let empty = match message.get("content") {
            None | Some(Value::Null) => true,
            Some(Value::String(text)) => py_blank(text),
            Some(Value::Array(blocks)) => {
                blocks.is_empty()
                    || blocks.iter().all(|block| {
                        block.is_object()
                            && block.get("type").and_then(Value::as_str) == Some("text")
                            && !block
                                .get("text")
                                .and_then(Value::as_str)
                                .is_some_and(|text| !py_blank(text))
                    })
            }
            _ => false,
        };
        if empty {
            let placeholder = if role == "tool" {
                "[empty tool result]"
            } else {
                "[empty message]"
            };
            message.insert("content".to_owned(), json!(placeholder));
        }
    }
}

fn reorder(map: &Msg, order: &[&str]) -> Msg {
    let mut out = Msg::new();
    for key in order {
        if let Some(value) = map.get(*key) {
            out.insert((*key).to_owned(), value.clone());
        }
    }
    let mut rest: Vec<&String> = map.keys().filter(|key| !out.contains_key(*key)).collect();
    rest.sort();
    for key in rest {
        out.insert(key.clone(), map[key].clone());
    }
    out
}

const MESSAGE_ORDER: [&str; 9] = [
    "role",
    "content",
    "reasoning_content",
    "thinking_signature",
    "reasoning_details",
    "tool_calls",
    "tool_call_id",
    "name",
    "extra_content",
];

fn canonicalize(message: &Msg) -> Msg {
    let mut out = reorder(message, &MESSAGE_ORDER);
    if let Some(Value::Array(calls)) = message.get("tool_calls") {
        let calls = calls
            .iter()
            .map(|call| match call {
                Value::Object(call) => {
                    let mut call = reorder(call, &["id", "type", "function", "extra_content"]);
                    if let Some(Value::Object(function)) = call.get("function") {
                        let function = reorder(function, &["name", "arguments"]);
                        call.insert("function".to_owned(), Value::Object(function));
                    }
                    Value::Object(call)
                }
                other => other.clone(),
            })
            .collect();
        out.insert("tool_calls".to_owned(), Value::Array(calls));
    }
    out
}

fn model_family_unported(model: &str) -> Option<&'static str> {
    let lower = model.to_ascii_lowercase();
    if lower.contains("claude") {
        Some("Claude reasoning replay and cache markers")
    } else if lower.contains("gemini") {
        Some("Gemini thought signatures")
    } else if lower.contains("kimi") || lower.contains("moonshot") {
        Some("Kimi schema dialect")
    } else if lower.contains("mistral") || lower.contains("codestral") {
        Some("Mistral tool-call IDs")
    } else {
        None
    }
}

/// The canonical OpenAI-shaped body `build_body` returns for `messages`.
pub fn build_body(messages: &[Value], spec: &BodySpec<'_>) -> Result<Msg> {
    if messages.is_empty() {
        return unsupported("empty message list");
    }
    if let Some(family) = model_family_unported(spec.model) {
        return unsupported(family);
    }
    let mut clean = strip_non_api_fields(messages)?;
    strip_empty_text_blocks(&mut clean);
    let clean = fix_tool_call_wire_shape(clean)?;
    let clean = fix_orphaned_tool_calls(clean);
    let clean = drop_empty_assistant_messages(clean);
    let mut clean = merge_consecutive_same_role(clean);
    fix_empty_user_messages(&mut clean);
    let messages: Vec<Value> = clean
        .iter()
        .map(|message| Value::Object(canonicalize(message)))
        .collect();
    let mut body = Msg::new();
    body.insert("model".to_owned(), json!(spec.model));
    body.insert("messages".to_owned(), Value::Array(messages));
    body.insert("max_tokens".to_owned(), json!(spec.max_tokens));
    body.insert("stream".to_owned(), json!(true));
    if let Some(tools) = spec.tools.filter(|tools| !tools.is_empty()) {
        body.insert("tools".to_owned(), Value::Array(tools.to_vec()));
    }
    if let Some(temperature) = spec.temperature {
        body.insert("temperature".to_owned(), json!(temperature));
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> BodySpec<'static> {
        BodySpec {
            model: "m",
            max_tokens: 10,
            temperature: Some(1.0),
            tools: None,
        }
    }

    #[test]
    fn consecutive_users_merge_and_hints_never_reach_the_body() {
        let messages = vec![
            json!({"role": "user", "content": "a"}),
            json!({"role": "user", "content": "b", "_isMeta": true}),
        ];
        let body = build_body(&messages, &spec()).unwrap();
        assert_eq!(
            body["messages"],
            json!([{"role": "user", "content": "a\n\nb"}])
        );
    }

    #[test]
    fn orphaned_results_and_unanswered_calls_are_removed() {
        let messages = vec![
            json!({"role": "user", "content": "go"}),
            json!({"role": "assistant", "content": "x", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}),
            json!({"role": "tool", "tool_call_id": "a", "content": "ok"}),
            json!({"role": "tool", "tool_call_id": "zzz", "content": "lost"}),
        ];
        let body = build_body(&messages, &spec()).unwrap();
        let wire = &body["messages"];
        assert_eq!(wire.as_array().unwrap().len(), 3);
        assert_eq!(wire[1]["tool_calls"].as_array().unwrap().len(), 1);
        assert_eq!(wire[2]["tool_call_id"], "a");
    }

    #[test]
    fn blank_text_folds_into_its_plain_neighbour() {
        let blocks = vec![
            json!({"type": "text", "text": "a"}),
            json!({"type": "text", "text": "  "}),
            json!({"type": "text", "text": "b"}),
        ];
        assert_eq!(
            normalize_text_blocks(&blocks),
            vec![
                json!({"type": "text", "text": "a  "}),
                json!({"type": "text", "text": "b"})
            ]
        );
    }

    #[test]
    fn unported_inputs_refuse() {
        let messages = vec![
            json!({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}),
        ];
        assert!(build_body(&messages, &spec()).is_err());
    }
}
