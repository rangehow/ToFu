// Commit validation and the exclusive lane read cold pages before the writer.
// Caches stay enabled; every page these specifications need is evicted first,
// so the off-lock pass alone can make the writer's hold free of VFS reads.

/// Every VFS read the engine issues while armed, split by whether the
/// authority mutex was held at that moment (single-threaded specifications).
#[derive(Default)]
struct HeldReadObserver {
    authority: std::sync::OnceLock<std::sync::Weak<Mutex<AuthorityDatabase>>>,
    armed: std::sync::atomic::AtomicBool,
    reads: Mutex<Vec<(bool, &'static str, std::path::PathBuf)>>,
}

impl HeldReadObserver {
    fn note(&self, kind: &'static str, path: &std::path::Path) {
        if !self.armed.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        let held = self.authority.get().and_then(std::sync::Weak::upgrade)
            .is_some_and(|authority| authority.try_lock().is_err());
        self.reads.lock().unwrap().push((held, kind, path.to_path_buf()));
    }

    fn arm(&self) {
        self.reads.lock().unwrap().clear();
        self.armed.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    /// (reads under the writer mutex, reads outside it)
    fn disarm(&self) -> (Vec<(&'static str, std::path::PathBuf)>, usize) {
        self.armed.store(false, std::sync::atomic::Ordering::SeqCst);
        let reads = std::mem::take(&mut *self.reads.lock().unwrap());
        let free = reads.iter().filter(|(held, ..)| !held).count();
        (reads.into_iter().filter(|(held, ..)| *held).map(|(_, kind, path)| (kind, path)).collect(), free)
    }
}

struct HeldReadVfs {
    observer: Arc<HeldReadObserver>,
}

struct HeldReadFile {
    inner: Box<dyn crate::vfs::VfsFile>,
    path: std::path::PathBuf,
    observer: Arc<HeldReadObserver>,
}

impl crate::vfs::VfsFile for HeldReadFile {
    fn len(&self) -> io::Result<u64> {
        self.observer.note("len", &self.path);
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.observer.note("read", &self.path);
        self.inner.read_all(maximum)
    }
    fn read_all_with_observed_length(&mut self, maximum: usize, observed: u64) -> io::Result<Vec<u8>> {
        self.observer.note("read", &self.path);
        self.inner.read_all_with_observed_length(maximum, observed)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.observer.note("read", &self.path);
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.inner.set_len(length)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}

struct HeldReadPositioned {
    inner: Arc<dyn crate::vfs::VfsPositionedReader>,
    path: std::path::PathBuf,
    observer: Arc<HeldReadObserver>,
}

impl crate::vfs::VfsPositionedReader for HeldReadPositioned {
    fn len(&self) -> io::Result<u64> {
        self.observer.note("len", &self.path);
        self.inner.len()
    }
    fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.observer.note("read", &self.path);
        self.inner.read_exact_at(offset, bytes)
    }
}

impl crate::vfs::Vfs for HeldReadVfs {
    fn supports_buffered_wal_writes(&self) -> bool {
        crate::vfs::RealVfs.supports_buffered_wal_writes()
    }
    fn supports_concurrent_file_publication(&self) -> bool {
        crate::vfs::RealVfs.supports_concurrent_file_publication()
    }
    fn filesystem_capacity(&self, path: &std::path::Path) -> io::Result<crate::vfs::FilesystemCapacity> {
        crate::vfs::RealVfs.filesystem_capacity(path)
    }
    fn open(&self, path: &std::path::Path, request: crate::vfs::OpenRequest) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        // Writable handles (WAL, CONTROL, new blocks) are journal and
        // publication I/O, not the page reads these specifications bound.
        if request.write {
            return crate::vfs::RealVfs.open(path, request);
        }
        self.observer.note("open", path);
        Ok(Box::new(HeldReadFile {
            inner: crate::vfs::RealVfs.open(path, request)?,
            path: path.to_path_buf(),
            observer: Arc::clone(&self.observer),
        }))
    }
    fn open_positioned_reader(&self, path: &std::path::Path) -> io::Result<Option<Arc<dyn crate::vfs::VfsPositionedReader>>> {
        self.observer.note("open", path);
        Ok(crate::vfs::RealVfs.open_positioned_reader(path)?.map(|inner| Arc::new(HeldReadPositioned {
            inner,
            path: path.to_path_buf(),
            observer: Arc::clone(&self.observer),
        }) as Arc<dyn crate::vfs::VfsPositionedReader>))
    }
    fn create_dir(&self, path: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.create_dir(path)
    }
    fn rename(&self, source: &std::path::Path, destination: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.rename(source, destination)
    }
    fn remove_file(&self, path: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.remove_file(path)
    }
    fn metadata(&self, path: &std::path::Path) -> io::Result<crate::vfs::FileKind> {
        self.observer.note("metadata", path);
        crate::vfs::RealVfs.metadata(path)
    }
    fn read_directory(&self, path: &std::path::Path) -> io::Result<Vec<std::path::PathBuf>> {
        self.observer.note("directory", path);
        crate::vfs::RealVfs.read_directory(path)
    }
    fn read_directory_entries(&self, path: &std::path::Path) -> io::Result<Vec<crate::vfs::DirectoryEntry>> {
        self.observer.note("directory", path);
        crate::vfs::RealVfs.read_directory_entries(path)
    }
    fn read_directory_bounded(&self, path: &std::path::Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        self.observer.note("directory", path);
        crate::vfs::RealVfs.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &std::path::Path, prefix: &str, scanned: usize, matched: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        self.observer.note("directory", path);
        crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, matched)
    }
    fn sync_directory(&self, path: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.sync_directory(path)
    }
}

/// Enough rows that the root reaches each row through internal pages, every
/// one a separate block that must be read when cold.
const COLD_ROWS: usize = 1_500;

fn cold_row(index: usize) -> String {
    format!("row-{index:05}")
}

struct ColdAuthority {
    _directory: tempfile::TempDir,
    observer: Arc<HeldReadObserver>,
    database: Arc<Mutex<AuthorityDatabase>>,
    view: crate::authority::PublishedQueryView,
    session: StorageV2Session,
}

impl ColdAuthority {
    fn stores(&self, published: bool) -> StorageV2Stores<'_> {
        let stores = StorageV2Stores::new(&self.database, None);
        if published { stores.with_published_queries(self.view.clone()) } else { stores }
    }

    fn record_value(&self, key: &str) -> Value {
        dispatched(&mut self.database.lock().unwrap(), &self.session,
            &request("record.get", 11, json!({"namespace": "cold", "key": key})), 1_000)["value"].clone()
    }
}

/// Production-shaped authority (write overlay, tombstone reclaim, published
/// queries) whose seeded rows are merged and checkpointed out of memory.
fn cold_authority(label: &str, seed: impl FnOnce(&mut AuthorityDatabase, &StorageV2Session)) -> ColdAuthority {
    let directory = tempfile::tempdir().unwrap();
    let observer = Arc::new(HeldReadObserver::default());
    let vfs = Arc::new(HeldReadVfs { observer: Arc::clone(&observer) });
    let mut authority = AuthorityDatabase::initialize_with_vfs(directory.path(), vfs).unwrap();
    authority.set_write_overlay_for_test(true);
    authority.set_entity_tombstone_reclaim_override_for_test(Some(true));
    authority.set_worker_lease_test_time(1_000);
    let session = authenticator().authenticate(&hello()).unwrap();
    let rows: Vec<usize> = (0..COLD_ROWS).collect();
    for chunk in rows.chunks(100) {
        let mut transaction = authority.begin(7, 11).unwrap();
        for &index in chunk {
            crate::versioned_document::put(&authority, &mut transaction, crate::versioned_document::PutRequest {
                key: crate::versioned_document::record_key(7, 11, "cold", &cold_row(index)).unwrap(),
                namespace: "cold".into(), logical_key: cold_row(index),
                value_json: serde_json::to_vec(&json!({"row": index, "pad": "x".repeat(96)})).unwrap(),
                expected_version: None, updated_at_ms: 1_000,
            }).unwrap();
        }
        authority.commit(transaction).unwrap();
    }
    seed(&mut authority, &session);
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    republish_root([&mut authority], &session, label);
    let view = authority.enable_published_queries().unwrap();
    let database = Arc::new(Mutex::new(authority));
    observer.authority.set(Arc::downgrade(&database)).unwrap();
    ColdAuthority { _directory: directory, observer, database, view, session }
}

fn two_queued_jobs(authority: &mut AuthorityDatabase, session: &StorageV2Session) {
    for job in ["first", "second"] {
        dispatched(authority, session, &worker_claim_job(job), 1_000);
    }
}

/// Moves every committed page out of memory: merge, checkpoint, then empty
/// the shared caches and give them their capacity back (enabled but cold).
fn evict_to_cold(authority: &mut AuthorityDatabase) {
    authority.merge_entity_overlay_fully().unwrap();
    authority.checkpoint_wal_window().unwrap();
    authority.configure_hot_data_caches(0, 0).unwrap();
    authority.configure_hot_data_caches(64 << 20, 64 << 20).unwrap();
}

/// Dispatches `command`; without `allow_parallel` it goes straight to the
/// exclusive lane, as the embedded serialized fallback does.
fn dispatch_value(cold: &ColdAuthority, stores: &StorageV2Stores<'_>, command: &Request, allow_parallel: bool) -> Value {
    let RequestDispatch::Responded(response) = dispatch_one_request_inner_mode(
        stores, &cold.session, command, false, 1_000, &mut || 1_000, &mut || true, allow_parallel).unwrap()
        else { panic!("dispatch disconnected") };
    assert_eq!(response.status, 0, "{} {:?}", command.operation, response.error_message);
    serde_json::from_slice(&response.payload).unwrap()
}

fn report(scenario: &str, published: bool, held: &[(&'static str, std::path::PathBuf)], free: usize) {
    eprintln!("cold_io scenario={scenario} published={published} vfs_calls_under_writer={} vfs_calls_off_lock={free}",
        held.len());
}

#[test]
fn prepared_commit_validation_reads_cold_pages_before_claiming_the_writer() {
    for published in [false, true] {
        let cold = cold_authority(&format!("validation-{published}"), |_, _| {});
        let stores = cold.stores(published);
        let command = retention_command("record.put", json!({
            "namespace": "cold", "key": cold_row(7), "value": {"prepared": true},
        }), &format!("prepared-{published}"));
        let admitted = cold.session.admit(&command, 1_000).unwrap();
        let ParallelPreparationOutcome::Prepared(prepared) = prepare_parallel_command(
            &stores, &cold.session, admitted, &mut || 1_000, &mut || true).unwrap()
            else { panic!("record.put must prepare outside the writer") };
        {
            // An unrelated commit moves the head; its merge also replaces the
            // physical root, so validation walks pages preparation never read.
            let mut writer = cold.database.lock().unwrap();
            dispatched(&mut writer, &cold.session, &retention_command("record.put", json!({
                "namespace": "cold", "key": cold_row(COLD_ROWS - 3), "value": {"unrelated": true},
            }), &format!("unrelated-{published}")), 1_000);
            evict_to_cold(&mut writer);
        }
        cold.observer.arm();
        let finished = finish_parallel_command(&stores, &cold.session, admitted, prepared,
            &mut || 1_000, &mut || true).unwrap();
        let (held, free) = cold.observer.disarm();
        report("commit_validation", published, &held, free);
        let Some(RequestDispatch::Responded(response)) = finished
            else { panic!("an unrelated commit refused the preparation (published={published})") };
        assert_eq!(response.status, 0, "{:?}", response.error_message);
        if published {
            assert!(held.is_empty(), "commit validation read cold pages under the writer: {held:?}");
            assert!(free > 0, "the pages validation needed were not cold before the call");
        } else {
            assert!(!held.is_empty(), "control: without the off-lock pass validation reads cold pages in the hold");
        }
        assert_eq!(cold.record_value(&cold_row(7)), json!({"prepared": true}));
        assert_eq!(cold.database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
    }
}

#[test]
fn a_refusal_the_published_head_already_proves_never_queues_for_the_writer() {
    for published in [false, true] {
        let cold = cold_authority(&format!("refusal-{published}"), two_queued_jobs);
        let stores = cold.stores(published);
        let command = worker_claim_request("late-worker", &format!("late-claim-{published}"));
        let admitted = cold.session.admit(&command, 1_000).unwrap();
        let ParallelPreparationOutcome::Prepared(prepared) = prepare_parallel_command(
            &stores, &cold.session, admitted, &mut || 1_000, &mut || true).unwrap()
            else { panic!("worker_job.claim_next must prepare outside the writer") };
        {
            // A competing worker claims the queue head the preparation chose.
            let mut writer = cold.database.lock().unwrap();
            let early = dispatched(&mut writer, &cold.session,
                &worker_claim_request("early-worker", &format!("early-claim-{published}")), 1_000);
            assert_eq!(early["taskId"], "first");
            evict_to_cold(&mut writer);
        }
        let admissions = cold.database.lock().unwrap().foreground_admissions();
        let before = admissions.load(std::sync::atomic::Ordering::SeqCst);
        cold.observer.arm();
        let finished = finish_parallel_command(&stores, &cold.session, admitted, prepared,
            &mut || 1_000, &mut || true).unwrap();
        let (held, free) = cold.observer.disarm();
        report("refused_commit", published, &held, free);
        assert!(finished.is_none(), "the claimed queue head must refuse the prepared claim");
        let acquisitions = admissions.load(std::sync::atomic::Ordering::SeqCst) - before;
        if published {
            assert_eq!(acquisitions, 0, "a refusal the published head proves never takes the writer");
            assert!(held.is_empty(), "{held:?}");
            assert!(free > 0, "the refusal was not proven from cold pages");
        } else {
            assert_eq!(acquisitions, 1);
            assert!(!held.is_empty(), "control: the doomed commit validates cold pages under the writer");
        }
        // The FIFO exclusive lane still guarantees progress: it claims the next job.
        cold.observer.arm();
        let claimed = dispatch_value(&cold, &stores, &command, false);
        let (held, free) = cold.observer.disarm();
        report("exclusive_after_refusal", published, &held, free);
        assert_eq!(claimed["taskId"], "second");
        if published {
            assert!(held.is_empty(), "exclusive claim read cold pages under the writer: {held:?}");
        }
        assert_eq!(cold.database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
    }
}

#[test]
fn exclusive_execution_reads_cold_pages_before_claiming_the_writer() {
    for published in [false, true] {
        let cold = cold_authority(&format!("exclusive-{published}"), |authority, session| {
            two_queued_jobs(authority, session);
            dispatched(authority, session, &retention_command("conversation.create",
                json!({"conv_id": "cold-conversation", "user_id": 11}), "cold-conversation"), 1_000);
        });
        let stores = cold.stores(published);
        // A certified claim forced onto the exclusive lane (where a refused
        // commit lands) and an uncertified command, which always runs there.
        let claim = worker_claim_request("exclusive-worker", &format!("exclusive-claim-{published}"));
        let enqueue = retention_command("queue.enqueue", json!({
            "conv_id": "cold-conversation", "user_id": 11, "queue_id": "cold-item", "kind": "real",
            "priority": 100, "message": {"text": "cold"}, "config": {}, "created_at_ms": 1_000,
        }), &format!("exclusive-enqueue-{published}"));
        // Conversation creation also maintains the owner's recency index, which a
        // rehearsal replica defers to commit-owned effects.
        let create = retention_command("conversation.create",
            json!({"conv_id": "second-conversation", "user_id": 11}), &format!("exclusive-create-{published}"));
        for (scenario, command, allow_parallel) in [("exclusive_claim", &claim, false),
            ("uncertified_enqueue", &enqueue, true), ("exclusive_conversation_create", &create, false)] {
            evict_to_cold(&mut cold.database.lock().unwrap());
            cold.observer.arm();
            let result = dispatch_value(&cold, &stores, command, allow_parallel);
            let (held, free) = cold.observer.disarm();
            report(scenario, published, &held, free);
            if published {
                assert!(held.is_empty(), "{scenario} read cold pages under the writer: {held:?}");
                assert!(free > 0, "the pages {scenario} needed were not cold before the call");
            } else {
                assert!(!held.is_empty(), "control: without the off-lock pass {scenario} reads cold pages in the hold");
            }
            match scenario {
                "exclusive_claim" => assert_eq!(result["taskId"], "first"),
                "uncertified_enqueue" => assert_eq!(result["queueId"], "cold-item"),
                _ => assert!(result.is_object(), "{result}"),
            }
        }
        let stored = dispatched(&mut cold.database.lock().unwrap(), &cold.session,
            &request("worker_job.get", 11, json!({"task_id": "first", "user_id": 11})), 1_000);
        assert_eq!(stored["claimOwner"], "exclusive-worker");
        let queued = dispatched(&mut cold.database.lock().unwrap(), &cold.session,
            &request("queue.list", 11, json!({"conv_id": "cold-conversation", "user_id": 11})), 1_000);
        assert_eq!(queued.as_array().unwrap().len(), 1);
        assert_eq!(cold.database.lock().unwrap().snapshot_pin_metrics().unwrap().active_handles, 0);
    }
}
