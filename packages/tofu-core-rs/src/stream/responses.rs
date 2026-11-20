//! `lib/llm/responses_outbound/_sse.py` `ResponsesSSETranslator`: Responses
//! API events → OpenAI chat chunks, including readable reasoning
//! (`_reasoning.py`), tool callers, multi-agent attribution and the
//! `program_output` follow-up marker. The opaque replay sidecar
//! (`response_items`, encrypted reasoning) is not projected.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};

use super::errors::{invalid_encrypted_content_object, Flow};
use super::py::{self, truthy};
use super::reasoning::ReasoningReconciler;
use super::usage::convert_responses_usage;
use super::Translated;

const MAX_OUTPUT_INDEX: i64 = 4095;
const MAX_ITEM_ID_CHARS: usize = 512;
const MAX_TOOL_NAME_CHARS: usize = 512;

const REASONING_EVENTS: &[&str] = &[
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning_summary_text.done",
    "response.reasoning_text.done",
    "response.reasoning_summary_part.added",
    "response.reasoning_summary_part.done",
];

/// A terminal response and its object output items.
type TerminalOutput<'a> = (&'a Map<String, Value>, Vec<&'a Map<String, Value>>);

#[derive(Clone, PartialEq, Eq, Hash)]
enum Position {
    OutputIndex(i64),
    ItemId(String),
}

#[derive(Clone)]
struct Identity {
    call_id: String,
    name: String,
    caller_present: bool,
    caller: Value,
    agent: Value,
    initial_arguments: String,
}

impl PartialEq for Identity {
    fn eq(&self, other: &Self) -> bool {
        self.call_id == other.call_id
            && self.name == other.name
            && self.caller_present == other.caller_present
            && py::eq(&self.caller, &other.caller)
            && py::eq(&self.agent, &other.agent)
            && self.initial_arguments == other.initial_arguments
    }
}

#[derive(Default)]
pub(crate) struct ResponsesTranslator {
    tc_count: i64,
    item_slot: HashMap<String, i64>,
    ambiguous_item_ids: HashSet<String>,
    output_index_slot: HashMap<i64, i64>,
    slot_arguments: HashMap<i64, String>,
    slot_identity: HashMap<i64, Identity>,
    function_position_slot: HashMap<Position, i64>,
    function_position_identity: HashMap<Position, Identity>,
    hidden_text_output_indices: HashSet<i64>,
    emitted_text: String,
    reasoning: ReasoningReconciler,
}

fn protocol_error(message: &str) -> Vec<Translated> {
    vec![Translated::Chunk(json!({"error": {
        "message": format!("invalid Responses stream: {message}"),
        "type": "server_error",
        "http_code": "500",
    }}))]
}

use py::repr as py_repr;

/// A non-empty text `agent_name` of an agent object.
fn agent_name(agent: &Map<String, Value>) -> Option<&str> {
    agent
        .get("agent_name")
        .and_then(Value::as_str)
        .filter(|name| !name.is_empty())
}

/// `_multi_agent_message_is_user_visible`.
fn message_is_user_visible(item: &Map<String, Value>) -> bool {
    match item.get("agent") {
        None | Some(Value::Null) => true,
        Some(Value::Object(agent)) => {
            agent_name(agent) == Some("/root")
                && item.get("phase").and_then(Value::as_str) == Some("final_answer")
        }
        Some(_) => false,
    }
}

/// `_program_needs_followup`.
fn program_needs_followup(output: &[&Map<String, Value>]) -> bool {
    let has = |kind: &str| {
        output
            .iter()
            .any(|item| item.get("type").and_then(Value::as_str) == Some(kind))
    };
    has("program_output") && !has("message")
}

/// `output_index` validity: `Ok(None)` absent/null, `Err` invalid.
fn bounded_index(value: Option<&Value>) -> Result<Option<i64>, ()> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_i64()
            .filter(|index| (0..=MAX_OUTPUT_INDEX).contains(index))
            .map(Some)
            .ok_or(()),
    }
}

fn text_or_null(value: Option<&Value>) -> Option<String> {
    match value {
        None | Some(Value::Null) => Some(String::new()),
        Some(Value::String(text)) => Some(text.clone()),
        Some(_) => None,
    }
}

impl ResponsesTranslator {
    fn chunk(
        &mut self,
        delta: Value,
        finish: Option<&str>,
        usage: Option<Map<String, Value>>,
    ) -> Translated {
        if let Some(text) = delta.get("content").and_then(Value::as_str) {
            self.emitted_text.push_str(text);
        }
        let mut chunk = Map::new();
        chunk.insert(
            "choices".into(),
            json!([{"index": 0, "delta": delta, "finish_reason": finish}]),
        );
        if let Some(usage) = usage.filter(|u| !u.is_empty()) {
            chunk.insert("usage".into(), Value::Object(usage));
        }
        Translated::Chunk(Value::Object(chunk))
    }

    fn reasoning_chunks(&mut self, texts: Result<Vec<String>, String>) -> Vec<Translated> {
        match texts {
            Ok(texts) => texts
                .into_iter()
                .map(|text| self.chunk(json!({ "reasoning_content": text }), None, None))
                .collect(),
            Err(error) => protocol_error(&error),
        }
    }

    /// `_slot_for`: `Ok(Ok(slot))` or a route error text.
    fn slot_for(
        &self,
        item_id: Option<&Value>,
        output_index: Option<&Value>,
    ) -> Flow<Result<Option<i64>, String>> {
        let item_id = match item_id {
            None | Some(Value::Null) => None,
            Some(Value::String(id)) => Some(id.as_str()),
            Some(_) => return Ok(Err("item_id must be text or null".into())),
        };
        if item_id.is_some_and(|id| py::char_len(id) > MAX_ITEM_ID_CHARS) {
            return Ok(Err("item_id exceeds the bounded identity size".into()));
        }
        let Ok(output_index) = bounded_index(output_index) else {
            return Ok(Err("output_index must be a bounded integer or null".into()));
        };
        let mut slots = Vec::new();
        if let Some(id) = item_id.filter(|id| !id.is_empty()) {
            if self.ambiguous_item_ids.contains(id) {
                if output_index.is_none() {
                    return Ok(Err(format!(
                        "ambiguous function-call item_id {}; output_index is required",
                        py_repr(py::prefix(id, 80))?
                    )));
                }
            } else {
                match self.item_slot.get(id) {
                    None => {
                        return Ok(Err(format!(
                            "unknown function-call item_id {}",
                            py_repr(py::prefix(id, 80))?
                        )))
                    }
                    Some(slot) => slots.push(*slot),
                }
            }
        }
        if let Some(index) = output_index {
            match self.output_index_slot.get(&index) {
                None => return Ok(Err(format!("unknown function-call output_index {index}"))),
                Some(slot) => slots.push(*slot),
            }
        }
        if let Some(first) = slots.first() {
            if slots.iter().any(|slot| slot != first) {
                return Ok(Err(
                    "item_id and output_index resolve to different calls".into()
                ));
            }
            return Ok(Ok(Some(*first)));
        }
        Ok(Ok((self.tc_count > 0).then_some(self.tc_count - 1)))
    }

    fn complete_arguments(
        &mut self,
        slot: i64,
        complete: Option<&Value>,
        event_type: &str,
    ) -> Result<Vec<Translated>, String> {
        let Some(Value::String(complete)) = complete else {
            return Err(format!("{event_type} complete arguments must be text"));
        };
        let accumulated = self.slot_arguments.get(&slot).cloned().unwrap_or_default();
        if *complete == accumulated {
            return Ok(Vec::new());
        }
        let Some(suffix) = complete.strip_prefix(accumulated.as_str()) else {
            return Err(format!(
                "{event_type} complete arguments disagree with streamed prefix for slot {slot}"
            ));
        };
        let suffix = suffix.to_owned();
        self.slot_arguments.insert(slot, complete.clone());
        if suffix.is_empty() {
            return Ok(Vec::new());
        }
        Ok(vec![self.chunk(
            json!({"tool_calls": [{"index": slot, "function": {"arguments": suffix}}]}),
            None,
            None,
        )])
    }

    fn reconcile_function_calls(
        &mut self,
        output: &[&Map<String, Value>],
        event_type: &str,
        output_indices: Option<&[Option<&Value>]>,
    ) -> Flow<Result<Vec<Translated>, String>> {
        let mut chunks = Vec::new();
        for (position, item) in output.iter().enumerate() {
            if item.get("type").and_then(Value::as_str) != Some("function_call") {
                continue;
            }
            let output_index: Option<Value> = match output_indices {
                Some(indices) => indices.get(position).copied().flatten().cloned(),
                None => Some(json!(position)),
            };
            let routed_index = output_index.filter(|index| {
                index
                    .as_i64()
                    .is_some_and(|i| self.output_index_slot.contains_key(&i))
            });
            let slot = match self.slot_for(item.get("id"), routed_index.as_ref())? {
                Err(error) => return Ok(Err(error)),
                Ok(None) => {
                    return Ok(Err(format!(
                        "{event_type} function call had no started slot"
                    )))
                }
                Ok(Some(slot)) => slot,
            };
            let identity = self.slot_identity.get(&slot).cloned();
            for field in ["call_id", "name"] {
                let value = match item.get(field) {
                    None => "",
                    Some(Value::String(text)) => text.as_str(),
                    Some(_) => {
                        return Ok(Err(format!(
                            "{event_type} function_call.{field} must be text"
                        )))
                    }
                };
                let known = identity.as_ref().map(|identity| {
                    if field == "call_id" {
                        identity.call_id.as_str()
                    } else {
                        identity.name.as_str()
                    }
                });
                if !value.is_empty() && known != Some(value) {
                    return Ok(Err(format!(
                        "{event_type} function_call.{field} changed for slot {slot}"
                    )));
                }
            }
            if let Some(caller) = item.get("caller") {
                let unchanged = identity.as_ref().is_some_and(|identity| {
                    identity.caller_present && py::eq(&identity.caller, caller)
                });
                if !unchanged {
                    return Ok(Err(format!(
                        "{event_type} function_call.caller changed for slot {slot}"
                    )));
                }
            }
            if item.contains_key("arguments") {
                match self.complete_arguments(slot, item.get("arguments"), event_type) {
                    Ok(completed) => chunks.extend(completed),
                    Err(error) => return Ok(Err(error)),
                }
            }
        }
        Ok(Ok(chunks))
    }

    fn reconcile_terminal_text(
        &mut self,
        output: &[&Map<String, Value>],
    ) -> Result<Vec<Translated>, String> {
        let mut parts = String::new();
        let mut any_part = false;
        let mut snapshot_complete = true;
        for item in output {
            if item.get("type").and_then(Value::as_str) != Some("message")
                || !message_is_user_visible(item)
            {
                continue;
            }
            let content = match item.get("content") {
                None | Some(Value::Null) => Vec::new(),
                Some(Value::Array(parts)) => parts.iter().collect(),
                Some(_) => return Err("terminal message content must be an array".into()),
            };
            let mut item_has_text = false;
            for part in content {
                let Some(part) = part.as_object() else {
                    return Err("terminal message content part must be an object".into());
                };
                let Some(part_type) = part.get("type").and_then(Value::as_str) else {
                    return Err("terminal message part type must be text".into());
                };
                let field = match part_type {
                    "output_text" => "text",
                    "refusal" => "refusal",
                    _ => continue,
                };
                match part.get(field) {
                    None | Some(Value::Null) => snapshot_complete = false,
                    Some(Value::String(text)) => {
                        item_has_text = true;
                        any_part = true;
                        parts.push_str(text);
                    }
                    Some(_) => return Err("terminal message text must be text or null".into()),
                }
            }
            snapshot_complete = snapshot_complete && item_has_text;
        }
        if !snapshot_complete || !any_part {
            return Ok(Vec::new());
        }
        let emitted_chars = py::char_len(&self.emitted_text);
        let prefix = py::prefix(&parts, emitted_chars);
        if py::char_len(prefix) != emitted_chars || prefix != self.emitted_text {
            return Err("terminal message contradicts the emitted text prefix".into());
        }
        let suffix = parts[prefix.len()..].to_owned();
        if suffix.is_empty() {
            return Ok(Vec::new());
        }
        Ok(vec![self.chunk(json!({ "content": suffix }), None, None)])
    }

    fn function_call_added(
        &mut self,
        event: &Map<String, Value>,
        item: &Map<String, Value>,
        output_index: Option<i64>,
    ) -> Flow<Vec<Translated>> {
        let item_id = match item.get("id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(id)) => id.clone(),
            Some(_) => return Ok(protocol_error("function_call item.id must be text or null")),
        };
        if py::char_len(&item_id) > MAX_ITEM_ID_CHARS {
            return Ok(protocol_error(
                "function_call item.id exceeds the bounded identity size",
            ));
        }
        let call_id = match item.get("call_id") {
            None => String::new(),
            Some(Value::String(id)) => id.clone(),
            Some(_) => return Ok(protocol_error("function_call call_id must be text")),
        };
        if py::char_len(&call_id) > MAX_ITEM_ID_CHARS {
            return Ok(protocol_error(
                "function_call call_id exceeds the bounded identity size",
            ));
        }
        let name = match item.get("name") {
            None => String::new(),
            Some(Value::String(name)) => name.clone(),
            Some(_) => return Ok(protocol_error("function_call name must be text")),
        };
        if py::char_len(&name) > MAX_TOOL_NAME_CHARS {
            return Ok(protocol_error(
                "function_call name exceeds the bounded identity size",
            ));
        }
        let Some(initial_arguments) = text_or_null(item.get("arguments")) else {
            return Ok(protocol_error(
                "function_call arguments must be text or null",
            ));
        };
        let raw_caller = item.get("caller").cloned().unwrap_or(Value::Null);
        let caller_present = !raw_caller.is_null();
        let agent = match item.get("agent") {
            None | Some(Value::Null) => event.get("agent").cloned().unwrap_or(Value::Null),
            Some(agent) => agent.clone(),
        };
        match &agent {
            Value::Null => {}
            Value::Object(object) if agent_name(object).is_none() => {
                return Ok(protocol_error(
                    "function_call agent_name must be non-empty text",
                ))
            }
            Value::Object(_) => {}
            _ => {
                return Ok(protocol_error(
                    "function_call agent must be an object or null",
                ))
            }
        }
        let position = match output_index {
            Some(index) => Some(Position::OutputIndex(index)),
            None if !item_id.is_empty() => Some(Position::ItemId(item_id.clone())),
            None => None,
        };
        let identity = Identity {
            call_id: call_id.clone(),
            name: name.clone(),
            caller_present,
            caller: raw_caller.clone(),
            agent: agent.clone(),
            initial_arguments: initial_arguments.clone(),
        };
        if let Some(position) = &position {
            if let Some(previous) = self.function_position_identity.get(position) {
                if *previous != identity {
                    return Ok(protocol_error(
                        "function_call response position was reused with different identity fields",
                    ));
                }
                return Ok(Vec::new());
            }
        }
        let slot = self.tc_count;
        self.tc_count += 1;
        if let Some(position) = position {
            self.function_position_slot.insert(position.clone(), slot);
            self.function_position_identity
                .insert(position, identity.clone());
        }
        if !item_id.is_empty() && !self.ambiguous_item_ids.contains(&item_id) {
            match self.item_slot.get(&item_id) {
                None => {
                    self.item_slot.insert(item_id.clone(), slot);
                }
                Some(prior) if *prior != slot => {
                    self.item_slot.remove(&item_id);
                    self.ambiguous_item_ids.insert(item_id.clone());
                }
                Some(_) => {}
            }
        }
        if let Some(index) = output_index {
            if self
                .output_index_slot
                .get(&index)
                .is_some_and(|prior| *prior != slot)
            {
                return Ok(protocol_error(
                    "function_call output_index was reused by another call",
                ));
            }
            self.output_index_slot.insert(index, slot);
        }
        self.slot_arguments.insert(slot, initial_arguments.clone());
        self.slot_identity.insert(slot, identity);
        let mut tool_call = json!({
            "index": slot, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": initial_arguments},
        });
        let call = tool_call.as_object_mut().expect("tool call is an object");
        if caller_present {
            call.insert("caller".into(), raw_caller);
        }
        if let Some(name) = agent.as_object().and_then(agent_name) {
            let caller = call
                .entry("caller")
                .or_insert_with(|| json!({"type": "multi_agent"}));
            if let Some(caller) = caller.as_object_mut() {
                caller.insert("agent_name".into(), json!(name));
            }
        }
        Ok(vec![self.chunk(
            json!({ "tool_calls": [tool_call] }),
            None,
            None,
        )])
    }

    /// `_terminal_response`: the validated output items.
    fn terminal_output<'a>(
        event: &'a Map<String, Value>,
        event_type: &str,
    ) -> Result<TerminalOutput<'a>, Vec<Translated>> {
        let Some(response) = event.get("response").and_then(Value::as_object) else {
            return Err(protocol_error(&format!(
                "{event_type}.response must be an object"
            )));
        };
        let output = match response.get("output") {
            None | Some(Value::Null) => Vec::new(),
            Some(Value::Array(items)) => items.iter().collect(),
            Some(_) => {
                return Err(protocol_error(&format!(
                    "{event_type}.response.output must be an array or null"
                )))
            }
        };
        let mut objects = Vec::new();
        for item in output {
            match item.as_object() {
                Some(object) => objects.push(object),
                None => {
                    return Err(protocol_error(&format!(
                        "{event_type}.response.output contains a non-object item"
                    )))
                }
            }
        }
        Ok((response, objects))
    }

    fn terminal(&mut self, event: &Map<String, Value>, event_type: &str) -> Flow<Vec<Translated>> {
        let (response, output) = match Self::terminal_output(event, event_type) {
            Ok(parts) => parts,
            Err(error) => return Ok(error),
        };
        let mut out = match self.reconcile_function_calls(&output, event_type, None)? {
            Ok(chunks) => chunks,
            Err(error) => return Ok(protocol_error(&error)),
        };
        let finish = if event_type == "response.completed" {
            if self.tc_count > 0 {
                "tool_calls"
            } else {
                "stop"
            }
        } else {
            let Some(details) = response
                .get("incomplete_details")
                .and_then(Value::as_object)
            else {
                return Ok(protocol_error(
                    "response.incomplete incomplete_details must be an object",
                ));
            };
            let reason = match details.get("reason") {
                None => "",
                Some(Value::String(reason)) => reason.as_str(),
                Some(_) => return Ok(protocol_error("response.incomplete reason must be text")),
            };
            if reason != "max_output_tokens" {
                let label = if reason.is_empty() {
                    "missing reason"
                } else {
                    reason
                };
                out.push(Translated::Chunk(json!({"error": {
                    "message": format!("response.incomplete: {label}"),
                    "type": if reason.is_empty() { "server_error" } else { reason },
                }})));
                out.push(Translated::Done);
                return Ok(out);
            }
            "length"
        };
        match self.reconcile_terminal_text(&output) {
            Ok(chunks) => out.extend(chunks),
            Err(error) => return Ok(protocol_error(&error)),
        }
        match self.reasoning.terminal(&output) {
            Ok(texts) => out.extend(self.reasoning_chunks(Ok(texts))),
            Err(error) => return Ok(protocol_error(&error)),
        }
        let raw_usage = response
            .get("usage")
            .filter(|u| truthy(Some(u)))
            .cloned()
            .unwrap_or(json!({}));
        let mut usage = convert_responses_usage(&raw_usage)?;
        if program_needs_followup(&output) {
            usage.insert("_program_pending".into(), json!(true));
        }
        out.push(self.chunk(json!({}), Some(finish), Some(usage)));
        out.push(Translated::Done);
        Ok(out)
    }

    pub(crate) fn translate(&mut self, event: &Map<String, Value>) -> Flow<Vec<Translated>> {
        let etype = match event.get("type") {
            None => "",
            Some(Value::String(text)) => text.as_str(),
            Some(_) => return Ok(protocol_error("event.type must be text")),
        };
        if REASONING_EVENTS.contains(&etype) {
            let texts = self.reasoning.event(event, etype);
            return Ok(self.reasoning_chunks(texts));
        }
        match etype {
            "response.output_text.delta" => {
                if bounded_index(event.get("output_index")).is_err() {
                    return Ok(protocol_error(
                        "output_text.delta output_index must be a bounded integer",
                    ));
                }
                let hidden_by_agent = match event.get("agent") {
                    None | Some(Value::Null) => false,
                    Some(Value::Object(agent)) => match agent_name(agent) {
                        None => {
                            return Ok(protocol_error(
                                "output_text.delta agent_name must be non-empty text",
                            ))
                        }
                        Some(name) => name != "/root",
                    },
                    Some(_) => {
                        return Ok(protocol_error(
                            "output_text.delta agent must be an object or null",
                        ))
                    }
                };
                let Some(delta) = text_or_null(event.get("delta")) else {
                    return Ok(protocol_error(
                        "output_text.delta delta must be text or null",
                    ));
                };
                let hidden_index = event
                    .get("output_index")
                    .and_then(Value::as_i64)
                    .is_some_and(|index| self.hidden_text_output_indices.contains(&index));
                if hidden_index || hidden_by_agent {
                    return Ok(Vec::new());
                }
                Ok(vec![self.chunk(json!({ "content": delta }), None, None)])
            }
            "response.refusal.delta" => {
                let Some(delta) = text_or_null(event.get("delta")) else {
                    return Ok(protocol_error("refusal.delta delta must be text or null"));
                };
                Ok(vec![self.chunk(json!({ "content": delta }), None, None)])
            }
            "response.output_item.added" => {
                let Some(item) = event.get("item").and_then(Value::as_object) else {
                    return Ok(protocol_error("output_item.added item must be an object"));
                };
                let Ok(output_index) = bounded_index(event.get("output_index")) else {
                    return Ok(protocol_error(
                        "output_item.added output_index must be a bounded integer",
                    ));
                };
                let valid_agent = match item.get("agent") {
                    None | Some(Value::Null) => true,
                    Some(Value::Object(agent)) => agent_name(agent).is_some(),
                    Some(_) => false,
                };
                if !valid_agent {
                    return Ok(protocol_error(
                        "output_item.added agent must carry a non-empty text agent_name",
                    ));
                }
                let item_type = item.get("type").and_then(Value::as_str);
                if item_type == Some("message") && !message_is_user_visible(item) {
                    if let Some(index) = output_index {
                        self.hidden_text_output_indices.insert(index);
                    }
                }
                match item_type {
                    Some("reasoning") => {
                        let texts = self.reasoning.item(item, event.get("output_index"), true);
                        Ok(self.reasoning_chunks(texts))
                    }
                    Some("function_call") => self.function_call_added(event, item, output_index),
                    _ => Ok(Vec::new()),
                }
            }
            "response.function_call_arguments.delta" => {
                let Some(delta) = text_or_null(event.get("delta")) else {
                    return Ok(protocol_error(
                        "function_call_arguments.delta delta must be text or null",
                    ));
                };
                let slot = match self.slot_for(event.get("item_id"), event.get("output_index"))? {
                    Err(error) => {
                        return Ok(protocol_error(&format!(
                            "function_call_arguments.delta {error}"
                        )))
                    }
                    Ok(None) => {
                        return Ok(protocol_error(
                            "function_call_arguments.delta arrived before a function call",
                        ))
                    }
                    Ok(Some(slot)) => slot,
                };
                self.slot_arguments
                    .entry(slot)
                    .or_default()
                    .push_str(&delta);
                Ok(vec![self.chunk(
                    json!({"tool_calls": [{"index": slot, "function": {"arguments": delta}}]}),
                    None,
                    None,
                )])
            }
            "response.function_call_arguments.done" => {
                let slot = match self.slot_for(event.get("item_id"), event.get("output_index"))? {
                    Err(error) => {
                        return Ok(protocol_error(&format!(
                            "function_call_arguments.done {error}"
                        )))
                    }
                    Ok(None) => {
                        return Ok(protocol_error(
                            "function_call_arguments.done arrived before a function call",
                        ))
                    }
                    Ok(Some(slot)) => slot,
                };
                match self.complete_arguments(slot, event.get("arguments"), etype) {
                    Ok(chunks) => Ok(chunks),
                    Err(error) => Ok(protocol_error(&error)),
                }
            }
            "response.output_item.done" => {
                let Some(item) = event.get("item").and_then(Value::as_object) else {
                    return Ok(protocol_error("output_item.done item must be an object"));
                };
                match item.get("type").and_then(Value::as_str) {
                    Some("function_call") => {
                        let indices = [event.get("output_index")];
                        match self.reconcile_function_calls(&[item], etype, Some(&indices))? {
                            Ok(chunks) => Ok(chunks),
                            Err(error) => Ok(protocol_error(&error)),
                        }
                    }
                    Some("reasoning") => {
                        let texts = self.reasoning.item(item, event.get("output_index"), false);
                        Ok(self.reasoning_chunks(texts))
                    }
                    _ => Ok(Vec::new()),
                }
            }
            "response.completed" | "response.incomplete" => self.terminal(event, etype),
            "error" | "response.failed" | "response.error" => {
                let response = match event.get("response") {
                    None | Some(Value::Null) => None,
                    Some(Value::Object(response)) => Some(response),
                    Some(_) => {
                        return Ok(protocol_error(&format!(
                            "{etype}.response must be an object or null"
                        )))
                    }
                };
                let candidates = [response.and_then(|r| r.get("error")), event.get("error")];
                let chosen = candidates.into_iter().flatten().find(|v| truthy(Some(v)));
                let mut error: Map<String, Value> = match chosen {
                    Some(Value::Object(error)) => error.clone(),
                    Some(_) => Map::new(),
                    None if etype == "error" && !event.is_empty() => event.clone(),
                    None => Map::new(),
                };
                if invalid_encrypted_content_object(&error) {
                    error.insert("code".into(), json!("invalid_encrypted_content"));
                }
                let code = match error.get("code") {
                    Some(Value::String(code)) if !code.is_empty() => code.clone(),
                    _ => etype.to_owned(),
                };
                let message = match error.get("message") {
                    Some(Value::String(message)) if !message.is_empty() => message.clone(),
                    _ => etype.to_owned(),
                };
                let error_type = error
                    .get("type")
                    .filter(|v| truthy(Some(v)))
                    .cloned()
                    .unwrap_or_else(|| json!(code));
                let mut projected = error;
                projected.insert("message".into(), json!(format!("{code}: {message}")));
                projected.insert("type".into(), error_type);
                Ok(vec![Translated::Chunk(json!({ "error": projected }))])
            }
            _ => Ok(Vec::new()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_repr_like_python() {
        assert_eq!(py_repr("fc_9").unwrap(), "'fc_9'");
        assert_eq!(py_repr("it's").unwrap(), "\"it's\"");
    }
}
