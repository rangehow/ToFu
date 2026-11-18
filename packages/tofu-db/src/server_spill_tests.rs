// A commit whose carried payloads pass the WAL record bound makes its pending
// blocks durable without the writer (docs/TOFUDB_PUBLICATION_LATENCY.md,
// "Spills leave the authority").

/// Block-store publication calls (new files, renames, directory syncs) the
/// engine issues while armed, split by whether the authority mutex was held.
#[derive(Default)]
struct SpillObserver {
    authority: std::sync::OnceLock<std::sync::Weak<Mutex<AuthorityDatabase>>>,
    armed: std::sync::atomic::AtomicBool,
    calls: Mutex<Vec<(bool, &'static str, std::path::PathBuf)>>,
}

impl SpillObserver {
    fn note(&self, kind: &'static str, path: &std::path::Path) {
        let block_store = path.components().any(|part| {
            let part = part.as_os_str().to_string_lossy();
            part == "blocks" || part == "blocks-overflow" || part == "payload-segments"
        });
        if !block_store || !self.armed.load(std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        // Concurrent shard workers probe one at a time, so one worker's probe
        // never reads as another's held writer.
        let mut calls = self.calls.lock().unwrap();
        let held = self.authority.get().and_then(std::sync::Weak::upgrade)
            .is_some_and(|authority| authority.try_lock().is_err());
        calls.push((held, kind, path.to_path_buf()));
    }

    fn arm(&self) {
        self.calls.lock().unwrap().clear();
        self.armed.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    /// (calls under the writer mutex, calls outside it)
    fn disarm(&self) -> (Vec<(&'static str, std::path::PathBuf)>, usize) {
        self.armed.store(false, std::sync::atomic::Ordering::SeqCst);
        let calls = std::mem::take(&mut *self.calls.lock().unwrap());
        if calls.iter().any(|(held, ..)| *held) {
            for (index, (held, kind, path)) in calls.iter().enumerate() {
                eprintln!("spill call #{index} held={held} {kind} {}", path.display());
            }
        }
        let free = calls.iter().filter(|(held, ..)| !held).count();
        (calls.into_iter().filter(|(held, ..)| *held).map(|(_, kind, path)| (kind, path)).collect(), free)
    }
}

struct SpillObserverVfs {
    observer: Arc<SpillObserver>,
}

impl crate::vfs::Vfs for SpillObserverVfs {
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
        if request.write {
            self.observer.note("open_write", path);
        }
        crate::vfs::RealVfs.open(path, request)
    }
    fn open_positioned_reader(&self, path: &std::path::Path) -> io::Result<Option<Arc<dyn crate::vfs::VfsPositionedReader>>> {
        crate::vfs::RealVfs.open_positioned_reader(path)
    }
    fn create_dir(&self, path: &std::path::Path) -> io::Result<()> {
        self.observer.note("create_dir", path);
        crate::vfs::RealVfs.create_dir(path)
    }
    fn rename(&self, source: &std::path::Path, destination: &std::path::Path) -> io::Result<()> {
        self.observer.note("rename", destination);
        crate::vfs::RealVfs.rename(source, destination)
    }
    fn remove_file(&self, path: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.remove_file(path)
    }
    fn metadata(&self, path: &std::path::Path) -> io::Result<crate::vfs::FileKind> {
        crate::vfs::RealVfs.metadata(path)
    }
    fn read_directory(&self, path: &std::path::Path) -> io::Result<Vec<std::path::PathBuf>> {
        crate::vfs::RealVfs.read_directory(path)
    }
    fn read_directory_entries(&self, path: &std::path::Path) -> io::Result<Vec<crate::vfs::DirectoryEntry>> {
        crate::vfs::RealVfs.read_directory_entries(path)
    }
    fn read_directory_bounded(&self, path: &std::path::Path, maximum: usize) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        crate::vfs::RealVfs.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(&self, path: &std::path::Path, prefix: &str, scanned: usize, matched: usize)
        -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, matched)
    }
    fn sync_directory(&self, path: &std::path::Path) -> io::Result<()> {
        self.observer.note("sync_directory", path);
        crate::vfs::RealVfs.sync_directory(path)
    }
}

/// Hex text of incompressible bytes: its compressed blob chunks still carry
/// more than `MAX_CARRIED_PAYLOAD_BYTES` (2 MiB), within the request bound.
fn incompressible_text(bytes: usize) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut text = String::with_capacity(bytes * 2);
    for byte in (0_u64..).flat_map(|block| Sha256::digest(block.to_le_bytes())).take(bytes) {
        text.push(HEX[usize::from(byte >> 4)] as char);
        text.push(HEX[usize::from(byte & 0x0f)] as char);
    }
    text
}

/// 2026-11-13 05:34: a small turn.event.record held the authority 33.4 s,
/// almost all of it untimed `engine_commit` self time beside ~13 ms of VFS
/// work: its carried set passed the WAL record bound and the spill published
/// loose blocks inside the hold, joining one publication worker per hash
/// shard on a stalled DolphinFS mount. Every writer queued behind it.
#[test]
fn a_commit_past_the_carried_bound_publishes_its_blocks_without_the_writer() {
    let value = json!({"body": incompressible_text(2_600_000)});
    for allow_parallel in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let observer = Arc::new(SpillObserver::default());
        let vfs = Arc::new(SpillObserverVfs { observer: Arc::clone(&observer) });
        let mut authority = AuthorityDatabase::initialize_with_vfs(directory.path(), vfs).unwrap();
        authority.set_write_overlay_for_test(true);
        let session = authenticator().authenticate(&hello()).unwrap();
        let database = Arc::new(Mutex::new(authority));
        observer.authority.set(Arc::downgrade(&database)).unwrap();
        let stores = StorageV2Stores::new(&database, None);
        let command = retention_command("record.put",
            json!({"namespace": "spill", "key": "large", "value": value}),
            &format!("spill-{allow_parallel}"));
        observer.arm();
        let RequestDispatch::Responded(response) = dispatch_one_request_inner_mode(
            &stores, &session, &command, false, 1_000, &mut || 1_000, &mut || true, allow_parallel).unwrap()
            else { panic!("dispatch disconnected") };
        let (held, free) = observer.disarm();
        eprintln!("spill parallel={allow_parallel} block_store_calls_under_writer={} off_lock={free}", held.len());
        assert_eq!(response.status, 0, "{:?}", response.error_message);
        assert!(held.is_empty(), "the commit published blocks under the writer (parallel={allow_parallel}): {:?}",
            &held[..held.len().min(8)]);
        assert!(free > 0, "the commit never made its blocks durable (parallel={allow_parallel})");
        {
            let authority = database.lock().unwrap();
            assert!(authority.block_store_shared().publication_window_block_ids().is_empty(),
                "the spill's publication window stayed open");
        }
        drop(stores);
        drop(Arc::try_unwrap(database).ok().expect("one authority owner").into_inner().unwrap());
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let stored = dispatched(&mut reopened, &session,
            &request("record.get", 11, json!({"namespace": "spill", "key": "large"})), 1_000);
        assert_eq!(stored["value"], value, "parallel={allow_parallel}");
    }
}

/// 2026-11-18 10:39: a turn.event.record past the single-frame payload bound
/// arrives as a reassembled streamed request; its commit defers its spill and
/// the replay re-admitted it as a single-frame request, so the payload bound
/// refused it as `invalid_request` and the conversation's event delivery
/// failed on every attempt.
#[test]
fn a_streamed_commit_that_defers_its_spill_replays_as_the_streamed_request_it_is() {
    let value = json!({"body": incompressible_text(crate::protocol::MAX_PAYLOAD_BYTES / 2 + 4_096)});
    for allow_parallel in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let mut authority = AuthorityDatabase::initialize(directory.path()).unwrap();
        authority.set_write_overlay_for_test(true);
        let session = authenticator().authenticate(&hello()).unwrap();
        let database = Mutex::new(authority);
        let stores = StorageV2Stores::new(&database, None);
        let command = retention_command("record.put",
            json!({"namespace": "spill", "key": "streamed", "value": value}),
            &format!("streamed-spill-{allow_parallel}"));
        assert!(command.payload.len() > crate::protocol::MAX_PAYLOAD_BYTES, "the request must need a streamed upload");
        let RequestDispatch::Responded(response) = dispatch_one_request_inner_mode(
            &stores, &session, &command, true, 1_000, &mut || 1_000, &mut || true, allow_parallel).unwrap()
            else { panic!("dispatch disconnected") };
        assert_eq!(response.status, 0, "parallel={allow_parallel}: {:?} {:?}",
            response.error_code, response.error_message);
        let stored = dispatched(&mut database.lock().unwrap(), &session,
            &request("record.get", 11, json!({"namespace": "spill", "key": "streamed"})), 1_000);
        assert_eq!(stored["value"], value, "parallel={allow_parallel}");
    }
}
