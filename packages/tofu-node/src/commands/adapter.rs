//! Subscription adapter sidecar supervisor (`adapter_ensure` / `adapter_status` /
//! `adapter_stop`, and `maybe_resume` when an egress-enabled link starts).
//! Port of `lib/desktop_agent/_adapter.py`.
//!
//! Runs a pinned, SHA-256-verified CLIProxyAPI binary as a loopback-only
//! subprocess; the server reaches it only through the egress relay's
//! `loopback` target class, whose port is this policy's port. Files under
//! `<config dir>/adapter/`: `policy.json` (server-owned port, api key,
//! management secret, version pin, auto-update; persisted so the sidecar
//! resumes across node restarts), `config.yaml` (written here: 127.0.0.1
//! only, keyed, remote management off), `cliproxyapi[.exe]`, `version.txt`,
//! `auth/` (the adapter's accounts) and `adapter.log` (its console).
//!
//! Distribution: download on first ensure from the GitHub releases of
//! router-for-me/CLIProxyAPI (initial URL host-pinned to GitHub), verify the
//! asset against the release's checksums.txt, install only the binary member,
//! and check for updates weekly. A crash watch respawns the sidecar with
//! capped backoff while the policy is active. Every command here is gated on
//! the `egress` tier by the link.

use super::egress::{self, wire};
use crate::log;
use serde_json::{json, Map, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{Duration, Instant};

const GH_API: &str = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases";
const ALLOWED_DOWNLOAD_HOSTS: &[&str] = &[
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
];
const DEFAULT_PORT: u16 = 8317;
/// `_adapter.py` constants: health gate, its probe timeout and pause.
const HEALTH_TIMEOUT: Duration = Duration::from_secs(30);
const HEALTH_PROBE_TIMEOUT: Duration = Duration::from_secs(3);
const HEALTH_PROBE_PAUSE: Duration = Duration::from_millis(500);
const UPDATE_INTERVAL: Duration = Duration::from_secs(7 * 86400);
const UPDATE_JITTER_SECONDS: f64 = 86400.0;
const UPDATE_FIRST_DELAY_SECONDS: f64 = 60.0;
const RESTART_BACKOFF_START_SECONDS: u64 = 5;
const RESTART_BACKOFF_CAP_SECONDS: u64 = 300;
const STOP_GRACE: Duration = Duration::from_secs(8);
/// Ports tried from the desired one on multi-user machines (design O2).
const PORT_CANDIDATES: u32 = 8;
const MAX_ADAPTER_DOWNLOAD_BYTES: u64 = 256 * 1024 * 1024;
const MAX_ADAPTER_ARCHIVE_MEMBERS: usize = 10_000;
/// GitHub API calls and the archive download (`_gh_get` timeouts).
const GH_TIMEOUT: Duration = Duration::from_secs(20);
const GH_ARCHIVE_TIMEOUT: Duration = Duration::from_secs(600);
/// Requests' `Session.max_redirects`.
const MAX_REDIRECTS: usize = 30;

#[derive(Default)]
struct Sidecar {
    child: Option<Child>,
    started_at: f64,
}

fn sidecar() -> MutexGuard<'static, Sidecar> {
    static SIDECAR: OnceLock<Mutex<Sidecar>> = OnceLock::new();
    SIDECAR
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner())
}

/// Serializes ensure / stop / resume; status never waits on it.
fn lifecycle() -> MutexGuard<'static, ()> {
    static LIFECYCLE: Mutex<()> = Mutex::new(());
    LIFECYCLE.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Default)]
struct UpdateState {
    last_check: f64,
    available: String,
}

fn update_state() -> MutexGuard<'static, UpdateState> {
    static STATE: OnceLock<Mutex<UpdateState>> = OnceLock::new();
    STATE
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner())
}

// ── paths & policy ──────────────────────────────────────────────────────────

fn adapter_root() -> PathBuf {
    let root = crate::config::config_path()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_default()
        .join("adapter");
    let _ = fs::create_dir_all(&root);
    root
}

fn binary_name() -> &'static str {
    if cfg!(windows) {
        "cliproxyapi.exe"
    } else {
        "cliproxyapi"
    }
}

fn binary_path() -> PathBuf {
    adapter_root().join(binary_name())
}

fn read_policy() -> Map<String, Value> {
    fs::read(adapter_root().join("policy.json"))
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn write_policy(policy: &Map<String, Value>) -> Result<(), String> {
    let bytes = serde_json::to_vec_pretty(policy).map_err(|e| e.to_string())?;
    crate::config::write_atomic(&adapter_root().join("policy.json"), &bytes)
        .map_err(|e| format!("adapter policy not saved: {e}"))
}

fn installed_version() -> String {
    fs::read_to_string(adapter_root().join("version.txt"))
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// Python `int(value or 0)` for a port; None when not an integer port.
fn port_value(value: Option<&Value>) -> Option<u32> {
    match value.filter(|v| !egress::falsy(v)) {
        None => Some(0),
        Some(Value::Number(n)) => n.as_u64().and_then(|p| u32::try_from(p).ok()),
        Some(Value::String(s)) => s.trim().parse::<u32>().ok(),
        Some(Value::Bool(true)) => Some(1),
        Some(_) => None,
    }
}

fn policy_port(policy: &Map<String, Value>) -> u16 {
    port_value(policy.get("port"))
        .and_then(|p| u16::try_from(p).ok())
        .unwrap_or(0)
}

/// The port the loopback egress whitelist accepts (0 = no adapter).
pub fn loopback_port() -> u16 {
    policy_port(&read_policy())
}

// ── download + verify ───────────────────────────────────────────────────────

fn gh_get(url: &str, timeout: Duration) -> Result<wire::Response, String> {
    let mut current = wire::parse_url(url)?;
    if !ALLOWED_DOWNLOAD_HOSTS.contains(&current.host.as_str()) {
        return Err(format!("download host not allowed: {}", current.host));
    }
    let headers = vec![
        (
            "User-Agent".to_string(),
            "tofu-agent-adapter/1.0".to_string(),
        ),
        (
            "Accept".to_string(),
            "application/vnd.github+json".to_string(),
        ),
    ];
    for _ in 0..=MAX_REDIRECTS {
        let proxy = egress::resolve_proxy("env", &current.absolute());
        let options = wire::Options {
            proxy: proxy.as_deref(),
            connect_timeout: timeout,
            read_timeout: timeout,
        };
        let response = wire::send("GET", &current, &headers, b"", &options, &mut |_| {})
            .map_err(|e| e.to_string())?;
        let status = response.status;
        if [301, 302, 303, 307, 308].contains(&status) {
            let location = response
                .header("location")
                .ok_or_else(|| format!("{status} redirect without Location"))?;
            // GitHub hands assets to its CDN; the SHA-256 check, not the host, guards them.
            current = current.join(location)?;
            if current.scheme != "https" {
                return Err("adapter download redirected off https".into());
            }
            continue;
        }
        if status >= 400 {
            return Err(format!(
                "{status} Error for url: {}",
                current.absolute().split('?').next().unwrap_or("")
            ));
        }
        return Ok(response);
    }
    Err(format!("Exceeded {MAX_REDIRECTS} redirects."))
}

fn read_capped(response: wire::Response) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    response
        .into_body()
        .take(MAX_ADAPTER_DOWNLOAD_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_ADAPTER_DOWNLOAD_BYTES {
        return Err("adapter download exceeds the 256 MiB limit".into());
    }
    Ok(bytes)
}

/// `latest` or a pin → (tag_name, assets).
fn resolve_release(version: &str) -> Result<(String, Vec<Value>), String> {
    let version = version.trim();
    let url = if version.is_empty() || version == "latest" {
        format!("{GH_API}/latest")
    } else if version.starts_with('v') {
        format!("{GH_API}/tags/{version}")
    } else {
        format!("{GH_API}/tags/v{version}")
    };
    let data: Value = serde_json::from_slice(&read_capped(gh_get(&url, GH_TIMEOUT)?)?)
        .map_err(|e| format!("release lookup answered no JSON: {e}"))?;
    let tag = data["tag_name"].as_str().unwrap_or("").to_string();
    let assets = data["assets"].as_array().cloned().unwrap_or_default();
    Ok((tag, assets))
}

/// The release asset for an OS (`std::env::consts::OS`) and architecture.
fn asset_for(os: &str, arch: &str, assets: &[Value]) -> Result<(String, String), String> {
    let arch = if arch == "aarch64" || arch == "arm64" {
        "aarch64"
    } else {
        "amd64"
    };
    let want = match os {
        "windows" => format!("windows_{arch}.zip"),
        "macos" => format!("darwin_{arch}.tar.gz"),
        _ => format!("linux_{arch}.tar.gz"),
    };
    for asset in assets {
        let name = asset["name"].as_str().unwrap_or("");
        if name.ends_with(&want) && !name.contains("no-plugin") {
            let url = asset["browser_download_url"].as_str().unwrap_or("");
            return Ok((name.to_string(), url.to_string()));
        }
    }
    Err(format!("no release asset matching *{want}"))
}

/// goreleaser checksums.txt → {asset name: sha256 hex}.
fn parse_checksums(text: &str) -> std::collections::HashMap<String, String> {
    text.lines()
        .filter_map(|line| {
            let parts: Vec<&str> = line.split_whitespace().collect();
            (parts.len() == 2 && parts[0].len() == 64)
                .then(|| (parts[1].to_string(), parts[0].to_ascii_lowercase()))
        })
        .collect()
}

fn sha256_file(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    std::io::copy(&mut file, &mut hasher).map_err(|e| e.to_string())?;
    Ok(hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

/// Cross-platform traversal check for archive member names.
fn safe_archive_name(name: &str) -> bool {
    let portable = name.replace('\\', "/");
    let bytes = portable.as_bytes();
    let drive = bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':';
    !name.is_empty()
        && !portable.starts_with('/')
        && !drive
        && !portable.split('/').any(|part| part == "..")
}

fn is_adapter_binary(name: &str) -> bool {
    let portable = name.replace('\\', "/");
    let base = portable.rsplit('/').next().unwrap_or("");
    base.to_lowercase()
        .replace('-', "")
        .starts_with("cliproxyapi")
}

/// Copy one archive member and atomically replace the binary; a short,
/// oversized or corrupt member never replaces a working binary.
fn install_binary_stream(
    src: &mut dyn Read,
    declared_size: u64,
    dest_dir: &Path,
    expected_crc: Option<u32>,
) -> Result<PathBuf, String> {
    if declared_size == 0 || declared_size > MAX_ADAPTER_DOWNLOAD_BYTES {
        return Err("adapter binary has an unsafe declared size".into());
    }
    fs::create_dir_all(dest_dir).map_err(|e| e.to_string())?;
    let dst = dest_dir.join(binary_name());
    let staged = dest_dir.join(format!(".cliproxyapi-{}", uuid::Uuid::new_v4().simple()));
    let result = (|| {
        let mut out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&staged)
            .map_err(|e| e.to_string())?;
        let mut crc = crc32fast::Hasher::new();
        let mut total: u64 = 0;
        let mut buffer = vec![0u8; 1 << 20];
        loop {
            let n = src.read(&mut buffer).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            total += n as u64;
            if total > MAX_ADAPTER_DOWNLOAD_BYTES {
                return Err("adapter binary exceeds the safe size limit".to_string());
            }
            crc.update(&buffer[..n]);
            out.write_all(&buffer[..n]).map_err(|e| e.to_string())?;
        }
        out.sync_all().map_err(|e| e.to_string())?;
        if total != declared_size {
            return Err(format!(
                "adapter binary size mismatch: expected {declared_size}, got {total}"
            ));
        }
        if expected_crc.is_some_and(|want| want != crc.finalize()) {
            return Err("adapter binary failed its CRC-32 check".to_string());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&staged, fs::Permissions::from_mode(0o755))
                .map_err(|e| e.to_string())?;
        }
        fs::rename(&staged, &dst).map_err(|e| e.to_string())
    })();
    let _ = fs::remove_file(&staged);
    result.map(|_| dst)
}

fn extract_tar_gz(archive: &Path, dest_dir: &Path) -> Result<PathBuf, String> {
    let file = File::open(archive).map_err(|e| e.to_string())?;
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(file));
    for (index, entry) in tar.entries().map_err(|e| e.to_string())?.enumerate() {
        if index + 1 > MAX_ADAPTER_ARCHIVE_MEMBERS {
            return Err("adapter archive has too many members".into());
        }
        let mut entry = entry.map_err(|e| e.to_string())?;
        let name = String::from_utf8_lossy(&entry.path_bytes()).into_owned();
        if !safe_archive_name(&name) {
            return Err(format!("unsafe adapter archive path: {name:?}"));
        }
        let kind = entry.header().entry_type();
        if kind.is_symlink()
            || kind.is_hard_link()
            || kind.is_character_special()
            || kind.is_block_special()
            || kind.is_fifo()
        {
            return Err("adapter archive contains a special member".into());
        }
        if (kind.is_file() || kind.is_contiguous()) && is_adapter_binary(&name) {
            let size = entry.header().size().map_err(|e| e.to_string())?;
            return install_binary_stream(&mut entry, size, dest_dir, None);
        }
    }
    Err("cliproxyapi binary not found in archive".into())
}

fn le16(bytes: &[u8], at: usize) -> Result<u64, String> {
    bytes
        .get(at..at + 2)
        .map(|b| u64::from(u16::from_le_bytes([b[0], b[1]])))
        .ok_or_else(|| "truncated zip structure".to_string())
}

fn le32(bytes: &[u8], at: usize) -> Result<u64, String> {
    bytes
        .get(at..at + 4)
        .map(|b| u64::from(u32::from_le_bytes([b[0], b[1], b[2], b[3]])))
        .ok_or_else(|| "truncated zip structure".to_string())
}

/// Reads the central directory of a (non-zip64) zip and installs only the
/// binary member: stored or deflated, CRC-checked.
fn extract_zip(archive: &Path, dest_dir: &Path) -> Result<PathBuf, String> {
    const EOCD: &[u8] = b"PK\x05\x06";
    let mut file = File::open(archive).map_err(|e| e.to_string())?;
    let len = file.metadata().map_err(|e| e.to_string())?.len();
    let tail_len = len.min(22 + 65535);
    file.seek(SeekFrom::Start(len - tail_len))
        .map_err(|e| e.to_string())?;
    let mut tail = Vec::new();
    (&mut file)
        .take(tail_len)
        .read_to_end(&mut tail)
        .map_err(|e| e.to_string())?;
    let at = (0..tail.len().saturating_sub(21))
        .rev()
        .find(|&i| &tail[i..i + 4] == EOCD)
        .ok_or("File is not a zip file")?;
    let total = le16(&tail, at + 10)?;
    let cd_size = le32(&tail, at + 12)?;
    let cd_offset = le32(&tail, at + 16)?;
    if total == 0xFFFF || cd_offset == 0xFFFF_FFFF {
        return Err("zip64 adapter archives are not supported".into());
    }
    if total as usize > MAX_ADAPTER_ARCHIVE_MEMBERS {
        return Err("adapter archive has too many members".into());
    }
    file.seek(SeekFrom::Start(cd_offset))
        .map_err(|e| e.to_string())?;
    let mut cd = Vec::new();
    (&mut file)
        .take(cd_size)
        .read_to_end(&mut cd)
        .map_err(|e| e.to_string())?;
    let mut pos = 0usize;
    for _ in 0..total {
        if cd.get(pos..pos + 4) != Some(b"PK\x01\x02") {
            return Err("bad zip central directory".into());
        }
        let method = le16(&cd, pos + 10)?;
        let crc = le32(&cd, pos + 16)? as u32;
        let compressed = le32(&cd, pos + 20)?;
        let size = le32(&cd, pos + 24)?;
        let name_len = le16(&cd, pos + 28)? as usize;
        let extra_len = le16(&cd, pos + 30)? as usize;
        let comment_len = le16(&cd, pos + 32)? as usize;
        let external = le32(&cd, pos + 38)?;
        let local = le32(&cd, pos + 42)?;
        let name_bytes = cd
            .get(pos + 46..pos + 46 + name_len)
            .ok_or("truncated zip structure")?;
        let name = String::from_utf8_lossy(name_bytes).into_owned();
        pos += 46 + name_len + extra_len + comment_len;
        if !safe_archive_name(&name) {
            return Err(format!("unsafe adapter archive path: {name:?}"));
        }
        if (external >> 16) & 0o170000 == 0o120000 {
            return Err("adapter archive contains a symbolic link".into());
        }
        if name.ends_with('/') || !is_adapter_binary(&name) {
            continue;
        }
        file.seek(SeekFrom::Start(local))
            .map_err(|e| e.to_string())?;
        let mut header = [0u8; 30];
        file.read_exact(&mut header).map_err(|e| e.to_string())?;
        if &header[..4] != b"PK\x03\x04" {
            return Err("bad zip local header".into());
        }
        let skip = le16(&header, 26)? + le16(&header, 28)?;
        file.seek(SeekFrom::Current(skip as i64))
            .map_err(|e| e.to_string())?;
        let data = (&mut file).take(compressed);
        let mut reader: Box<dyn Read> = match method {
            0 => Box::new(data),
            8 => Box::new(flate2::read::DeflateDecoder::new(data)),
            other => return Err(format!("unsupported zip compression method {other}")),
        };
        return install_binary_stream(&mut reader, size, dest_dir, Some(crc));
    }
    Err("cliproxyapi binary not found in archive".into())
}

fn extract_binary(archive: &Path, dest_dir: &Path) -> Result<PathBuf, String> {
    if archive.to_string_lossy().ends_with(".zip") {
        extract_zip(archive, dest_dir)
    } else {
        extract_tar_gz(archive, dest_dir)
    }
}

/// Download, verify and install the pinned (or latest) release; returns the
/// installed tag. Verification happens entirely in a temp dir before the swap.
fn install_binary(version: &str) -> Result<String, String> {
    let (tag, assets) = resolve_release(version)?;
    if tag.is_empty() {
        return Err("release lookup returned no tag_name".into());
    }
    let (name, url) = asset_for(std::env::consts::OS, std::env::consts::ARCH, &assets)?;
    let sums_url = assets
        .iter()
        .find(|a| a["name"] == "checksums.txt")
        .and_then(|a| a["browser_download_url"].as_str())
        .ok_or("release carries no checksums.txt — refusing unverifiable download")?;
    let tmpdir = std::env::temp_dir().join(format!("adapter-dl-{}", uuid::Uuid::new_v4().simple()));
    fs::create_dir_all(&tmpdir).map_err(|e| e.to_string())?;
    let result = (|| {
        let sums =
            String::from_utf8_lossy(&read_capped(gh_get(sums_url, GH_TIMEOUT)?)?).into_owned();
        let expected = parse_checksums(&sums)
            .remove(&name)
            .ok_or_else(|| format!("{name} not listed in checksums.txt"))?;
        let archive = tmpdir.join(&name);
        log::info(&format!("[Adapter] downloading {name} ({tag})"));
        let response = gh_get(&url, GH_ARCHIVE_TIMEOUT)?;
        let declared = response
            .header("content-length")
            .and_then(|v| v.trim().parse::<u64>().ok())
            .unwrap_or(0);
        if declared > MAX_ADAPTER_DOWNLOAD_BYTES {
            return Err("adapter archive exceeds the 256 MiB limit".to_string());
        }
        let mut out = File::create(&archive).map_err(|e| e.to_string())?;
        let copied = std::io::copy(
            &mut response.into_body().take(MAX_ADAPTER_DOWNLOAD_BYTES + 1),
            &mut out,
        )
        .map_err(|e| e.to_string())?;
        if copied > MAX_ADAPTER_DOWNLOAD_BYTES {
            return Err("adapter archive exceeds the 256 MiB limit".to_string());
        }
        drop(out);
        let actual = sha256_file(&archive)?;
        if actual != expected {
            return Err(format!(
                "SHA-256 mismatch for {name}: expected {expected}, got {actual}"
            ));
        }
        let dst = extract_binary(&archive, &adapter_root())?;
        fs::write(adapter_root().join("version.txt"), &tag).map_err(|e| e.to_string())?;
        log::info(&format!("[Adapter] installed {tag} → {}", dst.display()));
        Ok(tag.clone())
    })();
    let _ = fs::remove_dir_all(&tmpdir);
    result
}

// ── config + process lifecycle ──────────────────────────────────────────────

/// A YAML double-quoted scalar; escaping keeps a policy value from adding keys.
fn yaml_quoted(value: &str) -> String {
    let mut out = String::from("\"");
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn config_yaml(port: u16, mgmt_secret: &str, api_key: &str, auth_dir: &str) -> String {
    format!(
        "# Written by tofu node (packages/tofu-node/src/commands/adapter.rs) — do not edit.\n\
         host: \"127.0.0.1\"\n\
         port: {port}\n\
         tls:\n  enable: false\n  cert: \"\"\n  key: \"\"\n\
         remote-management:\n  allow-remote: false\n  secret-key: {}\n  disable-control-panel: true\n\
         auth-dir: {}\n\
         api-keys:\n  - {}\n\
         debug: false\n",
        yaml_quoted(mgmt_secret),
        yaml_quoted(auth_dir),
        yaml_quoted(api_key)
    )
}

fn policy_text(policy: &Map<String, Value>, key: &str) -> String {
    egress::text(&Value::Object(policy.clone()), key, "")
}

fn write_config(policy: &Map<String, Value>) -> Result<PathBuf, String> {
    let root = adapter_root();
    let auth_dir = root.join("auth");
    fs::create_dir_all(&auth_dir).map_err(|e| e.to_string())?;
    let config = root.join("config.yaml");
    let content = config_yaml(
        policy_port(policy),
        &policy_text(policy, "mgmt_secret"),
        &policy_text(policy, "api_key"),
        &auth_dir.to_string_lossy().replace('\\', "/"),
    );
    fs::write(&config, content).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&config, fs::Permissions::from_mode(0o600));
    }
    Ok(config)
}

fn is_running() -> bool {
    sidecar()
        .child
        .as_mut()
        .is_some_and(|c| matches!(c.try_wait(), Ok(None)))
}

fn spawn(policy: &Map<String, Value>) -> Result<(), String> {
    let root = adapter_root();
    let console = OpenOptions::new()
        .create(true)
        .append(true)
        .open(root.join("adapter.log"))
        .map_err(|e| e.to_string())?;
    let mut command = Command::new(binary_path());
    command
        .arg("--config")
        .arg(root.join("config.yaml"))
        .current_dir(&root)
        .stdin(Stdio::null())
        .stdout(console.try_clone().map_err(|e| e.to_string())?)
        .stderr(console);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: setsid is async-signal-safe (Python `start_new_session=True`).
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};
        command.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    }
    let child = command.spawn().map_err(|e| e.to_string())?;
    log::info(&format!(
        "[Adapter] spawned pid={} port={}",
        child.id(),
        policy_port(policy)
    ));
    let mut state = sidecar();
    state.child = Some(child);
    state.started_at = crate::util::unix_now();
    Ok(())
}

fn healthy(port: u16, api_key: &str) -> bool {
    let deadline = Instant::now() + HEALTH_TIMEOUT;
    let Ok(url) = wire::parse_url(&format!("http://127.0.0.1:{port}/v1/models")) else {
        return false;
    };
    let headers = vec![("Authorization".to_string(), format!("Bearer {api_key}"))];
    let options = wire::Options {
        proxy: None,
        connect_timeout: HEALTH_PROBE_TIMEOUT,
        read_timeout: HEALTH_PROBE_TIMEOUT,
    };
    while Instant::now() < deadline {
        if let Ok(response) = wire::send("GET", &url, &headers, b"", &options, &mut |_| {}) {
            if response.status == 200 {
                return true;
            }
        }
        if !is_running() {
            return false;
        }
        std::thread::sleep(HEALTH_PROBE_PAUSE);
    }
    false
}

fn stop_proc() {
    let child = sidecar().child.take();
    if let Some(mut child) = child {
        if matches!(child.try_wait(), Ok(None)) {
            let pid = child.id();
            #[cfg(unix)]
            // SAFETY: plain signal to our own, not yet reaped child.
            unsafe {
                libc::kill(pid as libc::pid_t, libc::SIGTERM);
            }
            #[cfg(windows)]
            let _ = child.kill();
            let (done, exited) = std::sync::mpsc::channel();
            std::thread::spawn(move || {
                let _ = child.wait();
                let _ = done.send(());
            });
            if exited.recv_timeout(STOP_GRACE).is_err() {
                log::warn(&format!("[Adapter] pid={pid} ignored terminate, killing"));
                #[cfg(unix)]
                // SAFETY: the waiter thread has not reaped the child yet.
                unsafe {
                    libc::kill(pid as libc::pid_t, libc::SIGKILL);
                }
            }
        }
    }
    log::info("[Adapter] stopped");
}

/// Crash watch: respawn with capped backoff while a policy is active.
fn start_supervisor() {
    static STARTED: AtomicBool = AtomicBool::new(false);
    if STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let spawned = std::thread::Builder::new()
        .name("adapter-supervise".into())
        .spawn(|| {
            let mut backoff = RESTART_BACKOFF_START_SECONDS;
            loop {
                std::thread::sleep(Duration::from_secs(backoff));
                let policy = read_policy();
                if !policy.get("active").is_some_and(|v| !egress::falsy(v)) {
                    continue;
                }
                if is_running() {
                    backoff = RESTART_BACKOFF_START_SECONDS;
                    continue;
                }
                log::warn(&format!(
                    "[Adapter] sidecar died — restarting (backoff {backoff}s)"
                ));
                if let Err(e) = spawn(&policy) {
                    log::error(&format!("[Adapter] respawn failed: {e}"));
                }
                backoff = (backoff * 2).min(RESTART_BACKOFF_CAP_SECONDS);
            }
        });
    if spawned.is_err() {
        STARTED.store(false, Ordering::SeqCst);
    }
}

/// Weekly (+jitter) release check: a binary pinned forever would re-create the
/// cloaking drift the upstream community keeps fixing.
fn start_update_thread() {
    static STARTED: AtomicBool = AtomicBool::new(false);
    if STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let spawned = std::thread::Builder::new()
        .name("adapter-update".into())
        .spawn(|| {
            let jitter = UPDATE_FIRST_DELAY_SECONDS
                + crate::util::random_unit() * (UPDATE_JITTER_SECONDS - UPDATE_FIRST_DELAY_SECONDS);
            std::thread::sleep(Duration::from_secs_f64(jitter));
            loop {
                std::thread::sleep(UPDATE_INTERVAL);
                let policy = read_policy();
                let on = |k: &str| policy.get(k).is_some_and(|v| !egress::falsy(v));
                if !on("active") || !on("auto_update") {
                    continue;
                }
                update_state().last_check = crate::util::unix_now();
                match resolve_release("latest") {
                    Ok((tag, _)) => {
                        let current = installed_version();
                        if !tag.is_empty() && !current.is_empty() && tag != current {
                            update_state().available = tag.clone();
                            log::info(&format!(
                                "[Adapter] update available: {current} → {tag}, applying"
                            ));
                            match install_binary("latest") {
                                Ok(_) => {
                                    stop_proc(); // the supervisor respawns on the new binary
                                    update_state().available.clear();
                                }
                                Err(e) => log::warn(&format!("[Adapter] update failed: {e}")),
                            }
                        }
                    }
                    Err(e) => log::warn(&format!("[Adapter] update check failed: {e}")),
                }
            }
        });
    if spawned.is_err() {
        STARTED.store(false, Ordering::SeqCst);
    }
}

/// The desired port, or the next free one among the candidates. A port is
/// free when nothing answers a loopback connect.
fn pick_port(desired: u32) -> Result<u16, String> {
    for port in desired..desired + PORT_CANDIDATES {
        let Ok(port) = u16::try_from(port) else {
            break;
        };
        if port != 0 && TcpStream::connect(("127.0.0.1", port)).is_err() {
            return Ok(port);
        }
    }
    Err(format!(
        "no free loopback port in {desired}..{}",
        desired + PORT_CANDIDATES - 1
    ))
}

// ── bridge commands ─────────────────────────────────────────────────────────

fn status_value() -> Value {
    let policy = read_policy();
    let (running, pid, uptime) = {
        let mut state = sidecar();
        let started_at = state.started_at;
        match state.child.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(None) => (
                    true,
                    child.id(),
                    (crate::util::unix_now() - started_at).max(0.0) as u64,
                ),
                _ => (false, 0, 0),
            },
            None => (false, 0, 0),
        }
    };
    let accounts = fs::read_dir(adapter_root().join("auth"))
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .filter(|e| e.file_name().to_string_lossy().ends_with(".json"))
                .count()
        })
        .unwrap_or(0);
    let updates = update_state();
    let on = |k: &str| policy.get(k).is_some_and(|v| !egress::falsy(v));
    json!({
        "installed": binary_path().is_file(),
        "running": running,
        "version": installed_version(),
        "port": policy_port(&policy),
        "pid": pid,
        "uptime_s": uptime,
        "accounts": accounts,
        "auto_update": on("auto_update"),
        "active": on("active"),
        "last_update_check": updates.last_check,
        "update_available": updates.available,
    })
}

/// Idempotent bring-up: policy → binary (download + verify when needed) →
/// config → spawn → health gate. Long on first download; the server sends it
/// with a 600 s TTL.
pub fn ensure(params: &Value) -> Value {
    let _lifecycle = lifecycle();
    let mut policy = read_policy();
    let current = Value::Object(policy.clone());
    let pick = |key: &str, default: &str| {
        let from_params = egress::text(params, key, "");
        if from_params.is_empty() {
            egress::text(&current, key, default)
        } else {
            from_params
        }
    };
    let port = match port_value(params.get("port")) {
        Some(0) => port_value(policy.get("port"))
            .filter(|p| *p != 0)
            .unwrap_or(u32::from(DEFAULT_PORT)),
        Some(port) => port,
        None => return json!({"error": "adapter policy has an invalid port"}),
    };
    let api_key = pick("api_key", "");
    let mgmt_secret = pick("mgmt_secret", "");
    let version = pick("version", "latest");
    let auto_update = params.get("auto_update").is_none_or(|v| !egress::falsy(v));
    if api_key.is_empty() || mgmt_secret.is_empty() {
        return json!({"error": "adapter policy incomplete (api_key/mgmt_secret)"});
    }
    let port = if is_running() {
        match u16::try_from(port) {
            Ok(port) => port,
            Err(_) => return json!({"error": "adapter policy has an invalid port"}),
        }
    } else {
        match pick_port(port) {
            Ok(port) => port,
            Err(e) => {
                log::warn(&format!("[Adapter] invalid policy: {e}"));
                return json!({"error": e});
            }
        }
    };
    policy.insert("port".into(), port.into());
    policy.insert("api_key".into(), api_key.clone().into());
    policy.insert("mgmt_secret".into(), mgmt_secret.into());
    policy.insert("version".into(), version.clone().into());
    policy.insert("auto_update".into(), auto_update.into());
    policy.insert("active".into(), true.into());
    if let Err(e) = write_policy(&policy) {
        return json!({"error": e});
    }
    let installed = installed_version();
    let version_mismatch = version != "latest"
        && !installed.is_empty()
        && installed != version
        && installed != format!("v{version}");
    if !binary_path().is_file() || version_mismatch {
        if let Err(e) = install_binary(&version) {
            log::error(&format!("[Adapter] binary install failed: {e}"));
            return json!({"error": format!("adapter download/verify failed: {e}")});
        }
    }
    if let Err(e) = write_config(&policy) {
        return json!({"error": format!("adapter config not written: {e}")});
    }
    if !is_running() {
        if let Err(e) = spawn(&policy) {
            log::error(&format!("[Adapter] spawn failed: {e}"));
            return json!({"error": format!("adapter spawn failed: {e}")});
        }
    }
    start_supervisor();
    start_update_thread();
    if !healthy(port, &api_key) {
        let mut failed = status_value();
        failed["error"] = json!(format!(
            "adapter failed health check within {}s — see adapter.log",
            HEALTH_TIMEOUT.as_secs()
        ));
        return failed;
    }
    status_value()
}

pub fn status(_params: &Value) -> Value {
    status_value()
}

pub fn stop(_params: &Value) -> Value {
    let _lifecycle = lifecycle();
    let mut policy = read_policy();
    policy.insert("active".into(), false.into());
    if let Err(e) = write_policy(&policy) {
        log::warn(&format!("[Adapter] {e}"));
    }
    stop_proc();
    status_value()
}

/// A previously ensured sidecar comes back with an egress-enabled link,
/// without waiting for a fresh ensure.
pub fn maybe_resume() {
    let policy = read_policy();
    if !policy.get("active").is_some_and(|v| !egress::falsy(v)) {
        return;
    }
    let _lifecycle = lifecycle();
    log::info(&format!(
        "[Adapter] resuming sidecar (port={})",
        policy_port(&policy)
    ));
    if !binary_path().is_file() {
        log::warn("[Adapter] binary missing on resume — waiting for next ensure");
        return;
    }
    let resumed =
        write_config(&policy).and_then(|_| if is_running() { Ok(()) } else { spawn(&policy) });
    match resumed {
        Ok(()) => {
            start_supervisor();
            start_update_thread();
        }
        Err(e) => log::error(&format!("[Adapter] resume failed: {e}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checksums_and_assets_select_like_the_python_node() {
        let sums = format!(
            "{} cliproxyapi_1.0_linux_amd64.tar.gz\nshort x\n{} b",
            "A".repeat(64),
            "b".repeat(64)
        );
        let parsed = parse_checksums(&sums);
        assert_eq!(parsed["cliproxyapi_1.0_linux_amd64.tar.gz"], "a".repeat(64));
        assert_eq!(parsed.len(), 2);
        let assets = vec![
            json!({"name": "CLIProxyAPI_1.0_linux_amd64_no-plugin.tar.gz", "browser_download_url": "u0"}),
            json!({"name": "CLIProxyAPI_1.0_linux_amd64.tar.gz", "browser_download_url": "u1"}),
            json!({"name": "CLIProxyAPI_1.0_windows_amd64.zip", "browser_download_url": "u2"}),
            json!({"name": "CLIProxyAPI_1.0_darwin_aarch64.tar.gz", "browser_download_url": "u3"}),
        ];
        assert_eq!(asset_for("linux", "x86_64", &assets).unwrap().1, "u1");
        assert_eq!(asset_for("windows", "x86_64", &assets).unwrap().1, "u2");
        assert_eq!(asset_for("macos", "aarch64", &assets).unwrap().1, "u3");
        assert!(asset_for("linux", "aarch64", &assets).is_err());
    }

    #[test]
    fn archive_names_are_checked_for_traversal() {
        assert!(safe_archive_name("dir/cliproxyapi"));
        for bad in [
            "",
            "/etc/x",
            "../x",
            "a/../../x",
            "a\\..\\x",
            "C:\\x",
            "C:/x",
            "\\x",
        ] {
            assert!(!safe_archive_name(bad), "{bad}");
        }
        assert!(is_adapter_binary("CLIProxyAPI_1.0/cli-proxy-api"));
        assert!(is_adapter_binary("x\\CLIProxyAPI.exe"));
        assert!(!is_adapter_binary("README.md"));
    }

    #[test]
    fn config_quotes_policy_values() {
        let yaml = config_yaml(8317, "s\"\nhost: 0.0.0.0", "k", "/a/auth");
        assert!(yaml.contains("host: \"127.0.0.1\"\n"));
        assert!(!yaml.contains("\nhost: 0.0.0.0"));
        assert!(yaml.contains("secret-key: \"s\\\"\\x0ahost: 0.0.0.0\""));
        assert!(yaml.contains("api-keys:\n  - \"k\"\n"));
    }

    #[test]
    fn port_values_follow_python_int() {
        assert_eq!(port_value(None), Some(0));
        assert_eq!(port_value(Some(&json!("8318"))), Some(8318));
        assert_eq!(port_value(Some(&json!(8319))), Some(8319));
        assert_eq!(port_value(Some(&json!("x"))), None);
        let mut policy = Map::new();
        policy.insert("port".into(), json!(70000));
        assert_eq!(policy_port(&policy), 0);
    }

    #[test]
    fn pick_port_skips_a_busy_port() {
        let busy = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = u32::from(busy.local_addr().unwrap().port());
        let picked = pick_port(port).unwrap();
        assert_ne!(u32::from(picked), port);
        assert!(u32::from(picked) < port + PORT_CANDIDATES);
        assert!(pick_port(65535 + 1).is_err());
    }

    fn tar_gz(members: &[(&str, &[u8], tar::EntryType)]) -> Vec<u8> {
        let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(
            Vec::new(),
            flate2::Compression::default(),
        ));
        for (name, body, kind) in members {
            let mut header = tar::Header::new_gnu();
            header.set_entry_type(*kind);
            header.set_size(body.len() as u64);
            header.set_mode(0o755);
            let raw = header.as_old_mut();
            raw.name[..name.len()].copy_from_slice(name.as_bytes());
            header.set_cksum();
            builder.append(&header, *body).unwrap();
        }
        builder.into_inner().unwrap().finish().unwrap()
    }

    #[test]
    fn tar_install_is_atomic_and_refuses_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("a_linux_amd64.tar.gz");
        fs::write(
            &archive,
            tar_gz(&[
                ("README.md", b"hi", tar::EntryType::Regular),
                ("cli-proxy-api", b"new binary", tar::EntryType::Regular),
            ]),
        )
        .unwrap();
        let dest = dir.path().join("dest");
        let installed = extract_binary(&archive, &dest).unwrap();
        assert_eq!(fs::read(&installed).unwrap(), b"new binary");
        let leftovers: Vec<_> = fs::read_dir(&dest)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|e| e.file_name().to_string_lossy().starts_with('.'))
            .collect();
        assert!(leftovers.is_empty());
        fs::write(
            &archive,
            tar_gz(&[("../cliproxyapi", b"evil", tar::EntryType::Regular)]),
        )
        .unwrap();
        assert!(extract_binary(&archive, &dest)
            .unwrap_err()
            .contains("unsafe"));
        assert_eq!(fs::read(&installed).unwrap(), b"new binary");
        fs::write(
            &archive,
            tar_gz(&[("cliproxyapi", b"", tar::EntryType::Symlink)]),
        )
        .unwrap();
        assert!(extract_binary(&archive, &dest)
            .unwrap_err()
            .contains("special"));
    }

    fn zip_stored(name: &str, body: &[u8], mode: u32, crc: u32) -> Vec<u8> {
        let mut out = Vec::new();
        let n = name.len() as u16;
        let size = body.len() as u32;
        out.extend_from_slice(b"PK\x03\x04");
        out.extend_from_slice(&[20, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
        out.extend_from_slice(&crc.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&n.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(name.as_bytes());
        out.extend_from_slice(body);
        let cd_offset = out.len() as u32;
        let mut cd = Vec::new();
        cd.extend_from_slice(b"PK\x01\x02");
        cd.extend_from_slice(&[20, 3, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
        cd.extend_from_slice(&crc.to_le_bytes());
        cd.extend_from_slice(&size.to_le_bytes());
        cd.extend_from_slice(&size.to_le_bytes());
        cd.extend_from_slice(&n.to_le_bytes());
        cd.extend_from_slice(&[0, 0, 0, 0, 0, 0, 0, 0]);
        cd.extend_from_slice(&(mode << 16).to_le_bytes());
        cd.extend_from_slice(&0u32.to_le_bytes());
        cd.extend_from_slice(name.as_bytes());
        out.extend_from_slice(&cd);
        out.extend_from_slice(b"PK\x05\x06\0\0\0\0");
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&(cd.len() as u32).to_le_bytes());
        out.extend_from_slice(&cd_offset.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out
    }

    #[test]
    fn zip_install_checks_crc_symlinks_and_traversal() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("a_windows_amd64.zip");
        let dest = dir.path().join("dest");
        let crc = crc32fast::hash(b"exe");
        fs::write(
            &archive,
            zip_stored("cliproxyapi.exe", b"exe", 0o100755, crc),
        )
        .unwrap();
        let installed = extract_binary(&archive, &dest).unwrap();
        assert_eq!(fs::read(&installed).unwrap(), b"exe");
        fs::write(
            &archive,
            zip_stored("cliproxyapi.exe", b"bad", 0o100755, crc),
        )
        .unwrap();
        assert!(extract_binary(&archive, &dest).unwrap_err().contains("CRC"));
        assert_eq!(fs::read(&installed).unwrap(), b"exe");
        fs::write(&archive, zip_stored("cliproxyapi", b"x", 0o120777, 0)).unwrap();
        assert!(extract_binary(&archive, &dest)
            .unwrap_err()
            .contains("symbolic"));
        fs::write(&archive, zip_stored("../cliproxyapi", b"x", 0o100644, 0)).unwrap();
        assert!(extract_binary(&archive, &dest)
            .unwrap_err()
            .contains("unsafe"));
    }
}
