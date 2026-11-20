//! Execution engine: the Rust owner of a Turn's agent loop (Phase 4 of
//! `docs/RUST_REWRITE_EVIDENCE.md`), replacing `lib/tasks_pkg` and the
//! execution part of `lib/agent_core` once parity is proven.
//!
//! Responsibility: run one accepted Turn attempt round by round — provider
//! request, stream outcome, tool dispatch with tool-effect intents, round
//! checkpoint, steer injection, compaction admission, cancellation — and
//! settle it exactly once. The core is sans-IO and deterministic: every side
//! effect goes through a port (`ports`). The parity boundary (`replay`)
//! runs scripted sessions; no resident host or provider route is installed.
//!
//! Policy modules (pure, each pinned by the Python owner it replaces):
//! `abort` (ABORT_CAUSES), `settlement` (monotonic terminal, commit-then-
//! publish, terminal replay), `tool_effect` (intent before entry, outcome
//! classes, restart classification), `fallback` (content-filter fallback and
//! return to the primary), `steer` (queue, receipt bound, requeue/bounce),
//! `checkpoint` (rewind to the protocol-valid boundary, self-drain save),
//! `compaction` (admission without head truncation), and `redaction`
//! (credential redaction at tool settlement). `session` composes them into the loop.
//!
//! Dependencies: `tofu-contracts` (events_v1 types), the provider wire of this
//! crate, and the storage.v2 client through `ports::DurableStore`.

pub mod abort;
pub mod cancel;
pub mod checkpoint;
pub mod compaction;
pub mod events;
pub mod fallback;
pub mod intent_stall;
pub mod memory_store;
pub mod ports;
pub mod provider_wire;
pub mod redaction;
pub mod replay;
pub mod session;
pub mod settlement;
pub mod steer;
pub mod storage_v2;
pub mod tool_effect;
pub mod types;
