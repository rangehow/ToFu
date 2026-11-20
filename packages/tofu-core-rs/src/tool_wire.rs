//! Final provider tool visibility: the `lib/llm/tool_wire.py`
//! `project_tool_wire` port that runs before cache markers and translation.
//!
//! Ported, in Python's order: background control and direct-call retention
//! (`lib/tools/gateway.py` `preserve_background_wire_tools` with the
//! `lib/tools/background_guidance.py` prose projection,
//! `lib/tools/direct_wire.py`), MCP search-only withholding
//! (`lib/tools/mcp_exposure.py`), the Tool Search `local`/`full` backends with
//! fail-open (`local_wire_tools`, `full_wire_tools`), the local programmatic
//! projection (`ptc_local_wire_tools`, program/batch tiers, additive and
//! gateway-only exposure), the local Swarm projection
//! (`lib/swarm/routing.py` `project_multi_agent_wire_tools`), the schema cost
//! target when it provably keeps every schema, and the route's tool-name
//! budget with its full required floor.
//!
//! Everything else refuses with `Unsupported` instead of guessing: the
//! provider-native programmatic/multi-agent backends, the `code_api`
//! exposure, a schema budget that could drop or compact a schema (it needs
//! the Python token counter), malformed catalog rows, non-integer counts and
//! any Tool Search body field this port does not know.

use std::collections::{BTreeSet, HashSet};

use serde_json::{Map, Value};

use crate::canonical::{Result, Unsupported};
use crate::pyjson::{compact, truthy};
use crate::route::{provider_tool_name, tool_name_budget_bytes, RouteSpec};

type Msg = Map<String, Value>;

const SEARCH_TOOLS: &str = "search_tools";
const EXECUTE_TOOLS: &str = "execute_tools";
const EXECUTE_PROGRAM: &str = "execute_program";
const GATEWAY_TOOL_NAMES: [&str; 3] = [SEARCH_TOOLS, EXECUTE_TOOLS, EXECUTE_PROGRAM];
const BACKGROUND_TOOL_NAMES: [&str; 2] = ["poll", "kill"];
const CODE_CORE_DIRECT_TOOL_NAMES: [&str; 5] = [
    "read_files",
    "search_files",
    "find_files",
    "edit_file",
    "run_command",
];
const LOCAL_TOOL_SEARCH_MIN_FUNCTIONS: usize = 12;
const MCP_TOOL_PREFIX: &str = "mcp__";
const SPAWN_AGENTS: &str = "spawn_agents";
const GET_AGENT_RESULT: &str = "get_agent_result";

/// Body fields `project_tool_wire` reads that this port implements, or that
/// only carry diagnostics (decision sink, stage prose, row identity).
const HANDLED_FIELDS: [&str; 22] = [
    "_executable_tool_catalog",
    "_tool_wire_catalog",
    "_tool_search_mode",
    "_tool_search_fail_open",
    "_tool_search_catalog_size",
    "_tool_searchable_count",
    "_tool_schema_budget_tokens",
    "_tool_discovery_policy_by_name",
    "_tool_orchestration_decision_sink",
    "_tool_orchestration_policy_version",
    "_tool_orchestration_composition",
    "_frontend_selected_tool_names",
    "_required_direct_tool_names",
    "_programmatic_tool_calling",
    "_programmatic_stage",
    "_programmatic_tier",
    "_programmatic_eligible_tools",
    "_programmatic_exposure",
    "_force_local_programmatic",
    "_multi_agent_mode",
    "_multi_agent_stage",
    "_force_local_multi_agent",
];

/// Prefixes of the Tool Search / orchestration field family. A field with one
/// of them that is not in [`HANDLED_FIELDS`] may change Python's wire.
const FIELD_FAMILY: [&str; 9] = [
    "_executable_tool",
    "_tool_search",
    "_tool_wire",
    "_tool_schema",
    "_tool_discovery",
    "_tool_orchestration",
    "_programmatic",
    "_multi_agent",
    "_force_local",
];

fn unsupported<T>(what: &str) -> Result<T> {
    Err(Unsupported(what.to_owned()).into())
}

fn is_background(name: &str) -> bool {
    BACKGROUND_TOOL_NAMES.contains(&name)
}

fn is_mcp(name: &str) -> bool {
    name.starts_with(MCP_TOOL_PREFIX)
}

fn names_of(tools: &[Value]) -> HashSet<String> {
    tools
        .iter()
        .map(|tool| provider_tool_name(tool).to_owned())
        .collect()
}

/// Rows whose names Python reads with `str(...)` under two dialect readers
/// (`_schema_name`, `tool_schema_name`) agree only when every row is an
/// object with one non-empty string name; anything else is not ported.
fn checked_rows<'v>(value: Option<&'v Value>, field: &str) -> Result<Option<&'v Vec<Value>>> {
    let Some(Value::Array(rows)) = value else {
        return Ok(None);
    };
    for row in rows {
        let named = match row {
            Value::Object(tool) => match tool.get("function") {
                Some(Value::Object(function)) => {
                    matches!(function.get("name"), Some(Value::String(name)) if !name.is_empty())
                }
                Some(_) => false,
                None => matches!(tool.get("name"), Some(Value::String(name)) if !name.is_empty()),
            },
            _ => false,
        };
        if !named {
            return unsupported(&format!("malformed tool row in {field}"));
        }
    }
    Ok(Some(rows))
}

fn string_names(value: Option<&Value>, field: &str) -> Result<Vec<String>> {
    match value {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item {
                Value::String(name) => Ok(name.clone()),
                _ => unsupported(&format!("non-string name in {field}")),
            })
            .collect(),
        Some(_) => unsupported(&format!("non-list {field}")),
    }
}

/// `str(value or default).strip().lower()` for a string-or-falsy field.
fn mode(body: &Msg, field: &str, default: &str) -> Result<String> {
    match body.get(field) {
        Some(value) if !truthy(Some(value)) => Ok(default.to_owned()),
        None => Ok(default.to_owned()),
        Some(Value::String(text)) => Ok(text.trim().to_lowercase()),
        Some(_) => unsupported(&format!("non-string {field}")),
    }
}

/// `int(value)` for a JSON integer; `None` for absent/null.
fn optional_int(body: &Msg, field: &str) -> Result<Option<i64>> {
    match body.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(number)) if number.as_i64().is_some() => Ok(number.as_i64()),
        Some(_) => unsupported(&format!("non-integer {field}")),
    }
}

// ── background control guidance (lib/tools/background_guidance.py) ─────────

const RUN_COMMAND_STOP: &str =
    "runs until it finishes, the user stops it, or you stop a backgrounded one with `kill`.";
const STOP_WITHOUT_CONTROL: &str = "runs until it finishes or the user stops it.";
const RUN_HANDOFF: &str = "A command still running after 120 s moves to the background and returns a `bg_*` handle; its result reaches your next step once it finishes, or `poll` waits for it. ";
const PULL_HANDOFF: &str = "A command still running after 120 s moves to the background and returns a `bg_*` handle; collect its result with `poll` before you finish. ";
const BACKGROUND_INTRO: &str = "Start in background and return a command_id handle. Completion is delivered automatically — end the turn or do other work. ";
const RUN_INTRO: &str = "Start in background and return a command_id handle. Its result reaches your next step once it finishes; nothing delivers it after this run returns. ";
const PULL_ONLY_INTRO: &str = "Start in background and return a command_id handle. Nothing delivers its completion to this task automatically. ";
const BACKGROUND_CONTROLS: &str = "`poll` reads its status or waits for it; `kill` stops it.";

#[derive(Clone, Copy, PartialEq, Eq)]
struct Controls {
    poll: bool,
    kill: bool,
}

impl Controls {
    fn full(self) -> bool {
        self.poll && self.kill
    }
}

fn control_names(catalog: &[Value]) -> Controls {
    let mut controls = Controls {
        poll: false,
        kill: false,
    };
    for tool in catalog {
        let Value::Object(map) = tool else {
            continue;
        };
        let function = match map.get("function") {
            Some(Value::Object(function)) => function,
            _ => map,
        };
        match function.get("name").and_then(Value::as_str) {
            Some("poll") => controls.poll = true,
            Some("kill") => controls.kill = true,
            _ => {}
        }
    }
    controls
}

fn control_guidance(controls: Controls, delivery: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    if controls.poll {
        parts.push(match delivery {
            "conversation" => "Call `poll` with this command ID and `wait_s` to read its status or wait mid-turn.",
            "run" => "Call `poll` with this command ID and `wait_s` to wait for it; a blocking wait returns as soon as it finishes, so collect it before you finish.",
            _ => "Call `poll` with this command ID and `wait_s` to read its status or wait for it; collect its result before you finish.",
        });
    }
    if controls.kill {
        parts.push("Use `kill` with this command ID to stop it.");
    } else {
        parts.push("If stopping is needed, return the command ID to the parent or operator; this task has no stop control.");
    }
    if !controls.poll {
        parts.push(if delivery == "conversation" {
            "No result control is authorized here; its completion is delivered automatically, so wait for it or return the command ID to the parent or operator for handling."
        } else {
            "No result control is authorized here; return the command ID to the parent or operator for handling."
        });
    }
    if !controls.full() {
        parts.push("Do not emulate unavailable controls.");
    }
    parts.join(" ")
}

fn project_text(text: &str, controls: Controls) -> String {
    let stop = if controls.kill {
        RUN_COMMAND_STOP
    } else {
        STOP_WITHOUT_CONTROL
    };
    let mut text = text
        .replace(RUN_COMMAND_STOP, stop)
        .replace(STOP_WITHOUT_CONTROL, stop);
    if !controls.poll {
        text = text.replace(RUN_HANDOFF, "").replace(PULL_HANDOFF, "");
    }
    for (intro, delivery) in [
        (BACKGROUND_INTRO, "conversation"),
        (RUN_INTRO, "run"),
        (PULL_ONLY_INTRO, "poll"),
    ] {
        if text.starts_with(intro) {
            let suffix = if controls.full() {
                BACKGROUND_CONTROLS.to_owned()
            } else {
                control_guidance(controls, delivery)
            };
            return format!("{intro}{suffix}");
        }
    }
    let advice = if controls.full() {
        BACKGROUND_CONTROLS.to_owned()
    } else {
        control_guidance(controls, "conversation")
    };
    text.replace(BACKGROUND_CONTROLS, &advice)
}

fn project_background_tool_guidance(tools: Vec<Value>, authority: &[Value]) -> Vec<Value> {
    let controls = control_names(authority);
    tools
        .into_iter()
        .map(|mut tool| {
            let Value::Object(map) = &mut tool else {
                return tool;
            };
            let function = if matches!(map.get("function"), Some(Value::Object(_))) {
                map.get_mut("function")
                    .and_then(Value::as_object_mut)
                    .expect("checked object")
            } else {
                map
            };
            if function.get("name").and_then(Value::as_str) != Some("run_command") {
                return tool;
            }
            if let Some(Value::String(description)) = function.get_mut("description") {
                *description = project_text(description, controls);
            }
            if let Some(Value::Object(background)) = function
                .get_mut("parameters")
                .and_then(Value::as_object_mut)
                .and_then(|parameters| parameters.get_mut("properties"))
                .and_then(Value::as_object_mut)
                .and_then(|properties| properties.get_mut("background"))
            {
                if let Some(Value::String(description)) = background.get_mut("description") {
                    *description = project_text(description, controls);
                }
            }
            tool
        })
        .collect()
}

// ── control and direct-call retention ──────────────────────────────────────

/// `preserve_background_wire_tools` / `preserve_direct_wire_tools`: restore
/// the live authority schema at its existing position, drop revoked or
/// duplicate copies, append missing ones in authority order.
fn preserve_named(
    tools: Vec<Value>,
    authority: &[Value],
    protected: &dyn Fn(&str) -> bool,
) -> Vec<Value> {
    let mut current: Vec<(String, Value)> = Vec::new();
    for tool in authority {
        let name = provider_tool_name(tool);
        if !protected(name) {
            continue;
        }
        // dict comprehension: the last authority row wins at the first position.
        match current.iter_mut().find(|(known, _)| known == name) {
            Some(slot) => slot.1 = tool.clone(),
            None => current.push((name.to_owned(), tool.clone())),
        }
    }
    let mut visible = Vec::with_capacity(tools.len() + current.len());
    let mut seen: HashSet<String> = HashSet::new();
    for tool in tools {
        let name = provider_tool_name(&tool).to_owned();
        if !tool.is_object() || !protected(&name) {
            visible.push(tool);
        } else if let Some((_, live)) = current.iter().find(|(known, _)| *known == name) {
            if seen.insert(name) {
                visible.push(live.clone());
            }
        }
    }
    for (name, tool) in current {
        if !seen.contains(&name) {
            visible.push(tool);
        }
    }
    visible
}

fn preserve_background(tools: Vec<Value>, authority: &[Value]) -> Vec<Value> {
    let visible = preserve_named(tools, authority, &is_background);
    project_background_tool_guidance(visible, authority)
}

fn preserve_direct(
    tools: Vec<Value>,
    authority: &[Value],
    declared: &BTreeSet<String>,
) -> Vec<Value> {
    preserve_named(tools, authority, &|name: &str| declared.contains(name))
}

// ── gateway schemas (lib/tools/gateway.py) ─────────────────────────────────

fn search_tools_schema() -> Value {
    serde_json::json!({
        "type": "function",
        "function": {
            "name": SEARCH_TOOLS,
            "description": "Find task-available tools absent from this request. This only finds tools; call execute_tools with a result's exact name and arguments_schema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 512},
                    "namespace": {"type": "string", "maxLength": 128,
                                  "description": "Optional namespace from an earlier result. Omit if unsure."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8,
                              "description": "Max results per call (1–20)."},
                    "cursor": {"type": "string", "maxLength": 128},
                },
                "required": ["query"],
                "additionalProperties": false,
            },
        },
    })
}

fn execute_tools_batch_schema() -> Value {
    serde_json::json!({
        "type": "function",
        "function": {
            "name": EXECUTE_TOOLS,
            "description": "Run independent calls with known JSON arguments using execution=parallel. Use direct tools for single actions. Sequential preserves order: inspect prior results before dependent calls. Find exact schemas with search_tools. Never repeat direct calls here. Permissions/approvals apply.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calls": {
                        "type": "array",
                        "maxItems": 16,
                        "description": "Tool calls. Use a task-executable exact name and arguments that match its schema; search_tools is optional.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "arguments": {"type": "object"},
                                "call_id": {"type": "string"},
                            },
                            "required": ["name", "arguments"],
                            "additionalProperties": false,
                        },
                    },
                    "execution": {"type": "string", "enum": ["auto", "sequential", "parallel"], "default": "auto"},
                },
                "additionalProperties": false,
            },
        },
    })
}

fn execute_program_schema() -> Value {
    serde_json::json!({
        "type": "function",
        "function": {
            "name": EXECUTE_PROGRAM,
            "description": concat!(
                "Same permissions/approvals. No imports/timers/files/processes/network; ",
                "external effects use tools.*. ",
                "Async JS body, no wrapper; return, not console.log. ",
                "Example: return {sum:1+2+3}; ",
                "await tools.invoke(name,args) runs NEW calls and stops on failure; Promise.all batches. ",
                "await tools.call(name,args) returns a receipt: check .status, then await tools.value(receipt). ",
                "programInput is metadata, NOT a global; paste programInput.read. ",
                "tools.result(name,args) or tools.result({ref}): retained decoded data only; unavailable hands off. ",
                "tools.inputs({name,offset:0,limit:16}): {inputs,total,nextOffset}; check inputs.length before inputs[i].ref. ",
                "Empty page is not empty source. Use input_ refs; artifactRef needs read_tool_artifact. ",
                "jsonType=type, not coverage. tools.keep(value): computed JSON only. ",
                "tools.observe(reason,data): unresolved judgment; stops with no resume. ",
                "tools.parseJSON(text) preserves BigInt; serialize with String(). ",
                "Never replay completed actions. ",
                "32KiB code;16 calls/reads/queries/keeps each;1MiB inputs."),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "minLength": 1}},
                "required": ["code"],
                "additionalProperties": false,
            },
        },
    })
}

// ── schema cost target ─────────────────────────────────────────────────────

/// `fit_tool_schema_budget` keeps every schema exactly when the token count
/// of the normalized list is within the target. Python counts with a
/// byte-level BPE (or the ≤1-token-per-character heuristic), so a compact
/// encoding no longer than the target proves the no-op; anything else needs
/// the Python token counter and refuses.
fn schema_budget_keeps_all(tools: &[Value], budget: i64, model: &str) -> Result<()> {
    let preflighted = crate::wire::preflight_tools(tools, model)?;
    if preflighted.len() != tools.len() {
        return unsupported("schema budget over a schema the preflight isolates");
    }
    // The preflight adds bytes the fit's normalized rows lack (type repair,
    // decoded-result prose) but drops `decoded_result_schema`; add it back.
    let mut bound = compact(&Value::Array(preflighted)).len();
    for tool in tools {
        if let Some(declared) = tool.get("decoded_result_schema") {
            bound += "\"decoded_result_schema\":,".len() + compact(declared).len();
        }
    }
    if bound as i64 > budget {
        return unsupported("schema budget that may drop or compact a schema");
    }
    Ok(())
}

// ── local Tool Search surface ──────────────────────────────────────────────

struct LocalSurface<'a> {
    policy: Option<&'a Map<String, Value>>,
    catalog_size: Option<i64>,
    searchable_count: Option<i64>,
    schema_budget: i64,
    keep_discovery: bool,
}

fn policy_of<'a>(policy: Option<&'a Map<String, Value>>, name: &str) -> Option<&'a Value> {
    policy.and_then(|policy| policy.get(name))
}

fn local_wire_tools(tools: Vec<Value>, surface: &LocalSurface<'_>) -> Result<Vec<Value>> {
    let names = names_of(&tools);
    let gateways: Vec<Value> = [search_tools_schema(), execute_tools_batch_schema()]
        .into_iter()
        .filter(|gateway| !names.contains(provider_tool_name(gateway)))
        .collect();
    let total = match surface.catalog_size {
        Some(size) => (tools.len() as i64).max(size),
        None => tools.len() as i64,
    };
    let is_searchable = |tool: &Value| matches!(policy_of(surface.policy, provider_tool_name(tool)), Some(Value::String(p)) if p == "searchable");
    let visible_searchable = tools.iter().filter(|tool| is_searchable(tool)).count() as i64;
    let searchable = match surface.searchable_count {
        Some(count) => visible_searchable.max(count),
        None => visible_searchable,
    };
    if total < LOCAL_TOOL_SEARCH_MIN_FUNCTIONS as i64 || searchable <= 0 {
        let budget_pressure = surface.keep_discovery
            || (surface.schema_budget != 0 && {
                // tool_schema_tokens(tools) > budget, decided only when the
                // byte bound proves it false.
                if compact(&Value::Array(tools.clone())).len() as i64 > surface.schema_budget {
                    return unsupported("schema budget pressure on a small surface");
                }
                false
            });
        let mut surface_tools = tools;
        if budget_pressure {
            surface_tools.extend(gateways);
        }
        return Ok(surface_tools);
    }
    let eager: Vec<Value> = tools
        .into_iter()
        .filter(|tool| {
            let name = provider_tool_name(tool);
            let eager = match policy_of(surface.policy, name) {
                None => true,
                Some(Value::String(p)) => p == "eager",
                Some(_) => false,
            };
            eager || is_background(name)
        })
        .collect();
    let eager_names = names_of(&eager);
    let mut out = eager;
    out.extend(
        gateways
            .into_iter()
            .filter(|gateway| !eager_names.contains(provider_tool_name(gateway))),
    );
    Ok(out)
}

// ── programmatic projection ────────────────────────────────────────────────

fn local_programmatic_present(tools: &[Value]) -> bool {
    tools.iter().any(|tool| {
        let Value::Object(map) = tool else {
            return false;
        };
        if map.get("type").and_then(Value::as_str) != Some("function") {
            return false;
        }
        let function = match map.get("function") {
            Some(Value::Object(function)) => function,
            Some(_) => return false,
            None => map,
        };
        matches!(function.get("name"), Some(Value::String(name))
            if !name.trim().is_empty() && !GATEWAY_TOOL_NAMES.contains(&name.as_str()))
    })
}

fn ptc_local_wire_tools(tools: Vec<Value>, program: bool, exposure: &str) -> Result<Vec<Value>> {
    if program && exposure == "code_api" {
        return unsupported("programmatic code_api exposure");
    }
    let gateway_only = exposure == "gateway_only";
    let mut out = Vec::with_capacity(tools.len() + 1);
    for tool in tools {
        if !tool.is_object() {
            continue;
        }
        let name = provider_tool_name(&tool);
        if name == EXECUTE_TOOLS {
            if program {
                out.push(execute_tools_batch_schema());
            }
        } else if name != EXECUTE_PROGRAM && (!gateway_only || is_background(name)) {
            out.push(tool);
        }
    }
    out.push(if program {
        execute_program_schema()
    } else {
        execute_tools_batch_schema()
    });
    Ok(out)
}

/// A route that declares a provider-native lane may resolve to it; that
/// converter path is not ported.
fn refuse_native_lane(
    body: &Msg,
    protocol: &str,
    route: &RouteSpec<'_>,
    feature: &str,
) -> Result<()> {
    if route
        .inference_profile
        .and_then(|profile| profile.get("features"))
        .and_then(|features| features.get(feature))
        .is_some()
    {
        return unsupported(&format!("route declaring {feature}"));
    }
    if route.inference_profile.is_none() {
        let profile = mode(body, "_responses_feature_profile", "compatible")?;
        if profile.is_empty() || (protocol == "responses" && profile == "openai") {
            return unsupported("openai Responses feature profile");
        }
    }
    Ok(())
}

// ── multi-agent projection ─────────────────────────────────────────────────

fn project_local_swarm(tools: Vec<Value>, authority: &[Value]) -> Vec<Value> {
    let Some(source) = authority
        .iter()
        .find(|tool| provider_tool_name(tool) == SPAWN_AGENTS)
        .and_then(Value::as_object)
    else {
        return tools;
    };
    let mut spawn = source.clone();
    let function = if matches!(spawn.get("function"), Some(Value::Object(_))) {
        spawn
            .get_mut("function")
            .and_then(Value::as_object_mut)
            .expect("checked object")
    } else {
        &mut spawn
    };
    if let Some(Value::Object(agents)) = function
        .get_mut("parameters")
        .and_then(Value::as_object_mut)
        .and_then(|parameters| parameters.get_mut("properties"))
        .and_then(Value::as_object_mut)
        .and_then(|properties| properties.get_mut("agents"))
    {
        agents.shift_remove("maxItems");
    }
    let spawn = Value::Object(spawn);
    let mut out = Vec::with_capacity(tools.len() + 1);
    let mut inserted = false;
    for tool in tools {
        if provider_tool_name(&tool) == SPAWN_AGENTS {
            if !inserted {
                out.push(spawn.clone());
                inserted = true;
            }
            continue;
        }
        out.push(tool);
    }
    if !inserted {
        out.push(spawn);
    }
    out
}

// ── tool-name budget ───────────────────────────────────────────────────────

fn name_bytes(name: &str) -> usize {
    name.len() + 1
}

fn tool_name_bytes(tools: &[Value]) -> usize {
    tools
        .iter()
        .map(provider_tool_name)
        .filter(|name| !name.is_empty())
        .map(name_bytes)
        .sum()
}

fn hides_a_name(tools: &[Value], budget: usize, floor: &HashSet<String>) -> bool {
    budget != 0
        && tool_name_bytes(tools) > budget
        && tools
            .iter()
            .map(provider_tool_name)
            .any(|name| !name.is_empty() && !floor.contains(name))
}

/// `fit_tool_name_budget`: hide the longest optional names (later position
/// first on ties) until the joined names fit; `floor` always stays.
fn fit_tool_name_budget(tools: Vec<Value>, budget: usize, floor: &HashSet<String>) -> Vec<Value> {
    let mut total = tool_name_bytes(&tools);
    if total <= budget {
        return tools;
    }
    let mut optional: Vec<(usize, &str)> = tools
        .iter()
        .enumerate()
        .map(|(index, tool)| (index, provider_tool_name(tool)))
        .filter(|(_, name)| !name.is_empty() && !floor.contains(*name))
        .collect();
    optional.sort_by(|a, b| {
        name_bytes(b.1)
            .cmp(&name_bytes(a.1))
            .then_with(|| b.0.cmp(&a.0))
    });
    let mut hidden = HashSet::new();
    for (index, name) in optional {
        if total <= budget {
            break;
        }
        hidden.insert(index);
        total -= name_bytes(name);
    }
    tools
        .into_iter()
        .enumerate()
        .filter(|(index, _)| !hidden.contains(index))
        .map(|(_, tool)| tool)
        .collect()
}

// ── project_tool_wire ──────────────────────────────────────────────────────

fn guard_fields(body: &Msg) -> Result<()> {
    for key in body.keys() {
        if FIELD_FAMILY.iter().any(|prefix| key.starts_with(prefix))
            && !HANDLED_FIELDS.contains(&key.as_str())
        {
            return unsupported(&format!("Tool Search field {key}"));
        }
    }
    Ok(())
}

/// `project_tool_wire` up to (not including) the final wire preflight, which
/// `wire::prepare` applies next. Mutates `body["tools"]` / `tool_choice`.
pub fn project_tool_wire(body: &mut Msg, protocol: &str, route: &RouteSpec<'_>) -> Result<()> {
    guard_fields(body)?;
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let catalog: Option<Vec<Value>> = checked_rows(
        body.get("_executable_tool_catalog"),
        "_executable_tool_catalog",
    )?
    .cloned();
    let private_projection = catalog.is_some()
        || body
            .keys()
            .any(|key| FIELD_FAMILY.iter().any(|prefix| key.starts_with(prefix)))
        || body.contains_key("_required_direct_tool_names")
        || body.contains_key("_frontend_selected_tool_names");
    if private_projection {
        checked_rows(body.get("tools"), "tools")?;
        checked_rows(body.get("_tool_wire_catalog"), "_tool_wire_catalog")?;
    }
    let body_tools: Option<Vec<Value>> = match body.get("tools") {
        Some(Value::Array(tools)) => Some(tools.clone()),
        _ => None,
    };
    let authority: Vec<Value> = catalog
        .clone()
        .or_else(|| body_tools.clone())
        .unwrap_or_default();
    let background_names: BTreeSet<String> = authority
        .iter()
        .map(provider_tool_name)
        .filter(|name| is_background(name))
        .map(str::to_owned)
        .collect();
    let mut declarations: BTreeSet<String> = match body.get("_required_direct_tool_names") {
        Some(Value::Array(names)) => names
            .iter()
            .filter_map(Value::as_str)
            .filter(|name| !name.is_empty())
            .map(str::to_owned)
            .collect(),
        _ => BTreeSet::new(),
    };
    let choice_name = match body
        .get("tool_choice")
        .and_then(|choice| choice.get("function"))
    {
        Some(Value::Object(function)) if truthy(function.get("name")) => match function.get("name")
        {
            Some(Value::String(name)) => Some(name.clone()),
            _ => return unsupported("non-string tool_choice function name"),
        },
        _ => None,
    };
    let choice_is_function_object = matches!(
        body.get("tool_choice")
            .and_then(|choice| choice.get("function")),
        Some(Value::Object(_))
    ) && body.get("tool_choice").is_some_and(Value::is_object);
    if let Some(name) = &choice_name {
        declarations.insert(name.clone());
    }
    declarations.retain(|name| !is_mcp(name));
    if catalog.is_some() && choice_is_function_object && choice_name.as_deref().is_some_and(is_mcp)
    {
        body.shift_remove("tool_choice");
    }
    let authority_names = names_of(&authority);
    let direct_names: BTreeSet<String> = declarations
        .iter()
        .filter(|name| authority_names.contains(*name))
        .cloned()
        .collect();
    let protected: BTreeSet<String> = background_names.union(&direct_names).cloned().collect();
    if !protected.is_empty() {
        let mut pins: BTreeSet<String> = string_names(
            body.get("_frontend_selected_tool_names"),
            "_frontend_selected_tool_names",
        )?
        .into_iter()
        .collect();
        pins.extend(protected.iter().cloned());
        body.insert(
            "_frontend_selected_tool_names".to_owned(),
            Value::Array(pins.into_iter().map(Value::String).collect()),
        );
        let mut policy = match body.get("_tool_discovery_policy_by_name") {
            None | Some(Value::Null) => Map::new(),
            Some(Value::Object(policy)) => policy.clone(),
            Some(value) if !truthy(Some(value)) => Map::new(),
            Some(_) => return unsupported("non-object _tool_discovery_policy_by_name"),
        };
        for name in &protected {
            policy.insert(name.clone(), Value::String("eager".to_owned()));
        }
        body.insert(
            "_tool_discovery_policy_by_name".to_owned(),
            Value::Object(policy),
        );
    }
    let mut required_direct: HashSet<String> = direct_names.iter().cloned().collect();
    let name_budget = tool_name_budget_bytes(route, protocol, &model);
    let mut backend = "";
    let mut tools: Option<Vec<Value>> = body_tools;
    let schema_budget = match optional_int(body, "_tool_schema_budget_tokens")? {
        Some(budget) if budget < 0 => return unsupported("negative schema budget"),
        budget => budget.unwrap_or(0),
    };
    if let Some(catalog) = &catalog {
        backend = if truthy(body.get("_tool_search_fail_open"))
            || mode(body, "_tool_search_mode", "local")? == "off"
        {
            "full"
        } else {
            "local"
        };
        // Pins only rank schemas when a budget must cut; that path refuses.
        string_names(
            body.get("_frontend_selected_tool_names"),
            "_frontend_selected_tool_names",
        )?;
        let mut wire: Vec<Value> = match body.get("_tool_wire_catalog") {
            Some(Value::Array(rows)) => rows.clone(),
            _ => catalog.clone(),
        };
        if backend == "full" && truthy(body.get("_tool_search_fail_open")) {
            wire = catalog.clone();
        }
        wire.retain(|tool| !is_mcp(provider_tool_name(tool)));
        wire = preserve_background(wire, &authority);
        wire = preserve_direct(wire, &authority, &declarations);
        let wire_names = names_of(&wire);
        if CODE_CORE_DIRECT_TOOL_NAMES
            .iter()
            .any(|name| *name != "read_files" && wire_names.contains(*name))
        {
            required_direct.extend(
                CODE_CORE_DIRECT_TOOL_NAMES
                    .iter()
                    .filter(|name| wire_names.contains(**name))
                    .map(|name| (*name).to_owned()),
            );
        }
        tools = Some(if backend == "local" {
            let withholds_mcp = catalog.iter().any(|tool| {
                let name = provider_tool_name(tool);
                is_mcp(name) && !wire_names.contains(name)
            });
            let mut floor = required_direct.clone();
            floor.extend(GATEWAY_TOOL_NAMES.iter().map(|name| (*name).to_owned()));
            floor.extend(BACKGROUND_TOOL_NAMES.iter().map(|name| (*name).to_owned()));
            let policy = match body.get("_tool_discovery_policy_by_name") {
                Some(Value::Object(policy)) => Some(policy),
                None | Some(Value::Null) => None,
                Some(value) if !truthy(Some(value)) => None,
                Some(_) => return unsupported("non-object _tool_discovery_policy_by_name"),
            };
            let surface = LocalSurface {
                policy,
                catalog_size: optional_int(body, "_tool_search_catalog_size")?,
                searchable_count: optional_int(body, "_tool_searchable_count")?,
                schema_budget,
                keep_discovery: withholds_mcp || hides_a_name(&wire, name_budget, &floor),
            };
            local_wire_tools(wire, &surface)?
        } else {
            wire
        });
    }

    // Programmatic Tool Calling.
    let requested_ptc = mode(body, "_programmatic_tool_calling", "off")?;
    let ptc_active = requested_ptc == "on" || requested_ptc == "auto";
    let local_ptc_present =
        local_programmatic_present(catalog.as_deref().or(tools.as_deref()).unwrap_or(&[]));
    let ptc_local =
        if truthy(body.get("_force_local_programmatic")) && ptc_active && local_ptc_present {
            true
        } else if !ptc_active {
            false
        } else {
            let eligible = truthy(body.get("_programmatic_eligible_tools"));
            if !eligible && !local_ptc_present {
                false
            } else {
                if eligible {
                    refuse_native_lane(body, protocol, route, "responses_programmatic")?;
                }
                true
            }
        };
    if ptc_local {
        if let Some(current) = tools.take() {
            let mut program = ptc_active;
            if body.get("_programmatic_tier").and_then(Value::as_str) == Some("batch") {
                program = false;
            }
            let exposure = mode(body, "_programmatic_exposure", "additive")?;
            tools = Some(ptc_local_wire_tools(current, program, &exposure)?);
        }
    }

    // Multi-agent; without a catalog the authority is the post-PTC surface.
    let ptc_surface = tools.clone().unwrap_or_default();
    let multi_authority: &[Value] = catalog.as_deref().unwrap_or(&ptc_surface);
    let local_swarm_available = multi_authority
        .iter()
        .any(|tool| provider_tool_name(tool) == SPAWN_AGENTS);
    let forced_mode = match body.get("_multi_agent_mode") {
        Some(value) if !truthy(Some(value)) => String::new(),
        None => String::new(),
        Some(Value::String(text)) => text.to_lowercase(),
        Some(_) => return unsupported("non-string _multi_agent_mode"),
    };
    let multi_local = if truthy(body.get("_force_local_multi_agent"))
        && (forced_mode == "read_only" || forced_mode == "full")
    {
        local_swarm_available
    } else {
        match mode(body, "_multi_agent_mode", "off")?.as_str() {
            "full" => local_swarm_available,
            "read_only" => {
                refuse_native_lane(body, protocol, route, "responses_multi_agent")?;
                local_swarm_available
            }
            _ => false,
        }
    };
    if multi_local {
        if let Some(current) = tools.take() {
            tools = Some(project_local_swarm(current, multi_authority));
        }
    }

    if tools.is_some() || !background_names.is_empty() {
        tools = Some(preserve_background(tools.unwrap_or_default(), &authority));
    }
    if tools.is_some() || !direct_names.is_empty() {
        tools = Some(preserve_direct(
            tools.unwrap_or_default(),
            &authority,
            &declarations,
        ));
    }

    let mut required = required_direct;
    if multi_local {
        required.insert(SPAWN_AGENTS.to_owned());
        required.insert(GET_AGENT_RESULT.to_owned());
    }
    if schema_budget != 0 && backend == "local" {
        if let Some(current) = &tools {
            schema_budget_keeps_all(current, schema_budget, &model)?;
        }
    }
    let mut floor = required;
    floor.extend(GATEWAY_TOOL_NAMES.iter().map(|name| (*name).to_owned()));
    floor.extend(BACKGROUND_TOOL_NAMES.iter().map(|name| (*name).to_owned()));
    if let Some(current) = tools.take() {
        let names = names_of(&current);
        tools = Some(
            if name_budget != 0 && names.contains(SEARCH_TOOLS) && names.contains(EXECUTE_TOOLS) {
                fit_tool_name_budget(current, name_budget, &floor)
            } else {
                current
            },
        );
    }
    if let Some(tools) = tools {
        body.insert("tools".to_owned(), Value::Array(tools));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn full_controls_keep_canonical_run_command_prose() {
        let full = Controls {
            poll: true,
            kill: true,
        };
        let text = format!("Run. NO timeout: {RUN_COMMAND_STOP} Rest.");
        assert_eq!(project_text(&text, full), text);
        let intro = format!("{BACKGROUND_INTRO}{BACKGROUND_CONTROLS}");
        assert_eq!(project_text(&intro, full), intro);
    }

    #[test]
    fn missing_kill_rewrites_the_stop_clause() {
        let poll_only = Controls {
            poll: true,
            kill: false,
        };
        let text = format!("NO timeout: {RUN_COMMAND_STOP}");
        assert_eq!(
            project_text(&text, poll_only),
            format!("NO timeout: {STOP_WITHOUT_CONTROL}")
        );
    }

    #[test]
    fn name_budget_keeps_the_floor() {
        let tool = |name: &str| json!({"type": "function", "function": {"name": name}});
        let tools = vec![
            tool("search_tools"),
            tool("execute_tools"),
            tool(&"x".repeat(30)),
            tool("poll"),
        ];
        let floor: HashSet<String> = ["search_tools", "execute_tools", "poll"]
            .iter()
            .map(|n| n.to_string())
            .collect();
        let fitted = fit_tool_name_budget(tools, 40, &floor);
        assert_eq!(names_of(&fitted).len(), 3);
    }
}
