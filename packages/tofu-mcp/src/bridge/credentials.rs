//! Credential health probes, renewal and browser credential learning
//! (`MCPBridge.get_cred_health`/`_cred_probe_spec`/`_run_cred_probe`/
//! `_cred_probe_due`/`_probe_cred_health_async`/`_renew_expired_credential`/
//! `_newer_credential`/`_credential_renewal`/`_stored_server_config`/
//! `_auth_failure_detail`/`_credential_followup`/
//! `_learn_browser_credentials_async` in `lib/mcp/client/_bridge.py`, over
//! `lib/mcp/health_probe.py` and `lib/mcp/browser_credentials.py`).
//!
//! Registry facts (a catalog entry's `health_probe`, `env_specs`,
//! `read_only_tools`/`endpoint`) are read live through `registry::get_catalog_entry`
//! (the binding call `mcp.registry_catalog`): the oracle's catalog is
//! assembled at runtime (core `CATALOG`, installed bundle `mcp_catalog/*.json`
//! drop-ins, the opensource-build filter, mtime hot-reload), so a generated
//! copy would differ per installation. Without a binding answer a server has
//! no catalog facts (a custom server's own `health_probe` still applies).

use super::Bridge;
use crate::browser_credentials::{Owners, RefreshOutcome};
use crate::errors::CallError;
use crate::health_probe::{
    classify_probe_result, validate_health_probe, STRUCTURED_EXPIRED_MARKERS,
};
use crate::names::make_namespaced_name;
use crate::result::CallOutput;
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, OnceLock};

/// `MCP_CRED_PROBE_INTERVAL` (lib/mcp/types.py): env `TOFU_MCP_CRED_PROBE_INTERVAL`, default 900 s.
pub fn mcp_cred_probe_interval() -> i64 {
    static CELL: OnceLock<i64> = OnceLock::new();
    *CELL.get_or_init(|| {
        std::env::var("TOFU_MCP_CRED_PROBE_INTERVAL")
            .ok()
            .and_then(|v| v.trim().parse().ok())
            .unwrap_or(900)
    })
}

/// `MCP_CRED_PROBE_TIMEOUT` (lib/mcp/types.py): env
/// `TOFU_MCP_CRED_PROBE_TIMEOUT_SECONDS`, default 30, clamped to 1..=300.
pub fn mcp_cred_probe_timeout() -> i64 {
    static CELL: OnceLock<i64> = OnceLock::new();
    *CELL.get_or_init(|| {
        std::env::var("TOFU_MCP_CRED_PROBE_TIMEOUT_SECONDS")
            .ok()
            .and_then(|v| v.trim().parse::<i64>().ok())
            .unwrap_or(30)
            .clamp(1, 300)
    })
}

fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `registry.reviewed_read_only_tools(server_id, server_config)` over the
/// server's catalog entry: the review applies only at the catalog endpoint.
pub fn reviewed_read_only_tools(entry: Option<&Value>, config: &Value) -> BTreeSet<String> {
    let Some(entry) = entry else {
        return BTreeSet::new();
    };
    let reviewed = entry.get("read_only_tools");
    if !crate::pyos::truthy(reviewed) || !config.is_object() {
        return BTreeSet::new();
    }
    use crate::transport::normalize_transport;
    let url = config
        .get("url")
        .filter(|u| crate::pyos::truthy(Some(u)))
        .map(crate::pyos::py_str)
        .unwrap_or_default();
    if normalize_transport(config) != normalize_transport(entry)
        || entry.get("endpoint").and_then(Value::as_str) != Some(url.as_str())
    {
        return BTreeSet::new();
    }
    reviewed
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(crate::pyos::py_str)
        .collect()
}

fn env_or_empty(row: Option<&Value>) -> Value {
    row.and_then(|r| r.get("env"))
        .filter(|e| crate::pyos::truthy(Some(e)))
        .cloned()
        .unwrap_or_else(|| json!({}))
}

fn enabled(row: &Value) -> bool {
    row.get("enabled")
        .is_none_or(|e| crate::pyos::truthy(Some(e)))
}

/// Credential health and learning state (`_cred_health`, `_cred_probe_ts`,
/// `_cred_probe_inflight`, `_credential_learning_*`).
#[derive(Default)]
pub(super) struct CredentialState {
    health: BTreeMap<String, Value>,
    probe_ts: BTreeMap<String, f64>,
    probe_inflight: BTreeSet<String>,
    learning_inflight: BTreeSet<String>,
    learning_seen: BTreeMap<String, BTreeSet<String>>,
    /// The registry's reviewed reads per server, when the binding answered.
    reviewed: BTreeMap<String, BTreeSet<String>>,
}

type Boxed<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

impl Bridge {
    fn owners(&self) -> Option<Owners<'_>> {
        Some(Owners {
            binding: self.binding.as_ref(),
            hooks: self.hooks.as_ref(),
            owner: self.owner_user_id?,
        })
    }

    fn shared_self(&self) -> Option<Arc<Bridge>> {
        self.me.get().and_then(std::sync::Weak::upgrade)
    }

    fn status_of(&self, name: &str) -> Option<String> {
        self.lock()
            .credentials
            .health
            .get(name)
            .and_then(|h| h.get("status"))
            .and_then(Value::as_str)
            .map(str::to_string)
    }

    /// `get_cred_health`: the last credential verdict, or None when never probed.
    pub fn get_cred_health(&self, name: &str) -> Option<Value> {
        self.lock().credentials.health.get(name).cloned()
    }

    /// Test seam for the re-probe interval: age the last probe by `seconds`.
    pub fn age_cred_probe(&self, name: &str, seconds: f64) {
        if let Some(ts) = self.lock().credentials.probe_ts.get_mut(name) {
            *ts -= seconds;
        }
    }

    /// `_cred_probe_spec`: the live config's `health_probe`, else the catalog's, normalized.
    pub async fn cred_probe_spec(&self, name: &str) -> Option<Value> {
        let config = {
            let state = self.lock();
            state
                .servers
                .get(name)
                .map(|h| h.config.clone())
                .or_else(|| state.configs.get(name).cloned())
        };
        let mut raw = config
            .as_ref()
            .and_then(|c| c.get("health_probe"))
            .filter(|r| !r.is_null())
            .cloned();
        if raw.is_none() {
            match crate::registry::get_catalog_entry(self.binding.as_ref(), name).await {
                Ok(entry) => raw = entry.and_then(|e| e.get("health_probe").cloned()),
                Err(error) => self.hooks.log(
                    "debug",
                    &format!("[MCP] cred-probe catalog lookup for {name} failed: {error}"),
                ),
            }
        }
        let hooks = self.hooks.clone();
        validate_health_probe(raw.as_ref(), name, &mut |w| hooks.log("warning", &w))
    }

    /// `_run_cred_probe(name)`: probe, record and (when expired) renew.
    pub async fn run_cred_probe(&self, name: &str) -> Option<Value> {
        self.run_cred_probe_with(name, true).await
    }

    fn run_cred_probe_with<'a>(&'a self, name: &'a str, recover: bool) -> Boxed<'a, Option<Value>> {
        Box::pin(async move {
            let spec = self.cred_probe_spec(name).await?;
            if !self.lock().servers.contains_key(name) {
                return None;
            }
            let tool = spec["tool"].as_str().unwrap_or_default().to_string();
            let options = super::CallOptions {
                timeout_override: Some(mcp_cred_probe_timeout()),
                ..Default::default()
            };
            let (status, detail) = match self
                .call_tool(
                    &make_namespaced_name(name, &tool),
                    spec["args"].clone(),
                    options,
                )
                .await
            {
                Ok(output) => {
                    let (status, detail) = classify_probe_result(output.text(), &spec);
                    if status == "expired" {
                        self.hooks.log(
                            "warning",
                            &format!("[MCP] Credential probe for {name} → EXPIRED ({detail})"),
                        );
                    } else {
                        self.hooks
                            .log("debug", &format!("[MCP] Credential probe for {name} → ok"));
                    }
                    (status, detail)
                }
                Err(error) => {
                    self.hooks.log(
                        "debug",
                        &format!("[MCP] Credential probe for {name} raised ({error}) — reported as unknown"),
                    );
                    (
                        "unknown",
                        crate::pyjson::py_prefix(error.message(), 200).to_string(),
                    )
                }
            };
            let checked_at = now();
            let record = json!({"status": status, "checked_at": checked_at, "detail": detail});
            {
                let mut state = self.lock();
                state.credentials.health.insert(name.into(), record.clone());
                state.credentials.probe_ts.insert(name.into(), checked_at);
            }
            if status == "expired" {
                let mut fields = Map::new();
                fields.insert("server".into(), json!(name));
                fields.insert("tool".into(), json!(tool));
                self.hooks.audit("mcp_credential_expired", fields);
                if recover {
                    return Some(self.renew_expired_credential(name).await.unwrap_or(record));
                }
            }
            Some(record)
        })
    }

    /// `_renew_expired_credential`: reconnect with a newer credential, then re-probe.
    async fn renew_expired_credential(&self, name: &str) -> Option<Value> {
        let newer = self.newer_credential(name).await?;
        self.hooks.log(
            "info",
            &format!("[MCP] Reconnecting {name} with a renewed credential"),
        );
        if let Err(error) = self.renewal_reconnect(name, Some(newer)).await {
            self.hooks.log(
                "warning",
                &format!("[MCP] Credential renewal for {name} failed: {error}"),
            );
            return None;
        }
        self.run_cred_probe_with(name, false).await
    }

    /// `_reconnect_server(name, stored)` with a renewed row: the verdict about
    /// the replaced credential goes, and the reconnect starts from that row.
    pub(super) async fn renewal_reconnect(
        &self,
        name: &str,
        stored: Option<Value>,
    ) -> Result<(), String> {
        self.reconnect_server_with(name, stored).await
    }

    /// `_newer_credential`.
    pub(super) async fn newer_credential(&self, name: &str) -> Option<Value> {
        self.credential_renewal(name).await.0
    }

    /// `_credential_renewal`: the row to reconnect with and the refresh outcome explaining a None.
    async fn credential_renewal(&self, name: &str) -> (Option<Value>, Option<RefreshOutcome>) {
        let live = {
            let state = self.lock();
            let expired = state
                .credentials
                .health
                .get(name)
                .and_then(|h| h.get("status"))
                == Some(&json!("expired"));
            if !expired {
                return (None, None);
            }
            state
                .servers
                .get(name)
                .map(|h| h.config.clone())
                .or_else(|| state.configs.get(name).cloned())
        };
        let (stored, outcome) = self.stored_credential_config(name, true).await;
        let renewed = outcome.as_ref().is_some_and(|o| o.reason == "renewed");
        match stored {
            Some(row)
                if enabled(&row)
                    && (renewed || env_or_empty(Some(&row)) != env_or_empty(live.as_ref())) =>
            {
                (Some(row), outcome)
            }
            _ => (None, outcome),
        }
    }

    /// `_stored_server_config`: the owner's stored row (after a browser refresh when asked).
    async fn stored_credential_config(
        &self,
        name: &str,
        refresh_browser: bool,
    ) -> (Option<Value>, Option<RefreshOutcome>) {
        let Some(owners) = self.owners() else {
            return (None, None);
        };
        let read = if refresh_browser {
            owners
                .refresh_from_browser(name)
                .await
                .map(|outcome| (outcome.row.clone(), Some(outcome)))
        } else {
            owners.stored_row(name).await.map(|row| (row, None))
        };
        match read {
            Ok(found) => found,
            Err(error) => {
                self.hooks.log(
                    "warning",
                    &format!("[MCP] Stored config for {name} unavailable ({error}); using the last-known config"),
                );
                (None, None)
            }
        }
    }

    /// `_auth_failure_detail`: a snippet when a result reports a rejected credential.
    async fn auth_failure_detail(&self, server: &str, output: &CallOutput) -> Option<String> {
        let text = output.text();
        let is_error = matches!(output, CallOutput::Result(r) if r.is_error);
        let low = text.to_lowercase();
        if !(is_error || STRUCTURED_EXPIRED_MARKERS.iter().any(|m| low.contains(m))) {
            return None;
        }
        let spec = self
            .cred_probe_spec(server)
            .await
            .unwrap_or_else(|| json!({}));
        let (status, detail) = classify_probe_result(text, &spec);
        (status == "expired").then_some(detail)
    }

    /// `_credential_followup`: renew a browser-held credential a result rejected.
    /// `(renewed, notice)`; None when the result is not such a failure.
    pub(super) async fn credential_followup(
        &self,
        server: &str,
        tool: &str,
        output: &CallOutput,
    ) -> Option<(bool, String)> {
        let detail = self.auth_failure_detail(server, output).await?;
        let owners = self.owners()?;
        let row = self.stored_credential_config(server, false).await.0?;
        if owners
            .credential_bindings(server, &row)
            .await
            .map_or(true, |b| b.is_empty())
        {
            return None;
        }
        self.hooks.log(
            "warning",
            &format!("[MCP] {server}.{tool} rejected its browser-held credential ({detail})"),
        );
        self.lock().credentials.health.insert(
            server.into(),
            json!({"status": "expired", "checked_at": now(), "detail": detail}),
        );
        let mut fields = Map::new();
        fields.insert("server".into(), json!(server));
        fields.insert("tool".into(), json!(tool));
        self.hooks.audit("mcp_credential_expired", fields);
        let (newer, outcome) = self.credential_renewal(server).await;
        let Some(newer) = newer else {
            let reason = outcome.map_or("unbound", |o| o.reason);
            let notice = owners
                .failure_notice(server, &row, false, reason)
                .await
                .unwrap_or_default();
            return Some((false, notice));
        };
        if let Err(error) = self.renewal_reconnect(server, Some(newer.clone())).await {
            self.hooks.log(
                "warning",
                &format!("[MCP] Credential renewal for {server} failed: {error}"),
            );
            return Some((false, String::new()));
        }
        let notice = owners
            .failure_notice(server, &newer, true, "renewed")
            .await
            .unwrap_or_default();
        Some((true, notice))
    }

    /// One `learn_bindings` pass for `name` now (`_learn_browser_credentials_async`'s
    /// worker); false when unowned or a pass is already running.
    pub async fn learn_browser_credentials(&self, name: &str) -> bool {
        let Some(seen) = self.claim_learning(name) else {
            return false;
        };
        self.learning_pass(name, seen).await;
        true
    }

    /// Claim the per-server learning pass: the fingerprints already answered for.
    fn claim_learning(&self, name: &str) -> Option<BTreeSet<String>> {
        self.owner_user_id?;
        let mut state = self.lock();
        if !state.credentials.learning_inflight.insert(name.into()) {
            return None;
        }
        Some(
            state
                .credentials
                .learning_seen
                .get(name)
                .cloned()
                .unwrap_or_default(),
        )
    }

    async fn learning_pass(&self, name: &str, seen: BTreeSet<String>) {
        let Some(owners) = self.owners() else {
            return;
        };
        match owners.learn_bindings(name, &seen).await {
            Ok(answered) if !answered.is_empty() => {
                self.lock()
                    .credentials
                    .learning_seen
                    .entry(name.into())
                    .or_default()
                    .extend(answered);
            }
            Ok(_) => {}
            Err(error) => self.hooks.log(
                "debug",
                &format!("[MCP] browser credential learning for {name} failed: {error}"),
            ),
        }
        self.lock().credentials.learning_inflight.remove(name);
    }

    /// `_cred_probe_due`: a declared probe whose interval elapsed and none in flight.
    pub async fn cred_probe_due(&self, name: &str) -> bool {
        let interval = mcp_cred_probe_interval();
        if interval <= 0 || self.cred_probe_spec(name).await.is_none() {
            return false;
        }
        let state = self.lock();
        let last = state.credentials.probe_ts.get(name).copied().unwrap_or(0.0);
        !state.credentials.probe_inflight.contains(name) && now() - last >= interval as f64
    }

    /// `_probe_cred_health_async`: one singleflight probe, in the background when
    /// the bridge is shared (else awaited); false when nothing was started.
    pub async fn probe_cred_health_async(&self, name: &str) -> bool {
        if mcp_cred_probe_interval() < 0 || self.cred_probe_spec(name).await.is_none() {
            return false;
        }
        if !self.lock().credentials.probe_inflight.insert(name.into()) {
            return false;
        }
        let name = name.to_string();
        match self.shared_self() {
            Some(bridge) => {
                tokio::spawn(async move {
                    bridge.run_cred_probe(&name).await;
                    bridge.lock().credentials.probe_inflight.remove(&name);
                });
            }
            None => {
                self.run_cred_probe(&name).await;
                self.lock().credentials.probe_inflight.remove(&name);
            }
        }
        true
    }

    /// The lifecycle sweep's credential step for a server whose peer answered.
    pub(crate) async fn credential_sweep(&self, server: &str) {
        if self.cred_probe_due(server).await {
            self.probe_cred_health_async(server).await;
        }
    }

    /// What credential work runs now: probes and learning passes in flight.
    pub fn credential_inflight(&self) -> Value {
        let state = self.lock();
        json!({"probes": state.credentials.probe_inflight, "learning": state.credentials.learning_inflight})
    }

    /// After a successful (re)connect: the background credential probe and learning pass.
    pub(super) fn credential_after_connect(&self, name: &str) {
        let Some(bridge) = self.shared_self() else {
            return;
        };
        let probe = mcp_cred_probe_interval() >= 0
            && self.lock().credentials.probe_inflight.insert(name.into());
        let learning = self.claim_learning(name);
        let name = name.to_string();
        tokio::spawn(async move {
            if probe {
                if bridge.cred_probe_spec(&name).await.is_some() {
                    bridge.run_cred_probe(&name).await;
                }
                bridge.lock().credentials.probe_inflight.remove(&name);
            }
            if let Some(seen) = learning {
                bridge.learning_pass(&name, seen).await;
            }
        });
    }

    /// Test seam: record a credential verdict as the oracle's tests write `_cred_health`.
    pub fn record_cred_health(&self, name: &str, status: &str) {
        self.lock().credentials.health.insert(
            name.into(),
            json!({"status": status, "checked_at": now(), "detail": ""}),
        );
    }

    /// `_disconnect_one` without `preserve_catalog`: credential health is
    /// transient live state (a parked server keeps its last verdict).
    pub(super) fn credential_teardown(&self, name: &str) {
        let mut state = self.lock();
        state.credentials.health.remove(name);
        state.credentials.probe_ts.remove(name);
    }

    /// `disconnect_all`: credential health is transient live state.
    pub(super) fn credential_clear(&self) {
        let mut state = self.lock();
        state.credentials.health.clear();
        state.credentials.probe_ts.clear();
    }

    /// `_reconnect_server`'s configuration: the owner's current stored row
    /// (renewed from the browser first when the credential is known expired
    /// or the last connect failed),
    /// else the last-known config; `stored` is a caller-renewed row.
    pub(super) async fn reconnect_config(
        &self,
        name: &str,
        last_known: Value,
        stored: Option<Value>,
        failing: bool,
    ) -> Result<Value, String> {
        let expired = self.status_of(name).as_deref() == Some("expired");
        let mut renewed = stored.is_some();
        let stored = match stored {
            Some(row) => Some(row),
            None => {
                let (row, outcome) = self
                    .stored_credential_config(name, expired || failing)
                    .await;
                renewed = outcome.is_some_and(|o| o.reason == "renewed");
                row
            }
        };
        let mut config = last_known;
        if let Some(row) = stored {
            if renewed || env_or_empty(Some(&row)) != env_or_empty(Some(&config)) {
                self.lock().credentials.health.remove(name);
            }
            config = row;
        }
        if !enabled(&config) {
            return Err(format!("MCP server {name} is disabled"));
        }
        Ok(config)
    }

    /// Resolve the registry's reviewed reads for a server about to be installed.
    pub(super) async fn resolve_reviewed(&self, name: &str, config: &Value) {
        let answer = crate::registry::get_catalog_entry(self.binding.as_ref(), name).await;
        let mut state = self.lock();
        match answer {
            Ok(entry) => {
                state.credentials.reviewed.insert(
                    name.into(),
                    reviewed_read_only_tools(entry.as_ref(), config),
                );
            }
            Err(_) => {
                state.credentials.reviewed.remove(name);
            }
        }
    }

    pub(super) fn registry_reviewed(&self, name: &str) -> BTreeSet<String> {
        self.lock()
            .credentials
            .reviewed
            .get(name)
            .cloned()
            .unwrap_or_default()
    }

    /// One sweep step awaited to its end (the host's deterministic sweep seam):
    /// true when a due probe ran.
    pub async fn credential_sweep_now(&self, name: &str) -> bool {
        if !self.cred_probe_due(name).await
            || !self.lock().credentials.probe_inflight.insert(name.into())
        {
            return false;
        }
        self.run_cred_probe(name).await;
        self.lock().credentials.probe_inflight.remove(name);
        true
    }

    /// The host protocol's `credentials.<method>` requests.
    pub async fn credentials_host_call(
        &self,
        method: &str,
        params: &Value,
    ) -> Result<Value, CallError> {
        let text = |key: &str| {
            params
                .get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string()
        };
        let name = text("name");
        let owners = || {
            self.owners()
                .ok_or_else(|| CallError::Invalid("credential operations need an owner".into()))
        };
        Ok(match method {
            "validate_health_probe" => {
                let mut warnings = Vec::new();
                let spec = validate_health_probe(params.get("spec"), &text("server"), &mut |w| {
                    warnings.push(w)
                });
                json!({"spec": spec, "warnings": warnings})
            }
            "classify_probe_result" => {
                let (status, detail) = classify_probe_result(
                    &text("text"),
                    params.get("spec").unwrap_or(&Value::Null),
                );
                json!([status, detail])
            }
            "health_probe_schema" => crate::health_probe::health_probe_schema(),
            "reviewed_read_only" => {
                let entry =
                    crate::registry::get_catalog_entry(self.binding.as_ref(), &name).await?;
                json!(reviewed_read_only_tools(
                    entry.as_ref(),
                    params.get("config").unwrap_or(&Value::Null)
                ))
            }
            "get_cred_health" => self.get_cred_health(&name).unwrap_or(Value::Null),
            "cred_probe_spec" => self.cred_probe_spec(&name).await.unwrap_or(Value::Null),
            "run_cred_probe" => self.run_cred_probe(&name).await.unwrap_or(Value::Null),
            "cred_probe_due" => json!(self.cred_probe_due(&name).await),
            "age_cred_probe" => {
                self.age_cred_probe(
                    &name,
                    params.get("seconds").and_then(Value::as_f64).unwrap_or(0.0),
                );
                Value::Null
            }
            "sweep" => json!(self.credential_sweep_now(&name).await),
            "record_health" => {
                self.record_cred_health(&name, &text("status"));
                Value::Null
            }
            "learn" => json!(self.learn_browser_credentials(&name).await),
            "inflight" => self.credential_inflight(),
            "credential_bindings" => {
                let owners = owners()?;
                let row = owners.stored_row(&name).await?.unwrap_or_else(|| json!({}));
                Value::Object(owners.credential_bindings(&name, &row).await?)
            }
            "refresh_from_browser" => owners()?.refresh_from_browser(&name).await?.to_value(),
            "learn_bindings" => {
                let skip: BTreeSet<String> = params
                    .get("skip")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect();
                json!(owners()?.learn_bindings(&name, &skip).await?)
            }
            "failure_notice" => {
                let row = params.get("row").cloned().unwrap_or(Value::Null);
                json!(
                    owners()?
                        .failure_notice(
                            &name,
                            &row,
                            params.get("renewed") == Some(&Value::Bool(true)),
                            &text("reason")
                        )
                        .await?
                )
            }
            other => {
                return Err(CallError::Invalid(format!(
                    "unknown credentials method {other}"
                )))
            }
        })
    }
}
