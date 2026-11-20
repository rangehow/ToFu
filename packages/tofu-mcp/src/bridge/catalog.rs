//! Catalog projections and generations of the bridge (`MCPBridge`'s catalog
//! reads in `lib/mcp/client/_bridge.py`).
//!
//! The model-visible projection is the enabled rows of the tool index sorted
//! by namespaced name; the generation (`State::generation`) advances on every
//! change of it. Python caches each projection per generation; here they are
//! derived under one lock, and the binding (`lib/mcp/rust_host.py`) keeps the
//! per-generation objects (`catalog.projection` / `catalog.frozen_defs` answer
//! only the generation when the caller's is current).

use super::{disabled_tools, Bridge, State};
use crate::errors::CallError;
use crate::tool_search::{catalog_search_text_by_name, catalog_snapshot_fingerprint};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;

/// `catalog_snapshot_row`: one public snapshot row of a catalog row.
pub fn catalog_snapshot_row(row: &Value) -> Value {
    let text = |key: &str| {
        row.get(key)
            .filter(|v| crate::pyos::truthy(Some(v)))
            .map_or(String::new(), crate::pyos::py_str)
    };
    let dict = |key: &str| {
        row.get(key)
            .and_then(Value::as_object)
            .cloned()
            .map_or(json!({}), Value::Object)
    };
    json!({
        "server_id": row["server_name"],
        "server_name": row["server_name"],
        "tool_name": row["tool_name"],
        "namespaced_name": row["namespaced_name"],
        "description": row.get("description").cloned().unwrap_or(json!("")),
        "openai_def": row["openai_def"],
        "read_only_hint": crate::pyos::truthy(row.get("read_only_hint")),
        "annotations": dict("annotations"),
        "output_schema": dict("output_schema"),
        "meta": dict("meta"),
        "schema_hash": text("schema_hash"),
        "catalog_version": text("catalog_version"),
    })
}

/// `_tool_catalog_rows_locked`: enabled rows sorted by namespaced name.
fn catalog_rows(state: &State) -> Vec<Value> {
    state
        .tool_index
        .values()
        .filter(|info| {
            !disabled_tools(
                &state.configs,
                info["server_name"].as_str().unwrap_or_default(),
            )
            .contains(info["tool_name"].as_str().unwrap_or_default())
        })
        .cloned()
        .collect()
}

impl Bridge {
    /// `get_enabled_tool_summary`: per-server counts of model-visible tools.
    pub fn get_enabled_tool_summary(&self) -> Value {
        let state = self.lock();
        let mut counts: BTreeMap<&String, u64> = state.servers.keys().map(|n| (n, 0)).collect();
        for info in state.tool_index.values() {
            let server = info["server_name"].as_str().unwrap_or_default();
            let Some(count) = counts.get_mut(&server.to_string()) else {
                continue;
            };
            if !disabled_tools(&state.configs, server)
                .contains(info["tool_name"].as_str().unwrap_or_default())
            {
                *count += 1;
            }
        }
        let servers: Vec<Value> = counts
            .iter()
            .filter(|(_, count)| **count > 0)
            .map(|(name, count)| json!({"name": name, "count": count}))
            .collect();
        let total: u64 = counts.values().sum();
        json!({"servers": servers, "total": total})
    }

    /// `get_frozen_openai_tool_defs`: the generation and its definitions.
    pub fn get_frozen_openai_tool_defs(&self) -> (u64, Vec<Value>) {
        let state = self.lock();
        let defs = catalog_rows(&state)
            .into_iter()
            .map(|row| row["openai_def"].clone())
            .collect();
        (state.generation, defs)
    }

    /// `get_tool_catalog_snapshot`.
    pub fn get_tool_catalog_snapshot(&self) -> Vec<Value> {
        catalog_rows(&self.lock())
            .iter()
            .map(catalog_snapshot_row)
            .collect()
    }

    /// `get_tool_catalog_projection`: the internal rows and their fingerprint.
    pub fn get_tool_catalog_projection(&self) -> (String, Vec<Value>) {
        let rows = catalog_rows(&self.lock());
        (catalog_snapshot_fingerprint(&rows), rows)
    }

    /// `get_tool_catalog_search_text_projection`.
    pub fn get_tool_catalog_search_text_projection(&self) -> Map<String, Value> {
        catalog_search_text_by_name(&catalog_rows(&self.lock()))
    }

    /// `is_mcp_tool`: any discovered tool, disabled included.
    pub fn is_mcp_tool(&self, name: &str) -> bool {
        self.lock().tool_index.contains_key(name)
    }

    /// `server_count`, `tool_count` and `connected`.
    pub fn catalog_counts(&self) -> Value {
        let state = self.lock();
        json!({
            "server_count": state.servers.len(),
            "tool_count": state.tool_index.len(),
            "connected": state.started && !state.servers.is_empty(),
        })
    }

    /// The `catalog.*` host methods over this bridge.
    pub async fn catalog_host_call(
        &self,
        method: &str,
        params: &Value,
    ) -> Result<Value, CallError> {
        let known = params.get("known").and_then(Value::as_u64);
        match method {
            "catalog.enabled_tool_summary" => Ok(self.get_enabled_tool_summary()),
            "catalog.frozen_defs" => {
                let (generation, defs) = self.get_frozen_openai_tool_defs();
                Ok(if known == Some(generation) {
                    json!({"generation": generation})
                } else {
                    json!({"generation": generation, "defs": defs})
                })
            }
            "catalog.snapshot" => Ok(Value::Array(self.get_tool_catalog_snapshot())),
            "catalog.projection" => {
                let (generation, rows) = {
                    let state = self.lock();
                    (state.generation, catalog_rows(&state))
                };
                if known == Some(generation) {
                    return Ok(json!({"generation": generation}));
                }
                Ok(json!({
                    "generation": generation,
                    "fingerprint": catalog_snapshot_fingerprint(&rows),
                    "search_text": catalog_search_text_by_name(&rows),
                    "rows": rows,
                }))
            }
            "catalog.search_text" => Ok(Value::Object(
                self.get_tool_catalog_search_text_projection(),
            )),
            "catalog.is_mcp_tool" => Ok(json!(self.is_mcp_tool(
                params
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
            ))),
            "catalog.counts" => Ok(self.catalog_counts()),
            _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
        }
    }
}
