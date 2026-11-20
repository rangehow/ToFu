//! The lifecycle state machine (`server_manager.LifecycleManager`).
//!
//! One project's desired/observed server state, its worker generation, the
//! resident storage and execution hosts, and the restart commit point:
//! every refusal is decided while the old worker serves; once it stopped,
//! the manager drives the replacement to a serving worker, charging failures
//! to the windowed failure budget (docs/modules/infra_runtime.md, "A restart
//! that stopped its old server always ends serving").
//!
//! The state file `data/server-manager-state.json` keeps the Python schema
//! (version 1) so either manager can take over the other's record.

use crate::bridge::{Bridge, Composition};
use crate::datapaths;
use crate::envpolicy::{self, Env};
use crate::json::{self, read_object, Object, ObjectExt};
use crate::lockstatus::{self, port_accepts};
use crate::preparation::{self, Parts};
use crate::probe;
use crate::procfs::{self, hostname};
use crate::resident::{self, Kind, ResidentService, ServiceError, NATIVE_SERVICE_STOP_SECONDS};
use crate::timing::{now, sleep, Deadline};
use crate::{certification, mlog};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

pub const STATE_VERSION: i64 = 1;
pub const MANAGER_STATE_FILE: &str = "server-manager-state.json";
const DEFAULT_MONITOR_INTERVAL: f64 = 5.0;
const DEFAULT_BOOT_GRACE: f64 = 180.0;
const DEFAULT_WEDGE_STALE: f64 = 180.0;
const DEFAULT_WEDGE_STREAK: f64 = 120.0;
const DEFAULT_MAX_FAILURES: i64 = 5;
const DEFAULT_FAILURE_WINDOW: f64 = 120.0;
const DEFAULT_FAILURE_HISTORY_KEEP: usize = 50;
const DEFAULT_SIDECAR_LEASE_RELEASE_WAIT: f64 = 10.0;
const DEFAULT_ORPHAN_SIDECAR_REAP_WAIT: f64 = 15.0;
const DEFAULT_STORAGE_LEASE_POLL_INTERVAL: f64 = 0.1;
const DEFAULT_WORKER_SHUTDOWN_HARD_SECS: f64 = 30.0;
const WORKER_TERMINATE_TIMEOUT_MARGIN: f64 = 5.0;
const DEFAULT_EXIT_INTENT_WINDOW: f64 = 120.0;
const DEFAULT_BOOT_HEARTBEAT_GRACE: f64 = 180.0;
const LOG_MAINTENANCE_SECONDS: f64 = 900.0;
const CLEAN_WORKER_EXIT_REASONS: [&str; 4] = ["manual", "signal", "restart", "memory_recycle"];
const RESTART_IN_PROGRESS: [&str; 3] = ["started", "retrying", "deferred"];
const WORKER_BUDGET_STATUS_KEYS: [&str; 2] = ["TOFU_MAX_INFLIGHT_TASKS", "TOFU_AGENT_WORKERS"];
const RESOURCE_BUDGET_POLICY_ENV: &str = "TOFU_RESOURCE_BUDGET_POLICY_VERSION";
const RESOURCE_BUDGET_AUTOMATIC_ENV: &str = "TOFU_RESOURCE_BUDGET_AUTOMATIC_DEFAULTS";
const RETAINED_ENGINE_ENVIRONMENT: &str = "TOFU_STORAGE_RETAINED_ENGINE_SHA256";
const SOURCE_CHANGED_BEFORE_BOOT: &str = "source_changed_before_boot";
const CGROUP_OOM_EVENT_PATHS: [&str; 2] = [
    "/sys/fs/cgroup/memory.events",
    "/sys/fs/cgroup/memory/memory.oom_control",
];

/// Children present when this image started belong to an earlier image
/// (`/reload` re-execs in place); no `Child` here owns their exit.
static INHERITED_CHILDREN: Mutex<Vec<(i64, i64)>> = Mutex::new(Vec::new());

pub fn capture_inherited_children() {
    let mut found = Vec::new();
    if let Ok(threads) = std::fs::read_dir("/proc/self/task") {
        for thread in threads.flatten() {
            let Ok(text) = std::fs::read_to_string(thread.path().join("children")) else {
                continue;
            };
            for child in text
                .split_whitespace()
                .filter_map(|pid| pid.parse::<i64>().ok())
            {
                if let Some(ticks) = procfs::start_ticks(child) {
                    found.push((child, ticks));
                }
            }
        }
    }
    *INHERITED_CHILDREN.lock().unwrap() = found;
}

fn reap_children_of_previous_image(pending_worker: Option<i64>) {
    let mut inherited = INHERITED_CHILDREN.lock().unwrap();
    let me = std::process::id().to_string();
    inherited.retain(|(pid, ticks)| {
        let Some(fields) = procfs::stat_fields(*pid) else { return false };
        if fields[1] != me || fields[19] != ticks.to_string() {
            return false;
        }
        if fields[0] == "Z" && Some(*pid) != pending_worker {
            if let Some(status) = procfs::reap_exited_child(*pid, *ticks) {
                mlog!(Info, "reaped child pid={pid} inherited from an earlier Supervisor image (wait status {status})");
                return false;
            }
        }
        true
    });
}

fn cgroup_oom_kill_count() -> Option<i64> {
    for path in CGROUP_OOM_EVENT_PATHS {
        let Ok(text) = std::fs::read_to_string(path) else {
            continue;
        };
        for line in text.lines() {
            let fields: Vec<&str> = line.split_whitespace().collect();
            if fields.len() >= 2 && fields[0] == "oom_kill" {
                return fields[1].parse().ok();
            }
        }
    }
    None
}

fn boot_heartbeat_grace_seconds() -> f64 {
    std::env::var("TOFU_BOOT_HEARTBEAT_GRACE_SECS")
        .ok()
        .and_then(|raw| raw.trim().parse::<f64>().ok())
        .unwrap_or(DEFAULT_BOOT_HEARTBEAT_GRACE)
        .clamp(60.0, 900.0)
}

fn env_float(name: &str) -> Option<f64> {
    std::env::var(name)
        .ok()
        .and_then(|raw| raw.trim().parse::<f64>().ok())
}

fn value_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn env_from_value(value: Option<&Value>) -> Env {
    value
        .and_then(Value::as_object)
        .map(|map| {
            map.iter()
                .map(|(key, value)| (key.clone(), value_text(value)))
                .collect()
        })
        .unwrap_or_default()
}

fn env_value(env: &Env) -> Value {
    json!(env)
}

fn merge(mut base: Object, extra: Object) -> Object {
    for (key, value) in extra {
        base.insert(key, value);
    }
    base
}

fn obj(value: Value) -> Object {
    match value {
        Value::Object(map) => map,
        _ => Object::new(),
    }
}

fn int_of(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|f| f as i64)),
        _ => None,
    }
}

/// A prepared launch carried across the commit point.
#[derive(Clone, Debug)]
pub struct Prepared {
    pub environment: Env,
    pub storage_plan: Option<Value>,
    pub execution_plan: Option<Value>,
}

enum SpawnError {
    StorageLeaseStillHeld(Object),
    Failed(String),
}

pub struct Inner {
    pub state: Object,
    had_state: bool,
    state_needs_save: bool,
    project_env: Env,
    worker_process: Option<Child>,
    replacement_inputs: Option<Prepared>,
    trace_requested_at: Option<f64>,
    last_monitor_error: String,
    monitor_error_count: u64,
    last_log_maintenance: Option<Instant>,
    head_reflog: Option<PathBuf>,
    head_reflog_seen: Option<(u64, i64)>,
    /// Replica generation and bytecode namespace leases of the current worker.
    worker_leases: Vec<std::os::fd::OwnedFd>,
    /// `(root, pythons)` to build once the worker is ready.
    pending_runtime_build: Option<(String, Vec<String>)>,
    last_bytecode_sweep_request: Option<Instant>,
}

pub struct LifecycleManager {
    pub project: PathBuf,
    pub python: String,
    pub data_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub state_path: PathBuf,
    pub worker_log: PathBuf,
    pub bridge: Arc<Bridge>,
    pub storage: ResidentService,
    pub execution: ResidentService,
    server_env_keys: BTreeSet<String>,
    resource_budget_keys: BTreeSet<String>,
    resource_budget_policy_version: String,
    bytecode_sweep_interval: f64,
    bytecode_sweep_marker: String,
    pub monitor_interval: f64,
    pub failure_window: f64,
    pub max_failures: i64,
    inner: Mutex<Inner>,
    closed: AtomicBool,
    precertifying: Arc<AtomicBool>,
}

impl LifecycleManager {
    pub fn new(project: &Path, python: &str, bridge: Arc<Bridge>) -> Result<Arc<Self>, String> {
        let project =
            std::fs::canonicalize(project).map_err(|e| format!("{}: {e}", project.display()))?;
        let python = crate::runtime::authoritative_interpreter(python);
        let python = python.as_str();
        let described = bridge.call(json!({"operation": "describe"}))?;
        let keys = |name: &str| -> BTreeSet<String> {
            described
                .get(name)
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| item.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default()
        };
        let monitor_interval = env_float("TOFU_MANAGER_INTERVAL")
            .unwrap_or(DEFAULT_MONITOR_INTERVAL)
            .max(0.2);
        let failure_window = env_float("TOFU_MANAGER_FAILURE_WINDOW_SECS")
            .unwrap_or(DEFAULT_FAILURE_WINDOW)
            .clamp(30.0, 3600.0);
        let max_failures = std::env::var("TOFU_MANAGER_MAX_FAILURES")
            .ok()
            .and_then(|raw| raw.trim().parse::<i64>().ok())
            .unwrap_or(DEFAULT_MAX_FAILURES)
            .clamp(2, 100);
        let manager = LifecycleManager {
            data_dir: project.join("data"),
            logs_dir: project.join("logs"),
            state_path: project.join("data").join(MANAGER_STATE_FILE),
            worker_log: project.join("logs/server-console.log"),
            storage: ResidentService::new(Kind::Storage, &project, python),
            execution: ResidentService::new(Kind::Execution, &project, python),
            python: python.to_owned(),
            bridge,
            server_env_keys: keys("serverEnvKeys"),
            resource_budget_keys: keys("resourceBudgetKeys"),
            resource_budget_policy_version: described
                .str_of("resourceBudgetPolicyVersion")
                .to_owned(),
            bytecode_sweep_interval: described
                .get("bytecodeSweepIntervalSeconds")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
            bytecode_sweep_marker: described.str_of("bytecodeSweepMarkerName").to_owned(),
            monitor_interval,
            failure_window,
            max_failures,
            inner: Mutex::new(Inner {
                state: Object::new(),
                had_state: false,
                state_needs_save: false,
                project_env: Env::new(),
                worker_process: None,
                replacement_inputs: None,
                trace_requested_at: None,
                last_monitor_error: String::new(),
                monitor_error_count: 0,
                last_log_maintenance: None,
                head_reflog: None,
                head_reflog_seen: None,
                worker_leases: Vec::new(),
                pending_runtime_build: None,
                last_bytecode_sweep_request: None,
            }),
            closed: AtomicBool::new(false),
            precertifying: Arc::new(AtomicBool::new(false)),
            project,
        };
        {
            let mut s = manager.lock();
            s.project_env = manager.project_server_env();
            let loaded = read_object(&manager.state_path);
            s.had_state = loaded.i64_of("version") == Some(STATE_VERSION);
            s.state = manager.load_state(&mut s, loaded);
            if s.state_needs_save {
                manager.save(&mut s)?;
            }
        }
        manager.adopt_existing();
        {
            let mut s = manager.lock();
            manager.restore_worker_leases(&mut s);
        }
        Ok(Arc::new(manager))
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn read_project_dotenv(&self) -> Env {
        match envpolicy::read_dotenv(&self.project.join(".env")) {
            Ok(values) => values,
            Err(error) => {
                mlog!(Warning, "project .env not applied; inherited Supervisor values remain in effect: {error}");
                Env::new()
            }
        }
    }

    /// `project_server_env`: the lifecycle-allowlisted values of `.env`.
    pub fn project_server_env(&self) -> Env {
        self.read_project_dotenv()
            .into_iter()
            .filter(|(key, _)| self.server_env_keys.contains(key))
            .collect()
    }

    fn port_of(state: &Object) -> u16 {
        int_of(state.get("port"))
            .filter(|port| (1..=65535).contains(port))
            .map(|port| port as u16)
            .unwrap_or(envpolicy::DEFAULT_SERVER_PORT)
    }

    pub fn port(&self, s: &Inner) -> u16 {
        Self::port_of(&s.state)
    }

    fn effective_env(&self, overlay: &Env) -> Env {
        let mut env = envpolicy::process_environment();
        env.extend(overlay.iter().map(|(k, v)| (k.clone(), v.clone())));
        env
    }

    fn default_state(&self, s: &Inner) -> Object {
        let default_env = s.project_env.clone();
        let port = envpolicy::server_port(&[], &self.effective_env(&default_env));
        let stamp = now();
        obj(json!({
            "version": STATE_VERSION, "projectPath": self.project.to_string_lossy(),
            "desired": "stopped", "observed": "stopped", "worker": {}, "serverArgs": [],
            "serverEnv": default_env, "serverEnvOverrides": {}, "port": port, "restartCount": 0,
            "workerFailureCount": 0, "executionRecovery": {}, "plannedExitCount": 0,
            "exitAccountingSince": stamp, "lastWorkerExitAt": 0.0, "lastWorkerExitKind": "",
            "lastWorkerExitReason": "", "lastConsumedExitMarker": "", "pendingRecoverySource": "",
            "pendingWorkerExitIntent": {}, "consecutiveFailures": 0, "failureHistory": [],
            "activeFailureAt": 0.0, "lastFailureAt": 0.0, "lastFailureReason": "", "lastExitCause": "",
            "lastCgroupOomDelta": 0, "lastCgroupOomKillCount": null, "workerBytecodeCache": {},
            "workerRuntime": {}, "runtimeSuspect": {}, "lastRecoveredAt": 0.0, "lastRecoverySeconds": null,
            "storageBlocker": {}, "storageRetention": {}, "knownGood": {}, "storageRecoveries": [],
            "lastPortReconciledAt": 0.0, "lastPortReconciledFrom": null, "lastPortReconciledSource": "",
            "nextRetryAt": 0.0, "wedgeSince": 0.0, "unreadySince": 0.0, "lastError": "",
            "environmentQuarantine": {}, "lastTransitionAt": stamp, "launchSource": "", "updatedAt": stamp,
        }))
    }

    fn load_state(&self, s: &mut Inner, loaded: Object) -> Object {
        let mut state = self.default_state(s);
        let current = loaded.i64_of("version") == Some(STATE_VERSION);
        if current {
            for (key, value) in loaded.iter() {
                state.insert(key.clone(), value.clone());
            }
        }
        state.insert("projectPath".into(), json!(self.project.to_string_lossy()));
        if !matches!(state.str_of("desired"), "running" | "stopped") {
            state.insert("desired".into(), json!("stopped"));
        }
        if !state
            .get("serverArgs")
            .map(Value::is_array)
            .unwrap_or(false)
        {
            state.insert("serverArgs".into(), json!([]));
        }
        let raw_env = state
            .get("serverEnv")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let normalized: Env = raw_env
            .iter()
            .filter(|(key, value)| {
                self.server_env_keys.contains(*key)
                    && !value.is_null()
                    && !value_text(value).is_empty()
            })
            .map(|(key, value)| (key.clone(), value_text(value)))
            .collect();
        if Value::Object(raw_env.clone()) != env_value(&normalized) {
            s.state_needs_save = true;
        }
        state.insert("serverEnv".into(), env_value(&normalized));
        let overrides: Env = match loaded.get("serverEnvOverrides") {
            Some(Value::Object(_)) if current => env_from_value(loaded.get("serverEnvOverrides")),
            _ => envpolicy::operator_overrides(&normalized, &s.project_env),
        };
        let overrides: Env = overrides
            .into_iter()
            .filter(|(key, value)| normalized.get(key) == Some(value))
            .collect();
        state.insert("serverEnvOverrides".into(), env_value(&overrides));
        for key in ["failureHistory", "storageRecoveries"] {
            if !state.get(key).map(Value::is_array).unwrap_or(false) {
                state.insert(key.into(), json!([]));
            }
        }
        for key in [
            "storageBlocker",
            "storageRetention",
            "knownGood",
            "workerBytecodeCache",
            "workerRuntime",
            "runtimeSuspect",
            "pendingWorkerExitIntent",
            "environmentQuarantine",
        ] {
            if !state.get(key).map(Value::is_object).unwrap_or(false) {
                state.insert(key.into(), json!({}));
            }
        }
        if !state
            .get("pendingRecoverySource")
            .map(Value::is_string)
            .unwrap_or(false)
        {
            state.insert("pendingRecoverySource".into(), json!(""));
        }
        let project_error = envpolicy::production_environment_error(&self.project, &s.project_env);
        let persisted_error = envpolicy::production_environment_error(&self.project, &normalized);
        let error = if project_error.is_empty() {
            persisted_error
        } else {
            project_error.clone()
        };
        if !error.is_empty() {
            let discarded: Vec<String> = normalized.keys().cloned().collect();
            state.insert("serverArgs".into(), json!([]));
            if !project_error.is_empty() {
                state.insert("desired".into(), json!("stopped"));
                state.insert("serverEnv".into(), json!({}));
            } else {
                state.insert("serverEnv".into(), env_value(&s.project_env));
            }
            state.insert("serverEnvOverrides".into(), json!({}));
            let env = self.effective_env(&env_from_value(state.get("serverEnv")));
            state.insert("port".into(), json!(envpolicy::server_port(&[], &env)));
            state.insert(
                "environmentQuarantine".into(),
                json!({"detectedAt": now(), "reason": error, "discardedKeys": discarded}),
            );
            let observed = if state.str_of("desired") == "running" {
                "degraded"
            } else {
                "stopped"
            };
            state.insert("observed".into(), json!(observed));
            state.insert("lastError".into(), json!(error));
            state.insert("lastTransitionAt".into(), json!(now()));
            s.state_needs_save = true;
            mlog!(
                Error,
                "{error}; persisted worker environment was quarantined"
            );
        }
        state
    }

    fn save(&self, s: &mut Inner) -> Result<(), String> {
        s.state.insert("updatedAt".into(), json!(now()));
        json::write_atomic(&self.state_path, &Value::Object(s.state.clone()))
            .map_err(|error| error.to_string())
    }

    fn save_logged(&self, s: &mut Inner) {
        if let Err(error) = self.save(s) {
            mlog!(Error, "could not persist the manager state to {}: {error}; recovery continues from memory", self.state_path.display());
        }
    }

    /// Persist a failure outcome; the in-memory state still drives recovery if it cannot.
    fn save_failure_record(&self, s: &mut Inner, record: &str) {
        if let Err(error) = self.save(s) {
            mlog!(
                Error,
                "could not persist the {record} to {}: {error}; recovery continues from memory",
                self.state_path.display()
            );
        }
    }

    fn manager_values(&self, port: u16) -> Env {
        [
            ("TOFU_SERVER_WORKER", "1".to_owned()),
            ("TOFU_MANAGED_BY", "supervisor".to_owned()),
            (
                "TOFU_EXTERNAL_CONSOLE_LOG",
                self.worker_log.to_string_lossy().into_owned(),
            ),
            ("TOFU_EXTERNAL_CONSOLE_STREAM", "server_console".to_owned()),
            (
                "TOFU_PROJECT_PATH",
                self.project.to_string_lossy().into_owned(),
            ),
            ("PORT", port.to_string()),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect()
    }

    fn composition(&self, server_env: &Env, port: u16) -> Result<Composition, String> {
        self.bridge
            .compose(&self.project, server_env, &self.manager_values(port))
    }

    /// `_worker_environment`: the same launch environment for preparation and spawning.
    fn worker_environment(&self, server_env: &Env, port: u16) -> Result<Env, String> {
        self.composition(server_env, port)
            .map(|composition| composition.environment)
    }

    fn server_env(s: &Inner) -> Env {
        env_from_value(s.state.get("serverEnv"))
    }

    fn worker_data_directory(&self, s: &Inner) -> PathBuf {
        let dotenv = envpolicy::read_dotenv(&self.project.join(".env")).unwrap_or_default();
        datapaths::worker_data_directory(&self.project, &dotenv, &Self::server_env(s))
    }

    fn storage_data_dir(&self, s: &Inner) -> PathBuf {
        datapaths::storage_data_directory(&self.project, &Self::server_env(s))
    }

    fn lock_status(&self, s: &Inner) -> Object {
        lockstatus::read_lock_status(&self.project, &self.worker_data_directory(s))
    }

    fn set_observed(s: &mut Inner, observed: &str, error: &str) {
        if s.state.str_of("observed") != observed {
            s.state.insert("observed".into(), json!(observed));
            s.state.insert("lastTransitionAt".into(), json!(now()));
        }
        if !error.is_empty() {
            s.state.insert("lastError".into(), json!(error));
        }
    }

    fn worker(s: &Inner) -> Object {
        s.state.obj_of("worker")
    }

    fn trace_total_suffix(s: &Inner) -> String {
        match s.trace_requested_at {
            Some(requested) if requested > 0.0 => {
                format!(" totalSinceRequest={:.1}s", now() - requested)
            }
            _ => String::new(),
        }
    }

    fn recent_failure_times(&self, s: &Inner, at: f64) -> Vec<f64> {
        let cutoff = at - self.failure_window;
        let mut values: Vec<f64> = s
            .state
            .get("failureHistory")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| match item {
                        Value::Number(n) => n.as_f64(),
                        Value::String(t) => t.parse().ok(),
                        _ => None,
                    })
                    .filter(|value| cutoff <= *value && *value <= at + 1.0)
                    .collect()
            })
            .unwrap_or_default();
        values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let keep = values.len().saturating_sub(DEFAULT_FAILURE_HISTORY_KEEP);
        values.split_off(keep)
    }

    /// Clear crash-loop gating only after an explicit human start/restart.
    fn clear_failure_budget(s: &mut Inner) {
        for (key, value) in [
            ("consecutiveFailures", json!(0)),
            ("failureHistory", json!([])),
            ("activeFailureAt", json!(0.0)),
            ("nextRetryAt", json!(0.0)),
            ("executionRecovery", json!({})),
            ("replacementRetry", json!({})),
            ("storageRecoveries", json!([])),
        ] {
            s.state.insert(key.into(), value);
        }
        s.replacement_inputs = None;
    }

    fn record_lead_audit(&self, kind: &str, fields: &Object) {
        let _ = self.bridge.call(json!({
            "operation": "lead-audit", "kind": kind, "fields": fields,
            "logsDir": self.logs_dir.to_string_lossy(),
        }));
    }

    /// Record the outcome of the restart in progress; other spawns leave it alone.
    fn settle_restart(&self, s: &mut Inner, outcome: &str, message: &str, fields: Object) {
        let Some(Value::Object(record)) = s.state.get_mut("lastRestart") else {
            return;
        };
        if !RESTART_IN_PROGRESS.contains(&record.str_of("outcome")) {
            return;
        }
        record.insert("outcome".into(), json!(outcome));
        record.insert(
            "message".into(),
            json!(message.chars().take(2000).collect::<String>()),
        );
        record.insert("settledAt".into(), json!(now()));
        for (key, value) in fields {
            record.insert(key, value);
        }
        let snapshot = record.clone();
        self.record_lead_audit("restart_outcome", &snapshot);
    }

    fn note_unclean_close(s: &mut Inner, service: &str, detail: &str) {
        if let Some(Value::Object(record)) = s.state.get_mut("lastRestart") {
            if RESTART_IN_PROGRESS.contains(&record.str_of("outcome")) {
                let closes = record.entry("uncleanCloses").or_insert_with(|| json!([]));
                if let Value::Array(items) = closes {
                    items.push(json!({"service": service, "detail": detail, "at": now()}));
                }
            }
        }
    }

    /// A refusal decided while the old worker still serves; it keeps serving.
    fn refuse_restart(&self, s: &mut Inner, stage: &str, message: &str, fields: Object) -> Object {
        self.settle_restart(s, "refused", message, obj(json!({"stage": stage})));
        self.save_failure_record(s, "restart refusal");
        mlog!(
            Error,
            "worker restart refused at {stage} stage{}; the serving worker is retained: {message}",
            Self::trace_total_suffix(s)
        );
        merge(obj(json!({"ok": false, "message": message})), fields)
    }

    fn failure_retry_delay(consecutive: i64, recent: i64) -> f64 {
        (2f64.powi(consecutive.max(recent).min(64) as i32)).min(60.0)
    }

    fn account_failure(&self, s: &mut Inner) -> (f64, i64, i64) {
        let at = now();
        let mut history = self.recent_failure_times(s, at);
        history.push(at);
        let keep = history.len().saturating_sub(DEFAULT_FAILURE_HISTORY_KEEP);
        let history = history.split_off(keep);
        let failures = s.state.i64_of("consecutiveFailures").unwrap_or(0) + 1;
        let active = s.state.f64_of("activeFailureAt");
        s.state.insert("failureHistory".into(), json!(history));
        s.state.insert(
            "activeFailureAt".into(),
            json!(if active > 0.0 { active } else { at }),
        );
        s.state.insert("lastFailureAt".into(), json!(at));
        s.state
            .insert("consecutiveFailures".into(), json!(failures));
        (at, history.len() as i64, failures)
    }

    /// A generation that failed before its worker ran is retried, never latched at once.
    fn replacement_failed(
        &self,
        s: &mut Inner,
        source: &str,
        message: &str,
        inputs: Option<Prepared>,
        reset_failed: bool,
    ) -> Object {
        let (at, recent, consecutive) = self.account_failure(s);
        s.state.insert("lastFailureReason".into(), json!(message));
        s.state
            .insert("lastExitCause".into(), json!("replacement_start_failed"));
        if recent >= self.max_failures {
            s.state.insert("replacementRetry".into(), json!({}));
            s.replacement_inputs = None;
            s.state.insert("nextRetryAt".into(), json!(0.0));
            let paused = format!(
                "{message}; worker failed {recent} times in {:.0}s; recovery paused",
                self.failure_window
            );
            Self::set_observed(s, "crashloop", &paused);
            self.settle_restart(s, "paused", &paused, Object::new());
            self.save_failure_record(s, "paused recovery");
            mlog!(
                Error,
                "worker recovery paused after {recent} failures in {:.0}s; explicit start/restart is required to reset the crash-loop budget: {message}",
                self.failure_window
            );
            return obj(json!({"ok": false, "message": paused}));
        }
        let delay = Self::failure_retry_delay(consecutive, recent);
        s.state.insert("nextRetryAt".into(), json!(at + delay));
        s.state.insert(
            "replacementRetry".into(),
            json!({"source": source, "resetFailed": reset_failed, "failedAt": at, "error": message.chars().take(2000).collect::<String>()}),
        );
        s.replacement_inputs = inputs;
        let retrying = format!("{message}; retrying in {delay:.0}s");
        Self::set_observed(s, "starting", &retrying);
        self.settle_restart(
            s,
            "retrying",
            &retrying,
            obj(json!({"nextRetryAt": at + delay})),
        );
        self.save_failure_record(s, "replacement retry");
        mlog!(
            Error,
            "worker replacement failed to start (failure {recent}/{} in {:.0}s window, source={source}): {message}; retrying in {delay:.0}s",
            self.max_failures,
            self.failure_window
        );
        obj(json!({
            "ok": true, "retrying": true, "alreadyRunning": false, "launcherPid": null,
            "message": format!("replacement start failed; the manager retries in {delay:.0}s: {message}"),
        }))
    }

    fn restart_configuration(&self, s: &Inner) -> String {
        let worker = Self::worker(s);
        json::python_compact_sorted(&json!({
            "desired": s.state.get("desired"), "pid": worker.get("pid"),
            "processStartTime": worker.get("processStartTime"), "serverArgs": s.state.get("serverArgs"),
            "serverEnv": s.state.get("serverEnv"), "port": self.port(s),
        }))
    }

    fn disable_legacy_guard(&self) {
        let _ = std::fs::create_dir_all(&self.data_dir);
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data_dir.join(".tofu_guard_disabled"));
    }

    fn maintain_process_logs(&self, s: &mut Inner, force: bool) {
        if !force
            && s.last_log_maintenance
                .map(|at| at.elapsed().as_secs_f64() < LOG_MAINTENANCE_SECONDS)
                .unwrap_or(false)
        {
            return;
        }
        s.last_log_maintenance = Some(Instant::now());
        if let Ok(reply) = self
            .bridge
            .call(json!({"operation": "maintain-logs", "logsDir": self.logs_dir.to_string_lossy()}))
        {
            for failure in reply
                .get("failures")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                mlog!(Warning, "{}", failure.as_str().unwrap_or(""));
            }
        }
    }

    // ── worker identity ────────────────────────────────────────────────

    fn launcher_is_alive(s: &Inner) -> bool {
        let worker = Self::worker(s);
        let Some(pid) = worker.i64_of("pid") else {
            return false;
        };
        let host = worker.str_of("host");
        if (!host.is_empty() && host != hostname()) || !lockstatus::pid_is_server(pid) {
            return false;
        }
        match (worker.i64_of("processStartTime"), procfs::start_ticks(pid)) {
            (Some(expected), Some(actual)) => expected == actual,
            _ => true,
        }
    }

    fn worker_within_boot_grace(s: &Inner) -> bool {
        let spawned = Self::worker(s).f64_of("spawnedAt");
        spawned == 0.0 || now() - spawned <= DEFAULT_BOOT_GRACE
    }

    fn identity_error(&self, s: &Inner, status: &Object) -> Option<String> {
        let pid = status.i64_of("pid")?;
        if !status.truthy("running") {
            return None;
        }
        if status.get("sameHost") == Some(&json!(false)) {
            return Some(format!(
                "lock belongs to host {}",
                value_text(status.get("host").unwrap_or(&Value::Null))
            ));
        }
        let Some(cmdline) = status.get("cmdline").and_then(Value::as_str) else {
            return Some(format!("cannot verify cmdline for pid {pid}"));
        };
        if !cmdline.contains("server.py") {
            return Some(format!("pid {pid} is not server.py"));
        }
        if status.get("projectMatches") == Some(&json!(false)) {
            return Some(format!(
                "pid {pid} cwd is {}, not this project",
                value_text(status.get("processCwd").unwrap_or(&Value::Null))
            ));
        }
        let worker_environment: Env = envpolicy::identity_environment_keys()
            .into_iter()
            .filter_map(|key| procfs::env_value(pid, key).map(|value| (key.to_owned(), value)))
            .collect();
        let environment_error =
            envpolicy::production_environment_error(&self.project, &worker_environment);
        if !environment_error.is_empty() {
            return Some(format!(
                "pid {pid} has unsafe environment: {environment_error}"
            ));
        }
        let external = status.str_of("externalOwner");
        if !external.is_empty() && external != "supervisor" {
            return Some(format!(
                "pid {pid} is owned by {external}, not tofu-manager"
            ));
        }
        let remembered = Self::worker(s);
        if remembered.i64_of("pid") == Some(pid) {
            if let (Some(expected), Some(actual)) = (
                remembered.i64_of("processStartTime"),
                status.i64_of("processStartTime"),
            ) {
                if expected != actual {
                    return Some(format!("pid {pid} was reused (process start time changed)"));
                }
            }
        }
        None
    }

    fn port_conflict(&self, s: &Inner, status: &Object) -> (bool, Vec<i64>) {
        let port = self.port(s);
        let pids = lockstatus::listener_pids(port);
        let mut worker_pid = if status.truthy("running") {
            status.i64_of("pid")
        } else {
            None
        };
        if worker_pid.is_none() && pids.len() == 1 {
            let remembered = Self::worker(s);
            let candidate = pids[0];
            if remembered.i64_of("pid") == Some(candidate)
                && remembered.i64_of("processStartTime").is_some()
                && remembered.i64_of("processStartTime") == procfs::start_ticks(candidate)
                && procfs::cwd(candidate).as_deref() == Some(&*self.project.to_string_lossy())
                && procfs::cmdline(candidate)
                    .unwrap_or_default()
                    .contains("server.py")
                && procfs::env_value(candidate, "TOFU_MANAGED_BY").as_deref() == Some("supervisor")
            {
                worker_pid = Some(candidate);
            }
        }
        let foreign: Vec<i64> = pids
            .iter()
            .copied()
            .filter(|pid| Some(*pid) != worker_pid)
            .collect();
        let unknown_listener = worker_pid.is_none() && pids.is_empty() && port_accepts(port);
        let owners = if worker_pid.is_some() {
            foreign.clone()
        } else {
            pids.clone()
        };
        (
            !foreign.is_empty() || (!pids.is_empty() && worker_pid.is_none()) || unknown_listener,
            owners,
        )
    }

    fn remembered_worker_status(&self, s: &Inner) -> Option<Object> {
        let worker = Self::worker(s);
        let pid = worker.i64_of("pid")?;
        let host = if worker.str_of("host").is_empty() {
            hostname()
        } else {
            worker.str_of("host").to_owned()
        };
        if host != hostname() || !procfs::pid_is_alive(pid) {
            return None;
        }
        let cwd = procfs::cwd(pid);
        Some(obj(json!({
            "projectPath": self.project.to_string_lossy(), "running": true, "pid": pid, "host": host,
            "sameHost": true, "lockPresent": false, "stale": false,
            "processStartTime": procfs::start_ticks(pid), "processStartedAt": procfs::start_epoch(pid),
            "processCwd": cwd, "projectMatches": cwd.as_ref().map(|cwd| Path::new(cwd) == self.project),
            "cmdline": procfs::cmdline(pid), "externalOwner": procfs::env_value(pid, "TOFU_MANAGED_BY"),
        })))
    }

    fn remember_worker(s: &mut Inner, status: &Object, source: &str) {
        let Some(pid) = status.i64_of("pid").filter(|pid| *pid != 0) else {
            return;
        };
        let spawned = Self::worker(s).f64_of("spawnedAt");
        let host = if status.str_of("host").is_empty() {
            hostname()
        } else {
            status.str_of("host").to_owned()
        };
        s.state.insert(
            "worker".into(),
            json!({
                "pid": pid, "host": host,
                "processStartTime": status.get("processStartTime"), "processCwd": status.get("processCwd"),
                "spawnedAt": if spawned > 0.0 { spawned } else { now() },
            }),
        );
        if !source.is_empty() {
            s.state.insert("launchSource".into(), json!(source));
        }
    }

    fn live_worker_port(status: &Object) -> (Option<u16>, String) {
        if let Some(cmdline) = status
            .get("cmdline")
            .and_then(Value::as_str)
            .filter(|c| !c.trim().is_empty())
        {
            let args: Vec<String> = cmdline.split_whitespace().map(str::to_owned).collect();
            if let Some(port) = envpolicy::explicit_server_port(&args) {
                return (Some(port), "argv".into());
            }
        }
        if let Some(pid) = status.i64_of("pid") {
            for name in ["_TOFU_RUNTIME_PORT", "PORT"] {
                if let Some(port) =
                    procfs::env_value(pid, name).and_then(|value| envpolicy::valid_port(&value))
                {
                    return (Some(port), format!("env:{name}"));
                }
            }
        }
        (None, String::new())
    }

    /// Adopt the endpoint declared by an already identity-checked worker.
    fn reconcile_live_worker_port(&self, s: &mut Inner, status: &Object) -> bool {
        let (observed, source) = Self::live_worker_port(status);
        let Some(observed) = observed else {
            return false;
        };
        let previous = self.port(s);
        if observed == previous {
            return false;
        }
        s.state.insert("port".into(), json!(observed));
        let mut server_env = Self::server_env(s);
        server_env.insert("PORT".into(), observed.to_string());
        s.state.insert("serverEnv".into(), env_value(&server_env));
        let mut overrides = env_from_value(s.state.get("serverEnvOverrides"));
        overrides.insert("PORT".into(), observed.to_string());
        s.state
            .insert("serverEnvOverrides".into(), env_value(&overrides));
        let mut args: Vec<String> = s
            .state
            .get("serverArgs")
            .and_then(Value::as_array)
            .map(|items| items.iter().map(value_text).collect())
            .unwrap_or_default();
        for index in 0..args.len() {
            if args[index] == "--port" && index + 1 < args.len() {
                args[index + 1] = observed.to_string();
                break;
            }
            if args[index].starts_with("--port=") {
                args[index] = format!("--port={observed}");
                break;
            }
        }
        s.state.insert("serverArgs".into(), json!(args));
        s.state.insert("lastPortReconciledAt".into(), json!(now()));
        s.state
            .insert("lastPortReconciledFrom".into(), json!(previous));
        s.state
            .insert("lastPortReconciledSource".into(), json!(source));
        mlog!(
            Warning,
            "reconciled live worker port pid={} persisted={previous} observed={observed} source={source}",
            value_text(status.get("pid").unwrap_or(&Value::Null))
        );
        true
    }

    fn adopt_existing(&self) {
        let mut s = self.lock();
        let status = self.lock_status(&s);
        if !status.truthy("running") {
            return;
        }
        if let Some(error) = self.identity_error(&s, &status) {
            Self::set_observed(&mut s, "conflict", &error);
            self.save_logged(&mut s);
            return;
        }
        self.reconcile_live_worker_port(&mut s, &status);
        if !(s.had_state && s.state.str_of("desired") == "stopped") {
            s.state.insert("desired".into(), json!("running"));
        }
        let target = if s.state.str_of("desired") == "stopped" {
            "stopping"
        } else if port_accepts(self.port(&s)) {
            "running"
        } else {
            "starting"
        };
        Self::set_observed(&mut s, target, "");
        let source = if s.state.str_of("launchSource").is_empty() {
            "adopted".to_owned()
        } else {
            s.state.str_of("launchSource").to_owned()
        };
        Self::remember_worker(&mut s, &status, &source);
        self.disable_legacy_guard();
        self.save_logged(&mut s);
    }

    fn foreign_owner(s: &Inner, status: &Object) -> String {
        let here = hostname();
        let worker = Self::worker(s);
        let host = worker.str_of("host");
        if worker.truthy("pid") && !host.is_empty() && host != here {
            let pid = value_text(&worker["pid"]);
            return format!(
                "worker pid {pid} runs on host {host}; this Supervisor on {here} cannot observe it, so it neither records its exit nor starts another worker (start explicitly here only after {host} stopped serving this checkout)"
            );
        }
        if status.get("sameHost") == Some(&json!(false)) {
            return format!(
                "instance lock is held by pid {} on host {}; this Supervisor on {here} cannot observe it and starts no worker beside it",
                value_text(status.get("pid").unwrap_or(&Value::Null)),
                value_text(status.get("host").unwrap_or(&Value::Null))
            );
        }
        String::new()
    }

    // ── heartbeat and readiness ────────────────────────────────────────

    fn heartbeat_stale_age(&self, s: &Inner, pid: i64) -> Option<f64> {
        let dotenv = envpolicy::read_dotenv(&self.project.join(".env")).unwrap_or_default();
        let directory = dotenv
            .get("TOFU_HEARTBEAT_DIR")
            .filter(|value| !value.is_empty())
            .cloned()
            .or_else(|| std::env::var("TOFU_HEARTBEAT_DIR").ok())
            .unwrap_or_default();
        let heartbeat =
            datapaths::instance_heartbeat_path(&self.worker_data_directory(s), &directory);
        let value = read_object(&heartbeat);
        if int_of(value.get("pid")) != Some(pid) {
            return None;
        }
        let ts = match value.get("ts") {
            Some(Value::Number(n)) => n.as_f64()?,
            _ => return None,
        };
        let age = now() - ts;
        if age < 0.0 {
            return None;
        }
        let threshold = if value.str_of("phase") == "booting" {
            boot_heartbeat_grace_seconds()
        } else {
            DEFAULT_WEDGE_STALE
        };
        (age > threshold).then_some(age)
    }

    fn http_probe(&self, s: &Inner, pid: i64) -> Object {
        let mode =
            std::fs::read_to_string(self.data_dir.join(".last_serve_mode")).unwrap_or_default();
        let preferred = if mode.trim() == "https" {
            "https"
        } else {
            "http"
        };
        probe::probe_application_readiness(self.port(s), Some(pid), preferred, 2.0)
    }

    fn native_storage_verdict(&self) -> String {
        let record = self.storage.record();
        if record.str_of("state") != "failed" {
            return String::new();
        }
        let error = if record.truthy("error") {
            value_text(&record["error"])
        } else {
            "no error recorded".into()
        };
        if record.truthy("retrying") {
            return format!("native storage host degraded: {error}; it retries the start minutes apart, and an explicit restart retries at once");
        }
        format!("native storage host failed: {error}; explicit restart required")
    }

    fn note_unready_worker(&self, s: &mut Inner, pid: i64, readiness_error: &str) {
        let verdict = self.native_storage_verdict();
        let error = if verdict.is_empty() {
            readiness_error.to_owned()
        } else {
            format!("{readiness_error}; {verdict}")
        };
        if s.state.f64_of("unreadySince") == 0.0 {
            s.state.insert("unreadySince".into(), json!(now()));
        }
        if s.state.str_of("lastError") != error {
            mlog!(Error, "worker pid={pid} is live but not ready: {error}");
        }
        Self::set_observed(s, "degraded", &error);
    }

    fn worker_resource_budget_snapshot(&self, pid: Option<i64>) -> Value {
        let Some(pid) = pid.filter(|pid| *pid > 0) else {
            return Value::Null;
        };
        let Ok(raw) = std::fs::read(format!("/proc/{pid}/environ")) else {
            return Value::Null;
        };
        let mut found = std::collections::BTreeMap::new();
        for item in raw.split(|byte| *byte == 0) {
            let Some(at) = item.iter().position(|byte| *byte == b'=') else {
                continue;
            };
            let key = String::from_utf8_lossy(&item[..at]).into_owned();
            if WORKER_BUDGET_STATUS_KEYS.contains(&key.as_str())
                || key == RESOURCE_BUDGET_POLICY_ENV
                || key == RESOURCE_BUDGET_AUTOMATIC_ENV
            {
                found.insert(key, String::from_utf8_lossy(&item[at + 1..]).into_owned());
            }
        }
        let automatic: BTreeSet<&str> = found
            .get(RESOURCE_BUDGET_AUTOMATIC_ENV)
            .map(String::as_str)
            .unwrap_or("")
            .split(',')
            .filter(|name| self.resource_budget_keys.contains(*name))
            .collect();
        let policy = found
            .get(RESOURCE_BUDGET_POLICY_ENV)
            .cloned()
            .unwrap_or_default();
        let values: Object = WORKER_BUDGET_STATUS_KEYS
            .iter()
            .filter_map(|name| {
                found
                    .get(*name)
                    .map(|value| (name.to_string(), json!(value)))
            })
            .collect();
        json!({
            "policyVersion": if policy.is_empty() { Value::Null } else { json!(policy) },
            "currentPolicyVersion": self.resource_budget_policy_version,
            "policyCurrent": policy == self.resource_budget_policy_version,
            "automatic": automatic.into_iter().collect::<Vec<_>>(),
            "values": values,
        })
    }

    // ── storage lease ──────────────────────────────────────────────────

    fn active_storage_lease(&self, s: &Inner) -> Object {
        lockstatus::read_storage_lease_status(&self.storage_data_dir(s))
    }

    fn storage_lease_blocks_worker(&self, lease: &Object) -> bool {
        lease.truthy("held") && !self.storage.owns_lease(lease)
    }

    /// Let a just-stopped worker's child sidecar release its OS lease.
    fn wait_for_stopped_sidecar_release(&self, s: &Inner) -> Object {
        let started = Instant::now();
        let deadline = Deadline::after(DEFAULT_SIDECAR_LEASE_RELEASE_WAIT);
        loop {
            let lease = self.active_storage_lease(s);
            if self.storage.owns_lease(&lease) {
                return lease;
            }
            if !lease.truthy("held") || lease.str_of("kind") != "storage_authority_sql" {
                let waited = started.elapsed().as_secs_f64();
                if waited >= DEFAULT_STORAGE_LEASE_POLL_INTERVAL {
                    mlog!(
                        Info,
                        "stopped storage sidecar released its lease after {waited:.1}s"
                    );
                }
                return lease;
            }
            if deadline.expired() {
                mlog!(Warning, "stopped storage sidecar still holds its lease after {DEFAULT_SIDECAR_LEASE_RELEASE_WAIT:.1}s");
                return self.reap_orphaned_storage_authority_sql(s, lease);
            }
            sleep(DEFAULT_STORAGE_LEASE_POLL_INTERVAL.min(deadline.remaining()));
        }
    }

    fn reap_orphaned_storage_authority_sql(&self, s: &Inner, lease: Object) -> Object {
        if self.storage.owns_lease(&lease) {
            return lease;
        }
        let Some(pid) = lease.i64_of("pid") else {
            return lease;
        };
        if !lease.truthy("held")
            || lease.str_of("kind") != "storage_authority_sql"
            || !lease.truthy("holderVerified")
        {
            return lease;
        }
        let (kind, _) =
            lockstatus::legacy_storage_lease_owner(&procfs::cmdline(pid).unwrap_or_default());
        if kind != "storage_authority_sql" {
            mlog!(Warning, "storage lease holder pid={pid} no longer matches a sidecar identity; refusing to reap");
            return lease;
        }
        mlog!(
            Warning,
            "reaping orphaned storage sidecar pid={pid} that survived its worker"
        );
        for (signal, wait, name) in [
            (libc::SIGTERM, DEFAULT_ORPHAN_SIDECAR_REAP_WAIT, "SIGTERM"),
            (libc::SIGKILL, 3.0, "SIGKILL"),
        ] {
            if unsafe { libc::kill(pid as i32, signal) } != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::ESRCH) {
                    return self.active_storage_lease(s);
                }
                mlog!(
                    Warning,
                    "orphaned storage sidecar pid={pid} {name} failed: {error}"
                );
                return lease;
            }
            let deadline = Deadline::after(wait);
            while !deadline.expired() {
                let current = self.active_storage_lease(s);
                if !current.truthy("held") {
                    mlog!(
                        Info,
                        "orphaned storage sidecar pid={pid} released its lease after {name}"
                    );
                    return current;
                }
                sleep(DEFAULT_STORAGE_LEASE_POLL_INTERVAL);
            }
        }
        mlog!(
            Error,
            "orphaned storage sidecar pid={pid} survived SIGKILL; start stays refused"
        );
        lease
    }

    fn storage_blocker_message(lease: &Object) -> String {
        let label = if lease.str_of("label").is_empty() {
            "Storage operation"
        } else {
            lease.str_of("label")
        };
        let holder = match (lease.i64_of("pid"), lease.str_of("host")) {
            (Some(pid), _) => format!("{label} (PID {pid})"),
            (None, host) if !host.is_empty() => format!("{label} on {host}"),
            _ => label.to_owned(),
        };
        if lease.str_of("kind") == "offline_maintenance" {
            return format!("{holder} holds the storage lease; start is queued and will resume automatically after maintenance");
        }
        format!("{holder} holds the storage lease; refusing to start a second storage authority")
    }

    fn defer_for_storage_lease(&self, s: &mut Inner, lease: &Object, resume_source: &str) -> bool {
        let maintenance = lease.str_of("kind") == "offline_maintenance";
        let mut blocker: Object = [
            "held",
            "kind",
            "label",
            "pid",
            "host",
            "startedAt",
            "holderVerified",
        ]
        .iter()
        .map(|key| {
            (
                key.to_string(),
                lease.get(*key).cloned().unwrap_or(Value::Null),
            )
        })
        .collect();
        if !resume_source.is_empty() {
            blocker.insert("resumeSource".into(), json!(resume_source));
        }
        s.state
            .insert("storageBlocker".into(), Value::Object(blocker));
        Self::release_worker_leases(s);
        s.state.insert("worker".into(), json!({}));
        Self::set_observed(
            s,
            if maintenance {
                "maintenance"
            } else {
                "conflict"
            },
            &Self::storage_blocker_message(lease),
        );
        self.save_logged(s);
        maintenance
    }

    // ── exit accounting ────────────────────────────────────────────────

    fn clean_exit_marker(&self, s: &Inner) -> Option<Object> {
        let worker = Self::worker(s);
        let pid = worker.i64_of("pid")?;
        let marker = read_object(&self.storage_data_dir(s).join(".server_shutdown.json"));
        let reason = marker.str_of("reason").to_owned();
        if marker.str_of("state") != "clean"
            || marker.i64_of("pid") != Some(pid)
            || marker.str_of("host") != hostname()
            || !CLEAN_WORKER_EXIT_REASONS.contains(&reason.as_str())
        {
            return None;
        }
        let clean_ts = marker.get("clean_ts").and_then(Value::as_f64)?;
        let spawned = worker.f64_of("spawnedAt");
        if !clean_ts.is_finite() || clean_ts < (spawned - 1.0).max(0.0) || clean_ts > now() + 5.0 {
            return None;
        }
        let key = format!("{pid}:{clean_ts:.6}:{reason}");
        Some(obj(json!({
            "pid": pid, "reason": reason, "cleanTs": clean_ts, "markerKey": key,
            "alreadyConsumed": key == s.state.str_of("lastConsumedExitMarker"),
        })))
    }

    fn pending_exit_intent(s: &Inner, pid: i64, clean_ts: f64) -> Object {
        let intent = s.state.obj_of("pendingWorkerExitIntent");
        let reason = intent.str_of("reason");
        if intent.i64_of("pid") != Some(pid)
            || intent.str_of("host") != hostname()
            || !matches!(reason, "manual" | "restart")
        {
            return Object::new();
        }
        let Some(requested) = intent.get("requestedAt").and_then(Value::as_f64) else {
            return Object::new();
        };
        let spawned = Self::worker(s).f64_of("spawnedAt");
        if !requested.is_finite()
            || requested < (spawned - 1.0).max(0.0)
            || requested > now() + 5.0
            || requested > clean_ts + 1.0
            || clean_ts - requested > DEFAULT_EXIT_INTENT_WINDOW
        {
            return Object::new();
        }
        intent
    }

    fn departed_worker_exit(&self, s: &Inner) -> Object {
        let Some(clean) = self.clean_exit_marker(s) else {
            return obj(json!({"kind": "failure"}));
        };
        if clean.truthy("alreadyConsumed") {
            return obj(json!({"kind": "failure"}));
        }
        let mut reason = clean.str_of("reason").to_owned();
        let pid = clean.i64_of("pid").unwrap_or(0);
        let clean_ts = clean.f64_of("cleanTs");
        let intent = Self::pending_exit_intent(s, pid, clean_ts);
        if reason == "signal" && !intent.is_empty() {
            reason = intent.str_of("reason").to_owned();
        }
        let planned = |recovery: &str, reason: &str| {
            obj(
                json!({"kind": "planned", "reason": reason, "pid": pid, "markerKey": clean.str_of("markerKey"),
                       "exitedAt": clean_ts, "recoverySource": recovery}),
            )
        };
        match reason.as_str() {
            "manual" => planned("", "manual"),
            "restart" => {
                let recovery = if intent.str_of("recoverySource").is_empty() {
                    "clean-restart-recovery"
                } else {
                    intent.str_of("recoverySource")
                };
                planned(recovery, "restart")
            }
            "signal" => {
                let ready = s.state.f64_of("workerReadyLoggedAt");
                if 0.0 < ready && ready <= clean_ts + 1.0 {
                    planned("external-drain-recovery", "signal")
                } else {
                    obj(
                        json!({"kind": "failure", "exitedAt": clean_ts, "exitCause": "unintended_signal",
                               "failureReason": format!("worker exited after unplanned graceful signal pid {pid}")}),
                    )
                }
            }
            _ => obj(
                json!({"kind": "failure", "exitedAt": clean_ts, "exitCause": "memory_recycle",
                            "failureReason": format!("worker requested graceful memory recycle pid {pid}")}),
            ),
        }
    }

    /// Exit status of a retired generation, read before it is reaped.
    fn worker_exit_observation(s: &mut Inner, worker: &Object) -> Object {
        let pid = worker.i64_of("pid").unwrap_or(0);
        let observation =
            procfs::exit_observation(pid, worker.i64_of("processStartTime").unwrap_or(0));
        if !observation.is_empty() {
            return observation;
        }
        let Some(child) = s
            .worker_process
            .as_mut()
            .filter(|child| child.id() as i64 == pid)
        else {
            return observation;
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                use std::os::unix::process::ExitStatusExt;
                s.worker_process = None;
                let signal = status.signal();
                obj(json!({
                    "pid": pid, "processStartTime": worker.get("processStartTime"), "source": "popen_returncode",
                    "rawStatus": null, "signal": signal, "signalName": signal.map(procfs::signal_name).unwrap_or_default(),
                    "exitCode": status.code(), "coreDumpFlag": null,
                }))
            }
            _ => Object::new(),
        }
    }

    fn reap_retired_worker(s: &mut Inner, worker: &Object) {
        let pid = worker.i64_of("pid").unwrap_or(0);
        if let Some(child) = s.worker_process.as_mut() {
            if child.id() as i64 == pid {
                if !matches!(child.try_wait(), Ok(None)) {
                    s.worker_process = None;
                }
                return;
            }
        }
        procfs::reap_exited_child(pid, worker.i64_of("processStartTime").unwrap_or(0));
    }

    fn log_worker_exit(worker: &Object, observation: &Object, level: crate::log::Level) {
        let message = if observation.is_empty() {
            format!(
                "worker process exit observation unavailable pid={}: not an unreaped child of this Supervisor process",
                value_text(worker.get("pid").unwrap_or(&Value::Null))
            )
        } else {
            format!(
                "worker process exit observation: {}",
                Value::Object(observation.clone())
            )
        };
        crate::log::write(level, "server_manager", &message);
    }

    /// Consume one controlled exit without spending crash-loop budget.
    fn record_planned_exit(
        &self,
        s: &mut Inner,
        reason: &str,
        pid: Option<i64>,
        marker_key: &str,
        recovery: &str,
        exited_at: Option<f64>,
    ) -> bool {
        if !marker_key.is_empty() && marker_key == s.state.str_of("lastConsumedExitMarker") {
            return false;
        }
        let retired = Self::worker(s);
        let planned = s.state.i64_of("plannedExitCount").unwrap_or(0) + 1;
        s.state.insert("plannedExitCount".into(), json!(planned));
        s.state.insert(
            "lastWorkerExitAt".into(),
            json!(exited_at.unwrap_or_else(now)),
        );
        s.state
            .insert("lastWorkerExitKind".into(), json!("planned"));
        s.state.insert("lastWorkerExitReason".into(), json!(reason));
        s.state
            .insert("lastConsumedExitMarker".into(), json!(marker_key));
        s.state
            .insert("pendingRecoverySource".into(), json!(recovery));
        s.state.insert("pendingWorkerExitIntent".into(), json!({}));
        s.state.insert("nextRetryAt".into(), json!(0.0));
        let observation = Self::worker_exit_observation(s, &retired);
        s.state.insert(
            "lastWorkerProcessExit".into(),
            Value::Object(observation.clone()),
        );
        s.state.insert("worker".into(), json!({}));
        Self::release_worker_leases(s);
        mlog!(
            Info,
            "worker controlled exit recorded pid={} reason={reason} recovery={}",
            pid.map(|p| p.to_string()).unwrap_or_else(|| "None".into()),
            if recovery.is_empty() {
                "none"
            } else {
                recovery
            }
        );
        Self::log_worker_exit(&retired, &observation, crate::log::Level::Info);
        self.save_logged(s);
        Self::reap_retired_worker(s, &retired);
        true
    }

    fn account_worker_failure(
        &self,
        s: &mut Inner,
        exit_cause: &str,
        failure_reason: &str,
        exited_at: Option<f64>,
    ) -> (f64, i64, i64, Object) {
        let (at, recent, failures) = self.account_failure(s);
        let prior_oom = s.state.i64_of("lastCgroupOomKillCount");
        let current_oom = cgroup_oom_kill_count();
        let oom_delta = match (prior_oom, current_oom) {
            (Some(prior), Some(current)) => (current - prior).max(0),
            _ => 0,
        };
        if let Some(current) = current_oom {
            s.state
                .insert("lastCgroupOomKillCount".into(), json!(current));
        }
        let prior_error = s.state.str_of("lastError").to_owned();
        let retired = Self::worker(s);
        let observation = Self::worker_exit_observation(s, &retired);
        s.state.insert(
            "lastWorkerProcessExit".into(),
            Value::Object(observation.clone()),
        );
        let (cause, reason) = if !exit_cause.is_empty() && !failure_reason.is_empty() {
            (exit_cause.to_owned(), failure_reason.to_owned())
        } else if oom_delta > 0 {
            (
                "cgroup_oom_event".into(),
                format!("worker disappeared while shared cgroup oom_kill advanced by {oom_delta}"),
            )
        } else if prior_error.starts_with("recovered wedged worker") {
            ("health_wedge".into(), prior_error.clone())
        } else if observation.truthy("signal") {
            (
                "fatal_signal".into(),
                format!(
                    "worker pid={} terminated with {} ({}); signal sender unknown; inspect the native core",
                    value_text(&observation["pid"]),
                    observation.str_of("signalName"),
                    value_text(&observation["signal"])
                ),
            )
        } else {
            (
                "unexpected_exit".into(),
                if prior_error.is_empty() {
                    "worker exited unexpectedly".into()
                } else {
                    prior_error
                },
            )
        };
        for (key, value) in [
            ("lastFailureReason", json!(reason)),
            ("lastExitCause", json!(cause)),
            ("lastWorkerExitAt", json!(exited_at.unwrap_or(at))),
            ("lastWorkerExitKind", json!("failure")),
            ("lastWorkerExitReason", json!(cause)),
            ("lastCgroupOomDelta", json!(oom_delta)),
            (
                "restartCount",
                json!(s.state.i64_of("restartCount").unwrap_or(0) + 1),
            ),
            (
                "workerFailureCount",
                json!(s.state.i64_of("workerFailureCount").unwrap_or(0) + 1),
            ),
            ("pendingWorkerExitIntent", json!({})),
            ("worker", json!({})),
        ] {
            s.state.insert(key.into(), value);
        }
        Self::note_unready_runtime(s, &reason);
        Self::release_worker_leases(s);
        mlog!(
            Warning,
            "worker failure {recent}/{} in {:.0}s window (consecutive={failures}, cause={cause}): {reason}",
            self.max_failures,
            self.failure_window
        );
        Self::log_worker_exit(&retired, &observation, crate::log::Level::Warning);
        (at, recent, failures, retired)
    }

    /// Record a gone generation's exit before anything retires its record.
    fn settle_departed_worker(&self, s: &mut Inner) {
        let worker = Self::worker(s);
        let Some(pid) = worker.i64_of("pid") else {
            return;
        };
        let host = worker.str_of("host");
        if (!host.is_empty() && host != hostname()) || Self::launcher_is_alive(s) {
            return;
        }
        mlog!(Info, "worker pid={pid} exited while unobserved; recording its exit before the record is retired");
        let departed = self.departed_worker_exit(s);
        if departed.str_of("kind") == "planned" {
            self.record_planned_exit(
                s,
                departed.str_of("reason"),
                departed.i64_of("pid"),
                departed.str_of("markerKey"),
                "",
                departed.get("exitedAt").and_then(Value::as_f64),
            );
            return;
        }
        let (_, _, _, retired) = self.account_worker_failure(
            s,
            departed.str_of("exitCause"),
            departed.str_of("failureReason"),
            departed.get("exitedAt").and_then(Value::as_f64),
        );
        self.save_logged(s);
        Self::reap_retired_worker(s, &retired);
    }

    // ── resident services ──────────────────────────────────────────────

    fn storage_retention_active(&self, s: &mut Inner) -> bool {
        if !s.state.truthy("storageRetention") {
            return false;
        }
        let Some(plan) = self.storage.retained_plan() else {
            mlog!(Warning, "retained storage host is no longer ready; the next launch prepares the current storage generation");
            s.state.insert("storageRetention".into(), json!({}));
            return false;
        };
        let refusal = self.retained_engine_refusal(&plan);
        if !refusal.is_empty() {
            mlog!(Warning, "retained storage host can no longer serve this source; the next launch prepares the current storage generation: {refusal}");
            s.state.insert("storageRetention".into(), json!({}));
            return false;
        }
        true
    }

    fn retained_engine_refusal(&self, plan: &Value) -> String {
        match self.bridge.retention_refusal(&self.project, plan) {
            Ok(refusal) => refusal,
            Err(error) => format!("the storage contract of this source could not be read: {error}"),
        }
    }

    /// Prepare every generation before replacing any serving process.
    fn prepare_resident_services(
        &self,
        environment: &Env,
        execution_enabled: bool,
        retain_storage: bool,
    ) -> Result<Prepared, String> {
        let mut env = environment.clone();
        env.remove(RETAINED_ENGINE_ENVIRONMENT);
        let storage_plan = if retain_storage {
            let plan = self
                .storage
                .retained_plan()
                .ok_or("no ready storage host of this manager can be retained")?;
            if let Some(sha) = plan
                .get("binarySha256")
                .and_then(Value::as_str)
                .filter(|sha| !sha.is_empty())
            {
                env.insert(RETAINED_ENGINE_ENVIRONMENT.into(), sha.to_owned());
            }
            Some(plan)
        } else {
            self.storage.prepare(&env)?
        };
        let mut execution_plan = None;
        if execution_enabled {
            let mut execution_env = env.clone();
            if storage_plan.is_some() {
                execution_env.insert(
                    "TOFU_STORAGE_CONNECTION_FILE".into(),
                    self.storage.result_path(),
                );
            }
            if execution_env
                .get("TOFU_STORAGE_CONNECTION_FILE")
                .map(String::is_empty)
                .unwrap_or(true)
            {
                return Err("Independent execution requires a shared storage endpoint".into());
            }
            execution_plan = self.execution.prepare(&execution_env)?;
        }
        Ok(Prepared {
            environment: env,
            storage_plan,
            execution_plan,
        })
    }

    /// Reuse unchanged execution; replace dependencies only after joined drain.
    fn ensure_resident_services(
        &self,
        s: &mut Inner,
        prepared: &Prepared,
        reset_failed: bool,
    ) -> Result<Env, SpawnError> {
        let mut env = prepared.environment.clone();
        let storage_changes = self
            .storage
            .needs_replacement(prepared.storage_plan.as_ref(), reset_failed);
        let execution_changes = self
            .execution
            .needs_replacement(prepared.execution_plan.as_ref(), reset_failed);
        let failed = |error: ServiceError| SpawnError::Failed(error.to_string());
        if storage_changes || execution_changes {
            let retired = self
                .execution
                .stop(NATIVE_SERVICE_STOP_SECONDS, reset_failed, &self.bridge)
                .map_err(|error| {
                    SpawnError::Failed(format!("execution shutdown failed: {error}"))
                })?;
            if !retired.is_empty() {
                mlog!(Warning, "explicit restart replaced an execution host that exited after an unclean close: {retired}");
                Self::note_unclean_close(s, "execution", &retired);
            }
        }
        if storage_changes {
            let retired = self
                .storage
                .stop(NATIVE_SERVICE_STOP_SECONDS, reset_failed, &self.bridge)
                .map_err(failed)?;
            if !retired.is_empty() {
                mlog!(
                    Warning,
                    "explicit restart replaced a native storage host whose close was not confirmed: {retired}; its successor opens the storage authority through crash recovery"
                );
                Self::note_unclean_close(s, "native storage", &retired);
            }
            let lease = self.wait_for_stopped_sidecar_release(s);
            if self.storage_lease_blocks_worker(&lease) {
                return Err(SpawnError::StorageLeaseStillHeld(lease));
            }
        }
        if let Some(plan) = &prepared.storage_plan {
            let connection = self.storage.ensure(plan, &env).map_err(failed)?;
            env.insert("TOFU_STORAGE_CONNECTION_FILE".into(), connection);
        }
        if let Some(plan) = &prepared.execution_plan {
            self.execution.ensure(plan, &env).map_err(failed)?;
            env.insert("TOFU_PROCESS_ROLE".into(), "api".into());
        }
        Ok(env)
    }

    fn stop_resident_services(&self, s: &mut Inner) -> Result<(), String> {
        self.execution
            .stop(NATIVE_SERVICE_STOP_SECONDS, false, &self.bridge)
            .map_err(|e| e.to_string())?;
        self.storage
            .stop(NATIVE_SERVICE_STOP_SECONDS, false, &self.bridge)
            .map_err(|e| e.to_string())?;
        s.state.insert("storageRetention".into(), json!({}));
        Ok(())
    }

    fn resident_replacement_refusal(&self, prepared: &Prepared) -> String {
        let storage_changes = self
            .storage
            .needs_replacement(prepared.storage_plan.as_ref(), true);
        let execution_changes = self
            .execution
            .needs_replacement(prepared.execution_plan.as_ref(), true);
        let refusal = self.execution.replacement_refusal(
            prepared.execution_plan.as_ref(),
            storage_changes || execution_changes,
        );
        if !refusal.is_empty() {
            return refusal;
        }
        self.storage
            .replacement_refusal(prepared.storage_plan.as_ref(), storage_changes)
    }

    // ── spawn ──────────────────────────────────────────────────────────

    fn spawn(
        &self,
        s: &mut Inner,
        source: &str,
        prepared: Option<Prepared>,
        reset_failed_storage: bool,
        prepare_engine: bool,
    ) -> Object {
        s.state.insert("pendingRecoverySource".into(), json!(""));
        s.state.insert("pendingWorkerExitIntent".into(), json!({}));
        let mut prepared = prepared;
        let mut reset_failed_storage = reset_failed_storage;
        let retry = s.state.obj_of("replacementRetry");
        if !retry.is_empty() && prepared.is_none() {
            reset_failed_storage = reset_failed_storage || retry.truthy("resetFailed");
            prepared = s.replacement_inputs.clone();
        }
        let server_env = Self::server_env(s);
        let port = self.port(s);
        let composition = match self.composition(&server_env, port) {
            Ok(composition) => composition,
            Err(error) => {
                return self.replacement_failed(
                    s,
                    source,
                    &format!("launch environment could not be composed: {error}"),
                    prepared,
                    reset_failed_storage,
                )
            }
        };
        let mut env = prepared
            .as_ref()
            .map(|p| p.environment.clone())
            .unwrap_or_else(|| composition.environment.clone());
        let environment_error = envpolicy::production_environment_error(&self.project, &env);
        if !environment_error.is_empty() {
            s.state.insert("desired".into(), json!("stopped"));
            s.state.insert(
                "environmentQuarantine".into(),
                json!({"detectedAt": now(), "reason": environment_error, "discardedKeys": []}),
            );
            Self::set_observed(s, "stopped", &environment_error);
            self.save_logged(s);
            mlog!(Error, "{environment_error}; worker spawn was blocked");
            return obj(json!({"ok": false, "message": environment_error}));
        }
        Self::release_worker_leases(s);
        let _ = std::fs::create_dir_all(&self.logs_dir);
        self.maintain_process_logs(s, true);
        let _ = std::fs::create_dir_all(&self.data_dir);
        self.disable_legacy_guard();
        let retain_storage = prepared.is_none() && self.storage_retention_active(s);
        if prepared.is_none() {
            let parts = if prepare_engine && !retain_storage {
                Parts::All
            } else {
                Parts::Frontend
            };
            let preflight = preparation::run_frontend_preflight(
                &self.project,
                &self.python,
                &mut env,
                &format!("manager {source}"),
                parts,
                None,
            );
            if !preflight.ready {
                let message = format!(
                    "worker spawn refused during artifact preparation: {}",
                    preflight.error
                );
                Self::set_observed(s, "crashloop", &message);
                self.save_logged(s);
                mlog!(Error, "{message}");
                return obj(json!({"ok": false, "message": message}));
            }
        }
        let retry_inputs = prepared.clone();
        let prepared = match prepared {
            Some(prepared) => prepared,
            None => match self.prepare_resident_services(
                &env,
                composition.execution_enabled,
                retain_storage,
            ) {
                Ok(prepared) => prepared,
                Err(error) => {
                    return self.replacement_failed(
                        s,
                        source,
                        &format!("resident service preparation failed: {error}"),
                        retry_inputs,
                        reset_failed_storage,
                    )
                }
            },
        };
        let env = match self.ensure_resident_services(s, &prepared, reset_failed_storage) {
            Ok(env) => env,
            Err(SpawnError::StorageLeaseStillHeld(lease)) => {
                let maintenance = lease.str_of("kind") == "offline_maintenance";
                if retry_inputs.is_some() {
                    s.state.insert(
                        "replacementRetry".into(),
                        json!({"source": source, "resetFailed": reset_failed_storage, "failedAt": now(),
                               "error": format!("storage authority lease is still held by pid {}", value_text(lease.get("pid").unwrap_or(&Value::Null)))}),
                    );
                    s.replacement_inputs = retry_inputs;
                }
                let message = Self::storage_blocker_message(&lease);
                self.settle_restart(s, "deferred", &message, Object::new());
                self.defer_for_storage_lease(s, &lease, source);
                self.save_logged(s);
                let status = self.status_locked(s, false);
                return merge(
                    obj(
                        json!({"ok": maintenance, "message": message, "waitingForMaintenance": maintenance, "blockedByStorageLease": true}),
                    ),
                    status,
                );
            }
            Err(SpawnError::Failed(error)) => {
                return self.replacement_failed(
                    s,
                    source,
                    &format!("resident service preparation failed: {error}"),
                    retry_inputs,
                    reset_failed_storage,
                )
            }
        };
        let mut env = env;
        let worker_python = self.select_worker_runtime(s, &mut env);
        let args: Vec<String> = s
            .state
            .get("serverArgs")
            .and_then(Value::as_array)
            .map(|items| items.iter().map(value_text).collect())
            .unwrap_or_default();
        let log = match std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.worker_log)
        {
            Ok(log) => log,
            Err(error) => {
                return self.replacement_failed(
                    s,
                    source,
                    &format!("cannot open worker log: {error}"),
                    retry_inputs,
                    reset_failed_storage,
                )
            }
        };
        let mut command = std::process::Command::new(&worker_python);
        command
            .arg("server.py")
            .args(&args)
            .current_dir(&self.project)
            .env_clear()
            .envs(&env)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::from(match log.try_clone() {
                Ok(clone) => clone,
                Err(error) => {
                    return self.replacement_failed(
                        s,
                        source,
                        &format!("cannot open worker log: {error}"),
                        retry_inputs,
                        reset_failed_storage,
                    )
                }
            }))
            .stderr(std::process::Stdio::from(log));
        unsafe {
            use std::os::unix::process::CommandExt;
            command.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                return self.replacement_failed(
                    s,
                    source,
                    &format!("spawn failed: {error}"),
                    retry_inputs,
                    reset_failed_storage,
                )
            }
        };
        let pid = child.id() as i64;
        if let Some(mut previous) = s.worker_process.replace(child) {
            let returncode = match previous.try_wait() {
                Ok(Some(status)) => format!(
                    "returncode {}",
                    status
                        .code()
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| "signal".into())
                ),
                _ => "still running".into(),
            };
            mlog!(Warning, "worker pid={} ({returncode}) was replaced by generation pid={pid} before its exit was recorded", previous.id());
        }
        s.state.insert(
            "worker".into(),
            json!({"pid": pid, "host": hostname(), "processStartTime": procfs::start_ticks(pid),
                   "processCwd": self.project.to_string_lossy(), "spawnedAt": now()}),
        );
        s.state.insert("workerReadyLoggedAt".into(), json!(0.0));
        s.state.insert("launchSource".into(), json!(source));
        s.state.insert("lastError".into(), json!(""));
        s.state.insert("storageBlocker".into(), json!({}));
        s.state.insert("replacementRetry".into(), json!({}));
        s.replacement_inputs = None;
        self.settle_restart(
            s,
            "spawned",
            &format!("worker pid={pid} spawned (source={source})"),
            obj(json!({"workerPid": pid})),
        );
        Self::set_observed(s, "starting", "");
        self.save_logged(s);
        mlog!(
            Info,
            "worker spawned pid={pid} source={source}{}; boot progress follows in {}",
            Self::trace_total_suffix(s),
            self.worker_log.display()
        );
        obj(
            json!({"ok": true, "alreadyRunning": false, "launcherPid": pid, "message": "started; poll /status for readiness"}),
        )
    }

    // ── public lifecycle operations ────────────────────────────────────

    /// `_validated_server_environment`: project values, forwarded values, error.
    fn validated_server_environment(
        &self,
        s: &Inner,
        server_env: Option<&Object>,
    ) -> (Option<Env>, Env, String) {
        let current_project = self.project_server_env();
        let project_error =
            envpolicy::production_environment_error(&self.project, &current_project);
        if !project_error.is_empty() {
            return (Some(current_project), Env::new(), project_error);
        }
        let (project_environment, candidate) = match server_env {
            None => (None, Self::server_env(s)),
            Some(forwarded) => {
                let unsafe_keys: Vec<&String> = forwarded
                    .keys()
                    .filter(|key| !self.server_env_keys.contains(*key))
                    .collect();
                if !unsafe_keys.is_empty() {
                    let mut names: Vec<String> = unsafe_keys.into_iter().cloned().collect();
                    names.sort();
                    return (
                        None,
                        Env::new(),
                        format!("unsupported serverEnv key(s): {}", names.join(", ")),
                    );
                }
                let mut candidate = current_project.clone();
                for (key, value) in forwarded {
                    if !value.is_null() && !value_text(value).is_empty() {
                        candidate.insert(key.clone(), value_text(value));
                    }
                }
                (Some(current_project), candidate)
            }
        };
        let error =
            envpolicy::production_environment_error(&self.project, &self.effective_env(&candidate));
        (project_environment, candidate, error)
    }

    pub fn start(
        &self,
        server_args: Option<&Value>,
        server_env: Option<&Value>,
        source: &str,
        explicit: bool,
    ) -> Object {
        let mut s = self.lock();
        self.start_locked(&mut s, server_args, server_env, source, explicit, None)
    }

    fn start_locked(
        &self,
        s: &mut Inner,
        server_args: Option<&Value>,
        server_env: Option<&Value>,
        source: &str,
        explicit: bool,
        prepared: Option<Prepared>,
    ) -> Object {
        if server_args.is_some_and(|args| !args.is_array()) {
            return obj(json!({"ok": false, "message": "serverArgs must be a JSON array"}));
        }
        if server_env.is_some_and(|env| !env.is_object()) {
            return obj(json!({"ok": false, "message": "serverEnv must be a JSON object"}));
        }
        let (project_environment, forwarded, environment_error) =
            self.validated_server_environment(s, server_env.and_then(Value::as_object));
        if !environment_error.is_empty() {
            return obj(json!({"ok": false, "message": environment_error}));
        }
        let status = self.lock_status(s);
        if status.truthy("running") {
            let identity_error = self.identity_error(s, &status);
            if identity_error.is_none() {
                self.reconcile_live_worker_port(s, &status);
            }
            let (conflict, foreign) = self.port_conflict(s, &status);
            if identity_error.is_some() || conflict {
                let error = identity_error.unwrap_or_else(|| {
                    format!("port {} also owned by pid(s) {:?}", self.port(s), foreign)
                });
                Self::set_observed(s, "conflict", &error);
                self.save_logged(s);
                return merge(
                    obj(
                        json!({"ok": false, "alreadyRunning": false, "launcherPid": null, "message": error}),
                    ),
                    self.status_locked(s, false),
                );
            }
            s.state.insert("desired".into(), json!("running"));
            if explicit {
                Self::clear_failure_budget(s);
            }
            let launch_source = if s.state.str_of("launchSource").is_empty() {
                "adopted".to_owned()
            } else {
                s.state.str_of("launchSource").to_owned()
            };
            Self::remember_worker(s, &status, &launch_source);
            let observed = if port_accepts(self.port(s)) {
                "running"
            } else {
                "starting"
            };
            Self::set_observed(s, observed, "");
            self.disable_legacy_guard();
            self.save_logged(s);
            let supplied = server_args
                .and_then(Value::as_array)
                .is_some_and(|a| !a.is_empty())
                || server_env
                    .and_then(Value::as_object)
                    .is_some_and(|e| !e.is_empty());
            let message = if supplied {
                "already running; supplied server options were ignored"
            } else {
                "already running"
            };
            return merge(
                obj(
                    json!({"ok": true, "alreadyRunning": true, "launcherPid": status.get("pid"), "message": message}),
                ),
                self.status_locked(s, false),
            );
        }
        if let Some(args) = server_args.and_then(Value::as_array) {
            let cleaned: Vec<String> = args.iter().map(value_text).collect();
            if cleaned.len() > 32 || cleaned.iter().any(|arg| arg.len() > 4096) {
                return obj(json!({"ok": false, "message": "serverArgs exceeds safety limit"}));
            }
            s.state.insert("serverArgs".into(), json!(cleaned));
        }
        if server_env.is_some() {
            s.project_env = project_environment.unwrap_or_default();
            s.state.insert("serverEnv".into(), env_value(&forwarded));
            let overrides = envpolicy::operator_overrides(&forwarded, &s.project_env);
            s.state
                .insert("serverEnvOverrides".into(), env_value(&overrides));
            s.state.insert("environmentQuarantine".into(), json!({}));
        }
        if server_args.is_some() || server_env.is_some() {
            self.refresh_port(s);
        }
        let status = self.lock_status(s);
        let (conflict, foreign) = self.port_conflict(s, &status);
        if conflict {
            s.state.insert("desired".into(), json!("running"));
            let owner = if foreign.is_empty() {
                "an unknown process".to_owned()
            } else {
                format!("pid(s) {foreign:?}")
            };
            let message = format!("port {} owned by {owner}", self.port(s));
            Self::set_observed(s, "conflict", &message);
            self.save_logged(s);
            return merge(
                obj(
                    json!({"ok": false, "alreadyRunning": false, "launcherPid": null, "message": message}),
                ),
                self.status_locked(s, false),
            );
        }
        s.state.insert("desired".into(), json!("running"));
        self.settle_departed_worker(s);
        if explicit {
            Self::clear_failure_budget(s);
        }
        if Self::launcher_is_alive(s) {
            Self::set_observed(s, "starting", "");
            self.save_logged(s);
            let pid = Self::worker(s).get("pid").cloned().unwrap_or(Value::Null);
            return merge(
                obj(
                    json!({"ok": true, "alreadyRunning": true, "launcherPid": pid, "message": "startup already in progress"}),
                ),
                self.status_locked(s, false),
            );
        }
        let lease = self.wait_for_stopped_sidecar_release(s);
        if self.storage_lease_blocks_worker(&lease) {
            let maintenance = self.defer_for_storage_lease(s, &lease, "");
            return merge(
                obj(
                    json!({"ok": maintenance, "alreadyRunning": false, "launcherPid": null,
                           "waitingForMaintenance": maintenance, "blockedByStorageLease": true,
                           "message": Self::storage_blocker_message(&lease)}),
                ),
                self.status_locked(s, false),
            );
        }
        match prepared {
            Some(prepared) => self.spawn(s, source, Some(prepared), explicit, false),
            None => self.spawn(s, source, None, explicit, explicit),
        }
    }

    fn refresh_port(&self, s: &mut Inner) {
        let args: Vec<String> = s
            .state
            .get("serverArgs")
            .and_then(Value::as_array)
            .map(|items| items.iter().map(value_text).collect())
            .unwrap_or_default();
        let env = self.effective_env(&Self::server_env(s));
        s.state
            .insert("port".into(), json!(envpolicy::server_port(&args, &env)));
    }

    fn worker_shutdown_hard_seconds(&self, s: &Inner) -> f64 {
        let server_env = Self::server_env(s);
        let raw = [
            server_env
                .get("TOFU_SHUTDOWN_HARD_SECS")
                .cloned()
                .unwrap_or_default(),
            std::env::var("TOFU_SHUTDOWN_HARD_SECS").unwrap_or_default(),
            s.project_env
                .get("TOFU_SHUTDOWN_HARD_SECS")
                .cloned()
                .unwrap_or_default(),
        ]
        .into_iter()
        .find(|value| !value.is_empty())
        .unwrap_or_default();
        let value = if raw.is_empty() {
            DEFAULT_WORKER_SHUTDOWN_HARD_SECS
        } else {
            raw.trim()
                .parse()
                .unwrap_or(DEFAULT_WORKER_SHUTDOWN_HARD_SECS)
        };
        value.clamp(5.0, 300.0)
    }

    fn log_terminated_worker_exit(status: &Object, elapsed: f64) -> String {
        let pid = status.i64_of("pid").unwrap_or(0);
        let observation =
            procfs::exit_observation(pid, status.i64_of("processStartTime").unwrap_or(0));
        if observation.get("exitCode") == Some(&json!(0)) {
            mlog!(
                Info,
                "worker pid={pid} exited cleanly {elapsed:.1}s after SIGTERM"
            );
            return "stopped cleanly".into();
        }
        if observation.is_empty() {
            mlog!(Info, "worker pid={pid} exited {elapsed:.1}s after SIGTERM; exit status unavailable (not an unreaped child of this Supervisor)");
            return "stopped; exit status unavailable".into();
        }
        let outcome = match observation.get("exitCode") {
            Some(code) if !code.is_null() => format!("exit code {code}"),
            _ => format!(
                "signal {}",
                if observation.str_of("signalName").is_empty() {
                    value_text(&observation["signal"])
                } else {
                    observation.str_of("signalName").to_owned()
                }
            ),
        };
        mlog!(Warning, "worker pid={pid} exited {elapsed:.1}s after SIGTERM with {outcome}; its graceful shutdown did not complete cleanly");
        format!("stopped with {outcome}")
    }

    fn terminate(&self, s: &Inner, status: &Object) -> (bool, bool, String) {
        let timeout = self.worker_shutdown_hard_seconds(s) + WORKER_TERMINATE_TIMEOUT_MARGIN;
        let Some(pid) = status.i64_of("pid").filter(|_| status.truthy("running")) else {
            return (true, false, "nothing running".into());
        };
        if let Some(error) = self.identity_error(s, status) {
            return (false, false, error);
        }
        if unsafe { libc::kill(pid as i32, libc::SIGTERM) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                return (true, false, "already stopped".into());
            }
            return (false, false, format!("SIGTERM failed: {error}"));
        }
        let started = Instant::now();
        mlog!(
            Info,
            "worker pid={pid} SIGTERM sent; waiting up to {timeout:.1}s for graceful shutdown"
        );
        let deadline = Deadline::after(timeout);
        while !deadline.expired() {
            if !procfs::pid_is_alive(pid) {
                return (
                    true,
                    false,
                    Self::log_terminated_worker_exit(status, started.elapsed().as_secs_f64()),
                );
            }
            sleep(0.2);
        }
        mlog!(
            Warning,
            "worker pid={pid} still alive {:.1}s after SIGTERM; escalating to SIGKILL (worker shutdown sequence was pre-empted)",
            started.elapsed().as_secs_f64()
        );
        if unsafe { libc::kill(pid as i32, libc::SIGKILL) } != 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                return (true, false, "stopped cleanly".into());
            }
            return (false, false, format!("SIGKILL failed: {error}"));
        }
        let deadline = Deadline::after(2.0);
        while !deadline.expired() {
            if !procfs::pid_is_alive(pid) {
                mlog!(
                    Info,
                    "worker pid={pid} exited after SIGKILL ({:.1}s after SIGTERM)",
                    started.elapsed().as_secs_f64()
                );
                return (true, true, "stopped with SIGKILL".into());
            }
            sleep(0.1);
        }
        (false, true, format!("pid {pid} survived SIGKILL"))
    }

    pub fn stop(&self, source: &str) -> Object {
        let mut s = self.lock();
        self.stop_locked(&mut s, source, false)
    }

    fn stop_locked(&self, s: &mut Inner, source: &str, keep_desired_running: bool) -> Object {
        let mut status = self.lock_status(s);
        let foreign_owner = if status.truthy("running") {
            String::new()
        } else {
            Self::foreign_owner(s, &status)
        };
        if !foreign_owner.is_empty() {
            Self::set_observed(s, "conflict", &foreign_owner);
            return merge(
                obj(
                    json!({"ok": false, "wasRunning": false, "exitCode": 1, "message": foreign_owner}),
                ),
                self.status_locked(s, false),
            );
        }
        if !status.truthy("running") {
            if let Some(remembered) = self.remembered_worker_status(s) {
                status = remembered;
            }
        }
        let was_running = status.truthy("running");
        let planned_reason = if was_running {
            Self::remember_worker(s, &status, "");
            if keep_desired_running {
                "restart"
            } else {
                "manual"
            }
        } else {
            self.settle_departed_worker(s);
            ""
        };
        s.state.insert(
            "desired".into(),
            json!(if keep_desired_running {
                "running"
            } else {
                "stopped"
            }),
        );
        s.state.insert("launchSource".into(), json!(source));
        let recovery = if keep_desired_running {
            "manager-restart"
        } else {
            ""
        };
        s.state.insert(
            "pendingWorkerExitIntent".into(),
            if was_running {
                json!({"pid": status.get("pid"), "host": hostname(), "reason": planned_reason, "requestedAt": now(), "recoverySource": recovery})
            } else {
                json!({})
            },
        );
        if !keep_desired_running {
            s.state.insert("storageBlocker".into(), json!({}));
            s.state.insert("replacementRetry".into(), json!({}));
            s.replacement_inputs = None;
        }
        Self::set_observed(s, if was_running { "stopping" } else { "stopped" }, "");
        self.disable_legacy_guard();
        // desired=stopped must win before the signal is sent.
        if let Err(error) = self.save(s) {
            mlog!(
                Error,
                "could not persist the stop intent to {}: {error}",
                self.state_path.display()
            );
        }
        let (mut ok, killed, mut message) = self.terminate(s, &status);
        if ok && !keep_desired_running {
            if let Err(error) = self.stop_resident_services(s) {
                ok = false;
                message = format!("resident service shutdown incomplete: {error}");
            }
        }
        if ok {
            if was_running {
                let pid = status.i64_of("pid");
                let key = format!(
                    "manager:{}:{:.6}:{planned_reason}",
                    pid.map(|p| p.to_string()).unwrap_or_default(),
                    now()
                );
                self.record_planned_exit(s, planned_reason, pid, &key, recovery, None);
            }
            s.state.insert("storageBlocker".into(), json!({}));
            Self::set_observed(s, "stopped", "");
            s.state.insert("lastError".into(), json!(""));
        } else {
            s.state.insert("pendingWorkerExitIntent".into(), json!({}));
            Self::set_observed(s, "conflict", &message);
        }
        self.save_logged(s);
        let exit_code = if killed && ok {
            2
        } else if ok {
            0
        } else {
            1
        };
        merge(
            obj(
                json!({"ok": ok, "wasRunning": was_running, "exitCode": exit_code, "message": message}),
            ),
            self.status_locked(s, false),
        )
    }

    /// Replace the application; storage follows only a prepared engine.
    ///
    /// Every refusal is decided before the old worker stops (the commit
    /// point); afterwards the manager always drives a replacement to serving.
    pub fn restart(
        &self,
        server_args: Option<&Value>,
        server_env: Option<&Value>,
        source: &str,
        requested_at: Option<f64>,
        engine_override: Option<&Value>,
    ) -> Object {
        let started = Instant::now();
        let (launch_env, preparation_args, configuration) = {
            let mut s = self.lock();
            s.trace_requested_at = requested_at;
            s.state.insert(
                "lastRestart".into(),
                json!({"source": source, "outcome": "started", "startedAt": now(),
                       "requestedAt": requested_at.unwrap_or_else(now), "uncleanCloses": []}),
            );
            if server_args.is_some_and(|args| !args.is_array()) {
                return self.refuse_restart(
                    &mut s,
                    "validation",
                    "serverArgs must be a JSON array",
                    Object::new(),
                );
            }
            if server_env.is_some_and(|env| !env.is_object()) {
                return self.refuse_restart(
                    &mut s,
                    "validation",
                    "serverEnv must be a JSON object",
                    Object::new(),
                );
            }
            if server_args.is_none() && server_env.is_none() {
                let live = self.lock_status(&s);
                if live.truthy("running") && self.identity_error(&s, &live).is_none() {
                    self.reconcile_live_worker_port(&mut s, &live);
                }
            }
            let forwarded_value: Value = match server_env {
                Some(env) => env.clone(),
                None => {
                    let mut env = self.project_server_env();
                    env.extend(env_from_value(s.state.get("serverEnvOverrides")));
                    env_value(&env)
                }
            };
            let launch_args: Vec<String> = match server_args {
                Some(args) => args
                    .as_array()
                    .map(|items| items.iter().map(value_text).collect())
                    .unwrap_or_default(),
                None => s
                    .state
                    .get("serverArgs")
                    .and_then(Value::as_array)
                    .map(|items| items.iter().map(value_text).collect())
                    .unwrap_or_default(),
            };
            if launch_args.len() > 32 || launch_args.iter().any(|arg| arg.len() > 4096) {
                return self.refuse_restart(
                    &mut s,
                    "validation",
                    "serverArgs exceeds safety limit",
                    Object::new(),
                );
            }
            let (project_environment, forwarded, environment_error) =
                self.validated_server_environment(&s, forwarded_value.as_object());
            if !environment_error.is_empty() {
                return self.refuse_restart(
                    &mut s,
                    "validation",
                    &environment_error,
                    Object::new(),
                );
            }
            let launch_port = if server_args.is_some() || server_env.is_some() {
                envpolicy::server_port(&launch_args, &self.effective_env(&forwarded))
            } else {
                self.port(&s)
            };
            (
                (forwarded, project_environment, launch_port),
                launch_args,
                self.restart_configuration(&s),
            )
        };
        let (forwarded, project_environment, launch_port) = launch_env;
        let validated_override = match engine_override {
            None => None,
            Some(payload) => match certification::parse_engine_override(payload) {
                Ok(payload) => Some(payload),
                Err(error) => {
                    let mut s = self.lock();
                    return self.refuse_restart(
                        &mut s,
                        "validation",
                        &format!("the uncertified engine override is malformed: {error}"),
                        Object::new(),
                    );
                }
            },
        };
        let composition = match self.composition(&forwarded, launch_port) {
            Ok(composition) => composition,
            Err(error) => {
                let mut s = self.lock();
                return self.refuse_restart(
                    &mut s,
                    "validation",
                    &format!("launch environment could not be composed: {error}"),
                    Object::new(),
                );
            }
        };
        let mut preparation_environment = composition.environment.clone();
        let operation = format!("manager {source} before shutdown");
        let mut retention_reason = String::new();
        let engine = preparation::run_frontend_preflight(
            &self.project,
            &self.python,
            &mut preparation_environment,
            &operation,
            Parts::StorageEngine,
            validated_override.as_ref(),
        );
        let engine_decision = engine.engine_decision.clone();
        if !engine.ready {
            let mut s = self.lock();
            if engine_decision.str_of("decision") == "refused" || validated_override.is_some() {
                let message = if engine_decision.str_of("message").is_empty() {
                    engine.error.clone()
                } else {
                    engine_decision.str_of("message").to_owned()
                };
                return self.refuse_restart(
                    &mut s,
                    "storage engine",
                    &message,
                    obj(json!({"engineDecision": engine_decision})),
                );
            }
            let Some(retained) = self.storage.retained_plan() else {
                return self.refuse_restart(
                    &mut s,
                    "storage engine preparation",
                    &engine.error,
                    Object::new(),
                );
            };
            let incompatible = self.retained_engine_refusal(&retained);
            if !incompatible.is_empty() {
                return self.refuse_restart(
                    &mut s,
                    "storage engine preparation",
                    &format!("{}\n{incompatible}", engine.error),
                    Object::new(),
                );
            }
            retention_reason = format!("storage engine preparation failed: {}", engine.error)
                .chars()
                .take(1000)
                .collect();
            mlog!(
                Warning,
                "storage engine preparation failed; retaining the serving storage host and replacing only the application: {}",
                engine.error
            );
        }
        let frontend = preparation::run_frontend_preflight(
            &self.project,
            &self.python,
            &mut preparation_environment,
            &operation,
            Parts::Frontend,
            None,
        );
        if !frontend.ready {
            let mut s = self.lock();
            return self.refuse_restart(
                &mut s,
                "frontend preparation",
                &frontend.error,
                Object::new(),
            );
        }
        let prepared = match self.prepare_resident_services(
            &preparation_environment,
            composition.execution_enabled,
            !retention_reason.is_empty(),
        ) {
            Ok(prepared) => prepared,
            Err(error) => {
                let mut s = self.lock();
                return self.refuse_restart(
                    &mut s,
                    "resident preparation",
                    &format!("resident service preparation failed before shutdown: {error}"),
                    Object::new(),
                );
            }
        };
        if retention_reason.is_empty() {
            if let Err(error) = self
                .storage
                .probe_generation(prepared.storage_plan.as_ref(), &prepared.environment)
            {
                let mut s = self.lock();
                return self.refuse_restart(
                    &mut s,
                    "storage probe",
                    &format!("the new storage engine did not start and answer on a disposable authority before shutdown: {error}"),
                    Object::new(),
                );
            }
        }
        let mut s = self.lock();
        if configuration != self.restart_configuration(&s) {
            return self.refuse_restart(
                &mut s,
                "preparation",
                "Worker lifecycle changed during restart preparation; retry against current state.",
                Object::new(),
            );
        }
        let mut refusal =
            envpolicy::production_environment_error(&self.project, &prepared.environment);
        if refusal.is_empty() {
            refusal = self.resident_replacement_refusal(&prepared);
        }
        if !refusal.is_empty() {
            return self.refuse_restart(&mut s, "admission", &refusal, Object::new());
        }
        // Stop and replacement must persist this state file; a volume refusing
        // its write (EDQUOT/ENOSPC) refuses here, while the old worker serves.
        if let Err(error) = self.save(&mut s) {
            return self.refuse_restart(
                &mut s,
                "state persistence",
                &format!(
                    "the server manager cannot write its state file {}: {error}; free space on that volume and retry",
                    self.state_path.display()
                ),
                Object::new(),
            );
        }
        if server_args.is_some() {
            s.state.insert("serverArgs".into(), json!(preparation_args));
        }
        let retention = if retention_reason.is_empty() {
            json!({})
        } else {
            json!({"reason": retention_reason, "since": now(),
                   "engineSha256": prepared.storage_plan.as_ref().and_then(|p| p.get("binarySha256")).and_then(Value::as_str).unwrap_or("")})
        };
        s.state.insert("storageRetention".into(), retention);
        if let Some(Value::Object(record)) = s.state.get_mut("lastRestart") {
            if !retention_reason.is_empty() {
                record.insert("storageRetained".into(), json!(retention_reason));
            }
            if !engine_decision.is_empty() {
                record.insert(
                    "engineDecision".into(),
                    json!({"decision": engine_decision.get("decision"), "message": engine_decision.get("message")}),
                );
            }
        }
        s.project_env = project_environment.unwrap_or_default();
        s.state.insert("serverEnv".into(), env_value(&forwarded));
        let overrides = envpolicy::operator_overrides(&forwarded, &s.project_env);
        s.state
            .insert("serverEnvOverrides".into(), env_value(&overrides));
        s.state.insert("environmentQuarantine".into(), json!({}));
        self.refresh_port(&mut s);
        // The commit point.
        let stopped = self.stop_locked(&mut s, source, true);
        if !stopped.truthy("ok") {
            let message = if stopped.str_of("message").is_empty() {
                "stop refused".to_owned()
            } else {
                stopped.str_of("message").to_owned()
            };
            mlog!(
                Error,
                "worker restart failed at stop stage after {:.1}s{}: {message}",
                started.elapsed().as_secs_f64(),
                Self::trace_total_suffix(&s)
            );
            self.settle_restart(&mut s, "refused", &message, obj(json!({"stage": "stop"})));
            self.save_logged(&mut s);
            return stopped;
        }
        self.wait_for_stopped_sidecar_release(&s);
        Self::clear_failure_budget(&mut s);
        let args = s.state.get("serverArgs").cloned().unwrap_or(json!([]));
        let env = s.state.get("serverEnv").cloned().unwrap_or(json!({}));
        let mut result = self.start_locked(
            &mut s,
            Some(&args),
            Some(&env),
            source,
            true,
            Some(prepared),
        );
        let mut outcome = s.state.obj_of("lastRestart").str_of("outcome").to_owned();
        if outcome == "started" {
            outcome = if result.truthy("alreadyRunning") {
                "spawned"
            } else if s.state.str_of("desired") == "running" {
                "deferred"
            } else {
                "refused"
            }
            .to_owned();
            let message = result.str_of("message").to_owned();
            self.settle_restart(&mut s, &outcome, &message, obj(json!({"stage": "start"})));
            self.save_logged(&mut s);
        }
        let elapsed = started.elapsed().as_secs_f64();
        if outcome == "spawned" {
            let closes: String = s
                .state
                .obj_of("lastRestart")
                .get("uncleanCloses")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .map(|close| {
                            format!(
                                "; recovered {} after an unclean close",
                                close.get("service").and_then(Value::as_str).unwrap_or("")
                            )
                        })
                        .collect()
                })
                .unwrap_or_default();
            mlog!(
                Info,
                "worker restart replacement spawned in {elapsed:.1}s{}{closes}",
                Self::trace_total_suffix(&s)
            );
        } else if RESTART_IN_PROGRESS.contains(&outcome.as_str()) {
            mlog!(
                Error,
                "worker restart replacement pending after {elapsed:.1}s{}; the manager completes it automatically: {}",
                Self::trace_total_suffix(&s),
                result.str_of("message")
            );
        } else {
            mlog!(
                Error,
                "worker restart failed at start stage after {elapsed:.1}s{}: {}",
                Self::trace_total_suffix(&s),
                result.str_of("message")
            );
        }
        if s.state.truthy("storageRetention") {
            result.insert(
                "storageRetention".into(),
                s.state
                    .get("storageRetention")
                    .cloned()
                    .unwrap_or(json!({})),
            );
        }
        if !engine_decision.is_empty() {
            result.insert("engineDecision".into(), Value::Object(engine_decision));
        }
        result
    }

    pub fn status(&self, probe_health: bool) -> Object {
        let mut s = self.lock();
        self.status_locked(&mut s, probe_health)
    }

    fn status_locked(&self, s: &mut Inner, probe_health: bool) -> Object {
        let low = self.lock_status(s);
        let (conflict, foreign) = self.port_conflict(s, &low);
        let worker = Self::worker(s);
        let recent = self.recent_failure_times(s, now()).len() as i64;
        let desired = s.state.str_of("desired").to_owned();
        let launcher_alive = Self::launcher_is_alive(s);
        let mut observed = if s.state.str_of("observed").is_empty() {
            "stopped".to_owned()
        } else {
            s.state.str_of("observed").to_owned()
        };
        let mut storage_lease = Object::new();
        if desired == "running" && !low.truthy("running") && !launcher_alive {
            let candidate = self.active_storage_lease(s);
            if self.storage_lease_blocks_worker(&candidate) {
                storage_lease = candidate;
            }
        }
        let identity_error = if low.truthy("running") {
            self.identity_error(s, &low)
        } else {
            None
        };
        let foreign_owner = if desired == "running" && !low.truthy("running") && !launcher_alive {
            Self::foreign_owner(s, &low)
        } else {
            String::new()
        };
        if conflict || identity_error.is_some() || !foreign_owner.is_empty() {
            observed = "conflict".into();
        } else if low.truthy("running") {
            observed = if desired == "stopped" {
                "stopping".into()
            } else if port_accepts(self.port(s)) {
                "running".into()
            } else {
                "starting".into()
            };
        } else if desired == "stopped" {
            observed = "stopped".into();
        } else if launcher_alive {
            observed = if Self::worker_within_boot_grace(s) {
                "starting".into()
            } else {
                "degraded".into()
            };
        } else if !storage_lease.is_empty() {
            observed = if storage_lease.str_of("kind") == "offline_maintenance" {
                "maintenance".into()
            } else {
                "conflict".into()
            };
        } else if recent >= self.max_failures {
            observed = "crashloop".into();
        }
        let mut probe = Object::new();
        let (mut liveness, mut ready) = (Value::Null, Value::Null);
        if probe_health && low.truthy("running") && !conflict {
            probe = self.http_probe(s, low.i64_of("pid").unwrap_or(0));
            let live = probe.get("liveness") == Some(&json!(true));
            let is_ready = probe.get("ready") == Some(&json!(true));
            liveness = json!(live);
            ready = json!(is_ready);
            if live && is_ready && observed == "starting" {
                observed = "running".into();
            }
            if !(live && is_ready) && observed == "running" {
                observed = "degraded".into();
            }
        }
        let mut last_error = if foreign_owner.is_empty() {
            s.state.str_of("lastError").to_owned()
        } else {
            foreign_owner.clone()
        };
        if liveness == json!(true) && ready == json!(true) {
            last_error.clear();
        } else if !probe.is_empty() {
            last_error = if liveness == json!(true) {
                value_text(probe.get("readinessError").unwrap_or(&Value::Null))
            } else if probe.truthy("livenessError") {
                probe.str_of("livenessError").to_owned()
            } else {
                last_error
            };
            if liveness == json!(true) {
                let verdict = self.native_storage_verdict();
                if !verdict.is_empty() {
                    last_error = format!("{last_error}; {verdict}");
                }
            }
        }
        let execution_record = self.execution.record();
        let execution_error = if desired == "running" {
            self.execution
                .readiness_error(&execution_record, &s.state.obj_of("executionRecovery"))
        } else {
            String::new()
        };
        if !execution_error.is_empty() {
            ready = json!(false);
            last_error = execution_error.clone();
            if !matches!(observed.as_str(), "conflict" | "stopping" | "crashloop") {
                observed = "degraded".into();
            }
        }
        let pick = |record: &Object, keys: &[&str]| -> Value {
            let picked: Object = record
                .iter()
                .filter(|(key, _)| keys.contains(&key.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            if picked.is_empty() {
                Value::Null
            } else {
                Value::Object(picked)
            }
        };
        let storage_record = self.storage.record();
        let budget = self.worker_resource_budget_snapshot(low.i64_of("pid"));
        let fields = json!({
            "desired": s.state.get("desired"), "observed": observed, "port": self.port(s),
            "health": liveness, "liveness": liveness, "ready": ready,
            "probeScheme": probe.get("scheme"), "livenessError": probe.str_of("livenessError"),
            "readinessError": if execution_error.is_empty() { probe.str_of("readinessError").to_owned() } else { execution_error.clone() },
            "readinessState": probe.get("readinessState"), "storageState": probe.get("storageState"),
            "storageLease": if storage_lease.is_empty() { Value::Null } else { Value::Object(storage_lease) },
            "nativeStorageService": pick(&storage_record, &["state", "pid", "nativePid", "serviceId", "error"]),
            "executionService": pick(&execution_record, &["state", "pid", "serviceId", "error"]),
            "owner": "tofu-manager", "managerPid": std::process::id(),
            "managerImplementation": "rust",
            "resourceBudgetPolicyVersion": self.resource_budget_policy_version,
            "workerResourceBudget": budget, "worker": worker,
            "restartCount": s.state.i64_of("restartCount").unwrap_or(0),
            "workerFailureCount": s.state.i64_of("workerFailureCount").unwrap_or(0),
            "plannedExitCount": s.state.i64_of("plannedExitCount").unwrap_or(0),
            "exitAccountingSince": s.state.f64_of("exitAccountingSince"),
            "lastWorkerExitAt": s.state.f64_of("lastWorkerExitAt"),
            "lastWorkerExitKind": s.state.str_of("lastWorkerExitKind"),
            "lastWorkerExitReason": s.state.str_of("lastWorkerExitReason"),
            "consecutiveFailures": s.state.i64_of("consecutiveFailures").unwrap_or(0),
            "recentFailureCount": recent, "failureWindowSeconds": self.failure_window, "maxFailures": self.max_failures,
            "lastFailureAt": s.state.f64_of("lastFailureAt"), "lastFailureReason": s.state.str_of("lastFailureReason"),
            "lastExitCause": s.state.str_of("lastExitCause"),
            "lastWorkerProcessExit": s.state.obj_of("lastWorkerProcessExit"),
            "lastCgroupOomDelta": s.state.i64_of("lastCgroupOomDelta").unwrap_or(0),
            "cgroupOomKillCount": s.state.get("lastCgroupOomKillCount"),
            "workerBytecodeCache": s.state.obj_of("workerBytecodeCache"), "workerRuntime": s.state.obj_of("workerRuntime"),
            "runtimeSuspect": s.state.obj_of("runtimeSuspect"), "lastRecoveredAt": s.state.f64_of("lastRecoveredAt"),
            "lastRecoverySeconds": s.state.get("lastRecoverySeconds"), "nextRetryAt": s.state.f64_of("nextRetryAt"),
            "lastRestart": s.state.obj_of("lastRestart"), "storageRetention": s.state.obj_of("storageRetention"),
            "lastError": last_error, "environmentQuarantine": s.state.obj_of("environmentQuarantine"),
            "launchSource": s.state.str_of("launchSource"), "serverArgs": s.state.get("serverArgs").cloned().unwrap_or(json!([])),
            "serverEnv": s.state.obj_of("serverEnv"), "foreignListenerPids": foreign,
            "workerLog": self.worker_log.to_string_lossy(),
            "managerLog": self.logs_dir.join("server-manager.log").to_string_lossy(),
            "stateFile": self.state_path.to_string_lossy(),
        });
        merge(low, obj(fields))
    }

    // ── worker runtime replica and bytecode cache ──────────────────────

    /// `_release_worker_leases`: drop the cache and replica leases of a gone worker.
    fn release_worker_leases(s: &mut Inner) {
        s.worker_leases.clear();
    }

    /// `_restore_worker_leases`: protect an adopted worker's exact cache and runtime.
    fn restore_worker_leases(&self, s: &mut Inner) {
        if !Self::launcher_is_alive(s) {
            return;
        }
        let cache = s.state.obj_of("workerBytecodeCache");
        if cache.truthy("managed") {
            let valid = self
                .bridge
                .call(json!({"operation": "validate-bytecode-namespace",
                             "project": self.project.to_string_lossy(), "python": self.python,
                             "cacheRoot": cache.str_of("cacheRoot"), "namespace": cache.str_of("namespace")}))
                .map(|reply| reply.truthy("valid"));
            match valid.as_ref().map(|valid| {
                *valid
                    && crate::runtime::lease_bytecode_namespace(
                        cache.str_of("cacheRoot"),
                        cache.str_of("namespace"),
                    )
                    .map(|lease| s.worker_leases.push(lease))
                    .is_some()
            }) {
                Ok(true) => {}
                Ok(false) => mlog!(
                    Warning,
                    "server bytecode cache lease restore could not validate namespace"
                ),
                Err(error) => mlog!(
                    Warning,
                    "server bytecode cache lease restore skipped: {error}"
                ),
            }
        }
        let runtime = s.state.obj_of("workerRuntime");
        if runtime.truthy("replica") {
            for generation in runtime
                .get("generationPaths")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let path = value_text(&generation);
                match crate::runtime::lease_generation(Path::new(&path)) {
                    Some(lease) => s.worker_leases.push(lease),
                    None => mlog!(
                        Warning,
                        "worker runtime replica lease restore failed for {path}"
                    ),
                }
            }
        }
    }

    /// The interpreter of the next worker and its environment's bytecode prefix
    /// (`_spawn`'s replica selection and cache activation), with leases held here.
    fn select_worker_runtime(&self, s: &mut Inner, env: &mut Env) -> String {
        let suspect = s
            .state
            .obj_of("runtimeSuspect")
            .str_of("generationPath")
            .to_owned();
        let reply = self.bridge.call(json!({
            "operation": "prepare-worker-runtime", "project": self.project.to_string_lossy(),
            "python": self.python, "environment": env, "avoid": suspect,
        }));
        let reply = match reply {
            Ok(reply) => reply,
            Err(error) => {
                mlog!(Warning, "worker runtime replica selection skipped: {error}");
                s.state.insert(
                    "workerRuntime".into(),
                    json!({"replica": false, "reason": "runtime-helper-unavailable", "python": self.python}),
                );
                s.state.insert(
                    "workerBytecodeCache".into(),
                    json!({"selected": false, "managed": false, "reason": "cache-helper-unavailable"}),
                );
                return self.python.clone();
            }
        };
        let mut runtime = reply.obj_of("runtime");
        let mut worker_python = reply.str_of("workerPython").to_owned();
        if let Some(error) = runtime.remove("error") {
            mlog!(Warning, "{}", value_text(&error));
        }
        let builds: Vec<String> = reply
            .get("buildPythons")
            .and_then(Value::as_array)
            .map(|items| items.iter().map(value_text).collect())
            .unwrap_or_default();
        s.pending_runtime_build =
            (!builds.is_empty()).then(|| (reply.str_of("buildRoot").to_owned(), builds));
        if runtime.truthy("replica") {
            let generations: Vec<String> = runtime
                .get("generationPaths")
                .and_then(Value::as_array)
                .map(|items| items.iter().map(value_text).collect())
                .unwrap_or_default();
            let leases: Option<Vec<_>> = generations
                .iter()
                .map(|generation| crate::runtime::lease_generation(Path::new(generation)))
                .collect();
            match leases {
                Some(leases) if !leases.is_empty() => {
                    s.worker_leases.extend(leases);
                    if !suspect.is_empty() {
                        // A newer generation superseded the suspect one.
                        s.state.insert("runtimeSuspect".into(), json!({}));
                    }
                }
                _ => {
                    worker_python = self.python.clone();
                    runtime.insert("replica".into(), json!(false));
                    runtime.insert("reason".into(), json!("replica-lease-unavailable"));
                    runtime.insert("python".into(), json!(self.python));
                    runtime.insert("generationPaths".into(), json!([]));
                }
            }
        }
        if worker_python.is_empty() {
            worker_python = self.python.clone();
        }
        s.state
            .insert("workerRuntime".into(), Value::Object(runtime));
        let mut bytecode = reply.obj_of("bytecode");
        if let Some(error) = bytecode.remove("error") {
            mlog!(Warning, "{}", value_text(&error));
        }
        if bytecode.truthy("managed") {
            match crate::runtime::lease_bytecode_namespace(
                bytecode.str_of("cacheRoot"),
                bytecode.str_of("namespace"),
            ) {
                Some(lease) => {
                    s.worker_leases.push(lease);
                    env.insert(
                        "PYTHONPYCACHEPREFIX".into(),
                        reply.str_of("pycachePrefix").to_owned(),
                    );
                }
                None => {
                    bytecode.insert("managed".into(), json!(false));
                    bytecode.insert("reason".into(), json!("namespace-busy"));
                }
            }
        }
        s.state
            .insert("workerBytecodeCache".into(), Value::Object(bytecode));
        worker_python
    }

    /// `_note_unready_runtime`: a pre-readiness failure on a replica makes it suspect.
    fn note_unready_runtime(s: &mut Inner, reason: &str) {
        if s.state.truthy("workerReadyLoggedAt") {
            return;
        }
        let runtime = s.state.obj_of("workerRuntime");
        let first = runtime
            .get("generationPaths")
            .and_then(Value::as_array)
            .and_then(|items| items.first())
            .map(value_text);
        match first {
            Some(generation) if runtime.truthy("replica") => {
                let reason: String = reason.chars().take(500).collect();
                s.state.insert(
                    "runtimeSuspect".into(),
                    json!({"generationPath": generation, "reason": reason, "since": now()}),
                );
                mlog!(Warning, "worker failed before readiness on runtime replica {generation}; the next spawn uses the source interpreter to attribute the failure");
            }
            _ if s.state.truthy("runtimeSuspect") => {
                s.state.insert("runtimeSuspect".into(), json!({}));
                mlog!(Info, "worker also failed before readiness on the source interpreter; runtime replica suspicion cleared");
            }
            _ => {}
        }
    }

    /// `_settle_runtime_suspect`: the source interpreter became ready where the replica did not.
    fn settle_runtime_suspect(&self, s: &mut Inner) {
        let suspect = s.state.obj_of("runtimeSuspect");
        let generation = suspect.str_of("generationPath").to_owned();
        if generation.is_empty() || s.state.obj_of("workerRuntime").truthy("replica") {
            return;
        }
        s.state.insert("runtimeSuspect".into(), json!({}));
        match self.bridge.call(json!({"operation": "reject-runtime-generation",
                                      "generation": generation, "reason": suspect.str_of("reason")})) {
            Ok(_) => mlog!(Warning, "worker runtime replica {generation} rejected: it failed before readiness and the source interpreter did not"),
            Err(error) => mlog!(Warning, "worker runtime replica rejection failed: {error}"),
        }
    }

    /// `_request_pending_runtime_build`: copy replicas once the worker no longer needs the volume.
    fn request_pending_runtime_build(&self, s: &mut Inner) {
        let Some((root, pythons)) = s.pending_runtime_build.take() else {
            return;
        };
        match self
            .bridge
            .call(json!({"operation": "request-runtime-build", "root": root,
                                      "pythons": pythons, "python": self.python}))
        {
            Ok(reply) if reply.truthy("started") => mlog!(
                Info,
                "worker runtime replica build started for {} in {root}",
                pythons.join(", ")
            ),
            Ok(_) => {}
            Err(error) => mlog!(
                Warning,
                "worker runtime replica build request failed: {error}"
            ),
        }
    }

    /// `_request_bytecode_sweep`, gated here on the owner's sweep marker so a
    /// healthy monitor pass starts no helper process while the last sweep is recent.
    fn request_bytecode_sweep(&self, s: &mut Inner) {
        let cache = s.state.obj_of("workerBytecodeCache");
        if !cache.truthy("managed") || self.bytecode_sweep_marker.is_empty() {
            return;
        }
        let interval = Duration::from_secs_f64(self.bytecode_sweep_interval.max(0.0));
        if s.last_bytecode_sweep_request
            .is_some_and(|at| at.elapsed() < interval)
        {
            return;
        }
        let marker = Path::new(cache.str_of("cacheRoot"))
            .join(cache.str_of("namespace"))
            .join(&self.bytecode_sweep_marker);
        if let Ok(modified) = std::fs::symlink_metadata(&marker).and_then(|m| m.modified()) {
            if modified.elapsed().is_ok_and(|age| age < interval) {
                return;
            }
        }
        s.last_bytecode_sweep_request = Some(Instant::now());
        match self.bridge.call(json!({"operation": "request-bytecode-sweep",
                                      "cacheRoot": cache.str_of("cacheRoot"),
                                      "namespace": cache.str_of("namespace"), "python": self.python})) {
            Ok(reply) if reply.truthy("started") => mlog!(
                Info,
                "server bytecode sweep started for {}",
                cache.str_of("namespace")
            ),
            Ok(_) => {}
            Err(error) => mlog!(Warning, "server bytecode sweep request failed: {error}"),
        }
    }

    // ── recovery ───────────────────────────────────────────────────────

    fn remember_known_good(&self, s: &mut Inner) {
        match self.bridge.call(
            json!({"operation": "retain-known-good", "project": self.project.to_string_lossy()}),
        ) {
            Ok(reply) => {
                if let Some(Value::Object(mut known)) = reply.get("knownGood").cloned() {
                    if !known.is_empty() {
                        known.insert("at".into(), json!(now()));
                        s.state.insert("knownGood".into(), Value::Object(known));
                    }
                }
            }
            Err(error) => mlog!(
                Warning,
                "last known-good storage engine was not retained: {error}"
            ),
        }
        if let Some(Value::Object(record)) = s.state.get_mut("lastRestart") {
            if let Some(Value::Object(recovery)) = record.get_mut("recovery") {
                if !recovery.truthy("restoredAt") {
                    let restored = now();
                    recovery.insert("restoredAt".into(), json!(restored));
                    let at = recovery.f64_of("at");
                    let action = if recovery.str_of("action").is_empty() {
                        "storage host replaced".to_owned()
                    } else {
                        recovery.str_of("action").to_owned()
                    };
                    mlog!(
                        Info,
                        "service restored after automatic storage recovery ({action}) in {:.1}s",
                        restored - if at > 0.0 { at } else { restored }
                    );
                }
            }
        }
    }

    /// Restore service when a new generation's storage failed before it ever served.
    fn recover_generation_that_never_served(&self, s: &mut Inner, pid: i64) -> bool {
        if s.state.truthy("workerReadyLoggedAt")
            || matches!(s.state.str_of("launchSource"), "" | "adopted")
        {
            return false;
        }
        let record = self.storage.record();
        if record.str_of("state") != "failed" {
            return false;
        }
        let verdict = format!(
            "native storage host failed: {}",
            if record.truthy("error") {
                value_text(&record["error"])
            } else {
                "no error recorded".into()
            }
        );
        let mut recoveries: Vec<Value> = s
            .state
            .get("storageRecoveries")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if recoveries.len() as i64 >= self.max_failures {
            if !s
                .state
                .str_of("lastError")
                .starts_with("automatic storage recovery exhausted")
            {
                let error = format!(
                    "automatic storage recovery exhausted after {} attempts; {verdict}; explicit restart required",
                    recoveries.len()
                );
                mlog!(Error, "{error}");
                Self::set_observed(s, "degraded", &error);
                self.save_logged(s);
            }
            return true;
        }
        let at = now();
        let known = s.state.obj_of("knownGood");
        let action = match self.bridge.call(json!({"operation": "restore-known-good", "project": self.project.to_string_lossy(), "knownGood": known})) {
            Ok(reply) => reply.str_of("action").to_owned(),
            Err(error) => {
                let engine: String = known.str_of("binarySha256").chars().take(12).collect();
                mlog!(
                    Error,
                    "rollback to the last known-good storage engine {} refused: {error}; replacing the failed storage host with the current generation",
                    if engine.is_empty() { "none".into() } else { engine }
                );
                String::new()
            }
        };
        recoveries.push(json!(at));
        let attempt = recoveries.len();
        s.state
            .insert("storageRecoveries".into(), Value::Array(recoveries));
        if !s
            .state
            .get("lastRestart")
            .map(Value::is_object)
            .unwrap_or(false)
        {
            s.state.insert(
                "lastRestart".into(),
                json!({"source": "automatic", "startedAt": at}),
            );
        }
        let action_text = if action.is_empty() {
            "failed storage host and unready worker replaced".to_owned()
        } else {
            action.clone()
        };
        if let Some(Value::Object(outcome)) = s.state.get_mut("lastRestart") {
            outcome.insert(
                "recovery".into(),
                json!({"at": at, "attempt": attempt, "cause": verdict.chars().take(2000).collect::<String>(),
                       "action": action_text, "rolledBack": !action.is_empty(),
                       "knownGoodEngine": known.str_of("binarySha256")}),
            );
        }
        mlog!(
            Error,
            "worker pid={pid} never became ready: {verdict}; restoring service automatically (recovery {attempt}/{}): {}",
            self.max_failures,
            if action.is_empty() { "replacing the failed storage host and the unready worker" } else { &action }
        );
        let stopped = self.stop_locked(s, "storage-recovery", true);
        if !stopped.truthy("ok") {
            mlog!(
                Error,
                "automatic storage recovery could not stop worker pid={pid}: {}",
                stopped.str_of("message")
            );
            return true;
        }
        self.wait_for_stopped_sidecar_release(s);
        let result = self.spawn(s, "storage-recovery", None, true, false);
        if let Some(Value::Object(outcome)) = s.state.get_mut("lastRestart") {
            if let Some(Value::Object(recovery)) = outcome.get_mut("recovery") {
                recovery.insert(
                    "spawn".into(),
                    json!(result
                        .str_of("message")
                        .chars()
                        .take(500)
                        .collect::<String>()),
                );
            }
        }
        self.save_logged(s);
        true
    }

    /// Recover a departed manager's host without changing a live app's engine.
    fn recover_native_storage_service(
        &self,
        s: &mut Inner,
        worker_pid: i64,
    ) -> Result<bool, String> {
        let mut record = self.storage.record();
        if record.is_empty() || self.storage.host_matches(&record) {
            return Ok(true);
        }
        if record.str_of("state") == "failed" {
            return Err(if record.truthy("error") {
                value_text(&record["error"])
            } else {
                "native storage failed; explicit restart required".into()
            });
        }
        if self.active_storage_lease(s).truthy("held") {
            return Ok(true);
        }
        let me = std::process::id() as i64;
        if record.i64_of("managerPid") == Some(me)
            && resident::process_matches(Some(me), record.i64_of("managerStartTime"))
        {
            let error = "native storage host exited unexpectedly; explicit restart required";
            record.insert("state".into(), json!("failed"));
            record.insert("error".into(), json!(error));
            let _ = json::write_private_atomic(&self.storage.state_path, &Value::Object(record));
            return Err(error.into());
        }
        let environment = self.worker_environment(&Self::server_env(s), self.port(s))?;
        let plan = self.storage.prepare(&environment)?;
        if let Some(plan) = plan
            .as_ref()
            .filter(|plan| record.get("plan") == Some(*plan))
        {
            self.storage
                .ensure(plan, &environment)
                .map_err(|e| e.to_string())?;
            return Ok(true);
        }
        if record.str_of("host") != hostname()
            || resident::process_matches(
                record.i64_of("managerPid"),
                record.i64_of("managerStartTime"),
            )
            || procfs::env_value(worker_pid, "TOFU_STORAGE_CONNECTION_FILE").as_deref()
                != Some(record.str_of("connectionFile"))
        {
            return Err(
                "native storage configuration changed; drain/restart the worker to replace it"
                    .into(),
            );
        }
        Ok(false)
    }

    /// `ExecutionService.recover` with the manager's shared failure budget.
    fn recover_execution_service(&self, s: &mut Inner) -> bool {
        let record = self.execution.record();
        if record.is_empty() {
            return true;
        }
        if self.execution.host_matches(&record) {
            if record.str_of("state") == "ready" {
                s.state.insert("executionRecovery".into(), json!({}));
                return true;
            }
            let error = if record.truthy("error") {
                value_text(&record["error"])
            } else {
                "execution service is starting".into()
            };
            Self::set_observed(s, "degraded", &error);
            return false;
        }
        let mut recovery = s.state.obj_of("executionRecovery");
        if recovery.get("serviceId") != record.get("serviceId") {
            let (at, recent, consecutive) = self.account_failure(s);
            let exhausted = recent >= self.max_failures;
            let error = if exhausted {
                "execution recovery paused; explicit restart required"
            } else {
                "execution service exited; waiting for recovery"
            };
            recovery = obj(
                json!({"serviceId": record.get("serviceId"), "retryAt": at + Self::failure_retry_delay(consecutive, recent),
                                  "requiresRestart": exhausted, "error": error}),
            );
            s.state
                .insert("executionRecovery".into(), Value::Object(recovery.clone()));
            s.state.insert("lastFailureReason".into(), json!(error));
            s.state
                .insert("lastExitCause".into(), json!("execution_exit"));
        }
        if recovery.truthy("requiresRestart") || now() < recovery.f64_of("retryAt") {
            let observed = if recovery.truthy("requiresRestart") {
                "crashloop"
            } else {
                "degraded"
            };
            Self::set_observed(s, observed, recovery.str_of("error"));
            return false;
        }
        let outcome = (|| -> Result<(), ServiceError> {
            let failed = ServiceError::Failed;
            let mut environment = self
                .worker_environment(&Self::server_env(s), self.port(s))
                .map_err(failed)?;
            environment
                .entry("TOFU_STORAGE_CONNECTION_FILE".into())
                .or_insert_with(|| self.storage.result_path());
            let engine = s
                .state
                .obj_of("storageRetention")
                .str_of("engineSha256")
                .to_owned();
            if !engine.is_empty() {
                environment.insert(RETAINED_ENGINE_ENVIRONMENT.into(), engine);
            }
            let plan = self
                .execution
                .prepare(&environment)
                .map_err(ServiceError::Failed)?
                .unwrap_or(json!({}));
            let original = record.get("plan").cloned().unwrap_or(json!({}));
            let unentered_source_change = record.str_of("state") == "failed"
                && record.str_of("failureKind") == SOURCE_CHANGED_BEFORE_BOOT
                && self
                    .bridge
                    .call(json!({"operation": "source-only-change", "expected": original, "actual": plan}))
                    .map(|reply| reply.truthy("sourceOnly"))
                    .unwrap_or(false);
            if original != plan && !unentered_source_change {
                let (a, b) = (
                    original.as_object().cloned().unwrap_or_default(),
                    plan.as_object().cloned().unwrap_or_default(),
                );
                let changed: BTreeSet<&String> = a
                    .keys()
                    .chain(b.keys())
                    .filter(|key| a.get(*key) != b.get(*key))
                    .collect();
                let changed: Vec<&str> = changed.into_iter().map(String::as_str).collect();
                return Err(ServiceError::Failed(format!(
                    "execution configuration or code changed; explicit restart required (changed fields: {})",
                    changed.join(", ")
                )));
            }
            if unentered_source_change && !self.storage_retention_active(s) {
                let env = self
                    .worker_environment(&Self::server_env(s), self.port(s))
                    .map_err(ServiceError::Failed)?;
                let storage_plan = self.storage.prepare(&env).map_err(ServiceError::Failed)?;
                if self.storage.needs_replacement(storage_plan.as_ref(), false) {
                    return Err(ServiceError::Failed(
                        "storage generation changed; explicit restart required".into(),
                    ));
                }
            }
            self.execution
                .stop(NATIVE_SERVICE_STOP_SECONDS, false, &self.bridge)?;
            self.execution.ensure(&plan, &environment)?;
            Ok(())
        })();
        match outcome {
            Err(ServiceError::Draining(error)) => {
                let since = match recovery.get("drainingSince").and_then(Value::as_f64) {
                    Some(since) => since,
                    None => {
                        let since = now();
                        recovery.insert("drainingSince".into(), json!(since));
                        since
                    }
                };
                if now() - since < NATIVE_SERVICE_STOP_SECONDS {
                    s.state
                        .insert("executionRecovery".into(), Value::Object(recovery));
                    Self::set_observed(
                        s,
                        "degraded",
                        &format!("execution service is still exiting: {error}"),
                    );
                    return false;
                }
                let message = format!("execution recovery failed: {error}");
                recovery.insert("requiresRestart".into(), json!(true));
                recovery.insert("error".into(), json!(message));
                s.state
                    .insert("executionRecovery".into(), Value::Object(recovery));
                Self::set_observed(s, "degraded", &message);
                false
            }
            Err(ServiceError::Failed(error)) => {
                let message = format!("execution recovery failed: {error}");
                recovery.insert("requiresRestart".into(), json!(true));
                recovery.insert("error".into(), json!(message));
                s.state
                    .insert("executionRecovery".into(), Value::Object(recovery));
                Self::set_observed(s, "degraded", &message);
                false
            }
            Ok(()) => {
                Self::set_observed(s, "degraded", "execution service is starting");
                false
            }
        }
    }

    fn record_failure(
        &self,
        s: &mut Inner,
        exit_cause: &str,
        failure_reason: &str,
        exited_at: Option<f64>,
    ) {
        if s.state.str_of("observed") == "crashloop" {
            return;
        }
        let retry_at = s.state.f64_of("nextRetryAt");
        if retry_at > 0.0 {
            if now() < retry_at {
                self.save_logged(s);
                return;
            }
            s.state.insert("nextRetryAt".into(), json!(0.0));
            if self.lock_status(s).truthy("running") {
                self.save_logged(s);
                return;
            }
            self.spawn(s, "automatic-recovery", None, false, false);
            return;
        }
        let (at, recent, failures, retired) =
            self.account_worker_failure(s, exit_cause, failure_reason, exited_at);
        if recent >= self.max_failures {
            Self::set_observed(
                s,
                "crashloop",
                &format!(
                    "worker failed {recent} times in {:.0}s; recovery paused",
                    self.failure_window
                ),
            );
            s.state.insert("nextRetryAt".into(), json!(0.0));
            self.save_logged(s);
            Self::reap_retired_worker(s, &retired);
            mlog!(
                Error,
                "worker recovery paused after {recent} failures in {:.0}s; explicit start/restart is required to reset the crash-loop budget",
                self.failure_window
            );
            return;
        }
        let delay = Self::failure_retry_delay(failures, recent);
        s.state.insert("nextRetryAt".into(), json!(at + delay));
        Self::set_observed(
            s,
            "starting",
            &format!("worker exited; retrying in {delay:.0}s"),
        );
        self.save_logged(s);
        Self::reap_retired_worker(s, &retired);
    }

    pub fn reconcile(&self) {
        let mut guard = self.lock();
        let s = &mut *guard;
        self.maintain_process_logs(s, false);
        reap_children_of_previous_image(Self::worker(s).i64_of("pid"));
        let mut status = self.lock_status(s);
        if !status.truthy("running") && s.state.str_of("desired") == "stopped" {
            if let Some(remembered) = self.remembered_worker_status(s) {
                status = remembered;
            }
        }
        let desired = s.state.str_of("desired").to_owned();
        if status.truthy("running") {
            if let Some(error) = self.identity_error(s, &status) {
                Self::set_observed(s, "conflict", &error);
                self.save_logged(s);
                return;
            }
            self.reconcile_live_worker_port(s, &status);
        }
        let (conflict, foreign) = self.port_conflict(s, &status);
        if conflict {
            let owner = if foreign.is_empty() {
                "an unknown process".to_owned()
            } else {
                format!("pid(s) {foreign:?}")
            };
            let message = format!("port {} owned by {owner}", self.port(s));
            Self::set_observed(s, "conflict", &message);
            self.save_logged(s);
            return;
        }
        if desired == "stopped" {
            if status.truthy("running") {
                s.state.insert(
                    "pendingWorkerExitIntent".into(),
                    json!({"pid": status.get("pid"), "host": hostname(), "reason": "manual", "requestedAt": now(), "recoverySource": ""}),
                );
                Self::set_observed(s, "stopping", "");
                self.save_logged(s);
                let (ok, _, error) = self.terminate(s, &status);
                if !ok {
                    Self::set_observed(s, "conflict", &error);
                    self.save_logged(s);
                    return;
                }
            }
            self.settle_departed_worker(s);
            if let Err(error) = self.stop_resident_services(s) {
                Self::set_observed(
                    s,
                    "conflict",
                    &format!("resident service shutdown incomplete: {error}"),
                );
                self.save_logged(s);
                return;
            }
            s.state.insert("pendingWorkerExitIntent".into(), json!({}));
            s.state.insert("storageBlocker".into(), json!({}));
            Self::set_observed(s, "stopped", "");
            self.save_logged(s);
            return;
        }
        if status.truthy("running") {
            let pid = status.i64_of("pid").unwrap_or(0);
            match self.recover_native_storage_service(s, pid) {
                Err(error) => {
                    Self::set_observed(
                        s,
                        "degraded",
                        &format!("native storage recovery failed: {error}"),
                    );
                    self.save_logged(s);
                    return;
                }
                Ok(false) => {
                    mlog!(
                        Warning,
                        "native storage host of a departed manager cannot return at worker pid={pid} generation; draining the worker to start the current storage generation"
                    );
                    self.stop_locked(s, "native-storage-recovery", true);
                    return;
                }
                Ok(true) => {}
            }
            Self::remember_worker(s, &status, "");
            let execution_ready = self.recover_execution_service(s);
            let probe = self.http_probe(s, pid);
            if probe.truthy("liveness") && probe.truthy("ready") {
                let unready_since = s.state.f64_of("unreadySince");
                if unready_since > 0.0 {
                    s.state.insert("unreadySince".into(), json!(0.0));
                    mlog!(
                        Info,
                        "worker pid={pid} is ready again after {:.0}s unready",
                        (now() - unready_since).max(0.0)
                    );
                }
                if !execution_ready {
                    self.save_logged(s);
                    return;
                }
                let recovered_from = s.state.f64_of("activeFailureAt");
                let at = now();
                if recovered_from > 0.0 {
                    let elapsed = (at - recovered_from).max(0.0);
                    s.state.insert("lastRecoveredAt".into(), json!(at));
                    s.state.insert(
                        "lastRecoverySeconds".into(),
                        json!((elapsed * 1000.0).round() / 1000.0),
                    );
                    s.state.insert("activeFailureAt".into(), json!(0.0));
                    mlog!(
                        Info,
                        "worker recovered pid={pid} in {elapsed:.3}s after failure"
                    );
                }
                if !s.state.truthy("workerReadyLoggedAt") {
                    s.state.insert("workerReadyLoggedAt".into(), json!(at));
                    let spawned = Self::worker(s).f64_of("spawnedAt");
                    let boot = (spawned > 0.0).then(|| (at - spawned).max(0.0));
                    let launch = if s.state.str_of("launchSource").is_empty() {
                        "unknown".to_owned()
                    } else {
                        s.state.str_of("launchSource").to_owned()
                    };
                    mlog!(
                        Info,
                        "worker ready pid={pid} source={launch}{}{}",
                        boot.map(|b| format!(" bootTime={b:.1}s"))
                            .unwrap_or_default(),
                        Self::trace_total_suffix(s)
                    );
                    let since_request = s
                        .trace_requested_at
                        .filter(|r| *r > 0.0)
                        .map(|r| ((now() - r) * 10.0).round() / 10.0);
                    self.record_lead_audit(
                        "restart_outcome",
                        &obj(json!({"outcome": "ready", "pid": pid, "source": launch,
                                    "bootSeconds": boot.map(|b| (b * 10.0).round() / 10.0), "secondsSinceRequest": since_request})),
                    );
                    self.settle_runtime_suspect(s);
                    self.request_pending_runtime_build(s);
                    self.remember_known_good(s);
                }
                let history = self.recent_failure_times(s, at);
                s.state.insert("failureHistory".into(), json!(history));
                if let Some(count) = cgroup_oom_kill_count() {
                    s.state
                        .insert("lastCgroupOomKillCount".into(), json!(count));
                }
                s.state.insert("consecutiveFailures".into(), json!(0));
                s.state.insert("nextRetryAt".into(), json!(0.0));
                s.state.insert("wedgeSince".into(), json!(0.0));
                s.state.insert("lastError".into(), json!(""));
                Self::set_observed(s, "running", "");
                self.save_logged(s);
                self.request_bytecode_sweep(s);
                return;
            }
            if probe.truthy("liveness") {
                s.state.insert("wedgeSince".into(), json!(0.0));
                if self.recover_generation_that_never_served(s, pid) {
                    return;
                }
                let error = if probe.truthy("readinessError") {
                    probe.str_of("readinessError").to_owned()
                } else {
                    "application readiness probe failed".into()
                };
                self.note_unready_worker(s, pid, &error);
                self.save_logged(s);
                return;
            }
            let Some(age) = self.heartbeat_stale_age(s, pid) else {
                let error = if probe.truthy("livenessError") {
                    probe.str_of("livenessError").to_owned()
                } else {
                    "identity liveness failed".into()
                };
                Self::set_observed(s, "degraded", &format!("{error}; heartbeat not stale"));
                self.save_logged(s);
                return;
            };
            let first = s.state.f64_of("wedgeSince");
            if first == 0.0 {
                s.state.insert("wedgeSince".into(), json!(now()));
                Self::set_observed(
                    s,
                    "degraded",
                    &format!("possible wedge; heartbeat stale {age:.0}s"),
                );
                self.save_logged(s);
                return;
            }
            if now() - first < DEFAULT_WEDGE_STREAK {
                self.save_logged(s);
                return;
            }
            let error = format!(
                "worker pid {pid} preserved during event-loop stall; health probes and heartbeat remain unavailable; waiting for in-place recovery"
            );
            if s.state.str_of("lastError") != error {
                mlog!(Error, "{error} (heartbeat stale {age:.0}s)");
            }
            Self::set_observed(s, "degraded", &error);
            self.save_logged(s);
            return;
        }
        if Self::launcher_is_alive(s) {
            if Self::worker_within_boot_grace(s) {
                Self::set_observed(s, "starting", "");
                self.save_logged(s);
                return;
            }
            let error = format!(
                "worker pid {} is alive but holds no instance lock {DEFAULT_BOOT_GRACE:.0}s after spawn; retained, no second worker is started",
                value_text(&Self::worker(s)["pid"])
            );
            if s.state.str_of("lastError") != error {
                mlog!(Error, "{error}");
            }
            Self::set_observed(s, "degraded", &error);
            self.save_logged(s);
            return;
        }
        let foreign_owner = Self::foreign_owner(s, &status);
        if !foreign_owner.is_empty() {
            if s.state.str_of("lastError") != foreign_owner {
                mlog!(Warning, "{foreign_owner}");
            }
            Self::set_observed(s, "conflict", &foreign_owner);
            return;
        }
        let prior_blocker = s.state.obj_of("storageBlocker");
        if !prior_blocker.is_empty() {
            let lease = self.active_storage_lease(s);
            if self.storage_lease_blocks_worker(&lease) {
                self.defer_for_storage_lease(s, &lease, prior_blocker.str_of("resumeSource"));
                return;
            }
            s.state.insert("storageBlocker".into(), json!({}));
            s.state.insert("lastError".into(), json!(""));
            let source = [
                prior_blocker.str_of("resumeSource"),
                s.state.str_of("pendingRecoverySource"),
            ]
            .into_iter()
            .find(|value| !value.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                if prior_blocker.str_of("kind") == "offline_maintenance" {
                    "storage-maintenance-complete"
                } else {
                    "storage-lease-released"
                }
                .to_owned()
            });
            let retry_at = s.state.f64_of("nextRetryAt");
            if retry_at > 0.0 && now() < retry_at {
                Self::set_observed(
                    s,
                    "starting",
                    &format!("storage lease released; retrying at {retry_at:.3}"),
                );
                self.save_logged(s);
                return;
            }
            if retry_at > 0.0 {
                s.state.insert("nextRetryAt".into(), json!(0.0));
            }
            self.spawn(s, &source, None, false, false);
            return;
        }
        let pending = s.state.str_of("pendingRecoverySource").to_owned();
        if !pending.is_empty() {
            let lease = self.wait_for_stopped_sidecar_release(s);
            if self.storage_lease_blocks_worker(&lease) {
                self.defer_for_storage_lease(s, &lease, &pending);
                return;
            }
            self.spawn(s, &pending, None, false, false);
            return;
        }
        let departed = self.departed_worker_exit(s);
        if departed.str_of("kind") == "planned" {
            let reason = departed.str_of("reason").to_owned();
            let recovery = departed.str_of("recoverySource").to_owned();
            if reason == "manual" {
                s.state.insert("desired".into(), json!("stopped"));
            }
            self.record_planned_exit(
                s,
                &reason,
                departed.i64_of("pid"),
                departed.str_of("markerKey"),
                &recovery,
                departed.get("exitedAt").and_then(Value::as_f64),
            );
            if reason == "manual" {
                s.state.insert("storageBlocker".into(), json!({}));
                Self::set_observed(s, "stopped", "");
                s.state.insert("lastError".into(), json!(""));
                self.save_logged(s);
                return;
            }
            let lease = self.wait_for_stopped_sidecar_release(s);
            if self.storage_lease_blocks_worker(&lease) {
                self.defer_for_storage_lease(s, &lease, &recovery);
            } else {
                self.spawn(s, &recovery, None, false, false);
            }
            return;
        }
        if departed.truthy("failureReason") {
            s.state
                .insert("lastError".into(), json!(departed.str_of("failureReason")));
        }
        self.record_failure(
            s,
            departed.str_of("exitCause"),
            departed.str_of("failureReason"),
            departed.get("exitedAt").and_then(Value::as_f64),
        );
        if s.state.str_of("observed") == "crashloop" || Self::launcher_is_alive(s) {
            return;
        }
        let lease = self.wait_for_stopped_sidecar_release(s);
        if self.storage_lease_blocks_worker(&lease) {
            self.defer_for_storage_lease(s, &lease, "automatic-recovery");
        }
    }

    /// Certify HEAD's storage engine in the background once HEAD moves.
    fn precertify_engine_after_head_moves(&self) {
        let reflog = {
            let mut s = self.lock();
            if s.head_reflog.is_none() {
                let path = std::process::Command::new("git")
                    .args([
                        "-C",
                        &self.project.to_string_lossy(),
                        "rev-parse",
                        "--git-path",
                        "logs/HEAD",
                    ])
                    .output()
                    .ok()
                    .filter(|output| output.status.success())
                    .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
                    .unwrap_or_default();
                s.head_reflog = Some(if path.is_empty() {
                    PathBuf::new()
                } else {
                    self.project.join(path)
                });
            }
            let reflog = s.head_reflog.clone().unwrap_or_default();
            if reflog.as_os_str().is_empty() {
                return;
            }
            let Ok(metadata) = std::fs::metadata(&reflog) else {
                return;
            };
            use std::os::unix::fs::MetadataExt;
            let observed = (
                metadata.size(),
                metadata.mtime_nsec() + metadata.mtime() * 1_000_000_000,
            );
            if s.head_reflog_seen == Some(observed) || self.precertifying.load(Ordering::SeqCst) {
                return;
            }
            s.head_reflog_seen = Some(observed);
            reflog
        };
        let _ = reflog;
        self.precertifying.store(true, Ordering::SeqCst);
        let flag = self.precertifying.clone();
        let bridge = self.bridge.clone();
        let (project, python) = (self.project.clone(), self.python.clone());
        std::thread::Builder::new()
            .name("tofu-engine-precertify".into())
            .spawn(move || {
                if let Ok(reply) = bridge.call(json!({"operation": "precertify", "project": project.to_string_lossy(), "python": python})) {
                    let result = reply.obj_of("result");
                    if result.truthy("started") {
                        mlog!(
                            Info,
                            "background storage engine certification started pid={}: {}; log {}",
                            value_text(result.get("pid").unwrap_or(&Value::Null)),
                            value_text(result.get("reason").unwrap_or(&Value::Null)),
                            value_text(result.get("log").unwrap_or(&Value::Null))
                        );
                    }
                }
                flag.store(false, Ordering::SeqCst);
            })
            .ok();
    }

    /// One monitor thread per project; it reconciles every interval.
    pub fn monitor_loop(self: Arc<Self>) {
        {
            let mut s = self.lock();
            self.maintain_process_logs(&mut s, true);
        }
        while !self.closed.load(Ordering::SeqCst) {
            sleep(self.monitor_interval);
            if self.closed.load(Ordering::SeqCst) {
                break;
            }
            let outcome =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.reconcile()));
            match outcome {
                Ok(()) => {
                    let mut s = self.lock();
                    s.last_monitor_error.clear();
                    s.monitor_error_count = 0;
                }
                Err(panic) => {
                    let text = panic
                        .downcast_ref::<String>()
                        .cloned()
                        .or_else(|| panic.downcast_ref::<&str>().map(|t| t.to_string()))
                        .unwrap_or_else(|| "panic".into());
                    let mut s = self.lock();
                    Self::set_observed(&mut s, "degraded", &format!("monitor error: {text}"));
                    let saved = self.save(&mut s);
                    let signature: String = text.chars().take(240).collect();
                    if signature == s.last_monitor_error {
                        s.monitor_error_count += 1;
                    } else {
                        s.last_monitor_error = signature;
                        s.monitor_error_count = 1;
                    }
                    let occurrence = s.monitor_error_count;
                    if occurrence.is_power_of_two() {
                        mlog!(
                            Error,
                            "lifecycle monitor reconcile failed (occurrences={occurrence}, degraded_state_saved={}): {text}",
                            if saved.is_ok() { "True" } else { "False" }
                        );
                        if let Err(error) = saved {
                            mlog!(
                                Warning,
                                "lifecycle monitor could not persist degraded state: {error}"
                            );
                        }
                    }
                }
            }
            self.precertify_engine_after_head_moves();
        }
    }

    pub fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
    }

    /// Writer descriptors of this manager's hosts that must survive `/reload`.
    pub fn exec_retained_descriptors(&self) -> Vec<i32> {
        [
            self.storage.exec_retained_descriptor(),
            self.execution.exec_retained_descriptor(),
        ]
        .into_iter()
        .flatten()
        .collect()
    }

    /// Whether a worker serves (used by ownership loss retirement).
    pub fn worker_running(&self) -> bool {
        let s = self.lock();
        self.lock_status(&s).truthy("running")
    }

    /// Exposed for tests.
    pub fn state_snapshot(&self) -> Object {
        self.lock().state.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::truthy;

    #[test]
    fn retry_delay_doubles_to_the_minute_cap() {
        assert_eq!(LifecycleManager::failure_retry_delay(1, 1), 2.0);
        assert_eq!(LifecycleManager::failure_retry_delay(1, 3), 8.0);
        assert_eq!(LifecycleManager::failure_retry_delay(9, 2), 60.0);
    }

    #[test]
    fn storage_blocker_messages_name_the_holder() {
        let lease =
            obj(json!({"label": "Storage sidecar", "pid": 42, "kind": "storage_authority_sql"}));
        assert_eq!(
            LifecycleManager::storage_blocker_message(&lease),
            "Storage sidecar (PID 42) holds the storage lease; refusing to start a second storage authority"
        );
        let maintenance =
            obj(json!({"label": "SQLite deep clean", "host": "h", "kind": "offline_maintenance"}));
        assert!(LifecycleManager::storage_blocker_message(&maintenance)
            .ends_with("resume automatically after maintenance"));
    }

    #[test]
    fn value_text_matches_python_str_for_scalars() {
        assert_eq!(value_text(&json!(null)), "None");
        assert_eq!(value_text(&json!(true)), "True");
        assert_eq!(value_text(&json!(5)), "5");
        assert_eq!(value_text(&json!("x")), "x");
    }

    #[test]
    fn truthiness_follows_python() {
        assert!(!truthy(Some(&json!({}))));
        assert!(truthy(Some(&json!({"a": 1}))));
        assert!(!truthy(Some(&json!(0.0))));
    }
}
