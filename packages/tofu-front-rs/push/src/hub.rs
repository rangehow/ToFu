//! Subscription routing: port of `PushHub` (`lib/agent_core/push.py`).
//!
//! `(channel, taskId)` subscriptions select local sockets, `taskId = "*"`
//! subscribes to every task of a channel, and the frame owner narrows
//! delivery to that owner's sockets. Empty buckets are pruned so the
//! registry does not grow with every task ever viewed.
//!
//! The hub also reports *mirror* transitions: the first socket of an owner
//! subscribing to `(channel, taskId)` and the last one leaving. The bridge
//! sends them to Python so `PushHub.has_subscriber` (demand-driven producers)
//! and the cross-replica subscription lease keep working while the sockets
//! live here.
//!
//! A hub generation fences registration against a bridge that died between
//! admission and registration (`reset`).

use crate::client::{PushClient, Stream};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

pub type SocketId = u64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MirrorChange {
    Subscribed {
        channel: String,
        task_id: String,
        user_id: String,
    },
    Unsubscribed {
        channel: String,
        task_id: String,
        user_id: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RegisterRefusal {
    /// The hub is closing for shutdown (Python's `register` fence: 503).
    Draining,
    /// The bridge generation that admitted the socket is gone.
    StaleGeneration,
}

#[derive(Default)]
struct HubState {
    sockets: HashMap<SocketId, Arc<PushClient>>,
    subscriptions: HashMap<String, HashMap<String, HashSet<SocketId>>>,
    mirror: HashMap<(String, String, String), usize>,
    draining: bool,
    generation: u64,
    next_socket: SocketId,
}

#[derive(Default)]
pub struct PushHub {
    state: Mutex<HubState>,
}

impl PushHub {
    pub fn new() -> Self {
        Self::default()
    }

    fn state(&self) -> std::sync::MutexGuard<'_, HubState> {
        self.state
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn generation(&self) -> u64 {
        self.state().generation
    }

    pub fn is_draining(&self) -> bool {
        self.state().draining
    }

    pub fn register(
        &self,
        client: Arc<PushClient>,
        generation: u64,
    ) -> Result<SocketId, RegisterRefusal> {
        let mut state = self.state();
        if state.draining {
            return Err(RegisterRefusal::Draining);
        }
        if state.generation != generation {
            return Err(RegisterRefusal::StaleGeneration);
        }
        state.next_socket += 1;
        let id = state.next_socket;
        state.sockets.insert(id, client);
        Ok(id)
    }

    pub fn client(&self, id: SocketId) -> Option<Arc<PushClient>> {
        self.state().sockets.get(&id).cloned()
    }

    pub fn client_count(&self) -> usize {
        self.state().sockets.len()
    }

    pub fn subscribe(&self, id: SocketId, channel: &str, task_id: &str) -> Option<MirrorChange> {
        let mut state = self.state();
        let user_id = state.sockets.get(&id)?.user_id.clone();
        let inserted = state
            .subscriptions
            .entry(channel.to_string())
            .or_default()
            .entry(task_id.to_string())
            .or_default()
            .insert(id);
        if !inserted {
            return None;
        }
        let count = state
            .mirror
            .entry((channel.to_string(), task_id.to_string(), user_id.clone()))
            .or_default();
        *count += 1;
        (*count == 1).then(|| MirrorChange::Subscribed {
            channel: channel.to_string(),
            task_id: task_id.to_string(),
            user_id,
        })
    }

    pub fn unsubscribe(&self, id: SocketId, channel: &str, task_id: &str) -> Option<MirrorChange> {
        let mut state = self.state();
        let user_id = state.sockets.get(&id)?.user_id.clone();
        let channel_subs = state.subscriptions.get_mut(channel)?;
        let task_clients = channel_subs.get_mut(task_id)?;
        if !task_clients.remove(&id) {
            return None;
        }
        if task_clients.is_empty() {
            channel_subs.remove(task_id);
            if channel_subs.is_empty() {
                state.subscriptions.remove(channel);
            }
        }
        Self::release_mirror(&mut state, channel, task_id, user_id)
    }

    fn release_mirror(
        state: &mut HubState,
        channel: &str,
        task_id: &str,
        user_id: String,
    ) -> Option<MirrorChange> {
        let key = (channel.to_string(), task_id.to_string(), user_id);
        let count = state.mirror.get_mut(&key)?;
        *count -= 1;
        if *count > 0 {
            return None;
        }
        state.mirror.remove(&key);
        let (channel, task_id, user_id) = key;
        Some(MirrorChange::Unsubscribed {
            channel,
            task_id,
            user_id,
        })
    }

    /// Remove a socket and every subscription it held.
    pub fn unregister(&self, id: SocketId) -> Vec<MirrorChange> {
        let mut state = self.state();
        let Some(client) = state.sockets.remove(&id) else {
            return Vec::new();
        };
        let mut held = Vec::new();
        state.subscriptions.retain(|channel, channel_subs| {
            channel_subs.retain(|task_id, task_clients| {
                if task_clients.remove(&id) {
                    held.push((channel.clone(), task_id.clone()));
                }
                !task_clients.is_empty()
            });
            !channel_subs.is_empty()
        });
        held.into_iter()
            .filter_map(|(channel, task_id)| {
                Self::release_mirror(&mut state, &channel, &task_id, client.user_id.clone())
            })
            .collect()
    }

    /// Deliver one published frame (already-encoded `text`) to this hub's
    /// subscribers of `(channel, taskId)` and `(channel, "*")` owned by
    /// `owner_user_id`. Returns the number of sockets reached.
    pub fn deliver(
        &self,
        channel: &str,
        task_id: &str,
        owner_user_id: &str,
        latest_value_key: Option<&str>,
        text: Arc<str>,
    ) -> usize {
        let owner = owner_user_id.trim();
        if owner.is_empty() {
            eprintln!("[Push] rejected ownerless frame channel={channel} task={task_id}");
            return 0;
        }
        let targets: Vec<Arc<PushClient>> = {
            let state = self.state();
            if state.draining {
                return 0;
            }
            let Some(channel_subs) = state.subscriptions.get(channel) else {
                return 0;
            };
            let mut ids: HashSet<SocketId> = HashSet::new();
            for key in [task_id, "*"] {
                if let Some(task_clients) = channel_subs.get(key) {
                    ids.extend(task_clients.iter().copied());
                }
            }
            ids.into_iter()
                .filter_map(|id| state.sockets.get(&id))
                .filter(|client| client.user_id == owner)
                .cloned()
                .collect()
        };
        let stream: Stream = (channel.to_string(), task_id.to_string());
        for client in &targets {
            match latest_value_key {
                Some(key) if !key.is_empty() => {
                    client.enqueue_latest(&stream, key, Arc::clone(&text))
                }
                _ => client.enqueue_prepared(Arc::clone(&text), Some(&stream)),
            }
        }
        targets.len()
    }

    /// Shutdown fence (`close_clients_for_shutdown`): refuse new sockets and
    /// wake every live one so it closes with the server-release code.
    pub fn close_for_shutdown(&self) -> usize {
        let clients: Vec<Arc<PushClient>> = {
            let mut state = self.state();
            state.draining = true;
            state.sockets.values().cloned().collect()
        };
        for client in &clients {
            client.disconnect();
        }
        clients.len()
    }

    /// The Python bridge ended: every socket admitted through it is released
    /// (the browser reconnects and reconciles) and its registrations are
    /// dropped without mirror traffic, since Python already discarded them.
    pub fn reset(&self) -> usize {
        let clients: Vec<Arc<PushClient>> = {
            let mut state = self.state();
            state.generation += 1;
            state.subscriptions.clear();
            state.mirror.clear();
            state.sockets.drain().map(|(_, client)| client).collect()
        };
        for client in &clients {
            client.disconnect();
        }
        clients.len()
    }

    pub fn subscription_buckets(&self) -> usize {
        self.state().subscriptions.values().map(HashMap::len).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::Outgoing;

    fn socket(hub: &PushHub, user: &str) -> (SocketId, Arc<PushClient>) {
        let client = Arc::new(PushClient::new(user.into(), String::new(), 64));
        let id = hub.register(Arc::clone(&client), hub.generation()).unwrap();
        (id, client)
    }

    fn drained(client: &PushClient) -> Vec<String> {
        std::iter::from_fn(|| match client.try_next() {
            Some(Outgoing::Text(text)) => Some(text.to_string()),
            _ => None,
        })
        .collect()
    }

    #[test]
    fn routes_by_task_wildcard_and_owner() {
        let hub = PushHub::new();
        let (a, client_a) = socket(&hub, "1");
        let (b, client_b) = socket(&hub, "1");
        let (c, client_c) = socket(&hub, "2");
        hub.subscribe(a, "chat", "t1");
        hub.subscribe(b, "chat", "*");
        hub.subscribe(c, "chat", "*");
        assert_eq!(hub.deliver("chat", "t1", "1", None, Arc::from("f1")), 2);
        assert_eq!(hub.deliver("chat", "t2", "1", None, Arc::from("f2")), 1);
        assert_eq!(hub.deliver("paper", "t1", "1", None, Arc::from("f3")), 0);
        assert_eq!(hub.deliver("chat", "t1", " ", None, Arc::from("f4")), 0);
        assert_eq!(drained(&client_a), ["f1"]);
        assert_eq!(drained(&client_b), ["f1", "f2"]);
        assert!(drained(&client_c).is_empty());
    }

    #[test]
    fn a_socket_subscribed_twice_receives_a_frame_once() {
        let hub = PushHub::new();
        let (a, client) = socket(&hub, "1");
        hub.subscribe(a, "notify", "*");
        hub.subscribe(a, "notify", "x");
        hub.deliver("notify", "x", "1", None, Arc::from("f"));
        assert_eq!(drained(&client), ["f"]);
    }

    #[test]
    fn mirror_reports_first_and_last_subscriber_per_owner() {
        let hub = PushHub::new();
        let (a, _) = socket(&hub, "1");
        let (b, _) = socket(&hub, "1");
        let sub = |channel: &str, task: &str| MirrorChange::Subscribed {
            channel: channel.into(),
            task_id: task.into(),
            user_id: "1".into(),
        };
        let unsub = |channel: &str, task: &str| MirrorChange::Unsubscribed {
            channel: channel.into(),
            task_id: task.into(),
            user_id: "1".into(),
        };
        assert_eq!(hub.subscribe(a, "notify", "*"), Some(sub("notify", "*")));
        assert_eq!(hub.subscribe(a, "notify", "*"), None);
        assert_eq!(hub.subscribe(b, "notify", "*"), None);
        assert_eq!(hub.unsubscribe(a, "notify", "*"), None);
        assert_eq!(hub.unsubscribe(a, "notify", "*"), None);
        assert_eq!(hub.unsubscribe(a, "unknown", "*"), None);
        hub.subscribe(b, "chat", "t");
        let mut released = hub.unregister(b);
        released.sort_by_key(|change| format!("{change:?}"));
        assert_eq!(released, [unsub("chat", "t"), unsub("notify", "*")]);
        assert_eq!(hub.subscription_buckets(), 0);
    }

    #[test]
    fn shutdown_refuses_registration_and_disconnects() {
        let hub = PushHub::new();
        let (_, client) = socket(&hub, "1");
        assert_eq!(hub.close_for_shutdown(), 1);
        assert!(!client.is_connected());
        let late = Arc::new(PushClient::new("1".into(), String::new(), 64));
        assert_eq!(
            hub.register(late, hub.generation()),
            Err(RegisterRefusal::Draining)
        );
    }

    #[test]
    fn reset_fences_sockets_admitted_by_a_dead_bridge() {
        let hub = PushHub::new();
        let admitted_under = hub.generation();
        let (a, client) = socket(&hub, "1");
        hub.subscribe(a, "notify", "*");
        assert_eq!(hub.reset(), 1);
        assert!(!client.is_connected());
        assert!(hub.unregister(a).is_empty());
        let late = Arc::new(PushClient::new("1".into(), String::new(), 64));
        assert_eq!(
            hub.register(late, admitted_under),
            Err(RegisterRefusal::StaleGeneration)
        );
    }

    #[test]
    fn latest_value_frames_coalesce_per_socket() {
        let hub = PushHub::new();
        let (a, client) = socket(&hub, "1");
        hub.subscribe(a, "office", "__office__");
        for value in ["v1", "v2", "v3"] {
            hub.deliver("office", "__office__", "1", Some("fact"), Arc::from(value));
        }
        assert_eq!(drained(&client), ["v3"]);
    }
}
