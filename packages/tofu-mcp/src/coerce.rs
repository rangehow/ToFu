//! LLM-shaped argument coercion to a tool's input schema
//! (`lib/mcp/client/_coerce.py`, the Python oracle).
//!
//! The oracle's `anyOf`/`oneOf` choice tests object identity (`coerced is not
//! value`); [`coerce_one`] returns that fact alongside the value.

use serde_json::{Map, Value};

fn declared_type(schema: &Map<String, Value>) -> Option<&str> {
    match schema.get("type") {
        Some(Value::String(t)) => Some(t),
        Some(Value::Array(types)) => types
            .iter()
            .find(|t| t.as_str() != Some("null"))
            .and_then(Value::as_str),
        _ => None,
    }
}

fn python_int(text: &str) -> Option<Value> {
    let digits = text.trim_start_matches('-');
    if text.is_empty() || digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    if let Ok(v) = text.parse::<i64>() {
        return Some(Value::from(v));
    }
    text.parse::<u64>().ok().map(Value::from)
}

fn python_float(text: &str) -> Option<Value> {
    let cleaned = text.trim();
    let lower = cleaned.to_ascii_lowercase();
    if lower.contains("inf") || lower.contains("nan") {
        return None;
    }
    cleaned
        .parse::<f64>()
        .ok()
        .and_then(serde_json::Number::from_f64)
        .map(Value::Number)
}

/// `_coerce_one`: `(value, produced_new_object)`.
pub fn coerce_one(value: &Value, schema: &Value) -> (Value, bool) {
    let Some(schema) = schema.as_object() else {
        return (value.clone(), false);
    };
    for key in ["anyOf", "oneOf"] {
        if let Some(branches) = schema
            .get(key)
            .and_then(Value::as_array)
            .filter(|b| !b.is_empty())
        {
            for branch in branches {
                let (coerced, changed) = coerce_one(value, branch);
                if changed {
                    return (coerced, true);
                }
            }
            return (value.clone(), false);
        }
    }
    let declared = declared_type(schema);
    match (declared, value) {
        (Some("integer"), Value::String(text)) => {
            let stripped = crate::pyjson::py_strip(text);
            match python_int(stripped) {
                Some(int) => (int, true),
                None => (value.clone(), false),
            }
        }
        (Some("number"), Value::String(text)) => {
            match python_float(crate::pyjson::py_strip(text)) {
                Some(number) => (number, true),
                None => (value.clone(), false),
            }
        }
        (Some("boolean"), Value::String(text)) => {
            match crate::pyjson::py_strip(text).to_lowercase().as_str() {
                "true" | "1" | "yes" | "y" => (Value::Bool(true), true),
                "false" | "0" | "no" | "n" => (Value::Bool(false), true),
                _ => (value.clone(), false),
            }
        }
        (Some("array"), _) => {
            let items = match schema.get("items") {
                Some(items) if crate::pyos::truthy(Some(items)) => items.clone(),
                _ => Value::Object(Map::new()),
            };
            let (list, wrapped) = match value {
                Value::Array(list) => (list.clone(), false),
                other => (vec![other.clone()], true),
            };
            if items.is_object() {
                (
                    Value::Array(list.iter().map(|v| coerce_one(v, &items).0).collect()),
                    true,
                )
            } else {
                (Value::Array(list), wrapped)
            }
        }
        (Some("object"), Value::Object(map)) => {
            let props = match schema.get("properties") {
                Some(props) if crate::pyos::truthy(Some(props)) => props.clone(),
                _ => Value::Object(Map::new()),
            };
            match props.as_object() {
                Some(props) => {
                    let out = map
                        .iter()
                        .map(|(k, v)| {
                            (
                                k.clone(),
                                props
                                    .get(k)
                                    .map_or_else(|| v.clone(), |s| coerce_one(v, s).0),
                            )
                        })
                        .collect();
                    (Value::Object(out), true)
                }
                None => (value.clone(), false),
            }
        }
        _ => (value.clone(), false),
    }
}

/// `_coerce_args_to_schema`.
pub fn coerce_args_to_schema(arguments: &Value, schema: &Value) -> Value {
    let (Some(args), Some(schema)) = (arguments.as_object(), schema.as_object()) else {
        return arguments.clone();
    };
    let Some(props) = schema.get("properties").and_then(Value::as_object) else {
        return arguments.clone();
    };
    Value::Object(
        args.iter()
            .map(|(k, v)| match props.get(k).filter(|s| s.is_object()) {
                Some(sub) => (k.clone(), coerce_one(v, sub).0),
                None => (k.clone(), v.clone()),
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn llm_shaped_values_reach_the_declared_types() {
        let schema = json!({"properties": {
            "n": {"type": "integer"}, "f": {"type": ["number", "null"]}, "b": {"type": "boolean"},
            "l": {"type": "array", "items": {"type": "integer"}}, "u": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            "bad": {"type": "integer"}
        }});
        let out = coerce_args_to_schema(
            &json!({"n": " 12 ", "f": "1.5", "b": "Yes", "l": "3", "u": "7", "bad": "1.0", "other": "x"}),
            &schema,
        );
        assert_eq!(
            out,
            json!({"n": 12, "f": 1.5, "b": true, "l": [3], "u": 7, "bad": "1.0", "other": "x"})
        );
    }
}
