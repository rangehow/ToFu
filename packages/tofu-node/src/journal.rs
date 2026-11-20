//! Node call journal: a side-effecting command id runs at most once on this node.
//!
//! Port of `lib/desktop_agent/_journal.py`, on the same directory
//! (`<config path without extension>.calls`) and record format. Before a
//! command whose tier is not read/control starts, the node writes `started`
//! for its id; on completion it writes the outcome (Codex/egress retain only a
//! sensitive_done marker, never result/error). A command id that reaches
//! the node again — also after a node restart — is answered from the journal
//! (the recorded outcome, or a typed `device_unavailable` refusal when the node
//! died mid-run) and never re-run. Records retire after the contract's
//! `x-idempotency-journal-retention-seconds`, swept when the journal opens.

use crate::config::{config_path, write_atomic};
use crate::contract;
use crate::util::{sha256_hex, unix_now};
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

pub const UNKNOWN_OUTCOME: &str = "device_unavailable: this call already started on this device before it lost its connection or restarted; its outcome is unknown and it was not run again.";

pub const SENSITIVE_OUTCOME: &str = "device_unavailable: sensitive result was not retained; this call completed and was not run again. Use a new call id for a fresh authorized request.";

fn sensitive(cmd_type: &str) -> bool {
    matches!(
        cmd_type,
        "codex_login_read" | "egress_http" | "egress_http_stream"
    )
}

fn sensitive_record(record: &Value) -> bool {
    record
        .get("type")
        .and_then(Value::as_str)
        .is_some_and(sensitive)
}

fn redacted(record: &Value) -> Value {
    json!({"id": record["id"], "type": record["type"],
           "state": "sensitive_done", "at": record["at"]})
}

pub fn journal_dir() -> PathBuf {
    let path = config_path();
    let stem = match path.extension() {
        Some(_) => path.with_extension(""),
        None => path,
    };
    PathBuf::from(format!("{}.calls", stem.display()))
}

fn journaled(cmd_type: &str, params: &Value) -> bool {
    !matches!(
        contract::required_tier(cmd_type, params),
        None | Some("read") | Some("control")
    )
}

pub struct CallJournal {
    directory: PathBuf,
    lock: Mutex<()>,
}

impl CallJournal {
    pub fn open(directory: Option<PathBuf>) -> std::io::Result<Self> {
        let directory = directory.unwrap_or_else(journal_dir);
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&directory)?;
        }
        #[cfg(not(unix))]
        fs::create_dir_all(&directory)?;
        let journal = Self {
            directory,
            lock: Mutex::new(()),
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&journal.directory, fs::Permissions::from_mode(0o700))?;
        }
        journal.migrate()?;
        journal.sweep();
        Ok(journal)
    }

    fn path(&self, cmd_id: &str) -> PathBuf {
        self.directory
            .join(format!("{}.json", sha256_hex(cmd_id.as_bytes())))
    }

    // Set the old mtime on the replacement *before* publishing it: even a
    // crash during migration must not give a record a new retention window.
    fn migrate(&self) -> std::io::Result<()> {
        for entry in fs::read_dir(&self.directory)? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.file_type().is_file() {
                return Err(std::io::Error::other("invalid call journal record"));
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
            }
            let record: Value =
                serde_json::from_slice(&fs::read(&path)?).map_err(std::io::Error::other)?;
            let id = record
                .get("id")
                .and_then(Value::as_str)
                .ok_or_else(|| std::io::Error::other("invalid call journal id"))?;
            if self.path(id) != path {
                return Err(std::io::Error::other("call journal id mismatch"));
            }
            if sensitive_record(&record)
                && (record.get("state").and_then(Value::as_str) == Some("done")
                    || record.get("result").is_some()
                    || record.get("error").is_some())
            {
                let temp = self
                    .directory
                    .join(format!(".{}.tmp", uuid::Uuid::new_v4()));
                let outcome = (|| {
                    write_atomic(&temp, &serde_json::to_vec(&redacted(&record))?)?;
                    fs::File::options()
                        .write(true)
                        .open(&temp)?
                        .set_times(fs::FileTimes::new().set_modified(metadata.modified()?))?;
                    fs::rename(&temp, &path)
                })();
                if outcome.is_err() {
                    let _ = fs::remove_file(&temp);
                }
                outcome?;
            }
        }
        Ok(())
    }

    fn sweep(&self) {
        let horizon =
            SystemTime::now() - Duration::from_secs_f64(contract::journal_retention_seconds());
        let Ok(entries) = fs::read_dir(&self.directory) else {
            return;
        };
        for entry in entries.flatten() {
            if let Ok(modified) = entry.metadata().and_then(|m| m.modified()) {
                if modified < horizon {
                    let _ = fs::remove_file(entry.path());
                }
            }
        }
    }

    fn read(&self, cmd_id: &str) -> Option<Value> {
        match fs::read(self.path(cmd_id)) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
            // An unreadable record is not proof the call never ran.
            Err(_) => Some(json!({"id": cmd_id, "state": "started"})),
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(record)
                    if record.get("id").and_then(Value::as_str) == Some(cmd_id)
                        && record.get("type").is_some_and(Value::is_string)
                        && record.get("at").is_some_and(Value::is_number)
                        && matches!(
                            record.get("state").and_then(Value::as_str),
                            Some("started" | "done" | "sensitive_done")
                        ) =>
                {
                    Some(record)
                }
                Ok(_) => Some(json!({"id": cmd_id, "state": "started"})),
                Err(_) => Some(json!({"id": cmd_id, "state": "started"})),
            },
        }
    }

    fn write(&self, record: &Value, cmd_id: &str) -> std::io::Result<()> {
        let bytes = serde_json::to_vec(record).map_err(std::io::Error::other)?;
        write_atomic(&self.path(cmd_id), &bytes)
    }

    /// None when the command may run now; else the completion to send instead.
    pub fn begin(&self, cmd_id: &str, cmd_type: &str, params: &Value) -> Option<Value> {
        let record = {
            let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
            match self.read(cmd_id) {
                None => {
                    if !sensitive(cmd_type) && !journaled(cmd_type, params) {
                        return None;
                    }
                    let started = json!({"id": cmd_id, "type": cmd_type, "state": "started", "at": unix_now()});
                    if let Err(error) = self.write(&started, cmd_id) {
                        // Without a durable start record a repeat could run twice: refuse.
                        return Some(json!({"id": cmd_id, "result": null,
                                           "error": format!("call journal unavailable: {error}")}));
                    }
                    return None;
                }
                Some(record) => record,
            }
        };
        let state = record.get("state").and_then(Value::as_str);
        if state == Some("sensitive_done")
            || (state == Some("done") && (sensitive(cmd_type) || sensitive_record(&record)))
        {
            return Some(json!({"id": cmd_id, "result": null, "error": SENSITIVE_OUTCOME}));
        }
        if state == Some("done") {
            crate::log::info(&format!(
                "repeated call {} answered from the journal",
                &cmd_id[..cmd_id.len().min(12)]
            ));
            return Some(
                json!({"id": cmd_id, "result": record.get("result").cloned().unwrap_or(Value::Null),
                               "error": record.get("error").cloned().unwrap_or(Value::Null)}),
            );
        }
        Some(json!({"id": cmd_id, "result": null, "error": UNKNOWN_OUTCOME}))
    }

    pub fn complete(&self, cmd_type: &str, params: &Value, completion: &Value) {
        let Some(cmd_id) = completion.get("id").and_then(Value::as_str) else {
            return;
        };
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        let stored = self.read(cmd_id);
        let is_sensitive = sensitive(cmd_type) || stored.as_ref().is_some_and(sensitive_record);
        if !is_sensitive && !journaled(cmd_type, params) {
            return;
        }
        let record = if is_sensitive {
            let stored_type = stored
                .as_ref()
                .filter(|r| sensitive_record(r))
                .and_then(|r| r.get("type"))
                .and_then(Value::as_str)
                .unwrap_or(cmd_type);
            json!({"id": cmd_id, "type": stored_type, "state": "sensitive_done", "at": unix_now()})
        } else {
            json!({"id": cmd_id, "type": cmd_type, "state": "done", "at": unix_now(),
                            "result": completion.get("result").cloned().unwrap_or(Value::Null),
                            "error": completion.get("error").cloned().unwrap_or(Value::Null)})
        };
        if let Err(error) = self.write(&record, cmd_id) {
            crate::log::warn(&format!("call journal write failed: {error}"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sensitive_success_failure_retry_and_reopen() {
        for kind in ["codex_login_read", "egress_http", "egress_http_stream"] {
            for failed in [false, true] {
                let dir = tempfile::tempdir().unwrap();
                let journal = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
                assert!(journal.begin("secret", kind, &json!({})).is_none());
                journal.complete(kind, &json!({}), &json!({"id": "secret", "result": "TOKEN", "error": if failed { Some("SECRET ERROR") } else { None }}));
                let record: Value =
                    serde_json::from_slice(&fs::read(journal.path("secret")).unwrap()).unwrap();
                assert_eq!(
                    record,
                    json!({"id": "secret", "type": kind, "state": "sensitive_done", "at": record["at"]})
                );
                for incoming in [kind, "desktop_read_file", "desktop_write_file"] {
                    assert_eq!(
                        journal.begin("secret", incoming, &json!({})).unwrap()["error"],
                        SENSITIVE_OUTCOME
                    );
                }
                let reopened = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
                assert_eq!(
                    reopened.begin("secret", kind, &json!({})).unwrap()["error"],
                    SENSITIVE_OUTCOME
                );
            }
        }
    }

    #[test]
    fn legacy_migration_preserves_retention_and_private_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let journal = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
        let old = SystemTime::now() - Duration::from_secs(60);
        for kind in ["codex_login_read", "egress_http", "egress_http_stream"] {
            journal.write(&json!({"id": kind, "type": kind, "state": "done", "at": 1, "result": "TOKEN", "error": "SECRET"}), kind).unwrap();
            fs::File::options()
                .write(true)
                .open(journal.path(kind))
                .unwrap()
                .set_times(fs::FileTimes::new().set_modified(old))
                .unwrap();
        }
        let reopened = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
        for kind in ["codex_login_read", "egress_http", "egress_http_stream"] {
            let path = reopened.path(kind);
            assert_eq!(fs::metadata(&path).unwrap().modified().unwrap(), old);
            let record = reopened.read(kind).unwrap();
            assert!(record.get("result").is_none());
            assert!(record.get("error").is_none());
            assert_eq!(record["state"], "sensitive_done");
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                assert_eq!(
                    fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                    0o600
                );
                assert_eq!(
                    fs::metadata(dir.path()).unwrap().permissions().mode() & 0o777,
                    0o700
                );
            }
            let expired = SystemTime::now()
                - Duration::from_secs_f64(contract::journal_retention_seconds() + 60.0);
            fs::File::options()
                .write(true)
                .open(path)
                .unwrap()
                .set_times(fs::FileTimes::new().set_modified(expired))
                .unwrap();
        }
        let reopened = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
        assert!(reopened.read("egress_http").is_none());
    }

    #[test]
    fn invalid_records_and_write_failures_never_authorize_reexecution() {
        let dir = tempfile::tempdir().unwrap();
        let journal = CallJournal::open(Some(dir.path().to_path_buf())).unwrap();
        for bytes in [
            b"not json".as_slice(),
            b"{\"id\":\"wrong\",\"state\":\"done\"}".as_slice(),
        ] {
            fs::write(journal.path("bad"), bytes).unwrap();
            assert!(journal.begin("bad", "egress_http", &json!({})).is_some());
            assert!(CallJournal::open(Some(dir.path().to_path_buf())).is_err());
        }
        fs::remove_file(journal.path("bad")).unwrap();
        fs::create_dir(journal.path("blocked")).unwrap();
        assert!(journal
            .begin("blocked", "egress_http", &json!({}))
            .is_some());
        journal.complete(
            "egress_http",
            &json!({}),
            &json!({"id": "blocked", "result": "TOKEN"}),
        );
        assert!(journal
            .begin("blocked", "egress_http", &json!({}))
            .is_some());
        fs::remove_dir_all(dir.path()).unwrap();
        assert!(journal.begin("new", "egress_http", &json!({})).is_some());
    }

    #[test]
    fn incoming_type_and_failed_completion_remain_safe() {
        let dir = tempfile::tempdir().unwrap();
        let journal = CallJournal::open(Some(dir.path().join("calls"))).unwrap();
        journal.write(&json!({"id": "legacy", "type": "desktop_write_file", "state": "done", "at": unix_now(), "result": "TOKEN"}), "legacy").unwrap();
        assert_eq!(
            journal.begin("legacy", "egress_http", &json!({})).unwrap()["error"],
            SENSITIVE_OUTCOME
        );
        assert!(journal
            .begin("started", "egress_http", &json!({}))
            .is_none());
        // A storage outage must leave the durable start safe to reopen.
        let saved = dir.path().join("saved");
        fs::rename(&journal.directory, &saved).unwrap();
        journal.complete(
            "egress_http",
            &json!({}),
            &json!({"id": "started", "result": "TOKEN", "error": "SECRET"}),
        );
        fs::rename(saved, &journal.directory).unwrap();
        let reopened = CallJournal::open(Some(journal.directory.clone())).unwrap();
        assert_eq!(
            reopened
                .begin("started", "egress_http", &json!({}))
                .unwrap()["error"],
            UNKNOWN_OUTCOME
        );
        assert!(!fs::read_to_string(reopened.path("started"))
            .unwrap()
            .contains("TOKEN"));
    }

    #[test]
    fn a_side_effect_runs_once_across_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let journal = CallJournal::open(Some(dir.path().join("calls"))).unwrap();
        let params = json!({"path": "x"});
        assert!(journal.begin("c1", "desktop_write_file", &params).is_none());
        // Died mid-run: a reopened journal refuses to run it again.
        let reopened = CallJournal::open(Some(dir.path().join("calls"))).unwrap();
        let refusal = reopened.begin("c1", "desktop_write_file", &params).unwrap();
        assert!(refusal["error"].as_str().unwrap().contains("not run again"));
        // Completed: the recorded outcome answers the repeat.
        assert!(reopened
            .begin("c2", "desktop_write_file", &params)
            .is_none());
        reopened.complete(
            "desktop_write_file",
            &params,
            &json!({"id": "c2", "result": {"ok": true}, "error": null}),
        );
        assert_eq!(
            reopened.begin("c2", "desktop_write_file", &params).unwrap()["result"],
            json!({"ok": true})
        );
        // Reads are never journaled.
        assert!(reopened
            .begin("r1", "desktop_read_file", &json!({}))
            .is_none());
        assert!(reopened
            .begin("r1", "desktop_read_file", &json!({}))
            .is_none());
    }
}
