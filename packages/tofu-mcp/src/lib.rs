//! Rust port of Tofu's MCP client (`lib/mcp`), Phase 5 of
//! `docs/RUST_REWRITE_EVIDENCE.md`. The Python package is the oracle; `tests/`
//! and the `tests/mcp_protocol/test_rust_*.py` suites hold the parity cases
//! (`test_rust_coverage_map.py` proves every oracle file is covered).
//!
//! Owners: [`bridge`] (`call_tool` gates, catalog, lifecycle, credentials),
//! [`queue_policy`] (owner Hope queue ban, fail closed), [`stdio_env`],
//! [`launcher`] and [`vendor`] (child environment and launch), [`network`]
//! (shared network authority over [`callback`]), [`transport`] (templating,
//! redaction), [`result`], [`errors`], [`health_probe`] and
//! [`browser_credentials`], [`tool_search`], [`progressive`],
//! [`connector_guidance`], [`registry`], [`updates`], [`relay`]
//! (execution-host relay owner), [`rmcp_session`] (rmcp transports), [`host`]
//! (JSON-lines host protocol used by the Python binding).

pub mod bridge;
pub mod browser_credentials;
pub mod callback;
pub mod coerce;
pub mod connector_guidance;
pub mod errors;
pub mod generated_pyunicode;
pub mod health_probe;
pub mod host;
pub mod launcher;
pub mod names;
pub mod network;
pub mod progressive;
pub mod pyjson;
pub mod pyos;
pub mod queue_policy;
pub mod registry;
pub mod relay;
pub mod result;
pub mod rmcp_session;
pub mod stdio_env;
pub mod tool_search;
pub mod transport;
pub mod updates;
pub mod vendor;
