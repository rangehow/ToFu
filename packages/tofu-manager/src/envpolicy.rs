//! Launch-environment admission shared with `server_manager`.
//!
//! `.env` parsing follows `tofu_dotenv.parse_dotenv`; the production guard
//! follows `production_server_environment_error`. The allow-list of
//! forwardable keys (`SERVER_ENV_KEYS`) is read from the Python owner through
//! the bridge, so both managers admit exactly the same names.

use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};

pub type Env = BTreeMap<String, String>;

pub const DEFAULT_SERVER_PORT: u16 = 15000;
const MAX_DOTENV_BYTES: u64 = 256 * 1024;
const DATA_LOCATION_ENV_KEYS: [&str; 3] = ["TOFU_DATA_DIR", "XDG_DATA_HOME", "LOCALAPPDATA"];
const PYTEST_CONTEXT_ENV_KEYS: [&str; 2] = ["PYTEST_CURRENT_TEST", "TOFU_PYTEST_RUN_ROOT"];

fn valid_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// `tofu_dotenv.parse_dotenv`.
pub fn parse_dotenv(text: &str) -> Env {
    let mut values = Env::new();
    for raw in text.lines() {
        let line = raw.trim().trim_start_matches('\u{feff}');
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((name, value)) = line.split_once('=') else {
            continue;
        };
        let name = name.trim();
        if !valid_name(name) {
            continue;
        }
        let value = value.trim().trim_matches('"').trim_matches('\'');
        values.insert(name.to_owned(), value.to_owned());
    }
    values
}

/// `tofu_dotenv.read_dotenv_values`; `Err` carries the warning text.
pub fn read_dotenv(path: &Path) -> Result<Env, String> {
    let metadata = match std::fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Env::new()),
        Err(error) => return Err(error.to_string()),
    };
    if metadata.len() > MAX_DOTENV_BYTES {
        return Err(format!(
            "{}: .env exceeds the {MAX_DOTENV_BYTES}-byte limit",
            path.display()
        ));
    }
    let bytes = std::fs::read(path).map_err(|error| error.to_string())?;
    let text = String::from_utf8(bytes)
        .map_err(|_| format!("{}: .env must be valid UTF-8", path.display()))?;
    Ok(parse_dotenv(&text))
}

fn lexical_absolute(project: &Path, raw: &str) -> PathBuf {
    let expanded = if let Some(rest) = raw.strip_prefix("~/") {
        PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(rest)
    } else {
        PathBuf::from(raw)
    };
    let joined = if expanded.is_absolute() {
        expanded
    } else {
        project.join(expanded)
    };
    std::fs::canonicalize(&joined).unwrap_or_else(|_| {
        let mut out = PathBuf::new();
        for component in joined.components() {
            match component {
                Component::ParentDir => {
                    out.pop();
                }
                Component::CurDir => {}
                other => out.push(other),
            }
        }
        out
    })
}

/// `_path_is_pytest_owned`.
pub fn path_is_pytest_owned(raw: &str, project: &Path) -> bool {
    let value = raw.trim();
    if value.is_empty() {
        return false;
    }
    let resolved = lexical_absolute(project, value);
    resolved.components().any(|component| {
        let part = component.as_os_str().to_string_lossy();
        part.starts_with("tofu-pytest-runs-")
            || part.starts_with("tofu-test-data-")
            || part.starts_with("tofu-test-storage-")
            || part.starts_with("pytest-of-")
            || (part.starts_with("pytest-")
                && part.len() > 7
                && part[7..].chars().all(|c| c.is_ascii_digit()))
            || (part.starts_with("popen-gw")
                && part.len() > 8
                && part[8..].chars().all(|c| c.is_ascii_digit()))
    })
}

/// `production_server_environment_error`.
pub fn production_environment_error(project: &Path, environment: &Env) -> String {
    if path_is_pytest_owned(&project.to_string_lossy(), project) {
        return String::new();
    }
    for key in DATA_LOCATION_ENV_KEYS {
        if path_is_pytest_owned(
            environment.get(key).map(String::as_str).unwrap_or(""),
            project,
        ) {
            return format!(
                "production lifecycle refused: {key} points into pytest-owned temporary storage"
            );
        }
    }
    for key in PYTEST_CONTEXT_ENV_KEYS {
        if !environment
            .get(key)
            .map(|v| v.trim())
            .unwrap_or("")
            .is_empty()
        {
            return format!("production lifecycle refused: inherited pytest context ({key})");
        }
    }
    if environment.get("TOFU_TESTING").map(|v| v.trim()) == Some("1") {
        return "production lifecycle refused: inherited pytest context (TOFU_TESTING)".to_owned();
    }
    String::new()
}

/// The keys `_identity_error` reads from a live worker's environment.
pub fn identity_environment_keys() -> Vec<&'static str> {
    let mut keys: Vec<&'static str> = DATA_LOCATION_ENV_KEYS.to_vec();
    keys.extend(PYTEST_CONTEXT_ENV_KEYS);
    keys.push("TOFU_TESTING");
    keys
}

pub fn valid_port(raw: &str) -> Option<u16> {
    raw.trim()
        .parse::<u32>()
        .ok()
        .filter(|port| (1..=65535).contains(port))
        .map(|p| p as u16)
}

pub fn explicit_server_port(args: &[String]) -> Option<u16> {
    for (index, arg) in args.iter().enumerate() {
        if arg == "--port" {
            return args.get(index + 1).and_then(|value| valid_port(value));
        }
        if let Some(value) = arg.strip_prefix("--port=") {
            return valid_port(value);
        }
    }
    None
}

/// `_server_port`.
pub fn server_port(args: &[String], environment: &Env) -> u16 {
    explicit_server_port(args)
        .or_else(|| environment.get("PORT").and_then(|value| valid_port(value)))
        .unwrap_or(DEFAULT_SERVER_PORT)
}

/// `_operator_overrides`.
pub fn operator_overrides(server_env: &Env, project_environment: &Env) -> Env {
    server_env
        .iter()
        .filter(|(key, value)| project_environment.get(*key) != Some(*value))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

pub fn process_environment() -> Env {
    std::env::vars_os()
        .map(|(key, value)| {
            (
                key.to_string_lossy().into_owned(),
                value.to_string_lossy().into_owned(),
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotenv_follows_the_canonical_parser() {
        let parsed =
            parse_dotenv("\u{feff}A=1\n# c\nB = \"two\" \nbad-name=3\nC='x#y'\nnoassign\nA=4\n");
        assert_eq!(parsed.get("A").unwrap(), "4");
        assert_eq!(parsed.get("B").unwrap(), "two");
        assert_eq!(parsed.get("C").unwrap(), "x#y");
        assert!(!parsed.contains_key("bad-name"));
    }

    #[test]
    fn production_checkout_refuses_pytest_storage_and_context() {
        let project = Path::new("/srv/tofu");
        let mut env = Env::new();
        assert_eq!(production_environment_error(project, &env), "");
        env.insert(
            "TOFU_DATA_DIR".into(),
            "/tmp/pytest-of-me/pytest-3/data".into(),
        );
        assert!(production_environment_error(project, &env).contains("TOFU_DATA_DIR points into"));
        let mut env = Env::new();
        env.insert("PYTEST_CURRENT_TEST".into(), "x".into());
        assert!(production_environment_error(project, &env).contains("PYTEST_CURRENT_TEST"));
        let test_project = Path::new("/tmp/pytest-of-me/pytest-3/project");
        assert_eq!(production_environment_error(test_project, &env), "");
    }

    #[test]
    fn port_precedence_is_argument_then_environment_then_default() {
        let mut env = Env::new();
        env.insert("PORT".into(), "16000".into());
        assert_eq!(server_port(&["--port".into(), "17000".into()], &env), 17000);
        assert_eq!(server_port(&["--port=18000".into()], &env), 18000);
        assert_eq!(server_port(&[], &env), 16000);
        assert_eq!(server_port(&[], &Env::new()), DEFAULT_SERVER_PORT);
    }
}
