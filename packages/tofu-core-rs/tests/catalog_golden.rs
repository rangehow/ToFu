//! Registered-catalog sweep: every model of `current_model_defaults()` on its
//! selected fixture route, with text-only history and API-key auth, under
//! every listed reasoning setting, must
//! reproduce the Python oracle (`scripts/gen_catalog_golden.py`): the same
//! URL, headers and payload digest, or the same exception. Any `Unsupported`
//! fails within this tranche. Images, reasoning history, OAuth and other
//! routes/features of these same models are NOT certified by this sweep.

use std::collections::BTreeMap;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tofu_core_rs::canonical::Error;
use tofu_core_rs::wire::prepare_request_json;

fn fixture() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/catalog_golden.json"
    );
    serde_json::from_str(&std::fs::read_to_string(path).expect("catalog fixture")).expect("json")
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn every_catalog_model_and_reasoning_setting_matches_the_oracle() {
    let golden = fixture();
    let routes = &golden["routes"];
    let intents = golden["intents"].as_object().expect("intents");
    let models: BTreeMap<&str, &Value> = golden["models"]
        .as_array()
        .expect("models")
        .iter()
        .map(|m| (m["model_id"].as_str().expect("model id"), m))
        .collect();
    let cases = golden["cases"].as_array().expect("cases");
    assert!(models.len() >= 200, "catalog shrank to {}", models.len());
    assert_eq!(
        cases.len(),
        models.len() * intents.len(),
        "every catalog model runs every reasoning setting"
    );
    let mut unsupported: Vec<String> = Vec::new();
    let mut mismatches: Vec<String> = Vec::new();
    for case in cases {
        let model_id = case["model_id"].as_str().expect("model id");
        let intent = case["intent"].as_str().expect("intent");
        let model = models[model_id];
        let route = &routes[model["route"].as_str().expect("route")];
        let request = json!({
            "protocol": route["protocol"],
            "model": model_id,
            "max_tokens": golden["max_tokens"],
            "temperature": golden["temperature"],
            "tools": golden["tools"],
            "messages": golden["messages"],
            "base_url": route["base_url"],
            "api_key": golden["api_key"],
            "inference_profile": model["inference_profile"],
            "gateway_profiles": golden["gateway_profiles"],
            "working_dir": golden["working_dir"],
            "reasoning": intents[intent],
        });
        let label = format!("{model_id} / {intent}");
        match (prepare_request_json(&request), case.get("error")) {
            (Err(Error::Unsupported(what)), _) => unsupported.push(format!("{label}: {what}")),
            (Ok(sealed), None) => {
                let headers: Vec<Value> =
                    sealed.headers.iter().map(|(k, v)| json!([k, v])).collect();
                let digest = hex(&Sha256::digest(&sealed.payload));
                if sealed.url != route["url"].as_str().unwrap_or("")
                    || Value::Array(headers) != route["headers"]
                    || digest != case["payload_sha256"].as_str().unwrap_or("")
                {
                    mismatches.push(format!(
                        "{label}: payload {}",
                        String::from_utf8_lossy(&sealed.payload)
                    ));
                }
            }
            (Err(Error::Raised { class, message }), Some(expected)) => {
                if expected != &json!({"class": class, "message": message}) {
                    mismatches.push(format!(
                        "{label}: raised {class}: {message}, oracle {expected}"
                    ));
                }
            }
            (Ok(_), Some(expected)) => {
                mismatches.push(format!("{label}: oracle raised {expected}"))
            }
            (Err(Error::Raised { class, message }), None) => {
                mismatches.push(format!("{label}: raised {class}: {message}, oracle sealed"))
            }
        }
    }
    let report = |items: &[String]| {
        items
            .iter()
            .take(20)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    };
    assert!(
        unsupported.is_empty(),
        "{} registered cases are Unsupported:\n{}",
        unsupported.len(),
        report(&unsupported)
    );
    assert!(
        mismatches.is_empty(),
        "{} of {} cases differ from Python:\n{}",
        mismatches.len(),
        cases.len(),
        report(&mismatches)
    );
}
