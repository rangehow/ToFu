//! `turn_search_chunks.py`: search-only chunk extraction and the disposable
//! trigram index; no authority writes.

use serde_json::Value;

use super::text::lower;
use crate::error::{Failure, OpResult};
use crate::session::Session;

pub const CHUNK_SCHEMA: [&str; 2] = [
    "CREATE TABLE IF NOT EXISTS storage_search_chunks (\
     user_id BIGINT NOT NULL,conversation_id TEXT NOT NULL,turn_id TEXT NOT NULL,\
     chunk_index BIGINT NOT NULL,ordinal BIGINT NOT NULL,role TEXT NOT NULL,\
     match_field TEXT NOT NULL,search_text TEXT NOT NULL,folded_text TEXT NOT NULL,\
     generation_token TEXT NOT NULL,PRIMARY KEY(user_id,conversation_id,turn_id,chunk_index))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_chunks_owner ON \
     storage_search_chunks(user_id,conversation_id,ordinal,turn_id,chunk_index)",
];

pub const FIELDS: [&str; 4] = ["originalContent", "content", "translatedContent", "thinking"];

/// `field_text(value)`: only public text parts.
pub fn field_text(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| match part {
                Value::Object(map) if map.get("type") == Some(&Value::String("text".into())) => {
                    map.get("text").and_then(Value::as_str)
                }
                _ => None,
            })
            .collect(),
        _ => String::new(),
    }
}

/// One `(index, role, field, text, folded)` chunk.
pub struct Chunk {
    pub index: i64,
    pub role: String,
    pub field: &'static str,
    pub text: String,
    pub folded: String,
}

/// `chunks(projection, actor)`.
pub fn chunks(projection: &Value, actor: &str) -> Vec<Chunk> {
    let Value::Object(projection) = projection else { return Vec::new() };
    let role = match projection.get("role") {
        Some(Value::String(role)) if role == "user" || role == "assistant" => role.clone(),
        _ => {
            if matches!(actor, "human" | "critic" | "virtual_user" | "user") {
                "user".into()
            } else {
                "assistant".into()
            }
        }
    };
    let mut result = Vec::new();
    let mut index = 0i64;
    for field in FIELDS {
        let value: Vec<char> = field_text(projection.get(field)).chars().collect();
        let mut start = 0usize;
        while start < value.len() {
            let end = (start + 4096).min(value.len());
            let text: String = value[start..end].iter().collect();
            let folded = lower(&text);
            result.push(Chunk { index, role: role.clone(), field, text, folded });
            index += 1;
            if start + 4096 >= value.len() {
                break;
            }
            start += 3584;
        }
    }
    result
}

/// `install_fts(session)`: optional acceleration.
pub fn install_fts(session: &Session<'_>) -> OpResult<()> {
    match session.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS storage_search_chunks_fts USING \
         fts5(folded_text,content='storage_search_chunks',content_rowid='rowid',tokenize='trigram')",
        &[],
    ) {
        Ok(_) => {}
        // `except sqlite3.OperationalError: return` (SQLITE_ERROR class).
        Err(Failure::Sqlite(rusqlite::Error::SqliteFailure(error, _)))
            if error.code == rusqlite::ErrorCode::Unknown =>
        {
            return Ok(())
        }
        Err(other) => return Err(other),
    }
    session.execute(
        "CREATE TRIGGER IF NOT EXISTS search_chunks_ai AFTER INSERT ON storage_search_chunks BEGIN \
         INSERT INTO storage_search_chunks_fts(rowid,folded_text) VALUES(new.rowid,new.folded_text); END",
        &[],
    )?;
    session.execute(
        "CREATE TRIGGER IF NOT EXISTS search_chunks_ad AFTER DELETE ON storage_search_chunks BEGIN \
         INSERT INTO storage_search_chunks_fts(storage_search_chunks_fts,rowid,folded_text) \
         VALUES('delete',old.rowid,old.folded_text); END",
        &[],
    )?;
    session.execute(
        "CREATE TRIGGER IF NOT EXISTS search_chunks_au AFTER UPDATE ON storage_search_chunks BEGIN \
         INSERT INTO storage_search_chunks_fts(storage_search_chunks_fts,rowid,folded_text) \
         VALUES('delete',old.rowid,old.folded_text); \
         INSERT INTO storage_search_chunks_fts(rowid,folded_text) VALUES(new.rowid,new.folded_text); END",
        &[],
    )?;
    Ok(())
}
