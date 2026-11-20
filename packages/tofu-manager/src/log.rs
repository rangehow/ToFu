//! `logs/server-manager.log` lines, byte-compatible with the Python manager.
//!
//! `supervisor.py` configures `%(asctime)s %(levelname)s [%(name)s] %(message)s`
//! on stderr and `supervisor.sh` redirects stderr into
//! `logs/server-manager.log`; restart and lifecycle outcomes are recorded only
//! there (AGENTS.md). This writer emits the same layout so every existing
//! `rg` recipe and outcome parser keeps working.

use std::io::Write;
use std::sync::Mutex;

static SINK: Mutex<()> = Mutex::new(());

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Info,
    Warning,
    Error,
}

impl Level {
    pub fn name(self) -> &'static str {
        match self {
            Level::Info => "INFO",
            Level::Warning => "WARNING",
            Level::Error => "ERROR",
        }
    }
}

/// Python logging's default `asctime`: local `YYYY-MM-DD HH:MM:SS,mmm`.
pub fn asctime(epoch: f64) -> String {
    let seconds = epoch.floor() as libc::time_t;
    let millis = ((epoch - epoch.floor()) * 1000.0).floor() as u32;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe {
        libc::localtime_r(&seconds, &mut tm);
    }
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02},{:03}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        millis.min(999)
    )
}

pub fn format_line(epoch: f64, level: Level, logger: &str, message: &str) -> String {
    format!(
        "{} {} [{}] {}\n",
        asctime(epoch),
        level.name(),
        logger,
        message
    )
}

pub fn write(level: Level, logger: &str, message: &str) {
    let line = format_line(crate::timing::now(), level, logger, message);
    let _guard = SINK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let mut stderr = std::io::stderr().lock();
    let _ = stderr.write_all(line.as_bytes());
    let _ = stderr.flush();
}

/// Messages of the lifecycle state machine (`server_manager` in Python).
#[macro_export]
macro_rules! mlog {
    ($level:ident, $($arg:tt)*) => {
        $crate::log::write($crate::log::Level::$level, "server_manager", &format!($($arg)*))
    };
}

/// Messages of the HTTP door (`supervisor` in Python).
#[macro_export]
macro_rules! slog {
    ($level:ident, $($arg:tt)*) => {
        $crate::log::write($crate::log::Level::$level, "supervisor", &format!($($arg)*))
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_matches_python_logging_layout() {
        let line = format_line(
            1_700_000_000.25,
            Level::Error,
            "server_manager",
            "worker restart refused",
        );
        let (stamp, rest) = line.split_at(23);
        assert_eq!(rest, " ERROR [server_manager] worker restart refused\n");
        assert_eq!(&stamp[4..5], "-");
        assert_eq!(&stamp[10..11], " ");
        assert_eq!(&stamp[19..20], ",");
        assert_eq!(&stamp[20..23], "250");
    }
}
