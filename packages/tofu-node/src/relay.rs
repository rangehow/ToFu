//! Browser relay: a loopback broker through which a signed-in Tofu page
//! carries this node's polls (and a pairing redeem) past an SSO edge.
//!
//! Port of `lib/desktop_agent/_browser_relay.py`; the page side is
//! `frontend/src/features/devices/relay.ts`. The node binds the first free
//! port of 127.0.0.1:15180–15189; the page discovers it (`GET /v1/status`),
//! long-polls one job (`GET /v1/take`), sends it to the Home with its own
//! cookies and hands the exact status and body back (`POST /v1/result`).
//! Every request must carry an `Origin` of the link's own server, so a page
//! finds only the relay of the server it belongs to; no cookie ever enters
//! this process and the job exposes only the bridge credential.
//! Entry points: [`BrowserRelay::start`], [`BrowserRelay::request`].

use crate::http::Response;
use crate::log;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// The broker port range the page scans (relay.ts BROKER_PORT_FIRST/LAST).
pub const PORT_START: u16 = 15180;
pub const PORT_END: u16 = 15189;
/// Largest relayed body (the Python broker's `_MAX_BODY`).
const MAX_BODY: usize = 2 * 1024 * 1024;
/// A page that contacted the broker this recently is considered present.
const BROWSER_FRESH: Duration = Duration::from_secs(22);
/// How long one `/v1/take` is held open (the page's take deadline is 13 s).
const TAKE_HOLD: Duration = Duration::from_secs(10);
/// Request line / header line bound of Python's `http.server` (`_MAXLINE`).
const MAX_LINE: usize = 65536;
const STATUS_KIND: &str = "tofu-agent-browser-relay";

/// Lower-cased HTTP(S) origin of `url`, else `""` (`origin_of`).
pub fn origin_of(url: &str) -> String {
    let url = url.trim();
    let Some((scheme, rest)) = url.split_once("://") else {
        return String::new();
    };
    let scheme = scheme.to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return String::new();
    }
    let netloc = rest.split(['/', '?', '#']).next().unwrap_or("");
    if netloc.is_empty() {
        return String::new();
    }
    format!("{scheme}://{}", netloc.to_ascii_lowercase())
}

#[derive(Default)]
struct Inner {
    jobs: HashMap<String, Value>,
    pending: VecDeque<String>,
    answers: HashMap<String, Response>,
    closed: bool,
    last_browser: Option<Instant>,
}

struct Shared {
    origins: Vec<String>,
    inner: Mutex<Inner>,
    /// Signalled when a job is queued or the broker closes (take waiters).
    queued: Condvar,
    /// Signalled when a job is answered or the broker closes (request waiters).
    answered: Condvar,
}

impl Shared {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// One link's loopback broker. Dropping it closes it.
pub struct BrowserRelay {
    shared: Arc<Shared>,
    port: u16,
    accepting: Arc<AtomicBool>,
}

impl BrowserRelay {
    /// Bind the first free port of the relay range and serve. `allowed_urls`
    /// are the link's own server routes; only their origins are admitted.
    pub fn start(allowed_urls: &[String]) -> Option<Self> {
        Self::start_on(allowed_urls, PORT_START..=PORT_END)
    }

    /// [`start`](Self::start) over an explicit port range (`0..=0`: any free port).
    pub fn start_on(allowed_urls: &[String], ports: std::ops::RangeInclusive<u16>) -> Option<Self> {
        let mut origins: Vec<String> = allowed_urls
            .iter()
            .map(|u| origin_of(u))
            .filter(|o| !o.is_empty())
            .collect();
        origins.dedup();
        let (first, last) = (*ports.start(), *ports.end());
        let listener = ports
            .into_iter()
            .find_map(|port| TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST, port))).ok());
        let Some(listener) = listener else {
            log::warn(&format!(
                "Browser relay could not bind ports {first}-{last}"
            ));
            return None;
        };
        let port = listener.local_addr().ok()?.port();
        let shared = Arc::new(Shared {
            origins,
            inner: Mutex::new(Inner::default()),
            queued: Condvar::new(),
            answered: Condvar::new(),
        });
        let accepting = Arc::new(AtomicBool::new(true));
        let serve_shared = shared.clone();
        let serve_accepting = accepting.clone();
        std::thread::Builder::new()
            .name("tofu-node-browser-relay".into())
            .spawn(move || {
                for stream in listener.incoming() {
                    if !serve_accepting.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(stream) = stream else { continue };
                    let shared = serve_shared.clone();
                    let _ = std::thread::Builder::new()
                        .name("tofu-node-browser-relay-conn".into())
                        .spawn(move || serve(&shared, port, stream));
                }
            })
            .ok()?;
        log::info(&format!("Browser relay listening on 127.0.0.1:{port}"));
        Some(Self {
            shared,
            port,
            accepting,
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// Whether a page contacted the broker recently.
    pub fn browser_available(&self) -> bool {
        let inner = self.shared.lock();
        !inner.closed
            && inner
                .last_browser
                .is_some_and(|seen| seen.elapsed() < BROWSER_FRESH)
    }

    /// Ask the signed-in page to perform one POST. `None` when no page is
    /// present or it does not answer within `timeout`; the caller then uses
    /// its direct route.
    pub fn request(
        &self,
        url: &str,
        payload: &Value,
        bridge_secret: &str,
        timeout: Duration,
    ) -> Option<Response> {
        if !self.browser_available() {
            return None;
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let mut headers = Map::new();
        if !bridge_secret.trim().is_empty() {
            headers.insert("X-Bridge-Secret".into(), bridge_secret.trim().into());
        }
        let job = json!({"id": id, "url": url, "payload": payload, "headers": headers});
        let mut inner = self.shared.lock();
        if inner.closed {
            return None;
        }
        inner.jobs.insert(id.clone(), job);
        inner.pending.push_back(id.clone());
        self.shared.queued.notify_all();
        let deadline = Instant::now() + timeout.max(Duration::from_millis(100));
        loop {
            if let Some(answer) = inner.answers.remove(&id) {
                return Some(answer);
            }
            let now = Instant::now();
            if inner.closed || now >= deadline {
                inner.jobs.remove(&id);
                inner.pending.retain(|p| p != &id);
                return None;
            }
            inner = self
                .shared
                .answered
                .wait_timeout(inner, deadline - now)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }

    pub fn close(&self) {
        {
            let mut inner = self.shared.lock();
            inner.closed = true;
            inner.last_browser = None;
            inner.jobs.clear();
            inner.pending.clear();
            inner.answers.clear();
        }
        self.shared.queued.notify_all();
        self.shared.answered.notify_all();
        if self.accepting.swap(false, Ordering::SeqCst) {
            // Wake the accept loop so it sees the flag and drops the listener.
            let _ = TcpStream::connect_timeout(
                &SocketAddr::from((Ipv4Addr::LOCALHOST, self.port)),
                Duration::from_secs(1),
            );
        }
    }
}

impl Drop for BrowserRelay {
    fn drop(&mut self) {
        self.close();
    }
}

fn take(shared: &Shared) -> Option<Value> {
    let deadline = Instant::now() + TAKE_HOLD;
    let mut inner = shared.lock();
    loop {
        if inner.closed {
            return None;
        }
        while let Some(id) = inner.pending.pop_front() {
            if let Some(job) = inner.jobs.get(&id) {
                return Some(job.clone());
            }
        }
        let now = Instant::now();
        if now >= deadline {
            return None;
        }
        inner = shared
            .queued
            .wait_timeout(inner, deadline - now)
            .unwrap_or_else(|e| e.into_inner())
            .0;
    }
}

fn complete(shared: &Shared, id: &str, status: &Value, body: &str) -> bool {
    let Some(code) = status
        .as_i64()
        .or_else(|| status.as_str().and_then(|s| s.trim().parse().ok()))
    else {
        return false;
    };
    if !(100..=599).contains(&code) || body.len() > MAX_BODY {
        return false;
    }
    let mut inner = shared.lock();
    if inner.jobs.remove(id).is_none() {
        return false;
    }
    inner.pending.retain(|p| p != id);
    inner.answers.insert(
        id.to_string(),
        Response {
            status: code as u16,
            body: body.as_bytes().to_vec(),
        },
    );
    shared.answered.notify_all();
    true
}

struct Request {
    method: String,
    path: String,
    origin: String,
    body: Vec<u8>,
    content_length: Option<usize>,
}

fn read_line(reader: &mut impl BufRead) -> Option<String> {
    let mut line = Vec::new();
    reader
        .by_ref()
        .take(MAX_LINE as u64 + 1)
        .read_until(b'\n', &mut line)
        .ok()?;
    if line.len() > MAX_LINE || line.is_empty() {
        return None;
    }
    Some(String::from_utf8_lossy(&line).trim_end().to_string())
}

fn read_request(stream: &TcpStream) -> Option<Request> {
    let mut reader = BufReader::new(stream);
    let request_line = read_line(&mut reader)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next()?.to_string();
    let path = parts.next()?.to_string();
    let mut origin = String::new();
    let mut content_length = None;
    loop {
        let line = read_line(&mut reader)?;
        if line.is_empty() {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            match name.trim().to_ascii_lowercase().as_str() {
                "origin" => origin = value.trim().to_ascii_lowercase(),
                "content-length" => content_length = value.trim().parse::<usize>().ok(),
                _ => {}
            }
        }
    }
    let mut body = Vec::new();
    if let Some(size) = content_length.filter(|s| (1..=MAX_BODY).contains(s)) {
        body.resize(size, 0);
        reader.read_exact(&mut body).ok()?;
    }
    Some(Request {
        method,
        path,
        origin,
        body,
        content_length,
    })
}

fn respond(mut stream: &TcpStream, status: u16, origin: &str, body: Option<&Value>) {
    let reason = match status {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "",
    };
    let mut head =
        format!("HTTP/1.1 {status} {reason}\r\nServer: TofuNodeRelay/1\r\nConnection: close\r\n");
    if !origin.is_empty() {
        head.push_str(&format!(
            "Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n\
             Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n\
             Access-Control-Allow-Headers: Content-Type\r\n\
             Access-Control-Allow-Private-Network: true\r\n"
        ));
    }
    if status != 403 {
        head.push_str("Cache-Control: no-store\r\n");
    }
    let payload = body
        .map(|b| serde_json::to_vec(b).unwrap_or_default())
        .unwrap_or_default();
    if body.is_some() {
        head.push_str("Content-Type: application/json; charset=utf-8\r\n");
    }
    head.push_str(&format!("Content-Length: {}\r\n\r\n", payload.len()));
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(&payload);
    let _ = stream.flush();
}

fn serve(shared: &Shared, port: u16, stream: TcpStream) {
    let Some(request) = read_request(&stream) else {
        return;
    };
    // Never log request details: polls carry the bridge credential.
    if request.origin.is_empty() || !shared.origins.contains(&request.origin) {
        return respond(&stream, 403, "", None);
    }
    shared.lock().last_browser = Some(Instant::now());
    let origin = request.origin.as_str();
    match (request.method.as_str(), request.path.as_str()) {
        ("OPTIONS", _) => respond(&stream, 204, origin, None),
        ("GET", "/v1/status") => {
            let pending = shared.lock().jobs.len();
            let body = json!({"kind": STATUS_KIND, "version": 1, "port": port, "pending": pending});
            respond(&stream, 200, origin, Some(&body));
        }
        ("GET", "/v1/take") => match take(shared) {
            Some(job) => respond(&stream, 200, origin, Some(&job)),
            None => respond(&stream, 204, origin, None),
        },
        ("POST", "/v1/result") => {
            let data = request
                .content_length
                .filter(|s| (1..=MAX_BODY).contains(s))
                .and_then(|_| serde_json::from_slice::<Value>(&request.body).ok())
                .filter(Value::is_object);
            let Some(data) = data else {
                return respond(&stream, 400, origin, None);
            };
            let id = match data.get("id") {
                Some(Value::String(s)) => s.clone(),
                _ => String::new(),
            };
            let body = match data.get("body") {
                Some(Value::String(s)) => s.clone(),
                _ => String::new(),
            };
            let accepted = complete(
                shared,
                &id,
                data.get("status").unwrap_or(&Value::Null),
                &body,
            );
            respond(
                &stream,
                if accepted { 200 } else { 404 },
                origin,
                Some(&json!({"accepted": accepted})),
            );
        }
        _ => respond(&stream, 404, origin, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PAGE: &str = "https://abc-vscode.example.com";

    fn relay() -> BrowserRelay {
        BrowserRelay::start_on(
            &["https://abc-vscode.example.com/proxy/15000/".to_string()],
            0..=0,
        )
        .expect("relay binds")
    }

    fn call(port: u16, method: &str, path: &str, origin: &str, body: &str) -> (u16, String) {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let origin = if origin.is_empty() {
            String::new()
        } else {
            format!("Origin: {origin}\r\n")
        };
        write!(
            stream,
            "{method} {path} HTTP/1.1\r\nHost: x\r\n{origin}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut text = String::new();
        stream.read_to_string(&mut text).unwrap();
        let status = text[9..12].parse().unwrap();
        let body = text
            .split_once("\r\n\r\n")
            .map(|(_, b)| b.to_string())
            .unwrap_or_default();
        (status, body)
    }

    #[test]
    fn origin_keeps_scheme_and_host_only() {
        assert_eq!(
            origin_of("https://ABC.example.com/proxy/15000/"),
            "https://abc.example.com"
        );
        assert_eq!(origin_of("http://127.0.0.1:8/x?y#z"), "http://127.0.0.1:8");
        assert_eq!(origin_of("javascript:alert(1)"), "");
    }

    #[test]
    fn only_the_links_origin_is_admitted() {
        let relay = relay();
        assert_eq!(
            call(
                relay.port(),
                "GET",
                "/v1/status",
                "https://evil.example",
                ""
            )
            .0,
            403
        );
        assert_eq!(call(relay.port(), "GET", "/v1/status", "", "").0, 403);
        assert!(!relay.browser_available());
        let (status, body) = call(relay.port(), "GET", "/v1/status", PAGE, "");
        assert_eq!(status, 200);
        let body: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(body["kind"], json!(STATUS_KIND));
        assert!(relay.browser_available());
        assert_eq!(call(relay.port(), "POST", "/v1/attach", PAGE, "{}").0, 404);
    }

    #[test]
    fn a_page_round_trip_carries_only_the_bridge_secret() {
        let relay = Arc::new(relay());
        let port = relay.port();
        assert_eq!(call(port, "GET", "/v1/status", PAGE, "").0, 200);
        let agent = relay.clone();
        let poll = std::thread::spawn(move || {
            agent.request(
                "https://abc-vscode.example.com/proxy/15000/api/desktop/poll",
                &json!({"results": []}),
                "tofu_live_test",
                Duration::from_secs(5),
            )
        });
        let (status, job) = call(port, "GET", "/v1/take", PAGE, "");
        assert_eq!(status, 200);
        let job: Value = serde_json::from_str(&job).unwrap();
        assert_eq!(job["headers"], json!({"X-Bridge-Secret": "tofu_live_test"}));
        let result =
            json!({"id": job["id"], "status": 200, "body": "{\"commands\":[]}"}).to_string();
        let (status, accepted) = call(port, "POST", "/v1/result", PAGE, &result);
        assert_eq!((status, accepted.as_str()), (200, "{\"accepted\":true}"));
        let answer = poll.join().unwrap().expect("answered");
        assert_eq!(answer.status, 200);
        assert_eq!(answer.json(), Some(json!({"commands": []})));
        // A completed (or unknown) job cannot be answered twice.
        assert_eq!(call(port, "POST", "/v1/result", PAGE, &result).0, 404);
    }

    #[test]
    fn an_unanswered_job_expires_and_is_never_handed_out() {
        let relay = relay();
        assert_eq!(call(relay.port(), "GET", "/v1/status", PAGE, "").0, 200);
        assert!(relay
            .request(
                "https://abc-vscode.example.com/x",
                &json!({}),
                "",
                Duration::from_millis(50)
            )
            .is_none());
        assert!(relay.shared.lock().pending.is_empty());
        relay.close();
        assert!(!relay.browser_available());
    }
}
