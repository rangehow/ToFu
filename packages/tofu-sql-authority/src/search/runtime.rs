//! `turn_search_projection.py::TurnSearchProjectionRuntime`: one background
//! worker consuming the authority's transactional dirty set
//! (`storage_projection_outbox`) and materializing the disposable local
//! projection. Its failure degrades search only; it never holds or weakens
//! the authority writer beyond its own short maintenance commands.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::sources::{history_page, turn_snapshot, value_error_reason};
use super::target::{failure_type_name, LocalTarget, StartError};
use crate::backend::BackendCore;
use crate::error::{Failure, OpResult, StorageError};
use crate::json;
use crate::ops::conversations::support::py_str;
use crate::params;
use crate::session::{json_param, Row, Session, SqlValue};

pub const PROJECTION_NAME: &str = "turn_search.v1";
const DIRTY_BATCH: i64 = 16;
const BACKFILL_ROWS: usize = 8;
const BACKFILL_BYTES: i64 = 2_000_000;
const IDLE_POLL: Duration = Duration::from_secs(2);
const RETRY: Duration = Duration::from_millis(250);
const AUTHORITY_DEADLINE: Duration = Duration::from_secs(5);
const SHUTDOWN_JOIN: Duration = Duration::from_secs(5);
const BACKFILL_TOKEN: &str = "backfill-v2";

/// `threading.Event`.
#[derive(Default)]
struct Event {
    flag: Mutex<bool>,
    condition: Condvar,
}

impl Event {
    fn set(&self) {
        *self.flag.lock().expect("event") = true;
        self.condition.notify_all();
    }

    fn clear(&self) {
        *self.flag.lock().expect("event") = false;
    }

    fn is_set(&self) -> bool {
        *self.flag.lock().expect("event")
    }

    fn wait(&self, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        let mut flag = self.flag.lock().expect("event");
        while !*flag {
            let now = Instant::now();
            if now >= deadline {
                return;
            }
            flag = self.condition.wait_timeout(flag, deadline - now).expect("event").0;
        }
    }
}

#[derive(Clone, Copy, Default)]
struct Metrics {
    dirty_applied: i64,
    dirty_acked: i64,
    backfill_scanned: i64,
    backfill_indexed: i64,
    oversize_skipped: i64,
    failures: i64,
}

impl Metrics {
    fn to_json(self) -> Value {
        json!({
            "dirty_applied": self.dirty_applied,
            "dirty_acked": self.dirty_acked,
            "backfill_scanned": self.backfill_scanned,
            "backfill_indexed": self.backfill_indexed,
            "oversize_skipped": self.oversize_skipped,
            "failures": self.failures,
        })
    }
}

struct Observed {
    search_status: Map<String, Value>,
    metrics: Metrics,
    last_error: String,
}

struct Shared {
    stop: Event,
    wake: Event,
    observed: Mutex<Observed>,
    target_closed: Mutex<bool>,
    alive: Mutex<bool>,
    exited: Condvar,
}

impl Shared {
    fn metrics<R>(&self, update: impl FnOnce(&mut Metrics) -> R) -> R {
        update(&mut self.observed.lock().expect("projection metrics").metrics)
    }
}

fn close_target_once(shared: &Shared, target: &LocalTarget) {
    let mut closed = shared.target_closed.lock().expect("target close");
    if *closed {
        return;
    }
    target.close();
    *closed = true;
}

pub struct Runtime {
    target: Arc<LocalTarget>,
    shared: Arc<Shared>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

fn coverage_reasons(metrics: &Metrics) -> Value {
    if metrics.oversize_skipped != 0 {
        json!(["source_skipped"])
    } else {
        json!([])
    }
}

impl Runtime {
    /// `TurnSearchProjectionRuntime(...).start()`; `Err` is the exception class name.
    pub fn start(backend: Arc<BackendCore>, target: LocalTarget, backfill_delay_s: f64) -> Result<Arc<Self>, String> {
        let target = Arc::new(target);
        target.start().map_err(|StartError(name)| name)?;
        let search_status = target.status().map_err(|failure| failure_type_name(&failure))?;
        let skipped = search_status.get("source_skipped").and_then(Value::as_i64).unwrap_or(0);
        let shared = Arc::new(Shared {
            stop: Event::default(),
            wake: Event::default(),
            observed: Mutex::new(Observed {
                search_status,
                metrics: Metrics { oversize_skipped: skipped, ..Metrics::default() },
                last_error: String::new(),
            }),
            target_closed: Mutex::new(false),
            alive: Mutex::new(true),
            exited: Condvar::new(),
        });
        let mut worker = Worker {
            backend,
            target: Arc::clone(&target),
            shared: Arc::clone(&shared),
            jobs: Vec::new(),
            backfill_after: Instant::now() + Duration::from_secs_f64(backfill_delay_s.max(0.0)),
            persisted_skips: 0,
        };
        let spawned = std::thread::Builder::new().name("turn-search-projection".into()).spawn(move || worker.run());
        match spawned {
            Ok(handle) => Ok(Arc::new(Self { target, shared, thread: Mutex::new(Some(handle)) })),
            Err(_) => {
                *shared.alive.lock().expect("worker alive") = false;
                close_target_once(&shared, &target);
                Err("RuntimeError".into())
            }
        }
    }

    pub fn wake(&self) {
        self.shared.wake.set();
    }

    pub fn query<T>(&self, operation: impl FnOnce(&Session<'_>) -> OpResult<T>, deadline: Instant) -> OpResult<T> {
        self.target.query(operation, deadline)
    }

    /// `search_status()`: lock-free in Python; one short mutex here.
    pub fn search_status(&self) -> Map<String, Value> {
        let observed = self.shared.observed.lock().expect("projection status");
        let mut status = observed.search_status.clone();
        status.insert("metrics".into(), observed.metrics.to_json());
        status.insert("last_worker_error".into(), Value::String(observed.last_error.clone()));
        status.insert("coverage_reasons".into(), coverage_reasons(&observed.metrics));
        status
    }

    pub fn status(&self) -> Value {
        let mut status = self.target.status().unwrap_or_default();
        let observed = self.shared.observed.lock().expect("projection status");
        let metrics = observed.metrics;
        let coverage = if metrics.oversize_skipped != 0 {
            "partial"
        } else if status.get("backfill_complete").is_some_and(json::truthy) {
            "ready"
        } else {
            "indexing"
        };
        status.insert("coverage".into(), Value::String(coverage.into()));
        status.insert("coverage_reasons".into(), coverage_reasons(&metrics));
        let alive = self.thread.lock().expect("projection thread").is_some()
            && *self.shared.alive.lock().expect("worker alive");
        status.insert("worker_alive".into(), Value::Bool(alive));
        status.insert("metrics".into(), metrics.to_json());
        status.insert("last_worker_error".into(), Value::String(observed.last_error.clone()));
        Value::Object(status)
    }

    /// `close()`: stop, then a bounded join; a worker stuck in authority I/O
    /// keeps ownership of the target close.
    pub fn close(&self) {
        self.shared.stop.set();
        self.shared.wake.set();
        let mut thread = self.thread.lock().expect("projection thread");
        let Some(handle) = thread.take() else {
            close_target_once(&self.shared, &self.target);
            return;
        };
        let deadline = Instant::now() + SHUTDOWN_JOIN;
        let mut alive = self.shared.alive.lock().expect("worker alive");
        while *alive {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            alive = self.shared.exited.wait_timeout(alive, deadline - now).expect("worker alive").0;
        }
        if *alive {
            drop(alive);
            self.shared.observed.lock().expect("projection status").last_error =
                "shutdown_waiting_for_authority_io".into();
            eprintln!(
                "[turn-search] projection shutdown left one bounded worker waiting for authority I/O; \
                 target close is deferred"
            );
            *thread = Some(handle);
            return;
        }
        drop(alive);
        let _ = handle.join();
    }
}

struct Dirty {
    kind: String,
    user_id: i64,
    key: String,
    token: String,
}

impl Dirty {
    fn from_row(row: &Row) -> OpResult<Self> {
        let user_id = row.require("user_id")?.as_int().ok_or_else(|| Failure::Internal("TypeError: int()".into()))?;
        Ok(Self {
            kind: py_str(row.require("entity_kind")?),
            user_id,
            key: py_str(row.require("entity_key")?),
            token: py_str(row.require("version_token")?),
        })
    }
}

#[derive(PartialEq)]
enum Phase {
    Turns,
    History,
}

struct Job {
    token: String,
    header: Row,
    cursor: String,
    history_cursor: String,
    phase: Phase,
}

struct Page {
    rows: Vec<Row>,
    cursor: String,
    remaining: bool,
    skipped: i64,
}

fn authority<T>(result: Result<T, StorageError>) -> OpResult<T> {
    result.map_err(Failure::Storage)
}

fn skippable(error: &StorageError) -> bool {
    error.code == "database_integrity" || error.code == "database_internal"
}

/// `_materialize_candidates(session, candidates=..., cursor=...)`.
fn materialize_candidates(session: &Session<'_>, candidates: &[(Row, String)], cursor: &str) -> OpResult<Page> {
    let mut rows = Vec::new();
    let mut total_bytes = 0i64;
    let mut next_cursor = cursor.to_owned();
    let mut skipped = 0i64;
    let mut remaining = candidates.len() > BACKFILL_ROWS;
    for (candidate, candidate_cursor) in candidates.iter().take(BACKFILL_ROWS) {
        let user_id =
            candidate.require("user_id")?.as_int().ok_or_else(|| Failure::Internal("TypeError: int()".into()))?;
        let turn_id = py_str(candidate.require("turn_id")?);
        let row = match turn_snapshot(session, user_id, &turn_id) {
            Ok(row) => row,
            Err(failure) => {
                if let Some(reason) = value_error_reason(&failure) {
                    if reason == "source_changed" {
                        return Err(StorageError::retryable(
                            "database_unavailable",
                            "Search source changed during projection",
                            25,
                        )
                        .into());
                    }
                } else {
                    match &failure {
                        Failure::Storage(error) if skippable(error) => {}
                        _ => return Err(failure),
                    }
                }
                skipped += 1;
                next_cursor = candidate_cursor.clone();
                continue;
            }
        };
        let Some(row) = row else {
            next_cursor = candidate_cursor.clone();
            continue;
        };
        let size = match row.get("projection_json") {
            SqlValue::Blob(bytes) => bytes.len() as i64,
            other => other.text_or_empty().len() as i64,
        };
        if !rows.is_empty() && total_bytes + size * 4 > BACKFILL_BYTES {
            remaining = true;
            break;
        }
        rows.push(row);
        total_bytes += size * 4;
        next_cursor = candidate_cursor.clone();
    }
    Ok(Page { rows, cursor: next_cursor, remaining, skipped })
}

struct Worker {
    backend: Arc<BackendCore>,
    target: Arc<LocalTarget>,
    shared: Arc<Shared>,
    /// Insertion-ordered like the Python dict (at most `_DIRTY_BATCH + 1`).
    jobs: Vec<((i64, String), Job)>,
    backfill_after: Instant,
    persisted_skips: i64,
}

impl Worker {
    fn deadline() -> Instant {
        Instant::now() + AUTHORITY_DEADLINE
    }

    fn authority_command(&self, name: &str, callback: &dyn Fn(&Session<'_>) -> OpResult<Value>) -> OpResult<Value> {
        let digest = hex::encode(Sha256::digest(name.as_bytes()));
        authority(self.backend.command(name, &digest, None, callback, Self::deadline(), false, None))
    }

    fn job(&self, identity: &(i64, String)) -> Option<&Job> {
        self.jobs.iter().find(|(key, _)| key == identity).map(|(_, job)| job)
    }

    fn job_mut(&mut self, identity: &(i64, String)) -> Option<&mut Job> {
        self.jobs.iter_mut().find(|(key, _)| key == identity).map(|(_, job)| job)
    }

    fn pop_job(&mut self, identity: &(i64, String)) {
        self.jobs.retain(|(key, _)| key != identity);
    }

    fn list_dirty(&self) -> OpResult<Vec<Row>> {
        authority(self.backend.query(
            |session| {
                session.fetch_all(
                    "SELECT projection_name,entity_kind,user_id,entity_key,\
                     version_token,enqueued_at_ms FROM storage_projection_outbox \
                     WHERE projection_name=? ORDER BY enqueued_at_ms,entity_kind,\
                     user_id,entity_key LIMIT ?",
                    &params![PROJECTION_NAME, DIRTY_BATCH],
                )
            },
            Self::deadline(),
        ))
    }

    fn ack(&self, dirty: &Dirty) -> OpResult<bool> {
        let changed = self.authority_command("projection.turn_search.outbox_ack", &|session| {
            Ok(Value::from(session.execute(
                "DELETE FROM storage_projection_outbox \
                 WHERE projection_name=? AND entity_kind=? AND user_id=? \
                 AND entity_key=? AND version_token=?",
                &params![PROJECTION_NAME, dirty.kind.as_str(), dirty.user_id, dirty.key.as_str(), dirty.token.as_str()],
            )?))
        })?;
        let acked = json::truthy(&changed);
        if acked {
            self.shared.metrics(|metrics| metrics.dirty_acked += 1);
        }
        Ok(acked)
    }

    fn turn_snapshot(&self, dirty: &Dirty) -> OpResult<Option<Row>> {
        match self.backend.query(|session| turn_snapshot(session, dirty.user_id, &dirty.key), Self::deadline()) {
            Ok(row) => Ok(row),
            Err(error) if skippable(&error) => {
                self.shared.metrics(|metrics| metrics.oversize_skipped += 1);
                Ok(None)
            }
            Err(error) => Err(error.into()),
        }
    }

    fn conversation_header(&self, dirty: &Dirty) -> OpResult<Option<Row>> {
        authority(self.backend.query(
            |session| {
                session.fetch_one(
                    "SELECT id,user_id,updated_at_ms,title FROM storage_conversations \
                     WHERE user_id=? AND id=?",
                    &params![dirty.user_id, dirty.key.as_str()],
                )
            },
            Self::deadline(),
        ))
    }

    fn conversation_page(&self, dirty: &Dirty, cursor: &str) -> OpResult<Page> {
        authority(self.backend.query(
            |session| {
                let candidates = session.fetch_all(
                    "SELECT t.user_id,t.conversation_id,t.turn_id,length(t.projection_json) AS projection_units,\
                     t.turn_id AS candidate_cursor \
                     FROM storage_conversation_turns AS t \
                     WHERE t.user_id=? AND t.conversation_id=? \
                     AND t.lane_id='main' \
                     AND t.status NOT IN ('pending','running') \
                     AND t.turn_id>? ORDER BY t.turn_id LIMIT ?",
                    &params![dirty.user_id, dirty.key.as_str(), cursor, BACKFILL_ROWS as i64 + 1],
                )?;
                let decorated: Vec<(Row, String)> = candidates
                    .into_iter()
                    .map(|row| {
                        let candidate_cursor = py_str(row.get("candidate_cursor"));
                        (row, candidate_cursor)
                    })
                    .collect();
                materialize_candidates(session, &decorated, cursor)
            },
            Self::deadline(),
        ))
    }

    fn process_turn(&mut self, dirty: &Dirty) -> OpResult<()> {
        let snapshot = self.turn_snapshot(dirty)?;
        let token = match &snapshot {
            Some(snapshot) => {
                let identity = (dirty.user_id, py_str(snapshot.require("conversation_id")?));
                self.job(&identity).map(|job| job.token.clone())
            }
            None => None,
        }
        .unwrap_or_else(|| dirty.token.clone());
        self.target.apply_turn(dirty.user_id, &dirty.key, snapshot.as_ref(), &token)?;
        self.shared.metrics(|metrics| metrics.dirty_applied += 1);
        self.ack(dirty)?;
        Ok(())
    }

    /// `_process_conversation`: at most one source page per round.
    fn process_conversation(&mut self, dirty: &Dirty) -> OpResult<bool> {
        let token = dirty.token.clone();
        let identity = (dirty.user_id, dirty.key.clone());
        let current = self.job(&identity).map(|job| job.token == token);
        if current != Some(true) {
            let header = self.conversation_header(dirty)?;
            self.target.begin_conversation(dirty.user_id, &dirty.key, header.as_ref(), &token)?;
            let Some(header) = header else {
                self.pop_job(&identity);
                self.ack(dirty)?;
                return Ok(true);
            };
            if self.jobs.len() > DIRTY_BATCH as usize {
                self.jobs.remove(0);
            }
            let job = Job {
                token: token.clone(),
                header,
                cursor: String::new(),
                history_cursor: String::new(),
                phase: Phase::Turns,
            };
            match self.job_mut(&identity) {
                Some(existing) => *existing = job,
                None => self.jobs.push((identity.clone(), job)),
            }
        }
        if self.shared.stop.is_set() {
            return Ok(false);
        }
        let (turns_phase, cursor, history_cursor) = {
            let job = self.job(&identity).expect("conversation job");
            (job.phase == Phase::Turns, job.cursor.clone(), job.history_cursor.clone())
        };
        if turns_phase {
            let page = self.conversation_page(dirty, &cursor)?;
            if !page.rows.is_empty() {
                self.target.apply_conversation_page(&page.rows, &token)?;
            }
            self.shared.metrics(|metrics| metrics.oversize_skipped += page.skipped);
            let job = self.job_mut(&identity).expect("conversation job");
            if page.remaining {
                if page.cursor == job.cursor {
                    return Err(StorageError::new("database_internal", "Conversation search cursor stalled").into());
                }
                job.cursor = page.cursor;
                return Ok(false);
            }
            job.phase = Phase::History;
            return Ok(false);
        }
        let page = authority(
            self.backend
                .query(|session| history_page(session, dirty.user_id, &dirty.key, &history_cursor), Self::deadline()),
        )?;
        let (title, updated_at_ms) = {
            let job = self.job(&identity).expect("conversation job");
            (job.header.require("title")?.clone(), job.header.require("updated_at_ms")?.clone())
        };
        let mut rows = page.rows;
        for row in &mut rows {
            row.set("conversation_title", title.clone());
            row.set("conversation_updated_at_ms", updated_at_ms.clone());
        }
        if !rows.is_empty() {
            self.target.apply_conversation_page(&rows, &token)?;
        }
        self.shared.metrics(|metrics| metrics.oversize_skipped += page.skipped);
        if self.shared.stop.is_set() {
            return Ok(false);
        }
        if page.remaining {
            let job = self.job_mut(&identity).expect("conversation job");
            if page.cursor == job.history_cursor {
                return Err(StorageError::new("database_internal", "Conversation history search cursor stalled").into());
            }
            job.history_cursor = page.cursor;
            return Ok(false);
        }
        self.target.finalize_conversation(dirty.user_id, &dirty.key, &token)?;
        self.pop_job(&identity);
        self.shared.metrics(|metrics| metrics.dirty_applied += 1);
        self.ack(dirty)?;
        Ok(true)
    }

    fn process_rebuild(&mut self, dirty: &Dirty) -> OpResult<()> {
        self.target.reset()?;
        self.jobs.clear();
        self.shared.metrics(|metrics| metrics.oversize_skipped = 0);
        self.target.set_backfill_state("", false)?;
        self.backfill_after = Instant::now();
        self.shared.metrics(|metrics| metrics.dirty_applied += 1);
        self.ack(dirty)?;
        Ok(())
    }

    fn drain_dirty(&mut self) -> OpResult<bool> {
        let rows = self.list_dirty()?;
        for row in &rows {
            let dirty = Dirty::from_row(row)?;
            match dirty.kind.as_str() {
                "turn" => self.process_turn(&dirty)?,
                "conversation" => {
                    self.process_conversation(&dirty)?;
                }
                "rebuild" => self.process_rebuild(&dirty)?,
                _ => {}
            }
        }
        Ok(!rows.is_empty())
    }

    fn advance_backfill(&mut self) -> OpResult<bool> {
        let (mut cursor, complete) = self.target.backfill_state()?;
        if complete {
            return Ok(false);
        }
        let (owner, conv) = match json::loads(cursor.as_bytes()) {
            Ok(Value::Array(items)) if !cursor.is_empty() && items.len() == 2 => {
                match (json_param(&items[0]), json_param(&items[1])) {
                    (Ok(owner), Ok(conv)) => (owner, conv),
                    _ => (SqlValue::Integer(0), SqlValue::Text(String::new())),
                }
            }
            _ => (SqlValue::Integer(0), SqlValue::Text(String::new())),
        };
        let headers = authority(self.backend.query(
            |session| {
                session.fetch_all(
                    "SELECT user_id,id FROM storage_conversations \
                     WHERE user_id>? OR (user_id=? AND id>?) ORDER BY user_id,id LIMIT 1",
                    &params![&owner, &owner, &conv],
                )
            },
            Self::deadline(),
        ))?;
        for header in &headers {
            let user_id =
                header.require("user_id")?.as_int().ok_or_else(|| Failure::Internal("TypeError: int()".into()))?;
            let id = py_str(header.require("id")?);
            let dirty = Dirty { kind: "conversation".into(), user_id, key: id.clone(), token: BACKFILL_TOKEN.into() };
            if self.job(&(user_id, id.clone())).is_some_and(|job| job.token != BACKFILL_TOKEN) {
                return Ok(true);
            }
            if !self.process_conversation(&dirty)? {
                return Ok(!self.shared.stop.is_set());
            }
            let encoded = json::dumps(&Value::Array(vec![header.require("user_id")?.to_json()?, Value::String(id)]));
            cursor = String::from_utf8_lossy(&encoded).into_owned();
            self.target.set_backfill_state(&cursor, false)?;
            self.shared.metrics(|metrics| metrics.backfill_scanned += 1);
        }
        let done = headers.is_empty();
        self.target.set_backfill_state(&cursor, done)?;
        Ok(!done)
    }

    fn round(&mut self, did_work: &mut bool) -> OpResult<()> {
        *did_work = self.drain_dirty()?;
        if Instant::now() >= self.backfill_after {
            *did_work = self.advance_backfill()? || *did_work;
        }
        let skipped = self.shared.metrics(|metrics| metrics.oversize_skipped);
        if skipped != self.persisted_skips {
            self.target.record_skips(skipped)?;
            self.persisted_skips = skipped;
        }
        let status = self.target.status()?;
        let mut observed = self.shared.observed.lock().expect("projection status");
        observed.search_status = status;
        observed.last_error = String::new();
        Ok(())
    }

    fn run(&mut self) {
        while !self.shared.stop.is_set() {
            let mut did_work = false;
            let outcome = catch_unwind(AssertUnwindSafe(|| self.round(&mut did_work)));
            match outcome {
                Ok(Ok(())) => {}
                Ok(Err(Failure::Storage(error))) => {
                    {
                        let mut observed = self.shared.observed.lock().expect("projection status");
                        observed.metrics.failures += 1;
                        observed.last_error = format!("{}:{}", error.code, error.message);
                    }
                    if error.code == "database_unavailable" && error.message.contains("resource budget") {
                        eprintln!("[turn-search] projection paused: {}", error.message);
                        self.shared.stop.wait(IDLE_POLL);
                    } else {
                        eprintln!("[turn-search] projection retryable failure: {}", error.message);
                        self.shared.stop.wait(RETRY);
                    }
                }
                Ok(Err(failure)) => {
                    self.record_failure(failure_type_name(&failure));
                }
                Err(_) => self.record_failure("Exception".into()),
            }
            if did_work {
                self.shared.stop.wait(Duration::from_millis(10));
                continue;
            }
            self.shared.wake.wait(IDLE_POLL);
            self.shared.wake.clear();
        }
        // The worker owns target shutdown; a bounded close may have stopped
        // waiting for it while authority I/O was still in flight.
        close_target_once(&self.shared, &self.target);
        *self.shared.alive.lock().expect("worker alive") = false;
        self.shared.exited.notify_all();
    }

    fn record_failure(&self, name: String) {
        {
            let mut observed = self.shared.observed.lock().expect("projection status");
            observed.metrics.failures += 1;
            observed.last_error = name;
        }
        eprintln!("[turn-search] projection worker failed");
        self.shared.stop.wait(RETRY);
    }
}
