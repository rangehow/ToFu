//! `operation_domains/paper_documents.py`: owned paper source bindings,
//! build publication fences and reading-state CAS
//! (`operations_pkg/_paper_documents.py`).
//!
//! Records go through the generic record primitive (`_records._record_get` /
//! `_record_put`): sorted orjson BLOBs with a per-record version.

use std::io::Read;

use serde_json::{json, Map, Value};

use super::paper_artifacts::contract::{limit, python_json_dumps, sha256_hex, source_binding_key, validate};
use super::paper_artifacts::{latest_retained_receipt, now_ms, read_retained_receipt, retain_build};
use super::research::workspace::research_workspace_get;
use super::{register as add, OperationSpec, Registry};
use crate::codec::{dump_param, load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get as payload_get, integer, required_text, Payload};
use crate::session::Session;

const DOCUMENT: &str = "paper.document.v1";
const SOURCE: &str = "paper.document.source.v1";
const CURRENT: &str = "paper.document.current.v1";
const BUDGET: &str = "paper.document.budget.v1";

pub fn register(registry: &mut Registry) {
    add(registry, "paper.document.register", OperationSpec::command(false, paper_document_register));
    add(registry, "paper.document.resolve", OperationSpec::query(paper_document_resolve));
    add(registry, "paper.document.get", OperationSpec::query(paper_document_get));
    add(registry, "paper.document.current", OperationSpec::query(paper_document_current));
    add(registry, "paper.document.request", OperationSpec::command(false, paper_document_request));
    add(registry, "paper.document.finish", OperationSpec::command(false, paper_document_finish));
    add(registry, "paper.document.abandon", OperationSpec::command(false, paper_document_abandon));
    add(registry, "paper.document.reading", OperationSpec::command(true, paper_document_reading));
}

fn record_key(key: &str) -> OpResult<()> {
    if key.is_empty() || key.chars().count() > 512 {
        return fail("database_protocol_error", "Invalid key in storage request");
    }
    Ok(())
}

/// `_get`: private port of `operations_pkg/_records.py::_record_get` for
/// this module's fixed namespaces; shared owner: core.
fn get(session: &Session<'_>, namespace: &str, key: &str) -> OpResult<Value> {
    record_key(key)?;
    let row = session.fetch_one(
        "SELECT value_json, version, updated_at_ms FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![namespace, key],
    )?;
    match row {
        Some(row) => load(row.get("value_json")),
        None => Ok(Value::Null),
    }
}

/// `_put`: private port of `operations_pkg/_records.py::_record_put` (no
/// expected version); shared owner: core.
fn put(session: &Session<'_>, namespace: &str, key: &str, value: &Value) -> OpResult<()> {
    record_key(key)?;
    let encoded = dump_param(value)?;
    let now = now_ms();
    session.lock_key("record", &format!("{namespace}:{key}"));
    let current = session.fetch_one(
        "SELECT version FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![namespace, key],
    )?;
    let actual = match current {
        Some(row) => row.int("version")?,
        None => 0,
    };
    session.execute(
        "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) VALUES (?, ?, ?, ?, ?) ON CONFLICT(namespace, record_key) DO UPDATE SET value_json = excluded.value_json, version = excluded.version, updated_at_ms = excluded.updated_at_ms",
        &params![namespace, key, encoded, actual + 1, now],
    )?;
    Ok(())
}

fn put_map(session: &Session<'_>, namespace: &str, key: &str, value: &Map<String, Value>) -> OpResult<()> {
    put(session, namespace, key, &Value::Object(value.clone()))
}

/// `_validated(value, definition)`.
fn validated(value: &Value, definition: &str) -> OpResult<()> {
    validate(value, definition).or_else(|error| fail("database_protocol_error", error.0))
}

fn owner(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

/// `_document_key(owner, document_id)`.
fn document_key(owner: i64, document_id: &Value) -> OpResult<String> {
    match document_id {
        Value::String(id) if id.len() == 64 && id.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) => {
            Ok(format!("{owner}:{id}"))
        }
        _ => fail("database_protocol_error", "Invalid logical paper document identity"),
    }
}

fn field<'v>(value: &'v Map<String, Value>, name: &str) -> OpResult<&'v Value> {
    match value.get(name) {
        Some(item) => Ok(item),
        None => internal(format!("KeyError: {name}")),
    }
}

/// `value[name]` of a JSON value that must be a mapping.
fn item<'v>(value: &'v Value, name: &str) -> OpResult<&'v Value> {
    match value {
        Value::Object(map) => field(map, name),
        _ => internal(format!("TypeError: {name}")),
    }
}

fn number(value: &Value) -> OpResult<f64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64 as f64),
        Value::Number(number) => Ok(number.as_f64().unwrap_or(0.0)),
        _ => internal("TypeError: '<' not supported"),
    }
}

fn increment(value: &Value) -> OpResult<Value> {
    match value {
        Value::Bool(flag) => Ok(Value::from(*flag as i64 + 1)),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => Ok(Value::from(integer + 1)),
            None => Ok(crate::json::float_value(number.as_f64().unwrap_or(0.0) + 1.0)),
        },
        _ => internal("TypeError: unsupported operand"),
    }
}

/// `_document(session, owner, document_id, lock=...)`.
fn document(
    session: &Session<'_>,
    owner: i64,
    document_id: &Value,
    lock: bool,
) -> OpResult<(String, Map<String, Value>)> {
    let key = document_key(owner, document_id)?;
    if lock {
        session.lock_key(DOCUMENT, &key);
    }
    match get(session, DOCUMENT, &key)? {
        Value::Object(row) => Ok((key, row)),
        _ => fail("database_not_found", "Paper document is unavailable"),
    }
}

/// `_projection(session, owner, row)`.
fn projection(session: &Session<'_>, owner: i64, row: &Map<String, Value>) -> OpResult<Value> {
    let receipt = latest_retained_receipt(session, owner, field(row, "document_id")?)?;
    let mut status = field(row, "status")?.clone();
    if status == "pending" && number(field(row, "request_expires_at_ms")?)? <= now_ms() as f64 {
        status = json!("interrupted");
    }
    Ok(json!({"document_id": field(row, "document_id")?, "source": field(row, "source")?,
        "generation": field(row, "generation")?, "status": status, "receipt": receipt,
        "reading": field(row, "reading")?, "reading_revision": field(row, "reading_revision")?}))
}

/// `secrets.token_hex(32)`.
fn token_hex_32() -> OpResult<String> {
    let mut bytes = [0u8; 32];
    match std::fs::File::open("/dev/urandom").and_then(|mut file| file.read_exact(&mut bytes)) {
        Ok(()) => Ok(hex::encode(bytes)),
        Err(error) => internal(format!("urandom: {error}")),
    }
}

fn binding_key(source: &Value) -> OpResult<String> {
    // `source_binding_key` raises an uncaught BuildError: an internal failure.
    source_binding_key(source).or_else(|error| internal(error.0))
}

fn paper_document_register(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let offered = payload_get(payload, "source");
    validated(offered, "PaperStoredSourceV1")?;
    let mut source = offered.as_object().cloned().unwrap_or_default();
    if source.get("source").is_some_and(|kind| kind == "research") {
        source = research_source(session, owner, source)?;
    }
    let supplied = source.shift_remove("document_id").unwrap_or(Value::Null);
    let binding = binding_key(&Value::Object(source.clone()))?;
    let source_key = format!("{owner}:{binding}");
    session.lock_key(BUDGET, "capacity");
    let existing = get(session, SOURCE, &source_key)?;
    if truthy(&existing) {
        let existing_id = item(&existing, "document_id")?;
        if truthy(&supplied) && !py_eq(existing_id, &supplied) {
            return fail("database_conflict", "Paper document does not match this source binding");
        }
        return Ok(Value::Object(document(session, owner, existing_id, false)?.1));
    }
    let research = field(&source, "source")? == "research";
    if truthy(&supplied) && !research {
        return fail("database_conflict", "Paper document source binding changed; select the source again");
    }
    let budget = match get(session, BUDGET, "capacity")? {
        Value::Object(budget) if !budget.is_empty() => budget,
        _ => json!({"count": 0}).as_object().cloned().expect("object"),
    };
    let document_id = if research { supplied } else { Value::String(token_hex_32()?) };
    // An existing retained Research ID cannot capture another source's catalog row.
    if !get(session, DOCUMENT, &document_key(owner, &document_id)?)?.is_null() {
        return fail("database_conflict", "Paper document already belongs to another source");
    }
    source.insert("document_id".into(), document_id.clone());
    let status = if research { "ready" } else { "idle" };
    let row = json!({"document_id": document_id, "source": source, "generation": 0, "request_key": "",
        "request_expires_at_ms": 0, "status": status,
        "reading": null, "reading_revision": 0,
        "completed_build_id": null, "completed_receipt_digest": null, "completed_published": false});
    put(session, DOCUMENT, &document_key(owner, &document_id)?, &row)?;
    put(session, SOURCE, &source_key, &json!({"document_id": document_id}))?;
    let mut next_budget = budget.clone();
    next_budget.insert("count".into(), increment(field(&budget, "count")?)?);
    put_map(session, BUDGET, "capacity", &next_budget)?;
    Ok(row)
}

/// `_research_source`: only an owned workspace's retained publication can
/// bind its existing ID.
fn research_source(session: &Session<'_>, owner: i64, source: Map<String, Value>) -> OpResult<Map<String, Value>> {
    let mut lookup = Payload::new();
    lookup.insert("user_id".into(), Value::from(owner));
    lookup.insert("paper_hash".into(), field(&source, "paper_hash")?.clone());
    lookup.insert("lang".into(), field(&source, "lang")?.clone());
    let workspace = research_workspace_get(session, &lookup)?;
    let document = if truthy(&workspace) { item(&workspace, "workspace")?.clone() } else { Value::Null };
    let compilation =
        if truthy(&document) { document.get("compilation").cloned().unwrap_or(Value::Null) } else { Value::Null };
    let reference = match &compilation {
        Value::Object(map) => map.get("build").cloned().unwrap_or(Value::Null),
        value if !truthy(value) => Value::Null,
        _ => return internal("AttributeError: get"),
    };
    let Value::Object(reference) = reference else {
        return fail("database_not_found", "Research manuscript is unavailable");
    };
    let null = Value::Null;
    let reference_id = reference.get("document_id").unwrap_or(&null);
    if let Some(offered) = source.get("document_id").filter(|value| truthy(value)) {
        if !py_eq(offered, reference_id) {
            return fail("database_conflict", "Research manuscript identity changed");
        }
    }
    let receipt = read_retained_receipt(session, owner, reference_id, reference.get("build_id").unwrap_or(&null))?;
    let main = item(&receipt, "main_file")?.clone();
    let mut result = source;
    result.insert("document_id".into(), item(&receipt, "document_id")?.clone());
    result.insert("main_file".into(), main.clone());
    result.insert("label".into(), main);
    Ok(result)
}

fn require_build_authority(row: &Map<String, Value>) -> OpResult<()> {
    if item(field(row, "source")?, "source")? == "research" {
        return fail("database_conflict", "Research workspace owns manuscript publication");
    }
    Ok(())
}

fn paper_document_resolve(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let Value::Array(sources) = payload_get(payload, "sources") else {
        return fail("database_protocol_error", "Paper source lookup requires an array");
    };
    let mut result = Vec::with_capacity(sources.len());
    for source in sources {
        validated(source, "PaperDocumentV1")?;
        let row = get(session, SOURCE, &format!("{owner}:{}", binding_key(source)?))?;
        result.push(if truthy(&row) { item(&row, "document_id")?.clone() } else { Value::Null });
    }
    Ok(Value::Array(result))
}

fn paper_document_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let (_, row) = document(session, owner, payload_get(payload, "document_id"), false)?;
    projection(session, owner, &row)
}

fn paper_document_current(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let conversation_id = required_text(payload, "conversation_id", 160)?;
    let pointer = get(session, CURRENT, &format!("{owner}:{conversation_id}"))?;
    if pointer.is_null() {
        return Ok(Value::Null);
    }
    let (_, row) = document(session, owner, item(&pointer, "document_id")?, false)?;
    projection(session, owner, &row)
}

fn paper_document_request(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let conversation_id = required_text(payload, "conversation_id", 160)?;
    let request_key = required_text(payload, "request_key", 64)?;
    session.lock_key(BUDGET, "capacity");
    let current_key = format!("{owner}:{conversation_id}");
    if get(session, CURRENT, &current_key)?.is_null() {
        let mut budget = match get(session, BUDGET, "capacity")? {
            Value::Object(budget) if !budget.is_empty() => budget,
            _ => json!({"count": 0}).as_object().cloned().expect("object"),
        };
        let associations = increment(budget.get("associations").unwrap_or(&Value::from(0i64)))?;
        budget.insert("associations".into(), associations);
        put_map(session, BUDGET, "capacity", &budget)?;
    }
    let (key, mut row) = document(session, owner, payload_get(payload, "document_id"), true)?;
    require_build_authority(&row)?;
    if field(&row, "status")? != "pending"
        || field(&row, "request_key")? != &Value::String(request_key.clone())
        || number(field(&row, "request_expires_at_ms")?)? <= now_ms() as f64
    {
        let generation = increment(field(&row, "generation")?)?;
        row.insert("generation".into(), generation);
        row.insert("request_key".into(), Value::String(request_key));
        row.insert("status".into(), json!("pending"));
        row.insert("request_expires_at_ms".into(), Value::from(now_ms() + limit("document_request_seconds") * 1000));
        row.insert("completed_build_id".into(), Value::Null);
        row.insert("completed_receipt_digest".into(), Value::Null);
        row.insert("completed_published".into(), Value::Bool(false));
        put_map(session, DOCUMENT, &key, &row)?;
    }
    put(session, CURRENT, &current_key, &json!({"document_id": field(&row, "document_id")?}))?;
    Ok(json!({"generation": field(&row, "generation")?, "request_key": field(&row, "request_key")?}))
}

fn paper_document_finish(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let generation = integer(payload, "generation", None, Some(1), None)?;
    let receipt = payload_get(payload, "receipt").clone();
    validated(&receipt, "PaperBuildReceiptV1")?;
    let (key, mut row) = document(session, owner, payload_get(payload, "document_id"), true)?;
    require_build_authority(&row)?;
    let row_document = field(&row, "document_id")?.clone();
    if !py_eq(&receipt["document_id"], &row_document) || !py_eq(&receipt["owner_user_id"], &Value::from(owner)) {
        return fail("database_protocol_error", "Build receipt belongs to a different paper document");
    }
    if !py_eq(&Value::from(generation), field(&row, "generation")?)
        || field(&row, "status")? == "interrupted"
        || number(field(&row, "request_expires_at_ms")?)? <= now_ms() as f64
    {
        return Ok(json!({"current": false, "published": false}));
    }
    let digest = sha256_hex(python_json_dumps(&receipt, true).as_bytes());
    let completed = field(&row, "completed_build_id")?.clone();
    if !completed.is_null() {
        if !py_eq(&completed, &receipt["build_id"]) {
            return Ok(json!({"current": false, "published": false}));
        }
        if field(&row, "completed_receipt_digest")? != &Value::String(digest) {
            return fail("database_conflict", "Completed paper build receipt changed");
        }
        return Ok(json!({"current": true, "published": field(&row, "completed_published")?}));
    }
    let published;
    if receipt["status"] == "success" {
        let proof = payload_get(payload, "proof");
        let null = Value::Null;
        let valid = match proof {
            Value::Object(proof) => {
                py_eq(proof.get("document_id").unwrap_or(&null), &row_document)
                    && py_eq(proof.get("build_id").unwrap_or(&null), &receipt["build_id"])
            }
            _ => false,
        };
        if !valid {
            return fail("database_protocol_error", "A successful paper build requires retained artifact evidence");
        }
        published = retain_build(session, owner, proof)?;
        // Compare all receipt fields, not just the caller-supplied build ID.
        if published && !py_eq(&read_retained_receipt(session, owner, &row_document, &receipt["build_id"])?, &receipt) {
            return fail("database_conflict", "Retained receipt differs from the completed build");
        }
        row.insert("status".into(), json!("ready"));
    } else {
        published = false;
        row.insert("status".into(), json!("failed"));
    }
    row.insert("completed_build_id".into(), receipt["build_id"].clone());
    row.insert("completed_receipt_digest".into(), Value::String(digest));
    row.insert("completed_published".into(), Value::Bool(published));
    put_map(session, DOCUMENT, &key, &row)?;
    Ok(json!({"current": true, "published": published}))
}

fn paper_document_abandon(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let generation = integer(payload, "generation", None, Some(1), None)?;
    let (key, mut row) = document(session, owner, payload_get(payload, "document_id"), true)?;
    require_build_authority(&row)?;
    if py_eq(&Value::from(generation), field(&row, "generation")?) && field(&row, "status")? == "pending" {
        row.insert("status".into(), json!("interrupted"));
        put_map(session, DOCUMENT, &key, &row)?;
    }
    Ok(json!({"abandoned": field(&row, "status")? == "interrupted"}))
}

fn paper_document_reading(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = owner(payload)?;
    let expected = integer(payload, "expected_revision", None, Some(0), None)?;
    let mut request = Map::new();
    for name in ["expected_revision", "state", "writer_id", "sequence"] {
        request.insert(name.to_owned(), payload_get(payload, name).clone());
    }
    let request = Value::Object(request);
    validated(&request, "PaperReadingStateRequestV1")?;
    let state = payload_get(payload, "state").clone();
    validated(&state, "PaperReadingStateV1")?;
    if python_json_dumps(&state, false).len() as i64 > limit("reading_state_bytes") {
        return fail("database_protocol_error", "Reading position exceeds its byte budget");
    }
    let (key, mut row) = document(session, owner, payload_get(payload, "document_id"), true)?;
    let null = Value::Null;
    let same_writer = py_eq(row.get("reading_writer_id").unwrap_or(&null), &request["writer_id"])
        && py_eq(row.get("reading_writer_base_revision").unwrap_or(&null), &Value::from(expected));
    if same_writer {
        let sequence = number(&request["sequence"])?;
        let writer_sequence = number(field(&row, "reading_writer_sequence")?)?;
        if sequence <= writer_sequence {
            if sequence == writer_sequence && !py_eq(&state, field(&row, "reading")?) {
                return fail("database_conflict", "Reading update sequence was reused with different content");
            }
            return Ok(json!({"revision": field(&row, "reading_revision")?, "state": field(&row, "reading")?}));
        }
    }
    if !same_writer && !py_eq(field(&row, "reading_revision")?, &Value::from(expected)) {
        return fail("database_conflict", "Paper reading position changed in another viewer");
    }
    read_retained_receipt(session, owner, field(&row, "document_id")?, &state["build_id"])?;
    let revision = increment(field(&row, "reading_revision")?)?;
    row.insert("reading".into(), state.clone());
    row.insert("reading_revision".into(), revision.clone());
    row.insert("reading_writer_id".into(), request["writer_id"].clone());
    row.insert("reading_writer_base_revision".into(), Value::from(expected));
    row.insert("reading_writer_sequence".into(), request["sequence"].clone());
    put_map(session, DOCUMENT, &key, &row)?;
    Ok(json!({"revision": revision, "state": state}))
}
