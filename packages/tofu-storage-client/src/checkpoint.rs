//! Instance-bound native checkpoint control (`lib/storage/checkpoint_control.py`).
//!
//! The transport supplies one attempt and its scope/close checks. This port
//! keeps no database and no retry loop; a stale binding never auto-refreshes
//! (the daemon's runtime UUID fences capability IDs across a restart).

use std::collections::BTreeMap;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{Map, Value};

use crate::contract::contract;
use crate::error::{StorageError, StorageResult};
use crate::timed_lock::TimedLock;

/// `(runtime_id, maximum_response_bytes, available)`.
pub type Binding = (String, u64, bool);

#[derive(Default)]
struct BindingState {
    bindings: BTreeMap<u64, Binding>,
    unavailable: Option<Binding>,
    closed: bool,
}

pub(crate) fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

pub struct CheckpointPort {
    timeout: Duration,
    negotiation: TimedLock,
    state: Mutex<BindingState>,
}

/// The transport half the port borrows for one call.
pub trait CheckpointTransport {
    fn request_once(
        &self,
        operation: &str,
        payload: Map<String, Value>,
        owner: u64,
        deadline_unix_ms: u64,
    ) -> StorageResult<Value>;
    fn check_scope(&self, owner: u64) -> StorageResult<()>;
    fn is_closed(&self) -> bool;
}

impl CheckpointPort {
    pub fn new(timeout: Duration) -> Self {
        Self {
            timeout,
            negotiation: TimedLock::default(),
            state: Mutex::new(BindingState::default()),
        }
    }

    pub fn close(&self) {
        self.lock_unbounded().closed = true;
    }

    fn lock_unbounded(&self) -> MutexGuard<'_, BindingState> {
        self.state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    fn open(
        &self,
        transport: &dyn CheckpointTransport,
        owner: u64,
        closed: bool,
    ) -> StorageResult<()> {
        if owner == 0 {
            return Err(StorageError::new(
                "database_forbidden",
                "native checkpoint owner differs",
            ));
        }
        transport.check_scope(owner)?;
        if closed || transport.is_closed() {
            return Err(StorageError::new(
                "database_unavailable",
                "native checkpoint client closed",
            ));
        }
        Ok(())
    }

    pub fn binding(
        &self,
        transport: &dyn CheckpointTransport,
        owner: u64,
        deadline_unix_ms: u64,
    ) -> StorageResult<Binding> {
        let closed = self.lock_unbounded().closed;
        self.open(transport, owner, closed)?;
        let remaining = self
            .timeout
            .as_secs_f64()
            .min(deadline_unix_ms as f64 / 1000.0 - now_seconds());
        if remaining <= 0.0 {
            return Err(StorageError::new(
                "database_timeout",
                "checkpoint binding deadline elapsed",
            ));
        }
        // One binding negotiation at a time, for at most the caller's budget.
        let Some(_negotiation) = self.negotiation.acquire(Duration::from_secs_f64(remaining))
        else {
            return Err(StorageError::new(
                "database_timeout",
                "checkpoint binding deadline elapsed",
            ));
        };
        {
            let state = self.lock_unbounded();
            self.open(transport, owner, state.closed)?;
            if let Some(binding) = &state.unavailable {
                return Ok(binding.clone());
            }
            if let Some(binding) = state.bindings.get(&owner) {
                return Ok(binding.clone());
            }
        }
        if deadline_unix_ms as f64 / 1000.0 <= now_seconds() {
            return Err(StorageError::new(
                "database_timeout",
                "checkpoint binding deadline elapsed",
            ));
        }
        let result = transport.request_once(
            &contract().checkpoint_capabilities_operation,
            Map::new(),
            owner,
            deadline_unix_ms,
        )?;
        let binding = validate_capabilities(&result)?;
        let mut state = self.lock_unbounded();
        self.open(transport, owner, state.closed)?;
        if !binding.2 {
            state.unavailable = Some(binding.clone());
            return Ok(binding);
        }
        state.bindings.insert(owner, binding.clone());
        Ok(binding)
    }

    pub fn available(
        &self,
        transport: &dyn CheckpointTransport,
        owner: u64,
    ) -> StorageResult<bool> {
        let deadline = ((now_seconds() + self.timeout.as_secs_f64()) * 1000.0) as u64;
        Ok(self.binding(transport, owner, deadline)?.2)
    }

    pub fn control(
        &self,
        transport: &dyn CheckpointTransport,
        operation: &str,
        payload: Map<String, Value>,
        owner: u64,
        deadline_unix_ms: u64,
        response_bytes: u64,
    ) -> StorageResult<Value> {
        let contract = contract();
        if !contract
            .checkpoint_operations
            .iter()
            .any(|name| name == operation)
            || response_bytes == 0
            || response_bytes > isize::MAX as u64
            || deadline_unix_ms == 0
        {
            return Err(StorageError::new(
                "database_protocol_error",
                "invalid checkpoint control admission",
            ));
        }
        let (runtime_id, _maximum, available) = self.binding(transport, owner, deadline_unix_ms)?;
        if !available {
            return Err(StorageError::new(
                "database_unsupported",
                "selected authority lacks durable native checkpoint control",
            ));
        }
        if deadline_unix_ms as f64 / 1000.0 <= now_seconds() {
            return Err(StorageError::new(
                "database_timeout",
                "checkpoint deadline elapsed before effects",
            ));
        }
        let closed = self.lock_unbounded().closed;
        self.open(transport, owner, closed)?;
        let mut request = Map::new();
        request.insert(
            "format".to_owned(),
            Value::from(contract.checkpoint_transport_format.clone()),
        );
        request.insert("runtime_id".to_owned(), Value::from(runtime_id));
        request.insert("operation".to_owned(), Value::from(operation));
        request.insert("payload".to_owned(), Value::Object(payload));
        request.insert("response_bytes".to_owned(), Value::from(response_bytes));
        let result = transport.request_once(
            &contract.checkpoint_control_operation,
            request,
            owner,
            deadline_unix_ms,
        )?;
        let closed = self.lock_unbounded().closed;
        self.open(transport, owner, closed)?;
        let valid = result.get("format").and_then(Value::as_str)
            == Some(contract.checkpoint_format.as_str())
            && result.get("accepted").is_some_and(Value::is_boolean);
        if !valid {
            return Err(StorageError::new(
                "database_protocol_error",
                "invalid checkpoint control response",
            ));
        }
        Ok(result)
    }
}

fn validate_capabilities(result: &Value) -> StorageResult<Binding> {
    let invalid = || {
        StorageError::new(
            "database_protocol_error",
            "invalid checkpoint capability response",
        )
    };
    let object = result.as_object().ok_or_else(invalid)?;
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    if keys
        != [
            "available",
            "format",
            "maximum_response_bytes",
            "runtime_id",
        ]
    {
        return Err(invalid());
    }
    if object["format"].as_str() != Some(contract().checkpoint_transport_format.as_str()) {
        return Err(invalid());
    }
    let available = object["available"].as_bool().ok_or_else(invalid)?;
    let maximum = match &object["maximum_response_bytes"] {
        Value::Number(number) if number.is_u64() && number.as_u64() > Some(0) => {
            number.as_u64().unwrap()
        }
        _ => return Err(invalid()),
    };
    let runtime_id = object["runtime_id"].as_str().ok_or_else(invalid)?;
    let canonical = uuid::Uuid::parse_str(runtime_id)
        .map(|parsed| parsed.hyphenated().to_string() == runtime_id)
        .unwrap_or(false);
    if !canonical {
        return Err(StorageError::new(
            "database_protocol_error",
            "invalid checkpoint runtime identity",
        ));
    }
    Ok((runtime_id.to_owned(), maximum, available))
}
