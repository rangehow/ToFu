//! Calls into `serverctl_pkg/manager_bridge.py`, the Python policy port.
//!
//! The bridge runs from the manager's source checkout (never the managed
//! project, whose package names a test or an older tree may shadow) and
//! inherits this manager's environment, exactly the snapshot Python's
//! `os.environ` would be inside `server_manager`.

use crate::child::{self, Spec};
use crate::envpolicy::{process_environment, Env};
use crate::json::Object;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// First admission may import from a slow installed environment
/// (`restart_budget.NATIVE_SERVICE_PREPARATION_SECONDS`).
pub const BRIDGE_SECONDS: f64 = 120.0;

#[derive(Clone, Debug, Default)]
pub struct Composition {
    pub environment: Env,
    pub execution_enabled: bool,
}

pub struct Bridge {
    pub python: String,
    pub source_root: PathBuf,
}

fn env_of(value: Option<&Value>) -> Env {
    value
        .and_then(Value::as_object)
        .map(|map| {
            map.iter()
                .map(|(key, value)| {
                    let text = match value {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    };
                    (key.clone(), text)
                })
                .collect()
        })
        .unwrap_or_default()
}

impl Bridge {
    pub fn new(python: String, source_root: PathBuf) -> Self {
        Bridge {
            python,
            source_root,
        }
    }

    pub fn call(&self, request: Value) -> Result<Object, String> {
        let environment = process_environment();
        let operation = request["operation"].as_str().unwrap_or("").to_owned();
        let output = child::run(Spec {
            argv: vec![
                self.python.clone(),
                "-m".into(),
                "serverctl_pkg.manager_bridge".into(),
            ],
            cwd: &self.source_root,
            environment: &environment,
            timeout: BRIDGE_SECONDS,
            progress: None,
            termination_grace: 5.0,
            failure_subject: &format!("Manager policy bridge ({operation})"),
            stdin: Some(request.to_string().into_bytes()),
        });
        let text = match output {
            Ok(output) => output.stdout,
            Err(error) => {
                // A failing bridge prints its structured error on stdout too.
                return Err(error);
            }
        };
        match serde_json::from_str::<Value>(&text) {
            Ok(Value::Object(map)) => match map.get("error").and_then(Value::as_str) {
                Some(error) => Err(error.to_owned()),
                None => Ok(map),
            },
            _ => Err(format!(
                "manager policy bridge ({operation}) returned no JSON object"
            )),
        }
    }

    pub fn server_env_keys(&self) -> Result<BTreeSet<String>, String> {
        let reply = self.call(json!({"operation": "describe"}))?;
        Ok(reply
            .get("serverEnvKeys")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| item.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default())
    }

    /// The worker launch environment, composed afresh for every launch.
    pub fn compose(
        &self,
        project: &Path,
        server_env: &Env,
        manager_values: &Env,
    ) -> Result<Composition, String> {
        let reply = self.call(json!({
            "operation": "compose",
            "project": project.to_string_lossy(),
            "serverEnv": server_env,
            "managerValues": manager_values,
        }))?;
        Ok(Composition {
            environment: env_of(reply.get("environment")),
            execution_enabled: reply
                .get("executionEnabled")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        })
    }

    pub fn retention_refusal(&self, project: &Path, plan: &Value) -> Result<String, String> {
        let reply = self.call(json!({"operation": "retention-refusal", "project": project.to_string_lossy(), "plan": plan}))?;
        Ok(reply
            .get("refusal")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned())
    }

    /// `lib.process_owner.record_exit`; `Ok(None)` while the kernel does not prove the exit yet.
    pub fn record_exit(&self, owner: &Value, directory: &str) -> Result<Option<String>, String> {
        let reply =
            self.call(json!({"operation": "record-exit", "owner": owner, "directory": directory}))?;
        if let Some(path) = reply.get("path").and_then(Value::as_str) {
            return Ok(Some(path.to_owned()));
        }
        Ok(None)
    }
}
