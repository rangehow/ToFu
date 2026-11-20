//! Loop-level rows of "Behaviours that are easy to lose"
//! (docs/RUST_REWRITE_EVIDENCE.md) pinned on the composed engine, beyond the
//! per-module tests and the Python-parity corpus (`engine_session_golden.rs`).

use std::sync::{Arc, Barrier, Mutex};

use serde_json::{json, Value};
use tofu_core_rs::engine::cancel::CancelToken;
use tofu_core_rs::engine::memory_store::{MemoryStore, OP_TOOL_INTENT};
use tofu_core_rs::engine::ports::{
    EventSink, ProviderPort, ProviderRequest, StoreError, StreamItem, ToolPort,
};
use tofu_core_rs::engine::redaction::ExactValueRedactor;
use tofu_core_rs::engine::session::{run, Ports, SessionConfig, SessionOutcome};
use tofu_core_rs::engine::types::{EffectClass, Message, ProviderOutcome, Role, ToolCall, ToolRun};

struct Script(Mutex<Vec<ProviderOutcome>>);

impl ProviderPort for Script {
    fn stream(
        &self,
        _request: &ProviderRequest,
        _on_item: &mut dyn FnMut(StreamItem),
        _cancel: &CancelToken,
    ) -> ProviderOutcome {
        let mut steps = self.0.lock().unwrap();
        if steps.is_empty() {
            return ProviderOutcome::Internal {
                detail: "script exhausted".into(),
            };
        }
        steps.remove(0)
    }
}

fn answer(text: &str) -> ProviderOutcome {
    ProviderOutcome::Completed {
        text: text.into(),
        tool_calls: Vec::new(),
        finish_reason: "stop".into(),
        usage: json!({}),
    }
}

fn calls(names: &[(&str, &str)]) -> ProviderOutcome {
    ProviderOutcome::Completed {
        text: String::new(),
        tool_calls: names
            .iter()
            .map(|(id, name)| ToolCall {
                id: (*id).into(),
                name: (*name).into(),
                arguments: "{}".into(),
            })
            .collect(),
        finish_reason: "tool_calls".into(),
        usage: json!({}),
    }
}

struct Published(Mutex<Vec<Value>>);

impl EventSink for Published {
    fn publish(&self, event: &Value) {
        self.0.lock().unwrap().push(event.clone());
    }
}

fn config() -> SessionConfig {
    SessionConfig {
        task_id: "t".into(),
        attempt_id: "a".into(),
        turn_id: "turn".into(),
        owner_user_id: 7,
        primary_model: "primary".into(),
        fallback_model: None,
        resumed_accepted_model: None,
        auto_turn_retry_max: 1,
    }
}

fn drive(
    provider: &dyn ProviderPort,
    tools: &dyn ToolPort,
    store: &MemoryStore,
    redactor: &ExactValueRedactor,
) -> (SessionOutcome, Vec<Value>) {
    let sink = Published(Mutex::new(Vec::new()));
    let pause = |_: f64| {};
    let ports = Ports {
        provider,
        tools,
        store,
        sink: &sink,
        redactor,
        pause: &pause,
    };
    let outcome = run(
        &config(),
        vec![Message::text(Role::User, "go")],
        &ports,
        &CancelToken::new(),
    );
    let published = sink.0.into_inner().unwrap();
    (outcome, published)
}

/// Row "Defensive: calls in a round run in parallel" (no pool, no lease):
/// both calls of one round must be inside their handlers at the same time.
#[test]
fn calls_of_one_round_run_at_the_same_time() {
    struct Rendezvous(Barrier);
    impl ToolPort for Rendezvous {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, call: &ToolCall, _: &CancelToken) -> ToolRun {
            self.0.wait();
            ToolRun::Finished {
                output: format!("ran {}", call.id),
                is_error: false,
            }
        }
    }
    let provider = Script(Mutex::new(vec![
        calls(&[("a", "read_files"), ("b", "read_files")]),
        answer("done"),
    ]));
    let tools = Rendezvous(Barrier::new(2));
    let store = MemoryStore::new();
    let (outcome, _) = drive(&provider, &tools, &store, &ExactValueRedactor::new([]));
    let results: Vec<&str> = outcome.tools.iter().map(|t| t.result.as_str()).collect();
    assert_eq!(results, ["ran a", "ran b"]);
    assert_eq!(outcome.content, "done");
}

/// Row "Tool effects: non-read-only calls commit an intent with an exact
/// storage ack before entry" (tests/tasks_pkg/test_tool_outcome_finality.py):
/// without the ack the handler never runs and the call settles not_started.
#[test]
fn a_mutating_call_without_an_acknowledged_intent_never_enters() {
    struct Forbidden(Arc<Mutex<u32>>);
    impl ToolPort for Forbidden {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::Mutating
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            *self.0.lock().unwrap() += 1;
            ToolRun::Finished {
                output: "wrote".into(),
                is_error: false,
            }
        }
    }
    let entered = Arc::new(Mutex::new(0));
    let provider = Script(Mutex::new(vec![
        calls(&[("w", "write_file")]),
        answer("ok"),
    ]));
    let store = MemoryStore::new();
    store.fail_next(
        OP_TOOL_INTENT,
        1,
        StoreError {
            code: "quota_exceeded".into(),
            message: "refused".into(),
            transient: false,
            not_dispatched: true,
        },
    );
    let (outcome, _) = drive(
        &provider,
        &Forbidden(Arc::clone(&entered)),
        &store,
        &ExactValueRedactor::new([]),
    );
    assert_eq!(*entered.lock().unwrap(), 0);
    let record = &outcome.tools[0];
    assert_eq!(
        record.effect.as_ref().unwrap().state.as_str(),
        "not_started"
    );
    assert!(record.is_error);
    assert!(store.intents().is_empty());
}

/// Row "a read pays nothing": read-only calls commit no intent and no
/// settlement.
#[test]
fn a_read_only_call_commits_no_intent() {
    struct Reader;
    impl ToolPort for Reader {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            ToolRun::Finished {
                output: "text".into(),
                is_error: false,
            }
        }
    }
    let provider = Script(Mutex::new(vec![
        calls(&[("r", "read_files")]),
        answer("ok"),
    ]));
    let store = MemoryStore::new();
    drive(&provider, &Reader, &store, &ExactValueRedactor::new([]));
    assert!(store.intents().is_empty());
    assert!(store.settlements().is_empty());
}

/// Recent fix "credential redaction at tool settlement": a known credential
/// in tool output never reaches the model-visible result, the settlement
/// commit or the published events.
#[test]
fn a_known_credential_is_redacted_before_settlement_and_publication() {
    const SECRET: &str = "sk-live-0123456789abcdef";
    struct Leaky;
    impl ToolPort for Leaky {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::Mutating
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            ToolRun::Finished {
                output: format!("token={SECRET}"),
                is_error: false,
            }
        }
    }
    let provider = Script(Mutex::new(vec![
        calls(&[("c", "run_command")]),
        answer("ok"),
    ]));
    let store = MemoryStore::new();
    let redactor = ExactValueRedactor::new([("api_key", SECRET)]);
    let (outcome, published) = drive(&provider, &Leaky, &store, &redactor);
    let everything = format!("{:?}{:?}{}", outcome.messages, published, store.snapshot());
    assert!(!everything.contains(SECRET));
    assert!(!outcome.tools[0].result.contains(SECRET));
}

/// Row "Settlement: terminal state is monotonic; settlement and terminal
/// projection commit together; nothing reaches the browser before commit".
#[test]
fn the_terminal_is_published_only_after_it_commits_with_its_projection() {
    struct NoTools;
    impl ToolPort for NoTools {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            unreachable!()
        }
    }
    let provider = Script(Mutex::new(vec![answer("final")]));
    let store = MemoryStore::new();
    store.fail_next(
        tofu_core_rs::engine::memory_store::OP_TERMINAL,
        2,
        StoreError {
            code: "storage_unavailable".into(),
            message: "restarting".into(),
            transient: true,
            not_dispatched: true,
        },
    );
    let (outcome, published) = drive(&provider, &NoTools, &store, &ExactValueRedactor::new([]));
    let (terminal, projection) = store.terminal().expect("settled once");
    assert_eq!(terminal["type"], "done");
    assert_eq!(projection["content"], "final");
    assert_eq!(outcome.content, "final");
    let dones: Vec<_> = published.iter().filter(|e| e["type"] == "done").collect();
    assert_eq!(
        dones,
        [&terminal],
        "the retried commit replays one terminal"
    );
    assert_eq!(store.snapshot()["failedCommits"]["terminal"], 2);
}

/// A refused terminal is retained, never reported as a committed result.
#[test]
fn terminal_commit_failure_is_exposed_without_a_false_completion() {
    struct NoTools;
    impl ToolPort for NoTools {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            unreachable!()
        }
    }
    let provider = Script(Mutex::new(vec![answer("final")]));
    let store = MemoryStore::new();
    store.fail_next(
        tofu_core_rs::engine::memory_store::OP_TERMINAL,
        1,
        StoreError {
            code: "disk_full".into(),
            message: "refused commit".into(),
            transient: false,
            not_dispatched: true,
        },
    );
    let (outcome, published) = drive(&provider, &NoTools, &store, &ExactValueRedactor::new([]));
    assert!(!outcome.terminal_committed);
    assert_eq!(outcome.settlement_error.as_ref().unwrap().code, "disk_full");
    assert!(outcome.pending_terminal.is_some());
    assert!(store.terminal().is_none());
    assert!(published.iter().all(|event| event["type"] != "done"));
}

/// A tool panic after entry settles unknown and cannot erase its intent or
/// prevent an independent parallel call's result from settling.
#[test]
fn tool_panics_settle_unknown_without_losing_sibling_results() {
    struct Panicking;
    impl ToolPort for Panicking {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::Mutating
        }
        fn run(&self, call: &ToolCall, _: &CancelToken) -> ToolRun {
            if call.id == "panic" {
                panic!("lost handler");
            }
            ToolRun::Finished {
                output: "sibling result".into(),
                is_error: false,
            }
        }
    }
    let provider = Script(Mutex::new(vec![
        calls(&[("panic", "write_file"), ("sibling", "write_file")]),
        answer("[END_TURN: blocked]"),
    ]));
    let store = MemoryStore::new();
    let (outcome, _) = drive(&provider, &Panicking, &store, &ExactValueRedactor::new([]));
    assert_eq!(
        outcome.tools[0].effect.as_ref().unwrap().state.as_str(),
        "unknown"
    );
    assert_eq!(outcome.tools[1].result, "sibling result");
    assert_eq!(store.intents().len(), 2);
    assert_eq!(store.settlements().len(), 2);
}

#[test]
fn a_refused_terminal_can_retry_the_exact_pair_without_rerunning_work() {
    struct NoTools;
    impl ToolPort for NoTools {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            unreachable!()
        }
    }
    let provider = Script(Mutex::new(vec![answer("final")]));
    let store = MemoryStore::new();
    store.fail_next(
        tofu_core_rs::engine::memory_store::OP_TERMINAL,
        1,
        StoreError {
            code: "disk_full".into(),
            message: "refused commit".into(),
            transient: false,
            not_dispatched: true,
        },
    );
    let (mut outcome, _) = drive(&provider, &NoTools, &store, &ExactValueRedactor::new([]));
    let retained = outcome.pending_terminal.as_ref().unwrap().clone();
    let sink = Published(Mutex::new(Vec::new()));
    tofu_core_rs::engine::session::retry_terminal(&mut outcome, &store, &sink).unwrap();
    assert!(outcome.terminal_committed);
    assert_eq!(
        store.terminal(),
        Some((retained.event, retained.projection))
    );
    assert!(provider.0.lock().unwrap().is_empty());
    assert_eq!(outcome.requests.len(), 1);
}

#[test]
fn reused_provider_call_ids_have_distinct_effect_occurrences() {
    struct Writer;
    impl ToolPort for Writer {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::Mutating
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            ToolRun::Finished {
                output: "wrote".into(),
                is_error: false,
            }
        }
    }
    let provider = Script(Mutex::new(vec![
        calls(&[("same", "write_file"), ("same", "run_command")]),
        calls(&[("same", "write_file")]),
        answer("done"),
    ]));
    let store = MemoryStore::new();
    let (outcome, _) = drive(&provider, &Writer, &store, &ExactValueRedactor::new([]));
    let ids: std::collections::BTreeSet<_> = outcome
        .tools
        .iter()
        .map(|record| record.effect.as_ref().unwrap().operation_id.as_str())
        .collect();
    assert_eq!(ids.len(), 3);
}

#[test]
fn verified_steers_are_acknowledged_and_never_injected_again() {
    struct NoTools;
    impl ToolPort for NoTools {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            unreachable!()
        }
    }
    let provider = Script(Mutex::new(vec![answer("received"), answer("next")]));
    let store = MemoryStore::new();
    let steer = tofu_core_rs::engine::steer::SteerCommand {
        conversation_id: "conv",
        attempt_id: "a",
        command_id: "steer1",
        text: "use a scratch deployment",
        author: None,
    }
    .record()
    .unwrap();
    store.push_steer(steer);
    let (first, _) = drive(&provider, &NoTools, &store, &ExactValueRedactor::new([]));
    assert_eq!(
        first.requests[0].messages.last().unwrap().content,
        "use a scratch deployment"
    );
    assert_eq!(
        store.snapshot()["confirmedSteers"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let (second, _) = drive(&provider, &NoTools, &store, &ExactValueRedactor::new([]));
    assert_eq!(second.requests[0].messages.len(), 1);
    assert!(store.snapshot()["requeuedSteers"]
        .as_array()
        .unwrap()
        .is_empty());
}

#[test]
fn a_steer_committed_during_the_last_request_is_requeued_on_completion() {
    struct LastRequest<'a>(&'a MemoryStore);
    impl ProviderPort for LastRequest<'_> {
        fn stream(
            &self,
            _: &ProviderRequest,
            _: &mut dyn FnMut(StreamItem),
            _: &CancelToken,
        ) -> ProviderOutcome {
            let steer = tofu_core_rs::engine::steer::SteerCommand {
                conversation_id: "conv",
                attempt_id: "a",
                command_id: "late",
                text: "do not lose this",
                author: None,
            }
            .record()
            .unwrap();
            self.0.push_steer(steer);
            answer("already finishing")
        }
    }
    struct NoTools;
    impl ToolPort for NoTools {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::ReadOnly
        }
        fn run(&self, _: &ToolCall, _: &CancelToken) -> ToolRun {
            unreachable!()
        }
    }
    let store = MemoryStore::new();
    let (_, _) = drive(
        &LastRequest(&store),
        &NoTools,
        &store,
        &ExactValueRedactor::new([]),
    );
    let snapshot = store.snapshot();
    let queued = snapshot["requeuedSteers"].as_array().unwrap();
    assert_eq!(queued.len(), 1);
    assert_eq!(queued[0]["previews"][0]["text"], "do not lose this");
}

#[test]
fn cancelled_entered_writes_expose_the_nonretryable_unknown_receipt() {
    struct Interrupted;
    impl ToolPort for Interrupted {
        fn effect_class(&self, _: &str) -> EffectClass {
            EffectClass::Mutating
        }
        fn run(&self, _: &ToolCall, cancel: &CancelToken) -> ToolRun {
            cancel.cancel("server_shutdown");
            ToolRun::Cancelled { entered: true }
        }
    }
    let provider = Script(Mutex::new(vec![calls(&[("write", "write_file")])]));
    let store = MemoryStore::new();
    let (outcome, _) = drive(
        &provider,
        &Interrupted,
        &store,
        &ExactValueRedactor::new([]),
    );
    let record = &outcome.tools[0];
    let receipt: Value = serde_json::from_str(&record.result).unwrap();
    assert_eq!(receipt["code"], "tool_effect_unknown");
    assert_eq!(receipt["retryable"], false);
    assert!(record
        .result
        .contains(&record.effect.as_ref().unwrap().operation_id));
}
