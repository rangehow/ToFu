//! The bridge lifecycle (`src/bridge/lifecycle.rs`) against injected sessions:
//! the liveness verdict of `tests/mcp_protocol/test_mcp_liveness_probe.py`,
//! idle parking (`test_mcp_idle_parking.py`), first-connect recovery and the
//! breaker (`test_mcp_initial_connect_recovery.py`), reconnects re-reading the
//! catalog, `tools/list_changed`, shutdown ordering, and (with the recorded
//! replay server and rmcp) the handshake deadline scope. Time-driven cases use
//! `Bridge::advance_clock` and `Bridge::sweep_once`, never a shortened interval.

use async_trait::async_trait;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tofu_mcp::bridge::{Bridge, CallOptions, Connected, Connector, Hooks, Session};
use tofu_mcp::callback::BindingCalls;
use tofu_mcp::errors::CallError;
use tokio::sync::mpsc;

#[derive(Clone, Copy, PartialEq, Debug)]
enum Answer {
    Ok,
    NotFound,
    InvalidParams,
    Dead,
    Hang,
}

#[derive(Clone)]
struct Peer {
    sdk: u8,
    protocol: &'static str,
    offered: Vec<&'static str>,
    probes: Vec<(&'static str, Answer)>,
    call: Answer,
}

impl Default for Peer {
    fn default() -> Self {
        Peer {
            sdk: 1,
            protocol: "2025-06-18",
            offered: vec!["send_ping", "list_tools"],
            probes: Vec::new(),
            call: Answer::Ok,
        }
    }
}

impl Peer {
    fn answer(&self, method: &str) -> Answer {
        self.probes
            .iter()
            .find(|(m, _)| *m == method)
            .map_or(Answer::Ok, |(_, a)| *a)
    }
}

/// What every session saw: (session id, event).
type Events = Arc<Mutex<Vec<(usize, String)>>>;

async fn respond(answer: Answer) -> Result<(), CallError> {
    match answer {
        Answer::Ok => Ok(()),
        Answer::NotFound => Err(CallError::Peer {
            code: -32601,
            message: "Method not found".into(),
        }),
        Answer::InvalidParams => Err(CallError::Peer {
            code: -32602,
            message: "Invalid params".into(),
        }),
        Answer::Dead => Err(CallError::Transport("connection closed".into())),
        Answer::Hang => {
            std::future::pending::<()>().await;
            Ok(())
        }
    }
}

struct FakeSession {
    id: usize,
    peer: Peer,
    events: Events,
    catalog: Arc<Mutex<Vec<Value>>>,
    notifications: Mutex<Option<mpsc::UnboundedReceiver<String>>>,
    close_hangs: bool,
}

#[async_trait]
impl Session for FakeSession {
    async fn call_tool(
        &self,
        name: &str,
        _arguments: Map<String, Value>,
        _meta: Option<Map<String, Value>>,
        _timeout: Option<Duration>,
    ) -> Result<Value, CallError> {
        self.events
            .lock()
            .unwrap()
            .push((self.id, format!("call:{name}")));
        respond(self.peer.call).await?;
        Ok(json!({"content": [{"type": "text", "text": format!("ok from {}", self.id)}]}))
    }
    async fn close(&self) {
        self.events.lock().unwrap().push((self.id, "close".into()));
        if self.close_hangs {
            std::future::pending::<()>().await;
        }
    }
    fn sdk_generation(&self) -> u8 {
        self.peer.sdk
    }
    fn probe_methods(&self) -> Vec<&'static str> {
        self.peer.offered.clone()
    }
    async fn probe(&self, method: &str) -> Result<(), CallError> {
        self.events
            .lock()
            .unwrap()
            .push((self.id, format!("probe:{method}")));
        respond(self.peer.answer(method)).await
    }
    async fn list_tools(&self) -> Result<(Vec<Value>, String), CallError> {
        Ok((self.catalog.lock().unwrap().clone(), String::new()))
    }
    fn tools_list_changed(&self) -> bool {
        true
    }
    fn take_notifications(&self) -> Option<mpsc::UnboundedReceiver<String>> {
        self.notifications.lock().unwrap().take()
    }
}

#[derive(Default)]
struct FakeConnector {
    peer: Mutex<Peer>,
    events: Events,
    catalog: Arc<Mutex<Vec<Value>>>,
    connects: AtomicUsize,
    fail: Mutex<BTreeSet<String>>,
    configs: Mutex<Vec<(String, Value)>>,
    senders: Mutex<Vec<mpsc::UnboundedSender<String>>>,
    gate: Mutex<Option<Arc<tokio::sync::Notify>>>,
    close_hangs: Mutex<bool>,
}

#[async_trait]
impl Connector for FakeConnector {
    async fn connect(
        &self,
        name: &str,
        config: &Value,
        _owner: Option<u64>,
    ) -> Result<Connected, String> {
        let id = self.connects.fetch_add(1, Ordering::SeqCst) + 1;
        self.configs
            .lock()
            .unwrap()
            .push((name.into(), config.clone()));
        let gate = self.gate.lock().unwrap().clone();
        if let Some(gate) = gate {
            gate.notified().await;
        }
        if self.fail.lock().unwrap().contains(name) {
            return Err(format!(
                "MCP server '{name}': launcher exited before the handshake"
            ));
        }
        let peer = self.peer.lock().unwrap().clone();
        let (sender, receiver) = mpsc::unbounded_channel();
        self.senders.lock().unwrap().push(sender);
        Ok(Connected {
            session: Arc::new(FakeSession {
                id,
                peer: peer.clone(),
                events: self.events.clone(),
                catalog: self.catalog.clone(),
                notifications: Mutex::new(Some(receiver)),
                close_hangs: *self.close_hangs.lock().unwrap(),
            }),
            tools: self.catalog.lock().unwrap().clone(),
            catalog_version: String::new(),
            server_name: "fake".into(),
            server_version: "1".into(),
            protocol_version: peer.protocol.into(),
        })
    }
}

#[derive(Default)]
struct RecordingHooks {
    audit: Mutex<Vec<(String, Map<String, Value>)>>,
    logs: Mutex<Vec<(String, String)>>,
}

impl Hooks for RecordingHooks {
    fn audit(&self, event: &str, fields: Map<String, Value>) {
        self.audit.lock().unwrap().push((event.into(), fields));
    }
    fn log(&self, level: &str, message: &str) {
        self.logs
            .lock()
            .unwrap()
            .push((level.into(), message.into()));
    }
}

/// The owner's stored rows (`mcp.config_rows`); everything else is unanswered.
#[derive(Default)]
struct Rows(Mutex<Value>);

#[async_trait]
impl BindingCalls for Rows {
    async fn ask(&self, method: &str, _params: Value) -> Result<Value, CallError> {
        match method {
            "mcp.config_rows" => Ok(self.0.lock().unwrap().clone()),
            "mcp.catalog_entry" => Ok(json!({})),
            "mcp.registry_catalog" => Ok(json!([])),
            _ => Err(CallError::Unavailable(format!("no handler for {method}"))),
        }
    }
}

fn tool(name: &str, properties: Value) -> Value {
    json!({"name": name, "inputSchema": {"type": "object", "properties": properties}})
}

struct Scene {
    bridge: Arc<Bridge>,
    connector: Arc<FakeConnector>,
    hooks: Arc<RecordingHooks>,
    rows: Arc<Rows>,
}

impl Scene {
    fn new(peer: Peer) -> Scene {
        Scene::build(peer, false)
    }

    /// `shared` bridges run background credential work like the host's.
    fn build(peer: Peer, shared: bool) -> Scene {
        // The oracle's defaults, whatever the developer's shell exports.
        std::env::set_var("TOFU_MCP_STDIO_IDLE_SECONDS", "300");
        std::env::set_var("TOFU_MCP_KEEPALIVE_INTERVAL", "45");
        let connector = Arc::new(FakeConnector::default());
        *connector.peer.lock().unwrap() = peer;
        *connector.catalog.lock().unwrap() = vec![
            tool("echo", json!({"text": {"type": "string"}})),
            tool("cred_check", json!({})),
        ];
        let hooks = Arc::new(RecordingHooks::default());
        let rows = Arc::new(Rows::default());
        let bridge =
            Bridge::new(Some(7), connector.clone(), hooks.clone()).with_binding(rows.clone());
        let bridge = if shared {
            bridge.shared()
        } else {
            Arc::new(bridge)
        };
        Scene {
            bridge,
            connector,
            hooks,
            rows,
        }
    }

    async fn connect(&self, config: Value) {
        self.bridge.connect_server("srv", config).await.unwrap();
    }

    fn events(&self) -> Vec<(usize, String)> {
        self.connector.events.lock().unwrap().clone()
    }

    fn named(&self, prefix: &str) -> Vec<String> {
        self.events()
            .into_iter()
            .map(|(_, e)| e)
            .filter(|e| e.starts_with(prefix))
            .collect()
    }

    fn connects(&self) -> usize {
        self.connector.connects.load(Ordering::SeqCst)
    }

    async fn sweeps(&self, passes: usize) {
        for _ in 0..passes {
            self.bridge.sweep_once().await;
        }
    }

    fn state(&self) -> Value {
        self.bridge.lifecycle_state("srv")
    }
}

fn stdio() -> Value {
    json!({"command": "fake-mcp", "transport": "stdio"})
}

fn not_found_ping() -> Peer {
    Peer {
        probes: vec![("send_ping", Answer::NotFound)],
        ..Peer::default()
    }
}

// ── liveness (test_mcp_liveness_probe.py) ───────────────────────────────────

#[tokio::test]
async fn liveness_ping_method_not_found_never_reconnects() {
    let scene = Scene::new(not_found_ping());
    scene.connect(stdio()).await;
    scene.sweeps(5).await;
    assert!(!scene.named("probe:").is_empty());
    assert_eq!(
        scene.connects(),
        1,
        "a -32601 ping answer reconnected a live peer"
    );
}

#[tokio::test]
async fn liveness_falls_back_to_a_probe_the_peer_implements() {
    let scene = Scene::new(not_found_ping());
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(
        scene.named("probe:"),
        ["probe:send_ping", "probe:list_tools"]
    );
    assert_eq!(scene.state()["probe_method"], "list_tools");
}

#[tokio::test]
async fn liveness_memo_stops_repaying_the_fallback_walk() {
    let scene = Scene::new(not_found_ping());
    scene.connect(stdio()).await;
    scene.sweeps(4).await;
    let probes = scene.named("probe:");
    assert_eq!(probes.iter().filter(|p| *p == "probe:send_ping").count(), 1);
    assert!(probes.iter().filter(|p| *p == "probe:list_tools").count() > 1);
}

#[tokio::test]
async fn liveness_prefers_server_discover() {
    let scene = Scene::new(Peer {
        offered: vec!["discover", "send_ping", "list_tools"],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.named("probe:"), ["probe:discover"]);
    assert_eq!(scene.connects(), 1);
}

#[tokio::test]
async fn liveness_modern_session_never_pings() {
    let scene = Scene::new(Peer {
        protocol: "2026-07-28",
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.named("probe:"), ["probe:list_tools"]);
    assert_eq!(scene.state()["probe_method"], "list_tools");
}

#[tokio::test]
async fn liveness_sdk_v2_client_never_pings_a_legacy_peer() {
    let scene = Scene::new(Peer {
        sdk: 2,
        protocol: "2025-11-25",
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.named("probe:"), ["probe:list_tools"]);
}

#[tokio::test]
async fn liveness_skips_a_probe_that_needs_arguments() {
    // `send_discover(version)` is not zero-argument, so the session does not offer it.
    let scene = Scene::new(Peer {
        offered: vec!["send_ping", "list_tools"],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert!(!scene
        .named("probe:")
        .contains(&"probe:send_discover".to_string()));
    assert_eq!(scene.connects(), 1);
    assert_eq!(scene.state()["probe_method"], "send_ping");
}

#[tokio::test]
async fn liveness_healthy_ping_short_circuits() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    scene.sweeps(2).await;
    assert_eq!(
        scene.named("probe:"),
        ["probe:send_ping", "probe:send_ping"]
    );
    assert_eq!(scene.connects(), 1);
}

#[tokio::test]
async fn liveness_other_protocol_error_is_alive_without_moving_on() {
    let scene = Scene::new(Peer {
        probes: vec![("send_ping", Answer::InvalidParams)],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.named("probe:"), ["probe:send_ping"]);
    assert_eq!(scene.connects(), 1);
}

#[tokio::test]
async fn liveness_dead_pipe_triggers_reconnect() {
    let scene = Scene::new(Peer {
        probes: vec![("send_ping", Answer::Dead), ("list_tools", Answer::Dead)],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 2, "a dead transport was not reconnected");
    assert!(scene.events().contains(&(1, "close".into())));
}

#[tokio::test]
async fn liveness_peer_that_never_answers_triggers_reconnect() {
    // The oracle bounds each probe by MCP_PING_TIMEOUT (env TOFU_MCP_PING_TIMEOUT).
    std::env::set_var("TOFU_MCP_PING_TIMEOUT", "1");
    let scene = Scene::new(Peer {
        probes: vec![("send_ping", Answer::Hang)],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 2, "a hung peer was treated as alive");
}

#[tokio::test]
async fn liveness_credential_probe_runs_when_ping_answers_method_not_found() {
    let scene = Scene::new(not_found_ping());
    scene
        .connect(json!({"command": "fake-mcp", "health_probe": {"tool": "cred_check", "args": {}}}))
        .await;
    scene.sweeps(1).await;
    assert_eq!(scene.named("call:"), ["call:cred_check"]);
}

#[tokio::test]
async fn liveness_credential_probe_skipped_on_a_dead_transport() {
    let scene = Scene::new(Peer {
        offered: vec!["send_ping"],
        probes: vec![("send_ping", Answer::Dead)],
        ..Peer::default()
    });
    scene
        .connect(json!({"command": "fake-mcp", "health_probe": {"tool": "cred_check", "args": {}}}))
        .await;
    scene.sweeps(1).await;
    assert!(
        !scene.events().contains(&(1, "call:cred_check".into())),
        "a credential probe ran through a dead transport"
    );
}

#[tokio::test]
async fn liveness_no_usable_probe_reads_alive() {
    let scene = Scene::new(Peer {
        offered: vec![],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.sweeps(2).await;
    assert!(scene.named("probe:").is_empty());
    assert_eq!(scene.connects(), 1);
}

// ── idle parking (test_mcp_idle_parking.py, test_mcp_v2_protocol.py) ────────

#[tokio::test]
async fn idle_parking_releases_transport_but_preserves_catalog() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    scene.bridge.advance_clock(301.0);
    assert!(scene.bridge.park_idle_stdio_server("srv").await);
    assert!(scene.events().contains(&(1, "close".into())));
    assert_eq!(scene.state()["parked"], true);
    assert_eq!(scene.state()["connected"], false);
    assert!(scene.bridge.get_tool_info("mcp__srv__echo").is_some());
    let defs: Vec<Value> = scene
        .bridge
        .get_openai_tool_defs()
        .into_iter()
        .map(|d| d["function"]["name"].clone())
        .collect();
    assert_eq!(
        defs,
        [json!("mcp__srv__cred_check"), json!("mcp__srv__echo")]
    );
    assert_eq!(scene.bridge.list_servers()[0]["parked"], true);
    let audit = scene.hooks.audit.lock().unwrap();
    let (event, fields) = audit.last().unwrap();
    assert_eq!(event, "mcp_stdio_parked");
    assert_eq!(fields["idle_budget_seconds"], 300);
}

#[tokio::test]
async fn idle_parking_skips_active_young_and_remote_servers() {
    let scene = Scene::new(Peer {
        call: Answer::Hang,
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.bridge.advance_clock(301.0);
    let call = scene
        .bridge
        .call_tool("mcp__srv__echo", json!({}), CallOptions::default());
    tokio::select! {
        _ = call => panic!("the hanging call answered"),
        parked = async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            scene.bridge.park_idle_stdio_server("srv").await
        } => assert!(!parked, "an active call was parked"),
    }
    assert_eq!(scene.state()["active_calls"], 0);
    // The call that just ended is recent activity.
    assert!(!scene.bridge.park_idle_stdio_server("srv").await);

    let remote = Scene::new(Peer::default());
    remote
        .connect(json!({"transport": "streamable-http", "url": "https://example.invalid/mcp"}))
        .await;
    remote.bridge.advance_clock(301.0);
    assert!(!remote.bridge.park_idle_stdio_server("srv").await);
    assert_eq!(remote.state()["connected"], true);
}

#[tokio::test]
async fn idle_parked_catalog_wakes_on_the_next_call() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    scene.bridge.advance_clock(301.0);
    assert!(scene.bridge.park_idle_stdio_server("srv").await);
    let output = scene
        .bridge
        .call_tool("mcp__srv__echo", json!({}), CallOptions::default())
        .await
        .unwrap();
    assert_eq!(output.text(), "ok from 2");
    assert_eq!(scene.connects(), 2);
    assert_eq!(scene.state()["parked"], false);
    assert_eq!(scene.bridge.list_servers()[0]["parked"], false);
}

#[tokio::test]
async fn idle_call_activity_is_released_on_failure() {
    let scene = Scene::new(Peer {
        call: Answer::InvalidParams,
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    scene.bridge.advance_clock(301.0);
    let error = scene
        .bridge
        .call_tool("mcp__srv__echo", json!({}), CallOptions::default())
        .await
        .unwrap_err();
    assert!(error.is_peer_answered());
    assert_eq!(scene.state()["active_calls"], 0);
    assert!(
        !scene.bridge.park_idle_stdio_server("srv").await,
        "a failed call did not record activity"
    );
}

#[tokio::test]
async fn idle_maintenance_parks_before_liveness_work() {
    let scene = Scene::new(Peer::default());
    scene
        .connect(json!({"command": "fake-mcp", "health_probe": {"tool": "cred_check", "args": {}}}))
        .await;
    scene.bridge.advance_clock(301.0);
    scene.sweeps(1).await;
    assert_eq!(scene.state()["parked"], true);
    assert!(
        scene.named("probe:").is_empty(),
        "a parked server was probed"
    );
    assert!(
        scene.named("call:").is_empty(),
        "a parked server ran credential work"
    );
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 1, "the sweep woke a parked server");
}

#[tokio::test(flavor = "current_thread")]
async fn sweep_reconnects_inline_on_the_current_thread_runtime() {
    // The oracle offloads blocking maintenance to a one-shot worker so its loop
    // keeps no default executor; the port has no blocking pool at all.
    let scene = Scene::new(Peer {
        probes: vec![("send_ping", Answer::Dead), ("list_tools", Answer::Dead)],
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    tokio::time::timeout(Duration::from_secs(10), scene.bridge.sweep_once())
        .await
        .expect("the sweep's reconnect deadlocked its own runtime");
    assert_eq!(scene.connects(), 2);
}

#[tokio::test]
async fn credential_probe_is_singleflight_per_server_and_unbounded_across_servers() {
    let scene = Scene::build(
        Peer {
            call: Answer::Hang,
            ..Peer::default()
        },
        true,
    );
    let config = json!({"command": "fake-mcp", "health_probe": {"tool": "cred_check", "args": {}}});
    for index in 0..16 {
        scene
            .bridge
            .connect_server(&format!("probe{index}"), config.clone())
            .await
            .unwrap();
    }
    // Each connect started one background probe; none waits behind another.
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while scene.named("call:cred_check").len() < 16 {
        assert!(tokio::time::Instant::now() < deadline, "probes were queued");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(!scene.bridge.probe_cred_health_async("probe0").await);
    assert_eq!(scene.named("call:cred_check").len(), 16);
}

// ── first connect, breaker (test_mcp_initial_connect_recovery.py) ───────────

async fn failing_boot() -> Scene {
    let scene = Scene::new(Peer::default());
    *scene.rows.0.lock().unwrap() = json!({
        "flaky": {"command": "npx", "args": ["-y", "flaky-mcp"], "enabled": true},
        "ok": {"command": "ok"},
    });
    scene.connector.fail.lock().unwrap().insert("flaky".into());
    let connected = scene.bridge.connect_all().await.unwrap();
    assert_eq!(
        Value::Object(connected),
        json!({"ok": ["echo", "cred_check"]})
    );
    scene
}

#[tokio::test]
async fn failed_first_connect_is_retried_by_the_keepalive() {
    let scene = failing_boot().await;
    let breaker = scene.bridge.get_breaker_state("flaky").unwrap();
    assert_eq!(breaker["failures"], 1);
    assert!(breaker["retry_in"].as_f64().unwrap() > 29.0);
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 2, "the sweep ignored the backoff");
    scene.bridge.advance_clock(31.0);
    scene.sweeps(1).await;
    let flaky = scene
        .connector
        .configs
        .lock()
        .unwrap()
        .iter()
        .filter(|(n, _)| n == "flaky")
        .count();
    assert_eq!(flaky, 2, "the elapsed backoff was not retried");
}

#[tokio::test]
async fn retry_connects_with_the_stored_row_and_resets_the_breaker() {
    let scene = failing_boot().await;
    scene.connector.fail.lock().unwrap().clear();
    scene.rows.0.lock().unwrap()["flaky"]["command"] = json!("npx-renewed");
    scene.bridge.reconnect_server("flaky").await.unwrap();
    let configs = scene.connector.configs.lock().unwrap().clone();
    assert_eq!(configs.last().unwrap().1["command"], "npx-renewed");
    assert!(scene.bridge.get_breaker_state("flaky").is_none());
}

#[tokio::test]
async fn server_disabled_since_boot_is_forgotten_not_retried() {
    let scene = failing_boot().await;
    scene.rows.0.lock().unwrap()["flaky"]["enabled"] = json!(false);
    let error = scene.bridge.reconnect_server("flaky").await.unwrap_err();
    assert_eq!(error, "MCP server flaky is disabled");
    assert_eq!(scene.connects(), 2);
    assert!(scene.bridge.get_breaker_state("flaky").is_none());
    assert_eq!(scene.bridge.lifecycle_state("flaky")["configured"], false);
}

#[tokio::test]
async fn forget_disconnect_clears_a_first_connect_backoff() {
    let scene = failing_boot().await;
    assert!(scene
        .bridge
        .disconnect_one("flaky", true, false)
        .await
        .unwrap());
    assert!(scene.bridge.get_breaker_state("flaky").is_none());
    scene.bridge.advance_clock(3600.0);
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 2);
}

#[tokio::test]
async fn failed_runtime_connect_handed_to_the_keepalive_is_retried() {
    let scene = Scene::new(Peer::default());
    scene.connector.fail.lock().unwrap().insert("flaky".into());
    let row = json!({"command": "npx"});
    assert!(scene
        .bridge
        .connect_server("flaky", row.clone())
        .await
        .is_err());
    scene.bridge.retry_failed_first_connect("flaky", &row);
    assert_eq!(
        scene.bridge.get_breaker_state("flaky").unwrap()["failures"],
        1
    );
    scene.connector.fail.lock().unwrap().clear();
    scene.bridge.advance_clock(31.0);
    scene.sweeps(1).await;
    assert!(scene.bridge.get_breaker_state("flaky").is_none());
    assert_eq!(scene.bridge.lifecycle_state("flaky")["connected"], true);
}

#[tokio::test]
async fn breaker_backoff_doubles_to_its_ceiling() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    scene.connector.fail.lock().unwrap().insert("srv".into());
    let mut delays = Vec::new();
    for _ in 0..7 {
        assert!(scene.bridge.reconnect_server("srv").await.is_err());
        delays.push(
            scene.bridge.get_breaker_state("srv").unwrap()["retry_in"]
                .as_f64()
                .unwrap()
                .round() as i64,
        );
    }
    assert_eq!(delays, [30, 60, 120, 240, 480, 600, 600]);
    // A failed reconnect withdrew the catalog; the stored row keeps it retried.
    assert!(scene.bridge.get_tool_info("mcp__srv__echo").is_none());
    assert_eq!(scene.state()["configured"], true);
}

// ── reconnects re-read the catalog (test_mcp_reconnect_rereads_catalog.py) ──

fn upgrade(scene: &Scene) {
    *scene.connector.catalog.lock().unwrap() =
        vec![tool("echo", json!({"doc": {"type": "string"}}))];
}

fn live_properties(scene: &Scene) -> Vec<String> {
    let info = scene.bridge.get_tool_info("mcp__srv__echo").unwrap();
    info["openai_def"]["function"]["parameters"]["properties"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect()
}

#[tokio::test]
async fn reconnect_rereads_tool_definitions() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    upgrade(&scene);
    let generation = scene.bridge.tool_catalog_generation();
    scene.bridge.reconnect_server("srv").await.unwrap();
    assert_eq!(live_properties(&scene), ["doc"]);
    assert!(scene.bridge.tool_catalog_generation() > generation);
    assert!(scene.events().contains(&(1, "close".into())));
}

#[tokio::test]
async fn crashed_server_recovered_by_the_next_call_rereads_tool_definitions() {
    let scene = Scene::new(Peer {
        call: Answer::Dead,
        ..Peer::default()
    });
    scene.connect(stdio()).await;
    upgrade(&scene);
    scene.connector.peer.lock().unwrap().call = Answer::Ok;
    // A write is not replayed; the dead transport still refreshes the catalog.
    let error = scene
        .bridge
        .call_tool("mcp__srv__cred_check", json!({}), CallOptions::default())
        .await
        .unwrap_err();
    assert!(error.is_transport_dead());
    assert_eq!(scene.named("call:"), ["call:cred_check"]);
    assert_eq!(scene.connects(), 2);
    assert!(scene.bridge.get_tool_info("mcp__srv__cred_check").is_none());
    assert_eq!(live_properties(&scene), ["doc"]);
}

#[tokio::test]
async fn explicit_connect_of_a_connected_server_rereads_tool_definitions() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    upgrade(&scene);
    scene.connect(stdio()).await;
    assert_eq!(live_properties(&scene), ["doc"]);
    assert!(
        scene.events().contains(&(1, "close".into())),
        "the replaced transport was left running"
    );
}

// ── tools/list_changed ──────────────────────────────────────────────────────

async fn until(mut done: impl FnMut() -> bool) {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while !done() {
        assert!(
            tokio::time::Instant::now() < deadline,
            "condition never held"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test]
async fn list_changed_replaces_the_catalog_and_bumps_the_generation() {
    let scene = Scene::build(Peer::default(), true);
    scene.bridge.start_maintenance();
    scene.connect(stdio()).await;
    let generation = scene.bridge.tool_catalog_generation();
    upgrade(&scene);
    scene.connector.senders.lock().unwrap()[0]
        .send("notifications/tools/list_changed".into())
        .unwrap();
    until(|| scene.bridge.tool_catalog_generation() > generation).await;
    assert_eq!(live_properties(&scene), ["doc"]);
    assert_eq!(scene.connects(), 1, "a list change reconnected");
    // An unchanged catalog is a no-op.
    let generation = scene.bridge.tool_catalog_generation();
    scene.connector.senders.lock().unwrap()[0]
        .send("notifications/tools/list_changed".into())
        .unwrap();
    until(|| {
        scene
            .hooks
            .logs
            .lock()
            .unwrap()
            .iter()
            .any(|(_, m)| m.contains("tools/list_changed: no-op"))
    })
    .await;
    assert_eq!(scene.bridge.tool_catalog_generation(), generation);
    scene.bridge.stop_maintenance();
}

#[tokio::test]
async fn list_changed_from_a_replaced_session_is_ignored() {
    let scene = Scene::build(Peer::default(), true);
    scene.bridge.start_maintenance();
    scene.connect(stdio()).await;
    scene.connect(stdio()).await;
    upgrade(&scene);
    let generation = scene.bridge.tool_catalog_generation();
    scene.connector.senders.lock().unwrap()[0]
        .send("notifications/tools/list_changed".into())
        .unwrap();
    scene.connector.senders.lock().unwrap()[1]
        .send("notifications/other".into())
        .unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(scene.bridge.tool_catalog_generation(), generation);
    scene.bridge.stop_maintenance();
}

// ── shutdown (test_mcp_loop_shutdown.py, test_mcp_submission_lifecycle.py,
//    test_mcp_startup_lifecycle.py) ─────────────────────────────────────────

#[tokio::test]
async fn disconnect_all_closes_every_transport_before_returning() {
    let scene = Scene::new(Peer::default());
    for name in ["a", "b", "c"] {
        scene.bridge.connect_server(name, stdio()).await.unwrap();
    }
    scene.bridge.advance_clock(301.0);
    assert!(scene.bridge.park_idle_stdio_server("b").await);
    let generation = scene.bridge.tool_catalog_generation();
    scene.bridge.disconnect_all().await;
    let closes: BTreeSet<usize> = scene
        .events()
        .into_iter()
        .filter(|(_, e)| e == "close")
        .map(|(id, _)| id)
        .collect();
    assert_eq!(closes, BTreeSet::from([1, 2, 3]));
    assert!(scene.bridge.list_servers().is_empty());
    assert!(scene.bridge.tool_index_rows().is_empty());
    assert!(scene.bridge.tool_catalog_generation() > generation);
    assert_eq!(scene.bridge.catalog_mirror("")["connected"], false);
}

#[tokio::test]
async fn calls_after_shutdown_are_refused_without_a_send() {
    let scene = Scene::new(Peer::default());
    scene.connect(stdio()).await;
    scene.bridge.disconnect_all().await;
    let error = scene
        .bridge
        .call_tool("mcp__srv__echo", json!({}), CallOptions::default())
        .await
        .unwrap_err();
    assert_eq!(
        error,
        CallError::Invalid("MCP server not connected: srv".into())
    );
    assert!(scene.named("call:").is_empty());
    scene.sweeps(1).await;
    assert_eq!(scene.connects(), 1, "a sweep after shutdown reconnected");
}

#[tokio::test]
async fn connection_cannot_publish_after_shutdown() {
    let scene = Scene::new(Peer::default());
    let gate = Arc::new(tokio::sync::Notify::new());
    *scene.connector.gate.lock().unwrap() = Some(gate.clone());
    let connect = scene.bridge.connect_server("late", stdio());
    let shutdown = async {
        until(|| scene.connects() == 1).await;
        scene.bridge.disconnect_all().await;
        gate.notify_one();
    };
    let (outcome, ()) = tokio::join!(connect, shutdown);
    assert_eq!(
        outcome.unwrap_err(),
        "MCP connection completed during shutdown"
    );
    assert!(scene.bridge.list_servers().is_empty());
    assert_eq!(scene.bridge.lifecycle_state("late")["configured"], false);
    assert_eq!(scene.bridge.catalog_mirror("")["connected"], false);
    assert!(
        scene.events().contains(&(1, "close".into())),
        "the late transport leaked"
    );
}

#[tokio::test]
async fn maintenance_task_ends_when_stopped() {
    let scene = Scene::new(Peer::default());
    let stop = tokio_util::sync::CancellationToken::new();
    let task = tokio::spawn({
        let bridge = scene.bridge.clone();
        let stop = stop.clone();
        async move { bridge.run_maintenance(stop).await }
    });
    stop.cancel();
    tokio::time::timeout(Duration::from_secs(5), task)
        .await
        .expect("the maintenance task outlived its stop")
        .unwrap();
}

#[tokio::test]
async fn a_stuck_close_is_bounded_by_the_disconnect_budget() {
    let scene = Scene::new(Peer::default());
    *scene.connector.close_hangs.lock().unwrap() = true;
    scene.connect(stdio()).await;
    let started = std::time::Instant::now();
    assert!(!scene
        .bridge
        .disconnect_one("srv", false, false)
        .await
        .unwrap());
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(scene.bridge.list_servers().is_empty());
    assert_eq!(
        scene
            .bridge
            .disconnect_one("srv", true, true)
            .await
            .unwrap_err(),
        "forget and preserve_catalog are mutually exclusive"
    );
}

// ── handshake deadline scope (test_mcp_handshake_deadline_scope.py) ─────────

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .unwrap()
        .to_path_buf()
}

fn replay_bridge(dir: &std::path::Path, handshake: Duration) -> (Bridge, Value) {
    use tofu_mcp::rmcp_session::{OwnedEnvContext, RmcpConnector};
    let sessions = repo().join("tests/fixtures/mcp_sessions");
    let shim = dir.join("replay");
    std::fs::write(
        &shim,
        format!(
            "#!/bin/sh\nexec \"${{REPLAY_PYTHON:-python3}}\" {} {}\n",
            sessions.join("replay_server.py").display(),
            sessions.join("lifecycle_like.json").display()
        ),
    )
    .unwrap();
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755)).unwrap();
    let base = dir.join("runtime");
    let context = Arc::new(move || {
        struct NoVault;
        impl tofu_mcp::transport::VaultResolver for NoVault {
            fn resolve(&self, _entry: &str) -> Option<String> {
                None
            }
        }
        OwnedEnvContext {
            process_env: std::env::vars().collect(),
            runtime_base: base.to_string_lossy().into_owned(),
            repo_root: repo().to_string_lossy().into_owned(),
            python_minor: "3.12".into(),
            policy_path: Arc::new(|_| String::new()),
            vault: Arc::new(NoVault),
            pool_proxy: None,
        }
    });
    let connector = Arc::new(RmcpConnector {
        context,
        connect_timeout: handshake,
        binding: Arc::new(tofu_mcp::callback::NoBinding),
    });
    let hooks = Arc::new(RecordingHooks::default());
    (
        Bridge::new(None, connector, hooks),
        json!({"command": shim.to_string_lossy()}),
    )
}

/// Stands in for `MCP_CONNECT_TIMEOUT`; `past_handshake` answers after 2.5s.
const HANDSHAKE: Duration = Duration::from_secs(2);

#[tokio::test]
async fn unbudgeted_call_outlives_the_handshake_budget() {
    let dir = tempfile::tempdir().unwrap();
    let (bridge, config) = replay_bridge(dir.path(), HANDSHAKE);
    bridge.connect_server("deadline", config).await.unwrap();
    let output = bridge
        .call_tool(
            "mcp__deadline__past_handshake",
            json!({}),
            CallOptions::default(),
        )
        .await
        .unwrap();
    assert_eq!(output.text(), "slow-ok");
    bridge.disconnect_all().await;
}

#[tokio::test]
async fn explicit_per_server_budget_still_bounds_the_call() {
    let dir = tempfile::tempdir().unwrap();
    let (bridge, mut config) = replay_bridge(dir.path(), HANDSHAKE);
    config["timeout"] = json!(1);
    bridge.connect_server("deadline", config).await.unwrap();
    let error = bridge
        .call_tool("mcp__deadline__slower", json!({}), CallOptions::default())
        .await
        .unwrap_err();
    assert!(error.is_call_timeout(), "{error:?}");
    bridge.disconnect_all().await;
}
