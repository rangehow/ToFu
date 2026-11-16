//! Window-local liveness at materialization (R5-SP physical half, DB-5's
//! spec 3): a page staged and then superseded inside one WAL window is never
//! materialized. A synchronous-write store (the search projection's shape:
//! no write overlay, no rotation driver) rewrites one row many times inside
//! one window; the checkpoint that retires the window may materialize only
//! what the final root reaches, so its output cannot grow with the number of
//! superseded versions.
use crate::authority::AuthorityDatabase;
use crate::entity::EntityKey;

/// Blocks and payload bytes one checkpoint materialized after `versions`
/// rewrites of the same row inside a single WAL window.
fn materialized_after_rewrites(versions: u32) -> (u64, u64) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_enabled(false);
    let catalogued = |database: &mut AuthorityDatabase| {
        let engine = database.engine_mut_for_test();
        let (blocks, bytes) = engine.payload_manifest.as_ref().map_or((0, 0), |manifest| {
            manifest
                .segments
                .iter()
                .fold((0u64, 0u64), |(blocks, bytes), segment| {
                    (
                        blocks + u64::from(segment.block_count),
                        bytes + segment.payload_bytes,
                    )
                })
        });
        let loose = engine.block_write_metrics();
        (blocks + loose.blocks_written, bytes + loose.bytes_written)
    };
    // A first window holds the row once, so every measured window starts
    // from the same materialized tree.
    let mut transaction = database.begin(7, 11).unwrap();
    let key = EntityKey::new(7, 11, "window_liveness", b"row").unwrap();
    database
        .entity_put(&mut transaction, key.clone(), vec![0; 4096])
        .unwrap();
    database.commit(transaction).unwrap();
    database.checkpoint_wal_window().unwrap();
    let (blocks_before, bytes_before) = catalogued(&mut database);
    for version in 1..=versions {
        let mut transaction = database.begin(7, 11).unwrap();
        let mut value = vec![0; 4096];
        value[..4].copy_from_slice(&version.to_be_bytes());
        database
            .entity_put(&mut transaction, key.clone(), value)
            .unwrap();
        database.commit(transaction).unwrap();
    }
    database.checkpoint_wal_window().unwrap();
    let (blocks_after, bytes_after) = catalogued(&mut database);
    (blocks_after - blocks_before, bytes_after - bytes_before)
}

#[test]
#[ignore = "R5-SP stage 2 failing-first spec (DB-5 spec 3): passes once checkpoint materialization keeps only staged pages reachable from the window's roots and live pins"]
fn a_staged_page_superseded_within_its_window_is_never_materialized() {
    let (one_blocks, one_bytes) = materialized_after_rewrites(1);
    let (many_blocks, many_bytes) = materialized_after_rewrites(64);
    eprintln!(
        "window_liveness_probe one_version_blocks={one_blocks} one_version_bytes={one_bytes} \
         sixty_four_versions_blocks={many_blocks} sixty_four_versions_bytes={many_bytes}"
    );
    assert_eq!(
        (many_blocks, many_bytes),
        (one_blocks, one_bytes),
        "a window that superseded 63 versions of a row materialized them"
    );
}
