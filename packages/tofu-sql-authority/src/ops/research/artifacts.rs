//! Research survey/ideation artifacts (`operations_pkg/_artifacts.py`
//! `_research_artifact_upsert`, `_research_artifacts_get`,
//! `_research_directions_list`).

use serde_json::{json, Map, Value};

use super::{json_text, load_lenient_skip, optional_text, py_strip};
use crate::error::{fail, internal, OpResult};
use crate::json::{python_str, truthy};
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::Session;

/// `_papers.py::_research_lang`.
fn research_lang(payload: &Payload) -> OpResult<String> {
    let lang = required_text(payload, "lang", 32)?;
    if lang.contains(':') {
        return fail("database_protocol_error", "Invalid research language");
    }
    Ok(lang)
}

pub(super) fn research_artifact_upsert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang_key = required_text(payload, "lang_key", 64)?;
    if !(lang_key.starts_with("survey:") || lang_key.starts_with("ideate:")) {
        return fail("database_protocol_error", "Invalid research artifact kind");
    }
    let meta = get(payload, "meta");
    if !meta.is_object() {
        return fail("database_protocol_error", "Invalid research metadata");
    }
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.lock_key("research.artifact", &format!("{user_id}:{}:{paper_hash}{lang_key}", paper_hash.chars().count()));
    let report = optional_text(payload, "report", "", 10_000_000, "research")?;
    let model = optional_text(payload, "model", "", 512, "research")?;
    let meta = json_text(meta)?;
    session.execute(
        "INSERT INTO paper_reports(user_id, paper_hash, lang, report, model, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, paper_hash, lang) DO UPDATE SET report = excluded.report, model = excluded.model, meta = excluded.meta, created_at = excluded.created_at",
        &params![user_id, paper_hash, lang_key, report, model, meta, created_at],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn research_artifacts_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = research_lang(payload)?;
    let rows = session.fetch_all(
        "SELECT lang, report, meta FROM paper_reports WHERE user_id = ? AND paper_hash = ? AND lang IN (?, ?) ORDER BY lang",
        &params![user_id, paper_hash, format!("survey:{lang}"), format!("ideate:{lang}")],
    )?;
    let mut result = Vec::new();
    for row in rows {
        let Some(meta) = load_lenient_skip(row.get("meta"))? else { continue };
        if meta.is_object() {
            result.push(json!({
                "lang_key": row.get("lang").to_json()?,
                "report": super::text_or_empty(&row, "report")?,
                "meta": meta,
            }));
        }
    }
    Ok(Value::Array(result))
}

/// Python `len(value)` of a JSON value.
fn py_len(value: &Value) -> OpResult<i64> {
    match value {
        Value::String(text) => Ok(text.chars().count() as i64),
        Value::Array(items) => Ok(items.len() as i64),
        Value::Object(map) => Ok(map.len() as i64),
        _ => internal("TypeError: object has no len()"),
    }
}

/// `value or default`.
fn or_value(value: Option<&Value>, default: Value) -> Value {
    match value {
        Some(value) if truthy(value) => value.clone(),
        _ => default,
    }
}

pub(super) fn research_directions_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let limit = integer(payload, "limit", Some(50), Some(1), Some(1000))?;
    let rows = session.fetch_all(
        "SELECT paper_hash, lang, meta, created_at FROM paper_reports WHERE user_id = ? AND (lang LIKE ? OR lang LIKE ?) ORDER BY created_at DESC LIMIT ?",
        &params![user_id, "survey:%", "ideate:%", 2000.min(limit * 2)],
    )?;
    let mut keys: Vec<(String, String)> = Vec::new();
    let mut folded: Vec<Map<String, Value>> = Vec::new();
    for row in rows {
        let Some(Value::Object(meta)) = load_lenient_skip(row.get("meta"))? else { continue };
        let direction_value = or_value(meta.get("direction"), Value::String(String::new()));
        let direction = py_strip(&python_str(&direction_value)).to_owned();
        if direction.is_empty() {
            continue;
        }
        let lang_key = row.get("lang").text_or_empty();
        let lang = match lang_key.split_once(':') {
            Some((_, rest)) => rest.to_owned(),
            None => "en".to_owned(),
        };
        let paper_hash = match row.get("paper_hash") {
            crate::session::SqlValue::Null => "None".to_owned(),
            value => python_str(&value.to_json()?),
        };
        let created_at = super::int_or_zero(&row, "created_at");
        let key = (paper_hash, lang.clone());
        let index = match keys.iter().position(|existing| *existing == key) {
            Some(index) => index,
            None => {
                keys.push(key);
                folded.push(
                    json!({
                        "direction": direction,
                        "lang": lang,
                        "created_at": created_at,
                        "accepted": 0,
                        "rejected": 0,
                        "gate_reached": "",
                        "degraded": false,
                        "has_survey": false,
                        "has_ideas": false,
                    })
                    .as_object()
                    .cloned()
                    .expect("object"),
                );
                folded.len() - 1
            }
        };
        let item = &mut folded[index];
        let previous = item["created_at"].as_i64().unwrap_or(0);
        item.insert("created_at".into(), Value::from(previous.max(created_at)));
        let kind = meta.get("kind");
        if kind.is_some_and(|kind| kind == "survey") {
            item.insert("has_survey".into(), Value::Bool(true));
        } else if kind.is_some_and(|kind| kind == "ideate") {
            item.insert("has_ideas".into(), Value::Bool(true));
            item.insert("accepted".into(), Value::from(py_len(&or_value(meta.get("accepted"), json!([])))?));
            item.insert("rejected".into(), Value::from(py_len(&or_value(meta.get("rejected"), json!([])))?));
            item.insert("gate_reached".into(), or_value(meta.get("gate_reached"), Value::String(String::new())));
            item.insert("degraded".into(), Value::Bool(meta.get("degraded").is_some_and(truthy)));
        }
    }
    folded.sort_by(|left, right| right["created_at"].as_i64().cmp(&left["created_at"].as_i64()));
    Ok(Value::Array(folded.into_iter().take(limit as usize).map(Value::Object).collect()))
}
