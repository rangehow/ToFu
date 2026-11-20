//! Stdio binding of Tofu's channel protocol: JSON-RPC 2.0, one JSON object per line.
//!
//! Responsibility: a Rust port of `lib/channels/protocol/stdio.py` (the
//! oracle) with the same wire bytes and the same answers. Both sides share one
//! endpoint ([`peer::Peer`]): answers resolve pending requests by ID on the
//! reader thread; incoming requests and notifications run in arrival order on
//! one dispatcher thread, so a nested call cannot deadlock. A request ends with
//! its answer or with the binding; the protocol adds no timeout.
//!
//! Entry points: [`link::StdioLink`] (core side: spawns and reaps one adapter
//! process group), [`serve::serve`] (adapter side), [`peer::Peer`], and the
//! `tofu-channel-stdio-link` binary that puts the Rust core side behind
//! Python's `AdapterHost` (`lib/channels/protocol/rust_stdio.py`).
//! Dependencies: `contracts/channel_protocol_v1.schema.json` (`x-methods`,
//! `x-errors`, embedded at build time), `serde_json`, `libc`.

pub mod contract;
pub mod link;
pub mod peer;
pub mod pyjson;
pub mod serve;
mod threads;

pub use peer::{HandlerError, Peer, PeerClient, ProtocolError};
