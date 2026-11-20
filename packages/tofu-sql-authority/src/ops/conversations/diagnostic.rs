//! `operations_pkg/_conversation_diagnostic.py`: read-only, revision-fenced
//! conversation diagnostics without projection hydration (SQLite dialect).

use base64::Engine;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::support::{
    projection_head_state, turn_public_metadata_with, HeadState, PROJECTION_HEAD_MAX_PATCHES,
    PROJECTION_HEAD_MAX_PATCH_BYTES, PROJECTION_PATCH_VERSION,
};
use crate::codec;
use crate::error::{fail, Failure, OpResult};
use crate::json;
use crate::params;
use crate::payload::Payload;
use crate::session::{Row, Session, SqlValue};

const CHUNK_BYTES: i64 = 65536;
const METADATA_BYTES: i64 = 1024 * 1024;
const EVENT_ROWS: i64 = PROJECTION_HEAD_MAX_PATCHES * 4 + 1;
const TURN_COLUMNS: &str = "turn_id,conversation_id,user_id,presentation_id,lane_id,parent_turn_id,\
ordinal,actor,kind,run_id,status,current_attempt_id,projection_revision,\
projection_checkpoint_revision,projection_materialized_revision,\
projection_patch_count,projection_patch_bytes,created_at,updated_at";
const PAYLOAD_FIELDS: &[&str] = &["conv_id", "user_id", "kind", "revision", "index", "offset", "limit", "source_token"];

fn invalid<T>(message: &str) -> OpResult<T> {
    fail("database_invalid_payload", message)
}

fn integrity<T>(message: &str) -> OpResult<T> {
    fail("database_integrity", message)
}

/// `type(value) is int` (bool excluded).
fn exact_int(value: &Value) -> Option<i64> {
    json::as_strict_int(value)
}

/// `_payload(payload)`.
fn validate_payload(payload: &Payload) -> OpResult<(String, i64, String)> {
    if payload.keys().any(|key| !PAYLOAD_FIELDS.contains(&key.as_str())) {
        return invalid("Invalid conversation diagnostic payload");
    }
    let conv_id = match payload.get("conv_id") {
        Some(Value::String(text)) if !super::support::py_strip(text).is_empty() => text.clone(),
        _ => return invalid("Invalid diagnostic conversation identity"),
    };
    let user_id = payload.get("user_id").and_then(exact_int).filter(|user| *user >= 1);
    let kind = match payload.get("kind") {
        Some(Value::String(kind)) if matches!(kind.as_str(), "header" | "turn" | "chunk") => Some(kind.clone()),
        _ => None,
    };
    let (Some(user_id), Some(kind)) = (user_id, kind) else {
        return invalid("Invalid diagnostic owner or kind");
    };
    for field in ["revision", "index", "offset", "limit"] {
        let Some(value) = payload.get(field) else { continue };
        let minimum = if field == "limit" { 1 } else { 0 };
        match exact_int(value) {
            Some(number) if number >= minimum => {
                if field == "limit" && number > CHUNK_BYTES {
                    return invalid("Diagnostic chunk limit exceeds 65536 bytes");
                }
            }
            _ => return invalid(&format!("Invalid diagnostic {field}")),
        }
    }
    if kind != "header" && !payload.contains_key("index") {
        return invalid("Diagnostic turn index is required");
    }
    let has_any = |fields: &[&str]| fields.iter().any(|field| payload.contains_key(*field));
    if kind == "header" && has_any(&["index", "offset", "limit", "source_token"]) {
        return invalid("Invalid fields for diagnostic header");
    }
    if kind == "turn" && has_any(&["offset", "limit", "source_token"]) {
        return invalid("Invalid fields for diagnostic turn");
    }
    if kind == "chunk" {
        let valid = matches!(payload.get("source_token"), Some(Value::String(token))
            if token.len() == 64 && token.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')));
        if !valid {
            return invalid("Diagnostic chunk requires a 64-lowercase-hex source_token");
        }
    }
    Ok((conv_id, user_id, kind))
}

fn bytes_expr(expression: &str) -> String {
    format!("CAST(({expression}) AS BLOB)")
}

fn json_type_expr(expression: &str) -> String {
    let text = format!("CAST(({expression}) AS TEXT)");
    format!("CASE WHEN json_valid({text}) THEN json_type({text}) ELSE 'invalid' END")
}

fn bounded_json(column: &str) -> String {
    let raw = bytes_expr(column);
    format!("CASE WHEN length({raw}) <= {METADATA_BYTES} THEN {raw} END")
}

/// `_object(raw, label)`.
fn object(raw: &SqlValue, label: &str) -> OpResult<Map<String, Value>> {
    let decoded = match raw {
        SqlValue::Null => return integrity(&format!("Diagnostic {label} exceeds its metadata budget")),
        SqlValue::Integer(integer) => Value::from(*integer),
        SqlValue::Real(real) => json::float_value(*real),
        SqlValue::Text(text) => match json::loads(text.as_bytes()) {
            Ok(value) => value,
            Err(_) => return integrity(&format!("Diagnostic {label} is malformed")),
        },
        SqlValue::Blob(bytes) => match json::loads(bytes) {
            Ok(value) => value,
            Err(_) => return integrity(&format!("Diagnostic {label} is malformed")),
        },
    };
    match codec::load_value(decoded)? {
        Value::Object(map) => Ok(map),
        _ => integrity(&format!("Diagnostic {label} must be an object")),
    }
}

/// `_fence`.
fn fence(session: &Session<'_>, conv_id: &str, user_id: i64, revision: i64) -> OpResult<bool> {
    let Some(row) = session
        .fetch_one("SELECT rev FROM storage_conversations WHERE id=? AND user_id=?", &params![conv_id, user_id])?
    else {
        return Ok(false);
    };
    if *row.get("rev") != SqlValue::Integer(revision) {
        return fail("database_conflict", "Conversation diagnostic revision changed");
    }
    Ok(true)
}

/// `_legacy_count`.
fn legacy_count(session: &Session<'_>, conv_id: &str, user_id: i64) -> OpResult<i64> {
    let text = "CAST(messages_json AS TEXT)";
    let count = format!(
        "CASE WHEN json_valid({text}) THEN CASE WHEN json_type({text})='array' \
         THEN json_array_length({text}) ELSE -1 END ELSE -1 END"
    );
    let Some(row) = session.fetch_one(
        &format!("SELECT {count} AS count FROM storage_conversations WHERE id=? AND user_id=?"),
        &params![conv_id, user_id],
    )?
    else {
        return fail("database_conflict", "Conversation disappeared during diagnostic");
    };
    let count = super::support::py_int(row.get("count"))?;
    if count < 0 {
        return integrity("Legacy conversation messages are malformed");
    }
    Ok(count)
}

/// The `_source(...)` row: SQL metadata plus the SQLite source digest.
struct Source {
    row: Row,
    digest: Option<String>,
}

impl Source {
    fn total_bytes(&self) -> i64 {
        self.row.get("total_bytes").as_int().unwrap_or(0)
    }

    fn field(&self, key: &str) -> Value {
        if self.row.contains(key) {
            self.row.get(key).to_json().unwrap_or(Value::Null)
        } else {
            Value::Null
        }
    }
}

/// `_source(...)`.
fn source(
    session: &Session<'_>,
    turn: &Row,
    head: &HeadState,
    validate: bool,
    digest: bool,
    offset: Option<i64>,
    limit: i64,
) -> OpResult<Source> {
    let checkpoint = head.checkpoint_active();
    let expression = if checkpoint { "cp.projection_json" } else { "t.projection_json" };
    let raw = bytes_expr(expression);
    let mut columns = vec![format!("length({raw}) AS total_bytes")];
    let source_alias = if checkpoint { "cp" } else { "t" };
    if digest {
        columns.push(format!("{source_alias}.rowid AS source_rowid"));
    }
    if validate {
        columns.push(format!("{} AS source_type", json_type_expr(expression)));
    }
    if checkpoint {
        columns.push("cp.projection_bytes AS declared_bytes".into());
        columns.push("cp.updated_at AS checkpoint_updated_at".into());
    }
    let mut values = Vec::new();
    if let Some(offset) = offset {
        columns.push(format!("substr({raw}, ?, ?) AS data"));
        values.push(SqlValue::from(offset + 1));
        values.push(SqlValue::from(limit));
    }
    let join = if checkpoint {
        " LEFT JOIN storage_turn_projection_checkpoints cp \
         ON cp.turn_id=t.turn_id AND cp.conversation_id=t.conversation_id \
         AND cp.user_id=t.user_id AND cp.attempt_id=COALESCE(t.current_attempt_id,'') \
         AND cp.projection_revision=t.projection_checkpoint_revision"
    } else {
        ""
    };
    values.extend([
        turn.get("turn_id").clone(),
        turn.get("conversation_id").clone(),
        turn.get("user_id").clone(),
        turn.get("projection_revision").clone(),
    ]);
    let Some(row) = session.fetch_one(
        &format!(
            "SELECT {} FROM storage_conversation_turns t{join} WHERE t.turn_id=? AND t.conversation_id=? AND t.user_id=? \
             AND t.projection_revision=?",
            columns.join(",")
        ),
        &values,
    )?
    else {
        return fail("database_conflict", "Diagnostic turn revision changed");
    };
    let size = match row.get("total_bytes") {
        SqlValue::Integer(size) if *size >= 2 => *size,
        _ => return integrity("Diagnostic projection base is missing or empty"),
    };
    if validate && *row.get("source_type") != SqlValue::Text("object".into()) {
        return integrity("Diagnostic projection base must be a valid JSON object");
    }
    if checkpoint {
        let declared = match row.get("declared_bytes") {
            SqlValue::Integer(declared) if *declared >= 1 => *declared,
            _ => return integrity("Diagnostic checkpoint byte evidence is invalid"),
        };
        if declared != size {
            return integrity("Diagnostic checkpoint byte evidence is inconsistent");
        }
    }
    let mut source_digest = None;
    if digest {
        let table = if checkpoint { "storage_turn_projection_checkpoints" } else { "storage_conversation_turns" };
        let blob = session.fetch_one(
            &format!("SELECT CAST(projection_json AS BLOB) AS source FROM {table} WHERE rowid=?"),
            &[row.get("source_rowid").clone()],
        )?;
        let bytes = match blob.as_ref().map(|blob| blob.get("source")) {
            Some(SqlValue::Blob(bytes)) => bytes.clone(),
            Some(SqlValue::Text(text)) => text.as_bytes().to_vec(),
            _ => Vec::new(),
        };
        if bytes.len() as i64 != size {
            return fail("database_conflict", "Diagnostic projection source changed");
        }
        source_digest = Some(hex::encode(Sha256::digest(&bytes)));
    }
    Ok(Source { row, digest: source_digest })
}

/// `_same_source(before, after)` (SQLite carries no row versions).
fn same_source(before: &Source, after: &Source) -> OpResult<()> {
    for key in ["total_bytes", "declared_bytes", "checkpoint_updated_at"] {
        if before.field(key) != after.field(key) {
            return fail("database_conflict", "Diagnostic projection source changed");
        }
    }
    Ok(())
}

/// `_validate_patch(patch, revision)`.
fn validate_patch(patch: &Map<String, Value>, revision: i64) -> OpResult<()> {
    for (field, expected) in
        [("version", PROJECTION_PATCH_VERSION), ("baseRevision", revision - 1), ("targetRevision", revision)]
    {
        if patch.get(field).and_then(exact_int) != Some(expected) {
            return integrity("Diagnostic projection patch chain is misbased or unsupported");
        }
    }
    let Some(Value::Array(operations)) = patch.get("operations") else {
        return integrity("Diagnostic projection patch operations are malformed");
    };
    for operation in operations {
        let Value::Object(operation) = operation else {
            return integrity("Diagnostic projection patch operation is malformed");
        };
        let path = match operation.get("path") {
            Some(Value::Array(path))
                if path.iter().all(|part| part.is_string() || exact_int(part).is_some_and(|index| index >= 0)) =>
            {
                path
            }
            _ => return integrity("Diagnostic projection patch path is malformed"),
        };
        let op = match operation.get("op") {
            Some(Value::String(op))
                if matches!(op.as_str(), "set" | "remove" | "append_text" | "append" | "truncate") =>
            {
                op.as_str()
            }
            _ => return integrity("Diagnostic projection patch operation is unsupported"),
        };
        let value = operation.get("value");
        let malformed = match op {
            "set" => value.is_none() || (path.is_empty() && !value.is_some_and(Value::is_object)),
            "remove" => path.is_empty(),
            "append_text" => !value.is_some_and(Value::is_string),
            "append" => !value.is_some_and(Value::is_array),
            _ => !operation.get("length").and_then(exact_int).is_some_and(|length| length >= 0),
        };
        if malformed {
            return integrity("Diagnostic projection patch value is malformed");
        }
    }
    Ok(())
}

/// `_patches(session, turn, head)`.
fn patches(session: &Session<'_>, turn: &Row, head: &HeadState) -> OpResult<Vec<Value>> {
    let Some(base) = head.materialized_revision else { return Ok(Vec::new()) };
    let revision = turn.get("projection_revision").as_int().unwrap_or(0);
    if revision - base != head.patch_count || !turn.get("current_attempt_id").truthy() {
        return integrity("Diagnostic projection patch revisions are inconsistent");
    }
    let payload = "CAST(payload_json AS TEXT)";
    let patch =
        format!("CASE WHEN json_valid({payload}) THEN json_extract({payload}, '$.payload.projectionPatch') END");
    let patch_type =
        format!("CASE WHEN json_valid({payload}) THEN json_type({payload}, '$.payload.projectionPatch') END");
    let raw = bytes_expr(&patch);
    let filter = " FROM storage_attempt_events WHERE attempt_id=? AND conversation_id=? \
                  AND turn_id=? AND projection_revision>? AND projection_revision<=?";
    let base_params = vec![
        turn.get("current_attempt_id").clone(),
        turn.get("conversation_id").clone(),
        turn.get("turn_id").clone(),
        SqlValue::from(base),
        SqlValue::from(revision),
    ];
    let candidate_sql = format!(
        "SELECT sequence,projection_revision,{} AS envelope_type,{patch_type} AS patch_type,length({raw}) AS patch_bytes{filter} ORDER BY sequence LIMIT ?",
        json_type_expr("payload_json")
    );
    let mut candidate_params = base_params.clone();
    candidate_params.push(SqlValue::from(EVENT_ROWS));
    let candidates = session.fetch_all(&candidate_sql, &candidate_params)?;
    if candidates.len() as i64 >= EVENT_ROWS {
        return integrity("Diagnostic projection patch event range is ambiguous");
    }
    let mut selected: std::collections::BTreeMap<i64, &Row> = std::collections::BTreeMap::new();
    let physical_byte_budget = head.patch_bytes;
    let mut physical_bytes = 0i64;
    for row in &candidates {
        if *row.get("envelope_type") != SqlValue::Text("object".into()) {
            return integrity("Diagnostic projection event envelope is malformed");
        }
        if row.get("patch_type").is_null() {
            continue;
        }
        if *row.get("patch_type") != SqlValue::Text("object".into()) {
            return integrity("Diagnostic projection patch must be an object");
        }
        let target = row.get("projection_revision");
        if let SqlValue::Integer(target) = target {
            if selected.contains_key(target) {
                return integrity("Diagnostic projection revision has duplicate patches");
            }
        }
        let (SqlValue::Integer(target), SqlValue::Integer(size)) = (target, row.get("patch_bytes")) else {
            return integrity("Diagnostic projection patch byte evidence is invalid");
        };
        if *size < 1 {
            return integrity("Diagnostic projection patch byte evidence is invalid");
        }
        physical_bytes += size;
        if physical_bytes > physical_byte_budget {
            return integrity("Diagnostic projection patch head exceeds its physical byte budget");
        }
        selected.insert(*target, row);
    }
    if !selected.keys().copied().eq(base + 1..=revision) {
        return integrity("Diagnostic projection patch chain has a gap");
    }
    let mut result = Vec::new();
    let mut canonical_bytes = 0i64;
    for target in base + 1..=revision {
        let candidate = selected[&target];
        let mut values = base_params.clone();
        values.extend([
            candidate.get("sequence").clone(),
            SqlValue::from(target),
            candidate.get("patch_bytes").clone(),
        ]);
        let Some(row) = session.fetch_one(
            &format!(
                "SELECT {raw} AS patch_source{filter} AND sequence=? AND projection_revision=? AND length({raw})=?"
            ),
            &values,
        )?
        else {
            return fail("database_conflict", "Diagnostic projection patch source changed");
        };
        let decoded = object(row.get("patch_source"), "projection patch")?;
        validate_patch(&decoded, target)?;
        let decoded = Value::Object(decoded);
        canonical_bytes += codec::dump(&decoded)?.len() as i64;
        if canonical_bytes > PROJECTION_HEAD_MAX_PATCH_BYTES {
            return integrity("Diagnostic projection patch head exceeds its canonical byte budget");
        }
        result.push(json!({"projectionRevision": target, "projectionPatch": decoded}));
    }
    if canonical_bytes != head.patch_bytes {
        return integrity("Diagnostic projection patch byte evidence is inconsistent");
    }
    Ok(result)
}

/// `_source_token(turn, source, patches, index)`.
fn source_token(turn: &Row, settlement: &Value, source: &Source, patches: &[Value], index: i64) -> OpResult<String> {
    let columns = TURN_COLUMNS.split(',').map(|column| turn.get(column).to_json()).collect::<OpResult<Vec<Value>>>()?;
    let envelope = json!([
        "tofu.sql-diagnostic-source/v2",
        index,
        columns,
        settlement,
        patches,
        source.total_bytes(),
        source.field("declared_bytes"),
        source.field("checkpoint_updated_at"),
        source.digest.clone(),
    ]);
    Ok(hex::encode(Sha256::digest(codec::dump(&envelope)?)))
}

/// `_read(session, payload, header, kind)`.
fn read(session: &Session<'_>, payload: &Payload, header: &Row, kind: &str) -> OpResult<Value> {
    let conv_id = super::support::py_str(header.get("id"));
    let user_id = super::support::py_int(header.get("user_id"))?;
    let revision = header.get("rev").to_json()?;
    let counts = session.fetch_one(
        "SELECT COUNT(*) AS total_count FROM storage_conversation_turns \
         WHERE conversation_id=? AND user_id=? AND lane_id='main'",
        &params![conv_id.as_str(), user_id],
    )?;
    let mut total = match counts {
        Some(row) => super::support::py_int(row.get("total_count"))?,
        None => return crate::error::internal("TypeError: 'NoneType' object is not subscriptable"),
    };
    let mut native = total > 0
        || session
            .fetch_one(
                "SELECT 1 AS present FROM storage_conversation_turns \
                 WHERE conversation_id=? AND user_id=? LIMIT 1",
                &params![conv_id.as_str(), user_id],
            )?
            .is_some();
    if !native {
        total = legacy_count(session, &conv_id, user_id)?;
        native = total == 0;
    }
    if kind == "header" {
        let settings = object(header.get("settings_json"), "conversation settings")?;
        let document = super::conversation_document(header, Value::Object(settings), false, Vec::new(), None)?;
        return Ok(json!({"revision": revision, "totalCount": total, "document": document, "turnNative": native}));
    }
    if !native {
        return fail("database_unsupported", "Legacy messages have no bounded diagnostic turn source");
    }
    let index = payload["index"].as_i64().unwrap_or(0);
    if index >= total {
        return invalid("Diagnostic turn index is outside the main lane");
    }
    let Some(turn) = session.fetch_one(
        &format!(
            "SELECT {TURN_COLUMNS},{} AS settlement_json FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? \
             AND lane_id='main' ORDER BY ordinal LIMIT 1 OFFSET ?",
            bounded_json("settlement_json")
        ),
        &params![conv_id.as_str(), user_id, index],
    )?
    else {
        return fail("database_conflict", "Diagnostic turn rank changed");
    };
    let head = projection_head_state(&turn)?;
    let projection_revision = match turn.get("projection_revision") {
        SqlValue::Integer(revision) if *revision >= 0 => *revision,
        _ => return integrity("Diagnostic turn projection revision is invalid"),
    };
    if let Some(materialized) = head.materialized_revision {
        if projection_revision - materialized != head.patch_count {
            return integrity("Diagnostic projection patch revisions are inconsistent");
        }
    }
    let settlement = Value::Object(object(turn.get("settlement_json"), "turn settlement")?);
    let source_row = source(session, &turn, &head, true, true, None, CHUNK_BYTES)?;
    let size = source_row.total_bytes();
    let patch_list = patches(session, &turn, &head)?;
    let token = source_token(&turn, &settlement, &source_row, &patch_list, index)?;
    if kind == "turn" {
        let metadata = turn_public_metadata_with(&turn, codec::load_value(settlement)?)?;
        return Ok(json!({
            "revision": revision,
            "index": index,
            "totalCount": total,
            "turn": metadata,
            "source": {"format": "projection", "totalBytes": size, "token": token},
            "patches": patch_list,
        }));
    }
    if payload["source_token"] != Value::String(token.clone()) {
        return fail("database_conflict", "Diagnostic projection source token changed");
    }
    let offset = payload.get("offset").and_then(Value::as_i64).unwrap_or(0);
    if offset > size {
        return invalid("Diagnostic chunk offset exceeds the source length");
    }
    let limit = payload.get("limit").and_then(Value::as_i64).unwrap_or(CHUNK_BYTES);
    let chunk = source(session, &turn, &head, false, false, Some(offset), limit)?;
    same_source(&source_row, &chunk)?;
    let data = match chunk.row.get("data") {
        SqlValue::Blob(bytes) => bytes.clone(),
        SqlValue::Text(text) => text.as_bytes().to_vec(),
        SqlValue::Null => return crate::error::internal("TypeError: cannot convert 'NoneType' object to bytes"),
        _ => return crate::error::internal("TypeError: cannot convert to bytes"),
    };
    let end = offset + data.len() as i64;
    if data.len() as i64 != limit.min(size - offset) {
        return integrity("Diagnostic projection chunk is incomplete");
    }
    Ok(json!({
        "revision": revision,
        "index": index,
        "offset": offset,
        "totalBytes": size,
        "sourceToken": token,
        "data": base64::engine::general_purpose::STANDARD.encode(&data),
        "nextOffset": if end < size { Value::from(end) } else { Value::Null },
    }))
}

/// `_conversation_diagnostic`.
pub fn conversation_diagnostic(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (conv_id, user_id, kind) = validate_payload(payload)?;
    let metadata = if kind == "header" {
        format!(",title,created_at_ms,updated_at_ms,msg_count,{} AS settings_json", bounded_json("settings_json"))
    } else {
        String::new()
    };
    let Some(header) = session.fetch_one(
        &format!("SELECT id,user_id,rev{metadata} FROM storage_conversations WHERE id=? AND user_id=?"),
        &params![conv_id.as_str(), user_id],
    )?
    else {
        return Ok(Value::Null);
    };
    let revision = match header.get("rev") {
        SqlValue::Integer(revision) if *revision >= 0 => *revision,
        _ => return integrity("Diagnostic conversation revision is invalid"),
    };
    let requested = payload.get("revision");
    if (kind != "header" && requested.is_none())
        || requested.is_some_and(|value| !codec::py_eq(value, &json!(revision)))
    {
        return fail("database_conflict", "Diagnostic requires the exact conversation revision");
    }
    match read(session, payload, &header, &kind) {
        Ok(result) => Ok(if fence(session, &conv_id, user_id, revision)? { result } else { Value::Null }),
        Err(Failure::Storage(error)) => {
            if !fence(session, &conv_id, user_id, revision)? {
                return Ok(Value::Null);
            }
            Err(Failure::Storage(error))
        }
        Err(other) => Err(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patch_validation_matches_operation_shapes() {
        let patch = json!({"version": 1, "baseRevision": 1, "targetRevision": 2, "operations": [
            {"op": "set", "path": [], "value": {}}, {"op": "truncate", "path": ["a"], "length": 0}]});
        assert!(validate_patch(patch.as_object().unwrap(), 2).is_ok());
        let bad =
            json!({"version": 1, "baseRevision": 1, "targetRevision": 2, "operations": [{"op": "remove", "path": []}]});
        assert!(validate_patch(bad.as_object().unwrap(), 2).is_err());
        assert!(super::super::support::nonnegative_integer(&SqlValue::Integer(-1), "x").is_err());
    }
}
