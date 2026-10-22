//! Metadata-only Turn reads over the existing versioned-document byte codec.
//!
//! Entry point: get. Physical owner/tenant scope, logical Turn identity, JSON
//! grammar/depth and byte admission stay with the current authority readers.
//! Only public fields and the two normalization inputs are decoded; projection
//! and private head bodies are skipped, never reconstructed or cached here.
//! Legacy monolithic documents still require bounded whole-byte validation;
//! the existing codec retains its Serde-marker compatibility fallback.

use super::*;
use serde::de::{Deserialize, Deserializer, IgnoredAny, MapAccess, Visitor};

pub(super) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let (logical_key, bytes) = crate::versioned_document::materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    if logical_key != turn_logical_key(conversation_id, turn_id) {
        return Err(invalid_data("Turn metadata document identity differs"));
    }
    // The byte reader above has already applied the strict JSON/depth fence.
    // IgnoredAny is only a selective decode, never a JSON-validation authority.
    let Metadata(document) =
        serde_json::from_slice(&bytes).map_err(|_| invalid_data("Turn metadata is malformed"))?;
    validate_metadata(&document, conversation_id, turn_id)?;
    encode_response(&public_turn(document, execution_epoch)?).map(Some)
}

struct Metadata(Map<String, Value>);

impl<'de> Deserialize<'de> for Metadata {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_map(MetadataVisitor)
    }
}

struct MetadataVisitor;

impl<'de> Visitor<'de> for MetadataVisitor {
    type Value = Metadata;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a Turn metadata object")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut fields: A) -> Result<Self::Value, A::Error> {
        let mut document = Map::new();
        while let Some(key) = fields.next_key::<String>()? {
            match key.as_str() {
                "turnId"
                | "presentationId"
                | "conversationId"
                | "laneId"
                | "parentTurnId"
                | "ordinal"
                | "actor"
                | "kind"
                | "runId"
                | "status"
                | "currentAttemptId"
                | "projectionRevision"
                | "modelContextRevision"
                | "settlement"
                | "createdAt"
                | "updatedAt"
                | "_executionEpoch"
                | "_modelContextDisplayOffset" => {
                    // As in Value, the final duplicate field wins. Validate the
                    // resulting metadata, not an overwritten legacy spelling.
                    document.insert(key, fields.next_value::<Value>()?);
                }
                _ => {
                    fields.next_value::<IgnoredAny>()?;
                }
            }
        }
        Ok(Metadata(document))
    }
}

fn validate_metadata(
    document: &Map<String, Value>,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<()> {
    if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || document.get("turnId").and_then(Value::as_str) != Some(turn_id)
    {
        return Err(invalid_data("Turn metadata identity differs"));
    }
    for field in ["laneId", "actor", "kind", "runId", "status"] {
        if document.get(field).and_then(Value::as_str).is_none() {
            return Err(invalid_data("Turn metadata text field is malformed"));
        }
    }
    // Earlier Turn documents can omit presentationId and nullable pointers.
    // Retain absence exactly; never manufacture a second public normalizer.
    if document
        .get("presentationId")
        .is_some_and(|value| !value.is_string())
    {
        return Err(invalid_data("Turn presentation identity is malformed"));
    }
    for field in ["parentTurnId", "currentAttemptId"] {
        if document
            .get(field)
            .is_some_and(|value| !value.is_null() && !value.is_string())
        {
            return Err(invalid_data("Turn metadata pointer is malformed"));
        }
    }
    for field in ["ordinal", "projectionRevision", "createdAt", "updatedAt"] {
        if document.get(field).and_then(Value::as_u64).is_none() {
            return Err(invalid_data("Turn metadata integer field is malformed"));
        }
    }
    for field in [
        "modelContextRevision",
        "_executionEpoch",
        "_modelContextDisplayOffset",
    ] {
        if document
            .get(field)
            .is_some_and(|value| value.as_u64().is_none())
        {
            return Err(invalid_data(
                "Turn metadata normalization input is malformed",
            ));
        }
    }
    // Validate stored settlement even when an epoch transition will replace it.
    if document
        .get("settlement")
        .and_then(Value::as_object)
        .is_none()
    {
        return Err(invalid_data("turn settlement is malformed"));
    }
    Ok(())
}
