//! Off-authority payload-segment packing.
//!
//! Staged blocks must reach durable storage before the checkpoint retires
//! the active WAL. Packing them on the commit path made rotation commits
//! stall for a full 8 MiB segment write + fsync (~16 ms p99 spikes). This
//! worker reserves a staged batch through `BlockStore::begin_background_pack`,
//! builds and fsyncs the segment file without holding the authority mutex,
//! and records the result; the commit path publishes the manifest reference
//! and retires the staged copies (`Engine::adopt_background_pack`). Every
//! failure leaves payloads staged, so the synchronous checkpoint drain
//! remains the complete deterministic fallback.

use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use crate::block::BlockStore;
use crate::engine::MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES;
use crate::payload_manifest::PayloadSegmentReference;

const WORKER_STACK_BYTES: usize = 512 * 1024;
const IDLE_INTERVAL: Duration = Duration::from_millis(25);
const ERROR_BACKOFF: Duration = Duration::from_millis(250);
/// Smallest staged batch the worker packs on its own. Batches stopped by
/// the 8 MiB segment budget always qualify (`begin_background_pack` treats
/// them as full); this floor only keeps sub-MiB tails on the foreground
/// checkpoint path, where their fsync cost stays off the latency tail.
const MINIMUM_WORKER_BATCH_BYTES: u64 = 1024 * 1024;

#[derive(Clone, Debug)]
struct ErrorSnapshot {
    kind: io::ErrorKind,
    message: String,
}

impl ErrorSnapshot {
    fn capture(error: &io::Error) -> Self {
        Self {
            kind: error.kind(),
            message: error.to_string(),
        }
    }

    fn into_error(self) -> io::Error {
        io::Error::new(self.kind, self.message)
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BackgroundPackerMetrics {
    pub packed_segments: u64,
    pub packed_bytes: u64,
    pub failed_rounds: u64,
}

#[derive(Default)]
struct PackerState {
    stopping: bool,
    metrics: BackgroundPackerMetrics,
    terminal_error: Option<ErrorSnapshot>,
}

struct Shared {
    blocks: Arc<BlockStore>,
    state: Mutex<PackerState>,
    wake: Condvar,
}

#[derive(Clone)]
pub struct BackgroundPackerObserver {
    shared: Weak<Shared>,
}

impl BackgroundPackerObserver {
    pub fn require_healthy(&self) -> io::Result<()> {
        let Some(shared) = self.shared.upgrade() else {
            return Ok(());
        };
        let state = shared
            .state
            .lock()
            .map_err(|_| io::Error::other("background packer state poisoned"))?;
        match &state.terminal_error {
            Some(error) => Err(error.clone().into_error()),
            None => Ok(()),
        }
    }
}

pub struct BackgroundPacker {
    shared: Arc<Shared>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl BackgroundPacker {
    pub fn start(blocks: Arc<BlockStore>) -> io::Result<Self> {
        let shared = Arc::new(Shared {
            blocks,
            state: Mutex::new(PackerState::default()),
            wake: Condvar::new(),
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::Builder::new()
            .name("tofu-db-background-pack".to_owned())
            .stack_size(WORKER_STACK_BYTES)
            .spawn(move || run_worker(&worker_shared))?;
        Ok(Self {
            shared,
            worker: Mutex::new(Some(worker)),
        })
    }

    pub fn observer(&self) -> BackgroundPackerObserver {
        BackgroundPackerObserver {
            shared: Arc::downgrade(&self.shared),
        }
    }

    pub(crate) fn metrics(&self) -> BackgroundPackerMetrics {
        self.shared
            .state
            .lock()
            .map(|state| state.metrics)
            .unwrap_or_default()
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        {
            let mut state = self
                .shared
                .state
                .lock()
                .map_err(|_| io::Error::other("background packer state poisoned"))?;
            state.stopping = true;
        }
        self.shared.wake.notify_all();
        let worker = self
            .worker
            .lock()
            .map_err(|_| io::Error::other("background packer worker lock poisoned"))?
            .take();
        if worker.is_some_and(|worker| worker.join().is_err()) {
            return Err(io::Error::other("tofu-db background pack worker panicked"));
        }
        Ok(())
    }
}

enum PackOutcome {
    Packed,
    Idle,
    Failed(io::Error),
    Panicked,
}

fn run_worker(shared: &Arc<Shared>) {
    loop {
        {
            let state = match shared.state.lock() {
                Ok(state) => state,
                Err(_) => return,
            };
            if state.stopping {
                return;
            }
        }
        match pack_once(shared) {
            PackOutcome::Packed => {
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.packed_segments += 1;
                }
            }
            PackOutcome::Idle => {
                if let Ok(state) = shared.state.lock() {
                    let _ = shared.wake.wait_timeout(state, IDLE_INTERVAL);
                }
            }
            PackOutcome::Failed(error) => {
                if let Ok(mut state) = shared.state.lock() {
                    state.metrics.failed_rounds += 1;
                }
                eprintln!("tofu-db background pack failed: {error}");
                if let Ok(state) = shared.state.lock() {
                    let _ = shared.wake.wait_timeout(state, ERROR_BACKOFF);
                }
            }
            PackOutcome::Panicked => {
                if let Ok(mut state) = shared.state.lock() {
                    state.terminal_error = Some(ErrorSnapshot::capture(&io::Error::other(
                        "background pack worker panicked",
                    )));
                }
                return;
            }
        }
    }
}

fn pack_once(shared: &Arc<Shared>) -> PackOutcome {
    let Some((segment_id, ids)) = (match shared.blocks.begin_background_pack(
        MINIMUM_WORKER_BATCH_BYTES,
        MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES,
    ) {
        Ok(batch) => batch,
        Err(error) => return PackOutcome::Failed(error),
    }) else {
        return PackOutcome::Idle;
    };
    let built =
        catch_unwind(AssertUnwindSafe(|| {
            let segment = shared.blocks.payload_segment_store()?.create(
                segment_id,
                ids.clone(),
                |block_id| shared.blocks.get(block_id),
            )?;
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )?;
            shared.blocks.verify_payload_segment(&reference)?;
            // The packed filter must know these IDs before any publication can
            // retire their staged copies; otherwise a post-retirement read would
            // skip the segment scan and report NotFound.
            shared.blocks.note_packed(&ids)?;
            Ok::<PayloadSegmentReference, io::Error>(reference)
        }));
    match built {
        Ok(Ok(reference)) => {
            let payload_bytes = reference.payload_bytes;
            match shared
                .blocks
                .complete_background_pack(segment_id, reference)
            {
                Ok(()) => {
                    if let Ok(mut state) = shared.state.lock() {
                        state.metrics.packed_bytes += payload_bytes;
                    }
                    PackOutcome::Packed
                }
                Err(error) => PackOutcome::Failed(error),
            }
        }
        Ok(Err(error)) => {
            let _ = shared.blocks.fail_background_pack(segment_id);
            PackOutcome::Failed(error)
        }
        Err(_) => {
            let _ = shared.blocks.fail_background_pack(segment_id);
            PackOutcome::Panicked
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_packs_staged_payloads_off_the_authority() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        // The worker only takes batches at the materialization threshold;
        // smaller staged sets stay with the foreground checkpoint path.
        let mut staged = Vec::new();
        for slot in 0..9_u8 {
            let payload = vec![slot; 1024 * 1024];
            staged.push((store.put_deferred(&payload).unwrap().0, payload));
        }

        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        let mut published = None;
        for _ in 0..200 {
            if let Some((reference, packed)) = store.take_completed_background_pack().unwrap() {
                published = Some((reference, packed));
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        let metrics = packer.metrics();
        let (reference, packed) = published.unwrap_or_else(|| {
            panic!(
                "worker publishes the staged batch: metrics={metrics:?} creating={:?}",
                store.background_pack_creating()
            )
        });
        // The reservation stops at the 8 MiB batch bound, so exactly eight
        // of the nine 1 MiB blocks ride the background segment.
        assert_eq!(packed.len(), 8);
        // The completed segment is a real durable file that passes the same
        // verification the foreground publication path requires.
        store.verify_payload_segment(&reference).unwrap();
        for (id, payload) in &staged {
            assert_eq!(&store.get(*id).unwrap(), payload);
        }

        let metrics = packer.metrics();
        assert_eq!(metrics.packed_segments, 1);
        assert!(metrics.packed_bytes > 0);
        assert_eq!(metrics.failed_rounds, 0);
        packer.observer().require_healthy().unwrap();
        packer.stop_and_join().unwrap();
        // A second join is a no-op rather than an error.
        packer.stop_and_join().unwrap();
    }

    #[test]
    fn worker_stays_idle_without_staged_payloads() {
        let directory = tempfile::tempdir().unwrap();
        let store = Arc::new(BlockStore::initialize(directory.path()).unwrap());
        let packer = BackgroundPacker::start(Arc::clone(&store)).unwrap();
        thread::sleep(Duration::from_millis(100));
        let metrics = packer.metrics();
        assert_eq!(metrics.packed_segments, 0);
        assert_eq!(metrics.failed_rounds, 0);
        packer.observer().require_healthy().unwrap();
        packer.stop_and_join().unwrap();
    }
}
