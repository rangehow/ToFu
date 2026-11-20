//! End-to-end socket behaviour of `PushFrontDoor` against a scripted Python
//! bridge (the `tofu.push-bridge/v1` peer), over real TCP WebSockets.

use futures_util::{SinkExt, StreamExt};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use serde_json::{json, Value};
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tofu_front_push::bridge::{bridge_token, BRIDGE_HEADER, BRIDGE_PATH};
use tofu_front_push::socket::PushTiming;
use tofu_front_push::PushFrontDoor;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

const SECRET: &str = "test-secret";
const WAIT: Duration = Duration::from_secs(10);

type Client = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// The scripted Python side: answers `admit` from `decide`, records every
/// op, and publishes whatever the test sends on `publish`.
struct FakePython {
    addr: SocketAddr,
    ops: mpsc::UnboundedReceiver<Value>,
    publish: mpsc::UnboundedSender<Option<Value>>,
}

async fn fake_python(decide: fn(&Value) -> Value) -> FakePython {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (ops_tx, ops) = mpsc::unbounded_channel();
    let (publish, publish_rx) = mpsc::unbounded_channel::<Option<Value>>();
    let publish_rx = Arc::new(tokio::sync::Mutex::new(publish_rx));
    tokio::spawn(async move {
        loop {
            let (stream, _) = listener.accept().await.unwrap();
            let ops_tx = ops_tx.clone();
            let publish_rx = Arc::clone(&publish_rx);
            tokio::spawn(async move {
                let expected = bridge_token(SECRET);
                // tungstenite's `Callback` fixes the error type.
                #[allow(clippy::result_large_err)]
                let check = |request: &Request, response: Response| {
                    assert_eq!(request.uri().path(), BRIDGE_PATH);
                    assert_eq!(request.headers()[BRIDGE_HEADER], expected.as_str());
                    Ok(response)
                };
                let Ok(socket) = tokio_tungstenite::accept_hdr_async(stream, check).await else {
                    return;
                };
                let (mut write, mut read) = socket.split();
                write
                    .send(Message::text(
                        json!({"op": "build_id", "buildId": "main-abc.js"}).to_string(),
                    ))
                    .await
                    .unwrap();
                let mut publish_rx = publish_rx.lock().await;
                loop {
                    tokio::select! {
                        incoming = read.next() => {
                            let Some(Ok(Message::Text(text))) = incoming else { return };
                            let op: Value = serde_json::from_str(text.as_str()).unwrap();
                            if op["op"] == "admit" {
                                let mut reply = decide(&op);
                                reply["op"] = json!("admitted");
                                reply["id"] = op["id"].clone();
                                write.send(Message::text(reply.to_string())).await.unwrap();
                            }
                            if op["op"] == "subscribe" && op["channel"] != "held" {
                                let ack = json!({"op": "mirrored", "id": op["id"]});
                                write.send(Message::text(ack.to_string())).await.unwrap();
                            }
                            let _ = ops_tx.send(op);
                        }
                        published = publish_rx.recv() => match published {
                            Some(Some(op)) => write.send(Message::text(op.to_string())).await.unwrap(),
                            // `None`: drop the bridge as a restarting Python would.
                            _ => return,
                        },
                    }
                }
            });
        }
    });
    FakePython { addr, ops, publish }
}

fn owner_one(_op: &Value) -> Value {
    json!({"status": 101, "userId": "1", "rid": "page-1", "subprotocol": null})
}

async fn front_door(
    python: &FakePython,
    timing: PushTiming,
    capacity: usize,
) -> (SocketAddr, Arc<PushFrontDoor>) {
    let door = Arc::new(
        PushFrontDoor::new(&python.addr.to_string(), SECRET)
            .with_timing(timing)
            .with_queue_capacity(capacity),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let served = Arc::clone(&door);
    tokio::spawn(async move {
        loop {
            let (stream, peer) = listener.accept().await.unwrap();
            let door = Arc::clone(&served);
            tokio::spawn(async move {
                let service = service_fn(move |request| {
                    let door = Arc::clone(&door);
                    async move {
                        assert!(PushFrontDoor::accepts(&request));
                        Ok::<_, Infallible>(door.handle(request, peer).await)
                    }
                });
                let _ = http1::Builder::new()
                    .serve_connection(TokioIo::new(stream), service)
                    .with_upgrades()
                    .await;
            });
        }
    });
    (addr, door)
}

async fn connect(addr: SocketAddr, query: &str) -> Result<Client, WsError> {
    let request = format!("ws://{addr}/api/push{query}")
        .into_client_request()
        .unwrap();
    tokio_tungstenite::connect_async(request)
        .await
        .map(|(socket, _)| socket)
}

async fn next_op(python: &mut FakePython, kind: &str) -> Value {
    loop {
        let op = tokio::time::timeout(WAIT, python.ops.recv())
            .await
            .unwrap()
            .unwrap();
        if op["op"] == kind {
            return op;
        }
    }
}

async fn next_text(socket: &mut Client) -> String {
    loop {
        match tokio::time::timeout(WAIT, socket.next()).await.unwrap() {
            Some(Ok(Message::Text(text))) => return text.to_string(),
            Some(Ok(Message::Ping(_) | Message::Pong(_))) => continue,
            other => panic!("expected a text frame, got {other:?}"),
        }
    }
}

async fn close_code(socket: &mut Client) -> Option<CloseCode> {
    loop {
        match tokio::time::timeout(WAIT, socket.next()).await.unwrap() {
            Some(Ok(Message::Close(frame))) => return frame.map(|frame| frame.code),
            Some(Ok(_)) => continue,
            _ => return None,
        }
    }
}

async fn send(socket: &mut Client, frame: Value) {
    socket.send(Message::text(frame.to_string())).await.unwrap();
}

fn frame(channel: &str, task: &str, user: &str, latest: Option<&str>, text: &str) -> Value {
    json!({"op": "frame", "channel": channel, "taskId": task, "userId": user, "latestKey": latest, "text": text})
}

fn quiet() -> PushTiming {
    PushTiming {
        ping_after_idle: Duration::from_secs(3600),
        receive_idle: Duration::from_secs(3600),
    }
}

#[tokio::test]
async fn admission_forwards_the_handshake_and_refusals_keep_their_status() {
    let mut python = fake_python(|op| {
        let query = op["query"].as_str().unwrap();
        if query.contains("deny") {
            json!({"status": 401, "rid": "x"})
        } else {
            json!({"status": 101, "userId": "1", "rid": "page-1", "subprotocol": null})
        }
    })
    .await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    match connect(addr, "?deny=1").await {
        Err(WsError::Http(response)) => assert_eq!(response.status().as_u16(), 401),
        other => panic!("expected an HTTP refusal, got {:?}", other.map(|_| ())),
    }
    let admit = next_op(&mut python, "admit").await;
    assert_eq!(admit["query"], "deny=1");
    assert_eq!(admit["peer"][0], "127.0.0.1");
    assert!(admit["headers"]
        .as_array()
        .unwrap()
        .iter()
        .any(|pair| pair[0] == "sec-websocket-key"));
    let _socket = connect(addr, "?_rid=page-1").await.unwrap();
    let open = next_op(&mut python, "open").await;
    assert_eq!(open["userId"], "1");
    assert_eq!(open["rid"], "page-1");
}

#[tokio::test]
async fn subscriptions_route_frames_by_task_wildcard_and_owner() {
    let mut python = fake_python(owner_one).await;
    let (addr, door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "chat", "taskId": "t1"}),
    )
    .await;
    let mut mirror = next_op(&mut python, "subscribe").await;
    assert!(mirror["id"].is_u64());
    mirror.as_object_mut().unwrap().remove("id");
    assert_eq!(
        mirror,
        json!({"op": "subscribe", "channel": "chat", "taskId": "t1", "userId": "1"})
    );
    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "notify"}),
    )
    .await;
    assert_eq!(next_op(&mut python, "subscribe").await["taskId"], "*");

    python
        .publish
        .send(Some(frame("chat", "t1", "2", None, "foreign")))
        .unwrap();
    python
        .publish
        .send(Some(frame("chat", "t2", "1", None, "other-task")))
        .unwrap();
    python
        .publish
        .send(Some(frame("chat", "t1", "1", None, r#"{"a":1}"#)))
        .unwrap();
    python
        .publish
        .send(Some(frame("notify", "anything", "1", None, "wild")))
        .unwrap();
    assert_eq!(next_text(&mut socket).await, r#"{"a":1}"#);
    assert_eq!(next_text(&mut socket).await, "wild");

    send(
        &mut socket,
        json!({"action": "unsubscribe", "channel": "chat", "taskId": "t1"}),
    )
    .await;
    assert_eq!(next_op(&mut python, "unsubscribe").await["taskId"], "t1");
    assert_eq!(door.hub().subscription_buckets(), 1);
}

#[tokio::test]
async fn ping_answers_a_python_byte_identical_pong_and_build_probe() {
    let mut python = fake_python(owner_one).await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    let _ = next_op(&mut python, "open").await;
    send(
        &mut socket,
        json!({"action": "ping", "t": 1731234567890i64}),
    )
    .await;
    assert_eq!(
        next_text(&mut socket).await,
        r#"{"channel": "system", "t": 1731234567890, "type": "pong"}"#
    );
    socket
        .send(Message::text(
            r#"{"action":"ping","t":12.50,"buildProbe":true}"#,
        ))
        .await
        .unwrap();
    assert_eq!(
        next_text(&mut socket).await,
        r#"{"buildId": "main-abc.js", "channel": "system", "t": 12.5, "type": "pong"}"#
    );
    let _ = next_op(&mut python, "build_probe").await;
    send(&mut socket, json!({"action": "ping"})).await;
    assert_eq!(
        next_text(&mut socket).await,
        r#"{"channel": "system", "t": null, "type": "pong"}"#
    );
}

#[tokio::test]
async fn abort_and_rpc_frames_reach_python_and_rpc_replies_jump_events() {
    let mut python = fake_python(owner_one).await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    let open = next_op(&mut python, "open").await;
    send(
        &mut socket,
        json!({"action": "abort", "channel": "chat", "taskId": "t9"}),
    )
    .await;
    let abort = next_op(&mut python, "abort").await;
    assert_eq!(abort["taskId"], "t9");
    assert_eq!(abort["userId"], "1");
    // Python ignores a wildcard abort and a non-chat abort.
    send(&mut socket, json!({"action": "abort", "channel": "chat"})).await;
    send(
        &mut socket,
        json!({"action": "abort", "channel": "paper", "taskId": "t9"}),
    )
    .await;
    let request = json!({"jsonrpc": "2.0", "id": "r1", "method": "project.browse", "params": {}});
    send(&mut socket, request.clone()).await;
    let rpc = next_op(&mut python, "rpc").await;
    assert_eq!(rpc["frame"], request);
    assert_eq!(rpc["socket"], open["socket"]);
    // A JSON-RPC frame never reaches the push actions, even with `action`.
    send(
        &mut socket,
        json!({"jsonrpc": "2.0", "action": "abort", "channel": "chat", "taskId": "zz"}),
    )
    .await;
    assert_eq!(next_op(&mut python, "rpc").await["frame"]["taskId"], "zz");

    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "chat", "taskId": "t"}),
    )
    .await;
    let _ = next_op(&mut python, "subscribe").await;
    python
        .publish
        .send(Some(frame("chat", "t", "1", None, "event")))
        .unwrap();
    assert_eq!(next_text(&mut socket).await, "event");
    python
        .publish
        .send(Some(
            json!({"op": "rpc", "socket": open["socket"], "text": "{\"id\": \"r1\"}"}),
        ))
        .unwrap();
    assert_eq!(next_text(&mut socket).await, r#"{"id": "r1"}"#);
}

#[tokio::test]
async fn latest_value_frames_coalesce_while_the_socket_is_behind() {
    let mut python = fake_python(owner_one).await;
    let (_addr, door) = front_door(&python, quiet(), 64).await;
    // Drive the hub directly: a connected socket drains immediately, so
    // coalescing is observable only on a socket that is behind.
    let hub = door.hub();
    let client = Arc::new(tofu_front_push::client::PushClient::new(
        "1".into(),
        String::new(),
        64,
    ));
    let id = hub.register(Arc::clone(&client), hub.generation()).unwrap();
    hub.subscribe(id, "office", "__office__");
    for value in ["v1", "v2", "v3"] {
        hub.deliver("office", "__office__", "1", Some("fact"), Arc::from(value));
    }
    assert_eq!(client.queued_events(), 1);
    drop(python.ops.try_recv());
}

#[tokio::test]
async fn idle_socket_gets_python_keepalive_ping_and_lease_heartbeat() {
    let mut python = fake_python(owner_one).await;
    let timing = PushTiming {
        ping_after_idle: Duration::from_millis(200),
        receive_idle: Duration::from_secs(3600),
    };
    let (addr, _door) = front_door(&python, timing, 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    assert_eq!(
        next_text(&mut socket).await,
        r#"{"channel": "system", "type": "ping"}"#
    );
    let _ = next_op(&mut python, "heartbeat").await;
}

#[tokio::test]
async fn receive_idle_releases_with_going_away() {
    let mut python = fake_python(owner_one).await;
    let timing = PushTiming {
        ping_after_idle: Duration::from_secs(3600),
        receive_idle: Duration::from_millis(200),
    };
    let (addr, door) = front_door(&python, timing, 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "notify"}),
    )
    .await;
    let _ = next_op(&mut python, "subscribe").await;
    assert_eq!(close_code(&mut socket).await, Some(CloseCode::Away));
    assert_eq!(
        next_op(&mut python, "unsubscribe").await["channel"],
        "notify"
    );
    let _ = next_op(&mut python, "close").await;
    assert_eq!(door.hub().client_count(), 0);
}

#[tokio::test]
async fn malformed_json_releases_and_oversize_closes_1009() {
    let python = fake_python(owner_one).await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    socket.send(Message::text("{not json")).await.unwrap();
    assert_eq!(close_code(&mut socket).await, Some(CloseCode::Away));
    let mut socket = connect(addr, "").await.unwrap();
    socket
        .send(Message::text("x".repeat(64 * 1024 + 1)))
        .await
        .unwrap();
    assert_eq!(close_code(&mut socket).await, Some(CloseCode::Size));
}

#[tokio::test]
async fn backlog_overflow_releases_the_stalled_socket() {
    let mut python = fake_python(owner_one).await;
    let (_addr, door) = front_door(&python, quiet(), 64).await;
    let hub = door.hub();
    let client = Arc::new(tofu_front_push::client::PushClient::new(
        "1".into(),
        String::new(),
        64,
    ));
    let id = hub.register(Arc::clone(&client), hub.generation()).unwrap();
    hub.subscribe(id, "chat", "*");
    for index in 0..65 {
        hub.deliver("chat", "t", "1", None, Arc::from(index.to_string()));
    }
    assert!(!client.is_connected());
    drop(python.ops.try_recv());
}

#[tokio::test]
async fn bridge_loss_releases_sockets_and_the_next_handshake_reconnects() {
    let mut python = fake_python(owner_one).await;
    let (addr, door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    let _ = next_op(&mut python, "open").await;
    python.publish.send(None).unwrap();
    assert_eq!(close_code(&mut socket).await, Some(CloseCode::Away));
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert_eq!(door.hub().client_count(), 0);
    let _again = connect(addr, "").await.unwrap();
    let _ = next_op(&mut python, "open").await;
    assert_eq!(door.hub().client_count(), 1);
}

#[tokio::test]
async fn shutdown_releases_sockets_and_refuses_new_ones_with_503() {
    let mut python = fake_python(owner_one).await;
    let (addr, door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    let _ = next_op(&mut python, "open").await;
    assert_eq!(door.shutdown(), 1);
    assert_eq!(close_code(&mut socket).await, Some(CloseCode::Away));
    match connect(addr, "").await {
        Err(WsError::Http(response)) => assert_eq!(response.status().as_u16(), 503),
        other => panic!("expected 503, got {:?}", other.map(|_| ())),
    }
}

#[tokio::test]
async fn no_python_answers_502_like_the_relay() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dead = listener.local_addr().unwrap();
    drop(listener);
    let python = FakePython {
        addr: dead,
        ops: mpsc::unbounded_channel().1,
        publish: mpsc::unbounded_channel().0,
    };
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    match connect(addr, "").await {
        Err(WsError::Http(response)) => assert_eq!(response.status().as_u16(), 502),
        other => panic!("expected 502, got {:?}", other.map(|_| ())),
    }
}

#[tokio::test]
async fn subprotocol_selected_by_python_is_echoed() {
    let python = fake_python(
        |_| json!({"status": 101, "userId": "1", "rid": "r", "subprotocol": "tofu.push.v1"}),
    )
    .await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    let mut request = format!("ws://{addr}/api/push")
        .into_client_request()
        .unwrap();
    request.headers_mut().insert(
        "sec-websocket-protocol",
        "tofu.push.v1, tofu.origin-proof.abc".parse().unwrap(),
    );
    let (_socket, response) = tokio_tungstenite::connect_async(request).await.unwrap();
    assert_eq!(response.headers()["sec-websocket-protocol"], "tofu.push.v1");
}

#[test]
fn contracted_push_frames_route_on_their_declared_channel() {
    use tofu_contracts::generated_events_v1::{push_frame_metadata, PUSH_FRAMES};
    let fixtures =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../contracts/fixtures/push");
    let hub = tofu_front_push::hub::PushHub::new();
    let client = Arc::new(tofu_front_push::client::PushClient::new(
        "7".into(),
        String::new(),
        64,
    ));
    let id = hub.register(Arc::clone(&client), hub.generation()).unwrap();
    for metadata in PUSH_FRAMES {
        hub.subscribe(id, metadata.channel, "*");
    }
    let mut routed = 0;
    for entry in std::fs::read_dir(fixtures).unwrap() {
        let path = entry.unwrap().path();
        let text = std::fs::read_to_string(&path).unwrap();
        let fixture: Value = serde_json::from_str(&text).unwrap();
        let metadata =
            push_frame_metadata(fixture["type"].as_str().unwrap()).expect("contracted type");
        assert_eq!(
            hub.deliver(
                metadata.channel,
                metadata.task,
                "7",
                None,
                Arc::from(text.as_str())
            ),
            1,
            "{}",
            path.display()
        );
        routed += 1;
    }
    assert_eq!(routed, PUSH_FRAMES.len());
}

#[tokio::test]
async fn a_first_subscription_is_acknowledged_by_python_before_the_next_frame_is_read() {
    let mut python = fake_python(owner_one).await;
    let (addr, _door) = front_door(&python, quiet(), 64).await;
    let mut socket = connect(addr, "").await.unwrap();
    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "held"}),
    )
    .await;
    let _ = next_op(&mut python, "subscribe").await;
    send(&mut socket, json!({"action": "ping", "t": 1})).await;
    let early = tokio::time::timeout(Duration::from_millis(300), socket.next()).await;
    assert!(
        early.is_err(),
        "pong overtook the unacknowledged subscription: {early:?}"
    );
    send(
        &mut socket,
        json!({"action": "subscribe", "channel": "notify"}),
    )
    .await;
    // Still held: the second subscribe is not read until the first is acked.
    assert!(
        tokio::time::timeout(Duration::from_millis(300), python.ops.recv())
            .await
            .is_err()
    );
}
