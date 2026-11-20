//! Outbound HTTP through Tofu's shared network authority (`docs/NETWORK.md`).
//!
//! The route plan and route health stay with the Python authority
//! (`lib/proxy.py` `network_route_candidates` / `report_network_route`); this
//! module asks the binding for them (`callback.rs`) and never selects a route
//! itself. Per request it mirrors `RoutingAsyncTransport.handle_async_request`
//! (`lib/http_transport/outbound_async.py`): routes are tried once each in plan
//! order, only a proven connection-setup failure (reqwest `is_connect`: DNS,
//! TCP, TLS or proxy CONNECT before any request byte was written) is reported
//! as a failure and advances to the next route, any delivered response is
//! reported `ok` with its latency, and status codes, read timeouts and
//! ambiguous disconnects are never replayed. Exhausting the plan is
//! [`NetworkError::RoutesExhausted`].
//!
//! [`RoutedHttpClient`] is rmcp's `StreamableHttpClient` over that loop: one
//! reqwest client per proxy route (idle pools only, no active-request limit),
//! delegating each attempt to rmcp's own reqwest implementation so the
//! Streamable HTTP wire handling stays rmcp's.
//!
//! Standalone use: with no binding attached (`CallError::Unavailable`), the
//! client receives no policy and takes one direct route without reading proxy
//! environment variables, and reports nothing (`docs/NETWORK.md`, "Standalone
//! SDKs ... receive policy through explicit composition ports": the composition
//! port here is the binding). Any other binding failure fails the request
//! closed rather than bypassing the authority's proxy policy.

use crate::callback::BindingCalls;
use crate::errors::CallError;
use futures::stream::BoxStream;
use http::{HeaderName, HeaderValue};
use rmcp::model::ClientJsonRpcMessage;
use rmcp::transport::streamable_http_client::{
    AuthRequiredError, InsufficientScopeError, SseError, StreamableHttpClient, StreamableHttpError,
    StreamableHttpPostResponse, StreamableHttpResponseLimits,
};
use serde_json::{json, Value};
use sse_stream::Sse;
use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// `mcp.shared._httpx_utils.MCP_DEFAULT_TIMEOUT` (connect/write/pool).
pub const MCP_DEFAULT_TIMEOUT: Duration = Duration::from_secs(30);
/// `mcp.shared._httpx_utils.MCP_DEFAULT_SSE_READ_TIMEOUT` (per read).
pub const MCP_DEFAULT_SSE_READ_TIMEOUT: Duration = Duration::from_secs(300);
/// rmcp's default client redirect bound (`MAX_REDIRECTS`), kept for its same-origin policy.
const MAX_REDIRECTS: usize = 10;

/// One concrete route of the authority's plan (`lib.network_routes.Route`).
#[derive(Clone, Debug)]
pub struct Route {
    pub route_id: String,
    /// `Route.async_proxy_url()`: `None` is a direct connection.
    pub proxy_url: Option<String>,
    /// The authority's own description, echoed back when reporting.
    pub raw: Value,
}

#[derive(Debug)]
pub enum NetworkError {
    Http(reqwest::Error),
    /// Every route in the plan failed connection setup (`RoutesExhausted`).
    RoutesExhausted,
    /// The binding could not produce a plan (fail closed).
    Authority(String),
}

impl std::fmt::Display for NetworkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NetworkError::Http(e) => std::fmt::Display::fmt(e, f),
            // The oracle's `Exhausted('No configured network route connected')`.
            NetworkError::RoutesExhausted => f.write_str("No configured network route connected"),
            NetworkError::Authority(m) => write!(f, "network route authority failed: {m}"),
        }
    }
}

impl std::error::Error for NetworkError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            NetworkError::Http(e) => Some(e),
            _ => None,
        }
    }
}

impl NetworkError {
    /// A connect-class failure (the oracle's `httpx.ConnectError` family).
    pub fn is_connect(&self) -> bool {
        match self {
            NetworkError::Http(e) => e.is_connect(),
            NetworkError::RoutesExhausted => true,
            NetworkError::Authority(_) => false,
        }
    }
}

/// What one attempt proved about delivery.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    /// Connection setup failed before any request byte: safe to try the next route.
    ConnectFailed,
    /// A response arrived (any status): the route works.
    Responded,
    /// The request may have been delivered (read timeout, reset): never replay.
    Unknown,
}

/// Classify a reqwest failure.
pub fn reqwest_delivery(error: &reqwest::Error) -> Delivery {
    if error.is_connect() {
        Delivery::ConnectFailed
    } else if error.status().is_some() || error.is_body() || error.is_decode() {
        Delivery::Responded
    } else {
        Delivery::Unknown
    }
}

/// Classify a failure of rmcp's reqwest Streamable HTTP client.
fn streamable_delivery(error: &StreamableHttpError<NetworkError>) -> Delivery {
    match error {
        StreamableHttpError::Client(NetworkError::Http(e)) => reqwest_delivery(e),
        StreamableHttpError::Client(_) => Delivery::Unknown,
        // Raised before sending (a header rmcp reserves): nothing was delivered.
        StreamableHttpError::ReservedHeaderConflict(_) => Delivery::Unknown,
        StreamableHttpError::Io(_) | StreamableHttpError::TransportChannelClosed => {
            Delivery::Unknown
        }
        _ => Delivery::Responded,
    }
}

/// The same-origin 307/308 redirect policy of rmcp's default client: a
/// cross-origin hop would replay custom (credential) headers to another host.
fn same_origin_redirect_policy() -> reqwest::redirect::Policy {
    reqwest::redirect::Policy::custom(|attempt| {
        let preserves_body = matches!(
            attempt.status(),
            reqwest::StatusCode::TEMPORARY_REDIRECT | reqwest::StatusCode::PERMANENT_REDIRECT
        );
        let same_origin = attempt
            .previous()
            .first()
            .is_some_and(|original| original.origin() == attempt.url().origin());
        if !preserves_body || !same_origin {
            attempt.stop()
        } else if attempt.previous().len() > MAX_REDIRECTS {
            attempt.error(format!("exceeded {MAX_REDIRECTS} redirects"))
        } else {
            attempt.follow()
        }
    })
}

fn build_client(proxy_url: Option<&str>) -> Result<reqwest::Client, reqwest::Error> {
    // `trust_env=False`: the route plan, not the environment, chooses the proxy.
    let mut builder = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(MCP_DEFAULT_TIMEOUT)
        .read_timeout(MCP_DEFAULT_SSE_READ_TIMEOUT)
        .redirect(same_origin_redirect_policy());
    if let Some(proxy) = proxy_url {
        builder = builder.proxy(reqwest::Proxy::all(proxy)?);
    }
    builder.build()
}

fn route_from(value: &Value) -> Option<Route> {
    let route_id = value.get("route_id")?.as_str()?.to_string();
    let proxy_url = value
        .get("proxy_url")
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty())
        .map(str::to_string);
    Some(Route {
        route_id,
        proxy_url,
        raw: value.clone(),
    })
}

/// Route admission and per-route client pools for one owner's outbound HTTP.
pub struct NetworkClient {
    binding: Arc<dyn BindingCalls>,
    scope: Option<String>,
    pools: Mutex<HashMap<Option<String>, reqwest::Client>>,
}

enum Plan {
    Routes(Vec<Route>),
    /// No binding: one direct route, nothing reported.
    Standalone,
}

impl NetworkClient {
    pub fn new(binding: Arc<dyn BindingCalls>, scope: Option<String>) -> NetworkClient {
        NetworkClient {
            binding,
            scope,
            pools: Mutex::new(HashMap::new()),
        }
    }

    fn client(&self, proxy_url: Option<&str>) -> Result<reqwest::Client, NetworkError> {
        let mut pools = self.pools.lock().unwrap_or_else(|p| p.into_inner());
        let key = proxy_url.map(str::to_string);
        if let Some(client) = pools.get(&key) {
            return Ok(client.clone());
        }
        let client = build_client(proxy_url).map_err(NetworkError::Http)?;
        pools.insert(key, client.clone());
        Ok(client)
    }

    async fn plan(&self, url: &str) -> Result<Plan, NetworkError> {
        let answer = self
            .binding
            .ask(
                "network.route_candidates",
                json!({"url": url, "scope": self.scope}),
            )
            .await;
        match answer {
            Ok(Value::Array(rows)) => {
                Ok(Plan::Routes(rows.iter().filter_map(route_from).collect()))
            }
            Ok(other) => Err(NetworkError::Authority(format!(
                "route plan is not a list: {other}"
            ))),
            Err(CallError::Unavailable(_)) => Ok(Plan::Standalone),
            Err(error) => Err(NetworkError::Authority(error.message().to_string())),
        }
    }

    async fn report(&self, url: &str, route: &Route, ok: bool, latency_ms: Option<f64>) {
        // A report the binding cannot take changes nothing about this request.
        let _ = self
            .binding
            .ask(
                "network.report",
                json!({"url": url, "route_id": route.route_id, "route": route.raw,
                       "ok": ok, "latency_ms": latency_ms}),
            )
            .await;
    }

    /// Run `send` over the plan for `url`. `classify` says what a failure proved;
    /// `lift` turns a routing failure into the caller's error type.
    pub async fn attempt<T, E, F, Fut>(
        &self,
        url: &str,
        mut send: F,
        classify: impl Fn(&E) -> Delivery,
        lift: impl Fn(NetworkError) -> E,
    ) -> Result<T, E>
    where
        F: FnMut(reqwest::Client) -> Fut,
        Fut: Future<Output = Result<T, E>>,
    {
        let mut routes = match self.plan(url).await.map_err(&lift)? {
            Plan::Standalone => {
                let client = self.client(None).map_err(&lift)?;
                return send(client).await;
            }
            Plan::Routes(routes) => routes,
        };
        let mut attempted: HashSet<String> = HashSet::new();
        let mut index = 0;
        while index < routes.len() {
            let route = routes[index].clone();
            index += 1;
            if !attempted.insert(route.route_id.clone()) {
                continue;
            }
            let client = self.client(route.proxy_url.as_deref()).map_err(&lift)?;
            let started = Instant::now();
            let outcome = send(client).await;
            let latency = started.elapsed().as_secs_f64() * 1000.0;
            let delivery = match &outcome {
                Ok(_) => Delivery::Responded,
                Err(error) => classify(error),
            };
            match delivery {
                Delivery::ConnectFailed => {
                    self.report(url, &route, false, None).await;
                    // `append_untried`: the authority may now offer other routes.
                    if let Ok(Plan::Routes(fresh)) = self.plan(url).await {
                        let mut known: HashSet<String> =
                            routes.iter().map(|r| r.route_id.clone()).collect();
                        for candidate in fresh {
                            if !attempted.contains(&candidate.route_id)
                                && known.insert(candidate.route_id.clone())
                            {
                                routes.push(candidate);
                            }
                        }
                    }
                    continue;
                }
                Delivery::Responded => self.report(url, &route, true, Some(latency)).await,
                Delivery::Unknown => {}
            }
            return outcome;
        }
        Err(lift(NetworkError::RoutesExhausted))
    }

    /// One request whose response headers decide delivery (used for plain GETs).
    pub async fn send(
        &self,
        url: &str,
        build: impl Fn(&reqwest::Client) -> reqwest::RequestBuilder,
    ) -> Result<reqwest::Response, NetworkError> {
        self.attempt(
            url,
            |client| {
                let request = build(&client);
                async move { request.send().await.map_err(NetworkError::Http) }
            },
            |error| match error {
                NetworkError::Http(e) => reqwest_delivery(e),
                _ => Delivery::Unknown,
            },
            |error| error,
        )
        .await
    }
}

/// The host process's client for its own requests (registry lookups, `network_get`).
pub fn host_network(binding: Arc<dyn BindingCalls>) -> Arc<NetworkClient> {
    static NETWORK: std::sync::OnceLock<Arc<NetworkClient>> = std::sync::OnceLock::new();
    NETWORK
        .get_or_init(|| Arc::new(NetworkClient::new(binding, None)))
        .clone()
}

/// Host method `network_get {url, headers}`: one GET through the route plan.
/// Answers `{status, body}`; failures are scrubbed of credentials.
pub async fn host_get(binding: Arc<dyn BindingCalls>, params: &Value) -> Result<Value, CallError> {
    let url = params
        .get("url")
        .and_then(Value::as_str)
        .ok_or_else(|| CallError::Invalid("missing string parameter url".into()))?;
    let headers: Vec<(String, String)> = params
        .get("headers")
        .and_then(Value::as_object)
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default();
    let scrub = |error: &dyn std::fmt::Display| crate::transport::scrub_text(&error.to_string());
    let response = host_network(binding)
        .send(url, |client| {
            headers
                .iter()
                .fold(client.get(url), |request, (k, v)| request.header(k, v))
        })
        .await
        .map_err(|error| {
            if error.is_connect() {
                CallError::Transport(scrub(&error))
            } else {
                CallError::Error(scrub(&error))
            }
        })?;
    let status = response.status().as_u16();
    match response.text().await {
        Ok(body) => Ok(json!({"status": status, "body": body})),
        Err(error) => Ok(json!({"status": status, "error": scrub(&error)})),
    }
}

/// rmcp's Streamable HTTP client over [`NetworkClient`].
#[derive(Clone)]
pub struct RoutedHttpClient {
    pub network: Arc<NetworkClient>,
}

type SseStream = BoxStream<'static, Result<Sse, SseError>>;

fn lift_error(error: StreamableHttpError<reqwest::Error>) -> StreamableHttpError<NetworkError> {
    use StreamableHttpError as E;
    match error {
        E::Sse(e) => E::Sse(e),
        E::Io(e) => E::Io(e),
        E::Client(e) => E::Client(NetworkError::Http(e)),
        E::HttpStatus(e) => E::HttpStatus(e),
        E::UnexpectedEndOfStream => E::UnexpectedEndOfStream,
        E::UnexpectedServerResponse(m) => E::UnexpectedServerResponse(m),
        E::ResponseBodyTooLarge { max_size } => E::ResponseBodyTooLarge { max_size },
        E::UnexpectedContentType(c) => E::UnexpectedContentType(c),
        E::ServerDoesNotSupportSse => E::ServerDoesNotSupportSse,
        E::ServerDoesNotSupportDeleteSession => E::ServerDoesNotSupportDeleteSession,
        E::TokioJoinError(e) => E::TokioJoinError(e),
        E::Deserialize(e) => E::Deserialize(e),
        E::TransportChannelClosed => E::TransportChannelClosed,
        E::MissingSessionIdInResponse => E::MissingSessionIdInResponse,
        E::AuthRequired(AuthRequiredError {
            www_authenticate_header,
            ..
        }) => E::AuthRequired(AuthRequiredError::new(www_authenticate_header)),
        E::InsufficientScope(InsufficientScopeError {
            www_authenticate_header,
            required_scope,
            ..
        }) => E::InsufficientScope(InsufficientScopeError::new(
            www_authenticate_header,
            required_scope,
        )),
        E::ReservedHeaderConflict(h) => E::ReservedHeaderConflict(h),
        E::SessionExpired => E::SessionExpired,
        E::SessionRecoveryTimeout => E::SessionRecoveryTimeout,
        E::ReinitializationFailed(e) => E::ReinitializationFailed(e),
        E::ControlRequestTimeout => E::ControlRequestTimeout,
        other => E::UnexpectedServerResponse(other.to_string().into()),
    }
}

impl RoutedHttpClient {
    async fn routed<T, F, Fut>(
        &self,
        uri: &str,
        send: F,
    ) -> Result<T, StreamableHttpError<NetworkError>>
    where
        F: FnMut(reqwest::Client) -> Fut,
        Fut: Future<Output = Result<T, StreamableHttpError<NetworkError>>>,
    {
        self.network
            .attempt(uri, send, streamable_delivery, StreamableHttpError::Client)
            .await
    }
}

impl StreamableHttpClient for RoutedHttpClient {
    type Error = NetworkError;

    async fn post_message(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session_id: Option<Arc<str>>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<NetworkError>> {
        self.post_message_with_response_limits(
            uri,
            message,
            session_id,
            auth_header,
            custom_headers,
            StreamableHttpResponseLimits::default(),
        )
        .await
    }

    async fn post_message_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session_id: Option<Arc<str>>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
        max_sse_event_size: usize,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<NetworkError>> {
        let mut limits = StreamableHttpResponseLimits::default();
        limits.max_sse_event_size = max_sse_event_size;
        self.post_message_with_response_limits(
            uri,
            message,
            session_id,
            auth_header,
            custom_headers,
            limits,
        )
        .await
    }

    async fn post_message_with_response_limits(
        &self,
        uri: Arc<str>,
        message: ClientJsonRpcMessage,
        session_id: Option<Arc<str>>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
        limits: StreamableHttpResponseLimits,
    ) -> Result<StreamableHttpPostResponse, StreamableHttpError<NetworkError>> {
        let target = uri.clone();
        self.routed(&target, move |client| {
            let (uri, message, session_id, auth_header, headers) = (
                uri.clone(),
                message.clone(),
                session_id.clone(),
                auth_header.clone(),
                custom_headers.clone(),
            );
            async move {
                client
                    .post_message_with_response_limits(
                        uri,
                        message,
                        session_id,
                        auth_header,
                        headers,
                        limits,
                    )
                    .await
                    .map_err(lift_error)
            }
        })
        .await
    }

    async fn delete_session(
        &self,
        uri: Arc<str>,
        session_id: Arc<str>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<(), StreamableHttpError<NetworkError>> {
        let target = uri.clone();
        self.routed(&target, move |client| {
            let (uri, session_id, auth_header, headers) = (
                uri.clone(),
                session_id.clone(),
                auth_header.clone(),
                custom_headers.clone(),
            );
            async move {
                client
                    .delete_session(uri, session_id, auth_header, headers)
                    .await
                    .map_err(lift_error)
            }
        })
        .await
    }

    async fn get_stream(
        &self,
        uri: Arc<str>,
        session_id: Option<Arc<str>>,
        last_event_id: Option<String>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
    ) -> Result<SseStream, StreamableHttpError<NetworkError>> {
        let target = uri.clone();
        self.routed(&target, move |client| {
            let (uri, session_id, last_event_id, auth_header, headers) = (
                uri.clone(),
                session_id.clone(),
                last_event_id.clone(),
                auth_header.clone(),
                custom_headers.clone(),
            );
            async move {
                client
                    .get_stream(uri, session_id, last_event_id, auth_header, headers)
                    .await
                    .map_err(lift_error)
            }
        })
        .await
    }

    async fn get_stream_with_max_sse_event_size(
        &self,
        uri: Arc<str>,
        session_id: Option<Arc<str>>,
        last_event_id: Option<String>,
        auth_header: Option<String>,
        custom_headers: HashMap<HeaderName, HeaderValue>,
        max_sse_event_size: usize,
    ) -> Result<SseStream, StreamableHttpError<NetworkError>> {
        let target = uri.clone();
        self.routed(&target, move |client| {
            let (uri, session_id, last_event_id, auth_header, headers) = (
                uri.clone(),
                session_id.clone(),
                last_event_id.clone(),
                auth_header.clone(),
                custom_headers.clone(),
            );
            async move {
                client
                    .get_stream_with_max_sse_event_size(
                        uri,
                        session_id,
                        last_event_id,
                        auth_header,
                        headers,
                        max_sse_event_size,
                    )
                    .await
                    .map_err(lift_error)
            }
        })
        .await
    }
}
