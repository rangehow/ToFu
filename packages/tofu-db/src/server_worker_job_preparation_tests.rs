// Worker-job claim preparation keeps cold queue/index reads outside the writer.
// Competing queue mutations must invalidate the prepared selection and use the
// ordinary exclusive fallback; source faults must retain the exclusive result.

#[derive(Clone, Copy)]
enum WorkerClaimReadGate {
    Open,
    Block,
    Fault,
}

struct WorkerClaimReadProbe {
    preparation_thread: std::sync::Mutex<Option<std::thread::ThreadId>>,
    gate: std::sync::RwLock<WorkerClaimReadGate>,
    armed: std::sync::atomic::AtomicBool,
    entered: std::sync::mpsc::Sender<()>,
    release: std::sync::Mutex<std::sync::mpsc::Receiver<()>>,
}

impl WorkerClaimReadProbe {
    fn gate(&self) -> std::sync::RwLockReadGuard<'_, WorkerClaimReadGate> {
        self.gate
            .read()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    fn read(&self) -> io::Result<()> {
        if *self.preparation_thread.lock().unwrap() != Some(std::thread::current().id()) {
            return Ok(());
        }
        let gate = *self.gate();
        match gate {
            WorkerClaimReadGate::Open => Ok(()),
            WorkerClaimReadGate::Fault => Err(io::Error::new(
                io::ErrorKind::Other,
                "worker claim cold read fault",
            )),
            WorkerClaimReadGate::Block => {
                if self.armed.swap(false, std::sync::atomic::Ordering::AcqRel) {
                    self.entered.send(()).unwrap();
                    self.release
                        .lock()
                        .unwrap()
                        .recv_timeout(Duration::from_secs(10))
                        .map_err(|_| io::Error::other("worker claim read release missing"))?;
                }
                Ok(())
            }
        }
    }
}

struct WorkerClaimReadVfs {
    probe: Arc<WorkerClaimReadProbe>,
}

struct WorkerClaimReadFile {
    inner: Box<dyn crate::vfs::VfsFile>,
    probe: Arc<WorkerClaimReadProbe>,
}

impl crate::vfs::VfsFile for WorkerClaimReadFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.probe.read()?;
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.probe.read()?;
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

impl crate::vfs::Vfs for WorkerClaimReadVfs {
    fn open(
        &self,
        path: &std::path::Path,
        request: crate::vfs::OpenRequest,
    ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
        let inner = crate::vfs::RealVfs.open(path, request)?;
        if request.write {
            Ok(inner)
        } else {
            Ok(Box::new(WorkerClaimReadFile {
                inner,
                probe: Arc::clone(&self.probe),
            }))
        }
    }
    fn open_positioned_reader(
        &self,
        path: &std::path::Path,
    ) -> io::Result<Option<Arc<dyn crate::vfs::VfsPositionedReader>>> {
        let _ = path;
        Ok(None)
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
        crate::vfs::RealVfs.metadata(path)
    }
    fn read_directory(&self, path: &std::path::Path) -> io::Result<Vec<std::path::PathBuf>> {
        crate::vfs::RealVfs.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &std::path::Path,
        maximum: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
        crate::vfs::RealVfs.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &std::path::Path,
        prefix: &str,
        scanned: usize,
        matched: usize,
    ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
        crate::vfs::RealVfs.match_directory_prefix_bounded(path, prefix, scanned, matched)
    }
    fn sync_directory(&self, path: &std::path::Path) -> io::Result<()> {
        crate::vfs::RealVfs.sync_directory(path)
    }
}

fn worker_claim_request(worker: &str, command_id: &str) -> Request {
    retention_command(
        "worker_job.claim_next",
        json!({
            "worker_id": worker,
            "task_kinds": ["cold-claim"],
            "now_ms": 1_000,
            "lease_ms": 60_000,
        }),
        command_id,
    )
}

fn worker_claim_job(task_id: &str) -> Request {
    retention_command(
        "worker_job.enqueue",
        json!({
            "task_id": task_id,
            "user_id": 11,
            "tenant_id": "",
            "task_kind": "cold-claim",
            "idempotency_key": task_id,
            "payload": {"task": task_id},
            "priority": 100,
            "available_at_ms": 0,
            "now_ms": 1_000,
        }),
        &format!("enqueue-{task_id}"),
    )
}

#[test]
fn worker_claim_cold_read_prepares_outside_writer_and_revalidates_queue_write() {
    let directory = tempfile::tempdir().unwrap();
    let (entered_sender, entered_receiver) = std::sync::mpsc::channel();
    let (release_sender, release_receiver) = std::sync::mpsc::channel::<()>();
    let probe = Arc::new(WorkerClaimReadProbe {
        preparation_thread: std::sync::Mutex::new(None),
        gate: std::sync::RwLock::new(WorkerClaimReadGate::Open),
        armed: std::sync::atomic::AtomicBool::new(true),
        entered: entered_sender,
        release: std::sync::Mutex::new(release_receiver),
    });
    let vfs = Arc::new(WorkerClaimReadVfs {
        probe: Arc::clone(&probe),
    });
    let mut seeded = AuthorityDatabase::initialize_with_vfs(directory.path(), vfs).unwrap();
    seeded.set_write_overlay_for_test(true);
    seeded.set_entity_tombstone_reclaim_override_for_test(Some(true));
    seeded.set_worker_lease_test_time(1_000);
    let session = authenticator().authenticate(&hello()).unwrap();
    dispatched(&mut seeded, &session, &worker_claim_job("first"), 1_000);
    dispatched(&mut seeded, &session, &worker_claim_job("second"), 1_000);
    seeded.merge_entity_overlay_fully().unwrap();
    seeded.checkpoint_wal_window().unwrap();
    republish_root([&mut seeded], &session, "cold-claim");
    seeded.configure_hot_data_caches(0, 0).unwrap();
    let database = Mutex::new(seeded);
    let stores = StorageV2Stores::new(&database, None);
    let prepared_command = worker_claim_request("prepared-worker", "prepared-claim");
    let concurrent_command = worker_claim_job("concurrent");
    let admitted = session.admit(&prepared_command, 1_000).unwrap();
    *probe.gate.write().unwrap() = WorkerClaimReadGate::Block;
    let (prepared, concurrent) = thread::scope(|scope| {
        let preparation = scope.spawn(|| {
            *probe.preparation_thread.lock().unwrap() = Some(std::thread::current().id());
            prepare_parallel_command(&stores, &session, admitted, &mut || 1_000, &mut || true)
        });
        entered_receiver
            .recv_timeout(Duration::from_secs(10))
            .expect("claim preparation must reach a cold source read");
        let concurrent = dispatched(
            &mut database
                .try_lock()
                .expect("cold claim read must not hold the writer"),
            &session,
            &concurrent_command,
            1_000,
        );
        *probe.preparation_thread.lock().unwrap() = None;
        *probe.gate.write().unwrap() = WorkerClaimReadGate::Open;
        release_sender.send(()).unwrap();
        (preparation.join().unwrap().unwrap(), concurrent)
    });
    assert_eq!(concurrent["created"], true);
    assert_eq!(concurrent["job"]["taskId"], "concurrent");
    let prepared = match prepared {
        ParallelPreparationOutcome::Prepared(prepared) => prepared,
        ParallelPreparationOutcome::Fallback => panic!("claim preparation fell back"),
        ParallelPreparationOutcome::Finished(RequestDispatch::Responded(response)) => panic!(
            "claim preparation failed: status={} code={:?} message={:?}",
            response.status, response.error_code, response.error_message,
        ),
        ParallelPreparationOutcome::Finished(RequestDispatch::ConnectionClosed) => {
            panic!("claim preparation disconnected")
        }
    };
    let finished = finish_parallel_command(
        &stores,
        &session,
        admitted,
        prepared,
        &mut || 1_000,
        &mut || true,
    )
    .unwrap();
    assert!(
        finished.is_none(),
        "a competing queue write must invalidate the prepared selection"
    );
    let fallback = dispatched(
        &mut database.lock().unwrap(),
        &session,
        &prepared_command,
        1_000,
    );
    assert_eq!(fallback["taskId"], "concurrent");
    assert_eq!(fallback["fencingToken"], 1);
    let stored = dispatched(
        &mut database.lock().unwrap(),
        &session,
        &request(
            "worker_job.get",
            11,
            json!({"task_id": "concurrent", "user_id": 11}),
        ),
        1_000,
    );
    assert_eq!(stored["claimOwner"], "prepared-worker");
    assert_eq!(stored["status"], "running");
    assert_eq!(stored["fencingToken"], 1);
    for (task_id, owner) in [("first", "first-worker"), ("second", "second-worker")] {
        let claimed = dispatched(
            &mut database.lock().unwrap(),
            &session,
            &worker_claim_request(owner, &format!("claim-{task_id}")),
            1_000,
        );
        assert_eq!(claimed["taskId"], task_id);
        assert_eq!(claimed["fencingToken"], 1);
    }
    assert!(dispatched(
        &mut database.lock().unwrap(),
        &session,
        &worker_claim_request("empty-queue", "empty-queue"),
        1_000,
    )
    .is_null());
    assert_eq!(
        database
            .lock()
            .unwrap()
            .snapshot_pin_metrics()
            .unwrap()
            .active_handles,
        0
    );
}

#[test]
fn worker_claim_uncontended_and_cold_read_fault_match_exclusive_execution() {
    for fault in [false, true] {
        let mut outcomes = Vec::new();
        for parallel in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let release_receiver = std::sync::mpsc::channel::<()>().1;
            let probe = Arc::new(WorkerClaimReadProbe {
                preparation_thread: std::sync::Mutex::new(None),
                gate: std::sync::RwLock::new(WorkerClaimReadGate::Open),
                armed: std::sync::atomic::AtomicBool::new(false),
                entered: std::sync::mpsc::channel().0,
                release: std::sync::Mutex::new(release_receiver),
            });
            let vfs = Arc::new(WorkerClaimReadVfs {
                probe: Arc::clone(&probe),
            });
            let mut database =
                AuthorityDatabase::initialize_with_vfs(directory.path(), vfs).unwrap();
            database.set_write_overlay_for_test(true);
            database.set_entity_tombstone_reclaim_override_for_test(Some(true));
            database.set_worker_lease_test_time(1_000);
            let session = authenticator().authenticate(&hello()).unwrap();
            dispatched(&mut database, &session, &worker_claim_job("only"), 1_000);
            database.merge_entity_overlay_fully().unwrap();
            database.checkpoint_wal_window().unwrap();
            republish_root([&mut database], &session, "cold-claim-fault");
            database.configure_hot_data_caches(0, 0).unwrap();
            let command = worker_claim_request("worker", "cold-claim-result");
            if fault {
                *probe.preparation_thread.lock().unwrap() = Some(std::thread::current().id());
                *probe.gate.write().unwrap() = WorkerClaimReadGate::Fault;
            }
            let response = if parallel {
                let database = Mutex::new(database);
                let stores = StorageV2Stores::new(&database, None);
                let admitted = session.admit(&command, 1_000).unwrap();
                let outcome =
                    try_parallel_command(&stores, &session, admitted, &mut || 1_000, &mut || true)
                        .unwrap()
                        .expect("claim preparation returns a response");
                let RequestDispatch::Responded(response) = outcome else {
                    panic!("claim dispatch disconnected")
                };
                let response = response.into_response();
                assert_eq!(
                    database
                        .lock()
                        .unwrap()
                        .snapshot_pin_metrics()
                        .unwrap()
                        .active_handles,
                    0
                );
                response
            } else {
                session.dispatch(&mut database, &command, 1_000)
            };
            if fault {
                assert_ne!(response.status, 0, "the armed cold read fault must fire");
            } else {
                assert_eq!(response.status, 0, "{:?}", response.error_message);
            }
            outcomes.push((
                response.status,
                response.error_code,
                response.error_message,
                response.payload,
            ));
        }
        assert_eq!(
            outcomes[0], outcomes[1],
            "parallel={:?} differs from exclusive",
            fault
        );
    }
}
