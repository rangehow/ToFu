//! Provider transport: blocking HTTP/1.1 keep-alive POST and SSE decoding of
//! the three wire faces (Chat Completions, Anthropic Messages, Responses)
//! through the ported production stream parser (`crate::stream`).
//!
//! Scope: the plain-HTTP loopback provider of `benchmarks/harness_perf`
//! (`stub_provider.py`), which answers with a `Content-Length` SSE body. TLS,
//! proxies, chunked bodies, retries and route health stay in the product's
//! network authority and are not ported.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::time::Instant;

use serde_json::Value;

use crate::stream::{self, StreamOutcome};
use crate::wire::{Protocol, SealedRequest};

#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: String,
}

#[derive(Debug, Default, Clone, PartialEq)]
pub struct Completion {
    pub text: String,
    pub tool_calls: Vec<ToolCall>,
    pub finish_reason: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
}

/// Timings of one exchange (monotonic, for the round profile).
#[derive(Debug, Default, Clone, Copy)]
pub struct ExchangeTiming {
    pub send_us: u64,
    pub wait_headers_us: u64,
    pub body_us: u64,
    pub parse_us: u64,
}

pub struct Connection {
    host_port: String,
    reader: Option<BufReader<TcpStream>>,
}

fn split_url(url: &str) -> io::Result<(&str, &str)> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "only http:// is supported"))?;
    Ok(match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, "/"),
    })
}

impl Connection {
    pub fn new(url: &str) -> io::Result<Self> {
        let (host_port, _) = split_url(url)?;
        Ok(Self {
            host_port: host_port.to_owned(),
            reader: None,
        })
    }

    fn stream(&mut self) -> io::Result<&mut BufReader<TcpStream>> {
        if self.reader.is_none() {
            let stream = TcpStream::connect(&self.host_port)?;
            stream.set_nodelay(true)?;
            self.reader = Some(BufReader::with_capacity(64 * 1024, stream));
        }
        Ok(self.reader.as_mut().expect("connected above"))
    }

    /// POST `request` and return the response body.
    pub fn post(
        &mut self,
        request: &SealedRequest,
        trace_id: &str,
        timing: &mut ExchangeTiming,
    ) -> io::Result<Vec<u8>> {
        let reused = self.reader.is_some();
        match self.post_once(request, trace_id, timing) {
            // A keep-alive peer closed the idle connection: it answered no
            // status line, so the request was not processed.
            Err(error) if reused && error.kind() == io::ErrorKind::ConnectionReset => {
                self.post_once(request, trace_id, timing)
            }
            other => other,
        }
    }

    fn post_once(
        &mut self,
        request: &SealedRequest,
        trace_id: &str,
        timing: &mut ExchangeTiming,
    ) -> io::Result<Vec<u8>> {
        let (_, path) = split_url(&request.url)?;
        let mut head = format!("POST {path} HTTP/1.1\r\nHost: {}\r\n", self.host_port);
        for (name, value) in &request.headers {
            head.push_str(name);
            head.push_str(": ");
            head.push_str(value);
            head.push_str("\r\n");
        }
        head.push_str("M-TraceId: ");
        head.push_str(trace_id);
        head.push_str(&format!(
            "\r\nContent-Length: {}\r\nConnection: keep-alive\r\n\r\n",
            request.payload.len()
        ));
        let started = Instant::now();
        let result = (|| {
            let reader = self.stream()?;
            let stream = reader.get_mut();
            stream.write_all(head.as_bytes())?;
            stream.write_all(&request.payload)?;
            timing.send_us = started.elapsed().as_micros() as u64;
            let mut line = String::new();
            if reader.read_line(&mut line)? == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::ConnectionReset,
                    "provider closed before a status line",
                ));
            }
            let status: u16 = line
                .split_whitespace()
                .nth(1)
                .and_then(|code| code.parse().ok())
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "bad status line"))?;
            let mut length = None;
            loop {
                line.clear();
                reader.read_line(&mut line)?;
                let header = line.trim_end();
                if header.is_empty() {
                    break;
                }
                if let Some((name, value)) = header.split_once(':') {
                    if name.eq_ignore_ascii_case("content-length") {
                        length = value.trim().parse::<usize>().ok();
                    }
                }
            }
            timing.wait_headers_us = started.elapsed().as_micros() as u64 - timing.send_us;
            let length = length
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "no Content-Length"))?;
            let mut body = vec![0u8; length];
            reader.read_exact(&mut body)?;
            timing.body_us =
                started.elapsed().as_micros() as u64 - timing.send_us - timing.wait_headers_us;
            if status != 200 {
                return Err(io::Error::other(format!("provider status {status}")));
            }
            Ok(body)
        })();
        if result.is_err() {
            self.reader = None;
        }
        result
    }
}

/// Decode one complete SSE body through the ported production parser
/// (`crate::stream`); only a verified provider finish is a completion.
pub fn decode_sse(protocol: Protocol, body: &[u8]) -> io::Result<Completion> {
    let outcome = stream::parse(protocol, "", "", body)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
    let finished = match outcome {
        StreamOutcome::Raised(raised) => {
            return Err(io::Error::other(format!(
                "{}: {}",
                raised.class, raised.message
            )))
        }
        StreamOutcome::Finished(finished) => finished,
    };
    if finished.state != "provider_finished" {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            format!("stream is not a verified completion ({})", finished.state),
        ));
    }
    let text = |value: &Value| value.as_str().unwrap_or("").to_owned();
    let tool_calls = finished
        .message
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .map(|call| ToolCall {
                    id: text(&call["id"]),
                    name: text(&call["function"]["name"]),
                    arguments: text(&call["function"]["arguments"]),
                })
                .collect()
        })
        .unwrap_or_default();
    Ok(Completion {
        text: finished
            .message
            .get("content")
            .map(text)
            .unwrap_or_default(),
        tool_calls,
        finish_reason: finished.finish_reason().to_owned(),
        input_tokens: finished.normalized.input.max(0) as u64,
        output_tokens: finished.normalized.output.max(0) as u64,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn anthropic_tool_use_stream_decodes() {
        let body = concat!(
            "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":9}}}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"ok\"}}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"t1\",\"name\":\"run_command\",\"input\":{}}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"command\\\": \\\"true\\\"}\"}}\n\n",
            "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":8}}\n\n",
            "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        );
        let completion = decode_sse(Protocol::Anthropic, body.as_bytes()).unwrap();
        assert_eq!(completion.text, "ok");
        assert_eq!(
            completion.tool_calls[0].arguments,
            "{\"command\": \"true\"}"
        );
        assert_eq!((completion.input_tokens, completion.output_tokens), (9, 8));
    }

    #[test]
    fn a_cut_stream_is_not_a_completion() {
        let body = "data: {\"type\":\"message_start\",\"message\":{\"usage\":{}}}\n\n";
        assert!(decode_sse(Protocol::Anthropic, body.as_bytes()).is_err());
    }
}
