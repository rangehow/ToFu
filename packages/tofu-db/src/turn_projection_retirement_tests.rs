// Projection-head retirement: bounded WAL cleanup, isolation, and crash atomicity.
// Entry points are private Turn helpers; dependencies are Authority and deterministic VFS.
use super::*;
use std::path::Path;
use std::sync::Arc;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

fn head() -> ProjectionHeadDescriptor {
    ProjectionHeadDescriptor { head_id: "head".into(), attempt_id: "attempt".into(),
        base_revision: 0, patch_count: MAX_TURN_PROJECTION_HEAD_PATCHES, patch_bytes: 64,
        text_tail: None, frame_witness: None }
}

fn fixture(extra: usize) -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut db = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    db.set_write_overlay_for_test(true);
    let mut tx = db.begin(7, 11).unwrap();
    let key = projection_checkpoint_key(&tx, "conversation", "turn", "head").unwrap();
    db.entity_put(&mut tx, key, b"checkpoint".to_vec()).unwrap();
    for index in 1..=MAX_TURN_PROJECTION_HEAD_PATCHES + extra {
        let key = projection_patch_key(&tx, "conversation", "turn", "head", index as u64).unwrap();
        db.entity_put(&mut tx, key, b"patch".to_vec()).unwrap();
    }
    let key = EntityKey::new(7, 11, "unrelated", b"pending").unwrap();
    db.entity_put(&mut tx, key, b"keep".to_vec()).unwrap();
    db.commit(tx).unwrap();
    let mut other = db.begin(7, 12).unwrap();
    let key = projection_checkpoint_key(&other, "conversation", "turn", "head").unwrap();
    db.entity_put(&mut other, key, b"other owner".to_vec()).unwrap();
    db.commit(other).unwrap();
    (vfs, db)
}

type Retirement = fn(&AuthorityDatabase, &mut AuthorityTransaction, &str, &str,
    Option<&ProjectionHeadDescriptor>) -> io::Result<()>;

/// Event settlement and single-Turn projection updates retire a head the same
/// bounded way; only updates also weigh the transaction's overlay envelope.
const RETIREMENTS: [(&str, Retirement); 2] =
    [("event", retire_event_projection_head), ("update", retire_updated_projection_head)];

fn retire_with(db: &AuthorityDatabase, retirement: Retirement) -> AuthorityTransaction {
    let mut tx = db.begin(7, 11).unwrap();
    retirement(db, &mut tx, "conversation", "turn", Some(&head())).unwrap();
    let key = EntityKey::new(7, 11, "settlement", b"done").unwrap();
    db.entity_put(&mut tx, key, b"yes".to_vec()).unwrap();
    tx
}

fn head_count(db: &AuthorityDatabase, owner: u64) -> usize {
    let mut tx = db.begin(7, owner).unwrap();
    let (start, end) = projection_head_range(&tx, "conversation", "turn", "head").unwrap();
    db.entity_scan(&mut tx, &start, &end, 100).unwrap().len()
}

#[test]
fn bounded_retirement_preserves_unmerged_debt_and_owner_isolation() {
    for (name, retirement) in RETIREMENTS {
        let (_, mut db) = fixture(0);
        let replica = db.build_read_replica().unwrap();
        let before = db.entity_overlay_entries_for_test();
        let tx = retire_with(&db, retirement);
        db.commit(tx).unwrap();
        assert_eq!(db.entity_overlay_entries_for_test(), before + 66, "{name}");
        assert_eq!(head_count(&db, 11), 0, "{name}");
        assert_eq!(head_count(&db, 12), 1, "{name}");
        assert_eq!(head_count(&replica, 11), 65, "{name}");
    }
}

#[test]
fn unexpected_oversized_head_keeps_complete_structural_retirement() {
    for (name, retirement) in RETIREMENTS {
        let (_, mut db) = fixture(2);
        let tx = retire_with(&db, retirement);
        db.commit(tx).unwrap();
        assert_eq!(head_count(&db, 11), 0, "{name}");
        assert_eq!(head_count(&db, 12), 1, "{name}");
    }
}

#[test]
fn tombstones_the_overlay_envelope_cannot_carry_keep_structural_retirement() {
    let (_, mut db) = fixture(0);
    let mut tx = db.begin(7, 11).unwrap();
    let checkpoint = projection_checkpoint_key(&tx, "conversation", "turn", "head").unwrap();
    let tombstones = (1..=MAX_TURN_PROJECTION_HEAD_PATCHES).map(|index| {
        projection_patch_key(&tx, "conversation", "turn", "head", index as u64).unwrap()
    }).chain([checkpoint])
        .map(|key| crate::entity::overlay_write_bytes(key.encoded().len(), None)).sum::<usize>();
    // Leave the delta some room, but less than the head's tombstones need.
    let filler = |index: usize| EntityKey::new(7, 11, "filler", format!("{index:04}").as_bytes()).unwrap();
    let row_bytes = |value: usize| crate::entity::overlay_write_bytes(filler(0).encoded().len(), Some(value));
    let mut index = 0;
    while db.entity_overlay_delta_room(&tx) > tombstones + row_bytes(4_000) {
        db.entity_put(&mut tx, filler(index), vec![5; 4_000]).unwrap();
        index += 1;
    }
    let value = db.entity_overlay_delta_room(&tx) - tombstones / 2 - row_bytes(0);
    db.entity_put(&mut tx, filler(index), vec![6; value]).unwrap();
    let room = db.entity_overlay_delta_room(&tx);
    assert!(room > 0 && room < tombstones, "room {room}, tombstones {tombstones}");
    retire_updated_projection_head(&db, &mut tx, "conversation", "turn", Some(&head())).unwrap();
    assert_eq!(db.entity_overlay_delta_room(&tx), room, "a structural retirement adds no delta bytes");
    db.commit(tx).unwrap();
    assert_eq!(head_count(&db, 11), 0);
    assert_eq!(head_count(&db, 12), 1);
}

#[test]
fn retirement_conflicts_with_a_concurrent_projection_patch() {
    for (name, retirement) in RETIREMENTS {
        let (_, mut db) = fixture(0);
        let stale = retire_with(&db, retirement);
        let mut insertion = db.begin(7, 11).unwrap();
        let key = projection_patch_key(&insertion, "conversation", "turn", "head", 65).unwrap();
        db.entity_put(&mut insertion, key, b"concurrent".to_vec()).unwrap();
        db.commit(insertion).unwrap();
        assert_eq!(db.commit(stale).unwrap_err().kind(), io::ErrorKind::WouldBlock, "{name}");
        assert_eq!(head_count(&db, 11), 66, "{name}");
    }
}

#[test]
fn retirement_and_settlement_are_atomic_at_every_commit_io_fault() {
    for (name, retirement) in RETIREMENTS {
        let (vfs, mut baseline) = fixture(0);
        let tx = retire_with(&baseline, retirement);
        vfs.arm_fault(None).unwrap();
        baseline.commit(tx).unwrap();
        let trace = vfs.trace().unwrap();
        assert!(!trace.is_empty(), "{name}");
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
            for action in actions {
                let (vfs, mut db) = fixture(0);
                let tx = retire_with(&db, retirement);
                vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
                let acknowledged = db.commit(tx).is_ok();
                // Capture durable state before Drop can flush or repair it.
                let durable = Arc::new(vfs.fork_durable().unwrap());
                let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
                let mut read = reopened.begin(7, 11).unwrap();
                let marker = EntityKey::new(7, 11, "settlement", b"done").unwrap();
                let settled = reopened.entity_get(&mut read, &marker).unwrap().is_some();
                assert_eq!(head_count(&reopened, 11), if settled { 0 } else { 65 }, "{name} fault {index}");
                assert!(!acknowledged || settled, "{name} lost acknowledged settlement at {index}");
                assert_eq!(head_count(&reopened, 12), 1, "{name}");
                let pending = EntityKey::new(7, 11, "unrelated", b"pending").unwrap();
                assert_eq!(reopened.entity_get(&mut read, &pending).unwrap(), Some(b"keep".to_vec()), "{name}");
            }
        }
    }
}
