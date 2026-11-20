//! `lib/llm/responses_outbound/_reasoning.py` `ReasoningSummaryReconciler`:
//! readable Responses reasoning (summary and content parts) reconciled across
//! delta/done events, part lifecycles, item snapshots and the terminal output.
//! Each part keeps its streamed text where Python keeps a SHA-256 prefix
//! proof; both accept exactly the snapshots whose prefix equals that text.

use std::collections::{BTreeMap, HashMap};

use serde_json::{Map, Value};

use super::py;

const MAX_INDEX: i64 = 4095;
const MAX_ITEM_ID_CHARS: usize = 512;
/// `lib/llm/_sse_framer.py` `MAX_SSE_EVENT_BYTES`.
const MAX_SSE_EVENT_BYTES: usize = 1 << 20;

/// `(channel, index)` with summary (0) sorting before content (1): `_part_order`.
type PartKey = (u8, i64);

const SUMMARY: u8 = 0;
const CONTENT: u8 = 1;

/// A `ReasoningSummaryError` message.
pub(crate) type ReasoningError = String;
type Step<T> = Result<T, ReasoningError>;

#[derive(Default)]
struct Part {
    chars: usize,
    text: String,
    pending: Vec<String>,
    closed: bool,
    started: bool,
    emitted_order: Option<usize>,
}

#[derive(Default)]
struct Item {
    output_index: Option<i64>,
    item_id: Option<String>,
    implicit: bool,
    closed: bool,
    parts: BTreeMap<PartKey, Part>,
    current_parts: HashMap<u8, i64>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Source {
    Event,
    Added,
    ItemDone,
    Terminal,
}

#[derive(Default)]
pub(crate) struct ReasoningReconciler {
    items: Vec<Item>,
    by_index: HashMap<i64, usize>,
    by_id: HashMap<String, Vec<usize>>,
    current_item: Option<usize>,
    part_count: i64,
    pending_bytes: usize,
    emitted_part_count: usize,
    last_emitted: Option<(usize, PartKey)>,
}

fn index_of(value: Option<&Value>, field: &str) -> Step<Option<i64>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(number)) => number
            .as_i64()
            .filter(|index| (0..=MAX_INDEX).contains(index))
            .map(Some)
            .ok_or_else(|| format!("reasoning {field} must be a bounded integer or null")),
        Some(_) => Err(format!(
            "reasoning {field} must be a bounded integer or null"
        )),
    }
}

fn identity(value: Option<&Value>) -> Step<Option<String>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if text.is_empty() => Ok(None),
        Some(Value::String(text)) if py::char_len(text) <= MAX_ITEM_ID_CHARS => {
            Ok(Some(text.clone()))
        }
        Some(_) => Err("reasoning item_id must be bounded text or null".into()),
    }
}

fn text_of(value: Option<&Value>) -> Step<Option<String>> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err("reasoning text must be text or null".into()),
    }
}

impl ReasoningReconciler {
    fn bind(&mut self, item: usize, item_id: Option<&str>, index: Option<i64>) -> Step<usize> {
        if let Some(index) = index {
            if self.items[item]
                .output_index
                .is_some_and(|known| known != index)
            {
                return Err("reasoning output_index changed its identity".into());
            }
            self.items[item].output_index = Some(index);
            self.by_index.insert(index, item);
        }
        if let Some(item_id) = item_id {
            match &self.items[item].item_id {
                Some(known) if known != item_id => {
                    return Err(
                        "reasoning item_id and output_index identify different items".into(),
                    )
                }
                Some(_) => {}
                None => {
                    self.items[item].item_id = Some(item_id.to_owned());
                    self.by_id.entry(item_id.to_owned()).or_default().push(item);
                }
            }
        }
        Ok(item)
    }

    fn new_item(&mut self, implicit: bool) -> Step<usize> {
        if self.items.len() as i64 > MAX_INDEX {
            return Err("reasoning item state exceeds the response-position bound".into());
        }
        self.items.push(Item {
            implicit,
            ..Item::default()
        });
        Ok(self.items.len() - 1)
    }

    fn resolve_item(
        &mut self,
        item_id: Option<&Value>,
        output_index: Option<&Value>,
        source: Source,
    ) -> Step<usize> {
        let index = index_of(output_index, "output_index")?;
        let item_id = identity(item_id)?;
        let item_id = item_id.as_deref();
        let candidates: Vec<usize> = item_id
            .and_then(|id| self.by_id.get(id))
            .cloned()
            .unwrap_or_default();
        let indexed = index.and_then(|index| self.by_index.get(&index).copied());
        let lifecycle = source != Source::Event;
        if let Some(indexed) = indexed {
            let known = self.items[indexed].item_id.as_deref();
            if let (Some(id), Some(known)) = (item_id, known) {
                if id != known {
                    return Err(
                        "reasoning item_id and output_index identify different items".into(),
                    );
                }
            }
            if item_id.is_some()
                && known.is_none()
                && !candidates.is_empty()
                && !candidates.contains(&indexed)
                && !lifecycle
            {
                return Err("reasoning routing identities resolve to different items".into());
            }
            return self.bind(indexed, item_id, index);
        }
        if !candidates.is_empty() {
            if index.is_none() {
                if candidates.len() != 1 {
                    return Err("ambiguous reasoning item_id; output_index is required".into());
                }
                return Ok(candidates[0]);
            }
            let unindexed: Vec<usize> = candidates
                .iter()
                .copied()
                .filter(|item| self.items[*item].output_index.is_none())
                .collect();
            if unindexed.len() == 1 {
                return self.bind(unindexed[0], item_id, index);
            }
            if !lifecycle {
                return Err(
                    "reasoning item_id and output_index resolve to different identities".into(),
                );
            }
        } else {
            let unbound = self.items.iter().position(|item| {
                item.output_index.is_none() && (item_id.is_none() || item.item_id.is_none())
            });
            if let (Source::Terminal, Some(unbound)) = (source, unbound) {
                return self.bind(unbound, item_id, index);
            }
            let compatible: Vec<usize> = (0..self.items.len())
                .filter(|position| {
                    let item = &self.items[*position];
                    !item.closed
                        && item.implicit
                        && (index.is_none()
                            || item.output_index.is_none()
                            || item.output_index == index)
                        && (item_id.is_none()
                            || item.item_id.is_none()
                            || item.item_id.as_deref() == item_id)
                })
                .collect();
            if compatible.len() == 1 && (index.is_some() || item_id.is_some()) {
                return self.bind(compatible[0], item_id, index);
            }
        }
        if index.is_none() && item_id.is_none() && source != Source::Terminal {
            if let Some(current) = self.current_item {
                let closed = self.items[current].closed;
                if (!matches!(source, Source::Added | Source::ItemDone) || !closed)
                    && (source != Source::Added || self.items[current].implicit)
                {
                    return Ok(current);
                }
            }
        }
        let fresh = self.new_item(!lifecycle)?;
        let item = self.bind(fresh, item_id, index)?;
        if self.current_item.is_none() || source != Source::Terminal {
            self.current_item = Some(item);
        }
        Ok(item)
    }

    fn ordered_items(&self) -> Vec<usize> {
        let mut order: Vec<usize> = (0..self.items.len()).collect();
        if self.items.iter().all(|item| item.output_index.is_some()) {
            order.sort_by_key(|item| self.items[*item].output_index);
        }
        order
    }

    fn validate_emitted_order(&self) -> Step<()> {
        let mut indexed: Vec<usize> = self.by_index.values().copied().collect();
        indexed.sort_by_key(|item| self.items[*item].output_index);
        for order in [self.ordered_items(), indexed] {
            let mut previous: Option<usize> = None;
            for item in order {
                for part in self.items[item].parts.values() {
                    let Some(emitted) = part.emitted_order else {
                        continue;
                    };
                    if previous.is_some_and(|previous| emitted <= previous) {
                        return Err(
                            "reasoning terminal order contradicts the emitted paragraph prefix"
                                .into(),
                        );
                    }
                    previous = Some(emitted);
                }
            }
        }
        Ok(())
    }

    fn part(&mut self, item: usize, channel: u8, index: i64) -> Step<PartKey> {
        for preceding in 0..=index {
            let key = (channel, preceding);
            if !self.items[item].parts.contains_key(&key) {
                if self.part_count > MAX_INDEX {
                    return Err("reasoning part state exceeds the response-position bound".into());
                }
                self.items[item].parts.insert(key, Part::default());
                self.part_count += 1;
            }
        }
        Ok((channel, index))
    }

    fn resolve_part(
        &mut self,
        item: usize,
        event: &Map<String, Value>,
        channel: u8,
        added: bool,
    ) -> Step<PartKey> {
        let field = if channel == SUMMARY {
            "summary_index"
        } else {
            "content_index"
        };
        let mut index = index_of(event.get(field), field)?;
        if index.is_none() {
            let mut current = self.items[item]
                .current_parts
                .get(&channel)
                .copied()
                .unwrap_or(0);
            if let Some(part) = self.items[item].parts.get_mut(&(channel, current)) {
                if added && (part.started || part.chars > 0 || part.closed) {
                    part.closed = true;
                    current += 1;
                    if current > MAX_INDEX {
                        return Err(format!(
                            "reasoning {field} must be a bounded integer or null"
                        ));
                    }
                }
            }
            index = Some(current);
        }
        let index = index.unwrap_or(0);
        let key = self.part(item, channel, index)?;
        self.items[item].current_parts.insert(channel, index);
        if added {
            self.part_mut(item, key).started = true;
        }
        Ok(key)
    }

    fn part_mut(&mut self, item: usize, key: PartKey) -> &mut Part {
        self.items[item].parts.get_mut(&key).expect("part exists")
    }

    fn can_emit(&self, target: usize, target_key: PartKey) -> bool {
        for item in self.ordered_items() {
            for (key, part) in &self.items[item].parts {
                if item == target && *key == target_key {
                    return true;
                }
                if !part.closed {
                    return false;
                }
            }
            if !self.items[item].closed {
                return false;
            }
        }
        false
    }

    fn append(&mut self, item: usize, key: PartKey, text: &str) -> Step<()> {
        if text.is_empty() {
            return Ok(());
        }
        if let Some((last_item, last_key)) = self.last_emitted {
            let ordered = self.ordered_items();
            let position = |wanted: usize| ordered.iter().position(|item| *item == wanted);
            if position(item) < position(last_item) || (item == last_item && key < last_key) {
                return Err(
                    "reasoning snapshot extends a prefix before an already emitted paragraph"
                        .into(),
                );
            }
        }
        if let Some(own) = self.items[item].output_index {
            let later_emitted = self.by_index.iter().any(|(index, indexed)| {
                *index > own
                    && self.items[*indexed]
                        .parts
                        .values()
                        .any(|part| part.emitted_order.is_some())
            });
            if later_emitted {
                return Err(
                    "reasoning snapshot inserts text before an emitted indexed paragraph prefix"
                        .into(),
                );
            }
        }
        if self.pending_bytes + text.len() > MAX_SSE_EVENT_BYTES && !self.can_emit(item, key) {
            return Err("reasoning pending text exceeds the SSE byte bound".into());
        }
        let part = self.part_mut(item, key);
        part.chars += py::char_len(text);
        part.text.push_str(text);
        part.pending.push(text.to_owned());
        self.pending_bytes += text.len();
        Ok(())
    }

    fn complete(&mut self, item: usize, key: PartKey, text: Option<&str>) -> Step<()> {
        let Some(text) = text else {
            return Ok(());
        };
        let part = &self.items[item].parts[&key];
        let prefix = py::prefix(text, part.chars);
        if py::char_len(prefix) != part.chars || prefix != part.text {
            return Err("reasoning snapshot contradicts its streamed text prefix".into());
        }
        let suffix = text[prefix.len()..].to_owned();
        self.append(item, key, &suffix)
    }

    fn drain(&mut self) -> Vec<String> {
        let mut emitted = Vec::new();
        for item in self.ordered_items() {
            let keys: Vec<PartKey> = self.items[item].parts.keys().copied().collect();
            for key in keys {
                if !self.items[item].parts[&key].pending.is_empty() {
                    if self.last_emitted.is_some_and(|last| last != (item, key)) {
                        emitted.push("\n\n".to_owned());
                    }
                    let order = self.emitted_part_count;
                    let part = self.part_mut(item, key);
                    let text = part.pending.concat();
                    part.pending.clear();
                    let first_emission = part.emitted_order.is_none();
                    if first_emission {
                        part.emitted_order = Some(order);
                    }
                    self.pending_bytes -= text.len();
                    emitted.push(text);
                    if first_emission {
                        self.emitted_part_count += 1;
                    }
                    self.last_emitted = Some((item, key));
                }
                if !self.items[item].parts[&key].closed {
                    return emitted;
                }
            }
            if !self.items[item].closed {
                return emitted;
            }
        }
        emitted
    }

    fn finish_event(&mut self) -> Step<Vec<String>> {
        self.validate_emitted_order()?;
        let emitted = self.drain();
        self.validate_emitted_order()?;
        if self.pending_bytes > MAX_SSE_EVENT_BYTES {
            return Err("reasoning pending text exceeds the SSE byte bound".into());
        }
        Ok(emitted)
    }

    /// `event`: text deltas/done and summary-part lifecycle events.
    pub(crate) fn event(
        &mut self,
        event: &Map<String, Value>,
        event_type: &str,
    ) -> Step<Vec<String>> {
        let channel = if event_type.starts_with("response.reasoning_text.") {
            CONTENT
        } else {
            SUMMARY
        };
        let item = self.resolve_item(
            event.get("item_id"),
            event.get("output_index"),
            Source::Event,
        )?;
        let is_part = event_type.starts_with("response.reasoning_summary_part.");
        let added = is_part && event_type.ends_with(".added");
        let key = self.resolve_part(item, event, channel, added)?;
        if is_part {
            if let Some(snapshot) = event.get("part") {
                let Some(snapshot) = snapshot.as_object() else {
                    return Err("reasoning summary part must be an object".into());
                };
                let text = text_of(snapshot.get("text"))?;
                if !added || text.as_deref().is_some_and(|text| !text.is_empty()) {
                    self.complete(item, key, text.as_deref())?;
                }
            }
        } else if event_type.ends_with(".delta") {
            let text = text_of(event.get("delta"))?.unwrap_or_default();
            self.append(item, key, &text)?;
        } else {
            let text = text_of(event.get("text"))?;
            self.complete(item, key, text.as_deref())?;
        }
        if event_type.ends_with(".done") {
            self.part_mut(item, key).closed = true;
        }
        self.finish_event()
    }

    fn snapshot_parts(item: &Map<String, Value>) -> Step<Vec<(u8, Vec<Option<String>>)>> {
        let mut snapshots = Vec::new();
        for (channel, name) in [(SUMMARY, "summary"), (CONTENT, "content")] {
            let parts = match item.get(name) {
                None | Some(Value::Null) => continue,
                Some(Value::Array(parts)) => parts,
                Some(_) => return Err(format!("reasoning {name} must be an array or null")),
            };
            if parts.len() as i64 > MAX_INDEX + 1 {
                return Err("reasoning snapshot parts exceed the response-position bound".into());
            }
            let mut texts = Vec::new();
            for part in parts {
                let Some(part) = part.as_object() else {
                    return Err("reasoning snapshot part must be an object".into());
                };
                texts.push(text_of(part.get("text"))?);
            }
            snapshots.push((channel, texts));
        }
        Ok(snapshots)
    }

    fn snapshot(
        &mut self,
        item: usize,
        snapshots: &[(u8, Vec<Option<String>>)],
        done: bool,
    ) -> Step<()> {
        for (channel, texts) in snapshots {
            if !texts.is_empty() && texts.iter().all(Option::is_some) {
                let omitted = self.items[item].parts.iter().any(|(key, part)| {
                    key.0 == *channel && key.1 >= texts.len() as i64 && part.chars > 0
                });
                if omitted {
                    return Err("reasoning snapshot omitted a streamed part prefix".into());
                }
            }
            for (index, text) in texts.iter().enumerate() {
                let key = self.part(item, *channel, index as i64)?;
                if done || text.as_deref().is_some_and(|text| !text.is_empty()) {
                    self.complete(item, key, text.as_deref())?;
                }
                if done {
                    self.part_mut(item, key).closed = true;
                }
            }
        }
        if done {
            let item = &mut self.items[item];
            item.closed = true;
            for part in item.parts.values_mut() {
                part.closed = true;
            }
        }
        Ok(())
    }

    /// `item`: one reasoning output item (`added` for `output_item.added`).
    pub(crate) fn item(
        &mut self,
        item: &Map<String, Value>,
        output_index: Option<&Value>,
        added: bool,
    ) -> Step<Vec<String>> {
        let snapshots = Self::snapshot_parts(item)?;
        let source = if added {
            Source::Added
        } else {
            Source::ItemDone
        };
        let state = self.resolve_item(item.get("id"), output_index, source)?;
        if added {
            self.items[state].implicit = false;
            self.current_item = Some(state);
        }
        self.snapshot(state, &snapshots, !added)?;
        self.finish_event()
    }

    /// `terminal`: every final reasoning snapshot, then close all state.
    pub(crate) fn terminal(&mut self, output: &[&Map<String, Value>]) -> Step<Vec<String>> {
        let mut snapshots = Vec::new();
        for (index, item) in output.iter().enumerate() {
            if item.get("type").and_then(Value::as_str) == Some("reasoning") {
                snapshots.push((index, *item, Self::snapshot_parts(item)?));
            }
        }
        for (index, item, parts) in snapshots {
            let position = Value::from(index);
            let state = self.resolve_item(item.get("id"), Some(&position), Source::Terminal)?;
            self.snapshot(state, &parts, true)?;
        }
        for item in &mut self.items {
            item.closed = true;
            for part in item.parts.values_mut() {
                part.closed = true;
            }
        }
        self.finish_event()
    }
}
