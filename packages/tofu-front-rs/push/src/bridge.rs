//! The Python publish bridge (`tofu.push-bridge/v1`).
//!
//! During the strangler period Python owners keep publishing through
//! `lib.agent_core.push.hub.push_event`; the sockets live here. The front
//! door opens one WebSocket to the Python upstream at [`BRIDGE_PATH`],
//! authenticated by [`BRIDGE_HEADER`] = `hex(HMAC-SHA256(secret_key,
//! BRIDGE_LABEL))` (Python accepts it from a loopback peer only:
//! `lib/agent_core/push_front_door.py`). The link is demand-driven: the first
//! browser handshake opens it and a lost link releases every socket it
//! admitted (close 1001), so browser reconnects, not a timer, reopen it.
//!
//! Python -> Rust ops (JSON text messages):
//! - `{"op":"admitted","id","status","userId","rid","subprotocol"}`
//! - `{"op":"mirrored","id"}`: a `subscribe` mirror is held by Python's hub;
//! - `{"op":"frame","channel","taskId","userId","latestKey","text"}`: one
//!   publication, `text` already encoded by `tofu_workers.json_frames`;
//! - `{"op":"build_id","buildId"}`: the current Vite build snapshot;
//! - `{"op":"rpc","socket","text"}`: one correlated control-RPC response.
//!
//! Rust -> Python ops:
//! - `admit` (handshake auth stays single-sourced in Python), `open`,
//!   `close`, `subscribe` / `unsubscribe` (mirror transitions), `heartbeat`
//!   (`refresh_subscriptions` on each idle ping), `abort`, `build_probe`,
//!   `rpc` (the default control-RPC lane, until P1f's Rust session).

use crate::hub::{MirrorChange, PushHub, SocketId};
use futures_util::{SinkExt, StreamExt};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::Sha256;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tofu_contracts::generated_events_v1::PushTransportEnvelope;
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

/// One publication. Routing uses the generated push transport envelope
/// (`contracts/events_v1.json` `pushTransportEnvelope`): the subscription
/// channel and key, never the producing execution id.
#[derive(Debug, Deserialize)]
pub struct BridgeFrame {
    #[serde(flatten)]
    pub envelope: PushTransportEnvelope,
    #[serde(rename = "userId")]
    pub user_id: String,
    #[serde(rename = "latestKey", default)]
    pub latest_key: Option<String>,
    pub text: String,
}

pub const BRIDGE_PATH: &str = "/api/push/front-door-bridge";
pub const BRIDGE_HEADER: &str = "x-tofu-push-bridge";
pub const BRIDGE_LABEL: &[u8] = b"tofu.push-bridge/v1";

/// `hex(HMAC-SHA256(secret_key, BRIDGE_LABEL))`.
pub fn bridge_token(secret_key: &str) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret_key.as_bytes())
        .expect("HMAC accepts keys of any length");
    mac.update(BRIDGE_LABEL);
    hex::encode(mac.finalize().into_bytes())
}

#[derive(Debug, Clone)]
pub struct AdmitRequest {
    pub headers: Vec<(String, String)>,
    pub query: String,
    pub peer: (String, u16),
    pub subprotocols: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdmitReply {
    pub status: u16,
    pub user_id: String,
    pub rid: String,
    pub subprotocol: Option<String>,
}

/// One live bridge connection. Sockets keep the link that admitted them, so
/// their later ops never reach a newer Python session.
pub struct BridgeLink {
    pub generation: u64,
    outbound: mpsc::UnboundedSender<Message>,
    replies: Mutex<HashMap<u64, oneshot::Sender<Value>>>,
    next_call: AtomicU64,
    alive: AtomicBool,
}

impl BridgeLink {
    pub fn is_alive(&self) -> bool {
        self.alive.load(Ordering::SeqCst)
    }

    pub fn send(&self, op: Value) {
        if self.is_alive() {
            let _ = self.outbound.send(Message::text(op.to_string()));
        }
    }

    /// Send `op` with a fresh `id`; the receiver resolves with Python's
    /// reply carrying that `id`, or fails when the link dies first.
    pub fn call(&self, mut op: Value) -> oneshot::Receiver<Value> {
        let id = self.next_call.fetch_add(1, Ordering::SeqCst) + 1;
        let (reply, answer) = oneshot::channel();
        op["id"] = json!(id);
        self.lock_replies().insert(id, reply);
        self.send(op);
        if !self.is_alive() {
            self.lock_replies().remove(&id);
        }
        answer
    }

    /// Report a mirror transition. A first subscription is acknowledged once
    /// Python's hub holds it, so the socket reads its next frame only then:
    /// a publication caused after any later reply is never missed, as with
    /// Python's synchronous `hub.subscribe`.
    pub fn send_mirror(&self, change: &MirrorChange) -> Option<oneshot::Receiver<Value>> {
        match change {
            MirrorChange::Subscribed {
                channel,
                task_id,
                user_id,
            } => Some(self.call(
                json!({"op": "subscribe", "channel": channel, "taskId": task_id, "userId": user_id}),
            )),
            MirrorChange::Unsubscribed {
                channel,
                task_id,
                user_id,
            } => {
                self.send(json!({"op": "unsubscribe", "channel": channel, "taskId": task_id, "userId": user_id}));
                None
            }
        }
    }

    /// Ask Python to decide a handshake. `None`: the link died first.
    pub async fn admit(&self, request: &AdmitRequest) -> Option<AdmitReply> {
        let reply = self
            .call(json!({
                "op": "admit",
                "headers": request.headers,
                "query": request.query,
                "peer": [request.peer.0, request.peer.1],
                "subprotocols": request.subprotocols,
            }))
            .await
            .ok()?;
        let text = |name: &str| reply.get(name).and_then(Value::as_str);
        Some(AdmitReply {
            status: reply
                .get("status")
                .and_then(Value::as_u64)
                .and_then(|status| u16::try_from(status).ok())
                .unwrap_or(500),
            user_id: text("userId").unwrap_or_default().to_string(),
            rid: text("rid").unwrap_or_default().to_string(),
            subprotocol: text("subprotocol").map(str::to_string),
        })
    }

    fn lock_replies(&self) -> std::sync::MutexGuard<'_, HashMap<u64, oneshot::Sender<Value>>> {
        self.replies
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    fn resolve(&self, reply: Value) {
        let Some(id) = reply.get("id").and_then(Value::as_u64) else {
            return;
        };
        if let Some(waiter) = self.lock_replies().remove(&id) {
            let _ = waiter.send(reply);
        }
    }

    fn end(&self) {
        self.alive.store(false, Ordering::SeqCst);
        self.lock_replies().clear();
    }
}

pub struct Bridge {
    upstream: String,
    token: String,
    hub: Arc<PushHub>,
    link: tokio::sync::Mutex<Option<Arc<BridgeLink>>>,
    build_id: Mutex<String>,
}

/// Why no link is available; the handshake answers with this status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BridgeUnavailable(pub u16);

impl Bridge {
    /// `upstream` is the Python server's `host:port` (an `http://` prefix is
    /// accepted, matching the relay's `--upstream`).
    pub fn new(upstream: &str, secret_key: &str, hub: Arc<PushHub>) -> Arc<Self> {
        let upstream = upstream
            .trim_start_matches("http://")
            .trim_end_matches('/')
            .to_string();
        Arc::new(Self {
            upstream,
            token: bridge_token(secret_key),
            hub,
            link: tokio::sync::Mutex::new(None),
            build_id: Mutex::new(String::new()),
        })
    }

    pub fn build_id(&self) -> String {
        self.build_id
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .clone()
    }

    /// The live link, opening one when none is. Concurrent handshakes share
    /// one connection attempt.
    pub async fn link(self: &Arc<Self>) -> Result<Arc<BridgeLink>, BridgeUnavailable> {
        let mut slot = self.link.lock().await;
        if let Some(link) = slot.as_ref() {
            if link.is_alive() {
                return Ok(Arc::clone(link));
            }
        }
        let link = self.connect().await?;
        *slot = Some(Arc::clone(&link));
        Ok(link)
    }

    async fn connect(self: &Arc<Self>) -> Result<Arc<BridgeLink>, BridgeUnavailable> {
        let unavailable = BridgeUnavailable(502);
        let stream = TcpStream::connect(&self.upstream).await.map_err(|error| {
            eprintln!("[Push] bridge connect to {} failed: {error}", self.upstream);
            unavailable
        })?;
        let _ = stream.set_nodelay(true);
        let mut request = format!("ws://{}{}", self.upstream, BRIDGE_PATH)
            .into_client_request()
            .map_err(|_| unavailable)?;
        request.headers_mut().insert(
            BRIDGE_HEADER,
            HeaderValue::from_str(&self.token).map_err(|_| unavailable)?,
        );
        let (socket, _) = tokio_tungstenite::client_async(request, stream)
            .await
            .map_err(|error| match error {
                // A draining Python refuses the bridge like a push socket.
                WsError::Http(response) if response.status().as_u16() == 503 => {
                    BridgeUnavailable(503)
                }
                other => {
                    eprintln!("[Push] bridge handshake failed: {other}");
                    unavailable
                }
            })?;
        let (outbound, mut queued) = mpsc::unbounded_channel();
        let link = Arc::new(BridgeLink {
            generation: self.hub.generation(),
            outbound,
            replies: Mutex::new(HashMap::new()),
            next_call: AtomicU64::new(0),
            alive: AtomicBool::new(true),
        });
        let bridge = Arc::clone(self);
        let owned = Arc::clone(&link);
        tokio::spawn(async move {
            let (mut write, mut read) = socket.split();
            loop {
                tokio::select! {
                    incoming = read.next() => match incoming {
                        Some(Ok(Message::Text(text))) => bridge.receive(&owned, text.as_str()),
                        Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                        Some(Ok(_)) => {}
                    },
                    op = queued.recv() => match op {
                        Some(message) => {
                            if write.send(message).await.is_err() {
                                break;
                            }
                        }
                        None => break,
                    },
                }
            }
            bridge.lost(&owned).await;
        });
        Ok(link)
    }

    async fn lost(&self, link: &Arc<BridgeLink>) {
        // Fence registration first, then publish the link as dead, so no
        // socket admitted by it can outlive it.
        let released = self.hub.reset();
        link.end();
        let mut slot = self.link.lock().await;
        if slot
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, link))
        {
            *slot = None;
        }
        eprintln!("[Push] bridge to Python closed; released {released} push socket(s)");
    }

    fn receive(&self, link: &BridgeLink, text: &str) {
        let Ok(op) = serde_json::from_str::<Value>(text) else {
            eprintln!("[Push] bridge sent a non-JSON message; ignored");
            return;
        };
        let field = |name: &str| op.get(name).and_then(Value::as_str).unwrap_or_default();
        match field("op") {
            "frame" => match BridgeFrame::deserialize(&op) {
                Ok(frame) => {
                    self.hub.deliver(
                        &frame.envelope.channel,
                        &frame.envelope.task_id,
                        &frame.user_id,
                        frame.latest_key.as_deref(),
                        Arc::from(frame.text),
                    );
                }
                Err(error) => eprintln!("[Push] bridge frame without a routing envelope: {error}"),
            },
            "admitted" | "mirrored" => link.resolve(op.clone()),
            "build_id" => {
                *self.build_id.lock().unwrap_or_else(|p| p.into_inner()) =
                    field("buildId").to_string();
            }
            "rpc" => {
                let socket: Option<SocketId> = op.get("socket").and_then(Value::as_u64);
                if let Some(client) = socket.and_then(|id| self.hub.client(id)) {
                    client.enqueue_rpc(Arc::from(field("text")));
                }
            }
            other => eprintln!("[Push] bridge sent unknown op {other:?}; ignored"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_matches_python_hmac() {
        // python3 -c "import hmac,hashlib;print(hmac.new(b'k',
        //   b'tofu.push-bridge/v1',hashlib.sha256).hexdigest())"
        assert_eq!(
            bridge_token("k"),
            "74bc21f785ad241f51ccb50e4fc003a848c14264cd24bd369bc6aea78d22af64"
        );
    }
}
