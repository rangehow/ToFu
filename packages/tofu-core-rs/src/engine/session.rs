//! The agent loop of one Turn attempt, composed from the policy modules.
//!
//! Responsibility: drive rounds until the attempt settles exactly once.
//! Each round: take committed steers at the boundary, request the model on
//! the model `fallback` selects, decide the outcome (accept, resend, fall
//! back, re-run the turn, fail), run the requested tool calls in parallel
//! behind their effect intents, checkpoint the protocol-valid history and
//! commit the round's events before publishing them. A Stop is accepted by
//! the cancel token and settled here; a drain/restart stop saves the
//! checkpoint first and settles paused. Python owners replaced:
//! `lib/tasks_pkg/orchestrator/_root_agent_loop.py`, `_tool_dispatch_round.py`,
//! `_finalize.py`, `lib/tasks_pkg/turn_retry.py`, `lib/tasks_pkg/executor/_execute.py`.
//!
//! Observable rules pinned against the Python loop (`tests/rust_parity_engine`):
//! the terminal event is always `done` (an error settles `done` with
//! `finishReason=error` and an error envelope); a stream that ends in tool
//! calls announces `tool_start` while streaming (first attempt of a round)
//! and then resets the live text (`delta_reset`); a refusal's partial text is
//! reset before `model_fallback`; a dropped stream resets, closes the round
//! and resends the identical request in a new round; a whole-turn re-run
//! (`turn_retry`) closes the round and emits `retry_reset`.
//!
//! Entry point: `run(&SessionConfig, Vec<Message>, &Ports, &CancelToken)`.
//! The loop is synchronous and deterministic: time and randomness enter only
//! through ports (`Ports::pause`), so a scripted session replays exactly.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::cancel::CancelToken;
use super::checkpoint::{build_checkpoint, settle_plan, CheckpointInput};
use super::events::Emitter;
use super::fallback::{auto_retry_turn, FailureKind, FallbackDecision, FallbackState};
use super::intent_stall::{
    delivered_answer, paragraph_separator, stall_decision, StallDecision, StallInputs,
    ToolResultMeta, ToolRoundOutcome,
};
use super::ports::{
    DurableStore, EventSink, ProviderPort, ProviderRequest, StoreError, StreamItem, ToolPort,
};
use super::redaction::{redact_tool_content, redact_tool_run, SettlementRedactor};
use super::settlement::{stop_done_event, TerminalCandidate, TerminalKind, TurnSettlement};
use super::steer::{delivery_record, inject_steers, take_steers, SteerLedger};
use super::tool_effect::{
    decide_entry, plan_effect, project_failure, settle_effect, EffectScope, EntryDecision,
    EntryRefusal, ExecutionEffect, FailureContext, RecoveryPolicy, ToolFailure, TypedToolError,
    INTENT_NOT_ACKNOWLEDGED,
};
use super::types::{Message, ProviderOutcome, RaisedClass, Role, ToolCall, ToolRun};
use crate::pyjson::{dumps_spaced, py_prefix};

/// `turn_retry._AUTO_TURN_RETRY_MAX` is the default of the per-task
/// `autoTurnRetryMax` config; the session takes the resolved value.
pub use super::fallback::AUTO_TURN_RETRY_MAX;

/// `_execute._argument_preview`: `json.dumps(args)[:300]`.
const ARGUMENT_PREVIEW_CHARS: usize = 300;

pub struct SessionConfig {
    pub task_id: String,
    pub attempt_id: String,
    pub turn_id: String,
    pub owner_user_id: u64,
    pub primary_model: String,
    /// The configured fallback model; `None` (or equal to the primary) means
    /// a refusal has nowhere to go and settles as a model refusal.
    pub fallback_model: Option<String>,
    /// A resumed attempt runs on the accepted model, never the last fallback.
    pub resumed_accepted_model: Option<String>,
    /// The task's `autoTurnRetryMax` (default `AUTO_TURN_RETRY_MAX`).
    pub auto_turn_retry_max: u32,
}

pub struct Ports<'a> {
    pub provider: &'a dyn ProviderPort,
    pub tools: &'a dyn ToolPort,
    pub store: &'a dyn DurableStore,
    pub sink: &'a dyn EventSink,
    pub redactor: &'a dyn SettlementRedactor,
    /// Waits `seconds` (provider backoff). The host sleeps; replay records.
    pub pause: &'a (dyn Fn(f64) + Sync),
}

/// One settled tool call as the loop recorded it.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolRecord {
    /// The model round that requested the call (its batch identity).
    pub round: u32,
    pub call: ToolCall,
    pub effect: Option<ExecutionEffect>,
    pub is_error: bool,
    pub result: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FallbackRecord {
    pub from: String,
    pub to: String,
    pub kind: String,
}

/// Everything observable about one run: the settled terminal and the trace
/// the parity boundary projects. `events` is in emission order (intents
/// included) and holds only events whose commit succeeded or is pending
/// with the terminal.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionOutcome {
    pub kind: TerminalKind,
    /// `kind` is a verdict, not proof of durability; only a committed terminal
    /// authorizes completion delivery or successor dispatch.
    pub terminal_committed: bool,
    pub settlement_error: Option<StoreError>,
    pub pending_terminal: Option<TerminalCandidate>,
    pub pending_steer_acks: Vec<Value>,
    pub pending_steer_requeues: Vec<Value>,
    pub control_errors: Vec<StoreError>,
    pub finish_reason: Option<String>,
    pub content: String,
    pub error_kind: Option<String>,
    pub requests: Vec<ProviderRequest>,
    pub tools: Vec<ToolRecord>,
    pub fallbacks: Vec<FallbackRecord>,
    pub billing_labels: Vec<String>,
    pub events: Vec<Value>,
    pub messages: Vec<Message>,
}

enum RoundEnd {
    Answer {
        text: String,
        tool_calls: Vec<ToolCall>,
        finish_reason: String,
    },
    /// The stream ended without a provider finish: resend the identical
    /// request in a new round.
    Resend,
    /// A whole-turn re-run (`turn_retry`) after `backoff_s`.
    RetryTurn {
        backoff_s: f64,
        kind: String,
    },
    Failed {
        finish_reason: String,
        kind: String,
        detail: String,
    },
    Stopped {
        partial: String,
    },
}

struct Loop<'a> {
    config: &'a SessionConfig,
    ports: &'a Ports<'a>,
    cancel: &'a CancelToken,
    emitter: Emitter,
    fallback: FallbackState,
    ledger: SteerLedger,
    settlement: TurnSettlement,
    pending_events: Vec<Value>,
    turn_retries: u32,
    stall_nudges: u32,
    /// Recovered presentation and answers vetoed by an intent-stall nudge.
    live_text: String,
    trace: SessionOutcome,
}

pub fn run(
    config: &SessionConfig,
    messages: Vec<Message>,
    ports: &Ports,
    cancel: &CancelToken,
) -> SessionOutcome {
    run_with_content(config, messages, String::new(), ports, cancel)
}

pub(crate) fn run_with_content(
    config: &SessionConfig,
    messages: Vec<Message>,
    content: String,
    ports: &Ports,
    cancel: &CancelToken,
) -> SessionOutcome {
    let fallback = FallbackState::new(
        config
            .resumed_accepted_model
            .as_ref()
            .unwrap_or(&config.primary_model)
            .clone(),
        config.fallback_model.clone().unwrap_or_default(),
    );
    let mut state = Loop {
        config,
        ports,
        cancel,
        emitter: Emitter::new(&config.task_id, &config.attempt_id),
        fallback,
        ledger: SteerLedger::default(),
        settlement: TurnSettlement::new(),
        pending_events: Vec::new(),
        turn_retries: 0,
        stall_nudges: 0,
        live_text: content.clone(),
        trace: SessionOutcome {
            kind: TerminalKind::Error,
            terminal_committed: false,
            settlement_error: None,
            pending_terminal: None,
            pending_steer_acks: Vec::new(),
            pending_steer_requeues: Vec::new(),
            control_errors: Vec::new(),
            finish_reason: None,
            content,
            error_kind: None,
            requests: Vec::new(),
            tools: Vec::new(),
            fallbacks: Vec::new(),
            billing_labels: Vec::new(),
            events: Vec::new(),
            messages,
        },
    };
    state.drive();
    state.trace
}

impl Loop<'_> {
    fn drive(&mut self) {
        let mut round: u32 = 0;
        loop {
            round += 1;
            if self.cancel.is_cancelled() {
                return self.settle_stopped("");
            }
            if let Err(error) = self.inject_round_steers() {
                return self.settle_storage_failure(&error);
            }
            self.emit("round_start", json!({"roundNum": round}));
            match self.request_round(round) {
                RoundEnd::Stopped { partial } => {
                    self.trace.content = format!("{}{partial}", self.live_text);
                    return self.settle_stopped("");
                }
                RoundEnd::Failed {
                    finish_reason,
                    kind,
                    detail,
                } => return self.settle_failed(round, &finish_reason, &kind, &detail),
                RoundEnd::Resend => {
                    self.emit("round_end", json!({"roundNum": round, "reason": "resend"}));
                }
                RoundEnd::RetryTurn { backoff_s, kind } => {
                    self.emit("round_end", json!({"roundNum": round, "reason": "error"}));
                    self.emit(
                        "retry_reset",
                        json!({"attempt": self.turn_retries, "max": self.config.auto_turn_retry_max,
                               "kind": kind, "contentEpoch": round}),
                    );
                    (self.ports.pause)(backoff_s);
                }
                RoundEnd::Answer {
                    text,
                    tool_calls,
                    finish_reason,
                } => {
                    let confirmed = self.ledger.confirm_consumed();
                    let records: Vec<_> = confirmed
                        .iter()
                        .map(|item| delivery_record(item, round, "delivered"))
                        .collect();
                    if !records.is_empty() {
                        if let Err(error) =
                            self.retry_transient(|store| store.confirm_steers(&records))
                        {
                            self.trace.control_errors.push(error);
                            self.trace.pending_steer_acks.extend(records);
                        }
                    }
                    if tool_calls.is_empty() {
                        let rounds = self.tool_round_outcomes();
                        let decision = stall_decision(&StallInputs {
                            tool_rounds: &rounds,
                            answer_text: &text,
                            answer_tool_calls: &[],
                            pending_human_guidance: false,
                            nudges_used: self.stall_nudges,
                            round_num: i64::from(round),
                        });
                        if let StallDecision::Nudge(nudge) = decision {
                            self.stall_nudges += 1;
                            self.trace.messages.push(nudge.assistant);
                            self.trace.messages.push(nudge.nudge);
                            self.live_text.push_str(&text);
                            self.live_text
                                .push_str(paragraph_separator(&self.live_text));
                            self.trace.content = self.live_text.clone();
                            self.emit(
                                "round_end",
                                json!({"roundNum": round, "reason": "intent_stall"}),
                            );
                        } else {
                            self.trace.messages.push(Message {
                                role: Role::Assistant,
                                content: text.trim_end().to_string(),
                                tool_calls: Vec::new(),
                                tool_call_id: None,
                            });
                            let answer = format!("{}{text}", self.live_text);
                            self.trace.content = delivered_answer(&answer).into_owned();
                            return self.settle_done(round, &finish_reason);
                        }
                        self.flush_events();
                        continue;
                    }
                    self.trace.messages.push(Message {
                        role: Role::Assistant,
                        content: text.trim_end().to_string(),
                        tool_calls: tool_calls.clone(),
                        tool_call_id: None,
                    });
                    self.run_tools(round, &tool_calls);
                    self.checkpoint(None);
                    self.emit(
                        "round_end",
                        json!({"roundNum": round, "reason": "tool_calls"}),
                    );
                }
            }
            self.flush_events();
        }
    }

    fn inject_round_steers(&mut self) -> Result<(), StoreError> {
        let records = self.retry_transient(|store| store.take_steers())?;
        let items = take_steers(&records, Vec::new(), &self.ledger);
        inject_steers(&mut self.trace.messages, items, &mut self.ledger);
        Ok(())
    }

    fn request_round(&mut self, round: u32) -> RoundEnd {
        let python_round = round - 1;
        // The refusal handler resends the refused request on the fallback
        // itself; only a later request probes the primary.
        let mut resend_on_fallback = false;
        for attempt in 0.. {
            let in_refused_tool_loop =
                resend_on_fallback || self.fallback.take_refused_tool_loop(&self.trace.messages);
            if let Some(event) = self.fallback.switch_back_event(python_round) {
                if self.fallback.model_for_request(in_refused_tool_loop) == event.fallback_model {
                    self.publish_fallback(event);
                }
            }
            let model = self
                .fallback
                .begin_request(in_refused_tool_loop)
                .to_string();
            let request = ProviderRequest {
                model,
                messages: self.trace.messages.clone(),
                round,
            };
            self.trace.requests.push(request.clone());
            // Everything emitted so far is durable before the paid request.
            self.flush_events();
            let mut streamed = String::new();
            let mut announced: Vec<ToolCall> = Vec::new();
            let provider = self.ports.provider;
            let cancel = self.cancel;
            let outcome = provider.stream(
                &request,
                &mut |item| match item {
                    _ if cancel.is_cancelled() => {}
                    StreamItem::Text(text) => {
                        streamed.push_str(&text);
                        self.emit("delta", json!({"content": text}));
                        self.flush_events();
                    }
                    StreamItem::ToolCall(call) => {
                        // A round's first attempt announces each call as it
                        // streams, durably, before the stream ends.
                        if attempt == 0 {
                            self.emit_tool_start(round, &call);
                            self.flush_events();
                        }
                        announced.push(call);
                    }
                },
                cancel,
            );
            if self.cancel.is_cancelled() || matches!(outcome, ProviderOutcome::Cancelled { .. }) {
                return RoundEnd::Stopped { partial: streamed };
            }
            if let ProviderOutcome::Dropped { .. } = outcome {
                self.reset_live_text(round, &streamed);
                return RoundEnd::Resend;
            }
            match self.fallback.on_outcome(python_round, &outcome) {
                FallbackDecision::Accept { .. } => {
                    let ProviderOutcome::Completed {
                        text,
                        tool_calls,
                        finish_reason,
                        ..
                    } = outcome
                    else {
                        unreachable!("fallback accepts only completed outcomes")
                    };
                    if !tool_calls.is_empty() {
                        let streamed_live = attempt == 0 && !announced.is_empty();
                        self.announce_tools(round, &tool_calls, attempt == 0, streamed_live);
                    }
                    return RoundEnd::Answer {
                        text,
                        tool_calls,
                        finish_reason,
                    };
                }
                FallbackDecision::RetrySameModel { backoff_s, .. } => {
                    self.reset_live_text(round, &streamed);
                    (self.ports.pause)(backoff_s);
                }
                FallbackDecision::RetryOnFallback {
                    event,
                    billing_label,
                    discard_partial,
                } => {
                    if discard_partial {
                        self.reset_live_text(round, &streamed);
                    }
                    self.trace.billing_labels.push(billing_label);
                    let refusal = event.fallback_kind == super::fallback::CONTENT_FILTER_KIND;
                    self.publish_fallback(*event);
                    if refusal {
                        self.fallback.record_refused_history(&self.trace.messages);
                    }
                    resend_on_fallback = true;
                }
                FallbackDecision::Fail { kind } => {
                    if kind == FailureKind::ModelRefusal {
                        self.reset_live_text(round, &streamed);
                    }
                    let (finish_reason, kind, detail) = failure_text(&kind, &outcome);
                    if let Some(backoff_s) = self.turn_retry(&kind) {
                        return RoundEnd::RetryTurn { backoff_s, kind };
                    }
                    return RoundEnd::Failed {
                        finish_reason,
                        kind,
                        detail,
                    };
                }
            }
        }
        unreachable!("the attempt loop returns")
    }

    fn turn_retry(&mut self, kind: &str) -> Option<f64> {
        if self.turn_retries >= self.config.auto_turn_retry_max {
            return None;
        }
        let backoff = auto_retry_turn(kind, self.turn_retries, false, false)?;
        self.turn_retries += 1;
        Some(backoff)
    }

    /// Streamed calls are announced as they arrive on a round's first
    /// attempt; a retried attempt announces them after its reset.
    fn announce_tools(
        &mut self,
        round: u32,
        calls: &[ToolCall],
        while_streaming: bool,
        already_announced: bool,
    ) {
        if !while_streaming {
            self.emit_reset(round);
        }
        if !already_announced {
            for call in calls {
                self.emit_tool_start(round, call);
            }
        }
        if while_streaming {
            self.emit_reset(round);
        }
    }

    fn emit_tool_start(&mut self, round: u32, call: &ToolCall) {
        self.emit(
            "tool_start",
            json!({"roundNum": round, "toolName": call.name, "query": call.arguments}),
        );
    }

    fn reset_live_text(&mut self, round: u32, streamed: &str) {
        if !streamed.is_empty() {
            self.emit_reset(round);
        }
    }

    fn emit_reset(&mut self, round: u32) {
        self.emit(
            "delta_reset",
            json!({"roundNum": round, "discard": true, "contentEpoch": round}),
        );
    }

    fn publish_fallback(&mut self, event: tofu_contracts::generated_events_v1::ModelFallbackEvent) {
        self.trace.fallbacks.push(FallbackRecord {
            from: event.fallback_from.clone(),
            to: event.fallback_model.clone(),
            kind: event.fallback_kind.clone(),
        });
        let fields = serde_json::to_value(&event).expect("generated events serialize");
        self.emit("model_fallback", fields);
    }

    /// Plan every call, commit the intents in call order, run the admitted
    /// calls at once (one thread per call, no pool), then settle in order.
    fn run_tools(&mut self, round: u32, calls: &[ToolCall]) {
        let mut admitted: Vec<(usize, ExecutionEffect)> = Vec::new();
        let mut settled: Vec<Option<(ExecutionEffect, ToolRun)>> = vec![None; calls.len()];
        for (index, call) in calls.iter().enumerate() {
            let policy = RecoveryPolicy::from(self.ports.tools.effect_class(&call.name));
            let planned = plan_effect(
                call,
                i64::from(round),
                policy,
                EffectScope::Attempt,
                operation_id(&self.config.attempt_id, round, index, &call.id),
            );
            // Events emitted before an effect enters are durable before it.
            self.flush_events();
            let ack = planned
                .intent
                .as_ref()
                .map(|intent| self.retry_transient(|store| store.commit_tool_intent(intent)));
            if let (Some(intent), Some(Ok(()))) = (&planned.intent, &ack) {
                self.trace.events.push(intent.clone());
                self.ports.sink.publish(intent);
            }
            match decide_entry(planned, ack, self.cancel.is_cancelled()) {
                EntryDecision::Enter(effect) => admitted.push((index, effect)),
                EntryDecision::Refused { effect, reason } => {
                    let reason = match reason {
                        EntryRefusal::IntentNotAcknowledged(_) => INTENT_NOT_ACKNOWLEDGED,
                        EntryRefusal::Cancelled => "cancelled before entry",
                    };
                    settled[index] = Some((
                        effect,
                        ToolRun::RefusedBeforeEntry {
                            reason: reason.to_string(),
                        },
                    ));
                }
            }
        }
        let tools = self.ports.tools;
        let cancel = self.cancel;
        let runs: Vec<(usize, ExecutionEffect, ToolRun)> = std::thread::scope(|scope| {
            let handles: Vec<_> = admitted
                .into_iter()
                .map(|(index, effect)| {
                    let call = &calls[index];
                    scope.spawn(move || {
                        let run = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            tools.run(call, cancel)
                        }))
                        .unwrap_or_else(|_| ToolRun::LostAfterEntry {
                            detail: "tool executor panicked after entry".into(),
                        });
                        (index, effect, run)
                    })
                })
                .collect();
            handles
                .into_iter()
                .zip(0..)
                .map(|(handle, _)| match handle.join() {
                    Ok(run) => run,
                    Err(panic) => std::panic::resume_unwind(panic),
                })
                .collect()
        });
        for (index, effect, run) in runs {
            settled[index] = Some((effect, run));
        }
        for (call, slot) in calls.iter().zip(settled) {
            let (effect, run) = slot.expect("every call is planned");
            self.settle_tool(round, call, effect, run);
        }
    }

    fn settle_tool(&mut self, round: u32, call: &ToolCall, effect: ExecutionEffect, run: ToolRun) {
        let owner = self.config.owner_user_id;
        let run = redact_tool_run(self.ports.redactor, owner, &run);
        let mut effect = settle_effect(&effect, &run);
        let (result, is_error) = match &run {
            ToolRun::Finished { output, is_error } if output.trim().is_empty() => (
                format!("({} completed with no output)", call.name),
                *is_error,
            ),
            ToolRun::Finished { output, is_error } => (output.clone(), *is_error),
            ToolRun::RefusedBeforeEntry { reason } => (reason.clone(), true),
            ToolRun::Cancelled { .. } => {
                let reason = self.cancel.reason().unwrap_or_default();
                let paused = super::abort::is_pause(&reason)
                    .then(|| super::abort::system_abort_label(&reason));
                let projected = project_failure(
                    &FailureContext {
                        tool_name: &call.name,
                        current_policy: effect.recovery,
                        paused_by: paused.as_deref(),
                        actuals_complete: true,
                        message: None,
                    },
                    Some(&effect),
                    &ToolFailure {
                        kind: super::tool_effect::FailureKind::Cancelled,
                        type_name: "Cancelled",
                        detail: &"tool cancellation",
                    },
                );
                if let Some(settled) = projected.effect {
                    effect = settled;
                }
                (model_error_text(&projected.error), true)
            }
            ToolRun::LostAfterEntry { detail } => {
                let projected =
                    self.project_raised(call, &effect, "LostExecutor", detail, &RaisedClass::Other);
                effect = projected.0;
                (projected.1, true)
            }
            ToolRun::Raised {
                type_name,
                detail,
                class,
            } => {
                let projected = self.project_raised(call, &effect, type_name, detail, class);
                effect = projected.0;
                (projected.1, true)
            }
        };
        let result = redact_tool_content(self.ports.redactor, owner, &result);
        if effect.recovery != RecoveryPolicy::ReadOnly {
            let settlement = json!({
                "type": "tool_effect", "roundNum": round, "toolName": call.name,
                "toolCallId": call.id, "executionEffect": effect.to_json(),
                "toolContent": result, "isError": is_error,
            });
            // The settled state lands on the tool round; a lost commit leaves
            // the durable `entered` intent, which recovery reads as uncertain.
            let _ = self.retry_transient(|store| store.commit_tool_settlement(&settlement));
        }
        self.emit(
            "tool_complete",
            json!({"roundNum": round, "toolCallId": call.id, "toolName": call.name,
                   "toolContent": result, "isError": is_error}),
        );
        self.trace
            .messages
            .push(Message::tool_result(&call.id, result.clone()));
        self.trace.tools.push(ToolRecord {
            round,
            call: call.clone(),
            effect: Some(effect),
            is_error,
            result,
        });
    }

    /// `_handler_error_result` + `tool_exception_result`: the executor's
    /// message, the effect class and the typed envelope the model reads.
    fn project_raised(
        &self,
        call: &ToolCall,
        effect: &ExecutionEffect,
        type_name: &str,
        detail: &str,
        class: &RaisedClass,
    ) -> (ExecutionEffect, String) {
        let arguments = serde_json::from_str::<Value>(&call.arguments)
            .map(|value| dumps_spaced(&value))
            .unwrap_or_else(|_| call.arguments.clone());
        let message = format!(
            "Error: tool \"{}\" execution failed with {type_name}: {detail}. Arguments received: {}",
            call.name,
            py_prefix(&arguments, ARGUMENT_PREVIEW_CHARS)
        );
        let kind = match class {
            RaisedClass::AdmissionRefusal => super::tool_effect::FailureKind::AdmissionRefusal,
            RaisedClass::Storage { code, transient } => super::tool_effect::FailureKind::Storage {
                code: code.clone(),
                transient: *transient,
                retry_after_ms: None,
            },
            RaisedClass::Other => super::tool_effect::FailureKind::Other,
        };
        let context = FailureContext {
            tool_name: &call.name,
            current_policy: effect.recovery,
            paused_by: None,
            actuals_complete: true,
            message: Some(&message),
        };
        let failure = ToolFailure {
            kind,
            type_name,
            detail: &detail,
        };
        let projected = project_failure(&context, Some(effect), &failure);
        let effect = projected.effect.unwrap_or_else(|| effect.clone());
        (effect, model_error_text(&projected.error))
    }

    /// The Turn's tool rounds as the intent-stall classifier reads them.
    fn tool_round_outcomes(&self) -> Vec<ToolRoundOutcome> {
        self.trace
            .tools
            .iter()
            .map(|record| {
                let status = if record.is_error { "error" } else { "done" };
                ToolRoundOutcome {
                    tool_name: record.call.name.clone(),
                    batch: u64::from(record.round),
                    round_num: Some(i64::from(record.round)),
                    status: status.into(),
                    rejected: None,
                    rejection_kind: None,
                    results: vec![ToolResultMeta {
                        not_run: false,
                        badge: String::new(),
                        result_type: if record.is_error {
                            "error".into()
                        } else {
                            String::new()
                        },
                        exit_code: None,
                        rejected: false,
                        reason: record.result.clone(),
                    }],
                }
            })
            .collect()
    }

    fn checkpoint(&mut self, abort_reason: Option<&str>) {
        let input = CheckpointInput {
            turn_id: &self.config.turn_id,
            attempt_id: &self.config.attempt_id,
            messages: &self.trace.messages,
            includes_current_run: true,
            unverified_inbox_delivery: !self.ledger.pending().is_empty(),
            repeatable_cut_call_ids: &[],
            content: &self.trace.content,
            thinking: "",
        };
        if let Ok(checkpoint) = build_checkpoint(&input, abort_reason) {
            // A checkpoint is recovery evidence, not the Turn's result: a
            // failed save keeps the previous one and the round continues.
            let _ = self.retry_transient(|store| store.save_checkpoint(&checkpoint));
        }
    }

    fn emit(&mut self, event_type: &str, fields: Value) {
        let event = self
            .emitter
            .emit(event_type, fields)
            .unwrap_or_else(|refused| panic!("engine built a non-contract event: {refused:?}"));
        self.trace.events.push(event.clone());
        self.pending_events.push(event);
    }

    /// Commit pending events, then publish them in commit order. A batch the
    /// authority refuses (non-transient) is not published and does not stop
    /// the Turn: the terminal carries the verdict and commits on its own, as
    /// Python's `record_task_event` failures do.
    fn flush_events(&mut self) {
        if self.pending_events.is_empty() {
            return;
        }
        let events = std::mem::take(&mut self.pending_events);
        if self
            .retry_transient(|store| store.record_events(&events))
            .is_ok()
        {
            for event in &events {
                self.ports.sink.publish(event);
            }
        }
    }

    /// Replay-safe storage calls wait out transient errors while the
    /// authority is alive (`docs/STORAGE.md` "Client retry contract"); the
    /// storage client owns the waiting, the loop only re-issues.
    fn retry_transient<T>(
        &self,
        mut call: impl FnMut(&dyn DurableStore) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        loop {
            match call(self.ports.store) {
                Err(error) if error.transient && !self.cancel.is_cancelled() => continue,
                other => return other,
            }
        }
    }

    fn projection(&self, status: &str) -> Value {
        json!({
            "turnId": self.config.turn_id,
            "attemptId": self.config.attempt_id,
            "status": status,
            "content": self.trace.content,
        })
    }

    fn settle_done(&mut self, round: u32, finish_reason: &str) {
        self.emit(
            "round_end",
            json!({"roundNum": round, "reason": "complete"}),
        );
        self.trace.finish_reason = Some(finish_reason.to_string());
        let fields =
            json!({"finishReason": finish_reason, "model": self.fallback.model_for_request(false)});
        let done = serde_json::from_value(fields).expect("DoneEvent fields");
        let projection = self.projection("done");
        self.commit_terminal(TerminalCandidate::done(done, projection));
    }

    /// A failed Turn settles `done` with its error envelope, as
    /// `_finalize.py` does; there is no separate terminal `error` event.
    fn settle_failed(&mut self, round: u32, finish_reason: &str, kind: &str, detail: &str) {
        self.emit("round_end", json!({"roundNum": round, "reason": "error"}));
        if kind == "internal" {
            // `handle_task_fatal` closes the round again from the post-loop tail.
            self.emit("round_end", json!({"roundNum": round, "reason": "error"}));
        }
        self.checkpoint(None);
        self.trace.error_kind = Some(kind.to_string());
        self.trace.finish_reason = Some(finish_reason.to_string());
        let mut envelope = Map::new();
        envelope.insert("kind".into(), Value::from(kind));
        envelope.insert("detail".into(), Value::from(detail));
        let fields = json!({"finishReason": finish_reason, "error": Value::Object(envelope)});
        let done = serde_json::from_value(fields).expect("DoneEvent fields");
        let projection = self.projection("error");
        self.commit_terminal(TerminalCandidate::done(done, projection));
    }

    fn settle_storage_failure(&mut self, error: &StoreError) {
        let round = self.trace.requests.last().map_or(1, |r| r.round);
        self.settle_failed(round, "error", &error.code, &error.message);
    }

    fn settle_stopped(&mut self, pre_abort_finish: &str) {
        let reason = self.cancel.reason().unwrap_or_default();
        let plan = settle_plan(Some(&reason), false);
        let round = self.trace.requests.last().map_or(1, |r| r.round);
        self.emit("round_end", json!({"roundNum": round, "reason": "aborted"}));
        if plan.save_checkpoint_first {
            self.checkpoint(Some(&reason));
        }
        let model = self.fallback.model_for_request(false).to_string();
        let done = stop_done_event(&reason, "user", pre_abort_finish, &model);
        self.trace.finish_reason = done.finish_reason.clone();
        let status = if plan.paused {
            "interrupted"
        } else {
            "aborted"
        };
        let projection = self.projection(status);
        self.commit_terminal(TerminalCandidate::done(done, projection));
    }

    fn salvage_steers(&mut self) {
        let late = match self.retry_transient(|store| store.take_steers()) {
            Ok(records) => take_steers(&records, Vec::new(), &self.ledger),
            Err(error) => {
                self.trace.control_errors.push(error);
                Vec::new()
            }
        };
        let mut pending = self.ledger.salvage();
        pending.extend(late);
        let records: Vec<_> = pending
            .iter()
            .map(|item| delivery_record(item, 0, "pending"))
            .collect();
        if !records.is_empty() {
            if let Err(error) = self.retry_transient(|store| store.requeue_steers(&records)) {
                self.trace.control_errors.push(error);
                self.trace.pending_steer_requeues.extend(records);
            }
        }
    }

    fn commit_terminal(&mut self, candidate: TerminalCandidate) {
        let kind = candidate.kind;
        let terminal_event = candidate.event.clone();
        let retained_candidate = candidate.clone();
        if self.settlement.propose(candidate).is_err() {
            return;
        }
        self.salvage_steers();
        self.flush_events();
        loop {
            match self.settlement.commit(self.ports.store, self.ports.sink) {
                Ok(settled) => {
                    self.trace.kind = settled;
                    self.trace.terminal_committed = true;
                    self.trace.events.push(terminal_event);
                    return;
                }
                Err(super::settlement::CommitError::Store(error)) if error.transient => continue,
                Err(error) => {
                    self.trace.kind = kind;
                    self.trace.pending_terminal = Some(retained_candidate);
                    if let super::settlement::CommitError::Store(error) = error {
                        self.trace.settlement_error = Some(error);
                    }
                    return;
                }
            }
        }
    }
}

/// Retry only the retained terminal event/projection, without executing the
/// model, tools, checkpoint or terminal observers again.
pub fn retry_terminal(
    outcome: &mut SessionOutcome,
    store: &dyn DurableStore,
    sink: &dyn EventSink,
) -> Result<(), super::settlement::CommitError> {
    if outcome.terminal_committed {
        return Err(super::settlement::CommitError::AlreadySettled(outcome.kind));
    }
    let candidate = outcome
        .pending_terminal
        .as_ref()
        .ok_or(super::settlement::CommitError::NoTerminal)?
        .clone();
    let mut settlement = TurnSettlement::new();
    settlement
        .propose(candidate.clone())
        .expect("fresh settlement");
    match settlement.commit(store, sink) {
        Ok(kind) => {
            outcome.kind = kind;
            outcome.terminal_committed = true;
            outcome.settlement_error = None;
            outcome.pending_terminal = None;
            outcome.events.push(candidate.event);
            Ok(())
        }
        Err(error) => {
            if let super::settlement::CommitError::Store(store_error) = &error {
                outcome.settlement_error = Some(store_error.clone());
            }
            Err(error)
        }
    }
}

/// `(finishReason, error kind, detail)` of a settled provider failure.
fn failure_text(kind: &FailureKind, outcome: &ProviderOutcome) -> (String, String, String) {
    let detail = match outcome {
        ProviderOutcome::ContentFilter { detail, .. }
        | ProviderOutcome::HttpError { detail, .. }
        | ProviderOutcome::Dropped { detail, .. }
        | ProviderOutcome::Internal { detail } => detail.clone(),
        other => format!("{other:?}"),
    };
    match kind {
        FailureKind::ModelRefusal => ("content_filter".into(), "content_filter".into(), detail),
        FailureKind::GatewayContentFilter => {
            ("error".into(), "gateway_content_filter".into(), detail)
        }
        FailureKind::AbnormalStop => ("error".into(), "abnormal_stop".into(), detail),
        FailureKind::ProviderError { kind } => ("error".into(), kind.clone(), detail),
    }
}

/// `ToolResultEnvelope.to_model_text` for an error: compact sorted JSON of
/// the sparse projection (`lib/tools/result_envelope.py::_model_projection`).
fn model_error_text(error: &TypedToolError) -> String {
    let mut projection = std::collections::BTreeMap::new();
    projection.insert("code", Value::from(error.code.as_str()));
    projection.insert("message", Value::from(error.message.as_str()));
    projection.insert("nextAction", Value::from(error.next_action.as_str()));
    projection.insert("retryable", Value::from(error.retryable));
    projection.insert("status", Value::from("error"));
    serde_json::to_string(&projection).expect("strings and bools encode")
}

/// The admitted occurrence, not the recyclable provider call id, owns the
/// operation id. Retries retain it; later rounds and duplicate batch positions
/// never alias each other's effect evidence.
fn operation_id(attempt_id: &str, round: u32, position: usize, call_id: &str) -> String {
    let digest = Sha256::digest(format!("{attempt_id}\0{round}\0{position}\0{call_id}").as_bytes());
    digest[..16].iter().map(|b| format!("{b:02x}")).collect()
}
