//! Supervise one TeX command, its descendants and its output
//! (`lib/paper/builds/process.py` + `process_worker.py`).
//!
//! The command runs under the node's process supervisor
//! (`exec::spawn_supervised_argv`): cancellation, a deadline or an output
//! limit closes the supervisor's stdin, which retires the whole process tree
//! (process group and subreaper on Linux, tree cookie on macOS, Job object
//! on Windows). Output goes to the caller's log file, never to memory.

use super::{check_cancelled, Cancel, Fault, Outcome};
use crate::commands::exec;
use std::fs::File;
use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// `process.run_command`'s wait slice and output-inspection cadence.
const WAIT_SLICE: Duration = Duration::from_millis(20);
const INSPECT_INTERVAL: Duration = Duration::from_millis(100);

// The exit codes `process.run_command` interprets (POSIX signal numbers).
const SIGKILL: i64 = 9;
const SIGXCPU: i64 = 24;
const SIGXFSZ: i64 = 25;

/// Limits of one run; `None` is unbounded.
pub(crate) struct Bounds<'a> {
    pub timeout: Option<Duration>,
    pub log_bytes: Option<u64>,
    pub inspect_output: Option<&'a dyn Fn() -> Outcome<()>>,
}

/// Run `argv` in `cwd` with `envs` added, writing stdout and stderr to `log`.
/// Returns the command's exit code (128+signal for a signalled command).
pub(crate) fn run_command(
    argv: &[String],
    cwd: &Path,
    envs: &[(String, String)],
    log: &Path,
    bounds: Bounds,
    cancel: Cancel,
) -> Outcome<i64> {
    check_cancelled(cancel)?;
    let mut output = File::create(log)?;
    let cwd = super::sources::canonical(cwd)?;
    let mut child = exec::spawn_supervised_argv(argv, &cwd, envs)?;
    let written = Arc::new(AtomicU64::new(0));
    // A sentinel byte past the limit distinguishes a complete maximum-sized
    // log from a truncated one, as RLIMIT_FSIZE does for the Python worker.
    let keep = bounds.log_bytes.map(|limit| limit + 1);
    let (done, finished) = mpsc::channel();
    let reader = child.stdout.take().map(|pipe| {
        let written = written.clone();
        std::thread::spawn(move || {
            let status = exec::read_supervised(pipe, |_, bytes| {
                let so_far = written.load(Ordering::SeqCst);
                let room = keep.map_or(bytes.len() as u64, |k| k.saturating_sub(so_far));
                let take = (bytes.len() as u64).min(room) as usize;
                if take > 0 && output.write_all(&bytes[..take]).is_ok() {
                    written.fetch_add(take as u64, Ordering::SeqCst);
                }
            });
            let _ = done.send(());
            status
        })
    });
    let mut liveness = child.stdin.take();
    let deadline = bounds.timeout.map(|t| Instant::now() + t);
    let mut next_inspection = Instant::now();
    let failure = loop {
        if cancel() {
            break Some(Fault::build("cancelled", "Paper build cancelled"));
        }
        if deadline.is_some_and(|d| Instant::now() >= d) {
            break Some(Fault::build(
                "compile_timeout",
                "Paper compilation timed out",
            ));
        }
        if bounds
            .log_bytes
            .is_some_and(|limit| written.load(Ordering::SeqCst) > limit)
        {
            break Some(Fault::build(
                "output_limit",
                "Compiler log exceeds the byte limit",
            ));
        }
        let exited = match finished.recv_timeout(WAIT_SLICE) {
            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => true,
            Err(mpsc::RecvTimeoutError::Timeout) => false,
        };
        if let Some(inspect) = bounds.inspect_output {
            if exited || Instant::now() >= next_inspection {
                if let Err(fault) = inspect() {
                    break Some(fault);
                }
                next_inspection = Instant::now() + INSPECT_INTERVAL;
            }
        }
        if exited {
            break None;
        }
    };
    // EOF on its stdin is the supervisor's stop request; after a normal exit
    // it changes nothing.
    drop(liveness.take());
    let status = reader.and_then(|reader| reader.join().ok()).flatten();
    let supervisor = child.wait()?;
    if let Some(fault) = failure {
        truncate_log(log, bounds.log_bytes);
        return Err(fault);
    }
    check_cancelled(cancel)?;
    let returncode = match status.as_ref().and_then(|s| s["returncode"].as_i64()) {
        Some(code) if code < 0 => 128 - code,
        Some(code) => code,
        None => supervisor_code(supervisor),
    };
    let result = interpret(returncode, bounds.log_bytes, written.load(Ordering::SeqCst));
    truncate_log(log, bounds.log_bytes);
    result
}

fn interpret(returncode: i64, log_bytes: Option<u64>, written: u64) -> Outcome<i64> {
    if returncode == 124 || returncode == -SIGKILL {
        return Err(Fault::build(
            "compile_timeout",
            "Paper compilation exceeded its native deadline",
        ));
    }
    if returncode == 125 {
        return Err(Fault::build(
            "compile_unavailable",
            "Cannot start the bounded paper compiler",
        ));
    }
    if log_bytes.is_some_and(|limit| returncode != 0 && written >= limit) {
        return Err(Fault::build(
            "output_limit",
            "Compiler log exhausted its byte limit",
        ));
    }
    if returncode == 128 + SIGXFSZ {
        return Err(Fault::build(
            "output_limit",
            "Paper compilation exceeded its native file byte limit",
        ));
    }
    if returncode == 128 + SIGXCPU || returncode == 128 + SIGKILL {
        return Err(Fault::build(
            "compile_limit",
            "Paper compilation exceeded its native resource budget",
        ));
    }
    Ok(returncode)
}

fn supervisor_code(status: std::process::ExitStatus) -> i64 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return -i64::from(signal);
        }
    }
    status.code().map(i64::from).unwrap_or(-1)
}

fn truncate_log(log: &Path, limit: Option<u64>) {
    if let Some(limit) = limit {
        if std::fs::metadata(log).is_ok_and(|m| m.len() > limit) {
            if let Ok(file) = std::fs::OpenOptions::new().write(true).open(log) {
                let _ = file.set_len(limit);
            }
        }
    }
}
