//! `operations_pkg/_model_continuation.py`: durable derived model context
//! over `storage_records`, with private ports of
//! `lib/storage/model_continuation.py::{encode_projection, settled_witness}`.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::codec;
use crate::error::{internal, OpResult};
use crate::json;
use crate::ops::conversations::continuation_prefix;
use crate::ops::conversations::support::{load, now_ms, py_int, turn_public};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Session, SqlValue};

const NAMESPACE: &str = "model.continuation.v1";
const MAX_ENTRY_BYTES: usize = 8 * 1024 * 1024;
const MAX_MESSAGES: usize = 16384;
const MAX_TURN_WITNESSES: usize = 4096;
const MAX_JSON_SAFE_INTEGER: i64 = (1 << 53) - 1;
const DISPLAY_ONLY_FIELDS: &[&str] = &[
    "timingTrace",
    "translation",
    "apiRounds",
    "usage",
    "cost",
    "lastRoundUsage",
    "activityTimeline",
    "modifiedFiles",
    "modifiedFileList",
    "fileChanges",
    "provenance",
    "translatedContent",
    "_showingTranslation",
    "_translateDone",
    "_translateFailed",
    "_translateModel",
];
const ROUND_DISPLAY_ONLY_FIELDS: &[&str] = &["_swarmSnapshot", "_swarm", "timingTrace", "translation", "usage"];
const SEGMENT_DISPLAY_ONLY_FIELDS: &[&str] = &["translatedText"];
const TRANSLATED_SEGMENT_TYPES: &[&str] = &["text", "thinking"];

/// Python `json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))`.
fn python_json(value: &Value, sort_keys: bool, out: &mut String) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => {
            if number.is_f64() {
                out.push_str(&json::python_float_repr(number.as_f64().unwrap_or(0.0)));
            } else {
                out.push_str(&number.to_string());
            }
        }
        Value::String(text) => out.push_str(&serde_json::to_string(text).expect("string encoding")),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                python_json(item, sort_keys, out);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if sort_keys {
                entries.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
            }
            out.push('{');
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(key).expect("key encoding"));
                out.push(':');
                python_json(item, sort_keys, out);
            }
            out.push('}');
        }
    }
}

fn is_lower_hex_64(value: &Value) -> bool {
    matches!(value, Value::String(text) if text.len() == 64 && text.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f')))
}

/// `encode_projection(projection)`: bounded JSON text or `None`.
pub fn encode_projection(projection: &Value) -> Option<String> {
    let Value::Object(map) = projection else { return None };
    let witnesses_allowed = if map.contains_key("historyProof") { 1 } else { MAX_TURN_WITNESSES };
    let Some(Value::Array(messages)) = map.get("messages") else { return None };
    if messages.is_empty() || messages.len() > MAX_MESSAGES || !messages.iter().all(Value::is_object) {
        return None;
    }
    if messages.last().and_then(|message| message.get("role")) != Some(&Value::String("assistant".into())) {
        return None;
    }
    let Some(Value::Array(turns)) = map.get("turns") else { return None };
    if turns.is_empty() || turns.len() > witnesses_allowed {
        return None;
    }
    if let Some(proof) = map.get("historyProof") {
        let Value::Object(proof) = proof else { return None };
        let keys_ok = proof.len() == 3 && ["version", "count", "digest"].iter().all(|key| proof.contains_key(*key));
        let version_ok = matches!(json::as_strict_int(&proof["version"]), Some(1 | 2)) && keys_ok;
        if !keys_ok
            || !version_ok
            || !json::as_strict_int(&proof["count"]).is_some_and(|count| (0..=MAX_JSON_SAFE_INTEGER).contains(&count))
            || !is_lower_hex_64(&proof["digest"])
        {
            return None;
        }
    }
    for witness in turns {
        let Value::Array(pair) = witness else { return None };
        if pair.len() != 2 {
            return None;
        }
        let Value::String(turn_id) = &pair[0] else { return None };
        if turn_id.is_empty() || turn_id.chars().count() > 256 || !is_lower_hex_64(&pair[1]) {
            return None;
        }
    }
    let mut encoded = String::new();
    python_json(projection, false, &mut encoded);
    if encoded.len() > MAX_ENTRY_BYTES {
        return None;
    }
    Some(encoded)
}

fn model_round(round_entry: &Value) -> Value {
    match round_entry {
        Value::Object(map) => Value::Object(
            map.iter()
                .filter(|(key, _)| !ROUND_DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// `model_projection_for_context(projection)`.
fn model_projection_for_context(projection: &serde_json::Map<String, Value>) -> Value {
    let mut result: serde_json::Map<String, Value> = projection
        .iter()
        .filter(|(key, _)| !DISPLAY_ONLY_FIELDS.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    if let Some(Value::Array(rounds)) = result.get("toolRounds") {
        let rounds = rounds.iter().map(model_round).collect();
        result.insert("toolRounds".into(), Value::Array(rounds));
    }
    if let Some(Value::Array(segments)) = result.get("segments") {
        let mut projected_segments = Vec::with_capacity(segments.len());
        for segment in segments {
            let Value::Object(segment) = segment else {
                projected_segments.push(segment.clone());
                continue;
            };
            let translated = matches!(segment.get("type"), Some(Value::String(kind)) if TRANSLATED_SEGMENT_TYPES.contains(&kind.as_str()));
            let mut projected: serde_json::Map<String, Value> = segment
                .iter()
                .filter(|(key, _)| !(translated && SEGMENT_DISPLAY_ONLY_FIELDS.contains(&key.as_str())))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            if let Some(round) = projected.get("_round") {
                let round = model_round(round);
                projected.insert("_round".into(), round);
            }
            projected_segments.push(Value::Object(projected));
        }
        result.insert("segments".into(), Value::Array(projected_segments));
    }
    Value::Object(result)
}

/// `settled_witness(row)` for one public turn.
fn settled_witness(row: &serde_json::Map<String, Value>) -> OpResult<Value> {
    let mut source = serde_json::Map::new();
    for key in ["actor", "laneId", "parentTurnId", "status", "currentAttemptId"] {
        source.insert(key.into(), row.get(key).cloned().unwrap_or(Value::Null));
    }
    if row.contains_key("modelContextRevision") || row.contains_key("projectionRevision") {
        let revision = match row.get("modelContextRevision") {
            Some(value) => value.clone(),
            None => Value::from(row.get("projectionRevision").and_then(Value::as_i64).unwrap_or(0)),
        };
        source.insert("modelContextRevision".into(), revision);
    }
    let projection = match row.get("projection") {
        Some(Value::Object(map)) => map.clone(),
        Some(value) if json::truthy(value) => return internal("projection.items() of a non-mapping"),
        _ => serde_json::Map::new(),
    };
    source.insert("projection".into(), model_projection_for_context(&projection));
    let mut encoded = String::new();
    python_json(&Value::Object(source), true, &mut encoded);
    let Some(turn_id) = row.get("turnId") else { return internal("KeyError: turnId") };
    Ok(Value::Array(vec![turn_id.clone(), Value::String(hex::encode(Sha256::digest(encoded.as_bytes())))]))
}

/// `_identity(session, payload, lock=)`.
fn identity(session: &Session<'_>, payload: &Payload, lock: bool) -> OpResult<(i64, String, String, String, bool)> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let conversation_id = required_text(payload, "conversation_id", 256)?;
    let lane_id = required_text(payload, "lane_id", 256)?;
    if lock {
        session.lock_key("conversation", &format!("{user_id}:{conversation_id}"));
        session.lock_key("turn_conversation", &format!("{user_id}:{conversation_id}"));
    }
    let owned = session.fetch_one(
        "SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?",
        &params![conversation_id.as_str(), user_id],
    )?;
    let key = continuation_prefix(user_id, &conversation_id) + &hex::encode(Sha256::digest(lane_id.as_bytes()));
    Ok((user_id, conversation_id, lane_id, key, owned.is_some()))
}

pub fn continuation_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (_, _, _, key, owned) = identity(session, payload, false)?;
    if !owned {
        return Ok(Value::Null);
    }
    let Some(row) = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
        &params![NAMESPACE, key.as_str()],
    )?
    else {
        return Ok(Value::Null);
    };
    let projection = load(row.get("value_json"))?;
    Ok(if encode_projection(&projection).is_some() { projection } else { Value::Null })
}

pub fn continuation_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, conversation_id, lane_id, key, owned) = identity(session, payload, true)?;
    if !owned {
        return Ok(Value::Bool(false));
    }
    let projection = get(payload, "projection");
    let Some(encoded) = encode_projection(projection) else { return Ok(Value::Bool(false)) };
    let witness = projection["turns"].as_array().and_then(|turns| turns.last()).cloned().unwrap_or(Value::Null);
    let witness_turn = witness[0].as_str().unwrap_or_default().to_owned();
    session.lock_key("turn_attempt", &witness_turn);
    let row = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? \
         AND user_id=? AND lane_id=? AND turn_id=?",
        &params![conversation_id.as_str(), user_id, lane_id.as_str(), witness_turn.as_str()],
    )?;
    let Some(row) = row else { return Ok(Value::Bool(false)) };
    if !matches!(row.get("status"), SqlValue::Text(status) if status == "completed" || status == "truncated") {
        return Ok(Value::Bool(false));
    }
    if !codec::py_eq(&settled_witness(&turn_public(session, &row)?)?, &witness) {
        return Ok(Value::Bool(false));
    }
    let frontier = py_int(row.get("ordinal"))? + 1;
    let current = session.fetch_one(
        "SELECT value_json,version FROM storage_records WHERE namespace=? AND record_key=?",
        &params![NAMESPACE, key.as_str()],
    )?;
    if let Some(current) = current {
        let actual = py_int(current.get("version"))?;
        if frontier < actual {
            return Ok(Value::Bool(false));
        }
        if frontier == actual {
            return Ok(Value::Bool(codec::py_eq(&load(current.get("value_json"))?, projection)));
        }
        let changed = session.execute(
            "UPDATE storage_records SET value_json=?,version=?,updated_at_ms=? \
             WHERE namespace=? AND record_key=? AND version=?",
            &params![encoded, frontier, now_ms(), NAMESPACE, key.as_str(), actual],
        )?;
        return Ok(Value::Bool(changed == 1));
    }
    session.execute(
        "INSERT INTO storage_records(namespace,record_key,value_json,version,updated_at_ms) \
         VALUES (?,?,?,?,?)",
        &params![NAMESPACE, key.as_str(), encoded, frontier, now_ms()],
    )?;
    Ok(Value::Bool(true))
}

pub fn continuation_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (_, _, _, key, owned) = identity(session, payload, true)?;
    if !owned {
        return Ok(Value::Bool(false));
    }
    let deleted = session
        .execute("DELETE FROM storage_records WHERE namespace=? AND record_key=?", &params![NAMESPACE, key.as_str()])?;
    Ok(Value::Bool(deleted != 0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn python_json_text_matches_the_stdlib_encoder() {
        let mut out = String::new();
        python_json(&json!({"b": [1.0, 1e16, "é\n"], "a": null}), true, &mut out);
        assert_eq!(out, "{\"a\":null,\"b\":[1.0,1e+16,\"é\\n\"]}");
        let projection = json!({"messages": [{"role": "assistant"}], "turns": [["t", "a".repeat(64)]]});
        assert!(encode_projection(&projection).is_some());
        assert!(encode_projection(&json!({"messages": [{"role": "user"}], "turns": [["t", "a".repeat(64)]]})).is_none());
    }
}
