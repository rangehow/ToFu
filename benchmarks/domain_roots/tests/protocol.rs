//! Independent balance-vector expectations plus real engine fault/reopen tests.
mod support;
use std::{
    io,
    path::{Path, PathBuf},
    sync::{Arc, Barrier},
    thread,
};
use support::fork_durable_public;
use tofu_db::{
    engine::Engine,
    vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs},
};
use tofudb_domain_roots::{
    store::Backend, Change, Database, DomainKey, Phase, Predicate, Request, Scope, Seed,
};

fn scope() -> Scope {
    Scope {
        tenant: 1,
        owner: 1,
    }
}

#[test]
fn another_domain_finishes_preparation_while_actual_wal_sync_is_blocked() {
    use std::{
        sync::{mpsc, Mutex},
        time::Duration,
    };
    use support::{GatedVfs, SyncGate};
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("db");
    std::fs::create_dir(&path).unwrap();
    let (gate, entered, release) = SyncGate::new();
    let wal = Arc::new(Mutex::new(None));
    let vfs = Arc::new(GatedVfs {
        inner: Arc::new(tofu_db::vfs::RealVfs),
        wal: wal.clone(),
        gate: gate.clone(),
    });
    let engine = Engine::initialize_with_vfs(&path, vfs).unwrap();
    *wal.lock().unwrap() = Some(engine.wal_path().unwrap());
    let db = Arc::new(Database::initialize(Backend::Native(engine), seeds()).unwrap());
    let before = db.snapshot().unwrap();
    let first = db.prepare(&before, request("disk", 0)).unwrap();
    gate.arm();
    thread::scope(|threads| {
        let writer = {
            let db = db.clone();
            threads.spawn(move || db.commit(first))
        };
        let reached = entered.recv_timeout(Duration::from_secs(5));
        let (finished, ready) = mpsc::sync_channel(1);
        let db2 = db.clone();
        let snapshot = before.clone();
        let preparer = threads.spawn(move || {
            let p = db2.prepare(&snapshot, request("cpu", 1));
            finished.send(p).unwrap();
        });
        let prepared = ready.recv_timeout(Duration::from_secs(2));
        // Always release/join before asserting so a failure never strands I/O.
        release.send(()).unwrap();
        writer.join().unwrap().unwrap();
        preparer.join().unwrap();
        assert!(reached.is_ok(), "did not gate the actual WAL sync");
        let prepared = prepared
            .expect("independent CPU preparation blocked behind WAL fsync")
            .unwrap();
        db.commit(prepared).unwrap();
    });
    assert_eq!(db.snapshot().unwrap().values(scope(), 0).unwrap()[0], 99);
    assert_eq!(db.snapshot().unwrap().values(scope(), 1).unwrap()[0], 99);
}
fn seeds() -> Vec<Seed> {
    (0..4)
        .map(|domain| Seed {
            key: DomainKey {
                scope: scope(),
                domain,
            },
            values: vec![100; 8],
            evidence_bytes: 128,
        })
        .collect()
}
fn backend(kind: &str, path: &Path, create: bool) -> Backend {
    match kind {
        "native" => Backend::native(path, create).unwrap(),
        "sqlite" => Backend::sqlite(path, create).unwrap(),
        _ => unreachable!(),
    }
}
fn request(id: &str, domain: u64) -> Request {
    Request {
        scope: scope(),
        command_id: id.into(),
        reads: vec![],
        predicates: vec![],
        changes: vec![
            Change {
                domain,
                slot: 0,
                delta: -1,
            },
            Change {
                domain,
                slot: 1,
                delta: 1,
            },
        ],
    }
}
fn execute(db: &Database, req: Request) -> tofudb_domain_roots::Outcome {
    db.commit(db.prepare(&db.snapshot().unwrap(), req).unwrap())
        .unwrap()
}

#[test]
fn independent_preparation_overlaps_and_old_frontier_stays_atomic() {
    for kind in ["native", "sqlite"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("db");
        let db = Arc::new(Database::initialize(backend(kind, &path, true), seeds()).unwrap());
        let old = db.snapshot().unwrap();
        let barrier = Arc::new(Barrier::new(4));
        thread::scope(|group| {
            let handles: Vec<_> = (0..4)
                .map(|domain| {
                    let db = db.clone();
                    let barrier = barrier.clone();
                    let old = old.clone();
                    group.spawn(move || {
                        let prepared = db
                            .prepare_with_hook(&old, request(&format!("p{domain}"), domain), || {
                                barrier.wait();
                                Ok(())
                            })
                            .unwrap();
                        db.commit(prepared).unwrap()
                    })
                })
                .collect();
            for handle in handles {
                assert!(!handle.join().unwrap().replayed);
            }
        });
        assert_eq!(db.max_parallel_preparations(), 4);
        for domain in 0..4 {
            assert_eq!(old.values(scope(), domain).unwrap(), vec![100; 8]);
            assert_eq!(
                db.snapshot().unwrap().values(scope(), domain).unwrap(),
                vec![99, 101, 100, 100, 100, 100, 100, 100]
            );
        }
        let receipts = db.receipts().unwrap();
        let current = db.snapshot().unwrap();
        drop(db);
        let reopened = Database::open(backend(kind, &path, false)).unwrap();
        assert_eq!(reopened.receipts().unwrap(), receipts);
        assert_eq!(reopened.snapshot().unwrap().roots(), current.roots());
    }
}

#[test]
fn read_only_dependencies_prevent_write_skew_and_same_content_aba() {
    for kind in ["native", "sqlite"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("db");
        let db = Database::initialize(backend(kind, &path, true), seeds()).unwrap();
        let old = db.snapshot().unwrap();
        let mut a = request("a", 0);
        a.predicates.push(Predicate {
            domain: 1,
            slot: 0,
            at_least: 100,
        });
        let mut b = request("b", 1);
        b.predicates.push(Predicate {
            domain: 0,
            slot: 0,
            at_least: 100,
        });
        let a = db.prepare(&old, a).unwrap();
        let b = db.prepare(&old, b).unwrap();
        db.commit(a).unwrap();
        assert_eq!(db.commit(b).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        let mut stale = request("stale", 2);
        stale.reads.push(3);
        let stale = db.prepare(&old, stale).unwrap();
        let before = db.snapshot().unwrap();
        let zero = Request {
            scope: scope(),
            command_id: "aba".into(),
            reads: vec![],
            predicates: vec![],
            changes: vec![Change {
                domain: 3,
                slot: 0,
                delta: 0,
            }],
        };
        execute(&db, zero);
        assert_eq!(
            before.values(scope(), 3).unwrap(),
            db.snapshot().unwrap().values(scope(), 3).unwrap()
        );
        assert_eq!(
            db.commit(stale).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }
}

#[test]
fn replay_scope_incarnation_overflow_and_multi_domain_failure_are_closed() {
    for kind in ["native", "sqlite"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("db");
        let db = Database::initialize(backend(kind, &path, true), seeds()).unwrap();
        let req = request("once", 0);
        let first = execute(&db, req.clone());
        execute(&db, request("later", 0));
        let replay = execute(&db, req.clone());
        assert!(replay.replayed);
        assert_eq!(replay.receipt, first.receipt);
        let mut conflict = req;
        conflict.changes[0].delta = -2;
        assert_eq!(
            db.prepare(&db.snapshot().unwrap(), conflict)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        let mut foreign = request("foreign", 0);
        foreign.scope.owner = 2;
        assert_eq!(
            db.prepare(&db.snapshot().unwrap(), foreign)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        let original = db.snapshot().unwrap();
        let mut cross = request("cross", 0);
        cross.changes.push(Change {
            domain: 1,
            slot: 0,
            delta: i64::MAX,
        });
        assert!(db.prepare(&original, cross).is_err());
        assert_eq!(db.snapshot().unwrap().roots(), original.roots());
        let other_path = temp.path().join("other");
        let other = Database::initialize(backend(kind, &other_path, true), seeds()).unwrap();
        assert_eq!(
            other
                .prepare(&original, request("bad", 0))
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        let prepared = db.prepare(&original, request("prepared", 0)).unwrap();
        assert_eq!(
            other.commit(prepared).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }
}

#[test]
fn cross_domain_decision_and_receipt_recover_together_at_each_publish_phase() {
    for kind in ["native", "sqlite"] {
        for target in [
            Phase::BeforeDurable,
            Phase::AfterDurable,
            Phase::AfterPublish,
        ] {
            let temp = tempfile::tempdir().unwrap();
            let path = temp.path().join("db");
            let db = Database::initialize(backend(kind, &path, true), seeds()).unwrap();
            let before = db.snapshot().unwrap();
            let mut req = request("cross", 0);
            req.changes.push(Change {
                domain: 1,
                slot: 0,
                delta: 7,
            });
            let prepared = db.prepare(&before, req).unwrap();
            let result = db.commit_with_hooks(
                prepared,
                |phase| {
                    if phase == target {
                        Err(io::Error::other("injected publish phase"))
                    } else {
                        Ok(())
                    }
                },
                |_| Ok(()),
            );
            assert!(result.is_err());
            if target == Phase::BeforeDurable {
                assert_eq!(db.snapshot().unwrap().roots(), before.roots());
            } else {
                assert!(db.snapshot().is_err());
            }
            drop(db);
            let recovered = Database::open(backend(kind, &path, false)).unwrap();
            let after = recovered.snapshot().unwrap();
            let applied = target != Phase::BeforeDurable;
            assert_eq!(
                after.values(scope(), 0).unwrap()[0],
                if applied { 99 } else { 100 }
            );
            assert_eq!(
                after.values(scope(), 1).unwrap()[0],
                if applied { 107 } else { 100 }
            );
            assert_eq!(recovered.receipts().unwrap().len(), usize::from(applied));
        }
    }
}

#[test]
fn sqlite_every_partial_page_or_decision_write_rolls_back() {
    let temp = tempfile::tempdir().unwrap();
    let probe_path = temp.path().join("probe");
    let probe = Database::initialize(backend("sqlite", &probe_path, true), seeds()).unwrap();
    let mut req = request("cross", 0);
    req.changes.push(Change {
        domain: 1,
        slot: 0,
        delta: 4,
    });
    let prepared = probe
        .prepare(&probe.snapshot().unwrap(), req.clone())
        .unwrap();
    let writes = prepared.work.prepared_pages as usize + 1;
    drop(probe);
    for fail in 1..=writes {
        let path = temp.path().join(format!("case-{fail}"));
        let db = Database::initialize(backend("sqlite", &path, true), seeds()).unwrap();
        let before = db.snapshot().unwrap();
        let prepared = db.prepare(&before, req.clone()).unwrap();
        let mut fired = false;
        assert!(db
            .commit_with_hooks(
                prepared,
                |_| Ok(()),
                |index| if index == fail {
                    fired = true;
                    Err(io::Error::other("SQL partial write"))
                } else {
                    Ok(())
                }
            )
            .is_err());
        assert!(fired);
        drop(db);
        let recovered = Database::open(backend("sqlite", &path, false)).unwrap();
        assert_eq!(recovered.snapshot().unwrap().roots(), before.roots());
        assert!(recovered.receipts().unwrap().is_empty());
    }
}

fn simulated() -> (Arc<DeterministicVfs>, Database) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let db = Database::initialize(Backend::Native(engine), seeds()).unwrap();
    (vfs, db)
}

#[test]
fn native_every_write_and_sync_fault_uses_pre_drop_durable_image() {
    let (vfs, probe) = simulated();
    let mut req = request("cross", 0);
    req.changes.push(Change {
        domain: 1,
        slot: 0,
        delta: 5,
    });
    let prepared = probe
        .prepare(&probe.snapshot().unwrap(), req.clone())
        .unwrap();
    vfs.arm_fault(None).unwrap();
    probe.commit(prepared).unwrap();
    let trace = vfs.trace().unwrap();
    let mut cases = 0;
    for (index, operation) in trace.iter().enumerate() {
        if !matches!(
            operation,
            Operation::Write | Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        ) {
            continue;
        }
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        for action in actions {
            let (vfs, db) = simulated();
            let old = db.snapshot().unwrap();
            let prepared = db.prepare(&old, req.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = db.commit(prepared).is_ok();
            let actual_trace = vfs.trace().unwrap();
            assert!(actual_trace.len() > index);
            assert_eq!(
                &actual_trace[..=index],
                &trace[..=index],
                "armed absolute fault must reach the exact operation"
            );
            let durable = fork_durable_public(&vfs);
            let engine = Engine::open_with_vfs(Path::new("/data"), durable).unwrap();
            let recovered = Database::open(Backend::Native(engine)).unwrap();
            let state = recovered.snapshot().unwrap();
            let applied = state.sequence() == 2;
            assert!(!acknowledged || applied);
            assert_eq!(
                state.values(scope(), 0).unwrap()[0],
                if applied { 99 } else { 100 }
            );
            assert_eq!(
                state.values(scope(), 1).unwrap()[0],
                if applied { 105 } else { 100 }
            );
            assert_eq!(recovered.receipts().unwrap().len(), usize::from(applied));
            cases += 1;
        }
    }
    assert!(cases >= 3, "fault trace {trace:?}");
}

fn child_line(
    child: &mut std::process::Child,
) -> (std::sync::mpsc::Receiver<String>, thread::JoinHandle<()>) {
    use std::io::BufRead;
    let stdout = child.stdout.take().unwrap();
    let (send, recv) = std::sync::mpsc::channel();
    let join = thread::spawn(move || {
        for line in io::BufReader::new(stdout).lines() {
            if send.send(line.unwrap()).is_err() {
                break;
            }
        }
    });
    (recv, join)
}

#[test]
fn real_sigkill_before_decision_after_durability_and_after_ack() {
    use std::{
        io::Write,
        process::{Command, Stdio},
        time::Duration,
    };
    for kind in ["native", "sqlite"] {
        for pause in ["before_durable", "after_durable", "after_publish", "ack"] {
            let temp = tempfile::tempdir().unwrap();
            let path: PathBuf = temp.path().join("db");
            let db = Database::initialize(backend(kind, &path, true), seeds()).unwrap();
            drop(db);
            let mut child = Command::new(env!("CARGO_BIN_EXE_tofudb-domain-roots"))
                .args([kind, path.to_str().unwrap(), "run", pause])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let (lines, join) = child_line(&mut child);
            let mut req = request("killed", 0);
            req.changes.push(Change {
                domain: 1,
                slot: 0,
                delta: 9,
            });
            writeln!(
                child.stdin.as_mut().unwrap(),
                "{}",
                serde_json::json!({"op":"write","request":req})
            )
            .unwrap();
            child.stdin.as_mut().unwrap().flush().unwrap();
            let response = lines.recv_timeout(Duration::from_secs(10));
            child.kill().unwrap();
            let status = child.wait().unwrap();
            join.join().unwrap();
            assert!(!status.success());
            let response: serde_json::Value = serde_json::from_str(&response.unwrap()).unwrap();
            if pause == "ack" {
                assert_eq!(response["ok"], true);
            } else {
                assert_eq!(response["phase"], pause);
            }
            let recovered = Database::open(backend(kind, &path, false)).unwrap();
            let applied = pause != "before_durable";
            let snapshot = recovered.snapshot().unwrap();
            assert_eq!(
                snapshot.values(scope(), 0).unwrap()[0],
                if applied { 99 } else { 100 }
            );
            assert_eq!(
                snapshot.values(scope(), 1).unwrap()[0],
                if applied { 109 } else { 100 }
            );
            assert_eq!(recovered.receipts().unwrap().len(), usize::from(applied));
        }
    }
}

#[test]
fn readers_never_combine_domains_from_two_commit_frontiers() {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    for kind in ["native", "sqlite"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("db");
        let db = Arc::new(Database::initialize(backend(kind, &path, true), seeds()).unwrap());
        let done = AtomicBool::new(false);
        let reads = AtomicUsize::new(0);
        let started = Barrier::new(2);
        thread::scope(|threads| {
            let db_reader = db.clone();
            let done = &done;
            let reads = &reads;
            let started = &started;
            let reader = threads.spawn(move || {
                started.wait();
                while !done.load(Ordering::Acquire) {
                    let snap = db_reader.snapshot().unwrap();
                    assert_eq!(
                        snap.values(scope(), 0).unwrap()[0] + snap.values(scope(), 1).unwrap()[0],
                        200
                    );
                    reads.fetch_add(1, Ordering::Relaxed);
                    thread::yield_now();
                }
            });
            started.wait();
            for index in 0..32 {
                let req = Request {
                    scope: scope(),
                    command_id: format!("atomic-{index}"),
                    reads: vec![],
                    predicates: vec![],
                    changes: vec![
                        Change {
                            domain: 0,
                            slot: 0,
                            delta: -1,
                        },
                        Change {
                            domain: 1,
                            slot: 0,
                            delta: 1,
                        },
                    ],
                };
                execute(&db, req);
            }
            done.store(true, Ordering::Release);
            reader.join().unwrap();
        });
        assert!(reads.load(Ordering::Relaxed) > 0);
        assert_eq!(db.snapshot().unwrap().values(scope(), 0).unwrap()[0], 68);
    }
}

#[test]
fn randomized_history_matches_an_independent_vector_and_all_receipts() {
    for kind in ["native", "sqlite"] {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("db");
        let db = Database::initialize(backend(kind, &path, true), seeds()).unwrap();
        let mut expected = vec![vec![100i64; 8]; 4];
        let mut random = 20261019u64;
        let mut prior = Vec::new();
        for index in 0..96 {
            random ^= random << 13;
            random ^= random >> 7;
            random ^= random << 17;
            let domain = (random % 4) as usize;
            let other = ((random >> 8) % 4) as usize;
            let slot = ((random >> 16) % 8) as usize;
            let req = Request {
                scope: scope(),
                command_id: format!("random-{index}"),
                reads: vec![(domain ^ 1) as u64],
                predicates: vec![],
                changes: vec![
                    Change {
                        domain: domain as u64,
                        slot: slot as u32,
                        delta: -1,
                    },
                    Change {
                        domain: other as u64,
                        slot: slot as u32,
                        delta: 1,
                    },
                ],
            };
            let outcome = execute(&db, req.clone());
            let mut values = Vec::new();
            for change in &req.changes {
                expected[change.domain as usize][change.slot as usize] += change.delta;
                values.push(tofudb_domain_roots::ValueRecord {
                    domain: change.domain,
                    slot: change.slot,
                    value: expected[change.domain as usize][change.slot as usize],
                });
            }
            assert_eq!(outcome.receipt.values, values);
            assert_eq!(outcome.receipt.sequence, index + 2);
            prior.push((req, outcome.receipt));
            if index % 7 == 0 {
                let (req, receipt) = &prior[(random as usize) % prior.len()];
                assert_eq!(execute(&db, req.clone()).receipt, *receipt);
            }
        }
        for domain in 0..4 {
            assert_eq!(
                db.snapshot().unwrap().values(scope(), domain).unwrap(),
                expected[domain as usize]
            );
        }
        let receipts = db.receipts().unwrap();
        drop(db);
        let reopened = Database::open(backend(kind, &path, false)).unwrap();
        assert_eq!(reopened.receipts().unwrap(), receipts);
        for domain in 0..4 {
            assert_eq!(
                reopened
                    .snapshot()
                    .unwrap()
                    .values(scope(), domain)
                    .unwrap(),
                expected[domain as usize]
            );
        }
    }
}
