//! Mount classification (`storage_capabilities.describe_mount`), used for
//! the online-reclaim topology gate and the health preflight report.

use std::path::Path;

use serde_json::{json, Value};

const NETWORK: &[&str] = &[
    "9p",
    "afs",
    "beegfs",
    "bgfuse",
    "ceph",
    "cifs",
    "davfs",
    "davfs2",
    "gfs2",
    "glusterfs",
    "gpfs",
    "lustre",
    "ncpfs",
    "nfs",
    "nfs4",
    "ocfs2",
    "smb",
    "smb2",
    "smb3",
    "sshfs",
];
const LOCAL_BLOCK: &[&str] = &[
    "apfs", "bcachefs", "btrfs", "exfat", "ext2", "ext3", "ext4", "f2fs", "hfs", "hfsplus", "jfs", "nilfs2", "ntfs",
    "ntfs3", "reiserfs", "ufs", "vfat", "xfs", "zfs",
];

#[derive(Clone, Debug)]
pub struct Mount {
    pub filesystem_type: String,
    pub mount_point: String,
    pub storage_class: &'static str,
}

impl Mount {
    pub fn as_json(&self) -> Value {
        json!({
            "filesystem_type": self.filesystem_type,
            "mount_point": self.mount_point,
            "storage_class": self.storage_class,
        })
    }
}

pub fn classify(filesystem_type: &str) -> &'static str {
    let normalized = filesystem_type.to_lowercase();
    if NETWORK.contains(&normalized.as_str())
        || ["fuse.beegfs", "fuse.bgfuse", "fuse.ceph", "fuse.glusterfs", "fuse.sshfs"]
            .iter()
            .any(|prefix| normalized.starts_with(prefix))
    {
        return "network-filesystem";
    }
    match normalized.as_str() {
        "ramfs" | "tmpfs" => "memory-filesystem",
        "aufs" | "overlay" | "overlayfs" => "container-overlay",
        other if LOCAL_BLOCK.contains(&other) => "local-block",
        other if other.starts_with("fuse") => "userspace-filesystem",
        _ => "unknown",
    }
}

fn decode(value: &str) -> String {
    value.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
}

pub fn describe(path: &Path) -> Mount {
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let resolved = resolved.to_string_lossy().into_owned();
    let text = std::fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    let mut best_mount = String::new();
    let mut best_type = String::new();
    for line in text.lines() {
        let Some((before, after)) = line.split_once(" - ") else { continue };
        let fields: Vec<&str> = before.split_whitespace().collect();
        let Some(filesystem) = after.split_whitespace().next() else { continue };
        if fields.len() < 5 {
            continue;
        }
        let mount_point = decode(fields[4]);
        let normalized = {
            let trimmed = mount_point.trim_end_matches('/');
            if trimmed.is_empty() {
                "/".to_owned()
            } else {
                trimmed.to_owned()
            }
        };
        let prefix = format!("{}/", normalized.trim_end_matches('/'));
        if !(resolved == normalized || resolved.starts_with(&prefix)) {
            continue;
        }
        if normalized.len() >= best_mount.len() {
            best_mount = normalized;
            best_type = filesystem.to_owned();
        }
    }
    Mount {
        storage_class: classify(&best_type),
        filesystem_type: if best_type.is_empty() { "unknown".into() } else { best_type },
        mount_point: best_mount,
    }
}

/// `describe_mount(...).persistence`.
fn persistence(path: &Path, storage_class: &str) -> &'static str {
    let base = match storage_class {
        "memory-filesystem" => return "ephemeral",
        "local-block" => "persistent",
        _ => "unknown",
    };
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut ephemeral_roots = vec![std::env::temp_dir()];
    if let Ok(runtime) = std::env::var("XDG_RUNTIME_DIR") {
        if !runtime.trim().is_empty() {
            ephemeral_roots.push(runtime.trim().into());
        }
    }
    for root in ephemeral_roots {
        let root = std::fs::canonicalize(&root).unwrap_or(root);
        if resolved.starts_with(&root) {
            return "ephemeral";
        }
    }
    base
}

/// `preflight.run_filesystem_preflight`: refuse an authority volume that
/// lacks free space, fsync, atomic replace or enforced lock exclusion; report
/// (never refuse on) its latency.
pub fn filesystem_preflight(data_dir: &Path) -> Result<Value, crate::error::StorageError> {
    use std::io::Write;
    use std::os::fd::AsRawFd;

    let unavailable = || {
        crate::error::StorageError::new(
            "database_unavailable",
            "Project filesystem failed storage durability preflight",
        )
    };
    std::fs::create_dir_all(data_dir).map_err(|_| unavailable())?;
    let free_bytes = {
        let path = std::ffi::CString::new(data_dir.as_os_str().as_encoded_bytes()).map_err(|_| unavailable())?;
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        if unsafe { libc::statvfs(path.as_ptr(), &mut stat) } != 0 {
            return Err(unavailable());
        }
        stat.f_bavail as u64 * stat.f_frsize as u64
    };
    let minimum: u64 = match std::env::var("TOFU_STORAGE_MIN_FREE_BYTES").ok().filter(|raw| !raw.is_empty()) {
        Some(raw) => raw.trim().parse().map_err(|_| unavailable())?,
        None => 256 * 1024 * 1024,
    };
    if free_bytes < minimum {
        return Err(crate::error::StorageError::new("database_unavailable", "Insufficient project storage space"));
    }
    let stem = format!(".storage-preflight-{}", uuid::Uuid::new_v4().simple());
    let source = data_dir.join(format!("{stem}.new"));
    let target = data_dir.join(format!("{stem}.ready"));
    let lock_path = data_dir.join(format!("{stem}.lock"));
    let started = std::time::Instant::now();
    let probe = || -> std::io::Result<bool> {
        let payload: Vec<u8> = (0..256).flat_map(|_| *uuid::Uuid::new_v4().as_bytes()).collect();
        {
            let mut stream = std::fs::OpenOptions::new().write(true).create_new(true).open(&source)?;
            stream.write_all(&payload)?;
            stream.sync_all()?;
        }
        std::fs::rename(&source, &target)?;
        if std::fs::read(&target)? != payload {
            return Err(std::io::Error::other("atomic replacement content mismatch"));
        }
        std::fs::File::open(data_dir)?.sync_all()?;
        let lock = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&lock_path)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        let contender = std::fs::OpenOptions::new().read(true).write(true).open(&lock_path)?;
        let excluded = unsafe { libc::flock(contender.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0;
        if !excluded {
            unsafe { libc::flock(contender.as_raw_fd(), libc::LOCK_UN) };
        }
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) };
        Ok(excluded)
    };
    let outcome = probe();
    for path in [&source, &target, &lock_path] {
        let _ = std::fs::remove_file(path);
    }
    if !matches!(outcome, Ok(true)) {
        return Err(unavailable());
    }
    let fsync_ms = started.elapsed().as_secs_f64() * 1000.0;
    let mount = describe(data_dir);
    Ok(json!({
        "filesystem": data_dir.display().to_string(),
        "filesystem_type": mount.filesystem_type,
        "storage_class": mount.storage_class,
        "persistence": persistence(data_dir, mount.storage_class),
        "free_bytes": free_bytes,
        "fsync_ms": crate::json::float_value((fsync_ms * 1000.0).round() / 1000.0),
        "atomic_replace": true,
        "file_lock": true,
    }))
}

/// `reclaim_policy.online_reclaim_allowed`.
pub fn online_reclaim_allowed(storage_class: &str) -> bool {
    matches!(storage_class, "local-block" | "memory-filesystem" | "container-overlay")
}

/// `reclaim_policy.requires_offline_compaction`.
pub fn requires_offline_compaction(freelist_pages: i64, page_count: i64) -> bool {
    let free = freelist_pages.max(0);
    let total = page_count.max(0);
    free >= 1_048_576 && free as f64 / total.max(1) as f64 >= 0.25
}
