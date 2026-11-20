//! Device paper builds (`paper_resolve`, `paper_build`, `paper_build_cancel`):
//! the port of `lib/desktop_agent/_paper.py` and the device side of
//! `lib/paper/builds` it uses.
//!
//! Commands resolve this node's declared share roots again (`project::resolve`);
//! no Home path is accepted. A build freezes the project sources into a
//! private temporary directory (`sources`), compiles them with latexmk under
//! the node's process supervisor (`toolchain`, `process`, `compiler`), resolves
//! the edit hint through SyncTeX (`synctex`) and streams the manifest and
//! artifact chunks the Home's `lib/paper/builds/desktop.py` receives
//! (`bundle`). Payloads are validated against the embedded
//! `contracts/paper_build_v1.schema.json` (`contract`); manifests are
//! serialized exactly as Python's `json.dumps` would (`pyjson`).
//!
//! Each build runs on the link's own command thread; nothing is pooled.

mod audit;
mod bundle;
mod compiler;
mod contract;
mod process;
mod pyjson;
mod sources;
mod synctex;
mod toolchain;

use crate::util::Stop;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

/// A typed failure, reported as the Python node's `f'{type}: {message}'`.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum Fault {
    /// `lib.paper.builds.errors.BuildError(code, message)`.
    Build {
        code: String,
        message: String,
    },
    /// `lib.desktop_agent._project.ProjectError`.
    Project(String),
    /// An operating-system error outside the build's own handling.
    Os(String),
    Runtime(String),
    Value(String),
    Key(String),
}

impl Fault {
    pub(crate) fn build(code: &str, message: &str) -> Fault {
        Fault::Build {
            code: code.into(),
            message: message.chars().take(2000).collect(),
        }
    }

    pub(crate) fn code(&self) -> Option<&str> {
        match self {
            Fault::Build { code, .. } => Some(code),
            _ => None,
        }
    }

    fn parts(&self) -> (&'static str, &str) {
        match self {
            Fault::Build { message, .. } => ("BuildError", message),
            Fault::Project(message) => ("ProjectError", message),
            Fault::Os(message) => ("OSError", message),
            Fault::Runtime(message) => ("RuntimeError", message),
            Fault::Value(message) => ("ValueError", message),
            Fault::Key(message) => ("KeyError", message),
        }
    }

    /// `f'{type(e).__name__}: {e}'` (`_dispatch.dispatch_command`).
    pub(crate) fn python_text(&self) -> String {
        let (kind, message) = self.parts();
        format!("{kind}: {message}")
    }

    /// `f'{type(exc).__name__}: {str(exc)[:1000]}'` (`_run.execute`).
    pub(crate) fn command_error(&self) -> String {
        let (kind, message) = self.parts();
        format!("{kind}: {}", crate::util::truncate_chars(message, 1000))
    }
}

impl From<std::io::Error> for Fault {
    fn from(error: std::io::Error) -> Fault {
        Fault::Os(error.to_string())
    }
}

pub(crate) type Outcome<T> = Result<T, Fault>;

/// Whether the build must stop (the event of `errors.check_cancelled`).
pub(crate) type Cancel<'a> = &'a (dyn Fn() -> bool + Sync);

pub(crate) fn check_cancelled(cancel: Cancel) -> Outcome<()> {
    if cancel() {
        Err(Fault::build("cancelled", "Paper build cancelled"))
    } else {
        Ok(())
    }
}

/// `path.relative_to(root).as_posix()`, `.` for the root itself.
pub(crate) fn relative_posix(root: &Path, path: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    let parts: Vec<String> = relative
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    Some(if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    })
}

fn param_str<'a>(params: &'a Value, key: &str) -> &'a str {
    params.get(key).and_then(Value::as_str).unwrap_or("")
}

fn resolve_document(params: &Value) -> Outcome<Value> {
    contract::validate(params, "PaperDesktopResolveV1")?;
    let (root, candidate) =
        super::project::resolve(params, params.get("path")).map_err(Fault::Project)?;
    let main = sources::resolve_main(&root, &candidate, param_str(params, "main_file"))?;
    let root = sources::canonical(&root).map_err(|_| sources::outside())?;
    let project = match param_str(params, "project_path") {
        "" => main.parent().map(Path::to_path_buf).unwrap_or_default(),
        project_path => sources::contained_path(&root, Path::new(project_path))?,
    };
    if !project.is_dir() {
        return Err(Fault::build(
            "project_root",
            "Choose a paper source directory",
        ));
    }
    let main = sources::contained_path(&project, &main)?;
    let edited = sources::contained_path(&project, &candidate)?;
    let document = json!({
        "schema_version": 1,
        "project_path": relative_posix(&root, &project).unwrap_or_default(),
        "main_file": relative_posix(&project, &main).unwrap_or_default(),
        "edited_file": relative_posix(&project, &edited).unwrap_or_default(),
    });
    contract::validate(&document, "PaperDesktopDocumentV1")?;
    Ok(document)
}

pub fn resolve(params: &Value) -> Value {
    resolve_document(params).unwrap_or_else(|fault| json!({"error": fault.python_text()}))
}

/// paper_build_v1 `x-resource-limits.desktop_connection_seconds`.
pub fn desktop_connection_seconds() -> u64 {
    contract::limit("desktop_connection_seconds")
}

/// Whether the link heard from its Home recently enough to keep building.
pub type Connected = Arc<dyn Fn() -> bool + Send + Sync>;
/// Queue one stream frame `(stream, data)`; `Err` means the transfer stopped.
pub type Emit<'a> = &'a dyn Fn(&str, String) -> Result<(), String>;

/// One admitted build's stop condition (`_paper._Cancellation`).
struct Cancellation {
    local: AtomicBool,
    stop: Stop,
    connected: Connected,
}

impl Cancellation {
    fn is_set(&self) -> bool {
        self.local.load(Ordering::SeqCst) || self.stop.is_set() || !(self.connected)()
    }
}

#[derive(Default)]
struct Pending {
    builds: HashMap<String, Arc<Cancellation>>,
    closing: bool,
}

/// Owns the paper builds of one link (admission, cancellation, close).
pub struct PaperBuildWorker {
    stop: Stop,
    connected: Connected,
    temporary_parent: Option<PathBuf>,
    pending: Mutex<Pending>,
    settled: Condvar,
}

impl PaperBuildWorker {
    pub fn new(stop: Stop, connected: Connected) -> Self {
        Self {
            stop,
            connected,
            temporary_parent: None,
            pending: Mutex::new(Pending::default()),
            settled: Condvar::new(),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Pending> {
        self.pending.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Admit a build id before its thread starts; false when stopping or owned.
    pub fn admit(&self, command_id: &str) -> bool {
        let mut pending = self.lock();
        if pending.builds.contains_key(command_id) || pending.closing || self.stop.is_set() {
            return false;
        }
        pending.builds.insert(
            command_id.to_string(),
            Arc::new(Cancellation {
                local: AtomicBool::new(false),
                stop: self.stop.clone(),
                connected: self.connected.clone(),
            }),
        );
        true
    }

    pub fn cancel(&self, command_id: &str) {
        if let Some(control) = self.lock().builds.get(command_id) {
            control.local.store(true, Ordering::SeqCst);
        }
    }

    pub fn release(&self, command_id: &str) {
        self.lock().builds.remove(command_id);
        self.settled.notify_all();
    }

    /// Run one admitted build, streaming its bundle through `emit`; always
    /// releases the id. `Err` is the command's error text.
    pub fn execute(&self, command_id: &str, params: &Value, emit: Emit) -> Result<Value, String> {
        let outcome = self.build(command_id, params, emit);
        self.release(command_id);
        outcome.map_err(|fault| fault.command_error())
    }

    fn build(&self, command_id: &str, params: &Value, emit: Emit) -> Outcome<Value> {
        contract::validate(params, "PaperDesktopBuildV1")?;
        let build_id = param_str(params, "build_id");
        if build_id != command_id {
            return Err(Fault::build(
                "build_identity",
                "Paper command and build identities differ",
            ));
        }
        let control = self
            .lock()
            .builds
            .get(command_id)
            .cloned()
            .ok_or_else(|| Fault::Key(super::project::py_repr(command_id)))?;
        let is_set = move || control.is_set();
        let cancel: Cancel = &is_set;
        check_cancelled(cancel)?;
        let (_, project) =
            super::project::resolve(params, params.get("project_path")).map_err(Fault::Project)?;
        let project = sources::canonical(&project).map_err(|_| sources::outside())?;
        let main = sources::contained_path(&project, Path::new(param_str(params, "main_file")))?;
        let edited =
            sources::contained_path(&project, Path::new(param_str(params, "edited_file")))?;
        let edited = relative_posix(&project, &edited).unwrap_or_default();
        let temporary = TemporaryDirectory::create(self.temporary_parent.as_deref())?;
        let snapshot =
            sources::snapshot_sources(&project, &main, &temporary.path.join("sources"), cancel)?;
        let identity = compiler::Identity {
            owner_user_id: params["owner_user_id"].as_i64().unwrap_or(0),
            document_id: param_str(params, "document_id").into(),
            build_id: build_id.into(),
            engine: params
                .get("engine")
                .and_then(Value::as_str)
                .unwrap_or("auto")
                .into(),
        };
        let build = compiler::compile_snapshot(
            &snapshot,
            &temporary.path.join("build"),
            &identity,
            cancel,
        )?;
        check_cancelled(cancel)?;
        let location = synctex::build_location(
            &build,
            &snapshot,
            &edited,
            params.get("line").and_then(Value::as_i64),
            param_str(params, "needle"),
        )?;
        let send = |stream: &str, data: String| -> Outcome<()> {
            if cancel() {
                return Err(Fault::Runtime(
                    "Desktop transfer cancelled before delivery".into(),
                ));
            }
            emit(stream, data).map_err(|error| {
                if error.contains("exceeds") {
                    Fault::Value(error)
                } else {
                    Fault::Runtime(error)
                }
            })
        };
        bundle::iter_bundle(&build, location, cancel, &send)?;
        send("paper-end", String::new())?;
        Ok(json!({"schema_version": 1, "build_id": build_id}))
    }

    /// Cancel every pending build and wait until they settle.
    pub fn close(&self) {
        let mut pending = self.lock();
        pending.closing = true;
        for control in pending.builds.values() {
            control.local.store(true, Ordering::SeqCst);
        }
        while !pending.builds.is_empty() {
            pending = self
                .settled
                .wait(pending)
                .unwrap_or_else(|e| e.into_inner());
        }
    }
}

/// `tempfile.TemporaryDirectory(prefix='tofu-paper-')`: removed on drop,
/// including the read-only frozen sources.
struct TemporaryDirectory {
    path: PathBuf,
}

impl TemporaryDirectory {
    fn create(parent: Option<&Path>) -> Outcome<TemporaryDirectory> {
        let parent = parent
            .map(Path::to_path_buf)
            .unwrap_or_else(std::env::temp_dir);
        let path = parent.join(format!("tofu-paper-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir(&path)?;
        Ok(TemporaryDirectory { path })
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        if std::fs::remove_dir_all(&self.path).is_err() {
            make_writable(&self.path);
            let _ = std::fs::remove_dir_all(&self.path);
        }
    }
}

/// Windows refuses to delete read-only files; POSIX needs writable directories.
fn make_writable(path: &Path) {
    let Ok(meta) = std::fs::symlink_metadata(path) else {
        return;
    };
    if meta.is_dir() {
        for entry in std::fs::read_dir(path).into_iter().flatten().flatten() {
            make_writable(&entry.path());
        }
    }
    if !meta.file_type().is_symlink() {
        let mut permissions = meta.permissions();
        #[allow(clippy::permissions_set_readonly_false)]
        permissions.set_readonly(false);
        let _ = std::fs::set_permissions(path, permissions);
    }
}

#[cfg(test)]
mod tests;
