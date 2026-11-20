//! `desktop_system_info`: the port of `lib/desktop_agent/_gui.py::cmd_system_info`.
//!
//! The Python node reads these facts through psutil; this node reads the
//! platform directly (`/proc` and `statvfs` on Linux, `sysctl`/`vm_stat`/`ps`
//! on macOS, `tasklist`/`taskkill` on Windows). A fact a platform cannot give
//! portably is `null` under the same key. `kind: "kill"` is the exec tier and
//! is permission-gated before dispatch.

use serde_json::{json, Value};
use std::process::Command;

// psutil's `cpu_percent(interval=0.5)` sample.
#[cfg(target_os = "linux")]
const CPU_SAMPLE: std::time::Duration = std::time::Duration::from_millis(500);
const DEFAULT_TOP: i64 = 15;

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn gigabytes(bytes: u64) -> f64 {
    round1(bytes as f64 / 1e9)
}

pub fn system_info(params: &Value) -> Value {
    let kind = params
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("overview");
    match kind {
        "overview" => overview(),
        "processes" => {
            let top = match params.get("top") {
                None | Some(Value::Null) => DEFAULT_TOP,
                Some(v) => v
                    .as_i64()
                    .or_else(|| v.as_f64().map(|f| f as i64))
                    .unwrap_or(DEFAULT_TOP),
            };
            let mut procs = processes(true);
            procs.sort_by(|a, b| {
                b.cpu
                    .partial_cmp(&a.cpu)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            procs.truncate(top.max(0) as usize);
            json!({"processes": procs.iter().map(Proc::to_json).collect::<Vec<_>>()})
        }
        "kill" => match params
            .get("pid")
            .and_then(Value::as_i64)
            .filter(|pid| *pid != 0)
        {
            Some(pid) => kill(pid),
            None => json!({"error": format!("Unknown info type: {kind}")}),
        },
        other => json!({"error": format!("Unknown info type: {other}")}),
    }
}

fn overview() -> Value {
    let cpu_count = std::thread::available_parallelism().map(|n| n.get()).ok();
    let memory = memory();
    let disk = disk();
    json!({
        "platform": platform(),
        "agent": format!("tofu-node {}", env!("CARGO_PKG_VERSION")),
        "cpu_count": cpu_count,
        "cpu_percent": cpu_percent(),
        "memory": {
            "total_gb": memory.map(|m| gigabytes(m.0)),
            "used_gb": memory.map(|m| gigabytes(m.1)),
            "percent": memory.map(|m| m.2),
        },
        "disk": {
            "total_gb": disk.map(|d| gigabytes(d.0)),
            "used_gb": disk.map(|d| gigabytes(d.1)),
            "percent": disk.map(|d| d.2),
        },
        "user": std::env::var("USER").or_else(|_| std::env::var("USERNAME")).unwrap_or_else(|_| "unknown".into()),
        "home": crate::config::home_dir().display().to_string(),
        "cwd": std::env::current_dir().map(|d| d.display().to_string()).unwrap_or_default(),
    })
}

#[cfg_attr(target_os = "linux", allow(dead_code))]
fn command_stdout(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program).args(args).output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).into_owned())
}

#[cfg(unix)]
fn platform() -> String {
    // SAFETY: uname only writes the caller-owned struct.
    let mut name: libc::utsname = unsafe { std::mem::zeroed() };
    if unsafe { libc::uname(&mut name) } != 0 {
        return std::env::consts::OS.into();
    }
    let field = |raw: &[libc::c_char]| {
        let bytes: Vec<u8> = raw
            .iter()
            .take_while(|&&c| c != 0)
            .map(|&c| c as u8)
            .collect();
        String::from_utf8_lossy(&bytes).into_owned()
    };
    format!(
        "{}-{}-{}",
        field(&name.sysname),
        field(&name.release),
        field(&name.machine)
    )
}

#[cfg(not(unix))]
fn platform() -> String {
    format!("Windows-{}", std::env::consts::ARCH)
}

/// `(total, used, percent)` of the root filesystem, as psutil computes them.
#[cfg(unix)]
fn disk() -> Option<(u64, u64, f64)> {
    let root = std::ffi::CString::new("/").ok()?;
    // SAFETY: statvfs only writes the caller-owned struct.
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(root.as_ptr(), &mut stat) } != 0 {
        return None;
    }
    let frsize = stat.f_frsize as u64;
    let total = stat.f_blocks as u64 * frsize;
    let used = (stat.f_blocks as u64 - stat.f_bfree as u64) * frsize;
    let avail = stat.f_bavail as u64 * frsize;
    let percent = if used + avail > 0 {
        round1(used as f64 / (used + avail) as f64 * 100.0)
    } else {
        0.0
    };
    Some((total, used, percent))
}

#[cfg(not(unix))]
fn disk() -> Option<(u64, u64, f64)> {
    None
}

/// `(total, used, percent)` of physical memory.
#[cfg(target_os = "linux")]
fn memory() -> Option<(u64, u64, f64)> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    let field = |key: &str| -> Option<u64> {
        text.lines()
            .find_map(|line| {
                line.strip_prefix(key)?
                    .strip_prefix(':')?
                    .split_whitespace()
                    .next()?
                    .parse::<u64>()
                    .ok()
            })
            .map(|kb| kb * 1024)
    };
    let total = field("MemTotal")?;
    let free = field("MemFree").unwrap_or(0);
    let buffers = field("Buffers").unwrap_or(0);
    let cached = field("Cached").unwrap_or(0) + field("SReclaimable").unwrap_or(0);
    let available = field("MemAvailable").unwrap_or(free + buffers + cached);
    let mut used = total.saturating_sub(free + buffers + cached);
    if used == 0 {
        used = total.saturating_sub(free);
    }
    Some((
        total,
        used,
        round1(total.saturating_sub(available) as f64 / total as f64 * 100.0),
    ))
}

#[cfg(target_os = "macos")]
fn memory() -> Option<(u64, u64, f64)> {
    let total: u64 = command_stdout("sysctl", &["-n", "hw.memsize"])?
        .trim()
        .parse()
        .ok()?;
    let stats = command_stdout("vm_stat", &[])?;
    let page: u64 = stats
        .split("page size of ")
        .nth(1)?
        .split_whitespace()
        .next()?
        .parse()
        .ok()?;
    let pages = |key: &str| -> u64 {
        stats
            .lines()
            .find_map(|line| {
                line.strip_prefix(key)?
                    .split(':')
                    .nth(1)?
                    .trim()
                    .trim_end_matches('.')
                    .parse::<u64>()
                    .ok()
            })
            .unwrap_or(0)
            * page
    };
    let used = pages("Pages active") + pages("Pages wired down");
    let available = pages("Pages free") + pages("Pages inactive");
    Some((
        total,
        used,
        round1(total.saturating_sub(available) as f64 / total as f64 * 100.0),
    ))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn memory() -> Option<(u64, u64, f64)> {
    None
}

#[cfg(target_os = "linux")]
fn cpu_percent() -> Option<f64> {
    let sample = || -> Option<(u64, u64)> {
        let text = std::fs::read_to_string("/proc/stat").ok()?;
        let values: Vec<u64> = text
            .lines()
            .next()?
            .split_whitespace()
            .skip(1)
            .filter_map(|v| v.parse().ok())
            .collect();
        let total: u64 = values.iter().take(8).sum();
        let idle = values.get(3).copied().unwrap_or(0) + values.get(4).copied().unwrap_or(0);
        Some((total, idle))
    };
    let (total0, idle0) = sample()?;
    std::thread::sleep(CPU_SAMPLE);
    let (total1, idle1) = sample()?;
    let total = total1.saturating_sub(total0);
    if total == 0 {
        return Some(0.0);
    }
    let busy = total.saturating_sub(idle1.saturating_sub(idle0));
    Some(round1(busy as f64 / total as f64 * 100.0))
}

#[cfg(not(target_os = "linux"))]
fn cpu_percent() -> Option<f64> {
    None
}

struct Proc {
    pid: i64,
    name: String,
    cpu: f64,
    memory_bytes: u64,
    status: String,
}

impl Proc {
    fn to_json(&self) -> Value {
        json!({
            "pid": self.pid,
            "name": self.name,
            "cpu": self.cpu,
            "memory_mb": round1(self.memory_bytes as f64 / 1e6),
            "status": self.status,
        })
    }
}

#[cfg(target_os = "linux")]
fn processes(sample_cpu: bool) -> Vec<Proc> {
    struct Raw {
        pid: i64,
        name: String,
        state: char,
        ticks: u64,
        rss_pages: u64,
    }
    fn read_all() -> Vec<Raw> {
        let Ok(dir) = std::fs::read_dir("/proc") else {
            return Vec::new();
        };
        dir.filter_map(Result::ok)
            .filter_map(|entry| {
                let pid: i64 = entry.file_name().to_str()?.parse().ok()?;
                let stat = std::fs::read_to_string(entry.path().join("stat")).ok()?;
                let (open, close) = (stat.find('(')?, stat.rfind(')')?);
                let name = stat[open + 1..close].to_string();
                let rest: Vec<&str> = stat[close + 1..].split_whitespace().collect();
                let state = rest.first()?.chars().next()?;
                let ticks =
                    rest.get(11)?.parse::<u64>().ok()? + rest.get(12)?.parse::<u64>().ok()?;
                let statm = std::fs::read_to_string(entry.path().join("statm")).unwrap_or_default();
                let rss_pages = statm
                    .split_whitespace()
                    .nth(1)
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(0);
                Some(Raw {
                    pid,
                    name,
                    state,
                    ticks,
                    rss_pages,
                })
            })
            .collect()
    }
    // SAFETY: sysconf has no preconditions.
    let page = unsafe { libc::sysconf(libc::_SC_PAGESIZE) }.max(1) as u64;
    let hertz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
    let first = read_all();
    let (second, elapsed) = if sample_cpu {
        let start = std::time::Instant::now();
        std::thread::sleep(CPU_SAMPLE);
        (read_all(), start.elapsed().as_secs_f64())
    } else {
        (Vec::new(), 0.0)
    };
    let before: std::collections::HashMap<i64, u64> =
        first.iter().map(|r| (r.pid, r.ticks)).collect();
    let rows = if sample_cpu { second } else { first };
    rows.into_iter()
        .map(|raw| {
            let cpu = match before.get(&raw.pid) {
                Some(ticks) if elapsed > 0.0 => {
                    round1(raw.ticks.saturating_sub(*ticks) as f64 / hertz / elapsed * 100.0)
                }
                _ => 0.0,
            };
            let status = match raw.state {
                'R' => "running",
                'S' => "sleeping",
                'D' => "disk-sleep",
                'T' => "stopped",
                't' => "tracing-stop",
                'Z' => "zombie",
                'X' | 'x' => "dead",
                'K' => "wake-kill",
                'W' => "waking",
                'I' => "idle",
                'P' => "parked",
                _ => "unknown",
            };
            Proc {
                pid: raw.pid,
                name: raw.name,
                cpu,
                memory_bytes: raw.rss_pages * page,
                status: status.into(),
            }
        })
        .collect()
}

#[cfg(target_os = "macos")]
fn processes(_sample_cpu: bool) -> Vec<Proc> {
    let Some(text) = command_stdout("ps", &["-axo", "pid=,pcpu=,rss=,state=,comm="]) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|line| {
            let mut parts = line.split_whitespace();
            let pid = parts.next()?.parse().ok()?;
            let cpu = parts.next()?.parse().ok()?;
            let rss_kb: u64 = parts.next()?.parse().ok()?;
            let state = parts.next()?.chars().next()?;
            let command = parts.collect::<Vec<_>>().join(" ");
            let name = command.rsplit('/').next().unwrap_or(&command).to_string();
            let status = match state {
                'R' => "running",
                'S' => "sleeping",
                'I' => "idle",
                'T' => "stopped",
                'U' => "disk-sleep",
                'Z' => "zombie",
                _ => "unknown",
            };
            Some(Proc {
                pid,
                name,
                cpu,
                memory_bytes: rss_kb * 1024,
                status: status.into(),
            })
        })
        .collect()
}

#[cfg(windows)]
fn processes(_sample_cpu: bool) -> Vec<Proc> {
    fn csv_fields(line: &str) -> Vec<String> {
        line.split("\",\"")
            .map(|f| f.trim_matches('"').to_string())
            .collect()
    }
    let Some(text) = command_stdout("tasklist", &["/fo", "csv", "/nh"]) else {
        return Vec::new();
    };
    text.lines()
        .filter_map(|line| {
            let fields = csv_fields(line.trim());
            let pid = fields.get(1)?.parse().ok()?;
            let kb: u64 = fields
                .get(4)?
                .chars()
                .filter(char::is_ascii_digit)
                .collect::<String>()
                .parse()
                .unwrap_or(0);
            Some(Proc {
                pid,
                name: fields[0].clone(),
                cpu: 0.0,
                memory_bytes: kb * 1024,
                status: "running".into(),
            })
        })
        .collect()
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn processes(_sample_cpu: bool) -> Vec<Proc> {
    Vec::new()
}

#[cfg(unix)]
fn terminate(pid: i64) -> std::io::Result<()> {
    let pid = libc::pid_t::try_from(pid)
        .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
    // SAFETY: kill(2) takes plain integers.
    if unsafe { libc::kill(pid, libc::SIGTERM) } == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(windows)]
fn terminate(pid: i64) -> std::io::Result<()> {
    let status = Command::new("taskkill")
        .args(["/F", "/PID", &pid.to_string()])
        .output()?
        .status;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "taskkill exited with {status}"
        )))
    }
}

fn kill(pid: i64) -> Value {
    if pid < 0 {
        return json!({"error": format!("pid must be a positive integer, got {pid}")});
    }
    let Some(name) = processes(false)
        .into_iter()
        .find(|p| p.pid == pid)
        .map(|p| p.name)
    else {
        return json!({"error": format!("process no longer exists (pid={pid})")});
    };
    match terminate(pid) {
        Ok(()) => json!({"killed": pid, "name": name, "success": true}),
        Err(e) => {
            crate::log::warn(&format!("desktop_system_info kill failed pid={pid}: {e}"));
            json!({"error": e.to_string()})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overview_keeps_the_python_keys() {
        let info = system_info(&json!({}));
        for key in [
            "platform",
            "cpu_count",
            "cpu_percent",
            "memory",
            "disk",
            "user",
            "home",
            "cwd",
        ] {
            assert!(info.get(key).is_some(), "{key}: {info}");
        }
        for key in ["total_gb", "used_gb", "percent"] {
            assert!(info["memory"].get(key).is_some() && info["disk"].get(key).is_some());
        }
        assert!(info["cpu_count"].as_u64().unwrap() >= 1);
        #[cfg(target_os = "linux")]
        assert!(
            info["memory"]["total_gb"].as_f64().unwrap() > 0.0
                && info["disk"]["total_gb"].as_f64().unwrap() > 0.0
        );
    }

    #[test]
    fn processes_kill_and_unknown_types() {
        let listed = system_info(&json!({"type": "processes", "top": 3}));
        let procs = listed["processes"].as_array().unwrap();
        #[cfg(target_os = "linux")]
        assert!(!procs.is_empty() && procs.len() <= 3);
        for p in procs {
            for key in ["pid", "name", "cpu", "memory_mb", "status"] {
                assert!(p.get(key).is_some());
            }
        }
        assert_eq!(
            system_info(&json!({"type": "bogus"})),
            json!({"error": "Unknown info type: bogus"})
        );
        assert_eq!(
            system_info(&json!({"type": "kill"})),
            json!({"error": "Unknown info type: kill"})
        );
        #[cfg(unix)]
        {
            let mut child = Command::new("sleep").arg("30").spawn().unwrap();
            let pid = child.id() as i64;
            let killed = system_info(&json!({"type": "kill", "pid": pid}));
            assert_eq!(killed["killed"], pid, "{killed}");
            assert_eq!(killed["name"], "sleep");
            assert!(!child.wait().unwrap().success());
        }
    }
}
