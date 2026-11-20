//! Rust storage.v2 client for the native Tofu-DB authority.
//!
//! The single application-side storage client for the Rust phases: the Rust
//! counterpart of `lib/storage/v2_client.py` over the engine's own framing
//! (`tofu_db::protocol`, `tofu_db::daemon::derive_auth_token`). It never
//! selects, starts or switches a backend; the authority (the `tofu-db serve`
//! daemon) owns durable state and is reached only over its authenticated
//! loopback protocol.
//!
//! Entry point: [`StorageV2Client`]. Modules:
//! - `session`: one authenticated connection (Hello, upload/response streams,
//!   pipelined reads); no retry policy.
//! - `pool`: demand-created sessions with bounded idle reuse.
//! - `client`: replay classification, backoff, deadlines, health lane,
//!   commit watches, committed-event publication and the write-redaction hook.
//! - `semantics`: owner injection, bounded progress, notice mapping.
//! - `redaction`: write-time known-credential redaction
//!   (`lib/tool_results/credential_redaction.py`) including the vault reader.
//! - `model_routing_secrets`: read-only port of the model-routing owner's
//!   decrypting read, the production `OwnerCredentialSource`.
//! - `checkpoint`: instance-bound native checkpoint control.
//! - `contract`: every constant derived from `contracts/*.json`.
//!
//! Differential evidence against the Python client:
//! `tests/storage/test_storage_v2_rust_client_differential.py`, driven
//! through the `tofu-storage-client-diff` binary.

mod checkpoint;
mod client;
pub mod contract;
mod error;
mod model_routing_secrets;
mod pool;
pub mod redaction;
pub mod semantics;
mod session;
mod timed_lock;

pub use client::{
    Cancellation, ClientConfig, RetryNotice, RuntimeRecovery, StorageObserver, StorageV2Client,
    WriteRedactor, SLOW_RPC_WARN,
};
pub use error::{StorageError, StorageResult};
pub use model_routing_secrets::ModelRoutingKnownSecrets;
pub use pool::PoolSnapshot;
pub use redaction::{
    CredentialFiles, CredentialRedactor, KnownCredentialRedactor, OwnerCredentialSource,
};
