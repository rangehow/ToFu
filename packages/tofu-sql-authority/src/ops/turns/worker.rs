//! Private ports of the worker-publication fence used by turn commands.
//!
//! `operations_pkg/_worker_jobs.py::_require_conversation_publication_claim`,
//! `_live_claim`, `_starts_tool_effect` and
//! `lib/storage/worker_process.py::process_claim_protected`; shared owner:
//! worker_jobs.

use serde_json::Value;

use crate::codec::load;
use crate::error::{fail, OpResult};
use crate::params;
use crate::payload::get;
use crate::session::{Row, Session};

const WORKER_PROCESS_OWNER_PREFIX: &str = "tofu.worker-process/v1:";
const WORKER_LEASE_MAX_NOW_MS: i64 = 9_223_372_036_854_475_000;

struct WorkerProcess {
    boot: String,
    namespace: String,
    pid: i32,
    started: String,
}

fn parse_worker_process(worker_id: &str) -> Option<WorkerProcess> {
    if worker_id.chars().count() > 256 {
        return None;
    }
    let rest = worker_id.strip_prefix(WORKER_PROCESS_OWNER_PREFIX)?;
    let parts: Vec<&str> = rest.split(':').collect();
    if parts.len() != 5 {
        return None;
    }
    let (boot, namespace, pid, started, instance) = (parts[0], parts[1], parts[2], parts[3], parts[4]);
    let uuid_ok = boot.len() == 36
        && boot.char_indices().all(|(index, c)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                c == '-'
            } else {
                c.is_ascii_digit() || ('a'..='f').contains(&c)
            }
        });
    let decimal = |text: &str| !text.is_empty() && !text.starts_with('0') && text.bytes().all(|b| b.is_ascii_digit());
    let hex32 = instance.len() == 32 && instance.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if !uuid_ok || !decimal(namespace) || !decimal(pid) || !decimal(started) || !hex32 {
        return None;
    }
    let pid: u128 = pid.parse().ok()?;
    let fits_u64 = |text: &str| text.parse::<u128>().ok().is_some_and(|value| value <= u64::MAX as u128);
    if pid > i32::MAX as u128 || !fits_u64(namespace) || !fits_u64(started) {
        return None;
    }
    Some(WorkerProcess { boot: boot.into(), namespace: namespace.into(), pid: pid as i32, started: started.into() })
}

fn process_stat(pid: i32) -> Result<(String, String, i64), std::io::Error> {
    let invalid = || std::io::Error::new(std::io::ErrorKind::InvalidData, "invalid stat");
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat"))?;
    let tail = stat.rsplit_once(')').ok_or_else(invalid)?.1;
    let fields: Vec<&str> = tail.split_whitespace().collect();
    if fields.len() < 20 {
        return Err(invalid());
    }
    let (state, started, threads) = (fields[0], fields[19], fields[17]);
    if !matches!(state, "R" | "S" | "D" | "Z" | "T" | "t" | "X" | "x" | "K" | "W" | "P" | "I")
        || started.is_empty()
        || !started.bytes().all(|b| b.is_ascii_digit())
        || threads.is_empty()
        || !threads.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(invalid());
    }
    let threads: i64 = threads.parse().map_err(|_| invalid())?;
    if threads < 1 {
        return Err(invalid());
    }
    Ok((started.to_owned(), state.to_owned(), threads))
}

/// `process_owner()` boot id and pid namespace of this process.
fn current_owner() -> Option<(String, String)> {
    use std::os::unix::fs::MetadataExt;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?.trim().to_owned();
    let namespace = std::fs::metadata("/proc/self/ns/pid").ok()?.ino().to_string();
    process_stat(std::process::id() as i32).ok()?;
    Some((boot, namespace))
}

/// `process_claim_protected(worker_id)`.
fn process_claim_protected(worker_id: &str) -> bool {
    let Some(process) = parse_worker_process(worker_id) else { return false };
    let Some((boot, namespace)) = current_owner() else { return true };
    if process.boot != boot || process.namespace != namespace {
        return true;
    }
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else { return true };
    let Some(line) = status.lines().find(|line| line.starts_with("NSpid:")) else { return true };
    let pids: Vec<&str> = line.split_whitespace().skip(1).collect();
    if pids != [std::process::id().to_string().as_str()] {
        return true;
    }
    // SAFETY: plain syscalls on an integer pid; the returned fd is closed.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, process.pid, 0) };
    if fd >= 0 {
        let mut poll = libc::pollfd { fd: fd as i32, events: libc::POLLIN, revents: 0 };
        let ready = unsafe { libc::poll(&mut poll, 1, 0) };
        unsafe { libc::close(fd as i32) };
        if ready > 0 && poll.revents & libc::POLLIN != 0 {
            return false;
        }
    } else {
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::ESRCH) => return false,
            Some(libc::ENOSYS) => {
                if unsafe { libc::kill(process.pid, 0) } != 0 {
                    return std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH);
                }
            }
            _ => return true,
        }
    }
    match process_stat(process.pid) {
        Ok((started, state, threads)) => {
            !(started != process.started || (matches!(state.as_str(), "Z" | "X" | "x") && threads == 1))
        }
        Err(_) => true,
    }
}

fn live_claim(row: &Row, worker_id: &str, fence: i64, epoch: &str, now: i64) -> OpResult<bool> {
    Ok(row.text("status") == "running"
        && row.get("claim_owner").text_or_empty() == worker_id
        && row.int("fencing_token")? == fence
        && row.get("lease_authority_epoch").text_or_empty() == epoch
        && (row.int("lease_deadline_ms")? > now || process_claim_protected(&row.get("claim_owner").text_or_empty())))
}

/// `_starts_tool_effect(event)`.
pub fn starts_tool_effect(event: &Value) -> bool {
    let Value::Object(event) = event else { return false };
    if event.get("type") != Some(&Value::String("tool_effect".into())) {
        return false;
    }
    matches!(event.get("executionEffect"), Some(Value::Object(effect))
        if effect.get("state") == Some(&Value::String("entered".into()))
            && effect.get("reconciliation").is_none_or(Value::is_null))
}

/// `_require_conversation_publication_claim(...)`.
#[allow(clippy::too_many_arguments)]
pub fn require_conversation_publication_claim(
    session: &Session<'_>,
    attempt_id: &str,
    task_id: &str,
    user_id: i64,
    proof: &Value,
    terminal: bool,
    drain_entered: bool,
) -> OpResult<()> {
    let parsed = match proof {
        Value::Null => None,
        Value::Object(map) => {
            let keys_ok = map.len() == 2 && map.contains_key("worker_id") && map.contains_key("fencing_token");
            let worker = match map.get("worker_id") {
                Some(Value::String(worker)) if !worker.is_empty() && worker.chars().count() <= 256 => {
                    Some(worker.clone())
                }
                _ => None,
            };
            let fence = match map.get("fencing_token") {
                Some(Value::Number(number)) => number.as_i64().filter(|value| *value > 0),
                _ => None,
            };
            match (keys_ok, worker, fence) {
                (true, Some(worker), Some(fence)) => Some((worker, fence)),
                _ => return fail("database_protocol_error", "Invalid worker publication claim"),
            }
        }
        _ => return fail("database_protocol_error", "Invalid worker publication claim"),
    };
    if task_id != format!("conversation-attempt:{attempt_id}") {
        if parsed.is_some() {
            return fail("database_conflict", "Attempt has no durable worker binding");
        }
        return Ok(());
    }
    let Some((worker_id, fence)) = parsed else {
        return fail("database_conflict", "Durable attempt requires a worker claim");
    };
    let row = session.fetch_one(
        "SELECT task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, request_digest, status, priority, available_at_ms, claim_owner, lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms FROM storage_worker_jobs WHERE task_id=? AND user_id=?",
        &params![task_id, user_id],
    )?;
    let lease = session.worker_lease_authority()?;
    let epoch_ok =
        lease.epoch.len() == 32 && lease.epoch.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    if !epoch_ok
        || !(0 < lease.lease_now_ms && lease.lease_now_ms <= WORKER_LEASE_MAX_NOW_MS)
        || !(0 < lease.utc_now_ms && lease.utc_now_ms <= lease.lease_now_ms)
    {
        return fail("database_internal", "Worker lease authority clock is invalid");
    }
    let admitted = match &row {
        Some(row) => {
            live_claim(row, &worker_id, fence, &lease.epoch, lease.lease_now_ms)?
                && row.text("task_kind") == "conversation-attempt"
                && {
                    let payload = load(row.get("payload_json"))?;
                    match &payload {
                        Value::Object(map) => get(map, "attemptId") == &Value::String(attempt_id.into()),
                        _ if !crate::json::truthy(&payload) => false,
                        _ => return crate::error::internal("job payload has no get"),
                    }
                }
        }
        None => false,
    };
    if !admitted {
        return fail("database_conflict", "Worker claim cannot publish this attempt");
    }
    let row = row.expect("admitted row");
    if !terminal && row.int("cancel_requested_at_ms")? > 0 {
        let entered = drain_entered
            && session
                .fetch_one(
                    "SELECT 1 AS entered FROM storage_generation_attempts a JOIN storage_conversation_turns t ON t.turn_id=a.turn_id WHERE a.attempt_id=? AND t.user_id=? AND a.started_at IS NOT NULL",
                    &params![attempt_id, user_id],
                )?
                .is_some();
        if !entered {
            return fail("database_conflict", "Worker claim cannot publish this attempt");
        }
    }
    Ok(())
}
