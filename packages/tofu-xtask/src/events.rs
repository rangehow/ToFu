//! `contracts/events_v1.json` -> `frontend/src/api/event-contract.generated.ts`
//! (TypeScript interfaces) and `packages/tofu-contracts/src/generated_events_v1.rs`
//! (serde types and metadata).
//!
//! Field kinds are the FieldSpec vocabulary (`str`, `bool`, `int`, `number`,
//! `dict`, `list`, `None`, joined by `|`). In Rust an `int | number` field is a
//! `serde_json::Number`, any other mixed kind a `serde_json::Value`; an optional
//! field that admits null (or is a `Value`) is `Option<Option<T>>`/`Option<Value>`
//! with `crate::wire::present`, so absent and explicit null stay different.

use serde_json::Value;

use crate::json::{array, boolean, field, integer, object, string, Object};
use crate::names::{doc_text, rust_field_name, rust_string, rust_type_name};

pub const CONTRACT: &str = "contracts/events_v1.json";
pub const CONTRACT_ID: &str = "tofu.events/v1";
pub const TS_OUTPUT: &str = "frontend/src/api/event-contract.generated.ts";
pub const RUST_OUTPUT: &str = "packages/tofu-contracts/src/generated_events_v1.rs";

pub fn load(text: &str) -> Result<Value, String> {
    let document: Value =
        serde_json::from_str(text).map_err(|error| format!("{CONTRACT}: {error}"))?;
    if document.get("contract").and_then(Value::as_str) != Some(CONTRACT_ID) {
        return Err(format!("{CONTRACT} is not {CONTRACT_ID}"));
    }
    Ok(document)
}

fn ts_kind(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "str" => "string",
        "bool" => "boolean",
        "int" | "number" => "number",
        "dict" => "Record<string, unknown>",
        "list" => "unknown[]",
        "None" => "null",
        _ => return None,
    })
}

fn rust_kind(kind: &str) -> &'static str {
    match kind {
        "str" => "String",
        "bool" => "bool",
        "int" => "i64",
        "number" => "f64",
        "dict" => "serde_json::Map<String, serde_json::Value>",
        _ => "Vec<serde_json::Value>",
    }
}

fn alternatives(kind: &str) -> Result<Vec<&str>, String> {
    let parts: Vec<&str> = kind.split('|').map(str::trim).collect();
    if parts.iter().any(|part| ts_kind(part).is_none()) {
        return Err(format!("unknown FieldSpec kind alternative: {kind:?}"));
    }
    Ok(parts)
}

fn ts_type(kind: &str) -> Result<String, String> {
    let mut mapped: Vec<&str> = Vec::new();
    for part in alternatives(kind)? {
        let name = ts_kind(part).expect("validated");
        if !mapped.contains(&name) {
            mapped.push(name);
        }
    }
    Ok(mapped.join(" | "))
}

/// (Rust type of the non-null alternatives, whether null is allowed).
fn rust_value_type(kind: &str) -> Result<(String, bool), String> {
    let parts = alternatives(kind)?;
    let nullable = parts.contains(&"None");
    let mut mapped: Vec<&str> = Vec::new();
    for part in parts.iter().filter(|part| **part != "None") {
        let name = rust_kind(part);
        if !mapped.contains(&name) {
            mapped.push(name);
        }
    }
    if mapped.is_empty() {
        return Ok(("serde_json::Value".into(), true));
    }
    let mut set = mapped.clone();
    set.sort_unstable();
    if set == ["f64", "i64"] {
        return Ok(("serde_json::Number".into(), nullable));
    }
    if mapped.len() > 1 {
        // A JSON value carries its own null; nullability is not a second layer.
        return Ok(("serde_json::Value".into(), false));
    }
    Ok((mapped[0].to_string(), nullable))
}

/// Python `str.capitalize` over the ASCII wire names: first letter upper, rest lower.
fn capitalize(part: &str) -> String {
    let mut chars = part.chars();
    match chars.next() {
        Some(first) => first
            .to_uppercase()
            .chain(chars.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

fn interface_name(event_type: &str) -> String {
    let mut name: String = event_type
        .split('_')
        .map(|part| {
            if part.is_empty() {
                "_".to_string()
            } else {
                capitalize(part)
            }
        })
        .collect();
    name.push_str("Event");
    name
}

fn push_interface_name(frame_type: &str) -> String {
    // Push frame types mix '_' and '.' (codex.reset_offer.updated).
    let mut name: String = frame_type
        .split(['_', '.'])
        .map(|part| {
            if part.is_empty() {
                "_".to_string()
            } else {
                capitalize(part)
            }
        })
        .collect();
    name.push_str("PushFrame");
    name
}

fn ts_doc(text: &str) -> String {
    // A literal `*/` in prose would terminate the block comment early.
    doc_text(text).replace("*/", "* /")
}

struct FieldSpec<'a> {
    name: &'a str,
    kind: &'a str,
    required: bool,
}

fn field_specs<'a>(value: &'a Value, what: &str) -> Result<Vec<FieldSpec<'a>>, String> {
    let mut specs = Vec::new();
    for item in array(value, what)? {
        let item = object(item, what)?;
        specs.push(FieldSpec {
            name: string(field(item, "name", what)?, what)?,
            kind: string(field(item, "kind", what)?, what)?,
            required: boolean(field(item, "required", what)?, what)?,
        });
    }
    Ok(specs)
}

fn text<'a>(spec: &'a Object, key: &str) -> Result<&'a str, String> {
    let what = spec.get("type").and_then(Value::as_str).unwrap_or(key);
    string(field(spec, key, what)?, what)
}

fn descriptions(spec: &Object) -> Result<&Object, String> {
    object(field(spec, "fields", text(spec, "type")?)?, "fields")
}

fn description<'a>(fields: &'a Object, name: &str) -> &'a str {
    fields.get(name).and_then(Value::as_str).unwrap_or("")
}

/// (spec, its schema) for every spec with a field-level schema.
fn contracted<'a>(specs: &'a [Value]) -> Result<Vec<(&'a Object, Vec<FieldSpec<'a>>)>, String> {
    let mut out = Vec::new();
    for spec in specs {
        let spec = object(spec, "spec")?;
        let schema = field(spec, "schema", text(spec, "type")?)?;
        if !schema.is_null() {
            out.push((spec, field_specs(schema, text(spec, "type")?)?));
        }
    }
    Ok(out)
}

fn ts_fields(lines: &mut Vec<String>, schema: &[FieldSpec], fields: &Object) -> Result<(), String> {
    for item in schema {
        let description = description(fields, item.name);
        if !description.is_empty() {
            lines.push(format!("  /** {} */", ts_doc(description)));
        }
        let optional = if item.required { "" } else { "?" };
        lines.push(format!(
            "  {}{optional}: {};",
            item.name,
            ts_type(item.kind)?
        ));
    }
    Ok(())
}

pub fn render_typescript(document: &Value) -> Result<String, String> {
    let root = object(document, CONTRACT)?;
    let events = array(field(root, "events", CONTRACT)?, "events")?;
    let mut lines: Vec<String> = [
        "/* AUTO-GENERATED by tofu-xtask contracts (packages/tofu-xtask).",
        " * Canonical source: contracts/events_v1.json (checked against the",
        " * lib/agent_core/events.py emission registry in both directions by",
        " * scripts/gen_event_contract.py). DO NOT EDIT BY HAND.",
        " *",
        " * Field-level streaming-event contract. Each interface mirrors the",
        " * FieldSpec tuple the backend validates at emission (build_event",
        " * raises on an undeclared field under TOFU_EVENT_SCHEMA=strict), so",
        " * reading an undeclared field here is a typecheck error — the two",
        " * sides of the contract cannot drift in either direction. The push",
        " * frame section mirrors owner-scoped push-hub frames the same way",
        " * (build_push_frame is the backend gate).",
        " */",
        "",
    ]
    .map(String::from)
    .to_vec();
    lines.push(format!(
        "export const EVENT_CONTRACT_VERSION = {} as const;",
        integer(
            field(root, "eventContractVersion", CONTRACT)?,
            "eventContractVersion"
        )?
    ));
    lines.push(String::new());
    let schemad = contracted(events)?;
    for (spec, schema) in &schemad {
        let event_type = text(spec, "type")?;
        lines.push(format!("/** {} */", ts_doc(text(spec, "purpose")?)));
        lines.push(format!(
            "export interface {} {{",
            interface_name(event_type)
        ));
        lines.push(format!("  type: '{event_type}';"));
        ts_fields(&mut lines, schema, descriptions(spec)?)?;
        lines.push("}".into());
        lines.push(String::new());
    }
    let mut members = Vec::new();
    for (spec, _) in &schemad {
        members.push(interface_name(text(spec, "type")?));
    }
    lines.push("/** Events with a field-level contract (the gated set). */".into());
    lines.push("export type ContractedWireEvent =".into());
    lines.push(if members.is_empty() {
        "  never;".into()
    } else {
        format!("  {};", members.join(" |\n  "))
    });
    lines.push(String::new());
    lines.extend(
        [
            "/** Any wire event; un-migrated types stay permissive. */",
            "export interface UnknownWireEvent {",
            "  type: string;",
            "  [field: string]: unknown;",
            "}",
            "",
        ]
        .map(String::from),
    );
    lines.push(format!(
        "export const PUSH_FRAME_CONTRACT_VERSION = {} as const;",
        integer(
            field(root, "pushFrameContractVersion", CONTRACT)?,
            "pushFrameContractVersion"
        )?
    ));
    lines.push(String::new());
    lines.push(
        "/** PushHub routing envelope; execution identity never replaces the subscription key. */"
            .into(),
    );
    lines.push("export interface PushTransportEnvelope {".into());
    let envelope = array(
        field(root, "pushTransportEnvelope", CONTRACT)?,
        "pushTransportEnvelope",
    )?;
    for (item, spec) in envelope.iter().zip(field_specs(
        field(root, "pushTransportEnvelope", CONTRACT)?,
        "pushTransportEnvelope",
    )?) {
        let description = text(object(item, "pushTransportEnvelope")?, "description")?;
        lines.push(format!("  /** {} */", ts_doc(description)));
        let optional = if spec.required { "" } else { "?" };
        lines.push(format!(
            "  {}{optional}: {};",
            spec.name,
            ts_type(spec.kind)?
        ));
    }
    lines.push("}".into());
    lines.push(String::new());
    let frames = array(field(root, "pushFrames", CONTRACT)?, "pushFrames")?;
    let push_specs = contracted(frames)?;
    for (spec, schema) in &push_specs {
        let frame_type = text(spec, "type")?;
        lines.push(format!(
            "/** {} (push channel `{}`, task `{}`) */",
            ts_doc(text(spec, "purpose")?),
            text(spec, "channel")?,
            ts_doc(text(spec, "task")?)
        ));
        lines.push(format!(
            "export interface {} {{",
            push_interface_name(frame_type)
        ));
        lines.push(format!("  type: '{frame_type}';"));
        ts_fields(&mut lines, schema, descriptions(spec)?)?;
        lines.push("}".into());
        lines.push(String::new());
    }
    let mut push_members = Vec::new();
    let mut type_literals = Vec::new();
    for (spec, _) in &push_specs {
        push_members.push(push_interface_name(text(spec, "type")?));
        type_literals.push(format!("'{}'", text(spec, "type")?));
    }
    lines.push("/** Declared owner-scoped push-hub frames. */".into());
    lines.push("export type ContractedPushFrame =".into());
    lines.push(if push_members.is_empty() {
        "  never;".into()
    } else {
        format!("  {};", push_members.join(" |\n  "))
    });
    lines.push(String::new());
    lines.push("/** Wire `type` tags of every declared push frame. */".into());
    lines.push("export const CONTRACTED_PUSH_FRAME_TYPES = [".into());
    lines.push(if type_literals.is_empty() {
        String::new()
    } else {
        format!("  {},", type_literals.join(",\n  "))
    });
    lines.push("] as const satisfies readonly ContractedPushFrame['type'][];".into());
    lines.push(String::new());
    Ok(lines.join("\n"))
}

fn rust_struct(
    name: &str,
    doc: &str,
    fields: &[FieldSpec],
    descriptions: &dyn Fn(&str) -> String,
) -> Result<Vec<String>, String> {
    let mut lines = Vec::new();
    if !doc.is_empty() {
        lines.push(format!("/// {}", doc_text(doc)));
    }
    lines.push("#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]".into());
    lines.push(format!("pub struct {name} {{"));
    let exact: Vec<&str> = fields
        .iter()
        .filter(|item| rust_field_name(item.name) == item.name)
        .map(|item| item.name)
        .collect();
    let mut seen: Vec<String> = Vec::new();
    for item in fields {
        let mut ident = rust_field_name(item.name);
        if ident != item.name && exact.contains(&ident.as_str()) {
            // `task_id` and `taskId` on one event: the camelCase twin keeps
            // its letters unsplit.
            ident = rust_field_name(&item.name.to_lowercase());
        }
        if seen.contains(&ident) {
            return Err(format!(
                "{name}: two wire fields map to Rust field {ident:?}"
            ));
        }
        seen.push(ident.clone());
        let (value_type, nullable) = rust_value_type(item.kind)?;
        let description = descriptions(item.name);
        if !description.is_empty() {
            lines.push(format!("    /// {}", doc_text(&description)));
        }
        let mut attributes = vec![format!("rename = {}", rust_string(item.name))];
        let rust_type = if item.required {
            if nullable {
                format!("Option<{value_type}>")
            } else {
                value_type
            }
        } else if nullable || value_type == "serde_json::Value" {
            // Absent and explicit null are different wire facts.
            let inner = if nullable {
                format!("Option<{value_type}>")
            } else {
                value_type
            };
            attributes.extend([
                "default".to_string(),
                "deserialize_with = \"crate::wire::present\"".to_string(),
                "skip_serializing_if = \"Option::is_none\"".to_string(),
            ]);
            format!("Option<{inner}>")
        } else {
            attributes.extend([
                "default".to_string(),
                "skip_serializing_if = \"Option::is_none\"".to_string(),
            ]);
            format!("Option<{value_type}>")
        };
        lines.push(format!("    #[serde({})]", attributes.join(", ")));
        lines.push(format!("    pub {ident}: {rust_type},"));
    }
    lines.push("}".into());
    lines.push(String::new());
    Ok(lines)
}

fn str_slice(values: &Value, what: &str) -> Result<String, String> {
    let mut literals = Vec::new();
    for value in array(values, what)? {
        literals.push(rust_string(string(value, what)?));
    }
    Ok(format!("&[{}]", literals.join(", ")))
}

pub fn render_rust(document: &Value) -> Result<String, String> {
    let root = object(document, CONTRACT)?;
    let get = |key: &str| field(root, key, CONTRACT);
    let mut lines: Vec<String> = [
        "//! @generated by tofu-xtask contracts (packages/tofu-xtask) from contracts/events_v1.json; do not edit.",
        "//!",
        "//! Streaming task events and owner-scoped push frames. Contracted events",
        "//! have field-level serde types; every registered type has metadata.",
        "",
        "use serde::{Deserialize, Serialize};",
        "",
    ]
    .map(String::from)
    .to_vec();
    lines.push(format!(
        "pub const CONTRACT_ID: &str = {};",
        rust_string(string(get("contract")?, "contract")?)
    ));
    lines.push(format!(
        "pub const EVENT_CONTRACT_VERSION: u32 = {};",
        integer(get("eventContractVersion")?, "eventContractVersion")?
    ));
    lines.push(format!(
        "pub const PUSH_FRAME_CONTRACT_VERSION: u32 = {};",
        integer(get("pushFrameContractVersion")?, "pushFrameContractVersion")?
    ));
    lines.push(format!(
        "pub const FIELD_KINDS: &[&str] = {};",
        str_slice(get("fieldKinds")?, "fieldKinds")?
    ));
    lines.push(format!(
        "pub const EVENT_CATEGORIES: &[&str] = {};",
        str_slice(get("categories")?, "categories")?
    ));
    lines.push("/// Stream-internal types an application frontend need not handle.".into());
    lines.push(format!(
        "pub const TRANSPORT_TYPES: &[&str] = {};",
        str_slice(get("transportTypes")?, "transportTypes")?
    ));
    lines.push("/// Types stamped with `emittedAt` at construction.".into());
    lines.push(format!(
        "pub const CLOCK_STAMPED_TYPES: &[&str] = {};",
        str_slice(get("clockStampedTypes")?, "clockStampedTypes")?
    ));
    lines.push("/// Transport stamps composed into every contracted event schema.".into());
    let envelope_names: Vec<String> = field_specs(get("taskEventEnvelope")?, "taskEventEnvelope")?
        .iter()
        .map(|item| rust_string(item.name))
        .collect();
    lines.push(format!(
        "pub const TASK_EVENT_ENVELOPE_FIELDS: &[&str] = &[{}];",
        envelope_names.join(", ")
    ));
    lines.extend(
        [
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub enum ReadPolicy {",
            "    Durable,",
            "    Informational,",
            "}",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct EventMetadata {",
            "    pub event_type: &'static str,",
            "    pub category: &'static str,",
            "    pub terminal: bool,",
            "    pub requires_response: bool,",
            "    pub read_policy: ReadPolicy,",
            "    pub since: u32,",
            "    pub contracted: bool,",
            "}",
            "",
            "/// Every registered event type, in registry order.",
            "pub static EVENTS: &[EventMetadata] = &[",
        ]
        .map(String::from),
    );
    let events = array(get("events")?, "events")?;
    for spec in events {
        let spec = object(spec, "events")?;
        let what = text(spec, "type")?;
        let read_policy = match text(spec, "readPolicy")? {
            "durable" => "Durable",
            "informational" => "Informational",
            other => return Err(format!("{what}: unknown readPolicy {other:?}")),
        };
        lines.extend([
            "    EventMetadata {".to_string(),
            format!("        event_type: {},", rust_string(what)),
            format!(
                "        category: {},",
                rust_string(text(spec, "category")?)
            ),
            format!(
                "        terminal: {},",
                boolean(field(spec, "terminal", what)?, what)?
            ),
            format!(
                "        requires_response: {},",
                boolean(field(spec, "requiresResponse", what)?, what)?
            ),
            format!("        read_policy: ReadPolicy::{read_policy},"),
            format!(
                "        since: {},",
                integer(field(spec, "since", what)?, what)?
            ),
            format!(
                "        contracted: {},",
                !field(spec, "schema", what)?.is_null()
            ),
            "    },".to_string(),
        ]);
    }
    lines.extend(
        [
            "];",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct PhaseMetadata {",
            "    pub phase: &'static str,",
            "    pub domains: &'static [&'static str],",
            "    pub since: u32,",
            "}",
            "",
            "/// Declared `phase` values of the `phase` event.",
            "pub static PHASES: &[PhaseMetadata] = &[",
        ]
        .map(String::from),
    );
    for spec in array(get("phases")?, "phases")? {
        let spec = object(spec, "phases")?;
        let phase = string(field(spec, "phase", "phases")?, "phase")?;
        lines.push(format!(
            "    PhaseMetadata {{ phase: {}, domains: {}, since: {} }},",
            rust_string(phase),
            str_slice(field(spec, "domains", phase)?, phase)?,
            integer(field(spec, "since", phase)?, phase)?
        ));
    }
    lines.extend(
        [
            "];",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct PushFrameMetadata {",
            "    pub frame_type: &'static str,",
            "    pub channel: &'static str,",
            "    pub task: &'static str,",
            "    pub since: u32,",
            "    pub contracted: bool,",
            "}",
            "",
            "pub static PUSH_FRAMES: &[PushFrameMetadata] = &[",
        ]
        .map(String::from),
    );
    let frames = array(get("pushFrames")?, "pushFrames")?;
    for spec in frames {
        let spec = object(spec, "pushFrames")?;
        let what = text(spec, "type")?;
        lines.extend([
            "    PushFrameMetadata {".to_string(),
            format!("        frame_type: {},", rust_string(what)),
            format!("        channel: {},", rust_string(text(spec, "channel")?)),
            format!("        task: {},", rust_string(text(spec, "task")?)),
            format!(
                "        since: {},",
                integer(field(spec, "since", what)?, what)?
            ),
            format!(
                "        contracted: {},",
                !field(spec, "schema", what)?.is_null()
            ),
            "    },".to_string(),
        ]);
    }
    lines.extend(
        [
            "];",
            "",
            "pub fn event_metadata(event_type: &str) -> Option<&'static EventMetadata> {",
            "    EVENTS.iter().find(|event| event.event_type == event_type)",
            "}",
            "",
            "pub fn push_frame_metadata(frame_type: &str) -> Option<&'static PushFrameMetadata> {",
            "    PUSH_FRAMES.iter().find(|frame| frame.frame_type == frame_type)",
            "}",
            "",
        ]
        .map(String::from),
    );
    let envelope = array(get("pushTransportEnvelope")?, "pushTransportEnvelope")?;
    let envelope_descriptions = |name: &str| -> String {
        envelope
            .iter()
            .find(|item| item.get("name").and_then(Value::as_str) == Some(name))
            .and_then(|item| item.get("description"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    };
    lines.extend(rust_struct(
        "PushTransportEnvelope",
        "PushHub routing envelope; execution identity never replaces the subscription key.",
        &field_specs(get("pushTransportEnvelope")?, "pushTransportEnvelope")?,
        &envelope_descriptions,
    )?);
    let schemad = contracted(events)?;
    for (spec, schema) in &schemad {
        let fields = descriptions(spec)?;
        lines.extend(rust_struct(
            &format!("{}Event", rust_type_name(text(spec, "type")?)),
            text(spec, "purpose")?,
            schema,
            &|name| description(fields, name).to_string(),
        )?);
    }
    lines.extend(
        [
            "/// A wire event with a field-level contract, tagged by `type`.",
            "#[allow(clippy::large_enum_variant)]",
            "#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]",
            "#[serde(tag = \"type\")]",
            "pub enum ContractedEvent {",
        ]
        .map(String::from),
    );
    for (spec, _) in &schemad {
        let event_type = text(spec, "type")?;
        let variant = rust_type_name(event_type);
        lines.push(format!(
            "    #[serde(rename = {})]",
            rust_string(event_type)
        ));
        lines.push(format!("    {variant}({variant}Event),"));
    }
    lines.push("}".into());
    lines.push(String::new());
    let push_specs = contracted(frames)?;
    for (spec, schema) in &push_specs {
        let fields = descriptions(spec)?;
        lines.extend(rust_struct(
            &format!("{}PushFrame", rust_type_name(text(spec, "type")?)),
            &format!(
                "{} (push channel `{}`, task `{}`)",
                text(spec, "purpose")?,
                text(spec, "channel")?,
                text(spec, "task")?
            ),
            schema,
            &|name| description(fields, name).to_string(),
        )?);
    }
    lines.extend(
        [
            "/// A declared owner-scoped push-hub frame, tagged by `type`.",
            "#[allow(clippy::large_enum_variant)]",
            "#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]",
            "#[serde(tag = \"type\")]",
            "pub enum ContractedPushFrame {",
        ]
        .map(String::from),
    );
    for (spec, _) in &push_specs {
        let frame_type = text(spec, "type")?;
        let variant = rust_type_name(frame_type);
        lines.push(format!(
            "    #[serde(rename = {})]",
            rust_string(frame_type)
        ));
        lines.push(format!("    {variant}({variant}PushFrame),"));
    }
    lines.push("}".into());
    lines.push(String::new());
    Ok(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interface_names_capitalize_each_part() {
        assert_eq!(interface_name("tool_start"), "ToolStartEvent");
        assert_eq!(interface_name("a__b"), "A_BEvent");
        assert_eq!(
            push_interface_name("codex.reset_offer.updated"),
            "CodexResetOfferUpdatedPushFrame"
        );
    }

    #[test]
    fn kinds_map_to_one_rust_type() {
        assert_eq!(
            rust_value_type("int | number").unwrap(),
            ("serde_json::Number".into(), false)
        );
        assert_eq!(
            rust_value_type("str | None").unwrap(),
            ("String".into(), true)
        );
        assert_eq!(
            rust_value_type("str | int").unwrap(),
            ("serde_json::Value".into(), false)
        );
        assert_eq!(
            rust_value_type("None").unwrap(),
            ("serde_json::Value".into(), true)
        );
        assert!(rust_value_type("float").is_err());
        assert_eq!(ts_type("int | number | None").unwrap(), "number | null");
    }
}
