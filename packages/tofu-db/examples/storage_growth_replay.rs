//! Replays this release's storage-growth reclamation on a COPY of a closed
//! authority or search projection and prints before/after measurements.
//! Usage: cargo run --release --example storage_growth_replay -- <copy-dir> [retain-history <segments>] [epochs <n>]
//! `retain-history` retires history down to that many segments first (the
//! search projection's maintenance does this online); `epochs` then runs that
//! many online-GC epochs back to back. Never point it at a live store.
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tofu_db::authority::AuthorityDatabase;
use tofu_db::authority_gc::AuthorityGarbageCollectionBudget;
use tofu_db::authority_gc_online::OnlineGarbageCollector;

fn store_bytes(directory: &Path) -> std::io::Result<(u64, u64)> {
    let mut bytes = 0;
    let mut files = 0;
    let mut pending = vec![directory.join("payload-segments"), directory.join("blocks")];
    while let Some(next) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&next) else {
            continue;
        };
        for entry in entries {
            let entry = entry?;
            let metadata = entry.metadata()?;
            if metadata.is_dir() {
                pending.push(entry.path());
            } else {
                bytes += metadata.len();
                files += 1;
            }
        }
    }
    Ok((bytes, files))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut arguments = std::env::args().skip(1);
    let directory = std::path::PathBuf::from(arguments.next().ok_or("copy directory required")?);
    let mut retain_history = None;
    let mut epochs = 0_usize;
    while let Some(flag) = arguments.next() {
        let value = arguments.next().ok_or("flag value required")?;
        match flag.as_str() {
            "retain-history" => retain_history = Some(value.parse::<usize>()?),
            "epochs" => epochs = value.parse()?,
            _ => return Err("unknown flag".into()),
        }
    }
    let (bytes, files) = store_bytes(&directory)?;
    println!("before store_bytes={bytes} files={files}");
    let started = Instant::now();
    let mut database = AuthorityDatabase::open(&directory)?;
    println!("open_ms={}", started.elapsed().as_millis());
    if let Some(retained) = retain_history {
        let started = Instant::now();
        let metrics = database.compact_history(retained)?;
        println!(
            "history_retirement retired_segments={} retained_segments={} elapsed_ms={}",
            metrics.retired_segments,
            metrics.retained_segments,
            started.elapsed().as_millis()
        );
    }
    let database = Arc::new(Mutex::new(database));
    let mut collector = OnlineGarbageCollector::with_min_epoch_interval(
        AuthorityGarbageCollectionBudget::conservative(),
        Duration::ZERO,
    );
    let stop = AtomicBool::new(false);
    for epoch in 0..epochs {
        let started = Instant::now();
        let outcome = collector.run_epoch(&database, &stop)?;
        let metrics = collector.handle().metrics();
        let (bytes, files) = store_bytes(&directory)?;
        println!(
            "epoch={epoch} outcome={outcome:?} elapsed_ms={} reclaimed_bytes_total={} segments_rewritten={} segments_retired={} consolidations={} store_bytes={bytes} files={files}",
            started.elapsed().as_millis(),
            metrics.total_reclaimed_bytes,
            metrics.total_segments_rewritten,
            metrics.total_segments_retired,
            metrics.total_consolidations,
        );
    }
    Ok(())
}
