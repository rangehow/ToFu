//! Route facts that shape a request before protocol translation: declared
//! gateway traits (`lib/provider/gateway_profiles.py`), selected-route
//! features (`lib/llm/route_features.py`), model-family predicates
//! (`lib/model_info/_family.py`), prompt-cache affinity
//! (`lib/llm/cache_affinity.py`), the tool-name budget
//! (`lib/llm/tool_wire.py`), Anthropic cache breakpoints
//! (`lib/llm/cache.py`). Tool-schema projection lives in `schema.rs`.
//!
//! Environment overrides Python reads (`TOFU_CACHE_MID_MODE`,
//! `TOFU_CACHE_MARKERS_NONCLAUDE`) are fixed at their defaults: the mid-history
//! anchor is dropped and non-Claude markers follow the model family.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::canonical::{Result, Unsupported};
use crate::pyjson::{py_blank, truthy};

type Msg = Map<String, Value>;

fn unsupported<T>(what: &str) -> Result<T> {
    Err(Unsupported(what.to_owned()).into())
}

/// Everything about the selected route that `prepare_request` reads besides
/// the canonical body.
#[derive(Clone, Copy)]
pub struct RouteSpec<'a> {
    pub base_url: &'a str,
    pub api_key: &'a str,
    /// Validated `tofu.gateway-profile/v1` documents (`id`, `hosts`,
    /// `provider_ids`, `traits`), in bundle order.
    pub gateway_profiles: &'a [Value],
    /// The selected route's inference profile (`_route_inference_profile`).
    pub inference_profile: Option<&'a Value>,
    /// `lib.CACHE_EXTENDED_TTL` (or the task's latched value).
    pub extended_cache_ttl: bool,
    /// The request lineage (`_cache_affinity_scope`, `_conv_id` or `_task_id`).
    pub lineage: &'a str,
    /// The process working directory a `working_directory_header` reports.
    pub working_dir: &'a str,
}

impl<'a> RouteSpec<'a> {
    pub fn plain(base_url: &'a str, api_key: &'a str) -> Self {
        Self {
            base_url,
            api_key,
            gateway_profiles: &[],
            inference_profile: None,
            extended_cache_ttl: false,
            lineage: "",
            working_dir: "/",
        }
    }
}

// ── model families ─────────────────────────────────────────────────────────

pub fn is_claude(model: &str) -> bool {
    let m = model.to_lowercase();
    m.contains("claude") || m.contains("anthropic") || m.contains("fable")
}

pub fn is_kimi(model: &str) -> bool {
    let m = model.to_lowercase();
    m.contains("kimi") || m.contains("moonshot")
}

// ── gateway profiles ───────────────────────────────────────────────────────

/// `urlparse(base_url).hostname`, lowercased.
fn hostname(url: &str) -> String {
    let rest = url.split_once("://").map_or("", |(_, rest)| rest);
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let host_port = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
    let host = if let Some(stripped) = host_port.strip_prefix('[') {
        stripped.split(']').next().unwrap_or("")
    } else {
        host_port.split(':').next().unwrap_or("")
    };
    host.to_lowercase()
}

/// `gateway_trait(trait, base_url=...)`: the first matching profile's value.
pub fn gateway_trait<'v>(profiles: &'v [Value], name: &str, base_url: &str) -> Option<&'v Value> {
    let host = if base_url.is_empty() {
        String::new()
    } else {
        hostname(base_url)
    };
    if host.is_empty() {
        return None;
    }
    profiles.iter().find_map(|profile| {
        let value = profile.get("traits")?.get(name)?;
        let matches = profile
            .get("hosts")?
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .any(|pattern| match pattern.strip_prefix('.') {
                Some(domain) => host == domain || host.ends_with(pattern),
                None => host == pattern,
            });
        matches.then_some(value)
    })
}

// ── route features ─────────────────────────────────────────────────────────

/// Features this port evaluates; any other declaration refuses, since the
/// Python adapters may act on it.
const KNOWN_FEATURES: [&str; 3] = [
    "cache_affinity_mt_session_id",
    "chat_cache_breakpoints",
    "responses_reasoning_replay",
];

pub fn check_profile(profile: Option<&Value>) -> Result<()> {
    let Some(profile) = profile else {
        return Ok(());
    };
    let Value::Object(map) = profile else {
        return unsupported("non-object inference profile");
    };
    for (key, value) in map {
        match key.as_str() {
            "features" => {
                let Value::Object(features) = value else {
                    return unsupported("non-object profile features");
                };
                if let Some(name) = features
                    .keys()
                    .find(|name| !KNOWN_FEATURES.contains(&name.as_str()))
                {
                    return unsupported(&format!("route feature {name}"));
                }
            }
            "reasoning" | "sampling" if value.is_object() => {}
            "source" => {}
            other => return unsupported(&format!("inference profile field {other}")),
        }
    }
    Ok(())
}

fn declared_feature<'v>(profile: Option<&'v Value>, feature: &str) -> Option<&'v Value> {
    profile?.get("features")?.get(feature)
}

/// `route_feature` for an api-key route: `None` without a profile, else the
/// closed-world declaration bound to the feature's wire.
pub fn route_feature(profile: Option<&Value>, feature: &str, protocol: &str) -> Option<bool> {
    profile?;
    let wire = match feature {
        "cache_affinity_mt_session_id" => None,
        "chat_cache_breakpoints" => Some("openai"),
        "responses_reasoning_replay" => Some("responses"),
        _ => return Some(false),
    };
    if wire.is_some_and(|wire| wire != protocol) {
        return Some(false);
    }
    Some(declared_feature(profile, feature).and_then(Value::as_str) == Some("supported"))
}

// ── prompt-cache affinity ──────────────────────────────────────────────────

/// `cache_affinity_key`: a UUID-shaped SHA-256 digest of the lineage.
pub fn cache_affinity_key(lineage: &str) -> String {
    if lineage.is_empty() {
        return String::new();
    }
    let digest = Sha256::digest(format!("tofu-responses-cache:{lineage}").as_bytes());
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    bytes[6] = (bytes[6] & 0x0f) | 0x50;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

/// `merge_cache_affinity_headers` for the one declared carrier.
pub fn merge_affinity_headers(
    headers: &mut Vec<(String, String)>,
    profile: Option<&Value>,
    protocol: &str,
    lineage: &str,
) {
    let key = cache_affinity_key(lineage);
    if key.is_empty()
        || route_feature(profile, "cache_affinity_mt_session_id", protocol) != Some(true)
        || headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("mt-session-id"))
    {
        return;
    }
    headers.push(("Mt-Session-Id".to_owned(), key));
}

// ── tool-name budget ───────────────────────────────────────────────────────

/// Names that are never hidden (`GATEWAY_TOOL_NAMES | BACKGROUND_TOOL_NAMES`;
/// a body without an executable catalog declares no required direct tools).
const NAME_FLOOR: [&str; 5] = [
    "search_tools",
    "execute_tools",
    "execute_program",
    "poll",
    "kill",
];

pub fn provider_tool_name(tool: &Value) -> &str {
    match tool.get("function") {
        Some(Value::Object(function)) => function.get("name").and_then(Value::as_str).unwrap_or(""),
        _ => tool.get("name").and_then(Value::as_str).unwrap_or(""),
    }
}

fn name_bytes(name: &str) -> usize {
    name.len() + 1
}

/// `provider_tool_name_budget_bytes`: 0 means no budget.
pub fn tool_name_budget_bytes(route: &RouteSpec<'_>, protocol: &str, model: &str) -> usize {
    if protocol != "anthropic" || !is_claude(model) {
        return 0;
    }
    gateway_trait(
        route.gateway_profiles,
        "anthropic_claude_tool_name_budget_bytes",
        route.base_url,
    )
    .and_then(Value::as_u64)
    .unwrap_or(0) as usize
}

/// `fit_tool_name_budget` behind the discovery pair: hide the longest
/// optional names (later wire position first on ties) until the joined names
/// fit; a request without both discovery tools keeps every tool.
pub fn fit_tool_name_budget(tools: Vec<Value>, budget: usize) -> Vec<Value> {
    if budget == 0 {
        return tools;
    }
    let names: Vec<&str> = tools.iter().map(provider_tool_name).collect();
    if !(names.contains(&"search_tools") && names.contains(&"execute_tools")) {
        return tools;
    }
    let mut total: usize = names
        .iter()
        .filter(|n| !n.is_empty())
        .map(|n| name_bytes(n))
        .sum();
    if total <= budget {
        return tools;
    }
    let mut optional: Vec<(usize, &str)> = names
        .iter()
        .enumerate()
        .filter(|(_, name)| !name.is_empty() && !NAME_FLOOR.contains(name))
        .map(|(index, name)| (index, *name))
        .collect();
    optional.sort_by(|a, b| {
        name_bytes(b.1)
            .cmp(&name_bytes(a.1))
            .then_with(|| b.0.cmp(&a.0))
    });
    let mut hidden = Vec::new();
    for (index, name) in optional {
        if total <= budget {
            break;
        }
        hidden.push(index);
        total -= name_bytes(name);
    }
    tools
        .into_iter()
        .enumerate()
        .filter(|(index, _)| !hidden.contains(index))
        .map(|(_, tool)| tool)
        .collect()
}

// ── cache breakpoints ──────────────────────────────────────────────────────

const MAX_CACHE_BP: usize = 4;
const MARKERS_HELP: [&str; 3] = ["glm-5", "qwen", "deepseek"];
const PREFILL_SENTINEL: &str = "[Your previous response for context]:";

/// `_gateway_honors_cache_markers`.
pub fn honors_cache_markers(model: &str, profile: Option<&Value>, protocol: &str) -> bool {
    let declared = declared_feature(profile, "chat_cache_breakpoints");
    if protocol == "openai" && declared.is_some_and(|value| value.as_str() != Some("supported")) {
        return false;
    }
    if is_claude(model) {
        return true;
    }
    let lowered = model.to_lowercase();
    MARKERS_HELP.iter().any(|family| lowered.contains(family))
}

fn role(message: &Value) -> &str {
    message.get("role").and_then(Value::as_str).unwrap_or("")
}

fn is_prefill_converted(message: &Value) -> bool {
    if role(message) != "user" {
        return false;
    }
    let text = match message.get("content") {
        Some(Value::String(text)) => text.as_str(),
        Some(Value::Array(blocks)) if blocks.first().is_some_and(Value::is_object) => {
            blocks[0].get("text").and_then(Value::as_str).unwrap_or("")
        }
        _ => return false,
    };
    text.starts_with(PREFILL_SENTINEL)
}

fn with_content(message: &Value, content: Value) -> Value {
    let mut copy = message.as_object().cloned().unwrap_or_default();
    copy.insert("content".to_owned(), content);
    Value::Object(copy)
}

fn marked_text(message: &Value, text: &str, marker: &Value) -> Value {
    with_content(
        message,
        json!([{"type": "text", "text": text, "cache_control": marker}]),
    )
}

fn marked_last(message: &Value, blocks: &[Value], marker: &Value) -> Value {
    let mut blocks = blocks.to_vec();
    if let Some(Value::Object(last)) = blocks.last_mut() {
        last.insert("cache_control".to_owned(), marker.clone());
    }
    with_content(message, Value::Array(blocks))
}

/// `add_cache_breakpoints` with the default `drop` mid-anchor layout.
pub fn add_cache_breakpoints(body: &mut Msg, route: &RouteSpec<'_>, protocol: &str) {
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if !honors_cache_markers(&model, route.inference_profile, protocol) {
        return;
    }
    let skip_tool_marking = protocol != "anthropic";
    let stable = if route.extended_cache_ttl && is_claude(&model) {
        json!({"type": "ephemeral", "ttl": "1h"})
    } else {
        json!({"type": "ephemeral"})
    };
    let tail = json!({"type": "ephemeral"});
    let mut messages: Vec<Value> = match body.get("messages") {
        Some(Value::Array(messages)) => messages.clone(),
        _ => Vec::new(),
    };
    let mut tools: Option<Vec<Value>> = match body.get("tools") {
        Some(Value::Array(tools)) => Some(tools.clone()),
        _ => None,
    };

    // Phase 0: strip every existing marker.
    for message in messages.iter_mut() {
        if let Some(Value::Array(blocks)) = message.get_mut("content") {
            for block in blocks.iter_mut() {
                if let Value::Object(block) = block {
                    block.shift_remove("cache_control");
                }
            }
        }
    }
    for tool in tools.iter_mut().flatten() {
        if let Some(Value::Object(function)) = tool.get_mut("function") {
            function.shift_remove("cache_control");
        }
    }

    // Phase 0.5: one representation whether or not a message is anchored.
    for message in messages.iter_mut() {
        match role(message) {
            "tool" | "user" => {
                if let Some(Value::String(text)) = message.get("content") {
                    if !text.is_empty() {
                        let wrapped = json!([{"type": "text", "text": text}]);
                        *message = with_content(message, wrapped);
                    }
                }
            }
            "assistant" => {
                let empty_with_calls = truthy(message.get("tool_calls"))
                    && match message.get("content") {
                        None => false,
                        Some(Value::Null) => true,
                        Some(Value::String(text)) => py_blank(text),
                        Some(Value::Array(blocks)) => blocks.is_empty(),
                        Some(_) => false,
                    };
                if empty_with_calls {
                    if let Value::Object(map) = message {
                        map.shift_remove("content");
                    }
                }
                if let Some(Value::String(text)) = message.get("content") {
                    if !text.is_empty() {
                        let wrapped = json!([{"type": "text", "text": text}]);
                        *message = with_content(message, wrapped);
                    }
                }
            }
            _ => {}
        }
    }

    let has_tools = tools.as_ref().is_some_and(|tools| !tools.is_empty());
    let mut reserve = 0;
    if has_tools {
        reserve += 1;
    }
    if messages.len() >= 2 {
        reserve += 1;
    }
    let system_budget = MAX_CACHE_BP.saturating_sub(reserve);
    let mut bp = 0;

    for slot in messages.iter_mut() {
        if role(slot) != "system" || bp >= system_budget {
            continue;
        }
        let message = slot.clone();
        match message.get("content") {
            Some(Value::String(text)) if !py_blank(text) => {
                *slot = marked_text(&message, text, &stable);
                bp += 1;
            }
            Some(Value::Array(blocks)) if !blocks.is_empty() => {
                let text_indexes: Vec<usize> = blocks
                    .iter()
                    .enumerate()
                    .filter(|(_, b)| b.get("type").and_then(Value::as_str) == Some("text"))
                    .map(|(i, _)| i)
                    .collect();
                let slots = system_budget - bp;
                let chosen = &text_indexes[text_indexes.len().saturating_sub(slots)..];
                if !chosen.is_empty() {
                    let mut blocks = blocks.clone();
                    for &i in chosen {
                        if let Value::Object(block) = &mut blocks[i] {
                            block.insert("cache_control".to_owned(), stable.clone());
                        }
                    }
                    *slot = with_content(&message, Value::Array(blocks));
                    bp += chosen.len();
                }
            }
            _ => {}
        }
    }

    if let Some(tools) = tools.as_mut().filter(|tools| !tools.is_empty()) {
        if bp < MAX_CACHE_BP {
            let last = tools.last_mut().expect("non-empty");
            if let Some(Value::Object(function)) = last.get_mut("function") {
                if !function.is_empty() {
                    function.insert("cache_control".to_owned(), stable.clone());
                    bp += 1;
                }
            }
        }
    }

    if messages.len() >= 2 && bp < MAX_CACHE_BP {
        let window = if skip_tool_marking {
            messages.len()
        } else {
            messages.len().min(6)
        };
        for offset in 1..window {
            let index = messages.len() - offset;
            if index == 0 {
                break;
            }
            let message = messages[index].clone();
            if role(&message) == "system" {
                break;
            }
            if is_prefill_converted(&message) || (skip_tool_marking && role(&message) == "tool") {
                continue;
            }
            match message.get("content") {
                Some(Value::String(text)) if !text.is_empty() => {
                    messages[index] = marked_text(&message, text, &tail);
                    break;
                }
                Some(Value::Array(blocks)) if blocks.last().is_some_and(Value::is_object) => {
                    messages[index] = marked_last(&message, blocks, &tail);
                    break;
                }
                _ => {}
            }
        }
    }

    if body.contains_key("messages") {
        body.insert("messages".to_owned(), Value::Array(messages));
    }
    if let Some(tools) = tools {
        body.insert("tools".to_owned(), Value::Array(tools));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn affinity_key_is_a_version_5_uuid_of_the_lineage() {
        let key = cache_affinity_key("conv-1");
        assert_eq!(key.len(), 36);
        assert_eq!(&key[14..15], "5");
        assert_eq!(cache_affinity_key(""), "");
    }

    #[test]
    fn hosts_match_exactly_or_on_a_dot_boundary() {
        let profiles = vec![
            json!({"hosts": [".example.com"], "traits": {"t": 1}}),
            json!({"hosts": ["exact.org"], "traits": {"t": 2}}),
        ];
        assert_eq!(
            gateway_trait(&profiles, "t", "https://a.example.com/v1"),
            Some(&json!(1))
        );
        assert_eq!(
            gateway_trait(&profiles, "t", "https://EXAMPLE.com"),
            Some(&json!(1))
        );
        assert_eq!(
            gateway_trait(&profiles, "t", "https://badexample.com"),
            None
        );
        assert_eq!(
            gateway_trait(&profiles, "t", "http://exact.org:8080/x"),
            Some(&json!(2))
        );
    }

    #[test]
    fn budget_hides_longest_optional_names_only_behind_discovery() {
        let tool = |name: &str| json!({"type": "function", "function": {"name": name}});
        let tools = vec![
            tool("search_tools"),
            tool("execute_tools"),
            tool("aaaaaaaaaa"),
            tool("bbbbbbbbbb"),
            tool("cc"),
        ];
        let kept = fit_tool_name_budget(tools.clone(), 41);
        let names: Vec<&str> = kept.iter().map(provider_tool_name).collect();
        assert_eq!(names, ["search_tools", "execute_tools", "aaaaaaaaaa", "cc"]);
        let undiscoverable: Vec<Value> = tools[2..].to_vec();
        assert_eq!(
            fit_tool_name_budget(undiscoverable.clone(), 5),
            undiscoverable
        );
    }

    #[test]
    fn doubao_and_minimax_never_get_markers() {
        for model in ["Doubao-Seed-2.0-pro", "MiniMax-M2.5", "kimi-k3"] {
            assert!(!honors_cache_markers(model, None, "openai"), "{model}");
            assert!(!honors_cache_markers(model, None, "anthropic"), "{model}");
        }
        for model in [
            "aws.claude-opus-4.8",
            "glm-5.3",
            "deepseek-v4-flash",
            "qwen3.5-plus",
        ] {
            assert!(honors_cache_markers(model, None, "openai"), "{model}");
        }
        let off = json!({"features": {"chat_cache_breakpoints": "unsupported"}});
        assert!(!honors_cache_markers("glm-5.3", Some(&off), "openai"));
        assert!(honors_cache_markers("glm-5.3", Some(&off), "anthropic"));
    }
}
