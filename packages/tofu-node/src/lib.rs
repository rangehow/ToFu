//! Tofu node program: every machine runs this one program and is an equal
//! node of each Home it joined. See docs/modules/remote_execution.md
//! ("Nodes and pairing", "Device routing and permissions") and
//! packages/tofu-node/README.md.

pub mod commands;
pub mod config;
pub mod contract;
pub mod host_identity;
pub mod http;
pub mod journal;
pub mod links;
pub mod log;
pub mod pairing;
pub mod permissions;
pub mod poll;
pub mod relay;
pub mod runtime;
pub mod tray;
pub mod util;
