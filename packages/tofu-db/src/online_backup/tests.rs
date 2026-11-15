//! Online-backup epoch tests. Fault-injection coverage of the shared
//! copy/manifest machinery lives in `backup.rs`'s own matrix; these tests
//! pin the *online* properties: anchoring under load, GC-anchor
//! interaction, incrementalism, retention, and doorbell receipts.

use super::*;
use crate::authority::AuthorityDatabase;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::authority_gc_online::OnlineGarbageCollector;
use crate::entity::EntityKey;
use crate::vfs::DeterministicVfs;

fn authority() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    vfs.create_dir(Path::new("/backup")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

const STORE: &str = "/backup/closed";

fn context(vfs: &Arc<DeterministicVfs>) -> EpochContext {
    EpochContext {
        backup_dir: PathBuf::from("/backup"),
        store_dir: PathBuf::from(STORE),
        closed_store: true,
        destination_vfs: vfs.clone(),
        retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
        _fastpath_lease: None,
        automatic_epoch_owner: None,
    }
}

fn fastpath_context(
    shadow: PathBuf,
    destination_vfs: Arc<dyn Vfs>,
    lease: Arc<crate::fastpath::FastpathLease>,
) -> EpochContext {
    EpochContext {
        backup_dir: shadow.clone(),
        store_dir: shadow,
        closed_store: false,
        destination_vfs,
        retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
        _fastpath_lease: Some(lease),
        automatic_epoch_owner: None,
    }
}

fn commit_value(database: &Arc<Mutex<AuthorityDatabase>>, key: &[u8], value: &[u8]) {
    let mut database = database.lock().unwrap();
    let key = EntityKey::new(7, 11, "record", key).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key, value.to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
}

fn run(driver: &Arc<Mutex<AuthorityDatabase>>, context: &EpochContext) -> OnlineBackupEpochReport {
    let stop = AtomicBool::new(false);
    run_epoch(driver, context, &stop, &EpochStatus::default()).unwrap()
}

pub(super) fn restore_store(vfs: &Arc<DeterministicVfs>, target: &str) -> crate::backup::BackupManifest {
    vfs.create_dir(Path::new(target)).unwrap();
    crate::backup::restore_latest_backup_with_vfs(Path::new(STORE), Path::new(target), vfs.clone())
        .unwrap()
}

pub(super) fn read_value(database: &AuthorityDatabase, key: &[u8]) -> Option<Vec<u8>> {
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_get(&mut transaction, &EntityKey::new(7, 11, "record", key).unwrap())
        .unwrap()
}

/// Seed one atomic commit of `count` small values under the generous
/// default budget (no seed commit trips the commit-path trigger), leaving
/// its whole delta pending in the write overlay.
fn seed_overlay_commit(database: &Arc<Mutex<AuthorityDatabase>>, prefix: &str, count: u32) {
    let mut guard = database.lock().unwrap();
    let mut transaction = guard.begin(7, 11).unwrap();
    for index in 0..count {
        guard
            .entity_put(
                &mut transaction,
                EntityKey::new(7, 11, "record", format!("{prefix}-{index}").as_bytes()).unwrap(),
                vec![7_u8; 24],
            )
            .unwrap();
    }
    guard.commit(transaction).unwrap();
}

/// The anchor's authority hold copies state; it never merges, checkpoints
/// or writes a file (2026-11-08 02:00: a synchronous checkpoint inside this
/// hold wedged the daemon until the supervisor recycled it).
#[test]
fn the_anchor_hold_writes_nothing_and_leaves_overlay_and_wal_tail_in_place() {
    let (vfs, database) = authority();
    database.lock().unwrap().set_write_overlay_for_test(true);
    seed_overlay_commit(&database, "deferred", 600);
    commit_value(&database, b"tail", b"value-tail");
    let mut guard = database.lock().unwrap();
    let generation = guard.writer_engine().unwrap().state().generation;
    let entries = guard.entity_overlay_entries_for_test();
    assert!(entries >= 600, "the seeded deltas must still be pending");
    assert!(guard.writer_engine().unwrap().has_active_transactions());
    vfs.arm_fault(None).unwrap();
    let anchor = guard.capture_backup_anchor().unwrap();
    let trace = vfs.trace().unwrap();
    assert!(
        trace.iter().all(|operation| !matches!(
            operation,
            crate::vfs::Operation::Write
                | crate::vfs::Operation::SetLen
                | crate::vfs::Operation::SyncData
                | crate::vfs::Operation::SyncAll
                | crate::vfs::Operation::SyncDirectory
                | crate::vfs::Operation::Rename
                | crate::vfs::Operation::CreateDir
                | crate::vfs::Operation::RemoveFile
        )),
        "the anchor hold performed file I/O: {trace:?}"
    );
    assert_eq!(guard.writer_engine().unwrap().state().generation, generation, "no CONTROL publication");
    assert_eq!(guard.entity_overlay_entries_for_test(), entries, "no overlay merge");
    assert!(guard.writer_engine().unwrap().has_active_transactions(), "no checkpoint");
    guard.deregister_backup_anchor(anchor.anchor_token);
}

/// A generation anchored with deltas still in the write overlay and commits
/// after the last checkpoint restores every one of them: the tail becomes
/// history segments in the backup and the restored root replays them.
#[test]
fn an_epoch_over_an_unmerged_overlay_and_wal_tail_restores_every_commit() {
    let (vfs, database) = authority();
    database.lock().unwrap().set_write_overlay_for_test(true);
    commit_value(&database, b"checkpointed", b"value-checkpointed");
    database.lock().unwrap().checkpoint_for_test().unwrap();
    seed_overlay_commit(&database, "deferred", 600);
    commit_value(&database, b"tail", b"value-tail");
    let source_sequence = database.lock().unwrap().writer_engine().unwrap().state().durable_sequence;
    let report = run(&database, &context(&vfs));
    assert_eq!(report.durable_sequence, source_sequence, "the generation includes the uncheckpointed tail");
    assert!(report.tail_transactions >= 2);
    let manifest = restore_store(&vfs, "/restore");
    assert_eq!(manifest.durable_sequence, source_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    assert_eq!(read_value(&restored, b"checkpointed").as_deref(), Some(&b"value-checkpointed"[..]));
    assert_eq!(read_value(&restored, b"tail").as_deref(), Some(&b"value-tail"[..]));
    for index in [0, 299, 599] {
        assert_eq!(
            read_value(&restored, format!("deferred-{index}").as_bytes()),
            Some(vec![7_u8; 24]),
            "deferred-{index}"
        );
    }
}

/// An epoch's work follows what is new: after one more commit the copy
/// skips every subgraph the closed store already holds, so it reads and
/// copies a few blocks, not the authority again. With nothing new it
/// reuses the published generation.
#[test]
fn an_epoch_reads_and_copies_only_what_the_closed_store_lacks() {
    let (vfs, database) = authority();
    database.lock().unwrap().set_write_overlay_for_test(false);
    for index in 0..400_u32 {
        commit_value(&database, format!("bulk-{index}").as_bytes(), &[3_u8; 96]);
    }
    let context = context(&vfs);
    let first = run(&database, &context);
    assert!(first.copied_blocks > 50, "{first:?}");
    let unchanged = run(&database, &context);
    assert_eq!((unchanged.copied_blocks, unchanged.source_reads), (0, 0));
    assert_eq!(unchanged.manifest, first.manifest);
    commit_value(&database, b"bulk-7", &[4_u8; 96]);
    let second = run(&database, &context);
    assert!(second.durable_sequence > first.durable_sequence);
    assert!(second.present_subgraphs > 0);
    assert!(
        second.source_reads * 10 < first.source_reads && second.copied_blocks * 10 < first.copied_blocks,
        "first read {} and copied {}; second read {} and copied {}",
        first.source_reads, first.copied_blocks, second.source_reads, second.copied_blocks
    );
    let manifest = restore_store(&vfs, "/restore");
    assert_eq!(manifest.durable_sequence, second.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    assert_eq!(read_value(&restored, b"bulk-7"), Some(vec![4_u8; 96]));
    assert_eq!(read_value(&restored, b"bulk-399"), Some(vec![3_u8; 96]));
}

#[test]
fn epoch_publishes_a_restorable_manifest_and_reports_the_receipt_shape() {
    let (_vfs, database) = authority();
    commit_value(&database, b"alpha", b"value-alpha");
    let context = context(&_vfs);
    let report = run(&database, &context);
    assert!(report.ok);
    assert_eq!(report.backend, "tofudb");
    assert_eq!(report.artifact_kind, "directory");
    assert!(!report.backup.is_empty());
    assert!(!report.manifest.is_empty());
    assert!(report.bytes >= report.manifest_bytes);
    assert_eq!(report.sha256.len(), 64);
    assert!(report.durable_sequence >= 1);
    assert!(report.copied_blocks >= 1);
    // The destination BlockStore holds the anchor-reachable blocks: a
    // second authority restores from it without the source.
    let destination_blocks = report.copied_block_bytes;
    assert!(destination_blocks > 0);
    // Restore through the certified offline path against the same vfs.
    assert_eq!(report.backup, STORE);
    let manifest = restore_store(&_vfs, "/restore");
    assert_eq!(manifest.durable_sequence, report.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), _vfs.clone()).unwrap();
    let key = EntityKey::new(7, 11, "record", b"alpha").unwrap();
    let mut transaction = restored.begin(7, 11).unwrap();
    assert_eq!(
        restored.entity_get(&mut transaction, &key).unwrap(),
        Some(b"value-alpha".to_vec())
    );
}

#[test]
fn second_epoch_copies_only_new_blocks() {
    let (_vfs, database) = authority();
    for index in 0..400_u32 {
        commit_value(
            &database,
            format!("bulk-{index}").as_bytes(),
            format!("value-bulk-{index}-{}", "x".repeat(80)).as_bytes(),
        );
    }
    let context = context(&_vfs);
    let first = run(&database, &context);
    commit_value(&database, b"two", b"value-two");
    let second = run(&database, &context);
    assert!(second.durable_sequence > first.durable_sequence);
    assert!(
        second.copied_blocks < first.copied_blocks,
        "incremental epoch copies only the delta: first={} second={}",
        first.copied_blocks,
        second.copied_blocks
    );
    assert!(second.copied_blocks >= 1);
}

#[test]
fn registered_anchor_protects_anchor_history_from_online_gc() {
    let (_vfs, database) = authority();
    commit_value(&database, b"anchored", b"value-anchored");
    let anchor = {
        let mut database_guard = database.lock().unwrap();
        database_guard.checkpoint_for_test().unwrap();
        database_guard.capture_backup_anchor().unwrap()
    };
    let history_blocks = anchor.state.history_manifest_block_id;
    assert!(history_blocks.is_some());
    // Advance the authority and retire the anchored history segment.
    commit_value(&database, b"anchored", b"value-rewritten");
    {
        let mut database_guard = database.lock().unwrap();
        database_guard.checkpoint_for_test().unwrap();
        database_guard.compact_history(1).unwrap();
    }
    // Two full GC epochs would ordinarily confirm and collect the retired
    // segment's blocks; the registered anchor keeps them marked.
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        AuthorityGarbageCollectionBudget::conservative(),
        Duration::from_millis(1),
    );
    let stop = AtomicBool::new(false);
    collector.run_epoch(&database, &stop).unwrap();
    collector.run_epoch(&database, &stop).unwrap();
    let anchored_history = history_blocks.unwrap();
    {
        let database_guard = database.lock().unwrap();
        assert!(
            database_guard.read_block_for_test(anchored_history).is_ok(),
            "anchor-referenced history manifest must survive online GC"
        );
    }
    // The off-mutex copy resolves every anchored reference.
    let context = context(&_vfs);
    let (destination, _) =
        crate::backup::initialize_or_open_backup(&context.backup_dir, _vfs.clone()).unwrap();
    let (manifest, built) = crate::backup::plan_anchored_generation(
        &anchor.state, anchor.history.as_ref(), &anchor.tail).unwrap();
    crate::backup::copy_generation(
        &anchor.source,
        &destination,
        None,
        &DestinationPolicy::unverified(),
        &crate::backup::GenerationCopyPlan { manifest: &manifest, built },
        &stop,
        None,
        None,
    )
    .unwrap();
    {
        let database_guard = database.lock().unwrap();
        database_guard.deregister_backup_anchor(anchor.anchor_token);
    }
}

/// Destination that stops an epoch's copy at its first block write: it waits
/// there until released, or panics there. The epoch's anchor is registered
/// meanwhile, so whatever runs before the release runs mid-copy.
pub(super) struct FirstBlockWriteGate {
    inner: Arc<DeterministicVfs>,
    blocks: PathBuf,
    panics: bool,
    armed: AtomicBool,
    entered: std::sync::mpsc::SyncSender<()>,
    released: Mutex<bool>,
    signal: Condvar,
}

impl FirstBlockWriteGate {
    pub(super) fn new(
        inner: &Arc<DeterministicVfs>,
        panics: bool,
    ) -> (Arc<Self>, std::sync::mpsc::Receiver<()>) {
        let (entered, reached) = std::sync::mpsc::sync_channel(1);
        let gate = Arc::new(Self {
            inner: inner.clone(),
            blocks: Path::new(STORE).join("blocks"),
            panics,
            armed: AtomicBool::new(true),
            entered,
            released: Mutex::new(false),
            signal: Condvar::new(),
        });
        (gate, reached)
    }

    pub(super) fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.signal.notify_all();
    }
}

impl Vfs for FirstBlockWriteGate {
    fn open(
        &self,
        path: &Path,
        request: crate::vfs::OpenRequest,
    ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        if request.write && path.starts_with(&self.blocks) && self.armed.swap(false, Ordering::AcqRel) {
            assert!(!self.panics, "injected panic at the copy's first block write");
            self.entered
                .send(())
                .map_err(|_| io::Error::other("gate receiver closed"))?;
            let mut released = self.released.lock().unwrap();
            while !*released {
                released = self.signal.wait(released).unwrap();
            }
        }
        self.inner.open(path, request)
    }
    fn open_positioned_reader(
        &self,
        path: &Path,
    ) -> io::Result<Option<Arc<dyn crate::vfs::VfsPositionedReader>>> {
        self.inner.open_positioned_reader(path)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir(path)
    }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        self.inner.rename(source, destination)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> {
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }
    fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<crate::vfs::DirectoryEntry>> {
        self.inner.read_directory_entries(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum_entries: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum_entries)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(
            path, name_prefix, maximum_entries_scanned, maximum_matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

pub(super) const ANCHORED_RECORDS: u32 = 300;

pub(super) fn anchored_record(index: u32) -> Vec<u8> {
    format!("anchored-{index}").into_bytes()
}

/// A checkpointed generation of distinct records spread over many entity
/// pages; returns its state root.
pub(super) fn seed_anchored_generation(database: &Arc<Mutex<AuthorityDatabase>>) -> BlockId {
    database.lock().unwrap().set_write_overlay_for_test(false);
    for index in 0..ANCHORED_RECORDS {
        commit_value(database, &anchored_record(index), format!("anchored-value-{index:0>80}").as_bytes());
    }
    let mut guard = database.lock().unwrap();
    guard.checkpoint_for_test().unwrap();
    guard.writer_engine().unwrap().state().authority_state_root.flatten().unwrap()
}

/// Rewrites every record and retires the history of the old values: from
/// here on only a backup anchor can still reference the old generation.
fn supersede_anchored_generation(database: &Arc<Mutex<AuthorityDatabase>>) {
    for index in 0..ANCHORED_RECORDS {
        commit_value(database, &anchored_record(index), format!("current-value-{index:0>80}").as_bytes());
    }
    let mut guard = database.lock().unwrap();
    guard.checkpoint_for_test().unwrap();
    guard.compact_history(1).unwrap();
}

fn complete_gc_epochs(collector: &mut OnlineGarbageCollector, database: &Arc<Mutex<AuthorityDatabase>>) {
    let stop = AtomicBool::new(false);
    for _ in 0..2 {
        assert_eq!(
            collector.run_epoch(database, &stop).unwrap(),
            crate::authority_gc_online::OnlineGarbageCollectionOutcome::EpochComplete
        );
    }
}

fn block_is_reclaimed(database: &Arc<Mutex<AuthorityDatabase>>, id: BlockId) -> bool {
    database
        .lock()
        .unwrap()
        .read_block_for_test(id)
        .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
}

/// Online GC deletes every confirmed victim (2026-11-09). Two complete GC
/// epochs that run while an epoch's copy waits at its first block write, after
/// foreground writes and history retirement left the epoch's anchor as the
/// only reference to the generation it copies, still delete unreferenced
/// garbage but nothing that generation needs: the copy completes and restores
/// the anchored values. Once the epoch ends, the same epochs reclaim it.
#[test]
fn gc_epochs_during_a_copy_never_reclaim_what_its_anchor_references() {
    let (vfs, database) = authority();
    let anchored_root = seed_anchored_generation(&database);
    let (gate, reached) = FirstBlockWriteGate::new(&vfs, false);
    let epoch = {
        let database = database.clone();
        let mut context = context(&vfs);
        context.destination_vfs = gate.clone();
        std::thread::spawn(move || {
            run_epoch(&database, &context, &AtomicBool::new(false), &EpochStatus::default())
        })
    };
    if reached.recv_timeout(Duration::from_secs(60)).is_err() {
        gate.release();
        panic!("the epoch never reached its first block write: {:?}", epoch.join());
    }
    supersede_anchored_generation(&database);
    let garbage = database.lock().unwrap().write_orphan_block_for_test(b"unanchored-garbage").unwrap();
    let mut collector = OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
    complete_gc_epochs(&mut collector, &database);
    let reclaimed_mid_copy = collector.handle().metrics().total_reclaimed_blocks;
    assert!(block_is_reclaimed(&database, garbage), "the collector deletes garbage mid-copy");
    assert!(
        !block_is_reclaimed(&database, anchored_root),
        "the collector deleted the anchored root while the copy still needed it"
    );
    gate.release();
    let report = epoch.join().unwrap().expect("the copy completes with every anchored block");
    assert!(report.copied_blocks > 1, "{report:?}");
    let manifest = restore_store(&vfs, "/restore");
    assert_eq!(manifest.durable_sequence, report.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    for index in [0, ANCHORED_RECORDS / 2, ANCHORED_RECORDS - 1] {
        assert_eq!(
            read_value(&restored, &anchored_record(index)),
            Some(format!("anchored-value-{index:0>80}").into_bytes())
        );
    }
    complete_gc_epochs(&mut collector, &database);
    assert!(
        block_is_reclaimed(&database, anchored_root),
        "an ended epoch's anchor keeps nothing alive"
    );
    assert!(collector.handle().metrics().total_reclaimed_blocks > reclaimed_mid_copy);
}

/// The anchor no longer checkpoints: commits after the last checkpoint reach
/// the copy as the anchor's committed tail, whose blocks only the tail block
/// IDs the anchor registers name once the foreground supersedes those records
/// and history retires their checkpoint. GC epochs mid-copy keep every such
/// block, the restore replays the tail, and after the epoch they are reclaimed.
#[test]
fn gc_epochs_during_a_copy_never_reclaim_the_anchors_committed_tail() {
    let (vfs, database) = authority();
    seed_anchored_generation(&database);
    for index in 0..40_u32 {
        commit_value(&database, format!("tail-{index}").as_bytes(), format!("tail-value-{index:0>80}").as_bytes());
    }
    let tail_blocks: Vec<BlockId> = database
        .lock()
        .unwrap()
        .writer_engine()
        .unwrap()
        .active_transactions_snapshot()
        .iter()
        .flat_map(|transaction| transaction.envelope.block_ids.iter().copied())
        .collect();
    assert!(!tail_blocks.is_empty(), "the uncheckpointed commits must name blocks");
    let (gate, reached) = FirstBlockWriteGate::new(&vfs, false);
    let epoch = {
        let database = database.clone();
        let mut context = context(&vfs);
        context.destination_vfs = gate.clone();
        std::thread::spawn(move || {
            run_epoch(&database, &context, &AtomicBool::new(false), &EpochStatus::default())
        })
    };
    if reached.recv_timeout(Duration::from_secs(60)).is_err() {
        gate.release();
        panic!("the epoch never reached its first block write: {:?}", epoch.join());
    }
    for index in 0..40_u32 {
        commit_value(&database, format!("tail-{index}").as_bytes(), b"superseded-tail");
    }
    supersede_anchored_generation(&database);
    {
        // One more checkpoint, so the retained history no longer holds the
        // checkpoint that took the anchored tail out of the WAL.
        commit_value(&database, b"after-tail", b"after-tail-value");
        let mut guard = database.lock().unwrap();
        guard.checkpoint_for_test().unwrap();
        guard.compact_history(1).unwrap();
    }
    let mut collector = OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
    complete_gc_epochs(&mut collector, &database);
    let reclaimed: Vec<BlockId> =
        tail_blocks.iter().copied().filter(|id| block_is_reclaimed(&database, *id)).collect();
    assert!(reclaimed.is_empty(), "the collector deleted {} anchored tail blocks mid-copy", reclaimed.len());
    gate.release();
    let report = epoch.join().unwrap().expect("the copy completes with the anchored tail");
    assert!(report.tail_transactions > 0, "{report:?}");
    restore_store(&vfs, "/restore");
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    for index in [0_u32, 39] {
        assert_eq!(
            read_value(&restored, format!("tail-{index}").as_bytes()),
            Some(format!("tail-value-{index:0>80}").into_bytes())
        );
    }
    complete_gc_epochs(&mut collector, &database);
    assert!(
        tail_blocks.iter().any(|id| block_is_reclaimed(&database, *id)),
        "the anchored tail stays referenced after the epoch ended"
    );
}

/// An epoch releases its anchor however it ends: a panic in its copy leaves
/// nothing registered, so the superseded generation is still reclaimed.
#[test]
fn an_epoch_that_panics_mid_copy_still_releases_its_anchor() {
    let (vfs, database) = authority();
    let anchored_root = seed_anchored_generation(&database);
    let (gate, _reached) = FirstBlockWriteGate::new(&vfs, true);
    let mut context = context(&vfs);
    context.destination_vfs = gate;
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        run_epoch(&database, &context, &AtomicBool::new(false), &EpochStatus::default())
    }));
    assert!(outcome.is_err(), "the injected copy panic propagates");
    supersede_anchored_generation(&database);
    let mut collector = OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
    complete_gc_epochs(&mut collector, &database);
    assert!(
        block_is_reclaimed(&database, anchored_root),
        "a panicked epoch's anchor still pins the generation it copied"
    );
}

#[test]
fn epochs_prune_to_the_retained_generation_count() {
    let (_vfs, database) = authority();
    let context = context(&_vfs);
    for value in [b"a", b"b", b"c"] {
        commit_value(&database, b"rolling", value);
        run(&database, &context);
    }
    let manifests =
        crate::backup::existing_manifests(&Path::new(STORE).join("snapshots"), _vfs.as_ref()).unwrap();
    assert_eq!(
        manifests.len(),
        ONLINE_BACKUP_RETAINED_GENERATIONS,
        "retention keeps exactly the newest verified generations"
    );
}

#[test]
fn conflicting_backup_lease_defers_the_second_epoch() {
    let (_vfs, database) = authority();
    commit_value(&database, b"lease", b"value-lease");
    let context = context(&_vfs);
    // Establish a valid backup layout first; an empty directory with only a
    // lease file is (correctly) rejected as an invalid backup target.
    run(&database, &context);
    let _lease = crate::backup::acquire_backup_lease(Path::new(STORE), _vfs.as_ref()).unwrap();
    let stop = AtomicBool::new(false);
    let error = run_epoch(&database, &context, &stop, &EpochStatus::default()).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
}

#[test]
fn driver_answers_a_doorbell_with_a_receipt() {
    let (_vfs, database) = authority();
    commit_value(&database, b"doorbell", b"value-doorbell");
    let driver = OnlineBackupDriver::start_with_destination_vfs(
        &database,
        PathBuf::from("/backup"),
        _vfs.clone(),
        None,
    )
    .unwrap();
    let observer = driver.observer();
    let deadline = now_unix_ms() + 30_000;
    let report = observer.run_and_wait(deadline).unwrap();
    assert!(report.ok);
    assert!(observer.metrics().epochs_completed >= 1);
    assert!(observer.require_healthy().is_ok());
    driver.stop_and_join().unwrap();
}

/// Fails the first barrier of a hash-shard directory without performing it,
/// as an I/O error between a batch's renames and its barrier would.
struct FailFirstShardBarrierVfs {
    inner: Arc<DeterministicVfs>,
    armed: AtomicBool,
}
impl crate::vfs::Vfs for FailFirstShardBarrierVfs {
    fn open(&self, path: &Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        self.inner.open(path, request)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.inner.rename(source, destination) }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.inner.remove_file(path) }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        let shard = path.parent().is_some_and(|parent| parent.ends_with("closed/blocks"));
        if shard && self.armed.swap(false, Ordering::AcqRel) {
            return Err(io::Error::other("injected shard barrier failure"));
        }
        self.inner.sync_directory(path)
    }
}

/// A batch whose shard barrier failed left renamed names that are visible
/// but not durable. The next epoch in the same process trusts presence, so
/// the closed store syncs every shard before that copy: a crash after the
/// second epoch publishes still restores the whole generation.
#[test]
fn names_a_failed_shard_barrier_left_are_durable_before_a_later_epoch_trusts_them() {
    let (vfs, database) = authority();
    database.lock().unwrap().set_write_overlay_for_test(false);
    for index in 0..300_u32 {
        commit_value(&database, format!("barrier-{index}").as_bytes(), &[5_u8; 64]);
    }
    let failing = Arc::new(FailFirstShardBarrierVfs { inner: vfs.clone(), armed: AtomicBool::new(true) });
    let context = EpochContext { destination_vfs: failing.clone(), ..context(&vfs) };
    let stop = AtomicBool::new(false);
    let error = run_epoch(&database, &context, &stop, &EpochStatus::default()).unwrap_err();
    assert!(error.to_string().contains("injected shard barrier failure"), "{error}");
    assert!(!failing.armed.load(Ordering::Acquire));
    let report = run(&database, &context);
    assert!(report.ok);
    vfs.crash().unwrap();
    let manifest = restore_store(&vfs, "/restore");
    assert_eq!(manifest.durable_sequence, report.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    for index in [0_u32, 150, 299] {
        assert_eq!(read_value(&restored, format!("barrier-{index}").as_bytes()), Some(vec![5_u8; 64]));
    }
}

/// Fails the first removal of a snapshot manifest (prune's first removal).
struct FailFirstManifestRemovalVfs {
    inner: Arc<DeterministicVfs>,
    armed: AtomicBool,
}
impl crate::vfs::Vfs for FailFirstManifestRemovalVfs {
    fn open(&self, path: &Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        self.inner.open(path, request)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.inner.rename(source, destination) }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        let manifest = path.parent().is_some_and(|parent| parent.ends_with("snapshots"));
        if manifest && self.armed.swap(false, Ordering::AcqRel) {
            return Err(io::Error::other("injected manifest removal failure"));
        }
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> { self.inner.sync_directory(path) }
}

/// Retention runs after publication: a failed prune reports the published
/// generation with its error instead of failing the backup, and the next
/// epoch resumes the committed plan.
#[test]
fn a_failed_prune_keeps_the_published_generation_and_the_next_epoch_resumes_it() {
    let (vfs, database) = authority();
    let failing = Arc::new(FailFirstManifestRemovalVfs { inner: vfs.clone(), armed: AtomicBool::new(true) });
    let mut context = EpochContext { destination_vfs: failing.clone(), ..context(&vfs) };
    context.retention = 1;
    commit_value(&database, b"rolling", b"one");
    let first = run(&database, &context);
    assert_eq!(first.prune_error, None);
    commit_value(&database, b"rolling", b"two");
    let second = run(&database, &context);
    assert!(second.ok);
    assert!(second.prune_error.as_deref().is_some_and(|error| error.contains("injected")), "{second:?}");
    commit_value(&database, b"rolling", b"three");
    let third = run(&database, &context);
    assert_eq!(third.prune_error, None);
    assert_eq!(third.retained_generations, 1);
    assert!(third.pruned_generations >= 2, "{third:?}");
    let manifests =
        crate::backup::existing_manifests(&Path::new(STORE).join("snapshots"), vfs.as_ref()).unwrap();
    assert_eq!(manifests.len(), 1);
    assert_eq!(restore_store(&vfs, "/restore").durable_sequence, third.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    assert_eq!(read_value(&restored, b"rolling").as_deref(), Some(&b"three"[..]));
}

/// Records the bytes every read returns, per path.
struct ReadRecordingVfs {
    inner: Arc<DeterministicVfs>,
    reads: Arc<Mutex<Vec<(PathBuf, usize)>>>,
}
impl crate::vfs::Vfs for ReadRecordingVfs {
    fn open(&self, path: &Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        let file = self.inner.open(path, request)?;
        Ok(Box::new(ReadRecordingFile { inner: file, path: path.to_path_buf(), reads: self.reads.clone() }))
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> { self.inner.create_dir(path) }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> { self.inner.rename(source, destination) }
    fn remove_file(&self, path: &Path) -> io::Result<()> { self.inner.remove_file(path) }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> { self.inner.metadata(path) }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> { self.inner.read_directory(path) }
    fn read_directory_bounded(&self, path: &Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &Path, prefix: &str, maximum: usize, matches: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.inner.match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> { self.inner.sync_directory(path) }
}
struct ReadRecordingFile {
    inner: Box<dyn crate::vfs::VfsFile>,
    path: PathBuf,
    reads: Arc<Mutex<Vec<(PathBuf, usize)>>>,
}
impl crate::vfs::VfsFile for ReadRecordingFile {
    fn len(&self) -> io::Result<u64> { self.inner.len() }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        let bytes = self.inner.read_all(maximum)?;
        self.reads.lock().unwrap().push((self.path.clone(), bytes.len()));
        Ok(bytes)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)?;
        self.reads.lock().unwrap().push((self.path.clone(), bytes.len()));
        Ok(())
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> { self.inner.write_all_at(offset, bytes) }
    fn set_len(&mut self, length: u64) -> io::Result<()> { self.inner.set_len(length) }
    fn sync_data(&mut self) -> io::Result<()> { self.inner.sync_data() }
    fn sync_all(&mut self) -> io::Result<()> { self.inner.sync_all() }
    fn try_lock_exclusive(&mut self) -> io::Result<()> { self.inner.try_lock_exclusive() }
}

#[test]
fn unchanged_generation_reads_no_held_blob_chunk_from_either_store() {
    let inner = Arc::new(DeterministicVfs::new(None));
    for directory in ["/data", "/backup"] {
        inner.create_dir(Path::new(directory)).unwrap();
        inner.sync_directory(Path::new("/")).unwrap();
    }
    let reads = Arc::new(Mutex::new(Vec::new()));
    let vfs = Arc::new(ReadRecordingVfs { inner: inner.clone(), reads: reads.clone() });
    let mut authority = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let document = EntityKey::new(7, 11, "record", b"chunked").unwrap();
    let mut state = 0x2545_f491_4f6c_dd1d_u64;
    let text: String = (0..3 * crate::blob::BLOB_CHUNK_BYTES + 1024).map(|_| {
        state ^= state << 13; state ^= state >> 7; state ^= state << 17;
        char::from(b"0123456789abcdef"[(state & 15) as usize])
    }).collect();
    let mut transaction = authority.begin(7, 11).unwrap();
    crate::versioned_document::put(&authority, &mut transaction, crate::versioned_document::PutRequest {
        key: document.clone(), namespace: "record".into(), logical_key: "chunked".into(),
        value_json: serde_json::to_vec(&text).unwrap(), expected_version: None, updated_at_ms: 1,
    }).unwrap();
    let stored = authority.entity_get(&mut transaction, &document).unwrap().unwrap();
    authority.commit(transaction).unwrap();
    let reference = crate::versioned_document::stored_blob_reference(&stored).unwrap().unwrap();
    let manifest = reference.manifest_block_id;
    let mut chunks = Vec::new();
    crate::blob::visit_blob_references(7, 11, reference, |id| authority.block_store_shared().get(id),
        |id| { chunks.push(id); Ok(()) }).unwrap();
    chunks.retain(|id| *id != manifest);
    assert_eq!(chunks.len(), 4);
    authority.configure_hot_data_caches(0, 0).unwrap();
    let database = Arc::new(Mutex::new(authority));
    let context = context(&inner);
    let context = EpochContext { destination_vfs: vfs.clone(), ..context };
    let first = run(&database, &context);
    assert!(first.copied_blocks >= 5);
    reads.lock().unwrap().clear();
    let second = run(&database, &context);
    assert_eq!(second.copied_blocks, 0);
    let reads = reads.lock().unwrap().clone();
    // Neither store reads a held chunk as a block: not its loose file in
    // either directory, and no source payload-segment read as large as one
    // encoded chunk (each exceeds 256 KiB). Retained history segments are
    // still decoded to enumerate their transactions.
    let loose = |root: &str, id: &BlockId| {
        let name = id.to_hex();
        Path::new(root).join("blocks").join(&name[..2]).join(format!("{name}.blk"))
    };
    let held = chunks.iter().flat_map(|id| [loose(STORE, id), loose("/data", id)]).collect::<Vec<_>>();
    assert!(reads.iter().all(|(path, _)| !held.contains(path)), "{reads:?}");
    let segment_bytes: usize = reads.iter()
        .filter(|(path, _)| path.starts_with("/data/payload-segments"))
        .map(|(_, bytes)| bytes).sum();
    assert!(segment_bytes < 256 * 1024, "unchanged generation read {segment_bytes} source segment bytes");
    let destination_bytes: usize = reads.iter()
        .filter(|(path, _)| path.starts_with("/backup/closed/blocks"))
        .map(|(_, bytes)| bytes).sum();
    println!("unchanged_generation source_segment_bytes={segment_bytes} destination_block_bytes={destination_bytes} reads={}",
        reads.len());
}

#[test]
fn driver_attributes_copy_reads_to_the_online_backup_component() {
    let (vfs, database) = authority();
    commit_value(&database, b"attributed", b"value-attributed");
    let reads = || crate::fsync_stats::component_stage_calls(
        crate::fsync_stats::COMPONENT_ONLINE_BACKUP, crate::fsync_stats::STAGE_BLOCK_GET);
    let before = reads();
    let driver = OnlineBackupDriver::start_with_destination_vfs(
        &database,
        PathBuf::from("/backup"),
        vfs.clone(),
        None,
    )
    .unwrap();
    assert!(driver.observer().run_and_wait(now_unix_ms() + 30_000).unwrap().ok);
    driver.stop_and_join().unwrap();
    assert!(reads() > before, "backup copies must not be billed to foreground");
}

#[test]
fn concurrent_doorbells_each_receive_the_finished_epoch_receipt() {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    let backup_dir = root.path().join("backup");
    std::fs::create_dir(&source).unwrap();
    std::fs::create_dir(&backup_dir).unwrap();
    let database = Arc::new(Mutex::new(AuthorityDatabase::initialize(&source).unwrap()));
    commit_value(&database, b"seed", b"seed-value");
    let (entered, reached) = std::sync::mpsc::sync_channel(1);
    let gate = Arc::new(GatedDestination {
        armed: AtomicBool::new(true),
        fail_writes: AtomicBool::new(false),
        entered,
        released: Mutex::new(false),
        signal: Condvar::new(),
    });
    let driver = OnlineBackupDriver::start_with_destination_vfs(&database, backup_dir, gate.clone(), None)
        .unwrap();
    let observer = driver.observer();
    let first = std::thread::spawn({
        let observer = observer.clone();
        move || observer.run_and_wait(now_unix_ms() + 30_000)
    });
    let reached_result = reached.recv_timeout(Duration::from_secs(10));
    if reached_result.is_err() {
        gate.release();
    }
    assert!(reached_result.is_ok());
    let later: Vec<_> = (0..2)
        .map(|_| {
            let observer = observer.clone();
            std::thread::spawn(move || observer.run_and_wait(now_unix_ms() + 30_000))
        })
        .collect();
    while observer.shared.doorbell.load(Ordering::Acquire) < 3 {
        std::thread::sleep(Duration::from_millis(1));
    }
    // Both later requests wait on the in-flight epoch before it finishes.
    std::thread::sleep(Duration::from_millis(100));
    gate.release();
    let mut receipts = vec![first.join().unwrap()];
    receipts.extend(later.into_iter().map(|waiter| waiter.join().unwrap()));
    driver.stop_and_join().unwrap();
    for receipt in receipts {
        let receipt = receipt.expect("every concurrent doorbell receives a receipt");
        assert!(receipt.ok);
        assert!(Path::new(&receipt.manifest).is_file());
    }
}

struct GatedDestination {
    armed: AtomicBool,
    fail_writes: AtomicBool,
    entered: std::sync::mpsc::SyncSender<()>,
    released: Mutex<bool>,
    signal: Condvar,
}

impl GatedDestination {
    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.signal.notify_all();
    }
}

impl Vfs for GatedDestination {
    fn open(
        &self,
        path: &Path,
        request: crate::vfs::OpenRequest,
    ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        if request.write && self.fail_writes.load(Ordering::Acquire) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "injected shadow write failure",
            ));
        }
        if request.write && self.armed.swap(false, Ordering::AcqRel) {
            self.entered
                .send(())
                .map_err(|_| io::Error::other("gate receiver closed"))?;
            let mut released = self.released.lock().unwrap();
            while !*released {
                released = self.signal.wait(released).unwrap();
            }
        }
        crate::vfs::RealVfs.open(path, request)
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.create_dir(path)
    }
    fn rename(&self, a: &Path, b: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.rename(a, b)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> {
        crate::vfs::RealVfs.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        crate::vfs::RealVfs.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        max: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        crate::vfs::RealVfs.read_directory_bounded(path, max)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        scanned: usize,
        max: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, max)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        crate::vfs::RealVfs.sync_directory(path)
    }
}

fn native_fastpath() -> (
    tempfile::TempDir,
    crate::fastpath::FastpathConfig,
    Arc<crate::fastpath::FastpathLease>,
    Arc<Mutex<AuthorityDatabase>>,
) {
    let root = tempfile::tempdir().unwrap();
    let source = root.path().join("source");
    let local = root.path().join("local");
    std::fs::create_dir(&source).unwrap();
    std::fs::create_dir(&local).unwrap();
    let database = Arc::new(Mutex::new(AuthorityDatabase::initialize(&source).unwrap()));
    commit_value(&database, b"seed", b"seed-value");
    {
        let mut guard = database.lock().unwrap();
        let anchor = guard.capture_backup_anchor().unwrap();
        guard.deregister_backup_anchor(anchor.anchor_token);
    }
    drop(database);
    let config = crate::fastpath::FastpathConfig::new(&source, &local, Some(1000)).unwrap();
    let lease = config.prepare_for_test(None).unwrap();
    let database = Arc::new(Mutex::new(
        AuthorityDatabase::open(&config.local_authority).unwrap(),
    ));
    (root, config, lease, database)
}

#[test]
fn native_shadow_slow_io_allows_foreground_commits_and_retains_the_source_lease() {
    let (_root, config, lease, database) = native_fastpath();
    commit_value(&database, b"before-copy", b"before-copy-value");
    let (entered, reached) = std::sync::mpsc::sync_channel(1);
    let gate = Arc::new(GatedDestination {
        armed: AtomicBool::new(true),
        fail_writes: AtomicBool::new(false),
        entered,
        released: Mutex::new(false),
        signal: Condvar::new(),
    });
    let driver = OnlineBackupDriver::start_scheduled(
        &database,
        fastpath_context(config.shadow_directory.clone(), gate.clone(), lease),
    )
    .unwrap();
    let observer = driver.observer();
    let requested = std::thread::spawn(move || observer.run_and_wait(now_unix_ms() + 30_000));
    let reached_result = reached.recv_timeout(Duration::from_secs(10));
    if reached_result.is_err() {
        gate.release();
    }
    assert!(reached_result.is_ok());
    // The driver owns the only remaining source-lease reference while its
    // destination I/O is blocked outside authority.
    assert!(crate::engine::acquire_lease(&config.durable_authority, &crate::vfs::RealVfs).is_err());
    let writer_database = database.clone();
    let (finished, completion) = std::sync::mpsc::sync_channel(1);
    let writer = std::thread::spawn(move || {
        commit_value(&writer_database, b"during-copy", b"concurrent-value");
        finished.send(()).unwrap();
    });
    let completed = completion.recv_timeout(Duration::from_secs(10));
    gate.release();
    writer.join().unwrap();
    let first = requested.join().unwrap().unwrap();
    assert!(
        completed.is_ok(),
        "a blocked durable shadow must not hold the foreground writer"
    );
    let current = database
        .lock()
        .unwrap()
        .writer_engine()
        .unwrap()
        .state()
        .durable_sequence;
    assert!(
        first.durable_sequence < current,
        "the captured snapshot must exclude the concurrent suffix"
    );
    let second = driver
        .observer()
        .run_and_wait(now_unix_ms() + 30_000)
        .unwrap();
    assert!(second.durable_sequence >= current);
    driver.stop_and_join().unwrap();
    assert!(crate::engine::acquire_lease(&config.durable_authority, &crate::vfs::RealVfs).is_ok());
    let restored = _root.path().join("restored");
    backup::restore_latest_backup(&config.shadow_directory, &restored).unwrap();
    let mut restored = AuthorityDatabase::open(&restored).unwrap();
    let mut read = restored.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "record", b"during-copy").unwrap();
    assert_eq!(
        restored.entity_get(&mut read, &key).unwrap().unwrap(),
        b"concurrent-value"
    );
}

#[test]
fn native_shadow_idle_ticks_do_not_build_unchanged_generations() {
    let (_root, config, lease, database) = native_fastpath();
    let driver =
        OnlineBackupDriver::start_fastpath(&database, config.shadow_directory, None, lease)
            .unwrap();
    std::thread::sleep(Duration::from_millis(2250));
    let metrics = driver.observer().metrics();
    driver.stop_and_join().unwrap();
    assert_eq!(metrics.epochs_completed, 0);
    assert_eq!(metrics.epochs_failed, 0);
}

#[test]
fn native_shadow_failures_back_off_without_losing_foreground_data() {
    let (_root, config, lease, database) = native_fastpath();
    commit_value(&database, b"pending", b"still-local");
    let (entered, _receiver) = std::sync::mpsc::sync_channel(1);
    let destination = Arc::new(GatedDestination {
        armed: AtomicBool::new(false),
        fail_writes: AtomicBool::new(true),
        entered,
        released: Mutex::new(true),
        signal: Condvar::new(),
    });
    let driver = OnlineBackupDriver::start_scheduled(
        &database,
        fastpath_context(config.shadow_directory.clone(), destination, lease),
    )
    .unwrap();
    // The first epoch's anchor/copy latency follows the host volume; the
    // anti-spin window starts at its observed failure.
    let first_failure_deadline = std::time::Instant::now() + Duration::from_secs(30);
    while driver.observer().metrics().epochs_failed == 0 {
        assert!(
            std::time::Instant::now() < first_failure_deadline,
            "the failing replication epoch never ran"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    std::thread::sleep(Duration::from_millis(2250));
    commit_value(&database, b"after-failure", b"preserved-local");
    let metrics = driver.observer().metrics();
    driver.stop_and_join().unwrap();
    assert!(
        (1..=3).contains(&metrics.epochs_failed),
        "failed replication must not spin: {metrics:?}"
    );
    assert!(metrics.last_error.is_some());
    let mut guard = database.lock().unwrap();
    let mut read = guard.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "record", b"after-failure").unwrap();
    assert_eq!(
        guard.entity_get(&mut read, &key).unwrap().unwrap(),
        b"preserved-local"
    );
}

#[test]
fn native_shadow_doorbell_reuses_an_unchanged_restored_generation() {
    let (_root, config, lease, database) = native_fastpath();
    let driver = OnlineBackupDriver::start_fastpath(&database,config.shadow_directory,None,lease).unwrap();
    let report = driver.observer().run_and_wait(now_unix_ms()+30_000).unwrap();
    driver.stop_and_join().unwrap();
    assert_eq!(report.copied_blocks,0);
    assert_eq!(report.manifest_bytes,0);
    assert!(report.ok);
}


#[test]
fn epoch_resumes_interrupted_prune_before_copying_latest_state() {
    use crate::vfs::{FaultAction, FaultRule};
    fn seed() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>, EpochContext) {
        let (vfs, database) = authority();
        let mut context = context(&vfs); context.retention = 4;
        for value in [b"one", b"two", b"new"] {
            commit_value(&database, b"rolling", value);
            run(&database, &context);
        }
        (vfs, database, context)
    }
    let (baseline, _, _) = seed();
    baseline.arm_fault(None).unwrap();
    crate::backup::prune_backup_with_vfs(Path::new(STORE), 1, baseline.clone()).unwrap();
    let operations = baseline.trace().unwrap().len();
    let mut exercised = false;
    for operation_number in 1..=operations as u64 {
        let (vfs, database, mut context) = seed();
        vfs.arm_fault(Some(FaultRule { operation_number,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted) })).unwrap();
        let failed = crate::backup::prune_backup_with_vfs(Path::new(STORE), 1, vfs.clone());
        vfs.arm_fault(None).unwrap();
        if failed.is_err() && crate::backup::ensure_no_prune(Path::new(STORE), vfs.as_ref())
            .is_err_and(|error| error.kind() == io::ErrorKind::WouldBlock) {
            commit_value(&database, b"rolling", b"latest");
            context.retention = 2; // the pending plan still owns its old retention set
            let report = run(&database, &context);
            assert!(report.ok);
            crate::backup::ensure_no_prune(Path::new(STORE), vfs.as_ref()).unwrap();
            restore_store(&vfs, "/restore");
            let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs).unwrap();
            let mut tx = restored.begin(7, 11).unwrap();
            assert_eq!(restored.entity_get(&mut tx, &EntityKey::new(7, 11,
                "record", b"rolling").unwrap()).unwrap().unwrap(), b"latest");
            exercised = true;
            break;
        }
    }
    assert!(exercised, "fault injection must leave a committed pending prune plan");
}

/// A closed store whose shards refuse new names, as DolphinFS directories past
/// their entry cap do, overflows into `blocks-overflow`: the epoch publishes,
/// and a restore whose target refuses too reads every commit
/// (docs/STORAGE_OPERATIONS.md, "Loose shards past a per-directory entry cap").
#[test]
fn an_epoch_into_a_capped_closed_store_publishes_and_restores_through_overflow() {
    let (vfs, database) = authority();
    for index in 0..24 {
        commit_value(&database, format!("capped-{index}").as_bytes(), format!("value-{index}").as_bytes());
    }
    database.lock().unwrap().checkpoint_for_test().unwrap();
    commit_value(&database, b"tail", b"value-tail");
    for shard in 0..=u8::MAX {
        let name = format!("{shard:02x}");
        vfs.cap_directory_entries(&Path::new(STORE).join("blocks").join(&name), 0).unwrap();
        vfs.cap_directory_entries(&Path::new("/restored/blocks").join(&name), 0).unwrap();
    }
    let report = run(&database, &context(&vfs));
    assert!(report.ok && report.copied_blocks > 0);
    assert_eq!(vfs.metadata(&Path::new(STORE).join("blocks-overflow")).unwrap(), crate::vfs::FileKind::Directory);
    vfs.create_dir(Path::new("/restored")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    crate::backup::restore_latest_backup_with_vfs(Path::new(STORE), Path::new("/restored"), vfs.clone()).unwrap();
    assert_eq!(vfs.metadata(Path::new("/restored/blocks-overflow")).unwrap(), crate::vfs::FileKind::Directory);
    // What the restore returned is durable, overflowed blocks included.
    vfs.crash().unwrap();
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restored"), vfs.clone()).unwrap();
    for index in 0..24 {
        assert_eq!(
            read_value(&restored, format!("capped-{index}").as_bytes()),
            Some(format!("value-{index}").into_bytes())
        );
    }
    assert_eq!(read_value(&restored, b"tail").as_deref(), Some(&b"value-tail"[..]));
}


/// A run interrupted between an overflow leaf's renames and its barrier leaves
/// names that are visible but not durable. The next run's destination policy
/// makes them durable before it trusts presence, as it does for shard
/// directories.
#[test]
fn destination_policy_makes_overflow_leaf_names_durable_before_trusting_presence() {
    let (vfs, database) = authority();
    commit_value(&database, b"seed", b"value-seed");
    for shard in 0..=u8::MAX {
        vfs.cap_directory_entries(&Path::new(STORE).join("blocks").join(format!("{shard:02x}")), 0).unwrap();
    }
    assert!(run(&database, &context(&vfs)).ok);
    let store = crate::block::BlockStore::open_with_vfs(Path::new(STORE), vfs.clone()).unwrap();
    let payload = b"visible but not yet durable".to_vec();
    let id = crate::block::BlockId::for_payload(&payload);
    store.ensure_copy_shard(id).unwrap();
    assert!(store.publish_copied_block_unbarriered(id, &payload).unwrap());
    drop(store);
    crate::backup::destination_policy(Path::new(STORE), vfs.as_ref()).unwrap();
    vfs.crash().unwrap();
    let reopened = crate::block::BlockStore::open_with_vfs(Path::new(STORE), vfs.clone()).unwrap();
    assert_eq!(reopened.get(id).unwrap(), payload);
}
