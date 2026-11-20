//! `operations_pkg/_conversations.py::_conversation_search_op`: served by the
//! turn-search projection (`crate::search`), never the authority session,
//! exactly as `SQLiteBackend.query` routes `conversation.search`.

use serde_json::Value;

use crate::error::OpResult;
use crate::payload::Payload;
use crate::session::Session;

pub fn conversation_search(_authority: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    crate::search::conversation_search(payload)
}
