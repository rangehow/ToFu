//! Loopback readiness probe (`probe_application_readiness`).
//!
//! `/api/ready` carries the worker PID in current generations; `/api/health`
//! is the identity-liveness fallback. A worker served over TLS
//! (`.last_serve_mode == https`, `TOFU_TLS=1`) is probed over HTTPS with its
//! certificate unverified, exactly like Python's
//! `ssl._create_unverified_context()`: the peer is this host's own loopback
//! worker with a self-signed development certificate, and identity comes from
//! the PID in the payload, not from the certificate.

use crate::json::Object;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

pub const HTTP_PROBE_RESPONSE_LIMIT: usize = 64 * 1024;

fn decode_chunked(mut body: &[u8]) -> Result<Vec<u8>, String> {
    let mut out = Vec::new();
    loop {
        let line_end = body
            .windows(2)
            .position(|w| w == b"\r\n")
            .ok_or("malformed chunked body")?;
        let size_text = String::from_utf8_lossy(&body[..line_end]);
        let size = usize::from_str_radix(size_text.split(';').next().unwrap_or("").trim(), 16)
            .map_err(|_| "malformed chunk size")?;
        body = &body[line_end + 2..];
        if size == 0 {
            return Ok(out);
        }
        if body.len() < size {
            return Err("truncated chunked body".into());
        }
        out.extend_from_slice(&body[..size]);
        body = body.get(size + 2..).unwrap_or(&[]);
    }
}

#[derive(Debug)]
struct UnverifiedLoopbackCertificate(Arc<rustls::crypto::CryptoProvider>);

impl rustls::client::danger::ServerCertVerifier for UnverifiedLoopbackCertificate {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &rustls::DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

fn tls_config() -> Result<Arc<rustls::ClientConfig>, String> {
    static CONFIG: OnceLock<Result<Arc<rustls::ClientConfig>, String>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let provider = Arc::new(rustls::crypto::ring::default_provider());
            let config = rustls::ClientConfig::builder_with_provider(provider.clone())
                .with_safe_default_protocol_versions()
                .map_err(|e| e.to_string())?
                .dangerous()
                .with_custom_certificate_verifier(Arc::new(UnverifiedLoopbackCertificate(provider)))
                .with_no_client_auth();
            Ok(Arc::new(config))
        })
        .clone()
}

/// One bounded GET on loopback over plain HTTP; `(status, JSON object)`.
pub fn get_json(port: u16, path: &str, timeout: f64) -> Result<(u16, Object), String> {
    get_json_over(port, path, timeout, "http")
}

/// One bounded GET on loopback over `scheme` (`http` or `https`).
pub fn get_json_over(
    port: u16,
    path: &str,
    timeout: f64,
    scheme: &str,
) -> Result<(u16, Object), String> {
    let address: SocketAddr = ([127, 0, 0, 1], port).into();
    let duration = Duration::from_secs_f64(timeout.max(0.05));
    let stream = TcpStream::connect_timeout(&address, duration).map_err(|e| e.to_string())?;
    let _ = stream.set_nodelay(true);
    stream
        .set_read_timeout(Some(duration))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(duration))
        .map_err(|e| e.to_string())?;
    if scheme == "https" {
        let connection = rustls::ClientConnection::new(
            tls_config()?,
            ServerName::IpAddress(std::net::Ipv4Addr::LOCALHOST.into()),
        )
        .map_err(|e| e.to_string())?;
        return exchange(rustls::StreamOwned::new(connection, stream), port, path);
    }
    exchange(stream, port, path)
}

fn exchange<S: Read + Write>(
    mut stream: S,
    port: u16,
    path: &str,
) -> Result<(u16, Object), String> {
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    let mut buffer = [0u8; 8192];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) => break,
            // A TLS peer that closes without close_notify ends the response too.
            Err(error) if error.kind() == std::io::ErrorKind::UnexpectedEof && !raw.is_empty() => {
                break
            }
            Ok(read) => {
                raw.extend_from_slice(&buffer[..read]);
                if raw.len() > HTTP_PROBE_RESPONSE_LIMIT + 16 * 1024 {
                    return Err(format!(
                        "probe response exceeds {HTTP_PROBE_RESPONSE_LIMIT} bytes"
                    ));
                }
            }
            Err(error) => return Err(error.to_string()),
        }
    }
    let header_end = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("probe response has no header terminator")?;
    let head = String::from_utf8_lossy(&raw[..header_end]).into_owned();
    let status: u16 = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .ok_or("probe response has no status line")?;
    let chunked = head.lines().any(|line| {
        line.to_ascii_lowercase().starts_with("transfer-encoding:")
            && line.to_ascii_lowercase().contains("chunked")
    });
    let mut body = raw[header_end + 4..].to_vec();
    if chunked {
        body = decode_chunked(&body)?;
    }
    if body.len() > HTTP_PROBE_RESPONSE_LIMIT {
        return Err(format!(
            "probe response exceeds {HTTP_PROBE_RESPONSE_LIMIT} bytes"
        ));
    }
    let text = String::from_utf8(body).map_err(|e| e.to_string())?;
    let text = if text.trim().is_empty() {
        "{}".to_owned()
    } else {
        text
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(map)) => Ok((status, map)),
        Ok(_) => Err("probe response must be a JSON object".into()),
        Err(error) => Err(error.to_string()),
    }
}

fn readiness_failure_detail(status: u16, payload: &Object) -> String {
    let (mut code, mut message) = (String::new(), String::new());
    match payload.get("error") {
        Some(Value::Object(error)) => {
            code = error
                .get("code")
                .map(value_text)
                .unwrap_or_default()
                .trim()
                .to_owned();
            message = error
                .get("message")
                .map(value_text)
                .unwrap_or_default()
                .trim()
                .to_owned();
        }
        Some(value) if crate::json::truthy(Some(value)) => {
            message = value_text(value).trim().to_owned()
        }
        _ => {}
    }
    let top = payload
        .get("message")
        .filter(|v| crate::json::truthy(Some(v)))
        .map(value_text);
    let message = top.unwrap_or(message).trim().to_owned();
    let state = payload
        .get("state")
        .filter(|v| crate::json::truthy(Some(v)))
        .map(value_text)
        .unwrap_or_else(|| "unknown".into());
    let storage_state = match payload.get("storage") {
        Some(Value::Object(storage)) => storage
            .get("state")
            .filter(|v| crate::json::truthy(Some(v)))
            .map(value_text)
            .unwrap_or_else(|| "unknown".into()),
        _ => "unknown".into(),
    };
    let reason = [code, message]
        .into_iter()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(": ");
    let suffix = format!(
        "lifecycle={}, storage={}, HTTP {status}",
        state.trim(),
        storage_state.trim()
    );
    if reason.is_empty() {
        suffix
    } else {
        format!("{reason}; {suffix}")
    }
}

fn value_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn settle(result: &mut Object, scheme: &str, port: u16, status: u16, payload: &Object) {
    let ready = (200..300).contains(&status)
        && payload.get("ok") == Some(&json!(true))
        && payload.get("ready") == Some(&json!(true));
    result.insert("ready".into(), json!(ready));
    result.insert(
        "readinessState".into(),
        payload.get("state").cloned().unwrap_or(Value::Null),
    );
    result.insert(
        "storageState".into(),
        payload
            .get("storage")
            .and_then(|s| s.get("state"))
            .cloned()
            .unwrap_or(Value::Null),
    );
    if ready {
        result.insert("url".into(), json!(format!("{scheme}://localhost:{port}")));
        return;
    }
    let detail = format!(
        "application readiness failed: {}",
        readiness_failure_detail(status, payload)
    );
    result.insert("readinessError".into(), json!(detail));
    result.insert("error".into(), json!(detail));
}

pub fn probe_application_readiness(
    port: u16,
    expected_pid: Option<i64>,
    preferred_scheme: &str,
    timeout: f64,
) -> Object {
    let mut result = json!({
        "health": false, "liveness": false, "ready": false, "scheme": null, "url": null,
        "liveUrl": null, "payloadPid": null, "pidMatches": false, "livenessError": "",
        "readinessError": "", "readinessState": null, "storageState": null,
    })
    .as_object()
    .unwrap()
    .clone();
    let Some(expected_pid) = expected_pid else {
        result.insert(
            "livenessError".into(),
            json!("locked worker PID is unavailable"),
        );
        result.insert("error".into(), json!("locked worker PID is unavailable"));
        return result;
    };
    let mut schemes = Vec::new();
    if preferred_scheme == "http" || preferred_scheme == "https" {
        schemes.push(preferred_scheme);
    }
    for scheme in ["http", "https"] {
        if !schemes.contains(&scheme) {
            schemes.push(scheme);
        }
    }
    let mut last_error = String::new();
    for scheme in schemes {
        let (ready_reply, ready_error) = match get_json_over(port, "/api/ready", timeout, scheme) {
            Ok(reply) => (Some(reply), String::new()),
            Err(error) => (None, format!("{scheme} readiness probe failed: {error}")),
        };
        let live = |result: &mut Object, pid: &Value| {
            result.insert("health".into(), json!(true));
            result.insert("liveness".into(), json!(true));
            result.insert("scheme".into(), json!(scheme));
            result.insert(
                "liveUrl".into(),
                json!(format!("{scheme}://localhost:{port}")),
            );
            result.insert("payloadPid".into(), pid.clone());
            result.insert("pidMatches".into(), json!(true));
            result.insert("livenessError".into(), json!(""));
        };
        if let Some((status, payload)) = &ready_reply {
            if let Some(pid) = payload.get("pid").filter(|pid| !pid.is_null()) {
                if pid.as_i64() != Some(expected_pid) {
                    last_error = format!(
                        "readiness response PID does not match the locked worker (expected {expected_pid}, got {pid})"
                    );
                    continue;
                }
                live(&mut result, pid);
                settle(&mut result, scheme, port, *status, payload);
                return result;
            }
        }
        let (health_status, health) = match get_json_over(port, "/api/health", timeout, scheme) {
            Ok(reply) => reply,
            Err(error) => {
                last_error = format!("{scheme} identity liveness probe failed: {error}");
                continue;
            }
        };
        let payload_pid = health.get("pid").cloned().unwrap_or(Value::Null);
        let pid_matches = payload_pid.as_i64() == Some(expected_pid);
        if !((200..300).contains(&health_status)
            && health.get("ok") == Some(&json!(true))
            && pid_matches)
        {
            last_error = if !pid_matches {
                format!(
                    "identity liveness response PID does not match the locked worker (expected {expected_pid}, got {})",
                    if payload_pid.is_null() { "None".to_owned() } else { payload_pid.to_string() }
                )
            } else {
                format!("identity liveness probe returned HTTP {health_status} without ok=true")
            };
            continue;
        }
        live(&mut result, &payload_pid);
        match &ready_reply {
            None => {
                let detail = if ready_error.is_empty() {
                    format!("{scheme} readiness probe failed")
                } else {
                    ready_error
                };
                result.insert("readinessError".into(), json!(detail));
                result.insert("error".into(), json!(detail));
            }
            Some((status, payload)) => settle(&mut result, scheme, port, *status, payload),
        }
        return result;
    }
    let error = if last_error.is_empty() {
        "identity liveness probe failed".to_owned()
    } else {
        last_error
    };
    result.insert("livenessError".into(), json!(error));
    result.insert("error".into(), json!(error));
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    fn serve_once(listener: TcpListener, responses: Vec<String>) {
        std::thread::spawn(move || {
            for response in responses {
                if let Ok((mut stream, _)) = listener.accept() {
                    let mut buffer = [0u8; 1024];
                    let _ = stream.read(&mut buffer);
                    let _ = stream.write_all(response.as_bytes());
                }
            }
        });
    }

    fn reply(body: &str) -> String {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        )
    }

    #[test]
    fn ready_payload_with_pid_is_one_probe() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        serve_once(
            listener,
            vec![reply(
                "{\"ok\":true,\"ready\":true,\"pid\":42,\"state\":\"ready\"}",
            )],
        );
        let result = probe_application_readiness(port, Some(42), "http", 2.0);
        assert_eq!(result["liveness"], json!(true));
        assert_eq!(result["ready"], json!(true));
        assert_eq!(result["url"], json!(format!("http://localhost:{port}")));
    }

    #[test]
    fn unready_worker_is_live_with_a_readiness_error() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        serve_once(
            listener,
            vec![reply("{\"ok\":false,\"ready\":false,\"state\":\"booting\",\"storage\":{\"state\":\"opening\"}}"), reply("{\"ok\":true,\"pid\":7}")],
        );
        let result = probe_application_readiness(port, Some(7), "http", 2.0);
        assert_eq!(result["liveness"], json!(true));
        assert_eq!(result["ready"], json!(false));
        assert_eq!(
            result["readinessError"],
            json!("application readiness failed: lifecycle=booting, storage=opening, HTTP 200")
        );
    }
}
