//! Online SQLite maintenance: verified project-local recovery copies and baseline.
//! Callers own the read lease and nonblocking backup lock; source connections may
//! belong to either the classic authority or a fastpath snapshot owner.

pub(crate) mod policy;

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{ffi, Connection};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::{Failure, OpResult, StorageError};
use crate::session::Session;

fn io(error: std::io::Error) -> Failure {
    Failure::Internal(error.to_string())
}

fn deadline(at: Instant, message: &str) -> OpResult<()> {
    if Instant::now() >= at {
        return Err(StorageError::retryable("database_timeout", message, 100).into());
    }
    Ok(())
}

fn appended(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

pub(crate) fn sync_directory(path: &Path) -> OpResult<()> {
    File::open(path).map_err(io)?.sync_all().map_err(io)
}

pub(crate) fn write_json(path: &Path, value: &Value) -> OpResult<()> {
    let replacement = appended(path, ".new");
    let mut file = OpenOptions::new().write(true).create_new(true).open(&replacement).map_err(io)?;
    file.write_all(&crate::json::dumps_sorted(value)).map_err(io)?;
    file.write_all(b"\n").map_err(io)?;
    file.sync_all().map_err(io)?;
    fs::rename(replacement, path).map_err(io)?;
    sync_directory(path.parent().expect("manifest parent"))
}

pub(crate) fn checksum(path: &Path, at: Instant) -> OpResult<String> {
    let mut input = File::open(path).map_err(io)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0; 4 * 1024 * 1024];
    let result = (|| loop {
        deadline(at, "Storage maintenance deadline expired")?;
        let count = input.read(&mut buffer).map_err(io)?;
        if count == 0 {
            return Ok(hex::encode(digest.finalize()));
        }
        digest.update(&buffer[..count]);
    })();
    unsafe {
        libc::posix_fadvise(input.as_raw_fd(), 0, 0, libc::POSIX_FADV_DONTNEED);
    }
    result
}

pub(crate) fn timestamps() -> (String, String) {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).expect("wall clock");
    let seconds = now.as_secs() as libc::time_t;
    let mut utc = std::mem::MaybeUninit::<libc::tm>::uninit();
    // gmtime_r writes only caller-owned memory and strftime receives fixed formats.
    unsafe {
        libc::gmtime_r(&seconds, utc.as_mut_ptr());
        let utc = utc.assume_init();
        let format = |pattern: &[u8]| {
            let mut bytes = [0u8; 64];
            let len = libc::strftime(bytes.as_mut_ptr().cast(), bytes.len(), pattern.as_ptr().cast(), &utc);
            String::from_utf8_lossy(&bytes[..len]).into_owned()
        };
        (format(b"%Y%m%dT%H%M%SZ\0"), format!("{}.{:06}+00:00", format(b"%Y-%m-%dT%H:%M:%S\0"), now.subsec_micros()))
    }
}

struct ManifestLock(Option<File>);

impl ManifestLock {
    fn acquire(manifest: &Path) -> OpResult<Self> {
        let file = OpenOptions::new().write(true).create(true).truncate(false).open(appended(manifest, ".lock")).ok();
        // json_store.locked_path also degrades on filesystems without flock;
        // the caller's backup lock still excludes concurrent local jobs.
        if let Some(file) = &file {
            unsafe {
                libc::flock(file.as_raw_fd(), libc::LOCK_EX);
            }
        }
        Ok(Self(file))
    }
}

impl Drop for ManifestLock {
    fn drop(&mut self) {
        if let Some(file) = &self.0 {
            unsafe {
                libc::flock(file.as_raw_fd(), libc::LOCK_UN);
            }
        }
    }
}

pub(crate) fn job(temporary: &Path, source: &Path, state: &str, estimate: u64, pages: Option<i32>) -> OpResult<()> {
    let _lock = ManifestLock::acquire(&appended(temporary, ".job.json"))?;
    let mut value = json!({"version":1,"pid":std::process::id(),"source":source,
        "temporary_path":temporary,"state":state,"updated_at":SystemTime::now().duration_since(UNIX_EPOCH).expect("wall clock").as_secs_f64(),
        "estimated_bytes":estimate});
    if let Some(pages) = pages {
        value["total_pages"] = json!(pages);
    }
    write_json(&appended(temporary, ".job.json"), &value)
}

fn unlink(path: &Path) -> OpResult<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(io(error)),
    }
}

pub(crate) fn retire_job(path: &Path) -> OpResult<()> {
    unlink(&appended(path, ".job.json"))?;
    unlink(&appended(path, ".job.json.lock"))
}

pub(crate) fn cleanup(path: &Path) -> OpResult<()> {
    unlink(path)?;
    unlink(&appended(path, "-wal"))?;
    unlink(&appended(path, "-shm"))?;
    retire_job(path)
}

fn copy(
    source: &Connection,
    destination: &Connection,
    at: Instant,
    complete: impl FnOnce(i32) -> OpResult<()>,
) -> OpResult<()> {
    struct Copy(*mut ffi::sqlite3_backup);
    impl Drop for Copy {
        fn drop(&mut self) {
            unsafe {
                ffi::sqlite3_backup_finish(self.0);
            }
        }
    }
    // The two Connections outlive Copy; no other call accesses their handles
    // while sqlite3_backup_step owns the destination connection.
    let backup =
        unsafe { ffi::sqlite3_backup_init(destination.handle(), c"main".as_ptr(), source.handle(), c"main".as_ptr()) };
    if backup.is_null() {
        return Err(rusqlite::Error::SqliteFailure(
            unsafe { ffi::Error::new(ffi::sqlite3_errcode(destination.handle())) },
            None,
        )
        .into());
    }
    let backup = Copy(backup);
    loop {
        let status = unsafe { ffi::sqlite3_backup_step(backup.0, 4096) };
        deadline(at, "SQLite backup deadline expired")?;
        match status {
            ffi::SQLITE_DONE => {
                complete(unsafe { ffi::sqlite3_backup_pagecount(backup.0) })?;
                return Ok(());
            }
            ffi::SQLITE_OK => {}
            ffi::SQLITE_BUSY | ffi::SQLITE_LOCKED => std::thread::sleep(Duration::from_millis(10)),
            code => return Err(rusqlite::Error::SqliteFailure(ffi::Error::new(code), None).into()),
        }
    }
}

/// Publish a copy without assuming the source's physical location. The caller
/// must not wrap this in a write transaction or hold an uncheckpointed fastpath
/// pin beyond its own owner-defined lifecycle.
pub fn backup_from_connection(
    source: &Connection,
    authority_path: &Path,
    data_dir: &Path,
    project_root: &Path,
    at: Instant,
) -> OpResult<Value> {
    let backups = data_dir.join("backups");
    fs::create_dir_all(&backups).map_err(io)?;
    let reclaimed = policy::reclaim(&backups)?;
    let (stamp, _) = timestamps();
    let id = uuid::Uuid::new_v4().simple().to_string();
    let target = backups.join(format!("storage-sqlite-{stamp}-{}.sqlite3", &id[..8]));
    let temporary = backups.join(format!(
        ".{}.tmp-{}",
        target.file_name().expect("target").to_string_lossy(),
        uuid::Uuid::new_v4().simple()
    ));
    if target.exists() {
        return Err(StorageError::new("database_conflict", "Backup target already exists").into());
    }
    let manifest_path = appended(&target, ".manifest.json");
    let result = (|| {
        let count: u64 = source.query_row("PRAGMA page_count", [], |row| row.get(0))?;
        let size: u64 = source.query_row("PRAGMA page_size", [], |row| row.get(0))?;
        let capacity = policy::capacity(&backups, count * size)?;
        job(&temporary, authority_path, "copying", count * size, None)?;
        {
            let destination = Connection::open(&temporary)?;
            destination.busy_timeout(Duration::from_millis(2000))?;
            destination.progress_handler(10_000, Some(move || Instant::now() >= at));
            copy(source, &destination, at, |pages| {
                job(&temporary, authority_path, "verifying", count * size, Some(pages))
            })?;
            let integrity = destination.query_row("PRAGMA integrity_check", [], |row| row.get::<_, String>(0));
            match integrity {
                Err(_) if Instant::now() >= at => {
                    return Err(StorageError::retryable(
                        "database_timeout",
                        "SQLite backup verification deadline expired",
                        100,
                    )
                    .into())
                }
                Err(error) => return Err(error.into()),
                Ok(value) if value != "ok" => {
                    return Err(StorageError::new("database_integrity", "SQLite backup verification failed").into())
                }
                Ok(_) => {}
            }
            destination.close().map_err(|(_, error)| Failure::Sqlite(error))?;
        }
        File::open(&temporary).map_err(io)?.sync_all().map_err(io)?;
        let bytes = fs::metadata(&temporary).map_err(io)?.len();
        let sha256 = checksum(&temporary, at)?;
        fs::rename(&temporary, &target).map_err(io)?;
        sync_directory(&backups)?;
        write_json(
            &manifest_path,
            &json!({"format":"tofu.storage-backup.v1","backend":"sqlite","created_at":timestamps().1,
            "artifact":target.file_name().expect("target").to_string_lossy(),"bytes":bytes,"sha256":sha256,"integrity":"ok"}),
        )?;
        let pruned = policy::prune(&backups, &target)?;
        let relative = |path: &Path| {
            path.strip_prefix(project_root)
                .map(|p| p.to_string_lossy().into_owned())
                .map_err(|e| Failure::Internal(e.to_string()))
        };
        let mut result = capacity;
        let object = result.as_object_mut().expect("capacity object");
        object.insert("ok".into(), json!(true));
        object.insert("backup".into(), json!(relative(&target)?));
        object.insert("manifest".into(), json!(relative(&manifest_path)?));
        object.insert("bytes".into(), json!(bytes));
        object.insert("sha256".into(), json!(sha256));
        object.insert("reclaimed_temp_artifacts".into(), json!(reclaimed));
        object.insert("pruned".into(), json!(pruned));
        Ok(result)
    })();
    if result.is_err() {
        cleanup(&temporary)?;
        if target.exists() && !manifest_path.exists() {
            unlink(&target)?;
        }
    }
    retire_job(&temporary)?;
    result
}

/// Copy a source path whose lifetime/fastpath pin is owned by the caller.
pub fn backup_from_path(
    source_path: &Path,
    authority_path: &Path,
    data_dir: &Path,
    project_root: &Path,
    at: Instant,
) -> OpResult<Value> {
    let connection = Connection::open_with_flags(source_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    connection.busy_timeout(at.saturating_duration_since(Instant::now()))?;
    connection.progress_handler(1000, Some(move || Instant::now() >= at));
    backup_from_connection(&connection, authority_path, data_dir, project_root, at)
}

pub fn baseline(session: &Session<'_>, authority_path: &Path, at: Instant) -> OpResult<Value> {
    let rows = session.fetch_all("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' ORDER BY type, name", &[])?;
    let mut tables = Vec::new();
    let mut indexes = Vec::new();
    for row in &rows {
        if row.text("type") == "index" {
            indexes.push(row.text("name"));
            continue;
        }
        deadline(at, "Storage baseline deadline expired")?;
        let identifier = row.text("name").replace('"', "\"\"");
        let count = session
            .fetch_one(&format!("SELECT COUNT(*) AS count FROM \"{identifier}\""), &[])?
            .expect("COUNT row")
            .int("count")?;
        tables.push(json!({"name":row.text("name"),"rows":count}));
    }
    let version = session
        .fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &crate::params!["schema_version"])?;
    let version = version.map(|row| row.int("meta_value")).transpose()?;
    Ok(json!({"backend":"sqlite","schema_version":version,"tables":tables,"indexes":indexes,
        "database_bytes":fs::metadata(authority_path).map_err(io)?.len()}))
}

#[cfg(test)]
mod tests;
