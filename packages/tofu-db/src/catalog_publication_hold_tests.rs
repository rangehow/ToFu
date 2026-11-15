//! Opt-in measurement of payload catalog publication holds on a
//! production-shaped catalog (docs/TOFUDB_BLOCK_READ_PATH.md, "Packed filter
//! built off the authority mutex"). `TOFU_DB_CATALOG_HOLD_BENCH_DIR` names the
//! volume under test; the fabricated authority there is reused by later runs.
//!
//! The fixture fills the 4,096-entry catalog the way the live store was on
//! 2026-11-09: 4,090 segments full by block count and six byte-heavy segments
//! that are the only ones an oversized commit's spill chunk fits, so every
//! spill takes the full-catalog merge exactly as in production. 268,300,288
//! blocks size the packed filter at 1 GiB, as for the live 234,070,323. The
//! fixture is left with an empty WAL, so each measured open is clean, like the
//! live restart's (`TOFU_DB_CATALOG_HOLD_BENCH_SETTLE=1` only re-settles it).
use super::*;
use crate::block::{BlockId, BlockStore};
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::MAX_SEGMENT_BLOCKS;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const FULL_SEGMENTS: usize = 4_090;
const HEAVY_SEGMENT_MIB: [usize; 6] = [128, 144, 160, 176, 192, 208];
const HEAVY_BLOCK_BYTES: usize = 4_096;
const SPILL_ROWS: u32 = 768;

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn full_payload(segment: usize, block: usize) -> Vec<u8> {
    let mut payload = vec![0x7a_u8; 16];
    payload[..4].copy_from_slice(&(segment as u32).to_le_bytes());
    payload[4..8].copy_from_slice(&(block as u32).to_le_bytes());
    payload
}

fn heavy_payload(segment: usize, block: usize) -> Vec<u8> {
    let mut payload = vec![(block % 251) as u8; HEAVY_BLOCK_BYTES];
    payload[..4].copy_from_slice(&(segment as u32).to_le_bytes());
    payload[4..8].copy_from_slice(&(block as u32).to_le_bytes());
    payload
}

/// The daemon's probe-derived budgets. Applied before any settling checkpoint
/// too: they size the read hints saved at close, which a clean open needs to
/// find a packed root page without walking the catalog.
fn configure_like_the_daemon(
    database: &mut AuthorityDatabase,
    directory: &Path,
) -> io::Result<crate::resource_probe::DaemonResourceBudget> {
    let mut snapshot = crate::resource_probe::probe_launch_resources(directory);
    snapshot.volume_free_bytes = Some(fs2::available_space(directory)?);
    let budget = crate::resource_probe::DaemonResourceBudget::from_snapshot_with_volume_class(
        snapshot,
        Some(crate::resource_probe::volume_class(directory)),
    );
    database.configure_hot_data_caches(budget.block_cache_bytes, budget.entity_page_cache_bytes)?;
    database.configure_write_path_budgets(
        budget.overlay_budget_bytes,
        budget.overlay_wal_watermark_bytes,
        budget.checkpoint_materialize_batch_bytes,
        budget.payload_segment_cache_bytes,
    )?;
    Ok(budget)
}

/// Retires the WAL window the way a graceful daemon stop does, so the next
/// open skips the recovery catalog pass.
fn settle(database: &mut AuthorityDatabase) -> io::Result<()> {
    database.merge_entity_overlay_fully()?;
    match database.checkpoint_wal_window() {
        Err(error) if error.kind() == io::ErrorKind::InvalidInput => Ok(()),
        result => result.map(|_| ()),
    }
}

/// Fabricates the production-shaped catalog beside one seeded entity and
/// publishes it.
fn fabricate(directory: &Path, full_segments: usize) -> io::Result<()> {
    let mut database = AuthorityDatabase::initialize(directory)?;
    configure_like_the_daemon(&mut database, directory)?;
    let mut seed = database.begin(7, 11)?;
    database.entity_put(
        &mut seed,
        crate::entity::EntityKey::new(7, 11, "semantic", b"seed")?,
        b"seed".to_vec(),
    )?;
    database.commit(seed)?;
    let blocks = database.block_store_shared();
    let store = blocks.payload_segment_store()?;
    let total = full_segments + HEAVY_SEGMENT_MIB.len();
    let next = AtomicUsize::new(0);
    let references = Mutex::new(Vec::with_capacity(total));
    let started = Instant::now();
    let workers = std::thread::available_parallelism()
        .map_or(8, |count| count.get())
        .min(48);
    std::thread::scope(|scope| -> io::Result<()> {
        let handles: Vec<_> = (0..workers)
            .map(|_| {
                scope.spawn(|| -> io::Result<()> {
                    loop {
                        let number = next.fetch_add(1, Ordering::Relaxed);
                        if number >= total {
                            return Ok(());
                        }
                        let payloads: HashMap<BlockId, Vec<u8>> = if number < full_segments {
                            (0..MAX_SEGMENT_BLOCKS)
                                .map(|block| full_payload(number, block))
                                .map(|payload| (BlockId::for_payload(&payload), payload))
                                .collect()
                        } else {
                            let mib = HEAVY_SEGMENT_MIB[number - full_segments];
                            (0..mib * 1024 * 1024 / HEAVY_BLOCK_BYTES)
                                .map(|block| heavy_payload(number, block))
                                .map(|payload| (BlockId::for_payload(&payload), payload))
                                .collect()
                        };
                        let segment = store.create(
                            Uuid::from_u128(
                                0xca7a_0000_0000_0000_0000_0000_0000_0000 | number as u128,
                            ),
                            payloads.keys().copied().collect(),
                            |id| Ok(payloads[&id].clone()),
                        )?;
                        references
                            .lock()
                            .unwrap()
                            .push(PayloadSegmentReference::new(
                                segment.metadata(),
                                segment.first_block_id(),
                                segment.last_block_id(),
                            )?);
                    }
                })
            })
            .collect();
        for handle in handles {
            handle
                .join()
                .map_err(|_| io::Error::other("fabrication worker panicked"))??;
        }
        Ok(())
    })?;
    let fabricated_ms = started.elapsed().as_millis();
    let mut catalog = references.into_inner().unwrap();
    if let Some(existing) = blocks.installed_payload_manifest()? {
        catalog.extend(existing.segments);
    }
    let segments = catalog.len();
    let catalog_blocks: u64 = catalog
        .iter()
        .map(|reference| u64::from(reference.block_count))
        .sum();
    let started = Instant::now();
    database
        .engine_mut_for_test()
        .publish_payload_manifest_for_test(PayloadManifest::new(catalog)?)?;
    let publish_ms = started.elapsed().as_millis();
    settle(&mut database)?;
    println!(
        "{{\"step\":\"fabricated\",\"segments\":{segments},\"blocks\":{catalog_blocks},\
         \"fabricate_ms\":{fabricated_ms},\"publish_ms\":{publish_ms}}}"
    );
    Ok(())
}

/// One reference commit whose fresh copy-on-write pages exceed the WAL carry
/// ceiling, so it spills through the segment packer.
fn commit_spill(database: &mut AuthorityDatabase, commit: usize) -> io::Result<()> {
    let mut transaction = database.begin(7, 11)?;
    for row in 0..SPILL_ROWS {
        let mut value = vec![(row % 251) as u8; 4096];
        value[..4].copy_from_slice(&row.to_be_bytes());
        value[4..12].copy_from_slice(&(commit as u64).to_be_bytes());
        database.entity_put(
            &mut transaction,
            crate::entity::EntityKey::new(
                7,
                11,
                "semantic",
                format!("spill-{commit:05}-{row:05}").as_bytes(),
            )?,
            value,
        )?;
    }
    database.commit(transaction).map(|_| ())
}

const MEASURED_STAGES: [(&str, usize); 6] = [
    ("catalog_install", crate::fsync_stats::STAGE_CATALOG_INSTALL),
    ("catalog_publish", crate::fsync_stats::STAGE_CATALOG_PUBLISH),
    ("step_chunk", crate::fsync_stats::STAGE_STEP_CHUNK),
    ("pack_filter", crate::fsync_stats::STAGE_PACK_FILTER),
    ("catalog_control", crate::fsync_stats::STAGE_CATALOG_CONTROL),
    ("catalog_write", crate::fsync_stats::STAGE_CATALOG_WRITE),
];

fn stage_snapshot() -> Vec<u64> {
    MEASURED_STAGES
        .iter()
        .map(|(_, stage)| crate::fsync_stats::stage_nanos(*stage))
        .collect()
}

fn stage_delta_json(before: &[u64]) -> String {
    MEASURED_STAGES
        .iter()
        .zip(before)
        .map(|((name, stage), before)| {
            format!(
                "\"{name}_ms\":{:.1}",
                (crate::fsync_stats::stage_nanos(*stage) - before) as f64 / 1e6
            )
        })
        .collect::<Vec<_>>()
        .join(",")
}

/// (built, covers the installed catalog).
fn filter_state(blocks: &BlockStore) -> (bool, bool) {
    blocks.packed_filter_state_for_test()
}

/// The online GC driver's off-mutex work on its own thread, in the driver's
/// order: the first catalog location table, then the packed filter.
fn spawn_filter_build(
    blocks: Arc<BlockStore>,
) -> Option<std::thread::JoinHandle<(bool, Duration)>> {
    Some(std::thread::spawn(move || {
        let started = Instant::now();
        blocks.publish_first_catalog_locations(|| Ok(())).unwrap();
        println!(
            "{{\"step\":\"first_location_table\",\"elapsed_ms\":{}}}",
            started.elapsed().as_millis()
        );
        let installed = blocks.build_packed_filter(|| Ok(())).unwrap();
        (installed, started.elapsed())
    }))
}

/// Clean open, then `TOFU_DB_CATALOG_HOLD_BENCH_COMMITS` spill commits, the
/// first of which is the first catalog publication of the process. With
/// `TOFU_DB_CATALOG_HOLD_BENCH_BUILDER=1` a thread does the online GC driver's
/// off-mutex work (first location table, then the packed filter) and
/// `TOFU_DB_CATALOG_HOLD_BENCH_AFTER` more commits follow its completion;
/// `TOFU_DB_CATALOG_HOLD_BENCH_WAIT_FOR_TABLE=1` starts the first commit only
/// once the table is published, as on the live restart (the first spill came
/// at 11:07:37.9, right after the table).
#[cfg(target_os = "linux")]
#[test]
#[ignore]
fn catalog_publication_hold_measurement() {
    let Some(root) = std::env::var_os("TOFU_DB_CATALOG_HOLD_BENCH_DIR") else {
        return;
    };
    let directory = PathBuf::from(root).join("authority");
    if !directory.join("CONTROL").exists() {
        std::fs::create_dir_all(&directory).unwrap();
        fabricate(
            &directory,
            env_usize("TOFU_DB_CATALOG_HOLD_BENCH_FULL_SEGMENTS", FULL_SEGMENTS),
        )
        .unwrap();
    }
    let started = Instant::now();
    let mut database = AuthorityDatabase::open(&directory).unwrap();
    let open_ms = started.elapsed().as_millis();
    let blocks = database.block_store_shared();
    let (built_at_open, _) = filter_state(&blocks);
    let budget = configure_like_the_daemon(&mut database, &directory).unwrap();
    if std::env::var_os("TOFU_DB_CATALOG_HOLD_BENCH_SETTLE").is_some() {
        settle(&mut database).unwrap();
        println!("{{\"step\":\"settled\",\"open_ms\":{open_ms},\"filter_built_at_open\":{built_at_open}}}");
        return;
    }
    let commits = env_usize("TOFU_DB_CATALOG_HOLD_BENCH_COMMITS", 8);
    let after = env_usize("TOFU_DB_CATALOG_HOLD_BENCH_AFTER", 4);
    database.set_write_overlay_for_test(false);
    let segments = database.writer_engine().unwrap().payload_segment_count();
    println!(
        "{{\"step\":\"opened\",\"open_ms\":{open_ms},\"segments\":{segments},\
         \"filter_built_at_open\":{built_at_open},\"payload_segment_cache_bytes\":{}}}",
        budget.payload_segment_cache_bytes
    );
    let mut builder = if std::env::var_os("TOFU_DB_CATALOG_HOLD_BENCH_BUILDER").is_some() {
        spawn_filter_build(Arc::clone(&blocks))
    } else {
        None
    };
    if builder.is_some() && std::env::var_os("TOFU_DB_CATALOG_HOLD_BENCH_WAIT_FOR_TABLE").is_some()
    {
        while blocks.published_catalog_locations_for_test().is_none() {
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    let commit_base = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs() as usize % 100_000)
        * 64;
    let mut index = 0;
    let mut measure =
        |database: &mut AuthorityDatabase,
         builder: &Option<std::thread::JoinHandle<(bool, Duration)>>| {
            let during_build = builder.as_ref().is_some_and(|handle| !handle.is_finished());
            let before = stage_snapshot();
            let started = Instant::now();
            commit_spill(database, commit_base + index).unwrap();
            let elapsed = started.elapsed();
            let (built, covered) = filter_state(&blocks);
            println!(
            "{{\"step\":\"commit\",\"index\":{index},\"commit_ms\":{:.1},\"started_during_build\":{during_build},\
             \"build_running_after\":{},\"filter_built\":{built},\"filter_covers\":{covered},{}}}",
            elapsed.as_secs_f64() * 1e3,
            builder.as_ref().is_some_and(|handle| !handle.is_finished()),
            stage_delta_json(&before)
        );
            index += 1;
        };
    for _ in 0..commits {
        measure(&mut database, &builder);
        std::thread::sleep(Duration::from_millis(10));
    }
    if let Some(handle) = builder.take() {
        let (installed, elapsed) = handle.join().unwrap();
        let (built, covered) = filter_state(&blocks);
        println!(
            "{{\"step\":\"filter_built\",\"installed\":{installed},\"build_ms\":{},\"filter_built\":{built},\
             \"filter_covers\":{covered}}}",
            elapsed.as_millis()
        );
        for _ in 0..after {
            measure(&mut database, &builder);
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    drop(database);
}
