//! Process entry point (`lib/storage_authority_sql/__main__.py`): the same
//! environment, project lease, stdout startup envelope and stdin/PPID
//! ownership channel as the Python Sidecar, so `StorageSupervisor` can own
//! either implementation.
//!
//! `tofu-sql-authority serve` serves one data directory; `tofu-sql-authority
//! unported` lists catalog operations not yet registered.

use std::fs::OpenOptions;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde_json::json;
use tofu_sql_authority::backend::{Backend, Config};
use tofu_sql_authority::error::StorageError;
use tofu_sql_authority::server::{Server, PROTOCOL_VERSION};

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn request_stop(_signal: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

fn startup_error(code: &str, diagnostic: &str) {
    let line = json!({
        "type": "storage.error",
        "protocol": PROTOCOL_VERSION,
        "code": code.chars().take(80).collect::<String>(),
        "diagnostic": diagnostic.chars().take(500).collect::<String>(),
    });
    let mut stdout = std::io::stdout();
    let _ = writeln!(stdout, "{line}");
    let _ = stdout.flush();
}

struct Environment {
    data_dir: PathBuf,
    token: String,
    parent_pid: Option<i32>,
}

fn environment() -> Result<Environment, String> {
    let allow_override = std::env::var("TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE").as_deref() == Ok("1");
    let backend = std::env::var("TOFU_STORAGE_TEST_BACKEND").unwrap_or_default().trim().to_lowercase();
    if !backend.is_empty() {
        if !allow_override {
            return Err("TOFU_STORAGE_TEST_BACKEND requires explicit test authority".into());
        }
        if backend != "sqlite" {
            return Err("the Rust SQL authority serves only the SQLite backend".into());
        }
    }
    if !std::env::var("TOFU_STORAGE_CONNECTION_FILE").unwrap_or_default().trim().is_empty() {
        return Err("connection-file mode is not ported to the Rust SQL authority".into());
    }
    if !std::env::var("TOFU_STORAGE_NATIVE_CHECKPOINT").unwrap_or_default().is_empty() {
        return Err("configured native checkpoint control is not ported to the Rust SQL authority".into());
    }
    let token = std::env::var("TOFU_STORAGE_TOKEN").unwrap_or_default();
    if token.len() < 32 {
        return Err("TOFU_STORAGE_TOKEN is missing or too short".into());
    }
    let root = std::env::var("TOFU_STORAGE_PROJECT_ROOT").unwrap_or_default();
    let root = root.trim();
    if root.is_empty() {
        return Err("the Rust SQL authority requires TOFU_STORAGE_PROJECT_ROOT".into());
    }
    if !allow_override {
        return Err("project-root override requires explicit test authority".into());
    }
    std::fs::create_dir_all(root).map_err(|error| format!("project root: {error}"))?;
    let project_root = std::fs::canonicalize(root).map_err(|error| format!("project root: {error}"))?;
    let data_dir = project_root.join("data");
    std::fs::create_dir_all(&data_dir).map_err(|error| format!("data dir: {error}"))?;
    std::fs::create_dir_all(project_root.join("logs")).map_err(|error| format!("logs dir: {error}"))?;
    let parent_pid = match std::env::var("TOFU_STORAGE_PARENT_PID").unwrap_or_default().trim() {
        "" => None,
        raw => match raw.parse::<i32>() {
            Ok(pid) if pid > 1 => Some(pid),
            _ => return Err("invalid TOFU_STORAGE_PARENT_PID".into()),
        },
    };
    Ok(Environment { data_dir, token, parent_pid })
}

/// `ProjectLease.acquire`: the process-held exclusive lock every storage
/// child takes before it serves.
fn acquire_lease(data_dir: &std::path::Path) -> Result<std::fs::File, StorageError> {
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(data_dir.join(".storage-sidecar.lock"))
        .map_err(|_| StorageError::new("database_unavailable", "storage project lease is unavailable"))?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(StorageError::new("database_unavailable", "Another storage authority owns this project"));
    }
    let lease = json!({"pid": std::process::id(), "implementation": "rust", "protocol": PROTOCOL_VERSION});
    let _ = std::fs::write(data_dir.join(".storage-sidecar-lease.json"), lease.to_string());
    Ok(lock)
}

fn watch_parent(parent_pid: i32) {
    std::thread::Builder::new()
        .name("storage-parent-watch".into())
        .spawn(move || {
            let mut stdin = std::io::stdin();
            let mut byte = [0u8; 1];
            // EOF on the ownership pipe covers parent death and exec.
            loop {
                match stdin.read(&mut byte) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => continue,
                }
            }
            STOP.store(true, Ordering::SeqCst);
        })
        .expect("parent watch thread");
    std::thread::Builder::new()
        .name("storage-ppid-watch".into())
        .spawn(move || loop {
            if unsafe { libc::getppid() } != parent_pid {
                STOP.store(true, Ordering::SeqCst);
                return;
            }
            std::thread::sleep(Duration::from_millis(250));
        })
        .expect("ppid watch thread");
}

fn serve() -> i32 {
    let environment = match environment() {
        Ok(environment) => environment,
        Err(diagnostic) => {
            startup_error("database_unavailable", &diagnostic);
            return 2;
        }
    };
    let _lease = match acquire_lease(&environment.data_dir) {
        Ok(lease) => lease,
        Err(error) => {
            startup_error(&error.code, &error.message);
            return 2;
        }
    };
    let config = match Config::from_environment(environment.data_dir.clone()) {
        Ok(config) => config,
        Err(diagnostic) => {
            startup_error("database_unavailable", &diagnostic);
            return 2;
        }
    };
    let backend = match Backend::start(config) {
        Ok(backend) => Arc::new(backend),
        Err(error) => {
            startup_error(&error.code, &error.message);
            return 2;
        }
    };
    let listener = match Server::bind() {
        Ok(listener) => listener,
        Err(error) => {
            startup_error("database_unavailable", &format!("bind failed: {error}"));
            return 2;
        }
    };
    let port = listener.local_addr().map(|address| address.port()).unwrap_or(0);
    unsafe {
        libc::signal(libc::SIGTERM, request_stop as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, request_stop as *const () as libc::sighandler_t);
    }
    if let Some(parent) = environment.parent_pid {
        watch_parent(parent);
        if unsafe { libc::getppid() } != parent {
            startup_error("database_unavailable", "Storage supervisor exited during startup");
            return 2;
        }
    }
    let server = Server::new(Arc::clone(&backend), environment.token, false);
    let accept_server = Arc::clone(&server);
    std::thread::Builder::new()
        .name("storage-accept".into())
        .spawn(move || accept_server.serve(listener))
        .expect("accept thread");
    let ready = json!({"type": "storage.ready", "protocol": PROTOCOL_VERSION, "port": port, "backend": "sqlite"});
    let mut stdout = std::io::stdout();
    let _ = writeln!(stdout, "{ready}");
    let _ = stdout.flush();
    while !STOP.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(200));
    }
    server.stop();
    backend.close();
    0
}

fn main() {
    let command = std::env::args().nth(1).unwrap_or_else(|| "serve".into());
    let code = match command.as_str() {
        "serve" => serve(),
        "unported" => {
            for name in tofu_sql_authority::ops::unported_operations() {
                println!("{name}");
            }
            0
        }
        other => {
            eprintln!("unknown command {other:?}; expected serve or unported");
            2
        }
    };
    std::process::exit(code);
}
