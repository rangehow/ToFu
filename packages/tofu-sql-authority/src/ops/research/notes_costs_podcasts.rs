//! Paper notes, the daily cost cache and paper podcasts
//! (`operations_pkg/_papers.py`).

use serde_json::{json, Map, Value};

use super::{float_or_zero, int_or_zero, json_text, load_lenient, optional_text, py_isdigit, text_or_empty};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

pub(super) fn paper_note_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = optional_text(payload, "lang", "", 64, "paper note")?;
    let rows = session.fetch_all(
        "SELECT id, paper_hash, lang, anchor, note, created_at, updated_at FROM paper_notes WHERE user_id = ? AND paper_hash = ? AND lang = ? ORDER BY created_at ASC, id ASC",
        &params![user_id, paper_hash, lang],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        let anchor = load_lenient(row.get("anchor"))?;
        result.push(json!({
            "id": row.get("id").to_json()?,
            "paper_hash": row.get("paper_hash").to_json()?,
            "lang": row.get("lang").to_json()?,
            "anchor": if anchor.is_object() { anchor } else { json!({}) },
            "note": text_or_empty(&row, "note")?,
            "created_at": int_or_zero(&row, "created_at"),
            "updated_at": int_or_zero(&row, "updated_at"),
        }));
    }
    Ok(Value::Array(result))
}

pub(super) fn paper_note_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let note_id = required_text(payload, "id", 256)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = optional_text(payload, "lang", "", 64, "paper note")?;
    let anchor = payload.get("anchor").cloned().unwrap_or_else(|| json!({}));
    if !anchor.is_object() {
        return fail("database_protocol_error", "Invalid paper note anchor");
    }
    let note = required_text(payload, "note", 100_000)?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    let updated_at = integer(payload, "updated_at", None, Some(0), None)?;
    session.lock_key("paper.note", &format!("{user_id}:{note_id}"));
    session.execute(
        "INSERT INTO paper_notes(user_id, id, paper_hash, lang, anchor, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        &params![user_id, note_id, paper_hash, lang, json_text(&anchor)?, note, created_at, updated_at],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn paper_note_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let note_id = required_text(payload, "id", 256)?;
    let note = required_text(payload, "note", 100_000)?;
    let updated_at = integer(payload, "updated_at", None, Some(0), None)?;
    session.lock_key("paper.note", &format!("{user_id}:{note_id}"));
    let changed = session.execute(
        "UPDATE paper_notes SET note = ?, updated_at = ? WHERE user_id = ? AND id = ?",
        &params![note, updated_at, user_id, note_id],
    )?;
    Ok(json!({"updated": changed != 0}))
}

pub(super) fn paper_note_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let note_id = required_text(payload, "id", 256)?;
    session.lock_key("paper.note", &format!("{user_id}:{note_id}"));
    let changed =
        session.execute("DELETE FROM paper_notes WHERE user_id = ? AND id = ?", &params![user_id, note_id])?;
    Ok(json!({"deleted": changed != 0}))
}

/// `_daily_cost_date(payload, key)`.
fn daily_cost_date(payload: &Payload, key: &str) -> OpResult<String> {
    let value = required_text(payload, key, 10)?;
    let characters: Vec<char> = value.chars().collect();
    let valid = characters.len() == 10 && characters[4] == '-' && characters[7] == '-' && {
        let digits: String = characters[..4].iter().chain(&characters[5..7]).chain(&characters[8..]).collect();
        py_isdigit(&digits)
    };
    if !valid {
        return fail("database_protocol_error", format!("Invalid {key} in storage request"));
    }
    Ok(value)
}

/// `_load(column) or {}`.
fn conversations(row: &Row) -> OpResult<Value> {
    let loaded = codec::load(row.get("conversations_json"))?;
    Ok(if truthy(&loaded) { loaded } else { json!({}) })
}

fn daily_cost_item(row: &Row) -> OpResult<Value> {
    Ok(json!({
        "date": row.get("date").to_json()?,
        "cost": float_or_zero(row, "cost"),
        "conversations": conversations(row)?,
        "computed_at": int_or_zero(row, "computed_at"),
    }))
}

pub(super) fn daily_cost_month(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let year = integer(payload, "year", None, Some(1970), Some(9999))?;
    let month = integer(payload, "month", None, Some(1), Some(12))?;
    let rows = session.fetch_all(
        "SELECT date, cost, conversations_json, computed_at FROM daily_cost_cache WHERE user_id = ? AND date LIKE ? ORDER BY date",
        &params![user_id, format!("{year:04}-{month:02}-%")],
    )?;
    rows.iter().map(daily_cost_item).collect::<OpResult<Vec<_>>>().map(Value::Array)
}

pub(super) fn daily_cost_upsert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let date = daily_cost_date(payload, "date")?;
    let conversations = payload.get("conversations").cloned().unwrap_or_else(|| Value::Object(Map::new()));
    if !conversations.is_object() {
        return fail("database_protocol_error", "Invalid conversations in storage request");
    }
    let cost = number(payload, "cost", 0.0, 1_000_000_000.0)?;
    let computed_at = integer(payload, "computed_at", None, Some(0), None)?;
    session.lock_key("daily.cost", &format!("{user_id}:{date}"));
    session.execute(
        "INSERT INTO daily_cost_cache(user_id, date, cost, conversations_json, computed_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, date) DO UPDATE SET cost = excluded.cost, conversations_json = excluded.conversations_json, computed_at = excluded.computed_at",
        &params![user_id, date, cost, json_text(&conversations)?, computed_at],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn daily_cost_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let count = if get(payload, "date").is_null() {
        session.execute("DELETE FROM daily_cost_cache WHERE user_id = ?", &params![user_id])?
    } else {
        let date = daily_cost_date(payload, "date")?;
        session.lock_key("daily.cost", &format!("{user_id}:{date}"));
        session.execute("DELETE FROM daily_cost_cache WHERE user_id = ? AND date = ?", &params![user_id, date])?
    };
    Ok(json!({"deleted": count}))
}

pub(super) fn daily_cost_persisted_dates(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let Value::Array(values) = get(payload, "dates") else {
        return fail("database_protocol_error", "Invalid dates in storage request");
    };
    let mut dates: Vec<String> = Vec::new();
    for value in values {
        let mut probe = Payload::new();
        probe.insert("date".into(), value.clone());
        let date = daily_cost_date(&probe, "date")?;
        if !dates.contains(&date) {
            dates.push(date);
        }
    }
    if dates.is_empty() {
        return Ok(json!({"dates": []}));
    }
    let mut existing: Vec<String> = Vec::new();
    // One read transaction, portable bind windows, complete owner-scoped result.
    for window in dates.chunks(512) {
        let mut args: Vec<SqlValue> = vec![user_id.into()];
        args.extend(window.iter().map(SqlValue::from));
        let rows = session.fetch_all(
            &format!(
                "SELECT date FROM daily_cost_cache WHERE user_id = ? AND date IN ({})",
                vec!["?"; window.len()].join(",")
            ),
            &args,
        )?;
        existing.extend(rows.iter().filter_map(|row| row.get("date").as_str().map(str::to_owned)));
    }
    Ok(json!({"dates": dates.into_iter().filter(|date| existing.contains(date)).collect::<Vec<_>>()}))
}

pub(super) fn daily_cost_latest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let row = session.fetch_one(
        "SELECT date, cost, conversations_json, computed_at FROM daily_cost_cache WHERE user_id = ? ORDER BY date DESC LIMIT 1",
        &params![user_id],
    )?;
    match row {
        Some(row) => daily_cost_item(&row),
        None => Ok(Value::Null),
    }
}

/// `_paper_podcast_key(payload)`.
fn podcast_key(payload: &Payload) -> OpResult<(i64, String, String, String, String)> {
    Ok((
        integer(payload, "user_id", None, Some(1), None)?,
        required_text(payload, "paper_hash", 128)?,
        required_text(payload, "mode", 64)?,
        required_text(payload, "lang", 32)?,
        optional_text(payload, "voice", "", 256, "paper podcast")?,
    ))
}

const PODCAST_STATUSES: [&str; 6] = ["generating", "interrupted", "done", "script_only", "error", "aborted"];

pub(super) fn paper_podcast_upsert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, paper_hash, mode, lang, voice) = podcast_key(payload)?;
    let script = payload.get("script").cloned().unwrap_or_else(|| json!({}));
    let meta = payload.get("meta").cloned().unwrap_or_else(|| json!({}));
    if !script.is_object() || !meta.is_object() {
        return fail("database_protocol_error", "Invalid paper podcast document");
    }
    let status = required_text(payload, "status", 64)?;
    if !PODCAST_STATUSES.contains(&status.as_str()) {
        return fail("database_protocol_error", "Invalid paper podcast status");
    }
    let now = integer(payload, "updated_at", None, Some(0), None)?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.lock_key(
        "paper.podcast",
        &format!(
            "{user_id}:{}:{paper_hash}{}:{mode}{}:{lang}{voice}",
            paper_hash.chars().count(),
            mode.chars().count(),
            lang.chars().count()
        ),
    );
    let script_json = json_text(&script)?;
    let file_path = optional_text(payload, "file_path", "", 10_000, "paper podcast")?;
    let duration_sec = number(payload, "duration_sec", 0.0, 10_000_000.0)?;
    let model = optional_text(payload, "model", "", 512, "paper podcast")?;
    let tts_model = optional_text(payload, "tts_model", "", 512, "paper podcast")?;
    let meta_json = json_text(&meta)?;
    session.execute(
        "INSERT INTO paper_podcasts(user_id, paper_hash, mode, lang, voice, status, script_json, file_path, duration_sec, model, tts_model, meta, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, paper_hash, mode, lang, voice) DO UPDATE SET status = excluded.status, script_json = excluded.script_json, file_path = excluded.file_path, duration_sec = excluded.duration_sec, model = excluded.model, tts_model = excluded.tts_model, meta = excluded.meta, updated_at = excluded.updated_at",
        &params![
            user_id,
            paper_hash,
            mode,
            lang,
            voice,
            status,
            script_json,
            file_path,
            duration_sec,
            model,
            tts_model,
            meta_json,
            created_at,
            now
        ],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn paper_podcast_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (user_id, paper_hash, mode, lang, voice) = podcast_key(payload)?;
    let row = session.fetch_one(
        "SELECT status, script_json, file_path, duration_sec, model, tts_model, meta, created_at, updated_at FROM paper_podcasts WHERE user_id = ? AND paper_hash = ? AND mode = ? AND lang = ? AND voice = ?",
        &params![user_id, &paper_hash, &mode, &lang, &voice],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let script = codec::load(row.get("script_json"))?;
    let meta = codec::load(row.get("meta"))?;
    if !script.is_object() || !meta.is_object() {
        return fail("database_integrity", "Paper podcast JSON is invalid");
    }
    Ok(json!({
        "user_id": user_id,
        "paper_hash": paper_hash,
        "mode": mode,
        "lang": lang,
        "voice": voice,
        "status": text_or_empty(&row, "status")?,
        "script_json": script,
        "file_path": text_or_empty(&row, "file_path")?,
        "duration_sec": float_or_zero(&row, "duration_sec"),
        "model": text_or_empty(&row, "model")?,
        "tts_model": text_or_empty(&row, "tts_model")?,
        "meta": meta,
        "created_at": int_or_zero(&row, "created_at"),
        "updated_at": int_or_zero(&row, "updated_at"),
    }))
}

pub(super) fn paper_podcast_mark_interrupted(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let updated_at = integer(payload, "updated_at", None, Some(0), None)?;
    let count = session.execute(
        "UPDATE paper_podcasts SET status = 'interrupted', updated_at = ? WHERE status = 'generating'",
        &params![updated_at],
    )?;
    Ok(json!({"changed": count}))
}
