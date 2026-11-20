//! Process-lifetime attempt-event write counters
//! (`operations_pkg/_turns_core.py::_attempt_event_metrics`), reported as
//! `system.metrics.attempt_events`. Like Python, observations are process
//! counters: a rolled-back transaction still counted its attempted write.

use std::collections::BTreeMap;
use std::sync::Mutex;

use serde_json::{json, Value};

pub const ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES: usize = 4 * 1024 * 1024;

#[derive(Default)]
struct TypeCounters {
    accepted_events: i64,
    accepted_payload_bytes: i64,
    max_accepted_payload_bytes: i64,
    rejected_events: i64,
    rejected_payload_bytes: i64,
    max_rejected_payload_bytes: i64,
    projection_blob_write_skips: i64,
    projection_blob_write_skipped_bytes: i64,
    projection_blob_write_deferrals: i64,
    projection_blob_write_deferred_bytes: i64,
    projection_checkpoint_materializations: i64,
    projection_checkpoint_materialized_bytes: i64,
    projection_inline_released_bytes: i64,
}

static METRICS: Mutex<BTreeMap<String, TypeCounters>> = Mutex::new(BTreeMap::new());

fn with_type(event_type: &str, update: impl FnOnce(&mut TypeCounters)) {
    let mut metrics = METRICS.lock().expect("attempt event metrics");
    update(metrics.entry(event_type.to_owned()).or_default());
}

/// `_observe_attempt_event_payload`.
pub fn observe_payload(event_type: &str, payload_bytes: i64, accepted: bool) {
    with_type(event_type, |metrics| {
        if accepted {
            metrics.accepted_events += 1;
            metrics.accepted_payload_bytes += payload_bytes;
            metrics.max_accepted_payload_bytes = metrics.max_accepted_payload_bytes.max(payload_bytes);
        } else {
            metrics.rejected_events += 1;
            metrics.rejected_payload_bytes += payload_bytes;
            metrics.max_rejected_payload_bytes = metrics.max_rejected_payload_bytes.max(payload_bytes);
        }
    });
}

/// `_observe_projection_blob_write_skip`.
pub fn observe_blob_write_skip(event_type: &str, projection_bytes: i64) {
    with_type(event_type, |metrics| {
        metrics.projection_blob_write_skips += 1;
        metrics.projection_blob_write_skipped_bytes += projection_bytes;
    });
}

/// `_observe_projection_blob_write_deferral`.
pub fn observe_blob_write_deferral(event_type: &str, projection_bytes: i64) {
    with_type(event_type, |metrics| {
        metrics.projection_blob_write_deferrals += 1;
        metrics.projection_blob_write_deferred_bytes += projection_bytes.max(0);
    });
}

/// `_observe_projection_checkpoint_materialization`.
pub fn observe_checkpoint_materialization(event_type: &str, checkpoint_bytes: i64, inline_released_bytes: i64) {
    with_type(event_type, |metrics| {
        metrics.projection_checkpoint_materializations += 1;
        metrics.projection_checkpoint_materialized_bytes += checkpoint_bytes.max(0);
        metrics.projection_inline_released_bytes += inline_released_bytes.max(0);
    });
}

/// `attempt_event_write_metrics()`.
pub fn snapshot() -> Value {
    let metrics = METRICS.lock().expect("attempt event metrics");
    let mut by_type = serde_json::Map::new();
    for (event_type, values) in metrics.iter() {
        by_type.insert(
            event_type.clone(),
            json!({
                "accepted_events": values.accepted_events,
                "accepted_payload_bytes": values.accepted_payload_bytes,
                "max_accepted_payload_bytes": values.max_accepted_payload_bytes,
                "rejected_events": values.rejected_events,
                "rejected_payload_bytes": values.rejected_payload_bytes,
                "max_rejected_payload_bytes": values.max_rejected_payload_bytes,
                "projection_blob_write_skips": values.projection_blob_write_skips,
                "projection_blob_write_skipped_bytes": values.projection_blob_write_skipped_bytes,
                "projection_blob_write_deferrals": values.projection_blob_write_deferrals,
                "projection_blob_write_deferred_bytes": values.projection_blob_write_deferred_bytes,
                "projection_checkpoint_materializations": values.projection_checkpoint_materializations,
                "projection_checkpoint_materialized_bytes": values.projection_checkpoint_materialized_bytes,
                "projection_inline_released_bytes": values.projection_inline_released_bytes,
            }),
        );
    }
    json!({
        "max_nonterminal_payload_bytes": ATTEMPT_EVENT_MAX_NONTERMINAL_BYTES,
        "by_type": by_type,
    })
}
