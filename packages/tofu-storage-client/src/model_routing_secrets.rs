//! Read-only port of the model-routing owner's decrypting read
//! (`lib/model_routing/known_secrets.owner_credential_keys`) for write-time
//! redaction.
//!
//! It yields `(credential_id, usable key)` for every credential of one owner,
//! enabled or not, exactly as the Python read does: the aggregate comes from
//! `model_routing.get`, each referenced secret from `model_routing.secret.get`
//! (both through the client it is bound to), and each ciphertext is opened as
//! `lib/secret_envelope.open_secret` opens it: Fernet under
//! `TOFU_SECRET_ENCRYPTION_KEY` or the personal key file, then the exact
//! `{version, purpose, owner_user_id, record_id}` binding. Envelope secrets
//! (`tofu.credential-secret/v1`) yield their `api_key`; raw secrets of
//! `api_key` credentials yield themselves; other kinds and missing secrets are
//! skipped; any other failure fails the whole read, which the redactor treats
//! like the Python one (keep the previous set, retry on the next call).
//!
//! It never writes: no key is minted, no stamp is touched, no plaintext is
//! cached; the caller keeps only fingerprints. One deliberate difference: the
//! aggregate is not re-validated by `normalize_document`. A document Python
//! would reject fails the Python read (owner keys unknown); here its keys are
//! still recognised, which only ever redacts more.
//!
//! Entry points: [`ModelRoutingKnownSecrets`] and
//! [`StorageV2Client::install_credential_redaction`].

use std::path::{Path, PathBuf};
use std::sync::{Arc, Weak};

use serde_json::{json, Map, Value};

use crate::redaction::{fernet_decrypt, python_space, python_str, python_truthy};
use crate::{CredentialFiles, CredentialRedactor, OwnerCredentialSource, StorageV2Client};

/// `lib/model_routing/repository._SECRET_PURPOSE`.
const SECRET_PURPOSE: &str = "model-routing-credential";
/// `lib/secret_envelope._ENVELOPE_VERSION`.
const SECRET_ENVELOPE_VERSION: i64 = 1;
/// `lib/secret_envelope._ENVIRONMENT_KEY`.
const SECRET_KEY_ENVIRONMENT: &str = "TOFU_SECRET_ENCRYPTION_KEY";
/// `lib/secret_envelope._KEY_PATH`, relative to the configuration directory.
const SECRET_KEY_FILE: &str = ".secret_encryption.key";
/// `lib/model_routing/credential_envelope.ENVELOPE_FORMAT`.
const CREDENTIAL_ENVELOPE_FORMAT: &str = "tofu.credential-secret/v1";
/// `lib/secret_envelope._binding`: record IDs hold 1..=256 characters.
const MAX_RECORD_ID_CHARS: usize = 256;

/// The model-routing owner's decrypting read, over one storage client.
///
/// Holds the client weakly: the client owns its redactor, which owns this.
pub struct ModelRoutingKnownSecrets {
    client: Weak<StorageV2Client>,
    key_file: PathBuf,
}

impl ModelRoutingKnownSecrets {
    /// `config_dir` holds the personal key file (`lib/config_dir.config_path`).
    pub fn new(client: &Arc<StorageV2Client>, config_dir: &Path) -> Self {
        Self {
            client: Arc::downgrade(client),
            key_file: config_dir.join(SECRET_KEY_FILE),
        }
    }
}

impl OwnerCredentialSource for ModelRoutingKnownSecrets {
    fn owner_credential_keys(&self, owner_user_id: u64) -> Result<Vec<(String, String)>, String> {
        let client = self
            .client
            .upgrade()
            .ok_or("storage client is gone".to_owned())?;
        if owner_user_id != client.owner_id() {
            return Err("model-routing keys belong to another owner's client".to_owned());
        }
        let boundary = json!({"owner_user_id": owner_user_id, "tenant_id": ""});
        let boundary = boundary.as_object().expect("literal object");
        let row = client
            .query("model_routing.get", Some(boundary), None, None)
            .map_err(|error| format!("model_routing.get: {}", error.code))?;
        if row.is_null() {
            return Ok(Vec::new());
        }
        let document = &row["document"];
        if !same_integer(&document["revision"], &row["revision"]) {
            return Err("stored aggregate revision does not match its repository row".to_owned());
        }
        let mut credentials: Vec<&Map<String, Value>> = match &document["credentials"] {
            Value::Array(rows) => rows.iter().filter_map(Value::as_object).collect(),
            _ => Vec::new(),
        };
        // normalize_document orders credentials by ID; the first label of a
        // value shared by two credentials must match.
        credentials.sort_by_key(|credential| text(credential.get("credential_id")));
        let mut key = SecretKey::new(&self.key_file);
        let mut keys = Vec::new();
        for credential in credentials {
            let reference = text(credential.get("secret_reference"));
            if reference.is_empty() {
                continue;
            }
            let Some(plaintext) = resolve_secret(&client, boundary, &reference, &mut key)? else {
                continue;
            };
            let usable = match credential_envelope(&plaintext) {
                Some(envelope) => text(envelope.get("api_key")),
                None if credential.get("kind").and_then(Value::as_str) == Some("api_key") => {
                    plaintext
                }
                None => continue,
            };
            if !usable.is_empty() {
                let id = text(credential.get("credential_id"));
                keys.push((if id.is_empty() { "?".to_owned() } else { id }, usable));
            }
        }
        Ok(keys)
    }
}

/// `ModelRoutingRepository.resolve_secret`; `None` is a missing secret.
fn resolve_secret(
    client: &StorageV2Client,
    boundary: &Map<String, Value>,
    reference: &str,
    key: &mut SecretKey,
) -> Result<Option<String>, String> {
    let reference = reference.trim_matches(python_space);
    if reference.is_empty() {
        return Ok(Some(String::new()));
    }
    let mut payload = boundary.clone();
    payload.insert("secret_reference".to_owned(), Value::from(reference));
    let row = client
        .query("model_routing.secret.get", Some(&payload), None, None)
        .map_err(|error| format!("model_routing.secret.get: {}", error.code))?;
    if row.is_null() {
        return Ok(None);
    }
    let owner = client.owner_id();
    open_secret(&python_str(&row["ciphertext"]), owner, reference, key).map(Some)
}

/// `lib/secret_envelope.open_secret` with the model-routing purpose.
fn open_secret(
    ciphertext: &str,
    owner_user_id: u64,
    record_id: &str,
    key: &mut SecretKey,
) -> Result<String, String> {
    let record_count = record_id.chars().count();
    if record_count == 0 || record_count > MAX_RECORD_ID_CHARS {
        return Err("secret record_id must be 1-256 characters".to_owned());
    }
    let raw = fernet_decrypt(key.material()?, ciphertext)
        .ok_or("stored secret could not be decrypted".to_owned())?;
    let envelope: Value = std::str::from_utf8(&raw)
        .ok()
        .and_then(|text| serde_json::from_str(text).ok())
        .ok_or("stored secret could not be decrypted".to_owned())?;
    let bound = envelope.as_object().is_some_and(|envelope| {
        python_int_eq(envelope.get("version"), SECRET_ENVELOPE_VERSION)
            && envelope.get("purpose").and_then(Value::as_str) == Some(SECRET_PURPOSE)
            && python_int_eq(envelope.get("owner_user_id"), owner_user_id as i64)
            && envelope.get("record_id").and_then(Value::as_str) == Some(record_id)
    });
    if !bound {
        return Err("stored secret ownership binding is invalid".to_owned());
    }
    match envelope.get("value") {
        Some(Value::String(value)) => Ok(value.clone()),
        _ => Err("stored secret payload is invalid".to_owned()),
    }
}

/// `TOFU_SECRET_ENCRYPTION_KEY`, else the personal key file, read once per
/// read and only when a secret is opened. A missing file fails the read: the
/// Python owner would mint a new key, which cannot open existing secrets.
struct SecretKey<'a> {
    file: &'a Path,
    material: Option<Vec<u8>>,
}

impl<'a> SecretKey<'a> {
    fn new(file: &'a Path) -> Self {
        Self {
            file,
            material: None,
        }
    }

    fn material(&mut self) -> Result<&[u8], String> {
        if self.material.is_none() {
            let configured = std::env::var(SECRET_KEY_ENVIRONMENT).unwrap_or_default();
            let configured = configured.trim_matches(python_space);
            self.material = Some(if configured.is_empty() {
                std::fs::read(self.file)
                    .map_err(|_| "secret encryption key could not be read".to_owned())?
                    .trim_ascii()
                    .to_vec()
            } else {
                configured.as_bytes().to_vec()
            });
        }
        Ok(self.material.as_deref().expect("set above"))
    }
}

/// `decode_credential_envelope`.
fn credential_envelope(plaintext: &str) -> Option<Map<String, Value>> {
    match serde_json::from_str(plaintext) {
        Ok(Value::Object(envelope))
            if envelope.get("format").and_then(Value::as_str)
                == Some(CREDENTIAL_ENVELOPE_FORMAT) =>
        {
            Some(envelope)
        }
        _ => None,
    }
}

/// Python `str(value or "")`.
fn text(value: Option<&Value>) -> String {
    match value {
        Some(value) if python_truthy(value) => python_str(value),
        _ => String::new(),
    }
}

/// Python `value == expected` for a JSON value and an `int`.
fn python_int_eq(value: Option<&Value>, expected: i64) -> bool {
    match value {
        Some(Value::Bool(flag)) => i64::from(*flag) == expected,
        Some(Value::Number(number)) => number
            .as_i64()
            .map(|n| n == expected)
            .unwrap_or_else(|| number.as_f64() == Some(expected as f64)),
        _ => false,
    }
}

/// The aggregate's revision equals its row's (`int(row["revision"])`).
fn same_integer(document: &Value, row: &Value) -> bool {
    let row = match row {
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|f| f.trunc() as i64)),
        Value::String(text) => text.trim().parse().ok(),
        _ => None,
    };
    row.is_some_and(|row| python_int_eq(Some(document), row))
}

impl StorageV2Client {
    /// Install the production write redaction: the vault, `server_config.json`
    /// and this client's owner's model-routing keys, read through this client.
    pub fn install_credential_redaction(
        self: &Arc<Self>,
        config_dir: &Path,
    ) -> Arc<CredentialRedactor> {
        let redactor = Arc::new(CredentialRedactor::new(
            CredentialFiles::in_config_dir(config_dir),
            Arc::new(ModelRoutingKnownSecrets::new(self, config_dir)),
        ));
        self.set_write_redactor(redactor.clone());
        redactor
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_equality_of_binding_integers() {
        assert!(python_int_eq(Some(&json!(1)), 1));
        assert!(python_int_eq(Some(&json!(1.0)), 1));
        assert!(python_int_eq(Some(&json!(true)), 1));
        assert!(!python_int_eq(Some(&json!("1")), 1));
        assert!(!python_int_eq(None, 1));
        assert!(same_integer(&json!(3), &json!("3")));
        assert!(!same_integer(&json!(3), &json!(4)));
    }

    #[test]
    fn only_tagged_envelopes_are_envelopes() {
        assert!(credential_envelope(r#"{"format":"tofu.credential-secret/v1"}"#).is_some());
        assert!(credential_envelope(r#"{"format":"other"}"#).is_none());
        assert!(credential_envelope("sk-raw-key").is_none());
        assert!(credential_envelope("123").is_none());
    }

    #[test]
    fn record_ids_outside_the_binding_bounds_fail_before_decryption() {
        let file = PathBuf::from("/nonexistent/key");
        let mut key = SecretKey::new(&file);
        let long = "r".repeat(MAX_RECORD_ID_CHARS + 1);
        assert_eq!(
            open_secret("token", 7, &long, &mut key).unwrap_err(),
            "secret record_id must be 1-256 characters"
        );
    }
}
