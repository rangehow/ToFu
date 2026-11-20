//! Tool-result projection (`MCPBridge._async_call_tool` after the SDK call,
//! `lib/mcp/result_content.py` and `lib/mcp/documents.py`, the Python oracles).
//!
//! Input is the wire `CallToolResult` object (`content`, `structuredContent`,
//! `isError`). Display text is bounded and truncated; the canonical document,
//! structured JSON and ordered block descriptors carry no base64, and every
//! loss is an explicit receipt.

use crate::pyjson::{self, py_len, py_prefix, thousands};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

pub const MCP_MAX_RESULT_CHARS: usize = 200_000;
pub const MCP_MAX_DOCUMENT_BYTES: usize = 256 * 1024;
pub const MCP_MAX_DOCUMENT_NODES: usize = 8192;
pub const MCP_MAX_DOCUMENT_DEPTH: usize = 32;
pub const MCP_MAX_RETAINED_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_MCP_CONTENT_BLOCKS: usize = 1024;
pub const MAX_MCP_IMAGES_PER_TURN: usize = 20;
pub const MAX_MCP_IMAGE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_MCP_IMAGE_TOTAL_BYTES: usize = 40 * 1024 * 1024;
const ALLOWED_IMAGE_MIME_TYPES: [&str; 5] = [
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
];

/// `MCPToolResult`: display text plus its runtime payloads (the relay's `tool_result` fields).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ToolResult {
    pub text: String,
    pub image_contents: Vec<Value>,
    /// A JSON text, or (for a queue-policy refusal) the refusal object itself.
    pub structured_json: Option<Value>,
    pub document_text: Option<String>,
    pub document_error: Option<String>,
    pub is_error: bool,
    pub content_blocks: Vec<Value>,
    pub retention_error: Option<String>,
}

/// What `call_tool` returns: a plain string (the degraded fast-fail) or a typed result.
#[derive(Clone, Debug, PartialEq)]
pub enum CallOutput {
    Text(String),
    Result(ToolResult),
}

impl CallOutput {
    pub fn text(&self) -> &str {
        match self {
            CallOutput::Text(text) => text,
            CallOutput::Result(result) => &result.text,
        }
    }

    /// The relay's `_encode_result`.
    pub fn relay_encode(&self) -> Value {
        match self {
            CallOutput::Text(text) => json!({"text": text}),
            CallOutput::Result(r) => json!({"text": r.text, "tool_result": {
                "image_contents": r.image_contents, "structured_json": r.structured_json,
                "document_text": r.document_text, "document_error": r.document_error,
                "is_error": r.is_error, "content_blocks": r.content_blocks,
                "retention_error": r.retention_error,
            }}),
        }
    }
}

fn field<'a>(block: &'a Value, name: &str) -> Option<&'a Value> {
    block.get(name)
}

fn field_str<'a>(block: &'a Value, name: &str) -> Option<&'a str> {
    block.get(name).and_then(Value::as_str)
}

/// `safe_field_text`.
fn safe_text(value: Option<&Value>, default: &str) -> String {
    match value.and_then(Value::as_str) {
        Some(text) => py_prefix(text, 2048).to_string(),
        None => default.to_string(),
    }
}

// ── documents ──────────────────────────────────────────────────────────────

fn int_bit_length(number: &serde_json::Number) -> usize {
    if let Some(v) = number.as_i64() {
        (64 - v.unsigned_abs().leading_zeros()) as usize
    } else if let Some(v) = number.as_u64() {
        (64 - v.leading_zeros()) as usize
    } else {
        0
    }
}

/// `encode_document`: bounded validation, then `json.dumps(sort_keys=True, ensure_ascii=False)`.
pub fn encode_document(value: &Value, max_bytes: usize) -> Result<String, String> {
    let mut nodes = 0usize;
    let mut string_bytes = 0usize;
    fn visit(
        item: &Value,
        depth: usize,
        nodes: &mut usize,
        bytes: &mut usize,
        max: usize,
    ) -> Result<(), String> {
        *nodes += 1;
        if *nodes > MCP_MAX_DOCUMENT_NODES || depth > MCP_MAX_DOCUMENT_DEPTH {
            return Err("MCP document exceeds its structure limit".into());
        }
        let limit = || "MCP document exceeds its byte limit".to_string();
        match item {
            Value::String(text) => {
                if py_len(text) > max {
                    return Err(limit());
                }
                *bytes += text.len();
                if *bytes > max {
                    return Err(limit());
                }
            }
            Value::Object(map) => {
                for (key, child) in map {
                    visit(&Value::String(key.clone()), depth + 1, nodes, bytes, max)?;
                    visit(child, depth + 1, nodes, bytes, max)?;
                }
            }
            Value::Array(items) => {
                for child in items {
                    visit(child, depth + 1, nodes, bytes, max)?;
                }
            }
            Value::Null | Value::Bool(_) => *bytes += 5,
            Value::Number(number) if number.is_f64() => *bytes += 24,
            Value::Number(number) => *bytes += int_bit_length(number) * 30103 / 100000 + 2,
        }
        if *bytes > max {
            return Err(limit());
        }
        Ok(())
    }
    visit(value, 0, &mut nodes, &mut string_bytes, max_bytes)?;
    let text = pyjson::dumps(value, pyjson::SPACED_SORTED);
    if text.len() > max_bytes {
        return Err("MCP document exceeds its byte limit".into());
    }
    Ok(text)
}

/// `structured_document`: the payload across field spellings; JSON `null` is absent.
pub fn structured_document(result: &Value) -> Option<&Value> {
    ["structured_content", "structuredContent"]
        .iter()
        .find_map(|name| result.get(*name).filter(|v| !v.is_null()))
}

/// `decode_document`: one complete JSON document, or the reason it is unavailable.
pub fn decode_document(output: &CallOutput) -> Result<Value, String> {
    let raw = match output {
        CallOutput::Text(text) => text.clone(),
        CallOutput::Result(result) => {
            if result.is_error {
                return Err("MCP tool returned an error".into());
            }
            if let Some(error) = result.document_error.as_ref().filter(|e| !e.is_empty()) {
                return Err(error.clone());
            }
            match (&result.structured_json, &result.document_text) {
                (Some(Value::String(text)), _) => text.clone(),
                (Some(other), _) => pyjson::dumps(other, pyjson::SPACED),
                (None, Some(text)) => text.clone(),
                (None, None) => result.text.clone(),
            }
        }
    };
    if py_len(&raw) > MCP_MAX_DOCUMENT_BYTES || raw.len() > MCP_MAX_DOCUMENT_BYTES {
        return Err("MCP document exceeds its byte limit".into());
    }
    let value: Value = serde_json::from_str(&raw)
        .map_err(|_| "MCP tool did not return a complete JSON document".to_string())?;
    encode_document(&value, MCP_MAX_DOCUMENT_BYTES)?;
    Ok(value)
}

// ── content blocks ─────────────────────────────────────────────────────────

/// `bounded_content_blocks`: bound aggregate text before joins/copies; one loss receipt.
pub fn bounded_content_blocks(content: &[Value], max_bytes: usize) -> (Vec<Value>, Option<String>) {
    let mut blocks = Vec::new();
    let mut used = 0usize;
    let mut loss = None;
    for (ordinal, block) in content.iter().enumerate() {
        if ordinal >= MAX_MCP_CONTENT_BLOCKS {
            loss = Some("content_block_limit".to_string());
            blocks.push(json!({"type": "unavailable", "reason": "content_block_limit", "status": "unavailable",
                               "sourceOrdinal": ordinal, "omittedCount": content.len() - ordinal}));
            break;
        }
        let kind = safe_text(field(block, "type"), "unknown");
        let mut entry = Map::new();
        entry.insert("type".into(), Value::String(kind.clone()));
        if kind == "image" {
            for key in ["data", "mimeType", "mime_type", "caption"] {
                if let Some(value) = field_str(block, key) {
                    let value = if key == "data" {
                        value
                    } else {
                        py_prefix(value, 2048)
                    };
                    entry.insert(key.into(), Value::String(value.to_string()));
                }
            }
        }
        let empty = Value::Object(Map::new());
        let resource = if kind == "resource" {
            field(block, "resource").unwrap_or(&empty)
        } else {
            block
        };
        if let Some(text) = field_str(resource, "text") {
            let room = max_bytes.saturating_sub(used);
            let stored = if py_len(text) > room || text.len() > room {
                loss = Some("document_retention_limit".to_string());
                entry.insert("status".into(), "unavailable".into());
                entry.insert("reason".into(), "document_retention_limit".into());
                "[content unavailable: document retention limit]".to_string()
            } else {
                used += text.len() + 1;
                text.to_string()
            };
            entry.insert("text".into(), Value::String(stored));
        }
        let uri = safe_text(field(resource, "uri"), "");
        if !uri.is_empty() {
            entry.insert("uri".into(), Value::String(uri));
        }
        if kind == "resource" {
            let mut resource_entry = Map::new();
            for key in ["text", "uri"] {
                if let Some(value) = entry.remove(key) {
                    resource_entry.insert(key.into(), value);
                }
            }
            if field(resource, "blob").is_some_and(|b| !b.is_null()) {
                resource_entry.insert("blob".into(), Value::Bool(true));
            }
            entry.insert("resource".into(), Value::Object(resource_entry));
        }
        blocks.push(Value::Object(entry));
    }
    (blocks, loss)
}

/// `extract_mcp_image_contents`: bound native data before decoding; rejections stay explicit.
pub fn extract_image_contents(content: &[Value]) -> Vec<Value> {
    let mut images: Vec<Value> = Vec::new();
    let mut total = 0usize;
    let base64_bound = |bytes: usize| 4 * bytes.div_ceil(3);
    for (ordinal, block) in content.iter().enumerate() {
        if ordinal >= MAX_MCP_CONTENT_BLOCKS {
            images.push(
                json!({"status": "unavailable", "reason": "content_block_limit",
                               "sourceOrdinal": ordinal, "mimeType": ""}),
            );
            break;
        }
        if safe_text(field(block, "type"), "").to_lowercase() != "image" {
            continue;
        }
        let data = field(block, "data");
        let raw_mime = match field(block, "mimeType").filter(|m| pyjson_truthy(m)) {
            Some(m) => Some(m),
            None => field(block, "mime_type"),
        };
        let mut mime = pyjson::py_strip(&safe_text(raw_mime, "").to_lowercase()).to_string();
        if mime == "image/jpg" {
            mime = "image/jpeg".into();
        }
        let mut item = Map::new();
        item.insert("mimeType".into(), Value::String(mime.clone()));
        item.insert("sourceOrdinal".into(), Value::from(ordinal));
        if let Some(caption) = field_str(block, "caption") {
            item.insert(
                "caption".into(),
                Value::String(py_prefix(caption, 2048).to_string()),
            );
        }
        if images.len() >= MAX_MCP_IMAGES_PER_TURN {
            item.insert("status".into(), "unavailable".into());
            item.insert("reason".into(), "image_count_limit".into());
            item.insert("aggregate".into(), Value::Bool(true));
            images.push(Value::Object(item));
            break;
        }
        let reason = match data.and_then(Value::as_str) {
            _ if !ALLOWED_IMAGE_MIME_TYPES.contains(&mime.as_str()) => Some("unsupported_mime"),
            None | Some("") => Some("invalid_base64"),
            Some(d) if py_len(d) > base64_bound(MAX_MCP_IMAGE_BYTES) => Some("image_byte_limit"),
            Some(d) if total + py_len(d) > base64_bound(MAX_MCP_IMAGE_TOTAL_BYTES) => {
                Some("image_total_byte_limit")
            }
            Some(_) => None,
        };
        match reason {
            Some(reason) => {
                item.insert("status".into(), "unavailable".into());
                item.insert("reason".into(), reason.into());
            }
            None => {
                let d = data.and_then(Value::as_str).unwrap_or_default();
                item.insert("data".into(), Value::String(d.to_string()));
                total += py_len(d);
            }
        }
        images.push(Value::Object(item));
    }
    images
}

fn pyjson_truthy(value: &Value) -> bool {
    crate::pyos::truthy(Some(value))
}

/// `MCPBridge._extract_text` over bounded blocks.
pub fn extract_text(content: &[Value]) -> String {
    let (bounded, _) = bounded_content_blocks(content, MCP_MAX_RETAINED_DOCUMENT_BYTES);
    let mut parts: Vec<String> = Vec::new();
    for block in &bounded {
        let kind = safe_text(field(block, "type"), "unknown");
        if let Some(text) = field_str(block, "text") {
            parts.push(text.to_string());
        } else if kind == "resource" {
            let resource_text = block
                .get("resource")
                .and_then(|r| r.get("text"))
                .and_then(Value::as_str);
            parts.push(resource_text.map_or(
                "[resource content unavailable: unsupported binary resource]".into(),
                str::to_string,
            ));
        } else if kind == "image"
            || kind == "audio"
            || field(block, "data").is_some_and(|d| !d.is_null())
        {
            let raw = match field(block, "mimeType").filter(|m| pyjson_truthy(m)) {
                Some(m) => Some(m),
                None => field(block, "mime_type"),
            };
            let mime = safe_text(raw, "").to_lowercase();
            if kind == "image"
                && !(ALLOWED_IMAGE_MIME_TYPES.contains(&mime.as_str()) || mime == "image/jpg")
            {
                parts.push("[image content unavailable: unsupported MIME type]".into());
            } else {
                parts.push(format!("[{kind} content]"));
            }
        } else if let Some(uri) = field(block, "uri").filter(|u| pyjson_truthy(u)) {
            parts.push(format!("[Resource: {}]", crate::pyos::py_str(uri)));
        } else {
            parts.push(format!("[{kind} content]"));
        }
    }
    parts.join("\n")
}

/// `MCPBridge._merge_result_parts`.
pub fn merge_result_parts(text: &str, structured: Option<&str>) -> String {
    let structured = pyjson::py_strip(structured.unwrap_or_default());
    if structured.is_empty() {
        return text.to_string();
    }
    if pyjson::py_strip(text).is_empty() {
        return structured.to_string();
    }
    if text.contains(structured) {
        return text.to_string();
    }
    let mut candidate = pyjson::py_strip(text);
    if let Some(rest) = candidate.strip_prefix("MCP Error: ") {
        candidate = pyjson::py_strip(rest);
    }
    if py_len(candidate) <= MCP_MAX_RESULT_CHARS && py_len(structured) <= MCP_MAX_RESULT_CHARS {
        let canonical = |raw: &str| {
            serde_json::from_str::<Value>(raw)
                .ok()
                .map(|v| pyjson::dumps(&v, pyjson::COMPACT_SORTED_ASCII))
        };
        if let (Some(a), Some(b)) = (canonical(candidate), canonical(structured)) {
            if a == b {
                return text.to_string();
            }
        }
    }
    format!("{text}\n\n[Structured result]\n{structured}")
}

/// `_tool_result_is_error`: the canonical snake_case field wins whenever it is present.
pub fn tool_result_is_error(result: &Value) -> bool {
    match result.get("is_error") {
        Some(flag) => pyjson_truthy(flag),
        None => result.get("isError").is_some_and(pyjson_truthy),
    }
}

/// `MCPBridge._extract_structured_content`: the stable JSON text, or None when absent or empty.
pub fn extract_structured_content(result: &Value) -> Result<Option<String>, String> {
    let Some(document) = structured_document(result) else {
        return Ok(None);
    };
    let text = encode_document(document, MCP_MAX_DOCUMENT_BYTES)?;
    Ok((!matches!(text.as_str(), "{}" | "[]" | "")).then_some(text))
}

/// The full post-call projection of one wire `CallToolResult`.
pub fn project_call_result(result: &Value) -> ToolResult {
    let is_error = tool_result_is_error(result);
    let document = structured_document(result);
    let content: Vec<Value> = result
        .get("content")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let (bounded, ingress_loss) = bounded_content_blocks(&content, MCP_MAX_RETAINED_DOCUMENT_BYTES);
    let image_contents = extract_image_contents(&bounded);
    let original_text = extract_text(&bounded);
    let mut structured_json: Option<String> = None;
    let mut document_error: Option<String> = None;
    if let Some(document) = document {
        match encode_document(document, MCP_MAX_DOCUMENT_BYTES) {
            Ok(text) => structured_json = Some(text),
            Err(error) => document_error = Some(error),
        }
    } else if py_len(&original_text) > MCP_MAX_RESULT_CHARS
        && !(py_len(&original_text) <= MCP_MAX_DOCUMENT_BYTES
            && original_text.len() <= MCP_MAX_DOCUMENT_BYTES)
    {
        document_error = Some("MCP document exceeds its byte limit".into());
    }
    let mut structured_text = structured_json.clone().filter(|s| s != "{}" && s != "[]");
    if let (Some(error), Some(_)) = (&document_error, document) {
        structured_text = Some(format!("[Structured result omitted: {error}]"));
    }
    let mut text = original_text.clone();
    let mut retention_error = ingress_loss;
    if let (Some(document), None) = (document, &structured_json) {
        match encode_document(document, MCP_MAX_RETAINED_DOCUMENT_BYTES) {
            Ok(encoded) => structured_json = Some(encoded),
            Err(error) => retention_error = Some(error),
        }
    }
    let combined = original_text.len() + structured_json.as_ref().map_or(0, String::len) + 64;
    if combined > MCP_MAX_RETAINED_DOCUMENT_BYTES {
        retention_error = Some("MCP canonical document exceeds retention byte limit".into());
        structured_json = None;
        text = format!(
            "{}\n[Aggregate document unavailable: retention byte limit]",
            py_prefix(&original_text, MCP_MAX_RESULT_CHARS)
        );
    } else if let Some(structured) = &structured_json {
        text = merge_result_parts(&original_text, Some(structured));
    } else if document_error.is_some() && document.is_some() {
        text = merge_result_parts(&original_text, structured_text.as_deref());
    }
    if is_error {
        text = format!("MCP Error: {text}");
    }
    let mut document_text = if retention_error.is_some() {
        None
    } else {
        Some(text.clone())
    };
    if text.len() > MCP_MAX_RETAINED_DOCUMENT_BYTES {
        retention_error = Some("MCP canonical document exceeds retention byte limit".into());
        document_text = None;
    }
    let mut blocks = Vec::new();
    for (ordinal, block) in bounded.iter().enumerate() {
        let kind = crate::pyos::py_str(
            block
                .get("type")
                .unwrap_or(&Value::String("unknown".into())),
        );
        let mut entry = Map::new();
        entry.insert("type".into(), Value::String(kind.clone()));
        entry.insert("sourceOrdinal".into(), Value::from(ordinal));
        if field_str(block, "status") == Some("unavailable") {
            entry.insert("status".into(), "unavailable".into());
            entry.insert(
                "reason".into(),
                block.get("reason").cloned().unwrap_or(Value::Null),
            );
            if let Some(count) = block.get("omittedCount").filter(|c| !c.is_null()) {
                entry.insert("omittedCount".into(), count.clone());
            }
        }
        match kind.as_str() {
            "text" => {
                entry.insert(
                    "text".into(),
                    block
                        .get("text")
                        .cloned()
                        .unwrap_or(Value::String(String::new())),
                );
            }
            "image" => {
                entry.insert("mediaOrdinal".into(), Value::from(ordinal));
            }
            "resource" => {
                let resource = block
                    .get("resource")
                    .cloned()
                    .unwrap_or(Value::Object(Map::new()));
                if let Some(t) = field_str(&resource, "text") {
                    entry.insert("text".into(), Value::String(t.into()));
                }
                if let Some(uri) = resource.get("uri").filter(|u| pyjson_truthy(u)) {
                    entry.insert("uri".into(), uri.clone());
                }
                if resource.get("blob").is_some_and(|b| !b.is_null()) {
                    entry.insert("status".into(), "unavailable".into());
                    entry.insert("reason".into(), "unsupported_resource_blob".into());
                }
            }
            _ => {
                if let Some(uri) = block.get("uri").filter(|u| pyjson_truthy(u)) {
                    entry.insert("uri".into(), uri.clone());
                }
            }
        }
        if document_text.is_none() && entry.remove("text").is_some() {
            entry.insert("status".into(), "unavailable".into());
            entry.insert("reason".into(), "document_retention_limit".into());
        }
        blocks.push(Value::Object(entry));
    }
    let chars = py_len(&text);
    if chars > MCP_MAX_RESULT_CHARS {
        text = format!(
            "{}\n\n[Truncated: {} chars total, showing first {}]",
            py_prefix(&text, MCP_MAX_RESULT_CHARS),
            thousands(chars),
            thousands(MCP_MAX_RESULT_CHARS)
        );
    }
    ToolResult {
        text,
        image_contents,
        structured_json: structured_json.map(Value::String),
        document_text,
        document_error,
        is_error,
        content_blocks: blocks,
        retention_error,
    }
}

/// `_with_credential_notice`: guidance on an error result only; every other result stays byte-exact.
pub fn with_credential_notice(output: CallOutput, notice: &str) -> CallOutput {
    match output {
        CallOutput::Result(mut result) if !notice.is_empty() && result.is_error => {
            let suffix = format!("\n\n{notice}");
            result.text.push_str(&suffix);
            if let Some(document) = result.document_text.as_mut() {
                document.push_str(&suffix);
            }
            CallOutput::Result(result)
        }
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_structured_and_error_projection() {
        let plain = project_call_result(&json!({"content": [{"type": "text", "text": "hi"}]}));
        assert_eq!(plain.text, "hi");
        assert_eq!(plain.document_text.as_deref(), Some("hi"));
        assert_eq!(
            plain.content_blocks,
            vec![json!({"type": "text", "sourceOrdinal": 0, "text": "hi"})]
        );
        let echoed = project_call_result(&json!({
            "content": [{"type": "text", "text": "{\"b\": 1, \"a\": 2}"}],
            "structuredContent": {"a": 2, "b": 1}
        }));
        assert_eq!(echoed.text, "{\"b\": 1, \"a\": 2}");
        assert_eq!(
            echoed.structured_json,
            Some(Value::String("{\"a\": 2, \"b\": 1}".into()))
        );
        let both = project_call_result(&json!({"content": [{"type": "text", "text": "done"}],
                                               "structuredContent": {"n": 1.0}, "isError": true}));
        assert_eq!(
            both.text,
            "MCP Error: done\n\n[Structured result]\n{\"n\": 1.0}"
        );
        let long = "x".repeat(MCP_MAX_RESULT_CHARS + 5);
        let truncated = project_call_result(&json!({"content": [{"type": "text", "text": long}]}));
        assert!(truncated
            .text
            .ends_with("[Truncated: 200,005 chars total, showing first 200,000]"));
        assert_eq!(truncated.document_error.as_deref(), None);
    }

    #[test]
    fn media_never_leaks_base64_into_descriptors() {
        let result = project_call_result(&json!({"content": [
            {"type": "image", "data": "AAAA", "mimeType": "image/jpg"},
            {"type": "image", "data": "AAAA", "mimeType": "image/tiff"},
            {"type": "resource", "resource": {"uri": "file:///x", "blob": "QUJD"}},
            {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
        ]}));
        assert_eq!(
            result.text,
            "[image content]\n[image content unavailable: unsupported MIME type]\n\
             [resource content unavailable: unsupported binary resource]\n[audio content]"
        );
        assert_eq!(result.image_contents[0]["mimeType"], "image/jpeg");
        assert_eq!(result.image_contents[1]["reason"], "unsupported_mime");
        assert_eq!(
            result.content_blocks[2]["reason"],
            "unsupported_resource_blob"
        );
        assert!(!serde_json::to_string(&result.content_blocks)
            .unwrap()
            .contains("QUJD"));
    }

    #[test]
    fn snake_case_error_flag_and_structured_payload_win() {
        assert!(!tool_result_is_error(
            &json!({"is_error": false, "isError": true})
        ));
        assert!(!tool_result_is_error(
            &json!({"is_error": null, "isError": true})
        ));
        assert!(tool_result_is_error(&json!({"isError": true})));
        assert_eq!(
            extract_structured_content(
                &json!({"structured_content": {"v": "snake"}, "structuredContent": {"v": "camel"}})
            ),
            Ok(Some("{\"v\": \"snake\"}".into()))
        );
        assert_eq!(
            extract_structured_content(&json!({"structuredContent": []})),
            Ok(None)
        );
    }

    #[test]
    fn documents_are_bounded() {
        assert_eq!(
            encode_document(&json!({"b": [1, 2.5], "a": "é"}), 1024).unwrap(),
            "{\"a\": \"é\", \"b\": [1, 2.5]}"
        );
        let mut deep = json!(1);
        for _ in 0..40 {
            deep = json!([deep]);
        }
        assert_eq!(
            encode_document(&deep, 1024).unwrap_err(),
            "MCP document exceeds its structure limit"
        );
        let ok = CallOutput::Result(project_call_result(
            &json!({"content": [], "structuredContent": {"k": 1}}),
        ));
        assert_eq!(decode_document(&ok).unwrap(), json!({"k": 1}));
        let error = CallOutput::Result(ToolResult {
            is_error: true,
            ..Default::default()
        });
        assert_eq!(
            decode_document(&error).unwrap_err(),
            "MCP tool returned an error"
        );
    }
}
