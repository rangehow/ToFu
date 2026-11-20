//! `contracts/restart_preparation_v1.json`: the restart preparation projection.
//!
//! Preparation children report `TOFU_ENGINE_PROGRESS {"phase": ...}` frames
//! on stderr (`native_engine._report_progress`); the application projects them
//! onto the lifecycle approval record the browser observes. The manager
//! validates the same frames against the contract and records each phase
//! transition in `logs/server-manager.log`.

use serde_json::{json, Value};

pub const PROGRESS_PREFIX: &str = "TOFU_ENGINE_PROGRESS ";
pub const PHASES: [&str; 11] = [
    "checking",
    "waiting",
    "downloading",
    "cached",
    "compiling",
    "retrying",
    "validating",
    "publishing",
    "frontend",
    "prepared",
    "failed",
];

#[derive(Clone, Debug, PartialEq)]
pub struct RestartPreparation {
    pub phase: String,
    pub recovering: Option<bool>,
    pub elapsed_seconds: u64,
    pub remaining_seconds: Option<u64>,
    pub updated_at: f64,
}

impl RestartPreparation {
    /// Project one progress frame onto the contract; `None` for a frame the contract rejects.
    pub fn from_frame(frame: &Value, elapsed_seconds: u64, updated_at: f64) -> Option<Self> {
        let map = frame.as_object()?;
        if map
            .keys()
            .any(|key| !matches!(key.as_str(), "phase" | "recovering" | "remainingSeconds"))
        {
            return None;
        }
        let phase = map.get("phase")?.as_str()?;
        if !PHASES.contains(&phase) {
            return None;
        }
        let recovering = match map.get("recovering") {
            None => None,
            Some(Value::Bool(flag)) => Some(*flag),
            Some(_) => return None,
        };
        let remaining_seconds = match map.get("remainingSeconds") {
            None => None,
            Some(value) => Some(value.as_u64()?),
        };
        Some(RestartPreparation {
            phase: phase.to_owned(),
            recovering,
            elapsed_seconds,
            remaining_seconds,
            updated_at,
        })
    }

    pub fn to_value(&self) -> Value {
        let mut value = json!({"phase": self.phase, "elapsedSeconds": self.elapsed_seconds, "updatedAt": self.updated_at});
        if let Some(recovering) = self.recovering {
            value["recovering"] = json!(recovering);
        }
        if let Some(remaining) = self.remaining_seconds {
            value["remainingSeconds"] = json!(remaining);
        }
        value
    }
}

/// Valid progress frames of a finished preparation's stderr, in order.
pub fn frames(stderr: &str) -> Vec<Value> {
    stderr
        .lines()
        .filter_map(|line| line.strip_prefix(PROGRESS_PREFIX))
        .filter_map(|raw| serde_json::from_str::<Value>(raw).ok())
        .filter(|frame| RestartPreparation::from_frame(frame, 0, 0.0).is_some())
        .collect()
}

/// Record each phase transition a preparation reported.
pub fn log_phases(stderr: &str) {
    let mut last = String::new();
    for frame in frames(stderr) {
        let phase = frame["phase"].as_str().unwrap_or("").to_owned();
        if phase != last {
            crate::mlog!(Info, "restart preparation phase={phase}");
            last = phase;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contract() -> Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../contracts/restart_preparation_v1.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn phases_and_fields_match_the_contract() {
        let schema = contract();
        let enumerated: Vec<&str> = schema["properties"]["phase"]["enum"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(enumerated, PHASES);
        let mut properties: Vec<&String> =
            schema["properties"].as_object().unwrap().keys().collect();
        properties.sort();
        let projected = RestartPreparation::from_frame(
            &json!({"phase": "compiling", "recovering": true, "remainingSeconds": 3}),
            7,
            1.5,
        )
        .unwrap()
        .to_value();
        let mut keys: Vec<&String> = projected.as_object().unwrap().keys().collect();
        keys.sort();
        assert_eq!(keys, properties);
        for required in schema["required"].as_array().unwrap() {
            assert!(projected.get(required.as_str().unwrap()).is_some());
        }
        assert_eq!(schema["additionalProperties"], json!(false));
    }

    #[test]
    fn frames_outside_the_contract_are_rejected() {
        assert!(RestartPreparation::from_frame(&json!({"phase": "booting"}), 0, 0.0).is_none());
        assert!(
            RestartPreparation::from_frame(&json!({"phase": "checking", "extra": 1}), 0, 0.0)
                .is_none()
        );
        assert!(RestartPreparation::from_frame(
            &json!({"phase": "checking", "remainingSeconds": -1}),
            0,
            0.0
        )
        .is_none());
        let text = "TOFU_ENGINE_PROGRESS {\"phase\": \"checking\"}\nnoise\nTOFU_ENGINE_PROGRESS {\"phase\": \"bad\"}\nTOFU_ENGINE_PROGRESS {\"phase\": \"compiling\", \"remainingSeconds\": 9}";
        assert_eq!(frames(text).len(), 2);
    }
}
