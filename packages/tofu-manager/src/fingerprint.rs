//! Source generation identity reported by `/health` (`supervisor_protocol`).
//!
//! `serverctl.py` refreshes a manager whose `sourceFingerprint` differs from
//! `supervisor_source_fingerprint(project)`. The Rust manager's behaviour
//! still depends on that Python closure (bridge, preparation children, host
//! programs), so it reports the same digest; its own binary identity is
//! reported separately as `binarySha256`.

use sha2::{Digest, Sha256};
use std::path::Path;

pub const SUPERVISOR_VERSION: &str = "0.3.0";
pub const SUPERVISOR_PROTOCOL_VERSION: u32 = 1;

pub const SUPERVISOR_SOURCE_FILES: [&str; 22] = [
    "supervisor.py",
    "supervisor_protocol.py",
    "server_manager.py",
    "serverctl_pkg/__init__.py",
    "serverctl_pkg/execution_service.py",
    "serverctl_pkg/execution_plan.py",
    "serverctl_pkg/native_engine.py",
    "serverctl_pkg/native_storage_service.py",
    "serverctl_pkg/readiness_liveness.py",
    "serverctl_pkg/resident_service.py",
    "serverctl_pkg/restart_budget.py",
    "runtime_guards.py",
    "runtime_path_policy.py",
    "tofu_workers/__init__.py",
    "tofu_workers/executor.py",
    "tofu_workers/resources.py",
    "bootstrap_pkg/__init__.py",
    "bootstrap_pkg/process_roles.py",
    "bootstrap_pkg/resource_policy_generated.py",
    "tofu_dotenv.py",
    "tofu_db/__init__.py",
    "tofu_db/_loopback.py",
];

/// `supervisor_source_fingerprint`.
pub fn supervisor_source_fingerprint(project: &Path) -> String {
    let project = std::fs::canonicalize(project).unwrap_or(project.to_path_buf());
    let mut digest = Sha256::new();
    for relative in SUPERVISOR_SOURCE_FILES {
        digest.update(relative.as_bytes());
        digest.update(b"\0");
        match std::fs::read(project.join(relative)) {
            Ok(bytes) => digest.update(&bytes),
            Err(error) => {
                let name = match error.kind() {
                    std::io::ErrorKind::NotFound => "FileNotFoundError",
                    std::io::ErrorKind::PermissionDenied => "PermissionError",
                    _ => "OSError",
                };
                digest.update(format!("<unreadable:{name}>").as_bytes());
            }
        }
        digest.update(b"\0");
    }
    digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub fn binary_sha256() -> String {
    std::env::current_exe()
        .ok()
        .and_then(|path| crate::certification::sha256_file(&path).ok())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_closure_is_the_python_protocol_list() {
        let source = std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../supervisor_protocol.py"),
        )
        .unwrap();
        let start = source.find("SUPERVISOR_SOURCE_FILES = (").unwrap();
        let end = start + source[start..].find("\n)").unwrap();
        let listed: Vec<String> = source[start..end]
            .lines()
            .skip(1)
            .filter_map(|line| {
                line.trim()
                    .strip_prefix('\'')
                    .and_then(|l| l.strip_suffix("',"))
                    .map(str::to_owned)
            })
            .collect();
        assert_eq!(listed, SUPERVISOR_SOURCE_FILES.to_vec());
    }
}
