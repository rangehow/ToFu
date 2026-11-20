//! The paper-build wire authority (`contracts/paper_build_v1.schema.json`),
//! embedded at build time, and the boundary validation of
//! `lib/paper/builds/contract.py`.
//!
//! `validate` implements the JSON Schema keywords this contract uses and
//! reports the first failing field in the order Python `jsonschema` visits
//! the schema, as `Invalid paper build field: <path>`.

use super::{Fault, Outcome};
use regex::Regex;
use serde_json::{Map, Value};
use std::sync::OnceLock;

pub const SCHEMA: &str = include_str!("../../../../../contracts/paper_build_v1.schema.json");

pub fn schema() -> &'static Value {
    static CELL: OnceLock<Value> = OnceLock::new();
    CELL.get_or_init(|| serde_json::from_str(SCHEMA).expect("embedded contract is valid JSON"))
}

/// `x-resource-limits` of the paper build contract.
pub fn limit(name: &str) -> u64 {
    schema()["x-resource-limits"][name]
        .as_u64()
        .unwrap_or_else(|| panic!("unknown paper build limit {name}"))
}

/// `x-source-suffixes`: the lower-case suffixes a source snapshot copies.
pub fn source_suffixes() -> Vec<&'static str> {
    schema()["x-source-suffixes"]
        .as_array()
        .expect("x-source-suffixes")
        .iter()
        .filter_map(Value::as_str)
        .collect()
}

fn invalid(field: &str) -> Fault {
    Fault::build(
        "build_contract",
        &format!("Invalid paper build field: {field}"),
    )
}

pub fn validate(value: &Value, definition: &str) -> Outcome<()> {
    if let Some(version) = value.get("schema_version") {
        if !(version.is_i64() || version.is_u64()) {
            return Err(invalid("schema_version"));
        }
    }
    let target = &schema()["$defs"][definition];
    let mut path = Vec::new();
    if let Some(failed) = first_error(target, value, &mut path) {
        let field = if failed.is_empty() {
            definition.to_string()
        } else {
            failed.join(".")
        };
        return Err(invalid(&field));
    }
    if definition == "PaperBuildReceiptV1" {
        let names: Vec<&str> = value["artifacts"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| row["name"].as_str())
            .collect();
        let mut unique = names.clone();
        unique.sort_unstable();
        unique.dedup();
        if unique.len() != names.len() {
            return Err(Fault::build("build_contract", "Duplicate build artifact"));
        }
        let errors = value["diagnostics"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|row| row["severity"] == "error");
        if value["status"] == "success" && (!names.contains(&"document.pdf") || errors) {
            return Err(Fault::build(
                "build_contract",
                "Successful build requires a PDF and no errors",
            ));
        }
    }
    Ok(())
}

fn type_matches(value: &Value, name: &str) -> bool {
    match name {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "boolean" => value.is_boolean(),
        "null" => value.is_null(),
        "number" => value.is_number(),
        "integer" => {
            value.is_i64() || value.is_u64() || value.as_f64().is_some_and(|f| f.fract() == 0.0)
        }
        _ => false,
    }
}

fn number(value: &Value) -> Option<f64> {
    value.as_f64()
}

/// The instance path of the first failure, `None` when `value` is valid.
fn first_error(schema: &Value, value: &Value, path: &mut Vec<String>) -> Option<Vec<String>> {
    let rules = schema.as_object()?;
    for (keyword, rule) in rules {
        let failed = match keyword.as_str() {
            "$ref" => {
                let name = rule.as_str()?.strip_prefix("#/$defs/")?;
                if let Some(found) = first_error(&self::schema()["$defs"][name], value, path) {
                    return Some(found);
                }
                false
            }
            "type" => match rule {
                Value::String(name) => !type_matches(value, name),
                Value::Array(names) => !names
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|name| type_matches(value, name)),
                _ => false,
            },
            "const" => value != rule,
            "enum" => !rule
                .as_array()
                .is_some_and(|options| options.contains(value)),
            "required" => value.as_object().is_some_and(|object| {
                rule.as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .any(|key| !object.contains_key(key))
            }),
            "additionalProperties" if rule == &Value::Bool(false) => {
                let declared = rules.get("properties").and_then(Value::as_object);
                value.as_object().is_some_and(|object| {
                    object
                        .keys()
                        .any(|key| !declared.is_some_and(|d| d.contains_key(key)))
                })
            }
            "properties" => {
                if let (Some(object), Some(properties)) = (value.as_object(), rule.as_object()) {
                    if let Some(found) = descend_properties(object, properties, path) {
                        return Some(found);
                    }
                }
                false
            }
            "minLength" => value
                .as_str()
                .is_some_and(|s| (s.chars().count() as u64) < rule.as_u64().unwrap_or(0)),
            "maxLength" => value
                .as_str()
                .is_some_and(|s| s.chars().count() as u64 > rule.as_u64().unwrap_or(u64::MAX)),
            "pattern" => value.as_str().is_some_and(|s| {
                Regex::new(rule.as_str().unwrap_or(""))
                    .map(|pattern| !pattern.is_match(s))
                    .unwrap_or(true)
            }),
            "minimum" => {
                !value.is_boolean()
                    && number(value).is_some_and(|n| number(rule).is_some_and(|m| n < m))
            }
            "maximum" => {
                !value.is_boolean()
                    && number(value).is_some_and(|n| number(rule).is_some_and(|m| n > m))
            }
            "maxItems" => value
                .as_array()
                .is_some_and(|items| items.len() as u64 > rule.as_u64().unwrap_or(u64::MAX)),
            "items" => {
                for (index, item) in value.as_array().into_iter().flatten().enumerate() {
                    path.push(index.to_string());
                    let found = first_error(rule, item, path);
                    path.pop();
                    if found.is_some() {
                        return found;
                    }
                }
                false
            }
            "anyOf" => !rule
                .as_array()
                .into_iter()
                .flatten()
                .any(|option| first_error(option, value, &mut Vec::new()).is_none()),
            "allOf" => {
                for part in rule.as_array().into_iter().flatten() {
                    if let Some(found) = first_error(part, value, path) {
                        return Some(found);
                    }
                }
                false
            }
            "if" => {
                let branch = if first_error(rule, value, &mut Vec::new()).is_none() {
                    rules.get("then")
                } else {
                    rules.get("else")
                };
                if let Some(found) = branch.and_then(|branch| first_error(branch, value, path)) {
                    return Some(found);
                }
                false
            }
            _ => false,
        };
        if failed {
            return Some(path.clone());
        }
    }
    None
}

fn descend_properties(
    object: &Map<String, Value>,
    properties: &Map<String, Value>,
    path: &mut Vec<String>,
) -> Option<Vec<String>> {
    for (name, rule) in properties {
        if let Some(item) = object.get(name) {
            path.push(name.clone());
            let found = first_error(rule, item, path);
            path.pop();
            if found.is_some() {
                return found;
            }
        }
    }
    None
}
