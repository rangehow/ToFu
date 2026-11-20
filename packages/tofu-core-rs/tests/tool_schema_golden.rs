//! The Rust tool boundary reproduces Python's provider tool-schema projection
//! for every built-in tool schema and every hand-written case of
//! `tests/fixtures/tool_schema_golden.json` (generated from
//! `lib.tools.gateway.sanitize_wire_tools` and `build_body` +
//! `prepare_request` by `scripts/gen_tool_schema_golden.py`): the same
//! projected bytes, the same dropped tools, the same Python exception.

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use tofu_core_rs::canonical::Error;
use tofu_core_rs::pyjson::compact;
use tofu_core_rs::schema::sanitize_wire_tools;
use tofu_core_rs::wire::prepare_request_json;

fn golden() -> Value {
    serde_json::from_str(include_str!("fixtures/tool_schema_golden.json")).unwrap()
}

fn raised(error: &Error) -> Option<Value> {
    match error {
        Error::Raised { class, message } => Some(json!({"class": class, "message": message})),
        Error::Unsupported(_) => None,
    }
}

#[test]
fn every_tool_schema_matches_the_python_preflight() {
    let golden = golden();
    let preflight = golden["preflight"].as_array().unwrap();
    assert!(preflight.len() >= 200, "{}", preflight.len());
    let mut failures = Vec::new();
    let mut checked = 0;
    for case in preflight {
        let tool = &golden["corpora"][case["corpus"].as_str().unwrap()]
            [case["index"].as_u64().unwrap() as usize];
        let label = format!(
            "{}#{} {}",
            case["corpus"].as_str().unwrap(),
            case["index"],
            tool["function"]["name"]
        );
        for (model, result) in case["models"].as_object().unwrap() {
            let expected = &case["results"][result.as_u64().unwrap() as usize];
            checked += 1;
            let actual = match sanitize_wire_tools(std::slice::from_ref(tool), model) {
                Ok((tools, dropped)) => {
                    let isolated: Vec<Value> = dropped
                        .iter()
                        .map(|row| json!([row.tool_name, row.reason_code]))
                        .collect();
                    if expected.get("unchanged").is_some() {
                        if isolated.is_empty()
                            && tools.len() == 1
                            && compact(&tools[0]) == compact(tool)
                        {
                            continue;
                        }
                        failures.push(format!(
                            "{label} [{model}]: expected unchanged, got {} dropped={isolated:?}",
                            serde_json::to_string(&tools).unwrap()
                        ));
                        continue;
                    }
                    json!({"tools": tools, "isolated": isolated})
                }
                Err(error) => match raised(&error) {
                    Some(raised) => json!({"error": raised}),
                    None => {
                        failures.push(format!("{label} [{model}]: {error}"));
                        continue;
                    }
                },
            };
            if compact(&actual) != compact(expected) {
                failures.push(format!(
                    "{label} [{model}]\n  rust:   {}\n  python: {}",
                    String::from_utf8_lossy(&compact(&actual)),
                    String::from_utf8_lossy(&compact(expected))
                ));
            }
        }
    }
    assert!(checked >= 1200, "{checked}");
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn every_catalog_request_matches_the_python_wire_bytes() {
    let golden = golden();
    let requests = golden["requests"].as_array().unwrap();
    assert!(requests.len() >= 12);
    let mut failures = Vec::new();
    for case in requests {
        let name = case["name"].as_str().unwrap();
        let set = &golden["request_sets"][case["tools"].as_str().unwrap()];
        let corpus = &golden["corpora"][set[0].as_str().unwrap()];
        let tools: Vec<Value> = set[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|index| corpus[index.as_u64().unwrap() as usize].clone())
            .collect();
        let request = json!({
            "protocol": case["protocol"], "model": case["model"], "base_url": case["base_url"],
            "max_tokens": golden["max_tokens"], "temperature": golden["temperature"],
            "api_key": golden["api_key"], "working_dir": golden["working_dir"],
            "gateway_profiles": golden["gateway_profiles"], "messages": golden["messages"],
            "tools": tools,
        });
        match (prepare_request_json(&request), case.get("error")) {
            (Ok(sealed), None) => {
                let digest: String = Sha256::digest(&sealed.payload)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                if digest != case["payload_sha256"].as_str().unwrap() {
                    failures.push(format!(
                        "{name}: payload ({} bytes, python {})",
                        sealed.payload.len(),
                        case["payload_len"]
                    ));
                }
                if sealed.url != case["url"].as_str().unwrap() {
                    failures.push(format!("{name}: url {}", sealed.url));
                }
                let headers: Vec<(String, String)> =
                    serde_json::from_value(case["headers"].clone()).unwrap();
                if sealed.headers != headers {
                    failures.push(format!("{name}: headers {:?}", sealed.headers));
                }
            }
            (Err(error), Some(expected)) if raised(&error).as_ref() == Some(expected) => {}
            (Err(error), expected) => {
                failures.push(format!("{name}: {error} (python {expected:?})"))
            }
            (Ok(_), Some(expected)) => failures.push(format!("{name}: python raised {expected}")),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
