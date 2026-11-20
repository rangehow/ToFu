//! Instance-lock, storage-lease and listener observations (`server_manager`).

use crate::json::{read_object, Object, ObjectExt};
use crate::procfs;
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::net::{SocketAddr, TcpStream};
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::time::Duration;

/// `pid_is_server`: an unreadable live process is ambiguous and reads as live.
pub fn pid_is_server(pid: i64) -> bool {
    if !procfs::pid_is_alive(pid) {
        return false;
    }
    procfs::cmdline(pid)
        .map(|cmdline| cmdline.contains("server.py"))
        .unwrap_or(true)
}

/// `read_lock_status` for the worker data directory the bridge resolved.
pub fn read_lock_status(project: &Path, worker_data_directory: &Path) -> Object {
    let lock_path = worker_data_directory.join(".server.lock");
    let mut result = json!({
        "projectPath": project.to_string_lossy(),
        "running": false, "pid": null, "host": null, "sameHost": null,
        "lockPresent": lock_path.is_file(), "stale": false,
        "processStartTime": null, "processStartedAt": null, "processCwd": null,
        "projectMatches": null, "cmdline": null, "externalOwner": null,
    });
    let map = result.as_object_mut().unwrap();
    if !map["lockPresent"].as_bool().unwrap_or(false) {
        return map.clone();
    }
    let Ok(text) = std::fs::read_to_string(&lock_path) else {
        map.insert("stale".into(), json!(true));
        return map.clone();
    };
    let entry = text.lines().next().unwrap_or("").trim().to_owned();
    let Some((raw_pid, host)) = entry.split_once('@') else {
        map.insert("stale".into(), json!(true));
        return map.clone();
    };
    if raw_pid.is_empty() || !raw_pid.chars().all(|c| c.is_ascii_digit()) {
        map.insert("stale".into(), json!(true));
        return map.clone();
    }
    let pid: i64 = raw_pid.parse().unwrap_or(0);
    let here = procfs::hostname();
    let cwd = procfs::cwd(pid);
    map.insert("pid".into(), json!(pid));
    map.insert(
        "host".into(),
        if host.is_empty() {
            Value::Null
        } else {
            json!(host)
        },
    );
    map.insert(
        "sameHost".into(),
        if host.is_empty() {
            Value::Null
        } else {
            json!(host == here)
        },
    );
    map.insert("processStartTime".into(), json!(procfs::start_ticks(pid)));
    map.insert("processStartedAt".into(), json!(procfs::start_epoch(pid)));
    map.insert("processCwd".into(), json!(cwd));
    map.insert(
        "projectMatches".into(),
        match &cwd {
            Some(cwd) => json!(Path::new(cwd) == project),
            None => Value::Null,
        },
    );
    map.insert("cmdline".into(), json!(procfs::cmdline(pid)));
    map.insert(
        "externalOwner".into(),
        json!(procfs::env_value(pid, "TOFU_MANAGED_BY")),
    );
    if map.get("sameHost") == Some(&json!(false)) {
        return map.clone();
    }
    if pid_is_server(pid) {
        map.insert("running".into(), json!(true));
    } else {
        map.insert("stale".into(), json!(true));
    }
    map.clone()
}

fn try_flock(file: &File) -> bool {
    let fd = file.as_raw_fd();
    unsafe {
        if libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) == 0 {
            libc::flock(fd, libc::LOCK_UN);
            true
        } else {
            false
        }
    }
}

const OFFLINE_STORAGE_COMMAND_LABELS: [(&str, &str); 2] = [
    ("scripts/storage_deep_clean.py", "SQLite deep clean"),
    (
        "scripts/migrate_sqlite_to_postgres.py",
        "SQLite to PostgreSQL migration",
    ),
];
const OFFLINE_STORAGECTL_COMMANDS: [&str; 4] =
    ["baseline", "integrity-check", "restore", "handoff"];

/// `_legacy_storage_lease_owner` for a live holder's command line.
pub fn legacy_storage_lease_owner(cmdline: &str) -> (&'static str, String) {
    for (marker, label) in OFFLINE_STORAGE_COMMAND_LABELS {
        if cmdline.contains(marker) {
            return ("offline_maintenance", label.to_owned());
        }
    }
    if cmdline.contains("scripts/storagectl.py") {
        for operation in OFFLINE_STORAGECTL_COMMANDS {
            if cmdline.split_whitespace().any(|token| token == operation) {
                return ("offline_maintenance", format!("Storage {operation}"));
            }
        }
    }
    if cmdline.contains("-m lib.storage_authority_sql") {
        return ("storage_authority_sql", "Storage sidecar".to_owned());
    }
    let tokens: Vec<&str> = cmdline.split_whitespace().collect();
    if tokens.len() >= 2 {
        let base = Path::new(tokens[0])
            .file_name()
            .map(|n| n.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if (base == "tofu-db" || base == "tofu-db.exe") && tokens[1] == "serve" {
            return ("storage_authority_sql", "Storage sidecar".to_owned());
        }
    }
    ("unknown", "Storage operation".to_owned())
}

/// `read_storage_lease_status`: the OS lock is the sole authority.
pub fn read_storage_lease_status(data_dir: &Path) -> Object {
    let lock_path = data_dir.join(".storage-sidecar.lock");
    let lease_path = data_dir.join(".storage-sidecar-lease.json");
    let mut result = json!({
        "held": false, "kind": null, "label": null, "pid": null, "host": null,
        "startedAt": null, "ageSeconds": null, "holderVerified": false,
    })
    .as_object()
    .unwrap()
    .clone();
    if !lock_path.is_file() {
        return result;
    }
    match OpenOptions::new().read(true).write(true).open(&lock_path) {
        Err(_) => {
            result.insert("held".into(), json!(true));
            result.insert("kind".into(), json!("unknown"));
            result.insert("label".into(), json!("Storage operation"));
            return result;
        }
        Ok(file) => {
            if try_flock(&file) {
                return result;
            }
            result.insert("held".into(), json!(true));
        }
    }
    let stamp = read_object(&lease_path);
    let host: String = stamp.str_of("host").trim().chars().take(255).collect();
    let pid = stamp.i64_of("pid").filter(|pid| *pid > 1);
    let started_at = Some(stamp.f64_of("started_unix_ms") / 1000.0).filter(|value| *value > 0.0);
    let mut kind = match stamp.str_of("owner_kind").trim().to_lowercase().as_str() {
        "offline_maintenance" => "offline_maintenance".to_owned(),
        "storage_authority_sql" | "tofudb" => "storage_authority_sql".to_owned(),
        "storage_operation" => "storage_operation".to_owned(),
        _ => String::new(),
    };
    let mut label: String = stamp
        .str_of("owner_label")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect();
    let same_host = !host.is_empty() && host == procfs::hostname();
    let alive = pid
        .map(|pid| same_host && procfs::pid_is_alive(pid))
        .unwrap_or(false);
    if alive {
        let (inferred_kind, inferred_label) =
            legacy_storage_lease_owner(&procfs::cmdline(pid.unwrap()).unwrap_or_default());
        if kind.is_empty() || inferred_kind == "offline_maintenance" {
            kind = inferred_kind.to_owned();
            label = inferred_label;
        }
    }
    if kind.is_empty() {
        kind = "unknown".into();
    }
    if label.is_empty() {
        label = match kind.as_str() {
            "offline_maintenance" => "Offline storage maintenance",
            "storage_authority_sql" => "Storage sidecar",
            _ => "Storage operation",
        }
        .to_owned();
    }
    result.insert("kind".into(), json!(kind));
    result.insert("label".into(), json!(label));
    result.insert("pid".into(), json!(pid));
    result.insert(
        "host".into(),
        if host.is_empty() {
            Value::Null
        } else {
            json!(host)
        },
    );
    result.insert("startedAt".into(), json!(started_at));
    result.insert(
        "ageSeconds".into(),
        json!(started_at.map(|at| ((crate::timing::now() - at).max(0.0) * 10.0).round() / 10.0)),
    );
    result.insert("holderVerified".into(), json!(alive));
    result
}

/// `listener_pids` through `ss -ltnp`; `[]` also means unavailable.
pub fn listener_pids(port: u16) -> Vec<i64> {
    let Ok(output) = std::process::Command::new("ss").arg("-ltnp").output() else {
        return Vec::new();
    };
    let suffix = format!(":{port}");
    let mut found = std::collections::BTreeSet::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let columns: Vec<&str> = line.split_whitespace().collect();
        if !columns
            .iter()
            .take(6)
            .any(|column| column.ends_with(&suffix))
        {
            continue;
        }
        let mut rest = line;
        while let Some(at) = rest.find("pid=") {
            rest = &rest[at + 4..];
            let digits: String = rest.chars().take_while(char::is_ascii_digit).collect();
            if let Ok(pid) = digits.parse() {
                found.insert(pid);
            }
        }
    }
    found.into_iter().collect()
}

pub fn port_accepts(port: u16) -> bool {
    let address: SocketAddr = ([127, 0, 0, 1], port).into();
    TcpStream::connect_timeout(&address, Duration::from_millis(500)).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_live_lock_holder_reports_running_with_identity() {
        let directory =
            std::env::temp_dir().join(format!("tofu-manager-lock-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let mut child = std::process::Command::new("sh")
            .args(["-c", "exec -a server.py sleep 30"])
            .spawn()
            .unwrap();
        std::fs::write(
            directory.join(".server.lock"),
            format!("{}@{}\n", child.id(), procfs::hostname()),
        )
        .unwrap();
        let deadline = crate::timing::Deadline::after(10.0);
        while !procfs::cmdline(child.id() as i64)
            .unwrap_or_default()
            .contains("server.py")
            && !deadline.expired()
        {
            crate::timing::sleep(0.02);
        }
        let status = read_lock_status(Path::new("/nonexistent-project"), &directory);
        assert_eq!(status["pid"], json!(child.id()));
        assert_eq!(status["sameHost"], json!(true));
        assert_eq!(status["running"], json!(true), "{status:?}");
        assert_eq!(status["projectMatches"], json!(false));
        let _ = child.kill();
        let _ = child.wait();
        let stale = read_lock_status(Path::new("/x"), &directory);
        assert_eq!(stale["stale"], json!(true));
        let _ = std::fs::remove_dir_all(&directory);
    }

    #[test]
    fn an_unheld_storage_lock_is_free() {
        let directory =
            std::env::temp_dir().join(format!("tofu-manager-lease-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        std::fs::write(directory.join(".storage-sidecar.lock"), b"").unwrap();
        assert_eq!(read_storage_lease_status(&directory)["held"], json!(false));
        let _ = std::fs::remove_dir_all(&directory);
    }
}
