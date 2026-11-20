//! Private ports of `lib/tool_results/tool_round_replay.py`
//! (`scan_replayable_tool_round_prefix`, `checkpoint_retention_positions`,
//! `is_superseded_provider_attempt_round`) and the model-receipt readers its
//! delegated-receipt coverage needs: `tool_round_identity.py`
//! (`execution_identity`, `execution_llm_round`), `tool_caller_identity.py`
//! (`normalize_tool_caller`), `gateway_result_projection.py`
//! (`gateway_result_children`) and `result_projection_text.py`
//! (`load_text_field`). Only what restart settlement reads is returned: the
//! replayable count, the blocked position and its reason.

use std::collections::HashSet;

use serde_json::Value;

use super::pyjson::{loads, Mode, PyStr, PyVal};
use crate::json::truthy;

const MAX_TOOL_CALLER_ID_CHARS: usize = 512;
const MAX_REPLAY_TOOL_NAME_CHARS: usize = 512;
const MAX_EXECUTION_ID_CHARS: usize = 256;
const MAX_LLM_ROUND_TEXT_CHARS: usize = 64;
const DELEGATED_EXECUTION_SOURCES: [&str; 2] = ["execute_calls", "execute_program"];
const CHILD_HEADING_PREFIX: &str = "\n## results[";

/// `str.isspace()`: Unicode White_Space plus the C0 separators U+001C..U+001F.
pub fn py_isspace(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// `str.strip()`.
pub fn py_strip(text: &str) -> &str {
    text.trim_matches(py_isspace)
}

fn char_count(text: &str) -> usize {
    text.chars().count()
}

/// `ToolRoundReplayPrefix` fields restart settlement reads.
pub struct ReplayPrefix {
    pub replayable: usize,
    pub blocked_position: Option<usize>,
    pub blocked_reason: &'static str,
}

#[derive(Clone, PartialEq, Eq, Hash)]
enum LlmRound {
    /// A JSON integer token (Python `int`).
    Int(String),
    Text(String),
}

type Scope = (String, String, LlmRound);
type ReceiptKey = (String, String, LlmRound, PyStr, Option<PyStr>);

fn get<'v>(row: &'v serde_json::Map<String, Value>, key: &str) -> Option<&'v Value> {
    row.get(key)
}

/// `_identity_text`.
fn identity_text(value: &Value) -> String {
    let text = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) if number.is_i64() || number.is_u64() => number.to_string(),
        _ => return String::new(),
    };
    let stripped: String = py_strip(&text).chars().take(MAX_EXECUTION_ID_CHARS).collect();
    if stripped.starts_with("@dispatching:") {
        String::new()
    } else {
        stripped
    }
}

/// `execution_identity(value)`: `value.get(a) or value.get(b)`.
fn execution_identity(row: &serde_json::Map<String, Value>) -> (String, String) {
    let either = |first: &str, second: &str| -> String {
        let value = get(row, first).unwrap_or(&Value::Null);
        let chosen = if truthy(value) { value } else { get(row, second).unwrap_or(&Value::Null) };
        identity_text(chosen)
    };
    (either("attemptId", "_attemptId"), either("taskId", "_taskId"))
}

/// `execution_llm_round(value)`.
fn execution_llm_round(row: &serde_json::Map<String, Value>) -> Option<LlmRound> {
    match get(row, "llmRound")? {
        Value::Number(number) if number.is_i64() || number.is_u64() => Some(LlmRound::Int(number.to_string())),
        Value::String(raw) => {
            let normalized = py_strip(raw);
            (!normalized.is_empty() && char_count(normalized) <= MAX_LLM_ROUND_TEXT_CHARS)
                .then(|| LlmRound::Text(normalized.to_owned()))
        }
        _ => None,
    }
}

/// `_receipt_scope(row)`.
fn receipt_scope(row: &serde_json::Map<String, Value>) -> Option<Scope> {
    let (attempt_id, task_id) = execution_identity(row);
    let llm_round = execution_llm_round(row)?;
    if attempt_id.is_empty() && task_id.is_empty() {
        return None;
    }
    Some((attempt_id, task_id, llm_round))
}

/// `_receipt_id(value)` for a stored JSON value.
fn receipt_id(value: Option<&Value>) -> Option<PyStr> {
    match value {
        Some(Value::String(text)) if !text.is_empty() && char_count(text) <= MAX_TOOL_CALLER_ID_CHARS => {
            Some(PyStr::Text(text.clone()))
        }
        _ => None,
    }
}

/// `_receipt_id(value)` for a decoded receipt value.
fn receipt_id_py(value: Option<&PyVal>) -> Option<PyStr> {
    match value {
        Some(PyVal::Str(text)) if text.len() > 0 && text.len() <= MAX_TOOL_CALLER_ID_CHARS => Some(text.clone()),
        _ => None,
    }
}

/// `str.partition(separator)` for a one-character separator.
fn partition(text: &str, separator: char) -> (&str, bool, &str) {
    match text.find(separator) {
        Some(index) => (&text[..index], true, &text[index + separator.len_utf8()..]),
        None => (text, false, ""),
    }
}

/// Byte offset after the first `count` characters, or `None` when shorter.
fn char_offset(text: &str, count: usize) -> Option<usize> {
    if count == 0 {
        return Some(0);
    }
    text.char_indices().nth(count - 1).map(|(index, character)| index + character.len_utf8())
}

/// `<field> \(text; ([0-9]+) chars\):` fullmatch: the declared length,
/// `None` for no match, `Some(None)` for a length beyond any text.
fn body_label(label: &str, field: &str) -> Option<Option<usize>> {
    let digits = label.strip_prefix(field)?.strip_prefix(" (text; ")?.strip_suffix(" chars):")?;
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    Some(digits.parse::<usize>().ok())
}

/// `gateway_result_children(text)`: does any child list exist, and each
/// child's `call_id` / `resultId` (`None` = key absent).
#[allow(clippy::type_complexity)]
fn gateway_result_children(text: &str) -> Option<Vec<(Option<PyStr>, Option<Option<PyStr>>)>> {
    fn child_ids(child: &PyVal) -> (Option<PyStr>, Option<Option<PyStr>>) {
        let call_id = receipt_id_py(child.get("call_id"));
        let result_id = child.get("resultId").map(|value| receipt_id_py(Some(value)));
        (call_id, result_id)
    }
    let value = loads(text, Mode::Projection, 3);
    if let Some(value) = value.filter(|value| !matches!(value, PyVal::Null)) {
        let PyVal::List(results) = value.get("results")? else { return None };
        if !results.iter().all(|child| matches!(child, PyVal::Dict(_))) {
            return None;
        }
        return Some(results.iter().map(child_ids).collect());
    }
    let (head, separator, mut rest) = partition(text, '\n');
    let top = loads(head, Mode::Projection, 1)?;
    if !separator || !matches!(top, PyVal::Dict(_)) || top.contains("results") {
        return None;
    }
    if rest == "results: []" {
        return Some(vec![]);
    }
    let mut children = Vec::new();
    while !rest.is_empty() {
        let heading = format!("{CHILD_HEADING_PREFIX}{}]\n", children.len());
        let after = rest.strip_prefix(heading.as_str())?;
        let (line, newline_found, tail_rest) = partition(after, '\n');
        let mut newline = newline_found;
        rest = tail_rest;
        let child = loads(line, Mode::Projection, 1)?;
        if !matches!(child, PyVal::Dict(_)) {
            return None;
        }
        // Body sections only add `output`/`error` text keys.
        while newline && !rest.is_empty() && !rest.starts_with(CHILD_HEADING_PREFIX) {
            let (label, found, tail) = partition(rest, '\n');
            newline = found;
            let field = if label.starts_with("output") { "output" } else { "error" };
            let size = body_label(label, field)?;
            let end = size.and_then(|size| char_offset(tail, size));
            let end = end.filter(|_| newline)?;
            rest = &tail[end..];
            if rest.starts_with('\n') && !rest.starts_with(CHILD_HEADING_PREFIX) {
                rest = &rest[1..];
            } else if !rest.is_empty() && !rest.starts_with(CHILD_HEADING_PREFIX) {
                return None;
            }
        }
        children.push(child_ids(&child));
    }
    Some(children)
}

/// `load_text_field(text, 'summary')`, then `envelope.get('truncated') is not
/// True`: the summary body when the envelope is an exact, untruncated view.
fn untruncated_summary(text: &str) -> Option<&str> {
    let (metadata, separator, tail) = partition(text, '\n');
    let (label, newline, body) = partition(tail, '\n');
    let size = body_label(label, "summary")?;
    if !separator || !newline {
        return None;
    }
    if size != Some(char_count(body)) {
        return None;
    }
    let value = loads(metadata, Mode::Plain, 1)?;
    if !matches!(value, PyVal::Dict(_)) || value.contains("summary") {
        return None;
    }
    if matches!(value.get("truncated"), Some(PyVal::Bool(true))) {
        return None;
    }
    Some(body)
}

/// `_container_receipt_keys(row)`.
fn container_receipt_keys(row: &serde_json::Map<String, Value>) -> Vec<ReceiptKey> {
    let Some(scope) = receipt_scope(row) else { return vec![] };
    if get(row, "_hiddenToolAdapter") != Some(&Value::Bool(true)) {
        return vec![];
    }
    let content = get(row, "toolContent");
    let mut children = match content {
        Some(Value::String(text)) => gateway_result_children(text),
        _ => None,
    };
    if children.is_none() {
        if let Some(Value::String(text)) = content {
            if let Some(summary) = untruncated_summary(text) {
                children = gateway_result_children(summary);
            }
        }
    }
    let mut keys = Vec::new();
    for (call_id, result_id) in children.unwrap_or_default() {
        let Some(call_id) = call_id else { continue };
        let result_id = match result_id {
            None => None,
            Some(None) => continue,
            Some(Some(result_id)) => Some(result_id),
        };
        keys.push((scope.0.clone(), scope.1.clone(), scope.2.clone(), call_id, result_id));
    }
    keys
}

/// `is_delegated_execution_receipt(row)` and `_is_covered_delegated_receipt`.
fn is_covered_delegated_receipt(row: &serde_json::Map<String, Value>, covered: &HashSet<ReceiptKey>) -> bool {
    match get(row, "source") {
        Some(Value::String(source)) if DELEGATED_EXECUTION_SOURCES.contains(&source.as_str()) => {}
        _ => return false,
    }
    let evidence = match get(row, "toolResultEvidence") {
        Some(Value::Object(evidence)) => evidence.get("resultId"),
        _ => None,
    };
    let (Some(result_id), Some(call_id), Some(scope)) =
        (receipt_id(evidence), receipt_id(get(row, "toolCallId")), receipt_scope(row))
    else {
        return false;
    };
    let exact = (scope.0.clone(), scope.1.clone(), scope.2.clone(), call_id.clone(), Some(result_id));
    covered.contains(&exact) || covered.contains(&(scope.0, scope.1, scope.2, call_id, None))
}

/// `is_superseded_provider_attempt_round(value)`.
pub fn is_superseded_provider_attempt_round(value: &Value) -> bool {
    let Value::Object(row) = value else { return false };
    if get(row, "_providerAttemptDiscarded") == Some(&Value::Bool(true)) {
        return true;
    }
    let Some(Value::Array(results)) = get(row, "results") else { return false };
    let Some(Value::Object(first)) = results.first() else { return false };
    if first.get("badge") != Some(&Value::String("superseded".into())) {
        return false;
    }
    let has_fetched_chars =
        matches!(first.get("fetchedChars"), Some(Value::Number(n)) if n.as_f64().is_some_and(|v| v > 0.0));
    let has_real_result = get(row, "toolContent").is_some_and(|content| !content.is_null())
        || first.get("fetched") == Some(&Value::Bool(true))
        || has_fetched_chars;
    !has_real_result
}

/// `normalize_tool_caller(value)` (persisted callers require an identity):
/// only whether it is an error matters to the scan.
fn tool_caller_error(value: Option<&Value>) -> bool {
    let caller = match value {
        None | Some(Value::Null) => return false,
        Some(Value::Object(caller)) => caller,
        Some(_) => return true,
    };
    let field = match caller.get("type") {
        Some(Value::String(kind)) if kind == "program" => "caller_id",
        Some(Value::String(kind)) if kind == "multi_agent" => "agent_name",
        _ => return true,
    };
    let Some(Value::String(identity)) = caller.get(field) else { return true };
    let identity = py_strip(identity);
    identity.is_empty() || char_count(identity) > MAX_TOOL_CALLER_ID_CHARS
}

/// `normalize_replay_tool_arguments(value)[0] is None`: strings, objects and
/// absence are replayable (an object decoded from storage always serializes).
fn tool_arguments_unreplayable(value: Option<&Value>) -> bool {
    !matches!(value, None | Some(Value::Null | Value::String(_) | Value::Object(_)))
}

/// `scan_replayable_tool_round_prefix(values)`.
pub fn scan_replayable_tool_round_prefix(values: &Value) -> ReplayPrefix {
    let empty = Vec::new();
    let rows = match values {
        Value::Array(rows) => rows,
        _ => &empty,
    };
    let mut replayable = 0;
    let mut covered: HashSet<ReceiptKey> = HashSet::new();
    for (position, raw) in rows.iter().enumerate() {
        let Value::Object(row) = raw else { continue };
        if is_superseded_provider_attempt_round(raw) {
            continue;
        }
        if is_covered_delegated_receipt(row, &covered) {
            covered.extend(container_receipt_keys(row));
            continue;
        }
        let tool_call_id = match get(row, "toolCallId") {
            None | Some(Value::Null) => continue,
            Some(value) => value,
        };
        let blocked = |reason: &'static str| ReplayPrefix {
            replayable,
            blocked_position: Some(position),
            blocked_reason: reason,
        };
        if !matches!(tool_call_id, Value::String(id) if !id.is_empty() && char_count(id) <= MAX_TOOL_CALLER_ID_CHARS) {
            return blocked("invalid_tool_call_id");
        }
        if !matches!(get(row, "toolName"), Some(Value::String(name)) if !name.is_empty() && char_count(name) <= MAX_REPLAY_TOOL_NAME_CHARS)
        {
            return blocked("invalid_tool_name");
        }
        match get(row, "toolContent") {
            Some(Value::String(_)) => {}
            None | Some(Value::Null) => return blocked("missing_tool_result"),
            Some(_) => return blocked("invalid_tool_result"),
        }
        if tool_caller_error(get(row, "caller")) {
            return blocked("invalid_tool_caller");
        }
        if tool_arguments_unreplayable(get(row, "toolArgs")) {
            return blocked("invalid_tool_arguments");
        }
        replayable += 1;
        covered.extend(container_receipt_keys(row));
    }
    ReplayPrefix { replayable, blocked_position: None, blocked_reason: "" }
}

/// `checkpoint_retention_positions(values, prefix)`.
pub fn checkpoint_retention_positions(values: &Value, prefix: &ReplayPrefix) -> (usize, Vec<usize>) {
    let empty = Vec::new();
    let rows = match values {
        Value::Array(rows) => rows,
        _ => &empty,
    };
    let boundary = prefix.blocked_position.unwrap_or(rows.len());
    let positions = (0..boundary).filter(|position| !is_superseded_provider_attempt_round(&rows[*position])).collect();
    (boundary, positions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn covered_gateway_children_are_transparent() {
        let container = json!({
            "toolCallId": "c1", "toolName": "execute_tools", "toolContent":
                "{\"status\":\"ok\",\"warnings\":[]}\n\n## results[0]\n{\"call_id\":\"k1\",\"name\":\"x\",\"status\":\"ok\"}\noutput (text; 2 chars):\nhi",
            "_hiddenToolAdapter": true, "attemptId": "a", "llmRound": 3,
        });
        let child = json!({
            "toolCallId": "k1", "toolName": "x", "toolContent": 7, "source": "execute_calls",
            "toolResultEvidence": {"resultId": "r"}, "attemptId": "a", "llmRound": 3,
        });
        let prefix = scan_replayable_tool_round_prefix(&json!([container, child]));
        assert_eq!((prefix.replayable, prefix.blocked_position), (1, None));
        let uncovered = json!({"toolCallId": "k1", "toolName": "x", "toolContent": 7, "source": "execute_calls",
            "toolResultEvidence": {"resultId": "r"}, "attemptId": "a", "llmRound": "4"});
        let prefix = scan_replayable_tool_round_prefix(&json!([container, uncovered]));
        assert_eq!((prefix.blocked_position, prefix.blocked_reason), (Some(1), "invalid_tool_result"));
    }

    #[test]
    fn summary_envelopes_must_be_exact_and_untruncated() {
        assert_eq!(untruncated_summary("{\"a\":1}\nsummary (text; 2 chars):\nhi"), Some("hi"));
        assert_eq!(untruncated_summary("{\"truncated\":true}\nsummary (text; 2 chars):\nhi"), None);
        assert_eq!(untruncated_summary("{\"a\":1}\nsummary (text; 3 chars):\nhi"), None);
        assert_eq!(untruncated_summary("{\"summary\":1}\nsummary (text; 2 chars):\nhi"), None);
    }
}
