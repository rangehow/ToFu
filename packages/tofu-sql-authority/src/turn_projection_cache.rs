//! `turn_projection_cache.py`: bounded reconstructible cache of live Turn
//! projection write baselines beside one backend.
//!
//! The database revision remains the authority: a miss, stale revision,
//! eviction, process restart or rollback always falls back to the durable
//! row. Entry points are [`TurnProjectionCache`], [`CachedTurnProjection`]
//! and [`projection_cache_key`].

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

pub type ProjectionCacheKey = (String, i64, String, String, String);
pub const PROJECTION_CACHE_CHARGE_MULTIPLIER: i64 = 3;
const DEFAULT_MAX_IDLE_SECONDS: f64 = 600.0;

/// One immutable-by-convention public baseline and its storage evidence.
#[derive(Clone, Debug)]
pub struct CachedTurnProjection {
    pub revision: i64,
    pub projection: Arc<Value>,
    pub charge_bytes: i64,
    pub text_bytes: i64,
    pub stored_payload_bytes: Option<i64>,
    pub stored_matches_projection: bool,
    pub stable_segments: bool,
    last_used_at: Instant,
    tick: u64,
}

/// The explicit authority/owner/attempt cache identity.
pub fn projection_cache_key(
    backend: &str,
    user_id: i64,
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
) -> ProjectionCacheKey {
    (backend.to_owned(), user_id, conversation_id.to_owned(), turn_id.to_owned(), attempt_id.to_owned())
}

/// `str(value or "")` for one cumulative text field.
fn text_field(projection: &Value, field: &str) -> usize {
    match projection.get(field) {
        Some(value) if crate::json::truthy(value) => crate::json::python_str(value).len(),
        _ => 0,
    }
}

/// `projection_text_bytes`: exact UTF-8 bytes of `content` and `thinking`.
pub fn projection_text_bytes(projection: &Value) -> i64 {
    (text_field(projection, "content") + text_field(projection, "thinking")) as i64
}

#[derive(Default)]
struct Counters {
    hits: u64,
    misses: u64,
    stale_evictions: u64,
    expired_evictions: u64,
    capacity_evictions: u64,
    oversize_rejections: u64,
}

#[derive(Default)]
struct Inner {
    entries: HashMap<ProjectionCacheKey, CachedTurnProjection>,
    /// LRU order: oldest tick first.
    order: BTreeMap<u64, ProjectionCacheKey>,
    next_tick: u64,
    charged_bytes: i64,
    metrics: Counters,
}

impl Inner {
    fn remove(&mut self, key: &ProjectionCacheKey) -> bool {
        let Some(entry) = self.entries.remove(key) else { return false };
        self.order.remove(&entry.tick);
        self.charged_bytes = (self.charged_bytes - entry.charge_bytes).max(0);
        true
    }

    fn touch(&mut self, key: &ProjectionCacheKey) -> u64 {
        let tick = self.next_tick;
        self.next_tick += 1;
        self.order.insert(tick, key.clone());
        tick
    }

    fn prune_expired(&mut self, now: Instant, max_idle: Duration) {
        while let Some((_, key)) = self.order.first_key_value() {
            let key = key.clone();
            let entry = &self.entries[&key];
            if now.saturating_duration_since(entry.last_used_at) < max_idle {
                return;
            }
            self.remove(&key);
            self.metrics.expired_evictions += 1;
        }
    }
}

/// Thread-safe LRU bounded by charged bytes and idle lifetime.
pub struct TurnProjectionCache {
    max_bytes: i64,
    max_idle_seconds: f64,
    max_idle: Duration,
    inner: Mutex<Inner>,
}

impl TurnProjectionCache {
    pub fn new(max_bytes: i64) -> Self {
        assert!(max_bytes > 0, "projection cache max_bytes must be positive");
        Self {
            max_bytes,
            max_idle_seconds: DEFAULT_MAX_IDLE_SECONDS,
            max_idle: Duration::from_secs_f64(DEFAULT_MAX_IDLE_SECONDS),
            inner: Mutex::new(Inner::default()),
        }
    }

    /// Return an exact-revision baseline or a safe cache miss.
    pub fn get(&self, key: &ProjectionCacheKey, revision: i64) -> Option<CachedTurnProjection> {
        let now = Instant::now();
        let mut inner = self.inner.lock().expect("projection cache");
        inner.prune_expired(now, self.max_idle);
        let Some(entry) = inner.entries.get(key) else {
            inner.metrics.misses += 1;
            return None;
        };
        if entry.revision != revision {
            inner.remove(key);
            inner.metrics.stale_evictions += 1;
            inner.metrics.misses += 1;
            return None;
        }
        let old_tick = entry.tick;
        inner.order.remove(&old_tick);
        let tick = inner.touch(key);
        let entry = inner.entries.get_mut(key).expect("cached entry");
        entry.last_used_at = now;
        entry.tick = tick;
        let entry = entry.clone();
        inner.metrics.hits += 1;
        Some(entry)
    }

    /// Retain one baseline when it fits the byte budget.
    #[allow(clippy::too_many_arguments)]
    pub fn remember(
        &self,
        key: &ProjectionCacheKey,
        revision: i64,
        projection: Arc<Value>,
        charge_bytes: i64,
        stored_payload_bytes: Option<i64>,
        stored_matches_projection: bool,
        stable_segments: bool,
    ) -> bool {
        let charge = charge_bytes.max(1);
        let now = Instant::now();
        let mut inner = self.inner.lock().expect("projection cache");
        inner.prune_expired(now, self.max_idle);
        inner.remove(key);
        if charge > self.max_bytes {
            inner.metrics.oversize_rejections += 1;
            return false;
        }
        let tick = inner.touch(key);
        let text_bytes = projection_text_bytes(&projection);
        inner.entries.insert(
            key.clone(),
            CachedTurnProjection {
                revision,
                projection,
                charge_bytes: charge,
                text_bytes,
                stored_payload_bytes: stored_payload_bytes.map(|bytes| bytes.max(0)),
                stored_matches_projection,
                stable_segments,
                last_used_at: now,
                tick,
            },
        );
        inner.charged_bytes += charge;
        // Charged bytes are the only capacity bound: an entry count would
        // evict live baselines once concurrent Turns outnumber it.
        while inner.charged_bytes > self.max_bytes {
            let Some((_, oldest)) = inner.order.first_key_value() else { break };
            let oldest = oldest.clone();
            inner.remove(&oldest);
            inner.metrics.capacity_evictions += 1;
        }
        inner.entries.contains_key(key)
    }

    /// Forget one reconstructible baseline.
    pub fn discard(&self, key: &ProjectionCacheKey) -> bool {
        self.inner.lock().expect("projection cache").remove(key)
    }

    /// Release all reconstructible state and return the entry count.
    pub fn clear(&self) -> usize {
        let mut inner = self.inner.lock().expect("projection cache");
        let count = inner.entries.len();
        inner.entries.clear();
        inner.order.clear();
        inner.charged_bytes = 0;
        count
    }

    /// Bounded, content-free cache observability.
    pub fn stats(&self) -> Value {
        let mut inner = self.inner.lock().expect("projection cache");
        inner.prune_expired(Instant::now(), self.max_idle);
        let metrics = &inner.metrics;
        json!({
            "hits": metrics.hits,
            "misses": metrics.misses,
            "stale_evictions": metrics.stale_evictions,
            "expired_evictions": metrics.expired_evictions,
            "capacity_evictions": metrics.capacity_evictions,
            "oversize_rejections": metrics.oversize_rejections,
            "entries": inner.entries.len(),
            "charged_bytes": inner.charged_bytes,
            "max_bytes": self.max_bytes,
            "max_idle_seconds": self.max_idle_seconds,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(turn: &str) -> ProjectionCacheKey {
        projection_cache_key("sqlite", 1, "c", turn, "a")
    }

    #[test]
    fn exact_revision_hits_and_stale_revision_misses() {
        let cache = TurnProjectionCache::new(1000);
        assert!(cache.get(&key("t"), 1).is_none());
        assert!(cache.remember(&key("t"), 2, Arc::new(json!({"content": "hé"})), 10, Some(5), true, false));
        let entry = cache.get(&key("t"), 2).expect("hit");
        assert_eq!(entry.text_bytes, 3);
        assert!(cache.get(&key("t"), 3).is_none());
        let stats = cache.stats();
        assert_eq!(stats["hits"], 1);
        assert_eq!(stats["misses"], 2);
        assert_eq!(stats["stale_evictions"], 1);
        assert_eq!(stats["entries"], 0);
        assert_eq!(stats["charged_bytes"], 0);
        assert_eq!(stats["max_idle_seconds"], json!(600.0));
    }

    #[test]
    fn charged_bytes_evict_least_recently_used_and_reject_oversize() {
        let cache = TurnProjectionCache::new(20);
        assert!(cache.remember(&key("a"), 1, Arc::new(json!({})), 8, None, true, false));
        assert!(cache.remember(&key("b"), 1, Arc::new(json!({})), 8, None, true, false));
        assert!(cache.get(&key("a"), 1).is_some());
        assert!(cache.remember(&key("c"), 1, Arc::new(json!({})), 8, None, true, false));
        assert!(cache.get(&key("b"), 1).is_none());
        assert!(cache.get(&key("a"), 1).is_some());
        assert!(!cache.remember(&key("d"), 1, Arc::new(json!({})), 21, None, true, false));
        let stats = cache.stats();
        assert_eq!(stats["capacity_evictions"], 1);
        assert_eq!(stats["oversize_rejections"], 1);
        assert_eq!(stats["entries"], 2);
        assert_eq!(stats["charged_bytes"], 16);
        assert!(cache.discard(&key("a")));
        assert_eq!(cache.clear(), 1);
    }
}
