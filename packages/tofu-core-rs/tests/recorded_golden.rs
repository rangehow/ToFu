//! Existing recorded request corpus, without copied transcripts: load the
//! content-addressed fields and reproduce byte_identity.py's two injections.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::Path;
use tofu_core_rs::wire::prepare_request_json;

fn read(path: &Path) -> Value {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn all_75_recorded_requests_match_the_python_oracle() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let replay = root.join("tests/fixtures/llm_replay");
    let cases =
        read(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/recorded_golden.json"));
    let cases = cases.as_array().unwrap();
    assert_eq!(cases.len(), 75);
    for case in cases {
        let scenario = case["scenario"].as_str().unwrap();
        let script = std::fs::read_to_string(replay.join(scenario).join("script.jsonl")).unwrap();
        let steps: Vec<Value> = script
            .lines()
            .map(|s| serde_json::from_str::<Value>(s).unwrap())
            .filter(|s| s["field_digests"].get("messages").is_some())
            .collect();
        let digests = steps[case["step"].as_u64().unwrap() as usize]["field_digests"]
            .as_object()
            .unwrap();
        let field = |key: &str| -> Value {
            match digests.get(key).and_then(Value::as_str) {
                Some(digest) => read(
                    &replay
                        .join("requests")
                        .join(format!("{key}-{}.json", &digest[..16])),
                ),
                None => Value::Null,
            }
        };
        let mut messages = field("messages").as_array().unwrap().clone();
        let system = field("system");
        if system.as_str().is_some_and(|s| !s.is_empty())
            && messages.first().is_none_or(|m| m["role"] != "system")
        {
            messages.insert(0, json!({"role":"system","content":system}));
        }
        if let Some(last_user) = messages.iter().rposition(|m| m["role"] == "user") {
            messages.insert(last_user, json!({"role":"system","content":"Managed section body.\nSecond line.","_isMeta":true}));
            messages
                .push(json!({"role":"user","content":"A private progress event.","_isMeta":true}));
        }
        let model = field("model");
        let model = model
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("claude-sonnet-4");
        let request = json!({"protocol":case["protocol"],"model":model,"max_tokens":4096,
            "temperature":1.0,"tools":field("tools"),"messages":messages,
            "base_url":"http://127.0.0.1:9/v1","api_key":"sk-byte-identity"});
        let sealed = prepare_request_json(&request).unwrap();
        let hash: String = Sha256::digest(&sealed.payload)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(
            hash, case["sha256"],
            "{scenario} / {} / {}",
            case["step"], case["protocol"]
        );
        assert_eq!(
            sealed.payload.len(),
            case["bytes"].as_u64().unwrap() as usize
        );
        assert_eq!(sealed.url, case["url"]);
        assert_eq!(
            serde_json::to_value(sealed.headers).unwrap(),
            case["headers"]
        );
    }
}
