//! Provider wire projection: tool preflight, the three protocol emitters and
//! sealing (`lib/llm/_sse_core.py::prepare_request`, `lib/tools/gateway.py`
//! `sanitize_wire_tools`, `lib/llm/{openai,anthropic,responses}_outbound`).
//!
//! The sealed payload is the compact insertion-order JSON of the public
//! fields. Off-wire evidence that Python seals beside it (context lineage,
//! semantic/prefix identities, admitted token counts, wire fingerprints and
//! raw-archive capture preparation) is not computed here; the round writes
//! the raw archive itself.

use serde_json::{json, Map, Value};

use crate::canonical::{normalize_text_blocks, Result, Unsupported};
use crate::pyjson::{compact, dumps_sorted_ascii, dumps_spaced, public_fields, py_blank, truthy};

type Msg = Map<String, Value>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Protocol {
    OpenAi,
    Anthropic,
    Responses,
}

impl Protocol {
    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "openai" => Some(Self::OpenAi),
            "anthropic" => Some(Self::Anthropic),
            "responses" => Some(Self::Responses),
            _ => None,
        }
    }
}

/// A sealed provider request: endpoint, headers (without the per-request
/// trace id, which the transport adds) and the exact payload bytes.
#[derive(Debug, Clone)]
pub struct SealedRequest {
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub payload: Vec<u8>,
    pub tool_names: Vec<String>,
}

fn unsupported<T>(what: &str) -> Result<T> {
    Err(Unsupported(what.to_owned()))
}

// ── tool preflight ─────────────────────────────────────────────────────────

fn schema_allows_object(schema: &Msg) -> Result<bool> {
    match schema.get("type") {
        None => Ok(true),
        Some(Value::String(name)) => Ok(name == "object"),
        Some(Value::Array(names)) if !names.is_empty() => {
            Ok(names.iter().any(|name| name.as_str() == Some("object")))
        }
        _ => unsupported("invalid parameters.type"),
    }
}

/// `_project_moonshot_root_object` for the root shapes the prototype ports:
/// a plain object, a missing type, and one `anyOf`/`oneOf` whose branches
/// carry only `required`/`properties` (no `allOf`, no nested types).
fn project_root_object(schema: &Msg) -> Result<Option<Msg>> {
    if !schema_allows_object(schema)? {
        return unsupported("non-object tool arguments");
    }
    let combinators: Vec<&str> = ["anyOf", "oneOf", "allOf"]
        .into_iter()
        .filter(|key| schema.contains_key(*key))
        .collect();
    if combinators.is_empty() {
        if schema.get("type").and_then(Value::as_str) == Some("object") {
            return Ok(None);
        }
        let mut projected = Msg::new();
        projected.insert("type".to_owned(), json!("object"));
        for (key, value) in schema {
            if key != "type" {
                projected.insert(key.clone(), value.clone());
            }
        }
        return Ok(Some(projected));
    }
    if combinators.len() > 1 || combinators[0] == "allOf" {
        return unsupported("root allOf or multiple root combinators");
    }
    let combinator = combinators[0];
    let Some(Value::Array(branches)) = schema.get(combinator) else {
        return unsupported("empty root combinator");
    };
    let mut viable: Vec<&Msg> = Vec::new();
    for branch in branches {
        let Value::Object(branch) = branch else {
            return unsupported("non-object combinator branch");
        };
        if schema_allows_object(branch)? {
            viable.push(branch);
        }
    }
    if viable.is_empty() {
        return unsupported("no object-compatible branch");
    }
    let parent_properties = match schema.get("properties") {
        None => None,
        Some(Value::Object(map)) => Some(map),
        Some(_) => return unsupported("properties must be an object"),
    };
    let mut merged = parent_properties.cloned().unwrap_or_default();
    let mut candidates: Vec<(String, Vec<Value>, usize)> = Vec::new();
    for branch in &viable {
        if let Some(Value::Object(properties)) = branch.get("properties") {
            for (name, child) in properties {
                if merged.contains_key(name) {
                    continue;
                }
                match candidates.iter_mut().find(|(known, _, _)| known == name) {
                    Some((_, values, presence)) => {
                        values.push(child.clone());
                        *presence += 1;
                    }
                    None => candidates.push((name.clone(), vec![child.clone()], 1)),
                }
            }
        }
    }
    for (name, values, presence) in candidates {
        let mut unique: Vec<Value> = Vec::new();
        for value in values {
            if !unique.contains(&value) {
                unique.push(value);
            }
        }
        let value = if presence < viable.len() {
            json!({})
        } else if unique.len() > 1 {
            json!({"anyOf": unique})
        } else {
            unique.remove(0)
        };
        merged.insert(name, value);
    }
    let required_of = |map: &Msg| -> Vec<Value> {
        match map.get("required") {
            Some(Value::Array(names)) => names.clone(),
            _ => Vec::new(),
        }
    };
    let branch_required: Vec<Vec<Value>> = viable.iter().map(|b| required_of(b)).collect();
    let mut unconditional: Vec<Value> = branch_required[0].clone();
    for names in &branch_required[1..] {
        unconditional.retain(|name| names.contains(name));
    }
    let mut merged_required: Vec<Value> = Vec::new();
    for name in required_of(schema).into_iter().chain(
        branch_required
            .iter()
            .flatten()
            .filter(|n| unconditional.contains(n))
            .cloned(),
    ) {
        if !merged_required.contains(&name) {
            merged_required.push(name);
        }
    }
    let mut projected = Msg::new();
    projected.insert("type".to_owned(), json!("object"));
    for (key, value) in schema {
        if !matches!(key.as_str(), "type" | "properties" | "required") && key != combinator {
            projected.insert(key.clone(), value.clone());
        }
    }
    if !merged.is_empty() || parent_properties.is_some() {
        projected.insert("properties".to_owned(), Value::Object(merged));
    }
    if !merged_required.is_empty() {
        projected.insert("required".to_owned(), Value::Array(merged_required));
    }
    if !schema.contains_key("additionalProperties")
        && viable
            .iter()
            .all(|branch| branch.get("additionalProperties") == Some(&Value::Bool(false)))
    {
        projected.insert("additionalProperties".to_owned(), json!(false));
    }
    Ok(Some(projected))
}

fn tool_name(tool: &Value) -> Option<&str> {
    tool.get("function")
        .and_then(|function| function.get("name"))
        .or_else(|| tool.get("name"))
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
}

/// `sanitize_wire_tools` for an ordinary (non-Kimi) model.
pub fn preflight_tools(tools: &[Value]) -> Result<Vec<Value>> {
    let mut out = Vec::with_capacity(tools.len());
    for tool in tools {
        let Value::Object(source) = tool else {
            continue;
        };
        if tool_name(tool).is_none() {
            continue;
        }
        let mut keep = source.clone();
        match keep.get_mut("function") {
            Some(Value::Object(function)) => match function.get("parameters") {
                None => {
                    function.insert(
                        "parameters".to_owned(),
                        json!({"type": "object", "properties": {}}),
                    );
                }
                Some(Value::Bool(true)) => {
                    function.insert("parameters".to_owned(), json!({"type": "object"}));
                }
                Some(Value::Object(parameters)) => {
                    if let Some(projected) = project_root_object(parameters)? {
                        function.insert("parameters".to_owned(), Value::Object(projected));
                    }
                }
                Some(_) => return unsupported("tool parameters shape"),
            },
            _ => return unsupported("non-function tool"),
        }
        if keep.get("type").and_then(Value::as_str) != Some("function") {
            keep.insert("type".to_owned(), json!("function"));
        }
        if keep.contains_key("decoded_result_schema") {
            let declared = keep.shift_remove("decoded_result_schema");
            if let Some(Value::Object(declared)) = declared {
                let text = dumps_sorted_ascii(&Value::Object(declared));
                let function = keep["function"].as_object_mut().expect("function object");
                let description = function
                    .get("description")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned();
                function.insert(
                    "description".to_owned(),
                    Value::String(format!(
                        "{description}\nSuccessful decoded JSON value (producer declaration; coverage/freshness unknown): {text}"
                    )),
                );
            } else if declared.is_some_and(|value| !value.is_null()) {
                return unsupported("invalid decoded_result_schema");
            }
        }
        out.push(Value::Object(keep));
    }
    Ok(out)
}

// ── tool-call IDs ──────────────────────────────────────────────────────────

fn check_call_ids(messages: &[Value], protocol: Protocol) -> Result<()> {
    let accepts = |id: &str| match protocol {
        Protocol::OpenAi => id.chars().count() <= 40,
        Protocol::Responses => id.chars().count() <= 64,
        Protocol::Anthropic => id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-'),
    };
    for message in messages {
        let mut ids: Vec<&str> = Vec::new();
        if let Some(Value::Array(calls)) = message.get("tool_calls") {
            ids.extend(
                calls
                    .iter()
                    .filter_map(|call| call.get("id").and_then(Value::as_str)),
            );
        }
        if let Some(id) = message.get("tool_call_id").and_then(Value::as_str) {
            ids.push(id);
        }
        if ids.into_iter().any(|id| !id.is_empty() && !accepts(id)) {
            return unsupported("tool-call ID respelling");
        }
    }
    Ok(())
}

// ── Chat Completions ───────────────────────────────────────────────────────

fn all_plain_text(content: &[Value]) -> bool {
    !content.is_empty()
        && content.iter().all(|part| {
            part.as_object().is_some_and(|map| {
                map.get("type").and_then(Value::as_str) == Some("text")
                    && map.get("text").is_some_and(Value::is_string)
                    && map.keys().all(|key| key == "type" || key == "text")
            })
        })
}

fn to_chat(body: &Msg) -> Result<Msg> {
    let messages = body["messages"].as_array().expect("canonical messages");
    check_call_ids(messages, Protocol::OpenAi)?;
    let mut projected = public_fields(body);
    let wire: Vec<Value> = messages
        .iter()
        .map(|message| {
            let mut clean = public_fields(message.as_object().expect("canonical message"));
            if let Some(Value::Array(parts)) = clean.get("content") {
                if all_plain_text(parts) {
                    let joined: String = parts
                        .iter()
                        .map(|part| part["text"].as_str().unwrap_or(""))
                        .collect();
                    clean.insert("content".to_owned(), Value::String(joined));
                }
            }
            Value::Object(clean)
        })
        .collect();
    projected.insert("messages".to_owned(), Value::Array(wire));
    Ok(projected)
}

// ── Anthropic Messages ─────────────────────────────────────────────────────

fn anthropic_content_blocks(content: Option<&Value>) -> Result<Vec<Value>> {
    match content {
        Some(Value::String(text)) => Ok(if py_blank(text) {
            Vec::new()
        } else {
            vec![json!({"type": "text", "text": text})]
        }),
        Some(Value::Array(blocks)) => {
            let mut out = Vec::new();
            for block in normalize_text_blocks(blocks) {
                let Value::Object(block) = block else {
                    continue;
                };
                match block.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        let text = block.get("text").and_then(Value::as_str).unwrap_or("");
                        let mut converted = json!({"type": "text", "text": text});
                        if truthy(block.get("cache_control")) {
                            converted["cache_control"] = block["cache_control"].clone();
                        }
                        out.push(converted);
                    }
                    Some("image_url") => return unsupported("image blocks"),
                    _ => {}
                }
            }
            Ok(out)
        }
        _ => Ok(Vec::new()),
    }
}

fn has_cache_control(content: Option<&Value>) -> bool {
    matches!(content, Some(Value::Array(blocks)) if blocks.iter().any(|b| truthy(b.get("cache_control"))))
}

fn object_arguments(arguments: Option<&Value>) -> Result<Value> {
    match arguments {
        Some(Value::String(text)) => match serde_json::from_str::<Value>(text) {
            Ok(value @ Value::Object(_)) => Ok(value),
            _ => unsupported("non-object tool arguments"),
        },
        Some(value @ Value::Object(_)) => Ok(value.clone()),
        None => Ok(json!({})),
        _ => unsupported("tool arguments"),
    }
}

fn to_anthropic(body: &Msg) -> Result<Msg> {
    let messages = body["messages"].as_array().expect("canonical messages");
    check_call_ids(messages, Protocol::Anthropic)?;
    for key in [
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "effort",
        "reasoning_effort",
        "output_config",
        "stop",
    ] {
        if body.contains_key(key) {
            return unsupported(key);
        }
    }
    let mut out = Msg::new();
    out.insert(
        "model".to_owned(),
        body.get("model").cloned().unwrap_or(json!("")),
    );
    out.insert(
        "max_tokens".to_owned(),
        body.get("max_tokens").cloned().unwrap_or(json!(4096)),
    );
    if truthy(body.get("stream")) {
        out.insert("stream".to_owned(), json!(true));
    }
    for key in [
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "thinking",
    ] {
        if let Some(value) = body.get(key) {
            out.insert(key.to_owned(), value.clone());
        }
    }
    if let Some(Value::Array(tools)) = body.get("tools") {
        let mut converted = Vec::new();
        for tool in tools {
            let Some(function) = tool.get("function").filter(|f| truthy(Some(f))) else {
                continue;
            };
            let mut at = Msg::new();
            at.insert(
                "name".to_owned(),
                function.get("name").cloned().unwrap_or(json!("")),
            );
            at.insert(
                "description".to_owned(),
                function.get("description").cloned().unwrap_or(json!("")),
            );
            at.insert(
                "input_schema".to_owned(),
                function
                    .get("parameters")
                    .cloned()
                    .unwrap_or(json!({"type": "object", "properties": {}})),
            );
            if let Some(strict) = function.get("strict") {
                at.insert("strict".to_owned(), strict.clone());
            }
            if truthy(function.get("cache_control")) {
                at.insert(
                    "cache_control".to_owned(),
                    function["cache_control"].clone(),
                );
            }
            converted.push(Value::Object(at));
        }
        if !converted.is_empty() {
            out.insert("tools".to_owned(), Value::Array(converted));
        }
    }
    let mut system: Vec<Value> = Vec::new();
    // Each entry is a message, or a tool-result batch still being filled.
    let mut wire: Vec<(Value, bool)> = Vec::new();
    for message in messages {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        let content = message.get("content");
        match role {
            "system" | "developer" => {
                let blocks = match content {
                    Some(value) if truthy(Some(value)) => anthropic_content_blocks(Some(value))?,
                    _ => anthropic_content_blocks(Some(&json!("")))?,
                };
                system.extend(blocks);
            }
            "tool" => {
                if has_cache_control(content) {
                    return unsupported("cache markers on tool results");
                }
                let tool_content = match content {
                    Some(Value::String(text)) => json!(text),
                    other => {
                        let blocks = anthropic_content_blocks(other)?;
                        if blocks.is_empty() {
                            json!("")
                        } else {
                            Value::Array(blocks)
                        }
                    }
                };
                let block = json!({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id").cloned().unwrap_or(json!("")),
                    "content": tool_content,
                });
                match wire.last_mut() {
                    Some((Value::Array(batch), true)) => batch.push(block),
                    _ => wire.push((Value::Array(vec![block]), true)),
                }
            }
            "assistant" => {
                if has_cache_control(content) {
                    return unsupported("cache markers on assistant messages");
                }
                let text = match content {
                    Some(value) if truthy(Some(value)) => value.clone(),
                    _ => json!(""),
                };
                let mut blocks = anthropic_content_blocks(Some(&text))?;
                if let Some(Value::Array(calls)) = message.get("tool_calls") {
                    for call in calls {
                        let function = call.get("function").cloned().unwrap_or(json!({}));
                        blocks.push(json!({
                            "type": "tool_use",
                            "id": call.get("id").filter(|id| truthy(Some(id))).cloned().unwrap_or(json!("")),
                            "name": function.get("name").cloned().unwrap_or(json!("")),
                            "input": object_arguments(Some(function.get("arguments").unwrap_or(&json!("{}"))))?,
                        }));
                    }
                }
                if blocks.is_empty() {
                    blocks.push(json!({"type": "text", "text": "[empty response]"}));
                }
                wire.push((json!({"role": "assistant", "content": blocks}), false));
            }
            "user" => {
                let text = match content {
                    Some(value) if truthy(Some(value)) => value.clone(),
                    _ => json!(""),
                };
                let mut blocks = anthropic_content_blocks(Some(&text))?;
                if blocks.is_empty() {
                    blocks.push(json!({"type": "text", "text": "[empty message]"}));
                }
                wire.push((json!({"role": "user", "content": blocks}), false));
            }
            _ => {}
        }
    }
    if !system.is_empty() {
        if system.len() == 1 && !truthy(system[0].get("cache_control")) {
            out.insert(
                "system".to_owned(),
                system[0].get("text").cloned().unwrap_or(json!("")),
            );
        } else {
            out.insert("system".to_owned(), Value::Array(system));
        }
    }
    let messages: Vec<Value> = wire
        .into_iter()
        .map(|(entry, batch)| {
            if batch {
                json!({"role": "user", "content": entry})
            } else {
                entry
            }
        })
        .collect();
    out.insert("messages".to_owned(), Value::Array(messages));
    Ok(out)
}

// ── Responses ──────────────────────────────────────────────────────────────

fn to_responses(body: &Msg) -> Result<Msg> {
    let messages = body["messages"].as_array().expect("canonical messages");
    check_call_ids(messages, Protocol::Responses)?;
    for key in [
        "tool_choice",
        "response_format",
        "text",
        "reasoning",
        "output_config",
        "stop",
        "parallel_tool_calls",
    ] {
        if body.contains_key(key) {
            return unsupported(key);
        }
    }
    let mut out = Msg::new();
    out.insert(
        "model".to_owned(),
        body.get("model").cloned().unwrap_or(json!("")),
    );
    out.insert("store".to_owned(), json!(false));
    out.insert("stream".to_owned(), json!(true));
    out.insert("parallel_tool_calls".to_owned(), json!(true));
    for key in ["temperature", "top_p"] {
        if let Some(value) = body.get(key) {
            out.insert(key.to_owned(), value.clone());
        }
    }
    if let Some(value) = body.get("max_tokens") {
        out.insert("max_output_tokens".to_owned(), value.clone());
    }
    let mut input: Vec<Value> = Vec::new();
    for message in messages {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        let content = message.get("content");
        let mut calls = Vec::new();
        if role == "assistant" {
            if let Some(Value::Array(tool_calls)) = message.get("tool_calls") {
                for call in tool_calls {
                    if call
                        .get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("function")
                        != "function"
                    {
                        continue;
                    }
                    let function = call.get("function").cloned().unwrap_or(json!({}));
                    let name = function.get("name").and_then(Value::as_str).unwrap_or("");
                    if name.chars().count() > 64 {
                        return unsupported("tool-name aliasing");
                    }
                    let arguments = match function.get("arguments") {
                        None => json!("{}"),
                        Some(Value::String(text)) => json!(text),
                        Some(other) => json!(dumps_spaced(&object_arguments(Some(other))?)),
                    };
                    calls.push(json!({
                        "type": "function_call",
                        "call_id": call.get("id").cloned().unwrap_or(json!("")),
                        "name": name,
                        "arguments": arguments,
                    }));
                }
            }
        }
        if role == "tool" {
            if let Some(Value::Array(blocks)) = content {
                if blocks
                    .iter()
                    .any(|b| b.get("type").and_then(Value::as_str) == Some("image_url"))
                {
                    return unsupported("image tool results");
                }
            }
            let output = match content {
                Some(Value::String(text)) => json!(text),
                None | Some(Value::Null) => json!(dumps_spaced(&json!(""))),
                Some(other) => json!(dumps_spaced(other)),
            };
            input.push(json!({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id").filter(|id| truthy(Some(id))).cloned().unwrap_or(json!("")),
                "output": output,
            }));
            continue;
        }
        let api_role = if role == "system" { "developer" } else { role };
        let part_type = if role == "assistant" {
            "output_text"
        } else {
            "input_text"
        };
        let mut parts: Vec<Value> = Vec::new();
        match content {
            Some(Value::String(text)) if !text.is_empty() => {
                parts.push(json!({"type": part_type, "text": text}));
            }
            Some(Value::Array(blocks)) => {
                for block in blocks {
                    match block.get("type").and_then(Value::as_str) {
                        Some("text") => parts.push(json!({
                            "type": part_type,
                            "text": block.get("text").cloned().unwrap_or(json!("")),
                        })),
                        Some("image_url") if role == "user" => return unsupported("image input"),
                        _ => {}
                    }
                }
            }
            _ => {}
        }
        if role != "assistant" || !parts.is_empty() {
            input.push(json!({"type": "message", "role": api_role, "content": parts}));
        }
        input.extend(calls);
    }
    out.insert("input".to_owned(), Value::Array(input));
    if let Some(Value::Array(tools)) = body.get("tools").filter(|tools| truthy(Some(tools))) {
        let mut converted = Vec::new();
        for tool in tools {
            if !tool.is_object() {
                continue;
            }
            if tool.get("type").and_then(Value::as_str) != Some("function") {
                converted.push(tool.clone());
                continue;
            }
            let function = tool.get("function").cloned().unwrap_or(json!({}));
            let name = function.get("name").and_then(Value::as_str).unwrap_or("");
            if name.chars().count() > 64 {
                return unsupported("tool-name aliasing");
            }
            let mut converted_tool = Msg::new();
            converted_tool.insert("type".to_owned(), json!("function"));
            converted_tool.insert("name".to_owned(), json!(name));
            if truthy(function.get("description")) {
                converted_tool.insert("description".to_owned(), function["description"].clone());
            }
            if let Some(parameters) = function.get("parameters") {
                converted_tool.insert("parameters".to_owned(), parameters.clone());
            }
            converted_tool.insert(
                "strict".to_owned(),
                function.get("strict").cloned().unwrap_or(json!(false)),
            );
            converted.push(Value::Object(converted_tool));
        }
        out.insert("tools".to_owned(), Value::Array(converted));
    }
    Ok(out)
}

// ── prepare + seal ─────────────────────────────────────────────────────────

fn sent_tool_names(payload: &Msg) -> Vec<String> {
    match payload.get("tools") {
        Some(Value::Array(tools)) => tools
            .iter()
            .filter_map(tool_name)
            .map(str::to_owned)
            .collect(),
        _ => Vec::new(),
    }
}

/// `prepare_request` for an api-key route without OAuth or a gateway profile.
pub fn prepare(
    mut body: Msg,
    protocol: Protocol,
    base_url: &str,
    api_key: &str,
) -> Result<SealedRequest> {
    if let Some(Value::Array(tools)) = body.get("tools") {
        let tools = preflight_tools(tools)?;
        if tools.is_empty() {
            body.shift_remove("tools");
            body.shift_remove("tool_choice");
        } else {
            body.insert("tools".to_owned(), Value::Array(tools));
        }
    }
    let base = base_url.trim_end_matches('/');
    let (payload, url, headers) = match protocol {
        Protocol::OpenAi => (
            to_chat(&body)?,
            format!("{base}/chat/completions"),
            vec![
                ("Content-Type".to_owned(), "application/json".to_owned()),
                ("Authorization".to_owned(), format!("Bearer {api_key}")),
            ],
        ),
        Protocol::Anthropic => {
            let url = if base.ends_with("/messages") {
                base.to_owned()
            } else if base.ends_with("/v1") {
                format!("{base}/messages")
            } else {
                format!("{base}/v1/messages")
            };
            (
                to_anthropic(&body)?,
                url,
                vec![
                    ("Content-Type".to_owned(), "application/json".to_owned()),
                    ("anthropic-version".to_owned(), "2023-06-01".to_owned()),
                    ("x-api-key".to_owned(), api_key.to_owned()),
                    ("Authorization".to_owned(), format!("Bearer {api_key}")),
                ],
            )
        }
        Protocol::Responses => (
            to_responses(&body)?,
            format!("{base}/responses"),
            vec![
                ("Content-Type".to_owned(), "application/json".to_owned()),
                ("Authorization".to_owned(), format!("Bearer {api_key}")),
            ],
        ),
    };
    let payload = public_fields(&payload);
    let tool_names = sent_tool_names(&payload);
    Ok(SealedRequest {
        url,
        headers,
        payload: compact(&Value::Object(payload)),
        tool_names,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_one_of_relaxes_to_a_plain_object() {
        let schema = json!({"additionalProperties": false, "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
                            "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "type": "object"});
        let projected = project_root_object(schema.as_object().unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(
            compact(&Value::Object(projected)),
            br#"{"type":"object","additionalProperties":false,"properties":{"a":{"type":"string"},"b":{"type":"string"}}}"#
        );
    }

    #[test]
    fn decoded_result_schema_moves_into_the_description() {
        let tools = vec![
            json!({"type": "function", "decoded_result_schema": {"b": 1, "a": "\u{e9}"},
                                "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}),
        ];
        let out = preflight_tools(&tools).unwrap();
        assert_eq!(out[0].get("decoded_result_schema"), None);
        assert_eq!(
            out[0]["function"]["description"],
            "d\nSuccessful decoded JSON value (producer declaration; coverage/freshness unknown): {\"a\":\"\\u00e9\",\"b\":1}"
        );
    }

    #[test]
    fn tool_results_batch_into_one_anthropic_user_turn() {
        let body: Msg = serde_json::from_value(json!({
            "model": "m", "max_tokens": 5, "stream": true,
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a", "tool_calls": [
                    {"id": "x", "type": "function", "function": {"name": "t", "arguments": "{\"k\": 1}"}},
                    {"id": "y", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "x", "content": "1"},
                {"role": "tool", "tool_call_id": "y", "content": "2"}]})).unwrap();
        let sealed = prepare(body, Protocol::Anthropic, "http://h/v1", "k").unwrap();
        let wire: Value = serde_json::from_slice(&sealed.payload).unwrap();
        assert_eq!(sealed.url, "http://h/v1/messages");
        assert_eq!(wire["system"], "s");
        assert_eq!(wire["messages"].as_array().unwrap().len(), 3);
        assert_eq!(wire["messages"][2]["content"].as_array().unwrap().len(), 2);
        assert_eq!(wire["messages"][1]["content"][1]["input"], json!({"k": 1}));
    }
}
