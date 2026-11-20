//! `lib/mcp/updates.py` port: the parts with no Python counterpart to diff
//! against at the host boundary (the cache's clock) and Unicode digit values.

use std::time::{Duration, Instant};
use tofu_mcp::updates::{
    compare_versions, parse_version, LatestCache, LATEST_CACHE_MAX_SIZE, LATEST_CACHE_TTL,
};

fn key(n: usize) -> (String, String) {
    ("pypi".to_string(), format!("p{n}"))
}

#[test]
fn the_latest_cache_expires_after_its_ttl() {
    let mut cache = LatestCache::default();
    let start = Instant::now();
    cache.set(key(0), "1.0".into(), start);
    assert_eq!(
        cache.get(&key(0), start + LATEST_CACHE_TTL - Duration::from_secs(1)),
        Some("1.0".into())
    );
    assert_eq!(cache.get(&key(0), start + LATEST_CACHE_TTL), None);
    assert!(cache.is_empty());
}

#[test]
fn the_latest_cache_evicts_the_least_recently_used_sighting() {
    let mut cache = LatestCache::default();
    let start = Instant::now();
    for n in 0..LATEST_CACHE_MAX_SIZE {
        cache.set(key(n), "1.0".into(), start);
    }
    // Reading p0 makes p1 the least recently used when the next entry arrives.
    assert!(cache.get(&key(0), start).is_some());
    cache.set(key(LATEST_CACHE_MAX_SIZE), "1.0".into(), start);
    assert_eq!(cache.len(), LATEST_CACHE_MAX_SIZE);
    assert!(cache.get(&key(0), start).is_some());
    assert!(cache.get(&key(1), start).is_none());
    cache.invalidate(&key(0));
    assert!(cache.get(&key(0), start).is_none());
}

#[test]
fn unicode_digits_take_their_int_value() {
    let key = parse_version("\u{0663}\u{0660}.\u{1D7D9}").expect("Nd digits parse");
    assert_eq!(key.release, vec!["30".to_string(), "1".to_string()]);
    assert_eq!(compare_versions("\u{0661}.0", "1"), Some(0));
    assert_eq!(
        compare_versions("99999999999999999999999.1", "99999999999999999999998.9"),
        Some(1)
    );
}
