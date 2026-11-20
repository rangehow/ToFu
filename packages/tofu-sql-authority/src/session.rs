//! The backend-neutral operation `Session` (`adapters/base.py::Session`)
//! over one rusqlite connection, with Python `sqlite3` value semantics.
//!
//! Rows are ordered column maps like `dict(sqlite3.Row)`; parameters bind
//! like Python's `sqlite3` adapters (`str`→TEXT, `bytes`→BLOB, `bool`→INTEGER).

use std::cell::RefCell;
use std::collections::BTreeSet;

use rusqlite::types::{ToSqlOutput, Value as SqliteValue, ValueRef};
use rusqlite::{Connection, ToSql};
use serde_json::Value;

use crate::control::WorkerLeaseClock;
use crate::error::{fail, internal, OpResult, StorageError};
use crate::turn_projection_cache::TurnProjectionCache;

#[derive(Clone, Debug, PartialEq)]
pub enum SqlValue {
    Null,
    Integer(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}

impl SqlValue {
    pub fn is_null(&self) -> bool {
        matches!(self, SqlValue::Null)
    }

    /// Python `int(value)` for an INTEGER/REAL/TEXT column.
    pub fn as_int(&self) -> Option<i64> {
        match self {
            SqlValue::Integer(value) => Some(*value),
            SqlValue::Real(value) => Some(value.trunc() as i64),
            SqlValue::Text(text) => text.trim().parse().ok(),
            _ => None,
        }
    }

    pub fn as_f64(&self) -> Option<f64> {
        match self {
            SqlValue::Integer(value) => Some(*value as f64),
            SqlValue::Real(value) => Some(*value),
            SqlValue::Text(text) => text.trim().parse().ok(),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            SqlValue::Text(text) => Some(text),
            _ => None,
        }
    }

    /// Python `str(value or '')` for TEXT-ish columns.
    pub fn text_or_empty(&self) -> String {
        match self {
            SqlValue::Null => String::new(),
            SqlValue::Integer(value) => {
                if *value == 0 {
                    String::new()
                } else {
                    value.to_string()
                }
            }
            SqlValue::Real(value) => {
                if *value == 0.0 {
                    String::new()
                } else {
                    crate::json::python_float_repr(*value)
                }
            }
            SqlValue::Text(text) => text.clone(),
            SqlValue::Blob(bytes) => {
                if bytes.is_empty() {
                    String::new()
                } else {
                    format!("b{:?}", String::from_utf8_lossy(bytes))
                }
            }
        }
    }

    pub fn bytes(&self) -> Option<&[u8]> {
        match self {
            SqlValue::Blob(bytes) => Some(bytes),
            SqlValue::Text(text) => Some(text.as_bytes()),
            _ => None,
        }
    }

    /// The JSON value Python would return for this column inside a result
    /// dict (`bytes` is not JSON: callers decode or encode it explicitly).
    pub fn to_json(&self) -> OpResult<Value> {
        Ok(match self {
            SqlValue::Null => Value::Null,
            SqlValue::Integer(value) => Value::from(*value),
            SqlValue::Real(value) => crate::json::float_value(*value),
            SqlValue::Text(text) => Value::String(text.clone()),
            SqlValue::Blob(_) => return internal("bytes are not JSON serializable"),
        })
    }

    /// Python truthiness of the column value.
    pub fn truthy(&self) -> bool {
        match self {
            SqlValue::Null => false,
            SqlValue::Integer(value) => *value != 0,
            SqlValue::Real(value) => *value != 0.0,
            SqlValue::Text(text) => !text.is_empty(),
            SqlValue::Blob(bytes) => !bytes.is_empty(),
        }
    }
}

impl ToSql for SqlValue {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(match self {
            SqlValue::Null => ToSqlOutput::Owned(SqliteValue::Null),
            SqlValue::Integer(value) => ToSqlOutput::Owned(SqliteValue::Integer(*value)),
            SqlValue::Real(value) => ToSqlOutput::Owned(SqliteValue::Real(*value)),
            SqlValue::Text(text) => ToSqlOutput::Borrowed(ValueRef::Text(text.as_bytes())),
            SqlValue::Blob(bytes) => ToSqlOutput::Borrowed(ValueRef::Blob(bytes)),
        })
    }
}

impl From<ValueRef<'_>> for SqlValue {
    fn from(value: ValueRef<'_>) -> Self {
        match value {
            ValueRef::Null => SqlValue::Null,
            ValueRef::Integer(value) => SqlValue::Integer(value),
            ValueRef::Real(value) => SqlValue::Real(value),
            ValueRef::Text(text) => SqlValue::Text(String::from_utf8_lossy(text).into_owned()),
            ValueRef::Blob(bytes) => SqlValue::Blob(bytes.to_vec()),
        }
    }
}

macro_rules! sql_from {
    ($($ty:ty => $body:expr),* $(,)?) => {
        $(impl From<$ty> for SqlValue {
            fn from(value: $ty) -> Self {
                let convert: fn($ty) -> SqlValue = $body;
                convert(value)
            }
        })*
    };
}

sql_from! {
    i64 => SqlValue::Integer,
    i32 => |v| SqlValue::Integer(v as i64),
    u32 => |v| SqlValue::Integer(v as i64),
    usize => |v| SqlValue::Integer(v as i64),
    bool => |v| SqlValue::Integer(v as i64),
    f64 => SqlValue::Real,
    String => SqlValue::Text,
    &str => |v| SqlValue::Text(v.to_owned()),
    &String => |v| SqlValue::Text(v.clone()),
    Vec<u8> => SqlValue::Blob,
    &[u8] => |v| SqlValue::Blob(v.to_vec()),
}

impl<T: Into<SqlValue>> From<Option<T>> for SqlValue {
    fn from(value: Option<T>) -> Self {
        value.map(Into::into).unwrap_or(SqlValue::Null)
    }
}

impl From<&SqlValue> for SqlValue {
    fn from(value: &SqlValue) -> Self {
        value.clone()
    }
}

/// Bind a decoded JSON scalar the way Python's sqlite3 adapters would.
pub fn json_param(value: &Value) -> OpResult<SqlValue> {
    Ok(match value {
        Value::Null => SqlValue::Null,
        Value::Bool(flag) => SqlValue::Integer(*flag as i64),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                SqlValue::Integer(integer)
            } else if number.as_u64().is_some() {
                return internal("Python int too large to convert to SQLite INTEGER");
            } else {
                SqlValue::Real(number.as_f64().unwrap_or(0.0))
            }
        }
        Value::String(text) => SqlValue::Text(text.clone()),
        _ => return internal("Error binding parameter: type is not supported"),
    })
}

/// `params![a, b, ...]` builds a `[SqlValue; N]` (borrow it as `&params![..]`;
/// `.to_vec()` for `execute_many` rows).
#[macro_export]
macro_rules! params {
    () => { [$crate::session::SqlValue::Null; 0] };
    ($($value:expr),+ $(,)?) => { [$($crate::session::SqlValue::from($value)),+] };
}

/// One result row: ordered `(column, value)` pairs like `dict(sqlite3.Row)`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Row {
    columns: Vec<(String, SqlValue)>,
}

static NULL: SqlValue = SqlValue::Null;

impl Row {
    pub fn get(&self, column: &str) -> &SqlValue {
        self.columns.iter().find(|(name, _)| name == column).map(|(_, value)| value).unwrap_or(&NULL)
    }

    pub fn contains(&self, column: &str) -> bool {
        self.columns.iter().any(|(name, _)| name == column)
    }

    /// Python `row[column]`: a missing column is a KeyError.
    pub fn require(&self, column: &str) -> OpResult<&SqlValue> {
        match self.columns.iter().find(|(name, _)| name == column) {
            Some((_, value)) => Ok(value),
            None => internal(format!("KeyError: {column}")),
        }
    }

    pub fn int(&self, column: &str) -> OpResult<i64> {
        match self.require(column)?.as_int() {
            Some(value) => Ok(value),
            None => internal(format!("int() of {column}")),
        }
    }

    pub fn opt_int(&self, column: &str) -> Option<i64> {
        self.get(column).as_int()
    }

    pub fn text(&self, column: &str) -> String {
        self.get(column).text_or_empty()
    }

    pub fn first(&self) -> &SqlValue {
        self.columns.first().map(|(_, value)| value).unwrap_or(&NULL)
    }

    pub fn columns(&self) -> impl Iterator<Item = (&str, &SqlValue)> {
        self.columns.iter().map(|(name, value)| (name.as_str(), value))
    }

    pub fn set(&mut self, column: &str, value: SqlValue) {
        if let Some(slot) = self.columns.iter_mut().find(|(name, _)| name == column) {
            slot.1 = value;
        } else {
            self.columns.push((column.to_owned(), value));
        }
    }

    /// `dict(row)` as a JSON object; BLOB columns are an error like orjson's.
    pub fn to_json(&self) -> OpResult<Value> {
        let mut map = serde_json::Map::new();
        for (name, value) in &self.columns {
            map.insert(name.clone(), value.to_json()?);
        }
        Ok(Value::Object(map))
    }
}

/// Worker-watch publication hints collected inside one transaction
/// (`worker_control.py::WorkerCommitHints`), published after COMMIT.
#[derive(Default, Debug)]
pub struct WorkerHints {
    pub queue_changed: bool,
    pub cancelled_tasks: BTreeSet<String>,
    pub conversation_changes: BTreeSet<(i64, String)>,
}

/// `WorkerLeaseAuthoritySnapshot(epoch, lease_now_ms, utc_now_ms)`.
#[derive(Clone, Debug)]
pub struct LeaseSnapshot {
    pub epoch: String,
    pub lease_now_ms: i64,
    pub utc_now_ms: i64,
}

pub struct Session<'c> {
    connection: &'c Connection,
    pub backend: &'static str,
    pub event_read_epoch: String,
    pub worker_hints: RefCell<WorkerHints>,
    lease_clock: Option<&'c WorkerLeaseClock>,
    /// True inside a writer transaction (`BEGIN IMMEDIATE`).
    pub writable: bool,
    /// `session.turn_projection_cache`: the backend's reconstructible cache
    /// (absent during bootstrap, like Python's cache-less startup session).
    pub turn_projection_cache: Option<&'c TurnProjectionCache>,
    logical_mutations: RefCell<Option<Vec<Value>>>,
}

impl<'c> Session<'c> {
    pub fn new(
        connection: &'c Connection,
        event_read_epoch: &str,
        lease_clock: Option<&'c WorkerLeaseClock>,
        writable: bool,
    ) -> Self {
        Self {
            connection,
            backend: "sqlite",
            event_read_epoch: event_read_epoch.to_owned(),
            worker_hints: RefCell::new(WorkerHints::default()),
            lease_clock,
            writable,
            turn_projection_cache: None,
            logical_mutations: RefCell::new(None),
        }
    }

    pub fn with_turn_projection_cache(mut self, cache: &'c TurnProjectionCache) -> Self {
        self.turn_projection_cache = Some(cache);
        self
    }

    pub fn begin_logical_capture(&self) {
        *self.logical_mutations.borrow_mut() = Some(Vec::new());
    }

    pub fn finish_logical_capture(&self) -> Vec<Value> {
        self.logical_mutations.borrow_mut().take().unwrap_or_default()
    }

    fn validate_logical_statement(&self, sql: &str) -> OpResult<()> {
        if self.logical_mutations.borrow().is_none() {
            return Ok(());
        }
        if sql.trim().is_empty() {
            return fail("database_protocol_error", "Logical mutation statement must be non-empty text");
        }
        let normalized = sql.trim_start().to_lowercase();
        if !["delete ", "insert ", "replace ", "update ", "with "].iter().any(|p| normalized.starts_with(p)) {
            return fail("database_protocol_error", "Logical command attempted a non-replayable mutation statement");
        }
        Ok(())
    }

    fn record_logical_mutation(&self, sql: &str, params: &[SqlValue], rowcount: usize) -> OpResult<()> {
        if let Some(mutations) = self.logical_mutations.borrow_mut().as_mut() {
            let params: Vec<Value> = params
                .iter()
                .map(|value| match value {
                    SqlValue::Blob(bytes) => Ok(crate::codec::wire_bytes(bytes)),
                    value => value.to_json(),
                })
                .collect::<OpResult<_>>()?;
            mutations.push(serde_json::json!({"params": params, "rowcount": rowcount, "sql": sql}));
        }
        Ok(())
    }

    /// `session.worker_lease_authority()`.
    pub fn worker_lease_authority(&self) -> Result<LeaseSnapshot, StorageError> {
        let Some(clock) = self.lease_clock else {
            return Err(StorageError::new("database_internal", "Worker lease authority is unavailable"));
        };
        let (lease_now_ms, utc_now_ms) = clock.snapshot();
        Ok(LeaseSnapshot { epoch: self.event_read_epoch.clone(), lease_now_ms, utc_now_ms })
    }

    pub fn connection(&self) -> &Connection {
        self.connection
    }

    /// The single physical writer already serializes every key.
    pub fn lock_key(&self, _namespace: &str, _key: &str) {}

    pub fn index_exists(&self, index_name: &str) -> OpResult<bool> {
        Ok(self
            .fetch_one("SELECT 1 AS present FROM sqlite_schema WHERE type='index' AND name=?", &params![index_name])?
            .is_some())
    }

    /// `cursor.rowcount` clamped at zero.
    ///
    /// rusqlite's statement cache trims the SQL text, but SQLite records a
    /// CREATE statement's trailing whitespace in `sqlite_master` exactly as
    /// Python's sqlite3 passes it; untrimmed text is prepared uncached.
    pub fn execute(&self, sql: &str, params: &[SqlValue]) -> OpResult<usize> {
        self.validate_logical_statement(sql)?;
        if sql.len() != sql.trim_end().len() {
            let mut statement = self.connection.prepare(sql)?;
            let readonly = statement.readonly();
            let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
            while rows.next()?.is_some() {}
            drop(rows);
            let affected = if readonly { 0 } else { self.connection.changes() as usize };
            self.record_logical_mutation(sql, params, affected)?;
            return Ok(affected);
        }
        let mut statement = self.connection.prepare_cached(sql)?;
        let readonly = statement.readonly();
        let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
        while rows.next()?.is_some() {}
        drop(rows);
        let affected = if readonly { 0 } else { self.connection.changes() as usize };
        self.record_logical_mutation(sql, params, affected)?;
        Ok(affected)
    }

    /// `executemany` that must affect every input row.
    pub fn execute_many_exact(&self, sql: &str, rows: &[Vec<SqlValue>]) -> OpResult<usize> {
        if rows.is_empty() {
            return Ok(0);
        }
        let mut affected = 0usize;
        for params in rows {
            affected += self.execute(sql, params)?;
        }
        if affected != rows.len() {
            return fail("database_conflict", "Bulk mutation did not affect every expected row");
        }
        Ok(affected)
    }

    /// `executemany` with the summed rowcount.
    pub fn execute_many(&self, sql: &str, rows: &[Vec<SqlValue>]) -> OpResult<usize> {
        let mut affected = 0usize;
        for params in rows {
            affected += self.execute(sql, params)?;
        }
        Ok(affected)
    }

    pub fn fetch_all(&self, sql: &str, params: &[SqlValue]) -> OpResult<Vec<Row>> {
        let mut statement = self.connection.prepare_cached(sql)?;
        let names: Vec<String> = statement.column_names().iter().map(|name| (*name).to_owned()).collect();
        let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
        let mut result = Vec::new();
        while let Some(row) = rows.next()? {
            let mut columns = Vec::with_capacity(names.len());
            for (index, name) in names.iter().enumerate() {
                columns.push((name.clone(), SqlValue::from(row.get_ref(index)?)));
            }
            result.push(Row { columns });
        }
        Ok(result)
    }

    pub fn fetch_one(&self, sql: &str, params: &[SqlValue]) -> OpResult<Option<Row>> {
        let mut statement = self.connection.prepare_cached(sql)?;
        let names: Vec<String> = statement.column_names().iter().map(|name| (*name).to_owned()).collect();
        let mut rows = statement.query(rusqlite::params_from_iter(params.iter()))?;
        let Some(row) = rows.next()? else {
            return Ok(None);
        };
        let mut columns = Vec::with_capacity(names.len());
        for (index, name) in names.iter().enumerate() {
            columns.push((name.clone(), SqlValue::from(row.get_ref(index)?)));
        }
        Ok(Some(Row { columns }))
    }

    /// The enclosing `BEGIN IMMEDIATE` already serializes this decision.
    pub fn fetch_one_for_update(&self, sql: &str, params: &[SqlValue]) -> OpResult<Option<Row>> {
        self.fetch_one(sql, params)
    }

    pub fn fetch_one_for_update_skip_locked(&self, sql: &str, params: &[SqlValue]) -> OpResult<Option<Row>> {
        self.fetch_one(sql, params)
    }

    /// `session.worker_hints.queue_changed = True`.
    pub fn hint_queue_changed(&self) {
        self.worker_hints.borrow_mut().queue_changed = true;
    }

    /// `session.worker_hints.cancelled_tasks.add(task_id)`.
    pub fn hint_cancelled_task(&self, task_id: &str) {
        self.worker_hints.borrow_mut().cancelled_tasks.insert(task_id.to_owned());
    }

    /// `session.worker_hints.conversation_changes.add((owner, conversation))`.
    pub fn hint_conversation_change(&self, owner: i64, conversation_id: &str) {
        self.worker_hints.borrow_mut().conversation_changes.insert((owner, conversation_id.to_owned()));
    }
}

/// Guard used by handlers that must only run in a writer transaction.
pub fn require_writable(session: &Session<'_>) -> Result<(), StorageError> {
    if session.writable {
        Ok(())
    } else {
        Err(StorageError::new("database_internal", "attempt to write a readonly database"))
    }
}

#[cfg(test)]
mod logical_capture_tests {
    use super::*;

    #[test]
    fn semantic_mutations_capture_bound_bytes_and_stop_before_receipts() {
        let connection = Connection::open_in_memory().unwrap();
        let session = Session::new(&connection, "test", None, true);
        session.execute("CREATE TABLE items(k TEXT PRIMARY KEY, v BLOB)", &[]).unwrap();
        session.begin_logical_capture();
        let sql = "INSERT INTO items(k,v) VALUES (?,?)";
        session.execute(sql, &params!["k", vec![0u8, 255]]).unwrap();
        assert!(session.fetch_one("SELECT v FROM items WHERE k=?", &params!["k"]).unwrap().is_some());
        let recorded = session.finish_logical_capture();
        assert_eq!(recorded, vec![serde_json::json!({"sql":sql,"params":["k",{"$bytes":"AP8="}],"rowcount":1})]);
        session.execute("UPDATE items SET v=? WHERE k=?", &params![vec![1u8], "k"]).unwrap();
        assert!(session.finish_logical_capture().is_empty());
    }

    #[test]
    fn non_replayable_statement_refuses_before_execution() {
        let connection = Connection::open_in_memory().unwrap();
        let session = Session::new(&connection, "test", None, true);
        session.begin_logical_capture();
        let error = session.execute("CREATE TABLE refused(k TEXT)", &[]).unwrap_err();
        assert!(matches!(error, crate::error::Failure::Storage(e) if e.code == "database_protocol_error"));
        assert!(session.finish_logical_capture().is_empty());
        assert!(session.fetch_one("SELECT name FROM sqlite_master WHERE name='refused'", &[]).unwrap().is_none());
    }
}
