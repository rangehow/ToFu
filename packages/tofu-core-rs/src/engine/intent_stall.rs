//! Intent-stall nudge: the four-criterion classifier, its one-per-task
//! budget, the exact nudge message, and the `[END_TURN: …]` / `[SILENT]`
//! control-token hygiene of the delivered answer.
//!
//! Replaces, for the engine loop:
//! - `lib/tasks_pkg/stream_handler/_intent_stall.py` (`parse_end_turn_reason`
//!   :96, `is_silent_turn` :117, `strip_silent_turn_marker` :143,
//!   `strip_end_turn_marker` :150, `build_stall_nudge_record` :180,
//!   `known_external_blocker` :239, `_uncovered_failure` :262, `_round_failed`
//!   :293, `_is_non_retryable` :318, `_awaiting_human` :337,
//!   `should_nudge_intent_stall` :375, `NUDGE_TEXT` :424),
//! - the call site `lib/tasks_pkg/stream_handler/_analyse.py::
//!   _handle_intent_stall_nudge` (:189): checked after the commentary lane and
//!   before the output-contract lane, in the no-tool-call branch; on a nudge it
//!   appends the vetoed answer as an assistant prose row
//!   (`assistant_messages.py::append_assistant_prose_message` :45) and then the
//!   nudge as a user-role injection, and continues the loop,
//! - the live-text rule `lib/tasks_pkg/manager/_events.py::
//!   begin_live_text_paragraph` (:223): the vetoed answer stays the provisional
//!   answer and the next round's text starts a new paragraph after it,
//! - the delivered-answer hygiene of `lib/tasks_pkg/orchestrator/_finalize.py::
//!   _strip_end_turn_control_token` (:1246).
//!
//! THE SHAPE: a tool round is rejected or errors and the model's next round
//! is prose only with zero tool calls. Every criterion reads structure (round
//! status, result metadata, tool-call count, rejection class, an explicit
//! closed-set end declaration); wording is never a criterion.
//!
//! Entry points: after a completed round with no tool calls, the loop calls
//! `stall_decision`; on `Nudge` it appends `assistant` then `nudge` to the
//! history, increments its nudge counter, extends its live text with
//! `paragraph_separator`, and issues another request. At settlement the
//! delivered content is `delivered_answer(live_text)`.
//!
//! Pure: no I/O. Depends only on `engine::types` and `crate::pyjson`.

use std::borrow::Cow;

use super::types::{Message, Role, ToolCall};
use crate::pyjson::py_blank;

/// `_intent_stall.py::END_TURN_MARKER`.
pub const END_TURN_MARKER: &str = "[END_TURN:";
/// `_intent_stall.py::END_TURN_AWAITING_HUMAN`.
pub const END_TURN_AWAITING_HUMAN: &str = "awaiting_human";
/// `_intent_stall.py::END_TURN_DONE`.
pub const END_TURN_DONE: &str = "done";
/// `_intent_stall.py::END_TURN_BLOCKED`.
pub const END_TURN_BLOCKED: &str = "blocked";
/// `_intent_stall.py::_VALID_END_REASONS`.
const VALID_END_REASONS: [&str; 3] = [END_TURN_AWAITING_HUMAN, END_TURN_DONE, END_TURN_BLOCKED];
/// `_intent_stall.py::SILENT_TURN_MARKER`.
pub const SILENT_TURN_MARKER: &str = "[SILENT]";

/// `_intent_stall.py::_HUMAN_HANDOFF_TOOLS`.
const HUMAN_HANDOFF_TOOLS: [&str; 2] = ["ask_user", "request_human_input"];
/// Round statuses `_awaiting_human` treats as a live handoff (D1b).
const LIVE_WAIT_STATUSES: [&str; 3] = ["awaiting_human", "awaiting_stdin", "pending_approval"];
/// `_intent_stall.py::_NON_RETRYABLE_MARKERS`.
const NON_RETRYABLE_MARKERS: [&str; 2] =
    ["is not a real tool", "not in the list of tools available"];
/// Round statuses `_round_failed` reads as failure.
const FAILED_STATUSES: [&str; 4] = ["rejected", "error", "failed", "timeout"];
/// Result badges `_round_failed` reads as failure.
const FAILED_BADGES: [&str; 4] = ["blocked", "error", "failed", "timeout"];
/// Rejection descriptor kind `known_external_blocker` reports.
const GIT_GUARD_KIND: &str = "git_guard";

/// Nudges per task: `_analyse.py::_handle_intent_stall_nudge`
/// (`if _stall_nudges >= 1: return None`, :208) and the chip's `'max': 1`
/// (`_intent_stall.py::build_stall_nudge_record`, :235).
pub const MAX_STALL_NUDGES: u32 = 1;

/// `_intent_stall.py::NUDGE_TEXT`, with `END_TURN_MARKER` expanded.
pub const NUDGE_TEXT: &str = concat!(
    "[SYSTEM: UNRESOLVED TOOL FAILURE]\n",
    "A previous tool call was rejected or failed, and no later tool result ",
    "has resolved that failure. This response contains no tool calls.\n\n",
    "Inspect the failure evidence before deciding the next action. A failed ",
    "command may already have executed and changed state; do not blindly ",
    "repeat side effects. If recovery is needed, use an appropriate tool, ",
    "adjusting invalid arguments or checking existing state first.\n",
    "If you are waiting on the user, or the task is genuinely finished or ",
    "blocked, end your turn with [END_TURN: awaiting_human], ",
    "[END_TURN: done] or [END_TURN: blocked] so this is not ",
    "mistaken for an interrupted turn."
);

/// The `should_nudge_intent_stall` reasons, spelled as Python returns them.
pub const REASON_INTENT_STALL: &str = "intent_stall";
pub const REASON_NO_CONTENT: &str = "no_content";
pub const REASON_HAS_TOOL_CALLS: &str = "has_tool_calls";
pub const REASON_AWAITING_HUMAN: &str = "awaiting_human";
pub const REASON_NO_TOOL_ROUNDS: &str = "no_tool_rounds";
pub const REASON_EXTERNAL_BLOCKER: &str = "external_blocker";
pub const REASON_PREV_TOOL_OK: &str = "prev_tool_ok";
pub const REASON_NON_RETRYABLE: &str = "non_retryable";
/// Rust-side label for the budget check; Python returns `None` from
/// `_handle_intent_stall_nudge` before classifying.
pub const REASON_BUDGET_EXHAUSTED: &str = "budget_exhausted";

/// One result's metadata within a tool round (`toolRounds[].results[]`):
/// exactly the fields `_round_failed` and `_is_non_retryable` read.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ToolResultMeta {
    /// `notRun`.
    pub not_run: bool,
    /// `badge`.
    pub badge: String,
    /// `type` (`"error"` marks an execution-layer error returned as content).
    pub result_type: String,
    /// `str(exitCode)`; `None` when absent or `None`. A numeric zero is `"0"`.
    pub exit_code: Option<String>,
    /// `rejected or _rejected` is truthy.
    pub rejected: bool,
    /// `reason or content`, as text.
    pub reason: String,
}

/// One tool round of the Turn, in append order (`task['toolRounds']`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ToolRoundOutcome {
    /// `tool or toolName`.
    pub tool_name: String,
    /// Model-batch identity (`tool_round_identity.execution_batch_keys`): the
    /// rounds of one provider response share it; a resumed attempt does not.
    pub batch: u64,
    /// `roundNum` (only carried into the chip record).
    pub round_num: Option<i64>,
    /// `status`.
    pub status: String,
    /// `str(_rejected)` when the round-level dispatcher descriptor is truthy.
    pub rejected: Option<String>,
    /// `tool_rejection_descriptor(entry)['kind']` (round or any result).
    pub rejection_kind: Option<String>,
    pub results: Vec<ToolResultMeta>,
}

/// What the loop knows when a round completed with no tool calls.
#[derive(Debug, Clone, Copy)]
pub struct StallInputs<'a> {
    /// The Turn's tool rounds so far, in append order.
    pub tool_rounds: &'a [ToolRoundOutcome],
    /// This round's assistant text (`assistant_msg['content']`).
    pub answer_text: &'a str,
    /// This round's tool calls (criterion B).
    pub answer_tool_calls: &'a [ToolCall],
    /// `task['_pendingHumanGuidance'] or task['pendingGuidanceId']` (D1c).
    pub pending_human_guidance: bool,
    /// `task['_intent_stall_nudge_count']`.
    pub nudges_used: u32,
    /// The 1-based round that just completed (for the chip anchor).
    pub round_num: i64,
}

/// `build_stall_nudge_record`: the display-only chip of an injected nudge.
#[derive(Debug, Clone, PartialEq)]
pub struct StallNudgeRecord {
    /// `round_num + 2`: anchored to the round that CONSUMES the nudge.
    pub round: i64,
    pub tool: String,
    pub failed_round: Option<i64>,
    pub badge: String,
    pub prompt: &'static str,
    pub max: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub enum StallDecision {
    /// Leave the stop alone; `reason` names the criterion that rejected it.
    Settle { reason: &'static str },
    /// Append `assistant`, then `nudge`, and issue another request.
    Nudge(Box<StallNudge>),
}

/// The rows a nudge appends, in order, and its chip record.
#[derive(Debug, Clone, PartialEq)]
pub struct StallNudge {
    /// The vetoed answer as an assistant prose row.
    pub assistant: Message,
    /// `NUDGE_TEXT` as a user-role injection.
    pub nudge: Message,
    pub record: StallNudgeRecord,
}

/// Every non-overlapping match of `\[END_TURN:[^\]]*\]`, as byte ranges.
fn end_turn_tokens(content: &str) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut pos = 0;
    while let Some(found) = content[pos..].find(END_TURN_MARKER) {
        let start = pos + found;
        let body = start + END_TURN_MARKER.len();
        let Some(close) = content[body..].find(']') else {
            break;
        };
        let end = body + close + 1;
        out.push((start, end));
        pos = end;
    }
    out
}

fn remove_end_turn_tokens(content: &str) -> String {
    let mut out = String::with_capacity(content.len());
    let mut last = 0;
    for (start, end) in end_turn_tokens(content) {
        out.push_str(&content[last..start]);
        last = end;
    }
    out.push_str(&content[last..]);
    out
}

fn py_space(ch: char) -> bool {
    py_blank(ch.encode_utf8(&mut [0u8; 4]))
}

/// `parse_end_turn_reason`: the last complete closed-set declaration wins; an
/// unknown reason reads as no declaration.
pub fn parse_end_turn_reason(content: &str) -> Option<&'static str> {
    if !content.contains(END_TURN_MARKER) {
        return None;
    }
    let mut reason = None;
    for (start, end) in end_turn_tokens(content) {
        let candidate = content[start + END_TURN_MARKER.len()..end - 1]
            .trim_matches(py_space)
            .to_lowercase();
        if let Some(valid) = VALID_END_REASONS.iter().find(|r| **r == candidate) {
            reason = Some(*valid);
        }
    }
    reason
}

/// `is_silent_turn`: exactly a bare `[SILENT]` (END_TURN tokens and
/// whitespace aside).
pub fn is_silent_turn(content: &str) -> bool {
    if !content.contains(SILENT_TURN_MARKER) {
        return false;
    }
    py_blank(&remove_end_turn_tokens(content).replace(SILENT_TURN_MARKER, ""))
}

/// `strip_silent_turn_marker`.
pub fn strip_silent_turn_marker(content: &str) -> &str {
    if is_silent_turn(content) {
        ""
    } else {
        content
    }
}

/// `strip_end_turn_marker`: remove ANY `[END_TURN:…]` token (display hygiene
/// is total, unlike the closed-set reader), collapse `\n{3,}` to `\n\n`, and
/// strip trailing whitespace. Borrowed (unchanged) when no marker is present.
pub fn strip_end_turn_marker(content: &str) -> Cow<'_, str> {
    if !content.contains(END_TURN_MARKER) {
        return Cow::Borrowed(content);
    }
    let removed = remove_end_turn_tokens(content);
    let mut out = String::with_capacity(removed.len());
    let mut newlines = 0usize;
    for ch in removed.chars() {
        if ch == '\n' {
            newlines += 1;
            continue;
        }
        push_newlines(&mut out, newlines);
        newlines = 0;
        out.push(ch);
    }
    push_newlines(&mut out, newlines);
    Cow::Owned(out.trim_end_matches(py_space).to_string())
}

fn push_newlines(out: &mut String, count: usize) {
    out.push_str(&"\n".repeat(count.min(2)));
}

/// The settled answer: `_finalize.py::_strip_end_turn_control_token` applies
/// `strip_silent_turn_marker(strip_end_turn_marker(content))`.
pub fn delivered_answer(content: &str) -> Cow<'_, str> {
    match strip_end_turn_marker(content) {
        Cow::Borrowed(text) => Cow::Borrowed(strip_silent_turn_marker(text)),
        Cow::Owned(text) => Cow::Owned(strip_silent_turn_marker(&text).to_string()),
    }
}

/// `begin_live_text_paragraph` for one channel: the separator appended to the
/// live text after a nudge so the next round's text starts a new paragraph.
/// The provisional answer keeps it: if the nudged request then fails, the
/// delivered content is `<answer>\n\n`.
pub fn paragraph_separator(live: &str) -> &'static str {
    if live.is_empty() || live.ends_with("\n\n") {
        ""
    } else if live.ends_with('\n') {
        "\n"
    } else {
        "\n\n"
    }
}

/// The trailing run of rounds sharing the last round's batch identity.
fn final_batch(rounds: &[ToolRoundOutcome]) -> &[ToolRoundOutcome] {
    let Some(last) = rounds.last() else {
        return rounds;
    };
    let start = rounds
        .iter()
        .rposition(|round| round.batch != last.batch)
        .map_or(0, |index| index + 1);
    &rounds[start..]
}

/// `known_external_blocker`: a Git guard rejection in the final batch.
fn known_external_blocker(rounds: &[ToolRoundOutcome]) -> bool {
    final_batch(rounds)
        .iter()
        .any(|round| round.rejection_kind.as_deref() == Some(GIT_GUARD_KIND))
}

/// `_round_failed` (criterion A).
pub fn round_failed(round: &ToolRoundOutcome) -> bool {
    if FAILED_STATUSES.contains(&round.status.to_lowercase().as_str()) {
        return true;
    }
    round.results.iter().any(|result| {
        result.not_run
            || FAILED_BADGES.contains(&result.badge.to_lowercase().as_str())
            || result.result_type == "error"
            || result
                .exit_code
                .as_deref()
                .is_some_and(|code| !matches!(code, "" | "0" | "background"))
    })
}

/// `_uncovered_failure`: the last failing round of the final batch.
fn uncovered_failure(rounds: &[ToolRoundOutcome]) -> Option<&ToolRoundOutcome> {
    final_batch(rounds)
        .iter()
        .rev()
        .find(|round| round_failed(round))
}

/// `_is_non_retryable` (criterion C).
fn is_non_retryable(round: &ToolRoundOutcome) -> bool {
    let reasons = round.results.iter().map(|result| result.reason.as_str());
    let blob = if round.rejected.is_some() || round.results.iter().any(|r| r.rejected) {
        let mut blob = round.rejected.clone().unwrap_or_default();
        blob.extend(reasons);
        blob
    } else {
        reasons.collect::<Vec<_>>().join(" ")
    };
    let low = blob.to_lowercase();
    NON_RETRYABLE_MARKERS
        .iter()
        .any(|marker| low.contains(marker))
}

/// `_awaiting_human` (criterion D). D1a (a handoff tool in this round's own
/// calls) is unreachable after criterion B and therefore not repeated here.
fn awaiting_human(inputs: &StallInputs<'_>) -> bool {
    inputs.tool_rounds.iter().any(|round| {
        HUMAN_HANDOFF_TOOLS.contains(&round.tool_name.as_str())
            || LIVE_WAIT_STATUSES.contains(&round.status.as_str())
    }) || inputs.pending_human_guidance
        || parse_end_turn_reason(inputs.answer_text).is_some()
}

/// `should_nudge_intent_stall`: `(should_nudge, reason)`, criteria in the
/// Python order (content, B, D, rounds, external blocker, A, C).
pub fn should_nudge_intent_stall(inputs: &StallInputs<'_>) -> (bool, &'static str) {
    if py_blank(inputs.answer_text) {
        return (false, REASON_NO_CONTENT);
    }
    if !inputs.answer_tool_calls.is_empty() {
        return (false, REASON_HAS_TOOL_CALLS);
    }
    if awaiting_human(inputs) {
        return (false, REASON_AWAITING_HUMAN);
    }
    if inputs.tool_rounds.is_empty() {
        return (false, REASON_NO_TOOL_ROUNDS);
    }
    if known_external_blocker(inputs.tool_rounds) {
        return (false, REASON_EXTERNAL_BLOCKER);
    }
    let Some(entry) = uncovered_failure(inputs.tool_rounds) else {
        return (false, REASON_PREV_TOOL_OK);
    };
    if is_non_retryable(entry) {
        return (false, REASON_NON_RETRYABLE);
    }
    (true, REASON_INTENT_STALL)
}

/// `build_stall_nudge_record`.
fn build_stall_nudge_record(inputs: &StallInputs<'_>) -> StallNudgeRecord {
    let entry = uncovered_failure(inputs.tool_rounds);
    StallNudgeRecord {
        round: inputs.round_num + 2,
        tool: entry.map(|e| e.tool_name.clone()).unwrap_or_default(),
        failed_round: entry.and_then(|e| e.round_num),
        badge: entry
            .and_then(|e| e.results.first())
            .map(|r| r.badge.clone())
            .unwrap_or_default(),
        prompt: NUDGE_TEXT,
        max: MAX_STALL_NUDGES,
    }
}

/// `_handle_intent_stall_nudge`: the budget, then the classifier.
pub fn stall_decision(inputs: &StallInputs<'_>) -> StallDecision {
    if inputs.nudges_used >= MAX_STALL_NUDGES {
        return StallDecision::Settle {
            reason: REASON_BUDGET_EXHAUSTED,
        };
    }
    match should_nudge_intent_stall(inputs) {
        (true, _) => StallDecision::Nudge(Box::new(StallNudge {
            assistant: Message::text(Role::Assistant, inputs.answer_text),
            nudge: Message::text(Role::User, NUDGE_TEXT),
            record: build_stall_nudge_record(inputs),
        })),
        (false, reason) => StallDecision::Settle { reason },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const STALL_TEXT: &str = "该钩子阻止了我的回退方案。让我仅使用显式路径。";

    fn blocked_round() -> ToolRoundOutcome {
        ToolRoundOutcome {
            tool_name: "run_command".into(),
            batch: 17,
            round_num: Some(17),
            status: "blocked".into(),
            results: vec![ToolResultMeta {
                badge: "blocked".into(),
                not_run: true,
                reason: "Tool blocked by pre-execution hook: Blocked catastrophic delete of $(git"
                    .into(),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    fn ok_round() -> ToolRoundOutcome {
        ToolRoundOutcome {
            tool_name: "read_files".into(),
            batch: 3,
            status: "ok".into(),
            results: vec![ToolResultMeta {
                badge: "ok".into(),
                exit_code: Some("0".into()),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    fn background_round(tool: &str) -> ToolRoundOutcome {
        ToolRoundOutcome {
            tool_name: tool.into(),
            batch: 1,
            round_num: Some(1),
            status: "done".into(),
            results: vec![ToolResultMeta {
                exit_code: Some("background".into()),
                badge: "background".into(),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    fn inputs<'a>(rounds: &'a [ToolRoundOutcome], text: &'a str) -> StallInputs<'a> {
        StallInputs {
            tool_rounds: rounds,
            answer_text: text,
            answer_tool_calls: &[],
            pending_human_guidance: false,
            nudges_used: 0,
            round_num: 18,
        }
    }

    fn verdict(rounds: &[ToolRoundOutcome], text: &str) -> (bool, &'static str) {
        should_nudge_intent_stall(&inputs(rounds, text))
    }

    // tests/tasks_pkg/test_intent_stall_nudge.py

    #[test]
    fn the_ground_truth_sample_is_nudged() {
        assert_eq!(
            verdict(&[blocked_round()], STALL_TEXT),
            (true, "intent_stall")
        );
    }

    #[test]
    fn an_errored_round_also_counts_not_just_a_blocked_one() {
        let mut errored = ok_round();
        errored.results[0] = ToolResultMeta {
            badge: "error".into(),
            exit_code: Some("1".into()),
            ..Default::default()
        };
        assert!(verdict(&[errored], STALL_TEXT).0);
    }

    #[test]
    fn a_successful_previous_round_is_not_a_stall() {
        let text = "Done. All 27 tests pass.";
        assert_eq!(verdict(&[ok_round()], text), (false, "prev_tool_ok"));
    }

    #[test]
    fn a_round_that_still_called_a_tool_is_not_a_stall() {
        let rounds = [blocked_round()];
        let calls = [ToolCall {
            id: "c1".into(),
            name: "run_command".into(),
            arguments: "{}".into(),
        }];
        let mut probe = inputs(&rounds, "Retrying with explicit paths.");
        probe.answer_tool_calls = &calls;
        assert_eq!(should_nudge_intent_stall(&probe), (false, "has_tool_calls"));
    }

    #[test]
    fn an_absent_tool_is_never_nudged() {
        let absent = ToolRoundOutcome {
            tool_name: "project_board_complete".into(),
            batch: 9,
            status: "rejected".into(),
            rejected: Some("{'attempted': 'project_board_complete'}".into()),
            results: vec![ToolResultMeta {
                badge: "error".into(),
                reason: "Error: `project_board_complete` is not a real tool and was NOT \
                         executed. It is not in the list of tools available to you this turn."
                    .into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        assert_eq!(
            verdict(&[absent], "Let me mark it done another way."),
            (false, "non_retryable")
        );
    }

    #[test]
    fn a_hand_back_to_the_user_is_never_nudged() {
        let ask = ToolRoundOutcome {
            tool_name: "ask_user".into(),
            batch: 18,
            status: "ok".into(),
            ..Default::default()
        };
        assert_eq!(
            verdict(&[blocked_round(), ask], "要我按 A 还是 B 走?"),
            (false, "awaiting_human")
        );
    }

    #[test]
    fn a_prose_hand_back_is_suppressed_by_the_self_declaration() {
        let text = "两条路你定。[END_TURN: awaiting_human]";
        assert_eq!(verdict(&[blocked_round()], text), (false, "awaiting_human"));
    }

    #[test]
    fn an_explicit_done_or_blocked_declaration_also_stops() {
        for declared in ["done", "blocked"] {
            let text = format!("收工。[END_TURN: {declared}]");
            assert_eq!(
                verdict(&[blocked_round()], &text),
                (false, "awaiting_human")
            );
        }
    }

    #[test]
    fn an_invented_end_reason_does_not_suppress_the_nudge() {
        let text = "继续。[END_TURN: whatever_i_want]";
        assert_eq!(parse_end_turn_reason(text), None);
        assert!(verdict(&[blocked_round()], text).0);
    }

    #[test]
    fn wording_is_not_a_criterion_in_either_direction() {
        assert!(verdict(&[blocked_round()], "嗯。").0);
        assert_eq!(
            verdict(&[ok_round()], "让我立刻用显式路径重跑一遍。"),
            (false, "prev_tool_ok")
        );
    }

    #[test]
    fn the_analyser_re_drives_the_loop_and_appends_the_nudge() {
        let rounds = [blocked_round()];
        let StallDecision::Nudge(nudged) = stall_decision(&inputs(&rounds, STALL_TEXT)) else {
            panic!("the intent stall must re-drive the loop");
        };
        let StallNudge {
            assistant,
            nudge,
            record,
        } = *nudged;
        assert_eq!(assistant, Message::text(Role::Assistant, STALL_TEXT));
        assert_eq!(nudge, Message::text(Role::User, NUDGE_TEXT));
        // tests/diagnostics/test_stall_nudge_lane_e2e.py::
        // test_the_record_carries_the_trigger_provenance
        assert_eq!(
            record,
            StallNudgeRecord {
                round: 20,
                tool: "run_command".into(),
                failed_round: Some(17),
                badge: "blocked".into(),
                prompt: NUDGE_TEXT,
                max: 1,
            }
        );
    }

    #[test]
    fn the_nudge_is_bounded_to_one_per_task() {
        let rounds = [blocked_round()];
        let mut probe = inputs(&rounds, STALL_TEXT);
        probe.nudges_used = 1;
        assert_eq!(
            stall_decision(&probe),
            StallDecision::Settle {
                reason: REASON_BUDGET_EXHAUSTED
            }
        );
    }

    #[test]
    fn a_normal_stop_is_untouched() {
        let rounds = [ok_round()];
        assert_eq!(
            stall_decision(&inputs(&rounds, "All done.")),
            StallDecision::Settle {
                reason: "prev_tool_ok"
            }
        );
    }

    #[test]
    fn live_structured_wait_suppresses_stall() {
        for status in ["pending_approval", "awaiting_human", "awaiting_stdin"] {
            let wait = ToolRoundOutcome {
                tool_name: "run_command".into(),
                status: status.into(),
                ..Default::default()
            };
            assert_eq!(
                verdict(&[blocked_round(), wait], STALL_TEXT),
                (false, "awaiting_human")
            );
        }
    }

    #[test]
    fn pending_human_guidance_suppresses_stall() {
        let rounds = [blocked_round()];
        let mut probe = inputs(&rounds, STALL_TEXT);
        probe.pending_human_guidance = true;
        assert_eq!(should_nudge_intent_stall(&probe), (false, "awaiting_human"));
    }

    #[test]
    fn end_turn_declaration_requires_closing_bracket() {
        for reason in ["done", "blocked", "awaiting_human"] {
            assert_eq!(
                parse_end_turn_reason(&format!("Status. [END_TURN: {reason}")),
                None
            );
            assert_eq!(
                parse_end_turn_reason(&format!("Status. [END_TURN: {reason}]")),
                Some(reason)
            );
        }
    }

    #[test]
    fn unknown_token_cannot_hide_later_explicit_handoff() {
        let text = "[END_TURN: unknown] Status. [END_TURN: awaiting_human]";
        assert_eq!(parse_end_turn_reason(text), Some("awaiting_human"));
        assert_eq!(verdict(&[blocked_round()], text), (false, "awaiting_human"));
    }

    #[test]
    fn the_nudge_names_the_structural_fact_and_teaches_the_contract() {
        assert!(NUDGE_TEXT.contains("rejected or failed"));
        assert!(NUDGE_TEXT.contains("may already have executed"));
        assert!(NUDGE_TEXT.contains("do not blindly"));
        assert!(!NUDGE_TEXT.contains("so it never executed"));
        for reason in ["awaiting_human", "done", "blocked"] {
            assert!(NUDGE_TEXT.contains(&format!("{END_TURN_MARKER} {reason}]")));
        }
    }

    #[test]
    fn git_guard_is_external_blocker_without_model_end_token() {
        let guarded = ToolRoundOutcome {
            tool_name: "run_command".into(),
            batch: 3,
            status: "rejected".into(),
            rejection_kind: Some("git_guard".into()),
            ..Default::default()
        };
        let mut sibling = ok_round();
        sibling.batch = 3;
        let mut rounds = vec![guarded, sibling];
        assert_eq!(verdict(&rounds, STALL_TEXT), (false, "external_blocker"));
        let mut later = ok_round();
        later.batch = 4;
        rounds.push(later);
        assert_eq!(verdict(&rounds, STALL_TEXT), (false, "prev_tool_ok"));
    }

    #[test]
    fn empty_answer_is_not_a_stall() {
        assert_eq!(verdict(&[blocked_round()], " \n"), (false, "no_content"));
        assert_eq!(verdict(&[], STALL_TEXT), (false, "no_tool_rounds"));
    }

    // tests/tasks_pkg/test_intent_stall_background.py

    #[test]
    fn background_launch_does_not_nudge_duplicate_execution() {
        for tool in ["run_command", "code_exec"] {
            assert_eq!(
                verdict(
                    &[background_round(tool)],
                    "The command is running in the background."
                ),
                (false, "prev_tool_ok")
            );
        }
    }

    #[test]
    fn background_flag_does_not_hide_structural_failure() {
        let failures: [fn(&mut ToolResultMeta); 7] = [
            |r| r.exit_code = Some("1".into()),
            |r| r.exit_code = Some("2".into()),
            |r| r.exit_code = Some("timeout".into()),
            |r| r.exit_code = Some("not-run".into()),
            |r| r.not_run = true,
            |r| r.badge = "error".into(),
            |r| r.result_type = "error".into(),
        ];
        for apply in failures {
            let mut entry = background_round("run_command");
            apply(&mut entry.results[0]);
            assert_eq!(verdict(&[entry], "bg"), (true, "intent_stall"));
        }
    }

    #[test]
    fn background_receipt_does_not_hide_failed_round_status() {
        let mut entry = background_round("run_command");
        entry.status = "failed".into();
        assert_eq!(verdict(&[entry], "bg"), (true, "intent_stall"));
    }

    #[test]
    fn background_sibling_does_not_hide_failure_in_same_batch() {
        let mut failed = background_round("run_command");
        failed.results[0] = ToolResultMeta {
            exit_code: Some("1".into()),
            badge: "exit 1".into(),
            ..Default::default()
        };
        let background = background_round("code_exec");
        for rounds in [
            [failed.clone(), background.clone()],
            [background.clone(), failed.clone()],
        ] {
            assert_eq!(verdict(&rounds, "bg"), (true, "intent_stall"));
        }
    }

    #[test]
    fn background_result_does_not_hide_failure_in_same_round() {
        let mut entry = background_round("run_command");
        entry.results.push(ToolResultMeta {
            exit_code: Some("1".into()),
            ..Default::default()
        });
        assert_eq!(verdict(&[entry], "bg"), (true, "intent_stall"));
    }

    // tests/tasks_pkg/test_intent_stall_error_string_blindness.py (the
    // `type: error` stamp is produced by `build_project_tool_meta`, upstream
    // of this module; content mentioning "Error:" is never read here).

    fn error_as_content(batch: u64, round_num: i64) -> ToolRoundOutcome {
        ToolRoundOutcome {
            tool_name: "read_files".into(),
            batch,
            round_num: Some(round_num),
            status: "done".into(),
            results: vec![ToolResultMeta {
                badge: "21961L".into(),
                result_type: "error".into(),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    fn success_mentioning_error(batch: u64, round_num: i64) -> ToolRoundOutcome {
        ToolRoundOutcome {
            tool_name: "grep_search".into(),
            batch,
            round_num: Some(round_num),
            status: "done".into(),
            results: vec![ToolResultMeta {
                badge: "12 matches".into(),
                reason: "Error: requested line range 6171-6162 is empty".into(),
                exit_code: Some("0".into()),
                ..Default::default()
            }],
            ..Default::default()
        }
    }

    #[test]
    fn successful_rounds_whose_content_mentions_error_are_not_failures() {
        assert!(!round_failed(&success_mentioning_error(6, 11)));
        assert!(round_failed(&error_as_content(25, 35)));
    }

    #[test]
    fn recovered_failure_in_an_earlier_batch_is_covered() {
        let rounds = [error_as_content(25, 35), success_mentioning_error(26, 36)];
        assert_eq!(verdict(&rounds, STALL_TEXT), (false, "prev_tool_ok"));
    }

    #[test]
    fn a_failure_in_the_final_batch_is_uncovered_even_beside_a_success() {
        let rounds = [error_as_content(9, 40), success_mentioning_error(9, 41)];
        assert_eq!(
            uncovered_failure(&rounds).and_then(|r| r.round_num),
            Some(40)
        );
        assert_eq!(verdict(&rounds, STALL_TEXT), (true, "intent_stall"));
    }

    #[test]
    fn a_stall_directly_after_the_error_round_does_fire() {
        assert_eq!(
            verdict(&[error_as_content(25, 35)], STALL_TEXT),
            (true, "intent_stall")
        );
    }

    // tests/tasks_pkg/test_intent_stall_ui_contract.py

    #[test]
    fn the_control_token_is_removed_from_the_answer() {
        for (body, expect) in [
            (
                "Done — the migration is applied.\n\n[END_TURN: done]",
                "Done — the migration is applied.",
            ),
            (
                "I need your call on this.\n[END_TURN: awaiting_human]",
                "I need your call on this.",
            ),
            (
                "Blocked on credentials.\n\n\n[END_TURN: blocked]",
                "Blocked on credentials.",
            ),
            (
                "First part. [END_TURN: done] Second part.",
                "First part.  Second part.",
            ),
        ] {
            assert_eq!(strip_end_turn_marker(body), expect);
        }
    }

    #[test]
    fn an_invented_reason_is_also_stripped() {
        let text = "All set.\n\n[END_TURN: banana]";
        assert_eq!(parse_end_turn_reason(text), None);
        assert_eq!(strip_end_turn_marker(text), "All set.");
    }

    #[test]
    fn ordinary_prose_is_returned_untouched() {
        let body = "Here is the summary you asked for.\n\nNo control tokens in sight.";
        assert!(matches!(strip_end_turn_marker(body), Cow::Borrowed(b) if std::ptr::eq(b, body)));
    }

    #[test]
    fn stripping_is_idempotent() {
        let once = strip_end_turn_marker("Done.\n\n[END_TURN: done]").into_owned();
        assert_eq!(strip_end_turn_marker(&once), once);
    }

    #[test]
    fn bracketed_prose_that_is_not_the_token_survives() {
        let body = "See [END_TURN_MARKER] in the docs, and [note: keep this].";
        assert_eq!(strip_end_turn_marker(body), body);
    }

    #[test]
    fn interior_blank_runs_collapse_only_after_a_token() {
        assert_eq!(
            strip_end_turn_marker("a\n\n\n\nb [END_TURN: done]\n\n"),
            "a\n\nb"
        );
        assert_eq!(strip_end_turn_marker("a\n\nb\n[END_TURN: x]"), "a\n\nb");
    }

    #[test]
    fn the_nudge_text_teaches_exactly_the_token_the_stripper_removes() {
        let taught = end_turn_tokens(NUDGE_TEXT);
        assert_eq!(taught.len(), 3);
        for (start, end) in taught {
            let token = &NUDGE_TEXT[start..end];
            assert_eq!(
                strip_end_turn_marker(&format!("Answer.\n\n{token}")),
                "Answer."
            );
        }
    }

    #[test]
    fn the_exact_nudge_text() {
        assert_eq!(
            NUDGE_TEXT,
            "[SYSTEM: UNRESOLVED TOOL FAILURE]\nA previous tool call was rejected or failed, \
             and no later tool result has resolved that failure. This response contains no \
             tool calls.\n\nInspect the failure evidence before deciding the next action. A \
             failed command may already have executed and changed state; do not blindly repeat \
             side effects. If recovery is needed, use an appropriate tool, adjusting invalid \
             arguments or checking existing state first.\nIf you are waiting on the user, or \
             the task is genuinely finished or blocked, end your turn with [END_TURN: \
             awaiting_human], [END_TURN: done] or [END_TURN: blocked] so this is not mistaken \
             for an interrupted turn."
        );
    }

    // SILENT marker (tests/router/test_router_silence.py covers the lane).

    #[test]
    fn only_a_bare_silent_token_is_silence() {
        assert!(is_silent_turn("[SILENT]"));
        assert!(is_silent_turn("  [SILENT]\n[END_TURN: done]"));
        assert!(!is_silent_turn("Nothing to do. [SILENT]"));
        assert!(!is_silent_turn("silent"));
        assert_eq!(strip_silent_turn_marker("[SILENT]"), "");
        assert_eq!(strip_silent_turn_marker("x [SILENT]"), "x [SILENT]");
    }

    // tests/tasks_pkg/test_vetoed_answer_ownership.py

    #[test]
    fn the_next_message_starts_a_new_paragraph() {
        for (live, separator) in [
            ("answer", "\n\n"),
            ("answer\n", "\n"),
            ("answer\n\n", ""),
            ("", ""),
        ] {
            assert_eq!(paragraph_separator(live), separator);
        }
    }

    #[test]
    fn the_stall_nudge_keeps_the_vetoed_answer_as_the_provisional_answer() {
        let answer = "The worktree is clean.";
        let mut live = answer.to_string();
        live.push_str(paragraph_separator(&live));
        // The nudged request fails: the provisional answer is delivered.
        assert_eq!(delivered_answer(&live), format!("{answer}\n\n"));
        // The follow-up only declares the end of the turn.
        live.push_str("[END_TURN: done]");
        assert_eq!(delivered_answer(&live), answer);
    }

    #[test]
    fn a_provisional_answer_followed_by_prose_only_renders_once() {
        let mut live = "ANSWER".to_string();
        live.push_str(paragraph_separator(&live));
        live.push_str("ADDENDUM\n\n[END_TURN: done]");
        assert_eq!(delivered_answer(&live), "ANSWER\n\nADDENDUM");
    }
}
