//! `tofu-core-rs` command line.
//!
//! `byte-identity`: read JSON lines `{protocol, model, max_tokens,
//! temperature, tools, messages, base_url, api_key}` on stdin and write one
//! line per request: `{url, headers, payload_b64}` or `{error}`.
//!
//! `sessions CONFIG.json`: run the configured Turns against a harness stub and
//! a TofuDB engine; print one JSON report (per-session stub correlation keys,
//! per-round stage profile, process RSS and CPU).

use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;

use base64::Engine as _;
use serde_json::{json, Value};

use tofu_core_rs::canonical::{build_body, BodySpec};
use tofu_core_rs::round::{monotonic_ns, run_session, EventMode, SessionConfig};
use tofu_core_rs::store::ConnectionInfo;
use tofu_core_rs::wire::{prepare, Protocol};

fn byte_identity() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let request: Value = serde_json::from_str(&line)?;
        let protocol = Protocol::parse(request["protocol"].as_str().unwrap_or(""))
            .ok_or("unknown protocol")?;
        let messages = request["messages"].as_array().cloned().unwrap_or_default();
        let tools = request["tools"].as_array().cloned();
        let outcome = build_body(
            &messages,
            &BodySpec {
                model: request["model"].as_str().unwrap_or(""),
                max_tokens: request["max_tokens"].as_u64().unwrap_or(4096),
                temperature: request["temperature"].as_f64(),
                tools: tools.as_deref(),
            },
        )
        .and_then(|body| {
            prepare(
                body,
                protocol,
                request["base_url"].as_str().unwrap_or(""),
                request["api_key"].as_str().unwrap_or(""),
            )
        });
        let answer = match outcome {
            Ok(sealed) => json!({
                "url": sealed.url,
                "headers": sealed.headers,
                "payload_b64": base64::engine::general_purpose::STANDARD.encode(&sealed.payload),
                "tools": sealed.tool_names,
            }),
            Err(error) => json!({"error": error.to_string()}),
        };
        serde_json::to_writer(&mut stdout, &answer)?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()?;
    Ok(())
}

fn process_stats() -> Value {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let field = |name: &str| {
        status
            .lines()
            .find(|line| line.starts_with(name))
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|kb| kb.parse::<u64>().ok())
            .unwrap_or(0)
    };
    // SAFETY: getrusage fills one rusage struct we own.
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) };
    let cpu = |tv: libc::timeval| tv.tv_sec as f64 + tv.tv_usec as f64 / 1e6;
    json!({
        "rss_kb": field("VmRSS:"), "peak_rss_kb": field("VmHWM:"), "threads": field("Threads:"),
        "user_cpu_s": cpu(usage.ru_utime), "system_cpu_s": cpu(usage.ru_stime),
    })
}

fn sessions(path: &str) -> Result<(), Box<dyn std::error::Error>> {
    let launched_ns = monotonic_ns();
    let config: Value = serde_json::from_slice(&std::fs::read(path)?)?;
    let storage = ConnectionInfo::from_file(
        PathBuf::from(
            config["connection_file"]
                .as_str()
                .ok_or("connection_file")?,
        )
        .as_path(),
    )?;
    let protocol =
        Protocol::parse(config["protocol"].as_str().unwrap_or("anthropic")).ok_or("protocol")?;
    let events = match config["events"].as_str().unwrap_or("parity") {
        "lean" => EventMode::Lean,
        _ => EventMode::Parity,
    };
    let base = SessionConfig {
        protocol,
        base_url: config["base_url"].as_str().unwrap_or("").to_owned(),
        api_key: config["api_key"]
            .as_str()
            .unwrap_or("sk-harness")
            .to_owned(),
        model: config["model"]
            .as_str()
            .unwrap_or("harness-perf-stub")
            .to_owned(),
        max_tokens: config["max_tokens"].as_u64().unwrap_or(16384),
        temperature: config["temperature"].as_f64(),
        system: config["system"].as_str().unwrap_or("").to_owned(),
        tools: Arc::new(config["tools"].as_array().cloned().unwrap_or_default()),
        workspace: PathBuf::from(config["workspace"].as_str().unwrap_or(".")),
        storage,
        schema_id: config["schema_id"].as_u64().unwrap_or(0) as u32,
        conversation_id: String::new(),
        prompt: String::new(),
        events,
        max_rounds: 0,
    };
    // Each worker runs its sessions in order; workers run concurrently.
    let workers = config["workers"].as_array().cloned().unwrap_or_default();
    let started_ns = monotonic_ns();
    let handles: Vec<_> = workers
        .into_iter()
        .map(|sessions| {
            let base = base.clone();
            thread::spawn(move || {
                sessions
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .into_iter()
                    .map(|session| {
                        let mut config = base.clone();
                        config.conversation_id = session["conversation_id"].as_str().unwrap_or("").to_owned();
                        config.prompt = session["prompt"].as_str().unwrap_or("").to_owned();
                        config.max_rounds = session["max_rounds"].as_u64().unwrap_or(1) as usize;
                        match run_session(&config) {
                            Ok(report) => json!({
                                "session": session["session"], "status": report.status, "requests": report.requests,
                                "started_ns": report.started_ns, "first_request_ns": report.first_request_ns,
                                "settled_ns": report.settled_ns, "setup_us": report.setup_us,
                                "conversation_id": config.conversation_id, "turn_id": report.turn_id,
                                "attempt_id": report.attempt_id, "events_committed": report.events_committed,
                                "rounds": report.rounds.iter().map(|r| r.to_json()).collect::<Vec<_>>(),
                            }),
                            Err(error) => json!({"session": session["session"], "status": "failed", "error": error.to_string()}),
                        }
                    })
                    .collect::<Vec<_>>()
            })
        })
        .collect();
    let results: Vec<Value> = handles
        .into_iter()
        .flat_map(|handle| handle.join().unwrap_or_default())
        .collect();
    let report = json!({
        "launched_ns": launched_ns, "started_ns": started_ns, "finished_ns": monotonic_ns(),
        "process": process_stats(), "sessions": results,
    });
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let outcome = match args.get(1).map(String::as_str) {
        Some("byte-identity") => byte_identity(),
        Some("sessions") if args.len() > 2 => sessions(&args[2]),
        _ => Err("usage: tofu-core-rs byte-identity | sessions CONFIG.json".into()),
    };
    if let Err(error) = outcome {
        eprintln!("tofu-core-rs: {error}");
        std::process::exit(1);
    }
}
