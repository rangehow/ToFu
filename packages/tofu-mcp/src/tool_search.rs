//! Pre-request MCP catalog search with conversation-sticky native schemas
//! (`lib/mcp/tool_search.py`, the Python oracle).
//!
//! A content-addressed catalog index (term postings, risk-ordered fallback,
//! phrase boosts), dependency/bundle expansion, and the per-scope selection
//! state: called-tool history plus the frozen wire. Like the oracle's module
//! globals, one [`ToolSearch`] serves the process ([`global`]).
//!
//! Facts other owners keep come through host-to-binding calls
//! (`callback.rs`): the index LRU capacity
//! (`tool_search.index_capacity`, `lib/tools/resource_policy.py`) and
//! the durable conversation wire (`mcp.declarations_get` /
//! `mcp.declarations_freeze`, `lib/storage/mcp_declarations.py`). Task-only
//! wires stay bounded process state, validated like `lib/mcp/declarations.py`.
//!
//! Text semantics are CPython's: `casefold`, `lower`, `\w` and `strip` come
//! from `generated_pyunicode.rs`.

use crate::callback::BindingCalls;
use crate::errors::CallError;
use crate::generated_pyunicode as uni;
use crate::names::parse_namespaced_name;
use crate::pyjson::{self, py_strip};
use crate::pyos::{py_str, truthy};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

/// `DEFAULT_ACTIVE_LIMIT`.
pub const DEFAULT_ACTIVE_LIMIT: i64 = 8;
/// `MAX_DEPENDENCY_DEPTH`.
pub const MAX_DEPENDENCY_DEPTH: usize = 8;
/// `_STATE_TTL_SECONDS`.
pub const STATE_TTL_SECONDS: f64 = 24.0 * 60.0 * 60.0;
/// `_MAX_INLINE_STATE_KEY_CHARS`.
const MAX_INLINE_STATE_KEY_CHARS: usize = 256;
/// `lib/mcp/declarations.py` `MAX_DECLARATION_BYTES`.
pub const MAX_DECLARATION_BYTES: usize = 256 * 1024;
/// `lib/mcp/declarations.py` `MAX_DECLARATIONS`.
pub const MAX_DECLARATIONS: usize = 128;

const READ_ONLY_RISKS: [&str; 4] = ["none", "read", "read-only", "readonly"];
const META_KEYS: [&str; 8] = [
    "bundle",
    "requires",
    "profiles",
    "intents",
    "aliases",
    "risk",
    "schemaHash",
    "catalogVersion",
];

// ── CPython text helpers ─────────────────────────────────────────────────────

/// `str.casefold()`.
pub fn py_casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match uni::casefold_mapping(ch) {
            Some(mapped) => out.push_str(mapped),
            None => out.push(ch),
        }
    }
    out
}

/// `str.lower()`: per-character mappings, with Final_Sigma decided in context.
pub fn py_lower(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for (index, ch) in text.char_indices() {
        if ch == '\u{3a3}' {
            // Rust's `str::to_lowercase` implements the same Final_Sigma rule;
            // the prefix's lowering has the same byte length whichever sigma it chose.
            let whole = text.to_lowercase();
            let offset = text[..index].to_lowercase().len();
            out.push(whole[offset..].chars().next().unwrap_or('\u{3c3}'));
            continue;
        }
        match uni::lower_mapping(ch) {
            Some(mapped) => out.push_str(mapped),
            None => out.push(ch),
        }
    }
    out
}

/// A `\w` character of a `re.UNICODE` pattern (`str.isalnum()` or `_`).
pub fn py_word_char(ch: char) -> bool {
    ch == '_' || uni::is_alnum(ch)
}

/// Python `int(value)` for a decoded JSON value; `None` where it raises.
pub fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(number) => number.as_i64().or_else(|| {
            number
                .as_f64()
                .filter(|f| f.is_finite())
                .map(|f| f.trunc() as i64)
        }),
        Value::String(text) => {
            let text = py_strip(text);
            let (negative, digits) = match text.as_bytes().first() {
                Some(b'-') => (true, &text[1..]),
                Some(b'+') => (false, &text[1..]),
                _ => (false, text),
            };
            if digits.is_empty()
                || digits.starts_with('_')
                || digits.ends_with('_')
                || digits.contains("__")
                || !digits.chars().all(|c| c.is_ascii_digit() || c == '_')
            {
                return None;
            }
            let parsed: i64 = digits.replace('_', "").parse().ok()?;
            Some(if negative { -parsed } else { parsed })
        }
        _ => None,
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `str(value or '')`.
fn str_or_empty(value: Option<&Value>) -> String {
    if truthy(value) {
        value.map(py_str).unwrap_or_default()
    } else {
        String::new()
    }
}

/// The first truthy value of `a or b or ...`, as `str`.
fn first_truthy(values: &[Option<&Value>]) -> String {
    values
        .iter()
        .find(|v| truthy(**v))
        .and_then(|v| v.map(py_str))
        .unwrap_or_default()
}

fn object(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

// ── terms ───────────────────────────────────────────────────────────────────

/// `[a-z0-9_.-]` under `re.IGNORECASE`: CPython also matches these four.
fn latin_term_char(ch: char) -> bool {
    ch.is_ascii_alphanumeric()
        || matches!(
            ch,
            '_' | '.' | '-' | '\u{130}' | '\u{131}' | '\u{17f}' | '\u{212a}'
        )
}

/// `_iter_terms`.
pub fn iter_terms(text: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let chars: Vec<char> = text.chars().collect();
    let mut index = 0;
    while index < chars.len() {
        let ch = chars[index];
        let word = if latin_term_char(ch) {
            let start = index;
            while index < chars.len() && latin_term_char(chars[index]) {
                index += 1;
            }
            chars[start..index].iter().collect::<String>()
        } else if ('\u{3400}'..='\u{9fff}').contains(&ch) {
            index += 1;
            ch.to_string()
        } else {
            index += 1;
            continue;
        };
        let word = py_casefold(&word);
        terms.push(word.clone());
        for part in word.split(['_', '.', '/', ':', '-']) {
            if !part.is_empty() && part != word {
                terms.push(part.to_string());
            }
        }
    }
    terms
}

/// `_query_profile`: term multiplicities in first-seen order plus the sequence digest.
pub fn query_profile(text: &str) -> (Vec<(String, usize)>, String) {
    let mut counts: Vec<(String, usize)> = Vec::new();
    let mut positions: HashMap<String, usize> = HashMap::new();
    let mut digest = Sha256::new();
    for term in iter_terms(text) {
        digest.update((term.len() as u32).to_be_bytes());
        digest.update(term.as_bytes());
        match positions.get(&term) {
            Some(&at) => counts[at].1 += 1,
            None => {
                positions.insert(term.clone(), counts.len());
                counts.push((term, 1));
            }
        }
    }
    let hex = digest
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    (counts, hex)
}

/// `_string_list`.
fn string_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::String(text)) => {
            if py_strip(text).is_empty() {
                vec![]
            } else {
                vec![text.clone()]
            }
        }
        Some(Value::Array(items)) => items
            .iter()
            .filter(|item| !py_strip(&str_or_empty(Some(item))).is_empty())
            .map(py_str)
            .collect(),
        _ => vec![],
    }
}

/// `_normalized_meta`.
fn normalized_meta(value: Option<&Value>) -> Map<String, Value> {
    let Some(meta) = value.and_then(Value::as_object) else {
        return Map::new();
    };
    META_KEYS
        .iter()
        .filter_map(|k| meta.get(*k).map(|v| (k.to_string(), v.clone())))
        .collect()
}

/// `canonical_schema_hash`.
pub fn canonical_schema_hash(schema: &Value) -> String {
    let payload = if schema.is_object() {
        schema.clone()
    } else {
        json!({})
    };
    format!(
        "sha256:{}",
        sha256_hex(pyjson::dumps(&payload, pyjson::COMPACT_SORTED).as_bytes())
    )
}

fn function_of(row: &Map<String, Value>) -> Map<String, Value> {
    let definition = row.get("openai_def").filter(|d| truthy(Some(d)));
    object(
        definition
            .and_then(|d| d.get("function"))
            .filter(|f| truthy(Some(f))),
    )
}

/// `catalog_snapshot_fingerprint`.
pub fn catalog_snapshot_fingerprint(snapshot: &[Value]) -> String {
    let mut rows: Vec<Value> = Vec::new();
    for row in snapshot.iter().filter_map(Value::as_object) {
        let function = function_of(row);
        let meta = normalized_meta(row.get("meta"));
        let schema_hash = if truthy(meta.get("schemaHash")) || truthy(row.get("schema_hash")) {
            first_truthy(&[meta.get("schemaHash"), row.get("schema_hash")])
        } else {
            canonical_schema_hash(&Value::Object(function.clone()))
        };
        rows.push(json!({
            "server": first_truthy(&[row.get("server_id"), row.get("server_name")]),
            "version": first_truthy(&[row.get("catalog_version"), meta.get("catalogVersion")]),
            "name": first_truthy(&[function.get("name"), row.get("namespaced_name")]),
            "schemaHash": schema_hash,
            "meta": meta,
            "description": str_or_empty(function.get("description")),
        }));
    }
    rows.sort_by(|a, b| {
        (a["server"].as_str(), a["name"].as_str()).cmp(&(b["server"].as_str(), b["name"].as_str()))
    });
    sha256_hex(pyjson::dumps(&Value::Array(rows), pyjson::COMPACT_SORTED).as_bytes())
}

/// `catalog_search_text_by_name`.
pub fn catalog_search_text_by_name(snapshot: &[Value]) -> Map<String, Value> {
    let mut out = Map::new();
    for row in snapshot.iter().filter_map(Value::as_object) {
        let function = function_of(row);
        let name = first_truthy(&[function.get("name"), row.get("namespaced_name")]);
        if name.is_empty() {
            continue;
        }
        let meta = object(row.get("meta").filter(|m| truthy(Some(m))));
        let server_id = if row.contains_key("server_id") {
            row.get("server_id")
        } else {
            row.get("server_name")
        };
        let values = [
            server_id,
            row.get("server_name"),
            row.get("tool_name"),
            row.get("description"),
            meta.get("bundle"),
            meta.get("profiles"),
            meta.get("intents"),
            meta.get("aliases"),
        ];
        let text = values
            .iter()
            .map(|value| match value {
                Some(Value::Array(items)) => items.iter().map(py_str).collect::<Vec<_>>().join(" "),
                other => str_or_empty(*other),
            })
            .collect::<Vec<_>>()
            .join(" ");
        // A repeated name keeps its first position, like a dict assignment.
        out.insert(name, Value::String(text));
    }
    out
}

// ── the index ───────────────────────────────────────────────────────────────

/// `CatalogTool`.
#[derive(Clone, Debug)]
pub struct CatalogTool {
    pub name: String,
    pub server_id: String,
    pub short_name: String,
    pub definition: Value,
    pub description: String,
    pub schema_hash: String,
    pub catalog_version: String,
    pub bundle: String,
    pub requires: Vec<String>,
    pub profiles: Vec<String>,
    pub intents: Vec<String>,
    pub aliases: Vec<String>,
    pub risk: String,
}

/// `CatalogIndex`; `id` names the object (the oracle's identity).
#[derive(Debug)]
pub struct CatalogIndex {
    pub id: u64,
    pub fingerprint: String,
    pub tools: Vec<CatalogTool>,
    pub by_name: HashMap<String, usize>,
    pub by_server_short: HashMap<(String, String), String>,
    pub term_postings: HashMap<String, Vec<(String, usize)>>,
    pub fallback_names: Vec<String>,
    pub max_query_boost_chars: usize,
}

impl CatalogIndex {
    fn tool(&self, name: &str) -> Option<&CatalogTool> {
        self.by_name.get(name).map(|&i| &self.tools[i])
    }

    /// The index's observable facts, for the host protocol.
    pub fn describe(&self) -> Value {
        let postings: BTreeMap<&String, Vec<Value>> = self
            .term_postings
            .iter()
            .map(|(term, rows)| (term, rows.iter().map(|(n, f)| json!([n, f])).collect()))
            .collect();
        json!({
            "id": self.id,
            "fingerprint": self.fingerprint,
            "names": self.tools.iter().map(|t| t.name.clone()).collect::<Vec<_>>(),
            "fallback_names": self.fallback_names,
            "max_query_boost_chars": self.max_query_boost_chars,
            "term_postings": postings,
        })
    }
}

/// `_risk_fallback_priority`.
fn risk_fallback_priority(risk: &str) -> u8 {
    let normalized = py_casefold(py_strip(risk));
    if READ_ONLY_RISKS.contains(&normalized.as_str()) {
        0
    } else if normalized.is_empty() {
        1
    } else {
        2
    }
}

fn index_from_snapshot(snapshot: &[Value], fingerprint: String, id: u64) -> CatalogIndex {
    let mut tools: Vec<CatalogTool> = Vec::new();
    let mut by_server_short = HashMap::new();
    let mut posting_rows: HashMap<String, Vec<(String, usize)>> = HashMap::new();
    for row in snapshot.iter().filter_map(Value::as_object) {
        let Some(definition) = row.get("openai_def").filter(|d| d.is_object()) else {
            continue;
        };
        let function = object(definition.get("function").filter(|f| truthy(Some(f))));
        let name = first_truthy(&[function.get("name"), row.get("namespaced_name")]);
        let Some((server_id, short_name)) = parse_namespaced_name(&name) else {
            continue;
        };
        let meta = normalized_meta(row.get("meta"));
        let description = first_truthy(&[function.get("description"), row.get("description")]);
        let schema_hash = if truthy(meta.get("schemaHash")) || truthy(row.get("schema_hash")) {
            first_truthy(&[meta.get("schemaHash"), row.get("schema_hash")])
        } else {
            canonical_schema_hash(&Value::Object(function.clone()))
        };
        let tool = CatalogTool {
            catalog_version: first_truthy(&[
                row.get("catalog_version"),
                meta.get("catalogVersion"),
            ]),
            bundle: str_or_empty(meta.get("bundle")),
            requires: string_list(meta.get("requires")),
            profiles: string_list(meta.get("profiles")),
            intents: string_list(meta.get("intents")),
            aliases: string_list(meta.get("aliases")),
            risk: str_or_empty(meta.get("risk")),
            name: name.clone(),
            server_id: server_id.clone(),
            short_name: short_name.clone(),
            definition: definition.clone(),
            description,
            schema_hash,
        };
        let mut parts = vec![
            name.clone(),
            short_name.clone(),
            server_id.clone(),
            tool.description.clone(),
            tool.bundle.clone(),
            tool.risk.clone(),
        ];
        parts.extend(tool.profiles.iter().cloned());
        parts.extend(tool.intents.iter().cloned());
        parts.extend(tool.aliases.iter().cloned());
        let mut counts: Vec<(String, usize)> = Vec::new();
        let mut seen: HashMap<String, usize> = HashMap::new();
        for term in iter_terms(&parts.join(" ")) {
            match seen.get(&term) {
                Some(&at) => counts[at].1 += 1,
                None => {
                    seen.insert(term.clone(), counts.len());
                    counts.push((term, 1));
                }
            }
        }
        by_server_short.insert((server_id, short_name), name.clone());
        for (term, frequency) in counts {
            posting_rows
                .entry(term)
                .or_default()
                .push((name.clone(), frequency));
        }
        tools.push(tool);
    }
    tools.sort_by(|a, b| a.name.cmp(&b.name));
    let mut fallback: Vec<&CatalogTool> = tools.iter().collect();
    fallback.sort_by(|a, b| {
        (risk_fallback_priority(&a.risk), &a.name).cmp(&(risk_fallback_priority(&b.risk), &b.name))
    });
    let fallback_names = fallback.iter().map(|t| t.name.clone()).collect();
    let max_query_boost_chars = tools
        .iter()
        .flat_map(|tool| {
            [&tool.name, &tool.short_name]
                .into_iter()
                .chain(tool.aliases.iter())
                .chain(tool.intents.iter())
                .map(|value| py_casefold(value).chars().count())
                .collect::<Vec<_>>()
        })
        .max()
        .unwrap_or(0);
    let by_name = tools
        .iter()
        .enumerate()
        .map(|(i, t)| (t.name.clone(), i))
        .collect();
    let term_postings = posting_rows
        .into_iter()
        .map(|(term, mut rows)| {
            rows.sort();
            (term, rows)
        })
        .collect();
    CatalogIndex {
        id,
        fingerprint,
        tools,
        by_name,
        by_server_short,
        term_postings,
        fallback_names,
        max_query_boost_chars,
    }
}

fn resolve_dependency(index: &CatalogIndex, owner: &CatalogTool, raw: &str) -> Option<String> {
    let value = py_strip(raw);
    if value.is_empty() {
        return None;
    }
    if index.by_name.contains_key(value) {
        return Some(value.to_string());
    }
    index
        .by_server_short
        .get(&(owner.server_id.clone(), value.to_string()))
        .cloned()
}

fn dedupe_known(index: &CatalogIndex, names: &[String]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for name in names {
        if index.by_name.contains_key(name) && !out.contains(name) {
            out.push(name.clone());
        }
    }
    out
}

/// `_expand_dependencies`.
fn expand_dependencies(index: &CatalogIndex, names: &[String]) -> Vec<String> {
    fn visit(
        index: &CatalogIndex,
        out: &mut Vec<String>,
        visiting: &mut HashSet<String>,
        name: &str,
        depth: usize,
    ) {
        if depth > MAX_DEPENDENCY_DEPTH || visiting.contains(name) {
            return;
        }
        visiting.insert(name.to_string());
        let Some(tool) = index.tool(name) else {
            return;
        };
        for raw in &tool.requires {
            let Some(dep) = resolve_dependency(index, tool, raw) else {
                continue;
            };
            if visiting.contains(&dep) {
                continue;
            }
            let owner_position = out.iter().position(|n| n == name).unwrap_or(out.len());
            match out.iter().position(|n| *n == dep) {
                Some(dependency_position) => {
                    if dependency_position > owner_position {
                        out.remove(dependency_position);
                        out.insert(owner_position, dep.clone());
                    }
                }
                None => out.insert(owner_position, dep.clone()),
            }
            visit(index, out, visiting, &dep, depth + 1);
        }
        visiting.remove(name);
    }
    let mut out = dedupe_known(index, names);
    let mut visiting = HashSet::new();
    for current in out.clone() {
        visit(index, &mut out, &mut visiting, &current, 0);
    }
    out
}

/// `_expand_bundles`.
fn expand_bundles(index: &CatalogIndex, names: &[String], limit: usize) -> Vec<String> {
    let mut out = dedupe_known(index, names);
    let mut bundles: Vec<String> = Vec::new();
    for name in &out {
        let bundle = &index
            .tool(name)
            .map(|t| t.bundle.clone())
            .unwrap_or_default();
        if !bundle.is_empty() && !bundles.contains(bundle) {
            bundles.push(bundle.clone());
        }
    }
    for bundle in &bundles {
        for tool in &index.tools {
            if out.len() >= limit {
                return out;
            }
            if &tool.bundle == bundle && !out.contains(&tool.name) {
                out.push(tool.name.clone());
            }
        }
    }
    out
}

/// `_apply_query_phrase_boosts`.
fn apply_query_phrase_boosts(
    index: &CatalogIndex,
    scores: &mut HashMap<String, f64>,
    qtext: &str,
    query_has_terms: bool,
) {
    let sparse = query_has_terms && scores.len() * 8 < index.tools.len() * 7;
    let candidates: Vec<&CatalogTool> = if sparse {
        scores.keys().filter_map(|n| index.tool(n)).collect()
    } else {
        index.tools.iter().collect()
    };
    for tool in candidates {
        let mut score = scores.get(&tool.name).copied().unwrap_or(0.0);
        if qtext == py_casefold(&tool.short_name) || qtext == py_casefold(&tool.name) {
            score += 1000.0;
        } else if tool.aliases.iter().any(|a| qtext == py_casefold(a)) {
            score += 500.0;
        } else if tool.intents.iter().any(|i| py_casefold(i).contains(qtext)) {
            score += 100.0;
        }
        if score > 0.0 {
            scores.insert(tool.name.clone(), score);
        }
    }
}

// ── selection state ─────────────────────────────────────────────────────────

/// One scope's bounded state (`_SELECTION_STATE[key]`).
#[derive(Clone, Debug, Default)]
pub struct SelectionState {
    pub used: Vec<String>,
    used_set: HashSet<String>,
    pub touched: f64,
    pub wire: Option<Vec<Value>>,
}

impl SelectionState {
    /// `_record_used_name`.
    fn record(&mut self, name: &str) {
        if self.used_set.insert(name.to_string()) {
            self.used.push(name.to_string());
        }
    }
}

/// An insertion-ordered map with move-to-end (`collections.OrderedDict`).
struct Ordered<V> {
    next: u64,
    order: BTreeMap<u64, String>,
    entries: HashMap<String, (u64, V)>,
}

impl<V> Default for Ordered<V> {
    fn default() -> Self {
        Ordered {
            next: 0,
            order: BTreeMap::new(),
            entries: HashMap::new(),
        }
    }
}

impl<V> Ordered<V> {
    fn len(&self) -> usize {
        self.entries.len()
    }
    fn get(&self, key: &str) -> Option<&V> {
        self.entries.get(key).map(|(_, v)| v)
    }
    fn pop(&mut self, key: &str) -> Option<V> {
        let (seq, value) = self.entries.remove(key)?;
        self.order.remove(&seq);
        Some(value)
    }
    fn insert_last(&mut self, key: String, value: V) {
        self.pop(&key);
        self.next += 1;
        self.order.insert(self.next, key.clone());
        self.entries.insert(key, (self.next, value));
    }
    fn move_to_end(&mut self, key: &str) {
        if let Some(value) = self.pop(key) {
            self.insert_last(key.to_string(), value);
        }
    }
    fn first(&self) -> Option<(&String, &V)> {
        let (_, key) = self.order.iter().next()?;
        self.entries.get(key).map(|(_, v)| (key, v))
    }
    fn pop_first(&mut self) -> Option<(String, V)> {
        let (_, key) = self.order.pop_first()?;
        self.entries.remove(&key).map(|(_, v)| (key, v))
    }
    fn keys(&self) -> impl Iterator<Item = &String> {
        self.order.values()
    }
    fn clear(&mut self) {
        self.order.clear();
        self.entries.clear();
    }
}

#[derive(Default)]
struct Inner {
    indexes: Ordered<Arc<CatalogIndex>>,
    states: Ordered<SelectionState>,
    index_evictions: u64,
    state_expirations: u64,
}

/// The process's catalog indexes and selection state.
#[derive(Default)]
pub struct ToolSearch {
    inner: Mutex<Inner>,
    next_index_id: AtomicU64,
}

/// The process-wide instance (the oracle's module globals).
pub fn global() -> &'static ToolSearch {
    static GLOBAL: OnceLock<ToolSearch> = OnceLock::new();
    GLOBAL.get_or_init(ToolSearch::default)
}

/// `time.monotonic()` of this process.
pub fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// `_bounded_state_key`.
pub fn bounded_state_key(value: &str) -> String {
    if value.chars().count() <= MAX_INLINE_STATE_KEY_CHARS {
        return value.to_string();
    }
    format!("sha256:{}", sha256_hex(value.as_bytes()))
}

/// `mcp_selection_scope_id`.
pub fn mcp_selection_scope_id(
    task_id: &str,
    conv_id: &str,
    owner_user_id: i64,
    storage_free: bool,
) -> String {
    let conversation = if storage_free { "" } else { py_strip(conv_id) };
    if !conversation.is_empty() {
        return format!("conversation:{owner_user_id}:{conversation}");
    }
    let task = if task_id.is_empty() {
        "anonymous"
    } else {
        task_id
    };
    format!("task:{task}")
}

fn declaration_error(message: &str) -> CallError {
    CallError::Error(format!("MCPDeclarationError: {message}"))
}

/// `lib/mcp/declarations.py` `validate_definitions`.
pub fn validate_definitions(definitions: &Value) -> Result<Vec<Value>, CallError> {
    let Some(list) = definitions
        .as_array()
        .filter(|l| l.len() <= MAX_DECLARATIONS)
    else {
        return Err(declaration_error(
            "MCP declaration count exceeds the bounded slot",
        ));
    };
    let mut names = HashSet::new();
    for tool in list {
        let name = tool
            .get("function")
            .filter(|f| f.is_object())
            .and_then(|f| f.get("name"))
            .and_then(Value::as_str);
        let valid = tool.is_object()
            && tool.get("type") == Some(&json!("function"))
            && name.is_some_and(|n| n.starts_with("mcp__") && !names.contains(n));
        if !valid {
            return Err(declaration_error("Malformed or duplicate MCP declaration"));
        }
        names.insert(name.unwrap_or_default());
    }
    let compact = pyjson::Style {
        spaced: false,
        sorted: false,
        ascii: false,
    };
    if pyjson::dumps(definitions, compact).len() > MAX_DECLARATION_BYTES {
        return Err(declaration_error(
            "MCP declaration bytes exceed the bounded slot",
        ));
    }
    Ok(list.clone())
}

/// `_conversation_identity`: `Some((owner, conversation))` for a conversation scope.
fn conversation_identity(scope: &str) -> Result<Option<(i64, String)>, CallError> {
    let Some(rest) = scope.strip_prefix("conversation:") else {
        return Ok(None);
    };
    let mut parts = rest.splitn(2, ':');
    let owner = parts.next().unwrap_or_default();
    let Some(conversation) = parts.next() else {
        return Err(declaration_error("Malformed MCP conversation scope"));
    };
    let owner = py_int(&Value::String(owner.to_string()))
        .ok_or_else(|| declaration_error("Malformed MCP conversation scope"))?;
    Ok(Some((owner, conversation.to_string())))
}

fn wire_or_none(value: Value) -> Result<Option<Vec<Value>>, CallError> {
    match value {
        Value::Null => Ok(None),
        Value::Array(items) => Ok(Some(items)),
        other => Err(CallError::Error(format!(
            "the declaration store answered a non-list wire: {}",
            py_str(&other)
        ))),
    }
}

impl ToolSearch {
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// `build_catalog_index`: content addressed; unchanged catalogs reuse the object.
    pub async fn build_catalog_index(
        &self,
        binding: &dyn BindingCalls,
        snapshot: &[Value],
        fingerprint_hint: &str,
    ) -> Result<Arc<CatalogIndex>, CallError> {
        let hinted = py_lower(py_strip(fingerprint_hint));
        let fingerprint = if hinted.len() == 64
            && hinted
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            hinted
        } else {
            catalog_snapshot_fingerprint(snapshot)
        };
        {
            let mut inner = self.lock();
            if let Some(index) = inner.indexes.get(&fingerprint).cloned() {
                inner.indexes.move_to_end(&fingerprint);
                return Ok(index);
            }
        }
        let id = self.next_index_id.fetch_add(1, Ordering::Relaxed) + 1;
        let index = Arc::new(index_from_snapshot(snapshot, fingerprint.clone(), id));
        let capacity = binding.ask("tool_search.index_capacity", json!({})).await?;
        let capacity = py_int(&capacity).ok_or_else(|| {
            CallError::Error(format!(
                "tool_search.index_capacity answered {}",
                py_str(&capacity)
            ))
        })?;
        let mut inner = self.lock();
        if let Some(index) = inner.indexes.get(&fingerprint).cloned() {
            inner.indexes.move_to_end(&fingerprint);
            return Ok(index);
        }
        inner.indexes.insert_last(fingerprint, index.clone());
        let capacity = capacity.max(1) as usize;
        while inner.indexes.len() > capacity {
            inner.indexes.pop_first();
            inner.index_evictions += 1;
        }
        Ok(index)
    }

    /// `invalidate_server_catalog`.
    pub fn invalidate_server_catalog(&self, server_id: &str) {
        let mut inner = self.lock();
        let doomed: Vec<String> = inner
            .indexes
            .keys()
            .filter(|key| {
                inner
                    .indexes
                    .get(key)
                    .is_some_and(|index| index.tools.iter().any(|t| t.server_id == server_id))
            })
            .cloned()
            .collect();
        for key in doomed {
            inner.indexes.pop(&key);
        }
    }

    /// `_prune_states`: expiry is an ordered-prefix operation.
    fn prune_locked(inner: &mut Inner, now: f64) {
        while let Some((_, oldest)) = inner.states.first() {
            if now - oldest.touched <= STATE_TTL_SECONDS {
                break;
            }
            inner.states.pop_first();
            inner.state_expirations += 1;
        }
    }

    /// `_state_for_update_locked`: prune, then touch (and move to the end).
    fn touch_locked<'a>(inner: &'a mut Inner, key: &str, now: f64) -> &'a mut SelectionState {
        Self::prune_locked(inner, now);
        let mut state = inner.states.pop(key).unwrap_or_default();
        state.touched = now;
        inner.states.insert_last(key.to_string(), state);
        &mut inner.states.entries.get_mut(key).expect("just inserted").1
    }

    /// Run `_prune_states(now)` (a deterministic test seam for the TTL).
    pub fn prune(&self, now: f64) {
        Self::prune_locked(&mut self.lock(), now);
    }

    /// `record_mcp_tool_used`.
    pub fn record_mcp_tool_used(
        &self,
        task_id: &str,
        namespaced_name: &str,
        selection_scope_id: &str,
        now: Option<f64>,
    ) {
        let key = bounded_state_key(if selection_scope_id.is_empty() {
            task_id
        } else {
            selection_scope_id
        });
        if key.is_empty() || parse_namespaced_name(namespaced_name).is_none() {
            return;
        }
        let now = now.unwrap_or_else(monotonic);
        let mut inner = self.lock();
        Self::touch_locked(&mut inner, &key, now).record(namespaced_name);
    }

    /// `freeze_wire_definitions`.
    pub async fn freeze_wire_definitions(
        &self,
        binding: &dyn BindingCalls,
        selection_scope_id: &str,
        definitions: &Value,
        now: Option<f64>,
    ) -> Result<Vec<Value>, CallError> {
        if let Some((user_id, conversation_id)) = conversation_identity(selection_scope_id)? {
            let wire = binding
                .ask(
                    "mcp.declarations_freeze",
                    json!({"definitions": definitions, "user_id": user_id,
                           "conversation_id": conversation_id}),
                )
                .await?;
            return wire_or_none(wire).map(Option::unwrap_or_default);
        }
        let definitions = validate_definitions(definitions)?;
        let key = bounded_state_key(selection_scope_id);
        if key.is_empty() {
            return Ok(definitions);
        }
        let now = now.unwrap_or_else(monotonic);
        let mut inner = self.lock();
        let state = Self::touch_locked(&mut inner, &key, now);
        Ok(state.wire.get_or_insert(definitions).clone())
    }

    /// `frozen_wire_definitions`: read-only; never creates or touches state.
    pub async fn frozen_wire_definitions(
        &self,
        binding: &dyn BindingCalls,
        selection_scope_id: &str,
    ) -> Result<Option<Vec<Value>>, CallError> {
        if let Some((user_id, conversation_id)) = conversation_identity(selection_scope_id)? {
            let wire = binding
                .ask(
                    "mcp.declarations_get",
                    json!({"user_id": user_id, "conversation_id": conversation_id}),
                )
                .await?;
            return wire_or_none(wire);
        }
        let key = bounded_state_key(selection_scope_id);
        if key.is_empty() {
            return Ok(None);
        }
        Ok(self.lock().states.get(&key).and_then(|s| s.wire.clone()))
    }

    /// `frozen_wire_tool_names`.
    pub async fn frozen_wire_tool_names(
        &self,
        binding: &dyn BindingCalls,
        selection_scope_id: &str,
    ) -> Result<(Vec<String>, bool), CallError> {
        let Some(wire) = self
            .frozen_wire_definitions(binding, selection_scope_id)
            .await?
        else {
            return Ok((vec![], false));
        };
        let names = wire
            .iter()
            .map(|tool| str_or_empty(tool.get("function").and_then(|f| f.get("name"))))
            .filter(|name| !name.is_empty())
            .collect();
        Ok((names, true))
    }

    /// `select_active_mcp_tools`.
    #[allow(clippy::too_many_arguments)]
    pub async fn select_active_mcp_tools(
        &self,
        binding: &dyn BindingCalls,
        snapshot: &[Value],
        task_id: &str,
        query: &str,
        used_names: &[String],
        limit: &Value,
        selection_scope_id: &str,
        catalog_fingerprint: &str,
        now: Option<f64>,
    ) -> Result<Vec<Value>, CallError> {
        let scope = if selection_scope_id.is_empty() {
            task_id
        } else {
            selection_scope_id
        };
        if let Some(frozen) = self.frozen_wire_definitions(binding, scope).await? {
            return Ok(frozen);
        }
        let index = self
            .build_catalog_index(binding, snapshot, catalog_fingerprint)
            .await?;
        let now = now.unwrap_or_else(monotonic);
        let key = bounded_state_key(if !scope.is_empty() {
            scope
        } else {
            &index.fingerprint
        });
        let retained_used: Vec<String> = {
            let mut inner = self.lock();
            let state = Self::touch_locked(&mut inner, &key, now);
            for name in used_names {
                if index.by_name.contains_key(name) {
                    state.record(name);
                }
            }
            state
                .used
                .iter()
                .filter(|n| index.by_name.contains_key(*n))
                .cloned()
                .collect()
        };
        let freeze_scope = if scope.is_empty() {
            key.clone()
        } else {
            scope.to_string()
        };
        let limit = if truthy(Some(limit)) {
            py_int(limit).ok_or_else(|| {
                CallError::Invalid(format!(
                    "MCP selection limit is not an integer: {}",
                    py_str(limit)
                ))
            })?
        } else {
            DEFAULT_ACTIVE_LIMIT
        };
        if index.tools.len() as i64 <= limit.max(1) {
            let all: Vec<Value> = index.tools.iter().map(|t| t.definition.clone()).collect();
            return self
                .freeze_wire_definitions(binding, &freeze_scope, &Value::Array(all), Some(now))
                .await;
        }
        let wanted = limit.clamp(1, 8) as usize;
        let (query_terms, _digest) = query_profile(query);
        let mut scores: HashMap<String, f64> = HashMap::new();
        for (term, query_frequency) in &query_terms {
            let Some(postings) = index.term_postings.get(term).filter(|p| !p.is_empty()) else {
                continue;
            };
            let weight = 1.0 + index.tools.len() as f64 / postings.len() as f64;
            for (name, tool_frequency) in postings {
                *scores.entry(name.clone()).or_insert(0.0) +=
                    *query_frequency as f64 * *tool_frequency as f64 * weight;
            }
        }
        let qtext = py_strip(&py_casefold(query)).to_string();
        if !qtext.is_empty() && qtext.chars().count() <= index.max_query_boost_chars {
            apply_query_phrase_boosts(&index, &mut scores, &qtext, !query_terms.is_empty());
        }
        let mut ranked: Vec<(f64, String)> = scores
            .into_iter()
            .filter(|(_, score)| *score > 0.0)
            .map(|(name, score)| (-score, name))
            .collect();
        ranked.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
        let mut selected: Vec<String> = Vec::new();
        for name in &retained_used {
            if !selected.contains(name) {
                selected.push(name.clone());
            }
        }
        for (_, name) in ranked {
            if !selected.contains(&name) {
                selected.push(name);
            }
            if selected.len() >= wanted {
                break;
            }
        }
        if selected.is_empty() {
            selected = index
                .fallback_names
                .iter()
                .take(wanted.min(4))
                .cloned()
                .collect();
        }
        let cap = wanted.max(retained_used.len());
        selected.truncate(cap);
        let selected = expand_bundles(&index, &selected, cap);
        let selected = expand_dependencies(&index, &selected);
        let definitions: Vec<Value> = selected
            .iter()
            .filter_map(|n| index.tool(n).map(|t| t.definition.clone()))
            .collect();
        self.freeze_wire_definitions(
            binding,
            &freeze_scope,
            &Value::Array(definitions),
            Some(now),
        )
        .await
    }

    /// `release_transient_task_selection`.
    pub fn release_transient_task_selection(&self, task: &Value) -> Result<bool, CallError> {
        let owner = task.get("_userId").and_then(Value::as_i64);
        let id = task.get("id").and_then(Value::as_str).unwrap_or_default();
        let owned = task.get("_transientRuntime") == Some(&Value::Bool(true))
            && task
                .get("_userId")
                .is_some_and(|v| v.is_i64() || v.is_u64())
            && owner.is_some_and(|o| o > 0)
            && !id.is_empty();
        if !owned {
            return Err(CallError::Invalid(
                "MCP task selector disposal requires owned transient identity".into(),
            ));
        }
        let scope = mcp_selection_scope_id(id, "", owner.unwrap_or_default(), true);
        Ok(self.lock().states.pop(&bounded_state_key(&scope)).is_some())
    }

    /// `catalog_cache_stats` (the capacity is the binding's).
    pub async fn catalog_cache_stats(
        &self,
        binding: &dyn BindingCalls,
    ) -> Result<Value, CallError> {
        let capacity = binding.ask("tool_search.index_capacity", json!({})).await?;
        let inner = self.lock();
        Ok(json!({
            "indexes": inner.indexes.len(),
            "indexCapacity": py_int(&capacity).unwrap_or(1).max(1),
            "tasks": inner.states.len(),
            "indexEvictions": inner.index_evictions,
            "stateExpirations": inner.state_expirations,
        }))
    }

    /// `reclaim_tool_search_indexes`.
    pub fn reclaim_tool_search_indexes(&self) -> usize {
        let mut inner = self.lock();
        let dropped = inner.indexes.len();
        inner.indexes.clear();
        dropped
    }

    /// `clear_tool_search_caches`.
    pub fn clear_tool_search_caches(&self) -> usize {
        let mut inner = self.lock();
        let dropped = inner.indexes.len() + inner.states.len();
        inner.indexes.clear();
        inner.states.clear();
        dropped
    }

    /// The selection states in order (keys, history, touch time, wire presence).
    pub fn states(&self) -> Vec<Value> {
        let inner = self.lock();
        inner
            .states
            .keys()
            .filter_map(|key| {
                inner.states.get(key).map(|s| {
                    json!({"key": key, "used": s.used, "touched": s.touched,
                           "wire": s.wire.is_some()})
                })
            })
            .collect()
    }
}

/// `_mcp_name_from_call`.
fn mcp_name_from_call(value: &Value) -> String {
    let Some(map) = value.as_object() else {
        return String::new();
    };
    let candidate = match map.get("function") {
        Some(function) if function.is_object() => function.get("name").cloned(),
        _ => ["toolName", "tool_name", "name"]
            .iter()
            .find_map(|k| map.get(*k).filter(|v| truthy(Some(v))).cloned()),
    };
    let name = str_or_empty(candidate.as_ref());
    if parse_namespaced_name(&name).is_some() {
        name
    } else {
        String::new()
    }
}

/// `recent_conversation_mcp_tool_names`.
pub fn recent_conversation_mcp_tool_names(
    messages: &Value,
    limit: &Value,
) -> Result<Vec<String>, CallError> {
    let rows: &[Value] = messages.as_array().map_or(&[], Vec::as_slice);
    let users: Vec<usize> = rows
        .iter()
        .enumerate()
        .filter(|(_, m)| m.get("role") == Some(&json!("user")) && m.is_object())
        .map(|(i, _)| i)
        .collect();
    if users.len() < 2 {
        return Ok(vec![]);
    }
    let (start, stop) = (users[users.len() - 2] + 1, users[users.len() - 1]);
    let limit = if truthy(Some(limit)) {
        py_int(limit).ok_or_else(|| CallError::Invalid("limit must be an integer".into()))?
    } else {
        DEFAULT_ACTIVE_LIMIT
    };
    let wanted = limit.clamp(1, 8) as usize;
    let mut names: Vec<String> = Vec::new();
    let mut add = |call: &Value| {
        let name = mcp_name_from_call(call);
        if !name.is_empty() && !names.contains(&name) && names.len() < wanted {
            names.push(name);
        }
    };
    let list = |value: Option<&Value>| -> Vec<Value> {
        match value {
            Some(v) if truthy(Some(v)) => v.as_array().cloned().unwrap_or_default(),
            _ => vec![],
        }
    };
    for message in &rows[start..stop] {
        if !message.is_object() {
            continue;
        }
        for call in list(message.get("toolRounds")) {
            add(&call);
        }
        for call in list(message.get("tool_calls")) {
            add(&call);
        }
        for api_round in list(message.get("apiRounds")) {
            if api_round.is_object() {
                for call in list(api_round.get("toolCalls")) {
                    add(&call);
                }
            }
        }
        if let Some(Value::Array(blocks)) = message.get("content") {
            for block in blocks {
                if matches!(
                    block.get("type").and_then(Value::as_str),
                    Some("tool_use" | "function_call")
                ) {
                    add(block);
                }
            }
        }
    }
    Ok(names)
}

fn text_param(params: &Value, key: &str) -> String {
    params
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn now_param(params: &Value) -> Option<f64> {
    params.get("now").and_then(Value::as_f64)
}

fn snapshot_param(params: &Value) -> Vec<Value> {
    params
        .get("snapshot")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

/// The `tool_search.*` host methods. `bridge` supplies an owner bridge's
/// generation projection when a request names no explicit snapshot.
pub async fn host_call(
    binding: &dyn BindingCalls,
    bridge: Option<Arc<crate::bridge::Bridge>>,
    method: &str,
    params: &Value,
) -> Result<Value, CallError> {
    let search = global();
    match method {
        "tool_search.canonical_schema_hash" => Ok(json!(canonical_schema_hash(
            params.get("schema").unwrap_or(&Value::Null)
        ))),
        "tool_search.fingerprint" => {
            Ok(json!(catalog_snapshot_fingerprint(&snapshot_param(params))))
        }
        "tool_search.search_text" => Ok(Value::Object(catalog_search_text_by_name(
            &snapshot_param(params),
        ))),
        "tool_search.terms" => {
            let (counts, digest) = query_profile(&text_param(params, "text"));
            Ok(
                json!({"terms": iter_terms(&text_param(params, "text")), "counts": counts, "digest": digest}),
            )
        }
        "tool_search.build_index" => Ok(search
            .build_catalog_index(
                binding,
                &snapshot_param(params),
                &text_param(params, "fingerprint_hint"),
            )
            .await?
            .describe()),
        "tool_search.invalidate_server" => {
            search.invalidate_server_catalog(&text_param(params, "server_id"));
            Ok(Value::Null)
        }
        "tool_search.record_used" => {
            search.record_mcp_tool_used(
                &text_param(params, "task_id"),
                &text_param(params, "name"),
                &text_param(params, "selection_scope_id"),
                now_param(params),
            );
            Ok(Value::Null)
        }
        "tool_search.freeze" => Ok(Value::Array(
            search
                .freeze_wire_definitions(
                    binding,
                    &text_param(params, "selection_scope_id"),
                    params.get("definitions").unwrap_or(&Value::Null),
                    now_param(params),
                )
                .await?,
        )),
        "tool_search.frozen_wire" => Ok(search
            .frozen_wire_definitions(binding, &text_param(params, "selection_scope_id"))
            .await?
            .map_or(Value::Null, Value::Array)),
        "tool_search.frozen_names" => {
            let (names, frozen) = search
                .frozen_wire_tool_names(binding, &text_param(params, "selection_scope_id"))
                .await?;
            Ok(json!([names, frozen]))
        }
        "tool_search.select" => {
            let (fingerprint, snapshot) = match (params.get("snapshot"), bridge) {
                (None | Some(Value::Null), Some(bridge)) => bridge.get_tool_catalog_projection(),
                _ => (
                    text_param(params, "catalog_fingerprint"),
                    snapshot_param(params),
                ),
            };
            let used: Vec<String> = params
                .get("used_names")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(py_str)
                .collect();
            Ok(Value::Array(
                search
                    .select_active_mcp_tools(
                        binding,
                        &snapshot,
                        &text_param(params, "task_id"),
                        &text_param(params, "query"),
                        &used,
                        params.get("limit").unwrap_or(&json!(DEFAULT_ACTIVE_LIMIT)),
                        &text_param(params, "selection_scope_id"),
                        &fingerprint,
                        now_param(params),
                    )
                    .await?,
            ))
        }
        "tool_search.recent_names" => Ok(json!(recent_conversation_mcp_tool_names(
            params.get("messages").unwrap_or(&Value::Null),
            params.get("limit").unwrap_or(&json!(DEFAULT_ACTIVE_LIMIT)),
        )?)),
        "tool_search.scope_id" => {
            let storage_free = match params.get("storage_free") {
                None => false,
                Some(Value::Bool(flag)) => *flag,
                Some(_) => {
                    return Err(CallError::Invalid(
                        "MCP selector lifetime must be explicit".into(),
                    ))
                }
            };
            let owner = params.get("owner").filter(|v| truthy(Some(v)));
            let owner = match owner {
                Some(value) => py_int(value)
                    .ok_or_else(|| CallError::Invalid("owner_user_id must be an integer".into()))?,
                None => 0,
            };
            Ok(json!(mcp_selection_scope_id(
                &str_or_empty(params.get("task_id")),
                &str_or_empty(params.get("conv_id")),
                owner,
                storage_free,
            )))
        }
        "tool_search.release_transient" => Ok(json!(
            search.release_transient_task_selection(params.get("task").unwrap_or(&Value::Null))?
        )),
        "tool_search.validate_definitions" => Ok(Value::Array(validate_definitions(
            params.get("definitions").unwrap_or(&Value::Null),
        )?)),
        "tool_search.stats" => search.catalog_cache_stats(binding).await,
        "tool_search.reclaim" => Ok(json!(search.reclaim_tool_search_indexes())),
        "tool_search.clear" => Ok(json!(search.clear_tool_search_caches())),
        "tool_search.prune" => {
            search.prune(now_param(params).unwrap_or_else(monotonic));
            Ok(Value::Null)
        }
        "tool_search.states" => Ok(Value::Array(search.states())),
        "tool_search.monotonic" => Ok(json!(monotonic())),
        _ => Err(CallError::Invalid(format!("unknown host method {method}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;

    struct Capacity(i64);

    #[async_trait]
    impl BindingCalls for Capacity {
        async fn ask(&self, method: &str, _params: Value) -> Result<Value, CallError> {
            match method {
                "tool_search.index_capacity" => Ok(json!(self.0)),
                _ => Err(CallError::Unavailable(method.into())),
            }
        }
    }

    fn row(server: &str, name: &str, description: &str, meta: Value) -> Value {
        json!({"server_id": server, "tool_name": name, "namespaced_name": format!("mcp__{server}__{name}"),
               "openai_def": {"type": "function", "function": {"name": format!("mcp__{server}__{name}"),
               "description": format!("[MCP:{server}] {description}"), "parameters": {"type": "object", "properties": {}}}},
               "meta": meta, "catalog_version": "v1"})
    }

    fn block_on<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("runtime")
            .block_on(future)
    }

    #[test]
    fn cpython_text_semantics() {
        assert_eq!(py_casefold("Straße ﬁ K"), "strasse fi k");
        assert_eq!(py_lower("ΣΑΣ Σ"), "σας σ");
        assert_eq!(
            iter_terms("Hope_Search 请编辑"),
            ["hope_search", "hope", "search", "请", "编", "辑"]
        );
        assert_eq!(py_int(&json!(" -7_0 ")), Some(-70));
        assert_eq!(py_int(&json!("7__0")), None);
        assert!(py_word_char('²') && !py_word_char('\u{301}'));
    }

    #[test]
    fn prune_reads_only_the_expired_prefix() {
        let search = ToolSearch::default();
        let now = 100_000.0;
        for (key, touched) in [
            ("a", now - STATE_TTL_SECONDS - 2.0),
            ("b", now - STATE_TTL_SECONDS - 1.0),
            ("c", now - STATE_TTL_SECONDS),
            ("d", now),
        ] {
            search.record_mcp_tool_used(key, "mcp__docs__read", "", Some(touched));
        }
        search.prune(now);
        let keys: Vec<Value> = search.states().iter().map(|s| s["key"].clone()).collect();
        assert_eq!(keys, [json!("c"), json!("d")]);
        assert_eq!(search.lock().state_expirations, 2);
    }

    #[test]
    fn index_cache_is_lru_bounded_and_content_addressed() {
        let search = ToolSearch::default();
        let binding = Capacity(2);
        let rows = |version: &str| {
            let mut r = row("hope", "search", "search", json!({}));
            r["catalog_version"] = json!(version);
            vec![r]
        };
        block_on(async {
            let first = search
                .build_catalog_index(&binding, &rows("v1"), "")
                .await
                .unwrap();
            let second = search
                .build_catalog_index(&binding, &rows("v2"), "")
                .await
                .unwrap();
            assert!(Arc::ptr_eq(
                &first,
                &search
                    .build_catalog_index(&binding, &rows("v1"), "")
                    .await
                    .unwrap()
            ));
            search
                .build_catalog_index(&binding, &rows("v3"), "")
                .await
                .unwrap();
            assert_eq!(search.lock().indexes.len(), 2);
            assert!(!Arc::ptr_eq(
                &second,
                &search
                    .build_catalog_index(&binding, &rows("v2"), "")
                    .await
                    .unwrap()
            ));
        });
    }

    #[test]
    fn dependency_precedes_owner_and_wire_freezes() {
        let search = ToolSearch::default();
        let binding = Capacity(4);
        let mut snapshot = vec![
            row("x", "prepare", "load source", json!({"bundle": "edit"})),
            row(
                "x",
                "update",
                "update document",
                json!({"bundle": "edit", "requires": ["prepare"], "intents": ["编辑文档"]}),
            ),
        ];
        snapshot.extend((0..8).map(|i| row("hope", &format!("t{i}"), "unrelated", json!({}))));
        block_on(async {
            let wire = search
                .select_active_mcp_tools(
                    &binding,
                    &snapshot,
                    "t",
                    "编辑文档",
                    &[],
                    &json!(4),
                    "",
                    "",
                    Some(1.0),
                )
                .await
                .unwrap();
            let names: Vec<&str> = wire
                .iter()
                .filter_map(|d| d["function"]["name"].as_str())
                .collect();
            let at = |n: &str| names.iter().position(|x| *x == n).unwrap();
            assert!(at("mcp__x__prepare") < at("mcp__x__update"));
            let again = search
                .select_active_mcp_tools(
                    &binding,
                    &snapshot,
                    "t",
                    "hope",
                    &[],
                    &json!(4),
                    "",
                    "",
                    Some(2.0),
                )
                .await
                .unwrap();
            assert_eq!(again, wire);
        });
    }
}
