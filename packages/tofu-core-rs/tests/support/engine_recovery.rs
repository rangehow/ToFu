//! Scratch-only process-loss adapter for the Phase 4 engine. The parent owns
//! the disposable real storage.v2 authority; neither child owns its lifetime.
//! This verifies the completed view but cannot turn it into execution authority.

use super::real_authority::{command, fixture_client, Daemon};
use super::*;
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;
use tofu_core_rs::engine::cancel::CancelToken;
use tofu_core_rs::engine::checkpoint::{message_json, resume_execution_digest};
use tofu_core_rs::engine::ports::{EventSink, ProviderPort, ProviderRequest, StreamItem, ToolPort};
use tofu_core_rs::engine::redaction::ExactValueRedactor;
use tofu_core_rs::engine::session::{run, Ports, SessionConfig};
use tofu_core_rs::engine::types::{EffectClass, Message, ProviderOutcome, Role, ToolCall, ToolRun};

const ARCHIVE: &str = "scratch-recovery-checkpoint";
const MODEL: &str = "synthetic-model";
const CHILD: &str = "scratch_recovery::engine_child";
const FRAME: &str = "scratch-engine-frame: ";
const NO_AUTHORITY: &str = "accepted_successor_and_claim_required";
const WATCHDOG: Duration = Duration::from_secs(30);

fn tool_call() -> ToolCall {
    ToolCall {
        id: "scratch-call".into(),
        name: "scratch_marker".into(),
        arguments: "{}".into(),
    }
}

fn user_message() -> Message {
    Message::text(Role::User, "Write one synthetic marker.")
}

fn record(path: &Path, value: &Value) {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .unwrap();
    writeln!(file, "{value}").unwrap();
    file.sync_all().unwrap();
}

struct OfflinePorts<'a>(&'a Path);
impl ProviderPort for OfflinePorts<'_> {
    fn stream(
        &self,
        request: &ProviderRequest,
        emit: &mut dyn FnMut(StreamItem),
        _: &CancelToken,
    ) -> ProviderOutcome {
        record(
            &self.0.join("dispatch.jsonl"),
            &json!({
                "model": request.model, "round": request.round,
                "messages": request.messages.iter().map(message_json).collect::<Vec<_>>(),
            }),
        );
        assert_eq!(request.round, 1, "the cut must precede dispatch two");
        let call = tool_call();
        emit(StreamItem::ToolCall(call.clone()));
        ProviderOutcome::Completed {
            text: String::new(),
            tool_calls: vec![call],
            finish_reason: "tool_calls".into(),
            usage: json!({}),
        }
    }
}
impl ToolPort for OfflinePorts<'_> {
    fn effect_class(&self, _: &str) -> EffectClass {
        EffectClass::Mutating
    }
    fn run(&self, call: &ToolCall, _: &CancelToken) -> ToolRun {
        record(&self.0.join("effect.jsonl"), &json!({"call": call.id}));
        ToolRun::Finished {
            output: "marker written".into(),
            is_error: false,
        }
    }
}

/// The scratch projector allocates task-log sequences independently of the
/// emitter's sequence; the intent and settlement also occupy ordinary rows.
struct OrderedProjection(AtomicU64, u64);
impl StorageProjection for OrderedProjection {
    fn prepare(
        &self,
        id: &StorageIdentity,
        event: &Value,
        terminal: Option<&Value>,
    ) -> Result<PreparedTurnEvent, StoreError> {
        let seq = self.0.fetch_add(1, Ordering::SeqCst);
        let mut prepared = Projection(AtomicU64::new(0), 0).prepare(id, event, terminal)?;
        prepared.sequence = seq + 1;
        prepared.base_revision = self.1 + seq;
        Ok(prepared)
    }
}

fn bound_store(
    client: Arc<StorageV2Client>,
    identity: &StorageIdentity,
    revision: u64,
) -> StorageV2DurableStore {
    StorageV2DurableStore::new(
        client,
        identity.clone(),
        CheckpointArchive {
            archive_id: ARCHIVE.into(),
            created_at_ms: 100,
        },
        Arc::new(OrderedProjection(AtomicU64::new(0), revision)),
        Arc::new(Requeue::default()),
    )
    .unwrap()
}

fn expected_view(id: &StorageIdentity) -> Value {
    let mut assistant = Message::text(Role::Assistant, "");
    assistant.tool_calls.push(tool_call());
    json!({
        "turnId": id.turn_id, "attemptId": id.attempt_id,
        "status": "completed", "trigger": "checkpoint",
        "implementation": "suspended_working_context",
        "resumeWitness": {"version": 1, "turnId": id.turn_id, "toolRoundCount": 1,
                          "executionDigest": resume_execution_digest("", "")},
        "postMessages": [message_json(&user_message()), message_json(&assistant),
                         message_json(&Message::tool_result("scratch-call", "marker written"))],
    })
}

fn read_frontier(store: &StorageV2DurableStore) -> Result<Value, String> {
    // Read the exact archive, not the newest archive or a barrier value sent
    // back by the parent. The witness alone is not a grant for model entry.
    let archive = store.read_checkpoint(ARCHIVE).map_err(|e| e.message)?;
    let mut view = archive["archive"]["receipt"].clone();
    if !view.is_object() {
        return Err("checkpoint receipt is absent".into());
    }
    view["postMessages"] = archive["postMessages"].clone();
    if view != expected_view(store.identity()) {
        return Err("checkpoint receipt or post view differs from the supported frontier".into());
    }
    Ok(view)
}

fn frame(value: &Value) {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "\n{FRAME}{value}").unwrap();
    stdout.flush().unwrap();
}

struct Cut<'a>(&'a StorageV2DurableStore);
impl EventSink for Cut<'_> {
    fn publish(&self, event: &Value) {
        if event["type"] == "round_end" && event["reason"] == "tool_calls" {
            frame(&json!({"kind": "fault_ready", "archiveId": ARCHIVE,
                         "checkpoint": read_frontier(self.0).unwrap()}));
            loop {
                std::thread::park();
            }
        }
    }
}

#[test]
#[ignore = "child entry: requires a disposable parent-owned authority on stdin"]
fn engine_child() {
    let request: Value =
        serde_json::from_str(&io::stdin().lock().lines().next().unwrap().unwrap()).unwrap();
    let id = StorageIdentity {
        owner_user_id: 7,
        conversation_id: "scratch-recovery".into(),
        turn_id: request["turnId"].as_str().unwrap().into(),
        attempt_id: request["attemptId"].as_str().unwrap().into(),
        task_id: "scratch-task".into(),
    };
    let client = fixture_client(u16::try_from(request["port"].as_u64().unwrap()).unwrap());
    let store = bound_store(client, &id, request["revision"].as_u64().unwrap());
    if request["phase"] == "inspect" {
        match read_frontier(&store) {
            Ok(view) => frame(&json!({"unsupported": NO_AUTHORITY, "archiveId": ARCHIVE,
                                     "verifiedCheckpoint": view})),
            Err(reason) => frame(&json!({"error": reason})),
        }
        return;
    }
    assert_eq!(request["phase"], "initial");
    let offline = OfflinePorts(Path::new(request["root"].as_str().unwrap()));
    let config = SessionConfig {
        task_id: id.task_id,
        turn_id: id.turn_id,
        attempt_id: id.attempt_id,
        owner_user_id: id.owner_user_id,
        primary_model: MODEL.into(),
        fallback_model: None,
        resumed_accepted_model: None,
        auto_turn_retry_max: 0,
    };
    run(
        &config,
        vec![user_message()],
        &Ports {
            provider: &offline,
            tools: &offline,
            store: &store,
            sink: &Cut(&store),
            redactor: &ExactValueRedactor::new([]),
            pause: &|_| panic!("no offline backoff"),
        },
        &CancelToken::new(),
    );
    panic!("engine returned without reaching the fault barrier");
}

struct Process(Child, Receiver<Value>);
impl Process {
    fn launch(mut request: Value, phase: &str) -> Self {
        request["phase"] = json!(phase);
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", CHILD, "--ignored", "--nocapture"])
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Some(raw) = line.strip_prefix(FRAME) {
                    let _ = tx.send(serde_json::from_str(raw).unwrap());
                } else {
                    eprintln!("engine child: {line}");
                }
            }
        });
        let mut process = Self(child, rx);
        writeln!(process.0.stdin.take().unwrap(), "{request}").unwrap();
        process
    }
    fn next(&self) -> Value {
        self.1
            .recv_timeout(WATCHDOG)
            .expect("engine child emitted no evidence")
    }
    fn expect_eof(&self) {
        assert_eq!(
            self.1.recv_timeout(WATCHDOG),
            Err(mpsc::RecvTimeoutError::Disconnected)
        );
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        if matches!(self.0.try_wait(), Ok(None)) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

fn read_records(path: &Path) -> Vec<Value> {
    BufReader::new(std::fs::File::open(path).unwrap())
        .lines()
        .map(|line| serde_json::from_str(&line.unwrap()).unwrap())
        .collect()
}

#[test]
fn real_engine_sigkill_at_completed_view_then_fresh_child_refuses_dispatch() {
    let daemon = Daemon::start();
    let client = daemon.client();
    let pair = command(
        &client,
        "turn.create_pair",
        json!({
            "conversation_id": "scratch-recovery", "user_id": 7, "command_id": "scratch-pair",
            "input_projection": {"content": "synthetic input"}, "config": {"model": MODEL},
            "now": 100, "conversation_defaults": {"allowCreate": true, "title": "scratch recovery", "createdAt": 100, "settings": {}},
        }),
        "scratch-pair",
    );
    let id = StorageIdentity {
        owner_user_id: 7,
        conversation_id: "scratch-recovery".into(),
        turn_id: pair["attempt"]["turnId"].as_str().unwrap().into(),
        attempt_id: pair["attempt"]["attemptId"].as_str().unwrap().into(),
        task_id: "scratch-task".into(),
    };
    command(
        &client,
        "turn.attempt.bind",
        json!({"attempt_id": id.attempt_id, "task_id": id.task_id, "user_id": 7}),
        "scratch-bind",
    );
    let store = bound_store(client, &id, 0);
    let request = json!({"root": daemon.root, "port": daemon.port, "attemptId": id.attempt_id,
                         "turnId": id.turn_id, "revision": store.read_turn().unwrap()["projectionRevision"]});
    let mut initial = Process::launch(request.clone(), "initial");
    let barrier = initial.next();
    assert_eq!(
        barrier,
        json!({"kind": "fault_ready", "archiveId": ARCHIVE, "checkpoint": expected_view(&id)})
    );
    let rows = store.read_task_events(0, 100).unwrap();
    let rows_ref = rows.as_array().unwrap();
    assert_eq!(rows_ref.last().unwrap()["event"]["reason"], "tool_calls");
    let effect_rows: Vec<_> = rows_ref
        .iter()
        .map(|r| &r["event"])
        .filter(|e| e["type"] == "tool_effect")
        .collect();
    assert_eq!(effect_rows.len(), 2);
    assert_eq!(effect_rows[0]["executionEffect"]["state"], "entered");
    assert_eq!(effect_rows[1]["executionEffect"]["state"], "returned");
    let operation = &effect_rows[0]["executionEffect"]["operationId"];
    assert!(operation.as_str().is_some_and(|id| !id.is_empty()));
    assert_eq!(*operation, effect_rows[1]["executionEffect"]["operationId"]);
    assert_eq!(effect_rows[1]["toolContent"], "marker written");
    assert_eq!(initial.0.try_wait().unwrap(), None);
    initial.0.kill().unwrap();
    assert_eq!(initial.0.wait().unwrap().signal(), Some(libc::SIGKILL));

    let head = store.read_turn().unwrap();
    assert_eq!(head["currentAttemptId"], id.attempt_id);
    let mut fresh = Process::launch(request, "inspect");
    assert_ne!(initial.0.id(), fresh.0.id());
    let refusal = fresh.next();
    assert_eq!(
        refusal,
        json!({"unsupported": NO_AUTHORITY, "archiveId": ARCHIVE,
                               "verifiedCheckpoint": barrier["checkpoint"]})
    );
    fresh.expect_eof();
    assert!(fresh.0.wait().unwrap().success());
    assert_eq!(store.read_task_events(0, 100).unwrap(), rows);
    assert_eq!(store.read_turn().unwrap(), head);
    assert_eq!(
        read_records(&daemon.root.join("dispatch.jsonl")),
        vec![json!({"model": MODEL,
        "round": 1, "messages": [message_json(&user_message())]})]
    );
    assert_eq!(
        read_records(&daemon.root.join("effect.jsonl")),
        vec![json!({"call": "scratch-call"})]
    );
    println!(
        "scratch SIGKILL={} -> fresh={}; operation={operation}; {refusal}",
        initial.0.id(),
        fresh.0.id()
    );
}
