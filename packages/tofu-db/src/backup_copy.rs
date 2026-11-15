//! Post-order, parallel copy of a content-addressed block graph from one
//! block store into another: online backup epochs and offline restore
//! (docs/STORAGE_OPERATIONS.md, "Native online backup").
//!
//! A block is published in the destination only after every block it
//! references is published there and that block's shard directory barrier
//! has returned. A destination written only this way is *closed*: it holds
//! the whole graph below every block it holds. A copy into a closed store
//! therefore skips the subgraph of every block already present, so its work
//! follows the blocks that are new since the store's last copy, and an
//! interrupted copy keeps every subgraph it finished.
//!
//! Workers, one per hash-shard directory of the block store's layout (nothing
//! here is sized by the host), take interior nodes deepest first. A worker
//! reads its node, copies the node's leaves itself (one directory barrier per
//! shard they touch) and queues the node's interior children; the node is
//! published once they finish. A node is open from the moment a worker takes
//! it until it finishes. Nodes open while fewer are open than there are
//! workers, or when deeper than every open node, so the open set stays near
//! (workers + graph depth) whatever the graph's width; when nothing runs and
//! nothing is admissible (a wait on a child shared with a shallower open
//! node), the deepest queued node opens anyway, so the copy always
//! progresses. Memory is the open nodes' payloads (each within the block
//! format's bound) and their children's references. A worker that finds a
//! block another worker renamed but has not yet made durable syncs that
//! shard itself before relying on it. A VFS without concurrent file
//! publication runs the same schedule on the calling thread, so fault traces
//! stay deterministic.
//!
//! An online epoch copies beside a foreground that shares the mount: its
//! presence probes, reads and writes then pass a [`ForegroundShare`], which
//! admits as many concurrent operations as the foreground's authority
//! admission tolerates. Offline copies (restore, operator backup) run free.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap, HashMap, HashSet};
use std::io;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::Duration;

use crate::block::{BlockId, BlockStore};

/// One directory per leading byte of a block ID (`BlockStore` layout), and
/// one worker per directory.
const SHARDS: usize = 256;

/// How often the calling thread re-checks the stop flag while it waits.
const STOP_POLL: Duration = Duration::from_millis(250);

fn shard_of(id: BlockId) -> usize {
    usize::from(id.0[0])
}

/// One visit of one block: the block and the variant of its references.
type VisitKey = (BlockId, u8);

/// Concurrent copy I/O beside a foreground that shares the mount
/// (docs/STORAGE_OPERATIONS.md, "Native online backup"). Each sampled
/// foreground window (`fsync_stats::sample_admission_pressure`) in which
/// foreground requests queued for authority admission halves the admitted
/// operations, never below one, so the copy always progresses; each window
/// without that queuing admits one more, up to one per worker. A share starts
/// at one admitted operation.
pub(crate) struct ForegroundShare {
    state: Mutex<ShareState>,
    released: Condvar,
}

struct ShareState {
    admitted: usize,
    active: usize,
    sampler: crate::fsync_stats::ForegroundSampler,
    /// Specifications pin admission; every production share samples.
    sampling: bool,
}

impl ShareState {
    fn sampled(&mut self, pressure: bool) {
        self.admitted = if pressure {
            (self.admitted / 2).max(1)
        } else {
            (self.admitted + 1).min(SHARDS)
        };
    }
}

impl ForegroundShare {
    pub(crate) fn new() -> Self {
        Self::starting_at(1, true)
    }

    fn starting_at(admitted: usize, sampling: bool) -> Self {
        Self {
            state: Mutex::new(ShareState {
                admitted,
                active: 0,
                sampler: crate::fsync_stats::foreground_hotness_sampler(),
                sampling,
            }),
            released: Condvar::new(),
        }
    }

    fn lock(&self) -> MutexGuard<'_, ShareState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Waits for an admitted operation; `stop` ends the wait with `stopping`.
    fn enter(&self, stop: &AtomicBool, stopping: &'static str) -> io::Result<ShareSlot<'_>> {
        let mut state = self.lock();
        loop {
            if state.sampling {
                if let Some(pressure) =
                    crate::fsync_stats::sample_admission_pressure(&mut state.sampler)
                {
                    state.sampled(pressure);
                }
            }
            if state.active < state.admitted {
                state.active += 1;
                return Ok(ShareSlot(self));
            }
            if stop.load(Ordering::Acquire) {
                return Err(io::Error::new(io::ErrorKind::WouldBlock, stopping));
            }
            // Wake for a released operation, or to sample the next window.
            state = self
                .released
                .wait_timeout(state, crate::fsync_stats::FOREGROUND_SAMPLE_WINDOW)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
    }

    /// Operations the share currently admits at once.
    #[cfg(test)]
    pub(crate) fn admitted(&self) -> usize {
        self.lock().admitted
    }
}

struct ShareSlot<'a>(&'a ForegroundShare);

impl Drop for ShareSlot<'_> {
    fn drop(&mut self) {
        self.0.lock().active -= 1;
        self.0.released.notify_one();
    }
}

fn enter_share<'a>(
    share: Option<&'a ForegroundShare>,
    stop: &AtomicBool,
    stopping: &'static str,
) -> io::Result<Option<ShareSlot<'a>>> {
    share.map(|share| share.enter(stop, stopping)).transpose()
}

/// The reference semantics of the graph being copied.
pub(crate) trait CopyGraph: Sync {
    type Node: Clone + Send + Sync;

    fn block_id(&self, node: &Self::Node) -> BlockId;

    /// Separates visits of one block whose references differ (a discovery
    /// walk of a pin catalog page also references the pinned roots); each
    /// variant is a visit of its own.
    fn variant(&self, _node: &Self::Node) -> u8 {
        0
    }

    /// Whether a present copy proves the subgraph this visit needs. A visit
    /// whose references depend on how the node was reached proves nothing by
    /// presence: the copy that published the block may have walked another
    /// variant.
    fn presence_proves_subgraph(&self, _node: &Self::Node) -> bool {
        true
    }

    /// A node whose payload names no other block: a published copy of it is
    /// complete in any store.
    fn is_leaf(&self, node: &Self::Node) -> bool;

    /// A leaf reached through a reference that does not name the block's
    /// kind (history transaction references). It may hold references the
    /// destination lacks, so a closed store records it before publishing it.
    fn is_untyped(&self, node: &Self::Node) -> bool;

    /// The node's verified source bytes.
    fn read(&self, node: &Self::Node) -> io::Result<Vec<u8>>;

    /// The nodes `payload` (the node's verified bytes) references.
    fn references(&self, node: &Self::Node, payload: &[u8]) -> io::Result<Vec<Self::Node>>;
}

/// What a present destination block proves.
pub(crate) enum PresentBlocks<'a> {
    /// The destination is closed, except for the listed untyped copies: a
    /// present block's whole subgraph is present.
    Closed { untyped: &'a HashSet<BlockId> },
    /// Nothing beyond the block itself: present interior blocks are read and
    /// their references copied again (stores another writer filled).
    Unverified,
}

/// Live counters a status reader can sample while a copy runs.
#[derive(Debug, Default)]
pub(crate) struct GraphCopyProgress {
    pub visited_blocks: AtomicU64,
    pub present_subgraphs: AtomicU64,
    pub source_reads: AtomicU64,
    pub copied_blocks: AtomicU64,
    pub copied_bytes: AtomicU64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
pub(crate) struct GraphCopyMetrics {
    /// Nodes whose presence the copy decided.
    pub visited_blocks: u64,
    /// Present nodes whose whole subgraph the copy did not walk.
    pub present_subgraphs: u64,
    pub source_reads: u64,
    pub copied_blocks: u64,
    /// Payload bytes of the blocks this copy published.
    pub copied_bytes: u64,
    pub directory_barriers: u64,
    /// The most interior nodes open at once (taken and not yet finished).
    pub peak_open_nodes: u64,
}

impl GraphCopyMetrics {
    pub(crate) fn add(&mut self, other: Self) {
        self.visited_blocks += other.visited_blocks;
        self.present_subgraphs += other.present_subgraphs;
        self.source_reads += other.source_reads;
        self.copied_blocks += other.copied_blocks;
        self.copied_bytes += other.copied_bytes;
        self.directory_barriers += other.directory_barriers;
        self.peak_open_nodes = self.peak_open_nodes.max(other.peak_open_nodes);
    }
}

pub(crate) struct GraphCopyOutcome {
    pub metrics: GraphCopyMetrics,
    /// Untyped copies this copy walked as typed interior nodes and found
    /// complete: they no longer need their untyped record.
    pub closed_untyped: Vec<BlockId>,
}

/// Records untyped copies durably before they are published (closed stores).
pub(crate) type UntypedRecorder<'a> = &'a (dyn Fn(&[BlockId]) -> io::Result<()> + Sync);

pub(crate) struct GraphCopy<'a, G: CopyGraph> {
    pub graph: &'a G,
    pub destination: &'a BlockStore,
    pub present: PresentBlocks<'a>,
    pub record_untyped: Option<UntypedRecorder<'a>>,
    pub stop: &'a AtomicBool,
    pub progress: Option<&'a GraphCopyProgress>,
    /// Present for a copy beside a foreground on the same mount (online epochs).
    pub share: Option<&'a ForegroundShare>,
}

#[derive(Clone, Copy)]
enum Parent {
    Root,
    Block(VisitKey),
}

enum State {
    /// Discovered; waits in the heap for a worker and a slot.
    Queued,
    /// A worker holds it: reading it and copying its leaves.
    Expanding,
    /// Waits for its interior children; `payload` is the copy to publish
    /// then (`None`: the destination already holds the block).
    Waiting {
        payload: Option<Vec<u8>>,
        waiting: usize,
    },
    /// Its children finished; a worker publishes it.
    Publishing,
}

struct Active<N> {
    node: N,
    depth: u32,
    parents: Vec<Parent>,
    state: State,
}

struct Schedule<N> {
    /// Queued visits, deepest first, then oldest first. An entry whose node
    /// has since grown deeper or left `Queued` is stale and skipped.
    heap: BinaryHeap<(u32, Reverse<u64>, VisitKey)>,
    sequence: u64,
    active: HashMap<VisitKey, Active<N>>,
    /// Interior visits this copy finished whose presence alone does not
    /// prove their subgraph (unverified stores, untyped copies, variants).
    walked: HashSet<VisitKey>,
    publish: Vec<VisitKey>,
    /// Open nodes (taken, not finished) by depth.
    open: BTreeMap<u32, usize>,
    open_count: usize,
    busy: usize,
    roots_waiting: usize,
    failure: Option<io::Error>,
    metrics: GraphCopyMetrics,
    closed_untyped: Vec<BlockId>,
}

impl<N> Schedule<N> {
    fn finished(&self) -> bool {
        self.failure.is_some() || self.roots_waiting == 0
    }

    fn push(&mut self, depth: u32, key: VisitKey) {
        self.sequence += 1;
        self.heap.push((depth, Reverse(self.sequence), key));
    }

    fn release(&mut self, depth: u32) {
        self.open_count -= 1;
        if let Some(count) = self.open.get_mut(&depth) {
            *count -= 1;
            if *count == 0 {
                self.open.remove(&depth);
            }
        }
    }
}

enum Task<N> {
    Expand(VisitKey, N),
    Publish(VisitKey, Option<Vec<u8>>),
}

enum Expanded<N> {
    /// A trusted present copy: the whole subgraph is there.
    Present,
    Opened {
        payload: Option<Vec<u8>>,
        interiors: Vec<N>,
    },
}

/// Per-shard group commit of directory barriers. A request covers every
/// rename its requester finished before requesting; one sync of the shard
/// directory serves every request registered before it began. With
/// concurrent publication each shard has a syncing thread, so a worker's
/// requests to many shards complete in about one sync; otherwise the
/// waiter syncs inline, in request order.
struct ShardBarriers {
    shards: Vec<(Mutex<BarrierState>, Condvar)>,
    threaded: bool,
    syncs: AtomicU64,
}

#[derive(Default)]
struct BarrierState {
    requested: u64,
    durable: u64,
    failure: Option<(io::ErrorKind, String)>,
    closed: bool,
}

impl ShardBarriers {
    fn new(threaded: bool) -> Self {
        Self {
            shards: (0..SHARDS)
                .map(|_| (Mutex::new(BarrierState::default()), Condvar::new()))
                .collect(),
            threaded,
            syncs: AtomicU64::new(0),
        }
    }

    fn state(&self, shard: usize) -> MutexGuard<'_, BarrierState> {
        self.shards[shard]
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn request(&self, shard: usize) -> u64 {
        let ticket = {
            let mut state = self.state(shard);
            state.requested += 1;
            state.requested
        };
        self.shards[shard].1.notify_all();
        ticket
    }

    fn sync(&self, destination: &BlockStore, shard: usize) -> io::Result<()> {
        self.syncs.fetch_add(1, Ordering::Relaxed);
        destination.sync_copy_shard(BlockId([shard as u8; 32]))
    }

    fn wait(&self, destination: &BlockStore, shard: usize, ticket: u64) -> io::Result<()> {
        let mut state = self.state(shard);
        loop {
            if let Some((kind, message)) = &state.failure {
                return Err(io::Error::new(*kind, message.clone()));
            }
            if state.durable >= ticket {
                return Ok(());
            }
            if !self.threaded {
                let target = state.requested;
                drop(state);
                let result = self.sync(destination, shard);
                state = self.state(shard);
                match result {
                    Ok(()) => state.durable = state.durable.max(target),
                    Err(error) => state.failure = Some((error.kind(), error.to_string())),
                }
                continue;
            }
            state = self.shards[shard]
                .1
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
    }

    /// A shard's syncing thread: one sync per round, covering every request
    /// registered before the round began.
    fn serve(&self, destination: &BlockStore, shard: usize) {
        let (lock, signal) = &self.shards[shard];
        let mut state = lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        loop {
            if state.closed || state.failure.is_some() {
                return;
            }
            if state.requested == state.durable {
                state = signal
                    .wait(state)
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                continue;
            }
            let target = state.requested;
            drop(state);
            let result = self.sync(destination, shard);
            state = lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            match result {
                Ok(()) => state.durable = target,
                Err(error) => state.failure = Some((error.kind(), error.to_string())),
            }
            signal.notify_all();
        }
    }

    fn close(&self) {
        for (lock, signal) in &self.shards {
            lock.lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .closed = true;
            signal.notify_all();
        }
    }
}

struct Shared<'a, 'c, G: CopyGraph> {
    copy: &'c GraphCopy<'a, G>,
    schedule: Mutex<Schedule<G::Node>>,
    wake: Condvar,
    /// Blocks renamed into a shard whose barrier has not returned yet. A
    /// worker that finds one present makes its shard durable before anything
    /// above it can be published.
    unbarriered: Vec<Mutex<HashSet<BlockId>>>,
    barriers: ShardBarriers,
    /// This copy's own counters; the caller's `progress` gets the same.
    counters: GraphCopyProgress,
}

impl<'a, 'c, G: CopyGraph> Shared<'a, 'c, G> {
    fn lock(&self) -> MutexGuard<'_, Schedule<G::Node>> {
        self.schedule
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn key(&self, node: &G::Node) -> VisitKey {
        (
            self.copy.graph.block_id(node),
            self.copy.graph.variant(node),
        )
    }

    fn trusted(&self, node: &G::Node) -> bool {
        self.copy.graph.presence_proves_subgraph(node)
            && match self.copy.present {
                PresentBlocks::Closed { untyped } => {
                    !untyped.contains(&self.copy.graph.block_id(node))
                }
                PresentBlocks::Unverified => false,
            }
    }

    fn stopping(&self) -> io::Result<()> {
        if self.copy.stop.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "backup copy stopping",
            ));
        }
        Ok(())
    }

    /// One admitted I/O operation when the copy shares its mount. Holders
    /// never wait for a barrier or another worker, so admission cannot
    /// deadlock the schedule.
    fn admit(&self) -> io::Result<Option<ShareSlot<'a>>> {
        enter_share(self.copy.share, self.copy.stop, "backup copy stopping")
    }

    fn note(&self, counter: impl Fn(&GraphCopyProgress) -> &AtomicU64, amount: u64) {
        counter(&self.counters).fetch_add(amount, Ordering::Relaxed);
        if let Some(progress) = self.copy.progress {
            counter(progress).fetch_add(amount, Ordering::Relaxed);
        }
    }

    fn metrics(&self, peak_open_nodes: u64) -> GraphCopyMetrics {
        let counters = &self.counters;
        GraphCopyMetrics {
            visited_blocks: counters.visited_blocks.load(Ordering::Relaxed),
            present_subgraphs: counters.present_subgraphs.load(Ordering::Relaxed),
            source_reads: counters.source_reads.load(Ordering::Relaxed),
            copied_blocks: counters.copied_blocks.load(Ordering::Relaxed),
            copied_bytes: counters.copied_bytes.load(Ordering::Relaxed),
            directory_barriers: self.barriers.syncs.load(Ordering::Relaxed),
            peak_open_nodes,
        }
    }

    /// Whether the destination holds `id`, and whether its name may still be
    /// waiting for another worker's barrier (the caller then makes that
    /// shard durable before relying on it).
    fn present(&self, id: BlockId) -> io::Result<(bool, bool)> {
        if !self.copy.destination.retain_held_block(id)? {
            return Ok((false, false));
        }
        let in_flight = self.unbarriered[shard_of(id)]
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .contains(&id);
        Ok((true, in_flight))
    }

    /// Makes every rename finished so far in `shards` durable: requests all
    /// first, so threaded shards sync in parallel, then waits for each.
    fn make_durable(&self, shards: &BTreeMap<usize, Vec<BlockId>>) -> io::Result<()> {
        let tickets = shards
            .keys()
            .map(|shard| (*shard, self.barriers.request(*shard)))
            .collect::<Vec<_>>();
        for (shard, ticket) in tickets {
            self.barriers.wait(self.copy.destination, shard, ticket)?;
        }
        for (shard, ids) in shards {
            let mut in_flight = self.unbarriered[*shard]
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            for id in ids {
                in_flight.remove(id);
            }
        }
        Ok(())
    }

    /// Renames `id` into its shard; the caller's barrier makes it durable
    /// (also when another copy of the same block won the rename).
    fn publish_unbarriered(&self, id: BlockId, payload: &[u8]) -> io::Result<()> {
        self.unbarriered[shard_of(id)]
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(id);
        if self
            .copy
            .destination
            .publish_copied_block_unbarriered(id, payload)?
        {
            self.note(|progress| &progress.copied_blocks, 1);
            self.note(|progress| &progress.copied_bytes, payload.len() as u64);
        }
        Ok(())
    }

    /// Copies leaves in three passes: presence probes; one record of the
    /// absent untyped leaves (closed stores) before any of them appears;
    /// then each absent leaf read, written and synced before its rename, one
    /// payload at a time. Every shard touched (by a rename, or by a present
    /// name another worker has not made durable) is made durable before this
    /// returns, so nothing above these leaves can be published first.
    fn copy_leaves(&self, leaves: Vec<G::Node>) -> io::Result<()> {
        let copy = self.copy;
        let mut touched: BTreeMap<usize, Vec<BlockId>> = BTreeMap::new();
        let mut absent = Vec::new();
        for leaf in leaves {
            self.stopping()?;
            let id = copy.graph.block_id(&leaf);
            self.note(|progress| &progress.visited_blocks, 1);
            let held = {
                let _slot = self.admit()?;
                self.present(id)?
            };
            match held {
                (true, in_flight) => {
                    self.note(|progress| &progress.present_subgraphs, 1);
                    if in_flight {
                        touched.entry(shard_of(id)).or_default();
                    }
                }
                (false, _) => absent.push(leaf),
            }
        }
        if let Some(record) = copy.record_untyped {
            let untyped = absent
                .iter()
                .filter(|leaf| copy.graph.is_untyped(leaf))
                .map(|leaf| copy.graph.block_id(leaf))
                .collect::<Vec<_>>();
            if !untyped.is_empty() {
                record(&untyped)?;
            }
        }
        for leaf in absent {
            self.stopping()?;
            let id = copy.graph.block_id(&leaf);
            let _slot = self.admit()?;
            let payload = copy.graph.read(&leaf)?;
            self.note(|progress| &progress.source_reads, 1);
            let renamed = touched.entry(shard_of(id)).or_default();
            if renamed.is_empty() {
                copy.destination.ensure_copy_shard(id)?;
            }
            self.publish_unbarriered(id, &payload)?;
            renamed.push(id);
        }
        self.make_durable(&touched)
    }

    fn expand(&self, key: VisitKey, node: &G::Node) -> io::Result<Expanded<G::Node>> {
        self.stopping()?;
        self.note(|progress| &progress.visited_blocks, 1);
        let (present, in_flight) = {
            let _slot = self.admit()?;
            self.present(key.0)?
        };
        if in_flight {
            self.make_durable(&BTreeMap::from([(shard_of(key.0), Vec::new())]))?;
        }
        if present && self.trusted(node) {
            self.note(|progress| &progress.present_subgraphs, 1);
            return Ok(Expanded::Present);
        }
        let payload = {
            let _slot = self.admit()?;
            self.copy.graph.read(node)?
        };
        self.note(|progress| &progress.source_reads, 1);
        let (leaves, interiors) = self
            .copy
            .graph
            .references(node, &payload)?
            .into_iter()
            .partition::<Vec<_>, _>(|reference| self.copy.graph.is_leaf(reference));
        self.copy_leaves(leaves)?;
        Ok(Expanded::Opened {
            payload: (!present).then_some(payload),
            interiors,
        })
    }

    fn publish(&self, key: VisitKey, payload: Option<Vec<u8>>) -> io::Result<()> {
        let Some(payload) = payload else {
            return Ok(());
        };
        self.stopping()?;
        {
            let _slot = self.admit()?;
            self.copy.destination.ensure_copy_shard(key.0)?;
            self.publish_unbarriered(key.0, &payload)?;
        }
        self.make_durable(&BTreeMap::from([(shard_of(key.0), vec![key.0])]))
    }

    /// Admits `node` below `parent`; whether the parent must wait for it.
    fn discover(
        &self,
        schedule: &mut Schedule<G::Node>,
        node: G::Node,
        parent: Parent,
        depth: u32,
    ) -> bool {
        let key = self.key(&node);
        if schedule.walked.contains(&key) {
            return false;
        }
        if let Some(active) = schedule.active.get_mut(&key) {
            active.parents.push(parent);
            // Reached deeper: queue it at its new depth (the old entry goes
            // stale), so the deepest open node's children stay admissible.
            let deeper = matches!(active.state, State::Queued) && depth > active.depth;
            if deeper {
                active.depth = depth;
                schedule.push(depth, key);
            }
            return true;
        }
        schedule.active.insert(
            key,
            Active {
                node,
                depth,
                parents: vec![parent],
                state: State::Queued,
            },
        );
        schedule.push(depth, key);
        true
    }

    /// The deepest queued visit, when a slot admits it: fewer open nodes
    /// than workers, or deeper than every open node. With `stuck` (nothing
    /// runs and nothing is admissible: every slot waits on a child that is
    /// shared with a shallower open node) the deepest visit opens anyway.
    fn take_visit(
        &self,
        schedule: &mut Schedule<G::Node>,
        stuck: bool,
    ) -> Option<(VisitKey, G::Node)> {
        while let Some(&(depth, _, key)) = schedule.heap.peek() {
            let current = schedule.active.get(&key).is_some_and(|active| {
                matches!(active.state, State::Queued) && active.depth == depth
            });
            if !current {
                schedule.heap.pop();
                continue;
            }
            let deepest_open = schedule.open.keys().next_back().copied();
            if !stuck
                && schedule.open_count >= SHARDS
                && deepest_open.is_some_and(|open| depth <= open)
            {
                return None;
            }
            schedule.heap.pop();
            schedule.open_count += 1;
            *schedule.open.entry(depth).or_default() += 1;
            schedule.metrics.peak_open_nodes = schedule
                .metrics
                .peak_open_nodes
                .max(schedule.open_count as u64);
            let active = schedule
                .active
                .get_mut(&key)
                .expect("a current entry is active");
            active.state = State::Expanding;
            return Some((key, active.node.clone()));
        }
        None
    }

    /// Finishes `key` and every parent it was the last child of.
    fn finish(&self, schedule: &mut Schedule<G::Node>, key: VisitKey) -> usize {
        let mut woken = 0;
        let mut finishing = vec![key];
        while let Some(key) = finishing.pop() {
            let Some(active) = schedule.active.remove(&key) else {
                continue;
            };
            schedule.release(active.depth);
            woken += 1;
            let untrusted = match self.copy.present {
                PresentBlocks::Closed { untyped } => {
                    let recorded = untyped.contains(&key.0);
                    if recorded {
                        // Walked as a typed node: its whole subgraph is now here.
                        schedule.closed_untyped.push(key.0);
                    }
                    recorded || !self.copy.graph.presence_proves_subgraph(&active.node)
                }
                PresentBlocks::Unverified => true,
            };
            if untrusted {
                schedule.walked.insert(key);
            }
            for parent in active.parents {
                let Parent::Block(parent) = parent else {
                    schedule.roots_waiting -= 1;
                    continue;
                };
                let Some(active) = schedule.active.get_mut(&parent) else {
                    continue;
                };
                let State::Waiting { payload, waiting } = &mut active.state else {
                    continue;
                };
                *waiting -= 1;
                if *waiting == 0 {
                    if payload.is_some() {
                        schedule.publish.push(parent);
                        woken += 1;
                    } else {
                        finishing.push(parent);
                    }
                }
            }
        }
        woken
    }

    fn fail(&self, schedule: &mut Schedule<G::Node>, error: io::Error) {
        if schedule.failure.is_none() {
            schedule.failure = Some(error);
        }
    }

    fn apply(&self, task_key: VisitKey, result: io::Result<Option<Expanded<G::Node>>>) {
        let mut schedule = self.lock();
        schedule.busy -= 1;
        let mut woken = 0;
        match result {
            Err(error) => self.fail(&mut schedule, error),
            // A published node.
            Ok(None) => woken += self.finish(&mut schedule, task_key),
            Ok(Some(Expanded::Present)) => woken += self.finish(&mut schedule, task_key),
            Ok(Some(Expanded::Opened { payload, interiors })) => {
                let depth = schedule
                    .active
                    .get(&task_key)
                    .map_or(0, |active| active.depth);
                let mut waiting = 0;
                for child in interiors {
                    if self.discover(&mut schedule, child, Parent::Block(task_key), depth + 1) {
                        waiting += 1;
                        woken += 1;
                    }
                }
                let publish = payload.is_some();
                if let Some(active) = schedule.active.get_mut(&task_key) {
                    active.state = State::Waiting { payload, waiting };
                }
                if waiting == 0 {
                    if publish {
                        schedule.publish.push(task_key);
                        woken += 1;
                    } else {
                        woken += self.finish(&mut schedule, task_key);
                    }
                }
            }
        }
        let finished = schedule.finished();
        drop(schedule);
        if finished {
            self.wake.notify_all();
        } else {
            for _ in 0..woken.min(SHARDS) {
                self.wake.notify_one();
            }
        }
    }

    /// A worker: publishes ready nodes first, then expands the deepest
    /// admissible visit, until the copy finishes. The calling thread also
    /// watches the stop flag while it waits.
    fn work(&self, watches_stop: bool) {
        loop {
            let task =
                {
                    let mut schedule = self.lock();
                    loop {
                        if schedule.finished() {
                            break None;
                        }
                        if let Some(key) = schedule.publish.pop() {
                            let payload = schedule.active.get_mut(&key).and_then(|active| {
                                match std::mem::replace(&mut active.state, State::Publishing) {
                                    State::Waiting { payload, .. } => payload,
                                    _ => None,
                                }
                            });
                            schedule.busy += 1;
                            break Some(Task::Publish(key, payload));
                        }
                        if let Some((key, node)) = self.take_visit(&mut schedule, false) {
                            schedule.busy += 1;
                            break Some(Task::Expand(key, node));
                        }
                        if schedule.busy == 0 {
                            if let Some((key, node)) = self.take_visit(&mut schedule, true) {
                                schedule.busy += 1;
                                break Some(Task::Expand(key, node));
                            }
                            let error = io::Error::other(
                                "backup copy schedule stalled before its roots finished",
                            );
                            self.fail(&mut schedule, error);
                            break None;
                        }
                        if watches_stop {
                            if self.copy.stop.load(Ordering::Acquire) {
                                let error = io::Error::new(
                                    io::ErrorKind::WouldBlock,
                                    "backup copy stopping",
                                );
                                self.fail(&mut schedule, error);
                                break None;
                            }
                            schedule = self
                                .wake
                                .wait_timeout(schedule, STOP_POLL)
                                .unwrap_or_else(|poisoned| poisoned.into_inner())
                                .0;
                        } else {
                            schedule = self
                                .wake
                                .wait(schedule)
                                .unwrap_or_else(|poisoned| poisoned.into_inner());
                        }
                    }
                };
            match task {
                None => {
                    self.wake.notify_all();
                    return;
                }
                Some(Task::Publish(key, payload)) => {
                    let result = self.publish(key, payload).map(|()| None);
                    self.apply(key, result);
                }
                Some(Task::Expand(key, node)) => {
                    let result = self.expand(key, &node).map(Some);
                    self.apply(key, result);
                }
            }
        }
    }
}

/// Copies every block reachable from `roots` that the destination lacks.
pub(crate) fn copy_graph<G: CopyGraph>(
    copy: &GraphCopy<'_, G>,
    roots: Vec<G::Node>,
) -> io::Result<GraphCopyOutcome> {
    let parallel = copy.destination.supports_concurrent_file_publication();
    let shared = Shared {
        copy,
        schedule: Mutex::new(Schedule {
            heap: BinaryHeap::new(),
            sequence: 0,
            active: HashMap::new(),
            walked: HashSet::new(),
            publish: Vec::new(),
            open: BTreeMap::new(),
            open_count: 0,
            busy: 0,
            roots_waiting: 0,
            failure: None,
            metrics: GraphCopyMetrics::default(),
            closed_untyped: Vec::new(),
        }),
        wake: Condvar::new(),
        unbarriered: (0..SHARDS).map(|_| Mutex::new(HashSet::new())).collect(),
        barriers: ShardBarriers::new(parallel),
        counters: GraphCopyProgress::default(),
    };
    let (leaf_roots, interior_roots): (Vec<_>, Vec<_>) =
        roots.into_iter().partition(|root| copy.graph.is_leaf(root));
    // Leaf roots copy first; interior roots queue before any worker starts,
    // so no worker can see an empty, finished schedule.
    let prepare = || -> io::Result<bool> {
        shared.copy_leaves(leaf_roots)?;
        let mut schedule = shared.lock();
        for root in interior_roots {
            if shared.discover(&mut schedule, root, Parent::Root, 0) {
                schedule.roots_waiting += 1;
            }
        }
        Ok(schedule.roots_waiting > 0)
    };
    let result = if parallel {
        let component = crate::fsync_stats::current_component();
        std::thread::scope(|scope| {
            let shared = &shared;
            for shard in 0..SHARDS {
                // An unserved shard would never answer its waiters, so a
                // refused syncing thread refuses the copy.
                let spawned = std::thread::Builder::new()
                    .name("tofu-db-backup-sync".to_owned())
                    .spawn_scoped(scope, move || {
                        shared.barriers.serve(shared.copy.destination, shard)
                    });
                if let Err(error) = spawned {
                    shared.barriers.close();
                    return Err(error);
                }
            }
            let result = prepare().map(|has_work| {
                if !has_work {
                    return;
                }
                for _ in 1..SHARDS {
                    // A refused worker spawn only leaves fewer workers.
                    let _ = std::thread::Builder::new()
                        .name("tofu-db-backup-copy".to_owned())
                        .spawn_scoped(scope, move || {
                            crate::fsync_stats::set_current_component(component);
                            shared.work(false);
                        });
                }
                shared.work(true);
                // A worker may still be finishing a task (and waiting on a
                // barrier) after the schedule finished: the syncing threads
                // stop only once none runs.
                let mut schedule = shared.lock();
                while schedule.busy > 0 {
                    schedule = shared
                        .wake
                        .wait(schedule)
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                }
            });
            shared.barriers.close();
            result
        })
    } else {
        prepare().map(|has_work| {
            if has_work {
                shared.work(true);
            }
        })
    };
    result?;
    let peak_open_nodes = shared.lock().metrics.peak_open_nodes;
    let metrics = shared.metrics(peak_open_nodes);
    let schedule = shared
        .schedule
        .into_inner()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(error) = schedule.failure {
        return Err(error);
    }
    Ok(GraphCopyOutcome {
        metrics,
        closed_untyped: schedule.closed_untyped,
    })
}

struct VisitSchedule<N> {
    /// Interior nodes to read, deepest first, then oldest first.
    heap: BinaryHeap<(u32, Reverse<u64>, usize)>,
    nodes: HashMap<usize, N>,
    sequence: u64,
    /// Interior visits already admitted. Leaves are marked on every visit
    /// instead (marks are idempotent), so this holds interiors only.
    seen: HashSet<VisitKey>,
    busy: usize,
    failure: Option<io::Error>,
    visited: u64,
}

/// Visits every node reachable from `roots`, reading each interior visit
/// once and handing every block to `mark` (prune marks with it exactly the
/// blocks a copy of the retained generations reads). Workers take interior
/// nodes deepest first and mark a node's leaves themselves, so only interior
/// nodes wait in memory. One worker per hash shard when `parallel`; an online
/// epoch's `share` admits their reads beside the foreground.
pub(crate) fn visit_graph<G: CopyGraph>(
    graph: &G,
    roots: Vec<G::Node>,
    parallel: bool,
    stop: &AtomicBool,
    share: Option<&ForegroundShare>,
    mark: &(dyn Fn(&[BlockId]) -> io::Result<()> + Sync),
) -> io::Result<u64> {
    let schedule = Mutex::new(VisitSchedule {
        heap: BinaryHeap::new(),
        nodes: HashMap::new(),
        sequence: 0,
        seen: HashSet::new(),
        busy: 0,
        failure: None,
        visited: 0,
    });
    let wake = Condvar::new();
    let lock = || {
        schedule
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    };
    let admit = |state: &mut VisitSchedule<G::Node>, node: G::Node, depth: u32| {
        if state
            .seen
            .insert((graph.block_id(&node), graph.variant(&node)))
        {
            state.sequence += 1;
            let slot = state.sequence as usize;
            state.nodes.insert(slot, node);
            state.heap.push((depth, Reverse(state.sequence), slot));
            true
        } else {
            false
        }
    };
    let (leaf_roots, interior_roots): (Vec<_>, Vec<_>) =
        roots.into_iter().partition(|root| graph.is_leaf(root));
    mark(
        &leaf_roots
            .iter()
            .map(|root| graph.block_id(root))
            .collect::<Vec<_>>(),
    )?;
    {
        let mut state = lock();
        state.visited += leaf_roots.len() as u64;
        for root in interior_roots {
            admit(&mut state, root, 0);
        }
    }
    let process = |node: G::Node| -> io::Result<(Vec<G::Node>, u64)> {
        if stop.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "backup mark stopping",
            ));
        }
        let payload = {
            let _slot = enter_share(share, stop, "backup mark stopping")?;
            graph.read(&node)?
        };
        let (leaves, interiors): (Vec<_>, Vec<_>) = graph
            .references(&node, &payload)?
            .into_iter()
            .partition(|reference| graph.is_leaf(reference));
        let mut ids = Vec::with_capacity(leaves.len() + 1);
        ids.push(graph.block_id(&node));
        ids.extend(leaves.iter().map(|leaf| graph.block_id(leaf)));
        mark(&ids)?;
        Ok((interiors, ids.len() as u64))
    };
    let work = || loop {
        let next = {
            let mut state = lock();
            loop {
                if state.failure.is_some() {
                    break None;
                }
                if let Some((depth, _, slot)) = state.heap.pop() {
                    let node = state.nodes.remove(&slot).expect("a queued node is stored");
                    state.busy += 1;
                    break Some((depth, node));
                }
                if state.busy == 0 {
                    break None;
                }
                state = wake
                    .wait(state)
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
            }
        };
        let Some((depth, node)) = next else {
            wake.notify_all();
            return;
        };
        let result = process(node);
        let mut state = lock();
        state.busy -= 1;
        let mut admitted = 0;
        match result {
            Ok((interiors, visited)) => {
                state.visited += visited;
                for child in interiors {
                    if admit(&mut state, child, depth + 1) {
                        admitted += 1;
                    }
                }
            }
            Err(error) => {
                if state.failure.is_none() {
                    state.failure = Some(error);
                }
            }
        }
        let done = state.failure.is_some() || (state.busy == 0 && state.heap.is_empty());
        drop(state);
        if done {
            wake.notify_all();
        } else {
            for _ in 0..admitted.min(SHARDS) {
                wake.notify_one();
            }
        }
    };
    if parallel {
        let component = crate::fsync_stats::current_component();
        std::thread::scope(|scope| {
            for _ in 1..SHARDS {
                let work = &work;
                let _ = std::thread::Builder::new()
                    .name("tofu-db-backup-mark".to_owned())
                    .spawn_scoped(scope, move || {
                        crate::fsync_stats::set_current_component(component);
                        work();
                    });
            }
            work();
        });
    } else {
        work();
    }
    let state = schedule
        .into_inner()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    match state.failure {
        Some(error) => Err(error),
        None => Ok(state.visited),
    }
}

#[cfg(test)]
#[path = "backup_copy_tests.rs"]
mod tests;
