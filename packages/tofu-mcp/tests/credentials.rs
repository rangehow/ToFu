//! Credential owners on an injected session and binding
//! (`tests/mcp_protocol/test_mcp_cred_health.py`,
//! `test_mcp_online_docs_catalog.py` bridge cases): the probe carries the
//! oracle's `MCP_CRED_PROBE_TIMEOUT`, the re-probe interval gates the sweep, a
//! disconnect drops the verdict, and the registry's reviewed reads fold into
//! the installed catalog only at the catalog endpoint.

use async_trait::async_trait;
use serde_json::{json, Map, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tofu_mcp::bridge::{Bridge, Connected, Connector, NoHooks, Session};
use tofu_mcp::callback::BindingCalls;
use tofu_mcp::errors::CallError;

const KDOCS_ENDPOINT: &str = "https://mcp.example.test/kdocs";

struct FakeSession {
    text: String,
    timeouts: Arc<Mutex<Vec<Option<Duration>>>>,
}

#[async_trait]
impl Session for FakeSession {
    async fn call_tool(
        &self,
        _name: &str,
        _arguments: Map<String, Value>,
        _meta: Option<Map<String, Value>>,
        timeout: Option<Duration>,
    ) -> Result<Value, CallError> {
        self.timeouts.lock().unwrap().push(timeout);
        Ok(json!({"content": [{"type": "text", "text": self.text}]}))
    }
    async fn close(&self) {}
}

struct FakeConnector {
    text: String,
    tools: Vec<Value>,
    timeouts: Arc<Mutex<Vec<Option<Duration>>>>,
}

#[async_trait]
impl Connector for FakeConnector {
    async fn connect(
        &self,
        _name: &str,
        _config: &Value,
        _owner: Option<u64>,
    ) -> Result<Connected, String> {
        Ok(Connected {
            session: Arc::new(FakeSession {
                text: self.text.clone(),
                timeouts: self.timeouts.clone(),
            }),
            tools: self.tools.clone(),
            catalog_version: String::new(),
            server_name: "fake".into(),
            server_version: "1".into(),
            protocol_version: "2025-06-18".into(),
        })
    }
}

/// Answers `mcp.registry_catalog` like `lib/mcp/rust_host.py`.
struct Catalog;

#[async_trait]
impl BindingCalls for Catalog {
    async fn ask(&self, method: &str, _params: Value) -> Result<Value, CallError> {
        match method {
            "mcp.registry_catalog" => Ok(json!([
                {"id": "overleaf", "name": "Overleaf", "category": "Other",
                 "health_probe": {"tool": "list_projects", "fail_patterns": ["overleaf_session"]}},
                {"id": "kdocs", "name": "Kingsoft", "category": "Other", "transport": "streamable-http",
                 "endpoint": KDOCS_ENDPOINT, "read_only_tools": ["sheet.get_range_data"]},
            ])),
            other => Err(CallError::Unavailable(format!("no handler for {other}"))),
        }
    }
}

fn tool(name: &str, read_only: bool) -> Value {
    json!({"name": name, "description": name, "inputSchema": {"type": "object", "properties": {}},
           "annotations": {"readOnlyHint": read_only, "destructiveHint": true}})
}

type Deadlines = Arc<Mutex<Vec<Option<Duration>>>>;

fn bridge(text: &str, tools: Vec<Value>) -> (Arc<Bridge>, Deadlines) {
    let timeouts = Arc::new(Mutex::new(Vec::new()));
    let connector = Arc::new(FakeConnector {
        text: text.into(),
        tools,
        timeouts: timeouts.clone(),
    });
    let bridge = Bridge::new(None, connector, Arc::new(NoHooks)).with_binding(Arc::new(Catalog));
    (Arc::new(bridge), timeouts)
}

#[tokio::test(flavor = "current_thread")]
async fn probe_carries_the_probe_deadline_and_the_interval_gates_the_sweep() {
    let (bridge, timeouts) = bridge(
        "Error fetching projects — overleaf_session cookie expired",
        vec![tool("list_projects", true)],
    );
    bridge
        .connect_server("overleaf", json!({"command": "x"}))
        .await
        .unwrap();
    assert!(bridge.cred_probe_due("overleaf").await);
    let record = bridge.run_cred_probe("overleaf").await.unwrap();
    assert_eq!(record["status"], "expired");
    assert_eq!(
        timeouts.lock().unwrap().as_slice(),
        &[Some(Duration::from_secs(30))]
    );
    assert!(!bridge.cred_probe_due("overleaf").await);
    assert!(!bridge.credential_sweep_now("overleaf").await);
    bridge.age_cred_probe("overleaf", 901.0);
    assert!(bridge.cred_probe_due("overleaf").await);
    assert!(bridge.credential_sweep_now("overleaf").await);
    assert_eq!(timeouts.lock().unwrap().len(), 2);
    bridge
        .disconnect_one("overleaf", true, false)
        .await
        .unwrap();
    assert!(bridge.get_cred_health("overleaf").is_none());
}

#[tokio::test(flavor = "current_thread")]
async fn reviewed_reads_fold_in_only_at_the_catalog_endpoint() {
    let tools = vec![
        tool("sheet.get_range_data", false),
        tool("sheet.update_range_data", false),
        tool("server_declared_read", true),
    ];
    for (url, expected) in [
        (
            KDOCS_ENDPOINT,
            json!({"mcp__kdocs__server_declared_read": true,
                                "mcp__kdocs__sheet_get_range_data": true,
                                "mcp__kdocs__sheet_update_range_data": false}),
        ),
        (
            "https://example.invalid/mcp",
            json!({"mcp__kdocs__server_declared_read": true,
                                               "mcp__kdocs__sheet_get_range_data": false,
                                               "mcp__kdocs__sheet_update_range_data": false}),
        ),
    ] {
        let (bridge, _) = bridge("ok", tools.clone());
        bridge
            .connect_server("kdocs", json!({"transport": "streamable-http", "url": url}))
            .await
            .unwrap();
        assert_eq!(Value::Object(bridge.get_tool_safety()), expected, "{url}");
        let info = bridge
            .get_tool_info("mcp__kdocs__sheet_get_range_data")
            .unwrap();
        assert_eq!(info["annotations"]["readOnlyHint"], false);
    }
}
