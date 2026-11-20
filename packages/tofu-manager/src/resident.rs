//! Manager ownership of resident hosts (`serverctl_pkg/resident_service.py`).
//!
//! One process lease retained across API restarts and manager exec. This
//! owner alone publishes PID evidence, signals and proves physical drain:
//! the host inherits a private lease (flock) and the reader of a pipe whose
//! writer only this manager keeps, so manager death is the host's EOF.
//! Policies (storage, execution) supply identity, launch and preparation.

use crate::bridge::Bridge;
use crate::child::{self, Spec};
use crate::envpolicy::Env;
use crate::json::{read_object, write_private_atomic, Object, ObjectExt};
use crate::procfs;
use crate::timing::{sleep, Deadline};
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

/// `restart_budget.NATIVE_SERVICE_STOP_SECONDS`.
pub const NATIVE_SERVICE_STOP_SECONDS: f64 = 100.0;
/// `restart_budget.NATIVE_SERVICE_PREPARATION_SECONDS`.
pub const NATIVE_SERVICE_PREPARATION_SECONDS: f64 = 120.0;
/// `restart_budget.NATIVE_GENERATION_PROBE_SECONDS`.
pub const NATIVE_GENERATION_PROBE_SECONDS: f64 = NATIVE_SERVICE_PREPARATION_SECONDS;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Storage,
    Execution,
}

#[derive(Debug)]
pub enum ServiceError {
    /// The previous host still holds its lease; it has not finished exiting.
    Draining(String),
    Failed(String),
}

impl std::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ServiceError::Draining(message) | ServiceError::Failed(message) => f.write_str(message),
        }
    }
}

pub struct ResidentService {
    pub kind: Kind,
    pub project: PathBuf,
    pub python: String,
    pub state_path: PathBuf,
    pub lock_path: PathBuf,
    process: Mutex<Option<Child>>,
}

pub fn process_matches(pid: Option<i64>, ticks: Option<i64>) -> bool {
    match (pid, ticks) {
        (Some(pid), Some(ticks)) if pid > 0 && ticks > 0 => {
            procfs::pid_is_alive(pid) && procfs::start_ticks(pid) == Some(ticks)
        }
        _ => false,
    }
}

/// Verify an endpoint against its private kernel pipe identity.
pub fn manager_control_matches(record: &Object, descriptor: i64, write: bool) -> bool {
    let control = record.obj_of("managerControl");
    if control.is_empty() || descriptor < 0 || descriptor > i32::MAX as i64 {
        return false;
    }
    let fd = descriptor as i32;
    let mut info: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut info) } != 0 {
        return false;
    }
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return false;
    }
    (info.st_mode & libc::S_IFMT) == libc::S_IFIFO
        && control.get("device").and_then(Value::as_u64) == Some(info.st_dev)
        && control.get("inode").and_then(Value::as_u64) == Some(info.st_ino)
        && (flags & libc::O_ACCMODE)
            == if write {
                libc::O_WRONLY
            } else {
                libc::O_RDONLY
            }
}

fn manager_identity() -> (i64, Option<i64>) {
    let me = std::process::id() as i64;
    (me, procfs::start_ticks(me))
}

/// Only this manager may close the exact writer it published, including after exec.
pub fn close_manager_control(record: &Object) {
    let (me, ticks) = manager_identity();
    if record.i64_of("managerPid") != Some(me)
        || !process_matches(Some(me), record.i64_of("managerStartTime"))
        || ticks.is_none()
    {
        return;
    }
    let descriptor = record.obj_of("managerControl").i64_of("fd").unwrap_or(-1);
    if manager_control_matches(record, descriptor, true) {
        unsafe {
            libc::close(descriptor as i32);
        }
    }
}

/// The writer descriptor this manager published, if it still holds it.
pub fn owned_control_descriptor(record: &Object) -> Option<i32> {
    let (me, _) = manager_identity();
    if record.i64_of("managerPid") != Some(me) {
        return None;
    }
    let descriptor = record.obj_of("managerControl").i64_of("fd")?;
    manager_control_matches(record, descriptor, true).then_some(descriptor as i32)
}

pub fn host_matches(record: &Object, host_module: &str) -> bool {
    if record.str_of("host") != procfs::hostname() {
        return false;
    }
    let pid = record.i64_of("pid");
    if !process_matches(pid, record.i64_of("processStartTime")) {
        return false;
    }
    let Some(args) = procfs::cmdline_args(pid.unwrap()) else {
        return false;
    };
    let Some(index) = args
        .iter()
        .position(|arg| arg.as_slice() == b"--service-id")
    else {
        return false;
    };
    args.iter()
        .any(|arg| arg.as_slice() == host_module.as_bytes())
        && args.get(index + 1).map(|id| id.as_slice())
            == Some(record.str_of("serviceId").as_bytes())
}

/// `resident_service.read_record`: private, bounded, of the expected format.
pub fn read_record_file(path: &Path, record_format: &str) -> Object {
    let Ok(info) = std::fs::symlink_metadata(path) else {
        return Object::new();
    };
    if !info.file_type().is_file()
        || info.mode() & 0o077 != 0
        || info.len() == 0
        || info.len() > 65536
    {
        return Object::new();
    }
    let record = read_object(path);
    if record.str_of("format") == record_format {
        record
    } else {
        Object::new()
    }
}

fn acquire_service_lock(path: &Path) -> Result<File, ServiceError> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| {
            ServiceError::Failed(format!("resident service lock unavailable: {error}"))
        })?;
    let info = file
        .metadata()
        .map_err(|error| ServiceError::Failed(error.to_string()))?;
    if !info.file_type().is_file()
        || info.uid() != unsafe { libc::geteuid() }
        || info.mode() & 0o077 != 0
    {
        return Err(ServiceError::Failed(
            "resident service lock is not private".into(),
        ));
    }
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(ServiceError::Draining(
            "resident service owner is still draining".into(),
        ));
    }
    Ok(file)
}

fn new_service_id() -> String {
    let mut bytes = [0u8; 16];
    if let Ok(mut random) = File::open("/dev/urandom") {
        use std::io::Read;
        let _ = random.read_exact(&mut bytes);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn clear_cloexec(fd: i32) {
    unsafe {
        let flags = libc::fcntl(fd, libc::F_GETFD);
        if flags >= 0 {
            libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC);
        }
    }
}

impl ResidentService {
    pub fn new(kind: Kind, project: &Path, python: &str) -> Self {
        let state_name = match kind {
            Kind::Storage => "native-storage-service",
            Kind::Execution => "execution-service",
        };
        ResidentService {
            kind,
            project: project.to_path_buf(),
            python: python.to_owned(),
            state_path: project.join("data").join(format!("{state_name}.json")),
            lock_path: project.join("data").join(format!(".{state_name}.lock")),
            process: Mutex::new(None),
        }
    }

    pub fn service_name(&self) -> &'static str {
        match self.kind {
            Kind::Storage => "native storage",
            Kind::Execution => "execution",
        }
    }

    pub fn record_format(&self) -> &'static str {
        match self.kind {
            Kind::Storage => "tofu.native-storage-service/v1",
            Kind::Execution => "tofu.execution-service/v1",
        }
    }

    pub fn host_module(&self) -> &'static str {
        match self.kind {
            Kind::Storage => "serverctl_pkg.native_storage_host",
            Kind::Execution => "serverctl_pkg.execution_host",
        }
    }

    pub fn connection_path(&self) -> PathBuf {
        self.project.join("data/native-storage-connection.json")
    }

    fn identity_fields(&self) -> Object {
        let mut fields = Object::new();
        if self.kind == Kind::Storage {
            fields.insert(
                "connectionFile".into(),
                json!(self.connection_path().to_string_lossy()),
            );
        }
        fields
    }

    pub fn result_path(&self) -> String {
        match self.kind {
            Kind::Storage => self.connection_path().to_string_lossy().into_owned(),
            Kind::Execution => self.state_path.to_string_lossy().into_owned(),
        }
    }

    pub fn host_matches(&self, record: &Object) -> bool {
        host_matches(record, self.host_module())
    }

    pub fn process_environment(&self, environment: &Env) -> Env {
        let mut env = environment.clone();
        match self.kind {
            Kind::Storage => {
                env.remove("TOFU_STORAGE_CONNECTION_FILE");
            }
            Kind::Execution => {
                env.insert("TOFU_PROCESS_ROLE".into(), "execution".into());
            }
        }
        env
    }

    fn launch_python(&self, plan: &Value) -> String {
        match self.kind {
            Kind::Execution => plan
                .get("python")
                .and_then(Value::as_str)
                .filter(|python| !python.is_empty())
                .unwrap_or(&self.python)
                .to_owned(),
            Kind::Storage => self.python.clone(),
        }
    }

    /// `ExecutionService._preparation_python`: the installed marker's interpreter.
    fn preparation_python(&self) -> String {
        let marker = read_object(&self.project.join(".tofu_env.json"));
        let candidate = marker.str_of("python");
        let executable = !candidate.is_empty()
            && std::ffi::CString::new(candidate)
                .map(|path| unsafe { libc::access(path.as_ptr(), libc::X_OK) } == 0)
                .unwrap_or(false);
        if executable {
            candidate.to_owned()
        } else {
            self.python.clone()
        }
    }

    pub fn record(&self) -> Object {
        let record = read_record_file(&self.state_path, self.record_format());
        if record.str_of("projectPath") != self.project.to_string_lossy()
            || self
                .identity_fields()
                .iter()
                .any(|(key, value)| record.get(key) != Some(value))
        {
            return Object::new();
        }
        record
    }

    pub fn needs_replacement(&self, plan: Option<&Value>, reset_failed: bool) -> bool {
        let record = self.record();
        !record.is_empty()
            && match plan {
                None => true,
                Some(plan) => {
                    record.get("plan") != Some(plan)
                        || (reset_failed && record.str_of("state") == "failed")
                }
            }
    }

    fn stop_refusal(&self, record: &Object) -> String {
        let host = record.get("host");
        if !matches!(host, None | Some(Value::Null)) && record.str_of("host") != procfs::hostname()
        {
            return format!("{} service belongs to another host", self.service_name());
        }
        let (me, _) = manager_identity();
        if self.host_matches(record)
            && record.i64_of("managerPid") != Some(me)
            && process_matches(
                record.i64_of("managerPid"),
                record.i64_of("managerStartTime"),
            )
        {
            return format!(
                "{} service belongs to another live manager",
                self.service_name()
            );
        }
        String::new()
    }

    fn ensure_refusal(&self, plan: &Value, record: &Object) -> String {
        let host = record.get("host");
        if !matches!(host, None | Some(Value::Null)) && record.str_of("host") != procfs::hostname()
        {
            return format!("{} service belongs to another host", self.service_name());
        }
        let (me, _) = manager_identity();
        if self.host_matches(record) {
            if record.i64_of("managerPid") != Some(me)
                || !process_matches(
                    record.i64_of("managerPid"),
                    record.i64_of("managerStartTime"),
                )
                || record.get("plan") != Some(plan)
                || record.str_of("state") == "failed"
            {
                return format!(
                    "{} replacement requires a drained worker",
                    self.service_name()
                );
            }
            match record.get("managerControl") {
                None | Some(Value::Null) => {}
                Some(Value::Object(control)) => {
                    let fd = control.get("fd").and_then(Value::as_i64).unwrap_or(-1);
                    if !manager_control_matches(record, fd, true) {
                        return format!(
                            "{} manager control was lost; drain the host",
                            self.service_name()
                        );
                    }
                }
                Some(_) => {
                    return format!(
                        "{} manager control was lost; drain the host",
                        self.service_name()
                    )
                }
            }
            return String::new();
        }
        if record.str_of("state") == "failed" {
            return format!(
                "{} service failed; explicit restart required",
                self.service_name()
            );
        }
        String::new()
    }

    /// Why an explicit replacement would refuse this host, decided while the old API serves.
    pub fn replacement_refusal(&self, plan: Option<&Value>, replace: bool) -> String {
        let record = self.record();
        if replace {
            return self.stop_refusal(&record);
        }
        match plan {
            Some(plan) if !plan.is_null() => self.ensure_refusal(plan, &record),
            _ => String::new(),
        }
    }

    pub fn ensure(&self, plan: &Value, environment: &Env) -> Result<String, ServiceError> {
        let record = self.record();
        let refusal = self.ensure_refusal(plan, &record);
        if !refusal.is_empty() {
            return Err(ServiceError::Failed(refusal));
        }
        if self.host_matches(&record) {
            return Ok(self.result_path());
        }
        if let Some(parent) = self.state_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| ServiceError::Failed(e.to_string()))?;
        }
        let lease = acquire_service_lock(&self.lock_path)?;
        close_manager_control(&record);
        let mut fds = [0i32; 2];
        if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
            return Err(ServiceError::Failed(format!(
                "pipe failed: {}",
                std::io::Error::last_os_error()
            )));
        }
        let (reader, writer) = (unsafe { File::from_raw_fd(fds[0]) }, fds[1]);
        let mut published = false;
        let result = (|| {
            let mut control_info: libc::stat = unsafe { std::mem::zeroed() };
            unsafe {
                libc::fstat(writer, &mut control_info);
            }
            let service_id = new_service_id();
            let (me, ticks) = manager_identity();
            let ticks = ticks.ok_or_else(|| {
                ServiceError::Failed(format!(
                    "{} requires verifiable manager process identity",
                    self.service_name()
                ))
            })?;
            let env = self.process_environment(environment);
            let log = self.project.join("logs/server-console.log");
            if let Some(parent) = log.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let output = OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(&log)
                .map_err(|e| ServiceError::Failed(format!("cannot open {}: {e}", log.display())))?;
            let lease_fd = lease.as_raw_fd();
            let reader_fd = reader.as_raw_fd();
            let mut command = Command::new(self.launch_python(plan));
            command
                .args(["-m", self.host_module(), "--service-id", &service_id])
                .args([
                    "--manager-pid",
                    &me.to_string(),
                    "--manager-start",
                    &ticks.to_string(),
                ])
                .args([
                    "--manager-fd",
                    &reader_fd.to_string(),
                    "--lease-fd",
                    &lease_fd.to_string(),
                ])
                .args(["--state-file", &self.state_path.to_string_lossy()])
                .current_dir(&self.project)
                .env_clear()
                .envs(&env)
                .stdin(Stdio::null())
                .stdout(Stdio::from(
                    output
                        .try_clone()
                        .map_err(|e| ServiceError::Failed(e.to_string()))?,
                ))
                .stderr(Stdio::from(output));
            unsafe {
                command.pre_exec(move || {
                    libc::setsid();
                    clear_cloexec(lease_fd);
                    clear_cloexec(reader_fd);
                    Ok(())
                });
            }
            let child = command.spawn().map_err(|e| {
                ServiceError::Failed(format!("{} host spawn failed: {e}", self.service_name()))
            })?;
            let pid = child.id() as i64;
            let mut record = Object::new();
            record.insert("format".into(), json!(self.record_format()));
            record.insert("host".into(), json!(procfs::hostname()));
            record.insert("projectPath".into(), json!(self.project.to_string_lossy()));
            for (key, value) in self.identity_fields() {
                record.insert(key, value);
            }
            record.insert("serviceId".into(), json!(service_id));
            record.insert("pid".into(), json!(pid));
            record.insert("processStartTime".into(), json!(procfs::start_ticks(pid)));
            record.insert("managerPid".into(), json!(me));
            record.insert("managerStartTime".into(), json!(ticks));
            record.insert(
                "managerControl".into(),
                json!({"fd": writer, "device": control_info.st_dev, "inode": control_info.st_ino}),
            );
            record.insert("plan".into(), plan.clone());
            record.insert("state".into(), json!("starting"));
            *self.process.lock().unwrap() = Some(child);
            if let Err(error) = write_private_atomic(&self.state_path, &Value::Object(record)) {
                unsafe {
                    libc::kill(pid as i32, libc::SIGTERM);
                }
                if let Some(mut child) = self.process.lock().unwrap().take() {
                    let deadline = Deadline::after(10.0);
                    while !deadline.expired() && matches!(child.try_wait(), Ok(None)) {
                        sleep(0.05);
                    }
                }
                return Err(ServiceError::Failed(format!(
                    "{} record could not be published: {error}",
                    self.service_name()
                )));
            }
            published = true;
            Ok(self.result_path())
        })();
        drop(lease);
        drop(reader);
        if !published {
            unsafe {
                libc::close(writer);
            }
        }
        result
    }

    fn reap_process(&self) {
        let mut guard = self.process.lock().unwrap();
        if let Some(child) = guard.as_mut() {
            if !matches!(child.try_wait(), Ok(None)) {
                *guard = None;
            }
        }
    }

    /// Signal, join physical exit and release the lease of this manager's host.
    pub fn stop(
        &self,
        timeout: f64,
        retire_failed_close: bool,
        bridge: &Bridge,
    ) -> Result<String, ServiceError> {
        let record = self.record();
        let refusal = self.stop_refusal(&record);
        if !refusal.is_empty() {
            return Err(ServiceError::Failed(refusal));
        }
        let mut retired = String::new();
        let mut deadline = Deadline::after(timeout);
        if self.host_matches(&record) {
            let pid = record.i64_of("pid").unwrap_or(0) as i32;
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
            while self.host_matches(&record) && !deadline.expired() {
                self.reap_process();
                sleep(0.05);
            }
            if self.host_matches(&record) {
                if !retire_failed_close {
                    return Err(ServiceError::Failed(format!(
                        "{} service has not finished draining",
                        self.service_name()
                    )));
                }
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
                retired = format!("did not stop within {timeout:.0}s; killed");
                deadline = Deadline::after(timeout);
                while self.host_matches(&record) && !deadline.expired() {
                    self.reap_process();
                    sleep(0.05);
                }
                if self.host_matches(&record) {
                    return Err(ServiceError::Draining(format!(
                        "{} service survived SIGKILL",
                        self.service_name()
                    )));
                }
            }
            let stopped = self.record();
            if retired.is_empty()
                && (stopped.get("serviceId") != record.get("serviceId")
                    || stopped.str_of("state") != "stopped")
            {
                let detail = match stopped.get("error") {
                    Some(error) if crate::json::truthy(Some(error)) => error
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| error.to_string()),
                    _ => "clean close was not confirmed".to_owned(),
                };
                let detail: String = detail.chars().take(300).collect();
                if !retire_failed_close || stopped.get("serviceId") != record.get("serviceId") {
                    return Err(ServiceError::Failed(format!(
                        "{} shutdown failed: {detail}",
                        self.service_name()
                    )));
                }
                retired = detail;
            }
        }
        self.reap_process();
        if self.lock_path.exists() {
            let lease = self.acquire_released_lease(retire_failed_close.then_some(deadline))?;
            if !record.is_empty() && self.record().get("serviceId") == record.get("serviceId") {
                self.record_exit(&record, deadline, bridge);
                close_manager_control(&record);
                let _ = std::fs::remove_file(&self.state_path);
            }
            drop(lease);
        }
        Ok(retired)
    }

    /// Publish the joined exit of the host's self-reported incarnation (`_record_exit`).
    fn record_exit(&self, record: &Object, deadline: Deadline, bridge: &Bridge) {
        let owner = record.obj_of("processOwner");
        let directory = record.str_of("exitEvidence");
        if owner.is_empty()
            || directory.is_empty()
            || owner.get("pid") != record.get("pid")
            || owner.str_of("started")
                != record
                    .get("processStartTime")
                    .map(|v| v.to_string())
                    .unwrap_or_default()
        {
            return;
        }
        let error = loop {
            match bridge.record_exit(&Value::Object(owner.clone()), directory) {
                Ok(Some(_)) => return,
                Ok(None) if !deadline.expired() => sleep(0.05),
                Ok(None) => break "process exit is not proved by local kernel evidence".to_owned(),
                Err(error) => break error,
            }
        };
        crate::log::write(
            crate::log::Level::Warning,
            "serverctl_pkg.resident_service",
            &format!(
                "{} exit evidence for pid {} was not recorded: {error}",
                self.service_name(),
                record.get("pid").map(|v| v.to_string()).unwrap_or_default()
            ),
        );
    }

    fn acquire_released_lease(&self, deadline: Option<Deadline>) -> Result<File, ServiceError> {
        loop {
            match acquire_service_lock(&self.lock_path) {
                Ok(file) => return Ok(file),
                Err(ServiceError::Draining(message)) => {
                    if deadline.is_none_or(|deadline| deadline.expired()) {
                        return Err(ServiceError::Draining(message));
                    }
                }
                Err(error) => return Err(error),
            }
            sleep(0.05);
        }
    }

    /// Prepare this host's plan; `None` when another owner keeps the resource.
    pub fn prepare(&self, environment: &Env) -> Result<Option<Value>, String> {
        if self.kind == Kind::Storage
            && (!cfg!(target_os = "linux")
                || environment.contains_key("TOFU_STORAGE_CONNECTION_FILE")
                || !self
                    .project
                    .join("contracts/native_storage_connection_v2.json")
                    .is_file())
        {
            return Ok(None);
        }
        let scratch = std::env::temp_dir().join(format!(
            "tofu-manager-plan-{}-{}",
            std::process::id(),
            (crate::timing::now() * 1e6) as u64
        ));
        std::fs::DirBuilder::new()
            .recursive(true)
            .create(&scratch)
            .map_err(|error| error.to_string())?;
        let output_path = scratch.join("plan.json");
        let (python, environment, subject) = match self.kind {
            Kind::Storage => (
                self.python.clone(),
                environment.clone(),
                "Storage engine preparation",
            ),
            Kind::Execution => (
                self.preparation_python(),
                self.process_environment(environment),
                "Execution service preparation",
            ),
        };
        let result = child::run(Spec {
            argv: vec![
                python,
                "-m".into(),
                self.host_module().into(),
                "--plan".into(),
                "--plan-output".into(),
                output_path.to_string_lossy().into_owned(),
            ],
            cwd: &self.project,
            environment: &environment,
            timeout: NATIVE_SERVICE_PREPARATION_SECONDS,
            progress: None,
            termination_grace: 5.0,
            failure_subject: subject,
            stdin: None,
        })
        .and_then(|_| {
            let text = std::fs::read_to_string(&output_path).map_err(|e| e.to_string())?;
            serde_json::from_str::<Value>(&text).map_err(|e| e.to_string())
        });
        let _ = std::fs::remove_dir_all(&scratch);
        let plan = result?;
        Ok(match self.kind {
            Kind::Storage if !crate::json::truthy(Some(&plan)) => None,
            _ => Some(plan),
        })
    }

    /// This manager's ready storage host's own plan, or `None` when it cannot be kept.
    pub fn retained_plan(&self) -> Option<Value> {
        let record = self.record();
        let (me, _) = manager_identity();
        if record.str_of("state") != "ready"
            || !matches!(record.get("plan"), Some(Value::Object(_)))
            || !self.host_matches(&record)
            || record.i64_of("managerPid") != Some(me)
            || !process_matches(
                record.i64_of("managerPid"),
                record.i64_of("managerStartTime"),
            )
        {
            return None;
        }
        record.get("plan").cloned()
    }

    /// Start a new engine on a disposable authority before drain (`probe_generation`).
    pub fn probe_generation(&self, plan: Option<&Value>, environment: &Env) -> Result<(), String> {
        let serving = self.record().obj_of("plan");
        let Some(plan) = plan else { return Ok(()) };
        let binary = plan.get("binary").and_then(Value::as_str).unwrap_or("");
        if binary.is_empty() || plan.get("binarySha256") == serving.get("binarySha256") {
            return Ok(());
        }
        child::run(Spec {
            argv: vec![
                self.python.clone(),
                self.project
                    .join("scripts/check_tofudb_engine_drift.py")
                    .to_string_lossy()
                    .into_owned(),
                "--binary".into(),
                binary.into(),
                "--json".into(),
            ],
            cwd: &self.project,
            environment,
            timeout: NATIVE_GENERATION_PROBE_SECONDS,
            progress: Some("Starting the new storage engine on a disposable authority"),
            termination_grace: 5.0,
            failure_subject: "Storage engine preparation",
            stdin: None,
        })
        .map(|_| ())
    }

    /// Whether the storage lease is this manager's own host's daemon (`owns_lease`).
    pub fn owns_lease(&self, lease: &Object) -> bool {
        if self.kind != Kind::Storage {
            return false;
        }
        let record = self.record();
        if !lease.truthy("held")
            || !lease.truthy("holderVerified")
            || lease.str_of("kind") != "storage_authority_sql"
            || !self.host_matches(&record)
        {
            return false;
        }
        let (me, _) = manager_identity();
        if record.i64_of("managerPid") != Some(me)
            || !process_matches(
                record.i64_of("managerPid"),
                record.i64_of("managerStartTime"),
            )
        {
            return false;
        }
        lease
            .i64_of("pid")
            .and_then(procfs::stat_fields)
            .and_then(|fields| fields[1].parse::<i64>().ok())
            == record.i64_of("pid")
    }

    /// `ExecutionService.readiness_error`: a live API is ready only with a ready executor.
    pub fn readiness_error(&self, record: &Object, recovery: &Object) -> String {
        if record.is_empty() || (record.str_of("state") == "ready" && self.host_matches(record)) {
            return String::new();
        }
        for candidate in [recovery.get("error"), record.get("error")] {
            if crate::json::truthy(candidate) {
                return candidate
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .unwrap_or_default();
            }
        }
        "execution service is not ready".to_owned()
    }

    /// Writer descriptors that must survive this manager's exec.
    pub fn exec_retained_descriptor(&self) -> Option<i32> {
        owned_control_descriptor(&self.record())
    }
}

/// Mark every inherited descriptor above stderr close-on-exec, so no child
/// inherits a descriptor it was not passed (Python's `close_fds=True`).
pub fn close_inherited_descriptors_on_exec() {
    let Ok(entries) = std::fs::read_dir("/proc/self/fd") else {
        return;
    };
    let descriptors: Vec<i32> = entries
        .flatten()
        .filter_map(|entry| entry.file_name().to_string_lossy().parse().ok())
        .filter(|fd| *fd > 2)
        .collect();
    for fd in descriptors {
        unsafe {
            let flags = libc::fcntl(fd, libc::F_GETFD);
            if flags >= 0 {
                libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC);
            }
        }
    }
}

/// Keep `fd` open across this process's own exec (`/reload`).
pub fn retain_across_exec(fd: i32) {
    clear_cloexec(fd);
}
