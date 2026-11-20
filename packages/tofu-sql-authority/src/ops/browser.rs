//! `operation_domains/browser.py`: bounded owner-scoped storage for
//! non-executable browser observations (`operations_pkg/_browser.py`).

use std::collections::BTreeSet;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value};

use super::artifacts::pyjson;
use super::{register as add, OperationSpec, Registry};
use crate::codec::py_eq;
use crate::error::{OpResult, StorageError};
use crate::params;
use crate::payload::{char_len, integer, required_text, Payload};
use crate::session::{Row, Session, SqlValue};

const MAX_OBSERVATION_BYTES: usize = 4_096;
const MAX_OBSERVATIONS_PER_OWNER: i64 = 200;
const RETENTION_MS: i64 = 30 * 24 * 60 * 60 * 1000;
const OUTCOMES: &[&str] = &[
    "success",
    "not_observed",
    "structure_mismatch",
    "not_found",
    "auth_challenge",
    "rate_limited",
    "transient_failure",
    "policy_denied",
];
const STRATEGIES: &[&str] = &["token_gated_api", "hydrated_state", "captured_api", "rendered_dom"];
const IDENTIFIER_PARENT_SEGMENTS: &[&str] = &[
    "account",
    "accounts",
    "document",
    "documents",
    "employee",
    "employees",
    "item",
    "items",
    "member",
    "members",
    "order",
    "orders",
    "org",
    "orgs",
    "organization",
    "organizations",
    "people",
    "person",
    "profile",
    "profiles",
    "project",
    "projects",
    "team",
    "teams",
    "user",
    "users",
];
const HINT_FIELDS: &[&str] = &["method", "origin", "path_template", "shape_summary", "score", "passive_only"];

pub fn register(registry: &mut Registry) {
    add(registry, "browser.site_observation.get", OperationSpec::query(site_observation_get));
    add(registry, "browser.site_observation.record", OperationSpec::command(false, site_observation_record));
}

fn route_family_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^/(?:[a-z][a-z_-]{0,39}|\{(?:segment|truncated)\})?(?:/(?:[a-z][a-z_-]{0,39}|\{(?:segment|truncated)\}))*$",
        )
        .expect("route family regex")
    })
}

fn sensitive_hint_field_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?i)(?:token|secret|password|passwd|authorization|cookie|credential|session|ticket|sso|api[_-]?key)",
        )
        .expect("sensitive field regex")
    })
}

fn shape_descriptor_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"^(?:null|boolean|number|string(?:\(len=\d+\))?|array\(\d+\)|object(?:\(empty\))?|reached \d+-entry budget)$",
        )
        .expect("shape descriptor regex")
    })
}

/// `_route_family_valid`.
fn route_family_valid(route_family: &str) -> bool {
    if !route_family_re().is_match(route_family) {
        return false;
    }
    let segments: Vec<&str> = route_family.split('/').skip(1).collect();
    segments.iter().enumerate().all(|(index, segment)| {
        index == 0 || !IDENTIFIER_PARENT_SEGMENTS.contains(&segments[index - 1]) || *segment == "{segment}"
    })
}

fn protocol_error(message: &str) -> StorageError {
    StorageError::protocol(message)
}

/// The `urllib.parse.urlsplit` fields the origin checks read.
struct SplitUrl {
    scheme: String,
    netloc: String,
    path: String,
    query: String,
    fragment: String,
}

const SCHEME_CHARS: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.";

/// `urllib.parse.urlsplit`; `None` is its `ValueError`.
fn urlsplit(url: &str) -> Option<SplitUrl> {
    let stripped = url.trim_start_matches(|c: char| (c as u32) <= 0x20);
    let mut url: String = stripped.chars().filter(|c| !matches!(c, '\t' | '\r' | '\n')).collect();
    let mut scheme = String::new();
    if let Some(index) = url.find(':') {
        let first = url.chars().next();
        if index > 0
            && first.is_some_and(|c| c.is_ascii_alphabetic())
            && url[..index].chars().all(|c| SCHEME_CHARS.contains(c))
        {
            scheme = url[..index].to_ascii_lowercase();
            url = url[index + 1..].to_owned();
        }
    }
    let mut netloc = String::new();
    if url.starts_with("//") {
        let rest = &url[2..];
        let delimiter = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        netloc = rest[..delimiter].to_owned();
        url = rest[delimiter..].to_owned();
        if netloc.contains('[') != netloc.contains(']') {
            return None;
        }
        if let Some((_, after)) = netloc.split_once('[') {
            let bracketed = after.split_once(']').map(|(host, _)| host).unwrap_or(after);
            if !check_bracketed_host(bracketed) {
                return None;
            }
        }
    }
    let mut fragment = String::new();
    if let Some((before, after)) = url.split_once('#') {
        fragment = after.to_owned();
        url = before.to_owned();
    }
    let mut query = String::new();
    if let Some((before, after)) = url.split_once('?') {
        query = after.to_owned();
        url = before.to_owned();
    }
    Some(SplitUrl { scheme, netloc, path: url, query, fragment })
}

/// `urllib.parse._check_bracketed_host`.
fn check_bracketed_host(hostname: &str) -> bool {
    if let Some(rest) = hostname.strip_prefix('v') {
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new(r"^[a-fA-F0-9]+\.(?s:.)+$").expect("ipvfuture regex"));
        return re.is_match(rest);
    }
    hostname.parse::<std::net::Ipv6Addr>().is_ok()
}

impl SplitUrl {
    fn userinfo(&self) -> Option<&str> {
        self.netloc.rsplit_once('@').map(|(userinfo, _)| userinfo)
    }

    fn username(&self) -> Option<&str> {
        self.userinfo().map(|userinfo| userinfo.split_once(':').map(|(user, _)| user).unwrap_or(userinfo))
    }

    fn password(&self) -> Option<&str> {
        self.userinfo().and_then(|userinfo| userinfo.split_once(':').map(|(_, password)| password))
    }

    fn hostname(&self) -> Option<String> {
        let hostinfo = self.netloc.rsplit_once('@').map(|(_, host)| host).unwrap_or(&self.netloc);
        let hostname = match hostinfo.split_once('[') {
            Some((_, bracketed)) => bracketed.split_once(']').map(|(host, _)| host).unwrap_or(bracketed),
            None => hostinfo.split_once(':').map(|(host, _)| host).unwrap_or(hostinfo),
        };
        if hostname.is_empty() {
            None
        } else {
            Some(hostname.to_lowercase())
        }
    }

    /// The shared origin predicate of `_identity` and hint validation.
    fn is_bare_http_origin(&self) -> bool {
        matches!(self.scheme.as_str(), "http" | "https")
            && self.hostname().is_some()
            && self.username().is_none_or(str::is_empty)
            && self.password().is_none_or(str::is_empty)
            && self.path.is_empty()
            && self.query.is_empty()
            && self.fragment.is_empty()
    }
}

fn origin_valid(origin: &str) -> bool {
    urlsplit(origin).is_some_and(|parts| parts.is_bare_http_origin())
}

/// `_identity`.
fn identity(payload: &Payload) -> OpResult<(i64, String, String, String)> {
    let owner_user_id = integer(payload, "owner_user_id", None, Some(1), None)?;
    let origin = required_text(payload, "origin", 512)?;
    let route_family = required_text(payload, "route_family", 512)?;
    let operation = required_text(payload, "operation", 64)?;
    if !origin_valid(&origin) {
        return Err(protocol_error("Invalid site observation origin").into());
    }
    if !route_family_valid(&route_family) {
        return Err(protocol_error("Invalid site observation route family").into());
    }
    Ok((owner_user_id, origin, route_family, operation))
}

/// Python `str(row[column])`.
fn py_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        other => {
            if other.truthy() {
                other.text_or_empty()
            } else {
                match other {
                    SqlValue::Integer(_) => "0".into(),
                    SqlValue::Real(real) => crate::json::python_float_repr(*real),
                    SqlValue::Blob(_) => "b''".into(),
                    _ => String::new(),
                }
            }
        }
    }
}

/// `_decode_hints`.
fn decode_hints(raw: &SqlValue) -> OpResult<Value> {
    let text = if raw.truthy() { py_str(raw) } else { "[]".into() };
    let invalid = || StorageError::new("database_integrity", "Stored site observation hints are invalid");
    let value: Value = serde_json::from_str(&text).map_err(|_| invalid())?;
    let Value::Array(items) = value else { return Err(invalid().into()) };
    Ok(Value::Array(items.into_iter().filter(Value::is_object).take(5).collect()))
}

/// `_row_document`.
fn row_document(row: &Row) -> OpResult<Value> {
    let mut document = Map::new();
    document.insert("schema_version".into(), Value::from(row.int("schema_version")?));
    for column in ["origin", "route_family", "operation", "strategy"] {
        document.insert(column.into(), Value::String(py_str(row.require(column)?)));
    }
    document.insert("api_hints".into(), decode_hints(row.require("hints_json")?)?);
    for column in ["anti_bot_vendor", "auth_signal", "status"] {
        document.insert(column.into(), Value::String(py_str(row.require(column)?)));
    }
    for column in [
        "confidence_milli",
        "visit_count",
        "successful_visits",
        "hinted_visits",
        "hint_match_visits",
        "consecutive_failures",
    ] {
        document.insert(column.into(), Value::from(row.int(column)?));
    }
    document.insert("last_outcome".into(), Value::String(py_str(row.require("last_outcome")?)));
    for column in ["last_elapsed_ms", "last_verified_at_ms", "last_observed_at_ms", "expires_at_ms", "payload_bytes"] {
        document.insert(column.into(), Value::from(row.int(column)?));
    }
    Ok(Value::Object(document))
}

const SELECT_OBSERVATION: &str = "SELECT * FROM storage_browser_site_observations WHERE \
owner_user_id=? AND origin=? AND route_family=? AND operation=?";

/// `_browser_site_observation_get`.
fn site_observation_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, origin, route_family, operation) = identity(payload)?;
    let now_ms = integer(payload, "now_ms", None, Some(0), None)?;
    let row = session.fetch_one(SELECT_OBSERVATION, &params![owner_user_id, &origin, &route_family, &operation])?;
    match row {
        Some(row) if row.int("expires_at_ms")? > now_ms => row_document(&row),
        _ => Ok(Value::Null),
    }
}

struct Observation {
    schema_version: i64,
    strategy: String,
    hints_json: String,
    anti_bot_vendor: String,
    auth_signal: String,
    last_elapsed_ms: i64,
    payload_bytes: i64,
    capture_hint_used: bool,
    capture_hint_matched: bool,
}

fn is_python_number(value: &Value) -> bool {
    matches!(value, Value::Number(_))
}

/// `_validated_observation`.
fn validated_observation(payload: &Payload) -> OpResult<Observation> {
    let Some(Value::Object(source)) = payload.get("observation") else {
        return Err(protocol_error("A successful site observation requires an observation object").into());
    };
    let schema_version = source.get("schema_version").unwrap_or(&Value::Null);
    if !py_eq(schema_version, &Value::from(1)) || schema_version.is_boolean() {
        return Err(protocol_error("Unsupported site observation schema version").into());
    }
    let strategy_value = source.get("strategy").unwrap_or(&Value::Null);
    let strategy =
        if crate::json::truthy(strategy_value) { crate::json::python_str(strategy_value) } else { String::new() };
    if !STRATEGIES.contains(&strategy.as_str()) {
        return Err(protocol_error("Invalid site observation strategy").into());
    }
    let hints = match source.get("api_hints") {
        Some(Value::Array(hints)) if hints.len() <= 5 => hints,
        _ => return Err(protocol_error("Invalid site observation API hints").into()),
    };
    let mut normalized_hints = Vec::new();
    for hint in hints {
        let Value::Object(hint) = hint else {
            return Err(protocol_error("Site observation hints must be passive-only").into());
        };
        if hint.get("passive_only") != Some(&Value::Bool(true)) {
            return Err(protocol_error("Site observation hints must be passive-only").into());
        }
        let keys: BTreeSet<&str> = hint.keys().map(String::as_str).collect();
        let expected: BTreeSet<&str> = HINT_FIELDS.iter().copied().collect();
        if keys != expected {
            return Err(protocol_error("Site observation hint fields are invalid").into());
        }
        let method = &hint["method"];
        let hint_origin = &hint["origin"];
        let hint_path = &hint["path_template"];
        let shape = &hint["shape_summary"];
        let score = &hint["score"];
        if !matches!(method.as_str(), Some("GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE")) {
            return Err(protocol_error("Invalid site observation hint method").into());
        }
        match hint_origin {
            Value::String(text) if char_len(text) <= 512 && origin_valid(text) => {}
            _ => return Err(protocol_error("Invalid site observation hint origin").into()),
        }
        match hint_path {
            Value::String(text) if char_len(text) <= 512 && route_family_valid(text) => {}
            _ => return Err(protocol_error("Site observation hints cannot retain query data").into()),
        }
        let shape_valid = match shape {
            Value::Object(shape) => {
                shape.len() <= 12
                    && shape.iter().all(|(key, value)| {
                        !key.is_empty()
                            && char_len(key) <= 240
                            && (!sensitive_hint_field_re().is_match(key) || key == "$.[sensitive]")
                            && matches!(value, Value::String(text)
                                if !text.is_empty() && char_len(text) <= 80 && shape_descriptor_re().is_match(text))
                    })
            }
            _ => false,
        };
        if !shape_valid {
            return Err(protocol_error("Invalid site observation hint shape").into());
        }
        let score_value = score.as_f64().filter(|_| is_python_number(score));
        let Some(score_value) = score_value.filter(|value| value.is_finite() && (0.0..=1.0).contains(value)) else {
            return Err(protocol_error("Invalid site observation hint score").into());
        };
        let mut normalized = Map::new();
        normalized.insert("method".into(), method.clone());
        normalized.insert("origin".into(), hint_origin.clone());
        normalized.insert("path_template".into(), hint_path.clone());
        normalized.insert("shape_summary".into(), shape.clone());
        normalized.insert("score".into(), crate::json::float_value(score_value));
        normalized.insert("passive_only".into(), Value::Bool(true));
        normalized_hints.push(Value::Object(normalized));
    }
    let encoded_hints = pyjson::dumps(&Value::Array(normalized_hints), true);
    let document_bytes = pyjson::dumps(&Value::Object(source.clone()), true).len();
    if document_bytes > MAX_OBSERVATION_BYTES {
        return Err(protocol_error("Site observation exceeds its byte budget").into());
    }
    let elapsed_ms = match source.get("elapsed_ms") {
        None => Some(0),
        Some(Value::Number(number)) => number.as_i64(),
        Some(_) => None,
    };
    let Some(elapsed_ms) = elapsed_ms.filter(|value| (0..=120_000).contains(value)) else {
        return Err(protocol_error("Invalid site observation elapsed time").into());
    };
    let flag = |key: &str| match source.get(key) {
        None => Some(false),
        Some(Value::Bool(flag)) => Some(*flag),
        Some(_) => None,
    };
    let (Some(capture_hint_used), Some(capture_hint_matched)) =
        (flag("capture_hint_used"), flag("capture_hint_matched"))
    else {
        return Err(protocol_error("Invalid site observation capture-hint metrics").into());
    };
    if capture_hint_matched && !capture_hint_used {
        return Err(protocol_error("A capture hint cannot match when no hint was used").into());
    }
    let anti_bot_vendor = match source.get("anti_bot_vendor") {
        None => Some(""),
        Some(Value::String(text)) => Some(text.as_str()),
        Some(_) => None,
    };
    let auth_signal = match source.get("auth_signal") {
        None => Some("none"),
        Some(Value::String(text)) => Some(text.as_str()),
        Some(_) => None,
    };
    let Some(anti_bot_vendor) =
        anti_bot_vendor.filter(|vendor| ["", "aliyun_waf", "cloudflare", "akamai", "geetest"].contains(vendor))
    else {
        return Err(protocol_error("Invalid site observation anti-bot vendor").into());
    };
    let Some(auth_signal) = auth_signal.filter(|signal| ["none", "challenge"].contains(signal)) else {
        return Err(protocol_error("Invalid site observation auth signal").into());
    };
    Ok(Observation {
        schema_version: 1,
        strategy,
        hints_json: encoded_hints,
        anti_bot_vendor: anti_bot_vendor.to_owned(),
        auth_signal: auth_signal.to_owned(),
        last_elapsed_ms: elapsed_ms,
        payload_bytes: document_bytes as i64,
        capture_hint_used,
        capture_hint_matched,
    })
}

const DELETE_OBSERVATION: &str = "DELETE FROM storage_browser_site_observations WHERE owner_user_id=? \
AND origin=? AND route_family=? AND operation=?";

/// `_prune_owner_lru`.
fn prune_owner_lru(session: &Session<'_>, owner_user_id: i64, now_ms: i64) -> OpResult<()> {
    let expired = session.fetch_all(
        "SELECT origin, route_family, operation FROM storage_browser_site_observations \
         WHERE owner_user_id=? AND expires_at_ms<=? \
         ORDER BY expires_at_ms, origin, route_family, operation LIMIT 64",
        &params![owner_user_id, now_ms],
    )?;
    for row in &expired {
        session.execute(
            DELETE_OBSERVATION,
            &params![owner_user_id, row.get("origin"), row.get("route_family"), row.get("operation")],
        )?;
    }
    let rows = session.fetch_all(
        "SELECT origin, route_family, operation FROM storage_browser_site_observations \
         WHERE owner_user_id=? ORDER BY last_observed_at_ms DESC, origin, \
         route_family, operation LIMIT ?",
        &params![owner_user_id, MAX_OBSERVATIONS_PER_OWNER + 1],
    )?;
    for row in rows.iter().skip(MAX_OBSERVATIONS_PER_OWNER as usize) {
        session.execute(
            DELETE_OBSERVATION,
            &params![owner_user_id, row.get("origin"), row.get("route_family"), row.get("operation")],
        )?;
    }
    Ok(())
}

/// `_browser_site_observation_record`.
fn site_observation_record(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (owner_user_id, origin, route_family, operation) = identity(payload)?;
    let observed_at_ms = integer(payload, "observed_at_ms", None, Some(1), None)?;
    let outcome = required_text(payload, "outcome", 32)?;
    if !OUTCOMES.contains(&outcome.as_str()) {
        return Err(protocol_error("Invalid site observation outcome").into());
    }
    session.execute(
        "INSERT INTO storage_browser_site_observation_owners(owner_user_id,touched_at_ms) VALUES (?,?) \
         ON CONFLICT(owner_user_id) DO UPDATE SET touched_at_ms=excluded.touched_at_ms",
        &params![owner_user_id, observed_at_ms],
    )?;
    let previous =
        session.fetch_one(SELECT_OBSERVATION, &params![owner_user_id, &origin, &route_family, &operation])?;

    let current;
    let confidence;
    let successful_visits;
    let hinted_visits;
    let hint_match_visits;
    let consecutive_failures;
    let status;
    let verified_at_ms;
    if outcome == "success" {
        current = validated_observation(payload)?;
        let same_strategy =
            previous.as_ref().is_some_and(|row| row.get("strategy").as_str() == Some(current.strategy.as_str()));
        confidence = match &previous {
            Some(row) if same_strategy => 1_000.min(row.int("confidence_milli")? + 100),
            None => 500,
            Some(_) => 400,
        };
        successful_visits = match &previous {
            Some(row) => row.int("successful_visits")? + 1,
            None => 1,
        };
        hinted_visits = match &previous {
            Some(row) => row.int("hinted_visits")?,
            None => 0,
        } + current.capture_hint_used as i64;
        hint_match_visits = match &previous {
            Some(row) => row.int("hint_match_visits")?,
            None => 0,
        } + current.capture_hint_matched as i64;
        consecutive_failures = 0;
        status = "active".to_owned();
        verified_at_ms = observed_at_ms;
    } else {
        let Some(row) = &previous else { return Ok(Value::Null) };
        current = Observation {
            schema_version: row.int("schema_version")?,
            strategy: py_str(row.require("strategy")?),
            hints_json: py_str(row.require("hints_json")?),
            anti_bot_vendor: py_str(row.require("anti_bot_vendor")?),
            auth_signal: if outcome == "auth_challenge" {
                "challenge".into()
            } else {
                py_str(row.require("auth_signal")?)
            },
            last_elapsed_ms: row.int("last_elapsed_ms")?,
            payload_bytes: row.int("payload_bytes")?,
            capture_hint_used: false,
            capture_hint_matched: false,
        };
        let mut value = row.int("confidence_milli")?;
        successful_visits = row.int("successful_visits")?;
        hinted_visits = row.int("hinted_visits")?;
        hint_match_visits = row.int("hint_match_visits")?;
        let mut failures = row.int("consecutive_failures")?;
        if outcome == "structure_mismatch" || outcome == "not_found" {
            value = 0.max(value - 250);
            failures += 1;
        } else if outcome == "not_observed" {
            value = 0.max(value - 100);
            failures += 1;
        }
        confidence = value;
        consecutive_failures = failures;
        status = if consecutive_failures >= 3 { "quarantined".into() } else { py_str(row.require("status")?) };
        verified_at_ms = row.int("last_verified_at_ms")?;
    }

    let visit_count = match &previous {
        Some(row) => row.int("visit_count")? + 1,
        None => 1,
    };
    let expires_at_ms = observed_at_ms + RETENTION_MS;
    session.execute(
        "INSERT INTO storage_browser_site_observations(\
owner_user_id,origin,route_family,operation,schema_version,strategy,\
hints_json,anti_bot_vendor,auth_signal,status,confidence_milli,\
visit_count,successful_visits,hinted_visits,hint_match_visits,\
consecutive_failures,last_outcome,\
last_elapsed_ms,last_verified_at_ms,last_observed_at_ms,expires_at_ms,\
payload_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) \
ON CONFLICT(owner_user_id,origin,route_family,operation) DO UPDATE SET \
schema_version=excluded.schema_version,strategy=excluded.strategy,\
hints_json=excluded.hints_json,anti_bot_vendor=excluded.anti_bot_vendor,\
auth_signal=excluded.auth_signal,status=excluded.status,\
confidence_milli=excluded.confidence_milli,visit_count=excluded.visit_count,\
successful_visits=excluded.successful_visits,\
hinted_visits=excluded.hinted_visits,\
hint_match_visits=excluded.hint_match_visits,\
consecutive_failures=excluded.consecutive_failures,\
last_outcome=excluded.last_outcome,last_elapsed_ms=excluded.last_elapsed_ms,\
last_verified_at_ms=excluded.last_verified_at_ms,\
last_observed_at_ms=excluded.last_observed_at_ms,\
expires_at_ms=excluded.expires_at_ms,payload_bytes=excluded.payload_bytes",
        &params![
            owner_user_id,
            &origin,
            &route_family,
            &operation,
            current.schema_version,
            &current.strategy,
            &current.hints_json,
            &current.anti_bot_vendor,
            &current.auth_signal,
            &status,
            confidence,
            visit_count,
            successful_visits,
            hinted_visits,
            hint_match_visits,
            consecutive_failures,
            &outcome,
            current.last_elapsed_ms,
            verified_at_ms,
            observed_at_ms,
            expires_at_ms,
            current.payload_bytes,
        ],
    )?;
    prune_owner_lru(session, owner_user_id, observed_at_ms)?;
    let row = session.fetch_one(SELECT_OBSERVATION, &params![owner_user_id, &origin, &route_family, &operation])?;
    match row {
        Some(row) => row_document(&row),
        None => Ok(Value::Null),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins_follow_urlsplit() {
        assert!(origin_valid("https://example.com"));
        assert!(origin_valid("HTTPS://Example.com:8443"));
        assert!(origin_valid("https://@example.com"));
        assert!(origin_valid("https://example.com?"));
        assert!(origin_valid("https://[::1]:80"));
        assert!(!origin_valid("https://example.com/"));
        assert!(!origin_valid("https://u:p@example.com"));
        assert!(!origin_valid("ftp://example.com"));
        assert!(!origin_valid("https://[1.2.3.4]"));
        assert!(!origin_valid("https://[::1"));
        assert!(!origin_valid("https://"));
    }

    #[test]
    fn route_families_refuse_identifiers() {
        assert!(route_family_valid("/users/{segment}"));
        assert!(!route_family_valid("/users/abc"));
        assert!(route_family_valid("/"));
        assert!(!route_family_valid("/a?b"));
    }
}
