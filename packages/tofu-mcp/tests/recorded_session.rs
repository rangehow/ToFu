//! Differential against the Python bridge on a recorded MCP session: the Rust
//! client, over real rmcp stdio, connects to
//! `tests/fixtures/mcp_sessions/replay_server.py` and must reproduce every
//! outcome and catalog row the Python bridge recorded in
//! `tests/fixtures/hope_like.golden.json` (`scripts/gen_session_golden.py`).
//! Needs `python3` (or `REPLAY_PYTHON`) for the replay server only.

use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;
use tofu_mcp::bridge::{Bridge, CallOptions, Hooks};
use tofu_mcp::rmcp_session::{OwnedEnvContext, RmcpConnector, MCP_CONNECT_TIMEOUT};
use tofu_mcp::transport::NoVault;

fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

struct PolicyHooks;
impl Hooks for PolicyHooks {
    fn policy_path(&self, _owner: u64) -> String {
        "/nonexistent/HOPE_QUEUE_POLICY.json".into()
    }
}

#[tokio::test]
async fn rust_client_reproduces_the_python_bridge_on_the_recorded_session() {
    let golden: Value = serde_json::from_str(
        &std::fs::read_to_string(
            repo().join("packages/tofu-mcp/tests/fixtures/hope_like.golden.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let scratch = tempfile::tempdir().unwrap();
    let sessions = repo().join("tests/fixtures/mcp_sessions");
    let shim = scratch.path().join("hope-mcp");
    std::fs::write(
        &shim,
        format!(
            "#!/bin/sh\nexec \"${{REPLAY_PYTHON:-python3}}\" {} {} '' {}\n",
            sessions.join("replay_server.py").display(),
            sessions.join("hope_like.json").display(),
            scratch.path().join("state").display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&shim, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();

    let mut process_env: tofu_mcp::stdio_env::Env = std::env::vars().collect();
    let cache = scratch.path().join("cache");
    process_env.insert(
        "TOFU_MCP_CACHE_DIR".into(),
        cache.to_string_lossy().into_owned(),
    );
    let base = scratch.path().join("base").to_string_lossy().into_owned();
    let context = Arc::new(move || OwnedEnvContext {
        process_env: process_env.clone(),
        runtime_base: base.clone(),
        repo_root: repo().to_string_lossy().into_owned(),
        python_minor: "3.12".into(),
        policy_path: Arc::new(|_| "/nonexistent/HOPE_QUEUE_POLICY.json".into()),
        vault: Arc::new(NoVault),
        pool_proxy: None,
    });
    let connector = Arc::new(RmcpConnector {
        context,
        connect_timeout: MCP_CONNECT_TIMEOUT,
        binding: Arc::new(tofu_mcp::callback::NoBinding),
    });
    let bridge = Bridge::new(Some(1), connector, Arc::new(PolicyHooks));
    bridge
        .connect_server("hope", json!({"command": shim.to_string_lossy()}))
        .await
        .unwrap();

    for call in golden["calls"].as_array().unwrap() {
        let tool = call["tool"].as_str().unwrap();
        let options = CallOptions {
            meta: call["meta"].as_object().cloned(),
            ..Default::default()
        };
        let outcome = match bridge
            .call_tool(
                &format!("mcp__hope__{tool}"),
                call["arguments"].clone(),
                options,
            )
            .await
        {
            Ok(output) => json!({"result": output.relay_encode()}),
            Err(error) => json!({"error": error.kind()}),
        };
        let expected = call
            .get("result")
            .map_or_else(|| json!({"error": call["error"]}), |r| json!({"result": r}));
        assert_eq!(outcome, expected, "{tool}");
    }
    let rows: Vec<Value> = bridge
        .tool_index_rows()
        .into_iter()
        .map(|(n, i, d)| json!([n, i, d]))
        .collect();
    assert_eq!(Value::Array(rows), golden["tool_index_rows"]);
    bridge.disconnect_all().await;
}
