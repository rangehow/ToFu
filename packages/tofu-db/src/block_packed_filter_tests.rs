//! The packed filter is built off the authority mutex
//! (docs/TOFUDB_BLOCK_READ_PATH.md, "Packed filter built off the authority
//! mutex"): no install reads an index its publication did not add,
//! `build_packed_filter` converges on the catalog installed by then, and
//! negative answers stay exact before, during and after the build, across
//! publications that land while it runs, stops, read faults and crashes.
use super::*;
use crate::payload_segment::read_path_counters;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

const DATA: &str = "/data";

fn simulated_store(vfs: &DeterministicVfs) -> BlockStore {
    vfs.create_dir(Path::new(DATA)).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let store = BlockStore::initialize_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    store.set_cache_capacity_bytes(0).unwrap();
    store
}

fn payloads(label: &str, count: usize) -> BTreeMap<BlockId, Vec<u8>> {
    (0..count)
        .map(|number| format!("{label}-{number}").into_bytes())
        .map(|payload| (BlockId::for_payload(&payload), payload))
        .collect()
}

/// One unpublished segment holding exactly `payloads`.
fn segment_of(
    store: &BlockStore,
    number: u128,
    payloads: &BTreeMap<BlockId, Vec<u8>>,
) -> PayloadSegmentReference {
    let segment = store
        .payload_segment_store()
        .unwrap()
        .create(
            Uuid::from_u128(0xf117_e500_0000_0000_0000_0000_0000_0000 | number),
            payloads.keys().copied().collect(),
            |id| Ok(payloads[&id].clone()),
        )
        .unwrap();
    PayloadSegmentReference::new(
        segment.metadata(),
        segment.first_block_id(),
        segment.last_block_id(),
    )
    .unwrap()
}

/// A catalog of `segments` segments, reopened the way a clean engine open
/// reopens it: the catalog is installed and the packed filter is not built.
fn reopened_catalog(
    segments: usize,
) -> (
    DeterministicVfs,
    BlockStore,
    PayloadManifest,
    BTreeMap<BlockId, Vec<u8>>,
) {
    let vfs = DeterministicVfs::new(None);
    let writer = simulated_store(&vfs);
    let mut references = Vec::new();
    let mut catalogued = BTreeMap::new();
    for number in 0..segments {
        let contents = payloads(&format!("catalogued-{number}"), 8);
        references.push(segment_of(&writer, number as u128, &contents));
        catalogued.extend(contents);
    }
    let manifest = PayloadManifest::new(references).unwrap();
    drop(writer);
    let store = reopen(&vfs, &manifest);
    (vfs, store, manifest, catalogued)
}

fn reopen(vfs: &DeterministicVfs, manifest: &PayloadManifest) -> BlockStore {
    let store = BlockStore::open_with_vfs(Path::new(DATA), Arc::new(vfs.clone())).unwrap();
    store.set_cache_capacity_bytes(0).unwrap();
    store
        .install_recovery_payload_manifest(manifest.clone())
        .unwrap();
    assert!(
        !store.packed_filter.read().unwrap().built,
        "fixture: a clean open builds no filter"
    );
    store
}

/// Every catalogued block passes the filter and reads back; a block nobody
/// wrote reads as missing, and the filter excludes it exactly when it covers
/// the installed catalog.
fn assert_exact(store: &BlockStore, catalogued: &BTreeMap<BlockId, Vec<u8>>) {
    for (id, payload) in catalogued {
        assert!(
            store.packed_filter_may_contain(*id).unwrap(),
            "a catalogued block failed the packed filter"
        );
        assert_eq!(&store.get(*id).unwrap(), payload);
    }
    let missing = BlockId::for_payload(b"never written by anyone");
    let covered = store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap());
    assert_eq!(
        store.packed_filter_excludes(missing).unwrap().is_some(),
        covered
    );
    assert_eq!(
        store.get(missing).unwrap_err().kind(),
        io::ErrorKind::NotFound
    );
}

/// Live 2026-11-09: the first catalog publication after a clean restart was a
/// foreground commit, and its install scanned all 4,096 segment indexes
/// (234,070,323 IDs) into the filter inside a 68.2 s authority hold. Before the
/// change this install read every catalogued index.
#[test]
fn an_install_reads_no_index_its_publication_did_not_add_while_the_filter_is_unbuilt() {
    let (_vfs, store, manifest, mut catalogued) = reopened_catalog(12);
    let added = payloads("added by a spill", 4);
    let reference = segment_of(&store, 100, &added);
    store.verify_payload_segment(&reference).unwrap();
    let mut segments = manifest.segments.clone();
    segments.push(reference);
    read_path_counters::take_index_work();
    store
        .install_payload_manifest(PayloadManifest::new(segments).unwrap())
        .unwrap();
    assert_eq!(
        read_path_counters::take_index_work().0,
        0,
        "the install read catalogued indexes"
    );
    assert!(
        !store.packed_filter.read().unwrap().built,
        "no install builds the filter of a catalog it did not add"
    );
    catalogued.extend(added);
    assert_exact(&store, &catalogued);
    assert!(store.build_packed_filter(|| Ok(())).unwrap());
    assert!(store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()));
    assert_exact(&store, &catalogued);
}

/// A store with nothing catalogued still builds its filter at its first
/// install: that scan reads only the segments the publication adds.
#[test]
fn the_first_install_of_an_empty_store_builds_the_filter_from_what_it_adds() {
    let vfs = DeterministicVfs::new(None);
    let store = simulated_store(&vfs);
    let added = payloads("first publication", 4);
    let reference = segment_of(&store, 1, &added);
    read_path_counters::take_index_work();
    store
        .install_payload_manifest(PayloadManifest::new(vec![reference]).unwrap())
        .unwrap();
    assert_eq!(
        read_path_counters::take_index_work().0,
        1,
        "only the added segment's index is read"
    );
    assert!(store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()));
    assert!(
        !store.build_packed_filter(|| Ok(())).unwrap(),
        "nothing is left for the builder"
    );
    assert_exact(&store, &added);
}

/// A publication lands between any two index reads of the build (`check` runs
/// before each read): while the build runs its segment is uncovered and no
/// lookup is excluded; the build then reads it too and its negative answers
/// cover the catalog installed by then, never hiding the new block.
#[test]
fn publications_landing_during_the_build_are_covered_and_never_hidden() {
    const SEGMENTS: usize = 6;
    for landing in 1..=SEGMENTS + 1 {
        let (_vfs, store, manifest, mut catalogued) = reopened_catalog(SEGMENTS);
        let added = payloads(&format!("published during the build at read {landing}"), 3);
        let mut calls = 0;
        let built = store
            .build_packed_filter(|| {
                calls += 1;
                if calls == landing {
                    let reference = segment_of(&store, 200, &added);
                    store.verify_payload_segment(&reference)?;
                    let mut segments = manifest.segments.clone();
                    segments.push(reference);
                    store.install_payload_manifest(PayloadManifest::new(segments)?)?;
                    for (id, payload) in &added {
                        assert!(
                            store.packed_filter_may_contain(*id)?,
                            "an uncovered publication hid a block"
                        );
                        assert_eq!(&store.get(*id)?, payload);
                    }
                    assert_eq!(
                        store.packed_filter_excludes(BlockId::for_payload(
                            b"never written by anyone"
                        ))?,
                        None
                    );
                }
                Ok(())
            })
            .unwrap();
        assert!(built, "landing {landing}");
        assert!(
            calls >= landing,
            "the publication must land inside the build"
        );
        catalogued.extend(added);
        assert!(
            store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()),
            "landing {landing}: the build converged on the installed catalog"
        );
        assert_exact(&store, &catalogued);
    }
}

/// Publications race the build from another thread, as a foreground spill
/// races the online GC driver's build: after every publication each block of
/// the installed catalog passes the filter, and whichever finishes last, the
/// build or an install, leaves negative answers covering the installed catalog.
#[test]
fn publications_racing_the_build_see_no_false_negative_and_end_covered() {
    const SEGMENTS: usize = 24;
    const PUBLICATIONS: usize = 24;
    for round in 0..4 {
        let (_vfs, store, manifest, mut catalogued) = reopened_catalog(SEGMENTS);
        let added: Vec<BTreeMap<BlockId, Vec<u8>>> = (0..PUBLICATIONS)
            .map(|number| payloads(&format!("raced-{round}-{number}"), 3))
            .collect();
        std::thread::scope(|scope| {
            let builder = scope.spawn(|| {
                store.build_packed_filter(|| {
                    std::thread::yield_now();
                    Ok(())
                })
            });
            let mut segments = manifest.segments.clone();
            for (number, contents) in added.iter().enumerate() {
                let reference = segment_of(&store, 300 + number as u128, contents);
                store.verify_payload_segment(&reference).unwrap();
                segments.push(reference);
                store
                    .install_payload_manifest(PayloadManifest::new(segments.clone()).unwrap())
                    .unwrap();
                for id in added[..=number].iter().flat_map(|contents| contents.keys()) {
                    assert!(
                        store.packed_filter_may_contain(*id).unwrap(),
                        "a published block failed the filter"
                    );
                }
            }
            builder.join().unwrap().unwrap();
        });
        for contents in added {
            catalogued.extend(contents);
        }
        assert!(
            store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()),
            "round {round}: the installed catalog is covered"
        );
        assert_exact(&store, &catalogued);
    }
}

/// The off-mutex build can install its filter between an install's coverage
/// step and its catalog swap; the seam runs it exactly there. The build covered
/// the catalog being replaced, so the install covers what it added after its
/// swap and leaves no publication uncovered: both when the coverage step found
/// the filter unbuilt (the first build) and when it had bound the publication
/// to the filter a saturation rebuild then replaced.
#[test]
fn a_build_landing_inside_an_install_leaves_its_publication_covered() {
    for saturated in [false, true] {
        let (_vfs, store, manifest, mut catalogued) = reopened_catalog(6);
        if saturated {
            assert!(store.build_packed_filter(|| Ok(())).unwrap());
            let mut filler = 0_u64;
            while !store.packed_filter.read().unwrap().saturated() {
                filler += 1;
                store
                    .note_packed(&[BlockId::for_payload(&filler.to_le_bytes())])
                    .unwrap();
            }
        }
        let added = payloads(
            &format!("published around the build, saturated={saturated}"),
            3,
        );
        let reference = segment_of(&store, 400, &added);
        store.verify_payload_segment(&reference).unwrap();
        store.before_next_install_swap_for_test(|store| {
            assert!(
                store.build_packed_filter(|| Ok(())).unwrap(),
                "the build lands inside the install"
            );
        });
        let mut segments = manifest.segments.clone();
        segments.push(reference);
        store
            .install_payload_manifest(PayloadManifest::new(segments).unwrap())
            .unwrap();
        catalogued.extend(added);
        assert!(
            store.filter_covers_installed_catalog(&store.packed_filter.read().unwrap()),
            "saturated={saturated}: the install left its publication uncovered"
        );
        assert_exact(&store, &catalogued);
    }
}

/// Live 2026-11-09 each catalog publication rebuilt the routes of the whole
/// 4,096-segment catalog, about 250 ms of every spill's install on the
/// production-shaped fixture. A publication now moves the installed routes:
/// replacing one segment writes that segment's prefix routes and its
/// replacement's, however many segments stay.
#[test]
fn a_publication_moves_only_the_routes_of_the_segments_it_changes() {
    const SEGMENTS: usize = 24;
    let vfs = DeterministicVfs::new(None);
    let store = simulated_store(&vfs);
    // A budget that routes every segment of the catalog.
    store.set_segment_index_cache_bytes(64 << 20).unwrap();
    let mut catalog: Vec<PayloadSegmentReference> = (0..SEGMENTS)
        .map(|number| {
            segment_of(
                &store,
                number as u128,
                &payloads(&format!("routed-{number}"), 256),
            )
        })
        .collect();
    store
        .install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap())
        .unwrap();
    let prefixes_of = |reference: &PayloadSegmentReference| {
        let segment = store
            .payload_segment_store()
            .unwrap()
            .open_segment(reference.segment_id)
            .unwrap();
        segment
            .block_ids()
            .map(|id| [id.0[0], id.0[1]])
            .collect::<BTreeSet<_>>()
            .len() as u64
    };
    let replacement = segment_of(&store, 100, &payloads("replacement", 256));
    store.verify_payload_segment(&replacement).unwrap();
    let expected = prefixes_of(&catalog[0]) + prefixes_of(&replacement);
    catalog[0] = replacement;
    crate::payload_routing::route_counters::take_prefix_route_writes();
    store
        .install_payload_manifest(PayloadManifest::new(catalog.clone()).unwrap())
        .unwrap();
    assert_eq!(
        crate::payload_routing::route_counters::take_prefix_route_writes(),
        expected,
        "a one-segment publication rewrote the routes of segments it kept"
    );
    for reference in &catalog {
        let segment = store
            .payload_segment_store()
            .unwrap()
            .open_segment(reference.segment_id)
            .unwrap();
        for id in segment.block_ids() {
            assert!(
                store
                    .payload_segment_candidates(id)
                    .unwrap()
                    .contains(reference),
                "a block lost its route"
            );
        }
    }
}

/// A stop at any point abandons the build with nothing installed, and an
/// injected I/O error at any operation of the build leaves at most the
/// failing segment uncovered. A crash at that point (the build writes
/// nothing) reopens as a clean open, and a later build covers the catalog.
#[test]
fn a_stopped_failing_or_crashed_build_never_installs_unverified_coverage() {
    const SEGMENTS: usize = 4;
    for stop_at in 1..=SEGMENTS + 1 {
        let (_vfs, store, _manifest, catalogued) = reopened_catalog(SEGMENTS);
        let mut calls = 0;
        let stopped = store.build_packed_filter(|| {
            calls += 1;
            if calls == stop_at {
                Err(io::Error::new(io::ErrorKind::WouldBlock, "stopped"))
            } else {
                Ok(())
            }
        });
        assert_eq!(stopped.unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert!(
            !store.packed_filter.read().unwrap().built,
            "a stopped build installed a filter"
        );
        assert_exact(&store, &catalogued);
    }
    let operations = {
        let (vfs, store, _manifest, _catalogued) = reopened_catalog(SEGMENTS);
        vfs.arm_fault(None).unwrap();
        assert!(store.build_packed_filter(|| Ok(())).unwrap());
        vfs.trace().unwrap()
    };
    assert!(
        operations.contains(&Operation::Read),
        "the build must read indexes"
    );
    for operation_number in 1..=operations.len() as u64 {
        let (vfs, store, manifest, catalogued) = reopened_catalog(SEGMENTS);
        vfs.arm_fault(Some(FaultRule {
            operation_number,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        let _ = store.build_packed_filter(|| Ok(()));
        vfs.arm_fault(None).unwrap();
        assert_exact(&store, &catalogued);
        drop(store);
        vfs.crash().unwrap();
        let reopened = reopen(&vfs, &manifest);
        assert_exact(&reopened, &catalogued);
        assert!(reopened.build_packed_filter(|| Ok(())).unwrap());
        assert!(
            reopened.filter_covers_installed_catalog(&reopened.packed_filter.read().unwrap()),
            "fault at operation {operation_number}: the rebuilt filter covers the catalog"
        );
        assert_exact(&reopened, &catalogued);
    }
}

/// The certification drill's crash probe kills the engine while the
/// `packed_filter_build_pause` failpoint holds the build open. On the compact
/// production store (386 segments) the build finished in about 0.4 s, before
/// a fixed sleep elapsed; the pause holds it after its first index read
/// whatever the catalog size, up to the 4,096-segment ceiling, installs
/// nothing, and a crash there reopens as a clean open.
#[test]
fn the_build_pause_failpoint_holds_every_catalog_size_inside_the_build() {
    for segments in [386, 4_096] {
        let (vfs, store, manifest, catalogued) = reopened_catalog(segments);
        read_path_counters::take_index_work();
        let mut calls = 0;
        let stopped = store.build_packed_filter_pausing(
            || {
                calls += 1;
                // Unpaused, `check` runs once before each index read: its
                // third call would follow the second read.
                if calls == 3 {
                    assert_eq!(
                        read_path_counters::take_index_work().0,
                        1,
                        "{segments} segments: the build paused after its first index read"
                    );
                    assert!(
                        !store.packed_filter.read().unwrap().built,
                        "{segments} segments: the paused build installed its filter"
                    );
                    return Err(io::Error::new(io::ErrorKind::WouldBlock, "killed"));
                }
                Ok(())
            },
            true,
        );
        assert_eq!(stopped.unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert!(!store.packed_filter.read().unwrap().built);
        drop(store);
        vfs.crash().unwrap();
        let reopened = reopen(&vfs, &manifest);
        assert!(reopened.build_packed_filter(|| Ok(())).unwrap());
        assert!(reopened.filter_covers_installed_catalog(&reopened.packed_filter.read().unwrap()));
        if segments == 386 {
            assert_exact(&reopened, &catalogued);
        }
    }
}
