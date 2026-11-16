//! Recovery lookup by catalog location table (docs/TOFUDB_RECOVERY_ATTRIBUTION.md, "Recovery
//! builds the catalog location table"). A differential specification: every selected ID gets the
//! answer recovery gave before it kept positions, whether the block is held in several segments,
//! in history and the catalog, only in history, only loose, only in the catalog, or nowhere; with
//! every index verified it also equals the exact read path's answer. A segment whose index fails
//! verification leaves the lookup incomplete, contributes no entry to the published table and
//! never makes its blocks look uncatalogued. A crashed open publishes the catalog location table,
//! so the online GC driver builds no second first table.
use super::*;
use crate::vfs::DeterministicVfs;

const COLD_SEGMENTS: usize = 64;
const DAMAGED_ONLY: &str = "the damaged segment only";
const TWO_SEGMENTS: &str = "two segments";
const NOWHERE: &str = "nowhere";

fn simulated_engine() -> (Arc<DeterministicVfs>, Engine) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, engine)
}

fn seeded(kind: &str, index: usize) -> Vec<u8> {
    format!("differential recovery {kind} {index}").into_bytes()
}

fn rotate_into_indexed_history(engine: &mut Engine) {
    let snapshot = match engine.try_prepare_rotation_checkpoint().unwrap() {
        RotationCheckpointPreparation::Ready(snapshot) => snapshot,
        other => panic!("expected ready indexed rotation: {other:?}"),
    };
    let outcome = encode_rotation_segments(&snapshot);
    engine
        .finish_rotation_checkpoint(snapshot, outcome)
        .unwrap();
}

fn create_segment(engine: &Engine, payloads: &[Vec<u8>]) -> PayloadSegmentReference {
    let by_id: BTreeMap<BlockId, Vec<u8>> = payloads
        .iter()
        .map(|payload| (BlockId::for_payload(payload), payload.clone()))
        .collect();
    let segment = engine
        .blocks
        .payload_segment_store()
        .unwrap()
        .create(Uuid::new_v4(), by_id.keys().copied().collect(), |id| {
            Ok(by_id[&id].clone())
        })
        .unwrap();
    PayloadSegmentReference::new(
        segment.metadata(),
        segment.first_block_id(),
        segment.last_block_id(),
    )
    .unwrap()
}

/// What replay can tell apart about one block: its bytes, or a missing/invalid copy (replay
/// truncates or fails closed on both alike), or another error kind.
#[derive(Debug, PartialEq)]
enum Answer {
    Bytes(Vec<u8>),
    MissingOrInvalid,
    Error(io::ErrorKind),
}

fn answer(result: io::Result<Vec<u8>>) -> Answer {
    match result {
        Ok(bytes) => Answer::Bytes(bytes),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::InvalidData
            ) =>
        {
            Answer::MissingOrInvalid
        }
        Err(error) => Answer::Error(error.kind()),
    }
}

struct SeededStore {
    vfs: Arc<DeterministicVfs>,
    engine: Engine,
    /// Every seeded (place, payload).
    blocks: Vec<(&'static str, Vec<u8>)>,
    /// The segment the damaged variants corrupt.
    damaged: PayloadSegmentReference,
    /// Every other catalogued segment.
    indexed: Vec<PayloadSegmentReference>,
}

/// A store whose blocks sit in every place recovery resolves.
fn seeded_store() -> SeededStore {
    let (vfs, mut engine) = simulated_engine();
    let history: Vec<Vec<u8>> = (0..12).map(|index| seeded("history", index)).collect();
    for payload in &history {
        engine
            .commit_transaction(b"history owner", &[payload.as_slice()])
            .unwrap();
    }
    rotate_into_indexed_history(&mut engine);
    assert!(engine
        .history_manifest
        .as_ref()
        .unwrap()
        .payload_index_root_id
        .is_some());
    let several: Vec<Vec<u8>> = (0..8).map(|index| seeded("several", index)).collect();
    let damaged_only: Vec<Vec<u8>> = (0..8).map(|index| seeded("damaged only", index)).collect();
    let catalog_only: Vec<Vec<u8>> = (0..8).map(|index| seeded("catalog", index)).collect();
    let damaged = create_segment(&engine, &[several.clone(), damaged_only.clone()].concat());
    let intact = create_segment(&engine, &[several.clone(), catalog_only.clone()].concat());
    let history_too = create_segment(&engine, &history[..4]);
    engine
        .publish_payload_manifest_for_test(
            PayloadManifest::new(vec![damaged, intact, history_too]).unwrap(),
        )
        .unwrap();
    let loose: Vec<Vec<u8>> = (0..8).map(|index| seeded("loose", index)).collect();
    for payload in &loose {
        engine.blocks.put_loose(payload).unwrap();
    }
    let mut blocks = Vec::new();
    for (place, payloads) in [
        ("history and the catalog", &history[..4]),
        ("history only", &history[4..]),
        (TWO_SEGMENTS, &several[..]),
        (DAMAGED_ONLY, &damaged_only[..]),
        ("one segment", &catalog_only[..]),
        ("loose only", &loose[..]),
    ] {
        blocks.extend(payloads.iter().map(|payload| (place, payload.clone())));
    }
    blocks.extend((0..8).map(|index| (NOWHERE, seeded("nowhere", index))));
    SeededStore {
        vfs,
        engine,
        blocks,
        damaged,
        indexed: vec![intact, history_too],
    }
}

/// Flips one byte of the last entry of `segment`'s index, so a reader that recorded entries
/// while streaming would have recorded every other entry before the trailer digest failed.
fn damage_last_index_entry(vfs: &DeterministicVfs, segment: &PayloadSegmentReference) {
    let path =
        Path::new("/data/payload-segments").join(format!("{}.pseg", segment.segment_id.simple()));
    let mut file = vfs
        .open(
            &path,
            crate::vfs::OpenRequest {
                read: true,
                write: true,
                ..Default::default()
            },
        )
        .unwrap();
    let last_entry_byte = 48 + segment.payload_bytes + u64::from(segment.block_count - 1) * 48 + 40;
    let mut byte = [0_u8; 1];
    file.read_exact_at(last_entry_byte, &mut byte).unwrap();
    file.write_all_at(last_entry_byte, &[byte[0] ^ 0x5a])
        .unwrap();
}

/// Damages the seeded store's segment; no resident verified copy may hide the damage.
fn damage_seeded_segment(seeded: &SeededStore) {
    damage_last_index_entry(&seeded.vfs, &seeded.damaged);
    seeded
        .engine
        .blocks
        .set_segment_index_cache_bytes(0)
        .unwrap();
}

fn selected_ids(seeded: &SeededStore) -> BTreeSet<BlockId> {
    seeded
        .blocks
        .iter()
        .map(|(_, payload)| BlockId::for_payload(payload))
        .collect()
}

/// The exact read of `id` through the catalog, history and loose paths, with no location
/// table and no cached copy.
fn exact_read(engine: &Engine, id: BlockId) -> Answer {
    crate::block::set_catalog_locations_disabled_for_test(true);
    engine.blocks.set_cache_capacity_bytes(0).unwrap();
    let read = answer(engine.blocks.get(id));
    crate::block::set_catalog_locations_disabled_for_test(false);
    read
}

fn assert_recovery_answers(damage_index: bool) {
    let seeded = seeded_store();
    if damage_index {
        damage_seeded_segment(&seeded);
    }
    let engine = &seeded.engine;
    let selected = selected_ids(&seeded);
    let exact: BTreeMap<BlockId, Answer> = selected
        .iter()
        .map(|id| (*id, exact_read(engine, *id)))
        .collect();
    engine.blocks.set_cache_capacity_bytes(0).unwrap();
    let mut lookup = engine
        .blocks
        .prepare_carried_recovery(selected.clone())
        .unwrap();
    assert_eq!(
        lookup.is_complete_for_test(),
        !damage_index,
        "a lookup is complete only when every index verified"
    );
    for (place, payload) in &seeded.blocks {
        let id = BlockId::for_payload(payload);
        // A block held only by the damaged segment, or by nothing, is missing or invalid, as it
        // was before positions were kept. A block whose other copy sits in a verified index is
        // served from that copy, as reads are once the first location table is published; the
        // old catalog-order recovery scan surfaced the damaged segment's error first.
        let expected = if *place == NOWHERE || (damage_index && *place == DAMAGED_ONLY) {
            Answer::MissingOrInvalid
        } else {
            Answer::Bytes(payload.clone())
        };
        assert_eq!(
            answer(engine.blocks.get_for_recovery(id, &lookup)),
            expected,
            "identifier-only recovery of a block in {place} (index damaged: {damage_index})"
        );
        if !damage_index {
            assert_eq!(exact[&id], expected, "the exact read of a block in {place}");
        }
        if damage_index && *place == DAMAGED_ONLY {
            assert_eq!(
                engine.blocks.published_catalog_answer_for_test(id),
                "unknown",
                "a damaged index made its block look uncatalogued"
            );
        }
    }
    // Every carried copy restores, and afterwards each block reads back exactly.
    for (place, payload) in &seeded.blocks {
        let id = BlockId::for_payload(payload);
        assert_eq!(
            engine
                .blocks
                .restore_carried_with_lookup(payload, &mut lookup)
                .unwrap(),
            id,
            "carried recovery of a block in {place} (index damaged: {damage_index})"
        );
    }
    for (place, payload) in &seeded.blocks {
        let id = BlockId::for_payload(payload);
        let read = engine
            .blocks
            .pending_payload(id)
            .unwrap()
            .map(|staged| (*staged).clone())
            .map_or_else(|| answer(engine.blocks.get(id)), Answer::Bytes);
        assert_eq!(
            read,
            Answer::Bytes(payload.clone()),
            "a block in {place} after carried recovery"
        );
    }
}

#[test]
fn recovery_answers_every_selected_id_as_before_and_as_the_exact_read_path() {
    assert_recovery_answers(false);
}

#[test]
fn an_index_that_fails_verification_never_makes_its_blocks_look_uncatalogued() {
    assert_recovery_answers(true);
}

/// An index that fails verification after its other entries were read leaves none of them in
/// the published table: its blocks answer Unknown, and copies verified indexes hold answer from
/// those.
#[test]
fn a_damaged_index_leaves_none_of_its_entries_in_the_recovery_table() {
    let seeded = seeded_store();
    damage_seeded_segment(&seeded);
    let unindexed_before = crate::fsync_stats::counters().recovery_unindexed_segments;
    let lookup = seeded
        .engine
        .blocks
        .prepare_carried_recovery(selected_ids(&seeded))
        .unwrap();
    assert!(!lookup.is_complete_for_test());
    // Other specifications may count concurrently; the counter only grows.
    assert!(
        crate::fsync_stats::counters().recovery_unindexed_segments > unindexed_before,
        "recovery did not count the segment whose index failed verification"
    );
    let (publication, table) = seeded
        .engine
        .blocks
        .published_catalog_locations_for_test()
        .expect("recovery published no catalog location table");
    assert_eq!(
        publication,
        seeded
            .engine
            .blocks
            .installed_catalog_publication_for_test()
    );
    assert_eq!(table.unindexed_segments(), 1);
    assert!(!table.segments().contains(&seeded.damaged));
    assert_eq!(
        table.located_blocks(),
        seeded
            .indexed
            .iter()
            .map(|segment| segment.block_count as usize)
            .sum::<usize>(),
        "entries of the damaged index stayed in the published table"
    );
    for (place, payload) in &seeded.blocks {
        let read = seeded
            .engine
            .blocks
            .published_catalog_answer_for_test(BlockId::for_payload(payload));
        match *place {
            DAMAGED_ONLY => assert_eq!(read, "unknown", "a block only the damaged index held"),
            TWO_SEGMENTS => assert_eq!(read, "held", "a block a verified index also holds"),
            _ => {}
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Referenced {
    Twin,
    DamagedOnly,
}

/// A payload whose block ID sorts before every block of `others`, so the segment holding it
/// comes first in catalog order (the canonical order starts with a segment's first block ID).
fn payload_ordered_before(others: &[Vec<u8>]) -> Vec<u8> {
    let floor = others
        .iter()
        .map(|payload| BlockId::for_payload(payload))
        .min()
        .unwrap();
    (0..)
        .map(|index| seeded("catalog order", index))
        .find(|payload| BlockId::for_payload(payload) < floor)
        .unwrap()
}

/// A crashed authority whose catalog holds the twin block in two segments, the damaged one
/// (its index fails verification at reopen) earlier or later in catalog order than the verified
/// one, plus a block only the damaged segment holds. Its last record names `referenced` by
/// identifier only, with CONTROL confirming that record or not. Returns the VFS, the
/// acknowledged durable sequence and CONTROL's durable sequence at the crash.
fn crashed_store_with_a_damaged_index(
    damaged_first: bool,
    referenced: Referenced,
    confirmed: bool,
) -> (Arc<DeterministicVfs>, u64, u64) {
    let (vfs, mut engine) = simulated_engine();
    let twin = seeded("twin", 0);
    let damaged_only = seeded("damaged only", 0);
    let verified_only = seeded("verified only", 0);
    let first =
        payload_ordered_before(&[twin.clone(), damaged_only.clone(), verified_only.clone()]);
    let mut damaged_blocks = vec![twin.clone(), damaged_only.clone()];
    let mut verified_blocks = vec![twin.clone(), verified_only];
    if damaged_first {
        damaged_blocks.push(first);
    } else {
        verified_blocks.push(first);
    }
    let damaged = create_segment(&engine, &damaged_blocks);
    let verified = create_segment(&engine, &verified_blocks);
    let manifest = PayloadManifest::new(vec![damaged, verified]).unwrap();
    assert_eq!(
        manifest.segments[0] == damaged,
        damaged_first,
        "catalog order"
    );
    engine.publish_payload_manifest_for_test(manifest).unwrap();
    let named = match referenced {
        Referenced::Twin => &twin,
        Referenced::DamagedOnly => &damaged_only,
    };
    engine
        .commit_references(b"window identifier-only", &[BlockId::for_payload(named)])
        .unwrap();
    if confirmed {
        engine.flush_dirty_control(ControlStaging::Staged).unwrap();
    }
    let durable = engine.state.durable_sequence;
    let control_durable = ControlFile::open_with_vfs(Path::new("/data"), vfs.clone())
        .unwrap()
        .read_current()
        .unwrap()
        .durable_sequence;
    assert_eq!(
        control_durable == durable,
        confirmed,
        "CONTROL confirms the last record exactly when the case says so"
    );
    std::mem::forget(engine);
    vfs.crash().unwrap();
    damage_last_index_entry(&vfs, &damaged);
    (vfs, durable, control_durable)
}

/// A damaged index changes one recovery outcome only: a record naming a block whose other copy
/// a verified index holds replays, whether the damaged copy comes earlier or later in catalog
/// order and whether CONTROL confirmed the record. A record naming a block only the damaged
/// segment holds keeps today's outcome: the open fails when CONTROL confirmed the record, and the
/// record is truncated when not.
#[test]
fn a_damaged_index_changes_only_the_outcome_of_a_block_with_a_verified_twin() {
    for damaged_first in [true, false] {
        for confirmed in [true, false] {
            let case = format!("damaged copy first: {damaged_first}, confirmed: {confirmed}");
            let (vfs, durable, _) =
                crashed_store_with_a_damaged_index(damaged_first, Referenced::Twin, confirmed);
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap_or_else(|error| {
                panic!("a record naming a block with a verified twin failed the open ({case}): {error}")
            });
            assert_eq!(
                reopened.state().durable_sequence,
                durable,
                "the record naming the twin replays ({case})"
            );
            assert_eq!(
                reopened
                    .read_block(BlockId::for_payload(&seeded("twin", 0)))
                    .unwrap(),
                seeded("twin", 0)
            );
            drop(reopened);
            let (vfs, durable, control_durable) = crashed_store_with_a_damaged_index(
                damaged_first,
                Referenced::DamagedOnly,
                confirmed,
            );
            match Engine::open_with_vfs(Path::new("/data"), vfs.clone()) {
                Err(error) => {
                    assert!(
                        confirmed,
                        "an unconfirmed record failed the open ({case}): {error}"
                    );
                    assert_eq!(error.kind(), io::ErrorKind::InvalidData, "{case}");
                }
                Ok(reopened) => {
                    assert!(!confirmed, "a confirmed record naming a block only the damaged segment holds opened ({case})");
                    assert_eq!(
                        reopened.state().durable_sequence,
                        control_durable,
                        "the unconfirmed record is truncated ({case})"
                    );
                    assert_eq!(control_durable + 1, durable);
                }
            }
        }
    }
}

/// Online GC compacts every live loose block, history manifests, routing roots and pages
/// included, into payload segments. A crashed authority whose history metadata only a
/// catalogued segment holds, behind `COLD_SEGMENTS` cold one-block segments. Returns the VFS,
/// the acknowledged durable sequence and the history payloads.
fn crashed_store_with_catalogued_history() -> (Arc<DeterministicVfs>, u64, Vec<Vec<u8>>) {
    let (vfs, mut engine) = simulated_engine();
    let history: Vec<Vec<u8>> = (0..64)
        .map(|index| seeded("catalogued history", index))
        .collect();
    for payload in &history {
        engine
            .commit_transaction(b"history owner", &[payload.as_slice()])
            .unwrap();
    }
    rotate_into_indexed_history(&mut engine);
    let mut metadata = engine
        .blocks
        .history_payload_metadata_ids(engine.history_manifest.as_ref().unwrap())
        .unwrap();
    metadata.extend(engine.state.history_manifest_block_id);
    let metadata_payloads: Vec<Vec<u8>> = metadata
        .iter()
        .map(|id| engine.blocks.get(*id).unwrap())
        .collect();
    let mut references: Vec<_> = (0..COLD_SEGMENTS)
        .map(|index| create_segment(&engine, &[seeded("cold", index)]))
        .collect();
    references.push(create_segment(&engine, &metadata_payloads));
    engine
        .publish_payload_manifest_for_test(PayloadManifest::new(references).unwrap())
        .unwrap();
    // As after loose compaction: only the catalog holds the history metadata.
    engine.blocks.remove_blocks(&metadata).unwrap();
    for payload in history.iter().take(8) {
        engine
            .commit_references(b"history reader", &[BlockId::for_payload(payload)])
            .unwrap();
    }
    engine
        .commit_references(b"cold reader", &[BlockId::for_payload(&seeded("cold", 3))])
        .unwrap();
    let durable = engine.state.durable_sequence;
    std::mem::forget(engine);
    vfs.crash().unwrap();
    (vfs, durable, history)
}

/// A dirty reopen reads its catalogued history metadata through the catalog: each catalogued
/// index is read once, and the metadata resolves by position, never by a candidate scan.
#[test]
fn a_dirty_reopen_reads_catalogued_history_metadata_without_scanning_the_catalog() {
    let (vfs, durable, history) = crashed_store_with_catalogued_history();
    crate::payload_segment::read_path_counters::take_index_work();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let (index_reads, _, _) = crate::payload_segment::read_path_counters::take_index_work();
    assert_eq!(reopened.state().durable_sequence, durable);
    assert!(
        index_reads <= COLD_SEGMENTS as u64 + 1,
        "the reopen read {index_reads} catalogued indexes for {} segments",
        COLD_SEGMENTS + 1
    );
    for payload in &history {
        assert_eq!(
            &reopened.read_block(BlockId::for_payload(payload)).unwrap(),
            payload
        );
    }
}

/// The dirty reopen publishes the fully indexed location table of the installed catalog, so the
/// online GC driver finds it and builds no second first table after readiness.
#[test]
fn a_dirty_reopen_publishes_the_first_catalog_location_table() {
    let (vfs, _, _) = crashed_store_with_catalogued_history();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let (publication, table) = reopened
        .blocks
        .published_catalog_locations_for_test()
        .expect("the dirty reopen published no catalog location table");
    assert_eq!(
        publication,
        reopened.blocks.installed_catalog_publication_for_test()
    );
    assert_eq!(table.unindexed_segments(), 0);
    assert_eq!(table.segments().len(), COLD_SEGMENTS + 1);
    assert!(
        !reopened
            .blocks
            .publish_first_catalog_locations(|| Ok(()))
            .unwrap(),
        "the online GC driver built a second first table"
    );
}
