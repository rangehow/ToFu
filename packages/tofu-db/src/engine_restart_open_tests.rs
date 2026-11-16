//! Dirty-open (unclean exit) recovery cost: an explicit measurement harness on
//! a disposable real-filesystem authority. Three ignored tests, each run in its
//! own process (method and results: `docs/TOFUDB_RECOVERY_ATTRIBUTION.md`,
//! "Reopen time against WAL size"):
//!
//! 1. `dirty_open_build_base` writes retained indexed history and a payload
//!    catalog at production scale (4,092 segments of 57,000 blocks by default,
//!    against production's 234.5 M located blocks), then closes cleanly.
//! 2. `dirty_open_build_window` reopens a copy of that base and commits an
//!    unretired window until the active WAL (and, for the sealed shape, the
//!    CONTROL-owned sealed predecessor) holds the requested bytes. It ends the
//!    process without engine teardown, which leaves what a SIGKILL leaves: no
//!    rotation, no CONTROL catch-up after the last commits, no read-hint save.
//! 3. `dirty_open_phase_measurement` opens the crashed fixture with a cold page
//!    cache, checks the reopened durable sequence, reads back with exact bytes
//!    every carried window block and a sample of the catalogued and history
//!    blocks the window references, and prints one
//!    `dirty_open_result` JSON line. A diagnostic build
//!    (`--features recovery-profile`, `TOFU_DB_RECOVERY_PROFILE=1`) adds the
//!    engine's own phase report; a sampler records process CPU, open-thread
//!    CPU and I/O counters at a fixed period so the phases can be attributed.
//!
//! The window follows production's WAL composition: about 360 distinct
//! references per MiB (the 572,675,112-byte incident WAL held 226,130) and a
//! 27% inline share (34.7 of 129.8 MB in the 2026-10-13 composition
//! measurement). Each round commits four transactions with 64 references and
//! 48 KiB of inline payload each: carried 4 KiB payloads that the packer
//! publishes after their commit, carried 4 KiB payloads still pending at the
//! crash, identifier-only references to random catalogued blocks, and payloads
//! that repeat retained history content.
use super::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering as AtomicOrdering};

const BASE_FILE: &str = "bench-base-complete";
const FIXTURE_FILE: &str = "bench-fixture-complete";
const WINDOW_REFERENCES_PER_COMMIT: usize = 64;
const WINDOW_PAYLOAD_BYTES: usize = 4096;
const WINDOW_INLINE_BYTES: usize = 48 * 1024;
/// Catalog slots one window may add for the payloads packed after commit.
const WINDOW_PACKED_SEGMENT_SLOTS: usize = 4;
/// Below both segment bounds (65,536 blocks, 256 MiB of payload).
const WINDOW_PACKED_BLOCKS_PER_SEGMENT: usize = 60_000;
/// Fixture construction only: keep every catalogued index resident while the
/// window commits identifier-only references. The measured open runs in a new
/// process with the engine's default budgets.
const FIXTURE_INDEX_CACHE_BYTES: usize = 16 << 30;
/// Fixture construction only: every commit's dedupe probe consults retained
/// history, so a block cache that holds the routing pages keeps the build from
/// rereading a 4 MiB page per probe. The measured open keeps the engine's
/// default budgets, as the daemon's open does (it sizes caches after open).
const FIXTURE_BLOCK_CACHE_BYTES: usize = 16 << 30;
const SAMPLE_PERIOD: std::time::Duration = std::time::Duration::from_millis(20);

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

fn bench_payload(kind: &str, first: usize, second: usize) -> Vec<u8> {
    format!("{kind}:{first}:{second}").into_bytes()
}

/// Incompressible, reproducible window bytes: the build and the read-back
/// check derive the same payload from its kind and index.
fn window_payload(kind: &str, index: usize, bytes: usize) -> Vec<u8> {
    let mut hasher = blake3::Hasher::new();
    hasher.update(kind.as_bytes());
    hasher.update(&(index as u64).to_le_bytes());
    let mut payload = vec![0; bytes];
    hasher.finalize_xof().fill(&mut payload);
    payload
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn bench_directory() -> PathBuf {
    std::env::var_os("TOFUDB_RESTART_BENCH_DIR")
        .map(PathBuf::from)
        .expect("TOFUDB_RESTART_BENCH_DIR names the disposable authority")
}

struct BaseShape {
    segments: usize,
    blocks_per_segment: usize,
    history_locations: usize,
}

impl BaseShape {
    fn from_environment() -> Self {
        let segment_slots = MAX_PAYLOAD_SEGMENTS - WINDOW_PACKED_SEGMENT_SLOTS;
        Self {
            segments: env_usize("TOFUDB_RESTART_BENCH_SEGMENTS", segment_slots).min(segment_slots),
            blocks_per_segment: env_usize("TOFUDB_RESTART_BENCH_BLOCKS_PER_SEGMENT", 57_000)
                .min(crate::payload_segment::MAX_SEGMENT_BLOCKS),
            history_locations: env_usize("TOFUDB_RESTART_BENCH_HISTORY_LOCATIONS", 1_000_000),
        }
    }

    /// The window and the read-back check use the shape the base recorded.
    fn from_base(directory: &Path) -> Self {
        let recorded: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(directory.join(BASE_FILE)).expect("a completed base"),
        )
        .unwrap();
        let field = |name: &str| recorded[name].as_u64().unwrap() as usize;
        Self {
            segments: field("segments"),
            blocks_per_segment: field("blocksPerSegment"),
            history_locations: field("historyLocations"),
        }
    }

    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "segments": self.segments,
            "blocksPerSegment": self.blocks_per_segment,
            "historyLocations": self.history_locations,
        })
    }

    /// A random catalogued block (both coordinates from one 64-bit mix).
    fn catalog_coordinates(&self, index: usize) -> (usize, usize) {
        let mixed = (index as u64 ^ 0x5DEE_CE66_D1CE_4E5B).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        let segment = (mixed % self.segments as u64) as usize;
        let block = ((mixed >> 32) % self.blocks_per_segment as u64) as usize;
        (segment, block)
    }

    fn history_index(&self, index: usize) -> usize {
        (index * 104_729) % self.history_locations
    }
}

fn create_bench_segments(
    store: &crate::payload_segment::PayloadSegmentStore,
    shape: &BaseShape,
) -> Vec<PayloadSegmentReference> {
    let next = std::sync::atomic::AtomicUsize::new(0);
    let references = Mutex::new(Vec::with_capacity(shape.segments));
    std::thread::scope(|scope| {
        for _ in 0..32 {
            scope.spawn(|| loop {
                let segment = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if segment >= shape.segments {
                    return;
                }
                let payloads: std::collections::HashMap<BlockId, Vec<u8>> = (0..shape
                    .blocks_per_segment)
                    .map(|block| {
                        let payload = bench_payload("catalog", segment, block);
                        (BlockId::for_payload(&payload), payload)
                    })
                    .collect();
                let created = store
                    .create(Uuid::new_v4(), payloads.keys().copied().collect(), |id| {
                        Ok(payloads[&id].clone())
                    })
                    .unwrap();
                let reference = PayloadSegmentReference::new(
                    created.metadata(),
                    created.first_block_id(),
                    created.last_block_id(),
                )
                .unwrap();
                references.lock().unwrap().push(reference);
            });
        }
    });
    references.into_inner().unwrap()
}

#[test]
#[ignore = "explicit dirty-open measurement: builds retained history and the payload catalog"]
fn dirty_open_build_base() {
    let directory = bench_directory();
    let shape = BaseShape::from_environment();
    let started = std::time::Instant::now();
    let mut engine = Engine::initialize(&directory).unwrap();
    // The explicit rotations below own retirement; no commit starts one.
    engine.set_rotation_threshold_bytes_for_test(u64::MAX);
    engine
        .blocks
        .set_cache_capacity_bytes(FIXTURE_BLOCK_CACHE_BYTES)
        .unwrap();
    let per_commit = 4096;
    let per_rotation = (shape.history_locations / 8).max(per_commit);
    let mut written = 0;
    while written < shape.history_locations {
        let end = (written + per_rotation).min(shape.history_locations);
        let mut next = written;
        while next < end {
            let batch_end = (next + per_commit).min(end);
            let payloads: Vec<Vec<u8>> = (next..batch_end)
                .map(|index| bench_payload("history", index, 0))
                .collect();
            let borrowed: Vec<&[u8]> = payloads.iter().map(Vec::as_slice).collect();
            engine.commit_transaction(b"history", &borrowed).unwrap();
            next = batch_end;
        }
        rotate_into_indexed_history(&mut engine);
        written = end;
    }
    let history_elapsed_ms = started.elapsed().as_millis();
    let blocks = Arc::clone(&engine.blocks);
    let store = blocks.payload_segment_store().unwrap();
    let references = create_bench_segments(store, &shape);
    let segments_elapsed_ms = started.elapsed().as_millis();
    engine
        .publish_payload_manifest_for_test(PayloadManifest::new(references).unwrap())
        .unwrap();
    drop(engine);
    let mut recorded = shape.to_json();
    recorded["buildMillis"] = serde_json::json!({
        "history": history_elapsed_ms,
        "segments": segments_elapsed_ms,
        "total": started.elapsed().as_millis(),
    });
    std::fs::write(directory.join(BASE_FILE), recorded.to_string()).unwrap();
    eprintln!("dirty_open_base {recorded}");
}

struct WindowShape {
    active_bytes: u64,
    sealed_bytes: u64,
}

impl WindowShape {
    fn from_environment() -> Self {
        let bytes = |name: &str| {
            std::env::var(name)
                .ok()
                .and_then(|value| value.parse().ok())
        };
        Self {
            active_bytes: bytes("TOFUDB_RESTART_BENCH_WAL_BYTES")
                .expect("TOFUDB_RESTART_BENCH_WAL_BYTES names the active window's bytes"),
            sealed_bytes: bytes("TOFUDB_RESTART_BENCH_SEALED_WAL_BYTES").unwrap_or(0),
        }
    }
}

/// How far each reference kind has been committed; the read-back check
/// regenerates every referenced block from these counts.
#[derive(Default)]
struct WindowCursor {
    commits: usize,
    packed: usize,
    pending: usize,
    identifier_only: usize,
    history: usize,
}

impl WindowCursor {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "commits": self.commits,
            "packedAfterCommit": self.packed,
            "pendingAtCrash": self.pending,
            "identifierOnly": self.identifier_only,
            "historyRepeats": self.history,
        })
    }

    fn from_json(value: &serde_json::Value) -> Self {
        let field = |name: &str| value[name].as_u64().unwrap() as usize;
        Self {
            commits: field("commits"),
            packed: field("packedAfterCommit"),
            pending: field("pendingAtCrash"),
            identifier_only: field("identifierOnly"),
            history: field("historyRepeats"),
        }
    }
}

fn commit_window_payloads(
    engine: &mut Engine,
    cursor: &mut WindowCursor,
    kind: &str,
    payloads: &[Vec<u8>],
) {
    let inline = window_payload("inline", cursor.commits, WINDOW_INLINE_BYTES);
    let borrowed: Vec<&[u8]> = payloads.iter().map(Vec::as_slice).collect();
    engine
        .commit_transaction(&[kind.as_bytes(), inline.as_slice()].concat(), &borrowed)
        .unwrap();
    cursor.commits += 1;
}

fn commit_window_round(engine: &mut Engine, base: &BaseShape, cursor: &mut WindowCursor) {
    let range = |start: usize| start..start + WINDOW_REFERENCES_PER_COMMIT;
    let packed: Vec<Vec<u8>> = range(cursor.packed)
        .map(|index| window_payload("window-packed", index, WINDOW_PAYLOAD_BYTES))
        .collect();
    commit_window_payloads(engine, cursor, "window-packed", &packed);
    cursor.packed += WINDOW_REFERENCES_PER_COMMIT;
    let pending: Vec<Vec<u8>> = range(cursor.pending)
        .map(|index| window_payload("window-pending", index, WINDOW_PAYLOAD_BYTES))
        .collect();
    commit_window_payloads(engine, cursor, "window-pending", &pending);
    cursor.pending += WINDOW_REFERENCES_PER_COMMIT;
    let catalogued: Vec<BlockId> = range(cursor.identifier_only)
        .map(|index| {
            let (segment, block) = base.catalog_coordinates(index);
            BlockId::for_payload(&bench_payload("catalog", segment, block))
        })
        .collect();
    let inline = window_payload("inline", cursor.commits, WINDOW_INLINE_BYTES);
    engine
        .commit_references(
            &[b"window-identifier-only".as_slice(), inline.as_slice()].concat(),
            &catalogued,
        )
        .unwrap();
    cursor.commits += 1;
    cursor.identifier_only += WINDOW_REFERENCES_PER_COMMIT;
    if base.history_locations > 0 {
        let repeats: Vec<Vec<u8>> = range(cursor.history)
            .map(|index| bench_payload("history", base.history_index(index), 0))
            .collect();
        commit_window_payloads(engine, cursor, "window-history", &repeats);
        cursor.history += WINDOW_REFERENCES_PER_COMMIT;
    }
}

fn fill_window(
    engine: &mut Engine,
    base: &BaseShape,
    active_bytes: u64,
    cursor: &mut WindowCursor,
) {
    while engine.wal.len() < active_bytes {
        commit_window_round(engine, base, cursor);
    }
}

/// The packer's publication after commit: every packed-kind payload lands in
/// a catalogued segment while its WAL record still carries it.
fn publish_packed_after_commit(engine: &mut Engine, cursor: &WindowCursor) {
    let blocks = Arc::clone(&engine.blocks);
    let store = blocks.payload_segment_store().unwrap();
    let mut references = engine
        .payload_manifest
        .as_ref()
        .map_or_else(Vec::new, |manifest| manifest.segments.clone());
    for start in (0..cursor.packed).step_by(WINDOW_PACKED_BLOCKS_PER_SEGMENT) {
        let payloads: std::collections::HashMap<BlockId, Vec<u8>> = (start
            ..(start + WINDOW_PACKED_BLOCKS_PER_SEGMENT).min(cursor.packed))
            .map(|index| {
                let payload = window_payload("window-packed", index, WINDOW_PAYLOAD_BYTES);
                (BlockId::for_payload(&payload), payload)
            })
            .collect();
        let created = store
            .create(Uuid::new_v4(), payloads.keys().copied().collect(), |id| {
                Ok(payloads[&id].clone())
            })
            .unwrap();
        references.push(
            PayloadSegmentReference::new(
                created.metadata(),
                created.first_block_id(),
                created.last_block_id(),
            )
            .unwrap(),
        );
    }
    assert!(
        references.len() <= MAX_PAYLOAD_SEGMENTS,
        "the window's packed segments exceed the catalog"
    );
    engine
        .publish_payload_manifest_for_test(PayloadManifest::new(references).unwrap())
        .unwrap();
}

fn wal_file_sizes(directory: &Path) -> serde_json::Map<String, serde_json::Value> {
    let mut sizes = serde_json::Map::new();
    for entry in std::fs::read_dir(directory).unwrap().flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.ends_with(".wal") {
            sizes.insert(name, serde_json::json!(entry.metadata().unwrap().len()));
        }
    }
    sizes
}

#[test]
#[ignore = "explicit dirty-open measurement: commits an unretired window over a copied base, then crashes"]
fn dirty_open_build_window() {
    let directory = bench_directory();
    let base = BaseShape::from_base(&directory);
    assert!(
        !directory.join(FIXTURE_FILE).exists(),
        "this copy already holds a window"
    );
    let window = WindowShape::from_environment();
    let started = std::time::Instant::now();
    let mut engine = Engine::open(&directory).unwrap();
    engine.set_rotation_threshold_bytes_for_test(u64::MAX);
    engine
        .blocks
        .set_cache_capacity_bytes(FIXTURE_BLOCK_CACHE_BYTES)
        .unwrap();
    engine
        .blocks
        .set_segment_index_cache_bytes(FIXTURE_INDEX_CACHE_BYTES)
        .unwrap();
    // Routes let identifier-only commits resolve each catalogued block directly.
    engine.blocks.build_packed_filter(|| Ok(())).unwrap();
    let mut cursor = WindowCursor::default();
    let mut sealed_commits = 0;
    if window.sealed_bytes > 0 {
        fill_window(&mut engine, &base, window.sealed_bytes, &mut cursor);
        // What a rotation's generation handover leaves before its encoding:
        // CONTROL owns the sealed predecessor and an empty successor.
        engine.seal_wal_for_checkpoint().unwrap();
        sealed_commits = cursor.commits;
    }
    fill_window(&mut engine, &base, window.active_bytes, &mut cursor);
    publish_packed_after_commit(&mut engine, &cursor);
    // At a crash CONTROL lags the WAL: the last round follows the last publication.
    commit_window_round(&mut engine, &base, &mut cursor);
    let durable = engine.state.durable_sequence;
    let recorded = serde_json::json!({
        "base": base.to_json(),
        "activeBytesTarget": window.active_bytes,
        "sealedBytesTarget": window.sealed_bytes,
        "sealedCommits": sealed_commits,
        "cursor": cursor.to_json(),
        "walFiles": wal_file_sizes(&directory),
        "durableSequence": durable,
        "controlDurableSequence": ControlFile::open_with_vfs(&directory, Arc::new(RealVfs)).unwrap()
            .read_current().unwrap().durable_sequence,
        "buildMillis": started.elapsed().as_millis(),
    });
    std::fs::write(
        directory.join(FIXTURE_FILE),
        format!("dirty-open window\n{durable}\n{recorded}\n"),
    )
    .unwrap();
    eprintln!("dirty_open_window {recorded}");
    // No engine teardown: the process ends as a SIGKILL would end it.
    std::mem::forget(engine);
}

/// Drops the kernel page cache of every fixture file this process can open,
/// so each measured open reads cold from the volume (no privileges needed).
fn drop_fixture_page_cache(directory: &Path) {
    let mut pending = vec![directory.to_path_buf()];
    while let Some(path) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&path) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                pending.push(path);
            } else if let Ok(file) = std::fs::File::open(&path) {
                use std::os::fd::AsRawFd;
                let _ = file.sync_all();
                // SAFETY: the descriptor stays open for the duration of the call.
                unsafe { libc::posix_fadvise(file.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED) };
            }
        }
    }
}

fn process_io() -> std::collections::BTreeMap<String, u64> {
    std::fs::read_to_string("/proc/self/io")
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let (key, value) = line.split_once(':')?;
            Some((key.trim().to_owned(), value.trim().parse().ok()?))
        })
        .collect()
}

/// utime + stime of every thread of this process, in clock ticks.
fn process_cpu_ticks() -> u64 {
    let stat = std::fs::read_to_string("/proc/self/stat").unwrap_or_default();
    let Some(close) = stat.rfind(')') else {
        return 0;
    };
    let fields: Vec<&str> = stat[close + 1..].split_whitespace().collect();
    let field = |index: usize| {
        fields
            .get(index)
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0)
    };
    // After the command: state is field 3 (index 0), utime 14, stime 15.
    field(11) + field(12)
}

fn thread_cpu_nanos(thread: i64) -> u64 {
    std::fs::read_to_string(format!("/proc/self/task/{thread}/schedstat"))
        .ok()
        .and_then(|text| {
            text.split_whitespace()
                .next()
                .and_then(|value| value.parse().ok())
        })
        .unwrap_or(0)
}

fn peak_resident_kib() -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .unwrap_or_default()
        .lines()
        .find_map(|line| line.strip_prefix("VmHWM:"))
        .and_then(|value| value.trim().trim_end_matches("kB").trim().parse().ok())
        .unwrap_or(0)
}

/// Fixed-period samples of CPU and I/O counters, timed from `origin`.
struct Sampler {
    stop: Arc<AtomicBool>,
    handle: std::thread::JoinHandle<Vec<String>>,
}

impl Sampler {
    fn start(origin: std::time::Instant, open_thread: i64) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&stop);
        let handle = std::thread::spawn(move || {
            let mut rows = vec!["t_ns,process_cpu_ticks,open_thread_cpu_ns,rchar,read_bytes,syscr,wchar,write_bytes".to_owned()];
            loop {
                let io = process_io();
                let value = |key: &str| io.get(key).copied().unwrap_or(0);
                rows.push(format!(
                    "{},{},{},{},{},{},{},{}",
                    origin.elapsed().as_nanos(),
                    process_cpu_ticks(),
                    thread_cpu_nanos(open_thread),
                    value("rchar"),
                    value("read_bytes"),
                    value("syscr"),
                    value("wchar"),
                    value("write_bytes")
                ));
                if flag.load(AtomicOrdering::Acquire) {
                    return rows;
                }
                std::thread::sleep(SAMPLE_PERIOD);
            }
        });
        Self { stop, handle }
    }

    fn finish(self) -> Vec<String> {
        self.stop.store(true, AtomicOrdering::Release);
        self.handle.join().unwrap()
    }
}

/// Reads back, with exact bytes, every carried block the window wrote and a
/// deterministic sample of the blocks it referenced from the catalog and from
/// history (every 64th, plus the last round). Point reads of catalogued blocks
/// before the catalog location table exists cost an index probe each, which
/// is not the open this harness measures.
fn verify_window_reads(engine: &Engine, base: &BaseShape, cursor: &WindowCursor) -> u64 {
    engine
        .blocks
        .set_cache_capacity_bytes(FIXTURE_BLOCK_CACHE_BYTES)
        .unwrap();
    engine
        .blocks
        .set_segment_index_cache_bytes(FIXTURE_INDEX_CACHE_BYTES)
        .unwrap();
    let sampled = |index: usize, count: usize| {
        index.is_multiple_of(64) || index + WINDOW_REFERENCES_PER_COMMIT >= count
    };
    let mut verified = 0;
    let mut check = |payload: Vec<u8>| {
        let read = engine.blocks.get(BlockId::for_payload(&payload)).unwrap();
        assert_eq!(read, payload, "a window block reads back different bytes");
        verified += 1;
    };
    for index in 0..cursor.packed {
        check(window_payload("window-packed", index, WINDOW_PAYLOAD_BYTES));
    }
    for index in 0..cursor.pending {
        check(window_payload(
            "window-pending",
            index,
            WINDOW_PAYLOAD_BYTES,
        ));
    }
    for index in (0..cursor.identifier_only).filter(|index| sampled(*index, cursor.identifier_only))
    {
        let (segment, block) = base.catalog_coordinates(index);
        check(bench_payload("catalog", segment, block));
    }
    for index in (0..cursor.history).filter(|index| sampled(*index, cursor.history)) {
        check(bench_payload("history", base.history_index(index), 0));
    }
    verified
}

#[test]
#[ignore = "explicit dirty-open measurement on a disposable authority"]
fn dirty_open_phase_measurement() {
    let directory = bench_directory();
    let fixture =
        std::fs::read_to_string(directory.join(FIXTURE_FILE)).expect("a crashed window fixture");
    let durable: u64 = fixture.lines().nth(1).unwrap().parse().unwrap();
    let recorded: serde_json::Value =
        serde_json::from_str(fixture.lines().nth(2).unwrap()).unwrap();
    let base = BaseShape::from_base(&directory);
    let cursor = WindowCursor::from_json(&recorded["cursor"]);
    let cold = std::env::var("TOFUDB_RESTART_BENCH_COLD").map_or(true, |value| value != "0");
    let wal_files = wal_file_sizes(&directory);
    if cold {
        drop_fixture_page_cache(&directory);
    }
    // SAFETY: gettid has no preconditions.
    let open_thread = unsafe { libc::syscall(libc::SYS_gettid) } as i64;
    let origin = std::time::Instant::now();
    let sampler = Sampler::start(origin, open_thread);
    let io_before = process_io();
    let cpu_before = process_cpu_ticks();
    let thread_before = thread_cpu_nanos(open_thread);
    let started = std::time::Instant::now();
    let engine = Engine::open(&directory).unwrap();
    let elapsed = started.elapsed();
    let thread_after = thread_cpu_nanos(open_thread);
    let cpu_after = process_cpu_ticks();
    let io_after = process_io();
    let samples = sampler.finish();
    let delta = |key: &str| {
        io_after
            .get(key)
            .copied()
            .unwrap_or(0)
            .saturating_sub(io_before.get(key).copied().unwrap_or(0))
    };
    assert_eq!(
        engine.state.durable_sequence, durable,
        "reopen must reach the acknowledged durable sequence"
    );
    let replayed = engine.committed.len();
    let restaged_bytes = engine.blocks.pending_staged_bytes().unwrap();
    let peak_after_open_kib = peak_resident_kib();
    let verify_started = std::time::Instant::now();
    let verified = verify_window_reads(&engine, &base, &cursor);
    let verify_elapsed = verify_started.elapsed();
    // SAFETY: sysconf has no preconditions.
    let ticks_per_second = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as u64;
    let result = serde_json::json!({
        "bench": "dirty_open_result",
        "cold": cold,
        "fixture": recorded,
        "walFilesAtOpen": wal_files,
        "openOffsetNanos": started.duration_since(origin).as_nanos(),
        "elapsedNanos": elapsed.as_nanos(),
        "durableSequence": engine.state.durable_sequence,
        "replayedTransactions": replayed,
        "restagedBytes": restaged_bytes,
        "io": {
            "rchar": delta("rchar"), "readBytes": delta("read_bytes"), "syscr": delta("syscr"),
            "wchar": delta("wchar"), "writeBytes": delta("write_bytes"), "syscw": delta("syscw"),
        },
        "cpuNanos": {
            "process": cpu_after.saturating_sub(cpu_before) * 1_000_000_000 / ticks_per_second,
            "openThread": thread_after.saturating_sub(thread_before),
        },
        "peakResidentKiBAfterOpen": peak_after_open_kib,
        "verify": {"blocks": verified, "elapsedNanos": verify_elapsed.as_nanos()},
        "samplePeriodNanos": SAMPLE_PERIOD.as_nanos(),
        "loadavg": std::fs::read_to_string("/proc/loadavg").unwrap_or_default().trim(),
    });
    if let Some(path) = std::env::var_os("TOFUDB_RESTART_BENCH_SAMPLES") {
        std::fs::write(path, samples.join("\n") + "\n").unwrap();
    }
    println!("{result}");
    // Leave the fixture as the crash left it; the driver restores it anyway.
    std::mem::forget(engine);
}
