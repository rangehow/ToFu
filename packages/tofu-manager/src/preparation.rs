//! Artifact preparation before any serving process is replaced.
//!
//! The manager imports no application graph: the frontend gate and the
//! storage-engine preparation (including the engine certification gate's
//! decision) run in `serverctl.py prepare-frontend` children with composed
//! deadlines from `serverctl_pkg/restart_budget.py`. Their decision reaches
//! the manager as a `TOFU_ENGINE_DECISION <json>` line on stderr.

use crate::child::{self, Spec};
use crate::envpolicy::{read_dotenv, Env};
use crate::json::Object;
use crate::restart_preparation;
use crate::timing::now;
use serde_json::Value;
use std::path::Path;

pub const DECISION_PREFIX: &str = "TOFU_ENGINE_DECISION ";
const NATIVE_CHILD_SECONDS: f64 = 600.0 + 600.0 + 30.0;
pub const STORAGE_ENGINE_PREFLIGHT_SECONDS: f64 = NATIVE_CHILD_SECONDS + 60.0;
pub const FRONTEND_PREFLIGHT_SECONDS: f64 = 60.0 + 600.0 + 60.0;
pub const ARTIFACT_PREPARATION_SECONDS: f64 =
    STORAGE_ENGINE_PREFLIGHT_SECONDS + FRONTEND_PREFLIGHT_SECONDS;
const PREFLIGHT_TERMINATION_GRACE: f64 = 40.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Parts {
    All,
    Frontend,
    StorageEngine,
}

impl Parts {
    pub fn name(self) -> &'static str {
        match self {
            Parts::All => "all",
            Parts::Frontend => "frontend",
            Parts::StorageEngine => "storage-engine",
        }
    }

    fn timeout(self) -> f64 {
        match self {
            Parts::All => ARTIFACT_PREPARATION_SECONDS,
            Parts::Frontend => FRONTEND_PREFLIGHT_SECONDS,
            Parts::StorageEngine => STORAGE_ENGINE_PREFLIGHT_SECONDS,
        }
    }
}

/// `native_engine.parse_engine_decision`: the last decision line in `text`.
pub fn parse_engine_decision(text: &str) -> Option<Object> {
    let mut found = None;
    for line in text.lines() {
        let Some(rest) = line.strip_prefix(DECISION_PREFIX) else {
            continue;
        };
        if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(rest) {
            if map.get("decision").map(Value::is_string).unwrap_or(false) {
                found = Some(map);
            }
        }
    }
    found
}

pub struct Preflight {
    pub ready: bool,
    pub error: String,
    pub engine_decision: Object,
}

/// `run_frontend_preflight`. A successful native preparation may have
/// replaced the project's managed engine pin; the launch environment adopts
/// it when it still carried the old project value.
pub fn run_frontend_preflight(
    project: &Path,
    python: &str,
    environment: &mut Env,
    operation: &str,
    parts: Parts,
    engine_override: Option<&Value>,
) -> Preflight {
    let command_path = project.join("serverctl.py");
    if !command_path.is_file() {
        return Preflight {
            ready: false,
            error: format!(
                "frontend preflight command is missing: {}",
                command_path.display()
            ),
            engine_decision: Object::new(),
        };
    }
    let mut child_environment = environment.clone();
    child_environment.insert(
        "TOFU_PROJECT_PATH".into(),
        std::fs::canonicalize(project)
            .unwrap_or(project.to_path_buf())
            .to_string_lossy()
            .into_owned(),
    );
    child_environment.insert("TOFU_PYTHON".into(), python.to_owned());
    let project_before = read_dotenv(&project.join(".env")).unwrap_or_default();
    let label = if parts == Parts::StorageEngine {
        "storage engine preflight"
    } else {
        "frontend preflight"
    };
    crate::mlog!(Info, "{label} starting (operation={operation})");
    let started = now();
    let operation_text: String = if operation.is_empty() {
        "manager worker spawn".into()
    } else {
        operation.chars().take(120).collect()
    };
    let mut argv = vec![
        python.to_owned(),
        command_path.to_string_lossy().into_owned(),
        "prepare-frontend".into(),
        "--operation".into(),
        operation_text,
        "--parts".into(),
        parts.name().into(),
    ];
    if let Some(engine_override) = engine_override {
        argv.push("--uncertified-engine-override".into());
        argv.push(crate::json::python_compact_sorted(engine_override));
    }
    let result = child::run(Spec {
        argv,
        cwd: project,
        environment: &child_environment,
        timeout: parts.timeout(),
        progress: Some("Preparing worker artifacts"),
        termination_grace: PREFLIGHT_TERMINATION_GRACE,
        failure_subject: "Worker artifact preparation",
        stdin: None,
    });
    let output = match result {
        Ok(output) => output,
        Err(error) => {
            let decision = parse_engine_decision(&error).unwrap_or_default();
            crate::mlog!(
                Error,
                "{label} failed after {:.1}s (operation={operation}): {error}",
                now() - started
            );
            let error = if error.is_empty() {
                "Worker artifact preparation cancelled".into()
            } else {
                error
            };
            return Preflight {
                ready: false,
                error,
                engine_decision: decision,
            };
        }
    };
    let decision = parse_engine_decision(&output.stderr).unwrap_or_default();
    restart_preparation::log_phases(&output.stderr);
    let project_after = read_dotenv(&project.join(".env")).unwrap_or_default();
    for key in ["TOFUDB_BIN", "TOFUDB_EXPECTED_SHA256"] {
        if let Some(value) = project_after.get(key).filter(|value| !value.is_empty()) {
            let current = environment.get(key);
            if current.is_none() || current == project_before.get(key) {
                environment.insert(key.to_owned(), value.clone());
            }
        }
    }
    crate::mlog!(
        Info,
        "{label} passed in {:.1}s (operation={operation})",
        now() - started
    );
    Preflight {
        ready: true,
        error: String::new(),
        engine_decision: decision,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_last_decision_line_wins_and_noise_is_ignored() {
        let text = "progress\nTOFU_ENGINE_DECISION {\"decision\":\"kept\",\"message\":\"a\"}\nTOFU_ENGINE_DECISION not-json\nTOFU_ENGINE_DECISION {\"decision\":\"refused\",\"message\":\"b\"}\n  TOFU_ENGINE_DECISION {\"decision\":\"indented\"}";
        let decision = parse_engine_decision(text).unwrap();
        assert_eq!(decision["decision"], "refused");
        assert_eq!(decision["message"], "b");
        assert!(parse_engine_decision("Worker artifact preparation failed (exit 1): TOFU_ENGINE_DECISION {\"decision\":\"x\"}").is_none());
    }

    #[test]
    fn budgets_compose_like_restart_budget() {
        assert_eq!(STORAGE_ENGINE_PREFLIGHT_SECONDS, 1290.0);
        assert_eq!(FRONTEND_PREFLIGHT_SECONDS, 720.0);
        assert_eq!(ARTIFACT_PREPARATION_SECONDS, 2010.0);
    }
}
