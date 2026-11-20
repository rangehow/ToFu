//! Worker runtime replica and shared bytecode cache leases.
//!
//! Selection policy stays in `serverctl_pkg.runtime_replica` and
//! `serverctl_pkg.python_bytecode_cache`, reached through the
//! `prepare-worker-runtime` bridge operation. The leases themselves must live
//! in this process for the worker's lifetime (the bridge child exits), so the
//! manager re-takes them here with the owners' exact lock rules: a shared,
//! non-blocking `flock` on the generation's `.tofu-replica.lease` (released
//! when its manifest is gone, as collection removes the manifest first) and on
//! the cache namespace's `.active.lock` (regular, owned, private file). A
//! lease that cannot be re-taken never runs the worker on that replica or
//! cache: the spawn falls back to the source interpreter / no cache prefix.

use crate::json::Object;
use serde_json::Value;
use std::ffi::CString;
use std::os::fd::{FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

const REPLICA_SCHEMA: i64 = 1;
const MANIFEST_NAME: &str = ".tofu-replica.json";
const LEASE_NAME: &str = ".tofu-replica.lease";
const ACTIVE_LOCK_NAME: &str = ".active.lock";

fn manifest(generation: &Path) -> Option<Object> {
    let text = std::fs::read_to_string(generation.join(MANIFEST_NAME)).ok()?;
    match serde_json::from_str::<Value>(&text).ok()? {
        Value::Object(map) if map.get("schema").and_then(Value::as_i64) == Some(REPLICA_SCHEMA) => {
            Some(map)
        }
        _ => None,
    }
}

fn open_shared_lock(path: &Path, create: bool, private_file: bool) -> Option<OwnedFd> {
    let raw = CString::new(path.as_os_str().as_bytes()).ok()?;
    let flags =
        libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC | if create { libc::O_CREAT } else { 0 };
    // SAFETY: a valid NUL-terminated path; the descriptor is owned below.
    let fd = unsafe { libc::open(raw.as_ptr(), flags, 0o600 as libc::c_uint) };
    if fd < 0 {
        return None;
    }
    // SAFETY: `fd` was just opened by this process and is owned once.
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    if private_file {
        // SAFETY: zeroed stat is a valid out-parameter for fstat.
        let mut info: libc::stat = unsafe { std::mem::zeroed() };
        // SAFETY: `fd` is open; `info` is a valid pointer.
        if unsafe { libc::fstat(fd, &mut info) } != 0
            || info.st_mode & libc::S_IFMT != libc::S_IFREG
            // SAFETY: getuid has no preconditions.
            || info.st_uid != unsafe { libc::getuid() }
            || info.st_mode & 0o077 != 0
        {
            return None;
        }
    }
    // SAFETY: `fd` is open.
    if unsafe { libc::flock(fd, libc::LOCK_SH | libc::LOCK_NB) } != 0 {
        return None;
    }
    Some(owned)
}

/// `runtime_replica.lease_generation`.
pub fn lease_generation(generation: &Path) -> Option<OwnedFd> {
    let lease_path = generation.join(LEASE_NAME);
    let descriptor = open_shared_lock(&lease_path, false, false)?;
    manifest(generation)?;
    let now = std::time::SystemTime::now();
    if let Ok(file) = std::fs::File::options().write(true).open(&lease_path) {
        let _ = file.set_modified(now);
    }
    Some(descriptor)
}

/// The namespace lease `prepare_server_python_cache` holds for the worker.
pub fn lease_bytecode_namespace(cache_root: &str, namespace: &str) -> Option<OwnedFd> {
    if cache_root.is_empty() || namespace.is_empty() || namespace.contains('/') {
        return None;
    }
    open_shared_lock(
        &Path::new(cache_root).join(namespace).join(ACTIVE_LOCK_NAME),
        false,
        true,
    )
}

/// `runtime_replica.authoritative_interpreter`: a replica interpreter maps to
/// its source interpreter; every other interpreter maps to itself.
pub fn authoritative_interpreter(python: &str) -> String {
    if python.is_empty() {
        return python.to_owned();
    }
    let path = Path::new(python);
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        match std::env::current_dir() {
            Ok(cwd) => cwd.join(path),
            Err(_) => return python.to_owned(),
        }
    };
    let Some(directory) = absolute
        .parent()
        .and_then(|parent| std::fs::canonicalize(parent).ok())
    else {
        return python.to_owned();
    };
    let candidates: Vec<PathBuf> = [
        Some(directory.clone()),
        directory.parent().map(Path::to_path_buf),
    ]
    .into_iter()
    .flatten()
    .collect();
    for generation in candidates {
        let Some(manifest) = manifest(&generation) else {
            continue;
        };
        if let Some(source) = manifest.get("sourcePython").and_then(Value::as_str) {
            let source_path = Path::new(source);
            let raw = CString::new(source.as_bytes()).unwrap_or_default();
            // SAFETY: valid NUL-terminated path.
            if source_path.is_absolute() && unsafe { libc::access(raw.as_ptr(), libc::X_OK) } == 0 {
                return source.to_owned();
            }
        }
        break;
    }
    python.to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::AsRawFd;

    fn generation(root: &Path, schema: i64) -> PathBuf {
        let generation = root.join("g-1");
        std::fs::create_dir_all(generation.join("bin")).unwrap();
        std::fs::write(
            generation.join(MANIFEST_NAME),
            format!("{{\"schema\": {schema}, \"sourcePython\": \"/bin/sh\"}}"),
        )
        .unwrap();
        std::fs::write(generation.join(LEASE_NAME), "").unwrap();
        generation
    }

    fn temp(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("tofu-runtime-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn a_published_generation_is_leased_shared_and_blocks_collection() {
        let root = temp("lease");
        let generation = generation(&root, 1);
        let lease = lease_generation(&generation).expect("lease");
        let second = lease_generation(&generation).expect("leases are shared");
        let raw = CString::new(generation.join(LEASE_NAME).as_os_str().as_bytes()).unwrap();
        // SAFETY: test-only probe of an exclusive claim, closed below.
        let probe = unsafe { libc::open(raw.as_ptr(), libc::O_RDWR) };
        // SAFETY: probe is open.
        assert_ne!(
            unsafe { libc::flock(probe, libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        // SAFETY: probe is open.
        unsafe { libc::close(probe) };
        assert!(lease.as_raw_fd() >= 0 && second.as_raw_fd() >= 0);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_generation_without_its_manifest_is_never_leased() {
        let root = temp("manifest");
        let generation = generation(&root, 2);
        assert!(lease_generation(&generation).is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_replica_interpreter_maps_to_its_source() {
        let root = temp("source");
        let generation = generation(&root, 1);
        let python = generation.join("bin/python");
        assert_eq!(
            authoritative_interpreter(&python.to_string_lossy()),
            "/bin/sh"
        );
        assert_eq!(
            authoritative_interpreter("/usr/bin/python3"),
            "/usr/bin/python3"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_shared_cache_lock_must_be_a_private_owned_file() {
        let root = temp("cache");
        let namespace = root.join("ns-a");
        std::fs::create_dir_all(&namespace).unwrap();
        let lock = namespace.join(ACTIVE_LOCK_NAME);
        std::fs::write(&lock, "").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&lock, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(lease_bytecode_namespace(&root.to_string_lossy(), "ns-a").is_none());
        std::fs::set_permissions(&lock, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(lease_bytecode_namespace(&root.to_string_lossy(), "ns-a").is_some());
        assert!(lease_bytecode_namespace(&root.to_string_lossy(), "../x").is_none());
        let _ = std::fs::remove_dir_all(&root);
    }
}
