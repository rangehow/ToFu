//! One outbound bridge loop ("link") to one Home: poll, execute, report.
//!
//! Port of `lib/desktop_agent/_run.py::run_agent` (bridge v3). Every poll
//! carries the registration frame (`AgentRegistration` in
//! contracts/desktop_bridge_v3.schema.json), the acknowledged-prefix outboxes
//! and receives a validated command batch. A pending node (not yet confirmed
//! by the Home's owner) keeps polling and gets no commands. Tofu's own refusal
//! (401 revoked; 409 identity conflict / unregistered) ends the link; network
//! failures back off with jitter and repair the route over the attachment's
//! own candidates only. A link with a browser relay sends each poll through
//! the signed-in page while one is present (route `browser_relay`), else
//! directly (route `direct`).

use crate::commands;
use crate::commands::paper::PaperBuildWorker;
use crate::contract::{self, protocol_version};
use crate::host_identity;
use crate::http::{self, HttpError};
use crate::journal::CallJournal;
use crate::log;
use crate::permissions::{home_tier_refusal, permission_refusal, Permissions, SharedPermissions};
use crate::relay::BrowserRelay;
use crate::runtime::{PollError, RuntimeState};
use crate::util::{python_platform, retry_delay, Stop};
use serde_json::{json, Map, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Poll refusals that retrying cannot fix: the link stops and reports why.
const TERMINAL_REFUSALS: &[&str] = &[
    "desktop_node_identity_conflict",
    "desktop_node_unregistered",
];
/// Results larger than this are replaced by a typed error (the Python node's bound).
const RESULT_TRANSPORT_BYTES: usize = 500_000;
/// Consecutive dead-route polls before the candidate walk, and its pacing
/// (the Python node's route self-repair constants).
const ROUTE_REPAIR_THRESHOLD: u32 = 6;
const ROUTE_REPAIR_COOLDOWN: Duration = Duration::from_secs(300);
const POLL_TIMEOUT: Duration = Duration::from_secs(15);
/// How long a relayed poll may wait for the page (the Python node's bound).
const RELAY_POLL_TIMEOUT: Duration = Duration::from_secs(20);

/// One per node process (contracts/desktop_node_registry_v1 x-identity.boot).
pub fn boot_id() -> &'static str {
    static BOOT: OnceLock<String> = OnceLock::new();
    BOOT.get_or_init(|| uuid::Uuid::new_v4().simple().to_string())
}

pub fn product_version() -> &'static str {
    env!("TOFU_PRODUCT_VERSION")
}

fn which(program: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    let suffixes: &[&str] = if cfg!(windows) {
        &[".exe", ".cmd", ".bat", ""]
    } else {
        &[""]
    };
    std::env::split_paths(&path).any(|dir| {
        suffixes
            .iter()
            .any(|s| dir.join(format!("{program}{s}")).is_file())
    })
}

/// OS, architecture and command shells (`SystemFacts`); mirrors what the
/// executor can run.
pub fn system_facts() -> Option<Value> {
    let family = match python_platform() {
        "win32" => "windows",
        "darwin" => "macos",
        "linux" => "linux",
        _ => return None,
    };
    let (available, default): (Vec<&str>, &str) = if family == "windows" {
        let mut shells = vec!["powershell", "cmd"];
        if commands::exec::windows_posix_shell().is_some() {
            shells.insert(0, "sh");
        }
        (shells, "powershell")
    } else {
        let mut shells = vec!["sh"];
        if which("pwsh") {
            shells.push("powershell");
        }
        (shells, "sh")
    };
    let (release, version) = os_release_version();
    Some(json!({
        "os": family,
        "release": crate::util::truncate_chars(&release, 128),
        "version": crate::util::truncate_chars(&version, 256),
        "arch": crate::util::truncate_chars(arch(), 64),
        "shells": {"available": available, "default": default},
    }))
}

fn arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" if cfg!(windows) => "AMD64",
        "aarch64" if cfg!(target_os = "macos") => "arm64",
        "aarch64" if cfg!(windows) => "ARM64",
        other => other,
    }
}

#[cfg(unix)]
fn os_release_version() -> (String, String) {
    // SAFETY: utsname is plain data; uname fills it and NUL-terminates fields.
    let mut uts: libc::utsname = unsafe { std::mem::zeroed() };
    if unsafe { libc::uname(&mut uts) } != 0 {
        return (String::new(), String::new());
    }
    let field = |raw: &[libc::c_char]| {
        let bytes: Vec<u8> = raw
            .iter()
            .take_while(|c| **c != 0)
            .map(|c| *c as u8)
            .collect();
        String::from_utf8_lossy(&bytes).into_owned()
    };
    let mut release = field(&uts.release);
    if cfg!(target_os = "macos") {
        if let Ok(out) = std::process::Command::new("sw_vers")
            .arg("-productVersion")
            .output()
        {
            let mac = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !mac.is_empty() {
                release = mac;
            }
        }
    }
    (release, field(&uts.version))
}

#[cfg(not(unix))]
fn os_release_version() -> (String, String) {
    let out = std::process::Command::new("cmd")
        .args(["/d", "/c", "ver"])
        .output();
    let text = out
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    let version = text
        .rsplit(' ')
        .next()
        .unwrap_or("")
        .trim_end_matches(']')
        .to_string();
    let release = version.split('.').next().unwrap_or("").to_string();
    (release, version)
}

/// The registration frame sent with every poll (`AgentRegistration`).
pub fn agent_frame(
    agent_id: &str,
    permissions: &Permissions,
    share_roots: &[Value],
) -> Map<String, Value> {
    let mut frame = Map::new();
    frame.insert("agent_id".into(), agent_id.into());
    frame.insert("boot_id".into(), boot_id().into());
    frame.insert("protocol_version".into(), protocol_version().into());
    frame.insert("name".into(), host_identity::hostname().into());
    frame.insert("platform".into(), python_platform().into());
    frame.insert("version".into(), product_version().into());
    frame.insert("capabilities".into(), permissions.capabilities());
    frame.insert("share_roots".into(), Value::Array(share_roots.to_vec()));
    if let Some(system) = system_facts() {
        frame.insert("system".into(), system);
    }
    frame
}

/// Link transition reported to the owner of the loop (`on_status`).
#[derive(Clone, Debug, PartialEq)]
pub enum Status {
    /// Polled; `via_browser` when the signed-in page carried the poll.
    Ok {
        via_browser: bool,
    },
    Pending(String),
    /// Tofu itself refused the credential: revoked.
    Auth,
    /// A gateway, not Tofu, refused: the address is wrong.
    Proxy,
    Conflict {
        reason: String,
    },
    Http(u16),
    Unreachable,
}

/// Records a repaired route as the attachment's URL.
pub type RoutePersister = Box<dyn Fn(&str) + Send + Sync>;

pub struct LinkOptions {
    pub url: String,
    pub secret: String,
    pub candidates: Vec<String>,
    pub poll_interval: f64,
    pub stop_on_refusal: bool,
    pub on_status: Box<dyn Fn(&Status) + Send + Sync>,
    pub persist_route: Option<RoutePersister>,
    /// The link's browser relay (remote links only).
    pub relay: Option<Arc<BrowserRelay>>,
}

fn is_tofu_error_envelope(body: &Value) -> bool {
    body.get("error").and_then(Value::as_str) == Some("bridge_auth_required")
        || (body.get("ok") == Some(&Value::Bool(false))
            && body.get("error").is_some_and(Value::is_object))
}

fn is_route_url(url: &str) -> bool {
    crate::pairing::http_base(url).is_ok_and(|base| base == url.trim_end_matches('/'))
        && !url.chars().any(|c| c.is_whitespace() || (c as u32) < 32)
        && !url.contains('\\')
}

/// Verify `url` is answered by Tofu (`/api/health` with a `bootId`).
fn probe_server(url: &str, timeout: Duration) -> Result<(), String> {
    let base = url.trim().trim_end_matches('/');
    if base.is_empty() || !is_route_url(base) {
        return Err("unreachable".into());
    }
    match http::get(&format!("{base}/api/health"), timeout) {
        Err(HttpError::Timeout) => Err("timeout".into()),
        Err(HttpError::Unreachable(_)) => Err("unreachable".into()),
        Ok(response) if response.status != 200 => Err(format!("http_{}", response.status)),
        Ok(response) => match response.json() {
            Some(body)
                if body
                    .get("bootId")
                    .is_some_and(|b| !b.is_null() && b != "" && b != &json!(false)) =>
            {
                Ok(())
            }
            _ => Err("not_tofu".into()),
        },
    }
}

/// Probe-first route repair over the attachment's own candidates
/// (`lib/desktop_agent/_pair.py::resume_attachment`): a credential is never
/// carried to a discovered route.
fn repair_route(current: &str, candidates: &[String]) -> Option<String> {
    if probe_server(current, Duration::from_secs(3)).is_ok() {
        return None;
    }
    log::info(&format!(
        "Saved attachment {current} is not answering — checking its routes"
    ));
    let mut seen: Vec<String> = Vec::new();
    for candidate in candidates {
        let candidate = candidate.trim().trim_end_matches('/').to_string();
        if !is_route_url(&candidate) || candidate == current || seen.contains(&candidate) {
            continue;
        }
        seen.push(candidate.clone());
        match probe_server(&candidate, Duration::from_millis(2500)) {
            Ok(()) => {
                log::info(&format!(
                    "Attachment route {candidate} answers — re-pointing (credential kept)"
                ));
                return Some(candidate);
            }
            Err(reason) => log::info(&format!(
                "Attachment route {candidate} not reachable: {reason}"
            )),
        }
    }
    log::info("No route of this attachment answers; retrying the saved one");
    None
}

struct Executor {
    runtime: RuntimeState,
    journal: Arc<CallJournal>,
    permissions: SharedPermissions,
    paper: OnceLock<Arc<PaperBuildWorker>>,
    /// When the last 200 poll answered: a paper build stops once the Home is
    /// silent for `desktop_connection_seconds` (paper_build_v1).
    last_contact: Arc<Mutex<Instant>>,
}

impl Executor {
    fn paper_worker(&self) -> &Arc<PaperBuildWorker> {
        self.paper.get_or_init(|| {
            let contact = self.last_contact.clone();
            let window = Duration::from_secs(crate::commands::paper::desktop_connection_seconds());
            Arc::new(PaperBuildWorker::new(
                self.runtime.stop.clone(),
                Arc::new(move || {
                    contact.lock().unwrap_or_else(|e| e.into_inner()).elapsed() <= window
                }),
            ))
        })
    }

    /// Queue a completion: journal it, deliver it unless stopping, release the id.
    fn complete(&self, cmd: Option<(&str, &Value)>, result: Value) {
        if let Some((cmd_type, params)) = cmd {
            self.journal.complete(cmd_type, params, &result);
        }
        let id = result
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !self.runtime.stop.is_set() {
            self.runtime.push_result(result);
        }
        self.runtime.finish(&id);
    }

    fn execute(self: &Arc<Self>, cmd: Value) {
        let cmd_id = cmd["id"].as_str().unwrap_or("").to_string();
        let cmd_type = cmd["type"].as_str().unwrap_or("").to_string();
        let params = cmd.get("params").cloned().unwrap_or_else(|| json!({}));
        if let Some(refusal) = home_tier_refusal(&cmd_type, &params, cmd.get("home_tiers")) {
            return self.complete(
                None,
                json!({"id": cmd_id, "result": null, "error": refusal}),
            );
        }
        if let Some(recorded) = self.journal.begin(&cmd_id, &cmd_type, &params) {
            return self.complete(None, recorded);
        }
        let refusal = permission_refusal(
            &cmd_type,
            &params,
            &self.permissions.read().unwrap_or_else(|e| e.into_inner()),
        );
        if let Some(refusal) = refusal {
            return self.complete(
                Some((&cmd_type, &params)),
                json!({"id": cmd_id, "result": null, "error": refusal}),
            );
        }
        if cmd_type == "paper_build" {
            return self.run_paper_build(cmd_id, cmd_type, params);
        }
        if commands::is_streamed(&cmd_type) {
            return self.start_streamed(cmd_id, cmd_type, params);
        }
        let mut result = commands::dispatch(&cmd_type, &params);
        let encoded = serde_json::to_string(&result).unwrap_or_default();
        if encoded.len() > RESULT_TRANSPORT_BYTES {
            result = json!({"error": "Result too large for transport", "partial": crate::util::truncate_chars(&encoded, 100_000)});
        }
        let error = result.get("error").cloned().unwrap_or(Value::Null);
        self.complete(
            Some((&cmd_type, &params)),
            json!({"id": cmd_id, "result": result, "error": error}),
        );
    }

    fn run_paper_build(&self, cmd_id: String, cmd_type: String, params: Value) {
        let sequence = AtomicU64::new(1);
        let runtime = &self.runtime;
        let emit = |stream: &str, data: String| -> Result<(), String> {
            let seq = sequence.fetch_add(1, Ordering::SeqCst);
            runtime.push_stream(json!({"cmd_id": cmd_id, "seq": seq, "stream": stream, "data": data, "done": stream == "paper-end"}))
        };
        let outcome = self.paper_worker().execute(&cmd_id, &params, &emit);
        let completion = match outcome {
            Ok(result) => json!({"id": cmd_id, "result": result, "error": null}),
            Err(error) => json!({"id": cmd_id, "result": null, "error": error}),
        };
        self.complete(Some((&cmd_type, &params)), completion);
    }

    fn start_streamed(self: &Arc<Self>, cmd_id: String, cmd_type: String, params: Value) {
        let sequence = Arc::new(AtomicU64::new(1));
        let runtime = self.runtime.clone();
        let chunk_id = cmd_id.clone();
        let chunk_sequence = sequence.clone();
        let on_chunk: commands::ChunkSink = Arc::new(move |stream: &str, data: String| {
            let seq = chunk_sequence.fetch_add(1, Ordering::SeqCst);
            let _ = runtime.push_stream(json!({"cmd_id": chunk_id, "seq": seq, "stream": stream, "data": data, "done": false}));
        });
        let owner = self.clone();
        let exit_id = cmd_id.clone();
        let exit_type = cmd_type.clone();
        let exit_params = params.clone();
        let on_exit: commands::ExitSink = Box::new(move |outcome: Value| {
            if !owner.runtime.stop.is_set() {
                let seq = sequence.fetch_add(1, Ordering::SeqCst);
                let _ = owner.runtime.push_stream(json!({"cmd_id": exit_id, "seq": seq, "stream": "meta", "data": "", "done": true}));
            }
            log::info(&format!(
                "     {exit_type} done (exit={}, status={})",
                outcome.get("exit_code").unwrap_or(&Value::Null),
                outcome.get("status").unwrap_or(&Value::Null)
            ));
            // An egress stream reports its failure in the outcome itself.
            let error = if exit_type == "egress_http_stream" {
                outcome.get("error").cloned().unwrap_or(Value::Null)
            } else {
                Value::Null
            };
            owner.complete(
                Some((&exit_type, &exit_params)),
                json!({"id": exit_id, "result": outcome, "error": error}),
            );
        });
        if let Err(refusal) = commands::start_run(
            &cmd_type,
            &cmd_id,
            &params,
            on_chunk,
            on_exit,
            self.runtime.stop.clone(),
        ) {
            log::warn(&format!("     {cmd_type} refused: {refusal}"));
            self.complete(
                Some((&cmd_type, &params)),
                json!({"id": cmd_id, "result": null, "error": refusal}),
            );
        }
    }
}

/// Run one link until `stop` is set or Tofu refuses the node for good.
pub fn run_link(agent_id: &str, permissions: SharedPermissions, options: LinkOptions, stop: Stop) {
    let runtime = RuntimeState::new(stop.clone());
    let journal = match CallJournal::open(None) {
        Ok(journal) => Arc::new(journal),
        Err(error) => {
            log::error(&format!(
                "call journal unavailable, link not started: {error}"
            ));
            return;
        }
    };
    let last_contact = Arc::new(Mutex::new(Instant::now()));
    let executor = Arc::new(Executor {
        runtime: runtime.clone(),
        journal,
        permissions: permissions.clone(),
        paper: OnceLock::new(),
        last_contact: last_contact.clone(),
    });
    let current = |permissions: &SharedPermissions| -> Permissions {
        permissions
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    };
    if current(&permissions).allows("egress") {
        // A previously ensured subscription adapter resumes with the link.
        commands::adapter::maybe_resume();
    }
    let mut url = options.url.trim_end_matches('/').to_string();
    let secret = options.secret.clone();
    let mut frame = agent_frame(
        agent_id,
        &current(&permissions),
        &crate::config::share_roots(),
    );
    let mut last_status: Option<Status> = None;
    let emit = |status: Status, last: &mut Option<Status>| {
        if last.as_ref() != Some(&status) {
            (options.on_status)(&status);
            *last = Some(status);
        }
    };
    let mut route_dead_streak = 0u32;
    let mut last_walk: Option<Instant> = None;
    let mut consecutive_errors = 0u32;
    let mut negotiated = false;
    let mut rtt_ms: Option<u64> = None;
    log::info(&format!(
        "Node link starting: server {url}, node {} ({}), credential {}",
        &agent_id[..agent_id.len().min(8)],
        python_platform(),
        if secret.is_empty() {
            "missing"
        } else {
            "configured"
        }
    ));

    let route_dead = |url: &mut String, streak: &mut u32, last_walk: &mut Option<Instant>| {
        *streak += 1;
        if *streak < ROUTE_REPAIR_THRESHOLD
            || last_walk.is_some_and(|w| w.elapsed() < ROUTE_REPAIR_COOLDOWN)
        {
            return;
        }
        *last_walk = Some(Instant::now());
        if let Some(new_url) = repair_route(url, &options.candidates) {
            if let Some(persist) = &options.persist_route {
                persist(&new_url);
            }
            log::info(&format!("route re-pointed by the repair walk: {new_url}"));
            *url = new_url;
            *streak = 0;
        }
    };

    while !stop.is_set() {
        let (out_results, out_streams) = {
            let inner = runtime.lock();
            (inner.results.prefix(), inner.streams.prefix())
        };
        frame.insert("execution_slots".into(), runtime.available_slots().into());
        frame.insert("capabilities".into(), current(&permissions).capabilities());
        frame.insert(
            "active_command_ids".into(),
            json!(runtime.active_command_ids()),
        );
        if let Some(rtt) = rtt_ms {
            frame.insert("rtt_ms".into(), rtt.into());
        }
        let started = Instant::now();
        let endpoint = format!("{url}{}", contract::transport_path("poll_path"));
        let mut payload_for = |route: &str| {
            frame.insert("route".into(), route.into());
            json!({"results": out_results, "streams": out_streams, "agent": Value::Object(frame.clone())})
        };
        // A present page is preferred over a direct route an SSO edge refuses.
        let relayed = options
            .relay
            .as_ref()
            .filter(|relay| relay.browser_available())
            .and_then(|relay| {
                relay.request(
                    &endpoint,
                    &payload_for("browser_relay"),
                    &secret,
                    RELAY_POLL_TIMEOUT,
                )
            });
        let via_browser = relayed.is_some();
        let sent = match relayed {
            Some(response) => Ok(response),
            None => {
                let headers: Vec<(&str, &str)> = if secret.is_empty() {
                    vec![]
                } else {
                    vec![("X-Bridge-Secret", secret.as_str())]
                };
                http::post_json(&endpoint, &payload_for("direct"), &headers, POLL_TIMEOUT)
            }
        };
        let response = match sent {
            Ok(response) => response,
            Err(error) => {
                consecutive_errors += 1;
                emit(Status::Unreachable, &mut last_status);
                route_dead(&mut url, &mut route_dead_streak, &mut last_walk);
                if consecutive_errors == 1 {
                    log::info(&format!(
                        "Cannot reach server at {url}, retrying... ({error})"
                    ));
                }
                stop.wait(retry_delay(options.poll_interval, consecutive_errors));
                continue;
            }
        };
        let body = response.json();
        if response.status == 401 || response.status == 403 {
            if body.as_ref().is_some_and(is_tofu_error_envelope) {
                emit(Status::Auth, &mut last_status);
                route_dead_streak = 0;
                log::error(&format!("Server returned {} — the device credential is missing, revoked, or belongs to another owner. Pair the device again.", response.status));
                if options.stop_on_refusal {
                    break;
                }
            } else {
                emit(Status::Proxy, &mut last_status);
                route_dead(&mut url, &mut route_dead_streak, &mut last_walk);
                log::error("401 answered by a GATEWAY, not Tofu — the server address is intercepted (SSO proxy?).");
            }
            consecutive_errors += 1;
            stop.wait(retry_delay(options.poll_interval, consecutive_errors));
            continue;
        }
        if response.status == 409 {
            let refusal = body.clone().unwrap_or(Value::Null);
            let error = refusal.get("error").and_then(Value::as_str).unwrap_or("");
            if TERMINAL_REFUSALS.contains(&error) {
                let reason = refusal.get("reason").and_then(Value::as_str).unwrap_or("");
                emit(
                    Status::Conflict {
                        reason: crate::util::truncate_chars(reason, 64),
                    },
                    &mut last_status,
                );
                route_dead_streak = 0;
                log::error(&format!(
                    "The Home refused this node: {error} ({reason}). {}",
                    refusal.get("hint").and_then(Value::as_str).unwrap_or("")
                ));
                if options.stop_on_refusal {
                    break;
                }
                consecutive_errors += 1;
                stop.wait(retry_delay(options.poll_interval, consecutive_errors));
                continue;
            }
        }
        if response.status != 200 {
            emit(Status::Http(response.status), &mut last_status);
            log::info(&format!("Server returned {}", response.status));
            if [301, 302, 303, 307, 308, 408, 502, 503, 504].contains(&response.status) {
                route_dead(&mut url, &mut route_dead_streak, &mut last_walk);
            }
            consecutive_errors += 1;
            stop.wait(retry_delay(options.poll_interval, consecutive_errors));
            continue;
        }
        let Some(data) = body else {
            emit(Status::Proxy, &mut last_status);
            route_dead(&mut url, &mut route_dead_streak, &mut last_walk);
            consecutive_errors += 1;
            stop.wait(retry_delay(options.poll_interval, consecutive_errors));
            continue;
        };
        let commands = match crate::runtime::validate_poll_response(&data, agent_id, negotiated) {
            Ok(commands) => commands,
            Err(PollError::ProtocolMismatch) => {
                emit(Status::Http(426), &mut last_status);
                route_dead_streak = 0;
                consecutive_errors += 1;
                stop.wait(retry_delay(options.poll_interval, consecutive_errors));
                continue;
            }
            Err(PollError::Invalid(why)) => {
                log::warn(&format!("poll response refused: {why}"));
                emit(Status::Proxy, &mut last_status);
                route_dead(&mut url, &mut route_dead_streak, &mut last_walk);
                consecutive_errors += 1;
                stop.wait(retry_delay(options.poll_interval, consecutive_errors));
                continue;
            }
        };
        if data.get("protocol_version").and_then(Value::as_i64) == Some(protocol_version()) {
            negotiated = true;
            runtime.observe_server_time(
                data["server_time"].as_f64().unwrap_or(0.0),
                started.elapsed().as_secs_f64(),
            );
        }
        // Only a 200 acknowledges the exact prefix this poll carried.
        {
            let mut inner = runtime.lock();
            inner.results.acknowledge(out_results.len());
            inner.streams.acknowledge(out_streams.len());
        }
        consecutive_errors = 0;
        *last_contact.lock().unwrap_or_else(|e| e.into_inner()) = Instant::now();
        if !out_results.is_empty() || !out_streams.is_empty() {
            rtt_ms = Some(started.elapsed().as_millis() as u64);
        }
        match data.get("node").filter(|n| n.is_object()) {
            Some(pending) => emit(
                Status::Pending(
                    pending
                        .get("fingerprint")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                ),
                &mut last_status,
            ),
            None => emit(Status::Ok { via_browser }, &mut last_status),
        }
        route_dead_streak = 0;
        if !commands.is_empty() {
            log::info(&format!("Received {} command(s)", commands.len()));
        }
        for cmd in commands {
            let cmd_type = cmd["type"].as_str().unwrap_or("");
            let expires_at = cmd.get("expires_at").and_then(Value::as_f64);
            if contract::required_tier(cmd_type, &json!({})) == Some("control") {
                // Cancellation is a nonblocking control-plane signal, not work.
                if runtime.expired(expires_at) {
                    continue;
                }
                let target = cmd
                    .get("params")
                    .and_then(|p| p.get("cmd_id"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                match cmd_type {
                    "egress_cancel" => {
                        commands::egress::cancel_inflight(target);
                    }
                    "paper_build_cancel" => {
                        if let Some(worker) = executor.paper.get() {
                            worker.cancel(target);
                        }
                    }
                    _ => {}
                }
                continue;
            }
            let deadline_enforced = cmd
                .get("deadline_enforced")
                .and_then(Value::as_bool)
                .unwrap_or(true);
            let cmd_id = cmd["id"].as_str().unwrap_or("").to_string();
            if stop.is_set() || !runtime.admit(&cmd_id, expires_at, deadline_enforced) {
                continue;
            }
            if cmd_type == "paper_build"
                && current(&permissions).allows("exec")
                && !executor.paper_worker().admit(&cmd_id)
            {
                executor.complete(
                    None,
                    json!({"id": cmd_id, "result": null, "error": "Paper build is stopping or already owned"}),
                );
                continue;
            }
            let worker = executor.clone();
            let spawned = std::thread::Builder::new()
                .name("tofu-node-command".into())
                .spawn(move || {
                    let paper = (cmd["type"] == "paper_build")
                        .then(|| cmd["id"].as_str().unwrap_or("").to_string());
                    worker.execute(cmd);
                    // Every exit path (refusal, journal replay, build) frees the admission.
                    if let (Some(id), Some(paper)) = (paper, worker.paper.get()) {
                        paper.release(&id);
                    }
                });
            if let Err(error) = spawned {
                if let Some(paper) = executor.paper.get() {
                    paper.release(&cmd_id);
                }
                executor.complete(
                    None,
                    json!({"id": cmd_id, "result": null, "error": error.to_string()}),
                );
            }
        }
        if !runtime.draining() {
            stop.wait(Duration::from_secs_f64(options.poll_interval.max(0.01)));
        }
    }
    stop.set();
    if let Some(worker) = executor.paper.get() {
        worker.close();
    }
    runtime.close();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_is_a_valid_agent_registration() {
        let frame = agent_frame(
            "node-1",
            &Permissions::from_tiers(&["write"]),
            &[json!({"name": "r", "path": "/tmp"})],
        );
        let schema: Value = contract::bridge().clone();
        let mut registration = schema["$defs"]["AgentRegistration"].clone();
        registration["$defs"] = schema["$defs"].clone();
        let mut frame = Value::Object(frame);
        frame["execution_slots"] = json!(256);
        frame["active_command_ids"] = json!([]);
        frame["route"] = json!("direct");
        frame["rtt_ms"] = json!(12);
        let validator = jsonschema::validator_for(&registration).unwrap();
        let errors: Vec<String> = validator
            .iter_errors(&frame)
            .map(|e| e.to_string())
            .collect();
        assert!(errors.is_empty(), "{errors:?}");
        let system_schema = {
            let mut s = schema["$defs"]["SystemFacts"].clone();
            s["$defs"] = schema["$defs"].clone();
            s
        };
        if let Some(system) = frame.get("system") {
            assert!(jsonschema::validator_for(&system_schema)
                .unwrap()
                .is_valid(system));
        }
        assert_eq!(frame["capabilities"]["write"], json!(true));
        assert_eq!(frame["capabilities"]["exec"], json!(false));
        assert_eq!(boot_id().len(), 32);
    }

    #[test]
    fn tofu_refusals_are_told_apart_from_gateways() {
        assert!(is_tofu_error_envelope(
            &json!({"error": "bridge_auth_required"})
        ));
        assert!(is_tofu_error_envelope(
            &json!({"ok": false, "error": {"code": "x"}})
        ));
        assert!(!is_tofu_error_envelope(&json!({"error": "Unauthorized"})));
    }
}
