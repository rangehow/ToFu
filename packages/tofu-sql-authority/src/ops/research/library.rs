//! Paper library (bookshelf) operations (`operations_pkg/_papers.py`).

use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use super::{int_or_zero, optional_text, py_strip, text_or_empty};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const PAPER_FANIN_MAX_PAPERS: usize = 40;
const PAPER_FANIN_MAX_TEXT_CHARS: i64 = 6_000;
const PAPER_QA_MAX_SOURCE_CHARS: i64 = 1_000_000;

const SCOPE: &str = "paper library";

pub(super) fn paper_library_put(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let paper_id = required_text(payload, "id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = optional_text(payload, "paper_hash", "", 128, SCOPE)?;
    session.lock_key("paper.library", &format!("{user_id}:{paper_id}"));
    let title = optional_text(payload, "title", "", 1000, SCOPE)?;
    let pdf_url = optional_text(payload, "pdf_url", "", 10_000, SCOPE)?;
    let pdf_filename = optional_text(payload, "pdf_filename", "", 2000, SCOPE)?;
    let arxiv_id = optional_text(payload, "arxiv_id", "", 256, SCOPE)?;
    let parsed_text = optional_text(payload, "parsed_text", "", 20_000_000, SCOPE)?;
    let parser_version = optional_text(payload, "parser_version", "", 256, SCOPE)?;
    let qa_history = optional_text(payload, "qa_history", "[]", 10_000_000, SCOPE)?;
    let images = optional_text(payload, "images", "[]", 10_000_000, SCOPE)?;
    let babel_cache = optional_text(payload, "babel_cache", "{}", 10_000_000, SCOPE)?;
    let page_count = integer(payload, "page_count", Some(0), Some(0), None)?;
    let folder_id = optional_text(payload, "folder_id", "", 512, SCOPE)?;
    let created_at = integer(payload, "created_at", None, Some(0), None)?;
    let updated_at = integer(payload, "updated_at", None, Some(0), None)?;
    session.execute(
        "INSERT INTO paper_library(id, user_id, title, pdf_url, pdf_filename, arxiv_id, paper_hash, parsed_text, parser_version, qa_history, images, babel_cache, page_count, folder_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id, user_id) DO UPDATE SET title = excluded.title, pdf_url = excluded.pdf_url, pdf_filename = excluded.pdf_filename, arxiv_id = excluded.arxiv_id, paper_hash = excluded.paper_hash, parsed_text = excluded.parsed_text, parser_version = excluded.parser_version, qa_history = excluded.qa_history, images = excluded.images, babel_cache = excluded.babel_cache, page_count = excluded.page_count, folder_id = excluded.folder_id, updated_at = excluded.updated_at",
        &params![
            paper_id,
            user_id,
            title,
            pdf_url,
            pdf_filename,
            arxiv_id,
            paper_hash,
            parsed_text,
            parser_version,
            qa_history,
            images,
            babel_cache,
            page_count,
            folder_id,
            created_at,
            updated_at
        ],
    )?;
    Ok(json!({"saved": true}))
}

pub(super) fn paper_library_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let paper_id = required_text(payload, "id", 256)?;
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    session.lock_key("paper.library", &format!("{user_id}:{paper_id}"));
    let deleted = session.execute("DELETE FROM paper_library WHERE id=? AND user_id=?", &params![paper_id, user_id])?;
    Ok(json!({"deleted": deleted != 0}))
}

pub(super) fn paper_library_recent(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let exclude_hash = optional_text(payload, "exclude_paper_hash", "", 128, SCOPE)?;
    let limit = integer(payload, "limit", Some(40), Some(1), Some(200))?;
    let rows = session.fetch_all(
        "SELECT title, arxiv_id FROM paper_library WHERE user_id = ? AND paper_hash != ? AND title != '' ORDER BY updated_at DESC LIMIT ?",
        &params![user_id, exclude_hash, limit],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        result.push(json!({"title": text_or_empty(&row, "title")?, "arxiv_id": text_or_empty(&row, "arxiv_id")?}));
    }
    Ok(Value::Array(result))
}

/// `_load(column) or default`.
fn load_or(value: &SqlValue, default: Value) -> OpResult<Value> {
    let loaded = codec::load(value)?;
    Ok(if truthy(&loaded) { loaded } else { default })
}

/// `_paper_library_detail_item`.
fn detail_item(row: &Row, include_babel_cache: bool) -> OpResult<Value> {
    let mut item = json!({
        "id": row.get("id").to_json()?,
        "title": text_or_empty(row, "title")?,
        "pdfUrl": text_or_empty(row, "pdf_url")?,
        "pdfFilename": text_or_empty(row, "pdf_filename")?,
        "arxivId": text_or_empty(row, "arxiv_id")?,
        "paperHash": text_or_empty(row, "paper_hash")?,
        "parsedText": text_or_empty(row, "parsed_text")?,
        "qaHistory": load_or(row.get("qa_history"), json!([]))?,
        "images": load_or(row.get("images"), json!([]))?,
        "pageCount": int_or_zero(row, "page_count"),
        "folderId": text_or_empty(row, "folder_id")?,
        "parserVersion": text_or_empty(row, "parser_version")?,
        "createdAt": int_or_zero(row, "created_at"),
        "updatedAt": int_or_zero(row, "updated_at"),
        "hasReport": row.get("has_report").truthy(),
    });
    if include_babel_cache {
        item["babelCache"] = load_or(row.get("babel_cache"), json!({}))?;
    }
    Ok(item)
}

pub(super) fn paper_library_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let rows = session.fetch_all(
        "SELECT library.id, library.title, library.pdf_url, library.pdf_filename, library.arxiv_id, library.paper_hash, library.parsed_text, library.qa_history, library.images, library.babel_cache, library.page_count, library.folder_id, library.parser_version, library.created_at, library.updated_at, EXISTS(SELECT 1 FROM paper_reports AS report WHERE report.user_id=library.user_id AND report.paper_hash=library.paper_hash) AS has_report FROM paper_library AS library WHERE library.user_id = ? ORDER BY library.updated_at DESC",
        &params![user_id],
    )?;
    rows.iter().map(|row| detail_item(row, true)).collect::<OpResult<Vec<_>>>().map(Value::Array)
}

pub(super) fn paper_library_summaries(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let rows = session.fetch_all(
        "SELECT library.id, library.title, library.pdf_url, library.pdf_filename, library.arxiv_id, library.paper_hash, library.page_count, library.folder_id, library.created_at, library.updated_at, EXISTS(SELECT 1 FROM paper_reports AS report WHERE report.user_id=library.user_id AND report.paper_hash=library.paper_hash) AS has_report FROM paper_library AS library WHERE library.user_id = ? ORDER BY library.updated_at DESC",
        &params![user_id],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        result.push(json!({
            "id": row.get("id").to_json()?,
            "title": text_or_empty(&row, "title")?,
            "pdfUrl": text_or_empty(&row, "pdf_url")?,
            "pdfFilename": text_or_empty(&row, "pdf_filename")?,
            "arxivId": text_or_empty(&row, "arxiv_id")?,
            "paperHash": text_or_empty(&row, "paper_hash")?,
            "pageCount": int_or_zero(&row, "page_count"),
            "folderId": text_or_empty(&row, "folder_id")?,
            "createdAt": int_or_zero(&row, "created_at"),
            "updatedAt": int_or_zero(&row, "updated_at"),
            "hasReport": row.get("has_report").truthy(),
        }));
    }
    Ok(Value::Array(result))
}

pub(super) fn paper_library_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_id = required_text(payload, "id", 256)?;
    let row = session.fetch_one(
        "SELECT library.id, library.title, library.pdf_url, library.pdf_filename, library.arxiv_id, library.paper_hash, library.parsed_text, library.qa_history, library.images, library.babel_cache, library.page_count, library.folder_id, library.parser_version, library.created_at, library.updated_at, EXISTS(SELECT 1 FROM paper_reports AS report WHERE report.user_id=library.user_id AND report.paper_hash=library.paper_hash) AS has_report FROM paper_library AS library WHERE library.user_id = ? AND library.id = ?",
        &params![user_id, paper_id],
    )?;
    match row {
        Some(row) => detail_item(&row, true),
        None => Ok(Value::Null),
    }
}

pub(super) fn paper_library_reader(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_id = required_text(payload, "id", 256)?;
    let row = session.fetch_one(
        "SELECT library.id, library.title, library.pdf_url, library.pdf_filename, library.arxiv_id, library.paper_hash, library.parsed_text, library.qa_history, library.images, library.page_count, library.folder_id, library.parser_version, library.created_at, library.updated_at, EXISTS(SELECT 1 FROM paper_reports AS report WHERE report.user_id=library.user_id AND report.paper_hash=library.paper_hash) AS has_report FROM paper_library AS library WHERE library.user_id = ? AND library.id = ?",
        &params![user_id, paper_id],
    )?;
    match row {
        Some(row) => detail_item(&row, false),
        None => Ok(Value::Null),
    }
}

pub(super) fn paper_library_inputs(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let max_text_chars = integer(payload, "max_text_chars", Some(0), Some(0), Some(PAPER_FANIN_MAX_TEXT_CHARS))?;
    let offered = match get(payload, "arxiv_ids") {
        Value::Array(offered) if offered.len() <= PAPER_FANIN_MAX_PAPERS => offered,
        _ => {
            return fail(
                "database_protocol_error",
                format!("paper library inputs require at most {PAPER_FANIN_MAX_PAPERS} arxiv_ids"),
            )
        }
    };
    let mut arxiv_ids: Vec<String> = Vec::new();
    for value in offered {
        let normalized = match value {
            Value::String(text) if !py_strip(text).is_empty() && text.chars().count() <= 256 => py_strip(text),
            _ => return fail("database_protocol_error", "Invalid paper library arxiv_id"),
        };
        if !arxiv_ids.iter().any(|existing| existing == normalized) {
            arxiv_ids.push(normalized.to_owned());
        }
    }
    if arxiv_ids.is_empty() {
        return Ok(json!([]));
    }
    let mut args: Vec<SqlValue> = vec![max_text_chars.into(), user_id.into()];
    args.extend(arxiv_ids.iter().map(SqlValue::from));
    let rows = session.fetch_all(
        &format!(
            "SELECT id, title, arxiv_id, paper_hash, substr(parsed_text, 1, ?) AS parsed_text, length(parsed_text) AS parsed_text_length, parser_version, page_count, folder_id, created_at, updated_at FROM paper_library WHERE user_id=? AND arxiv_id IN ({}) ORDER BY updated_at DESC",
            vec!["?"; arxiv_ids.len()].join(",")
        ),
        &args,
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in rows {
        result.push(json!({
            "id": row.get("id").to_json()?,
            "title": text_or_empty(&row, "title")?,
            "arxivId": text_or_empty(&row, "arxiv_id")?,
            "paperHash": text_or_empty(&row, "paper_hash")?,
            "parsedText": text_or_empty(&row, "parsed_text")?,
            "parsedTextLength": int_or_zero(&row, "parsed_text_length"),
            "parserVersion": text_or_empty(&row, "parser_version")?,
            "pageCount": int_or_zero(&row, "page_count"),
            "folderId": text_or_empty(&row, "folder_id")?,
            "createdAt": int_or_zero(&row, "created_at"),
            "updatedAt": int_or_zero(&row, "updated_at"),
        }));
    }
    Ok(Value::Array(result))
}

fn zero_text_only<T>() -> OpResult<T> {
    fail("database_protocol_error", "Text length may be omitted only for a zero-text projection")
}

pub(super) fn paper_library_identity(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let offered_max = get(payload, "max_text_chars");
    let include_text_length = match payload.get("include_text_length") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "Invalid include_text_length in storage request"),
    };
    let row = if offered_max.is_null() {
        if !include_text_length {
            return zero_text_only();
        }
        session.fetch_one(
            "SELECT title, arxiv_id, parsed_text, length(parsed_text) AS parsed_text_length FROM paper_library WHERE user_id = ? AND paper_hash = ? ORDER BY updated_at DESC LIMIT 1",
            &params![user_id, &paper_hash],
        )?
    } else {
        let max_text_chars = integer(payload, "max_text_chars", None, Some(0), Some(PAPER_QA_MAX_SOURCE_CHARS))?;
        if !include_text_length {
            if max_text_chars != 0 {
                return zero_text_only();
            }
            session.fetch_one(
                "SELECT title, arxiv_id, '' AS parsed_text, 0 AS parsed_text_length FROM paper_library WHERE user_id = ? AND paper_hash = ? ORDER BY updated_at DESC LIMIT 1",
                &params![user_id, &paper_hash],
            )?
        } else {
            session.fetch_one(
                "SELECT title, arxiv_id, substr(parsed_text, 1, ?) AS parsed_text, length(parsed_text) AS parsed_text_length FROM paper_library WHERE user_id = ? AND paper_hash = ? ORDER BY updated_at DESC LIMIT 1",
                &params![max_text_chars, user_id, &paper_hash],
            )?
        }
    };
    match row {
        Some(row) => Ok(json!({
            "title": text_or_empty(&row, "title")?,
            "arxiv_id": text_or_empty(&row, "arxiv_id")?,
            "parsed_text": text_or_empty(&row, "parsed_text")?,
            "parsed_text_length": int_or_zero(&row, "parsed_text_length"),
        })),
        None => Ok(Value::Null),
    }
}

/// `is_placeholder(value)` of `_paper_library_title_backfill`.
fn is_placeholder(value: &SqlValue) -> bool {
    let normalized = py_strip(&value.text_or_empty()).to_lowercase();
    normalized.is_empty() || normalized.starts_with("arxiv:") || normalized.starts_with("arxiv ")
}

pub(super) fn paper_library_title_backfill(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = integer(payload, "user_id", None, Some(1), None)?;
    let paper_hash = required_text(payload, "paper_hash", 128)?;
    let title = py_strip(&required_text(payload, "title", 1000)?).to_owned();
    if title.is_empty() {
        return fail("database_protocol_error", "Invalid title in storage request");
    }
    session.lock_key("paper.library.title", &format!("{user_id}:{paper_hash}"));
    let rows = session.fetch_all(
        "SELECT id, user_id, title FROM paper_library WHERE user_id = ? AND paper_hash = ? ORDER BY updated_at DESC",
        &params![user_id, &paper_hash],
    )?;
    if rows.is_empty() {
        return Ok(json!({"title": title, "updated": 0}));
    }
    let authoritative = rows
        .iter()
        .find(|row| !is_placeholder(row.get("title")))
        .map(|row| py_strip(&row.get("title").text_or_empty()).to_owned())
        .unwrap_or_default();
    let mut updated = 0usize;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    for row in &rows {
        if !is_placeholder(row.get("title")) {
            continue;
        }
        updated += session.execute(
            "UPDATE paper_library SET title = ?, updated_at = ? WHERE id = ? AND user_id = ? AND title = ?",
            &params![&title, now, row.get("id"), user_id, row.get("title")],
        )?;
    }
    let title = if authoritative.is_empty() { title } else { authoritative };
    Ok(json!({"title": title, "updated": updated}))
}
