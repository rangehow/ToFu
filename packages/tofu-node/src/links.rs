//! Node links: one outbound bridge loop per joined server, plus the packaged
//! app's optional local link to its own server
//! (`lib/desktop_agent/links.py::NodeLinks`).
//!
//! Every link shares one permission set. A remote link gets status, route
//! repair over its attachment's own candidates and its own browser relay
//! (admitting only that server's origins, so a signed-in page finds exactly
//! its own relay); Tofu's refusal ends it and marks the attachment. The local
//! link polls with the launcher-provided process capability, has no relay,
//! discovers nothing and keeps retrying on refusal.
//! Entry points: [`NodeLinks`], [`LinkRow`].

use crate::config;
use crate::log;
use crate::pairing::{self, JoinError};
use crate::permissions::SharedPermissions;
use crate::poll::{run_link, LinkOptions, Status};
use crate::relay::BrowserRelay;
use crate::util::Stop;
use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub const LOCAL_LINK: &str = "local";

/// Called whenever a link's displayed facts change (the tray re-renders).
pub type OnChange = Arc<dyn Fn() + Send + Sync>;

/// Live facts of one running link.
#[derive(Default)]
struct LinkState {
    last_status: Mutex<Option<Status>>,
    refused: Mutex<Option<String>>,
    enabled: AtomicBool,
}

struct Running {
    label: String,
    stop: Stop,
    relay: Option<Arc<BrowserRelay>>,
    state: Arc<LinkState>,
}

impl Running {
    fn stop(&self) {
        self.stop.set();
        if let Some(relay) = &self.relay {
            relay.close();
        }
    }
}

/// One display row: a joined server, or the local link while it runs.
#[derive(Clone, Debug, PartialEq)]
pub struct LinkRow {
    pub id: String,
    pub label: String,
    pub url: String,
    pub local: bool,
    /// `active`, or why Tofu refused the node for good (`revoked`, `conflict`).
    pub state: String,
    pub fingerprint: String,
    pub enabled: bool,
    pub last_status: Option<Status>,
}

#[derive(Default)]
struct Inner {
    running: bool,
    links: BTreeMap<String, Running>,
}

pub struct NodeLinks {
    agent_id: String,
    permissions: SharedPermissions,
    poll_interval: f64,
    on_change: OnChange,
    inner: Mutex<Inner>,
}

fn text(record: &Map<String, Value>, key: &str) -> String {
    record
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn mark(attachment_id: &str, state: &str) {
    let mut fields = Map::new();
    fields.insert("state".into(), state.into());
    if let Err(error) = config::update_attachment(attachment_id, fields) {
        log::warn(&format!("Could not record refused link: {error}"));
    }
}

impl NodeLinks {
    pub fn new(
        agent_id: &str,
        permissions: SharedPermissions,
        poll_interval: f64,
        on_change: OnChange,
    ) -> Self {
        Self {
            agent_id: agent_id.to_string(),
            permissions,
            poll_interval,
            on_change,
            inner: Mutex::new(Inner::default()),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn spawn(
        &self,
        id: &str,
        label: &str,
        mut options: LinkOptions,
        remote: Option<String>,
    ) -> Running {
        let stop = Stop::new();
        let state = Arc::new(LinkState::default());
        state.enabled.store(true, Ordering::SeqCst);
        let status_state = state.clone();
        let status_stop = stop.clone();
        let status_label = label.to_string();
        let changed = self.on_change.clone();
        options.on_status = Box::new(move |status: &Status| {
            log::info(&format!("Link status ({status_label}): {status:?}"));
            *status_state
                .last_status
                .lock()
                .unwrap_or_else(|e| e.into_inner()) = Some(status.clone());
            // Tofu refused this node for good: stop the link and record why. A
            // refused second process of this very node (duplicate_boot) leaves
            // the attachment it shares with the surviving process active.
            let refused = match status {
                Status::Auth => Some(("revoked", false)),
                Status::Conflict { reason } => Some(("conflict", reason == "duplicate_boot")),
                _ => None,
            };
            if let (Some(attachment_id), Some((verdict, duplicate))) = (&remote, refused) {
                status_stop.set();
                *status_state
                    .refused
                    .lock()
                    .unwrap_or_else(|e| e.into_inner()) = Some(verdict.to_string());
                if !duplicate {
                    mark(attachment_id, verdict);
                }
            }
            changed();
        });
        let relay = options.relay.clone();
        let agent_id = self.agent_id.clone();
        let permissions = self.permissions.clone();
        let link_stop = stop.clone();
        let done_state = state.clone();
        let done = self.on_change.clone();
        std::thread::Builder::new()
            .name(format!("tofu-node-link-{id}"))
            .spawn(move || {
                run_link(&agent_id, permissions, options, link_stop);
                done_state.enabled.store(false, Ordering::SeqCst);
                done();
            })
            .expect("spawn link thread");
        Running {
            label: label.to_string(),
            stop,
            relay,
            state,
        }
    }

    fn start_attachment(&self, inner: &mut Inner, record: &Map<String, Value>) {
        let id = text(record, "id");
        let url = text(record, "url");
        let label = Some(text(record, "label"))
            .filter(|l| !l.is_empty())
            .unwrap_or_else(|| url.clone());
        let candidates = strings(record.get("candidates"));
        let mut origins = vec![url.clone()];
        origins.extend(candidates.iter().cloned());
        let persist_id = id.clone();
        let options = LinkOptions {
            url: url.clone(),
            secret: text(record, "secret"),
            candidates,
            poll_interval: self.poll_interval,
            stop_on_refusal: true,
            on_status: Box::new(|_| {}),
            persist_route: Some(Box::new(move |new_url: &str| {
                let mut fields = Map::new();
                fields.insert("url".into(), new_url.into());
                if let Err(error) = config::update_attachment(&persist_id, fields) {
                    log::warn(&format!("Could not persist re-pointed attachment: {error}"));
                }
            })),
            relay: BrowserRelay::start(&origins).map(Arc::new),
        };
        let running = self.spawn(&id, &label, options, Some(id.clone()));
        inner.links.insert(id, running);
    }

    /// Start the local link (`(url, capability)`) and every active attachment.
    pub fn start(&self, local: Option<(String, String)>) -> Result<(), String> {
        let records = config::attachments()?;
        let mut inner = self.lock();
        inner.running = true;
        if let Some((url, secret)) = local {
            if !inner.links.contains_key(LOCAL_LINK) {
                let options = LinkOptions {
                    url: url.clone(),
                    secret,
                    candidates: Vec::new(),
                    poll_interval: self.poll_interval,
                    stop_on_refusal: false,
                    on_status: Box::new(|_| {}),
                    persist_route: None,
                    relay: None,
                };
                let running = self.spawn(LOCAL_LINK, &url, options, None);
                inner.links.insert(LOCAL_LINK.to_string(), running);
            }
        }
        for record in &records {
            let active = record
                .get("state")
                .and_then(Value::as_str)
                .unwrap_or("active")
                == "active";
            if active && !inner.links.contains_key(&text(record, "id")) {
                self.start_attachment(&mut inner, record);
            }
        }
        drop(inner);
        (self.on_change)();
        Ok(())
    }

    /// Signal every link to stop; a link mid-poll finishes that poll first.
    pub fn stop(&self) {
        let links = {
            let mut inner = self.lock();
            inner.running = false;
            std::mem::take(&mut inner.links)
        };
        for running in links.values() {
            running.stop();
        }
        (self.on_change)();
    }

    pub fn running(&self) -> bool {
        self.lock().running
    }

    /// Labels of the links started so far.
    pub fn labels(&self) -> Vec<String> {
        self.lock()
            .links
            .values()
            .map(|r| r.label.clone())
            .collect()
    }

    /// Redeem one pairing input and, while the node runs, link at once.
    pub fn join(
        &self,
        input: &str,
        kind: &str,
        stop: &Stop,
    ) -> Result<Map<String, Value>, JoinError> {
        let record = pairing::join(input, &self.agent_id, kind, &|line| log::info(line), stop)?;
        let mut inner = self.lock();
        if let Some(old) = inner.links.remove(&text(&record, "id")) {
            old.stop();
        }
        if inner.running {
            self.start_attachment(&mut inner, &record);
        }
        drop(inner);
        (self.on_change)();
        Ok(record)
    }

    /// Leave a joined server: stop its link and drop its attachment.
    pub fn leave(&self, attachment_id: &str) -> Result<bool, String> {
        if let Some(running) = self.lock().links.remove(attachment_id) {
            running.stop();
        }
        let removed = config::remove_attachment(attachment_id);
        (self.on_change)();
        removed
    }

    /// One row per joined server (and the local link while it runs).
    pub fn statuses(&self) -> Vec<LinkRow> {
        let records = config::attachments().unwrap_or_else(|error| {
            log::warn(&format!("could not read joined servers: {error}"));
            Vec::new()
        });
        let inner = self.lock();
        let facts = |running: Option<&Running>| {
            running
                .map(|r| {
                    (
                        r.state.enabled.load(Ordering::SeqCst),
                        r.state
                            .last_status
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .clone(),
                        r.state
                            .refused
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .clone(),
                    )
                })
                .unwrap_or((false, None, None))
        };
        let mut rows = Vec::new();
        if let Some(local) = inner.links.get(LOCAL_LINK) {
            let (enabled, last_status, _) = facts(Some(local));
            rows.push(LinkRow {
                id: LOCAL_LINK.into(),
                label: local.label.clone(),
                url: local.label.clone(),
                local: true,
                state: "active".into(),
                fingerprint: String::new(),
                enabled,
                last_status,
            });
        }
        for record in &records {
            let id = text(record, "id");
            let (enabled, last_status, refused) = facts(inner.links.get(&id));
            let saved = text(record, "state");
            let state = refused
                .or_else(|| {
                    ["revoked", "conflict"]
                        .contains(&saved.as_str())
                        .then_some(saved)
                })
                .unwrap_or_else(|| "active".into());
            let url = text(record, "url");
            rows.push(LinkRow {
                label: Some(text(record, "label"))
                    .filter(|l| !l.is_empty())
                    .unwrap_or_else(|| url.clone()),
                id,
                url,
                local: false,
                state,
                fingerprint: text(record, "fingerprint"),
                enabled,
                last_status,
            });
        }
        rows
    }
}
