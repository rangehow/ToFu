//! Private port of `lib/turn_trace_contract.py`: bounded timing-trace
//! documents (`compact_trace_document`, `merge_client_trace_evidence`,
//! `append_client_trace_observation`).

use serde_json::{json, Value};

use super::core::{py_int_or_zero, Obj};
use super::normalize::py_json_dumps;
use crate::error::OpResult;
use crate::json::{python_str, truthy};
use crate::payload::get;

pub const TRACE_CONTRACT_VERSION: i64 = 1;
const TRACE_MAX_SPANS: usize = 256;
const TRACE_MAX_GAPS: usize = 128;
const TRACE_MAX_STATUS_ENTRIES: usize = 128;
const TRACE_MAX_CLIENT_OBSERVATIONS: usize = 64;
const TRACE_MAX_PERSISTED_BYTES: usize = 96 * 1024;
const TRACE_MAX_COUNTER: i64 = 2_147_483_647;
const COMPACTION_MARKERS: &[&str] = &[
    "droppedSpans",
    "droppedGaps",
    "statusDroppedCount",
    "clientObservationDroppedCount",
    "overBudgetDroppedCount",
    "detailCompacted",
];

/// `_bounded_trace_text(value, maximum)`.
pub fn bounded_trace_text(value: &Value, maximum: usize) -> String {
    if value.is_null() {
        return String::new();
    }
    python_str(value).chars().take(maximum).collect()
}

fn sanitize(value: &Value, depth: usize) -> Value {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::String(text) => Value::String(text.chars().take(400).collect()),
        _ if depth >= 5 => Value::String(bounded_trace_text(value, 200)),
        Value::Object(map) => {
            let mut out = Obj::new();
            for (key, child) in map.iter().take(32) {
                let name: String = key.chars().take(100).collect();
                if name.is_empty() || name.starts_with('_') {
                    continue;
                }
                out.insert(name, sanitize(child, depth + 1));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().take(32).map(|child| sanitize(child, depth + 1)).collect()),
    }
}

fn bounded_rows(rows: &Value, maximum: usize, keep_recent: bool) -> OpResult<(Vec<Value>, i64)> {
    let items: Vec<Value> = match rows {
        _ if !truthy(rows) => vec![],
        Value::Array(items) => items.clone(),
        Value::Object(map) => map.keys().map(|key| Value::String(key.clone())).collect(),
        Value::String(text) => text.chars().map(|c| Value::String(c.to_string())).collect(),
        _ => return crate::error::internal("trace rows are not iterable"),
    };
    let clean: Vec<Value> = items.iter().filter(|row| row.is_object()).map(|row| sanitize(row, 0)).collect();
    if clean.len() <= maximum {
        return Ok((clean, 0));
    }
    let dropped = (clean.len() - maximum) as i64;
    if keep_recent {
        return Ok((clean[clean.len() - maximum..].to_vec(), dropped));
    }
    let head = maximum / 2;
    let tail = maximum - head;
    let mut kept = clean[..head].to_vec();
    kept.extend_from_slice(&clean[clean.len() - tail..]);
    Ok((kept, dropped))
}

fn encoded_size(out: &Obj) -> usize {
    py_json_dumps(&Value::Object(out.clone()), false).len()
}

fn rows_len(out: &Obj, key: &str) -> usize {
    match out.get(key) {
        Some(Value::Array(rows)) => rows.len(),
        _ => 0,
    }
}

fn bump(out: &mut Obj, key: &str, amount: i64) -> OpResult<()> {
    let total = (py_int_or_zero(get(out, key))? + amount).min(TRACE_MAX_COUNTER);
    out.insert(key.into(), Value::from(total));
    Ok(())
}

/// `compact_trace_document(document, source=)`.
pub fn compact_trace_document(document: &Value, source: &Value) -> OpResult<Obj> {
    let Value::Object(document) = document else { return Ok(Obj::new()) };
    let mut out = Obj::new();
    for key in [
        "version",
        "taskId",
        "eventsAvailable",
        "eventLogAvailable",
        "status",
        "running",
        "coverage",
        "coverageReason",
        "clockSource",
        "tStart",
        "tEnd",
        "totalMs",
    ] {
        if let Some(value) = document.get(key) {
            out.insert(key.into(), sanitize(value, 0));
        }
    }
    let source = if truthy(source) { source.clone() } else { get(document, "source").clone() };
    if truthy(&source) {
        out.insert("source".into(), Value::String(bounded_trace_text(&source, 40)));
    }
    if truthy(get(document, "detailCompacted")) {
        out.insert("detailCompacted".into(), json!(true));
    }
    if truthy(get(document, "compacted")) {
        out.insert("compacted".into(), json!(true));
    }
    let mut dropped_over_budget = 0;
    if let Some(summary @ Value::Object(raw)) = document.get("summary") {
        let mut sanitized = sanitize(summary, 0);
        if let Some(over @ Value::Array(_)) = raw.get("overBudget") {
            let (rows, dropped) = bounded_rows(over, TRACE_MAX_SPANS, false)?;
            dropped_over_budget = dropped;
            sanitized["overBudget"] = Value::Array(rows);
        }
        out.insert("summary".into(), sanitized);
    }
    let (spans, dropped_spans) = bounded_rows(get(document, "spans"), TRACE_MAX_SPANS, false)?;
    let (gaps, dropped_gaps) = bounded_rows(get(document, "gaps"), TRACE_MAX_GAPS, false)?;
    let (statuses, dropped_statuses) = bounded_rows(get(document, "statusHistory"), TRACE_MAX_STATUS_ENTRIES, true)?;
    let (observations, dropped_observations) =
        bounded_rows(get(document, "clientObservations"), TRACE_MAX_CLIENT_OBSERVATIONS, true)?;
    for (key, rows) in
        [("spans", spans), ("gaps", gaps), ("statusHistory", statuses), ("clientObservations", observations)]
    {
        if !rows.is_empty() || document.contains_key(key) {
            out.insert(key.into(), Value::Array(rows));
        }
    }
    for (key, newly) in [
        ("droppedSpans", dropped_spans),
        ("droppedGaps", dropped_gaps),
        ("statusDroppedCount", dropped_statuses),
        ("clientObservationDroppedCount", dropped_observations),
        ("overBudgetDroppedCount", dropped_over_budget),
    ] {
        let total = (py_int_or_zero(get(document, key))?.max(0) + newly).min(TRACE_MAX_COUNTER);
        if total != 0 {
            out.insert(key.into(), Value::from(total));
            out.insert("compacted".into(), json!(true));
        }
    }
    let marker = |out: &Obj| COMPACTION_MARKERS.iter().any(|key| truthy(get(out, key)));
    if encoded_size(&out) <= TRACE_MAX_PERSISTED_BYTES {
        if marker(&out) {
            out.insert("compacted".into(), json!(true));
        }
        return Ok(out);
    }
    for (key, floor, dropped_key) in [
        ("spans", 24, "droppedSpans"),
        ("statusHistory", 16, "statusDroppedCount"),
        ("gaps", 16, "droppedGaps"),
        ("clientObservations", 16, "clientObservationDroppedCount"),
    ] {
        while encoded_size(&out) > TRACE_MAX_PERSISTED_BYTES && rows_len(&out, key) > floor {
            let Some(Value::Array(rows)) = out.get_mut(key) else { break };
            let remove = (rows.len() / 4).max(1);
            if key == "statusHistory" || key == "clientObservations" {
                rows.drain(..remove);
            } else {
                let middle = ((rows.len() - remove) / 2).max(1);
                rows.drain(middle..(middle + remove).min(rows.len()));
            }
            bump(&mut out, dropped_key, remove as i64)?;
            out.insert("compacted".into(), json!(true));
        }
    }
    if encoded_size(&out) > TRACE_MAX_PERSISTED_BYTES {
        if let Some(Value::Array(spans)) = out.get_mut("spans") {
            for span in spans.iter_mut() {
                if let Value::Object(span) = span {
                    if truthy(get(span, "attrs")) {
                        span.insert("attrs".into(), json!({}));
                    }
                }
            }
        }
        out.insert("detailCompacted".into(), json!(true));
        out.insert("compacted".into(), json!(true));
    }
    for (key, floor, dropped_key) in [
        ("gaps", 0, "droppedGaps"),
        ("spans", 1, "droppedSpans"),
        ("statusHistory", 1, "statusDroppedCount"),
        ("clientObservations", 1, "clientObservationDroppedCount"),
    ] {
        while encoded_size(&out) > TRACE_MAX_PERSISTED_BYTES && rows_len(&out, key) > floor {
            let Some(Value::Array(rows)) = out.get_mut(key) else { break };
            let remove = (rows.len() - floor).min(rows.len() / 4).max(1);
            if key == "statusHistory" || key == "clientObservations" {
                rows.drain(..remove);
            } else {
                let middle = (rows.len() - remove) / 2;
                rows.drain(middle..(middle + remove).min(rows.len()));
            }
            bump(&mut out, dropped_key, remove as i64)?;
            out.insert("detailCompacted".into(), json!(true));
            out.insert("compacted".into(), json!(true));
        }
    }
    loop {
        if encoded_size(&out) <= TRACE_MAX_PERSISTED_BYTES {
            break;
        }
        let Some(Value::Object(summary)) = out.get_mut("summary") else { break };
        let Some(Value::Array(over)) = summary.get_mut("overBudget") else { break };
        if over.is_empty() {
            break;
        }
        let index = over.len() / 2;
        over.remove(index);
        bump(&mut out, "overBudgetDroppedCount", 1)?;
        out.insert("detailCompacted".into(), json!(true));
        out.insert("compacted".into(), json!(true));
    }
    if encoded_size(&out) > TRACE_MAX_PERSISTED_BYTES {
        for (key, dropped_key) in [
            ("gaps", "droppedGaps"),
            ("spans", "droppedSpans"),
            ("statusHistory", "statusDroppedCount"),
            ("clientObservations", "clientObservationDroppedCount"),
        ] {
            let removed = rows_len(&out, key);
            if removed != 0 {
                out.insert(key.into(), json!([]));
                bump(&mut out, dropped_key, removed as i64)?;
            }
        }
        if let Some(Value::Object(summary)) = out.get("summary").cloned() {
            let remaining = match summary.get("overBudget") {
                Some(Value::Array(rows)) => rows.len(),
                _ => 0,
            };
            if remaining != 0 {
                bump(&mut out, "overBudgetDroppedCount", remaining as i64)?;
            }
            let mut kept = Obj::new();
            for key in [
                "totalMs",
                "llmMs",
                "toolMs",
                "waitMs",
                "compactionMs",
                "approvalWaitMs",
                "flowStepMs",
                "orchestrationMs",
                "unattributedMs",
                "ttftMs",
                "failedAttemptMs",
                "admissionWaitMs",
                "retryWaitMs",
                "spawnWaitMs",
                "startupMs",
                "roundPrepMs",
                "roundSettleMs",
                "finalizeMs",
            ] {
                if let Some(value) = summary.get(key) {
                    kept.insert(key.into(), value.clone());
                }
            }
            kept.insert("overBudget".into(), json!([]));
            out.insert("summary".into(), Value::Object(kept));
        }
        out.insert("detailCompacted".into(), json!(true));
        out.insert("compacted".into(), json!(true));
    }
    if marker(&out) {
        out.insert("compacted".into(), json!(true));
    }
    Ok(out)
}

/// `merge_client_trace_evidence(server_trace, receipt_trace, task_id=)`.
pub fn merge_client_trace_evidence(server_trace: &Value, receipt_trace: &Value, task_id: &str) -> OpResult<Obj> {
    let mut merged = match server_trace {
        Value::Object(map) => map.clone(),
        _ => Obj::new(),
    };
    let receipts = match receipt_trace {
        Value::Object(map) => map.clone(),
        _ => Obj::new(),
    };
    let receipt_task = super::core::text(get(&receipts, "taskId"));
    if !receipt_task.is_empty() && receipt_task != task_id {
        let source = get(&merged, "source").clone();
        return compact_trace_document(&Value::Object(merged), &source);
    }
    if receipts.contains_key("clientObservations") {
        let observations = get(&receipts, "clientObservations");
        merged.insert("clientObservations".into(), if truthy(observations) { observations.clone() } else { json!([]) });
        if truthy(get(&receipts, "clientObservationDroppedCount")) {
            let count = py_int_or_zero(get(&receipts, "clientObservationDroppedCount"))?.max(0);
            merged.insert("clientObservationDroppedCount".into(), Value::from(count));
        } else {
            merged.shift_remove("clientObservationDroppedCount");
        }
    }
    if !merged.is_empty() {
        merged.insert("version".into(), json!(TRACE_CONTRACT_VERSION));
        merged.insert("taskId".into(), Value::String(task_id.chars().take(256).collect()));
    }
    let source = get(&merged, "source").clone();
    compact_trace_document(&Value::Object(merged), &source)
}

const TEXT_LIMITS: &[(&str, usize)] = &[
    ("observationId", 160),
    ("attemptId", 128),
    ("clientId", 64),
    ("phase", 80),
    ("detailKey", 160),
    ("reason", 160),
    ("healthState", 32),
    ("visibility", 16),
];
const NUMERIC_LIMITS: &[(&str, i64)] = &[
    ("serverEmittedAt", 9_007_199_254_740_991),
    ("receivedAt", 9_007_199_254_740_991),
    ("paintedAt", 9_007_199_254_740_991),
    ("observedAt", 9_007_199_254_740_991),
    ("durationMs", 9_007_199_254_740_991),
    ("generation", 2_147_483_647),
    ("projectionRevision", 9_007_199_254_740_991),
    ("retryCount", 2_147_483_647),
    ("clientDroppedBefore", 2_147_483_647),
];

fn python_repr_list(items: &[String]) -> String {
    python_str(&Value::Array(items.iter().map(|item| Value::String(item.clone())).collect()))
}

/// `append_client_trace_observation(...)`: `Err(message)` is a `ValueError`.
pub fn append_client_trace_observation(
    timing_trace: &Value,
    observation: &Obj,
    task_id: &str,
    attempt_id: &str,
    recorded_at: i64,
) -> OpResult<Result<(Obj, bool), String>> {
    let allowed = |key: &str| {
        key == "kind"
            || TEXT_LIMITS.iter().any(|(name, _)| *name == key)
            || NUMERIC_LIMITS.iter().any(|(name, _)| *name == key)
    };
    let mut unknown: Vec<String> = observation.keys().filter(|key| !allowed(key)).cloned().collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Ok(Err(format!("unsupported perception fields: {}", python_repr_list(&unknown))));
    }
    for (field, maximum) in TEXT_LIMITS {
        let value = get(observation, field);
        if matches!(*field, "observationId" | "attemptId" | "clientId") && !truthy(value) {
            return Ok(Err(format!("{field} is required")));
        }
        if !value.is_null() && !matches!(value, Value::String(text) if text.chars().count() <= *maximum) {
            return Ok(Err(format!("{field} must be a string of at most {maximum}")));
        }
    }
    let observation_id = super::core::text(get(observation, "observationId"));
    let id_ok = {
        let mut chars = observation_id.chars();
        chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
            && observation_id.chars().count() <= 160
            && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '~' | '-'))
    };
    if !id_ok {
        return Ok(Err("observationId has an invalid format".into()));
    }
    if super::core::text(get(observation, "attemptId")) != attempt_id {
        return Ok(Err("attemptId does not match the authoritative attempt".into()));
    }
    let visibility = get(observation, "visibility");
    if !visibility.is_null() && !matches!(visibility.as_str(), Some("visible" | "hidden")) {
        return Ok(Err("invalid perception visibility".into()));
    }
    let health = get(observation, "healthState");
    if !health.is_null()
        && !matches!(
            health.as_str(),
            Some("idle" | "connecting" | "live" | "recovering" | "degraded" | "offline" | "closed")
        )
    {
        return Ok(Err("invalid perception healthState".into()));
    }
    for (field, maximum) in NUMERIC_LIMITS {
        let value = get(observation, field);
        if value.is_null() {
            continue;
        }
        let valid = match value {
            Value::Number(number) => match number.as_i64() {
                Some(integer) => (0..=*maximum).contains(&integer),
                None => number.as_f64().is_some_and(|float| {
                    float.is_finite() && float.fract() == 0.0 && float >= 0.0 && float <= *maximum as f64
                }),
            },
            _ => false,
        };
        if !valid {
            return Ok(Err(format!("{field} must be a nonnegative safe integer")));
        }
    }
    let kind = super::core::text(get(observation, "kind"));
    if !matches!(kind.as_str(), "phase_painted" | "terminal_painted" | "transport_degraded" | "transport_recovered") {
        return Ok(Err("invalid perception observation kind".into()));
    }
    let mut trace = match timing_trace {
        Value::Object(map) => map.clone(),
        _ => Obj::new(),
    };
    let existing = get(&trace, "clientObservations");
    let mut observations: Vec<Value> = match existing {
        _ if !truthy(existing) => vec![],
        Value::Array(items) => items.iter().filter(|item| item.is_object()).cloned().collect(),
        _ => vec![],
    };
    if observations
        .iter()
        .any(|item| get(item.as_object().expect("object"), "observationId") == &Value::String(observation_id.clone()))
    {
        let compacted = compact_trace_document(&Value::Object(trace), &Value::Null)?;
        return Ok(Ok((compacted, false)));
    }
    let mut row = Obj::new();
    row.insert("observationId".into(), Value::String(observation_id));
    row.insert("kind".into(), Value::String(kind));
    row.insert("taskId".into(), Value::String(task_id.chars().take(256).collect()));
    row.insert("attemptId".into(), Value::String(attempt_id.chars().take(128).collect()));
    row.insert("clientId".into(), Value::String(bounded_trace_text(get(observation, "clientId"), 64)));
    row.insert("recordedAt".into(), Value::from(recorded_at.max(0)));
    for (source, maximum) in
        [("phase", 80), ("detailKey", 160), ("reason", 160), ("healthState", 32), ("visibility", 16)]
    {
        let value = bounded_trace_text(get(observation, source), maximum);
        if !value.is_empty() {
            row.insert(source.into(), Value::String(value));
        }
    }
    for (field, _) in NUMERIC_LIMITS {
        let value = get(observation, field);
        if value.is_null() {
            continue;
        }
        let integer = value.as_i64().or_else(|| value.as_f64().map(|float| float as i64)).unwrap_or(0);
        row.insert((*field).into(), Value::from(integer));
    }
    let received = row.get("receivedAt").and_then(Value::as_i64);
    let painted = row.get("paintedAt").and_then(Value::as_i64);
    if let (Some(received), Some(painted)) = (received, painted) {
        row.insert("renderMs".into(), Value::from((painted - received).clamp(0, 600_000)));
    }
    let emitted = row.get("serverEmittedAt").and_then(Value::as_i64);
    if let (Some(emitted), Some(received)) = (emitted, received) {
        let transport = received - emitted;
        if (-60_000..=24 * 60 * 60 * 1000).contains(&transport) {
            row.insert("transportMs".into(), Value::from(transport.max(0)));
            if transport < 0 {
                row.insert("clockSkewSuspected".into(), json!(true));
            }
        } else {
            row.insert("clockSkewSuspected".into(), json!(true));
        }
    }
    observations.push(Value::Object(row));
    let overflow = observations.len().saturating_sub(TRACE_MAX_CLIENT_OBSERVATIONS);
    if overflow != 0 {
        observations.drain(..overflow);
    }
    trace.insert("version".into(), json!(TRACE_CONTRACT_VERSION));
    trace.insert("taskId".into(), Value::String(task_id.chars().take(256).collect()));
    trace.insert("clientObservations".into(), Value::Array(observations));
    if overflow != 0 {
        let total = (py_int_or_zero(get(&trace, "clientObservationDroppedCount"))?.max(0) + overflow as i64)
            .min(TRACE_MAX_COUNTER);
        trace.insert("clientObservationDroppedCount".into(), Value::from(total));
    }
    let source = get(&trace, "source").clone();
    Ok(Ok((compact_trace_document(&Value::Object(trace), &source)?, true)))
}
