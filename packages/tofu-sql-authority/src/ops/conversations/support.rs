//! Private ports of the turn/projection helpers the conversation and archive
//! handlers share with other domains:
//!
//! - `operations_pkg/_turns_core.py` (`_turn_public`, `_turn_public_metadata`,
//!   `_mark_turn_search_projection_dirty`); shared owner: turns
//! - `turn_projection_head.py` (head state, base load, patch fold); shared owner: turns
//! - `lib/turn_projection_patch.py::apply_projection_patch`; shared owner: turns
//! - `lib/turn_verdict::normalize_turn_settlement` and the
//!   `lib/error_envelope::make_envelope` kinds it emits; shared owner: turns
//! - `operations_pkg/_raw_archives.py::_debit_stored_bytes_for_delete`; shared owner: turns
//!
//! The process-local `TurnProjectionCache` is a reconstructible accelerator
//! and is not ported: every read reconstructs from durable rows.

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::codec;
use crate::error::{fail, internal, OpResult};
use crate::json::{self, python_str, truthy};
use crate::params;
use crate::session::{Row, Session, SqlValue};

/// `int(time.time() * 1000)`.
pub fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_millis() as i64).unwrap_or(0)
}

/// Python `str(value)` of one SQLite column value.
pub fn py_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(integer) => integer.to_string(),
        SqlValue::Real(real) => json::python_float_repr(*real),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(bytes) => format!("b{:?}", String::from_utf8_lossy(bytes)),
    }
}

/// Python `int(value)` of one SQLite column value.
pub fn py_int(value: &SqlValue) -> OpResult<i64> {
    match value.as_int() {
        Some(integer) => Ok(integer),
        None => internal("int() of a non-integer column"),
    }
}

/// Python `int(value or 0)`.
pub fn int_or_zero(value: &SqlValue) -> OpResult<i64> {
    if value.truthy() {
        py_int(value)
    } else {
        Ok(0)
    }
}

/// Python `str(value or default)`.
pub fn str_or(value: &SqlValue, default: &str) -> String {
    if value.truthy() {
        py_str(value)
    } else {
        default.to_owned()
    }
}

/// Python `str.isspace()` for one code point.
pub fn py_isspace(code: u32) -> bool {
    matches!(code, 0x09..=0x0d | 0x1c..=0x20 | 0x85 | 0xa0 | 0x1680 | 0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000)
}

/// Python `str.strip()`.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(|character: char| py_isspace(character as u32))
}

/// Python `text[:count]` in code points.
pub fn prefix_chars(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// Python `sorted(set_of_str)` rendered by `repr` (`['a', 'b']`).
pub fn sorted_repr(mut items: Vec<String>) -> String {
    items.sort_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    json::python_repr(&Value::Array(items.into_iter().map(Value::String).collect()))
}

/// `_load(column)` returning the decoded JSON value.
pub fn load(value: &SqlValue) -> OpResult<Value> {
    codec::load(value)
}

// ── Turn-search dirty markers (`_turns_core.py`) ──────────────────────────

const TURN_SEARCH_PROJECTION_NAME: &str = "turn_search.v1";

/// `_mark_turn_search_projection_dirty`.
pub fn mark_turn_search_projection_dirty(
    session: &Session<'_>,
    entity_kind: &str,
    user_id: i64,
    entity_key: &str,
) -> OpResult<String> {
    if !matches!(entity_kind, "turn" | "conversation" | "rebuild") {
        return fail("database_internal", "Invalid turn-search projection entity");
    }
    if entity_key.is_empty() || entity_key.chars().count() > 256 {
        return fail("database_internal", "Invalid turn-search projection key");
    }
    let token = uuid::Uuid::new_v4().simple().to_string();
    session.execute(
        "INSERT INTO storage_projection_outbox(\
         projection_name,entity_kind,user_id,entity_key,version_token,\
         enqueued_at_ms) VALUES (?,?,?,?,?,?) \
         ON CONFLICT(projection_name,entity_kind,user_id,entity_key) \
         DO UPDATE SET version_token=excluded.version_token,\
         enqueued_at_ms=excluded.enqueued_at_ms",
        &params![TURN_SEARCH_PROJECTION_NAME, entity_kind, user_id, entity_key, token.as_str(), now_ms()],
    )?;
    Ok(token)
}

/// `_mark_conversation_search_projection_dirty`.
pub fn mark_conversation_search_projection_dirty(
    session: &Session<'_>,
    conversation_id: &str,
    user_id: i64,
) -> OpResult<String> {
    mark_turn_search_projection_dirty(session, "conversation", user_id, conversation_id)
}

// ── Raw-archive usage counter (`_raw_archives.py`) ────────────────────────

const RAW_ARCHIVE_USAGE_META_KEY: &str = "raw_archive_stored_bytes";

fn stored_bytes_usage(session: &Session<'_>) -> OpResult<i64> {
    if let Some(row) = session
        .fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key=?", &params![RAW_ARCHIVE_USAGE_META_KEY])?
    {
        return py_int(row.get("meta_value"));
    }
    let total_row =
        session.fetch_one("SELECT COALESCE(SUM(stored_bytes),0) AS total FROM storage_raw_archives", &[])?;
    let total = match total_row {
        Some(row) => int_or_zero(row.get("total"))?,
        None => 0,
    };
    session.execute(
        "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)",
        &params![RAW_ARCHIVE_USAGE_META_KEY, total.to_string()],
    )?;
    Ok(total)
}

/// `_debit_stored_bytes_for_delete`.
pub fn debit_stored_bytes_for_delete(session: &Session<'_>, predicate: &str, values: &[SqlValue]) -> OpResult<()> {
    let row = session.fetch_one(
        &format!("SELECT COALESCE(SUM(stored_bytes),0) AS total FROM storage_raw_archives WHERE {predicate}"),
        values,
    )?;
    let total = match row {
        Some(row) => int_or_zero(row.get("total"))?,
        None => 0,
    };
    if total != 0 {
        let usage = stored_bytes_usage(session)?;
        session.execute(
            "UPDATE storage_meta SET meta_value=? WHERE meta_key=?",
            &params![(usage - total).to_string(), RAW_ARCHIVE_USAGE_META_KEY],
        )?;
    }
    Ok(())
}

// ── Projection head (`turn_projection_head.py`) ───────────────────────────

pub const PROJECTION_HEAD_MAX_PATCHES: i64 = 64;
pub const PROJECTION_HEAD_MAX_PATCH_BYTES: i64 = 1024 * 1024;

#[derive(Clone, Copy, Debug)]
pub struct HeadState {
    pub materialized_revision: Option<i64>,
    pub patch_count: i64,
    pub patch_bytes: i64,
    pub checkpoint_revision: Option<i64>,
}

impl HeadState {
    pub fn active(&self) -> bool {
        self.materialized_revision.is_some()
    }

    pub fn checkpoint_active(&self) -> bool {
        self.checkpoint_revision.is_some()
    }
}

/// `_row_value(row, field)` without a default.
pub fn row_value<'r>(row: &'r Row, field: &str) -> OpResult<&'r SqlValue> {
    if row.contains(field) {
        Ok(row.get(field))
    } else {
        fail("database_integrity", format!("Turn row is missing {field}"))
    }
}

/// `_nonnegative_integer(value, field)`.
pub fn nonnegative_integer(value: &SqlValue, field: &str) -> OpResult<i64> {
    match value {
        SqlValue::Integer(integer) if *integer >= 0 => Ok(*integer),
        _ => fail("database_integrity", format!("Turn {field} must be a nonnegative integer")),
    }
}

/// `projection_head_state(row)`.
pub fn projection_head_state(row: &Row) -> OpResult<HeadState> {
    let raw_checkpoint = row.get("projection_checkpoint_revision");
    let checkpoint_revision = if raw_checkpoint.is_null() {
        None
    } else {
        Some(nonnegative_integer(raw_checkpoint, "projection checkpoint revision")?)
    };
    let raw_materialized = row.get("projection_materialized_revision");
    let materialized_revision = if raw_materialized.is_null() {
        None
    } else {
        Some(nonnegative_integer(raw_materialized, "projection materialized revision")?)
    };
    let zero = SqlValue::Integer(0);
    let count_value = if row.contains("projection_patch_count") { row.get("projection_patch_count") } else { &zero };
    let patch_count = nonnegative_integer(count_value, "projection patch count")?;
    let bytes_value = if row.contains("projection_patch_bytes") { row.get("projection_patch_bytes") } else { &zero };
    let patch_bytes = nonnegative_integer(bytes_value, "projection patch bytes")?;
    match materialized_revision {
        None => {
            if patch_count != 0 || patch_bytes != 0 {
                return fail("database_integrity", "Materialized Turn has residual projection patch metadata");
            }
        }
        Some(_) => {
            if !(1..=PROJECTION_HEAD_MAX_PATCHES).contains(&patch_count)
                || !(1..=PROJECTION_HEAD_MAX_PATCH_BYTES).contains(&patch_bytes)
            {
                return fail("database_integrity", "Turn projection patch head exceeds its budget");
            }
        }
    }
    if let Some(checkpoint) = checkpoint_revision {
        let current_revision = nonnegative_integer(row_value(row, "projection_revision")?, "projection revision")?;
        let expected = materialized_revision.unwrap_or(current_revision);
        if checkpoint != expected {
            return fail("database_integrity", "Turn projection checkpoint revision is inconsistent");
        }
    }
    Ok(HeadState { materialized_revision, patch_count, patch_bytes, checkpoint_revision })
}

/// `projection_from_turn_row`: the turns owner's reader, so conversation
/// reads share its projection cache exactly as Python's do.
pub fn projection_from_turn_row(session: &Session<'_>, row: &Row) -> OpResult<Value> {
    crate::ops::turns::head::projection_from_turn_row(session, row)
}

// ── Projection patch (`lib/turn_projection_patch.py`) ─────────────────────

pub const PROJECTION_PATCH_VERSION: i64 = 1;

// ── Settlement normalization (`lib/turn_verdict`) ─────────────────────────

const FAILED_STREAM_STATES: &[&str] = &[
    "premature_close",
    "malformed_stream",
    "semantic_progress_timeout",
    "no_actionable_output",
    "empty_response",
    "tool_payload_missing",
    "gateway_limit_response",
];
const STREAM_STATES: &[&str] = &[
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

/// `parse_provider_stream_state(value)` as its enum value.
fn parse_provider_stream_state(value: Option<&Value>) -> Option<&'static str> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) if text.is_empty() => None,
        Some(other) => {
            let text = python_str(other);
            Some(STREAM_STATES.iter().copied().find(|state| *state == text).unwrap_or("unknown"))
        }
    }
}

fn truncate_chars(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// `make_envelope(kind, detail=..., context='settlement-normalization',
/// source='turn-verdict', raw=...)` for the kinds normalization emits.
fn settlement_envelope(kind: &str, detail: &str, raw: &str) -> Value {
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
    json!({
        "kind": kind,
        "severity": severity,
        "retryable": retryable,
        "message": message,
        "hint": hint,
        "detail": truncate_chars(detail, 300),
        "model": "",
        "context": "settlement-normalization",
        "source": "turn-verdict",
        "raw": truncate_chars(raw, 300),
        "titleKey": format!("err.k.{kind}.title"),
        "hintKey": format!("err.k.{kind}.hint"),
    })
}

fn text_or(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(value) if truthy(value) => python_str(value),
        _ => default.to_owned(),
    }
}

/// `normalize_turn_settlement(value, status=status)`.
pub fn normalize_turn_settlement(value: &Value, status: &str) -> Map<String, Value> {
    let mut settlement = value.as_object().cloned().unwrap_or_default();
    let default_outcome = if status == "superseded" {
        "interrupted"
    } else if status.is_empty() {
        "failed"
    } else {
        status
    };
    let mut outcome = text_or(settlement.get("outcome"), default_outcome);
    if !matches!(outcome.as_str(), "completed" | "interrupted" | "truncated" | "failed") {
        outcome = "failed".into();
    }
    let mut cause = text_or(settlement.get("cause"), "ingested");
    let finish_reason = text_or(settlement.get("providerFinishReason"), "");
    let mut stream_state = parse_provider_stream_state(settlement.get("streamState"));
    let raw_evidence = text_or(settlement.get("evidence"), "");
    let historical_stream_failure = matches!(finish_reason.as_str(), "premature_close" | "abnormal_stop")
        || stream_state.is_some_and(|state| FAILED_STREAM_STATES.contains(&state))
        || cause == "provider_stream_error"
        || raw_evidence == "provider_stream_failure";
    let historical_completion_missing = stream_state == Some("unknown")
        || cause == "completion_evidence_missing"
        || raw_evidence == "missing_completion_evidence";
    let historical_generation_failure = settlement.get("error").is_some_and(truthy)
        || matches!(finish_reason.as_str(), "error" | "timeout")
        || cause == "generation_error"
        || raw_evidence == "generation_error";
    let contradictory_completed = outcome == "completed"
        && (historical_stream_failure
            || historical_completion_missing
            || historical_generation_failure
            || matches!(cause.as_str(), "provider_stream_error" | "generation_error" | "completion_evidence_missing")
            || matches!(
                raw_evidence.as_str(),
                "provider_stream_failure" | "generation_error" | "missing_completion_evidence"
            ));
    if contradictory_completed {
        outcome = "failed".into();
        let original_cause = text_or(settlement.get("cause"), "");
        let (evidence, error_kind) = if historical_stream_failure {
            cause = "provider_stream_error".into();
            let state = stream_state.unwrap_or("premature_close");
            stream_state = Some(state);
            settlement.insert("streamState".into(), Value::String(state.into()));
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
        if !settlement.get("error").is_some_and(truthy) {
            let reason = if !finish_reason.is_empty() {
                finish_reason.clone()
            } else if !raw_evidence.is_empty() {
                raw_evidence.clone()
            } else {
                cause.clone()
            };
            let detail =
                format!("A historical turn marked completed carried explicit terminal failure evidence ({reason}).");
            let raw = format!(
                "outcome=completed cause={original_cause} finish={finish_reason} stream_state={} evidence={raw_evidence}",
                stream_state.unwrap_or("")
            );
            settlement.insert("error".into(), settlement_envelope(&error_kind, &detail, &raw));
        }
    }
    settlement.insert("outcome".into(), Value::String(outcome.clone()));
    settlement.insert("cause".into(), Value::String(cause.clone()));
    for (key, default) in [
        ("providerFinishReason", Value::Null),
        ("error", Value::Null),
        ("resumeOptions", Value::Array(Vec::new())),
        ("streamState", Value::Null),
    ] {
        if !settlement.contains_key(key) {
            settlement.insert(key.into(), default);
        }
    }
    if !settlement.get("evidence").is_some_and(truthy) {
        let inferred = if cause == "server_restart" || cause == "conversation_deleted" {
            "system_recovery"
        } else if outcome == "truncated" {
            "provider_limit"
        } else if outcome == "failed" {
            "generation_error"
        } else if cause == "provider_finished" && settlement.get("providerFinishReason").is_some_and(truthy) {
            "legacy_finish"
        } else {
            "external_authority"
        };
        settlement.insert("evidence".into(), Value::String(inferred.into()));
    }
    settlement
}

// ── Public turn documents (`_turns_core.py`) ──────────────────────────────

const TERMINAL_STATUSES: &[&str] = &["completed", "interrupted", "truncated", "failed", "superseded"];

/// `_stored_object(_load(raw), field_name)` given the already loaded value.
pub fn stored_object(loaded: Value, field_name: &str) -> OpResult<Map<String, Value>> {
    match loaded {
        Value::Object(map) => Ok(map),
        _ => fail("database_integrity", format!("Stored {field_name} must be an object")),
    }
}

/// `_turn_public_metadata(row)` with `_load(row['settlement_json'])` supplied.
pub fn turn_public_metadata_with(row: &Row, loaded_settlement: Value) -> OpResult<Map<String, Value>> {
    let stored_status = py_str(row.require("status")?);
    let raw_settlement = stored_object(loaded_settlement, "turn settlement")?;
    let terminal = TERMINAL_STATUSES.contains(&stored_status.as_str());
    let public_settlement = if terminal {
        normalize_turn_settlement(&Value::Object(raw_settlement), &stored_status)
    } else {
        raw_settlement
    };
    let mut status = stored_status.clone();
    let normalized_outcome = text_or(public_settlement.get("outcome"), "");
    if terminal && matches!(normalized_outcome.as_str(), "completed" | "interrupted" | "truncated" | "failed") {
        status = normalized_outcome;
    }
    let turn_id = row.require("turn_id")?;
    let presentation = if row.contains("presentation_id") && row.get("presentation_id").truthy() {
        py_str(row.get("presentation_id"))
    } else {
        py_str(turn_id)
    };
    let projection_revision = int_or_zero(row.require("projection_revision")?)?;
    let mut map = Map::new();
    map.insert("turnId".into(), Value::String(py_str(turn_id)));
    map.insert("presentationId".into(), Value::String(presentation));
    map.insert("conversationId".into(), Value::String(py_str(row.require("conversation_id")?)));
    map.insert("laneId".into(), Value::String(str_or(row.require("lane_id")?, "main")));
    map.insert("parentTurnId".into(), row.require("parent_turn_id")?.to_json()?);
    map.insert("ordinal".into(), Value::from(py_int(row.require("ordinal")?)?));
    map.insert("actor".into(), Value::String(py_str(row.require("actor")?)));
    map.insert("kind".into(), Value::String(str_or(row.require("kind")?, "reply")));
    map.insert("runId".into(), Value::String(str_or(row.require("run_id")?, "")));
    map.insert("status".into(), Value::String(status));
    map.insert("currentAttemptId".into(), row.require("current_attempt_id")?.to_json()?);
    map.insert("projectionRevision".into(), Value::from(projection_revision));
    if row.contains("model_context_display_offset") && !row.get("model_context_display_offset").is_null() {
        let offset = int_or_zero(row.get("model_context_display_offset"))?;
        map.insert("modelContextRevision".into(), Value::from(projection_revision - offset));
    }
    map.insert("settlement".into(), Value::Object(public_settlement));
    map.insert("createdAt".into(), Value::from(py_int(row.require("created_at")?)?));
    map.insert("updatedAt".into(), Value::from(py_int(row.require("updated_at")?)?));
    Ok(map)
}

/// `_turn_public(session, row)`.
pub fn turn_public(session: &Session<'_>, row: &Row) -> OpResult<Map<String, Value>> {
    let mut public = turn_public_metadata_with(row, load(row.require("settlement_json")?)?)?;
    public.insert("projection".into(), projection_from_turn_row(session, row)?);
    Ok(public)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patch_operations_copy_on_write() {
        let base = json!({"content": "a", "list": [1, 2, 3]});
        let patch = json!({"version": 1, "baseRevision": 0, "targetRevision": 1, "operations": [
            {"op": "append_text", "path": ["content"], "value": "b"},
            {"op": "truncate", "path": ["list"], "length": 1},
            {"op": "append", "path": ["list"], "value": [9]},
            {"op": "set", "path": ["new"], "value": {"x": 1}},
            {"op": "remove", "path": ["new", "x"]},
        ]});
        let next = crate::ops::turns::patch::apply_projection_patch(&base, &patch).unwrap();
        assert_eq!(next, json!({"content": "ab", "list": [1, 9], "new": {}}));
        assert!(crate::ops::turns::patch::apply_projection_patch(
            &base,
            &json!({"version": 1, "operations": [{"op": "remove", "path": []}]})
        )
        .is_err());
        assert!(crate::ops::turns::patch::apply_projection_patch(&base, &json!({"version": true, "operations": []}))
            .is_err());
    }

    #[test]
    fn contradictory_completed_settlement_fails_closed() {
        let settlement = normalize_turn_settlement(
            &json!({"outcome": "completed", "providerFinishReason": "abnormal_stop"}),
            "completed",
        );
        assert_eq!(settlement["outcome"], "failed");
        assert_eq!(settlement["cause"], "provider_stream_error");
        assert_eq!(settlement["streamState"], "premature_close");
        assert_eq!(settlement["error"]["kind"], "abnormal_stop");
        let plain = normalize_turn_settlement(&json!({}), "completed");
        assert_eq!(serde_json::to_string(&plain).unwrap(), "{\"outcome\":\"completed\",\"cause\":\"ingested\",\"providerFinishReason\":null,\"error\":null,\"resumeOptions\":[],\"streamState\":null,\"evidence\":\"external_authority\"}");
    }
}
