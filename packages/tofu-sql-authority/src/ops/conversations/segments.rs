//! `lib/turn_projection_segments.py::projection_with_stable_segments` and the
//! pure owners it calls (`lib/tasks_pkg/segments/_assemble.py`,
//! `_serde.py::segments_to_json`, `_types.py`,
//! `lib/tool_results/tool_round_identity.py`,
//! `tool_round_replay.py::is_superseded_provider_attempt_round`), ported for
//! `conversation.clone`, which freezes a live source turn through it.
//!
//! `injected_notes` is always `None` at that call site, so the final
//! `merge_injected_note_segments(segments, None)` is the identity and is not
//! a parameter here. `normalize_projection_document` is a private copy of
//! `ops/turns/normalize.rs` (the `turns` domain is owned and built separately).

use std::collections::{HashMap, HashSet};

use serde_json::{json, Map, Value};

use crate::codec::py_eq;
use crate::error::{internal, OpResult};
use crate::json::{python_str, truthy};
use crate::payload::get;

use self::normalize::{normalize_projection_document, py_strip};

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
fn execution_llm_round(value: &Obj) -> Option<Value> {
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
fn execution_identity(value: &Obj) -> (String, String) {
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

// Private copy of `ops/turns/normalize.rs` (`normalize_projection_document`):
// the `turns` domain is owned and built separately from `conversations`.
mod normalize {
    //! Private port of `lib/turn_projection_patch.py::normalize_projection_document`
    //! and the pure normalizers it calls (`background_command_projection.py`,
    //! `plan_contract.py`, `model_routing/route_snapshot.py::legacy_route_snapshot`,
    //! `turn_activity_timeline.py::normalize_activity_timeline`).

    use regex::Regex;
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};

    use super::{text, text_or, value_in, Obj};
    use crate::error::OpResult;
    use crate::json::{python_float_repr, python_str, truthy};
    use crate::payload::get;

    /// Python `json.dumps(value, ensure_ascii=False, separators=(',', ':'))`.
    pub fn py_json_dumps(value: &Value, sort_keys: bool) -> String {
        fn write(out: &mut String, value: &Value, sort_keys: bool) {
            match value {
                Value::Null => out.push_str("null"),
                Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
                Value::Number(number) => {
                    if let Some(integer) = number.as_i64() {
                        out.push_str(&integer.to_string());
                    } else if let Some(integer) = number.as_u64() {
                        out.push_str(&integer.to_string());
                    } else {
                        out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0)));
                    }
                }
                Value::String(text) => out.push_str(&serde_json::to_string(text).unwrap_or_default()),
                Value::Array(items) => {
                    out.push('[');
                    for (index, item) in items.iter().enumerate() {
                        if index > 0 {
                            out.push(',');
                        }
                        write(out, item, sort_keys);
                    }
                    out.push(']');
                }
                Value::Object(map) => {
                    out.push('{');
                    let mut entries: Vec<(&String, &Value)> = map.iter().collect();
                    if sort_keys {
                        entries.sort_by(|a, b| a.0.cmp(b.0));
                    }
                    for (index, (key, item)) in entries.into_iter().enumerate() {
                        if index > 0 {
                            out.push(',');
                        }
                        out.push_str(&serde_json::to_string(key).unwrap_or_default());
                        out.push(':');
                        write(out, item, sort_keys);
                    }
                    out.push('}');
                }
            }
        }
        let mut out = String::new();
        write(&mut out, value, sort_keys);
        out
    }

    /// Python `str.isspace` for one character.
    pub fn py_isspace(character: char) -> bool {
        character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
    }

    /// Python `str.strip()`.
    pub fn py_strip(value: &str) -> &str {
        value.trim_matches(py_isspace)
    }

    fn chars_prefix(value: &str, limit: usize) -> String {
        value.chars().take(limit).collect()
    }

    fn copy_named(value: &Value, fields: &[&str]) -> Obj {
        match value {
            Value::Object(map) => map
                .iter()
                .filter(|(key, _)| fields.contains(&key.as_str()))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
            _ => Obj::new(),
        }
    }

    /// `_positive_int`.
    fn positive_int(value: &Value) -> Option<i64> {
        match value {
            Value::Number(number) => match number.as_i64() {
                Some(integer) => Some(integer.max(0)),
                None => number.as_f64().map(|float| (float.trunc() as i64).max(0)),
            },
            _ => None,
        }
    }

    const VALID_INITIATORS: &[&str] =
        &["autopilot", "brain", "channel", "human", "operator", "peer", "proactive", "swarm", "timer"];
    const CHANNEL_ORIGIN_FIELDS: &[&str] = &["channel", "thread", "eventIds", "title"];
    const BRAIN_FIELDS: &[&str] =
        &["epicId", "epicTitle", "originatorConv", "originatorTitle", "route", "method", "answered"];
    const TRANSLATION_FIELDS: &[&str] = &[
        "status",
        "model",
        "error",
        "taskId",
        "statusMessage",
        "statusKind",
        "partial",
        "partialByRound",
        "sendFailure",
        "skippedReason",
    ];
    const ORIGIN_FIELDS: &[&str] = &[
        "blockId",
        "initiator",
        "sourceConversationId",
        "peerHuman",
        "brain",
        "autopilotRunId",
        "boardTaskId",
        "scheduledTaskId",
        "channel",
        "forwardedFrom",
    ];
    const FORWARDED_FROM_FIELDS: &[&str] = &["conversationId", "turnId", "steerId", "instructionId"];
    const COMPACTION_FIELDS: &[&str] = &[
        "blockId",
        "archiveId",
        "conversationId",
        "trigger",
        "timestamp",
        "tokensBefore",
        "tokensAfter",
        "messagesBefore",
        "messagesAfter",
        "reductionPercent",
        "foldedToolRounds",
        "estimatedPromptTokens",
    ];
    const IMAGE_GENERATION_FIELDS: &[&str] = &["blockId", "mode", "status", "results", "error"];
    const IMAGE_RESULT_FIELDS: &[&str] = &[
        "ok",
        "prompt",
        "model",
        "providerId",
        "aspectRatio",
        "resolution",
        "imageUrl",
        "remoteImageUrl",
        "fileSize",
        "elapsedSeconds",
        "responseText",
        "error",
        "errorType",
    ];
    const ROLLED_BACK_FIELDS: &[&str] = &["blockId", "attemptId", "at", "content", "thinking"];
    const PROJECTION_IDENTITY_KEYS: &[&str] = &[
        "turnId",
        "attemptId",
        "_turnId",
        "_attemptId",
        "_msgId",
        "_taskId",
        "activeTaskId",
        "role",
        "_turnActor",
        "_turnKind",
        "_turnLaneId",
        "_turnStatus",
        "_turnSettlement",
        "_projectionRevision",
        "_commandPending",
        "branches",
    ];
    pub const PUBLIC_PROJECTION_FIELDS: &[&str] = &[
        "content",
        "thinking",
        "toolRounds",
        "segments",
        "usage",
        "usageComplete",
        "apiRounds",
        "cost",
        "lastRoundUsage",
        "model",
        "preset",
        "providerId",
        "routeSnapshot",
        "thinkingDepth",
        "modifiedFiles",
        "modifiedFileList",
        "modifiedFilesTruncated",
        "fileChanges",
        "todoState",
        "waitingOn",
        "fallbackModel",
        "fallbackFrom",
        "fallbackReason",
        "fallbackKind",
        "translatedContent",
        "originalContent",
        "translation",
        "timestamp",
        "images",
        "attachments",
        "videos",
        "pdfTexts",
        "convRefs",
        "replyQuotes",
        "_branchLanes",
        "orchestration",
        "provenance",
        "_inboxInjects",
        "_peerInjects",
        "_userSteerInjects",
        "_bgCommandInjects",
        "backgroundCommand",
        "_stallNudges",
        "origin",
        "contextSnapshot",
        "compaction",
        "imageGeneration",
        "proposedPlan",
        "planExecution",
        "usageRecovery",
        "activityTimeline",
        "timingTrace",
        "rolledBack",
    ];

    fn legacy_initiator(projection: &Obj) -> OpResult<String> {
        let declared = get(projection, "_initiator");
        if value_in(declared, VALID_INITIATORS)? {
            return Ok(python_str(declared));
        }
        let flag = |key: &str| truthy(get(projection, key));
        Ok(if flag("_peerMessage") {
            if flag("_peerHuman") {
                "operator"
            } else {
                "peer"
            }
        } else if flag("_isVirtualUser") || flag("_autopilotRunId") {
            "autopilot"
        } else if flag("_proactive") {
            "proactive"
        } else if flag("_timer") {
            "timer"
        } else if flag("_brainDispatch") {
            "brain"
        } else if flag("_swarmAutoContinue") {
            "swarm"
        } else {
            "human"
        }
        .to_owned())
    }

    fn normalize_origin(projection: &Obj) -> OpResult<Option<Obj>> {
        let mut origin = copy_named(get(projection, "origin"), ORIGIN_FIELDS);
        let mut brain = copy_named(get(&origin, "brain"), BRAIN_FIELDS);
        let legacy_brain = copy_named(get(projection, "_brainEpic"), BRAIN_FIELDS);
        for (key, value) in legacy_brain {
            brain.insert(key, value);
        }
        if !brain.is_empty() {
            origin.insert("brain".into(), Value::Object(brain));
        } else {
            origin.shift_remove("brain");
        }
        let channel = copy_named(get(&origin, "channel"), CHANNEL_ORIGIN_FIELDS);
        if truthy(get(&channel, "channel")) && truthy(get(&channel, "thread")) {
            let normalized: Obj = channel
                .iter()
                .map(|(key, value)| {
                    let item = if key == "eventIds" {
                        match value {
                            Value::Array(items) => {
                                Value::Array(items.iter().map(|i| Value::String(python_str(i))).collect())
                            }
                            _ => Value::Array(vec![]),
                        }
                    } else {
                        Value::String(python_str(value))
                    };
                    (key.clone(), item)
                })
                .collect();
            origin.insert("channel".into(), Value::Object(normalized));
        } else {
            origin.shift_remove("channel");
        }
        let forwarded = copy_named(&origin.shift_remove("forwardedFrom").unwrap_or(Value::Null), FORWARDED_FROM_FIELDS);
        if forwarded.values().all(|value| matches!(value, Value::String(text) if !text.is_empty()))
            && forwarded.contains_key("conversationId")
            && forwarded.contains_key("turnId")
        {
            origin.insert("forwardedFrom".into(), Value::Object(forwarded));
        }
        let declared = get(&origin, "initiator").clone();
        let initiator =
            if value_in(&declared, VALID_INITIATORS)? { python_str(&declared) } else { legacy_initiator(projection)? };
        let has_legacy_origin = [
            "_initiator",
            "_peerMessage",
            "_peerHuman",
            "_fromConv",
            "_brainDispatch",
            "_brainEpic",
            "_isVirtualUser",
            "_autopilotRunId",
            "_proactive",
            "_proactiveTaskId",
            "_timer",
            "_swarmAutoContinue",
            "_boardTaskId",
            "boardTaskId",
        ]
        .iter()
        .any(|key| !get(projection, key).is_null());
        if initiator == "human" && origin.is_empty() && !has_legacy_origin {
            return Ok(None);
        }
        let block = text_or(get(&origin, "blockId"), "origin");
        origin.insert("blockId".into(), Value::String(block));
        origin.insert("initiator".into(), Value::String(initiator));
        let source = if truthy(get(&origin, "sourceConversationId")) {
            get(&origin, "sourceConversationId").clone()
        } else {
            get(projection, "_fromConv").clone()
        };
        if truthy(&source) {
            origin.insert("sourceConversationId".into(), Value::String(python_str(&source)));
        }
        if !get(projection, "_peerHuman").is_null() {
            origin.insert("peerHuman".into(), Value::Bool(truthy(get(projection, "_peerHuman"))));
        }
        for (target, legacy) in [
            ("autopilotRunId", "_autopilotRunId"),
            ("boardTaskId", "_boardTaskId"),
            ("scheduledTaskId", "_proactiveTaskId"),
        ] {
            let mut candidates = vec![get(&origin, target).clone(), get(projection, legacy).clone()];
            if target == "boardTaskId" {
                candidates.push(get(projection, "boardTaskId").clone());
            }
            if let Some(value) = candidates.into_iter().find(truthy) {
                origin.insert(target.into(), Value::String(python_str(&value)));
            }
        }
        Ok(Some(origin))
    }

    fn normalize_translation(projection: &Obj) -> OpResult<Option<Obj>> {
        let mut translation = copy_named(get(projection, "translation"), TRANSLATION_FIELDS);
        for (key, legacy) in [
            ("model", "_translateModel"),
            ("error", "_translateError"),
            ("taskId", "_translateTaskId"),
            ("statusMessage", "_translateStatus"),
            ("statusKind", "_translateStatusKind"),
            ("partial", "_translatePartial"),
            ("partialByRound", "_translatePartialByRound"),
            ("sendFailure", "_translateFailed"),
            ("skippedReason", "_translateSkippedReason"),
        ] {
            let value = get(projection, legacy);
            if !value.is_null() {
                translation.insert(key.into(), value.clone());
            }
        }
        let done_present = projection.contains_key("_translateDone");
        let done = get(projection, "_translateDone");
        if !get(&translation, "error").is_null() {
            translation.insert("status".into(), json!("failed"));
        } else if truthy(get(&translation, "skippedReason")) {
            translation.insert("status".into(), json!("skipped"));
        } else if done == &Value::Bool(true) {
            translation.insert("status".into(), json!("completed"));
        } else if done == &Value::Bool(false)
            || truthy(get(&translation, "taskId"))
            || truthy(get(&translation, "partial"))
        {
            translation.insert("status".into(), json!("pending"));
        } else if (!get(projection, "translatedContent").is_null() || !get(projection, "originalContent").is_null())
            && !translation.contains_key("status")
        {
            translation.insert("status".into(), json!("completed"));
        }
        if translation.is_empty() && !done_present {
            return Ok(None);
        }
        if !value_in(get(&translation, "status"), &["pending", "completed", "skipped", "failed"])? {
            translation.shift_remove("status");
        }
        Ok(if translation.is_empty() { None } else { Some(translation) })
    }

    fn normalize_context(projection: &Obj) -> Option<Value> {
        if let Some(Value::Object(current)) = projection.get("contextSnapshot") {
            if let Some(snapshot @ Value::Object(_)) = current.get("snapshot") {
                return Some(
                    json!({"blockId": text_or(get(current, "blockId"), "turn-context"), "snapshot": snapshot}),
                );
            }
        }
        if let Some(legacy @ Value::Object(_)) = projection.get("_ctx") {
            return Some(json!({"blockId": "turn-context", "snapshot": legacy}));
        }
        None
    }

    fn normalize_compaction(projection: &Obj) -> Option<Obj> {
        let mut compaction = copy_named(get(projection, "compaction"), COMPACTION_FIELDS);
        let marker = match get(projection, "_compactions") {
            Value::Array(markers) if !markers.is_empty() => markers[0].as_object().cloned().unwrap_or_default(),
            _ => Obj::new(),
        };
        let is_legacy = get(projection, "_isCompactionSummary") == &Value::Bool(true);
        if compaction.is_empty() && !is_legacy {
            return None;
        }
        let block = text_or(get(&compaction, "blockId"), "compaction");
        compaction.insert("blockId".into(), Value::String(block));
        let or = |left: &Value, right: &Value| if truthy(left) { left.clone() } else { right.clone() };
        let text_fields = [
            ("archiveId", or(get(projection, "_compactionArchiveId"), get(&marker, "archiveId"))),
            ("conversationId", get(&marker, "convId").clone()),
            ("trigger", get(&marker, "trigger").clone()),
        ];
        for (key, value) in text_fields {
            if !value.is_null() && value != Value::String(String::new()) {
                compaction.insert(key.into(), Value::String(python_str(&value)));
            }
        }
        let number_fields = [
            ("timestamp", get(&marker, "ts").clone()),
            ("tokensBefore", get(&marker, "tokensBefore").clone()),
            ("tokensAfter", get(&marker, "tokensAfter").clone()),
            ("messagesBefore", get(&marker, "msgsBefore").clone()),
            ("messagesAfter", get(&marker, "msgsAfter").clone()),
            ("reductionPercent", get(&marker, "reductionPct").clone()),
            ("foldedToolRounds", or(get(&marker, "foldedToolRounds"), get(projection, "_foldedToolRounds"))),
            ("estimatedPromptTokens", get(projection, "_estimatedPromptTokens").clone()),
        ];
        for (key, value) in number_fields {
            if let Some(normalized) = positive_int(&value) {
                compaction.insert(key.into(), Value::from(normalized));
            }
        }
        Some(compaction)
    }

    /// Python `float(value)` for a decoded JSON value.
    fn py_float(value: &Value) -> Option<f64> {
        match value {
            Value::Bool(flag) => Some(*flag as i64 as f64),
            Value::Number(number) => number.as_f64(),
            Value::String(text) => {
                let cleaned = py_strip(text).replace('_', "");
                match cleaned.to_ascii_lowercase().as_str() {
                    "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
                    "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
                    "nan" | "+nan" | "-nan" => Some(f64::NAN),
                    other => other.parse::<f64>().ok(),
                }
            }
            _ => None,
        }
    }

    fn normalize_image_result(value: &Value) -> Option<Obj> {
        let Value::Object(source) = value else { return None };
        let mut result = copy_named(value, IMAGE_RESULT_FIELDS);
        for (target, legacy) in [
            ("providerId", "provider_id"),
            ("aspectRatio", "aspect_ratio"),
            ("imageUrl", "image_url"),
            ("remoteImageUrl", "remote_image_url"),
            ("fileSize", "file_size"),
            ("elapsedSeconds", "elapsed"),
            ("responseText", "response_text"),
            ("errorType", "error_type"),
        ] {
            if !result.contains_key(target) && !get(source, legacy).is_null() {
                result.insert(target.into(), get(source, legacy).clone());
            }
        }
        let ok = match result.get("ok") {
            Some(value) => truthy(value),
            None => truthy(get(source, "image_url")) || truthy(get(source, "imageUrl")),
        };
        result.insert("ok".into(), Value::Bool(ok));
        let prompt = text(get(&result, "prompt"));
        result.insert("prompt".into(), Value::String(prompt));
        let model = text(get(&result, "model"));
        result.insert("model".into(), Value::String(model));
        if !get(&result, "elapsedSeconds").is_null() {
            match py_float(get(&result, "elapsedSeconds")) {
                Some(seconds) => {
                    let bounded = if seconds > 0.0 { seconds } else { 0.0 };
                    result.insert("elapsedSeconds".into(), crate::json::float_value(bounded));
                }
                None => {
                    result.shift_remove("elapsedSeconds");
                }
            }
        }
        if !get(&result, "fileSize").is_null() {
            match positive_int(get(&result, "fileSize")) {
                Some(size) => {
                    result.insert("fileSize".into(), Value::from(size));
                }
                None => {
                    result.shift_remove("fileSize");
                }
            }
        }
        Some(result)
    }

    fn normalize_image_generation(projection: &Obj) -> OpResult<Option<Value>> {
        let current = copy_named(get(projection, "imageGeneration"), IMAGE_GENERATION_FIELDS);
        let legacy_single = get(projection, "_igResult");
        let legacy_batch = get(projection, "_igResults");
        let raw_results: Vec<Value> = match get(&current, "results") {
            Value::Array(items) => items.clone(),
            _ => match (legacy_batch, legacy_single) {
                (Value::Array(items), _) => items.clone(),
                (_, single @ Value::Object(_)) => vec![single.clone()],
                _ => vec![],
            },
        };
        let results: Vec<Obj> = raw_results.iter().filter_map(normalize_image_result).collect();
        let legacy_error = get(projection, "_igError");
        let flag = |key: &str| truthy(get(projection, key));
        let has_legacy = !results.is_empty()
            || flag("_isImageGen")
            || flag("_isImageEdit")
            || !legacy_error.is_null()
            || flag("_igBatchPending");
        if current.is_empty() && !has_legacy {
            return Ok(None);
        }
        let mut mode = get(&current, "mode").clone();
        if !value_in(&mode, &["generate", "edit", "batch"])? {
            mode = json!(if legacy_batch.is_array() {
                "batch"
            } else if flag("_isImageEdit") {
                "edit"
            } else {
                "generate"
            });
        }
        let mut status = get(&current, "status").clone();
        if !value_in(&status, &["running", "completed", "failed", "cancelled"])? {
            status = json!(if flag("_igBatchPending") {
                "running"
            } else if !legacy_error.is_null()
                || (!results.is_empty() && !results.iter().any(|result| truthy(get(result, "ok"))))
            {
                "failed"
            } else {
                "completed"
            });
        }
        let mut document = Obj::new();
        document.insert("blockId".into(), Value::String(text_or(get(&current, "blockId"), "image-generation")));
        document.insert("mode".into(), mode);
        document.insert("status".into(), status);
        document.insert("results".into(), Value::Array(results.into_iter().map(Value::Object).collect()));
        if !get(&current, "error").is_null() {
            document.insert("error".into(), get(&current, "error").clone());
        }
        if !legacy_error.is_null() {
            document.insert("error".into(), legacy_error.clone());
        }
        Ok(Some(Value::Object(document)))
    }

    /// `background_command_projection.normalize_handoff_round`.
    fn normalize_handoff_round(round: Obj) -> OpResult<Obj> {
        if !value_in(get(&round, "toolName"), &["run_command", "code_exec"])? {
            return Ok(round);
        }
        let Value::Array(results) = get(&round, "results").clone() else { return Ok(round) };
        static HANDOFF: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
        let pattern = HANDOFF.get_or_init(|| {
            Regex::new(
                r"\n\[Command moved to background: (bg_[a-f0-9]{12})\. The command is still running;[^\n]*\]\s*$",
            )
            .expect("handoff regex")
        });
        let mut normalized = Vec::new();
        for meta in results {
            let eligible = match &meta {
                Value::Object(fields) => {
                    !truthy(get(fields, "backgroundCommandId"))
                        && (truthy(get(fields, "backgrounded")) || get(fields, "exitCode") == &json!("background"))
                }
                _ => false,
            };
            if !eligible {
                normalized.push(meta);
                continue;
            }
            let fields = meta.as_object().cloned().unwrap_or_default();
            let source = if truthy(get(&round, "content")) {
                get(&round, "content").clone()
            } else {
                get(&fields, "output").clone()
            };
            let body = text(&source);
            let matched = pattern.captures(&body).map(|captures| Value::String(captures[1].to_owned()));
            let command_id = if truthy(get(&round, "backgroundCommandId")) {
                get(&round, "backgroundCommandId").clone()
            } else {
                matched.unwrap_or(Value::Null)
            };
            if truthy(&command_id) {
                let mut updated = fields;
                updated.insert("backgroundCommandId".into(), command_id);
                normalized.push(Value::Object(updated));
            } else {
                normalized.push(Value::Object(fields));
            }
        }
        let mut round = round;
        round.insert("results".into(), Value::Array(normalized));
        Ok(round)
    }

    fn public_tool_round(item: &Obj) -> OpResult<Value> {
        let mut round = item.clone();
        if get(&round, "results").is_null() {
            round.shift_remove("results");
        }
        Ok(Value::Object(normalize_handoff_round(round)?))
    }

    /// `_normalize_tool_rounds`.
    pub fn normalize_tool_rounds(value: &Value) -> OpResult<Value> {
        let Value::Array(items) = value else { return Ok(value.clone()) };
        let mut normalized = Vec::with_capacity(items.len());
        for item in items {
            match item {
                Value::Object(round) => normalized.push(public_tool_round(round)?),
                other => normalized.push(other.clone()),
            }
        }
        Ok(Value::Array(normalized))
    }

    fn normalize_rolled_back(value: &Value) -> Option<Vec<Value>> {
        let Value::Array(items) = value else { return None };
        let mut normalized = Vec::new();
        for item in items {
            if !item.is_object() {
                continue;
            }
            let mut entry = copy_named(item, ROLLED_BACK_FIELDS);
            if !matches!(entry.get("content"), Some(Value::String(text)) if !text.is_empty()) {
                entry.shift_remove("content");
            }
            if !matches!(entry.get("thinking"), Some(Value::String(text)) if !text.is_empty()) {
                entry.shift_remove("thinking");
            }
            if !entry.contains_key("content") && !entry.contains_key("thinking") {
                continue;
            }
            let block = text_or(get(&entry, "blockId"), "rolled-back");
            entry.insert("blockId".into(), Value::String(block));
            if !get(&entry, "attemptId").is_null() {
                let attempt = python_str(get(&entry, "attemptId"));
                entry.insert("attemptId".into(), Value::String(attempt));
            } else {
                entry.shift_remove("attemptId");
            }
            let valid_at =
                matches!(entry.get("at"), Some(Value::Number(number)) if number.as_i64().is_some_and(|at| at > 0));
            if !valid_at {
                entry.shift_remove("at");
            }
            normalized.push(Value::Object(entry));
        }
        if normalized.is_empty() {
            return None;
        }
        let start = normalized.len().saturating_sub(4);
        Some(normalized[start..].to_vec())
    }

    // ── plan_contract.py ──────────────────────────────────────────────────────

    const MAX_PROPOSED_PLAN_CHARS: usize = 64_000;

    fn normalized_plan_text(value: &Value) -> Option<String> {
        let Value::String(raw) = value else { return None };
        let text = py_strip(raw);
        let count = text.chars().count();
        if text.is_empty() || count > MAX_PROPOSED_PLAN_CHARS || text.len() > 4 * MAX_PROPOSED_PLAN_CHARS {
            return None;
        }
        Some(text.to_owned())
    }

    fn extract_proposed_plan(content: &Value) -> Option<String> {
        let Value::String(text) = content else { return None };
        if !text.to_lowercase().contains("<proposed_plan>") {
            return None;
        }
        static PLAN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
        let pattern = PLAN
            .get_or_init(|| Regex::new(r"(?is)<proposed_plan>\s*\n?(.*?)\n?\s*</proposed_plan>").expect("plan regex"));
        let last = pattern.captures_iter(text).last()?;
        normalized_plan_text(&Value::String(last[1].to_owned()))
    }

    fn proposed_plan_id(text: &str) -> String {
        format!("plan_{}", &hex::encode(Sha256::digest(text.as_bytes()))[..24])
    }

    fn proposed_plan_document(value: &Obj, content: &Value) -> Option<Value> {
        let text = extract_proposed_plan(content)?;
        if normalized_plan_text(get(value, "text")).as_deref() != Some(text.as_str()) {
            return None;
        }
        let plan_id = get(value, "planId");
        if !plan_id.is_null() && plan_id != &Value::String(proposed_plan_id(&text)) {
            return None;
        }
        let block = get(value, "blockId");
        if !block.is_null() && block != &json!("proposed-plan") {
            return None;
        }
        let revision = get(value, "revision");
        if !revision.is_null() && !crate::codec::py_eq(revision, &json!(1)) {
            return None;
        }
        let format = get(value, "format");
        if !format.is_null() && format != &json!("markdown") {
            return None;
        }
        Some(json!({
            "blockId": "proposed-plan",
            "planId": proposed_plan_id(&text),
            "revision": 1,
            "format": "markdown",
            "text": text,
        }))
    }

    fn plan_execution_document(value: &Value) -> Option<Value> {
        let Value::Object(value) = value else { return None };
        let plan_text = normalized_plan_text(get(value, "planText"))?;
        let source_turn_id = match get(value, "sourceTurnId") {
            Value::String(text) if !py_strip(text).is_empty() => py_strip(text).to_owned(),
            _ => return None,
        };
        let source_revision = match get(value, "sourceProjectionRevision") {
            Value::Number(number) => number.as_i64().filter(|revision| *revision >= 0)?,
            _ => return None,
        };
        let context_mode = text(get(value, "contextMode"));
        if context_mode != "current" && context_mode != "fresh" {
            return None;
        }
        let plan_id = proposed_plan_id(&plan_text);
        let declared = get(value, "planId");
        if !declared.is_null() && declared != &Value::String(plan_id.clone()) {
            return None;
        }
        let block = get(value, "blockId");
        if !block.is_null() && block != &json!("plan-execution") {
            return None;
        }
        Some(json!({
            "blockId": "plan-execution",
            "planId": plan_id,
            "sourceTurnId": source_turn_id,
            "sourceProjectionRevision": source_revision,
            "contextMode": context_mode,
            "planText": plan_text,
        }))
    }

    // ── background_command_projection.py ─────────────────────────────────────

    /// `bounded_completion_preview` (both fields are bounded at 2000 chars).
    pub fn bounded_completion_preview(metadata: &Obj) -> Obj {
        let mut result = metadata.clone();
        for field in ["command", "resultPreview"] {
            if let Some(Value::String(value)) = result.get(field) {
                if value.chars().count() > 2000 {
                    let truncated = chars_prefix(value, 2000);
                    result.insert(field.into(), Value::String(truncated));
                    if field == "resultPreview" {
                        result.insert("resultTruncated".into(), Value::Bool(true));
                    }
                }
            }
        }
        result
    }

    fn bound_projection_completion_previews(projection: &mut Obj) {
        if let Some(Value::Object(completion)) = projection.get("backgroundCommand") {
            let bounded = bounded_completion_preview(completion);
            projection.insert("backgroundCommand".into(), Value::Object(bounded));
        }
        let Some(Value::Array(injections)) = projection.get("_bgCommandInjects") else { return };
        let mut normalized = Vec::new();
        for injection in injections {
            let previews = match injection {
                Value::Object(fields) => match fields.get("previews") {
                    Some(Value::Array(previews)) => previews,
                    _ => {
                        normalized.push(injection.clone());
                        continue;
                    }
                },
                _ => {
                    normalized.push(injection.clone());
                    continue;
                }
            };
            let mut bounded = Vec::new();
            for preview in previews {
                match preview {
                    Value::Object(fields) if fields.get("backgroundCommand").is_some_and(Value::is_object) => {
                        let mut fields = fields.clone();
                        let completion =
                            bounded_completion_preview(fields["backgroundCommand"].as_object().expect("object"));
                        fields.insert("backgroundCommand".into(), Value::Object(completion));
                        bounded.push(Value::Object(fields));
                    }
                    other => bounded.push(other.clone()),
                }
            }
            let mut injection = injection.as_object().cloned().unwrap_or_default();
            injection.insert("previews".into(), Value::Array(bounded));
            normalized.push(Value::Object(injection));
        }
        projection.insert("_bgCommandInjects".into(), Value::Array(normalized));
    }

    fn legacy_route_snapshot(model_id: &str, provider_id: &str) -> Value {
        let bounded = |value: &str| chars_prefix(value, 256);
        json!({
            "contract_version": "tofu.route-snapshot/v2",
            "legacy": true,
            "selected_model": null,
            "provider_scoped_selection": null,
            "preferred_provider_id": bounded(provider_id),
            "actual_model": if model_id.is_empty() { Value::Null } else { json!({"creator_id": "legacy", "model_id": bounded(model_id)}) },
            "provider_id": bounded(provider_id),
            "offering_id": "",
            "deployment_id": "",
            "connection_id": "",
            "credential": null,
            "wire_model_id": bounded(model_id),
            "transitions": [],
            "degradation_reasons": ["legacy_turn_projection"],
            "recorded_at": 0.0,
        })
    }

    /// `normalize_projection_document(raw)`.
    pub fn normalize_projection_document(raw: &Value) -> OpResult<Obj> {
        let source = match raw {
            Value::String(content) => {
                let mut document = Obj::new();
                document.insert("content".into(), Value::String(content.clone()));
                return Ok(document);
            }
            Value::Object(map) => map.clone(),
            _ => {
                let mut document = Obj::new();
                document.insert("content".into(), Value::String(String::new()));
                return Ok(document);
            }
        };
        let mut result = source.clone();
        bound_projection_completion_previews(&mut result);
        for key in PROJECTION_IDENTITY_KEYS {
            result.shift_remove(*key);
        }
        if get(&result, "providerId").is_null() && !get(&result, "provider_id").is_null() {
            let provider = get(&result, "provider_id").clone();
            result.insert("providerId".into(), provider);
        }
        result.shift_remove("provider_id");
        if !result.contains_key("content") && result.contains_key("text") {
            let content = if truthy(get(&result, "text")) { get(&result, "text").clone() } else { json!("") };
            result.insert("content".into(), content);
        }
        if !result.contains_key("content") {
            result.insert("content".into(), json!(""));
        }
        if get(&result, "usage").is_null() {
            result.shift_remove("usage");
        }
        if !get(&result, "routeSnapshot").is_object() {
            let model_id = py_strip(&text(get(&result, "model"))).to_owned();
            let provider_id = py_strip(&text(get(&result, "providerId"))).to_owned();
            if !model_id.is_empty() || !provider_id.is_empty() {
                result.insert("routeSnapshot".into(), legacy_route_snapshot(&model_id, &provider_id));
            }
        }
        if result.contains_key("toolRounds") {
            let rounds = normalize_tool_rounds(&result["toolRounds"])?;
            result.insert("toolRounds".into(), rounds);
        }
        if let Some(Value::Array(segments)) = result.get("segments") {
            let mut normalized = Vec::with_capacity(segments.len());
            for segment in segments {
                match segment {
                    Value::Object(fields) if fields.get("_round").is_some_and(Value::is_object) => {
                        let round = normalize_tool_rounds(&Value::Array(vec![fields["_round"].clone()]))?;
                        let mut fields = fields.clone();
                        fields.insert("_round".into(), round[0].clone());
                        normalized.push(Value::Object(fields));
                    }
                    other => normalized.push(other.clone()),
                }
            }
            result.insert("segments".into(), Value::Array(normalized));
        }
        let origin = normalize_origin(&source)?;
        let translation = normalize_translation(&source)?;
        let context_snapshot = normalize_context(&source);
        let compaction = normalize_compaction(&source);
        let image_generation = normalize_image_generation(&source)?;
        let activity_timeline = normalize_activity_timeline(get(&source, "activityTimeline"));
        let proposed_plan = match source.get("proposedPlan") {
            Some(Value::Object(plan)) => proposed_plan_document(plan, get(&source, "content")),
            _ => None,
        };
        let plan_execution = plan_execution_document(get(&source, "planExecution"));
        if let Some(origin) = origin {
            result.insert("origin".into(), Value::Object(origin));
        }
        if let Some(translation) = translation {
            result.insert("translation".into(), Value::Object(translation));
        }
        if let Some(context) = context_snapshot {
            result.insert("contextSnapshot".into(), context);
        }
        if let Some(compaction) = compaction {
            result.insert("compaction".into(), Value::Object(compaction));
        }
        if let Some(image_generation) = image_generation {
            result.insert("imageGeneration".into(), image_generation);
        }
        match activity_timeline {
            Some(timeline) => {
                result.insert("activityTimeline".into(), timeline);
            }
            None => {
                result.shift_remove("activityTimeline");
            }
        }
        match proposed_plan {
            Some(plan) => {
                result.insert("proposedPlan".into(), plan);
            }
            None => {
                result.shift_remove("proposedPlan");
            }
        }
        match plan_execution {
            Some(plan) => {
                result.insert("planExecution".into(), plan);
            }
            None => {
                result.shift_remove("planExecution");
            }
        }
        match normalize_rolled_back(get(&source, "rolledBack")) {
            Some(rolled) => {
                result.insert("rolledBack".into(), Value::Array(rolled));
            }
            None => {
                result.shift_remove("rolledBack");
            }
        }
        Ok(result.into_iter().filter(|(key, _)| PUBLIC_PROJECTION_FIELDS.contains(&key.as_str())).collect())
    }

    // ── turn_activity_timeline.py::normalize_activity_timeline ───────────────

    const ACTIVITY_TIMELINE_MAX_ENTRIES: usize = 128;
    const ACTIVITY_TIMELINE_MAX_JSON_BYTES: usize = 96 * 1024;
    const INTEGER_MAX: i64 = 9_007_199_254_740_991;
    const COUNT_MAX: i64 = 2_147_483_647;

    fn string_limit(key: &str) -> Option<usize> {
        Some(match key {
            "id" | "spanId" | "parentSpanId" | "summaryKey" | "detailKey" | "model" | "providerId" | "toolName"
            | "toolCallId" | "reasonCode" | "fromModel" | "toModel" | "archiveId" | "routeId" | "envelopeKind" => 160,
            "kind" | "status" | "tokenCountKind" | "timingMode" | "routeMode" | "anchorPosition" | "requestLevel"
            | "attemptScope" => 24,
            "severity" => 16,
            "summary" => 180,
            "detail" => 400,
            "keyHint" => 64,
            "action" => 48,
            "requestTag" | "phase" | "trigger" | "routeDecision" | "failureStage" => 80,
            "taskId" | "attemptId" => 256,
            "anchorBlockId" => 320,
            "taskModel" | "summaryModel" | "summarySelectedModel" | "summaryProviderId" => 160,
            _ => return None,
        })
    }

    const INTEGER_FIELDS: &[&str] = &[
        "seq",
        "occurredAt",
        "startedAt",
        "endedAt",
        "roundNum",
        "llmRound",
        "anchorToolRoundNum",
        "count",
        "durationMs",
        "statusCode",
        "tokensBefore",
        "tokensAfter",
        "messagesBefore",
        "messagesAfter",
        "reductionPercent",
        "attempt",
    ];

    /// `_text(value, limit)`.
    fn activity_text(value: &Value, limit: usize) -> String {
        let rendered = match value {
            Value::Null => return String::new(),
            Value::String(text) => text.clone(),
            Value::Object(map) => {
                let chosen =
                    ["message", "detail", "content", "error", "reason"].iter().find_map(|key| match map.get(*key) {
                        Some(Value::String(candidate)) if !py_strip(candidate).is_empty() => Some(candidate.clone()),
                        _ => None,
                    });
                chosen.unwrap_or_else(|| py_json_dumps(value, false))
            }
            Value::Array(_) => py_json_dumps(value, false),
            other => python_str(other),
        };
        let joined = rendered.split(py_isspace).filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" ");
        chars_prefix(&joined, limit)
    }

    /// `bound_text(value, max_chars)` (`log_record_values.py`).
    fn bound_text(text: &str, max_chars: usize) -> String {
        let limit = max_chars.max(128);
        let length = text.chars().count();
        if length <= limit {
            return text.to_owned();
        }
        let reserve = format!("\n… <log policy omitted {length} chars> …\n").chars().count();
        let available = (limit as i64 - reserve as i64).max(32) as usize;
        let head = (available as f64 * 0.62) as usize;
        let tail = available - head;
        let chars: Vec<char> = text.chars().collect();
        let head_text: String = chars[..head].iter().collect();
        let tail_text: String = chars[length - tail..].iter().collect();
        let omitted = length - head - tail;
        format!("{head_text}\n… <log policy omitted {omitted} chars> …\n{tail_text}")
    }

    fn safe_arg(value: &Value) -> Value {
        match value {
            Value::Null | Value::Bool(_) => value.clone(),
            Value::Number(_) => value.clone(),
            Value::String(text) => Value::String(chars_prefix(&bound_text(text, 160), 160)),
            Value::Array(items) => Value::Array(
                items.iter().take(8).filter(|item| !item.is_array() && !item.is_object()).map(safe_arg).collect(),
            ),
            Value::Object(_) => Value::String(chars_prefix(&bound_text(&activity_text(value, 160), 160), 160)),
        }
    }

    fn safe_args(value: &Value) -> Obj {
        let Value::Object(map) = value else { return Obj::new() };
        let mut result = Obj::new();
        for (key, item) in map.iter().take(12) {
            result.insert(chars_prefix(key, 80), safe_arg(item));
        }
        result
    }

    /// Python `int(value)` that answers `None` on TypeError/ValueError.
    fn lenient_int(value: &Value) -> Option<i64> {
        match value {
            Value::Bool(_) => None,
            Value::Number(number) => number.as_i64().or_else(|| number.as_f64().map(|float| float.trunc() as i64)),
            Value::String(text) => {
                let cleaned = py_strip(text);
                let digits = cleaned.trim_start_matches(['+', '-']);
                if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
                    return None;
                }
                cleaned.replace('_', "").parse().ok()
            }
            _ => None,
        }
    }

    fn nonnegative_int(value: &Value, maximum: i64) -> Option<i64> {
        let parsed = lenient_int(value)?;
        if parsed >= 0 {
            Some(parsed.min(maximum))
        } else {
            None
        }
    }

    fn identity_int(value: &Value) -> Option<i64> {
        let parsed = match value {
            Value::Number(number) => number.as_i64()?,
            Value::String(text) => {
                if text.is_empty() || !text.chars().all(|c| c.is_ascii_digit()) || text.chars().count() > 10 {
                    return None;
                }
                text.parse().ok()?
            }
            _ => return None,
        };
        if (0..=INTEGER_MAX).contains(&parsed) {
            Some(parsed)
        } else {
            None
        }
    }

    fn compaction_fact_text(value: &Value, limit: usize) -> String {
        let Value::String(text) = value else { return String::new() };
        static SECRET: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
        let pattern = SECRET.get_or_init(|| {
            Regex::new(concat!(
                r#"(?i)(?:authorization(?:\\?["'])?\s*:|bearer\s+|"#,
                r#"(?:api[_-]?(?:key|secret)|(?:access|refresh)[_-]?token|token|password|secret)"#,
                r#"(?:\\?["'])?\s*[:=]|https?://[^/\s:@]+:[^/\s@]+@)"#
            ))
            .expect("secret regex")
        });
        if pattern.is_match(text) {
            return "<redacted>".into();
        }
        activity_text(value, limit)
    }

    fn summary_calls(value: &Value) -> Vec<Value> {
        const ALLOWED: &[&str] = &[
            "model",
            "providerId",
            "status",
            "costUsd",
            "known",
            "reason",
            "costBasis",
            "pricingSource",
            "pricingSnapshot",
            "source",
            "currency",
            "provider",
            "selectedPromptTokens",
            "tierId",
            "maxPromptTokens",
            "usdToCny",
            "rates",
            "input",
            "output",
            "cacheReadMul",
            "cacheWriteMul",
            "cacheWrite1hMul",
            "cacheWrite1hTokens",
        ];
        fn clean(row: &Obj, depth: usize) -> Obj {
            let mut result = Obj::new();
            for (key, item) in row {
                if !ALLOWED.contains(&key.as_str()) {
                    continue;
                }
                if matches!(key.as_str(), "model" | "providerId" | "provider") && !item.is_string() {
                    continue;
                }
                match item {
                    Value::Object(nested) if matches!(key.as_str(), "pricingSnapshot" | "rates") && depth < 2 => {
                        result.insert(key.clone(), Value::Object(clean(nested, depth + 1)));
                    }
                    Value::String(_) => {
                        result.insert(key.clone(), Value::String(compaction_fact_text(item, 160)));
                    }
                    Value::Null | Value::Bool(_) => {
                        result.insert(key.clone(), item.clone());
                    }
                    Value::Number(number) => {
                        let magnitude = number.as_f64().map(f64::abs).unwrap_or(f64::INFINITY);
                        let within = match number.as_i64() {
                            Some(integer) => integer.unsigned_abs() <= INTEGER_MAX as u64,
                            None => magnitude <= INTEGER_MAX as f64,
                        };
                        if within && magnitude.is_finite() {
                            result.insert(key.clone(), item.clone());
                        }
                    }
                    _ => {}
                }
            }
            result
        }
        let Value::Array(rows) = value else { return vec![] };
        rows.iter().take(16).filter_map(Value::as_object).map(|row| Value::Object(clean(row, 0))).collect()
    }

    fn is_failure_archive_bridge(value: &str) -> bool {
        [
            "summary-failed:",
            "summary-declined:",
            "summary-aborted:",
            "summary-cancelled:",
            "summary-rejected:",
            "failed:",
        ]
        .iter()
        .any(|prefix| value.starts_with(prefix))
    }

    fn normalize_entry(raw: &Value) -> Option<Obj> {
        let Value::Object(raw) = raw else { return None };
        for key in ["taskId", "attemptId", "anchorBlockId"] {
            match raw.get(key) {
                None | Some(Value::Null) => {}
                Some(Value::String(value))
                    if !py_strip(value).is_empty() && value.chars().count() <= string_limit(key)? => {}
                Some(_) => return None,
            }
        }
        if !get(raw, "anchorToolRoundNum").is_null() && identity_int(get(raw, "anchorToolRoundNum")).is_none() {
            return None;
        }
        match get(raw, "anchorPosition") {
            Value::Null => {}
            Value::String(position) if matches!(position.as_str(), "before" | "after" | "producer-start") => {}
            _ => return None,
        }
        let mut entry = Obj::new();
        for (key, value) in raw {
            if value.is_null() {
                continue;
            }
            if let Some(limit) = string_limit(key) {
                if key == "archiveId" && is_failure_archive_bridge(&activity_text(value, limit)) {
                    continue;
                }
                let rendered = match key.as_str() {
                    "taskId" | "attemptId" | "anchorBlockId" => text(value),
                    "taskModel" | "summaryModel" | "summarySelectedModel" | "summaryProviderId" => {
                        compaction_fact_text(value, limit)
                    }
                    _ => activity_text(value, limit),
                };
                if !rendered.is_empty() {
                    entry.insert(key.clone(), Value::String(rendered));
                }
            } else if INTEGER_FIELDS.contains(&key.as_str()) {
                let parsed = if key == "llmRound" || key == "anchorToolRoundNum" {
                    identity_int(value)
                } else {
                    nonnegative_int(value, if key == "reductionPercent" { 100 } else { INTEGER_MAX })
                };
                if let Some(parsed) = parsed {
                    entry.insert(key.clone(), Value::from(parsed));
                }
            } else if key == "summaryCalls" {
                let calls = summary_calls(value);
                if !calls.is_empty() {
                    entry.insert(key.clone(), Value::Array(calls));
                }
            } else if key == "summaryArgs" || key == "detailArgs" {
                let args = safe_args(value);
                if !args.is_empty() {
                    entry.insert(key.clone(), Value::Object(args));
                }
            }
        }
        if !truthy(get(&entry, "id")) || !truthy(get(&entry, "spanId")) {
            return None;
        }
        if !matches!(get(&entry, "kind").as_str(), Some("model" | "tool" | "status" | "error" | "system")) {
            return None;
        }
        if !matches!(
            get(&entry, "status").as_str(),
            Some(
                "started"
                    | "running"
                    | "waiting"
                    | "succeeded"
                    | "failed"
                    | "skipped"
                    | "switched"
                    | "aborted"
                    | "superseded"
            )
        ) {
            return None;
        }
        if !matches!(get(&entry, "severity").as_str(), Some("info" | "warning" | "error")) {
            return None;
        }
        if !entry.contains_key("occurredAt") {
            return None;
        }
        let count = get(&entry, "count").as_i64().filter(|count| *count != 0).unwrap_or(1);
        entry.insert("count".into(), Value::from(count.clamp(1, COUNT_MAX)));
        Some(entry)
    }

    fn eviction_rank(entry: &Obj) -> u8 {
        if matches!(get(entry, "reasonCode").as_str(), Some("context_compaction" | "compacting")) {
            return 4;
        }
        if get(entry, "severity").as_str() == Some("error")
            || matches!(get(entry, "status").as_str(), Some("failed" | "switched"))
        {
            return 3;
        }
        if get(entry, "severity").as_str() != Some("info") {
            return 2;
        }
        if matches!(get(entry, "kind").as_str(), Some("status" | "model")) {
            0
        } else {
            1
        }
    }

    fn eviction_index(entries: &[Obj]) -> usize {
        let mut best: Option<(usize, u8)> = None;
        for (index, entry) in entries.iter().enumerate() {
            let rank = eviction_rank(entry);
            if rank == 0 {
                return index;
            }
            if best.is_none_or(|(_, best_rank)| rank < best_rank) {
                best = Some((index, rank));
            }
        }
        best.map(|(index, _)| index).unwrap_or(0)
    }

    fn timeline_document(entries: &[Obj], dropped: i64) -> Value {
        let mut document = Obj::new();
        document.insert("blockId".into(), json!("activity-timeline"));
        document.insert("version".into(), json!(1));
        document.insert("entries".into(), Value::Array(entries.iter().cloned().map(Value::Object).collect()));
        if dropped != 0 {
            document.insert("droppedCount".into(), Value::from(dropped));
        }
        Value::Object(document)
    }

    fn trim_entries(mut entries: Vec<Obj>, mut dropped: i64) -> (Vec<Obj>, i64) {
        while entries.len() > ACTIVITY_TIMELINE_MAX_ENTRIES {
            let index = eviction_index(&entries);
            entries.remove(index);
            dropped = (dropped + 1).min(COUNT_MAX);
        }
        if entries.is_empty() {
            return (entries, dropped);
        }
        let mut size = py_json_dumps(&timeline_document(&entries, dropped), false).len() as i64;
        while !entries.is_empty() && size > ACTIVITY_TIMELINE_MAX_JSON_BYTES as i64 {
            let index = eviction_index(&entries);
            let row_bytes = py_json_dumps(&Value::Object(entries[index].clone()), false).len() as i64;
            entries.remove(index);
            let previous = dropped;
            dropped = (dropped + 1).min(COUNT_MAX);
            size -= row_bytes + 1;
            if previous != 0 {
                size += dropped.to_string().len() as i64 - previous.to_string().len() as i64;
            } else if dropped != 0 {
                size += ",\"droppedCount\":".len() as i64 + dropped.to_string().len() as i64;
            }
        }
        (entries, dropped)
    }

    /// `normalize_activity_timeline(raw)`.
    pub fn normalize_activity_timeline(raw: &Value) -> Option<Value> {
        let Value::Object(raw) = raw else { return None };
        let entries: Vec<Obj> = match raw.get("entries") {
            Some(Value::Array(items)) => items.iter().filter_map(normalize_entry).collect(),
            _ => vec![],
        };
        if entries.is_empty() {
            return None;
        }
        let dropped = nonnegative_int(get(raw, "droppedCount"), COUNT_MAX).unwrap_or(0);
        let (entries, dropped) = trim_entries(entries, dropped);
        Some(timeline_document(&entries, dropped))
    }
}
