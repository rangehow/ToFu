//! Verified backup publication from an immutable checkpointed shadow generation.
use super::{storage, Fastpath};
use crate::backup::{self, policy};
use crate::error::{Failure, OpResult, StorageError};
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::fs::{self, File};
use std::path::Path;
use std::time::Instant;

impl Fastpath {
    /// The caller owns the writer lock and the backend's nonblocking backup lock.
    pub fn backup(&self, writer: &Connection, data_dir: &Path, project_root: &Path, at: Instant) -> OpResult<Value> {
        let backups = data_dir.join("backups");
        fs::create_dir_all(&backups).map_err(storage::io)?;
        let reclaimed = policy::reclaim(&backups)?;
        let (stamp, _) = backup::timestamps();
        let id = uuid::Uuid::new_v4().simple().to_string();
        let target = backups.join(format!("storage-sqlite-{stamp}-{}.sqlite3", &id[..8]));
        let temporary = backups.join(format!(
            ".{}.tmp-{}",
            target.file_name().expect("backup name").to_string_lossy(),
            uuid::Uuid::new_v4().simple()
        ));
        let manifest = storage::appended(&target, ".manifest.json");
        let result = (|| -> OpResult<Value> {
            let estimate = storage::size(&self.local_db)?;
            let mut capacity = policy::capacity_fastpath(&backups, estimate)?;
            let require_hardlink = capacity["budget_rotation_required"].as_bool().unwrap_or(false);
            let retire: Vec<String> = capacity["retire_verified_artifacts"]
                .as_array()
                .expect("retirement names")
                .iter()
                .map(|value| value.as_str().expect("artifact name").to_owned())
                .collect();
            capacity.as_object_mut().expect("capacity object").remove("retire_verified_artifacts");
            backup::job(&temporary, &self.local_db, "copying", estimate, None)?;
            let mut state = self.state.lock().expect("fastpath state");
            self.snapshot_locked(&mut state, writer, at)?;
            storage::deadline(at)?;
            let strategy = pin(&state.snapshot, &temporary, require_hardlink, at)?;
            let generation = state.generation;
            let recovery = state.recovery_point;
            drop(state);
            backup::job(&temporary, &self.local_db, "verifying", estimate, None)?;
            // immutable=1 prevents SQLite from creating sidecars or modifying a
            // hard-linked snapshot during verification, even on connection close.
            let uri = format!(
                "file:{}?immutable=1",
                temporary.to_string_lossy().replace('%', "%25").replace('?', "%3f").replace('#', "%23")
            );
            let connection =
                Connection::open_with_flags(uri, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI)?;
            connection.progress_handler(1000, Some(move || Instant::now() >= at));
            let integrity: String = connection.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
            if integrity != "ok" {
                return Err(StorageError::new("database_integrity", "SQLite backup verification failed").into());
            }
            drop(connection);
            storage::deadline(at)?;
            File::open(&temporary).map_err(storage::io)?.sync_all().map_err(storage::io)?;
            let bytes = storage::size(&temporary)?;
            let sha256 = backup::checksum(&temporary, at)?;
            let recovery_point_at = iso_timestamp(recovery);
            fs::rename(&temporary, &target).map_err(storage::io)?;
            storage::sync_dir(&backups)?;
            backup::write_json(
                &manifest,
                &json!({"format":"tofu.storage-backup.v1","backend":"sqlite",
                "created_at":backup::timestamps().1,"artifact":target.file_name().expect("target name").to_string_lossy(),
                "bytes":bytes,"sha256":sha256,"integrity":"ok","source_mode":"fastpath-checkpointed-shadow",
                "snapshot_generation":generation,"copy_strategy":strategy,"recovery_point_at":recovery_point_at}),
            )?;
            let pruned = policy::prune_with_retire(&backups, &target, &retire)?;
            let relative = |p: &Path| {
                p.strip_prefix(project_root)
                    .map(|v| v.to_string_lossy().into_owned())
                    .map_err(|e| Failure::Internal(e.to_string()))
            };
            let object = capacity.as_object_mut().expect("capacity object");
            for (key, value) in json!({"ok":true,"backup":relative(&target)?,"manifest":relative(&manifest)?,
                "bytes":bytes,"sha256":sha256,"reclaimed_temp_artifacts":reclaimed,"pruned":pruned,
                "budget_retired_backups":retire.len(),
                "source_mode":"fastpath-checkpointed-shadow","snapshot_generation":generation,
                "copy_strategy":strategy,"recovery_point_at":recovery_point_at})
            .as_object()
            .expect("result")
            {
                object.insert(key.clone(), value.clone());
            }
            Ok(capacity)
        })();
        if result.is_err() {
            backup::cleanup(&temporary)?;
            if target.exists() && !manifest.exists() {
                fs::remove_file(&target).map_err(storage::io)?;
            }
        }
        backup::retire_job(&temporary)?;
        result
    }
}
fn iso_timestamp(seconds: f64) -> String {
    let time = seconds as libc::time_t;
    let mut utc = std::mem::MaybeUninit::<libc::tm>::uninit();
    unsafe {
        libc::gmtime_r(&time, utc.as_mut_ptr());
        let utc = utc.assume_init();
        let mut bytes = [0u8; 64];
        let n = libc::strftime(bytes.as_mut_ptr().cast(), bytes.len(), c"%Y-%m-%dT%H:%M:%S".as_ptr(), &utc);
        format!("{}.{:06}+00:00", String::from_utf8_lossy(&bytes[..n]), ((seconds.fract() * 1e6) as u32))
    }
}

fn pin(source: &Path, destination: &Path, require_hardlink: bool, at: Instant) -> OpResult<&'static str> {
    let error = match fs::hard_link(source, destination) {
        Ok(()) => return Ok("hardlink"),
        Err(error) => error,
    };
    if !matches!(error.raw_os_error(), Some(libc::EXDEV | libc::EPERM | libc::EOPNOTSUPP)) {
        return Err(storage::io(error));
    }
    if error.raw_os_error() != Some(libc::EXDEV) {
        let sibling = storage::appended(source, &format!(".backup-link-{}", uuid::Uuid::new_v4().simple()));
        if fs::hard_link(source, &sibling).is_ok() {
            storage::sync_dir(source.parent().expect("snapshot parent"))?;
            if fs::rename(&sibling, destination).is_ok() {
                storage::sync_dir(destination.parent().expect("backup parent"))?;
                return Ok("hardlink-rename");
            }
            let _ = fs::remove_file(sibling);
        }
    }
    if require_hardlink {
        return Err(StorageError::new(
            "database_unavailable",
            "SQLite backup budget rotation requires a same-filesystem hard link; refusing full-copy fallback",
        )
        .into());
    }
    storage::copy_durable(source, destination, at)?;
    Ok("sequential-copy")
}
