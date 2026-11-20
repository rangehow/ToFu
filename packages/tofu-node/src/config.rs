//! Node-local config: node ID per host, joined servers, share roots.
//!
//! Port of `lib/desktop_agent/config.py` on the same file
//! (`TOFU_DESKTOP_CONFIG`, else `~/.tofu/desktop_agent.json`) with the same
//! locking (`flock` on `<path>.lock`, as `lib/json_store.update_json_atomic`)
//! and the same per-host slots, so Python and Rust nodes can share one file
//! (twins on one network home) without ever sharing a node ID or credential.

use crate::host_identity;
use crate::pairing;
use serde_json::{json, Map, Value};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

pub type Config = Map<String, Value>;

fn defaults() -> Config {
    let Value::Object(map) = json!({
        "agent_id": "",
        "agent_ids": {},
        "host_facts": {},
        "predecessors": {},
        "host_attachments": {},
        "share_roots": [],
        "attachments": [],
        "remote_server": {},
        "computer_control": {},
    }) else {
        unreachable!()
    };
    map
}

pub fn config_path() -> PathBuf {
    if let Some(path) = std::env::var_os("TOFU_DESKTOP_CONFIG").filter(|p| !p.is_empty()) {
        return PathBuf::from(path);
    }
    home_dir().join(".tofu").join("desktop_agent.json")
}

pub fn home_dir() -> PathBuf {
    let var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(var)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// `~` expansion of a user-supplied path (Python `os.path.expanduser`).
pub fn expand_user(path: &str) -> PathBuf {
    if path == "~" {
        return home_dir();
    }
    if let Some(rest) = path.strip_prefix("~/").or_else(|| path.strip_prefix("~\\")) {
        return home_dir().join(rest);
    }
    PathBuf::from(path)
}

fn absolute(path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|d| d.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    }
}

#[cfg(unix)]
fn private_dir(dir: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)?;
    let _ = fs::set_permissions(dir, fs::Permissions::from_mode(0o700));
    Ok(())
}

#[cfg(not(unix))]
fn private_dir(dir: &Path) -> std::io::Result<()> {
    fs::create_dir_all(dir)
}

#[cfg(unix)]
fn create_private(path: &Path) -> std::io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
}

#[cfg(not(unix))]
fn create_private(path: &Path) -> std::io::Result<File> {
    OpenOptions::new().write(true).create_new(true).open(path)
}

/// Write `bytes` to `path` atomically with private permissions.
pub fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("config");
    let temp = parent.join(format!(".{name}.{}.tmp", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut file = create_private(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

/// One locked read-modify-write transaction on the config file. `mutate`
/// returns whether it changed the config; nothing is written otherwise.
pub fn update<T>(
    mutate: impl FnOnce(&mut Config) -> T,
    changed: impl Fn(&T) -> bool,
) -> Result<T, String> {
    let path = absolute(&config_path());
    if let Some(parent) = path.parent() {
        private_dir(parent).map_err(|e| format!("config directory {}: {e}", parent.display()))?;
    }
    let lock_path = PathBuf::from(format!("{}.lock", path.display()));
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(&lock_path)
        .map_err(|e| format!("config lock {}: {e}", lock_path.display()))?;
    // An unsupported lock (some network filesystems) leaves the transaction
    // unlocked, as json_store does.
    let locked = lock.lock().is_ok();
    let outcome = (|| {
        let mut cfg = defaults();
        match fs::read(&path) {
            Ok(bytes) if !bytes.iter().all(u8::is_ascii_whitespace) => {
                let current: Value = serde_json::from_slice(&bytes).map_err(|e| {
                    format!(
                        "desktop agent config is not valid JSON: {}: {e}",
                        path.display()
                    )
                })?;
                let Value::Object(current) = current else {
                    return Err(format!(
                        "desktop agent config is not a JSON object: {}",
                        path.display()
                    ));
                };
                cfg.extend(current);
            }
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => {
                return Err(format!(
                    "desktop agent config unreadable: {}: {e}",
                    path.display()
                ))
            }
        }
        let result = mutate(&mut cfg);
        if changed(&result) {
            let mut text =
                serde_json::to_string_pretty(&Value::Object(cfg)).map_err(|e| e.to_string())?;
            text.push('\n');
            write_atomic(&path, text.as_bytes())
                .map_err(|e| format!("config write {}: {e}", path.display()))?;
        }
        Ok(result)
    })();
    if locked {
        let _ = lock.unlock();
    }
    outcome
}

/// Read the config merged over defaults. Never fails.
pub fn load() -> Config {
    let mut cfg = defaults();
    let path = config_path();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if path.exists() {
            let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
        }
    }
    if let Ok(bytes) = fs::read(&path) {
        if let Ok(Value::Object(current)) = serde_json::from_slice::<Value>(&bytes) {
            cfg.extend(current);
        }
    }
    cfg
}

fn object(cfg: &Config, key: &str) -> Map<String, Value> {
    cfg.get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string()
}

struct Host {
    key: String,
    facts: Map<String, Value>,
    phase0: String,
}

fn this_host() -> Host {
    Host {
        key: host_identity::host_key(),
        facts: host_identity::host_facts(),
        phase0: host_identity::phase0_host_key(),
    }
}

/// Resolve this host's node ID inside one config transaction (the same order
/// as `config._resolve_identity`). Returns `(node_id, changed)`.
fn resolve_identity(cfg: &mut Config, host: &Host) -> (String, bool) {
    let mut ids = object(cfg, "agent_ids");
    let mut known = object(cfg, "host_facts");
    let mut slots = object(cfg, "host_attachments");
    let mut predecessors = object(cfg, "predecessors");
    let legacy = text(cfg.get("agent_id"));
    let mut changed = false;
    let key = host.key.clone();
    let has_id = |ids: &Map<String, Value>, k: &str| {
        ids.get(k)
            .map(|v| !text(Some(v)).is_empty())
            .unwrap_or(false)
    };

    let move_slot = |old: &str,
                     ids: &mut Map<String, Value>,
                     slots: &mut Map<String, Value>,
                     known: &mut Map<String, Value>| {
        if let Some(id) = ids.shift_remove(old) {
            ids.insert(key.clone(), id);
        }
        if let Some(slot) = slots.shift_remove(old) {
            slots.insert(key.clone(), slot);
        }
        known.shift_remove(old);
    };

    let node_id;
    if has_id(&ids, &key) {
        node_id = text(ids.get(&key));
    } else if host.phase0 != key && has_id(&ids, &host.phase0) && !known.contains_key(&host.phase0)
    {
        move_slot(&host.phase0, &mut ids, &mut slots, &mut known);
        node_id = text(ids.get(&key));
        changed = true;
    } else if !legacy.is_empty() && !ids.values().any(|v| text(Some(v)) == legacy) {
        ids.insert(key.clone(), legacy.clone().into());
        node_id = legacy.clone();
        changed = true;
    } else {
        let container = text(host.facts.get("container"));
        let machine = text(host.facts.get("machine"));
        let renamed: Vec<String> = known
            .iter()
            .filter(|(k, f)| {
                !container.is_empty()
                    && ids.contains_key(k.as_str())
                    && text(f.get("container")) == container
                    && text(f.get("machine")) == machine
            })
            .map(|(k, _)| k.clone())
            .collect();
        if let Some(old) = renamed.first() {
            move_slot(old, &mut ids, &mut slots, &mut known);
            node_id = text(ids.get(&key));
        } else {
            let hostname = text(host.facts.get("hostname"));
            let mut similar: Vec<String> = known
                .iter()
                .filter(|(k, f)| {
                    ids.contains_key(k.as_str())
                        && !machine.is_empty()
                        && text(f.get("machine")) == machine
                })
                .map(|(k, _)| k.clone())
                .collect();
            similar.sort_by_key(|k| text(known[k].get("hostname")) != hostname);
            node_id = uuid::Uuid::new_v4().simple().to_string();
            ids.insert(key.clone(), node_id.clone().into());
            if let Some(first) = similar.first() {
                predecessors.insert(key.clone(), ids[first].clone());
            }
        }
        changed = true;
    }
    if known.get(&key).and_then(Value::as_object) != Some(&host.facts) {
        known.insert(key.clone(), Value::Object(host.facts.clone()));
        changed = true;
    }
    if legacy.is_empty() {
        cfg.insert("agent_id".into(), node_id.clone().into());
        changed = true;
    }
    if changed {
        cfg.insert("agent_ids".into(), Value::Object(ids));
        cfg.insert("host_facts".into(), Value::Object(known));
        cfg.insert("host_attachments".into(), Value::Object(slots));
        cfg.insert("predecessors".into(), Value::Object(predecessors));
    }
    (node_id, changed)
}

#[derive(Clone, Debug, PartialEq)]
pub struct Identity {
    pub node_id: String,
    pub predecessor: Option<String>,
}

/// This host's node ID (and the node it replaced), resolved and persisted atomically.
pub fn node_identity() -> Result<Identity, String> {
    let host = this_host();
    update(
        |cfg| {
            let (node_id, changed) = resolve_identity(cfg, &host);
            let predecessor = cfg
                .get("predecessors")
                .and_then(|p| p.get(&host.key))
                .map(|v| text(Some(v)))
                .filter(|v| !v.is_empty());
            (
                Identity {
                    node_id,
                    predecessor,
                },
                changed,
            )
        },
        |(_, changed)| *changed,
    )
    .map(|(identity, _)| identity)
}

/// The Home recorded the re-pair; later pairings of this host name no predecessor.
pub fn clear_predecessor() -> Result<(), String> {
    let key = host_identity::host_key();
    update(
        |cfg| {
            let mut predecessors = object(cfg, "predecessors");
            let removed = predecessors.shift_remove(&key).is_some();
            if removed {
                cfg.insert("predecessors".into(), Value::Object(predecessors));
            }
            removed
        },
        |removed| *removed,
    )
    .map(|_| ())
}

/// This host's attachment list (created when absent). The host owning the
/// legacy `agent_id` keeps the top-level list older builds read.
fn host_rows<'a>(cfg: &'a mut Config, key: &str) -> &'a mut Vec<Value> {
    let legacy_owner = cfg.get("agent_ids").and_then(|ids| ids.get(key)) == cfg.get("agent_id");
    if legacy_owner {
        if !matches!(cfg.get("attachments"), Some(Value::Array(_))) {
            cfg.insert("attachments".into(), json!([]));
        }
        return cfg
            .get_mut("attachments")
            .and_then(Value::as_array_mut)
            .expect("attachments list");
    }
    if !matches!(cfg.get("host_attachments"), Some(Value::Object(_))) {
        cfg.insert("host_attachments".into(), json!({}));
    }
    let slots = cfg
        .get_mut("host_attachments")
        .and_then(Value::as_object_mut)
        .expect("slots");
    if !matches!(slots.get(key), Some(Value::Array(_))) {
        slots.insert(key.to_string(), json!([]));
    }
    slots
        .get_mut(key)
        .and_then(Value::as_array_mut)
        .expect("host rows")
}

/// Run `change(rows) -> changed` on this host's attachments in one transaction.
fn attachment_transaction<T>(
    change: impl FnOnce(&mut Vec<Value>) -> (T, bool),
) -> Result<T, String> {
    let host = this_host();
    update(
        |cfg| {
            let (_, mut changed) = resolve_identity(cfg, &host);
            let legacy_owner =
                cfg.get("agent_ids").and_then(|ids| ids.get(&host.key)) == cfg.get("agent_id");
            if legacy_owner {
                changed = pairing::migrate_attachments(cfg) || changed;
            }
            let (out, rows_changed) = change(host_rows(cfg, &host.key));
            (out, changed || rows_changed)
        },
        |(_, changed)| *changed,
    )
    .map(|(out, _)| out)
}

/// This host's joined servers.
pub fn attachments() -> Result<Vec<Map<String, Value>>, String> {
    attachment_transaction(|rows| {
        (
            rows.iter().filter_map(|r| r.as_object().cloned()).collect(),
            false,
        )
    })
}

/// Insert or replace one attachment of this host by `id`.
pub fn save_attachment(record: Map<String, Value>) -> Result<(), String> {
    let id = record.get("id").cloned();
    attachment_transaction(move |rows| {
        rows.retain(|r| r.is_object() && r.get("id") != id.as_ref());
        rows.push(Value::Object(record));
        ((), true)
    })
}

/// Change fields of one attachment; returns it, or None when absent.
pub fn update_attachment(
    attachment_id: &str,
    fields: Map<String, Value>,
) -> Result<Option<Map<String, Value>>, String> {
    attachment_transaction(|rows| {
        for row in rows.iter_mut() {
            if let Some(obj) = row.as_object_mut() {
                if obj.get("id").and_then(Value::as_str) == Some(attachment_id) {
                    obj.extend(fields);
                    return (Some(obj.clone()), true);
                }
            }
        }
        (None, false)
    })
}

/// Leave one joined server: drop its attachment; returns whether it existed.
pub fn remove_attachment(attachment_id: &str) -> Result<bool, String> {
    attachment_transaction(|rows| {
        let before = rows.len();
        rows.retain(|r| r.get("id").and_then(Value::as_str) != Some(attachment_id));
        let removed = rows.len() != before;
        (removed, removed)
    })
}

/// Merge `--root NAME=PATH` specs into the share roots (CLI wins on a name).
pub fn merge_cli_roots(existing: &Value, specs: &[String]) -> Result<Vec<Value>, String> {
    let mut order: Vec<String> = Vec::new();
    let mut by_name: Map<String, Value> = Map::new();
    for root in existing.as_array().into_iter().flatten() {
        if let Some(name) = root
            .get("name")
            .and_then(Value::as_str)
            .filter(|n| !n.is_empty())
        {
            if !by_name.contains_key(name) {
                order.push(name.to_string());
            }
            by_name.insert(name.to_string(), root.clone());
        }
    }
    for spec in specs {
        let Some((name, path)) = spec.split_once('=') else {
            return Err(format!("--root must be NAME=PATH, got {spec:?}"));
        };
        let (name, path) = (name.trim(), path.trim());
        if name.is_empty() || path.is_empty() {
            return Err(format!("--root must be NAME=PATH, got {spec:?}"));
        }
        let path = expand_user(path).to_string_lossy().into_owned();
        match by_name.get_mut(name) {
            Some(root) => {
                root["path"] = path.into();
            }
            None => {
                by_name.insert(name.into(), json!({"name": name, "path": path}));
                order.push(name.into());
            }
        }
    }
    Ok(order.iter().map(|n| by_name[n].clone()).collect())
}

pub fn share_roots() -> Vec<Value> {
    load()
        .get("share_roots")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host(key: &str, machine: &str, hostname: &str, container: &str) -> Host {
        let mut facts = Map::new();
        facts.insert("machine".into(), machine.into());
        facts.insert("hostname".into(), hostname.into());
        facts.insert("container".into(), container.into());
        Host {
            key: key.into(),
            facts,
            phase0: format!("{key}-phase0"),
        }
    }

    #[test]
    fn twins_on_one_file_get_distinct_ids_and_the_second_names_a_predecessor() {
        let mut cfg = defaults();
        let (a, _) = resolve_identity(&mut cfg, &host("A", "m", "h1", "c1"));
        let (b, _) = resolve_identity(&mut cfg, &host("B", "m", "h1", "c2"));
        assert_ne!(a, b);
        assert_eq!(
            cfg["agent_id"],
            json!(a),
            "the first host claims the legacy id"
        );
        assert_eq!(cfg["predecessors"]["B"], json!(a));
        // Stable on re-resolution.
        assert_eq!(resolve_identity(&mut cfg, &host("A", "m", "h1", "c1")).0, a);
        assert_eq!(resolve_identity(&mut cfg, &host("B", "m", "h1", "c2")).0, b);
    }

    #[test]
    fn same_container_under_a_new_hostname_keeps_its_node() {
        let mut cfg = defaults();
        let (a, _) = resolve_identity(&mut cfg, &host("A", "m", "h1", "c1"));
        let (renamed, _) = resolve_identity(&mut cfg, &host("A2", "m", "h2", "c1"));
        assert_eq!(a, renamed);
        assert!(cfg["agent_ids"].get("A").is_none());
    }

    #[test]
    fn legacy_single_id_goes_to_the_first_host() {
        let mut cfg = defaults();
        cfg.insert("agent_id".into(), "legacy-id".into());
        assert_eq!(
            resolve_identity(&mut cfg, &host("A", "m", "h", "")).0,
            "legacy-id"
        );
        assert_ne!(
            resolve_identity(&mut cfg, &host("B", "m", "h2", "")).0,
            "legacy-id"
        );
    }

    #[test]
    fn phase0_slot_is_claimed_once() {
        let mut cfg = defaults();
        cfg.insert("agent_ids".into(), json!({"A-phase0": "old"}));
        cfg.insert("agent_id".into(), "old".into());
        assert_eq!(
            resolve_identity(&mut cfg, &host("A", "m", "h", "c")).0,
            "old"
        );
    }

    #[test]
    fn roots_merge_cli_wins() {
        let merged = merge_cli_roots(
            &json!([{"name": "a", "path": "/x"}]),
            &["a=/y".into(), "b=/z".into()],
        )
        .unwrap();
        assert_eq!(
            merged,
            vec![
                json!({"name": "a", "path": "/y"}),
                json!({"name": "b", "path": "/z"})
            ]
        );
        assert!(merge_cli_roots(&json!([]), &["nope".into()]).is_err());
    }
}
