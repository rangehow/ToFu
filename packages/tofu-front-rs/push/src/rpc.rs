//! The control-RPC lane boundary on `/api/push` (P1f owns its Rust session).
//!
//! Inbound order is `routes/push.py::_receiver`'s: every frame is offered to
//! the RPC lane first (`ControlRpcSession.receive`), and only frames it
//! declines reach the push action handler. Responses go through
//! [`SocketHandle::enqueue_rpc`], the priority lane after pongs and before
//! events (`PushClient.enqueue_rpc`).
//!
//! [`BridgeRpcLane`] is the default until the Rust session lands: it relays
//! every JSON-RPC frame to a Python `ControlRpcSession` bound to the socket
//! over the bridge, so `contracts/control_rpc_v1.yaml` limits and error
//! codes stay Python's.

use crate::bridge::BridgeLink;
use crate::client::PushClient;
use crate::hub::SocketId;
use serde_json::{json, Value};
use std::sync::Arc;

/// One admitted socket as the RPC lane sees it.
#[derive(Clone)]
pub struct SocketHandle {
    pub id: SocketId,
    pub user_id: String,
    pub req_id: String,
    client: Arc<PushClient>,
    link: Arc<BridgeLink>,
}

impl SocketHandle {
    pub(crate) fn new(id: SocketId, client: Arc<PushClient>, link: Arc<BridgeLink>) -> Self {
        Self {
            id,
            user_id: client.user_id.clone(),
            req_id: client.req_id.clone(),
            client,
            link,
        }
    }

    /// Queue one encoded response; `false` once the socket disconnected.
    pub fn enqueue_rpc(&self, text: Arc<str>) -> bool {
        self.client.enqueue_rpc(text)
    }

    pub(crate) fn enqueue_control(&self, text: Arc<str>) {
        self.client.enqueue_control(text);
    }

    pub(crate) fn link(&self) -> &BridgeLink {
        &self.link
    }
}

pub trait RpcLane: Send + Sync {
    /// Consume one inbound frame; `false` hands it to the push actions.
    fn receive(&self, socket: &SocketHandle, frame: &Value) -> bool;
    /// The socket ended; cancel and release what it owns.
    fn close(&self, socket: &SocketHandle);
}

/// Python's `ControlRpcSession.receive` claims every object carrying
/// `jsonrpc`, valid or not; the Python session answers the invalid ones.
pub struct BridgeRpcLane;

impl RpcLane for BridgeRpcLane {
    fn receive(&self, socket: &SocketHandle, frame: &Value) -> bool {
        if !frame
            .as_object()
            .is_some_and(|map| map.contains_key("jsonrpc"))
        {
            return false;
        }
        socket
            .link()
            .send(json!({"op": "rpc", "socket": socket.id, "frame": frame}));
        true
    }

    fn close(&self, _socket: &SocketHandle) {
        // The bridge `close` op already ends the Python session of the socket.
    }
}
