//! Core side of the stdio binding: one adapter child process (Python `StdioLink`).
//!
//! Spawns the adapter in its own session (process group) with exactly the
//! environment it is given, answers its `events` requests and `health`
//! notifications through the bound [`CoreHost`], keeps the last non-empty
//! stderr line as diagnostics only, and on [`StdioLink::close`] closes stdin
//! (the adapter's stop signal), waits, then signals the process group and
//! reaps it. A waiter thread observes the exit without reaping (`WNOWAIT`), so
//! the process group ID stays reserved until this link reaps it and a signal
//! can never reach a recycled group.

use std::io::{self, BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, RwLock};
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::contract::{self, error_code, Direction, Kind};
use crate::peer::{HandlerError, HandlerResult, Peer, ProtocolError};
use crate::pyjson::{py_isspace, py_repr_str};
use crate::threads::{self, Joinable};

/// The core's adapter-to-core methods (Python `AdapterHost.events`/`.health`).
pub trait CoreHost: Send + Sync {
    fn events(&self, batch: Map<String, Value>) -> HandlerResult;
    fn health(&self, health: Map<String, Value>) -> Result<(), HandlerError>;
}

type BoundHost = Arc<RwLock<Option<Arc<dyn CoreHost>>>>;

#[derive(Default)]
struct ExitState {
    /// The child exited (observed by the waiter thread or by a reap).
    exited: bool,
    /// Python `Popen.returncode` once reaped: exit status, or minus the signal.
    returncode: Option<i32>,
}

struct Exit {
    state: Mutex<ExitState>,
    changed: Condvar,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poison| poison.into_inner())
}

pub struct StdioLink {
    pid: libc::pid_t,
    peer: Peer,
    host: BoundHost,
    stderr_tail: Arc<Mutex<String>>,
    stderr_thread: Joinable,
    exit: Arc<Exit>,
    waiter_thread: Joinable,
}

impl StdioLink {
    /// Spawn `argv` with stdin/stdout/stderr piped in a new session. `env`
    /// replaces the environment exactly when given; `cwd` sets the directory.
    pub fn spawn(
        argv: &[String],
        env: Option<&[(String, String)]>,
        cwd: Option<&Path>,
    ) -> io::Result<StdioLink> {
        let Some((program, args)) = argv.split_first() else {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "argv is empty"));
        };
        let mut command = Command::new(program);
        command
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(env) = env {
            command
                .env_clear()
                .envs(env.iter().map(|(key, value)| (key, value)));
        }
        if let Some(cwd) = cwd {
            command.current_dir(cwd);
        }
        // SAFETY: setsid is async-signal-safe and touches no parent state.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn()?;
        let pid = child.id() as libc::pid_t;
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        // The link reaps the child itself (waitid/waitpid on `pid`); std's Child is not used again.
        drop(child);

        let host: BoundHost = Arc::new(RwLock::new(None));
        let handler_host = host.clone();
        let peer = Peer::new(
            stdout,
            stdin,
            Box::new(move |method, params| handle(&handler_host, method, params)),
            "channel-adapter",
        );
        let stderr_tail = Arc::new(Mutex::new(String::new()));
        let tail = stderr_tail.clone();
        let stderr_thread = threads::spawn("channel-adapter-stderr", move || drain(stderr, &tail));
        let exit = Arc::new(Exit {
            state: Mutex::new(ExitState::default()),
            changed: Condvar::new(),
        });
        let observed = exit.clone();
        let waiter_thread = threads::spawn("channel-adapter-waiter", move || {
            observe_exit(pid, &observed)
        });
        peer.start();
        Ok(StdioLink {
            pid,
            peer,
            host,
            stderr_tail,
            stderr_thread,
            exit,
            waiter_thread,
        })
    }

    pub fn pid(&self) -> i32 {
        self.pid
    }

    pub fn bind(&self, host: Arc<dyn CoreHost>) {
        *self
            .host
            .write()
            .unwrap_or_else(|poison| poison.into_inner()) = Some(host);
    }

    pub fn request(&self, method: &str, params: Value) -> Result<Value, ProtocolError> {
        self.peer.request(method, params)
    }

    /// The last non-empty stripped stderr line (diagnostics only).
    pub fn stderr_tail(&self) -> String {
        lock(&self.stderr_tail).clone()
    }

    /// Python `Popen.poll()`: reaps when the child has exited, never blocks.
    pub fn returncode(&self) -> Option<i32> {
        let mut state = lock(&self.exit.state);
        self.reap(&mut state);
        state.returncode
    }

    fn reap(&self, state: &mut ExitState) {
        if state.returncode.is_some() {
            return;
        }
        let mut status = 0;
        // SAFETY: plain syscall on our own child.
        let reaped = unsafe { libc::waitpid(self.pid, &mut status, libc::WNOHANG) };
        if reaped == self.pid {
            state.returncode = Some(if libc::WIFSIGNALED(status) {
                -libc::WTERMSIG(status)
            } else {
                libc::WEXITSTATUS(status)
            });
        } else if reaped == -1 && io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD) {
            // Python's poll() reports 0 for a child it can no longer wait for.
            state.returncode = Some(0);
        } else {
            return;
        }
        state.exited = true;
        self.exit.changed.notify_all();
    }

    /// Wait until the child exited (`None`: no deadline) and reap it; true when it did.
    pub fn wait(&self, timeout: Option<Duration>) -> bool {
        let deadline = timeout.map(|timeout| Instant::now() + timeout);
        let mut state = lock(&self.exit.state);
        while !state.exited {
            match deadline {
                None => {
                    state = self
                        .exit
                        .changed
                        .wait(state)
                        .unwrap_or_else(|poison| poison.into_inner())
                }
                Some(deadline) => {
                    let now = Instant::now();
                    if now >= deadline {
                        break;
                    }
                    state = self
                        .exit
                        .changed
                        .wait_timeout(state, deadline - now)
                        .unwrap_or_else(|poison| poison.into_inner())
                        .0;
                }
            }
        }
        self.reap(&mut state);
        state.returncode.is_some()
    }

    fn signal(&self, signal: libc::c_int) {
        let state = lock(&self.exit.state);
        if state.returncode.is_some() {
            return; // reaped: the group ID may already belong to someone else
        }
        // SAFETY: plain syscall; an unreaped leader keeps the group ID ours.
        // ESRCH (the group already exited) and any other refusal leave nothing to do here.
        unsafe {
            libc::killpg(self.pid, signal);
        }
    }

    /// Close the adapter's stdin, the stop signal; a `close` follows to reap it.
    pub fn close_stdin(&self) {
        self.peer.close_writer();
    }

    /// Close stdin (the adapter's stop signal), then escalate to SIGTERM and
    /// SIGKILL of the process group, each after `timeout`; always reap.
    /// Returns whether the process exited.
    pub fn close(&self, timeout: Duration) -> bool {
        self.close_stdin();
        if !self.wait(Some(timeout)) {
            self.signal(libc::SIGTERM);
            if !self.wait(Some(timeout)) {
                self.signal(libc::SIGKILL);
                self.wait(None);
            }
        }
        self.peer.join(Some(timeout));
        self.stderr_thread.join(Some(timeout));
        self.waiter_thread.join(Some(timeout));
        self.returncode().is_some()
    }
}

fn observe_exit(pid: libc::pid_t, exit: &Exit) {
    loop {
        // SAFETY: siginfo_t is plain data; waitid with WNOWAIT leaves the child unreaped.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                pid as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result == 0 || io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            break;
        }
    }
    lock(&exit.state).exited = true;
    exit.changed.notify_all();
}

fn drain(stderr: impl io::Read, tail: &Mutex<String>) {
    let mut reader = BufReader::new(stderr);
    let mut raw = Vec::new();
    loop {
        raw.clear();
        match reader.read_until(b'\n', &mut raw) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
        let line = String::from_utf8_lossy(&raw);
        let stripped = line.trim_matches(py_isspace);
        if !stripped.is_empty() {
            *lock(tail) = stripped.to_string();
        }
    }
}

fn handle(host: &BoundHost, method: &str, params: Map<String, Value>) -> HandlerResult {
    let spec = match contract::method(method) {
        Some(spec) if spec.direction == Direction::AdapterToCore => spec,
        _ => {
            return Err(ProtocolError::new(
                error_code("methodNotFound"),
                format!("The core has no method {}", py_repr_str(method)),
            )
            .into())
        }
    };
    let Some(host) = host
        .read()
        .unwrap_or_else(|poison| poison.into_inner())
        .clone()
    else {
        return Err(
            ProtocolError::new(error_code("notInitialized"), "No core endpoint is bound").into(),
        );
    };
    let result = match method {
        "events" => host.events(params)?,
        "health" => {
            host.health(params)?;
            Value::Null
        }
        // A contract method CoreHost does not port yet: Python's getattr raises AttributeError.
        _ => return Err(HandlerError::Other("AttributeError".into())),
    };
    Ok(if spec.kind == Kind::Request {
        result
    } else {
        Value::Null
    })
}
