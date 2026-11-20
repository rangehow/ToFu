//! The engine's side-effect boundary. The loop (`session`) talks to the
//! outside world only through these traits; `host` binds them to the
//! provider network, the tool registry and the storage.v2 client
//! (`packages/tofu-storage-client`), `memory_store` and `replay` bind them to
//! a scripted session for tests and the parity harness.

use serde_json::Value;

use super::cancel::CancelToken;
use super::types::{EffectClass, Message, ProviderOutcome, ToolCall, ToolRun};

/// One provider request as the loop issues it. The wire bytes are produced
/// by the provider adapter (the P4-wire layer of this crate), never here.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderRequest {
    pub model: String,
    pub messages: Vec<Message>,
    /// 1-based model round inside the attempt.
    pub round: u32,
}

/// Incremental output observed while a request streams.
#[derive(Debug, Clone, PartialEq)]
pub enum StreamItem {
    Text(String),
    /// A tool call whose arguments finished streaming (announced live).
    ToolCall(super::types::ToolCall),
}

pub trait ProviderPort: Send + Sync {
    fn stream(
        &self,
        request: &ProviderRequest,
        on_item: &mut dyn FnMut(StreamItem),
        cancel: &CancelToken,
    ) -> ProviderOutcome;
}

pub trait ToolPort: Send + Sync {
    fn effect_class(&self, name: &str) -> EffectClass;
    /// Runs one call. Must observe `cancel` and report `Cancelled{entered}`
    /// itself; the engine never abandons a running call.
    fn run(&self, call: &ToolCall, cancel: &CancelToken) -> ToolRun;
}

/// A storage failure as the storage.v2 client reports it
/// (`tofu_storage_client::StorageError`): the code is the contract code,
/// `transient` is `is_transient()`, `not_dispatched` is the literal
/// `request_not_dispatched == true`.
#[derive(Debug, Clone, PartialEq)]
pub struct StoreError {
    pub code: String,
    pub message: String,
    pub transient: bool,
    pub not_dispatched: bool,
}

/// Durable operations the engine needs. The storage adapter compiles these
/// values into existing semantic operations over StorageV2Client (which owns
/// write-time redaction); it never forwards a guessed engine envelope.
/// Terminal event and full projection commit together. Checkpoints first
/// create a pending archive, then atomically adopt its completed view/receipt.
/// Steer reads observe committed records; confirmation and requeue borrow
/// the application's existing projection and scheduled-Turn admission owners.
pub trait DurableStore: Send + Sync {
    /// Commit a tool-effect intent before the effect enters.
    fn commit_tool_intent(&self, intent: &Value) -> Result<(), StoreError>;
    /// Commit a tool-effect settlement (result already redacted).
    fn commit_tool_settlement(&self, settlement: &Value) -> Result<(), StoreError>;
    /// Commit a round checkpoint (the protocol-valid history boundary).
    fn save_checkpoint(&self, checkpoint: &Value) -> Result<(), StoreError>;
    /// Commit non-terminal events of a round.
    fn record_events(&self, events: &[Value]) -> Result<(), StoreError>;
    /// Commit the terminal event and the Turn's terminal projection together.
    fn settle_terminal(&self, terminal: &Value, projection: &Value) -> Result<(), StoreError>;
    /// Steers committed for this attempt and not yet consumed, in order.
    fn take_steers(&self) -> Result<Vec<Value>, StoreError>;
    /// Persist delivery against the original block identities after a verified
    /// provider return. The host owns the existing steer projection writer.
    fn confirm_steers(&self, steers: &[Value]) -> Result<(), StoreError>;
    /// Return unconsumed steers to the conversation queue (failed settlement).
    fn requeue_steers(&self, steers: &[Value]) -> Result<(), StoreError>;
}

/// Where emitted events go after their durable commit (SSE/push mirrors).
pub trait EventSink: Send + Sync {
    fn publish(&self, event: &Value);
}
