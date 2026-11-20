//! Draft 2020-12 validation as `jsonschema.Draft202012Validator(schema)`
//! (4.x, no format assertion) performs it, for exactly the keywords used by
//! `contracts/automation_storage_v1.schema.json` and the action schemas of
//! `contracts/automation_actions_v1.json`: `$ref` (local JSON pointer),
//! `type`, `enum`, `const`, `properties`, `additionalProperties`, `required`,
//! `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`,
//! `exclusiveMinimum`, `items`, `minItems`, `maxItems`, `uniqueItems`,
//! `anyOf`, `oneOf`, `allOf`, `not`, `if`/`then`/`else`. Other keys are
//! annotations (`$schema`, `$id`, `$defs`, `x-*`).
//!
//! Errors are produced in Python's keyword (schema insertion) order with its
//! `ValidationError.message` text, so `next(iter_errors(...))` is `errors()[0]`.

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use regex::Regex;
use serde_json::{Number, Value};

use crate::json::{python_float_repr, python_repr};

/// Python `repr()` of a JSON-decoded instance.
pub fn repr(value: &Value) -> String {
    match value {
        Value::Number(number) if number.as_i64().is_none() && number.as_u64().is_none() => {
            python_float_repr(number.as_f64().unwrap_or(0.0))
        }
        other => python_repr(other),
    }
}

fn is_number(value: &Value) -> bool {
    matches!(value, Value::Number(_))
}

/// `Draft202012Validator.TYPE_CHECKER.is_type`.
fn is_type(value: &Value, kind: &str) -> bool {
    match kind {
        "object" => value.is_object(),
        "array" => value.is_array(),
        "string" => value.is_string(),
        "null" => value.is_null(),
        "boolean" => value.is_boolean(),
        "number" => is_number(value),
        "integer" => match value {
            Value::Number(number) => {
                number.as_i64().is_some()
                    || number.as_u64().is_some()
                    || number.as_f64().is_some_and(|float| float.is_finite() && float.fract() == 0.0)
            }
            _ => false,
        },
        _ => false,
    }
}

fn compare(left: &Number, right: &Number) -> Option<std::cmp::Ordering> {
    if let (Some(a), Some(b)) = (left.as_i64(), right.as_i64()) {
        return Some(a.cmp(&b));
    }
    if let (Some(a), Some(b)) = (left.as_u64(), right.as_u64()) {
        return Some(a.cmp(&b));
    }
    left.as_f64()?.partial_cmp(&right.as_f64()?)
}

/// `jsonschema._utils.equal`: Python `==` except that booleans only equal booleans.
pub fn equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::String(a), Value::String(b)) => a == b,
        (Value::String(_), _) | (_, Value::String(_)) => false,
        (Value::Array(a), Value::Array(b)) => a.len() == b.len() && a.iter().zip(b).all(|(x, y)| equal(x, y)),
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len() && a.iter().all(|(key, value)| b.get(key).is_some_and(|other| equal(value, other)))
        }
        (Value::Bool(a), Value::Bool(b)) => a == b,
        (Value::Bool(_), _) | (_, Value::Bool(_)) => false,
        (Value::Null, Value::Null) => true,
        (Value::Number(a), Value::Number(b)) => compare(a, b) == Some(std::cmp::Ordering::Equal),
        _ => false,
    }
}

/// `re.search(pattern, text)` for Python patterns: `(?![\s\S])` and `\Z`
/// are the absolute end; a bare `$` also matches before one final newline.
fn search(pattern: &str, text: &str) -> bool {
    static CACHE: OnceLock<Mutex<HashMap<String, Option<Regex>>>> = OnceLock::new();
    let mut cache = CACHE.get_or_init(Default::default).lock().unwrap_or_else(|poison| poison.into_inner());
    let compiled = cache.entry(pattern.to_owned()).or_insert_with(|| Regex::new(&translate(pattern)).ok());
    compiled.as_ref().is_some_and(|regex| regex.is_match(text))
}

fn translate(pattern: &str) -> String {
    let pattern = pattern.replace(r"(?![\s\S])", r"\z");
    let mut out = String::with_capacity(pattern.len());
    let mut characters = pattern.chars();
    let mut in_class = false;
    while let Some(character) = characters.next() {
        match character {
            '\\' => match characters.next() {
                Some('Z') => out.push_str(r"\z"),
                Some(escaped) => {
                    out.push('\\');
                    out.push(escaped);
                }
                None => out.push('\\'),
            },
            '[' if !in_class => {
                in_class = true;
                out.push('[');
            }
            ']' if in_class => {
                in_class = false;
                out.push(']');
            }
            '$' if !in_class => out.push_str(r"(?:\n?\z)"),
            '\0' => out.push_str(r"\x00"),
            other => out.push(other),
        }
    }
    out
}

pub struct Validator<'s> {
    root: &'s Value,
}

impl<'s> Validator<'s> {
    pub fn new(root: &'s Value) -> Self {
        Self { root }
    }

    fn resolve(&self, reference: &str) -> Option<&'s Value> {
        let pointer = reference.strip_prefix('#')?;
        if pointer.is_empty() {
            return Some(self.root);
        }
        let mut current = self.root;
        for token in pointer.strip_prefix('/')?.split('/') {
            let token = token.replace("~1", "/").replace("~0", "~");
            current = match current {
                Value::Object(map) => map.get(&token)?,
                Value::Array(items) => items.get(token.parse::<usize>().ok()?)?,
                _ => return None,
            };
        }
        Some(current)
    }

    pub fn is_valid(&self, schema: &'s Value, instance: &Value) -> bool {
        let mut errors = Vec::new();
        self.collect(schema, instance, &mut errors, true);
        errors.is_empty()
    }

    /// `[error.message for error in iter_errors(instance)]`.
    #[cfg(test)]
    pub fn errors(&self, schema: &'s Value, instance: &Value) -> Vec<String> {
        let mut errors = Vec::new();
        self.collect(schema, instance, &mut errors, false);
        errors
    }

    pub fn first_error(&self, schema: &'s Value, instance: &Value) -> Option<String> {
        let mut errors = Vec::new();
        self.collect(schema, instance, &mut errors, true);
        errors.into_iter().next()
    }

    fn collect(&self, schema: &'s Value, instance: &Value, out: &mut Vec<String>, first: bool) {
        let map = match schema {
            Value::Bool(true) => return,
            Value::Bool(false) => {
                out.push(format!("False schema does not allow {}", repr(instance)));
                return;
            }
            Value::Object(map) => map,
            _ => return,
        };
        for (keyword, value) in map {
            if first && !out.is_empty() {
                return;
            }
            self.keyword(map, keyword, value, instance, out, first);
        }
    }

    fn keyword(
        &self,
        schema: &'s serde_json::Map<String, Value>,
        keyword: &str,
        value: &'s Value,
        instance: &Value,
        out: &mut Vec<String>,
        first: bool,
    ) {
        match keyword {
            "$ref" => {
                if let Some(target) = value.as_str().and_then(|reference| self.resolve(reference)) {
                    self.collect(target, instance, out, first);
                } else {
                    out.push(format!("Unresolvable: {}", crate::json::python_str(value)));
                }
            }
            "type" => {
                let types: Vec<&Value> = match value {
                    Value::Array(items) => items.iter().collect(),
                    other => vec![other],
                };
                if !types.iter().any(|kind| kind.as_str().is_some_and(|kind| is_type(instance, kind))) {
                    let names: Vec<String> = types.iter().map(|kind| repr(kind)).collect();
                    out.push(format!("{} is not of type {}", repr(instance), names.join(", ")));
                }
            }
            "enum" => {
                let options = value.as_array().map(Vec::as_slice).unwrap_or_default();
                if options.iter().all(|option| !equal(option, instance)) {
                    out.push(format!("{} is not one of {}", repr(instance), repr(value)));
                }
            }
            "const" => {
                if !equal(instance, value) {
                    out.push(format!("{} was expected", repr(value)));
                }
            }
            "properties" => {
                let (Value::Object(object), Value::Object(properties)) = (instance, value) else { return };
                for (property, subschema) in properties {
                    if let Some(child) = object.get(property) {
                        self.collect(subschema, child, out, first);
                        if first && !out.is_empty() {
                            return;
                        }
                    }
                }
            }
            "additionalProperties" => {
                let Value::Object(object) = instance else { return };
                let declared = schema.get("properties").and_then(Value::as_object);
                let extras: Vec<&String> =
                    object.keys().filter(|key| !declared.is_some_and(|declared| declared.contains_key(*key))).collect();
                if value.is_object() {
                    for extra in extras {
                        self.collect(value, &object[extra], out, first);
                        if first && !out.is_empty() {
                            return;
                        }
                    }
                } else if value == &Value::Bool(false) && !extras.is_empty() {
                    let mut sorted = extras;
                    sorted.sort();
                    let names: Vec<String> = sorted.iter().map(|key| repr(&Value::String((*key).clone()))).collect();
                    let verb = if names.len() == 1 { "was" } else { "were" };
                    out.push(format!("Additional properties are not allowed ({} {verb} unexpected)", names.join(", ")));
                }
            }
            "required" => {
                let (Value::Object(object), Value::Array(required)) = (instance, value) else { return };
                for property in required {
                    if property.as_str().is_some_and(|property| !object.contains_key(property)) {
                        out.push(format!("{} is a required property", repr(property)));
                    }
                }
            }
            "minLength" | "maxLength" => {
                let (Value::String(text), Some(bound)) = (instance, value.as_u64()) else { return };
                let length = text.chars().count() as u64;
                if keyword == "minLength" && length < bound {
                    let message = if bound == 1 { "should be non-empty" } else { "is too short" };
                    out.push(format!("{} {message}", repr(instance)));
                } else if keyword == "maxLength" && length > bound {
                    let message = if bound == 0 { "is expected to be empty" } else { "is too long" };
                    out.push(format!("{} {message}", repr(instance)));
                }
            }
            "pattern" => {
                let (Value::String(text), Some(pattern)) = (instance, value.as_str()) else { return };
                if !search(pattern, text) {
                    out.push(format!("{} does not match {}", repr(instance), repr(value)));
                }
            }
            "minimum" | "maximum" | "exclusiveMinimum" | "exclusiveMaximum" => {
                let (Value::Number(number), Value::Number(bound)) = (instance, value) else { return };
                let Some(order) = compare(number, bound) else { return };
                use std::cmp::Ordering::{Equal, Greater, Less};
                let message = match (keyword, order) {
                    ("minimum", Less) => "is less than the minimum of",
                    ("maximum", Greater) => "is greater than the maximum of",
                    ("exclusiveMinimum", Less | Equal) => "is less than or equal to the minimum of",
                    ("exclusiveMaximum", Greater | Equal) => "is greater than or equal to the maximum of",
                    _ => return,
                };
                out.push(format!("{} {message} {}", repr(instance), repr(value)));
            }
            "items" => {
                let Value::Array(items) = instance else { return };
                if items.is_empty() {
                    return;
                }
                if value == &Value::Bool(false) {
                    let rest = if items.len() == 1 { repr(&items[0]) } else { repr(instance) };
                    out.push(format!("Expected at most 0 items but found {} extra: {rest}", items.len()));
                    return;
                }
                for item in items {
                    self.collect(value, item, out, first);
                    if first && !out.is_empty() {
                        return;
                    }
                }
            }
            "minItems" | "maxItems" => {
                let (Value::Array(items), Some(bound)) = (instance, value.as_u64()) else { return };
                let length = items.len() as u64;
                if keyword == "minItems" && length < bound {
                    let message = if bound == 1 { "should be non-empty" } else { "is too short" };
                    out.push(format!("{} {message}", repr(instance)));
                } else if keyword == "maxItems" && length > bound {
                    let message = if bound == 0 { "is expected to be empty" } else { "is too long" };
                    out.push(format!("{} {message}", repr(instance)));
                }
            }
            "uniqueItems" => {
                let Value::Array(items) = instance else { return };
                if value == &Value::Bool(true)
                    && items.iter().enumerate().any(|(index, item)| items[..index].iter().any(|seen| equal(seen, item)))
                {
                    out.push(format!("{} has non-unique elements", repr(instance)));
                }
            }
            "anyOf" => {
                let options = value.as_array().map(Vec::as_slice).unwrap_or_default();
                if !options.iter().any(|option| self.is_valid(option, instance)) {
                    out.push(format!("{} is not valid under any of the given schemas", repr(instance)));
                }
            }
            "oneOf" => {
                let options = value.as_array().map(Vec::as_slice).unwrap_or_default();
                let Some(first_valid) = options.iter().position(|option| self.is_valid(option, instance)) else {
                    out.push(format!("{} is not valid under any of the given schemas", repr(instance)));
                    return;
                };
                let mut more: Vec<&Value> =
                    options[first_valid + 1..].iter().filter(|option| self.is_valid(option, instance)).collect();
                if !more.is_empty() {
                    more.push(&options[first_valid]);
                    let reprs: Vec<String> = more.iter().map(|schema| repr(schema)).collect();
                    out.push(format!("{} is valid under each of {}", repr(instance), reprs.join(", ")));
                }
            }
            "allOf" => {
                for option in value.as_array().map(Vec::as_slice).unwrap_or_default() {
                    self.collect(option, instance, out, first);
                    if first && !out.is_empty() {
                        return;
                    }
                }
            }
            "not" => {
                if self.is_valid(value, instance) {
                    out.push(format!("{} should not be valid under {}", repr(instance), repr(value)));
                }
            }
            "if" => {
                if self.is_valid(value, instance) {
                    if let Some(then) = schema.get("then") {
                        self.collect(then, instance, out, first);
                    }
                } else if let Some(otherwise) = schema.get("else") {
                    self.collect(otherwise, instance, out, first);
                }
            }
            _ => {}
        }
    }
}

const STORAGE_SCHEMA: &str = include_str!("../../../../../contracts/automation_storage_v1.schema.json");

pub fn storage_schema() -> &'static Value {
    static SCHEMA: OnceLock<Value> = OnceLock::new();
    SCHEMA.get_or_init(|| serde_json::from_str(STORAGE_SCHEMA).expect("automation storage schema"))
}

/// `next(storage_input_validator(kind).iter_errors(instance), None) is None`:
/// the root schema with `oneOf: [{'$ref': '#/$defs/<kind>'}]`.
pub fn storage_input_valid(kind: &str, instance: &Value) -> bool {
    let root = storage_schema();
    let Some(definition) = root["$defs"].get(kind) else { return false };
    Validator::new(root).is_valid(definition, instance)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn messages_follow_python_jsonschema() {
        let schema = json!({"type": "object", "additionalProperties": false,
            "properties": {"startedAt": {"type": "number", "exclusiveMinimum": 0},
                "text": {"type": "string", "minLength": 1, "maxLength": 3}},
            "required": ["startedAt", "text"]});
        let validator = Validator::new(&schema);
        assert_eq!(validator.first_error(&schema, &json!(null)).unwrap(), "None is not of type 'object'");
        assert_eq!(
            validator.first_error(&schema, &json!({"startedAt": 0, "text": "", "b": 1, "a": 2})).unwrap(),
            "Additional properties are not allowed ('a', 'b' were unexpected)"
        );
        assert_eq!(
            validator.errors(&schema, &json!({"startedAt": 0.0, "text": ""})),
            vec!["0.0 is less than or equal to the minimum of 0", "'' should be non-empty"]
        );
        assert_eq!(validator.first_error(&schema, &json!({"startedAt": 1})).unwrap(), "'text' is a required property");
        assert!(validator.is_valid(&schema, &json!({"startedAt": 1.5, "text": "abc"})));
    }

    #[test]
    fn python_pattern_and_type_semantics() {
        assert!(search("^[a-f0-9]{2}(?![\\s\\S])", "ab"));
        assert!(!search("^[a-f0-9]{2}(?![\\s\\S])", "ab\n"));
        assert!(search("^task-start:[a-f0-9]{2}$", "task-start:ab\n"));
        assert!(!search("^[^\u{0}]+(?![\\s\\S])", "a\u{0}b"));
        assert!(is_type(&json!(2.0), "integer"));
        assert!(!is_type(&json!(true), "number"));
        assert!(!equal(&json!(true), &json!(1)));
        assert!(equal(&json!([1, {"a": 1.0}]), &json!([1.0, {"a": 1}])));
        assert!(storage_input_valid("eventDeliveryCursor", &json!({"index": 0, "afterKey": "", "processed": 0})));
        assert!(!storage_input_valid("eventDeliveryCursor", &json!({"index": 5, "afterKey": "", "processed": 0})));
    }
}
