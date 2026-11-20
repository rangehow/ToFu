//! Write-time redaction of known credential values
//! (`lib/tool_results/credential_redaction.py`).
//!
//! The known set is exact, never guessed: every credential-vault entry, every
//! provider key in `server_config.json`, and for an explicit owner every
//! model-routing credential key of that owner (through the model-routing
//! owner's decrypting read, the [`OwnerCredentialSource`] port). Each value is
//! matched raw and in its two URL-encoded forms.
//!
//! The index keeps no plaintext: per variant its length, a keyed BLAKE3
//! digest and its marker, plus a keyed SipHash of every `b`-character
//! substring. Every variant has at least `2b - 1` characters, so any
//! occurrence fully contains one of the text's aligned `b`-blocks; a block hit
//! names a candidate start that the digest verifies. Accepted matches are the
//! longest first, then the earliest, without overlap, replaced in one pass.
//! Lengths and offsets count Unicode scalar values, as Python `str` does.
//!
//! [`CredentialRedactor`] is the [`crate::WriteRedactor`] the client applies in
//! `command()`. It rebuilds an owner's set only when a change signal moves:
//! the vault store, its key or `server_config.json` (inode, size, mtime), and
//! that owner's model-routing stamp. Values are never logged.

use std::cell::Cell;
use std::collections::hash_map::RandomState;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::hash::BuildHasher;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};

/// A correctness floor, not a tuning knob: shorter strings are placeholders
/// ("EMPTY", "none", "sk-xxx") rather than credentials.
const MIN_CREDENTIAL_CHARS: usize = 8;

fn marker(label: &str) -> String {
    format!("[redacted credential: {label}]")
}

/// Python `str.isspace()`: Rust's `White_Space` plus the four ASCII
/// information separators Python also counts as whitespace.
pub(crate) fn python_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

fn quote_into(value: &str, plus: bool) -> String {
    let mut out = String::with_capacity(value.len());
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

/// `urllib.parse.quote(value, safe='')`.
pub fn quote(value: &str) -> String {
    quote_into(value, false)
}

/// `urllib.parse.quote_plus(value, safe='')`.
pub fn quote_plus(value: &str) -> String {
    quote_into(value, true)
}

/// The matched forms of one value: raw, URL-encoded, form-encoded.
pub fn credential_variants(value: &str) -> Vec<String> {
    let mut variants: Vec<String> = Vec::with_capacity(3);
    for variant in [value.to_owned(), quote(value), quote_plus(value)] {
        if !variants.contains(&variant) {
            variants.push(variant);
        }
    }
    variants
}

/// A text with scalar-value offsets, so slices follow Python `str` indexing.
struct Characters<'a> {
    text: &'a str,
    /// Byte offset of every scalar value, plus the text's end.
    offsets: Vec<usize>,
}

impl<'a> Characters<'a> {
    fn new(text: &'a str) -> Self {
        let mut offsets: Vec<usize> = text.char_indices().map(|(offset, _)| offset).collect();
        offsets.push(text.len());
        Self { text, offsets }
    }

    fn len(&self) -> usize {
        self.offsets.len() - 1
    }

    fn slice(&self, start: usize, end: usize) -> &'a str {
        &self.text[self.offsets[start]..self.offsets[end]]
    }
}

/// An immutable known-credential index holding fingerprints only.
#[derive(Clone, Debug, Default)]
pub struct KnownCredentialRedactor {
    block: usize,
    longest: usize,
    /// `(length, keyed digest, marker)` per variant.
    variants: Vec<(usize, [u8; 32], String)>,
    /// Hash of a `b`-substring → `(variant index, offset)`.
    grams: HashMap<u64, Vec<(usize, usize)>>,
    key: [u8; 32],
    hasher: RandomState,
}

fn random_key() -> [u8; 32] {
    let mut key = [0u8; 32];
    key[..16].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
    key[16..].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
    key
}

impl KnownCredentialRedactor {
    /// Index `(label, value)` pairs; the first label of a variant wins.
    pub fn build<I, L, V>(labelled_values: I) -> Self
    where
        I: IntoIterator<Item = (L, Option<V>)>,
        L: AsRef<str>,
        V: AsRef<str>,
    {
        let mut pending: Vec<(String, String)> = Vec::new();
        for (label, value) in labelled_values {
            let Some(value) = value else { continue };
            let value = value.as_ref().trim_matches(python_space);
            if value.chars().count() < MIN_CREDENTIAL_CHARS {
                continue;
            }
            let label_marker = marker(label.as_ref());
            for variant in credential_variants(value) {
                pending.push((variant, label_marker.clone()));
            }
        }
        let key = random_key();
        let hasher = RandomState::new();
        if pending.is_empty() {
            return Self {
                key,
                hasher,
                ..Self::default()
            };
        }
        let block = pending
            .iter()
            .map(|(variant, _)| variant.chars().count())
            .min()
            .unwrap_or(0)
            .div_ceil(2);
        let mut seen: HashSet<[u8; 32]> = HashSet::new();
        let mut variants = Vec::new();
        let mut grams: HashMap<u64, Vec<(usize, usize)>> = HashMap::new();
        for (variant, label_marker) in pending {
            let digest = *blake3::keyed_hash(&key, variant.as_bytes()).as_bytes();
            if !seen.insert(digest) {
                continue;
            }
            let index = variants.len();
            let characters = Characters::new(&variant);
            for offset in 0..=characters.len() - block {
                let gram = hasher.hash_one(characters.slice(offset, offset + block));
                grams.entry(gram).or_default().push((index, offset));
            }
            variants.push((characters.len(), digest, label_marker));
        }
        let longest = variants
            .iter()
            .map(|(length, _, _)| *length)
            .max()
            .unwrap_or(0);
        Self {
            block,
            longest,
            variants,
            grams,
            key,
            hasher,
        }
    }

    pub fn is_empty(&self) -> bool {
        self.variants.is_empty()
    }

    /// Non-overlapping `(start, end, variant index)` matches in text order.
    fn matches(&self, characters: &Characters<'_>) -> Vec<(usize, usize, usize)> {
        let block = self.block;
        let length = characters.len();
        if block == 0 || length < 2 * block - 1 {
            return Vec::new();
        }
        let mut found: HashSet<(usize, usize, usize)> = HashSet::new();
        let mut position = 0;
        while position * block + block <= length {
            let start_of_block = position * block;
            let gram = self
                .hasher
                .hash_one(characters.slice(start_of_block, start_of_block + block));
            if let Some(entries) = self.grams.get(&gram) {
                for (index, offset) in entries {
                    let (variant_length, digest, _) = &self.variants[*index];
                    let Some(start) = start_of_block.checked_sub(*offset) else {
                        continue;
                    };
                    if start + variant_length > length {
                        continue;
                    }
                    let candidate = characters.slice(start, start + variant_length);
                    if blake3::keyed_hash(&self.key, candidate.as_bytes()).as_bytes() == digest {
                        found.insert((start, *variant_length, *index));
                    }
                }
            }
            position += 1;
        }
        let mut found: Vec<(usize, usize, usize)> = found.into_iter().collect();
        found.sort_by(|left, right| right.1.cmp(&left.1).then(left.0.cmp(&right.0)));
        let mut accepted: Vec<(usize, usize, usize)> = Vec::new();
        for (start, variant_length, index) in found {
            let end = start + variant_length;
            if accepted
                .iter()
                .all(|(taken_start, taken_end, _)| end <= *taken_start || start >= *taken_end)
            {
                accepted.push((start, end, index));
            }
        }
        accepted.sort();
        accepted
    }

    pub fn contains(&self, text: &str) -> bool {
        !self.matches(&Characters::new(text)).is_empty()
    }

    pub fn redact(&self, text: &str) -> String {
        let characters = Characters::new(text);
        let matches = self.matches(&characters);
        if matches.is_empty() {
            return text.to_owned();
        }
        let mut out = String::with_capacity(text.len());
        let mut cursor = 0;
        for (start, end, index) in matches {
            out.push_str(characters.slice(cursor, start));
            out.push_str(&self.variants[index].2);
            cursor = end;
        }
        out.push_str(characters.slice(cursor, characters.len()));
        out
    }

    /// Redact a stream: `(safe output, new pending)`. Output never ends
    /// inside a possible occurrence: the last `longest - 1` characters, and
    /// any match crossing that cut, stay pending until more text or `last`.
    pub fn redact_stream(&self, pending: &str, chunk: &str, last: bool) -> (String, String) {
        let text = format!("{pending}{chunk}");
        if self.variants.is_empty() {
            return (text, String::new());
        }
        if last {
            return (self.redact(&text), String::new());
        }
        let characters = Characters::new(&text);
        let mut cut = characters.len().saturating_sub(self.longest - 1);
        for (start, end, _) in self.matches(&characters) {
            if start < cut && cut < end {
                cut = start;
            }
        }
        (
            self.redact(characters.slice(0, cut)),
            characters.slice(cut, characters.len()).to_owned(),
        )
    }

    /// Redact every string value inside JSON data; object keys are kept.
    pub fn redact_value(&self, value: &Value) -> Value {
        if self.variants.is_empty() {
            return value.clone();
        }
        match value {
            Value::String(text) => Value::String(self.redact(text)),
            Value::Array(items) => {
                Value::Array(items.iter().map(|item| self.redact_value(item)).collect())
            }
            Value::Object(object) => Value::Object(
                object
                    .iter()
                    .map(|(key, item)| (key.clone(), self.redact_value(item)))
                    .collect(),
            ),
            other => other.clone(),
        }
    }

    pub fn redact_payload(&self, payload: &Map<String, Value>) -> Map<String, Value> {
        match self.redact_value(&Value::Object(payload.clone())) {
            Value::Object(object) => object,
            _ => unreachable!("an object redacts to an object"),
        }
    }
}

/// `lib/credentials_vault.py::env_var_name`: `[^A-Z0-9]+` → `_`, trimmed.
pub fn env_var_name(entry: &str) -> String {
    let mut out = String::new();
    let mut in_run = false;
    for character in entry.to_uppercase().chars() {
        if character.is_ascii_uppercase() || character.is_ascii_digit() {
            out.push(character);
            in_run = false;
        } else if !in_run {
            out.push('_');
            in_run = true;
        }
    }
    out.trim_matches('_').to_owned()
}

/// The provider keys of one `server_config.json` (absent file: none).
pub fn server_config_values(path: &Path) -> std::io::Result<Vec<(String, Option<String>)>> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let config: Value = serde_json::from_slice(&bytes)?;
    let mut values = Vec::new();
    let Value::Object(config) = config else {
        return Ok(values);
    };
    let as_text = |value: Option<&Value>| value.and_then(Value::as_str).map(str::to_owned);
    if let Some(Value::Array(providers)) = config.get("providers") {
        for provider in providers {
            let Value::Object(provider) = provider else {
                continue;
            };
            // OAuth providers carry the 'oauth-managed' sentinel, not a key.
            if provider.get("oauth").is_some_and(python_truthy) {
                continue;
            }
            let name = [provider.get("id"), provider.get("name")]
                .into_iter()
                .flatten()
                .find(|value| python_truthy(value))
                .map(python_str)
                .unwrap_or_else(|| "?".to_owned());
            let label = format!("provider {name}");
            values.push((label.clone(), as_text(provider.get("api_key"))));
            if let Some(Value::Array(keys)) = provider.get("api_keys") {
                for key in keys {
                    values.push((label.clone(), as_text(Some(key))));
                }
            }
        }
    }
    if let Some(Value::Object(mt_provider)) = config.get("mt_provider") {
        values.push((
            "provider mt_provider".to_owned(),
            as_text(mt_provider.get("api_key")),
        ));
    }
    Ok(values)
}

pub(crate) fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(object) => !object.is_empty(),
    }
}

/// Python `str(value)` for the JSON scalars a provider label can hold.
pub(crate) fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        other => other.to_string(),
    }
}

/// Fernet (`cryptography.fernet`) verification and decryption, no TTL.
pub(crate) fn fernet_decrypt(key: &[u8], token: &str) -> Option<Vec<u8>> {
    use aes::cipher::{BlockDecrypt, KeyInit};
    use base64::engine::general_purpose::URL_SAFE;
    use base64::Engine;
    use hmac::Mac;

    let key = URL_SAFE.decode(key).ok().filter(|key| key.len() == 32)?;
    let data = URL_SAFE.decode(token.as_bytes()).ok()?;
    if data.len() < 1 + 8 + 16 + 16 + 32 || data[0] != 0x80 || (data.len() - 57) % 16 != 0 {
        return None;
    }
    let (signed, signature) = data.split_at(data.len() - 32);
    let mut mac = <hmac::Hmac<sha2::Sha256> as Mac>::new_from_slice(&key[..16]).ok()?;
    mac.update(signed);
    mac.verify_slice(signature).ok()?;
    let cipher = aes::Aes128::new_from_slice(&key[16..]).ok()?;
    let mut previous: [u8; 16] = signed[9..25].try_into().ok()?;
    let mut plain = Vec::with_capacity(signed.len() - 25);
    for chunk in signed[25..].chunks(16) {
        let mut block = aes::Block::clone_from_slice(chunk);
        cipher.decrypt_block(&mut block);
        plain.extend(
            block
                .iter()
                .zip(previous.iter())
                .map(|(byte, iv)| byte ^ iv),
        );
        previous.copy_from_slice(chunk);
    }
    let padding = *plain.last()? as usize;
    if padding == 0
        || padding > 16
        || plain[plain.len() - padding..]
            .iter()
            .any(|b| *b as usize != padding)
    {
        return None;
    }
    plain.truncate(plain.len() - padding);
    Some(plain)
}

/// Every decryptable vault entry as `(ENV_VAR_NAME, value)`, sorted by name.
///
/// A missing store or key file holds no entries (the Python owner mints the
/// key lazily on its first write). An entry that fails to decrypt is skipped,
/// as `get_entries_with_prefixes` skips it.
pub fn vault_values(store: &Path, key: &Path) -> std::io::Result<Vec<(String, Option<String>)>> {
    let bytes = match std::fs::read(store) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    // `read_json(..., default=None)`: an unreadable store holds no entries.
    let store: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    let Some(entries) = store.get("entries").and_then(Value::as_object) else {
        return Ok(Vec::new());
    };
    let key = match std::fs::read(key) {
        Ok(key) => key,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let key = key.trim_ascii();
    let sorted: BTreeMap<&String, &Value> = entries.iter().collect();
    let mut values = Vec::new();
    for (name, row) in sorted {
        let Some(token) = row
            .get("ct")
            .and_then(Value::as_str)
            .filter(|ct| !ct.is_empty())
        else {
            continue;
        };
        let Some(plain) = fernet_decrypt(key, token) else {
            continue;
        };
        let Ok(plain) = String::from_utf8(plain) else {
            continue;
        };
        values.push((env_var_name(name), Some(plain)));
    }
    Ok(values)
}

/// Where the owner-independent known set lives (`lib/config_dir.config_path`).
#[derive(Clone, Debug)]
pub struct CredentialFiles {
    pub config_dir: PathBuf,
    pub vault_store: PathBuf,
    pub vault_key: PathBuf,
    pub server_config: PathBuf,
}

impl CredentialFiles {
    /// The files under one configuration directory (`data/config`).
    pub fn in_config_dir(directory: &Path) -> Self {
        Self {
            config_dir: directory.to_path_buf(),
            vault_store: directory.join("credentials_vault.json"),
            vault_key: directory.join(".credentials_vault.key"),
            server_config: directory.join("server_config.json"),
        }
    }

    /// `lib/model_routing/change_notifications.model_routing_secrets_stamp_path`:
    /// replaced after every commit or secret write of that owner.
    pub fn model_routing_stamp(&self, owner_user_id: u64) -> PathBuf {
        self.config_dir
            .join(format!("model_routing_secrets.{owner_user_id}.stamp"))
    }

    fn identity(&self, owner_user_id: Option<u64>) -> Vec<Option<(u64, u64, i128)>> {
        let mut paths = vec![
            self.vault_store.clone(),
            self.vault_key.clone(),
            self.server_config.clone(),
        ];
        if let Some(owner) = owner_user_id {
            paths.push(self.model_routing_stamp(owner));
        }
        paths.iter().map(|path| stat_identity(path)).collect()
    }

    fn load(&self) -> std::io::Result<Vec<(String, Option<String>)>> {
        let mut labelled = vault_values(&self.vault_store, &self.vault_key)?;
        labelled.extend(server_config_values(&self.server_config)?);
        Ok(labelled)
    }
}

/// The model-routing owner's decrypting read of one owner's credential keys
/// (`lib/model_routing/known_secrets.owner_credential_keys`): every
/// `(credential_id, key)` of that owner, enabled or not. The production
/// implementation is [`crate::ModelRoutingKnownSecrets`]; callers keep only
/// the fingerprints built from it.
pub trait OwnerCredentialSource: Send + Sync {
    fn owner_credential_keys(&self, owner_user_id: u64) -> Result<Vec<(String, String)>, String>;
}

#[cfg(unix)]
fn stat_identity(path: &Path) -> Option<(u64, u64, i128)> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::metadata(path).ok()?;
    Some((
        metadata.ino(),
        metadata.size(),
        metadata.mtime() as i128 * 1_000_000_000 + metadata.mtime_nsec() as i128,
    ))
}

#[cfg(not(unix))]
fn stat_identity(path: &Path) -> Option<(u64, u64, i128)> {
    let metadata = std::fs::metadata(path).ok()?;
    let modified = metadata
        .modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?;
    Some((0, metadata.len(), modified.as_nanos() as i128))
}

type SourceIdentity = Vec<Option<(u64, u64, i128)>>;

thread_local! {
    /// A source read that itself writes through storage must not rebuild.
    static BUILDING: Cell<bool> = const { Cell::new(false) };
}

/// The redactor for the current vault, server_config and owner secrets.
pub struct CredentialRedactor {
    files: CredentialFiles,
    owner_source: Arc<dyn OwnerCredentialSource>,
    /// Per explicit owner (`None`: no owner): fingerprints only, never plaintext.
    cached: Mutex<HashMap<Option<u64>, CachedRedactor>>,
}

type CachedRedactor = (SourceIdentity, Arc<KnownCredentialRedactor>);

impl CredentialRedactor {
    /// The owner source is required: a redactor that could not know an
    /// owner's model-routing keys would store them in plaintext.
    pub fn new(files: CredentialFiles, owner_source: Arc<dyn OwnerCredentialSource>) -> Self {
        Self {
            files,
            owner_source,
            cached: Mutex::new(HashMap::new()),
        }
    }

    /// The current index; `owner_user_id` adds that owner's model-routing keys.
    pub fn current(&self, owner_user_id: Option<u64>) -> Arc<KnownCredentialRedactor> {
        let identity = self.files.identity(owner_user_id);
        if BUILDING.get() {
            let cached = self.cached.lock().unwrap_or_else(|p| p.into_inner());
            return cached
                .get(&owner_user_id)
                .map(|(_, redactor)| redactor.clone())
                .unwrap_or_default();
        }
        let mut cached = self.cached.lock().unwrap_or_else(|p| p.into_inner());
        if let Some((seen, redactor)) = cached.get(&owner_user_id) {
            if *seen == identity {
                return redactor.clone();
            }
        }
        let previous = cached
            .get(&owner_user_id)
            .map(|(_, redactor)| redactor.clone());
        BUILDING.set(true);
        let rebuilt = self.rebuild(owner_user_id);
        BUILDING.set(false);
        match rebuilt {
            Rebuilt::Complete(redactor) => {
                cached.insert(owner_user_id, (identity, redactor.clone()));
                redactor
            }
            // Keep the previous set rather than drop to none; the next
            // change of either file retries.
            Rebuilt::FilesFailed => {
                let redactor = previous.unwrap_or_default();
                cached.insert(owner_user_id, (identity, redactor.clone()));
                redactor
            }
            // Not recorded under this identity: the next call retries, so a
            // storage outage cannot leave the owner's keys unknown until the
            // next model-routing change.
            Rebuilt::OwnerFailed(without_owner) => previous
                .filter(|redactor| !redactor.is_empty())
                .unwrap_or(without_owner),
        }
    }

    fn rebuild(&self, owner_user_id: Option<u64>) -> Rebuilt {
        let Ok(mut labelled) = self.files.load() else {
            return Rebuilt::FilesFailed;
        };
        if let Some(owner) = owner_user_id {
            match self.owner_source.owner_credential_keys(owner) {
                Ok(keys) => labelled.extend(keys.into_iter().map(|(credential_id, key)| {
                    (format!("model-routing {credential_id}"), Some(key))
                })),
                Err(_) => {
                    return Rebuilt::OwnerFailed(Arc::new(KnownCredentialRedactor::build(labelled)))
                }
            }
        }
        Rebuilt::Complete(Arc::new(KnownCredentialRedactor::build(labelled)))
    }
}

enum Rebuilt {
    Complete(Arc<KnownCredentialRedactor>),
    FilesFailed,
    OwnerFailed(Arc<KnownCredentialRedactor>),
}

impl crate::WriteRedactor for CredentialRedactor {
    fn redact(&self, payload: &Map<String, Value>, owner_user_id: u64) -> Map<String, Value> {
        self.current(Some(owner_user_id)).redact_payload(payload)
    }
}

impl crate::WriteRedactor for KnownCredentialRedactor {
    fn redact(&self, payload: &Map<String, Value>, _owner_user_id: u64) -> Map<String, Value> {
        self.redact_payload(payload)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn redactor(values: &[(&str, &str)]) -> KnownCredentialRedactor {
        KnownCredentialRedactor::build(values.iter().map(|(label, value)| (*label, Some(*value))))
    }

    #[test]
    fn raw_and_url_encoded_variants_are_replaced_longest_first() {
        let known = redactor(&[("GITHUB_TOKEN", "ghp_secret value/1")]);
        let text = "a ghp_secret value/1 b ghp_secret%20value%2F1 c ghp_secret+value%2F1";
        assert_eq!(
            known.redact(text),
            "a [redacted credential: GITHUB_TOKEN] b [redacted credential: GITHUB_TOKEN] \
             c [redacted credential: GITHUB_TOKEN]"
        );
        assert!(known.contains("Bearer ghp_secret value/1"));
        assert!(!known.contains("ghp_secret"));
    }

    #[test]
    fn short_and_missing_values_are_not_credentials() {
        assert!(redactor(&[("A", "  short ")]).is_empty());
        assert!(KnownCredentialRedactor::build([("A", None::<&str>)]).is_empty());
    }

    #[test]
    fn json_values_redact_strings_but_not_keys() {
        let known = redactor(&[("K", "sk-1234567890")]);
        let value = json!({"sk-1234567890": ["x sk-1234567890", 1, null, {"n": "sk-1234567890"}]});
        assert_eq!(
            known.redact_value(&value),
            json!({"sk-1234567890": ["x [redacted credential: K]", 1, null,
                                     {"n": "[redacted credential: K]"}]})
        );
    }

    #[test]
    fn non_ascii_text_counts_characters_like_python() {
        let known = redactor(&[("K", "密钥密钥密钥密钥")]);
        assert_eq!(
            known.redact("前密钥密钥密钥密钥后"),
            "前[redacted credential: K]后"
        );
        assert_eq!(quote("密"), "%E5%AF%86");
    }

    #[test]
    fn overlapping_matches_keep_the_longest_then_the_earliest() {
        let known = redactor(&[("LONG", "abcdefghijkl"), ("SHORT", "ijklmnop")]);
        assert_eq!(
            known.redact("abcdefghijklmnop ijklmnop"),
            "[redacted credential: LONG]mnop [redacted credential: SHORT]"
        );
    }

    #[test]
    fn the_index_keeps_no_plaintext() {
        let known = redactor(&[("K", "sk-plaintext-value")]);
        assert!(!format!("{known:?}").contains("plaintext"));
    }

    #[test]
    fn a_stream_never_emits_a_split_occurrence() {
        let known = redactor(&[("K", "sk-1234567890")]);
        let (first, pending) = known.redact_stream("", "head sk-12345", false);
        assert!(!first.contains("sk-1"));
        let (second, pending) = known.redact_stream(&pending, "67890 tail", false);
        let (last, rest) = known.redact_stream(&pending, "", true);
        assert_eq!(
            format!("{first}{second}{last}"),
            "head [redacted credential: K] tail"
        );
        assert!(rest.is_empty());
    }

    struct FlakyOwner(std::sync::atomic::AtomicBool);

    impl OwnerCredentialSource for FlakyOwner {
        fn owner_credential_keys(&self, _owner: u64) -> Result<Vec<(String, String)>, String> {
            if self.0.swap(true, std::sync::atomic::Ordering::SeqCst) {
                Ok(vec![("cred-1".to_owned(), "sk-owner-key-123".to_owned())])
            } else {
                Err("storage outage".to_owned())
            }
        }
    }

    #[test]
    fn an_owner_source_failure_is_retried_on_the_next_call() {
        let directory = std::env::temp_dir().join(format!("redaction-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&directory).unwrap();
        let redactor = CredentialRedactor::new(
            CredentialFiles::in_config_dir(&directory),
            Arc::new(FlakyOwner(std::sync::atomic::AtomicBool::new(false))),
        );
        assert_eq!(
            redactor.current(Some(7)).redact("sk-owner-key-123"),
            "sk-owner-key-123"
        );
        assert_eq!(
            redactor.current(Some(7)).redact("sk-owner-key-123"),
            "[redacted credential: model-routing cred-1]"
        );
        assert_eq!(
            redactor.current(None).redact("sk-owner-key-123"),
            "sk-owner-key-123"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn env_var_names_match_the_vault() {
        assert_eq!(env_var_name("github_token"), "GITHUB_TOKEN");
        assert_eq!(env_var_name("npm-token.v2"), "NPM_TOKEN_V2");
        assert_eq!(env_var_name("_x__y_"), "X_Y");
    }
}
