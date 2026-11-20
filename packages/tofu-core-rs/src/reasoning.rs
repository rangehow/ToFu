//! Reasoning intent capture and compilation to the selected route's wire
//! (`lib/llm/reasoning.py`: `capture_intent`, `native_intent`,
//! `resolve_reasoning_control`, `compile_reasoning`, `_apply_sampling`; the
//! no-declaration compatibility boundary `lib/llm/_reasoning_legacy.py`).
//!
//! Every registered catalog profile declares `reasoning.support` and takes
//! the strict compiler, except the GPT-5.6/6 seeds whose profile only
//! carries sampling: those reach the legacy boundary, whose GPT-5 branch and
//! plain fallback are ported. Other legacy families, plugin dialects and
//! `chat_template_kwargs`/`reasoning`/`output_config` extras refuse.
//! A Python `ReasoningContractError` is reproduced as [`Error::Raised`].

use serde_json::{json, Map, Value};

use crate::canonical::{Error, Result, Unsupported};
use crate::family::{
    gpt_generation, is_claude_opus_47, is_doubao, is_ernie, is_gemini, is_glm, is_gpt5, is_grok,
    is_kimi_k3, is_longcat, is_minimax, is_muse_spark, is_qwen,
};
use crate::pyjson::truthy;
use crate::route::{is_claude, is_kimi};

type Msg = Map<String, Value>;

const SAMPLING: [&str; 6] = [
    "temperature",
    "top_p",
    "top_k",
    "presence_penalty",
    "frequency_penalty",
    "stop",
];
const WIRE: [&str; 5] = [
    "thinking",
    "enable_thinking",
    "reasoning_split",
    "reasoning_effort",
    "effort",
];
const BUILTIN_FORMATS: [&str; 5] = [
    "thinking_type",
    "reasoning_effort",
    "enable_thinking",
    "chat_template_kwargs",
    "reasoning_split",
];
const RANKED_DEPTHS: [&str; 7] = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"];

fn contract_error<T>(kind: &str, details: &str) -> Result<T> {
    Err(Error::raised(
        "ReasoningContractError",
        format!("{kind}: {details}"),
    ))
}

fn unsupported<T>(what: &str) -> Result<T> {
    Err(Unsupported(what.to_owned()).into())
}

/// `_reasoning_intent` (version 1).
#[derive(Clone, Debug)]
pub struct Intent {
    pub mode: String,
    pub value: Option<String>,
    pub strict: bool,
    pub sampling: Vec<(String, Value)>,
    pub budget_tokens: Option<i64>,
}

fn py_len(text: &str) -> usize {
    text.chars().count()
}

/// The ported `extra=` keys: the wire spellings native ingress reads and the
/// sampling fields. Anything else would survive into the body unported.
fn check_extra(extra: &Msg) -> Result<()> {
    for key in extra.keys() {
        if !WIRE.contains(&key.as_str()) && !SAMPLING.contains(&key.as_str()) {
            return unsupported(&format!("build_body extra field {key}"));
        }
    }
    Ok(())
}

/// `native_reasoning_effort` for the ported spellings.
pub fn native_reasoning_effort(body: &Msg) -> Result<Option<Value>> {
    let mut values: Vec<&Value> = ["effort", "reasoning_effort"]
        .iter()
        .filter_map(|key| body.get(*key).filter(|value| !value.is_null()))
        .collect();
    for key in ["reasoning", "output_config"] {
        if let Some(Value::Object(section)) = body.get(key) {
            if let Some(value) = section.get("effort").filter(|value| !value.is_null()) {
                values.push(value);
            }
        }
    }
    if values
        .iter()
        .any(|value| !matches!(value, Value::String(s) if !s.is_empty() && py_len(s) <= 64))
    {
        return contract_error("invalid_intent", "reasoning effort is invalid");
    }
    if values.iter().skip(1).any(|value| *value != values[0]) {
        return contract_error("reasoning_effort_conflict", "native efforts disagree");
    }
    Ok(values.first().map(|value| (*value).clone()))
}

fn is_py_int(value: &Value) -> bool {
    matches!(value, Value::Number(n) if n.is_i64() || n.is_u64())
}

fn valid_budget(value: &Value) -> Option<i64> {
    is_py_int(value)
        .then(|| value.as_i64())
        .flatten()
        .filter(|budget| (1..=1_000_000).contains(budget))
}

/// `native_intent`.
pub fn native_intent(body: &Msg) -> Result<Intent> {
    let mut sections: Vec<Msg> = Vec::new();
    for key in [
        "thinking",
        "chat_template_kwargs",
        "reasoning",
        "output_config",
    ] {
        match body.get(key) {
            None | Some(Value::Null) => sections.push(Msg::new()),
            Some(Value::Object(section)) => sections.push(section.clone()),
            Some(_) => {
                return contract_error("invalid_intent", "native reasoning fields must be objects")
            }
        }
    }
    let effort = native_reasoning_effort(body)?;
    let thinking = &sections[0];
    let template = &sections[1];
    let mut enabled: Option<Value> = body
        .get("enable_thinking")
        .or_else(|| template.get("enable_thinking"))
        .cloned();
    if let Some(Value::String(kind)) = thinking.get("type") {
        if matches!(kind.as_str(), "enabled" | "adaptive" | "disabled") {
            enabled = Some(Value::Bool(kind != "disabled"));
        }
    }
    let (mut mode, value) = if effort.as_ref().and_then(Value::as_str) == Some("none")
        || enabled == Some(Value::Bool(false))
    {
        ("off", None)
    } else if let Some(effort) = &effort {
        ("exact", effort.as_str().map(str::to_owned))
    } else if enabled == Some(Value::Bool(true)) || truthy(body.get("reasoning_split")) {
        ("enabled", None)
    } else {
        ("default", None)
    };
    let budget = match thinking.get("budget_tokens") {
        None | Some(Value::Null) => None,
        Some(value) => match valid_budget(value) {
            Some(budget) => Some(budget),
            None => return contract_error("invalid_intent", "reasoning budget is invalid"),
        },
    };
    if budget.is_some() && mode == "default" {
        mode = "enabled";
    }
    Ok(Intent {
        mode: mode.to_owned(),
        value,
        strict: mode != "default",
        sampling: SAMPLING
            .iter()
            .filter_map(|key| body.get(*key).map(|v| ((*key).to_owned(), v.clone())))
            .collect(),
        budget_tokens: budget,
    })
}

fn choice<'v>(request: &'v Value, key: &str) -> Result<Option<&'v str>> {
    match request.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) if py_len(text) <= 64 => Ok(Some(text)),
        Some(_) => contract_error("invalid_intent", "reasoning choice is invalid"),
    }
}

/// `capture_intent(thinking_enabled, thinking_depth, effort, preset,
/// temperature, extra)` from a JSON object of those arguments.
pub fn capture_intent(request: &Value, temperature: &Value) -> Result<Intent> {
    let depth = choice(request, "thinking_depth")?;
    let effort = choice(request, "effort")?;
    let preset = match request.get("preset") {
        None => Some("medium"),
        Some(_) => choice(request, "preset")?,
    };
    let enabled = match request.get("thinking_enabled") {
        None | Some(Value::Null) => None,
        Some(Value::Bool(flag)) => Some(*flag),
        Some(_) => return contract_error("invalid_intent", "reasoning enablement must be boolean"),
    };
    let empty = Msg::new();
    let extra = match request.get("extra") {
        None | Some(Value::Null) => &empty,
        Some(Value::Object(extra)) => extra,
        Some(_) => return unsupported("non-object build_body extra"),
    };
    check_extra(extra)?;
    let control = ["off", "default", "lowest", "enabled"];
    let (mode, value, strict): (String, Option<String>, bool) =
        if enabled == Some(false) && !matches!(depth, Some("off" | "default") | None) {
            ("off".into(), None, true)
        } else if let Some(depth) = depth {
            let mode = if control.contains(&depth) {
                depth
            } else {
                "preset"
            };
            (mode.into(), Some(depth.into()), true)
        } else if let Some(effort) = effort {
            let mode = if effort == "none" {
                "off"
            } else if control.contains(&effort) {
                effort
            } else {
                "exact"
            };
            (mode.into(), Some(effort.into()), true)
        } else if let Some(enabled) = enabled {
            if enabled {
                ("preset".into(), preset.map(str::to_owned), false)
            } else {
                ("off".into(), None, false)
            }
        } else if WIRE.iter().any(|key| extra.contains_key(*key)) {
            let mut native = extra.clone();
            if !native.contains_key("temperature") {
                native.insert("temperature".to_owned(), temperature.clone());
            }
            let mut intent = native_intent(&native)?;
            // `dict(extra, temperature=...)` keeps an existing key's position
            // but `native_intent` reads SAMPLING in canonical order anyway.
            intent.sampling = SAMPLING
                .iter()
                .filter_map(|key| native.get(*key).map(|v| ((*key).to_owned(), v.clone())))
                .collect();
            return Ok(intent);
        } else {
            ("default".into(), None, false)
        };
    let mut sampling = vec![("temperature".to_owned(), temperature.clone())];
    for key in SAMPLING {
        if let Some(value) = extra.get(key) {
            match sampling.iter_mut().find(|(name, _)| name == key) {
                Some(slot) => slot.1 = value.clone(),
                None => sampling.push((key.to_owned(), value.clone())),
            }
        }
    }
    Ok(Intent {
        mode,
        value,
        strict,
        sampling,
        budget_tokens: None,
    })
}

fn is_finite_number(value: &Value) -> bool {
    matches!(value, Value::Number(n) if n.as_f64().is_some_and(f64::is_finite))
}

/// `_validate_intent` for a captured intent.
fn validate_intent(intent: &Intent) -> Result<()> {
    if let Some(value) = &intent.value {
        if value.is_empty() || py_len(value) > 64 {
            return contract_error("invalid_intent", "reasoning effort is invalid");
        }
    }
    for (key, value) in &intent.sampling {
        if value.is_null() {
            continue;
        }
        if key == "stop" {
            let values: Vec<&Value> = match value {
                Value::String(_) => vec![value],
                Value::Array(items) => items.iter().collect(),
                _ => return contract_error("invalid_intent", "stop sequences are invalid"),
            };
            if values.len() > 64
                || values
                    .iter()
                    .any(|v| !matches!(v, Value::String(s) if py_len(s) <= 8192))
            {
                return contract_error("invalid_intent", "stop sequences are invalid");
            }
        } else if !is_finite_number(value) {
            return contract_error("invalid_intent", "sampling values must be finite numbers");
        }
    }
    Ok(())
}

// ── declared controls ──────────────────────────────────────────────────────

fn rank(name: &str) -> Option<usize> {
    RANKED_DEPTHS.iter().position(|depth| *depth == name)
}

fn str_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::Array(items)) => items
            .iter()
            .filter_map(|item| item.as_str().map(str::to_owned))
            .collect(),
        _ => Vec::new(),
    }
}

fn get_str<'v>(map: &'v Value, key: &str) -> Option<&'v str> {
    map.get(key).and_then(Value::as_str)
}

/// `_canonical_ordering`.
fn canonical_ordering(efforts: &[String], reasoning: &Value) -> Vec<String> {
    let disabled = get_str(reasoning, "disabled_effort");
    let selectable: Vec<&String> = efforts
        .iter()
        .filter(|e| e.as_str() != "none" && Some(e.as_str()) != disabled)
        .collect();
    let mut known: Vec<&String> = selectable
        .iter()
        .copied()
        .filter(|e| rank(e).is_some())
        .collect();
    known.sort_by_key(|e| rank(e));
    let unknown: Vec<&String> = selectable
        .iter()
        .copied()
        .filter(|e| rank(e).is_none())
        .collect();
    let (mut ki, mut ui) = (0, 0);
    selectable
        .iter()
        .map(|e| {
            if rank(e).is_some() {
                ki += 1;
                known[ki - 1].clone()
            } else {
                ui += 1;
                unknown[ui - 1].clone()
            }
        })
        .collect()
}

fn cheapest_rung(efforts: &[String], reasoning: &Value) -> Option<String> {
    canonical_ordering(efforts, reasoning).into_iter().next()
}

/// `_clamp_preset` (the clamp flag only feeds private bookkeeping).
fn clamp_preset(value: &str, efforts: &[String], reasoning: &Value) -> Option<String> {
    let mapped = if efforts.iter().any(|e| e == value) {
        value.to_owned()
    } else {
        reasoning
            .get("effort_map")
            .and_then(|map| map.get(value))
            .and_then(Value::as_str)
            .unwrap_or(value)
            .to_owned()
    };
    if efforts.contains(&mapped) {
        return Some(mapped);
    }
    let order = canonical_ordering(efforts, reasoning);
    if let Some(requested) = rank(value) {
        if let Some(below) = order
            .iter()
            .rev()
            .find(|e| rank(e).is_some_and(|r| r <= requested))
        {
            return Some(below.clone());
        }
    }
    if let Some(default) = get_str(reasoning, "default_effort") {
        if efforts.iter().any(|e| e == default) {
            return Some(default.to_owned());
        }
    }
    order.into_iter().next()
}

struct Control {
    fmt: String,
    enabled: bool,
    resolved: Option<String>,
    mode: String,
}

/// `resolve_reasoning_control(reasoning, mode, value)`.
fn resolve_control(reasoning: &Value, mode: &str, value: Option<&str>) -> Result<Control> {
    let fmt = get_str(reasoning, "wire_format")
        .unwrap_or("none")
        .to_owned();
    let support = get_str(reasoning, "support").unwrap_or("unknown");
    let mut enabled = mode != "off" && mode != "default";
    if mode == "default" {
        if support == "supported" && fmt == "reasoning_effort" {
            if let Some(default) = reasoning.get("default_effort").filter(|v| !v.is_null()) {
                return Ok(Control {
                    fmt,
                    enabled: true,
                    resolved: default.as_str().map(str::to_owned),
                    mode: "default".into(),
                });
            }
        }
        return Ok(Control {
            fmt,
            enabled: false,
            resolved: None,
            mode: "default".into(),
        });
    }
    if support != "supported" {
        if mode == "off" && support == "unsupported" {
            return Ok(Control {
                fmt: "none".into(),
                enabled: false,
                resolved: None,
                mode: "off".into(),
            });
        }
        return contract_error(
            "unsupported_reasoning",
            "destination reasoning support is not declared",
        );
    }
    // An undocumented ladder grants no guessed effort. Match the shared
    // Python resolver: product choices degrade to omission, exact API effort
    // fails with the declared contract error.
    if get_str(reasoning, "effort_knowledge") == Some("undocumented") {
        if mode == "exact" {
            return contract_error(
                "unsupported_effort",
                "destination effort ladder is undocumented",
            );
        }
        return Ok(Control {
            fmt,
            enabled: false,
            resolved: None,
            mode: "default".into(),
        });
    }
    let mut mode = mode.to_owned();
    if mode == "off" && reasoning.get("can_disable") != Some(&Value::Bool(true)) {
        mode = "lowest".into();
        enabled = true;
    }
    if fmt == "none" {
        return contract_error(
            "unrepresentable_reasoning",
            "destination has no reasoning control",
        );
    }
    if !BUILTIN_FORMATS.contains(&fmt.as_str()) {
        return unsupported("plugin reasoning dialect");
    }
    let efforts = str_list(reasoning.get("supported_efforts"));
    let resolved: Option<String> = match mode.as_str() {
        "exact" => match value {
            Some(value) if efforts.iter().any(|e| e == value) => Some(value.to_owned()),
            _ => return contract_error("unsupported_effort", "exact effort cannot be represented"),
        },
        "preset" => clamp_preset(value.unwrap_or(""), &efforts, reasoning),
        "lowest" => cheapest_rung(&efforts, reasoning),
        "enabled" => get_str(reasoning, "default_effort")
            .map(str::to_owned)
            .or_else(|| cheapest_rung(&efforts, reasoning)),
        "off" => get_str(reasoning, "disabled_effort")
            .map(str::to_owned)
            .or_else(|| efforts.iter().any(|e| e == "none").then(|| "none".into())),
        _ => None,
    };
    if let Some(resolved) = &resolved {
        if !efforts.contains(resolved) {
            return contract_error(
                "unsupported_effort",
                "requested effort is not in destination ladder",
            );
        }
        if enabled
            && (resolved == "none"
                || Some(resolved.as_str()) == get_str(reasoning, "disabled_effort"))
        {
            return contract_error(
                "unsupported_effort",
                "an enabled request resolved to a disabled effort",
            );
        }
    }
    if fmt == "reasoning_effort" && resolved.is_none() {
        return contract_error(
            "unrepresentable_reasoning",
            "destination has no effort for this intent",
        );
    }
    Ok(Control {
        fmt,
        enabled,
        resolved,
        mode,
    })
}

/// `resolve_reasoning_control(reasoning, mode, value)[::2]`: the wire format
/// and resolved effort (the Responses emitter's exact-effort check).
pub fn resolve_wire_effort(
    reasoning: &Value,
    mode: &str,
    value: Option<&str>,
) -> Result<(String, Option<String>)> {
    let control = resolve_control(reasoning, mode, value)?;
    Ok((control.fmt, control.resolved))
}

// ── legacy boundary ────────────────────────────────────────────────────────

/// `normalize_gpt56_reasoning_effort`.
fn normalize_gpt56(value: &str) -> String {
    let requested = if value.is_empty() { "medium" } else { value }
        .trim()
        .to_lowercase();
    let normalized = match requested.as_str() {
        "off" | "minimal" => "none",
        "ultra" => "max",
        other => other,
    };
    if ["none", "low", "medium", "high", "xhigh", "max"].contains(&normalized) {
        normalized.to_owned()
    } else {
        "medium".to_owned()
    }
}

/// `gpt_reasoning_effort(effort, thinking_enabled, model)`.
fn gpt_reasoning_effort(effort: &str, enabled: bool, model: &str) -> String {
    let generation = gpt_generation(model);
    let spark = model.to_lowercase().contains("codex-spark");
    if !enabled {
        return if spark || generation >= (6, 0) {
            "low"
        } else if generation >= (5, 3) {
            "none"
        } else {
            "minimal"
        }
        .to_owned();
    }
    let requested = if effort.is_empty() { "medium" } else { effort }.to_lowercase();
    let floor = matches!(requested.as_str(), "off" | "minimal" | "none");
    if spark && floor {
        return "low".into();
    }
    if generation >= (6, 0) {
        return if floor {
            "low".into()
        } else {
            normalize_gpt56(&requested)
        };
    }
    if generation >= (5, 6) {
        return normalize_gpt56(&requested);
    }
    let mapped = if generation >= (5, 3) {
        match requested.as_str() {
            "off" | "minimal" | "none" => "none",
            "low" => "low",
            "high" => "high",
            "xhigh" | "max" | "ultra" => "xhigh",
            _ => "medium",
        }
    } else {
        match requested.as_str() {
            "off" | "minimal" => "minimal",
            "low" => "low",
            "high" | "xhigh" | "max" | "ultra" => "high",
            _ => "medium",
        }
    };
    mapped.to_owned()
}

/// `apply_legacy` with no declared dialect, for the ported branches.
fn apply_legacy(
    body: &mut Msg,
    model: &str,
    format: &str,
    enabled: bool,
    temperature: Value,
    effort: &str,
) -> Result<()> {
    if !format.is_empty() {
        return unsupported("legacy reasoning dialect declaration");
    }
    let unported = is_gemini(model)
        || is_longcat(model)
        || is_qwen(model)
        || is_ernie(model)
        || is_doubao(model)
        || is_glm(model)
        || is_kimi(model)
        || is_minimax(model)
        || is_muse_spark(model)
        || is_grok(model);
    if unported {
        return unsupported("legacy name-based reasoning for this family");
    }
    if is_gpt5(model) {
        body.insert(
            "reasoning_effort".to_owned(),
            json!(gpt_reasoning_effort(effort, enabled, model)),
        );
    } else if is_claude(model) && (enabled || is_claude_opus_47(model)) {
        return unsupported("legacy name-based Claude reasoning");
    } else {
        body.insert("temperature".to_owned(), temperature);
    }
    if is_kimi_k3(model) || is_gpt5(model) || is_gemini(model) || is_muse_spark(model) {
        body.shift_remove("temperature");
    }
    if is_claude_opus_47(model) {
        for key in ["temperature", "top_p", "top_k"] {
            body.shift_remove(key);
        }
    }
    Ok(())
}

// ── compilation ────────────────────────────────────────────────────────────

/// `_clear` for a body whose extras passed [`check_extra`].
fn clear(body: &mut Msg) {
    for key in WIRE.iter().chain(SAMPLING.iter()) {
        body.shift_remove(*key);
    }
}

fn as_f64(value: &Value) -> Option<f64> {
    value.as_f64()
}

/// `_apply_sampling`.
fn apply_sampling(body: &mut Msg, sampling: &Value, mode: &str) {
    let enabled = mode != "off" && mode != "default";
    let key = if enabled {
        "enabled_temperature"
    } else {
        "disabled_temperature"
    };
    let mut temperature = if mode != "default" {
        sampling.get(key).filter(|v| !v.is_null())
    } else {
        None
    };
    if temperature.is_none() {
        temperature = sampling.get("temperature").filter(|v| !v.is_null());
    }
    match temperature {
        Some(Value::String(text)) if text == "omit" => {
            body.shift_remove("temperature");
        }
        Some(Value::String(text)) if text == "allow" => {}
        Some(value) => {
            body.insert("temperature".to_owned(), value.clone());
        }
        None => {}
    }
    if let Some(current) = body.get("temperature").filter(|v| !v.is_null()).cloned() {
        let mut current = current;
        if let (Some(low), Some(t)) = (
            sampling.get("min_temperature").filter(|v| !v.is_null()),
            as_f64(&current),
        ) {
            if as_f64(low).is_some_and(|low| low > t) {
                current = low.clone();
            }
        }
        if let (Some(high), Some(t)) = (
            sampling.get("max_temperature").filter(|v| !v.is_null()),
            as_f64(&current),
        ) {
            if as_f64(high).is_some_and(|high| high < t) {
                current = high.clone();
            }
        }
        body.insert("temperature".to_owned(), current);
    }
    for list in ["omit_params", "omit"] {
        for key in str_list(sampling.get(list)) {
            body.shift_remove(&key);
        }
    }
}

/// `compile_reasoning(body, model, inference_profile=profile)` with the
/// captured intent; `profile` must already be the validated document (route
/// snapshots store validated profiles; validation is idempotent).
pub fn compile(
    body: &mut Msg,
    model: &str,
    profile: Option<&Value>,
    intent: &Intent,
) -> Result<()> {
    validate_intent(intent)?;
    let mut mode = intent.mode.clone();
    let value = intent.value.clone();
    clear(body);
    for (key, sampled) in &intent.sampling {
        body.insert(key.clone(), sampled.clone());
    }
    let empty = json!({});
    let reasoning = profile
        .and_then(|p| p.get("reasoning"))
        .filter(|r| r.is_object())
        .unwrap_or(&empty);
    let legacy_unconfigured = !truthy(reasoning.get("support"));
    if profile.is_none() || legacy_unconfigured {
        let format = get_str(reasoning, "wire_format").unwrap_or("");
        let mut legacy_value = value.clone().unwrap_or_else(|| "medium".into());
        if mode == "preset"
            && !intent.strict
            && ["opus", "qwen", "gemini", "minimax", "doubao", "off", "low"]
                .contains(&legacy_value.as_str())
        {
            legacy_value = "medium".into();
        }
        if mode != "default" {
            let temperature = body.shift_remove("temperature").unwrap_or(json!(1.0));
            apply_legacy(
                body,
                model,
                format,
                mode != "off",
                temperature,
                &legacy_value,
            )?;
        }
        let actual = native_intent(body)?;
        if profile.is_some() && intent.strict {
            if mode == "off" && actual.mode != "off" {
                return contract_error(
                    "cannot_disable",
                    "destination cannot guarantee reasoning off",
                );
            }
            if mode == "exact" && (actual.mode != "exact" || actual.value != value) {
                return contract_error(
                    "unsupported_effort",
                    "destination cannot represent exact effort",
                );
            }
            if ["enabled", "lowest", "preset"].contains(&mode.as_str())
                && (actual.mode == "off" || actual.mode == "default")
            {
                return contract_error(
                    "unsupported_reasoning",
                    "destination reasoning support is not declared",
                );
            }
        }
    } else {
        let support = get_str(reasoning, "support").unwrap_or("unknown");
        if mode == "off" && !intent.strict && support == "unknown" {
            mode = "default".into();
        }
        let control = resolve_control(reasoning, &mode, value.as_deref())?;
        mode = control.mode.clone();
        let effort_field = get_str(reasoning, "effort_field")
            .unwrap_or("reasoning_effort")
            .to_owned();
        if mode != "default" && support != "unsupported" {
            match control.fmt.as_str() {
                "thinking_type" => {
                    let enabled_type = reasoning
                        .get("enabled_type")
                        .cloned()
                        .unwrap_or(json!("enabled"));
                    let mut thinking = Msg::new();
                    if control.enabled {
                        thinking.insert("type".to_owned(), enabled_type.clone());
                        if let Some(Value::Object(extra)) = reasoning.get("thinking_extra") {
                            for (key, value) in extra {
                                thinking.insert(key.clone(), value.clone());
                            }
                        }
                        thinking.insert("type".to_owned(), enabled_type);
                        if truthy(reasoning.get("display")) {
                            thinking.insert("display".to_owned(), reasoning["display"].clone());
                        }
                        if get_str(reasoning, "preserve_history") == Some("keep_all") {
                            thinking.insert("keep".to_owned(), json!("all"));
                        }
                        // `clear_thinking_false` needs replayed reasoning_content,
                        // which canonical bodies here never carry.
                        if let Some(budget) = reasoning
                            .get("default_budget_tokens")
                            .filter(|v| !v.is_null())
                        {
                            thinking.insert(
                                "budget_tokens".to_owned(),
                                default_budget(budget, reasoning)?,
                            );
                        }
                    } else {
                        thinking.insert("type".to_owned(), json!("disabled"));
                    }
                    body.insert("thinking".to_owned(), Value::Object(thinking));
                }
                "enable_thinking" | "reasoning_split" => {
                    body.insert(control.fmt.clone(), Value::Bool(control.enabled));
                }
                "chat_template_kwargs" => {
                    body.insert(
                        "chat_template_kwargs".to_owned(),
                        json!({"enable_thinking": control.enabled}),
                    );
                }
                "none" | "reasoning_effort" => {}
                _ => return unsupported("plugin reasoning dialect"),
            }
            if let Some(resolved) = &control.resolved {
                if control.enabled || control.fmt == "reasoning_effort" {
                    body.insert(effort_field.clone(), json!(resolved));
                }
            }
            if control.fmt == "none" {
                return contract_error(
                    "unrepresentable_reasoning",
                    "destination has no reasoning control",
                );
            }
        }
        if mode == "default" && control.fmt == "reasoning_effort" {
            if let Some(resolved) = &control.resolved {
                body.insert(effort_field, json!(resolved));
            }
        }
    }
    let sampling = profile
        .and_then(|p| p.get("sampling"))
        .cloned()
        .unwrap_or(json!({}));
    apply_sampling(body, &sampling, &mode);
    if let Some(budget) = intent.budget_tokens {
        let enabled_thinking = body
            .get("thinking")
            .and_then(|t| t.get("type"))
            .and_then(Value::as_str)
            == Some("enabled");
        if mode == "off" || mode == "default" || !enabled_thinking {
            return contract_error(
                "unrepresentable_budget",
                "destination cannot represent an explicit reasoning budget",
            );
        }
        let low = reasoning
            .get("min_budget_tokens")
            .and_then(Value::as_f64)
            .unwrap_or(1.0);
        let high = reasoning
            .get("max_budget_tokens")
            .and_then(Value::as_f64)
            .unwrap_or(1_000_000.0);
        let requested = budget as f64;
        if !(low <= requested && requested <= high) {
            return contract_error(
                "unsupported_budget",
                "requested reasoning budget exceeds destination bounds",
            );
        }
        body["thinking"]["budget_tokens"] = json!(budget);
    }
    native_intent(body)?;
    Ok(())
}

/// `max(min_budget or 0, min(budget, max_budget or budget))` on integers.
fn default_budget(budget: &Value, reasoning: &Value) -> Result<Value> {
    let int = |value: Option<&Value>| -> Result<Option<i64>> {
        match value {
            None | Some(Value::Null) => Ok(None),
            Some(v) if is_py_int(v) => Ok(v.as_i64()),
            Some(_) => unsupported("non-integer reasoning budget bound"),
        }
    };
    let Some(budget) = int(Some(budget))? else {
        return unsupported("non-integer default budget");
    };
    let low = int(reasoning.get("min_budget_tokens"))?
        .filter(|v| *v != 0)
        .unwrap_or(0);
    let high = int(reasoning.get("max_budget_tokens"))?
        .filter(|v| *v != 0)
        .unwrap_or(budget);
    Ok(json!(low.max(budget.min(high))))
}
