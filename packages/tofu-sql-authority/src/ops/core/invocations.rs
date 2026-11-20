//! `operations_pkg/_invocations.py` helpers used by records/events, plus a
//! private port of the worker publication fence they call.

use std::collections::BTreeSet;

use serde_json::{Map, Value};

use super::codecs::hydrated_task_result_value;
use super::py::{attr_get, eq_str, get, map_get, strict_int};
use super::records::record_get_value;
use crate::codec::{load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::params;
use crate::session::{Row, Session, SqlValue};

const ATTEMPT_PREFIX: &str = "conversation-attempt:";

fn task_result_value(session: &Session<'_>, key: &str) -> OpResult<Option<Value>> {
    let row = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["task_results", key],
    )?;
    match row {
        Some(row) => Ok(Some(load(row.get("value_json"))?)),
        None => Ok(None),
    }
}

/// `worker_root_for_task`; `user_id` is the caller's (possibly absent) claim.
pub fn worker_root_for_task(session: &Session<'_>, task_id: &str, user_id: Option<&Value>) -> OpResult<Option<String>> {
    let mut user_id: Option<Value> = user_id.filter(|value| !value.is_null()).cloned();
    let mut current = task_id.to_owned();
    let mut seen = BTreeSet::new();
    let mut conversation: Option<Value> = None;
    loop {
        if !seen.insert(current.clone()) {
            return fail("database_integrity", "Child invocation ancestry is cyclic");
        }
        if current.starts_with(ATTEMPT_PREFIX) {
            return Ok(Some(current));
        }
        let Some(value) = task_result_value(session, &current)? else { return Ok(None) };
        let identity = if value.is_object() { get(&value, "child_invocation").clone() } else { Value::Null };
        if !crate::json::truthy(&identity) {
            return Ok(None);
        }
        let Value::Object(identity) = identity else {
            return fail("database_integrity", "Child invocation identity is malformed");
        };
        let owner = match strict_int(map_get(&identity, "user_id")) {
            Some(owner) if owner >= 1 => owner,
            _ => return fail("database_not_found", "Task not found"),
        };
        if user_id.as_ref().is_some_and(|claim| !py_eq(&Value::from(owner), claim)) {
            return fail("database_not_found", "Task not found");
        }
        user_id = Some(Value::from(owner));
        let conv = map_get(&identity, "conversation_id");
        let parent = map_get(&identity, "parent_task_id");
        let malformed = !eq_str(map_get(&identity, "child_task_id"), &current)
            || !py_eq(get(&value, "user_id"), &Value::from(owner))
            || !py_eq(get(&value, "conv_id"), conv)
            || !matches!(conv, Value::String(text) if !text.is_empty())
            || conversation.as_ref().is_some_and(|known| !py_eq(known, conv))
            || !matches!(parent, Value::String(text) if !text.is_empty());
        if malformed {
            return fail("database_integrity", "Child invocation ancestry is malformed");
        }
        conversation = Some(conv.clone());
        current = parent.as_str().expect("parent is text").to_owned();
    }
}

/// `worker_root_for_event_stream`.
pub fn worker_root_for_event_stream(
    session: &Session<'_>,
    task_id: &str,
    user_id: Option<&Value>,
) -> OpResult<Option<String>> {
    let Some((parent, child)) = task_id.split_once("#agent:") else {
        return worker_root_for_task(session, task_id, user_id);
    };
    let parent_root = worker_root_for_task(session, parent, user_id)?;
    let child_root = worker_root_for_task(session, child, user_id)?;
    if parent_root.is_none() && child_root.is_none() {
        return Ok(None);
    }
    let value = task_result_value(session, child)?.unwrap_or_else(|| Value::Object(Map::new()));
    let birth = attr_get(&value, "child_invocation")?;
    let birth = if crate::json::truthy(birth) { birth.clone() } else { Value::Object(Map::new()) };
    if parent_root != child_root || parent_root.is_none() {
        return fail("database_protocol_error", "Inspector stream differs from child birth");
    }
    if !eq_str(attr_get(&birth, "parent_task_id")?, parent) || !eq_str(attr_get(&birth, "child_task_id")?, child) {
        return fail("database_protocol_error", "Inspector stream differs from child birth");
    }
    Ok(parent_root)
}

fn tool_rows(value: &Value) -> OpResult<Vec<Value>> {
    let raw = attr_get(value, "tool_rounds")?;
    let raw = if crate::json::truthy(raw) { raw.clone() } else { Value::Array(Vec::new()) };
    let result = match raw {
        Value::String(text) => match serde_json::from_str::<Value>(&text) {
            Ok(parsed) => parsed,
            Err(_) => return fail("database_protocol_error", "Invalid child tool receipts"),
        },
        other => other,
    };
    match result {
        Value::Array(items) => Ok(items),
        _ => fail("database_protocol_error", "Invalid child tool receipts"),
    }
}

fn effect_of(row: &Value) -> Option<&Map<String, Value>> {
    match row {
        Value::Object(map) => match map.get("executionEffect") {
            Some(Value::Object(effect)) => Some(effect),
            _ => None,
        },
        _ => None,
    }
}

fn effect_operations(value: &Value, entered_only: bool) -> OpResult<BTreeSet<String>> {
    let mut result = BTreeSet::new();
    for row in tool_rows(value)? {
        let Some(effect) = effect_of(&row) else { continue };
        let operation = match effect.get("operationId") {
            Some(Value::String(text)) if !text.is_empty() => text.clone(),
            _ => return fail("database_protocol_error", "Invalid child effect identity"),
        };
        if !entered_only || eq_str(map_get(effect, "state"), "entered") {
            result.insert(operation);
        }
    }
    Ok(result)
}

/// `starts_new_tool_effect`.
pub fn starts_new_tool_effect(
    session: &Session<'_>,
    task_id: &str,
    incoming: &Map<String, Value>,
    intent: Option<&str>,
) -> OpResult<bool> {
    if !incoming.contains_key("tool_rounds") {
        if intent.is_some() {
            return fail("database_protocol_error", "Tool entry requires its exact invocation receipt");
        }
        return Ok(false);
    }
    let current = task_result_value(session, task_id)?;
    let value = match current {
        Some(loaded) => hydrated_task_result_value(loaded, Some(&["tool_rounds"]))?,
        None => Value::Object(Map::new()),
    };
    let incoming_value = Value::Object(incoming.clone());
    if let Some(intent) = intent {
        let matching = |rows: Vec<Value>| -> Vec<Value> {
            rows.into_iter()
                .filter(|row| effect_of(row).is_some_and(|effect| eq_str(map_get(effect, "operationId"), intent)))
                .collect()
        };
        let selected = matching(tool_rows(&incoming_value)?);
        let invalid = selected.len() != 1 || {
            let row = &selected[0];
            let effect = effect_of(row).expect("selected effect");
            !eq_str(get(row, "taskId"), task_id)
                || !eq_str(map_get(effect, "scope"), "invocation")
                || !eq_str(map_get(effect, "state"), "entered")
                || !map_get(effect, "reconciliation").is_null()
        };
        if invalid {
            return fail("database_protocol_error", "Tool entry requires its exact invocation receipt");
        }
        let previous = matching(tool_rows(&value)?);
        if !previous.is_empty() && !py_eq(&Value::Array(previous), &Value::Array(selected)) {
            return fail("database_conflict", "Tool effect admission changed its occurrence");
        }
        return Ok(true);
    }
    let incoming_ops = effect_operations(&incoming_value, true)?;
    let current_ops = effect_operations(&value, false)?;
    Ok(incoming_ops.difference(&current_ops).next().is_some())
}

/// `guard_child_record`.
pub fn guard_child_record(session: &Session<'_>, key: &str, incoming: Option<&Value>) -> OpResult<()> {
    let current = record_get_value(session, "task_results", key)?;
    let value = if current.is_null() { Value::Null } else { current["value"].clone() };
    let value = if crate::json::truthy(&value) { value } else { Value::Object(Map::new()) };
    let identity = attr_get(&value, "child_invocation")?.clone();
    let incoming_map = incoming.and_then(Value::as_object);
    let incoming_birth = incoming_map.map(|map| map_get(map, "child_invocation"));
    if !crate::json::truthy(&identity) && incoming_birth.is_some_and(crate::json::truthy) {
        return fail("database_protocol_error", "Child birth requires invocation registration");
    }
    if crate::json::truthy(&identity) {
        let Some(incoming) = incoming_map else {
            return fail("database_conflict", "Child identity is immutable");
        };
        if !py_eq(map_get(incoming, "child_invocation"), &identity) {
            return fail("database_conflict", "Child identity is immutable");
        }
        let field = |name: &str| -> OpResult<Value> {
            match &identity {
                Value::Object(map) => match map.get(name) {
                    Some(value) => Ok(value.clone()),
                    None => internal(format!("KeyError: {name}")),
                },
                _ => internal("TypeError: identity is not subscriptable"),
            }
        };
        if !py_eq(map_get(incoming, "task_id"), &field("child_task_id")?)
            || !py_eq(map_get(incoming, "user_id"), &field("user_id")?)
            || !py_eq(map_get(incoming, "conv_id"), &field("conversation_id")?)
        {
            return fail("database_conflict", "Child identity is immutable");
        }
    }
    Ok(())
}

// ── private port of operations_pkg/_worker_jobs.py; shared owner: worker_jobs ──

const WORKER_JOB_COLUMNS: &str = "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, \
request_digest, status, priority, available_at_ms, claim_owner, \
lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, \
cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, \
result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms";
/// `generated_worker_job_contract.WORKER_LEASE_MAX_NOW_MS`.
const WORKER_LEASE_MAX_NOW_MS: i64 = 9_223_372_036_854_475_000;
const WORKER_LEASE_EPOCH_CHARACTERS: usize = 32;
const WORKER_PROCESS_OWNER_PREFIX: &str = "tofu.worker-process/v1:";

/// private port of `_worker_jobs.py::_starts_tool_effect`; shared owner: worker_jobs
pub fn starts_tool_effect(event: &Value) -> bool {
    if !eq_str(get(event, "type"), "tool_effect") {
        return false;
    }
    match get(event, "executionEffect") {
        Value::Object(effect) => {
            eq_str(map_get(effect, "state"), "entered") && map_get(effect, "reconciliation").is_null()
        }
        _ => false,
    }
}

/// private port of `_worker_jobs.py::_lease_authority`; shared owner: worker_jobs
fn lease_authority(session: &Session<'_>) -> OpResult<(String, i64)> {
    let snapshot = session.worker_lease_authority()?;
    let valid = snapshot.epoch.len() == WORKER_LEASE_EPOCH_CHARACTERS
        && snapshot.epoch.chars().all(|c| "0123456789abcdef".contains(c))
        && 0 < snapshot.lease_now_ms
        && snapshot.lease_now_ms <= WORKER_LEASE_MAX_NOW_MS
        && 0 < snapshot.utc_now_ms
        && snapshot.utc_now_ms <= snapshot.lease_now_ms;
    if !valid {
        return fail("database_internal", "Worker lease authority clock is invalid");
    }
    Ok((snapshot.epoch, snapshot.lease_now_ms))
}

fn row_int(row: &Row, column: &str) -> OpResult<i64> {
    row.int(column)
}

/// private port of `_worker_jobs.py::_live_claim`; shared owner: worker_jobs
fn live_claim(row: Option<&Row>, worker_id: &str, fence: i64, epoch: &str, now: i64) -> OpResult<bool> {
    let Some(row) = row else { return Ok(false) };
    if python_str_column(row.get("status")) != "running" {
        return Ok(false);
    }
    if python_str_column(row.get("claim_owner")) != worker_id {
        return Ok(false);
    }
    if row_int(row, "fencing_token")? != fence {
        return Ok(false);
    }
    if python_str_column(row.get("lease_authority_epoch")) != epoch {
        return Ok(false);
    }
    Ok(row_int(row, "lease_deadline_ms")? > now || process_claim_protected(&python_str_column(row.get("claim_owner"))))
}

/// `str(column)` for TEXT columns (NULL is `'None'`).
fn python_str_column(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => crate::json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(_) => value.text_or_empty(),
    }
}

/// private port of `_worker_jobs.py::_require_conversation_publication_claim`; shared owner: worker_jobs
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
    let proof_claim = if proof.is_null() {
        None
    } else {
        let valid = match proof {
            Value::Object(map) => {
                map.len() == 2
                    && matches!(map.get("worker_id"), Some(Value::String(id)) if (1..=256).contains(&id.chars().count()))
                    && matches!(map.get("fencing_token").and_then(strict_int), Some(token) if token > 0)
            }
            _ => false,
        };
        if !valid {
            return fail("database_protocol_error", "Invalid worker publication claim");
        }
        let map = proof.as_object().expect("validated proof");
        Some((
            map["worker_id"].as_str().expect("worker id").to_owned(),
            strict_int(&map["fencing_token"]).expect("token"),
        ))
    };
    if task_id != format!("{ATTEMPT_PREFIX}{attempt_id}") {
        if proof_claim.is_some() {
            return fail("database_conflict", "Attempt has no durable worker binding");
        }
        return Ok(());
    }
    let Some((worker_id, fence)) = proof_claim else {
        return fail("database_conflict", "Durable attempt requires a worker claim");
    };
    session.lock_key("worker_job", task_id);
    let row = session.fetch_one_for_update(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id=? AND user_id=?"),
        &params![task_id, user_id],
    )?;
    let (epoch, now) = lease_authority(session)?;
    let denied = || -> OpResult<()> { fail("database_conflict", "Worker claim cannot publish this attempt") };
    if !live_claim(row.as_ref(), &worker_id, fence, &epoch, now)? {
        return denied();
    }
    let row = row.expect("live claim row");
    if !matches!(row.get("task_kind"), SqlValue::Text(kind) if kind == "conversation-attempt") {
        return denied();
    }
    let payload = load(row.get("payload_json"))?;
    let payload = if crate::json::truthy(&payload) { payload } else { Value::Object(Map::new()) };
    if !eq_str(attr_get(&payload, "attemptId")?, attempt_id) {
        return denied();
    }
    if !terminal && row_int(&row, "cancel_requested_at_ms")? > 0 {
        let entered = drain_entered
            && session
                .fetch_one(
                    "SELECT 1 AS entered FROM storage_generation_attempts a \
                     JOIN storage_conversation_turns t ON t.turn_id=a.turn_id \
                     WHERE a.attempt_id=? AND t.user_id=? AND a.started_at IS NOT NULL",
                    &params![attempt_id, user_id],
                )?
                .is_some();
        if !entered {
            return denied();
        }
    }
    Ok(())
}

// ── private port of lib/storage/worker_process.py + lib/process_owner.py; shared owner: worker_jobs ──

struct WorkerProcess {
    boot: String,
    pid_namespace: String,
    pid: i32,
    started: String,
}

fn positive_decimal(field: &str) -> Option<u128> {
    if field.is_empty() || field.starts_with('0') || !field.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    field.parse().ok()
}

fn parse_worker_process(worker_id: &str) -> Option<WorkerProcess> {
    let raw = worker_id.strip_prefix(WORKER_PROCESS_OWNER_PREFIX)?;
    let fields: Vec<&str> = raw.split(':').collect();
    if fields.len() != 5 || worker_id.chars().count() > 256 {
        return None;
    }
    let boot = fields[0];
    let lower_hex = |text: &str| text.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    let groups: Vec<&str> = boot.split('-').collect();
    if groups.len() != 5
        || groups.iter().map(|group| group.len()).collect::<Vec<_>>() != [8, 4, 4, 4, 12]
        || !groups.iter().all(|group| lower_hex(group))
    {
        return None;
    }
    let namespace = positive_decimal(fields[1])?;
    let pid = positive_decimal(fields[2])?;
    let started = positive_decimal(fields[3])?;
    if fields[4].len() != 32 || !lower_hex(fields[4]) {
        return None;
    }
    if pid > i32::MAX as u128 || namespace.max(started) > u64::MAX as u128 {
        return None;
    }
    Some(WorkerProcess {
        boot: boot.to_owned(),
        pid_namespace: fields[1].to_owned(),
        pid: pid as i32,
        started: fields[3].to_owned(),
    })
}

/// `_process_stat(pid)` -> (started, state, threads); `Err(true)` is a
/// missing file (`FileNotFoundError`), `Err(false)` any other failure.
fn process_stat(pid: i32) -> Result<(String, String, i64), bool> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .map_err(|error| error.kind() == std::io::ErrorKind::NotFound)?;
    let tail = text.rsplit_once(')').ok_or(false)?.1;
    let fields: Vec<&str> = tail.split_whitespace().collect();
    if fields.len() < 20 {
        return Err(false);
    }
    let (state, started, threads) = (fields[0], fields[19], fields[17]);
    let valid_state = ["R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"].contains(&state);
    let decimal = |text: &str| !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit());
    if !valid_state || !decimal(started) || !decimal(threads) {
        return Err(false);
    }
    let threads: i64 = threads.parse().map_err(|_| false)?;
    if threads < 1 {
        return Err(false);
    }
    Ok((started.to_owned(), state.to_owned(), threads))
}

fn process_owner() -> Option<(String, String)> {
    use std::os::unix::fs::MetadataExt;
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").ok()?.trim().to_owned();
    let namespace = std::fs::metadata("/proc/self/ns/pid").ok()?.ino().to_string();
    process_stat(std::process::id() as i32).ok()?;
    Some((boot, namespace))
}

/// `process_claim_protected`.
fn process_claim_protected(worker_id: &str) -> bool {
    let Some(process) = parse_worker_process(worker_id) else { return false };
    let Some((boot, namespace)) = process_owner() else { return true };
    if process.boot != boot || process.pid_namespace != namespace {
        return true;
    }
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else { return true };
    let Some(line) = status.lines().find(|line| line.starts_with("NSpid:")) else { return true };
    let pids: Vec<&str> = line.split_whitespace().skip(1).collect();
    if pids != [std::process::id().to_string()] {
        return true;
    }
    // SAFETY: plain syscalls on an integer pid / owned fd.
    unsafe {
        let fd = libc::syscall(libc::SYS_pidfd_open, process.pid, 0) as i32;
        if fd < 0 {
            let errno = *libc::__errno_location();
            if errno == libc::ESRCH {
                return false;
            }
            if errno != libc::ENOSYS {
                return true;
            }
            if libc::kill(process.pid, 0) != 0 {
                return *libc::__errno_location() != libc::ESRCH;
            }
        } else {
            let mut poll = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
            let ready = libc::poll(&mut poll, 1, 0);
            libc::close(fd);
            if ready < 0 {
                return true;
            }
            if ready > 0 && poll.revents & libc::POLLIN != 0 {
                return false;
            }
        }
    }
    match process_stat(process.pid) {
        Ok((started, state, threads)) => {
            !(started != process.started || (["Z", "X", "x"].contains(&state.as_str()) && threads == 1))
        }
        Err(_) => true,
    }
}
