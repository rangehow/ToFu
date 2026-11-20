//! In-memory `DurableStore` with deterministic fault injection.
//!
//! Responsibility: the durable side of the parity boundary (`replay`) and of
//! the engine's own tests. It records every commit in order and renders the
//! canonical durable state the parity harness compares. Faults are scripted
//! per operation: the next `n` calls of an operation fail with a storage
//! error before anything is recorded (a failed commit leaves no trace, as a
//! refused storage transaction does).

use std::collections::BTreeMap;
use std::sync::Mutex;

use serde_json::{json, Value};

use super::ports::{DurableStore, StoreError};

pub const OP_TOOL_INTENT: &str = "tool_intent";
pub const OP_TOOL_SETTLEMENT: &str = "tool_settlement";
pub const OP_CHECKPOINT: &str = "checkpoint";
pub const OP_EVENTS: &str = "events";
pub const OP_TERMINAL: &str = "terminal";
pub const OP_STEERS: &str = "steers";

#[derive(Default)]
struct State {
    intents: Vec<Value>,
    settlements: Vec<Value>,
    checkpoints: Vec<Value>,
    events: Vec<Value>,
    terminal: Option<(Value, Value)>,
    pending_steers: Vec<Value>,
    requeued_steers: Vec<Value>,
    confirmed_steers: Vec<Value>,
    failures: BTreeMap<String, (u32, StoreError)>,
    failed_calls: BTreeMap<String, u32>,
}

#[derive(Default)]
pub struct MemoryStore {
    state: Mutex<State>,
}

impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Fail the next `times` calls of `op` with `error`.
    pub fn fail_next(&self, op: &str, times: u32, error: StoreError) {
        self.lock().failures.insert(op.to_string(), (times, error));
    }

    pub fn push_steer(&self, steer: Value) {
        self.lock().pending_steers.push(steer);
    }

    pub fn terminal(&self) -> Option<(Value, Value)> {
        self.lock().terminal.clone()
    }

    pub fn events(&self) -> Vec<Value> {
        self.lock().events.clone()
    }

    pub fn checkpoints(&self) -> Vec<Value> {
        self.lock().checkpoints.clone()
    }

    pub fn intents(&self) -> Vec<Value> {
        self.lock().intents.clone()
    }

    pub fn settlements(&self) -> Vec<Value> {
        self.lock().settlements.clone()
    }

    /// Canonical durable state: everything committed, in commit order.
    pub fn snapshot(&self) -> Value {
        let state = self.lock();
        json!({
            "toolIntents": state.intents,
            "toolSettlements": state.settlements,
            "checkpoints": state.checkpoints,
            "terminal": state.terminal.as_ref().map(|(event, projection)| json!({
                "event": event, "projection": projection,
            })),
            "requeuedSteers": state.requeued_steers,
            "confirmedSteers": state.confirmed_steers,
            "failedCommits": state.failed_calls,
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn admit(&self, op: &str) -> Result<std::sync::MutexGuard<'_, State>, StoreError> {
        let mut state = self.lock();
        if let Some((remaining, error)) = state.failures.get_mut(op) {
            if *remaining > 0 {
                *remaining -= 1;
                let error = error.clone();
                *state.failed_calls.entry(op.to_string()).or_default() += 1;
                return Err(error);
            }
        }
        Ok(state)
    }
}

impl DurableStore for MemoryStore {
    /// The intent is a `tool_effect` event of the Turn's log as well.
    fn commit_tool_intent(&self, intent: &Value) -> Result<(), StoreError> {
        let mut state = self.admit(OP_TOOL_INTENT)?;
        state.intents.push(intent.clone());
        state.events.push(intent.clone());
        Ok(())
    }

    fn commit_tool_settlement(&self, settlement: &Value) -> Result<(), StoreError> {
        self.admit(OP_TOOL_SETTLEMENT)?
            .settlements
            .push(settlement.clone());
        Ok(())
    }

    fn save_checkpoint(&self, checkpoint: &Value) -> Result<(), StoreError> {
        self.admit(OP_CHECKPOINT)?
            .checkpoints
            .push(checkpoint.clone());
        Ok(())
    }

    fn record_events(&self, events: &[Value]) -> Result<(), StoreError> {
        self.admit(OP_EVENTS)?.events.extend_from_slice(events);
        Ok(())
    }

    fn settle_terminal(&self, terminal: &Value, projection: &Value) -> Result<(), StoreError> {
        let mut state = self.admit(OP_TERMINAL)?;
        state.events.push(terminal.clone());
        state.terminal = Some((terminal.clone(), projection.clone()));
        Ok(())
    }

    fn take_steers(&self) -> Result<Vec<Value>, StoreError> {
        Ok(self.admit(OP_STEERS)?.pending_steers.clone())
    }

    fn confirm_steers(&self, steers: &[Value]) -> Result<(), StoreError> {
        let mut state = self.admit(OP_STEERS)?;
        for steer in steers {
            state
                .pending_steers
                .retain(|pending| pending["blockId"] != steer["blockId"]);
            if !state
                .confirmed_steers
                .iter()
                .any(|saved| saved["blockId"] == steer["blockId"])
            {
                state.confirmed_steers.push(steer.clone());
            }
        }
        Ok(())
    }

    fn requeue_steers(&self, steers: &[Value]) -> Result<(), StoreError> {
        let mut state = self.admit(OP_STEERS)?;
        for steer in steers {
            state
                .pending_steers
                .retain(|pending| pending["blockId"] != steer["blockId"]);
            if !state
                .requeued_steers
                .iter()
                .any(|saved| saved["blockId"] == steer["blockId"])
            {
                state.requeued_steers.push(steer.clone());
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unavailable() -> StoreError {
        StoreError {
            code: "storage_unavailable".into(),
            message: "injected".into(),
            transient: true,
            not_dispatched: true,
        }
    }

    #[test]
    fn a_failed_commit_leaves_no_trace_and_the_next_one_lands() {
        let store = MemoryStore::new();
        store.fail_next(OP_TOOL_INTENT, 1, unavailable());
        assert!(store.commit_tool_intent(&json!({"id": "c1"})).is_err());
        store.commit_tool_intent(&json!({"id": "c1"})).unwrap();
        let snapshot = store.snapshot();
        assert_eq!(snapshot["toolIntents"], json!([{"id": "c1"}]));
        assert_eq!(snapshot["failedCommits"][OP_TOOL_INTENT], 1);
    }
}
