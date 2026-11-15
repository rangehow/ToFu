//! Online incremental backup for the serving daemon.
//!
//! One dedicated `tofu-db-online-backup` thread. Each epoch captures an anchor
//! under one short authority hold: the durable state, its checkpointed
//! history, a copy of the committed tail after that checkpoint, and the
//! anchor registration. The hold neither merges the write overlay nor
//! checkpoints, and writes no file. Off the mutex the epoch then copies into
//! the closed store below the backup directory
//! (`backup::CLOSED_STORE_DIRECTORY`): the tail becomes history segments
//! written straight into the backup, every block is published after the
//! blocks it references, and the subgraph of a block the store already holds
//! is skipped (`backup_copy`), so an epoch's work follows what is new since
//! the store's last copy and an interrupted epoch keeps what it finished.
//! Source blocks are immutable and the registered anchor joins every
//! online-GC mark, so the copy can never lose a block mid-epoch. The snapshot
//! manifest publishes last; retention prune follows. The `system.backup`
//! control operation rings the doorbell and waits on a completion channel; it
//! never touches the authority mutex itself.

use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime};

use crate::authority::AuthorityDatabase;
use crate::authority::BackupAnchorCapture;
use crate::backup::{self, BackupMetrics, DestinationPolicy};
use crate::backup_copy::{ForegroundShare, GraphCopyProgress};
use crate::block::BlockId;
use crate::history::HistorySegmentReference;
use crate::vfs::Vfs;

/// Recovery-point bound (contracts/storage_v2.json `nativeOnlineBackup`): a
/// generation is due once the newest complete one is this old, measured from
/// its publication, so restarts never postpone it. The daily application
/// scheduler is the primary trigger and waits for an epoch's outcome within
/// the same bound.
pub const ONLINE_BACKUP_MAX_INTERVAL: Duration =
    Duration::from_secs(crate::generated_storage_v2::ONLINE_BACKUP_RECOVERY_POINT_SECONDS);
/// Generations retained after each successful epoch (mirrors the sidecar
/// default retention of two verified restore points).
pub const ONLINE_BACKUP_RETAINED_GENERATIONS: usize =
    crate::generated_storage_v2::ONLINE_BACKUP_RETAINED_GENERATIONS;
/// The last epoch's outcome, kept in the store so it survives restarts.
const LAST_EPOCH_FILE: &str = "LAST_EPOCH";

const DRIVER_POLL_INTERVAL: Duration = Duration::from_millis(250);
const DRIVER_STACK_BYTES: usize = 1024 * 1024;
const DRIVER_STOP_JOIN_BUDGET: Duration = Duration::from_secs(5);
/// Routine anchor contention (`WouldBlock`) is retried inside the epoch
/// before it ever surfaces as a scheduler-visible failure.
const TRANSIENT_RETRY_LIMIT: u32 = 8;
const TRANSIENT_RETRY_BACKOFF: Duration = Duration::from_millis(250);
/// A failed epoch is retried no sooner than this; its published progress
/// stays in the closed store, so the retry resumes rather than restarts.
const FAILED_EPOCH_RETRY_INTERVAL: Duration = Duration::from_secs(30);

/// Everything the online GC needs to protect while an off-mutex backup
/// copy is reading (docs/TOFUDB_ONLINE_GC.md rule S2, extended to backup
/// anchors). Captured under the anchor hold; registered on the authority.
#[derive(Clone, Debug)]
pub struct BackupAnchorProjection {
    pub root: Option<BlockId>,
    pub history_manifest_block_id: Option<BlockId>,
    pub history_segments: Vec<HistorySegmentReference>,
    /// Frozen manifest descendants and payload-index metadata for this exact
    /// anchor, retained even after the current history root is replaced.
    pub history_payload_metadata_ids: Vec<BlockId>,
    /// Blocks the committed tail references (and open publication windows):
    /// the epoch copies them after a later checkpoint may have retired them.
    pub tail_block_ids: Vec<BlockId>,
}

/// One completed epoch's receipt; the `system.backup` response derives
/// from this.
#[derive(Clone, Debug, serde::Serialize)]
pub struct OnlineBackupEpochReport {
    pub ok: bool,
    pub backend: &'static str,
    pub artifact_kind: &'static str,
    /// The store `tofu-db restore --backup-dir` restores from.
    pub backup: String,
    pub manifest: String,
    pub bytes: u64,
    pub sha256: String,
    pub durable_sequence: u64,
    pub copied_blocks: u64,
    pub copied_block_bytes: u64,
    pub manifest_bytes: u64,
    /// Blocks the store already held whose whole subgraph the copy skipped.
    pub present_subgraphs: u64,
    pub source_reads: u64,
    /// Directory syncs the copy issued (group commit: one sync serves every
    /// rename finished before it began).
    pub directory_syncs: u64,
    /// The most graph nodes the copy held open at once (bounded by the
    /// workers plus graph depth, whatever the generation's width).
    pub peak_open_nodes: u64,
    /// Committed transactions after the source's checkpoint that the epoch
    /// wrote into the backup as history segments.
    pub tail_transactions: u64,
    pub anchor_hold_ms: u64,
    pub copy_ms: u64,
    pub prune_ms: u64,
    pub elapsed_ms: u64,
    pub retained_generations: u32,
    pub pruned_generations: u32,
    pub pruned_block_bytes: u64,
    /// Why retention failed after the generation was published; the next
    /// epoch resumes the committed prune plan.
    pub prune_error: Option<String>,
}

/// A complete, restorable generation.
#[derive(Clone, Debug, serde::Serialize)]
pub struct CompleteGeneration {
    pub durable_sequence: u64,
    pub manifest: String,
    /// When its manifest was published; `None` when the store cannot say.
    pub published_unix_ms: Option<u64>,
    /// Filled when metrics are read.
    pub age_ms: Option<u64>,
}

/// The running epoch, sampled while it runs.
#[derive(Clone, Debug, serde::Serialize)]
pub struct EpochProgressReport {
    pub started_unix_ms: u64,
    pub phase: &'static str,
    pub visited_blocks: u64,
    pub present_subgraphs: u64,
    pub source_reads: u64,
    pub copied_blocks: u64,
    pub copied_bytes: u64,
}

/// The last epoch's outcome as the store recorded it (`LAST_EPOCH`).
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct RecordedEpochOutcome {
    pub outcome: String,
    pub started_unix_ms: u64,
    pub finished_unix_ms: u64,
    pub durable_sequence: Option<u64>,
    pub manifest: Option<String>,
    pub phase: Option<String>,
    pub error: Option<String>,
    pub copied_blocks: u64,
    pub present_subgraphs: u64,
}

/// Process-local collector state surfaced through `system.metrics`.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct OnlineBackupMetrics {
    pub replicated_sequence: Option<u64>,
    pub replication_interval_ms: Option<u64>,
    pub epochs_completed: u64,
    pub epochs_failed: u64,
    /// Epochs stopped because the owner disabled the backup's scheduler task
    /// while they ran; none of them published a generation.
    pub epochs_paused: u64,
    pub epoch_in_progress: bool,
    pub last_success_unix_ms: Option<u64>,
    pub last_error: Option<String>,
    pub last_failure_unix_ms: Option<u64>,
    pub last_report: Option<OnlineBackupEpochReport>,
    /// The store epochs write and `system.backup` names.
    pub store: Option<String>,
    /// The newest generation restorable from `store`.
    pub last_complete_generation: Option<CompleteGeneration>,
    /// The newest generation an older engine's store at the top of the
    /// backup directory still holds (restorable from that directory).
    pub legacy_newest_generation: Option<CompleteGeneration>,
    /// When the fallback epoch is due without a doorbell.
    pub next_fallback_due_unix_ms: Option<u64>,
    /// The due fallback waits because the owner disabled the backup's
    /// scheduler task; doorbells still run.
    pub fallback_held: bool,
    pub current_epoch: Option<EpochProgressReport>,
    /// The last outcome an earlier process recorded in the store.
    pub recorded_last_epoch: Option<RecordedEpochOutcome>,
}

#[derive(Default)]
struct EpochStatus {
    started_unix_ms: AtomicU64,
    phase: Mutex<&'static str>,
    progress: GraphCopyProgress,
}

impl EpochStatus {
    fn begin(&self) {
        self.started_unix_ms.store(now_unix_ms(), Ordering::Release);
        for counter in [
            &self.progress.visited_blocks,
            &self.progress.present_subgraphs,
            &self.progress.source_reads,
            &self.progress.copied_blocks,
            &self.progress.copied_bytes,
        ] {
            counter.store(0, Ordering::Relaxed);
        }
        self.set_phase("anchor");
    }

    fn set_phase(&self, phase: &'static str) {
        *self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = phase;
    }

    fn phase(&self) -> &'static str {
        *self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn report(&self) -> EpochProgressReport {
        EpochProgressReport {
            started_unix_ms: self.started_unix_ms.load(Ordering::Acquire),
            phase: self.phase(),
            visited_blocks: self.progress.visited_blocks.load(Ordering::Relaxed),
            present_subgraphs: self.progress.present_subgraphs.load(Ordering::Relaxed),
            source_reads: self.progress.source_reads.load(Ordering::Relaxed),
            copied_blocks: self.progress.copied_blocks.load(Ordering::Relaxed),
            copied_bytes: self.progress.copied_bytes.load(Ordering::Relaxed),
        }
    }
}

struct SharedState {
    metrics: RwLock<OnlineBackupMetrics>,
    epoch: EpochStatus,
    doorbell: AtomicU64,
    /// Finished-epoch count and the latest receipt, updated together so a
    /// waiter never observes a count without its result. Waiters clone the
    /// receipt: every request waiting on one epoch receives it.
    completed: Mutex<(u64, Option<Result<OnlineBackupEpochReport, String>>)>,
    completion_signal: Condvar,
    /// The owner's disabled backup task holds automatic epochs; the driver's
    /// own decision state, mirrored into the metrics when they are read.
    fallback_held: AtomicBool,
}

impl SharedState {
    fn note_start(&self) {
        self.epoch.begin();
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epoch_in_progress = true;
        }
    }

    fn note_success(&self, report: OnlineBackupEpochReport, now_unix_ms: u64) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.replicated_sequence = Some(report.durable_sequence);
            metrics.epochs_completed += 1;
            metrics.epoch_in_progress = false;
            metrics.last_success_unix_ms = Some(now_unix_ms);
            metrics.last_error = None;
            metrics.last_complete_generation = Some(CompleteGeneration {
                durable_sequence: report.durable_sequence,
                manifest: report.manifest.clone(),
                published_unix_ms: Some(now_unix_ms),
                age_ms: None,
            });
            metrics.last_report = Some(report.clone());
        }
        self.finish(Ok(report));
    }

    fn note_failure(&self, error: &io::Error, phase: &str, now_unix_ms: u64) {
        let message = format!("online backup epoch failed in phase {phase}: {error}");
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epochs_failed += 1;
            metrics.epoch_in_progress = false;
            metrics.last_error = Some(message.clone());
            metrics.last_failure_unix_ms = Some(now_unix_ms);
        }
        self.finish(Err(format!(
            "{message}; {}",
            self.newest_generation_summary()
        )));
    }

    /// The owner disabled the backup's scheduler task while the epoch ran: it
    /// stopped at its next block and published nothing. Not a failure; a
    /// waiting `system.backup` learns why.
    fn note_paused(&self, phase: &str) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.epochs_paused += 1;
            metrics.epoch_in_progress = false;
        }
        self.finish(Err(format!(
            "online backup epoch stopped in phase {phase}: the owner disabled the {} scheduler task; {}",
            crate::generated_storage_v2::ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
            self.newest_generation_summary(),
        )));
    }

    fn note_next_fallback(&self, due: Duration) {
        if let Ok(mut metrics) = self.metrics.write() {
            metrics.next_fallback_due_unix_ms =
                Some(now_unix_ms().saturating_add(due.as_millis() as u64));
        }
    }

    /// Records whether the owner's disabled backup task holds automatic
    /// epochs, logging each change once.
    fn note_fallback_held(&self, held: bool, owner: AutomaticEpochOwner) {
        if self.fallback_held.swap(held, Ordering::AcqRel) != held {
            crate::engine_log::info(
                "online_backup",
                &format!(
                    "fallback={} reason=scheduler-task system_key={} tenant={} owner={}",
                    if held { "held" } else { "released" },
                    crate::generated_storage_v2::ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
                    owner.tenant_id,
                    owner.owner_user_id
                ),
            );
        }
    }

    fn finish(&self, result: Result<OnlineBackupEpochReport, String>) {
        {
            let mut completed = self
                .completed
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            completed.0 += 1;
            completed.1 = Some(result);
        }
        self.completion_signal.notify_all();
    }

    /// Why a waiter's deadline passed: the running epoch's progress and the
    /// newest restorable generation, so the caller can report what is true.
    fn wait_expired_message(&self, waited: Duration) -> String {
        let metrics = self.metrics_with_ages();
        let epoch = metrics
            .current_epoch
            .map(|epoch| {
                format!(
                    "epoch running {} s in phase {} (copied {} blocks, {} present subgraphs skipped)",
                    now_unix_ms().saturating_sub(epoch.started_unix_ms) / 1000,
                    epoch.phase,
                    epoch.copied_blocks,
                    epoch.present_subgraphs
                )
            })
            .unwrap_or_else(|| "no epoch running".to_owned());
        format!(
            "online backup still running after the request waited {} s: {epoch}; {}",
            waited.as_secs(),
            self.newest_generation_summary()
        )
    }

    /// The newest restorable generation and its age, for failure messages.
    fn newest_generation_summary(&self) -> String {
        let metrics = self.metrics_with_ages();
        let describe = |label: &str, generation: &CompleteGeneration| {
            format!(
                "{label} durable_sequence={} age_s={} manifest={}",
                generation.durable_sequence,
                generation
                    .age_ms
                    .map_or("unknown".to_owned(), |age| (age / 1000).to_string()),
                generation.manifest
            )
        };
        match (
            &metrics.last_complete_generation,
            &metrics.legacy_newest_generation,
        ) {
            (Some(generation), _) => describe("newest complete generation", generation),
            (None, Some(generation)) => {
                describe("newest complete generation (pre-closed-store)", generation)
            }
            (None, None) => "no complete generation".to_owned(),
        }
    }

    fn metrics_with_ages(&self) -> OnlineBackupMetrics {
        let mut metrics = self
            .metrics
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        let now = now_unix_ms();
        for generation in [
            metrics.last_complete_generation.as_mut(),
            metrics.legacy_newest_generation.as_mut(),
        ]
        .into_iter()
        .flatten()
        {
            generation.age_ms = generation
                .published_unix_ms
                .map(|published| now.saturating_sub(published));
        }
        if metrics.epoch_in_progress {
            metrics.current_epoch = Some(self.epoch.report());
        }
        metrics.fallback_held = self.fallback_held.load(Ordering::Acquire);
        metrics
    }
}

/// Cloneable observer for the control operations and health checks.
#[derive(Clone)]
pub struct OnlineBackupObserver {
    shared: Arc<SharedState>,
    stopped: Arc<AtomicBool>,
}

impl OnlineBackupObserver {
    pub fn metrics(&self) -> OnlineBackupMetrics {
        self.shared.metrics_with_ages()
    }

    pub fn require_healthy(&self) -> io::Result<()> {
        if self.stopped.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "online backup driver stopped unexpectedly",
            ));
        }
        Ok(())
    }

    /// Rings the doorbell and waits for the next finished epoch, at most
    /// until `deadline_unix_ms`. The epoch itself has no deadline: a wait
    /// that ends first reports the running epoch's progress (`TimedOut`),
    /// and the epoch continues. Errors otherwise carry the epoch's failure.
    pub fn run_and_wait(&self, deadline_unix_ms: u64) -> io::Result<OnlineBackupEpochReport> {
        let started = Instant::now();
        self.shared.doorbell.fetch_add(1, Ordering::AcqRel);
        let mut completed = self
            .shared
            .completed
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let served_at_request = completed.0;
        loop {
            if let (served, Some(result)) = &*completed {
                if *served > served_at_request {
                    return result.clone().map_err(io::Error::other);
                }
            }
            let now_unix_ms = now_unix_ms();
            if now_unix_ms >= deadline_unix_ms {
                drop(completed);
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    self.shared.wait_expired_message(started.elapsed()),
                ));
            }
            let remaining = Duration::from_millis((deadline_unix_ms - now_unix_ms).min(250));
            completed = self
                .shared
                .completion_signal
                .wait_timeout(completed, remaining)
                .map_err(|_| io::Error::other("backup completion lock poisoned"))?
                .0;
        }
    }
}

/// Dedicated driver thread. Every authority hold it takes is a `try_lock`:
/// contention always defers the backup, never the foreground.
pub struct OnlineBackupDriver {
    stop: Arc<AtomicBool>,
    /// The owner's backup task: a stop also raises its interrupt, the stop
    /// flag of the running epoch (`scheduler::SystemTaskWatch`).
    watch: Option<Arc<crate::scheduler::SystemTaskWatch>>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    observer: OnlineBackupObserver,
}

/// The owner whose built-in scheduler task (`nativeOnlineBackup.schedulerSystemKey`
/// in contracts/storage_v2.json) stands for the daily backup. While that
/// task is disabled no automatic epoch starts, however overdue; enabling it
/// restores the cadence, and a doorbell (`system.backup`) always runs.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AutomaticEpochOwner {
    pub tenant_id: u64,
    pub owner_user_id: u64,
}

struct EpochContext {
    /// The directory the store lives in or is.
    backup_dir: PathBuf,
    /// Where epochs write: the closed store below `backup_dir`, or the
    /// fastpath shadow itself.
    store_dir: PathBuf,
    /// The store is a closed store this engine creates and owns.
    closed_store: bool,
    destination_vfs: Arc<dyn Vfs>,
    retention: usize,
    _fastpath_lease: Option<Arc<crate::fastpath::FastpathLease>>,
    /// Whose disabled scheduler task holds automatic epochs; the fastpath
    /// shadow's replication has none.
    automatic_epoch_owner: Option<AutomaticEpochOwner>,
}

impl OnlineBackupDriver {
    pub fn start(
        database: &Arc<Mutex<AuthorityDatabase>>,
        backup_dir: PathBuf,
        at_rest_key: Option<crate::at_rest::AtRestKey>,
        automatic_epoch_owner: Option<AutomaticEpochOwner>,
    ) -> io::Result<Self> {
        if !backup_dir.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "online backup directory must be absolute",
            ));
        }
        {
            let guard = database
                .lock()
                .map_err(|_| io::Error::other("authority mutex poisoned"))?;
            guard.validate_online_backup_directory(&backup_dir)?;
        }
        std::fs::create_dir_all(&backup_dir)?;
        Self::start_with_destination_vfs(
            database,
            backup_dir.clone(),
            crate::at_rest::destination_vfs(&[&backup_dir], at_rest_key),
            automatic_epoch_owner,
        )
    }

    pub(crate) fn start_fastpath(
        database: &Arc<Mutex<AuthorityDatabase>>,
        backup_dir: PathBuf,
        key: Option<crate::at_rest::AtRestKey>,
        lease: Arc<crate::fastpath::FastpathLease>,
    ) -> io::Result<Self> {
        database
            .lock()
            .map_err(|_| io::Error::other("authority mutex poisoned"))?
            .validate_online_backup_directory(&backup_dir)?;
        let context = EpochContext {
            store_dir: backup_dir.clone(),
            closed_store: false,
            destination_vfs: crate::at_rest::destination_vfs(&[&backup_dir], key),
            backup_dir,
            retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
            _fastpath_lease: Some(lease),
            automatic_epoch_owner: None,
        };
        Self::start_scheduled(database, context)
    }

    /// Constructor with an explicit destination VFS; production wraps it
    /// through `at_rest::destination_vfs`, tests pass the deterministic one.
    pub(crate) fn start_with_destination_vfs(
        database: &Arc<Mutex<AuthorityDatabase>>,
        backup_dir: PathBuf,
        destination_vfs: Arc<dyn Vfs>,
        automatic_epoch_owner: Option<AutomaticEpochOwner>,
    ) -> io::Result<Self> {
        let context = EpochContext {
            store_dir: backup::closed_store_path(&backup_dir),
            closed_store: true,
            backup_dir,
            destination_vfs,
            retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
            _fastpath_lease: None,
            automatic_epoch_owner,
        };
        Self::start_scheduled(database, context)
    }

    fn start_scheduled(
        database: &Arc<Mutex<AuthorityDatabase>>,
        context: EpochContext,
    ) -> io::Result<Self> {
        Self::start_scheduled_with_first_due(database, context, None)
    }

    /// `first_due_override` replaces the derived first fallback
    /// (specifications only).
    fn start_scheduled_with_first_due(
        database: &Arc<Mutex<AuthorityDatabase>>,
        context: EpochContext,
        first_due_override: Option<Duration>,
    ) -> io::Result<Self> {
        let fastpath = context._fastpath_lease.is_some();
        let initial_sequence = context
            ._fastpath_lease
            .as_ref()
            .map(|lease| lease.shadow_sequence);
        let interval = context
            ._fastpath_lease
            .as_ref()
            .map(|lease| lease.interval)
            .unwrap_or(ONLINE_BACKUP_MAX_INTERVAL);
        let mut initial = OnlineBackupMetrics {
            replicated_sequence: initial_sequence,
            replication_interval_ms: fastpath.then_some(interval.as_millis() as u64),
            store: Some(context.store_dir.to_string_lossy().into_owned()),
            ..OnlineBackupMetrics::default()
        };
        observe_existing_generations(&context, &mut initial);
        // The fastpath shadow keeps its boot-relative cadence. The classic
        // store's first fallback follows its newest generation's age (an
        // older engine's generations count until the closed store has one),
        // never earlier than the daemon's boot grace: startup is the
        // latency-sensitive phase.
        let first_due = if let Some(first_due) = first_due_override {
            first_due
        } else if fastpath {
            interval
        } else {
            initial
                .last_complete_generation
                .as_ref()
                .or(initial.legacy_newest_generation.as_ref())
                .and_then(|generation| generation.published_unix_ms)
                .map_or(Duration::ZERO, |published| {
                    let age = Duration::from_millis(now_unix_ms().saturating_sub(published));
                    interval.saturating_sub(age)
                })
                .max(crate::authority_gc_online::ONLINE_GC_BOOT_GRACE)
        };
        let shared = Arc::new(SharedState {
            metrics: RwLock::new(initial),
            epoch: EpochStatus::default(),
            doorbell: AtomicU64::new(0),
            completed: Mutex::new((0, None)),
            completion_signal: Condvar::new(),
            fallback_held: AtomicBool::new(false),
        });
        shared.note_next_fallback(first_due);
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        // The owner's backup task holds automatic epochs, and disabling it
        // stops the running epoch (`scheduler::SystemTaskHolds`).
        let watch = match context.automatic_epoch_owner {
            Some(owner) => {
                let guard = database
                    .lock()
                    .map_err(|_| io::Error::other("authority mutex poisoned"))?;
                let authority: &AuthorityDatabase = &guard;
                Some(authority.system_task_holds().watch(
                    authority,
                    database,
                    owner.tenant_id,
                    owner.owner_user_id,
                    crate::generated_storage_v2::ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
                ))
            }
            None => None,
        };
        let thread = std::thread::Builder::new()
            .name("tofu-db-online-backup".to_owned())
            .stack_size(DRIVER_STACK_BYTES)
            .spawn({
                let shared = Arc::clone(&shared);
                let stop = Arc::clone(&stop);
                let stopped = Arc::clone(&stopped);
                let database = Arc::clone(database);
                let watch = watch.clone();
                move || {
                    crate::fsync_stats::set_current_component(crate::fsync_stats::COMPONENT_ONLINE_BACKUP);
                    let mut last_attempt = Instant::now();
                    let mut cadence = first_due;
                    let mut replicated_sequence = initial_sequence;
                    let mut served_count = 0_u64;
                    loop {
                        if stop.load(Ordering::Acquire) {
                            break;
                        }
                        let requested = shared.doorbell.load(Ordering::Acquire);
                        let mut fallback_due = last_attempt.elapsed() >= cadence;
                        // A due fallback asks the owner's backup task, and a
                        // hold is re-read every poll so a resume ends it at
                        // once; a row that could not be read holds and is
                        // read again here. A doorbell is an explicit request
                        // and runs.
                        if let (Some(owner), Some(watch)) = (context.automatic_epoch_owner, &watch) {
                            if fallback_due || shared.fallback_held.load(Ordering::Acquire) {
                                watch.refresh_if_unread();
                                let held = watch.held();
                                shared.note_fallback_held(held, owner);
                                fallback_due &= !held;
                            }
                        }
                        if requested > served_count || fallback_due {
                            // A quiet native fastpath must not walk the entire
                            // corpus on every tick. Doorbells still force epochs.
                            if fastpath && requested <= served_count {
                                let sequence = database.try_lock().ok()
                                    .and_then(|guard| guard.writer_engine().ok().map(|engine| engine.state().durable_sequence));
                                if sequence.is_none() {
                                    std::thread::park_timeout(DRIVER_POLL_INTERVAL);
                                    continue;
                                }
                                if sequence == replicated_sequence {
                                    last_attempt = Instant::now();
                                    std::thread::park_timeout(DRIVER_POLL_INTERVAL);
                                    continue;
                                }
                            }
                            let trigger = if requested > served_count { "doorbell" } else { "fallback" };
                            // A disable published from here on stops the epoch
                            // at its next block; one that landed since the
                            // gate read the hold keeps the fallback waiting.
                            let held_at_start = watch.as_ref().is_some_and(|watch| watch.begin_epoch(&stop));
                            if held_at_start && trigger == "fallback" {
                                continue;
                            }
                            let epoch_stop: &AtomicBool = watch
                                .as_deref()
                                .map_or(&*stop, crate::scheduler::SystemTaskWatch::interrupt);
                            last_attempt = Instant::now();
                            shared.note_start();
                            crate::engine_log::info("online_backup", &format!(
                                "epoch=start trigger={trigger} store={}", context.store_dir.display()));
                            let mut attempt = 0_u32;
                            let outcome = loop {
                                match run_epoch(&database, &context, epoch_stop, &shared.epoch) {
                                    Err(error)
                                        if error.kind() == io::ErrorKind::WouldBlock
                                            && attempt < TRANSIENT_RETRY_LIMIT
                                            && !epoch_stop.load(Ordering::Acquire) =>
                                    {
                                        // Anchor contention at boot or under
                                        // load is routine; absorb it inside
                                        // the epoch so the scheduler never sees it.
                                        attempt += 1;
                                        std::thread::park_timeout(TRANSIENT_RETRY_BACKOFF);
                                        continue;
                                    }
                                    outcome => break outcome,
                                }
                            };
                            let succeeded = outcome.is_ok();
                            // Every stop check ends an epoch with WouldBlock.
                            let paused = !stop.load(Ordering::Acquire)
                                && epoch_stop.load(Ordering::Acquire)
                                && outcome.as_ref().err().is_some_and(|error| error.kind() == io::ErrorKind::WouldBlock);
                            match outcome {
                                Ok(report) => {
                                    crate::engine_log::info("online_backup", &format!(
                                        "epoch=published durable_sequence={} copied_blocks={} copied_block_bytes={} present_subgraphs={} source_reads={} directory_syncs={} peak_open_nodes={} tail_transactions={} anchor_hold_ms={} copy_ms={} prune_ms={} elapsed_ms={} manifest={}",
                                        report.durable_sequence, report.copied_blocks, report.copied_block_bytes,
                                        report.present_subgraphs, report.source_reads, report.directory_syncs,
                                        report.peak_open_nodes, report.tail_transactions,
                                        report.anchor_hold_ms, report.copy_ms, report.prune_ms, report.elapsed_ms,
                                        report.manifest));
                                    record_epoch_outcome(&context, &shared.epoch, Ok(&report), false);
                                    replicated_sequence = Some(report.durable_sequence);
                                    shared.note_success(report, now_unix_ms());
                                }
                                Err(error) if paused => {
                                    // Copied blocks stay in the store; the next
                                    // epoch finds them present and reuses them.
                                    let progress = shared.epoch.report();
                                    crate::engine_log::info("online_backup", &format!(
                                        "epoch=paused reason=scheduler-task system_key={} phase={} copied_blocks={} present_subgraphs={}",
                                        crate::generated_storage_v2::ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
                                        progress.phase, progress.copied_blocks, progress.present_subgraphs));
                                    record_epoch_outcome(&context, &shared.epoch, Err(&error), true);
                                    shared.note_paused(progress.phase);
                                }
                                Err(error) => {
                                    let progress = shared.epoch.report();
                                    crate::engine_log::error("online_backup", &format!(
                                        "epoch=failed phase={} elapsed_ms={} copied_blocks={} present_subgraphs={} error={error}",
                                        progress.phase,
                                        now_unix_ms().saturating_sub(progress.started_unix_ms),
                                        progress.copied_blocks, progress.present_subgraphs));
                                    record_epoch_outcome(&context, &shared.epoch, Err(&error), false);
                                    shared.note_failure(&error, progress.phase, now_unix_ms());
                                }
                            }
                            // A failed epoch is retried on a bounded cadence, never
                            // a tight loop. Slow fastpath epochs adapt cadence to
                            // their measured copy cost.
                            cadence = if succeeded { interval } else { interval.min(FAILED_EPOCH_RETRY_INTERVAL) };
                            if fastpath { cadence = cadence.max(last_attempt.elapsed()); }
                            if fastpath {
                                if let Ok(mut metrics) = shared.metrics.write() {
                                    metrics.replication_interval_ms = Some(cadence.as_millis() as u64);
                                }
                            }
                            last_attempt = Instant::now();
                            shared.note_next_fallback(cadence);
                            served_count = served_count.max(requested);
                            continue;
                        }
                        std::thread::park_timeout(DRIVER_POLL_INTERVAL);
                    }
                    stopped.store(true, Ordering::Release);
                }
            })?;
        if let Some(watch) = &watch {
            watch.set_driver(thread.thread().clone());
        }
        Ok(Self {
            stop,
            watch,
            thread: Mutex::new(Some(thread)),
            observer: OnlineBackupObserver { shared, stopped },
        })
    }

    pub fn observer(&self) -> OnlineBackupObserver {
        self.observer.clone()
    }

    pub fn stop_and_join(&self) -> io::Result<()> {
        // Set before the interrupt: `SystemTaskWatch::begin_epoch` relies on it.
        self.stop.store(true, Ordering::SeqCst);
        if let Some(watch) = &self.watch {
            watch.raise_for_stop();
        }
        let thread = self
            .thread
            .lock()
            .map_err(|_| io::Error::other("online driver thread lock poisoned"))?
            .take();
        if let Some(thread) = thread {
            thread.thread().unpark();
            // Bounded join: every epoch phase is crash-safe at any
            // interruption point, and the copy checks the stop flag before
            // every block, so a slow phase never stalls shutdown.
            let deadline = std::time::Instant::now() + DRIVER_STOP_JOIN_BUDGET;
            loop {
                if thread.is_finished() {
                    break;
                }
                if std::time::Instant::now() >= deadline {
                    // Detach: the thread exits at its next phase boundary
                    // and takes nothing the shutdown path still needs.
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            thread
                .join()
                .map_err(|_| io::Error::other("online backup driver panicked"))?;
        }
        Ok(())
    }
}

fn now_unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or(0)
}

/// Reads what the store (and an older engine's store at the top of the
/// backup directory) already holds, so status and the fallback cadence are
/// right before the first epoch. Unreadable stores leave the fields empty.
fn observe_existing_generations(context: &EpochContext, metrics: &mut OnlineBackupMetrics) {
    let vfs = context.destination_vfs.as_ref();
    metrics.last_complete_generation = newest_generation(&context.store_dir, vfs);
    if context.closed_store {
        metrics.legacy_newest_generation = newest_generation(&context.backup_dir, vfs);
    }
    metrics.recorded_last_epoch = read_recorded_epoch_outcome(&context.store_dir, vfs);
}

fn newest_generation(store_dir: &Path, vfs: &dyn Vfs) -> Option<CompleteGeneration> {
    let snapshots = store_dir.join("snapshots");
    let manifest = backup::existing_manifests(&snapshots, vfs)
        .ok()?
        .into_iter()
        .max_by_key(|manifest| {
            (
                manifest.durable_sequence,
                manifest.source_control_generation,
            )
        })?;
    let path = snapshots.join(manifest.file_name());
    let published_unix_ms = std::fs::metadata(&path)
        .and_then(|metadata| metadata.modified())
        .ok()
        .and_then(|modified| modified.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|duration| duration.as_millis() as u64);
    Some(CompleteGeneration {
        durable_sequence: manifest.durable_sequence,
        manifest: path.to_string_lossy().into_owned(),
        published_unix_ms,
        age_ms: None,
    })
}

fn read_recorded_epoch_outcome(store_dir: &Path, vfs: &dyn Vfs) -> Option<RecordedEpochOutcome> {
    let mut file = vfs
        .open(
            &store_dir.join(LAST_EPOCH_FILE),
            crate::vfs::OpenRequest {
                read: true,
                ..crate::vfs::OpenRequest::default()
            },
        )
        .ok()?;
    serde_json::from_slice(&file.read_all(64 * 1024).ok()?).ok()
}

/// Best effort: the outcome is status, never part of a generation.
fn record_epoch_outcome(
    context: &EpochContext,
    status: &EpochStatus,
    outcome: Result<&OnlineBackupEpochReport, &io::Error>,
    paused: bool,
) {
    let progress = status.report();
    let recorded = RecordedEpochOutcome {
        outcome: match (outcome.is_ok(), paused) {
            (true, _) => "published",
            (false, true) => "paused",
            (false, false) => "failed",
        }
        .to_owned(),
        started_unix_ms: progress.started_unix_ms,
        finished_unix_ms: now_unix_ms(),
        durable_sequence: outcome.ok().map(|report| report.durable_sequence),
        manifest: outcome.ok().map(|report| report.manifest.clone()),
        phase: outcome.is_err().then(|| progress.phase.to_owned()),
        error: outcome.err().map(|error| error.to_string()),
        copied_blocks: progress.copied_blocks,
        present_subgraphs: progress.present_subgraphs,
    };
    if context
        .destination_vfs
        .metadata(&context.store_dir)
        .is_err()
    {
        return;
    }
    if let Ok(encoded) = serde_json::to_vec(&recorded) {
        let _ = backup::publish_small_file(
            &context.store_dir,
            LAST_EPOCH_FILE,
            &encoded,
            context.destination_vfs.as_ref(),
        );
    }
}

fn run_epoch(
    database: &Arc<Mutex<AuthorityDatabase>>,
    context: &EpochContext,
    stop: &AtomicBool,
    status: &EpochStatus,
) -> io::Result<OnlineBackupEpochReport> {
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "backup driver stopping",
        ));
    }
    let started = Instant::now();
    status.set_phase("anchor");
    let capture = database
        .try_lock()
        .map_err(|_| io::Error::new(io::ErrorKind::WouldBlock, "backup anchor hold contended"))?
        .capture_backup_anchor()?;
    let registration = RegisteredAnchor {
        database,
        token: capture.anchor_token,
    };
    let anchor_hold_ms = started.elapsed().as_millis() as u64;
    let result = run_epoch_off_mutex(&capture, context, stop, status);
    drop(registration);
    result.map(|mut report| {
        report.anchor_hold_ms = anchor_hold_ms;
        report.elapsed_ms = started.elapsed().as_millis() as u64;
        report
    })
}

/// An epoch's anchor registration. Every online-GC mark keeps what a
/// registered anchor references, so the registration ends with the epoch
/// however it ends, a panic in the copy included; otherwise the generation
/// it copied would stay unreclaimable until the daemon restarts.
struct RegisteredAnchor<'a> {
    database: &'a Arc<Mutex<AuthorityDatabase>>,
    token: u64,
}

impl Drop for RegisteredAnchor<'_> {
    fn drop(&mut self) {
        self.database
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .deregister_backup_anchor(self.token);
    }
}

fn open_store(context: &EpochContext) -> io::Result<(crate::block::BlockStore, PathBuf)> {
    if context.closed_store {
        return backup::initialize_or_open_closed_backup(
            &context.store_dir,
            Arc::clone(&context.destination_vfs),
        );
    }
    backup::initialize_or_open_backup(&context.store_dir, Arc::clone(&context.destination_vfs))
}

fn run_epoch_off_mutex(
    capture: &BackupAnchorCapture,
    context: &EpochContext,
    stop: &AtomicBool,
    status: &EpochStatus,
) -> io::Result<OnlineBackupEpochReport> {
    status.set_phase("open");
    let (destination, snapshots_path) = open_store(context)?;
    // The epoch's whole-store passes run beside the foreground on the same
    // mount; one share sizes their I/O by the foreground's admission waits.
    let share = ForegroundShare::new();
    // A prior epoch may have committed its prune plan before losing the
    // process or I/O. Resume that exact plan through the existing lease owner
    // before copying; otherwise every future epoch fences on the same marker.
    let resumed_prune =
        match backup::ensure_no_prune(&context.store_dir, context.destination_vfs.as_ref()) {
            Ok(()) => None,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                status.set_phase("prune");
                Some(backup::prune_backup_with_share(
                    &context.store_dir,
                    context.retention,
                    Arc::clone(&context.destination_vfs),
                    Some(&share),
                )?)
            }
            Err(error) => return Err(error),
        };
    // The lease scopes only the copy+publish phase: prune acquires the same
    // path-locked BACKUP_LOCK internally and would conflict with our hold.
    let copy_started = Instant::now();
    let copy_result = {
        let _lease =
            backup::acquire_backup_lease(&context.store_dir, context.destination_vfs.as_ref())?;
        backup::ensure_no_prune(&context.store_dir, context.destination_vfs.as_ref())?;
        // Read under the lease: the store's records decide what presence proves.
        let policy =
            backup::destination_policy(&context.store_dir, context.destination_vfs.as_ref())?;
        run_copy_phase(
            capture,
            context,
            stop,
            &destination,
            &snapshots_path,
            &policy,
            status,
            &share,
        )
    };
    let (metrics, copy, manifest_path) = copy_result?;
    let copy_ms = copy_started.elapsed().as_millis() as u64;
    let manifest_sha256 = sha256_file(&manifest_path, context.destination_vfs.as_ref())?;
    // The generation is complete and restorable from here on. Retention is
    // maintenance after it: a failed prune leaves its committed plan for the
    // next epoch to resume and never turns a published generation into a
    // failed backup.
    status.set_phase("prune");
    let prune_started = Instant::now();
    let prune = backup::prune_backup_with_share(
        &context.store_dir,
        context.retention,
        Arc::clone(&context.destination_vfs),
        Some(&share),
    );
    let prune_ms = prune_started.elapsed().as_millis() as u64;
    let (prune, prune_error) = match prune {
        Ok(prune) => (prune, None),
        Err(error) => {
            crate::engine_log::error("online_backup", &format!(
                "prune=failed elapsed_ms={prune_ms} error={error}; the generation stays published and the next epoch resumes the prune"));
            let retained =
                backup::existing_manifests(&snapshots_path, context.destination_vfs.as_ref())
                    .map_or(0, |manifests| manifests.len() as u32);
            (
                backup::BackupPruneMetrics {
                    retained_generations: retained,
                    ..Default::default()
                },
                Some(error.to_string()),
            )
        }
    };
    Ok(OnlineBackupEpochReport {
        ok: true,
        backend: "tofudb",
        artifact_kind: "directory",
        backup: context.store_dir.to_string_lossy().into_owned(),
        manifest: manifest_path.to_string_lossy().into_owned(),
        bytes: metrics.copied_block_bytes + metrics.manifest_bytes,
        sha256: manifest_sha256,
        durable_sequence: metrics.durable_sequence,
        copied_blocks: metrics.copied_blocks,
        copied_block_bytes: metrics.copied_block_bytes,
        manifest_bytes: metrics.manifest_bytes,
        present_subgraphs: copy.present_subgraphs,
        source_reads: copy.source_reads,
        directory_syncs: copy.directory_barriers,
        peak_open_nodes: copy.peak_open_nodes,
        tail_transactions: capture.tail.len() as u64,
        anchor_hold_ms: 0,
        copy_ms,
        prune_ms,
        elapsed_ms: 0,
        retained_generations: prune.retained_generations,
        pruned_generations: prune.removed_generations
            + resumed_prune
                .as_ref()
                .map_or(0, |old| old.removed_generations),
        pruned_block_bytes: prune.removed_block_bytes
            + resumed_prune
                .as_ref()
                .map_or(0, |old| old.removed_block_bytes),
        prune_error,
    })
}

#[allow(clippy::too_many_arguments)]
fn run_copy_phase(
    capture: &BackupAnchorCapture,
    context: &EpochContext,
    stop: &AtomicBool,
    destination: &crate::block::BlockStore,
    snapshots_path: &Path,
    policy: &DestinationPolicy,
    status: &EpochStatus,
    share: &ForegroundShare,
) -> io::Result<(BackupMetrics, crate::backup_copy::GraphCopyMetrics, PathBuf)> {
    let (manifest, built) =
        backup::plan_anchored_generation(&capture.state, capture.history.as_ref(), &capture.tail)?;
    let existing = backup::existing_manifests(snapshots_path, context.destination_vfs.as_ref())?;
    if existing
        .iter()
        .any(|candidate| candidate.authority_uuid != manifest.authority_uuid)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "backup directory belongs to another authority",
        ));
    }
    if let Some(previous) = existing.iter().find(|candidate| {
        candidate.durable_sequence == manifest.durable_sequence
            && candidate.root_hash == manifest.root_hash
    }) {
        // A same-lineage committed sequence is already recoverable. A later
        // checkpoint, merge, or local restore/drain changes physical witnesses
        // (history segment bounds, roots) without a new commit; a second
        // manifest for the same commit would only displace a distinct
        // generation from retention.
        return Ok((
            BackupMetrics {
                durable_sequence: previous.durable_sequence,
                ..BackupMetrics::default()
            },
            Default::default(),
            snapshots_path.join(previous.file_name()),
        ));
    }
    if existing.len() == backup::MAX_BACKUP_GENERATIONS
        && !existing.iter().any(|candidate| candidate == &manifest)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "backup generation limit reached",
        ));
    }
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "backup driver stopping",
        ));
    }
    status.set_phase("copy");
    let before = destination.write_metrics();
    let copy = backup::copy_generation(
        &capture.source,
        destination,
        Some(&context.store_dir),
        policy,
        &backup::GenerationCopyPlan {
            manifest: &manifest,
            built,
        },
        stop,
        Some(&status.progress),
        Some(share),
    )?;
    let after = destination.write_metrics();
    if stop.load(Ordering::Acquire) {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "backup driver stopping",
        ));
    }
    status.set_phase("publish");
    let manifest_bytes =
        backup::publish_manifest(snapshots_path, &manifest, context.destination_vfs.as_ref())?;
    let metrics: BackupMetrics = BackupMetrics {
        durable_sequence: manifest.durable_sequence,
        copied_blocks: after.blocks_written - before.blocks_written,
        copied_block_bytes: after.bytes_written - before.bytes_written,
        manifest_bytes,
    };
    let manifest_path = snapshots_path.join(manifest.file_name());
    Ok((metrics, copy.total(), manifest_path))
}

fn sha256_file(path: &Path, vfs: &dyn Vfs) -> io::Result<String> {
    use sha2::Digest;
    let mut file = vfs.open(
        path,
        crate::vfs::OpenRequest {
            read: true,
            ..crate::vfs::OpenRequest::default()
        },
    )?;
    let bytes = file.read_all(4096)?;
    let digest = sha2::Sha256::digest(&bytes);
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(encoded, "{byte:02x}");
    }
    Ok(encoded)
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod automatic_epoch_tests;
