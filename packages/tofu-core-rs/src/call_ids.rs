//! Tool-call correlation IDs on the wire (`lib/llm/tool_call_ids.py`): the
//! contract one request must satisfy and the deterministic respelling of
//! every ID it rejects. The prototype applies the one map to the canonical
//! messages before protocol translation; each Python emitter reads exactly
//! `tool_calls[].id` and `tool_call_id` through `wire_ids.get(id, id)`, so
//! the emitted bytes are the same.

use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::family::creator_family;

const DIGEST_ALPHABET: &[u8; 62] =
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
/// `MISTRAL_TOOL_CALL_ID_CHARS`: Mistral's fixed nine `[a-zA-Z0-9]`.
const MISTRAL_TOOL_CALL_ID_CHARS: usize = 9;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Charset {
    Any,
    /// `[a-zA-Z0-9_-]` (Anthropic Messages).
    Messages,
    /// `[a-zA-Z0-9]` (Mistral).
    Alnum,
}

impl Charset {
    fn accepts(self, c: char) -> bool {
        match self {
            Self::Any => true,
            Self::Messages => c.is_ascii_alphanumeric() || c == '_' || c == '-',
            Self::Alnum => c.is_ascii_alphanumeric(),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Contract {
    charset: Charset,
    max_length: Option<usize>,
    fixed_length: bool,
}

impl Contract {
    fn accepts(&self, value: &str) -> bool {
        let length = value.chars().count();
        !value.is_empty()
            && self.max_length.is_none_or(|max| length <= max)
            && !(self.fixed_length && Some(length) != self.max_length)
            && value.chars().all(|c| self.charset.accepts(c))
    }
}

const ANTHROPIC_MESSAGES: Contract = Contract {
    charset: Charset::Messages,
    max_length: None,
    fixed_length: false,
};
const MISTRAL: Contract = Contract {
    charset: Charset::Alnum,
    max_length: Some(MISTRAL_TOOL_CALL_ID_CHARS),
    fixed_length: true,
};

/// `tool_call_id_contract(protocol, logical_model)`.
pub fn contract(protocol: &str, logical_model: &str) -> Contract {
    let family = creator_family(logical_model);
    if family == Some("mistral") {
        return MISTRAL;
    }
    let max_length = match protocol {
        "anthropic" => return ANTHROPIC_MESSAGES,
        "responses" => 64,
        _ => 40,
    };
    Contract {
        charset: if family == Some("anthropic") {
            Charset::Messages
        } else {
            Charset::Any
        },
        max_length: Some(max_length),
        fixed_length: false,
    }
}

/// `_digest`: nine base-62 digits of SHA-256, least significant first.
fn digest(text: &str) -> String {
    let mut number: Vec<u8> = Sha256::digest(text.as_bytes()).to_vec();
    let mut out = String::with_capacity(MISTRAL_TOOL_CALL_ID_CHARS);
    for _ in 0..MISTRAL_TOOL_CALL_ID_CHARS {
        let mut remainder: u32 = 0;
        for byte in number.iter_mut() {
            let value = (remainder << 8) | u32::from(*byte);
            *byte = (value / 62) as u8;
            remainder = value % 62;
        }
        out.push(DIGEST_ALPHABET[remainder as usize] as char);
    }
    out
}

fn respelled(original: &str, contract: &Contract, attempt: usize) -> String {
    let digest = digest(&if attempt == 0 {
        original.to_owned()
    } else {
        format!("{original}\u{0}{attempt}")
    });
    if contract.fixed_length {
        return digest;
    }
    let mut stem: String = original
        .chars()
        .map(|c| if contract.charset.accepts(c) { c } else { '_' })
        .collect();
    if let Some(max) = contract.max_length {
        let room = max.saturating_sub(digest.chars().count() + 1);
        stem = stem.chars().take(room).collect();
    }
    if stem.is_empty() {
        digest
    } else {
        format!("{stem}_{digest}")
    }
}

fn correlation_ids(message: &Value) -> Vec<&str> {
    let mut ids = Vec::new();
    if let Some(Value::Array(calls)) = message.get("tool_calls") {
        ids.extend(
            calls
                .iter()
                .filter_map(|call| call.get("id").and_then(Value::as_str)),
        );
    }
    if let Some(id) = message.get("tool_call_id").and_then(Value::as_str) {
        ids.push(id);
    }
    ids
}

/// `wire_tool_call_ids`: every rejected ID → its unique wire spelling.
/// Canonical bodies carry no native sidecars here, so no opaque IDs are taken.
fn wire_ids(messages: &[Value], contract: &Contract) -> Vec<(String, String)> {
    let mut originals: Vec<&str> = Vec::new();
    for message in messages.iter().filter(|m| m.is_object()) {
        for id in correlation_ids(message) {
            if !originals.contains(&id) {
                originals.push(id);
            }
        }
    }
    let rejected: Vec<&str> = originals
        .iter()
        .copied()
        .filter(|id| !id.is_empty() && !contract.accepts(id))
        .collect();
    if rejected.is_empty() {
        return Vec::new();
    }
    let mut taken: Vec<String> = originals
        .iter()
        .filter(|id| contract.accepts(id))
        .map(|id| (*id).to_owned())
        .collect();
    let mut spelled = Vec::new();
    for original in rejected {
        let mut attempt = 0;
        let mut candidate = respelled(original, contract, attempt);
        while taken.contains(&candidate) {
            attempt += 1;
            candidate = respelled(original, contract, attempt);
        }
        taken.push(candidate.clone());
        spelled.push((original.to_owned(), candidate));
    }
    spelled
}

/// Respell, in place, every correlation ID `contract` rejects.
pub fn respell(messages: &mut [Value], contract: &Contract) {
    let map = wire_ids(messages, contract);
    if map.is_empty() {
        return;
    }
    let lookup = |id: &str| map.iter().find(|(from, _)| from == id).map(|(_, to)| to);
    for message in messages.iter_mut() {
        if let Some(Value::Array(calls)) = message.get_mut("tool_calls") {
            for call in calls.iter_mut() {
                let replacement = call.get("id").and_then(Value::as_str).and_then(lookup);
                if let Some(to) = replacement {
                    call["id"] = Value::String(to.clone());
                }
            }
        }
        let replacement = message
            .get("tool_call_id")
            .and_then(Value::as_str)
            .and_then(lookup);
        if let Some(to) = replacement {
            message["tool_call_id"] = Value::String(to.clone());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn mistral_ids_become_nine_alphanumerics_and_stay_paired() {
        let mut messages = vec![
            json!({"role": "assistant", "tool_calls": [{"id": "call_a1", "type": "function"}]}),
            json!({"role": "tool", "tool_call_id": "call_a1", "content": "x"}),
        ];
        respell(&mut messages, &contract("openai", "mistral-large-4-0"));
        let id = messages[0]["tool_calls"][0]["id"].as_str().unwrap();
        assert_eq!(id.len(), 9);
        assert!(id.chars().all(|c| c.is_ascii_alphanumeric()));
        assert_eq!(messages[1]["tool_call_id"], id);
    }

    #[test]
    fn accepted_ids_are_untouched() {
        let mut messages = vec![json!({"role": "tool", "tool_call_id": "call_a1"})];
        respell(&mut messages, &contract("openai", "glm-5.3"));
        assert_eq!(messages[0]["tool_call_id"], "call_a1");
    }
}
