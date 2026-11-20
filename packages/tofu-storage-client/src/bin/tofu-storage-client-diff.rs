//! Differential driver: one JSON command per stdin line, one JSON outcome
//! per stdout line. `tests/storage/test_storage_v2_rust_client_differential.py`
//! replays the same corpus through `lib/storage/v2_client.py` and compares.
//!
//! Commands: `open`, `query`, `command`, `maintenance`, `query_many`,
//! `health`, `metrics`, `worker_watch`, `conversation_watch`,
//! `checkpoint_available`, `set_redactor`, `redact`, `contract`, `close`, and
//! the production model-routing source: `install_credential_redaction`,
//! `redact_installed`.
//!
//! `open` with a `config_dir` installs the recorded redactor at once, as
//! Python's `command()` always redacts; the client refuses payload commands
//! with none installed.

use std::io::{BufRead, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use tofu_storage_client::{
    ClientConfig, CredentialFiles, CredentialRedactor, OwnerCredentialSource, StorageError,
    StorageV2Client,
};

/// A recorded owner read for the hook and cache semantics: the `owner_keys`
/// pairs a command carries, or a failure when it carries none. The real read
/// (`ModelRoutingKnownSecrets`) runs under `install_credential_redaction`.
struct RecordedOwnerKeys(Value);

impl OwnerCredentialSource for RecordedOwnerKeys {
    fn owner_credential_keys(&self, _owner_user_id: u64) -> Result<Vec<(String, String)>, String> {
        let Some(pairs) = self.0.as_array() else {
            return Err("owner keys unavailable".to_owned());
        };
        Ok(pairs
            .iter()
            .map(|pair| {
                (
                    pair[0].as_str().unwrap_or_default().to_owned(),
                    pair[1].as_str().unwrap_or_default().to_owned(),
                )
            })
            .collect())
    }
}

fn redactor(command: &Value) -> CredentialRedactor {
    let files = CredentialFiles::in_config_dir(Path::new(text(command, "config_dir")));
    let keys = command.get("owner_keys").cloned().unwrap_or(Value::Null);
    CredentialRedactor::new(files, Arc::new(RecordedOwnerKeys(keys)))
}

fn outcome(result: Result<Value, StorageError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "value": value}),
        Err(error) => json!({"ok": false, "error": error.to_payload()}),
    }
}

fn seconds(command: &Value, field: &str) -> Option<Duration> {
    command
        .get(field)
        .and_then(Value::as_f64)
        .map(Duration::from_secs_f64)
}

fn payload(command: &Value) -> Option<Map<String, Value>> {
    command.get("payload").and_then(Value::as_object).cloned()
}

fn text<'a>(command: &'a Value, field: &str) -> &'a str {
    command
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or_default()
}

fn deadline_unix_ms(command: &Value) -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_millis() as i64)
        .unwrap_or(0);
    (now + command["deadline_ms_from_now"].as_i64().unwrap_or(0)).max(0) as u64
}

fn open(command: &Value) -> Result<Arc<StorageV2Client>, StorageError> {
    let mut config = ClientConfig::new(
        text(command, "host"),
        command["port"].as_u64().unwrap_or(0) as u16,
        text(command, "secret").as_bytes(),
        command["owner_id"].as_u64().unwrap_or(0),
        command["tenant_id"].as_u64(),
    );
    if let Some(timeout) = seconds(command, "timeout") {
        config.timeout = timeout;
    }
    if let Some(attempts) = command["read_attempts"].as_u64() {
        config.read_attempts = attempts as u32;
    }
    let client = Arc::new(StorageV2Client::new(config)?);
    if command.get("config_dir").is_some() {
        client.set_write_redactor(Arc::new(redactor(command)));
    }
    Ok(client)
}

fn main() {
    let mut client: Option<Arc<StorageV2Client>> = None;
    let mut installed: Option<Arc<CredentialRedactor>> = None;
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let command: Value = serde_json::from_str(&line).expect("one JSON command per line");
        let call = text(&command, "call").to_owned();
        let result = match (call.as_str(), client.as_ref()) {
            ("open", _) => match open(&command) {
                Ok(opened) => {
                    client = Some(opened);
                    json!({"ok": true, "value": null})
                }
                Err(error) => outcome(Err(error)),
            },
            ("contract", _) => {
                let contract = tofu_storage_client::contract::contract();
                json!({"ok": true, "value": {
                    "error_codes": contract.error_codes.iter().collect::<Vec<_>>(),
                    "operations": contract.operations.keys().collect::<Vec<_>>(),
                }})
            }
            ("redact", _) => {
                let known = redactor(&command).current(command["owner_id"].as_u64());
                json!({"ok": true, "value": known.redact_value(&command["value"])})
            }
            (_, None) => json!({"ok": false, "error": "no client is open"}),
            ("set_redactor", Some(storage)) => {
                storage.set_write_redactor(Arc::new(redactor(&command)));
                json!({"ok": true, "value": null})
            }
            ("install_credential_redaction", Some(storage)) => {
                installed = Some(
                    storage.install_credential_redaction(Path::new(text(&command, "config_dir"))),
                );
                json!({"ok": true, "value": null})
            }
            ("redact_installed", Some(_)) => match &installed {
                Some(redactor) => {
                    let known = redactor.current(command["owner_id"].as_u64());
                    json!({"ok": true, "value": known.redact_value(&command["value"])})
                }
                None => json!({"ok": false, "error": "no credential redaction installed"}),
            },
            ("query", Some(storage)) => outcome(storage.query(
                text(&command, "operation"),
                payload(&command).as_ref(),
                seconds(&command, "deadline"),
                None,
            )),
            ("command", Some(storage)) => outcome(storage.command(
                text(&command, "operation"),
                payload(&command).as_ref(),
                command.get("command_id").and_then(Value::as_str),
                seconds(&command, "deadline"),
            )),
            ("maintenance", Some(storage)) => outcome(storage.maintenance(
                text(&command, "operation"),
                payload(&command).as_ref(),
                seconds(&command, "deadline"),
            )),
            ("query_many", Some(storage)) => {
                let requests: Vec<(String, Option<Map<String, Value>>)> = command["requests"]
                    .as_array()
                    .map(|requests| {
                        requests
                            .iter()
                            .map(|request| {
                                (
                                    request[0].as_str().unwrap_or_default().to_owned(),
                                    request[1].as_object().cloned(),
                                )
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                let outcomes: Vec<Value> = storage
                    .query_many(&requests, seconds(&command, "deadline"))
                    .into_iter()
                    .map(outcome)
                    .collect();
                json!({"ok": true, "value": outcomes})
            }
            ("health", Some(storage)) => {
                json!({"ok": true, "value": storage.health(seconds(&command, "deadline"))})
            }
            ("metrics", Some(storage)) => {
                json!({"ok": true, "value": storage.metrics(seconds(&command, "deadline"))})
            }
            ("worker_watch", Some(storage)) => outcome(storage.worker_watch(
                command["cursor"].clone(),
                deadline_unix_ms(&command),
                None,
            )),
            ("conversation_watch", Some(storage)) => outcome(storage.conversation_watch(
                command["cursor"].clone(),
                deadline_unix_ms(&command),
                None,
            )),
            ("checkpoint_available", Some(storage)) => outcome(
                storage
                    .native_checkpoint_available(command["user_id"].as_u64().unwrap_or(0))
                    .map(Value::from),
            ),
            ("close", Some(storage)) => {
                storage.close();
                json!({"ok": true, "value": null})
            }
            (other, Some(_)) => json!({"ok": false, "error": format!("unknown call {other}")}),
        };
        serde_json::to_writer(&mut stdout, &result).expect("stdout");
        stdout.write_all(b"\n").expect("stdout");
        stdout.flush().expect("stdout");
    }
}
