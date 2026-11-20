//! Credential redaction at tool settlement: every known credential value in
//! a tool result is replaced by `[redacted credential: <label>]` before the
//! settlement commit, the events and the model see it.
//!
//! Replaces the settlement call site in
//! `lib/tasks_pkg/tool_dispatch/_pipeline.py` (after post-tool hooks, before
//! the empty-result marker and `capture_result`), which applies
//! `lib/tool_results/credential_redaction.py` `known_credential_redactor()` to
//! the model-visible content and to the canonical source. Contract:
//! `docs/contracts/TOOL_EXECUTION_POLICY.md`, "Known credentials are redacted
//! at write time".
//!
//! Entry points: `SettlementRedactor` (the port), `redact_tool_content`,
//! `redact_tool_run`. The production host binds the port to
//! `tofu_storage_client::CredentialRedactor::current(Some(owner))`
//! (`Arc<KnownCredentialRedactor>`), which owns the vault and
//! `server_config.json` scan and its block-filter matcher; this module never
//! reads credentials.
//!
//! Scope, as in Python: only tool results are redacted at settlement.
//! Assistant (model) output is not rewritten in the loop; its durable copies
//! are redacted by the storage write seam (`StorageV2Client::command`), which
//! the production `DurableStore` adapter goes through.
//!
//! Owner model-routing keys join the known set through
//! `tofu_storage_client::ModelRoutingKnownSecrets` (the storage client's
//! `OwnerCredentialSource`); `storage_v2::StorageCredentialRedactor` is the
//! production binding. `ExactValueRedactor` is the minimal exact-value
//! implementation for tests and the parity harness: same minimum length,
//! variants (raw, `quote(v, safe='')`, `quote_plus(v, safe='')`), first label
//! wins, longest variant replaced first, and the same marker text.

use serde_json::Value;

use super::types::ToolRun;

pub trait SettlementRedactor: Send + Sync {
    /// Redact every string inside JSON-shaped data; object keys are kept.
    /// `owner_user_id` selects the owner's known set (vault, provider keys and
    /// the owner's model-routing keys).
    fn redact_value(&self, owner_user_id: u64, v: &Value) -> Value;
}

/// `_MIN_CREDENTIAL_CHARS`: shorter strings are placeholders, not credentials.
pub const MIN_CREDENTIAL_CHARS: usize = 8;

/// `_marker(label)`.
pub fn credential_marker(label: &str) -> String {
    format!("[redacted credential: {label}]")
}

/// Python `urllib.parse.quote(value, safe='')` (`plus = false`) or
/// `quote_plus(value, safe='')` (`plus = true`) over UTF-8.
fn percent_encode(value: &str, plus: bool) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(byte as char)
            }
            b' ' if plus => out.push('+'),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Exact-value redactor for tests (see module header).
#[derive(Debug, Clone, Default)]
pub struct ExactValueRedactor {
    /// (variant, marker), longest variant first.
    variants: Vec<(String, String)>,
}

impl ExactValueRedactor {
    pub fn new<'a>(labelled_values: impl IntoIterator<Item = (&'a str, &'a str)>) -> Self {
        let mut variants: Vec<(String, String)> = Vec::new();
        for (label, value) in labelled_values {
            let value = value.trim();
            if value.chars().count() < MIN_CREDENTIAL_CHARS {
                continue;
            }
            let marker = credential_marker(label);
            for variant in [
                value.to_string(),
                percent_encode(value, false),
                percent_encode(value, true),
            ] {
                if !variants.iter().any(|(known, _)| *known == variant) {
                    variants.push((variant, marker.clone()));
                }
            }
        }
        variants.sort_by_key(|(variant, _)| std::cmp::Reverse(variant.chars().count()));
        Self { variants }
    }

    pub fn redact(&self, text: &str) -> String {
        let mut text = text.to_string();
        for (variant, marker) in &self.variants {
            if text.contains(variant.as_str()) {
                text = text.replace(variant.as_str(), marker);
            }
        }
        text
    }

    pub fn redact_json(&self, v: &Value) -> Value {
        match v {
            Value::String(text) => Value::String(self.redact(text)),
            Value::Array(items) => {
                Value::Array(items.iter().map(|i| self.redact_json(i)).collect())
            }
            Value::Object(map) => Value::Object(
                map.iter()
                    .map(|(key, item)| (key.clone(), self.redact_json(item)))
                    .collect(),
            ),
            other => other.clone(),
        }
    }
}

/// One known set for every owner: tests have no per-owner model-routing keys.
impl SettlementRedactor for ExactValueRedactor {
    fn redact_value(&self, _owner_user_id: u64, v: &Value) -> Value {
        self.redact_json(v)
    }
}

/// Redact one settled tool content string (model text, event text and the
/// stored round all derive from this one result).
pub fn redact_tool_content(
    redactor: &dyn SettlementRedactor,
    owner_user_id: u64,
    content: &str,
) -> String {
    match redactor.redact_value(owner_user_id, &Value::String(content.to_string())) {
        Value::String(text) => text,
        other => other.to_string(),
    }
}

/// Redact every text a tool run carries before the loop settles it.
pub fn redact_tool_run(
    redactor: &dyn SettlementRedactor,
    owner_user_id: u64,
    run: &ToolRun,
) -> ToolRun {
    let redact = |text: &str| redact_tool_content(redactor, owner_user_id, text);
    match run {
        ToolRun::Finished { output, is_error } => ToolRun::Finished {
            output: redact(output),
            is_error: *is_error,
        },
        ToolRun::RefusedBeforeEntry { reason } => ToolRun::RefusedBeforeEntry {
            reason: redact(reason),
        },
        ToolRun::LostAfterEntry { detail } => ToolRun::LostAfterEntry {
            detail: redact(detail),
        },
        ToolRun::Cancelled { entered } => ToolRun::Cancelled { entered: *entered },
        ToolRun::Raised {
            type_name,
            detail,
            class,
        } => ToolRun::Raised {
            type_name: type_name.clone(),
            detail: redact(detail),
            class: class.clone(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// TOOL_EXECUTION_POLICY.md "Known credentials are redacted at write time":
    /// a known value printed by any tool never reaches the settlement, raw or
    /// URL-encoded; the marker text matches `credential_redaction._marker`
    /// (tests/credentials_vault/test_known_credential_redaction.py).
    #[test]
    fn known_credential_is_redacted_in_tool_output_before_settlement() {
        let redactor =
            ExactValueRedactor::new([("GITHUB_TOKEN", "ghp_secret value/1"), ("SHORT", "sk-xxx")]);
        let run = ToolRun::Finished {
            output: "token=ghp_secret value/1 url=ghp_secret%20value%2F1 form=ghp_secret+value%2F1 sk-xxx".into(),
            is_error: false,
        };
        let ToolRun::Finished { output, .. } = redact_tool_run(&redactor, 1, &run) else {
            unreachable!()
        };
        assert_eq!(
            output,
            "token=[redacted credential: GITHUB_TOKEN] url=[redacted credential: GITHUB_TOKEN] form=[redacted credential: GITHUB_TOKEN] sk-xxx"
        );
        let canonical = json!({"ghp_secret value/1": ["Bearer ghp_secret value/1", 3]});
        assert_eq!(
            redactor.redact_value(1, &canonical),
            json!({"ghp_secret value/1": ["Bearer [redacted credential: GITHUB_TOKEN]", 3]})
        );
        let lost = ToolRun::LostAfterEntry {
            detail: "ghp_secret value/1".into(),
        };
        assert_eq!(
            redact_tool_run(&redactor, 1, &lost),
            ToolRun::LostAfterEntry {
                detail: "[redacted credential: GITHUB_TOKEN]".into()
            }
        );
    }

    /// The longest variant wins over a contained shorter value; the first label wins.
    #[test]
    fn longest_known_value_is_replaced_first_and_first_label_wins() {
        let redactor = ExactValueRedactor::new([
            ("A", "abcdefgh"),
            ("B", "abcdefgh-longer"),
            ("C", "abcdefgh"),
        ]);
        assert_eq!(
            redactor.redact("abcdefgh-longer abcdefgh"),
            "[redacted credential: B] [redacted credential: A]"
        );
        assert_eq!(ExactValueRedactor::default().redact("abcdefgh"), "abcdefgh");
    }
}
