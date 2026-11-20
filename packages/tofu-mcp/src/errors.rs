//! Call failure categories (`lib/mcp/client/_errors.py` and the relay's
//! `_error_kind`, the Python oracles).
//!
//! Classification is by structured code first: `-32000` (connection closed)
//! is a dead transport, `-32001` (request timeout) a call timeout; any other
//! JSON-RPC error response proves the peer is alive.

use serde::Serialize;

pub const MCP_CONNECTION_CLOSED: i64 = -32000;
pub const MCP_REQUEST_TIMEOUT: i64 = -32001;
pub const JSONRPC_METHOD_NOT_FOUND: i64 = -32601;
/// Child stderr kept for connect diagnostics.
pub const STDERR_TAIL_BYTES: usize = 8192;

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", content = "message", rename_all = "lowercase")]
pub enum CallError {
    /// The owner's cancel signal stopped the call (never a health failure).
    Cancelled(String),
    /// The call exceeded its deadline (outer budget, or the peer's -32001).
    Timeout(String),
    /// The transport died: no answer proves the outcome.
    Transport(String),
    /// A caller error before anything was sent (`ValueError` in the oracle).
    Invalid(String),
    /// The peer answered with a JSON-RPC error.
    Peer { code: i64, message: String },
    /// Any other failure.
    Error(String),
    /// The binding has no handler for a host-to-binding call (`callback.rs`).
    Unavailable(String),
}

impl CallError {
    /// The relay's `_error_kind`.
    pub fn kind(&self) -> &'static str {
        match self {
            CallError::Cancelled(_) => "cancelled",
            CallError::Timeout(_) => "timeout",
            CallError::Transport(_) => "transport",
            CallError::Invalid(_) => "invalid",
            CallError::Peer { code, .. } if *code == MCP_REQUEST_TIMEOUT => "timeout",
            CallError::Peer { code, .. } if *code == MCP_CONNECTION_CLOSED => "transport",
            CallError::Peer { .. } | CallError::Error(_) | CallError::Unavailable(_) => "error",
        }
    }

    pub fn message(&self) -> &str {
        match self {
            CallError::Cancelled(m)
            | CallError::Timeout(m)
            | CallError::Transport(m)
            | CallError::Invalid(m)
            | CallError::Error(m)
            | CallError::Unavailable(m)
            | CallError::Peer { message: m, .. } => m,
        }
    }

    /// `_is_transport_dead_error`.
    pub fn is_transport_dead(&self) -> bool {
        self.kind() == "transport"
    }

    /// `_is_call_timeout_error`.
    pub fn is_call_timeout(&self) -> bool {
        self.kind() == "timeout"
    }

    /// `_is_peer_answered_error`: a JSON-RPC error response is proof of liveness.
    pub fn is_peer_answered(&self) -> bool {
        matches!(self, CallError::Peer { .. })
            && !self.is_call_timeout()
            && !self.is_transport_dead()
    }

    /// `_is_method_not_found`.
    pub fn is_method_not_found(&self) -> bool {
        matches!(self, CallError::Peer { code, .. } if *code == JSONRPC_METHOD_NOT_FOUND)
    }

    /// Rebuild from a relayed `{kind, message}` (`_raise_error`).
    pub fn from_relay(kind: &str, message: String) -> CallError {
        match kind {
            "cancelled" => CallError::Cancelled(message),
            "timeout" => CallError::Timeout(message),
            "transport" => CallError::Transport(message),
            "invalid" => CallError::Invalid(message),
            _ => CallError::Error(message),
        }
    }

    /// A binding's `{kind, message}` answer to a host-to-binding call.
    pub fn from_kind(kind: &str, message: &str) -> CallError {
        match kind {
            "unavailable" => CallError::Unavailable(message.to_string()),
            _ => CallError::from_relay(kind, message.to_string()),
        }
    }
}

impl std::fmt::Display for CallError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}

impl From<rmcp::ServiceError> for CallError {
    fn from(error: rmcp::ServiceError) -> Self {
        use rmcp::ServiceError as E;
        match error {
            E::McpError(data) => {
                let code = i64::from(data.code.0);
                let message = crate::transport::scrub_text(&data.message);
                match code {
                    MCP_CONNECTION_CLOSED => CallError::Transport(message),
                    MCP_REQUEST_TIMEOUT => CallError::Timeout(message),
                    _ => CallError::Peer { code, message },
                }
            }
            E::TransportClosed => CallError::Transport("Connection closed".into()),
            E::TransportSend(inner) => CallError::Transport(crate::transport::scrub_text(
                &format!("transport closed: {inner}"),
            )),
            E::Timeout { timeout } => CallError::Timeout(format!(
                "Timed out while waiting for response after {}s",
                timeout.as_secs_f64()
            )),
            E::Cancelled { reason } => {
                CallError::Cancelled(reason.unwrap_or_else(|| "MCP call cancelled".into()))
            }
            other => CallError::Error(crate::transport::scrub_text(&other.to_string())),
        }
    }
}

/// `MCPConnectError`: the user-facing connect failure, credentials scrubbed last.
pub fn connect_error_message(server_name: &str, cause: &str, stderr_tail: &str) -> String {
    let mut cause = cause.trim().to_string();
    if cause == "Connection closed" {
        cause = "Connection closed by server during initialize \
                 (the launcher started but exited before completing the MCP handshake)"
            .into();
    }
    let mut message = format!(
        "MCP server {}: {cause}",
        crate::pyjson::py_repr(server_name)
    );
    if !stderr_tail.is_empty() {
        let chars = stderr_tail.chars().count();
        let tail = if chars > 1500 {
            format!(
                "…{}",
                stderr_tail.chars().skip(chars - 1500).collect::<String>()
            )
        } else {
            stderr_tail.to_string()
        };
        message.push_str(&format!("\n\nServer stderr (tail):\n{tail}"));
    }
    crate::transport::scrub_text(&message)
}

/// `_read_stderr_tail` over a byte ring: decode lossily, drop a partial first line, strip.
pub fn stderr_tail_text(bytes: &[u8], truncated: bool) -> String {
    let text = String::from_utf8_lossy(bytes).into_owned();
    let text = if truncated {
        match text.split_once('\n') {
            Some((_, rest)) => rest.to_string(),
            None => text,
        }
    } else {
        text
    };
    crate::pyjson::py_strip(&text).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn categories_follow_codes() {
        assert!(CallError::Peer {
            code: -32000,
            message: "x".into()
        }
        .is_transport_dead());
        assert!(CallError::Peer {
            code: -32001,
            message: "x".into()
        }
        .is_call_timeout());
        let alive = CallError::Peer {
            code: -32601,
            message: "Method not found".into(),
        };
        assert!(alive.is_peer_answered() && alive.is_method_not_found() && alive.kind() == "error");
        assert_eq!(CallError::Invalid("bad".into()).kind(), "invalid");
        let message =
            connect_error_message("amap", "Connection closed", "GET https://h/mcp?key=SECRET");
        assert!(
            message.contains("before completing the MCP handshake") && message.contains("key=***")
        );
        assert!(!message.contains("SECRET"));
        assert_eq!(stderr_tail_text(b"partial\nline two\n", true), "line two");
    }
}
