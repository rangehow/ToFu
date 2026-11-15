// Logical OCC over the tree and pending WAL overlay. No physical drain is
// required to serialize independent transactions; conflicting snapshots abort.
use super::*;
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::path::Path;

fn database() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    db.set_write_overlay_for_test(true);
    // These witnesses specify the default OCC boundary; TOFU_DB_TOMBSTONE_RECLAIM
    // deliberately makes leaf-changing range validation conservative.
    db.set_entity_tombstone_reclaim_override_for_test(Some(false));
    (vfs, db)
}

fn key(namespace: &str, bytes: &[u8]) -> EntityKey {
    EntityKey::new(1, 1, namespace, bytes).unwrap()
}

fn stage(db: &AuthorityDatabase, namespace: &str, bytes: &[u8]) -> AuthorityTransaction {
    let mut tx = db.begin(1, 1).unwrap();
    db.entity_put(&mut tx, key(namespace, bytes), b"value".to_vec())
        .unwrap();
    tx
}

#[test]
fn replica_persistent_snapshot_uses_captured_pin_and_remains_read_only_after_live_removal() {
    for overlay in [false, true] {
        let (_, mut db) = database();
        db.set_write_overlay_for_test(overlay);
        db.commit(stage(&db, "rows", b"old")).unwrap();
        let mut pin = db.begin(1, 1).unwrap();
        db.stage_persistent_snapshot_pin(&mut pin, b"retirement").unwrap();
        db.commit(pin).unwrap();
        let replica = db.build_read_replica().unwrap();
        let mut remove = db.begin(1, 1).unwrap();
        assert!(db.remove_persistent_snapshot_pin(&mut remove, b"retirement").unwrap());
        db.entity_put(&mut remove, key("rows", b"old"), b"new value".to_vec()).unwrap();
        db.commit(remove).unwrap();
        assert!(db.begin_persistent_snapshot(1, 1, b"retirement").unwrap().is_none());
        assert!(replica.begin_persistent_snapshot(1, 2, b"retirement").unwrap().is_none());
        let mut historical = replica.begin_persistent_snapshot(1, 1, b"retirement").unwrap().unwrap();
        assert_eq!(replica.entity_get(&mut historical, &key("rows", b"old")).unwrap(), Some(b"value".to_vec()));
        assert_eq!(replica.entity_put(&mut historical, key("rows", b"old"), b"forbidden".to_vec()).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        // The historical transaction registers its own older root pin; it
        // outlives both the durable pin and the replica's captured-root lease.
        drop(replica);
        assert_eq!(db.entity_get(&mut historical, &key("rows", b"old")).unwrap(), Some(b"value".to_vec()));
        assert_eq!(db.commit(historical).unwrap_err().kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(db.snapshot_pin_metrics().unwrap().active_handles, 0);
    }
}

#[test]
fn independent_stale_writers_commit_without_draining_pending_deltas() {
    let (_, mut db) = database();
    let transactions: Vec<_> = (0..24_u8)
        .map(|index| stage(&db, "rows", &[index]))
        .collect();
    for (index, tx) in transactions.into_iter().enumerate() {
        db.commit(tx).unwrap();
        assert_eq!(db.entity_overlay_entries_for_test(), index + 1);
    }
    let mut read = db.begin(1, 1).unwrap();
    for index in 0..24_u8 {
        assert_eq!(
            db.entity_get(&mut read, &key("rows", &[index])).unwrap(),
            Some(b"value".to_vec())
        );
    }
}

#[test]
fn logical_point_and_blind_write_witnesses_reject_updates_deletes_and_aba() {
    for (read_before_write, materialize) in [(false, false), (true, false), (false, true), (true, true)] {
        for mutation in ["update", "delete", "aba"] {
            let (_, mut db) = database();
            let seed = stage(&db, "rows", b"same");
            db.commit(seed).unwrap();
            let mut stale = db.begin(1, 1).unwrap();
            if read_before_write {
                assert!(db
                    .entity_get(&mut stale, &key("rows", b"same"))
                    .unwrap()
                    .is_some());
            }
            db.entity_put(&mut stale, key("rows", b"same"), b"stale".to_vec())
                .unwrap();
            let mut other = db.begin(1, 1).unwrap();
            if mutation == "update" {
                db.entity_put(&mut other, key("rows", b"same"), b"value".to_vec())
                    .unwrap();
            } else {
                db.entity_delete(&mut other, key("rows", b"same")).unwrap();
            }
            db.commit(other).unwrap();
            if mutation == "aba" {
                let recreated = stage(&db, "rows", b"same");
                db.commit(recreated).unwrap();
            }
            if materialize {
                // The newer write left the overlay: validation must find it
                // in the tree, never treat its absence as "unchanged".
                db.merge_entity_overlay_for_test().unwrap();
            }
            let pending = db.entity_overlay_entries_for_test();
            assert_eq!(
                db.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
            assert_eq!(db.entity_overlay_entries_for_test(), pending);
        }
    }
}

#[test]
fn stale_witnesses_survive_background_rewrites_of_their_unchanged_leaf_under_tombstone_reclaim() {
    // A background slice that materializes only writes older than the
    // snapshot rewrites the witnessed leaf without changing its logical
    // content; with production tombstone reclamation only a newer pending
    // write may abort the stale transaction.
    for reverse in [false, true] {
        for mutation in ["unrelated", "range-insert", "point-update", "blind-update"] {
            let (_, mut db) = database();
            db.set_entity_tombstone_reclaim_override_for_test(Some(true));
            for suffix in [b"a-one".as_slice(), b"a-two", b"z-outside"] {
                let tx = stage(&db, "predicate", suffix);
                db.commit(tx).unwrap();
            }
            db.merge_entity_overlay_for_test().unwrap();
            let older = stage(&db, "predicate", b"m-older");
            db.commit(older).unwrap();
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let rows = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 10) }
                else { db.entity_scan(&mut stale, &start, &end, 10) }.unwrap();
            assert_eq!(rows.len(), 2);
            assert!(db.entity_get(&mut stale, &key("predicate", b"z-outside")).unwrap().is_some());
            db.entity_put(&mut stale, key("result", b"out"), b"fresh".to_vec()).unwrap();
            let slice = db.prepare_entity_overlay_merge().unwrap().unwrap();
            let (namespace, suffix): (&str, &[u8]) = match mutation {
                "range-insert" => ("predicate", b"a-three"),
                "point-update" => ("predicate", b"z-outside"),
                "blind-update" => ("result", b"out"),
                _ => ("unrelated", b"tail"),
            };
            let newer = stage(&db, namespace, suffix);
            db.commit(newer).unwrap();
            assert!(db.finish_entity_overlay_merge(slice.encode().unwrap()).unwrap());
            assert_eq!(db.entity_overlay_entries_for_test(), 1, "the newer write stays pending");
            if mutation == "unrelated" {
                db.commit(stale).unwrap();
                let mut read = db.begin(1, 1).unwrap();
                assert_eq!(db.entity_get(&mut read, &key("result", b"out")).unwrap(), Some(b"fresh".to_vec()));
            } else {
                assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock, "{mutation}");
            }
        }
    }
}

#[test]
fn range_witnesses_detect_overlay_and_materialized_phantoms_in_both_directions() {
    for reverse in [false, true] {
        for materialize in [false, true] {
            let (_, mut db) = database();
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let rows = if reverse {
                db.entity_scan_reverse(&mut stale, &start, &end, 1)
            } else {
                db.entity_scan(&mut stale, &start, &end, 1)
            }
            .unwrap();
            assert!(rows.is_empty());
            db.entity_put(&mut stale, key("result", b"out"), b"empty".to_vec())
                .unwrap();
            let insert = stage(&db, "predicate", b"a-new");
            db.commit(insert).unwrap();
            if materialize {
                db.merge_entity_overlay_for_test().unwrap();
            }
            assert_eq!(
                db.commit(stale).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
    }
}

#[test]
fn disjoint_range_writers_and_point_readers_survive_other_commits_and_merges() {
    for materialize in [false, true] {
        let (_, mut db) = database();
        let mut stale = db.begin(1, 1).unwrap();
        assert!(db
            .entity_get(&mut stale, &key("predicate", b"missing"))
            .unwrap()
            .is_none());
        if !materialize {
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            assert!(db
                .entity_scan(&mut stale, &start, &end, 1)
                .unwrap()
                .is_empty());
        }
        db.entity_put(&mut stale, key("result", b"out"), b"correct".to_vec())
            .unwrap();
        let other = stage(&db, "predicate", b"b");
        db.commit(other).unwrap();
        if materialize {
            db.merge_entity_overlay_for_test().unwrap();
        }
        db.commit(stale).unwrap();
        let mut read = db.begin(1, 1).unwrap();
        assert_eq!(
            db.entity_get(&mut read, &key("result", b"out")).unwrap(),
            Some(b"correct".to_vec())
        );
    }
}

#[test]
fn stale_overlay_commit_faults_preserve_every_acknowledged_transaction() {
    fn prepared() -> (
        Arc<DeterministicVfs>,
        AuthorityDatabase,
        AuthorityTransaction,
    ) {
        let (vfs, mut db) = database();
        let stale = stage(&db, "rows", b"stale");
        let other = stage(&db, "rows", b"other");
        db.commit(other).unwrap();
        (vfs, db, stale)
    }
    let (vfs, mut db, tx) = prepared();
    vfs.arm_fault(None).unwrap();
    db.commit(tx).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(!trace.is_empty());
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        for action in actions {
            let (vfs, mut db, tx) = prepared();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = db.commit(tx).is_ok();
            let durable = Arc::new(vfs.fork_durable().unwrap());
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
            let mut read = reopened.begin(1, 1).unwrap();
            assert_eq!(
                reopened
                    .entity_get(&mut read, &key("rows", b"other"))
                    .unwrap(),
                Some(b"value".to_vec())
            );
            if acknowledged {
                assert_eq!(
                    reopened
                        .entity_get(&mut read, &key("rows", b"stale"))
                        .unwrap(),
                    Some(b"value".to_vec())
                );
            }
        }
    }
}

#[test]
fn range_versions_ignore_disjoint_leaf_rewrites_and_overlay_materialization() {
    for reverse in [false, true] {
        for preexisting in [false, true] {
            let (_, mut db) = database();
            for suffix in [b"a-one".as_slice(), b"z-other"] {
                if preexisting || suffix == b"z-other" { let tx = stage(&db, "predicate", suffix); db.commit(tx).unwrap(); }
            }
            db.merge_entity_overlay_for_test().unwrap();
            // A pending value must remain equivalent when materialized too.
            if preexisting { let tx = stage(&db, "predicate", b"a-two"); db.commit(tx).unwrap(); }
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let before = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 10) }
                else { db.entity_scan(&mut stale, &start, &end, 10) }.unwrap();
            assert_eq!(before.len(), if preexisting { 2 } else { 0 });
            db.entity_put(&mut stale, key("result", b"out"), b"unchanged predicate".to_vec()).unwrap();
            let other = stage(&db, "predicate", b"z-other"); db.commit(other).unwrap();
            db.merge_entity_overlay_for_test().unwrap();
            db.commit(stale).unwrap();
        }
    }
}

#[test]
fn range_versions_reject_materialized_updates_deletes_and_absent_present_absent() {
    for reverse in [false, true] {
        for mutation in ["update", "delete", "absent-aba"] {
            let (_, mut db) = database();
            let seed = stage(&db, "predicate", b"z-outside"); db.commit(seed).unwrap();
            if mutation != "absent-aba" { let seed = stage(&db, "predicate", b"a-row"); db.commit(seed).unwrap(); }
            db.merge_entity_overlay_for_test().unwrap();
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 1) }
                else { db.entity_scan(&mut stale, &start, &end, 1) }.unwrap();
            db.entity_put(&mut stale, key("result", b"out"), b"must abort".to_vec()).unwrap();
            if mutation != "delete" { let tx = stage(&db, "predicate", b"a-row"); db.commit(tx).unwrap(); }
            if mutation != "update" {
                let mut tx = db.begin(1, 1).unwrap(); db.entity_delete(&mut tx, key("predicate", b"a-row")).unwrap(); db.commit(tx).unwrap();
            }
            db.merge_entity_overlay_for_test().unwrap();
            assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        }
    }
}

#[test]
fn range_versions_reject_retirement_of_rows_owned_only_by_snapshot_overlay() {
    for reverse in [false, true] {
        let (_, mut db) = database();
        let seed = stage(&db, "predicate", b"a-row"); db.commit(seed).unwrap();
        let mut stale = db.begin(1, 1).unwrap();
        let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
        let rows = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 1) }
            else { db.entity_scan(&mut stale, &start, &end, 1) }.unwrap();
        assert_eq!(rows.len(), 1);
        db.entity_put(&mut stale, key("result", b"out"), b"must abort".to_vec()).unwrap();
        let mut retire = db.begin(1, 1).unwrap();
        db.entity_retire_range(&mut retire, &start, &end).unwrap();
        db.commit(retire).unwrap();
        assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }
}

#[test]
fn randomized_histories_commit_a_stale_transaction_exactly_when_its_witnesses_are_unchanged() {
    // Safety on every path: a transaction whose point, blind-write or range
    // witness was overwritten after its snapshot never commits. Exactness while
    // the overlay still holds every newer write: an unchanged transaction always
    // commits, whatever merges (including tombstone reclamation) rewrote.
    struct Rng(u64);
    impl Rng {
        fn below(&mut self, bound: u64) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0 % bound
        }
    }
    let row = |index: u64| key("rows", format!("k{index:02}").as_bytes());
    for seed in 1..=32_u64 {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut db = EntityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        db.set_write_overlay_enabled(true);
        db.set_tombstone_reclaim_override_for_test(Some(seed % 2 == 0));
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15));
        let mut seed_rows = db.begin(1, 1).unwrap();
        for index in 0..16 {
            if rng.below(2) == 0 { db.put(&mut seed_rows, row(index), vec![index as u8]).unwrap(); }
        }
        db.put(&mut seed_rows, key("result", b"out"), b"seed".to_vec()).unwrap();
        db.commit(seed_rows).unwrap();
        for round in 0..24_u64 {
            let mut stale = db.begin(1, 1).unwrap();
            let snapshot = stale.snapshot.sequence;
            let (read_a, read_b, blind) = (rng.below(16), rng.below(16), rng.below(16));
            let (low, high) = { let (a, b) = (rng.below(16), rng.below(16)); (a.min(b), a.max(b)) };
            db.get(&mut stale, &row(read_a)).unwrap();
            db.get(&mut stale, &row(read_b)).unwrap();
            db.scan(&mut stale, &row(low), &row(high + 1), 100).unwrap();
            db.put(&mut stale, row(blind), b"stale".to_vec()).unwrap();
            db.put(&mut stale, key("result", b"out"), round.to_le_bytes().to_vec()).unwrap();
            let mut written = Vec::new();
            for _ in 0..rng.below(3) {
                let index = rng.below(16);
                let mut other = db.begin(1, 1).unwrap();
                if rng.below(3) == 0 { db.delete(&mut other, row(index)).unwrap(); }
                else { db.put(&mut other, row(index), round.to_be_bytes().to_vec()).unwrap(); }
                db.commit(other).unwrap();
                written.push(index);
            }
            match rng.below(4) {
                0 => {}
                1 => if let Some(slice) = db.prepare_overlay_merge().unwrap() {
                    assert!(db.finish_overlay_merge(slice.encode().unwrap()).unwrap());
                },
                2 => { db.merge_overlay().unwrap(); }
                _ => db.merge_overlay_fully().unwrap(),
            }
            let complete = db.overlay.read().unwrap().tree_through() <= snapshot;
            let changed = written.iter()
                .any(|index| [read_a, read_b, blind].contains(index) || (low..=high).contains(index));
            match db.commit(stale) {
                Ok(_) => assert!(!changed, "seed {seed} round {round}: a stale transaction committed"),
                Err(error) => {
                    assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "seed {seed} round {round}: {error}");
                    assert!(changed || !complete,
                        "seed {seed} round {round}: an unchanged transaction aborted beside a complete overlay");
                }
            }
        }
    }
}

/// VFS reads while `transaction` validates against the current head with the
/// caches enabled but emptied, and whether validation refused it.
fn cold_validation(vfs: &DeterministicVfs, db: &AuthorityDatabase, transaction: &AuthorityTransaction) -> (usize, bool) {
    db.configure_hot_data_caches(0, 0).unwrap();
    db.configure_hot_data_caches(64 << 20, 64 << 20).unwrap();
    vfs.arm_fault(None).unwrap();
    let refused = match db.rehearse_commit_validation(transaction) {
        Ok(()) => false,
        Err(error) => {
            assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "{error}");
            true
        }
    };
    let reads = vfs.trace().unwrap().into_iter().filter(|operation| *operation == Operation::Read).count();
    (reads, refused)
}

/// A snapshot whose point read, range scan and blind write all resolve from
/// merged, checkpointed tree pages.
fn stale_over_tree(db: &mut AuthorityDatabase) -> AuthorityTransaction {
    for (namespace, suffix) in [("predicate", b"a-one".as_slice()), ("predicate", b"a-two"),
        ("predicate", b"z-other"), ("rows", b"read"), ("rows", b"blind")] {
        let seed = stage(db, namespace, suffix);
        db.commit(seed).unwrap();
    }
    db.merge_entity_overlay_for_test().unwrap();
    db.checkpoint_wal_window().unwrap();
    let mut stale = db.begin(1, 1).unwrap();
    assert!(db.entity_get(&mut stale, &key("rows", b"read")).unwrap().is_some());
    let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
    assert_eq!(db.entity_scan(&mut stale, &start, &end, 10).unwrap().len(), 2);
    db.entity_put(&mut stale, key("rows", b"blind"), b"stale".to_vec()).unwrap();
    stale
}

fn commit_change(db: &mut AuthorityDatabase, change: &str) {
    let mut tx = db.begin(1, 1).unwrap();
    match change {
        "unrelated" => db.entity_put(&mut tx, key("rows", b"unrelated"), b"value".to_vec()).unwrap(),
        "witnessed" => db.entity_put(&mut tx, key("rows", b"read"), b"newer".to_vec()).unwrap(),
        "blind" => db.entity_put(&mut tx, key("rows", b"blind"), b"newer".to_vec()).unwrap(),
        "phantom" => db.entity_put(&mut tx, key("predicate", b"a-three"), b"value".to_vec()).unwrap(),
        "deleted" => db.entity_delete(&mut tx, key("predicate", b"a-one")).unwrap(),
        other => panic!("unknown change {other}"),
    }
    db.commit(tx).unwrap();
}

const CHANGES: [&str; 5] = ["unrelated", "witnessed", "blind", "phantom", "deleted"];

#[test]
fn snapshots_newer_than_every_merged_write_validate_from_pending_writes_alone() {
    for reclaim in [false, true] {
        for change in CHANGES {
            let (vfs, mut db) = database();
            db.set_entity_tombstone_reclaim_override_for_test(Some(reclaim));
            let stale = stale_over_tree(&mut db);
            commit_change(&mut db, "unrelated");
            if change != "unrelated" { commit_change(&mut db, change); }
            let refuse = change != "unrelated";
            assert_eq!(cold_validation(&vfs, &db, &stale), (0, refuse), "reclaim={reclaim} change={change}");
            let committed = db.commit(stale);
            if refuse {
                assert_eq!(committed.unwrap_err().kind(), io::ErrorKind::WouldBlock);
            } else {
                committed.unwrap();
                let mut read = db.begin(1, 1).unwrap();
                assert_eq!(db.entity_get(&mut read, &key("rows", b"blind")).unwrap(), Some(b"stale".to_vec()));
            }
        }
    }
}

#[test]
fn a_merge_of_writes_newer_than_the_snapshot_validates_against_the_tree() {
    for change in CHANGES {
        let (vfs, mut db) = database();
        let stale = stale_over_tree(&mut db);
        commit_change(&mut db, "unrelated");
        if change != "unrelated" { commit_change(&mut db, change); }
        db.merge_entity_overlay_for_test().unwrap();
        db.checkpoint_wal_window().unwrap();
        assert_eq!(db.entity_overlay_entries_for_test(), 0);
        let (reads, refused) = cold_validation(&vfs, &db, &stale);
        assert!(reads > 0, "change={change}: the merged writes are only in the tree");
        assert_eq!(refused, change != "unrelated", "change={change}");
    }
}

#[test]
fn a_drain_slice_of_writes_older_than_the_snapshot_never_refuses_an_unchanged_range() {
    // Production drains merge the oldest pending slice while newer commits stay
    // pending. With tombstone reclaim (the production setting) the tree path
    // refuses any range whose leaves a merge rewrote, unchanged or not.
    for consumes_newer in [false, true] {
        for reverse in [false, true] {
            let (vfs, mut db) = database();
            db.set_entity_tombstone_reclaim_override_for_test(Some(true));
            let seed = stage(&db, "predicate", b"a-seed");
            db.commit(seed).unwrap();
            db.merge_entity_overlay_for_test().unwrap();
            db.checkpoint_wal_window().unwrap();
            // Pending at the snapshot: more than one 64-entry drain slice.
            for index in 0..100_u32 {
                let tx = stage(&db, "predicate", format!("a-{index:03}").as_bytes());
                db.commit(tx).unwrap();
            }
            let mut stale = db.begin(1, 1).unwrap();
            let (start, end) = EntityKey::prefix_range(1, 1, "predicate", b"a").unwrap();
            let rows = if reverse { db.entity_scan_reverse(&mut stale, &start, &end, 200) }
                else { db.entity_scan(&mut stale, &start, &end, 200) }.unwrap();
            assert_eq!(rows.len(), 101);
            db.entity_put(&mut stale, key("result", b"out"), b"unchanged".to_vec()).unwrap();
            commit_change(&mut db, "unrelated");
            let pending = db.entity_overlay_entries_for_test();
            if consumes_newer {
                db.merge_entity_overlay_for_test().unwrap();
                assert_eq!(db.entity_overlay_entries_for_test(), 0);
                db.checkpoint_wal_window().unwrap();
            } else {
                assert!(db.merge_entity_overlay().unwrap());
                let left = db.entity_overlay_entries_for_test();
                assert!(left > 1 && left < pending, "the slice leaves the newest writes pending");
            }
            let (reads, refused) = cold_validation(&vfs, &db, &stale);
            if consumes_newer {
                assert!(reads > 0 && refused, "reverse={reverse}: the conservative tree path");
            } else {
                assert_eq!((reads, refused), (0, false), "reverse={reverse}");
                db.commit(stale).unwrap();
            }
        }
    }
}

#[test]
fn replayed_pending_writes_validate_later_snapshots_from_the_overlay() {
    let (vfs, mut db) = database();
    let seed = stage(&db, "rows", b"read");
    db.commit(seed).unwrap();
    db.merge_entity_overlay_for_test().unwrap();
    db.checkpoint_wal_window().unwrap();
    let replayed = stage(&db, "rows", b"replayed");
    db.commit(replayed).unwrap();
    let durable = Arc::new(vfs.fork_durable().unwrap());
    let mut reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable.clone()).unwrap();
    reopened.set_write_overlay_for_test(true);
    reopened.set_entity_tombstone_reclaim_override_for_test(Some(true));
    assert_eq!(reopened.entity_overlay_entries_for_test(), 1);
    let mut stale = reopened.begin(1, 1).unwrap();
    assert!(reopened.entity_get(&mut stale, &key("rows", b"read")).unwrap().is_some());
    assert!(reopened.entity_get(&mut stale, &key("rows", b"replayed")).unwrap().is_some());
    reopened.entity_put(&mut stale, key("rows", b"out"), b"value".to_vec()).unwrap();
    commit_change(&mut reopened, "unrelated");
    assert_eq!(cold_validation(&durable, &reopened, &stale), (0, false));
    commit_change(&mut reopened, "witnessed");
    assert_eq!(cold_validation(&durable, &reopened, &stale), (0, true));
}

#[test]
fn tree_synchronous_commits_after_the_snapshot_validate_against_the_tree() {
    let (vfs, mut db) = database();
    db.set_write_overlay_for_test(false);
    let stale = stale_over_tree(&mut db);
    commit_change(&mut db, "unrelated");
    let (reads, refused) = cold_validation(&vfs, &db, &stale);
    assert!(reads > 0 && !refused);
    commit_change(&mut db, "witnessed");
    assert!(cold_validation(&vfs, &db, &stale).1);
}
