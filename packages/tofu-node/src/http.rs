//! Outbound HTTP for the node: JSON POST/GET with the process proxy settings.
//!
//! Every connection is node-to-server. Like the Python node (Requests), the
//! node honours `HTTP(S)_PROXY`/`ALL_PROXY` and `NO_PROXY` (host suffixes,
//! IPv4 CIDRs, `*`), never follows a redirect (a gateway redirect is a route
//! fact, not a destination), and never attaches ambient credentials.

use std::collections::HashMap;
use std::io::Read;
use std::net::Ipv4Addr;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

#[derive(Debug)]
pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Response {
    pub fn json(&self) -> Option<serde_json::Value> {
        serde_json::from_slice(&self.body).ok()
    }
}

#[derive(Debug)]
pub enum HttpError {
    Timeout,
    Unreachable(String),
}

impl std::fmt::Display for HttpError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HttpError::Timeout => write!(f, "timed out"),
            HttpError::Unreachable(detail) => write!(f, "unreachable: {detail}"),
        }
    }
}

fn env_first(names: &[&str]) -> Option<String> {
    names
        .iter()
        .filter_map(|n| std::env::var(n).ok())
        .map(|v| v.trim().to_string())
        .find(|v| !v.is_empty())
}

fn split_host(url: &str) -> (String, String, Option<u16>) {
    let (scheme, rest) = url.split_once("://").unwrap_or(("http", url));
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let authority = authority.rsplit('@').next().unwrap_or(authority);
    let (host, port) = if let Some(stripped) = authority.strip_prefix('[') {
        let (h, tail) = stripped.split_once(']').unwrap_or((stripped, ""));
        (
            h.to_string(),
            tail.strip_prefix(':').and_then(|p| p.parse().ok()),
        )
    } else {
        match authority.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().ok()),
            None => (authority.to_string(), None),
        }
    };
    (scheme.to_ascii_lowercase(), host.to_ascii_lowercase(), port)
}

fn cidr_contains(cidr: &str, host: &str) -> bool {
    let Some((net, bits)) = cidr.split_once('/') else {
        return false;
    };
    let (Ok(net), Ok(bits), Ok(ip)) = (
        net.parse::<Ipv4Addr>(),
        bits.parse::<u32>(),
        host.parse::<Ipv4Addr>(),
    ) else {
        return false;
    };
    if bits > 32 {
        return false;
    }
    let mask = if bits == 0 {
        0
    } else {
        u32::MAX << (32 - bits)
    };
    (u32::from(net) & mask) == (u32::from(ip) & mask)
}

fn bypassed(host: &str, port: Option<u16>, no_proxy: &str) -> bool {
    no_proxy
        .split(',')
        .map(str::trim)
        .filter(|e| !e.is_empty())
        .any(|entry| {
            if entry == "*" {
                return true;
            }
            if entry.contains('/') {
                return cidr_contains(entry, host);
            }
            let entry = entry.to_ascii_lowercase();
            let (name, entry_port) = match entry.rsplit_once(':') {
                Some((n, p)) if p.chars().all(|c| c.is_ascii_digit()) => {
                    (n.to_string(), p.parse::<u16>().ok())
                }
                _ => (entry.clone(), None),
            };
            if entry_port.is_some() && entry_port != port {
                return false;
            }
            let name = name.trim_start_matches('.');
            host == name || host.ends_with(&format!(".{name}"))
        })
}

/// The proxy for `url` per the process environment, or None for direct.
pub fn proxy_for(url: &str) -> Option<String> {
    let (scheme, host, port) = split_host(url);
    let no_proxy = env_first(&["no_proxy", "NO_PROXY"]).unwrap_or_default();
    if bypassed(&host, port, &no_proxy) {
        return None;
    }
    match scheme.as_str() {
        "https" => env_first(&["https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]),
        _ => env_first(&["http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"]),
    }
}

fn build_agent(proxy: Option<&str>) -> Result<ureq::Agent, String> {
    let mut builder = ureq::AgentBuilder::new().redirects(0).no_delay(true);
    if let Some(proxy) = proxy {
        builder =
            builder.proxy(ureq::Proxy::new(proxy).map_err(|e| format!("proxy {proxy}: {e}"))?);
    }
    #[cfg(not(target_os = "linux"))]
    {
        let connector = native_tls::TlsConnector::new().map_err(|e| format!("tls: {e}"))?;
        builder = builder.tls_connector(std::sync::Arc::new(connector));
    }
    Ok(builder.build())
}

fn agent_for(url: &str) -> Result<ureq::Agent, HttpError> {
    static AGENTS: OnceLock<Mutex<HashMap<Option<String>, ureq::Agent>>> = OnceLock::new();
    let proxy = proxy_for(url);
    let mut agents = AGENTS
        .get_or_init(Default::default)
        .lock()
        .unwrap_or_else(|e| e.into_inner());
    if let Some(agent) = agents.get(&proxy) {
        return Ok(agent.clone());
    }
    let agent = build_agent(proxy.as_deref()).map_err(HttpError::Unreachable)?;
    agents.insert(proxy, agent.clone());
    Ok(agent)
}

fn finish(result: Result<ureq::Response, ureq::Error>) -> Result<Response, HttpError> {
    let response = match result {
        Ok(response) => response,
        Err(ureq::Error::Status(_, response)) => response,
        Err(ureq::Error::Transport(transport)) => {
            let text = transport.to_string();
            if text.contains("timed out") || text.contains("Timeout") {
                return Err(HttpError::Timeout);
            }
            return Err(HttpError::Unreachable(text));
        }
    };
    let status = response.status();
    let mut body = Vec::new();
    response.into_reader().read_to_end(&mut body).map_err(|e| {
        if e.kind() == std::io::ErrorKind::TimedOut {
            HttpError::Timeout
        } else {
            HttpError::Unreachable(e.to_string())
        }
    })?;
    Ok(Response { status, body })
}

pub fn post_json(
    url: &str,
    body: &serde_json::Value,
    headers: &[(&str, &str)],
    timeout: Duration,
) -> Result<Response, HttpError> {
    let bytes = serde_json::to_vec(body).map_err(|e| HttpError::Unreachable(e.to_string()))?;
    let mut request = agent_for(url)?
        .post(url)
        .timeout(timeout)
        .set("Content-Type", "application/json")
        .set("Accept", "application/json");
    for (name, value) in headers {
        request = request.set(name, value);
    }
    finish(request.send_bytes(&bytes))
}

pub fn get(url: &str, timeout: Duration) -> Result<Response, HttpError> {
    finish(
        agent_for(url)?
            .get(url)
            .timeout(timeout)
            .set("Accept", "application/json")
            .call(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_proxy_matches_suffixes_ports_and_cidrs() {
        let list = "localhost,127.0.0.1,.internal.example.com,10.0.0.0/8,example.org:8443";
        assert!(bypassed("localhost", None, list));
        assert!(bypassed("a.internal.example.com", None, list));
        assert!(bypassed("internal.example.com", None, list));
        assert!(!bypassed("notexample_corp.com", None, list));
        assert!(bypassed("10.2.3.4", None, list));
        assert!(!bypassed("11.2.3.4", None, list));
        assert!(bypassed("example.org", Some(8443), list));
        assert!(!bypassed("example.org", Some(443), list));
        assert!(bypassed("anything", None, "*"));
        assert!(!bypassed("anything", None, ""));
    }

    #[test]
    fn hosts_are_split_from_urls() {
        assert_eq!(
            split_host("http://h.example:8080/p?q"),
            ("http".into(), "h.example".into(), Some(8080))
        );
        assert_eq!(
            split_host("https://[::1]:9/x"),
            ("https".into(), "::1".into(), Some(9))
        );
    }
}
