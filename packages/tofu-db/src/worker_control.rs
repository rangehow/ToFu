//! Commit-owned worker and conversation hints; no semantic transaction or log.
//! One generic wait owner provides gap detection, coalescing and joined close.
use crate::{
    protocol::{Request, Response},
    semantic::AuthenticatedScope,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex,
    },
    time::Duration,
};
use uuid::Uuid;

#[path = "generated_worker_control.rs"]
mod contract;
pub(crate) use contract::{CONVERSATION_WATCH_OPERATION, WATCH_OPERATION};

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct Cursor {
    epoch: String,
    #[serde(flatten)]
    generations: BTreeMap<String, u64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WatchRequest {
    cursor: Option<Cursor>,
}

struct State<H> {
    cursor: Cursor,
    closed: bool,
    waiters: BTreeMap<Uuid, BTreeSet<H>>,
}

struct CommitWatch<H> {
    state: Mutex<State<H>>,
    changed: Condvar,
    format: &'static str,
    hint_key: &'static str,
    hint_generation: &'static str,
}

impl<H: Clone + Ord + Serialize> CommitWatch<H> {
    fn new(
        generations: &[&str],
        format: &'static str,
        hint_key: &'static str,
        hint_generation: &'static str,
    ) -> Self {
        Self {
            state: Mutex::new(State {
                cursor: Cursor {
                    epoch: Uuid::new_v4().to_string(),
                    generations: generations.iter().map(|k| (k.to_string(), 0)).collect(),
                },
                closed: false,
                waiters: BTreeMap::new(),
            }),
            changed: Condvar::new(),
            format,
            hint_key,
            hint_generation,
        }
    }

    fn publish(&self, changed: &[&str], hints: &BTreeSet<H>) {
        if changed.is_empty() {
            return;
        }
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.closed {
            return;
        }
        if state.cursor.generations.values().any(|v| *v == u64::MAX) {
            state.cursor.epoch = Uuid::new_v4().to_string();
            state.cursor.generations.values_mut().for_each(|v| *v = 0);
        }
        for key in changed {
            *state
                .cursor
                .generations
                .get_mut(*key)
                .expect("declared generation") += 1;
        }
        for observed in state.waiters.values_mut() {
            observed.extend(hints.iter().cloned());
        }
        self.changed.notify_all();
    }

    fn close(&self) {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).closed = true;
        self.changed.notify_all();
    }

    #[cfg(test)]
    fn watch(&self, request: WatchRequest, timeout: Duration) -> io::Result<Vec<u8>> {
        self.watch_cancelled(request, timeout, None)
    }

    fn notify_cancel(&self) {
        let _state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        self.changed.notify_all();
    }

    fn watch_cancelled(
        &self,
        request: WatchRequest,
        timeout: Duration,
        cancellation: Option<&AtomicBool>,
    ) -> io::Result<Vec<u8>> {
        let cancelled = || cancellation.is_some_and(|flag| flag.load(Ordering::Acquire));
        let mut state = self
            .state
            .lock()
            .map_err(|_| io::Error::other("commit watch poisoned"))?;
        if request
            .cursor
            .as_ref()
            .is_some_and(|old| old.generations.keys().ne(state.cursor.generations.keys()))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid commit watch cursor",
            ));
        }
        let mut observed: Option<Vec<H>> = None;
        if request.cursor.as_ref() == Some(&state.cursor) && !state.closed && !cancelled() {
            let key = Uuid::new_v4();
            state.waiters.insert(key, BTreeSet::new());
            let (next, _) = self
                .changed
                .wait_timeout_while(state, timeout, |state| {
                    !state.closed && !cancelled() && request.cursor.as_ref() == Some(&state.cursor)
                })
                .map_err(|_| io::Error::other("commit watch poisoned"))?;
            state = next;
            let hints = state.waiters.remove(&key).unwrap_or_default();
            if request
                .cursor
                .as_ref()
                .is_some_and(|old| old.epoch == state.cursor.epoch)
            {
                observed = Some(hints.into_iter().collect());
            }
        } else if request.cursor.as_ref().is_some_and(|old| {
            old.epoch == state.cursor.epoch
                && old.generations[self.hint_generation]
                    == state.cursor.generations[self.hint_generation]
        }) {
            observed = Some(Vec::new());
        }
        // A closed component, like the sequencer or the WAL barrier: the restarted
        // authority serves the retry.
        if state.closed {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "commit control closed",
            ));
        }
        if cancelled() {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "commit watch cancelled",
            ));
        }
        serde_json::to_vec(&json!({"format":self.format,
            "cursor":state.cursor, self.hint_key:observed}))
        .map_err(io::Error::other)
    }
}

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConversationHint {
    user_id: u64,
    conversation_id: String,
}

pub(crate) struct WorkerControl {
    workers: CommitWatch<String>,
    conversations: CommitWatch<ConversationHint>,
    /// Hints publish at commit, before the WAL acknowledges it; an answer
    /// waits for that acknowledgement, so no watcher learns of a change a
    /// crash can still erase.
    acknowledgement: std::sync::OnceLock<crate::wal::WalBarrier>,
}

impl WorkerControl {
    pub(crate) fn new() -> Arc<Self> {
        Arc::new(Self {
            workers: CommitWatch::new(
                &["queue", "cancel"],
                contract::FORMAT,
                "cancelTasks",
                "cancel",
            ),
            conversations: CommitWatch::new(
                &["change"],
                contract::CONVERSATION_FORMAT,
                "conversations",
                "change",
            ),
            acknowledgement: std::sync::OnceLock::new(),
        })
    }

    pub(crate) fn attach_wal_barrier(&self, barrier: &crate::wal::WalBarrier) {
        let _ = self.acknowledgement.set(barrier.clone());
    }

    pub(crate) fn publish(&self, queue: bool, cancelled: &BTreeSet<String>) {
        let mut changed = Vec::new();
        if queue {
            changed.push("queue");
        }
        if !cancelled.is_empty() {
            changed.push("cancel");
        }
        self.workers.publish(&changed, cancelled);
    }

    pub(crate) fn publish_conversations<'a>(
        &self,
        user_id: u64,
        keys: impl Iterator<Item = &'a String>,
    ) {
        let hints: BTreeSet<_> = keys
            .map(|key| ConversationHint {
                user_id,
                conversation_id: key.clone(),
            })
            .collect();
        if !hints.is_empty() {
            self.conversations.publish(&["change"], &hints);
        }
    }

    pub(crate) fn close(&self) {
        self.workers.close();
        self.conversations.close();
    }

    pub(crate) fn notify_cancel(&self) {
        self.workers.notify_cancel();
        self.conversations.notify_cancel();
    }

    pub(crate) fn respond(
        &self,
        request: &Request,
        schema_id: u32,
        now: u64,
        scope: AuthenticatedScope,
    ) -> Response {
        self.respond_cancelled(request, schema_id, now, scope, None)
    }

    pub(crate) fn respond_cancelled(
        &self,
        request: &Request,
        schema_id: u32,
        now: u64,
        scope: AuthenticatedScope,
        cancellation: Option<&AtomicBool>,
    ) -> Response {
        let result = (|| {
            if request.schema_id != schema_id
                || request.command_id.is_some()
                || request.owner_id != scope.owner_id
                || request.tenant_id != scope.tenant_id
            {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "commit watch binding differs",
                ));
            }
            if request.deadline_unix_ms <= now {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "commit watch deadline elapsed",
                ));
            }
            let payload: serde_json::Value = serde_json::from_slice(&request.payload)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
            if payload
                .as_object()
                .is_none_or(|v| v.len() != 1 || !v.contains_key("cursor"))
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "commit watch requires cursor",
                ));
            }
            let watch = serde_json::from_value(payload)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
            let timeout = Duration::from_millis(request.deadline_unix_ms - now);
            let answer = match request.operation.as_str() {
                WATCH_OPERATION => self.workers.watch_cancelled(watch, timeout, cancellation),
                CONVERSATION_WATCH_OPERATION => {
                    self.conversations
                        .watch_cancelled(watch, timeout, cancellation)
                }
                _ => Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "unknown commit watch",
                )),
            }?;
            if let Some(barrier) = self.acknowledgement.get() {
                barrier.wait_requested_acknowledged(
                    std::time::Instant::now() + crate::wal::MAX_BARRIER_WAIT,
                )?;
            }
            Ok(answer)
        })();
        let (status, payload, error_code, error_message, retryable) = match result {
            Ok(value) => (0, value, None, None, false),
            // A binding (the permission check) or payload the caller must correct.
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::PermissionDenied | io::ErrorKind::InvalidInput
                ) =>
            {
                (
                    1,
                    Vec::new(),
                    Some("worker_control_refused".into()),
                    Some(error.to_string()),
                    false,
                )
            }
            // A closed or cancelled control, an elapsed deadline, an engine fault.
            Err(error) => {
                let message = error.to_string();
                let failure = crate::semantic_executor::map_storage_error(error);
                (
                    1,
                    Vec::new(),
                    Some(failure.code().into()),
                    Some(message),
                    failure.retryable(),
                )
            }
        };
        Response {
            correlation_id: request.correlation_id,
            schema_id,
            payload,
            status,
            error_code,
            error_message,
            retryable,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A watch reports a change only once the WAL acknowledges it: a hint
    /// published by a commit whose frame is still queued would let a watcher
    /// act on state a crash can erase.
    #[test]
    fn a_watch_answers_only_after_the_wal_acknowledges_the_change_it_reports() {
        use crate::vfs::{DeterministicVfs, Vfs};
        use std::path::Path;
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        let mut log =
            crate::wal::ActiveLog::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        log.set_durability(crate::wal::WalDurability::Normal);
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(65536);
        log.attach_barrier(&barrier);
        let control = WorkerControl::new();
        control.attach_wal_barrier(&barrier);
        let cursor = control.conversations.state.lock().unwrap().cursor.clone();
        log.append(1, [0; 32], b"queued commit").unwrap();
        control.publish_conversations(11, [String::from("c")].iter());
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        let request = Request {
            correlation_id: [0; 16],
            deadline_unix_ms: now + 60_000,
            owner_id: 11,
            tenant_id: None,
            command_id: None,
            schema_id: 1,
            operation: CONVERSATION_WATCH_OPERATION.to_owned(),
            payload: serde_json::to_vec(&json!({ "cursor": cursor })).unwrap(),
        };
        let scope = AuthenticatedScope {
            owner_id: 11,
            tenant_id: None,
        };
        let waiting = control.clone();
        let answer = std::thread::spawn(move || waiting.respond(&request, 1, now, scope));
        std::thread::sleep(Duration::from_millis(200));
        assert!(
            !answer.is_finished(),
            "a watch reported a change whose WAL frame is unacknowledged"
        );
        barrier.write_for_test(vfs.as_ref()).unwrap();
        let response = answer.join().unwrap();
        assert_eq!(response.status, 0, "{:?}", response.error_message);
        let value: serde_json::Value = serde_json::from_slice(&response.payload).unwrap();
        assert_eq!(value["cursor"]["change"], 1);
    }
    #[test]
    fn cancel_wakes_only_its_wait_without_publication() {
        let control = WorkerControl::new();
        let cursor = control.conversations.state.lock().unwrap().cursor.clone();
        let cancelled = Arc::new(AtomicBool::new(false));
        let waiting = control.clone();
        let flag = cancelled.clone();
        let expected = cursor.clone();
        let thread = std::thread::spawn(move || {
            waiting.conversations.watch_cancelled(
                WatchRequest {
                    cursor: Some(expected),
                },
                Duration::from_secs(60),
                Some(&flag),
            )
        });
        // Also covers cancellation before enrollment: the flag is persistent,
        // and notification takes the same condition lock as the predicate.
        cancelled.store(true, Ordering::Release);
        control.notify_cancel();
        assert_eq!(
            thread.join().unwrap().unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(control
            .conversations
            .state
            .lock()
            .unwrap()
            .waiters
            .is_empty());
        let value: serde_json::Value = serde_json::from_slice(
            &control
                .conversations
                .watch(
                    WatchRequest {
                        cursor: Some(cursor),
                    },
                    Duration::ZERO,
                )
                .unwrap(),
        )
        .unwrap();
        assert_eq!(value["cursor"]["change"], 0);
        assert_eq!(value["conversations"], json!([]));
    }

    #[test]
    fn conversation_gap_is_independent_and_history_free() {
        let control = WorkerControl::new();
        let initial = control.conversations.state.lock().unwrap().cursor.clone();
        control.publish(true, &BTreeSet::from(["worker".into()]));
        let unchanged: serde_json::Value = serde_json::from_slice(
            &control
                .conversations
                .watch(
                    WatchRequest {
                        cursor: Some(initial.clone()),
                    },
                    Duration::ZERO,
                )
                .unwrap(),
        )
        .unwrap();
        assert_eq!(unchanged["cursor"]["change"], 0);
        assert_eq!(unchanged["conversations"], json!([]));
        control.publish_conversations(41, [&"one".to_string()].into_iter());
        let changed: serde_json::Value = serde_json::from_slice(
            &control
                .conversations
                .watch(
                    WatchRequest {
                        cursor: Some(initial),
                    },
                    Duration::ZERO,
                )
                .unwrap(),
        )
        .unwrap();
        assert_eq!(changed["cursor"]["change"], 1);
        assert!(changed["conversations"].is_null());
        assert!(control
            .conversations
            .state
            .lock()
            .unwrap()
            .waiters
            .is_empty());
        control.close();
        assert!(control
            .conversations
            .watch(WatchRequest { cursor: None }, Duration::ZERO)
            .is_err());
    }
    #[test]
    fn gap_survives_without_notification_history() {
        let control = WorkerControl::new();
        let first: serde_json::Value = serde_json::from_slice(
            &control
                .workers
                .watch(WatchRequest { cursor: None }, Duration::ZERO)
                .unwrap(),
        )
        .unwrap();
        control.publish(true, &BTreeSet::from(["task".into()]));
        let next: serde_json::Value = serde_json::from_slice(
            &control
                .workers
                .watch(
                    WatchRequest {
                        cursor: Some(serde_json::from_value(first["cursor"].clone()).unwrap()),
                    },
                    Duration::ZERO,
                )
                .unwrap(),
        )
        .unwrap();
        assert_eq!(next["cursor"]["queue"], 1);
        assert!(next["cancelTasks"].is_null());
        assert!(control.workers.state.lock().unwrap().waiters.is_empty());
    }
    /// A closed commit control is a component closed for a stop or restart,
    /// like the sequencer, the logical outbox and the WAL barrier: BrokenPipe,
    /// which the storage mapping keeps retryable, never an unclassified error
    /// (DB-6, R4b B10 condition C1).
    #[test]
    fn a_closed_commit_control_fails_its_watches_as_a_closed_component() {
        let control = WorkerControl::new();
        control.close();
        for error in [
            control
                .workers
                .watch(WatchRequest { cursor: None }, Duration::ZERO)
                .unwrap_err(),
            control
                .conversations
                .watch(WatchRequest { cursor: None }, Duration::ZERO)
                .unwrap_err(),
        ] {
            assert_eq!(error.kind(), io::ErrorKind::BrokenPipe, "{error}");
            assert!(crate::semantic_executor::map_storage_error(error).retryable());
        }
    }
    #[test]
    fn shutdown_releases_wait_without_authority_lock() {
        let control = WorkerControl::new();
        let cursor = control.workers.state.lock().unwrap().cursor.clone();
        let waiting = control.clone();
        let thread = std::thread::spawn(move || {
            waiting.workers.watch(
                WatchRequest {
                    cursor: Some(cursor),
                },
                Duration::from_secs(60),
            )
        });
        control.close();
        assert!(thread.join().unwrap().is_err());
    }
}
