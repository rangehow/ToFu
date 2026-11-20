//! The Rust stream parser reproduces Python's `SSEByteStream` →
//! `SSEAccumulator` → `finalize` projection for every case of
//! `tests/fixtures/stream_golden.json` (from `scripts/gen_stream_golden.py`).

use serde_json::Value;

use tofu_core_rs::stream::parse_request;
use tofu_core_rs::wire::Protocol;

#[test]
fn every_stream_case_matches_the_python_projection() {
    let golden: Value = serde_json::from_str(include_str!("fixtures/stream_golden.json")).unwrap();
    let trace_id = golden["trace_id"].as_str().unwrap();
    let max_tokens = golden["max_tokens"].as_i64();
    let cases = golden["cases"].as_array().unwrap();
    assert!(!cases.is_empty());
    let mut mismatches = Vec::new();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let protocol = Protocol::parse(case["protocol"].as_str().unwrap()).unwrap();
        let model = case["model"].as_str().unwrap();
        let body = case["sse"].as_str().unwrap().as_bytes();
        match parse_request(protocol, model, max_tokens, trace_id, body) {
            Ok(outcome) => {
                let actual = outcome.to_json();
                if actual != case["expected"] {
                    mismatches.push(format!(
                        "{name}:\n  rust:   {actual}\n  python: {}",
                        case["expected"]
                    ));
                }
            }
            Err(unsupported) => mismatches.push(format!("{name}: {unsupported}")),
        }
    }
    assert!(
        mismatches.is_empty(),
        "{} of {} cases differ:\n{}",
        mismatches.len(),
        cases.len(),
        mismatches.join("\n")
    );
}
