//! The stable storage failure taxonomy (`lib/agent_core/storage_errors.py`).

use std::fmt;

use crate::contract::contract;

/// A sanitized, transport-stable storage failure.
///
/// Field semantics match the Python `StorageError` dataclass exactly: a code
/// outside the stable taxonomy folds to `database_internal` and keeps the
/// authority's own code in `wire_code` for diagnostics, and
/// `request_not_dispatched` (which unlocks command replay) is only ever set
/// by this client's own pre-dispatch proofs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StorageError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub retry_after_ms: Option<u64>,
    pub operation_id: String,
    pub request_not_dispatched: bool,
    pub wire_code: String,
    /// The peer closed at a frame boundary (`_ConnectionClosedError`): an
    /// availability failure whose command replay is receipt-safe.
    pub(crate) connection_closed: bool,
}

impl StorageError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        let (code, wire_code) = if contract().error_codes.contains(code) {
            (code.to_owned(), String::new())
        } else {
            ("database_internal".to_owned(), code.to_owned())
        };
        Self {
            code,
            message: message.into(),
            retryable: false,
            retry_after_ms: None,
            operation_id: String::new(),
            request_not_dispatched: false,
            wire_code,
            connection_closed: false,
        }
    }

    /// `StorageError(code, message, retryable, retry_after_ms)`.
    pub fn retryable(code: &str, message: impl Into<String>, retry_after_ms: u64) -> Self {
        let mut error = Self::new(code, message);
        error.retryable = true;
        error.retry_after_ms = Some(retry_after_ms);
        error
    }

    pub(crate) fn not_dispatched(mut self) -> Self {
        self.request_not_dispatched = true;
        self
    }

    pub(crate) fn with_operation(mut self, operation: &str) -> Self {
        self.operation_id = operation.to_owned();
        self
    }

    /// `is_transient_storage_error`: a classified availability failure.
    pub fn is_transient(&self) -> bool {
        matches!(
            self.code.as_str(),
            "database_timeout" | "database_busy" | "database_unavailable"
        )
    }

    /// The JSON shape of `StorageError.to_payload()`.
    pub fn to_payload(&self) -> serde_json::Value {
        let mut payload = serde_json::json!({
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_ms": self.retry_after_ms,
            "operation_id": self.operation_id,
        });
        if self.request_not_dispatched {
            payload["request_not_dispatched"] = serde_json::Value::Bool(true);
        }
        payload
    }
}

impl fmt::Display for StorageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for StorageError {}

pub type StorageResult<T> = Result<T, StorageError>;

/// Availability-class retries escalate from this base (`v2_client.py`
/// `_AVAILABILITY_BACKOFF_BASE_S`); load shedding uses it as its first hint.
pub(crate) const AVAILABILITY_BACKOFF_BASE_MS: u64 = 500;

/// `StorageV2Client._to_storage_error`: one failed wire response (or a
/// refused Hello) mapped onto the stable taxonomy.
pub(crate) fn from_wire(
    wire_code: Option<&str>,
    wire_message: Option<&str>,
    retryable: bool,
    operation: &str,
) -> StorageError {
    let wire_code = wire_code.unwrap_or("");
    let code = match wire_code {
        // A retryable refusal is transient load shedding; a non-retryable
        // one is a deterministic per-request bound the caller must shrink.
        "database_resource_exhausted" if retryable => "database_unavailable",
        "database_resource_exhausted" => "database_resource_exhausted",
        "authority_scope_mismatch" => "database_forbidden",
        "deadline_elapsed" => "database_timeout",
        other => other,
    };
    let message = match wire_message {
        Some(message) if !message.is_empty() => message,
        _ => "storage.v2 request failed",
    };
    let mut error = StorageError::new(code, message).with_operation(operation);
    if retryable {
        error.retryable = true;
        error.retry_after_ms = Some(if wire_code == "database_resource_exhausted" {
            AVAILABILITY_BACKOFF_BASE_MS
        } else {
            50
        });
    }
    error
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_codes_fold_to_internal_and_keep_the_wire_code() {
        let error = StorageError::new("invalid_request", "bad");
        assert_eq!(error.code, "database_internal");
        assert_eq!(error.wire_code, "invalid_request");
    }

    #[test]
    fn wire_mapping_splits_resource_exhaustion_by_retry_policy() {
        let shed = from_wire(Some("database_resource_exhausted"), Some("x"), true, "op");
        assert_eq!(
            (shed.code.as_str(), shed.retry_after_ms),
            ("database_unavailable", Some(500))
        );
        let bound = from_wire(Some("database_resource_exhausted"), Some("x"), false, "op");
        assert_eq!(
            (bound.code.as_str(), bound.retry_after_ms),
            ("database_resource_exhausted", None)
        );
        let deadline = from_wire(Some("deadline_elapsed"), None, false, "op");
        assert_eq!(deadline.code, "database_timeout");
        assert_eq!(deadline.message, "storage.v2 request failed");
        assert_eq!(deadline.operation_id, "op");
        let scope = from_wire(Some("authority_scope_mismatch"), Some("m"), true, "op");
        assert_eq!(
            (scope.code.as_str(), scope.retry_after_ms),
            ("database_forbidden", Some(50))
        );
    }
}
