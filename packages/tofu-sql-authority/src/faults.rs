//! Explicit fault points for isolated certification processes (`faults.py`).
//! Disabled unless `TOFU_STORAGE_ENABLE_FAULT_INJECTION=1`; each point named
//! in `TOFU_STORAGE_FAULT_ONCE` raises exactly once per process.

use std::collections::BTreeSet;
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::error::{OpResult, StorageError};

static CONSUMED: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

fn enabled() -> bool {
    std::env::var("TOFU_STORAGE_ENABLE_FAULT_INJECTION").as_deref() == Ok("1")
}

fn requested() -> BTreeSet<String> {
    std::env::var("TOFU_STORAGE_FAULT_ONCE")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(str::to_owned)
        .collect()
}

pub fn status() -> Value {
    let enabled = enabled();
    let requested: Vec<String> = if enabled { requested().into_iter().collect() } else { Vec::new() };
    let consumed: Vec<String> =
        if enabled { CONSUMED.lock().expect("faults").iter().cloned().collect() } else { Vec::new() };
    json!({"enabled": enabled, "requested": requested, "consumed": consumed})
}

/// `inject_once(point)`.
pub fn inject_once(point: &str) -> OpResult<()> {
    if !enabled() || !requested().contains(point) {
        return Ok(());
    }
    if !CONSUMED.lock().expect("faults").insert(point.to_owned()) {
        return Ok(());
    }
    Err(StorageError::new("database_internal", format!("Injected storage fault at {point}")).into())
}
