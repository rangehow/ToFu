//! Keepalive/liveness sweep, idle parking, `tools/list_changed`, reconnects
//! and the circuit breaker, connect/disconnect ordering
//! (`MCPBridge._keepalive_loop`/`_probe_liveness`/`_park_idle_stdio_server`/
//! `_handle_server_message`/`_reconnect_server`/`_breaker_*`/`connect_all`/
//! `_disconnect_one`/`disconnect_all` in `lib/mcp/client/_bridge.py`).
//!
//! Constants and their environment variables come from `lib/mcp/types.py`.
//! Time is read through [`Bridge::monotonic`]/[`Bridge::wall`], which add a
//! test-only offset ([`Bridge::advance_clock`]) so idle budgets and breaker
//! backoffs are exercised without sleeping; [`Bridge::sweep_once`] runs one
//! maintenance pass now, and [`Bridge::run_maintenance`] is the production
//! task on the oracle's cadence.

use super::{disabled_tools, Bridge, Session};
use crate::errors::CallError;
use crate::rmcp_session::MCP_CONNECT_TIMEOUT;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

/// `MCP_KEEPALIVE_INTERVAL` default (env `TOFU_MCP_KEEPALIVE_INTERVAL`, 0 disables liveness).
pub const MCP_KEEPALIVE_INTERVAL: i64 = 45;
/// `MCP_PING_TIMEOUT` default (env `TOFU_MCP_PING_TIMEOUT`): one liveness probe's bound.
pub const MCP_PING_TIMEOUT: i64 = 10;
/// `MCP_STDIO_IDLE_SECONDS` default (env `TOFU_MCP_STDIO_IDLE_SECONDS`, clamped to a day).
pub const MCP_STDIO_IDLE_SECONDS: i64 = 300;
const MCP_STDIO_IDLE_CEILING: i64 = 24 * 60 * 60;
/// The production parking cadence never polls more often than this (`_maintenance_interval_seconds`).
const MCP_IDLE_SWEEP_SECONDS: f64 = 45.0;
/// `MCP_BREAKER_BASE_BACKOFF` / `MCP_BREAKER_MAX_BACKOFF` defaults.
pub const MCP_BREAKER_BASE_BACKOFF: i64 = 30;
pub const MCP_BREAKER_MAX_BACKOFF: i64 = 600;
/// `MCPBridge._DISCONNECT_TIMEOUT`: the shutdown budget of one server's transport.
pub const MCP_DISCONNECT_TIMEOUT: Duration = Duration::from_secs(5);
/// `MCP_CURRENT_PROTOCOL_VERSION` lives with the compatibility notice.
use super::MCP_CURRENT_PROTOCOL_VERSION;

const LIST_CHANGED: &str = "notifications/tools/list_changed";

fn env_int(name: &str, default: i64) -> i64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(default)
}

pub fn keepalive_interval() -> i64 {
    env_int("TOFU_MCP_KEEPALIVE_INTERVAL", MCP_KEEPALIVE_INTERVAL)
}

pub fn ping_timeout() -> i64 {
    env_int("TOFU_MCP_PING_TIMEOUT", MCP_PING_TIMEOUT)
}

pub fn stdio_idle_seconds() -> i64 {
    env_int("TOFU_MCP_STDIO_IDLE_SECONDS", MCP_STDIO_IDLE_SECONDS).clamp(0, MCP_STDIO_IDLE_CEILING)
}

fn breaker_base() -> i64 {
    env_int("TOFU_MCP_BREAKER_BASE_BACKOFF", MCP_BREAKER_BASE_BACKOFF)
}

fn breaker_max() -> i64 {
    env_int("TOFU_MCP_BREAKER_MAX_BACKOFF", MCP_BREAKER_MAX_BACKOFF)
}

/// `_maintenance_interval_seconds`: the smallest enabled cadence; zero means none.
pub fn maintenance_interval_seconds() -> f64 {
    let mut intervals = Vec::new();
    let keepalive = keepalive_interval();
    if keepalive > 0 {
        intervals.push(keepalive as f64);
    }
    let idle = stdio_idle_seconds();
    if idle > 0 {
        intervals.push(MCP_IDLE_SWEEP_SECONDS.min(idle as f64));
    }
    intervals
        .into_iter()
        .fold(0.0, |acc: f64, v| if acc == 0.0 { v } else { acc.min(v) })
}

/// A server message forwarded from a live session (`_handle_server_message`).
pub(super) struct ServerMessage {
    server: String,
    method: String,
    session: Weak<dyn Session>,
}

/// Lifecycle state that lives outside the bridge's catalog lock.
pub(super) struct Lifecycle {
    clock_offset_nanos: AtomicU64,
    messages: mpsc::UnboundedSender<ServerMessage>,
    inbox: Mutex<Option<mpsc::UnboundedReceiver<ServerMessage>>>,
    stop: CancellationToken,
}

impl Default for Lifecycle {
    fn default() -> Self {
        let (messages, inbox) = mpsc::unbounded_channel();
        Lifecycle {
            clock_offset_nanos: AtomicU64::new(0),
            messages,
            inbox: Mutex::new(Some(inbox)),
            stop: CancellationToken::new(),
        }
    }
}

/// Holds `_active_calls[name]` for one SDK await; releasing records activity.
pub(super) struct ActiveCall<'a> {
    bridge: &'a Bridge,
    server: String,
}

impl Drop for ActiveCall<'_> {
    fn drop(&mut self) {
        let now = self.bridge.monotonic();
        let mut state = self.bridge.lock();
        let remaining = state
            .active_calls
            .get(&self.server)
            .copied()
            .unwrap_or(1)
            .saturating_sub(1);
        if remaining > 0 {
            state.active_calls.insert(self.server.clone(), remaining);
        } else {
            state.active_calls.remove(&self.server);
        }
        state.last_activity.insert(self.server.clone(), now);
    }
}

fn enabled(config: &Value) -> bool {
    config
        .get("enabled")
        .is_none_or(|v| crate::pyos::truthy(Some(v)))
}

fn same_session(a: &Arc<dyn Session>, b: &Arc<dyn Session>) -> bool {
    Arc::ptr_eq(a, b)
}

impl Bridge {
    // ── clock ────────────────────────────────────────────────────────────────

    fn clock_offset(&self) -> Duration {
        Duration::from_nanos(self.lifecycle.clock_offset_nanos.load(Ordering::SeqCst))
    }

    /// `time.monotonic()` plus the test offset.
    pub(super) fn monotonic(&self) -> Instant {
        Instant::now() + self.clock_offset()
    }

    /// `time.time()` plus the test offset.
    fn wall(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
            + self.clock_offset().as_secs_f64()
    }

    /// Test seam: move the bridge's clocks forward (idle budgets, breaker backoff).
    pub fn advance_clock(&self, seconds: f64) {
        let nanos = Duration::from_secs_f64(seconds.max(0.0)).as_nanos() as u64;
        self.lifecycle
            .clock_offset_nanos
            .fetch_add(nanos, Ordering::SeqCst);
    }

    pub(super) fn active_call(&self, server: &str) -> ActiveCall<'_> {
        *self
            .lock()
            .active_calls
            .entry(server.to_string())
            .or_insert(0) += 1;
        ActiveCall {
            bridge: self,
            server: server.to_string(),
        }
    }

    // ── circuit breaker ──────────────────────────────────────────────────────

    /// `_breaker_blocks`: in backoff and the next retry has not arrived.
    pub(super) fn breaker_blocks(&self, name: &str) -> bool {
        let entry = self.lock().breaker.get(name).copied();
        entry.is_some_and(|(_, next)| self.wall() < next)
    }

    /// `get_breaker_state`: `None`, or `{failures, retry_in, next_retry_ts}`.
    pub fn get_breaker_state(&self, name: &str) -> Option<Value> {
        let (failures, next) = self.lock().breaker.get(name).copied()?;
        Some(json!({
            "failures": failures,
            "retry_in": (next - self.wall()).max(0.0),
            "next_retry_ts": next,
        }))
    }

    /// `_breaker_record_failure`: bump the streak, return the backoff applied.
    pub(super) fn breaker_record_failure(&self, name: &str) -> i64 {
        let now = self.wall();
        let (base, max) = (breaker_base(), breaker_max());
        let mut state = self.lock();
        let failures = state.breaker.get(name).map_or(0, |e| e.0) + 1;
        let delay = 2i64
            .checked_pow(failures - 1)
            .and_then(|p| base.checked_mul(p))
            .map_or(max, |d| d.min(max));
        state
            .breaker
            .insert(name.to_string(), (failures, now + delay as f64));
        delay
    }

    /// `_retry_failed_first_connect`: hand a failed first connect to the keepalive's backoff.
    pub fn retry_failed_first_connect(&self, name: &str, config: &Value) {
        self.lock()
            .configs
            .entry(name.to_string())
            .or_insert_with(|| config.clone());
        let delay = self.breaker_record_failure(name);
        self.hooks.log(
            "warning",
            &format!("[MCP] {name} will be retried in {delay}s"),
        );
    }

    // ── connect / disconnect ─────────────────────────────────────────────────

    /// `connect_all`: connect every enabled row of the owner's stored configuration.
    pub async fn connect_all(&self) -> Result<Map<String, Value>, CallError> {
        let rows = self
            .binding
            .ask(
                "mcp.config_rows",
                json!({"owner_user_id": self.owner_user_id}),
            )
            .await?;
        let rows = rows.as_object().cloned().unwrap_or_default();
        let mut result = Map::new();
        if rows.is_empty() {
            self.hooks.log("info", "[MCP] No MCP servers configured");
            return Ok(result);
        }
        for (name, row) in rows {
            if !enabled(&row) {
                self.hooks
                    .log("info", &format!("[MCP] Skipping disabled server: {name}"));
                continue;
            }
            match self.connect_server(&name, row.clone()).await {
                Ok(tools) => {
                    result.insert(name, json!(tools));
                }
                Err(error) => {
                    self.hooks.log(
                        "error",
                        &format!("[MCP] Failed to connect server {name}: {error}"),
                    );
                    self.retry_failed_first_connect(&name, &row);
                }
            }
        }
        Ok(result)
    }

    /// `connect_server` / `_launch_and_register`: replace any live transport,
    /// connect, and publish unless a shutdown crossed the handshake.
    pub async fn connect_server(&self, name: &str, config: Value) -> Result<Vec<String>, String> {
        let now = self.monotonic();
        let (had_old, was_parked, shutdowns) = {
            let mut state = self.lock();
            state.last_activity.insert(name.into(), now);
            (
                state.servers.contains_key(name),
                state.parked.contains(name),
                state.shutdowns,
            )
        };
        if had_old {
            self.hooks.log(
                "info",
                &format!("[MCP] Reconnecting server {name} (was already connected)"),
            );
            if let Err(error) = self.disconnect_one(name, false, was_parked).await {
                self.hooks.log(
                    "warning",
                    &format!("[MCP] Error disconnecting old {name}: {error}"),
                );
            }
        }
        let connected = self
            .connector
            .connect(name, &config, self.owner_user_id)
            .await?;
        if self.lock().shutdowns != shutdowns {
            let _ = tokio::time::timeout(MCP_DISCONNECT_TIMEOUT, connected.session.close()).await;
            return Err("MCP connection completed during shutdown".into());
        }
        let tools: Vec<String> = connected
            .tools
            .iter()
            .filter_map(|t| t.get("name").and_then(Value::as_str))
            .map(str::to_string)
            .collect();
        let session = connected.session.clone();
        self.resolve_reviewed(name, &config).await;
        self.install(name, config.clone(), connected);
        self.hooks.log(
            "info",
            &format!(
                "[MCP] Server {name} connected — {} tools discovered: {}",
                tools.len(),
                tools.join(", ")
            ),
        );
        let now = self.monotonic();
        let reset = {
            let mut state = self.lock();
            state.last_activity.insert(name.into(), now);
            state.started = true;
            let before = disabled_tools(&state.configs, name);
            state.configs.insert(name.into(), config);
            if disabled_tools(&state.configs, name) != before {
                state.generation += 1;
            }
            state.breaker.remove(name).is_some()
        };
        if reset {
            self.hooks.log(
                "info",
                &format!("[MCP] Circuit breaker reset for {name} (reconnected)"),
            );
        }
        self.forward_server_messages(name, &session);
        self.credential_after_connect(name);
        Ok(tools)
    }

    /// `_disconnect_one`: close one transport (bounded). `forget` drops the
    /// breaker and stored configuration (explicit user removal);
    /// `preserve_catalog` keeps the handle and catalog (idle parking).
    /// `Ok(false)` when the transport did not close inside the budget.
    pub async fn disconnect_one(
        &self,
        name: &str,
        forget: bool,
        preserve_catalog: bool,
    ) -> Result<bool, String> {
        if forget && preserve_catalog {
            return Err("forget and preserve_catalog are mutually exclusive".into());
        }
        if !preserve_catalog {
            self.credential_teardown(name);
        }
        let session = {
            let mut state = self.lock();
            let session = if preserve_catalog {
                state.servers.get_mut(name).and_then(|h| h.session.take())
            } else {
                let session = state.servers.remove(name).and_then(|h| h.session);
                let before = state.tool_index.len();
                state
                    .tool_index
                    .retain(|_, info| info["server_name"] != name);
                if state.tool_index.len() != before {
                    state.generation += 1;
                }
                state.parked.remove(name);
                state.last_activity.remove(name);
                state.active_calls.remove(name);
                session
            };
            if forget {
                state.breaker.remove(name);
                state.configs.remove(name);
            }
            state.probe_method.remove(name);
            session
        };
        let Some(session) = session else {
            return Ok(true);
        };
        if tokio::time::timeout(MCP_DISCONNECT_TIMEOUT, session.close())
            .await
            .is_err()
        {
            self.hooks.log(
                "warning",
                &format!(
                    "[MCP] Disconnect {name} did not complete within {}s — force-cancelling owner task",
                    crate::pyjson::float_repr(MCP_DISCONNECT_TIMEOUT.as_secs_f64())
                ),
            );
            return Ok(false);
        }
        Ok(true)
    }

    /// `disconnect_all`: close every transport, then clear all lifecycle state.
    /// A connect still in its handshake is refused when it completes.
    pub async fn disconnect_all(&self) {
        let names: Vec<String> = {
            let mut state = self.lock();
            state.shutdowns += 1;
            state.servers.keys().cloned().collect()
        };
        for name in names {
            match self.disconnect_one(&name, false, false).await {
                Ok(_) => self
                    .hooks
                    .log("info", &format!("[MCP] Disconnected server: {name}")),
                Err(error) => self.hooks.log(
                    "warning",
                    &format!("[MCP] Error disconnecting {name}: {error}"),
                ),
            }
        }
        {
            let mut state = self.lock();
            state.servers.clear();
            state.tool_index.clear();
            state.generation += 1;
            state.breaker.clear();
            state.configs.clear();
            state.probe_method.clear();
            state.parked.clear();
            state.last_activity.clear();
            state.active_calls.clear();
            state.started = false;
        }
        self.credential_clear();
        self.hooks.log("info", "[MCP] All servers disconnected");
    }

    /// Close a server's transport, keeping its catalog until a call wakes it.
    pub async fn park(&self, name: &str) {
        self.lock().parked.insert(name.into());
        let _ = self.disconnect_one(name, false, true).await;
    }

    // ── reconnect ────────────────────────────────────────────────────────────

    /// `_reconnect_server`: one serialized reconnect from the owner's stored
    /// row (else the last-known row). A failure records a breaker backoff; a
    /// row disabled since it connected is forgotten instead of retried.
    pub async fn reconnect_server(&self, name: &str) -> Result<(), String> {
        self.reconnect_server_with(name, None).await
    }

    /// `_reconnect_server(name, stored)`: `stored` is a caller-renewed row.
    pub async fn reconnect_server_with(
        &self,
        name: &str,
        stored: Option<Value>,
    ) -> Result<(), String> {
        let (config, old, failing) = {
            let state = self.lock();
            let config = state
                .servers
                .get(name)
                .map(|h| h.config.clone())
                .or_else(|| state.configs.get(name).cloned());
            let old = state.servers.get(name).and_then(|h| h.session.clone());
            (config, old, state.breaker.contains_key(name))
        };
        let Some(config) = config else {
            return Err(format!("cannot reconnect unknown MCP server: {name}"));
        };
        let config = match self.reconnect_config(name, config, stored, failing).await {
            Ok(config) => config,
            Err(disabled) => {
                let _ = self.disconnect_one(name, true, false).await;
                return Err(disabled);
            }
        };
        let lock = self.reconnect_lock(name);
        let _guard = lock.lock().await;
        let current = self
            .lock()
            .servers
            .get(name)
            .and_then(|h| h.session.clone());
        if let Some(current) = current {
            if old.as_ref().is_none_or(|old| !same_session(old, &current)) {
                self.hooks.log(
                    "info",
                    &format!("[MCP] {name} already reconnected by another caller"),
                );
                return Ok(());
            }
        }
        let mut fields = Map::new();
        fields.insert("server".into(), json!(name));
        self.hooks.audit("mcp_reconnect", fields);
        self.hooks
            .log("info", &format!("[MCP] Reconnecting server {name}"));
        if let Err(error) = self.connect_server(name, config).await {
            let delay = self.breaker_record_failure(name);
            let failures = self.lock().breaker.get(name).map_or(0, |e| e.0);
            self.hooks.log(
                "warning",
                &format!(
                    "[MCP] Reconnect of {name} failed (consecutive={failures}) — backing off {delay}s before next attempt"
                ),
            );
            return Err(error);
        }
        Ok(())
    }

    // ── idle parking ─────────────────────────────────────────────────────────

    /// `_stdio_idle_due`: the sweep's cheap filter (parking rechecks under the lock).
    fn stdio_idle_due(&self, name: &str) -> bool {
        let idle = stdio_idle_seconds();
        if idle <= 0 {
            return false;
        }
        let now = self.monotonic();
        let state = self.lock();
        let Some(handle) = state.servers.get(name) else {
            return false;
        };
        if handle.session.is_none()
            || !crate::transport::is_stdio(&handle.config)
            || state.parked.contains(name)
            || state.active_calls.get(name).copied().unwrap_or(0) > 0
        {
            return false;
        }
        let last = state.last_activity.get(name).copied().unwrap_or(now);
        now.saturating_duration_since(last).as_secs_f64() >= idle as f64
    }

    /// `_park_idle_stdio_server`: close one idle local transport, keeping its
    /// tools; eligibility is rechecked under the reconnect lock so a call that
    /// arrived after the sweep's snapshot vetoes parking.
    pub async fn park_idle_stdio_server(&self, name: &str) -> bool {
        let idle = stdio_idle_seconds();
        if idle <= 0 {
            return false;
        }
        let lock = self.reconnect_lock(name);
        let _guard = lock.lock().await;
        let now = self.monotonic();
        let idle_for = {
            let mut state = self.lock();
            let last = state.last_activity.get(name).copied().unwrap_or(now);
            let idle_for = now.saturating_duration_since(last).as_secs_f64();
            let eligible = state
                .servers
                .get(name)
                .is_some_and(|h| h.session.is_some() && crate::transport::is_stdio(&h.config))
                && !state.parked.contains(name)
                && state.active_calls.get(name).copied().unwrap_or(0) == 0
                && idle_for >= idle as f64;
            if !eligible {
                return false;
            }
            state.parked.insert(name.into());
            idle_for
        };
        if !matches!(self.disconnect_one(name, false, true).await, Ok(true)) {
            let now = self.monotonic();
            let mut state = self.lock();
            state.parked.remove(name);
            state.last_activity.insert(name.into(), now);
            drop(state);
            self.hooks.log(
                "warning",
                &format!("[MCP] Idle parking of {name} exceeded shutdown budget; leaving it eligible for health recovery"),
            );
            return false;
        }
        let mut fields = Map::new();
        fields.insert("server".into(), json!(name));
        fields.insert(
            "idle_seconds".into(),
            json!((idle_for * 10.0).round() / 10.0),
        );
        fields.insert("idle_budget_seconds".into(), json!(idle));
        self.hooks.audit("mcp_stdio_parked", fields);
        self.hooks.log(
            "info",
            &format!("[MCP] Parked idle stdio server {name} after {idle_for:.0}s (catalog retained; next call reconnects)"),
        );
        true
    }

    // ── liveness ─────────────────────────────────────────────────────────────

    /// `_probe_liveness`: did the peer ANSWER one cheap question. A JSON-RPC
    /// error proves the round trip; `-32601` moves on to the next probe; only a
    /// timeout or a transport failure is dead. Candidates follow the
    /// negotiated era (no `ping` for 2026-07-28 or an SDK-v2-style client),
    /// filtered to what the session offers, the memoised probe first.
    pub(super) async fn probe_liveness(
        &self,
        name: &str,
        session: &Arc<dyn Session>,
        protocol_version: &str,
    ) -> bool {
        let modern = crate::pyjson::py_strip(protocol_version) == MCP_CURRENT_PROTOCOL_VERSION;
        let order: &[&str] = if modern || session.sdk_generation() >= 2 {
            &["discover", "send_discover", "list_tools"]
        } else {
            &["discover", "send_discover", "send_ping", "list_tools"]
        };
        let offered = session.probe_methods();
        let preferred = self.lock().probe_method.get(name).cloned();
        let mut candidates: Vec<&str> = Vec::new();
        if let Some(preferred) = preferred.as_deref() {
            if let Some(m) = order.iter().find(|m| **m == preferred) {
                candidates.push(m);
            }
        }
        for method in order {
            if !candidates.contains(method) && offered.contains(method) {
                candidates.push(method);
            }
        }
        if candidates.is_empty() {
            self.hooks.log(
                "debug",
                &format!(
                    "[MCP] {name}: no usable liveness probe on session; skipping health sweep"
                ),
            );
            return true;
        }
        let bound = Duration::from_secs(ping_timeout().max(0) as u64);
        for method in candidates {
            if !offered.contains(&method) {
                continue;
            }
            let failure = match tokio::time::timeout(bound, session.probe(method)).await {
                Ok(Ok(())) => {
                    let changed = {
                        let mut state = self.lock();
                        let changed =
                            state.probe_method.get(name).map(String::as_str) != Some(method);
                        state.probe_method.insert(name.into(), method.into());
                        changed
                    };
                    if changed {
                        self.hooks.log(
                            "info",
                            &format!("[MCP] {name}: health probe resolved to {method}()"),
                        );
                    }
                    return true;
                }
                Ok(Err(error)) if error.is_method_not_found() => {
                    self.lock().probe_method.remove(name);
                    continue;
                }
                Ok(Err(error)) if error.is_peer_answered() => return true,
                Ok(Err(error)) => error.to_string(),
                Err(_) => String::new(),
            };
            self.hooks.log(
                "warning",
                &format!(
                    "[MCP] Health probe {method}() on {name} failed ({failure}) — reconnecting"
                ),
            );
            return false;
        }
        true
    }

    // ── maintenance ──────────────────────────────────────────────────────────

    /// One `_keepalive_loop` pass: park idle stdio servers, probe the rest
    /// (credential sweep on a live answer), reconnect dead or backed-off ones.
    pub async fn sweep_once(&self) {
        let (candidates, parked) = {
            let state = self.lock();
            let candidates: BTreeSet<String> = state
                .servers
                .keys()
                .chain(state.breaker.keys())
                .cloned()
                .collect();
            (candidates, state.parked.clone())
        };
        for name in candidates {
            if parked.contains(&name) || self.breaker_blocks(&name) {
                continue;
            }
            let live = |bridge: &Bridge| {
                let state = bridge.lock();
                state
                    .servers
                    .get(&name)
                    .and_then(|h| h.session.clone().map(|s| (s, h.protocol_version.clone())))
            };
            let mut session = live(self);
            if session.is_some() {
                if self.stdio_idle_due(&name) {
                    if self.park_idle_stdio_server(&name).await {
                        continue;
                    }
                    if self.lock().parked.contains(&name) {
                        continue;
                    }
                    session = live(self);
                }
                if keepalive_interval() <= 0 {
                    continue;
                }
                match session {
                    None => self.hooks.log(
                        "info",
                        &format!("[MCP] Keepalive found {name} without a live session after idle arbitration"),
                    ),
                    Some((session, protocol)) => {
                        if self.probe_liveness(&name, &session, &protocol).await {
                            self.credential_sweep(&name).await;
                            continue;
                        }
                    }
                }
            } else {
                if keepalive_interval() <= 0 {
                    continue;
                }
                self.hooks.log(
                    "info",
                    &format!("[MCP] Keepalive retrying backed-off server {name}"),
                );
            }
            match self.reconnect_server(&name).await {
                Ok(()) => self
                    .hooks
                    .log("info", &format!("[MCP] Keepalive reconnected {name}")),
                Err(error) => self.hooks.log(
                    "debug",
                    &format!("[MCP] Keepalive reconnect of {name} still failing: {error}"),
                ),
            }
        }
    }

    /// The production maintenance task: a sweep every
    /// [`maintenance_interval_seconds`] (none when it is zero) and every
    /// forwarded server message, until `stop`.
    pub async fn run_maintenance(&self, stop: CancellationToken) {
        let inbox = self
            .lifecycle
            .inbox
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .take();
        let sweeps = async {
            loop {
                let interval = maintenance_interval_seconds();
                if interval <= 0.0 {
                    return;
                }
                tokio::time::sleep(Duration::from_secs_f64(interval)).await;
                self.sweep_once().await;
            }
        };
        let messages = async {
            if let Some(mut inbox) = inbox {
                while let Some(message) = inbox.recv().await {
                    self.handle_server_message(message).await;
                }
            }
        };
        tokio::select! {
            () = stop.cancelled() => {}
            _ = futures_join(sweeps, messages) => {}
        }
    }

    /// `_start_keepalive`: the bridge's maintenance task, until [`Bridge::stop_maintenance`].
    pub fn start_maintenance(self: &Arc<Self>) {
        let bridge = self.clone();
        let stop = self.lifecycle.stop.clone();
        tokio::spawn(async move { bridge.run_maintenance(stop).await });
    }

    pub fn stop_maintenance(&self) {
        self.lifecycle.stop.cancel();
    }

    /// The failure categories of a JSON-RPC error response with `code` and
    /// `message` (`_is_call_timeout_error`/`_is_transport_dead_error`/
    /// `_is_peer_answered_error`/`_is_method_not_found`). A code that is not a
    /// JSON integer is no protocol reply (`_jsonrpc_error_code` is None).
    pub fn classify_error(code: &Value, message: &str) -> Value {
        let error = match code.as_i64().filter(|_| code.is_i64() || code.is_u64()) {
            Some(code) if i32::try_from(code).is_ok() => {
                CallError::from(rmcp::ServiceError::McpError(rmcp::ErrorData::new(
                    rmcp::model::ErrorCode(code as i32),
                    message.to_string(),
                    None,
                )))
            }
            _ => CallError::Error(message.to_string()),
        };
        json!({
            "timeout": error.is_call_timeout(),
            "dead": error.is_transport_dead(),
            "peer_answered": error.is_peer_answered(),
            "method_not_found": error.is_method_not_found(),
            "kind": error.kind(),
        })
    }

    // ── tools/list_changed ───────────────────────────────────────────────────

    fn forward_server_messages(&self, name: &str, session: &Arc<dyn Session>) {
        let Some(mut receiver) = session.take_notifications() else {
            return;
        };
        let sender = self.lifecycle.messages.clone();
        let server = name.to_string();
        let weak = Arc::downgrade(session);
        tokio::spawn(async move {
            while let Some(method) = receiver.recv().await {
                let message = ServerMessage {
                    server: server.clone(),
                    method,
                    session: weak.clone(),
                };
                if sender.send(message).is_err() {
                    return;
                }
            }
        });
    }

    /// `_handle_server_message`: a `tools/list_changed` from the live session
    /// re-lists and replaces that server's catalog (generation bump on change).
    async fn handle_server_message(&self, message: ServerMessage) {
        if message.method != LIST_CHANGED {
            return;
        }
        let Some(session) = message.session.upgrade() else {
            return;
        };
        let name = message.server;
        let declared = {
            let state = self.lock();
            match state.servers.get(&name) {
                Some(h)
                    if h.session
                        .as_ref()
                        .is_some_and(|s| same_session(s, &session)) =>
                {
                    h.tools_list_changed
                }
                _ => return,
            }
        };
        if !declared {
            self.hooks.log(
                "warning",
                &format!("[MCP] {name} sent tools/list_changed without declaring tools.listChanged=true; refreshing defensively"),
            );
        }
        if let Err(error) = self.refresh_tool_catalog(&name, &session).await {
            self.hooks.log(
                "warning",
                &format!("[MCP] tools/list_changed refresh failed for {name}: {error}"),
            );
        }
    }

    /// `_async_refresh_tool_catalog`.
    async fn refresh_tool_catalog(
        &self,
        name: &str,
        session: &Arc<dyn Session>,
    ) -> Result<bool, CallError> {
        let (tools, version) = tokio::time::timeout(MCP_CONNECT_TIMEOUT, session.list_tools())
            .await
            .map_err(|_| CallError::Timeout("tools/list timed out".into()))??;
        let config = self
            .lock()
            .servers
            .get(name)
            .map(|h| h.config.clone())
            .unwrap_or(Value::Null);
        let mut reviewed = self.hooks.reviewed_read_only(name, &config);
        reviewed.extend(self.registry_reviewed(name));
        let changed =
            Self::replace_catalog_locked(&mut self.lock(), name, &tools, &version, &reviewed);
        self.hooks.log(
            "info",
            &format!(
                "[MCP] Server {name} tools/list_changed: {} ({} tools)",
                if changed { "catalog rebuilt" } else { "no-op" },
                tools.len()
            ),
        );
        Ok(changed)
    }

    /// Lifecycle facts of one server, for the host and the parity tests.
    pub fn lifecycle_state(&self, name: &str) -> Value {
        let state = self.lock();
        json!({
            "connected": state.servers.get(name).is_some_and(|h| h.session.is_some()),
            "parked": state.parked.contains(name),
            "configured": state.configs.contains_key(name),
            "active_calls": state.active_calls.get(name).copied().unwrap_or(0),
            "probe_method": state.probe_method.get(name),
        })
    }
}

async fn futures_join(
    a: impl std::future::Future<Output = ()>,
    b: impl std::future::Future<Output = ()>,
) {
    tokio::join!(a, b);
}
