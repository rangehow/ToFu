//! Demand-created exclusive sessions with bounded idle reuse
//! (`lib/storage/session_pool.py`).
//!
//! The size bounds retained idle sockets only, never admission: every caller
//! immediately claims an idle session or creates one under its own attempt
//! deadline. An idle session is reused only inside the freshness cap and
//! after its liveness probe; a broken session is never reused. Nothing here
//! is sized from the host (no CPU- or memory-derived width).

use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::error::{StorageError, StorageResult};
use crate::session::{Session, TransportResult};

#[derive(Default)]
struct PoolState {
    idle: Vec<(Session, Instant)>,
    size: usize,
    live: usize,
    retiring: usize,
    completed: u64,
    closed: bool,
}

/// Counters of `SessionPool.resource_snapshot`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PoolSnapshot {
    pub capacity: usize,
    pub used: usize,
    pub idle: usize,
    pub retiring: usize,
    pub completed: u64,
}

pub struct SessionPool {
    state: Mutex<PoolState>,
    idle_cap: Duration,
}

/// How an acquired session left the factory, distinguishing pre-dispatch
/// pool fences from transport failures while opening a new session.
pub enum Acquire {
    Session(Session),
    Transport(crate::session::TransportError),
}

impl SessionPool {
    pub fn new(size: usize, idle_cap: Duration) -> Self {
        assert!(size >= 1, "idle session target must be positive");
        Self {
            state: Mutex::new(PoolState {
                size,
                ..PoolState::default()
            }),
            idle_cap,
        }
    }

    fn lock(&self) -> MutexGuard<'_, PoolState> {
        self.state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn is_closed(&self) -> bool {
        self.lock().closed
    }

    fn closed_error() -> StorageError {
        StorageError::retryable("database_unavailable", "storage client is closed", 100)
            .not_dispatched()
    }

    fn budget_error() -> StorageError {
        StorageError::retryable("database_timeout", "storage connection budget elapsed", 25)
            .not_dispatched()
    }

    fn retire(&self, session: Session) {
        session.close();
        drop(session);
        let mut state = self.lock();
        state.live -= 1;
        state.retiring -= 1;
    }

    fn discard_reserved(&self, session: Option<Session>) {
        let mut state = self.lock();
        match session {
            Some(session) => {
                state.retiring += 1;
                drop(state);
                self.retire(session);
            }
            None => state.live -= 1,
        }
    }

    /// Claim immediately, then validate the exclusive socket unlocked.
    pub fn acquire(
        &self,
        timeout: Duration,
        factory: impl Fn(Duration) -> TransportResult<Session>,
    ) -> StorageResult<Acquire> {
        let deadline = Instant::now() + timeout.max(Duration::from_millis(1));
        loop {
            let claimed = {
                let mut state = self.lock();
                if state.closed {
                    return Err(Self::closed_error());
                }
                match state.idle.pop() {
                    Some(idle) => Some(idle),
                    None => {
                        state.live += 1;
                        None
                    }
                }
            };
            let (session, reusable) = match claimed {
                None => {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        self.discard_reserved(None);
                        return Err(Self::budget_error());
                    }
                    match factory(remaining) {
                        Ok(session) => (session, true),
                        Err(error) => {
                            self.discard_reserved(None);
                            return Ok(Acquire::Transport(error));
                        }
                    }
                }
                Some((session, released_at)) => {
                    let fresh = released_at.elapsed() <= self.idle_cap;
                    let reusable = fresh && session.probe_liveness();
                    (session, reusable)
                }
            };
            let fence = {
                let state = self.lock();
                if state.closed {
                    Some(Self::closed_error())
                } else if Instant::now() >= deadline {
                    Some(Self::budget_error())
                } else {
                    None
                }
            };
            if let Some(error) = fence {
                self.discard_reserved(Some(session));
                return Err(error);
            }
            if reusable {
                return Ok(Acquire::Session(session));
            }
            self.discard_reserved(Some(session));
        }
    }

    pub fn release(&self, session: Session, broken: bool) {
        let mut state = self.lock();
        state.completed += 1;
        let retire = broken || state.closed || state.idle.len() >= state.size;
        if retire {
            state.retiring += 1;
            drop(state);
            self.retire(session);
        } else {
            state.idle.push((session, Instant::now()));
        }
    }

    /// Resize only idle retention; socket teardown runs outside the lock.
    pub fn resize(&self, size: usize) {
        if size < 1 {
            return;
        }
        let retired: Vec<Session> = {
            let mut state = self.lock();
            state.size = size;
            let mut retired = Vec::new();
            while state.idle.len() > size {
                let (session, _) = state.idle.pop().expect("idle session");
                state.retiring += 1;
                retired.push(session);
            }
            retired
        };
        for session in retired {
            self.retire(session);
        }
    }

    pub fn close(&self) {
        let idle = {
            let mut state = self.lock();
            state.closed = true;
            let idle = std::mem::take(&mut state.idle);
            state.retiring += idle.len();
            idle
        };
        for (session, _) in idle {
            self.retire(session);
        }
    }

    pub fn snapshot(&self) -> PoolSnapshot {
        let state = self.lock();
        PoolSnapshot {
            capacity: state.size,
            used: state.live - state.idle.len(),
            idle: state.idle.len(),
            retiring: state.retiring,
            completed: state.completed,
        }
    }
}
