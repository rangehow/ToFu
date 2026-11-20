//! Who stopped a Turn: the one `ABORT_CAUSES` table.
//!
//! Replaces `lib/turn_verdict/__init__.py` (`ABORT_CAUSES`, `abort_cause`,
//! `system_abort_label`, `_SYSTEM_STOP_VERDICTS`) and the stop branch of
//! `lib/tasks_pkg/orchestrator/_finalize.py::_settle_post_loop_finish_reason`
//! (docs/contracts/TURN_SETTLEMENT.md, "Abort causes").
//!
//! Entry points: `abort_cause` (the cause record of an abort reason; an
//! unregistered reason reads as a person's Stop and is flagged), the caller-
//! owned `UnregisteredAbortReasons` (decides the one ERROR log per process),
//! `system_abort_label`, `stop_verdict` (the Turn verdict of a stop) and
//! `post_loop_stop` (finish reason and error envelope the finalizer settles).
//! Pure: no I/O, no logging, no global state.

use std::collections::HashSet;

/// Who stopped work that carries an abort reason (`AbortInitiator`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AbortInitiator {
    Person,
    ServerRestart,
    LeadHold,
    StorageFence,
    DispatchAuthority,
    StuckTaskReaper,
    TofuGuard,
}

/// How every reader reports work stopped with one abort reason.
/// `label` names a system stop (`{reason}` is the stamp); empty for a person.
/// `error_kind` is the error-envelope kind of a stop that fails the Turn.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AbortCause {
    pub initiator: AbortInitiator,
    pub label: &'static str,
    pub error_kind: Option<&'static str>,
}

const PERSON: AbortCause = AbortCause {
    initiator: AbortInitiator::Person,
    label: "",
    error_kind: None,
};
const SERVER_RESTART: AbortCause = AbortCause {
    initiator: AbortInitiator::ServerRestart,
    label: "server restart",
    error_kind: None,
};
const STORAGE_FENCE: AbortCause = AbortCause {
    initiator: AbortInitiator::StorageFence,
    label: "storage authority fence ({reason})",
    error_kind: Some("storage_fence"),
};

pub const DISPATCH_GRANT_REVOKED: &str = "dispatch_grant_revoked";
pub const DISPATCH_AUTHORITY_LOST: &str = "dispatch_authority_lost";
pub const RESTART_RECOVERY: &str = "restart_recovery";
pub const STOPPED_ELSEWHERE: &str = "attempt_stopped_by_user";
pub const STUCK_NO_PROGRESS: &str = "stuck_no_progress";
pub const LEAD_HOLD: &str = "lead_hold";
pub const AWAITING_USER_ACTION: &str = "awaiting_user_action";

/// Every abort reason Tofu stamps, and who stopped the work. The verdict,
/// tool-result labels, the post-loop finish reason and the model-request
/// activity row all read this one table.
pub const ABORT_CAUSES: &[(&str, AbortCause)] = &[
    ("user_abort", PERSON),
    (STOPPED_ELSEWHERE, PERSON),
    ("send_abort_race", PERSON),
    ("conversation_deleted", PERSON),
    ("human_stop", PERSON),
    ("superseded_by_human", PERSON),
    ("superseded_by_new_task", PERSON),
    ("real_message_preempts_vu", PERSON),
    ("server_shutdown", SERVER_RESTART),
    (RESTART_RECOVERY, SERVER_RESTART),
    (
        LEAD_HOLD,
        AbortCause {
            initiator: AbortInitiator::LeadHold,
            label: "project lead hold",
            error_kind: None,
        },
    ),
    ("storage_authority_integrity", STORAGE_FENCE),
    ("storage_frame_overflow", STORAGE_FENCE),
    ("turn_attempt_stale_fence", STORAGE_FENCE),
    ("conversation_attempt_stale_fence", STORAGE_FENCE),
    ("worker_lease_lost", STORAGE_FENCE),
    (
        DISPATCH_GRANT_REVOKED,
        AbortCause {
            initiator: AbortInitiator::DispatchAuthority,
            label: "project dispatch authority ({reason})",
            error_kind: Some(DISPATCH_GRANT_REVOKED),
        },
    ),
    (
        DISPATCH_AUTHORITY_LOST,
        AbortCause {
            initiator: AbortInitiator::DispatchAuthority,
            label: "project dispatch authority ({reason})",
            error_kind: Some(DISPATCH_AUTHORITY_LOST),
        },
    ),
    (
        STUCK_NO_PROGRESS,
        AbortCause {
            initiator: AbortInitiator::StuckTaskReaper,
            label: "stuck-task reaper",
            error_kind: None,
        },
    ),
    (
        "hallucination_loop",
        AbortCause {
            initiator: AbortInitiator::TofuGuard,
            label: "unattended tool-loop breaker",
            error_kind: Some("tool_loop"),
        },
    ),
    (
        AWAITING_USER_ACTION,
        AbortCause {
            initiator: AbortInitiator::TofuGuard,
            label: "user-action guard",
            error_kind: Some("tool_call_rejected"),
        },
    ),
    (
        "legacy_autopilot_retired",
        AbortCause {
            initiator: AbortInitiator::TofuGuard,
            label: "retired Legacy Autopilot",
            error_kind: Some("bad_request"),
        },
    ),
];

/// The answer of `abort_cause`. `unregistered` is true for a non-empty reason
/// without a row: it reads as a person's Stop (a rehydrated result can carry
/// an earlier build's reason) and the caller reports it at ERROR once per
/// process through `UnregisteredAbortReasons`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AbortLookup {
    pub cause: AbortCause,
    pub unregistered: bool,
}

/// The registered cause of an abort reason; a person's Stop when none was
/// stamped or the stamp has no row.
pub fn abort_cause(reason: &str) -> AbortLookup {
    match ABORT_CAUSES.iter().find(|(name, _)| *name == reason) {
        Some((_, cause)) => AbortLookup {
            cause: *cause,
            unregistered: false,
        },
        None => AbortLookup {
            cause: PERSON,
            unregistered: !reason.is_empty(),
        },
    }
}

/// The process's memory of unregistered reasons already reported, owned by
/// the host so this module keeps no global state.
#[derive(Debug, Default)]
pub struct UnregisteredAbortReasons {
    reported: HashSet<String>,
}

impl UnregisteredAbortReasons {
    /// True exactly once per reason: the caller logs ERROR
    /// ("unregistered abort reason {reason:?} reads as a person's Stop;
    /// register it in ABORT_CAUSES") when this returns true.
    pub fn first_report(&mut self, lookup: &AbortLookup, reason: &str) -> bool {
        lookup.unregistered && self.reported.insert(reason.to_string())
    }
}

/// Who stopped the work, worded for tool results and logs; empty for a
/// person's Stop (results then say "aborted by user").
pub fn system_abort_label(reason: &str) -> String {
    abort_cause(reason).cause.label.replace("{reason}", reason)
}

/// A restart's quiesce or a lead's hold: a pause, resumable from checkpoint.
pub fn is_pause(reason: &str) -> bool {
    matches!(
        abort_cause(reason).cause.initiator,
        AbortInitiator::ServerRestart | AbortInitiator::LeadHold
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TurnStatus {
    Interrupted,
    Failed,
}

impl TurnStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            TurnStatus::Interrupted => "interrupted",
            TurnStatus::Failed => "failed",
        }
    }
}

/// The Turn verdict of stopped work: (status, cause, termination evidence).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopVerdict {
    pub status: TurnStatus,
    pub cause: String,
    pub evidence: &'static str,
}

/// `derive_turn_verdict` for an aborted Turn: a system stop's initiator
/// decides (`_SYSTEM_STOP_VERDICTS`); a person's Stop reads `user_abort`.
pub fn stop_verdict(reason: &str) -> StopVerdict {
    let (status, cause, evidence) = match abort_cause(reason).cause.initiator {
        AbortInitiator::StorageFence => (
            TurnStatus::Failed,
            "internal_fence_abort",
            "external_authority",
        ),
        AbortInitiator::DispatchAuthority => (TurnStatus::Failed, reason, "external_authority"),
        AbortInitiator::ServerRestart => {
            (TurnStatus::Interrupted, "server_restart", "system_recovery")
        }
        AbortInitiator::LeadHold => (TurnStatus::Interrupted, LEAD_HOLD, "system_recovery"),
        AbortInitiator::StuckTaskReaper => (TurnStatus::Failed, "worker_lost", "generation_error"),
        AbortInitiator::TofuGuard => (TurnStatus::Failed, "generation_error", "generation_error"),
        AbortInitiator::Person => (TurnStatus::Interrupted, "user_abort", "user_abort"),
    };
    StopVerdict {
        status,
        cause: cause.to_string(),
        evidence,
    }
}

/// The error envelope the finalizer stamps for a system stop when the task
/// carries no error yet. `retryable: Some(false)` is the explicit
/// non-retryable flag; `None` leaves the envelope default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StopEnvelope {
    pub kind: String,
    pub detail: String,
    pub raw: String,
    pub retryable: Option<bool>,
}

/// The finish reason an aborted loop settles with, and its envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PostLoopStop {
    pub finish_reason: &'static str,
    pub envelope: Option<StopEnvelope>,
}

/// One explanation of a dispatch-authority stop (`dispatch_authority_stop_detail`).
pub fn dispatch_authority_stop_detail(reason: &str, provenance: &str) -> String {
    let detail = format!(
        "Project dispatch authority stopped this task before its next model or tool call ({reason}); this was not a manual stop."
    );
    if provenance.is_empty() {
        detail
    } else {
        format!("{detail} {provenance}")
    }
}

/// `_settle_post_loop_finish_reason` for an aborted task. `pre_abort_finish`
/// is the loop's finish reason before the abort; `provenance` is the task's
/// `_abortReasonDetail`. A system stop that fails the Turn settles `error`;
/// a person, a restart or a lead hold settles `aborted`.
pub fn post_loop_stop(reason: &str, pre_abort_finish: &str, provenance: &str) -> PostLoopStop {
    let cause = abort_cause(reason).cause;
    let raw = format!("abort_reason={reason} finish={pre_abort_finish}");
    let envelope = |kind: &str, detail: String, retryable: Option<bool>| StopEnvelope {
        kind: kind.to_string(),
        detail,
        raw: raw.clone(),
        retryable,
    };
    let (finish_reason, envelope) = match cause.initiator {
        AbortInitiator::StuckTaskReaper => ("error", None),
        AbortInitiator::StorageFence => (
            "error",
            Some(envelope(
                cause.error_kind.unwrap_or("storage_fence"),
                format!(
                    "The server storage authority rejected this turn's event writes and stopped the in-flight request (internal fence: {reason})."
                ),
                None,
            )),
        ),
        AbortInitiator::DispatchAuthority => (
            "error",
            Some(envelope(
                cause.error_kind.unwrap_or(reason),
                dispatch_authority_stop_detail(reason, provenance),
                Some(false),
            )),
        ),
        AbortInitiator::TofuGuard => (
            "error",
            Some(envelope(
                cause.error_kind.unwrap_or("internal"),
                format!(
                    "The {} stopped this turn; this was not a manual stop.",
                    system_abort_label(reason)
                ),
                Some(false),
            )),
        ),
        AbortInitiator::ServerRestart | AbortInitiator::LeadHold | AbortInitiator::Person => {
            ("aborted", None)
        }
    };
    PostLoopStop {
        finish_reason,
        envelope,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    type Row = (
        (&'static str, &'static str, &'static str),
        &'static str,
        &'static str,
        Option<&'static str>,
    );

    const PERSON_ROW: Row = (
        ("interrupted", "user_abort", "user_abort"),
        "",
        "aborted",
        None,
    );
    const RESTART_ROW: Row = (
        ("interrupted", "server_restart", "system_recovery"),
        "server restart",
        "aborted",
        None,
    );

    fn expected(reason: &str) -> (StopVerdict, String, &'static str, Option<String>) {
        let fence = |r: &str| {
            (
                ("failed", "internal_fence_abort", "external_authority"),
                format!("storage authority fence ({r})"),
                "error",
                Some("storage_fence".to_string()),
            )
        };
        let dispatch = |r: &'static str| {
            (
                ("failed", r, "external_authority"),
                format!("project dispatch authority ({r})"),
                "error",
                Some(r.to_string()),
            )
        };
        let fixed = |row: Row| (row.0, row.1.to_string(), row.2, row.3.map(str::to_string));
        let (verdict, label, finish, kind) = match reason {
            ""
            | "send_abort_race"
            | "superseded_by_new_task"
            | "conversation_deleted"
            | "attempt_stopped_by_user"
            | "human_stop"
            | "superseded_by_human"
            | "real_message_preempts_vu"
            | "user_abort" => fixed(PERSON_ROW),
            "server_shutdown" | "restart_recovery" => fixed(RESTART_ROW),
            "lead_hold" => fixed((
                ("interrupted", "lead_hold", "system_recovery"),
                "project lead hold",
                "aborted",
                None,
            )),
            "worker_lease_lost"
            | "turn_attempt_stale_fence"
            | "conversation_attempt_stale_fence"
            | "storage_frame_overflow"
            | "storage_authority_integrity" => fence(reason),
            "dispatch_grant_revoked" => dispatch("dispatch_grant_revoked"),
            "dispatch_authority_lost" => dispatch("dispatch_authority_lost"),
            "stuck_no_progress" => fixed((
                ("failed", "worker_lost", "generation_error"),
                "stuck-task reaper",
                "error",
                None,
            )),
            "hallucination_loop" => fixed((
                ("failed", "generation_error", "generation_error"),
                "unattended tool-loop breaker",
                "error",
                Some("tool_loop"),
            )),
            "awaiting_user_action" => fixed((
                ("failed", "generation_error", "generation_error"),
                "user-action guard",
                "error",
                Some("tool_call_rejected"),
            )),
            other => panic!("no pinned attribution for {other}"),
        };
        let status = if verdict.0 == "failed" {
            TurnStatus::Failed
        } else {
            TurnStatus::Interrupted
        };
        (
            StopVerdict {
                status,
                cause: verdict.1.to_string(),
                evidence: verdict.2,
            },
            label,
            finish,
            kind,
        )
    }

    /// Row "One `ABORT_CAUSES` table" (docs/RUST_REWRITE_EVIDENCE.md,
    /// "Behaviours that are easy to lose"); pins the `ATTRIBUTION` table of
    /// `tests/turn_lifecycle/test_abort_cause_table.py::
    /// test_each_stamped_abort_reason_is_attributed_the_same_at_every_site`
    /// (verdict, label, post-loop finish reason and envelope kind).
    #[test]
    fn each_stamped_abort_reason_is_attributed_the_same_at_every_site() {
        let mut reasons: Vec<&str> = ABORT_CAUSES
            .iter()
            .map(|(name, _)| *name)
            .filter(|name| *name != "legacy_autopilot_retired")
            .collect();
        reasons.push("");
        for reason in reasons {
            let stop = post_loop_stop(reason, "stop", "");
            let observed = (
                stop_verdict(reason),
                system_abort_label(reason),
                stop.finish_reason,
                stop.envelope.map(|e| e.kind),
            );
            assert_eq!(observed, expected(reason), "reason {reason:?}");
        }
    }

    /// Row "One `ABORT_CAUSES` table"; pins
    /// `test_abort_cause_table.py::test_every_registered_abort_reason_has_its_attribution_pinned`
    /// against the Python table size (22 rows in `lib/turn_verdict/__init__.py`).
    #[test]
    fn the_table_has_one_row_per_reason_with_no_duplicates() {
        let names: HashSet<&str> = ABORT_CAUSES.iter().map(|(n, _)| *n).collect();
        assert_eq!(names.len(), ABORT_CAUSES.len());
        assert_eq!(ABORT_CAUSES.len(), 22);
    }

    /// Row "an unknown reason reads as a person's Stop and logs ERROR once";
    /// pins `test_abort_cause_table.py::
    /// test_an_unregistered_reason_reads_as_a_person_and_is_reported_once`.
    #[test]
    fn an_unregistered_reason_reads_as_a_person_and_is_reported_once() {
        let mut reported = UnregisteredAbortReasons::default();
        let mut errors = 0;
        for _ in 0..3 {
            let lookup = abort_cause("a_stop_without_a_row");
            assert_eq!(lookup.cause, abort_cause("").cause);
            assert!(lookup.unregistered);
            if reported.first_report(&lookup, "a_stop_without_a_row") {
                errors += 1;
            }
        }
        assert_eq!(errors, 1);
        let unstamped = abort_cause("");
        assert!(!unstamped.unregistered);
        assert!(!reported.first_report(&unstamped, ""));
    }

    /// Pins `test_abort_cause_table.py::
    /// test_the_tool_loop_breaker_fails_the_turn_without_inviting_a_retry`.
    #[test]
    fn the_tool_loop_breaker_fails_the_turn_without_inviting_a_retry() {
        let stop = post_loop_stop("hallucination_loop", "stop", "");
        assert_eq!(stop.finish_reason, "error");
        let envelope = stop.envelope.expect("guard envelope");
        assert_eq!(
            (envelope.kind.as_str(), envelope.retryable),
            ("tool_loop", Some(false))
        );
        assert!(envelope.detail.contains("unattended tool-loop breaker"));
    }

    /// A dispatch-authority stop names its provenance and is non-retryable
    /// (`_finalize.py` DISPATCH_AUTHORITY branch).
    #[test]
    fn a_dispatch_authority_stop_is_not_a_manual_stop_and_not_retryable() {
        let stop = post_loop_stop(DISPATCH_GRANT_REVOKED, "stop", "by coordinator c1");
        let envelope = stop.envelope.expect("envelope");
        assert_eq!(envelope.retryable, Some(false));
        assert_eq!(
            envelope.detail,
            "Project dispatch authority stopped this task before its next model or tool call \
             (dispatch_grant_revoked); this was not a manual stop. by coordinator c1"
        );
        assert_eq!(
            envelope.raw,
            "abort_reason=dispatch_grant_revoked finish=stop"
        );
    }

    #[test]
    fn restarts_and_lead_holds_are_pauses_and_a_person_is_not() {
        assert!(is_pause("server_shutdown"));
        assert!(is_pause(RESTART_RECOVERY));
        assert!(is_pause(LEAD_HOLD));
        assert!(!is_pause("user_abort"));
        assert!(!is_pause("turn_attempt_stale_fence"));
    }
}
