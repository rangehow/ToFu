// Physical publication tests: real blocked fsync, exact successor reconciliation,
// deterministic crash faults and bounded same-process paired latency measurement.

fn prepared_rotation_publication(engine: &mut Engine) -> Box<PreparedRotationPublication> {
    let snapshot = generation_snapshot(engine);
    let outcome = encode_rotation_segments(&snapshot);
    match engine
        .prepare_rotation_publication(snapshot, outcome)
        .unwrap()
    {
        RotationPublication::Prepared(prepared) => prepared,
        RotationPublication::Complete(_) => panic!("sealed rotation must prepare publication"),
    }
}

#[test]
fn rotation_publication_preparation_has_no_io_and_preserves_newer_successor_state() {
    for normal in [false, true] {
        let (vfs, mut engine) = initialized_simulated_engine();
        engine.attach_rotation_driver();
        if normal {
            engine.set_wal_durability(WalDurability::Normal).unwrap();
        }
        engine
            .commit_transaction(b"old", &[b"old payload"])
            .unwrap();
        let snapshot = generation_snapshot(&mut engine);
        engine
            .commit_transaction(b"captured successor", &[b"captured payload"])
            .unwrap();
        let outcome = encode_rotation_segments(&snapshot);
        vfs.arm_fault(None).unwrap();
        let prepared = match engine
            .prepare_rotation_publication(snapshot, outcome)
            .unwrap()
        {
            RotationPublication::Prepared(prepared) => prepared,
            _ => panic!("expected publication"),
        };
        assert!(
            vfs.trace().unwrap().is_empty(),
            "publication preparation must be metadata-only"
        );
        engine
            .commit_transaction(b"concurrent successor", &[b"concurrent payload"])
            .unwrap();
        let newest = engine.state.clone();
        let published = publish_rotation_publication(&prepared);
        engine
            .finish_rotation_publication(prepared, published)
            .unwrap();
        assert_eq!(engine.state.durable_sequence, newest.durable_sequence);
        assert_eq!(engine.state.root_hash, newest.root_hash);
        assert_eq!(
            engine.state.authority_state_root,
            newest.authority_state_root
        );
        assert_eq!(engine.state.checkpoint_sequence, 1);
        assert!(engine.control_dirty);
        assert!(!engine.async_publication_in_flight);
        engine.wal.sync_pending().unwrap(); // Normal's last tail is explicitly covered before crash.
        std::mem::forget(engine);
        vfs.crash().unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state.durable_sequence, 3);
        for text in [
            b"old payload".as_slice(),
            b"captured payload",
            b"concurrent payload",
        ] {
            assert_eq!(
                recovered.read_block(BlockId::for_payload(text)).unwrap(),
                text
            );
        }
    }
}

#[test]
fn rotation_publication_every_io_fault_keeps_acknowledged_predecessor_and_successor() {
    let (seed, mut engine) = initialized_simulated_engine();
    engine
        .commit_transaction(b"prefix", &[b"old payload"])
        .unwrap();
    engine.flush_dirty_control(ControlStaging::Staged).unwrap();
    std::mem::forget(engine);
    seed.crash().unwrap();
    let perform = |engine: &mut Engine, vfs: &DeterministicVfs| {
        engine.attach_rotation_driver();
        let prepared = prepared_rotation_publication(engine);
        engine
            .commit_transaction(b"concurrent", &[b"new payload"])
            .unwrap();
        vfs.arm_fault(None).unwrap();
        prepared
    };
    let sample = Arc::new(seed.fork_durable().unwrap());
    let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
    let prepared = perform(&mut engine, &sample);
    let outcome = publish_rotation_publication(&prepared);
    let trace = sample.trace().unwrap();
    engine
        .finish_rotation_publication(prepared, outcome)
        .unwrap();
    let mut cases = 0;
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(3));
        }
        if matches!(
            operation,
            Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let prepared = perform(&mut engine, &vfs);
            let occurrence = trace[..=index]
                .iter()
                .filter(|candidate| *candidate == operation)
                .count();
            vfs.arm_operation_fault(*operation, occurrence, action)
                .unwrap();
            let outcome = publish_rotation_publication(&prepared);
            let failed = outcome.is_err();
            let result = engine.finish_rotation_publication(prepared, outcome);
            assert!(
                vfs.operation_fault_fired().unwrap(),
                "fault {index} {operation:?} must fire"
            );
            if failed {
                assert!(result.is_err());
                assert!(engine.is_restart_required());
            }
            std::mem::forget(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap_or_else(|error| panic!("fault {index} {operation:?}: {error}"));
            assert_eq!(recovered.state.durable_sequence, 2);
            assert_eq!(
                recovered
                    .read_block(BlockId::for_payload(b"old payload"))
                    .unwrap(),
                b"old payload"
            );
            assert_eq!(
                recovered
                    .read_block(BlockId::for_payload(b"new payload"))
                    .unwrap(),
                b"new payload"
            );
            cases += 1;
        }
    }
    assert!(cases > 10);
    eprintln!(
        "rotation publication fault matrix: {cases} cases / {} operations",
        trace.len()
    );
}

// A wrapper around the actual RealVfs CONTROL fd, not a fake storage call.
// The blocked phase is identical in both arms; the changed lock placement
// determines whether the foreground commit waits for it.
fn measure_rotation_publication_overlap(unlocked: bool, delay_ms: u64) -> (f64, f64) {
    let directory = match std::env::var_os("TOFUDB_ROTATION_PUBLICATION_BENCH_PARENT") {
        Some(parent) => tempfile::tempdir_in(parent).unwrap(),
        None => tempfile::tempdir().unwrap(),
    };
    let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
    let gate = Arc::new(GenerationIoGate {
        entered: entered_sender,
        armed: std::sync::atomic::AtomicBool::new(false),
        released: Mutex::new(false),
        wake: std::sync::Condvar::new(),
        maximum_pause: Some(std::time::Duration::from_secs(10)),
    });
    let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), true));
    let engine = Arc::new(Mutex::new(
        Engine::initialize_with_vfs(directory.path(), vfs.clone()).unwrap(),
    ));
    let snapshot = {
        let mut engine = engine.lock().unwrap();
        engine.attach_rotation_driver();
        engine
            .commit_transaction(b"prefix", &[b"durable prefix payload"])
            .unwrap();
        generation_snapshot(&mut engine)
    };
    let encoded = encode_rotation_segments(&snapshot);
    let publication_engine = Arc::clone(&engine);
    gate.armed.store(true, std::sync::atomic::Ordering::Release);
    let publisher = std::thread::spawn(move || {
        let start = std::time::Instant::now();
        if unlocked {
            let prepared = match publication_engine
                .lock()
                .unwrap()
                .prepare_rotation_publication(snapshot, encoded)
                .unwrap()
            {
                RotationPublication::Prepared(prepared) => prepared,
                _ => panic!("expected prepared publication"),
            };
            let outcome = publish_rotation_publication(&prepared);
            publication_engine
                .lock()
                .unwrap()
                .finish_rotation_publication(prepared, outcome)
                .unwrap();
        } else {
            publication_engine
                .lock()
                .unwrap()
                .finish_rotation_checkpoint(snapshot, encoded)
                .unwrap();
        }
        start.elapsed().as_secs_f64() * 1000.0
    });
    entered_receiver
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    let release_gate = Arc::clone(&gate);
    let release = std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(delay_ms));
        *release_gate.released.lock().unwrap() = true;
        release_gate.wake.notify_all();
    });
    let start = std::time::Instant::now();
    engine
        .lock()
        .unwrap()
        .commit_transaction(b"foreground ACK", &[b"durable successor payload"])
        .unwrap();
    let commit_ms = start.elapsed().as_secs_f64() * 1000.0;
    release.join().unwrap();
    let publication_ms = publisher.join().unwrap();
    // Exact real-file reopen is required in every measured sample.
    drop(engine);
    let recovered = Engine::open(directory.path()).unwrap();
    assert_eq!(recovered.state.durable_sequence, 2);
    assert_eq!(
        recovered
            .read_block(BlockId::for_payload(b"durable prefix payload"))
            .unwrap(),
        b"durable prefix payload"
    );
    assert_eq!(
        recovered
            .read_block(BlockId::for_payload(b"durable successor payload"))
            .unwrap(),
        b"durable successor payload"
    );
    (commit_ms, publication_ms)
}

#[test]
fn rotation_publication_real_control_fsync_does_not_block_foreground_ack() {
    let (commit_ms, publication_ms) = measure_rotation_publication_overlap(true, 500);
    assert!(
        commit_ms < publication_ms / 2.0,
        "foreground {commit_ms} ms waited for publication {publication_ms} ms"
    );
}

#[test]
fn history_publication_real_control_fsync_does_not_block_foreground_ack() {
    let directory = tempfile::tempdir().unwrap();
    let (entered_sender, entered_receiver) = std::sync::mpsc::sync_channel(1);
    let gate = Arc::new(GenerationIoGate {
        entered: entered_sender,
        armed: std::sync::atomic::AtomicBool::new(false),
        released: Mutex::new(false),
        wake: std::sync::Condvar::new(),
        maximum_pause: Some(std::time::Duration::from_secs(10)),
    });
    let vfs = Arc::new(GenerationPausedVfs(Arc::clone(&gate), true));
    let mut engine = Engine::initialize_with_vfs(directory.path(), vfs).unwrap();
    let root = engine.write_block(b"authority root").unwrap();
    engine
        .commit_references_with_authority_state(b"old", &[], Some(root))
        .unwrap();
    indexed_rotate(&mut engine);
    engine
        .commit_transaction(b"new", &[b"promoted history bytes"])
        .unwrap();
    indexed_rotate(&mut engine);
    let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
    gate.armed.store(true, std::sync::atomic::Ordering::Release);
    let publisher = std::thread::spawn(move || {
        let outcome = encode_history_compaction(&prepared);
        (prepared, outcome)
    });
    let entered = entered_receiver.recv_timeout(std::time::Duration::from_secs(5));
    let foreground =
        engine.commit_transaction(b"during history publish", &[b"live history-window bytes"]);
    *gate.released.lock().unwrap() = true;
    gate.wake.notify_all();
    let (prepared, outcome) = publisher.join().unwrap();
    entered.expect("history publication must pause in actual CONTROL sync");
    assert_eq!(foreground.unwrap().sequence, 3);
    engine.finish_history_compaction(prepared, outcome).unwrap();
    assert_eq!(engine.state.durable_sequence, 3);
    assert!(engine.control_dirty);
    drop(engine);
    let recovered = Engine::open(directory.path()).unwrap();
    assert_eq!(recovered.state.durable_sequence, 3);
    assert_eq!(
        recovered
            .read_block(BlockId::for_payload(b"live history-window bytes"))
            .unwrap(),
        b"live history-window bytes"
    );
}

#[test]
fn history_publication_every_io_fault_preserves_concurrent_acknowledgements() {
    let (seed, mut engine) = initialized_simulated_engine();
    let root = engine.write_block(b"authority root").unwrap();
    engine
        .commit_references_with_authority_state(b"old", &[], Some(root))
        .unwrap();
    // Retirement must rewrite a mixed routing page, not merely publish an
    // unchanged index after retiring a source with no carried payloads.
    engine
        .commit_transaction(b"retiring", &[b"retired history payload"])
        .unwrap();
    indexed_rotate(&mut engine);
    engine
        .commit_transaction(b"new", &[b"promoted history bytes"])
        .unwrap();
    indexed_rotate(&mut engine);
    std::mem::forget(engine);
    seed.crash().unwrap();
    let prepare = |engine: &mut Engine| {
        let mut prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
        // Identical promotion IDs make manifest hashes and shard-creation
        // operations identical in the trace and every fault replay.
        prepared.set_payload_segment_test_ids(
            (1..=16).map(|id| Uuid::from_u128(0x1000 + id)).collect(),
        );
        engine
            .commit_transaction(b"concurrent", &[b"concurrent history payload"])
            .unwrap();
        prepared
    };
    let sample = Arc::new(seed.fork_durable().unwrap());
    let mut engine = Engine::open_with_vfs(Path::new("/data"), sample.clone()).unwrap();
    let prepared = prepare(&mut engine);
    sample.arm_fault(None).unwrap();
    let outcome = encode_history_compaction(&prepared);
    let trace = sample.trace().unwrap();
    engine.finish_history_compaction(prepared, outcome).unwrap();
    let mut cases = 0;
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(3));
        }
        if matches!(
            operation,
            Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let vfs = Arc::new(seed.fork_durable().unwrap());
            let mut engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let prepared = prepare(&mut engine);
            let occurrence = trace[..=index]
                .iter()
                .filter(|candidate| *candidate == operation)
                .count();
            vfs.arm_operation_fault(*operation, occurrence, action)
                .unwrap();
            let outcome = encode_history_compaction(&prepared);
            let _ = engine.finish_history_compaction(prepared, outcome);
            assert!(
                vfs.operation_fault_fired().unwrap(),
                "history fault {index} {operation:?} must fire"
            );
            std::mem::forget(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap_or_else(|error| panic!("history fault {index} {operation:?}: {error}"));
            assert_eq!(recovered.state.durable_sequence, 4);
            assert_eq!(
                recovered
                    .read_block(BlockId::for_payload(b"retired history payload"))
                    .unwrap(),
                b"retired history payload"
            );
            assert_eq!(
                recovered
                    .read_block(BlockId::for_payload(b"promoted history bytes"))
                    .unwrap(),
                b"promoted history bytes"
            );
            assert_eq!(
                recovered
                    .read_block(BlockId::for_payload(b"concurrent history payload"))
                    .unwrap(),
                b"concurrent history payload"
            );
            cases += 1;
        }
    }
    assert!(cases > 10);
    eprintln!(
        "history publication fault matrix: {cases} cases / {} operations",
        trace.len()
    );
}

#[test]
#[ignore = "explicit paired slow-I/O latency measurement; writes only disposable authorities"]
fn rotation_publication_paired_latency_measurement() {
    let samples = std::env::var("TOFUDB_ROTATION_PUBLICATION_BENCH_SAMPLES")
        .ok()
        .map(|value| value.parse::<usize>().unwrap())
        .unwrap_or(20);
    assert!((4..=1000).contains(&samples));
    let delay_ms = std::env::var("TOFUDB_ROTATION_PUBLICATION_BENCH_DELAY_MS")
        .ok()
        .map(|value| value.parse::<u64>().unwrap())
        .unwrap_or(100);
    assert!((10..=2000).contains(&delay_ms));
    let mut rows = Vec::new();
    for pair in 0..samples {
        for unlocked in if pair % 2 == 0 {
            [false, true]
        } else {
            [true, false]
        } {
            let (commit_ms, publication_ms) =
                measure_rotation_publication_overlap(unlocked, delay_ms);
            rows.push(serde_json::json!({"pair": pair, "unlocked": unlocked,
                    "foreground_commit_ms": commit_ms, "publication_ms": publication_ms}));
        }
    }
    eprintln!(
        "ROTATION_PUBLICATION_MEASUREMENT={}",
        serde_json::json!({
            "source_sha256": env!("TOFUDB_SOURCE_SHA256"), "delay_ms": delay_ms,
            "samples_per_arm": samples, "all_exact_reopens": true, "rows": rows})
    );
}

#[test]
fn rotation_publication_refuses_a_failed_unwritten_successor_prefix() {
    let (vfs, mut engine) = initialized_simulated_engine();
    engine.attach_rotation_driver();
    engine
        .commit_transaction(b"confirmed predecessor", &[b"confirmed payload"])
        .unwrap();
    let snapshot = generation_snapshot(&mut engine);
    let encoded = encode_rotation_segments(&snapshot);
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"unconfirmed successor", &[b"queued payload"])
        .unwrap();
    let prepared = match engine
        .prepare_rotation_publication(snapshot, encoded)
        .unwrap()
    {
        RotationPublication::Prepared(prepared) => prepared,
        _ => panic!("sealed predecessor must prepare publication"),
    };
    vfs.arm_operation_fault(
        Operation::Write,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    )
    .unwrap();
    assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
    assert!(barrier.barrier_error().is_some());
    let outcome = publish_rotation_publication(&prepared);
    assert!(
        outcome.is_err(),
        "raw fsync of empty successor cannot publish its queued sequence"
    );
    assert!(engine
        .finish_rotation_publication(prepared, outcome)
        .is_err());
    assert!(engine.is_restart_required());
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 1);
    assert_eq!(
        reopened
            .read_block(BlockId::for_payload(b"confirmed payload"))
            .unwrap(),
        b"confirmed payload"
    );
}

#[test]
fn pack_publication_refuses_a_failed_unwritten_strict_prefix() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"queued command", &[b"queued packed payload"])
        .unwrap();
    build_and_complete_background_pack(&engine, &[b"queued packed payload"]);
    let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared,
        _ => panic!("completed segment must prepare adoption"),
    };
    vfs.arm_operation_fault(
        Operation::Write,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    )
    .unwrap();
    assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
    let outcome = publish_prepared_pack_adoption(&mut prepared);
    assert!(
        outcome.is_err(),
        "Strict handoff is not physical WAL coverage"
    );
    assert!(engine.finish_pack_adoption(prepared, outcome).is_err());
    assert!(engine.is_restart_required());
    std::mem::forget(engine);
    vfs.crash().unwrap();
    assert_eq!(
        Engine::open_with_vfs(Path::new("/data"), vfs)
            .unwrap()
            .state
            .durable_sequence,
        0
    );
}

#[test]
fn history_publication_refuses_a_failed_unwritten_active_prefix() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let root = engine.write_block(b"confirmed authority root").unwrap();
    engine
        .commit_references_with_authority_state(b"first", &[], Some(root))
        .unwrap();
    engine
        .commit_transaction(b"old", &[b"confirmed pruned payload"])
        .unwrap();
    indexed_rotate(&mut engine);
    engine
        .commit_transaction(b"new", &[b"confirmed retained payload"])
        .unwrap();
    indexed_rotate(&mut engine);
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"queued active", &[b"unconfirmed active payload"])
        .unwrap();
    let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
    vfs.arm_operation_fault(
        Operation::Write,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
    )
    .unwrap();
    assert!(barrier.sync_for_test(vfs.as_ref()).is_err());
    let outcome = encode_history_compaction(&prepared);
    assert!(
        outcome.is_err(),
        "history publication cannot cover empty physical active WAL"
    );
    assert!(engine.finish_history_compaction(prepared, outcome).is_err());
    assert!(engine.is_restart_required());
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 3);
    assert_eq!(
        reopened
            .read_block(BlockId::for_payload(b"confirmed retained payload"))
            .unwrap(),
        b"confirmed retained payload"
    );
}

/// 2026-11-13 02:06: a DolphinFS fsync stalled past the barrier deadline
/// while a pack adoption waited for its WAL cover, the publication
/// latched the authority, and the recycle cost a 625 s reopen. The cover
/// precedes every CONTROL write, so nothing had been published: the
/// adoption is abandoned like a stopped one, the authority keeps serving,
/// the unreferenced segment is reclaimed as an orphan, and the next
/// adoption publishes once the sync completes.
#[test]
fn a_pack_adoption_whose_wal_cover_misses_the_barrier_deadline_is_abandoned_without_latching() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged command", &[b"stalled packed payload"])
        .unwrap();
    // Written, so a Normal-tier command was acknowledged; its sync stalls.
    barrier.write_for_test(vfs.as_ref()).unwrap();
    let payload = BlockId::for_payload(b"stalled packed payload");
    build_and_complete_background_pack(&engine, &[b"stalled packed payload"]);
    let before = engine.state.clone();
    let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared,
        _ => panic!("completed segment must prepare adoption"),
    };
    let segment_id = prepared.segment_id;
    prepared
        .wal_cover
        .as_mut()
        .expect("unsynced WAL bytes need a cover")
        .expire_barrier_wait_for_test();
    vfs.arm_fault(None).unwrap();
    let outcome = publish_prepared_pack_adoption(&mut prepared);
    let error = outcome.as_ref().unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(
        vfs.trace().unwrap().is_empty(),
        "nothing may be written before the cover: {:?}",
        vfs.trace().unwrap()
    );
    assert!(engine.finish_pack_adoption(prepared, outcome).is_err());
    assert!(
        !engine.is_restart_required(),
        "a slow but live WAL sync latched the authority"
    );
    engine.require_usable_authority().unwrap();
    assert_eq!(engine.state.generation, before.generation);
    assert_eq!(
        engine.state.payload_manifest_block_id,
        before.payload_manifest_block_id
    );
    assert_eq!(
        engine.read_block(payload).unwrap(),
        b"stalled packed payload"
    );
    engine.commit_transaction(b"after the stall", &[]).unwrap();
    let candidate = engine
        .plan_payload_segment_orphan()
        .unwrap()
        .candidate
        .expect("the abandoned segment is an orphan");
    assert_eq!(candidate.segment_id, Some(segment_id));
    assert!(engine.remove_payload_segment_orphan(&candidate).unwrap() > 0);
    // Once the sync completes, the next adoption publishes.
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    build_and_complete_background_pack(&engine, &[b"stalled packed payload"]);
    let mut retried = match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared,
        _ => panic!("the repacked segment must prepare adoption"),
    };
    let outcome = publish_prepared_pack_adoption(&mut retried);
    engine.finish_pack_adoption(retried, outcome).unwrap();
    assert!(engine.state.generation > before.generation);
    assert_ne!(
        engine.state.payload_manifest_block_id,
        before.payload_manifest_block_id
    );
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 2);
    assert_eq!(
        reopened.read_block(payload).unwrap(),
        b"stalled packed payload"
    );
}

/// Only the deadline is abandonable: a failed sync of the cover itself
/// leaves the WAL's durability unknown, so it still latches.
#[test]
fn a_pack_adoption_whose_wal_cover_sync_fails_still_latches() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged command", &[b"covered packed payload"])
        .unwrap();
    barrier.write_for_test(vfs.as_ref()).unwrap();
    build_and_complete_background_pack(&engine, &[b"covered packed payload"]);
    let mut prepared = match engine.try_prepare_pack_adoption().unwrap() {
        PackAdoptionPreparation::Ready(prepared) => prepared,
        _ => panic!("completed segment must prepare adoption"),
    };
    assert!(prepared.wal_cover.is_some());
    // The worker covers the witness; the cover's own sync then fails.
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    let outcome = publish_prepared_pack_adoption(&mut prepared);
    assert!(vfs.operation_fault_fired().unwrap());
    assert!(outcome.is_err());
    assert!(engine.finish_pack_adoption(prepared, outcome).is_err());
    assert!(engine.is_restart_required());
}

/// Two retirable history sources and a buffered barrier whose sync of a
/// written (Normal-acknowledged) command stalls, so the next history
/// publication waits on a WAL cover.
fn history_engine_with_a_stalled_wal_sync(
) -> (Arc<DeterministicVfs>, Engine, crate::wal::WalBarrier) {
    let (vfs, mut engine) = initialized_simulated_engine();
    let root = engine.write_block(b"confirmed authority root").unwrap();
    engine
        .commit_references_with_authority_state(b"first", &[], Some(root))
        .unwrap();
    engine
        .commit_transaction(b"old", &[b"retired history payload"])
        .unwrap();
    indexed_rotate(&mut engine);
    engine
        .commit_transaction(b"new", &[b"retained history payload"])
        .unwrap();
    indexed_rotate(&mut engine);
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged command", &[b"stalled active payload"])
        .unwrap();
    barrier.write_for_test(vfs.as_ref()).unwrap();
    (vfs, engine, barrier)
}

/// History compaction waits on the same WAL cover as pack adoption, before
/// its first CONTROL write. A cover that misses the barrier deadline (a
/// slow but live fsync, 2026-11-13 02:06) published nothing: the slice is
/// abandoned without latching, its promoted segment is an orphan the
/// segment lane reclaims, and the next slice retires the source.
#[test]
fn a_history_compaction_whose_wal_cover_misses_the_barrier_deadline_is_abandoned_without_latching()
{
    let (vfs, mut engine, barrier) = history_engine_with_a_stalled_wal_sync();
    let before = engine.state.clone();
    let sources = engine.history_manifest.as_ref().unwrap().segments.len();
    let prepared = {
        let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
        engine.try_prepare_history_compaction(1).unwrap().unwrap()
    };
    let outcome = encode_history_compaction(&prepared);
    let error = outcome
        .as_ref()
        .err()
        .expect("the cover missed its deadline");
    assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(engine.finish_history_compaction(prepared, outcome).is_err());
    assert!(
        !engine.is_restart_required(),
        "a slow but live WAL sync latched the authority"
    );
    engine.require_usable_authority().unwrap();
    assert_eq!(
        engine.state.generation, before.generation,
        "nothing was published"
    );
    assert_eq!(
        engine.state.history_manifest_block_id,
        before.history_manifest_block_id
    );
    assert_eq!(
        engine.state.payload_manifest_block_id,
        before.payload_manifest_block_id
    );
    assert_eq!(
        engine.history_manifest.as_ref().unwrap().segments.len(),
        sources
    );
    assert_eq!(
        engine
            .read_block(BlockId::for_payload(b"retired history payload"))
            .unwrap(),
        b"retired history payload"
    );
    engine.commit_transaction(b"after the stall", &[]).unwrap();
    let candidate = engine
        .plan_payload_segment_orphan()
        .unwrap()
        .candidate
        .expect("the abandoned promotion is an orphan");
    assert!(engine.remove_payload_segment_orphan(&candidate).unwrap() > 0);
    // Once the sync completes, the next slice retires the source.
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    let retried = engine.try_prepare_history_compaction(1).unwrap().unwrap();
    let outcome = encode_history_compaction(&retried);
    let metrics = engine.finish_history_compaction(retried, outcome).unwrap();
    assert_eq!(metrics.retired_segments as usize, sources - 1);
    assert!(engine.state.generation > before.generation);
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 5);
    for payload in [
        b"retired history payload".as_slice(),
        b"retained history payload",
        b"stalled active payload",
    ] {
        assert_eq!(
            reopened.read_block(BlockId::for_payload(payload)).unwrap(),
            payload
        );
    }
}

/// Only the deadline is abandonable: a failed sync of the cover itself
/// leaves the WAL's durability unknown, so history compaction still latches.
#[test]
fn a_history_compaction_whose_wal_cover_sync_fails_still_latches() {
    let (vfs, mut engine, barrier) = history_engine_with_a_stalled_wal_sync();
    let prepared = engine.try_prepare_history_compaction(1).unwrap().unwrap();
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    vfs.arm_operation_fault(
        Operation::SyncData,
        1,
        FaultAction::ErrorBefore(io::ErrorKind::Other),
    )
    .unwrap();
    let outcome = encode_history_compaction(&prepared);
    assert!(vfs.operation_fault_fired().unwrap());
    assert!(outcome.is_err());
    assert!(engine.finish_history_compaction(prepared, outcome).is_err());
    assert!(engine.is_restart_required());
}

/// A rotation publication waits on the successor WAL's cover before its
/// first CONTROL write. A cover that misses the barrier deadline published
/// nothing: the predecessor stays sealed and named by CONTROL, the encoded
/// pair is parked like a publication-lane deferral, and the retry
/// publishes it without re-encoding once the sync completes.
#[test]
fn a_rotation_publication_whose_wal_cover_misses_the_barrier_deadline_is_retried_without_latching()
{
    let (vfs, mut engine) = initialized_simulated_engine();
    engine.attach_rotation_driver();
    engine
        .commit_transaction(b"sealed predecessor", &[b"predecessor payload"])
        .unwrap();
    let snapshot = generation_snapshot(&mut engine);
    let encoded = encode_rotation_segments(&snapshot);
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged successor", &[b"successor payload"])
        .unwrap();
    barrier.write_for_test(vfs.as_ref()).unwrap();
    let before = engine.state.clone();
    let prepared = {
        let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
        match engine
            .prepare_rotation_publication(snapshot, encoded)
            .unwrap()
        {
            RotationPublication::Prepared(prepared) => prepared,
            _ => panic!("sealed predecessor must prepare publication"),
        }
    };
    vfs.arm_fault(None).unwrap();
    let outcome = publish_rotation_publication(&prepared);
    let error = outcome.as_ref().unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(
        vfs.trace().unwrap().is_empty(),
        "nothing may be written before the cover: {:?}",
        vfs.trace().unwrap()
    );
    assert!(engine
        .finish_rotation_publication(prepared, outcome)
        .is_err());
    assert!(
        !engine.is_restart_required(),
        "a slow but live WAL sync latched the authority"
    );
    engine.require_usable_authority().unwrap();
    assert_eq!(
        engine.state.generation, before.generation,
        "nothing was published"
    );
    assert_eq!(
        engine.state.sealed_log, before.sealed_log,
        "the predecessor stays sealed for the retry"
    );
    assert!(!engine.async_publication_in_flight);
    engine
        .commit_transaction(b"during the stall", &[b"window payload"])
        .unwrap();
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    let Some(RotationWork::Deferred(snapshot, encoded)) = engine.take_prepared_rotation().unwrap()
    else {
        panic!("the abandoned rotation is parked for its retry");
    };
    let prepared = match engine
        .prepare_rotation_publication(snapshot, Ok(encoded))
        .unwrap()
    {
        RotationPublication::Prepared(prepared) => prepared,
        _ => panic!("the parked pair must prepare publication"),
    };
    let outcome = publish_rotation_publication(&prepared);
    engine
        .finish_rotation_publication(prepared, outcome)
        .unwrap();
    assert!(engine.state.sealed_log.is_none());
    assert_eq!(engine.state.checkpoint_sequence, 1);
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 3);
    for payload in [
        b"predecessor payload".as_slice(),
        b"successor payload",
        b"window payload",
    ] {
        assert_eq!(
            reopened.read_block(BlockId::for_payload(payload)).unwrap(),
            payload
        );
    }
}

/// Online-GC holds first make both CONTROL slots name the current physical
/// roots (rule S8). That synchronous publication waits for the WAL barrier
/// under the authority before its CONTROL write and before it changes any
/// engine state, so a missed deadline published nothing: the stabilization
/// is abandoned without latching and a later hold stabilizes again.
#[test]
fn a_control_slot_stabilization_whose_wal_barrier_misses_the_deadline_is_abandoned_without_latching(
) {
    let (vfs, mut engine) = initialized_simulated_engine();
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged command", &[b"stabilized payload"])
        .unwrap();
    barrier.write_for_test(vfs.as_ref()).unwrap();
    let before = engine.state.clone();
    vfs.arm_fault(None).unwrap();
    let error = {
        let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
        engine.stabilize_control_slots().unwrap_err()
    };
    assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(abandoned_uncovered(&error), "{error}");
    assert!(
        vfs.trace().unwrap().is_empty(),
        "nothing may be written before the barrier: {:?}",
        vfs.trace().unwrap()
    );
    assert!(
        !engine.is_restart_required(),
        "a slow but live WAL sync latched the authority"
    );
    engine.require_usable_authority().unwrap();
    assert_eq!(
        engine.state.generation, before.generation,
        "nothing was published"
    );
    engine.commit_transaction(b"after the stall", &[]).unwrap();
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    engine.stabilize_control_slots().unwrap();
    assert!(engine.state.generation > before.generation);
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 2);
    assert_eq!(
        reopened
            .read_block(BlockId::for_payload(b"stabilized payload"))
            .unwrap(),
        b"stabilized payload"
    );
}

/// The rotation driver's prepare seals the active WAL under the authority:
/// `seal_wal_for_checkpoint` flushes a dirty CONTROL, pre-creates the
/// successor WAL and publishes the handover, and every CONTROL publication
/// first waits for the WAL barrier. That wait comes before each CONTROL
/// write and before any engine state change, so a missed deadline (a slow
/// but live sync: CRASH-SAFETY-1's 35 s stall latched R3 here, 2026-11-13)
/// published nothing. The preparation is abandoned without latching, a
/// successor file it may have created is reused by the retry, and the next
/// preparation seals once the sync completes.
#[test]
fn a_rotation_handover_whose_wal_barrier_misses_the_deadline_is_abandoned_without_latching() {
    let (vfs, mut engine) = initialized_simulated_engine();
    engine.attach_rotation_driver();
    let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
    engine.attach_wal_barrier(&barrier).unwrap();
    engine
        .commit_transaction(b"acknowledged before the stall", &[b"handover payload"])
        .unwrap();
    barrier.write_for_test(vfs.as_ref()).unwrap();
    let before = engine.state.clone();
    let error = {
        let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
        match engine.try_prepare_rotation_checkpoint() {
            Err(error) => error,
            Ok(_) => panic!("the handover must wait for the stalled WAL barrier"),
        }
    };
    assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(abandoned_uncovered(&error), "{error}");
    assert!(
        !engine.is_restart_required(),
        "a slow but live WAL sync latched the authority"
    );
    engine.require_usable_authority().unwrap();
    assert_eq!(
        engine.state.generation, before.generation,
        "nothing was published"
    );
    assert_eq!(
        engine.state.active_log_generation,
        before.active_log_generation
    );
    assert!(
        engine.state.sealed_log.is_none(),
        "the active WAL was not sealed"
    );
    engine
        .commit_transaction(b"during the stall", &[b"stall payload"])
        .unwrap();
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    match engine.try_prepare_rotation_checkpoint().unwrap() {
        RotationCheckpointPreparation::Ready(_) => {}
        _ => panic!("once the sync completes the next preparation seals"),
    }
    assert!(
        engine.state.sealed_log.is_some(),
        "the retry sealed the active WAL"
    );
    assert!(engine.state.generation > before.generation);
    barrier.sync_for_test(vfs.as_ref()).unwrap();
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, 2);
    for payload in [b"handover payload".as_slice(), b"stall payload"] {
        assert_eq!(
            reopened.read_block(BlockId::for_payload(payload)).unwrap(),
            payload
        );
    }
}

/// The publication rule itself: a synchronous CONTROL publication waits for
/// the WAL barrier before its write. A caller that changed no engine state
/// first (`ControlStaging::Unstaged`) abandons a missed deadline without
/// latching; a caller that staged state first (`ControlStaging::Staged`)
/// latches, as every other barrier failure does.
#[test]
fn only_an_unstaged_control_publication_abandons_a_missed_barrier_deadline() {
    for staging in [
        ControlStaging::Unstaged("test publication"),
        ControlStaging::Staged,
    ] {
        let (vfs, mut engine) = initialized_simulated_engine();
        let barrier = crate::wal::WalBarrierShared::buffered_for_test(1024 * 1024);
        engine.attach_wal_barrier(&barrier).unwrap();
        engine
            .commit_transaction(b"acknowledged command", &[b"published payload"])
            .unwrap();
        barrier.write_for_test(vfs.as_ref()).unwrap();
        let before = engine.state.clone();
        let next = ControlState {
            generation: before.generation + 1,
            ..before.clone()
        };
        vfs.arm_fault(None).unwrap();
        let error = {
            let _expired = crate::wal::ExpiredPublicationCovers::on_this_thread();
            engine.publish_control(&next, staging).unwrap_err()
        };
        assert_eq!(error.kind(), io::ErrorKind::TimedOut, "{error}");
        assert!(
            vfs.trace().unwrap().is_empty(),
            "nothing may be written before the barrier: {:?}",
            vfs.trace().unwrap()
        );
        assert_eq!(engine.state.generation, before.generation);
        match staging {
            ControlStaging::Unstaged(_) => {
                assert!(abandoned_uncovered(&error), "{error}");
                assert!(
                    !engine.is_restart_required(),
                    "an unstaged publication latched"
                );
                engine.require_usable_authority().unwrap();
            }
            ControlStaging::Staged => {
                assert!(!abandoned_uncovered(&error), "{error}");
                assert!(
                    engine.is_restart_required(),
                    "a staged publication must latch"
                );
            }
        }
        std::mem::forget(engine);
    }
}

/// Inside a `SpillDeferralScope`, a foreground commit whose carried
/// payloads pass the WAL record bound writes and applies nothing and
/// leaves its references to the request dispatcher; once they are durable
/// off the authority, the replay carries no payload and survives a crash.
#[test]
fn a_deferred_spill_writes_nothing_and_its_replay_survives_a_crash() {
    let (vfs, mut engine) = initialized_simulated_engine();
    let payloads: Vec<Vec<u8>> = (1..=3_u8).map(|byte| vec![byte; 1024 * 1024]).collect();
    let ids: Vec<BlockId> = payloads
        .iter()
        .map(|payload| engine.write_blob_block(payload).unwrap())
        .collect();
    let before = engine.state.clone();
    let wal_before = engine.wal_len();
    vfs.arm_fault(None).unwrap();
    let deferred = {
        let scope = SpillDeferralScope::allow();
        let error = engine
            .commit_references_with_authority_state(b"deferred", &ids, None)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "{error}");
        scope
            .take_deferred()
            .expect("the commit deferred its spill")
    };
    assert!(
        vfs.trace().unwrap().is_empty(),
        "a deferred commit wrote: {:?}",
        vfs.trace().unwrap()
    );
    assert_eq!(
        engine.state, before,
        "a deferred commit changed the engine state"
    );
    assert_eq!(engine.wal_len(), wal_before);
    assert!(
        ids.iter().all(|id| deferred.contains(id)),
        "deferred references miss a carried block"
    );
    engine
        .block_store_shared()
        .publish_deferred_spill(&deferred)
        .unwrap();
    // The replay writes the same blobs again; they are durable and cached,
    // so nothing is staged and the commit carries nothing.
    for payload in &payloads {
        let id = engine.write_blob_block(payload).unwrap();
        assert!(
            engine
                .block_store_shared()
                .pending_payload(id)
                .unwrap()
                .is_none(),
            "the replay staged a published block again"
        );
    }
    let replayed = engine
        .commit_references_with_authority_state(b"replayed", &ids, None)
        .unwrap();
    assert!(
        engine.wal_len() - wal_before < 64 * 1024,
        "the replay carried its payloads"
    );
    std::mem::forget(engine);
    vfs.crash().unwrap();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
    assert_eq!(reopened.state.durable_sequence, replayed.sequence);
    for (id, payload) in ids.iter().zip(&payloads) {
        assert_eq!(&reopened.read_block(*id).unwrap(), payload);
    }
}

/// Only the first commit inside a scope may defer: a request that already
/// committed must never be replayed, so a later oversized commit spills
/// inline exactly as outside the request dispatcher.
#[test]
fn only_the_first_commit_in_a_spill_deferral_scope_may_defer() {
    let (_vfs, mut engine) = initialized_simulated_engine();
    let small = engine.write_block(b"small committed payload").unwrap();
    let large: Vec<BlockId> = (1..=3_u8)
        .map(|byte| engine.write_block(&vec![byte; 1024 * 1024]).unwrap())
        .collect();
    let scope = SpillDeferralScope::allow();
    engine
        .commit_references_with_authority_state(b"first", &[small], None)
        .unwrap();
    engine
        .commit_references_with_authority_state(b"second", &large, None)
        .unwrap();
    assert!(scope.take_deferred().is_none());
    for id in large {
        assert!(
            engine
                .block_store_shared()
                .pending_payload(id)
                .unwrap()
                .is_none(),
            "the inline spill left a carried block staged"
        );
    }
}
