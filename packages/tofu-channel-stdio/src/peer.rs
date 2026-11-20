//! One JSON-RPC 2.0 endpoint over a pair of byte streams (Python `_Peer`).
//!
//! Lines are `json.dumps(ensure_ascii=False)` bytes plus `\n`, written under one
//! lock. Request IDs count from 1. The reader thread answers malformed lines,
//! resolves pending requests by ID and queues requests and notifications for
//! the dispatcher thread, which runs them in arrival order. A response with a
//! non-integer ID matches nothing; a malformed error response fails its own
//! request with internalError. When the reader ends (EOF or read error), the
//! binding is closed: every pending request fails with internalError
//! "The channel adapter binding closed" and the dispatcher stops after the
//! queued messages.

use std::collections::HashMap;
use std::fmt;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::panic::{self, AssertUnwindSafe};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use serde_json::{Map, Value};

use crate::contract::error_code;
use crate::pyjson::{self, py_str, truthy};
use crate::threads::{self, Joinable};

pub const CLOSED: &str = "The channel adapter binding closed";

/// A protocol violation or refusal with its JSON-RPC code (Python `ProtocolError`).
#[derive(Clone, Debug, PartialEq)]
pub struct ProtocolError {
    pub code: i64,
    pub message: String,
    pub data: Map<String, Value>,
}

impl ProtocolError {
    pub fn new(code: i64, message: impl Into<String>) -> Self {
        ProtocolError {
            code,
            message: message.into(),
            data: Map::new(),
        }
    }

    pub fn with_data(mut self, data: Map<String, Value>) -> Self {
        self.data = data;
        self
    }

    pub fn closed() -> Self {
        ProtocolError::new(error_code("internalError"), CLOSED)
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ProtocolError {}

/// What a handler raises: a `ProtocolError` is answered as is; any other
/// failure is answered internalError with its type name as the message.
#[derive(Clone, Debug, PartialEq)]
pub enum HandlerError {
    Protocol(ProtocolError),
    Other(String),
}

impl From<ProtocolError> for HandlerError {
    fn from(error: ProtocolError) -> Self {
        HandlerError::Protocol(error)
    }
}

pub type HandlerResult = Result<Value, HandlerError>;
pub type Handler = Box<dyn Fn(&str, Map<String, Value>) -> HandlerResult + Send>;
/// Sees each incoming request/notification on the reader thread, before the
/// dispatcher; returning true consumes it (a binding's own control messages
/// that must take effect in line order).
pub type ReaderHook = Box<dyn Fn(&Map<String, Value>) -> bool + Send>;

type Reply = Result<Value, ProtocolError>;

struct State {
    closed: bool,
    next_id: u64,
    pending: HashMap<u64, Sender<Reply>>,
}

struct Shared {
    state: Mutex<State>,
    writer: Mutex<Option<Box<dyn Write + Send>>>,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poison| poison.into_inner())
}

impl Shared {
    fn write(&self, message: &Value) -> io::Result<()> {
        let mut data = pyjson::dumps(message).into_bytes();
        data.push(b'\n');
        let mut writer = lock(&self.writer);
        let Some(writer) = writer.as_mut() else {
            return Err(io::Error::new(
                io::ErrorKind::BrokenPipe,
                "write to closed file",
            ));
        };
        writer.write_all(&data)?;
        writer.flush()
    }

    fn answer(&self, id: Value, outcome: Result<Value, ProtocolError>) {
        let mut message = Map::new();
        message.insert("jsonrpc".into(), "2.0".into());
        message.insert("id".into(), id);
        match outcome {
            Ok(result) => {
                message.insert("result".into(), result);
            }
            Err(error) => {
                let mut body = Map::new();
                body.insert("code".into(), error.code.into());
                body.insert("message".into(), error.message.into());
                if !error.data.is_empty() {
                    body.insert("data".into(), Value::Object(error.data));
                }
                message.insert("error".into(), Value::Object(body));
            }
        }
        // The other side is gone; its pending request fails with the binding.
        let _ = self.write(&Value::Object(message));
    }

    fn request(&self, method: &str, params: Value) -> Reply {
        let (sender, receiver) = mpsc::channel();
        let id = {
            let mut state = lock(&self.state);
            if state.closed {
                return Err(ProtocolError::closed());
            }
            let id = state.next_id;
            state.next_id += 1;
            state.pending.insert(id, sender);
            id
        };
        let mut message = Map::new();
        message.insert("jsonrpc".into(), "2.0".into());
        message.insert("id".into(), id.into());
        message.insert("method".into(), method.into());
        message.insert("params".into(), params);
        if self.write(&Value::Object(message)).is_err() {
            lock(&self.state).pending.remove(&id);
            return Err(ProtocolError::closed());
        }
        receiver
            .recv()
            .unwrap_or_else(|_| Err(ProtocolError::closed()))
    }

    fn notify(&self, method: &str, params: Value) -> Result<(), ProtocolError> {
        let mut message = Map::new();
        message.insert("jsonrpc".into(), "2.0".into());
        message.insert("method".into(), method.into());
        message.insert("params".into(), params);
        self.write(&Value::Object(message))
            .map_err(|_| ProtocolError::closed())
    }
}

/// A cloneable handle that sends requests and notifications on a peer
/// (an adapter's `CoreLink`, a forwarder to another binding).
#[derive(Clone)]
pub struct PeerClient(Arc<Shared>);

impl PeerClient {
    pub fn request(&self, method: &str, params: Value) -> Reply {
        self.0.request(method, params)
    }

    pub fn notify(&self, method: &str, params: Value) -> Result<(), ProtocolError> {
        self.0.notify(method, params)
    }
}

struct Unstarted {
    reader: Box<dyn Read + Send>,
    handler: Handler,
    hook: Option<ReaderHook>,
}

pub struct Peer {
    shared: Arc<Shared>,
    name: String,
    unstarted: Mutex<Option<Unstarted>>,
    threads: Mutex<Vec<Arc<Joinable>>>,
}

impl Peer {
    pub fn new(
        reader: impl Read + Send + 'static,
        writer: impl Write + Send + 'static,
        handler: Handler,
        name: &str,
    ) -> Peer {
        Peer {
            shared: Arc::new(Shared {
                state: Mutex::new(State {
                    closed: false,
                    next_id: 1,
                    pending: HashMap::new(),
                }),
                writer: Mutex::new(Some(Box::new(writer))),
            }),
            name: name.to_string(),
            unstarted: Mutex::new(Some(Unstarted {
                reader: Box::new(reader),
                handler,
                hook: None,
            })),
            threads: Mutex::new(Vec::new()),
        }
    }

    /// Install a [`ReaderHook`]; only before `start`.
    pub fn with_reader_hook(self, hook: ReaderHook) -> Peer {
        if let Some(unstarted) = lock(&self.unstarted).as_mut() {
            unstarted.hook = Some(hook);
        }
        self
    }

    pub fn client(&self) -> PeerClient {
        PeerClient(self.shared.clone())
    }

    pub fn start(&self) {
        let Some(Unstarted {
            reader,
            handler,
            hook,
        }) = lock(&self.unstarted).take()
        else {
            return;
        };
        let (inbox, queue) = mpsc::channel();
        let shared = self.shared.clone();
        let reader_thread = threads::spawn(&format!("{}-reader", self.name), move || {
            read_lines(&shared, reader, hook, &inbox)
        });
        let shared = self.shared.clone();
        let dispatch_thread = threads::spawn(&format!("{}-dispatch", self.name), move || {
            dispatch(&shared, handler, queue)
        });
        lock(&self.threads).extend([Arc::new(reader_thread), Arc::new(dispatch_thread)]);
    }

    /// Join the reader, then the dispatcher, each up to `timeout` (`None`: no deadline).
    pub fn join(&self, timeout: Option<Duration>) {
        let threads: Vec<Arc<Joinable>> = lock(&self.threads).clone();
        for thread in threads {
            if !thread.is_current() {
                thread.join(timeout);
            }
        }
    }

    /// Wait until the reader ended (the binding is closed); true when it did.
    pub fn wait_closed(&self, timeout: Option<Duration>) -> bool {
        let reader = lock(&self.threads).first().cloned();
        match reader {
            Some(reader) => reader.join(timeout),
            None => lock(&self.shared.state).closed,
        }
    }

    /// Close the writing stream (Python closes the child's stdin under the peer);
    /// later writes fail as on a closed file.
    pub fn close_writer(&self) {
        lock(&self.shared.writer).take();
    }

    pub fn request(&self, method: &str, params: Value) -> Reply {
        self.shared.request(method, params)
    }

    pub fn notify(&self, method: &str, params: Value) -> Result<(), ProtocolError> {
        self.shared.notify(method, params)
    }
}

fn read_lines(
    shared: &Shared,
    reader: Box<dyn Read + Send>,
    hook: Option<ReaderHook>,
    inbox: &Sender<Option<Map<String, Value>>>,
) {
    let mut reader = BufReader::new(reader);
    let mut raw = Vec::new();
    loop {
        raw.clear();
        match reader.read_until(b'\n', &mut raw) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        read_line(shared, &raw, hook.as_ref(), inbox);
    }
    let pending: Vec<Sender<Reply>> = {
        let mut state = lock(&shared.state);
        state.closed = true;
        state.pending.drain().map(|(_, sender)| sender).collect()
    };
    for sender in pending {
        let _ = sender.send(Err(ProtocolError::closed()));
    }
    let _ = inbox.send(None);
}

fn read_line(
    shared: &Shared,
    raw: &[u8],
    hook: Option<&ReaderHook>,
    inbox: &Sender<Option<Map<String, Value>>>,
) {
    let Some(message) = pyjson::loads(raw) else {
        shared.answer(
            Value::Null,
            Err(ProtocolError::new(
                error_code("parseError"),
                "A line is not JSON",
            )),
        );
        return;
    };
    let message = match message {
        Value::Object(map) if map.get("jsonrpc") == Some(&Value::String("2.0".into())) => map,
        _ => {
            shared.answer(
                Value::Null,
                Err(ProtocolError::new(
                    error_code("invalidRequest"),
                    "Not a JSON-RPC 2.0 object",
                )),
            );
            return;
        }
    };
    if message.contains_key("method") {
        if !hook.is_some_and(|hook| hook(&message)) {
            let _ = inbox.send(Some(message));
        }
        return;
    }
    // Python: `type(request_id) is int`; this endpoint issues integer IDs only.
    let Some(key) = message
        .get("id")
        .and_then(json_int)
        .and_then(|id| u64::try_from(id).ok())
    else {
        return;
    };
    let Some(sender) = lock(&shared.state).pending.remove(&key) else {
        return;
    };
    let reply = match message.get("error") {
        Some(Value::Object(error)) => Err(error_from_answer(error)),
        _ => Ok(message.get("result").cloned().unwrap_or(Value::Null)),
    };
    let _ = sender.send(reply);
}

/// A JSON integer token (Python `type(value) is int`); floats and booleans are not.
fn json_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) if !number.is_f64() => number.as_i64(),
        _ => None,
    }
}

/// Python `_response_error`: `code` None or an int (falsy: internalError),
/// `data` None or an object; anything else is a malformed error response.
fn error_from_answer(error: &Map<String, Value>) -> ProtocolError {
    let code = match error.get("code") {
        None | Some(Value::Null) => Some(0),
        Some(code) => json_int(code),
    };
    let data = match error.get("data") {
        None | Some(Value::Null) => Some(Map::new()),
        Some(Value::Object(data)) => Some(data.clone()),
        Some(_) => None,
    };
    let (Some(code), Some(data)) = (code, data) else {
        return ProtocolError::new(
            error_code("internalError"),
            "The adapter sent a malformed error response",
        );
    };
    let message = match error.get("message") {
        Some(message) if truthy(message) => py_str(message),
        _ => String::new(),
    };
    ProtocolError {
        code: if code == 0 {
            error_code("internalError")
        } else {
            code
        },
        message,
        data,
    }
}

fn dispatch(shared: &Shared, handler: Handler, queue: Receiver<Option<Map<String, Value>>>) {
    while let Ok(Some(message)) = queue.recv() {
        let id = message.get("id").cloned().unwrap_or(Value::Null);
        let outcome = match message.get("params") {
            Some(Value::Object(params)) => {
                let method = py_str(&message["method"]);
                let params = params.clone();
                panic::catch_unwind(AssertUnwindSafe(|| handler(&method, params)))
                    .unwrap_or_else(|_| Err(HandlerError::Other("RustPanic".into())))
            }
            _ => Err(HandlerError::Protocol(ProtocolError::new(
                error_code("invalidParams"),
                "params must be an object",
            ))),
        };
        if id.is_null() {
            continue;
        }
        let outcome = outcome.map_err(|error| match error {
            HandlerError::Protocol(error) => error,
            HandlerError::Other(name) => ProtocolError::new(error_code("internalError"), name),
        });
        shared.answer(id, outcome);
    }
}
