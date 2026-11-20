//! Paper reports, second-pass accounting and translations
//! (`operations_pkg/_papers.py`; `lib/paper/report_metadata.py`).

use serde_json::{json, Map, Value};

use super::{int_or_zero, json_text, load_lenient, optional_text, py_strip, text_or_empty};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::float_value;
use crate::params;
use crate::payload::{get, integer, number, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const PAPER_FANIN_MAX_PAPERS: usize = 40;
const PAPER_FANIN_MAX_TEXT_CHARS: i64 = 6_000;
const PAPER_REPORT_REOPEN_MAX_SIBLINGS: usize = 8;
const PAPER_TRANSLATION_MAX_OUTPUT_CHARS: usize = 2_000_000;
const PAPER_TRANSLATION_MAX_OUTPUT_BYTES: usize = 4_000_000;

/// `report_metadata.REPORT_TOKEN_FIELDS`.
const REPORT_TOKEN_FIELDS: [(&str, &str); 5] = [
    ("prompt_tokens", "promptTokens"),
    ("completion_tokens", "completionTokens"),
    ("cache_read_tokens", "cacheReadTokens"),
    ("cache_write_tokens", "cacheWriteTokens"),
    ("reasoning_tokens", "reasoningTokens"),
];

/// `report_metadata.recorded_usage_integer`.
fn recorded_usage_integer(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number.as_i64().or_else(|| number.as_u64().map(|_| i64::MAX)).unwrap_or(0),
        _ => 0,
    }
}

/// `report_metadata._recorded_cost`.
fn recorded_cost(value: Option<&Value>) -> f64 {
    match value {
        Some(Value::Number(number)) => number.as_f64().unwrap_or(0.0),
        _ => 0.0,
    }
}

/// `report_metadata.recompute_report_totals`.
fn recompute_report_totals(metadata: &mut Map<String, Value>) {
    let entries: Vec<Map<String, Value>> = match metadata.get("secondPasses") {
        Some(Value::Object(passes)) => passes.values().filter_map(Value::as_object).cloned().collect(),
        _ => Vec::new(),
    };
    let mut total: Vec<(&str, i64)> =
        REPORT_TOKEN_FIELDS.iter().map(|(key, field)| (*key, recorded_usage_integer(metadata.get(*field)))).collect();
    for entry in &entries {
        if let Some(Value::Object(usage)) = entry.get("usage") {
            for (key, amount) in total.iter_mut() {
                *amount += recorded_usage_integer(usage.get(*key));
            }
        }
    }
    let total: Map<String, Value> =
        total.into_iter().map(|(key, amount)| (key.to_owned(), Value::from(amount))).collect();
    metadata.insert("totalUsage".into(), Value::Object(total));
    for suffix in ["Cny", "Usd"] {
        let field = format!("cost{suffix}");
        let mut passes = 0.0;
        for entry in &entries {
            passes += recorded_cost(entry.get(&field));
        }
        let amount = recorded_cost(metadata.get(&field)) + passes;
        let total_field = format!("totalCost{suffix}");
        if amount != 0.0 || metadata.contains_key(&total_field) {
            metadata.insert(total_field, float_value(amount));
        }
    }
}

/// `report_metadata.merge_report_second_pass`.
fn merge_report_second_pass(metadata: &mut Map<String, Value>, name: &str, entry: Value) {
    if !metadata.get("secondPasses").is_some_and(Value::is_object) {
        metadata.insert("secondPasses".into(), Value::Object(Map::new()));
    }
    if let Some(Value::Object(passes)) = metadata.get_mut("secondPasses") {
        passes.insert(name.to_owned(), entry);
    }
    recompute_report_totals(metadata);
}

fn report_key(user_id: i64, paper_hash: &str, lang: &str) -> String {
    format!("{user_id}:{}:{paper_hash}{lang}", paper_hash.chars().count())
}

pub(super) fn paper_report_upsert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 64)?;
    let meta = payload.get("meta").cloned().unwrap_or_else(|| Value::Object(Map::new()));
    if !meta.is_object() {
        return fail("database_protocol_error", "Invalid paper report metadata");
    }
    session.lock_key("paper.report", &report_key(user_id, &paper_hash, &lang));
    let report = optional_text(payload, "report", "", 10_000_000, "paper report")?;
    let model = optional_text(payload, "model", "", 512, "paper report")?;
    let meta = json_text(&meta)?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.execute(
        "INSERT INTO paper_reports(user_id, paper_hash, lang, report, model, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, paper_hash, lang) DO UPDATE SET report = excluded.report, model = excluded.model, meta = excluded.meta, created_at = excluded.created_at",
        &params![user_id, paper_hash, lang, report, model, meta, created_at],
    )?;
    Ok(json!({"saved": true}))
}

/// `_paper_report_projection`.
fn report_projection(row: &Row, user_id: i64, paper_hash: &str, lang: Value) -> OpResult<Value> {
    let meta = load_lenient(row.get("meta"))?;
    Ok(json!({
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": lang,
        "report": text_or_empty(row, "report")?,
        "model": text_or_empty(row, "model")?,
        "meta": if meta.is_object() { meta } else { json!({}) },
        "created_at": int_or_zero(row, "created_at"),
    }))
}

pub(super) fn paper_report_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 64)?;
    let mut select_projection = "report, model, meta, created_at";
    let mut args: Vec<SqlValue> = params![user_id, &paper_hash, &lang].to_vec();
    if !get(payload, "max_report_chars").is_null() {
        let max_report_chars = integer(payload, "max_report_chars", None, Some(1), Some(PAPER_FANIN_MAX_TEXT_CHARS))?;
        select_projection = "substr(report, 1, ?) AS report, '' AS model, '{}' AS meta, created_at";
        args = params![max_report_chars, user_id, &paper_hash, &lang].to_vec();
    }
    let row = session.fetch_one(
        &format!("SELECT {select_projection} FROM paper_reports WHERE user_id = ? AND paper_hash = ? AND lang = ?"),
        &args,
    )?;
    match row {
        Some(row) => report_projection(&row, user_id, &paper_hash, Value::String(lang)),
        None => Ok(Value::Null),
    }
}

fn placeholders(count: usize) -> String {
    vec!["?"; count].join(",")
}

pub(super) fn paper_report_resolve(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let preferred_lang = required_text(payload, "preferred_lang", 64)?;
    let mut languages = vec![preferred_lang.clone()];
    if !get(payload, "fallback_lang").is_null() {
        let fallback_lang = required_text(payload, "fallback_lang", 64)?;
        if fallback_lang != preferred_lang {
            languages.push(fallback_lang);
        }
    }
    let mut args: Vec<SqlValue> = vec![user_id.into(), paper_hash.as_str().into()];
    args.extend(languages.iter().map(SqlValue::from));
    args.push(preferred_lang.into());
    let row = session.fetch_one(
        &format!(
            "SELECT lang, report, model, meta, created_at FROM paper_reports WHERE user_id = ? AND paper_hash = ? AND lang IN ({}) ORDER BY CASE WHEN lang = ? THEN 0 ELSE 1 END, created_at DESC, lang ASC LIMIT 1",
            placeholders(languages.len())
        ),
        &args,
    )?;
    match row {
        Some(row) => {
            let lang = text_or_empty(&row, "lang")?;
            report_projection(&row, user_id, &paper_hash, lang)
        }
        None => Ok(Value::Null),
    }
}

pub(super) fn paper_report_reopen(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let preferred_lang = required_text(payload, "preferred_lang", 64)?;
    let mut fallback_lang = None;
    if !get(payload, "fallback_lang").is_null() {
        fallback_lang = Some(required_text(payload, "fallback_lang", 64)?);
    }
    let offered_groups = payload.get("sibling_langs_by_base").cloned().unwrap_or_else(|| json!({}));
    let Value::Object(offered_groups) = offered_groups else {
        return fail("database_protocol_error", "paper report reopen sibling groups must be an object");
    };
    let mut base_langs = vec![preferred_lang.clone()];
    if let Some(fallback) = fallback_lang.as_ref().filter(|fallback| **fallback != preferred_lang) {
        base_langs.push(fallback.clone());
    }
    let mut sibling_langs_by_base: Vec<(String, Vec<String>)> = Vec::new();
    let mut offered_sibling_count = 0usize;
    for (base_lang, offered_siblings) in &offered_groups {
        let Value::Array(offered_siblings) = offered_siblings else {
            return fail("database_protocol_error", "Invalid paper report sibling group");
        };
        if !base_langs.contains(base_lang) {
            return fail("database_protocol_error", "Invalid paper report sibling group");
        }
        offered_sibling_count += offered_siblings.len();
        if offered_sibling_count > PAPER_REPORT_REOPEN_MAX_SIBLINGS {
            return fail(
                "database_protocol_error",
                format!("paper report reopen requires at most {PAPER_REPORT_REOPEN_MAX_SIBLINGS} sibling languages"),
            );
        }
        let mut normalized_siblings: Vec<String> = Vec::new();
        for value in offered_siblings {
            let normalized = match value {
                Value::String(text) => py_strip(text),
                _ => "",
            };
            if normalized.is_empty() || normalized.chars().count() > 64 {
                return fail("database_protocol_error", "Invalid paper report sibling language");
            }
            if !normalized_siblings.iter().any(|existing| existing == normalized) {
                normalized_siblings.push(normalized.to_owned());
            }
        }
        sibling_langs_by_base.push((base_lang.clone(), normalized_siblings));
    }
    let siblings_of = |base: &str| -> Vec<String> {
        sibling_langs_by_base.iter().find(|(name, _)| name == base).map(|(_, list)| list.clone()).unwrap_or_default()
    };
    let mut clauses = vec!["p.lang = r.lang".to_owned()];
    let mut clause_args: Vec<SqlValue> = Vec::new();
    for base_lang in &base_langs {
        let siblings = siblings_of(base_lang);
        if siblings.is_empty() {
            continue;
        }
        clauses.push(format!("(r.lang = ? AND p.lang IN ({}))", placeholders(siblings.len())));
        clause_args.push(base_lang.into());
        clause_args.extend(siblings.iter().map(SqlValue::from));
    }
    let mut args: Vec<SqlValue> = vec![user_id.into(), paper_hash.as_str().into()];
    args.extend(base_langs.iter().map(SqlValue::from));
    args.push(preferred_lang.as_str().into());
    args.push(user_id.into());
    args.push(paper_hash.as_str().into());
    args.extend(clause_args);
    let rows = session.fetch_all(
        &format!(
            "WITH resolved_lang AS (SELECT lang FROM paper_reports WHERE user_id = ? AND paper_hash = ? AND lang IN ({}) ORDER BY CASE WHEN lang = ? THEN 0 ELSE 1 END, created_at DESC, lang ASC LIMIT 1) SELECT p.lang, p.report, p.model, p.meta, p.created_at FROM paper_reports AS p JOIN resolved_lang AS r ON 1 = 1 WHERE p.user_id = ? AND p.paper_hash = ? AND ({}) ORDER BY p.lang ASC",
            placeholders(base_langs.len()),
            clauses.join(" OR ")
        ),
        &args,
    )?;
    let mut rows_by_lang: Vec<(String, Row)> = Vec::new();
    for row in rows {
        let lang = row.get("lang").text_or_empty();
        match rows_by_lang.iter_mut().find(|(existing, _)| *existing == lang) {
            Some(slot) => slot.1 = row,
            None => rows_by_lang.push((lang, row)),
        }
    }
    let find = |lang: &str| rows_by_lang.iter().find(|(existing, _)| existing == lang).map(|(_, row)| row);
    let mut resolved_lang = if find(&preferred_lang).is_some() { preferred_lang.clone() } else { String::new() };
    if resolved_lang.is_empty() {
        if let Some(fallback) = fallback_lang.as_ref().filter(|fallback| find(fallback).is_some()) {
            resolved_lang = fallback.clone();
        }
    }
    let resolved = match find(&resolved_lang).filter(|_| !resolved_lang.is_empty()) {
        Some(row) => report_projection(row, user_id, &paper_hash, Value::String(resolved_lang.clone()))?,
        None => Value::Null,
    };
    let mut siblings = Vec::new();
    for lang in siblings_of(&resolved_lang) {
        if let Some(row) = find(&lang) {
            siblings.push(report_projection(row, user_id, &paper_hash, Value::String(lang.clone()))?);
        }
    }
    Ok(json!({"report": resolved, "siblings": siblings}))
}

pub(super) fn paper_report_excerpts(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let lang = required_text(payload, "lang", 64)?;
    let max_report_chars = integer(payload, "max_report_chars", None, Some(1), Some(PAPER_FANIN_MAX_TEXT_CHARS))?;
    let offered = match get(payload, "paper_hashes") {
        Value::Array(offered) if offered.len() <= PAPER_FANIN_MAX_PAPERS => offered,
        _ => {
            return fail(
                "database_protocol_error",
                format!("paper report excerpts require at most {PAPER_FANIN_MAX_PAPERS} paper_hashes"),
            )
        }
    };
    let mut paper_hashes: Vec<String> = Vec::new();
    for value in offered {
        let valid = match value {
            Value::String(text) if !py_strip(text).is_empty() && text.chars().count() <= 128 => py_strip(text),
            _ => return fail("database_protocol_error", "Invalid paper report hash"),
        };
        if !paper_hashes.iter().any(|existing| existing == valid) {
            paper_hashes.push(valid.to_owned());
        }
    }
    if paper_hashes.is_empty() {
        return Ok(json!([]));
    }
    let mut args: Vec<SqlValue> = vec![max_report_chars.into(), user_id.into(), lang.as_str().into()];
    args.extend(paper_hashes.iter().map(SqlValue::from));
    let rows = session.fetch_all(
        &format!(
            "SELECT paper_hash, substr(report, 1, ?) AS report, created_at FROM paper_reports WHERE user_id=? AND lang=? AND paper_hash IN ({}) ORDER BY paper_hash ASC",
            placeholders(paper_hashes.len())
        ),
        &args,
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        result.push(json!({
            "user_id": user_id,
            "paper_hash": row.get("paper_hash").to_json()?,
            "lang": lang,
            "report": text_or_empty(&row, "report")?,
            "created_at": int_or_zero(&row, "created_at"),
        }));
    }
    Ok(Value::Array(result))
}

pub(super) fn paper_report_latest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let row = session.fetch_one(
        "SELECT lang, report, model, meta, created_at FROM paper_reports WHERE user_id = ? AND paper_hash = ? ORDER BY created_at DESC, lang ASC LIMIT 1",
        &params![user_id, &paper_hash],
    )?;
    match row {
        Some(row) => {
            let lang = text_or_empty(&row, "lang")?;
            report_projection(&row, user_id, &paper_hash, lang)
        }
        None => Ok(Value::Null),
    }
}

/// The current report metadata document, `{}` when undecodable or not a dict.
fn locked_meta(
    session: &Session<'_>,
    user_id: i64,
    paper_hash: &str,
    lang: &str,
) -> OpResult<Option<Map<String, Value>>> {
    session.lock_key("paper.report.meta", &report_key(user_id, paper_hash, lang));
    let row = session.fetch_one(
        "SELECT meta FROM paper_reports WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        &params![user_id, paper_hash, lang],
    )?;
    let Some(row) = row else { return Ok(None) };
    Ok(Some(match load_lenient(row.get("meta"))? {
        Value::Object(map) => map,
        _ => Map::new(),
    }))
}

fn store_meta(
    session: &Session<'_>,
    current: Map<String, Value>,
    user_id: i64,
    paper_hash: &str,
    lang: &str,
) -> OpResult<Value> {
    let current = Value::Object(current);
    session.execute(
        "UPDATE paper_reports SET meta = ? WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        &params![json_text(&current)?, user_id, paper_hash, lang],
    )?;
    Ok(json!({"found": true, "meta": current}))
}

pub(super) fn paper_report_second_pass_merge(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 64)?;
    let name = required_text(payload, "name", 64)?;
    let entry = get(payload, "entry");
    if !entry.is_object() {
        return fail("database_protocol_error", "Invalid paper second-pass entry");
    }
    // Validate and detach the entire document before taking the row lock.
    let entry = codec::load(&SqlValue::Blob(codec::dump(entry)?))?;
    let Some(mut current) = locked_meta(session, user_id, &paper_hash, &lang)? else {
        return Ok(json!({"found": false, "meta": null}));
    };
    merge_report_second_pass(&mut current, &name, entry);
    store_meta(session, current, user_id, &paper_hash, &lang)
}

pub(super) fn paper_report_second_pass_accumulate(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 64)?;
    let name = required_text(payload, "name", 64)?;
    let Value::Object(raw_usage) = get(payload, "usage") else {
        return fail("database_protocol_error", "Invalid paper second-pass usage");
    };
    let mut usage = Vec::with_capacity(REPORT_TOKEN_FIELDS.len());
    for (key, _) in REPORT_TOKEN_FIELDS {
        usage.push((key, integer(raw_usage, key, Some(0), Some(0), Some(10_000_000_000))?));
    }
    let mut incremental_costs = Vec::with_capacity(2);
    for suffix in ["Cny", "Usd"] {
        let field = format!("cost{suffix}");
        let increment = if payload.contains_key(&field) { number(payload, &field, 0.0, 1_000_000_000.0)? } else { 0.0 };
        incremental_costs.push((field, increment));
    }
    let Some(mut current) = locked_meta(session, user_id, &paper_hash, &lang)? else {
        return Ok(json!({"found": false, "meta": null}));
    };
    if !current.get("secondPasses").is_some_and(Value::is_object) {
        current.insert("secondPasses".into(), Value::Object(Map::new()));
    }
    let Some(Value::Object(passes)) = current.get_mut("secondPasses") else { unreachable!("secondPasses is a dict") };
    if !passes.get(&name).is_some_and(Value::is_object) {
        passes.insert(name.clone(), Value::Object(Map::new()));
    }
    let Some(Value::Object(entry)) = passes.get_mut(&name) else { unreachable!("pass entry is a dict") };
    let previous_usage = match entry.get("usage") {
        Some(Value::Object(previous)) => previous.clone(),
        _ => Map::new(),
    };
    let next_usage: Map<String, Value> = usage
        .iter()
        .map(|(key, amount)| {
            ((*key).to_owned(), Value::from(recorded_usage_integer(previous_usage.get(*key)) + amount))
        })
        .collect();
    entry.insert("usage".into(), Value::Object(next_usage));
    let calls = recorded_usage_integer(entry.get("calls")) + 1;
    entry.insert("calls".into(), Value::from(calls));
    for (field, increment) in incremental_costs {
        let prior = recorded_cost(entry.get(&field));
        if prior != 0.0 || increment != 0.0 {
            entry.insert(field, float_value(prior + increment));
        }
    }
    recompute_report_totals(&mut current);
    store_meta(session, current, user_id, &paper_hash, &lang)
}

/// `_paper_translation_text`.
fn paper_translation_text(payload: &Payload) -> OpResult<String> {
    let text = optional_text(payload, "text", "", PAPER_TRANSLATION_MAX_OUTPUT_CHARS, "paper translation")?;
    if text.len() > PAPER_TRANSLATION_MAX_OUTPUT_BYTES {
        return fail(
            "storage_payload_too_large",
            format!("Paper translation exceeds {PAPER_TRANSLATION_MAX_OUTPUT_BYTES} UTF-8 bytes"),
        );
    }
    Ok(text)
}

pub(super) fn paper_translation_upsert(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 128)?;
    session.lock_key("paper.translation", &report_key(user_id, &paper_hash, &lang));
    let text = paper_translation_text(payload)?;
    let model = optional_text(payload, "model", "", 512, "paper translation")?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    session.execute(
        "INSERT INTO paper_translations(user_id, paper_hash, lang, text, model, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, paper_hash, lang) DO UPDATE SET text = excluded.text, model = excluded.model, created_at = excluded.created_at",
        &params![user_id, paper_hash, lang, text, model, created_at],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn paper_translation_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let lang = required_text(payload, "lang", 128)?;
    let row = session.fetch_one(
        "SELECT substr(text, 1, ?) AS bounded_text, model, created_at FROM paper_translations WHERE user_id = ? AND paper_hash = ? AND lang = ?",
        &params![(PAPER_TRANSLATION_MAX_OUTPUT_CHARS + 1) as i64, user_id, &paper_hash, &lang],
    )?;
    let Some(row) = row else { return Ok(Value::Null) };
    let text = text_or_empty(&row, "bounded_text")?;
    if let Value::String(content) = &text {
        if content.chars().count() > PAPER_TRANSLATION_MAX_OUTPUT_CHARS
            || content.len() > PAPER_TRANSLATION_MAX_OUTPUT_BYTES
        {
            return Ok(Value::Null);
        }
    }
    Ok(json!({
        "user_id": user_id,
        "paper_hash": paper_hash,
        "lang": lang,
        "text": text,
        "model": text_or_empty(&row, "model")?,
        "created_at": int_or_zero(&row, "created_at"),
    }))
}
