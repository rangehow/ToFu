//! `server_manager.SupervisorOwnership`: when the checkout or declared owner is gone.
//!
//! The checkout owns the manager while its source file (`supervisor.py` in
//! the source root, Python's `__file__`) exists. `git merge`/`checkout`
//! replace a changed file by unlinking and recreating it, so one missing
//! observation is not loss: the file must stay missing for
//! `SOURCE_MISSING_GRACE_SECONDS`, or `CHECKOUT_MISSING_GRACE_SECONDS` once
//! the checkout directory is gone or names a different directory than at
//! start (moved away and recreated). Only ENOENT/ENOTDIR count as missing; an
//! unreadable path neither confirms nor clears loss. The declared owner is
//! gone once it exited (an unreaped zombie included) or its PID names a
//! process with other start ticks.

use crate::mlog;
use crate::procfs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::time::Instant;

/// `server_manager.SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS`.
pub const SOURCE_MISSING_GRACE_SECONDS: f64 = 10.0;
/// `server_manager.SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS`.
pub const CHECKOUT_MISSING_GRACE_SECONDS: f64 = 0.5;

enum PathState {
    Present((u64, u64)),
    Missing,
    Unknown,
}

fn path_state(path: &Path) -> PathState {
    match std::fs::metadata(path) {
        Ok(info) => PathState::Present((info.dev(), info.ino())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => PathState::Missing,
        Err(error) if error.raw_os_error() == Some(libc::ENOTDIR) => PathState::Missing,
        Err(_) => PathState::Unknown,
    }
}

pub struct SupervisorOwnership {
    source_file: PathBuf,
    checkout_dir: PathBuf,
    owner_pid: Option<i64>,
    owner_start_ticks: Option<i64>,
    checkout_identity: Option<(u64, u64)>,
    missing_since: Option<Instant>,
}

impl SupervisorOwnership {
    pub fn new(source_file: &Path, owner_pid: Option<i64>) -> Self {
        let source_file =
            std::fs::canonicalize(source_file).unwrap_or_else(|_| source_file.to_path_buf());
        let checkout_dir = source_file
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default();
        let checkout_identity = match path_state(&checkout_dir) {
            PathState::Present(identity) => Some(identity),
            _ => None,
        };
        SupervisorOwnership {
            owner_start_ticks: owner_pid.and_then(procfs::start_ticks),
            source_file,
            checkout_dir,
            owner_pid,
            checkout_identity,
            missing_since: None,
        }
    }

    /// `''` while owned; otherwise why ownership is lost for good.
    pub fn lost(&mut self) -> String {
        let owner = self.owner_lost();
        if !owner.is_empty() {
            return owner;
        }
        let source = self.source_file.display().to_string();
        match path_state(&self.source_file) {
            PathState::Unknown => return String::new(),
            PathState::Present(_) => {
                if let Some(since) = self.missing_since.take() {
                    mlog!(
                        Info,
                        "supervisor ownership sentinel reappeared after {:.2}s: {source}",
                        since.elapsed().as_secs_f64()
                    );
                }
                return String::new();
            }
            PathState::Missing => {}
        }
        let place = match path_state(&self.checkout_dir) {
            PathState::Missing => "checkout directory is gone",
            PathState::Present(identity)
                if self.checkout_identity.is_some() && Some(identity) != self.checkout_identity =>
            {
                "checkout directory was replaced"
            }
            _ => "checkout directory still exists",
        };
        let grace = if place == "checkout directory still exists" {
            SOURCE_MISSING_GRACE_SECONDS
        } else {
            CHECKOUT_MISSING_GRACE_SECONDS
        };
        let Some(since) = self.missing_since else {
            self.missing_since = Some(Instant::now());
            mlog!(
                Warning,
                "supervisor ownership sentinel missing: {source} ({place}); ownership is lost only if it stays missing for {grace:.1}s"
            );
            return String::new();
        };
        let missing_for = since.elapsed().as_secs_f64();
        if missing_for < grace {
            return String::new();
        }
        format!("supervisor ownership sentinel disappeared: {source} missing for {missing_for:.1}s ({place})")
    }

    fn owner_lost(&self) -> String {
        let Some(pid) = self.owner_pid else {
            return String::new();
        };
        if !procfs::pid_is_alive(pid) {
            return format!("supervisor owner process disappeared: pid={pid}");
        }
        let ticks = procfs::start_ticks(pid);
        if self.owner_start_ticks.is_some() && ticks.is_some() && ticks != self.owner_start_ticks {
            return format!(
                "supervisor owner process disappeared: pid={pid} now names a different process"
            );
        }
        String::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn checkout(name: &str) -> PathBuf {
        let root =
            std::env::temp_dir().join(format!("tofu-ownership-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("supervisor.py"), "").unwrap();
        root
    }

    #[test]
    fn a_transiently_replaced_source_is_not_loss() {
        let root = checkout("transient");
        let mut ownership = SupervisorOwnership::new(&root.join("supervisor.py"), None);
        std::fs::remove_file(root.join("supervisor.py")).unwrap();
        assert_eq!(ownership.lost(), "");
        std::fs::write(root.join("supervisor.py"), "next").unwrap();
        assert_eq!(ownership.lost(), "");
        assert!(ownership.missing_since.is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_moved_checkout_is_lost_after_the_short_grace_even_when_recreated() {
        let root = checkout("moved");
        let mut ownership = SupervisorOwnership::new(&root.join("supervisor.py"), None);
        let moved = root.with_extension("moved");
        let _ = std::fs::remove_dir_all(&moved);
        std::fs::rename(&root, &moved).unwrap();
        std::fs::create_dir_all(&root).unwrap();
        assert_eq!(ownership.lost(), "");
        std::thread::sleep(std::time::Duration::from_secs_f64(
            CHECKOUT_MISSING_GRACE_SECONDS + 0.1,
        ));
        assert!(ownership.lost().contains("checkout directory was replaced"));
        let _ = std::fs::remove_dir_all(&root);
        let _ = std::fs::remove_dir_all(&moved);
    }

    #[test]
    fn an_exited_owner_is_gone() {
        let mut child = std::process::Command::new("true").spawn().unwrap();
        let pid = child.id() as i64;
        let root = checkout("owner");
        let mut ownership = SupervisorOwnership::new(&root.join("supervisor.py"), Some(pid));
        child.wait().unwrap();
        assert!(ownership
            .lost()
            .starts_with("supervisor owner process disappeared: pid="));
        let _ = std::fs::remove_dir_all(&root);
    }
}
