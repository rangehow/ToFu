//! Usage normalisation: `lib/cost.py` (`normalize_usage`,
//! `canonicalize_usage_cache_keys`, `usage_cache_convention`,
//! `split_input_tokens`) and the provider usage converters of the Anthropic
//! (`_convert_usage`) and Responses (`_usage_to_openai`) translators.

use serde_json::{json, Map, Value};

use super::errors::{unsupported, Flow};
use super::py::{self, truthy, IntError};

const CANONICAL: [&str; 5] = ["input", "output", "cache_write", "cache_read", "thinking"];
const ALIASES: [&[&str]; 5] = [
    &["prompt_tokens", "input_tokens"],
    &["completion_tokens", "output_tokens"],
    &["cache_write_tokens", "cache_creation_input_tokens"],
    &[
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "effectiveCachedTokens",
        "prompt_cache_hit_tokens",
        "cached_content_token_count",
    ],
    &["reasoning_tokens", "thinking_tokens"],
];

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Normalized {
    pub input: i64,
    pub output: i64,
    pub cache_write: i64,
    pub cache_read: i64,
    pub thinking: i64,
}

impl Normalized {
    fn set(&mut self, index: usize, value: i64) {
        *[
            &mut self.input,
            &mut self.output,
            &mut self.cache_write,
            &mut self.cache_read,
            &mut self.thinking,
        ][index] = value;
    }

    fn get(&self, index: usize) -> i64 {
        [
            self.input,
            self.output,
            self.cache_write,
            self.cache_read,
            self.thinking,
        ][index]
    }

    pub fn to_json(self) -> Value {
        let mut out = Map::new();
        for (index, name) in CANONICAL.iter().enumerate() {
            out.insert((*name).into(), json!(self.get(index)));
        }
        Value::Object(out)
    }
}

/// `normalize_usage`.
pub fn normalize_usage(usage: &Map<String, Value>) -> Flow<Normalized> {
    let mut out = Normalized::default();
    if usage.is_empty() {
        return Ok(out);
    }
    for (index, keys) in ALIASES.iter().enumerate() {
        let value = keys
            .iter()
            .filter_map(|key| usage.get(*key))
            .find(|v| truthy(Some(v)));
        if let Some(value) = value {
            out.set(index, py::int_or_none(value)?.unwrap_or(0));
        }
    }
    for (index, parent, child) in [
        (3, "prompt_tokens_details", "cached_tokens"),
        (4, "completion_tokens_details", "reasoning_tokens"),
    ] {
        if out.get(index) != 0 {
            continue;
        }
        if let Some(value) = usage
            .get(parent)
            .and_then(Value::as_object)
            .and_then(|details| details.get(child))
            .filter(|v| truthy(Some(v)))
        {
            if let Some(number) = py::int_or_none(value)? {
                out.set(index, number);
            }
        }
    }
    Ok(out)
}

/// `canonicalize_usage_cache_keys` (in place).
pub fn canonicalize_usage_cache_keys(usage: &mut Map<String, Value>) -> Flow<()> {
    let normalized = normalize_usage(usage)?;
    if normalized.cache_read != 0
        && !(truthy(usage.get("cache_read_tokens")) || truthy(usage.get("cache_read_input_tokens")))
    {
        usage.insert("cache_read_tokens".into(), json!(normalized.cache_read));
    }
    if normalized.cache_write != 0
        && !(truthy(usage.get("cache_write_tokens"))
            || truthy(usage.get("cache_creation_input_tokens")))
    {
        usage.insert("cache_write_tokens".into(), json!(normalized.cache_write));
    }
    Ok(())
}

fn present(usage: &Map<String, Value>, key: &str) -> bool {
    !matches!(usage.get(key), None | Some(Value::Null))
}

/// `int(value or 0)` outside any `try`.
fn strict_int(value: Option<&Value>) -> Flow<i64> {
    match value.filter(|v| truthy(Some(v))) {
        None => Ok(0),
        Some(value) => match py::int(value)? {
            Ok(number) => Ok(number),
            Err(_) => unsupported("int() raises out of usage_cache_convention"),
        },
    }
}

fn nested_cached(usage: &Map<String, Value>) -> Flow<i64> {
    for key in [
        "cached_tokens",
        "effectiveCachedTokens",
        "prompt_cache_hit_tokens",
        "cached_content_token_count",
    ] {
        if let Some(value) = usage.get(key).filter(|v| truthy(Some(v))) {
            if let Some(number) = py::int_or_none(value)? {
                return Ok(number);
            }
        }
    }
    if let Some(details) = usage
        .get("prompt_tokens_details")
        .and_then(Value::as_object)
    {
        if let Some(value) = details.get("cached_tokens").filter(|v| truthy(Some(v))) {
            return Ok(py::int_or_none(value)?.unwrap_or(0));
        }
    }
    Ok(0)
}

/// `usage_cache_convention`.
pub fn usage_cache_convention(usage: &Map<String, Value>) -> Flow<&'static str> {
    if present(usage, "cache_read_input_tokens") || present(usage, "cache_creation_input_tokens") {
        return Ok("anthropic");
    }
    if present(usage, "prompt_tokens") {
        let mut cached = strict_int(usage.get("cache_read_tokens"))?;
        if cached == 0 {
            cached = nested_cached(usage)?;
        }
        let written = strict_int(usage.get("cache_write_tokens"))?;
        let prompt = strict_int(usage.get("prompt_tokens"))?;
        return Ok(if cached + written > prompt {
            "anthropic"
        } else {
            "openai"
        });
    }
    Ok(if present(usage, "input_tokens") {
        "anthropic"
    } else {
        "openai"
    })
}

/// `split_input_tokens`.
pub fn split_input_tokens(usage: &Map<String, Value>) -> Flow<(i64, i64)> {
    let normalized = normalize_usage(usage)?;
    let (input, write, read) = (
        normalized.input,
        normalized.cache_write,
        normalized.cache_read,
    );
    if write == 0 && read == 0 {
        return Ok((input, input));
    }
    if usage_cache_convention(usage)? == "anthropic" {
        let residual = match usage.get("input_tokens") {
            None | Some(Value::Null) => input,
            Some(native) if !truthy(Some(native)) => 0,
            Some(native) => py::int_or_none(native)?.unwrap_or(input),
        };
        return Ok((residual, residual + write + read));
    }
    let uncached = (input - write - read).max(0);
    let total = if input == 0 { write + read } else { input };
    Ok((uncached, total))
}

/// `max(0, int(value or 0))` with every conversion error mapped to 0.
fn lenient_count(value: Option<&Value>) -> Flow<i64> {
    match value.filter(|v| truthy(Some(v))) {
        None => Ok(0),
        Some(value) => Ok(py::int(value)?.unwrap_or(0).max(0)),
    }
}

/// Anthropic `_convert_usage`.
pub fn convert_anthropic_usage(usage: &Map<String, Value>) -> Flow<Map<String, Value>> {
    let count = |row: &Map<String, Value>, key: &str| lenient_count(row.get(key));
    let top_inp = count(usage, "input_tokens")?;
    let top_out = count(usage, "output_tokens")?;
    let top_cw = count(usage, "cache_creation_input_tokens")?;
    let top_cr = count(usage, "cache_read_input_tokens")?;
    let top_thinking = count(usage, "thinking_tokens")?;
    let effective_prompt = top_inp + top_cw + top_cr;
    let iterations: Vec<&Map<String, Value>> = match usage.get("iterations") {
        Some(Value::Array(rows)) => rows.iter().filter_map(Value::as_object).collect(),
        _ => Vec::new(),
    };
    let (inp, out, cw, cr, thinking) = if iterations.is_empty() {
        (top_inp, top_out, top_cw, top_cr, top_thinking)
    } else {
        let sum = |key: &str| -> Flow<i64> { iterations.iter().map(|row| count(row, key)).sum() };
        (
            sum("input_tokens")?,
            sum("output_tokens")?,
            sum("cache_creation_input_tokens")?.max(top_cw),
            sum("cache_read_input_tokens")?.max(top_cr),
            sum("thinking_tokens")?.max(top_thinking),
        )
    };
    let full_prompt = inp + cr + cw;
    let mut result = Map::new();
    for (key, value) in [
        ("prompt_tokens", full_prompt),
        ("completion_tokens", out),
        ("total_tokens", full_prompt + out),
        ("input_tokens", inp),
        ("output_tokens", out),
        ("cache_creation_input_tokens", cw),
        ("cache_read_input_tokens", cr),
        ("effective_prompt_tokens", effective_prompt),
        ("effective_output_tokens", top_out),
    ] {
        result.insert(key.into(), json!(value));
    }
    if thinking != 0 {
        result.insert("thinking_tokens".into(), json!(thinking));
        result.insert("reasoning_tokens".into(), json!(thinking));
    }
    if !iterations.is_empty() {
        let compaction: Vec<&&Map<String, Value>> = iterations
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("compaction"))
            .collect();
        let mut input_total = 0;
        let mut output_total = 0;
        for row in &compaction {
            input_total += count(row, "input_tokens")?
                + count(row, "cache_creation_input_tokens")?
                + count(row, "cache_read_input_tokens")?;
            output_total += count(row, "output_tokens")?;
        }
        result.insert("compaction_iterations".into(), json!(compaction.len()));
        result.insert("compaction_input_tokens".into(), json!(input_total));
        result.insert("compaction_output_tokens".into(), json!(output_total));
    }
    let mut breakdowns: Vec<&Map<String, Value>> = iterations
        .iter()
        .filter_map(|row| row.get("cache_creation").and_then(Value::as_object))
        .collect();
    if breakdowns.is_empty() {
        if let Some(own) = usage.get("cache_creation").and_then(Value::as_object) {
            breakdowns.push(own);
        }
    }
    if !breakdowns.is_empty() {
        let mut creation = Map::new();
        for key in ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"] {
            let mut total = 0;
            for row in &breakdowns {
                total += count(row, key)?;
            }
            creation.insert(key.into(), json!(total));
        }
        result.insert("cache_creation".into(), Value::Object(creation));
    }
    Ok(result)
}

/// Responses `_usage_to_openai`.
pub fn convert_responses_usage(usage: &Value) -> Flow<Map<String, Value>> {
    let mut out = Map::new();
    let Some(usage) = usage.as_object().filter(|map| !map.is_empty()) else {
        return Ok(out);
    };
    let token_count = |value: Option<&Value>| -> Flow<i64> {
        match value.filter(|v| truthy(Some(v))) {
            None => Ok(0),
            Some(value) => Ok(match py::int(value)? {
                Ok(number) => number.max(0),
                Err(IntError::Type | IntError::Value | IntError::Overflow) => 0,
            }),
        }
    };
    let input = token_count(usage.get("input_tokens"))?;
    let output = token_count(usage.get("output_tokens"))?;
    let total = if usage.contains_key("total_tokens") {
        token_count(usage.get("total_tokens"))?
    } else {
        input + output
    };
    out.insert("prompt_tokens".into(), json!(input));
    out.insert("completion_tokens".into(), json!(output));
    out.insert("total_tokens".into(), json!(total));
    if let Some(details) = usage.get("input_tokens_details").and_then(Value::as_object) {
        let mut projected = Map::new();
        if details.contains_key("cached_tokens") {
            projected.insert(
                "cached_tokens".into(),
                json!(token_count(details.get("cached_tokens"))?),
            );
        }
        if details.contains_key("cache_write_tokens") {
            let written = token_count(details.get("cache_write_tokens"))?;
            projected.insert("cache_write_tokens".into(), json!(written));
            out.insert("cache_write_tokens".into(), json!(written));
        }
        if !projected.is_empty() {
            out.insert("prompt_tokens_details".into(), Value::Object(projected));
        }
    }
    if let Some(details) = usage
        .get("output_tokens_details")
        .and_then(Value::as_object)
    {
        if details.contains_key("reasoning_tokens") {
            out.insert(
                "completion_tokens_details".into(),
                json!({"reasoning_tokens": token_count(details.get("reasoning_tokens"))?}),
            );
        }
    }
    Ok(out)
}
