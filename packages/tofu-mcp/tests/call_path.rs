//! `tests/mcp_protocol/test_hope_mcp_queue_policy_boundary.py` and the bridge
//! call-path cases, mapped onto the Rust bridge with an injected session: the
//! owner Hope queue guard refuses before any send and fails closed on the
//! contract default; deadlines, the degraded fast-fail, read-only replay
//! after a dead transport, cancellation and disabled tools follow the oracle.

use async_trait::async_trait;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tofu_mcp::bridge::{Bridge, CallOptions, Connected, Connector, Hooks, Session};
use tofu_mcp::errors::CallError;
use tofu_mcp::result::CallOutput;
use tokio_util::sync::CancellationToken;

const M17: &str = "root.hldy_training_cluster.your-user.h910c_m17_public";
const H800: &str = "root.hldy_training_cluster.your-user.h800_elastic";
const OTHER_BAN: &str = "root.hldy_training_cluster.your-user.h910c_training_data";

fn entry(id: &str, queue: &str) -> Value {
    json!({"id": id, "queue": queue, "since": "2026-11-13T11:01+08:00",
           "reason": "queue admin asked for release; owner decision"})
}

fn policy(entries: &[Value]) -> String {
    json!({"version": 1, "off_limits": entries}).to_string()
}

/// Every request the fake server received: (tool, arguments, request meta).
type Sent = Arc<Mutex<Vec<(String, Value, Option<Map<String, Value>>)>>>;

#[derive(Clone, Copy, PartialEq)]
enum Behaviour {
    Answer,
    Hang,
    DieOnce,
    Die,
}

struct FakeSession {
    sent: Sent,
    behaviour: Arc<Mutex<Behaviour>>,
}

#[async_trait]
impl Session for FakeSession {
    async fn call_tool(
        &self,
        name: &str,
        arguments: Map<String, Value>,
        meta: Option<Map<String, Value>>,
        timeout: Option<Duration>,
    ) -> Result<Value, CallError> {
        self.sent
            .lock()
            .unwrap()
            .push((name.into(), Value::Object(arguments), meta));
        let behaviour = *self.behaviour.lock().unwrap();
        match behaviour {
            Behaviour::Answer => Ok(json!({"content": [{"type": "text", "text": "SENT"}]})),
            Behaviour::Hang => {
                match timeout {
                    Some(limit) => tokio::time::sleep(limit).await,
                    None => std::future::pending::<()>().await,
                }
                Err(CallError::Timeout("timed out".into()))
            }
            Behaviour::DieOnce => {
                *self.behaviour.lock().unwrap() = Behaviour::Answer;
                Err(CallError::Transport("Connection closed".into()))
            }
            Behaviour::Die => Err(CallError::Transport("Connection closed".into())),
        }
    }
    async fn close(&self) {}
}

struct FakeConnector {
    sent: Sent,
    behaviour: Arc<Mutex<Behaviour>>,
    connects: AtomicUsize,
}

fn tool(name: &str, read_only: bool) -> Value {
    let mut tool = json!({"name": name, "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}});
    if read_only {
        tool["annotations"] = json!({"readOnlyHint": true});
    }
    tool
}

#[async_trait]
impl Connector for FakeConnector {
    async fn connect(
        &self,
        _name: &str,
        _config: &Value,
        _owner: Option<u64>,
    ) -> Result<Connected, String> {
        self.connects.fetch_add(1, Ordering::SeqCst);
        Ok(Connected {
            session: Arc::new(FakeSession {
                sent: self.sent.clone(),
                behaviour: self.behaviour.clone(),
            }),
            tools: ["submit_job", "confirm_rerun", "catalog_write", "write"]
                .iter()
                .map(|n| tool(n, false))
                .chain(["get_job"].iter().map(|n| tool(n, true)))
                .collect(),
            catalog_version: String::new(),
            server_name: "fake".into(),
            server_version: "1".into(),
            protocol_version: "2026-07-28".into(),
        })
    }
}

#[derive(Default)]
struct RecordingHooks {
    policy: PathBuf,
    audit: Mutex<Vec<(String, Map<String, Value>)>>,
}

impl Hooks for RecordingHooks {
    fn audit(&self, event: &str, fields: Map<String, Value>) {
        self.audit.lock().unwrap().push((event.into(), fields));
    }
    fn policy_path(&self, _owner: u64) -> String {
        self.policy.to_string_lossy().into_owned()
    }
    fn reviewed_read_only(&self, _server: &str, _config: &Value) -> BTreeSet<String> {
        BTreeSet::new()
    }
}

struct Scene {
    dir: tempfile::TempDir,
    bridge: Bridge,
    hooks: Arc<RecordingHooks>,
    connector: Arc<FakeConnector>,
}

impl Scene {
    async fn new(policy_text: Option<&str>, server: &str, command: &str) -> Scene {
        let dir = tempfile::tempdir().unwrap();
        let policy = dir.path().join("router").join("HOPE_QUEUE_POLICY.json");
        if let Some(text) = policy_text {
            std::fs::create_dir_all(policy.parent().unwrap()).unwrap();
            std::fs::write(&policy, text).unwrap();
        }
        let hooks = Arc::new(RecordingHooks {
            policy,
            ..Default::default()
        });
        let connector = Arc::new(FakeConnector {
            sent: Arc::default(),
            behaviour: Arc::new(Mutex::new(Behaviour::Answer)),
            connects: AtomicUsize::new(0),
        });
        let bridge = Bridge::new(Some(1), connector.clone(), hooks.clone());
        bridge
            .connect_server(server, json!({"command": command, "timeout": 60}))
            .await
            .unwrap();
        Scene {
            dir,
            bridge,
            hooks,
            connector,
        }
    }

    fn path(&self) -> &Path {
        self.dir.path()
    }

    fn workdir(&self, queue: &str, name: &str) -> String {
        let workdir = self.path().join(name);
        std::fs::create_dir_all(&workdir).unwrap();
        std::fs::write(
            workdir.join("train.hope"),
            format!("[resource]\nqueue = {queue}\n"),
        )
        .unwrap();
        workdir.to_string_lossy().into_owned()
    }

    fn sent(&self) -> usize {
        self.connector.sent.lock().unwrap().len()
    }

    async fn call(&self, name: &str, arguments: Value) -> Result<CallOutput, CallError> {
        self.bridge
            .call_tool(name, arguments, CallOptions::default())
            .await
    }
}

fn refusal(output: Result<CallOutput, CallError>) -> Map<String, Value> {
    let CallOutput::Result(result) = output.expect("a refusal is a result") else {
        panic!("expected a typed result")
    };
    assert!(result.is_error);
    let document: Value = serde_json::from_str(&result.text).unwrap();
    assert_eq!(Some(&document), result.structured_json.as_ref());
    assert_eq!(
        (&document["ok"], &document["submitted"]),
        (&json!(false), &json!(false))
    );
    document.as_object().unwrap().clone()
}

fn sent_ok(output: Result<CallOutput, CallError>) {
    assert_eq!(output.expect("the call reaches the server").text(), "SENT");
}

#[tokio::test]
async fn mcp_submit_to_m17_is_refused_before_the_call() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let path = scene.path().to_string_lossy().into_owned();
    let out = refusal(
        scene
            .call(
                "mcp__hope__submit_job",
                json!({"workdir": path, "usergroup": "g", "queue": M17}),
            )
            .await,
    );
    assert_eq!(
        (out["error_code"].as_str(), out["stage"].as_str()),
        (Some("queue_off_limits"), Some("queue_policy"))
    );
    assert_eq!(out["policy_entry"]["id"], "m17");
    assert_eq!(out["policy_source"], "file");
    assert_eq!(out["queue"], M17);
    let error = out["error"].as_str().unwrap();
    assert!(error.contains("m17") && error.contains(out["policy_path"].as_str().unwrap()));
    assert_eq!(scene.sent(), 0);
}

#[tokio::test]
async fn m17_in_hope_body_of_workdir_is_refused() {
    for conf_name in [None, Some("train.hope")] {
        let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
        let mut arguments = json!({"workdir": scene.workdir(M17, "job"), "usergroup": "g"});
        if let Some(conf) = conf_name {
            arguments["conf_name"] = json!(conf);
        }
        assert_eq!(
            refusal(scene.call("mcp__hope__submit_job", arguments).await)["error_code"],
            "queue_off_limits"
        );
        assert_eq!(scene.sent(), 0);
    }
}

#[tokio::test]
async fn m17_override_through_arguments_or_extra_is_refused() {
    for extra in [
        json!({"arguments": format!("-Dafo.app.queue={M17} --foo")}),
        json!({"extra": {"queue": M17}}),
    ] {
        let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
        let mut arguments = json!({"workdir": scene.workdir(H800, "job"), "usergroup": "g"});
        for (key, value) in extra.as_object().unwrap() {
            arguments[key] = value.clone();
        }
        assert_eq!(
            refusal(scene.call("mcp__hope__submit_job", arguments).await)["error_code"],
            "queue_off_limits"
        );
        assert_eq!(scene.sent(), 0);
    }
}

#[tokio::test]
async fn rerun_and_clone_with_m17_body_are_refused() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let body = format!("queue = {M17}\n");
    refusal(
        scene
            .call(
                "mcp__hope__confirm_rerun",
                json!({"confirmation_token": "t", "run_id": "r", "spec_job_id": "s",
                                                     "job_name": "j", "hope_conf": body}),
            )
            .await,
    );
    refusal(
        scene
            .call(
                "mcp__hope__catalog_write",
                json!({"operation": "job_clone", "path_params": {"jobId": "1"},
                                                     "body": {"hopeConf": body}, "dry_run": false}),
            )
            .await,
    );
    assert_eq!(scene.sent(), 0);
}

#[tokio::test]
async fn codelab_create_on_m17_is_refused_and_other_queues_pass() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let call = |body: Value| {
        scene.call(
            "mcp__hope__catalog_write",
            json!({"operation": "codelab_create", "body": body, "dry_run": false}),
        )
    };
    assert_eq!(
        refusal(call(json!({"queue": M17})).await)["error_code"],
        "queue_off_limits"
    );
    sent_ok(call(json!({"queue": H800})).await);
    sent_ok(call(json!({"name": "box"})).await);
    assert_eq!(scene.sent(), 2);
}

#[tokio::test]
async fn refusal_is_audited_with_queue_and_caller() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let mut meta = Map::new();
    meta.insert("tofu/project".into(), json!("/projects/knowledge"));
    let path = scene.path().to_string_lossy().into_owned();
    let options = CallOptions {
        meta: Some(meta),
        ..Default::default()
    };
    refusal(
        scene
            .bridge
            .call_tool(
                "mcp__hope__submit_job",
                json!({"workdir": path, "usergroup": "g", "queue": M17}),
                options,
            )
            .await,
    );
    let audit = scene.hooks.audit.lock().unwrap();
    let records: Vec<_> = audit
        .iter()
        .filter(|(event, _)| event == "mcp_queue_policy_refusal")
        .collect();
    assert_eq!(records.len(), 1);
    let fields = &records[0].1;
    assert_eq!(
        (&fields["queue"], &fields["owner_user_id"]),
        (&json!(M17), &json!(1))
    );
    assert_eq!(
        (&fields["project"], &fields["tool"]),
        (&json!("/projects/knowledge"), &json!("submit_job"))
    );
}

#[tokio::test]
async fn h800_elastic_submission_passes_unchanged() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let arguments = json!({"workdir": scene.workdir(H800, "job"), "usergroup": "g", "queue": H800});
    sent_ok(scene.call("mcp__hope__submit_job", arguments.clone()).await);
    sent_ok(
        scene
            .call("mcp__hope__confirm_rerun", json!({"confirmation_token": "t", "run_id": "r", "spec_job_id": "s",
                                                     "job_name": "j", "hope_conf": format!("queue = {H800}\n")}))
            .await,
    );
    sent_ok(
        scene
            .call("mcp__hope__get_job", json!({"hope_run_id": "x"}))
            .await,
    );
    assert_eq!(scene.sent(), 3);
    assert_eq!(
        scene.connector.sent.lock().unwrap()[0].1,
        arguments,
        "arguments reach the server unchanged"
    );
}

#[tokio::test]
async fn missing_or_invalid_policy_fails_closed_on_m17_like_the_shims() {
    for text in [
        None,
        Some("{broken"),
        Some(r#"{"version":1,"off_limits":[{"id":"x"}]}"#),
    ] {
        let scene = Scene::new(text, "hope", "hope-mcp").await;
        let path = scene.path().to_string_lossy().into_owned();
        let out = refusal(
            scene
                .call(
                    "mcp__hope__submit_job",
                    json!({"workdir": path, "usergroup": "g", "queue": M17}),
                )
                .await,
        );
        assert_eq!(out["error_code"], "queue_off_limits");
        assert_eq!(
            (&out["policy_source"], &out["policy_entry"]["id"]),
            (&json!("built-in default"), &json!("m17"))
        );
        sent_ok(
            scene
                .call(
                    "mcp__hope__submit_job",
                    json!({"workdir": path, "usergroup": "g", "queue": H800}),
                )
                .await,
        );
        assert_eq!(scene.sent(), 1);
    }
}

#[tokio::test]
async fn policy_file_is_the_single_source() {
    let scene = Scene::new(Some(&policy(&[entry("td", OTHER_BAN)])), "hope", "hope-mcp").await;
    let path = scene.path().to_string_lossy().into_owned();
    refusal(
        scene
            .call(
                "mcp__hope__submit_job",
                json!({"workdir": path, "usergroup": "g", "queue": OTHER_BAN}),
            )
            .await,
    );
    sent_ok(
        scene
            .call(
                "mcp__hope__submit_job",
                json!({"workdir": path, "usergroup": "g", "queue": M17}),
            )
            .await,
    );
}

#[tokio::test]
async fn submission_with_no_resolvable_queue_is_unresolved() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "hope", "hope-mcp").await;
    let empty = scene.path().join("empty");
    std::fs::create_dir_all(&empty).unwrap();
    let out = refusal(
        scene
            .call(
                "mcp__hope__submit_job",
                json!({"workdir": empty, "usergroup": "g"}),
            )
            .await,
    );
    assert_eq!(out["error_code"], "queue_policy_unresolved");
    assert_eq!(scene.sent(), 0);
}

#[tokio::test]
async fn server_launched_as_hope_mcp_under_another_name_is_guarded() {
    let scene = Scene::new(
        Some(&policy(&[entry("m17", M17)])),
        "jobs",
        "/opt/bin/hope-mcp",
    )
    .await;
    let path = scene.path().to_string_lossy().into_owned();
    refusal(
        scene
            .call(
                "mcp__jobs__submit_job",
                json!({"workdir": path, "usergroup": "g", "queue": M17}),
            )
            .await,
    );
    assert_eq!(scene.sent(), 0);
}

#[tokio::test]
async fn unrelated_server_tool_named_submit_job_is_not_inspected() {
    let scene = Scene::new(Some(&policy(&[entry("m17", M17)])), "other", "other-mcp").await;
    sent_ok(
        scene
            .call("mcp__other__submit_job", json!({"queue": M17}))
            .await,
    );
}

// ── the rest of the call path ────────────────────────────────────────────────

#[tokio::test]
async fn timeouts_count_toward_the_degraded_fast_fail_and_a_success_resets_it() {
    let scene = Scene::new(None, "other", "other-mcp").await;
    *scene.connector.behaviour.lock().unwrap() = Behaviour::Hang;
    let timed = || CallOptions {
        timeout_override: Some(1),
        ..Default::default()
    };
    for _ in 0..3 {
        let error = scene
            .bridge
            .call_tool("mcp__other__write", json!({}), timed())
            .await
            .unwrap_err();
        assert!(error.is_call_timeout());
    }
    let degraded = scene
        .bridge
        .call_tool("mcp__other__write", json!({}), timed())
        .await
        .unwrap();
    assert!(degraded.text().starts_with("MCP Error: server 'other' is degraded — 3 consecutive call timeouts (configured call budget: 1s)"));
    assert_eq!(scene.sent(), 3, "the degraded call was not attempted");
    let audit = scene.hooks.audit.lock().unwrap();
    assert_eq!(
        audit
            .iter()
            .filter(|(e, _)| e == "mcp_call_timeout")
            .count(),
        3
    );
    assert_eq!(
        audit
            .iter()
            .filter(|(e, _)| e == "mcp_server_degraded")
            .count(),
        1
    );
}

#[tokio::test]
async fn a_dead_transport_replays_an_unchanged_read_once_and_never_a_write() {
    let scene = Scene::new(None, "other", "other-mcp").await;
    *scene.connector.behaviour.lock().unwrap() = Behaviour::DieOnce;
    sent_ok(scene.call("mcp__other__get_job", json!({})).await);
    assert_eq!(scene.sent(), 2);
    assert_eq!(scene.connector.connects.load(Ordering::SeqCst), 2);
    *scene.connector.behaviour.lock().unwrap() = Behaviour::DieOnce;
    let error = scene
        .call("mcp__other__write", json!({}))
        .await
        .unwrap_err();
    assert!(error.is_transport_dead());
    assert_eq!(scene.sent(), 3, "a write is never repeated");
    assert_eq!(
        scene.connector.connects.load(Ordering::SeqCst),
        3,
        "its dead transport's catalog is refreshed"
    );
    *scene.connector.behaviour.lock().unwrap() = Behaviour::Die;
    let trusted = CallOptions {
        retry_on_disconnect: Some(false),
        ..Default::default()
    };
    assert!(scene
        .bridge
        .call_tool("mcp__other__get_job", json!({}), trusted)
        .await
        .unwrap_err()
        .is_transport_dead());
}

#[tokio::test]
async fn cancellation_stops_a_waiting_call_without_a_health_failure() {
    let scene = Scene::new(None, "other", "other-mcp").await;
    *scene.connector.behaviour.lock().unwrap() = Behaviour::Hang;
    let cancel = CancellationToken::new();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(100)).await;
        trigger.cancel();
    });
    let options = CallOptions {
        cancel: Some(cancel.clone()),
        ..Default::default()
    };
    let error = scene
        .bridge
        .call_tool("mcp__other__write", json!({}), options)
        .await
        .unwrap_err();
    assert_eq!(error.kind(), "cancelled");
    let already = CallOptions {
        cancel: Some(cancel),
        ..Default::default()
    };
    assert_eq!(
        scene
            .bridge
            .call_tool("mcp__other__write", json!({}), already)
            .await
            .unwrap_err()
            .kind(),
        "cancelled"
    );
    assert!(scene.hooks.audit.lock().unwrap().is_empty());
}

#[tokio::test]
async fn disabled_tools_bad_names_and_coercion() {
    let scene = Scene::new(None, "other", "other-mcp").await;
    scene.bridge.set_disabled_tools("other", &["write".into()]);
    let error = scene
        .call("mcp__other__write", json!({}))
        .await
        .unwrap_err();
    assert_eq!(error.kind(), "invalid");
    assert!(error.message().contains("disabled by user"));
    assert_eq!(
        scene.call("nope", json!({})).await.unwrap_err().kind(),
        "invalid"
    );
    assert_eq!(
        scene
            .call("mcp__ghost__x", json!({}))
            .await
            .unwrap_err()
            .message(),
        "MCP server not connected: ghost"
    );
    sent_ok(scene.call("mcp__other__get_job", json!({"n": "7"})).await);
    assert_eq!(scene.connector.sent.lock().unwrap()[0].1, json!({"n": 7}));
    assert_eq!(
        scene.bridge.get_openai_tool_defs().len(),
        4,
        "a disabled tool leaves the model-visible catalog"
    );
}

#[tokio::test]
async fn a_parked_server_wakes_on_the_next_call() {
    let scene = Scene::new(None, "other", "other-mcp").await;
    scene.bridge.park("other").await;
    sent_ok(scene.call("mcp__other__get_job", json!({})).await);
    assert_eq!(scene.connector.connects.load(Ordering::SeqCst), 2);
}
