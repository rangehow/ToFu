//! Filesystem-only recovery-copy capacity, publication retention and stale jobs.
use std::fs::{self, File};
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use super::{appended, io, unlink};
use crate::error::{OpResult, StorageError};

fn setting(name: &str, default: u64, minimum: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .map(|v| v.max(minimum as i64) as u64)
        .unwrap_or(default)
}

fn retention() -> usize {
    setting("TOFU_STORAGE_SQLITE_BACKUP_RETENTION", 2, 1) as usize
}

fn regular(path: &Path) -> Option<fs::Metadata> {
    fs::symlink_metadata(path).ok().filter(|m| m.file_type().is_file())
}

fn allocated(metadata: &fs::Metadata) -> u64 {
    let blocks = metadata.blocks() * 512;
    if blocks > 0 {
        blocks
    } else {
        metadata.len()
    }
}

fn publication_valid(path: &Path, bytes: u64) -> bool {
    let manifest = appended(path, ".manifest.json");
    if regular(&manifest).is_none() {
        return false;
    }
    let read = || -> Option<Value> {
        let mut payload = Vec::new();
        File::open(manifest).ok()?.take(16 * 1024 + 1).read_to_end(&mut payload).ok()?;
        if payload.len() > 16 * 1024 {
            return None;
        }
        serde_json::from_slice(&payload).ok()
    };
    let Some(value) = read() else {
        return false;
    };
    value["format"] == "tofu.storage-backup.v1"
        && value["backend"] == "sqlite"
        && value["artifact"].as_str() == path.file_name().and_then(|v| v.to_str())
        && value["bytes"].as_u64() == Some(bytes)
        && value["integrity"] == "ok"
        && value["sha256"].as_str().is_some_and(|hash| {
            hash.len() == 64 && hash.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
}

struct Artifact {
    path: PathBuf,
    bytes: u64,
    modified: f64,
    valid: bool,
}

fn inventory(backups: &Path) -> OpResult<Vec<Artifact>> {
    let mut artifacts = Vec::new();
    for entry in fs::read_dir(backups).map_err(io)? {
        let path = entry.map_err(io)?.path();
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        if !name.starts_with("storage-sqlite-") || !name.ends_with(".sqlite3") {
            continue;
        }
        let Some(metadata) = regular(&path) else {
            continue;
        };
        artifacts.push(Artifact {
            bytes: allocated(&metadata),
            modified: ((metadata.mtime() as f64 + metadata.mtime_nsec() as f64 / 1e9) * 1000.0).round() / 1000.0,
            valid: publication_valid(&path, metadata.len()),
            path,
        });
    }
    artifacts.sort_by(|a, b| b.modified.total_cmp(&a.modified).then_with(|| b.path.cmp(&a.path)));
    Ok(artifacts)
}

fn rollback_name(name: &str) -> bool {
    let Some(stamp) = name.strip_prefix("tofu.db.pre-compact-") else {
        return false;
    };
    let bytes = stamp.as_bytes();
    bytes.len() == 16
        && bytes[8] == b'T'
        && bytes[15] == b'Z'
        && bytes.iter().enumerate().all(|(i, b)| i == 8 || i == 15 || b.is_ascii_digit())
}

pub(crate) fn capacity(backups: &Path, estimate: u64) -> OpResult<Value> {
    capacity_with_rotation(backups, estimate, false)
}

pub(crate) fn capacity_fastpath(backups: &Path, estimate: u64) -> OpResult<Value> {
    capacity_with_rotation(backups, estimate, true)
}

fn capacity_with_rotation(backups: &Path, estimate: u64, allow_rotation: bool) -> OpResult<Value> {
    let artifacts = inventory(backups)?;
    let retained: u64 = artifacts.iter().map(|a| a.bytes).sum();
    let data = backups.parent().expect("backups parent");
    let same_volume =
        fs::metadata(backups).and_then(|b| fs::metadata(data).map(|d| b.dev() == d.dev())).unwrap_or(true);
    let mut rollback = 0;
    if same_volume {
        for entry in fs::read_dir(data).map_err(io)? {
            let path = entry.map_err(io)?.path();
            if rollback_name(&path.file_name().unwrap_or_default().to_string_lossy()) {
                if let Some(metadata) = regular(&path) {
                    rollback += allocated(&metadata);
                }
            }
        }
    }
    let retained = retained + rollback;
    let budget = recovery_budget_mib(data) * 1024 * 1024;
    let peak_projected = retained + estimate;
    let mut projected = peak_projected;
    let mut retire = Vec::new();
    if allow_rotation && peak_projected > budget && rollback + estimate <= budget {
        let old_budget = budget - rollback - estimate;
        let mut kept_bytes: u64 = artifacts.iter().filter(|a| !a.valid).map(|a| a.bytes).sum();
        let mut kept_count = 0;
        for artifact in &artifacts {
            if !artifact.valid {
                continue;
            }
            if kept_count < retention().saturating_sub(1) && kept_bytes + artifact.bytes <= old_budget {
                kept_bytes += artifact.bytes;
                kept_count += 1;
            } else {
                retire.push(artifact.path.file_name().expect("artifact name").to_string_lossy().into_owned());
            }
        }
        projected = rollback + estimate + kept_bytes;
    }
    let rotation_required = peak_projected > budget && !retire.is_empty() && projected <= budget;
    if peak_projected > budget && !rotation_required {
        return Err(StorageError::new("database_unavailable", format!("SQLite recovery-copy budget exceeded ({peak_projected} > {budget} bytes); use an independent backup volume or raise the explicit bounded budget")).into());
    }
    let reserve = setting("TOFU_STORAGE_SQLITE_BACKUP_RESERVE_BYTES", (64 * 1024 * 1024).max(estimate / 20), 0);
    let path = std::ffi::CString::new(backups.as_os_str().as_bytes())
        .map_err(|e| crate::error::Failure::Internal(e.to_string()))?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    let status = unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) };
    if status != 0 {
        return Err(io(std::io::Error::last_os_error()));
    }
    let stats = unsafe { stats.assume_init() };
    let free = stats.f_bavail * stats.f_frsize;
    if free < estimate + reserve {
        return Err(
            StorageError::new("database_unavailable", "Insufficient capacity for a verified SQLite backup").into()
        );
    }
    let mut result = json!({"estimated_bytes":estimate,"recovery_copy_budget_bytes":budget,
        "retained_recovery_bytes":retained,"projected_recovery_bytes":projected,"same_volume_rollback_bytes":rollback});
    if allow_rotation {
        result["peak_projected_recovery_bytes"] = json!(peak_projected);
        result["budget_rotation_required"] = json!(rotation_required);
        result["retire_verified_artifacts"] = json!(retire);
    }
    Ok(result)
}

fn recovery_budget_mib(data: &Path) -> u64 {
    let override_value = std::env::var("TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .filter(|v| *v > 0);
    let default = || {
        if std::env::var("TOFU_DEPLOYMENT_MODE").unwrap_or_default().trim().eq_ignore_ascii_case("distributed") {
            return 1048576;
        }
        let Ok(path) = std::ffi::CString::new(data.as_os_str().as_bytes()) else {
            return 65536;
        };
        let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        if unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 {
            return 65536;
        }
        let stats = unsafe { stats.assume_init() };
        let total_mib = stats.f_blocks * stats.f_frsize / (1024 * 1024);
        (total_mib / 2).clamp(4096, 512 * 1024)
    };
    override_value.unwrap_or_else(default).clamp(4096, 8 * 1024 * 1024)
}

pub(crate) fn prune(backups: &Path, preserve: &Path) -> OpResult<u64> {
    prune_with_retire(backups, preserve, &[])
}

pub(crate) fn prune_with_retire(backups: &Path, preserve: &Path, retire: &[String]) -> OpResult<u64> {
    let candidates: Vec<_> = inventory(backups)?.into_iter().filter(|a| a.valid).collect();
    if !candidates.iter().any(|a| a.path == preserve) {
        return Err(
            StorageError::new("database_integrity", "Backup retention requires a published recovery point").into()
        );
    }
    let mut removed = 0;
    for (index, artifact) in candidates.into_iter().enumerate() {
        if artifact.path == preserve
            || (index < retention()
                && !retire.iter().any(|name| artifact.path.file_name().is_some_and(|file| file == name.as_str())))
        {
            continue;
        }
        unlink(&artifact.path)?;
        unlink(&appended(&artifact.path, ".manifest.json"))?;
        removed += 1;
    }
    Ok(removed)
}

fn alive(value: &Value) -> bool {
    let pid = value
        .as_i64()
        .or_else(|| value.as_str()?.parse().ok())
        .or_else(|| value.as_f64().map(|v| v as i64))
        .unwrap_or(0);
    if pid < 1 || pid > i32::MAX as i64 {
        return false;
    }
    if pid == std::process::id() as i64 {
        return true;
    }
    unsafe { libc::kill(pid as i32, 0) == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM) }
}

fn age(metadata: &fs::Metadata) -> f64 {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).expect("wall clock").as_secs_f64();
    now - (metadata.mtime() as f64 + metadata.mtime_nsec() as f64 / 1e9)
}

pub(crate) fn reclaim(backups: &Path) -> OpResult<u64> {
    let ttl = setting("TOFU_STORAGE_SQLITE_BACKUP_TEMP_TTL_SECONDS", 3600, 1) as f64;
    let mut removed = 0;
    let mut protected = std::collections::HashSet::new();
    let paths: Vec<_> =
        fs::read_dir(backups).map_err(io)?.map(|e| e.map(|e| e.path()).map_err(io)).collect::<OpResult<_>>()?;
    for manifest in &paths {
        let name = manifest.file_name().unwrap_or_default().to_string_lossy();
        if !name.starts_with(".storage-sqlite-") || !name.contains(".tmp-") || !name.ends_with(".job.json") {
            continue;
        }
        let temporary = PathBuf::from(manifest.as_os_str().to_string_lossy().trim_end_matches(".job.json"));
        let Some(metadata) = regular(manifest) else {
            protected.insert(temporary);
            continue;
        };
        let Ok(value) =
            fs::read(manifest).and_then(|v| serde_json::from_slice::<Value>(&v).map_err(std::io::Error::other))
        else {
            protected.insert(temporary);
            continue;
        };
        if age(&metadata) <= ttl || alive(&value["pid"]) {
            protected.insert(temporary);
            continue;
        }
        for path in [&temporary, manifest] {
            if path.try_exists().map_err(io)? {
                unlink(path)?;
                removed += 1;
            }
        }
        unlink(&appended(manifest, ".lock"))?;
    }
    for temporary in paths {
        let name = temporary.file_name().unwrap_or_default().to_string_lossy();
        if !name.starts_with(".storage-sqlite-")
            || !name.contains(".tmp-")
            || name.ends_with(".job.json")
            || name.ends_with(".job.json.lock")
            || protected.contains(&temporary)
        {
            continue;
        }
        let manifest = appended(&temporary, ".job.json");
        let lock = super::ManifestLock::acquire(&manifest)?;
        match fs::symlink_metadata(&manifest) {
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => continue,
        }
        let Some(metadata) = regular(&temporary) else {
            continue;
        };
        if age(&metadata) > ttl {
            unlink(&temporary)?;
            removed += 1;
        }
        drop(lock);
        unlink(&appended(&manifest, ".lock"))?;
    }
    removed += reclaim_legacy(backups, ttl)?;
    Ok(removed)
}

fn reclaim_legacy(backups: &Path, ttl: f64) -> OpResult<u64> {
    let directory = backups.parent().expect("data dir").join("db_snapshots");
    let Ok(entries) = fs::read_dir(&directory) else {
        return Ok(0);
    };
    let pattern = regex::Regex::new(r"^\.tofu-\d{8}_\d{6}-(?P<pid>[1-9]\d*)-[0-9a-f]{8}\.sqlite3\.tmp-[0-9a-f]{32}$")
        .expect("legacy name regex");
    let mut removed = 0;
    for entry in entries.take(256) {
        let path = entry.map_err(io)?.path();
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let Some(captures) = pattern.captures(&name) else {
            continue;
        };
        let Some(initial) = regular(&path) else {
            continue;
        };
        if age(&initial) <= ttl || alive(&json!(captures["pid"].to_string())) {
            continue;
        }
        let mut artifacts = Vec::new();
        let manifest = appended(&path, ".job.json");
        if fs::symlink_metadata(&manifest).is_ok() {
            let Some(metadata) = regular(&manifest) else {
                continue;
            };
            let Ok(value) =
                fs::read(&manifest).and_then(|v| serde_json::from_slice::<Value>(&v).map_err(std::io::Error::other))
            else {
                continue;
            };
            if age(&metadata) <= ttl || alive(&value["pid"]) {
                continue;
            }
            artifacts.push(manifest);
        }
        let mut unsafe_companion = false;
        for suffix in ["-journal", "-wal", "-shm"] {
            let companion = appended(&path, suffix);
            if fs::symlink_metadata(&companion).is_err() {
                continue;
            }
            if regular(&companion).is_none() {
                unsafe_companion = true;
                break;
            }
            artifacts.push(companion);
        }
        if unsafe_companion {
            continue;
        }
        let Some(current) = regular(&path) else {
            continue;
        };
        if (initial.dev(), initial.ino(), initial.len(), initial.mtime(), initial.mtime_nsec())
            != (current.dev(), current.ino(), current.len(), current.mtime(), current.mtime_nsec())
        {
            continue;
        }
        artifacts.push(path);
        for artifact in artifacts {
            match fs::remove_file(artifact) {
                Ok(()) => removed += 1,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(io(error)),
            }
        }
    }
    if removed > 0 {
        super::sync_directory(&directory)?;
    }
    Ok(removed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_validation_is_bounded_and_strict() {
        let root = tempfile::tempdir().unwrap();
        let artifact = root.path().join("storage-sqlite-test.sqlite3");
        fs::write(&artifact, b"copy").unwrap();
        let manifest = appended(&artifact, ".manifest.json");
        let mut value = json!({"format":"tofu.storage-backup.v1","backend":"sqlite","artifact":"storage-sqlite-test.sqlite3","bytes":4,"integrity":"ok","sha256":"a".repeat(64)});
        super::super::write_json(&manifest, &value).unwrap();
        assert!(publication_valid(&artifact, 4));
        value["bytes"] = json!(4.0);
        super::super::write_json(&manifest, &value).unwrap();
        assert!(!publication_valid(&artifact, 4));
        fs::write(&manifest, vec![b' '; 16385]).unwrap();
        assert!(!publication_valid(&artifact, 4));
    }

    #[test]
    fn prune_refuses_unpublished_preserve() {
        let root = tempfile::tempdir().unwrap();
        assert!(
            matches!(prune(root.path(), &root.path().join("absent")), Err(crate::error::Failure::Storage(e)) if e.code == "database_integrity")
        );
    }

    #[test]
    fn forced_rotation_never_retires_a_previous_point_before_new_publication() {
        let root = tempfile::tempdir().unwrap();
        let old = root.path().join("storage-sqlite-old.sqlite3");
        let new = root.path().join("storage-sqlite-new.sqlite3");
        fs::write(&old, b"old").unwrap();
        let publish = |path: &Path| {
            super::super::write_json(
                &appended(path, ".manifest.json"),
                &json!({
                    "format":"tofu.storage-backup.v1", "backend":"sqlite",
                    "artifact":path.file_name().unwrap().to_str().unwrap(),
                    "bytes":3, "integrity":"ok", "sha256":"a".repeat(64)
                }),
            )
            .unwrap();
        };
        publish(&old);
        let retired = vec![old.file_name().unwrap().to_str().unwrap().to_owned()];
        assert!(prune_with_retire(root.path(), &new, &retired).is_err());
        assert!(old.is_file());
        fs::write(&new, b"new").unwrap();
        publish(&new);
        assert_eq!(prune_with_retire(root.path(), &new, &retired).unwrap(), 1);
        assert!(new.is_file());
        assert!(!old.exists());
    }

    #[test]
    fn fastpath_capacity_records_peak_without_mutating_recovery_points() {
        let root = tempfile::tempdir().unwrap();
        let backups = root.path().join("backups");
        fs::create_dir(&backups).unwrap();
        let plan = capacity_fastpath(&backups, 0).unwrap();
        assert_eq!(plan["budget_rotation_required"], false);
        assert_eq!(plan["peak_projected_recovery_bytes"], 0);
        assert_eq!(plan["retire_verified_artifacts"], json!([]));
        assert_eq!(fs::read_dir(&backups).unwrap().count(), 0);
    }

    #[test]
    fn rollback_names_are_exact() {
        assert!(rollback_name("tofu.db.pre-compact-20261118T120000Z"));
        assert!(!rollback_name("tofu.db.pre-compact-20261118T120000Z-wal"));
        assert!(!rollback_name("tofu.db.pre-compact-20261118X120000Z"));
    }
}
