//! Transport classification, credential templating and redaction
//! (`lib/mcp/transport.py`, the Python oracle).
//!
//! Credentials have one home, a server's `env` block. `url` and `headers` are
//! templates of `${VAR}` placeholders; the resolved strings carry live secrets
//! and never reach a log or an API caller. Everything rendered from transport
//! text passes `scrub_text`, and config echoes pass the fail-closed
//! `redact_config` whitelist.

use regex::{Captures, Regex};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::sync::OnceLock;

pub const STDIO: &str = "stdio";
pub const SSE: &str = "sse";
pub const STREAMABLE_HTTP: &str = "streamable-http";
pub const PUBLIC_CONFIG_FIELDS: [&str; 6] = [
    "command",
    "args",
    "transport",
    "enabled",
    "description",
    "timeout",
];
pub const TRANSFORMED_CONFIG_FIELDS: [&str; 2] = ["url", "headers"];
pub const SECRET_CONFIG_FIELDS: [&str; 1] = ["env"];

fn placeholder() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}").expect("static pattern"))
}

fn url_in_text() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r#"https?://[^\s'"<>\\]+"#).expect("static pattern"))
}

/// The canonical transport name of a config (default `stdio`).
pub fn normalize_transport(config: &Value) -> String {
    let raw = match config.get("transport") {
        Some(Value::String(text)) if !text.is_empty() => text.trim().to_lowercase(),
        Some(other) if crate::pyos::truthy(Some(other)) => {
            crate::pyos::py_str(other).trim().to_lowercase()
        }
        _ => STDIO.to_string(),
    };
    match raw.as_str() {
        "http" | "https" | "streamable_http" | "streamablehttp" => STREAMABLE_HTTP.into(),
        _ => raw,
    }
}

/// Anything not a recognised remote transport is stdio.
pub fn is_stdio(config: &Value) -> bool {
    !matches!(normalize_transport(config).as_str(), SSE | STREAMABLE_HTTP)
}

/// Resolves a `vault:<entry>` value; `Err` carries the user-facing refusal.
pub trait VaultResolver {
    fn resolve(&self, entry: &str) -> Option<String>;
}

/// No vault available: every reference is unresolved (fail closed).
pub struct NoVault;

impl VaultResolver for NoVault {
    fn resolve(&self, _entry: &str) -> Option<String> {
        None
    }
}

/// `vault_reference_entry`.
pub fn vault_reference_entry(value: &str) -> Option<&str> {
    value.strip_prefix("vault:").map(crate::pyjson::py_strip)
}

/// `resolve_vault_env_references`: only the explicit `vault:` prefix is interpreted.
pub fn resolve_vault_env_references(
    overlay: &Map<String, Value>,
    vault: &dyn VaultResolver,
) -> Result<Map<String, Value>, String> {
    static ENTRY: OnceLock<Regex> = OnceLock::new();
    let entry_re =
        ENTRY.get_or_init(|| Regex::new(r"^[a-z0-9][a-z0-9_.-]{0,63}$").expect("static pattern"));
    let mut resolved = overlay.clone();
    for (key, value) in overlay {
        let Some(entry) = value.as_str().and_then(vault_reference_entry) else {
            continue;
        };
        if !entry_re.is_match(entry) {
            return Err(format!(
                "MCP env {key} names an invalid credential vault entry"
            ));
        }
        match vault.resolve(entry).filter(|secret| !secret.is_empty()) {
            Some(secret) => {
                resolved.insert(key.clone(), Value::String(secret));
            }
            None => {
                return Err(format!(
                    "MCP env {key} references vault entry {}, which is not configured. Add it in \
                     Settings → Advanced → 凭证保管库, or set a literal value for this server.",
                    crate::pyjson::py_repr(entry)
                ))
            }
        }
    }
    Ok(resolved)
}

fn resolve_placeholders(
    raw: &str,
    env: &Map<String, Value>,
    process_env: &BTreeMap<String, String>,
    vault: &dyn VaultResolver,
    missing: &mut Vec<String>,
) -> Result<String, String> {
    let mut failure = None;
    let out = placeholder().replace_all(raw, |caps: &Captures| {
        let var = caps[1].to_string();
        let from_env = env
            .get(&var)
            .filter(|v| crate::pyos::truthy(Some(v)))
            .map(crate::pyos::py_str);
        let value = from_env
            .or_else(|| process_env.get(&var).filter(|v| !v.is_empty()).cloned())
            .unwrap_or_default();
        if crate::pyjson::py_strip(&value).is_empty() {
            missing.push(var);
            return String::new();
        }
        if vault_reference_entry(&value).is_some() {
            let mut single = Map::new();
            single.insert(var.clone(), Value::String(value));
            match resolve_vault_env_references(&single, vault) {
                Ok(map) => return crate::pyos::py_str(&map[&var]),
                Err(error) => {
                    failure.get_or_insert(error);
                    return String::new();
                }
            }
        }
        value
    });
    match failure {
        Some(error) => Err(error),
        None => Ok(out.into_owned()),
    }
}

fn missing_error(server_name: &str, missing: &mut [String], what: &str) -> String {
    missing.sort();
    let mut unique: Vec<&str> = missing.iter().map(String::as_str).collect();
    unique.dedup();
    format!(
        "MCP server {}: {what} credential(s) not set: {}. Add the value in Settings → MCP so it is stored in the server's env block.",
        crate::pyjson::py_repr(server_name),
        unique.join(", ")
    )
}

/// `resolve_url`: the endpoint with its placeholders filled. Carries secrets: never log it.
pub fn resolve_url(
    config: &Value,
    server_name: &str,
    process_env: &BTreeMap<String, String>,
    vault: &dyn VaultResolver,
) -> Result<String, String> {
    let raw = config
        .get("url")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if raw.is_empty() {
        return Ok(String::new());
    }
    let empty = Map::new();
    let env = config
        .get("env")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let mut missing = Vec::new();
    let resolved = resolve_placeholders(raw, env, process_env, vault, &mut missing)?;
    if !missing.is_empty() {
        return Err(missing_error(server_name, &mut missing, "endpoint URL"));
    }
    Ok(resolved)
}

/// `resolve_headers`: the header template with its placeholders filled. Carries secrets.
pub fn resolve_headers(
    config: &Value,
    server_name: &str,
    process_env: &BTreeMap<String, String>,
    vault: &dyn VaultResolver,
) -> Result<Vec<(String, String)>, String> {
    let template = match config.get("headers") {
        None | Some(Value::Null) => return Ok(Vec::new()),
        Some(Value::Object(map)) => map,
        Some(other) if !crate::pyos::truthy(Some(other)) => return Ok(Vec::new()),
        Some(other) => {
            return Err(format!(
                "MCP server {}: \"headers\" must be an object, got {}",
                crate::pyjson::py_repr(server_name),
                python_type_name(other)
            ))
        }
    };
    let empty = Map::new();
    let env = config
        .get("env")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let mut missing = Vec::new();
    let mut resolved = Vec::new();
    for (key, raw) in template {
        let raw = crate::pyos::py_str(raw);
        resolved.push((
            key.clone(),
            resolve_placeholders(&raw, env, process_env, vault, &mut missing)?,
        ));
    }
    if !missing.is_empty() {
        return Err(missing_error(server_name, &mut missing, "header"));
    }
    Ok(resolved)
}

fn python_type_name(value: &Value) -> &'static str {
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

/// `redact_url`: mask query-parameter values, keep the endpoint's shape and placeholders.
pub fn redact_url(url: &str) -> String {
    let Some((base, query)) = url.split_once('?') else {
        return url.to_string();
    };
    if query.is_empty() {
        return base.to_string();
    }
    let parts: Vec<String> = query
        .split('&')
        .map(|pair| match pair.split_once('=') {
            None => pair.to_string(),
            Some((key, value)) => {
                let stripped = crate::pyjson::py_strip(value);
                let whole = placeholder()
                    .find(stripped)
                    .is_some_and(|m| m.start() == 0 && m.end() == stripped.len());
                if whole {
                    format!("{key}={value}")
                } else if value.is_empty() {
                    format!("{key}=")
                } else {
                    format!("{key}=***")
                }
            }
        })
        .collect();
    format!("{base}?{}", parts.join("&"))
}

/// `scrub_text`: mask credentials in any free text before it reaches a log or a user.
pub fn scrub_text(text: &str) -> String {
    url_in_text()
        .replace_all(text, |caps: &Captures| redact_url(&caps[0]))
        .into_owned()
}

fn mask_literal(value: &str) -> String {
    let mut rebuilt = String::new();
    let mut last = 0;
    let mut chunks: Vec<(bool, String)> = Vec::new();
    for caps in placeholder().captures_iter(value) {
        let whole = caps.get(0).expect("match");
        chunks.push((false, value[last..whole.start()].to_string()));
        chunks.push((true, caps[1].to_string()));
        last = whole.end();
    }
    chunks.push((false, value[last..].to_string()));
    for (is_var, chunk) in chunks {
        if is_var {
            rebuilt.push_str(&format!("${{{chunk}}}"));
        } else if !chunk.is_empty() {
            rebuilt.push_str(if chunk.chars().count() <= 8 && chunk.contains(' ') {
                &chunk
            } else {
                "***"
            });
        }
    }
    rebuilt
}

/// `redact_config`: fail-closed projection of a stored server config for an API caller.
/// Returns the projection and the unclassified fields that were withheld.
pub fn redact_config(config: &Value) -> (Map<String, Value>, Vec<String>) {
    let mut out = Map::new();
    let mut unknown = Vec::new();
    let empty = Map::new();
    let source = config.as_object().unwrap_or(&empty);
    for (key, value) in source {
        if PUBLIC_CONFIG_FIELDS.contains(&key.as_str()) {
            out.insert(key.clone(), value.clone());
        } else if !SECRET_CONFIG_FIELDS.contains(&key.as_str())
            && !TRANSFORMED_CONFIG_FIELDS.contains(&key.as_str())
        {
            unknown.push(key.clone());
        }
    }
    unknown.sort();
    if let Some(template) = source
        .get("headers")
        .and_then(Value::as_object)
        .filter(|m| !m.is_empty())
    {
        let masked: Map<String, Value> = template
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    Value::String(mask_literal(&crate::pyos::py_str(v))),
                )
            })
            .collect();
        out.insert("headers".into(), Value::Object(masked));
    }
    if let Some(url) = source.get("url").filter(|u| crate::pyos::truthy(Some(u))) {
        out.insert(
            "url".into(),
            Value::String(redact_url(&crate::pyos::py_str(url))),
        );
    }
    (out, unknown)
}

/// `header_env_keys`: env names referenced by the header template or the endpoint URL.
pub fn header_env_keys(config: &Value) -> Vec<String> {
    let mut keys: Vec<String> = Vec::new();
    if let Some(template) = config.get("headers").and_then(Value::as_object) {
        for raw in template.values() {
            keys.extend(
                placeholder()
                    .captures_iter(&crate::pyos::py_str(raw))
                    .map(|c| c[1].to_string()),
            );
        }
    }
    let url = config
        .get("url")
        .filter(|u| crate::pyos::truthy(Some(u)))
        .map(crate::pyos::py_str)
        .unwrap_or_default();
    keys.extend(placeholder().captures_iter(&url).map(|c| c[1].to_string()));
    keys.sort();
    keys.dedup();
    keys
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn redaction_masks_every_credential_carrier() {
        assert_eq!(
            redact_url("https://mcp.amap.com/mcp?key=SECRET&v=${V}&x"),
            "https://mcp.amap.com/mcp?key=***&v=${V}&x"
        );
        assert_eq!(
            scrub_text("failed for url 'https://h/x?key=abc' (status 401)"),
            "failed for url 'https://h/x?key=***' (status 401)"
        );
        let (out, unknown) = redact_config(&json!({
            "command": "npx", "env": {"K": "secret"}, "headers": {"Authorization": "Bearer ${TOKEN}", "X": "lit"},
            "url": "https://h/?k=v", "surprise": 1
        }));
        assert_eq!(
            out["headers"],
            json!({"Authorization": "Bearer ${TOKEN}", "X": "***"})
        );
        assert_eq!(out["url"], "https://h/?k=***");
        assert!(!out.contains_key("env") && !out.contains_key("surprise"));
        assert_eq!(unknown, vec!["surprise"]);
    }

    #[test]
    fn templates_fail_closed_on_missing_credentials() {
        let config = json!({"url": "https://h/mcp?key=${KEY}", "headers": {"A": "Bearer ${TOK}"}, "env": {"TOK": "t"}});
        let env = BTreeMap::new();
        let error = resolve_url(&config, "amap", &env, &NoVault).unwrap_err();
        assert!(error.contains("endpoint URL credential(s) not set: KEY"));
        assert_eq!(
            resolve_headers(&config, "amap", &env, &NoVault).unwrap(),
            vec![("A".into(), "Bearer t".into())]
        );
        assert_eq!(header_env_keys(&config), vec!["KEY", "TOK"]);
        assert_eq!(
            normalize_transport(&json!({"transport": "HTTP"})),
            "streamable-http"
        );
        assert!(is_stdio(&json!({"transport": "typo"})));
    }
}
