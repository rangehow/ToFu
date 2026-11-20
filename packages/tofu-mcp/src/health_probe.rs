//! The credential health-probe contract (`lib/mcp/health_probe.py`, the
//! Python oracle): `validate_health_probe` normalizes a server's declared
//! `health_probe` and `classify_probe_result` turns a probe's result text into
//! `ok`/`expired`. Both are pure; the bridge (`bridge/credentials.rs`) runs the
//! probe tool and maps a raised call to `unknown`.

use crate::pyjson::{py_prefix, py_repr, py_strip};
use serde_json::{json, Map, Value};

/// `DEFAULT_CRED_FAIL_PATTERNS`.
pub const DEFAULT_CRED_FAIL_PATTERNS: [&str; 28] = [
    "session expired",
    "session has expired",
    "session cookie has expired",
    "session may have expired",
    "not authenticated",
    "not logged in",
    "please login",
    "please log in",
    "authentication failed",
    "authentication required",
    "unauthorized",
    "unauthenticated",
    "token expired",
    "token has expired",
    "token is invalid",
    "invalid token",
    "invalid or expired",
    "credentials expired",
    "login required",
    "登录已过期",
    "登录已失效",
    "会话已过期",
    "请重新登录",
    "请先登录",
    "未登录",
    "未授权",
    "认证失败",
    "身份验证失败",
];

/// `STRUCTURED_EXPIRED_MARKERS`.
pub const STRUCTURED_EXPIRED_MARKERS: [&str; 3] = [
    "\"login_required\": true",
    "\"login_required\":true",
    "'login_required': true",
];

/// `HEALTH_PROBE_SCHEMA`.
pub fn health_probe_schema() -> Value {
    json!({
        "fields": {
            "tool": {"type": "string", "required": true,
                     "doc": "A cheap READ-ONLY tool whose success depends only on the stored credential."},
            "args": {"type": "object", "required": false, "default": {},
                     "doc": "Arguments passed to the probe tool."},
            "fail_patterns": {"type": "array<string>", "required": false,
                              "doc": "Case-insensitive substrings meaning the credential is dead. ADDED to the built-in default auth-phrase set."},
        },
        "default_fail_patterns": DEFAULT_CRED_FAIL_PATTERNS,
        "structured_markers": STRUCTURED_EXPIRED_MARKERS,
    })
}

/// Python `type(value).__name__` for a decoded JSON value.
fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// `validate_health_probe(spec, server=)`: the normalized `{tool, args,
/// fail_patterns}` or None; `warn` receives the oracle's warning lines.
pub fn validate_health_probe(
    spec: Option<&Value>,
    server: &str,
    warn: &mut dyn FnMut(String),
) -> Option<Value> {
    let label = py_repr(if server.is_empty() { "?" } else { server });
    let spec = match spec {
        None | Some(Value::Null) => return None,
        Some(Value::Object(map)) => map,
        Some(other) => {
            warn(format!(
                "[MCP] health_probe for {label} ignored: expected an object, got {}",
                type_name(other)
            ));
            return None;
        }
    };
    let tool = match spec.get("tool") {
        Some(Value::String(tool)) if !py_strip(tool).is_empty() => py_strip(tool).to_string(),
        _ => {
            warn(format!(
                "[MCP] health_probe for {label} ignored: missing/invalid required \"tool\" field"
            ));
            return None;
        }
    };
    let args = match spec.get("args") {
        None => Map::new(),
        Some(Value::Object(args)) => args.clone(),
        Some(other) => {
            warn(format!(
                "[MCP] health_probe for {label}: \"args\" is not an object ({}) — defaulting to {{}}",
                type_name(other)
            ));
            Map::new()
        }
    };
    let mut explicit: Vec<String> = Vec::new();
    match spec.get("fail_patterns") {
        Some(Value::Array(patterns)) => {
            for pattern in patterns {
                if let Value::String(text) = pattern {
                    if !py_strip(text).is_empty() {
                        explicit.push(text.to_lowercase());
                    }
                }
            }
        }
        Some(other) if crate::pyos::truthy(Some(other)) => warn(format!(
            "[MCP] health_probe for {label}: \"fail_patterns\" is not a list ({}) — ignoring",
            type_name(other)
        )),
        _ => {}
    }
    let mut merged: Vec<String> = Vec::new();
    for pattern in DEFAULT_CRED_FAIL_PATTERNS
        .iter()
        .map(|p| p.to_string())
        .chain(explicit)
    {
        if !merged.contains(&pattern) {
            merged.push(pattern);
        }
    }
    Some(json!({"tool": tool, "args": args, "fail_patterns": merged}))
}

/// `_snippet`: the first non-empty line, capped at 200 characters.
fn snippet(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    for line in crate::pyos::splitlines(py_strip(text)) {
        let line = py_strip(line);
        if !line.is_empty() {
            return py_prefix(line, 200).to_string();
        }
    }
    py_prefix(py_strip(text), 200).to_string()
}

/// `classify_probe_result(text, spec)` → `(status, detail)`.
pub fn classify_probe_result(text: &str, spec: &Value) -> (&'static str, String) {
    let low = text.to_lowercase();
    if STRUCTURED_EXPIRED_MARKERS.iter().any(|m| low.contains(m)) {
        return ("expired", snippet(text));
    }
    let hit = match spec.get("fail_patterns") {
        Some(Value::Array(patterns)) if !patterns.is_empty() => patterns.iter().any(|p| match p {
            Value::String(p) => low.contains(p.as_str()),
            _ => false,
        }),
        Some(Value::String(chars)) if !chars.is_empty() => chars.chars().any(|c| low.contains(c)),
        _ => DEFAULT_CRED_FAIL_PATTERNS.iter().any(|p| low.contains(p)),
    };
    if hit {
        return ("expired", snippet(text));
    }
    ("ok", String::new())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn validate(spec: Value) -> (Option<Value>, Vec<String>) {
        let mut warnings = Vec::new();
        let out = validate_health_probe(Some(&spec), "x", &mut |w| warnings.push(w));
        (out, warnings)
    }

    #[test]
    fn validation_merges_defaults_first_and_repairs_optional_fields() {
        let (spec, _) =
            validate(json!({"tool": " whoami ", "fail_patterns": ["Custom Boom", "custom boom"]}));
        let spec = spec.unwrap();
        assert_eq!(spec["tool"], "whoami");
        let patterns = spec["fail_patterns"].as_array().unwrap();
        assert_eq!(patterns[0], DEFAULT_CRED_FAIL_PATTERNS[0]);
        assert_eq!(patterns.iter().filter(|p| *p == "custom boom").count(), 1);
        let (repaired, warnings) =
            validate(json!({"tool": "ping", "args": "oops", "fail_patterns": "oops"}));
        assert_eq!(repaired.unwrap()["args"], json!({}));
        assert_eq!(warnings.len(), 2);
        assert!(validate(json!({"tool": "  "})).0.is_none());
        assert!(validate(json!(123)).0.is_none());
    }

    #[test]
    fn classification_prefers_structured_markers() {
        let (spec, _) = validate(json!({"tool": "t"}));
        let spec = spec.unwrap();
        assert_eq!(
            classify_probe_result("{\"ok\": false, \"login_required\": true}", &spec).0,
            "expired"
        );
        assert_eq!(
            classify_probe_result("错误：登录已过期，请重新登录", &spec).0,
            "expired"
        );
        assert_eq!(
            classify_probe_result("Your projects (1):\n  • login-service-docs", &spec),
            ("ok", String::new())
        );
        assert_eq!(
            classify_probe_result("\n  MCP Error: not authenticated\nmore", &json!({})),
            ("expired", "MCP Error: not authenticated".to_string())
        );
    }
}
