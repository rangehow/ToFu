//! `Peer` against a raw line stream: the exact bytes Python's `_Peer` writes
//! and the answers it gives (lib/channels/protocol/stdio.py is the oracle).

use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tofu_channel_stdio::peer::{Handler, HandlerError, Peer, ProtocolError, CLOSED};

struct Remote {
    reader: BufReader<UnixStream>,
    writer: UnixStream,
}

impl Remote {
    fn send(&mut self, line: &str) {
        self.writer.write_all(line.as_bytes()).unwrap();
        self.writer.write_all(b"\n").unwrap();
    }

    fn send_raw(&mut self, bytes: &[u8]) {
        self.writer.write_all(bytes).unwrap();
    }

    fn recv(&mut self) -> String {
        let mut line = String::new();
        self.reader.read_line(&mut line).unwrap();
        assert!(line.ends_with('\n'), "a whole line, got {line:?}");
        line.pop();
        line
    }

    fn hang_up(self) {
        self.writer.shutdown(std::net::Shutdown::Both).unwrap();
    }
}

fn connect(handler: Handler) -> (Peer, Remote) {
    let (local, remote) = UnixStream::pair().unwrap();
    remote
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let peer = Peer::new(local.try_clone().unwrap(), local, handler, "test");
    peer.start();
    let remote = Remote {
        reader: BufReader::new(remote.try_clone().unwrap()),
        writer: remote,
    };
    (peer, remote)
}

fn echo() -> Handler {
    Box::new(|method, params| Ok(json!({"method": method, "params": params})))
}

#[test]
fn malformed_lines_are_answered_with_null_ids() {
    let (_peer, mut remote) = connect(echo());
    remote.send("not json");
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "A line is not JSON"}}"#
    );
    remote.send_raw(b"\xff\xfe{}\n");
    assert!(remote.recv().contains("-32700"));
    remote.send("");
    assert!(remote.recv().contains("-32700"));
    for line in [
        "[1]",
        "{\"id\": 1, \"method\": \"m\"}",
        "{\"jsonrpc\": \"1.0\"}",
        "{\"jsonrpc\": 2.0}",
    ] {
        remote.send(line);
        assert_eq!(
            remote.recv(),
            r#"{"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "Not a JSON-RPC 2.0 object"}}"#,
            "{line}"
        );
    }
}

#[test]
fn requests_are_answered_in_python_bytes() {
    let (_peer, mut remote) = connect(echo());
    remote.send(r#"{"jsonrpc":"2.0","id":"a","method":"m","params":{"t":"é✓","n":1.50,"z":[]}}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": "a", "result": {"method": "m", "params": {"t": "é✓", "n": 1.5, "z": []}}}"#
    );
}

#[test]
fn params_must_be_an_object_and_notifications_are_never_answered() {
    let (_peer, mut remote) = connect(echo());
    remote.send(r#"{"jsonrpc": "2.0", "method": "note", "params": {}}"#);
    remote.send(r#"{"jsonrpc": "2.0", "id": null, "method": "note", "params": []}"#);
    remote.send(r#"{"jsonrpc": "2.0", "id": 4, "method": "m", "params": [1]}"#);
    remote.send(r#"{"jsonrpc": "2.0", "id": 5, "method": "m"}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "params must be an object"}}"#
    );
    assert!(remote
        .recv()
        .starts_with(r#"{"jsonrpc": "2.0", "id": 5, "error": {"code": -32602"#));
}

#[test]
fn handler_errors_become_error_answers() {
    let handler: Handler = Box::new(|method, _params| match method {
        "refuse" => Err(ProtocolError::new(-32004, "The platform failed").into()),
        "refuse-data" => {
            let mut data = Map::new();
            data.insert("code".into(), json!("history_failed"));
            Err(ProtocolError::new(-32004, "with data")
                .with_data(data)
                .into())
        }
        "crash" => Err(HandlerError::Other("KeyError".into())),
        "panic" => panic!("handler bug"),
        _ => Ok(Value::Null),
    });
    let (_peer, mut remote) = connect(handler);
    remote.send(r#"{"jsonrpc": "2.0", "id": 1, "method": "refuse", "params": {}}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": 1, "error": {"code": -32004, "message": "The platform failed"}}"#
    );
    remote.send(r#"{"jsonrpc": "2.0", "id": 2, "method": "refuse-data", "params": {}}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": 2, "error": {"code": -32004, "message": "with data", "data": {"code": "history_failed"}}}"#
    );
    remote.send(r#"{"jsonrpc": "2.0", "id": 3, "method": "crash", "params": {}}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "KeyError"}}"#
    );
    remote.send(r#"{"jsonrpc": "2.0", "id": 4, "method": "panic", "params": {}}"#);
    assert!(remote.recv().contains("-32603"));
    remote.send(r#"{"jsonrpc": "2.0", "id": 5, "method": "ok", "params": {}}"#);
    assert_eq!(
        remote.recv(),
        r#"{"jsonrpc": "2.0", "id": 5, "result": null}"#
    );
}

#[test]
fn messages_are_dispatched_in_arrival_order() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let record = seen.clone();
    let handler: Handler = Box::new(move |method, _params| {
        if method == "slow" {
            thread::sleep(Duration::from_millis(50));
        }
        record.lock().unwrap().push(method.to_string());
        Ok(Value::Null)
    });
    let (_peer, mut remote) = connect(handler);
    for method in ["slow", "a", "b"] {
        remote.send(&format!(
            r#"{{"jsonrpc": "2.0", "method": "{method}", "params": {{}}}}"#
        ));
    }
    remote.send(r#"{"jsonrpc": "2.0", "id": 9, "method": "last", "params": {}}"#);
    remote.recv();
    assert_eq!(*seen.lock().unwrap(), ["slow", "a", "b", "last"]);
}

#[test]
fn requests_count_from_one_and_resolve_by_id() {
    let (peer, mut remote) = connect(echo());
    let peer = Arc::new(peer);
    let (done, results) = mpsc::channel();
    for _ in 0..2 {
        let (peer, done) = (peer.clone(), done.clone());
        thread::spawn(move || {
            done.send(peer.request("send", json!({"text": "ü"})))
                .unwrap()
        });
        // Serialize the writes so the IDs are known.
        let line = remote.recv();
        let message: Value = serde_json::from_str(&line).unwrap();
        let id = message["id"].as_u64().unwrap();
        assert_eq!(
            line,
            format!(
                r#"{{"jsonrpc": "2.0", "id": {id}, "method": "send", "params": {{"text": "ü"}}}}"#
            )
        );
        if id == 1 {
            // Unknown and non-integer IDs (Python: `type(id) is not int`) match nothing.
            remote.send(r#"{"jsonrpc": "2.0", "id": 99, "result": "nobody"}"#);
            remote.send(r#"{"jsonrpc": "2.0", "id": "1", "result": "nobody"}"#);
            remote.send(r#"{"jsonrpc": "2.0", "id": 1.0, "result": "nobody"}"#);
            remote.send(r#"{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}"#);
        } else {
            assert_eq!(id, 2);
            remote.send(r#"{"jsonrpc": "2.0", "id": true, "result": "not an integer"}"#);
            remote.send(r#"{"jsonrpc": "2.0", "id": 2, "error": {"code": null, "data": null}}"#);
        }
        let got = results.recv_timeout(Duration::from_secs(10)).unwrap();
        if id == 1 {
            assert_eq!(got, Ok(json!({"ok": true})));
        } else {
            assert_eq!(got, Err(ProtocolError::new(-32603, "")));
        }
    }
}

#[test]
fn error_answers_follow_python_conversion() {
    let (peer, mut remote) = connect(echo());
    let peer = Arc::new(peer);
    let cases = [
        (
            r#"{"code": -32004, "message": 5, "data": {"code": "x"}}"#,
            -32004,
            "5",
            json!({"code": "x"}),
        ),
        (
            r#"{"code": 0, "message": null, "data": {}}"#,
            -32603,
            "",
            json!({}),
        ),
        (
            r#"{"code": "-32004", "message": "x"}"#,
            -32603,
            MALFORMED,
            json!({}),
        ),
        (
            r#"{"code": 7.9, "message": ""}"#,
            -32603,
            MALFORMED,
            json!({}),
        ),
        (r#"{"code": true}"#, -32603, MALFORMED, json!({})),
        (
            r#"{"code": -32000, "data": []}"#,
            -32603,
            MALFORMED,
            json!({}),
        ),
        (
            r#"{"code": -32000, "data": "text"}"#,
            -32603,
            MALFORMED,
            json!({}),
        ),
    ];
    for (body, code, message, data) in cases {
        let requester = peer.clone();
        let call = thread::spawn(move || requester.request("m", json!({})));
        let id = serde_json::from_str::<Value>(&remote.recv()).unwrap()["id"].clone();
        remote.send(&format!(
            r#"{{"jsonrpc": "2.0", "id": {id}, "error": {body}}}"#
        ));
        let error = call.join().unwrap().unwrap_err();
        assert_eq!(
            (error.code, error.message.as_str()),
            (code, message),
            "{body}"
        );
        assert_eq!(Value::Object(error.data), data, "{body}");
    }
}

const MALFORMED: &str = "The adapter sent a malformed error response";

#[test]
fn malformed_responses_never_close_the_binding() {
    let (peer, mut remote) = connect(echo());
    let peer = Arc::new(peer);
    let requester = peer.clone();
    let call = thread::spawn(move || requester.request("m", json!({})));
    let id = serde_json::from_str::<Value>(&remote.recv()).unwrap()["id"].clone();
    remote.send(&format!(
        r#"{{"jsonrpc": "2.0", "id": {id}, "error": {{"code": "x"}}}}"#
    ));
    remote.send(r#"{"jsonrpc": "2.0", "id": [1], "result": 1}"#);
    assert_eq!(
        call.join().unwrap(),
        Err(ProtocolError::new(-32603, MALFORMED))
    );
    let requester = peer.clone();
    let call = thread::spawn(move || requester.request("m", json!({})));
    let id = serde_json::from_str::<Value>(&remote.recv()).unwrap()["id"].clone();
    remote.send(&format!(
        r#"{{"jsonrpc": "2.0", "id": {id}, "result": "still open"}}"#
    ));
    assert_eq!(call.join().unwrap(), Ok(json!("still open")));
}

#[test]
fn the_end_of_the_stream_fails_pending_requests_and_later_ones() {
    let (peer, mut remote) = connect(echo());
    let peer = Arc::new(peer);
    let requester = peer.clone();
    let call = thread::spawn(move || requester.request("send", json!({})));
    remote.recv();
    remote.hang_up();
    assert_eq!(
        call.join().unwrap(),
        Err(ProtocolError::new(-32603, CLOSED))
    );
    assert_eq!(
        peer.request("send", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
    assert!(peer.wait_closed(Some(Duration::from_secs(5))));
    peer.join(Some(Duration::from_secs(5)));
}

struct FailingWriter;

impl Write for FailingWriter {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::new(io::ErrorKind::BrokenPipe, "gone"))
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn a_failed_write_fails_the_request_and_the_notification() {
    let (local, _remote) = UnixStream::pair().unwrap();
    let peer = Peer::new(local, FailingWriter, echo(), "test");
    peer.start();
    assert_eq!(
        peer.request("send", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
    assert_eq!(
        peer.notify("health", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
}

#[test]
fn a_closed_writer_fails_requests_like_a_closed_file() {
    let (peer, _remote) = connect(echo());
    peer.close_writer();
    assert_eq!(
        peer.request("send", json!({})),
        Err(ProtocolError::new(-32603, CLOSED))
    );
}

#[test]
fn a_nested_call_does_not_deadlock() {
    // The adapter's worker sends `events`; the core answers it only after its own
    // `download` request to the adapter returns (the core's dispatcher calls back).
    let (core_end, adapter_end) = UnixStream::pair().unwrap();
    let adapter = Peer::new(
        adapter_end.try_clone().unwrap(),
        adapter_end,
        Box::new(|method, params| Ok(json!({"answered": method, "params": params}))),
        "adapter",
    );
    let core_slot: Arc<Mutex<Option<tofu_channel_stdio::PeerClient>>> = Arc::new(Mutex::new(None));
    let callback = core_slot.clone();
    let core = Peer::new(
        core_end.try_clone().unwrap(),
        core_end,
        Box::new(move |method, params| {
            assert_eq!(method, "events");
            let client = callback.lock().unwrap().clone().unwrap();
            let download = client.request("download", json!({"for": params["batch"]}))?;
            Ok(json!({"download": download}))
        }),
        "core",
    );
    *core_slot.lock().unwrap() = Some(core.client());
    adapter.start();
    core.start();
    let adapter_client = adapter.client();
    let worker = thread::spawn(move || adapter_client.request("events", json!({"batch": 1})));
    let (done, finished) = mpsc::channel();
    thread::spawn(move || done.send(worker.join().unwrap()).unwrap());
    let answer = finished
        .recv_timeout(Duration::from_secs(10))
        .expect("no deadlock");
    assert_eq!(
        answer,
        Ok(json!({"download": {"answered": "download", "params": {"for": 1}}}))
    );
}
