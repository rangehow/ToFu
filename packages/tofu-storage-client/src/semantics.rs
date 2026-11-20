//! Native wire semantics adapted to the backend-neutral application port
//! (`lib/storage/v2_semantics.py` plus the committed-event envelope check of
//! `StorageV2Client.command`). Rules come from `contracts/storage_v2.json`.

use serde_json::{Map, Value};

use crate::contract::contract;
use crate::error::{StorageError, StorageResult};

/// `^[a-z][a-z0-9_.:-]{0,191}$` (`lib/storage/protocol.py::validate_operation`).
pub fn validate_operation(operation: &str) -> StorageResult<()> {
    let bytes = operation.as_bytes();
    let valid = !bytes.is_empty()
        && bytes.len() <= 192
        && bytes[0].is_ascii_lowercase()
        && bytes[1..].iter().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(byte, b'_' | b'.' | b':' | b'-')
        });
    if valid {
        Ok(())
    } else {
        Err(StorageError::new(
            "database_protocol_error",
            "Invalid storage operation name",
        ))
    }
}

fn exact_integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => None,
    }
}

/// The request body for one attempt: the owner field injected unless the
/// operation's strict contract omits it, and bounded-progress limits clamped
/// to one native maintenance transaction.
pub fn request_payload(
    operation: &str,
    payload: Option<&Map<String, Value>>,
    owner_id: u64,
) -> StorageResult<Map<String, Value>> {
    let contract = contract();
    let mut body = payload.cloned().unwrap_or_default();
    if !contract.owner_omitted_operations.contains(operation) && !body.contains_key("user_id") {
        body.insert("user_id".to_owned(), Value::from(owner_id));
    }
    if let Some(rule) = contract.bounded_progress_operations.get(operation) {
        let limit = match body.get(&rule.request_limit_field) {
            None => Some(rule.default_limit),
            Some(value) => exact_integer(value),
        };
        let limit = match limit {
            Some(limit) if (1..=rule.maximum_limit).contains(&limit) => limit,
            _ => {
                return Err(StorageError::new(
                    "database_protocol_error",
                    format!("Invalid {operation} limit"),
                ))
            }
        };
        body.insert(
            rule.request_limit_field.clone(),
            Value::from(limit.min(rule.native_maximum_limit)),
        );
    }
    Ok(body)
}

/// The application-facing result shape for one operation.
pub fn application_result(operation: &str, result: Value) -> StorageResult<Value> {
    let contract = contract();
    let result = match (operation, result) {
        ("turn.sync.snapshot", Value::Object(mut object)) => {
            if let Some(Value::Object(settings)) = object.get("settings") {
                let public: Map<String, Value> = settings
                    .iter()
                    .filter(|(key, _)| !contract.private_conversation_setting_keys.contains(*key))
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect();
                object.insert("settings".to_owned(), Value::Object(public));
            }
            Value::Object(object)
        }
        (_, result) => result,
    };
    let Some(rule) = contract.bounded_progress_operations.get(operation) else {
        return Ok(result);
    };
    let count = result
        .as_object()
        .and_then(|object| object.get(&rule.result_count_field))
        .and_then(exact_integer);
    match count {
        Some(count) if (0..=rule.native_maximum_limit).contains(&count) => Ok(Value::from(count)),
        _ => Err(StorageError::new(
            "database_protocol_error",
            format!("Malformed {operation} progress response"),
        )),
    }
}

/// Map one engine topic/key hint into the application's observer contract.
pub fn application_notice(
    notice: Map<String, Value>,
    owner_id: Option<u64>,
) -> StorageResult<Value> {
    let contract = contract();
    if notice.get("contract").and_then(Value::as_str) != Some(&contract.commit_notice_contract) {
        return Ok(Value::Object(notice));
    }
    let Some(topic) = notice.get("topic").and_then(Value::as_str) else {
        return Err(StorageError::new(
            "database_protocol_error",
            "Malformed committed storage notification topic",
        ));
    };
    let Some(rule) = contract.notification_topics.get(topic) else {
        return Ok(Value::Object(notice));
    };
    let malformed = || {
        StorageError::new(
            "database_protocol_error",
            "Malformed committed storage notification",
        )
    };
    let owner = notice
        .get("userId")
        .and_then(exact_integer)
        .filter(|owner| *owner > 0)
        .ok_or_else(malformed)?;
    if owner_id.is_some_and(|expected| owner as u64 != expected) {
        return Err(malformed());
    }
    let key = notice
        .get("key")
        .and_then(Value::as_str)
        .filter(|key| !key.is_empty() && key.len() <= contract.max_commit_notice_key_bytes)
        .ok_or_else(malformed)?;
    let sequence = notice
        .get("sequence")
        .and_then(exact_integer)
        .filter(|sequence| *sequence > 0)
        .ok_or_else(malformed)?;
    let mut event = Map::new();
    event.insert(rule.key_field.clone(), Value::from(key));
    event.insert(rule.sequence_field.clone(), Value::from(sequence));
    let mut mapped = Map::new();
    mapped.insert("contract".to_owned(), Value::from(rule.contract.clone()));
    mapped.insert("userId".to_owned(), Value::from(owner));
    mapped.insert("event".to_owned(), Value::Object(event));
    Ok(Value::Object(mapped))
}

/// A command's semantic value and the notices its commit published.
pub struct CommittedResult {
    pub value: Value,
    pub events: Option<Vec<Value>>,
}

/// Split the private committed-events envelope off a command result.
pub fn committed_result(result: Value) -> StorageResult<CommittedResult> {
    let contract = contract();
    let mut object = match result {
        Value::Object(object) if object.contains_key("_storageCommitContract") => object,
        other => {
            return Ok(CommittedResult {
                value: other,
                events: None,
            })
        }
    };
    if object.get("_storageCommitContract").and_then(Value::as_str)
        != Some(contract.committed_events_contract.as_str())
    {
        return Err(StorageError::new(
            "database_protocol_error",
            "Storage committed-event envelope version mismatch",
        ));
    }
    let well_formed = object.contains_key("value")
        && matches!(object.get("events"), Some(Value::Array(events))
            if events.len() <= contract.max_commit_notice_keys
                && events.iter().all(Value::is_object));
    if !well_formed {
        return Err(StorageError::new(
            "database_protocol_error",
            "Malformed storage committed-event envelope",
        ));
    }
    let events = match object.remove("events") {
        Some(Value::Array(events)) => events,
        _ => unreachable!("checked above"),
    };
    Ok(CommittedResult {
        value: object.remove("value").unwrap_or(Value::Null),
        events: Some(events),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn object(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn owner_is_injected_except_for_owner_omitted_operations() {
        let body =
            request_payload("conversation.get", Some(&object(json!({"id": "c"}))), 7).unwrap();
        assert_eq!(Value::Object(body), json!({"id": "c", "user_id": 7}));
        let explicit =
            request_payload("conversation.get", Some(&object(json!({"user_id": 9}))), 7).unwrap();
        assert_eq!(explicit["user_id"], 9);
        assert!(request_payload("system.schema_version", None, 7)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn bounded_progress_requests_and_results_follow_the_contract() {
        let body = request_payload("turn.cleanup", None, 1).unwrap();
        assert_eq!(body["limit"], 64);
        let clamped =
            request_payload("turn.cleanup", Some(&object(json!({"limit": 5000}))), 1).unwrap();
        assert_eq!(clamped["limit"], 64);
        for bad in [json!(0), json!(5001), json!(true), json!(1.5), json!("3")] {
            let error = request_payload("turn.cleanup", Some(&object(json!({"limit": bad}))), 1)
                .unwrap_err();
            assert_eq!(error.message, "Invalid turn.cleanup limit");
        }
        assert_eq!(
            application_result("turn.cleanup", json!({"deletedAttempts": 3})).unwrap(),
            json!(3)
        );
        assert!(application_result("turn.cleanup", json!({"deletedAttempts": 65})).is_err());
    }

    #[test]
    fn snapshot_settings_drop_private_keys() {
        let result = application_result(
            "turn.sync.snapshot",
            json!({"settings": {"model": "m", "_mcpWireDeclarations": [], "_turnId": "t"}}),
        )
        .unwrap();
        assert_eq!(result, json!({"settings": {"model": "m"}}));
    }

    #[test]
    fn operation_names_follow_the_storage_v1_pattern() {
        assert!(validate_operation("turn.sync.snapshot").is_ok());
        assert!(validate_operation("plugin.ns:op-1").is_ok());
        for bad in ["", "Turn", "1x", "a b", &"a".repeat(193)] {
            assert!(validate_operation(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn envelopes_and_notices_are_validated() {
        let contract = contract();
        let envelope = json!({
            "_storageCommitContract": contract.committed_events_contract,
            "value": {"ok": true},
            "events": [{"contract": contract.commit_notice_contract, "topic": "conversation",
                        "userId": 7, "key": "c1", "sequence": 3}],
        });
        let committed = committed_result(envelope).unwrap();
        assert_eq!(committed.value, json!({"ok": true}));
        let notice = application_notice(
            committed.events.unwrap()[0].as_object().unwrap().clone(),
            Some(7),
        )
        .unwrap();
        assert_eq!(
            notice,
            json!({"contract": "storage.conversation-commit/v1", "userId": 7,
                   "event": {"conversationId": "c1", "syncSeq": 3}})
        );
        assert!(committed_result(json!({"_storageCommitContract": "other", "value": 1})).is_err());
        assert!(committed_result(json!({
            "_storageCommitContract": contract.committed_events_contract, "value": 1, "events": [1]
        }))
        .is_err());
    }
}
