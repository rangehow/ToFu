//! JSON-lines host protocol: the Python binding (`lib/mcp/rust_host.py`) and
//! the differential tests drive the Rust MCP client through it.
//!
//! One JSON object per line on stdin: `{"id", "method", "params"}`. Each
//! request runs as its own task; the answer is `{"id", "result"}` or
//! `{"id", "error": {"kind", "message"}}` with the relay's failure categories.
//! `audit` and `log` notifications (`{"method", "params"}`) carry the bridge's
//! audit records and log lines. `cancel {"id"}` stops a running call the way
//! an owner's Stop does. EOF on stdin disconnects every server and exits.

use crate::bridge::{Bridge, Hooks};
use crate::callback::CallbackChannel;
use crate::errors::CallError;
use crate::relay::{call_options, RelayServer};
use crate::rmcp_session::{OwnedEnvContext, RmcpConnector, MCP_CONNECT_TIMEOUT};
use crate::stdio_env::{build_stdio_environment, Env, EnvContext};
use crate::transport::VaultResolver;
use crate::{coerce, names, queue_policy, result, transport};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex, RwLock};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

#[derive(Default, Clone)]
struct Settings {
    runtime_base: String,
    repo_root: String,
    python_minor: String,
    policy_path_template: String,
    vault: BTreeMap<String, String>,
    pool_proxy: Option<String>,
    reviewed_read_only: BTreeMap<String, BTreeSet<String>>,
}

impl Settings {
    fn policy_path(&self, owner: u64) -> String {
        self.policy_path_template
            .replace("{owner}", &owner.to_string())
    }
}

struct MapVault(BTreeMap<String, String>);

impl VaultResolver for MapVault {
    fn resolve(&self, entry: &str) -> Option<String> {
        self.0.get(entry).cloned()
    }
}

struct HostHooks {
    out: mpsc::UnboundedSender<Value>,
    settings: Arc<RwLock<Settings>>,
}

impl Hooks for HostHooks {
    fn audit(&self, event: &str, fields: Map<String, Value>) {
        let _ = self
            .out
            .send(json!({"method": "audit", "params": {"event": event, "fields": fields}}));
    }
    fn log(&self, level: &str, message: &str) {
        let _ = self
            .out
            .send(json!({"method": "log", "params": {"level": level, "message": message}}));
    }
    fn policy_path(&self, owner: u64) -> String {
        self.settings
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .policy_path(owner)
    }
    fn reviewed_read_only(&self, server: &str, _config: &Value) -> BTreeSet<String> {
        let settings = self.settings.read().unwrap_or_else(|p| p.into_inner());
        settings
            .reviewed_read_only
            .get(server)
            .cloned()
            .unwrap_or_default()
    }
}

struct Host {
    /// Host-to-binding calls (`callback.rs`), shared with every owner that needs them.
    binding: Arc<CallbackChannel>,
    settings: Arc<RwLock<Settings>>,
    bridges: Mutex<BTreeMap<Option<u64>, Arc<Bridge>>>,
    calls: Mutex<BTreeMap<String, CancellationToken>>,
    relay: tokio::sync::Mutex<Option<Arc<RelayServer>>>,
    out: mpsc::UnboundedSender<Value>,
}

fn process_env() -> Env {
    std::env::vars_os()
        .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
        .collect()
}

fn str_param<'a>(params: &'a Value, key: &str) -> Result<&'a str, CallError> {
    params
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| CallError::Invalid(format!("missing string parameter {key}")))
}

fn owner_param(params: &Value) -> Result<Option<u64>, CallError> {
    match params.get("owner_user_id") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) if n.as_u64().is_some_and(|v| v > 0) => Ok(n.as_u64()),
        Some(_) => Err(CallError::Invalid(
            "MCP owner_user_id must be a positive integer".into(),
        )),
    }
}

impl Host {
    fn bridge(self: &Arc<Self>, owner: Option<u64>) -> Arc<Bridge> {
        let mut bridges = self.bridges.lock().unwrap_or_else(|p| p.into_inner());
        bridges
            .entry(owner)
            .or_insert_with(|| {
                let settings = self.settings.clone();
                let context = Arc::new(move || {
                    let s = settings.read().unwrap_or_else(|p| p.into_inner()).clone();
                    let template = s.clone();
                    OwnedEnvContext {
                        process_env: process_env(),
                        runtime_base: s.runtime_base.clone(),
                        repo_root: s.repo_root.clone(),
                        python_minor: s.python_minor.clone(),
                        policy_path: Arc::new(move |owner| template.policy_path(owner)),
                        vault: Arc::new(MapVault(s.vault.clone())),
                        pool_proxy: s.pool_proxy.clone(),
                    }
                });
                let connector = Arc::new(RmcpConnector {
                    context,
                    connect_timeout: MCP_CONNECT_TIMEOUT,
                    binding: self.binding.clone(),
                });
                let hooks = Arc::new(HostHooks {
                    out: self.out.clone(),
                    settings: self.settings.clone(),
                });
                let bridge = Bridge::new(owner, connector, hooks)
                    .with_binding(self.binding.clone())
                    .shared();
                bridge.start_maintenance();
                bridge
            })
            .clone()
    }

    async fn dispatch(
        self: &Arc<Self>,
        id: &str,
        method: &str,
        params: &Value,
    ) -> Result<Value, CallError> {
        match method {
            "configure" => {
                let mut settings = self.settings.write().unwrap_or_else(|p| p.into_inner());
                let text = |key: &str| {
                    params
                        .get(key)
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string()
                };
                settings.runtime_base = text("runtime_base");
                settings.repo_root = text("repo_root");
                settings.python_minor = text("python_minor");
                settings.policy_path_template = text("policy_path_template");
                settings.pool_proxy = params
                    .get("pool_proxy")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                settings.vault = params
                    .get("vault")
                    .and_then(Value::as_object)
                    .map(|m| {
                        m.iter()
                            .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                            .collect()
                    })
                    .unwrap_or_default();
                settings.reviewed_read_only = params
                    .get("reviewed_read_only")
                    .and_then(Value::as_object)
                    .map(|m| {
                        m.iter()
                            .map(|(k, v)| {
                                let names = v
                                    .as_array()
                                    .into_iter()
                                    .flatten()
                                    .filter_map(Value::as_str)
                                    .map(str::to_string);
                                (k.clone(), names.collect())
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                Ok(Value::Null)
            }
            "connect_server" => {
                let bridge = self.bridge(owner_param(params)?);
                let config = params.get("config").cloned().unwrap_or(json!({}));
                bridge
                    .connect_server(str_param(params, "name")?, config)
                    .await
                    .map(|tools| json!(tools))
                    .map_err(CallError::Error)
            }
            "configure_server" => {
                let bridge = self.bridge(owner_param(params)?);
                bridge.configure(
                    str_param(params, "name")?,
                    params.get("config").cloned().unwrap_or(json!({})),
                );
                Ok(Value::Null)
            }
            "set_disabled_tools" => {
                let tools: Vec<String> = params
                    .get("tools")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect();
                self.bridge(owner_param(params)?)
                    .set_disabled_tools(str_param(params, "name")?, &tools);
                Ok(Value::Null)
            }
            "call_tool" | "call_tool_document" => {
                let bridge = self.bridge(owner_param(params)?);
                let cancel = CancellationToken::new();
                self.calls
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .insert(id.to_string(), cancel.clone());
                let name = str_param(params, "name")?.to_string();
                let arguments = params.get("arguments").cloned().unwrap_or(json!({}));
                let outcome = if method == "call_tool" {
                    match call_options(params, Some(cancel)) {
                        Ok(options) => bridge
                            .call_tool(&name, arguments, options)
                            .await
                            .map(|o| o.relay_encode()),
                        Err(error) => Err(error),
                    }
                } else {
                    let timeout = match params.get("timeout") {
                        None | Some(Value::Null) => Ok(None),
                        Some(Value::Number(n)) if n.is_i64() => Ok(n.as_i64()),
                        Some(_) => Err(CallError::Invalid(
                            "MCP document timeout must be a positive integer or None".into(),
                        )),
                    };
                    match timeout {
                        Ok(timeout) => {
                            bridge
                                .call_tool_document(&name, arguments, timeout, Some(cancel))
                                .await
                        }
                        Err(error) => Err(error),
                    }
                };
                self.calls
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .remove(id);
                outcome
            }
            "cancel" => {
                let target = params
                    .get("id")
                    .map(crate::pyos::py_str)
                    .unwrap_or_default();
                if let Some(token) = self
                    .calls
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .get(&target)
                {
                    token.cancel();
                }
                Ok(Value::Null)
            }
            "catalog" => Ok(self.bridge(owner_param(params)?).catalog_mirror(
                params
                    .get("known")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )),
            "list_servers" => Ok(Value::Array(
                self.bridge(owner_param(params)?).list_servers(),
            )),
            "tool_index_rows" => Ok(Value::Array(
                self.bridge(owner_param(params)?)
                    .tool_index_rows()
                    .into_iter()
                    .map(|(n, i, d)| json!([n, i, d]))
                    .collect(),
            )),
            "openai_tool_defs" => Ok(Value::Array(
                self.bridge(owner_param(params)?).get_openai_tool_defs(),
            )),
            "park" => {
                self.bridge(owner_param(params)?)
                    .park(str_param(params, "name")?)
                    .await;
                Ok(Value::Null)
            }
            "disconnect_all" => {
                self.bridge(owner_param(params)?).disconnect_all().await;
                Ok(Value::Null)
            }
            "relay_start" => {
                let path =
                    crate::relay::record_path(std::path::Path::new(str_param(params, "project")?));
                let host = self.clone();
                let bridges: crate::relay::Bridges =
                    Arc::new(move |owner| host.bridge(Some(owner)));
                let server = RelayServer::start(path.clone(), bridges)
                    .await
                    .map_err(|e| CallError::Error(e.to_string()))?;
                *self.relay.lock().await = Some(server);
                Ok(json!({"path": path.to_string_lossy()}))
            }
            "relay_stop" => {
                if let Some(server) = self.relay.lock().await.take() {
                    server.close().await;
                }
                Ok(Value::Null)
            }
            "policy_check" => {
                let meta = params.get("meta").and_then(Value::as_object);
                let outcome = queue_policy::submission_refusal(
                    str_param(params, "server")?,
                    params.get("config").filter(|c| !c.is_null()),
                    str_param(params, "tool")?,
                    params.get("arguments").unwrap_or(&Value::Null),
                    params
                        .get("policy_path")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                    meta,
                );
                Ok(json!({"refusal": outcome.refusal, "default_note": outcome.default_note}))
            }
            "load_policy" => {
                let policy = queue_policy::load_policy(
                    params
                        .get("path")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                );
                Ok(
                    json!({"path": policy.path, "entries": policy.entries, "default_reason": policy.default_reason,
                          "source": policy.source(), "described": policy.described()}),
                )
            }
            "cli_submission_request" => {
                let argv: Vec<String> = params
                    .get("argv")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(crate::pyos::py_str)
                    .collect();
                Ok(
                    match queue_policy::submission_request(&argv, str_param(params, "cwd")?) {
                        Some((queues, unknown)) => json!([queues, unknown]),
                        None => Value::Null,
                    },
                )
            }
            "stdio_environment" => {
                let process: Env = params
                    .get("process_env")
                    .and_then(Value::as_object)
                    .map(|m| {
                        m.iter()
                            .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                            .collect()
                    })
                    .unwrap_or_default();
                let text = |key: &str| {
                    params
                        .get(key)
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string()
                };
                let template = text("policy_path_template");
                let policy = move |owner: u64| template.replace("{owner}", &owner.to_string());
                let vault = MapVault(
                    params
                        .get("vault")
                        .and_then(Value::as_object)
                        .map(|m| {
                            m.iter()
                                .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                                .collect()
                        })
                        .unwrap_or_default(),
                );
                let ctx = EnvContext {
                    process_env: process,
                    runtime_base: text("runtime_base"),
                    repo_root: text("repo_root"),
                    python_minor: text("python_minor"),
                    policy_path: &policy,
                    vault: &vault,
                    pool_proxy: params
                        .get("pool_proxy")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    prepare_directories: params.get("prepare_directories")
                        != Some(&Value::Bool(false)),
                };
                let config = params.get("config").cloned().unwrap_or(Value::Null);
                build_stdio_environment(&config, owner_param(params)?, &ctx)
                    .map(|env| json!(env))
                    .map_err(CallError::Invalid)
            }
            "redact_config" => {
                let (out, unknown) =
                    transport::redact_config(params.get("config").unwrap_or(&Value::Null));
                Ok(json!({"config": out, "withheld": unknown}))
            }
            "scrub_text" => Ok(json!(transport::scrub_text(str_param(params, "text")?))),
            "header_env_keys" => Ok(json!(transport::header_env_keys(
                params.get("config").unwrap_or(&Value::Null)
            ))),
            "coerce" => Ok(coerce::coerce_args_to_schema(
                params.get("arguments").unwrap_or(&Value::Null),
                params.get("schema").unwrap_or(&Value::Null),
            )),
            "project_result" => {
                let projected =
                    result::project_call_result(params.get("result").unwrap_or(&Value::Null));
                Ok(result::CallOutput::Result(projected).relay_encode())
            }
            "namespaced_names" => {
                let tools: Vec<String> = params
                    .get("tools")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect();
                Ok(json!(names::namespaced_tool_names(
                    str_param(params, "server")?,
                    tools.iter().map(String::as_str)
                )))
            }
            "schema_hash" => Ok(json!(crate::bridge::schema_hash(
                params.get("tool").unwrap_or(&Value::Null)
            ))),
            "tool_safety" => Ok(Value::Object(
                self.bridge(owner_param(params)?).get_tool_safety(),
            )),
            "tool_catalog_generation" => Ok(json!(self
                .bridge(owner_param(params)?)
                .tool_catalog_generation())),
            "server_config" => Ok(self
                .bridge(owner_param(params)?)
                .server_config(str_param(params, "name")?)),
            "relay_active_calls" => Ok(json!(self
                .relay
                .lock()
                .await
                .as_ref()
                .map_or(0, |server| server.active_calls()))),
            "merge_result_parts" => Ok(json!(result::merge_result_parts(
                str_param(params, "text")?,
                params.get("structured").and_then(Value::as_str)
            ))),
            "extract_structured_content" => {
                result::extract_structured_content(params.get("result").unwrap_or(&Value::Null))
                    .map(|text| json!(text))
                    .map_err(CallError::Invalid)
            }
            "index_tool" => Ok(crate::bridge::index_row(
                str_param(params, "server")?,
                params.get("tool").unwrap_or(&Value::Null),
                params
                    .get("catalog_version")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )),
            "tool_result_is_error" => Ok(json!(result::tool_result_is_error(
                params.get("result").unwrap_or(&Value::Null)
            ))),
            launcher if launcher.starts_with("launcher.") => {
                crate::launcher::dispatch(
                    &launcher["launcher.".len()..],
                    params,
                    self.binding.clone(),
                )
                .await
            }
            m if m.starts_with("updates.") => {
                let bridge = self.bridge(owner_param(params)?);
                crate::updates::host_call(
                    &m["updates.".len()..],
                    params,
                    self.binding.clone(),
                    &bridge,
                )
                .await
            }
            "network_get" => crate::network::host_get(self.binding.clone(), params).await,
            m if m.starts_with("credentials.") => {
                self.bridge(owner_param(params)?)
                    .credentials_host_call(&m["credentials.".len()..], params)
                    .await
            }
            "sweep_once" => {
                self.bridge(owner_param(params)?).sweep_once().await;
                Ok(Value::Null)
            }
            "breaker_state" => Ok(self
                .bridge(owner_param(params)?)
                .get_breaker_state(str_param(params, "name")?)
                .unwrap_or(Value::Null)),
            "lifecycle_state" => Ok(self
                .bridge(owner_param(params)?)
                .lifecycle_state(str_param(params, "name")?)),
            "advance_clock" => {
                self.bridge(owner_param(params)?).advance_clock(
                    params
                        .get("seconds")
                        .and_then(Value::as_f64)
                        .unwrap_or_default(),
                );
                Ok(Value::Null)
            }
            "park_idle" => Ok(json!(
                self.bridge(owner_param(params)?)
                    .park_idle_stdio_server(str_param(params, "name")?)
                    .await
            )),
            "reconnect_server" => self
                .bridge(owner_param(params)?)
                .reconnect_server(str_param(params, "name")?)
                .await
                .map(|()| Value::Null)
                .map_err(CallError::Error),
            "disconnect_server" => self
                .bridge(owner_param(params)?)
                .disconnect_one(
                    str_param(params, "name")?,
                    params.get("forget") == Some(&Value::Bool(true)),
                    false,
                )
                .await
                .map(|closed| json!(closed))
                .map_err(CallError::Invalid),
            "connect_all" => self
                .bridge(owner_param(params)?)
                .connect_all()
                .await
                .map(Value::Object),
            "classify_error" => Ok(Bridge::classify_error(
                params.get("code").unwrap_or(&Value::Null),
                params
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )),
            m if m.starts_with("tool_search.") => {
                let bridge = match params.get("owner_user_id") {
                    None => None,
                    Some(_) => Some(self.bridge(owner_param(params)?)),
                };
                crate::tool_search::host_call(&*self.binding, bridge, m, params).await
            }
            m if m.starts_with("progressive.") => {
                let bridge = self.bridge(owner_param(params)?);
                crate::progressive::host_call(&bridge, m, params).await
            }
            m if m.starts_with("catalog.") => {
                let bridge = self.bridge(owner_param(params)?);
                bridge.catalog_host_call(m, params).await
            }
            m if m.starts_with("connector_guidance.") => {
                crate::connector_guidance::host_call(&*self.binding, m, params).await
            }
            m if m.starts_with("registry.") => {
                crate::registry::host_call(&*self.binding, m, params).await
            }
            "ask_binding" => {
                use crate::callback::BindingCalls;
                self.binding
                    .ask(
                        str_param(params, "method")?,
                        params.get("params").cloned().unwrap_or(json!({})),
                    )
                    .await
            }
            _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
        }
    }
}

/// Run the host on stdin/stdout until stdin closes.
pub async fn run() -> std::io::Result<()> {
    let (out, mut receiver) = mpsc::unbounded_channel::<Value>();
    let writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(message) = receiver.recv().await {
            if message.is_null() {
                return;
            }
            let mut line = serde_json::to_vec(&message).unwrap_or_default();
            line.push(b'\n');
            if stdout.write_all(&line).await.is_err() || stdout.flush().await.is_err() {
                return;
            }
        }
    });
    let host = Arc::new(Host {
        binding: Arc::new(CallbackChannel::new(out.clone())),
        settings: Arc::new(RwLock::new(Settings::default())),
        bridges: Mutex::new(BTreeMap::new()),
        calls: Mutex::new(BTreeMap::new()),
        relay: tokio::sync::Mutex::new(None),
        out: out.clone(),
    });
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let mut tasks = Vec::new();
    while let Some(line) = lines.next_line().await? {
        let message: Value = match serde_json::from_str(&line) {
            Ok(message) => message,
            Err(error) => {
                let _ = out.send(json!({"id": null, "error": {"kind": "invalid", "message": format!("not JSON: {error}")}}));
                continue;
            }
        };
        if message.get("reply").is_some() {
            host.binding.resolve(&message);
            continue;
        }
        let host = host.clone();
        let out = out.clone();
        tasks.push(tokio::spawn(async move {
            let id = message.get("id").map(crate::pyos::py_str).unwrap_or_default();
            let method = message.get("method").and_then(Value::as_str).unwrap_or_default().to_string();
            let params = message.get("params").cloned().unwrap_or(json!({}));
            let reply = match host.dispatch(&id, &method, &params).await {
                Ok(result) => json!({"id": message.get("id"), "result": result}),
                Err(error) => json!({"id": message.get("id"), "error": {"kind": error.kind(), "message": error.message()}}),
            };
            let _ = out.send(reply);
        }));
        tasks.retain(|task| !task.is_finished());
    }
    host.binding.close();
    crate::launcher::shutdown().await;
    if let Some(server) = host.relay.lock().await.take() {
        server.close().await;
    }
    let bridges: Vec<Arc<Bridge>> = host
        .bridges
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .values()
        .cloned()
        .collect();
    for bridge in bridges {
        bridge.stop_maintenance();
        bridge.disconnect_all().await;
    }
    for task in tasks {
        task.abort();
    }
    let _ = out.send(Value::Null);
    let _ = writer.await;
    Ok(())
}
