//! Terminal settlement of one Turn attempt.
//!
//! Replaces the settlement half of `lib/turn_lifecycle.py` (terminal event
//! recording, `_terminalTurnPending` retained replay, `automatic_recovery_
//! blocker`, `_drain_queue_after_settlement`), the cancellation branch of
//! `lib/tasks_pkg/orchestrator/_post_loop.py::handle_task_fatal`, the steer
//! salvage of `lib/tasks_pkg/orchestrator/_finalize.py::
//! _salvage_undelivered_steer` with `lib/tasks_pkg/committed_steers.py::
//! requeue_orphaned_steers`, and the once-only acknowledgement of
//! `lib/agent_core/terminal_delivery.py` (docs/contracts/TURN_SETTLEMENT.md,
//! "Invariants to test", "Retrying a failed terminal commit", "Explicit user
//! cancellation", "Known external blockers and unattended recovery").
//!
//! Rules this module enforces:
//! - terminal state is monotonic: the first terminal candidate is retained,
//!   every later one is refused and reported (`TerminalRefused`);
//! - the terminal event and the Turn's terminal projection commit together
//!   through `DurableStore::settle_terminal`; `EventSink` sees the event only
//!   after that call returns `Ok`;
//! - a failed commit keeps the retained event; a retry replays the same
//!   serialized bytes, never a rebuilt event;
//! - a Stop acknowledgement means accepted, not settled; frames offered after
//!   a Stop are refused with a typed cancellation (`LateFrame`), never a
//!   capacity error such as `server_busy`;
//! - a settlement drains the conversation queue into at most one successor,
//!   and only after a successful commit (`QueueDrain`).
//!
//! Entry points, in loop order: `TurnSettlement::new`, `admit_frame` (before
//! each non-terminal event), `accept_stop` (Stop command), `propose` with a
//! `TerminalCandidate` (`done`/`error`/`aborted`, or `stop_done_event` for a
//! stopped loop), `commit` (repeat on `Err` to replay), then `steer_
//! disposition` + `DurableStore::requeue_steers`, `queue_drain`.
//! `automatic_recovery_blocker` classifies a failed terminal's error.

use serde_json::{Map, Value};
use tofu_contracts::generated_events_v1::{AbortedEvent, ContractedEvent, DoneEvent, ErrorEvent};

use super::abort::post_loop_stop;
use super::cancel::CancelToken;
use super::ports::{DurableStore, EventSink, StoreError};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalKind {
    Done,
    Error,
    Aborted,
}

/// One terminal event (wire JSON with its `type` tag) and the Turn
/// projection it settles.
#[derive(Debug, Clone, PartialEq)]
pub struct TerminalCandidate {
    pub kind: TerminalKind,
    pub event: Value,
    pub projection: Value,
}

fn contracted(event: ContractedEvent) -> Value {
    serde_json::to_value(event).expect("generated event types serialize to JSON")
}

impl TerminalCandidate {
    pub fn done(event: DoneEvent, projection: Value) -> Self {
        Self {
            kind: TerminalKind::Done,
            event: contracted(ContractedEvent::Done(event)),
            projection,
        }
    }

    pub fn error(event: ErrorEvent, projection: Value) -> Self {
        Self {
            kind: TerminalKind::Error,
            event: contracted(ContractedEvent::Error(event)),
            projection,
        }
    }

    pub fn aborted(event: AbortedEvent, projection: Value) -> Self {
        Self {
            kind: TerminalKind::Aborted,
            event: contracted(ContractedEvent::Aborted(event)),
            projection,
        }
    }
}

/// The `done` event of a loop that ended because it was stopped
/// (`_settle_post_loop_finish_reason` + `handle_task_fatal` cancellation): a
/// person, a restart or a lead hold settles `finishReason=aborted` with no
/// error; a fence, dispatch authority or guard keeps its failed envelope.
/// The envelope carries the fields `_finalize.py` passes to `make_envelope`;
/// that builder's per-kind message and retryable defaults are not applied.
pub fn stop_done_event(
    abort_reason: &str,
    provenance: &str,
    pre_abort_finish: &str,
    model: &str,
) -> DoneEvent {
    let stop = post_loop_stop(abort_reason, pre_abort_finish, provenance);
    let error = stop.envelope.map(|envelope| {
        let mut map = Map::new();
        map.insert("kind".into(), Value::from(envelope.kind));
        map.insert("detail".into(), Value::from(envelope.detail));
        map.insert("model".into(), Value::from(model));
        map.insert("context".into(), Value::from("post-loop-settle"));
        map.insert("source".into(), Value::from("turn-finalize"));
        map.insert("raw".into(), Value::from(envelope.raw));
        if let Some(retryable) = envelope.retryable {
            map.insert("retryable".into(), Value::from(retryable));
        }
        Value::Object(map)
    });
    let mut fields = Map::new();
    fields.insert("finishReason".into(), Value::from(stop.finish_reason));
    if let Some(error) = error {
        fields.insert("error".into(), error);
    }
    serde_json::from_value(Value::Object(fields))
        .expect("finishReason and error are DoneEvent fields")
}

/// A terminal candidate refused because the attempt already holds one.
/// `committed` says whether the held terminal is durably settled or still
/// retained awaiting its commit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TerminalRefused {
    pub held: TerminalKind,
    pub refused: TerminalKind,
    pub committed: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub enum CommitError {
    /// The store refused or lost the acknowledgement; the event stays
    /// retained and the next `commit` replays it.
    Store(StoreError),
    NoTerminal,
    AlreadySettled(TerminalKind),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopAck {
    /// The Stop was accepted: the token is cancelled. The Turn is not settled
    /// until the loop commits its terminal.
    Accepted,
    /// An earlier Stop was already accepted; its reason stands.
    AlreadyStopping,
    /// The attempt is terminal or terminalizing; a racing Stop cannot
    /// rewrite it.
    Terminal(TerminalKind),
}

/// Why a frame offered for delivery was refused. Both are cancellations
/// (error-envelope kind `aborted`), never a capacity or persistence failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LateFrame {
    /// `EventDeliveryCancelled`: admission closed by an accepted Stop.
    Cancelled { reason: String },
    /// The attempt already holds its terminal; the frame is invisible.
    AfterTerminal { held: TerminalKind },
}

impl LateFrame {
    pub fn error_kind(&self) -> &'static str {
        "aborted"
    }
}

#[derive(Debug, Clone)]
struct Retained {
    kind: TerminalKind,
    event: Value,
    event_json: String,
    projection: Value,
}

#[derive(Debug, Clone)]
enum Phase {
    Running,
    Retained(Retained),
    Settled(TerminalKind),
}

/// The terminal state machine of one Turn attempt.
#[derive(Debug, Clone)]
pub struct TurnSettlement {
    phase: Phase,
    successor_released: bool,
}

impl Default for TurnSettlement {
    fn default() -> Self {
        Self::new()
    }
}

impl TurnSettlement {
    pub fn new() -> Self {
        Self {
            phase: Phase::Running,
            successor_released: false,
        }
    }

    fn held(&self) -> Option<(TerminalKind, bool)> {
        match &self.phase {
            Phase::Running => None,
            Phase::Retained(retained) => Some((retained.kind, false)),
            Phase::Settled(kind) => Some((*kind, true)),
        }
    }

    pub fn settled(&self) -> Option<TerminalKind> {
        match self.phase {
            Phase::Settled(kind) => Some(kind),
            _ => None,
        }
    }

    /// The serialized terminal event awaiting (or replaying) its commit.
    pub fn retained_event_json(&self) -> Option<&str> {
        match &self.phase {
            Phase::Retained(retained) => Some(&retained.event_json),
            _ => None,
        }
    }

    /// Admission of one non-terminal frame.
    pub fn admit_frame(&self, cancel: &CancelToken) -> Result<(), LateFrame> {
        if let Some((held, _)) = self.held() {
            return Err(LateFrame::AfterTerminal { held });
        }
        if cancel.is_cancelled() {
            return Err(LateFrame::Cancelled {
                reason: cancel.reason().unwrap_or_default(),
            });
        }
        Ok(())
    }

    /// Accept a Stop for this attempt.
    pub fn accept_stop(&self, cancel: &CancelToken, reason: &str) -> StopAck {
        if let Some((held, _)) = self.held() {
            return StopAck::Terminal(held);
        }
        if cancel.cancel(reason) {
            StopAck::Accepted
        } else {
            StopAck::AlreadyStopping
        }
    }

    /// Retain the attempt's terminal. Only the first candidate is kept.
    pub fn propose(&mut self, candidate: TerminalCandidate) -> Result<(), TerminalRefused> {
        if let Some((held, committed)) = self.held() {
            return Err(TerminalRefused {
                held,
                refused: candidate.kind,
                committed,
            });
        }
        let event_json = candidate.event.to_string();
        self.phase = Phase::Retained(Retained {
            kind: candidate.kind,
            event: candidate.event,
            event_json,
            projection: candidate.projection,
        });
        Ok(())
    }

    /// Commit the retained terminal with its projection, then publish it.
    /// On `Err(Store)` nothing was published and the same event is retained
    /// for the next call.
    pub fn commit(
        &mut self,
        store: &dyn DurableStore,
        sink: &dyn EventSink,
    ) -> Result<TerminalKind, CommitError> {
        let retained = match &self.phase {
            Phase::Running => return Err(CommitError::NoTerminal),
            Phase::Settled(kind) => return Err(CommitError::AlreadySettled(*kind)),
            Phase::Retained(retained) => retained,
        };
        store
            .settle_terminal(&retained.event, &retained.projection)
            .map_err(CommitError::Store)?;
        let kind = retained.kind;
        sink.publish(&retained.event);
        self.phase = Phase::Settled(kind);
        Ok(kind)
    }

    /// The successor decision of this settlement. Only a committed terminal
    /// releases one, and only the first call returns it.
    pub fn queue_drain(&mut self, inputs: &DrainInputs) -> QueueDrain {
        if self.settled().is_none() || self.successor_released {
            return QueueDrain::NoSuccessor;
        }
        self.successor_released = true;
        queue_drain_decision(inputs)
    }
}

/// What the loop knows when a settled Turn's lane frees
/// (`_drain_queue_after_settlement`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainInputs {
    /// The settled Turn status: `completed`, `interrupted`, `truncated` or
    /// `failed`; any other value starts nothing.
    pub turn_status: String,
    /// This process is stopping (`task_admission_closed`).
    pub admission_closed: bool,
    /// An autopilot/continuation successor already owns the lane.
    pub successor_owns_lane: bool,
    pub queue_has_work: bool,
    /// Recovery in this serving process settled the Attempt interrupted with
    /// `restart_recovery`.
    pub recovered_interruption: bool,
}

/// At most one successor per settlement.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueDrain {
    NoSuccessor,
    /// A failed Turn starts no queued successor: the loop runs coordinator
    /// dispatch recovery and wakes an idle lead for unread project messages.
    RecoverAfterFailure,
    /// Dispatch one successor: the next queued message when `queued_first`,
    /// else (or when none dispatched) the completed background batch; if
    /// neither started, resume the recovered Turn when `resume_recovered_turn`,
    /// then run dispatch recovery and wake an idle lead.
    DispatchOne {
        queued_first: bool,
        resume_recovered_turn: bool,
    },
}

pub fn queue_drain_decision(inputs: &DrainInputs) -> QueueDrain {
    let status = inputs.turn_status.as_str();
    if !matches!(status, "completed" | "interrupted" | "truncated" | "failed")
        || inputs.admission_closed
        || inputs.successor_owns_lane
    {
        return QueueDrain::NoSuccessor;
    }
    if status == "failed" {
        return QueueDrain::RecoverAfterFailure;
    }
    QueueDrain::DispatchOne {
        queued_first: inputs.queue_has_work,
        resume_recovered_turn: status == "interrupted" && inputs.recovered_interruption,
    }
}

/// `STEER_BLOCK_PREFIX` of `lib/turn_initiation.py`.
pub const STEER_BLOCK_PREFIX: &str = "injection:user-steer:";

/// What happens to the steers committed to a settled Turn.
#[derive(Debug, Clone, PartialEq)]
pub enum SteerDisposition {
    /// Nothing pending, or a restart pause: the resumed Attempt takes them.
    Keep,
    /// Queue each as the conversation's next Turn
    /// (`DurableStore::requeue_steers`); a forward the recipient refuses is
    /// bounced to its sender (`steer_bounce`).
    Requeue(Vec<Value>),
}

/// The pending steer records of a Turn that settles without consuming them
/// (`requeue_orphaned_steers`): a steer is pending when its `deliveryState`
/// is `pending`, its `blockId` is a user-steer block and no record of the
/// same block is `delivered` or `queued`. A restart pause keeps them for the
/// resumed Attempt; a failed or stopped settlement requeues them.
pub fn steer_disposition(records: &[Value], restart_paused: bool) -> SteerDisposition {
    if restart_paused {
        return SteerDisposition::Keep;
    }
    let block = |record: &Value| {
        record
            .get("blockId")
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let state = |record: &Value| {
        record
            .get("deliveryState")
            .and_then(Value::as_str)
            .map(str::to_string)
    };
    let taken: Vec<String> = records
        .iter()
        .filter(|record| matches!(state(record).as_deref(), Some("delivered" | "queued")))
        .filter_map(block)
        .collect();
    let pending: Vec<Value> = records
        .iter()
        .filter(|record| state(record).as_deref() == Some("pending"))
        .filter(|record| {
            block(record)
                .is_some_and(|id| id.starts_with(STEER_BLOCK_PREFIX) && !taken.contains(&id))
        })
        .cloned()
        .collect();
    if pending.is_empty() {
        SteerDisposition::Keep
    } else {
        SteerDisposition::Requeue(pending)
    }
}

/// A notice to a forward's sender that its owner message did not arrive.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SteerBounce {
    pub block_id: String,
    pub author: Value,
    pub reason: String,
}

/// Why a requeue did not land, worded as `committed_steers.py` words it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RequeueRefusal {
    /// The recipient did not accept it (the dispatch disposition).
    NotAccepted(String),
    /// Requeueing raised (the error's type or code).
    Failed(String),
}

/// The bounce of one steer whose requeue did not land. Only a forward (a
/// record carrying `steerAuthor`) has a sender to tell; any other steer keeps
/// its pending record.
pub fn steer_bounce(steer: &Value, refusal: &RequeueRefusal) -> Option<SteerBounce> {
    let author = steer
        .get("steerAuthor")
        .filter(|author| author.is_object())?;
    let reason = match refusal {
        RequeueRefusal::NotAccepted(disposition) => {
            format!("the recipient did not accept it as its next turn ({disposition})")
        }
        RequeueRefusal::Failed(error) => format!("requeueing it failed ({error})"),
    };
    Some(SteerBounce {
        block_id: steer
            .get("blockId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        author: author.clone(),
        reason,
    })
}

/// Typed evidence that unattended replay cannot resolve a failed terminal
/// (`lib/turn_lifecycle.py::automatic_recovery_blocker`). `Some` means the
/// Turn stays failed with no automatic retry.
pub fn automatic_recovery_blocker(error: &Value) -> Option<Value> {
    let error_map = error.as_object()?;
    if let Some(rejection) = tool_rejection_descriptor(error) {
        if rejection.get("kind").and_then(Value::as_str) == Some("git_guard") {
            let condition = rejection
                .get("condition")
                .filter(|value| truthy(value))
                .cloned()
                .unwrap_or_else(|| Value::from("git_guard_condition_changed"));
            return Some(serde_json::json!({
                "kind": "git_guard",
                "condition": condition,
                "retryPolicy": {"mode": "after_condition_change", "automatic": false},
            }));
        }
    }
    if let Some(policy) = error_map.get("retryPolicy").and_then(Value::as_object) {
        if policy.get("automatic") == Some(&Value::Bool(false)) {
            return Some(error.clone());
        }
    }
    let text = |field: &str| match error_map.get(field) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) if !truthy(other) => String::new(),
        Some(other) => other.to_string(),
    };
    let protocol_failure = error_map.get("context").and_then(Value::as_str)
        == Some("request-prepare")
        && ["detail", "raw", "code"]
            .iter()
            .any(|field| text(field).starts_with("protocol_conversion:"));
    let exhausted = error_map.get("autoRetryExhausted").is_some_and(truthy);
    let not_retryable = error_map.get("retryable") == Some(&Value::Bool(false))
        && error_map.get("code").and_then(Value::as_str) != Some("task_start_failed");
    if protocol_failure || exhausted || not_retryable {
        let mut blocked = error_map.clone();
        blocked.insert(
            "retryPolicy".into(),
            serde_json::json!({"mode": "manual", "automatic": false}),
        );
        return Some(Value::Object(blocked));
    }
    None
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `lib/tool_results/tool_rejection.py::tool_rejection_descriptor`.
fn tool_rejection_descriptor(source: &Value) -> Option<&Map<String, Value>> {
    let map = source.as_object()?;
    for field in ["rejection", "_rejected", "rejected"] {
        if let Some(descriptor) = map.get(field).and_then(Value::as_object) {
            if descriptor.get("kind").is_some_and(truthy) {
                return Some(descriptor);
            }
        }
    }
    if let Some(descriptor) = map.get("result").and_then(tool_rejection_descriptor) {
        return Some(descriptor);
    }
    map.get("results")
        .and_then(Value::as_array)?
        .iter()
        .find_map(tool_rejection_descriptor)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    /// Records every store call as serialized bytes; `fail_next` makes the
    /// next `settle_terminal` fail, after committing when `commit_first`.
    #[derive(Default)]
    struct FakeStore {
        settle_calls: Mutex<Vec<(String, String)>>,
        committed: Mutex<Vec<String>>,
        fail_next: Mutex<Option<bool>>,
        requeued: Mutex<Vec<Value>>,
    }

    impl FakeStore {
        fn failing(commit_first: bool) -> Self {
            let store = Self::default();
            *store.fail_next.lock().unwrap() = Some(commit_first);
            store
        }
    }

    fn timeout() -> StoreError {
        StoreError {
            code: "database_timeout".into(),
            message: "injected outage".into(),
            transient: true,
            not_dispatched: false,
        }
    }

    impl DurableStore for FakeStore {
        fn commit_tool_intent(&self, _: &Value) -> Result<(), StoreError> {
            Ok(())
        }
        fn commit_tool_settlement(&self, _: &Value) -> Result<(), StoreError> {
            Ok(())
        }
        fn save_checkpoint(&self, _: &Value) -> Result<(), StoreError> {
            Ok(())
        }
        fn record_events(&self, _: &[Value]) -> Result<(), StoreError> {
            Ok(())
        }
        fn settle_terminal(&self, terminal: &Value, projection: &Value) -> Result<(), StoreError> {
            let bytes = terminal.to_string();
            self.settle_calls
                .lock()
                .unwrap()
                .push((bytes.clone(), projection.to_string()));
            if let Some(commit_first) = self.fail_next.lock().unwrap().take() {
                if commit_first {
                    self.committed.lock().unwrap().push(bytes);
                }
                return Err(timeout());
            }
            let mut committed = self.committed.lock().unwrap();
            if !committed.contains(&bytes) {
                committed.push(bytes);
            }
            Ok(())
        }
        fn take_steers(&self) -> Result<Vec<Value>, StoreError> {
            Ok(Vec::new())
        }
        fn confirm_steers(&self, _: &[Value]) -> Result<(), StoreError> {
            Ok(())
        }
        fn requeue_steers(&self, steers: &[Value]) -> Result<(), StoreError> {
            self.requeued.lock().unwrap().extend_from_slice(steers);
            Ok(())
        }
    }

    /// Publishes only what the store already committed.
    struct CheckingSink<'a> {
        store: &'a FakeStore,
        published: Mutex<Vec<String>>,
    }

    impl<'a> CheckingSink<'a> {
        fn new(store: &'a FakeStore) -> Self {
            Self {
                store,
                published: Mutex::new(Vec::new()),
            }
        }
    }

    impl EventSink for CheckingSink<'_> {
        fn publish(&self, event: &Value) {
            let bytes = event.to_string();
            assert!(
                self.store.committed.lock().unwrap().contains(&bytes),
                "published before commit"
            );
            self.published.lock().unwrap().push(bytes);
        }
    }

    fn done(finish: &str) -> TerminalCandidate {
        let event: DoneEvent = serde_json::from_value(json!({"finishReason": finish})).unwrap();
        TerminalCandidate::done(event, json!({"status": "completed", "content": "answer"}))
    }

    fn error_candidate() -> TerminalCandidate {
        let event: ErrorEvent = serde_json::from_value(json!({"content": "boom"})).unwrap();
        TerminalCandidate::error(event, json!({"status": "failed"}))
    }

    fn aborted_candidate() -> TerminalCandidate {
        let event: AbortedEvent = serde_json::from_value(json!({"status": "aborted"})).unwrap();
        TerminalCandidate::aborted(event, json!({"status": "interrupted"}))
    }

    #[test]
    fn terminal_candidates_serialize_as_contracted_events_with_their_type() {
        assert_eq!(
            done("stop").event,
            json!({"type": "done", "finishReason": "stop"})
        );
        assert_eq!(
            error_candidate().event,
            json!({"type": "error", "content": "boom"})
        );
        assert_eq!(
            aborted_candidate().event,
            json!({"type": "aborted", "status": "aborted"})
        );
    }

    /// Row "Terminal state is monotonic" (docs/RUST_REWRITE_EVIDENCE.md,
    /// "Behaviours that are easy to lose"; TURN_SETTLEMENT.md "Invariants to
    /// test"); Python pins: `tests/turn_lifecycle/test_turn_lifecycle.py`,
    /// `test_internal_delivery_error_attribution.py::
    /// test_late_delivery_cancellation_preserves_stamped_terminal`.
    #[test]
    fn terminal_state_is_monotonic_and_later_candidates_are_refused_and_reported() {
        let store = FakeStore::default();
        let sink = CheckingSink::new(&store);
        let mut settlement = TurnSettlement::new();
        settlement.propose(done("stop")).unwrap();
        assert_eq!(
            settlement.propose(error_candidate()),
            Err(TerminalRefused {
                held: TerminalKind::Done,
                refused: TerminalKind::Error,
                committed: false,
            })
        );
        assert_eq!(settlement.commit(&store, &sink), Ok(TerminalKind::Done));
        assert_eq!(
            settlement.propose(aborted_candidate()),
            Err(TerminalRefused {
                held: TerminalKind::Done,
                refused: TerminalKind::Aborted,
                committed: true,
            })
        );
        assert_eq!(
            settlement.commit(&store, &sink),
            Err(CommitError::AlreadySettled(TerminalKind::Done))
        );
        assert_eq!(store.settle_calls.lock().unwrap().len(), 1);
        assert_eq!(sink.published.lock().unwrap().len(), 1);
    }

    /// Row "settlement and terminal projection commit together" (TURN_SETTLEMENT.md
    /// "Invariants to test"); Python pin: `test_turn_lifecycle.py::
    /// test_terminal_projection_and_attempt_cas_share_one_turn`.
    #[test]
    fn settlement_and_terminal_projection_commit_in_one_store_call() {
        let store = FakeStore::default();
        let sink = CheckingSink::new(&store);
        let mut settlement = TurnSettlement::new();
        settlement.propose(done("stop")).unwrap();
        settlement.commit(&store, &sink).unwrap();
        let calls = store.settle_calls.lock().unwrap();
        assert_eq!(
            calls.as_slice(),
            [(
                r#"{"type":"done","finishReason":"stop"}"#.to_string(),
                r#"{"status":"completed","content":"answer"}"#.to_string()
            )]
        );
    }

    /// Row "A failed terminal commit replays the retained terminal event;
    /// nothing reaches the browser before commit" (TURN_SETTLEMENT.md
    /// "Retrying a failed terminal commit"); Python pin:
    /// `test_turn_lifecycle.py::test_terminal_storage_outage_retries_turn_before_diagnostics`
    /// (rejection before commit and lost acknowledgement after commit).
    #[test]
    fn a_failed_terminal_commit_replays_the_same_retained_bytes_and_publishes_only_after_commit() {
        for commit_before_error in [false, true] {
            let store = FakeStore::failing(commit_before_error);
            let sink = CheckingSink::new(&store);
            let mut settlement = TurnSettlement::new();
            settlement.propose(done("stop")).unwrap();
            let retained = settlement.retained_event_json().unwrap().to_string();
            assert_eq!(
                settlement.commit(&store, &sink),
                Err(CommitError::Store(timeout()))
            );
            assert!(sink.published.lock().unwrap().is_empty());
            assert_eq!(settlement.settled(), None);
            assert_eq!(settlement.retained_event_json(), Some(retained.as_str()));
            assert!(settlement.propose(done("length")).is_err());
            assert_eq!(settlement.commit(&store, &sink), Ok(TerminalKind::Done));
            let calls = store.settle_calls.lock().unwrap();
            assert_eq!(calls.len(), 2);
            assert_eq!(calls[0], calls[1]);
            assert_eq!(calls[0].0, retained);
            assert_eq!(*sink.published.lock().unwrap(), vec![retained.clone()]);
            assert_eq!(store.committed.lock().unwrap().len(), 1);
        }
    }

    /// Row "A Stop ack means accepted, not settled; late frames raise typed
    /// cancellation, never `server_busy`" (TURN_SETTLEMENT.md "Explicit user
    /// cancellation"); Python pins: `test_internal_delivery_error_attribution.py::
    /// test_later_admission_preserves_cancellation_identity` and
    /// `test_cancelled_delivery_settles_interrupted_without_capacity_error`.
    #[test]
    fn a_stop_ack_is_accepted_not_settled_and_late_frames_raise_typed_cancellation() {
        let token = CancelToken::new();
        let mut settlement = TurnSettlement::new();
        assert_eq!(settlement.admit_frame(&token), Ok(()));
        assert_eq!(
            settlement.accept_stop(&token, "user_abort"),
            StopAck::Accepted
        );
        assert_eq!(settlement.settled(), None);
        assert_eq!(
            settlement.accept_stop(&token, "server_shutdown"),
            StopAck::AlreadyStopping
        );
        let late = settlement.admit_frame(&token).unwrap_err();
        assert_eq!(
            late,
            LateFrame::Cancelled {
                reason: "user_abort".into()
            }
        );
        assert_eq!(late.error_kind(), "aborted");

        let event = stop_done_event(&token.reason().unwrap(), "", "stop", "m");
        let store = FakeStore::default();
        let sink = CheckingSink::new(&store);
        settlement
            .propose(TerminalCandidate::done(
                event,
                json!({"status": "interrupted"}),
            ))
            .unwrap();
        settlement.commit(&store, &sink).unwrap();
        assert_eq!(
            *sink.published.lock().unwrap(),
            vec![r#"{"type":"done","finishReason":"aborted"}"#.to_string()]
        );
        assert_eq!(
            settlement.admit_frame(&token),
            Err(LateFrame::AfterTerminal {
                held: TerminalKind::Done
            })
        );
    }

    /// A terminalizing attempt cannot be rewritten by a racing Stop
    /// (TURN_SETTLEMENT.md "Explicit user cancellation").
    #[test]
    fn a_stop_racing_a_retained_terminal_does_not_cancel_or_rewrite_it() {
        let token = CancelToken::new();
        let mut settlement = TurnSettlement::new();
        settlement.propose(done("stop")).unwrap();
        assert_eq!(
            settlement.accept_stop(&token, "user_abort"),
            StopAck::Terminal(TerminalKind::Done)
        );
        assert!(!token.is_cancelled());
    }

    /// Cancellation keeps guarded failures failed; Python pin:
    /// `test_internal_delivery_error_attribution.py::
    /// test_delivery_cancellation_does_not_hide_internal_stop`.
    #[test]
    fn a_stop_by_an_internal_fence_settles_failed_with_its_envelope() {
        let event = stop_done_event("storage_authority_integrity", "", "stop", "m");
        let value = contracted(ContractedEvent::Done(event));
        assert_eq!(value["finishReason"], "error");
        assert_eq!(value["error"]["kind"], "storage_fence");
        let restart = contracted(ContractedEvent::Done(stop_done_event(
            "server_shutdown",
            "",
            "stop",
            "m",
        )));
        assert_eq!(restart, json!({"type": "done", "finishReason": "aborted"}));
    }

    fn drain(status: &str) -> DrainInputs {
        DrainInputs {
            turn_status: status.into(),
            admission_closed: false,
            successor_owns_lane: false,
            queue_has_work: true,
            recovered_interruption: false,
        }
    }

    /// Row "a terminal queue drain dispatches at most one successor"
    /// (TURN_SETTLEMENT.md "Invariants to test"; `lib/turn_lifecycle.py::
    /// _drain_queue_after_settlement`). A failed terminal commit cannot
    /// authorize a successor.
    #[test]
    fn a_terminal_queue_drain_dispatches_at_most_one_successor_and_only_after_commit() {
        let store = FakeStore::failing(false);
        let sink = CheckingSink::new(&store);
        let mut settlement = TurnSettlement::new();
        settlement.propose(done("stop")).unwrap();
        assert!(settlement.commit(&store, &sink).is_err());
        assert_eq!(
            settlement.queue_drain(&drain("completed")),
            QueueDrain::NoSuccessor
        );
        settlement.commit(&store, &sink).unwrap();
        assert_eq!(
            settlement.queue_drain(&drain("completed")),
            QueueDrain::DispatchOne {
                queued_first: true,
                resume_recovered_turn: false
            }
        );
        assert_eq!(
            settlement.queue_drain(&drain("completed")),
            QueueDrain::NoSuccessor
        );
    }

    #[test]
    fn a_failed_stopping_or_owned_lane_starts_no_queued_successor() {
        assert_eq!(
            queue_drain_decision(&drain("failed")),
            QueueDrain::RecoverAfterFailure
        );
        assert_eq!(
            queue_drain_decision(&drain("running")),
            QueueDrain::NoSuccessor
        );
        let stopping = DrainInputs {
            admission_closed: true,
            ..drain("completed")
        };
        assert_eq!(queue_drain_decision(&stopping), QueueDrain::NoSuccessor);
        let owned = DrainInputs {
            successor_owns_lane: true,
            ..drain("completed")
        };
        assert_eq!(queue_drain_decision(&owned), QueueDrain::NoSuccessor);
        let recovered = DrainInputs {
            queue_has_work: false,
            recovered_interruption: true,
            ..drain("interrupted")
        };
        assert_eq!(
            queue_drain_decision(&recovered),
            QueueDrain::DispatchOne {
                queued_first: false,
                resume_recovered_turn: true
            }
        );
    }

    /// Row "`git_guard` refusals stay failed with no automatic retry"
    /// (TURN_SETTLEMENT.md "Known external blockers and unattended recovery";
    /// `lib/turn_lifecycle.py::automatic_recovery_blocker`).
    #[test]
    fn git_guard_refusals_stay_failed_with_no_automatic_retry() {
        let error = json!({
            "kind": "tool_call_rejected", "retryable": true,
            "results": [{"rejection": {"kind": "git_guard", "condition": "dirty_tree"}}],
        });
        assert_eq!(
            automatic_recovery_blocker(&error),
            Some(json!({
                "kind": "git_guard", "condition": "dirty_tree",
                "retryPolicy": {"mode": "after_condition_change", "automatic": false},
            }))
        );
        let unnamed = json!({"rejection": {"kind": "git_guard"}});
        assert_eq!(
            automatic_recovery_blocker(&unnamed).unwrap()["condition"],
            "git_guard_condition_changed"
        );
    }

    #[test]
    fn other_blockers_are_manual_and_ordinary_failures_may_retry() {
        let not_retryable = json!({"kind": "tool_loop", "retryable": false});
        assert_eq!(
            automatic_recovery_blocker(&not_retryable).unwrap()["retryPolicy"],
            json!({"mode": "manual", "automatic": false})
        );
        let start = json!({"kind": "internal", "retryable": false, "code": "task_start_failed"});
        assert_eq!(automatic_recovery_blocker(&start), None);
        let protocol = json!({"context": "request-prepare", "code": "protocol_conversion:tool"});
        assert!(automatic_recovery_blocker(&protocol).is_some());
        assert!(automatic_recovery_blocker(&json!({"autoRetryExhausted": true})).is_some());
        let explicit = json!({"kind": "x", "retryPolicy": {"automatic": false, "mode": "manual"}});
        assert_eq!(
            automatic_recovery_blocker(&explicit),
            Some(explicit.clone())
        );
        assert_eq!(
            automatic_recovery_blocker(&json!({"kind": "upstream_error", "retryable": true})),
            None
        );
    }

    fn steer(block: &str, state: &str) -> Value {
        json!({"blockId": format!("{STEER_BLOCK_PREFIX}{block}"), "deliveryState": state})
    }

    /// When recovery settles a Turn failed or stopped, its unconsumed
    /// committed steers are requeued (commit 066b87248e; Python pin:
    /// `tests/conversation/test_worker_exit_recovery.py::
    /// test_a_turn_recovery_fails_requeues_the_forward_its_executor_never_took`).
    #[test]
    fn a_failed_recovery_requeues_the_pending_steers_its_executor_never_took() {
        let records = vec![
            steer("a", "pending"),
            steer("b", "delivered"),
            steer("c", "pending"),
            steer("c", "queued"),
            json!({"blockId": "injection:other:x", "deliveryState": "pending"}),
        ];
        let disposition = steer_disposition(&records, false);
        assert_eq!(
            disposition,
            SteerDisposition::Requeue(vec![steer("a", "pending")])
        );
        let store = FakeStore::default();
        if let SteerDisposition::Requeue(steers) = &disposition {
            store.requeue_steers(steers).unwrap();
        }
        assert_eq!(*store.requeued.lock().unwrap(), vec![steer("a", "pending")]);
        assert_eq!(steer_disposition(&records, true), SteerDisposition::Keep);
        assert_eq!(
            steer_disposition(&[steer("b", "delivered")], false),
            SteerDisposition::Keep
        );
    }

    /// A forward the recipient refuses is bounced to its sender; a steer
    /// without an author is not (Python pin: `test_worker_exit_recovery.py::
    /// test_a_forward_the_lead_cannot_take_is_bounced_to_the_global_router`).
    #[test]
    fn a_forward_the_recipient_cannot_take_is_bounced_to_its_sender() {
        let mut forward = steer("f", "pending");
        forward["steerAuthor"] = json!({"actor": "router", "text": "decide"});
        assert_eq!(
            steer_bounce(&forward, &RequeueRefusal::NotAccepted("lane_busy".into())),
            Some(SteerBounce {
                block_id: format!("{STEER_BLOCK_PREFIX}f"),
                author: json!({"actor": "router", "text": "decide"}),
                reason: "the recipient did not accept it as its next turn (lane_busy)".into(),
            })
        );
        assert_eq!(
            steer_bounce(&forward, &RequeueRefusal::Failed("StorageError".into()))
                .unwrap()
                .reason,
            "requeueing it failed (StorageError)"
        );
        assert_eq!(
            steer_bounce(&steer("h", "pending"), &RequeueRefusal::Failed("E".into())),
            None
        );
    }
}
