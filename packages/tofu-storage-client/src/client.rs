//! The application-facing storage.v2 client (`lib/storage/v2_client.py`).
//!
//! RPCs borrow demand-created authenticated sessions from [`SessionPool`];
//! replay-safety classification, backoff and deadlines are the Python
//! client's, constant for constant (`docs/STORAGE.md`, "Client retry
//! contract"): reads replay freely, commands replay only with a pre-dispatch
//! proof or a classified availability failure under their unchanged command
//! identity, and a runtime-bound client waits transient failures out while
//! its supervised authority is alive, with no aggregate time window.
//!
//! The client is synchronous: every call runs on its caller's thread. An
//! async caller runs it on a dedicated thread per call, never a capped or
//! CPU-sized pool (`docs/RESOURCE_CONTROL.md`, "Hardware never limits work").

use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, RwLock};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::checkpoint::{now_seconds, CheckpointPort, CheckpointTransport};
use crate::contract::contract;
use crate::error::{from_wire, StorageError, StorageResult, AVAILABILITY_BACKOFF_BASE_MS};
use crate::pool::{Acquire, PoolSnapshot, SessionPool};
use crate::semantics::{
    application_notice, application_result, committed_result, request_payload, validate_operation,
};
use crate::session::{Session, TransportError, TransportResult, WireResponse};
use crate::timed_lock::TimedLock;

const RETRYABLE_CODES: [&str; 3] = ["database_timeout", "database_busy", "database_unavailable"];
/// Busy is WAL flow control, not failure: a fine-grained schedule.
const BUSY_COMMAND_ATTEMPTS: u32 = 16;
const BUSY_COMMAND_BASE_DELAY_S: f64 = 0.002;
const BUSY_COMMAND_MAX_DELAY_S: f64 = 0.250;
/// Availability-class failures escalate past the server's blip hint.
const AVAILABILITY_BACKOFF_CAP_S: f64 = 8.0;
/// A successful call this slow is authority pressure no retry surfaced.
pub const SLOW_RPC_WARN: Duration = Duration::from_secs(2);
/// Idle sockets retained when the daemon declares no connection budget.
const POOL_SIZE_LEAN_FALLBACK: usize = 4;
/// The socket outlives the wire deadline only by the answer's round trip.
const SOCKET_TIMEOUT_SLACK: Duration = Duration::from_secs(1);
/// Retire idle sessions well before any daemon build's idle reaping (30 s).
const SESSION_IDLE_CAP: Duration = Duration::from_secs(20);

/// Write-time redaction applied to every command payload before it is sent.
pub trait WriteRedactor: Send + Sync {
    /// `owner_user_id` is the client's authenticated owner, whose
    /// model-routing keys join the known set.
    fn redact(&self, payload: &Map<String, Value>, owner_user_id: u64) -> Map<String, Value>;
}

/// Observability and commit publication ports; every method defaults to a no-op.
pub trait StorageObserver: Send + Sync {
    /// One finished call: `outcome` is `success`, `error` or `cancelled`.
    fn rpc(
        &self,
        _kind: &str,
        _operation: &str,
        _elapsed: Duration,
        _outcome: &str,
        _attempts: u32,
    ) {
    }
    /// One scheduled replay.
    fn retry(&self, _retry: &RetryNotice<'_>) {}
    /// A slow wait the calling thread's owner presents (`observe_storage_wait`).
    fn slow_wait(&self, _error: &StorageError) {}
    /// Post-commit notices of one command, already mapped for observers.
    fn committed_events(&self, _events: &[Value]) {}
    /// One command committed (`publish_committed_operation`).
    fn committed_operation(&self, _operation: &str) {}
}

/// One scheduled replay of a classified transient failure.
pub struct RetryNotice<'a> {
    pub kind: &'a str,
    pub operation: &'a str,
    pub next_attempt: u32,
    /// `None` while the call waits on the runtime's liveness instead.
    pub attempt_cap: Option<u32>,
    pub pause: Duration,
    pub error: &'a StorageError,
    pub waited: Duration,
}

struct SilentObserver;
impl StorageObserver for SilentObserver {}

/// The selected runtime's liveness witness (`bind_runtime_recovery`).
pub trait RuntimeRecovery: Send + Sync {
    /// Wait out one pause, then answer the ready generation, or fail once the
    /// authority is proven down or the runtime is stopping.
    fn resolve(&self, pause: Duration) -> StorageResult<Arc<StorageV2Client>>;
}

/// Cooperative cancellation of one call: no replay and no wait once set; an
/// in-flight exchange is woken by shutting its socket down.
#[derive(Default)]
pub struct Cancellation {
    cancelled: AtomicBool,
    bound: Mutex<Vec<(u64, TcpStream)>>,
    next: std::sync::atomic::AtomicU64,
}

impl Cancellation {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::SeqCst);
        for (_, stream) in self.bound.lock().unwrap_or_else(|p| p.into_inner()).iter() {
            let _ = stream.shutdown(std::net::Shutdown::Both);
        }
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }

    fn bind(&self, stream: TcpStream) -> CancellationBinding<'_> {
        let token = self.next.fetch_add(1, Ordering::SeqCst);
        let mut bound = self.bound.lock().unwrap_or_else(|p| p.into_inner());
        if self.is_cancelled() {
            let _ = stream.shutdown(std::net::Shutdown::Both);
        }
        bound.push((token, stream));
        CancellationBinding { owner: self, token }
    }
}

struct CancellationBinding<'a> {
    owner: &'a Cancellation,
    token: u64,
}

impl Drop for CancellationBinding<'_> {
    fn drop(&mut self) {
        self.owner
            .bound
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .retain(|(token, _)| *token != self.token);
    }
}

/// Construction parameters (`StorageV2Client.__init__`).
#[derive(Clone, Debug)]
pub struct ClientConfig {
    pub host: String,
    pub port: u16,
    pub secret: Vec<u8>,
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
    /// Per-request I/O policy; floored at 50 ms.
    pub timeout: Duration,
    /// Attempt bound without a liveness witness; floored at 1.
    pub read_attempts: u32,
    pub session_pool_size: Option<usize>,
}

impl ClientConfig {
    pub fn new(
        host: &str,
        port: u16,
        secret: &[u8],
        owner_id: u64,
        tenant_id: Option<u64>,
    ) -> Self {
        Self {
            host: host.to_owned(),
            port,
            secret: secret.to_vec(),
            owner_id,
            tenant_id,
            timeout: Duration::from_secs(5),
            read_attempts: 3,
            session_pool_size: None,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Query,
    Command,
    Maintenance,
    Metrics,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Kind::Query => "query",
            Kind::Command => "command",
            Kind::Maintenance => "maintenance",
            Kind::Metrics => "metrics",
        }
    }
}

/// A decode outcome after a complete exchange.
enum Decoded<T> {
    /// A classified failure answer; the session stays aligned.
    Failure(StorageError),
    /// The answer could not be decoded; the session is retired.
    Codec(String),
    Value(T),
}

fn pool_size_from_budget(budget: &Map<String, Value>) -> usize {
    match budget.get("maximumConnections") {
        Some(Value::Number(number)) if number.as_u64().is_some_and(|value| value >= 1) => {
            (number.as_u64().unwrap() / 2).max(1) as usize
        }
        _ => POOL_SIZE_LEAN_FALLBACK,
    }
}

fn unix_ms_after(timeout: Duration) -> u64 {
    ((now_seconds() + timeout.as_secs_f64()) * 1000.0) as u64
}

fn jitter_fraction() -> f64 {
    (uuid::Uuid::new_v4().as_u128() >> 75) as f64 / (1u64 << 53) as f64
}

fn deadline_or(deadline: Option<Duration>, default: Duration) -> Duration {
    deadline
        .map(|deadline| deadline.max(Duration::from_millis(1)))
        .unwrap_or(default)
}

/// The pooled-path mapping of a transport failure (`_exchange_once`).
fn classify_transport(error: TransportError, operation: &str) -> StorageError {
    match error {
        TransportError::Wire {
            code,
            message,
            retryable,
        } => from_wire(Some(&code), Some(&message), retryable, operation),
        TransportError::ConnectionClosed => {
            let mut error = StorageError::retryable(
                "database_unavailable",
                "storage.v2 authority closed the connection",
                50,
            );
            error.connection_closed = true;
            error
        }
        TransportError::Protocol(detail) => StorageError::new(
            "database_protocol_error",
            format!("storage.v2 protocol violation: {detail}"),
        ),
        TransportError::Io => StorageError::retryable(
            "database_unavailable",
            "storage.v2 authority is unavailable",
            100,
        ),
        TransportError::Codec(detail) => StorageError::new(
            "database_protocol_error",
            format!("storage.v2 codec failure: {detail}"),
        ),
    }
}

/// The dedicated health lane's mapping (`_health_call`).
fn classify_health_transport(error: TransportError, operation: &str) -> StorageError {
    match error {
        TransportError::ConnectionClosed => StorageError::retryable(
            "database_unavailable",
            "storage.v2 authority closed the health connection",
            100,
        ),
        TransportError::Protocol(_) => StorageError::new(
            "database_protocol_error",
            "storage.v2 health protocol violation",
        ),
        other => classify_transport(other, operation),
    }
}

fn decode_response(response: WireResponse, operation: &str) -> Decoded<Value> {
    if response.status != 0 {
        return Decoded::Failure(from_wire(
            response.error_code.as_deref(),
            response.error_message.as_deref(),
            response.retryable,
            operation,
        ));
    }
    match response.json() {
        Ok(value) => Decoded::Value(value),
        Err(detail) => Decoded::Codec(detail),
    }
}

fn transient_answer(error: &StorageError) -> bool {
    (error.retryable || error.code == "database_timeout")
        && RETRYABLE_CODES.contains(&error.code.as_str())
}

pub struct StorageV2Client {
    host: String,
    port: u16,
    secret: Vec<u8>,
    owner_id: u64,
    tenant_id: Option<u64>,
    timeout: Duration,
    read_attempts: u32,
    pool: SessionPool,
    readiness_budget: Mutex<Map<String, Value>>,
    health_lane: TimedLock,
    health_session: Mutex<Option<Session>>,
    recovery: RwLock<Option<Arc<dyn RuntimeRecovery>>>,
    redactor: RwLock<Option<Arc<dyn WriteRedactor>>>,
    observer: RwLock<Arc<dyn StorageObserver>>,
    checkpoint: CheckpointPort,
}

impl StorageV2Client {
    pub fn new(config: ClientConfig) -> StorageResult<Self> {
        let invalid = |message: &str| StorageError::new("database_protocol_error", message);
        if !matches!(config.host.as_str(), "127.0.0.1" | "localhost" | "::1") {
            return Err(invalid("storage.v2 is loopback-only"));
        }
        if !(32..=256).contains(&config.secret.len()) {
            return Err(invalid("storage.v2 secret must contain 32..256 bytes"));
        }
        if config.owner_id == 0 || config.tenant_id == Some(0) {
            return Err(invalid("storage.v2 owner/tenant IDs must be positive"));
        }
        let timeout = config.timeout.max(Duration::from_millis(50));
        let size = config
            .session_pool_size
            .unwrap_or(POOL_SIZE_LEAN_FALLBACK)
            .max(1);
        Ok(Self {
            host: config.host,
            port: config.port,
            secret: config.secret,
            owner_id: config.owner_id,
            tenant_id: config.tenant_id,
            timeout,
            read_attempts: config.read_attempts.max(1),
            pool: SessionPool::new(size, SESSION_IDLE_CAP),
            readiness_budget: Mutex::new(Map::new()),
            health_lane: TimedLock::default(),
            health_session: Mutex::new(None),
            recovery: RwLock::new(None),
            redactor: RwLock::new(None),
            observer: RwLock::new(Arc::new(SilentObserver)),
            checkpoint: CheckpointPort::new(timeout),
        })
    }

    pub fn endpoint(&self) -> (&str, u16) {
        (&self.host, self.port)
    }

    pub fn owner_id(&self) -> u64 {
        self.owner_id
    }

    pub fn tenant_id(&self) -> Option<u64> {
        self.tenant_id
    }

    pub fn request_timeout(&self) -> Duration {
        self.timeout
    }

    /// Bind the selected runtime's liveness witness before publication.
    pub fn bind_runtime_recovery(&self, recovery: Arc<dyn RuntimeRecovery>) {
        *self.recovery.write().unwrap_or_else(|p| p.into_inner()) = Some(recovery);
    }

    /// Install the write-time redaction every `command()` payload passes.
    /// Production installs [`StorageV2Client::install_credential_redaction`].
    pub fn set_write_redactor(&self, redactor: Arc<dyn WriteRedactor>) {
        *self.redactor.write().unwrap_or_else(|p| p.into_inner()) = Some(redactor);
    }

    pub fn set_observer(&self, observer: Arc<dyn StorageObserver>) {
        *self.observer.write().unwrap_or_else(|p| p.into_inner()) = observer;
    }

    fn observer(&self) -> Arc<dyn StorageObserver> {
        self.observer
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }

    fn budget(&self) -> MutexGuard<'_, Map<String, Value>> {
        self.readiness_budget
            .lock()
            .unwrap_or_else(|p| p.into_inner())
    }

    /// Record the daemon's credential-free readiness `resourceBudget`.
    pub fn set_readiness_budget(&self, budget: Option<Map<String, Value>>) {
        let budget = budget.unwrap_or_default();
        let size = pool_size_from_budget(&budget);
        *self.budget() = budget;
        self.pool.resize(size);
    }

    pub fn readiness_budget(&self) -> Map<String, Value> {
        self.budget().clone()
    }

    /// Pool counters plus the idle-retention ceiling the budget allows.
    pub fn resource_snapshot(&self) -> (PoolSnapshot, usize) {
        (self.pool.snapshot(), pool_size_from_budget(&self.budget()))
    }

    pub fn resize_resource_capacity(&self, capacity: usize) -> Result<(), String> {
        let ceiling = pool_size_from_budget(&self.budget());
        if !(1..=ceiling).contains(&capacity) {
            return Err("storage capacity exceeds selected authority budget".to_owned());
        }
        self.pool.resize(capacity);
        Ok(())
    }

    /// Close every idle session and the health lane; in-flight sessions
    /// retire on release. Idempotent; a closed client answers classified errors.
    pub fn close(&self) {
        self.pool.close();
        self.checkpoint.close();
        let session = self
            .health_session
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .take();
        if let Some(session) = session {
            session.close();
        }
    }

    fn open_session(&self, timeout: Duration) -> TransportResult<Session> {
        Session::connect(
            &self.host,
            self.port,
            &self.secret,
            self.owner_id,
            self.tenant_id,
            &[contract().storage_schema_version],
            timeout,
        )
    }

    /// One pooled-session exchange: `transfer` owns the wire, `decode` its answer.
    fn exchange<T, R>(
        &self,
        operation: &str,
        timeout: Duration,
        cancellation: Option<&Cancellation>,
        transfer: impl FnOnce(&mut Session) -> TransportResult<T>,
        decode: impl FnOnce(T) -> Decoded<R>,
    ) -> StorageResult<R> {
        let stop_at = Instant::now() + timeout;
        let mut session = match self
            .pool
            .acquire(timeout, |remaining| self.open_session(remaining))?
        {
            Acquire::Session(session) => session,
            Acquire::Transport(error) => return Err(classify_transport(error, operation)),
        };
        let remaining = stop_at.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            self.pool.release(session, false);
            return Err(StorageError::retryable(
                "database_timeout",
                "storage.v2 request expired before dispatch",
                25,
            )
            .not_dispatched());
        }
        if session
            .set_timeout(remaining + SOCKET_TIMEOUT_SLACK)
            .is_err()
        {
            self.pool.release(session, true);
            return Err(classify_transport(TransportError::Io, operation));
        }
        let outcome = {
            let _binding = match (cancellation, session.interrupt_handle()) {
                (Some(cancellation), Ok(handle)) => Some(cancellation.bind(handle)),
                _ => None,
            };
            transfer(&mut session)
        };
        let cancelled = cancellation.is_some_and(Cancellation::is_cancelled);
        let answer = match outcome {
            Ok(answer) => answer,
            Err(error) => {
                self.pool.release(session, true);
                return Err(classify_transport(error, operation));
            }
        };
        match decode(answer) {
            Decoded::Value(value) => {
                self.pool.release(session, cancelled);
                Ok(value)
            }
            Decoded::Failure(error) => {
                self.pool.release(session, cancelled);
                Err(error)
            }
            Decoded::Codec(detail) => {
                self.pool.release(session, true);
                Err(StorageError::new(
                    "database_protocol_error",
                    format!("storage.v2 codec failure: {detail}"),
                ))
            }
        }
    }

    /// One pooled transport attempt; replay policy belongs to the caller.
    fn request_once(
        &self,
        operation: &str,
        body: &Map<String, Value>,
        timeout: Duration,
        command_id: Option<&str>,
        deadline_unix_ms: u64,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        self.exchange(
            operation,
            timeout,
            cancellation,
            |session| session.request(operation, body, command_id, deadline_unix_ms),
            |response| decode_response(response, operation),
        )
    }

    fn attempt(
        &self,
        operation: &str,
        payload: Option<&Map<String, Value>>,
        timeout: Duration,
        command_id: Option<&str>,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        let body = request_payload(operation, payload, self.owner_id)?;
        let deadline_unix_ms = unix_ms_after(timeout);
        self.request_once(
            operation,
            &body,
            timeout,
            command_id,
            deadline_unix_ms,
            cancellation,
        )
    }

    fn waits_while_alive(&self, kind: Kind) -> bool {
        matches!(kind, Kind::Query | Kind::Command)
            && self
                .recovery
                .read()
                .unwrap_or_else(|p| p.into_inner())
                .is_some()
    }

    fn call(
        &self,
        kind: Kind,
        operation: &str,
        payload: Option<&Map<String, Value>>,
        deadline: Option<Duration>,
        command_id: Option<&str>,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        validate_operation(operation)?;
        let timeout = deadline_or(deadline, self.timeout);
        let read_replay_safe = matches!(kind, Kind::Query | Kind::Metrics);
        let attempts = if read_replay_safe || kind == Kind::Command {
            self.read_attempts
        } else {
            1
        };
        // tofu-db rejects any state-mutating op that lacks a command_id, and
        // maintenance-kind operations mutate. Only reads may omit it.
        let generated;
        let wire_command_id = if kind == Kind::Query {
            None
        } else {
            Some(match command_id {
                Some(command_id) => command_id,
                None => {
                    generated = format!("{operation}:{}", uuid::Uuid::new_v4().simple());
                    generated.as_str()
                }
            })
        };
        let observer = self.observer();
        let started = Instant::now();
        let mut active: Option<Arc<StorageV2Client>> = None;
        let mut waits_while_alive: Option<bool> = None;
        let mut attempt: u32 = 0;
        loop {
            let client = active.as_deref().unwrap_or(self);
            // Every replay gets the full wire deadline: no aggregate budget
            // shrinks it, so a retry after a slow attempt is never born expired.
            let error =
                match client.attempt(operation, payload, timeout, wire_command_id, cancellation) {
                    Ok(result) => {
                        observer.rpc(
                            kind.name(),
                            operation,
                            started.elapsed(),
                            "success",
                            attempt + 1,
                        );
                        return Ok(result);
                    }
                    Err(error) => error,
                };
            if cancellation.is_some_and(Cancellation::is_cancelled) {
                observer.rpc(
                    kind.name(),
                    operation,
                    started.elapsed(),
                    "cancelled",
                    attempt + 1,
                );
                return Err(error);
            }
            // `deadline_elapsed` arrives non-retryable because that single
            // wire flag also gates command replay, but both server variants
            // are replay-safe: admission rejects pre-dispatch, and an
            // execution deadline aborts the transaction pre-commit.
            let timeout_transient = error.code == "database_timeout";
            let retry_classified = transient_answer(&error);
            let busy_command = kind == Kind::Command && error.code == "database_busy";
            let command_replay_safe = kind == Kind::Command
                && (error.request_not_dispatched
                    || busy_command
                    || timeout_transient
                    || error.connection_closed
                    || error.code == "database_unavailable");
            if !retry_classified || !(read_replay_safe || command_replay_safe) {
                observer.rpc(
                    kind.name(),
                    operation,
                    started.elapsed(),
                    "error",
                    attempt + 1,
                );
                return Err(error);
            }
            let waits = *waits_while_alive.get_or_insert_with(|| self.waits_while_alive(kind));
            let mut attempt_cap = None;
            if !waits {
                let cap = if busy_command {
                    BUSY_COMMAND_ATTEMPTS
                } else {
                    attempts
                };
                attempt_cap = Some(cap);
                if attempt + 1 >= cap {
                    observer.rpc(
                        kind.name(),
                        operation,
                        started.elapsed(),
                        "error",
                        attempt + 1,
                    );
                    return Err(error);
                }
            }
            let delay = if busy_command && !error.request_not_dispatched {
                (BUSY_COMMAND_BASE_DELAY_S * 2f64.powi(attempt.min(16) as i32))
                    .min(BUSY_COMMAND_MAX_DELAY_S)
            } else if attempt >= 1 {
                (AVAILABILITY_BACKOFF_BASE_MS as f64 / 1000.0
                    * 2f64.powi((attempt - 1).min(16) as i32))
                .min(AVAILABILITY_BACKOFF_CAP_S)
            } else {
                match error.retry_after_ms {
                    Some(hint) if hint > 0 => hint as f64 / 1000.0,
                    _ => 0.05,
                }
            };
            let pause =
                Duration::from_secs_f64(delay + jitter_fraction() * (delay * 0.25).min(1.0));
            let waited = started.elapsed();
            observer.retry(&RetryNotice {
                kind: kind.name(),
                operation,
                next_attempt: attempt + 2,
                attempt_cap,
                pause,
                error: &error,
                waited,
            });
            attempt += 1;
            if !waits {
                std::thread::sleep(pause);
                continue;
            }
            // A slow call's wait belongs to its thread's owner, which presents
            // it and may end it (Stop, shutdown).
            if waited >= SLOW_RPC_WARN {
                observer.slow_wait(&error);
            }
            let recovery = self
                .recovery
                .read()
                .unwrap_or_else(|p| p.into_inner())
                .clone();
            let Some(recovery) = recovery else {
                observer.rpc(kind.name(), operation, started.elapsed(), "error", attempt);
                return Err(error);
            };
            // Transport loss does not prove whether a command committed;
            // replaying its unchanged receipt identity on the live generation
            // resolves the ambiguity.
            let candidate = match recovery.resolve(pause) {
                Ok(candidate) => candidate,
                Err(failure) => {
                    observer.rpc(kind.name(), operation, started.elapsed(), "error", attempt);
                    return Err(failure);
                }
            };
            if (candidate.owner_id, candidate.tenant_id) != (self.owner_id, self.tenant_id) {
                observer.rpc(kind.name(), operation, started.elapsed(), "error", attempt);
                return Err(StorageError::new(
                    "database_integrity",
                    "Storage recovery changed the authenticated authority scope",
                ));
            }
            active = Some(candidate);
        }
    }

    pub fn query(
        &self,
        operation: &str,
        payload: Option<&Map<String, Value>>,
        deadline: Option<Duration>,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        let result = self.call(
            Kind::Query,
            operation,
            payload,
            deadline,
            None,
            cancellation,
        )?;
        application_result(operation, result)
    }

    /// Reads answered by one pipelined exchange on one pooled session.
    ///
    /// Element `i` is what `query(requests[i])` returns. A read answered with
    /// a classified transient failure, or left unanswered because the
    /// exchange failed, is read again through `query` and its replay policy.
    pub fn query_many(
        &self,
        requests: &[(String, Option<Map<String, Value>>)],
        deadline: Option<Duration>,
    ) -> Vec<StorageResult<Value>> {
        let mut outcomes: Vec<Option<StorageResult<Value>>> = Vec::with_capacity(requests.len());
        let mut validated = true;
        for (operation, _) in requests {
            if let Err(error) = validate_operation(operation) {
                outcomes.push(Some(Err(error)));
                validated = false;
            } else {
                outcomes.push(None);
            }
        }
        if !validated {
            // validate_operation raises before any exchange in Python.
            let error = outcomes
                .into_iter()
                .flatten()
                .find(Result::is_err)
                .expect("invalid read");
            return requests.iter().map(|_| error.clone()).collect();
        }
        let observer = self.observer();
        if requests.len() > 1 {
            let timeout = deadline_or(deadline, self.timeout);
            let started = Instant::now();
            let bodies: StorageResult<Vec<(String, Map<String, Value>)>> = requests
                .iter()
                .map(|(operation, payload)| {
                    request_payload(operation, payload.as_ref(), self.owner_id)
                        .map(|body| (operation.clone(), body))
                })
                .collect();
            let bodies = match bodies {
                Ok(bodies) => bodies,
                Err(error) => return requests.iter().map(|_| Err(error.clone())).collect(),
            };
            let deadline_unix_ms = unix_ms_after(timeout);
            let responses = self
                .exchange(
                    &requests[0].0,
                    timeout,
                    None,
                    |session| session.request_many(&bodies, deadline_unix_ms),
                    Decoded::Value,
                )
                .unwrap_or_default();
            for (index, response) in responses.into_iter().enumerate() {
                let Some(response) = response else { continue };
                let operation = &requests[index].0;
                let outcome = match decode_response(response, operation) {
                    Decoded::Value(value) => application_result(operation, value),
                    Decoded::Failure(error) => Err(error),
                    Decoded::Codec(detail) => Err(StorageError::new(
                        "database_protocol_error",
                        format!("storage.v2 codec failure: {detail}"),
                    )),
                };
                match outcome {
                    Ok(value) => {
                        observer.rpc("query", operation, started.elapsed(), "success", 1);
                        outcomes[index] = Some(Ok(value));
                    }
                    Err(error) if transient_answer(&error) => {}
                    Err(error) => {
                        observer.rpc("query", operation, started.elapsed(), "error", 1);
                        outcomes[index] = Some(Err(error));
                    }
                }
            }
        }
        outcomes
            .into_iter()
            .enumerate()
            .map(|(index, outcome)| {
                outcome.unwrap_or_else(|| {
                    let (operation, payload) = &requests[index];
                    self.query(operation, payload.as_ref(), deadline, None)
                })
            })
            .collect()
    }

    /// One command; its payload passes the installed write redaction first.
    ///
    /// Python's `command()` always redacts. With no redactor installed a
    /// command that carries a payload is refused before anything is sent,
    /// rather than stored unredacted.
    pub fn command(
        &self,
        operation: &str,
        payload: Option<&Map<String, Value>>,
        command_id: Option<&str>,
        deadline: Option<Duration>,
    ) -> StorageResult<Value> {
        let redactor = self
            .redactor
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .clone();
        let redacted = match (payload, redactor) {
            (Some(payload), Some(redactor)) => Some(redactor.redact(payload, self.owner_id)),
            (Some(_), None) => {
                return Err(StorageError::new(
                    "database_protocol_error",
                    "storage.v2 command refused: no write redactor is installed",
                ))
            }
            (None, _) => None,
        };
        let result = self.call(
            Kind::Command,
            operation,
            redacted.as_ref(),
            deadline,
            command_id,
            None,
        )?;
        let observer = self.observer();
        let committed = committed_result(result)?;
        if let Some(events) = committed.events {
            let mut notices = Vec::with_capacity(events.len());
            for event in events {
                let Value::Object(event) = event else {
                    unreachable!("checked envelope")
                };
                notices.push(application_notice(event, Some(self.owner_id))?);
            }
            observer.committed_events(&notices);
        }
        let value = application_result(operation, committed.value)?;
        observer.committed_operation(operation);
        Ok(value)
    }

    pub fn maintenance(
        &self,
        operation: &str,
        payload: Option<&Map<String, Value>>,
        deadline: Option<Duration>,
    ) -> StorageResult<Value> {
        let deadline = Some(deadline.unwrap_or(Duration::from_secs(30)));
        self.call(Kind::Maintenance, operation, payload, deadline, None, None)
    }

    /// Daemon metrics plus the readiness budget; never fails.
    pub fn metrics(&self, deadline: Option<Duration>) -> Value {
        let budget = self.readiness_budget();
        let deadline = Some(deadline.unwrap_or(Duration::from_secs(2)));
        let reported = self
            .call(
                Kind::Metrics,
                "system.metrics",
                Some(&Map::new()),
                deadline,
                None,
                None,
            )
            .ok();
        let mut metrics = match reported {
            Some(Value::Object(reported)) => reported,
            _ => Map::new(),
        };
        metrics.insert("backend".to_owned(), Value::from("tofudb"));
        metrics.insert("protocol".to_owned(), Value::from("storage.v2"));
        if !budget.is_empty() && !metrics.contains_key("resourceBudget") {
            metrics.insert("resourceBudget".to_owned(), Value::Object(budget));
        }
        Value::Object(metrics)
    }

    /// Daemon-reported health on the dedicated lane, or the `conversation.count`
    /// round trip on binaries without `system.health`.
    pub fn health(&self, deadline: Option<Duration>) -> Value {
        let budget = self.readiness_budget();
        let deadline = Some(deadline.unwrap_or(Duration::from_secs(2)));
        let identity = |result: &mut Map<String, Value>, ready: bool| {
            result.insert("ready".to_owned(), Value::from(ready));
            result.insert("backend".to_owned(), Value::from("tofudb"));
            result.insert("protocol".to_owned(), Value::from("storage.v2"));
            result.insert("owner_id".to_owned(), Value::from(self.owner_id));
            result.insert(
                "tenant_id".to_owned(),
                self.tenant_id.map(Value::from).unwrap_or(Value::Null),
            );
        };
        let mut result = Map::new();
        identity(&mut result, false);
        match self.health_call("system.health", deadline) {
            Err(error) if error.code != "unknown_operation" => {
                result.insert("probe_error".to_owned(), Value::from(error.code));
                if !budget.is_empty() {
                    result.insert("resourceBudget".to_owned(), Value::Object(budget));
                }
                return Value::Object(result);
            }
            Err(_) => {}
            Ok(Value::Object(reported)) => {
                let ready = reported.get("ready").is_some_and(python_truthy);
                for (key, value) in reported {
                    result.insert(key, value);
                }
                identity(&mut result, ready);
                if !budget.is_empty() && !result.contains_key("resourceBudget") {
                    result.insert("resourceBudget".to_owned(), Value::Object(budget));
                }
                return Value::Object(result);
            }
            Ok(_) => {}
        }
        match self.health_call("conversation.count", deadline) {
            Ok(_) => {
                result.insert("ready".to_owned(), Value::from(true));
            }
            Err(error) => {
                result.insert("ready".to_owned(), Value::from(false));
                result.insert("probe_error".to_owned(), Value::from(error.code));
            }
        }
        if !budget.is_empty() {
            result.insert("resourceBudget".to_owned(), Value::Object(budget));
        }
        Value::Object(result)
    }

    /// One bounded RPC on the dedicated health session, never the pool, and
    /// a single attempt: the supervisor's liveness cadence is the retry loop.
    fn health_call(&self, operation: &str, deadline: Option<Duration>) -> StorageResult<Value> {
        validate_operation(operation)?;
        let timeout = deadline_or(deadline, self.timeout);
        let deadline_unix_ms = unix_ms_after(timeout);
        let started = Instant::now();
        let lane_elapsed = || {
            StorageError::retryable(
                "database_timeout",
                "storage.v2 health lane deadline elapsed",
                25,
            )
            .not_dispatched()
        };
        let Some(_lane) = self.health_lane.acquire(timeout) else {
            return Err(lane_elapsed());
        };
        if self.pool.is_closed() {
            return Err(
                StorageError::new("database_unavailable", "storage.v2 client is closed")
                    .not_dispatched(),
            );
        }
        let remaining = timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            return Err(lane_elapsed());
        }
        let mut slot = self
            .health_session
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        if slot.is_none() {
            match self.open_session(remaining) {
                Ok(session) => *slot = Some(session),
                Err(error) => return Err(classify_health_transport(error, operation)),
            }
        }
        let session = slot.as_mut().expect("health session");
        let remaining = timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            return Err(StorageError::retryable(
                "database_timeout",
                "storage.v2 health request expired before dispatch",
                25,
            )
            .not_dispatched());
        }
        let outcome = session
            .set_timeout(remaining)
            .map_err(TransportError::from)
            .and_then(|_| {
                let body = request_payload(operation, Some(&Map::new()), self.owner_id)
                    .map_err(|error| TransportError::Codec(error.message))?;
                session.request(operation, &body, None, deadline_unix_ms)
            });
        let response = match outcome {
            Ok(response) => response,
            Err(error) => {
                slot.take();
                return Err(classify_health_transport(error, operation));
            }
        };
        match decode_response(response, operation) {
            Decoded::Value(value) => {
                self.observer()
                    .rpc("health", operation, started.elapsed(), "success", 1);
                Ok(value)
            }
            Decoded::Failure(error) => Err(error),
            Decoded::Codec(detail) => {
                slot.take();
                Err(StorageError::new(
                    "database_protocol_error",
                    format!("storage.v2 codec failure: {detail}"),
                ))
            }
        }
    }

    /// Wait for committed worker changes without a semantic database read.
    pub fn worker_watch(
        &self,
        cursor: Value,
        deadline_unix_ms: u64,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        self.commit_watch(
            &contract().worker_watch_operation,
            cursor,
            deadline_unix_ms,
            cancellation,
        )
    }

    /// Wait for committed conversation changes without a semantic database read.
    pub fn conversation_watch(
        &self,
        cursor: Value,
        deadline_unix_ms: u64,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        self.commit_watch(
            &contract().conversation_watch_operation,
            cursor,
            deadline_unix_ms,
            cancellation,
        )
    }

    fn commit_watch(
        &self,
        operation: &str,
        cursor: Value,
        deadline_unix_ms: u64,
        cancellation: Option<&Cancellation>,
    ) -> StorageResult<Value> {
        let remaining = deadline_unix_ms as f64 / 1000.0 - now_seconds();
        if remaining <= 0.0 {
            return Err(StorageError::new(
                "database_timeout",
                "Commit watch deadline elapsed",
            ));
        }
        let mut body = Map::new();
        body.insert("cursor".to_owned(), cursor);
        self.request_once(
            operation,
            &body,
            Duration::from_secs_f64(remaining),
            None,
            deadline_unix_ms,
            cancellation,
        )
    }

    pub fn native_checkpoint_available(&self, user_id: u64) -> StorageResult<bool> {
        self.checkpoint.available(self, user_id)
    }

    pub fn native_checkpoint_control(
        &self,
        operation: &str,
        payload: Map<String, Value>,
        user_id: u64,
        deadline_unix_ms: u64,
        response_bytes: u64,
    ) -> StorageResult<Value> {
        self.checkpoint.control(
            self,
            operation,
            payload,
            user_id,
            deadline_unix_ms,
            response_bytes,
        )
    }
}

impl CheckpointTransport for StorageV2Client {
    fn request_once(
        &self,
        operation: &str,
        payload: Map<String, Value>,
        _owner: u64,
        deadline_unix_ms: u64,
    ) -> StorageResult<Value> {
        let remaining = self
            .timeout
            .as_secs_f64()
            .min(deadline_unix_ms as f64 / 1000.0 - now_seconds());
        if remaining <= 0.0 {
            return Err(StorageError::new(
                "database_timeout",
                "checkpoint deadline elapsed before effects",
            ));
        }
        StorageV2Client::request_once(
            self,
            operation,
            &payload,
            Duration::from_secs_f64(remaining),
            None,
            deadline_unix_ms,
            None,
        )
    }

    fn check_scope(&self, owner: u64) -> StorageResult<()> {
        if owner != self.owner_id {
            return Err(StorageError::new(
                "database_forbidden",
                "native checkpoint owner differs",
            ));
        }
        Ok(())
    }

    fn is_closed(&self) -> bool {
        self.pool.is_closed()
    }
}

impl Drop for StorageV2Client {
    fn drop(&mut self) {
        self.pool.close();
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(object) => !object.is_empty(),
    }
}
