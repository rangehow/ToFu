//! `lib/llm/anthropic_outbound/_sse.py` `AnthropicSSETranslator`: Anthropic
//! Messages events → OpenAI chat chunks.

use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};

use super::errors::{unsupported, Flow};
use super::py::{self, truthy, IntError};
use super::usage::convert_anthropic_usage;
use super::Translated;
use crate::pyjson::dumps_spaced;

const MAX_CONTENT_BLOCK_INDEX: i64 = 4095;
const MAX_TOOL_USE_ID_CHARS: usize = 512;
const MAX_TOOL_NAME_CHARS: usize = 512;

#[derive(Default)]
pub(crate) struct AnthropicTranslator {
    block_types: HashMap<i64, String>,
    block_start_identity: HashMap<i64, Value>,
    block_stopped: HashSet<i64>,
    initial_complete_input: HashSet<i64>,
    input_json: Vec<(i64, String)>,
    usage_raw: Map<String, Value>,
    pub model_refusal: Option<Value>,
}

fn protocol_error(message: &str) -> Vec<Translated> {
    vec![Translated::Chunk(json!({"error": {
        "message": format!("invalid Anthropic stream: {message}"),
        "type": "server_error",
        "http_code": "500",
    }}))]
}

fn block_index(event: &Map<String, Value>) -> Option<i64> {
    event
        .get("index")
        .and_then(Value::as_i64)
        .filter(|index| (0..=MAX_CONTENT_BLOCK_INDEX).contains(index))
}

fn delta_chunk(delta: Value) -> Vec<Translated> {
    vec![Translated::Chunk(json!({"choices": [{"delta": delta}]}))]
}

fn map_stop_reason(stop: &str) -> &str {
    match stop {
        "end_turn" | "stop_sequence" | "pause_turn" => "stop",
        "max_tokens" => "length",
        "tool_use" => "tool_calls",
        "refusal" => "content_filter",
        other => other,
    }
}

fn model_refusal_details(details: Option<&Value>) -> Value {
    let details = details.and_then(Value::as_object);
    let field = |name: &str, limit: usize, strip: bool| -> Value {
        match details.and_then(|d| d.get(name)).and_then(Value::as_str) {
            Some(text) => {
                let text = if strip { py::strip(text) } else { text };
                if text.is_empty() {
                    Value::Null
                } else {
                    json!(py::prefix(text, limit))
                }
            }
            None => Value::Null,
        }
    };
    json!({"category": field("category", 64, false), "explanation": field("explanation", 500, true)})
}

/// `json.loads(raw)` of accumulated tool input: `Some(is_object)` or `None`.
fn parse_tool_input(raw: &str) -> Flow<Option<bool>> {
    Ok(py::loads(raw)?.map(|value| value.is_object()))
}

impl AnthropicTranslator {
    fn input_json(&self, index: i64) -> &str {
        self.input_json
            .iter()
            .find(|(i, _)| *i == index)
            .map_or("", |(_, raw)| raw.as_str())
    }

    fn set_input_json(&mut self, index: i64, raw: String) {
        match self.input_json.iter_mut().find(|(i, _)| *i == index) {
            Some(slot) => slot.1 = raw,
            None => self.input_json.push((index, raw)),
        }
    }

    pub(crate) fn translate(&mut self, event: &Map<String, Value>) -> Flow<Vec<Translated>> {
        let etype = match event.get("type") {
            None => "",
            Some(Value::String(text)) => text.as_str(),
            Some(_) => return Ok(protocol_error("event.type must be text")),
        };
        match etype {
            "message_start" => {
                let Some(message) = event.get("message").and_then(Value::as_object) else {
                    return Ok(protocol_error("message_start.message must be an object"));
                };
                let usage = match message.get("usage") {
                    None | Some(Value::Null) => Map::new(),
                    Some(Value::Object(usage)) => usage.clone(),
                    Some(_) => {
                        return Ok(protocol_error(
                            "message_start usage must be an object or null",
                        ))
                    }
                };
                if usage.is_empty() {
                    return Ok(Vec::new());
                }
                self.usage_raw = usage;
                let converted = convert_anthropic_usage(&self.usage_raw)?;
                Ok(vec![Translated::Chunk(
                    json!({"choices": [{"delta": {}}], "usage": converted}),
                )])
            }
            "content_block_start" => self.block_start(event),
            "content_block_delta" => self.block_delta(event),
            "content_block_stop" => {
                let Some(index) = block_index(event) else {
                    return Ok(protocol_error(
                        "content_block_stop index must be a bounded integer",
                    ));
                };
                let Some(block_type) = self.block_types.get(&index) else {
                    return Ok(protocol_error(
                        "content_block_stop arrived before its block start",
                    ));
                };
                if self.block_stopped.contains(&index) {
                    return Ok(Vec::new());
                }
                if block_type == "tool_use" || block_type == "server_tool_use" {
                    let raw = self.input_json(index);
                    if !raw.is_empty() {
                        match parse_tool_input(raw)? {
                            None => {
                                return Ok(protocol_error(
                                    "tool input JSON was incomplete at block stop",
                                ))
                            }
                            Some(false) => {
                                return Ok(protocol_error(
                                    "tool input JSON must decode to an object",
                                ))
                            }
                            Some(true) => {}
                        }
                    }
                }
                self.block_stopped.insert(index);
                Ok(Vec::new())
            }
            "message_delta" => self.message_delta(event),
            "message_stop" => {
                for (index, raw) in &self.input_json {
                    if raw.is_empty() {
                        continue;
                    }
                    match parse_tool_input(raw)? {
                        None => {
                            return Ok(protocol_error(&format!(
                                "tool input JSON for block {index} was incomplete at message stop"
                            )))
                        }
                        Some(false) => {
                            return Ok(protocol_error(&format!(
                                "tool input JSON for block {index} must decode to an object"
                            )))
                        }
                        Some(true) => {}
                    }
                }
                Ok(vec![Translated::Done])
            }
            "error" => {
                let error = match event.get("error") {
                    Some(Value::Object(error)) => {
                        let mut error = error.clone();
                        if !error.get("message").is_some_and(Value::is_string) {
                            error.insert("message".into(), json!("anthropic stream error"));
                        }
                        if error.contains_key("type") && !error["type"].is_string() {
                            error.insert("type".into(), json!("server_error"));
                        }
                        error
                    }
                    _ => {
                        let mut error = Map::new();
                        error.insert("message".into(), json!("anthropic stream error"));
                        error
                    }
                };
                Ok(vec![Translated::Chunk(json!({ "error": error }))])
            }
            _ => Ok(Vec::new()),
        }
    }

    fn block_start(&mut self, event: &Map<String, Value>) -> Flow<Vec<Translated>> {
        let Some(index) = block_index(event) else {
            return Ok(protocol_error(
                "content_block_start index must be a bounded integer",
            ));
        };
        let Some(block) = event.get("content_block").and_then(Value::as_object) else {
            return Ok(protocol_error(
                "content_block_start content_block must be an object",
            ));
        };
        let btype = match block.get("type").and_then(Value::as_str) {
            Some(btype) if !btype.is_empty() => btype,
            _ => {
                return Ok(protocol_error(
                    "content_block_start block type must be non-empty text",
                ))
            }
        };
        if self.block_types.contains_key(&index) && !self.block_start_identity.contains_key(&index)
        {
            return Ok(protocol_error(
                "content_block_start arrived after a standalone delta",
            ));
        }
        let mut normalized = block.clone();
        let mut initial_delta = Map::new();
        let mut initial_input_json = String::new();
        let text_or_null = |name: &str| -> Option<Option<String>> {
            match block.get(name) {
                None | Some(Value::Null) => Some(Some(String::new())),
                Some(Value::String(text)) => Some(Some(text.clone())),
                Some(_) => None,
            }
        };
        match btype {
            "text" => {
                let Some(Some(text)) = text_or_null("text") else {
                    return Ok(protocol_error(
                        "text block initial text must be text or null",
                    ));
                };
                normalized.insert("text".into(), json!(text));
                if !text.is_empty() {
                    initial_delta.insert("content".into(), json!(text));
                }
            }
            "thinking" => {
                let Some(Some(thinking)) = text_or_null("thinking") else {
                    return Ok(protocol_error(
                        "thinking block initial thinking must be text or null",
                    ));
                };
                let Some(Some(signature)) = text_or_null("signature") else {
                    return Ok(protocol_error(
                        "thinking block initial signature must be text or null",
                    ));
                };
                normalized.insert("thinking".into(), json!(thinking));
                if block.contains_key("signature") {
                    normalized.insert("signature".into(), json!(signature));
                }
                if !thinking.is_empty() {
                    initial_delta.insert("reasoning_content".into(), json!(thinking));
                }
                if !signature.is_empty() {
                    initial_delta.insert("thinking_signature".into(), json!(signature));
                }
            }
            "tool_use" | "server_tool_use" => {
                let input = match block.get("input") {
                    None | Some(Value::Null) => Map::new(),
                    Some(Value::Object(input)) => input.clone(),
                    Some(_) => {
                        return Ok(protocol_error(&format!(
                            "{btype} initial input must be an object or null"
                        )))
                    }
                };
                if !input.is_empty() {
                    py::require_integral_numbers(&Value::Object(input.clone()))?;
                    initial_input_json = dumps_spaced(&Value::Object(input.clone()));
                }
                normalized.insert("input".into(), Value::Object(input));
            }
            "compaction"
                if !matches!(
                    block.get("content"),
                    None | Some(Value::Null | Value::String(_))
                ) =>
            {
                return Ok(protocol_error(
                    "compaction initial content must be text or null",
                ));
            }
            _ => {}
        }
        let identity = Value::Object(normalized);
        if let Some(previous) = self.block_start_identity.get(&index) {
            if *previous != identity {
                return Ok(protocol_error(
                    "content block index was reused with a different identity",
                ));
            }
            return Ok(Vec::new());
        }
        self.block_start_identity.insert(index, identity);
        self.block_types.insert(index, btype.to_owned());
        if matches!(btype, "tool_use" | "server_tool_use") && !initial_input_json.is_empty() {
            self.set_input_json(index, initial_input_json.clone());
            self.initial_complete_input.insert(index);
        }
        if btype == "tool_use" {
            let name = match block.get("name") {
                None => "",
                Some(Value::String(name)) => name.as_str(),
                Some(_) => return Ok(protocol_error("tool_use name must be text")),
            };
            if name.is_empty() || py::char_len(name) > MAX_TOOL_NAME_CHARS {
                return Ok(protocol_error(
                    "tool_use name must be bounded non-empty text",
                ));
            }
            let id = match block.get("id") {
                None => "",
                Some(Value::String(id)) => id.as_str(),
                Some(_) => return Ok(protocol_error("tool_use id must be text")),
            };
            if id.is_empty() || py::char_len(id) > MAX_TOOL_USE_ID_CHARS {
                return Ok(protocol_error("tool_use id must be bounded non-empty text"));
            }
            return Ok(delta_chunk(json!({"tool_calls": [{
                "index": index, "id": id, "type": "function",
                "function": {"name": name, "arguments": initial_input_json},
            }]})));
        }
        if initial_delta.is_empty() {
            return Ok(Vec::new());
        }
        Ok(delta_chunk(Value::Object(initial_delta)))
    }

    fn block_delta(&mut self, event: &Map<String, Value>) -> Flow<Vec<Translated>> {
        let Some(index) = block_index(event) else {
            return Ok(protocol_error(
                "content_block_delta index must be a bounded integer",
            ));
        };
        if self.block_stopped.contains(&index) {
            return Ok(protocol_error(
                "content_block_delta arrived after its block stop",
            ));
        }
        let Some(delta) = event.get("delta").and_then(Value::as_object) else {
            return Ok(protocol_error(
                "content_block_delta delta must be an object",
            ));
        };
        let dtype = match delta.get("type").and_then(Value::as_str) {
            Some(dtype) if !dtype.is_empty() => dtype,
            _ => {
                return Ok(protocol_error(
                    "content_block_delta type must be non-empty text",
                ))
            }
        };
        if let Entry::Vacant(vacant) = self.block_types.entry(index) {
            let inferred = match dtype {
                "text_delta" => "text",
                "thinking_delta" | "signature_delta" => "thinking",
                _ => {
                    return Ok(protocol_error(
                        "content_block_delta arrived before its block start",
                    ))
                }
            };
            vacant.insert(inferred.to_owned());
        }
        let field = match dtype {
            "text_delta" => Some("text"),
            "thinking_delta" => Some("thinking"),
            "signature_delta" => Some("signature"),
            "compaction_delta" => Some("content"),
            "input_json_delta" => Some("partial_json"),
            _ => None,
        };
        let mut value = String::new();
        if let Some(field) = field {
            match delta.get(field) {
                None | Some(Value::Null) => {}
                Some(Value::String(text)) => value = text.clone(),
                Some(_) => {
                    return Ok(protocol_error(&format!(
                        "{dtype}.{field} must be text or null"
                    )))
                }
            }
        }
        let block_type = self.block_types.get(&index).cloned().unwrap_or_default();
        let expected: Option<&[&str]> = match dtype {
            "text_delta" => Some(&["text"]),
            "thinking_delta" | "signature_delta" => Some(&["thinking"]),
            "compaction_delta" => Some(&["compaction"]),
            "input_json_delta" => Some(&["tool_use", "server_tool_use"]),
            _ => None,
        };
        if let Some(expected) = expected {
            if !expected.contains(&block_type.as_str()) {
                return Ok(protocol_error(&format!(
                    "{dtype} is invalid for '{block_type}' block {index}"
                )));
            }
        }
        if dtype == "input_json_delta" && self.initial_complete_input.contains(&index) {
            return Ok(protocol_error(
                "input_json_delta followed an already-complete initial input",
            ));
        }
        match dtype {
            "text_delta" => Ok(delta_chunk(json!({ "content": value }))),
            "thinking_delta" => Ok(delta_chunk(json!({ "reasoning_content": value }))),
            "signature_delta" => Ok(delta_chunk(json!({ "thinking_signature": value }))),
            "input_json_delta" => {
                let raw = format!("{}{value}", self.input_json(index));
                self.set_input_json(index, raw);
                if block_type != "tool_use" {
                    return Ok(Vec::new());
                }
                Ok(delta_chunk(json!({"tool_calls": [{
                    "index": index, "function": {"arguments": value},
                }]})))
            }
            _ => Ok(Vec::new()),
        }
    }

    fn message_delta(&mut self, event: &Map<String, Value>) -> Flow<Vec<Translated>> {
        let Some(delta) = event.get("delta").and_then(Value::as_object) else {
            return Ok(protocol_error("message_delta delta must be an object"));
        };
        let mut choice = Map::new();
        choice.insert("delta".into(), json!({}));
        match delta.get("stop_reason") {
            None | Some(Value::Null) => {}
            Some(Value::String(stop)) => {
                if !stop.is_empty() {
                    choice.insert("finish_reason".into(), json!(map_stop_reason(stop)));
                    if stop == "refusal" {
                        self.model_refusal = Some(model_refusal_details(delta.get("stop_details")));
                    }
                }
            }
            Some(_) => {
                return Ok(protocol_error(
                    "message_delta stop_reason must be text or null",
                ))
            }
        }
        let mut chunk = Map::new();
        chunk.insert("choices".into(), json!([choice]));
        match event.get("usage") {
            None | Some(Value::Null) => {}
            Some(Value::Object(raw)) => {
                if !raw.is_empty() {
                    for (key, value) in raw {
                        if value.is_null() {
                            continue;
                        }
                        if key == "output_tokens" {
                            self.usage_raw.insert(key.clone(), value.clone());
                            continue;
                        }
                        let previous = self.usage_raw.get(key).filter(|v| truthy(Some(v)));
                        let previous = match previous {
                            None => Ok(0),
                            Some(previous) => py::int(previous)?,
                        };
                        let merged = match (previous, py::int(value)?) {
                            (Err(IntError::Overflow), _) | (Ok(_), Err(IntError::Overflow)) => {
                                return unsupported("int() overflow in Anthropic usage merge")
                            }
                            (Ok(previous), Ok(current)) => json!(previous.max(current)),
                            _ => value.clone(),
                        };
                        self.usage_raw.insert(key.clone(), merged);
                    }
                    chunk.insert(
                        "usage".into(),
                        Value::Object(convert_anthropic_usage(&self.usage_raw)?),
                    );
                }
            }
            Some(_) => {
                return Ok(protocol_error(
                    "message_delta usage must be an object or null",
                ))
            }
        }
        Ok(vec![Translated::Chunk(Value::Object(chunk))])
    }
}
