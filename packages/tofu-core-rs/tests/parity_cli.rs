//! Actual subcommand integration: bounded text/API-key tranche and refusals.
use base64::Engine as _;
use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

fn request() -> Value {
    json!({"protocols": ["openai"], "model": "gpt-5", "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
        "base_url": "http://127.0.0.1:9/v1", "api_key": "test"})
}

fn invoke(rows: &[Value]) -> Vec<Value> {
    let mut child = Command::new(env!("CARGO_BIN_EXE_tofu-core-rs"))
        .arg("parity")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    for row in rows {
        writeln!(stdin, "{row}").unwrap();
    }
    drop(stdin);
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success(), "{:?}", output.stderr);
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn subcommand_propagates_reasoning_extra_and_named_choice() {
    let mut input = request();
    input["reasoning"] = json!({"effort": "high"});
    input["tools"] = json!([{"type": "function", "function": {"name": "read",
        "parameters": {"type": "object", "properties": {}}}}]);
    input["body_extra"] = json!({"metadata": {"probe": "preserved"},
        "tool_choice": {"type": "function", "function": {"name": "read"}}});
    let answers = invoke(&[json!({"id": "test", "boundary": "provider_wire", "input": input})]);
    assert_eq!(answers.len(), 1);
    assert_eq!(answers[0]["id"], "test");
    let parts = answers[0]["parts"].as_array().expect("ported request");
    let part = parts
        .iter()
        .find(|p| p["name"] == "openai:payload")
        .unwrap();
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(part["b64"].as_str().unwrap())
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["reasoning_effort"], "high");
    assert_eq!(body["metadata"]["probe"], "preserved");
    assert_eq!(body["tool_choice"]["function"]["name"], "read");
}

#[test]
fn unported_tranches_refuse_and_line_processing_continues() {
    let overrides = [
        (json!({"oauth": "codex"}), "OAuth"),
        (
            json!({"protocols":["anthropic"],"body_extra":{
            "tool_choice":{"type":"function","function":{"name":"read"}}}}),
            "tool_choice",
        ),
        (
            json!({"protocols":["responses"],"body_extra":{
            "tool_choice":{"type":"function","function":{"name":"read"}}}}),
            "tool_choice",
        ),
        (
            json!({"messages": [{"role": "user", "content": [{"type": "image_url",
            "image_url": {"url": "x"}}]}]}),
            "image",
        ),
        (
            json!({"messages": [{"role": "assistant", "content": "a",
            "reasoning_content": "private"}]}),
            "replay",
        ),
        (
            json!({"body_extra": {"_programmatic_tool_calling": "on",
            "_programmatic_eligible_tools": true}, "inference_profile": {
            "features": {"responses_programmatic": true}}}),
            "responses_programmatic",
        ),
        (
            json!({"body_extra": {"_multi_agent_mode": "read_only"},
                "inference_profile": {"features": {"responses_multi_agent": true}}}),
            "responses_multi_agent",
        ),
        (
            json!({"body_extra": {"_tool_schema_budget_tokens": 1}, "tools": [
            {"type": "function", "function": {"name": "read", "description": "long schema",
            "parameters": {"type": "object", "properties": {}}}}]}),
            "budget",
        ),
    ];
    let mut rows = Vec::new();
    for (index, (extra, _)) in overrides.iter().enumerate() {
        let mut input = request();
        for (key, value) in extra.as_object().unwrap() {
            input[key] = value.clone();
        }
        if input["body_extra"]
            .get("_tool_schema_budget_tokens")
            .is_some()
        {
            input["body_extra"]["_executable_tool_catalog"] = input["tools"].clone();
        }
        rows.push(json!({"id": index, "boundary": "provider_wire", "input": input}));
    }
    rows.push(json!({"id": "unknown", "boundary": "sync_snapshot", "input": {}}));
    rows.push(json!({"id": "next", "boundary": "provider_wire", "input": request()}));
    let answers = invoke(&rows);
    assert_eq!(answers.len(), rows.len());
    for (index, (_, fragment)) in overrides.iter().enumerate() {
        assert_eq!(answers[index]["id"], index);
        assert!(
            answers[index]["unsupported"].is_string(),
            "{}",
            answers[index]
        );
        assert!(
            answers[index]["unsupported"]
                .as_str()
                .unwrap()
                .contains(fragment),
            "{}",
            answers[index]
        );
        assert!(answers[index].get("parts").is_none());
        assert!(answers[index].get("error").is_none());
    }
    assert!(answers[overrides.len()]["unsupported"].is_string());
    assert!(answers.last().unwrap()["parts"].is_array());
}
