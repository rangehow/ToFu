//! Bounded preparation children (`native_engine._run_streams`).
//!
//! A child runs in its own session with stdout and stderr captured to
//! separate private files. Whole lines are relayed to the manager log while
//! it runs when a progress label is given. A timeout, a non-zero exit or the
//! manager's own cancellation stops the whole process group: SIGTERM, the
//! caller's grace, then SIGKILL, and the leader is always reaped.

use crate::envpolicy::Env;
use crate::timing::{now, sleep, Deadline};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

pub const MAX_LOG_BYTES: u64 = 8 * 1024 * 1024;
const PROGRESS_INTERVAL_SECONDS: f64 = 5.0;

pub struct Output {
    pub stdout: String,
    pub stderr: String,
}

pub struct Spec<'a> {
    pub argv: Vec<String>,
    pub cwd: &'a Path,
    pub environment: &'a Env,
    pub timeout: f64,
    pub progress: Option<&'a str>,
    pub termination_grace: f64,
    pub failure_subject: &'a str,
    pub stdin: Option<Vec<u8>>,
}

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn scratch_directory() -> std::io::Result<Scratch> {
    let base = std::env::temp_dir();
    for attempt in 0..100u32 {
        let candidate = base.join(format!(
            "tofu-manager-command-{}-{}-{}",
            std::process::id(),
            (now() * 1e6) as u64,
            attempt
        ));
        match fs::DirBuilder::new().mode_private().create(&candidate) {
            Ok(()) => return Ok(Scratch(candidate)),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(std::io::Error::other("no private scratch directory"))
}

trait PrivateDir {
    fn mode_private(&mut self) -> &mut Self;
}

impl PrivateDir for fs::DirBuilder {
    fn mode_private(&mut self) -> &mut Self {
        use std::os::unix::fs::DirBuilderExt;
        self.mode(0o700)
    }
}

fn stop_group(child: &mut Child, grace: f64) {
    let pid = child.id() as libc::pid_t;
    unsafe {
        libc::killpg(pid, libc::SIGTERM);
    }
    let deadline = Deadline::after(grace);
    while !deadline.expired() {
        if matches!(child.try_wait(), Ok(Some(_))) {
            break;
        }
        sleep(0.05);
    }
    unsafe {
        libc::killpg(pid, libc::SIGKILL);
    }
    let _ = child.wait();
}

fn read_range(path: &Path, from: u64) -> Vec<u8> {
    let Ok(mut file) = File::open(path) else {
        return Vec::new();
    };
    if file.seek(SeekFrom::Start(from)).is_err() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let _ = file.read_to_end(&mut out);
    out
}

/// Head + tail of a child log for an error report (`_failure_output`).
fn failure_output(path: &Path) -> String {
    const HEAD: usize = 4096;
    const TAIL: usize = 12288;
    let bytes = fs::read(path).unwrap_or_default();
    if bytes.len() <= HEAD + TAIL {
        return String::from_utf8_lossy(&bytes).trim().to_owned();
    }
    let head = String::from_utf8_lossy(&bytes[..HEAD]);
    let tail = String::from_utf8_lossy(&bytes[bytes.len() - TAIL..]);
    format!(
        "{}\n… [log truncated: {} bytes omitted] …\n{}",
        head.trim_end(),
        bytes.len() - HEAD - TAIL,
        tail.trim_start()
    )
    .trim()
    .to_owned()
}

struct Relay {
    path: PathBuf,
    offset: u64,
    partial: Vec<u8>,
}

impl Relay {
    fn forward(&mut self, final_pass: bool) -> bool {
        let fresh = read_range(&self.path, self.offset);
        self.offset += fresh.len() as u64;
        self.partial.extend_from_slice(&fresh);
        let cut = if final_pass {
            self.partial.len()
        } else {
            self.partial
                .iter()
                .rposition(|byte| *byte == b'\n')
                .map(|i| i + 1)
                .unwrap_or(0)
        };
        if cut == 0 {
            return false;
        }
        let lines: Vec<u8> = self.partial.drain(..cut).collect();
        let mut stderr = std::io::stderr().lock();
        let _ = stderr.write_all(&lines);
        if final_pass && !lines.ends_with(b"\n") {
            let _ = stderr.write_all(b"\n");
        }
        let _ = stderr.flush();
        true
    }
}

/// Run one bounded preparation child; `Err` carries the Python-shaped report.
pub fn run(spec: Spec) -> Result<Output, String> {
    let scratch = scratch_directory()
        .map_err(|error| format!("{} could not start: {error}", spec.failure_subject))?;
    let stdout_path = scratch.0.join("stdout.log");
    let stderr_path = scratch.0.join("stderr.log");
    let stdout_file = File::create(&stdout_path).map_err(|e| e.to_string())?;
    let stderr_file = File::create(&stderr_path).map_err(|e| e.to_string())?;
    let mut command = Command::new(&spec.argv[0]);
    command
        .args(&spec.argv[1..])
        .current_dir(spec.cwd)
        .env_clear()
        .envs(spec.environment)
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .stdin(if spec.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let started = now();
    let deadline = Deadline::after(spec.timeout);
    let mut child = command
        .spawn()
        .map_err(|error| format!("{} could not start: {error}", spec.failure_subject))?;
    if let Some(input) = spec.stdin {
        if let Some(mut pipe) = child.stdin.take() {
            let _ = pipe.write_all(&input);
        }
    }
    let mut relays = [
        Relay {
            path: stdout_path.clone(),
            offset: 0,
            partial: Vec::new(),
        },
        Relay {
            path: stderr_path.clone(),
            offset: 0,
            partial: Vec::new(),
        },
    ];
    let sizes = || {
        fs::metadata(&stdout_path).map(|m| m.len()).unwrap_or(0)
            + fs::metadata(&stderr_path).map(|m| m.len()).unwrap_or(0)
    };
    let mut next_progress = now() + PROGRESS_INTERVAL_SECONDS;
    let mut failure = String::new();
    let status = loop {
        if sizes() > MAX_LOG_BYTES {
            failure = format!("{} exceeded its log budget.", spec.failure_subject);
            break None;
        }
        if spec.progress.is_some() {
            let mut forwarded = false;
            for relay in relays.iter_mut() {
                forwarded |= relay.forward(false);
            }
            if forwarded {
                next_progress = now() + PROGRESS_INTERVAL_SECONDS;
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {}
            Err(error) => {
                failure = format!("{} could not be observed: {error}", spec.failure_subject);
                break None;
            }
        }
        if deadline.expired() {
            failure = format!(
                "{} timed out after {:.1}s (time budget exhausted).",
                spec.failure_subject,
                now() - started
            );
            break None;
        }
        if let Some(label) = spec.progress {
            if now() >= next_progress {
                crate::log::write(
                    crate::log::Level::Info,
                    "server_manager",
                    &format!(
                        "{label}: still running ({:.0}s elapsed, {:.0}s remaining).",
                        now() - started,
                        deadline.remaining()
                    ),
                );
                next_progress = now() + PROGRESS_INTERVAL_SECONDS;
            }
        }
        sleep(0.1);
    };
    let exit_code = status.and_then(|status| status.code());
    let signal = status.and_then(|status| {
        use std::os::unix::process::ExitStatusExt;
        status.signal()
    });
    if !failure.is_empty() || exit_code != Some(0) {
        stop_group(&mut child, spec.termination_grace);
    }
    if spec.progress.is_some() && failure.is_empty() {
        for relay in relays.iter_mut() {
            relay.forward(true);
        }
    }
    let stderr = failure_output(&stderr_path);
    if !failure.is_empty() || exit_code != Some(0) {
        let report = [failure_output(&stdout_path), stderr]
            .into_iter()
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
        if !failure.is_empty() {
            return Err(if report.is_empty() {
                failure
            } else {
                format!("{failure}\n{report}")
            });
        }
        if let Some(signal) = signal {
            return Err(format!(
                "{} was terminated by signal {signal}; preparation was cancelled. {report}",
                spec.failure_subject
            ));
        }
        return Err(format!(
            "{} failed (exit {}): {report}",
            spec.failure_subject,
            exit_code.unwrap_or(-1)
        ));
    }
    let stdout = String::from_utf8_lossy(&fs::read(&stdout_path).unwrap_or_default())
        .trim()
        .to_owned();
    Ok(Output { stdout, stderr })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec<'a>(argv: &[&str], env: &'a Env, timeout: f64) -> Spec<'a> {
        Spec {
            argv: argv.iter().map(|s| s.to_string()).collect(),
            cwd: Path::new("/"),
            environment: env,
            timeout,
            progress: None,
            termination_grace: 1.0,
            failure_subject: "Probe",
            stdin: None,
        }
    }

    #[test]
    fn success_separates_result_from_diagnostics() {
        let env = crate::envpolicy::process_environment();
        let output = run(spec(
            &["sh", "-c", "echo result; echo note >&2"],
            &env,
            10.0,
        ))
        .unwrap();
        assert_eq!(output.stdout, "result");
        assert_eq!(output.stderr, "note");
    }

    #[test]
    fn failure_reports_exit_and_both_streams() {
        let env = crate::envpolicy::process_environment();
        let error = run(spec(
            &["sh", "-c", "echo out; echo err >&2; exit 3"],
            &env,
            10.0,
        ))
        .err()
        .unwrap();
        assert_eq!(error, "Probe failed (exit 3): out\nerr");
    }

    #[test]
    fn timeout_kills_the_whole_group() {
        let env = crate::envpolicy::process_environment();
        let marker =
            std::env::temp_dir().join(format!("tofu-manager-group-{}", std::process::id()));
        let script = format!("(sleep 30; touch {}) & sleep 30", marker.display());
        let error = run(spec(&["sh", "-c", &script], &env, 0.5)).err().unwrap();
        assert!(error.starts_with("Probe timed out after"), "{error}");
        sleep(0.3);
        assert!(!marker.exists());
    }
}
