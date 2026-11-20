//! Adapter side of the stdio binding (Python `serve` and `_CoreProxy`): run a
//! [`ProtocolAdapter`] on a pair of streams until the input closes.
//!
//! The output carries protocol lines only, so an adapter logs to stderr.
//! Handlers run on the dispatcher thread and must not wait on a call to the
//! core there; an adapter delivers events from its own thread.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::Arc;

use serde_json::{Map, Value};

use crate::contract::{self, error_code, Direction};
use crate::peer::{HandlerError, HandlerResult, Peer, PeerClient, ProtocolError};
use crate::pyjson::py_repr_str;

type Params = Map<String, Value>;

/// Python `capability_missing(method)`.
pub fn capability_missing(method: &str) -> HandlerError {
    ProtocolError::new(
        error_code("capabilityMissing"),
        format!("This adapter does not implement {method}"),
    )
    .into()
}

fn not_implemented() -> HandlerError {
    HandlerError::Other("NotImplementedError".into())
}

/// The core-to-adapter methods (Python `ProtocolAdapter`), with the base
/// class's defaults: optional methods refuse with capabilityMissing, required
/// ones fail as NotImplementedError, `shutdown` answers `{}`.
pub trait ProtocolAdapter: Send + Sync {
    fn attach(&self, core: CoreProxy);
    fn initialize(&self, params: Params) -> HandlerResult;
    fn start(&self, _params: Params) -> HandlerResult {
        Err(not_implemented())
    }
    fn identity(&self, _params: Params) -> HandlerResult {
        Err(capability_missing("identity"))
    }
    fn send(&self, _intent: Params) -> HandlerResult {
        Err(not_implemented())
    }
    fn lookup(&self, _query: Params) -> HandlerResult {
        Err(capability_missing("lookup"))
    }
    fn history(&self, _query: Params) -> HandlerResult {
        Err(capability_missing("history"))
    }
    fn message(&self, _query: Params) -> HandlerResult {
        Err(capability_missing("message"))
    }
    fn threads(&self, _query: Params) -> HandlerResult {
        Err(capability_missing("threads"))
    }
    fn download(&self, _request: Params) -> HandlerResult {
        Err(capability_missing("download"))
    }
    fn shutdown(&self, _params: Params) -> HandlerResult {
        Ok(Value::Object(Map::new()))
    }
}

/// The adapter's `CoreLink` over the stdio binding.
#[derive(Clone)]
pub struct CoreProxy {
    client: PeerClient,
}

impl CoreProxy {
    pub fn events(&self, batch: Value) -> Result<Value, ProtocolError> {
        self.client.request("events", batch)
    }

    pub fn health(&self, health: Value) -> Result<(), ProtocolError> {
        self.client.notify("health", health)
    }
}

/// Further core-to-adapter requests (a test platform's controls).
pub type ExtraMethod = Box<dyn Fn(Params) -> HandlerResult + Send + Sync>;

fn call(adapter: &dyn ProtocolAdapter, method: &str, params: Params) -> HandlerResult {
    match method {
        "initialize" => adapter.initialize(params),
        "start" => adapter.start(params),
        "identity" => adapter.identity(params),
        "send" => adapter.send(params),
        "lookup" => adapter.lookup(params),
        "history" => adapter.history(params),
        "message" => adapter.message(params),
        "threads" => adapter.threads(params),
        "download" => adapter.download(params),
        "shutdown" => adapter.shutdown(params),
        // A contract method this trait does not port yet: Python's getattr raises AttributeError.
        _ => Err(HandlerError::Other("AttributeError".into())),
    }
}

/// Run `adapter` on `input`/`output` until `input` closes; returns 0.
pub fn serve(
    adapter: Arc<dyn ProtocolAdapter>,
    input: impl Read + Send + 'static,
    output: impl Write + Send + 'static,
    extra_methods: HashMap<String, ExtraMethod>,
) -> i32 {
    let handled = adapter.clone();
    let handler = move |method: &str, params: Params| {
        if contract::method(method).is_some_and(|spec| spec.direction == Direction::CoreToAdapter) {
            return call(handled.as_ref(), method, params);
        }
        if let Some(extra) = extra_methods.get(method) {
            return extra(params);
        }
        Err(ProtocolError::new(
            error_code("methodNotFound"),
            format!("The adapter has no method {}", py_repr_str(method)),
        )
        .into())
    };
    let peer = Peer::new(input, output, Box::new(handler), "channel-adapter-serve");
    adapter.attach(CoreProxy {
        client: peer.client(),
    });
    peer.start();
    peer.join(None);
    0
}

/// [`serve`] on the process's stdin and stdout.
pub fn serve_stdio(
    adapter: Arc<dyn ProtocolAdapter>,
    extra_methods: HashMap<String, ExtraMethod>,
) -> i32 {
    serve(adapter, std::io::stdin(), std::io::stdout(), extra_methods)
}
