//! Setup guidance for curated MCP connectors an owner cannot use yet
//! (`lib/mcp/connector_guidance.py`, the Python oracle).
//!
//! `connector_search_availability` explains a Tool Search that names a
//! curated connector (its catalog id as the namespace, or one of its
//! `aliases` in the query) absent from the task's executable catalog;
//! `connector_for_document_url` / `document_link_guidance` answer a document
//! link only a connector can read. Catalog entries come from `registry.rs`;
//! the owner's MCP configuration rows from the binding (`mcp.config_rows`,
//! the oracle's `load_mcp_config(user_id=...)`); an unreadable configuration
//! makes no claim. Read-only: nothing here starts a bridge or grants authority.

use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::pyjson::py_strip;
use crate::pyos::{py_str, truthy};
use crate::registry::{get_catalog, obtain_credential_spec};
use crate::tool_search::{py_casefold, py_int, py_lower};
use serde_json::{json, Value};
use std::collections::BTreeMap;

const NOT_INSTALLED: &str = "not_installed";
const DISABLED: &str = "disabled";
const INSTALLED: &str = "installed";

/// `_owner_id`: a positive owner, else 0.
pub fn owner_id(value: &Value) -> i64 {
    if !truthy(Some(value)) {
        return 0;
    }
    py_int(value).filter(|owner| *owner > 0).unwrap_or(0)
}

/// `_owner_states`: configured server id → install state; `None` when unreadable.
async fn owner_states(
    binding: &dyn BindingCalls,
    owner: i64,
) -> Option<BTreeMap<String, &'static str>> {
    let config = binding
        .ask("mcp.config_rows", json!({"owner_user_id": owner}))
        .await
        .ok()?;
    let rows = config.as_object()?;
    Some(
        rows.iter()
            .map(|(server_id, row)| {
                let disabled = row.is_object() && row.get("enabled") == Some(&Value::Bool(false));
                (
                    server_id.clone(),
                    if disabled { DISABLED } else { INSTALLED },
                )
            })
            .collect(),
    )
}

fn field(entry: &Value, key: &str) -> String {
    entry.get(key).map(py_str).unwrap_or_default()
}

/// `_token_route`.
fn token_route(entry: &Value) -> String {
    let Some(spec) = obtain_credential_spec(entry) else {
        return String::new();
    };
    let steps = spec
        .get("obtain_steps")
        .filter(|s| truthy(Some(s)))
        .and_then(Value::as_array)
        .map(|steps| steps.iter().map(py_str).collect::<Vec<_>>().join("; "))
        .unwrap_or_default();
    let mut route = format!("get a personal token at {}", field(spec, "obtain_url"));
    if !steps.is_empty() {
        route.push_str(&format!(" ({steps})"));
    }
    route
}

/// `_setup_texts`: `(message, recovery_hint)`.
fn setup_texts(entry: &Value, state: &str) -> (String, String) {
    let (name, server_id) = (field(entry, "name"), field(entry, "id"));
    let route = token_route(entry);
    if state == NOT_INSTALLED {
        return (
            format!(
                "The {name} connector (MCP catalog id '{server_id}') is not installed for this \
                 user, so its tools are unavailable."
            ),
            format!(
                "Ask the user to {}install '{name}' in Settings → MCP{}. Its tools appear on the \
                 next model round after installation.",
                if route.is_empty() {
                    String::new()
                } else {
                    format!("{route}, then ")
                },
                if route.is_empty() {
                    ""
                } else {
                    " with that token"
                },
            ),
        );
    }
    if state == DISABLED {
        return (
            format!("The {name} connector is installed but disabled for this user."),
            "Ask the user to enable it in Settings → MCP; its stored credentials are kept.".into(),
        );
    }
    (
        format!("The {name} connector is installed, but its tools are not available in this task."),
        format!(
            "Ask the user to check it in Settings → MCP, which shows a failed connection or an \
             expired token{}. This task's tool settings may also exclude it.",
            if route.is_empty() {
                String::new()
            } else {
                format!(" ({route}, then reinstall)")
            }
        ),
    )
}

/// `_names_connector`.
fn names_connector(entry: &Value, query: &str, namespace: &str) -> bool {
    if !namespace.is_empty() && namespace == py_lower(&field(entry, "id")) {
        return true;
    }
    entry
        .get("aliases")
        .filter(|a| truthy(Some(a)))
        .and_then(Value::as_array)
        .is_some_and(|aliases| {
            aliases
                .iter()
                .any(|a| query.contains(&py_casefold(&py_str(a))))
        })
}

/// `connector_search_availability`.
pub async fn connector_search_availability(
    binding: &dyn BindingCalls,
    owner_user_id: &Value,
    query: &str,
    namespace: &str,
    usable_namespaces: &[String],
) -> Result<Vec<Value>, CallError> {
    let owner = owner_id(owner_user_id);
    if owner == 0 {
        return Ok(vec![]);
    }
    let namespace = py_lower(py_strip(namespace));
    let query = py_casefold(py_strip(query));
    let usable: Vec<String> = usable_namespaces
        .iter()
        .map(|v| py_lower(py_strip(v)))
        .collect();
    let named: Vec<Value> = get_catalog(binding)
        .await?
        .into_iter()
        .filter(|entry| {
            !usable.contains(&py_lower(&field(entry, "id")))
                && names_connector(entry, &query, &namespace)
        })
        .collect();
    if named.is_empty() {
        return Ok(vec![]);
    }
    let Some(states) = owner_states(binding, owner).await else {
        return Ok(vec![]);
    };
    Ok(named
        .iter()
        .map(|entry| {
            let id = field(entry, "id");
            let state = states.get(&id).copied().unwrap_or(NOT_INSTALLED);
            let (message, recovery) = setup_texts(entry, state);
            json!({
                "capability": "mcp_connector", "namespace": id, "server_id": id,
                "available": false,
                "reason_code": if state == INSTALLED {
                    "mcp_connector_unavailable".to_string()
                } else {
                    format!("mcp_connector_{state}")
                },
                "message": message,
                "recovery_hint": format!(
                    "{recovery} Do not repeat this search until the user confirms the change."),
            })
        })
        .collect())
}

/// `urllib.parse.urlsplit(url)`'s scheme, lowered hostname and path; `None` where it raises.
fn split_url(url: &str) -> Option<(String, Option<String>, String)> {
    let url: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = url.as_str();
    let mut scheme = String::new();
    if let Some(i) = rest.find(':') {
        let head = &rest[..i];
        let first = head.chars().next();
        if i > 0
            && first.is_some_and(|c| c.is_ascii_alphabetic())
            && head
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c))
        {
            scheme = head.to_ascii_lowercase();
            rest = &rest[i + 1..];
        }
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        rest = &after[end..];
        if netloc.contains('[') != netloc.contains(']') {
            return None;
        }
        if netloc.contains('[') {
            let bracketed = netloc.split_once('[')?.1.split_once(']')?.0;
            let valid = if let Some(future) = bracketed.strip_prefix('v') {
                future.split_once('.').is_some_and(|(hex, tail)| {
                    !hex.is_empty()
                        && hex.chars().all(|c| c.is_ascii_hexdigit())
                        && !tail.is_empty()
                })
            } else {
                bracketed
                    .split('%')
                    .next()
                    .is_some_and(|addr| addr.parse::<std::net::Ipv6Addr>().is_ok())
            };
            if !valid {
                return None;
            }
        }
    }
    let path = rest
        .split(['#', '?'])
        .next()
        .unwrap_or_default()
        .to_string();
    let hostinfo = netloc.rsplit('@').next().unwrap_or_default();
    let hostname = if hostinfo.contains('[') {
        hostinfo
            .split_once('[')
            .map(|(_, b)| b.split(']').next().unwrap_or_default())
            .unwrap_or_default()
    } else {
        hostinfo.split(':').next().unwrap_or_default()
    };
    let hostname = (!hostname.is_empty()).then(|| match hostname.split_once('%') {
        Some((host, zone)) => format!("{}%{zone}", py_lower(host)),
        None => py_lower(hostname),
    });
    Some((scheme, hostname, path))
}

/// `connector_for_document_url`.
pub async fn connector_for_document_url(
    binding: &dyn BindingCalls,
    url: &str,
) -> Result<Option<Value>, CallError> {
    let Some((scheme, Some(hostname), path)) = split_url(py_strip(url)) else {
        return Ok(None);
    };
    if scheme != "http" && scheme != "https" {
        return Ok(None);
    }
    let normalized = format!("https://{hostname}{path}");
    Ok(get_catalog(binding).await?.into_iter().find(|entry| {
        entry
            .get("document_url_prefixes")
            .and_then(Value::as_array)
            .is_some_and(|prefixes| prefixes.iter().any(|p| normalized.starts_with(&py_str(p))))
    }))
}

/// `document_link_guidance`.
pub async fn document_link_guidance(
    binding: &dyn BindingCalls,
    owner_user_id: &Value,
    url: &str,
) -> Result<Option<String>, CallError> {
    let entry = connector_for_document_url(binding, url).await?;
    let owner = owner_id(owner_user_id);
    let Some(entry) = entry.filter(|_| owner != 0) else {
        return Ok(None);
    };
    let Some(states) = owner_states(binding, owner).await else {
        return Ok(None);
    };
    let (name, server_id) = (field(&entry, "name"), field(&entry, "id"));
    let head = format!(
        "[{name}] {url} is a {name} document. An anonymous fetch returns only a login page; it \
         is readable only through the user's {name} account."
    );
    let state = states.get(&server_id).copied().unwrap_or(NOT_INSTALLED);
    if state == INSTALLED {
        return Ok(Some(format!(
            "{head} Read it with the {name} connector tools (search_tools namespace \
             '{server_id}') instead of fetch_url. If those tools are missing from this task, ask \
             the user to check the connector in Settings → MCP."
        )));
    }
    let (message, recovery) = setup_texts(&entry, state);
    Ok(Some(format!(
        "{head} {message} {recovery} Do not fetch this link again until the user confirms the \
         change."
    )))
}

/// The `connector_guidance.*` host methods.
pub async fn host_call(
    binding: &dyn BindingCalls,
    method: &str,
    params: &Value,
) -> Result<Value, CallError> {
    let text = |key: &str| {
        params
            .get(key)
            .filter(|v| truthy(Some(v)))
            .map(py_str)
            .unwrap_or_default()
    };
    let owner = params.get("owner").cloned().unwrap_or(Value::Null);
    match method {
        "connector_guidance.search_availability" => {
            let usable: Vec<String> = params
                .get("usable")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(py_str)
                .collect();
            Ok(Value::Array(
                connector_search_availability(
                    binding,
                    &owner,
                    &text("query"),
                    &text("namespace"),
                    &usable,
                )
                .await?,
            ))
        }
        "connector_guidance.for_document_url" => {
            Ok(connector_for_document_url(binding, &text("url"))
                .await?
                .unwrap_or(Value::Null))
        }
        "connector_guidance.document_link_guidance" => {
            Ok(document_link_guidance(binding, &owner, &text("url"))
                .await?
                .map_or(Value::Null, Value::String))
        }
        _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_split_like_urllib() {
        let host = |url: &str| split_url(url).and_then(|(_, h, _)| h);
        assert_eq!(
            host("https://DOCS.QQ.COM/doc/x?tab=1"),
            Some("docs.qq.com".into())
        );
        assert_eq!(split_url("https://a/b?c#d").unwrap().2, "/b");
        assert_eq!(host("not a url"), None);
        assert!(split_url("http://[::1/").is_none());
        assert_eq!(split_url("ftp://docs.qq.com/sheet/x").unwrap().0, "ftp");
    }
}
