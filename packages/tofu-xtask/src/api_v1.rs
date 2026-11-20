//! `contracts/api_v1.yaml` -> `packages/tofu-contracts/src/generated_api_v1.rs`
//! and `contracts/api_endpoint_index_v1.json`.
//!
//! Mapping from the JSON Schema subset the contract uses (shape only; value
//! constraints such as lengths, bounds and patterns stay validated by the server):
//!   * `type: object` with `properties`: a struct. A required property is a
//!     plain field, an optional one `Option<T>` omitted when absent; an optional
//!     property that also admits `null` is `Option<Option<T>>` so absent and
//!     null stay different. Unless `additionalProperties: false`, unknown keys
//!     are kept in a flattened `extra` map, so a decode/encode round trip never
//!     drops fields.
//!   * `type: object` without properties: a map of the `additionalProperties`
//!     schema.
//!   * a string `enum`: a Rust enum with one renamed variant per value.
//!   * `type: [T, "null"]`, or `anyOf`/`oneOf` of one schema and
//!     `{type: null}`: `Option<T>`.
//!   * `array`: `Vec<items>`; `integer`: `i64`; `number`:
//!     `serde_json::Number` (keeps `1` and `1.0` distinct); `string`,
//!     `const` strings: `String`; `boolean`: `bool`.
//!   * Any other union (`oneOf`/`anyOf`/`allOf` of several schemas, mixed
//!     type lists) or an empty schema: `serde_json::Value`. Union variants are
//!     not distinguishable by shape without a declared discriminator, and an
//!     untagged guess could silently drop fields.
//!
//! Named items: one per `components.schemas` entry, one per local `$defs`
//! target reached through a `#/paths/...` reference, and per operation
//! `<Op>Params` (path and query parameters), `<Op>Request` (JSON request body)
//! and `<Op>Response<status>` (JSON response bodies). A struct field whose type
//! reaches its own struct again is boxed.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};

use crate::json::{array, boolean, field, integer, object, string, truthy, Object};
use crate::names::{doc_text, rust_field_name, rust_string, rust_type_name};

pub const CONTRACT: &str = "contracts/api_v1.yaml";
pub const CONTRACT_ID: &str = "tofu.api-contract/v1";
pub const RUST_OUTPUT: &str = "packages/tofu-contracts/src/generated_api_v1.rs";
pub const INDEX_OUTPUT: &str = "contracts/api_endpoint_index_v1.json";
/// The index's `$comment`; `scripts/gen_api_endpoint_index.py` (the Python
/// route check) compares its projection, comment included, with the file.
pub const INDEX_COMMENT: &str =
    "AUTO-GENERATED from contracts/api_v1.yaml (x-tofu-route records) by \
tofu-xtask contracts (packages/tofu-xtask); checked against production route assembly and handler \
metadata by scripts/gen_api_endpoint_index.py. DO NOT EDIT.";

const METHODS: &[&str] = &[
    "get", "put", "post", "delete", "options", "head", "patch", "trace",
];
const VALUE: &str = "serde_json::Value";
const JSON_MEDIA: &[&str] = &["application/json", "application/problem+json"];
const ROUTE_KEY: &str = "x-tofu-route";
const UNPUBLISHED_KEY: &str = "x-tofu-unpublished-routes";

pub fn load(text: &str) -> Result<Value, String> {
    let contract: Value =
        serde_yaml::from_str(text).map_err(|error| format!("{CONTRACT}: {error}"))?;
    if contract.get("x-tofu-contract").and_then(Value::as_str) != Some(CONTRACT_ID) {
        return Err(format!("{CONTRACT} is not {CONTRACT_ID}"));
    }
    Ok(contract)
}

fn unescape(token: &str) -> String {
    token.replace("~1", "/").replace("~0", "~")
}

/// `kind in (None, 'string')` of the Python mapping: absent, null or "string".
fn none_or_string(kind: Option<&Value>) -> bool {
    matches!(kind, None | Some(Value::Null)) || kind.and_then(Value::as_str) == Some("string")
}

fn all_strings(values: &Value) -> bool {
    values
        .as_array()
        .is_some_and(|items| items.iter().all(Value::is_string))
}

fn description(schema: &Object) -> &str {
    schema
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
}

/// (item index, line index, field type) of one emitted struct field.
type FieldLine = (usize, usize, String);

struct Renderer<'c> {
    contract: &'c Value,
    items: Vec<Vec<String>>,
    pointer_names: HashMap<String, String>,
    used_names: HashSet<String>,
    component_names: HashMap<String, String>,
    /// Direct (non-indirected) struct field edges, for Box on cycles.
    direct_edges: HashMap<String, HashSet<String>>,
    /// (struct, its fields) in emission order.
    struct_fields: Vec<(String, Vec<FieldLine>)>,
}

impl<'c> Renderer<'c> {
    fn new(contract: &'c Value) -> Self {
        let mut renderer = Renderer {
            contract,
            items: Vec::new(),
            pointer_names: HashMap::new(),
            used_names: HashSet::new(),
            component_names: HashMap::new(),
            direct_edges: HashMap::new(),
            struct_fields: Vec::new(),
        };
        if let Some(schemas) = component_schemas(contract) {
            for name in schemas.keys() {
                let claimed = renderer.claim(&rust_type_name(name));
                renderer.component_names.insert(name.clone(), claimed);
            }
        }
        renderer
    }

    fn claim(&mut self, name: &str) -> String {
        let mut candidate = name.to_string();
        let mut suffix = 2;
        while self.used_names.contains(&candidate) {
            candidate = format!("{name}{suffix}");
            suffix += 1;
        }
        self.used_names.insert(candidate.clone());
        candidate
    }

    fn resolve(&self, pointer: &str) -> Result<&'c Value, String> {
        let Some(path) = pointer.strip_prefix("#/") else {
            return Err(format!("non-local reference in contract: {pointer:?}"));
        };
        let mut target = self.contract;
        for token in path.split('/') {
            target = target
                .as_object()
                .and_then(|map| map.get(&unescape(token)))
                .ok_or_else(|| format!("unresolvable reference in contract: {pointer:?}"))?;
        }
        Ok(target)
    }

    // ── schema → type expression ──────────────────────────────────────
    fn ref_type(&mut self, pointer: &str, hint: &str) -> Result<String, String> {
        if let Some(name) = pointer.strip_prefix("#/components/schemas/") {
            let name = unescape(name);
            return self
                .component_names
                .get(&name)
                .cloned()
                .ok_or_else(|| format!("reference to unknown component schema {name:?}"));
        }
        if let Some(name) = self.pointer_names.get(pointer) {
            return Ok(name.clone());
        }
        let last = pointer.rsplit('/').next().unwrap_or(pointer);
        let name = self.claim(&format!("{hint}{}", rust_type_name(&unescape(last))));
        self.pointer_names.insert(pointer.to_string(), name.clone());
        let target = self.resolve(pointer)?;
        self.named_item(&name, target, &format!("Schema at `{pointer}`."))?;
        Ok(name)
    }

    /// (non-null schema, true) when `schema` is `T | null`, else (schema, false).
    fn nullable_inner(schema: &Value) -> (Cow<'_, Value>, bool) {
        let Some(map) = schema.as_object() else {
            return (Cow::Borrowed(schema), false);
        };
        if let Some(Value::Array(kinds)) = map.get("type") {
            if kinds.iter().any(|kind| kind == "null") {
                let rest: Vec<Value> = kinds
                    .iter()
                    .filter(|kind| *kind != "null")
                    .cloned()
                    .collect();
                if rest.is_empty() {
                    return (Cow::Owned(json!({})), true);
                }
                let mut replaced = map.clone();
                let kind = if rest.len() == 1 {
                    rest.into_iter().next().expect("one kind")
                } else {
                    Value::Array(rest)
                };
                replaced.insert("type".to_string(), kind);
                return (Cow::Owned(Value::Object(replaced)), true);
            }
        }
        let null = json!({"type": "null"});
        for key in ["anyOf", "oneOf"] {
            if let Some(Value::Array(options)) = map.get(key) {
                let rest: Vec<&Value> = options.iter().filter(|item| **item != null).collect();
                if rest.len() == 1 && options.len() == 2 {
                    if let Some(reference) = rest[0].get("$ref") {
                        return (Cow::Owned(json!({"$ref": reference})), true);
                    }
                    let mut merged: Object = map
                        .iter()
                        .filter(|(name, _)| name.as_str() != key)
                        .map(|(name, value)| (name.clone(), value.clone()))
                        .collect();
                    if let Some(option) = rest[0].as_object() {
                        for (name, value) in option {
                            merged.insert(name.clone(), value.clone());
                        }
                    }
                    return (Cow::Owned(Value::Object(merged)), true);
                }
            }
        }
        (Cow::Borrowed(schema), false)
    }

    /// (Rust type of the non-null shape, nullable).
    fn type_of(&mut self, schema: &Value, hint: &str) -> Result<(String, bool), String> {
        if !schema.is_object() {
            return Ok((VALUE.to_string(), false));
        }
        let (schema, nullable) = Self::nullable_inner(schema);
        let map = schema.as_object().expect("nullable_inner keeps objects");
        if let Some(reference) = map.get("$ref") {
            let reference = string(reference, "$ref")?;
            return Ok((self.ref_type(reference, hint)?, nullable));
        }
        if map.contains_key("oneOf") || map.contains_key("anyOf") {
            return Ok((VALUE.to_string(), false));
        }
        if let Some(options) = map.get("allOf") {
            let options = array(options, "allOf")?;
            if options.len() == 1 && map.len() == 1 {
                return self.type_of(&options[0], hint);
            }
            return Ok((VALUE.to_string(), false));
        }
        let kind = map.get("type");
        if none_or_string(kind) && map.get("const").is_some_and(Value::is_string) {
            return Ok(("String".to_string(), nullable));
        }
        if kind.is_some_and(Value::is_array) {
            return Ok((VALUE.to_string(), false));
        }
        if none_or_string(kind) {
            if let Some(values) = map.get("enum") {
                if truthy(values) && all_strings(values) {
                    let name = self.claim(hint);
                    self.emit_enum(&name, map, "")?;
                    return Ok((name, nullable));
                }
                return Ok((VALUE.to_string(), false));
            }
        }
        let primitive = match kind.and_then(Value::as_str) {
            Some("string") => Some("String"),
            Some("integer") => Some("i64"),
            Some("number") => Some("serde_json::Number"),
            Some("boolean") => Some("bool"),
            _ => None,
        };
        if let Some(primitive) = primitive {
            return Ok((primitive.to_string(), nullable));
        }
        match kind.and_then(Value::as_str) {
            Some("null") => return Ok((VALUE.to_string(), false)),
            Some("array") => {
                let empty = json!({});
                let items = map.get("items").unwrap_or(&empty);
                let (mut item_type, item_nullable) = self.type_of(items, &format!("{hint}Item"))?;
                if item_nullable {
                    item_type = format!("Option<{item_type}>");
                }
                return Ok((format!("Vec<{item_type}>"), nullable));
            }
            _ => {}
        }
        if kind.and_then(Value::as_str) == Some("object") || map.contains_key("properties") {
            if map.get("properties").is_some_and(truthy) {
                let name = self.claim(hint);
                self.emit_struct(&name, map, "")?;
                return Ok((name, nullable));
            }
            let open = Value::Bool(true);
            let extra = map.get("additionalProperties").unwrap_or(&open);
            let is_empty_object = extra.as_object().is_some_and(Object::is_empty);
            if extra == &Value::Bool(true) || is_empty_object || extra == &Value::Bool(false) {
                return Ok((
                    "serde_json::Map<String, serde_json::Value>".to_string(),
                    nullable,
                ));
            }
            let (mut value_type, value_nullable) = self.type_of(extra, &format!("{hint}Value"))?;
            if value_nullable {
                value_type = format!("Option<{value_type}>");
            }
            return Ok((
                format!("std::collections::BTreeMap<String, {value_type}>"),
                nullable,
            ));
        }
        Ok((VALUE.to_string(), false))
    }

    // ── named items ──────────────────────────────────────────────────
    fn named_item(&mut self, name: &str, schema: &Value, doc: &str) -> Result<(), String> {
        if schema.is_object() {
            let (inner, nullable) = Self::nullable_inner(schema);
            let inner = inner.as_object().expect("nullable_inner keeps objects");
            if !inner.contains_key("$ref") && !nullable {
                let kind = inner.get("type");
                let doc = if doc.is_empty() {
                    description(inner)
                } else {
                    doc
                };
                if (kind.and_then(Value::as_str) == Some("object")
                    || inner.contains_key("properties"))
                    && inner.get("properties").is_some_and(truthy)
                    && !["oneOf", "anyOf", "allOf"]
                        .iter()
                        .any(|key| inner.contains_key(*key))
                {
                    return self.emit_struct(name, inner, doc);
                }
                if let Some(values) = inner.get("enum") {
                    if none_or_string(kind)
                        && truthy(values)
                        && !inner.contains_key("const")
                        && all_strings(values)
                    {
                        return self.emit_enum(name, inner, doc);
                    }
                }
            }
        }
        let (mut rust_type, nullable) = self.type_of(schema, &format!("{name}Inner"))?;
        if nullable {
            rust_type = format!("Option<{rust_type}>");
        }
        let mut lines = Vec::new();
        if !doc.is_empty() {
            lines.push(format!("/// {}", doc_text(doc)));
        }
        lines.push(format!("pub type {name} = {rust_type};"));
        self.items.push(lines);
        Ok(())
    }

    fn emit_enum(&mut self, name: &str, schema: &Object, doc: &str) -> Result<(), String> {
        let doc = if doc.is_empty() {
            description(schema)
        } else {
            doc
        };
        let mut lines = Vec::new();
        if !doc.is_empty() {
            lines.push(format!("/// {}", doc_text(doc)));
        }
        lines.push(
            "#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]".into(),
        );
        lines.push(format!("pub enum {name} {{"));
        let mut seen = HashSet::new();
        for value in array(field(schema, "enum", name)?, name)? {
            let Some(value) = value.as_str() else {
                continue;
            };
            let base = rust_type_name(value);
            let mut variant = base.clone();
            let mut suffix = 2;
            while seen.contains(&variant) {
                variant = format!("{base}{suffix}");
                suffix += 1;
            }
            seen.insert(variant.clone());
            lines.push(format!("    #[serde(rename = {})]", rust_string(value)));
            lines.push(format!("    {variant},"));
        }
        lines.push("}".into());
        self.items.push(lines);
        Ok(())
    }

    fn emit_struct(&mut self, name: &str, schema: &Object, doc: &str) -> Result<(), String> {
        let doc = if doc.is_empty() {
            description(schema)
        } else {
            doc
        };
        let properties = object(field(schema, "properties", name)?, name)?;
        let required: HashSet<&str> = match schema.get("required") {
            Some(values) => array(values, name)?
                .iter()
                .filter_map(Value::as_str)
                .collect(),
            None => HashSet::new(),
        };
        let closed = schema.get("additionalProperties") == Some(&Value::Bool(false));
        let mut lines = Vec::new();
        if !doc.is_empty() {
            lines.push(format!("/// {}", doc_text(doc)));
        }
        lines.push("#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]".into());
        if closed {
            lines.push("#[serde(deny_unknown_fields)]".into());
        }
        lines.push(format!("pub struct {name} {{"));
        let index = self.items.len();
        self.items.push(lines); // reserve order; fields appended below
        let exact: HashSet<&str> = properties
            .keys()
            .filter(|wire| rust_field_name(wire) == **wire)
            .map(String::as_str)
            .collect();
        let mut seen: HashSet<String> = HashSet::new();
        if !closed {
            seen.insert("extra".into());
        }
        self.direct_edges.entry(name.to_string()).or_default();
        let fields_index = self.struct_fields.len();
        self.struct_fields.push((name.to_string(), Vec::new()));
        for (wire, property) in properties {
            let mut ident = rust_field_name(wire);
            if ident != *wire && exact.contains(ident.as_str()) {
                ident = rust_field_name(&wire.to_lowercase());
            }
            let base = ident.clone();
            let mut suffix = 2;
            while seen.contains(&ident) {
                ident = format!("{base}_{suffix}");
                suffix += 1;
            }
            seen.insert(ident.clone());
            let (value_type, nullable) =
                self.type_of(property, &format!("{name}{}", rust_type_name(wire)))?;
            let property_description = property
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("");
            let lines = &mut self.items[index];
            if !property_description.is_empty() {
                lines.push(format!("    /// {}", doc_text(property_description)));
            }
            let mut attributes = vec![format!("rename = {}", rust_string(wire))];
            let rust_type;
            if required.contains(wire.as_str()) {
                rust_type = if nullable {
                    format!("Option<{value_type}>")
                } else {
                    value_type.clone()
                };
                if value_type == VALUE && !nullable {
                    attributes.push("default".into());
                }
            } else if nullable || value_type == VALUE {
                let inner = if nullable {
                    format!("Option<{value_type}>")
                } else {
                    value_type.clone()
                };
                rust_type = format!("Option<{inner}>");
                attributes.extend([
                    "default".to_string(),
                    "deserialize_with = \"crate::wire::present\"".to_string(),
                    "skip_serializing_if = \"Option::is_none\"".to_string(),
                ]);
            } else {
                rust_type = format!("Option<{value_type}>");
                attributes.extend([
                    "default".to_string(),
                    "skip_serializing_if = \"Option::is_none\"".to_string(),
                ]);
            }
            lines.push(format!("    #[serde({})]", attributes.join(", ")));
            let line_index = lines.len();
            lines.push(format!("    pub {ident}: {rust_type},"));
            if self.used_names.contains(&value_type) {
                self.direct_edges
                    .get_mut(name)
                    .expect("edges registered")
                    .insert(value_type.clone());
            }
            self.struct_fields[fields_index]
                .1
                .push((index, line_index, value_type));
        }
        let lines = &mut self.items[index];
        if !closed {
            lines.push(
                "    /// Properties the schema does not name; kept for lossless round trips."
                    .into(),
            );
            lines.push("    #[serde(flatten)]".into());
            lines.push("    pub extra: serde_json::Map<String, serde_json::Value>,".into());
        }
        lines.push("}".into());
        Ok(())
    }

    fn reaches(&self, start: &str, goal: &str) -> bool {
        let mut stack = vec![start.to_string()];
        let mut seen = HashSet::new();
        while let Some(node) = stack.pop() {
            if node == goal {
                return true;
            }
            if !seen.insert(node.clone()) {
                continue;
            }
            if let Some(edges) = self.direct_edges.get(&node) {
                stack.extend(edges.iter().cloned());
            }
        }
        false
    }

    fn box_cycles(&mut self) {
        let mut replacements = Vec::new();
        for (name, fields) in &self.struct_fields {
            for (item_index, line_index, value_type) in fields {
                if self.direct_edges.contains_key(value_type) && self.reaches(value_type, name) {
                    replacements.push((*item_index, *line_index, value_type.clone()));
                }
            }
        }
        for (item_index, line_index, value_type) in replacements {
            // The field is `T`, `Option<T>` or `Option<Option<T>>`: box T once.
            let line = &mut self.items[item_index][line_index];
            for (from, to) in [
                (format!(": {value_type},"), format!(": Box<{value_type}>,")),
                (format!("<{value_type}>,"), format!("<Box<{value_type}>>,")),
                (
                    format!("<{value_type}>>,"),
                    format!("<Box<{value_type}>>>,"),
                ),
            ] {
                if line.ends_with(&from) {
                    *line = format!("{}{to}", &line[..line.len() - from.len()]);
                    break;
                }
            }
        }
    }
}

fn component_schemas(contract: &Value) -> Option<&Object> {
    contract.get("components")?.get("schemas")?.as_object()
}

/// (OpenAPI path or None, lower-case method, operation, route record) for every
/// registered operation: published paths first, then unpublished routes.
struct OperationRow<'c> {
    path: Option<&'c str>,
    method: String,
    operation: Option<&'c Object>,
    record: &'c Object,
}

fn operation_rows(contract: &Value) -> Result<Vec<OperationRow<'_>>, String> {
    let mut rows = Vec::new();
    for (path, item) in object(
        field(object(contract, CONTRACT)?, "paths", CONTRACT)?,
        "paths",
    )? {
        for (method, operation) in object(item, path)? {
            if !METHODS.contains(&method.as_str()) {
                continue;
            }
            let operation = object(operation, path)?;
            let what = format!("{} {path}", method.to_uppercase());
            rows.push(OperationRow {
                path: Some(path),
                method: method.clone(),
                operation: Some(operation),
                record: object(field(operation, ROUTE_KEY, &what)?, &what)?,
            });
        }
    }
    if let Some(unpublished) = contract.get(UNPUBLISHED_KEY) {
        for record in array(unpublished, UNPUBLISHED_KEY)? {
            let record = object(record, UNPUBLISHED_KEY)?;
            let method = string(field(record, "method", UNPUBLISHED_KEY)?, UNPUBLISHED_KEY)?;
            rows.push(OperationRow {
                path: None,
                method: method.to_lowercase(),
                operation: None,
                record,
            });
        }
    }
    Ok(rows)
}

fn optional_string(value: Option<&Value>, what: &str) -> Result<String, String> {
    match value {
        None | Some(Value::Null) => Ok("None".into()),
        Some(value) => Ok(format!("Some({})", rust_string(string(value, what)?))),
    }
}

pub fn render_rust(contract: &Value) -> Result<String, String> {
    let mut renderer = Renderer::new(contract);
    if let Some(schemas) = component_schemas(contract) {
        for (name, schema) in schemas {
            let claimed = renderer.component_names[name].clone();
            let doc = schema.as_object().map(description).unwrap_or("");
            renderer.named_item(&claimed, schema, doc)?;
        }
    }
    let rows = operation_rows(contract)?;
    for row in &rows {
        let (Some(path), Some(operation)) = (row.path, row.operation) else {
            continue;
        };
        let method = row.method.to_uppercase();
        let operation_id = string(field(operation, "operationId", path)?, path)?;
        let op_name = renderer.claim(&rust_type_name(operation_id));
        let mut properties = Object::new();
        let mut required = Vec::new();
        if let Some(parameters) = operation.get("parameters") {
            for parameter in array(parameters, path)? {
                let Some(parameter) = parameter.as_object() else {
                    continue;
                };
                let location = parameter.get("in").and_then(Value::as_str);
                if !matches!(location, Some("path" | "query")) {
                    continue;
                }
                let name = string(field(parameter, "name", path)?, path)?;
                if properties.contains_key(name) {
                    continue;
                }
                properties.insert(
                    name.to_string(),
                    parameter
                        .get("schema")
                        .cloned()
                        .unwrap_or_else(|| json!({})),
                );
                if parameter.get("required").is_some_and(truthy) {
                    required.push(Value::String(name.to_string()));
                }
            }
        }
        if !properties.is_empty() {
            let name = renderer.claim(&format!("{op_name}Params"));
            let schema = json!({
                "type": "object", "properties": properties, "required": required,
                "additionalProperties": false,
            });
            renderer.emit_struct(
                &name,
                schema.as_object().expect("object"),
                &format!("Path and query parameters of `{method} {path}`."),
            )?;
        }
        let mut body = operation.get("requestBody");
        if let Some(reference) = body.and_then(|body| body.get("$ref")) {
            body = Some(renderer.resolve(string(reference, "$ref")?)?);
        }
        if let Some(body) = body.filter(|body| truthy(body)) {
            for media in JSON_MEDIA {
                let schema = body
                    .get("content")
                    .and_then(|content| content.get(*media))
                    .and_then(|media| media.get("schema"))
                    .filter(|schema| !schema.is_null());
                if let Some(schema) = schema {
                    let name = renderer.claim(&format!("{op_name}Request"));
                    renderer.named_item(
                        &name,
                        schema,
                        &format!("JSON request body of `{method} {path}`."),
                    )?;
                    break;
                }
            }
        }
        if let Some(responses) = operation.get("responses") {
            for (status, response) in object(responses, path)? {
                if response.get("$ref").is_some() {
                    continue;
                }
                for media in JSON_MEDIA {
                    let schema = response
                        .get("content")
                        .and_then(|content| content.get(*media))
                        .and_then(|media| media.get("schema"))
                        .filter(|schema| !schema.is_null());
                    if let Some(schema) = schema {
                        let name =
                            renderer.claim(&format!("{op_name}Response{}", rust_type_name(status)));
                        renderer.named_item(
                            &name,
                            schema,
                            &format!("`{status}` JSON response of `{method} {path}`."),
                        )?;
                        break;
                    }
                }
            }
        }
    }
    renderer.box_cycles();

    let contract_map = object(contract, CONTRACT)?;
    let mut lines: Vec<String> = [
        "//! @generated by tofu-xtask contracts (packages/tofu-xtask) from contracts/api_v1.yaml; do not edit.",
        "//!",
        "//! Operation metadata for every registered HTTP/WebSocket route and serde",
        "//! types for the published request, response and error bodies. Mapping",
        "//! rules: packages/tofu-xtask/src/api_v1.rs.",
        "",
        "#![allow(clippy::large_enum_variant, clippy::enum_variant_names)]",
        "",
        "use serde::{Deserialize, Serialize};",
        "",
    ]
    .map(String::from)
    .to_vec();
    lines.push(format!(
        "pub const CONTRACT_ID: &str = {};",
        rust_string(string(
            field(contract_map, "x-tofu-contract", CONTRACT)?,
            CONTRACT
        )?)
    ));
    lines.push(format!(
        "pub const API_MAJOR: u32 = {};",
        integer(
            field(contract_map, "x-tofu-api-major", CONTRACT)?,
            "x-tofu-api-major"
        )?
    ));
    lines.push(format!(
        "pub const OPENAPI_VERSION: &str = {};",
        rust_string(string(field(contract_map, "openapi", CONTRACT)?, CONTRACT)?)
    ));
    lines.extend(
        [
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub enum Transport {",
            "    Http,",
            "    Websocket,",
            "}",
            "",
            "#[derive(Clone, Copy, Debug, Eq, PartialEq)]",
            "pub struct Operation {",
            "    pub method: &'static str,",
            "    /// OpenAPI path; `None` for registered routes outside the published surface.",
            "    pub path: Option<&'static str>,",
            "    /// Framework rule of the serving Python handler (`<int:id>` converters).",
            "    pub rule: &'static str,",
            "    pub operation_id: Option<&'static str>,",
            "    pub transport: Transport,",
            "    pub response_contract: Option<&'static str>,",
            "    pub raw_success: bool,",
            "    pub scope: Option<&'static str>,",
            "    pub public: Option<bool>,",
            "    pub required_scopes: Option<&'static [&'static str]>,",
            "    pub documented: bool,",
            "    pub owner: &'static str,",
            "    pub handler: &'static str,",
            "}",
            "",
            "/// Every registered operation, sorted by (rule, method, transport).",
            "pub static OPERATIONS: &[Operation] = &[",
        ]
        .map(String::from),
    );
    let mut sorted = Vec::with_capacity(rows.len());
    for row in &rows {
        let rule = string(field(row.record, "rule", ROUTE_KEY)?, ROUTE_KEY)?;
        let transport = string(field(row.record, "transport", ROUTE_KEY)?, ROUTE_KEY)?;
        sorted.push(((rule, row.method.to_uppercase(), transport), row));
    }
    sorted.sort_by(|left, right| left.0.cmp(&right.0));
    for ((rule, method, transport), row) in sorted {
        let record = row.record;
        let public = match record.get("public") {
            None | Some(Value::Null) => "None".to_string(),
            Some(value) => format!("Some({})", boolean(value, "public")?),
        };
        let scopes = match record.get("requiredScopes") {
            None | Some(Value::Null) => "None".to_string(),
            Some(values) => {
                let mut literals = Vec::new();
                for value in array(values, "requiredScopes")? {
                    literals.push(rust_string(string(value, "requiredScopes")?));
                }
                format!("Some(&[{}])", literals.join(", "))
            }
        };
        let operation_id = row
            .operation
            .and_then(|operation| operation.get("operationId"));
        let path = row.path.map(|path| Value::String(path.to_string()));
        lines.extend([
            "    Operation {".to_string(),
            format!("        method: {},", rust_string(&method)),
            format!("        path: {},", optional_string(path.as_ref(), "path")?),
            format!("        rule: {},", rust_string(rule)),
            format!(
                "        operation_id: {},",
                optional_string(operation_id, "operationId")?
            ),
            format!(
                "        transport: Transport::{},",
                if transport == "websocket" {
                    "Websocket"
                } else {
                    "Http"
                }
            ),
            format!(
                "        response_contract: {},",
                optional_string(record.get("responseContract"), "responseContract")?
            ),
            format!(
                "        raw_success: {},",
                record.get("rawSuccess").is_some_and(truthy)
            ),
            format!(
                "        scope: {},",
                optional_string(record.get("scope"), "scope")?
            ),
            format!("        public: {public},"),
            format!("        required_scopes: {scopes},"),
            format!(
                "        documented: {},",
                boolean(field(record, "documented", ROUTE_KEY)?, "documented")?
            ),
            format!(
                "        owner: {},",
                rust_string(string(field(record, "owner", ROUTE_KEY)?, "owner")?)
            ),
            format!(
                "        handler: {},",
                rust_string(string(field(record, "handler", ROUTE_KEY)?, "handler")?)
            ),
            "    },".to_string(),
        ]);
    }
    lines.extend(
        [
            "];",
            "",
            "pub fn operation(method: &str, rule: &str) -> Option<&'static Operation> {",
            "    OPERATIONS",
            "        .iter()",
            "        .find(|operation| operation.method == method && operation.rule == rule)",
            "}",
            "",
        ]
        .map(String::from),
    );
    for item in renderer.items {
        lines.extend(item);
        lines.push(String::new());
    }
    Ok(lines.join("\n"))
}

/// The endpoint index: every route record as `{method, path: rule, ...}`,
/// sorted by (path, method, transport).
pub fn render_index(contract: &Value) -> Result<String, String> {
    let mut endpoints = Vec::new();
    for row in operation_rows(contract)? {
        let mut entry = Object::new();
        entry.insert("method".into(), Value::String(row.method.to_uppercase()));
        entry.insert("path".into(), field(row.record, "rule", ROUTE_KEY)?.clone());
        for (key, value) in row.record {
            if key != "rule" && key != "method" {
                entry.insert(key.clone(), value.clone());
            }
        }
        endpoints.push(entry);
    }
    let key = |entry: &Object| -> (String, String, String) {
        let text = |name: &str| {
            entry
                .get(name)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        (text("path"), text("method"), text("transport"))
    };
    endpoints.sort_by_key(key);
    let contract_map = object(contract, CONTRACT)?;
    let mut document = Object::new();
    document.insert("$comment".into(), Value::String(INDEX_COMMENT.into()));
    document.insert(
        "contract".into(),
        Value::String("tofu.api-endpoint-index/v1".into()),
    );
    document.insert(
        "apiMajor".into(),
        field(contract_map, "x-tofu-api-major", CONTRACT)?.clone(),
    );
    document.insert(
        "scope".into(),
        field(contract_map, "x-tofu-scope", CONTRACT)?.clone(),
    );
    document.insert(
        "endpoints".into(),
        Value::Array(endpoints.into_iter().map(Value::Object).collect()),
    );
    document.insert("unresolved".into(), Value::Array(Vec::new()));
    let text = serde_json::to_string_pretty(&Value::Object(document))
        .map_err(|error| format!("{INDEX_OUTPUT}: {error}"))?;
    Ok(text + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contract(schemas: Value) -> Value {
        json!({
            "openapi": "3.1.0", "x-tofu-contract": CONTRACT_ID, "x-tofu-api-major": 4,
            "x-tofu-scope": "test", "paths": {}, "components": {"schemas": schemas},
        })
    }

    fn field_line(rendered: &str, name: &str) -> String {
        rendered
            .lines()
            .find(|line| line.trim_start().starts_with(&format!("pub {name}:")))
            .unwrap_or_else(|| panic!("no field {name}"))
            .trim()
            .to_string()
    }

    #[test]
    fn a_recursive_field_is_boxed_exactly_once() {
        let rendered = render_rust(&contract(json!({"Node": {
            "type": "object",
            "properties": {
                "child": {"$ref": "#/components/schemas/Node"},
                "next": {"$ref": "#/components/schemas/Node"},
                "maybe": {"anyOf": [{"$ref": "#/components/schemas/Node"}, {"type": "null"}]},
                "kids": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
            },
            "required": ["child"],
        }})))
        .unwrap();
        assert_eq!(field_line(&rendered, "child"), "pub child: Box<Node>,");
        assert_eq!(
            field_line(&rendered, "next"),
            "pub next: Option<Box<Node>>,"
        );
        assert_eq!(
            field_line(&rendered, "maybe"),
            "pub maybe: Option<Option<Box<Node>>>,"
        );
        assert_eq!(
            field_line(&rendered, "kids"),
            "pub kids: Option<Vec<Node>>,"
        );
    }

    #[test]
    fn absent_and_null_stay_distinct_and_open_objects_keep_extra() {
        let rendered = render_rust(&contract(json!({"Item": {
            "type": "object",
            "properties": {"note": {"type": ["string", "null"]}, "id": {"type": "integer"}},
            "required": ["id"],
        }})))
        .unwrap();
        assert_eq!(
            field_line(&rendered, "note"),
            "pub note: Option<Option<String>>,"
        );
        assert_eq!(field_line(&rendered, "id"), "pub id: i64,");
        assert!(rendered.contains("pub extra: serde_json::Map<String, serde_json::Value>,"));
    }
}
