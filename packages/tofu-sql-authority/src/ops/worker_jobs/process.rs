//! `lib/storage/worker_process.py`: process-bound worker identity codec and
//! local death evidence (`process_claim_protected`), with
//! `lib/process_owner.py::{process_owner, _process_stat}` for this process.

use std::sync::OnceLock;

use regex::Regex;

/// `generated_worker_job_contract.WORKER_PROCESS_OWNER_PREFIX`.
const WORKER_PROCESS_OWNER_PREFIX: &str = "tofu.worker-process/v1:";

struct ParsedProcess {
    boot: String,
    pid_namespace: String,
    pid: i32,
    started: String,
}

fn identity() -> &'static Regex {
    static IDENTITY: OnceLock<Regex> = OnceLock::new();
    IDENTITY.get_or_init(|| {
        Regex::new(&format!(
            "^{}([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}):([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*):([0-9a-f]{{32}})$",
            regex::escape(WORKER_PROCESS_OWNER_PREFIX)
        ))
        .expect("worker process identity pattern")
    })
}

fn fits(digits: &str, maximum: u128) -> bool {
    digits.len() <= 39 && digits.parse::<u128>().is_ok_and(|value| value <= maximum)
}

/// `parse_worker_process`.
fn parse_worker_process(worker_id: &str) -> Option<ParsedProcess> {
    let captures = identity().captures(worker_id)?;
    if worker_id.chars().count() > 256 {
        return None;
    }
    let (boot, namespace, pid, started) = (&captures[1], &captures[2], &captures[3], &captures[4]);
    if !fits(pid, (1u128 << 31) - 1) || !fits(namespace, u128::from(u64::MAX)) || !fits(started, u128::from(u64::MAX)) {
        return None;
    }
    Some(ParsedProcess {
        boot: boot.to_owned(),
        pid_namespace: namespace.to_owned(),
        pid: pid.parse().ok()?,
        started: started.to_owned(),
    })
}

/// `_process_stat(pid)`: `(started, state, threads)`; `None` is any
/// `OSError`/`ValueError`/`IndexError`.
fn process_stat(pid: i64) -> Option<(String, String, i64)> {
    let bytes = std::fs::read(format!("/proc/{pid}/stat")).ok()?;
    let text = String::from_utf8(bytes).ok()?;
    let (_, tail) = text.rsplit_once(')')?;
    let fields: Vec<&str> = tail.split_whitespace().collect();
    let (state, started, threads) = (*fields.first()?, *fields.get(19)?, *fields.get(17)?);
    let decimal = |text: &str| !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit());
    if !matches!(state, "R" | "S" | "D" | "Z" | "T" | "t" | "X" | "x" | "K" | "W" | "P" | "I")
        || !decimal(started)
        || !decimal(threads)
    {
        return None;
    }
    let threads: i64 = threads.parse().unwrap_or(i64::MAX);
    if threads < 1 {
        return None;
    }
    Some((started.to_owned(), state.to_owned(), threads))
}

/// `process_owner()` for this authority process: `(boot, pid_namespace)`.
fn current_owner() -> Option<(String, String)> {
    use std::os::unix::fs::MetadataExt;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?;
    let namespace = std::fs::metadata("/proc/self/ns/pid").ok()?.ino().to_string();
    process_stat(i64::from(std::process::id()))?;
    Some((crate::ops::timers::py::py_strip(&boot).to_owned(), namespace))
}

enum Probe {
    Dead,
    Unknown,
    Alive,
}

fn errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

/// `os.pidfd_open` + zero-timeout poll, or `os.kill(pid, 0)` without pidfd.
fn probe_pid(pid: i32) -> Probe {
    // SAFETY: plain syscalls on an integer pid and a descriptor we own.
    unsafe {
        let fd = libc::syscall(libc::SYS_pidfd_open, pid, 0) as i32;
        if fd < 0 {
            let error = errno();
            if error != libc::ENOSYS {
                return if error == libc::ESRCH { Probe::Dead } else { Probe::Unknown };
            }
            if libc::kill(pid, 0) != 0 {
                return if errno() == libc::ESRCH { Probe::Dead } else { Probe::Unknown };
            }
            return Probe::Alive;
        }
        let mut poll = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
        let ready = libc::poll(&mut poll, 1, 0);
        libc::close(fd);
        if ready < 0 {
            return Probe::Unknown;
        }
        if ready > 0 && poll.revents & libc::POLLIN != 0 {
            return Probe::Dead;
        }
        Probe::Alive
    }
}

/// `process_claim_protected(worker_id)`.
pub fn process_claim_protected(worker_id: &str) -> bool {
    let Some(process) = parse_worker_process(worker_id) else { return false };
    let Some((boot, namespace)) = current_owner() else { return true };
    if process.boot != boot || process.pid_namespace != namespace {
        return true;
    }
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else { return true };
    let Some(line) = status.lines().find(|line| line.starts_with("NSpid:")) else { return true };
    let pids: Vec<&str> = line.split_whitespace().skip(1).collect();
    if pids != [std::process::id().to_string().as_str()] {
        return true;
    }
    match probe_pid(process.pid) {
        Probe::Dead => return false,
        Probe::Unknown => return true,
        Probe::Alive => {}
    }
    let Some((started, state, threads)) = process_stat(i64::from(process.pid)) else { return true };
    !(started != process.started || (matches!(state.as_str(), "Z" | "X" | "x") && threads == 1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identities_parse_like_the_python_codec() {
        assert!(parse_worker_process("legacy:abc").is_none());
        let good = format!(
            "{WORKER_PROCESS_OWNER_PREFIX}01234567-89ab-cdef-0123-456789abcdef:4026531836:1:12345:{}",
            "a".repeat(32)
        );
        assert!(parse_worker_process(&good).is_some());
        assert!(parse_worker_process(&good.replace(":1:", ":2147483648:")).is_none());
        assert!(!process_claim_protected("worker-a"));
    }

    #[test]
    fn own_live_process_is_protected_and_unknown_pid_is_not() {
        let Some((boot, namespace)) = current_owner() else { return };
        let pid = std::process::id();
        let Some((started, ..)) = process_stat(i64::from(pid)) else { return };
        let me = format!("{WORKER_PROCESS_OWNER_PREFIX}{boot}:{namespace}:{pid}:{started}:{}", "b".repeat(32));
        if parse_worker_process(&me).is_some() {
            assert!(process_claim_protected(&me));
            let reused = me.replace(&format!(":{started}:"), &format!(":{}:", started.parse::<u64>().unwrap() + 1));
            assert!(!process_claim_protected(&reused));
        }
    }
}
