//! Data-root resolution for a managed worker (Linux source checkouts).
//!
//! Mirrors `runtime_path_policy.resolve_runtime_base` (instance lock and
//! heartbeat) and `runtime_guards._persistent_data_path` (storage authority
//! root). Both are re-evaluated on every observation: a fresh checkout's
//! `data/` becomes "populated" when its first worker boots, which moves the
//! answer from the per-user root to the checkout.

use crate::envpolicy::Env;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};

const DATA_ROOT_ENV_KEYS: [&str; 4] = [
    "TOFU_DATA_DIR",
    "TOFU_DATA_LAYOUT",
    "XDG_DATA_HOME",
    "LOCALAPPDATA",
];
const DEFAULT_HEARTBEAT_ROOT: &str = "/tmp/tofu/heartbeat";
const HEARTBEAT_FILE_NAME: &str = "server.heartbeat";

fn populated(path: &Path) -> bool {
    std::fs::read_dir(path)
        .map(|mut entries| entries.next().is_some())
        .unwrap_or(false)
}

fn absolute(raw: &str) -> PathBuf {
    let path = PathBuf::from(raw);
    let joined = if path.is_absolute() {
        path
    } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut out = PathBuf::new();
    for component in joined.components() {
        match component {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

fn per_user_base(environment: &Env) -> PathBuf {
    let base = environment
        .get("XDG_DATA_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(".local/share")
        });
    base.join("Tofu")
}

/// `resolve_runtime_base(environment, repository_root=project, allow_probe=False)`.
pub fn runtime_base(project: &Path, environment: &Env) -> PathBuf {
    if let Some(explicit) = environment
        .get("TOFU_DATA_DIR")
        .filter(|value| !value.is_empty())
    {
        let explicit = absolute(explicit);
        return if explicit.file_name().map(|n| n == "data").unwrap_or(false) {
            explicit.parent().map(Path::to_path_buf).unwrap_or(explicit)
        } else {
            explicit
        };
    }
    let layout = environment
        .get("TOFU_DATA_LAYOUT")
        .map(|v| v.trim().to_lowercase())
        .unwrap_or_default();
    let layout = if layout.is_empty() {
        "auto".to_owned()
    } else {
        layout
    };
    if layout == "intree" {
        return project.to_path_buf();
    }
    if layout != "xdg" && populated(&project.join("data")) {
        return project.to_path_buf();
    }
    absolute(&per_user_base(environment).to_string_lossy())
}

/// `worker_data_directory`: ambient, then the project's `.env`, then forwarded values.
pub fn worker_data_directory(project: &Path, project_dotenv: &Env, server_env: &Env) -> PathBuf {
    let ambient = crate::envpolicy::process_environment();
    let mut environment = Env::new();
    for key in DATA_ROOT_ENV_KEYS {
        for source in [&ambient, project_dotenv, server_env] {
            if let Some(value) = source.get(key).filter(|value| !value.is_empty()) {
                environment.insert(key.to_owned(), value.clone());
            }
        }
    }
    runtime_base(project, &environment).join("data")
}

/// `runtime_guards._persistent_data_path` with `TOFU_PROJECT_PATH=project`.
pub fn storage_data_directory(project: &Path, server_env: &Env) -> PathBuf {
    let mut environment = crate::envpolicy::process_environment();
    environment.extend(server_env.iter().map(|(k, v)| (k.clone(), v.clone())));
    if let Some(explicit) = environment
        .get("TOFU_DATA_DIR")
        .map(|v| v.trim())
        .filter(|v| !v.is_empty())
    {
        let configured = absolute(explicit);
        return if configured.file_name().map(|n| n == "data").unwrap_or(false) {
            configured
        } else {
            configured.join("data")
        };
    }
    let layout = environment
        .get("TOFU_DATA_LAYOUT")
        .map(|v| v.trim().to_lowercase())
        .unwrap_or_default();
    let intree = project.join("data");
    if layout == "intree" {
        return intree;
    }
    let per_user = per_user_base(&environment).join("data");
    if layout == "xdg" {
        return per_user;
    }
    if populated(&intree) {
        intree
    } else {
        per_user
    }
}

/// `instance_heartbeat_path`.
pub fn instance_heartbeat_path(data_directory: &Path, heartbeat_directory: &str) -> PathBuf {
    if !heartbeat_directory.trim().is_empty() {
        return PathBuf::from(heartbeat_directory).join(HEARTBEAT_FILE_NAME);
    }
    let real =
        std::fs::canonicalize(data_directory).unwrap_or_else(|_| data_directory.to_path_buf());
    let identity: String = Sha256::digest(real.to_string_lossy().as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    PathBuf::from(DEFAULT_HEARTBEAT_ROOT)
        .join(identity)
        .join(HEARTBEAT_FILE_NAME)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_data_dir_wins_and_a_populated_checkout_stays_in_tree() {
        let project =
            std::env::temp_dir().join(format!("tofu-manager-paths-{}", std::process::id()));
        std::fs::create_dir_all(project.join("data")).unwrap();
        let mut env = Env::new();
        env.insert("XDG_DATA_HOME".into(), "/xdg".into());
        assert_eq!(runtime_base(&project, &env), PathBuf::from("/xdg/Tofu"));
        std::fs::write(project.join("data/marker"), b"").unwrap();
        assert_eq!(runtime_base(&project, &env), project);
        env.insert("TOFU_DATA_DIR".into(), "/srv/instance/data".into());
        assert_eq!(runtime_base(&project, &env), PathBuf::from("/srv/instance"));
        let mut server_env = Env::new();
        server_env.insert("TOFU_DATA_DIR".into(), "/srv/other".into());
        assert_eq!(
            storage_data_directory(&project, &server_env),
            PathBuf::from("/srv/other/data")
        );
        assert_eq!(
            instance_heartbeat_path(&project, "/hb"),
            PathBuf::from("/hb/server.heartbeat")
        );
        let _ = std::fs::remove_dir_all(&project);
    }
}
