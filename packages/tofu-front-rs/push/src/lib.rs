//! tofu-front-push: Phase 1e of `docs/RUST_REWRITE_EVIDENCE.md`, a standalone
//! Rust `WS /api/push` multiplex library. No parent executable host or manifest
//! is tracked under `packages/tofu-front-rs`; historical scratch-host integration
//! does not establish current routing or runtime qualification.
//!
//! Responsibility when called by a host: manage the sockets passed to this
//! library (subscriptions, lanes, keepalive, backpressure, release codes) with
//! `PushHub` semantics. Python publishers use `hub.push_event` over the bridge;
//! the tracked Python `/api/push` handler remains in `routes/push.py`.
//!
//! Modules:
//! - [`hub`]: subscription routing (`PushHub`).
//! - [`client`]: one socket's lanes and backlog (`PushClient`).
//! - [`socket`]: handshake and the sender/receiver halves (`push_ws`).
//! - [`bridge`]: `tofu.push-bridge/v1` to `lib/agent_core/push_front_door.py`.
//! - [`rpc`]: the control-RPC lane boundary (P1f).
//! - [`python_json`]: byte-compatible `json.dumps` for Rust-built frames.
//!
//! Caller contract: a host integrating [`PushFrontDoor`] routes a request to
//! [`PushFrontDoor::handle`] when [`PushFrontDoor::accepts`] holds and calls
//! [`PushFrontDoor::shutdown`] when its shutdown trigger fires, before
//! waiting for connections. Runs on the caller's tokio runtime; it spawns
//! one task per socket and one for the bridge, and sizes no pool.

pub mod bridge;
pub mod client;
pub mod hub;
pub mod python_json;
pub mod rpc;
pub mod socket;

use bridge::Bridge;
use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Empty};
use hub::PushHub;
use hyper::body::Incoming;
use hyper::header::{
    HeaderValue, CONNECTION, SEC_WEBSOCKET_ACCEPT, SEC_WEBSOCKET_PROTOCOL, UPGRADE,
};
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use rpc::{BridgeRpcLane, RpcLane};
use socket::{admit, admit_request, serve_socket, websocket_config, PushTiming, PUSH_PATH};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;
use tokio_tungstenite::tungstenite::protocol::Role;
use tokio_tungstenite::WebSocketStream;

pub type PushBody = BoxBody<Bytes, hyper::Error>;

pub struct PushFrontDoor {
    hub: Arc<PushHub>,
    bridge: Arc<Bridge>,
    rpc: Arc<dyn RpcLane>,
    timing: PushTiming,
    queue_capacity: usize,
}

fn header_has_token(
    request: &Request<Incoming>,
    name: hyper::header::HeaderName,
    token: &str,
) -> bool {
    request
        .headers()
        .get_all(name)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .any(|item| item.trim().eq_ignore_ascii_case(token))
}

fn empty(status: StatusCode) -> Response<PushBody> {
    let mut response = Response::new(
        Empty::<Bytes>::new()
            .map_err(|never| match never {})
            .boxed(),
    );
    *response.status_mut() = status;
    response
}

impl PushFrontDoor {
    /// `upstream`: the Python server (`host:port`); `secret_key`: the Flask
    /// secret, supplied by the integrating host's secret authority.
    pub fn new(upstream: &str, secret_key: &str) -> Self {
        let hub = Arc::new(PushHub::new());
        Self {
            bridge: Bridge::new(upstream, secret_key, Arc::clone(&hub)),
            hub,
            rpc: Arc::new(BridgeRpcLane),
            timing: PushTiming::default(),
            queue_capacity: client::resolve_event_queue_capacity(
                std::env::var("TOFU_PUSH_EVENT_QUEUE_CAPACITY")
                    .ok()
                    .as_deref(),
            ),
        }
    }

    /// Replace the default (Python-relayed) control-RPC lane.
    pub fn with_rpc_lane(mut self, rpc: Arc<dyn RpcLane>) -> Self {
        self.rpc = rpc;
        self
    }

    /// Test seam for the keepalive and receive-idle windows.
    pub fn with_timing(mut self, timing: PushTiming) -> Self {
        self.timing = timing;
        self
    }

    pub fn with_queue_capacity(mut self, capacity: usize) -> Self {
        self.queue_capacity = capacity;
        self
    }

    pub fn hub(&self) -> &Arc<PushHub> {
        &self.hub
    }

    /// A WebSocket handshake for `/api/push`. Anything else (including
    /// `/api/push/debug/presence`) stays with the relay.
    pub fn accepts(request: &Request<Incoming>) -> bool {
        request.uri().path() == PUSH_PATH
            && request.method() == Method::GET
            && header_has_token(request, UPGRADE, "websocket")
            && header_has_token(request, CONNECTION, "upgrade")
            && request.headers().contains_key("sec-websocket-key")
            && request
                .headers()
                .get("sec-websocket-version")
                .is_some_and(|version| version == "13")
    }

    pub async fn handle(
        &self,
        mut request: Request<Incoming>,
        peer: SocketAddr,
    ) -> Response<PushBody> {
        let snapshot = admit_request(request.headers(), request.uri().query(), peer);
        let admitted = match admit(&self.hub, &self.bridge, snapshot, self.queue_capacity).await {
            Ok(admitted) => admitted,
            Err(status) => {
                return empty(StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY));
            }
        };
        let key = request
            .headers()
            .get("sec-websocket-key")
            .map(|key| derive_accept_key(key.as_bytes()))
            .unwrap_or_default();
        let mut response = empty(StatusCode::SWITCHING_PROTOCOLS);
        let headers = response.headers_mut();
        headers.insert(UPGRADE, HeaderValue::from_static("websocket"));
        headers.insert(CONNECTION, HeaderValue::from_static("Upgrade"));
        if let Ok(accept) = HeaderValue::from_str(&key) {
            headers.insert(SEC_WEBSOCKET_ACCEPT, accept);
        }
        if let Some(selected) = admitted
            .subprotocol
            .as_deref()
            .and_then(|value| HeaderValue::from_str(value).ok())
        {
            headers.insert(SEC_WEBSOCKET_PROTOCOL, selected);
        }
        let upgrade = hyper::upgrade::on(&mut request);
        let hub = Arc::clone(&self.hub);
        let bridge = Arc::clone(&self.bridge);
        let rpc = Arc::clone(&self.rpc);
        let timing = self.timing;
        tokio::spawn(async move {
            match upgrade.await {
                Ok(upgraded) => {
                    let socket = WebSocketStream::from_raw_socket(
                        TokioIo::new(upgraded),
                        Role::Server,
                        Some(websocket_config()),
                    )
                    .await;
                    serve_socket(socket, admitted, hub, bridge, rpc, timing).await;
                }
                Err(_) => {
                    // The client vanished during the upgrade: release the
                    // registration exactly as a closed socket would.
                    admitted.client.disconnect();
                    for change in hub.unregister(admitted.id) {
                        admitted.link.send_mirror(&change);
                    }
                    admitted
                        .link
                        .send(serde_json::json!({"op": "close", "socket": admitted.id}));
                }
            }
        });
        response
    }

    /// Shutdown fence: refuse new sockets and release every open one with
    /// 1001 so the front door's connection drain is not held by push.
    pub fn shutdown(&self) -> usize {
        self.hub.close_for_shutdown()
    }
}
