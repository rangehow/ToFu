//! Retry, deadline, streaming and publication specifications of the client,
//! mirrored from `tests/storage/test_storage_v2_client.py` and its neighbours.

mod fake_daemon;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use fake_daemon::{Action, FakeDaemon};
use serde_json::{json, Map, Value};
use tofu_storage_client::{
    Cancellation, ClientConfig, KnownCredentialRedactor, RuntimeRecovery, StorageError,
    StorageObserver, StorageResult, StorageV2Client,
};

const SECRET: &[u8] = b"0123456789abcdef0123456789abcdef";

fn client(daemon: &FakeDaemon) -> StorageV2Client {
    let mut config = ClientConfig::new("127.0.0.1", daemon.port, SECRET, 7, Some(1));
    config.timeout = Duration::from_secs(5);
    StorageV2Client::new(config).unwrap()
}

fn object(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

#[test]
fn constructor_validation_matches_the_python_client() {
    for config in [
        ClientConfig::new("10.0.0.1", 1, SECRET, 7, None),
        ClientConfig::new("127.0.0.1", 1, b"short", 7, None),
        ClientConfig::new("127.0.0.1", 1, SECRET, 0, None),
        ClientConfig::new("127.0.0.1", 1, SECRET, 7, Some(0)),
    ] {
        assert!(StorageV2Client::new(config).is_err());
    }
}

#[test]
fn queries_inject_the_owner_and_never_send_a_command_id() {
    let daemon = FakeDaemon::start(|_, _| Action::Ok(json!({"ok": 1})));
    let storage = client(&daemon);
    let result = storage
        .query(
            "conversation.get",
            Some(&object(json!({"id": "c"}))),
            None,
            None,
        )
        .unwrap();
    assert_eq!(result, json!({"ok": 1}));
    storage
        .command("conversation.delete", None, Some("cmd-1"), None)
        .unwrap();
    storage.maintenance("turn.repair", None, None).unwrap();
    let seen = daemon.seen();
    assert_eq!(seen[0].payload, json!({"id": "c", "user_id": 7}));
    assert_eq!(seen[0].command_id, None);
    assert_eq!(seen[1].command_id.as_deref(), Some("cmd-1"));
    let generated = seen[2].command_id.as_deref().unwrap();
    assert!(generated.starts_with("turn.repair:") && generated.len() == "turn.repair:".len() + 32);
    // One pooled session served all three calls.
    assert!(seen
        .iter()
        .all(|item| item.connection == seen[0].connection));
}

#[test]
fn a_closed_connection_replays_the_command_under_its_identity_then_raises() {
    let daemon = FakeDaemon::start(|_, _| Action::Close);
    let storage = client(&daemon);
    let error = storage
        .command(
            "conversation.delete",
            None,
            Some("same-id"),
            Some(Duration::from_secs(2)),
        )
        .unwrap_err();
    assert_eq!(
        (
            error.code.as_str(),
            error.message.as_str(),
            error.retryable,
            error.retry_after_ms
        ),
        (
            "database_unavailable",
            "storage.v2 authority closed the connection",
            true,
            Some(50)
        )
    );
    let seen = daemon.seen();
    assert_eq!(seen.len(), 3);
    assert!(seen
        .iter()
        .all(|item| item.command_id.as_deref() == Some("same-id")));
}

#[test]
fn deadline_elapsed_maps_to_timeout_and_is_replayed() {
    let daemon = FakeDaemon::start(|_, index| {
        if index <= 3 {
            Action::Error("deadline_elapsed", "deadline", false)
        } else {
            Action::Ok(json!(1))
        }
    });
    let storage = client(&daemon);
    let error = storage
        .query("conversation.count", None, None, None)
        .unwrap_err();
    assert_eq!(
        (error.code.as_str(), error.retryable),
        ("database_timeout", false)
    );
    assert_eq!(daemon.seen().len(), 3);
}

#[test]
fn a_non_transient_error_is_raised_once_with_its_operation() {
    let daemon = FakeDaemon::start(|_, _| Action::Error("database_conflict", "lost race", false));
    let storage = client(&daemon);
    let error = storage
        .command("conversation.delete", None, None, None)
        .unwrap_err();
    assert_eq!(
        (
            error.code.as_str(),
            error.message.as_str(),
            error.operation_id.as_str()
        ),
        ("database_conflict", "lost race", "conversation.delete")
    );
    assert_eq!(daemon.seen().len(), 1);
}

#[test]
fn a_busy_command_uses_its_own_sixteen_attempt_schedule() {
    let daemon = FakeDaemon::start(|_, _| Action::Error("database_busy", "busy", true));
    let storage = client(&daemon);
    let error = storage
        .command("conversation.delete", None, None, None)
        .unwrap_err();
    assert_eq!(error.code, "database_busy");
    assert_eq!(daemon.seen().len(), 16);
}

#[test]
fn resource_exhaustion_splits_by_retry_policy() {
    let shed =
        FakeDaemon::start(|_, _| Action::Error("database_resource_exhausted", "quota", true));
    let storage = client(&shed);
    let error = storage
        .query("conversation.count", None, None, None)
        .unwrap_err();
    assert_eq!(
        (
            error.code.as_str(),
            error.retry_after_ms,
            error.is_transient()
        ),
        ("database_unavailable", Some(500), true)
    );
    assert_eq!(shed.seen().len(), 3);

    let bound =
        FakeDaemon::start(|_, _| Action::Error("database_resource_exhausted", "bound", false));
    let storage = client(&bound);
    let error = storage
        .query("conversation.count", None, None, None)
        .unwrap_err();
    assert_eq!(
        (
            error.code.as_str(),
            error.retry_after_ms,
            error.is_transient()
        ),
        ("database_resource_exhausted", None, false)
    );
    assert_eq!(bound.seen().len(), 1);
}

#[test]
fn a_command_payload_without_a_write_redactor_is_refused_before_sending() {
    let daemon = FakeDaemon::start(|_, _| Action::Ok(json!({"ok": 1})));
    let storage = client(&daemon);
    let error = storage
        .command(
            "turn.event.append",
            Some(&object(json!({"text": "tool output"}))),
            None,
            None,
        )
        .unwrap_err();
    assert_eq!(
        (error.code.as_str(), error.message.as_str(), error.retryable),
        (
            "database_protocol_error",
            "storage.v2 command refused: no write redactor is installed",
            false
        )
    );
    assert!(daemon.seen().is_empty());
    // A payload-free command carries no output to redact.
    storage
        .command("conversation.delete", None, None, None)
        .unwrap();
    assert_eq!(daemon.seen().len(), 1);
}

#[test]
fn large_requests_upload_in_chunks_and_large_responses_reassemble() {
    let big = "x".repeat(9 * 1024 * 1024);
    let echo = big.clone();
    let daemon = FakeDaemon::start(move |_, _| Action::Chunked(json!({"text": echo}), 1024 * 1024));
    let storage = client(&daemon);
    storage.set_write_redactor(Arc::new(KnownCredentialRedactor::default()));
    let result = storage
        .command(
            "artifact.create",
            Some(&object(json!({"text": big}))),
            None,
            None,
        )
        .unwrap();
    assert_eq!(result["text"].as_str().unwrap().len(), 9 * 1024 * 1024);
    let seen = daemon.seen();
    assert_eq!(seen[0].chunks, 10);
    assert_eq!(seen[0].payload["user_id"], 7);
}

#[test]
fn query_many_pipelines_and_rereads_transient_answers_through_query() {
    let daemon = FakeDaemon::start(|seen, index| match seen.operation.as_str() {
        "conversation.count" if index == 1 => Action::Error("database_busy", "busy", true),
        "conversation.get" => Action::Error("database_not_found", "missing", false),
        _ => Action::Ok(json!(index)),
    });
    let storage = client(&daemon);
    let outcomes = storage.query_many(
        &[
            ("conversation.count".to_owned(), None),
            ("conversation.get".to_owned(), None),
            ("conversation.list".to_owned(), None),
        ],
        None,
    );
    assert_eq!(outcomes[0].as_ref().unwrap(), &json!(2));
    assert_eq!(outcomes[1].as_ref().unwrap_err().code, "database_not_found");
    assert_eq!(outcomes[2].as_ref().unwrap(), &json!(1));
    let seen = daemon.seen();
    assert_eq!(seen.len(), 4);
    assert!(seen[..3]
        .iter()
        .all(|item| item.connection == seen[0].connection));
}

#[derive(Default)]
struct Recorder {
    events: Mutex<Vec<Value>>,
    operations: Mutex<Vec<String>>,
}

impl StorageObserver for Recorder {
    fn committed_events(&self, events: &[Value]) {
        self.events.lock().unwrap().extend_from_slice(events);
    }
    fn committed_operation(&self, operation: &str) {
        self.operations.lock().unwrap().push(operation.to_owned());
    }
}

#[test]
fn commands_redact_known_credentials_and_publish_committed_notices() {
    let contract = tofu_storage_client::contract::contract();
    let envelope = json!({
        "_storageCommitContract": contract.committed_events_contract,
        "value": {"saved": true},
        "events": [{"contract": contract.commit_notice_contract, "topic": "conversation",
                    "userId": 7, "key": "c1", "sequence": 4}],
    });
    let daemon = FakeDaemon::start(move |_, _| Action::Ok(envelope.clone()));
    let storage = client(&daemon);
    storage.set_write_redactor(Arc::new(KnownCredentialRedactor::build([(
        "GITHUB_TOKEN",
        Some("ghp_0123456789abcdef"),
    )])));
    let recorder = Arc::new(Recorder::default());
    storage.set_observer(recorder.clone());
    let value = storage
        .command(
            "turn.event.append",
            Some(&object(
                json!({"text": "token ghp_0123456789abcdef used", "n": 1}),
            )),
            None,
            None,
        )
        .unwrap();
    assert_eq!(value, json!({"saved": true}));
    assert_eq!(
        daemon.seen()[0].payload,
        json!({"text": "token [redacted credential: GITHUB_TOKEN] used", "n": 1, "user_id": 7})
    );
    assert_eq!(
        recorder.events.lock().unwrap().as_slice(),
        &[
            json!({"contract": "storage.conversation-commit/v1", "userId": 7,
                 "event": {"conversationId": "c1", "syncSeq": 4}})
        ]
    );
    assert_eq!(
        recorder.operations.lock().unwrap().as_slice(),
        &["turn.event.append".to_owned()]
    );
}

#[test]
fn health_falls_back_to_a_semantic_round_trip_on_old_daemons() {
    let daemon = FakeDaemon::start(|seen, _| match seen.operation.as_str() {
        "system.health" => Action::Error("unknown_operation", "unknown", false),
        _ => Action::Ok(json!(3)),
    });
    let storage = client(&daemon);
    let health = storage.health(None);
    assert_eq!(health["ready"], true);
    assert_eq!(health["owner_id"], 7);
    let current =
        FakeDaemon::start(|_, _| Action::Ok(json!({"ready": true, "walDurability": "strict"})));
    let health = client(&current).health(None);
    assert_eq!(
        (health["ready"].clone(), health["walDurability"].clone()),
        (json!(true), json!("strict"))
    );
}

#[test]
fn a_wedged_peer_times_out_as_unavailable() {
    let daemon = FakeDaemon::start(|_, _| Action::Delay(Duration::from_secs(3), json!(1)));
    let mut config = ClientConfig::new("127.0.0.1", daemon.port, SECRET, 7, None);
    config.read_attempts = 1;
    let storage = StorageV2Client::new(config).unwrap();
    let error = storage
        .query(
            "conversation.count",
            None,
            Some(Duration::from_millis(100)),
            None,
        )
        .unwrap_err();
    assert_eq!(
        (
            error.code.as_str(),
            error.message.as_str(),
            error.retry_after_ms
        ),
        (
            "database_unavailable",
            "storage.v2 authority is unavailable",
            Some(100)
        )
    );
}

#[test]
fn an_unreachable_authority_is_unavailable() {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let storage =
        StorageV2Client::new(ClientConfig::new("127.0.0.1", port, SECRET, 7, None)).unwrap();
    let error = storage
        .query("conversation.count", None, None, None)
        .unwrap_err();
    assert_eq!(
        (error.code.as_str(), error.retryable),
        ("database_unavailable", true)
    );
}

#[test]
fn a_cancelled_call_is_not_replayed() {
    let daemon = FakeDaemon::start(|_, _| Action::Error("database_busy", "busy", true));
    let storage = client(&daemon);
    let cancellation = Cancellation::new();
    cancellation.cancel();
    assert!(storage
        .query("conversation.count", None, None, Some(&cancellation))
        .is_err());
    // A binding made after cancellation interrupts at once: at most one dispatch.
    assert!(daemon.seen().len() <= 1);
}

struct Generation {
    next: Arc<StorageV2Client>,
    calls: AtomicUsize,
}

impl RuntimeRecovery for Generation {
    fn resolve(&self, _pause: Duration) -> StorageResult<Arc<StorageV2Client>> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok(self.next.clone())
    }
}

#[test]
fn a_bound_runtime_waits_past_the_attempt_bound_and_fences_scope() {
    let daemon = FakeDaemon::start(|_, index| {
        if index <= 5 {
            Action::Error("database_unavailable", "restarting", true)
        } else {
            Action::Ok(json!("live"))
        }
    });
    let storage = client(&daemon);
    let recovery = Arc::new(Generation {
        next: Arc::new(client(&daemon)),
        calls: AtomicUsize::new(0),
    });
    storage.bind_runtime_recovery(recovery.clone());
    assert_eq!(
        storage
            .query("conversation.count", None, None, None)
            .unwrap(),
        json!("live")
    );
    assert_eq!(recovery.calls.load(Ordering::SeqCst), 5);

    let failing = FakeDaemon::start(|_, _| Action::Error("database_unavailable", "down", true));
    let storage = client(&failing);
    storage.bind_runtime_recovery(Arc::new(Generation {
        next: Arc::new(
            StorageV2Client::new(ClientConfig::new(
                "127.0.0.1",
                failing.port,
                SECRET,
                8,
                Some(1),
            ))
            .unwrap(),
        ),
        calls: AtomicUsize::new(0),
    }));
    let error: StorageError = storage
        .query("conversation.count", None, None, None)
        .unwrap_err();
    assert_eq!(error.code, "database_integrity");
}

#[test]
fn commit_watches_send_only_the_cursor_and_honor_their_deadline() {
    let daemon = FakeDaemon::start(|_, _| Action::Ok(json!({"cursor": {"queue": 1}})));
    let storage = client(&daemon);
    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    storage
        .worker_watch(Value::Null, now_ms + 2_000, None)
        .unwrap();
    assert_eq!(daemon.seen()[0].payload, json!({"cursor": null}));
    assert_eq!(daemon.seen()[0].operation, "worker_control.watch");
    let expired = storage
        .conversation_watch(Value::Null, now_ms - 1, None)
        .unwrap_err();
    assert_eq!(
        (expired.code.as_str(), expired.message.as_str()),
        ("database_timeout", "Commit watch deadline elapsed")
    );
}
