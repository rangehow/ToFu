//! `tofu-sql-authority`: the SQL storage authority (storage.v1) ported from
//! `lib/storage_authority_sql`. See `docs/RUST_REWRITE_EVIDENCE.md` (P3b) for
//! the port status and `src/ops/mod.rs` for the per-domain registry.

pub mod attempt_event_metrics;
pub mod backend;
pub mod backup;
pub mod codec;
pub mod control;
pub mod error;
pub mod fastpath;
pub mod faults;
pub mod json;
pub mod logical_outbox;
pub mod mount;
pub mod ops;
pub mod payload;
pub mod receipt;
pub mod schema;
pub mod search;
pub mod server;
pub mod session;
pub mod turn_projection_cache;
pub mod zlib;
