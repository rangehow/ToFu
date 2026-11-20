//! Curated MCP catalog facts (`lib/mcp/registry.py`, the Python oracle):
//! the build edition predicate and the catalog filter it drives.
//!
//! The entries come from the binding (`mcp.registry_catalog`, the oracle's
//! `CATALOG` after `_reload_catalog_if_changed`) rather than a generated copy:
//! `CATALOG` is not a static literal — installed bundles extend it from their
//! `mcp_catalog/*.json` at import, and an edit of `registry.py` hot-reloads it
//! in the running process — so a frozen artifact would drift from both.
//! The edition is decided here, from the same environment variable the oracle
//! reads once at import.

use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::pyjson::py_strip;
use crate::pyos::truthy;
use crate::tool_search::py_lower;
use serde_json::{json, Value};
use std::sync::OnceLock;

/// The environment variable the oracle's `_OPENSOURCE_BUILD` reads.
pub const OPENSOURCE_BUILD_ENV: &str = "TOFU_OPENSOURCE_BUILD";
/// The edition when the variable is unset; an opensource export bakes `"1"`
/// here, like the oracle's `_OPENSOURCE_BUILD` default (export_pkg/_sanitize.py).
pub const OPENSOURCE_BUILD_DEFAULT: &str = "1";

/// `_OPENSOURCE_BUILD` of a given environment value.
pub fn opensource_build_from(value: Option<&str>) -> bool {
    matches!(
        py_lower(py_strip(value.unwrap_or(OPENSOURCE_BUILD_DEFAULT))).as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// `is_opensource_build`: read once per process, like the oracle's import.
pub fn is_opensource_build() -> bool {
    static EDITION: OnceLock<bool> = OnceLock::new();
    *EDITION
        .get_or_init(|| opensource_build_from(std::env::var(OPENSOURCE_BUILD_ENV).ok().as_deref()))
}

/// `CATALOG` as the binding holds it (unfiltered).
async fn raw_catalog(binding: &dyn BindingCalls) -> Result<Vec<Value>, CallError> {
    match binding.ask("mcp.registry_catalog", json!({})).await? {
        Value::Array(entries) => Ok(entries),
        other => Err(CallError::Error(format!(
            "mcp.registry_catalog answered a non-list: {}",
            crate::pyos::py_str(&other)
        ))),
    }
}

fn hidden(entry: &Value, opensource: bool) -> bool {
    opensource && truthy(entry.get("internal_only"))
}

/// `get_catalog`: an opensource build hides `internal_only` entries.
pub async fn get_catalog(binding: &dyn BindingCalls) -> Result<Vec<Value>, CallError> {
    let opensource = is_opensource_build();
    Ok(raw_catalog(binding)
        .await?
        .into_iter()
        .filter(|entry| !hidden(entry, opensource))
        .collect())
}

/// `get_catalog_entry`: the last entry with this id (`_CATALOG_INDEX`), unless hidden.
pub async fn get_catalog_entry(
    binding: &dyn BindingCalls,
    server_id: &str,
) -> Result<Option<Value>, CallError> {
    let opensource = is_opensource_build();
    Ok(raw_catalog(binding)
        .await?
        .into_iter()
        .rfind(|entry| entry.get("id").and_then(Value::as_str) == Some(server_id))
        .filter(|entry| !hidden(entry, opensource)))
}

/// `obtain_credential_spec`: the first required credential with a token page.
pub fn obtain_credential_spec(entry: &Value) -> Option<&Value> {
    entry
        .get("env_specs")
        .and_then(Value::as_array)?
        .iter()
        .find(|spec| truthy(spec.get("required")) && truthy(spec.get("obtain_url")))
}

/// The `registry.*` host methods.
pub async fn host_call(
    binding: &dyn BindingCalls,
    method: &str,
    params: &Value,
) -> Result<Value, CallError> {
    match method {
        "registry.is_opensource_build" => Ok(json!(is_opensource_build())),
        "registry.catalog" => Ok(Value::Array(get_catalog(binding).await?)),
        "registry.catalog_entry" => Ok(get_catalog_entry(
            binding,
            params
                .get("server_id")
                .and_then(Value::as_str)
                .unwrap_or_default(),
        )
        .await?
        .unwrap_or(Value::Null)),
        _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_edition_follows_the_oracle_spellings() {
        for value in ["1", "true", " YES ", "On"] {
            assert!(opensource_build_from(Some(value)), "{value}");
        }
        for value in ["", "0", "no", "enabled"] {
            assert!(!opensource_build_from(Some(value)), "{value}");
        }
        assert!(!opensource_build_from(None));
    }
}
