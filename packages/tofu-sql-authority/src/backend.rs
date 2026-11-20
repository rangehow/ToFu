//! The SQLite backend (`adapters/sqlite.py::SQLiteBackend`): one ordered
//! physical writer, demand-created query-only readers, caller deadlines,
//! a per-transaction watchdog and exactly-once command receipts.
//!
//! Not ported (reported in health/metrics, documented in
//! docs/RUST_REWRITE_EVIDENCE.md P3b): the fastpath write front and WAL
//! shipper (a durable fastpath shadow makes startup refuse, as Python's
//! classic fallback does), the WAL-coalescing VFS and checkpointer, writer
//! batching of several jobs into one COMMIT.
//!
//! The turn-search projection (`crate::search`) starts after bootstrap; its
//! worker reaches the authority through the shared `BackendCore`.

use std::ops::Deref;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

use crate::control::{WorkerControl, WorkerLeaseClock};
use crate::error::{map_failure, Failure, OpResult, StorageError};
use crate::mount::{self, Mount};
use crate::params;
use crate::receipt;
use crate::schema;
use crate::session::{Session, SqlValue};
use crate::turn_projection_cache::TurnProjectionCache;

const WAL_AUTOCHECKPOINT_FRAMES: i64 = 1000;

pub struct Config {
    pub data_dir: PathBuf,
    pub sqlite_path: PathBuf,
    pub transaction_timeout_s: f64,
    pub writer_cache_mib: i64,
    pub read_pool_size: usize,
    pub rpc_capacity: i64,
    pub turn_search_projection_dir: PathBuf,
    pub turn_search_backfill_delay_s: f64,
    pub turn_projection_cache_mib: i64,
}

const RESOURCE_POLICY: &str = include_str!("../../../contracts/resource_policy_v1.json");

/// `SidecarConfig._bounded_int(name, deployment_resource_default(name))`:
/// the personal launch default from `contracts/resource_policy_v1.json`
/// unless the inherited launch environment sets the value.
fn launch_setting(name: &str, minimum: i64, maximum: i64) -> Result<i64, String> {
    let policy: Value = serde_json::from_str(RESOURCE_POLICY).expect("resource policy JSON");
    let default = policy
        .pointer(&format!("/settings/{name}/defaults/personal"))
        .or_else(|| find_setting(&policy, name))
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("unknown deployment resource: {name}"))?;
    let value = match std::env::var(name).ok().filter(|raw| !raw.is_empty()) {
        Some(raw) => raw.trim().parse::<i64>().map_err(|_| format!("invalid {name}"))?,
        None => default,
    };
    if !(minimum..=maximum).contains(&value) {
        return Err(format!("{name} must be between {minimum} and {maximum}"));
    }
    Ok(value)
}

fn find_setting<'v>(value: &'v Value, name: &str) -> Option<&'v Value> {
    match value {
        Value::Object(map) => map
            .get(name)
            .and_then(|setting| setting.pointer("/defaults/personal"))
            .or_else(|| map.values().find_map(|child| find_setting(child, name))),
        Value::Array(items) => items.iter().find_map(|child| find_setting(child, name)),
        _ => None,
    }
}

fn bounded_float(name: &str, default: f64, minimum: f64, maximum: f64) -> Result<f64, String> {
    let value = match std::env::var(name).ok().filter(|raw| !raw.is_empty()) {
        Some(raw) => raw.trim().parse::<f64>().map_err(|_| format!("invalid {name}"))?,
        None => default,
    };
    if !(minimum..=maximum).contains(&value) {
        return Err(format!("{name} must be between {minimum} and {maximum}"));
    }
    Ok(value)
}

impl Config {
    /// The launch configuration `SidecarConfig.from_environment` derives.
    pub fn from_environment(data_dir: PathBuf) -> Result<Self, String> {
        // The Rust authority runs only under the explicit test override, whose
        // projection lives in the disposable project root.
        let projection_override = std::env::var("TOFU_TURN_SEARCH_PROJECTION_DIR").unwrap_or_default();
        let projection_dir = match projection_override.trim() {
            "" => crate::search::resolve_directory(&data_dir.join("projections")),
            raw if Path::new(raw).is_absolute() => crate::search::resolve_directory(Path::new(raw)),
            _ => return Err("TOFU_TURN_SEARCH_PROJECTION_DIR must be absolute".into()),
        };
        Ok(Self {
            turn_search_projection_dir: projection_dir,
            turn_search_backfill_delay_s: bounded_float("TOFU_TURN_SEARCH_BACKFILL_DELAY_S", 60.0, 0.0, 3600.0)?,
            sqlite_path: data_dir.join("tofu.db"),
            data_dir,
            transaction_timeout_s: bounded_float("TOFU_STORAGE_TRANSACTION_TIMEOUT_S", 5.0, 1.0, 120.0)?,
            writer_cache_mib: launch_setting("TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB", 8, 256)?,
            read_pool_size: launch_setting("TOFU_STORAGE_SQLITE_READ_POOL", 1, 64)? as usize,
            rpc_capacity: launch_setting("TOFU_STORAGE_RPC_CAPACITY", 2, 256)?,
            turn_projection_cache_mib: launch_setting("TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB", 8, 1024)?,
        })
    }

    pub fn for_data_dir(data_dir: PathBuf) -> Self {
        Self {
            turn_search_projection_dir: crate::search::resolve_directory(&data_dir.join("projections")),
            turn_search_backfill_delay_s: 60.0,
            sqlite_path: data_dir.join("tofu.db"),
            data_dir,
            transaction_timeout_s: 5.0,
            writer_cache_mib: 32,
            read_pool_size: 16,
            rpc_capacity: 64,
            turn_projection_cache_mib: 16,
        }
    }
}

/// FIFO admission to the sole physical writer, bounded by caller deadlines.
struct WriterGate {
    state: Mutex<(u64, u64, Vec<u64>)>, // (next ticket, serving ticket, abandoned)
    condition: Condvar,
}

impl WriterGate {
    fn acquire(&self, deadline: Instant) -> Result<u64, StorageError> {
        let mut state = self.state.lock().expect("writer gate");
        let ticket = state.0;
        state.0 += 1;
        loop {
            if state.1 == ticket {
                return Ok(ticket);
            }
            let now = Instant::now();
            if now >= deadline {
                // Leave the queue: the ticket is skipped when it is reached.
                drop(state);
                self.abandon(ticket);
                return Err(StorageError::retryable("database_timeout", "Storage writer acquisition timed out", 25));
            }
            state = self.condition.wait_timeout(state, deadline - now).expect("writer gate").0;
        }
    }

    fn abandon(&self, ticket: u64) {
        let mut state = self.state.lock().expect("writer gate");
        state.2.push(ticket);
        Self::advance(&mut state);
        self.condition.notify_all();
    }

    fn advance(state: &mut (u64, u64, Vec<u64>)) {
        while let Some(position) = state.2.iter().position(|ticket| *ticket == state.1) {
            state.2.swap_remove(position);
            state.1 += 1;
        }
    }

    fn release(&self) {
        let mut state = self.state.lock().expect("writer gate");
        state.1 += 1;
        Self::advance(&mut state);
        self.condition.notify_all();
    }
}

pub struct Metrics {
    pub queries: u64,
    pub query_failures: u64,
    pub commands: u64,
    pub command_failures: u64,
    pub transaction_retries: u64,
    pub cancelled_before_start: u64,
    pub read_active_leases: u64,
}

/// The authority shared by the server and the turn-search worker.
pub struct Backend {
    core: Arc<BackendCore>,
}

impl Deref for Backend {
    type Target = BackendCore;

    fn deref(&self) -> &BackendCore {
        &self.core
    }
}

impl Backend {
    /// `SQLiteBackend.start`: bootstrap, then the reconstructible projection.
    pub fn start(config: Config) -> Result<Self, StorageError> {
        let core = Arc::new(BackendCore::start(config)?);
        let pipeline = crate::logical_outbox::LogicalOutboxPipeline::from_config(&core.config, &core)
            .map_err(|failure| map_failure(failure, false))?;
        if let Some(fastpath) = &core.fastpath {
            let weak = Arc::downgrade(&core);
            fastpath
                .start(Arc::new(move |deadline| {
                    let Some(core) = weak.upgrade() else {
                        return Err(StorageError::new("database_unavailable", "Storage writer is stopping").into());
                    };
                    let connection = core.writer.lock().expect("writer");
                    core.fastpath.as_ref().expect("selected fastpath").snapshot(&connection, deadline)
                }))
                .map_err(|failure| map_failure(failure, false))?;
        }
        if let Err(failure) = pipeline.start() {
            core.close();
            return Err(map_failure(failure, false));
        }
        core.logical_outbox.set(pipeline).ok().expect("one outbox lifecycle");
        core.search.start(&core);
        Ok(Self { core })
    }
}

pub struct BackendCore {
    pub config: Config,
    writer: Mutex<Connection>,
    backup_lock: Mutex<()>,
    gate: WriterGate,
    readers: Mutex<Vec<Connection>>,
    pub event_read_epoch: String,
    pub worker_control: WorkerControl,
    pub lease_clock: WorkerLeaseClock,
    pub mount: Mount,
    preflight: Value,
    fastpath_selection: Value,
    fastpath: Option<Arc<crate::fastpath::Fastpath>>,
    pub logical_outbox: OnceLock<Arc<crate::logical_outbox::LogicalOutboxPipeline>>,
    deferred_missing: Vec<String>,
    metrics: Mutex<Metrics>,
    turn_projection_cache: TurnProjectionCache,
    closed: Mutex<bool>,
    search: crate::search::Slot,
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn connect(path: &Path, query_only: bool, cache_mib: i64, fastpath: bool) -> Result<Connection, StorageError> {
    let unavailable = |_| StorageError::retryable("database_unavailable", "SQLite storage is unavailable", 100);
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let connection = Connection::open_with_flags(path, flags).map_err(unavailable)?;
    connection.busy_timeout(Duration::from_millis(2000)).map_err(unavailable)?;
    connection.execute_batch("PRAGMA foreign_keys=ON").map_err(unavailable)?;
    if query_only {
        connection.execute_batch("PRAGMA query_only=ON").map_err(unavailable)?;
    } else {
        let mode: String =
            connection.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0)).map_err(unavailable)?;
        if !mode.eq_ignore_ascii_case("wal") {
            return Err(StorageError::new("database_unavailable", "SQLite WAL mode is unavailable"));
        }
        connection
            .execute_batch(&format!(
                "PRAGMA synchronous=FULL; PRAGMA cache_size=-{}; PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_FRAMES};",
                cache_mib * 1024
            ))
            .map_err(unavailable)?;
    }
    if fastpath {
        crate::fastpath::configure(&connection).map_err(|failure| map_failure(failure, false))?;
    }
    connection.set_prepared_statement_cache_capacity(256);
    Ok(connection)
}

/// `_scratch_recovery_preflight`: prove WAL commit + reopen on this volume.
fn scratch_recovery_preflight(directory: &Path) -> Result<(), StorageError> {
    let path = directory.join(".storage-wal-preflight.sqlite3");
    let cleanup = || {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", path.display()));
        }
    };
    cleanup();
    let failed = || StorageError::new("database_integrity", "SQLite WAL recovery preflight failed");
    let result = (|| -> rusqlite::Result<bool> {
        let connection = Connection::open(&path)?;
        connection.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()))?;
        connection.execute_batch(
            "PRAGMA synchronous=FULL; CREATE TABLE durability_probe(value TEXT NOT NULL); BEGIN IMMEDIATE; \
             INSERT INTO durability_probe(value) VALUES ('committed'); COMMIT;",
        )?;
        drop(connection);
        let reopened = Connection::open(&path)?;
        let value: String = reopened.query_row("SELECT value FROM durability_probe", [], |row| row.get(0))?;
        let integrity: String = reopened.query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
        Ok(value == "committed" && integrity == "ok")
    })();
    cleanup();
    match result {
        Ok(true) => Ok(()),
        _ => Err(failed()),
    }
}

/// The deadline guard SQLite consults every 1000 VM steps.
fn arm_deadline(connection: &Connection, deadline: Instant) {
    connection.progress_handler(1000, Some(move || Instant::now() >= deadline));
}

fn disarm(connection: &Connection) {
    connection.progress_handler(1000, None::<fn() -> bool>);
}

fn run_operation<T>(operation: impl FnOnce() -> OpResult<T>) -> OpResult<T> {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(result) => result,
        Err(_) => Err(Failure::Internal("operation panicked".into())),
    }
}

impl BackendCore {
    /// `SQLiteBackend.start` up to the projection start.
    pub fn start(mut config: Config) -> Result<Self, StorageError> {
        std::fs::create_dir_all(&config.data_dir)
            .map_err(|_| StorageError::retryable("database_unavailable", "SQLite storage is unavailable", 100))?;
        let selection = crate::fastpath::select(&config.data_dir, &config.sqlite_path)
            .map_err(|failure| map_failure(failure, false))?;
        config.sqlite_path = selection.path.clone();
        let fastpath_selection = json!({"active": selection.active, "reason": selection.reason,
            "mode": selection.mode, "benchmark": selection.benchmark});
        let preflight = mount::filesystem_preflight(&config.data_dir)?;
        let mount = mount::describe(&config.data_dir);
        scratch_recovery_preflight(&config.data_dir)?;
        let writer = connect(&config.sqlite_path, false, config.writer_cache_mib, selection.active)?;
        let event_read_epoch = uuid::Uuid::new_v4().simple().to_string();
        let lease_clock = WorkerLeaseClock::default();
        let deferred_missing = Self::bootstrap(&writer, &event_read_epoch)?;
        let fastpath = if selection.active {
            let authority_uuid: String = writer
                .query_row("SELECT meta_value FROM storage_meta WHERE meta_key='authority_uuid'", [], |row| row.get(0))
                .map_err(|failure| map_failure(failure.into(), false))?;
            let fastpath = Arc::new(
                crate::fastpath::Fastpath::new(config.sqlite_path.clone(), &config.data_dir, authority_uuid)
                    .map_err(|failure| map_failure(failure, false))?,
            );
            fastpath
                .initialize(&writer, Instant::now() + Duration::from_secs(30))
                .map_err(|failure| map_failure(failure, false))?;
            Some(fastpath)
        } else {
            None
        };
        let turn_projection_cache = TurnProjectionCache::new(config.turn_projection_cache_mib * 1024 * 1024);
        Ok(Self {
            config,
            writer: Mutex::new(writer),
            backup_lock: Mutex::new(()),
            gate: WriterGate { state: Mutex::new((0, 0, Vec::new())), condition: Condvar::new() },
            readers: Mutex::new(Vec::new()),
            event_read_epoch,
            worker_control: WorkerControl::default(),
            lease_clock,
            mount,
            preflight,
            fastpath_selection,
            fastpath,
            logical_outbox: OnceLock::new(),
            deferred_missing,
            metrics: Mutex::new(Metrics {
                queries: 0,
                query_failures: 0,
                commands: 0,
                command_failures: 0,
                transaction_retries: 0,
                cancelled_before_start: 0,
                read_active_leases: 0,
            }),
            turn_projection_cache,
            closed: Mutex::new(false),
            search: crate::search::Slot::default(),
        })
    }

    fn bootstrap(writer: &Connection, epoch: &str) -> Result<Vec<String>, StorageError> {
        let integrity = |failure: Failure| map_failure(failure, false);
        let fresh = writer
            .query_row("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1", [], |_| Ok(()))
            .map(|_| false)
            .or_else(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => Ok(true),
                other => Err(other),
            })
            .map_err(|error| integrity(error.into()))?;
        if fresh {
            writer.execute_batch("PRAGMA auto_vacuum=INCREMENTAL; VACUUM;").map_err(|e| integrity(e.into()))?;
        }
        writer.execute_batch("BEGIN IMMEDIATE").map_err(|e| integrity(e.into()))?;
        let outcome = (|| -> OpResult<Vec<String>> {
            let session = Session::new(writer, epoch, None, true);
            schema::initialize_schema(&session)?;
            let deferred = &schema::contract().deferred_indexes;
            let mut missing = Vec::new();
            if fresh {
                for statement in deferred {
                    session.execute(statement, &[])?;
                }
            } else {
                for statement in deferred {
                    let name = schema::deferred_index_name(statement);
                    if session
                        .fetch_one(
                            "SELECT 1 FROM sqlite_schema WHERE type='index' AND name=?",
                            &params![name.as_str()],
                        )?
                        .is_none()
                    {
                        missing.push(name);
                    }
                }
            }
            session.execute(
                "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) \
                 ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",
                &params!["__startup_read_write_probe__", "ok"],
            )?;
            let probe = session.fetch_one(
                "SELECT meta_value FROM storage_meta WHERE meta_key = ?",
                &params!["__startup_read_write_probe__"],
            )?;
            if probe.map(|row| row.text("meta_value")) != Some("ok".into()) {
                return Err(StorageError::new("database_integrity", "SQLite read/write startup probe failed").into());
            }
            session.execute("DELETE FROM storage_meta WHERE meta_key = ?", &params!["__startup_read_write_probe__"])?;
            if session
                .fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params!["authority_uuid"])?
                .is_none()
            {
                session.execute(
                    "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)",
                    &params!["authority_uuid", uuid::Uuid::new_v4().simple().to_string()],
                )?;
            }
            Ok(missing)
        })();
        match outcome {
            Ok(missing) => {
                writer.execute_batch("COMMIT").map_err(|e| integrity(e.into()))?;
                Ok(missing)
            }
            Err(failure) => {
                let _ = writer.execute_batch("ROLLBACK");
                Err(integrity(failure))
            }
        }
    }

    fn acquire_reader(&self) -> Result<Connection, StorageError> {
        if let Some(connection) = self.readers.lock().expect("readers").pop() {
            return Ok(connection);
        }
        connect(&self.config.sqlite_path, true, 0, self.fastpath.is_some())
    }

    fn release_reader(&self, connection: Connection) {
        let mut readers = self.readers.lock().expect("readers");
        if readers.len() < self.config.read_pool_size.max(1) {
            readers.push(connection);
        }
    }

    /// `SQLiteBackend.query`: one read transaction, always rolled back.
    pub fn query<T>(
        &self,
        operation: impl FnOnce(&Session<'_>) -> OpResult<T>,
        deadline: Instant,
    ) -> Result<T, StorageError> {
        let connection = self.acquire_reader()?;
        self.metrics.lock().expect("metrics").read_active_leases += 1;
        let _search_scope = crate::search::QueryScope::enter(&self.search, deadline);
        arm_deadline(&connection, deadline);
        let result = (|| -> OpResult<T> {
            connection.execute_batch("BEGIN")?;
            let session = Session::new(&connection, &self.event_read_epoch, Some(&self.lease_clock), false)
                .with_turn_projection_cache(&self.turn_projection_cache);
            run_operation(|| operation(&session))
        })();
        let rollback_ok = connection.execute_batch("ROLLBACK").is_ok() || connection.is_autocommit();
        disarm(&connection);
        if rollback_ok {
            self.release_reader(connection);
        }
        let mut metrics = self.metrics.lock().expect("metrics");
        metrics.read_active_leases -= 1;
        match result {
            Ok(value) => {
                metrics.queries += 1;
                Ok(value)
            }
            Err(failure) => {
                metrics.query_failures += 1;
                Err(map_failure(failure, Instant::now() >= deadline))
            }
        }
    }

    /// One writer transaction with the watchdog, rollback-before-classify and
    /// the bounded busy retry of `_FairWriter._transaction`.
    fn transaction(
        &self,
        operation: &dyn Fn(&Session<'_>) -> OpResult<Value>,
        deadline: Instant,
        transaction_timeout_s: Option<f64>,
        replay_safe: bool,
    ) -> Result<Value, StorageError> {
        let connection = self.writer.lock().expect("writer");
        let mut attempts = 0;
        loop {
            let budget = Duration::from_secs_f64(transaction_timeout_s.unwrap_or(self.config.transaction_timeout_s));
            let transaction_deadline = deadline.min(Instant::now() + budget);
            arm_deadline(&connection, transaction_deadline);
            let mut committing = false;
            let session = Session::new(&connection, &self.event_read_epoch, Some(&self.lease_clock), true)
                .with_turn_projection_cache(&self.turn_projection_cache);
            let result = (|| -> OpResult<Value> {
                connection.execute_batch("BEGIN IMMEDIATE")?;
                let value = run_operation(|| operation(&session))?;
                if Instant::now() >= transaction_deadline {
                    return Err(StorageError::retryable(
                        "database_timeout",
                        "Storage transaction exceeded its watchdog",
                        25,
                    )
                    .into());
                }
                committing = true;
                connection.execute_batch("COMMIT")?;
                Ok(value)
            })();
            disarm(&connection);
            match result {
                Ok(value) => {
                    let hints = session.worker_hints.into_inner();
                    self.worker_control.publish(
                        hints.queue_changed,
                        &hints.cancelled_tasks,
                        &hints.conversation_changes,
                    );
                    return Ok(value);
                }
                Err(failure) => {
                    let still_active = !connection.is_autocommit();
                    let replay = replay_safe || !committing || still_active;
                    if still_active && connection.execute_batch("ROLLBACK").is_err() {
                        // A poisoned writer must never serve another job; the
                        // supervisor restarts the authority.
                        eprintln!("SQLite writer rollback failed; exiting for a supervised restart");
                        std::process::exit(70);
                    }
                    let mapped = map_failure(failure, Instant::now() >= transaction_deadline);
                    if replay
                        && mapped.retryable
                        && (mapped.code == "database_busy" || mapped.code == "database_unavailable")
                        && attempts < 3
                        && Instant::now() + Duration::from_millis(20) < deadline
                    {
                        attempts += 1;
                        self.metrics.lock().expect("metrics").transaction_retries += 1;
                        std::thread::sleep(Duration::from_millis(10 * (1 << attempts)));
                        continue;
                    }
                    return Err(mapped);
                }
            }
        }
    }

    /// `SQLiteBackend.command` (receipts) through the ordered writer.
    #[allow(clippy::too_many_arguments)]
    pub fn command(
        &self,
        operation_name: &str,
        payload_digest: &str,
        command_id: Option<&str>,
        operation: &dyn Fn(&Session<'_>) -> OpResult<Value>,
        deadline: Instant,
        receipt_required: bool,
        transaction_timeout_s: Option<f64>,
    ) -> Result<Value, StorageError> {
        if receipt_required && !command_id.is_some_and(|id| !id.is_empty() && id.chars().count() <= 200) {
            return Err(StorageError::protocol("A valid command_id is required"));
        }
        let identity = if receipt_required {
            Some(receipt::command_receipt_identity_v2(command_id.unwrap_or_default(), operation_name, payload_digest)?)
        } else {
            None
        };
        if operation_name == "system.reclaim" && !mount::online_reclaim_allowed(self.mount.storage_class) {
            return Ok(json!({
                "reclaimed": 0,
                "offline_required": true,
                "reason_code": "unsupported_storage_topology",
                "reason": format!("automatic SQLite page relocation is disabled on {}", self.mount.storage_class),
                "storage_class": self.mount.storage_class,
                "filesystem_type": self.mount.filesystem_type,
                "authority_path": self.config.sqlite_path.display().to_string(),
            }));
        }
        let transactional = |session: &Session<'_>| -> OpResult<Value> {
            if let Some((command_key, request_digest)) = &identity {
                let rows = session.fetch_all(
                    receipt::COMMAND_RECEIPT_LOOKUP_SQL,
                    &params![
                        operation_name,
                        payload_digest,
                        command_id.unwrap_or_default(),
                        operation_name,
                        request_digest.clone(),
                        command_key.clone()
                    ],
                )?;
                if let Some(replay) = receipt::decode_command_receipt_lookup(&rows)? {
                    return Ok(replay);
                }
            }
            let response = operation(session)?;
            if let Some((command_key, request_digest)) = &identity {
                if receipt::receipt_cacheable(&response) {
                    session.execute(
                        "INSERT INTO storage_command_receipts_v2(command_key, operation, request_digest, \
                         response_json, committed_at_ms) VALUES (?, ?, ?, ?, ?)",
                        &params![
                            command_key.clone(),
                            operation_name,
                            request_digest.clone(),
                            SqlValue::Blob(receipt::encode_receipt_response(&response)),
                            now_ms()
                        ],
                    )?;
                }
            }
            Ok(response)
        };
        if *self.closed.lock().expect("closed") {
            return Err(StorageError::retryable("database_unavailable", "Storage writer is stopping", 100));
        }
        if let Err(error) = self.gate.acquire(deadline) {
            self.metrics.lock().expect("metrics").cancelled_before_start += 1;
            return Err(error);
        }
        let result = self.transaction(&transactional, deadline, transaction_timeout_s, receipt_required);
        self.gate.release();
        if result.is_ok() {
            if let Some(fastpath) = &self.fastpath {
                fastpath.notify();
            }
            self.search.wake();
        }
        let mut metrics = self.metrics.lock().expect("metrics");
        metrics.commands += 1;
        if result.is_err() {
            metrics.command_failures += 1;
        }
        result
    }

    pub fn health(&self) -> Value {
        let mut health = json!({
            "ready": !*self.closed.lock().expect("closed"),
            "backend": "sqlite",
            "protocol": "storage.v1",
            "preflight": self.preflight,
            "authority_implementation": "rust",
        });
        if let Some(projection) = self.search.health() {
            health["turn_search_projection"] = projection;
        }
        health
    }

    pub fn metrics(&self) -> Value {
        let metrics = self.metrics.lock().expect("metrics");
        json!({
            "backend": "sqlite",
            "sqlite_version": rusqlite::version(),
            "read_pool_size": self.readers.lock().expect("readers").len(),
            "read_pool_capacity": 0,
            "read_idle_reuse_hint": self.config.read_pool_size,
            "read_active_leases": metrics.read_active_leases,
            "writer_cache_mib": self.config.writer_cache_mib,
            "turn_projection_cache": self.turn_projection_cache.stats(),
            "queries": {"queries": metrics.queries, "query_failures": metrics.query_failures},
            "writer": {
                "commands": metrics.commands,
                "command_failures": metrics.command_failures,
                "transaction_retries": metrics.transaction_retries,
                "queue_capacity": 0,
                // Admission never refuses work (capacity 0), so this stays 0.
                "queue_rejections": 0,
                "cancelled_before_start": metrics.cancelled_before_start,
            },
            "fastpath": self.fastpath_metrics(),
            "deferred_schema": {
                "automatic_build": "fresh-authority-only",
                "missing_indexes": self.deferred_missing,
            },
            "turn_search_projection": self.search.metrics(),
        })
    }

    fn fastpath_metrics(&self) -> Value {
        let mut metrics = self.fastpath_selection.clone();
        if let Some(fastpath) = &self.fastpath {
            if let Some(status) = fastpath.metrics().as_object() {
                metrics.as_object_mut().expect("selection metrics").extend(status.clone());
            }
        }
        metrics
    }

    /// `SQLiteBackend.backup`: a separate query-only source connection, so
    /// the online copy never waits while holding the physical writer.
    pub fn backup(&self, deadline: Instant) -> Result<Value, StorageError> {
        let _backup = match self.backup_lock.try_lock() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err(StorageError::retryable("database_busy", "A SQLite backup is already running", 1000));
            }
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err(StorageError::new("database_internal", "Storage operation failed"));
            }
        };
        let project_root = self.config.data_dir.parent().unwrap_or(&self.config.data_dir);
        if let Some(fastpath) = &self.fastpath {
            let connection = self.writer.lock().expect("writer");
            return fastpath
                .backup(&connection, &self.config.data_dir, project_root, deadline)
                .map_err(|failure| map_failure(failure, Instant::now() >= deadline));
        }
        let connection = self.acquire_reader()?;
        let result = crate::backup::backup_from_connection(
            &connection,
            &self.config.sqlite_path,
            &self.config.data_dir,
            project_root,
            deadline,
        );
        self.release_reader(connection);
        result.map_err(|failure| map_failure(failure, Instant::now() >= deadline))
    }

    pub fn baseline(&self, deadline: Instant) -> Result<Value, StorageError> {
        self.query(|session| crate::backup::baseline(session, &self.config.sqlite_path, deadline), deadline)
    }

    /// `system.integrity_check`.
    pub fn integrity_check(&self, deadline: Instant) -> Result<Value, StorageError> {
        self.query(
            |session| {
                let row = session.fetch_one("PRAGMA integrity_check", &[])?;
                let value = row.map(|row| row.first().text_or_empty()).unwrap_or_default();
                Ok(json!({"ok": value == "ok", "result": value}))
            },
            deadline,
        )
    }

    pub fn close(&self) {
        if let Some(pipeline) = self.logical_outbox.get() {
            pipeline.close();
        }
        *self.closed.lock().expect("closed") = true;
        self.search.close();
        if let Some(fastpath) = &self.fastpath {
            if let Err(error) = fastpath.close() {
                eprintln!("fastpath final shipping failed: {error:?}");
            }
        }
        self.worker_control.close();
        self.readers.lock().expect("readers").clear();
        self.turn_projection_cache.clear();
    }
}

#[cfg(test)]
mod writer_gate_tests {
    use super::*;

    fn gate() -> WriterGate {
        WriterGate { state: Mutex::new((0, 0, Vec::new())), condition: Condvar::new() }
    }

    #[test]
    fn abandoned_tickets_are_private_to_one_authority() {
        let first = gate();
        let second = gate();
        assert_eq!(first.acquire(Instant::now()).unwrap(), 0);
        assert_eq!(second.acquire(Instant::now()).unwrap(), 0);
        assert!(first.acquire(Instant::now()).is_err());
        second.release();
        assert_eq!(second.acquire(Instant::now()).unwrap(), 1);
        first.release();
        assert_eq!(first.acquire(Instant::now()).unwrap(), 2);
    }
}
