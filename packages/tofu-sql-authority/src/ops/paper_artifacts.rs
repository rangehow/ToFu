//! `operation_domains/paper_artifacts.py`: owner-scoped retained paper build
//! bytes, upload witnesses, read leases and document pins
//! (`operations_pkg/_paper_artifacts.py`).
//!
//! Records live in `storage_records` as insertion-ordered orjson TEXT
//! (`orjson.dumps(value).decode()`), exactly as the Python handlers write them.

pub mod contract;

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use self::contract::{b64decode_strict, b64encode, limit, sha256_hex, validate};
use super::{register as add, OperationSpec, Registry};
use crate::codec::py_eq;
use crate::error::{fail, internal, OpResult};
use crate::json::{dumps, truthy};
use crate::params;
use crate::payload::{integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const META: &str = "paper.build.artifact.v1";
const CHUNK: &str = "paper.build.chunk.v1";
const DOCUMENT: &str = "paper.build.document.v1";
const LEDGER: &str = "paper.build.budget.v1";
const NAMES: [&str; 4] = ["document.pdf", "document.synctex.gz", "sources.json", "build.log"];

pub fn register(registry: &mut Registry) {
    add(registry, "paper.artifact.prepare", OperationSpec::command(false, paper_artifact_prepare));
    add(registry, "paper.artifact.append", OperationSpec::command(false, paper_artifact_append));
    add(registry, "paper.artifact.seal", OperationSpec::command(false, paper_artifact_seal).with_timeout(30.0));
    add(registry, "paper.artifact.discard", OperationSpec::command(false, paper_artifact_discard));
    add(registry, "paper.artifact.lease", OperationSpec::command(false, paper_artifact_lease));
    add(registry, "paper.artifact.read", OperationSpec::query(paper_artifact_read));
    add(registry, "paper.artifact.latest", OperationSpec::query(paper_artifact_latest));
    add(registry, "paper.artifact.prune", OperationSpec::command(false, paper_artifact_prune));
}

/// `int(time.time() * 1000)`.
pub(crate) fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn is_hex(text: &str, minimum: usize, maximum: usize) -> bool {
    (minimum..=maximum).contains(&text.len()) && text.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

/// `_identity(payload)`.
fn identity(payload: &Payload) -> OpResult<(i64, String, String, String)> {
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    let document = required_text(payload, "document_id", 64)?;
    let build = required_text(payload, "build_id", 32)?;
    if !is_hex(&document, 32, 64) || !is_hex(&build, 32, 32) {
        return fail("database_protocol_error", "Invalid paper build identity");
    }
    let key = format!("{owner}:{document}:{build}");
    Ok((owner, document, build, key))
}

/// `_token(payload, name)`.
fn token(payload: &Payload, name: &str) -> OpResult<String> {
    let value = required_text(payload, name, 32)?;
    if !is_hex(&value, 32, 32) {
        return fail("database_protocol_error", "Invalid paper artifact witness");
    }
    Ok(value)
}

/// Python `obj[name]` on a stored document: a missing key is a KeyError.
fn field<'v>(value: &'v Map<String, Value>, name: &str) -> OpResult<&'v Value> {
    match value.get(name) {
        Some(item) => Ok(item),
        None => internal(format!("KeyError: {name}")),
    }
}

/// A Python number (bool included) for ordering comparisons.
fn number(value: &Value) -> OpResult<f64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64 as f64),
        Value::Number(number) => Ok(number.as_f64().unwrap_or(0.0)),
        _ => internal("TypeError: '<' not supported"),
    }
}

/// `int(value)` of a stored integral number.
fn int(value: &Value) -> OpResult<i64> {
    match value {
        Value::Bool(flag) => Ok(*flag as i64),
        Value::Number(number) => match number.as_i64() {
            Some(integer) => Ok(integer),
            None => Ok(number.as_f64().unwrap_or(0.0).trunc() as i64),
        },
        _ => internal("TypeError: int()"),
    }
}

fn object_mut<'v>(value: &'v mut Map<String, Value>, name: &str) -> OpResult<&'v mut Map<String, Value>> {
    match value.get_mut(name) {
        Some(Value::Object(map)) => Ok(map),
        Some(_) => internal(format!("TypeError: {name} is not a dict")),
        None => internal(format!("KeyError: {name}")),
    }
}

fn integrity<T>() -> OpResult<T> {
    fail("database_integrity", "Invalid retained paper artifact")
}

/// `_get(session, namespace, key)`.
fn get(session: &Session<'_>, namespace: &str, key: &str) -> OpResult<Option<Map<String, Value>>> {
    let row = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace = ? AND record_key = ?",
        &params![namespace, key],
    )?;
    let Some(row) = row else { return Ok(None) };
    let decoded = match row.get("value_json") {
        SqlValue::Text(text) => serde_json::from_str::<Value>(text),
        SqlValue::Blob(bytes) => serde_json::from_slice::<Value>(bytes),
        _ => return integrity(),
    };
    let Ok(Value::Object(document)) = decoded else { return integrity() };
    if namespace == CHUNK
        && (document.len() != 2
            || !document.contains_key("data")
            || !document.contains_key("sha256")
            || !document.values().all(Value::is_string))
    {
        return integrity();
    }
    Ok(Some(document))
}

/// `_put(session, namespace, key, value)`.
fn put(session: &Session<'_>, namespace: &str, key: &str, value: &Value) -> OpResult<()> {
    let encoded = String::from_utf8(dumps(value)).expect("orjson output is UTF-8");
    let changed = session.execute(
        "UPDATE storage_records SET value_json = ?, version = version + 1, updated_at_ms = ? WHERE namespace = ? AND record_key = ?",
        &params![&encoded, now_ms(), namespace, key],
    )?;
    if changed == 0 {
        session.execute(
            "INSERT INTO storage_records(namespace, record_key, value_json, version, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
            &params![namespace, key, encoded, 1i64, now_ms()],
        )?;
    }
    Ok(())
}

fn put_map(session: &Session<'_>, namespace: &str, key: &str, value: &Map<String, Value>) -> OpResult<()> {
    put(session, namespace, key, &Value::Object(value.clone()))
}

fn delete(session: &Session<'_>, namespace: &str, key: &str) -> OpResult<()> {
    session.execute("DELETE FROM storage_records WHERE namespace = ? AND record_key = ?", &params![namespace, key])?;
    Ok(())
}

fn lock(session: &Session<'_>) {
    session.lock_key(LEDGER, "capacity");
}

/// `_parts(receipt)`: `(name, offset, size)` per retained chunk.
fn parts(receipt: &Value) -> OpResult<Vec<(String, i64, i64)>> {
    let size = limit("retained_chunk_bytes");
    let Value::Array(artifacts) = &receipt["artifacts"] else { return internal("TypeError: artifacts") };
    let mut result = Vec::new();
    for artifact in artifacts {
        let total = int(&artifact["size_bytes"])?;
        let name = artifact["name"].as_str().unwrap_or_default().to_owned();
        let mut offset = 0;
        while offset < total {
            result.push((name.clone(), offset, size.min(total - offset)));
            offset += size;
        }
    }
    Ok(result)
}

/// `_metadata(session, key)`.
fn metadata(session: &Session<'_>, key: &str) -> OpResult<Map<String, Value>> {
    match get(session, META, key)? {
        Some(value) => Ok(value),
        None => fail("database_not_found", "Retained paper build is unavailable"),
    }
}

/// `_upload(session, payload)`.
fn upload(session: &Session<'_>, payload: &Payload) -> OpResult<(String, Map<String, Value>)> {
    let (.., key) = identity(payload)?;
    let witness = token(payload, "upload_id")?;
    let value = metadata(session, &key)?;
    if field(&value, "upload_id")? != &Value::String(witness) {
        return fail("database_conflict", "Paper artifact upload witness differs");
    }
    let state = field(&value, "state")?;
    if state == "reclaiming"
        || ((state == "uploading" || state == "sealed") && number(field(&value, "expires_at_ms")?)? <= now_ms() as f64)
    {
        return fail("database_conflict", "Paper artifact upload expired");
    }
    Ok((key, value))
}

/// `_readers(value)`: unexpired leases in stored order.
fn readers(value: &Map<String, Value>) -> OpResult<Map<String, Value>> {
    let now = now_ms() as f64;
    let Value::Object(readers) = field(value, "readers")? else { return internal("TypeError: readers") };
    let mut live = Map::new();
    for (key, expiry) in readers {
        if number(expiry)? > now {
            live.insert(key.clone(), expiry.clone());
        }
    }
    Ok(live)
}

fn add_numbers(left: &Value, right: &Value) -> Value {
    match (left.as_i64(), right.as_i64()) {
        (Some(a), Some(b)) => Value::from(a + b),
        _ => crate::json::float_value(left.as_f64().unwrap_or(0.0) + right.as_f64().unwrap_or(0.0)),
    }
}

fn paper_artifact_prepare(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner, document, build, key) = identity(payload)?;
    let upload_id = token(payload, "upload_id")?;
    let receipt = crate::payload::get(payload, "receipt").clone();
    if validate(&receipt, "PaperBuildReceiptV1").is_err() {
        return fail("database_protocol_error", "Invalid retained build receipt");
    }
    let artifacts = receipt["artifacts"].as_array().cloned().unwrap_or_default();
    if !py_eq(&receipt["owner_user_id"], &Value::from(owner))
        || receipt["document_id"] != Value::String(document)
        || receipt["build_id"] != Value::String(build)
        || receipt["status"] != "success"
        || artifacts.iter().any(|row| !NAMES.contains(&row["name"].as_str().unwrap_or_default()))
    {
        return fail("database_protocol_error", "Retained receipt does not match the successful build");
    }
    let receipt_bytes = dumps(&receipt).len() as i64;
    let mut payload_bytes = Value::from(0i64);
    for row in &artifacts {
        payload_bytes = add_numbers(&payload_bytes, &row["size_bytes"]);
    }
    if receipt_bytes > limit("retained_receipt_bytes") {
        return fail("database_protocol_error", "Retained paper build exceeds its byte limit");
    }
    lock(session);
    if get(session, META, &key)?.is_some() {
        let (_, existing) = upload(session, payload)?;
        if !py_eq(field(&existing, "receipt")?, &receipt) {
            return fail("database_conflict", "An immutable paper build cannot be replaced");
        }
        return Ok(json!({"next_sequence": field(&existing, "next_sequence")?, "state": field(&existing, "state")?}));
    }
    let mut ledger = match get(session, LEDGER, "capacity")? {
        Some(ledger) if !ledger.is_empty() => ledger,
        _ => json!({"records": {}}).as_object().cloned().expect("object"),
    };
    if !ledger.get("records").is_some_and(Value::is_object) {
        return fail("database_integrity", "Invalid retained paper artifact ledger");
    }
    let reserved = add_numbers(&payload_bytes, &Value::from(receipt_bytes));
    object_mut(&mut ledger, "records")?.insert(key.clone(), reserved);
    put_map(session, LEDGER, "capacity", &ledger)?;
    put(
        session,
        META,
        &key,
        &json!({"receipt": receipt, "upload_id": upload_id, "state": "uploading",
            "next_sequence": 0, "readers": {},
            "expires_at_ms": now_ms() + limit("retained_upload_seconds") * 1000}),
    )?;
    Ok(json!({"next_sequence": 0, "state": "uploading"}))
}

fn chunk_document(data: &[u8]) -> Value {
    json!({"data": b64encode(data), "sha256": sha256_hex(data)})
}

fn paper_artifact_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let sequence = integer(payload, "sequence", None, Some(0), None)?;
    let encoded = required_text(payload, "data", (4 * ((limit("retained_chunk_bytes") + 2) / 3)) as usize)?;
    let Some(data) = b64decode_strict(&encoded) else {
        return fail("database_protocol_error", "Invalid paper artifact chunk encoding");
    };
    lock(session);
    let (key, mut value) = upload(session, payload)?;
    let parts = parts(field(&value, "receipt")?)?;
    if sequence >= parts.len() as i64 || data.len() as i64 != parts[sequence as usize].2 {
        return fail("database_protocol_error", "Paper artifact chunk has an invalid position or length");
    }
    let chunk = chunk_document(&data);
    let next_sequence = int(field(&value, "next_sequence")?)?;
    if sequence < next_sequence {
        let committed = get(session, CHUNK, &format!("{key}:{sequence}"))?.map(Value::Object);
        if !committed.is_some_and(|committed| py_eq(&committed, &chunk)) {
            return fail("database_conflict", "Paper artifact chunk retry differs from its committed bytes");
        }
        return Ok(json!({"next_sequence": field(&value, "next_sequence")?}));
    }
    if sequence != next_sequence || field(&value, "state")? != "uploading" {
        return fail("database_conflict", "Paper artifact chunks must arrive in sequence");
    }
    put(session, CHUNK, &format!("{key}:{sequence}"), &chunk)?;
    value.insert("next_sequence".into(), Value::from(next_sequence + 1));
    put_map(session, META, &key, &value)?;
    Ok(json!({"next_sequence": next_sequence + 1}))
}

/// `_chunk_bytes(session, key, sequence)`.
fn chunk_bytes(session: &Session<'_>, key: &str, sequence: usize) -> OpResult<Vec<u8>> {
    let missing = || fail("database_integrity", "Retained paper artifact chunk is missing or corrupt");
    let Some(chunk) = get(session, CHUNK, &format!("{key}:{sequence}"))? else { return missing() };
    let Some(data) = chunk["data"].as_str().and_then(b64decode_strict) else { return missing() };
    if Value::String(sha256_hex(&data)) != chunk["sha256"] {
        return missing();
    }
    Ok(data)
}

fn paper_artifact_seal(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    lock(session);
    let (key, mut value) = upload(session, payload)?;
    if field(&value, "state")? != "uploading" {
        return Ok(json!({"sealed": true}));
    }
    let receipt = field(&value, "receipt")?.clone();
    let parts = parts(&receipt)?;
    if int(field(&value, "next_sequence")?)? != parts.len() as i64 {
        return fail("database_conflict", "Paper artifact upload is incomplete");
    }
    if !receipt.is_object() {
        return fail("database_conflict", "Paper artifact receipt is invalid");
    }
    let artifacts = receipt["artifacts"].as_array().cloned().unwrap_or_default();
    let mut hashes: Vec<(String, Sha256)> = Vec::new();
    for row in &artifacts {
        let name = row["name"].as_str().unwrap_or_default().to_owned();
        match hashes.iter_mut().find(|(existing, _)| *existing == name) {
            Some(slot) => slot.1 = Sha256::new(),
            None => hashes.push((name, Sha256::new())),
        }
    }
    for (sequence, (name, _, size)) in parts.iter().enumerate() {
        let data = chunk_bytes(session, &key, sequence)?;
        if data.len() as i64 != *size {
            return fail("database_integrity", "Retained artifact size changed");
        }
        if let Some((_, hasher)) = hashes.iter_mut().find(|(existing, _)| existing == name) {
            hasher.update(&data);
        }
    }
    let digests: Vec<(String, String)> =
        hashes.into_iter().map(|(name, hasher)| (name, hex::encode(hasher.finalize()))).collect();
    for row in &artifacts {
        let name = row["name"].as_str().unwrap_or_default();
        let digest = digests.iter().find(|(existing, _)| existing == name).map(|(_, digest)| digest.clone());
        if digest.map(Value::String).as_ref() != Some(&row["sha256"]) {
            return fail("database_integrity", "Retained artifact digest differs from the build receipt");
        }
    }
    value.insert("state".into(), json!("sealed"));
    put_map(session, META, &key, &value)?;
    Ok(json!({"sealed": true}))
}

/// `{**proof, 'user_id': user_id}`.
fn proof_payload(proof: &Map<String, Value>, user_id: i64) -> Payload {
    let mut payload = proof.clone();
    payload.insert("user_id".into(), Value::from(user_id));
    payload
}

/// `retain_research_build`: pin an exact verified receipt in the same
/// transaction as the research workspace CAS.
pub(crate) fn retain_research_build(
    session: &Session<'_>,
    user_id: i64,
    reference: &Value,
    proof: &Value,
) -> OpResult<bool> {
    let (Value::Object(reference), Value::Object(proof)) = (reference, proof) else {
        return fail("database_protocol_error", "A retained build requires its upload witness");
    };
    let payload = proof_payload(proof, user_id);
    let (_, document, build, _) = identity(&payload)?;
    lock(session);
    let (_, value) = upload(session, &payload)?;
    let receipt = field(&value, "receipt")?;
    let artifacts = receipt["artifacts"].as_array().map(Vec::as_slice).unwrap_or_default();
    let pdf = artifacts.iter().find(|row| row["name"] == "document.pdf");
    let state = field(&value, "state")?;
    let null = Value::Null;
    let reference_field = |name: &str| reference.get(name).unwrap_or(&null);
    let mismatch = !(state == "sealed" || state == "pinned")
        || !truthy(&receipt["version_verified"])
        || match pdf {
            None => true,
            Some(pdf) => {
                !py_eq(reference_field("document_id"), &Value::String(document.clone()))
                    || !py_eq(reference_field("build_id"), &Value::String(build.clone()))
                    || !py_eq(reference_field("manifest_digest"), &receipt["source_digest"])
                    || !py_eq(reference_field("pdf_sha256"), &pdf["sha256"])
            }
        };
    if mismatch {
        return fail("database_conflict", "Research reference differs from its retained verified build");
    }
    retain_build(session, user_id, &Value::Object(proof.clone()))
}

/// `read_retained_receipt`: owned immutable metadata of a published build.
pub(crate) fn read_retained_receipt(
    session: &Session<'_>,
    user_id: i64,
    document_id: &Value,
    build_id: &Value,
) -> OpResult<Value> {
    let mut payload = Payload::new();
    payload.insert("user_id".into(), Value::from(user_id));
    payload.insert("document_id".into(), document_id.clone());
    payload.insert("build_id".into(), build_id.clone());
    let (.., key) = identity(&payload)?;
    let value = metadata(session, &key)?;
    let state = field(&value, "state")?;
    if !(state == "pinned" || state == "retired") {
        return fail("database_not_found", "Retained paper build is unavailable");
    }
    Ok(field(&value, "receipt")?.clone())
}

/// `latest_retained_receipt`.
pub(crate) fn latest_retained_receipt(session: &Session<'_>, user_id: i64, document_id: &Value) -> OpResult<Value> {
    let mut payload = Payload::new();
    payload.insert("user_id".into(), Value::from(user_id));
    payload.insert("document_id".into(), document_id.clone());
    let pointer = paper_artifact_latest(session, &payload)?;
    if truthy(&pointer) {
        let build_id = match &pointer {
            Value::Object(map) => field(map, "build_id")?.clone(),
            _ => return internal("TypeError: pointer"),
        };
        read_retained_receipt(session, user_id, document_id, &build_id)
    } else {
        Ok(Value::Null)
    }
}

/// `retain_build`: publish an owned sealed build; an unverified result
/// cannot displace a verified one.
pub(crate) fn retain_build(session: &Session<'_>, user_id: i64, proof: &Value) -> OpResult<bool> {
    let Value::Object(proof) = proof else { return internal("TypeError: proof is not a mapping") };
    let payload = proof_payload(proof, user_id);
    let (owner, document, build, key) = identity(&payload)?;
    lock(session);
    let (_, mut value) = upload(session, &payload)?;
    let state = field(&value, "state")?;
    if !(state == "sealed" || state == "pinned") {
        return fail("database_conflict", "Only a sealed paper build can be retained");
    }
    let document_key = format!("{owner}:{document}");
    if let Some(prior) = get(session, DOCUMENT, &document_key)? {
        let prior_build = field(&prior, "build_id")?;
        if prior_build != &Value::String(build.clone()) {
            let prior_key = format!("{document_key}:{}", crate::json::python_str(prior_build));
            let mut previous = metadata(session, &prior_key)?;
            if truthy(&field(&previous, "receipt")?["version_verified"])
                && !truthy(&field(&value, "receipt")?["version_verified"])
            {
                return Ok(false);
            }
            previous.insert("state".into(), json!("retired"));
            previous.insert("expires_at_ms".into(), Value::from(now_ms() + limit("viewer_lease_seconds") * 1000));
            put_map(session, META, &prior_key, &previous)?;
        }
    }
    value.insert("state".into(), json!("pinned"));
    value.insert("expires_at_ms".into(), Value::from(0i64));
    put_map(session, META, &key, &value)?;
    put(session, DOCUMENT, &document_key, &json!({"build_id": build}))?;
    Ok(true)
}

fn paper_artifact_discard(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    lock(session);
    let (.., key) = identity(payload)?;
    let witness = token(payload, "upload_id")?;
    if let Some(mut value) = get(session, META, &key)? {
        if field(&value, "upload_id")? == &Value::String(witness) {
            let state = field(&value, "state")?;
            if state == "uploading" || state == "sealed" {
                value.insert("expires_at_ms".into(), Value::from(0i64));
                put_map(session, META, &key, &value)?;
            }
        }
    }
    // A lost workspace acknowledgement must never unpin its committed PDF.
    Ok(json!({"discarded": true}))
}

fn paper_artifact_lease(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (.., key) = identity(payload)?;
    let lease_id = token(payload, "lease_id")?;
    let operation = crate::payload::get(payload, "operation").as_str().unwrap_or_default().to_owned();
    if !matches!(operation.as_str(), "retain" | "renew" | "release")
        || !crate::payload::get(payload, "operation").is_string()
    {
        return fail("database_protocol_error", "Invalid retained artifact lease operation");
    }
    lock(session);
    let Some(mut value) = get(session, META, &key)? else {
        if operation == "release" {
            return Ok(json!({"retained": false}));
        }
        return fail("database_not_found", "Retained paper build is unavailable");
    };
    let mut live = readers(&value)?;
    if operation == "release" {
        live.shift_remove(&lease_id);
    } else {
        let state = field(&value, "state")?;
        if !(state == "pinned" || state == "retired")
            || (state == "retired" && number(field(&value, "expires_at_ms")?)? <= now_ms() as f64 && live.is_empty())
        {
            return fail("database_not_found", "Retained paper build is unavailable");
        }
        if operation == "renew" && !live.contains_key(&lease_id) {
            return fail("database_not_found", "Retained artifact reader expired");
        }
        live.insert(lease_id.clone(), Value::from(now_ms() + limit("viewer_lease_seconds") * 1000));
    }
    let retained = live.contains_key(&lease_id);
    value.insert("readers".into(), Value::Object(live));
    put_map(session, META, &key, &value)?;
    Ok(json!({"retained": retained, "receipt": field(&value, "receipt")?}))
}

fn paper_artifact_read(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (.., key) = identity(payload)?;
    let lease_id = token(payload, "lease_id")?;
    let sequence = integer(payload, "sequence", None, Some(0), None)?;
    let value = metadata(session, &key)?;
    if !readers(&value)?.contains_key(&lease_id) {
        return fail("database_not_found", "Retained artifact reader expired");
    }
    if sequence as f64 >= number(field(&value, "next_sequence")?)? {
        return fail("database_protocol_error", "Retained artifact sequence is out of bounds");
    }
    Ok(json!({"data": b64encode(&chunk_bytes(session, &key, sequence as usize)?)}))
}

fn paper_artifact_latest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    let document = required_text(payload, "document_id", 64)?;
    if !is_hex(&document, 32, 64) {
        return fail("database_protocol_error", "Invalid paper document identity");
    }
    Ok(get(session, DOCUMENT, &format!("{owner}:{document}"))?.map(Value::Object).unwrap_or(Value::Null))
}

fn paper_artifact_prune(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    lock(session);
    let mut ledger = match get(session, LEDGER, "capacity")? {
        Some(ledger) if !ledger.is_empty() => ledger,
        _ => json!({"records": {}}).as_object().cloned().expect("object"),
    };
    let cursor_key = format!("gc:{owner}");
    let cursor = match get(session, LEDGER, &cursor_key)? {
        Some(cursor) if !cursor.is_empty() => cursor,
        _ => json!({"key": "", "inclusive": false}).as_object().cloned().expect("object"),
    };
    if !cursor.get("inclusive").is_some_and(Value::is_boolean) {
        return fail("database_protocol_error", "Invalid retained artifact reclamation cursor");
    }
    let prefix = format!("{owner}:");
    let saved = cursor.get("key").cloned().unwrap_or(Value::String(String::new()));
    if truthy(&saved) && !saved.as_str().is_some_and(|saved| saved.starts_with(&prefix)) {
        return fail("database_protocol_error", "Invalid retained artifact reclamation cursor");
    }
    let inclusive = cursor["inclusive"] == Value::Bool(true);
    let mut keys: Vec<String> =
        field(&ledger, "records")?.as_object().map(|records| records.keys().cloned().collect()).unwrap_or_default();
    keys.sort();
    let mut rows = Vec::new();
    for key in keys {
        if !key.starts_with(&prefix) {
            continue;
        }
        let Some(saved) = saved.as_str() else { return internal("TypeError: '>' not supported") };
        if key.as_str() > saved || (inclusive && key == saved) {
            rows.push(key);
        }
    }
    let scan = limit("retained_gc_scan_rows") as usize;
    let page: Vec<String> = rows.into_iter().take(scan).collect();
    for key in &page {
        let mut value = metadata(session, key)?;
        if field(&value, "state")? == "pinned"
            || number(field(&value, "expires_at_ms")?)? > now_ms() as f64
            || !readers(&value)?.is_empty()
        {
            continue;
        }
        value.insert("state".into(), json!("reclaiming"));
        let begin = match value.get("gc_sequence") {
            Some(sequence) => int(sequence)?,
            None => 0,
        };
        let next_sequence = int(field(&value, "next_sequence")?)?;
        let end = next_sequence.min(begin + limit("retained_gc_chunks"));
        for sequence in begin..end {
            delete(session, CHUNK, &format!("{key}:{sequence}"))?;
        }
        if end == next_sequence {
            delete(session, META, key)?;
            object_mut(&mut ledger, "records")?.shift_remove(key);
            put_map(session, LEDGER, "capacity", &ledger)?;
        } else {
            value.insert("gc_sequence".into(), Value::from(end));
            put_map(session, META, key, &value)?;
        }
        put(session, LEDGER, &cursor_key, &json!({"key": key, "inclusive": end < next_sequence}))?;
        return Ok(json!({"progress": true, "chunks_deleted": end - begin}));
    }
    let full_page = page.len() == scan;
    if full_page {
        put(session, LEDGER, &cursor_key, &json!({"key": page.last().expect("full page"), "inclusive": false}))?;
    } else {
        delete(session, LEDGER, &cursor_key)?;
    }
    Ok(json!({"progress": full_page, "chunks_deleted": 0}))
}
