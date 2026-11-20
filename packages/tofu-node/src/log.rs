//! Node log lines on stderr (the headless node's log is its stderr, as for
//! `python -m lib.desktop_agent`). Credentials never reach a log line.

use std::io::Write;

fn line(level: &str, message: &str) {
    let now = crate::util::unix_now();
    let mut err = std::io::stderr().lock();
    let _ = writeln!(err, "{now:.3} {level} [tofu-node] {message}");
}

pub fn info(message: &str) {
    line("INFO", message);
}

pub fn warn(message: &str) {
    line("WARNING", message);
}

pub fn error(message: &str) {
    line("ERROR", message);
}
