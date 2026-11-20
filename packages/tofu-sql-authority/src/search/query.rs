//! `turn_search_query.py`: owner-scoped search over the disposable chunk
//! projection, never transcripts (`contracts/conversation_search_v2.json`),
//! plus `adapters/search_support.py`'s accelerator budget.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use super::sources::ARCHIVED_SOURCE_PREFIX;
use super::text::{self, lower, lower_len, split, unique};
use crate::codec::py_eq;
use crate::error::{fail, Failure, OpResult, StorageError};
use crate::json;
use crate::ops::conversations::support::py_str;
use crate::params;
use crate::payload::{integer, Payload};
use crate::session::{Row, Session, SqlValue};

pub const QUERY_CHARACTERS: usize = 512;
pub const QUERY_TERMS: usize = 32;
pub const SCAN_PAGE: i64 = 256;
pub const CHUNK_SCAN_PAGE: i64 = 512;
pub const CHUNK_SCAN_BATCH: i64 = 16;
pub const CURSOR_BYTES: usize = 8192;
const ACCELERATOR_VM_STEPS: u64 = 500_000;
const I64_MAX: i64 = 9_223_372_036_854_775_807;
const SENTINEL_ID: &str = "\u{ffff}";

fn invalid_cursor() -> Failure {
    StorageError::new("database_invalid_payload", "Invalid conversation search cursor").into()
}

/// `normalize_query(value)`.
pub fn normalize_query(value: Option<&Value>) -> OpResult<String> {
    let Some(Value::String(value)) = value else {
        return fail("database_protocol_error", "Invalid conversation search query");
    };
    if value.chars().count() > QUERY_CHARACTERS || value.contains('\0') {
        return fail("database_protocol_error", "Invalid conversation search query");
    }
    let query = lower(text::strip(value));
    if query.chars().count() > QUERY_CHARACTERS || split(&query).len() > QUERY_TERMS {
        return fail("database_protocol_error", "Conversation search query is too long");
    }
    Ok(query)
}

fn binding(user_id: i64, query: &str) -> String {
    hex::encode(Sha256::digest(format!("{user_id}\0{query}").as_bytes()))[..24].to_owned()
}

/// `json.loads(base64.b64decode(value, altchars=b'-_', validate=True))`.
fn cursor_document(value: &str) -> Result<Value, ()> {
    let raw = text::b64decode_urlsafe(value)?;
    json::loads(&raw).map_err(|_| ())
}

fn is_strict_int(value: &Value) -> bool {
    matches!(value, Value::Number(number) if number.is_i64() || number.is_u64())
}

fn int_in(value: &Value, low: i64, high: i64) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64().filter(|v| (low..=high).contains(v)),
        _ => None,
    }
}

/// `_decode_cursor(value, binding)` → `(phase, updated_at, conversation_id)`.
fn decode_cursor(value: Option<&Value>, binding: &str) -> OpResult<(i64, Option<i64>, String)> {
    let value = match value {
        None | Some(Value::Null) => return Ok((0, None, String::new())),
        Some(Value::String(text)) if text.is_empty() => return Ok((0, None, String::new())),
        Some(value) => value,
    };
    let Value::String(text) = value else { return Err(invalid_cursor()) };
    if text.chars().count() > CURSOR_BYTES {
        return Err(invalid_cursor());
    }
    let Ok(Value::Array(data)) = cursor_document(text) else { return Err(invalid_cursor()) };
    if !(data.len() == 5 || data.len() == 6)
        || !py_eq(&data[0], &json!(2))
        || data[1].as_str() != Some(binding)
        || !is_strict_int(&data[2])
        || int_in(&data[2], 0, 1).is_none()
        || !is_strict_int(&data[3])
        || int_in(&data[3], 0, I64_MAX).is_none()
        || !data[4].as_str().is_some_and(|id| (1..=256).contains(&id.chars().count()))
    {
        return Err(invalid_cursor());
    }
    Ok((data[2].as_i64().unwrap_or(0), data[3].as_i64(), data[4].as_str().unwrap_or_default().to_owned()))
}

fn encode(document: Value) -> String {
    text::b64encode_urlsafe(text::json_dumps_ascii(&document).as_bytes())
}

fn cursor(binding: &str, phase: i64, updated_at: i64, conversation_id: &str) -> String {
    encode(json!([2, binding, phase, updated_at, conversation_id]))
}

fn literal(term: &str) -> String {
    format!("%{}%", term.replace('!', "!!").replace('%', "!%").replace('_', "!_"))
}

fn fts_available(session: &Session<'_>) -> OpResult<bool> {
    Ok(session
        .fetch_one(
            "SELECT 1 AS present FROM sqlite_schema WHERE type='table' AND name=?",
            &params!["storage_search_chunks_fts"],
        )?
        .is_some())
}

fn text_predicate(term: &str, accelerated: bool, user_id: i64) -> (String, Vec<SqlValue>) {
    if accelerated {
        let phrase = format!("\"{}\"", term.replace('"', "\"\""));
        return (
            "c.id IN (SELECT s.conversation_id FROM storage_search_chunks_fts AS f \
             JOIN storage_search_chunks AS s ON s.rowid=f.rowid \
             WHERE storage_search_chunks_fts MATCH ? AND s.user_id=? AND s.folded_text LIKE ? ESCAPE '!')"
                .into(),
            params![phrase, user_id, literal(term)].to_vec(),
        );
    }
    (
        "EXISTS (SELECT 1 FROM storage_search_chunks AS s \
         WHERE s.user_id=c.user_id AND s.conversation_id=c.id AND s.folded_text LIKE ? ESCAPE '!')"
            .into(),
        params![literal(term)].to_vec(),
    )
}

fn predicate(query: &str, phase: i64, accelerated: bool, user_id: i64) -> (String, Vec<SqlValue>) {
    let lower_fn = "tofu_search_lower";
    let (phrase, phrase_params) = text_predicate(query, accelerated, user_id);
    let exact = format!("({lower_fn}(c.title) LIKE ? ESCAPE '!' OR {lower_fn}(c.id) LIKE ? ESCAPE '!' OR {phrase})");
    let mut exact_params = params![literal(query), literal(query)].to_vec();
    exact_params.extend(phrase_params);
    if phase == 0 {
        return (exact, exact_params);
    }
    let words = split(query);
    let terms = unique(&words);
    let mut pieces = Vec::new();
    let mut values = Vec::new();
    for term in terms {
        let (content, content_params) = text_predicate(term, accelerated, user_id);
        pieces.push(format!("({lower_fn}(c.title) LIKE ? ESCAPE '!' OR {content})"));
        values.push(SqlValue::from(literal(term)));
        values.extend(content_params);
    }
    values.extend(exact_params);
    (format!("({}) AND NOT {exact}", pieces.join(" AND ")), values)
}

fn after(updated_at: Option<i64>, conversation_id: &str) -> (&'static str, Vec<SqlValue>) {
    match updated_at {
        None => ("", Vec::new()),
        Some(updated_at) => (
            " AND (c.updated_at_ms<? OR (c.updated_at_ms=? AND c.id<?))",
            params![updated_at, updated_at, conversation_id].to_vec(),
        ),
    }
}

fn header_hit(conversation_id: &str, match_field: &str) -> Value {
    let snippet = if match_field == "id" { conversation_id } else { "" };
    json!({"id": conversation_id, "snippet": snippet, "role": null,
           "match_field": match_field, "turn_id": null, "ordinal": null})
}

fn column_text(row: &Row, column: &str) -> String {
    row.get(column).text_or_empty()
}

fn snippet(session: &Session<'_>, row: &Row, user_id: i64, query: &str, radius: i64) -> OpResult<Value> {
    let conversation_id = py_str(row.require("id")?);
    let title = column_text(row, "title");
    if lower(&title).contains(query) {
        return Ok(header_hit(&conversation_id, "title"));
    }
    if lower(&conversation_id).contains(query) {
        return Ok(header_hit(&conversation_id, "id"));
    }
    let first = split(query)[0];
    let chunk = session.fetch_one(
        "SELECT search_text,role,match_field,turn_id,ordinal FROM storage_search_chunks \
         WHERE user_id=? AND conversation_id=? \
         AND (folded_text LIKE ? ESCAPE '!' OR folded_text LIKE ? ESCAPE '!') \
         ORDER BY CASE WHEN folded_text LIKE ? ESCAPE '!' THEN 0 ELSE 1 END, \
         CASE WHEN role='user' THEN 0 ELSE 1 END, \
         CASE WHEN match_field='originalContent' THEN 0 ELSE 1 END, ordinal,turn_id,chunk_index LIMIT 1",
        &params![user_id, conversation_id.as_str(), literal(query), literal(first), literal(query)],
    )?;
    match chunk {
        None => Ok(header_hit(&conversation_id, "title")),
        Some(chunk) => chunk_snippet(&chunk, &conversation_id, query, radius),
    }
}

fn chunk_snippet(chunk: &Row, conversation_id: &str, query: &str, radius: i64) -> OpResult<Value> {
    let first = split(query)[0];
    let text: Vec<char> = column_text(chunk, "search_text").chars().collect();
    let source: String = text.iter().collect();
    let folded: Vec<char> = lower(&source).chars().collect();
    let query_chars: Vec<char> = query.chars().collect();
    let first_chars: Vec<char> = first.chars().collect();
    let (position, term_len) = match text::find(&folded, &query_chars) {
        Some(position) => (Some(position), query_chars.len()),
        None => (text::find(&folded, &first_chars), first_chars.len()),
    };
    let mut snippet = String::new();
    if let Some(position) = position {
        if radius > 0 {
            let (mut start_index, mut end_index) = (text.len(), text.len());
            let mut offset = 0usize;
            for (index, character) in text.iter().enumerate() {
                let next_offset = offset + lower_len(*character);
                if offset <= position && position < next_offset {
                    start_index = index;
                }
                if offset < position + term_len && position + term_len <= next_offset {
                    end_index = index + 1;
                    break;
                }
                offset = next_offset;
            }
            let radius = radius as usize;
            let start = start_index.saturating_sub(radius);
            let end = text.len().min(end_index + radius);
            let window: String = text[start.min(end)..end].iter().collect();
            snippet = format!("…{}…", text::strip(&window.replace('\n', " ")));
        }
    }
    let turn_id = py_str(chunk.require("turn_id")?);
    let live = !turn_id.starts_with(ARCHIVED_SOURCE_PREFIX);
    let ordinal = if live { Value::from(int(chunk.require("ordinal")?)?) } else { Value::Null };
    Ok(json!({
        "id": conversation_id, "snippet": snippet,
        "role": chunk.require("role")?.to_json()?, "match_field": chunk.require("match_field")?.to_json()?,
        "turn_id": if live { Value::String(turn_id) } else { Value::Null },
        "ordinal": ordinal,
    }))
}

fn int(value: &SqlValue) -> OpResult<i64> {
    value.as_int().ok_or_else(|| Failure::Internal("int() of a non-integer column".into()))
}

fn page(items: Vec<Value>, next_cursor: Option<String>, incomplete: bool) -> Value {
    json!({"items": items, "next_cursor": next_cursor, "incomplete": incomplete})
}

struct Request<'q> {
    user_id: i64,
    query: &'q str,
    limit: i64,
    radius: i64,
    cursor: Option<&'q Value>,
}

fn fast_page(session: &Session<'_>, request: &Request<'_>) -> OpResult<Value> {
    let Request { user_id, query, limit, radius, cursor: raw_cursor } = *request;
    let binding = binding(user_id, query);
    let (mut phase, mut updated_at, mut conversation_id) = decode_cursor(raw_cursor, &binding)?;
    let words_all = split(query);
    let words = unique(&words_all);
    if query.chars().count() < 2 {
        return Ok(page(Vec::new(), None, false));
    }
    let mut items: Vec<Value> = Vec::new();
    while phase <= if words.len() > 1 { 1 } else { 0 } {
        let (after_sql, after_params) = after(updated_at, &conversation_id);
        let (predicate_sql, predicate_params) = predicate(query, phase, true, user_id);
        let mut values = vec![SqlValue::Integer(user_id)];
        values.extend(after_params);
        values.extend(predicate_params);
        let wanted = limit - items.len() as i64;
        values.push(SqlValue::Integer(wanted + 1));
        let mut rows = session.fetch_all(
            &format!(
                "SELECT c.id,c.title,c.updated_at_ms FROM storage_search_conversations AS c \
                 WHERE c.user_id=?{after_sql} AND ({predicate_sql}) \
                 ORDER BY c.updated_at_ms DESC,c.id DESC LIMIT ?"
            ),
            &values,
        )?;
        let more = rows.len() as i64 > wanted;
        rows.truncate(wanted.max(0) as usize);
        for row in &rows {
            items.push(snippet(session, row, user_id, query, radius)?);
        }
        if let Some(last) = rows.last() {
            updated_at = Some(int(last.require("updated_at_ms")?)?);
            conversation_id = py_str(last.require("id")?);
        }
        if more {
            return Ok(page(items, Some(cursor(&binding, phase, updated_at.unwrap_or(0), &conversation_id)), false));
        }
        phase += 1;
        updated_at = None;
        conversation_id = String::new();
        if items.len() as i64 >= limit && phase <= 1 && words.len() > 1 {
            return Ok(page(items, Some(cursor(&binding, phase, I64_MAX, SENTINEL_ID)), false));
        }
    }
    Ok(page(items, None, false))
}

fn scan_cursor(binding: &str, phase: i64, updated_at: i64, conversation_id: &str, pending: Value) -> String {
    encode(json!([2, binding, phase, updated_at, conversation_id, {"scan": 1, "pending": pending}]))
}

fn point_valid(point: &Value) -> bool {
    match point {
        Value::Null => true,
        Value::Array(items) => {
            items.len() == 3
                && is_strict_int(&items[0])
                && int_in(&items[0], 0, I64_MAX).is_some()
                && items[1].as_str().is_some_and(|text| text.chars().count() <= 1024)
                && is_strict_int(&items[2])
                && int_in(&items[2], 0, I64_MAX).is_some()
        }
        _ => false,
    }
}

fn key_set_is(map: &Map<String, Value>, keys: &[&str]) -> bool {
    map.len() == keys.len() && keys.iter().all(|key| map.contains_key(*key))
}

/// `_scan_state(value, query)`: the scan extension or `None`.
fn scan_state(value: Option<&Value>, query: &str) -> OpResult<Option<Map<String, Value>>> {
    let text = match value {
        Some(value) if json::truthy(value) => value.as_str().unwrap_or_default(),
        _ => return Ok(None),
    };
    let Ok(Value::Array(data)) = cursor_document(text) else { return Err(invalid_cursor()) };
    if data.len() == 5 {
        return Ok(None);
    }
    let extension = &data[5];
    let mut valid = matches!(extension, Value::Object(map)
        if key_set_is(map, &["scan", "pending"]) && is_strict_int(&map["scan"]) && py_eq(&map["scan"], &json!(1)));
    let pending = extension.as_object().and_then(|map| map.get("pending")).unwrap_or(&Value::Null);
    if !pending.is_null() {
        let distinct = {
            let words = split(query);
            unique(&words).len()
        };
        valid = valid
            && matches!(pending, Value::Object(map)
                if key_set_is(map, &["after", "mask", "best"]) || key_set_is(map, &["after", "mask", "best", "phrase"]));
        if valid {
            let map = pending.as_object().expect("pending object");
            if let Some(phrase) = map.get("phrase") {
                valid = phrase.is_boolean();
            }
            if valid {
                valid = is_strict_int(&map["mask"]) && int_in(&map["mask"], 0, (1i64 << distinct) - 1).is_some();
                for key in ["after", "best"] {
                    valid = valid && point_valid(&map[key]);
                }
            }
        }
    }
    if !valid {
        return Err(invalid_cursor());
    }
    Ok(extension.as_object().cloned())
}

/// `_chunk_rank(chunk, query)`.
fn chunk_rank(chunk: &Row, query: &str) -> OpResult<(bool, bool, bool, i64, String, i64)> {
    Ok((
        !lower(&column_text(chunk, "search_text")).contains(query),
        chunk.require("role")?.as_str() != Some("user"),
        chunk.require("match_field")?.as_str() != Some("originalContent"),
        int(chunk.require("ordinal")?)?,
        py_str(chunk.require("turn_id")?),
        int(chunk.require("chunk_index")?)?,
    ))
}

fn word_mask(words: &[&str], text: &str) -> i64 {
    words.iter().enumerate().filter(|(_, word)| text.contains(**word)).map(|(index, _)| 1i64 << index).sum()
}

fn point_params(point: &Value) -> Vec<SqlValue> {
    point
        .as_array()
        .map(|items| items.iter().map(|item| crate::session::json_param(item).unwrap_or(SqlValue::Null)).collect())
        .unwrap_or_default()
}

fn portable_page(session: &Session<'_>, request: &Request<'_>) -> OpResult<Value> {
    let Request { user_id, query, limit, radius, cursor: raw_cursor } = *request;
    let binding = binding(user_id, query);
    let (mut phase, mut updated_at, mut conversation_id) = decode_cursor(raw_cursor, &binding)?;
    let extension = scan_state(raw_cursor, query)?;
    let mut pending: Value = extension.as_ref().and_then(|e| e.get("pending")).cloned().unwrap_or(Value::Null);
    let words_all = split(query);
    let words = unique(&words_all);
    let all_bits = (1i64 << words.len()) - 1;
    let mut items: Vec<Value> = Vec::new();
    let (mut headers_read, mut chunks_read) = (0i64, 0i64);
    let last_phase = (words.len() > 1) as i64;
    while phase <= last_phase {
        if headers_read >= SCAN_PAGE || chunks_read >= CHUNK_SCAN_PAGE {
            let resume_id = if conversation_id.is_empty() { SENTINEL_ID.to_owned() } else { conversation_id.clone() };
            return Ok(page(
                items,
                Some(scan_cursor(&binding, phase, updated_at.unwrap_or(I64_MAX), &resume_id, pending)),
                true,
            ));
        }
        let headers = if json::truthy(&pending) {
            let headers = session.fetch_all(
                "SELECT id,title,updated_at_ms FROM storage_search_conversations WHERE user_id=? AND id=?",
                &params![user_id, conversation_id.as_str()],
            )?;
            if headers.is_empty() {
                pending = Value::Null;
                continue;
            }
            headers
        } else {
            let (after_sql, after_params) = after(updated_at, &conversation_id);
            let mut values = vec![SqlValue::Integer(user_id)];
            values.extend(after_params);
            values.push(SqlValue::Integer(SCAN_PAGE - headers_read));
            session.fetch_all(
                &format!(
                    "SELECT c.id,c.title,c.updated_at_ms FROM storage_search_conversations c WHERE c.user_id=?\
                     {after_sql} ORDER BY c.updated_at_ms DESC,c.id DESC LIMIT ?"
                ),
                &values,
            )?
        };
        if headers.is_empty() {
            phase += 1;
            updated_at = None;
            conversation_id = String::new();
            continue;
        }
        for header in &headers {
            headers_read += 1;
            let cid = py_str(header.require("id")?);
            let title = lower(&column_text(header, "title"));
            let title_hit = title.contains(query);
            let id_hit = lower(&cid).contains(query);
            let pending_map = if json::truthy(&pending) { pending.as_object().cloned() } else { None };
            let mut phrase_hit = title_hit
                || id_hit
                || pending_map.as_ref().is_some_and(|map| map.get("phrase").is_some_and(json::truthy));
            let mut mask = match &pending_map {
                Some(map) => map["mask"].as_i64().unwrap_or(0),
                None => word_mask(&words, &title),
            };
            let mut chunk_after = pending_map.as_ref().map(|map| map["after"].clone()).unwrap_or(Value::Null);
            let mut best = pending_map.as_ref().map(|map| map["best"].clone()).unwrap_or(Value::Null);
            let mut best_chunk: Option<Row> = None;
            if let Value::Array(point) = &best {
                let mut values = params![user_id, cid.as_str()].to_vec();
                values.extend(point_params(&Value::Array(vec![point[1].clone(), point[2].clone()])));
                best_chunk = session.fetch_one(
                    "SELECT ordinal,turn_id,chunk_index,search_text,role,match_field FROM storage_search_chunks \
                     WHERE user_id=? AND conversation_id=? AND turn_id=? AND chunk_index=?",
                    &values,
                )?;
                chunks_read += 1;
            }
            let mut complete = title_hit || id_hit;
            while !complete {
                let budget = CHUNK_SCAN_BATCH.min(CHUNK_SCAN_PAGE - chunks_read);
                if budget <= 0 {
                    let pending_state = json!({"after": chunk_after, "mask": mask, "best": best, "phrase": phrase_hit});
                    return Ok(page(
                        items,
                        Some(scan_cursor(&binding, phase, int(header.require("updated_at_ms")?)?, &cid, pending_state)),
                        true,
                    ));
                }
                let mut values = params![user_id, cid.as_str()].to_vec();
                let mut after_sql = "";
                if json::truthy(&chunk_after) {
                    after_sql = " AND (ordinal,turn_id,chunk_index)>(?,?,?)";
                    values.extend(point_params(&chunk_after));
                }
                values.push(SqlValue::Integer(budget));
                let chunk_rows = session.fetch_all(
                    &format!(
                        "SELECT ordinal,turn_id,chunk_index,search_text,role,match_field FROM storage_search_chunks \
                         WHERE user_id=? AND conversation_id=?{after_sql} ORDER BY ordinal,turn_id,chunk_index LIMIT ?"
                    ),
                    &values,
                )?;
                chunks_read += chunk_rows.len() as i64;
                for chunk in &chunk_rows {
                    let text = lower(&column_text(chunk, "search_text"));
                    let point = json!([
                        int(chunk.require("ordinal")?)?,
                        py_str(chunk.require("turn_id")?),
                        int(chunk.require("chunk_index")?)?
                    ]);
                    mask |= word_mask(&words, &text);
                    phrase_hit |= text.contains(query);
                    if text.contains(words[0]) {
                        let better = match &best_chunk {
                            None => true,
                            Some(current) => chunk_rank(chunk, query)? < chunk_rank(current, query)?,
                        };
                        if better {
                            best = point;
                            best_chunk = Some(chunk.clone());
                        }
                    }
                }
                if let Some(last) = chunk_rows.last() {
                    chunk_after = json!([
                        int(last.require("ordinal")?)?,
                        py_str(last.require("turn_id")?),
                        int(last.require("chunk_index")?)?
                    ]);
                }
                if (chunk_rows.len() as i64) < budget {
                    complete = true;
                }
            }
            updated_at = Some(int(header.require("updated_at_ms")?)?);
            conversation_id = cid.clone();
            pending = Value::Null;
            let matched = if phase == 0 { phrase_hit } else { mask == all_bits && !phrase_hit };
            if matched {
                let hit = if title_hit || (best.is_null() && !id_hit) {
                    Some(header_hit(&cid, "title"))
                } else if id_hit {
                    Some(header_hit(&cid, "id"))
                } else {
                    let chunk = match best_chunk {
                        Some(chunk) => Some(chunk),
                        None => {
                            let point = best.as_array().cloned().unwrap_or_default();
                            let mut values = params![user_id, cid.as_str()].to_vec();
                            values.extend(point_params(&Value::Array(point[1..].to_vec())));
                            session.fetch_one(
                                "SELECT search_text,role,match_field,turn_id,ordinal FROM storage_search_chunks \
                                 WHERE user_id=? AND conversation_id=? AND turn_id=? AND chunk_index=?",
                                &values,
                            )?
                        }
                    };
                    match chunk {
                        Some(chunk) => Some(chunk_snippet(&chunk, &cid, query, radius)?),
                        None => None,
                    }
                };
                if let Some(hit) = hit {
                    items.push(hit);
                }
            }
            if items.len() as i64 >= limit || chunks_read >= CHUNK_SCAN_PAGE {
                let next = scan_cursor(&binding, phase, updated_at.unwrap_or(0), &cid, Value::Null);
                return Ok(page(items, Some(next), chunks_read >= CHUNK_SCAN_PAGE));
            }
        }
    }
    Ok(page(items, None, false))
}

/// `accelerator_budget(session)` around `_fast_page`: `Ok(None)` is
/// `AcceleratorBudgetExceeded`.
fn accelerated_page(session: &Session<'_>, request: &Request<'_>, deadline: Instant) -> OpResult<Option<Value>> {
    let connection = session.connection();
    let steps = Arc::new(AtomicU64::new(0));
    let exhausted = Arc::new(AtomicBool::new(false));
    {
        let steps = Arc::clone(&steps);
        let exhausted = Arc::clone(&exhausted);
        connection.progress_handler(
            1000,
            Some(move || {
                let total = steps.fetch_add(1000, Ordering::SeqCst) + 1000;
                let spent = total >= ACCELERATOR_VM_STEPS;
                exhausted.store(spent, Ordering::SeqCst);
                spent || Instant::now() >= deadline
            }),
        );
    }
    let result = fast_page(session, request);
    connection.progress_handler(1000, Some(move || Instant::now() >= deadline));
    match result {
        Err(Failure::Sqlite(rusqlite::Error::SqliteFailure(error, message)))
            if exhausted.load(Ordering::SeqCst)
                && (error.code == rusqlite::ErrorCode::OperationInterrupted
                    || message.as_deref().is_some_and(|m| m.contains("interrupted")))
                && Instant::now() < deadline =>
        {
            Ok(None)
        }
        other => other.map(Some),
    }
}

fn search_page(session: &Session<'_>, request: &Request<'_>, deadline: Instant) -> OpResult<Value> {
    decode_cursor(request.cursor, &binding(request.user_id, request.query))?;
    if request.query.chars().count() < 2 {
        return Ok(page(Vec::new(), None, false));
    }
    let scan = scan_state(request.cursor, request.query)?;
    if scan.is_none() && fts_available(session)? && split(request.query).iter().all(|term| term.chars().count() >= 3) {
        if let Some(result) = accelerated_page(session, request, deadline)? {
            return Ok(result);
        }
    }
    portable_page(session, request)
}

/// `search_conversations(session, payload)` on the projection session.
pub fn search_conversations(session: &Session<'_>, payload: &Payload, deadline: Instant) -> OpResult<Value> {
    let query = normalize_query(payload.get("query"))?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let limit = integer(payload, "limit", Some(50), Some(1), Some(200))?;
    let radius = integer(payload, "snippet_radius", Some(40), Some(0), Some(400))?;
    let page_mode = match payload.get("page") {
        None => false,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid conversation search page mode"),
    };
    let request = Request { user_id, query: &query, limit, radius, cursor: payload.get("cursor") };
    let result = search_page(session, &request, deadline)?;
    if page_mode {
        return Ok(result);
    }
    Ok(result["items"].clone())
}

/// `attach_search_coverage(result, status)`.
pub fn attach_search_coverage(result: Value, status: &Map<String, Value>) -> Value {
    let Value::Object(mut result) = result else { return result };
    if !result.contains_key("next_cursor") {
        return Value::Object(result);
    }
    let mut reasons: Vec<Value> = match status.get("coverage_reasons") {
        Some(Value::Array(items)) => items.iter().take(8).cloned().collect(),
        _ => Vec::new(),
    };
    let null = Value::Null;
    let metrics = status.get("metrics").unwrap_or(&null);
    let oversize = Value::String("oversize_source".into());
    if metrics.get("oversize_skipped").is_some_and(json::truthy) && !reasons.contains(&oversize) {
        reasons.push(oversize);
    }
    if status.get("last_worker_error").is_some_and(json::truthy) {
        reasons.push(Value::String("index_worker_error".into()));
    }
    if result.get("incomplete").is_some_and(json::truthy) {
        reasons.push(Value::String("scan_continuation".into()));
    }
    let coverage = if !reasons.is_empty() {
        "partial"
    } else if status.get("backfill_complete").is_some_and(json::truthy) {
        "ready"
    } else {
        "indexing"
    };
    let mut unique_reasons: Vec<Value> = Vec::new();
    for reason in reasons {
        if !unique_reasons.contains(&reason) {
            unique_reasons.push(reason);
        }
    }
    unique_reasons.truncate(8);
    result.insert("coverage".into(), Value::String(coverage.into()));
    result.insert("coverage_reasons".into(), Value::Array(unique_reasons));
    Value::Object(result)
}
