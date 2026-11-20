//! In-stream provider error classification: `SSEAccumulator._handle_sse_error`
//! and `llm_errors._classify_http_error` for every status an in-stream error
//! object reaches.

use serde_json::{json, Map, Value};

use super::pattern;
use super::py;
use crate::pyjson::dumps_spaced;

/// An exception the Python oracle raises, projected to its public attributes.
#[derive(Debug, Clone, PartialEq)]
pub struct Raised {
    pub class: &'static str,
    pub status_code: Option<i64>,
    pub is_gateway: Option<bool>,
    pub is_quota: Option<bool>,
    pub is_shared_contention: Option<bool>,
    pub reason: Option<String>,
    pub message: String,
    /// Further class attributes that are neither `None` nor `False`
    /// (`gen_stream_golden.EXTRA_ERROR_ATTRS`).
    pub extra: Vec<(&'static str, Value)>,
    /// The `_learn_model_limit(model, limit, route_key=...)` call Python made
    /// before raising; it mutates process-global learned limits.
    pub learned_limit: Option<LearnedLimit>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LearnedLimit {
    pub model: String,
    pub limit: i64,
    pub route_key: String,
}

impl Raised {
    pub(crate) fn plain(class: &'static str, message: String) -> Self {
        Self {
            class,
            status_code: None,
            is_gateway: None,
            is_quota: None,
            is_shared_contention: None,
            reason: None,
            message,
            extra: Vec::new(),
            learned_limit: None,
        }
    }

    fn with_status(class: &'static str, message: String, status: i64) -> Self {
        Self {
            status_code: Some(status),
            ..Self::plain(class, message)
        }
    }

    fn with(mut self, name: &'static str, value: Value) -> Self {
        self.extra.push((name, value));
        self
    }

    pub(crate) fn retryable(message: String, status: i64) -> Self {
        Self::with_status("RetryableAPIError", message, status)
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("class".into(), json!(self.class));
        if let Some(status) = self.status_code {
            out.insert("status_code".into(), json!(status));
        }
        for (name, flag) in [
            ("is_gateway", self.is_gateway),
            ("is_quota", self.is_quota),
            ("is_shared_contention", self.is_shared_contention),
        ] {
            if let Some(flag) = flag {
                out.insert(name.into(), json!(flag));
            }
        }
        if let Some(reason) = &self.reason {
            out.insert("reason".into(), json!(reason));
        }
        for (name, value) in &self.extra {
            out.insert((*name).into(), value.clone());
        }
        if let Some(learned) = &self.learned_limit {
            out.insert(
                "learned_limit".into(),
                json!({"model": learned.model, "limit": learned.limit, "route_key": learned.route_key}),
            );
        }
        out.insert("message".into(), json!(self.message));
        Value::Object(out)
    }
}

#[derive(Debug)]
pub(crate) enum Stop {
    Raise(Box<Raised>),
    Unsupported(String),
}

impl From<Raised> for Stop {
    fn from(raised: Raised) -> Self {
        Self::Raise(Box::new(raised))
    }
}

pub(crate) type Flow<T> = std::result::Result<T, Stop>;

pub(crate) fn unsupported<T>(what: impl Into<String>) -> Flow<T> {
    Err(Stop::Unsupported(what.into()))
}

/// The request and attempt facts `_handle_sse_error` reads.
pub(crate) struct ErrorContext<'a> {
    pub model: &'a str,
    pub trace_id: &'a str,
    /// `body.get('max_tokens', 0)`.
    pub max_tokens: i64,
    /// `SSEAccumulator.produced_output()`.
    pub produced_output: bool,
}

/// `lib/model_info/_limits.py` `_MAX_LEARNED_MODEL_LIMIT`.
const MAX_LEARNED_MODEL_LIMIT: i64 = 1_000_000;
const SUBSCRIPTION_COOLDOWN_CAP_S: f64 = 86400.0;

#[derive(Default)]
struct RateLimit {
    status: i64,
    is_gateway: bool,
    is_quota: bool,
    is_shared_contention: bool,
    reason: Option<String>,
    subscription: bool,
    retry_after: Option<f64>,
}

const SUBSCRIPTION_QUOTA_PATTERNS: &[&str] = &[
    "usage_limit_reached",
    "selected model is at capacity",
    "reached your 5-hour usage limit",
    "reached your weekly (7-day) usage limit",
    "reached your monthly usage limit",
];

fn is_subscription_quota_error(text: &str) -> bool {
    let lower = text.to_lowercase();
    SUBSCRIPTION_QUOTA_PATTERNS
        .iter()
        .any(|p| lower.contains(p))
}

/// `parse_subscription_retry_after`. A `resets_at` deadline already passed
/// yields `None` like Python; a future one is a wall-clock-derived duration
/// no two processes reproduce exactly, so it is `Unsupported`.
fn parse_subscription_retry_after(text: &str) -> Flow<Option<f64>> {
    if text.is_empty() {
        return Ok(None);
    }
    if let Some(number) = pattern::quoted_number_field(text, "resets_in_seconds") {
        let seconds: f64 = number.parse().unwrap_or(f64::INFINITY);
        return Ok(Some(SUBSCRIPTION_COOLDOWN_CAP_S.min(seconds.max(1.0))));
    }
    if let Some(number) = pattern::quoted_number_field(text, "resets_at") {
        let deadline: f64 = number.parse().unwrap_or(f64::INFINITY);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64());
        if deadline <= now {
            return Ok(None);
        }
        return unsupported("subscription-quota cooldown from a future resets_at");
    }
    Ok(None)
}

/// `RateLimitError(msg, ...)` (or its `ModelRouteUnavailableError` subclass).
fn rate_limit(class: &'static str, message: String, fields: RateLimit) -> Flow<Raised> {
    let subscription =
        fields.subscription || (!message.is_empty() && is_subscription_quota_error(&message));
    let mut retry_after = fields.retry_after;
    if subscription && retry_after.is_none() {
        retry_after = parse_subscription_retry_after(&message)?;
    }
    let reason = match fields.reason {
        Some(reason) if !reason.is_empty() => reason,
        _ => message.clone(),
    };
    let mut raised = Raised {
        status_code: Some(fields.status),
        is_gateway: Some(fields.is_gateway),
        is_quota: Some(fields.is_quota),
        is_shared_contention: Some(fields.is_shared_contention),
        reason: Some(py::prefix(&reason, 200).to_owned()),
        ..Raised::plain(class, message)
    };
    if subscription {
        raised = raised.with("is_subscription_quota", json!(true));
    }
    if let Some(seconds) = retry_after {
        raised = raised.with("retry_after_s", json!(seconds));
    }
    Ok(raised)
}

fn plain_rate_limit(message: String, fields: RateLimit) -> Flow<Raised> {
    rate_limit("RateLimitError", message, fields)
}

const CP1252_HIGH: &[(char, u8)] = &[
    ('\u{20ac}', 0x80),
    ('\u{201a}', 0x82),
    ('\u{0192}', 0x83),
    ('\u{201e}', 0x84),
    ('\u{2026}', 0x85),
    ('\u{2020}', 0x86),
    ('\u{2021}', 0x87),
    ('\u{02c6}', 0x88),
    ('\u{2030}', 0x89),
    ('\u{0160}', 0x8a),
    ('\u{2039}', 0x8b),
    ('\u{0152}', 0x8c),
    ('\u{017d}', 0x8e),
    ('\u{2018}', 0x91),
    ('\u{2019}', 0x92),
    ('\u{201c}', 0x93),
    ('\u{201d}', 0x94),
    ('\u{2022}', 0x95),
    ('\u{2013}', 0x96),
    ('\u{2014}', 0x97),
    ('\u{02dc}', 0x98),
    ('\u{2122}', 0x99),
    ('\u{0161}', 0x9a),
    ('\u{203a}', 0x9b),
    ('\u{0153}', 0x9c),
    ('\u{017e}', 0x9e),
    ('\u{0178}', 0x9f),
];

fn has_cjk(text: &str) -> bool {
    text.chars()
        .any(|ch| ('\u{4e00}'..='\u{9fff}').contains(&ch))
}

/// `llm_errors.repair_mojibake`; the flag says a repaired copy was returned.
pub(crate) fn repair_mojibake(text: &str) -> (String, bool) {
    if !text
        .chars()
        .any(|ch| ('\u{80}'..='\u{ff}').contains(&ch) || ch == '\u{201a}')
    {
        return (text.to_owned(), false);
    }
    let latin1: Option<Vec<u8>> = text
        .chars()
        .map(|ch| u8::try_from(u32::from(ch)).ok())
        .collect();
    let raw = latin1.or_else(|| {
        text.chars()
            .map(|ch| match u32::from(ch) {
                code @ (0..=0x7f | 0xa0..=0xff) => Some(code as u8),
                _ => CP1252_HIGH
                    .iter()
                    .find(|(mapped, _)| *mapped == ch)
                    .map(|(_, byte)| *byte),
            })
            .collect()
    });
    let Some(raw) = raw else {
        return (text.to_owned(), false);
    };
    match String::from_utf8(raw) {
        Ok(repaired) if has_cjk(&repaired) && !has_cjk(text) => (repaired, true),
        _ => (text.to_owned(), false),
    }
}

const PROVIDER_ERROR_STATUS: &[(&str, i64)] = &[
    ("rate_limit_exceeded", 429),
    ("rate_limit_error", 429),
    ("insufficient_quota", 429),
    ("usage_limit_reached", 429),
    ("server_error", 500),
    ("api_error", 500),
    ("internal_error", 500),
    ("overloaded", 529),
    ("overloaded_error", 529),
    ("invalid_api_key", 401),
    ("authentication_error", 401),
    ("permission_error", 403),
    ("permission_denied", 403),
    ("not_found_error", 404),
    ("invalid_request_error", 400),
    ("context_length_exceeded", 400),
    ("invalid_encrypted_content", 400),
    ("content_filter", 450),
    ("content_policy_violation", 450),
    ("request_too_large", 413),
];

const INVALID_ENCRYPTED_CONTENT: &str = "invalid_encrypted_content";

/// `_INVALID_ENCRYPTED_CONTENT_MESSAGE_RE.match`.
fn is_encrypted_content_sentence(message: &str) -> bool {
    let Some(rest) = message.strip_prefix("The encrypted content for item ") else {
        return false;
    };
    let id_len = rest
        .bytes()
        .take_while(|b| b.is_ascii_alphanumeric() || *b == b'_' || *b == b'-')
        .count();
    (1..=256).contains(&id_len) && rest[id_len..].starts_with(" could not be verified.")
}

/// `invalid_encrypted_content_error_object`.
pub(crate) fn invalid_encrypted_content_object(error: &Map<String, Value>) -> bool {
    if error.get("code").and_then(Value::as_str) == Some(INVALID_ENCRYPTED_CONTENT) {
        return true;
    }
    !py::truthy(error.get("code"))
        && error
            .get("message")
            .and_then(Value::as_str)
            .is_some_and(is_encrypted_content_sentence)
}

/// `InvalidEncryptedContentError(param=...).param`.
fn encrypted_content_param(param: Option<&Value>) -> Value {
    let Some(param) = param.and_then(Value::as_str) else {
        return Value::Null;
    };
    let Some(rest) = param.strip_prefix("input") else {
        return Value::Null;
    };
    let index_digits =
        |text: &str| -> usize { text.bytes().take_while(u8::is_ascii_digit).count() };
    let (index, rest) = if let Some(inner) = rest.strip_prefix('[') {
        let len = index_digits(inner);
        match inner[len..].strip_prefix(']') {
            Some(after) if (1..=10).contains(&len) => (Some(&inner[..len]), after),
            _ => (None, rest),
        }
    } else if let Some(inner) = rest.strip_prefix('.') {
        let len = index_digits(inner);
        if (1..=10).contains(&len) {
            (Some(&inner[..len]), &inner[len..])
        } else {
            (None, rest)
        }
    } else {
        (None, rest)
    };
    let suffix = match rest {
        "" => "",
        ".encrypted_content" => ".encrypted_content",
        _ => return Value::Null,
    };
    let index = index.map_or(String::new(), |digits| {
        let trimmed = digits.trim_start_matches('0');
        format!("[{}]", if trimmed.is_empty() { "0" } else { trimmed })
    });
    json!(format!("input{index}{suffix}"))
}

/// `provider_error_http_status`.
fn provider_error_http_status(error: &Map<String, Value>) -> Option<i64> {
    for field in ["http_code", "status_code", "status", "code"] {
        match error.get(field) {
            Some(Value::Number(number)) => {
                if let Some(code) = number.as_i64() {
                    if (400..=599).contains(&code) {
                        return Some(code);
                    }
                }
            }
            Some(Value::String(text))
                if text.len() == 3 && text.bytes().all(|b| b.is_ascii_digit()) =>
            {
                let code: i64 = text.parse().unwrap_or(0);
                if (400..=599).contains(&code) {
                    return Some(code);
                }
            }
            _ => {}
        }
    }
    for field in ["code", "type"] {
        if let Some(text) = error.get(field).and_then(Value::as_str) {
            if let Some((_, code)) = PROVIDER_ERROR_STATUS.iter().find(|(name, _)| *name == text) {
                return Some(*code);
            }
        }
    }
    invalid_encrypted_content_object(error).then_some(400)
}

/// `summarize_error_body` for a `json.dumps({'error': ...})` body.
fn summarize_error_body(text: &str) -> String {
    let body = py::strip(text);
    if !body.starts_with('{') {
        return text.to_owned();
    }
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(body) else {
        return text.to_owned();
    };
    match data
        .get("error")
        .and_then(Value::as_object)
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
    {
        Some(message) if !py::strip(message).is_empty() => message.to_owned(),
        _ => text.to_owned(),
    }
}

const QUOTA_EXHAUSTED_PATTERNS: &[&str] = &[
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "check your plan and billing",
    "insufficient_balance",
    "insufficient balance",
    "credit_balance_too_low",
    "credit balance is too low",
    "credit balance is insufficient",
    "billing_not_active",
    "account_deactivated",
    "quota_exceeded",
    "payment required",
    "out of credits",
    "余额不足",
    "额度不足",
    "余额为零",
    "欠费",
];

const SHARED_PROJECT_LIMIT_GROUPS: &[&[&str]] = &[
    &["reached project", "tpm rate limit"],
    &["reached project", "rpm rate limit"],
    &["reached project", "concurrency rate limit"],
    &["app:", "在模型:", "每分钟请求次数超过限制"],
];

const UPSTREAM_TRANSIENT_PATTERNS: &[&str] = &[
    "请稍后",
    "请稍候",
    "稍后重试",
    "稍后再试",
    "稍后再尝试",
    "try again later",
    "temporarily unavailable",
    "overloaded",
    "负载较高",
    "负载高",
];

const GATEWAY_FAULT_PATTERNS: &[&str] = &[
    "resolve groups failed",
    "model unsupported by selected groups",
    "nested exception is java.lang.outofmemoryerror",
    "bad response status code",
];

/// `ROUTE_MISSING_MARKERS`.
const ROUTE_MISSING_MARKERS: &[&str] = &[
    "model_not_found",
    "does not exist",
    "no such model",
    "unsupported model",
    "unsupported_model",
    "model not supported",
    "invalid model name",
    "不支持的模型类型",
];

const IMAGE_ERROR_PATTERNS: &[&str] = &[
    "image dimensions exceed",
    "exceed max allowed size",
    "could not process image",
    "invalid image",
    "image is too large",
    "image resolution exceed",
];

const STREAM_ONLY_PATTERNS: &[&str] = &[
    "only support stream",
    "only supports stream",
    "enable the stream parameter",
    "stream mode only",
    "stream must be set to true",
    "\"stream\" must be set to true",
    "'stream' must be set to true",
];

/// `REPAIRABLE_PARAMETER_WIRE_NAMES`.
const REPAIRABLE_PARAMETERS: &[(&str, &str)] = &[
    ("temperature", "temperature"),
    ("top_p", "top_p"),
    ("top_k", "top_k"),
    ("presence_penalty", "presence_penalty"),
    ("frequency_penalty", "frequency_penalty"),
    ("stop", "stop"),
    ("stop_sequences", "stop"),
    ("seed", "seed"),
    ("parallel_tool_calls", "parallel_tool_calls"),
    ("logprobs", "logprobs"),
    ("reasoning_effort", "reasoning_effort"),
    ("thinking.display", "thinking.display"),
    ("thinking.enabled.display", "thinking.display"),
    ("thinking.adaptive.display", "thinking.display"),
];

const UNSUPPORTED_PARAMETER_CODES: &[&str] = &["unsupported_parameter", "unknown_parameter"];

fn contains_any(text: &str, patterns: &[&str]) -> bool {
    patterns.iter().any(|p| text.contains(p))
}

/// `_is_upstream_vendor_transient`.
fn is_upstream_vendor_transient(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    let lower = text.to_lowercase();
    let squashed = lower.replace(' ', "");
    if contains_any(&lower, UPSTREAM_TRANSIENT_PATTERNS)
        || contains_any(&lower, GATEWAY_FAULT_PATTERNS)
    {
        return true;
    }
    let (repaired, changed) = repair_mojibake(text);
    if changed && contains_any(&repaired.to_lowercase(), UPSTREAM_TRANSIENT_PATTERNS) {
        return true;
    }
    if !squashed.contains("\"source\":\"upstream_vendor\"") {
        return false;
    }
    if let Some(digits) = pattern::three_digits_after(&squashed, "\"upstreamstatus\":", false) {
        let digits: Vec<char> = digits.chars().collect();
        let status = py::decimal_value(&digits, 999).unwrap_or(0);
        if status < 500 && status != 408 && status != 429 {
            return false;
        }
    }
    !squashed.contains("\"type\":\"invalid_request_error\"")
}

/// `_provider_error_projection(err_msg)` for a dumped `{'error': ...}` body:
/// the error object and its searchable message.
fn error_projection<'a>(error: &'a Map<String, Value>, err_msg: &'a str) -> &'a str {
    match error.get("message") {
        Some(Value::String(message)) if !message.is_empty() => message,
        _ => err_msg,
    }
}

/// `_unsupported_request_parameter` → `(param, error_code)`.
fn unsupported_request_parameter(
    error: &Map<String, Value>,
    err_msg: &str,
) -> Option<(&'static str, String)> {
    let mut names = pattern::unsupported_parameter_names(error_projection(error, err_msg));
    let raw_code = error.get("code");
    if let (Some(Value::String(code)), Some(Value::String(param))) = (raw_code, error.get("param"))
    {
        if UNSUPPORTED_PARAMETER_CODES.contains(&code.as_str()) && !param.is_empty() {
            names.insert(param.clone());
        }
    }
    let code = [raw_code, error.get("type")]
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .find(|candidate| pattern::is_error_code(candidate))
        .unwrap_or("")
        .to_owned();
    if names.len() != 1 {
        return None;
    }
    let name = names.into_iter().next()?;
    REPAIRABLE_PARAMETERS
        .iter()
        .find(|(wire, _)| *wire == name)
        .map(|(_, canonical)| (*canonical, code))
}

/// `_route_unavailable_evidence` → `(error_code, gateway_message)`.
fn route_unavailable_evidence(
    error: &Map<String, Value>,
    err_msg: &str,
) -> Option<(&'static str, String)> {
    let message = error_projection(error, err_msg);
    let evidence = if error.get("code").and_then(Value::as_str) == Some("model_not_found") {
        "model_not_found"
    } else if pattern::has_distributor_no_channel(message)
        || pattern::has_distributor_no_channel(err_msg)
    {
        "distributor_no_channel"
    } else {
        return None;
    };
    let message = pattern::strip_api_http_prefix(message);
    let gateway = pattern::drop_request_id_suffix(py::strip(message));
    Some((evidence, py::prefix(gateway, 160).to_owned()))
}

const SENSITIVE_INPUT_PREFIX: &str = "user sensitive words detected:";

/// `_classify_http_error(status, err_msg, model, ...)` (always raises).
fn classify_http_error(
    status: i64,
    err_msg: &str,
    error: &Map<String, Value>,
    ctx: &ErrorContext,
) -> Flow<Raised> {
    if invalid_encrypted_content_object(error) {
        return Ok(Raised::with_status(
            "InvalidEncryptedContentError",
            "Responses encrypted state rejected (invalid_encrypted_content); \
             stopped without replay or changes to conversation history."
                .into(),
            400,
        )
        .with_param(encrypted_content_param(error.get("param"))));
    }
    let display = summarize_error_body(err_msg);
    let lower = err_msg.to_lowercase();
    let model = ctx.model;
    let http_reason = |status: i64| Some(format!("HTTP {status}: {}", py::prefix(&display, 180)));
    let short_reason = || Some(py::prefix(&display, 200).to_owned());
    match status {
        429 => {
            if is_subscription_quota_error(err_msg) {
                return plain_rate_limit(
                    display.clone(),
                    RateLimit {
                        status: 429,
                        subscription: true,
                        retry_after: parse_subscription_retry_after(err_msg)?,
                        reason: short_reason(),
                        ..RateLimit::default()
                    },
                );
            }
            let quota = contains_any(&lower, QUOTA_EXHAUSTED_PATTERNS);
            let shared = !quota
                && SHARED_PROJECT_LIMIT_GROUPS
                    .iter()
                    .any(|group| group.iter().all(|p| lower.contains(p)));
            return plain_rate_limit(
                display.clone(),
                RateLimit {
                    status: 429,
                    is_quota: quota,
                    is_shared_contention: shared,
                    reason: short_reason(),
                    ..RateLimit::default()
                },
            );
        }
        402 => {
            return plain_rate_limit(
                display.clone(),
                RateLimit {
                    status: 402,
                    is_quota: true,
                    reason: short_reason(),
                    ..RateLimit::default()
                },
            )
        }
        450 => return Ok(Raised::plain("ContentFilterError", display)),
        401 | 403 => {
            if is_upstream_vendor_transient(err_msg) {
                return plain_rate_limit(
                    display.clone(),
                    RateLimit {
                        status,
                        is_gateway: true,
                        reason: http_reason(status),
                        ..RateLimit::default()
                    },
                );
            }
            return Ok(Raised::with_status("PermissionError_", display, status));
        }
        413 => return Ok(Raised::plain("PromptTooLongError", display)),
        400 => return classify_bad_request(err_msg, &display, &lower, error, ctx),
        _ => {}
    }
    if (500..=599).contains(&status) {
        let normalized = py::strip(pattern::strip_api_http_prefix(&display));
        if normalized
            .to_lowercase()
            .starts_with(SENSITIVE_INPUT_PREFIX)
        {
            let explanation = pattern::before_request_id(normalized);
            let who = if model.is_empty() { "The model" } else { model };
            return Ok(Raised::plain(
                "ModelRefusalError",
                format!(
                    "{who} declined the request (category: user_sensitive_words): {explanation}"
                ),
            )
            .with("model", json!(model))
            .with("category", json!("user_sensitive_words"))
            .with("explanation", json!(explanation)));
        }
        if let Some((error_code, gateway_message)) = route_unavailable_evidence(error, err_msg) {
            let gateway_message = if gateway_message.is_empty() {
                py::prefix(&display, 160).to_owned()
            } else {
                gateway_message
            };
            return Ok(rate_limit(
                "ModelRouteUnavailableError",
                display.clone(),
                RateLimit {
                    status,
                    is_gateway: true,
                    reason: http_reason(status),
                    ..RateLimit::default()
                },
            )?
            .with("model", json!(model))
            .with("error_code", json!(error_code))
            .with("gateway_message", json!(gateway_message)));
        }
    }
    if matches!(status, 502..=504) {
        return plain_rate_limit(
            display.clone(),
            RateLimit {
                status,
                is_gateway: true,
                reason: http_reason(status),
                ..RateLimit::default()
            },
        );
    }
    if status == 404 || status == 422 {
        if status == 404
            && (lower.contains("不支持协议") || lower.contains("does not support protocol"))
        {
            return Ok(Raised::with_status("ModelRouteMissingError", display, 404)
                .with("model", json!(model)));
        }
        return Ok(Raised::with_status("RequestScopedError", display, status));
    }
    if is_subscription_quota_error(err_msg) {
        return plain_rate_limit(
            display.clone(),
            RateLimit {
                status,
                subscription: true,
                retry_after: parse_subscription_retry_after(err_msg)?,
                reason: http_reason(status),
                ..RateLimit::default()
            },
        );
    }
    if status == 500 || status == 529 {
        if status == 500 && pattern::is_wrapped_overload(err_msg) {
            return plain_rate_limit(
                display.clone(),
                RateLimit {
                    status: 500,
                    ..RateLimit::default()
                },
            );
        }
        return Ok(Raised::retryable(display, status));
    }
    Ok(Raised::plain("Exception", display))
}

impl Raised {
    fn with_param(self, param: Value) -> Self {
        if param.is_null() {
            self
        } else {
            self.with("param", param)
        }
    }
}

/// The HTTP 400 branch of `_classify_http_error`.
fn classify_bad_request(
    err_msg: &str,
    display: &str,
    lower: &str,
    error: &Map<String, Value>,
    ctx: &ErrorContext,
) -> Flow<Raised> {
    let display = display.to_owned();
    let model = ctx.model;
    if contains_any(lower, QUOTA_EXHAUSTED_PATTERNS) {
        return plain_rate_limit(
            display.clone(),
            RateLimit {
                status: 400,
                is_quota: true,
                reason: Some(py::prefix(&display, 200).to_owned()),
                ..RateLimit::default()
            },
        );
    }
    if contains_any(lower, ROUTE_MISSING_MARKERS) {
        return Ok(
            Raised::with_status("ModelRouteMissingError", display, 0).with("model", json!(model))
        );
    }
    if let Some(limit) = pattern::token_limit(err_msg, MAX_LEARNED_MODEL_LIMIT) {
        return Ok(model_limit_error(display, limit, ctx));
    }
    if pattern::is_thinking_binding_mismatch(err_msg) {
        return Ok(Raised::plain("ThinkingBlockBindingError", display));
    }
    if let Some((param, code)) = unsupported_request_parameter(error, err_msg) {
        return Ok(Raised::plain("UnsupportedRequestParameterError", display)
            .with("param", json!(param))
            .with("error_code", json!(code)));
    }
    if contains_any(lower, IMAGE_ERROR_PATTERNS) {
        return Ok(Raised::plain("InvalidImageError", display));
    }
    if contains_any(lower, PROMPT_TOO_LONG_PATTERNS) {
        return Ok(Raised::plain("PromptTooLongError", display));
    }
    if contains_any(lower, STREAM_ONLY_PATTERNS) {
        return Ok(Raised::plain("StreamOnlyError", display).with("model", json!(model)));
    }
    let http_reason = Some(format!("HTTP 400: {}", py::prefix(&display, 180)));
    if is_upstream_vendor_transient(err_msg) {
        return plain_rate_limit(
            display,
            RateLimit {
                status: 400,
                is_gateway: true,
                reason: http_reason,
                ..RateLimit::default()
            },
        );
    }
    if is_subscription_quota_error(err_msg) {
        return plain_rate_limit(
            display,
            RateLimit {
                status: 400,
                subscription: true,
                retry_after: parse_subscription_retry_after(err_msg)?,
                reason: http_reason,
                ..RateLimit::default()
            },
        );
    }
    Ok(Raised::plain("BadRequestError", display))
}

/// `_learn_model_limit(model, limit)` + `ModelLimitError(message, ...)`.
fn model_limit_error(message: String, limit: i64, ctx: &ErrorContext) -> Raised {
    let mut raised = Raised::plain("ModelLimitError", message)
        .with("model", json!(ctx.model))
        .with("detected_limit", json!(limit))
        .with("requested_limit", json!(ctx.max_tokens));
    raised.learned_limit = Some(LearnedLimit {
        model: ctx.model.to_owned(),
        limit,
        route_key: String::new(),
    });
    raised
}

const PROMPT_TOO_LONG_PATTERNS: &[&str] = &[
    "prompt is too long",
    "context length exceeded",
    "maximum context length",
    "prompt too long",
    "input too long",
    "exceeds the model",
    "token limit",
    "context_length_exceeded",
    "max_prompt_tokens",
    "request too large",
];

const SSE_QUOTA_PATTERNS: &[&str] = &[
    "too many tokens",
    "too many requests",
    "quota exceeded",
    "rate exceeded",
    "tokens per day",
    "tokens per minute",
    "requests per day",
    "requests per minute",
    "throttling",
    "throttled",
];

const SSE_RETRYABLE_PATTERNS: &[&str] = &[
    "负载较高",
    "server overload",
    "service overload",
    "capacity",
    "try again later",
    "稍后重试",
    "temporarily unavailable",
];

const SSE_NON_RETRYABLE_PATTERNS: &[&str] = &[
    "not support model",
    "invalid api key",
    "unauthorized",
    "forbidden",
    "not found",
    "plan not support",
    "permission denied",
];

/// `REQUEST_REPAIR_ERRORS`.
const REQUEST_REPAIR_ERRORS: &[&str] = &[
    "UnsupportedRequestParameterError",
    "ThinkingBlockBindingError",
];

/// `SSEAccumulator._handle_sse_error`: the exception an embedded error raises.
pub(crate) fn handle_sse_error(error: &Value, ctx: &ErrorContext) -> Stop {
    match classify_sse_error(error, ctx) {
        Ok(raised) => raised.into(),
        Err(stop) => stop,
    }
}

fn classify_sse_error(error: &Value, ctx: &ErrorContext) -> Flow<Raised> {
    let object = error.as_object();
    let raw_text = match object {
        Some(map) => map
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        None => py::str_of(error)?,
    };
    let (err_text, _) = repair_mojibake(&raw_text);
    let lower = err_text.to_lowercase();
    if let Some(map) = object {
        if let Some(status) = provider_error_http_status(map) {
            py::require_integral_numbers(error)?;
            let dumped = dumps_spaced(&json!({ "error": error }));
            let (err_msg, repaired) = repair_mojibake(&dumped);
            let reparsed;
            let projected = if repaired {
                reparsed = match serde_json::from_str::<Value>(&err_msg) {
                    Ok(Value::Object(mut data)) => data.remove("error"),
                    _ => None,
                };
                match &reparsed {
                    Some(Value::Object(error)) => error,
                    _ => return unsupported("repaired error body without an error object"),
                }
            } else {
                map
            };
            let raised = classify_http_error(status, &err_msg, projected, ctx)?;
            if REQUEST_REPAIR_ERRORS.contains(&raised.class) && ctx.produced_output {
                return Ok(Raised::plain("BadRequestError", raised.message));
            }
            return Ok(raised);
        }
    }
    if let Some(limit) = pattern::token_limit(&err_text, MAX_LEARNED_MODEL_LIMIT) {
        return Ok(model_limit_error(
            format!("SSE error (token limit): {err_text}"),
            limit,
            ctx,
        ));
    }
    if contains_any(&lower, PROMPT_TOO_LONG_PATTERNS) {
        return Ok(Raised::plain(
            "PromptTooLongError",
            format!("SSE error: {err_text}"),
        ));
    }
    let err_type = object.and_then(|map| map.get("type"));
    let mut http_code = match object.and_then(|map| map.get("http_code")) {
        None => String::new(),
        Some(value) => py::str_of(value)?,
    };
    if http_code.is_empty() {
        if let Some(code) = pattern::three_digits_after(&lower, "status code", true) {
            http_code = code;
        }
    }
    let non_retryable = contains_any(&lower, SSE_NON_RETRYABLE_PATTERNS);
    let quota = !non_retryable && (http_code == "429" || contains_any(&lower, SSE_QUOTA_PATTERNS));
    let retryable = !non_retryable
        && !quota
        && (err_type.and_then(Value::as_str) == Some("server_error")
            || http_code.starts_with('5')
            || contains_any(&lower, SSE_RETRYABLE_PATTERNS));
    let message = format!("SSE error: {err_text}");
    if quota {
        let reason = format!("HTTP 429: {}", py::prefix(&err_text, 180));
        return plain_rate_limit(
            message,
            RateLimit {
                reason: Some(reason),
                ..RateLimit::default()
            },
        );
    }
    if retryable {
        let status =
            if !http_code.is_empty() && http_code.chars().all(|ch| py::decimal(ch).is_some()) {
                let digits: Vec<char> = http_code.chars().collect();
                if digits.iter().all(|ch| py::decimal(*ch).is_some()) {
                    match py::decimal_value(&digits, i64::MAX) {
                        Some(status) => status,
                        None => return unsupported("status digits beyond i64"),
                    }
                } else {
                    return Ok(Raised::plain(
                        "ValueError",
                        format!(
                            "invalid literal for int() with base 10: {}",
                            py::repr(&http_code)?
                        ),
                    ));
                }
            } else {
                500
            };
        if matches!(status, 502..=504) {
            let reason = format!("HTTP {status}: {}", py::prefix(&err_text, 180));
            return plain_rate_limit(
                message,
                RateLimit {
                    is_gateway: true,
                    reason: Some(reason),
                    ..RateLimit::default()
                },
            );
        }
        return Ok(Raised::retryable(message, status));
    }
    let text = if err_text.is_empty() {
        let or_unknown = |value: String| {
            if value.is_empty() {
                "?".to_owned()
            } else {
                value
            }
        };
        let err_type = match err_type {
            Some(value) if py::truthy(Some(value)) => py::str_of(value)?,
            _ => String::new(),
        };
        format!(
            "<empty error body> sse_type={} http_code={} model={} trace={}",
            or_unknown(err_type),
            or_unknown(http_code),
            if ctx.model.is_empty() { "?" } else { ctx.model },
            ctx.trace_id,
        )
    } else {
        err_text
    };
    Ok(Raised::plain(
        "ProviderStreamError",
        format!("SSE error: {text}"),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mojibake_repair_requires_gained_cjk() {
        let garbled: String = "稍后".bytes().map(char::from).collect();
        assert_eq!(repair_mojibake(&garbled), ("稍后".to_owned(), true));
        assert_eq!(repair_mojibake("café"), ("café".to_owned(), false));
    }

    #[test]
    fn wrapped_overload_spellings() {
        assert!(pattern::is_wrapped_overload("{\"status\" : 529}"));
        assert!(pattern::is_wrapped_overload("Status_Code: \"429\""));
        assert!(!pattern::is_wrapped_overload("status code 500"));
    }

    #[test]
    fn encrypted_content_params_normalize() {
        let param = |text: &str| encrypted_content_param(Some(&json!(text)));
        assert_eq!(
            param("input[007].encrypted_content"),
            json!("input[7].encrypted_content")
        );
        assert_eq!(param("input.3"), json!("input[3]"));
        assert_eq!(param("input"), json!("input"));
        assert_eq!(param("input[1]x"), Value::Null);
    }
}
