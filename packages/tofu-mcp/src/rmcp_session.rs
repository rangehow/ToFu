//! The rmcp transport behind [`crate::bridge::Connector`]: stdio children in
//! their own session (process group) with the exact stdio environment, the
//! oracle's OOM priority and a bounded stderr tail; Streamable HTTP with the
//! templated credentials. Handshake budget: `MCP_CONNECT_TIMEOUT`, or
//! `MCP_COLD_INSTALL_TIMEOUT` when the stdio pre-flight (`vendor.rs`) finds an
//! outstanding npx rebuild or a vendored launch with no warm record; a vendored
//! bare command launches isolated through `vendor::vendored_launch_argv`.

use crate::bridge::{Connected, Connector, Session};
use crate::errors::{connect_error_message, stderr_tail_text, CallError, STDERR_TAIL_BYTES};
use crate::stdio_env::{build_stdio_environment, Env, EnvContext};
use crate::transport::{
    is_stdio, normalize_transport, resolve_headers, resolve_url, VaultResolver, SSE,
};
use async_trait::async_trait;
use rmcp::model::{CallToolRequestParams, PaginatedRequestParams};
use rmcp::service::{ClientLifecycleMode, ClientServiceExt, RunningService};
use rmcp::RoleClient;
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::AsyncReadExt;

/// `MCP_CONNECT_TIMEOUT`: a handshake that never completes is a crash, not a wait.
pub const MCP_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// `OOM_SCORE_ADJ_MAX`: Tofu-started work is the kernel's first OOM victim.
const OOM_SCORE_ADJ_MAX: &[u8] = b"1000";

pub struct RmcpSession {
    service: tokio::sync::Mutex<Option<RunningService<RoleClient, ServerMessages>>>,
    peer: rmcp::Peer<RoleClient>,
    notifications: Mutex<Option<tokio::sync::mpsc::UnboundedReceiver<String>>>,
    tools_list_changed: bool,
}

/// The client handler: forwards the server's notification methods to the
/// bridge (`_handle_server_message`); the client info is rmcp's default.
pub struct ServerMessages {
    sender: tokio::sync::mpsc::UnboundedSender<String>,
}

impl rmcp::ClientHandler for ServerMessages {
    async fn on_tool_list_changed(&self, _context: rmcp::service::NotificationContext<RoleClient>) {
        let _ = self.sender.send("notifications/tools/list_changed".into());
    }
}

/// The oracle's `Client(mode='auto')`: `server/discover` for 2026-07-28, else
/// the legacy `initialize` (a compatibility notice, never a refusal).
fn auto_lifecycle() -> ClientLifecycleMode {
    ClientLifecycleMode::Auto {
        preferred_versions: vec![rmcp::model::ProtocolVersion::V_2026_07_28],
        legacy_version: None,
    }
}

fn server_messages() -> (ServerMessages, tokio::sync::mpsc::UnboundedReceiver<String>) {
    let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
    (ServerMessages { sender }, receiver)
}

/// Every `tools/list` page: wire tools and the first page's catalog version.
async fn list_tool_pages(peer: &rmcp::Peer<RoleClient>) -> Result<(Vec<Value>, String), CallError> {
    let mut tools = Vec::new();
    let mut catalog_version = String::new();
    let mut cursor: Option<String> = None;
    loop {
        let params = PaginatedRequestParams::default().with_cursor(cursor.clone());
        let page = peer
            .list_tools(Some(params))
            .await
            .map_err(CallError::from)?;
        let page = serde_json::to_value(page).map_err(|e| CallError::Error(e.to_string()))?;
        if catalog_version.is_empty() {
            if let Some(meta) = page.get("_meta") {
                catalog_version = meta
                    .get("catalogVersion")
                    .or_else(|| meta.get("catalog_version"))
                    .filter(|v| crate::pyos::truthy(Some(v)))
                    .map(crate::pyos::py_str)
                    .unwrap_or_default();
            }
        }
        tools.extend(
            page.get("tools")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
        );
        cursor = page
            .get("nextCursor")
            .and_then(Value::as_str)
            .filter(|c| !c.is_empty())
            .map(str::to_string);
        if cursor.is_none() {
            return Ok((tools, catalog_version));
        }
    }
}

#[async_trait]
impl Session for RmcpSession {
    async fn call_tool(
        &self,
        name: &str,
        arguments: Map<String, Value>,
        meta: Option<Map<String, Value>>,
        timeout: Option<Duration>,
    ) -> Result<Value, CallError> {
        let mut params = CallToolRequestParams::new(name.to_string());
        params.arguments = Some(arguments);
        if let Some(meta) = meta {
            params.meta = Some(serde_json::from_value(Value::Object(meta)).map_err(|e| {
                CallError::Invalid(format!("MCP request meta must be an object or None: {e}"))
            })?);
        }
        let call = self.peer.call_tool(params);
        let result = match timeout {
            Some(limit) => match tokio::time::timeout(limit, call).await {
                Ok(result) => result,
                Err(_) => {
                    return Err(CallError::Timeout(format!(
                        "Timed out while waiting for response to ClientRequest. Waited {} seconds.",
                        crate::pyjson::float_repr(limit.as_secs_f64())
                    )))
                }
            },
            None => call.await,
        };
        let result = result.map_err(CallError::from)?;
        let value = serde_json::to_value(result).map_err(|e| CallError::Error(e.to_string()))?;
        // The oracle's SDK model (`CallToolResult.structuredContent: dict | None`) refuses
        // any other JSON value as a validation error (a ValueError); rmcp keeps it.
        if value
            .get("structuredContent")
            .is_some_and(|s| !s.is_null() && !s.is_object())
        {
            return Err(CallError::Invalid(
                "1 validation error for CallToolResult\nstructuredContent\n  Input should be a valid dictionary [type=dict_type]".into(),
            ));
        }
        Ok(value)
    }

    async fn close(&self) {
        if let Some(service) = self.service.lock().await.take() {
            let _ = service.cancel().await;
        }
    }

    /// rmcp's client is the oracle's SDK-v2 `Client` analogue: its zero-argument
    /// probe is an uncached `tools/list` (`cache_mode='reload'`).
    fn probe_methods(&self) -> Vec<&'static str> {
        vec!["list_tools"]
    }

    async fn probe(&self, method: &str) -> Result<(), CallError> {
        if method != "list_tools" {
            return Err(CallError::Invalid(format!(
                "unsupported liveness probe {method}"
            )));
        }
        let request = rmcp::model::ClientRequest::ListToolsRequest(rmcp::model::ListToolsRequest {
            method: Default::default(),
            params: None,
            extensions: Default::default(),
        });
        self.peer
            .send_request(request)
            .await
            .map(|_| ())
            .map_err(CallError::from)
    }

    async fn list_tools(&self) -> Result<(Vec<Value>, String), CallError> {
        list_tool_pages(&self.peer).await
    }

    fn tools_list_changed(&self) -> bool {
        self.tools_list_changed
    }

    fn take_notifications(&self) -> Option<tokio::sync::mpsc::UnboundedReceiver<String>> {
        self.notifications
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .take()
    }
}

/// Connects with rmcp; every launch fact comes from the host's [`EnvContext`] factory.
pub struct RmcpConnector {
    pub context: Arc<dyn Fn() -> OwnedEnvContext + Send + Sync>,
    pub connect_timeout: Duration,
    /// Host-to-binding calls (`callback.rs`): the shared network authority's route plan.
    pub binding: Arc<dyn crate::callback::BindingCalls>,
}

/// An [`EnvContext`] with owned seams, built per launch (the oracle reads globals per spawn).
pub struct OwnedEnvContext {
    pub process_env: Env,
    pub runtime_base: String,
    pub repo_root: String,
    pub python_minor: String,
    pub policy_path: Arc<dyn Fn(u64) -> String + Send + Sync>,
    pub vault: Arc<dyn VaultResolver + Send + Sync>,
    pub pool_proxy: Option<String>,
}

/// `lib/tools/process_environment.resolve_executable`: `shutil.which` against the
/// mapping the child receives, made absolute.
pub fn resolve_executable(command: &str, env: &Env) -> Option<String> {
    let runnable = |path: &str| {
        let p = std::path::Path::new(path);
        p.exists() && crate::vendor::access_x(path) && !p.is_dir()
    };
    if command.contains('/') {
        return runnable(command).then(|| crate::vendor::abspath(command));
    }
    // `os.defpath`; an empty PATH resolves nothing (`shutil.which`).
    let path = env
        .get("PATH")
        .cloned()
        .unwrap_or_else(|| "/bin:/usr/bin".into());
    if path.is_empty() {
        return None;
    }
    let mut seen = std::collections::BTreeSet::new();
    path.split(':')
        .filter(|dir| seen.insert(dir.to_string()))
        .map(|dir| crate::pyos::join(dir, command))
        .find(|candidate| runnable(candidate))
        .map(|candidate| crate::vendor::abspath(&candidate))
}

struct StderrRing {
    bytes: Vec<u8>,
    truncated: bool,
}

async fn drain_stderr(mut stderr: tokio::process::ChildStderr, ring: Arc<Mutex<StderrRing>>) {
    let mut buffer = [0u8; 4096];
    loop {
        match stderr.read(&mut buffer).await {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                let mut ring = ring.lock().unwrap_or_else(|p| p.into_inner());
                ring.bytes.extend_from_slice(&buffer[..n]);
                if ring.bytes.len() > STDERR_TAIL_BYTES {
                    let excess = ring.bytes.len() - STDERR_TAIL_BYTES;
                    ring.bytes.drain(..excess);
                    ring.truncated = true;
                }
            }
        }
    }
}

fn describe_initialize_error(error: &str) -> String {
    let lower = error.to_lowercase();
    if lower.contains("closed") || lower.contains("eof") {
        "Connection closed".into()
    } else {
        error.to_string()
    }
}

/// The handshake is over once the catalog is listed: no session-level read
/// deadline survives it (`_release_handshake_read_deadline`): the connect
/// budget wraps only `serve` and `tools/list`, and a call without its own
/// budget waits.
async fn finish(
    name: &str,
    service: RunningService<RoleClient, ServerMessages>,
    notifications: tokio::sync::mpsc::UnboundedReceiver<String>,
) -> Result<Connected, String> {
    let peer = service.peer().clone();
    let info = peer.peer_info();
    let info_value = info
        .as_ref()
        .and_then(|i| serde_json::to_value(i.as_ref()).ok())
        .unwrap_or(Value::Null);
    let (tools, catalog_version) = list_tool_pages(&peer)
        .await
        .map_err(|e| connect_error_message(name, &e.to_string(), ""))?;
    let tools_list_changed = info_value
        .pointer("/capabilities/tools/listChanged")
        .is_some_and(|v| crate::pyos::truthy(Some(v)));
    let server_info = info_value.get("serverInfo").cloned().unwrap_or(Value::Null);
    Ok(Connected {
        session: Arc::new(RmcpSession {
            service: tokio::sync::Mutex::new(Some(service)),
            peer,
            notifications: Mutex::new(Some(notifications)),
            tools_list_changed,
        }),
        tools,
        catalog_version,
        server_name: server_info
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .into(),
        server_version: server_info
            .get("version")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .into(),
        protocol_version: info_value
            .get("protocolVersion")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .into(),
    })
}

fn launch_environment(
    owned: &OwnedEnvContext,
    config: &Value,
    owner_user_id: Option<u64>,
) -> Result<Env, String> {
    let policy = owned.policy_path.clone();
    let policy_fn = move |owner: u64| policy(owner);
    let vault: &(dyn VaultResolver + Send + Sync) = owned.vault.as_ref();
    let ctx = EnvContext {
        process_env: owned.process_env.clone(),
        runtime_base: owned.runtime_base.clone(),
        repo_root: owned.repo_root.clone(),
        python_minor: owned.python_minor.clone(),
        policy_path: &policy_fn,
        vault,
        pool_proxy: owned.pool_proxy.clone(),
        prepare_directories: true,
    };
    build_stdio_environment(config, owner_user_id, &ctx)
}

#[async_trait]
impl Connector for RmcpConnector {
    async fn connect(
        &self,
        name: &str,
        config: &Value,
        owner_user_id: Option<u64>,
    ) -> Result<Connected, String> {
        let owned = (self.context)();
        let transport = normalize_transport(config);
        if !is_stdio(config) {
            if config
                .get("url")
                .is_none_or(|u| !crate::pyos::truthy(Some(u)))
            {
                return Err(format!(
                    "MCP server {name}: {transport} transport requires \"url\""
                ));
            }
            if transport == SSE {
                return Err(connect_error_message(
                    name,
                    "the legacy HTTP+SSE transport is not implemented by the Rust client (rmcp); use streamable-http",
                    "",
                ));
            }
            let url = resolve_url(config, name, &owned.process_env, owned.vault.as_ref())?;
            let headers = resolve_headers(config, name, &owned.process_env, owned.vault.as_ref())?;
            let mut custom = std::collections::HashMap::new();
            for (key, value) in headers {
                let key = http::HeaderName::try_from(key.as_str())
                    .map_err(|e| connect_error_message(name, &e.to_string(), ""))?;
                let value = http::HeaderValue::try_from(value.as_str())
                    .map_err(|_| connect_error_message(name, "invalid header value", ""))?;
                custom.insert(key, value);
            }
            // No admission cap: requests are never queued behind a fixed concurrency limit.
            let transport_config = rmcp::transport::streamable_http_client::StreamableHttpClientTransportConfig::with_uri(url)
                .custom_headers(custom)
                .max_concurrent_requests(usize::MAX);
            // Every request routes through the shared network authority (`network.rs`).
            let client = crate::network::RoutedHttpClient {
                network: Arc::new(crate::network::NetworkClient::new(
                    self.binding.clone(),
                    None,
                )),
            };
            let transport = rmcp::transport::StreamableHttpClientTransport::with_client(
                client,
                transport_config,
            );
            let (handler, notifications) = server_messages();
            let service = match tokio::time::timeout(
                self.connect_timeout,
                handler.serve_with_lifecycle(transport, auto_lifecycle()),
            )
            .await
            {
                Ok(Ok(service)) => service,
                Ok(Err(error)) => {
                    return Err(connect_error_message(
                        name,
                        &describe_initialize_error(&error.to_string()),
                        "",
                    ))
                }
                Err(_) => {
                    return Err(connect_error_message(
                        name,
                        "Request 'initialize' timed out",
                        "",
                    ))
                }
            };
            return finish(name, service, notifications).await;
        }

        let command = config
            .get("command")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if command.is_empty() {
            return Err(format!(
                "MCP server {name}: stdio transport requires \"command\""
            ));
        }
        let args: Vec<String> = config
            .get("args")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(crate::pyos::py_str).collect())
            .unwrap_or_default();
        // `connect_server`'s pre-flight and `_server_owner`'s vendored translation:
        // a vendored bare name launches isolated (`uv run --no-project --with-editable`).
        let launcher = crate::vendor::LauncherContext::fetch(
            self.binding.clone(),
            crate::vendor::Fallback {
                process_env: owned.process_env.clone(),
                runtime_base: owned.runtime_base.clone(),
                repo_root: owned.repo_root.clone(),
                python_minor: owned.python_minor.clone(),
            },
        )
        .await;
        let launch = crate::vendor::plan_stdio_launch(&launcher, config, command, &args).await?;
        let handshake_budget = if launch.cold_install {
            crate::vendor::cold_install_timeout(&owned.process_env)
        } else {
            self.connect_timeout
        };
        let (command, args, _npm_lock) = (launch.command, launch.args, launch.npm_lock);
        let env = launch_environment(&owned, config, owner_user_id)?;
        let resolved = resolve_executable(&command, &env).ok_or_else(|| {
            format!(
                "MCP server {}: launcher {} is not on the child PATH. {}",
                crate::pyjson::py_repr(name),
                crate::pyjson::py_repr(&command),
                crate::launcher::launcher_install_hint(&launcher, &command)
            )
        })?;
        let mut cmd = tokio::process::Command::new(&resolved);
        cmd.args(&args).env_clear().envs(&env);
        // SAFETY: the pre-exec hook only performs async-signal-safe open/write/close.
        unsafe {
            cmd.pre_exec(|| {
                let path = b"/proc/self/oom_score_adj\0";
                let fd = libc::open(path.as_ptr().cast(), libc::O_WRONLY | libc::O_CLOEXEC);
                if fd >= 0 {
                    libc::write(
                        fd,
                        OOM_SCORE_ADJ_MAX.as_ptr().cast(),
                        OOM_SCORE_ADJ_MAX.len(),
                    );
                    libc::close(fd);
                }
                Ok(())
            });
        }
        let mut wrapped = process_wrap::tokio::CommandWrap::from(cmd);
        wrapped.wrap(process_wrap::tokio::ProcessSession);
        let (transport, stderr) = rmcp::transport::TokioChildProcess::builder(wrapped)
            .stderr(std::process::Stdio::piped())
            .spawn()
            .map_err(|e| connect_error_message(name, &e.to_string(), ""))?;
        let ring = Arc::new(Mutex::new(StderrRing {
            bytes: Vec::new(),
            truncated: false,
        }));
        let drain = stderr.map(|stderr| tokio::spawn(drain_stderr(stderr, ring.clone())));
        let failure = |cause: String| {
            let ring = ring.clone();
            async move {
                if let Some(drain) = drain {
                    let _ = tokio::time::timeout(MCP_CONNECT_TIMEOUT, drain).await;
                }
                let ring = ring.lock().unwrap_or_else(|p| p.into_inner());
                connect_error_message(name, &cause, &stderr_tail_text(&ring.bytes, ring.truncated))
            }
        };
        let (handler, notifications) = server_messages();
        match tokio::time::timeout(
            handshake_budget,
            handler.serve_with_lifecycle(transport, auto_lifecycle()),
        )
        .await
        {
            Ok(Ok(service)) => finish(name, service, notifications).await,
            Ok(Err(error)) => Err(failure(describe_initialize_error(&error.to_string())).await),
            Err(_) => Err(failure("Request 'initialize' timed out".into()).await),
        }
    }
}
