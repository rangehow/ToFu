//! One authenticated storage.v2 connection (`tofu_db/wire.py::StorageV2Session`).
//!
//! Frames and message types are the engine's own (`tofu_db::protocol`); this
//! module owns the client half of the session contract: Hello exactly once,
//! exact correlation/schema checks, bounded request upload streams for
//! payloads larger than one frame, strict response-chunk reassembly, and
//! pipelined reads in send-buffer segments. It holds no retry policy.

use std::io::{self, BufReader, Write};
use std::net::{Shutdown, SocketAddr, TcpStream, ToSocketAddrs};
use std::time::Duration;

use serde_json::{Map, Value};
use tofu_db::protocol::{
    read_frame_or_eof_with_admission, write_frame, BlobChunk, Hello, Message, Request, Response,
    MAX_BLOB_CHUNK_BYTES, MAX_PAYLOAD_BYTES, PROTOCOL_VERSION,
};

/// A transport-level outcome, classified by the client (`wire.py` exception types).
#[derive(Debug)]
pub enum TransportError {
    /// `StorageV2Error`: a classified failure response (here: a refused Hello).
    Wire {
        code: String,
        message: String,
        retryable: bool,
    },
    /// The peer closed at a frame boundary: the parser is still aligned.
    ConnectionClosed,
    /// The peer violated framing, checksum or session rules.
    Protocol(String),
    /// Socket I/O failed or timed out.
    Io,
    /// A local encoding failure before or while building a frame.
    Codec(String),
}

impl From<io::Error> for TransportError {
    fn from(_error: io::Error) -> Self {
        TransportError::Io
    }
}

pub type TransportResult<T> = Result<T, TransportError>;

/// One decoded response, success or classified failure.
#[derive(Debug)]
pub struct WireResponse {
    pub status: u16,
    pub payload: Vec<u8>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub retryable: bool,
}

impl WireResponse {
    /// The decoded JSON payload; an empty payload is `null`.
    pub fn json(&self) -> Result<Value, String> {
        if self.payload.is_empty() {
            return Ok(Value::Null);
        }
        serde_json::from_slice(&self.payload).map_err(|error| format!("JSONDecodeError: {error}"))
    }
}

fn read_error(error: io::Error) -> TransportError {
    match error.kind() {
        // A short read after the first byte and every framing/checksum
        // violation leave the parser misaligned.
        io::ErrorKind::UnexpectedEof | io::ErrorKind::InvalidData => {
            TransportError::Protocol(error.to_string())
        }
        _ => TransportError::Io,
    }
}

fn write_error(error: io::Error) -> TransportError {
    match error.kind() {
        // Frame validation refused a field before any byte was written; the
        // Python wire client raises the same text as a ValueError.
        io::ErrorKind::InvalidInput => TransportError::Codec(format!("ValueError: {error}")),
        _ => TransportError::Io,
    }
}

#[cfg(target_os = "linux")]
fn connect_from_alternate_loopback(target: SocketAddr, timeout: Duration) -> io::Result<TcpStream> {
    use std::os::fd::FromRawFd;
    let SocketAddr::V4(target) = target else {
        return Err(io::Error::from(io::ErrorKind::AddrNotAvailable));
    };
    // SAFETY: plain BSD-socket calls on a descriptor this function owns; the
    // TcpStream takes ownership immediately and closes it on every path.
    unsafe {
        let descriptor = libc::socket(libc::AF_INET, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0);
        if descriptor < 0 {
            return Err(io::Error::last_os_error());
        }
        let stream = TcpStream::from_raw_fd(descriptor);
        let address = |ip: [u8; 4], port: u16| libc::sockaddr_in {
            sin_family: libc::AF_INET as libc::sa_family_t,
            sin_port: port.to_be(),
            sin_addr: libc::in_addr {
                s_addr: u32::from_ne_bytes(ip),
            },
            sin_zero: [0; 8],
        };
        let length = std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t;
        let source = address([127, 0, 0, 2], 0);
        if libc::bind(
            descriptor,
            &source as *const _ as *const libc::sockaddr,
            length,
        ) != 0
        {
            return Err(io::Error::last_os_error());
        }
        // A blocking connect honors SO_SNDTIMEO on Linux.
        stream.set_write_timeout(Some(timeout))?;
        let destination = address(target.ip().octets(), target.port());
        if libc::connect(
            descriptor,
            &destination as *const _ as *const libc::sockaddr,
            length,
        ) != 0
        {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EINPROGRESS) {
                return Err(io::Error::from(io::ErrorKind::TimedOut));
            }
            return Err(error);
        }
        Ok(stream)
    }
}

/// `tofu_db/_loopback.py::create_connection`: connect once; a Linux loopback
/// tuple exhaustion retries from 127.0.0.2, whose tuple pool is independent.
/// No Hello or request has been sent when that recovery runs.
fn connect(host: &str, port: u16, timeout: Duration) -> io::Result<TcpStream> {
    let mut last_error = io::Error::new(io::ErrorKind::NotFound, "no address resolved");
    for address in (host, port).to_socket_addrs()? {
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(stream) => return Ok(stream),
            #[cfg(target_os = "linux")]
            Err(error)
                if matches!(
                    error.raw_os_error(),
                    Some(libc::EADDRINUSE) | Some(libc::EADDRNOTAVAIL)
                ) && host == "127.0.0.1" =>
            {
                return connect_from_alternate_loopback(address, timeout);
            }
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

pub struct Session {
    stream: TcpStream,
    reader: BufReader<TcpStream>,
    owner_id: u64,
    tenant_id: Option<u64>,
    schema_id: u32,
    counter: u32,
}

impl Session {
    pub fn connect(
        host: &str,
        port: u16,
        secret: &[u8],
        owner_id: u64,
        tenant_id: Option<u64>,
        schema_ids: &[u32],
        timeout: Duration,
    ) -> TransportResult<Self> {
        let token = tofu_db::daemon::derive_auth_token(secret)
            .map_err(|error| TransportError::Codec(error.to_string()))?;
        let mut schemas = schema_ids.to_vec();
        schemas.sort_unstable();
        schemas.dedup();
        let stream = connect(host, port, timeout)?;
        stream.set_nodelay(true)?;
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;
        let reader = BufReader::new(stream.try_clone()?);
        let mut session = Self {
            stream,
            reader,
            owner_id,
            tenant_id,
            schema_id: 0,
            counter: 1,
        };
        let correlation_id = session.next_correlation();
        let hello = Message::Hello(Hello {
            correlation_id,
            minimum_version: PROTOCOL_VERSION,
            maximum_version: PROTOCOL_VERSION,
            schema_ids: schemas.clone(),
            auth_token: token,
        });
        write_frame(&mut session.stream, &hello).map_err(write_error)?;
        let acknowledgement = session.read_frame()?;
        let Message::Response(response) = acknowledgement else {
            return Err(TransportError::Protocol(
                "storage.v2 hello acknowledgement mismatch".to_owned(),
            ));
        };
        if response.correlation_id != correlation_id {
            return Err(TransportError::Protocol(
                "storage.v2 hello acknowledgement mismatch".to_owned(),
            ));
        }
        if response.status != 0 {
            return Err(TransportError::Wire {
                code: response
                    .error_code
                    .filter(|code| !code.is_empty())
                    .unwrap_or_else(|| "unknown".to_owned()),
                message: response.error_message.unwrap_or_default(),
                retryable: response.retryable,
            });
        }
        if !schemas.contains(&response.schema_id) {
            return Err(TransportError::Protocol(
                "storage.v2 hello selected an unoffered schema".to_owned(),
            ));
        }
        session.schema_id = response.schema_id;
        Ok(session)
    }

    /// The socket's per-operation transport bound for the next exchange.
    pub fn set_timeout(&self, timeout: Duration) -> io::Result<()> {
        let timeout = timeout.max(Duration::from_millis(1));
        self.stream.set_read_timeout(Some(timeout))?;
        self.stream.set_write_timeout(Some(timeout))
    }

    /// A clone of the socket that `shutdown` wakes an in-flight exchange with.
    pub fn interrupt_handle(&self) -> io::Result<TcpStream> {
        self.stream.try_clone()
    }

    /// Idle receive readiness: the daemon never speaks first, so buffered
    /// bytes, EOF, reset or unsolicited bytes all make a session unusable.
    pub fn probe_liveness(&self) -> bool {
        if !self.reader.buffer().is_empty() {
            return false;
        }
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let mut descriptor = libc::pollfd {
                fd: self.stream.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            // SAFETY: one valid pollfd for a descriptor this session owns.
            let ready = unsafe { libc::poll(&mut descriptor, 1, 0) };
            ready == 0
        }
        #[cfg(not(unix))]
        {
            let mut byte = [0u8; 1];
            if self.stream.set_nonblocking(true).is_err() {
                return false;
            }
            let idle = matches!(self.stream.peek(&mut byte), Err(error) if error.kind() == io::ErrorKind::WouldBlock);
            idle && self.stream.set_nonblocking(false).is_ok()
        }
    }

    pub fn close(&self) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }

    fn next_correlation(&mut self) -> [u8; 16] {
        self.counter = self.counter.wrapping_add(1);
        if self.counter == 0 {
            self.counter = 1;
        }
        // Exactly the bytes wire.py sends: never all-zero, 16 bytes.
        let mut correlation = [0u8; 16];
        correlation[..10].copy_from_slice(b"tofu-diff-");
        correlation[10..14].copy_from_slice(&self.counter.to_be_bytes());
        correlation[14] = 1;
        correlation[15] = 1;
        correlation
    }

    fn read_frame(&mut self) -> TransportResult<Message> {
        match read_frame_or_eof_with_admission(&mut self.reader, |_| Ok(())) {
            Ok(Some(message)) => Ok(message),
            Ok(None) => Err(TransportError::ConnectionClosed),
            Err(error) => Err(read_error(error)),
        }
    }

    fn request_message(
        &self,
        correlation_id: [u8; 16],
        operation: &str,
        command_id: Option<&str>,
        deadline_unix_ms: u64,
        payload: Vec<u8>,
    ) -> Message {
        Message::Request(Request {
            correlation_id,
            deadline_unix_ms,
            owner_id: self.owner_id,
            tenant_id: self.tenant_id,
            command_id: command_id.map(str::to_owned),
            schema_id: self.schema_id,
            operation: operation.to_owned(),
            payload,
        })
    }

    pub fn request(
        &mut self,
        operation: &str,
        payload: &Map<String, Value>,
        command_id: Option<&str>,
        deadline_unix_ms: u64,
    ) -> TransportResult<WireResponse> {
        let correlation_id = self.next_correlation();
        let payload_bytes = serde_json::to_vec(payload)
            .map_err(|error| TransportError::Codec(format!("TypeError: {error}")))?;
        let wire_payload = if payload_bytes.len() > MAX_PAYLOAD_BYTES {
            let chunk_count = payload_bytes.len().div_ceil(MAX_BLOB_CHUNK_BYTES);
            if chunk_count as u64 > 1 << 32 {
                return Err(TransportError::Codec(
                    "ValueError: storage.v2 chunk index exceeds its u32 representation".to_owned(),
                ));
            }
            for (index, chunk) in payload_bytes.chunks(MAX_BLOB_CHUNK_BYTES).enumerate() {
                let message = Message::BlobChunk(BlobChunk {
                    correlation_id,
                    deadline_unix_ms,
                    owner_id: self.owner_id,
                    tenant_id: self.tenant_id,
                    command_id: command_id.map(str::to_owned),
                    schema_id: self.schema_id,
                    payload: chunk.to_vec(),
                    stream_id: correlation_id,
                    chunk_index: index as u32,
                    final_chunk: index + 1 == chunk_count,
                    total_payload_bytes: payload_bytes.len() as u64,
                });
                write_frame(&mut self.stream, &message).map_err(write_error)?;
            }
            Vec::new()
        } else {
            payload_bytes
        };
        let message = self.request_message(
            correlation_id,
            operation,
            command_id,
            deadline_unix_ms,
            wire_payload,
        );
        write_frame(&mut self.stream, &message).map_err(write_error)?;
        self.receive_response(correlation_id)
    }

    /// Pipelined reads: every response is read only after its segment is sent.
    ///
    /// The daemon serves a session's frames in order. Requests go out in
    /// segments that fit this socket's send buffer, so a send never waits for
    /// a peer that itself waits for its responses to be read. A read whose
    /// payload needs a streamed upload is not sent: its element is `None`.
    pub fn request_many(
        &mut self,
        requests: &[(String, Map<String, Value>)],
        deadline_unix_ms: u64,
    ) -> TransportResult<Vec<Option<WireResponse>>> {
        let mut frames: Vec<([u8; 16], Vec<u8>)> = Vec::new();
        let mut slots: Vec<Option<usize>> = Vec::new();
        for (operation, payload) in requests {
            let payload_bytes = serde_json::to_vec(payload)
                .map_err(|error| TransportError::Codec(format!("TypeError: {error}")))?;
            if payload_bytes.len() > MAX_PAYLOAD_BYTES {
                slots.push(None);
                continue;
            }
            let correlation_id = self.next_correlation();
            let message = self.request_message(
                correlation_id,
                operation,
                None,
                deadline_unix_ms,
                payload_bytes,
            );
            let mut frame = Vec::new();
            write_frame(&mut frame, &message).map_err(write_error)?;
            slots.push(Some(frames.len()));
            frames.push((correlation_id, frame));
        }
        let send_capacity = self.send_capacity();
        let mut responses = Vec::with_capacity(frames.len());
        let mut start = 0;
        while start < frames.len() {
            let mut end = start + 1;
            let mut size = frames[start].1.len();
            while end < frames.len()
                && send_capacity.is_some_and(|capacity| size + frames[end].1.len() <= capacity)
            {
                size += frames[end].1.len();
                end += 1;
            }
            let segment: Vec<u8> = frames[start..end]
                .iter()
                .flat_map(|(_, frame)| frame.iter().copied())
                .collect();
            self.stream.write_all(&segment)?;
            for (correlation_id, _) in &frames[start..end] {
                responses.push(self.receive_response(*correlation_id)?);
            }
            start = end;
        }
        if !self.reader.buffer().is_empty() {
            return Err(TransportError::Protocol(
                "storage.v2 peer sent bytes after the pipelined responses".to_owned(),
            ));
        }
        let mut responses: Vec<Option<WireResponse>> = responses.into_iter().map(Some).collect();
        Ok(slots
            .into_iter()
            .map(|slot| slot.and_then(|index| responses[index].take()))
            .collect())
    }

    /// `SO_SNDBUF`; without it each request is its own segment.
    fn send_capacity(&self) -> Option<usize> {
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let mut value: libc::c_int = 0;
            let mut length = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
            // SAFETY: getsockopt writes one c_int into a correctly sized buffer.
            let status = unsafe {
                libc::getsockopt(
                    self.stream.as_raw_fd(),
                    libc::SOL_SOCKET,
                    libc::SO_SNDBUF,
                    &mut value as *mut _ as *mut libc::c_void,
                    &mut length,
                )
            };
            (status == 0 && value > 0).then_some(value as usize)
        }
        #[cfg(not(unix))]
        {
            None
        }
    }

    fn receive_response(&mut self, correlation_id: [u8; 16]) -> TransportResult<WireResponse> {
        let mut streamed: Vec<u8> = Vec::new();
        let mut stream_id: Option<[u8; 16]> = None;
        let mut chunks: u32 = 0;
        let mut final_seen = false;
        loop {
            let message = self.read_frame()?;
            let (frame_correlation, frame_schema) = match &message {
                Message::Response(response) => (response.correlation_id, response.schema_id),
                Message::ResponseChunk(chunk) => (chunk.correlation_id, chunk.schema_id),
                Message::Hello(hello) => (hello.correlation_id, 0),
                Message::Request(request) => (request.correlation_id, request.schema_id),
                Message::BlobChunk(chunk) => (chunk.correlation_id, chunk.schema_id),
            };
            if frame_correlation != correlation_id {
                return Err(TransportError::Protocol(
                    "storage.v2 response correlation mismatch".to_owned(),
                ));
            }
            if frame_schema != self.schema_id {
                return Err(TransportError::Protocol(
                    "storage.v2 response schema mismatch".to_owned(),
                ));
            }
            match message {
                Message::ResponseChunk(chunk) => {
                    if final_seen
                        || chunk.payload.is_empty()
                        || chunk.payload.len() > MAX_BLOB_CHUNK_BYTES
                        || chunk.stream_id.iter().all(|byte| *byte == 0)
                        || chunk.chunk_index != chunks
                    {
                        return Err(TransportError::Protocol(
                            "invalid storage.v2 response chunk sequence".to_owned(),
                        ));
                    }
                    match stream_id {
                        None => stream_id = Some(chunk.stream_id),
                        Some(identity) if identity != chunk.stream_id => {
                            return Err(TransportError::Protocol(
                                "storage.v2 response stream identity changed".to_owned(),
                            ))
                        }
                        Some(_) => {}
                    }
                    streamed.extend_from_slice(&chunk.payload);
                    chunks += 1;
                    final_seen = chunk.final_chunk;
                }
                Message::Response(response) => {
                    return self.finish_response(response, chunks, final_seen, streamed);
                }
                _ => {
                    return Err(TransportError::Protocol(
                        "storage.v2 request answered by non-response".to_owned(),
                    ))
                }
            }
        }
    }

    fn finish_response(
        &self,
        response: Response,
        chunks: u32,
        final_seen: bool,
        streamed: Vec<u8>,
    ) -> TransportResult<WireResponse> {
        let payload = if chunks > 0 {
            if !final_seen
                || response.status != 0
                || !response.payload.is_empty()
                || response.error_code.is_some()
                || response.error_message.is_some()
                || response.retryable
            {
                return Err(TransportError::Protocol(
                    "invalid storage.v2 streamed response terminator".to_owned(),
                ));
            }
            streamed
        } else {
            response.payload
        };
        Ok(WireResponse {
            status: response.status,
            payload,
            error_code: response.error_code,
            error_message: response.error_message,
            retryable: response.retryable,
        })
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.close();
    }
}
