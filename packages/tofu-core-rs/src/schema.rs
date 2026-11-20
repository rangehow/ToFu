//! Provider tool-schema projection: the final tool boundary
//! (`lib/tools/gateway.py::sanitize_wire_tools`), the root-object relaxation
//! every model receives (`_project_moonshot_root_object`), Kimi's MFJS
//! applicator rewrites (`_normalize_provider_schema_anyof_types`), its subset
//! projection and validator (`lib/tools/moonshot_schema.py`) and the strict
//! required/properties check (`_required_property_error`).
//!
//! A schema Python rejects drops its tool and records an isolation, exactly as
//! Python does; a Python exception (a `TypeError` from a malformed `required`)
//! is reproduced as `Error::Raised`. Only a non-function legacy envelope, a
//! non-string tool name and a non-object `properties` merged into an `anyOf`
//! branch return `Unsupported`.
//!
//! Python walks nested keywords in frozenset order, which varies per process;
//! it decides only which of several violations a dropped tool reports, so the
//! isolation `detail` is informative, not compared.

use serde_json::{json, Map, Value};

use crate::canonical::{Error, Result, Unsupported};
use crate::pyjson::{dumps_sorted_ascii, truthy};
use crate::route::is_kimi;

type Msg = Map<String, Value>;

/// A schema violation (the tool is dropped) or a core error that aborts the
/// request.
enum Fail {
    Schema(String),
    Core(Error),
}

impl From<Error> for Fail {
    fn from(error: Error) -> Self {
        Self::Core(error)
    }
}

type R<T> = std::result::Result<T, Fail>;

fn schema_err<T>(message: String) -> R<T> {
    Err(Fail::Schema(message))
}

fn type_error<T>(message: String) -> R<T> {
    Err(Fail::Core(Error::raised("TypeError", message)))
}

fn unsupported<T>(what: &str) -> R<T> {
    Err(Fail::Core(Unsupported(what.to_owned()).into()))
}

// ── Python value semantics ────────────────────────────────────────────────

fn number_of(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(flag) => Some(f64::from(u8::from(*flag))),
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

/// Python `==` on decoded JSON (`True == 1 == 1.0`).
fn py_eq(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => match (x.as_i64(), y.as_i64()) {
            (Some(x), Some(y)) => x == y,
            _ => match (x.as_u64(), y.as_u64()) {
                (Some(x), Some(y)) => x == y,
                _ => x.as_f64() == y.as_f64(),
            },
        },
        (Value::Bool(_) | Value::Number(_), Value::Bool(_) | Value::Number(_)) => {
            number_of(a) == number_of(b)
        }
        (Value::Array(x), Value::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(a, b)| py_eq(a, b))
        }
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(key, value)| y.get(key).is_some_and(|other| py_eq(value, other)))
        }
        _ => a == b,
    }
}

fn py_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn iterate(value: &Value) -> Option<Vec<Value>> {
    match value {
        Value::Array(items) => Some(items.clone()),
        Value::String(text) => Some(text.chars().map(|c| Value::String(c.into())).collect()),
        Value::Object(map) => Some(map.keys().map(|k| Value::String(k.clone())).collect()),
        _ => None,
    }
}

/// `list(value or ())`.
fn py_list(value: Option<&Value>) -> R<Vec<Value>> {
    match value {
        Some(value) if truthy(Some(value)) => match iterate(value) {
            Some(items) => Ok(items),
            None => type_error(format!("'{}' object is not iterable", py_type(value))),
        },
        _ => Ok(Vec::new()),
    }
}

/// `[*(value or ())]`.
fn py_star(value: Option<&Value>) -> R<Vec<Value>> {
    match value {
        Some(value) if truthy(Some(value)) => match iterate(value) {
            Some(items) => Ok(items),
            None => type_error(format!(
                "Value after * must be an iterable, not {}",
                py_type(value)
            )),
        },
        _ => Ok(Vec::new()),
    }
}

fn check_hashable<'a>(items: impl IntoIterator<Item = &'a Value>) -> R<()> {
    for item in items {
        if matches!(item, Value::Array(_) | Value::Object(_)) {
            return type_error(format!("unhashable type: '{}'", py_type(item)));
        }
    }
    Ok(())
}

fn py_contains(items: &[Value], item: &Value) -> bool {
    items.iter().any(|known| py_eq(known, item))
}

/// `list(dict.fromkeys(items))`.
fn fromkeys(items: Vec<Value>) -> R<Vec<Value>> {
    check_hashable(&items)?;
    let mut out: Vec<Value> = Vec::new();
    for item in items {
        if !py_contains(&out, &item) {
            out.push(item);
        }
    }
    Ok(out)
}

fn present<'a>(map: &'a Msg, key: &str) -> Option<&'a Value> {
    map.get(key).filter(|value| !value.is_null())
}

fn without_keys(map: &Msg, keys: &[&str]) -> Msg {
    map.iter()
        .filter(|(key, _)| !keys.contains(&key.as_str()))
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect()
}

fn object_root(rest: &Msg, drop: &[&str]) -> Msg {
    let mut projected = Msg::new();
    projected.insert("type".to_owned(), json!("object"));
    for (key, value) in rest {
        if key != "type" && !drop.contains(&key.as_str()) {
            projected.insert(key.clone(), value.clone());
        }
    }
    projected
}

// ── JSON-Schema type unions ───────────────────────────────────────────────

const INSTANCE_TYPES: [&str; 7] = [
    "array", "boolean", "integer", "null", "number", "object", "string",
];

/// `_schema_type_names`.
fn type_names(value: Option<&Value>, path: &str) -> R<Vec<String>> {
    let candidates: Vec<&Value> = match value {
        Some(Value::String(_)) => vec![value.expect("matched")],
        Some(Value::Array(items)) if !items.is_empty() => items.iter().collect(),
        _ => {
            return schema_err(format!(
                "{path}.type must be a type name or non-empty type array"
            ))
        }
    };
    let mut names: Vec<String> = Vec::new();
    for candidate in &candidates {
        let Value::String(name) = candidate else {
            return schema_err(format!("{path}.type must contain only type names"));
        };
        names.push(name.clone());
    }
    let invalid: Vec<&str> = names
        .iter()
        .map(String::as_str)
        .filter(|name| !INSTANCE_TYPES.contains(name))
        .collect();
    if !invalid.is_empty() {
        return schema_err(format!(
            "{path}.type contains unknown names: {}",
            invalid.join(", ")
        ));
    }
    let mut unique: Vec<String> = Vec::new();
    for name in names {
        if !unique.contains(&name) {
            unique.push(name);
        }
    }
    Ok(unique)
}

/// `_intersect_schema_types` (integer ⊂ number).
fn intersect_types(parent: &[String], branch: &[String]) -> Vec<String> {
    let has = |set: &[String], name: &str| set.iter().any(|n| n == name);
    let mut out: Vec<String> = ["null", "boolean", "object", "array", "string"]
        .iter()
        .filter(|name| has(parent, name) && has(branch, name))
        .map(|name| (*name).to_owned())
        .collect();
    let parent_number = has(parent, "number");
    let branch_number = has(branch, "number");
    if parent_number && branch_number {
        out.push("number".to_owned());
    } else if (parent_number || has(parent, "integer")) && (branch_number || has(branch, "integer"))
    {
        out.push("integer".to_owned());
    }
    out
}

fn type_value(types: &[String]) -> Value {
    if types.len() == 1 {
        Value::String(types[0].clone())
    } else {
        json!(types)
    }
}

fn allows_object(schema: &Msg, path: &str) -> R<bool> {
    if !schema.contains_key("type") {
        return Ok(true);
    }
    Ok(type_names(schema.get("type"), path)?
        .iter()
        .any(|n| n == "object"))
}

// ── root object projection (every model) ──────────────────────────────────

/// `_project_moonshot_root_object`: a sound plain-object relaxation of a root
/// union, intersection or non-object type.
fn project_root(schema: &Msg, path: &str) -> R<Msg> {
    if !allows_object(schema, path)? {
        return schema_err(format!("{path}.type must allow object for tool arguments"));
    }
    let combinators: Vec<&str> = ["anyOf", "oneOf", "allOf"]
        .into_iter()
        .filter(|key| schema.contains_key(*key))
        .collect();
    if combinators.len() > 1 {
        return Ok(object_root(schema, &combinators));
    }
    let Some(&combinator) = combinators.first() else {
        if schema.get("type").and_then(Value::as_str) == Some("object") {
            return Ok(schema.clone());
        }
        return Ok(object_root(schema, &[]));
    };
    let branches = match schema.get(combinator) {
        Some(Value::Array(branches)) if !branches.is_empty() => branches,
        _ => {
            return schema_err(format!(
                "{path}.{combinator} must be a non-empty schema array"
            ))
        }
    };
    let empty = Msg::new();
    let mut viable: Vec<&Msg> = Vec::new();
    for (index, branch) in branches.iter().enumerate() {
        let branch_path = format!("{path}.{combinator}[{index}]");
        let branch = match branch {
            Value::Bool(false) => {
                if combinator == "allOf" {
                    return Ok(object_root(schema, &[combinator]));
                }
                continue;
            }
            Value::Bool(true) => {
                viable.push(&empty);
                continue;
            }
            Value::Object(branch) => branch,
            _ => return schema_err(format!("{branch_path} must be an object schema")),
        };
        let branch_allows = allows_object(branch, &branch_path)?;
        if combinator == "allOf" && !branch_allows {
            return schema_err(format!(
                "{branch_path}.type is incompatible with object arguments"
            ));
        }
        if branch_allows {
            viable.push(branch);
        }
    }
    if viable.is_empty() {
        return schema_err(format!(
            "{path}.{combinator} has no object-compatible branch"
        ));
    }
    let parent_properties = match present(schema, "properties") {
        None => None,
        Some(Value::Object(map)) => Some(map),
        Some(_) => return schema_err(format!("{path}.properties must be an object")),
    };
    let mut merged = parent_properties.cloned().unwrap_or_default();
    let mut candidates: Vec<(String, Vec<Value>, usize)> = Vec::new();
    for (index, branch) in viable.iter().enumerate() {
        let properties = match present(branch, "properties") {
            None => continue,
            Some(Value::Object(map)) => map,
            Some(_) => {
                return schema_err(format!(
                    "{path}.{combinator}[{index}].properties must be an object"
                ))
            }
        };
        for (name, child) in properties {
            if merged.contains_key(name) {
                continue;
            }
            match candidates.iter_mut().find(|(known, _, _)| known == name) {
                Some((_, values, presence)) => {
                    values.push(child.clone());
                    *presence += 1;
                }
                None => candidates.push((name.clone(), vec![child.clone()], 1)),
            }
        }
    }
    for (name, values, presence) in candidates {
        let mut unique: Vec<Value> = Vec::new();
        for value in values {
            if !py_contains(&unique, &value) {
                unique.push(value);
            }
        }
        let value = if combinator == "allOf" {
            unique.swap_remove(0)
        } else if presence < viable.len() {
            json!({})
        } else if unique.len() > 1 {
            json!({ "anyOf": unique })
        } else {
            unique.swap_remove(0)
        };
        merged.insert(name, value);
    }
    let parent_required = py_list(schema.get("required"))?;
    let mut branch_required: Vec<Vec<Value>> = Vec::with_capacity(viable.len());
    for branch in &viable {
        branch_required.push(py_list(branch.get("required"))?);
    }
    check_hashable(branch_required.iter().flatten())?;
    let unconditional: Vec<Value> = if combinator == "allOf" {
        branch_required.iter().flatten().cloned().collect()
    } else {
        let mut names = branch_required[0].clone();
        for others in &branch_required[1..] {
            names.retain(|name| py_contains(others, name));
        }
        names
    };
    let mut required = parent_required;
    required.extend(
        branch_required
            .iter()
            .flatten()
            .filter(|name| py_contains(&unconditional, name))
            .cloned(),
    );
    let required = fromkeys(required)?;
    let mut projected = object_root(schema, &[combinator, "properties", "required"]);
    if !merged.is_empty() || parent_properties.is_some() {
        projected.insert("properties".to_owned(), Value::Object(merged));
    }
    if !required.is_empty() {
        projected.insert("required".to_owned(), Value::Array(required));
    }
    if !schema.contains_key("additionalProperties")
        && viable
            .iter()
            .all(|branch| branch.get("additionalProperties") == Some(&Value::Bool(false)))
    {
        projected.insert("additionalProperties".to_owned(), json!(false));
    }
    Ok(projected)
}

// ── Kimi applicator normalization ─────────────────────────────────────────

const MAP_OF_SCHEMAS: [&str; 5] = [
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
];
const LIST_OF_SCHEMAS: [&str; 4] = ["allOf", "anyOf", "oneOf", "prefixItems"];
const SINGLE_SCHEMA: [&str; 11] = [
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
];

fn merge_all_of(schema: &Msg, path: &str) -> R<Msg> {
    let all_of = match schema.get("allOf") {
        Some(Value::Array(all_of)) if !all_of.is_empty() => all_of,
        _ => return schema_err(format!("{path}.allOf must be a non-empty schema array")),
    };
    let mut merged = without_keys(schema, &["allOf"]);
    for (index, branch) in all_of.iter().enumerate() {
        let branch = match branch {
            Value::Bool(true) => continue,
            Value::Bool(false) => {
                merged = without_keys(schema, &["allOf"]);
                break;
            }
            Value::Object(branch) => branch,
            _ => return schema_err(format!("{path}.allOf[{index}] must be an object schema")),
        };
        for (key, value) in branch {
            let Some(current) = merged.get(key) else {
                merged.insert(key.clone(), value.clone());
                continue;
            };
            if py_eq(current, value) {
                continue;
            }
            let replacement = match (key.as_str(), current, value) {
                ("required", Value::Array(left), Value::Array(right)) => Some(Value::Array(
                    fromkeys(left.iter().chain(right).cloned().collect())?,
                )),
                ("properties", Value::Object(left), Value::Object(right)) => {
                    let mut combined = left.clone();
                    for (name, child) in right {
                        combined
                            .entry(name.clone())
                            .or_insert_with(|| child.clone());
                    }
                    Some(Value::Object(combined))
                }
                ("type", _, _) => {
                    let left = type_names(Some(current), path)?;
                    let right = type_names(Some(value), path)?;
                    let intersection = intersect_types(&left, &right);
                    if intersection.is_empty() {
                        return schema_err(format!(
                            "{path}.allOf has incompatible type constraints"
                        ));
                    }
                    Some(type_value(&intersection))
                }
                ("additionalProperties", _, _)
                    if current == &Value::Bool(false) || value == &Value::Bool(false) =>
                {
                    Some(json!(false))
                }
                _ => None,
            };
            if let Some(replacement) = replacement {
                merged.insert(key.clone(), replacement);
            }
        }
    }
    Ok(merged)
}

/// `type` beside `anyOf`, distributed over the branches. `None` when no
/// branch survives: Python then drops `anyOf` and returns without visiting
/// children.
fn distribute_type(result: &Msg, path: &str) -> R<Option<Msg>> {
    let any_of = match result.get("anyOf") {
        Some(Value::Array(any_of)) if !any_of.is_empty() => any_of,
        _ => return schema_err(format!("{path}.anyOf must be a non-empty schema array")),
    };
    let parent_type = &result["type"];
    let parent_types = type_names(Some(parent_type), path)?;
    let mut branches: Vec<Value> = Vec::new();
    for (index, branch) in any_of.iter().enumerate() {
        let branch_path = format!("{path}.anyOf[{index}]");
        let branch = match branch {
            Value::Bool(false) => continue,
            Value::Bool(true) => {
                branches.push(json!({ "type": parent_type }));
                continue;
            }
            Value::Object(branch) => branch,
            _ => return schema_err(format!("{branch_path} must be an object schema")),
        };
        let Some(branch_type) = branch.get("type") else {
            let mut typed = Msg::new();
            typed.insert("type".to_owned(), parent_type.clone());
            typed.extend(branch.clone());
            branches.push(Value::Object(typed));
            continue;
        };
        let branch_types = type_names(Some(branch_type), &branch_path)?;
        let intersection = intersect_types(&parent_types, &branch_types);
        if intersection.is_empty() {
            continue;
        }
        let normalized = type_value(&intersection);
        let mut branch = branch.clone();
        if !py_eq(&normalized, branch_type) {
            branch.insert("type".to_owned(), normalized);
        }
        branches.push(Value::Object(branch));
    }
    if branches.is_empty() {
        return Ok(None);
    }
    let mut distributed = without_keys(result, &["type"]);
    distributed.insert("anyOf".to_owned(), Value::Array(branches));
    Ok(Some(distributed))
}

/// Parent `properties`/`required`/`additionalProperties` folded into every
/// object-capable `anyOf` branch.
fn fold_into_branches(result: &Msg, path: &str) -> R<Option<Msg>> {
    let sibling_keys: Vec<&str> = ["properties", "required", "additionalProperties"]
        .into_iter()
        .filter(|key| result.contains_key(*key))
        .collect();
    let any_of = match result.get("anyOf") {
        Some(Value::Array(any_of)) if !any_of.is_empty() => any_of,
        _ => return Ok(None),
    };
    if sibling_keys.is_empty() || !any_of.iter().all(Value::is_object) {
        return Ok(None);
    }
    let mut folded: Vec<Value> = Vec::new();
    for branch in any_of {
        let branch = branch.as_object().expect("checked object");
        if branch.contains_key("type")
            && !type_names(branch.get("type"), path)?
                .iter()
                .any(|n| n == "object")
        {
            folded.push(Value::Object(branch.clone()));
            continue;
        }
        let mut merged = branch.clone();
        if let Some(Value::Object(parent)) = result.get("properties") {
            if !parent.is_empty() {
                let mut combined = parent.clone();
                match merged.get("properties") {
                    Some(Value::Object(own)) => {
                        for (name, child) in own {
                            combined.insert(name.clone(), child.clone());
                        }
                    }
                    Some(value) if truthy(Some(value)) => {
                        return unsupported("non-object properties folded into anyOf");
                    }
                    _ => {}
                }
                merged.insert("properties".to_owned(), Value::Object(combined));
            }
        }
        if let Some(Value::Array(parent)) = result.get("required") {
            if !parent.is_empty() {
                let mut names = parent.clone();
                names.extend(py_star(merged.get("required"))?);
                merged.insert("required".to_owned(), Value::Array(fromkeys(names)?));
            }
        }
        match present(result, "additionalProperties") {
            Some(Value::Bool(false)) => {
                merged.insert("additionalProperties".to_owned(), json!(false));
            }
            Some(value) if !merged.contains_key("additionalProperties") => {
                merged.insert("additionalProperties".to_owned(), value.clone());
            }
            _ => {}
        }
        folded.push(Value::Object(merged));
    }
    let mut out = Msg::new();
    for (key, value) in result {
        if key == "anyOf" {
            out.insert(key.clone(), Value::Array(folded.clone()));
        } else if !sibling_keys.contains(&key.as_str()) {
            out.insert(key.clone(), value.clone());
        }
    }
    Ok(Some(out))
}

/// `_normalize_provider_schema_anyof_types`.
fn normalize_applicators(schema: &Msg, path: &str) -> R<Msg> {
    let mut result = schema.clone();
    if result.contains_key("allOf") {
        if result.contains_key("anyOf") || result.contains_key("oneOf") {
            result.shift_remove("allOf");
        } else {
            result = merge_all_of(&result, path)?;
        }
    }
    if result.contains_key("oneOf") {
        if result.contains_key("anyOf") {
            result.shift_remove("oneOf");
        } else {
            if !matches!(result.get("oneOf"), Some(Value::Array(items)) if !items.is_empty()) {
                return schema_err(format!("{path}.oneOf must be a non-empty schema array"));
            }
            result = result
                .into_iter()
                .map(|(key, value)| {
                    if key == "oneOf" {
                        ("anyOf".to_owned(), value)
                    } else {
                        (key, value)
                    }
                })
                .collect();
        }
    }
    if let Some(constant) = result.shift_remove("const") {
        let (key, value) = match &constant {
            Value::String(_) | Value::Number(_) => ("enum", json!([constant])),
            Value::Bool(_) => ("type", json!("boolean")),
            Value::Null => ("type", json!("null")),
            Value::Object(_) => ("type", json!("object")),
            Value::Array(_) => ("type", json!("array")),
        };
        result.entry(key.to_owned()).or_insert(value);
    }
    if result.get("type").is_some_and(Value::is_array) && !result.contains_key("anyOf") {
        let names = type_names(result.get("type"), path)?;
        result.shift_remove("type");
        let branches: Vec<Value> = names.iter().map(|name| json!({ "type": name })).collect();
        result.insert("anyOf".to_owned(), Value::Array(branches));
    }
    if result.contains_key("type") && result.contains_key("anyOf") {
        match distribute_type(&result, path)? {
            Some(distributed) => result = distributed,
            None => {
                result.shift_remove("anyOf");
                return Ok(result);
            }
        }
    }
    if result.contains_key("anyOf") {
        if let Some(folded) = fold_into_branches(&result, path)? {
            result = folded;
        }
    }
    for key in MAP_OF_SCHEMAS {
        if let Some(Value::Object(children)) = result.get_mut(key) {
            for (name, child) in children.iter_mut() {
                if let Value::Object(map) = child {
                    *map = normalize_applicators(map, &format!("{path}.{key}.{name}"))?;
                }
            }
        }
    }
    for key in LIST_OF_SCHEMAS {
        if let Some(Value::Array(children)) = result.get_mut(key) {
            for (index, child) in children.iter_mut().enumerate() {
                if let Value::Object(map) = child {
                    *map = normalize_applicators(map, &format!("{path}.{key}[{index}]"))?;
                }
            }
        }
    }
    for key in SINGLE_SCHEMA {
        match result.get_mut(key) {
            Some(Value::Object(map)) => {
                *map = normalize_applicators(map, &format!("{path}.{key}"))?;
            }
            Some(Value::Array(items)) => {
                for (index, item) in items.iter_mut().enumerate() {
                    if let Value::Object(map) = item {
                        *map = normalize_applicators(map, &format!("{path}.{key}[{index}]"))?;
                    }
                }
            }
            _ => {}
        }
    }
    Ok(result)
}

// ── MFJS subset projection (lib/tools/moonshot_schema.py) ─────────────────

const MFJS_KEYS: [&str; 11] = [
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "default",
    "description",
    "enum",
    "items",
    "properties",
    "required",
    "type",
];
const MFJS_TYPES: [&str; 7] = INSTANCE_TYPES;

fn pointer_escape(value: &str) -> String {
    value.replace('~', "~0").replace('/', "~1")
}

fn pointer_unescape(value: &str) -> String {
    value.replace("~1", "/").replace("~0", "~")
}

fn enum_kind(value: &Value) -> &'static str {
    match value {
        Value::String(_) => "string",
        Value::Number(n) if n.is_f64() => "number",
        Value::Number(_) => "integer",
        _ => "",
    }
}

fn valid_enum(value: &Value) -> bool {
    let Value::Array(items) = value else {
        return false;
    };
    let Some(first) = items.first() else {
        return false;
    };
    let kind = enum_kind(first);
    !kind.is_empty() && items.iter().all(|item| enum_kind(item) == kind)
}

struct Definitions {
    combined: Msg,
    aliases: Vec<(String, String)>,
}

/// `_root_definitions`: root `$defs` plus legacy `definitions`, renamed on a
/// conflicting collision.
fn root_definitions(schema: &Msg) -> Definitions {
    let mut combined = Msg::new();
    let mut aliases = Vec::new();
    if let Some(Value::Object(modern)) = schema.get("$defs") {
        combined = modern.clone();
    }
    if let Some(Value::Object(legacy)) = schema.get("definitions") {
        for (name, child) in legacy {
            let mut target = name.clone();
            if combined
                .get(&target)
                .is_some_and(|known| !py_eq(known, child))
            {
                let stem = format!("legacy_{name}");
                target = stem.clone();
                let mut suffix = 2;
                while combined.contains_key(&target) {
                    target = format!("{stem}_{suffix}");
                    suffix += 1;
                }
            }
            combined
                .entry(target.clone())
                .or_insert_with(|| child.clone());
            aliases.push((
                format!("#/definitions/{}", pointer_escape(name)),
                format!("#/$defs/{}", pointer_escape(&target)),
            ));
        }
    }
    Definitions { combined, aliases }
}

/// `_project_ref`: the rewritten reference (empty when it cannot be kept)
/// and whether it changed.
fn project_ref(value: &Value, aliases: &[(String, String)], names: &Msg) -> (String, bool) {
    let Value::String(value) = value else {
        return (String::new(), true);
    };
    let mut rewritten = value.clone();
    for (old, new) in aliases {
        if value == old || value.starts_with(&format!("{old}/")) {
            rewritten = format!("{new}{}", &value[old.len()..]);
            break;
        }
    }
    if rewritten == "#" {
        let changed = &rewritten != value;
        return (rewritten, changed);
    }
    let Some(rest) = rewritten.strip_prefix("#/$defs/") else {
        return (String::new(), true);
    };
    let first = rest.split('/').next().unwrap_or("");
    if !names.contains_key(&pointer_unescape(first)) {
        return (String::new(), true);
    }
    let changed = &rewritten != value;
    (rewritten, changed)
}

struct MfjsProjection<'a> {
    definitions: &'a Definitions,
}

impl MfjsProjection<'_> {
    fn project_definitions(&self, path: &str) -> R<Value> {
        let mut out = Msg::new();
        for (name, child) in &self.definitions.combined {
            out.insert(
                name.clone(),
                self.node(child, &format!("{path}.$defs.{name}"), false)?,
            );
        }
        Ok(Value::Object(out))
    }

    /// `_project_node`.
    fn node(&self, schema: &Value, path: &str, root: bool) -> R<Value> {
        let map = match schema {
            Value::Bool(_) => return Ok(json!({})),
            Value::Object(map) => map,
            _ => return schema_err(format!("{path} must be an object schema")),
        };
        let mut out = Msg::new();
        let mut definitions_emitted = false;
        let mut saw_pattern_properties = false;
        let mut saw_tuple_items = false;
        for (key, value) in map {
            let key_path = format!("{path}.{key}");
            match key.as_str() {
                "definitions" => {
                    if root && value.is_object() && !definitions_emitted {
                        out.insert("$defs".to_owned(), self.project_definitions(path)?);
                        definitions_emitted = true;
                    }
                }
                "$defs" => {
                    if !root || definitions_emitted {
                        continue;
                    }
                    let projected = self.project_definitions(path)?;
                    if projected.as_object().is_some_and(|m| !m.is_empty()) || value.is_object() {
                        out.insert("$defs".to_owned(), projected);
                    }
                    definitions_emitted = true;
                }
                "patternProperties" => saw_pattern_properties = true,
                "prefixItems" => saw_tuple_items = true,
                "items" if value.is_array() => saw_tuple_items = true,
                key if !MFJS_KEYS.contains(&key) => {}
                "$ref" => {
                    let (projected, _) =
                        project_ref(value, &self.definitions.aliases, &self.definitions.combined);
                    if !projected.is_empty() {
                        out.insert(key.to_owned(), Value::String(projected));
                    }
                }
                "description" => {
                    if value.is_string() {
                        out.insert(key.to_owned(), value.clone());
                    }
                }
                "default" | "type" | "required" => {
                    out.insert(key.to_owned(), value.clone());
                }
                "enum" => {
                    if valid_enum(value) {
                        out.insert(key.to_owned(), value.clone());
                    }
                }
                "properties" => {
                    let Value::Object(properties) = value else {
                        return schema_err(format!("{key_path} must be an object"));
                    };
                    let mut projected = Msg::new();
                    for (name, child) in properties {
                        projected.insert(
                            name.clone(),
                            self.node(child, &format!("{key_path}.{name}"), false)?,
                        );
                    }
                    out.insert(key.to_owned(), Value::Object(projected));
                }
                "anyOf" => {
                    let branches = match value {
                        Value::Array(branches) if !branches.is_empty() => branches,
                        _ => return schema_err(format!("{key_path} must be a non-empty array")),
                    };
                    let mut projected = Vec::new();
                    let mut unconstrained = false;
                    for (index, branch) in branches.iter().enumerate() {
                        match branch {
                            Value::Bool(false) => continue,
                            Value::Bool(true) => {
                                unconstrained = true;
                                break;
                            }
                            _ => {}
                        }
                        let child = self.node(branch, &format!("{key_path}[{index}]"), false)?;
                        let empty = child.as_object().is_some_and(Map::is_empty);
                        projected.push(child);
                        if empty {
                            unconstrained = true;
                            break;
                        }
                    }
                    if !unconstrained && !projected.is_empty() {
                        out.insert(key.to_owned(), Value::Array(projected));
                    }
                }
                "additionalProperties" => {
                    let projected = match value {
                        Value::Bool(_) => value.clone(),
                        Value::Object(_) => self.node(value, &key_path, false)?,
                        _ => json!(true),
                    };
                    out.insert(key.to_owned(), projected);
                }
                "items" => {
                    let projected = match value {
                        Value::Object(_) => self.node(value, &key_path, false)?,
                        _ => json!({}),
                    };
                    out.insert(key.to_owned(), projected);
                }
                _ => unreachable!("every MFJS key is matched above"),
            }
        }
        if root && !self.definitions.combined.is_empty() && !definitions_emitted {
            out.insert("$defs".to_owned(), self.project_definitions(path)?);
        }
        if saw_pattern_properties && out.get("additionalProperties") != Some(&json!(true)) {
            out.insert("additionalProperties".to_owned(), json!(true));
        }
        if saw_tuple_items {
            out.insert("items".to_owned(), json!({}));
        }
        if let Some(Value::Array(required)) = out.get("required") {
            if required.iter().all(Value::is_string) {
                let mut properties = match out.get("properties") {
                    Some(Value::Object(map)) => map.clone(),
                    _ => Msg::new(),
                };
                let missing: Vec<String> = required
                    .iter()
                    .filter_map(Value::as_str)
                    .filter(|name| !properties.contains_key(*name))
                    .map(str::to_owned)
                    .collect();
                if !missing.is_empty() {
                    for name in missing {
                        properties.insert(name, json!({}));
                    }
                    out.insert("properties".to_owned(), Value::Object(properties));
                }
            }
        }
        Ok(Value::Object(out))
    }
}

/// `project_mfjs_schema`.
fn project_mfjs(schema: &Msg, path: &str) -> R<Value> {
    let definitions = root_definitions(schema);
    MfjsProjection {
        definitions: &definitions,
    }
    .node(&Value::Object(schema.clone()), path, true)
}

/// `_schema_error`: the first documented MFJS violation.
fn mfjs_error(schema: &Value, path: &str, root: bool, names: &Msg) -> Option<String> {
    let Value::Object(map) = schema else {
        return Some(format!("{path} must be an object schema"));
    };
    if let Some(key) = map.keys().find(|key| !MFJS_KEYS.contains(&key.as_str())) {
        return Some(format!("{path} has unsupported MFJS keyword: {key}"));
    }
    if root {
        if map.get("type").and_then(Value::as_str) != Some("object") {
            return Some(format!(
                "{path}.type is required and must be object for Kimi"
            ));
        }
        if map.contains_key("anyOf") {
            return Some(format!(
                "{path}.anyOf cannot remain at a tool-parameter root"
            ));
        }
    } else if map.contains_key("$defs") {
        return Some(format!("{path}.$defs is only supported at the schema root"));
    }
    if let Some(raw_type) = present(map, "type") {
        if !raw_type
            .as_str()
            .is_some_and(|name| MFJS_TYPES.contains(&name))
        {
            return Some(format!("{path}.type must be one MFJS type string"));
        }
    }
    if map.contains_key("type") && map.contains_key("anyOf") {
        return Some(format!(
            "{path}.type must be declared inside anyOf branches"
        ));
    }
    if let Some(Value::Array(branches)) = map.get("anyOf") {
        for key in ["properties", "required", "additionalProperties"] {
            if map.contains_key(key)
                && branches
                    .iter()
                    .any(|branch| branch.as_object().is_some_and(|b| b.contains_key(key)))
            {
                return Some(format!(
                    "{path}.{key} conflicts with {key} inside anyOf branches; declare it on one side only"
                ));
            }
        }
    }
    if map.get("description").is_some_and(|d| !d.is_string()) {
        return Some(format!("{path}.description must be a string"));
    }
    if map.get("enum").is_some_and(|e| !valid_enum(e)) {
        return Some(format!(
            "{path}.enum must contain one supported scalar type"
        ));
    }
    if let Some(reference) = present(map, "$ref") {
        let (projected, changed) = project_ref(reference, &[], names);
        if changed || projected.is_empty() {
            return Some(format!("{path}.$ref must target # or a root $defs entry"));
        }
    }
    if let Some(required) = present(map, "required") {
        let Some(required) = required
            .as_array()
            .filter(|names| names.iter().all(Value::is_string))
        else {
            return Some(format!("{path}.required must be an array of strings"));
        };
        let declared = match map.get("properties") {
            Some(Value::Object(properties)) => Some(properties),
            _ => None,
        };
        if let Some(missing) = required
            .iter()
            .filter_map(Value::as_str)
            .find(|name| !declared.is_some_and(|p| p.contains_key(*name)))
        {
            return Some(format!(
                "{path}.required has undeclared property: {missing}"
            ));
        }
    }
    if let Some(properties) = present(map, "properties") {
        let Value::Object(properties) = properties else {
            return Some(format!("{path}.properties must be an object"));
        };
        for (name, child) in properties {
            let error = mfjs_error(child, &format!("{path}.properties.{name}"), false, names);
            if error.is_some() {
                return error;
            }
        }
    }
    if let Some(any_of) = present(map, "anyOf") {
        let Some(branches) = any_of.as_array().filter(|b| !b.is_empty()) else {
            return Some(format!("{path}.anyOf must be a non-empty schema array"));
        };
        for (index, branch) in branches.iter().enumerate() {
            let error = mfjs_error(branch, &format!("{path}.anyOf[{index}]"), false, names);
            if error.is_some() {
                return error;
            }
        }
    }
    if let Some(additional) = present(map, "additionalProperties") {
        if !additional.is_boolean() {
            let error = mfjs_error(
                additional,
                &format!("{path}.additionalProperties"),
                false,
                names,
            );
            if error.is_some() {
                return error;
            }
        }
    }
    if let Some(items) = present(map, "items") {
        let error = mfjs_error(items, &format!("{path}.items"), false, names);
        if error.is_some() {
            return error;
        }
    }
    if let Some(definitions) = present(map, "$defs") {
        let Value::Object(definitions) = definitions else {
            return Some(format!("{path}.$defs must be an object"));
        };
        for (name, child) in definitions {
            let error = mfjs_error(child, &format!("{path}.$defs.{name}"), false, names);
            if error.is_some() {
                return error;
            }
        }
    }
    None
}

/// `mfjs_schema_error` at a tool-parameter root.
fn mfjs_schema_error(schema: &Msg, path: &str) -> Option<String> {
    let names = match schema.get("$defs") {
        Some(Value::Object(definitions)) => definitions.clone(),
        _ => Msg::new(),
    };
    mfjs_error(&Value::Object(schema.clone()), path, true, &names)
}

// ── strict required/properties check (every model) ────────────────────────

/// `_required_property_error`.
fn required_property_error(schema: &Msg, path: &str, inherited: &[String]) -> Option<String> {
    let own: Vec<String> = match schema.get("properties") {
        Some(Value::Object(properties)) => properties.keys().cloned().collect(),
        _ => Vec::new(),
    };
    if let Some(required) = present(schema, "required") {
        let Some(required) = required
            .as_array()
            .filter(|names| names.iter().all(Value::is_string))
        else {
            return Some(format!("{path}.required must be an array of strings"));
        };
        let missing: Vec<&str> = required
            .iter()
            .filter_map(Value::as_str)
            .filter(|name| !own.iter().chain(inherited).any(|known| known == name))
            .collect();
        if !missing.is_empty() {
            return Some(format!(
                "{path}.required references properties not declared at that level: {}",
                missing.join(", ")
            ));
        }
    }
    for key in MAP_OF_SCHEMAS {
        if let Some(Value::Object(children)) = schema.get(key) {
            for (name, child) in children {
                if let Value::Object(child) = child {
                    let error =
                        required_property_error(child, &format!("{path}.{key}.{name}"), &[]);
                    if error.is_some() {
                        return error;
                    }
                }
            }
        }
    }
    for key in LIST_OF_SCHEMAS {
        if let Some(Value::Array(children)) = schema.get(key) {
            for (index, child) in children.iter().enumerate() {
                if let Value::Object(child) = child {
                    let error =
                        required_property_error(child, &format!("{path}.{key}[{index}]"), &own);
                    if error.is_some() {
                        return error;
                    }
                }
            }
        }
    }
    for key in SINGLE_SCHEMA {
        match schema.get(key) {
            Some(Value::Object(child)) => {
                let error = required_property_error(child, &format!("{path}.{key}"), &[]);
                if error.is_some() {
                    return error;
                }
            }
            Some(Value::Array(items)) => {
                for (index, item) in items.iter().enumerate() {
                    if let Value::Object(item) = item {
                        let error =
                            required_property_error(item, &format!("{path}.{key}[{index}]"), &[]);
                        if error.is_some() {
                            return error;
                        }
                    }
                }
            }
            _ => {}
        }
    }
    None
}

// ── one tool through the wire boundary ────────────────────────────────────

const PARAMETERS_PATH: &str = "$.function.parameters";

/// `_wire_tool_schema_error` for a function tool.
fn wire_tool_schema_error(parameters: Option<&Value>, kimi: bool) -> Option<String> {
    let parameters = parameters.filter(|p| !p.is_null())?;
    let Value::Object(parameters) = parameters else {
        return Some(format!("{PARAMETERS_PATH} must be an object schema"));
    };
    required_property_error(parameters, PARAMETERS_PATH, &[]).or_else(|| {
        if kimi {
            mfjs_schema_error(parameters, PARAMETERS_PATH)
        } else {
            None
        }
    })
}

/// `_source_wire_tool_schema_error`.
fn source_wire_tool_schema_error(parameters: Option<&Value>, kimi: bool) -> Option<String> {
    if !kimi {
        return wire_tool_schema_error(parameters, false);
    }
    match parameters {
        None | Some(Value::Null) | Some(Value::Bool(true)) | Some(Value::Object(_)) => None,
        Some(Value::Bool(false)) => Some(format!(
            "{PARAMETERS_PATH} does not permit object tool arguments"
        )),
        Some(_) => Some(format!("{PARAMETERS_PATH} must be an object schema")),
    }
}

/// `_normalize_provider_tool_schema` for a function tool: the projected
/// `parameters`, or `None` when the source value is kept as is.
fn normalize_parameters(parameters: Option<&Value>, kimi: bool) -> R<Option<Value>> {
    let parameters = match parameters {
        None | Some(Value::Null) => return Ok(Some(json!({"type": "object", "properties": {}}))),
        Some(Value::Bool(true)) => return Ok(Some(json!({"type": "object"}))),
        Some(Value::Bool(false)) => {
            return schema_err(format!(
                "{PARAMETERS_PATH} does not permit object tool arguments"
            ))
        }
        Some(Value::Object(parameters)) => parameters,
        Some(_) => return Ok(None),
    };
    let root = project_root(parameters, PARAMETERS_PATH)?;
    if !kimi {
        return Ok(Some(Value::Object(root)));
    }
    let normalized = normalize_applicators(&root, PARAMETERS_PATH)?;
    Ok(Some(project_mfjs(&normalized, PARAMETERS_PATH)?))
}

/// One tool the boundary removed (`_report_tool_isolation`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Isolated {
    pub tool_name: String,
    pub reason_code: &'static str,
    pub detail: String,
}

fn isolated(tool_name: &str, reason_code: &'static str, detail: String) -> Isolated {
    let name = if tool_name.is_empty() {
        "unknown tool"
    } else {
        tool_name
    };
    Isolated {
        tool_name: name.chars().take(160).collect(),
        reason_code,
        detail: detail.chars().take(400).collect(),
    }
}

/// `_schema_name`: `str(name or '')` of the function (or legacy) envelope.
fn schema_name(tool: &Msg) -> Result<String> {
    let name = match tool.get("function") {
        Some(Value::Object(function)) => function.get("name"),
        _ => tool.get("name"),
    };
    match name {
        Some(Value::String(name)) => Ok(name.clone()),
        Some(value) if truthy(Some(value)) => {
            Err(Unsupported("non-string tool name".to_owned()).into())
        }
        _ => Ok(String::new()),
    }
}

/// `_project_decoded_result_schema`.
fn project_decoded_result_schema(keep: &mut Msg) -> Result<()> {
    let Some(declared) = keep.shift_remove("decoded_result_schema") else {
        return Ok(());
    };
    match declared {
        Value::Null => Ok(()),
        Value::Object(declared) => {
            let text = dumps_sorted_ascii(&Value::Object(declared));
            let function = keep["function"].as_object_mut().expect("function object");
            let description = function
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            function.insert(
                "description".to_owned(),
                Value::String(format!(
                    "{description}\nSuccessful decoded JSON value (producer declaration; coverage/freshness unknown): {text}"
                )),
            );
            Ok(())
        }
        _ => Err(Unsupported("invalid decoded_result_schema".to_owned()).into()),
    }
}

/// `sanitize_wire_tools`: the provider-compatible tools in order, plus every
/// tool the boundary dropped.
pub fn sanitize_wire_tools(tools: &[Value], model: &str) -> Result<(Vec<Value>, Vec<Isolated>)> {
    let kimi = is_kimi(model);
    let mut out = Vec::with_capacity(tools.len());
    let mut dropped = Vec::new();
    for tool in tools {
        let Value::Object(source) = tool else {
            dropped.push(isolated(
                "",
                "non_object_schema",
                format!("expected object, got {}", py_type(tool)),
            ));
            continue;
        };
        let name = schema_name(source)?;
        if name.is_empty() {
            dropped.push(isolated(
                "",
                "missing_tool_name",
                "tool schema has no function/name field".to_owned(),
            ));
            continue;
        }
        let Some(Value::Object(function)) = source.get("function") else {
            return Err(Unsupported("non-function tool".to_owned()).into());
        };
        let parameters = function.get("parameters");
        let mut keep = source.clone();
        let error = match source_wire_tool_schema_error(parameters, kimi) {
            Some(error) => Some(error),
            None => match normalize_parameters(parameters, kimi) {
                Err(Fail::Core(error)) => return Err(error),
                Err(Fail::Schema(error)) => Some(error),
                Ok(projected) => {
                    if let Some(projected) = projected {
                        keep["function"]
                            .as_object_mut()
                            .expect("function object")
                            .insert("parameters".to_owned(), projected);
                    }
                    wire_tool_schema_error(keep["function"].get("parameters"), kimi)
                }
            },
        };
        if let Some(error) = error {
            dropped.push(isolated(&name, "invalid_schema", error));
            continue;
        }
        if keep.get("type").and_then(Value::as_str) != Some("function") {
            keep.insert("type".to_owned(), json!("function"));
        }
        project_decoded_result_schema(&mut keep)?;
        out.push(Value::Object(keep));
    }
    Ok((out, dropped))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root(schema: Value) -> Value {
        match project_root(schema.as_object().unwrap(), "$") {
            Ok(projected) => Value::Object(projected),
            Err(_) => panic!("root projection failed"),
        }
    }

    #[test]
    fn root_one_of_relaxes_to_a_plain_object() {
        let schema = json!({"additionalProperties": false, "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
                            "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "type": "object"});
        assert_eq!(
            crate::pyjson::compact(&root(schema)),
            br#"{"type":"object","additionalProperties":false,"properties":{"a":{"type":"string"},"b":{"type":"string"}}}"#
        );
    }

    #[test]
    fn python_equality_merges_numbers_and_booleans() {
        assert!(py_eq(&json!(1), &json!(1.0)));
        assert!(py_eq(&json!(true), &json!(1)));
        assert!(!py_eq(&json!(true), &json!(2)));
        assert!(py_eq(&json!({"a": [1, 2.0]}), &json!({"a": [true, 2]})));
        assert_eq!(
            fromkeys(vec![json!(1), json!(true), json!(1.0)]).ok(),
            Some(vec![json!(1)])
        );
    }

    #[test]
    fn kimi_rejects_what_projection_cannot_repair() {
        let tools = [json!({"type": "function", "function": {"name": "t",
            "parameters": {"type": "object", "properties": {"p": {"type": 5}}}}})];
        let (kept, dropped) = sanitize_wire_tools(&tools, "kimi-k3").unwrap();
        assert!(kept.is_empty());
        assert_eq!(dropped[0].reason_code, "invalid_schema");
        assert_eq!(
            dropped[0].detail,
            "$.function.parameters.properties.p.type must be one MFJS type string"
        );
        let (kept, _) = sanitize_wire_tools(&tools, "glm-5.3").unwrap();
        assert_eq!(kept.len(), 1);
    }
}
