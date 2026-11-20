//! SQL-transactional logical commit capture and a joined, durable shadow publisher.
//! Entry points mirror logical_outbox.py; filesystem effects occur only after SQL COMMIT.
mod crypto;
mod shadow;

use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::backend::{BackendCore, Config};
use crate::error::{map_failure, Failure, OpResult, StorageError};
use crate::params;
use crate::session::{Row, Session};

const STREAM: &str = "logical_outbox_stream_id";
const LAST: &str = "logical_outbox_last_sequence";
const PUBLISHED: &str = "logical_outbox_published_sequence";
const BYTES: &str = "logical_outbox_pending_bytes";
const KEY: &str = "logical_outbox_encryption_key_id";
const CODEC: &str = "tofu.bound-fernet-json.v1";
fn registry_version() -> i64 {
    static VERSION: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *VERSION.get_or_init(|| {
        let contract: Value = serde_json::from_str(include_str!("../../../contracts/storage_operations_v1.json"))
            .expect("storage operation contract");
        contract["sourceRegistryVersion"].as_i64().expect("registry version")
    })
}

pub(super) fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as i64
}
pub(super) fn canonical(value: &Value) -> Vec<u8> {
    fn sorted(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut keys: Vec<_> = map.keys().collect();
                keys.sort();
                Value::Object(keys.into_iter().map(|key| (key.clone(), sorted(&map[key]))).collect())
            }
            Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
            _ => value.clone(),
        }
    }
    serde_json::to_vec(&sorted(value)).expect("JSON value")
}
fn integrity(message: &str) -> Failure {
    StorageError::new("database_integrity", message).into()
}
fn meta(session: &Session<'_>, key: &str, default: i64) -> OpResult<i64> {
    match session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params![key])? {
        None => Ok(default),
        Some(row) => row
            .text("meta_value")
            .parse::<i64>()
            .ok()
            .filter(|value| *value >= 0)
            .ok_or_else(|| integrity("Logical outbox metadata is invalid")),
    }
}
fn set_meta(session: &Session<'_>, key: &str, value: impl ToString) -> OpResult<()> {
    session.execute("INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value",&params![key,value.to_string()])?;
    Ok(())
}

#[derive(Clone, Debug)]
pub struct LogicalOutboxPolicy {
    pub mode: String,
    pub capture_enabled: bool,
    pub publisher_enabled: bool,
    pub sink_root: Option<PathBuf>,
    pub reason: String,
    pub max_pending_bytes: u64,
    pub max_record_bytes: u64,
    pub max_segment_bytes: u64,
    pub max_shadow_bytes: u64,
    pub publish_batch_size: usize,
    pub access_mode: String,
}
fn env(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.into()).trim().to_owned()
}
fn positive(name: &str, default: u64) -> OpResult<u64> {
    let raw = std::env::var(name).unwrap_or_default();
    if raw.is_empty() {
        return Ok(default);
    }
    let value = raw.trim().parse::<i128>().map_err(|_| StorageError::protocol(format!("invalid {name}")))?;
    if value < 1 {
        return Err(StorageError::protocol(format!("{name} must be positive")).into());
    }
    value.try_into().map_err(|_| StorageError::protocol(format!("invalid {name}")).into())
}
fn log_budget_mib(data_dir: &std::path::Path) -> u64 {
    let contract: Value = serde_json::from_str(include_str!("../../../contracts/resource_policy_v1.json"))
        .expect("resource policy contract");
    let mode = if env("TOFU_DEPLOYMENT_MODE", "personal").to_lowercase() == "distributed" {
        "distributed"
    } else {
        "personal"
    };
    let fallback =
        contract["launch_settings"]["TOFU_LOG_TOTAL_BUDGET_MB"]["defaults"][mode].as_u64().expect("log budget default");
    if mode == "distributed" {
        return fallback;
    }
    use std::os::unix::ffi::OsStrExt;
    let Ok(path) = std::ffi::CString::new(data_dir.as_os_str().as_bytes()) else {
        return fallback;
    };
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return fallback;
    }
    let stats = unsafe { stats.assume_init() };
    let free_mib = (stats.f_bavail as u128 * stats.f_frsize as u128) / (1024 * 1024);
    if free_mib < 4096 {
        64
    } else if free_mib < 16 * 1024 {
        128
    } else {
        256
    }
}

impl LogicalOutboxPolicy {
    pub fn from_environment(config: &Config, backend: &BackendCore) -> OpResult<Self> {
        let mode = env("TOFU_STORAGE_LOGICAL_SHADOW", "off").to_lowercase();
        if !["off", "auto", "required"].contains(&mode.as_str()) {
            return Err(StorageError::protocol("TOFU_STORAGE_LOGICAL_SHADOW must be off, auto, or required").into());
        }
        let access_mode = env("TOFU_STORAGE_LOGICAL_ACCESS", "owner").to_lowercase();
        if !["owner", "group"].contains(&access_mode.as_str()) {
            return Err(StorageError::protocol("TOFU_STORAGE_LOGICAL_ACCESS must be owner or group").into());
        }
        let raw = env("TOFU_STORAGE_LOGICAL_SHADOW_DIR", "");
        let root = if raw.is_empty() {
            None
        } else {
            let path = PathBuf::from(raw);
            if !path.is_absolute() {
                return Err(StorageError::protocol("TOFU_STORAGE_LOGICAL_SHADOW_DIR must be absolute").into());
            }
            Some(path)
        };
        let active = backend.metrics().pointer("/fastpath/active").and_then(Value::as_bool).unwrap_or(false);
        let capture_enabled = mode != "off" && (mode != "auto" || root.is_some() || active);
        let reason = if mode == "off" {
            "disabled by TOFU_STORAGE_LOGICAL_SHADOW=off"
        } else if !capture_enabled {
            "auto mode waits for an active measured local write front"
        } else {
            "transactional capture active"
        };
        let publisher_enabled =
            capture_enabled && ["all", "scheduler"].contains(&env("TOFU_PROCESS_ROLE", "all").as_str());
        let max_pending_bytes = log_budget_mib(&config.data_dir) * 1024 * 1024;
        Ok(Self {
            mode,
            capture_enabled,
            publisher_enabled,
            sink_root: if capture_enabled {
                Some(root.unwrap_or_else(|| config.data_dir.join("logical-commits")))
            } else {
                None
            },
            reason: reason.into(),
            max_pending_bytes,
            max_record_bytes: 4 * 1024 * 1024,
            max_segment_bytes: positive("TOFU_STORAGE_LOGICAL_SEGMENT_MIB", 64)?
                .checked_mul(1024 * 1024)
                .ok_or_else(|| StorageError::protocol("logical segment size overflow"))?,
            max_shadow_bytes: max_pending_bytes.saturating_mul(4),
            publish_batch_size: positive("TOFU_STORAGE_LOGICAL_PUBLISH_BATCH", 32)?
                .try_into()
                .map_err(|_| StorageError::protocol("logical publish batch is invalid"))?,
            access_mode,
        })
    }
}

struct State {
    status: Value,
    cipher: Option<crypto::Cipher>,
    stream_id: String,
    stop: bool,
    wake: bool,
    startup_done: bool,
}
pub struct LogicalOutboxPipeline {
    backend: Weak<BackendCore>,
    pub policy: LogicalOutboxPolicy,
    state: Mutex<State>,
    condition: Condvar,
    thread: Mutex<Option<JoinHandle<()>>>,
}
impl LogicalOutboxPipeline {
    pub fn from_config(config: &Config, backend: &Arc<BackendCore>) -> OpResult<Arc<Self>> {
        let policy = LogicalOutboxPolicy::from_environment(config, backend)?;
        let status = json!({"mode":policy.mode,"capture_enabled":policy.capture_enabled,"publisher_enabled":policy.publisher_enabled,"state":if policy.capture_enabled {"starting"} else {"disabled"},"reason":policy.reason,"pending_records":0,"pending_bytes":0,"max_pending_bytes":policy.max_pending_bytes,"last_sequence":0,"published_sequence":0,"captured_total":0,"published_total":0,"duplicate_retries":0,"publish_failures":0,"last_publish_at_ms":0});
        Ok(Arc::new(Self {
            backend: Arc::downgrade(backend),
            policy,
            state: Mutex::new(State {
                status,
                cipher: None,
                stream_id: String::new(),
                stop: false,
                wake: false,
                startup_done: false,
            }),
            condition: Condvar::new(),
            thread: Mutex::new(None),
        }))
    }
    fn backend(&self) -> OpResult<Arc<BackendCore>> {
        self.backend
            .upgrade()
            .ok_or_else(|| StorageError::new("database_unavailable", "Logical outbox backend is closed").into())
    }
    fn command(&self, name: &str, callback: &dyn Fn(&Session<'_>) -> OpResult<Value>) -> OpResult<Value> {
        Ok(self.backend()?.command(
            name,
            &hex::encode(Sha256::digest(name.as_bytes())),
            None,
            callback,
            Instant::now() + Duration::from_secs(10),
            false,
            None,
        )?)
    }
    pub fn start(self: &Arc<Self>) -> OpResult<()> {
        if !self.policy.capture_enabled {
            return Ok(());
        }
        let cipher = crypto::Cipher::load(&self.backend()?.config.data_dir)?;
        let bootstrap = self.command("logical_outbox.bootstrap", &|session| bootstrap(session, &cipher.key_id))?;
        {
            let mut state = self.state.lock().expect("outbox state");
            state.stream_id = bootstrap["stream_id"].as_str().unwrap_or_default().into();
            for (key, value) in bootstrap.as_object().expect("bootstrap") {
                state.status[key] = value.clone();
            }
            state.cipher = Some(cipher);
            state.status["state"] =
                json!(if self.policy.publisher_enabled { "starting-publisher" } else { "capture-only" });
        }
        if !self.policy.publisher_enabled {
            return Ok(());
        }
        let pipeline = Arc::clone(self);
        *self.thread.lock().expect("outbox thread") = Some(
            std::thread::Builder::new()
                .name("storage-logical-publisher".into())
                .spawn(move || pipeline.run())
                .map_err(|_| StorageError::new("database_unavailable", "Logical publisher could not start"))?,
        );
        if self.policy.mode == "required" {
            let state = self.state.lock().expect("outbox state");
            let (state, _) = self
                .condition
                .wait_timeout_while(state, Duration::from_secs(5), |state| !state.startup_done)
                .expect("outbox startup");
            let ready = state.startup_done
                && ["ready", "standby"].contains(&state.status["state"].as_str().unwrap_or_default());
            drop(state);
            if !ready {
                self.close();
                return Err(StorageError::new("database_unavailable", "required logical sink failed preflight").into());
            }
        }
        Ok(())
    }
    pub fn capture_enabled(&self) -> bool {
        self.policy.capture_enabled && !self.state.lock().expect("outbox state").stream_id.is_empty()
    }
    #[allow(clippy::too_many_arguments)]
    pub fn capture(
        &self,
        session: &Session<'_>,
        operation: &str,
        request_id: &str,
        request_digest: &str,
        command_id: Option<&str>,
        payload: &Value,
        response: &Value,
        mutations: &[Value],
    ) -> OpResult<Option<usize>> {
        if !self.capture_enabled() || !crate::receipt::receipt_cacheable(response) {
            return Ok(None);
        }
        let state = self.state.lock().expect("outbox state");
        let cipher = state
            .cipher
            .as_ref()
            .ok_or_else(|| StorageError::new("database_unavailable", "Logical payload cipher is unavailable"))?;
        let command_id = command_id.filter(|id| !id.is_empty() && id.chars().count() <= 200).unwrap_or_default();
        let principal = &payload["principal"];
        let tenant = payload
            .get("tenant_id")
            .or_else(|| principal.get("tenant_id"))
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty() && v.chars().count() <= 128)
            .unwrap_or("system");
        let owner = payload
            .get("owner_user_id")
            .or_else(|| payload.get("user_id"))
            .or_else(|| principal.get("owner_user_id"))
            .and_then(Value::as_i64)
            .filter(|v| *v >= 0)
            .unwrap_or(0);
        let sequence = meta(session, LAST, 0)? + 1;
        let pending = meta(session, BYTES, 0)?;
        let identity = if command_id.is_empty() {
            format!("request\0{operation}\0{request_id}\0{request_digest}")
        } else {
            format!("command\0{operation}\0{command_id}")
        };
        let event = hex::encode(Sha256::digest(identity.as_bytes()));
        let schema = meta(session, "schema_version", 0)?;
        let contract = json!({"encryption_key_id":cipher.key_id,"operation_registry_version":registry_version(),"payload_codec":CODEC,"schema_version":schema});
        let clear = canonical(
            &json!({"binding":{"operation":operation,"request_digest":request_digest,"sequence":sequence,"stream_id":state.stream_id},"contract":contract,"mutations":mutations,"request":payload,"response":response}),
        );
        let encrypted = cipher.seal(std::str::from_utf8(&clear).expect("JSON UTF-8"), tenant, owner, &event)?;
        let committed = now_ms();
        let record = json!({"command_id":if command_id.is_empty(){Value::Null}else{json!(command_id)},"committed_at_ms":committed,"event_id":event,"format":"tofu.logical-commit.v1","operation":operation,"owner_user_id":owner,"payload":{"contract":contract,"ciphertext":encrypted},"request_digest":request_digest,"sequence":sequence,"stream_id":state.stream_id,"tenant_id":tenant});
        let size = canonical(&record).len();
        session.execute("INSERT INTO storage_logical_outbox(sequence, event_id, operation, schema_version, registry_version, request_id, request_digest, command_id, tenant_id, owner_user_id, encryption_key_id, payload_ciphertext, record_bytes, committed_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",&params![sequence,event,operation,schema,registry_version(),request_id,request_digest,command_id,tenant,owner,cipher.key_id.clone(),encrypted,size,committed])?;
        set_meta(session, LAST, sequence)?;
        set_meta(session, BYTES, pending + size as i64)?;
        Ok(Some(size))
    }
    pub fn notify(&self, committed_record_bytes: Option<usize>) {
        let mut state = self.state.lock().expect("outbox state");
        if committed_record_bytes.is_some() {
            increment(&mut state.status, "captured_total");
        }
        state.wake = true;
        self.condition.notify_all();
    }
    pub fn status(&self) -> Value {
        let state = self.state.lock().expect("outbox state");
        let mut status = state.status.clone();
        status["stream_id"] = json!(state.stream_id);
        status["encryption_key_id"] =
            json!(state.cipher.as_ref().map(|cipher| cipher.key_id.as_str()).unwrap_or_default());
        status["sink_kind"] = json!("filesystem");
        status["durability"] = json!(if self.policy.capture_enabled {
            "database-transactional-outbox-then-sink-fsync"
        } else {
            "database-authority-only"
        });
        status
    }
    pub fn health_ready(&self) -> bool {
        let status = self.status();
        self.policy.mode != "required"
            || !self.policy.capture_enabled
            || !["blocked", "poisoned", "degraded"].contains(&status["state"].as_str().unwrap_or_default())
    }
    fn stopped(&self) -> bool {
        self.state.lock().expect("outbox state").stop
    }
    fn wait(&self, duration: Duration) {
        let state = self.state.lock().expect("outbox state");
        let (mut state, _) =
            self.condition.wait_timeout_while(state, duration, |s| !s.stop && !s.wake).expect("outbox wake");
        state.wake = false;
    }
    fn run(&self) {
        let mut sink: Option<shadow::Shadow> = None;
        let mut backoff = Duration::from_millis(250);
        while !self.stopped() {
            let result = (|| -> OpResult<bool> {
                if sink.is_none() {
                    let stream = self.state.lock().expect("outbox state").stream_id.clone();
                    let opened = shadow::Shadow::open(&self.policy, &stream)?;
                    let snapshot = self.backend()?.query(snapshot, Instant::now() + Duration::from_secs(10))?;
                    let shadow_last = opened.next_sequence as i64 - 1;
                    if shadow_last < snapshot["published_sequence"].as_i64().unwrap_or_default() {
                        return Err(integrity("logical sink is behind the acknowledged publish cursor"));
                    }
                    if shadow_last > snapshot["last_sequence"].as_i64().unwrap_or_default() {
                        return Err(integrity("logical sink is ahead of the database stream"));
                    }
                    {
                        let mut state = self.state.lock().expect("outbox state");
                        for (key, value) in snapshot.as_object().expect("snapshot") {
                            state.status[key] = value.clone();
                        }
                        state.status["state"] = json!("ready");
                        state.status["reason"] = json!("publisher owns the durable sink");
                        state.status["sink"] = opened.status();
                        state.startup_done = true;
                        self.condition.notify_all();
                    }
                    sink = Some(opened);
                }
                let rows=self.backend()?.query(|session|{
                    let rows=session.fetch_all("SELECT sequence, event_id, operation, request_id, request_digest, schema_version, registry_version, command_id, tenant_id, owner_user_id, encryption_key_id, payload_ciphertext, record_bytes, committed_at_ms FROM storage_logical_outbox ORDER BY sequence LIMIT ?",&params![self.policy.publish_batch_size])?;
                    let snapshot=snapshot(session)?;
                    let mut state=self.state.lock().expect("outbox state");
                    for(key,value)in snapshot.as_object().expect("snapshot"){state.status[key]=value.clone();}
                    Ok(rows)
                },Instant::now()+Duration::from_secs(10))?;
                let empty = rows.is_empty();
                for row in rows {
                    if self.stopped() {
                        break;
                    }
                    let stream = self.state.lock().expect("outbox state").stream_id.clone();
                    let record = record(&row, &stream)?;
                    let duplicate = sink.as_mut().expect("sink").append(&record)?;
                    let ack = self.command("logical_outbox.ack", &|session| acknowledge(session, &row))?;
                    let mut state = self.state.lock().expect("outbox state");
                    state.status["published_sequence"] = ack["published_sequence"].clone();
                    state.status["pending_bytes"] = ack["pending_bytes"].clone();
                    let pending = state.status["pending_records"].as_u64().unwrap_or_default().saturating_sub(1);
                    state.status["pending_records"] = json!(pending);
                    increment(&mut state.status, "published_total");
                    if duplicate {
                        increment(&mut state.status, "duplicate_retries");
                    }
                    state.status["last_publish_at_ms"] = json!(now_ms());
                    state.status["sink"] = sink.as_ref().expect("sink").status();
                    state.status["state"] = json!("ready");
                }
                Ok(empty)
            })();
            match result {
                Ok(empty) => {
                    backoff = Duration::from_millis(250);
                    if empty {
                        self.wait(Duration::from_secs(1));
                    }
                }
                Err(error) => {
                    let error = map_failure(error, false);
                    sink = None;
                    {
                        let mut state = self.state.lock().expect("outbox state");
                        let kind = if error.code == "database_busy" {
                            "standby"
                        } else if error.code == "database_integrity" {
                            "poisoned"
                        } else {
                            "degraded"
                        };
                        state.status["state"] = json!(kind);
                        state.status["reason"] = json!(if kind == "standby" {
                            "another publisher owns the sink writer lock"
                        } else if kind == "poisoned" {
                            "logical sink continuity validation failed"
                        } else {
                            "publisher retrying after I/O failure"
                        });
                        if kind != "standby" {
                            increment(&mut state.status, "publish_failures");
                        }
                        state.startup_done = true;
                        self.condition.notify_all();
                    }
                    self.wait(if error.code == "database_integrity" { Duration::from_secs(30) } else { backoff });
                    backoff = (backoff * 2).min(Duration::from_secs(30));
                }
            }
        }
        drop(sink);
    }
    pub fn close(&self) {
        {
            let mut state = self.state.lock().expect("outbox state");
            state.stop = true;
            state.wake = true;
            self.condition.notify_all();
        }
        if let Some(thread) = self.thread.lock().expect("outbox thread").take() {
            let _ = thread.join();
        }
        self.state.lock().expect("outbox state").status["state"] = json!("closed");
    }
}
fn increment(status: &mut Value, key: &str) {
    status[key] = json!(status[key].as_u64().unwrap_or_default() + 1);
}
fn snapshot(session: &Session<'_>) -> OpResult<Value> {
    let row=session.fetch_one("SELECT COUNT(*) AS pending_records, COALESCE(SUM(record_bytes), 0) AS pending_bytes FROM storage_logical_outbox",&params![])?.ok_or_else(||integrity("Logical outbox aggregate is missing"))?;
    Ok(
        json!({"pending_records":row.int("pending_records")?,"pending_bytes":row.int("pending_bytes")?,"last_sequence":meta(session,LAST,0)?,"published_sequence":meta(session,PUBLISHED,0)?}),
    )
}
fn bootstrap(session: &Session<'_>, key: &str) -> OpResult<Value> {
    let stored = session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params![KEY])?;
    if let Some(stored) = stored {
        if stored.text("meta_value") != key {
            return Err(integrity("Logical encryption key changed without a replay keyring migration"));
        }
    } else {
        let pending=session.fetch_one("SELECT MIN(encryption_key_id) AS first_key, MAX(encryption_key_id) AS last_key FROM storage_logical_outbox",&params![])?.expect("aggregate");
        let first = pending.text("first_key");
        if !first.is_empty() && (first != key || pending.text("last_key") != first) {
            return Err(integrity("Logical outbox contains a different encryption key identity"));
        }
        set_meta(session, KEY, key)?;
    }
    let stream = if let Some(row) =
        session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params![STREAM])?
    {
        row.text("meta_value")
    } else {
        let id = session
            .fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params!["authority_uuid"])?
            .map(|r| r.text("meta_value"))
            .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
        set_meta(session, STREAM, &id)?;
        id
    };
    if stream.is_empty() || stream.chars().count() > 128 {
        return Err(integrity("Logical outbox stream identity is invalid"));
    }
    let row=session.fetch_one("SELECT COALESCE(MIN(sequence), 0) AS first_sequence, COALESCE(MAX(sequence), 0) AS last_pending_sequence, COALESCE(SUM(record_bytes), 0) AS pending_bytes, COUNT(*) AS pending_records FROM storage_logical_outbox",&params![])?.expect("aggregate");
    let first = row.int("first_sequence")?;
    let last_pending = row.int("last_pending_sequence")?;
    let count = row.int("pending_records")?;
    let bytes = row.int("pending_bytes")?;
    let last = meta(session, LAST, last_pending)?.max(last_pending);
    let published = meta(session, PUBLISHED, if first > 0 { first - 1 } else { last })?;
    if published > last {
        return Err(integrity("Logical outbox cursor exceeds its sequence"));
    }
    if count > 0 && (last_pending - first + 1 != count || first != published + 1 || last_pending != last) {
        return Err(integrity("Logical outbox pending sequence has a gap"));
    }
    if first == 0 && published != last {
        return Err(integrity("Logical outbox acknowledged rows are missing"));
    }
    set_meta(session, LAST, last)?;
    set_meta(session, PUBLISHED, published)?;
    set_meta(session, BYTES, bytes)?;
    Ok(
        json!({"stream_id":stream,"first_pending_sequence":first,"last_sequence":last,"published_sequence":published,"pending_bytes":bytes,"pending_records":count}),
    )
}
fn record(row: &Row, stream: &str) -> OpResult<Value> {
    let command = row.text("command_id");
    Ok(
        json!({"command_id":if command.is_empty(){Value::Null}else{json!(command)},"committed_at_ms":row.int("committed_at_ms")?,"event_id":row.text("event_id"),"format":"tofu.logical-commit.v1","operation":row.text("operation"),"owner_user_id":row.int("owner_user_id")?,"payload":{"contract":{"encryption_key_id":row.text("encryption_key_id"),"operation_registry_version":row.int("registry_version")?,"payload_codec":CODEC,"schema_version":row.int("schema_version")?},"ciphertext":row.text("payload_ciphertext")},"request_digest":row.text("request_digest"),"sequence":row.int("sequence")?,"stream_id":stream,"tenant_id":row.text("tenant_id")}),
    )
}
fn acknowledge(session: &Session<'_>, record: &Row) -> OpResult<Value> {
    let sequence = record.int("sequence")?;
    let event = record.text("event_id");
    let published = meta(session, PUBLISHED, 0)?;
    let row = session.fetch_one(
        "SELECT event_id, record_bytes FROM storage_logical_outbox WHERE sequence = ?",
        &params![sequence],
    )?;
    let Some(row) = row else {
        if published >= sequence {
            return Ok(json!({"published_sequence":published,"pending_bytes":meta(session,BYTES,0)?}));
        }
        return Err(integrity("Logical outbox row disappeared before publish"));
    };
    if row.text("event_id") != event {
        return Err(integrity("Logical outbox event identity changed"));
    }
    if sequence != published + 1 {
        return Err(integrity("Logical outbox acknowledgement is out of order"));
    }
    if session
        .execute("DELETE FROM storage_logical_outbox WHERE sequence = ? AND event_id = ?", &params![sequence, event])?
        != 1
    {
        return Err(integrity("Logical outbox acknowledgement lost its row"));
    }
    let bytes = (meta(session, BYTES, 0)? - row.int("record_bytes")?).max(0);
    set_meta(session, PUBLISHED, sequence)?;
    set_meta(session, BYTES, bytes)?;
    Ok(json!({"published_sequence":sequence,"pending_bytes":bytes}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn setup() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY,meta_value TEXT NOT NULL); CREATE TABLE storage_logical_outbox(sequence INTEGER PRIMARY KEY,event_id TEXT UNIQUE,operation TEXT,schema_version INTEGER,registry_version INTEGER,request_id TEXT,request_digest TEXT,command_id TEXT,tenant_id TEXT,owner_user_id INTEGER,encryption_key_id TEXT,payload_ciphertext TEXT,record_bytes INTEGER,committed_at_ms INTEGER); CREATE TABLE business(value TEXT);").unwrap();
        connection
    }
    fn pipeline(path: &std::path::Path) -> LogicalOutboxPipeline {
        LogicalOutboxPipeline {
            backend: Weak::new(),
            policy: LogicalOutboxPolicy {
                mode: "required".into(),
                capture_enabled: true,
                publisher_enabled: false,
                sink_root: None,
                reason: "test".into(),
                max_pending_bytes: 1,
                max_record_bytes: 1,
                max_segment_bytes: 1,
                max_shadow_bytes: 1,
                publish_batch_size: 1,
                access_mode: "owner".into(),
            },
            state: Mutex::new(State {
                status: json!({}),
                cipher: Some(crypto::Cipher::load(path).unwrap()),
                stream_id: "stream".into(),
                stop: false,
                wake: false,
                startup_done: false,
            }),
            condition: Condvar::new(),
            thread: Mutex::new(None),
        }
    }
    #[test]
    fn business_and_outbox_rollback_share_one_transaction() {
        let temp = tempfile::tempdir().unwrap();
        let pipeline = pipeline(temp.path());
        let connection = setup();
        let session = Session::new(&connection, "epoch", None, true);
        let key = pipeline.state.lock().unwrap().cipher.as_ref().unwrap().key_id.clone();
        bootstrap(&session, &key).unwrap();
        connection.execute_batch("BEGIN IMMEDIATE").unwrap();
        session.execute("INSERT INTO business(value) VALUES (?)", &params!["private"]).unwrap();
        let bytes = pipeline
            .capture(
                &session,
                "record.put",
                "request",
                &"a".repeat(64),
                Some("command"),
                &json!({"value":"private"}),
                &json!({"ok":true}),
                &[json!({"sql":"INSERT INTO business(value) VALUES (?)","params":["private"],"rowcount":1})],
            )
            .unwrap();
        assert!(bytes.unwrap() > 1);
        assert_eq!(meta(&session, LAST, 0).unwrap(), 1);
        connection.execute_batch("ROLLBACK").unwrap();
        assert_eq!(meta(&session, LAST, 0).unwrap(), 0);
        assert_eq!(
            session.fetch_one("SELECT COUNT(*) AS n FROM business", &params![]).unwrap().unwrap().int("n").unwrap(),
            0
        );
        assert_eq!(snapshot(&session).unwrap()["pending_records"], 0);
    }
    #[test]
    fn capture_preserves_complete_large_payload_without_capacity_refusal() {
        let temp = tempfile::tempdir().unwrap();
        let pipeline = pipeline(temp.path());
        let connection = setup();
        let session = Session::new(&connection, "epoch", None, true);
        let key = pipeline.state.lock().unwrap().cipher.as_ref().unwrap().key_id.clone();
        bootstrap(&session, &key).unwrap();
        let request = json!({"tenant_id":"tenant","owner_user_id":5,"value":"sensitive".repeat(1000)});
        pipeline
            .capture(&session, "record.put", "request", &"a".repeat(64), Some("command"), &request, &request, &[])
            .unwrap();
        let row = session.fetch_one("SELECT * FROM storage_logical_outbox", &params![]).unwrap().unwrap();
        assert_eq!(row.text("tenant_id"), "tenant");
        assert_eq!(row.int("owner_user_id").unwrap(), 5);
        assert!(row.text("payload_ciphertext").len() > 8000);
        assert!(!row.text("payload_ciphertext").contains("sensitive"));
        assert_eq!(meta(&session, BYTES, 0).unwrap(), row.int("record_bytes").unwrap());
        assert_eq!(canonical(&record(&row, "stream").unwrap()).len() as i64, row.int("record_bytes").unwrap());
    }
    #[test]
    fn bootstrap_rejects_key_rotation_and_acknowledged_row_loss() {
        let connection = setup();
        let session = Session::new(&connection, "epoch", None, true);
        bootstrap(&session, "0123456789abcdef").unwrap();
        assert_eq!(map_failure(bootstrap(&session, "fedcba9876543210").unwrap_err(), false).code, "database_integrity");
        set_meta(&session, LAST, 2).unwrap();
        assert!(bootstrap(&session, "0123456789abcdef").is_err());
    }
    #[test]
    fn acknowledgement_is_ordered_identity_fenced_and_idempotent() {
        let temp = tempfile::tempdir().unwrap();
        let pipeline = pipeline(temp.path());
        let connection = setup();
        let session = Session::new(&connection, "epoch", None, true);
        let key = pipeline.state.lock().unwrap().cipher.as_ref().unwrap().key_id.clone();
        bootstrap(&session, &key).unwrap();
        for id in ["one", "two"] {
            pipeline
                .capture(&session, "record.put", id, &"a".repeat(64), Some(id), &json!({}), &json!({}), &[])
                .unwrap();
        }
        let rows = session.fetch_all("SELECT * FROM storage_logical_outbox ORDER BY sequence", &params![]).unwrap();
        assert!(acknowledge(&session, &rows[1]).is_err());
        assert_eq!(acknowledge(&session, &rows[0]).unwrap()["published_sequence"], 1);
        assert_eq!(acknowledge(&session, &rows[0]).unwrap()["published_sequence"], 1);
        assert_eq!(acknowledge(&session, &rows[1]).unwrap()["pending_bytes"], 0);
        assert_eq!(snapshot(&session).unwrap()["pending_records"], 0);
    }
    #[test]
    fn canonical_objects_sort_recursively_and_preserve_unicode() {
        assert_eq!(
            canonical(&json!({"z":[{"b":1,"a":"中文"}],"a":true})),
            "{\"a\":true,\"z\":[{\"a\":\"中文\",\"b\":1}]}".as_bytes()
        );
    }
}

#[cfg(test)]
mod policy_tests {
    use super::*;
    #[test]
    fn legacy_budget_is_observational_and_comes_from_retention_contract() {
        let directory = tempfile::tempdir().unwrap();
        assert!([64, 128, 256, 512].contains(&log_budget_mib(directory.path())));
    }
}
