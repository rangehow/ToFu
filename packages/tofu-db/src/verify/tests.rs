//! Verify/salvage unit tests (real tempdirs: the commands are offline
//! operators over on-disk state).

use super::*;
use crate::authority::AuthorityDatabase;
use crate::engine::Engine;
use crate::entity::EntityKey;

fn authority_with_two_payloads() -> (tempfile::TempDir, AuthorityDatabase, BlockId, BlockId) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let first = database
        .commit_block_payload_for_test(b"verify-payload-one")
        .unwrap();
    let second = database
        .commit_block_payload_for_test(b"verify-payload-two")
        .unwrap();
    let key = EntityKey::new(7, 11, "record", b"salvage-key").unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key, b"salvage-value".to_vec())
        .unwrap();
    database.commit(transaction).unwrap();
    database.checkpoint_for_test().unwrap();
    (directory, database, first, second)
}

fn authority_with_live_and_orphan() -> (tempfile::TempDir, AuthorityDatabase, BlockId, BlockId) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let live = database
        .commit_block_payload_for_test(b"verify-live-payload")
        .unwrap();
    let orphan = database
        .write_orphan_block_for_test(b"verify-orphan-payload")
        .unwrap();
    database.checkpoint_for_test().unwrap();
    (directory, database, live, orphan)
}

fn loose_block_path(root: &Path, id: BlockId) -> std::path::PathBuf {
    let hex = id.to_hex();
    root.join("blocks")
        .join(&hex[..2])
        .join(format!("{hex}.blk"))
}

fn corrupt_file_byte(path: &Path) {
    let mut bytes = std::fs::read(path).unwrap();
    let middle = bytes.len() / 2;
    bytes[middle] ^= 0x5a;
    std::fs::write(path, bytes).unwrap();
}

fn drop_engine(database: AuthorityDatabase) {
    drop(database);
}

/// Every path below `root` with its kind and content digest: equal snapshots
/// mean nothing was created, removed, renamed or rewritten.
fn tree_snapshot(root: &Path) -> std::collections::BTreeMap<String, String> {
    use sha2::Digest;
    let mut snapshot = std::collections::BTreeMap::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in std::fs::read_dir(&directory).unwrap() {
            let path = entry.unwrap().path();
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            if path.is_dir() {
                snapshot.insert(name, "directory".to_owned());
                pending.push(path);
            } else {
                let bytes = std::fs::read(&path).unwrap();
                snapshot.insert(
                    name,
                    format!("{}:{:x}", bytes.len(), sha2::Sha256::digest(&bytes)),
                );
            }
        }
    }
    snapshot
}

/// Committed deltas still pending in the write overlay and the WAL tail: the
/// state the merge phase completes.
fn authority_with_pending_overlay() -> (tempfile::TempDir, BlockId) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_for_test(true);
    let payload = database
        .commit_block_payload_for_test(b"verify-overlay-payload")
        .unwrap();
    for index in 0..16_u32 {
        let key = EntityKey::new(7, 11, "record", format!("overlay-{index}").as_bytes()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut transaction, key, vec![index as u8; 64])
            .unwrap();
        database.commit(transaction).unwrap();
    }
    drop_engine(database);
    (directory, payload)
}

fn differences(
    before: &std::collections::BTreeMap<String, String>,
    after: &std::collections::BTreeMap<String, String>,
) -> Vec<String> {
    let mut changed: Vec<String> = before
        .iter()
        .filter(|(path, digest)| after.get(*path) != Some(*digest))
        .map(|(path, _)| format!("changed or removed: {path}"))
        .collect();
    changed.extend(
        after
            .keys()
            .filter(|path| !before.contains_key(*path))
            .map(|path| format!("created: {path}")),
    );
    changed
}

#[test]
fn verify_leaves_every_file_of_an_authority_with_a_pending_overlay_unchanged() {
    let (directory, payload) = authority_with_pending_overlay();
    let before = tree_snapshot(directory.path());
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "ok", "{report:?}");
    assert!(report
        .performance
        .iter()
        .any(|phase| phase.phase == "merge"));
    assert!(report.graph.pages_verified >= 1, "{report:?}");
    let changed = differences(&before, &tree_snapshot(directory.path()));
    assert!(
        changed.is_empty(),
        "verify wrote to the authority: {changed:?}"
    );
    // The authority still opens as a writer and serves what it committed.
    let reopened = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(
        reopened.read_block_for_test(payload).unwrap(),
        b"verify-overlay-payload".to_vec()
    );
    let mut read = reopened.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "record", b"overlay-15").unwrap();
    assert_eq!(
        reopened.entity_get(&mut read, &key).unwrap(),
        Some(vec![15_u8; 64])
    );
}

#[test]
fn verify_reads_an_authority_it_has_no_permission_to_write() {
    // SAFETY: geteuid has no preconditions and only reads process state.
    if unsafe { libc::geteuid() } == 0 {
        return; // Permission bits do not bind root.
    }
    use std::os::unix::fs::PermissionsExt;
    let (directory, _) = authority_with_pending_overlay();
    let set_modes = |file_mode: u32, directory_mode: u32| {
        let mut paths: Vec<std::path::PathBuf> = tree_snapshot(directory.path())
            .keys()
            .map(|name| directory.path().join(name))
            .collect();
        paths.push(directory.path().to_path_buf());
        // Deepest first, so a directory's entries stay reachable until its turn.
        paths.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        for path in paths {
            let mode = if path.is_dir() {
                directory_mode
            } else {
                file_mode
            };
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        }
    };
    set_modes(0o444, 0o555);
    let report = verify_authority(directory.path());
    set_modes(0o644, 0o755);
    let report = report.unwrap();
    assert_eq!(report.verdict, "ok", "{report:?}");
}

#[test]
fn verify_refuses_an_authority_a_writer_holds_and_writes_nothing() {
    let (directory, _) = authority_with_pending_overlay();
    let writer = AuthorityDatabase::open(directory.path()).unwrap();
    let before = tree_snapshot(directory.path());
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "fatal", "{report:?}");
    assert!(
        report
            .graph
            .fatal
            .as_deref()
            .is_some_and(|fatal| fatal.contains("owns the data directory")),
        "{report:?}"
    );
    assert!(differences(&before, &tree_snapshot(directory.path())).is_empty());
    drop(writer);
}

#[test]
fn verify_reports_ok_for_a_healthy_authority() {
    let (directory, database, _, _) = authority_with_two_payloads();
    drop_engine(database);
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "ok");
    assert_eq!(
        report
            .performance
            .iter()
            .map(|phase| phase.phase)
            .collect::<Vec<_>>(),
        vec!["control", "wal", "open", "merge", "payload", "loose", "graph", "history", "close"]
    );
    assert!(report.performance.iter().all(|phase| phase.elapsed_ns > 0));
    assert!(report.graph.pages_verified >= 1, "{report:?}");
    assert!(report.graph.history_blocks_verified >= 1);
    assert_eq!(report.graph.missing_block_ids.len(), 0);
    assert_eq!(report.payload.failed_segment_ids.len(), 0);
}

#[test]
fn verify_reports_degraded_with_exact_corrupt_block() {
    let (directory, database, live, orphan) = authority_with_live_and_orphan();
    drop_engine(database);
    corrupt_file_byte(&loose_block_path(directory.path(), orphan));
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "degraded");
    assert!(
        report
            .payload
            .corrupt_block_ids
            .iter()
            .any(|lost| lost.starts_with(&orphan.to_hex())),
        "corrupted block must be named: {:?}",
        report.payload.corrupt_block_ids
    );
    assert!(
        !report
            .payload
            .corrupt_block_ids
            .iter()
            .any(|lost| lost.starts_with(&live.to_hex())),
        "the intact payload must not be blamed"
    );
}

#[test]
fn salvage_after_a_torn_wal_tail_restores_the_complete_prefix() {
    let (directory, database, first, second) = authority_with_two_payloads();
    drop_engine(database);
    // A torn tail: garbage bytes after the last valid record of the
    // current generation (never below the durable witness).
    {
        use std::io::Write;
        let mut wal = std::fs::OpenOptions::new()
            .append(true)
            .open(directory.path().join("active-0000000000000002.wal"))
            .unwrap();
        wal.write_all(b"torn-tail-garbage").unwrap();
    }
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "degraded");
    assert!(report.wal.truncated_tail_bytes > 0);
    let target = tempfile::tempdir().unwrap();
    let target_path = target.path().join("salvage");
    let salvage = salvage_authority(directory.path(), &target_path).unwrap();
    let restored_dir = tempfile::tempdir().unwrap();
    let restored_path = restored_dir.path().join("restored");
    std::fs::create_dir(&restored_path).unwrap();
    let manifest = crate::backup::restore_latest_backup(&target_path, &restored_path).unwrap();
    assert_eq!(manifest.durable_sequence, salvage.durable_sequence);
    let restored = AuthorityDatabase::open(&restored_path).unwrap();
    assert_eq!(
        restored.read_block_for_test(first).unwrap(),
        b"verify-payload-one".to_vec()
    );
    assert_eq!(
        restored.read_block_for_test(second).unwrap(),
        b"verify-payload-two".to_vec()
    );
}

#[test]
fn salvage_refuses_fatally_damaged_control() {
    let (directory, database, _, _) = authority_with_two_payloads();
    drop_engine(database);
    std::fs::write(directory.path().join("CONTROL"), vec![0x5a; 8192]).unwrap();
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "fatal");
    let target = tempfile::tempdir().unwrap();
    let error = salvage_authority(directory.path(), &target.path().join("salvage")).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidData);
}

#[test]
fn verify_fatal_control_retains_failed_phase_without_inventing_later_work() {
    let directory = tempfile::tempdir().unwrap();
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "fatal");
    assert_eq!(report.performance.len(), 1);
    assert_eq!(report.performance[0].phase, "control");
    assert!(report.performance[0].elapsed_ns > 0);
    assert_eq!(report.performance[0].history_decode_attempts, 0);
}

fn sealed_generation_authority() -> (tempfile::TempDir, crate::control::ControlState) {
    let directory = tempfile::tempdir().unwrap();
    let mut engine = Engine::initialize(directory.path()).unwrap();
    engine.attach_rotation_driver();
    engine
        .commit_transaction(b"prefix", &[b"prefix carried bytes"])
        .unwrap();
    let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
        crate::engine::RotationCheckpointPreparation::Ready(snapshot) => snapshot,
        other => panic!("expected sealed generation: {other:?}"),
    };
    engine
        .commit_transaction(b"suffix", &[b"suffix carried bytes"])
        .unwrap();
    drop(engine);
    drop(snapshot);
    let state = ControlFile::open(directory.path())
        .unwrap()
        .read_current()
        .unwrap();
    assert!(state.sealed_log.is_some());
    (directory, state)
}

#[test]
fn verify_accepts_the_complete_control_selected_sealed_wal_chain() {
    let (directory, state) = sealed_generation_authority();
    let mut active = ActiveLog::open_generation_with_vfs(
        directory.path(),
        state.active_log_generation,
        std::sync::Arc::new(crate::vfs::RealVfs),
    )
    .unwrap();
    // This was the old verifier's false fatal: successor hashes start at the
    // sealed terminal witness, not the earlier checkpoint witness.
    assert!(active
        .scan(
            state.checkpoint_sequence,
            state.checkpoint_hash,
            state.durable_sequence
        )
        .is_err());
    let selected = crate::wal::scan_selected_generation_chain(
        directory.path(),
        &state,
        &mut active,
        std::sync::Arc::new(crate::vfs::RealVfs),
    )
    .unwrap();
    assert_eq!(selected.predecessor_record_count, 1);
    assert_eq!(selected.recovery.records.len(), 2);
    assert_eq!(
        selected.recovery.records[1].sequence,
        state.durable_sequence
    );
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "ok", "{report:?}");
    assert_eq!(report.wal.valid_records, 2);
    assert_eq!(report.wal.valid_bytes, selected.valid_bytes);
}

#[test]
fn verify_rejects_corrupt_sealed_and_acknowledged_successor_without_truncating() {
    for sealed in [true, false] {
        let (directory, state) = sealed_generation_authority();
        let generation = if sealed {
            state.sealed_log.as_ref().unwrap().generation
        } else {
            state.active_log_generation
        };
        let path = crate::wal::active_log_path(directory.path(), generation).unwrap();
        corrupt_file_byte(&path);
        let original = std::fs::read(&path).unwrap();
        let report = verify_authority(directory.path()).unwrap();
        assert_eq!(report.verdict, "fatal", "{report:?}");
        assert!(report.wal.fatal.is_some());
        assert_eq!(std::fs::read(&path).unwrap(), original);
    }
}

fn overflow_block_path(root: &Path, id: BlockId) -> std::path::PathBuf {
    let hex = id.to_hex();
    root.join(crate::block::OVERFLOW_DIRECTORY)
        .join(&hex[..2])
        .join(&hex[2..4])
        .join(format!("{hex}.blk"))
}

/// Moves every loose block where a capped shard writes it; each shard's
/// directory under `blocks-overflow` marks it overflowed for the next open.
fn relocate_every_loose_block_to_overflow(root: &Path) -> Vec<BlockId> {
    let mut moved = Vec::new();
    for shard in std::fs::read_dir(root.join("blocks")).unwrap() {
        for entry in std::fs::read_dir(shard.unwrap().path()).unwrap() {
            let path = entry.unwrap().path();
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            let Some(hex) = name.strip_suffix(".blk") else {
                continue;
            };
            let mut bytes = [0_u8; 32];
            for (index, byte) in bytes.iter_mut().enumerate() {
                *byte = u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16).unwrap();
            }
            let id = BlockId(bytes);
            let target = overflow_block_path(root, id);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            std::fs::rename(&path, target).unwrap();
            moved.push(id);
        }
    }
    moved
}

/// Blocks a capped shard put in `blocks-overflow` (docs/STORAGE_OPERATIONS.md,
/// "Loose shards past a per-directory entry cap") belong to the loose universe
/// verify walks and to what salvage extracts.
#[test]
fn verify_and_salvage_cover_overflowed_blocks() {
    let (directory, database, live, orphan) = authority_with_live_and_orphan();
    drop_engine(database);
    let moved = relocate_every_loose_block_to_overflow(directory.path());
    assert!(moved.contains(&orphan) && moved.len() > 1, "{moved:?}");
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "ok", "{report:?}");
    assert_eq!(report.graph.missing_block_ids.len(), 0, "{report:?}");

    let target = tempfile::tempdir().unwrap();
    let target_path = target.path().join("salvage");
    let salvage = salvage_authority(directory.path(), &target_path).unwrap();
    let restored_dir = tempfile::tempdir().unwrap();
    let restored_path = restored_dir.path().join("restored");
    std::fs::create_dir(&restored_path).unwrap();
    let manifest = crate::backup::restore_latest_backup(&target_path, &restored_path).unwrap();
    assert_eq!(manifest.durable_sequence, salvage.durable_sequence);
    let restored = AuthorityDatabase::open(&restored_path).unwrap();
    assert_eq!(
        restored.read_block_for_test(live).unwrap(),
        b"verify-live-payload".to_vec()
    );
    drop(restored);

    corrupt_file_byte(&overflow_block_path(directory.path(), orphan));
    let report = verify_authority(directory.path()).unwrap();
    assert_eq!(report.verdict, "degraded", "{report:?}");
    assert!(
        report
            .payload
            .corrupt_block_ids
            .iter()
            .any(|lost| lost.starts_with(&orphan.to_hex())),
        "the corrupt overflowed copy must be named: {:?}",
        report.payload.corrupt_block_ids
    );
}
