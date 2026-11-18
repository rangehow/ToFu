//! Evidence-only Rust prototype of Tofu's agent round.
//!
//! Responsibility: measure what the hot core of a Tofu round costs when it is
//! written in Rust, with the same request bytes and the same durable writes.
//! It is not wired into the product; `docs/RUST_REWRITE_EVIDENCE.md`, "Core
//! loop prototype", records what it measured and what it does not cover.
//!
//! Modules: `canonical` (stored history → canonical body, the `build_body`
//! subset), `wire` (tool preflight, Anthropic/OpenAI/Responses emitters,
//! sealing), `provider` (HTTP/1.1 + SSE against the harness stub), `tools`
//! (noop read and `run_command`), `store` (TofuDB storage.v2 client over
//! `tofu_db::protocol`), `round` (the agent loop and its durable events).

pub mod canonical;
pub mod provider;
pub mod pyjson;
pub mod round;
pub mod store;
pub mod tools;
pub mod wire;
