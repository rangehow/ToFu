//! Commit-owned worker/conversation watch channels and the worker lease
//! clock (`commit_watch.py`, `worker_control.py`, `worker_lease_clock.py`).

use std::collections::{BTreeSet, HashMap};
use std::sync::{Condvar, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use crate::error::StorageError;

pub const WORKER_FORMAT: &str = "tofu.worker-control/v1";
pub const WORKER_WATCH_OPERATION: &str = "worker_control.watch";
pub const CONVERSATION_FORMAT: &str = "tofu.conversation-control/v1";
pub const CONVERSATION_WATCH_OPERATION: &str = "conversation_control.watch";

/// A waiter-observed hint: a cancelled task id or an (owner, conversation).
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Hint {
    Task(String),
    Conversation(i64, String),
}

struct WatchState {
    epoch: String,
    generations: Vec<(String, u64)>,
    waiters: HashMap<u64, BTreeSet<Hint>>,
    next_waiter: u64,
    closed: bool,
}

pub struct CommitWatch {
    state: Mutex<WatchState>,
    condition: Condvar,
    format: &'static str,
    hint_key: &'static str,
    hint_generation: &'static str,
}

fn encode_hints(hints: &BTreeSet<Hint>) -> Value {
    Value::Array(
        hints
            .iter()
            .map(|hint| match hint {
                Hint::Task(task) => Value::String(task.clone()),
                Hint::Conversation(owner, conversation) => {
                    let mut entry = Map::new();
                    entry.insert("userId".into(), Value::from(*owner));
                    entry.insert("conversationId".into(), Value::String(conversation.clone()));
                    Value::Object(entry)
                }
            })
            .collect(),
    )
}

impl CommitWatch {
    fn new(generations: &[&str], format: &'static str, hint_key: &'static str, hint_generation: &'static str) -> Self {
        Self {
            state: Mutex::new(WatchState {
                epoch: uuid::Uuid::new_v4().to_string(),
                generations: generations.iter().map(|name| ((*name).to_owned(), 0)).collect(),
                waiters: HashMap::new(),
                next_waiter: 0,
                closed: false,
            }),
            condition: Condvar::new(),
            format,
            hint_key,
            hint_generation,
        }
    }

    fn cursor(state: &WatchState) -> Map<String, Value> {
        let mut cursor = Map::new();
        cursor.insert("epoch".into(), Value::String(state.epoch.clone()));
        for (name, generation) in &state.generations {
            cursor.insert(name.clone(), Value::from(*generation));
        }
        cursor
    }

    pub fn publish_changes(&self, changed: &[&str], hints: &BTreeSet<Hint>) {
        if changed.is_empty() {
            return;
        }
        let mut state = self.state.lock().expect("commit watch");
        if state.closed {
            return;
        }
        if state.generations.iter().map(|(_, generation)| *generation).max() == Some(u64::MAX) {
            state.epoch = uuid::Uuid::new_v4().to_string();
            for entry in &mut state.generations {
                entry.1 = 0;
            }
        }
        for (name, generation) in &mut state.generations {
            if changed.contains(&name.as_str()) {
                *generation += 1;
            }
        }
        for observed in state.waiters.values_mut() {
            observed.extend(hints.iter().cloned());
        }
        self.condition.notify_all();
    }

    pub fn close(&self) {
        self.state.lock().expect("commit watch").closed = true;
        self.condition.notify_all();
    }

    fn valid_cursor(&self, state: &WatchState, cursor: &Value) -> bool {
        let Value::Object(cursor) = cursor else { return false };
        let mut expected: BTreeSet<&str> = state.generations.iter().map(|(name, _)| name.as_str()).collect();
        expected.insert("epoch");
        let actual: BTreeSet<&str> = cursor.keys().map(String::as_str).collect();
        if actual != expected || !cursor["epoch"].is_string() {
            return false;
        }
        state.generations.iter().all(|(name, _)| matches!(&cursor[name], Value::Number(n) if n.as_u64().is_some()))
    }

    pub fn watch(&self, payload: &Value, deadline: Instant) -> Result<Value, StorageError> {
        let invalid_request = || StorageError::protocol("Invalid commit watch request");
        let Value::Object(payload) = payload else { return Err(invalid_request()) };
        if payload.len() != 1 || !payload.contains_key("cursor") {
            return Err(invalid_request());
        }
        let cursor = &payload["cursor"];
        let mut state = self.state.lock().expect("commit watch");
        if !cursor.is_null() && !self.valid_cursor(&state, cursor) {
            return Err(StorageError::protocol("Invalid commit watch cursor"));
        }
        let current = Value::Object(Self::cursor(&state));
        let mut observed: Option<Value> = None;
        if *cursor == current && !state.closed {
            let waiter = state.next_waiter;
            state.next_waiter += 1;
            state.waiters.insert(waiter, BTreeSet::new());
            loop {
                if state.closed || Value::Object(Self::cursor(&state)) != *cursor {
                    break;
                }
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                state = self.condition.wait_timeout(state, deadline - now).expect("commit watch").0;
            }
            let hints = state.waiters.remove(&waiter).unwrap_or_default();
            if cursor["epoch"].as_str() == Some(state.epoch.as_str()) {
                observed = Some(encode_hints(&hints));
            }
        } else if !cursor.is_null()
            && cursor["epoch"] == current["epoch"]
            && cursor[self.hint_generation] == current[self.hint_generation]
        {
            observed = Some(Value::Array(Vec::new()));
        }
        if state.closed {
            return Err(StorageError::new("database_unavailable", "Commit control closed"));
        }
        let mut result = Map::new();
        result.insert("format".into(), Value::String(self.format.into()));
        result.insert("cursor".into(), Value::Object(Self::cursor(&state)));
        result.insert(self.hint_key.into(), observed.unwrap_or(Value::Null));
        Ok(Value::Object(result))
    }
}

/// The worker channel plus its conversation sibling (`WorkerControl`).
pub struct WorkerControl {
    pub workers: CommitWatch,
    pub conversations: CommitWatch,
}

impl Default for WorkerControl {
    fn default() -> Self {
        Self {
            workers: CommitWatch::new(&["queue", "cancel"], WORKER_FORMAT, "cancelTasks", "cancel"),
            conversations: CommitWatch::new(&["change"], CONVERSATION_FORMAT, "conversations", "change"),
        }
    }
}

impl WorkerControl {
    pub fn publish(&self, queue_changed: bool, cancelled: &BTreeSet<String>, conversations: &BTreeSet<(i64, String)>) {
        let mut changed = Vec::new();
        if queue_changed {
            changed.push("queue");
        }
        if !cancelled.is_empty() {
            changed.push("cancel");
        }
        let task_hints = cancelled.iter().cloned().map(Hint::Task).collect();
        self.workers.publish_changes(&changed, &task_hints);
        let conversation_hints: BTreeSet<Hint> =
            conversations.iter().map(|(owner, id)| Hint::Conversation(*owner, id.clone())).collect();
        let changed: &[&str] = if conversations.is_empty() { &[] } else { &["change"] };
        self.conversations.publish_changes(changed, &conversation_hints);
    }

    pub fn close(&self) {
        self.workers.close();
        self.conversations.close();
    }
}

/// `WorkerLeaseClock`: advancing lease time owned by one backend lifecycle.
pub struct WorkerLeaseClock {
    origin: Instant,
    offset_ms: Mutex<i64>,
}

fn wall_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

impl Default for WorkerLeaseClock {
    fn default() -> Self {
        Self { origin: Instant::now(), offset_ms: Mutex::new(wall_ms()) }
    }
}

impl WorkerLeaseClock {
    /// `(lease_now_ms, utc_now_ms)`.
    pub fn snapshot(&self) -> (i64, i64) {
        let mut offset = self.offset_ms.lock().expect("lease clock");
        let wall = wall_ms();
        let elapsed = self.origin.elapsed().as_millis() as i64;
        *offset = (*offset).max(wall - elapsed);
        (*offset + elapsed, wall)
    }

    pub fn now_ms(&self) -> i64 {
        self.snapshot().0
    }
}
