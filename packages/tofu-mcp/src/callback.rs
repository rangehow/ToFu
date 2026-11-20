//! Host-to-binding calls: the Rust client asks the process that drives it
//! (the Python binding, `lib/mcp/rust_host.py`) for facts owned elsewhere —
//! the shared network authority's route plan (`docs/NETWORK.md`), credential
//! stores, browser devices — instead of keeping a second copy of that state.
//!
//! Wire (inside the JSON-lines host protocol): the host writes
//! `{"call": <n>, "method", "params"}`; the binding answers
//! `{"reply": <n>, "result"}` or `{"reply": <n>, "error": {"kind", "message"}}`.
//! A binding with no handler answers kind `unavailable`. When the binding goes
//! away every waiting call fails with kind `transport`.

use crate::errors::CallError;
use async_trait::async_trait;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use tokio::sync::{mpsc, oneshot};

/// Something that answers host-to-binding calls.
#[async_trait]
pub trait BindingCalls: Send + Sync {
    async fn ask(&self, method: &str, params: Value) -> Result<Value, CallError>;
}

/// No binding attached: every call is `unavailable` (standalone use, unit tests).
pub struct NoBinding;

#[async_trait]
impl BindingCalls for NoBinding {
    async fn ask(&self, method: &str, _params: Value) -> Result<Value, CallError> {
        Err(CallError::Unavailable(format!(
            "no binding answers {method}"
        )))
    }
}

/// The host side of the call channel.
pub struct CallbackChannel {
    out: mpsc::UnboundedSender<Value>,
    next: AtomicU64,
    waiting: Mutex<BTreeMap<u64, oneshot::Sender<Result<Value, CallError>>>>,
}

impl CallbackChannel {
    pub fn new(out: mpsc::UnboundedSender<Value>) -> Self {
        CallbackChannel {
            out,
            next: AtomicU64::new(1),
            waiting: Mutex::new(BTreeMap::new()),
        }
    }

    /// Settle the call a `{"reply": n, ...}` message answers; unknown replies are dropped.
    pub fn resolve(&self, message: &Value) {
        let Some(id) = message.get("reply").and_then(Value::as_u64) else {
            return;
        };
        let waiter = self
            .waiting
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .remove(&id);
        let Some(waiter) = waiter else {
            return;
        };
        let outcome = match message.get("error") {
            Some(error) => Err(CallError::from_kind(
                error.get("kind").and_then(Value::as_str).unwrap_or("error"),
                error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )),
            None => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
        };
        let _ = waiter.send(outcome);
    }

    /// Fail every waiting call: the binding is gone.
    pub fn close(&self) {
        let waiting = std::mem::take(&mut *self.waiting.lock().unwrap_or_else(|p| p.into_inner()));
        for (_, waiter) in waiting {
            let _ = waiter.send(Err(CallError::Transport("the binding closed".into())));
        }
    }
}

#[async_trait]
impl BindingCalls for CallbackChannel {
    async fn ask(&self, method: &str, params: Value) -> Result<Value, CallError> {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = oneshot::channel();
        self.waiting
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(id, sender);
        if self
            .out
            .send(json!({"call": id, "method": method, "params": params}))
            .is_err()
        {
            self.waiting
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .remove(&id);
            return Err(CallError::Transport("the binding closed".into()));
        }
        receiver
            .await
            .unwrap_or_else(|_| Err(CallError::Transport("the binding closed".into())))
    }
}
