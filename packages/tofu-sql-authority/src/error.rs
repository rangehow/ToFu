//! The stable storage failure taxonomy (`lib/agent_core/storage_errors.py`).
//!
//! `StorageError::to_payload` is the exact wire shape of the Python
//! dataclass; a code outside the taxonomy folds to `database_internal`.

use std::fmt;

use serde_json::{json, Value};

/// `lib/agent_core/storage_errors.py::ERROR_CODES`.
pub const ERROR_CODES: &[&str] = &[
    "database_not_found",
    "database_invalid_payload",
    "database_unsupported",
    "database_busy",
    "database_unavailable",
    "database_timeout",
    "database_resource_exhausted",
    "database_conflict",
    "database_forbidden",
    "turn_projection_stale",
    "turn_in_progress",
    "turn_parent_invalid",
    "turn_lane_advanced",
    "turn_superseded_by_human",
    "turn_later_turns_unacknowledged",
    "turn_later_turns_changed",
    "conversation_authority_conflict",
    "database_integrity",
    "unknown_operation",
    "storage_payload_too_large",
    "database_protocol_error",
    "database_internal",
    "plugin_storage_incompatible",
    "tool_result_continuation_unavailable",
    "tool_result_invalid_cursor",
    "tool_result_invalid_selector",
    "tool_result_query_budget_exhausted",
    "tool_result_query_unavailable",
];

pub const COMMAND_ID_REUSED_MESSAGE: &str = "command_id was reused for a different request";

#[derive(Clone, Debug, PartialEq)]
pub struct StorageError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub retry_after_ms: Option<i64>,
    pub operation_id: String,
    pub request_not_dispatched: bool,
}

impl StorageError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        let code = if ERROR_CODES.contains(&code) { code } else { "database_internal" };
        Self {
            code: code.to_owned(),
            message: message.into(),
            retryable: false,
            retry_after_ms: None,
            operation_id: String::new(),
            request_not_dispatched: false,
        }
    }

    /// `StorageError(code, message, retryable, retry_after_ms)`.
    pub fn retryable(code: &str, message: impl Into<String>, retry_after_ms: i64) -> Self {
        let mut error = Self::new(code, message);
        error.retryable = true;
        error.retry_after_ms = Some(retry_after_ms.max(0));
        error
    }

    /// `StorageError(code, message, False, retry_after_ms)` and similar
    /// explicit non-retryable constructions.
    pub fn with_retryable(mut self, retryable: bool) -> Self {
        self.retryable = retryable;
        self
    }

    pub fn protocol(message: impl Into<String>) -> Self {
        Self::new("database_protocol_error", message)
    }

    pub fn to_payload(&self) -> Value {
        let mut payload = json!({
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
            "operation_id": self.operation_id,
        });
        if self.request_not_dispatched {
            payload["request_not_dispatched"] = Value::Bool(true);
        }
        payload
    }
}

impl fmt::Display for StorageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for StorageError {}

/// A failure raised inside a semantic operation. SQLite errors keep their
/// native form until the backend classifies them against the transaction
/// deadline (`_map_sqlite_error`), exactly as Python lets `sqlite3.Error`
/// propagate to the adapter.
#[derive(Debug)]
pub enum Failure {
    Storage(StorageError),
    Sqlite(rusqlite::Error),
    /// Any other Python exception class: the adapter classifies it as
    /// `database_internal` / "Storage operation failed".
    Internal(String),
}

impl From<StorageError> for Failure {
    fn from(error: StorageError) -> Self {
        Failure::Storage(error)
    }
}

impl From<rusqlite::Error> for Failure {
    fn from(error: rusqlite::Error) -> Self {
        Failure::Sqlite(error)
    }
}

pub type OpResult<T> = Result<T, Failure>;

/// Raise a classified storage failure from an operation body.
pub fn fail<T>(code: &str, message: impl Into<String>) -> OpResult<T> {
    Err(Failure::Storage(StorageError::new(code, message)))
}

pub fn internal<T>(reason: impl Into<String>) -> OpResult<T> {
    Err(Failure::Internal(reason.into()))
}

const SQLITE_INTERRUPT: i32 = 9;
const SQLITE_FULL: i32 = 13;
const SQLITE_BUSY_CODES: &[i32] = &[5, 6]; // BUSY, LOCKED
const SQLITE_UNAVAILABLE_CODES: &[i32] = &[10, 14]; // IOERR, CANTOPEN
const SQLITE_INTEGRITY_CODES: &[i32] = &[11, 19, 26]; // CORRUPT, CONSTRAINT, NOTADB

/// `adapters/sqlite.py::_map_sqlite_error`.
pub fn map_failure(failure: Failure, deadline_elapsed: bool) -> StorageError {
    match failure {
        Failure::Storage(error) => error,
        Failure::Internal(_) => StorageError::new("database_internal", "Storage operation failed"),
        Failure::Sqlite(error) => {
            let code = match &error {
                rusqlite::Error::SqliteFailure(inner, _) => inner.extended_code & 0xFF,
                _ => return StorageError::new("database_internal", "Storage operation failed"),
            };
            if code == SQLITE_INTERRUPT && deadline_elapsed {
                return StorageError::retryable("database_timeout", "Storage transaction timed out", 25);
            }
            if SQLITE_BUSY_CODES.contains(&code) {
                return StorageError::retryable("database_busy", "Storage writer is busy", 25);
            }
            if code == SQLITE_FULL {
                return StorageError::new(
                    "database_unavailable",
                    "SQLite storage is full: the authority volume has no free space",
                );
            }
            if SQLITE_UNAVAILABLE_CODES.contains(&code) {
                return StorageError::retryable("database_unavailable", "SQLite storage is unavailable", 100);
            }
            if SQLITE_INTEGRITY_CODES.contains(&code) {
                return StorageError::new("database_integrity", "SQLite integrity constraint failed");
            }
            StorageError::new("database_internal", "SQLite operation failed")
        }
    }
}
