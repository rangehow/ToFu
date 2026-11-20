//! Engine event emission through the generated events_v1 contract.
//!
//! Every event the engine emits is built as JSON and admitted through
//! `tofu_contracts::generated_events_v1::ContractedEvent` (the serde types
//! generated from `contracts/events_v1.json`): an unknown type, a missing
//! required field or a wrong field kind is refused here instead of reaching
//! the durable log. The admitted value is the re-serialised contract type, so
//! field spelling always matches the contract. `seq` is stamped per attempt
//! in emission order (`lib/agent_core/events.py` assigns it the same way).

use serde_json::{Map, Value};
use tofu_contracts::generated_events_v1::ContractedEvent;

#[derive(Debug, Clone, PartialEq)]
pub struct EventRefused {
    pub event_type: String,
    pub reason: String,
}

/// Admit one event through the generated contract and return its wire value.
pub fn admit(event: Value) -> Result<Value, EventRefused> {
    let event_type = event
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let typed: ContractedEvent = serde_json::from_value(event).map_err(|error| EventRefused {
        event_type: event_type.clone(),
        reason: error.to_string(),
    })?;
    serde_json::to_value(typed).map_err(|error| EventRefused {
        event_type,
        reason: error.to_string(),
    })
}

/// Per-attempt emitter: admits events and stamps identity and `seq`.
#[derive(Debug, Clone)]
pub struct Emitter {
    task_id: String,
    attempt_id: String,
    next_seq: i64,
}

impl Emitter {
    pub fn new(task_id: &str, attempt_id: &str) -> Self {
        Self {
            task_id: task_id.to_string(),
            attempt_id: attempt_id.to_string(),
            next_seq: 1,
        }
    }

    pub fn emit(&mut self, event_type: &str, fields: Value) -> Result<Value, EventRefused> {
        let mut object = match fields {
            Value::Object(map) => map,
            Value::Null => Map::new(),
            other => {
                return Err(EventRefused {
                    event_type: event_type.to_string(),
                    reason: format!("event fields must be an object, got {other}"),
                })
            }
        };
        object.insert("type".into(), Value::from(event_type));
        object.insert("taskId".into(), Value::from(self.task_id.as_str()));
        object.insert("attemptId".into(), Value::from(self.attempt_id.as_str()));
        object.insert("seq".into(), Value::from(self.next_seq));
        let admitted = admit(Value::Object(object))?;
        self.next_seq += 1;
        Ok(admitted)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn contracted_event_is_admitted_with_identity_and_sequence() {
        let mut emitter = Emitter::new("task-1", "attempt-1");
        let first = emitter.emit("round_start", json!({"roundNum": 1})).unwrap();
        let second = emitter
            .emit("round_end", json!({"roundNum": 1, "reason": "complete"}))
            .unwrap();
        assert_eq!(first["type"], "round_start");
        assert_eq!(first["seq"], 1);
        assert_eq!(second["seq"], 2);
        assert_eq!(second["attemptId"], "attempt-1");
    }

    #[test]
    fn event_missing_a_required_field_is_refused_before_the_log() {
        let mut emitter = Emitter::new("task-1", "attempt-1");
        let refused = emitter
            .emit("round_end", json!({"roundNum": 1}))
            .unwrap_err();
        assert_eq!(refused.event_type, "round_end");
        // A refused event does not consume a sequence number.
        assert_eq!(
            emitter.emit("round_start", json!({"roundNum": 2})).unwrap()["seq"],
            1
        );
    }

    #[test]
    fn unknown_event_type_is_refused() {
        assert!(admit(json!({"type": "not_a_contract_event"})).is_err());
    }
}
