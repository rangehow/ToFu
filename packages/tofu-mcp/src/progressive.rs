//! The retired progressive MCP wrappers' search and call helpers
//! (`lib/mcp/progressive.py`, the Python oracle): `search_mcp_tools` ranks
//! enabled definitions and returns bounded schema-rich JSON;
//! `call_mcp_read_tool` / `call_mcp_write_tool` check the tool's read-only
//! class before calling it. MCP tools are search-only now; these remain for
//! the dispatcher, approval and display of rounds recorded under the old
//! wrappers.

use crate::bridge::{Bridge, CallOptions};
use crate::errors::CallError;
use crate::names::parse_namespaced_name;
use crate::pyjson::{self, py_strip};
use crate::pyos::{py_str, truthy};
use crate::result::CallOutput;
use crate::tool_search::{py_int, py_lower, py_word_char};
use serde_json::{json, Map, Value};

pub const MCP_SEARCH_TOOL_NAME: &str = "search_mcp_tools";
pub const MCP_READ_TOOL_NAME: &str = "call_mcp_read_tool";
pub const MCP_WRITE_TOOL_NAME: &str = "call_mcp_write_tool";
/// `_MAX_SEARCH_RESULTS`.
const MAX_SEARCH_RESULTS: i64 = 8;
/// `_SEARCH_MAX_RESULT_CHARS`.
pub const SEARCH_MAX_RESULT_CHARS: usize = 24_000;
/// `_SCHEMA_DESC_MAX`.
const SCHEMA_DESC_MAX: usize = 180;
/// `_SEARCH_SCHEMA_KEYS`.
const SEARCH_SCHEMA_KEYS: [&str; 18] = [
    "type",
    "enum",
    "const",
    "default",
    "required",
    "additionalProperties",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "format",
    "pattern",
    "nullable",
];

const COMPACT: pyjson::Style = pyjson::Style {
    spaced: false,
    sorted: false,
    ascii: false,
};

/// A search page: the model text, and the complete ranked selection when the
/// page dropped candidates for its budget (the oracle's `TrustedResultText`
/// with a canonical `CapturedToolResult`).
#[derive(Debug, PartialEq)]
pub struct SearchPage {
    pub text: String,
    pub canonical: Option<String>,
}

/// `_query_terms`: the whole query, then its `[\w.-]+` runs, deduplicated.
fn query_terms(query: &str) -> Vec<String> {
    let query = py_lower(py_strip(query));
    if query.is_empty() {
        return vec![];
    }
    let mut terms = vec![query.clone()];
    let mut current = String::new();
    for ch in query.chars().chain(std::iter::once(' ')) {
        if py_word_char(ch) || ch == '.' || ch == '-' {
            current.push(ch);
        } else if !current.is_empty() {
            let term = std::mem::take(&mut current);
            if !terms.contains(&term) {
                terms.push(term);
            }
        }
    }
    terms
}

/// `_compact_mcp_schema`.
pub fn compact_mcp_schema(schema: &Value, depth: usize) -> Value {
    let Some(map) = schema.as_object().filter(|_| depth <= 8) else {
        return if depth == 0 {
            json!({"type": "object", "properties": {}})
        } else {
            json!({})
        };
    };
    let mut out: Map<String, Value> = map
        .iter()
        .filter(|(k, _)| SEARCH_SCHEMA_KEYS.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let description = map
        .get("description")
        .filter(|d| truthy(Some(d)))
        .map(py_str)
        .unwrap_or_default();
    if !description.is_empty() {
        let mut text: String = description.chars().take(SCHEMA_DESC_MAX).collect();
        if description.chars().count() > SCHEMA_DESC_MAX {
            text.push('…');
        }
        out.insert("description".into(), Value::String(text));
    }
    if let Some(properties) = map.get("properties").and_then(Value::as_object) {
        out.insert(
            "properties".into(),
            Value::Object(
                properties
                    .iter()
                    .filter(|(_, v)| v.is_object())
                    .map(|(name, v)| (name.clone(), compact_mcp_schema(v, depth + 1)))
                    .collect(),
            ),
        );
    }
    if let Some(items) = map.get("items").filter(|i| i.is_object()) {
        out.insert("items".into(), compact_mcp_schema(items, depth + 1));
    }
    for choice_key in ["oneOf", "anyOf", "allOf"] {
        if let Some(choices) = map.get(choice_key).and_then(Value::as_array) {
            out.insert(
                choice_key.into(),
                Value::Array(
                    choices
                        .iter()
                        .take(16)
                        .filter(|c| c.is_object())
                        .map(|c| compact_mcp_schema(c, depth + 1))
                        .collect(),
                ),
            );
        }
    }
    if depth == 0 {
        out.entry("type").or_insert(json!("object"));
        out.entry("properties").or_insert(json!({}));
    }
    Value::Object(out)
}

/// `search_mcp_catalog` over enabled definitions and their read-only flags.
pub fn search_mcp_catalog(
    defs: &[Value],
    safety: &Map<String, Value>,
    query: &str,
    server: &str,
    limit: &Value,
) -> SearchPage {
    let terms = query_terms(query);
    if terms.is_empty() {
        return SearchPage {
            text: pyjson::dumps(
                &json!({"error": "query must not be empty", "matches": []}),
                pyjson::SPACED,
            ),
            canonical: None,
        };
    }
    let server_filter = py_lower(py_strip(server));
    let result_limit = py_int(limit).map_or(5, |n| n.clamp(1, MAX_SEARCH_RESULTS)) as usize;
    let full_query = &terms[0];
    let mut ranked: Vec<(i64, String, Map<String, Value>, String)> = Vec::new();
    for definition in defs {
        let function = definition
            .get("function")
            .filter(|f| truthy(Some(f)))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let name = function
            .get("name")
            .filter(|n| truthy(Some(n)))
            .map(py_str)
            .unwrap_or_default();
        let Some((server_name, short_name)) = parse_namespaced_name(&name) else {
            continue;
        };
        if !server_filter.is_empty() && py_lower(&server_name) != server_filter {
            continue;
        }
        let description = function
            .get("description")
            .filter(|d| truthy(Some(d)))
            .map(py_str)
            .unwrap_or_default();
        let name_text = py_lower(&format!("{name} {short_name}"));
        let desc_text = py_lower(&description);
        let mut score = 0;
        if *full_query == py_lower(&name) || *full_query == py_lower(&short_name) {
            score += 200;
        }
        if name_text.contains(full_query.as_str()) {
            score += 80;
        } else if desc_text.contains(full_query.as_str()) {
            score += 30;
        }
        for term in &terms[1..] {
            if name_text.contains(term.as_str()) {
                score += 20;
            } else if desc_text.contains(term.as_str()) {
                score += 6;
            }
        }
        if score > 0 {
            ranked.push((score, name, function, server_name));
        }
    }
    ranked.sort_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(&b.1)));
    let matches: Vec<Value> = ranked
        .iter()
        .take(result_limit)
        .map(|(_, name, function, server_name)| {
            let read_only = truthy(safety.get(name));
            let parameters = function
                .get("parameters")
                .filter(|p| truthy(Some(p)))
                .cloned()
                .unwrap_or(json!({"type": "object", "properties": {}}));
            json!({
                "name": name,
                "server": server_name,
                "description": function.get("description").filter(|d| truthy(Some(d))).cloned().unwrap_or(json!("")),
                "input_schema": compact_mcp_schema(&parameters, 0),
                "read_only": read_only,
                "invoke_with": if read_only { MCP_READ_TOOL_NAME } else { MCP_WRITE_TOOL_NAME },
            })
        })
        .collect();
    let mut out = json!({
        "query": query,
        "server": if server.is_empty() { Value::Null } else { json!(server) },
        "matches": matches,
        "instruction": "Call invoke_with using {\"name\": match.name, \"arguments\": {...}}. \
                        Arguments must match input_schema exactly.",
    });
    let full_out = pyjson::dumps(&out, COMPACT);
    let mut truncated = false;
    while out["matches"].as_array().is_some_and(|m| !m.is_empty())
        && pyjson::dumps(&out, COMPACT).chars().count() > SEARCH_MAX_RESULT_CHARS
    {
        if let Some(matches) = out["matches"].as_array_mut() {
            matches.pop();
        }
        out["truncated_for_budget"] = json!(true);
        truncated = true;
    }
    if truncated {
        let instruction = format!(
            "{} The page hit its bounded output budget; narrow the query for the omitted candidates.",
            out["instruction"].as_str().unwrap_or_default()
        );
        out["instruction"] = json!(instruction);
        return SearchPage {
            text: pyjson::dumps(&out, COMPACT),
            canonical: Some(full_out),
        };
    }
    SearchPage {
        text: pyjson::dumps(&out, COMPACT),
        canonical: None,
    }
}

/// What a progressive wrapper call produced.
pub enum ProgressiveOutcome {
    /// The wrapper refused before any call (the oracle's error string).
    Refused(String),
    Called(CallOutput),
}

/// `call_progressive_mcp`: validate the wrapper's safety class, then call.
pub async fn call_progressive_mcp(
    bridge: &Bridge,
    wrapper_name: &str,
    namespaced_name: &str,
    arguments: &Value,
) -> Result<ProgressiveOutcome, CallError> {
    let name = py_strip(namespaced_name);
    if parse_namespaced_name(name).is_none() {
        return Ok(ProgressiveOutcome::Refused(
            "MCP tool error: invalid namespaced name. Run search_mcp_tools and use the exact \
             mcp__server__tool value it returns."
                .into(),
        ));
    }
    if !arguments.is_object() {
        return Ok(ProgressiveOutcome::Refused(
            "MCP tool error: arguments must be a JSON object.".into(),
        ));
    }
    let Some(info) = bridge.get_tool_info(name) else {
        return Ok(ProgressiveOutcome::Refused(
            "MCP tool error: tool is unavailable or unknown. Run search_mcp_tools again to \
             refresh the enabled catalog."
                .into(),
        ));
    };
    let read_only = truthy(info.get("read_only_hint"));
    if wrapper_name == MCP_READ_TOOL_NAME && !read_only {
        return Ok(ProgressiveOutcome::Refused(format!(
            "MCP tool error: {name} is not annotated read-only; use {MCP_WRITE_TOOL_NAME} so \
             serialization and approval apply."
        )));
    }
    if wrapper_name == MCP_WRITE_TOOL_NAME && read_only {
        return Ok(ProgressiveOutcome::Refused(format!(
            "MCP tool error: {name} is read-only; use {MCP_READ_TOOL_NAME} for the parallel safe path."
        )));
    }
    bridge
        .call_tool(name, arguments.clone(), CallOptions::default())
        .await
        .map(ProgressiveOutcome::Called)
}

/// The `progressive.*` host methods. A search names explicit `defs`/`safety`
/// or reads the owner bridge's enabled catalog.
pub async fn host_call(bridge: &Bridge, method: &str, params: &Value) -> Result<Value, CallError> {
    let text = |key: &str| {
        params
            .get(key)
            .filter(|v| truthy(Some(v)))
            .map(py_str)
            .unwrap_or_default()
    };
    match method {
        "progressive.search" => {
            let defs = match params.get("defs").and_then(Value::as_array) {
                Some(defs) => defs.clone(),
                None => bridge.get_openai_tool_defs(),
            };
            let safety = match params.get("safety").and_then(Value::as_object) {
                Some(safety) => safety.clone(),
                None => bridge.get_tool_safety(),
            };
            let page = search_mcp_catalog(
                &defs,
                &safety,
                &text("query"),
                &text("server"),
                params.get("limit").unwrap_or(&json!(5)),
            );
            Ok(json!({"text": page.text, "canonical": page.canonical}))
        }
        "progressive.compact_schema" => Ok(compact_mcp_schema(
            params.get("schema").unwrap_or(&Value::Null),
            0,
        )),
        "progressive.call" => Ok(
            match call_progressive_mcp(
                bridge,
                &text("wrapper"),
                &text("name"),
                params.get("arguments").unwrap_or(&Value::Null),
            )
            .await?
            {
                ProgressiveOutcome::Refused(message) => json!({"refused": message}),
                ProgressiveOutcome::Called(output) => json!({"result": output.relay_encode()}),
            },
        ),
        _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn def(name: &str, description: &str) -> Value {
        json!({"type": "function", "function": {"name": name, "description": description,
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}})
    }

    #[test]
    fn search_ranks_and_labels_the_wrapper() {
        let defs = vec![
            def("mcp__github__search_issues", "Search GitHub issues"),
            def("mcp__github__create_issue", "Create a GitHub issue"),
        ];
        let safety: Map<String, Value> = [("mcp__github__search_issues".to_string(), json!(true))]
            .into_iter()
            .collect();
        let page = search_mcp_catalog(&defs, &safety, "search issues", "", &json!(5));
        let result: Value = serde_json::from_str(&page.text).unwrap();
        assert_eq!(result["matches"][0]["name"], "mcp__github__search_issues");
        assert_eq!(result["matches"][0]["invoke_with"], MCP_READ_TOOL_NAME);
        assert!(page.canonical.is_none());
    }

    #[test]
    fn budget_page_keeps_the_full_selection_canonical() {
        let defs: Vec<Value> = (0..12)
            .map(|i| {
                def(
                    &format!("mcp__srv__match_{i}"),
                    &"match capability guidance. ".repeat(160),
                )
            })
            .collect();
        let page = search_mcp_catalog(&defs, &Map::new(), "match", "", &json!(12));
        let model: Value = serde_json::from_str(&page.text).unwrap();
        let full: Value = serde_json::from_str(page.canonical.as_deref().unwrap()).unwrap();
        assert_eq!(model["truncated_for_budget"], true);
        assert!(
            full["matches"].as_array().unwrap().len() > model["matches"].as_array().unwrap().len()
        );
    }

    #[test]
    fn compact_schema_bounds_descriptions() {
        let schema = json!({"type": "object", "description": "x".repeat(200), "title": "dropped",
                            "properties": {"a": {"type": "string"}, "b": 3}});
        let out = compact_mcp_schema(&schema, 0);
        assert_eq!(out["description"].as_str().unwrap().chars().count(), 181);
        assert!(out.get("title").is_none());
        assert_eq!(out["properties"], json!({"a": {"type": "string"}}));
    }
}
