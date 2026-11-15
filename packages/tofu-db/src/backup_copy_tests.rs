//! Specifications of the post-order graph copier on a synthetic graph:
//! closure under every crash point, presence skipping, untyped copies,
//! stop handling and the shard-parallel schedule.

use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Vfs};
use std::path::Path;
use std::sync::Arc;

/// Interior payload: b'N' then the child IDs; leaf payload: b'L' then bytes.
struct TestGraph {
    blocks: HashMap<BlockId, Vec<u8>>,
    leaves: HashSet<BlockId>,
    untyped: HashSet<BlockId>,
    reads: AtomicU64,
    /// Every read, in order.
    read_log: Mutex<Vec<BlockId>>,
}

impl TestGraph {
    fn new() -> Self {
        Self {
            blocks: HashMap::new(),
            leaves: HashSet::new(),
            untyped: HashSet::new(),
            reads: AtomicU64::new(0),
            read_log: Mutex::new(Vec::new()),
        }
    }

    fn leaf(&mut self, content: &str) -> BlockId {
        let mut payload = b"L".to_vec();
        payload.extend_from_slice(content.as_bytes());
        let id = BlockId::for_payload(&payload);
        self.blocks.insert(id, payload);
        self.leaves.insert(id);
        id
    }

    fn node(&mut self, children: &[BlockId]) -> BlockId {
        let mut payload = b"N".to_vec();
        for child in children {
            payload.extend_from_slice(&child.0);
        }
        let id = BlockId::for_payload(&payload);
        self.blocks.insert(id, payload);
        id
    }

    fn children(&self, id: BlockId) -> Vec<BlockId> {
        let payload = &self.blocks[&id];
        if payload[0] == b'L' {
            return Vec::new();
        }
        payload[1..].chunks_exact(32).map(|raw| BlockId(raw.try_into().unwrap())).collect()
    }

    fn reachable(&self, root: BlockId) -> HashSet<BlockId> {
        let mut seen = HashSet::new();
        let mut pending = vec![root];
        while let Some(id) = pending.pop() {
            if seen.insert(id) {
                pending.extend(self.children(id));
            }
        }
        seen
    }

    /// A root over `width` subtrees of `fanout` interior nodes with
    /// `leaves` leaves each, one leaf shared by every subtree.
    fn tree(&mut self, label: &str, width: usize, fanout: usize, leaves: usize) -> BlockId {
        let shared = self.leaf(&format!("{label}-shared"));
        let mut subtrees = Vec::new();
        for subtree in 0..width {
            let mut interiors = Vec::new();
            for interior in 0..fanout {
                let mut children = vec![shared];
                for leaf in 0..leaves {
                    children.push(self.leaf(&format!("{label}-{subtree}-{interior}-{leaf}")));
                }
                interiors.push(self.node(&children));
            }
            subtrees.push(self.node(&interiors));
        }
        self.node(&subtrees)
    }
}

impl CopyGraph for TestGraph {
    type Node = BlockId;

    fn block_id(&self, node: &BlockId) -> BlockId {
        *node
    }

    fn is_leaf(&self, node: &BlockId) -> bool {
        self.leaves.contains(node) || self.untyped.contains(node)
    }

    fn is_untyped(&self, node: &BlockId) -> bool {
        self.untyped.contains(node)
    }

    fn read(&self, node: &BlockId) -> io::Result<Vec<u8>> {
        self.reads.fetch_add(1, Ordering::Relaxed);
        self.read_log.lock().unwrap().push(*node);
        self.blocks
            .get(node)
            .cloned()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "test block is missing"))
    }

    fn references(&self, node: &BlockId, _payload: &[u8]) -> io::Result<Vec<BlockId>> {
        Ok(self.children(*node))
    }
}

fn destination() -> (Arc<DeterministicVfs>, BlockStore) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/backup")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let store = BlockStore::initialize_with_vfs(Path::new("/backup"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, store)
}

fn copy(
    graph: &TestGraph,
    store: &BlockStore,
    present: PresentBlocks<'_>,
    root: BlockId,
) -> io::Result<GraphCopyOutcome> {
    let stop = AtomicBool::new(false);
    copy_graph(
        &GraphCopy { graph, destination: store, present, record_untyped: None, stop: &stop, progress: None, share: None },
        vec![root],
    )
}

fn assert_closed(graph: &TestGraph, store: &BlockStore) {
    for (&id, _) in &graph.blocks {
        if !store.retain_held_block(id).unwrap() {
            continue;
        }
        for child in graph.children(id) {
            assert!(
                store.retain_held_block(child).unwrap(),
                "block {} is present without its reference {}",
                id.to_hex(),
                child.to_hex()
            );
        }
    }
}

fn closed() -> HashSet<BlockId> {
    HashSet::new()
}

#[test]
fn every_crash_point_leaves_a_closed_store_and_a_rerun_completes_it() {
    let mut graph = TestGraph::new();
    let root = graph.tree("crash", 2, 2, 3);
    let (baseline_vfs, baseline) = destination();
    copy(&graph, &baseline, PresentBlocks::Unverified, root).unwrap();
    let operations = baseline_vfs.trace().unwrap().len() as u64;
    assert!(operations > 0);
    for operation_number in 1..=operations {
        let (vfs, store) = destination();
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        let untyped = closed();
        let _ = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, root);
        drop(store);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/backup"), vfs.clone()).unwrap();
        assert_closed(&graph, &reopened);
        copy(&graph, &reopened, PresentBlocks::Closed { untyped: &untyped }, root).unwrap();
        for id in graph.reachable(root) {
            assert!(reopened.retain_held_block(id).unwrap(), "operation {operation_number}");
        }
    }
}

#[test]
fn lost_syncs_never_leave_a_block_without_its_references() {
    let mut graph = TestGraph::new();
    let root = graph.tree("sync", 2, 2, 2);
    let (baseline_vfs, baseline) = destination();
    copy(&graph, &baseline, PresentBlocks::Unverified, root).unwrap();
    let trace = baseline_vfs.trace().unwrap();
    for (index, operation) in trace.iter().enumerate() {
        if !matches!(
            operation,
            crate::vfs::Operation::SyncData
                | crate::vfs::Operation::SyncAll
                | crate::vfs::Operation::SyncDirectory
        ) {
            continue;
        }
        let (vfs, store) = destination();
        vfs.arm_fault(Some(FaultRule {
            operation_number: index as u64 + 1,
            action: FaultAction::DropSync,
        }))
        .unwrap();
        let untyped = closed();
        copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, root).unwrap();
        drop(store);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = BlockStore::open_with_vfs(Path::new("/backup"), vfs.clone()).unwrap();
        assert_closed(&graph, &reopened);
    }
}

#[test]
fn a_closed_store_skips_present_subgraphs_so_work_follows_new_blocks() {
    let mut graph = TestGraph::new();
    let first = graph.tree("first", 4, 4, 8);
    let (_vfs, store) = destination();
    let untyped = closed();
    let full = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, first).unwrap();
    assert_eq!(full.metrics.copied_blocks as usize, graph.reachable(first).len());

    graph.reads.store(0, Ordering::Relaxed);
    let unchanged = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, first).unwrap();
    assert_eq!(graph.reads.load(Ordering::Relaxed), 0, "an unchanged root reads nothing");
    assert_eq!(unchanged.metrics.visited_blocks, 1);
    assert_eq!(unchanged.metrics.copied_blocks, 0);

    // One new leaf below one interior node: the new path and its siblings'
    // presence probes, never the rest of the tree.
    let old_subtrees = graph.children(first);
    let old_interiors = graph.children(old_subtrees[0]);
    let mut leaf_children = graph.children(old_interiors[0]);
    leaf_children.push(graph.leaf("added"));
    let interior = graph.node(&leaf_children);
    let mut interiors = old_interiors.clone();
    interiors[0] = interior;
    let subtree = graph.node(&interiors);
    let mut subtrees = old_subtrees.clone();
    subtrees[0] = subtree;
    let second = graph.node(&subtrees);
    graph.reads.store(0, Ordering::Relaxed);
    let incremental = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, second).unwrap();
    assert_eq!(incremental.metrics.copied_blocks, 4, "root, subtree, interior and the new leaf");
    assert_eq!(graph.reads.load(Ordering::Relaxed), 4);
    assert!(
        incremental.metrics.visited_blocks < graph.reachable(second).len() as u64 / 4,
        "visited {} of {} blocks",
        incremental.metrics.visited_blocks,
        graph.reachable(second).len()
    );
    assert_closed(&graph, &store);

    // The same change into a store whose presence proves nothing re-reads
    // every interior node.
    let (_vfs, unverified) = destination();
    copy(&graph, &unverified, PresentBlocks::Unverified, first).unwrap();
    graph.reads.store(0, Ordering::Relaxed);
    copy(&graph, &unverified, PresentBlocks::Unverified, second).unwrap();
    let interior_count = graph
        .reachable(second)
        .into_iter()
        .filter(|id| !graph.leaves.contains(id))
        .count() as u64;
    assert_eq!(graph.reads.load(Ordering::Relaxed), interior_count + 1);
}

#[test]
fn a_present_untyped_copy_is_walked_and_its_missing_references_copied() {
    let mut graph = TestGraph::new();
    let child = graph.leaf("only-below-the-untyped-copy");
    let page = graph.node(&[child]);
    let root = graph.node(&[page]);
    let (_vfs, store) = destination();
    // An earlier copy published `page` by identity alone (a history
    // transaction reference), without its reference.
    store.ensure_copy_shard(page).unwrap();
    store.publish_copied_block_unbarriered(page, &graph.blocks[&page]).unwrap();
    store.sync_copy_shard(page).unwrap();

    let trusted = closed();
    copy(&graph, &store, PresentBlocks::Closed { untyped: &trusted }, root).unwrap();
    assert!(!store.retain_held_block(child).unwrap(), "trusting the untyped copy skips its reference");

    let (_vfs, store) = destination();
    store.ensure_copy_shard(page).unwrap();
    store.publish_copied_block_unbarriered(page, &graph.blocks[&page]).unwrap();
    store.sync_copy_shard(page).unwrap();
    let recorded = HashSet::from([page]);
    let outcome = copy(&graph, &store, PresentBlocks::Closed { untyped: &recorded }, root).unwrap();
    assert!(store.retain_held_block(child).unwrap());
    assert_eq!(outcome.closed_untyped, vec![page]);
    assert_closed(&graph, &store);
}

#[test]
fn untyped_copies_are_recorded_before_they_are_published() {
    let mut graph = TestGraph::new();
    let reference = graph.leaf("transaction-reference");
    graph.leaves.remove(&reference);
    graph.untyped.insert(reference);
    let root = graph.node(&[reference]);
    let (_vfs, store) = destination();
    let recorded = Mutex::new(Vec::new());
    let record = |ids: &[BlockId]| -> io::Result<()> {
        for id in ids {
            assert!(!store.retain_held_block(*id).unwrap(), "recorded after publication");
        }
        recorded.lock().unwrap().extend_from_slice(ids);
        Ok(())
    };
    let untyped = closed();
    let stop = AtomicBool::new(false);
    copy_graph(
        &GraphCopy {
            graph: &graph,
            destination: &store,
            present: PresentBlocks::Closed { untyped: &untyped },
            record_untyped: Some(&record),
            stop: &stop,
            progress: None,
            share: None,
        },
        vec![root],
    )
    .unwrap();
    assert_eq!(*recorded.lock().unwrap(), vec![reference]);
    assert!(store.retain_held_block(reference).unwrap());
}

#[test]
fn a_stop_ends_the_copy_and_keeps_every_finished_subgraph_closed() {
    let mut graph = TestGraph::new();
    let root = graph.tree("stop", 3, 3, 4);
    let (_vfs, store) = destination();
    let stop = AtomicBool::new(false);
    let progress = GraphCopyProgress::default();
    let untyped = closed();
    struct StoppingGraph<'a> {
        inner: &'a TestGraph,
        stop: &'a AtomicBool,
        reads_before_stop: u64,
    }
    impl CopyGraph for StoppingGraph<'_> {
        type Node = BlockId;
        fn block_id(&self, node: &BlockId) -> BlockId { *node }
        fn is_leaf(&self, node: &BlockId) -> bool { self.inner.is_leaf(node) }
        fn is_untyped(&self, node: &BlockId) -> bool { self.inner.is_untyped(node) }
        fn read(&self, node: &BlockId) -> io::Result<Vec<u8>> {
            if self.inner.reads.load(Ordering::Relaxed) + 1 >= self.reads_before_stop {
                self.stop.store(true, Ordering::Release);
            }
            self.inner.read(node)
        }
        fn references(&self, node: &BlockId, payload: &[u8]) -> io::Result<Vec<BlockId>> {
            self.inner.references(node, payload)
        }
    }
    let stopping = StoppingGraph { inner: &graph, stop: &stop, reads_before_stop: 20 };
    let error = copy_graph(
        &GraphCopy {
            graph: &stopping,
            destination: &store,
            present: PresentBlocks::Closed { untyped: &untyped },
            record_untyped: None,
            stop: &stop,
            progress: Some(&progress),
            share: None,
        },
        vec![root],
    )
    .err()
    .expect("a stopped copy fails");
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    assert!(progress.copied_blocks.load(Ordering::Relaxed) > 0);
    assert!(!store.retain_held_block(root).unwrap());
    assert_closed(&graph, &store);
}

#[test]
fn the_shard_parallel_schedule_copies_a_shared_graph_completely() {
    let mut graph = TestGraph::new();
    let first = graph.tree("parallel", 8, 8, 16);
    let second = graph.tree("parallel-second", 4, 4, 4);
    let both = graph.node(&[first, second, first]);
    let (vfs, store) = destination();
    vfs.allow_concurrent_file_publication().unwrap();
    assert!(store.supports_concurrent_file_publication());
    let untyped = closed();
    let outcome = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, both).unwrap();
    let reachable = graph.reachable(both);
    assert_eq!(outcome.metrics.copied_blocks as usize, reachable.len());
    for id in reachable {
        assert!(store.retain_held_block(id).unwrap());
    }
    assert_closed(&graph, &store);
}

/// A failure in one shard's batch ends every worker and returns the error;
/// a finished schedule never strands a waiting worker (the copy used to
/// hang here when the coordinator took the failure out of the schedule).
#[test]
fn a_failed_read_in_the_parallel_schedule_ends_the_copy_with_its_error() {
    let mut graph = TestGraph::new();
    let root = graph.tree("failing", 8, 8, 8);
    let missing = graph.children(graph.children(graph.children(root)[3])[5])[2];
    graph.blocks.remove(&missing);
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let (vfs, store) = destination();
        vfs.allow_concurrent_file_publication().unwrap();
        let untyped = closed();
        let result = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, root)
            .map(|_| ());
        let _ = sender.send(result.map_err(|error| error.kind()));
    });
    let result = receiver
        .recv_timeout(std::time::Duration::from_secs(60))
        .expect("the failed parallel copy must end");
    assert_eq!(result, Err(io::ErrorKind::NotFound));
}

/// Nodes carry a variant; variant 1 of a block also references `extra`.
struct VariantGraph {
    inner: TestGraph,
    extra: HashMap<BlockId, BlockId>,
}

impl CopyGraph for VariantGraph {
    type Node = (BlockId, u8);
    fn block_id(&self, node: &(BlockId, u8)) -> BlockId { node.0 }
    fn variant(&self, node: &(BlockId, u8)) -> u8 { node.1 }
    fn presence_proves_subgraph(&self, node: &(BlockId, u8)) -> bool { node.1 == 0 }
    fn is_leaf(&self, node: &(BlockId, u8)) -> bool { self.inner.is_leaf(&node.0) }
    fn is_untyped(&self, _node: &(BlockId, u8)) -> bool { false }
    fn read(&self, node: &(BlockId, u8)) -> io::Result<Vec<u8>> { self.inner.read(&node.0) }
    fn references(&self, node: &(BlockId, u8), payload: &[u8]) -> io::Result<Vec<(BlockId, u8)>> {
        let mut references: Vec<_> =
            self.inner.references(&node.0, payload)?.into_iter().map(|id| (id, 0)).collect();
        if node.1 == 1 {
            references.extend(self.extra.get(&node.0).map(|id| (*id, 0)));
        }
        Ok(references)
    }
}

/// A discovery walk of a pin catalog page references pinned roots that
/// another walk of the same page does not: a copy that published the page
/// through the other walk proves nothing for it, in one copy or across copies.
#[test]
fn a_visit_whose_references_depend_on_its_variant_is_never_satisfied_by_another() {
    let mut inner = TestGraph::new();
    let pinned_leaf = inner.leaf("only-a-discovery-walk-reaches-this");
    let pinned = inner.node(&[pinned_leaf]);
    let catalog_leaf = inner.leaf("catalog-content");
    let catalog = inner.node(&[catalog_leaf]);
    let graph = VariantGraph { inner, extra: HashMap::from([(catalog, pinned)]) };
    let untyped = closed();
    let stop = AtomicBool::new(false);
    let run = |store: &BlockStore, roots: Vec<(BlockId, u8)>| {
        copy_graph(
            &GraphCopy {
                graph: &graph,
                destination: store,
                present: PresentBlocks::Closed { untyped: &untyped },
                record_untyped: None,
                stop: &stop,
                progress: None,
                share: None,
            },
            roots,
        )
        .unwrap()
    };
    // Across copies: the page was published by a walk without discovery.
    let (_vfs, store) = destination();
    run(&store, vec![(catalog, 0)]);
    assert!(store.retain_held_block(catalog).unwrap());
    assert!(!store.retain_held_block(pinned).unwrap());
    let outcome = run(&store, vec![(catalog, 1)]);
    assert!(store.retain_held_block(pinned).unwrap(), "the discovery visit trusted presence");
    assert!(store.retain_held_block(pinned_leaf).unwrap());
    assert_eq!(outcome.metrics.source_reads, 3, "the page, the pinned root and its leaf");
    // In one copy: both walks reach the page.
    let (_vfs, store) = destination();
    run(&store, vec![(catalog, 0), (catalog, 1)]);
    assert!(store.retain_held_block(pinned_leaf).unwrap());
}

#[test]
fn the_parallel_mark_reaches_exactly_the_graph_a_copy_reads() {
    let mut graph = TestGraph::new();
    let first = graph.tree("mark", 6, 6, 6);
    let second = graph.tree("mark-second", 3, 3, 3);
    let both = graph.node(&[first, second, first]);
    let unreachable = graph.tree("garbage", 2, 2, 2);
    let stop = AtomicBool::new(false);
    for parallel in [false, true] {
        let marked = Mutex::new(HashSet::new());
        let visited = visit_graph(&graph, vec![both], parallel, &stop, None, &|ids: &[BlockId]| {
            marked.lock().unwrap().extend(ids.iter().copied());
            Ok(())
        })
        .unwrap();
        let marked = marked.into_inner().unwrap();
        assert_eq!(marked, graph.reachable(both), "parallel={parallel}");
        assert!(!marked.contains(&unreachable));
        assert!(visited >= marked.len() as u64);
    }
    let mut broken = TestGraph::new();
    let root = broken.tree("broken", 8, 8, 2);
    let interior = broken.children(broken.children(root)[4])[3];
    broken.blocks.remove(&interior);
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let stop = AtomicBool::new(false);
        let result = visit_graph(&broken, vec![root], true, &stop, None, &|_: &[BlockId]| Ok(()));
        let _ = sender.send(result.map(|_| ()).map_err(|error| error.kind()));
    });
    let result = receiver
        .recv_timeout(std::time::Duration::from_secs(60))
        .expect("the failed parallel mark must end");
    assert_eq!(result, Err(io::ErrorKind::NotFound));
}

/// The most children that were read while their own interior child was not
/// yet read: a lower bound on how many nodes the copy held open at once.
fn peak_open_children(graph: &TestGraph, grandchild_of: &HashMap<BlockId, BlockId>) -> usize {
    let log = graph.read_log.lock().unwrap();
    let position: HashMap<BlockId, usize> = log.iter().enumerate().map(|(index, id)| (*id, index)).collect();
    let mut events = Vec::new();
    for (child, grandchild) in grandchild_of {
        events.push((position[child], 1_i64));
        events.push((position[grandchild], -1_i64));
    }
    events.sort();
    let (mut open, mut peak) = (0_i64, 0_i64);
    for (_, delta) in events {
        open += delta;
        peak = peak.max(open);
    }
    peak as usize
}

/// However wide a node's interior fan-out, the copy holds only about as many
/// nodes open (read, payload kept, children pending) as it has workers: a
/// production generation names ~156,000 history segments of up to 4 MiB
/// under a few manifest pages, and opening a whole shard queue of them at
/// once would hold hundreds of GiB.
#[test]
fn open_nodes_stay_bounded_by_the_workers_however_wide_the_graph() {
    let mut graph = TestGraph::new();
    let mut children = Vec::new();
    let mut grandchild_of = HashMap::new();
    for index in 0..1_000 {
        let leaves = [graph.leaf(&format!("wide-{index}-a")), graph.leaf(&format!("wide-{index}-b"))];
        let grandchild = graph.node(&leaves);
        let child = graph.node(&[grandchild]);
        grandchild_of.insert(child, grandchild);
        children.push(child);
    }
    let root = graph.node(&children);
    let depth = 3;
    for parallel in [false, true] {
        graph.read_log.lock().unwrap().clear();
        let (vfs, store) = destination();
        if parallel {
            vfs.allow_concurrent_file_publication().unwrap();
        }
        let untyped = closed();
        let outcome = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, root).unwrap();
        for id in graph.reachable(root) {
            assert!(store.retain_held_block(id).unwrap(), "parallel={parallel}");
        }
        let peak = peak_open_children(&graph, &grandchild_of);
        assert!(peak <= SHARDS + depth, "parallel={parallel}: {peak} children were open at once");
        assert!(outcome.metrics.peak_open_nodes as usize <= SHARDS + depth, "{:?}", outcome.metrics);
        if !parallel {
            assert!(peak <= depth, "one worker descends before it widens: {peak} open");
        }
    }
}

/// A child shared by a deep and a shallow parent is reached after it opened;
/// with every slot held the copy still finishes (no wait cycle on slots).
#[test]
fn a_child_shared_across_depths_never_deadlocks_the_slots() {
    let mut graph = TestGraph::new();
    let shared_leaf = graph.leaf("shared-below");
    let shared = graph.node(&[shared_leaf]);
    let mut chain = shared;
    for level in 0..6 {
        let filler = graph.leaf(&format!("chain-{level}"));
        chain = graph.node(&[chain, filler]);
    }
    let mut wide = vec![chain];
    for index in 0..(SHARDS * 3) {
        let leaf = graph.leaf(&format!("slot-{index}"));
        let inner = graph.node(&[shared, leaf]);
        wide.push(graph.node(&[inner]));
    }
    let root = graph.node(&wide);
    let graph = Arc::new(graph);
    for parallel in [false, true] {
        let (sender, receiver) = std::sync::mpsc::channel();
        let graph = Arc::clone(&graph);
        // Detached: a hung copy fails the receive below instead of the test.
        std::thread::spawn(move || {
            let (vfs, store) = destination();
            if parallel {
                vfs.allow_concurrent_file_publication().unwrap();
            }
            let untyped = closed();
            let result = copy(&graph, &store, PresentBlocks::Closed { untyped: &untyped }, root).map(|_| ());
            let complete = graph.reachable(root).into_iter().all(|id| store.retain_held_block(id).unwrap());
            let _ = sender.send((result.map_err(|error| error.kind()), complete));
        });
        let (result, complete) = receiver
            .recv_timeout(std::time::Duration::from_secs(120))
            .expect("the copy must finish");
        assert_eq!(result, Ok(()), "parallel={parallel}");
        assert!(complete, "parallel={parallel}");
    }
}

#[test]
fn an_unverified_store_walks_present_interiors_once_per_copy() {
    let mut graph = TestGraph::new();
    let shared = graph.tree("shared", 2, 2, 2);
    let root = graph.node(&[shared, shared]);
    let (_vfs, store) = destination();
    copy(&graph, &store, PresentBlocks::Unverified, root).unwrap();
    graph.reads.store(0, Ordering::Relaxed);
    let again = copy(&graph, &store, PresentBlocks::Unverified, root).unwrap();
    let interiors = graph
        .reachable(root)
        .into_iter()
        .filter(|id| !graph.leaves.contains(id))
        .count() as u64;
    assert_eq!(graph.reads.load(Ordering::Relaxed), interiors);
    assert_eq!(again.metrics.copied_blocks, 0);
}

/// Overlapping reads: each read stays open briefly and the peak of reads
/// open at once is recorded.
struct OverlapGraph<'a> {
    inner: &'a TestGraph,
    open: AtomicU64,
    peak: AtomicU64,
}

impl<'a> OverlapGraph<'a> {
    fn new(inner: &'a TestGraph) -> Self {
        Self { inner, open: AtomicU64::new(0), peak: AtomicU64::new(0) }
    }
}

impl CopyGraph for OverlapGraph<'_> {
    type Node = BlockId;
    fn block_id(&self, node: &BlockId) -> BlockId { *node }
    fn is_leaf(&self, node: &BlockId) -> bool { self.inner.is_leaf(node) }
    fn is_untyped(&self, node: &BlockId) -> bool { self.inner.is_untyped(node) }
    fn read(&self, node: &BlockId) -> io::Result<Vec<u8>> {
        let open = self.open.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(open, Ordering::SeqCst);
        std::thread::sleep(std::time::Duration::from_millis(2));
        self.open.fetch_sub(1, Ordering::SeqCst);
        self.inner.read(node)
    }
    fn references(&self, node: &BlockId, payload: &[u8]) -> io::Result<Vec<BlockId>> {
        self.inner.references(node, payload)
    }
}

/// An online epoch copies beside the foreground on the same mount: queuing
/// behind the writer halves its concurrent I/O, each calm window adds one
/// operation, and it always keeps one so the copy progresses.
#[test]
fn the_foreground_share_halves_under_admission_pressure_and_grows_one_per_calm_window() {
    let share = ForegroundShare::starting_at(1, false);
    let mut state = share.lock();
    state.sampled(false);
    state.sampled(false);
    assert_eq!(state.admitted, 3);
    state.sampled(true);
    assert_eq!(state.admitted, 1);
    state.sampled(true);
    assert_eq!(state.admitted, 1, "a pressured copy still progresses");
    for _ in 0..2 * SHARDS {
        state.sampled(false);
    }
    assert_eq!(state.admitted, SHARDS, "never more operations than workers");
    drop(state);
    assert_eq!(ForegroundShare::new().admitted(), 1, "an epoch starts with one operation");
}

#[test]
fn foreground_requests_queuing_for_the_writer_shrink_a_running_share() {
    let share = ForegroundShare::starting_at(SHARDS, true);
    {
        let _queued = crate::fsync_stats::ForegroundWait::start();
        std::thread::sleep(crate::fsync_stats::FOREGROUND_SAMPLE_WINDOW + std::time::Duration::from_millis(50));
    }
    let stop = AtomicBool::new(false);
    drop(share.enter(&stop, "backup copy stopping").unwrap());
    assert_eq!(share.admitted(), SHARDS / 2);
}

#[test]
fn a_shared_copy_never_overlaps_more_reads_than_admitted_and_still_completes() {
    let mut graph = TestGraph::new();
    let root = graph.tree("shared", 8, 8, 8);
    let untyped = closed();
    let stop = AtomicBool::new(false);
    let run = |share: Option<&ForegroundShare>| {
        let (vfs, store) = destination();
        vfs.allow_concurrent_file_publication().unwrap();
        let overlap = OverlapGraph::new(&graph);
        let outcome = copy_graph(
            &GraphCopy {
                graph: &overlap,
                destination: &store,
                present: PresentBlocks::Closed { untyped: &untyped },
                record_untyped: None,
                stop: &stop,
                progress: None,
                share,
            },
            vec![root],
        )
        .unwrap();
        assert_eq!(outcome.metrics.copied_blocks as usize, graph.reachable(root).len());
        assert_closed(&graph, &store);
        overlap.peak.load(Ordering::SeqCst)
    };
    assert!(run(None) > 2, "the unshared copy overlaps its reads");
    let share = ForegroundShare::starting_at(2, false);
    assert!(run(Some(&share)) <= 2);
}

#[test]
fn a_shared_mark_never_overlaps_more_reads_than_admitted() {
    let mut graph = TestGraph::new();
    let root = graph.tree("mark-shared", 6, 6, 6);
    let overlap = OverlapGraph::new(&graph);
    let share = ForegroundShare::starting_at(1, false);
    let stop = AtomicBool::new(false);
    let marked = Mutex::new(HashSet::new());
    visit_graph(&overlap, vec![root], true, &stop, Some(&share), &|ids: &[BlockId]| {
        marked.lock().unwrap().extend(ids.iter().copied());
        Ok(())
    })
    .unwrap();
    assert_eq!(marked.into_inner().unwrap(), graph.reachable(root));
    assert_eq!(overlap.peak.load(Ordering::SeqCst), 1);
}

#[test]
fn a_stop_ends_a_wait_for_admission() {
    let share = ForegroundShare::starting_at(1, false);
    let stop = AtomicBool::new(false);
    let held = share.enter(&stop, "backup copy stopping").unwrap();
    std::thread::scope(|scope| {
        let waiter = scope.spawn(|| share.enter(&stop, "backup copy stopping").map(|_| ()));
        std::thread::sleep(std::time::Duration::from_millis(50));
        stop.store(true, Ordering::Release);
        let error = waiter.join().unwrap().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    });
    drop(held);
}
