//! `tofu-channel-stdio-link [--close-timeout SECONDS] -- <adapter argv...>`
//!
//! Puts the Rust core side ([`StdioLink`]) behind Python's `AdapterHost`
//! (`lib/channels/protocol/rust_stdio.py`). It owns the adapter child (same
//! environment and directory as this process) and speaks JSON-RPC lines with
//! Python on its own stdin/stdout:
//!
//! - Python's requests are forwarded to the adapter and answered with
//!   Python's ID; errors keep code, message and data.
//! - The adapter's `events` requests and `health` notifications are forwarded
//!   to Python (requests with this link's own IDs; the answer goes back to the
//!   adapter).
//! - Link controls (methods starting `$/`): Python sends the notification
//!   `$/close {"timeout": seconds}` before closing this link's stdin; the link
//!   notifies `$/status {"returncode", "stderrTail"}` when the adapter exits.
//! - When Python closes stdin, the link closes the adapter with the timeout of
//!   `$/close` (else `--close-timeout`; with neither, it waits for the adapter
//!   to exit on its own, as an orphaned adapter would), notifies
//!   `$/exit {"returncode", "stderrTail"}` and exits 0.

use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tofu_channel_stdio::link::{CoreHost, StdioLink};
use tofu_channel_stdio::peer::{HandlerError, HandlerResult, Peer, PeerClient};

struct ForwardToPython(PeerClient);

impl CoreHost for ForwardToPython {
    fn events(&self, batch: Map<String, Value>) -> HandlerResult {
        self.0
            .request("events", Value::Object(batch))
            .map_err(HandlerError::Protocol)
    }

    fn health(&self, health: Map<String, Value>) -> Result<(), HandlerError> {
        self.0
            .notify("health", Value::Object(health))
            .map_err(HandlerError::Protocol)
    }
}

fn status(link: &StdioLink) -> Value {
    json!({"returncode": link.returncode(), "stderrTail": link.stderr_tail()})
}

fn parse_args(args: &[String]) -> Result<(Option<f64>, Vec<String>), String> {
    let mut close_timeout = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--" => {
                let argv = args[index + 1..].to_vec();
                if argv.is_empty() {
                    return Err("no adapter argv after --".into());
                }
                return Ok((close_timeout, argv));
            }
            "--close-timeout" => {
                let value = args.get(index + 1).ok_or("--close-timeout needs seconds")?;
                close_timeout =
                    Some(seconds(value).ok_or(format!("bad --close-timeout {value:?}"))?);
                index += 2;
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Err("usage: tofu-channel-stdio-link [--close-timeout SECONDS] -- <adapter argv...>".into())
}

fn seconds(text: &str) -> Option<f64> {
    text.parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value >= 0.0)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (default_timeout, argv) = match parse_args(&args) {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("tofu-channel-stdio-link: {message}");
            return ExitCode::from(2);
        }
    };
    let link = match StdioLink::spawn(&argv, None, None) {
        Ok(link) => Arc::new(link),
        Err(error) => {
            eprintln!("tofu-channel-stdio-link: cannot start the adapter: {error}");
            return ExitCode::from(2);
        }
    };

    let close_timeout = Arc::new(Mutex::new(default_timeout));
    let requested_timeout = close_timeout.clone();
    let forwarding = link.clone();
    let python = Peer::new(
        std::io::stdin(),
        std::io::stdout(),
        Box::new(move |method, params| {
            forwarding
                .request(method, Value::Object(params))
                .map_err(HandlerError::Protocol)
        }),
        "channel-stdio-link",
    )
    .with_reader_hook(Box::new(move |message| {
        if message.get("method").and_then(Value::as_str) != Some("$/close") {
            return false;
        }
        let timeout = message
            .get("params")
            .and_then(|params| params.get("timeout"))
            .and_then(Value::as_f64);
        if let Some(timeout) = timeout.filter(|value| value.is_finite() && *value >= 0.0) {
            *requested_timeout
                .lock()
                .unwrap_or_else(|poison| poison.into_inner()) = Some(timeout);
        }
        true
    }));
    link.bind(Arc::new(ForwardToPython(python.client())));
    python.start();

    let watched = link.clone();
    let reporter = python.client();
    let watcher = std::thread::Builder::new()
        .name("channel-stdio-link-exit".into())
        .spawn(move || {
            watched.wait(None);
            let _ = reporter.notify("$/status", status(&watched));
        })
        .expect("spawning the exit watcher");

    python.wait_closed(None);
    let timeout = *close_timeout
        .lock()
        .unwrap_or_else(|poison| poison.into_inner());
    match timeout.and_then(|seconds| Duration::try_from_secs_f64(seconds).ok()) {
        Some(timeout) => {
            link.close(timeout);
        }
        None => {
            link.close_stdin();
            link.wait(None);
            link.close(Duration::ZERO);
        }
    }
    let _ = watcher.join();
    let _ = python.notify("$/exit", status(&link));
    ExitCode::SUCCESS
}
