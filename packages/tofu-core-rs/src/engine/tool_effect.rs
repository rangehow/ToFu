//! Tool-effect evidence at the common handler boundary: intent before entry,
//! outcome classes, failure projection, restart classification, lost-executor
//! receipts and new-write admission after an unresolved file operation.
//!
//! Replaces (pure policy only; the loop performs every commit and query):
//! - `lib/tasks_pkg/executor/_effects.py` (`begin_tool_effect`,
//!   `finish_tool_effect`, `tool_exception_result`, `tool_timeout_result`,
//!   `restart_unstarted_result`, `effect_inspection_action`);
//! - `lib/tools/restart_effect_class.py` (`cut_call_effect_class`,
//!   `repeatable_cut_call`; the `run_command` shell classifier is supplied by
//!   the caller);
//! - `lib/tools/effect_interruption.py` (`record_interrupted_file_effects`);
//! - `lib/tasks_pkg/executor/_file_recovery.py` (`check_file_recovery`,
//!   `recovery_receipt`).
//!
//! Contract: `docs/contracts/TOOL_EFFECT_RECOVERY.md`; restart rules in
//! `docs/RUN_CONTINUITY.md`; diagnostics rule in `docs/DEFENSIVE_PATTERNS.md`
//! "Failure diagnostics cannot replace the original failure".
//!
//! Loop order for one call: `plan_effect` → if `intent` is `Some`, commit it
//! with `DurableStore::commit_tool_intent` → `decide_entry` (exact ack and
//! the cancellation recheck) → `ToolPort::run` only on `EntryDecision::Enter`
//! → `settle_effect` → on failure `project_failure`. Operation IDs are minted
//! by the host (Python: `uuid4().hex`, 32 lowercase hex characters).

use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::ports::StoreError;
use super::types::{EffectClass, RaisedClass, ToolCall, ToolRun};

/// `ToolContractV2.effectRecovery`. A missing or contradictory declaration
/// requires reconciliation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryPolicy {
    ReadOnly,
    Idempotent,
    Reconcile,
}

impl RecoveryPolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            RecoveryPolicy::ReadOnly => "read_only",
            RecoveryPolicy::Idempotent => "idempotent",
            RecoveryPolicy::Reconcile => "reconcile",
        }
    }

    pub fn from_declaration(declared: Option<&str>) -> Self {
        match declared {
            Some("read_only") => RecoveryPolicy::ReadOnly,
            Some("idempotent") => RecoveryPolicy::Idempotent,
            _ => RecoveryPolicy::Reconcile,
        }
    }

    fn repeatable(self) -> bool {
        matches!(self, RecoveryPolicy::ReadOnly | RecoveryPolicy::Idempotent)
    }
}

impl From<EffectClass> for RecoveryPolicy {
    fn from(class: EffectClass) -> Self {
        match class {
            EffectClass::ReadOnly => RecoveryPolicy::ReadOnly,
            EffectClass::Mutating => RecoveryPolicy::Reconcile,
        }
    }
}

/// `attempt`: durable conversation attempt; `invocation`: registered child;
/// `process`: storage-free transient invocation (no intent barrier).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectScope {
    Attempt,
    Invocation,
    Process,
}

impl EffectScope {
    pub fn as_str(self) -> &'static str {
        match self {
            EffectScope::Attempt => "attempt",
            EffectScope::Invocation => "invocation",
            EffectScope::Process => "process",
        }
    }
}

/// `entered` records possible entry, never success; `returned` means a handler
/// response was captured, never that an external operation completed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EffectState {
    Entered,
    Returned,
    Unknown,
    NotStarted,
}

impl EffectState {
    pub fn as_str(self) -> &'static str {
        match self {
            EffectState::Entered => "entered",
            EffectState::Returned => "returned",
            EffectState::Unknown => "unknown",
            EffectState::NotStarted => "not_started",
        }
    }
}

/// Where an inspection reads a producer occurrence (`ToolEffectInspectionReference`
/// or `InvocationToolEffectInspectionReference`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InspectionLocator {
    Turn(String),
    Invocation(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InspectionRef {
    pub conversation_id: String,
    pub locator: InspectionLocator,
    pub operation_id: String,
    pub owner_scope: String,
}

impl InspectionRef {
    fn kind(&self) -> (&'static str, &str) {
        match &self.locator {
            InspectionLocator::Turn(id) => ("turn", id),
            InspectionLocator::Invocation(id) => ("invocation", id),
        }
    }

    fn to_json(&self) -> Value {
        let (field, locator) = match &self.locator {
            InspectionLocator::Turn(id) => ("turnId", id),
            InspectionLocator::Invocation(id) => ("invocationId", id),
        };
        let mut map = Map::new();
        map.insert("conversationId".into(), json!(self.conversation_id));
        map.insert(field.into(), json!(locator));
        map.insert("operationId".into(), json!(self.operation_id));
        map.insert("ownerScope".into(), json!(self.owner_scope));
        Value::Object(map)
    }

    /// `inspect_tool_effect` arguments, `operation_id` first (`_inspection_arguments`).
    fn inspection_arguments(&self) -> String {
        let (field, locator) = match &self.locator {
            InspectionLocator::Turn(id) => ("turn_id", id),
            InspectionLocator::Invocation(id) => ("invocation_id", id),
        };
        let mut map = Map::new();
        map.insert("operation_id".into(), json!(self.operation_id));
        map.insert(field.into(), json!(locator));
        Value::Object(map).to_string()
    }
}

/// The canonical `ToolExecutionEffect` of one occurrence. `reconciliation` is
/// the domain-owned publication reference (`ProjectFileWriteReconciliation`),
/// kept opaque here.
#[derive(Debug, Clone, PartialEq)]
pub struct ExecutionEffect {
    pub operation_id: String,
    pub state: EffectState,
    pub recovery: RecoveryPolicy,
    pub scope: EffectScope,
    pub reconciliation: Option<Value>,
    pub inspection: Option<InspectionRef>,
}

impl ExecutionEffect {
    pub fn to_json(&self) -> Value {
        let mut map = Map::new();
        map.insert("operationId".into(), json!(self.operation_id));
        map.insert("state".into(), json!(self.state.as_str()));
        map.insert("recovery".into(), json!(self.recovery.as_str()));
        map.insert("scope".into(), json!(self.scope.as_str()));
        if let Some(reference) = &self.reconciliation {
            map.insert("reconciliation".into(), reference.clone());
        }
        if let Some(inspection) = &self.inspection {
            let field = match inspection.locator {
                InspectionLocator::Turn(_) => "inspection",
                InspectionLocator::Invocation(_) => "invocationInspection",
            };
            map.insert(field.into(), inspection.to_json());
        }
        Value::Object(map)
    }

    fn with_state(&self, state: EffectState) -> Self {
        Self {
            state,
            ..self.clone()
        }
    }

    fn publication_field(&self, key: &str) -> Option<&str> {
        self.reconciliation.as_ref()?.get(key)?.as_str()
    }
}

/// Reads pay nothing; storage-free invocations stay process-scoped.
pub fn requires_intent(policy: RecoveryPolicy, scope: EffectScope) -> bool {
    scope != EffectScope::Process && policy != RecoveryPolicy::ReadOnly
}

#[derive(Debug, Clone, PartialEq)]
pub struct PlannedEffect {
    pub effect: ExecutionEffect,
    /// The `tool_effect` event to commit before entry, when one is required.
    pub intent: Option<Value>,
}

/// `begin_tool_effect` without its I/O: the effect is stamped `entered` and,
/// for a non-read-only durable call, the exact intent event is returned.
pub fn plan_effect(
    call: &ToolCall,
    round_num: i64,
    policy: RecoveryPolicy,
    scope: EffectScope,
    operation_id: String,
) -> PlannedEffect {
    let effect = ExecutionEffect {
        operation_id,
        state: EffectState::Entered,
        recovery: policy,
        scope,
        reconciliation: None,
        inspection: None,
    };
    let intent = requires_intent(policy, scope).then(|| intent_event(call, round_num, &effect));
    PlannedEffect { effect, intent }
}

/// `build_event(EventType.TOOL_EFFECT, roundNum=, toolName=, toolCallId=,
/// executionEffect=)`, field order preserved; a `ContractedEvent::ToolEffect`.
pub fn intent_event(call: &ToolCall, round_num: i64, effect: &ExecutionEffect) -> Value {
    let mut map = Map::new();
    map.insert("type".into(), json!("tool_effect"));
    map.insert("roundNum".into(), json!(round_num));
    map.insert("toolName".into(), json!(call.name));
    map.insert("toolCallId".into(), json!(call.id));
    map.insert("executionEffect".into(), effect.to_json());
    Value::Object(map)
}

#[derive(Debug, Clone, PartialEq)]
pub enum EntryRefusal {
    /// No exact acknowledgement: `ack` is the storage error, or `None` when
    /// the required intent was never submitted.
    IntentNotAcknowledged(Option<StoreError>),
    /// Cancellation observed after the barrier.
    Cancelled,
}

#[derive(Debug, Clone, PartialEq)]
pub enum EntryDecision {
    Enter(ExecutionEffect),
    /// The handler must not run. The effect says `not_started` in this
    /// process; a lost ACK may still leave `entered` on disk, which recovery
    /// treats as uncertain.
    Refused {
        effect: ExecutionEffect,
        reason: EntryRefusal,
    },
}

/// The exact-ACK gate before handler entry. `ack` is the result of
/// `commit_tool_intent` (`None` when nothing was committed).
pub fn decide_entry(
    planned: PlannedEffect,
    ack: Option<Result<(), StoreError>>,
    cancelled_after_barrier: bool,
) -> EntryDecision {
    let refused = |reason| EntryDecision::Refused {
        effect: planned.effect.with_state(EffectState::NotStarted),
        reason,
    };
    if planned.intent.is_some() {
        match ack {
            Some(Ok(())) => {}
            Some(Err(error)) => return refused(EntryRefusal::IntentNotAcknowledged(Some(error))),
            None => return refused(EntryRefusal::IntentNotAcknowledged(None)),
        }
    }
    if cancelled_after_barrier {
        return refused(EntryRefusal::Cancelled);
    }
    EntryDecision::Enter(planned.effect)
}

/// The text of the refusal Python raises as `ToolEffectDurabilityError`.
pub const INTENT_NOT_ACKNOWLEDGED: &str = "Exact tool effect intent was not durably acknowledged";

/// Outcome class of an entered occurrence from what the tool port reported.
/// Only a typed pre-entry refusal (or a cancellation the port proves came
/// before entry) becomes `not_started`; a `returned` record is never demoted.
pub fn settle_effect(effect: &ExecutionEffect, run: &ToolRun) -> ExecutionEffect {
    let next = match (effect.state, run) {
        (EffectState::Entered, ToolRun::Finished { .. }) => EffectState::Returned,
        (
            EffectState::Entered | EffectState::Unknown,
            ToolRun::RefusedBeforeEntry { .. } | ToolRun::Cancelled { entered: false },
        ) => EffectState::NotStarted,
        (
            EffectState::Entered,
            ToolRun::LostAfterEntry { .. } | ToolRun::Cancelled { entered: true },
        ) => EffectState::Unknown,
        (
            EffectState::Entered | EffectState::Unknown,
            ToolRun::Raised {
                class: RaisedClass::AdmissionRefusal,
                ..
            },
        ) => EffectState::NotStarted,
        (EffectState::Entered, ToolRun::Raised { .. }) => EffectState::Unknown,
        (state, _) => state,
    };
    effect.with_state(next)
}

/// `typed_tool_error` with its bounds: code ≤ 96, next action ≤ 300 and
/// message ≤ 500 characters (`lib/tools/result_envelope.py`). The settlement
/// owner renders the envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypedToolError {
    pub code: String,
    pub retryable: bool,
    pub next_action: String,
    pub message: String,
}

const NEXT_ACTION_MAX_CHARS: usize = 300;
const DEFAULT_NEXT_ACTION: &str = "Inspect the error and current tool contract. Correct rejected arguments or use another authorized approach. Before repeating an operation that may have run, verify its outcome. Continue independent work; report any remaining blocker.";

fn chars_prefix(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// `tool_error_next_action`.
pub fn bounded_next_action(action: &str) -> String {
    let trimmed = action.trim();
    chars_prefix(
        if trimmed.is_empty() {
            DEFAULT_NEXT_ACTION
        } else {
            trimmed
        },
        NEXT_ACTION_MAX_CHARS,
    )
}

impl TypedToolError {
    pub fn new(code: &str, retryable: bool, next_action: &str, message: &str) -> Self {
        let code = if code.is_empty() { "tool_error" } else { code };
        Self {
            code: chars_prefix(code, 96),
            retryable,
            next_action: bounded_next_action(next_action),
            message: chars_prefix(message, 500),
        }
    }
}

/// The typed failure a tool port reports. Classification comes from this
/// kind, never from diagnostic text.
#[derive(Debug, Clone, PartialEq)]
pub enum FailureKind {
    Timeout,
    /// Cooperative cancellation (owner Stop, fence, authority, restart pause).
    Cancelled,
    PermissionDenied,
    ResourceMissing,
    Connection,
    Storage {
        code: String,
        transient: bool,
        retry_after_ms: Option<u64>,
    },
    /// A typed pre-execution admission refusal (`CommandAdmissionRefusal`).
    AdmissionRefusal,
    Other,
}

/// One failure as caught: its kind, the original type name and a diagnostic
/// whose `Display` may fail or panic.
pub struct ToolFailure<'a> {
    pub kind: FailureKind,
    pub type_name: &'a str,
    pub detail: &'a dyn fmt::Display,
}

/// `text_value`: a failing or panicking `Display` becomes
/// `<unprintable:{type name, ≤ 64 chars}>` and never replaces the failure.
pub fn diagnostic_text(detail: &dyn fmt::Display, type_name: &str) -> String {
    let rendered = catch_unwind(AssertUnwindSafe(|| {
        let mut out = String::new();
        fmt::write(&mut out, format_args!("{detail}")).map(|()| out)
    }));
    match rendered {
        Ok(Ok(text)) => text,
        _ => format!("<unprintable:{}>", chars_prefix(type_name, 64)),
    }
}

/// Facts the loop supplies about the call being settled.
#[derive(Debug, Clone, PartialEq)]
pub struct FailureContext<'a> {
    pub tool_name: &'a str,
    /// The current authoritative declaration.
    pub current_policy: RecoveryPolicy,
    /// The pause subject (`pause_subject`, default "a server restart") when a
    /// restart quiesce or a lead's hold, not a person, stopped the task.
    pub paused_by: Option<&'a str>,
    /// `task['_executionActualsComplete'] is not False`.
    pub actuals_complete: bool,
    /// The executor's own message for the failure (`_handler_error_result`
    /// passes `message=err_msg`); `None` uses the generic wording.
    pub message: Option<&'a str>,
}

fn capitalized(subject: &str) -> String {
    let mut chars = subject.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().chain(chars).collect(),
        None => String::new(),
    }
}

/// `_repeatable_effect`: current declaration and captured entry evidence must agree.
fn repeatable(ctx: &FailureContext<'_>, effect: Option<&ExecutionEffect>) -> bool {
    ctx.current_policy.repeatable()
        && effect.is_none_or(|captured| captured.recovery == ctx.current_policy)
}

fn possibly_entered(effect: Option<&ExecutionEffect>) -> Option<&ExecutionEffect> {
    effect.filter(|e| {
        !e.operation_id.is_empty()
            && matches!(
                e.state,
                EffectState::Entered | EffectState::Unknown | EffectState::Returned
            )
    })
}

/// `effect_inspection_action`: empty when the effect carries no inspection reference.
pub fn effect_inspection_action(effect: &ExecutionEffect) -> String {
    let Some(reference) = &effect.inspection else {
        return String::new();
    };
    let (field, locator) = match &reference.locator {
        InspectionLocator::Turn(id) => ("turn_id", id),
        InspectionLocator::Invocation(id) => ("invocation_id", id),
    };
    let mut arguments = Map::new();
    arguments.insert(field.into(), json!(locator));
    arguments.insert("operation_id".into(), json!(reference.operation_id));
    format!(
        "Do not repeat the write automatically. Call inspect_tool_effect({}) to inspect file-publication evidence. This read does not authorize another write or prove the current file or the entire task is correct.",
        Value::Object(arguments)
    )
}

/// `_unknown_effect_error`: non-retryable `tool_effect_unknown` that keeps the
/// producer occurrence and its inspection path.
pub fn unknown_effect_error(effect: &ExecutionEffect, message: &str) -> TypedToolError {
    let inspection = effect_inspection_action(effect);
    let action = if inspection.is_empty() {
        "Do not repeat this operation automatically. Query the owning service or inspect its durable result, then reconcile the effect before another write."
    } else {
        &inspection
    };
    TypedToolError::new(
        "tool_effect_unknown",
        false,
        action,
        &format!(
            "Operation {} may have taken effect. {message}",
            effect.operation_id
        ),
    )
}

/// `tool_timeout_result` for a deadline without a captured classified failure.
pub fn timeout_failure(
    ctx: &FailureContext<'_>,
    effect: Option<&ExecutionEffect>,
    message: Option<&str>,
) -> TypedToolError {
    let retryable = repeatable(ctx, effect);
    let default_message = format!("Tool execution timed out: {}", ctx.tool_name);
    let message = message.unwrap_or(&default_message);
    if !retryable {
        if let Some(entered) = possibly_entered(effect) {
            return unknown_effect_error(entered, message);
        }
    }
    let next_action = if retryable {
        "The declared recovery policy permits retry through the normal tool gateway and authorization checks. Earlier work may still be running; continue independent authorized work if timeouts persist."
    } else {
        "Do not repeat this operation automatically. Earlier work may still be running; inspect the owning service or durable result to reconcile its outcome. Continue independent authorized work."
    };
    TypedToolError::new("tool_execution_timeout", retryable, next_action, message)
}

/// `restart_unstarted_result`: a pause cut the call before it started.
pub fn restart_unstarted_error(tool_name: &str, subject: &str) -> TypedToolError {
    TypedToolError::new(
        "tool_execution_interrupted",
        true,
        &format!(
            "{} paused this work before the call started; this was not a person's Stop and not a revoked authorization. Repeat the call when the work continues, through the normal tool gateway and authorization checks. Continue independent authorized work.",
            capitalized(subject)
        ),
        &format!("Tool \"{tool_name}\" never started: {subject} stopped the task first."),
    )
}

/// The projected failure and the (possibly corrected) effect to store.
#[derive(Debug, Clone, PartialEq)]
pub struct ProjectedFailure {
    pub effect: Option<ExecutionEffect>,
    pub error: TypedToolError,
}

/// `tool_exception_result`. Unknown effects take precedence over every
/// diagnostic category; the diagnostic text never changes the class.
pub fn project_failure(
    ctx: &FailureContext<'_>,
    effect: Option<&ExecutionEffect>,
    failure: &ToolFailure<'_>,
) -> ProjectedFailure {
    let cancelled = failure.kind == FailureKind::Cancelled;
    let message = match (cancelled, ctx.paused_by) {
        (false, _) if ctx.message.is_some() => ctx.message.unwrap_or_default().to_string(),
        (true, Some(subject)) => format!(
            "Tool \"{}\" was interrupted by {subject} before it returned; whether it took effect is unknown.",
            ctx.tool_name
        ),
        _ => format!(
            "Tool \"{}\" failed with {}: {}",
            ctx.tool_name,
            failure.type_name,
            diagnostic_text(failure.detail, failure.type_name)
        ),
    };
    if failure.kind == FailureKind::Timeout {
        return ProjectedFailure {
            effect: effect.cloned(),
            error: timeout_failure(ctx, effect, Some(&message)),
        };
    }
    let effect = match effect {
        Some(e)
            if failure.kind == FailureKind::AdmissionRefusal
                && matches!(e.state, EffectState::Entered | EffectState::Unknown) =>
        {
            Some(e.with_state(EffectState::NotStarted))
        }
        other => other.cloned(),
    };
    let effect_ref = effect.as_ref();
    let repeatable = repeatable(ctx, effect_ref);
    let done = |error| ProjectedFailure {
        effect: effect.clone(),
        error,
    };
    if !repeatable {
        if let Some(entered) = possibly_entered(effect_ref) {
            let mut message = message.clone();
            if let FailureKind::Storage { code, .. } = &failure.kind {
                message.push_str(&format!(" [storage code: {code}]"));
            }
            return done(unknown_effect_error(entered, &message));
        }
    }
    if let FailureKind::Storage {
        code,
        transient,
        retry_after_ms,
    } = &failure.kind
    {
        let retryable = repeatable && *transient;
        let mut action = if retryable {
            let mut action = String::from("Retry through the normal tool gateway and authorization checks; the declared recovery policy permits another attempt. Keep the selected storage authority and source identity. ");
            if let Some(wait) = retry_after_ms {
                action = format!("Wait at least {wait} milliseconds before retrying. {action}");
            }
            action
        } else {
            let mut action = String::from("Inspect the storage failure and current owned state before repairing the request or selected authority. Preserve existing data. ");
            if !repeatable {
                action.push_str("Reconcile the prior outcome before repeating this operation. ");
            }
            action
        };
        action.push_str("Continue independent authorized work.");
        return done(TypedToolError::new(code, retryable, &action, &message));
    }
    let never_entered =
        ctx.actuals_complete && effect_ref.is_none_or(|e| e.state == EffectState::NotStarted);
    if cancelled && never_entered {
        if let Some(subject) = ctx.paused_by {
            return done(restart_unstarted_error(ctx.tool_name, subject));
        }
    }
    let mut retryable_out = false;
    let (code, mut action) = match &failure.kind {
        FailureKind::Cancelled => (
            "tool_execution_cancelled",
            match ctx.paused_by {
                Some(subject) => format!(
                    "{} interrupted this call, not a person's Stop and not a revoked authorization. When this Turn continues, check whether the operation took effect before repeating it, and continue the task.",
                    capitalized(subject)
                ),
                None => "Respect the cancellation; do not resume this operation automatically.".into(),
            },
        ),
        FailureKind::PermissionDenied => (
            "tool_permission_denied",
            "Check the reported access requirement and current authorized scope, or use another available authorized tool. Do not bypass access controls.".into(),
        ),
        FailureKind::ResourceMissing => (
            "tool_resource_missing",
            "Locate the required resource in the current workspace or service, or repair the reported environment before another attempt.".into(),
        ),
        FailureKind::Connection => {
            retryable_out = repeatable;
            (
                "tool_connection_failed",
                if repeatable {
                    "The declared recovery policy permits retry through the normal tool gateway and authorization checks, or use another available source."
                } else {
                    "Check connectivity and reconcile the prior outcome before another attempt; do not repeat this operation automatically."
                }
                .into(),
            )
        }
        _ => (
            "tool_execution_failed",
            "Inspect and repair the reported runtime or environment failure, or use another available tool; do not blindly repeat the same call.".into(),
        ),
    };
    if !cancelled {
        if !repeatable && code != "tool_connection_failed" {
            action.push_str(" Verify the prior outcome before repeating the operation.");
        }
        action.push_str(" Continue independent authorized work.");
    }
    done(TypedToolError::new(code, retryable_out, &action, &message))
}

// ── Restart classification (docs/RUN_CONTINUITY.md) ─────────────────────

/// `cut_call_effect_class` classes, safest first.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CutEffectClass {
    ReadOnly,
    Idempotent,
    Verifiable,
    Unknown,
}

/// The declaration decides first; any other tool than `run_command` is
/// `unknown`. `run_command_class` is the shell classifier's verdict
/// (`run_command_effect_class`), `None` when it could not be computed.
pub fn cut_call_effect_class(
    tool_name: &str,
    declared: RecoveryPolicy,
    run_command_class: Option<CutEffectClass>,
) -> CutEffectClass {
    match declared {
        RecoveryPolicy::ReadOnly => CutEffectClass::ReadOnly,
        RecoveryPolicy::Idempotent => CutEffectClass::Idempotent,
        RecoveryPolicy::Reconcile if tool_name == "run_command" => {
            run_command_class.unwrap_or(CutEffectClass::Unknown)
        }
        RecoveryPolicy::Reconcile => CutEffectClass::Unknown,
    }
}

/// One occurrence a dead execution process left behind.
#[derive(Debug, Clone, PartialEq)]
pub struct CutCall {
    pub call_id: String,
    /// State of the committed intent; `None` when no intent was committed.
    pub committed_state: Option<EffectState>,
    pub has_result: bool,
    pub class: CutEffectClass,
}

/// Call IDs that need a decision before the Turn resumes: only `unknown`-class
/// calls whose committed `entered`/`unknown` intent has no result. A call with
/// no committed intent, or stopped before it ran, leaves the Turn resumable.
pub fn calls_blocking_resume(calls: &[CutCall]) -> Vec<&str> {
    calls
        .iter()
        .filter(|call| {
            matches!(
                call.committed_state,
                Some(EffectState::Entered | EffectState::Unknown)
            ) && !call.has_result
                && call.class == CutEffectClass::Unknown
        })
        .map(|call| call.call_id.as_str())
        .collect()
}

/// `repeatable_cut_call`: harmless to run again whatever it already did.
/// `run_command_repeatable` is the shell classifier's read/test-only verdict.
pub fn repeatable_cut_call(
    tool_name: &str,
    state: Option<EffectState>,
    declared: RecoveryPolicy,
    run_command_repeatable: bool,
) -> bool {
    state == Some(EffectState::NotStarted)
        || declared.repeatable()
        || (tool_name == "run_command" && run_command_repeatable)
}

// ── Stored occurrences: lost-executor receipts and new-write admission ──

/// One retained tool occurrence of the Turn projection (`toolRounds` row).
#[derive(Debug, Clone, PartialEq)]
pub struct OccurrenceRow {
    pub tool_name: String,
    pub task_id: Option<String>,
    pub attempt_id: Option<String>,
    /// `status` (`done`, `error`, `aborted`, `rejected`, ...).
    pub status: String,
    /// `toolContent` is set or `toolModelContent` is a non-empty list.
    pub has_captured_response: bool,
    pub provider_attempt_discarded: bool,
    pub effect: Option<ExecutionEffect>,
}

fn lower_hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The receipt `turn.recover` stores for one lost-executor write.
#[derive(Debug, Clone, PartialEq)]
pub struct InterruptionReceipt {
    /// Index of the row in the projection.
    pub index: usize,
    pub error: TypedToolError,
    /// The row status becomes `aborted`.
    pub status: &'static str,
    /// The effect with state `unknown`; producer IDs and publication kept.
    pub effect: ExecutionEffect,
}

/// `record_interrupted_file_effects`: only a unique current task/attempt
/// `write_file` occurrence with attempt-scoped `reconcile` policy, state
/// `entered`/`unknown`, matching inspection/publication identity and no
/// captured response gets a non-retryable `tool_effect_unknown` receipt.
pub fn interrupted_write_receipts(
    rows: &[OccurrenceRow],
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
    task_id: &str,
) -> Vec<InterruptionReceipt> {
    if [conversation_id, turn_id, attempt_id, task_id]
        .iter()
        .any(|v| v.is_empty())
    {
        return Vec::new();
    }
    let occurrences = |operation: &str| {
        rows.iter()
            .filter(|row| {
                row.effect
                    .as_ref()
                    .is_some_and(|e| e.operation_id == operation)
            })
            .count()
    };
    let mut receipts = Vec::new();
    for (index, row) in rows.iter().enumerate() {
        if row.tool_name != "write_file"
            || row.task_id.as_deref() != Some(task_id)
            || row.attempt_id.as_deref() != Some(attempt_id)
            || row.has_captured_response
            || row.provider_attempt_discarded
        {
            continue;
        }
        let Some(effect) = &row.effect else { continue };
        if effect.scope != EffectScope::Attempt
            || effect.recovery != RecoveryPolicy::Reconcile
            || !matches!(effect.state, EffectState::Entered | EffectState::Unknown)
        {
            continue;
        }
        let operation = &effect.operation_id;
        let Some(reference) = &effect.inspection else {
            continue;
        };
        if !lower_hex(operation, 32)
            || occurrences(operation) != 1
            || effect
                .reconciliation
                .as_ref()
                .is_none_or(|p| !p.is_object())
            || &reference.operation_id != operation
            || reference.conversation_id != conversation_id
            || reference.locator != InspectionLocator::Turn(turn_id.to_string())
            || !lower_hex(&reference.owner_scope, 24)
            || effect.publication_field("kind") != Some("project_file_write")
            || effect.publication_field("taskId") != Some(task_id)
        {
            continue;
        }
        // json.dumps(sort_keys=True): operation_id sorts before turn_id.
        let arguments = json!({"operation_id": operation, "turn_id": turn_id});
        let action = format!(
            "Call inspect_tool_effect({arguments}). Do not repeat the write automatically."
        );
        if bounded_next_action(&action) != action {
            // A clipped identity would make the advertised query uncallable.
            continue;
        }
        receipts.push(InterruptionReceipt {
            index,
            error: TypedToolError::new(
                "tool_effect_unknown",
                false,
                &action,
                "The producing executor was retired before a tool response was captured. File publication remains unknown. This is an interruption observation, not a handler response, publication proof, task completion or approval.",
            ),
            status: "aborted",
            effect: effect.with_state(EffectState::Unknown),
        });
    }
    receipts
}

/// A decoded `ConversationToolEffectInspection`/`InvocationToolEffectInspection`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InspectionObservation {
    pub conversation_id: String,
    pub locator: InspectionLocator,
    pub operation_id: String,
    /// `file_published`, `file_not_published` or `unknown`.
    pub outcome: String,
    pub reason: String,
}

/// `_fact`: (conversation, `turn`|`invocation`, locator, operation, outcome, reason).
pub type InspectionFact = [String; 6];

type InspectionKey = (String, &'static str, String, String);

impl InspectionObservation {
    fn key(&self) -> InspectionKey {
        let (kind, locator) = match &self.locator {
            InspectionLocator::Turn(id) => ("turn", id.clone()),
            InspectionLocator::Invocation(id) => ("invocation", id.clone()),
        };
        (
            self.conversation_id.clone(),
            kind,
            locator,
            self.operation_id.clone(),
        )
    }

    fn fact(&self) -> InspectionFact {
        let (conversation, kind, locator, operation) = self.key();
        [
            conversation,
            kind.to_string(),
            locator,
            operation,
            self.outcome.clone(),
            self.reason.clone(),
        ]
    }
}

fn reference_key(reference: &InspectionRef) -> InspectionKey {
    let (kind, locator) = reference.kind();
    (
        reference.conversation_id.clone(),
        kind,
        locator.to_string(),
        reference.operation_id.clone(),
    )
}

/// A settled `inspect_tool_effect` row of this executor. `observation` is
/// `None` when its settled `toolContent` does not decode as an inspection.
#[derive(Debug, Clone, PartialEq)]
pub struct InspectionReceipt {
    pub llm_round: i64,
    pub status: String,
    /// `None` when the row carries no producer identity (it then counts as this task's).
    pub task_id: Option<String>,
    pub attempt_id: Option<String>,
    pub effect_state: EffectState,
    pub effect_recovery: RecoveryPolicy,
    pub provider_attempt_discarded: bool,
    pub observation: Option<InspectionObservation>,
}

/// A new `write_file` the loop is about to approve or enter.
#[derive(Debug, Clone, PartialEq)]
pub struct NewWrite<'a> {
    pub tool_name: &'a str,
    /// The resolved target; `None` when it could not be resolved. The caller
    /// canonicalizes symlinks (`os.path.realpath`); this module normalizes
    /// lexically only.
    pub target: Option<&'a str>,
    /// `llmRound` of the new call.
    pub current_round: Option<i64>,
    pub task_id: &'a str,
    pub attempt_id: Option<&'a str>,
    pub conversation_id: &'a str,
    /// `turn_image_owner_scope(owner, conversation)` of the caller.
    pub owner_scope: &'a str,
}

#[derive(Debug, Clone, PartialEq)]
pub enum WriteAdmission {
    /// Unaffected target or known facts: ordinary authorization still applies.
    Ordinary,
    /// Unknown evidence: only an exact fresh human decision on these facts admits.
    HumanGate {
        unknown: Vec<InspectionFact>,
        fingerprint: String,
    },
    Refused(TypedToolError),
}

fn normalize_path(path: &str) -> String {
    let absolute = path.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else if !absolute {
                    parts.push("..");
                }
            }
            other => parts.push(other),
        }
    }
    let joined = parts.join("/");
    match (absolute, joined.is_empty()) {
        (true, _) => format!("/{joined}"),
        (false, true) => ".".into(),
        (false, false) => joined,
    }
}

fn join_path(base: &str, path: &str) -> String {
    if path.starts_with('/') || base.is_empty() {
        path.to_string()
    } else {
        format!("{}/{path}", base.trim_end_matches('/'))
    }
}

fn recovery_refusal(
    code: &str,
    message: &str,
    reference: Option<&InspectionRef>,
) -> WriteAdmission {
    let mut message = message.to_string();
    let mut action = String::from(
        "Inspect the unresolved file operation before a new write; independent work may continue.",
    );
    if let Some(reference) = reference {
        action = format!(
            "Call inspect_tool_effect({}). Replan before another write.",
            reference.inspection_arguments()
        );
        if bounded_next_action(&action) != action {
            message.push_str(&format!(" Exact inspection: {action}"));
            action = "Inspect the exact producing operation described in the error, then replan the new write.".into();
        }
    }
    WriteAdmission::Refused(TypedToolError::new(code, false, &action, &message))
}

/// The SHA-256 of the sorted unknown facts as compact JSON (`FileRecoveryCheck.fingerprint`).
pub fn unknown_facts_fingerprint(unknown: &[InspectionFact]) -> String {
    let text = serde_json::to_string(unknown).expect("string arrays serialize");
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn unresolved_write(row: &OccurrenceRow) -> Option<&ExecutionEffect> {
    let effect = row.effect.as_ref()?;
    let unresolved = matches!(effect.state, EffectState::Entered | EffectState::Unknown)
        || (effect.state == EffectState::Returned
            && matches!(row.status.as_str(), "error" | "aborted" | "rejected"));
    (row.tool_name == "write_file"
        && matches!(effect.scope, EffectScope::Attempt | EffectScope::Invocation)
        && effect.recovery == RecoveryPolicy::Reconcile
        && unresolved
        && !row.provider_attempt_discarded
        && effect.publication_field("kind") == Some("project_file_write"))
    .then_some(effect)
}

/// `_observations`: only an earlier, complete, model-visible inspection of
/// this executor counts; the latest round wins per key.
fn earlier_observations(
    write: &NewWrite<'_>,
    receipts: &[InspectionReceipt],
) -> Vec<(InspectionKey, InspectionFact)> {
    let Some(current) = write.current_round else {
        return Vec::new();
    };
    let mut latest: Vec<(InspectionKey, i64, InspectionFact)> = Vec::new();
    for receipt in receipts {
        let Some(observation) = &receipt.observation else {
            continue;
        };
        if receipt.llm_round >= current
            || receipt.status != "done"
            || receipt
                .task_id
                .as_deref()
                .is_some_and(|t| t != write.task_id)
            || receipt
                .attempt_id
                .as_deref()
                .is_some_and(|a| Some(a) != write.attempt_id)
            || receipt.effect_state != EffectState::Returned
            || !receipt.effect_recovery.repeatable()
            || receipt.provider_attempt_discarded
        {
            continue;
        }
        let key = observation.key();
        match latest.iter_mut().find(|(k, _, _)| *k == key) {
            Some(entry) if receipt.llm_round >= entry.1 => {
                *entry = (key, receipt.llm_round, observation.fact())
            }
            Some(_) => {}
            None => latest.push((key, receipt.llm_round, observation.fact())),
        }
    }
    latest.into_iter().map(|(k, _, f)| (k, f)).collect()
}

/// `check_file_recovery`. `fresh` performs the owner-scoped inspection query
/// for one affected producer (`None` when the service is unavailable); it is
/// called only after an earlier observation exists, never speculatively.
/// Equal arguments never identify a prior operation; the target only scopes
/// the dependency.
pub fn admit_new_write(
    write: &NewWrite<'_>,
    rows: &[OccurrenceRow],
    receipts: &[InspectionReceipt],
    fresh: &mut dyn FnMut(&InspectionRef) -> Option<InspectionObservation>,
) -> WriteAdmission {
    if write.tool_name != "write_file" {
        return WriteAdmission::Ordinary;
    }
    let sources: Vec<(&OccurrenceRow, &ExecutionEffect)> = rows
        .iter()
        .filter_map(|row| unresolved_write(row).map(|effect| (row, effect)))
        .collect();
    if sources.is_empty() {
        return WriteAdmission::Ordinary;
    }
    let Some(target) = write.target else {
        return recovery_refusal(
            "tool_effect_target_unavailable",
            "The new file target could not be resolved; no write started.",
            None,
        );
    };
    let target = normalize_path(target);
    let mut seen: Vec<InspectionKey> = Vec::new();
    let mut unknown: Vec<InspectionFact> = Vec::new();
    let mut observations = None;
    for (row, effect) in sources {
        let (Some(base), Some(path)) = (
            effect.publication_field("basePath"),
            effect.publication_field("path"),
        ) else {
            return recovery_refusal(
                "tool_effect_evidence_invalid",
                "An unresolved file occurrence has invalid target evidence.",
                None,
            );
        };
        if normalize_path(&join_path(base, path)) != target {
            continue;
        }
        let producer = row.task_id.as_deref();
        let valid = effect.inspection.as_ref().filter(|reference| {
            let (_, locator) = reference.kind();
            let child_ok = match &reference.locator {
                InspectionLocator::Invocation(id) => Some(id.as_str()) == producer,
                InspectionLocator::Turn(_) => true,
            };
            reference.conversation_id == write.conversation_id
                && reference.owner_scope == write.owner_scope
                && reference.operation_id == effect.operation_id
                && effect.publication_field("taskId") == producer
                && effect.operation_id.chars().count() == 32
                && !locator.is_empty()
                && child_ok
        });
        let Some(reference) = valid else {
            return recovery_refusal(
                "tool_effect_evidence_invalid",
                "The affected file occurrence has no valid owned inspection identity.",
                None,
            );
        };
        let key = reference_key(reference);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key.clone());
        let observed = observations
            .get_or_insert_with(|| earlier_observations(write, receipts))
            .iter()
            .find(|(k, _)| *k == key)
            .map(|(_, fact)| fact.clone());
        let Some(observed) = observed else {
            return recovery_refusal(
                "tool_effect_inspection_required",
                "A prior write to this file is unresolved. Read its publication evidence before planning this new write.",
                Some(reference),
            );
        };
        let Some(current) = fresh(reference) else {
            return recovery_refusal(
                "tool_effect_inspection_unavailable",
                "The owning file service could not verify the prior operation; no new write started.",
                Some(reference),
            );
        };
        if current.fact() != observed {
            return recovery_refusal(
                "tool_effect_inspection_changed",
                "File-publication evidence changed after inspection. Read it again before planning a new write.",
                Some(reference),
            );
        }
        if matches!(
            current.reason.as_str(),
            "writer_active" | "evidence_unavailable"
        ) {
            return recovery_refusal(
                "tool_effect_inspection_unavailable",
                "The file writer is active or its evidence is unavailable. Wait for it to settle, then inspect again.",
                Some(reference),
            );
        }
        if current.outcome == "unknown" {
            unknown.push(current.fact());
        }
    }
    if unknown.is_empty() {
        return WriteAdmission::Ordinary;
    }
    unknown.sort();
    let fingerprint = unknown_facts_fingerprint(&unknown);
    WriteAdmission::HumanGate {
        unknown,
        fingerprint,
    }
}

/// `recovery_receipt`: only a human-authority receipt bound to exactly these
/// unknown facts admits entry. Auto, delegated and earlier general approvals
/// never do.
pub fn human_gate_admits(
    receipt_authority_is_human: bool,
    receipt_fingerprint: Option<&str>,
    gate_fingerprint: &str,
) -> bool {
    receipt_authority_is_human && receipt_fingerprint == Some(gate_fingerprint)
}

#[cfg(test)]
mod tests {
    use super::*;
    use tofu_contracts::generated_events_v1::ContractedEvent;

    fn call(name: &str) -> ToolCall {
        ToolCall {
            id: "call_1".into(),
            name: name.into(),
            arguments: "{}".into(),
        }
    }

    const OP: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const SCOPE: &str = "0123456789abcdef01234567";

    fn ack_error() -> StoreError {
        StoreError {
            code: "database_busy".into(),
            message: "busy".into(),
            transient: true,
            not_dispatched: false,
        }
    }

    fn ctx(policy: RecoveryPolicy) -> FailureContext<'static> {
        FailureContext {
            tool_name: "write_file",
            current_policy: policy,
            paused_by: None,
            actuals_complete: true,
            message: None,
        }
    }

    fn entered(policy: RecoveryPolicy) -> ExecutionEffect {
        plan_effect(
            &call("write_file"),
            3,
            policy,
            EffectScope::Attempt,
            OP.into(),
        )
        .effect
    }

    struct Unprintable;
    impl fmt::Display for Unprintable {
        fn fmt(&self, _: &mut fmt::Formatter<'_>) -> fmt::Result {
            Err(fmt::Error)
        }
    }

    struct Panicking;
    impl fmt::Display for Panicking {
        fn fmt(&self, _: &mut fmt::Formatter<'_>) -> fmt::Result {
            panic!("diagnostic renderer failed")
        }
    }

    /// Row "Non-read-only calls commit an intent with an exact storage ack
    /// before entry; reads pay nothing" (TOOL_EFFECT_RECOVERY.md;
    /// tests/tasks_pkg/test_tool_outcome_finality.py, test_tool_effect_barrier.py).
    #[test]
    fn mutating_durable_call_commits_a_contracted_intent_before_entry() {
        let planned = plan_effect(
            &call("write_file"),
            3,
            RecoveryPolicy::Reconcile,
            EffectScope::Attempt,
            OP.into(),
        );
        let intent = planned.intent.clone().expect("intent required");
        assert_eq!(
            intent.to_string(),
            format!(
                r#"{{"type":"tool_effect","roundNum":3,"toolName":"write_file","toolCallId":"call_1","executionEffect":{{"operationId":"{OP}","state":"entered","recovery":"reconcile","scope":"attempt"}}}}"#
            )
        );
        let event: ContractedEvent = serde_json::from_value(intent).expect("contracted");
        assert!(matches!(event, ContractedEvent::ToolEffect(_)));
        assert!(matches!(
            decide_entry(planned.clone(), Some(Ok(())), false),
            EntryDecision::Enter(e) if e.state == EffectState::Entered
        ));
        for ack in [None, Some(Err(ack_error()))] {
            match decide_entry(planned.clone(), ack, false) {
                EntryDecision::Refused { effect, reason } => {
                    assert_eq!(effect.state, EffectState::NotStarted);
                    assert!(matches!(reason, EntryRefusal::IntentNotAcknowledged(_)));
                }
                other => panic!("entered without ack: {other:?}"),
            }
        }
        assert!(matches!(
            decide_entry(planned, Some(Ok(())), true),
            EntryDecision::Refused {
                reason: EntryRefusal::Cancelled,
                ..
            }
        ));
    }

    /// Same row: reads and storage-free process-scoped calls pay no barrier.
    #[test]
    fn read_only_and_process_scoped_calls_pay_no_intent() {
        let read = plan_effect(
            &call("read_files"),
            1,
            RecoveryPolicy::ReadOnly,
            EffectScope::Attempt,
            OP.into(),
        );
        assert!(read.intent.is_none());
        assert!(matches!(
            decide_entry(read, None, false),
            EntryDecision::Enter(_)
        ));
        let transient = plan_effect(
            &call("write_file"),
            1,
            RecoveryPolicy::Reconcile,
            EffectScope::Process,
            OP.into(),
        );
        assert!(transient.intent.is_none());
        assert!(requires_intent(
            RecoveryPolicy::Idempotent,
            EffectScope::Invocation
        ));
        assert_eq!(
            RecoveryPolicy::from_declaration(None),
            RecoveryPolicy::Reconcile
        );
        assert_eq!(
            RecoveryPolicy::from(EffectClass::Mutating),
            RecoveryPolicy::Reconcile
        );
    }

    /// Row "`entered` is not success; post-entry exceptions are `unknown`; only
    /// typed pre-entry refusals become `not_started`"
    /// (tests/tasks_pkg/test_tool_admission_refusal_effect.py).
    #[test]
    fn entered_is_not_success_and_only_typed_pre_entry_refusal_is_not_started() {
        let effect = entered(RecoveryPolicy::Reconcile);
        let finished = ToolRun::Finished {
            output: "x".into(),
            is_error: true,
        };
        assert_eq!(
            settle_effect(&effect, &finished).state,
            EffectState::Returned
        );
        let lost = ToolRun::LostAfterEntry {
            detail: "panic".into(),
        };
        assert_eq!(settle_effect(&effect, &lost).state, EffectState::Unknown);
        let refused = ToolRun::RefusedBeforeEntry {
            reason: "workspace".into(),
        };
        assert_eq!(
            settle_effect(&effect, &refused).state,
            EffectState::NotStarted
        );
        let returned = settle_effect(&effect, &finished);
        assert_eq!(
            settle_effect(&returned, &refused).state,
            EffectState::Returned
        );

        // test_command_admission_refusal_settles_not_started_without_unknown_effect
        let refusal = ToolFailure {
            kind: FailureKind::AdmissionRefusal,
            type_name: "CommandAdmissionRefusal",
            detail: &"Error [workspace-isolation]: command was not started",
        };
        let projected = project_failure(&ctx(RecoveryPolicy::Reconcile), Some(&effect), &refusal);
        assert_eq!(projected.effect.unwrap().state, EffectState::NotStarted);
        assert_eq!(projected.error.code, "tool_execution_failed");
        // test_plain_value_error_still_reports_an_entered_unknown_effect
        let plain = ToolFailure {
            kind: FailureKind::Other,
            type_name: "ValueError",
            detail: &"bad",
        };
        let projected = project_failure(&ctx(RecoveryPolicy::Reconcile), Some(&effect), &plain);
        assert_eq!(projected.effect.unwrap().state, EffectState::Entered);
        assert_eq!(projected.error.code, "tool_effect_unknown");
        assert!(!projected.error.retryable);
        assert!(projected.error.message.starts_with(&format!(
            "Operation {OP} may have taken effect. Tool \"write_file\" failed with ValueError: bad"
        )));
    }

    /// Row "A failing `__str__` cannot replace the original failure or its
    /// recovery class" (DEFENSIVE_PATTERNS.md; tests/tasks_pkg/test_tool_diagnostic_recovery.py
    /// test_unprintable_failure_keeps_original_classification_and_settlement).
    #[test]
    fn unprintable_diagnostic_keeps_the_original_failure_class() {
        let read = ctx(RecoveryPolicy::ReadOnly);
        let cases: [(FailureKind, &str, &str, bool); 5] = [
            (
                FailureKind::Connection,
                "UnprintableConnection",
                "tool_connection_failed",
                true,
            ),
            (
                FailureKind::Timeout,
                "UnprintableTimeout",
                "tool_execution_timeout",
                true,
            ),
            (
                FailureKind::PermissionDenied,
                "UnprintablePermission",
                "tool_permission_denied",
                false,
            ),
            (
                FailureKind::Other,
                "UnprintableValue",
                "tool_execution_failed",
                false,
            ),
            (
                FailureKind::Storage {
                    code: "database_busy".into(),
                    transient: true,
                    retry_after_ms: None,
                },
                "UnprintableStorage",
                "database_busy",
                true,
            ),
        ];
        for (kind, type_name, code, retryable) in cases {
            for detail in [&Unprintable as &dyn fmt::Display, &Panicking] {
                let failure = ToolFailure {
                    kind: kind.clone(),
                    type_name,
                    detail,
                };
                let error = project_failure(&read, None, &failure).error;
                assert_eq!((error.code.as_str(), error.retryable), (code, retryable));
                assert!(error.message.contains(type_name));
                assert!(error
                    .message
                    .contains(&format!("<unprintable:{type_name}>")));
                assert!(!error.message.contains("diagnostic renderer failed"));
            }
        }
        // test_unprintable_failure_retains_whole_tool_replay_authority
        let failure = ToolFailure {
            kind: FailureKind::Connection,
            type_name: "X",
            detail: &Unprintable,
        };
        let error = project_failure(
            &ctx(RecoveryPolicy::Reconcile),
            Some(&entered(RecoveryPolicy::Reconcile)),
            &failure,
        )
        .error;
        assert_eq!(error.code, "tool_effect_unknown");
        // test_unprintable_control_failure_keeps_owner_cancellation
        let stop = ToolFailure {
            kind: FailureKind::Cancelled,
            type_name: "AbortedError",
            detail: &Unprintable,
        };
        let error = project_failure(&read, None, &stop).error;
        assert_eq!(error.code, "tool_execution_cancelled");
        assert_eq!(
            error.next_action,
            "Respect the cancellation; do not resume this operation automatically."
        );
    }

    /// Row "A lost-executor `write_file` gets a non-retryable `tool_effect_unknown`
    /// receipt" (TOOL_EFFECT_RECOVERY.md "Lost-executor interruption receipts";
    /// lib/tools/effect_interruption.py, selected-authority tests).
    #[test]
    fn lost_executor_write_gets_a_non_retryable_unknown_receipt() {
        let mut effect = entered(RecoveryPolicy::Reconcile);
        effect.reconciliation = Some(
            json!({"kind": "project_file_write", "taskId": "task", "basePath": "/w", "path": "file.txt"}),
        );
        effect.inspection = Some(InspectionRef {
            conversation_id: "conv".into(),
            locator: InspectionLocator::Turn("turn".into()),
            operation_id: OP.into(),
            owner_scope: SCOPE.into(),
        });
        let row = OccurrenceRow {
            tool_name: "write_file".into(),
            task_id: Some("task".into()),
            attempt_id: Some("att".into()),
            status: "running".into(),
            has_captured_response: false,
            provider_attempt_discarded: false,
            effect: Some(effect),
        };
        let receipts =
            interrupted_write_receipts(std::slice::from_ref(&row), "conv", "turn", "att", "task");
        assert_eq!(receipts.len(), 1);
        let receipt = &receipts[0];
        assert_eq!(
            (
                receipt.error.code.as_str(),
                receipt.error.retryable,
                receipt.status
            ),
            ("tool_effect_unknown", false, "aborted")
        );
        assert_eq!(receipt.effect.state, EffectState::Unknown);
        assert_eq!(
            receipt.error.next_action,
            format!(
                r#"Call inspect_tool_effect({{"operation_id":"{OP}","turn_id":"turn"}}). Do not repeat the write automatically."#
            )
        );
        let mut answered = row.clone();
        answered.has_captured_response = true;
        let duplicate = [row.clone(), row.clone()];
        assert!(interrupted_write_receipts(&[answered], "conv", "turn", "att", "task").is_empty());
        assert!(interrupted_write_receipts(&duplicate, "conv", "turn", "att", "task").is_empty());
        assert!(interrupted_write_receipts(
            std::slice::from_ref(&row),
            "conv",
            "turn",
            "other",
            "task"
        )
        .is_empty());
    }

    fn file_scenario(state: EffectState) -> (OccurrenceRow, NewWrite<'static>) {
        let mut effect = entered(RecoveryPolicy::Reconcile);
        effect.state = state;
        effect.reconciliation = Some(
            json!({"kind": "project_file_write", "taskId": "task", "basePath": "/w", "path": "file.txt"}),
        );
        effect.inspection = Some(InspectionRef {
            conversation_id: "conv".into(),
            locator: InspectionLocator::Turn("turn".into()),
            operation_id: OP.into(),
            owner_scope: SCOPE.into(),
        });
        let row = OccurrenceRow {
            tool_name: "write_file".into(),
            task_id: Some("task".into()),
            attempt_id: Some("att".into()),
            status: "aborted".into(),
            has_captured_response: true,
            provider_attempt_discarded: false,
            effect: Some(effect),
        };
        let write = NewWrite {
            tool_name: "write_file",
            target: Some("/w/./file.txt"),
            current_round: Some(2),
            task_id: "task",
            attempt_id: Some("att"),
            conversation_id: "conv",
            owner_scope: SCOPE,
        };
        (row, write)
    }

    fn observation(outcome: &str, reason: &str) -> InspectionObservation {
        InspectionObservation {
            conversation_id: "conv".into(),
            locator: InspectionLocator::Turn("turn".into()),
            operation_id: OP.into(),
            outcome: outcome.into(),
            reason: reason.into(),
        }
    }

    fn receipt(round: i64, outcome: &str, reason: &str) -> InspectionReceipt {
        InspectionReceipt {
            llm_round: round,
            status: "done".into(),
            task_id: Some("task".into()),
            attempt_id: Some("att".into()),
            effect_state: EffectState::Returned,
            effect_recovery: RecoveryPolicy::Idempotent,
            provider_attempt_discarded: false,
            observation: Some(observation(outcome, reason)),
        }
    }

    fn refusal_code(admission: &WriteAdmission) -> &str {
        match admission {
            WriteAdmission::Refused(error) => &error.code,
            other => panic!("not refused: {other:?}"),
        }
    }

    /// Row "a new write to that target needs an earlier-round inspection and,
    /// if still unknown, a human gate" (tests/tasks_pkg/test_file_recovery_admission.py
    /// test_only_earlier_complete_current_executor_observations_satisfy_admission,
    /// test_known_facts_permit_a_new_call_only_after_an_observed_round,
    /// test_unknown_facts_need_a_new_disclosed_human_decision_not_an_old_grant).
    #[test]
    fn new_write_after_unknown_effect_needs_earlier_inspection_then_human_gate() {
        let (row, write) = file_scenario(EffectState::Unknown);
        let rows = [row];
        let queries = std::cell::Cell::new(0);
        let mut never = |_: &InspectionRef| -> Option<InspectionObservation> {
            queries.set(queries.get() + 1);
            None
        };
        assert_eq!(
            refusal_code(&admit_new_write(&write, &rows, &[], &mut never)),
            "tool_effect_inspection_required"
        );
        assert_eq!(
            refusal_code(&admit_new_write(
                &write,
                &rows,
                &[receipt(2, "unknown", "missing_evidence")],
                &mut never
            )),
            "tool_effect_inspection_required"
        );
        let mut wrong_attempt = receipt(1, "unknown", "missing_evidence");
        wrong_attempt.attempt_id = Some("another".into());
        assert_eq!(
            refusal_code(&admit_new_write(
                &write,
                &rows,
                &[wrong_attempt],
                &mut never
            )),
            "tool_effect_inspection_required"
        );
        assert_eq!(queries.get(), 0, "no speculative query");

        let mut published =
            |_: &InspectionRef| Some(observation("file_published", "undo_committed"));
        assert_eq!(
            admit_new_write(
                &write,
                &rows,
                &[receipt(1, "file_published", "undo_committed")],
                &mut published
            ),
            WriteAdmission::Ordinary
        );
        assert_eq!(
            refusal_code(&admit_new_write(
                &write,
                &rows,
                &[receipt(1, "unknown", "missing_evidence")],
                &mut published
            )),
            "tool_effect_inspection_changed"
        );
        let mut busy = |_: &InspectionRef| Some(observation("unknown", "writer_active"));
        assert_eq!(
            refusal_code(&admit_new_write(
                &write,
                &rows,
                &[receipt(1, "unknown", "writer_active")],
                &mut busy
            )),
            "tool_effect_inspection_unavailable"
        );

        let mut still_unknown =
            |_: &InspectionRef| Some(observation("unknown", "missing_evidence"));
        let WriteAdmission::HumanGate {
            unknown,
            fingerprint,
        } = admit_new_write(
            &write,
            &rows,
            &[receipt(1, "unknown", "missing_evidence")],
            &mut still_unknown,
        )
        else {
            panic!("gate expected")
        };
        assert_eq!(
            unknown,
            vec![observation("unknown", "missing_evidence").fact()]
        );
        let expected = format!(r#"[["conv","turn","turn","{OP}","unknown","missing_evidence"]]"#);
        let digest: String = Sha256::digest(expected.as_bytes())
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(fingerprint, digest);
        assert!(human_gate_admits(true, Some(&fingerprint), &fingerprint));
        assert!(!human_gate_admits(false, Some(&fingerprint), &fingerprint));
        assert!(!human_gate_admits(true, Some("stale"), &fingerprint));

        let other = NewWrite {
            target: Some("/w/other.txt"),
            ..write.clone()
        };
        assert_eq!(
            admit_new_write(&other, &rows, &[], &mut never),
            WriteAdmission::Ordinary
        );
        let read = NewWrite {
            tool_name: "read_files",
            ..write
        };
        assert_eq!(
            admit_new_write(&read, &rows, &[], &mut never),
            WriteAdmission::Ordinary
        );
    }

    /// Row "After restart, only `unknown`-class cut calls block resume; a call
    /// stopped before it ran is repeatable" (RUN_CONTINUITY.md;
    /// tests/tools/test_restart_effect_class.py test_a_tool_declaration_decides_first,
    /// test_a_reconciling_tool_other_than_run_command_needs_a_decision).
    #[test]
    fn only_unknown_class_cut_calls_block_resume_and_unstarted_calls_repeat() {
        let class = |name, declared, shell| cut_call_effect_class(name, declared, shell);
        assert_eq!(
            class("write_file", RecoveryPolicy::Idempotent, None),
            CutEffectClass::Idempotent
        );
        assert_eq!(
            class(
                "write_file",
                RecoveryPolicy::Reconcile,
                Some(CutEffectClass::ReadOnly)
            ),
            CutEffectClass::Unknown
        );
        assert_eq!(
            class(
                "run_command",
                RecoveryPolicy::Reconcile,
                Some(CutEffectClass::Verifiable)
            ),
            CutEffectClass::Verifiable
        );
        assert_eq!(
            class("run_command", RecoveryPolicy::Reconcile, None),
            CutEffectClass::Unknown
        );
        let cut = |id: &str, state, has_result, class| CutCall {
            call_id: id.into(),
            committed_state: state,
            has_result,
            class,
        };
        let calls = [
            cut(
                "write",
                Some(EffectState::Entered),
                false,
                CutEffectClass::Unknown,
            ),
            cut(
                "read",
                Some(EffectState::Entered),
                false,
                CutEffectClass::ReadOnly,
            ),
            cut(
                "test",
                Some(EffectState::Unknown),
                false,
                CutEffectClass::Verifiable,
            ),
            cut("never", None, false, CutEffectClass::Unknown),
            cut(
                "stopped",
                Some(EffectState::NotStarted),
                false,
                CutEffectClass::Unknown,
            ),
            cut(
                "answered",
                Some(EffectState::Entered),
                true,
                CutEffectClass::Unknown,
            ),
        ];
        assert_eq!(calls_blocking_resume(&calls), vec!["write"]);
        assert!(repeatable_cut_call(
            "write_file",
            Some(EffectState::NotStarted),
            RecoveryPolicy::Reconcile,
            false
        ));
        assert!(!repeatable_cut_call(
            "write_file",
            Some(EffectState::Entered),
            RecoveryPolicy::Reconcile,
            true
        ));

        // A pause that cut a call before entry yields the retryable interruption.
        let paused = FailureContext {
            paused_by: Some("a server restart"),
            ..ctx(RecoveryPolicy::Reconcile)
        };
        let stop = ToolFailure {
            kind: FailureKind::Cancelled,
            type_name: "AbortedError",
            detail: &"stopped",
        };
        let unstarted = entered(RecoveryPolicy::Reconcile).with_state(EffectState::NotStarted);
        let error = project_failure(&paused, Some(&unstarted), &stop).error;
        assert_eq!(
            (error.code.as_str(), error.retryable),
            ("tool_execution_interrupted", true)
        );
        assert_eq!(
            error.message,
            "Tool \"write_file\" never started: a server restart stopped the task first."
        );
        assert!(error
            .next_action
            .starts_with("A server restart paused this work before the call started;"));
        let error =
            project_failure(&paused, Some(&entered(RecoveryPolicy::Reconcile)), &stop).error;
        assert_eq!(error.code, "tool_effect_unknown");
        let incomplete = FailureContext {
            actuals_complete: false,
            ..paused
        };
        assert_eq!(
            project_failure(&incomplete, None, &stop).error.code,
            "tool_execution_cancelled"
        );
    }
}
