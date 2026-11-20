//! Python `json.dumps(value, ensure_ascii=False, separators=(',', ':'))`
//! bytes, optionally with `sort_keys=True`. Unlike orjson, floats use
//! Python's `repr(float)` (`1e-05`, `1e+16`); string escapes are identical
//! to serde_json's (`\uXXXX` lowercase for other C0 controls).

use serde_json::Value;

use crate::json::python_float_repr;

fn write(out: &mut String, value: &Value, sort_keys: bool) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => {
            if let Some(integer) = number.as_i64() {
                out.push_str(&integer.to_string());
            } else if let Some(integer) = number.as_u64() {
                out.push_str(&integer.to_string());
            } else {
                out.push_str(&python_float_repr(number.as_f64().unwrap_or(0.0)));
            }
        }
        Value::String(text) => out.push_str(&serde_json::to_string(text).expect("string serialization")),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write(out, item, sort_keys);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            if sort_keys {
                entries.sort_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
            }
            for (index, (key, item)) in entries.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(key).expect("string serialization"));
                out.push(':');
                write(out, item, sort_keys);
            }
            out.push('}');
        }
    }
}

pub fn dumps(value: &Value, sort_keys: bool) -> String {
    let mut out = String::new();
    write(&mut out, value, sort_keys);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_json_dumps() {
        assert_eq!(
            dumps(&json!({"b": 1.0, "a": [1e-5, "é\n\u{1}"]}), true),
            "{\"a\":[1e-05,\"é\\n\\u0001\"],\"b\":1.0}"
        );
        assert_eq!(dumps(&json!({"b": 1e16, "a": null}), false), "{\"b\":1e+16,\"a\":null}");
    }
}
