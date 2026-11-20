//! The execution host's MCP relay owner, wire-compatible with
//! `lib/runtime/loopback_relay.py` and `lib/mcp/bridge_relay.py` (the Python
//! oracles): an unchanged Python `RelayedMCPBridge` in a shard reaches this
//! owner's bridges.
//!
//! Protocol: a network-order u32 length precedes one UTF-8 JSON object, both
//! directions. Request `{format, token, method, params[, keep]}`; response
//! `{format, result, error[, kept]}`. One call per connection, except session
//! verbs (`catalog`, `list_servers`) with `keep: true`, which read the next
//! request on the same connection until EOF. During a non-kept call any byte,
//! EOF or error from the caller cancels it (the caller's stop). A lost answer
//! is never replayed. Failures keep their category (`_error_kind`).

use crate::bridge::{Bridge, CallOptions};
use crate::errors::CallError;
use crate::pyjson;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_util::sync::CancellationToken;

pub const FORMAT: &str = "tofu.mcp-bridge-relay/v1";
pub const RECORD_NAME: &str = "mcp-bridge-relay.json";
/// `ACKNOWLEDGEMENT_SECONDS` of the private execution-control contract.
pub const ACKNOWLEDGEMENT: Duration = Duration::from_secs(2);
const SESSION_METHODS: [&str; 2] = ["catalog", "list_servers"];
const METHODS: [&str; 3] = ["catalog", "list_servers", "call_tool"];

/// Owner bridges by owner id; a missing owner gets an empty bridge, like `get_bridge`.
pub type Bridges = Arc<dyn Fn(u64) -> Arc<Bridge> + Send + Sync>;

pub fn record_path(project: &Path) -> PathBuf {
    project.join("data").join(RECORD_NAME)
}

fn random_token() -> String {
    let mut bytes = [0u8; 32];
    let filled = std::fs::File::open("/dev/urandom")
        .and_then(|mut f| std::io::Read::read_exact(&mut f, &mut bytes));
    if filled.is_err() {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        use sha2::Digest;
        bytes.copy_from_slice(&sha2::Sha256::digest(format!(
            "{nanos}:{}",
            std::process::id()
        )));
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn constant_time_eq(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

fn encode(value: &Value) -> Vec<u8> {
    let body = pyjson::dumps(value, pyjson::COMPACT_ASCII).into_bytes();
    let mut frame = (body.len() as u32).to_be_bytes().to_vec();
    frame.extend(body);
    frame
}

async fn read_frame(
    stream: &mut TcpStream,
    deadline: Option<Duration>,
) -> std::io::Result<Option<Value>> {
    let mut header = [0u8; 4];
    let first = async {
        let n = stream.read(&mut header[..1]).await?;
        if n == 0 {
            return Ok(None);
        }
        stream.read_exact(&mut header[1..]).await?;
        let mut body = vec![0u8; u32::from_be_bytes(header) as usize];
        stream.read_exact(&mut body).await?;
        serde_json::from_slice(&body)
            .map(Some)
            .map_err(std::io::Error::other)
    };
    match deadline {
        Some(limit) => tokio::time::timeout(limit, first)
            .await
            .map_err(|_| std::io::Error::other("deadline"))?,
        None => first.await,
    }
}

pub struct RelayServer {
    path: PathBuf,
    token: String,
    closing: CancellationToken,
    calls: Arc<Mutex<BTreeMap<u64, CancellationToken>>>,
    accept: Mutex<Option<tokio::task::JoinHandle<()>>>,
}

impl RelayServer {
    /// Listen on loopback and publish the endpoint record (mode 0600, atomically replaced).
    pub async fn start(path: PathBuf, bridges: Bridges) -> std::io::Result<Arc<RelayServer>> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await?;
        let port = listener.local_addr()?.port();
        let server = Arc::new(RelayServer {
            path: path.clone(),
            token: random_token(),
            closing: CancellationToken::new(),
            calls: Arc::new(Mutex::new(BTreeMap::new())),
            accept: Mutex::new(None),
        });
        let record = json!({"format": FORMAT, "host": "127.0.0.1", "port": port, "pid": std::process::id(), "token": server.token});
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let temp = path.with_extension(format!("json.tmp-{}", std::process::id()));
        {
            use std::os::unix::fs::OpenOptionsExt;
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&temp)?;
            std::io::Write::write_all(
                &mut file,
                pyjson::dumps(&record, pyjson::SPACED).as_bytes(),
            )?;
            file.sync_all()?;
        }
        std::fs::rename(&temp, &path)?;
        let accept_server = server.clone();
        let handle = tokio::spawn(async move {
            let mut next_id = 0u64;
            loop {
                let accepted = tokio::select! {
                    () = accept_server.closing.cancelled() => return,
                    accepted = listener.accept() => accepted,
                };
                let Ok((stream, _)) = accepted else { continue };
                let _ = stream.set_nodelay(true);
                next_id += 1;
                let cancel = CancellationToken::new();
                accept_server
                    .calls
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .insert(next_id, cancel.clone());
                let call_server = accept_server.clone();
                let bridges = bridges.clone();
                let id = next_id;
                tokio::spawn(async move {
                    call_server.serve(stream, cancel, bridges).await;
                    call_server
                        .calls
                        .lock()
                        .unwrap_or_else(|p| p.into_inner())
                        .remove(&id);
                });
            }
        });
        *server.accept.lock().unwrap_or_else(|p| p.into_inner()) = Some(handle);
        Ok(server)
    }

    pub fn port_record(&self) -> &Path {
        &self.path
    }

    /// Connections whose call or session is still entered (the oracle's `_calls`).
    pub fn active_calls(&self) -> usize {
        self.calls.lock().unwrap_or_else(|p| p.into_inner()).len()
    }

    async fn serve(&self, mut stream: TcpStream, cancel: CancellationToken, bridges: Bridges) {
        let mut deadline = Some(ACKNOWLEDGEMENT);
        loop {
            let request = tokio::select! {
                () = self.closing.cancelled() => return,
                request = read_frame(&mut stream, deadline) => request,
            };
            let Ok(Some(request)) = request else { return };
            let method = request
                .get("method")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let valid = request.get("format").and_then(Value::as_str) == Some(FORMAT)
                && request
                    .get("token")
                    .and_then(Value::as_str)
                    .is_some_and(|t| constant_time_eq(t, &self.token))
                && METHODS.contains(&method.as_str())
                && request.get("params").is_some_and(Value::is_object);
            let keep = valid
                && request.get("keep") == Some(&Value::Bool(true))
                && SESSION_METHODS.contains(&method.as_str());
            let mut response = if !valid {
                json!({"format": FORMAT, "result": null, "error": "relay_refused"})
            } else if keep {
                answer(&method, &request["params"], None, &bridges).await
            } else {
                let (mut reader, mut writer) = stream.split();
                let watcher = async {
                    let mut byte = [0u8; 1];
                    let _ = reader.read(&mut byte).await;
                };
                let outcome = tokio::select! {
                    response = answer(&method, &request["params"], Some(cancel.clone()), &bridges) => Some(response),
                    () = watcher => { cancel.cancel(); None }
                };
                match outcome {
                    Some(response) => {
                        let _ = tokio::time::timeout(
                            ACKNOWLEDGEMENT,
                            writer.write_all(&encode(&response)),
                        )
                        .await;
                    }
                    None => {
                        // The caller left: its call was cancelled; nothing to deliver.
                    }
                }
                let _ = stream.shutdown().await;
                return;
            };
            if keep {
                response["kept"] = Value::Bool(true);
            }
            if tokio::time::timeout(ACKNOWLEDGEMENT, stream.write_all(&encode(&response)))
                .await
                .is_err()
                || !keep
            {
                let _ = stream.shutdown().await;
                return;
            }
            deadline = None;
        }
    }

    /// Withdraw admission, cancel entered calls, and remove our record.
    pub async fn close(&self) {
        self.closing.cancel();
        for cancel in self
            .calls
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .values()
        {
            cancel.cancel();
        }
        let handle = self.accept.lock().unwrap_or_else(|p| p.into_inner()).take();
        if let Some(handle) = handle {
            let _ = handle.await;
        }
        if let Ok(text) = std::fs::read_to_string(&self.path) {
            let current: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
            if current.get("token").and_then(Value::as_str) == Some(&self.token) {
                let _ = std::fs::remove_file(&self.path);
            }
        }
    }
}

fn bad_request(kind: &str) -> Value {
    json!({"format": FORMAT, "result": null, "error": format!("relay_bad_request: {kind}")})
}

async fn answer(
    method: &str,
    params: &Value,
    cancel: Option<CancellationToken>,
    bridges: &Bridges,
) -> Value {
    let Some(owner) = params.get("owner_user_id") else {
        return bad_request("KeyError");
    };
    let Some(owner) = owner.as_u64() else {
        return bad_request("TypeError");
    };
    let bridge = bridges(owner);
    let result = match method {
        "catalog" => bridge.catalog_mirror(
            &params
                .get("known")
                .map(crate::pyos::py_str)
                .filter(|k| k != "None")
                .unwrap_or_default(),
        ),
        "list_servers" => Value::Array(bridge.list_servers()),
        _ => {
            let (Some(name), Some(arguments)) = (
                params.get("name").and_then(Value::as_str),
                params.get("arguments"),
            ) else {
                return bad_request("KeyError");
            };
            let options = match call_options(params, cancel) {
                Ok(options) => options,
                Err(error) => {
                    return json!({"format": FORMAT, "result": {"error": {"kind": error.kind(), "message": error.message()}}, "error": null})
                }
            };
            match bridge.call_tool(name, arguments.clone(), options).await {
                Ok(output) => json!({"result": output.relay_encode()}),
                Err(error) => json!({"error": {"kind": error.kind(), "message": error.message()}}),
            }
        }
    };
    json!({"format": FORMAT, "result": result, "error": null})
}

/// Relay/host call parameters with the oracle's argument checks.
pub fn call_options(
    params: &Value,
    cancel: Option<CancellationToken>,
) -> Result<CallOptions, CallError> {
    let timeout_override = match params.get("timeout_override") {
        None | Some(Value::Null) => None,
        Some(Value::Number(n)) if n.is_i64() && n.as_i64().is_some_and(|v| v > 0) => n.as_i64(),
        Some(_) => {
            return Err(CallError::Invalid(
                "MCP timeout_override must be a positive integer".into(),
            ))
        }
    };
    let retry_on_disconnect = match params.get("retry_on_disconnect") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(flag)) => Some(*flag),
        Some(_) => {
            return Err(CallError::Invalid(
                "MCP retry_on_disconnect must be a boolean or None".into(),
            ))
        }
    };
    let meta = match params.get("meta") {
        None | Some(Value::Null) => None,
        Some(Value::Object(map)) => Some(map.clone()),
        Some(_) => {
            return Err(CallError::Invalid(
                "MCP request meta must be an object or None".into(),
            ))
        }
    };
    Ok(CallOptions {
        timeout_override,
        cancel,
        retry_on_disconnect,
        meta,
    })
}
