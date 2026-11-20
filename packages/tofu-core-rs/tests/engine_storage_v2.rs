//! The adapter's public storage boundary, using P3a framing and real TofuDB.
//! The host projection remains a fixture, not rich Turn projection parity.

// This adapter uses a subset of the storage client's transport fault fixture.
#[allow(dead_code)]
#[path = "../../tofu-storage-client/tests/fake_daemon/mod.rs"]
mod fake_daemon;

use fake_daemon::{Action, FakeDaemon};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tofu_core_rs::engine::ports::{DurableStore, StoreError};
use tofu_core_rs::engine::storage_v2::{
    CheckpointArchive, PreparedTurnEvent, SteerRequeue, StorageIdentity, StorageProjection,
    StorageV2DurableStore,
};
use tofu_storage_client::{ClientConfig, KnownCredentialRedactor, StorageV2Client};

struct Projection(AtomicU64, u64);
impl StorageProjection for Projection {
    fn prepare(
        &self,
        _: &StorageIdentity,
        event: &Value,
        terminal: Option<&Value>,
    ) -> Result<PreparedTurnEvent, StoreError> {
        Ok(PreparedTurnEvent {
            base_revision: self.1 + event["seq"].as_u64().unwrap_or(1).saturating_sub(1),
            projection: json!({"content": "host-owned", "toolRounds": [{"opaque": true}]})
                .as_object()
                .unwrap()
                .clone(),
            status: if terminal.is_some() {
                "completed"
            } else {
                "running"
            }
            .into(),
            settlement: json!({}).as_object().unwrap().clone(),
            error: Value::Null,
            event_type: if terminal.is_some() {
                "terminal_settlement"
            } else {
                "projection_updated"
            }
            .into(),
            event_payload: json!({"updateKind": event["type"]})
                .as_object()
                .unwrap()
                .clone(),
            sequence: event["seq"]
                .as_u64()
                .unwrap_or_else(|| self.0.fetch_add(1, Ordering::SeqCst)),
            now_ms: 100,
        })
    }
}
#[derive(Default)]
struct Requeue(Mutex<Vec<(Value, String)>>);
impl SteerRequeue for Requeue {
    fn confirm(&self, _: &StorageIdentity, _: &[Value]) -> Result<(), StoreError> {
        Ok(())
    }
    fn requeue(
        &self,
        _: &StorageIdentity,
        record: &Value,
        command: &str,
    ) -> Result<(), StoreError> {
        self.0
            .lock()
            .unwrap()
            .push((record.clone(), command.into()));
        Ok(())
    }
}
fn identity() -> StorageIdentity {
    StorageIdentity {
        owner_user_id: 7,
        conversation_id: "conv".into(),
        task_id: "task".into(),
        turn_id: "turn".into(),
        attempt_id: "attempt".into(),
    }
}
fn store(daemon: &FakeDaemon, requeue: Arc<Requeue>) -> StorageV2DurableStore {
    let client = Arc::new(
        StorageV2Client::new(ClientConfig::new(
            "127.0.0.1",
            daemon.port,
            b"0123456789abcdef0123456789abcdef",
            7,
            Some(1),
        ))
        .unwrap(),
    );
    client.set_write_redactor(Arc::new(KnownCredentialRedactor::build([(
        "GITHUB_TOKEN",
        Some("secret-value-0123456789"),
    )])));
    StorageV2DurableStore::new(
        client,
        identity(),
        CheckpointArchive {
            archive_id: "archive".into(),
            created_at_ms: 100,
        },
        Arc::new(Projection(AtomicU64::new(100), 0)),
        requeue,
    )
    .unwrap()
}
fn event(seq: u64) -> Value {
    json!({"type": "round_start", "roundNum": 1, "seq": seq, "taskId": "task", "attemptId": "attempt"})
}

#[test]
fn terminal_and_full_host_projection_are_one_command_and_refusal_is_not_ack() {
    let daemon = FakeDaemon::start(|_, index| {
        Action::Ok(json!({"applied": index > 1, "task_event_count": 1}))
    });
    let store = store(&daemon, Arc::new(Requeue::default()));
    let terminal = json!({"type": "done", "finishReason": "stop"});
    let summary = json!({"turnId": "turn", "attemptId": "attempt", "status": "done", "content": "small-summary"});
    let error = store.settle_terminal(&terminal, &summary).unwrap_err();
    assert!(!error.transient && !error.not_dispatched);
    store.settle_terminal(&terminal, &summary).unwrap();
    let seen = daemon.seen();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0].operation, "turn.event.record");
    assert_eq!(seen[0].command_id, seen[1].command_id);
    assert_eq!(seen[0].payload, seen[1].payload);
    assert_eq!(
        seen[0].payload["projection_patch"]["operations"][0]["value"]["content"],
        "host-owned"
    );
    assert_eq!(seen[0].payload["task_event"]["event"], terminal);
    assert_eq!(seen[0].payload["terminal"], true);
    assert_eq!(seen[0].payload["user_id"], 7);
}

#[test]
fn structural_batch_is_one_atomic_operation_and_barriers_stay_separate() {
    let daemon = FakeDaemon::start(|seen, _| {
        if seen.operation == "turn.event.record_batch" {
            Action::Ok(
                json!([{"applied": true, "task_event_count": 1}, {"applied": true, "task_event_count": 1}]),
            )
        } else {
            Action::Ok(json!({"applied": true, "task_event_count": 1}))
        }
    });
    let store = store(&daemon, Arc::new(Requeue::default()));
    store.record_events(&[event(1), event(2)]).unwrap();
    let intent = json!({"type": "tool_effect", "roundNum": 1, "toolName": "write_file", "toolCallId": "call", "executionEffect": {"operationId": "operation", "state": "entered", "recovery": "reconcile", "scope": "attempt"}});
    store.commit_tool_intent(&intent).unwrap();
    assert!(store.record_events(&[event(3), intent]).is_err());
    let seen = daemon.seen();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0].operation, "turn.event.record_batch");
    assert_eq!(seen[0].payload["entries"][1]["task_event"]["sequence"], 2);
    assert_eq!(seen[1].operation, "turn.event.record");
}

#[test]
fn checkpoint_pending_create_precedes_atomic_view_and_receipt_update() {
    let daemon = FakeDaemon::start(|seen, _| {
        Action::Ok(match seen.operation.as_str() {
            "compaction_archive.create" => json!({"created": true, "archiveId": "archive"}),
            "compaction_archive.update_summary" => json!({"updated": true}),
            _ => panic!("unexpected operation"),
        })
    });
    let store = store(&daemon, Arc::new(Requeue::default()));
    let checkpoint = json!({"turnId": "turn", "attemptId": "attempt", "status": "completed", "implementation": "suspended_working_context", "trigger": "checkpoint", "resumeWitness": {"version": 1, "turnId": "turn"}, "postMessages": [{"role": "user", "content": "secret-value-0123456789"}]});
    store.save_checkpoint(&checkpoint).unwrap();
    store.save_checkpoint(&checkpoint).unwrap();
    let seen = daemon.seen();
    assert_eq!(seen[0].payload["receipt"]["status"], "pending");
    assert_eq!(seen[1].payload["receipt"]["status"], "completed");
    assert!(seen[1].payload["receipt"].get("postMessages").is_none());
    assert!(!seen[1]
        .payload
        .to_string()
        .contains("secret-value-0123456789"));
    assert_eq!(seen[0].command_id, seen[2].command_id);
    assert_eq!(seen[1].command_id, seen[3].command_id);
}

#[test]
fn reads_committed_steers_and_requeues_original_author_with_stable_id() {
    let record = json!({"blockId": "injection:user-steer:command", "deliveryState": "pending", "author": {"actor": "human", "text": "original"}, "previews": [{"text": "original", "images": [{"opaque": true}]}]});
    let original = record.clone();
    let daemon = FakeDaemon::start(move |_, _| {
        Action::Ok(
            json!({"currentAttemptId": "attempt", "projection": {"_userSteerInjects": [record]}}),
        )
    });
    let requeue = Arc::new(Requeue::default());
    let store = store(&daemon, requeue.clone());
    assert_eq!(store.take_steers().unwrap(), vec![original.clone()]);
    store
        .requeue_steers(&[
            json!({"blockId": "injection:user-steer:command", "deliveryState": "queued"}),
        ])
        .unwrap();
    assert_eq!(
        requeue.0.lock().unwrap()[0],
        (
            original,
            "steer-requeue:injection:user-steer:command".into()
        )
    );
    assert!(daemon
        .seen()
        .iter()
        .all(|seen| seen.operation == "turn.get"));
}

#[test]
fn foreign_events_are_rejected_before_transport() {
    let daemon = FakeDaemon::start(|_, _| panic!("must not dispatch"));
    let store = store(&daemon, Arc::new(Requeue::default()));
    let mut foreign = event(1);
    foreign["attemptId"] = json!("foreign");
    assert!(store.record_events(&[foreign]).unwrap_err().not_dispatched);
    assert!(daemon.seen().is_empty());
}

#[cfg(unix)]
#[path = "support/engine_recovery.rs"]
mod scratch_recovery;

#[cfg(unix)]
mod real_authority {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixStream;
    use std::path::PathBuf;
    use std::thread::JoinHandle;
    use std::time::{SystemTime, UNIX_EPOCH};
    use tofu_db::daemon::{serve_supervised, DaemonConfig};
    use tofu_db::engine::Engine;

    pub(super) struct Daemon {
        pub(super) root: PathBuf,
        lease: Option<UnixStream>,
        worker: Option<JoinHandle<()>>,
        pub(super) port: u16,
    }
    impl Daemon {
        pub(super) fn start() -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir()
                .join(format!("engine-storage-v2-{}-{nonce}", std::process::id()));
            let db = root.join("db");
            std::fs::create_dir_all(&root).unwrap();
            drop(Engine::initialize(&db).unwrap());
            let config = DaemonConfig::new(
                &db,
                7,
                Some(1),
                &mut b"0123456789abcdef0123456789abcdef".to_vec(),
            )
            .unwrap();
            let (lease, reader) = UnixStream::pair().unwrap();
            let (ready, mut writer) = UnixStream::pair().unwrap();
            let worker = std::thread::spawn(move || {
                serve_supervised(config, reader, &mut writer).unwrap();
            });
            let mut line = String::new();
            BufReader::new(ready).read_line(&mut line).unwrap();
            let readiness: Value = serde_json::from_str(&line).unwrap();
            Self {
                root,
                lease: Some(lease),
                worker: Some(worker),
                port: readiness["port"].as_u64().unwrap() as u16,
            }
        }
        pub(super) fn client(&self) -> Arc<StorageV2Client> {
            fixture_client(self.port)
        }
    }

    pub(super) fn fixture_client(port: u16) -> Arc<StorageV2Client> {
        let client = Arc::new(
            StorageV2Client::new(ClientConfig::new(
                "127.0.0.1",
                port,
                b"0123456789abcdef0123456789abcdef",
                7,
                Some(1),
            ))
            .unwrap(),
        );
        client.set_write_redactor(Arc::new(KnownCredentialRedactor::default()));
        client
    }
    impl Drop for Daemon {
        fn drop(&mut self) {
            self.lease.take();
            if let Some(worker) = self.worker.take() {
                worker.join().unwrap();
            }
            std::fs::remove_dir_all(&self.root).unwrap();
        }
    }
    pub(super) fn command(
        client: &StorageV2Client,
        operation: &str,
        payload: Value,
        id: &str,
    ) -> Value {
        client
            .command(operation, payload.as_object(), Some(id), None)
            .unwrap()
    }

    #[test]
    fn real_storage_commits_events_checkpoint_and_terminal_projection() {
        let daemon = Daemon::start();
        let client = daemon.client();
        let pair = command(
            &client,
            "turn.create_pair",
            json!({
                "conversation_id": "real-conversation", "user_id": 7, "command_id": "create-real-pair",
                "input_projection": {"content": "synthetic input"}, "config": {"model": "fixture"},
                "now": 100, "conversation_defaults": {"allowCreate": true, "title": "storage binding", "createdAt": 100, "settings": {}},
            }),
            "create-real-pair",
        );
        let attempt = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
        let turn = pair["attempt"]["turnId"].as_str().unwrap().to_owned();
        command(
            &client,
            "turn.attempt.bind",
            json!({"attempt_id": attempt, "task_id": "task", "user_id": 7}),
            "bind-real-attempt",
        );
        let identity = StorageIdentity {
            owner_user_id: 7,
            conversation_id: "real-conversation".into(),
            task_id: "task".into(),
            turn_id: turn.clone(),
            attempt_id: attempt.clone(),
        };
        let head = client
            .query(
                "turn.get",
                json!({
                    "conversation_id": "real-conversation", "turn_id": turn, "user_id": 7,
                })
                .as_object(),
                None,
                None,
            )
            .unwrap();
        let base_revision = head["projectionRevision"].as_u64().unwrap();
        let store = StorageV2DurableStore::new(
            client.clone(),
            identity,
            CheckpointArchive {
                archive_id: "real-checkpoint".into(),
                created_at_ms: 100,
            },
            Arc::new(Projection(AtomicU64::new(100), base_revision)),
            Arc::new(Requeue::default()),
        )
        .unwrap();
        let events = [
            json!({"type": "round_start", "roundNum": 1, "seq": 1}),
            json!({"type": "round_end", "roundNum": 1, "reason": "complete", "seq": 2}),
        ];
        let initial_revision = store.read_turn().unwrap()["projectionRevision"].clone();
        assert!(store
            .record_events(&[
                events[0].clone(),
                json!({"type": "round_start", "roundNum": 2, "seq": 3})
            ])
            .is_err());
        assert_eq!(
            store.read_turn().unwrap()["projectionRevision"],
            initial_revision
        );
        assert!(store
            .read_task_events(0, 100)
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty());
        store.record_events(&events).unwrap();
        let checkpoint = json!({"turnId": turn, "attemptId": attempt, "status": "completed", "implementation": "suspended_working_context", "trigger": "checkpoint", "resumeWitness": {"version": 1, "turnId": turn}, "postMessages": [{"role": "user", "content": "synthetic input"}]});
        store.save_checkpoint(&checkpoint).unwrap();
        let readback = store.read_checkpoint("real-checkpoint").unwrap();
        assert_eq!(readback["postMessages"], checkpoint["postMessages"]);
        assert_eq!(readback["archive"]["receipt"]["status"], "completed");
        let intent = json!({"type": "tool_effect", "seq": 3, "roundNum": 1, "toolName": "write_file", "toolCallId": "call", "executionEffect": {"operationId": "operation", "state": "entered", "recovery": "reconcile", "scope": "attempt"}});
        store.commit_tool_intent(&intent).unwrap();
        let revision = store.read_turn().unwrap()["projectionRevision"].clone();
        assert!(store.commit_tool_intent(&intent).is_err());
        assert_eq!(store.read_turn().unwrap()["projectionRevision"], revision);
        let terminal = json!({"type": "done", "finishReason": "stop", "seq": 4});
        store
            .settle_terminal(&terminal, &json!({"turnId": turn, "attemptId": attempt}))
            .unwrap();
        let settled_revision = store.read_turn().unwrap()["projectionRevision"].clone();
        store
            .settle_terminal(&terminal, &json!({"turnId": turn, "attemptId": attempt}))
            .unwrap();
        let turn = store.read_turn().unwrap();
        assert_eq!(turn["projectionRevision"], settled_revision);
        assert_eq!(turn["status"], "completed");
        assert_eq!(turn["projection"]["content"], "host-owned");
        let rows = store.read_task_events(0, 100).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 4);
        assert_eq!(rows[2]["event"]["type"], "tool_effect");
        assert_eq!(rows[3]["event"]["type"], "done");
        assert!(store
            .record_events(&[json!({"type": "round_start", "roundNum": 2, "seq": 5})])
            .is_err());
    }
}
