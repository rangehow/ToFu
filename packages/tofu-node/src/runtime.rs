//! Demand-driven execution state, outboxes and negotiated replay deadlines.
//!
//! Port of `lib/desktop_agent/_runtime.py`. Every admitted command runs at
//! once on its own thread: there is no worker pool and nothing sized from the
//! CPU or memory. `execution_slots` is the contract's advisory poll window
//! (`command_batch`), never an admission permit. Outbox limits describe one
//! poll (`result_*_floor`, `stream_*_floor`), never admission: pending frames
//! wait in memory until a 200 poll acknowledges their prefix.
//!
//! Replay: command ids stay known while running or unacknowledged; expired
//! finite-deadline ids are reclaimed only after completion and output ACK.

use crate::contract::{limit, protocol_version};
use crate::util::Stop;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Instant;

pub struct Outbox {
    records: VecDeque<(Value, usize, Option<String>)>,
    max_items: usize,
    max_bytes: usize,
    pub bytes: usize,
    pending: HashMap<String, usize>,
}

impl Outbox {
    fn new(max_items: usize, max_bytes: usize) -> Self {
        Self {
            records: VecDeque::new(),
            max_items,
            max_bytes,
            bytes: 0,
            pending: HashMap::new(),
        }
    }

    fn append(&mut self, item: Value) -> Result<(), String> {
        let size = serde_json::to_vec(&item).map_err(|e| e.to_string())?.len();
        if size > self.max_bytes {
            return Err("Desktop transport frame exceeds poll byte budget".into());
        }
        let id = item
            .get("id")
            .or_else(|| item.get("cmd_id"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if let Some(id) = &id {
            *self.pending.entry(id.clone()).or_default() += 1;
        }
        self.records.push_back((item, size, id));
        self.bytes += size;
        Ok(())
    }

    /// The longest prefix within one poll's item and byte budget.
    pub fn prefix(&self) -> Vec<Value> {
        let mut batch = Vec::new();
        let mut size = 0;
        for (item, length, _) in &self.records {
            if batch.len() >= self.max_items || size + length > self.max_bytes {
                break;
            }
            batch.push(item.clone());
            size += length;
        }
        batch
    }

    /// A 200 poll acknowledged the first `count` frames.
    pub fn acknowledge(&mut self, count: usize) {
        for _ in 0..count.min(self.records.len()) {
            if let Some((_, length, id)) = self.records.pop_front() {
                self.bytes -= length;
                if let Some(id) = id {
                    if let Some(n) = self.pending.get_mut(&id) {
                        *n -= 1;
                        if *n == 0 {
                            self.pending.remove(&id);
                        }
                    }
                }
            }
        }
    }

    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }
}

pub struct Inner {
    pub results: Outbox,
    pub streams: Outbox,
    seen: HashSet<String>,
    running: BTreeSet<String>,
    expiry: Vec<(f64, String)>,
    server_anchor: Option<(f64, Instant)>,
    active_cursor: usize,
    closed: bool,
}

impl Inner {
    fn server_now(&self) -> Option<f64> {
        self.server_anchor
            .map(|(t, at)| t + at.elapsed().as_secs_f64())
    }

    fn expired(&self, expires_at: Option<f64>) -> bool {
        match expires_at {
            None => false,
            Some(deadline) => match self.server_now() {
                None => true,
                Some(now) => deadline <= now,
            },
        }
    }

    fn reclaim(&mut self) {
        let Some(now) = self.server_now() else { return };
        let pending: HashSet<&String> = self
            .results
            .pending
            .keys()
            .chain(self.streams.pending.keys())
            .collect();
        let mut keep = Vec::new();
        let mut retire = Vec::new();
        for (deadline, id) in self.expiry.drain(..) {
            if deadline <= now && !self.running.contains(&id) && !pending.contains(&id) {
                retire.push(id);
            } else {
                keep.push((deadline, id));
            }
        }
        self.expiry = keep;
        for id in retire {
            self.seen.remove(&id);
        }
    }
}

#[derive(Clone)]
pub struct RuntimeState {
    inner: Arc<(Mutex<Inner>, Condvar)>,
    pub stop: Stop,
}

impl RuntimeState {
    pub fn new(stop: Stop) -> Self {
        let inner = Inner {
            results: Outbox::new(limit("result_items_floor"), limit("result_bytes_floor")),
            streams: Outbox::new(limit("stream_items_floor"), limit("stream_bytes_floor")),
            seen: HashSet::new(),
            running: BTreeSet::new(),
            expiry: Vec::new(),
            server_anchor: None,
            active_cursor: 0,
            closed: false,
        };
        Self {
            inner: Arc::new((Mutex::new(inner), Condvar::new())),
            stop,
        }
    }

    pub fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn push_result(&self, item: Value) {
        let mut inner = self.lock();
        if inner.closed || self.stop.is_set() {
            return;
        }
        if let Err(error) = inner.results.append(item) {
            crate::log::warn(&format!("result not queued: {error}"));
        }
    }

    pub fn push_stream(&self, item: Value) -> Result<(), String> {
        let mut inner = self.lock();
        if inner.closed || self.stop.is_set() {
            return Err("Desktop transport stopped before delivery".into());
        }
        inner.streams.append(item)
    }

    pub fn observe_server_time(&self, server_time: f64, request_elapsed: f64) {
        let mut inner = self.lock();
        let candidate = server_time + request_elapsed.max(0.0);
        let anchor = match inner.server_now() {
            Some(current) => candidate.max(current),
            None => candidate,
        };
        inner.server_anchor = Some((anchor, Instant::now()));
        inner.reclaim();
    }

    pub fn expired(&self, expires_at: Option<f64>) -> bool {
        self.lock().expired(expires_at)
    }

    pub fn available_slots(&self) -> usize {
        let mut inner = self.lock();
        inner.reclaim();
        if self.stop.is_set() {
            0
        } else {
            limit("command_batch")
        }
    }

    /// Rotating bounded pages of running ids; cardinality never refuses work.
    pub fn active_command_ids(&self) -> Vec<String> {
        let mut inner = self.lock();
        let active: Vec<String> = inner.running.iter().cloned().collect();
        if active.is_empty() {
            inner.active_cursor = 0;
            return Vec::new();
        }
        let size = active.len().min(limit("legacy_execution_ids"));
        let start = inner.active_cursor % active.len();
        let mut selected = Vec::new();
        let mut encoded = 2;
        for index in 0..size {
            let id = &active[(start + index) % active.len()];
            let item = json!(id).to_string().len() + 2;
            if !selected.is_empty() && encoded + item > limit("stream_bytes_floor") {
                break;
            }
            selected.push(id.clone());
            encoded += item;
        }
        inner.active_cursor = (start + selected.len()) % active.len();
        selected
    }

    pub fn admit(&self, cmd_id: &str, expires_at: Option<f64>, deadline_enforced: bool) -> bool {
        let mut inner = self.lock();
        inner.reclaim();
        if self.stop.is_set() || inner.closed || inner.seen.contains(cmd_id) {
            return false;
        }
        if deadline_enforced && inner.expired(expires_at) {
            let _ = inner.results.append(json!({"id": cmd_id, "result": null,
                                                "error": "Command expired before desktop execution"}));
            return false;
        }
        inner.seen.insert(cmd_id.to_string());
        if let Some(deadline) = expires_at {
            inner.expiry.push((deadline, cmd_id.to_string()));
        }
        inner.running.insert(cmd_id.to_string());
        true
    }

    pub fn finish(&self, cmd_id: &str) {
        let mut inner = self.lock();
        inner.running.remove(cmd_id);
        self.inner.1.notify_all();
    }

    pub fn draining(&self) -> bool {
        let inner = self.lock();
        !inner.results.is_empty() || !inner.streams.is_empty()
    }

    /// Stop admitting, wait for owned execution to settle, drop the outboxes.
    pub fn close(&self) {
        self.stop.set();
        let mut inner = self.lock();
        while !inner.running.is_empty() {
            inner = self.inner.1.wait(inner).unwrap_or_else(|e| e.into_inner());
        }
        inner.closed = true;
        inner.results = Outbox::new(0, 0);
        inner.streams = Outbox::new(0, 0);
        inner.seen.clear();
        inner.expiry.clear();
    }
}

#[derive(Debug, PartialEq)]
pub enum PollError {
    /// A reached server requires an incompatible wire version.
    ProtocolMismatch,
    Invalid(&'static str),
}

fn timestamp(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f.is_finite() && f > 0.0),
        _ => false,
    }
}

/// Validate the complete command batch before acknowledging any outbox.
pub fn validate_poll_response(
    data: &Value,
    agent_id: &str,
    require_v3: bool,
) -> Result<Vec<Value>, PollError> {
    let Some(commands) = data.get("commands").and_then(Value::as_array) else {
        return Err(PollError::Invalid("Invalid desktop poll envelope"));
    };
    let version = data.get("protocol_version");
    let negotiated = version.is_some_and(|v| !v.is_null());
    if negotiated {
        let exact = version
            .filter(|v| v.is_i64() || v.is_u64())
            .and_then(Value::as_i64);
        if exact != Some(protocol_version()) {
            return Err(PollError::ProtocolMismatch);
        }
    } else if require_v3 {
        return Err(PollError::ProtocolMismatch);
    }
    if negotiated && !timestamp(data.get("server_time")) {
        return Err(PollError::Invalid("Invalid desktop server clock"));
    }
    if commands.len() > limit("command_batch") {
        return Err(PollError::Invalid(
            "Desktop command batch exceeds admission budget",
        ));
    }
    for cmd in commands {
        let id_ok = cmd
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(|id| !id.is_empty() && id.chars().count() <= 256);
        let type_ok = cmd
            .get("type")
            .and_then(Value::as_str)
            .is_some_and(|t| !t.is_empty());
        let params_ok = cmd.get("params").is_none_or(Value::is_object);
        if !cmd.is_object() || !id_ok || !type_ok || !params_ok {
            return Err(PollError::Invalid("Invalid desktop command envelope"));
        }
        let target = cmd.get("target_agent_id");
        if let Some(target) = target.and_then(Value::as_str).filter(|t| !t.is_empty()) {
            if target != agent_id {
                return Err(PollError::Invalid(
                    "Desktop command addressed to a different device",
                ));
            }
        }
        if negotiated
            && (!timestamp(cmd.get("expires_at"))
                || !target
                    .and_then(Value::as_str)
                    .is_some_and(|t| !t.is_empty()))
        {
            return Err(PollError::Invalid(
                "Invalid desktop command lifetime or device",
            ));
        }
        if cmd
            .get("deadline_enforced")
            .is_some_and(|d| !d.is_boolean())
        {
            return Err(PollError::Invalid("Invalid desktop deadline policy"));
        }
    }
    Ok(commands.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn command(id: &str, target: &str) -> Value {
        json!({"id": id, "type": "desktop_read_file", "params": {}, "target_agent_id": target, "expires_at": 10.0})
    }

    #[test]
    fn poll_responses_are_validated_whole() {
        let ok =
            json!({"protocol_version": 3, "server_time": 1.0, "commands": [command("a", "me")]});
        assert_eq!(validate_poll_response(&ok, "me", true).unwrap().len(), 1);
        let foreign =
            json!({"protocol_version": 3, "server_time": 1.0, "commands": [command("a", "other")]});
        assert!(matches!(
            validate_poll_response(&foreign, "me", false),
            Err(PollError::Invalid(_))
        ));
        let v2 = json!({"commands": []});
        assert_eq!(
            validate_poll_response(&v2, "me", true),
            Err(PollError::ProtocolMismatch)
        );
        assert!(validate_poll_response(&v2, "me", false).is_ok());
        let v4 = json!({"protocol_version": 4, "server_time": 1.0, "commands": []});
        assert_eq!(
            validate_poll_response(&v4, "me", false),
            Err(PollError::ProtocolMismatch)
        );
        let float_version = json!({"protocol_version": 3.0, "server_time": 1.0, "commands": []});
        assert_eq!(
            validate_poll_response(&float_version, "me", false),
            Err(PollError::ProtocolMismatch)
        );
        let no_clock = json!({"protocol_version": 3, "commands": []});
        assert!(validate_poll_response(&no_clock, "me", false).is_err());
    }

    #[test]
    fn replay_ids_retire_only_after_completion_and_ack() {
        let state = RuntimeState::new(Stop::new());
        state.observe_server_time(100.0, 0.0);
        assert!(state.admit("a", Some(200.0), false));
        assert!(
            !state.admit("a", Some(200.0), false),
            "a known id is never admitted twice"
        );
        state.push_result(json!({"id": "a", "result": 1, "error": null}));
        state.finish("a");
        state.observe_server_time(300.0, 0.0);
        assert!(
            !state.admit("a", Some(200.0), false),
            "unacknowledged output pins the id"
        );
        state.lock().results.acknowledge(1);
        state.observe_server_time(301.0, 0.0);
        assert!(state.admit("a", Some(400.0), false));
        // Enforced pickup deadline already past: refused with a result, not run.
        assert!(!state.admit("late", Some(1.0), true));
        assert_eq!(state.lock().results.prefix()[0]["id"], json!("late"));
    }

    #[test]
    fn outbox_prefix_respects_one_poll() {
        let mut outbox = Outbox::new(2, 1_000);
        for i in 0..5 {
            outbox.append(json!({"id": format!("c{i}")})).unwrap();
        }
        assert_eq!(outbox.prefix().len(), 2);
        outbox.acknowledge(2);
        assert_eq!(outbox.prefix()[0]["id"], json!("c2"));
        assert!(outbox.append(json!({"id": "x".repeat(2_000)})).is_err());
    }

    #[test]
    fn slots_are_the_contract_window_not_hardware() {
        let state = RuntimeState::new(Stop::new());
        assert_eq!(state.available_slots(), limit("command_batch"));
        for i in 0..1_000 {
            assert!(state.admit(&format!("c{i}"), None, true));
        }
        assert_eq!(state.available_slots(), limit("command_batch"));
    }
}
