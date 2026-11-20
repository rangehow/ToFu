//! A scripted storage.v2 peer over the engine's own framing, for client tests.
//!
//! Every accepted connection answers Hello with the offered schema, then
//! hands each request (after reassembling an upload stream) to the script.

use std::io::{BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::Value;
use tofu_db::protocol::{read_frame, write_frame, Message, Request, Response, ResponseChunk};

#[derive(Clone, Debug)]
pub struct Seen {
    pub operation: String,
    pub command_id: Option<String>,
    pub payload: Value,
    pub chunks: usize,
    pub connection: usize,
}

pub enum Action {
    Ok(Value),
    /// A successful response streamed in chunks of this many bytes.
    Chunked(Value, usize),
    Error(&'static str, &'static str, bool),
    /// Close the connection without answering.
    Close,
    /// Answer after a delay.
    Delay(Duration, Value),
}

pub type Script = dyn Fn(&Seen, usize) -> Action + Send + Sync;

pub struct FakeDaemon {
    pub port: u16,
    pub seen: Arc<Mutex<Vec<Seen>>>,
}

fn response(
    request: &Request,
    status: u16,
    payload: Vec<u8>,
    error: Option<(&str, &str, bool)>,
) -> Message {
    Message::Response(Response {
        correlation_id: request.correlation_id,
        schema_id: request.schema_id,
        payload,
        status,
        error_code: error.map(|(code, _, _)| code.to_owned()),
        error_message: error.map(|(_, message, _)| message.to_owned()),
        retryable: error.is_some_and(|(_, _, retryable)| retryable),
    })
}

fn serve(stream: TcpStream, connection: usize, script: Arc<Script>, seen: Arc<Mutex<Vec<Seen>>>) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut writer = stream;
    let Ok(Message::Hello(hello)) = read_frame(&mut reader) else {
        return;
    };
    let ack = Message::Response(Response {
        correlation_id: hello.correlation_id,
        schema_id: hello.schema_ids[0],
        payload: Vec::new(),
        status: 0,
        error_code: None,
        error_message: None,
        retryable: false,
    });
    if write_frame(&mut writer, &ack).is_err() {
        return;
    }
    let mut upload: Vec<u8> = Vec::new();
    let mut chunks = 0;
    loop {
        let request = match read_frame(&mut reader) {
            Ok(Message::Request(request)) => request,
            Ok(Message::BlobChunk(chunk)) => {
                upload.extend_from_slice(&chunk.payload);
                chunks += 1;
                continue;
            }
            _ => return,
        };
        let body = if chunks > 0 {
            std::mem::take(&mut upload)
        } else {
            request.payload.clone()
        };
        let record = Seen {
            operation: request.operation.clone(),
            command_id: request.command_id.clone(),
            payload: serde_json::from_slice(&body).unwrap_or(Value::Null),
            chunks,
            connection,
        };
        chunks = 0;
        let index = {
            let mut seen = seen.lock().unwrap();
            seen.push(record.clone());
            seen.iter()
                .filter(|item| item.operation == record.operation)
                .count()
        };
        let message = match script(&record, index) {
            Action::Ok(value) => response(&request, 0, serde_json::to_vec(&value).unwrap(), None),
            Action::Delay(delay, value) => {
                thread::sleep(delay);
                response(&request, 0, serde_json::to_vec(&value).unwrap(), None)
            }
            Action::Error(code, message, retryable) => {
                response(&request, 503, Vec::new(), Some((code, message, retryable)))
            }
            Action::Close => return,
            Action::Chunked(value, size) => {
                let bytes = serde_json::to_vec(&value).unwrap();
                let pieces: Vec<&[u8]> = bytes.chunks(size).collect();
                for (index, piece) in pieces.iter().enumerate() {
                    let chunk = Message::ResponseChunk(ResponseChunk {
                        correlation_id: request.correlation_id,
                        schema_id: request.schema_id,
                        payload: piece.to_vec(),
                        stream_id: request.correlation_id,
                        chunk_index: index as u32,
                        final_chunk: index + 1 == pieces.len(),
                    });
                    write_frame(&mut writer, &chunk).unwrap();
                }
                response(&request, 0, Vec::new(), None)
            }
        };
        if write_frame(&mut writer, &message).is_err() || writer.flush().is_err() {
            return;
        }
    }
}

impl FakeDaemon {
    pub fn start(script: impl Fn(&Seen, usize) -> Action + Send + Sync + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let script: Arc<Script> = Arc::new(script);
        let shared = seen.clone();
        thread::spawn(move || {
            for (connection, stream) in listener.incoming().enumerate() {
                let Ok(stream) = stream else { return };
                let script = script.clone();
                let seen = shared.clone();
                thread::spawn(move || serve(stream, connection, script, seen));
            }
        });
        Self { port, seen }
    }

    pub fn seen(&self) -> Vec<Seen> {
        self.seen.lock().unwrap().clone()
    }
}
