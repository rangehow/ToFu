//! Catalog publication on the commit path while the packed filter is unbuilt
//! or building (docs/TOFUDB_BLOCK_READ_PATH.md, "Packed filter built off the
//! authority mutex"). Live 2026-11-09 the first catalog publication after a
//! clean restart was a foreground commit's spill, and its install scanned all
//! 4,096 segment indexes into the filter inside a 68.2 s authority hold.
use super::*;
use crate::block::{BlockId, BlockStore};
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::read_path_counters;
use crate::vfs::DeterministicVfs;
use std::collections::BTreeMap;
use std::sync::mpsc;

const CATALOGUED_SEGMENTS: usize = 16;
const SPILL_ROWS: u32 = 768;

fn key(name: &str) -> EntityKey {
    EntityKey::new(7, 11, "semantic", name.as_bytes()).unwrap()
}

fn spill_value(label: &str, row: u32) -> Vec<u8> {
    let mut value = vec![(row % 251) as u8; 4096];
    value[..4].copy_from_slice(&row.to_be_bytes());
    value[4..4 + label.len().min(32)].copy_from_slice(&label.as_bytes()[..label.len().min(32)]);
    value
}

/// One commit whose fresh copy-on-write pages exceed the WAL carry ceiling,
/// so its pages are packed into a segment and a catalog is published.
fn spill(database: &mut AuthorityDatabase, label: &str) -> io::Result<()> {
    let mut transaction = database.begin(7, 11)?;
    for row in 0..SPILL_ROWS {
        database.entity_put(&mut transaction, key(&format!("{label}-{row:05}")), spill_value(label, row))?;
    }
    database.commit(transaction).map(|_| ())
}

fn assert_spilled(database: &AuthorityDatabase, label: &str) {
    let mut inspect = database.begin(7, 11).unwrap();
    for row in 0..SPILL_ROWS {
        assert_eq!(
            database.entity_get(&mut inspect, &key(&format!("{label}-{row:05}"))).unwrap(),
            Some(spill_value(label, row)),
            "{label} row {row}"
        );
    }
}

fn assert_catalogued(blocks: &BlockStore, catalogued: &BTreeMap<BlockId, Vec<u8>>) {
    for (id, payload) in catalogued {
        assert_eq!(&blocks.get(*id).unwrap(), payload);
    }
    let missing = BlockId::for_payload(b"never written by anyone");
    assert_eq!(blocks.get(missing).unwrap_err().kind(), io::ErrorKind::NotFound);
}

/// A settled authority with `CATALOGUED_SEGMENTS` published segments, reopened
/// as a graceful restart reopens it: the WAL is empty, so the open installs
/// the catalog without scanning it.
fn reopened_catalogued_authority() -> (Arc<DeterministicVfs>, AuthorityDatabase, BTreeMap<BlockId, Vec<u8>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    database.set_write_overlay_for_test(false);
    let mut seed = database.begin(7, 11).unwrap();
    database.entity_put(&mut seed, key("seed"), b"seed".to_vec()).unwrap();
    database.commit(seed).unwrap();
    let blocks = database.block_store_shared();
    let store = blocks.payload_segment_store().unwrap();
    let mut references = blocks.installed_payload_manifest().unwrap().map_or_else(Vec::new, |manifest| manifest.segments);
    let mut catalogued = BTreeMap::new();
    for number in 0..CATALOGUED_SEGMENTS {
        let payloads: BTreeMap<BlockId, Vec<u8>> = (0..32)
            .map(|block| format!("catalogued-{number}-{block}").into_bytes())
            .map(|payload| (BlockId::for_payload(&payload), payload))
            .collect();
        let created = store
            .create(Uuid::from_u128(0xf117_0000 + number as u128), payloads.keys().copied().collect(), |id| {
                Ok(payloads[&id].clone())
            })
            .unwrap();
        references.push(
            PayloadSegmentReference::new(created.metadata(), created.first_block_id(), created.last_block_id()).unwrap(),
        );
        catalogued.extend(payloads);
    }
    database.engine_mut_for_test().publish_payload_manifest_for_test(PayloadManifest::new(references).unwrap()).unwrap();
    database.merge_entity_overlay_fully().unwrap();
    database.checkpoint_wal_window().unwrap();
    drop(database);
    let mut reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    reopened.set_write_overlay_for_test(false);
    (vfs, reopened, catalogued)
}

/// Before the change the spill's install read every catalogued index (here
/// 17, live 4,096) to build the filter; now it reads only its own segment's
/// index, which its creation and its verification read back.
#[test]
fn the_first_spill_after_a_clean_open_reads_only_the_segment_it_adds() {
    let (_vfs, mut database, catalogued) = reopened_catalogued_authority();
    let segments = database.writer_engine().unwrap().payload_segment_count();
    assert!(segments > CATALOGUED_SEGMENTS, "fixture: {segments} catalogued segments");
    read_path_counters::take_index_work();
    spill(&mut database, "first-publication").unwrap();
    let (index_reads, _, _) = read_path_counters::take_index_work();
    assert!(database.writer_engine().unwrap().payload_segment_count() > segments, "the spill must publish a segment");
    assert!(
        index_reads <= 2,
        "the first publication read {index_reads} segment indexes of a {segments}-segment catalog"
    );
    assert_spilled(&database, "first-publication");
    assert_catalogued(&database.block_store_shared(), &catalogued);
}

/// The online GC driver builds the filter on its own thread; a commit that
/// spills while that build is suspended between two index reads finishes on
/// its own reads, and the build then covers the catalog the commit published.
fn suspended_build(blocks: Arc<BlockStore>) -> (mpsc::Receiver<()>, mpsc::Sender<bool>, std::thread::JoinHandle<io::Result<bool>>) {
    let (entered, suspended) = mpsc::channel();
    let (release, released) = mpsc::channel::<bool>();
    let builder = std::thread::spawn(move || {
        let mut first = true;
        blocks.build_packed_filter(move || {
            if std::mem::take(&mut first) {
                entered.send(()).unwrap();
                if !released.recv().unwrap_or(false) {
                    return Err(io::Error::new(io::ErrorKind::WouldBlock, "builder stopped"));
                }
            }
            Ok(())
        })
    });
    (suspended, release, builder)
}

#[test]
fn a_spill_during_the_off_mutex_filter_build_neither_waits_for_it_nor_scans_the_catalog() {
    let (_vfs, mut database, catalogued) = reopened_catalogued_authority();
    let blocks = database.block_store_shared();
    assert_eq!(blocks.packed_filter_state_for_test(), (false, false), "fixture: a clean open builds no filter");
    let (suspended, release, builder) = suspended_build(Arc::clone(&blocks));
    suspended.recv().unwrap();
    read_path_counters::take_index_work();
    spill(&mut database, "during-the-build").unwrap();
    assert!(read_path_counters::take_index_work().0 <= 2, "the commit read catalogued indexes");
    assert!(!builder.is_finished(), "the commit finished while the build was suspended");
    assert_eq!(blocks.packed_filter_state_for_test(), (false, false), "an uncovered publication excludes nothing");
    assert_spilled(&database, "during-the-build");
    assert_catalogued(&blocks, &catalogued);
    release.send(true).unwrap();
    assert!(builder.join().unwrap().unwrap(), "the build installs its filter");
    assert_eq!(blocks.packed_filter_state_for_test(), (true, true), "the build covers the catalog the spill published");
    assert_spilled(&database, "during-the-build");
    assert_catalogued(&blocks, &catalogued);
    spill(&mut database, "after-the-build").unwrap();
    assert_eq!(blocks.packed_filter_state_for_test(), (true, true), "later publications stay covered");
    assert_spilled(&database, "after-the-build");
}

/// The build writes nothing: a crash while it is suspended loses no
/// acknowledged commit, and the reopened store reads exactly before and after
/// its own build.
#[test]
fn a_crash_during_the_off_mutex_filter_build_loses_no_acknowledged_commit() {
    let (vfs, mut database, catalogued) = reopened_catalogued_authority();
    let blocks = database.block_store_shared();
    let (suspended, release, builder) = suspended_build(Arc::clone(&blocks));
    suspended.recv().unwrap();
    spill(&mut database, "before-the-crash").unwrap();
    vfs.crash().unwrap();
    release.send(false).unwrap();
    assert_eq!(builder.join().unwrap().unwrap_err().kind(), io::ErrorKind::WouldBlock);
    assert_eq!(blocks.packed_filter_state_for_test(), (false, false), "a stopped build installed nothing");
    drop(blocks);
    drop(database);
    let mut reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    reopened.set_write_overlay_for_test(false);
    let blocks = reopened.block_store_shared();
    assert_spilled(&reopened, "before-the-crash");
    assert_catalogued(&blocks, &catalogued);
    let _ = blocks.build_packed_filter(|| Ok(())).unwrap();
    assert_eq!(blocks.packed_filter_state_for_test(), (true, true));
    assert_spilled(&reopened, "before-the-crash");
    assert_catalogued(&blocks, &catalogued);
    spill(&mut reopened, "after-the-crash").unwrap();
    assert_spilled(&reopened, "after-the-crash");
}
