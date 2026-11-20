//! Round checkpoints and interruption recovery: rewind to the last
//! protocol-valid boundary and resend byte-identically.
//!
//! Replaces, for the engine loop:
//! - `lib/tasks_pkg/compaction/_resume_projection.py`
//!   (`_closed_working_view` :272, `_without_repeatable_cut_batch` :320,
//!   `preserve_suspended_working_view` :366 and its `_not_saved` reasons,
//!   `compaction_resume_witness` :58, `resume_execution_digest` :53),
//! - `lib/tasks_pkg/compaction/_handoff.py::has_open_tool_batch` (:162) with
//!   `lib/tool_results/tool_history_pairing.py::adjacent_tool_call_result_pairs`
//!   and `_layer2/_anchor.py::_apiform_tool_rounds` (:278),
//! - `lib/tasks_pkg/orchestrator/_round_checkpoint.py` (a checkpoint at every
//!   round boundary, failure non-fatal),
//! - `lib/conversations/restart_resume.py::resume_conversation` (:428) and
//!   `lane_tail_verdict` (:103): a quiesced Turn resumes once, under command id
//!   `restart-resume:<interrupted Attempt>`.
//!
//! Contracts: `docs/contracts/TURN_SETTLEMENT.md` "Interruption recovery:
//! rewind and resend" (nothing synthetic is appended: no trailing-assistant
//! prefill, no continuation prompt); `docs/CONTEXT_COMPACTION.md`
//! "Continuation checkpoints" (every non-completed root Turn checkpoints its
//! working view, a Turn that restarts the server through a gateway keeps its
//! checkpoint instead of re-compressing); `docs/RUN_CONTINUITY.md`
//! "Restart-paused conversations resume by themselves".
//!
//! Entry points, in the order the loop calls them:
//! 1. `is_protocol_valid_boundary(messages)` before `DurableStore::save_checkpoint`
//!    at each round end; `build_checkpoint` makes the payload.
//! 2. `rewind(messages)` on a cut/Stop/restart: the exact request to resend.
//! 3. `settle_plan(abort_reason, completed)` before settling: a drain/restart
//!    pause saves its checkpoint first, then settles `interrupted`.
//! 4. `restart_resume_decision` + `ResumeTokens::consume` at boot.
//!
//! Pure: no I/O, no clock, no threads. The native actor's
//! `contracts/native_agent_checkpoint_v1.schema.json` belongs to the TofuDB
//! actor owner and is not this payload.

use std::collections::{BTreeSet, HashMap, VecDeque};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::abort::is_pause;
use super::types::{Message, Role};
use crate::pyjson::{compact_string, dumps_spaced};

/// `CHECKPOINT_IMPLEMENTATION` (`_resume_projection.py`).
pub const CHECKPOINT_IMPLEMENTATION: &str = "suspended_working_context";
/// `lib/agent_core/model_context_limits.py::MAX_MESSAGES`.
pub const MAX_CHECKPOINT_MESSAGES: usize = 16384;
/// `lib/agent_core/model_context_limits.py::MAX_ENTRY_BYTES` (the 8 MiB
/// continuation bound).
pub const MAX_CHECKPOINT_VIEW_BYTES: usize = 8 * 1024 * 1024;
/// `restart_resume.py` command-id prefix of an automatic restart resume.
pub const RESTART_RESUME_COMMAND_PREFIX: &str = "restart-resume:";
/// `restart_resume.RESUME_OPERATIONS`, in preference order.
pub const RESUME_OPERATIONS: [&str; 2] = ["continue", "checkpoint_resume"];

/// `_apiform_tool_rounds`: half-open spans of an assistant message carrying
/// tool calls plus every immediately following tool message.
pub fn tool_round_spans(messages: &[Message]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut index = 0;
    while index < messages.len() {
        let message = &messages[index];
        if message.role == Role::Assistant && !message.tool_calls.is_empty() {
            let mut end = index + 1;
            while end < messages.len() && messages[end].role == Role::Tool {
                end += 1;
            }
            spans.push((index, end));
            index = end;
        } else {
            index += 1;
        }
    }
    spans
}

/// `adjacent_tool_call_result_pairs` restricted to one span: how many calls
/// of `messages[start]` have a result in the contiguous tool run after it.
/// Duplicate ids pair by occurrence; blank ids never pair.
fn paired_calls(messages: &[Message], start: usize, end: usize) -> usize {
    let mut results: HashMap<&str, VecDeque<usize>> = HashMap::new();
    for (position, result) in messages.iter().enumerate().take(end).skip(start + 1) {
        let id = result.tool_call_id.as_deref().unwrap_or("").trim();
        if !id.is_empty() {
            results.entry(id).or_default().push_back(position);
        }
    }
    messages[start]
        .tool_calls
        .iter()
        .filter(|call| {
            let id = call.id.trim();
            !id.is_empty()
                && results
                    .get_mut(id)
                    .is_some_and(|queue| queue.pop_front().is_some())
        })
        .count()
}

/// `has_open_tool_batch`: some batch lacks a result for one of its calls, or
/// a tool message stands outside every closed batch.
pub fn has_open_tool_batch(messages: &[Message]) -> bool {
    let mut closed_results = 0;
    for (start, end) in tool_round_spans(messages) {
        let calls = messages[start].tool_calls.len();
        if !(paired_calls(messages, start, end) == calls && calls == end - start - 1) {
            return true;
        }
        closed_results += end - start - 1;
    }
    closed_results != messages.iter().filter(|m| m.role == Role::Tool).count()
}

/// A protocol-valid boundary: no assistant tool call without its result and
/// no orphan result. A checkpoint is taken only here.
pub fn is_protocol_valid_boundary(messages: &[Message]) -> bool {
    !messages.is_empty() && !has_open_tool_batch(messages)
}

/// The request an interruption resends, and what it dropped.
#[derive(Debug, Clone, PartialEq)]
pub struct Rewound {
    pub messages: Vec<Message>,
    /// Call ids of the unfinished trailing batch: they return only as the
    /// settled receipts a resume replays, never by re-execution here.
    pub dropped_call_ids: Vec<String>,
}

/// The message part of `_closed_working_view`: rewind to the last
/// protocol-valid boundary (the user message or the last complete tool
/// result). A trailing unfinished batch and its partial results are dropped;
/// nothing is appended. `None` when no such boundary exists (an open batch
/// that is not the last span, or one left open after the rewind).
pub fn rewind(messages: &[Message]) -> Option<Rewound> {
    if !has_open_tool_batch(messages) {
        return Some(Rewound {
            messages: messages.to_vec(),
            dropped_call_ids: Vec::new(),
        });
    }
    let &(start, end) = tool_round_spans(messages).last()?;
    if end != messages.len() {
        return None;
    }
    let view = messages[..start].to_vec();
    if has_open_tool_batch(&view) {
        return None;
    }
    Some(Rewound {
        messages: view,
        dropped_call_ids: messages[start]
            .tool_calls
            .iter()
            .map(|call| call.id.clone())
            .collect(),
    })
}

/// Why a checkpoint was not saved (`[CompactionResume] … checkpoint not saved:
/// <reason>`). The loop logs it at INFO and continues: a checkpoint failure
/// never breaks a healthy round.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotSaved {
    InputMessagesOnly,
    RestartCutBatchNotLast,
    NoProtocolBoundary,
    MessageCountOutOfBounds,
    OverContinuationBytes,
    NoResumeWitness,
}

impl NotSaved {
    pub fn as_str(self) -> &'static str {
        match self {
            NotSaved::InputMessagesOnly => "input_messages_only",
            NotSaved::RestartCutBatchNotLast => "restart_cut_batch_not_last",
            NotSaved::NoProtocolBoundary => "no_protocol_boundary",
            NotSaved::MessageCountOutOfBounds => "message_count_out_of_bounds",
            NotSaved::OverContinuationBytes => "over_continuation_bytes",
            NotSaved::NoResumeWitness => "no_resume_witness",
        }
    }
}

/// What the loop knows when it checkpoints.
#[derive(Debug, Clone, PartialEq)]
pub struct CheckpointInput<'a> {
    pub turn_id: &'a str,
    pub attempt_id: &'a str,
    /// The working messages, including this run's rounds.
    pub messages: &'a [Message],
    /// False while `messages` are still only the Turn's input.
    pub includes_current_run: bool,
    /// An inbox carrier (steer, peer, background) was appended after the
    /// latest verified provider return: restoring it would deliver it twice.
    pub unverified_inbox_delivery: bool,
    /// Calls of the last batch that a restart cut and that are repeatable
    /// (read-only/idempotent/verifiable, `tool_effect`). Only read for a
    /// pause; the view then ends at the request that produced the batch.
    pub repeatable_cut_call_ids: &'a [String],
    /// The Turn's text lanes the resume compares.
    pub content: &'a str,
    pub thinking: &'a str,
}

/// `resume_execution_digest`: sha256 over the sorted compact JSON of the
/// Turn's text lanes (`ensure_ascii=False`).
pub fn resume_execution_digest(content: &str, thinking: &str) -> String {
    let lanes = json!({"content": content, "thinking": thinking});
    hex(&Sha256::digest(compact_string(&lanes).as_bytes()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The api-form JSON of one message (the shape the archive stores).
pub fn message_json(message: &Message) -> Value {
    let mut object = Map::new();
    object.insert("role".into(), Value::from(message.role.as_str()));
    object.insert("content".into(), Value::from(message.content.as_str()));
    if !message.tool_calls.is_empty() {
        let calls = message
            .tool_calls
            .iter()
            .map(|call| {
                json!({"id": call.id, "type": "function",
                       "function": {"name": call.name, "arguments": call.arguments}})
            })
            .collect();
        object.insert("tool_calls".into(), Value::Array(calls));
    }
    if let Some(id) = &message.tool_call_id {
        object.insert("tool_call_id".into(), Value::from(id.as_str()));
    }
    Value::Object(object)
}

/// `preserve_suspended_working_view` for an uncompacted view: the payload
/// for `DurableStore::save_checkpoint`, or why none is saved.
/// `abort_reason` is the cancel reason (`None` while running).
pub fn build_checkpoint(
    input: &CheckpointInput,
    abort_reason: Option<&str>,
) -> Result<Value, NotSaved> {
    if !input.includes_current_run {
        return Err(NotSaved::InputMessagesOnly);
    }
    let mut messages = input.messages;
    let restart_settled = abort_reason.is_some_and(is_pause);
    if restart_settled && !input.repeatable_cut_call_ids.is_empty() {
        let &(start, end) = tool_round_spans(messages)
            .last()
            .ok_or(NotSaved::RestartCutBatchNotLast)?;
        let announced: BTreeSet<&str> = messages[start]
            .tool_calls
            .iter()
            .map(|c| c.id.as_str())
            .collect();
        let cut: BTreeSet<&str> = input
            .repeatable_cut_call_ids
            .iter()
            .map(String::as_str)
            .collect();
        if end != messages.len() || announced != cut {
            return Err(NotSaved::RestartCutBatchNotLast);
        }
        messages = &messages[..start];
    }
    let view = rewind(messages)
        .ok_or(NotSaved::NoProtocolBoundary)?
        .messages;
    if view.is_empty() || view.len() > MAX_CHECKPOINT_MESSAGES {
        return Err(NotSaved::MessageCountOutOfBounds);
    }
    let post_messages = Value::Array(view.iter().map(message_json).collect());
    if dumps_spaced(&post_messages).len() > MAX_CHECKPOINT_VIEW_BYTES {
        return Err(NotSaved::OverContinuationBytes);
    }
    if input.unverified_inbox_delivery || input.turn_id.is_empty() {
        return Err(NotSaved::NoResumeWitness);
    }
    let held_rounds = view.iter().filter(|m| m.role == Role::Tool).count();
    Ok(json!({
        "status": "completed",
        "implementation": CHECKPOINT_IMPLEMENTATION,
        "trigger": "checkpoint",
        "turnId": input.turn_id,
        "attemptId": input.attempt_id,
        "resumeWitness": {
            "version": 1,
            "turnId": input.turn_id,
            "toolRoundCount": held_rounds,
            "executionDigest": resume_execution_digest(input.content, input.thinking),
        },
        "postMessages": post_messages,
    }))
}

/// What the loop does before settling a stopped or finished Turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SettlePlan {
    /// Save the working-view checkpoint before the terminal commit.
    pub save_checkpoint_first: bool,
    /// The stop is a pause (server drain/restart or lead hold): the Turn
    /// settles `interrupted` and resumes from that checkpoint.
    pub paused: bool,
}

/// Every root Turn whose verdict is not `completed` checkpoints its working
/// view before it settles; a drain or restart cancel reason (a lead that
/// restarts its own server included) is a pause that keeps the checkpoint
/// rather than forcing the resumed attempt to rebuild and re-compress.
pub fn settle_plan(abort_reason: Option<&str>, completed: bool) -> SettlePlan {
    let paused = abort_reason.is_some_and(is_pause);
    SettlePlan {
        save_checkpoint_first: !completed || paused,
        paused,
    }
}

/// The durable facts of an interrupted Turn that a restart pass reads.
#[derive(Debug, Clone, PartialEq)]
pub struct InterruptedTurn<'a> {
    pub turn_id: &'a str,
    pub current_attempt_id: &'a str,
    pub status: &'a str,
    pub settlement_cause: &'a str,
    pub lane_id: &'a str,
    /// Operations the settlement offers, in order.
    pub resume_operations: &'a [&'a str],
    /// `error.unknownEffects` entries (calls cut mid-effect of class unknown).
    pub unknown_effect_count: usize,
    /// The first later input Turn on the lane that is not an ingested record.
    pub superseded_by: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResumeDecision {
    Resume {
        operation: String,
        command_id: String,
    },
    Skip {
        reason: String,
    },
}

/// `resume_conversation` without its I/O: whether a restart-paused Turn is
/// continued in place, with which operation and idempotent command id.
/// `interrupted_attempt` names the quiesce record's Attempt.
pub fn restart_resume_decision(
    turn: &InterruptedTurn,
    interrupted_attempt: Option<&str>,
) -> ResumeDecision {
    let skip = |reason: String| ResumeDecision::Skip { reason };
    let lane = if turn.lane_id.is_empty() {
        "main"
    } else {
        turn.lane_id
    };
    if lane != "main" {
        return skip(format!("it runs on lane {lane}, not the main lane"));
    }
    if interrupted_attempt.is_some_and(|attempt| attempt != turn.current_attempt_id) {
        return skip(format!(
            "already continued in Attempt {}",
            turn.current_attempt_id
        ));
    }
    if turn.status != "interrupted" {
        return skip(format!("its Turn is {}, not interrupted", turn.status));
    }
    if turn.settlement_cause != "server_restart" {
        let cause = if turn.settlement_cause.is_empty() {
            "an unknown cause"
        } else {
            turn.settlement_cause
        };
        return skip(format!("interrupted by {cause}, not by the restart"));
    }
    if let Some(input) = turn.superseded_by {
        return skip(format!("superseded by input {input}"));
    }
    if turn.unknown_effect_count > 0 {
        return skip("a call was cut off with an unknown side effect".to_string());
    }
    if turn.resume_operations.contains(&"answer_guidance") {
        return skip(
            "waiting for the owner\u{2019}s answer to its question (answer_guidance)".to_string(),
        );
    }
    match turn
        .resume_operations
        .iter()
        .find(|op| RESUME_OPERATIONS.contains(op))
    {
        Some(operation) => ResumeDecision::Resume {
            operation: operation.to_string(),
            command_id: format!("{RESTART_RESUME_COMMAND_PREFIX}{}", turn.current_attempt_id),
        },
        None => {
            let offered = if turn.resume_operations.is_empty() {
                "nothing".to_string()
            } else {
                turn.resume_operations.join(", ")
            };
            skip(format!(
                "only {offered} is offered; regenerate would redo the work"
            ))
        }
    }
}

/// Resume command ids already accepted. The storage command receipt is the
/// authority; this is its pure model: a repeated pass, process or restart
/// presents the same command id and is refused.
#[derive(Debug, Default, Clone)]
pub struct ResumeTokens {
    consumed: BTreeSet<String>,
}

impl ResumeTokens {
    /// True exactly once per command id.
    pub fn consume(&mut self, command_id: &str) -> bool {
        self.consumed.insert(command_id.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::types::ToolCall;

    fn call(id: &str) -> ToolCall {
        ToolCall {
            id: id.into(),
            name: "read_files".into(),
            arguments: "{\"path\":\"a\"}".into(),
        }
    }

    fn assistant_calls(ids: &[&str]) -> Message {
        Message {
            role: Role::Assistant,
            content: String::new(),
            tool_calls: ids.iter().map(|id| call(id)).collect(),
            tool_call_id: None,
        }
    }

    fn base() -> Vec<Message> {
        vec![
            Message::text(Role::System, "sys"),
            Message::text(Role::User, "fix the bug"),
            assistant_calls(&["c1"]),
            Message::tool_result("c1", "file body"),
        ]
    }

    fn input<'a>(messages: &'a [Message], cut: &'a [String]) -> CheckpointInput<'a> {
        CheckpointInput {
            turn_id: "turn-1",
            attempt_id: "att-1",
            messages,
            includes_current_run: true,
            unverified_inbox_delivery: false,
            repeatable_cut_call_ids: cut,
            content: "",
            thinking: "",
        }
    }

    /// Row "Interruption recovery rewinds to the last protocol-valid boundary
    /// and resends byte-identically: no prefill, no synthetic continue"
    /// (TURN_SETTLEMENT.md); pins `tests/llm/test_upstream_turn_replay.py::
    /// test_upstream_outage_recovers_in_the_real_invocation` and
    /// `test_llm_replay_scenarios.py::test_partial_stream_cut_resends_the_identical_request`
    /// (the cut attempt and its retry share one request).
    #[test]
    fn a_cut_round_resends_the_identical_request_without_prefill_or_continue() {
        let request = base();
        let rewound = rewind(&request).expect("closed boundary");
        assert_eq!(rewound.messages, request);
        assert!(rewound.dropped_call_ids.is_empty());
        let last = rewound.messages.last().unwrap();
        assert_ne!(last.role, Role::Assistant, "no trailing assistant prefill");
        assert_eq!(
            rewound.messages.len(),
            request.len(),
            "no synthetic continue"
        );
    }

    /// Row "rewinds to the last protocol-valid boundary"; pins
    /// `tests/compaction/test_suspended_working_view.py` (an unfinished
    /// trailing tool batch and its partial results are dropped) and
    /// `tests/llm/test_upstream_turn_replay.py::
    /// test_explicit_retry_opt_out_still_closes_the_failed_model_round`.
    #[test]
    fn rewind_drops_the_unfinished_trailing_batch_and_its_partial_results() {
        let request = base();
        let mut cut = request.clone();
        cut.push(assistant_calls(&["c2", "c3"]));
        cut.push(Message::tool_result("c2", "partial"));
        let rewound = rewind(&cut).expect("boundary");
        assert_eq!(rewound.messages, request);
        assert_eq!(
            rewound.dropped_call_ids,
            vec!["c2".to_string(), "c3".to_string()]
        );
        assert!(is_protocol_valid_boundary(&rewound.messages));
        assert!(!is_protocol_valid_boundary(&cut));
    }

    #[test]
    fn an_open_batch_that_is_not_last_has_no_boundary() {
        let mut messages = base();
        messages.push(assistant_calls(&["c2"]));
        messages.push(Message::text(Role::User, "steer"));
        assert_eq!(rewind(&messages), None);
        let orphan = vec![
            Message::text(Role::User, "u"),
            Message::tool_result("x", "orphan"),
        ];
        assert!(has_open_tool_batch(&orphan));
    }

    #[test]
    fn duplicate_call_ids_pair_by_occurrence() {
        let mut messages = vec![Message::text(Role::User, "u"), assistant_calls(&["d", "d"])];
        messages.push(Message::tool_result("d", "one"));
        assert!(has_open_tool_batch(&messages));
        messages.push(Message::tool_result("d", "two"));
        assert!(!has_open_tool_batch(&messages));
    }

    #[test]
    fn a_checkpoint_is_taken_only_at_a_protocol_valid_boundary() {
        let messages = base();
        let payload = build_checkpoint(&input(&messages, &[]), None).expect("saved");
        assert_eq!(payload["implementation"], CHECKPOINT_IMPLEMENTATION);
        assert_eq!(payload["postMessages"].as_array().unwrap().len(), 4);
        assert_eq!(payload["resumeWitness"]["toolRoundCount"], 1);
        assert_eq!(
            payload["resumeWitness"]["executionDigest"],
            resume_execution_digest("", "")
        );
        let mut open = messages.clone();
        open.push(assistant_calls(&["c2"]));
        let saved = build_checkpoint(&input(&open, &[]), None).unwrap();
        assert_eq!(
            saved["postMessages"], payload["postMessages"],
            "rewound before saving"
        );
        let mut not_last = open.clone();
        not_last.push(Message::text(Role::User, "late"));
        assert_eq!(
            build_checkpoint(&input(&not_last, &[]), None),
            Err(NotSaved::NoProtocolBoundary)
        );
    }

    /// `compaction_resume_witness` refuses a view holding a carrier the
    /// provider has not consumed (it would be delivered twice), and a view of
    /// only the Turn's input is not a checkpoint.
    #[test]
    fn unconsumed_inbox_carriers_and_input_only_views_are_not_saved() {
        let messages = base();
        let mut unverified = input(&messages, &[]);
        unverified.unverified_inbox_delivery = true;
        assert_eq!(
            build_checkpoint(&unverified, None),
            Err(NotSaved::NoResumeWitness)
        );
        let mut input_only = input(&messages, &[]);
        input_only.includes_current_run = false;
        assert_eq!(
            build_checkpoint(&input_only, None),
            Err(NotSaved::InputMessagesOnly)
        );
    }

    #[test]
    fn a_view_over_the_continuation_bound_is_not_saved() {
        let messages = vec![Message::text(
            Role::User,
            "x".repeat(MAX_CHECKPOINT_VIEW_BYTES),
        )];
        assert_eq!(
            build_checkpoint(&input(&messages, &[]), None),
            Err(NotSaved::OverContinuationBytes)
        );
    }

    /// The execution digest is the Python `_digest({'content','thinking'})`:
    /// sorted compact JSON with `ensure_ascii=False`.
    #[test]
    fn the_execution_digest_matches_python_canonical_json() {
        let expected = hex(&Sha256::digest(
            "{\"content\":\"é\\n\",\"thinking\":\"t\"}".as_bytes(),
        ));
        assert_eq!(resume_execution_digest("é\n", "t"), expected);
    }

    /// Row "Quiesced Turns resume exactly once from their checkpoint"
    /// (RUN_CONTINUITY.md "Restart-paused conversations resume by
    /// themselves"); pins the self-drain fix in CONTEXT_COMPACTION.md ("A Turn
    /// that restarts the server through `execute_tools`": the lead keeps its
    /// checkpoint instead of re-compressing) and
    /// `tests/compaction/test_suspended_working_view.py::
    /// test_turn_that_restarted_the_server_through_execute_tools_resumes_its_view`.
    #[test]
    fn a_drain_or_restart_stop_saves_its_checkpoint_before_settling_paused() {
        for reason in ["server_shutdown", "restart_recovery", "lead_hold"] {
            assert_eq!(
                settle_plan(Some(reason), false),
                SettlePlan {
                    save_checkpoint_first: true,
                    paused: true
                },
                "{reason}"
            );
        }
        let stop = settle_plan(Some("user_abort"), false);
        assert!(stop.save_checkpoint_first && !stop.paused);
        assert_eq!(
            settle_plan(None, true),
            SettlePlan {
                save_checkpoint_first: false,
                paused: false
            }
        );
    }

    /// A restart-cut repeatable last batch leaves the view at the request
    /// that produced it (`_without_repeatable_cut_batch`); a cut batch that
    /// is not the last span refuses (`restart_cut_batch_not_last`).
    #[test]
    fn a_restart_cut_repeatable_batch_is_dropped_from_the_saved_view() {
        let mut messages = base();
        messages.push(assistant_calls(&["c2"]));
        messages.push(Message::tool_result("c2", "cut by restart"));
        let cut = vec!["c2".to_string()];
        let payload = build_checkpoint(&input(&messages, &cut), Some("server_shutdown")).unwrap();
        assert_eq!(
            payload["postMessages"].as_array().unwrap().len(),
            base().len()
        );
        let other = vec!["c1".to_string()];
        assert_eq!(
            build_checkpoint(&input(&messages, &other), Some("server_shutdown")),
            Err(NotSaved::RestartCutBatchNotLast)
        );
        let running = build_checkpoint(&input(&messages, &cut), None).unwrap();
        assert_eq!(
            running["postMessages"].as_array().unwrap().len(),
            messages.len()
        );
    }

    fn interrupted<'a>(ops: &'a [&'a str]) -> InterruptedTurn<'a> {
        InterruptedTurn {
            turn_id: "t1",
            current_attempt_id: "a1",
            status: "interrupted",
            settlement_cause: "server_restart",
            lane_id: "main",
            resume_operations: ops,
            unknown_effect_count: 0,
            superseded_by: None,
        }
    }

    /// Row "Quiesced Turns resume exactly once from their checkpoint"; pins
    /// `tests/conversation/test_restart_resume.py::
    /// test_interrupted_chats_resume_from_their_checkpoint_once` and
    /// `test_the_restart_resume_pass_and_project_dispatch_resume_one_interruption_once`.
    #[test]
    fn a_quiesced_turn_resumes_once_under_its_restart_resume_command_id() {
        let ops = ["regenerate", "checkpoint_resume", "continue"];
        let decision = restart_resume_decision(&interrupted(&ops), Some("a1"));
        let ResumeDecision::Resume {
            operation,
            command_id,
        } = decision
        else {
            panic!("expected resume");
        };
        assert_eq!(operation, "checkpoint_resume");
        assert_eq!(command_id, "restart-resume:a1");
        let mut tokens = ResumeTokens::default();
        assert!(tokens.consume(&command_id));
        assert!(
            !tokens.consume(&command_id),
            "a repeated pass resumes nothing"
        );
        assert!(matches!(
            restart_resume_decision(&interrupted(&ops), Some("a0")),
            ResumeDecision::Skip { .. }
        ));
    }

    /// Pins `test_restart_resume.py::
    /// test_a_turn_with_a_call_cut_off_mid_effect_waits_for_its_owner`,
    /// `test_a_long_turn_a_queued_input_took_over_is_reported_superseded_and_nothing_is_deleted`,
    /// `test_a_conversation_the_owner_moved_on_from_is_not_resumed` and
    /// `test_a_restart_interruption_holding_a_cut_call_is_never_continued`.
    #[test]
    fn work_the_owner_must_review_or_that_was_superseded_is_left_alone() {
        let ops = ["continue"];
        let mut unknown = interrupted(&ops);
        unknown.unknown_effect_count = 1;
        let mut superseded = interrupted(&ops);
        superseded.superseded_by = Some("t2");
        let mut stopped = interrupted(&ops);
        stopped.settlement_cause = "user_abort";
        let regenerate_only = ["regenerate"];
        let guidance = ["answer_guidance", "continue"];
        for turn in [
            unknown,
            superseded,
            stopped,
            interrupted(&regenerate_only),
            interrupted(&guidance),
        ] {
            assert!(matches!(
                restart_resume_decision(&turn, Some("a1")),
                ResumeDecision::Skip { .. }
            ));
        }
    }
}
