//! One `/api/push` socket's outbound lanes: port of `PushClient`
//! (`lib/agent_core/push.py`).
//!
//! - control lane (`enqueue_control`: pongs) is drained first, then the
//!   correlated RPC lane (`enqueue_rpc`), then the FIFO event backlog;
//! - the event backlog holds already-encoded frame text shared by every
//!   socket a publication reaches (`Arc<str>`, never re-encoded per socket);
//! - a `latest_value_key` publication keeps one queued position per
//!   `(channel, taskId)` stream: a newer value of the same key replaces an
//!   unsent older one in place; any other frame of the stream fences it;
//! - a full event backlog disconnects the socket (the client reconciles
//!   from durable state) instead of growing without limit.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use tokio::sync::Notify;

pub type Stream = (String, String);

/// `TOFU_PUSH_EVENT_QUEUE_CAPACITY` resolution of
/// `lib/agent_core/push_policy.py::resolve_push_event_queue_capacity`: an
/// explicit override is clamped to 64..=16384; the default is the envelope
/// maximum. A `retention` bound on reconstructible transport data.
pub fn resolve_event_queue_capacity(override_value: Option<&str>) -> usize {
    const ENVELOPE_MIN: usize = 64;
    const ENVELOPE_MAX: usize = 16384;
    let parsed = override_value
        .map(str::trim)
        .filter(|raw| !raw.is_empty())
        .and_then(|raw| raw.parse::<i64>().ok())
        .unwrap_or(0);
    if parsed > 0 {
        (parsed as usize).clamp(ENVELOPE_MIN, ENVELOPE_MAX)
    } else {
        ENVELOPE_MAX
    }
}

struct LatestSlot {
    stream: Stream,
    key: String,
    value: Mutex<Arc<str>>,
}

enum Event {
    Prepared(Arc<str>),
    Latest(Arc<LatestSlot>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outgoing {
    Text(Arc<str>),
    /// The socket was disconnected (backlog overflow, bridge loss, shutdown).
    Closed,
}

struct Lanes {
    connected: bool,
    control: VecDeque<Arc<str>>,
    rpc: VecDeque<Arc<str>>,
    events: VecDeque<Event>,
    latest_open: HashMap<Stream, Arc<LatestSlot>>,
}

pub struct PushClient {
    pub user_id: String,
    pub req_id: String,
    capacity: usize,
    lanes: Mutex<Lanes>,
    wake: Notify,
}

impl PushClient {
    pub fn new(user_id: String, req_id: String, capacity: usize) -> Self {
        Self {
            user_id,
            req_id,
            capacity,
            lanes: Mutex::new(Lanes {
                connected: true,
                control: VecDeque::new(),
                rpc: VecDeque::new(),
                events: VecDeque::new(),
                latest_open: HashMap::new(),
            }),
            wake: Notify::new(),
        }
    }

    fn lanes(&self) -> std::sync::MutexGuard<'_, Lanes> {
        self.lanes
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn is_connected(&self) -> bool {
        self.lanes().connected
    }

    pub fn enqueue_prepared(&self, text: Arc<str>, stream: Option<&Stream>) {
        let mut lanes = self.lanes();
        if !lanes.connected {
            return;
        }
        if let Some(stream) = stream {
            lanes.latest_open.remove(stream);
        }
        self.put(lanes, Event::Prepared(text));
    }

    pub fn enqueue_latest(&self, stream: &Stream, key: &str, text: Arc<str>) {
        let mut lanes = self.lanes();
        if !lanes.connected {
            return;
        }
        if let Some(queued) = lanes.latest_open.get(stream) {
            if queued.key == key {
                *queued.value.lock().unwrap_or_else(|p| p.into_inner()) = text;
                return;
            }
        }
        let slot = Arc::new(LatestSlot {
            stream: stream.clone(),
            key: key.to_string(),
            value: Mutex::new(text),
        });
        lanes.latest_open.insert(stream.clone(), Arc::clone(&slot));
        self.put(lanes, Event::Latest(slot));
    }

    fn put(&self, mut lanes: std::sync::MutexGuard<'_, Lanes>, event: Event) {
        if lanes.events.len() >= self.capacity {
            eprintln!(
                "[Push] Client event backlog full ({}); disconnecting stalled socket for durable reconciliation (rid={})",
                self.capacity,
                if self.req_id.is_empty() { "?" } else { &self.req_id }
            );
            Self::clear(&mut lanes);
            drop(lanes);
            self.wake.notify_one();
            return;
        }
        lanes.events.push_back(event);
        drop(lanes);
        self.wake.notify_one();
    }

    pub fn enqueue_control(&self, text: Arc<str>) {
        let mut lanes = self.lanes();
        if !lanes.connected {
            return;
        }
        lanes.control.push_back(text);
        drop(lanes);
        self.wake.notify_one();
    }

    /// `False` tells the RPC owner that this socket has disconnected.
    pub fn enqueue_rpc(&self, text: Arc<str>) -> bool {
        let mut lanes = self.lanes();
        if !lanes.connected {
            return false;
        }
        lanes.rpc.push_back(text);
        drop(lanes);
        self.wake.notify_one();
        true
    }

    fn clear(lanes: &mut Lanes) {
        lanes.connected = false;
        lanes.control.clear();
        lanes.rpc.clear();
        lanes.events.clear();
        lanes.latest_open.clear();
    }

    pub fn disconnect(&self) {
        Self::clear(&mut self.lanes());
        self.wake.notify_one();
    }

    /// The next frame in lane priority order, without waiting.
    pub fn try_next(&self) -> Option<Outgoing> {
        let mut lanes = self.lanes();
        if !lanes.connected {
            return Some(Outgoing::Closed);
        }
        if let Some(text) = lanes.control.pop_front() {
            return Some(Outgoing::Text(text));
        }
        if let Some(text) = lanes.rpc.pop_front() {
            return Some(Outgoing::Text(text));
        }
        match lanes.events.pop_front()? {
            Event::Prepared(text) => Some(Outgoing::Text(text)),
            Event::Latest(slot) => {
                // Dequeued means sending: this value is final for this socket.
                if lanes
                    .latest_open
                    .get(&slot.stream)
                    .is_some_and(|open| Arc::ptr_eq(open, &slot))
                {
                    lanes.latest_open.remove(&slot.stream);
                }
                let text = Arc::clone(&slot.value.lock().unwrap_or_else(|p| p.into_inner()));
                Some(Outgoing::Text(text))
            }
        }
    }

    /// Wait for the next frame. Cancel-safe: a wake that arrives while no
    /// one waits is kept as the `Notify` permit.
    pub async fn next(&self) -> Outgoing {
        loop {
            if let Some(outgoing) = self.try_next() {
                return outgoing;
            }
            self.wake.notified().await;
        }
    }

    pub fn queued_events(&self) -> usize {
        self.lanes().events.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> Arc<str> {
        Arc::from(value)
    }

    fn stream(task: &str) -> Stream {
        ("chat".to_string(), task.to_string())
    }

    fn next_text(client: &PushClient) -> Option<String> {
        match client.try_next() {
            Some(Outgoing::Text(text)) => Some(text.to_string()),
            Some(Outgoing::Closed) => Some("<closed>".to_string()),
            None => None,
        }
    }

    #[test]
    fn capacity_follows_the_python_envelope() {
        assert_eq!(resolve_event_queue_capacity(None), 16384);
        assert_eq!(resolve_event_queue_capacity(Some("")), 16384);
        assert_eq!(resolve_event_queue_capacity(Some("junk")), 16384);
        assert_eq!(resolve_event_queue_capacity(Some("0")), 16384);
        assert_eq!(resolve_event_queue_capacity(Some("-5")), 16384);
        assert_eq!(resolve_event_queue_capacity(Some("1")), 64);
        assert_eq!(resolve_event_queue_capacity(Some(" 100 ")), 100);
        assert_eq!(resolve_event_queue_capacity(Some("999999")), 16384);
    }

    #[test]
    fn control_then_rpc_then_events() {
        let client = PushClient::new("1".into(), "r".into(), 64);
        client.enqueue_prepared(text("e1"), None);
        client.enqueue_rpc(text("r1"));
        client.enqueue_control(text("c1"));
        client.enqueue_prepared(text("e2"), None);
        let order: Vec<_> = std::iter::from_fn(|| next_text(&client)).collect();
        assert_eq!(order, ["c1", "r1", "e1", "e2"]);
    }

    #[test]
    fn latest_value_replaces_unsent_value_and_is_fenced_by_other_frames() {
        let client = PushClient::new("1".into(), "".into(), 64);
        client.enqueue_latest(&stream("a"), "k", text("v1"));
        client.enqueue_latest(&stream("a"), "k", text("v2"));
        client.enqueue_latest(&stream("b"), "k", text("b1"));
        client.enqueue_prepared(text("plain-a"), Some(&stream("a")));
        client.enqueue_latest(&stream("a"), "k", text("v3"));
        client.enqueue_latest(&stream("a"), "k", text("v4"));
        client.enqueue_latest(&stream("a"), "other", text("o1"));
        let order: Vec<_> = std::iter::from_fn(|| next_text(&client)).collect();
        assert_eq!(order, ["v2", "b1", "plain-a", "v4", "o1"]);
    }

    #[test]
    fn a_sent_latest_value_is_final_and_newer_values_queue_again() {
        let client = PushClient::new("1".into(), "".into(), 64);
        client.enqueue_latest(&stream("a"), "k", text("v1"));
        assert_eq!(next_text(&client).as_deref(), Some("v1"));
        client.enqueue_latest(&stream("a"), "k", text("v2"));
        assert_eq!(next_text(&client).as_deref(), Some("v2"));
        assert_eq!(next_text(&client), None);
    }

    #[test]
    fn a_full_backlog_disconnects_and_drops_everything() {
        let client = PushClient::new("1".into(), "".into(), 64);
        for index in 0..64 {
            client.enqueue_prepared(text(&index.to_string()), None);
        }
        assert!(client.is_connected());
        client.enqueue_prepared(text("overflow"), None);
        assert!(!client.is_connected());
        assert_eq!(client.queued_events(), 0);
        assert_eq!(client.try_next(), Some(Outgoing::Closed));
        assert!(!client.enqueue_rpc(text("late")));
    }

    #[tokio::test]
    async fn a_wake_before_waiting_is_not_lost() {
        let client = Arc::new(PushClient::new("1".into(), "".into(), 64));
        client.enqueue_control(text("pong"));
        assert_eq!(client.next().await, Outgoing::Text(text("pong")));
        let waiter = {
            let client = Arc::clone(&client);
            tokio::spawn(async move { client.next().await })
        };
        tokio::task::yield_now().await;
        client.enqueue_prepared(text("late"), None);
        assert_eq!(waiter.await.unwrap(), Outgoing::Text(text("late")));
    }
}
