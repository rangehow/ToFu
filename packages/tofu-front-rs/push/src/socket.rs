//! One `/api/push` socket: handshake, then `routes/push.py::push_ws`'s
//! sender and receiver halves.
//!
//! - Handshake: Python (over the bridge) decides auth, owner and the
//!   correlation id; a refusal is the HTTP status before any upgrade (401,
//!   403, 503), a missing bridge 502. A browser that offered `tofu.push`
//!   gets it selected (never the proof-carrying entry).
//! - Sender: lane priority from [`PushClient`]; after
//!   [`PUSH_DRAIN_PING_SECONDS`] without an outbound frame it sends the
//!   `{"channel": "system", "type": "ping"}` keepalive and asks Python to
//!   refresh the subscription leases.
//! - Receiver: [`PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS`] without an inbound data
//!   message releases the socket; malformed JSON releases it; a message over
//!   [`WEBSOCKET_MAX_MESSAGE_BYTES`] closes with 1009 like Hypercorn.
//! - Either half ending ends the socket. A server-side release closes with
//!   1001 ([`PUSH_SERVER_RELEASE_CLOSE_CODE`]); a peer close is answered by
//!   the protocol layer only.

use crate::bridge::{AdmitRequest, Bridge, BridgeLink};
use crate::client::{Outgoing, PushClient};
use crate::hub::{PushHub, RegisterRefusal, SocketId};
use crate::python_json::dumps_sorted_ascii;
use crate::rpc::{RpcLane, SocketHandle};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Map, Value};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::oneshot;
use tokio::time::{sleep_until, Instant};
use tokio_tungstenite::tungstenite::error::CapacityError;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, WebSocketConfig};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::WebSocketStream;

/// `routes/push.py::PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS`.
pub const PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS: u64 = 180;
/// `PushClient.drain`'s keepalive window (`lib/agent_core/push.py`).
pub const PUSH_DRAIN_PING_SECONDS: u64 = 30;
/// `routes/push.py::PUSH_SERVER_RELEASE_CLOSE_CODE` (RFC 6455 "going away").
pub const PUSH_SERVER_RELEASE_CLOSE_CODE: u16 = 1001;
/// `lib/hypercorn_runtime.py`: `config.websocket_max_message_size`.
pub const WEBSOCKET_MAX_MESSAGE_BYTES: usize = 64 * 1024;
/// `routes/api_v1/auth.py::PUSH_SUBPROTOCOL` is selected by Python's admit.
pub const PUSH_PATH: &str = "/api/push";

#[derive(Debug, Clone, Copy)]
pub struct PushTiming {
    pub ping_after_idle: Duration,
    pub receive_idle: Duration,
}

impl Default for PushTiming {
    fn default() -> Self {
        Self {
            ping_after_idle: Duration::from_secs(PUSH_DRAIN_PING_SECONDS),
            receive_idle: Duration::from_secs(PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS),
        }
    }
}

pub fn websocket_config() -> WebSocketConfig {
    WebSocketConfig::default()
        .max_message_size(Some(WEBSOCKET_MAX_MESSAGE_BYTES))
        .max_frame_size(Some(WEBSOCKET_MAX_MESSAGE_BYTES))
}

pub fn ping_frame() -> Arc<str> {
    Arc::from(dumps_sorted_ascii(
        &json!({"channel": "system", "type": "ping"}),
    ))
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| char::from(*byte)).collect()
}

/// The handshake facts Python's `push_ws` reads, as transport snapshots.
pub fn admit_request(
    headers: &hyper::HeaderMap,
    query: Option<&str>,
    peer: SocketAddr,
) -> AdmitRequest {
    let pairs = headers
        .iter()
        .map(|(name, value)| (name.as_str().to_string(), latin1(value.as_bytes())))
        .collect();
    let subprotocols = headers
        .get_all("sec-websocket-protocol")
        .iter()
        .flat_map(|value| {
            latin1(value.as_bytes())
                .split(',')
                .map(|item| item.trim().to_string())
                .collect::<Vec<_>>()
        })
        .filter(|item| !item.is_empty())
        .collect();
    AdmitRequest {
        headers: pairs,
        query: query.unwrap_or_default().to_string(),
        peer: (peer.ip().to_string(), peer.port()),
        subprotocols,
    }
}

pub struct Admitted {
    pub id: SocketId,
    pub client: Arc<PushClient>,
    pub link: Arc<BridgeLink>,
    pub subprotocol: Option<String>,
}

/// Decide a handshake. `Err(status)` refuses it before the upgrade.
pub async fn admit(
    hub: &Arc<PushHub>,
    bridge: &Arc<Bridge>,
    request: AdmitRequest,
    queue_capacity: usize,
) -> Result<Admitted, u16> {
    if hub.is_draining() {
        return Err(503);
    }
    let link = bridge.link().await.map_err(|unavailable| unavailable.0)?;
    let reply = link.admit(&request).await.ok_or(502u16)?;
    if reply.status != 101 {
        return Err(reply.status);
    }
    let client = Arc::new(PushClient::new(reply.user_id, reply.rid, queue_capacity));
    let id =
        hub.register(Arc::clone(&client), link.generation)
            .map_err(|refusal| match refusal {
                RegisterRefusal::Draining => 503u16,
                RegisterRefusal::StaleGeneration => 502u16,
            })?;
    link.send(json!({"op": "open", "socket": id, "userId": client.user_id, "rid": client.req_id}));
    Ok(Admitted {
        id,
        client,
        link,
        subprotocol: reply.subprotocol,
    })
}

enum End {
    /// Server-side release: close with 1001.
    Release,
    /// Inbound message over the size limit: close with 1009.
    TooBig,
    /// The peer closed or the transport failed; nothing more to send.
    Gone,
}

/// Serve an upgraded socket until either half ends, then unregister it.
pub async fn serve_socket<S>(
    mut socket: WebSocketStream<S>,
    admitted: Admitted,
    hub: Arc<PushHub>,
    bridge: Arc<Bridge>,
    rpc: Arc<dyn RpcLane>,
    timing: PushTiming,
) where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let Admitted {
        id, client, link, ..
    } = admitted;
    let handle = SocketHandle::new(id, Arc::clone(&client), Arc::clone(&link));
    let mut ping_at = Instant::now() + timing.ping_after_idle;
    let mut receive_by = Instant::now() + timing.receive_idle;
    let mut mirrored: Option<oneshot::Receiver<Value>> = None;
    let end = loop {
        tokio::select! {
            _ = async { mirrored.as_mut().expect("guarded").await }, if mirrored.is_some() => {
                mirrored = None;
            }
            outgoing = client.next() => match outgoing {
                Outgoing::Text(text) => {
                    if socket.send(Message::text(text.as_ref())).await.is_err() {
                        break End::Gone;
                    }
                    ping_at = Instant::now() + timing.ping_after_idle;
                }
                Outgoing::Closed => break End::Release,
            },
            _ = sleep_until(ping_at) => {
                link.send(json!({"op": "heartbeat"}));
                if socket.send(Message::text(ping_frame().as_ref())).await.is_err() {
                    break End::Gone;
                }
                ping_at = Instant::now() + timing.ping_after_idle;
            }
            _ = sleep_until(receive_by) => break End::Release,
            incoming = socket.next(), if mirrored.is_none() => match incoming {
                Some(Ok(Message::Text(text))) => {
                    receive_by = Instant::now() + timing.receive_idle;
                    match receive_frame(text.as_bytes(), &handle, &hub, &bridge, rpc.as_ref()) {
                        Received::Release => break End::Release,
                        Received::AwaitMirror(ack) => mirrored = Some(ack),
                        Received::Done => {}
                    }
                }
                Some(Ok(Message::Binary(bytes))) => {
                    receive_by = Instant::now() + timing.receive_idle;
                    match receive_frame(&bytes, &handle, &hub, &bridge, rpc.as_ref()) {
                        Received::Release => break End::Release,
                        Received::AwaitMirror(ack) => mirrored = Some(ack),
                        Received::Done => {}
                    }
                }
                Some(Ok(Message::Close(_))) | None => break End::Gone,
                Some(Ok(_)) => {}
                Some(Err(WsError::Capacity(CapacityError::MessageTooLong { .. }))) => break End::TooBig,
                Some(Err(_)) => break End::Gone,
            },
        }
    };
    let code = match end {
        End::Release => Some(CloseCode::from(PUSH_SERVER_RELEASE_CLOSE_CODE)),
        End::TooBig => Some(CloseCode::Size),
        End::Gone => None,
    };
    if let Some(code) = code {
        let _ = socket
            .close(Some(CloseFrame {
                code,
                reason: "".into(),
            }))
            .await;
    }
    rpc.close(&handle);
    client.disconnect();
    for change in hub.unregister(id) {
        link.send_mirror(&change);
    }
    link.send(json!({"op": "close", "socket": id}));
}

enum Received {
    Done,
    /// Read nothing more until Python's hub holds this subscription.
    AwaitMirror(oneshot::Receiver<Value>),
    /// Malformed JSON: Python's `receive_json` raising ends its receiver.
    Release,
}

fn receive_frame(
    bytes: &[u8],
    handle: &SocketHandle,
    hub: &PushHub,
    bridge: &Bridge,
    rpc: &dyn RpcLane,
) -> Received {
    let Ok(raw) = serde_json::from_slice::<Value>(bytes) else {
        return Received::Release;
    };
    if rpc.receive(handle, &raw) {
        return Received::Done;
    }
    if let Value::Object(frame) = &raw {
        if let Some(ack) = handle_client_frame(frame, handle, hub, bridge) {
            return Received::AwaitMirror(ack);
        }
    }
    Received::Done
}

/// Port of `routes/push.py::_handle_client_frame`. Returns the Python
/// acknowledgement to await when a subscription is the owner's first.
pub fn handle_client_frame(
    frame: &Map<String, Value>,
    handle: &SocketHandle,
    hub: &PushHub,
    bridge: &Bridge,
) -> Option<oneshot::Receiver<Value>> {
    if frame.is_empty() {
        return None;
    }
    let action = frame.get("action").and_then(Value::as_str).unwrap_or("");
    let channel = frame.get("channel").and_then(Value::as_str).unwrap_or("");
    // Python keys subscriptions by any JSON value; only strings can ever
    // match a published frame, so other keys are inert and skipped here.
    let task_id = match frame.get("taskId") {
        None => Some("*"),
        Some(Value::String(task)) => Some(task.as_str()),
        Some(_) => None,
    };
    match action {
        "subscribe" if !channel.is_empty() => {
            if let Some(task_id) = task_id {
                if let Some(change) = hub.subscribe(handle.id, channel, task_id) {
                    return handle.link().send_mirror(&change);
                }
            }
        }
        "unsubscribe" if !channel.is_empty() => {
            if let Some(task_id) = task_id {
                if let Some(change) = hub.unsubscribe(handle.id, channel, task_id) {
                    handle.link().send_mirror(&change);
                }
            }
        }
        "abort" if channel == "chat" && frame.get("taskId").is_some() && task_id != Some("*") => {
            handle.link().send(json!({
                "op": "abort",
                "taskId": frame.get("taskId"),
                "userId": handle.user_id,
                "rid": handle.req_id,
            }));
        }
        "ping" => {
            let mut pong = Map::new();
            pong.insert("channel".into(), json!("system"));
            pong.insert("type".into(), json!("pong"));
            pong.insert("t".into(), frame.get("t").cloned().unwrap_or(Value::Null));
            if frame.get("buildProbe") == Some(&Value::Bool(true)) {
                let build_id = bridge.build_id();
                if !build_id.is_empty() {
                    pong.insert("buildId".into(), json!(build_id));
                }
                // Python requests a background refresh and answers with the
                // last snapshot; the refreshed id rides the next probe.
                handle.link().send(json!({"op": "build_probe"}));
            }
            handle.enqueue_control(Arc::from(dumps_sorted_ascii(&Value::Object(pong))));
        }
        _ => {}
    }
    None
}
