//! Simulated restart keeps committed presentation independently of checkpoint history.
//! These are same-process parity tests, not process-crash recovery evidence.

use serde_json::{json, Value};
use tofu_core_rs::engine::replay::engine_session;

fn parts(input: &Value) -> Value {
    engine_session(input)
        .unwrap()
        .into_iter()
        .map(|(name, bytes)| (name, serde_json::from_slice::<Value>(&bytes).unwrap()))
        .collect::<serde_json::Map<_, _>>()
        .into()
}

fn case(restart: Value) -> Value {
    json!({
        "model": "primary", "user": "Write then confirm.",
        "tools": [{"name": "write_file", "readOnly": false}],
        "requests": [
            {"kind": "tool", "text": "Writing 文. ", "calls": [
                {"id": "write", "name": "write_file", "arguments": "{}"}
            ]},
            {"kind": "text", "text": "Confirmed 文."},
            {"kind": "text", "text": "Resumed."}
        ],
        "toolOutcomes": {"write": {"kind": "ok", "output": "WROTE"}},
        "restartAfterRequest": restart
    })
}

#[test]
fn committed_text_survives_an_unadopted_tool_reply_without_reentering_tools() {
    let result = parts(&case(json!(1)));
    assert_eq!(result["outcome"]["content"], "Writing 文. Confirmed 文.");
    assert_eq!(result["requests"][0], result["requests"][1]);
    assert_eq!(result["tools"][0]["effect"], "not_started");
    assert!(!result["events"]
        .as_array()
        .unwrap()
        .contains(&json!("tool_effect")));
}

#[test]
fn tool_reset_removes_commentary_but_not_the_committed_unadopted_answer() {
    let result = parts(&case(json!(2)));
    assert_eq!(result["outcome"]["content"], "Confirmed 文.Resumed.");
    assert_eq!(result["requests"][1], result["requests"][2]);
    assert_eq!(result["tools"].as_array().unwrap().len(), 1);
    assert_eq!(result["tools"][0]["effect"], "returned");
    assert_eq!(result["tools"][0]["result"], "WROTE");
}

#[test]
fn normal_session_still_excludes_tool_commentary() {
    let result = parts(&case(Value::Null));
    assert_eq!(result["outcome"]["content"], "Confirmed 文.");
    assert_eq!(result["requests"].as_array().unwrap().len(), 2);
}

#[test]
fn refused_delta_commits_are_not_recovered_as_presentation() {
    let mut input = case(json!(1));
    input["storageFailure"] = json!({"op": "append_event"});
    let result = parts(&input);
    assert_eq!(result["outcome"]["content"], "Confirmed 文.");
}
