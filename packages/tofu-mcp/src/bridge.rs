//! The MCP bridge call path (`lib/mcp/client/_bridge.py` `MCPBridge`, the
//! Python oracle): catalog indexing, and `call_tool` with every gate the
//! oracle runs before and after the SDK request.
//!
//! `call_tool` order: cancellation and argument checks; name parse; server and
//! tool lookup; user-disabled refusal; activity claim; the owner Hope
//! queue-policy guard (before any wake or send); wake a parked server;
//! schema coercion; the deadline (explicit override, else the server's budget
//! extended by a declared bounded wait); the degraded fast-fail; the request;
//! health settlement (timeout streak, peer-answer reset); replay after a dead
//! transport only for an unchanged enabled read declaration or a trusted
//! opt-in; a dead transport's catalog is refreshed by a reconnect; the
//! credential notice on error results.
//!
//! Owners split into child modules (they share `State`): `catalog`
//! (catalog projections and generations), `credentials` (credential probes,
//! renewal, browser credential learning), `lifecycle` (keepalive/liveness
//! sweep, idle parking, `tools/list_changed`, reconnects).
//!
//! Sessions and connections are injected (`Session`, `Connector`) so the
//! transport stays interchangeable and testable; `rmcp_session` holds the
//! rmcp implementation.

use crate::callback::{BindingCalls, NoBinding};
use crate::coerce::coerce_args_to_schema;
use crate::errors::CallError;
use crate::names::{namespaced_tool_names, parse_namespaced_name};
use crate::pyjson::{self, py_repr};
use crate::queue_policy;
use crate::result::{with_credential_notice, CallOutput, ToolResult};
use async_trait::async_trait;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio_util::sync::CancellationToken;

/// `MCP_DEGRADED_TIMEOUT_STREAK` default (the oracle's contract value).
pub const MCP_DEGRADED_TIMEOUT_STREAK: u32 = 3;
/// `TOOL_WAIT_COMPLETION_ALLOWANCE_MS` (lib/tools/execution_deadlines.py).
pub const TOOL_WAIT_COMPLETION_ALLOWANCE_MS: f64 = 60_000.0;
const WAIT_PARAMETERS: [&str; 3] = ["timeout_seconds", "wait_s", "timeout_sec"];

/// One live MCP session (rmcp in production, fakes in tests).
#[async_trait]
pub trait Session: Send + Sync {
    /// One `tools/call`; `timeout` is the call's read deadline, `None` waits.
    async fn call_tool(
        &self,
        name: &str,
        arguments: Map<String, Value>,
        meta: Option<Map<String, Value>>,
        timeout: Option<Duration>,
    ) -> Result<Value, CallError>;
    async fn close(&self);
    /// The client generation whose probe rules apply (`handle.sdk_generation`):
    /// 2 never pings, like the oracle's SDK-v2 `Client`.
    fn sdk_generation(&self) -> u8 {
        2
    }
    /// Liveness probes callable with no arguments (`_probe_callable`):
    /// `discover`, `send_discover`, `send_ping`, `list_tools`.
    fn probe_methods(&self) -> Vec<&'static str> {
        Vec::new()
    }
    /// One uncached liveness round trip with `method`.
    async fn probe(&self, method: &str) -> Result<(), CallError> {
        Err(CallError::Invalid(format!(
            "unsupported liveness probe {method}"
        )))
    }
    /// A fresh `tools/list`: wire tools and the catalog version.
    async fn list_tools(&self) -> Result<(Vec<Value>, String), CallError> {
        Err(CallError::Unavailable("session cannot list tools".into()))
    }
    /// Whether the server declared `tools.listChanged`.
    fn tools_list_changed(&self) -> bool {
        false
    }
    /// The server's notification methods, taken once by the bridge.
    fn take_notifications(&self) -> Option<tokio::sync::mpsc::UnboundedReceiver<String>> {
        None
    }
}

/// What a successful connect reports.
pub struct Connected {
    pub session: Arc<dyn Session>,
    /// Wire `Tool` objects.
    pub tools: Vec<Value>,
    pub catalog_version: String,
    pub server_name: String,
    pub server_version: String,
    pub protocol_version: String,
}

#[async_trait]
pub trait Connector: Send + Sync {
    async fn connect(
        &self,
        name: &str,
        config: &Value,
        owner_user_id: Option<u64>,
    ) -> Result<Connected, String>;
}

/// Host seams the oracle reaches through other owners.
pub trait Hooks: Send + Sync {
    /// `audit_log(event, **fields)`.
    fn audit(&self, _event: &str, _fields: Map<String, Value>) {}
    /// `logger.<level>(message)`.
    fn log(&self, _level: &str, _message: &str) {}
    /// The owner's Global Router Hope policy file.
    fn policy_path(&self, _owner_user_id: u64) -> String {
        String::new()
    }
    /// Reviewed reads added to the registry's (`credentials::reviewed_read_only_tools`).
    fn reviewed_read_only(&self, _server: &str, _config: &Value) -> BTreeSet<String> {
        BTreeSet::new()
    }
}

pub struct NoHooks;
impl Hooks for NoHooks {}

#[derive(Default, Clone)]
pub struct CallOptions {
    pub timeout_override: Option<i64>,
    pub cancel: Option<CancellationToken>,
    pub retry_on_disconnect: Option<bool>,
    pub meta: Option<Map<String, Value>>,
}

/// A deadline in the oracle's numeric type (rendered like Python's `str`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Budget {
    Int(i64),
    Float(f64),
}

impl Budget {
    fn seconds(self) -> f64 {
        match self {
            Budget::Int(v) => v as f64,
            Budget::Float(v) => v,
        }
    }
    fn truthy(self) -> bool {
        self.seconds() != 0.0
    }
    fn render(self) -> String {
        match self {
            Budget::Int(v) => v.to_string(),
            Budget::Float(v) => pyjson::float_repr(v),
        }
    }
    fn from_value(value: Option<&Value>) -> Option<Budget> {
        match value? {
            Value::Number(n) if n.is_f64() => n.as_f64().map(Budget::Float),
            Value::Number(n) => n.as_i64().map(Budget::Int),
            _ => None,
        }
    }
}

struct Handle {
    config: Value,
    session: Option<Arc<dyn Session>>,
    tools: Vec<Value>,
    server_name: String,
    server_version: String,
    protocol_version: String,
    catalog_version: String,
    catalog_fingerprint: String,
    tools_list_changed: bool,
}

#[derive(Default)]
struct State {
    servers: BTreeMap<String, Handle>,
    configs: BTreeMap<String, Value>,
    tool_index: BTreeMap<String, Value>,
    parked: BTreeSet<String>,
    timeout_streak: BTreeMap<String, u32>,
    last_activity: BTreeMap<String, Instant>,
    generation: u64,
    started: bool,
    /// `_breaker`: name → (consecutive failed reconnects, next retry epoch seconds).
    breaker: BTreeMap<String, (u32, f64)>,
    active_calls: BTreeMap<String, u32>,
    /// `_probe_method`: the memoised liveness probe per server.
    probe_method: BTreeMap<String, String>,
    /// Advanced by `disconnect_all`; a connect that crosses it is not published.
    shutdowns: u64,
    /// Credential health, probe timing and browser credential learning.
    credentials: credentials::CredentialState,
}

pub struct Bridge {
    owner_user_id: Option<u64>,
    state: Mutex<State>,
    reconnect: Mutex<BTreeMap<String, Arc<tokio::sync::Mutex<()>>>>,
    connector: Arc<dyn Connector>,
    hooks: Arc<dyn Hooks>,
    degraded_timeout_streak: u32,
    instance: String,
    /// Host-to-binding calls (`callback.rs`): credential stores, browser devices.
    binding: Arc<dyn BindingCalls>,
    lifecycle: lifecycle::Lifecycle,
    /// This bridge's own `Arc` (set by `shared`), so background credential work can outlive a call.
    me: std::sync::OnceLock<std::sync::Weak<Bridge>>,
}

fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn annotation_hint(annotations: &Value, camel: &str, snake: &str) -> bool {
    let value = match annotations.get(camel) {
        Some(v) if !v.is_null() => Some(v),
        _ => annotations.get(snake),
    };
    value == Some(&Value::Bool(true))
}

/// `_tool_input_schema`: empty → open object.
pub fn tool_input_schema(tool: &Value) -> Value {
    for key in ["input_schema", "inputSchema"] {
        if let Some(schema) = tool.get(key).filter(|s| crate::pyos::truthy(Some(s))) {
            return schema.clone();
        }
    }
    json!({"type": "object", "properties": {}})
}

fn tool_meta(tool: &Value) -> Map<String, Value> {
    ["meta", "_meta"]
        .iter()
        .find_map(|k| {
            tool.get(*k)
                .and_then(Value::as_object)
                .filter(|m| !m.is_empty())
                .cloned()
        })
        .unwrap_or_default()
}

/// The SDK v2 `ToolAnnotations.model_dump(by_alias=True)` the oracle hashes.
fn annotations_dump(tool: &Value) -> Value {
    let Some(annotations) = tool.get("annotations").filter(|a| !a.is_null()) else {
        return Value::Null;
    };
    let Some(map) = annotations.as_object() else {
        return Value::String(pyjson::dumps(annotations, pyjson::SPACED));
    };
    let mut out = Map::new();
    for key in [
        "title",
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    ] {
        out.insert(key.into(), map.get(key).cloned().unwrap_or(Value::Null));
    }
    for (key, value) in map {
        out.entry(key.clone()).or_insert(value.clone());
    }
    Value::Object(out)
}

/// `_schema_hash`: the server's `_meta.schemaHash`, else the canonical digest.
pub fn schema_hash(tool: &Value) -> String {
    if let Some(hash) = tool_meta(tool)
        .get("schemaHash")
        .filter(|h| crate::pyos::truthy(Some(h)))
    {
        return crate::pyos::py_str(hash);
    }
    let payload = json!({
        "name": tool.get("name").and_then(Value::as_str).unwrap_or_default(),
        "description": tool.get("description").and_then(Value::as_str).unwrap_or_default(),
        "inputSchema": tool_input_schema(tool),
        "annotations": annotations_dump(tool),
    });
    format!(
        "sha256:{}",
        sha256_hex(&pyjson::dumps(&payload, pyjson::COMPACT_SORTED))
    )
}

/// `_tool_to_openai`.
pub fn tool_to_openai(server: &str, tool: &Value, namespaced: &str) -> Value {
    let name = tool.get("name").and_then(Value::as_str).unwrap_or_default();
    let description = tool
        .get("description")
        .and_then(Value::as_str)
        .filter(|d| !d.is_empty())
        .map_or_else(|| format!("MCP tool: {name}"), str::to_string);
    let mut schema = tool_input_schema(tool)
        .as_object()
        .cloned()
        .unwrap_or_default();
    schema
        .entry("type")
        .or_insert(Value::String("object".into()));
    json!({"type": "function", "function": {
        "name": namespaced, "description": format!("[MCP:{server}] {description}"), "parameters": schema,
    }})
}

/// `_automatic_read_replay_identity`.
fn replay_identity(info: Option<&Value>) -> Option<Vec<String>> {
    let info = info?;
    if info.get("read_only_hint") != Some(&Value::Bool(true)) {
        return None;
    }
    let identity: Vec<String> = ["server_name", "tool_name", "namespaced_name", "schema_hash"]
        .iter()
        .map(|k| {
            info.get(*k)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        })
        .collect();
    identity.iter().all(|v| !v.is_empty()).then_some(identity)
}

/// `wait_execution_timeout_ms` over a tool's input schema.
pub fn declared_wait_ms(input_schema: &Value, arguments: &Value) -> Result<Option<f64>, String> {
    let properties = input_schema.get("properties").and_then(Value::as_object);
    let mut bounds: Vec<f64> = Vec::new();
    for (name, parameter) in properties.into_iter().flatten() {
        let numeric = matches!(
            parameter.get("type").and_then(Value::as_str),
            Some("integer" | "number")
        );
        if !WAIT_PARAMETERS.contains(&name.as_str()) || !numeric {
            continue;
        }
        let Some(maximum) = parameter.get("maximum").filter(|m| !m.is_null()) else {
            continue;
        };
        let maximum = match maximum {
            Value::Number(n) => n.as_f64().filter(|m| m.is_finite()),
            _ => None,
        }
        .ok_or_else(|| format!("tool wait parameter {name} lacks a finite maximum"))?;
        let requested = arguments
            .get(name)
            .or_else(|| parameter.get("default"))
            .map_or(Some(0.0), |v| match v {
                Value::Number(n) => n.as_f64(),
                _ => None,
            })
            .unwrap_or(maximum);
        bounds.push(maximum.min(requested.max(0.0)));
    }
    let wait = bounds.into_iter().fold(None, |acc: Option<f64>, b| {
        Some(acc.map_or(b, |a| a.max(b)))
    });
    Ok(match wait {
        Some(seconds) if seconds > 0.0 => {
            Some((seconds * 1000.0).ceil() + TOOL_WAIT_COMPLETION_ALLOWANCE_MS)
        }
        _ => None,
    })
}

fn info_row(
    server: &str,
    tool: &Value,
    namespaced: &str,
    reviewed: &BTreeSet<String>,
    catalog_version: &str,
) -> Value {
    let name = tool.get("name").and_then(Value::as_str).unwrap_or_default();
    let annotations = tool.get("annotations").cloned().unwrap_or(Value::Null);
    let mut hints = Map::new();
    for (camel, snake) in [
        ("readOnlyHint", "read_only_hint"),
        ("destructiveHint", "destructive_hint"),
        ("idempotentHint", "idempotent_hint"),
        ("openWorldHint", "open_world_hint"),
    ] {
        hints.insert(
            camel.into(),
            Value::Bool(annotations.is_object() && annotation_hint(&annotations, camel, snake)),
        );
    }
    let output_schema = ["output_schema", "outputSchema"]
        .iter()
        .find_map(|k| tool.get(*k).filter(|v| !v.is_null()))
        .map_or(
            json!({}),
            |v| if v.is_object() { v.clone() } else { json!({}) },
        );
    json!({
        "server_name": server,
        "tool_name": name,
        "namespaced_name": namespaced,
        "description": tool.get("description").and_then(Value::as_str).unwrap_or_default(),
        "input_schema": tool_input_schema(tool),
        "openai_def": tool_to_openai(server, tool, namespaced),
        "read_only_hint": hints["readOnlyHint"] == Value::Bool(true) || reviewed.contains(name),
        "annotations": hints,
        "output_schema": output_schema,
        "meta": tool_meta(tool),
        "schema_hash": schema_hash(tool),
        "catalog_version": catalog_version,
    })
}

/// One tool's catalog row as `_replace_server_catalog` indexes it alone (no reviewed reads).
pub fn index_row(server: &str, tool: &Value, catalog_version: &str) -> Value {
    let raw = tool.get("name").and_then(Value::as_str).unwrap_or_default();
    let namespaced = namespaced_tool_names(server, [raw])
        .remove(raw)
        .unwrap_or_default();
    info_row(server, tool, &namespaced, &BTreeSet::new(), catalog_version)
}

fn disabled_tools(configs: &BTreeMap<String, Value>, server: &str) -> BTreeSet<String> {
    configs
        .get(server)
        .and_then(|row| row.get("disabled_tools"))
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

impl Bridge {
    pub fn new(
        owner_user_id: Option<u64>,
        connector: Arc<dyn Connector>,
        hooks: Arc<dyn Hooks>,
    ) -> Bridge {
        let streak = std::env::var("TOFU_MCP_DEGRADED_TIMEOUT_STREAK")
            .ok()
            .and_then(|v| v.trim().parse::<u32>().ok())
            .unwrap_or(MCP_DEGRADED_TIMEOUT_STREAK);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        Bridge {
            owner_user_id,
            state: Mutex::new(State::default()),
            reconnect: Mutex::new(BTreeMap::new()),
            connector,
            hooks,
            degraded_timeout_streak: streak,
            instance: format!("{:x}{:x}", std::process::id(), nanos),
            binding: Arc::new(NoBinding),
            lifecycle: lifecycle::Lifecycle::default(),
            me: std::sync::OnceLock::new(),
        }
    }

    /// Share this bridge; background work (connect-time credential probes and
    /// learning) runs only on a shared bridge, else the work is awaited inline.
    pub fn shared(self) -> Arc<Bridge> {
        Arc::new_cyclic(|me| {
            let _ = self.me.set(me.clone());
            self
        })
    }

    /// Attach the binding that answers host-to-binding calls.
    pub fn with_binding(mut self, binding: Arc<dyn BindingCalls>) -> Bridge {
        self.binding = binding;
        self
    }

    pub fn owner_user_id(&self) -> Option<u64> {
        self.owner_user_id
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Register a server's stored configuration (the oracle's `_configs`).
    pub fn configure(&self, name: &str, config: Value) {
        self.lock().configs.insert(name.into(), config);
    }

    /// `set_disabled_tools`: hot-update the per-server tool filter.
    pub fn set_disabled_tools(&self, name: &str, tools: &[String]) {
        let mut names: Vec<String> = tools.to_vec();
        names.sort();
        names.dedup();
        let mut state = self.lock();
        let previous = disabled_tools(&state.configs, name);
        let row = state
            .configs
            .entry(name.into())
            .or_insert_with(|| json!({}));
        if let Some(map) = row.as_object_mut() {
            map.insert("disabled_tools".into(), json!(names));
        }
        if let Some(handle) = state.servers.get_mut(name) {
            if let Some(map) = handle.config.as_object_mut() {
                map.insert("disabled_tools".into(), json!(names));
            }
        }
        if names.iter().cloned().collect::<BTreeSet<_>>() != previous {
            state.generation += 1;
        }
    }

    fn install(&self, name: &str, config: Value, connected: Connected) {
        let mut reviewed = self.hooks.reviewed_read_only(name, &config);
        reviewed.extend(self.registry_reviewed(name));
        let mut state = self.lock();
        let previous_fingerprint = state
            .servers
            .get(name)
            .map(|h| h.catalog_fingerprint.clone())
            .unwrap_or_default();
        let tools_list_changed = connected.session.tools_list_changed();
        let handle = Handle {
            config,
            session: Some(connected.session),
            tools: Vec::new(),
            server_name: connected.server_name,
            server_version: connected.server_version,
            protocol_version: connected.protocol_version,
            catalog_version: String::new(),
            catalog_fingerprint: previous_fingerprint,
            tools_list_changed,
        };
        state.servers.insert(name.into(), handle);
        state.parked.remove(name);
        Self::replace_catalog_locked(
            &mut state,
            name,
            &connected.tools,
            &connected.catalog_version,
            &reviewed,
        );
    }

    /// `_replace_server_catalog`: atomically replace one catalog; true when it changed.
    fn replace_catalog_locked(
        state: &mut State,
        name: &str,
        tools: &[Value],
        catalog_version: &str,
        reviewed: &BTreeSet<String>,
    ) -> bool {
        let mut rows: Vec<Value> = tools
            .iter()
            .map(|tool| {
                json!({
                    "name": tool.get("name").and_then(Value::as_str).unwrap_or_default(),
                    "schemaHash": schema_hash(tool),
                    "meta": tool_meta(tool),
                })
            })
            .collect();
        rows.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        let live: BTreeSet<String> = rows
            .iter()
            .filter_map(|r| r["name"].as_str())
            .map(str::to_string)
            .collect();
        let mut identity =
            json!({"server": name, "catalogVersion": catalog_version, "tools": rows});
        if !reviewed.is_empty() {
            identity["reviewedReadOnly"] =
                json!(reviewed.intersection(&live).cloned().collect::<Vec<_>>());
        }
        let fingerprint = sha256_hex(&pyjson::dumps(&identity, pyjson::COMPACT_SORTED));
        let Some(handle) = state.servers.get_mut(name) else {
            return false;
        };
        if handle.catalog_fingerprint == fingerprint {
            return false;
        }
        let names = namespaced_tool_names(
            name,
            tools
                .iter()
                .filter_map(|t| t.get("name").and_then(Value::as_str)),
        );
        handle.tools = tools.to_vec();
        handle.catalog_version = catalog_version.to_string();
        handle.catalog_fingerprint = fingerprint;
        state
            .tool_index
            .retain(|_, info| info["server_name"] != name);
        for tool in tools {
            let raw = tool.get("name").and_then(Value::as_str).unwrap_or_default();
            let namespaced = names.get(raw).cloned().unwrap_or_default();
            state.tool_index.insert(
                namespaced.clone(),
                info_row(name, tool, &namespaced, reviewed, catalog_version),
            );
        }
        state.generation += 1;
        crate::tool_search::global().invalidate_server_catalog(name);
        true
    }

    fn reconnect_lock(&self, name: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut locks = self.reconnect.lock().unwrap_or_else(|p| p.into_inner());
        locks.entry(name.into()).or_default().clone()
    }

    fn reset_timeout_streak(&self, server: &str) {
        let previous = self.lock().timeout_streak.remove(server).unwrap_or(0);
        if self.degraded_timeout_streak > 0 && previous >= self.degraded_timeout_streak {
            self.hooks.log(
                "info",
                &format!(
                    "[MCP:Call] {server} recovered from degraded state (streak was {previous})"
                ),
            );
        }
    }

    /// `_record_call_failure`: true for a timeout (which ends the call); a peer answer resets the streak.
    fn record_call_failure(
        &self,
        server: &str,
        tool: &str,
        error: &CallError,
        timeout: Option<Budget>,
        started: Instant,
    ) -> bool {
        if error.is_call_timeout() {
            let elapsed = started.elapsed().as_secs_f64();
            let streak = {
                let mut state = self.lock();
                let streak = state.timeout_streak.get(server).copied().unwrap_or(0) + 1;
                state.timeout_streak.insert(server.into(), streak);
                streak
            };
            let budget = timeout
                .filter(|t| t.truthy())
                .map_or("none".to_string(), |t| format!("{}s", t.render()));
            self.hooks.log(
                "warning",
                &format!(
                    "[MCP:Call] {server}.{tool} TIMED OUT after {elapsed:.1}s (budget={budget}, consecutive={streak}). \
                     Inspect server health or review its configured call deadline."
                ),
            );
            let mut fields = Map::new();
            fields.insert("server".into(), json!(server));
            fields.insert("tool".into(), json!(tool));
            fields.insert(
                "timeout".into(),
                timeout.map_or(Value::Null, |t| json!(t.seconds())),
            );
            fields.insert("elapsed".into(), json!((elapsed * 10.0).round() / 10.0));
            fields.insert("consecutive".into(), json!(streak));
            self.hooks.audit("mcp_call_timeout", fields);
            return true;
        }
        if error.is_peer_answered() {
            self.reset_timeout_streak(server);
        }
        false
    }

    async fn attempt(
        &self,
        server: &str,
        tool: &str,
        arguments: &Map<String, Value>,
        timeout: Option<Budget>,
        options: &CallOptions,
    ) -> Result<ToolResult, CallError> {
        let session = {
            let state = self.lock();
            state.servers.get(server).and_then(|h| h.session.clone())
        }
        .ok_or_else(|| CallError::Transport("Connection closed".into()))?;
        let read = timeout
            .filter(|t| t.truthy())
            .map(|t| Duration::from_secs_f64(t.seconds().max(0.0)));
        let _active = self.active_call(server);
        let call = session.call_tool(tool, arguments.clone(), options.meta.clone(), read);
        let outcome = match &options.cancel {
            Some(cancel) => tokio::select! {
                result = call => result,
                () = cancel.cancelled() => Err(CallError::Cancelled("MCP call cancelled".into())),
            },
            None => call.await,
        };
        outcome.map(|value| crate::result::project_call_result(&value))
    }

    fn cancelled(options: &CallOptions) -> bool {
        options
            .cancel
            .as_ref()
            .is_some_and(CancellationToken::is_cancelled)
    }

    /// `call_tool`: execute one MCP tool call through every gate of the oracle.
    pub async fn call_tool(
        &self,
        namespaced: &str,
        arguments: Value,
        options: CallOptions,
    ) -> Result<CallOutput, CallError> {
        if Self::cancelled(&options) {
            return Err(CallError::Cancelled("MCP call cancelled".into()));
        }
        let (server, parsed_tool) = parse_namespaced_name(namespaced)
            .ok_or_else(|| CallError::Invalid(format!("Invalid MCP tool name: {namespaced}")))?;
        let (raw_tool, server_config, asleep, mut info) = {
            let mut state = self.lock();
            let info = state.tool_index.get(namespaced).cloned();
            let handle_present = state.servers.contains_key(&server);
            let known_config = state.configs.contains_key(&server);
            if !handle_present && !(known_config && info.is_some()) {
                return Err(CallError::Invalid(format!(
                    "MCP server not connected: {server}"
                )));
            }
            let raw_tool = info
                .as_ref()
                .and_then(|i| i.get("tool_name").and_then(Value::as_str))
                .map_or(parsed_tool.clone(), str::to_string);
            if disabled_tools(&state.configs, &server).contains(&raw_tool) {
                return Err(CallError::Invalid(format!(
                    "MCP tool disabled by user: {namespaced} (re-enable it in Settings → MCP)"
                )));
            }
            state.last_activity.insert(server.clone(), self.monotonic());
            let config = state
                .configs
                .get(&server)
                .cloned()
                .or_else(|| state.servers.get(&server).map(|h| h.config.clone()));
            let asleep = state
                .servers
                .get(&server)
                .is_none_or(|h| h.session.is_none())
                || state.parked.contains(&server);
            (raw_tool, config, asleep, info)
        };

        let policy_path = self
            .owner_user_id
            .map(|owner| self.hooks.policy_path(owner))
            .unwrap_or_default();
        let guard = queue_policy::submission_refusal(
            &server,
            server_config.as_ref(),
            &raw_tool,
            &arguments,
            &policy_path,
            options.meta.as_ref(),
        );
        if let Some(note) = &guard.default_note {
            self.hooks.log("warning", note);
        }
        if let Some(refused) = guard.refusal {
            let project = options
                .meta
                .as_ref()
                .and_then(|m| m.get(queue_policy::PROJECT_META))
                .filter(|p| crate::pyos::truthy(Some(p)))
                .map_or(String::new(), crate::pyos::py_str);
            let queue = refused
                .get("queue")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            self.hooks.log(
                "warning",
                &format!(
                    "[MCP:Call] {server}.{raw_tool} refused by owner queue policy: {} queue={queue} owner={} project={project}",
                    refused["error_code"].as_str().unwrap_or_default(),
                    self.owner_user_id.map_or("None".into(), |o| o.to_string()),
                ),
            );
            let mut fields = Map::new();
            fields.insert("server".into(), json!(server));
            fields.insert("tool".into(), json!(raw_tool));
            fields.insert("error_code".into(), refused["error_code"].clone());
            fields.insert("queue".into(), json!(queue));
            fields.insert("owner_user_id".into(), json!(self.owner_user_id));
            fields.insert("project".into(), json!(project));
            fields.insert("policy_source".into(), refused["policy_source"].clone());
            self.hooks.audit("mcp_queue_policy_refusal", fields);
            let document = Value::Object(refused);
            return Ok(CallOutput::Result(ToolResult {
                text: pyjson::dumps(&document, pyjson::SPACED),
                structured_json: Some(document),
                is_error: true,
                ..Default::default()
            }));
        }

        // Every call would fail on a credential known to be expired; a newer
        // one (stored or held by the owner's browser) reconnects first.
        let renewed = if asleep {
            None
        } else {
            self.newer_credential(&server).await
        };
        if asleep || renewed.is_some() {
            if asleep {
                self.hooks
                    .log("info", &format!("[MCP:Call] waking parked server {server}"));
                self.reconnect_server(&server)
                    .await
                    .map_err(CallError::Error)?;
            } else {
                self.hooks.log(
                    "info",
                    &format!("[MCP:Call] renewing the expired credential of {server}"),
                );
                self.renewal_reconnect(&server, renewed)
                    .await
                    .map_err(CallError::Error)?;
            }
            info = self.lock().tool_index.get(namespaced).cloned();
            if info.is_none() {
                return Err(CallError::Invalid(format!(
                    "MCP tool no longer exposed after reconnect: {namespaced}"
                )));
            }
        }

        let mut tool_name = parsed_tool;
        let mut arguments = arguments;
        if let Some(info) = &info {
            arguments = coerce_args_to_schema(&arguments, &info["input_schema"]);
            tool_name = info["tool_name"].as_str().unwrap_or_default().to_string();
        }

        let timeout = match options.timeout_override {
            Some(value) if value > 0 => Some(Budget::Int(value)),
            Some(_) => {
                return Err(CallError::Invalid(
                    "MCP timeout_override must be a positive integer".into(),
                ))
            }
            None => {
                let configured = {
                    let state = self.lock();
                    state
                        .servers
                        .get(&server)
                        .and_then(|h| Budget::from_value(h.config.get("timeout")))
                };
                match (configured, &info) {
                    (Some(budget), Some(info)) if budget.truthy() => {
                        match declared_wait_ms(&info["input_schema"], &arguments) {
                            Ok(Some(ms)) => {
                                let declared = (ms / 1000.0).ceil();
                                Some(match budget {
                                    Budget::Int(v) if (v as f64) >= declared => budget,
                                    Budget::Float(v) if v >= declared => budget,
                                    Budget::Int(_) => Budget::Int(declared as i64),
                                    Budget::Float(_) => Budget::Int(declared as i64),
                                })
                            }
                            _ => Some(budget),
                        }
                    }
                    (configured, _) => configured,
                }
            }
        };
        let budget_text = timeout
            .filter(|t| t.truthy())
            .map_or("none".to_string(), |t| format!("{}s", t.render()));

        if self.degraded_timeout_streak > 0 {
            let streak = self
                .lock()
                .timeout_streak
                .get(&server)
                .copied()
                .unwrap_or(0);
            if streak >= self.degraded_timeout_streak {
                self.hooks.log(
                    "warning",
                    &format!(
                        "[MCP:Call] {server}.{tool_name} SKIPPED — server degraded after {streak} consecutive call \
                         timeouts (timeout={budget_text}). Not retried; reconnect or raise the per-server timeout to recover."
                    ),
                );
                let mut fields = Map::new();
                fields.insert("server".into(), json!(server));
                fields.insert("tool".into(), json!(tool_name));
                fields.insert("consecutive_timeouts".into(), json!(streak));
                fields.insert(
                    "timeout".into(),
                    timeout.map_or(Value::Null, |t| json!(t.seconds())),
                );
                self.hooks.audit("mcp_server_degraded", fields);
                return Ok(CallOutput::Text(format!(
                    "MCP Error: server {} is degraded — {streak} consecutive call timeouts (configured call budget: \
                     {budget_text}). The call was not attempted. Inspect server health or review its configured call \
                     deadline before retrying.",
                    py_repr(&server)
                )));
            }
        }

        let arguments_map = arguments.as_object().cloned().unwrap_or_default();
        let identity = if options.retry_on_disconnect.is_none() {
            replay_identity(info.as_ref())
        } else {
            None
        };
        let may_replay = options.retry_on_disconnect == Some(true) || identity.is_some();
        let declaration_current = |bridge: &Bridge| {
            let state = bridge.lock();
            let current = state.tool_index.get(namespaced);
            let disabled = disabled_tools(&state.configs, &server).contains(&tool_name);
            let changed =
                options.retry_on_disconnect.is_none() && replay_identity(current) != identity;
            !(disabled || changed)
        };

        let started = Instant::now();
        let result = match self
            .attempt(&server, &tool_name, &arguments_map, timeout, &options)
            .await
        {
            Ok(result) => result,
            Err(error @ CallError::Cancelled(_)) => return Err(error),
            Err(error) => {
                if self.record_call_failure(&server, &tool_name, &error, timeout, started) {
                    return Err(error);
                }
                if may_replay && error.is_transport_dead() {
                    self.hooks.log(
                        "warning",
                        &format!("[MCP:Call] {server}.{tool_name} hit dead transport ({error}) — reconnecting and retrying once"),
                    );
                    if let Err(reconnect) = self.reconnect_server(&server).await {
                        self.hooks.log(
                            "error",
                            &format!("[MCP:Call] reconnect of {server} failed: {reconnect}"),
                        );
                        return Err(error);
                    }
                    if Self::cancelled(&options) {
                        return Err(CallError::Cancelled(
                            "MCP call cancelled during reconnect".into(),
                        ));
                    }
                    if !declaration_current(self) {
                        self.hooks.log(
                            "warning",
                            &format!("[MCP:Call] {server}.{tool_name} reconnected without replay: the enabled read declaration changed"),
                        );
                        return Err(error);
                    }
                    return match self
                        .attempt(&server, &tool_name, &arguments_map, timeout, &options)
                        .await
                    {
                        Ok(result) => {
                            self.reset_timeout_streak(&server);
                            Ok(CallOutput::Result(result))
                        }
                        Err(error @ CallError::Cancelled(_)) => Err(error),
                        Err(second) => {
                            self.record_call_failure(
                                &server, &tool_name, &second, timeout, started,
                            );
                            Err(second)
                        }
                    };
                }
                if error.is_transport_dead() && !Self::cancelled(&options) {
                    if let Err(reconnect) = self.reconnect_server(&server).await {
                        self.hooks.log(
                            "warning",
                            &format!("[MCP:Call] reconnect of {server} after its transport died failed: {reconnect}"),
                        );
                    }
                }
                return Err(error);
            }
        };
        self.reset_timeout_streak(&server);
        let output = CallOutput::Result(result);
        let Some((renewed, notice)) = self.credential_followup(&server, &tool_name, &output).await
        else {
            return Ok(output);
        };
        if !renewed || !may_replay || !declaration_current(self) {
            return Ok(with_credential_notice(output, &notice));
        }
        if Self::cancelled(&options) {
            return Err(CallError::Cancelled(
                "MCP call cancelled during credential renewal".into(),
            ));
        }
        self.hooks.log(
            "info",
            &format!("[MCP:Call] {server}.{tool_name} repeating once with the renewed credential"),
        );
        match self
            .attempt(&server, &tool_name, &arguments_map, timeout, &options)
            .await
        {
            Ok(result) => Ok(CallOutput::Result(result)),
            Err(error @ CallError::Cancelled(_)) => Err(error),
            Err(error) => {
                self.record_call_failure(&server, &tool_name, &error, timeout, started);
                Err(error)
            }
        }
    }

    /// `call_tool_document`: one complete JSON document; never replayed.
    pub async fn call_tool_document(
        &self,
        namespaced: &str,
        arguments: Value,
        timeout: Option<i64>,
        cancel: Option<CancellationToken>,
    ) -> Result<Value, CallError> {
        if timeout.is_some_and(|t| t <= 0) {
            return Err(CallError::Invalid(
                "MCP document timeout must be a positive integer or None".into(),
            ));
        }
        let output = self
            .call_tool(
                namespaced,
                arguments,
                CallOptions {
                    timeout_override: timeout,
                    cancel,
                    retry_on_disconnect: Some(false),
                    meta: None,
                },
            )
            .await?;
        crate::result::decode_document(&output).map_err(CallError::Invalid)
    }

    // ── catalog reads ────────────────────────────────────────────────────────

    pub fn tool_catalog_generation(&self) -> u64 {
        self.lock().generation
    }

    pub fn get_tool_info(&self, namespaced: &str) -> Option<Value> {
        self.lock().tool_index.get(namespaced).cloned()
    }

    /// `tool_index_rows`: every tool, sorted, with its disabled flag.
    pub fn tool_index_rows(&self) -> Vec<(String, Value, bool)> {
        let state = self.lock();
        state
            .tool_index
            .iter()
            .map(|(ns, info)| {
                let disabled = disabled_tools(
                    &state.configs,
                    info["server_name"].as_str().unwrap_or_default(),
                )
                .contains(info["tool_name"].as_str().unwrap_or_default());
                (ns.clone(), info.clone(), disabled)
            })
            .collect()
    }

    /// `get_openai_tool_defs`: enabled definitions sorted by namespaced name.
    pub fn get_openai_tool_defs(&self) -> Vec<Value> {
        self.tool_index_rows()
            .into_iter()
            .filter(|(_, _, d)| !d)
            .map(|(_, info, _)| info["openai_def"].clone())
            .collect()
    }

    /// `get_tool_safety`: every enabled tool's namespaced name → read-only flag.
    pub fn get_tool_safety(&self) -> Map<String, Value> {
        self.tool_index_rows()
            .into_iter()
            .filter(|(_, _, d)| !d)
            .map(|(ns, info, _)| (ns, Value::Bool(info["read_only_hint"] == Value::Bool(true))))
            .collect()
    }

    /// The stored configuration row (`_configs[name]`) and the live handle's copy.
    pub fn server_config(&self, name: &str) -> Value {
        let state = self.lock();
        json!({
            "config": state.configs.get(name),
            "handle_config": state.servers.get(name).map(|h| h.config.clone()),
        })
    }

    /// `catalog_mirror`: the relay export; rows only when `known` is stale.
    pub fn catalog_mirror(&self, known: &str) -> Value {
        let (identity, connected, servers) = {
            let state = self.lock();
            (
                format!("{}:{}", self.instance, state.generation),
                state.started && !state.servers.is_empty(),
                state.servers.keys().cloned().collect::<Vec<_>>(),
            )
        };
        let mut mirror = json!({"identity": identity, "connected": connected, "servers": servers});
        if identity != known {
            mirror["index"] = Value::Array(
                self.tool_index_rows()
                    .into_iter()
                    .map(|(ns, info, d)| json!([ns, info, d]))
                    .collect(),
            );
        }
        mirror
    }

    /// `list_servers`.
    pub fn list_servers(&self) -> Vec<Value> {
        let state = self.lock();
        state
            .servers
            .iter()
            .map(|(name, h)| {
                let tool_names: Vec<&str> = h
                    .tools
                    .iter()
                    .filter_map(|t| t.get("name").and_then(Value::as_str))
                    .collect();
                json!({
                    "name": name,
                    "tools_count": h.tools.len(),
                    "tool_names": tool_names,
                    "description": h.config.get("description").cloned().unwrap_or(json!("")),
                    "transport": h.config.get("transport").cloned().unwrap_or(json!("stdio")),
                    "server_version": h.server_version,
                    "server_impl_name": h.server_name,
                    "protocol_version": h.protocol_version,
                    "sdk_generation": 2,
                    "compatibility_notice": protocol_compatibility_notice(&h.protocol_version),
                    "catalog_version": h.catalog_version,
                    "tools_list_changed": h.tools_list_changed,
                    "parked": state.parked.contains(name),
                })
            })
            .collect()
    }
}

/// `MCP_CURRENT_PROTOCOL_VERSION`.
pub const MCP_CURRENT_PROTOCOL_VERSION: &str = "2026-07-28";

/// `_protocol_compatibility_notice`: a non-blocking advisory for an older dated revision.
pub fn protocol_compatibility_notice(version: &str) -> Value {
    let version = pyjson::py_strip(version);
    let parse = |v: &str| {
        v.split('-')
            .map(|p| p.trim().parse::<i64>())
            .collect::<Result<Vec<_>, _>>()
    };
    let (Ok(parts), Ok(current)) = (parse(version), parse(MCP_CURRENT_PROTOCOL_VERSION)) else {
        return Value::Null;
    };
    if parts.len() != 3 || parts >= current {
        return Value::Null;
    }
    json!({"kind": "legacy_protocol", "protocol_version": version, "target_protocol": MCP_CURRENT_PROTOCOL_VERSION,
           "update_recommended": true, "blocking": false})
}

mod catalog;
mod credentials;
mod lifecycle;
