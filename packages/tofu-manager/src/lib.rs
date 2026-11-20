#![recursion_limit = "512"]
//! Rust lifecycle manager: Phase 2 of `docs/RUST_REWRITE_EVIDENCE.md`.
//!
//! Responsibility: own one project's desired/observed server state exactly
//! like `server_manager.LifecycleManager` behind the `supervisor.py` HTTP
//! protocol, so `serverctl.py`, the application's restart door and the
//! Android shell talk to it unchanged.
//!
//! Entry points: [`http::serve`] (the `tofu-manager manager` subcommand) and
//! [`manager::LifecycleManager`]. Dependencies: the standard library, libc,
//! serde_json and sha2; Python policy owners are reached only through
//! `serverctl_pkg/manager_bridge.py` ([`bridge`]) and the existing
//! preparation children ([`preparation`]).
//!
//! Threads: one accept loop, one monitor per project, one thread per accepted
//! control request (demand-driven, never sized from the host) and the joined
//! children of a preparation. Nothing here reads the CPU count.

pub mod bridge;
pub mod certification;
pub mod child;
pub mod datapaths;
pub mod envpolicy;
pub mod fingerprint;
pub mod http;
pub mod json;
pub mod lockstatus;
pub mod log;
pub mod manager;
pub mod ownership;
pub mod preparation;
pub mod probe;
pub mod procfs;
pub mod resident;
pub mod restart_preparation;
pub mod runtime;
pub mod timing;
