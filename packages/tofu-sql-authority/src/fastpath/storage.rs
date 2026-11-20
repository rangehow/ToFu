//! Durable small lineage records and exact front installation primitives.
use crate::error::{Failure, OpResult, StorageError};
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::Instant;

pub(super) fn io(error: std::io::Error) -> Failure {
    StorageError::new("database_unavailable", format!("Fastpath filesystem I/O failed: {error}")).into()
}
pub(super) fn appended(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}
pub(super) fn deadline(at: Instant) -> OpResult<()> {
    if Instant::now() >= at {
        return Err(StorageError::retryable("database_timeout", "SQLite backup deadline expired", 100).into());
    }
    Ok(())
}
pub(super) fn sync_dir(path: &Path) -> OpResult<()> {
    File::open(path).map_err(io)?.sync_all().map_err(io)
}
pub(super) fn size(path: &Path) -> OpResult<u64> {
    match fs::metadata(path) {
        Ok(m) => Ok(m.len()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Err(e) => Err(io(e)),
    }
}
pub(super) fn load(path: &Path) -> OpResult<Option<Value>> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io(e)),
    };
    if !metadata.file_type().is_file() || metadata.len() > 65536 {
        return Err(StorageError::new("database_integrity", "fastpath manifest is not a bounded regular file").into());
    }
    let mut bytes = Vec::new();
    File::open(path).map_err(io)?.take(65537).read_to_end(&mut bytes).map_err(io)?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|e| Failure::Internal(e.to_string()))?;
    if !value.is_object() || bytes.len() > 65536 {
        return Err(StorageError::new(
            "database_integrity",
            "fastpath manifest is invalid; refusing authority fallback",
        )
        .into());
    }
    Ok(Some(value))
}
pub(super) fn write_json(path: &Path, value: &Value) -> OpResult<()> {
    let temporary = appended(path, &format!(".new-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut output = OpenOptions::new().create_new(true).write(true).open(&temporary).map_err(io)?;
        output.write_all(&crate::json::dumps_sorted(value)).map_err(io)?;
        output.write_all(b"\n").map_err(io)?;
        output.sync_all().map_err(io)?;
        #[cfg(test)]
        super::fault::check("manifest_replace")?;
        fs::rename(&temporary, path).map_err(io)?;
        sync_dir(path.parent().expect("manifest parent"))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result
}
pub(super) fn header(path: &Path) -> OpResult<Option<[u8; 32]>> {
    let mut input = match File::open(path) {
        Ok(v) => v,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(io(e)),
    };
    let mut bytes = [0; 32];
    match input.read_exact(&mut bytes) {
        Ok(()) => Ok(Some(bytes)),
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => Ok(None),
        Err(e) => Err(io(e)),
    }
}
pub(super) fn manifest_path(shadow: &Path, value: &Value, key: &str, default: &str) -> OpResult<PathBuf> {
    let name = value[key].as_str().unwrap_or(default);
    if Path::new(name).components().count() != 1 || name == "." || name == ".." || name.contains('/') {
        return Err(StorageError::new("database_integrity", "Invalid fastpath shadow artifact name").into());
    }
    Ok(shadow.join(name))
}
pub(super) fn copy_durable(source: &Path, destination: &Path, at: Instant) -> OpResult<()> {
    let mut input = File::open(source).map_err(io)?;
    let mut output = OpenOptions::new().create(true).truncate(true).write(true).open(destination).map_err(io)?;
    let mut buffer = vec![0; 8 * 1024 * 1024];
    loop {
        deadline(at)?;
        let n = input.read(&mut buffer).map_err(io)?;
        if n == 0 {
            break;
        }
        output.write_all(&buffer[..n]).map_err(io)?;
    }
    #[cfg(test)]
    super::fault::check("snapshot_sync")?;
    output.sync_all().map_err(io)
}
pub(super) fn link_replace(source: &Path, destination: &Path) -> OpResult<()> {
    if source == destination {
        return Ok(());
    }
    let temporary = appended(destination, &format!(".link-{}", uuid::Uuid::new_v4().simple()));
    fs::hard_link(source, &temporary).map_err(io)?;
    fs::rename(temporary, destination).map_err(io)
}
pub(super) fn free_bytes(path: &Path) -> OpResult<u64> {
    let path = std::ffi::CString::new(path.as_os_str().as_bytes()).map_err(|e| Failure::Internal(e.to_string()))?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return Err(io(std::io::Error::last_os_error()));
    }
    let stats = unsafe { stats.assume_init() };
    Ok(stats.f_bavail * stats.f_frsize)
}
pub(super) fn require_capacity(path: &Path, bytes: u64) -> OpResult<()> {
    let reserve = (bytes / 20).clamp(1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024);
    let required = (2 * 1024 * 1024 * 1024u64).max(bytes.saturating_add(reserve));
    if free_bytes(path)? < required {
        return Err(
            StorageError::new("database_unavailable", "Insufficient free space for fastpath authority copy").into()
        );
    }
    Ok(())
}

pub(super) fn reclaim_generations(shadow: &Path, snapshot: &Path, wal: &Path) -> OpResult<()> {
    let pattern = regex::Regex::new(r"^(?:snapshot-[0-9]+-[0-9a-f]{32}\.sqlite3|shadow-[0-9]+-[0-9a-f]{32}\.wal)$")
        .expect("owned generation names");
    for entry in fs::read_dir(shadow).map_err(io)? {
        let entry = entry.map_err(io)?;
        let path = entry.path();
        if path == snapshot || path == wal || !pattern.is_match(&entry.file_name().to_string_lossy()) {
            continue;
        }
        if entry.file_type().map_err(io)?.is_file() {
            fs::remove_file(path).map_err(io)?;
        }
    }
    sync_dir(shadow)
}
