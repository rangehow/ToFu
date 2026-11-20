//! Differential tests against the Python oracle (lib/channels/protocol/stdio.py):
//! the reference adapter driven through Python's `StdioLink`, through the Rust
//! `StdioLink`, and through Python's `RustStdioLink` over the link binary must
//! print the same bytes; `pyjson` must reprint JSON exactly like Python.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use tofu_channel_stdio::link::{CoreHost, StdioLink};
use tofu_channel_stdio::peer::{HandlerError, HandlerResult};
use tofu_channel_stdio::pyjson;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn python() -> PathBuf {
    let venv = repo_root().join(".venv/bin/python3");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python3")
    }
}

fn oracle_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/differential")
}

/// The adapter environment: this process's, with the repository first on PYTHONPATH.
fn adapter_env() -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = std::env::vars()
        .filter(|(key, _)| key != "PYTHONPATH")
        .collect();
    env.push(("PYTHONPATH".into(), repo_root().display().to_string()));
    env
}

fn oracle(mode: &str, input: Option<&[u8]>) -> String {
    let mut command = Command::new(python());
    command
        .arg(oracle_dir().join("oracle.py"))
        .arg(mode)
        .arg(oracle_dir().join("sequence.json"))
        .current_dir(repo_root())
        .env_clear()
        .envs(adapter_env())
        .env(
            "TOFU_CHANNEL_STDIO_LINK_BIN",
            env!("CARGO_BIN_EXE_tofu-channel-stdio-link"),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().expect("python3 runs");
    let mut stdin = child.stdin.take().unwrap();
    let input = input.unwrap_or_default().to_vec();
    let feeder = thread::spawn(move || {
        let _ = stdin.write_all(&input);
    });
    let output = child.wait_with_output().unwrap();
    feeder.join().unwrap();
    assert!(
        output.status.success(),
        "oracle {mode} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[derive(Default)]
struct Host {
    calls: Mutex<Vec<Value>>,
}

impl CoreHost for Host {
    fn events(&self, batch: Map<String, Value>) -> HandlerResult {
        let events: Vec<Value> = batch["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|event| json!([event["eventId"], event["parts"][0]["text"]]))
            .collect();
        self.calls.lock().unwrap().push(json!(["events", events]));
        Ok(json!({}))
    }

    fn health(&self, health: Map<String, Value>) -> Result<(), HandlerError> {
        self.calls
            .lock()
            .unwrap()
            .push(json!(["health", health["state"], health["code"]]));
        Ok(())
    }
}

/// `oracle.py drive` in Rust over the Rust `StdioLink`.
fn drive_rust_link() -> String {
    let sequence: Vec<Value> =
        serde_json::from_str(&std::fs::read_to_string(oracle_dir().join("sequence.json")).unwrap())
            .unwrap();
    let argv = vec![
        python().display().to_string(),
        "-m".into(),
        "tests.channels.conformance.reference".into(),
    ];
    let env = adapter_env();
    let link = StdioLink::spawn(&argv, Some(&env), Some(&repo_root())).unwrap();
    let host = Arc::new(Host::default());
    link.bind(host.clone());
    let mut out = String::new();
    let mut emit = |record: Value| {
        out.push_str(&pyjson::dumps(&record));
        out.push('\n');
    };
    for (index, step) in sequence.iter().enumerate() {
        if step.get("wait_idle").is_some() {
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                let idle = link.request("test/idle", json!({})).unwrap()["idle"] == json!(true);
                let delivered = host
                    .calls
                    .lock()
                    .unwrap()
                    .iter()
                    .any(|call| call[0] == "events");
                if idle && delivered {
                    break;
                }
                assert!(
                    Instant::now() < deadline,
                    "the reference adapter never went idle"
                );
                thread::sleep(Duration::from_millis(10));
            }
            continue;
        }
        match link.request(step["method"].as_str().unwrap(), step["params"].clone()) {
            Err(error) => emit(json!({"step": index, "error": {
                "code": error.code, "message": error.message, "data": error.data}})),
            Ok(result) if step.get("volatile").is_some() => {
                let mut keys: Vec<&String> = result.as_object().unwrap().keys().collect();
                keys.sort();
                emit(json!({"step": index, "keys": keys}));
            }
            Ok(result) => emit(json!({"step": index, "result": result})),
        }
    }
    emit(json!({"host": *host.calls.lock().unwrap()}));
    let closed = link.close(Duration::from_secs(5));
    emit(json!({"closed": closed, "returncode": link.returncode()}));
    out
}

#[test]
fn the_reference_adapter_answers_alike_through_python_and_rust_links() {
    let through_python = oracle("python", None);
    assert_eq!(through_python.lines().count(), 27, "{through_python}");
    assert!(through_python.contains("\"step\": 10, \"error\": {\"code\": -32601, \"message\": \"The adapter has no method 'test/nope'\""));
    assert!(through_python.contains("\"history_failed\""));
    let through_rust = drive_rust_link();
    assert_eq!(through_rust, through_python);
    let through_rust_binary = oracle("rust-link", None);
    assert_eq!(through_rust_binary, through_python);
}

#[test]
fn pyjson_reprints_json_like_python() {
    let lines: Vec<&[u8]> = vec![
        b"{\"a\": 1, \"b\": [1.0, 2.50, -0, -0.0, 1e5, 1E-7, 123456789012345678901234567890, 1e400, -1e400]}",
        b"{\"s\": \"\\u00e9\\u2028\\ud83d\\ude00 \\\" \\\\ \\/ \\b\\f\\n\\r\\t\\u0001\\u007f\", \"z\": {}, \"y\": []}",
        "\"é✓ 😀\"".as_bytes(),
        b"{\"dup\": 1, \"other\": 2, \"dup\": 3}",
        b"[0.1, 0.2, 0.30000000000000004, 1e16, 1e15, 1e-4, 1e-5, 5e-324, 1.7976931348623157e308]",
        b"  true  ",
        b"null",
        b"not json",
        b"{\"a\": }",
        b"\xff\"x\"",
        b"",
        b"[1,]",
    ];
    let input: Vec<u8> = lines
        .iter()
        .flat_map(|line| line.iter().copied().chain(*b"\n"))
        .collect();
    let expected = oracle("json", Some(&input));
    let mut got = String::new();
    for line in &lines {
        match pyjson::loads(line) {
            Some(value) => got.push_str(&pyjson::dumps(&value)),
            None => got.push_str("ERROR"),
        }
        got.push('\n');
    }
    assert_eq!(got, expected);
}
