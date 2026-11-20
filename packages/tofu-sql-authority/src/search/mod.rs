//! The conversation turn-search projection (`turn_search_projection.py`,
//! `turn_search_query.py`, `turn_search_sources.py`, `turn_search_chunks.py`
//! and the `SQLiteBackend` hooks of `adapters/sqlite.py`).
//!
//! A reconstructible SQLite under `<data>/projections/` (or
//! `TOFU_TURN_SEARCH_PROJECTION_DIR`) is fed by one background worker from
//! the authority's `storage_projection_outbox`; `conversation.search` reads
//! only that store. Its failure degrades search only, never authority
//! readiness or a user write.

mod chunks;
mod query;
mod runtime;
mod sources;
mod target;
mod text;

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde_json::{json, Value};

use crate::backend::BackendCore;
use crate::error::{OpResult, StorageError};
use crate::payload::Payload;

pub use runtime::{Runtime, PROJECTION_NAME};

/// `Path(value).resolve()` for the projection directory setting.
pub fn resolve_directory(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// The backend's `_turn_search_projection` / `_turn_search_projection_error`.
#[derive(Default)]
pub struct Slot {
    runtime: Mutex<Option<Arc<Runtime>>>,
    error: Mutex<String>,
}

impl Slot {
    /// `SQLiteBackend.start`'s projection start; a failure is recorded, never raised.
    pub fn start(&self, backend: &Arc<BackendCore>) {
        let config = &backend.config;
        let target = target::LocalTarget::new(config.turn_search_projection_dir.clone(), config.read_pool_size);
        match Runtime::start(Arc::clone(backend), target, config.turn_search_backfill_delay_s) {
            Ok(runtime) => *self.runtime.lock().expect("search slot") = Some(runtime),
            Err(name) => {
                eprintln!("[turn-search] local projection failed to start; conversation search is degraded");
                *self.error.lock().expect("search slot") = name;
            }
        }
    }

    fn current(&self) -> Option<Arc<Runtime>> {
        self.runtime.lock().expect("search slot").clone()
    }

    /// `projection.wake()` after each committed command.
    pub fn wake(&self) {
        if let Some(runtime) = self.current() {
            runtime.wake();
        }
    }

    /// `health()['turn_search_projection']`, absent when neither exists.
    pub fn health(&self) -> Option<Value> {
        if let Some(runtime) = self.current() {
            return Some(runtime.status());
        }
        let error = self.error.lock().expect("search slot").clone();
        if error.is_empty() {
            return None;
        }
        Some(json!({"state": "unavailable", "error_type": error}))
    }

    /// `metrics()['turn_search_projection']`.
    pub fn metrics(&self) -> Value {
        match self.current() {
            Some(runtime) => runtime.status(),
            None => json!({"state": "unavailable", "error_type": self.error.lock().expect("search slot").clone()}),
        }
    }

    pub fn close(&self) {
        let runtime = self.runtime.lock().expect("search slot").take();
        if let Some(runtime) = runtime {
            runtime.close();
        }
    }
}

thread_local! {
    static SCOPE: RefCell<Option<(Option<Arc<Runtime>>, Instant)>> = const { RefCell::new(None) };
}

/// Exposes the backend's projection and the caller deadline to the
/// `conversation.search` handler for the duration of one authority query.
pub struct QueryScope {
    previous: Option<(Option<Arc<Runtime>>, Instant)>,
}

impl QueryScope {
    pub fn enter(slot: &Slot, deadline: Instant) -> Self {
        let previous = SCOPE.with(|scope| scope.replace(Some((slot.current(), deadline))));
        Self { previous }
    }
}

impl Drop for QueryScope {
    fn drop(&mut self) {
        let previous = self.previous.take();
        SCOPE.with(|scope| *scope.borrow_mut() = previous);
    }
}

/// `SQLiteBackend.query('conversation.search', ...)`: the projection query
/// plus `attach_search_coverage`.
pub fn conversation_search(payload: &Payload) -> OpResult<Value> {
    let scope = SCOPE.with(|scope| scope.borrow().clone());
    let Some((Some(runtime), deadline)) = scope else {
        return Err(StorageError::retryable(
            "database_unavailable",
            "Conversation search projection is unavailable",
            250,
        )
        .into());
    };
    let result = runtime.query(|session| query::search_conversations(session, payload, deadline), deadline)?;
    Ok(query::attach_search_coverage(result, &runtime.search_status()))
}
