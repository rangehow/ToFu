//! Storage engine certification records (`serverctl_pkg/engine_certification.py`).
//!
//! A record certifies one engine source identity against one storage contract
//! identity: its exact binary (and embedded library) bytes passed every
//! required check of the engine gate. `scripts/engine_certify.py` is the only
//! writer; reading never changes a record or a deployment. Contract:
//! docs/NATIVE_STORAGE_LIFECYCLE.md, "Engine certification gate".

use crate::child::{self, Spec};
use crate::envpolicy::process_environment;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

pub const RECORD_FORMAT: &str = "tofu.engine-certification.v1";
pub const RECORD_FILE: &str = "certification.json";
pub const BINARY_NAME: &str = "tofu-db";
pub const LIBRARY_NAME: &str = "libtofu_db.so";
pub const REQUIRED_CHECKS: [&str; 8] = [
    "build",
    "certify",
    "certify-no-online-background",
    "cargo-lib",
    "cargo-fastpath",
    "ff-native",
    "load",
    "rollback",
];
const BUILD_INFO_SECONDS: f64 = 10.0;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CertificationRecord {
    pub directory: PathBuf,
    pub source_sha256: String,
    pub binary: PathBuf,
    pub binary_sha256: String,
    pub library_sha256: Option<String>,
    pub storage_contract_sha256: String,
    pub commit: String,
    pub certified_utc: String,
    pub operator: String,
}

impl CertificationRecord {
    pub fn summary(&self) -> String {
        let commit: String = self.commit.chars().take(10).collect();
        format!(
            "certified engine {} (commit {}, certified {} by {})",
            &self.source_sha256[..12],
            if commit.is_empty() {
                "unknown".into()
            } else {
                commit
            },
            if self.certified_utc.is_empty() {
                "at an unknown time"
            } else {
                &self.certified_utc
            },
            if self.operator.is_empty() {
                "an unknown operator"
            } else {
                &self.operator
            },
        )
    }
}

pub fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn certifications_root(root: &Path) -> PathBuf {
    root.join("packages/tofu-db/target/engine-certifications")
}

pub fn record_directory(root: &Path, source: &str, contract: &str) -> Result<PathBuf, String> {
    if !is_sha256(source) || !is_sha256(contract) {
        return Err(
            "Engine source and storage contract identities are lowercase SHA-256 digests.".into(),
        );
    }
    Ok(certifications_root(root).join(source).join(contract))
}

pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

/// `native_engine.build_identity`: the engine's own `--build-info` source identity.
pub fn build_source_identity(binary: &Path) -> Result<String, String> {
    let environment = process_environment();
    let output = child::run(Spec {
        argv: vec![binary.to_string_lossy().into_owned(), "--build-info".into()],
        cwd: binary.parent().unwrap_or(Path::new("/")),
        environment: &environment,
        timeout: BUILD_INFO_SECONDS,
        progress: None,
        termination_grace: 5.0,
        failure_subject: "Storage engine preparation",
        stdin: None,
    })?;
    let info: Value = serde_json::from_str(&output.stdout).map_err(|_| {
        "Storage engine has no verifiable source identity; an engine update is required.".to_owned()
    })?;
    let source = info
        .get("source_sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    if info.get("format").and_then(Value::as_str) != Some("tofu-db-build.v1") || !is_sha256(source)
    {
        return Err(
            "Storage engine has no verifiable source identity; an engine update is required."
                .into(),
        );
    }
    Ok(source.to_owned())
}

/// Verify one record against its own bytes (`read_record`); `Err` says why it does not certify.
pub fn read_record(directory: &Path) -> Result<CertificationRecord, String> {
    let file = directory.join(RECORD_FILE);
    let text = match fs::read_to_string(&file) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(format!("it has no {RECORD_FILE}"))
        }
        Err(error) => return Err(format!("its {RECORD_FILE} is unreadable: {error}")),
    };
    let document: Value = serde_json::from_str(&text)
        .map_err(|error| format!("its {RECORD_FILE} is unreadable: {error}"))?;
    let Some(document) = document
        .as_object()
        .filter(|d| d.get("format").and_then(Value::as_str) == Some(RECORD_FORMAT))
    else {
        return Err(format!("it is not a {RECORD_FORMAT} record"));
    };
    let text_of = |key: &str| {
        document
            .get(key)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned()
    };
    let (source, contract) = (text_of("source_sha256"), text_of("storage_contract_sha256"));
    let named = (
        directory
            .parent()
            .and_then(Path::file_name)
            .map(|n| n.to_string_lossy().into_owned()),
        directory
            .file_name()
            .map(|n| n.to_string_lossy().into_owned()),
    );
    if !is_sha256(&source)
        || !is_sha256(&contract)
        || named != (Some(source.clone()), Some(contract.clone()))
    {
        return Err("its source and storage contract identities do not name its directory".into());
    }
    if document.get("verdict").and_then(Value::as_str) != Some("pass") {
        let verdict = match document.get("verdict") {
            Some(Value::String(text)) => format!("'{text}'"),
            None | Some(Value::Null) => "None".into(),
            Some(other) => other.to_string(),
        };
        return Err(format!("its verdict is {verdict}"));
    }
    let verdicts: std::collections::BTreeMap<String, String> = document
        .get("checks")
        .and_then(Value::as_array)
        .map(|checks| {
            checks
                .iter()
                .filter_map(Value::as_object)
                .map(|check| {
                    (
                        text_of_value(check.get("id")),
                        text_of_value(check.get("verdict")),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    let failed: Vec<&str> = REQUIRED_CHECKS
        .iter()
        .copied()
        .filter(|check| verdicts.get(*check).map(String::as_str) != Some("pass"))
        .collect();
    if !failed.is_empty() {
        return Err(format!(
            "required checks did not pass: {}",
            failed.join(", ")
        ));
    }
    let binary = directory.join(BINARY_NAME);
    let library = directory.join(LIBRARY_NAME);
    let library_sha256 = document
        .get("library_sha256")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let read_failure = |error: std::io::Error| format!("its engine could not be read: {error}");
    if sha256_file(&binary).map_err(read_failure)? != text_of("binary_sha256") {
        return Err("its engine binary does not match the recorded SHA-256".into());
    }
    match &library_sha256 {
        Some(expected) => {
            if !library.is_file() || &sha256_file(&library).map_err(read_failure)? != expected {
                return Err("its embedded library does not match the recorded SHA-256".into());
            }
        }
        None if library.exists() => {
            return Err("it holds an embedded library the record does not declare".into())
        }
        None => {}
    }
    match build_source_identity(&binary) {
        Ok(identity) if identity == source => {}
        Ok(_) => return Err("its engine binary reports another source identity".into()),
        Err(error) => return Err(format!("its engine could not identify itself: {error}")),
    }
    let operator = match document.get("operator") {
        Some(Value::Object(operator)) => text_of_value(operator.get("user")),
        other => text_of_value(other),
    };
    Ok(CertificationRecord {
        directory: directory.to_path_buf(),
        source_sha256: source,
        binary,
        binary_sha256: text_of("binary_sha256"),
        library_sha256,
        storage_contract_sha256: contract,
        commit: text_of_value(document.get("commit")),
        certified_utc: text_of_value(document.get("certified_utc")),
        operator,
    })
}

fn text_of_value(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(false)) => String::new(),
        Some(other) => other.to_string(),
    }
}

/// `passing_record`: the verified record, or why there is none.
pub fn passing_record(
    root: &Path,
    source: &str,
    contract: &str,
) -> Result<CertificationRecord, String> {
    let directory = record_directory(root, source, contract)?;
    if !directory.is_dir() {
        return Err(format!(
            "no certification record exists for it with storage contract {}",
            &contract[..12]
        ));
    }
    read_record(&directory).map_err(|error| {
        format!(
            "its certification record {} does not verify: {error}",
            directory.display()
        )
    })
}

/// `record_directories`, optionally of one storage contract.
pub fn record_directories(root: &Path, contract: Option<&str>) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let Ok(sources) = fs::read_dir(certifications_root(root)) else {
        return found;
    };
    for source in sources.flatten() {
        if !is_sha256(&source.file_name().to_string_lossy()) {
            continue;
        }
        let Ok(contracts) = fs::read_dir(source.path()) else {
            continue;
        };
        for entry in contracts.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if is_sha256(&name)
                && contract.is_none_or(|wanted| wanted == name)
                && entry.path().is_dir()
            {
                found.push(entry.path());
            }
        }
    }
    found.sort();
    found
}

/// `record_for_binary`: the verified record holding exactly these engine bytes.
pub fn record_for_binary(
    root: &Path,
    binary_sha256: &str,
    contract: &str,
) -> Option<CertificationRecord> {
    record_directories(root, Some(contract))
        .into_iter()
        .find_map(|directory| {
            let text = fs::read_to_string(directory.join(RECORD_FILE)).ok()?;
            let document: Value = serde_json::from_str(&text).ok()?;
            if document.get("binary_sha256").and_then(Value::as_str) != Some(binary_sha256) {
                return None;
            }
            read_record(&directory).ok()
        })
}

/// `UncertifiedEngineOverride.from_payload(...).payload()`: an operator's one-shot
/// decision to deploy HEAD's uncertified engine, validated before any preparation.
pub fn parse_engine_override(payload: &Value) -> Result<Value, String> {
    let parsed;
    let payload = match payload {
        Value::String(text) => {
            parsed = serde_json::from_str::<Value>(text).map_err(|error| error.to_string())?;
            &parsed
        }
        other => other,
    };
    let Some(map) = payload.as_object() else {
        return Err("expected an object with decisionId, operator and reason".into());
    };
    let mut fields = Vec::new();
    for (name, key) in [
        ("decision_id", "decisionId"),
        ("operator", "operator"),
        ("reason", "reason"),
    ] {
        match map.get(key).and_then(Value::as_str) {
            Some(value) if !value.trim().is_empty() && !value.contains('\n') => fields.push(value),
            _ => return Err(format!("{name} must be a non-empty single line")),
        }
    }
    if fields[0].len() != 32
        || !fields[0]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("decisionId must be 32 lowercase hexadecimal characters".into());
    }
    Ok(
        serde_json::json!({"decisionId": fields[0].trim(), "operator": fields[1].trim(), "reason": fields[2].trim()}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn engine_override_payloads_are_validated_like_python() {
        let good = json!({"decisionId": "0123456789abcdef0123456789abcdef", "operator": " owner ", "reason": "hotfix"});
        assert_eq!(parse_engine_override(&good).unwrap()["operator"], "owner");
        assert_eq!(
            parse_engine_override(&json!([])).err().unwrap(),
            "expected an object with decisionId, operator and reason"
        );
        assert_eq!(
            parse_engine_override(&json!({"decisionId": "x", "operator": "o", "reason": "r\ns"}))
                .err()
                .unwrap(),
            "reason must be a non-empty single line"
        );
        assert_eq!(
            parse_engine_override(&json!({"decisionId": "ABC", "operator": "o", "reason": "r"}))
                .err()
                .unwrap(),
            "decisionId must be 32 lowercase hexadecimal characters"
        );
    }

    const SOURCE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const CONTRACT: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

    fn fixture(name: &str, reported_source: &str) -> (PathBuf, PathBuf) {
        let root =
            std::env::temp_dir().join(format!("tofu-manager-cert-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let directory = record_directory(&root, SOURCE, CONTRACT).unwrap();
        fs::create_dir_all(&directory).unwrap();
        let binary = directory.join(BINARY_NAME);
        fs::write(
            &binary,
            format!("#!/bin/sh\necho '{{\"format\":\"tofu-db-build.v1\",\"source_sha256\":\"{reported_source}\"}}'\n"),
        )
        .unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
        let checks: Vec<Value> = REQUIRED_CHECKS
            .iter()
            .map(|id| json!({"id": id, "verdict": "pass"}))
            .collect();
        let record = json!({
            "format": RECORD_FORMAT, "source_sha256": SOURCE, "storage_contract_sha256": CONTRACT,
            "verdict": "pass", "checks": checks, "binary_sha256": sha256_file(&binary).unwrap(),
            "commit": "0123456789abcdef", "certified_utc": "2026-11-18T00:00:00Z", "operator": {"user": "owner"},
        });
        fs::write(directory.join(RECORD_FILE), record.to_string()).unwrap();
        (root, directory)
    }

    #[test]
    fn a_complete_record_certifies_its_engine() {
        let (root, _) = fixture("pass", SOURCE);
        let record = passing_record(&root, SOURCE, CONTRACT).unwrap();
        assert_eq!(record.operator, "owner");
        assert!(record
            .summary()
            .starts_with("certified engine aaaaaaaaaaaa (commit 0123456789"));
        assert!(record_for_binary(&root, &record.binary_sha256, CONTRACT).is_some());
        assert!(record_for_binary(&root, &"c".repeat(64), CONTRACT).is_none());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn a_record_whose_engine_reports_another_source_does_not_certify() {
        let (root, _) = fixture("identity", &"c".repeat(64));
        let error = passing_record(&root, SOURCE, CONTRACT).err().unwrap();
        assert!(
            error.ends_with("its engine binary reports another source identity"),
            "{error}"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn altered_bytes_missing_checks_and_undeclared_libraries_do_not_certify() {
        let (root, directory) = fixture("defects", SOURCE);
        fs::write(directory.join(LIBRARY_NAME), b"lib").unwrap();
        assert!(read_record(&directory)
            .err()
            .unwrap()
            .contains("embedded library the record does not declare"));
        fs::remove_file(directory.join(LIBRARY_NAME)).unwrap();
        let mut record: Value =
            serde_json::from_str(&fs::read_to_string(directory.join(RECORD_FILE)).unwrap())
                .unwrap();
        record["checks"] = json!([{"id": "build", "verdict": "pass"}]);
        fs::write(directory.join(RECORD_FILE), record.to_string()).unwrap();
        assert!(read_record(&directory)
            .err()
            .unwrap()
            .starts_with("required checks did not pass: certify, "));
        record["binary_sha256"] = json!("0".repeat(64));
        record["checks"] = json!(REQUIRED_CHECKS
            .iter()
            .map(|id| json!({"id": id, "verdict": "pass"}))
            .collect::<Vec<_>>());
        fs::write(directory.join(RECORD_FILE), record.to_string()).unwrap();
        assert_eq!(
            read_record(&directory).err().unwrap(),
            "its engine binary does not match the recorded SHA-256"
        );
        let missing = passing_record(&root, SOURCE, &"d".repeat(64))
            .err()
            .unwrap();
        assert_eq!(
            missing,
            "no certification record exists for it with storage contract dddddddddddd"
        );
        let _ = fs::remove_dir_all(root);
    }
}
