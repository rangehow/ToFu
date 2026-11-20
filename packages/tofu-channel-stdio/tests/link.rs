//! `StdioLink` against real child processes: spawn, adapter-to-core dispatch,
//! stderr diagnostics, the end of the process, and close escalation.

use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use tofu_channel_stdio::link::{CoreHost, StdioLink};
use tofu_channel_stdio::peer::{HandlerError, HandlerResult, ProtocolError, CLOSED};

fn sh(script: &str) -> Vec<String> {
    vec!["/bin/sh".into(), "-c".into(), script.into()]
}

#[derive(Default)]
struct RecordingHost {
    calls: Mutex<Vec<(String, Value)>>,
}

impl CoreHost for RecordingHost {
    fn events(&self, batch: Map<String, Value>) -> HandlerResult {
        self.calls
            .lock()
            .unwrap()
            .push(("events".into(), Value::Object(batch)));
        Ok(json!({"accepted": true}))
    }

    fn health(&self, health: Map<String, Value>) -> Result<(), HandlerError> {
        self.calls
            .lock()
            .unwrap()
            .push(("health".into(), Value::Object(health)));
        Ok(())
    }
}

#[test]
fn a_pending_request_fails_when_the_adapter_process_ends() {
    let link = StdioLink::spawn(&sh("read line"), None, None).unwrap();
    assert_eq!(
        link.request("send", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
    assert!(link.close(Duration::from_secs(5)));
    assert_eq!(link.returncode(), Some(0));
    assert_eq!(
        link.request("send", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
}

/// An adapter script that waits for one line (see `go`), writes `lines`, then
/// echoes the `expected` answers it reads on one stderr line (the link keeps
/// only the last one).
fn answering(lines: &[&str], expected: usize) -> Vec<String> {
    let mut script = String::from("read -r go\n");
    for line in lines {
        script.push_str(&format!("printf '%s\\n' '{line}'\n"));
    }
    let reads: Vec<String> = (0..expected)
        .map(|index| format!("read -r a{index}"))
        .collect();
    let echoed: Vec<String> = (0..expected).map(|index| format!("$a{index}")).collect();
    script.push_str(&reads.join("; "));
    script.push_str(&format!(
        "\nprintf '%s\\n' \"{}\" >&2\nread -r done\n",
        echoed.join("|")
    ));
    sh(&script)
}

/// Let the script start once the test bound its host (the request stays
/// pending until the binding closes).
fn go(link: &Arc<StdioLink>) {
    let link = link.clone();
    thread::spawn(move || link.request("go", json!({})));
}

fn answers(link: &StdioLink) -> Vec<Value> {
    let deadline = Instant::now() + Duration::from_secs(10);
    while link.stderr_tail().is_empty() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    link.stderr_tail()
        .split('|')
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn an_unbound_link_refuses_with_not_initialized() {
    let link = StdioLink::spawn(
        &answering(
            &[r#"{"jsonrpc": "2.0", "id": 7, "method": "events", "params": {}}"#],
            1,
        ),
        None,
        None,
    )
    .map(Arc::new)
    .unwrap();
    go(&link);
    assert_eq!(
        answers(&link),
        [
            json!({"jsonrpc": "2.0", "id": 7, "error": {"code": -32002, "message": "No core endpoint is bound"}})
        ]
    );
    assert!(link.close(Duration::from_secs(5)));
}

#[test]
fn the_core_answers_adapter_to_core_methods_and_refuses_the_rest() {
    let link = StdioLink::spawn(
        &answering(
            &[
                "not json",
                r#"{"jsonrpc": "2.0", "id": 1, "method": "send", "params": {}}"#,
                r#"{"jsonrpc": "2.0", "id": 2, "method": "nope", "params": {}}"#,
                r#"{"jsonrpc": "2.0", "method": "health", "params": {"state": "connected"}}"#,
                r#"{"jsonrpc": "2.0", "id": 3, "method": "health", "params": {"state": "x"}}"#,
                r#"{"jsonrpc": "2.0", "id": 4, "method": "events", "params": {"events": []}}"#,
            ],
            5,
        ),
        None,
        None,
    )
    .map(Arc::new)
    .unwrap();
    let host = Arc::new(RecordingHost::default());
    link.bind(host.clone());
    go(&link);
    // The health notification gets no answer.
    assert_eq!(
        answers(&link),
        [
            json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "A line is not JSON"}}),
            json!({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "The core has no method 'send'"}}),
            json!({"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "The core has no method 'nope'"}}),
            json!({"jsonrpc": "2.0", "id": 3, "result": null}),
            json!({"jsonrpc": "2.0", "id": 4, "result": {"accepted": true}}),
        ]
    );
    assert!(link.close(Duration::from_secs(5)));
    let calls = host.calls.lock().unwrap();
    assert_eq!(
        *calls,
        [
            ("health".to_string(), json!({"state": "connected"})),
            ("health".to_string(), json!({"state": "x"})),
            ("events".to_string(), json!({"events": []})),
        ]
    );
}

#[test]
fn the_environment_is_replaced_and_the_directory_set() {
    let dir = std::env::temp_dir();
    let env = [("ONLY".to_string(), "this".to_string())];
    let link = StdioLink::spawn(
        &sh(r#"printf '%s %s %s\n' "$ONLY" "${HOME:-nohome}" "$(pwd -P)" >&2; read x"#),
        Some(&env),
        Some(&dir),
    )
    .unwrap();
    assert!(link.close(Duration::from_secs(5)));
    let canonical = std::fs::canonicalize(&dir).unwrap();
    assert_eq!(
        link.stderr_tail(),
        format!("this nohome {}", canonical.display())
    );
}

#[test]
fn stderr_keeps_the_last_non_empty_stripped_line() {
    let link = StdioLink::spawn(
        &sh(r#"echo first >&2; printf '  last \t\n' >&2; printf '\n   \n' >&2; printf 'bad \377\n' >&2; read x"#),
        None,
        None,
    )
    .unwrap();
    assert!(link.close(Duration::from_secs(5)));
    assert_eq!(link.stderr_tail(), "bad \u{fffd}");
}

#[test]
fn returncode_does_not_block_and_reports_the_exit_status() {
    let link = StdioLink::spawn(&sh("read x; exit 3"), None, None).unwrap();
    assert_eq!(link.returncode(), None);
    assert!(link.close(Duration::from_secs(5)));
    assert_eq!(link.returncode(), Some(3));
}

#[test]
fn an_adapter_stopping_on_stdin_eof_is_not_signalled() {
    let link = StdioLink::spawn(
        &sh("trap 'exit 9' TERM; while read x; do :; done; exit 0"),
        None,
        None,
    )
    .unwrap();
    let started = Instant::now();
    assert!(link.close(Duration::from_secs(10)));
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(link.returncode(), Some(0));
}

#[test]
fn sigterm_ends_an_adapter_that_ignores_stdin_eof() {
    let link = StdioLink::spawn(&sh("while :; do sleep 1; done"), None, None).unwrap();
    assert!(link.close(Duration::from_millis(300)));
    assert_eq!(link.returncode(), Some(-libc::SIGTERM));
}

fn gone(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(_) => true,
        Ok(stat) => stat
            .rsplit(')')
            .next()
            .unwrap_or("")
            .trim_start()
            .starts_with('Z'),
    }
}

#[test]
fn close_escalates_to_sigkill_and_reaps_the_whole_group() {
    // Leader and a grandchild ignore SIGTERM and stdin EOF.
    let link = StdioLink::spawn(
        &sh(r#"trap "" TERM; /bin/sh -c 'trap "" TERM; while :; do sleep 1; done' & echo $! >&2; while :; do sleep 1; done"#),
        None,
        None,
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while link.stderr_tail().is_empty() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let grandchild: i32 = link.stderr_tail().parse().unwrap();
    let leader = link.pid();
    let started = Instant::now();
    assert!(link.close(Duration::from_millis(300)));
    assert!(started.elapsed() >= Duration::from_millis(600));
    assert_eq!(link.returncode(), Some(-libc::SIGKILL));
    assert!(gone(leader));
    let deadline = Instant::now() + Duration::from_secs(10);
    while !gone(grandchild) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(20));
    }
    assert!(gone(grandchild), "the group's other member was killed too");
}

#[test]
fn spawning_a_missing_program_fails() {
    assert!(StdioLink::spawn(
        &["/nonexistent/adapter".to_string()],
        None,
        Some(Path::new("/"))
    )
    .is_err());
    assert!(StdioLink::spawn(&[], None, None).is_err());
}
