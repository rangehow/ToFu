//! Compaction-archive sources for the disposable search worker: a
//! conversation's retained archive IDs and one archive's search documents,
//! read from the caller's pinned snapshot. An archive above one source page
//! is never read and stays explicitly partial.

use super::*;
use crate::generated_conversation_search::SOURCE_PAGE_BYTES;

/// The archive IDs a conversation retains, in index order. Archives are
/// immutable once created, so an ID alone identifies their search content.
pub(crate) fn archive_ids(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
) -> io::Result<Vec<String>> {
    let prefix = conversation_prefix(conversation)?;
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut ids = Vec::new();
    loop {
        let rows = database.entity_scan(
            transaction,
            &start,
            &end,
            crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS,
        )?;
        let Some((last, _)) = rows.last() else {
            return Ok(ids);
        };
        let mut next = last.key_bytes().to_vec();
        next.push(0);
        let row_count = rows.len();
        for (_, value) in &rows {
            ids.push(archive_id_from_index(value)?);
        }
        if row_count < crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS {
            return Ok(ids);
        }
        start = key(
            transaction,
            COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE,
            &next,
        )?;
    }
}

/// What one retained archive contributes to conversation search.
pub(crate) enum SearchSourceArchive {
    /// Larger than one source page: not read, reported as partial coverage.
    Oversized,
    Documents {
        documents: Vec<crate::turn_search_projection::TurnSearchDocument>,
        source_bytes: u64,
    },
}

/// The search documents of one archive: every user/assistant message under
/// its `history:` identity, as conversation search has always named them.
pub(crate) fn archive_documents(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    archive: &str,
) -> io::Result<SearchSourceArchive> {
    let stored_key = document_key(transaction, COMPACTION_ARCHIVE_MESSAGES_NAMESPACE, archive)?;
    let stored = database
        .entity_get(transaction, &stored_key)?
        .ok_or_else(|| invalid_data("archive search source is missing"))?;
    let bytes = crate::versioned_document::stored_document_logical_bytes(
        &stored,
        COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
    )?;
    if bytes > SOURCE_PAGE_BYTES as u64 {
        return Ok(SearchSourceArchive::Oversized);
    }
    let (messages, _) = load_value(
        database,
        transaction,
        COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
        archive,
    )?
    .ok_or_else(|| invalid_data("archive search messages are missing"))?;
    let messages = messages
        .as_array()
        .ok_or_else(|| invalid_data("archive search messages are malformed"))?;
    let mut documents = Vec::new();
    for (index, message) in messages.iter().enumerate() {
        let Some(message) = message.as_object() else {
            return Err(invalid_data("archive search message is malformed"));
        };
        if let Some(role @ ("user" | "assistant")) = message.get("role").and_then(Value::as_str) {
            crate::search_text::chunks(
                &archived_turn_id(archive, index)?,
                index as u64,
                role,
                message,
                &mut documents,
            );
        }
    }
    Ok(SearchSourceArchive::Documents {
        documents,
        source_bytes: bytes,
    })
}

/// The chunk identity of one archived message (`history:` + `[archive, index]`).
pub(crate) fn archived_turn_id(archive: &str, index: usize) -> io::Result<String> {
    Ok(format!(
        "{}{}",
        crate::turn_search_projection::ARCHIVED_SOURCE_PREFIX,
        serde_json::to_string(&json!([archive, index]))
            .map_err(|_| invalid_data("archive search identity cannot be encoded"))?
    ))
}
