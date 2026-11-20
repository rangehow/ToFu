//! Model fallback policy of the loop (pure, no I/O).
//!
//! Entry points: [`FallbackState::model_for_request`] picks the model before
//! each provider dispatch, [`FallbackState::on_outcome`] decides retry /
//! fallback / settle after it, [`FallbackState::on_resume`] elects the model
//! of a resumed Attempt. Free functions [`auto_retry_turn`], [`refused_tag`],
//! [`fallback_max_429_attempts`] and [`model_fallback_receipt`] expose the
//! budgets and the paid-ledger evidence the loop and the review receipt need.
//!
//! Python owners replaced: `lib/tasks_pkg/llm_fallback/` (`_call.py` round
//! dispatch, `_switch.py` MODEL_FALLBACK stamps and the refused-history
//! exemption, `_retry.py` empty-stop classification and the 429 budget,
//! `_refusal.py` refusal retirement), `lib/tasks_pkg/turn_retry.py`
//! (whole-turn auto-retry) and
//! `lib/tasks_pkg/orchestrator/_model_continuation.py` (resume model and the
//! fallback receipt). `round_retry.py` only retires provisional text before a
//! retry; the `discard_partial` flag on a decision carries that instruction
//! to the loop, which owns the actual content epoch.

use sha2::{Digest, Sha256};
use tofu_contracts::generated_events_v1::ModelFallbackEvent;

use crate::engine::types::ProviderOutcome;

/// `lib/tasks_pkg/turn_retry.py` `_AUTO_TURN_RETRY_MAX`: whole-turn re-runs.
pub const AUTO_TURN_RETRY_MAX: u32 = 3;
/// `lib/tasks_pkg/turn_retry.py` `_AUTO_TURN_BACKOFF_BASE_S`.
pub const AUTO_TURN_BACKOFF_BASE_S: f64 = 3.0;
/// `lib/tasks_pkg/turn_retry.py` `_AUTO_TURN_BACKOFF_MAX_S`.
pub const AUTO_TURN_BACKOFF_MAX_S: f64 = 30.0;
/// `lib/tasks_pkg/turn_retry.py` `_AUTO_TURN_BACKOFF_JITTER_S`; the host adds
/// uniform jitter in `[0, JITTER_S)` — this module never draws randomness.
pub const AUTO_TURN_BACKOFF_JITTER_S: f64 = 1.0;
/// `lib/tasks_pkg/llm_fallback/_retry.py` `_DEFAULT_FALLBACK_MAX_429_ATTEMPTS`.
pub const DEFAULT_FALLBACK_MAX_429_ATTEMPTS: u32 = 3;
/// `lib/tasks_pkg/llm_fallback/_retry.py` `_HARD_MAX_FALLBACK_429_ATTEMPTS`.
pub const HARD_MAX_FALLBACK_429_ATTEMPTS: u32 = 16;
/// `lib/tasks_pkg/stream_handler/_budget.py` `_EMPTY_STOP_RETRY_MAX`: same-model
/// empty-stop retries before the stop surfaces as `abnormal_stop`.
pub const EMPTY_STOP_RETRY_MAX: u32 = 2;
/// `lib/llm/llm_errors.py` `_ERR_BODY_LIMIT`: detail kept from the provider error.
pub const ERR_BODY_LIMIT: usize = 4000;
/// `_switch.stamp_model_switch`: `str(reason or '')[:300]` — 300 chars, not bytes.
pub const FALLBACK_REASON_CHAR_LIMIT: usize = 300;
/// `_model_continuation.py`: the refusal's settlement kind and the switch-back kind.
pub const CONTENT_FILTER_KIND: &str = "content_filter";
pub const PRIMARY_RETRY_KIND: &str = "primary_retry";

/// Error kinds that justify re-running the WHOLE turn
/// (`turn_retry.py` `_AUTO_RETRY_KINDS`, exact strings).
pub const AUTO_RETRY_KINDS: [&str; 10] = [
    "ratelimit",
    "no_slot",
    "timeout",
    "network",
    "endpoint_unreachable",
    "premature_close",
    "abnormal_stop",
    "server_offline",
    "upstream_error",
    "tool_timeout",
];

/// `turn_retry.auto_turn_backoff_seconds`, without the jitter (host-side).
pub fn auto_turn_backoff_seconds(attempt: u32) -> f64 {
    let shift = attempt.saturating_sub(1).min(30);
    (AUTO_TURN_BACKOFF_BASE_S * 2f64.powi(shift as i32)).min(AUTO_TURN_BACKOFF_MAX_S)
}

/// `turn_retry.should_auto_retry_turn`: retryable kind, caller opt-in and budget.
/// `recovery_blocked` is `lib.turn_lifecycle.automatic_recovery_blocker(envelope)
/// is not None`; an explicit manual/condition-change blocker vetoes the retry.
pub fn auto_retry_turn(
    kind: &str,
    attempt: u32,
    disabled: bool,
    recovery_blocked: bool,
) -> Option<f64> {
    if disabled || recovery_blocked || attempt >= AUTO_TURN_RETRY_MAX {
        return None;
    }
    if !AUTO_RETRY_KINDS.contains(&kind) {
        return None;
    }
    Some(auto_turn_backoff_seconds(attempt + 1))
}

/// `_retry._fallback_max_429_attempts`: per-task override clamped to `[1, 16]`.
pub fn fallback_max_429_attempts(override_attempts: Option<i64>) -> u32 {
    match override_attempts {
        None => DEFAULT_FALLBACK_MAX_429_ATTEMPTS,
        Some(value) => value.clamp(1, HARD_MAX_FALLBACK_429_ATTEMPTS as i64) as u32,
    }
}

/// `_refusal._discard_refused_attempt`: non-authoring billing tag of a refusal.
pub fn refused_tag(round: u32) -> String {
    format!("R{}-REFUSED", round + 1)
}

/// `turn`-level failure the loop settles without another dispatch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FailureKind {
    /// No distinct fallback / fallback disabled / already on it: a refusal is
    /// not unavailability — no same-model resend, no pool rescue.
    ModelRefusal,
    /// HTTP 450 gateway block: never switches models.
    GatewayContentFilter,
    /// An exhausted same-model empty stop surfaces honestly, never as a filter.
    AbnormalStop,
    /// Both models failed, or a non-retryable error: the typed provider error
    /// the loop settles as `DONE(error)`.
    ProviderError { kind: String },
}

/// What the loop does after one provider outcome.
#[derive(Debug, Clone, PartialEq)]
pub enum FallbackDecision {
    /// Adopt the answer on `model`. On a primary round this retires the sticky
    /// fallback reporting (`_switch.clear_switch_stamps`).
    Accept { model: String },
    /// Same-model resend: empty-stop retry budget, or the per-request 429
    /// budget of the already-switched fallback. `backoff_s` excludes jitter.
    RetrySameModel { model: String, backoff_s: f64 },
    /// Retry the round ONCE on the configured fallback. The loop bills the
    /// refused primary attempt as `R<n>-REFUSED` (non-authoring), discards its
    /// partial text, and publishes `event` BEFORE the fallback stream starts.
    RetryOnFallback {
        event: Box<ModelFallbackEvent>,
        billing_label: String,
        discard_partial: bool,
    },
    /// Settle without another dispatch.
    Fail { kind: FailureKind },
}

/// A paid response-authoring row of the ledger (`checkpointApiRounds` /
/// `api_rounds`). Refused and discarded charges are billed with
/// `response_authoring = false` and never elect a model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerRow {
    pub round: i64,
    pub model: String,
    pub tag: String,
    pub response_authoring: bool,
}

impl LedgerRow {
    fn is_authoring_answer(&self) -> bool {
        if !self.response_authoring {
            return false;
        }
        let tag = self.tag.to_uppercase();
        !tag.contains("REFUSED") && !tag.contains("DISCARDED") && !self.model.trim().is_empty()
    }
}

/// `_model_continuation.last_authoring_model`: highest-round authoring row.
/// The receipt needs that row's model; it never decides the resume's model.
pub fn last_authoring_model(rows: &[LedgerRow]) -> Option<&str> {
    let mut best_round = 0i64;
    let mut model = "";
    for row in rows {
        if row.round <= best_round || !row.is_authoring_answer() {
            continue;
        }
        best_round = row.round;
        model = row.model.trim();
    }
    if model.is_empty() {
        None
    } else {
        Some(model)
    }
}

/// `_model_continuation.fallback_rounds`: authoring round numbers per model,
/// keyed in encounter order (`serde_json::Map` preserves insertion order).
pub fn fallback_rounds(rows: &[LedgerRow]) -> serde_json::Map<String, serde_json::Value> {
    let mut by_model = serde_json::Map::new();
    for row in rows {
        if !row.is_authoring_answer() {
            continue;
        }
        let entry = by_model
            .entry(row.model.trim().to_string())
            .or_insert_with(|| serde_json::Value::Array(Vec::new()));
        if let Some(rounds) = entry.as_array_mut() {
            rounds.push(serde_json::Value::from(row.round));
        }
    }
    by_model
}

/// `_model_continuation.model_fallback_receipt`: the review-visible receipt of
/// the rounds that ran on a non-primary model. Empty when none did.
pub fn model_fallback_receipt(rows: &[LedgerRow], accepted_model: &str) -> serde_json::Value {
    let by_model = fallback_rounds(rows);
    let primary = accepted_model.trim();
    if primary.is_empty() || by_model.is_empty() {
        return serde_json::Value::Object(serde_json::Map::new());
    }
    let mut fallback_round_ids: Vec<i64> = Vec::new();
    let mut names: Vec<String> = Vec::new();
    let mut total = 0usize;
    for (model, rounds) in &by_model {
        let count = rounds.as_array().map_or(0, Vec::len);
        total += count;
        if model != primary {
            if let Some(ids) = rounds.as_array() {
                fallback_round_ids.extend(ids.iter().filter_map(serde_json::Value::as_i64));
            }
            names.push(format!(
                "{} ({} round{})",
                model,
                count,
                if count == 1 { "" } else { "s" }
            ));
        }
    }
    if fallback_round_ids.is_empty() {
        return serde_json::Value::Object(serde_json::Map::new());
    }
    fallback_round_ids.sort_unstable();
    names.sort();
    let mut sorted_by_model = serde_json::Map::new();
    for (model, rounds) in &by_model {
        let mut ids: Vec<i64> = rounds
            .as_array()
            .map(|values| {
                values
                    .iter()
                    .filter_map(serde_json::Value::as_i64)
                    .collect()
            })
            .unwrap_or_default();
        ids.sort_unstable();
        sorted_by_model.insert(
            model.clone(),
            serde_json::Value::Array(ids.into_iter().map(serde_json::Value::from).collect()),
        );
    }
    let count = fallback_round_ids.len();
    serde_json::json!({
        "primaryModel": primary,
        "byModel": sorted_by_model,
        "fallbackRounds": fallback_round_ids,
        "totalRounds": total,
        "notice": format!(
            "{count} of {total} model rounds ran on a fallback model instead of {primary}: {}. A content_filter refusal is per-request, so later rounds retried the primary.",
            names.join(", ")
        ),
    })
}

/// Live fallback state of one Attempt. Mirrors `task['_fallback_model']`,
/// `task['_fallback_from']`, `task['_fallback_kind']`,
/// `task['_refused_history_length']` / `task['_refused_history_digest']` — all
/// process-local, so a resume starts from [`FallbackState::on_resume`].
#[derive(Debug, Clone)]
pub struct FallbackState {
    primary: String,
    fallback: String,
    fallback_disabled: bool,
    active_model: String,
    /// Model the current attempt was actually dispatched on; set by
    /// [`FallbackState::begin_request`], read by `on_outcome`.
    dispatch_model: String,
    active_kind: Option<String>,
    refused_history_length: Option<usize>,
    refused_history_digest: Option<String>,
    /// `task['_refusal_skip_left']`: the one request that repeats the refused
    /// payload plus the fallback's served answer stays on the fallback.
    refusal_skip_left: bool,
    empty_stop_retries: u32,
    fallback_429_attempts: u32,
    fallback_429_max: u32,
}

impl FallbackState {
    pub fn new(primary: impl Into<String>, fallback: impl Into<String>) -> Self {
        Self::with_config(primary, fallback, false, None)
    }

    /// `fallback_disabled` is `task['config']['disableModelFallback']`;
    /// `fallback_429_override` is `task['config']['fallbackMax429Attempts']`.
    pub fn with_config(
        primary: impl Into<String>,
        fallback: impl Into<String>,
        fallback_disabled: bool,
        fallback_429_override: Option<i64>,
    ) -> Self {
        let primary = primary.into();
        let fallback = fallback.into();
        Self {
            dispatch_model: primary.clone(),
            active_model: primary.clone(),
            primary,
            fallback,
            fallback_disabled,
            active_kind: None,
            refused_history_length: None,
            refused_history_digest: None,
            refusal_skip_left: false,
            empty_stop_retries: 0,
            fallback_429_attempts: 0,
            fallback_429_max: fallback_max_429_attempts(fallback_429_override),
        }
    }

    /// `_model_continuation.continue_resumed_model`: a resume continues the
    /// WORK, it does not re-decide the model. The accepted model (already the
    /// deliberate pause → set_model → continue choice when one exists) wins;
    /// the paid ledger never elects the model. A repeated refusal on the first
    /// resumed round falls back again for that round only.
    pub fn on_resume(accepted_model: impl Into<String>) -> Self {
        let accepted = accepted_model.into();
        Self {
            dispatch_model: accepted.clone(),
            active_model: accepted.clone(),
            primary: accepted,
            fallback: String::new(),
            fallback_disabled: true,
            active_kind: None,
            refused_history_length: None,
            refused_history_digest: None,
            refusal_skip_left: false,
            empty_stop_retries: 0,
            fallback_429_attempts: 0,
            fallback_429_max: DEFAULT_FALLBACK_MAX_429_ATTEMPTS,
        }
    }

    /// Model for the next provider dispatch. `in_refused_tool_loop` is
    /// `!_switch.retry_primary_for_messages(task, messages)`, evaluated by the
    /// loop against the CURRENT history: true only while the history is
    /// exactly the refused prefix plus this attempt's own appended tool rows
    /// (the served assistant tool-call row and its tool results). Use
    /// [`FallbackState::retry_primary_for_messages`] with the recorded prefix
    /// to compute it. Only a content-filter refusal has the exemption; a
    /// rate-limit/HTTP fallback keeps serving the fallback until a primary
    /// round succeeds.
    pub fn model_for_request(&self, in_refused_tool_loop: bool) -> &str {
        if self.active_kind.as_deref() == Some(CONTENT_FILTER_KIND) && !in_refused_tool_loop {
            &self.primary
        } else {
            &self.active_model
        }
    }

    /// Dispatch entry: the loop calls this once per provider request, with
    /// `in_refused_tool_loop = !retry_primary_for_messages(messages)`, and
    /// sends on the returned model. `on_outcome` attributes the outcome to
    /// this model, so a return probe's refusal is billed on the primary.
    pub fn begin_request(&mut self, in_refused_tool_loop: bool) -> &str {
        self.dispatch_model = self.model_for_request(in_refused_tool_loop).to_string();
        &self.dispatch_model
    }

    /// `_switch.stamp_switch_back`: the MODEL_FALLBACK event (kind
    /// `primary_retry`, direction primary) that names the return probe. The
    /// loop publishes it when `model_for_request` flips back to the primary,
    /// before building that round's request. Returns `None` when no return
    /// probe happens (including `primary == fallback`, which elects nothing).
    pub fn switch_back_event(&self, round: u32) -> Option<ModelFallbackEvent> {
        if self.active_kind.as_deref() != Some(CONTENT_FILTER_KIND)
            || self.active_model == self.primary
            || self.active_model.is_empty()
            || self.primary.is_empty()
        {
            return None;
        }
        Some(switch_event(
            &self.active_model,
            &self.primary,
            PRIMARY_RETRY_KIND,
            &format!(
                "The refusal applies to one request; round {} returns to the primary model.",
                round + 1
            ),
        ))
    }

    /// Decide what one provider outcome means for the round.
    ///
    /// The caller passes `round` (0-based, the Python `round_num`) and the
    /// outcome of the attempt dispatched on [`FallbackState::model_for_request`].
    /// The outcome is attributed to the model `begin_request` dispatched on.
    /// `Accept` on the primary clears the refusal state; a content-filter
    /// `RetryOnFallback` records the refusal, so the NEXT request returns to
    /// the primary unless the loop reports the refused tool loop; an
    /// availability `RetryOnFallback` latches the fallback for the attempt. A `Completed` empty `stop` is a
    /// transient retry, never a content filter; `HttpError` 402/429 are
    /// routing/quota failures and never stamp a refusal.
    pub fn on_outcome(&mut self, round: u32, outcome: &ProviderOutcome) -> FallbackDecision {
        match outcome {
            ProviderOutcome::ContentFilter { .. } => {
                self.empty_stop_retries = 0;
                if !self.has_distinct_fallback() {
                    return FallbackDecision::Fail {
                        kind: FailureKind::ModelRefusal,
                    };
                }
                self.active_kind = Some(CONTENT_FILTER_KIND.to_string());
                // `_switch.record_refused_history`: the loop snapshots the
                // refused prefix (length + digest) into the state right after
                // this decision, from the messages it actually sent.
                FallbackDecision::RetryOnFallback {
                    event: Box::new(switch_event(
                        &self.model_at_dispatch(),
                        &self.fallback,
                        CONTENT_FILTER_KIND,
                        &format!("{CONTENT_FILTER_KIND}: model refused (content_filter finish)"),
                    )),
                    billing_label: refused_tag(round),
                    discard_partial: true,
                }
            }
            ProviderOutcome::HttpError { status, detail, .. } => {
                self.empty_stop_retries = 0;
                if *status == 450 {
                    return FallbackDecision::Fail {
                        kind: FailureKind::GatewayContentFilter,
                    };
                }
                let kind = http_error_kind(*status);
                let on_fallback = self.model_at_dispatch() == self.fallback;
                if on_fallback {
                    // `_retry._fallback_max_429_attempts`: only the switched
                    // fallback's 429s stay bounded on the same model; other
                    // failures settle (pool rescue is a host policy).
                    if *status == 429 && self.fallback_429_attempts < self.fallback_429_max {
                        self.fallback_429_attempts += 1;
                        return FallbackDecision::RetrySameModel {
                            model: self.fallback.clone(),
                            backoff_s: auto_turn_backoff_seconds(self.fallback_429_attempts),
                        };
                    }
                    return FallbackDecision::Fail {
                        kind: FailureKind::ProviderError {
                            kind: kind.to_string(),
                        },
                    };
                }
                // Sensitive-word 5xx is the same deterministic refusal as a
                // content_filter finish (TURN_SETTLEMENT.md).
                if (500..600).contains(status)
                    && detail
                        .trim_start()
                        .starts_with("user sensitive words detected:")
                    && self.has_distinct_fallback()
                {
                    return FallbackDecision::RetryOnFallback {
                        event: Box::new(switch_event(
                            &self.model_at_dispatch(),
                            &self.fallback,
                            CONTENT_FILTER_KIND,
                            &fallback_reason(CONTENT_FILTER_KIND, detail),
                        )),
                        billing_label: refused_tag(round),
                        discard_partial: true,
                    };
                }
                if !self.has_distinct_fallback() {
                    return FallbackDecision::Fail {
                        kind: FailureKind::ProviderError {
                            kind: kind.to_string(),
                        },
                    };
                }
                // An availability fallback latches for the rest of the
                // attempt; only a content_filter refusal probes the primary.
                self.active_model = self.fallback.clone();
                self.active_kind = Some(kind.to_string());
                FallbackDecision::RetryOnFallback {
                    event: Box::new(switch_event(
                        &self.model_at_dispatch(),
                        &self.fallback,
                        kind,
                        &fallback_reason(kind, detail),
                    )),
                    billing_label: format!("R{}", round + 1),
                    discard_partial: false,
                }
            }
            ProviderOutcome::Dropped { detail, .. } => {
                self.empty_stop_retries = 0;
                if self.has_distinct_fallback() && self.model_at_dispatch() != self.fallback {
                    return FallbackDecision::RetryOnFallback {
                        event: Box::new(switch_event(
                            &self.model_at_dispatch(),
                            &self.fallback,
                            "network",
                            &fallback_reason("network", detail),
                        )),
                        billing_label: format!("R{}", round + 1),
                        discard_partial: true,
                    };
                }
                FallbackDecision::Fail {
                    kind: FailureKind::ProviderError {
                        kind: "network".to_string(),
                    },
                }
            }
            ProviderOutcome::Internal { .. } => FallbackDecision::Fail {
                kind: FailureKind::ProviderError {
                    kind: "internal".into(),
                },
            },
            ProviderOutcome::Cancelled { .. } => FallbackDecision::Fail {
                kind: FailureKind::ProviderError {
                    kind: "cancelled".to_string(),
                },
            },
            ProviderOutcome::Completed {
                text,
                tool_calls,
                finish_reason,
                ..
            } => {
                // `_retry._flag_empty_stop_for_retry` + `_EMPTY_STOP_RETRY_MAX`.
                if is_empty_stop(text, tool_calls.is_empty(), finish_reason)
                    && self.empty_stop_retries < EMPTY_STOP_RETRY_MAX
                {
                    self.empty_stop_retries += 1;
                    return FallbackDecision::RetrySameModel {
                        model: self.model_at_dispatch(),
                        backoff_s: 0.0,
                    };
                }
                if is_empty_stop(text, tool_calls.is_empty(), finish_reason) {
                    return FallbackDecision::Fail {
                        kind: FailureKind::AbnormalStop,
                    };
                }
                self.empty_stop_retries = 0;
                let model = self.model_at_dispatch();
                if self.active_kind.as_deref() == Some(CONTENT_FILTER_KIND) && model == self.primary
                {
                    // A served primary round retires the sticky refusal.
                    self.clear_refusal();
                }
                FallbackDecision::Accept { model }
            }
        }
    }

    /// `_switch.record_refused_history`: the loop calls this with the exact
    /// messages of the refused request, immediately after `RetryOnFallback`
    /// (only for a content_filter refusal — not for availability fallbacks).
    /// Only the refused PREFIX is remembered — comparing the whole growing
    /// list would always differ and probe the primary every tool round.
    pub fn record_refused_history(&mut self, messages: &[crate::engine::types::Message]) {
        let new_episode = self.refused_history_length.is_none();
        self.refused_history_length = Some(messages.len());
        self.refused_history_digest = Some(history_digest(messages));
        if new_episode {
            self.refusal_skip_left = true;
        }
        self.active_model = self.fallback.clone();
    }

    /// The dispatch gate (`TURN_SETTLEMENT.md` "Content-filter outcomes"):
    /// true, once, for the single request that repeats the refused history
    /// plus the fallback's own tool rows; that request stays on the fallback
    /// and consumes the skip, so every later request returns to the primary.
    /// Call it exactly once per request; preparation and transport must share
    /// this request-owned decision. A primary probe spends any remaining skip
    /// and a repeated refusal cannot re-arm it.
    pub fn take_refused_tool_loop(&mut self, messages: &[crate::engine::types::Message]) -> bool {
        if self.refusal_skip_left && !self.retry_primary_for_messages(messages) {
            self.refusal_skip_left = false;
            return true;
        }
        self.refusal_skip_left = false;
        false
    }

    /// `_switch.retry_primary_for_messages`: false only for exactly the
    /// refused prefix plus this attempt's own appended tool rows.
    pub fn retry_primary_for_messages(&self, messages: &[crate::engine::types::Message]) -> bool {
        let (Some(length), Some(refused)) = (
            self.refused_history_length,
            self.refused_history_digest.as_deref(),
        ) else {
            return true;
        };
        if length >= messages.len() || refused.is_empty() {
            return true;
        }
        if history_digest(&messages[..length]) != refused {
            return true;
        }
        !appended_rows_are_tool_rounds(&messages[length..])
    }

    /// A served primary round retires the sticky fallback reporting.
    pub fn clear_refusal(&mut self) {
        self.active_model = self.primary.clone();
        self.active_kind = None;
        self.refused_history_length = None;
        self.refused_history_digest = None;
        self.refusal_skip_left = false;
    }

    fn has_distinct_fallback(&self) -> bool {
        !self.fallback_disabled
            && !self.fallback.is_empty()
            && self.fallback != self.model_at_dispatch()
    }

    /// The model the current outcome was dispatched on: the return probe is
    /// visible in `model_for_request` but the refusal lands on the primary.
    fn model_at_dispatch(&self) -> String {
        self.dispatch_model.clone()
    }
}

fn switch_event(source: &str, destination: &str, kind: &str, reason: &str) -> ModelFallbackEvent {
    ModelFallbackEvent {
        fallback_model: destination.to_string(),
        fallback_from: source.to_string(),
        fallback_kind: kind.to_string(),
        fallback_reason: reason.chars().take(FALLBACK_REASON_CHAR_LIMIT).collect(),
        emitted_at: None,
        execution_at_ms: None,
        execution_origin_at_ms: None,
        execution_clock_id: None,
        task_id: None,
        conversation_id: None,
        attempt_id: None,
        request_id: None,
        seq: None,
        flow_projection: None,
        turn_role: None,
        emits: None,
        vu_msg_id: None,
        autopilot_run_id: None,
    }
}

/// `_configured.call_configured_fallback`: `'{kind}: {detail}'`, detail capped
/// at `_ERR_BODY_LIMIT` before the envelope caps the whole reason at 300 chars.
fn fallback_reason(kind: &str, detail: &str) -> String {
    let detail: String = detail.trim().chars().take(ERR_BODY_LIMIT).collect();
    if detail.is_empty() {
        kind.to_string()
    } else {
        format!("{kind}: {detail}")
    }
}

/// `lib.error_envelope` kinds the fallback path stamps for HTTP failures.
fn http_error_kind(status: u16) -> &'static str {
    match status {
        402 => "quota",
        429 => "ratelimit",
        408 | 504 => "timeout",
        500..=599 => "upstream_error",
        _ => "generic",
    }
}

/// `_retry._flag_empty_stop_for_retry`: a bare empty/whitespace `stop` with no
/// tool payload. `length`/`max_tokens` truncation is a different lane.
fn is_empty_stop(text: &str, no_tool_calls: bool, finish_reason: &str) -> bool {
    finish_reason == "stop" && no_tool_calls && text.trim().is_empty()
}

/// `_switch._history_digest`: sha256 over the JSON of the messages. The
/// refused stamp is process-local, so the digest needs internal consistency
/// (same history ⇒ same digest), not byte parity with the Python serializer.
fn history_digest(messages: &[crate::engine::types::Message]) -> String {
    let rows: Vec<serde_json::Value> = messages
        .iter()
        .map(|message| {
            serde_json::json!({
                "role": message.role.as_str(),
                "content": message.content,
                "tool_calls": message.tool_calls.iter().map(|call| serde_json::json!({
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                })).collect::<Vec<_>>(),
                "tool_call_id": message.tool_call_id,
            })
        })
        .collect();
    let blob = serde_json::to_string(&rows).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(blob.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// `_switch._appended_rows_are_tool_rounds`: the served assistant tool-call
/// row and its tool results — anything else ends the refusal's exemption.
fn appended_rows_are_tool_rounds(rows: &[crate::engine::types::Message]) -> bool {
    !rows.is_empty()
        && rows.iter().all(|row| {
            row.role == crate::engine::types::Role::Tool
                || (row.role == crate::engine::types::Role::Assistant && !row.tool_calls.is_empty())
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::types::{Message, Role, ToolCall};

    const PRIMARY: &str = "claude-opus-5.5";
    const FALLBACK: &str = "kimi-k3";

    fn new_state() -> FallbackState {
        FallbackState::new(PRIMARY, FALLBACK)
    }

    fn refused() -> ProviderOutcome {
        ProviderOutcome::ContentFilter {
            partial_text: "Partial refused text".to_string(),
            detail: "category: cyber".to_string(),
        }
    }

    fn answered(text: &str) -> ProviderOutcome {
        ProviderOutcome::Completed {
            text: text.to_string(),
            tool_calls: Vec::new(),
            finish_reason: "stop".to_string(),
            usage: serde_json::Value::Null,
        }
    }

    fn http(status: u16, detail: &str) -> ProviderOutcome {
        ProviderOutcome::HttpError {
            status,
            detail: detail.to_string(),
            partial_text: String::new(),
        }
    }

    fn fallback_event(decision: &FallbackDecision) -> &ModelFallbackEvent {
        match decision {
            FallbackDecision::RetryOnFallback { event, .. } => event,
            other => panic!("expected RetryOnFallback, got {other:?}"),
        }
    }

    /// Easy-to-lose row: "A content-filter or refusal skips same-model
    /// retries, bills `R<n>-REFUSED`, discards partial text and retries once
    /// on the fallback". Python: test_model_refusal_fallback.py
    /// ::test_refusal_discards_partial_output_and_answers_on_fallback.
    #[test]
    fn content_filter_skips_same_model_retry_bills_refused_and_discards_partial() {
        let mut state = new_state();
        let decision = state.on_outcome(5, &refused());
        let FallbackDecision::RetryOnFallback {
            billing_label,
            discard_partial,
            event,
            ..
        } = &decision
        else {
            panic!("expected RetryOnFallback, got {decision:?}");
        };
        assert_eq!(billing_label, "R6-REFUSED");
        assert!(*discard_partial);
        assert_eq!(event.fallback_model, FALLBACK);
        assert_eq!(event.fallback_from, PRIMARY);
        assert_eq!(event.fallback_kind, CONTENT_FILTER_KIND);
        assert!(event.fallback_reason.starts_with("content_filter: "));
    }

    /// Same row, terminal side: without a distinct fallback a refusal settles
    /// typed with no same-model resend and no pool rescue; a refused fallback
    /// also settles. Python: ::test_refusal_without_distinct_fallback_settles_typed_never_resends,
    /// ::test_fallback_refusal_also_settles_with_both_attempts_billed.
    #[test]
    fn refusal_without_distinct_fallback_settles_never_resends() {
        let mut no_fallback = FallbackState::new(PRIMARY, "");
        assert_eq!(
            no_fallback.on_outcome(0, &refused()),
            FallbackDecision::Fail {
                kind: FailureKind::ModelRefusal
            }
        );
        let mut disabled = FallbackState::with_config(PRIMARY, FALLBACK, true, None);
        assert_eq!(
            disabled.on_outcome(0, &refused()),
            FallbackDecision::Fail {
                kind: FailureKind::ModelRefusal
            }
        );
        let mut already_on_fallback = FallbackState::new(FALLBACK, FALLBACK);
        assert_eq!(
            already_on_fallback.on_outcome(0, &refused()),
            FallbackDecision::Fail {
                kind: FailureKind::ModelRefusal
            }
        );
    }

    /// Easy-to-lose row: "A refusal applies to one request; the next returns
    /// to the primary except inside the refused tool loop; every switch emits
    /// MODEL_FALLBACK". Python: ::test_next_request_after_refusal_returns_to_primary_and_audits_it.
    #[test]
    fn next_request_after_refusal_returns_to_primary_with_event() {
        let mut state = new_state();
        state.on_outcome(55, &refused());
        state.record_refused_history(&[Message::text(Role::User, "research objective")]);
        assert_eq!(state.active_model, FALLBACK);

        let mut history = vec![Message::text(Role::User, "research objective")];
        history.push(Message::text(Role::Assistant, "fallback r56"));
        history.push(Message::text(Role::User, "the lead's next request"));
        assert!(state.retry_primary_for_messages(&history));
        assert!(state.retry_primary_for_messages(&history));
        // in_refused_tool_loop = !retry_primary_for_messages = false
        assert_eq!(state.model_for_request(false), PRIMARY);
        let event = state.switch_back_event(56).expect("return probe event");
        assert_eq!(event.fallback_model, PRIMARY);
        assert_eq!(event.fallback_from, FALLBACK);
        assert_eq!(event.fallback_kind, PRIMARY_RETRY_KIND);
        assert_eq!(
            event.fallback_reason,
            "The refusal applies to one request; round 57 returns to the primary model."
        );
        // A served primary round retires the sticky refusal.
        state.on_outcome(56, &answered("primary r57"));
        assert_eq!(state.model_for_request(false), PRIMARY);
        assert!(state.switch_back_event(57).is_none());
    }

    /// Same row: a repeated refusal on the return probe falls back again for
    /// that request only, never sticks. Python:
    /// ::test_refusal_on_the_return_probe_falls_back_for_that_request_only.
    #[test]
    fn refusal_on_return_probe_falls_back_for_that_request_only() {
        let mut state = new_state();
        for round in 0..3 {
            state.on_outcome(round * 2, &refused());
            state.record_refused_history(&[Message::text(Role::User, "research objective")]);
            assert_eq!(state.model_for_request(false), PRIMARY);
            assert!(state.switch_back_event(round * 2 + 1).is_some());
            let served = state.on_outcome(round * 2 + 1, &answered("primary again"));
            assert_eq!(
                served,
                FallbackDecision::Accept {
                    model: PRIMARY.into()
                }
            );
        }
    }

    /// Same row, the exemption: only the refused attempt's OWN appended tool
    /// rounds (assistant tool-call row + tool results) stay on the fallback;
    /// a changed/shorter/compacted prefix or any other injection probes the
    /// primary. Python: ::test_same_attempt_appended_tool_rounds_stay_on_fallback,
    /// ::test_changed_history_retries_primary_unchanged_history_stays_on_fallback,
    /// ::test_tool_loop_appending_fallback_rounds_does_not_retry_primary.
    #[test]
    fn refused_tool_loop_stays_on_fallback_until_history_changes() {
        let mut state = new_state();
        let refused_history = vec![Message::text(Role::User, "unchanged research history")];
        state.on_outcome(0, &refused());
        state.record_refused_history(&refused_history);

        let mut tool_loop = refused_history.clone();
        tool_loop.push(Message {
            role: Role::Assistant,
            content: String::new(),
            tool_calls: vec![ToolCall {
                id: "c1".into(),
                name: "read_files".into(),
                arguments: "{}".into(),
            }],
            tool_call_id: None,
        });
        tool_loop.push(Message::tool_result("c1", "file body"));
        assert!(!state.retry_primary_for_messages(&tool_loop));
        assert_eq!(state.model_for_request(true), FALLBACK);
        assert!(state.switch_back_event(1).is_some()); // event exists but the loop skips it

        // A new user request ends the exemption.
        let mut next_turn = tool_loop.clone();
        next_turn.push(Message::text(Role::User, "next request"));
        assert!(state.retry_primary_for_messages(&next_turn));

        // A compacted / rewritten prefix probes the primary.
        assert!(state.retry_primary_for_messages(&[Message::text(
            Role::User,
            "a different, compacted history"
        )]));
        // A shorter (replaced) history probes the primary.
        assert!(state.retry_primary_for_messages(&[]));
        // No recorded prefix (legacy stamp) probes the primary.
        let mut legacy = state.clone();
        legacy.refused_history_digest = None;
        assert!(legacy.retry_primary_for_messages(&tool_loop));
    }

    /// MODEL_FALLBACK reason is `str(reason or '')[:300]` — 300 CHARS.
    /// Python: _switch.stamp_model_switch (no dedicated test; pinned here).
    #[test]
    fn fallback_reason_is_capped_at_300_chars_not_bytes() {
        let detail = "é".repeat(500);
        let event = switch_event(
            PRIMARY,
            FALLBACK,
            "ratelimit",
            &fallback_reason("ratelimit", &detail),
        );
        assert_eq!(
            event.fallback_reason.chars().count(),
            FALLBACK_REASON_CHAR_LIMIT
        );
        assert!(event.fallback_reason.starts_with("ratelimit: "));
    }

    /// Easy-to-lose row: "Empty or truncated stop is not a content filter;
    /// 402 and 429 never stamp one". Python:
    /// test_empty_stop_not_content_filter.py (unit cases + exhausted budget
    /// surfaces abnormal_stop) and test_model_refusal_fallback.py
    /// ::test_credit_and_rate_limit_stay_out_of_the_refusal_lane.
    #[test]
    fn empty_and_truncated_stop_are_never_content_filter() {
        let mut state = new_state();
        // Empty stop: two same-model retries, then abnormal_stop — never a filter.
        for expected_retries in [1, 2] {
            let decision = state.on_outcome(0, &answered(""));
            assert_eq!(
                decision,
                FallbackDecision::RetrySameModel {
                    model: PRIMARY.into(),
                    backoff_s: 0.0
                }
            );
            assert_eq!(state.empty_stop_retries, expected_retries);
        }
        assert_eq!(
            state.on_outcome(0, &answered("   \n")),
            FallbackDecision::Fail {
                kind: FailureKind::AbnormalStop
            }
        );
        // Whitespace-only body and earlier prose do not mask the current round.
        let mut state = new_state();
        assert!(matches!(
            state.on_outcome(3, &answered("  ")),
            FallbackDecision::RetrySameModel { .. }
        ));
        // Truncated finish is a different lane entirely: adopted, not retried.
        let truncated = ProviderOutcome::Completed {
            text: "partial but real".into(),
            tool_calls: Vec::new(),
            finish_reason: "length".into(),
            usage: serde_json::Value::Null,
        };
        let mut truncated_state = new_state();
        assert_eq!(
            truncated_state.on_outcome(0, &truncated),
            FallbackDecision::Accept {
                model: PRIMARY.into()
            }
        );
        // Real content / tool payloads are not flagged.
        let mut answered_state = new_state();
        assert!(matches!(
            answered_state.on_outcome(0, &answered("real answer")),
            FallbackDecision::Accept { .. }
        ));
    }

    /// Same row: 402/429 fall back with their quota/ratelimit kind and no
    /// `-REFUSED` billing, and the primary 402/429 never stamps a refusal.
    /// Python: ::test_credit_and_rate_limit_stay_out_of_the_refusal_lane;
    /// tests/llm/test_llm_replay_scenarios.py::test_rate_limit_fallback.
    #[test]
    fn http_402_and_429_fall_back_without_refusal_stamp() {
        for (status, kind) in [(402, "quota"), (429, "ratelimit")] {
            let mut state = new_state();
            let decision = state.on_outcome(0, &http(status, "API HTTP error"));
            let FallbackDecision::RetryOnFallback {
                billing_label,
                discard_partial,
                ..
            } = &decision
            else {
                panic!("expected RetryOnFallback, got {decision:?}");
            };
            assert_eq!(fallback_event(&decision).fallback_kind, kind);
            assert_eq!(billing_label, "R1");
            assert!(!discard_partial);
            assert!(!billing_label.ends_with("-REFUSED"));
            // The availability fallback latches for the next request (only
            // content_filter has the return-to-primary probe).
            assert_eq!(state.begin_request(false), FALLBACK);
            assert!(state.switch_back_event(1).is_none());
        }
    }

    /// The switched fallback's own 429s use the bounded same-model budget
    /// (`_DEFAULT_FALLBACK_MAX_429_ATTEMPTS = 3`, hard clamp 16), then settle.
    /// Python: _retry._fallback_max_429_attempts.
    #[test]
    fn fallback_429_budget_is_bounded_then_settles() {
        let mut state = new_state();
        state.begin_request(false);
        state.on_outcome(0, &http(429, "rate limit"));
        state.begin_request(false);
        for _ in 0..DEFAULT_FALLBACK_MAX_429_ATTEMPTS {
            assert!(matches!(
                state.on_outcome(0, &http(429, "rate limit")),
                FallbackDecision::RetrySameModel { model, .. } if model == FALLBACK
            ));
        }
        assert_eq!(
            state.on_outcome(0, &http(429, "rate limit")),
            FallbackDecision::Fail {
                kind: FailureKind::ProviderError {
                    kind: "ratelimit".into()
                }
            }
        );
        assert_eq!(fallback_max_429_attempts(None), 3);
        assert_eq!(fallback_max_429_attempts(Some(0)), 1);
        assert_eq!(fallback_max_429_attempts(Some(99)), 16);
    }

    /// HTTP 450 gateway block: terminal content_filter, never a model switch.
    /// Python: ::test_gateway_block_never_switches_models.
    #[test]
    fn gateway_http_450_never_switches_models() {
        let mut state = new_state();
        assert_eq!(
            state.on_outcome(0, &http(450, "blocked by policy")),
            FallbackDecision::Fail {
                kind: FailureKind::GatewayContentFilter
            }
        );
        assert_eq!(state.model_for_request(false), PRIMARY);
    }

    /// Sensitive-word 5xx is a deterministic refusal: immediate fallback,
    /// REFUSED billing. Python: ::test_sensitive_word_http_500_skips_retry_and_uses_configured_fallback.
    #[test]
    fn sensitive_word_http_500_skips_retry_and_uses_fallback() {
        let mut state = new_state();
        let decision = state.on_outcome(0, &http(500, "user sensitive words detected: example"));
        assert_eq!(fallback_event(&decision).fallback_kind, CONTENT_FILTER_KIND);
        match decision {
            FallbackDecision::RetryOnFallback { billing_label, .. } => {
                assert_eq!(billing_label, "R1-REFUSED")
            }
            other => panic!("{other:?}"),
        }
    }

    /// Easy-to-lose row: "A resumed Attempt runs on the accepted model, not
    /// the last fallback". Python: test_resume_model_continuation.py
    /// ::test_resume_returns_to_accepted_model_after_a_fallback,
    /// ::test_explicit_model_switch_on_continue_wins,
    /// ::test_refused_and_discarded_charges_never_elect_a_model.
    #[test]
    fn resumed_attempt_runs_on_accepted_model_not_last_fallback() {
        let rows = vec![
            LedgerRow {
                round: 1,
                model: PRIMARY.into(),
                tag: "R1".into(),
                response_authoring: true,
            },
            LedgerRow {
                round: 2,
                model: FALLBACK.into(),
                tag: "R2-FALLBACK".into(),
                response_authoring: true,
            },
            LedgerRow {
                round: 3,
                model: FALLBACK.into(),
                tag: "R3-REFUSED".into(),
                response_authoring: false,
            },
        ];
        // The ledger's last authoring row is the fallback; it never elects.
        assert_eq!(last_authoring_model(&rows), Some(FALLBACK));
        let resumed = FallbackState::on_resume(PRIMARY);
        assert_eq!(resumed.model_for_request(false), PRIMARY);
        // An explicit switch IS the accepted model passed by the caller.
        let switched = FallbackState::on_resume("owner-chosen-model");
        assert_eq!(switched.model_for_request(false), "owner-chosen-model");
        // A repeated refusal on the first resumed round falls back once more.
        let mut resumed = FallbackState::on_resume(PRIMARY);
        resumed.fallback = FALLBACK.into();
        resumed.fallback_disabled = false;
        assert!(matches!(
            resumed.on_outcome(0, &refused()),
            FallbackDecision::RetryOnFallback { .. }
        ));
    }

    /// The review receipt names the fallback rounds; refused/discarded charges
    /// are not answers. Python: ::test_refused_and_discarded_charges_never_elect_a_model
    /// and _model_continuation.model_fallback_receipt.
    #[test]
    fn fallback_receipt_names_fallback_rounds_and_skips_refused_charges() {
        let rows = vec![
            LedgerRow {
                round: 1,
                model: PRIMARY.into(),
                tag: "R1".into(),
                response_authoring: true,
            },
            LedgerRow {
                round: 2,
                model: FALLBACK.into(),
                tag: "R2-FALLBACK".into(),
                response_authoring: true,
            },
            LedgerRow {
                round: 3,
                model: FALLBACK.into(),
                tag: "R3-FALLBACK".into(),
                response_authoring: true,
            },
            LedgerRow {
                round: 4,
                model: "other".into(),
                tag: "R4-DISCARDED".into(),
                response_authoring: true,
            },
        ];
        let receipt = model_fallback_receipt(&rows, PRIMARY);
        assert_eq!(receipt["primaryModel"], PRIMARY);
        assert_eq!(receipt["fallbackRounds"], serde_json::json!([2, 3]));
        assert_eq!(receipt["totalRounds"], 3);
        assert!(receipt["notice"]
            .as_str()
            .unwrap()
            .contains("kimi-k3 (2 rounds)"));
        // Primary-only ledger → empty receipt.
        let primary_only = &rows[..1];
        assert_eq!(
            model_fallback_receipt(primary_only, PRIMARY),
            serde_json::json!({})
        );
    }

    /// `turn_retry.should_auto_retry_turn`: exact kinds, cap 3, opt-out and
    /// recovery blocker veto, backoff 3/6/12 capped at 30 (+host jitter).
    /// Python: lib/tasks_pkg/turn_retry.py (whole-turn budget named here).
    #[test]
    fn whole_turn_auto_retry_budget_and_backoff_match_python() {
        assert_eq!(auto_retry_turn("ratelimit", 0, false, false), Some(3.0));
        assert_eq!(auto_retry_turn("network", 1, false, false), Some(6.0));
        assert_eq!(
            auto_retry_turn("upstream_error", 2, false, false),
            Some(12.0)
        );
        assert_eq!(auto_retry_turn("ratelimit", 3, false, false), None);
        assert_eq!(auto_retry_turn("ratelimit", 0, true, false), None);
        assert_eq!(auto_retry_turn("ratelimit", 0, false, true), None);
        assert_eq!(auto_retry_turn("content_filter", 0, false, false), None);
        assert_eq!(auto_retry_turn("quota", 0, false, false), None);
        assert_eq!(auto_turn_backoff_seconds(10), AUTO_TURN_BACKOFF_MAX_S);
        assert_eq!(AUTO_TURN_BACKOFF_JITTER_S, 1.0);
        assert_eq!(AUTO_RETRY_KINDS.len(), 10);
    }
}

#[cfg(test)]
mod refusal_skip_tests {
    use super::*;
    use crate::engine::types::{Message, Role, ToolCall};

    fn tool_round(id: &str) -> [Message; 2] {
        [
            Message {
                role: Role::Assistant,
                content: String::new(),
                tool_calls: vec![ToolCall {
                    id: id.into(),
                    name: "read_files".into(),
                    arguments: "{}".into(),
                }],
                tool_call_id: None,
            },
            Message::tool_result(id, "ok"),
        ]
    }

    /// Row "Fallback: a refusal applies to one request; the next returns to
    /// the primary" with the bounded exemption of 115576397b
    /// (docs/contracts/TURN_SETTLEMENT.md "Content-filter outcomes"): only
    /// the single request repeating the refused payload plus the fallback's
    /// tool rows stays on the fallback; every later tool round returns.
    #[test]
    fn only_the_first_tool_round_after_a_refusal_stays_on_the_fallback() {
        let mut state = FallbackState::new("primary", "fallback");
        let refused = vec![Message::text(Role::User, "q")];
        state.record_refused_history(&refused);
        let mut history = refused.clone();
        history.extend(tool_round("a"));
        assert!(state.take_refused_tool_loop(&history));
        history.extend(tool_round("b"));
        assert!(!state.take_refused_tool_loop(&history));
    }
}
