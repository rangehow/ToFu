//! `serve`: the adapter side, mirroring tests/channels/conformance/
//! test_protocol_contract.py::test_a_stdio_adapter_answers_malformed_lines_and_unknown_methods.

use std::collections::HashMap;
use std::io::{self, BufRead, BufReader, Cursor, Write};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tofu_channel_stdio::serve::{serve, CoreProxy, ExtraMethod, ProtocolAdapter};

#[derive(Clone, Default)]
struct Output(Arc<Mutex<Vec<u8>>>);

impl Write for Output {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(data);
        Ok(data.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[derive(Default)]
struct Bare {
    core: Mutex<Option<CoreProxy>>,
}

impl ProtocolAdapter for Bare {
    fn attach(&self, core: CoreProxy) {
        *self.core.lock().unwrap() = Some(core);
    }

    fn initialize(
        &self,
        _params: Map<String, Value>,
    ) -> Result<Value, tofu_channel_stdio::HandlerError> {
        Ok(json!({"protocolVersion": 1}))
    }
}

fn run(input: &str, extra: HashMap<String, ExtraMethod>) -> Vec<Value> {
    let output = Output::default();
    let adapter = Arc::new(Bare::default());
    let code = serve(
        adapter,
        Cursor::new(input.as_bytes().to_vec()),
        output.clone(),
        extra,
    );
    assert_eq!(code, 0);
    let bytes = output.0.lock().unwrap().clone();
    String::from_utf8(bytes)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn a_stdio_adapter_answers_malformed_lines_and_unknown_methods() {
    let answers = run(
        "not json\n{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"nope\", \"params\": {}}\n",
        HashMap::new(),
    );
    assert_eq!(
        answers,
        [
            json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "A line is not JSON"}}),
            json!({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "The adapter has no method 'nope'"}}),
        ]
    );
}

#[test]
fn core_to_adapter_methods_reach_the_adapter_with_the_base_defaults() {
    let mut extra: HashMap<String, ExtraMethod> = HashMap::new();
    extra.insert(
        "test/echo".into(),
        Box::new(|params| Ok(Value::Object(params))),
    );
    let lines = [
        r#"{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}"#,
        r#"{"jsonrpc": "2.0", "id": 2, "method": "history", "params": {}}"#,
        r#"{"jsonrpc": "2.0", "id": 3, "method": "send", "params": {}}"#,
        r#"{"jsonrpc": "2.0", "id": 4, "method": "shutdown", "params": {}}"#,
        r#"{"jsonrpc": "2.0", "id": 5, "method": "test/echo", "params": {"a": 1}}"#,
        // adapter_to_core methods are not the adapter's
        r#"{"jsonrpc": "2.0", "id": 6, "method": "events", "params": {}}"#,
    ];
    let answers = run(&(lines.join("\n") + "\n"), extra);
    let results: Vec<Value> = answers
        .iter()
        .map(|answer| {
            answer
                .get("result")
                .cloned()
                .unwrap_or(answer["error"].clone())
        })
        .collect();
    assert_eq!(
        results,
        [
            json!({"protocolVersion": 1}),
            json!({"code": -32003, "message": "This adapter does not implement history"}),
            json!({"code": -32603, "message": "NotImplementedError"}),
            json!({}),
            json!({"a": 1}),
            json!({"code": -32601, "message": "The adapter has no method 'events'"}),
        ]
    );
}

#[test]
fn the_core_proxy_sends_events_requests_and_health_notifications() {
    let (adapter_end, core_end) = UnixStream::pair().unwrap();
    core_end
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let adapter = Arc::new(Bare::default());
    let served = adapter.clone();
    let server = thread::spawn(move || {
        serve(
            served,
            adapter_end.try_clone().unwrap(),
            adapter_end,
            HashMap::new(),
        )
    });
    let mut core = BufReader::new(core_end.try_clone().unwrap());
    let mut writer = core_end;
    while adapter.core.lock().unwrap().is_none() {
        thread::yield_now();
    }
    let proxy = adapter.core.lock().unwrap().clone().unwrap();
    proxy.health(json!({"state": "connected"})).unwrap();
    let delivery = thread::spawn(move || proxy.events(json!({"events": []})));
    let mut line = String::new();
    core.read_line(&mut line).unwrap();
    assert_eq!(
        line,
        "{\"jsonrpc\": \"2.0\", \"method\": \"health\", \"params\": {\"state\": \"connected\"}}\n"
    );
    line.clear();
    core.read_line(&mut line).unwrap();
    assert_eq!(
        line,
        "{\"jsonrpc\": \"2.0\", \"id\": 1, \"method\": \"events\", \"params\": {\"events\": []}}\n"
    );
    writer
        .write_all(b"{\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {}}\n")
        .unwrap();
    assert_eq!(delivery.join().unwrap(), Ok(json!({})));
    writer.shutdown(std::net::Shutdown::Both).unwrap();
    assert_eq!(server.join().unwrap(), 0);
}
