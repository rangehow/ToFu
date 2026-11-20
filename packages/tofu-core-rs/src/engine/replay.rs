//! The `engine_session` parity boundary: one scripted session through the
//! Rust engine, projected into the parts the Python reference
//! (`tests/rust_parity_engine/reference.py`) produces for the same case.
//!
//! Responsibility: bind the engine's ports to the case script and render the
//! canonical parts. The input schema, part names and normalisation rules are
//! the ones documented in that reference module; this file implements the
//! same contract and must not widen it.
//!
//! Port doubles and where they stand in for an unported owner:
//! - `ScriptedProvider` is the provider DISPATCH port (`lib/llm_dispatch`):
//!   the dispatch layer, not the loop, resends a pre-stream 429 and a
//!   mid-stream error on the same model, so those resends appear in the
//!   `requests` part but reach the loop as one outcome. A text chunk split
//!   (`len // 2` characters) mirrors the reference's two-chunk stream. A Stop
//!   lands after the first delivered chunk (the reference's abort seam
//!   observes the flag at the first chunk boundary either way).
//! - `ScriptedTools` returns the case's handler outcome; the effect intent,
//!   failure projection and settlement are the engine's own.
//! - `MemoryStore` is the durable side.
//!
//! Entry points: `engine_session(&Value) -> Result<Vec<(String, Vec<u8>)>, Unsupported>`
//! and `serve_lines` (the harness line protocol on stdin/stdout).

use std::collections::BTreeMap;
use std::io::{BufRead, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Mutex;

use base64::Engine as _;
use serde_json::{json, Map, Value};

use super::cancel::CancelToken;
use super::memory_store::{MemoryStore, OP_EVENTS};
use super::ports::{EventSink, ProviderPort, ProviderRequest, StoreError, StreamItem, ToolPort};
use super::redaction::ExactValueRedactor;
use super::session::{run, run_with_content, Ports, SessionConfig, SessionOutcome};
use super::types::{EffectClass, Message, ProviderOutcome, RaisedClass, Role, ToolCall, ToolRun};

/// The reference sets `autoTurnRetryMax = 1` on every case
/// (`tests/llm/test_upstream_turn_replay.py` production turn-retry policy).
const CASE_AUTO_TURN_RETRY_MAX: u32 = 1;
/// The owner Stop the reference trips (`task['aborted'] = True`).
const OWNER_STOP_REASON: &str = "user_abort";
/// Event types the `events` part keeps (reference `_EVENT_TYPES`).
const EVENT_TYPES: [&str; 11] = [
    "round_start",
    "round_end",
    "model_fallback",
    "tool_start",
    "tool_complete",
    "tool_effect",
    "done",
    "error",
    "aborted",
    "retry_reset",
    "delta_reset",
];

#[derive(Debug, Clone, PartialEq)]
pub struct Unsupported(pub String);

struct ScriptedProvider {
    steps: Vec<Value>,
    cursor: Mutex<usize>,
    /// Every request the dispatch layer issued, with the model it went to.
    log: Mutex<Vec<ProviderRequest>>,
    cancel_at: Option<usize>,
    /// Step index after whose delivery the process dies (`restartAfterRequest`).
    crash_after: Mutex<Option<usize>>,
}

/// The panic payload of a simulated process death.
struct SimulatedCrash;

fn chunks(text: &str) -> Vec<String> {
    let count = text.chars().count();
    let midpoint = count / 2;
    if text.is_empty() {
        return Vec::new();
    }
    if midpoint == 0 {
        return vec![text.to_string()];
    }
    let split = text
        .char_indices()
        .nth(midpoint)
        .map_or(text.len(), |(index, _)| index);
    vec![text[..split].to_string(), text[split..].to_string()]
}

fn text_field(step: &Value, key: &str) -> String {
    step.get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

impl ScriptedProvider {
    fn next(&self, request: &ProviderRequest) -> (usize, Option<Value>) {
        self.log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push(request.clone());
        let mut cursor = self.cursor.lock().unwrap_or_else(|e| e.into_inner());
        let index = *cursor;
        *cursor += 1;
        (index, self.steps.get(index).cloned())
    }

    fn deliver(
        &self,
        text: &str,
        index: usize,
        on_item: &mut dyn FnMut(StreamItem),
        cancel: &CancelToken,
    ) -> Result<String, String> {
        let mut delivered = String::new();
        for chunk in chunks(text) {
            on_item(StreamItem::Text(chunk.clone()));
            delivered.push_str(&chunk);
            if self.cancel_at == Some(index) {
                cancel.cancel(OWNER_STOP_REASON);
                return Err(delivered);
            }
        }
        Ok(delivered)
    }
}

impl ProviderPort for ScriptedProvider {
    fn stream(
        &self,
        request: &ProviderRequest,
        on_item: &mut dyn FnMut(StreamItem),
        cancel: &CancelToken,
    ) -> ProviderOutcome {
        loop {
            let (index, step) = self.next(request);
            let Some(step) = step else {
                return ProviderOutcome::Internal {
                    detail: "ScenarioDriftError: the replay script is exhausted".into(),
                };
            };
            let kind = text_field(&step, "kind");
            let deliver = |text: &str, on_item: &mut dyn FnMut(StreamItem)| {
                self.deliver(text, index, on_item, cancel)
            };
            match kind.as_str() {
                "text" | "tool" => {
                    let text = text_field(&step, "text");
                    if let Err(partial) = deliver(&text, on_item) {
                        return ProviderOutcome::Cancelled {
                            partial_text: partial,
                        };
                    }
                    let tool_calls: Vec<ToolCall> = step
                        .get("calls")
                        .and_then(Value::as_array)
                        .map(|calls| {
                            calls
                                .iter()
                                .map(|call| ToolCall {
                                    id: text_field(call, "id"),
                                    name: text_field(call, "name"),
                                    arguments: text_field(call, "arguments"),
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    for call in &tool_calls {
                        on_item(StreamItem::ToolCall(call.clone()));
                    }
                    let crash = self.crash_after.lock().unwrap_or_else(|e| e.into_inner());
                    if *crash == Some(index) {
                        drop(crash);
                        std::panic::resume_unwind(Box::new(SimulatedCrash));
                    }
                    drop(crash);
                    let finish_reason = if tool_calls.is_empty() {
                        "stop"
                    } else {
                        "tool_calls"
                    };
                    return ProviderOutcome::Completed {
                        text,
                        tool_calls,
                        finish_reason: finish_reason.into(),
                        usage: json!({}),
                    };
                }
                "content_filter" => {
                    let partial = text_field(&step, "partial");
                    if let Err(partial) = deliver(&partial, on_item) {
                        return ProviderOutcome::Cancelled {
                            partial_text: partial,
                        };
                    }
                    return ProviderOutcome::ContentFilter {
                        partial_text: partial,
                        detail: "content_filter".into(),
                    };
                }
                "http_error" => {
                    let status = step.get("status").and_then(Value::as_u64).unwrap_or(500) as u16;
                    let mid_stream = step
                        .get("midStream")
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    if mid_stream {
                        let partial = text_field(&step, "partial");
                        if let Err(partial) = deliver(&partial, on_item) {
                            return ProviderOutcome::Cancelled {
                                partial_text: partial,
                            };
                        }
                        continue;
                    }
                    if status == 429 {
                        continue;
                    }
                    return ProviderOutcome::HttpError {
                        status,
                        detail: format!("HTTP {status} provider error"),
                        partial_text: String::new(),
                    };
                }
                "drop" => {
                    let partial = text_field(&step, "partial");
                    if let Err(partial) = deliver(&partial, on_item) {
                        return ProviderOutcome::Cancelled {
                            partial_text: partial,
                        };
                    }
                    return ProviderOutcome::Dropped {
                        partial_text: partial,
                        detail: "premature_close".into(),
                    };
                }
                other => {
                    return ProviderOutcome::Internal {
                        detail: format!("unknown step kind {other:?}"),
                    }
                }
            }
        }
    }
}

struct ScriptedTools {
    read_only: BTreeMap<String, bool>,
    outcomes: Map<String, Value>,
}

impl ToolPort for ScriptedTools {
    fn effect_class(&self, name: &str) -> EffectClass {
        if self.read_only.get(name).copied().unwrap_or(false) {
            EffectClass::ReadOnly
        } else {
            EffectClass::Mutating
        }
    }

    fn run(&self, call: &ToolCall, _cancel: &CancelToken) -> ToolRun {
        let outcome = self.outcomes.get(&call.id).cloned().unwrap_or(Value::Null);
        let output = text_field(&outcome, "output");
        let raised = |type_name: &str, class: RaisedClass| ToolRun::Raised {
            type_name: type_name.into(),
            detail: output.clone(),
            class,
        };
        match outcome.get("kind").and_then(Value::as_str).unwrap_or("ok") {
            "error" => raised("RuntimeError", RaisedClass::Other),
            "refused_before_entry" => {
                raised("CommandAdmissionRefusal", RaisedClass::AdmissionRefusal)
            }
            "lost_after_entry" => raised(
                "StorageError",
                RaisedClass::Storage {
                    code: "database_busy".into(),
                    transient: true,
                },
            ),
            _ => ToolRun::Finished {
                output,
                is_error: false,
            },
        }
    }
}

struct NullSink;

impl EventSink for NullSink {
    fn publish(&self, _event: &Value) {}
}

fn canonical(value: &Value) -> Vec<u8> {
    fn sorted(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let ordered: BTreeMap<&String, Value> =
                    map.iter().map(|(k, v)| (k, sorted(v))).collect();
                let mut out = Map::new();
                for (key, item) in ordered {
                    out.insert(key.clone(), item);
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_vec(&sorted(value)).expect("JSON values encode")
}

fn message_part(message: &Message) -> Value {
    json!({
        "role": message.role.as_str(),
        "content": message.content,
        "toolCallId": message.tool_call_id,
        "toolCalls": message.tool_calls.iter().map(|call| json!({
            "id": call.id, "name": call.name, "arguments": call.arguments,
        })).collect::<Vec<_>>(),
    })
}

/// What recovery of a crashed attempt leaves before the resume: its
/// committed events plus the recovery's round close, and its tool rounds.
struct Recovered {
    events: Vec<Value>,
    tools: Vec<Value>,
    content: String,
}

/// The durable recovery of a dead attempt (`worker_recovery` +
/// `effect_interruption`): settled tool rounds keep their recorded state; a
/// call announced in the cut round settles `not_started` when no intent was
/// committed (it never ran, so it is repeatable) and `unknown` when its
/// intent was committed without a settlement. The cut round is closed.
fn recover_crashed_attempt(
    store: &MemoryStore,
    provider: &ScriptedProvider,
    restart_after: Option<usize>,
) -> Recovered {
    let mut events = store.events();
    events.push(json!({"type": "round_end", "reason": "recovered"}));
    let settlements = store.settlements();
    let intents = store.intents();
    let mut tools: Vec<Value> = settlements
        .iter()
        .map(|s| {
            json!({
                "id": s["toolCallId"], "name": s["toolName"],
                "effect": s["executionEffect"]["state"], "isError": s["isError"],
                "result": s["toolContent"],
            })
        })
        .collect();
    let cut_step = restart_after
        .and_then(|n| n.checked_sub(1))
        .and_then(|index| provider.steps.get(index));
    let settled = |id: &str| settlements.iter().any(|s| s["toolCallId"] == id);
    let intended = |id: &str| intents.iter().any(|s| s["toolCallId"] == id);
    for call in cut_step
        .and_then(|step| step.get("calls"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let id = text_field(call, "id");
        if settled(&id) {
            continue;
        }
        let effect = if intended(&id) {
            "unknown"
        } else {
            "not_started"
        };
        tools.push(json!({
            "id": id, "name": text_field(call, "name"), "effect": effect,
            "isError": true, "result": "",
        }));
    }
    // Presentation commits independently of the model-history checkpoint.
    // A reply need not have been adopted into history for its deltas to survive.
    let mut content = String::new();
    for event in &events {
        match event["type"].as_str() {
            Some("delta") => content.push_str(&text_field(event, "content")),
            Some("delta_reset" | "retry_reset") => content.clear(),
            _ => {}
        }
    }
    Recovered {
        events,
        tools,
        content,
    }
}

/// The resume view: the latest checkpoint's protocol-valid history.
fn resume_messages(store: &MemoryStore) -> Option<Vec<Message>> {
    let checkpoint = store.checkpoints().pop()?;
    let rows = checkpoint.get("postMessages")?.as_array()?;
    rows.iter()
        .map(|row| {
            let role = match row.get("role")?.as_str()? {
                "system" => Role::System,
                "user" => Role::User,
                "assistant" => Role::Assistant,
                "tool" => Role::Tool,
                _ => return None,
            };
            let tool_calls = row
                .get("tool_calls")
                .and_then(Value::as_array)
                .map(|calls| {
                    calls
                        .iter()
                        .map(|call| ToolCall {
                            id: text_field(call, "id"),
                            name: text_field(&call["function"], "name"),
                            arguments: text_field(&call["function"], "arguments"),
                        })
                        .collect()
                })
                .unwrap_or_default();
            Some(Message {
                role,
                content: text_field(row, "content"),
                tool_calls,
                tool_call_id: row
                    .get("tool_call_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            })
        })
        .collect()
}

/// Reference rule 1: minted operation ids become `op-<toolCallId>`.
fn normaliser(outcome: &SessionOutcome, store: &MemoryStore) -> impl Fn(&str) -> String {
    let committed: Vec<(String, String)> = store
        .intents()
        .iter()
        .map(|intent| {
            (
                text_field(&intent["executionEffect"], "operationId"),
                format!("op-{}", text_field(intent, "toolCallId")),
            )
        })
        .collect();
    let pairs: Vec<(String, String)> = outcome
        .tools
        .iter()
        .filter_map(|record| {
            record.effect.as_ref().map(|effect| {
                (
                    effect.operation_id.clone(),
                    format!("op-{}", record.call.id),
                )
            })
        })
        .chain(committed)
        .collect();
    move |text: &str| {
        pairs
            .iter()
            .fold(text.to_string(), |acc, (from, to)| acc.replace(from, to))
    }
}

pub fn engine_session(input: &Value) -> Result<Vec<(String, Vec<u8>)>, Unsupported> {
    let restart_after = input
        .get("restartAfterRequest")
        .and_then(Value::as_u64)
        .map(|n| n as usize);
    let storage_failure = input
        .get("storageFailure")
        .and_then(|v| v.get("op"))
        .and_then(Value::as_str);
    if storage_failure.is_some_and(|op| op != "append_event") {
        return Err(Unsupported(format!(
            "storageFailure {storage_failure:?}: only append_event is modelled (the reference \
             answers the same)"
        )));
    }
    let model = text_field(input, "model");
    let fallback = input
        .get("fallbackModel")
        .and_then(Value::as_str)
        .map(str::to_string);
    let steps = input
        .get("requests")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if steps.is_empty() {
        return Err(Unsupported("requests is empty".into()));
    }
    let cancel_at = input
        .get("cancelAt")
        .and_then(|c| c.get("request"))
        .and_then(Value::as_u64)
        .map(|n| n as usize);
    let provider = ScriptedProvider {
        steps,
        cursor: Mutex::new(0),
        log: Mutex::new(Vec::new()),
        cancel_at,
        crash_after: Mutex::new(restart_after.and_then(|n| n.checked_sub(1))),
    };
    let tools = ScriptedTools {
        read_only: input
            .get("tools")
            .and_then(Value::as_array)
            .map(|tools| {
                tools
                    .iter()
                    .map(|tool| {
                        (
                            text_field(tool, "name"),
                            tool.get("readOnly")
                                .and_then(Value::as_bool)
                                .unwrap_or(false),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default(),
        outcomes: input
            .get("toolOutcomes")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default(),
    };
    let store = MemoryStore::new();
    if storage_failure.is_some() {
        // Every non-terminal event commit fails; the terminal still commits.
        store.fail_next(
            OP_EVENTS,
            u32::MAX,
            StoreError {
                code: "storage_unavailable".into(),
                message: "injected append_event failure".into(),
                transient: false,
                not_dispatched: false,
            },
        );
    }
    let redactor = ExactValueRedactor::new([]);
    let pause = |_seconds: f64| {};
    let ports = Ports {
        provider: &provider,
        tools: &tools,
        store: &store,
        sink: &NullSink,
        redactor: &redactor,
        pause: &pause,
    };
    let config = SessionConfig {
        task_id: "engine-session".into(),
        attempt_id: "engine-session-attempt".into(),
        turn_id: "engine-session-turn".into(),
        owner_user_id: 1,
        primary_model: model,
        fallback_model: fallback,
        resumed_accepted_model: None,
        auto_turn_retry_max: CASE_AUTO_TURN_RETRY_MAX,
    };
    let cancel = CancelToken::new();
    let messages = vec![Message::text(Role::User, text_field(input, "user"))];
    let first = catch_unwind(AssertUnwindSafe(|| {
        run(&config, messages.clone(), &ports, &cancel)
    }));
    let (outcome, recovered) = match first {
        Ok(outcome) => (outcome, None),
        Err(payload) if payload.is::<SimulatedCrash>() => {
            *provider
                .crash_after
                .lock()
                .unwrap_or_else(|e| e.into_inner()) = None;
            let recovered = recover_crashed_attempt(&store, &provider, restart_after);
            let resumed = resume_messages(&store).unwrap_or(messages);
            let resume_config = SessionConfig {
                attempt_id: "engine-session-attempt-resumed".into(),
                resumed_accepted_model: Some(config.primary_model.clone()),
                ..config
            };
            let outcome = run_with_content(
                &resume_config,
                resumed,
                recovered.content.clone(),
                &ports,
                &CancelToken::new(),
            );
            (outcome, Some(recovered))
        }
        Err(payload) => std::panic::resume_unwind(payload),
    };
    let normalise = normaliser(&outcome, &store);

    let status = if cancel.is_cancelled() {
        "aborted"
    } else if outcome.error_kind.is_some() {
        "error"
    } else {
        "done"
    };
    let outcome_part = json!({
        "status": status,
        "content": normalise(&outcome.content),
        "finishReason": outcome.finish_reason,
        "errorKind": outcome.error_kind,
    });
    let requests_part: Vec<Value> = provider
        .log
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .iter()
        .map(|request| {
            let messages: Vec<Value> = request
                .messages
                .iter()
                .filter(|m| m.role != Role::System)
                .map(|m| {
                    let mut part = message_part(m);
                    part["content"] = Value::from(normalise(&m.content));
                    part
                })
                .collect();
            json!({"model": format!("{}-wire", request.model), "messages": messages})
        })
        .collect();
    let mut tools_part: Vec<Value> = recovered
        .as_ref()
        .map(|r| r.tools.clone())
        .unwrap_or_default();
    tools_part.extend(outcome.tools.iter().map(|record| {
        json!({
            "id": record.call.id,
            "name": record.call.name,
            "effect": record.effect.as_ref().map(|e| e.state.as_str()),
            "isError": record.is_error,
            "result": normalise(&record.result),
        })
    }));
    let fallbacks_part: Vec<Value> = outcome
        .fallbacks
        .iter()
        .map(|f| json!({"from": f.from, "to": f.to, "kind": f.kind}))
        .collect();
    let events_part: Vec<Value> = recovered
        .iter()
        .flat_map(|r| r.events.iter())
        .chain(outcome.events.iter())
        .filter_map(|event| event.get("type").and_then(Value::as_str))
        .filter(|kind| EVENT_TYPES.contains(kind))
        .map(Value::from)
        .collect();
    Ok(vec![
        ("outcome".into(), canonical(&outcome_part)),
        ("requests".into(), canonical(&Value::Array(requests_part))),
        ("tools".into(), canonical(&Value::Array(tools_part))),
        ("fallbacks".into(), canonical(&Value::Array(fallbacks_part))),
        ("events".into(), canonical(&Value::Array(events_part))),
    ])
}

/// The harness line protocol (`tests/rust_parity/runner.py`): one request per
/// line in, one answer per line out, in order.
pub fn serve_lines(input: impl BufRead, mut output: impl Write) -> std::io::Result<()> {
    for line in input.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let request: Value = match serde_json::from_str(&line) {
            Ok(value) => value,
            Err(error) => {
                writeln!(
                    output,
                    "{}",
                    json!({"id": Value::Null, "error": error.to_string()})
                )?;
                continue;
            }
        };
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        let answer = if request.get("boundary").and_then(Value::as_str) != Some("engine_session") {
            json!({"id": id, "unsupported": format!("tofu-engine answers only engine_session, not {}", request.get("boundary").unwrap_or(&Value::Null))})
        } else if request.get("process").is_some() {
            json!({"id": id, "unsupported": "real process recovery requires the accepted-Attempt storage-backed composition; MemoryStore replay is not crash evidence"})
        } else {
            match engine_session(request.get("input").unwrap_or(&Value::Null)) {
                Ok(parts) => {
                    json!({"id": id, "parts": parts.into_iter().map(|(name, bytes)| json!({
                    "name": name,
                    "b64": base64::engine::general_purpose::STANDARD.encode(bytes),
                })).collect::<Vec<_>>()})
                }
                Err(Unsupported(reason)) => json!({"id": id, "unsupported": reason}),
            }
        };
        writeln!(output, "{answer}")?;
        output.flush()?;
    }
    Ok(())
}
