use std::sync::atomic::{AtomicU64, Ordering};

static PUBLISH_CONTROL: AtomicU64 = AtomicU64::new(0);
static WAL_APPEND: AtomicU64 = AtomicU64::new(0);
static FLUSH_DEFERRED: AtomicU64 = AtomicU64::new(0);
static CHECKPOINT: AtomicU64 = AtomicU64::new(0);

pub fn note_publish_control() {
    PUBLISH_CONTROL.fetch_add(1, Ordering::Relaxed);
}

pub fn note_wal_append() {
    WAL_APPEND.fetch_add(1, Ordering::Relaxed);
}

pub fn note_flush_deferred() {
    FLUSH_DEFERRED.fetch_add(1, Ordering::Relaxed);
}

pub fn note_checkpoint() {
    CHECKPOINT.fetch_add(1, Ordering::Relaxed);
}

// Stage instrumentation for commit-path attribution; printed at daemon shutdown
// when TOFU_DB_FSYNC_STATS is set. All counters are lock-free atomics.
pub const STAGE_SEMANTIC: usize = 0;
pub const STAGE_PREPARE: usize = 1;
pub const STAGE_ENGINE_COMMIT: usize = 2;
pub const STAGE_APPLY: usize = 3;
pub const STAGE_APPLY_BATCH: usize = 4;
pub const STAGE_WAL: usize = 5;
pub const STAGE_ENCODE: usize = 6;
pub const STAGE_CARRIED: usize = 7;
pub const STAGE_IR_STEPS: usize = 8;
pub const STAGE_IR_COMMIT: usize = 9;
pub const STAGE_LOAD_NODE: usize = 10;
pub const STAGE_STORE_PAGE: usize = 11;
pub const STAGE_PUT_DEFERRED: usize = 12;
pub const STAGE_BLOCK_GET: usize = 13;
pub const STAGE_BUILDER: usize = 14;
pub const STAGE_CHECKPOINT_IF: usize = 15;
pub const STAGE_COMMIT_TAIL: usize = 16;
pub const STAGE_GET_PENDING: usize = 17;
pub const STAGE_GET_CACHE: usize = 18;
pub const STAGE_GET_UNCACHED_OK: usize = 19;
pub const STAGE_GET_UNCACHED_MISS: usize = 20;
pub const STAGE_PUT_STAGE: usize = 21;
pub const STAGE_DRAIN_STEP: usize = 22;
pub const STAGE_STEP_CHUNK: usize = 23;
pub const STAGE_STEP_MANIFEST: usize = 24;
pub const STAGE_CK_MATERIALIZE: usize = 25;
pub const STAGE_CK_SEGMENTS: usize = 26;
pub const STAGE_CK_MANIFEST: usize = 27;
pub const STAGE_CK_WAL: usize = 28;
pub const STAGE_CK_CONTROL: usize = 29;
pub const STAGE_CK_REMOVE: usize = 30;
pub const STAGE_FLUSH_DIRTY: usize = 31;
const STAGE_COUNT: usize = 32;
const STAGE_NAMES: [&str; STAGE_COUNT] = [
    "semantic",
    "prepare",
    "engine_commit",
    "apply",
    "apply_batch",
    "wal",
    "encode",
    "carried",
    "ir_steps",
    "ir_commit",
    "load_node",
    "store_page",
    "put_deferred",
    "block_get",
    "builder",
    "checkpoint_if",
    "commit_tail",
    "get_pending",
    "get_cache",
    "get_unc_ok",
    "get_unc_miss",
    "put_stage",
    "drain_step",
    "step_chunk",
    "step_manifest",
    "ck_body",
    "ck_segments",
    "ck_manifest",
    "ck_wal",
    "ck_control",
    "ck_remove",
    "flush_dirty",
];
static STAGE_NANOS: [AtomicU64; STAGE_COUNT] = [const { AtomicU64::new(0) }; STAGE_COUNT];
static STAGE_CALLS: [AtomicU64; STAGE_COUNT] = [const { AtomicU64::new(0) }; STAGE_COUNT];

pub struct StageTimer {
    stage: usize,
    start: std::time::Instant,
}

impl StageTimer {
    pub fn new(stage: usize) -> Self {
        Self {
            stage,
            start: std::time::Instant::now(),
        }
    }
}

impl Drop for StageTimer {
    fn drop(&mut self) {
        let nanos = self.start.elapsed().as_nanos() as u64;
        STAGE_NANOS[self.stage].fetch_add(nanos, Ordering::Relaxed);
        STAGE_CALLS[self.stage].fetch_add(1, Ordering::Relaxed);
    }
}

pub fn stage_report() -> String {
    let mut out = String::new();
    for (index, name) in STAGE_NAMES.iter().enumerate() {
        let nanos = STAGE_NANOS[index].load(Ordering::Relaxed);
        let calls = STAGE_CALLS[index].load(Ordering::Relaxed);
        out.push_str(&format!(
            " {name}: calls={calls} total={:.1}ms avg={:.1}us\n",
            nanos as f64 / 1e6,
            nanos as f64 / 1e3 / calls.max(1) as f64
        ));
    }
    out
}

pub fn stage_nanos(stage: usize) -> u64 {
    STAGE_NANOS[stage].load(Ordering::Relaxed)
}

pub fn stage_calls(stage: usize) -> u64 {
    STAGE_CALLS[stage].load(Ordering::Relaxed)
}

pub fn note_stage_elapsed(stage: usize, nanos: u64) {
    STAGE_NANOS[stage].fetch_add(nanos, Ordering::Relaxed);
    STAGE_CALLS[stage].fetch_add(1, Ordering::Relaxed);
}

pub fn report() -> String {
    format!(
        "publish_control={} wal_append={} flush_deferred={} checkpoint={}",
        PUBLISH_CONTROL.load(Ordering::Relaxed),
        WAL_APPEND.load(Ordering::Relaxed),
        FLUSH_DEFERRED.load(Ordering::Relaxed),
        CHECKPOINT.load(Ordering::Relaxed)
    )
}
