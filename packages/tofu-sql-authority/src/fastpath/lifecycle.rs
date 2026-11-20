//! Commit-notified one-second WAL shipping, joined before writer teardown.
use super::{storage, Fastpath};
use crate::error::{OpResult, StorageError};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub(super) struct Lifecycle {
    wake: Arc<(Mutex<(bool, bool)>, Condvar)>,
    worker: Mutex<Option<JoinHandle<()>>>,
}
impl Lifecycle {
    pub(super) fn new() -> Self {
        Self { wake: Arc::new((Mutex::new((false, false)), Condvar::new())), worker: Mutex::new(None) }
    }
}
impl Fastpath {
    /// The checkpoint callback acquires the backend writer before snapshot().
    /// It must capture a Weak backend so the shipper cannot keep its owner alive.
    pub fn start(self: &Arc<Self>, checkpoint: Arc<dyn Fn(Instant) -> OpResult<()> + Send + Sync>) -> OpResult<()> {
        let mut worker = self.lifecycle.worker.lock().expect("shipper worker");
        if self.lifecycle.wake.0.lock().expect("shipper lifecycle").0 {
            return Err(StorageError::new("database_unavailable", "WAL shipper is closed").into());
        }
        if worker.is_some() {
            return Ok(());
        }
        let owner = Arc::downgrade(self);
        let wake = Arc::clone(&self.lifecycle.wake);
        *worker = Some(
            thread::Builder::new()
                .name("storage-wal-shipper".into())
                .spawn(move || loop {
                    let (lock, condition) = &*wake;
                    let mut flags = lock.lock().expect("shipper lifecycle");
                    if !flags.0 && !flags.1 {
                        flags = condition.wait_timeout(flags, Duration::from_secs(1)).expect("shipper wait").0;
                    }
                    if flags.0 {
                        break;
                    }
                    flags.1 = false;
                    drop(flags);
                    let Some(owner) = owner.upgrade() else { break };
                    let needs_snapshot = {
                        let state = owner.state.lock().expect("fastpath state");
                        !state.ready
                            || storage::size(&storage::appended(&owner.local_db, "-wal"))
                                .is_ok_and(|bytes| bytes >= owner.rebase_trigger)
                    };
                    let result = if needs_snapshot {
                        checkpoint(Instant::now() + Duration::from_secs(30))
                    } else {
                        owner.ship()
                    };
                    if let Err(error) = result {
                        eprintln!("[fastpath] WAL ship pass failed: {error:?}");
                    }
                })
                .map_err(storage::io)?,
        );
        Ok(())
    }
    pub fn notify(&self) {
        let mut flags = self.lifecycle.wake.0.lock().expect("shipper lifecycle");
        flags.1 = true;
        self.lifecycle.wake.1.notify_one();
    }
    pub fn close(&self) -> OpResult<()> {
        {
            let mut flags = self.lifecycle.wake.0.lock().expect("shipper lifecycle");
            flags.0 = true;
            self.lifecycle.wake.1.notify_one();
        }
        let worker = self.lifecycle.worker.lock().expect("shipper worker").take();
        if let Some(worker) = worker {
            worker.join().map_err(|_| StorageError::new("database_internal", "WAL shipper worker panicked"))?;
            self.ship()?;
        }
        Ok(())
    }
    pub fn metrics(&self) -> serde_json::Value {
        self.status()
    }
}
