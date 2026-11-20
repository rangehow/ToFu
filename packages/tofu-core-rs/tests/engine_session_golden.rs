//! Parity of the execution engine with the Python loop, runnable by cargo.
//!
//! Every case of `tests/rust_parity_engine/cases.jsonl` runs through the
//! engine's `engine_session` boundary and must reproduce, byte for byte, the
//! parts the Python reference produced (`tests/fixtures/engine_session_golden.json`,
//! written by `python3 -m tests.rust_parity_engine.compare --write-golden`).
//! No behavioural differences are skipped. A diagnosed difference still
//! fails this gate; same-process replay cannot certify crash recovery.
//! Regenerate from Python after an intended Python change, never to match Rust.

use serde_json::Value;
use tofu_core_rs::engine::replay::engine_session;

const CASES: &str = include_str!("../../../tests/rust_parity_engine/cases.jsonl");
const GOLDEN: &str = include_str!("fixtures/engine_session_golden.json");

#[test]
fn every_recorded_engine_session_matches_the_python_loop() {
    let golden: Value = serde_json::from_str(GOLDEN).unwrap();
    let mut checked = 0;
    let mut differences = Vec::new();
    for line in CASES.lines().filter(|line| !line.trim().is_empty()) {
        let case: Value = serde_json::from_str(line).unwrap();
        let name = case["name"].as_str().unwrap();
        let Some(expected) = golden["cases"].get(name) else {
            panic!("case {name} has no Python golden; regenerate the golden file");
        };
        let parts = engine_session(&case["input"])
            .unwrap_or_else(|unsupported| panic!("{name}: unsupported: {}", unsupported.0));
        assert_eq!(
            parts.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
            ["outcome", "requests", "tools", "fallbacks", "events"],
            "{name}: part names"
        );
        for (part, bytes) in parts {
            let actual = String::from_utf8(bytes).unwrap();
            if expected[&part].as_str() != Some(actual.as_str()) {
                differences.push(format!(
                    "{name}/{part}\n  python: {}\n  rust:   {actual}",
                    expected[&part]
                ));
            }
        }
        checked += 1;
    }
    assert!(checked > 0, "the engine corpus is empty");
    assert_eq!(
        checked,
        golden["cases"].as_object().unwrap().len(),
        "orphaned or missing golden cases"
    );
    assert!(
        differences.is_empty(),
        "unexplained differences:\n{}",
        differences.join("\n")
    );
}
