//! Reopen bounded by what the last publication covers (docs/TOFUDB_RECOVERY_ATTRIBUTION.md,
//! "Reopen work bounded by the recovery witness"). A catalog of cold one-block segments
//! makes any catalog-wide recovery pass visible as at least one open per segment, and
//! recovery that indexes the catalog visible as a built packed filter.
use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

const COLD_SEGMENTS: usize = 64;
/// Opens of a reopen that reads no catalogued segment: lease, CONTROL, WAL, manifest,
/// witness and history roots. Far below one open per catalogued segment.
const WITNESSED_OPEN_MAXIMUM_OPENS: usize = COLD_SEGMENTS / 2;

fn simulated_engine() -> (Arc<DeterministicVfs>, Engine) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, engine)
}

fn cold_payload(index: usize) -> Vec<u8> {
    format!("cold catalog entry {index}").into_bytes()
}

fn carried_payload(index: usize) -> Vec<u8> {
    format!("window carried payload {index}").into_bytes()
}

/// Publishes `COLD_SEGMENTS` one-block segments and returns nothing: every payload is
/// `cold_payload(index)`.
fn publish_cold_catalog(engine: &mut Engine) {
    let mut references = Vec::new();
    for index in 0..COLD_SEGMENTS {
        let payload = cold_payload(index);
        let id = BlockId::for_payload(&payload);
        let segment = engine
            .blocks
            .payload_segment_store()
            .unwrap()
            .create(Uuid::new_v4(), vec![id], |_| Ok(payload.clone()))
            .unwrap();
        references.push(
            PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )
            .unwrap(),
        );
    }
    engine
        .publish_payload_manifest(PayloadManifest::new(references).unwrap(), &[])
        .unwrap();
}

/// An unretired window: carried payloads and an identifier-only reference into the cold
/// catalog. Returns the durable sequence after it.
fn commit_window(engine: &mut Engine, carried: usize) -> u64 {
    for index in 0..carried {
        engine
            .commit_transaction(b"window carried", &[carried_payload(index).as_slice()])
            .unwrap();
    }
    let catalogued = BlockId::for_payload(&cold_payload(COLD_SEGMENTS / 3));
    engine
        .commit_references(b"window identifier-only", &[catalogued])
        .unwrap();
    engine.state.durable_sequence
}

/// The daemon's off-mutex pack adoption of every staged payload.
fn adopt_staged_payloads(engine: &mut Engine) {
    let mut prepared = engine.prepare_pack_publication_for_test();
    let outcome = publish_prepared_pack_adoption(&mut prepared);
    engine.finish_pack_adoption(prepared, outcome).unwrap();
}

fn open_counting(vfs: &Arc<DeterministicVfs>) -> (Engine, usize) {
    vfs.arm_fault(None).unwrap();
    let engine = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let opens = vfs
        .trace()
        .unwrap()
        .iter()
        .filter(|operation| **operation == Operation::Open)
        .count();
    (engine, opens)
}

fn assert_window_reads_back(engine: &Engine, carried: usize) {
    for index in 0..carried {
        assert_eq!(
            engine
                .read_block(BlockId::for_payload(&carried_payload(index)))
                .unwrap(),
            carried_payload(index),
            "carried payload {index}"
        );
    }
    let catalogued = cold_payload(COLD_SEGMENTS / 3);
    assert_eq!(
        engine
            .read_block(BlockId::for_payload(&catalogued))
            .unwrap(),
        catalogued
    );
}

/// A crash after a pack adoption, with a later tail that references no payload: the
/// adoption's witness covers every payload reference, so the reopen reads no catalogued
/// segment and reaches the acknowledged durable sequence.
fn crash_after_witnessed_adoption() -> (Arc<DeterministicVfs>, u64) {
    let (vfs, mut engine) = simulated_engine();
    publish_cold_catalog(&mut engine);
    commit_window(&mut engine, 8);
    adopt_staged_payloads(&mut engine);
    engine.commit(b"inline tail after the adoption").unwrap();
    let durable = engine.state.durable_sequence;
    std::mem::forget(engine);
    vfs.crash().unwrap();
    (vfs, durable)
}

#[test]
#[ignore = "TOFUDB-REOPEN-1 lever 2 (a reopen bounded by the recovery witness) is not implemented; R3 and lever 1 both read the catalog on this reopen"]
fn a_crash_after_a_witnessed_adoption_reopens_without_reading_the_catalog() {
    let (vfs, durable) = crash_after_witnessed_adoption();
    let (reopened, opens) = open_counting(&vfs);
    assert_eq!(reopened.state().durable_sequence, durable);
    assert!(opens <= WITNESSED_OPEN_MAXIMUM_OPENS,
        "a reopen covered by the adoption's witness opened {opens} files ({COLD_SEGMENTS} cold segments)");
    assert_window_reads_back(&reopened, 8);
}

#[test]
#[ignore = "TOFUDB-REOPEN-1 lever 2 (a reopen bounded by the recovery witness) is not implemented; R3 and lever 1 both read the catalog on this reopen"]
fn a_crash_at_every_operation_of_a_witnessed_reopen_reaches_the_same_durable_sequence() {
    let (seed, durable) = crash_after_witnessed_adoption();
    let baseline = Arc::new(seed.fork_durable().unwrap());
    baseline.arm_fault(None).unwrap();
    drop(Engine::open_with_vfs(Path::new("/data"), baseline.clone()).unwrap());
    let operations = baseline.trace().unwrap().len() as u64;
    assert!(operations > 4);
    for operation_number in 1..=operations {
        let vfs = Arc::new(seed.fork_durable().unwrap());
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        if let Ok(engine) = Engine::open_with_vfs(Path::new("/data"), vfs.clone()) {
            std::mem::forget(engine);
        }
        vfs.crash().unwrap();
        let (reopened, opens) = open_counting(&vfs);
        assert_eq!(
            reopened.state().durable_sequence,
            durable,
            "crash at operation {operation_number}"
        );
        assert!(
            opens <= WITNESSED_OPEN_MAXIMUM_OPENS,
            "the reopen after a crash at operation {operation_number} opened {opens} files"
        );
        assert_window_reads_back(&reopened, 8);
    }
}

/// Without a witness (lever 1 alone) recovery still locates every reference the WAL makes,
/// but it reads each catalogued index once and builds none of the catalog-wide structures
/// (packed filter, routes, membership witnesses) that the online GC driver builds after
/// readiness, exactly as after a clean open.
#[test]
fn a_dirty_reopen_reads_each_catalogued_index_at_most_once_and_builds_no_packed_filter() {
    let (vfs, mut engine) = simulated_engine();
    publish_cold_catalog(&mut engine);
    for index in 0..COLD_SEGMENTS {
        engine
            .commit_references(
                b"identifier-only",
                &[BlockId::for_payload(&cold_payload(index))],
            )
            .unwrap();
    }
    for index in 0..8 {
        engine
            .commit_transaction(b"window carried", &[carried_payload(index).as_slice()])
            .unwrap();
    }
    let durable = engine.state.durable_sequence;
    std::mem::forget(engine);
    vfs.crash().unwrap();
    crate::payload_segment::read_path_counters::take_index_work();
    let reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let (index_reads, _, _) = crate::payload_segment::read_path_counters::take_index_work();
    assert_eq!(reopened.state().durable_sequence, durable);
    assert!(
        index_reads <= COLD_SEGMENTS as u64,
        "the reopen read {index_reads} catalogued indexes for {COLD_SEGMENTS} segments"
    );
    assert!(
        !reopened.blocks.packed_filter_state_for_test().0,
        "recovery built the packed filter"
    );
    for index in 0..COLD_SEGMENTS {
        assert_eq!(
            reopened
                .read_block(BlockId::for_payload(&cold_payload(index)))
                .unwrap(),
            cold_payload(index)
        );
    }
    for index in 0..8 {
        assert_eq!(
            reopened
                .read_block(BlockId::for_payload(&carried_payload(index)))
                .unwrap(),
            carried_payload(index)
        );
    }
}

#[test]
#[ignore = "TOFUDB-REOPEN-1 lever 2 (a reopen bounded by the recovery witness) is not implemented; R3 and lever 1 both read the catalog on this reopen"]
fn a_clean_close_reopens_without_reading_the_catalog_and_restages_the_staged_payloads() {
    let (vfs, mut engine) = simulated_engine();
    publish_cold_catalog(&mut engine);
    let durable = commit_window(&mut engine, 8);
    let staged_before = engine.blocks.pending_staged_bytes().unwrap();
    assert!(staged_before > 0);
    drop(engine);
    let (reopened, opens) = open_counting(&vfs);
    assert_eq!(reopened.state().durable_sequence, durable);
    assert!(
        opens <= WITNESSED_OPEN_MAXIMUM_OPENS,
        "a reopen after a clean close opened {opens} files ({COLD_SEGMENTS} cold segments)"
    );
    for index in 0..8 {
        assert!(
            reopened
                .blocks
                .pending_payload(BlockId::for_payload(&carried_payload(index)))
                .unwrap()
                .is_some(),
            "carried payload {index} must be restaged from its WAL copy"
        );
    }
    assert_window_reads_back(&reopened, 8);
}
