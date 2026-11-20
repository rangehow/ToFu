//! Private ports of `operations_pkg/_queue.py::{acknowledge_background_command_receipt,
//! retire_background_command_receipts, _validated_background_delivery_ids}`
//! (shared owner: queue). The final removal is the queue domain's registered
//! `queue.injection.ack` handler, which Python calls directly
//! (`_queue_injection_ack`) inside the same transaction.

use std::collections::HashMap;

use serde_json::{json, Map, Value};

use crate::codec::{load, py_eq};
use crate::error::{fail, internal, OpResult};
use crate::json::truthy;
use crate::payload::required_text;
use crate::session::Session;

const KIND_WORKFLOW: &str = "workflow_step";
const KIND_BACKGROUND_RECEIPT: &str = "background_receipt";

/// `projection.get('_bgCommandInjects', [])` iterated: only mapping items can
/// name a block; iterating a non-iterable raises TypeError.
fn inject_items(projection: &Map<String, Value>) -> OpResult<Vec<&Map<String, Value>>> {
    match projection.get("_bgCommandInjects") {
        None | Some(Value::String(_)) | Some(Value::Object(_)) => Ok(vec![]),
        Some(Value::Array(items)) => Ok(items.iter().filter_map(Value::as_object).collect()),
        Some(_) => internal("TypeError: _bgCommandInjects is not iterable"),
    }
}

fn block_items<'p>(projection: &'p Map<String, Value>, block_id: &str) -> OpResult<Vec<&'p Map<String, Value>>> {
    let block = Value::String(block_id.to_owned());
    Ok(inject_items(projection)?
        .into_iter()
        .filter(|item| py_eq(item.get("blockId").unwrap_or(&Value::Null), &block))
        .collect())
}

/// `_validated_background_delivery_ids`: queue IDs in delivery order.
fn validated_background_delivery_ids(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    member: &Value,
    projection: &Map<String, Value>,
) -> OpResult<Vec<String>> {
    let Value::Object(acknowledgement) = member else {
        return fail("database_protocol_error", "Invalid background acknowledgement");
    };
    let block_id = required_text(acknowledgement, "block_id", 256)?;
    let Some(Value::Array(deliveries)) = acknowledgement.get("deliveries") else {
        return fail("database_protocol_error", "Invalid background delivery batch");
    };
    let mut order: Vec<String> = Vec::new();
    let mut identities: HashMap<String, String> = HashMap::new();
    for delivery in deliveries {
        let Value::Object(delivery) = delivery else {
            return fail("database_protocol_error", "Invalid background delivery identity");
        };
        let queue_id = required_text(delivery, "queue_id", 256)?;
        let command_id = required_text(delivery, "command_id", 128)?;
        if identities.contains_key(&queue_id) {
            return fail("database_protocol_error", "Duplicate background delivery identity");
        }
        identities.insert(queue_id.clone(), command_id);
        order.push(queue_id);
    }
    let receipts = block_items(projection, &block_id)?;
    let previews = match receipts.as_slice() {
        [receipt] => match receipt.get("previews") {
            Some(Value::Array(previews)) => previews,
            _ => return fail("database_protocol_error", "Background acknowledgement requires its receipt"),
        },
        _ => return fail("database_protocol_error", "Background acknowledgement requires its receipt"),
    };
    let mut receipt_ids: HashMap<String, String> = HashMap::new();
    for preview in previews {
        let Value::Object(preview) = preview else {
            return fail("database_protocol_error", "Invalid background receipt preview");
        };
        if preview.contains_key("queueId") {
            let queue_id = required_text(preview, "queueId", 256)?;
            if receipt_ids.contains_key(&queue_id) {
                return fail("database_protocol_error", "Duplicate background receipt identity");
            }
            receipt_ids.insert(queue_id, required_text(preview, "commandId", 128)?);
        }
    }
    if identities != receipt_ids {
        return fail("database_protocol_error", "Background receipt identities disagree");
    }
    if identities.is_empty() {
        return Ok(vec![]);
    }
    session.lock_key("queue-conversation", &format!("{user_id}:{conv_id}"));
    let placeholders = vec!["?"; order.len()].join(",");
    let mut args = vec![conv_id.into(), user_id.into()];
    args.extend(order.iter().map(Into::into));
    let rows = session.fetch_all(
        &format!(
            "SELECT id,kind,payload_json,input_turn_id,output_turn_id,attempt_id FROM storage_queue_items WHERE conv_id=? AND user_id=? AND id IN ({placeholders})"
        ),
        &args,
    )?;
    for row in &rows {
        let loaded = load(row.get("payload_json"))?;
        let message = if truthy(&loaded) { loaded } else { json!({}) };
        let kind = row.text("kind");
        let expected = identities.get(&row.text("id")).cloned();
        let matches = match &message {
            Value::Object(message) => {
                !truthy(message.get("_peerHuman").unwrap_or(&Value::Null))
                    && match expected {
                        Some(command_id) => {
                            py_eq(message.get("_backgroundCommand").unwrap_or(&Value::Null), &Value::String(command_id))
                        }
                        None => return internal(format!("KeyError: {}", row.text("id"))),
                    }
            }
            _ => false,
        };
        if !(kind == KIND_WORKFLOW || kind == KIND_BACKGROUND_RECEIPT)
            || !matches
            || ["input_turn_id", "output_turn_id", "attempt_id"].iter().any(|key| row.get(key).truthy())
        {
            return fail("database_conflict", "Background delivery intent no longer matches receipt");
        }
    }
    Ok(order)
}

/// `_queue_injection_ack(session, {...})['removedQueueIds']`.
fn injection_ack(session: &Session<'_>, conv_id: &str, user_id: i64, queue_ids: Vec<String>) -> OpResult<Vec<Value>> {
    let Some(spec) = crate::ops::registry().get("queue.injection.ack") else {
        return internal("queue.injection.ack is not registered");
    };
    let mut payload = Map::new();
    payload.insert("conv_id".into(), json!(conv_id));
    payload.insert("user_id".into(), json!(user_id));
    payload.insert("queue_ids".into(), json!(queue_ids));
    let result = (spec.handler)(session, &payload)?;
    match result.get("removedQueueIds") {
        Some(Value::Array(ids)) => Ok(ids.clone()),
        _ => internal("queue.injection.ack returned no removedQueueIds"),
    }
}

/// `acknowledge_background_command_receipt`.
pub fn acknowledge_background_command_receipt(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    acknowledgement: &Value,
    projection: &Map<String, Value>,
) -> OpResult<Vec<Value>> {
    let queue_ids = validated_background_delivery_ids(session, conv_id, user_id, acknowledgement, projection)?;
    if queue_ids.is_empty() {
        return Ok(vec![]);
    }
    injection_ack(session, conv_id, user_id, queue_ids)
}

/// `retire_background_command_receipts`.
pub fn retire_background_command_receipts(
    session: &Session<'_>,
    conv_id: &str,
    user_id: i64,
    retirement: &Value,
    projection: &Map<String, Value>,
) -> OpResult<Vec<Value>> {
    let Value::Object(fields) = retirement else {
        return fail("database_protocol_error", "Invalid background receipt retirement");
    };
    match fields.get("disposition") {
        Some(Value::Array(_) | Value::Object(_)) => return internal("TypeError: unhashable disposition"),
        Some(Value::String(disposition)) if disposition == "settled_as_evidence" => {}
        _ => return fail("database_protocol_error", "Invalid background receipt retirement"),
    }
    if !matches!(fields.get("deliveries"), Some(Value::Array(items)) if !items.is_empty()) {
        return fail("database_protocol_error", "Background retirement names no receipt");
    }
    let queue_ids = validated_background_delivery_ids(session, conv_id, user_id, retirement, projection)?;
    let block_id = fields.get("block_id").and_then(Value::as_str).unwrap_or("");
    let Some(block) = block_items(projection, block_id)?.into_iter().next() else {
        return internal("StopIteration");
    };
    let round_ok = match block.get("round") {
        Some(Value::Number(number)) => number.as_f64() == Some(0.0),
        _ => false,
    };
    if !round_ok {
        return fail("database_protocol_error", "Background retirement requires round-0 evidence");
    }
    injection_ack(session, conv_id, user_id, queue_ids)
}
