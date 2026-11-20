//! Explicit cancellation: a shared token the loop and every port observe.
//!
//! A Stop is *accepted* when the token is cancelled; the Turn is *settled*
//! only when the loop commits its terminal (`settlement`). Dropping a future
//! or a thread never ends a tool effect silently: ports check the token and
//! report `ToolRun::Cancelled { entered }` themselves.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

#[derive(Clone, Default)]
pub struct CancelToken {
    inner: Arc<Inner>,
}

#[derive(Default)]
struct Inner {
    cancelled: AtomicBool,
    reason: Mutex<Option<String>>,
}

impl CancelToken {
    pub fn new() -> Self {
        Self::default()
    }

    /// Request cancellation with an abort reason (looked up in
    /// `abort::ABORT_CAUSES` at settlement). The first reason wins.
    /// Returns true when this call accepted the Stop.
    pub fn cancel(&self, reason: &str) -> bool {
        let mut slot = self.inner.reason.lock().unwrap_or_else(|e| e.into_inner());
        if self.inner.cancelled.swap(true, Ordering::SeqCst) {
            return false;
        }
        *slot = Some(reason.to_string());
        true
    }

    pub fn is_cancelled(&self) -> bool {
        self.inner.cancelled.load(Ordering::SeqCst)
    }

    pub fn reason(&self) -> Option<String> {
        self.inner
            .reason
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_stop_is_accepted_and_its_reason_kept() {
        let token = CancelToken::new();
        assert!(token.cancel("user_stop"));
        assert!(!token.cancel("restart"));
        assert!(token.is_cancelled());
        assert_eq!(token.reason().as_deref(), Some("user_stop"));
    }
}
