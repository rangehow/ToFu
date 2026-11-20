//! This host's identity key, binding the node ID to one machine.
//!
//! Port of `lib/desktop_agent/host_identity.py`, digest-identical so a Python
//! and a Rust node on one host (or one shared config file) agree on every slot.
//!
//! Problem: the node ID lives in the node config (`~/.tofu/desktop_agent.json`
//! or `TOFU_DESKTOP_CONFIG`); hosts sharing one home directory (containers on a
//! network home) read one file and would present one node ID.
//!
//! Decision: the config keeps one ID per host, keyed by a digest of the
//! platform machine identifier; on Linux also the hostname (containers of one
//! image share `/etc/machine-id`); plus the container ID when the process runs
//! in one (twins on the host network namespace share even the hostname). Only
//! digests are stored. See docs/modules/remote_execution.md "One node ID per host".

use crate::util::sha256_hex;
use std::sync::OnceLock;

/// The raw facts a key is derived from. `simulate` replaces them for the
/// fault-injection suite, which runs several "hosts" on one machine
/// (tests/chaos/node_main.py does the same for the Python node).
#[derive(Clone, Debug)]
pub struct HostFacts {
    pub machine: String,
    pub hostname: String,
    pub container: String,
    pub hostname_scoped: bool,
}

static SIMULATED: OnceLock<HostFacts> = OnceLock::new();

pub fn simulate(machine: &str, hostname: &str, container: &str) {
    let _ = SIMULATED.set(HostFacts {
        machine: machine.to_string(),
        hostname: hostname.to_string(),
        container: container.to_string(),
        hostname_scoped: true,
    });
}

pub fn facts() -> HostFacts {
    if let Some(sim) = SIMULATED.get() {
        return sim.clone();
    }
    HostFacts {
        machine: machine_id(),
        hostname: real_hostname(),
        container: container_id(),
        hostname_scoped: cfg!(target_os = "linux"),
    }
}

/// `socket.gethostname()` of this host (simulated under the chaos suite).
pub fn hostname() -> String {
    match SIMULATED.get() {
        Some(sim) => sim.hostname.clone(),
        None => real_hostname(),
    }
}

#[cfg(unix)]
fn real_hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: the buffer is valid for its length; gethostname NUL-terminates
    // within it or truncates, and we stop at the first NUL.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) };
    if rc != 0 {
        return String::new();
    }
    let end = buf.iter().position(|b| *b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

#[cfg(windows)]
fn real_hostname() -> String {
    std::env::var("COMPUTERNAME").unwrap_or_default()
}

#[cfg(target_os = "linux")]
fn machine_id() -> String {
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"] {
        if let Ok(text) = std::fs::read_to_string(path) {
            let value = text.trim();
            if !value.is_empty() {
                return value.to_string();
            }
        }
    }
    String::new()
}

#[cfg(target_os = "macos")]
fn machine_id() -> String {
    let out = std::process::Command::new("ioreg")
        .args(["-rd1", "-c", "IOPlatformExpertDevice"])
        .output();
    let Ok(out) = out else { return String::new() };
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        if line.contains("IOPlatformUUID") {
            let value = line.rsplit_once('=').map(|(_, v)| v).unwrap_or(line);
            return value.trim().trim_matches('"').to_string();
        }
    }
    String::new()
}

#[cfg(windows)]
fn machine_id() -> String {
    // HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid, the value winreg reads.
    let out = std::process::Command::new("reg")
        .args([
            "query",
            r"HKLM\SOFTWARE\Microsoft\Cryptography",
            "/v",
            "MachineGuid",
        ])
        .output();
    let Ok(out) = out else { return String::new() };
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() >= 3 && fields[0] == "MachineGuid" {
            return fields[2].trim().to_string();
        }
    }
    String::new()
}

#[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
fn machine_id() -> String {
    String::new()
}

/// The last maximal run of exactly 64 lowercase hex digits (the Python regex
/// `(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])`).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn last_container_hash(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut found = None;
    let mut start = None;
    for (i, b) in bytes.iter().chain(std::iter::once(&b' ')).enumerate() {
        let hex = b.is_ascii_digit() || (b'a'..=b'f').contains(b);
        match (hex, start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                if i - s == 64 {
                    found = Some(text[s..i].to_string());
                }
                start = None;
            }
            _ => {}
        }
    }
    found
}

/// The root overlay's private upper directory (unique per container) from
/// `/proc/self/mountinfo`, for a namespaced cgroup v2 that reads `0::/`.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn overlay_upperdir(mountinfo: &str) -> Option<String> {
    for line in mountinfo.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() > 4 && fields[4] == "/" && line.contains(" - overlay ") {
            let options = line.rsplit(' ').next().unwrap_or("");
            for option in options.split(',') {
                if let Some(dir) = option.strip_prefix("upperdir=") {
                    return Some(dir.to_string());
                }
            }
        }
    }
    None
}

#[cfg(target_os = "linux")]
fn container_id() -> String {
    if let Ok(cgroup) = std::fs::read_to_string("/proc/self/cgroup") {
        if let Some(id) = last_container_hash(&cgroup) {
            return id;
        }
    }
    std::fs::read_to_string("/proc/self/mountinfo")
        .ok()
        .and_then(|m| overlay_upperdir(&m))
        .unwrap_or_default()
}

#[cfg(not(target_os = "linux"))]
fn container_id() -> String {
    String::new()
}

fn digest(parts: &[&str]) -> String {
    sha256_hex(parts.join("\0").as_bytes())[..32].to_string()
}

fn phase0_of(facts: &HostFacts) -> String {
    let mut parts = vec![facts.machine.as_str()];
    if facts.hostname_scoped || facts.machine.is_empty() {
        parts.push(facts.hostname.as_str());
    }
    digest(&parts)
}

/// The key of builds that did not yet include the container ID (migration).
pub fn phase0_host_key() -> String {
    phase0_of(&facts())
}

/// Digests of the components of this host's identity (never the raw values).
pub fn host_facts() -> serde_json::Map<String, serde_json::Value> {
    let f = facts();
    let hostname = if f.hostname_scoped || f.machine.is_empty() {
        f.hostname.clone()
    } else {
        String::new()
    };
    let one = |label: &str, value: &str| {
        if value.is_empty() {
            String::new()
        } else {
            digest(&[label, value])
        }
    };
    let mut out = serde_json::Map::new();
    out.insert("machine".into(), one("machine", &f.machine).into());
    out.insert("hostname".into(), one("hostname", &hostname).into());
    out.insert("container".into(), one("container", &f.container).into());
    out
}

/// A stable digest naming this host; never empty. Outside a container it
/// equals `phase0_host_key`, so existing hosts keep their key.
pub fn host_key() -> String {
    let f = facts();
    let phase0 = phase0_of(&f);
    if f.container.is_empty() {
        return phase0;
    }
    digest(&[&phase0, "container", &f.container])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key_of(f: &HostFacts) -> String {
        let phase0 = phase0_of(f);
        if f.container.is_empty() {
            phase0
        } else {
            digest(&[&phase0, "container", &f.container])
        }
    }

    fn host(machine: &str, hostname: &str, container: &str) -> HostFacts {
        HostFacts {
            machine: machine.into(),
            hostname: hostname.into(),
            container: container.into(),
            hostname_scoped: true,
        }
    }

    #[test]
    fn digests_match_the_python_node() {
        // Values computed with lib/desktop_agent/host_identity.py helpers.
        assert_eq!(digest(&["m", "h"]), &sha256_hex(b"m\0h")[..32]);
        assert_eq!(key_of(&host("mid", "h", "")), digest(&["mid", "h"]));
    }

    #[test]
    fn containers_sharing_home_machine_and_hostname_get_distinct_keys() {
        let a = host("image-machine-id", "shared-host", &"a".repeat(64));
        let b = host("image-machine-id", "shared-host", &"b".repeat(64));
        assert_ne!(key_of(&a), key_of(&b));
        // Same image, different hostnames, no container: still distinct.
        assert_ne!(
            key_of(&host("image", "h1", "")),
            key_of(&host("image", "h2", ""))
        );
    }

    #[test]
    fn container_id_from_cgroup_and_overlay() {
        let id = "0123456789abcdef".repeat(4);
        let cgroup = format!("12:pids:/docker/{id}\n0::/system.slice/docker-{id}.scope\n");
        assert_eq!(last_container_hash(&cgroup).as_deref(), Some(id.as_str()));
        let too_long = format!("0::/{}{}", id, "a");
        assert_eq!(last_container_hash(&too_long), None);
        assert_eq!(last_container_hash("0::/"), None);
        let mountinfo = "1234 1 0:52 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/var/lib/docker/overlay2/xyz/diff,workdir=/w\n";
        assert_eq!(
            overlay_upperdir(mountinfo).as_deref(),
            Some("/var/lib/docker/overlay2/xyz/diff")
        );
    }
}
