use serde_json::{json, Value};
use tofu_core_rs::engine::cancel::CancelToken;
use tofu_core_rs::engine::ports::{ProviderPort, ProviderRequest, StreamItem};
use tofu_core_rs::engine::provider_wire::*;
use tofu_core_rs::engine::types::{Message, ProviderOutcome, Role, ToolCall};
use tofu_core_rs::route::RouteSpec;
use tofu_core_rs::stream::StreamOutcome;
use tofu_core_rs::wire::{Protocol, SealedRequest};

fn options(protocol: Protocol) -> RequestOptions<'static> {
    RequestOptions {
        protocol,
        route: RouteSpec::plain("http://127.0.0.1:1/v1", "scratch-key"),
        max_tokens: 64,
        temperature: None,
        tools: None,
        reasoning: None,
    }
}

fn request() -> ProviderRequest {
    ProviderRequest {
        model: "gpt-4.1".into(),
        messages: vec![Message::text(Role::User, "hello")],
        round: 1,
    }
}

fn frame(value: Value) -> String {
    format!("data: {value}\n\n")
}
fn text_frame(text: &str) -> String {
    frame(json!({"choices":[{"delta":{"content":text}}]}))
}
fn finish_frame(reason: &str) -> String {
    frame(json!({"choices":[{"delta":{},"finish_reason":reason}]}))
}
fn response(body: impl Into<Vec<u8>>) -> TransportResponse {
    TransportResponse {
        status: Some(200),
        body: body.into(),
        failure: None,
    }
}
fn decoded(body: String) -> ProviderReport {
    decode(
        &prepare(&request(), &options(Protocol::OpenAi)).unwrap(),
        response(body),
        "trace",
    )
}

#[test]
fn seals_canonical_history_and_selected_protocol() {
    let mut req = request();
    req.messages.push(Message::text(Role::User, "world"));
    for (protocol, suffix) in [
        (Protocol::OpenAi, "chat/completions"),
        (Protocol::Anthropic, "messages"),
        (Protocol::Responses, "responses"),
    ] {
        let prepared = prepare(&req, &options(protocol)).unwrap();
        assert!(prepared.sealed.url.ends_with(suffix));
        let payload: Value = serde_json::from_slice(&prepared.sealed.payload).unwrap();
        assert_eq!(payload["model"], "gpt-4.1");
        assert_eq!(payload["stream"], true);
        if protocol == Protocol::OpenAi {
            assert_eq!(
                payload["messages"],
                json!([{"role":"user","content":"hello\n\nworld"}])
            );
        }
    }
    let call = ToolCall {
        id: "call-1".into(),
        name: "lookup".into(),
        arguments: "{\"q\":1}".into(),
    };
    req.messages.push(Message {
        role: Role::Assistant,
        content: String::new(),
        tool_calls: vec![call.clone()],
        tool_call_id: None,
    });
    req.messages.push(Message::tool_result(&call.id, "found"));
    let prepared = prepare(&req, &options(Protocol::OpenAi)).unwrap();
    let payload: Value = serde_json::from_slice(&prepared.sealed.payload).unwrap();
    assert_eq!(
        payload["messages"][1]["tool_calls"][0]["function"]["arguments"],
        call.arguments
    );
    assert_eq!(payload["messages"][2]["tool_call_id"], call.id);
}

#[test]
fn completion_preserves_tool_arguments_finish_and_usage() {
    let usage = json!({"prompt_tokens":17,"completion_tokens":4,"prompt_tokens_details":{"cached_tokens":8}});
    let body = text_frame("checking")
        + &frame(
            json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"lookup","arguments":"{\"q\":"}}]}}]}),
        )
        + &frame(
            json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}),
        )
        + &frame(json!({"choices":[],"usage":usage}))
        + "data: [DONE]\n\n";
    let report = decoded(body);
    match report.outcome {
        ProviderOutcome::Completed {
            text,
            tool_calls,
            finish_reason,
            usage: actual,
        } => {
            assert_eq!(text, "checking");
            assert_eq!(finish_reason, "tool_calls");
            assert_eq!(
                tool_calls,
                vec![ToolCall {
                    id: "c".into(),
                    name: "lookup".into(),
                    arguments: "{\"q\":1}".into()
                }]
            );
            assert_eq!(actual["prompt_tokens"], 17);
            assert_eq!(actual["completion_tokens"], 4);
            assert_eq!(actual["prompt_tokens_details"]["cached_tokens"], 8);
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn refuses_malformed_unfinished_done_only_and_unknown_finishes() {
    for body in [
        text_frame("partial"),
        text_frame("partial") + "data: [DONE]\n\n",
        text_frame("partial") + "data: not-json\n\n" + &finish_frame("stop"),
    ] {
        let report = decoded(body);
        assert!(
            matches!(report.outcome, ProviderOutcome::Dropped {partial_text, ..} if partial_text == "partial")
        );
        assert!(matches!(
            report.parsed,
            Some(Ok(StreamOutcome::Finished(_)))
        ));
    }
    assert!(matches!(
        decoded(text_frame("x") + &finish_frame("invented")).outcome,
        ProviderOutcome::Internal { .. }
    ));
}

#[test]
fn preserves_refusal_raised_and_http_error_evidence() {
    assert!(
        matches!(decoded(text_frame("unsafe")+&finish_frame("content_filter")).outcome,ProviderOutcome::ContentFilter {partial_text,..} if partial_text=="unsafe")
    );
    let report = decoded(
        text_frame("partial")
            + &frame(json!({"error":{"code":429,"message":"rate limit exceeded"}})),
    );
    assert!(
        matches!(
            report.outcome,
            ProviderOutcome::HttpError { status: 429, .. }
        ),
        "{:?}",
        report.outcome
    );
    assert!(matches!(report.parsed, Some(Ok(StreamOutcome::Raised(_)))));
    assert!(String::from_utf8(report.response.body)
        .unwrap()
        .contains("partial"));
    let prepared = prepare(&request(), &options(Protocol::OpenAi)).unwrap();
    let report = decode(
        &prepared,
        TransportResponse {
            status: Some(503),
            body: b"unavailable".to_vec(),
            failure: None,
        },
        "trace",
    );
    assert_eq!(
        report.outcome,
        ProviderOutcome::HttpError {
            status: 503,
            detail: "unavailable".into(),
            partial_text: String::new()
        }
    );
}

struct Script(TransportResponse);
impl TransportPort for Script {
    fn exchange(&self, _: &SealedRequest, _: &str, _: &CancelToken) -> TransportResponse {
        self.0.clone()
    }
}

#[test]
fn callbacks_are_buffered_and_cancellation_never_emits_tools() {
    let adapter = WireProvider {
        model: "gpt-4.1",
        transport: Script(response(text_frame("ok") + &finish_frame("stop"))),
        options: options(Protocol::OpenAi),
        trace_id: "trace".into(),
    };
    let mut items = Vec::new();
    assert!(matches!(
        adapter.stream(
            &request(),
            &mut |item| items.push(item),
            &CancelToken::new()
        ),
        ProviderOutcome::Completed { .. }
    ));
    assert_eq!(items, vec![StreamItem::Text("ok".into())]);
    let cancel = CancelToken::new();
    cancel.cancel("user_stop");
    items.clear();
    assert!(matches!(
        adapter.stream(&request(), &mut |item| items.push(item), &cancel),
        ProviderOutcome::Cancelled { .. }
    ));
    assert!(items.is_empty());
}

#[test]
fn scratch_provider_midstream_disconnect_is_not_retried_or_completed() {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::{TcpListener, TcpStream};
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = std::thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        let mut reader = BufReader::new(socket.try_clone().unwrap());
        let mut line = String::new();
        let mut length = 0;
        loop {
            line.clear();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" {
                break;
            }
            if let Some(value) = line.strip_prefix("Content-Length: ") {
                length = value.trim().parse().unwrap();
            }
        }
        let mut body = vec![0; length];
        reader.read_exact(&mut body).unwrap();
        let body: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["model"], "gpt-4.1");
        let partial = text_frame("before fault");
        write!(
            socket,
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}",
            partial.len() + 500,
            partial
        )
        .unwrap();
    });
    struct Scratch(std::net::SocketAddr);
    impl TransportPort for Scratch {
        fn exchange(&self, request: &SealedRequest, _: &str, _: &CancelToken) -> TransportResponse {
            let mut socket = TcpStream::connect(self.0).unwrap();
            write!(socket,"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: {}\r\n\r\n",request.payload.len()).unwrap();
            socket.write_all(&request.payload).unwrap();
            let mut reader = BufReader::new(socket);
            let mut line = String::new();
            loop {
                line.clear();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
            }
            let mut body = Vec::new();
            reader.read_to_end(&mut body).unwrap();
            TransportResponse {
                status: Some(200),
                body,
                failure: Some(TransportFailure::Dropped(
                    "Content-Length body truncated".into(),
                )),
            }
        }
    }
    let adapter = WireProvider {
        model: "gpt-4.1",
        transport: Scratch(address),
        options: options(Protocol::OpenAi),
        trace_id: "scratch".into(),
    };
    let mut items = Vec::new();
    assert_eq!(
        adapter.stream(
            &request(),
            &mut |item| items.push(item),
            &CancelToken::new()
        ),
        ProviderOutcome::Dropped {
            partial_text: "before fault".into(),
            detail: "Content-Length body truncated".into()
        }
    );
    assert!(items.is_empty());
    server.join().unwrap();
}

#[test]
fn adapter_retains_every_python_stream_oracle_result() {
    let golden: Value = serde_json::from_str(include_str!("fixtures/stream_golden.json")).unwrap();
    for case in golden["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let mut req = request();
        req.model = case["model"].as_str().unwrap().into();
        let mut opts = options(Protocol::parse(case["protocol"].as_str().unwrap()).unwrap());
        opts.max_tokens = golden["max_tokens"].as_u64().unwrap_or(0);
        // Preparing every fixture model would also exercise unrelated reasoning
        // admission; here the oracle isolates the parser binding contract.
        let prepared = PreparedRequest {
            sealed: prepare(&request(), &opts).unwrap().sealed,
            protocol: opts.protocol,
            model: req.model,
            max_tokens: opts.max_tokens as i64,
        };
        let report = decode(
            &prepared,
            response(case["sse"].as_str().unwrap()),
            golden["trace_id"].as_str().unwrap(),
        );
        let actual = report.parsed.unwrap().unwrap().to_json();
        assert_eq!(actual, case["expected"], "{name}");
        if let Some(state) = actual["state"].as_str() {
            if state != "provider_finished" {
                assert!(
                    matches!(report.outcome, ProviderOutcome::Dropped { .. }),
                    "{name}: {:?}",
                    report.outcome
                );
            } else if actual["is_model_refusal"] == true {
                assert!(
                    matches!(report.outcome, ProviderOutcome::ContentFilter { .. }),
                    "{name}: {:?}",
                    report.outcome
                );
            }
        }
    }
}

#[test]
fn refuses_unrepresentable_replay_and_checks_late_cancellation() {
    let reasoning =
        frame(json!({"choices":[{"delta":{"reasoning_content":"private","content":"answer"}}]}))
            + &finish_frame("stop");
    let report = decoded(reasoning);
    assert!(matches!(report.outcome, ProviderOutcome::Internal { .. }));
    assert!(report.parsed.unwrap().unwrap().to_json()["message"]
        .get("reasoning_content")
        .is_some());
    struct CancelDuringRead;
    impl TransportPort for CancelDuringRead {
        fn exchange(&self, _: &SealedRequest, _: &str, cancel: &CancelToken) -> TransportResponse {
            cancel.cancel("user_stop");
            response(text_frame("partial") + &finish_frame("stop"))
        }
    }
    let adapter = WireProvider {
        model: "gpt-4.1",
        transport: CancelDuringRead,
        options: options(Protocol::OpenAi),
        trace_id: "trace".into(),
    };
    let mut items = Vec::new();
    assert_eq!(
        adapter.stream(
            &request(),
            &mut |item| items.push(item),
            &CancelToken::new()
        ),
        ProviderOutcome::Cancelled {
            partial_text: "partial".into()
        }
    );
    assert!(items.is_empty());
}

#[test]
fn fixed_route_rejects_model_switch_before_transport() {
    struct NoSend;
    impl TransportPort for NoSend {
        fn exchange(&self, _: &SealedRequest, _: &str, _: &CancelToken) -> TransportResponse {
            panic!("unsupported model must never reach this route")
        }
    }
    let adapter = WireProvider {
        model: "gpt-4.1",
        transport: NoSend,
        options: options(Protocol::OpenAi),
        trace_id: "trace".into(),
    };
    let mut req = request();
    req.model = "other-provider-model".into();
    assert!(matches!(
        adapter.exchange(&req, &CancelToken::new()),
        Err(tofu_core_rs::canonical::Error::Unsupported(_))
    ));
}

#[test]
fn gateway_450_is_not_a_model_refusal() {
    let report = decoded(frame(
        json!({"error":{"code":450,"message":"blocked by gateway policy"}}),
    ));
    assert!(
        matches!(
            report.outcome,
            ProviderOutcome::HttpError { status: 450, .. }
        ),
        "{:?}",
        report.outcome
    );
    assert_eq!(
        report.parsed.unwrap().unwrap().to_json()["error"]["class"],
        "ContentFilterError"
    );
}
