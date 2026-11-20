//! `operation_domains/worker_jobs.py`: durable worker-job claims, leases,
//! fencing, cancellation and settlement (`operations_pkg/_worker_jobs.py`).
//!
//! The database is the task authority: every execution starts with
//! `worker_job.claim_next` and every later write proves the returned fencing
//! token. SQLite serializes claims through its one writer.

mod process;

use std::collections::BTreeSet;
use std::sync::OnceLock;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use self::process::process_claim_protected;
use super::timers::py::{column_str, column_text, load_or_empty};
use super::{register as add, OperationSpec, Registry};
use crate::codec::{dump, py_eq};
use crate::error::{fail, internal, OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{LeaseSnapshot, Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "worker_job.active.list", OperationSpec::query(worker_job_active_list));
    add(registry, "worker_job.get", OperationSpec::query(worker_job_get));
    add(registry, "worker_job.enqueue", OperationSpec::command(true, worker_job_enqueue));
    // A retained claim ACK resolves one acquisition, never grants execution:
    // the runner revalidates the current token before entering any handler.
    add(
        registry,
        "worker_job.claim_next",
        OperationSpec::command(false, worker_job_claim_next).with_receipt_when_true("retain_claim"),
    );
    add(registry, "worker_job.claim_horizon", OperationSpec::query(worker_job_claim_horizon));
    add(registry, "worker_job.defer", OperationSpec::command(false, worker_job_defer));
    add(registry, "worker_job.heartbeat", OperationSpec::command(false, worker_job_heartbeat));
    add(registry, "worker_job.claim_state", OperationSpec::query(worker_job_claim_state));
    add(registry, "worker_job.request_cancel", OperationSpec::command(true, worker_job_request_cancel));
    add(registry, "worker_job.complete", OperationSpec::command(true, worker_job_complete));
}

const WORKER_JOB_COLUMNS: &str = "task_id, user_id, tenant_id, task_kind, payload_json, idempotency_key, \
     request_digest, status, priority, available_at_ms, claim_owner, \
     lease_deadline_ms, lease_authority_epoch, fencing_token, attempt_no, heartbeat_at_ms, \
     cancel_sequence, wake_sequence, cancel_requested_at_ms, cancel_reason, replay_cursor, \
     result_ref, error_json, created_at_ms, updated_at_ms, terminal_at_ms";

/// `generated_worker_job_contract.py` (source `contracts/worker_job_lifecycle_v1.json`).
const TERMINAL_STATUSES: &[&str] = &["succeeded", "failed", "cancelled"];
const MAX_CLOCK_MS: i64 = 9_223_372_036_854_775_000;
const WORKER_LEASE_MAX_NOW_MS: i64 = 9_223_372_036_854_475_000;
const WORKER_LEASE_MAX_MS: i64 = 300_000;
const WORKER_LEASE_EPOCH_CHARACTERS: usize = 32;

const MAX_JOB_PAYLOAD_BYTES: usize = 1024 * 1024;
const MAX_ERROR_BYTES: usize = 64 * 1024;
// Caller clocks are bounded observation/evidence, never lease authority.
const MAX_CLOCK_SKEW_MS: i64 = 24 * 60 * 60 * 1000;

/// `lib/automations/contract.py::RESERVED_WORKER_TASK_KINDS`.
fn reserved_task_kinds() -> &'static BTreeSet<String> {
    static KINDS: OnceLock<BTreeSet<String>> = OnceLock::new();
    KINDS.get_or_init(|| {
        let schema: Value =
            serde_json::from_str(include_str!("../../../../contracts/automation_storage_v1.schema.json"))
                .expect("automation storage schema");
        schema["x-reservedTaskKinds"].as_object().expect("x-reservedTaskKinds").keys().cloned().collect()
    })
}

fn protocol(message: &str) -> crate::error::Failure {
    StorageError::protocol(message).into()
}

fn optional_text(payload: &Payload, key: &str, maximum: usize) -> OpResult<String> {
    match payload.get(key) {
        None => Ok(String::new()),
        Some(Value::String(text)) if char_len(text) <= maximum => Ok(text.clone()),
        Some(_) => Err(protocol(&format!("Invalid {key} in storage request"))),
    }
}

fn job_owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn wall_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `_job_now`: validate the bounded caller observation.
fn job_now(payload: &Payload) -> OpResult<i64> {
    let client_now = integer(payload, "now_ms", None, Some(0), Some(MAX_CLOCK_MS))?;
    if client_now - wall_ms() > MAX_CLOCK_SKEW_MS {
        return fail("database_protocol_error", "Worker job clock is too far ahead of the server clock");
    }
    Ok(client_now)
}

/// `_lease_authority`: fail closed on an invalid lifecycle clock.
fn lease_authority(session: &Session<'_>) -> OpResult<LeaseSnapshot> {
    let snapshot = session.worker_lease_authority()?;
    let valid = snapshot.epoch.len() == WORKER_LEASE_EPOCH_CHARACTERS
        && snapshot.epoch.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        && 0 < snapshot.lease_now_ms
        && snapshot.lease_now_ms <= WORKER_LEASE_MAX_NOW_MS
        && 0 < snapshot.utc_now_ms
        && snapshot.utc_now_ms <= snapshot.lease_now_ms;
    if !valid {
        return fail("database_internal", "Worker lease authority clock is invalid");
    }
    Ok(snapshot)
}

fn col_int(row: &Row, column: &str) -> OpResult<i64> {
    row.int(column)
}

fn col_str(row: &Row, column: &str) -> OpResult<String> {
    Ok(column_str(row.require(column)?))
}

/// `_live_claim`.
fn live_claim(row: Option<&Row>, worker_id: &str, fence: i64, epoch: &str, now: i64) -> OpResult<bool> {
    let Some(row) = row else { return Ok(false) };
    Ok(col_str(row, "status")? == "running"
        && col_str(row, "claim_owner")? == worker_id
        && col_int(row, "fencing_token")? == fence
        && col_str(row, "lease_authority_epoch")? == epoch
        && (col_int(row, "lease_deadline_ms")? > now || process_claim_protected(&col_str(row, "claim_owner")?)))
}

/// `_job_document`.
fn job_document(row: &Row) -> OpResult<Value> {
    Ok(json!({
        "taskId": col_str(row, "task_id")?,
        "userId": col_int(row, "user_id")?,
        "tenantId": column_text(row.require("tenant_id")?),
        "taskKind": col_str(row, "task_kind")?,
        "payload": load_or_empty(row.require("payload_json")?)?,
        "idempotencyKey": col_str(row, "idempotency_key")?,
        "status": col_str(row, "status")?,
        "priority": col_int(row, "priority")?,
        "availableAtMs": col_int(row, "available_at_ms")?,
        "claimOwner": column_text(row.require("claim_owner")?),
        "leaseDeadlineMs": col_int(row, "lease_deadline_ms")?,
        "fencingToken": col_int(row, "fencing_token")?,
        "attempt": col_int(row, "attempt_no")?,
        "heartbeatAtMs": col_int(row, "heartbeat_at_ms")?,
        "cancelSequence": col_int(row, "cancel_sequence")?,
        "wakeSequence": col_int(row, "wake_sequence")?,
        "cancelRequestedAtMs": col_int(row, "cancel_requested_at_ms")?,
        "cancelReason": column_text(row.require("cancel_reason")?),
        "replayCursor": col_int(row, "replay_cursor")?,
        "resultRef": column_text(row.require("result_ref")?),
        "error": load_or_empty(row.require("error_json")?)?,
        "createdAtMs": col_int(row, "created_at_ms")?,
        "updatedAtMs": col_int(row, "updated_at_ms")?,
        "terminalAtMs": col_int(row, "terminal_at_ms")?,
    }))
}

fn job_row(session: &Session<'_>, task_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id = ?"),
        &params![task_id],
    )
}

fn job_row_for_update(session: &Session<'_>, task_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one_for_update(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id = ?"),
        &params![task_id],
    )
}

fn existing_document(row: Option<Row>, missing: &str) -> OpResult<Value> {
    match row {
        Some(row) => job_document(&row),
        None => internal(missing.to_owned()),
    }
}

fn worker_job_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = job_owner(payload)?;
    let row = session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id = ? AND user_id = ?"),
        &params![&task_id, user_id],
    )?;
    match row {
        None => Ok(Value::Null),
        Some(row) => job_document(&row),
    }
}

/// `_claim_kind_filter`: the deduplicated advertised kinds.
fn claim_kinds(payload: &Payload) -> OpResult<Vec<String>> {
    let values = match payload.get("task_kinds") {
        None | Some(Value::Null) => {
            return fail("database_protocol_error", "worker_job.claim_next requires explicit task_kinds")
        }
        Some(Value::Array(values)) if (1..=32).contains(&values.len()) => values,
        Some(_) => return fail("database_protocol_error", "Invalid task_kinds in storage request"),
    };
    let mut kinds: Vec<String> = Vec::new();
    for value in values {
        let Value::String(kind) = value else {
            return fail("database_protocol_error", "Invalid task_kinds in storage request");
        };
        if kind.is_empty() || char_len(kind) > 128 {
            return fail("database_protocol_error", "Invalid task_kinds in storage request");
        }
        if !kinds.contains(kind) {
            kinds.push(kind.clone());
        }
    }
    Ok(kinds)
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(", ")
}

fn worker_job_active_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let Some(raw_owner) = payload.get("user_id") else {
        return fail("database_protocol_error", "Worker activity requires an explicit owner scope");
    };
    let owner = if raw_owner.is_null() { None } else { Some(job_owner(payload)?) };
    let kinds = claim_kinds(payload)?;
    let owner_clause = if owner.is_some() { " AND user_id=?" } else { "" };
    let mut parameters: Vec<SqlValue> = kinds.iter().map(SqlValue::from).collect();
    if let Some(owner) = owner {
        parameters.push(SqlValue::Integer(owner));
    }
    let rows = session.fetch_all(
        &format!(
            "SELECT task_id,user_id,task_kind,status,created_at_ms,payload_json \
             FROM storage_worker_jobs WHERE status IN ('queued','running','suspended') \
             AND task_kind IN ({}){owner_clause} \
             ORDER BY created_at_ms,task_id",
            placeholders(kinds.len())
        ),
        &parameters,
    )?;
    let mut projected = Vec::with_capacity(rows.len());
    for row in &rows {
        let loaded = load_or_empty(row.require("payload_json")?)?;
        let Value::Object(document) = loaded else {
            return internal("'payload' object has no attribute 'get'");
        };
        let conversation_id = match document.get("conversationId") {
            Some(Value::String(text)) => text.clone(),
            _ => String::new(),
        };
        projected.push(json!({
            "taskId": row.require("task_id")?.to_json()?,
            "userId": row.require("user_id")?.to_json()?,
            "taskKind": row.require("task_kind")?.to_json()?,
            "conversationId": conversation_id,
            "status": row.require("status")?.to_json()?,
            "createdAtMs": row.require("created_at_ms")?.to_json()?,
        }));
    }
    Ok(Value::Array(projected))
}

/// Generic producers cannot bypass automation's atomic job owners.
fn worker_job_enqueue(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    if reserved_task_kinds().contains(&required_text(payload, "task_kind", 128)?) {
        return fail("database_protocol_error", "Automation jobs require their atomic owner");
    }
    worker_job_enqueue_owned(session, payload)
}

fn worker_job_enqueue_owned(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = job_owner(payload)?;
    let tenant_id = optional_text(payload, "tenant_id", 256)?;
    let task_kind = required_text(payload, "task_kind", 128)?;
    let idempotency_key = required_text(payload, "idempotency_key", 256)?;
    let priority = integer(payload, "priority", Some(100), Some(0), Some(1000))?;
    let now_ms = job_now(payload)?;
    let available_at_ms = integer(payload, "available_at_ms", Some(now_ms), Some(0), Some(MAX_CLOCK_MS))?;
    let job_payload = match payload.get("payload") {
        None => Value::Object(Default::default()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid payload in worker job"),
    };
    let encoded_payload = dump(&job_payload)?;
    if encoded_payload.len() > MAX_JOB_PAYLOAD_BYTES {
        return fail("storage_payload_too_large", format!("Worker job payload exceeds {MAX_JOB_PAYLOAD_BYTES} bytes"));
    }
    let request_digest = hex::encode(Sha256::digest(dump(&json!({
        "tenantId": tenant_id,
        "taskKind": task_kind,
        "payload": job_payload,
    }))?));

    session.lock_key("worker_job_idempotency", &format!("{user_id}:{idempotency_key}"));
    let existing = session.fetch_one(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE user_id = ? AND idempotency_key = ?"),
        &params![user_id, &idempotency_key],
    )?;
    if let Some(existing) = existing {
        if col_str(&existing, "request_digest")? != request_digest {
            return fail("database_conflict", "Worker job idempotency key was reused for another request");
        }
        return Ok(json!({"created": false, "job": job_document(&existing)?}));
    }
    if job_row(session, &task_id)?.is_some() {
        return fail("database_conflict", "Worker job task_id already exists");
    }
    session.execute(
        "INSERT INTO storage_worker_jobs(\
         task_id, user_id, tenant_id, task_kind, payload_json, \
         idempotency_key, request_digest, status, priority, available_at_ms, \
         created_at_ms, updated_at_ms) \
         VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        &params![
            &task_id,
            user_id,
            &tenant_id,
            &task_kind,
            encoded_payload,
            &idempotency_key,
            &request_digest,
            priority,
            available_at_ms,
            now_ms,
            now_ms,
        ],
    )?;
    session.hint_queue_changed();
    let created = job_row(session, &task_id)?;
    let Some(created) = created else { return fail("database_internal", "Worker job insert was not visible") };
    Ok(json!({"created": true, "job": job_document(&created)?}))
}

const LEASE_ORDER: [&str; 4] = ["lease_deadline_ms", "priority", "created_at_ms", "task_id"];

/// `_current_lease_candidate`: traverse only expired active holders; skip
/// live processes, never jobs. Returns `(row, recheck)`.
fn current_lease_candidate(
    session: &Session<'_>,
    kind: &str,
    epoch: &str,
    now_ms: i64,
    lock: bool,
) -> OpResult<(Option<Row>, Option<i64>)> {
    let mut after: Option<Vec<SqlValue>> = None;
    let mut recheck: Option<i64> = None;
    loop {
        let sql = format!(
            "SELECT task_id, claim_owner, lease_authority_epoch, lease_deadline_ms, \
             heartbeat_at_ms, priority, created_at_ms, fencing_token FROM storage_worker_jobs \
             WHERE status='running' AND task_kind=? AND lease_authority_epoch=? \
             {}ORDER BY lease_deadline_ms, priority, created_at_ms, task_id LIMIT 1",
            if after.is_some() {
                "AND (lease_deadline_ms, priority, created_at_ms, task_id) > (?, ?, ?, ?) "
            } else {
                ""
            }
        );
        let mut parameters = vec![SqlValue::from(kind), SqlValue::from(epoch)];
        if let Some(after) = &after {
            parameters.extend(after.iter().cloned());
        }
        let row = if lock {
            session.fetch_one_for_update_skip_locked(&sql, &parameters)?
        } else {
            session.fetch_one(&sql, &parameters)?
        };
        let Some(row) = row else { return Ok((None, recheck)) };
        if col_int(&row, "lease_deadline_ms")? > now_ms {
            return Ok((Some(row), recheck));
        }
        if !process_claim_protected(&col_str(&row, "claim_owner")?) {
            return Ok((Some(row), recheck));
        }
        let delay = col_int(&row, "lease_deadline_ms")? - col_int(&row, "heartbeat_at_ms")?;
        if delay <= 0 {
            return fail("database_integrity", "Invalid process liveness interval");
        }
        recheck = Some(recheck.map_or(delay, |current| current.min(delay)));
        after = Some(LEASE_ORDER.iter().map(|key| row.get(key).clone()).collect::<Vec<_>>());
    }
}

fn same_epoch(row: &Row, epoch: &str) -> bool {
    row.get("lease_authority_epoch").as_str() == Some(epoch)
}

/// Python tuple ordering of `(int, int, int, str)` sort keys.
type SortKey = (i128, i64, i64, String);

fn ordered_min(candidates: Vec<(Row, SortKey)>) -> Option<Row> {
    let mut best: Option<(Row, SortKey)> = None;
    for (row, key) in candidates {
        if best.as_ref().is_none_or(|(_, best_key)| key < *best_key) {
            best = Some((row, key));
        }
    }
    best.map(|(row, _)| row)
}

/// `_claim_candidate`: old epochs always fence; current holders need
/// process-death evidence.
fn claim_candidate(
    session: &Session<'_>,
    now_ms: i64,
    utc_now_ms: i64,
    epoch: &str,
    kinds: &[String],
) -> OpResult<Option<Row>> {
    let mut expired = Vec::new();
    for kind in kinds {
        for relation in ["<", ">"] {
            let row = session.fetch_one_for_update_skip_locked(
                &format!(
                    "SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs \
                     WHERE status='running' AND task_kind=? \
                     AND lease_authority_epoch {relation} ? \
                     ORDER BY lease_authority_epoch, lease_deadline_ms, priority, \
                     created_at_ms, task_id LIMIT 1"
                ),
                &params![kind, epoch],
            )?;
            if let Some(row) = row {
                expired.push(row);
            }
        }
        let (row, _) = current_lease_candidate(session, kind, epoch, now_ms, true)?;
        if let Some(row) = row {
            if col_int(&row, "lease_deadline_ms")? <= now_ms {
                expired.push(row);
            }
        }
    }
    let mut parameters = vec![SqlValue::Integer(utc_now_ms)];
    parameters.extend(kinds.iter().map(SqlValue::from));
    let queued = session.fetch_one_for_update_skip_locked(
        &format!(
            "SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs \
             WHERE status IN ('queued', 'suspended') AND available_at_ms <= ? \
             AND task_kind IN ({}) \
             ORDER BY priority, available_at_ms, created_at_ms, task_id LIMIT 1",
            placeholders(kinds.len())
        ),
        &parameters,
    )?;
    if expired.is_empty() {
        return Ok(queued);
    }
    let mut keyed = Vec::with_capacity(expired.len());
    for row in expired {
        let deadline = if same_epoch(&row, epoch) { col_int(&row, "lease_deadline_ms")? } else { 0 };
        let key = (
            i128::from(deadline),
            col_int(&row, "priority")?,
            col_int(&row, "created_at_ms")?,
            col_str(&row, "task_id")?,
        );
        keyed.push((row, key));
    }
    let lease = ordered_min(keyed).expect("expired is not empty");
    let Some(queued) = queued else { return Ok(Some(lease)) };
    // One order across both sets, as in the native engine: the candidate
    // claimable for longer wins, each wait read on its own clock. A lease
    // from another lifecycle has waited since before this one began
    // (`math.inf`). Ties use priority, creation, ID.
    let leased_for = if same_epoch(&lease, epoch) {
        i128::from(now_ms) - i128::from(col_int(&lease, "lease_deadline_ms")?)
    } else {
        i128::MAX
    };
    let waited_for = i128::from(utc_now_ms) - i128::from(col_int(&queued, "available_at_ms")?);
    let lease_key =
        (-leased_for, col_int(&lease, "priority")?, col_int(&lease, "created_at_ms")?, col_str(&lease, "task_id")?);
    let queued_key =
        (-waited_for, col_int(&queued, "priority")?, col_int(&queued, "created_at_ms")?, col_str(&queued, "task_id")?);
    Ok(ordered_min(vec![(lease, lease_key), (queued, queued_key)]))
}

fn worker_job_claim_next(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let worker_id = required_text(payload, "worker_id", 256)?;
    job_now(payload)?;
    let lease_ms = integer(payload, "lease_ms", Some(60_000), Some(10_000), Some(WORKER_LEASE_MAX_MS))?;
    let kinds = claim_kinds(payload)?;
    let authority = lease_authority(session)?;
    let row = claim_candidate(session, authority.lease_now_ms, authority.utc_now_ms, &authority.epoch, &kinds)?;
    let Some(row) = row else { return Ok(Value::Null) };
    // Candidate acquisition may block behind another transaction. Read
    // authority again after the row is owned, so the new lease starts here.
    let authority = lease_authority(session)?;
    let now_ms = authority.lease_now_ms;
    let task_id = col_str(&row, "task_id")?;
    let old_fence = col_int(&row, "fencing_token")?;
    let new_fence = old_fence + 1;
    let changed = session.execute(
        "UPDATE storage_worker_jobs SET status='running', claim_owner=?, \
         lease_deadline_ms=?, lease_authority_epoch=?, fencing_token=?, \
         attempt_no=attempt_no+1, heartbeat_at_ms=?, updated_at_ms=? \
         WHERE task_id=? AND fencing_token=?",
        &params![&worker_id, now_ms + lease_ms, &authority.epoch, new_fence, now_ms, now_ms, &task_id, old_fence],
    )?;
    if changed != 1 {
        return fail("database_conflict", "Worker job claim lost its fence");
    }
    let claimed = job_row(session, &task_id)?;
    let Some(claimed) = claimed else { return fail("database_internal", "Claimed worker job disappeared") };
    job_document(&claimed)
}

/// Read-only answer to: when can `worker_job.claim_next` claim these kinds?
fn worker_job_claim_horizon(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let kinds = claim_kinds(payload)?;
    let authority = lease_authority(session)?;
    let (epoch, now_ms, utc_now_ms) = (authority.epoch.as_str(), authority.lease_now_ms, authority.utc_now_ms);
    let mut delays: Vec<i64> = Vec::new();
    for kind in &kinds {
        for relation in ["<", ">"] {
            let row = session.fetch_one(
                &format!(
                    "SELECT task_id FROM storage_worker_jobs WHERE status='running' \
                     AND task_kind=? AND lease_authority_epoch {relation} ? LIMIT 1"
                ),
                &params![kind, epoch],
            )?;
            if row.is_some() {
                return Ok(json!({"claimableInMs": 0}));
            }
        }
        let (lease, recheck) = current_lease_candidate(session, kind, epoch, now_ms, false)?;
        if let Some(recheck) = recheck {
            delays.push(recheck);
        }
        if let Some(lease) = lease {
            delays.push((col_int(&lease, "lease_deadline_ms")? - now_ms).max(0));
        }
    }
    for status in ["queued", "suspended"] {
        let mut parameters = vec![SqlValue::from(status)];
        parameters.extend(kinds.iter().map(SqlValue::from));
        let queued = session.fetch_one(
            &format!(
                "SELECT available_at_ms FROM storage_worker_jobs WHERE status=? \
                 AND task_kind IN ({}) \
                 ORDER BY available_at_ms LIMIT 1",
                placeholders(kinds.len())
            ),
            &parameters,
        )?;
        if let Some(queued) = queued {
            delays.push((col_int(&queued, "available_at_ms")? - utc_now_ms).max(0));
        }
    }
    Ok(json!({"claimableInMs": delays.into_iter().min()}))
}

const DEFER_FIELDS: &[&str] = &[
    "task_id",
    "worker_id",
    "fencing_token",
    "now_ms",
    "available_at_ms",
    "replay_cursor",
    "observed_cancel_sequence",
];

/// Release a proved claim into indexed durable nonterminal waiting. An exact
/// lost-ACK replay performs no write.
fn worker_job_defer(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let fields: BTreeSet<&str> =
        payload.keys().map(String::as_str).filter(|key| *key != "observed_wake_sequence").collect();
    if fields != DEFER_FIELDS.iter().copied().collect::<BTreeSet<&str>>() {
        return fail("database_protocol_error", "Invalid worker defer fields");
    }
    let task_id = required_text(payload, "task_id", 256)?;
    let worker_id = required_text(payload, "worker_id", 256)?;
    let fence = integer(payload, "fencing_token", None, Some(1), None)?;
    let now_ms = job_now(payload)?;
    let available = integer(payload, "available_at_ms", None, Some(now_ms), Some(MAX_CLOCK_MS))?;
    let cursor = integer(payload, "replay_cursor", None, Some(0), None)?;
    if available - now_ms > MAX_CLOCK_SKEW_MS {
        return fail("database_protocol_error", "Worker defer exceeds its scheduling bound");
    }
    session.lock_key("worker_job", &task_id);
    let Some(row) = job_row_for_update(session, &task_id)? else {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    };
    let observed_cancel = integer(payload, "observed_cancel_sequence", None, Some(0), None)?;
    let observed_wake = integer(payload, "observed_wake_sequence", Some(0), Some(0), None)?;
    if observed_wake > col_int(&row, "wake_sequence")? {
        return fail("database_protocol_error", "Worker cannot acknowledge a future wakeup");
    }
    if observed_cancel > col_int(&row, "cancel_sequence")? {
        return fail("database_protocol_error", "Worker cannot acknowledge a future cancellation");
    }
    let mut effective_available = if col_int(&row, "cancel_sequence")? > observed_cancel { now_ms } else { available };
    if col_int(&row, "wake_sequence")? > observed_wake {
        effective_available = now_ms.min(col_int(&row, "available_at_ms")?);
    }
    if col_str(&row, "status")? == "suspended"
        && col_str(&row, "claim_owner")? == worker_id
        && col_int(&row, "fencing_token")? == fence
        && col_int(&row, "available_at_ms")? == effective_available
        && col_int(&row, "replay_cursor")? >= cursor
    {
        return Ok(json!({"ok": true, "replayed": true, "job": job_document(&row)?}));
    }
    let authority = lease_authority(session)?;
    if !live_claim(Some(&row), &worker_id, fence, &authority.epoch, authority.lease_now_ms)? {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    }
    let count = session.execute(
        "UPDATE storage_worker_jobs SET status=?, lease_deadline_ms=0, \
         available_at_ms=?, replay_cursor=?, updated_at_ms=? \
         WHERE task_id=? AND status='running' AND claim_owner=? \
         AND fencing_token=? AND lease_authority_epoch=?",
        &params![
            "suspended",
            effective_available,
            cursor.max(col_int(&row, "replay_cursor")?),
            now_ms,
            &task_id,
            &worker_id,
            fence,
            &authority.epoch,
        ],
    )?;
    if count == 0 {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    }
    session.hint_queue_changed();
    Ok(json!({
        "ok": true,
        "replayed": false,
        "job": existing_document(job_row(session, &task_id)?, "'NoneType' object is not subscriptable")?,
    }))
}

fn worker_job_heartbeat(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let worker_id = required_text(payload, "worker_id", 256)?;
    let fence = integer(payload, "fencing_token", None, Some(1), None)?;
    let now_ms = job_now(payload)?;
    let lease_ms = integer(payload, "lease_ms", Some(60_000), Some(10_000), Some(WORKER_LEASE_MAX_MS))?;
    let replay_cursor = integer(payload, "replay_cursor", Some(0), Some(0), None)?;
    session.lock_key("worker_job", &task_id);
    let row = job_row_for_update(session, &task_id)?;
    let authority = lease_authority(session)?;
    let authority_now = authority.lease_now_ms;
    if !live_claim(row.as_ref(), &worker_id, fence, &authority.epoch, authority_now)? {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    }
    let deadline = authority_now + lease_ms;
    let changed = session.execute(
        "UPDATE storage_worker_jobs SET \
         lease_deadline_ms=CASE WHEN lease_deadline_ms > ? \
         THEN lease_deadline_ms ELSE ? END, \
         heartbeat_at_ms=CASE WHEN heartbeat_at_ms > ? \
         THEN heartbeat_at_ms ELSE ? END, \
         replay_cursor=CASE WHEN replay_cursor > ? \
         THEN replay_cursor ELSE ? END, updated_at_ms=? \
         WHERE task_id=? AND status='running' AND claim_owner=? \
         AND fencing_token=? AND lease_authority_epoch=?",
        &params![
            deadline,
            deadline,
            authority_now,
            authority_now,
            replay_cursor,
            replay_cursor,
            now_ms,
            &task_id,
            &worker_id,
            fence,
            &authority.epoch,
        ],
    )?;
    if changed != 1 {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    }
    Ok(
        json!({"ok": true, "job": existing_document(job_row(session, &task_id)?, "'NoneType' object is not subscriptable")?}),
    )
}

fn worker_job_claim_state(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let worker_id = required_text(payload, "worker_id", 256)?;
    let fence = integer(payload, "fencing_token", None, Some(1), None)?;
    job_now(payload)?;
    let row = job_row(session, &task_id)?;
    let authority = lease_authority(session)?;
    if !live_claim(row.as_ref(), &worker_id, fence, &authority.epoch, authority.lease_now_ms)? {
        return Ok(json!({"ok": false, "error": "stale_fence"}));
    }
    let job = job_document(row.as_ref().expect("live claim has a row"))?;
    Ok(json!({
        "ok": true,
        "cancelSequence": job["cancelSequence"],
        "cancelRequestedAtMs": job["cancelRequestedAtMs"],
        "cancelReason": job["cancelReason"],
        "leaseDeadlineMs": job["leaseDeadlineMs"],
        "replayCursor": job["replayCursor"],
    }))
}

fn worker_job_request_cancel(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let user_id = job_owner(payload)?;
    let now_ms = job_now(payload)?;
    let reason = optional_text(payload, "reason", 1000)?;
    session.lock_key("worker_job", &task_id);
    let row = session.fetch_one_for_update(
        &format!("SELECT {WORKER_JOB_COLUMNS} FROM storage_worker_jobs WHERE task_id=? AND user_id=?"),
        &params![&task_id, user_id],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let status = col_str(&row, "status")?;
    if TERMINAL_STATUSES.contains(&status.as_str()) {
        return Ok(json!({"accepted": false, "alreadyTerminal": true, "job": job_document(&row)?}));
    }
    if col_int(&row, "cancel_requested_at_ms")? > 0 {
        return Ok(json!({"accepted": true, "alreadyRequested": true, "job": job_document(&row)?}));
    }
    let queued = status == "queued";
    // Conversation cancellation still owes a terminal Turn. Keep its queued
    // job claimable for the domain cancel handler; transport is not settlement.
    let domain_cancel = queued && py_eq(&row.require("task_kind")?.to_json()?, &json!("conversation-attempt"));
    let next_status = if queued && !domain_cancel { "cancelled".to_owned() } else { status.clone() };
    let terminal_at_ms = if queued && !domain_cancel { now_ms } else { 0 };
    let cancel_sequence = col_int(&row, "cancel_sequence")? + 1;
    let available_at_ms =
        if status == "suspended" || domain_cancel { now_ms } else { col_int(&row, "available_at_ms")? };
    session.execute(
        "UPDATE storage_worker_jobs SET status=?, cancel_sequence=?, \
         cancel_requested_at_ms=?, cancel_reason=?, updated_at_ms=?, \
         terminal_at_ms=?, available_at_ms=? WHERE task_id=? AND user_id=?",
        &params![
            &next_status,
            cancel_sequence,
            now_ms,
            &reason,
            now_ms,
            terminal_at_ms,
            available_at_ms,
            &task_id,
            user_id,
        ],
    )?;
    session.hint_queue_changed();
    session.hint_cancelled_task(&task_id);
    let updated = job_row(session, &task_id)?;
    Ok(json!({
        "accepted": true,
        "alreadyRequested": false,
        "job": existing_document(updated, "'NoneType' object is not subscriptable")?,
    }))
}

fn worker_job_complete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let task_id = required_text(payload, "task_id", 256)?;
    let worker_id = required_text(payload, "worker_id", 256)?;
    let fence = integer(payload, "fencing_token", None, Some(1), None)?;
    let now_ms = job_now(payload)?;
    let terminal_status = required_text(payload, "terminal_status", 32)?;
    if !TERMINAL_STATUSES.contains(&terminal_status.as_str()) {
        return fail("database_protocol_error", "Invalid terminal_status in storage request");
    }
    let result_ref = optional_text(payload, "result_ref", 1024)?;
    let replay_cursor = integer(payload, "replay_cursor", Some(0), Some(0), None)?;
    let error = match payload.get("error") {
        None => Value::Object(Default::default()),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return fail("database_protocol_error", "Invalid worker job error document"),
    };
    let encoded_error = dump(&error)?;
    if encoded_error.len() > MAX_ERROR_BYTES {
        return fail("storage_payload_too_large", format!("Worker job error exceeds {MAX_ERROR_BYTES} bytes"));
    }
    session.lock_key("worker_job", &task_id);
    let row = job_row_for_update(session, &task_id)?;
    let authority = lease_authority(session)?;
    if !live_claim(row.as_ref(), &worker_id, fence, &authority.epoch, authority.lease_now_ms)? {
        return Ok(json!({"ok": false, "error": "stale_fence_or_cancelled"}));
    }
    let changed = session.execute(
        "UPDATE storage_worker_jobs SET status=?, result_ref=?, error_json=?, \
         replay_cursor=CASE WHEN replay_cursor > ? \
         THEN replay_cursor ELSE ? END, lease_deadline_ms=0, \
         updated_at_ms=?, terminal_at_ms=? \
         WHERE task_id=? AND status='running' AND claim_owner=? \
         AND fencing_token=? AND lease_authority_epoch=? \
         AND (cancel_requested_at_ms=0 OR ?='cancelled')",
        &params![
            &terminal_status,
            &result_ref,
            encoded_error,
            replay_cursor,
            replay_cursor,
            now_ms,
            now_ms,
            &task_id,
            &worker_id,
            fence,
            &authority.epoch,
            &terminal_status,
        ],
    )?;
    if changed != 1 {
        return Ok(json!({"ok": false, "error": "stale_fence_or_cancelled"}));
    }
    Ok(
        json!({"ok": true, "job": existing_document(job_row(session, &task_id)?, "'NoneType' object is not subscriptable")?}),
    )
}
