//! The Rust request path reproduces Python's sealed wire bytes, URL and
//! headers for every route case of `tests/fixtures/wire_golden.json`
//! (generated from `build_body` + `prepare_request` by
//! `scripts/gen_wire_golden.py`). The full recorded corpus is compared by
//! `benchmarks/harness_perf/byte_identity.py --rust`.

use base64::Engine as _;
use serde_json::{json, Value};

use tofu_core_rs::canonical::Error;
use tofu_core_rs::wire::prepare_request_json;

fn golden() -> Value {
    serde_json::from_str(include_str!("fixtures/wire_golden.json")).unwrap()
}

fn request(golden: &Value, case: &Value) -> Value {
    json!({
        "protocol": case["protocol"], "model": case["model"], "base_url": case["base_url"],
        "max_tokens": golden["max_tokens"], "temperature": golden["temperature"],
        "api_key": golden["api_key"], "working_dir": golden["working_dir"],
        "gateway_profiles": golden["gateway_profiles"],
        "messages": golden["message_sets"][case["messages"].as_str().unwrap()],
        "tools": golden["tool_sets"][case["tools"].as_str().unwrap()],
        "inference_profile": case["inference_profile"],
        "extended_cache_ttl": case["extended_cache_ttl"], "lineage": case["lineage"],
        "reasoning": case.get("reasoning").cloned().unwrap_or(Value::Null),
    })
}

#[test]
fn every_route_matches_the_python_golden_bytes() {
    let golden = golden();
    let cases = golden["cases"].as_array().unwrap();
    assert!(cases.len() >= 20);
    let mut failures = Vec::new();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let outcome = prepare_request_json(&request(&golden, case));
        if let Some(expected) = case.get("error") {
            match outcome {
                Err(Error::Raised { class, message })
                    if expected == &json!({"class": class, "message": message}) => {}
                other => failures.push(format!("{name}: oracle raised {expected}, rust {other:?}")),
            }
            continue;
        }
        let sealed = match outcome {
            Ok(sealed) => sealed,
            Err(error) => {
                failures.push(format!("{name}: {error}"));
                continue;
            }
        };
        let expected = base64::engine::general_purpose::STANDARD
            .decode(case["payload_b64"].as_str().unwrap())
            .unwrap();
        let headers: Vec<(String, String)> =
            serde_json::from_value(case["headers"].clone()).unwrap();
        if sealed.payload != expected {
            failures.push(format!(
                "{name}: payload\n  rust:   {}\n  python: {}",
                String::from_utf8_lossy(&sealed.payload),
                String::from_utf8_lossy(&expected)
            ));
        }
        if sealed.url != case["url"].as_str().unwrap() {
            failures.push(format!("{name}: url {} != {}", sealed.url, case["url"]));
        }
        if sealed.headers != headers {
            failures.push(format!(
                "{name}: headers {:?} != {headers:?}",
                sealed.headers
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

fn payload_of(golden: &Value, name: &str) -> Value {
    let case = golden["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .unwrap();
    let sealed = prepare_request_json(&request(golden, case)).unwrap();
    serde_json::from_slice(&sealed.payload).unwrap()
}

fn marker_count(payload: &Value) -> usize {
    serde_json::to_string(payload)
        .unwrap()
        .matches("\"cache_control\"")
        .count()
}

#[test]
fn cache_markers_follow_the_capability_matrix() {
    let golden = golden();
    for name in [
        "friday-claude-anthropic",
        "friday-claude-anthropic-loop",
        "friday-glm-chat",
        "friday-deepseek-chat",
        "friday-qwen-chat",
        "catpaw-glm-loop",
    ] {
        assert_eq!(marker_count(&payload_of(&golden, name)), 4, "{name}");
    }
    for name in [
        "friday-doubao-chat-no-markers",
        "friday-doubao-chat-base",
        "friday-minimax-chat",
        "friday-kimi-k3-anthropic",
        "friday-glm-chat-markers-declared-off",
    ] {
        assert_eq!(marker_count(&payload_of(&golden, name)), 0, "{name}");
    }
    // Doubao keeps the collapsed string content of an unmarked route.
    let doubao = payload_of(&golden, "friday-doubao-chat-base");
    assert!(doubao["messages"][0]["content"].is_string());
}

#[test]
fn anthropic_tail_marker_rides_the_tool_result_block() {
    let payload = payload_of(&golden(), "friday-claude-anthropic-loop");
    let last = payload["messages"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .clone();
    let block = &last["content"][0];
    assert_eq!(block["type"], "tool_result");
    assert_eq!(block["cache_control"], json!({"type": "ephemeral"}));
    assert!(block["content"][0].get("cache_control").is_none());
}

#[test]
fn openai_tail_marker_walks_back_past_tool_results() {
    let payload = payload_of(&golden(), "friday-claude-openai-face");
    let messages = payload["messages"].as_array().unwrap();
    for message in messages.iter().filter(|m| m["role"] == "tool") {
        assert!(!serde_json::to_string(message)
            .unwrap()
            .contains("cache_control"));
    }
    assert_eq!(marker_count(&payload), 4);
}

#[test]
fn extended_ttl_marks_stable_prefix_for_an_hour() {
    let payload = payload_of(&golden(), "friday-claude-anthropic-extended-ttl");
    assert_eq!(
        payload["tools"].as_array().unwrap().last().unwrap()["cache_control"],
        json!({"type": "ephemeral", "ttl": "1h"})
    );
}

#[test]
fn tool_name_budget_hides_only_behind_discovery_on_claude() {
    let golden = golden();
    let names = |name: &str| -> Vec<String> {
        payload_of(&golden, name)["tools"]
            .as_array()
            .unwrap()
            .iter()
            .map(|tool| tool["name"].as_str().unwrap().to_owned())
            .collect()
    };
    let bytes = |names: &[String]| names.iter().map(|n| n.len() + 1).sum::<usize>();
    let fitted = names("friday-claude-tool-name-budget");
    assert!(bytes(&fitted) <= 448, "{}", bytes(&fitted));
    for floor in ["search_tools", "execute_tools", "poll"] {
        assert!(fitted.iter().any(|name| name == floor), "{floor}");
    }
    let undiscoverable = names("friday-claude-tool-name-budget-undiscoverable");
    assert!(bytes(&undiscoverable) > 448);
    let kimi = names("friday-kimi-k3-anthropic");
    assert!(bytes(&kimi) > 448);
}
