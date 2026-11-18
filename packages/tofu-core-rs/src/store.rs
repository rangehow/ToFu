//! TofuDB storage.v2 client over the engine's own framing types
//! (`tofu_db::protocol`, `tofu_db::daemon::derive_auth_token`).
//!
//! One authenticated connection per session; requests are JSON payloads in
//! MessagePack frames exactly as `tofu_db/wire.py` sends them. `query_many`
//! pipelines several single-frame reads in one exchange like
//! `StorageV2Session.request_many`. The client retry/backoff policy of
//! `lib/storage/v2_client.py` is not ported: any failure is returned.

use std::io::{self, BufReader, BufWriter, Write};
use std::net::TcpStream;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;
use tofu_db::protocol::{read_frame, write_frame, Hello, Message, Request, PROTOCOL_VERSION};

/// The fields of a `tofu.storage-connection/v2` document the client needs.
#[derive(Clone)]
pub struct ConnectionInfo {
    pub host: String,
    pub port: u16,
    pub secret: String,
    pub owner_id: u64,
    pub tenant_id: Option<u64>,
}

impl ConnectionInfo {
    pub fn from_file(path: &Path) -> io::Result<Self> {
        let document: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        let field = |name: &str| {
            document.get(name).cloned().ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("connection document lacks {name}"),
                )
            })
        };
        Ok(Self {
            host: field("host")?.as_str().unwrap_or("127.0.0.1").to_owned(),
            port: field("port")?.as_u64().unwrap_or(0) as u16,
            secret: field("token")?.as_str().unwrap_or("").to_owned(),
            owner_id: field("ownerId")?.as_u64().unwrap_or(1),
            tenant_id: document.get("tenantId").and_then(Value::as_u64),
        })
    }
}

#[derive(Debug)]
pub struct StorageError {
    pub code: String,
    pub message: String,
}

impl std::fmt::Display for StorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for StorageError {}

fn io_error(error: io::Error) -> StorageError {
    StorageError {
        code: "transport".to_owned(),
        message: error.to_string(),
    }
}

pub type StorageResult<T> = Result<T, StorageError>;

pub struct Client {
    reader: BufReader<TcpStream>,
    writer: BufWriter<TcpStream>,
    owner_id: u64,
    tenant_id: Option<u64>,
    schema_id: u32,
    counter: u32,
    seed: [u8; 8],
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Unwrap the private committed-events envelope like `StorageV2Response.value`.
fn semantic_value(value: Value) -> Value {
    match value {
        Value::Object(mut map) if map.contains_key("_storageCommitContract") => {
            map.remove("value").unwrap_or(Value::Null)
        }
        other => other,
    }
}

impl Client {
    pub fn connect(info: &ConnectionInfo, schema_id: u32) -> StorageResult<Self> {
        let token = tofu_db::daemon::derive_auth_token(info.secret.as_bytes()).map_err(io_error)?;
        let stream = TcpStream::connect((info.host.as_str(), info.port)).map_err(io_error)?;
        stream.set_nodelay(true).map_err(io_error)?;
        let mut seed = [0u8; 8];
        seed.copy_from_slice(&crate::round::random_bytes(8));
        let mut client = Self {
            reader: BufReader::with_capacity(256 * 1024, stream.try_clone().map_err(io_error)?),
            writer: BufWriter::with_capacity(256 * 1024, stream),
            owner_id: info.owner_id,
            tenant_id: info.tenant_id,
            schema_id,
            counter: 0,
            seed,
        };
        let correlation_id = client.next_correlation();
        let hello = Message::Hello(Hello {
            correlation_id,
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: vec![schema_id],
            auth_token: token,
        });
        write_frame(&mut client.writer, &hello).map_err(io_error)?;
        client.writer.flush().map_err(io_error)?;
        match read_frame(&mut client.reader).map_err(io_error)? {
            Message::Response(response)
                if response.correlation_id == correlation_id && response.status == 0 =>
            {
                client.schema_id = response.schema_id;
                Ok(client)
            }
            Message::Response(response) => Err(StorageError {
                code: response
                    .error_code
                    .unwrap_or_else(|| "hello_refused".to_owned()),
                message: response.error_message.unwrap_or_default(),
            }),
            _ => Err(StorageError {
                code: "protocol".to_owned(),
                message: "hello answered by a non-response".to_owned(),
            }),
        }
    }

    fn next_correlation(&mut self) -> [u8; 16] {
        self.counter = self.counter.wrapping_add(1).max(1);
        let mut id = [0u8; 16];
        id[..8].copy_from_slice(&self.seed);
        id[8..12].copy_from_slice(&self.counter.to_be_bytes());
        id[12] = 1;
        id
    }

    fn send(
        &mut self,
        operation: &str,
        payload: &Value,
        command_id: Option<&str>,
    ) -> StorageResult<[u8; 16]> {
        let correlation_id = self.next_correlation();
        let bytes = serde_json::to_vec(payload).expect("JSON payload encodes");
        if bytes.len() > tofu_db::protocol::MAX_PAYLOAD_BYTES {
            return Err(StorageError {
                code: "storage_payload_too_large".to_owned(),
                message: format!(
                    "{operation} payload of {} bytes needs streamed chunks (not ported)",
                    bytes.len()
                ),
            });
        }
        let request = Message::Request(Request {
            correlation_id,
            deadline_unix_ms: now_ms() + 120_000,
            owner_id: self.owner_id,
            tenant_id: self.tenant_id,
            command_id: command_id.map(str::to_owned),
            schema_id: self.schema_id,
            operation: operation.to_owned(),
            payload: bytes,
        });
        write_frame(&mut self.writer, &request).map_err(io_error)?;
        Ok(correlation_id)
    }

    fn receive(&mut self, correlation_id: [u8; 16]) -> StorageResult<Value> {
        let mut streamed: Vec<u8> = Vec::new();
        loop {
            match read_frame(&mut self.reader).map_err(io_error)? {
                Message::ResponseChunk(chunk) if chunk.correlation_id == correlation_id => {
                    streamed.extend_from_slice(&chunk.payload);
                }
                Message::Response(response) if response.correlation_id == correlation_id => {
                    if response.status != 0 {
                        return Err(StorageError {
                            code: response.error_code.unwrap_or_else(|| "unknown".to_owned()),
                            message: response.error_message.unwrap_or_default(),
                        });
                    }
                    let payload = if streamed.is_empty() {
                        response.payload
                    } else {
                        std::mem::take(&mut streamed)
                    };
                    if payload.is_empty() {
                        return Ok(Value::Null);
                    }
                    let value: Value =
                        serde_json::from_slice(&payload).map_err(|error| StorageError {
                            code: "protocol".to_owned(),
                            message: error.to_string(),
                        })?;
                    return Ok(semantic_value(value));
                }
                _ => {
                    return Err(StorageError {
                        code: "protocol".to_owned(),
                        message: "response correlation mismatch".to_owned(),
                    })
                }
            }
        }
    }

    /// One operation; `command_id` makes a command idempotent.
    pub fn call(
        &mut self,
        operation: &str,
        payload: &Value,
        command_id: Option<&str>,
    ) -> StorageResult<Value> {
        let correlation_id = self.send(operation, payload, command_id)?;
        self.writer.flush().map_err(io_error)?;
        self.receive(correlation_id)
    }

    /// Several reads pipelined in one exchange.
    pub fn query_many(&mut self, requests: &[(&str, Value)]) -> StorageResult<Vec<Value>> {
        let mut correlations = Vec::with_capacity(requests.len());
        for (operation, payload) in requests {
            correlations.push(self.send(operation, payload, None)?);
        }
        self.writer.flush().map_err(io_error)?;
        correlations
            .into_iter()
            .map(|id| self.receive(id))
            .collect()
    }
}
