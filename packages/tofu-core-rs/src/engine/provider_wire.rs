//! Buffered engine binding for canonical requests, sealed wire and the P4 parser.
//!
//! Entry points: `prepare`, `decode`, and `WireProvider` (`ProviderPort`).
//! Transport is explicitly injected: production bindings must use the shared
//! network authority in `docs/NETWORK.md`; this module never connects or retries.
//! Supports the text/tool history expressible by engine `Message` and the three
//! API-key wire protocols within canonical/wire's declared scope. No OAuth,
//! images or native reasoning-history replay is synthesized.
//!
//! The landed parser consumes a whole body. Callbacks are buffered, not live;
//! cancellation during I/O belongs to the transport. `ProviderReport` preserves
//! parser evidence and received bytes because engine outcomes cannot represent
//! every usage/refusal/error attribute. In particular Raised has no partial text
//! in the parser API; its bytes, not an invented partial projection, are retained.

use serde_json::{json, Value};

use crate::canonical::{self, BodySpec};
use crate::route::RouteSpec;
use crate::stream::{self, StreamOutcome};
use crate::wire::{self, Protocol, SealedRequest};

use super::cancel::CancelToken;
use super::ports::{ProviderPort, ProviderRequest, StreamItem};
use super::types::{ProviderOutcome, ToolCall};

pub struct RequestOptions<'a> {
    pub protocol: Protocol,
    pub route: RouteSpec<'a>,
    pub max_tokens: u64,
    pub temperature: Option<f64>,
    pub tools: Option<&'a [Value]>,
    pub reasoning: Option<&'a Value>,
}

pub struct PreparedRequest {
    pub sealed: SealedRequest,
    pub protocol: Protocol,
    pub model: String,
    pub max_tokens: i64,
}

pub fn prepare(
    request: &ProviderRequest,
    options: &RequestOptions<'_>,
) -> canonical::Result<PreparedRequest> {
    let max_tokens = i64::try_from(options.max_tokens).map_err(|_| {
        canonical::Error::Unsupported("max_tokens exceeds stream parser i64 range".into())
    })?;
    let messages: Vec<Value> = request
        .messages
        .iter()
        .map(|message| {
            let mut row = json!({"role": message.role.as_str(), "content": message.content});
            if !message.tool_calls.is_empty() {
                row["tool_calls"] = Value::Array(
                    message
                        .tool_calls
                        .iter()
                        .map(|call| {
                            json!({
                                "id": call.id, "type": "function",
                                "function": {"name": call.name, "arguments": call.arguments}
                            })
                        })
                        .collect(),
                );
            }
            if let Some(id) = &message.tool_call_id {
                row["tool_call_id"] = json!(id);
            }
            row
        })
        .collect();
    let body = canonical::build_body(
        &messages,
        &BodySpec {
            model: &request.model,
            max_tokens: options.max_tokens,
            temperature: options.temperature,
            tools: options.tools,
            inference_profile: options.route.inference_profile,
            reasoning: options.reasoning,
        },
    )?;
    let sealed = wire::prepare(body, options.protocol, &options.route)?;
    Ok(PreparedRequest {
        sealed,
        protocol: options.protocol,
        model: request.model.clone(),
        max_tokens,
    })
}

/// The transport must retain bytes received before a failure and must observe
/// cancellation while blocked. It owns policy, deadlines and response framing;
/// ambiguous delivery/read failures must never cause a blind resend.
pub trait TransportPort: Send + Sync {
    fn exchange(
        &self,
        request: &SealedRequest,
        trace_id: &str,
        cancel: &CancelToken,
    ) -> TransportResponse;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportFailure {
    Dropped(String),
    Cancelled,
    Internal(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct TransportResponse {
    /// None means no response headers arrived.
    pub status: Option<u16>,
    pub body: Vec<u8>,
    pub failure: Option<TransportFailure>,
}

/// Full evidence for hosts needing usage on failed/refused responses or exact
/// raised attributes (including learned limits). Not a credential-bearing request.
#[derive(Debug, Clone, PartialEq)]
pub struct ProviderReport {
    pub outcome: ProviderOutcome,
    pub parsed: Option<canonical::Result<StreamOutcome>>,
    pub response: TransportResponse,
}

pub fn decode(
    request: &PreparedRequest,
    response: TransportResponse,
    trace_id: &str,
) -> ProviderReport {
    let parsed = (response.status == Some(200)).then(|| {
        stream::parse_request(
            request.protocol,
            &request.model,
            Some(request.max_tokens),
            trace_id,
            &response.body,
        )
    });
    let partial_text = match &parsed {
        Some(Ok(StreamOutcome::Finished(done))) => done
            .message
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        _ => String::new(),
    };
    let outcome = match &response.failure {
        Some(TransportFailure::Cancelled) => ProviderOutcome::Cancelled { partial_text },
        Some(TransportFailure::Dropped(detail)) => ProviderOutcome::Dropped {
            partial_text,
            detail: detail.clone(),
        },
        Some(TransportFailure::Internal(detail)) => ProviderOutcome::Internal {
            detail: detail.clone(),
        },
        None => {
            match response.status {
                Some(status) if status != 200 => ProviderOutcome::HttpError {
                    status,
                    partial_text: String::new(),
                    detail: String::from_utf8_lossy(&response.body).into_owned(),
                },
                None => ProviderOutcome::Internal {
                    detail: "transport returned neither status nor failure".into(),
                },
                Some(_) => match parsed.as_ref().expect("200 response parsed") {
                    Err(error) => ProviderOutcome::Internal {
                        detail: error.to_string(),
                    },
                    Ok(StreamOutcome::Raised(raised)) => {
                        let detail = raised.to_json().to_string();
                        if raised.class == "ContentFilterError" {
                            return ProviderReport {
                                outcome: ProviderOutcome::HttpError {
                                    status: 450,
                                    partial_text,
                                    detail,
                                },
                                parsed,
                                response,
                            };
                        }
                        if raised.class == "ModelRefusalError" {
                            return ProviderReport {
                                outcome: ProviderOutcome::ContentFilter {
                                    partial_text,
                                    detail,
                                },
                                parsed,
                                response,
                            };
                        }
                        match raised
                            .status_code
                            .and_then(|status| u16::try_from(status).ok())
                        {
                            Some(status) => ProviderOutcome::HttpError {
                                status,
                                detail,
                                partial_text,
                            },
                            None => ProviderOutcome::Internal { detail },
                        }
                    }
                    Ok(StreamOutcome::Finished(done)) if done.state != "provider_finished" => {
                        ProviderOutcome::Dropped {
                            partial_text,
                            detail: format!(
                                "{} (malformed frames: {})",
                                done.state, done.malformed_frame_count
                            ),
                        }
                    }
                    Ok(StreamOutcome::Finished(done)) if done.is_model_refusal() => {
                        ProviderOutcome::ContentFilter {
                            partial_text,
                            detail: done
                                .model_refusal
                                .as_ref()
                                .map(Value::to_string)
                                .unwrap_or_else(|| "content_filter".into()),
                        }
                    }
                    Ok(StreamOutcome::Finished(done)) => {
                        let calls = done.message.get("tool_calls").and_then(Value::as_array);
                        let tool_calls = calls
                            .into_iter()
                            .flatten()
                            .map(|call| ToolCall {
                                id: call["id"].as_str().unwrap_or("").to_owned(),
                                name: call["function"]["name"].as_str().unwrap_or("").to_owned(),
                                arguments: call["function"]["arguments"]
                                    .as_str()
                                    .unwrap_or("")
                                    .to_owned(),
                            })
                            .collect();
                        // Engine Message cannot replay provider-private reasoning or tool metadata.
                        if !matches!(done.finish_reason(), "stop" | "tool_calls" | "length")
                            || done.message.keys().any(|key| {
                                !["role", "content", "tool_calls"].contains(&key.as_str())
                            })
                            || calls.into_iter().flatten().any(|call| {
                                call.as_object().is_some_and(|map| {
                                    map.keys().any(|key| {
                                        !["id", "type", "function"].contains(&key.as_str())
                                    })
                                })
                            })
                        {
                            ProviderOutcome::Internal { detail: "unsupported engine replay metadata; inspect ProviderReport.parsed".into() }
                        } else {
                            ProviderOutcome::Completed {
                                text: partial_text,
                                tool_calls,
                                finish_reason: done.finish_reason().to_owned(),
                                usage: Value::Object(done.usage.clone()),
                            }
                        }
                    }
                },
            }
        }
    };
    ProviderReport {
        outcome,
        parsed,
        response,
    }
}

/// Buffered adapter; the host supplies a request-scoped trace identity.
/// Use `exchange` when the full parser report is needed, not just engine policy.
pub struct WireProvider<'a, T> {
    /// The exact model admitted for this fixed route; no cross-provider fallback.
    pub model: &'a str,
    pub transport: T,
    pub options: RequestOptions<'a>,
    pub trace_id: String,
}

impl<T: TransportPort> WireProvider<'_, T> {
    pub fn exchange(
        &self,
        request: &ProviderRequest,
        cancel: &CancelToken,
    ) -> canonical::Result<ProviderReport> {
        if request.model != self.model {
            return Err(canonical::Error::Unsupported(
                "fixed-route provider cannot dispatch another model".into(),
            ));
        }
        let prepared = prepare(request, &self.options)?;
        let response = if cancel.is_cancelled() {
            TransportResponse {
                status: None,
                body: Vec::new(),
                failure: Some(TransportFailure::Cancelled),
            }
        } else {
            self.transport
                .exchange(&prepared.sealed, &self.trace_id, cancel)
        };
        let mut report = decode(&prepared, response, &self.trace_id);
        if cancel.is_cancelled() {
            let partial_text = match &report.parsed {
                Some(Ok(StreamOutcome::Finished(done))) => done
                    .message
                    .get("content")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                _ => String::new(),
            };
            report.outcome = ProviderOutcome::Cancelled { partial_text };
        }
        Ok(report)
    }
}

impl<T: TransportPort> ProviderPort for WireProvider<'_, T> {
    fn stream(
        &self,
        request: &ProviderRequest,
        on_item: &mut dyn FnMut(StreamItem),
        cancel: &CancelToken,
    ) -> ProviderOutcome {
        let report = match self.exchange(request, cancel) {
            Ok(report) => report,
            Err(error) => {
                return ProviderOutcome::Internal {
                    detail: error.to_string(),
                }
            }
        };
        if let ProviderOutcome::Completed {
            text, tool_calls, ..
        } = &report.outcome
        {
            if !text.is_empty() {
                on_item(StreamItem::Text(text.clone()));
            }
            for call in tool_calls {
                if cancel.is_cancelled() {
                    return ProviderOutcome::Cancelled {
                        partial_text: text.clone(),
                    };
                }
                on_item(StreamItem::ToolCall(call.clone()));
            }
            if cancel.is_cancelled() {
                return ProviderOutcome::Cancelled {
                    partial_text: text.clone(),
                };
            }
        }
        report.outcome
    }
}
