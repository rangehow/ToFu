//! `turn_search_projection.py` storage half: the projection write helpers
//! (`_apply_turn_to_session` …) and `LocalSQLiteTurnSearchTarget`, the
//! host-local disposable SQLite with corruption recovery and demand readers.

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};

use super::chunks::{chunks, install_fts, CHUNK_SCHEMA};
use super::text::{bounded_turn_search_text, build_search_text};
use crate::codec;
use crate::error::{Failure, OpResult, StorageError};
use crate::ops::conversations::support::{py_str, str_or};
use crate::params;
use crate::session::{Row, Session, SqlValue};

pub const LOCAL_FORMAT_VERSION: i64 = 2;
pub const LOCAL_DATABASE_NAME: &str = "turn-search-v1.sqlite3";

const LOCAL_SCHEMA: [&str; 5] = [
    "CREATE TABLE IF NOT EXISTS projection_meta (\
     meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS storage_search_conversations (\
     id TEXT NOT NULL,user_id INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,\
     generation_token TEXT NOT NULL DEFAULT '',title TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,id))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_conversations_order \
     ON storage_search_conversations(user_id,updated_at_ms,id)",
    "CREATE TABLE IF NOT EXISTS storage_search_turns (\
     conversation_id TEXT NOT NULL,user_id INTEGER NOT NULL,turn_id TEXT NOT NULL,\
     lane_id TEXT NOT NULL,ordinal INTEGER NOT NULL,search_text TEXT NOT NULL,\
     projection_revision INTEGER NOT NULL,updated_at INTEGER NOT NULL,\
     generation_token TEXT NOT NULL DEFAULT '',\
     PRIMARY KEY(user_id,conversation_id,turn_id))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_turns_owner_order \
     ON storage_search_turns(user_id,conversation_id,lane_id,ordinal)",
];

fn int(value: &SqlValue) -> OpResult<i64> {
    value.as_int().ok_or_else(|| Failure::Internal("TypeError: int() argument".into()))
}

fn int_or_zero(value: &SqlValue) -> OpResult<i64> {
    if value.truthy() {
        int(value)
    } else {
        Ok(0)
    }
}

/// `_turn_search_text(actor, projection)`.
fn turn_search_text(actor: &str, projection: &Map<String, Value>) -> OpResult<String> {
    let mut message = projection.clone();
    let role = if matches!(actor, "human" | "critic" | "virtual_user") { "user" } else { "assistant" };
    message.insert("role".into(), Value::String(role.into()));
    match build_search_text(&message) {
        Ok(text) => Ok(bounded_turn_search_text(text)),
        Err(()) => Err(StorageError::new("database_internal", "Turn search projection failed").into()),
    }
}

/// `_load(snapshot['projection_json']) or {}`.
fn loaded_projection(snapshot: &Row) -> OpResult<Value> {
    let projection = codec::load(snapshot.get("projection_json"))?;
    Ok(if crate::json::truthy(&projection) { projection } else { Value::Object(Map::new()) })
}

fn upsert_header(
    session: &Session<'_>,
    id: &SqlValue,
    user_id: &SqlValue,
    updated_at_ms: &SqlValue,
    title: &SqlValue,
    generation_token: &str,
) -> OpResult<()> {
    session.execute(
        "INSERT INTO storage_search_conversations(\
         id,user_id,updated_at_ms,generation_token,title) VALUES (?,?,?,?,?) \
         ON CONFLICT(user_id,id) DO UPDATE SET \
         updated_at_ms=excluded.updated_at_ms,title=excluded.title,\
         generation_token=excluded.generation_token",
        &params![py_str(id), int(user_id)?, int_or_zero(updated_at_ms)?, generation_token, str_or(title, "")],
    )?;
    Ok(())
}

/// `_apply_turn_to_session(session, identity, snapshot, generation_token)`.
pub fn apply_turn_to_session(
    session: &Session<'_>,
    user_id: i64,
    turn_id: &str,
    snapshot: Option<&Row>,
    generation_token: &str,
) -> OpResult<()> {
    match snapshot {
        Some(snapshot) => session.execute(
            "DELETE FROM storage_search_chunks WHERE user_id=? AND conversation_id=? AND turn_id=?",
            &params![user_id, py_str(snapshot.require("conversation_id")?), turn_id],
        )?,
        None => session
            .execute("DELETE FROM storage_search_chunks WHERE user_id=? AND turn_id=?", &params![user_id, turn_id])?,
    };
    let snapshot = match snapshot {
        Some(snapshot)
            if str_or(snapshot.get("lane_id"), "main") == "main"
                && !matches!(str_or(snapshot.get("status"), "").as_str(), "pending" | "running") =>
        {
            snapshot
        }
        _ => {
            session.execute(
                "DELETE FROM storage_search_turns WHERE user_id=? AND turn_id=?",
                &params![user_id, turn_id],
            )?;
            return Ok(());
        }
    };
    let conversation_id = snapshot.require("conversation_id")?;
    let zero = SqlValue::Integer(0);
    let updated_at_ms = snapshot.get("conversation_updated_at_ms");
    let updated_at_ms = if updated_at_ms.truthy() { updated_at_ms } else { &zero };
    let empty = SqlValue::Text(String::new());
    let title = snapshot.get("conversation_title");
    let title = if title.truthy() { title } else { &empty };
    upsert_header(session, conversation_id, snapshot.require("user_id")?, updated_at_ms, title, generation_token)?;
    let actor = str_or(snapshot.get("actor"), "");
    let projection = loaded_projection(snapshot)?;
    let empty_map = Map::new();
    let search_text = turn_search_text(&actor, projection.as_object().unwrap_or(&empty_map))?;
    session.execute(
        "INSERT INTO storage_search_turns(\
         conversation_id,user_id,turn_id,lane_id,ordinal,search_text,\
         projection_revision,updated_at,generation_token) \
         VALUES (?,?,?,?,?,?,?,?,?) \
         ON CONFLICT(user_id,conversation_id,turn_id) DO UPDATE SET \
         lane_id=excluded.lane_id,ordinal=excluded.ordinal,\
         search_text=excluded.search_text,\
         projection_revision=excluded.projection_revision,\
         updated_at=excluded.updated_at,\
         generation_token=excluded.generation_token",
        &params![
            py_str(conversation_id),
            int(snapshot.require("user_id")?)?,
            py_str(snapshot.require("turn_id")?),
            str_or(snapshot.get("lane_id"), "main"),
            int_or_zero(snapshot.get("ordinal"))?,
            search_text,
            int_or_zero(snapshot.get("projection_revision"))?,
            int_or_zero(snapshot.get("updated_at"))?,
            generation_token
        ],
    )?;
    let ordinal = int_or_zero(snapshot.get("ordinal"))?;
    for chunk in chunks(&projection, &actor) {
        session.execute(
            "INSERT INTO storage_search_chunks(user_id,conversation_id,turn_id,chunk_index,\
             ordinal,role,match_field,search_text,folded_text,generation_token) VALUES (?,?,?,?,?,?,?,?,?,?)",
            &params![
                user_id,
                py_str(conversation_id),
                turn_id,
                chunk.index,
                ordinal,
                chunk.role,
                chunk.field,
                chunk.text,
                chunk.folded,
                generation_token
            ],
        )?;
    }
    Ok(())
}

/// `_begin_conversation_in_session`.
pub fn begin_conversation_in_session(
    session: &Session<'_>,
    user_id: i64,
    conversation_id: &str,
    header: Option<&Row>,
    generation_token: &str,
) -> OpResult<()> {
    let Some(header) = header else {
        session.execute(
            "DELETE FROM storage_search_chunks WHERE user_id=? AND conversation_id=?",
            &params![user_id, conversation_id],
        )?;
        session.execute(
            "DELETE FROM storage_search_turns WHERE user_id=? AND conversation_id=?",
            &params![user_id, conversation_id],
        )?;
        session.execute(
            "DELETE FROM storage_search_conversations WHERE user_id=? AND id=?",
            &params![user_id, conversation_id],
        )?;
        return Ok(());
    };
    upsert_header(
        session,
        header.require("id")?,
        header.require("user_id")?,
        header.get("updated_at_ms"),
        header.get("title"),
        generation_token,
    )
}

/// `_apply_page_in_session`.
pub fn apply_page_in_session(session: &Session<'_>, rows: &[Row], generation_token: &str) -> OpResult<()> {
    for row in rows {
        let user_id = int(row.require("user_id")?)?;
        let turn_id = py_str(row.require("turn_id")?);
        apply_turn_to_session(session, user_id, &turn_id, Some(row), generation_token)?;
    }
    Ok(())
}

/// `_finalize_conversation_in_session`.
pub fn finalize_conversation_in_session(
    session: &Session<'_>,
    user_id: i64,
    conversation_id: &str,
    generation_token: &str,
) -> OpResult<()> {
    session.execute(
        "DELETE FROM storage_search_chunks WHERE user_id=? AND conversation_id=? AND generation_token<>?",
        &params![user_id, conversation_id, generation_token],
    )?;
    session.execute(
        "DELETE FROM storage_search_turns WHERE user_id=? AND conversation_id=? AND generation_token<>?",
        &params![user_id, conversation_id, generation_token],
    )?;
    Ok(())
}

/// `type(exc).__name__` for a failure raised as a Python exception.
pub fn failure_type_name(failure: &Failure) -> String {
    match failure {
        Failure::Storage(_) => "StorageError".into(),
        Failure::Sqlite(rusqlite::Error::SqliteFailure(error, _)) => match error.extended_code & 0xFF {
            19 => "IntegrityError",
            11 | 26 => "DatabaseError",
            18 | 20 => "DataError",
            7 => "MemoryError",
            21 => "ProgrammingError",
            _ => "OperationalError",
        }
        .into(),
        Failure::Sqlite(_) => "ProgrammingError".into(),
        Failure::Internal(reason) => {
            if reason.starts_with("ValueError:") {
                "ValueError".into()
            } else if reason.starts_with("orjson.JSONDecodeError") {
                "JSONDecodeError".into()
            } else {
                match reason.split_once(':') {
                    Some((name, _)) if !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric()) => {
                        name.into()
                    }
                    _ => "Exception".into(),
                }
            }
        }
    }
}

/// `type(exc).__name__` of a filesystem error.
pub fn io_type_name(error: &std::io::Error) -> String {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::AlreadyExists => "FileExistsError",
        _ => "OSError",
    }
    .into()
}

/// A start failure carries the Python exception class name.
pub struct StartError(pub String);

impl From<Failure> for StartError {
    fn from(failure: Failure) -> Self {
        StartError(failure_type_name(&failure))
    }
}

impl From<rusqlite::Error> for StartError {
    fn from(error: rusqlite::Error) -> Self {
        StartError(failure_type_name(&Failure::Sqlite(error)))
    }
}

impl From<std::io::Error> for StartError {
    fn from(error: std::io::Error) -> Self {
        StartError(io_type_name(&error))
    }
}

fn connect(path: &Path, query_only: bool) -> rusqlite::Result<Connection> {
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let connection = Connection::open_with_flags(path, flags)?;
    connection.busy_timeout(Duration::from_millis(2000))?;
    connection.execute_batch("PRAGMA busy_timeout=2000")?;
    if query_only {
        connection.execute_batch("PRAGMA query_only=ON")?;
        // `prepare_search_session`: the deterministic Unicode lower.
        connection.create_scalar_function(
            "tofu_search_lower",
            1,
            FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
            |context| {
                let value = SqlValue::from(context.get_raw(0));
                Ok(super::text::lower(&value.text_or_empty()))
            },
        )?;
    } else {
        connection.query_row("PRAGMA journal_mode=WAL", [], |_| Ok(()))?;
        // This store is disposable and replayable. NORMAL prevents a
        // host-cache fsync from entering the authority latency budget.
        connection.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA wal_autocheckpoint=1000;")?;
    }
    connection.set_prepared_statement_cache_capacity(64);
    Ok(connection)
}

#[derive(Default)]
struct PoolState {
    idle: Vec<Connection>,
    active: i64,
    peak: i64,
    rotations: i64,
    connections_created: i64,
    closed: bool,
}

/// `DemandSQLiteReadPool`: demand-created query-only readers, joined close.
struct ReaderPool {
    state: Mutex<PoolState>,
    released: Condvar,
    idle_reuse_hint: usize,
}

impl ReaderPool {
    fn acquire(&self, path: &Path, deadline: Instant) -> Result<Connection, StorageError> {
        let idle = {
            let mut state = self.state.lock().expect("reader pool");
            if state.closed {
                return Err(StorageError::new("database_unavailable", "Storage connection is not available"));
            }
            if Instant::now() >= deadline {
                return Err(StorageError::retryable("database_timeout", "Storage connection deadline expired", 25));
            }
            state.active += 1;
            state.peak = state.peak.max(state.active);
            state.idle.pop()
        };
        let connection = match idle {
            Some(connection) => Ok(connection),
            None => match connect(path, true) {
                Ok(connection) => {
                    self.state.lock().expect("reader pool").connections_created += 1;
                    Ok(connection)
                }
                Err(_) => Err(query_error(deadline)),
            },
        };
        let connection = connection.and_then(|connection| {
            if Instant::now() >= deadline {
                Err(StorageError::retryable("database_timeout", "Storage connection deadline expired", 25))
            } else {
                Ok(connection)
            }
        });
        if connection.is_err() {
            let mut state = self.state.lock().expect("reader pool");
            state.active -= 1;
            self.released.notify_all();
        }
        connection
    }

    fn release(&self, connection: Connection, retire: bool) {
        let mut state = self.state.lock().expect("reader pool");
        if retire || state.closed {
            if retire {
                state.rotations += 1;
            }
            drop(connection);
        } else {
            state.idle.push(connection);
        }
        state.active -= 1;
        self.released.notify_all();
    }

    fn close(&self) {
        let mut state = self.state.lock().expect("reader pool");
        state.closed = true;
        while state.active > 0 {
            state = self.released.wait(state).expect("reader pool");
        }
        state.idle.clear();
    }

    fn snapshot(&self) -> Value {
        let state = self.state.lock().expect("reader pool");
        json!({
            "idle": state.idle.len(), "active": state.active, "peak": state.peak,
            "idle_reuse_hint": self.idle_reuse_hint, "capacity": 0,
            "rotations": state.rotations, "connections_created": state.connections_created,
        })
    }
}

/// `LocalSQLiteTurnSearchTarget._query_error`.
fn query_error(deadline: Instant) -> StorageError {
    let expired = Instant::now() >= deadline;
    let mut error = StorageError::new(
        if expired { "database_timeout" } else { "database_unavailable" },
        "Conversation search projection query failed",
    );
    error.retryable = expired;
    error.retry_after_ms = Some(25);
    error
}

fn unavailable() -> Failure {
    StorageError::new("database_unavailable", "Search projection is unavailable").into()
}

pub struct LocalTarget {
    directory: PathBuf,
    path: PathBuf,
    writer: Mutex<Option<Connection>>,
    writer_open: AtomicBool,
    readers: ReaderPool,
    state: Mutex<&'static str>,
    last_error: Mutex<String>,
    writes: AtomicU64,
    source_skipped: AtomicI64,
}

impl LocalTarget {
    pub fn new(directory: PathBuf, idle_reuse_hint: usize) -> Self {
        Self {
            path: directory.join(LOCAL_DATABASE_NAME),
            directory,
            writer: Mutex::new(None),
            writer_open: AtomicBool::new(false),
            readers: ReaderPool {
                state: Mutex::new(PoolState::default()),
                released: Condvar::new(),
                idle_reuse_hint: idle_reuse_hint.max(1),
            },
            state: Mutex::new("starting"),
            last_error: Mutex::new(String::new()),
            writes: AtomicU64::new(0),
            source_skipped: AtomicI64::new(0),
        }
    }

    fn state(&self) -> &'static str {
        *self.state.lock().expect("projection state")
    }

    fn set_state(&self, state: &'static str) {
        *self.state.lock().expect("projection state") = state;
    }

    fn prepare_directory(&self) -> Result<(), StartError> {
        let mut builder = std::fs::DirBuilder::new();
        builder.recursive(true);
        std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
        builder.create(&self.directory)?;
        let link = std::fs::symlink_metadata(&self.directory)?;
        if link.file_type().is_symlink() || !self.directory.is_dir() {
            return Err(StartError("StorageError".into()));
        }
        let info = std::fs::metadata(&self.directory)?;
        if info.uid() != unsafe { libc::getuid() } {
            return Err(StartError("StorageError".into()));
        }
        if info.mode() & 0o077 != 0 {
            std::fs::set_permissions(&self.directory, std::fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }

    fn discard_corrupt(&self) {
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.path.display()));
        }
        *self.last_error.lock().expect("projection error") = "corrupt_projection_rebuilt".into();
        eprintln!("[turn-search] discarded corrupt disposable projection at {}", self.path.display());
    }

    fn open_writer(&self) -> rusqlite::Result<Connection> {
        let checked = connect(&self.path, false).and_then(|connection| {
            let result: String = connection.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
            if result != "ok" {
                return Err(rusqlite::Error::SqliteFailure(
                    rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CORRUPT),
                    Some("projection quick_check failed".into()),
                ));
            }
            Ok(connection)
        });
        match checked {
            Ok(connection) => Ok(connection),
            Err(_) => {
                self.discard_corrupt();
                connect(&self.path, false)
            }
        }
    }

    /// `start()`.
    pub fn start(&self) -> Result<(), StartError> {
        self.prepare_directory()?;
        let writer = self.open_writer()?;
        writer.execute_batch("BEGIN IMMEDIATE")?;
        let outcome = (|| -> OpResult<i64> {
            let session = Session::new(&writer, "", None, true);
            for statement in LOCAL_SCHEMA.iter().chain(CHUNK_SCHEMA.iter()) {
                session.execute(statement, &[])?;
            }
            let columns = session.fetch_all("PRAGMA table_info(storage_search_conversations)", &[])?;
            if !columns.iter().any(|row| row.get("name").as_str() == Some("title")) {
                session.execute(
                    "ALTER TABLE storage_search_conversations ADD COLUMN title TEXT NOT NULL DEFAULT ''",
                    &[],
                )?;
            }
            install_fts(&session)?;
            let version = session
                .fetch_one("SELECT meta_value FROM projection_meta WHERE meta_key=?", &params!["format_version"])?;
            if let Some(version) = version {
                let parsed = version
                    .get("meta_value")
                    .text_or_empty()
                    .trim()
                    .parse::<i64>()
                    .map_err(|_| Failure::Internal("ValueError: invalid literal for int()".into()))?;
                if parsed != LOCAL_FORMAT_VERSION {
                    session.execute("DELETE FROM storage_search_chunks", &[])?;
                    session.execute("DELETE FROM storage_search_turns", &[])?;
                    session.execute("DELETE FROM storage_search_conversations", &[])?;
                    session.execute("DELETE FROM projection_meta", &[])?;
                }
            }
            session.execute(
                "INSERT INTO projection_meta(meta_key,meta_value) VALUES (?,?) \
                 ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                &params!["format_version", LOCAL_FORMAT_VERSION.to_string()],
            )?;
            let skipped =
                session.fetch_one("SELECT meta_value FROM projection_meta WHERE meta_key='source_skipped'", &[])?;
            Ok(match skipped {
                Some(row) => row
                    .get("meta_value")
                    .text_or_empty()
                    .trim()
                    .parse::<i64>()
                    .map_err(|_| Failure::Internal("ValueError: invalid literal for int()".into()))?,
                None => 0,
            })
        })()
        .and_then(|skipped| {
            writer.execute_batch("COMMIT")?;
            Ok(skipped)
        });
        match outcome {
            Ok(skipped) => {
                self.source_skipped.store(skipped, Ordering::SeqCst);
                *self.writer.lock().expect("projection writer") = Some(writer);
                self.writer_open.store(true, Ordering::SeqCst);
                self.set_state("warming");
                Ok(())
            }
            Err(failure) => {
                let _ = writer.execute_batch("ROLLBACK");
                Err(failure.into())
            }
        }
    }

    fn database_bytes(&self) -> u64 {
        ["", "-wal", "-shm"]
            .iter()
            .filter_map(|suffix| std::fs::metadata(format!("{}{suffix}", self.path.display())).ok())
            .map(|info| info.len())
            .sum()
    }

    fn write(&self, callback: impl FnOnce(&Session<'_>) -> OpResult<()>) -> OpResult<()> {
        let guard = self.writer.lock().expect("projection writer");
        let Some(writer) = guard.as_ref().filter(|_| self.state() != "closed") else {
            return Err(unavailable());
        };
        writer.execute_batch("BEGIN IMMEDIATE")?;
        let result = callback(&Session::new(writer, "", None, true)).and_then(|()| {
            writer.execute_batch("COMMIT")?;
            Ok(())
        });
        match result {
            Ok(()) => {
                self.writes.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
            Err(failure) => {
                if !writer.is_autocommit() {
                    let _ = writer.execute_batch("ROLLBACK");
                }
                Err(failure)
            }
        }
    }

    /// `query(operation, deadline_at)` on one demand reader.
    pub fn query<T>(&self, operation: impl FnOnce(&Session<'_>) -> OpResult<T>, deadline: Instant) -> OpResult<T> {
        if !self.writer_open.load(Ordering::SeqCst) || self.state() == "closed" {
            return Err(unavailable());
        }
        let connection = self.readers.acquire(&self.path, deadline)?;
        connection.progress_handler(1000, Some(move || Instant::now() >= deadline));
        let result = (|| -> OpResult<T> {
            connection.execute_batch("BEGIN")?;
            operation(&Session::new(&connection, "", None, false))
        })();
        let poisoned = connection.execute_batch("ROLLBACK").is_err() && !connection.is_autocommit();
        self.readers.release(connection, poisoned);
        match result {
            Err(Failure::Sqlite(_)) => Err(query_error(deadline).into()),
            other => other,
        }
    }

    pub fn apply_turn(&self, user_id: i64, turn_id: &str, snapshot: Option<&Row>, token: &str) -> OpResult<()> {
        self.write(|session| apply_turn_to_session(session, user_id, turn_id, snapshot, token))
    }

    pub fn begin_conversation(
        &self,
        user_id: i64,
        conversation_id: &str,
        header: Option<&Row>,
        token: &str,
    ) -> OpResult<()> {
        self.write(|session| begin_conversation_in_session(session, user_id, conversation_id, header, token))
    }

    pub fn apply_conversation_page(&self, rows: &[Row], token: &str) -> OpResult<()> {
        self.write(|session| apply_page_in_session(session, rows, token))
    }

    pub fn finalize_conversation(&self, user_id: i64, conversation_id: &str, token: &str) -> OpResult<()> {
        self.write(|session| finalize_conversation_in_session(session, user_id, conversation_id, token))
    }

    pub fn reset(&self) -> OpResult<()> {
        self.write(|session| {
            session.execute("DELETE FROM storage_search_chunks", &[])?;
            session.execute("DELETE FROM storage_search_turns", &[])?;
            session.execute("DELETE FROM storage_search_conversations", &[])?;
            session.execute(
                "DELETE FROM projection_meta WHERE meta_key IN (?,?)",
                &params!["backfill_cursor", "backfill_complete"],
            )?;
            session.execute("DELETE FROM projection_meta WHERE meta_key='source_skipped'", &[])?;
            Ok(())
        })?;
        self.source_skipped.store(0, Ordering::SeqCst);
        self.set_state("warming");
        Ok(())
    }

    pub fn backfill_state(&self) -> OpResult<(String, bool)> {
        let guard = self.writer.lock().expect("projection writer");
        let Some(writer) = guard.as_ref() else { return Ok((String::new(), false)) };
        let session = Session::new(writer, "", None, true);
        let cursor = session
            .fetch_one("SELECT meta_value FROM projection_meta WHERE meta_key=?", &params!["backfill_cursor"])?;
        let complete = session
            .fetch_one("SELECT meta_value FROM projection_meta WHERE meta_key=?", &params!["backfill_complete"])?;
        Ok((
            cursor.map(|row| py_str(row.get("meta_value"))).unwrap_or_default(),
            complete.is_some_and(|row| row.get("meta_value").as_str() == Some("1")),
        ))
    }

    pub fn set_backfill_state(&self, cursor: &str, complete: bool) -> OpResult<()> {
        self.write(|session| {
            for (key, value) in [("backfill_cursor", cursor), ("backfill_complete", if complete { "1" } else { "0" })] {
                session.execute(
                    "INSERT INTO projection_meta(meta_key,meta_value) \
                     VALUES (?,?) ON CONFLICT(meta_key) DO UPDATE SET \
                     meta_value=excluded.meta_value",
                    &params![key, value],
                )?;
            }
            Ok(())
        })?;
        if complete {
            self.set_state("ready");
        }
        Ok(())
    }

    pub fn record_skips(&self, count: i64) -> OpResult<()> {
        self.write(|session| {
            session.execute(
                "INSERT INTO projection_meta(meta_key,meta_value) VALUES ('source_skipped',?) \
                 ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                &params![count.to_string()],
            )?;
            Ok(())
        })?;
        self.source_skipped.store(count, Ordering::SeqCst);
        Ok(())
    }

    pub fn status(&self) -> OpResult<Map<String, Value>> {
        let (cursor, complete) = self.backfill_state()?;
        let status = json!({
            "state": self.state(),
            "backend": "local-sqlite",
            "database_bytes": self.database_bytes(),
            "max_bytes": 0,
            "backfill_complete": complete,
            "backfill_cursor": cursor,
            "writes": self.writes.load(Ordering::SeqCst),
            "source_skipped": self.source_skipped.load(Ordering::SeqCst),
            "last_error": self.last_error.lock().expect("projection error").clone(),
            "read_connections": self.readers.snapshot(),
        });
        Ok(status.as_object().cloned().unwrap_or_default())
    }

    pub fn close(&self) {
        self.set_state("closed");
        self.readers.close();
        self.writer_open.store(false, Ordering::SeqCst);
        self.writer.lock().expect("projection writer").take();
    }
}
