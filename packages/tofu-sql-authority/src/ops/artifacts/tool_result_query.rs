//! `operations_pkg/_tool_result_query.py` plus the manifest/ref validation of
//! `lib/tool_results/tool_result_query_contract.py`: owner-scoped streaming
//! DFS read/search over retained tool results with HMAC-signed route cursors.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig, URL_SAFE_NO_PAD};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::generated_unicode_casefold::{full_casefold_mapping, python_is_digit};
use super::pyjson;
use crate::codec::py_eq;
use crate::error::{internal, Failure, OpResult, StorageError};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{Row, Session};

const CONTRACT_JSON: &str = include_str!("../../../../../contracts/tool_result_query_v1.json");

pub fn contract() -> &'static Value {
    static CONTRACT: OnceLock<Value> = OnceLock::new();
    CONTRACT.get_or_init(|| serde_json::from_str(CONTRACT_JSON).expect("tool result query contract"))
}

pub fn limit(name: &str) -> i64 {
    contract()["limits"][name].as_i64().expect("contract limit")
}

pub fn media_type() -> &'static str {
    contract()["manifestMediaType"].as_str().expect("manifest media type")
}

fn cursor_policy(name: &str) -> &'static str {
    contract()["cursor"][name].as_str().expect("cursor policy")
}

const WINDOW: usize = 65536;

/// `invalid(message, kind=)`.
fn failure(kind: &str, message: &str) -> Failure {
    let entry = &contract()["failures"][kind];
    let code = entry["storageCode"].as_str().expect("failure code");
    StorageError::new(code, message).with_retryable(entry["retryable"].as_bool().unwrap_or(false)).into()
}

fn invalid_cursor() -> Failure {
    failure("invalid_cursor", "Invalid continuation cursor")
}

/// `_artifacts.py::_tool_result_digest`.
pub fn tool_result_digest(artifact_ref: &Value) -> OpResult<String> {
    let text = if truthy(artifact_ref) { python_str(artifact_ref) } else { String::new() };
    let digest = text.strip_prefix("tool-result:").unwrap_or(&text);
    if digest.chars().count() != 64 || !digest.chars().all(|c| "0123456789abcdef".contains(c)) {
        return Err(StorageError::protocol("Invalid tool artifact ref").into());
    }
    Ok(digest.to_owned())
}

fn is_canonical_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(index, byte)| match index {
            8 | 13 | 18 | 23 => *byte == b'-',
            _ => byte.is_ascii_digit() || (b'a'..=b'f').contains(byte),
        })
}

/// `tool_result_query_contract.validate_manifest`. A caught validation
/// failure is "Invalid result manifest"; an invalid artifact ref escapes
/// with its own message exactly like Python's `StorageError`.
pub fn validate_manifest(text: &str) -> OpResult<Value> {
    let invalid = || -> Failure { StorageError::protocol("Invalid result manifest").into() };
    let Ok(value) = serde_json::from_str::<Value>(text) else { return Err(invalid()) };
    let Value::Object(map) = &value else { return Err(invalid()) };
    let required: HashSet<&str> =
        contract()["manifestRequired"].as_array().expect("required").iter().filter_map(Value::as_str).collect();
    if map.len() != required.len() || !map.keys().all(|key| required.contains(key.as_str())) {
        return Err(invalid());
    }
    if !map["resultId"].as_str().is_some_and(is_canonical_uuid) {
        return Err(invalid());
    }
    let statuses = contract()["executionStatuses"].as_array().expect("statuses");
    if !statuses.iter().any(|status| py_eq(status, &map["executionStatus"])) {
        return Err(invalid());
    }
    for key in ["sourceCoverage", "retentionCoverage"] {
        if !matches!(
            map[key].as_str(),
            Some("complete" | "full" | "partial" | "incomplete" | "unavailable" | "unknown")
        ) {
            return Err(invalid());
        }
    }
    let (Value::Array(parts), Value::Array(children)) = (&map["parts"], &map["children"]) else {
        return Err(invalid());
    };
    if (parts.len() + children.len()) as i64 > limit("scanNodes") {
        return Err(invalid());
    }
    let optional: Vec<&str> = contract()["manifestOptionalPartFields"]
        .as_array()
        .expect("optional")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    for part in parts {
        let Value::Object(part) = part else { return Err(invalid()) };
        let expected: &[&str] = match part.get("kind").and_then(Value::as_str) {
            Some("text" | "json") => &["kind", "text"],
            Some("artifact") => &["kind", "artifactRef"],
            Some("media") => &["kind", "reference", "mimeType"],
            _ => &[],
        };
        if expected.is_empty()
            || !expected.iter().all(|key| part.contains_key(*key))
            || part.keys().any(|key| !expected.contains(&key.as_str()) && !optional.contains(&key.as_str()))
            || expected.iter().any(|key| !part[*key].is_string())
        {
            return Err(invalid());
        }
        for key in optional.iter().filter(|key| part.contains_key(**key)) {
            let item = &part[*key];
            if matches!(*key, "sourceOrdinal" | "byteSize" | "omittedCount") {
                if !item.as_i64().is_some_and(|value| value >= 0) && !item.is_u64() {
                    return Err(invalid());
                }
            } else if !item.as_str().is_some_and(|text| char_len(text) <= 2048) {
                return Err(invalid());
            }
            if *key == "sha256" {
                let text = item.as_str().unwrap_or("");
                if text.chars().count() != 64 || !text.chars().all(|c| "0123456789abcdef".contains(c)) {
                    return Err(invalid());
                }
            }
        }
        let kind = part["kind"].as_str().unwrap_or("");
        if kind == "json" && serde_json::from_str::<Value>(part["text"].as_str().unwrap_or("")).is_err() {
            return Err(invalid());
        }
        if kind == "artifact" {
            tool_result_digest(&part["artifactRef"])?;
        }
        if kind == "media" {
            let reference = part["reference"].as_str().unwrap_or("");
            let unavailable = part.get("status").is_some_and(|status| status == "unavailable");
            if (reference.is_empty() && !unavailable)
                || char_len(reference) > 2048
                || char_len(part["mimeType"].as_str().unwrap_or("")) > 128
            {
                return Err(invalid());
            }
        }
    }
    for child in children {
        let Value::Object(child) = child else { return Err(invalid()) };
        let keys: HashSet<&str> = child.keys().map(String::as_str).collect();
        if keys != HashSet::from(["name", "callId", "artifactRef"]) || child.values().any(|value| !value.is_string()) {
            return Err(invalid());
        }
        if char_len(child["name"].as_str().unwrap_or("")) > 200
            || char_len(child["callId"].as_str().unwrap_or("")) > 200
        {
            return Err(invalid());
        }
        tool_result_digest(&child["artifactRef"])?;
    }
    if text.len() as i64 > limit("manifestBytes") {
        return Err(invalid());
    }
    Ok(value)
}

/// Python `str.casefold`.
pub fn casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        match full_casefold_mapping(character) {
            Some(folded) => out.push_str(folded),
            None => out.push(character),
        }
    }
    out
}

fn hmac_sha256(key: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner = Sha256::new();
    inner.update(block.map(|byte| byte ^ 0x36));
    for part in parts {
        inner.update(part);
    }
    let mut outer = Sha256::new();
    outer.update(block.map(|byte| byte ^ 0x5c));
    outer.update(inner.finalize());
    outer.finalize().into()
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len() && left.iter().zip(right).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
}

/// `cursor_key`.
fn cursor_key(session: &Session<'_>) -> OpResult<Vec<u8>> {
    let row = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["tool_result_private", "cursor-key-v1"],
    )?;
    let Some(row) = row else {
        return Err(failure("artifact_query_unavailable", "Continuation key unavailable"));
    };
    let Some(raw) = row.get("value_json").bytes() else { return internal("value_json is not text") };
    let Ok(Value::String(hex_key)) = serde_json::from_slice::<Value>(raw) else {
        return internal("cursor key is not a JSON string");
    };
    match hex::decode(hex_key.split_whitespace().collect::<String>()) {
        Ok(key) => Ok(key),
        Err(_) => internal("bytes.fromhex"),
    }
}

/// `binding(payload, operation)`.
fn binding(payload: &Payload, operation: &str) -> OpResult<Value> {
    let Some(owner) = payload.get("user_id").and_then(Value::as_i64) else { return internal("int(user_id)") };
    let artifact_ref = python_str(payload.get("artifact_ref").unwrap_or(&Value::Null));
    let query = if operation == "search" {
        python_str(payload.get("query").unwrap_or(&Value::String(String::new())))
    } else {
        String::new()
    };
    let selector = python_str(payload.get("selector").unwrap_or(&Value::String(String::new())));
    Ok(json!([owner, artifact_ref, operation, query, selector]))
}

/// `_scoped_mac`.
fn scoped_mac(key: &[u8], payload: &Payload, operation: &str, data: &[u8], previous: bool) -> OpResult<[u8; 32]> {
    let domain = contract()["cursor"]["authentication"][if previous { "previousDomain" } else { "domain" }]
        .as_str()
        .expect("cursor domain");
    let bound = pyjson::dumps(&binding(payload, operation)?, false);
    Ok(hmac_sha256(key, &[domain.as_bytes(), b"\0", bound.as_bytes(), b"\0", data]))
}

/// `_decode_base64url`: canonical unpadded base64url only.
fn decode_base64url(text: &str) -> OpResult<Vec<u8>> {
    let lenient = GeneralPurpose::new(
        &base64::alphabet::URL_SAFE,
        GeneralPurposeConfig::new().with_decode_padding_mode(DecodePaddingMode::Indifferent),
    );
    let data = lenient.decode(text).map_err(|_| invalid_cursor())?;
    if URL_SAFE_NO_PAD.encode(&data) != text {
        return Err(invalid_cursor());
    }
    Ok(data)
}

/// `base64.urlsafe_b64decode(token + padding)` without validation: bytes
/// outside the alphabet are discarded before decoding.
fn legacy_base64url(text: &str) -> OpResult<Vec<u8>> {
    let filtered: String = text
        .chars()
        .map(|c| match c {
            '-' => '+',
            '_' => '/',
            other => other,
        })
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '='))
        .collect();
    let lenient = GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_padding_mode(DecodePaddingMode::Indifferent)
            .with_decode_allow_trailing_bits(true),
    );
    lenient.decode(filtered.trim_end_matches('=')).map_err(|_| invalid_cursor())
}

#[derive(Clone, Debug, PartialEq)]
struct State {
    r: Vec<u64>,
    p: u64,
    o: u64,
    /// Python int: a numeric cursor may exceed 64 bits (refused at encode).
    skip: u128,
    expiry: u64,
    partial: bool,
}

/// `_position_bytes`.
fn position_bytes(state: &State) -> OpResult<Vec<u8>> {
    let flag = contract()["cursor"]["positionEncoding"]["partialFlag"].as_u64().expect("partial flag") as u128;
    let mut values: Vec<u128> = vec![if state.partial { flag } else { 0 }, state.r.len() as u128];
    values.extend(state.r.iter().map(|value| *value as u128));
    values.extend([state.p as u128, state.o as u128, state.skip, state.expiry as u128]);
    let mut encoded = Vec::new();
    for mut value in values {
        if value > u64::MAX as u128 {
            return Err(invalid_cursor());
        }
        while value >= 128 {
            encoded.push(((value & 127) | 128) as u8);
            value >>= 7;
        }
        encoded.push(value as u8);
    }
    Ok(encoded)
}

/// `_position_from_bytes`.
fn position_from_bytes(data: &[u8]) -> OpResult<State> {
    let mut offset = 0usize;
    let mut integer = || -> OpResult<u64> {
        let mut value: u64 = 0;
        for shift in (0..64).step_by(7) {
            if offset >= data.len() {
                return Err(invalid_cursor());
            }
            let byte = data[offset];
            offset += 1;
            if shift == 63 && byte > 1 {
                return Err(invalid_cursor());
            }
            value |= ((byte & 127) as u64) << shift;
            if byte & 128 == 0 {
                if shift != 0 && byte == 0 {
                    return Err(invalid_cursor());
                }
                return Ok(value);
            }
        }
        Err(invalid_cursor())
    };
    let partial_flag = contract()["cursor"]["positionEncoding"]["partialFlag"].as_u64().expect("partial flag");
    let flags = integer()?;
    let count = integer()?;
    if flags & !partial_flag != 0 || count as i64 > limit("depth") + 1 || count > i64::MAX as u64 {
        return Err(invalid_cursor());
    }
    let mut r = Vec::new();
    for _ in 0..count {
        r.push(integer()?);
    }
    let p = integer()?;
    let o = integer()?;
    let skip = integer()?;
    let expiry = integer()?;
    drop(integer);
    if offset != data.len() {
        return Err(invalid_cursor());
    }
    Ok(State { r, p, o, skip: skip as u128, expiry, partial: flags & partial_flag != 0 })
}

/// `encode_cursor`.
fn encode_cursor(session: &Session<'_>, payload: &Payload, operation: &str, state: &State) -> OpResult<String> {
    let data = position_bytes(state)?;
    let signature = scoped_mac(&cursor_key(session)?, payload, operation, &data, false)?;
    let token = format!(
        "{}.{}.{}",
        cursor_policy("currentPrefix"),
        URL_SAFE_NO_PAD.encode(&data),
        URL_SAFE_NO_PAD.encode(signature)
    );
    if token.chars().count() as u64 > contract()["cursor"]["maxLength"].as_u64().expect("max length") {
        return Err(failure("artifact_query_unavailable", "Continuation metadata too large"));
    }
    Ok(token)
}

/// A JSON position (`trq2`/`trq1`) validated like `decode_cursor`.
fn json_state(value: &Value) -> OpResult<State> {
    let Value::Object(map) = value else { return Err(invalid_cursor()) };
    let keys: HashSet<&str> = map.keys().map(String::as_str).collect();
    if keys != HashSet::from(["r", "p", "o", "skip", "expiry", "partial"]) {
        return Err(invalid_cursor());
    }
    let Value::Array(route) = &map["r"] else { return Err(invalid_cursor()) };
    if route.len() as i64 > limit("depth") + 1 {
        return Err(invalid_cursor());
    }
    let mut r = Vec::new();
    for node in route {
        match node.as_u64() {
            Some(node) if (node as i64) < limit("scanNodes") && node < i64::MAX as u64 => r.push(node),
            _ => return Err(invalid_cursor()),
        }
    }
    let field = |key: &str| map[key].as_u64().ok_or_else(invalid_cursor);
    let (p, o, skip, expiry) = (field("p")?, field("o")?, field("skip")?, field("expiry")?);
    let Value::Bool(partial) = map["partial"] else { return Err(invalid_cursor()) };
    Ok(State { r, p, o, skip: skip as u128, expiry, partial })
}

/// `decode_cursor`.
fn decode_cursor(session: &Session<'_>, payload: &Payload, operation: &str) -> OpResult<State> {
    let maximum = contract()["cursor"]["maxLength"].as_u64().expect("max length") as usize;
    let value = match payload.get("cursor") {
        Some(value) => value.clone(),
        None => payload.get("offset").cloned().unwrap_or(Value::from(0)),
    };
    let raw = match &value {
        Value::Number(number) if number.is_u64() => number.to_string(),
        Value::String(text) if char_len(text) <= maximum => {
            if text.is_empty() {
                "0".to_owned()
            } else {
                text.clone()
            }
        }
        _ => return Err(invalid_cursor()),
    };
    if raw.chars().all(python_is_digit) {
        if !raw.chars().all(|c| c.is_ascii_digit()) {
            return internal("int() of a non-decimal digit string");
        }
        let skip = raw.parse::<u128>().unwrap_or(u128::MAX);
        if operation == "search" && skip != 0 {
            return Err(invalid_cursor());
        }
        return Ok(State { r: Vec::new(), p: 0, o: 0, skip, expiry: 0, partial: false });
    }
    let parts: Vec<&str> = raw.split('.').collect();
    if parts.len() != 3 {
        return Err(invalid_cursor());
    }
    let (prefix, token, signature) = (parts[0], parts[1], parts[2]);
    let state = if prefix == cursor_policy("currentPrefix") || prefix == cursor_policy("previousPrefix") {
        let previous = prefix == cursor_policy("previousPrefix");
        let data = decode_base64url(token)?;
        let supplied = decode_base64url(signature)?;
        let expected = scoped_mac(&cursor_key(session)?, payload, operation, &data, previous)?;
        if !constant_time_eq(&supplied, &expected) {
            return Err(invalid_cursor());
        }
        let state = if previous {
            json_state(&serde_json::from_slice::<Value>(&data).map_err(|_| invalid_cursor())?)?
        } else {
            position_from_bytes(&data)?
        };
        if state.r.len() as i64 > limit("depth") + 1 || state.r.iter().any(|n| *n as i64 >= limit("scanNodes")) {
            return Err(invalid_cursor());
        }
        state
    } else if prefix == cursor_policy("legacyPrefix") {
        let data = legacy_base64url(token)?;
        let expected = hex::encode(hmac_sha256(&cursor_key(session)?, &[&data]));
        if !signature.is_ascii() || !constant_time_eq(signature.as_bytes(), expected.as_bytes()) {
            return Err(invalid_cursor());
        }
        let decoded: Value = serde_json::from_slice(&data).map_err(|_| invalid_cursor())?;
        let Value::Array(pair) = decoded else { return Err(invalid_cursor()) };
        if pair.len() != 2 || !py_eq(&pair[0], &binding(payload, operation)?) {
            return Err(invalid_cursor());
        }
        json_state(&pair[1])?
    } else {
        return Err(invalid_cursor());
    };
    if state.expiry != 0 {
        let Some(now) = payload.get("now_ms").and_then(Value::as_i64) else { return Err(invalid_cursor()) };
        if state.expiry as i128 <= now as i128 {
            return Err(failure("continuation_unavailable", "Continuation evidence expired; start a new query"));
        }
    }
    Ok(state)
}

/// A `select_json` failure Python's query loop catches
/// (`ValueError`/`KeyError`/`IndexError`/`TypeError`).
enum Selected {
    Text(String),
    Unavailable,
}

/// `select_json`.
fn select_json(text: &str, selector: &Value) -> OpResult<Selected> {
    if !truthy(selector) {
        return Ok(Selected::Text(text.to_owned()));
    }
    let Value::String(selector) = selector else {
        return Err(failure("invalid_selector", "Invalid JSON pointer"));
    };
    if !selector.starts_with('/') || char_len(selector) > 1024 {
        return Err(failure("invalid_selector", "Invalid JSON pointer"));
    }
    let tokens: Vec<&str> = selector[1..].split('/').collect();
    if tokens.len() as i64 > limit("pointerDepth") {
        return Err(failure("invalid_selector", "JSON pointer depth exceeded"));
    }
    let Ok(mut value) = serde_json::from_str::<Value>(text) else { return Ok(Selected::Unavailable) };
    for token in tokens {
        let mut characters = token.chars().peekable();
        while let Some(character) = characters.next() {
            if character == '~' && !matches!(characters.peek(), Some('0' | '1')) {
                return Err(failure("invalid_selector", "Invalid JSON pointer escape"));
            }
        }
        let key = token.replace("~1", "/").replace("~0", "~");
        value = match value {
            Value::Array(mut items) => {
                if key.is_empty()
                    || !key.chars().all(python_is_digit)
                    || (key.chars().count() > 1 && key.starts_with('0'))
                {
                    return Ok(Selected::Unavailable);
                }
                let Ok(index) = key.parse::<usize>() else { return Ok(Selected::Unavailable) };
                if index >= items.len() {
                    return Ok(Selected::Unavailable);
                }
                items.swap_remove(index)
            }
            Value::Object(mut map) => match map.shift_remove(&key) {
                Some(item) => item,
                None => return Ok(Selected::Unavailable),
            },
            _ => return Ok(Selected::Unavailable),
        };
    }
    Ok(Selected::Text(pyjson::dumps(&value, false)))
}

type Node = Option<(Row, Option<Value>)>;

enum Resolved {
    Issue(&'static str),
    End,
    Raw,
    Leaf(String, Value),
}

struct Walk<'s, 'c> {
    session: &'s Session<'c>,
    owner: i64,
    now: i64,
    state: State,
    cache: HashMap<String, Node>,
    issues: Vec<Value>,
    nodes: i64,
    scanned: i64,
    finished: bool,
    root: String,
}

impl Walk<'_, '_> {
    fn issue(&mut self, path: &[Value], reason: &str) {
        if (self.issues.len() as i64) < limit("scanNodes") {
            self.issues.push(json!({"path": path, "reason": reason}));
        }
        self.state.partial = true;
    }

    fn load(&mut self, reference: &str) -> OpResult<Node> {
        if let Some(node) = self.cache.get(reference) {
            return Ok(node.clone());
        }
        let digest = tool_result_digest(&Value::String(reference.to_owned()))?;
        let row = self.session.fetch_one(
            "SELECT media_type,size_bytes,expires_at_ms FROM tool_result_artifacts WHERE user_id=? AND content_sha256=? AND expires_at_ms>?",
            &params![self.owner, &digest, self.now],
        )?;
        self.nodes += 1;
        let Some(row) = row else {
            self.cache.insert(reference.to_owned(), None);
            return Ok(None);
        };
        let expiry = row.int("expires_at_ms")? as u64;
        let current = if self.state.expiry != 0 { self.state.expiry } else { expiry };
        self.state.expiry = current.min(expiry);
        let mut manifest = None;
        if row.get("media_type").as_str() == Some(media_type()) {
            let body = self.session.fetch_one(
                "SELECT content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?",
                &params![self.owner, &digest],
            )?;
            let Some(body) = body else { return internal("TypeError: 'NoneType' object is not subscriptable") };
            let Some(content) = body.get("content").as_str() else { return internal("manifest content is not text") };
            manifest = Some(validate_manifest(content)?);
            self.scanned += row.int("size_bytes")?;
        }
        let node = Some((row, manifest));
        self.cache.insert(reference.to_owned(), node.clone());
        Ok(node)
    }

    /// `resolve()`: (segment, ref, path, frames).
    fn resolve(&mut self) -> OpResult<(Resolved, String, Vec<Value>, Vec<usize>)> {
        let mut reference = self.root.clone();
        let mut path: Vec<Value> = Vec::new();
        let mut frames: Vec<usize> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        let mut depth = 0usize;
        loop {
            if seen.contains(&reference) {
                return Ok((Resolved::Issue("cycle"), reference, path, frames));
            }
            if depth as i64 > limit("depth") {
                return Ok((Resolved::Issue("depth_budget"), reference, path, frames));
            }
            seen.insert(reference.clone());
            let Some((_row, manifest)) = self.load(&reference)? else {
                return Ok((Resolved::Issue("unavailable"), reference, path, frames));
            };
            if let (Some(manifest), Some(Value::Object(last))) = (&manifest, path.last_mut()) {
                last.insert("resultId".into(), manifest["resultId"].clone());
                last.insert("executionStatus".into(), manifest["executionStatus"].clone());
            }
            let Some(manifest) = manifest else {
                if self.state.r.len() != depth {
                    return Err(failure("continuation_unavailable", "Continuation route unavailable"));
                }
                return Ok((Resolved::Raw, reference, path, frames));
            };
            for key in ["sourceCoverage", "retentionCoverage"] {
                if !matches!(manifest[key].as_str(), Some("complete" | "full")) {
                    self.issue(&path, key);
                }
            }
            let parts = manifest["parts"].as_array().expect("validated parts");
            let children = manifest["children"].as_array().expect("validated children");
            let total = parts.len() + children.len();
            if self.state.r.len() <= depth {
                self.state.r.push(0);
            }
            let index = self.state.r[depth];
            if index >= total as u64 {
                return Ok((Resolved::End, reference, path, frames));
            }
            let index = index as usize;
            frames.push(total);
            let mut child_path = path.clone();
            if index < parts.len() {
                let item = &parts[index];
                child_path.push(Value::String(format!("/parts/{index}")));
                let kind = item["kind"].as_str().unwrap_or("").to_owned();
                if kind != "artifact" {
                    if self.state.r.len() != depth + 1 {
                        return Err(failure("continuation_unavailable", "Continuation route unavailable"));
                    }
                    return Ok((Resolved::Leaf(kind, item.clone()), reference, child_path, frames));
                }
                reference = item["artifactRef"].as_str().unwrap_or("").to_owned();
            } else {
                let item = &children[index - parts.len()];
                child_path.push(json!({"name": item["name"], "callId": item["callId"]}));
                reference = item["artifactRef"].as_str().unwrap_or("").to_owned();
            }
            path = child_path;
            depth += 1;
        }
    }

    /// `advance(frames)`.
    fn advance(&mut self, mut frames: Vec<usize>) {
        self.state.p = 0;
        while let Some(size) = frames.pop() {
            self.state.r.truncate(frames.len() + 1);
            if let Some(last) = self.state.r.last_mut() {
                *last += 1;
                if *last < size as u64 {
                    return;
                }
            }
        }
        self.finished = true;
        self.state.r.clear();
    }
}

fn char_slice(characters: &[char], start: usize, end: usize) -> String {
    let start = start.min(characters.len());
    let end = end.min(characters.len()).max(start);
    characters[start..end].iter().collect()
}

/// `query_result(session, payload, operation)`.
pub fn query_result(session: &Session<'_>, payload: &Payload, operation: &str) -> OpResult<Value> {
    let read = operation == "read";
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    let now = integer(payload, "now_ms", None, Some(1), None)?;
    let root = required_text(payload, "artifact_ref", 96)?;
    tool_result_digest(&Value::String(root.clone()))?;
    let needle: Vec<char> =
        if read { Vec::new() } else { casefold(&required_text(payload, "query", 200)?).chars().collect() };
    let state = decode_cursor(session, payload, operation)?;
    let initial = state.clone();
    let limit_value = if read {
        integer(payload, "limit", Some(8192), Some(1), Some(65536))?
    } else {
        integer(payload, "limit", Some(8), Some(1), Some(20))?
    } as usize;
    let deadline = Instant::now() + Duration::from_secs(limit("scanSeconds") as u64);
    let mut walk = Walk {
        session,
        owner,
        now,
        state,
        cache: HashMap::new(),
        issues: Vec::new(),
        nodes: 0,
        scanned: 0,
        finished: false,
        root: root.clone(),
    };
    let mut media: Vec<Value> = Vec::new();
    let mut content: Vec<String> = Vec::new();
    let mut matches: Vec<Value> = Vec::new();
    let mut provenance: Vec<Value> = Vec::new();
    let mut emitted = 0usize;

    let Some((_, first_manifest)) = walk.load(&root)? else {
        if initial.expiry != 0 {
            return Err(failure("continuation_unavailable", "Continuation root unavailable"));
        }
        return Ok(Value::Null);
    };
    let mut metadata = Map::new();
    if let Some(manifest) = &first_manifest {
        for key in ["resultId", "executionStatus", "sourceCoverage", "retentionCoverage"] {
            metadata.insert(key.into(), manifest[key].clone());
        }
    }
    let selector = payload.get("selector").cloned().unwrap_or(Value::Null);

    while !walk.finished {
        if walk.nodes >= limit("scanNodes") || walk.scanned >= limit("scanBytes") || Instant::now() >= deadline {
            break;
        }
        let (segment, reference, path, frames) = walk.resolve()?;
        let leaf = match segment {
            Resolved::Issue(reason) => {
                if reason == "unavailable" && initial.expiry != 0 {
                    return Err(failure(
                        "continuation_unavailable",
                        "Continuation child unavailable; start a new query",
                    ));
                }
                walk.issue(&path, reason);
                walk.advance(frames);
                continue;
            }
            Resolved::End => {
                walk.advance(frames);
                continue;
            }
            Resolved::Leaf(kind, item) if kind == "media" => {
                if read {
                    let mut descriptor = item.as_object().cloned().unwrap_or_default();
                    descriptor.insert("artifactRef".into(), Value::String(reference.clone()));
                    descriptor.insert("path".into(), Value::Array(path.clone()));
                    media.push(Value::Object(descriptor));
                }
                walk.issue(&path, "non_text_media");
                walk.advance(frames);
                continue;
            }
            Resolved::Leaf(kind, item) => Some((kind, item)),
            Resolved::Raw => None,
        };
        let pos = walk.state.p as usize;
        let (piece, eof): (Vec<char>, bool) = match leaf {
            None => {
                let digest = tool_result_digest(&Value::String(reference.clone()))?;
                if truthy(&selector) {
                    let body = session.fetch_one(
                        "SELECT content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?",
                        &params![owner, &digest],
                    )?;
                    let Some(text) = body.as_ref().and_then(|row| row.get("content").as_str().map(str::to_owned))
                    else {
                        return internal("raw content is not text");
                    };
                    match select_json(&text, &selector)? {
                        Selected::Text(text) => {
                            let characters: Vec<char> = text.chars().collect();
                            let piece: Vec<char> = characters.iter().skip(pos).take(WINDOW).copied().collect();
                            let eof = pos + piece.len() >= characters.len();
                            (piece, eof)
                        }
                        Selected::Unavailable => {
                            walk.issue(&path, "selector_unavailable");
                            walk.advance(frames);
                            continue;
                        }
                    }
                } else {
                    let body = session.fetch_one(
                        "SELECT substr(content,?,?) AS content FROM tool_result_artifacts WHERE user_id=? AND content_sha256=?",
                        &params![(pos + 1) as i64, (WINDOW + 1) as i64, owner, &digest],
                    )?;
                    let Some(text) = body.as_ref().and_then(|row| row.get("content").as_str().map(str::to_owned))
                    else {
                        return internal("raw content is not text");
                    };
                    let characters: Vec<char> = text.chars().collect();
                    let eof = characters.len() <= WINDOW;
                    (characters.into_iter().take(WINDOW).collect(), eof)
                }
            }
            Some((kind, item)) => {
                if truthy(&selector) && kind != "json" {
                    walk.advance(frames);
                    continue;
                }
                match select_json(item["text"].as_str().unwrap_or(""), &selector)? {
                    Selected::Text(text) => {
                        let characters: Vec<char> = text.chars().collect();
                        let piece: Vec<char> = characters.iter().skip(pos).take(WINDOW).copied().collect();
                        let eof = pos + piece.len() >= characters.len();
                        (piece, eof)
                    }
                    Selected::Unavailable => {
                        walk.issue(&path, "selector_unavailable");
                        walk.advance(frames);
                        continue;
                    }
                }
            }
        };
        if piece.is_empty() {
            walk.advance(frames);
            continue;
        }
        let piece_text: String = piece.iter().collect();
        walk.scanned += piece_text.len() as i64;
        if read {
            let encoded = piece_text.as_bytes();
            let mut skip = walk.state.skip.min(encoded.len() as u128) as usize;
            while skip < encoded.len() && encoded[skip] & 0xC0 == 0x80 {
                skip += 1;
            }
            walk.state.skip = walk.state.skip.saturating_sub(skip as u128);
            let end = (skip + limit_value.saturating_sub(emitted)).min(encoded.len());
            let window = &encoded[skip..end];
            let valid = match std::str::from_utf8(window) {
                Ok(text) => text.len(),
                Err(error) => error.valid_up_to(),
            };
            let mut selected = std::str::from_utf8(&window[..valid]).expect("valid prefix").to_owned();
            if selected.is_empty() && skip < encoded.len() && emitted == 0 {
                selected = piece_text[skip..].chars().next().map(String::from).unwrap_or_default();
            }
            let consumed = skip + selected.len();
            if consumed == 0 && emitted != 0 {
                break;
            }
            let chars = piece_text[..consumed].chars().count();
            if !selected.is_empty() {
                provenance.push(json!({
                    "artifactRef": reference,
                    "path": path,
                    "segment": walk.state.r,
                    "start": walk.state.o + skip as u64,
                    "end": walk.state.o + consumed as u64,
                }));
                emitted += selected.len();
                content.push(selected);
            }
            walk.state.p += chars as u64;
            walk.state.o += consumed as u64;
            if chars == piece.len() && eof {
                walk.advance(frames);
            }
            if emitted >= limit_value {
                break;
            }
        } else {
            let mut window: VecDeque<char> = VecDeque::new();
            let mut origins: VecDeque<usize> = VecDeque::new();
            let mut consumed = 0usize;
            let mut last_match_start: i64 = -1;
            for (index, character) in piece.iter().enumerate() {
                if index != 0 && index % 4096 == 0 && Instant::now() >= deadline {
                    break;
                }
                let mut folded_buffer = [0u8; 4];
                let folded = match full_casefold_mapping(*character) {
                    Some(folded) => folded,
                    None => character.encode_utf8(&mut folded_buffer),
                };
                for folded in folded.chars() {
                    window.push_back(folded);
                    origins.push_back(index);
                    while window.len() > needle.len() {
                        window.pop_front();
                        origins.pop_front();
                    }
                    if window.iter().eq(needle.iter()) {
                        let origin = origins[0];
                        let found = walk.state.o + origin as u64;
                        if matches.last().is_none_or(|item: &Value| item["offset"].as_u64() != Some(found)) {
                            matches.push(json!({
                                "offset": found,
                                "text": char_slice(&piece, origin.saturating_sub(160), index + 321),
                                "artifactRef": reference,
                                "path": path,
                            }));
                            last_match_start = origin as i64;
                            if matches.len() >= limit_value {
                                consumed = origin + 1;
                                break;
                            }
                        }
                    }
                }
                if matches.len() >= limit_value {
                    break;
                }
                consumed = index + 1;
            }
            if matches.len() < limit_value && (!eof || consumed < piece.len()) {
                consumed = (consumed as i64 - needle.len() as i64).max(1).max(last_match_start + 1) as usize;
            }
            walk.state.p += consumed as u64;
            walk.state.o += consumed as u64;
            if consumed == piece.len() && eof {
                walk.advance(frames);
            }
            if matches.len() >= limit_value {
                break;
            }
        }
    }
    let finished = walk.finished;
    let state = &walk.state;
    if !finished && state.r == initial.r && state.p == initial.p && state.o == initial.o && state.skip == initial.skip {
        return Err(failure("query_budget_exhausted", "Query budget too small to make progress"));
    }
    let mut result = Map::new();
    result.insert("artifactRef".into(), Value::String(root));
    result.extend(metadata);
    result.insert("scope".into(), Value::String(if read { "text_and_media_descriptors" } else { "text_only" }.into()));
    result.insert(
        "coverage".into(),
        Value::String(if state.partial || !finished { "incomplete" } else { "complete" }.into()),
    );
    result.insert("unavailable".into(), Value::Array(walk.issues.clone()));
    result.insert("nextCursor".into(), Value::Null);
    result.insert("truncated".into(), Value::Bool(!finished));
    if read {
        result.insert("content".into(), Value::String(content.concat()));
        result.insert("offset".into(), Value::from(initial.o));
        result.insert("provenance".into(), Value::Array(provenance));
        result.insert("media".into(), Value::Array(media));
        if finished {
            result.insert("sizeBytes".into(), Value::from(state.o));
        }
    } else {
        result.insert("query".into(), payload.get("query").cloned().unwrap_or(Value::Null));
        result.insert("items".into(), Value::Array(matches));
    }
    if !finished {
        if walk.issues.iter().any(|item| item["reason"] == "unavailable") {
            return Err(failure("continuation_unavailable", "Continuation child unavailable; start a new query"));
        }
        result.insert("nextCursor".into(), Value::String(encode_cursor(session, payload, operation, &walk.state)?));
    }
    result.insert("resumeCursor".into(), Value::String(encode_cursor(session, payload, operation, &initial)?));
    Ok(Value::Object(result))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_matches_rfc4231_case_2() {
        let mac = hmac_sha256(b"Jefe", &[b"what do ya want ", b"for nothing?"]);
        assert_eq!(hex::encode(mac), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    }

    #[test]
    fn positions_round_trip_canonically() {
        let state = State { r: vec![1, 127], p: 300, o: 0, skip: 5, expiry: 1 << 40, partial: true };
        let bytes = position_bytes(&state).unwrap();
        assert_eq!(position_from_bytes(&bytes).unwrap(), state);
        assert!(position_from_bytes(&[0, 0, 0x80, 0, 0, 0, 0]).is_err());
    }

    #[test]
    fn casefold_is_full() {
        assert_eq!(casefold("Straße"), "strasse");
    }
}
