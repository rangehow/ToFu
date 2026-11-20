//! Pairing links, join input and attachment migration (desktop_node_v1), and
//! joining a Home by redeeming a one-time pairing.
//!
//! Port of `lib/desktop_wire/node_protocol.py` (link grammar, fingerprint,
//! attachment ids, legacy `remote_server` migration) and
//! `lib/desktop_agent/attachments.py::join`. When no route answers directly (an
//! SSO edge only the owner's signed-in browser passes), the node opens its
//! loopback browser relay for the link's origins and the open Local Control
//! page redeems the pairing on its behalf (`src/relay.rs`).

use crate::config;
use crate::contract;
use crate::host_identity;
use crate::http;
use crate::relay::BrowserRelay;
use crate::util::{python_platform, sha256_hex, truncate_chars, unix_now, Stop};
use serde_json::{json, Map, Value};
use std::time::Duration;

#[derive(Debug, PartialEq, Eq)]
pub struct InputError(pub &'static str);

#[derive(Debug, PartialEq, Eq, Clone)]
pub struct PairingLink {
    pub server_url: String,
    pub secret: String,
    pub candidates: Vec<String>,
}

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum JoinInput {
    Pairing(PairingLink),
    LegacyConnectLine { server_url: String, secret: String },
}

struct Split<'a> {
    scheme: &'a str,
    netloc: &'a str,
    path: &'a str,
}

/// `urllib.parse.urlsplit` for the parts the base needs.
fn urlsplit(url: &str) -> Split<'_> {
    let mut scheme = "";
    let mut rest = url;
    if let Some(colon) = url.find(':') {
        let candidate = &url[..colon];
        if !candidate.is_empty()
            && candidate
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            && candidate
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c))
        {
            scheme = candidate;
            rest = &url[colon + 1..];
        }
    }
    let rest = rest.split('#').next().unwrap_or("");
    let rest = rest.split('?').next().unwrap_or("");
    let (netloc, path) = match rest.strip_prefix("//") {
        Some(after) => match after.find('/') {
            Some(i) => (&after[..i], &after[i..]),
            None => (after, ""),
        },
        None => ("", rest),
    };
    Split {
        scheme,
        netloc,
        path,
    }
}

fn hostname_of(netloc: &str) -> Result<String, InputError> {
    let host = netloc.rsplit('@').next().unwrap_or(netloc);
    if let Some(inner) = host.strip_prefix('[') {
        let Some((h, _)) = inner.split_once(']') else {
            return Err(InputError("bad_url"));
        };
        return Ok(h.to_ascii_lowercase());
    }
    if host.contains(']') {
        return Err(InputError("bad_url"));
    }
    Ok(host.split(':').next().unwrap_or("").to_ascii_lowercase())
}

/// Normalize a server base: scheme+host+path, no query/fragment/trailing '/'.
pub fn http_base(value: &str) -> Result<String, InputError> {
    let parts = urlsplit(value.trim());
    let scheme = parts.scheme.to_ascii_lowercase();
    if !(scheme == "http" || scheme == "https") || hostname_of(parts.netloc)?.is_empty() {
        return Err(InputError("bad_url"));
    }
    if parts.netloc.contains('@') {
        return Err(InputError("bad_url"));
    }
    Ok(format!(
        "{scheme}://{}{}",
        parts.netloc,
        parts.path.trim_end_matches('/')
    ))
}

fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3])
                    .ok()
                    .and_then(|h| u8::from_str_radix(h, 16).ok());
                match hex {
                    Some(b) => {
                        out.push(b);
                        i += 3;
                    }
                    None => {
                        out.push(b'%');
                        i += 1;
                    }
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `parse_qsl(text, keep_blank_values=True)`.
fn parse_qsl(text: &str) -> Vec<(String, String)> {
    text.split('&')
        .filter(|f| !f.is_empty())
        .map(|field| match field.split_once('=') {
            Some((k, v)) => (percent_decode(k), percent_decode(v)),
            None => (percent_decode(field), String::new()),
        })
        .collect()
}

/// Parse a pasted pairing link; tolerates whitespace, a missing `/` before `#`
/// and an SPA route in front of the pairing key (`#/chat&tofu-pair=…`).
pub fn parse_pairing_link(text: &str) -> Result<PairingLink, InputError> {
    let fragment_key = contract::pairing_str("fragment_key");
    let via_key = contract::pairing_str("via_key");
    let raw = text.trim();
    let Some((head, fragment)) = raw.split_once('#') else {
        return Err(InputError("not_a_pairing_link"));
    };
    let fields = parse_qsl(fragment.replace('?', "&").trim_start_matches('/'));
    let secrets: Vec<&String> = fields
        .iter()
        .filter(|(k, _)| k.rsplit('/').next() == Some(fragment_key))
        .map(|(_, v)| v)
        .collect();
    if secrets.len() != 1 {
        return Err(InputError("not_a_pairing_link"));
    }
    let secret = secrets[0].trim().to_string();
    let length = secret.chars().count();
    if !(6..=64).contains(&length)
        || !secret
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(InputError("bad_secret"));
    }
    let base = http_base(head)?;
    let mut candidates: Vec<String> = Vec::new();
    for (key, value) in &fields {
        if key == via_key {
            let url = http_base(value)?;
            if !candidates.contains(&url) {
                candidates.push(url);
            }
        }
    }
    if !candidates.contains(&base) {
        candidates.push(base.clone());
    }
    candidates.truncate(8);
    Ok(PairingLink {
        server_url: base,
        secret,
        candidates,
    })
}

/// One join input: a pairing link; `<url> <6-digit code>`; or a legacy
/// `<url> <token>` connect line.
pub fn parse_join_input(text: &str) -> Result<JoinInput, InputError> {
    let raw = text.trim();
    if raw.contains('#') && raw.contains(&format!("{}=", contract::pairing_str("fragment_key"))) {
        return parse_pairing_link(raw).map(JoinInput::Pairing);
    }
    let parts: Vec<&str> = raw.split_whitespace().collect();
    match parts.len() {
        2 => {
            let base = http_base(parts[0])?;
            let secret = parts[1].trim().to_string();
            let digits = contract::pairing("short_code_digits").as_u64().unwrap_or(6) as usize;
            if secret.len() == digits && secret.chars().all(|c| c.is_ascii_digit()) {
                return Ok(JoinInput::Pairing(PairingLink {
                    server_url: base.clone(),
                    secret,
                    candidates: vec![base],
                }));
            }
            Ok(JoinInput::LegacyConnectLine {
                server_url: base,
                secret,
            })
        }
        0 | 1 => Err(InputError("missing_parts")),
        _ => Err(InputError("too_many_parts")),
    }
}

/// The short code both ends show before the owner confirms a node
/// (contracts/desktop_node_registry_v1.schema.json x-identity.fingerprint).
pub fn fingerprint(node_id: &str, credential_id: &str) -> String {
    let digest =
        sha256_hex(format!("tofu-node-fingerprint/v1\0{node_id}\0{credential_id}").as_bytes())
            .to_ascii_uppercase();
    format!("{}-{}", &digest[..4], &digest[4..8])
}

pub fn attachment_id(url: &str) -> String {
    let base = http_base(url).unwrap_or_else(|_| url.to_string());
    sha256_hex(base.as_bytes())[..12].to_string()
}

/// Fold a legacy single `remote_server` into `attachments`; returns whether
/// `cfg` changed. The legacy key is cleared after its credential moves.
pub fn migrate_attachments(cfg: &mut Map<String, Value>) -> bool {
    let keys = &contract::node()["x-migration"];
    let legacy_key = keys["legacy_remote_server_key"]
        .as_str()
        .unwrap_or("remote_server");
    let attachments_key = keys["attachments_key"].as_str().unwrap_or("attachments");
    let mut changed = false;
    let mut attachments = match cfg.get(attachments_key) {
        Some(Value::Array(rows)) => rows.clone(),
        _ => {
            changed = true;
            Vec::new()
        }
    };
    if let Some(Value::Object(legacy)) = cfg.get(legacy_key) {
        let url = legacy
            .get("url")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let secret = legacy
            .get("secret")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if !url.is_empty() && !secret.is_empty() {
            let url = http_base(&url).unwrap_or_default();
            if !url.is_empty()
                && !attachments
                    .iter()
                    .any(|a| a.get("url").and_then(Value::as_str) == Some(url.as_str()))
            {
                let candidates: Vec<Value> = cfg
                    .get("attach_candidates")
                    .and_then(Value::as_array)
                    .map(|c| c.iter().filter(|v| v.is_string()).cloned().collect())
                    .unwrap_or_default();
                attachments.push(
                    json!({"id": attachment_id(&url), "url": url, "secret": secret,
                                        "candidates": candidates, "label": url, "state": "active"}),
                );
            }
            cfg.insert(legacy_key.into(), json!({}));
            changed = true;
        }
    }
    if changed {
        cfg.insert(attachments_key.into(), Value::Array(attachments));
    }
    changed
}

#[derive(Debug)]
pub struct JoinError(pub String);

fn pair_outcome(response: &http::Response) -> Result<Map<String, Value>, String> {
    match response.status {
        201 => {
            let mut payload = response.json().unwrap_or(Value::Null);
            if let Some(data) = payload.get("data").filter(|d| d.is_object()).cloned() {
                payload = data;
            }
            match payload {
                Value::Object(map)
                    if map
                        .get("token")
                        .and_then(Value::as_str)
                        .is_some_and(|t| !t.is_empty()) =>
                {
                    Ok(map)
                }
                _ => Err("not_tofu".into()),
            }
        }
        409 => Err("invalid_code".into()),
        429 => Err("rate_limited".into()),
        code => Err(format!("http_{code}")),
    }
}

fn redeem(route: &str, body: &Value) -> Result<Map<String, Value>, String> {
    let url = format!("{route}{}", contract::transport_path("pair_path"));
    match http::post_json(&url, body, &[], Duration::from_secs(8)) {
        Ok(response) => pair_outcome(&response),
        Err(_) => Err("unreachable".into()),
    }
}

/// Redeem through the signed-in page's relay; waits while the pairing lives
/// (`x-pairing.ttl_seconds`, after which the secret is dead anyway) or until
/// `stop` is set.
fn redeem_via_browser(
    link: &PairingLink,
    body: &Value,
    log: &dyn Fn(&str),
    stop: &Stop,
) -> Result<Map<String, Value>, String> {
    let Some(relay) = BrowserRelay::start(&link.candidates) else {
        return Err("unreachable".into());
    };
    let ttl = contract::pairing("ttl_seconds").as_u64().unwrap_or(300);
    let deadline = std::time::Instant::now() + Duration::from_secs(ttl);
    log(&format!(
        "Waiting for the open Tofu page to carry the pairing (relay port {})",
        relay.port()
    ));
    while !relay.browser_available() {
        if std::time::Instant::now() >= deadline || stop.wait(Duration::from_millis(500)) {
            return Err("browser_unavailable".into());
        }
    }
    let url = format!(
        "{}{}",
        link.server_url,
        contract::transport_path("pair_path")
    );
    let remaining = deadline
        .saturating_duration_since(std::time::Instant::now())
        .max(Duration::from_secs(1));
    match relay.request(&url, body, "", remaining) {
        Some(response) => pair_outcome(&response),
        None => Err("browser_unavailable".into()),
    }
}

/// Join a server from one pasted input; returns the saved attachment. The
/// new node waits for the Home owner's confirmation; the attachment keeps the
/// fingerprint both ends show. A host that replaced an earlier node names it
/// (`previous_node_id`), so the Home shows a re-pair of a known device.
pub fn join(
    text: &str,
    node_id: &str,
    kind: &str,
    log: &dyn Fn(&str),
    stop: &Stop,
) -> Result<Map<String, Value>, JoinError> {
    let identity = config::node_identity().map_err(JoinError)?;
    let previous = if identity.node_id == node_id {
        identity.predecessor.clone()
    } else {
        None
    };
    let parsed = parse_join_input(text).map_err(|e| JoinError(e.0.to_string()))?;
    let name = {
        let host = host_identity::hostname();
        if host.trim().is_empty() {
            "desktop".to_string()
        } else {
            host.trim().to_string()
        }
    };
    let (url, secret, candidates, label, paired) = match &parsed {
        JoinInput::LegacyConnectLine { server_url, secret } => (
            server_url.clone(),
            secret.clone(),
            Vec::new(),
            server_url.clone(),
            None,
        ),
        JoinInput::Pairing(link) => {
            let mut body = json!({"code": link.secret, "node_id": node_id, "name": truncate_chars(&name, 80),
                                  "platform": truncate_chars(python_platform(), 40), "kind": kind});
            if let Some(previous) = previous.as_ref().filter(|p| p.as_str() != node_id) {
                body["previous_node_id"] = previous.clone().into();
            }
            let mut reasons: Vec<String> = Vec::new();
            let mut found = None;
            for route in &link.candidates {
                match redeem(route, &body) {
                    Ok(payload) => {
                        found = Some((route.clone(), payload));
                        break;
                    }
                    Err(reason) => {
                        log(&format!("Pairing via {route} failed: {reason}"));
                        let fatal = reason == "invalid_code" || reason == "rate_limited";
                        reasons.push(reason);
                        if fatal {
                            break;
                        }
                    }
                }
            }
            let refused = reasons
                .last()
                .is_some_and(|r| r == "invalid_code" || r == "rate_limited");
            if found.is_none() && !refused {
                match redeem_via_browser(link, &body, log, stop) {
                    Ok(payload) => found = Some((link.server_url.clone(), payload)),
                    Err(reason) => reasons.push(reason),
                }
            }
            let Some((route, payload)) = found else {
                return Err(JoinError(
                    reasons.pop().unwrap_or_else(|| "unreachable".into()),
                ));
            };
            let token = payload["token"].as_str().unwrap_or("").to_string();
            let candidates: Vec<String> = link
                .candidates
                .iter()
                .filter(|c| **c != route)
                .cloned()
                .collect();
            (
                route,
                token,
                candidates,
                link.server_url.clone(),
                Some(payload),
            )
        }
    };
    let mut record = Map::new();
    record.insert("id".into(), attachment_id(&url).into());
    record.insert("url".into(), url.clone().into());
    record.insert("secret".into(), secret.into());
    record.insert("candidates".into(), json!(candidates));
    record.insert("label".into(), label.clone().into());
    record.insert("paired_at".into(), json!(unix_now()));
    record.insert("state".into(), "active".into());
    if let Some(paired) = &paired {
        let credential_id = match paired.get("id") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Null) | None => String::new(),
            Some(other) => other.to_string(),
        };
        let print = fingerprint(node_id, &credential_id);
        if let Some(theirs) = paired
            .get("fingerprint")
            .and_then(Value::as_str)
            .filter(|f| !f.is_empty())
        {
            if theirs != print {
                return Err(JoinError("fingerprint_mismatch".into()));
            }
        }
        record.insert("fingerprint".into(), print.into());
    }
    config::save_attachment(record.clone()).map_err(JoinError)?;
    if previous.is_some() && paired.is_some() {
        config::clear_predecessor().map_err(JoinError)?;
    }
    match record.get("fingerprint").and_then(Value::as_str) {
        Some(print) => log(&format!(
            "Joined {label} as node {name}; waiting for the owner to confirm it on that Tofu (fingerprint {print})"
        )),
        None => log(&format!("Joined {label} as node {name}")),
    }
    Ok(record)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn links_parse_like_the_python_owner() {
        let link = parse_pairing_link("  https://h.example/proxy/15000/#/chat&tofu-pair=abcDEF_-12&via=http%3A%2F%2F10.0.0.2%3A15000  ").unwrap();
        assert_eq!(link.server_url, "https://h.example/proxy/15000");
        assert_eq!(link.secret, "abcDEF_-12");
        assert_eq!(
            link.candidates,
            vec![
                "http://10.0.0.2:15000".to_string(),
                "https://h.example/proxy/15000".to_string()
            ]
        );
        assert_eq!(
            parse_pairing_link("https://h/#tofu-pair=abc"),
            Err(InputError("bad_secret"))
        );
        assert_eq!(
            parse_pairing_link("https://h/"),
            Err(InputError("not_a_pairing_link"))
        );
        assert_eq!(
            parse_pairing_link("ftp://h/#tofu-pair=abcdef12"),
            Err(InputError("bad_url"))
        );
        assert_eq!(
            parse_pairing_link("https://u:p@h/#tofu-pair=abcdef12"),
            Err(InputError("bad_url"))
        );
        assert_eq!(
            http_base("HTTPS://Host:8/a/b/?q=1#x").unwrap(),
            "https://Host:8/a/b"
        );
    }

    #[test]
    fn join_inputs() {
        assert!(
            matches!(parse_join_input("http://h:1 123456"), Ok(JoinInput::Pairing(PairingLink { ref secret, .. })) if secret == "123456")
        );
        assert!(matches!(
            parse_join_input("http://h:1 tok-en"),
            Ok(JoinInput::LegacyConnectLine { .. })
        ));
        assert_eq!(
            parse_join_input("http://h:1"),
            Err(InputError("missing_parts"))
        );
        assert_eq!(parse_join_input("a b c"), Err(InputError("too_many_parts")));
    }

    #[test]
    fn fingerprint_and_ids_are_digest_stable() {
        let print = fingerprint("node", "cred");
        assert_eq!(print.len(), 9);
        assert!(print
            .chars()
            .all(|c| c == '-' || c.is_ascii_digit() || ('A'..='F').contains(&c)));
        assert_eq!(attachment_id("http://h/"), attachment_id("http://h"));
    }

    #[test]
    fn legacy_remote_server_migrates_once() {
        let mut cfg = Map::new();
        cfg.insert(
            "remote_server".into(),
            json!({"url": "http://h:1/", "secret": "s"}),
        );
        assert!(migrate_attachments(&mut cfg));
        assert_eq!(cfg["attachments"][0]["url"], json!("http://h:1"));
        assert_eq!(cfg["remote_server"], json!({}));
        assert!(!migrate_attachments(&mut cfg));
    }
}
