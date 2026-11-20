//! Compaction admission and adoption policy: an overfull prompt is refused,
//! never head-truncated; a summary replaces sources only when verified
//! complete and durably archived; the active checklist always survives.
//!
//! Replaces, for the engine loop:
//! - `lib/tasks_pkg/compaction/_admission_budget.py::resolve_admission_budget`
//!   with `_tokens.py::_usable_context` (:631) and
//!   `_compaction_trigger_threshold` (:805), constants from `_constants.py`
//!   (`_OUTPUT_RESERVE`, `_COMPACTION_RESERVE`, `_SUMMARY_TRIGGER_RATIO`) and
//!   `_tokens.py::_MIN_USABLE_RATIO`;
//! - the decisions of `_prompt_admission.py::enforce_dispatch_prompt_limit`
//!   (:201), including `_MIN/_MAX_SUMMARY_PRESERVE_TOKENS`;
//! - the completion gate of `_layer2/_summary_transport.py` (:139, :218) and
//!   `_layer2/_summary.py` (`summary_empty`);
//! - the joint L1/L2 detached candidate and its durable adoption
//!   (`_layer2/_preparation.py`, `_projection.py`, `_compact.py`);
//! - `llm_fallback/_credential_recovery.py::call_with_credential_recovery`
//!   (`_CREDENTIAL_PREPARE_MAX_ATTEMPTS`, `_CREDENTIAL_PREPARE_RECOVERY_SECONDS`);
//! - `context_composer/checklist.py` restoration with `lib/tools/todo.py`
//!   rendering and bounds.
//!
//! Contract: `docs/CONTEXT_COMPACTION.md` "Complete summaries without a
//! wall-clock deadline", "One provider-independent compaction owner"
//! (every L2 rewrite is this local compactor; no provider compaction control
//! exists in this API), "Durable adoption and Continue", "Credential-outage
//! robustness", "Authoritative checklist restoration".
//!
//! Entry points, in loop order before each provider request:
//! `restore_checklist` → (caller measures tokens) → `AdmissionBudget::resolve`
//! → `admit` → on `CompactionRequired`: build a `CompactionCandidate` (the L1
//! projection is the caller's), `summary_verdict` on the summary outcome
//! (retrying credential failures with `credential_retry`), `carry_checklist`,
//! re-measure, `CompactionCandidate::adoption` → archive through the store →
//! `PendingAdoption::commit` → `admit_after_compaction`.
//! Token counts are inputs; the token counter is not part of this module.
//! Pure: no I/O, no clock (elapsed time is an input).

use std::time::Duration;

use serde_json::{json, Value};

use super::types::{Message, ProviderOutcome, Role};

/// `_constants._OUTPUT_RESERVE`.
pub const OUTPUT_RESERVE_TOKENS: u64 = 128_000;
/// `_constants._COMPACTION_RESERVE`.
pub const COMPACTION_RESERVE_TOKENS: u64 = 8_000;
/// `_tokens._MIN_USABLE_RATIO`.
pub const MIN_USABLE_RATIO: f64 = 0.7;
/// `_constants._SUMMARY_TRIGGER_RATIO`.
pub const SUMMARY_TRIGGER_RATIO: f64 = 0.90;
/// `_prompt_admission._MIN_SUMMARY_PRESERVE_TOKENS` / `_MAX_…`.
pub const MIN_SUMMARY_PRESERVE_TOKENS: u64 = 8_000;
pub const MAX_SUMMARY_PRESERVE_TOKENS: u64 = 32_000;
/// Finish reasons of a complete summary (`_summary_transport.py`).
pub const COMPLETE_SUMMARY_FINISH_REASONS: [&str; 4] =
    ["stop", "end_turn", "stop_sequence", "completed"];
/// `_credential_recovery._CREDENTIAL_PREPARE_MAX_ATTEMPTS`.
pub const CREDENTIAL_PREPARE_MAX_ATTEMPTS: u32 = 3;
/// `_credential_recovery._CREDENTIAL_PREPARE_RECOVERY_SECONDS`.
pub const CREDENTIAL_PREPARE_RECOVERY: Duration = Duration::from_secs(30);
/// `lib/tools/todo.py` bounds.
pub const TODO_MAX_ITEMS: usize = 24;
pub const TODO_MAX_CONTENT_CHARS: usize = 512;
pub const TODO_MAX_EXPLANATION_CHARS: usize = 2048;
pub const TODO_MAX_RESTORED_ITEMS: usize = 144;
/// `checklist.CHECKLIST_HEADING`.
pub const CHECKLIST_HEADING: &str = "## Active Task Checklist";

/// `_usable_context`: tokens left after output and compaction reserves,
/// floored at 70 % of the window.
pub fn usable_context(window: u64) -> u64 {
    let raw = window as i64 - (OUTPUT_RESERVE_TOKENS + COMPACTION_RESERVE_TOKENS) as i64;
    raw.max((window as f64 * MIN_USABLE_RATIO) as i64).max(0) as u64
}

/// `_compaction_trigger_threshold`'s window safety.
pub fn window_safety(window: u64) -> u64 {
    (usable_context(window) as f64 * SUMMARY_TRIGGER_RATIO) as u64
}

/// The physical limits of one dispatch (`resolve_admission_budget`). There is
/// no round or economic input: a first round or a working-set target never
/// reduces route capacity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AdmissionBudget {
    pub context_window: u64,
    pub window_safety: u64,
    /// The resolved output reservation of this request.
    pub output_tokens: u64,
    /// The prompt ceiling; also the admission target.
    pub hard_ceiling: u64,
}

impl AdmissionBudget {
    pub fn resolve(context_window: u64, output_tokens: u64) -> Self {
        let safety = window_safety(context_window);
        let hard_ceiling = safety
            .max(1)
            .min(context_window.saturating_sub(output_tokens).max(1));
        Self {
            context_window,
            window_safety: safety,
            output_tokens,
            hard_ceiling,
        }
    }
}

/// One full-prompt measurement (messages plus tool schemas), from the token
/// counter outside this module.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PromptMeasurement {
    pub total_tokens: u64,
    pub tool_schema_tokens: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Admission {
    Admit,
    /// The prompt is over the ceiling: compact on a detached candidate, the
    /// recent tail preserved within `preserve_budget_tokens`.
    CompactionRequired {
        message_target: u64,
        preserve_budget_tokens: u64,
    },
    /// Refused; no main-provider request is sent and no history is removed.
    Refuse(Refusal),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refusal {
    /// `PromptTooLongError(reason='tool_schema_floor')`.
    ToolSchemaFloor {
        tool_schema_tokens: u64,
        hard_ceiling: u64,
    },
    /// `LocalRequestPreparationError(stage='prompt_admission_summary')`:
    /// summary credentials unavailable and the prompt still over.
    SummaryCredentialsUnavailable { total_tokens: u64 },
    /// `ContextCompactionError`: a known local summary failure.
    CompactionFailed { reason: String, total_tokens: u64 },
    /// `ContextCompactionError`: compaction did not get under the ceiling.
    StillOverCeiling {
        total_tokens: u64,
        hard_ceiling: u64,
    },
}

/// The first decision of `enforce_dispatch_prompt_limit`.
pub fn admit(measured: PromptMeasurement, budget: &AdmissionBudget) -> Admission {
    let target = budget.hard_ceiling;
    if measured.total_tokens <= target {
        return Admission::Admit;
    }
    if target <= measured.tool_schema_tokens {
        return Admission::Refuse(Refusal::ToolSchemaFloor {
            tool_schema_tokens: measured.tool_schema_tokens,
            hard_ceiling: budget.hard_ceiling,
        });
    }
    let message_target = target - measured.tool_schema_tokens;
    Admission::CompactionRequired {
        message_target,
        preserve_budget_tokens: (message_target / 4)
            .clamp(MIN_SUMMARY_PRESERVE_TOKENS, MAX_SUMMARY_PRESERVE_TOKENS),
    }
}

/// How the compaction attempt ended, as `admit_after_compaction` needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompactionResult {
    /// A candidate was adopted durably.
    Adopted,
    /// Nothing was adopted; `failure` names a known local summary failure
    /// (`SummaryFailure::code`), `None` when the attempt simply did not help.
    Kept { failure: Option<String> },
}

/// The decision after compaction (`enforce_dispatch_prompt_limit` tail).
/// There is no truncation outcome: a prompt that still does not fit is
/// refused with its typed reason.
pub fn admit_after_compaction(
    after: PromptMeasurement,
    budget: &AdmissionBudget,
    result: &CompactionResult,
) -> Admission {
    if after.total_tokens <= budget.hard_ceiling {
        return Admission::Admit;
    }
    match result {
        CompactionResult::Kept {
            failure: Some(code),
        } => {
            let fits = after.total_tokens <= budget.hard_ceiling.min(budget.window_safety)
                && after.total_tokens + budget.output_tokens <= budget.context_window
                && budget.output_tokens > 0;
            if fits {
                return Admission::Admit;
            }
            if code == SummaryFailure::CredentialsUnavailable.code() {
                return Admission::Refuse(Refusal::SummaryCredentialsUnavailable {
                    total_tokens: after.total_tokens,
                });
            }
            Admission::Refuse(Refusal::CompactionFailed {
                reason: code.clone(),
                total_tokens: after.total_tokens,
            })
        }
        _ => Admission::Refuse(Refusal::StillOverCeiling {
            total_tokens: after.total_tokens,
            hard_ceiling: budget.hard_ceiling,
        }),
    }
}

/// Summary failure codes (`_summary_failure.SUMMARY_FAILURE_CODES` subset the
/// engine produces).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SummaryFailure {
    Empty,
    Incomplete,
    Cancelled,
    DispatchFailed,
    CredentialsUnavailable,
}

impl SummaryFailure {
    pub fn code(self) -> &'static str {
        match self {
            SummaryFailure::Empty => "summary_empty",
            SummaryFailure::Incomplete => "summary_incomplete",
            SummaryFailure::Cancelled => "summary_cancelled",
            SummaryFailure::DispatchFailed => "summary_dispatch_failed",
            SummaryFailure::CredentialsUnavailable => "summary_credentials_unavailable",
        }
    }
}

/// Only a nonempty, provider-finished normal completion can become a
/// candidate. Length-limited, filtered, tool-call and unverified endings are
/// rejected and never clipped; elapsed time plays no part.
pub fn summary_verdict(outcome: &ProviderOutcome) -> Result<String, SummaryFailure> {
    match outcome {
        ProviderOutcome::Completed {
            text,
            tool_calls,
            finish_reason,
            ..
        } => {
            let finish = finish_reason.to_lowercase();
            if !tool_calls.is_empty() || !COMPLETE_SUMMARY_FINISH_REASONS.contains(&finish.as_str())
            {
                return Err(SummaryFailure::Incomplete);
            }
            if text.trim().is_empty() {
                return Err(SummaryFailure::Empty);
            }
            Ok(text.clone())
        }
        ProviderOutcome::Cancelled { .. } => Err(SummaryFailure::Cancelled),
        ProviderOutcome::ContentFilter { .. } | ProviderOutcome::Dropped { .. } => {
            Err(SummaryFailure::Incomplete)
        }
        ProviderOutcome::HttpError { .. } | ProviderOutcome::Internal { .. } => {
            Err(SummaryFailure::DispatchFailed)
        }
    }
}

/// A pre-ingress credential preparation failure of the summary request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CredentialFailure {
    /// The typed source declared the outage transient.
    pub transient: bool,
    /// Any provider ingress or work was observed.
    pub provider_reached: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialRetry {
    /// Wait this long (cancellable), then call `credential_retry_after_wait`.
    RetryAfter(Duration),
    /// Settle the attempt failed with its authentic cause.
    GiveUp,
}

/// `call_with_credential_recovery` after failed attempt `attempt` (1-based)
/// at `elapsed` since the first: at most three attempts within 30 seconds,
/// backing off `attempt` seconds, the same request each time. Permanent
/// faults, observed provider work and a delivered tool call never retry.
pub fn credential_retry(
    attempt: u32,
    elapsed: Duration,
    failure: CredentialFailure,
    retry_allowed: bool,
) -> CredentialRetry {
    if !failure.transient
        || failure.provider_reached
        || !retry_allowed
        || attempt >= CREDENTIAL_PREPARE_MAX_ATTEMPTS
        || elapsed >= CREDENTIAL_PREPARE_RECOVERY
    {
        return CredentialRetry::GiveUp;
    }
    let left = CREDENTIAL_PREPARE_RECOVERY - elapsed;
    CredentialRetry::RetryAfter(left.min(Duration::from_secs(u64::from(attempt))))
}

/// After the backoff: retry only while the 30-second window is open.
pub fn credential_retry_after_wait(elapsed: Duration) -> bool {
    elapsed < CREDENTIAL_PREPARE_RECOVERY
}

/// The terminal receipt of a failed compaction attempt: one honest failed
/// row instead of a stuck "compressing" ghost.
pub fn failed_attempt_receipt(trigger: &str, failure: SummaryFailure) -> Value {
    json!({"status": "failed", "trigger": trigger, "outcomeReason": failure.code(),
           "summary": {"accepted": false, "failure": {"code": failure.code()}}})
}

/// One accepted checklist row (`lib/tools/todo.py`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChecklistItem {
    pub content: String,
    /// `pending`, `in_progress`, `blocked` or `completed`.
    pub status: String,
}

/// The task's accepted checklist, including an explicitly empty one.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ChecklistSnapshot {
    pub items: Vec<ChecklistItem>,
    pub explanation: String,
}

impl ChecklistSnapshot {
    /// New writes: 24 items × 512 characters and a 2,048-character
    /// explanation; a restored legacy checkpoint may hold up to 144 rows.
    pub fn within_bounds(&self, restoring: bool) -> bool {
        let limit = if restoring {
            TODO_MAX_RESTORED_ITEMS
        } else {
            TODO_MAX_ITEMS
        };
        self.items.len() <= limit
            && self
                .items
                .iter()
                .all(|i| i.content.chars().count() <= TODO_MAX_CONTENT_CHARS)
            && self.explanation.chars().count() <= TODO_MAX_EXPLANATION_CHARS
    }

    /// `render_todo_snapshot(state, explanation_label=...)`.
    pub fn render(&self, explanation_label: &str) -> String {
        let lines: Vec<String> = self
            .items
            .iter()
            .map(|item| {
                let box_ = if item.status == "completed" {
                    "[x]"
                } else {
                    "[ ]"
                };
                let marker = match item.status.as_str() {
                    "in_progress" => " \u{23f3}",
                    "blocked" => " [blocked]",
                    _ => "",
                };
                format!("- {box_} {}{marker}", item.content)
            })
            .collect();
        let body = if lines.is_empty() {
            "The accepted todo list is empty.".to_string()
        } else {
            lines.join("\n")
        };
        if self.explanation.is_empty() {
            body
        } else {
            format!("{body}\n{explanation_label}: {}", self.explanation)
        }
    }

    /// The complete `active_checklist` envelope (`_snapshot_body` in `_envelope`).
    pub fn carrier_text(&self) -> String {
        let body = [
            CHECKLIST_HEADING.to_string(),
            "Accepted task progress preserved by the runtime. Restoration is not an update. \
             Status reports progress, not proof of success. Use only currently available tools \
             for any actual update."
                .to_string(),
            self.render("Progress explanation from the accepted todo update"),
        ]
        .join("\n");
        format!(
            "<!-- tofu-context:active_checklist:start -->\n<system-reminder>\n\
             [Context authority: workflow; source: task.todo_state]\n{body}\n</system-reminder>\n\
             <!-- tofu-context:active_checklist:end -->"
        )
    }

    /// An accepted todo result proves visibility (`_accepted_result`).
    fn accepted_result(&self, text: &str) -> bool {
        let Some((prefix, body)) = text.split_once('\n') else {
            return false;
        };
        (prefix.starts_with("Checklist ")
            || prefix == "todo_list load: current checklist (no state change)."
            || prefix == "todo_list lookup: current checklist (no state change).")
            && body == self.render("Explanation")
    }
}

/// `_evidence_scan` for carriers and direct todo results: whether the model
/// can see the complete current checklist in `messages`.
pub fn checklist_visible(messages: &[Message], snapshot: &ChecklistSnapshot) -> bool {
    let carrier = snapshot.carrier_text();
    let mut open: Vec<(String, String)> = Vec::new();
    let mut visible = false;
    for message in messages {
        match message.role {
            Role::Assistant => {
                open = message
                    .tool_calls
                    .iter()
                    .map(|c| (c.id.clone(), c.name.clone()))
                    .collect();
            }
            Role::User if message.content.matches(carrier.as_str()).count() == 1 => visible = true,
            Role::Tool => {
                let id = message.tool_call_id.as_deref().unwrap_or("");
                if let Some(position) = open.iter().position(|(call, _)| call == id) {
                    let (_, name) = open.remove(position);
                    if matches!(name.as_str(), "todo_write" | "todo_list")
                        && snapshot.accepted_result(&message.content)
                    {
                        visible = true;
                    }
                }
            }
            _ => {}
        }
    }
    visible
}

/// `restore_checklist_context`: append the complete current checklist as one
/// required user-role carrier when the visible history lost it. Never
/// truncated; only appends, so sent bytes stay identical. Call before every
/// final measurement (initial, after compaction, reactive retry).
pub fn restore_checklist(
    messages: &mut Vec<Message>,
    snapshot: Option<&ChecklistSnapshot>,
) -> bool {
    let Some(snapshot) = snapshot else {
        return false;
    };
    if checklist_visible(messages, snapshot) {
        return false;
    }
    messages.push(Message::text(Role::User, snapshot.carrier_text()));
    true
}

/// A detached L1+L2 candidate. `source` is the live history it was planned
/// from and is never mutated; `l1_projection` is the deterministic L1 view
/// (the caller's transforms) from which `summarized` whole units were chosen.
#[derive(Debug, Clone, PartialEq)]
pub struct CompactionCandidate {
    pub source: Vec<Message>,
    pub l1_projection: Vec<Message>,
    /// Half-open range of `l1_projection` replaced by the summary.
    pub summarized: (usize, usize),
    /// Matching range of `source`: the original observations the summary
    /// reads and the archive stores.
    pub source_units: (usize, usize),
}

impl CompactionCandidate {
    /// The summary request reads original evidence, never L1 previews.
    pub fn summary_input(&self) -> &[Message] {
        &self.source[self.source_units.0..self.source_units.1]
    }

    /// The projected view with `summary` installed, the checklist carried.
    /// `None` when the summary is not a verified complete output.
    pub fn adoption(
        &self,
        summary: Result<String, SummaryFailure>,
        checklist: Option<&ChecklistSnapshot>,
    ) -> Result<PendingAdoption, SummaryFailure> {
        let summary = summary?;
        let (start, end) = self.summarized;
        let mut view = self.l1_projection[..start].to_vec();
        view.push(Message::text(Role::User, summary.clone()));
        view.extend_from_slice(&self.l1_projection[end..]);
        restore_checklist(&mut view, checklist);
        let archive = json!({
            "trigger": "compaction",
            "sourceMessageCount": self.source.len(),
            "summarizedSourceRange": [self.source_units.0, self.source_units.1],
            "summary": {"accepted": true, "complete": true, "text": summary},
        });
        Ok(PendingAdoption { view, archive })
    }
}

/// A verified candidate waiting for its archive's storage acknowledgement.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingAdoption {
    pub view: Vec<Message>,
    /// The payload the loop archives (`DurableStore`) before adopting.
    pub archive: Value,
}

impl PendingAdoption {
    /// Adopt only after an exact storage ack; any failure keeps the sources.
    /// `fits` is the re-measured candidate's admission (over-budget or
    /// uneconomic candidates are not adopted either).
    pub fn commit<E>(
        self,
        archived: Result<(), E>,
        fits: bool,
        live: &mut Vec<Message>,
    ) -> CompactionResult {
        if archived.is_err() || !fits {
            return CompactionResult::Kept { failure: None };
        }
        *live = self.view;
        CompactionResult::Adopted
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::types::ToolCall;

    fn completed(text: &str, finish: &str) -> ProviderOutcome {
        ProviderOutcome::Completed {
            text: text.into(),
            tool_calls: vec![],
            finish_reason: finish.into(),
            usage: json!({}),
        }
    }

    fn measured(total: u64, tools: u64) -> PromptMeasurement {
        PromptMeasurement {
            total_tokens: total,
            tool_schema_tokens: tools,
        }
    }

    /// Row "an overfull prompt is refused, never head-truncated"
    /// (CONTEXT_COMPACTION.md "Complete summaries without a wall-clock
    /// deadline"); pins `tests/compaction/test_summary_completion_policy.py`
    /// and `tests/goals/test_checklist_context.py::
    /// test_required_checklist_overflow_refuses_instead_of_dispatching_partial_state`.
    #[test]
    fn an_overfull_prompt_is_refused_never_head_truncated() {
        let budget = AdmissionBudget::resolve(200_000, 32_000);
        assert_eq!(
            admit(measured(budget.hard_ceiling, 1_000), &budget),
            Admission::Admit
        );
        let over = measured(budget.hard_ceiling + 50_000, 2_000);
        assert!(matches!(
            admit(over, &budget),
            Admission::CompactionRequired { .. }
        ));
        assert!(matches!(
            admit_after_compaction(over, &budget, &CompactionResult::Kept { failure: None }),
            Admission::Refuse(Refusal::StillOverCeiling { .. })
        ));
        assert!(matches!(
            admit_after_compaction(over, &budget, &CompactionResult::Adopted),
            Admission::Refuse(Refusal::StillOverCeiling { .. })
        ));
        assert!(matches!(
            admit(
                measured(budget.hard_ceiling + 1, budget.hard_ceiling),
                &budget
            ),
            Admission::Refuse(Refusal::ToolSchemaFloor { .. })
        ));
    }

    /// Pins `test_summary_completion_policy.py::
    /// test_first_round_and_economic_target_do_not_reduce_route_capacity`:
    /// the ceiling is route capacity (window safety bounded by output room).
    #[test]
    fn capacity_is_physical_route_capacity() {
        assert_eq!(usable_context(1_000_000), 864_000);
        assert_eq!(window_safety(1_000_000), 777_600);
        assert_eq!(usable_context(100_000), 70_000);
        let budget = AdmissionBudget::resolve(1_050_000, 64_000);
        assert_eq!(budget.hard_ceiling, window_safety(1_050_000));
        let tight = AdmissionBudget::resolve(200_000, 150_000);
        assert_eq!(tight.hard_ceiling, 50_000);
        let Admission::CompactionRequired {
            preserve_budget_tokens,
            ..
        } = admit(measured(900_000, 1_000), &budget)
        else {
            panic!()
        };
        assert_eq!(preserve_budget_tokens, MAX_SUMMARY_PRESERVE_TOKENS);
    }

    /// Row "only verified complete outputs qualify"; pins
    /// `test_summary_completion_policy.py::
    /// test_planned_summary_allowance_reaches_transport_without_accepting_truncation`
    /// and `test_nonstream_incomplete_output_is_rejected_and_charged`
    /// (finishes length, max_tokens, content_filter, tool_calls, '').
    #[test]
    fn only_a_verified_complete_summary_qualifies() {
        for finish in ["stop", "end_turn", "stop_sequence", "completed", "STOP"] {
            assert_eq!(
                summary_verdict(&completed("## Objective", finish)).as_deref(),
                Ok("## Objective")
            );
        }
        for finish in ["length", "max_tokens", "content_filter", "tool_calls", ""] {
            assert_eq!(
                summary_verdict(&completed("## Objective", finish)),
                Err(SummaryFailure::Incomplete)
            );
        }
        assert_eq!(
            summary_verdict(&completed("  \n", "stop")),
            Err(SummaryFailure::Empty)
        );
        let dropped = ProviderOutcome::Dropped {
            partial_text: "## Obj".into(),
            detail: "eof".into(),
        };
        assert_eq!(summary_verdict(&dropped), Err(SummaryFailure::Incomplete));
        let with_call = ProviderOutcome::Completed {
            text: "x".into(),
            tool_calls: vec![ToolCall {
                id: "c".into(),
                name: "n".into(),
                arguments: "{}".into(),
            }],
            finish_reason: "stop".into(),
            usage: json!({}),
        };
        assert_eq!(summary_verdict(&with_call), Err(SummaryFailure::Incomplete));
    }

    fn history() -> Vec<Message> {
        vec![
            Message::text(Role::User, "original request"),
            Message::text(Role::Assistant, "long evidence ".repeat(50)),
            Message::text(Role::User, "follow-up"),
            Message::text(Role::Assistant, "recent tail"),
        ]
    }

    fn candidate() -> CompactionCandidate {
        let source = history();
        let mut l1 = source.clone();
        l1[1] = Message::text(Role::Assistant, "[preview]");
        CompactionCandidate {
            source,
            l1_projection: l1,
            summarized: (0, 2),
            source_units: (0, 2),
        }
    }

    /// Row "L1 and L2 planned jointly on a detached candidate, adopted
    /// durably"; pins `tests/compaction/test_compaction_joint_projection.py::
    /// test_joint_candidate_cleans_retained_images_and_keeps_original_archive`
    /// (the summary reads original observations, not L1 previews) and
    /// `test_joint_failure_keeps_source_and_has_no_l1_side_effects`.
    #[test]
    fn joint_candidate_is_detached_and_adopted_only_after_a_durable_ack() {
        let candidate = candidate();
        assert_eq!(candidate.summary_input(), &history()[0..2]);
        let mut live = history();
        for failure in [
            SummaryFailure::Empty,
            SummaryFailure::Incomplete,
            SummaryFailure::Cancelled,
        ] {
            assert_eq!(candidate.adoption(Err(failure), None).unwrap_err(), failure);
        }
        let pending = candidate.adoption(Ok("summary".into()), None).unwrap();
        assert_eq!(pending.archive["summary"]["complete"], true);
        assert_eq!(
            pending
                .clone()
                .commit(Err::<(), _>("storage down"), true, &mut live),
            CompactionResult::Kept { failure: None }
        );
        assert_eq!(
            pending.clone().commit(Ok::<(), ()>(()), false, &mut live),
            CompactionResult::Kept { failure: None }
        );
        assert_eq!(live, history(), "failure keeps the sources");
        assert_eq!(
            pending.commit(Ok::<(), ()>(()), true, &mut live),
            CompactionResult::Adopted
        );
        assert_eq!(live.len(), 3);
        assert_eq!(live[0].content, "summary");
        assert_eq!(candidate.source, history());
    }

    /// Row "Credential outages retry within the documented bound and settle
    /// failed attempts authentically"; pins
    /// `tests/compaction/test_summary_credential_resilience.py::
    /// test_route_reselection_is_bounded_and_request_lifecycle_not_reset` and
    /// `test_unsafe_or_owner_stopped_failure_never_reselects`.
    #[test]
    fn credential_outage_retries_at_most_three_times_within_thirty_seconds() {
        let outage = CredentialFailure {
            transient: true,
            provider_reached: false,
        };
        assert_eq!(
            credential_retry(1, Duration::ZERO, outage, true),
            CredentialRetry::RetryAfter(Duration::from_secs(1))
        );
        assert_eq!(
            credential_retry(2, Duration::from_secs(29), outage, true),
            CredentialRetry::RetryAfter(Duration::from_secs(1))
        );
        assert_eq!(
            credential_retry(2, Duration::from_millis(29_500), outage, true),
            CredentialRetry::RetryAfter(Duration::from_millis(500))
        );
        assert_eq!(
            credential_retry(3, Duration::ZERO, outage, true),
            CredentialRetry::GiveUp
        );
        assert_eq!(
            credential_retry(1, Duration::from_secs(30), outage, true),
            CredentialRetry::GiveUp
        );
        assert!(!credential_retry_after_wait(Duration::from_secs(30)));
        let reached = CredentialFailure {
            transient: true,
            provider_reached: true,
        };
        let permanent = CredentialFailure {
            transient: false,
            provider_reached: false,
        };
        assert_eq!(
            credential_retry(1, Duration::ZERO, reached, true),
            CredentialRetry::GiveUp
        );
        assert_eq!(
            credential_retry(1, Duration::ZERO, permanent, true),
            CredentialRetry::GiveUp
        );
        assert_eq!(
            credential_retry(1, Duration::ZERO, outage, false),
            CredentialRetry::GiveUp
        );
        let receipt = failed_attempt_receipt("working_set", SummaryFailure::CredentialsUnavailable);
        assert_eq!(receipt["status"], "failed");
        assert_eq!(receipt["outcomeReason"], "summary_credentials_unavailable");
    }

    /// A credential outage that leaves the prompt over the ceiling refuses
    /// with the typed cause (`refuse_summary_credentials`); a failed summary
    /// whose prompt still physically fits proceeds uncompacted.
    #[test]
    fn a_credential_outage_over_the_ceiling_refuses_with_its_cause() {
        let budget = AdmissionBudget::resolve(200_000, 32_000);
        let failed = CompactionResult::Kept {
            failure: Some("summary_credentials_unavailable".into()),
        };
        assert_eq!(
            admit_after_compaction(measured(budget.hard_ceiling + 1, 0), &budget, &failed),
            Admission::Refuse(Refusal::SummaryCredentialsUnavailable {
                total_tokens: budget.hard_ceiling + 1
            })
        );
        assert_eq!(
            admit_after_compaction(measured(1_000, 0), &budget, &failed),
            Admission::Admit
        );
        let other = CompactionResult::Kept {
            failure: Some("summary_empty".into()),
        };
        assert!(matches!(
            admit_after_compaction(measured(budget.hard_ceiling + 1, 0), &budget, &other),
            Admission::Refuse(Refusal::CompactionFailed { .. })
        ));
    }

    fn full_checklist() -> ChecklistSnapshot {
        ChecklistSnapshot {
            items: (0..TODO_MAX_ITEMS)
                .map(|i| ChecklistItem {
                    content: format!("{i:03}") + &"s".repeat(TODO_MAX_CONTENT_CHARS - 3),
                    status: ["pending", "in_progress", "blocked", "completed"][i % 4].into(),
                })
                .collect(),
            explanation: "e".repeat(TODO_MAX_EXPLANATION_CHARS),
        }
    }

    /// Row "The full active checklist survives final admission and
    /// compaction"; pins `tests/goals/test_checklist_context.py::
    /// test_final_summary_cannot_remove_snapshot_before_the_same_dispatch`,
    /// `test_maximum_active_list_is_complete_even_under_tiny_optional_context_budget`
    /// and `test_compaction_carries_state_without_emitting_another_intervention`.
    #[test]
    fn the_full_active_checklist_survives_compaction() {
        let checklist = full_checklist();
        assert!(checklist.within_bounds(false));
        let pending = candidate()
            .adoption(Ok("summary".into()), Some(&checklist))
            .unwrap();
        let carrier = pending.view.last().unwrap();
        assert_eq!(carrier.content, checklist.carrier_text());
        for item in &checklist.items {
            assert!(carrier.content.contains(&item.content));
        }
        assert!(carrier.content.contains(&checklist.explanation));
        assert!(checklist_visible(&pending.view, &checklist));
    }

    /// Pins `test_checklist_context.py::test_unchanged_state_preserves_cache_prefix_and_ignores_quoted_headings`,
    /// `test_accepted_todo_result_proves_visibility_without_reinjection` and
    /// `test_round_zero_admission_restores_snapshot_before_measurement`.
    #[test]
    fn restoration_appends_only_when_the_current_list_is_not_visible() {
        let checklist = ChecklistSnapshot {
            items: vec![ChecklistItem {
                content: "write tests".into(),
                status: "in_progress".into(),
            }],
            explanation: String::new(),
        };
        let mut messages = vec![Message::text(Role::User, "task")];
        assert!(restore_checklist(&mut messages, Some(&checklist)));
        let sent = messages.clone();
        assert!(!restore_checklist(&mut messages, Some(&checklist)));
        assert_eq!(messages, sent);
        let mut receipt_history = vec![
            Message::text(Role::User, "task"),
            Message {
                role: Role::Assistant,
                content: String::new(),
                tool_calls: vec![ToolCall {
                    id: "t".into(),
                    name: "todo_write".into(),
                    arguments: "{}".into(),
                }],
                tool_call_id: None,
            },
            Message::tool_result(
                "t",
                format!(
                    "Checklist updated: 0/1 completed.\n{}",
                    checklist.render("Explanation")
                ),
            ),
        ];
        assert!(!restore_checklist(&mut receipt_history, Some(&checklist)));
        let empty = ChecklistSnapshot::default();
        assert!(empty
            .carrier_text()
            .contains("The accepted todo list is empty."));
        assert!(!restore_checklist(&mut receipt_history, None));
    }
}
