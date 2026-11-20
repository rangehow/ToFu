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

use crate::canonical::{normalize_text_blocks, Error, Result, Unsupported};
use crate::pyjson::{compact, dumps_spaced, public_fields, py_blank, py_lstrip, truthy};
use crate::reasoning::{native_reasoning_effort, resolve_wire_effort};
use crate::route::{
    add_cache_breakpoints, gateway_trait, honors_cache_markers, is_claude, merge_affinity_headers,
    route_feature, RouteSpec,
};

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
    Err(Unsupported(what.to_owned()).into())
}

// ── tool preflight ─────────────────────────────────────────────────────────

fn tool_name(tool: &Value) -> Option<&str> {
    tool.get("function")
        .and_then(|function| function.get("name"))
        .or_else(|| tool.get("name"))
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
}

/// `sanitize_wire_tools`: drops every tool Python isolates, projects the
/// rest (`crate::schema`).
pub fn preflight_tools(tools: &[Value], model: &str) -> Result<Vec<Value>> {
    Ok(crate::schema::sanitize_wire_tools(tools, model)?.0)
}

// ── tool-call IDs ──────────────────────────────────────────────────────────

fn conversion_error<T>(code: &str) -> Result<T> {
    Err(Error::raised(
        "ProtocolConversionError",
        format!("protocol_conversion:{code}"),
    ))
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

fn to_chat(body: &Msg, route: &RouteSpec<'_>) -> Result<Msg> {
    let messages = body["messages"].as_array().expect("canonical messages");
    let model = body.get("model").and_then(Value::as_str).unwrap_or("");
    let preserve_text_parts = honors_cache_markers(model, route.inference_profile, "openai");
    let mut projected = public_fields(body);
    let wire: Vec<Value> = messages
        .iter()
        .map(|message| {
            let mut clean = public_fields(message.as_object().expect("canonical message"));
            if let Some(Value::Array(parts)) = clean.get("content") {
                if !preserve_text_parts && all_plain_text(parts) {
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

/// `_block_cache_control`: the marker a message-level breakpoint left on its
/// content, hoisted onto the emitted tool_result / last assistant block.
fn block_cache_control(content: Option<&Value>) -> Option<Value> {
    match content {
        Some(Value::Array(blocks)) => blocks
            .iter()
            .find(|block| block.is_object() && truthy(block.get("cache_control")))
            .map(|block| block["cache_control"].clone()),
        _ => None,
    }
}

fn strip_cache_control(blocks: &mut [Value]) {
    for block in blocks {
        if let Value::Object(block) = block {
            block.shift_remove("cache_control");
        }
    }
}

/// `apply_gateway_anthropic_shape`: the declared identity leads the system.
fn apply_gateway_anthropic_shape(out: &mut Msg, route: &RouteSpec<'_>) {
    let Some(declared) = gateway_trait(
        route.gateway_profiles,
        "anthropic_system_identity",
        route.base_url,
    ) else {
        return;
    };
    let identity = declared["text"].as_str().unwrap_or("").to_owned();
    let accepted = declared["accepted_prefix"]
        .as_str()
        .unwrap_or("")
        .to_lowercase();
    let leads = |text: &str| py_lstrip(text).to_lowercase().starts_with(&accepted);
    match out.get("system") {
        Some(Value::String(system)) => {
            if !leads(system) {
                let shaped = if py_blank(system) {
                    identity
                } else {
                    format!("{identity}\n\n{system}")
                };
                out.insert("system".to_owned(), Value::String(shaped));
            }
        }
        Some(Value::Array(system)) if !system.is_empty() => {
            let blocks: Vec<Value> = system.iter().filter(|b| b.is_object()).cloned().collect();
            let first_text = blocks
                .iter()
                .find(|b| b.get("type").and_then(Value::as_str) == Some("text"))
                .and_then(|b| b.get("text").and_then(Value::as_str))
                .unwrap_or("")
                .to_owned();
            if !leads(&first_text) {
                let mut shaped = vec![json!({"type": "text", "text": identity})];
                shaped.extend(blocks);
                out.insert("system".to_owned(), Value::Array(shaped));
            }
        }
        _ => {
            out.insert("system".to_owned(), Value::String(identity));
        }
    }
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
    for key in [
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
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
    // Messages carries both effort spellings as `output_config.effort`.
    if let Some(effort) = body.get("effort").or_else(|| body.get("reasoning_effort")) {
        out.insert("output_config".to_owned(), json!({"effort": effort}));
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
                let marker = block_cache_control(content);
                let tool_content = match content {
                    Some(Value::String(text)) => json!(text),
                    other => {
                        let mut blocks = anthropic_content_blocks(other)?;
                        if blocks.is_empty() {
                            json!("")
                        } else {
                            if marker.is_some() {
                                strip_cache_control(&mut blocks);
                            }
                            Value::Array(blocks)
                        }
                    }
                };
                let mut block = json!({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id").cloned().unwrap_or(json!("")),
                    "content": tool_content,
                });
                if let Some(marker) = marker {
                    block["cache_control"] = marker;
                }
                match wire.last_mut() {
                    Some((Value::Array(batch), true)) => batch.push(block),
                    _ => wire.push((Value::Array(vec![block]), true)),
                }
            }
            "assistant" => {
                let marker = block_cache_control(content);
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
                if let Some(marker) = marker.filter(|_| !blocks.is_empty()) {
                    strip_cache_control(&mut blocks);
                    let last = blocks.last_mut().expect("non-empty");
                    last["cache_control"] = marker;
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

/// The native effort `openai_body_to_responses` sends: a selected route must
/// represent it exactly on the `reasoning_effort` wire.
fn responses_effort(body: &Msg, route: &RouteSpec<'_>) -> Result<Option<String>> {
    let effort = native_reasoning_effort(body)?;
    let effort = effort.as_ref().and_then(Value::as_str).map(str::to_owned);
    if let (Some(selected), Some(value)) = (route.inference_profile, effort.as_deref()) {
        let empty = json!({});
        let declared = selected
            .get("reasoning")
            .filter(|r| r.is_object())
            .unwrap_or(&empty);
        let disabled = declared
            .get("disabled_effort")
            .and_then(Value::as_str)
            .unwrap_or("none");
        let mode = if value == "none" || value == disabled {
            "off"
        } else {
            "exact"
        };
        match resolve_wire_effort(declared, mode, Some(value)) {
            Ok((fmt, resolved)) => {
                if fmt != "reasoning_effort" || resolved.as_deref() != Some(value) {
                    return conversion_error("reasoning_wire_unrepresentable");
                }
            }
            Err(Error::Raised { class, message }) if class == "ReasoningContractError" => {
                let kind = message
                    .split_once(": ")
                    .map_or(message.as_str(), |(k, _)| k);
                return conversion_error(kind);
            }
            Err(other) => return Err(other),
        }
    }
    Ok(effort)
}

fn to_responses(body: &Msg, route: &RouteSpec<'_>) -> Result<Msg> {
    let messages = body["messages"].as_array().expect("canonical messages");
    let model = body.get("model").and_then(Value::as_str).unwrap_or("");
    // Without a selected route, the GPT-5.6 family opens stateful legacy
    // gates (prompt-cache key, breakpoints, compaction) this port lacks.
    if route.inference_profile.is_none() && model.to_lowercase().contains("gpt-5.6") {
        return unsupported("GPT-5.6 legacy Responses gates");
    }
    let replay = route_feature(
        route.inference_profile,
        "responses_reasoning_replay",
        "responses",
    )
    .unwrap_or(false);
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
    let effort = responses_effort(body, route)?;
    // A selected route declares no `responses_reasoning_summary` binding the
    // port accepts, and without one the generic face keeps omission.
    let mut reasoning = Msg::new();
    if let Some(effort) = effort.filter(|effort| !effort.is_empty()) {
        reasoning.insert("effort".to_owned(), json!(effort));
    }
    if replay {
        reasoning.insert("context".to_owned(), json!("all_turns"));
    }
    if !reasoning.is_empty() || replay {
        out.insert("reasoning".to_owned(), Value::Object(reasoning));
    }
    if replay {
        out.insert("include".to_owned(), json!(["reasoning.encrypted_content"]));
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

impl Protocol {
    pub fn name(self) -> &'static str {
        match self {
            Self::OpenAi => "openai",
            Self::Anthropic => "anthropic",
            Self::Responses => "responses",
        }
    }
}

/// `prepare_request` for an api-key route without OAuth: the final tool
/// projection (tool-name budget, preflight), cache breakpoints, protocol
/// translation, declared gateway shape and headers, then sealing.
pub fn prepare(mut body: Msg, protocol: Protocol, route: &RouteSpec<'_>) -> Result<SealedRequest> {
    crate::route::check_profile(route.inference_profile)?;
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    crate::tool_wire::project_tool_wire(&mut body, protocol.name(), route)?;
    let contract = crate::call_ids::contract(protocol.name(), &model);
    if let Some(Value::Array(messages)) = body.get_mut("messages") {
        crate::call_ids::respell(messages, &contract);
    }
    if let Some(Value::Array(tools)) = body.get("tools") {
        let tools = preflight_tools(tools, &model)?;
        if tools.is_empty() {
            body.shift_remove("tools");
            body.shift_remove("tool_choice");
        } else {
            body.insert("tools".to_owned(), Value::Array(tools));
        }
    }
    add_cache_breakpoints(&mut body, route, protocol.name());
    let mut extra: Vec<(String, String)> = Vec::new();
    if is_claude(&model) && route.extended_cache_ttl {
        extra.push((
            "anthropic-beta".to_owned(),
            "extended-cache-ttl-2025-04-11".to_owned(),
        ));
    }
    let api_key = route.api_key;
    let base = route.base_url.trim_end_matches('/');
    let (payload, url, mut headers) = match protocol {
        Protocol::OpenAi => {
            let mut headers = vec![
                ("Content-Type".to_owned(), "application/json".to_owned()),
                ("Authorization".to_owned(), format!("Bearer {api_key}")),
            ];
            headers.extend(extra);
            (
                to_chat(&body, route)?,
                format!("{base}/chat/completions"),
                headers,
            )
        }
        Protocol::Anthropic => {
            let url = if base.ends_with("/messages") {
                base.to_owned()
            } else if base.ends_with("/v1") {
                format!("{base}/messages")
            } else {
                format!("{base}/v1/messages")
            };
            let mut payload = to_anthropic(&body)?;
            apply_gateway_anthropic_shape(&mut payload, route);
            let mut headers = vec![
                ("Content-Type".to_owned(), "application/json".to_owned()),
                ("anthropic-version".to_owned(), "2023-06-01".to_owned()),
            ];
            if !api_key.is_empty() {
                headers.push(("x-api-key".to_owned(), api_key.to_owned()));
                headers.push(("Authorization".to_owned(), format!("Bearer {api_key}")));
            }
            headers.extend(extra);
            if let Some(Value::String(header)) = gateway_trait(
                route.gateway_profiles,
                "working_directory_header",
                route.base_url,
            ) {
                let dir = if route.working_dir.is_empty() {
                    "/"
                } else {
                    route.working_dir
                };
                headers.push((header.clone(), dir.to_owned()));
            }
            (payload, url, headers)
        }
        Protocol::Responses => {
            let mut headers = vec![
                ("Content-Type".to_owned(), "application/json".to_owned()),
                ("Authorization".to_owned(), format!("Bearer {api_key}")),
            ];
            headers.extend(extra);
            (
                to_responses(&body, route)?,
                format!("{base}/responses"),
                headers,
            )
        }
    };
    merge_affinity_headers(
        &mut headers,
        route.inference_profile,
        protocol.name(),
        route.lineage,
    );
    let payload = public_fields(&payload);
    let tool_names = sent_tool_names(&payload);
    Ok(SealedRequest {
        url,
        headers,
        payload: compact(&Value::Object(payload)),
        tool_names,
    })
}

/// One `byte-identity` request: `{protocol, model, max_tokens, temperature,
/// tools, messages, base_url, api_key}` plus the optional route facts
/// `inference_profile`, `gateway_profiles`, `extended_cache_ttl`, `lineage`
/// and `working_dir`. `body_extra` (an object) is merged into the canonical
/// body after `build_body`, the way Python callers attach private fields
/// (`_conv_id`, `_executable_tool_catalog`, ...).
pub fn prepare_request_json(request: &Value) -> Result<SealedRequest> {
    // OAuth changes provider headers and payload shaping; it is not a route
    // fact this prototype can safely discard.
    if truthy(request.get("oauth")) {
        return unsupported("OAuth request shaping");
    }
    let protocol = Protocol::parse(request["protocol"].as_str().unwrap_or(""))
        .ok_or_else(|| Unsupported("unknown protocol".to_owned()))?;
    let messages = request["messages"].as_array().cloned().unwrap_or_default();
    let tools = request["tools"].as_array().cloned();
    let profile = request.get("inference_profile").filter(|p| !p.is_null());
    let profiles = request["gateway_profiles"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let body = crate::canonical::build_body(
        &messages,
        &crate::canonical::BodySpec {
            model: request["model"].as_str().unwrap_or(""),
            max_tokens: request["max_tokens"].as_u64().unwrap_or(4096),
            temperature: request["temperature"].as_f64(),
            tools: tools.as_deref(),
            inference_profile: profile,
            reasoning: request.get("reasoning").filter(|r| !r.is_null()),
        },
    )?;
    let mut body = body;
    if let Some(Value::Object(extra)) = request.get("body_extra") {
        for (key, value) in extra {
            body.insert(key.clone(), value.clone());
        }
    }
    let route = RouteSpec {
        base_url: request["base_url"].as_str().unwrap_or(""),
        api_key: request["api_key"].as_str().unwrap_or(""),
        gateway_profiles: &profiles,
        inference_profile: profile,
        extended_cache_ttl: request["extended_cache_ttl"].as_bool().unwrap_or(false),
        lineage: request["lineage"].as_str().unwrap_or(""),
        working_dir: request["working_dir"].as_str().unwrap_or("/"),
    };
    prepare(body, protocol, &route)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decoded_result_schema_moves_into_the_description() {
        let tools = vec![
            json!({"type": "function", "decoded_result_schema": {"b": 1, "a": "\u{e9}"},
                                "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}),
        ];
        let out = preflight_tools(&tools, "m").unwrap();
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
        let sealed = prepare(
            body,
            Protocol::Anthropic,
            &RouteSpec::plain("http://h/v1", "k"),
        )
        .unwrap();
        let wire: Value = serde_json::from_slice(&sealed.payload).unwrap();
        assert_eq!(sealed.url, "http://h/v1/messages");
        assert_eq!(wire["system"], "s");
        assert_eq!(wire["messages"].as_array().unwrap().len(), 3);
        assert_eq!(wire["messages"][2]["content"].as_array().unwrap().len(), 2);
        assert_eq!(wire["messages"][1]["content"][1]["input"], json!({"k": 1}));
    }
}
