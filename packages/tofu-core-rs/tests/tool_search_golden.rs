//! The Rust request path reproduces Python's final Tool Search projection
//! (`lib/llm/tool_wire.py` `project_tool_wire`) for real Tofu task rounds:
//! every case of `tests/fixtures/tool_search_golden.json` (generated through
//! `assemble_round_tools` + `build_body` + `attach_tool_request_metadata` +
//! `prepare_request` by `scripts/gen_tool_search_golden.py`) must match the
//! sealed payload bytes (SHA-256 and length), URL and headers, or be refused
//! with `Unsupported` where the case says so.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use tofu_core_rs::canonical::Error;
use tofu_core_rs::wire::{prepare_request_json, SealedRequest};

fn golden() -> Value {
    serde_json::from_str(include_str!("fixtures/tool_search_golden.json")).unwrap()
}

fn resolve(golden: &Value, value: &Value) -> Value {
    match value {
        Value::Object(map) if map.len() == 1 && map.contains_key("$ref") => {
            golden["catalogs"][map["$ref"].as_str().unwrap()].clone()
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), resolve(golden, item)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn request(golden: &Value, case: &Value) -> Value {
    json!({
        "protocol": case["protocol"], "model": case["model"], "base_url": case["base_url"],
        "max_tokens": golden["max_tokens"], "temperature": golden["temperature"],
        "api_key": golden["api_key"], "working_dir": golden["working_dir"],
        "gateway_profiles": golden["gateway_profiles"],
        "messages": golden["messages"],
        "tools": resolve(golden, &case["tools"]),
        "inference_profile": case["inference_profile"],
        "extended_cache_ttl": false, "lineage": case["lineage"],
        "body_extra": resolve(golden, &case["body_extra"]),
    })
}

fn skeleton(payload: &[u8]) -> Value {
    let mut payload: Value = serde_json::from_slice(payload).unwrap();
    if let Some(Value::Array(tools)) = payload.get_mut("tools") {
        for tool in tools.iter_mut() {
            let name = tool
                .get("name")
                .or_else(|| tool.get("function").and_then(|f| f.get("name")))
                .cloned()
                .unwrap_or(Value::Null);
            *tool = name;
        }
    }
    payload
}

fn sealed(golden: &Value, name: &str) -> SealedRequest {
    let case = golden["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .unwrap();
    prepare_request_json(&request(golden, case)).unwrap()
}

#[test]
fn every_tool_search_case_matches_the_python_golden_bytes() {
    let golden = golden();
    let cases = golden["cases"].as_array().unwrap();
    assert!(cases.len() >= 30, "{}", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let result = prepare_request_json(&request(&golden, case));
        if case["expect_unsupported"] == json!(true) {
            if !matches!(result, Err(Error::Unsupported(_))) {
                failures.push(format!("{name}: expected Unsupported"));
            }
            continue;
        }
        if let Some(error) = case.get("error") {
            let expected = Error::raised(
                error["class"].as_str().unwrap(),
                error["message"].as_str().unwrap(),
            );
            if result.as_ref().err() != Some(&expected) {
                failures.push(format!("{name}: expected {expected}"));
            }
            continue;
        }
        let sealed = match result {
            Ok(sealed) => sealed,
            Err(error) => {
                failures.push(format!("{name}: {error}"));
                continue;
            }
        };
        let digest: String = Sha256::digest(&sealed.payload)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        if digest != case["payload_sha256"].as_str().unwrap()
            || sealed.payload.len() as u64 != case["payload_len"].as_u64().unwrap()
        {
            let rust = skeleton(&sealed.payload);
            failures.push(if rust == case["payload_skeleton"] {
                format!("{name}: payload differs inside tool schemas")
            } else {
                format!(
                    "{name}: payload\n  rust:   {rust}\n  python: {}",
                    case["payload_skeleton"]
                )
            });
        }
        if sealed.url != case["url"].as_str().unwrap() {
            failures.push(format!("{name}: url {} != {}", sealed.url, case["url"]));
        }
        let headers: Vec<(String, String)> =
            serde_json::from_value(case["headers"].clone()).unwrap();
        if sealed.headers != headers {
            failures.push(format!(
                "{name}: headers {:?} != {headers:?}",
                sealed.headers
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn claude_name_budget_hides_only_optional_names_behind_discovery() {
    let golden = golden();
    let names = sealed(&golden, "claude-anthropic-name-budget").tool_names;
    assert!(names.iter().map(|n| n.len() + 1).sum::<usize>() <= 448);
    for floor in [
        "search_tools",
        "execute_tools",
        "execute_program",
        "poll",
        "kill",
        "timer_create",
        "spawn_agents",
        "run_command",
        "read_files",
    ] {
        assert!(names.iter().any(|name| name == floor), "{floor}");
    }
    // Without a discovery path nothing is hidden, even over the budget.
    let off = sealed(&golden, "claude-anthropic-off").tool_names;
    assert!(off.iter().map(|n| n.len() + 1).sum::<usize>() > 448);
    assert!(!off.iter().any(|name| name == "search_tools"));
}

#[test]
fn mcp_tools_stay_search_only_and_their_named_choice_is_dropped() {
    let golden = golden();
    for name in [
        "claude-anthropic-mcp",
        "glm-chat-mcp",
        "kimi-k3-anthropic-mcp",
        "gpt-responses-mcp",
    ] {
        let request = sealed(&golden, name);
        assert!(
            !request
                .tool_names
                .iter()
                .any(|tool| tool.starts_with("mcp__")),
            "{name}"
        );
        assert!(request.tool_names.iter().any(|tool| tool == "search_tools"));
        let payload: Value = serde_json::from_slice(&request.payload).unwrap();
        assert!(payload.get("tool_choice").is_none(), "{name}");
    }
}
