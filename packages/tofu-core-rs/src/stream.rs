//! Provider stream parsing, ported from Python's production byte path:
//! `SSEByteStream` + `SSEFramer` (`lib/llm/_sse_stream.py`, `_sse_framer.py`)
//! → `SSEAccumulator.feed_event` (`lib/llm/_sse_core.py`, with the Anthropic
//! and Responses translators and in-stream error classification) →
//! `finalize`, projected with `lib/cost.py` usage normalisation.
//!
//! Python is the oracle: `tests/fixtures/stream_golden.json` comes from
//! `scripts/gen_stream_golden.py`. The request body is `{model, max_tokens}`
//! (no tools, so tool-name fragments are joined without a toolset oracle).
//! Python's process-global side effect (`_learn_model_limit`) is returned as
//! `Raised::learned_limit`. Protocol-private replay sidecars
//! (`_responses_items`, `_anthropic_content_blocks`) are not projected.
//! An input outside the port returns `Unsupported`, never guessed output.

mod anthropic;
mod errors;
mod framer;
mod pattern;
mod py;
mod reasoning;
mod responses;
mod usage;

use std::collections::{BTreeMap, HashMap};

use serde_json::{json, Map, Value};

use crate::canonical::{Result, Unsupported};
use crate::wire::Protocol;

pub use errors::{LearnedLimit, Raised};
pub use usage::Normalized;

use anthropic::AnthropicTranslator;
use errors::{handle_sse_error, ErrorContext, Flow, Stop};
use framer::Frame;
use py::truthy;
use responses::ResponsesTranslator;

/// One translated item: an OpenAI-shaped chunk or the internal `[DONE]`.
pub(crate) enum Translated {
    Chunk(Value),
    Done,
}

enum Translator {
    Chat,
    Anthropic(Box<AnthropicTranslator>),
    Responses(Box<ResponsesTranslator>),
}

const MAX_CONSECUTIVE_PARSE_ERRORS: u32 = 10;
const MAX_TOOL_CALL_INDEX: usize = 4095;
const MAX_TOOL_CALL_ID_CHARS: usize = 512;
const INTERNAL_TOOL_PREFIXES: [&str; 3] = ["antml:", "anthropic.", "__"];
const ANTHROPIC_MODEL_EVENT_TYPES: [&str; 6] = [
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
];
const GATEWAY_LIMIT_MAX_CHARS: usize = 120;
const GATEWAY_LIMIT_PHRASES: [&str; 8] = [
    "当前小时请求过于频繁",
    "请求过于频繁",
    "rate limit exceeded",
    "too many requests",
    "请求频率过高",
    "已达到 rate limit",
    "quota exceeded",
    "超出频率限制",
];

/// The finalized `ProviderStreamResult`, projected without nondeterministic
/// fields (trace ids, timings, request receipts, wire fingerprints).
#[derive(Debug, Clone, PartialEq)]
pub struct Finished {
    pub message: Map<String, Value>,
    pub provider_finish_reason: Option<String>,
    pub saw_done: bool,
    pub saw_finish_reason: bool,
    pub malformed_frame_count: u64,
    pub state: &'static str,
    pub model_refusal: Option<Value>,
    pub usage: Map<String, Value>,
    pub normalized: Normalized,
    pub convention: &'static str,
    pub split_input: (i64, i64),
}

impl Finished {
    /// The tuple-compatibility finish reason (`stop` when none was seen).
    pub fn finish_reason(&self) -> &str {
        self.provider_finish_reason.as_deref().unwrap_or("stop")
    }

    /// `_refusal.is_model_refusal`.
    pub fn is_model_refusal(&self) -> bool {
        self.state == "provider_finished" && self.finish_reason() == "content_filter"
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum StreamOutcome {
    Raised(Raised),
    Finished(Box<Finished>),
}

impl StreamOutcome {
    /// The golden-fixture projection (`gen_stream_golden.project`).
    pub fn to_json(&self) -> Value {
        match self {
            Self::Raised(raised) => json!({ "error": raised.to_json() }),
            Self::Finished(done) => json!({
                "message": done.message,
                "provider_finish_reason": done.provider_finish_reason,
                "finish_reason": done.finish_reason(),
                "saw_done": done.saw_done,
                "saw_finish_reason": done.saw_finish_reason,
                "malformed_frame_count": done.malformed_frame_count,
                "state": done.state,
                "model_refusal": done.model_refusal,
                "is_model_refusal": done.is_model_refusal(),
                "usage": done.usage,
                "normalized": done.normalized.to_json(),
                "convention": done.convention,
                "split_input": [done.split_input.0, done.split_input.1],
            }),
        }
    }
}

/// Parse one complete SSE response body exactly as the Python production path
/// does for a `{model}` request (no `max_tokens`) on `protocol`.
pub fn parse(
    protocol: Protocol,
    model: &str,
    trace_id: &str,
    body: &[u8],
) -> Result<StreamOutcome> {
    parse_request(protocol, model, None, trace_id, body)
}

/// [`parse`] for a `{model, max_tokens}` request; `max_tokens` only shapes
/// `ModelLimitError.requested_limit`.
pub fn parse_request(
    protocol: Protocol,
    model: &str,
    max_tokens: Option<i64>,
    trace_id: &str,
    body: &[u8],
) -> Result<StreamOutcome> {
    let translator = match protocol {
        Protocol::OpenAi => Translator::Chat,
        Protocol::Anthropic => Translator::Anthropic(Box::default()),
        Protocol::Responses => Translator::Responses(Box::default()),
    };
    let mut accumulator = Accumulator::new(model, trace_id, translator);
    accumulator.max_tokens = max_tokens.unwrap_or(0);
    match accumulator.run(body) {
        Ok(finished) => Ok(StreamOutcome::Finished(Box::new(finished))),
        Err(Stop::Raise(raised)) => Ok(StreamOutcome::Raised(*raised)),
        Err(Stop::Unsupported(what)) => Err(Unsupported(format!("stream: {what}")).into()),
    }
}

fn is_minimax(model: &str) -> bool {
    let lower = model.to_lowercase();
    lower.contains("minimax") || lower == "m2-her"
}

fn new_slot(id: &str, name: &str) -> Map<String, Value> {
    let mut slot = Map::new();
    slot.insert("id".into(), json!(id));
    slot.insert("type".into(), json!("function"));
    slot.insert("function".into(), json!({"name": name, "arguments": ""}));
    slot
}

fn slot_str<'a>(slot: &'a Map<String, Value>, key: &str) -> &'a str {
    match key {
        "id" => slot["id"].as_str().unwrap_or(""),
        field => slot["function"][field].as_str().unwrap_or(""),
    }
}

fn set_function(slot: &mut Map<String, Value>, field: &str, value: String) {
    if let Some(function) = slot.get_mut("function").and_then(Value::as_object_mut) {
        function.insert(field.into(), Value::String(value));
    }
}

/// `is_gateway_limit_response` for a tool-call-free body.
fn is_gateway_limit_response(content: &str, finish_reason: Option<&str>) -> bool {
    if finish_reason != Some("stop") {
        return false;
    }
    let text = py::strip(content);
    if text.is_empty() || py::char_len(text) > GATEWAY_LIMIT_MAX_CHARS {
        return false;
    }
    let lowered = text.to_lowercase();
    GATEWAY_LIMIT_PHRASES
        .iter()
        .any(|phrase| lowered.contains(phrase) || text.contains(phrase))
}

struct Accumulator {
    model: String,
    trace_id: String,
    max_tokens: i64,
    translator: Translator,
    mm_mode: bool,
    content: String,
    thinking: String,
    signature: String,
    tool_calls: BTreeMap<usize, Map<String, Value>>,
    finish_reason: Option<String>,
    usage: Option<Map<String, Value>>,
    saw_done: bool,
    saw_finish_reason: bool,
    chunk_count: u64,
    protocol_mismatch_stop: bool,
    mm_in_think: bool,
    mm_buf: String,
    consecutive_parse_errors: u32,
    malformed: u64,
    tc_unindexed_slot: Option<usize>,
    tc_index_map: HashMap<usize, usize>,
}

impl Accumulator {
    fn new(model: &str, trace_id: &str, translator: Translator) -> Self {
        Self {
            model: model.to_owned(),
            trace_id: trace_id.to_owned(),
            max_tokens: 0,
            translator,
            mm_mode: is_minimax(model),
            content: String::new(),
            thinking: String::new(),
            signature: String::new(),
            tool_calls: BTreeMap::new(),
            finish_reason: None,
            usage: None,
            saw_done: false,
            saw_finish_reason: false,
            chunk_count: 0,
            protocol_mismatch_stop: false,
            mm_in_think: false,
            mm_buf: String::new(),
            consecutive_parse_errors: 0,
            malformed: 0,
            tc_unindexed_slot: None,
            tc_index_map: HashMap::new(),
        }
    }

    /// `SSEByteStream.feed` + `finalize`, then `SSEAccumulator.finalize`.
    fn run(&mut self, body: &[u8]) -> Flow<Finished> {
        let (frames, truncated) = framer::frame(body)?;
        let mut stopped = false;
        for frame in frames {
            match frame {
                Frame::Issue => self.malformed += 1,
                Frame::Event(data) => {
                    if self.feed_payload(&data)? {
                        stopped = true;
                        break;
                    }
                }
            }
        }
        if !stopped && truncated {
            self.malformed += 1;
        }
        self.finalize()
    }

    fn feed_payload(&mut self, data: &str) -> Flow<bool> {
        if self.protocol_mismatch_stop {
            return Ok(true);
        }
        if data == "[DONE]" {
            self.saw_done = true;
            return Ok(true);
        }
        if py::strip(data).is_empty() {
            return Ok(false);
        }
        self.chunk_count += 1;
        if !matches!(self.translator, Translator::Chat) {
            return self.feed_translated(data);
        }
        let Some(chunk) = py::loads(data)? else {
            self.consecutive_parse_errors += 1;
            self.malformed += 1;
            if self.consecutive_parse_errors >= MAX_CONSECUTIVE_PARSE_ERRORS {
                return Err(Raised::retryable(
                    format!(
                        "{} consecutive SSE parse errors \u{2014} stream appears corrupt",
                        self.consecutive_parse_errors
                    ),
                    0,
                )
                .into());
            }
            return Ok(false);
        };
        self.consecutive_parse_errors = 0;
        if let Some(object) = chunk.as_object() {
            let foreign = !object.contains_key("choices")
                && matches!(object.get("error"), None | Some(Value::Null))
                && object
                    .get("type")
                    .and_then(Value::as_str)
                    .is_some_and(|kind| {
                        ANTHROPIC_MODEL_EVENT_TYPES.contains(&kind) || kind.starts_with("response.")
                    });
            if foreign {
                self.protocol_mismatch_stop = true;
                self.malformed += 1;
                return Ok(true);
            }
        }
        self.process_chunk(&chunk)?;
        Ok(false)
    }

    fn feed_translated(&mut self, data: &str) -> Flow<bool> {
        let Some(event) = py::loads(data)? else {
            self.malformed += 1;
            return Ok(false);
        };
        let Some(event) = event.as_object() else {
            self.malformed += 1;
            return Ok(false);
        };
        let items = match &mut self.translator {
            Translator::Anthropic(translator) => translator.translate(event)?,
            Translator::Responses(translator) => translator.translate(event)?,
            Translator::Chat => unreachable!("chat payloads are not translated"),
        };
        for item in items {
            match item {
                Translated::Done => {
                    self.saw_done = true;
                    return Ok(true);
                }
                Translated::Chunk(chunk) => self.process_chunk(&chunk)?,
            }
        }
        Ok(false)
    }

    fn process_chunk(&mut self, chunk: &Value) -> Flow<()> {
        let Some(chunk) = chunk.as_object() else {
            self.malformed += 1;
            return Ok(());
        };
        if let Some(error) = chunk.get("error").filter(|e| !e.is_null()) {
            let ctx = ErrorContext {
                model: &self.model,
                trace_id: &self.trace_id,
                max_tokens: self.max_tokens,
                produced_output: self.produced_output(),
            };
            return Err(handle_sse_error(error, &ctx));
        }
        if let Some(usage) = chunk.get("usage").filter(|u| truthy(Some(u))) {
            match usage {
                Value::Object(usage) => self.usage = Some(usage.clone()),
                _ => self.malformed += 1,
            }
        }
        let choices = match chunk.get("choices") {
            None => return Ok(()),
            Some(Value::Array(choices)) => choices,
            Some(_) => {
                self.malformed += 1;
                return Ok(());
            }
        };
        let Some(choice) = choices.first() else {
            return Ok(());
        };
        let Some(choice) = choice.as_object() else {
            self.malformed += 1;
            return Ok(());
        };
        if choice.contains_key("message") && !choice.contains_key("delta") {
            self.malformed += 1;
        }
        let empty = Map::new();
        let delta = match choice.get("delta") {
            None | Some(Value::Null) => &empty,
            Some(Value::Object(delta)) => delta,
            Some(_) => {
                self.malformed += 1;
                &empty
            }
        };
        if let Some(finish) = choice.get("finish_reason").filter(|f| truthy(Some(f))) {
            match finish {
                Value::String(finish) => {
                    self.finish_reason = Some(finish.clone());
                    self.saw_finish_reason = true;
                }
                _ => self.malformed += 1,
            }
        }
        if let Some(usage) = choice.get("usage").filter(|u| truthy(Some(u))) {
            match usage {
                Value::Object(usage) => self.usage = Some(usage.clone()),
                _ => self.malformed += 1,
            }
        }
        self.handle_delta(delta)
    }

    fn handle_delta(&mut self, delta: &Map<String, Value>) -> Flow<()> {
        let mut reasoning = String::new();
        for key in ["thinking", "reasoning_content", "reasoning"] {
            match delta.get(key) {
                None | Some(Value::Null) => continue,
                Some(Value::String(text)) => {
                    if !text.is_empty() {
                        reasoning = text.clone();
                        break;
                    }
                }
                Some(_) => self.malformed += 1,
            }
        }
        let role_thinking = delta.get("role").and_then(Value::as_str) == Some("thinking");
        if reasoning.is_empty() && role_thinking {
            match delta.get("content") {
                None | Some(Value::Null) => {}
                Some(Value::String(text)) => reasoning = text.clone(),
                Some(_) => self.malformed += 1,
            }
        }
        match delta.get("reasoning_details") {
            Some(Value::Array(details)) => {
                if reasoning.is_empty() {
                    for detail in details {
                        let Some(detail) = detail.as_object() else {
                            self.malformed += 1;
                            continue;
                        };
                        let text = detail
                            .get("thinking")
                            .filter(|v| truthy(Some(v)))
                            .or_else(|| detail.get("text"));
                        match text {
                            None | Some(Value::Null) => {}
                            Some(Value::String(text)) => reasoning.push_str(text),
                            Some(_) => self.malformed += 1,
                        }
                    }
                }
                for detail in details.iter().filter_map(Value::as_object) {
                    match detail.get("signature").filter(|v| truthy(Some(v))) {
                        None => {}
                        Some(Value::String(signature)) => self.signature.push_str(signature),
                        Some(_) => self.malformed += 1,
                    }
                }
            }
            None | Some(Value::Null) => {}
            Some(_) => self.malformed += 1,
        }
        if !reasoning.is_empty() {
            self.thinking.push_str(&reasoning);
        }
        match delta.get("thinking_signature").filter(|v| truthy(Some(v))) {
            None => {}
            Some(Value::String(signature)) => self.signature.push_str(signature),
            Some(_) => self.malformed += 1,
        }
        if delta.contains_key("content") && !role_thinking {
            let text = match &delta["content"] {
                Value::Null => "",
                Value::String(text) => text.as_str(),
                _ => {
                    self.malformed += 1;
                    ""
                }
            };
            if !text.is_empty() {
                if self.mm_mode {
                    self.feed_minimax(text);
                } else {
                    self.content.push_str(text);
                }
            }
        }
        match delta.get("tool_calls") {
            None | Some(Value::Null) => Ok(()),
            Some(Value::Array(calls)) => calls.iter().try_for_each(|call| self.tool_delta(call)),
            Some(_) => {
                self.malformed += 1;
                Ok(())
            }
        }
    }

    /// One `tool_calls` delta of `_handle_delta`.
    fn tool_delta(&mut self, call: &Value) -> Flow<()> {
        let Some(call) = call.as_object() else {
            self.malformed += 1;
            return Ok(());
        };
        let incoming_id = match call.get("id") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(id)) if py::char_len(id) > MAX_TOOL_CALL_ID_CHARS => {
                self.malformed += 1;
                String::new()
            }
            Some(Value::String(id)) => id.clone(),
            Some(_) => {
                self.malformed += 1;
                String::new()
            }
        };
        let empty = Map::new();
        let function = match call.get("function") {
            None | Some(Value::Null) => &empty,
            Some(Value::Object(function)) => function,
            Some(_) => {
                self.malformed += 1;
                &empty
            }
        };
        let caller = call.get("caller");
        if caller.is_some_and(|c| !c.is_null() && !c.is_object()) {
            self.malformed += 1;
        }
        let name = match function.get("name") {
            None | Some(Value::Null) => String::new(),
            Some(Value::String(name)) => name.clone(),
            Some(_) => {
                self.malformed += 1;
                String::new()
            }
        };
        let arguments = match function.get("arguments") {
            None | Some(Value::Null) => None,
            Some(Value::String(arguments)) => Some(arguments.clone()),
            Some(_) => {
                self.malformed += 1;
                None
            }
        };
        let upstream = match call.get("index") {
            None => None,
            Some(raw) => {
                let parsed = match raw {
                    Value::Number(number) => number
                        .as_u64()
                        .filter(|index| *index as usize <= MAX_TOOL_CALL_INDEX)
                        .map(|index| index as usize),
                    Value::String(text) => {
                        let trimmed = py::strip(text);
                        if !trimmed.is_empty()
                            && trimmed.chars().all(|ch| py::decimal(ch).is_some())
                        {
                            let digits = trimmed.trim_start_matches('0');
                            let digits: Vec<char> = if digits.is_empty() {
                                vec!['0']
                            } else {
                                digits.chars().collect()
                            };
                            if digits.len() > 4 {
                                None
                            } else if digits.iter().all(|ch| py::decimal(*ch).is_some()) {
                                py::decimal_value(&digits, MAX_TOOL_CALL_INDEX as i64)
                                    .map(|index| index as usize)
                            } else {
                                let digits: String = digits.iter().collect();
                                return Err(Raised::plain(
                                    "ValueError",
                                    format!(
                                        "invalid literal for int() with base 10: {}",
                                        py::repr(&digits)?
                                    ),
                                )
                                .into());
                            }
                        } else {
                            None
                        }
                    }
                    _ => None,
                };
                if parsed.is_none() {
                    self.malformed += 1;
                }
                parsed
            }
        };
        let mut idx = match upstream {
            Some(upstream) => self
                .tc_index_map
                .get(&upstream)
                .copied()
                .unwrap_or(upstream),
            None => {
                let idx = self.tc_unindexed_slot.unwrap_or_else(|| {
                    self.tool_calls
                        .keys()
                        .next_back()
                        .map_or(0, |last| last + 1)
                });
                self.tc_unindexed_slot = Some(idx);
                idx
            }
        };
        self.tool_calls
            .entry(idx)
            .or_insert_with(|| new_slot("", ""));
        let mut slot_id_before = slot_str(&self.tool_calls[&idx], "id").to_owned();
        let next_slot = |calls: &BTreeMap<usize, Map<String, Value>>| {
            calls.keys().next_back().map_or(0, |last| last + 1)
        };
        if !incoming_id.is_empty() && !slot_id_before.is_empty() && incoming_id != slot_id_before {
            idx = next_slot(&self.tool_calls);
            self.tc_unindexed_slot = Some(idx);
            if let Some(upstream) = upstream {
                self.tc_index_map.insert(upstream, idx);
            }
            self.tool_calls.insert(idx, new_slot(&incoming_id, ""));
            slot_id_before.clear();
        } else if !incoming_id.is_empty() {
            self.tool_calls
                .get_mut(&idx)
                .expect("slot exists")
                .insert("id".into(), json!(incoming_id));
        }
        let extra_content = call.get("extra_content").filter(|v| truthy(Some(v)));
        let slot = self.tool_calls.get_mut(&idx).expect("slot exists");
        if let Some(extra) = extra_content {
            slot.insert("extra_content".into(), extra.clone());
        }
        if let Some(caller) = caller {
            slot.insert("caller".into(), caller.clone());
        }
        let mut ignore_reissued_arguments = false;
        if !name.is_empty() {
            let previous = slot_str(&self.tool_calls[&idx], "name").to_owned();
            let new_id = !incoming_id.is_empty()
                && !slot_id_before.is_empty()
                && incoming_id != slot_id_before;
            if previous.is_empty() {
                set_function(
                    self.tool_calls.get_mut(&idx).expect("slot exists"),
                    "name",
                    name,
                );
            } else if previous == name && !new_id {
                let accumulated = slot_str(&self.tool_calls[&idx], "arguments");
                if arguments
                    .as_deref()
                    .is_some_and(|args| !args.is_empty() && args == accumulated)
                {
                    ignore_reissued_arguments = true;
                }
            } else if !new_id && self.is_name_fragment(&incoming_id, idx) {
                set_function(
                    self.tool_calls.get_mut(&idx).expect("slot exists"),
                    "name",
                    previous + &name,
                );
            } else {
                let slot = self.tool_calls.get_mut(&idx).expect("slot exists");
                if !slot_id_before.is_empty() {
                    slot.insert("id".into(), json!(slot_id_before));
                } else if !incoming_id.is_empty() {
                    slot.insert("id".into(), json!(""));
                }
                idx = next_slot(&self.tool_calls);
                self.tc_unindexed_slot = Some(idx);
                if let Some(upstream) = upstream {
                    self.tc_index_map.insert(upstream, idx);
                }
                let mut fresh = new_slot(&incoming_id, &name);
                if let Some(extra) = extra_content {
                    fresh.insert("extra_content".into(), extra.clone());
                }
                if let Some(caller) = caller {
                    fresh.insert("caller".into(), caller.clone());
                }
                self.tool_calls.insert(idx, fresh);
            }
        }
        if let Some(arguments) = arguments.filter(|_| !ignore_reissued_arguments) {
            let slot = self.tool_calls.get_mut(&idx).expect("slot exists");
            let joined = format!("{}{arguments}", slot_str(slot, "arguments"));
            set_function(slot, "arguments", joined);
        }
        Ok(())
    }

    /// `SSEAccumulator.produced_output`.
    fn produced_output(&self) -> bool {
        !self.content.is_empty()
            || !self.thinking.is_empty()
            || !self.signature.is_empty()
            || !self.tool_calls.is_empty()
            || !self.mm_buf.is_empty()
    }

    /// `_tc_is_name_fragment` with no offered toolset.
    fn is_name_fragment(&self, incoming_id: &str, idx: usize) -> bool {
        let slot = &self.tool_calls[&idx];
        let slot_id = slot_str(slot, "id");
        if !incoming_id.is_empty() && !slot_id.is_empty() && incoming_id != slot_id {
            return false;
        }
        slot_str(slot, "arguments").is_empty()
    }

    fn feed_minimax(&mut self, text: &str) {
        self.mm_buf.push_str(text);
        while !self.mm_buf.is_empty() {
            if self.mm_in_think {
                match self.mm_buf.find("</think>") {
                    None => {
                        self.thinking.push_str(&self.mm_buf);
                        self.mm_buf.clear();
                    }
                    Some(end) => {
                        self.thinking.push_str(&self.mm_buf[..end]);
                        self.mm_buf = self.mm_buf[end + "</think>".len()..].to_owned();
                        self.mm_in_think = false;
                    }
                }
                continue;
            }
            match self.mm_buf.find("<think>") {
                None => {
                    let chars: Vec<(usize, char)> = self.mm_buf.char_indices().collect();
                    let tail_start = chars.len().saturating_sub(7);
                    let tail_lt = chars[tail_start..].iter().rev().find(|(_, c)| *c == '<');
                    match tail_lt.filter(|_| chars.len() > 7) {
                        Some((cut, _)) => {
                            let cut = *cut;
                            self.content.push_str(&self.mm_buf[..cut]);
                            self.mm_buf = self.mm_buf[cut..].to_owned();
                            if cut == 0 {
                                break;
                            }
                        }
                        None => {
                            self.content.push_str(&self.mm_buf);
                            self.mm_buf.clear();
                        }
                    }
                }
                Some(start) => {
                    self.content.push_str(&self.mm_buf[..start]);
                    self.mm_buf = self.mm_buf[start + "<think>".len()..].to_owned();
                    self.mm_in_think = true;
                }
            }
        }
    }

    fn finalize(&mut self) -> Flow<Finished> {
        if self.mm_mode && !self.mm_buf.is_empty() {
            let rest = std::mem::take(&mut self.mm_buf);
            if self.mm_in_think {
                self.thinking.push_str(&rest);
            } else {
                self.content.push_str(&rest);
            }
        }
        if self.mm_mode && !self.thinking.is_empty() {
            if let Some(usage) = self.usage.as_mut().filter(|u| !u.is_empty()) {
                let reasoning = match usage.get("completion_tokens_details") {
                    None => json!(0),
                    Some(Value::Object(details)) => {
                        details.get("reasoning_tokens").cloned().unwrap_or(json!(0))
                    }
                    Some(_) => json!(0),
                };
                let positive = reasoning
                    .as_number()
                    .and_then(serde_json::Number::as_f64)
                    .is_some_and(|n| n > 0.0);
                if positive && !usage.contains_key("reasoning_tokens") {
                    usage.insert("reasoning_tokens".into(), reasoning);
                }
            }
        }
        let mut tool_calls = Vec::new();
        for slot in std::mem::take(&mut self.tool_calls).into_values() {
            let name = slot_str(&slot, "name");
            if INTERNAL_TOOL_PREFIXES
                .iter()
                .any(|prefix| name.starts_with(prefix))
            {
                continue;
            }
            let mut slot = slot;
            if py::strip(slot_str(&slot, "arguments")).is_empty() {
                set_function(&mut slot, "arguments", "{}".into());
            }
            tool_calls.push(Value::Object(slot));
        }

        let provider_finish = self.finish_reason.clone();
        let mut message = Map::new();
        message.insert("role".into(), json!("assistant"));
        if !self.thinking.is_empty() {
            message.insert("reasoning_content".into(), json!(self.thinking));
        }
        if !self.signature.is_empty() {
            message.insert("thinking_signature".into(), json!(self.signature));
        }
        if tool_calls.is_empty() {
            message.insert("content".into(), json!(self.content));
        } else {
            message.insert("tool_calls".into(), Value::Array(tool_calls.clone()));
            if !self.content.is_empty() {
                message.insert("content".into(), json!(self.content));
            }
        }
        if let Some(usage) = self.usage.as_mut().filter(|u| !u.is_empty()) {
            usage::canonicalize_usage_cache_keys(usage)?;
        }

        let finish = provider_finish.as_deref();
        let empty_response = self.saw_finish_reason
            && finish == Some("stop")
            && py::strip(&self.content).is_empty()
            && tool_calls.is_empty();
        let gateway_limit = self.saw_finish_reason
            && tool_calls.is_empty()
            && is_gateway_limit_response(&self.content, finish);
        let tool_payload_missing = matches!(finish, Some("tool_calls" | "tool_use"))
            && tool_calls.is_empty()
            && self.chunk_count > 0;
        let state = if self.malformed > 0 {
            "malformed_stream"
        } else if !self.saw_finish_reason {
            "premature_close"
        } else if tool_payload_missing {
            "tool_payload_missing"
        } else if empty_response {
            "empty_response"
        } else if gateway_limit {
            "gateway_limit_response"
        } else {
            "provider_finished"
        };
        let usage = self.usage.take().unwrap_or_default();
        let model_refusal = match &self.translator {
            Translator::Anthropic(translator) => translator.model_refusal.clone(),
            _ => None,
        };
        let normalized = usage::normalize_usage(&usage)?;
        let convention = usage::usage_cache_convention(&usage)?;
        let split_input = usage::split_input_tokens(&usage)?;
        let projected: Map<String, Value> = usage
            .into_iter()
            .filter(|(key, _)| {
                !key.starts_with('_') && key != "trace_id" && key != "stream_elapsed_ms"
            })
            .collect();
        Ok(Finished {
            message,
            provider_finish_reason: provider_finish,
            saw_done: self.saw_done,
            saw_finish_reason: self.saw_finish_reason,
            malformed_frame_count: self.malformed,
            state,
            model_refusal,
            usage: projected,
            normalized,
            convention,
            split_input,
        })
    }
}
