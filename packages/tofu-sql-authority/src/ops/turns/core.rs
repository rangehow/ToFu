//! `operations_pkg/_turns_core.py`: shared turn row projections, change
//! capture, attempt-event transport and search dirty markers.

use serde_json::{json, Map, Value};

use super::head::projection_from_turn_row;
use super::patch::build_projection_patch;
use crate::attempt_event_metrics;
use crate::codec::{dump, dump_param, load};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub type Obj = Map<String, Value>;

pub use crate::attempt_event_metrics::ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES;
/// `generated_tofudb_migration.MAX_ATTEMPT_EVENT_DOCUMENT_BYTES`.
pub const MAX_ATTEMPT_EVENT_DOCUMENT_BYTES: usize = 33_554_432;
const CONVERSATION_SYNC_EVENT_CONTRACT: &str = "tofu.conversation-sync.event/v1";
const STORAGE_COMMITTED_EVENTS_CONTRACT: &str = "storage.committed-events/v1";
const TURN_SEARCH_PROJECTION_NAME: &str = "turn_search.v1";

/// `lib.conversation_sync.settings_policy.PRIVATE_CONVERSATION_SETTING_KEYS`.
pub const SYNC_PRIVATE_SETTING_KEYS: &[&str] = &[
    "_attemptId",
    "_attended",
    "_conversationExecutionAdmission",
    "_conversationExecutionIdentity",
    "_conversationWorkerExecution",
    "_conversationWorkspace",
    "_mcpWireDeclarations",
    "_principal",
    "_principalContext",
    "_projectDispatchAdmission",
    "_turnId",
    "_turnOwnerUserId",
    "_turnPrincipalKind",
    "_turnPrincipalScopes",
    "_turnPrincipalSubjectId",
    "_turnPrincipalTenantId",
    "_turnReviewSource",
    "_unattended",
    "_userId",
    "canonicalProjectPath",
    "canonicalProjectPaths",
    "workspaceRuntimeRoots",
];

// ── small Python-semantics helpers shared by the turn slices ─────────────

/// `int(time.time() * 1000)`.
pub fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// `str(value or '')`.
pub fn text(value: &Value) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        String::new()
    }
}

/// `str(value or default)`.
pub fn text_or(value: &Value, default: &str) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        default.to_owned()
    }
}

/// Python `int(value)` of a decoded JSON value.
pub fn py_int(value: &Value) -> OpResult<i64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => Ok(integer),
            None => match number.as_f64() {
                Some(float) if float.is_finite() && float.abs() < 9.2e18 => Ok(float.trunc() as i64),
                _ => internal("int() overflow"),
            },
        },
        Value::String(text) => match text.trim().replace('_', "").parse::<i64>() {
            Ok(integer) => Ok(integer),
            Err(_) => internal("invalid literal for int()"),
        },
        _ => internal("int() argument must be a string or a number"),
    }
}

/// `int(value or 0)`.
pub fn py_int_or_zero(value: &Value) -> OpResult<i64> {
    if truthy(value) {
        py_int(value)
    } else {
        Ok(0)
    }
}

/// `int(payload.get("now") or time.time() * 1000)`.
pub fn payload_now(payload: &Payload) -> OpResult<i64> {
    let now = get(payload, "now");
    if truthy(now) {
        py_int(now)
    } else {
        Ok(now_ms())
    }
}

/// `x in frozenset(...)` for a JSON value: containers are unhashable.
pub fn value_in(value: &Value, members: &[&str]) -> OpResult<bool> {
    match value {
        Value::Array(_) | Value::Object(_) => internal("unhashable type"),
        Value::String(text) => Ok(members.contains(&text.as_str())),
        _ => Ok(false),
    }
}

/// `dict(value) if isinstance(value, Mapping) else {}`.
pub fn as_obj(value: &Value) -> Obj {
    match value {
        Value::Object(map) => map.clone(),
        _ => Obj::new(),
    }
}

/// A row column as the JSON value Python would place in a result dict.
pub fn col(row: &Row, column: &str) -> OpResult<Value> {
    row.get(column).to_json()
}

/// `_turn_identity(payload)`.
pub fn turn_identity(payload: &Payload) -> OpResult<(String, i64)> {
    let conv_id = required_text(payload, "conversation_id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    Ok((conv_id, user_id))
}

/// `_turn_exists`.
pub fn turn_exists(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id) = turn_identity(payload)?;
    Ok(Value::Bool(
        session
            .fetch_one(
                "SELECT 1 AS present FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? LIMIT 1",
                &params![conv_id, user_id],
            )?
            .is_some(),
    ))
}

/// `_stored_object(value, field_name)`.
pub fn stored_object(value: &SqlValue, field_name: &str) -> OpResult<Obj> {
    match load(value)? {
        Value::Object(map) => Ok(map),
        _ => fail("database_integrity", format!("Stored {field_name} must be an object")),
    }
}

// ── turn settlement normalization (`lib.turn_verdict`) ───────────────────

const TURN_OUTCOMES: &[&str] = &["completed", "interrupted", "truncated", "failed"];
const PROVIDER_STREAM_STATES: &[&str] = &[
    "provider_finished",
    "client_aborted",
    "premature_close",
    "malformed_stream",
    "semantic_progress_timeout",
    "no_actionable_output",
    "empty_response",
    "tool_payload_missing",
    "gateway_limit_response",
    "unknown",
];
const FAILED_STREAM_STATES: &[&str] = &[
    "premature_close",
    "malformed_stream",
    "semantic_progress_timeout",
    "no_actionable_output",
    "empty_response",
    "tool_payload_missing",
    "gateway_limit_response",
];
const FAILED_SETTLEMENT_CAUSES: &[&str] = &["provider_stream_error", "generation_error", "completion_evidence_missing"];
const FAILED_SETTLEMENT_EVIDENCE: &[&str] =
    &["provider_stream_failure", "generation_error", "missing_completion_evidence"];

/// `parse_provider_stream_state`: `None` for absent/empty values.
fn parse_stream_state(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) if text.is_empty() => None,
        other => {
            let text = python_str(other);
            Some(if PROVIDER_STREAM_STATES.contains(&text.as_str()) { text } else { "unknown".into() })
        }
    }
}

/// `lib.error_envelope.make_envelope(kind, detail=, context=, source=, raw=)`
/// for the kinds settlement normalization emits.
fn settlement_error_envelope(kind: &str, detail: &str, raw: &str) -> Value {
    let (severity, retryable, message, hint) = match kind {
        "premature_close" => (
            "warning",
            true,
            "⚠️ 网关/代理过早关闭流\nGateway closed the stream prematurely",
            "解决办法 / How to fix:\n• 重试已用完，回复可能不完整。可点击 Retry 重新生成。\n\n• Retries exhausted — the response may be incomplete. Click Retry to regenerate.",
        ),
        "abnormal_stop" => (
            "warning",
            true,
            "⚠️ API 流异常终止（缺失 finish 标记）\nStream ended without finish marker",
            "解决办法 / How to fix:\n• 回复可能不完整。可点击 Retry 重新生成。\n\n• The reply may be truncated. Click Retry to regenerate.",
        ),
        "internal" => (
            "error",
            false,
            "⚠️ 内部错误\nInternal error",
            "解决办法 / How to fix:\n• 请查看服务器日志（logs/error.log）了解详情。\n\n• Check the server logs (logs/error.log) for details.",
        ),
        _ => (
            "error",
            false,
            "⚠️ 模型调用失败\nLLM call failed",
            "解决办法 / How to fix:\n• 展开下方错误详情查看原始原因。\n• 若反复出现，请查看服务器日志（logs/error.log）定位根因；若确认是 Key/配额问题，再前往 「设置 → Keys / Providers」 处理。\n\n• Expand the error detail below for the underlying cause.\n• If it recurs, check the server logs (logs/error.log) for the root cause; only if it proves to be a key/quota problem, go to \"Settings → Keys / Providers\".",
        ),
    };
    let bounded = |text: &str| text.chars().take(300).collect::<String>();
    json!({
        "kind": kind,
        "severity": severity,
        "retryable": retryable,
        "message": message,
        "hint": hint,
        "detail": bounded(detail),
        "model": "",
        "context": "settlement-normalization",
        "source": "turn-verdict",
        "raw": bounded(raw),
        "titleKey": format!("err.k.{kind}.title"),
        "hintKey": format!("err.k.{kind}.hint"),
    })
}

fn set_default(map: &mut Obj, key: &str, value: Value) {
    if !map.contains_key(key) {
        map.insert(key.to_owned(), value);
    }
}

/// `normalize_turn_settlement(value, status=)`.
pub fn normalize_turn_settlement(value: &Obj, status: &str) -> Obj {
    let mut settlement = value.clone();
    let default_outcome = if status == "superseded" {
        "interrupted".to_owned()
    } else if status.is_empty() {
        "failed".to_owned()
    } else {
        status.to_owned()
    };
    let mut outcome = text_or(get(&settlement, "outcome"), &default_outcome);
    if !TURN_OUTCOMES.contains(&outcome.as_str()) {
        outcome = "failed".into();
    }
    let mut cause = text_or(get(&settlement, "cause"), "ingested");
    let finish_reason = text(get(&settlement, "providerFinishReason"));
    let mut stream_state = parse_stream_state(get(&settlement, "streamState"));
    let raw_evidence = text(get(&settlement, "evidence"));
    let stream_failed = stream_state.as_deref().is_some_and(|state| FAILED_STREAM_STATES.contains(&state));
    let historical_stream_failure = matches!(finish_reason.as_str(), "premature_close" | "abnormal_stop")
        || stream_failed
        || cause == "provider_stream_error"
        || raw_evidence == "provider_stream_failure";
    let historical_completion_missing = stream_state.as_deref() == Some("unknown")
        || cause == "completion_evidence_missing"
        || raw_evidence == "missing_completion_evidence";
    let historical_generation_failure = truthy(get(&settlement, "error"))
        || matches!(finish_reason.as_str(), "error" | "timeout")
        || cause == "generation_error"
        || raw_evidence == "generation_error";
    let contradictory_completed = outcome == "completed"
        && (historical_stream_failure
            || historical_completion_missing
            || historical_generation_failure
            || FAILED_SETTLEMENT_CAUSES.contains(&cause.as_str())
            || FAILED_SETTLEMENT_EVIDENCE.contains(&raw_evidence.as_str()));
    if contradictory_completed {
        outcome = "failed".into();
        let (evidence, error_kind) = if historical_stream_failure {
            cause = "provider_stream_error".into();
            if stream_state.is_none() {
                stream_state = Some("premature_close".into());
            }
            settlement.insert("streamState".into(), Value::String(stream_state.clone().unwrap_or_default()));
            let kind = if matches!(finish_reason.as_str(), "premature_close" | "abnormal_stop") {
                finish_reason.clone()
            } else {
                "premature_close".into()
            };
            ("provider_stream_failure", kind)
        } else if historical_completion_missing {
            cause = "completion_evidence_missing".into();
            ("missing_completion_evidence", "internal".to_owned())
        } else {
            cause = "generation_error".into();
            ("generation_error", "generic".to_owned())
        };
        settlement.insert("evidence".into(), Value::String(evidence.into()));
        if !truthy(get(&settlement, "error")) {
            let shown = if !finish_reason.is_empty() {
                finish_reason.clone()
            } else if !raw_evidence.is_empty() {
                raw_evidence.clone()
            } else {
                cause.clone()
            };
            let detail =
                format!("A historical turn marked completed carried explicit terminal failure evidence ({shown}).");
            let raw = format!(
                "outcome=completed cause={} finish={} stream_state={} evidence={}",
                text(get(&settlement, "cause")),
                finish_reason,
                stream_state.clone().unwrap_or_default(),
                raw_evidence
            );
            settlement.insert("error".into(), settlement_error_envelope(&error_kind, &detail, &raw));
        }
    }
    settlement.insert("outcome".into(), Value::String(outcome.clone()));
    settlement.insert("cause".into(), Value::String(cause.clone()));
    set_default(&mut settlement, "providerFinishReason", Value::Null);
    set_default(&mut settlement, "error", Value::Null);
    set_default(&mut settlement, "resumeOptions", Value::Array(vec![]));
    set_default(&mut settlement, "streamState", Value::Null);
    if !truthy(get(&settlement, "evidence")) {
        let inferred = if cause == "server_restart" || cause == "conversation_deleted" {
            "system_recovery"
        } else if outcome == "truncated" {
            "provider_limit"
        } else if outcome == "failed" {
            "generation_error"
        } else if cause == "provider_finished" && truthy(get(&settlement, "providerFinishReason")) {
            "legacy_finish"
        } else {
            "external_authority"
        };
        settlement.insert("evidence".into(), Value::String(inferred.into()));
    }
    settlement
}

const SETTLED_STATUSES: &[&str] = &["completed", "interrupted", "truncated", "failed", "superseded"];

pub(crate) fn turn_status_and_settlement(row: &Row) -> OpResult<(String, Obj)> {
    let stored_status = row.require("status")?.text_or_empty();
    let raw_settlement = stored_object(row.require("settlement_json")?, "turn settlement")?;
    let settled = SETTLED_STATUSES.contains(&stored_status.as_str());
    let public_settlement =
        if settled { normalize_turn_settlement(&raw_settlement, &stored_status) } else { raw_settlement };
    let mut status = stored_status.clone();
    let normalized_outcome = text(get(&public_settlement, "outcome"));
    if settled && TURN_OUTCOMES.contains(&normalized_outcome.as_str()) {
        status = normalized_outcome;
    }
    Ok((status, public_settlement))
}

/// `_turn_public_metadata(row)`.
pub fn turn_public_metadata(row: &Row) -> OpResult<Obj> {
    let (status, public_settlement) = turn_status_and_settlement(row)?;
    let turn_id = row.require("turn_id")?.text_or_empty();
    let presentation = row.text("presentation_id");
    let mut public = Obj::new();
    public.insert("turnId".into(), Value::String(turn_id.clone()));
    public.insert("presentationId".into(), Value::String(if presentation.is_empty() { turn_id } else { presentation }));
    public.insert("conversationId".into(), Value::String(row.require("conversation_id")?.text_or_empty()));
    let lane = row.require("lane_id")?.text_or_empty();
    public.insert("laneId".into(), Value::String(if lane.is_empty() { "main".into() } else { lane }));
    public.insert("parentTurnId".into(), col(row, "parent_turn_id")?);
    public.insert("ordinal".into(), Value::from(row.int("ordinal")?));
    public.insert("actor".into(), Value::String(row.require("actor")?.text_or_empty()));
    let kind = row.require("kind")?.text_or_empty();
    public.insert("kind".into(), Value::String(if kind.is_empty() { "reply".into() } else { kind }));
    public.insert("runId".into(), Value::String(row.require("run_id")?.text_or_empty()));
    public.insert("status".into(), Value::String(status));
    public.insert("currentAttemptId".into(), col(row, "current_attempt_id")?);
    let revision = row.require("projection_revision")?.as_int().unwrap_or(0);
    public.insert("projectionRevision".into(), Value::from(revision));
    if !row.get("model_context_display_offset").is_null() {
        let offset = row.opt_int("model_context_display_offset").unwrap_or(0);
        public.insert("modelContextRevision".into(), Value::from(revision - offset));
    }
    public.insert("settlement".into(), Value::Object(public_settlement));
    public.insert("createdAt".into(), Value::from(row.int("created_at")?));
    public.insert("updatedAt".into(), Value::from(row.int("updated_at")?));
    Ok(public)
}

/// `_turn_public(session, row)`.
pub fn turn_public(session: &Session<'_>, row: &Row) -> OpResult<Value> {
    let mut public = turn_public_metadata(row)?;
    public.insert("projection".into(), projection_from_turn_row(session, row)?);
    Ok(Value::Object(public))
}

/// `_attempt_public(row)`.
pub fn attempt_public(row: &Row) -> OpResult<Value> {
    let resume_anchor = stored_object(row.require("resume_anchor_json")?, "attempt resume anchor")?;
    let mut public = Obj::new();
    public.insert("attemptId".into(), Value::String(row.require("attempt_id")?.text_or_empty()));
    public.insert("conversationId".into(), Value::String(row.require("conversation_id")?.text_or_empty()));
    public.insert("turnId".into(), Value::String(row.require("turn_id")?.text_or_empty()));
    public.insert("commandId".into(), Value::String(row.require("command_id")?.text_or_empty()));
    public.insert("taskId".into(), Value::String(row.require("task_id")?.text_or_empty()));
    public.insert("operation".into(), Value::String(row.require("operation")?.text_or_empty()));
    public.insert("status".into(), Value::String(row.require("status")?.text_or_empty()));
    public.insert(
        "baseProjectionRevision".into(),
        Value::from(row.require("base_projection_revision")?.as_int().unwrap_or(0)),
    );
    public.insert("resumeAnchor".into(), Value::Object(resume_anchor));
    public.insert("createdAt".into(), Value::from(row.int("created_at")?));
    public.insert("startedAt".into(), col(row, "started_at")?);
    public.insert("settledAt".into(), col(row, "settled_at")?);
    let queue_id = row.text("queue_id");
    let queue_state = row.text("queue_state");
    if !queue_id.is_empty() && queue_state == "pending" && row.get("status").as_str() == Some("pending") {
        public.insert("queueBinding".into(), json!({"queueId": queue_id, "state": "pending"}));
    }
    Ok(Value::Object(public))
}

/// `_projection_change(...)`.
pub fn projection_change(
    turn_id: &str,
    before: &Value,
    after: &Value,
    base_revision: i64,
    target_revision: i64,
    updated_at: i64,
) -> Value {
    json!({
        "turnId": turn_id,
        "baseProjectionRevision": base_revision,
        "targetProjectionRevision": target_revision,
        "updatedAt": updated_at,
        "projectionPatch": build_projection_patch(before, after, base_revision, target_revision),
    })
}

// ── turn search dirty markers ────────────────────────────────────────────

/// `_mark_turn_search_projection_dirty`.
pub fn mark_search_dirty(session: &Session<'_>, entity_kind: &str, user_id: i64, entity_key: &str) -> OpResult<String> {
    if !matches!(entity_kind, "turn" | "conversation" | "rebuild") {
        return fail("database_internal", "Invalid turn-search projection entity");
    }
    if entity_key.is_empty() || entity_key.chars().count() > 256 {
        return fail("database_internal", "Invalid turn-search projection key");
    }
    let token = uuid::Uuid::new_v4().simple().to_string();
    session.execute(
        "INSERT INTO storage_projection_outbox(projection_name,entity_kind,user_id,entity_key,version_token,enqueued_at_ms) VALUES (?,?,?,?,?,?) ON CONFLICT(projection_name,entity_kind,user_id,entity_key) DO UPDATE SET version_token=excluded.version_token,enqueued_at_ms=excluded.enqueued_at_ms",
        &params![TURN_SEARCH_PROJECTION_NAME, entity_kind, user_id, entity_key, token.clone(), now_ms()],
    )?;
    Ok(token)
}

/// `_upsert_turn_search_row(session, row, status=)`.
pub fn upsert_turn_search_row(session: &Session<'_>, row: &Row, status: Option<&str>) -> OpResult<bool> {
    let lane = row.text("lane_id");
    let lane = if lane.is_empty() { "main".to_owned() } else { lane };
    let turn_id = row.text("turn_id");
    if lane != "main" {
        mark_search_dirty(session, "turn", row.int("user_id")?, &turn_id)?;
        return Ok(false);
    }
    let effective = match status {
        Some(status) => status.to_owned(),
        None => row.text("status"),
    };
    if effective == "pending" || effective == "running" {
        return Ok(false);
    }
    mark_search_dirty(session, "turn", row.int("user_id")?, &turn_id)?;
    Ok(true)
}

/// `_delete_turn_search_rows`.
pub fn delete_turn_search_rows<'r>(session: &Session<'_>, rows: impl Iterator<Item = &'r Row>) -> OpResult<()> {
    let mut conversations = std::collections::BTreeSet::new();
    for row in rows {
        conversations.insert((row.int("user_id")?, row.text("conversation_id")));
    }
    for (user_id, conversation_id) in conversations {
        mark_search_dirty(session, "conversation", user_id, &conversation_id)?;
    }
    Ok(())
}

/// `_turn_search_backfill`.
pub fn turn_search_backfill(session: &Session<'_>, _payload: &Payload) -> OpResult<Value> {
    mark_search_dirty(session, "rebuild", 0, "all")?;
    Ok(json!({
        "scanned": 0,
        "indexed": 0,
        "failed": 0,
        "projectionBytes": 0,
        "nextCursor": "",
        "remaining": false,
        "scheduled": true,
    }))
}

// ── attempt events (`_turns_events.py` transport primitives) ─────────────

/// `_insert_attempt_event(...)`.
#[allow(clippy::too_many_arguments)]
pub fn insert_attempt_event(
    session: &Session<'_>,
    attempt_id: &str,
    sequence: i64,
    conversation_id: &str,
    turn_id: &str,
    projection_revision: i64,
    event_type: &str,
    envelope: &Value,
    created_at: i64,
    allow_oversize: bool,
) -> OpResult<i64> {
    let encoded = dump(envelope)?;
    let payload_bytes = encoded.len();
    if payload_bytes > MAX_ATTEMPT_EVENT_DOCUMENT_BYTES {
        attempt_event_metrics::observe_payload(event_type, payload_bytes as i64, false);
        return fail("storage_payload_too_large", "Attempt event exceeds the attempt-event document bound");
    }
    if !allow_oversize && event_type != "terminal_settlement" && payload_bytes > ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES {
        attempt_event_metrics::observe_payload(event_type, payload_bytes as i64, false);
        return fail("storage_payload_too_large", "Non-terminal attempt event exceeds the durable transport limit");
    }
    session.execute(
        "INSERT INTO storage_attempt_events (attempt_id,sequence,conversation_id,turn_id,projection_revision,type,payload_json,payload_bytes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        &params![
            attempt_id,
            sequence,
            conversation_id,
            turn_id,
            projection_revision,
            event_type,
            encoded,
            payload_bytes as i64,
            created_at
        ],
    )?;
    attempt_event_metrics::observe_payload(event_type, payload_bytes as i64, true);
    Ok(payload_bytes as i64)
}

/// `_turn_event_append(session, {...})`: returns `(sequence, envelope, bytes)`.
pub fn turn_event_append(
    session: &Session<'_>,
    attempt_id: &str,
    conversation_id: &str,
    turn_id: &str,
    projection_revision: i64,
    event_type: &str,
    event: Obj,
) -> OpResult<Value> {
    let mut payload = Payload::new();
    payload.insert("attempt_id".into(), Value::String(attempt_id.into()));
    payload.insert("conversation_id".into(), Value::String(conversation_id.into()));
    payload.insert("turn_id".into(), Value::String(turn_id.into()));
    payload.insert("type".into(), Value::String(event_type.into()));
    payload.insert("projection_revision".into(), Value::from(projection_revision));
    let attempt_id = required_text(&payload, "attempt_id", 128)?;
    let conv_id = required_text(&payload, "conversation_id", 256)?;
    let turn_id = required_text(&payload, "turn_id", 128)?;
    let event_type = required_text(&payload, "type", 128)?;
    let projection_revision = integer(&payload, "projection_revision", None, Some(0), None)?;
    let current = session.fetch_one(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM storage_attempt_events WHERE attempt_id=?",
        &params![attempt_id.clone()],
    )?;
    let sequence = current.map(|row| row.opt_int("sequence").unwrap_or(0)).unwrap_or(0) + 1;
    let envelope = json!({
        "conversationId": conv_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": sequence,
        "projectionRevision": projection_revision,
        "type": event_type,
        "payload": Value::Object(event),
    });
    let payload_bytes = insert_attempt_event(
        session,
        &attempt_id,
        sequence,
        &conv_id,
        &turn_id,
        projection_revision,
        &event_type,
        &envelope,
        now_ms(),
        false,
    )?;
    Ok(json!({"sequence": sequence, "event": envelope, "payloadBytes": payload_bytes}))
}

// ── conversation sync log ────────────────────────────────────────────────

/// `_conversation_sync_head`.
pub fn conversation_sync_head(session: &Session<'_>, conversation_id: &str, user_id: i64) -> OpResult<i64> {
    let row = session.fetch_one(
        "SELECT sync_sequence FROM storage_conversation_sync_heads WHERE conversation_id=? AND user_id=?",
        &params![conversation_id, user_id],
    )?;
    Ok(row.map(|row| row.opt_int("sync_sequence").unwrap_or(0)).unwrap_or(0))
}

/// `_conversation_change_envelope`.
pub fn change_envelope(
    conversation_id: &str,
    sync_sequence: i64,
    change_type: &str,
    occurred_at: i64,
    payload: Obj,
    turn_id: &str,
    attempt_id: &str,
) -> Obj {
    let mut event = Obj::new();
    event.insert("contract".into(), Value::String(CONVERSATION_SYNC_EVENT_CONTRACT.into()));
    event.insert("type".into(), Value::String(change_type.into()));
    event.insert("conversationId".into(), Value::String(conversation_id.into()));
    event.insert("syncSeq".into(), Value::from(sync_sequence));
    event.insert("occurredAt".into(), Value::from(occurred_at));
    event.insert("payload".into(), Value::Object(payload));
    if !turn_id.is_empty() {
        event.insert("turnId".into(), Value::String(turn_id.into()));
    }
    if !attempt_id.is_empty() {
        event.insert("attemptId".into(), Value::String(attempt_id.into()));
    }
    event
}

/// `_append_conversation_change(...)`.
#[allow(clippy::too_many_arguments)]
pub fn append_conversation_change(
    session: &Session<'_>,
    conversation_id: &str,
    user_id: i64,
    change_type: &str,
    payload: Obj,
    turn_id: &str,
    attempt_id: &str,
    attempt_sequence: Option<&Value>,
) -> OpResult<Obj> {
    let attempt_sequence = match attempt_sequence {
        None => None,
        Some(value) => match value.as_i64().filter(|_| value.is_number()) {
            Some(sequence)
                if sequence >= 1 && change_type == "attempt.event" && !turn_id.is_empty() && !attempt_id.is_empty() =>
            {
                Some(sequence)
            }
            _ => return fail("database_protocol_error", "Invalid attempt-event change reference"),
        },
    };
    let now = now_ms();
    let sequence = conversation_sync_head(session, conversation_id, user_id)? + 1;
    let event = change_envelope(conversation_id, sequence, change_type, now, payload, turn_id, attempt_id);
    if sequence == 1 {
        session.execute(
            "INSERT INTO storage_conversation_sync_heads (conversation_id,user_id,sync_sequence,updated_at) VALUES (?,?,?,?)",
            &params![conversation_id, user_id, sequence, now],
        )?;
    } else {
        let updated = session.execute(
            "UPDATE storage_conversation_sync_heads SET sync_sequence=?,updated_at=? WHERE conversation_id=? AND user_id=? AND sync_sequence=?",
            &params![sequence, now, conversation_id, user_id, sequence - 1],
        )?;
        if updated != 1 {
            return fail("database_conflict", "Conversation sync sequence changed concurrently");
        }
    }
    let stored = if attempt_sequence.is_some() {
        dump_param(&Value::Object(Obj::new()))?
    } else {
        dump_param(&Value::Object(event.clone()))?
    };
    session.execute(
        "INSERT INTO storage_conversation_changes (conversation_id,user_id,sync_sequence,change_type,turn_id,attempt_id,attempt_sequence,event_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        &params![conversation_id, user_id, sequence, change_type, turn_id, attempt_id, attempt_sequence, stored, now],
    )?;
    Ok(event)
}

/// `_conversation_change_from_row`.
pub fn conversation_change_from_row(row: &Row) -> OpResult<Value> {
    let attempt_sequence = row.require("attempt_sequence")?;
    if attempt_sequence.is_null() {
        let event = load(row.require("event_json")?)?;
        if !event.is_object() {
            return fail("database_integrity", "Stored conversation change is invalid");
        }
        return Ok(event);
    }
    let sequence = match attempt_sequence {
        SqlValue::Integer(value) if *value >= 1 && row.text("change_type") == "attempt.event" => *value,
        _ => return fail("database_integrity", "Stored attempt-event change reference is invalid"),
    };
    let attempt_event = load(row.require("attempt_event_json")?)?;
    let valid = match &attempt_event {
        Value::Object(event) => {
            event.get("payload").is_some_and(Value::is_object)
                && matches!(event.get("seq"), Some(Value::Number(number)) if number.as_i64() == Some(sequence))
                && text(get(event, "conversationId")) == row.text("conversation_id")
                && text(get(event, "turnId")) == row.text("turn_id")
                && text(get(event, "attemptId")) == row.text("attempt_id")
        }
        _ => false,
    };
    if !valid {
        return fail("database_integrity", "Attempt-event change reference is unresolved");
    }
    let mut payload = Obj::new();
    payload.insert("event".into(), attempt_event);
    Ok(Value::Object(change_envelope(
        &row.text("conversation_id"),
        row.int("sync_sequence")?,
        "attempt.event",
        row.int("created_at")?,
        payload,
        &row.text("turn_id"),
        &row.text("attempt_id"),
    )))
}

/// `_conversation_owner_for_turn`.
fn conversation_owner_for_turn(session: &Session<'_>, conversation_id: &str, turn_id: &str) -> OpResult<i64> {
    let row = session.fetch_one(
        "SELECT user_id FROM storage_conversation_turns WHERE conversation_id=? AND turn_id=?",
        &params![conversation_id, turn_id],
    )?;
    match row {
        Some(row) => row.int("user_id"),
        None => fail("database_protocol_error", "Conversation change references an unknown turn"),
    }
}

fn committed_event_notice(event: Obj, user_id: i64) -> Value {
    json!({"contract": "storage.conversation-commit/v1", "userId": user_id, "event": Value::Object(event)})
}

fn mappings(value: &Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items.iter().filter(|item| item.is_object()).cloned().collect(),
        _ => vec![],
    }
}

fn str_list(value: &Value) -> Vec<Value> {
    match value {
        Value::Array(items) if truthy(value) => {
            items.iter().filter(|item| truthy(item)).map(|item| Value::String(python_str(item))).collect()
        }
        _ => vec![],
    }
}

fn revision_of(result: &Obj) -> OpResult<Value> {
    Ok(Value::from(py_int_or_zero(get(result, "conversationRevision"))?))
}

/// `_turn_change_capture`: the `after` hook of every replay-visible turn command.
pub fn turn_change_capture(
    session: &Session<'_>,
    operation: &str,
    payload: &Payload,
    result: Value,
) -> OpResult<Value> {
    let mut attempt_events: Vec<Value> = Vec::new();
    let mut turn_changes: Vec<Value> = Vec::new();
    let clean_result: Value;
    if operation == "turn.event.record_batch" {
        let Value::Array(items) = &result else {
            return fail("database_protocol_error", "Turn event batch returned an invalid result");
        };
        if !items.iter().all(Value::is_object) {
            return fail("database_protocol_error", "Turn event batch returned an invalid result");
        }
        let mut cleaned = Vec::new();
        for item in items {
            let mut item = item.as_object().cloned().unwrap_or_default();
            let private = item.shift_remove("_conversationSyncAttemptEvents").unwrap_or(Value::Array(vec![]));
            attempt_events.extend(mappings(&private));
            cleaned.push(Value::Object(item));
        }
        clean_result = Value::Array(cleaned);
    } else if let Value::Object(map) = &result {
        let mut map = map.clone();
        let private = map.shift_remove("_conversationSyncAttemptEvents").unwrap_or(Value::Array(vec![]));
        if let Some(change @ Value::Object(_)) = map.shift_remove("_conversationSyncTurnPatch") {
            turn_changes.push(change);
        }
        let changes = map.shift_remove("_conversationSyncTurnPatches").unwrap_or(Value::Array(vec![]));
        turn_changes.extend(mappings(&changes));
        attempt_events.extend(mappings(&private));
        if truthy(get(&map, "idempotentReplay")) || map.get("applied") == Some(&Value::Bool(false)) {
            return Ok(Value::Object(map));
        }
        clean_result = Value::Object(map);
    } else {
        clean_result = result;
    }
    let empty = Obj::new();
    let clean = clean_result.as_object().unwrap_or(&empty);
    let mut committed: Vec<Value> = Vec::new();
    match operation {
        "turn.create_pair" | "turn.append_settled" | "turn.queue.activate" => {
            if !clean_result.is_object() {
                return fail("database_protocol_error", "Turn command returned an invalid result");
            }
            let turns: Vec<Value> = [get(clean, "submittedTurn"), get(clean, "turn")]
                .into_iter()
                .filter(|item| item.is_object())
                .cloned()
                .collect();
            let attempts: Vec<Value> =
                [get(clean, "attempt")].into_iter().filter(|item| item.is_object()).cloned().collect();
            let Some(last) = turns.last() else {
                return fail("database_protocol_error", "Turn command produced no turn");
            };
            let conversation_id = text(&last["conversationId"]);
            let turn_id = text(&last["turnId"]);
            let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
            let mut change = Obj::new();
            change.insert("turns".into(), Value::Array(turns.clone()));
            change.insert("attempts".into(), Value::Array(attempts.clone()));
            change.insert("conversationRevision".into(), revision_of(clean)?);
            if let Some(item @ Value::Object(_)) = clean.get("queueItem") {
                change.insert("queueItemUpserts".into(), Value::Array(vec![item.clone()]));
            }
            if operation == "turn.queue.activate" && truthy(get(clean, "queueId")) {
                change.insert("removedQueueIds".into(), json!([python_str(get(clean, "queueId"))]));
            }
            let attempt_id = attempts.first().map(|attempt| text(&attempt["attemptId"])).unwrap_or_default();
            let event = append_conversation_change(
                session,
                &conversation_id,
                user_id,
                "turn.upsert",
                change,
                &turn_id,
                &attempt_id,
                None,
            )?;
            committed.push(committed_event_notice(event, user_id));
        }
        "turn.steer.commit" => {
            let conversation_id = python_str(clean.get("conversationId").ok_or_else(key_error)?);
            let turn_id = python_str(clean.get("turnId").ok_or_else(key_error)?);
            let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
            let revision = py_int(clean.get("conversationRevision").ok_or_else(key_error)?)?;
            let change = json!({"requiresSnapshot": true, "conversationRevision": revision});
            let event = append_conversation_change(
                session,
                &conversation_id,
                user_id,
                "conversation.activity",
                as_obj(&change),
                &turn_id,
                "",
                None,
            )?;
            committed.push(committed_event_notice(event, user_id));
        }
        "turn.compact" => {
            let Some(Value::Object(turn)) = clean.get("turn") else {
                return fail("database_protocol_error", "Turn compaction produced no summary turn");
            };
            let conversation_id = text(get(turn, "conversationId"));
            let turn_id = text(get(turn, "turnId"));
            let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
            let change = json!({"requiresSnapshot": true, "conversationRevision": revision_of(clean)?});
            let event = append_conversation_change(
                session,
                &conversation_id,
                user_id,
                "conversation.activity",
                as_obj(&change),
                &turn_id,
                "",
                None,
            )?;
            committed.push(committed_event_notice(event, user_id));
        }
        "turn.projection.update" | "turn.branch.create" | "turn.branch.delete" => {
            let Some(Value::Object(turn)) = clean.get("turn") else {
                return fail("database_protocol_error", "Turn mutation produced no turn");
            };
            let conversation_id = text(get(turn, "conversationId"));
            let turn_id = text(get(turn, "turnId"));
            let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
            if turn_changes.is_empty() {
                return fail("database_protocol_error", "Turn projection mutation produced no compact replay patch");
            }
            let mut change = Obj::new();
            change.insert("turnPatches".into(), Value::Array(turn_changes.clone()));
            change.insert("conversationRevision".into(), revision_of(clean)?);
            if let Some(Value::Array(ids)) = clean.get("deletedTurnIds") {
                if !ids.is_empty() {
                    change.insert(
                        "deletedTurnIds".into(),
                        Value::Array(ids.iter().map(|id| Value::String(python_str(id))).collect()),
                    );
                }
            }
            if let Some(Value::Array(ids)) = clean.get("removedQueueIds") {
                if !ids.is_empty() {
                    change.insert(
                        "removedQueueIds".into(),
                        Value::Array(ids.iter().map(|id| Value::String(python_str(id))).collect()),
                    );
                }
            }
            let event = append_conversation_change(
                session,
                &conversation_id,
                user_id,
                "turn.patch",
                change,
                &turn_id,
                "",
                None,
            )?;
            committed.push(committed_event_notice(event, user_id));
        }
        "turn.attempt.create" => {
            let turn = match clean.get("turn") {
                Some(Value::Object(turn))
                    if attempt_events.len() == 1
                        && !(payload.get("input_update").is_some_and(|value| !value.is_null())
                            && turn_changes.is_empty()) =>
                {
                    turn
                }
                _ => {
                    return fail("database_protocol_error", "Attempt creation produced incomplete compact replay state")
                }
            };
            let conversation_id = text(get(turn, "conversationId"));
            let turn_id = text(get(turn, "turnId"));
            let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
            if !turn_changes.is_empty() {
                let mut change = Obj::new();
                change.insert("turnPatches".into(), Value::Array(turn_changes.clone()));
                change.insert("conversationRevision".into(), revision_of(clean)?);
                let first_turn = text(&turn_changes[0]["turnId"]);
                let event = append_conversation_change(
                    session,
                    &conversation_id,
                    user_id,
                    "turn.patch",
                    change,
                    &first_turn,
                    "",
                    None,
                )?;
                committed.push(committed_event_notice(event, user_id));
            }
            let deleted = str_list(get(clean, "deletedTurnIds"));
            if !deleted.is_empty() {
                let mut change = Obj::new();
                change.insert("deletedTurnIds".into(), Value::Array(deleted));
                change.insert("conversationRevision".into(), revision_of(clean)?);
                let event = append_conversation_change(
                    session,
                    &conversation_id,
                    user_id,
                    "turn.deleted",
                    change,
                    "",
                    "",
                    None,
                )?;
                committed.push(committed_event_notice(event, user_id));
            }
        }
        "turn.delete" | "turn.queue.cancel" => {
            if !clean_result.is_object() {
                return fail("database_protocol_error", "Turn delete returned an invalid result");
            }
            let (conversation_id, user_id) = turn_identity(payload)?;
            let deleted = str_list(get(clean, "deletedTurnIds"));
            if !deleted.is_empty() {
                let mut change = Obj::new();
                change.insert("deletedTurnIds".into(), Value::Array(deleted));
                if operation == "turn.queue.cancel" && truthy(get(clean, "queueId")) {
                    change.insert("removedQueueIds".into(), json!([python_str(get(clean, "queueId"))]));
                }
                change.insert("conversationRevision".into(), revision_of(clean)?);
                let event = append_conversation_change(
                    session,
                    &conversation_id,
                    user_id,
                    "turn.deleted",
                    change,
                    "",
                    "",
                    None,
                )?;
                committed.push(committed_event_notice(event, user_id));
            } else if operation == "turn.queue.cancel" && truthy(get(clean, "cancelled")) {
                let queue_id = python_str(clean.get("queueId").ok_or_else(key_error)?);
                let revision = py_int(clean.get("conversationRevision").ok_or_else(key_error)?)?;
                let change = json!({
                    "turns": [],
                    "attempts": [],
                    "removedQueueIds": [queue_id],
                    "conversationRevision": revision,
                });
                let event = append_conversation_change(
                    session,
                    &conversation_id,
                    user_id,
                    "turn.upsert",
                    as_obj(&change),
                    "",
                    "",
                    None,
                )?;
                committed.push(committed_event_notice(event, user_id));
            }
        }
        "turn.attempt.bind"
        | "turn.attempt.dispatch_worker"
        | "turn.attempt.start"
        | "turn.event.record"
        | "turn.event.record_batch"
        | "turn.recover"
        | "turn.related.announce"
        | "turn.visible.sync" => {}
        _ => {
            return fail(
                "database_protocol_error",
                format!("Turn command {operation} has no conversation-sync projection"),
            )
        }
    }
    for attempt_event in &attempt_events {
        let event = attempt_event.as_object().cloned().unwrap_or_default();
        let conversation_id = text(get(&event, "conversationId"));
        let turn_id = text(get(&event, "turnId"));
        let attempt_id = text(get(&event, "attemptId"));
        let sequence = get(&event, "seq");
        let valid_sequence =
            matches!(sequence, Value::Number(number) if number.as_i64().is_some_and(|value| value >= 1));
        let valid_payload = matches!(get(&event, "payload"), Value::Object(body) if body.get("projectionPatch").is_some_and(Value::is_object));
        if !valid_sequence || !valid_payload {
            return fail("database_protocol_error", "Attempt change produced no revision-checked projection patch");
        }
        let user_id = conversation_owner_for_turn(session, &conversation_id, &turn_id)?;
        let mut change = Obj::new();
        change.insert("event".into(), attempt_event.clone());
        let appended = append_conversation_change(
            session,
            &conversation_id,
            user_id,
            "attempt.event",
            change,
            &turn_id,
            &attempt_id,
            Some(sequence),
        )?;
        committed.push(committed_event_notice(appended, user_id));
    }
    for carrier in &committed {
        let user_id = carrier["userId"].as_i64().unwrap_or(0);
        session.hint_conversation_change(user_id, carrier["event"]["conversationId"].as_str().unwrap_or(""));
    }
    if committed.is_empty() {
        return Ok(clean_result);
    }
    Ok(json!({
        "_storageCommitContract": STORAGE_COMMITTED_EVENTS_CONTRACT,
        "value": clean_result,
        "events": committed,
    }))
}

fn key_error() -> crate::error::Failure {
    crate::error::Failure::Internal("KeyError".into())
}
