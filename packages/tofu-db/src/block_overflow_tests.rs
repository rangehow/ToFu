//! Loose shards past a per-directory entry cap (docs/STORAGE_OPERATIONS.md,
//! "Loose shards past a per-directory entry cap"): a name the primary shard
//! refuses overflows that hash shard into `blocks-overflow/<xx>/<yy>/`,
//! durably, and every write, read, listing and deletion of the shard then
//! uses both locations.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

const DATA: &str = "/data";
/// The on-disk name is a contract with operators and older engines.
const OVERFLOW: &str = "blocks-overflow";
const SHARD: u8 = 0xc6;

fn store(vfs: &DeterministicVfs) -> BlockStore {
    vfs.create_dir(Path::new(DATA)).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let store = BlockStore::initialize_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    store.set_cache_capacity_bytes(0).unwrap();
    store
}

fn reopen(vfs: &DeterministicVfs) -> BlockStore {
    let store = BlockStore::open_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    store.set_cache_capacity_bytes(0).unwrap();
    store
}

/// Distinct payloads whose block IDs all fall in `shard`.
fn payloads_in_shard(shard: u8, count: usize, tag: &str) -> Vec<Vec<u8>> {
    (0_u64..)
        .map(|index| format!("{tag} {index}").into_bytes())
        .filter(|payload| BlockId::for_payload(payload).0[0] == shard)
        .take(count)
        .collect()
}

fn primary_shard(shard: u8) -> PathBuf {
    Path::new(DATA).join("blocks").join(format!("{shard:02x}"))
}

fn primary_file(id: BlockId) -> PathBuf {
    primary_shard(id.0[0]).join(format!("{}.blk", id.to_hex()))
}

fn overflow_leaf(id: BlockId) -> PathBuf {
    let hexadecimal = id.to_hex();
    Path::new(DATA).join(OVERFLOW).join(&hexadecimal[..2]).join(&hexadecimal[2..4])
}

fn overflow_file(id: BlockId) -> PathBuf {
    overflow_leaf(id).join(format!("{}.blk", id.to_hex()))
}

fn names(vfs: &DeterministicVfs, directory: &Path) -> Vec<String> {
    vfs.read_directory(directory)
        .unwrap()
        .iter()
        .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
        .collect()
}

fn listed(store: &BlockStore, shard: u8) -> Vec<BlockId> {
    let mut found = Vec::new();
    store
        .visit_block_shards(|byte, ids| {
            if byte == shard {
                found = ids;
            }
            Ok(())
        })
        .unwrap();
    found
}

/// No temporary name survives in any primary shard or overflow leaf.
fn assert_no_temporary_names(vfs: &DeterministicVfs) {
    let mut directories: Vec<PathBuf> = vfs.read_directory(Path::new("/data/blocks")).unwrap();
    if vfs.metadata(&Path::new(DATA).join(OVERFLOW)).is_ok() {
        for shard in vfs.read_directory(&Path::new(DATA).join(OVERFLOW)).unwrap() {
            directories.extend(vfs.read_directory(&shard).unwrap());
        }
    }
    for directory in directories {
        for name in names(vfs, &directory) {
            assert!(!name.starts_with(".new-"), "temporary {name} in {}", directory.display());
        }
    }
}

#[test]
fn a_refused_name_overflows_its_shard_durably_and_the_block_reads_back_after_reopen() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    let payloads = payloads_in_shard(SHARD, 4, "overflow");
    let first = store.put(&payloads[0]).unwrap();
    assert_eq!(vfs.metadata(&primary_file(first)).unwrap(), FileKind::File);
    vfs.cap_directory_entries(&primary_shard(SHARD), 1).unwrap();
    let second = store.put(&payloads[1]).unwrap();
    assert_eq!(vfs.metadata(&overflow_file(second)).unwrap(), FileKind::File);
    assert_eq!(names(&vfs, &primary_shard(SHARD)).len(), 1, "nothing new in the capped shard");
    // The shard stays overflowed once room is back: no retry of the primary.
    vfs.cap_directory_entries(&primary_shard(SHARD), usize::MAX).unwrap();
    let third = store.put(&payloads[2]).unwrap();
    assert_eq!(vfs.metadata(&overflow_file(third)).unwrap(), FileKind::File);
    drop(store);
    vfs.crash().unwrap();
    let reopened = reopen(&vfs);
    let written = [first, second, third];
    for (payload, id) in payloads.iter().zip(written) {
        assert_eq!(&reopened.get(id).unwrap(), payload);
        assert!(reopened.retain_held_block(id).unwrap());
        assert_eq!(reopened.block_file_bytes(id).unwrap(), (HEADER_BYTES + payload.len() + FOOTER_BYTES) as u64);
    }
    let mut expected = written.to_vec();
    expected.sort_unstable();
    assert_eq!(listed(&reopened, SHARD), expected);
    // The mark is durable: after the reopen the shard still writes overflow.
    let fourth = reopened.put(&payloads[3]).unwrap();
    assert_eq!(vfs.metadata(&overflow_file(fourth)).unwrap(), FileKind::File);
    assert_eq!(names(&vfs, &primary_shard(SHARD)).len(), 1);
}

#[test]
fn a_refused_publishing_rename_overflows_and_leaves_no_temporary_name() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    let payloads = payloads_in_shard(SHARD, 2, "rename");
    let first = store.put(&payloads[0]).unwrap();
    // Room for the temporary file, none for the name it is renamed to.
    vfs.cap_directory_entries(&primary_shard(SHARD), 2).unwrap();
    let second = store.put(&payloads[1]).unwrap();
    assert_eq!(vfs.metadata(&overflow_file(second)).unwrap(), FileKind::File);
    assert_eq!(names(&vfs, &primary_shard(SHARD)), vec![format!("{}.blk", first.to_hex())]);
    drop(store);
    vfs.crash().unwrap();
    assert_eq!(reopen(&vfs).get(second).unwrap(), payloads[1]);
    assert_no_temporary_names(&vfs);
}

#[test]
fn a_batch_overflows_only_the_shards_that_refuse() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    vfs.cap_directory_entries(&primary_shard(SHARD), 0).unwrap();
    let mut payloads = payloads_in_shard(SHARD, 3, "batch capped");
    payloads.extend((0..40).map(|index| format!("batch free {index}").into_bytes()));
    let mut staged = BTreeSet::new();
    for payload in &payloads {
        let (id, pending) = store.put_deferred_fresh(payload).unwrap();
        assert!(pending);
        staged.insert(id);
    }
    store.flush_pending_subset(&staged).unwrap();
    for payload in &payloads {
        let id = BlockId::for_payload(payload);
        let (expected, unexpected) = if id.0[0] == SHARD {
            (overflow_file(id), primary_file(id))
        } else {
            (primary_file(id), overflow_file(id))
        };
        assert_eq!(vfs.metadata(&expected).unwrap(), FileKind::File);
        assert_eq!(vfs.metadata(&unexpected).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
    drop(store);
    vfs.crash().unwrap();
    let reopened = reopen(&vfs);
    for payload in &payloads {
        assert_eq!(&reopened.get(BlockId::for_payload(payload)).unwrap(), payload);
    }
    assert_no_temporary_names(&vfs);
}

/// A copy (backup closed store, restore target) publishes without a barrier;
/// `sync_copy_shard` must then cover the overflow leaf it renamed into.
#[test]
fn a_copied_block_overflows_and_its_shard_barrier_covers_the_overflow_leaf() {
    for barrier in [false, true] {
        let vfs = DeterministicVfs::new(None);
        let store = store(&vfs);
        vfs.cap_directory_entries(&primary_shard(SHARD), 0).unwrap();
        let payload = payloads_in_shard(SHARD, 1, "copy").remove(0);
        let id = BlockId::for_payload(&payload);
        store.ensure_copy_shard(id).unwrap();
        assert!(store.publish_copied_block_unbarriered(id, &payload).unwrap());
        assert!(store.retain_held_block(id).unwrap());
        if barrier {
            store.sync_copy_shard(BlockId([SHARD; 32])).unwrap();
        }
        drop(store);
        vfs.crash().unwrap();
        let reopened = reopen(&vfs);
        assert_eq!(reopened.get(id).is_ok(), barrier, "barrier={barrier}");
    }
}

#[test]
fn deletion_removes_every_copy_in_both_locations_durably() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    let payloads = payloads_in_shard(SHARD, 3, "delete");
    let first = store.put(&payloads[0]).unwrap();
    vfs.cap_directory_entries(&primary_shard(SHARD), 1).unwrap();
    let second = store.put(&payloads[1]).unwrap();
    let third = store.put(&payloads[2]).unwrap();
    // A racing writer can leave one block in both locations.
    let bytes = vfs
        .open(&primary_file(first), OpenRequest { read: true, ..OpenRequest::default() })
        .unwrap()
        .read_all(MAX_BLOCK_FILE_BYTES as usize)
        .unwrap();
    if vfs.metadata(&overflow_leaf(first)).is_err() {
        vfs.create_dir(&overflow_leaf(first)).unwrap();
    }
    let mut duplicate = vfs
        .open(&overflow_file(first), OpenRequest { write: true, create_new: true, ..OpenRequest::default() })
        .unwrap();
    duplicate.write_all_at(0, &bytes).unwrap();
    duplicate.sync_all().unwrap();
    drop(duplicate);
    vfs.sync_directory(&overflow_leaf(first)).unwrap();
    assert_eq!(listed(&store, SHARD).iter().filter(|id| **id == first).count(), 1);

    let metrics = store.remove_blocks(&[first, second]).unwrap();
    assert_eq!(metrics.blocks_removed, 2);
    for id in [first, second] {
        assert_eq!(vfs.metadata(&primary_file(id)).unwrap_err().kind(), io::ErrorKind::NotFound);
        assert_eq!(vfs.metadata(&overflow_file(id)).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
    let mut absent = Vec::new();
    assert!(matches!(
        store.remove_block_noting_absent(third, &mut absent).unwrap(),
        LooseRemoval::Unlinked { .. }
    ));
    assert_eq!(absent, vec![third]);
    store.sync_block_shard(third).unwrap();
    drop(store);
    vfs.crash().unwrap();
    let reopened = reopen(&vfs);
    for id in [first, second, third] {
        assert_eq!(reopened.get(id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }
    assert!(listed(&reopened, SHARD).is_empty());
}

#[test]
fn temporary_names_in_overflow_leaves_are_planned_and_removed() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    vfs.cap_directory_entries(&primary_shard(SHARD), 0).unwrap();
    let id = store.put(&payloads_in_shard(SHARD, 1, "temporary").remove(0)).unwrap();
    let stray = overflow_leaf(id).join(temporary_block_file_name(Uuid::from_bytes([0x5a; 16])));
    let mut file = vfs
        .open(&stray, OpenRequest { write: true, create_new: true, ..OpenRequest::default() })
        .unwrap();
    file.write_all_at(0, b"torn").unwrap();
    drop(file);
    let candidate = store.plan_temporary_file().unwrap().candidate.expect("the stray file is planned");
    assert_eq!(candidate.path, stray);
    assert_eq!(store.remove_temporary_file(&candidate).unwrap(), 4);
    assert_eq!(vfs.metadata(&stray).unwrap_err().kind(), io::ErrorKind::NotFound);
    assert_eq!(store.get(id).unwrap().len(), payloads_in_shard(SHARD, 1, "temporary")[0].len());
}

#[test]
fn only_name_refusals_overflow_a_shard() {
    for (kind, overflows) in [(io::ErrorKind::StorageFull, true), (io::ErrorKind::PermissionDenied, false)] {
        let vfs = DeterministicVfs::new(None);
        let store = store(&vfs);
        let payload = payloads_in_shard(SHARD, 1, "refusal").remove(0);
        // The first open of a missing block's put is its temporary file.
        vfs.arm_operation_fault(Operation::Open, 1, FaultAction::ErrorBefore(kind)).unwrap();
        let outcome = store.put(&payload);
        assert!(vfs.operation_fault_fired().unwrap());
        let overflow_root = Path::new(DATA).join(OVERFLOW);
        if overflows {
            let id = outcome.unwrap();
            assert_eq!(vfs.metadata(&overflow_file(id)).unwrap(), FileKind::File);
        } else {
            assert_eq!(outcome.unwrap_err().kind(), kind);
            assert_eq!(vfs.metadata(&overflow_root).unwrap_err().kind(), io::ErrorKind::NotFound);
        }
    }
}

#[test]
fn a_store_that_never_meets_a_refusal_keeps_the_original_layout() {
    let vfs = DeterministicVfs::new(None);
    let store = store(&vfs);
    let mut staged = BTreeSet::new();
    for index in 0..64 {
        let payload = format!("layout {index}").into_bytes();
        if index % 2 == 0 {
            store.put(&payload).unwrap();
        } else {
            staged.insert(store.put_deferred_fresh(&payload).unwrap().0);
        }
    }
    store.flush_pending_subset(&staged).unwrap();
    drop(store);
    reopen(&vfs);
    assert_eq!(names(&vfs, Path::new(DATA)).iter().filter(|name| name.as_str() == OVERFLOW).count(), 0);
}

/// Every operation of the first overflow can fail or lose its sync: the store
/// still opens, the earlier block stays intact, a reported success survives
/// power loss, and a retry publishes the block durably.
#[test]
fn every_failure_during_the_first_overflow_leaves_a_consistent_store() {
    let mut exercised = 0;
    for (operation, action) in [
        (None, FaultAction::ErrorBefore(io::ErrorKind::Other)),
        (Some(Operation::SyncDirectory), FaultAction::DropSync),
        (Some(Operation::SyncAll), FaultAction::DropSync),
    ] {
        for step in 1_usize.. {
            let vfs = DeterministicVfs::new(None);
            let store = store(&vfs);
            let payloads = payloads_in_shard(SHARD, 2, "first overflow");
            let first = store.put(&payloads[0]).unwrap();
            vfs.cap_directory_entries(&primary_shard(SHARD), 1).unwrap();
            match operation {
                None => vfs.arm_fault(Some(FaultRule { operation_number: step as u64, action })).unwrap(),
                Some(operation) => vfs.arm_operation_fault(operation, step, action).unwrap(),
            }
            let outcome = store.put(&payloads[1]);
            let fired = match operation {
                None => vfs.trace().unwrap().len() >= step,
                Some(_) => vfs.operation_fault_fired().unwrap(),
            };
            if !fired {
                outcome.unwrap();
                break;
            }
            exercised += 1;
            drop(store);
            vfs.crash().unwrap();
            let reopened = reopen(&vfs);
            assert_eq!(reopened.get(first).unwrap(), payloads[0], "{operation:?} step {step}");
            let second = BlockId::for_payload(&payloads[1]);
            if outcome.is_ok() {
                assert_eq!(reopened.get(second).unwrap(), payloads[1], "{operation:?} step {step}");
            }
            listed(&reopened, SHARD);
            assert_eq!(reopened.put(&payloads[1]).unwrap(), second);
            while let Some(candidate) = reopened.plan_temporary_file().unwrap().candidate {
                reopened.remove_temporary_file(&candidate).unwrap();
            }
            drop(reopened);
            vfs.crash().unwrap();
            let retried = reopen(&vfs);
            assert_eq!(retried.get(second).unwrap(), payloads[1], "{operation:?} step {step}");
            assert_eq!(retried.get(first).unwrap(), payloads[0]);
            assert_no_temporary_names(&vfs);
        }
    }
    assert!(exercised > 10, "the campaign must reach the overflow operations ({exercised})");
}
