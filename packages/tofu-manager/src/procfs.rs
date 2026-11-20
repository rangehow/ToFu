//! Linux process identity from `/proc`, as `server_manager` and
//! `runtime_guards` read it. Start ticks disambiguate PID reuse.

use std::fs;

/// Fields after `comm`; index 0 is field 3 (state), 1 the parent PID, 19 start ticks.
pub fn stat_fields(pid: i64) -> Option<Vec<String>> {
    if pid <= 0 {
        return None;
    }
    let raw = fs::read(format!("/proc/{pid}/stat")).ok()?;
    let close = raw.iter().rposition(|byte| *byte == b')')?;
    let rest = String::from_utf8_lossy(raw.get(close + 2..)?).into_owned();
    let fields: Vec<String> = rest.split_whitespace().map(str::to_owned).collect();
    (fields.len() > 19).then_some(fields)
}

pub fn start_ticks(pid: i64) -> Option<i64> {
    stat_fields(pid)?.get(19)?.parse().ok()
}

/// `runtime_guards.pid_is_alive`: a zombie whose last thread is gone is dead.
pub fn pid_is_alive(pid: i64) -> bool {
    if pid <= 0 || pid > i32::MAX as i64 {
        return false;
    }
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    if result != 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::EPERM) {
        return false;
    }
    match stat_fields(pid) {
        Some(fields) if fields[0] == "Z" || fields[0] == "X" => {
            !matches!(fields[17].parse::<i64>(), Ok(threads) if threads <= 1)
        }
        _ => true,
    }
}

pub fn cmdline_args(pid: i64) -> Option<Vec<Vec<u8>>> {
    let raw = fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    Some(raw.split(|byte| *byte == 0).map(<[u8]>::to_vec).collect())
}

/// `proc_cmdline`: NUL-joined arguments as one space-separated string.
pub fn cmdline(pid: i64) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    let raw = fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    let text: Vec<u8> = raw
        .iter()
        .map(|byte| if *byte == 0 { b' ' } else { *byte })
        .collect();
    Some(String::from_utf8_lossy(&text).trim().to_owned())
}

pub fn cwd(pid: i64) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    let link = fs::read_link(format!("/proc/{pid}/cwd")).ok()?;
    Some(
        fs::canonicalize(&link)
            .unwrap_or(link)
            .to_string_lossy()
            .into_owned(),
    )
}

pub fn env_value(pid: i64, name: &str) -> Option<String> {
    if pid <= 0 {
        return None;
    }
    let raw = fs::read(format!("/proc/{pid}/environ")).ok()?;
    let prefix = format!("{name}=");
    raw.split(|byte| *byte == 0)
        .find(|entry| entry.starts_with(prefix.as_bytes()))
        .map(|entry| String::from_utf8_lossy(&entry[prefix.len()..]).into_owned())
}

fn clock_ticks() -> f64 {
    let ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if ticks > 0 {
        ticks as f64
    } else {
        100.0
    }
}

/// Wall-clock start of `pid`, from boot time plus its start ticks.
pub fn start_epoch(pid: i64) -> Option<f64> {
    let ticks = start_ticks(pid)? as f64;
    let stat = fs::read_to_string("/proc/stat").ok()?;
    let boot: f64 = stat
        .lines()
        .find_map(|line| line.strip_prefix("btime "))?
        .trim()
        .parse()
        .ok()?;
    Some(boot + ticks / clock_ticks())
}

pub fn hostname() -> String {
    let mut buffer = [0u8; 256];
    let result =
        unsafe { libc::gethostname(buffer.as_mut_ptr() as *mut libc::c_char, buffer.len()) };
    if result != 0 {
        return String::new();
    }
    let end = buffer
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(buffer.len());
    String::from_utf8_lossy(&buffer[..end]).into_owned()
}

pub fn signal_name(signum: i32) -> String {
    let name = match signum {
        1 => "SIGHUP",
        2 => "SIGINT",
        3 => "SIGQUIT",
        4 => "SIGILL",
        5 => "SIGTRAP",
        6 => "SIGABRT",
        7 => "SIGBUS",
        8 => "SIGFPE",
        9 => "SIGKILL",
        10 => "SIGUSR1",
        11 => "SIGSEGV",
        12 => "SIGUSR2",
        13 => "SIGPIPE",
        14 => "SIGALRM",
        15 => "SIGTERM",
        _ => return format!("SIGNAL_{signum}"),
    };
    name.to_owned()
}

/// `proc_exit_observation`: an identity-verified zombie's wait status, unreaped.
pub fn exit_observation(
    pid: i64,
    expected_ticks: i64,
) -> serde_json::Map<String, serde_json::Value> {
    use serde_json::json;
    let mut out = serde_json::Map::new();
    if pid <= 0 || expected_ticks <= 0 {
        return out;
    }
    let Some(fields) = stat_fields(pid) else {
        return out;
    };
    if fields[0] != "Z" || fields[19].parse::<i64>().ok() != Some(expected_ticks) {
        return out;
    }
    let Some(status) = fields.get(49).and_then(|raw| raw.parse::<i32>().ok()) else {
        return out;
    };
    let signaled = libc::WIFSIGNALED(status);
    let signum = if signaled {
        Some(libc::WTERMSIG(status))
    } else {
        None
    };
    out.insert("pid".into(), json!(pid));
    out.insert("processStartTime".into(), json!(expected_ticks));
    out.insert("source".into(), json!("linux_proc_stat"));
    out.insert("rawStatus".into(), json!(status));
    out.insert("signal".into(), json!(signum));
    out.insert(
        "signalName".into(),
        json!(signum.map(signal_name).unwrap_or_default()),
    );
    out.insert(
        "exitCode".into(),
        json!(if libc::WIFEXITED(status) {
            Some(libc::WEXITSTATUS(status))
        } else {
            None
        }),
    );
    out.insert(
        "coreDumpFlag".into(),
        json!(signum.is_some() && libc::WCOREDUMP(status)),
    );
    out
}

/// `reap_exited_child`: wait only for this process's identity-verified zombie.
pub fn reap_exited_child(pid: i64, ticks: i64) -> Option<i32> {
    let fields = stat_fields(pid)?;
    if fields[0] != "Z"
        || fields[1] != std::process::id().to_string()
        || fields[19] != ticks.to_string()
    {
        return None;
    }
    let mut status = 0;
    let reaped = unsafe { libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG) };
    (reaped == pid as libc::pid_t).then_some(status)
}

/// Reap every exited child (`reap_children_of_previous_image`): a re-exec'd
/// manager inherits children it never spawned and no other owner waits for.
pub fn reap_zombie_children() {
    let me = std::process::id().to_string();
    let Ok(entries) = fs::read_dir("/proc") else {
        return;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<i64>() else {
            continue;
        };
        if let Some(fields) = stat_fields(pid) {
            if fields[0] == "Z" && fields[1] == me {
                let mut status = 0;
                unsafe {
                    libc::waitpid(pid as libc::pid_t, &mut status, libc::WNOHANG);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn own_process_identity_is_readable() {
        let pid = std::process::id() as i64;
        assert!(pid_is_alive(pid));
        assert!(start_ticks(pid).unwrap() > 0);
        assert!(cmdline(pid).is_some());
        assert!(start_epoch(pid).unwrap() > 1_000_000_000.0);
        assert!(!pid_is_alive(0));
    }

    #[test]
    fn a_zombie_child_is_dead_and_its_exit_is_observed_unreaped() {
        let mut child = std::process::Command::new("sh")
            .args(["-c", "exit 7"])
            .spawn()
            .unwrap();
        let pid = child.id() as i64;
        let ticks = start_ticks(pid).unwrap();
        let deadline = crate::timing::Deadline::after(10.0);
        while stat_fields(pid).map(|f| f[0] != "Z").unwrap_or(false) && !deadline.expired() {
            crate::timing::sleep(0.01);
        }
        assert!(!pid_is_alive(pid));
        let observed = exit_observation(pid, ticks);
        assert_eq!(observed["exitCode"], serde_json::json!(7));
        assert_eq!(
            reap_exited_child(pid, ticks).map(|status| libc::WEXITSTATUS(status)),
            Some(7)
        );
        let _ = child.try_wait();
    }
}
