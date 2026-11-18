//! The Rust request path reproduces Python's sealed wire bytes for a history
//! that exercises every ported stage (`tests/fixtures/wire_golden.json`, from
//! `scripts/gen_wire_golden.py`). The full recorded corpus is compared by
//! `benchmarks/harness_perf/byte_identity.py --rust`.

use base64::Engine as _;
use serde_json::Value;

use tofu_core_rs::canonical::{build_body, BodySpec};
use tofu_core_rs::wire::{prepare, Protocol};

#[test]
fn every_wire_face_matches_the_python_golden_bytes() {
    let golden: Value = serde_json::from_str(include_str!("fixtures/wire_golden.json")).unwrap();
    let messages = golden["messages"].as_array().unwrap().clone();
    let tools = golden["tools"].as_array().unwrap().clone();
    let cases = golden["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 3);
    for case in cases {
        let protocol = Protocol::parse(case["protocol"].as_str().unwrap()).unwrap();
        let body = build_body(
            &messages,
            &BodySpec {
                model: golden["model"].as_str().unwrap(),
                max_tokens: golden["max_tokens"].as_u64().unwrap(),
                temperature: golden["temperature"].as_f64(),
                tools: Some(&tools),
            },
        )
        .unwrap();
        let sealed = prepare(
            body,
            protocol,
            golden["base_url"].as_str().unwrap(),
            golden["api_key"].as_str().unwrap(),
        )
        .unwrap();
        let expected = base64::engine::general_purpose::STANDARD
            .decode(case["payload_b64"].as_str().unwrap())
            .unwrap();
        assert_eq!(
            String::from_utf8(sealed.payload).unwrap(),
            String::from_utf8(expected).unwrap(),
            "{protocol:?} payload"
        );
        assert_eq!(sealed.url, case["url"].as_str().unwrap());
        let headers: Vec<(String, String)> =
            serde_json::from_value(case["headers"].clone()).unwrap();
        assert_eq!(sealed.headers, headers, "{protocol:?} headers");
    }
}
