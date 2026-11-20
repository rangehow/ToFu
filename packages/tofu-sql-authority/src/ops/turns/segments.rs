//! Private port of `lib/turn_projection_segments.py::projection_with_stable_segments`
//! and the pure owners it calls (`lib/tasks_pkg/segments/_assemble.py`,
//! `_serde.py::segments_to_json`, `_types.py`,
//! `lib/tool_results/tool_round_identity.py`,
//! `tool_round_replay.py::is_superseded_provider_attempt_round`).
//! Copied from `ops/conversations/segments.rs` (shared owner: conversations);
//! `injected_notes` is `None` at every turn call site.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};

use crate::codec::py_eq;
use crate::error::{internal, OpResult};
use crate::json::{python_str, truthy};
use crate::payload::get;

use super::normalize::{normalize_projection_document, py_strip};

type Obj = Map<String, Value>;

const SEGMENT_TYPES: &[&str] = &["text", "thinking", "tool_use", "system_note"];
const RESUMABLE_TURN_STATUSES: &[&str] = &["interrupted", "truncated"];
/// `generated_contract.OPENAPI_SCHEMAS['TurnSystemNoteSegment']` (contracts/conversation_sync_v3.yaml).
const NOTE_KINDS: &[&str] = &[
    "intent-stall",
    "todo-continuation",
    "system-intervention",
    "context-injection",
    "session-context",
    "deliberate-silence",
];
const NOTE_ACTIONS: &[&str] = &["prompt", "stop", "tool-policy", "continue"];
const NOTE_REASON_LIMIT: usize = 160;
const SYNTHETIC_INBOX_MARKERS: &[&str] =
    &["_inboxInject", "_peerInject", "_userSteerInject", "_stallNudge", "_programSynthetic"];
const LIVE_ROUND_STATUSES: &[&str] =
    &["queued", "searching", "executing", "pending_approval", "awaiting_human", "awaiting_stdin"];
const CONTINUATION_BLOCK_PREFIXES: &[&str] = &["thinking:continuation-", "text:continuation-"];
const SUPERSEDED_PROVIDER_ATTEMPT_FIELD: &str = "_providerAttemptDiscarded";
const MAX_EXECUTION_ID_CHARS: usize = 256;
const MAX_LLM_ROUND_TEXT_CHARS: usize = 64;
const MAX_SAFE_INTEGER: i128 = 9_007_199_254_740_991;

/// `str(value or "")`.
fn text(value: &Value) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        String::new()
    }
}

/// `str(value or default)`.
fn text_or(value: &Value, default: &str) -> String {
    if truthy(value) {
        python_str(value)
    } else {
        default.to_owned()
    }
}

/// `str(value or "").strip()`.
fn stripped(value: &Value) -> String {
    py_strip(&text(value)).to_owned()
}

/// `value in frozenset(members)`: an unhashable value raises `TypeError`.
fn value_in(value: &Value, members: &[&str]) -> OpResult<bool> {
    match value {
        Value::Array(_) | Value::Object(_) => internal("unhashable type"),
        Value::String(text) => Ok(members.contains(&text.as_str())),
        _ => Ok(false),
    }
}

/// `isinstance(value, int) and not isinstance(value, bool)`.
fn strict_int(value: &Value) -> Option<i128> {
    match value {
        Value::Number(number) => number.as_i64().map(i128::from).or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

fn is_type(segment: &Obj, kind: &str) -> bool {
    matches!(segment.get("type"), Some(Value::String(value)) if value == kind)
}

// ── tool_round_identity.py ───────────────────────────────────────────────

fn identity_text(value: &Value) -> String {
    let raw = match value {
        Value::String(text) => text.clone(),
        Value::Number(number) if number.is_i64() || number.is_u64() => python_str(value),
        _ => return String::new(),
    };
    let text: String = py_strip(&raw).chars().take(MAX_EXECUTION_ID_CHARS).collect();
    if text.starts_with("@dispatching:") {
        String::new()
    } else {
        text
    }
}

/// `execution_llm_round(value)`: the raw int or the stripped bounded string.
pub(super) fn execution_llm_round(value: &Obj) -> Option<Value> {
    match get(value, "llmRound") {
        raw @ Value::Number(_) if strict_int(raw).is_some() => Some(raw.clone()),
        Value::String(raw) => {
            let normalized = py_strip(raw);
            (!normalized.is_empty() && normalized.chars().count() <= MAX_LLM_ROUND_TEXT_CHARS)
                .then(|| Value::String(normalized.to_owned()))
        }
        _ => None,
    }
}

/// `execution_identity(value)`.
pub(super) fn execution_identity(value: &Obj) -> (String, String) {
    let or = |first: &str, second: &str| {
        let primary = get(value, first);
        identity_text(if truthy(primary) { primary } else { get(value, second) })
    };
    (or("attemptId", "_attemptId"), or("taskId", "_taskId"))
}

/// `model_batch_block_suffix(llm_round, attempt_id=, task_id=)`.
fn model_batch_block_suffix(llm_round: &Value, attempt_id: &str, task_id: &str) -> String {
    let attempt = identity_text(&Value::String(attempt_id.to_owned()));
    let scope = if attempt.is_empty() { identity_text(&Value::String(task_id.to_owned())) } else { attempt };
    let prefix = if scope.is_empty() { String::new() } else { format!("attempt-{scope}:") };
    format!("{prefix}llm-{}", python_str(llm_round))
}

/// `execution_batch_key` base tuple: `("llm", scope, llm_round)` or `("position", scope, position)`.
#[derive(Clone, PartialEq, Eq, Hash)]
struct BaseKey {
    llm: bool,
    scope: String,
    ident: String,
}

/// `(*base, "occurrence", occurrence)`.
type BatchKey = (BaseKey, usize);

fn execution_batch_key(value: &Obj, position: usize) -> BaseKey {
    let (attempt_id, task_id) = execution_identity(value);
    let scope = if attempt_id.is_empty() { task_id } else { attempt_id };
    match execution_llm_round(value) {
        Some(Value::String(text)) => BaseKey { llm: true, scope, ident: format!("s{text}") },
        Some(number) => BaseKey { llm: true, scope, ident: format!("i{}", python_str(&number)) },
        None => BaseKey { llm: false, scope, ident: position.to_string() },
    }
}

fn round_number(value: &Obj) -> Option<i128> {
    let mut raw = get(value, "roundNum");
    if raw.is_null() {
        if let Some(Value::Object(round)) = value.get("_round") {
            raw = get(round, "roundNum");
        }
    }
    strict_int(raw)
}

fn is_tool_carrier(value: &Obj) -> bool {
    is_type(value, "tool_use") || truthy(get(value, "toolCallId"))
}

/// `execution_batch_keys(values)`.
fn execution_batch_keys(values: &[Value]) -> OpResult<Vec<BatchKey>> {
    let empty = Obj::new();
    let mut occurrence_counts: HashMap<BaseKey, usize> = HashMap::new();
    let mut keys = Vec::with_capacity(values.len());
    let mut previous_base: Option<BaseKey> = None;
    let mut previous_value: Option<&Obj> = None;
    let mut current_key: Option<BatchKey> = None;
    let mut current_has_tool = false;
    for (position, raw_value) in values.iter().enumerate() {
        let value = raw_value.as_object().unwrap_or(&empty);
        let base = execution_batch_key(value, position);
        let (attempt_id, task_id) = execution_identity(value);
        let unscoped = attempt_id.is_empty() && task_id.is_empty();
        let mut starts_new_occurrence = current_key.is_none() || previous_base.as_ref() != Some(&base);
        if !starts_new_occurrence && unscoped {
            if let Some(previous) = previous_value {
                let previous_round = round_number(previous);
                let current_round = round_number(value);
                let counter_restarted = matches!((previous_round, current_round), (Some(p), Some(c)) if c <= p);
                let prose_after_tools = current_has_tool
                    && value_in(get(value, "type"), &["text", "thinking"])?
                    && !truthy(get(value, "terminal"));
                let round_prose_after_tools =
                    current_has_tool && (truthy(get(value, "assistantContent")) || truthy(get(value, "thinking")));
                starts_new_occurrence = counter_restarted || prose_after_tools || round_prose_after_tools;
            }
        }
        if starts_new_occurrence {
            let counter = occurrence_counts.entry(base.clone()).or_insert(0);
            current_key = Some((base.clone(), *counter));
            *counter += 1;
            current_has_tool = false;
        }
        keys.push(current_key.clone().expect("initialized by the first row"));
        current_has_tool = current_has_tool || is_tool_carrier(value);
        previous_base = Some(base);
        previous_value = Some(value);
    }
    Ok(keys)
}

// ── tasks_pkg/segments ───────────────────────────────────────────────────

/// `is_synthetic_inbox_round(round)` for a decoded mapping.
fn is_synthetic_inbox_round(round: &Obj) -> bool {
    SYNTHETIC_INBOX_MARKERS.iter().any(|key| truthy(get(round, key)))
}

/// `is_superseded_provider_attempt_round(value)`.
fn is_superseded_provider_attempt_round(value: &Obj) -> bool {
    if get(value, SUPERSEDED_PROVIDER_ATTEMPT_FIELD) == &Value::Bool(true) {
        return true;
    }
    let Some(Value::Array(results)) = value.get("results") else { return false };
    let Some(Value::Object(first)) = results.first() else { return false };
    if get(first, "badge") != &json!("superseded") {
        return false;
    }
    let has_fetched_chars = match get(first, "fetchedChars") {
        Value::Number(number) => number.as_f64().is_some_and(|chars| chars > 0.0),
        _ => false,
    };
    let has_real_result =
        !get(value, "toolContent").is_null() || get(first, "fetched") == &Value::Bool(true) || has_fetched_chars;
    !has_real_result
}

/// `_round_block_suffix(round_dict, position)`.
fn round_block_suffix(round: &Obj, position: usize) -> String {
    let (attempt_id, task_id) = execution_identity(round);
    if let Some(llm_round) = execution_llm_round(round) {
        return model_batch_block_suffix(&llm_round, &attempt_id, &task_id);
    }
    let scope = if attempt_id.is_empty() { task_id } else { attempt_id };
    let prefix = if scope.is_empty() { String::new() } else { format!("attempt-{scope}:") };
    match get(round, "roundNum") {
        Value::Null => format!("{prefix}legacy-{position}"),
        number => format!("{prefix}round-{}", python_str(number)),
    }
}

/// `tool_use_segment_from_round(round_dict, position)`.
fn tool_use_segment_from_round(round: &Obj, position: usize) -> Obj {
    let mut result = Obj::new();
    result.insert("content".into(), get(round, "toolContent").clone());
    if let Some(Value::String(status)) = round.get("status") {
        if !status.is_empty() {
            result.insert("status".into(), Value::String(status.clone()));
        }
    }
    let tool_call_id = stripped(get(round, "toolCallId"));
    let block_id = if tool_call_id.is_empty() {
        format!("tool:{}", round_block_suffix(round, position))
    } else {
        format!("tool:{tool_call_id}")
    };
    let mut segment = Obj::new();
    segment.insert("type".into(), json!("tool_use"));
    segment.insert("blockId".into(), Value::String(block_id));
    segment.insert("id".into(), round.get("toolCallId").cloned().unwrap_or_else(|| json!("")));
    segment.insert("name".into(), round.get("toolName").cloned().unwrap_or_else(|| json!("")));
    segment.insert("input".into(), round.get("toolArgs").cloned().unwrap_or_else(|| json!("")));
    segment.insert("llmRound".into(), execution_llm_round(round).unwrap_or(Value::Null));
    segment.insert("result".into(), Value::Object(result));
    segment.insert("_round".into(), Value::Object(round.clone()));
    let round_num = get(round, "roundNum");
    if strict_int(round_num).is_some_and(|number| (0..=MAX_SAFE_INTEGER).contains(&number)) {
        segment.insert("roundNum".into(), round_num.clone());
    }
    if !get(round, "toolModelContent").is_null() {
        if let Some(Value::Object(result)) = segment.get_mut("result") {
            result.insert("modelContent".into(), round["toolModelContent"].clone());
        }
    }
    let (attempt_id, task_id) = execution_identity(round);
    if !attempt_id.is_empty() {
        segment.insert("attemptId".into(), Value::String(attempt_id));
    }
    if !task_id.is_empty() {
        segment.insert("taskId".into(), Value::String(task_id));
    }
    if is_superseded_provider_attempt_round(round) {
        segment.insert(SUPERSEDED_PROVIDER_ATTEMPT_FIELD.into(), Value::Bool(true));
    }
    segment
}

/// `segments_to_json(segments)`.
fn segments_to_json(segments: Vec<Obj>) -> Vec<Obj> {
    segments
        .into_iter()
        .map(|mut segment| {
            if is_type(&segment, "tool_use") {
                segment.shift_remove("_round");
            }
            segment
        })
        .collect()
}

fn identity_fields(segment: &mut Obj, attempt_id: &str, task_id: &str) {
    if !attempt_id.is_empty() {
        segment.insert("attemptId".into(), Value::String(attempt_id.to_owned()));
    }
    if !task_id.is_empty() {
        segment.insert("taskId".into(), Value::String(task_id.to_owned()));
    }
}

fn prose_segment(kind: &str, text: &str, block_id: String, llm_round: &Option<Value>) -> Obj {
    let mut segment = Obj::new();
    segment.insert("type".into(), Value::String(kind.to_owned()));
    segment.insert("text".into(), Value::String(text.to_owned()));
    segment.insert("blockId".into(), Value::String(block_id));
    segment.insert("deliverable".into(), Value::Bool(false));
    segment.insert("llmRound".into(), llm_round.clone().unwrap_or(Value::Null));
    segment
}

/// `_build_round_segments(r, idx, batch_key, new_batch)`.
fn build_round_segments(round: &Obj, index: usize, batch_key: &BatchKey, new_batch: bool) -> (Vec<Obj>, bool) {
    let llm_round = execution_llm_round(round);
    let mut segments = Vec::new();
    let mut batch_emitted_prose = false;
    if new_batch {
        let mut block_suffix = round_block_suffix(round, index);
        if batch_key.1 != 0 {
            block_suffix.push_str(&format!(":occurrence-{}", batch_key.1));
        }
        let (attempt_id, task_id) = execution_identity(round);
        let think = match round.get("thinking") {
            Some(Value::String(text)) => text.as_str(),
            _ => "",
        };
        if !think.is_empty() {
            let mut segment = prose_segment("thinking", think, format!("thinking:{block_suffix}"), &llm_round);
            identity_fields(&mut segment, &attempt_id, &task_id);
            if let Some(Value::String(signature)) = round.get("thinkingSignature") {
                if !signature.is_empty() {
                    segment.insert("signature".into(), Value::String(signature.clone()));
                }
            }
            segments.push(segment);
        }
        let assistant = match round.get("assistantContent") {
            Some(Value::String(text)) => text.as_str(),
            _ => "",
        };
        let model_assistant = match round.get("assistantModelContent") {
            Some(Value::String(text)) => text.as_str(),
            _ => assistant,
        };
        if !model_assistant.is_empty() && assistant.is_empty() {
            let mut segment = prose_segment("text", "", format!("text:{block_suffix}"), &llm_round);
            segment.insert("modelText".into(), Value::String(model_assistant.to_owned()));
            identity_fields(&mut segment, &attempt_id, &task_id);
            segments.push(segment);
        }
        if !assistant.is_empty() {
            let mut segment = prose_segment("text", assistant, format!("text:{block_suffix}"), &llm_round);
            if !model_assistant.is_empty() {
                segment.insert("modelText".into(), Value::String(model_assistant.to_owned()));
            }
            identity_fields(&mut segment, &attempt_id, &task_id);
            segments.push(segment);
        }
        batch_emitted_prose = !think.is_empty() || !assistant.is_empty();
    }
    segments.push(tool_use_segment_from_round(round, index));
    (segments, batch_emitted_prose)
}

/// `assemble_segments({"content": content, "thinking": thinking}, merged=rounds)`.
///
/// That task carries no continuation-prose records, injected notes or
/// `finishReason`, so those tail passes are the identity here.
fn assemble_segments(content: &str, thinking: &str, rounds: &[Value]) -> OpResult<Vec<Obj>> {
    let mut segments: Vec<Obj> = Vec::new();
    let mut previous_batch_key: Option<&BatchKey> = None;
    let ordered_batch_keys = execution_batch_keys(rounds)?;
    let mut trailing_first_tool: Option<usize> = None;
    let mut trailing_batch_live = false;
    let mut trailing_batch_prose = false;
    for (index, raw) in rounds.iter().enumerate() {
        let Value::Object(round) = raw else { continue };
        if is_synthetic_inbox_round(round) {
            continue;
        }
        let batch_key = &ordered_batch_keys[index];
        let new_batch = previous_batch_key != Some(batch_key);
        if new_batch {
            previous_batch_key = Some(batch_key);
        }
        let (round_segments, batch_emitted_prose) = build_round_segments(round, index, batch_key, new_batch);
        segments.extend(round_segments);
        if new_batch {
            trailing_first_tool = Some(segments.len() - 1);
            trailing_batch_live = false;
            trailing_batch_prose = batch_emitted_prose;
        }
        if value_in(get(round, "status"), LIVE_ROUND_STATUSES)? {
            trailing_batch_live = true;
        }
    }
    let mut terminal_segments = Vec::new();
    if !thinking.is_empty() {
        terminal_segments.push(terminal_segment("thinking", thinking, false, true));
    }
    if !content.is_empty() {
        terminal_segments.push(terminal_segment("text", content, true, true));
    }
    match trailing_first_tool {
        Some(insert_at) if !terminal_segments.is_empty() && trailing_batch_live && !trailing_batch_prose => {
            segments.splice(insert_at..insert_at, terminal_segments);
        }
        _ => segments.extend(terminal_segments),
    }
    Ok(segments)
}

/// A terminal block; assembly writes `text` before `blockId`, the projection boundary after it.
fn terminal_segment(kind: &str, text: &str, deliverable: bool, text_first: bool) -> Obj {
    let mut segment = Obj::new();
    segment.insert("type".into(), Value::String(kind.to_owned()));
    if text_first {
        segment.insert("text".into(), Value::String(text.to_owned()));
        segment.insert("blockId".into(), Value::String(format!("{kind}:terminal")));
    } else {
        segment.insert("blockId".into(), Value::String(format!("{kind}:terminal")));
        segment.insert("text".into(), Value::String(text.to_owned()));
    }
    segment.insert("deliverable".into(), Value::Bool(deliverable));
    segment.insert("terminal".into(), Value::Bool(true));
    segment
}

// ── _assemble.py note placement ──────────────────────────────────────────

/// `_note_anchor_threshold`: `-inf < int < +inf`.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Threshold {
    NoNote,
    At(i128),
    EveryNote,
}

fn note_anchor_threshold(segment: &Obj) -> Threshold {
    if truthy(get(segment, "terminal")) {
        return Threshold::EveryNote;
    }
    let Some(round) = strict_int(get(segment, "llmRound")) else { return Threshold::NoNote };
    let block_id = text(get(segment, "blockId"));
    if is_type(segment, "system_note") || CONTINUATION_BLOCK_PREFIXES.iter().any(|prefix| block_id.starts_with(prefix))
    {
        Threshold::At(round)
    } else {
        Threshold::At(round + 1)
    }
}

fn anchors_note(threshold: Threshold, llm_round: Option<i128>) -> bool {
    match llm_round {
        None => threshold == Threshold::EveryNote,
        Some(round) => Threshold::At(round) < threshold,
    }
}

fn outside_note_scope(attempt_id: &Value, task_id: &Value, note_attempt: &Value, note_task: &Value) -> bool {
    (truthy(note_attempt) && truthy(attempt_id) && !py_eq(note_attempt, attempt_id))
        || (truthy(note_task) && truthy(task_id) && !py_eq(note_task, task_id))
}

struct PlacedNote {
    gap: usize,
    segment: Obj,
    threshold: Threshold,
    attempt_id: Value,
    task_id: Value,
}

impl PlacedNote {
    fn new(gap: usize, segment: Obj) -> Self {
        let mut note = Self {
            gap,
            segment: Obj::new(),
            threshold: Threshold::NoNote,
            attempt_id: Value::Null,
            task_id: Value::Null,
        };
        note.replace(segment);
        note
    }

    fn replace(&mut self, segment: Obj) {
        self.threshold = note_anchor_threshold(&segment);
        self.attempt_id = get(&segment, "attemptId").clone();
        self.task_id = get(&segment, "taskId").clone();
        self.segment = segment;
    }
}

type NoteKey = (String, String, String);

/// `_NotePlacement`: notes live in an arena; `order` holds their sequence.
struct NotePlacement {
    blocks: Vec<Obj>,
    arena: Vec<PlacedNote>,
    order: Vec<usize>,
    by_identity: HashMap<NoteKey, usize>,
}

impl NotePlacement {
    fn new(segments: Vec<Obj>) -> Self {
        let mut placement =
            Self { blocks: Vec::new(), arena: Vec::new(), order: Vec::new(), by_identity: HashMap::new() };
        for segment in segments {
            if is_type(&segment, "system_note") {
                let (index, gap) = (placement.order.len(), placement.blocks.len());
                placement.insert(index, gap, segment);
            } else {
                placement.blocks.push(segment);
            }
        }
        placement
    }

    fn key(segment: &Obj) -> Option<NoteKey> {
        match segment.get("blockId") {
            Some(Value::String(block_id)) => {
                let (attempt_id, task_id) = execution_identity(segment);
                Some((block_id.clone(), attempt_id, task_id))
            }
            _ => None,
        }
    }

    fn insert(&mut self, index: usize, gap: usize, segment: Obj) {
        let key = Self::key(&segment);
        let id = self.arena.len();
        self.arena.push(PlacedNote::new(gap, segment));
        self.order.insert(index, id);
        if let Some(key) = key {
            self.by_identity.entry(key).or_insert(id);
        }
    }

    fn first_anchoring_block(&self, llm_round: Option<i128>, note_attempt: &Value, note_task: &Value) -> usize {
        let scoped = truthy(note_attempt) || truthy(note_task);
        let mut maximum = Threshold::NoNote;
        for (index, block) in self.blocks.iter().enumerate() {
            let threshold = if scoped
                && outside_note_scope(get(block, "attemptId"), get(block, "taskId"), note_attempt, note_task)
            {
                Threshold::NoNote
            } else {
                note_anchor_threshold(block)
            };
            maximum = maximum.max(threshold);
            let found = match llm_round {
                None => maximum >= Threshold::EveryNote,
                Some(round) => maximum > Threshold::At(round),
            };
            if found {
                return index;
            }
        }
        self.blocks.len()
    }

    fn place(&mut self, note_segment: Obj, llm_round: Option<i128>) {
        let existing = match Self::key(&note_segment) {
            Some(key) => self.by_identity.get(&key).copied(),
            None => {
                let identity = execution_identity(&note_segment);
                self.order.iter().copied().find(|id| {
                    let note = &self.arena[*id].segment;
                    py_eq(get(note, "blockId"), get(&note_segment, "blockId")) && execution_identity(note) == identity
                })
            }
        };
        if let Some(id) = existing {
            self.arena[id].replace(note_segment);
            return;
        }
        let note_attempt = get(&note_segment, "attemptId").clone();
        let note_task = get(&note_segment, "taskId").clone();
        let anchor = self.first_anchoring_block(llm_round, &note_attempt, &note_task);
        for index in 0..self.order.len() {
            let note = &self.arena[self.order[index]];
            if note.gap > anchor {
                self.insert(index, anchor, note_segment);
                return;
            }
            if anchors_note(note.threshold, llm_round)
                && !outside_note_scope(&note.attempt_id, &note.task_id, &note_attempt, &note_task)
            {
                let gap = note.gap;
                self.insert(index, gap, note_segment);
                return;
            }
        }
        let index = self.order.len();
        self.insert(index, anchor, note_segment);
    }

    fn merged(self) -> Vec<Obj> {
        let mut merged = Vec::new();
        let mut start = 0;
        for id in &self.order {
            let note = &self.arena[*id];
            if note.gap > start {
                merged.extend(self.blocks[start..note.gap].iter().cloned());
            }
            merged.push(note.segment.clone());
            start = note.gap;
        }
        merged.extend(self.blocks[start.min(self.blocks.len())..].iter().cloned());
        merged
    }
}

/// `merge_injected_note_segments(segments, notes)` over task-shaped note entries.
fn merge_injected_note_segments(segments: Vec<Obj>, notes: &[Obj]) -> OpResult<Vec<Obj>> {
    if notes.is_empty() {
        return Ok(segments);
    }
    let mut placement: Option<NotePlacement> = None;
    let mut segments = Some(segments);
    for (position, entry) in notes.iter().enumerate() {
        let Value::String(kind) = get(entry, "kind") else { continue };
        let note_text = match entry.get("text") {
            Some(Value::String(text)) => text.clone(),
            _ => String::new(),
        };
        if py_strip(&note_text).is_empty() {
            continue;
        }
        let kind = if NOTE_KINDS.contains(&kind.as_str()) { kind.clone() } else { "system-intervention".to_owned() };
        let llm_round = get(entry, "llmRound");
        let llm_round = strict_int(llm_round).map(|round| (round, llm_round.clone()));
        let block_id = match get(entry, "blockId") {
            value if truthy(value) => value.clone(),
            _ => Value::String(format!("system-note:{kind}-{position}")),
        };
        let action =
            if value_in(get(entry, "action"), NOTE_ACTIONS)? { get(entry, "action").clone() } else { json!("prompt") };
        let mut note = Obj::new();
        note.insert("type".into(), json!("system_note"));
        note.insert("blockId".into(), block_id);
        note.insert("text".into(), Value::String(note_text));
        note.insert("noteKind".into(), Value::String(kind));
        note.insert("action".into(), action);
        if let Some(Value::String(reason)) = entry.get("reason") {
            note.insert("reason".into(), Value::String(reason.chars().take(NOTE_REASON_LIMIT).collect()));
        }
        note.insert("llmRound".into(), llm_round.as_ref().map_or(Value::Null, |(_, value)| value.clone()));
        for field in ["attemptId", "taskId"] {
            if let Some(Value::String(value)) = entry.get(field) {
                if !value.is_empty() {
                    note.insert(field.into(), Value::String(value.clone()));
                }
            }
        }
        let placement = placement.get_or_insert_with(|| NotePlacement::new(segments.take().unwrap_or_default()));
        placement.place(note, llm_round.map(|(round, _)| round));
    }
    Ok(match placement {
        Some(placement) => placement.merged(),
        None => segments.unwrap_or_default(),
    })
}

// ── turn_projection_segments.py ──────────────────────────────────────────

/// `_preserved_notes(value)`.
fn preserved_notes(value: &Value) -> OpResult<Vec<Obj>> {
    let Value::Array(items) = value else { return Ok(Vec::new()) };
    let mut notes = Vec::new();
    let mut seen = HashSet::new();
    for item in items {
        let Value::Object(item) = item else { continue };
        if !is_type(item, "system_note") || !get(item, "text").is_string() {
            continue;
        }
        let mut note = item.clone();
        if !value_in(get(&note, "noteKind"), NOTE_KINDS)? {
            note.insert("noteKind".into(), json!("system-intervention"));
        }
        let action = note.get("action").cloned().unwrap_or_else(|| json!("prompt"));
        if !value_in(&action, NOTE_ACTIONS)? {
            note.insert("action".into(), json!("prompt"));
        }
        if let Some(Value::String(identity)) = note.get("blockId") {
            if !identity.is_empty() {
                if seen.contains(identity) {
                    continue;
                }
                seen.insert(identity.clone());
            }
        }
        notes.push(note);
    }
    Ok(notes)
}

/// `_valid_segments(value)`.
fn valid_segments(value: &Value) -> OpResult<Option<Vec<Obj>>> {
    let Value::Array(items) = value else { return Ok(None) };
    let mut segments = Vec::with_capacity(items.len());
    for item in items {
        let Value::Object(item) = item else { return Ok(None) };
        if !value_in(get(item, "type"), SEGMENT_TYPES)? {
            return Ok(None);
        }
        let mut segment = item.clone();
        if is_type(&segment, "tool_use") {
            let Some(Value::Object(result)) = segment.get("result") else { return Ok(None) };
            if result.get("status").is_some_and(|status| !status.is_string()) {
                let result: Obj = result
                    .iter()
                    .filter(|(key, _)| key.as_str() != "status")
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();
                segment.insert("result".into(), Value::Object(result));
            }
        } else if !get(&segment, "text").is_string() {
            return Ok(None);
        }
        segments.push(segment);
    }
    Ok(Some(segments))
}

/// `_fallback_block_id(segment, position)`.
fn fallback_block_id(segment: &Obj, position: usize) -> String {
    let segment_type = text_or(get(segment, "type"), "text");
    if segment_type == "tool_use" {
        let call_id = stripped(get(segment, "id"));
        if !call_id.is_empty() {
            return format!("tool:{call_id}");
        }
    }
    if truthy(get(segment, "terminal")) {
        return format!("{segment_type}:terminal");
    }
    let llm_round = get(segment, "llmRound");
    if strict_int(llm_round).is_some() {
        let (attempt_id, task_id) = execution_identity(segment);
        let scope = if attempt_id.is_empty() { task_id } else { attempt_id };
        let prefix = if scope.is_empty() { String::new() } else { format!("attempt-{scope}:") };
        return format!("{segment_type}:{prefix}llm-{}", python_str(llm_round));
    }
    format!("{segment_type}:legacy-{position}")
}

/// `_stable_block_ids(segments, reserved_ids=)`.
fn stable_block_ids(segments: Vec<Obj>, reserved_ids: &HashSet<String>) -> Vec<Value> {
    let mut claimed: HashMap<String, usize> = reserved_ids.iter().map(|id| (id.clone(), 1)).collect();
    let mut normalized = Vec::with_capacity(segments.len());
    for (position, mut segment) in segments.into_iter().enumerate() {
        let preferred = match segment.get("blockId") {
            Some(Value::String(declared)) if !py_strip(declared).is_empty() => py_strip(declared).to_owned(),
            _ => fallback_block_id(&segment, position),
        };
        let occurrence = claimed.get(&preferred).copied().unwrap_or(0) + 1;
        claimed.insert(preferred.clone(), occurrence);
        let block_id = if occurrence == 1 { preferred } else { format!("{preferred}~{occurrence}") };
        segment.insert("blockId".into(), Value::String(block_id));
        segment.shift_remove("_round");
        normalized.push(Value::Object(segment));
    }
    normalized
}

fn tool_segment_call_ids(segments: &[Obj]) -> HashMap<String, usize> {
    let mut covered = HashMap::new();
    for segment in segments.iter().filter(|segment| is_type(segment, "tool_use")) {
        let call_id = stripped(get(segment, "id"));
        if !call_id.is_empty() {
            *covered.entry(call_id).or_insert(0) += 1;
        }
    }
    covered
}

/// `_segment_call_id(round_record)`.
fn segment_call_id(round: &Value) -> String {
    match round {
        Value::Object(round) if !is_synthetic_inbox_round(round) => stripped(get(round, "toolCallId")),
        _ => String::new(),
    }
}

/// `_segments_missing_tool_round(segments, tool_rounds)`.
fn segments_missing_tool_round(segments: &[Obj], tool_rounds: &Value) -> bool {
    let Value::Array(rounds) = tool_rounds else { return false };
    if rounds.is_empty() {
        return false;
    }
    let covered = tool_segment_call_ids(segments);
    let mut seen_rounds: HashMap<String, usize> = HashMap::new();
    for round in rounds {
        let call_id = segment_call_id(round);
        if call_id.is_empty() {
            continue;
        }
        let occurrence = seen_rounds.get(&call_id).copied().unwrap_or(0) + 1;
        seen_rounds.insert(call_id.clone(), occurrence);
        if occurrence > covered.get(&call_id).copied().unwrap_or(0) {
            return true;
        }
    }
    false
}

/// `_append_missing_live_tool_segments(segments, tool_rounds)`.
fn append_missing_live_tool_segments(mut segments: Vec<Obj>, tool_rounds: &Value) -> Vec<Obj> {
    let Value::Array(rounds) = tool_rounds else { return segments };
    let mut covered = tool_segment_call_ids(&segments);
    let mut seen_rounds: HashMap<String, usize> = HashMap::new();
    let mut appended = Vec::new();
    for (position, source) in rounds.iter().enumerate() {
        let Value::Object(round) = source else { continue };
        let call_id = segment_call_id(source);
        if call_id.is_empty() {
            continue;
        }
        let occurrence = seen_rounds.get(&call_id).copied().unwrap_or(0) + 1;
        seen_rounds.insert(call_id.clone(), occurrence);
        if occurrence <= covered.get(&call_id).copied().unwrap_or(0) {
            continue;
        }
        appended.push(tool_use_segment_from_round(round, position));
        *covered.entry(call_id).or_insert(0) += 1;
    }
    segments.extend(segments_to_json(appended));
    segments
}

/// `_normalize_injection_lane(records, channel)`.
fn normalize_injection_lane(records: &Value, channel: &str) -> Value {
    let Value::Array(items) = records else { return records.clone() };
    let mut claimed: HashMap<String, usize> = HashMap::new();
    let mut normalized = Vec::with_capacity(items.len());
    for item in items {
        let Value::Object(item) = item else {
            normalized.push(item.clone());
            continue;
        };
        let mut record = item.clone();
        let round_value = get(&record, "round");
        let round_token = match strict_int(round_value) {
            Some(_) => python_str(round_value),
            None => "unknown".to_owned(),
        };
        let preferred = match get(&record, "blockId") {
            Value::String(declared) if !py_strip(declared).is_empty() => py_strip(declared).to_owned(),
            _ => format!("injection:{channel}:round-{round_token}"),
        };
        let occurrence = claimed.get(&preferred).copied().unwrap_or(0) + 1;
        claimed.insert(preferred.clone(), occurrence);
        let block_id = if occurrence == 1 { preferred } else { format!("{preferred}~{occurrence}") };
        record.insert("blockId".into(), Value::String(block_id));
        normalized.push(Value::Object(record));
    }
    Value::Array(normalized)
}

/// `_normalize_sidecar_blocks(projection)`.
fn normalize_sidecar_blocks(projection: &mut Obj) -> OpResult<HashSet<String>> {
    let mut reserved = HashSet::new();
    for (field, fallback) in [
        ("origin", "origin"),
        ("contextSnapshot", "turn-context"),
        ("compaction", "compaction"),
        ("imageGeneration", "image-generation"),
        ("proposedPlan", "proposed-plan"),
        ("planExecution", "plan-execution"),
        ("activityTimeline", "activity-timeline"),
    ] {
        let Some(Value::Object(sidecar)) = projection.get(field) else { continue };
        let mut sidecar = sidecar.clone();
        let block_id = text_or(get(&sidecar, "blockId"), fallback);
        sidecar.insert("blockId".into(), Value::String(block_id.clone()));
        projection.insert(field.into(), Value::Object(sidecar));
        reserved.insert(block_id);
    }
    if ["images", "attachments", "videos", "pdfTexts", "convRefs", "replyQuotes"]
        .iter()
        .any(|field| truthy(get(projection, field)))
    {
        reserved.insert("attachments".to_owned());
    }
    if let Some(Value::Object(provenance)) = projection.get("provenance") {
        let mut provenance = provenance.clone();
        let block_id = text_or(get(&provenance, "blockId"), "provenance");
        provenance.insert("blockId".into(), Value::String(block_id.clone()));
        projection.insert("provenance".into(), Value::Object(provenance));
        reserved.insert(block_id);
    }
    for (field, channel) in [
        ("_inboxInjects", "inbox"),
        ("_peerInjects", "peer"),
        ("_userSteerInjects", "user-steer"),
        ("_bgCommandInjects", "background-command"),
        ("_stallNudges", "stall-nudge"),
    ] {
        if get(projection, field).is_null() {
            continue;
        }
        let records = normalize_injection_lane(&projection[field], channel);
        projection.insert(field.into(), records.clone());
        match &records {
            Value::Array(items) => {
                for item in items {
                    if let Value::Object(item) = item {
                        if truthy(get(item, "blockId")) {
                            reserved.insert(python_str(&item["blockId"]));
                        }
                    }
                }
            }
            // Iterating a str or dict yields no mappings; a scalar is not iterable.
            Value::String(_) | Value::Object(_) => {}
            _ => return internal("object is not iterable"),
        }
    }
    let mut file_changes = match (projection.get("fileChanges"), projection.get("modifiedFileList")) {
        (Some(Value::Object(changes)), _) => Some(changes.clone()),
        (_, Some(Value::Array(files))) if !files.is_empty() => {
            let raw_count = get(projection, "modifiedFiles");
            let (count, count_value) = match raw_count {
                Value::Bool(flag) => (*flag as i128, raw_count.clone()),
                number => match strict_int(number) {
                    Some(count) => (count, number.clone()),
                    None => (0, json!(0)),
                },
            };
            // `max(count, len(files))` keeps the first argument on ties.
            let count = if count >= files.len() as i128 { count_value } else { json!(files.len()) };
            let files: Vec<Value> = files.to_vec();
            let mut changes = Obj::new();
            changes.insert("blockId".into(), json!("file-changes"));
            changes.insert("count".into(), count);
            changes.insert("state".into(), json!("applied"));
            changes.insert("files".into(), Value::Array(files));
            Some(changes)
        }
        _ => None,
    };
    if let Some(Value::Array(rolled_back)) = projection.get("rolledBack") {
        let mut claimed: HashMap<String, usize> = HashMap::new();
        let mut normalized = Vec::new();
        for item in rolled_back {
            let Value::Object(item) = item else { continue };
            let mut record = item.clone();
            let preferred = match get(&record, "blockId") {
                Value::String(declared) if !py_strip(declared).is_empty() => py_strip(declared).to_owned(),
                _ => "rolled-back".to_owned(),
            };
            let occurrence = claimed.get(&preferred).copied().unwrap_or(0) + 1;
            claimed.insert(preferred.clone(), occurrence);
            let block_id = if occurrence == 1 { preferred } else { format!("{preferred}~{occurrence}") };
            record.insert("blockId".into(), Value::String(block_id.clone()));
            normalized.push(Value::Object(record));
            reserved.insert(block_id);
        }
        if !normalized.is_empty() {
            projection.insert("rolledBack".into(), Value::Array(normalized));
        }
    }
    if let Some(changes) = file_changes.as_mut() {
        let block_id = text_or(get(changes, "blockId"), "file-changes");
        changes.insert("blockId".into(), Value::String(block_id.clone()));
        if !changes.contains_key("state") {
            changes.insert("state".into(), json!("applied"));
        }
        projection.insert("fileChanges".into(), Value::Object(changes.clone()));
        reserved.insert(block_id);
    }
    Ok(reserved)
}

/// `_live_content_already_narrated(segments, content, status)`.
fn live_content_already_narrated(segments: &[Obj], content: &str, status: &str) -> bool {
    if status != "running" || py_strip(content).is_empty() {
        return false;
    }
    for segment in segments.iter().rev() {
        if is_type(segment, "text") && !truthy(get(segment, "terminal")) && !truthy(get(segment, "deliverable")) {
            return stripped(get(segment, "text")) == py_strip(content);
        }
    }
    false
}

/// `_drop_narrated_live_terminal_mirror(segments, projection, status)`.
fn drop_narrated_live_terminal_mirror(segments: Vec<Obj>, projection: &Obj, status: &str) -> Vec<Obj> {
    let content = text(get(projection, "content"));
    if !live_content_already_narrated(&segments, &content, status) {
        return segments;
    }
    segments
        .into_iter()
        .filter(|segment| {
            !(is_type(segment, "text") && (truthy(get(segment, "terminal")) || truthy(get(segment, "deliverable"))))
        })
        .collect()
}

/// `_synchronize_terminal_blocks(segments, projection, actor=, status=, ensure_assistant_text_placeholder=)`.
fn synchronize_terminal_blocks(segments: &mut Vec<Obj>, projection: &Obj, actor: &str, status: &str, ensure: bool) {
    let content = text(get(projection, "content"));
    let thinking = text(get(projection, "thinking"));
    let mut terminal_text_index = segments.iter().rposition(|segment| {
        is_type(segment, "text") && (truthy(get(segment, "terminal")) || truthy(get(segment, "deliverable")))
    });
    if terminal_text_index.is_none()
        && (!content.is_empty() || (ensure && actor != "human"))
        && !live_content_already_narrated(segments, &content, status)
    {
        segments.push(terminal_segment("text", &content, true, false));
        terminal_text_index = Some(segments.len() - 1);
    }
    if let Some(index) = terminal_text_index {
        let terminal_text = &mut segments[index];
        terminal_text.insert("text".into(), Value::String(content));
        terminal_text.insert("deliverable".into(), Value::Bool(true));
        terminal_text.insert("terminal".into(), Value::Bool(true));
        if RESUMABLE_TURN_STATUSES.contains(&status) {
            terminal_text.insert("resumable".into(), Value::Bool(true));
        } else {
            terminal_text.shift_remove("resumable");
        }
    }
    let terminal_thinking_index =
        segments.iter().rposition(|segment| is_type(segment, "thinking") && truthy(get(segment, "terminal")));
    if let Some(index) = terminal_thinking_index {
        segments[index].insert("text".into(), Value::String(thinking));
    } else if !thinking.is_empty() {
        let thinking_segment = terminal_segment("thinking", &thinking, false, false);
        match terminal_text_index {
            None => segments.push(thinking_segment),
            Some(index) => segments.insert(index, thinking_segment),
        }
    }
}

fn round_has_explicit_payload_compaction(round: &Obj) -> bool {
    get(round, "compactionLayer") == &json!("L1") || get(round, "_persistCompacted") == &Value::Bool(true)
}

/// `_tool_segment_matches_round(segment, round_record)`.
fn tool_segment_matches_round(segment: &Obj, round: &Obj) -> bool {
    let segment_id = stripped(get(segment, "id"));
    let round_id = stripped(get(round, "toolCallId"));
    if segment_id.is_empty() || segment_id != round_id {
        return false;
    }
    let segment_name = stripped(get(segment, "name"));
    let round_name = stripped(get(round, "toolName"));
    if !segment_name.is_empty() && !round_name.is_empty() && segment_name != round_name {
        return false;
    }
    if segment.contains_key("input") != round.contains_key("toolArgs")
        || (segment.contains_key("input") && !py_eq(get(segment, "input"), get(round, "toolArgs")))
    {
        return false;
    }
    let (segment_attempt, segment_task) = execution_identity(segment);
    let (round_attempt, round_task) = execution_identity(round);
    if !segment_attempt.is_empty() && !round_attempt.is_empty() && segment_attempt != round_attempt {
        return false;
    }
    if !segment_task.is_empty() && !round_task.is_empty() && segment_task != round_task {
        return false;
    }
    let segment_scope = if segment_attempt.is_empty() { &segment_task } else { &segment_attempt };
    let round_scope = if round_attempt.is_empty() { &round_task } else { &round_attempt };
    if !segment_scope.is_empty() && !round_scope.is_empty() && segment_scope != round_scope {
        return false;
    }
    match (execution_llm_round(segment), execution_llm_round(round)) {
        (Some(left), Some(right)) => py_eq(&left, &right),
        _ => true,
    }
}

/// `_segment_result_from_round(round_record)`.
fn segment_result_from_round(round: &Obj) -> Obj {
    let source = round.get("result").and_then(Value::as_object);
    let mut result = source.cloned().unwrap_or_default();
    let explicitly_compacted = round_has_explicit_payload_compaction(round);
    let has_content = source.is_some_and(|source| source.contains_key("content"));
    let has_status = source.is_some_and(|source| source.contains_key("status"));
    if (explicitly_compacted || !has_content) && round.contains_key("toolContent") {
        result.insert("content".into(), round["toolContent"].clone());
    }
    if !get(round, "toolModelContent").is_null() && !explicitly_compacted {
        result.insert("modelContent".into(), round["toolModelContent"].clone());
    } else if explicitly_compacted {
        result.shift_remove("modelContent");
    }
    if (explicitly_compacted || !has_status) && round.contains_key("status") {
        result.insert("status".into(), round["status"].clone());
    }
    result
}

/// `_synchronize_compacted_tool_segment_results(segments, tool_rounds)`.
fn synchronize_compacted_tool_segment_results(segments: Vec<Obj>, tool_rounds: &Value) -> Vec<Obj> {
    let Value::Array(rounds) = tool_rounds else { return segments };
    if !rounds.iter().any(|round| round.as_object().is_some_and(round_has_explicit_payload_compaction)) {
        return segments;
    }
    let mut rounds_by_id: HashMap<String, &Obj> = HashMap::new();
    let mut duplicates = HashSet::new();
    for round in rounds.iter().filter_map(Value::as_object) {
        let call_id = stripped(get(round, "toolCallId"));
        if call_id.is_empty() {
            continue;
        }
        if rounds_by_id.contains_key(&call_id) {
            duplicates.insert(call_id);
            continue;
        }
        rounds_by_id.insert(call_id, round);
    }
    for call_id in &duplicates {
        rounds_by_id.remove(call_id);
    }
    let segment_id_counts = tool_segment_call_ids(&segments);
    if rounds_by_id.is_empty() || segment_id_counts.is_empty() {
        return segments;
    }
    let mut changed = false;
    let mut synchronized = Vec::with_capacity(segments.len());
    for source in &segments {
        let call_id = if is_type(source, "tool_use") { stripped(get(source, "id")) } else { String::new() };
        let round = rounds_by_id.get(&call_id).copied();
        let Some(round) = round.filter(|round| {
            !call_id.is_empty()
                && segment_id_counts.get(&call_id) == Some(&1)
                && round_has_explicit_payload_compaction(round)
                && tool_segment_matches_round(source, round)
        }) else {
            synchronized.push(source.clone());
            continue;
        };
        let round_result = segment_result_from_round(round);
        let Some(content) = round_result.get("content") else {
            synchronized.push(source.clone());
            continue;
        };
        let source_result = get(source, "result");
        let mut result = source_result.as_object().cloned().unwrap_or_default();
        result.insert("content".into(), content.clone());
        if let Some(status) = round_result.get("status") {
            result.insert("status".into(), status.clone());
        }
        let result = Value::Object(result);
        if py_eq(&result, source_result) {
            synchronized.push(source.clone());
            continue;
        }
        let mut segment = source.clone();
        segment.insert("result".into(), result);
        synchronized.push(segment);
        changed = true;
    }
    if changed {
        synchronized
    } else {
        segments
    }
}

/// `projection_with_stable_segments(raw_projection, actor=, status=)`.
pub fn projection_with_stable_segments(raw_projection: &Value, actor: &str, status: &str) -> OpResult<Obj> {
    let mut projection = normalize_projection_document(raw_projection)?;
    let reserved_ids = normalize_sidecar_blocks(&mut projection)?;
    let actor = if actor.is_empty() { "assistant" } else { actor };
    let raw_segments = get(&projection, "segments").clone();
    let mut segments = valid_segments(&raw_segments)?;
    let preserved = preserved_notes(&raw_segments)?;
    if let Some(valid) = segments.take() {
        let notes_by_id: HashMap<&str, &Obj> = preserved
            .iter()
            .filter_map(|note| match note.get("blockId") {
                Some(Value::String(id)) if !id.is_empty() => Some((id.as_str(), note)),
                _ => None,
            })
            .collect();
        let mut seen_notes: HashSet<Option<String>> = HashSet::new();
        let mut normalized = Vec::with_capacity(valid.len());
        for item in valid {
            if !is_type(&item, "system_note") {
                normalized.push(item);
                continue;
            }
            let identity = match item.get("blockId") {
                Some(Value::String(id)) if !id.is_empty() => Some(id.clone()),
                _ => None,
            };
            if identity.is_some() && seen_notes.contains(&identity) {
                continue;
            }
            seen_notes.insert(identity.clone());
            match identity.as_deref().and_then(|id| notes_by_id.get(id)) {
                Some(note) => normalized.push((*note).clone()),
                None => normalized.push(item),
            }
        }
        segments = Some(normalized);
    }
    let tool_rounds = get(&projection, "toolRounds").clone();
    if let Some(mut valid) = segments.take() {
        if segments_missing_tool_round(&valid, &tool_rounds) {
            if status == "running" {
                synchronize_terminal_blocks(&mut valid, &projection, actor, status, false);
                segments = Some(append_missing_live_tool_segments(valid, &tool_rounds));
            }
        } else {
            segments = Some(valid);
        }
    }
    let mut segments = match segments {
        Some(segments) => segments,
        None => {
            let content = text(get(&projection, "content"));
            let thinking = text(get(&projection, "thinking"));
            let rounds = match &tool_rounds {
                Value::Array(rounds) => rounds.clone(),
                _ => Vec::new(),
            };
            let assembled = segments_to_json(assemble_segments(&content, &thinking, &rounds)?);
            let entries: Vec<Obj> = preserved
                .iter()
                .map(|note| {
                    let mut entry = note.clone();
                    let kind = get(note, "noteKind");
                    let kind = if truthy(kind) { kind.clone() } else { json!("system-intervention") };
                    entry.insert("kind".into(), kind);
                    entry
                })
                .collect();
            let merged = merge_injected_note_segments(assembled, &entries)?;
            // `{note.get('blockId'): note ...}`: the dict build hashes every truthy id.
            let keyed: Vec<&Obj> = preserved.iter().filter(|note| truthy(get(note, "blockId"))).collect();
            if keyed.iter().any(|note| matches!(get(note, "blockId"), Value::Array(_) | Value::Object(_))) {
                return internal("unhashable type");
            }
            merged
                .into_iter()
                .map(|segment| {
                    if !is_type(&segment, "system_note") {
                        return segment;
                    }
                    let block_id = get(&segment, "blockId");
                    match keyed.iter().rev().find(|note| py_eq(get(note, "blockId"), block_id)) {
                        Some(note) => (*note).clone(),
                        None => segment,
                    }
                })
                .collect()
        }
    };
    synchronize_terminal_blocks(&mut segments, &projection, actor, status, true);
    let segments = synchronize_compacted_tool_segment_results(segments, &tool_rounds);
    let segments = drop_narrated_live_terminal_mirror(segments, &projection, status);
    projection.insert("segments".into(), Value::Array(stable_block_ids(segments, &reserved_ids)));
    Ok(projection)
}
