//! The agent round: stored history → canonical body → protocol translation →
//! seal → provider stream → tool dispatch → durable events, on a real Turn
//! attempt created and claimed through TofuDB's own storage operations.
//!
//! Session lifecycle (the operations the Python worker issues for one Turn):
//! `turn.create_pair`, `turn.attempt.dispatch_worker`, `worker_job.claim_next`,
//! `turn.attempt.start`, per-round `turn.event.record_batch`, a terminal
//! `turn.event.record` and `worker_job.complete`. Each round also writes the
//! Request Inspector raw archive (`raw_archive.put`, zlib level 1) from a
//! writer thread, as `lib/raw_archive.py` does.
//!
//! `EventMode::Parity` repeats the per-round durable work of the Python round
//! measured on 2026-11-18 (five boundary reads in one exchange, the
//! per-request `turn.attempt.start`, fifteen projection events in three
//! commits); `EventMode::Lean` keeps only the durability contract (tool intent
//! before its effect, the tool receipt before the next request: four events in
//! two commits). The two modes separate the cost of the language from the
//! cost of the amount of durable work.

use std::io::Read;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use flate2::write::ZlibEncoder;
use flate2::Compression;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::canonical::{build_body, BodySpec};
use crate::provider::{decode_sse, Connection, ExchangeTiming};
use crate::store::{Client, ConnectionInfo};
use crate::tools::dispatch;
use crate::wire::{prepare, Protocol};

pub fn random_bytes(count: usize) -> Vec<u8> {
    let mut bytes = vec![0u8; count];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .expect("/dev/urandom is readable");
    bytes
}

pub fn random_hex(bytes: usize) -> String {
    random_bytes(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// `CLOCK_MONOTONIC` in nanoseconds: the clock the stub stamps its records
/// with (`time.monotonic_ns`), comparable across processes.
pub fn monotonic_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime writes one timespec we own.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventMode {
    Parity,
    Lean,
}

#[derive(Clone)]
pub struct SessionConfig {
    pub protocol: Protocol,
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub max_tokens: u64,
    pub temperature: Option<f64>,
    pub system: String,
    pub tools: Arc<Vec<Value>>,
    pub workspace: PathBuf,
    pub storage: ConnectionInfo,
    pub schema_id: u32,
    pub conversation_id: String,
    pub prompt: String,
    pub events: EventMode,
    pub max_rounds: usize,
}

/// Microseconds per stage of one round (provider response finished → next
/// request sent), the attribution of the measured round overhead.
#[derive(Debug, Default, Clone)]
pub struct RoundProfile {
    pub round: usize,
    pub decode_us: u64,
    pub intent_commit_us: u64,
    pub tool_us: u64,
    pub receipt_commit_us: u64,
    pub boundary_reads_us: u64,
    pub build_body_us: u64,
    pub translate_seal_us: u64,
    pub attempt_start_us: u64,
    pub send_us: u64,
    pub request_bytes: usize,
    pub tool: String,
}

impl RoundProfile {
    pub fn to_json(&self) -> Value {
        json!({
            "round": self.round, "tool": self.tool, "request_bytes": self.request_bytes,
            "decode_us": self.decode_us, "intent_commit_us": self.intent_commit_us,
            "tool_us": self.tool_us, "receipt_commit_us": self.receipt_commit_us,
            "boundary_reads_us": self.boundary_reads_us, "build_body_us": self.build_body_us,
            "translate_seal_us": self.translate_seal_us, "attempt_start_us": self.attempt_start_us,
            "send_us": self.send_us,
        })
    }
}

#[derive(Debug, Default)]
pub struct SessionReport {
    pub requests: usize,
    pub rounds: Vec<RoundProfile>,
    pub started_ns: u64,
    pub first_request_ns: u64,
    pub settled_ns: u64,
    pub setup_us: u64,
    pub status: String,
    pub attempt_id: String,
    pub turn_id: String,
    pub events_committed: u64,
}

struct Attempt {
    attempt_id: String,
    task_id: String,
    turn_id: String,
    worker_id: String,
    fencing_token: u64,
    revision: u64,
    sequence: u64,
    segments: usize,
    tool_rounds: usize,
}

/// One raw-archive record for the writer thread.
struct ArchiveJob {
    payload: Value,
    command_id: String,
}

fn zlib1(bytes: &[u8]) -> Vec<u8> {
    use std::io::Write;
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::new(1));
    encoder.write_all(bytes).expect("in-memory zlib");
    encoder.finish().expect("in-memory zlib")
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// The setup lock serializes dispatch and claim: `worker_job.claim_next`
/// hands out any queued job, so concurrent sessions in one process would
/// otherwise claim each other's attempts.
static CLAIM_LOCK: Mutex<()> = Mutex::new(());

fn worker_identity() -> String {
    format!("tofu-core-rs/v1:{}:{}", std::process::id(), random_hex(8))
}

impl Attempt {
    fn claim(&self) -> Value {
        json!({"worker_id": self.worker_id, "fencing_token": self.fencing_token})
    }
}

fn setup(
    store: &mut Client,
    config: &SessionConfig,
    user: u64,
) -> Result<Attempt, Box<dyn std::error::Error>> {
    let command_id = format!("rs-{}", random_hex(16));
    let now = now_ms();
    let created = store.call(
        "turn.create_pair",
        &json!({
            "conversation_id": config.conversation_id, "user_id": user, "command_id": command_id,
            "input_projection": {"content": config.prompt, "timestamp": now, "segments": [
                {"type": "text", "text": config.prompt, "blockId": "text:terminal", "deliverable": true, "terminal": true}]},
            "config": {"chatMode": "chat", "model": config.model, "projectPath": config.workspace.display().to_string(),
                       "_conversationExecutionIdentity": {"version": 1, "principal": {"kind": "user", "subject_id": "local",
                       "owner_user_id": user, "tenant_id": null, "scopes": []}, "attended": true}},
            "lane_id": "main", "parent_turn_id": null, "kind": "reply", "output_actor": "assistant", "run_id": "",
            "input_actor": "human", "input_kind": "input", "require_parent_is_lane_tail": false,
            "require_parent_not_stopped": false, "require_lane_idle": false, "reject_if_human_queued": false,
            "conversation_defaults": {"allowCreate": true, "title": "tofu-core-rs", "settings": {}},
            "dispatch_mode": "conversation_executor",
            "input_presentation_id": format!("{command_id}:input"), "output_presentation_id": format!("{command_id}:output"),
            "queue_binding": {}
        }),
        Some(&command_id),
    )?;
    let attempt_id = created["attempt"]["attemptId"]
        .as_str()
        .ok_or("create_pair returned no attempt")?
        .to_owned();
    let turn_id = created["attempt"]["turnId"]
        .as_str()
        .unwrap_or("")
        .to_owned();
    let worker_id = worker_identity();
    let job = {
        let _guard = CLAIM_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        store.call(
            "turn.attempt.dispatch_worker",
            &json!({"attempt_id": attempt_id, "user_id": user, "principal": {"kind": "user", "subject_id": "local",
                    "owner_user_id": user, "tenant_id": null, "scopes": []}, "priority": 100, "now_ms": now_ms()}),
            Some(&format!("turn-worker-dispatch:{attempt_id}")),
        )?;
        store.call(
            "worker_job.claim_next",
            &json!({"worker_id": worker_id, "task_kinds": ["conversation-attempt"], "now_ms": now_ms(),
                    "lease_ms": 60000, "retain_claim": true, "user_id": user}),
            Some(&format!("rs-claim:{}", random_hex(16))),
        )?
    };
    let claimed = job["payload"]["attemptId"].as_str().unwrap_or("");
    if claimed != attempt_id {
        return Err(
            format!("claimed job {claimed:?} is not this session's attempt {attempt_id}").into(),
        );
    }
    let task_id = job["taskId"].as_str().unwrap_or("").to_owned();
    let fencing_token = job["fencingToken"].as_u64().unwrap_or(1);
    let mut attempt = Attempt {
        attempt_id,
        task_id,
        turn_id,
        worker_id,
        fencing_token,
        revision: 0,
        sequence: 0,
        segments: 0,
        tool_rounds: 0,
    };
    store.call(
        "turn.attempt.start",
        &json!({"attempt_id": attempt.attempt_id, "task_id": attempt.task_id, "user_id": user, "worker_claim": attempt.claim()}),
        Some(&format!("turn-start:{}:{}", attempt.attempt_id, attempt.task_id)),
    )?;
    let turn = store.call(
        "turn.get",
        &json!({"conversation_id": config.conversation_id, "turn_id": attempt.turn_id, "user_id": user}),
        None,
    )?;
    attempt.revision = turn["projectionRevision"].as_u64().unwrap_or(0);
    Ok(attempt)
}

/// One projection event: operations applied to the Turn projection plus the
/// task event the replay journal keeps.
struct Event {
    kind: &'static str,
    operations: Value,
    task_event: Value,
}

fn entry(attempt: &mut Attempt, config: &SessionConfig, user: u64, event: Event) -> Value {
    let base = attempt.revision;
    attempt.revision += 1;
    let sequence = attempt.sequence;
    attempt.sequence += 1;
    let mut task_event = event.task_event;
    task_event["taskId"] = json!(attempt.task_id);
    task_event["attemptId"] = json!(attempt.attempt_id);
    task_event["conversationId"] = json!(config.conversation_id);
    task_event["turnId"] = json!(attempt.turn_id);
    task_event["seq"] = json!(sequence);
    json!({
        "attempt_id": attempt.attempt_id, "user_id": user, "task_id": attempt.task_id, "terminal": false,
        "status": "running", "settlement": {}, "error": {}, "event_type": "projection_updated",
        "event_payload": {"phase": null, "updateKind": event.kind}, "now": now_ms(),
        "worker_claim": attempt.claim(),
        "projection_patch": {"version": 1, "baseRevision": base, "targetRevision": base + 1, "operations": event.operations},
        "projection_segments_stable": true,
        "task_event": {"task_id": attempt.task_id, "sequence": sequence, "event": task_event},
    })
}

fn commit(
    store: &mut Client,
    attempt: &Attempt,
    user: u64,
    entries: Vec<Value>,
) -> Result<(), Box<dyn std::error::Error>> {
    let first = entries
        .first()
        .and_then(|e| e["projection_patch"]["baseRevision"].as_u64())
        .unwrap_or(0);
    let command_id = format!(
        "turn-event-batch:{}:{}:revision:{first}",
        attempt.attempt_id,
        now_ms()
    );
    store.call(
        "turn.event.record_batch",
        &json!({"user_id": user, "entries": entries}),
        Some(&command_id),
    )?;
    Ok(())
}

fn phase(detail: &str) -> Event {
    Event {
        kind: "phase",
        operations: json!([{"op": "set", "path": ["phaseDetail"], "value": detail}]),
        task_event: json!({"type": "phase", "phase": "working", "detail": detail}),
    }
}

/// Run one Turn to its terminal settlement.
pub fn run_session(config: &SessionConfig) -> Result<SessionReport, Box<dyn std::error::Error>> {
    let mut report = SessionReport {
        started_ns: monotonic_ns(),
        ..Default::default()
    };
    let started = Instant::now();
    let user = config.storage.owner_id;
    let mut store = Client::connect(&config.storage, config.schema_id)?;
    let mut attempt = setup(&mut store, config, user)?;
    let (archive_tx, archive_rx) = mpsc::channel::<ArchiveJob>();
    let archive_info = config.storage.clone();
    let schema_id = config.schema_id;
    let archive_writer = thread::Builder::new()
        .name("raw-archive-writer".into())
        .spawn(move || {
            let mut client = Client::connect(&archive_info, schema_id).ok();
            for job in archive_rx {
                if let Some(client) = client.as_mut() {
                    if let Err(error) =
                        client.call("raw_archive.put", &job.payload, Some(&job.command_id))
                    {
                        eprintln!("raw archive commit failed: {error}");
                    }
                }
            }
        })?;
    report.setup_us = started.elapsed().as_micros() as u64;

    let mut history: Vec<Value> = vec![
        json!({"role": "system", "content": config.system}),
        json!({"role": "user", "content": config.prompt}),
    ];
    let mut connection = Connection::new(&config.base_url)?;
    let mut profile = RoundProfile::default();
    let mut final_text = String::new();
    for round in 0..=config.max_rounds {
        profile.round = round;
        if config.events == EventMode::Parity && round > 0 {
            let t = Instant::now();
            store.query_many(&[
                ("conversation.get", json!({"conv_id": config.conversation_id, "user_id": user, "derive_messages": false})),
                ("turn.get", json!({"conversation_id": config.conversation_id, "turn_id": attempt.turn_id, "user_id": user, "include_projection": false})),
                ("turn.attempt.get", json!({"attempt_id": attempt.attempt_id, "user_id": user, "include_execution_config": true})),
                ("record.get", json!({"namespace": "turn_review_v1", "key": sha256_hex(attempt.turn_id.as_bytes()), "user_id": user})),
                ("record.get", json!({"namespace": "working_facts_v1", "key": sha256_hex(config.conversation_id.as_bytes()), "user_id": user})),
            ])?;
            profile.boundary_reads_us = t.elapsed().as_micros() as u64;
        }
        let t = Instant::now();
        let body = build_body(
            &history,
            &BodySpec {
                model: &config.model,
                max_tokens: config.max_tokens,
                temperature: config.temperature,
                tools: Some(&config.tools),
            },
        )?;
        profile.build_body_us = t.elapsed().as_micros() as u64;
        let t = Instant::now();
        let sealed = prepare(body, config.protocol, &config.base_url, &config.api_key)?;
        profile.translate_seal_us = t.elapsed().as_micros() as u64;
        profile.request_bytes = sealed.payload.len();
        if config.events == EventMode::Parity {
            let t = Instant::now();
            store.call(
                "turn.attempt.start",
                &json!({"attempt_id": attempt.attempt_id, "task_id": attempt.task_id, "user_id": user, "worker_claim": attempt.claim()}),
                Some(&format!("turn-start:{}:{}", attempt.attempt_id, attempt.task_id)),
            )?;
            profile.attempt_start_us = t.elapsed().as_micros() as u64;
        }
        let trace_id = random_hex(16);
        let mut timing = ExchangeTiming::default();
        if round == 0 {
            report.first_request_ns = monotonic_ns();
        }
        let response = connection.post(&sealed, &trace_id, &mut timing)?;
        report.requests += 1;
        profile.send_us = timing.send_us;
        if round > 0 {
            report.rounds.push(std::mem::take(&mut profile));
            profile.round = round;
        }
        let t = Instant::now();
        let completion = decode_sse(config.protocol, &response)?;
        profile.decode_us = t.elapsed().as_micros() as u64;

        let request_blob = zlib1(&sealed.payload);
        let response_blob = zlib1(&response);
        let request_sha = sha256_hex(&sealed.payload);
        let response_sha = sha256_hex(&response);
        let combined = sha256_hex(&[hex_bytes(&request_sha), hex_bytes(&response_sha)].concat());
        let archive_id = format!("raw-{}", random_hex(16));
        let _ = archive_tx.send(ArchiveJob {
            command_id: format!("raw-archive:{archive_id}"),
            payload: json!({
                "archive_id": archive_id, "user_id": user, "conversation_id": config.conversation_id,
                "turn_id": attempt.turn_id, "attempt_id": attempt.attempt_id, "task_id": attempt.task_id,
                "round_num": round + 1, "transport_attempt": 0,
                "request_blob_b64": base64::engine::general_purpose::STANDARD.encode(&request_blob),
                "response_blob_b64": base64::engine::general_purpose::STANDARD.encode(&response_blob),
                "request_bytes": sealed.payload.len(), "response_bytes": response.len(),
                "request_sha256": request_sha, "response_sha256": response_sha,
                "integrity": "complete", "truncation_reason": "",
                "summary": {"text": "Provider request/response", "model": config.model, "traceId": trace_id,
                            "statusCode": 200, "combinedSha256": combined},
                "budget_bytes": 268435456u64, "min_free_bytes": 268435456u64, "available_free_bytes": 1u64 << 40,
            }),
        });

        let Some(call) = completion.tool_calls.first().cloned() else {
            final_text = completion.text.clone();
            break;
        };
        profile.tool = call.name.clone();
        let tool_round = attempt.tool_rounds;
        let segment = attempt.segments;
        let text_segment = json!({"type": "text", "blockId": format!("text:attempt-{}:llm-{round}", attempt.attempt_id),
                                  "text": completion.text, "llmRound": round, "deliverable": false});
        let tool_segment = json!({"type": "tool_use", "blockId": format!("tool:{}", call.id), "id": call.id,
                                  "name": call.name, "input": call.arguments, "llmRound": round,
                                  "result": {"content": null, "status": "searching"}, "roundNum": round + 1});
        let mut intent = vec![
            Event {
                kind: "model_request_complete",
                operations: json!([{"op": "append", "path": ["segments"], "value": [text_segment, tool_segment]},
                                   {"op": "append", "path": ["toolRounds"], "value": [{"roundNum": round + 1, "toolCallId": call.id,
                                    "toolName": call.name, "toolArgs": call.arguments, "status": "searching"}]}]),
                task_event: json!({"type": "model_request_complete", "status": "succeeded", "finishReason": completion.finish_reason,
                                   "roundNum": round + 1, "usage": {"input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens}}),
            },
            Event {
                kind: "tool_start",
                operations: json!([]),
                task_event: json!({"type": "tool_start", "roundNum": round + 1, "toolCallId": call.id, "toolName": call.name, "toolArgs": call.arguments}),
            },
        ];
        if config.events == EventMode::Parity {
            intent.splice(
                0..0,
                [
                    phase("waiting_model"),
                    phase("model_request_start"),
                    phase("tool_wire_projection"),
                    phase("delta"),
                    phase("preparing_tool_call"),
                ],
            );
        }
        let t = Instant::now();
        let entries: Vec<Value> = intent
            .into_iter()
            .map(|event| entry(&mut attempt, config, user, event))
            .collect();
        if config.events == EventMode::Parity {
            let (first, second) = entries.split_at(4);
            commit(&mut store, &attempt, user, first.to_vec())?;
            commit(&mut store, &attempt, user, second.to_vec())?;
        } else {
            commit(&mut store, &attempt, user, entries)?;
        }
        profile.intent_commit_us = t.elapsed().as_micros() as u64;
        attempt.segments += 2;
        attempt.tool_rounds += 1;

        let t = Instant::now();
        let result = dispatch(&call.name, &call.arguments, &config.workspace);
        profile.tool_us = t.elapsed().as_micros() as u64;

        let status = if result.ok { "done" } else { "error" };
        let mut receipt = vec![
            Event {
                kind: "round_usage",
                operations: json!([{"op": "set", "path": ["lastRoundUsage"], "value": {"round": round + 1,
                                    "tokensIn": completion.input_tokens, "tokensOut": completion.output_tokens}}]),
                task_event: json!({"type": "round_usage", "roundNum": round + 1, "tokensIn": completion.input_tokens, "tokensOut": completion.output_tokens}),
            },
            Event {
                kind: "tool_result",
                operations: json!([{"op": "set", "path": ["segments", segment + 1, "result"], "value": {"content": result.content, "status": status}},
                                   {"op": "set", "path": ["toolRounds", tool_round, "status"], "value": status},
                                   {"op": "set", "path": ["toolRounds", tool_round, "toolContent"], "value": result.content}]),
                task_event: json!({"type": "tool_complete", "roundNum": round + 1, "toolCallId": call.id, "toolName": call.name,
                                   "toolContent": result.content, "status": status}),
            },
        ];
        if config.events == EventMode::Parity {
            receipt.extend([
                phase("model_request_complete"),
                phase("delta_reset"),
                phase("tool_exec"),
                phase("tool_result"),
                phase("tool_complete"),
            ]);
        }
        let t = Instant::now();
        let entries: Vec<Value> = receipt
            .into_iter()
            .map(|event| entry(&mut attempt, config, user, event))
            .collect();
        commit(&mut store, &attempt, user, entries)?;
        profile.receipt_commit_us = t.elapsed().as_micros() as u64;

        let mut assistant = json!({"role": "assistant", "content": completion.text,
            "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}]});
        if completion.text.is_empty() {
            assistant
                .as_object_mut()
                .expect("object")
                .shift_remove("content");
        }
        history.push(assistant);
        history.push(json!({"role": "tool", "tool_call_id": call.id, "content": result.content}));
    }

    let terminal_base = attempt.revision;
    let sequence = attempt.sequence;
    let settlement = json!({"outcome": "completed", "cause": "provider_finished", "evidence": "provider_finish",
                            "streamState": "provider_finished", "providerFinishReason": "stop", "error": null});
    store.call(
        "turn.event.record",
        &json!({
            "attempt_id": attempt.attempt_id, "user_id": user, "task_id": attempt.task_id, "terminal": true,
            "status": "completed", "settlement": settlement, "error": {}, "event_type": "terminal_settlement",
            "event_payload": {"status": "completed", "settlement": settlement, "phase": null}, "now": now_ms(),
            "worker_claim": attempt.claim(),
            "projection_patch": {"version": 1, "baseRevision": terminal_base, "targetRevision": terminal_base + 1,
                                 "operations": [{"op": "set", "path": ["content"], "value": final_text}]},
            "projection_segments_stable": true,
            "task_event": {"task_id": attempt.task_id, "sequence": sequence, "event": {"type": "done", "taskId": attempt.task_id,
                           "finishReason": "stop", "streamState": "provider_finished"}},
        }),
        Some(&format!("turn-event:{}:terminal", attempt.attempt_id)),
    )?;
    store.call(
        "worker_job.complete",
        &json!({"task_id": attempt.task_id, "worker_id": attempt.worker_id, "fencing_token": attempt.fencing_token,
                "now_ms": now_ms(), "terminal_status": "succeeded", "result_ref": attempt.task_id, "error": {},
                "replay_cursor": 0, "user_id": user}),
        Some(&format!("rs-complete:{}", attempt.task_id)),
    )?;
    report.settled_ns = monotonic_ns();
    report.attempt_id = attempt.attempt_id.clone();
    report.turn_id = attempt.turn_id.clone();
    report.events_committed = attempt.sequence + 1;
    drop(archive_tx);
    let _ = archive_writer.join();
    report.status = "completed".to_owned();
    Ok(report)
}

fn hex_bytes(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap_or(0))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_are_well_formed() {
        assert_eq!(random_hex(4).len(), 8);
        assert_eq!(hex_bytes("00ff10"), vec![0, 255, 16]);
        assert!(monotonic_ns() > 0);
    }
}
