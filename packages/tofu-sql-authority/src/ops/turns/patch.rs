//! Private port of `lib/turn_projection_patch.py`: revision patches
//! (`build_projection_patch`, `apply_projection_patch`,
//! `is_text_projection_patch`).

use serde_json::{json, Map, Value};

use crate::codec::py_eq;

pub const PROJECTION_PATCH_VERSION: i64 = 1;

/// `ProjectionPatchError`: callers map it to their own storage error.
#[derive(Debug)]
pub struct PatchError;

fn path_value(path: &[Value]) -> Value {
    Value::Array(path.to_vec())
}

fn diff_value(before: &Value, after: &Value, path: &mut Vec<Value>, operations: &mut Vec<Value>) {
    if py_eq(before, after) {
        return;
    }
    match (before, after) {
        (Value::String(old), Value::String(new)) => {
            if new.starts_with(old.as_str()) {
                let suffix = &new[old.len()..];
                if !suffix.is_empty() {
                    operations.push(json!({"op": "append_text", "path": path_value(path), "value": suffix}));
                }
                return;
            }
            operations.push(json!({"op": "set", "path": path_value(path), "value": new}));
        }
        (Value::Object(old), Value::Object(new)) => {
            let mut removed: Vec<&String> = old.keys().filter(|key| !new.contains_key(*key)).collect();
            removed.sort();
            for key in removed {
                let mut child = path.clone();
                child.push(Value::String(key.clone()));
                operations.push(json!({"op": "remove", "path": child}));
            }
            let mut added: Vec<&String> = new.keys().filter(|key| !old.contains_key(*key)).collect();
            added.sort();
            for key in added {
                let mut child = path.clone();
                child.push(Value::String(key.clone()));
                operations.push(json!({"op": "set", "path": child, "value": new[key.as_str()]}));
            }
            let mut shared: Vec<&String> = old.keys().filter(|key| new.contains_key(*key)).collect();
            shared.sort();
            for key in shared {
                path.push(Value::String(key.clone()));
                diff_value(&old[key.as_str()], &new[key.as_str()], path, operations);
                path.pop();
            }
        }
        (Value::Array(old), Value::Array(new)) => {
            let mut before_list = old.clone();
            let (start, evicted) = evicted_run(old, new);
            for _ in 0..evicted {
                let mut child = path.clone();
                child.push(Value::from(start as i64));
                operations.push(json!({"op": "remove", "path": child}));
            }
            before_list.drain(start..start + evicted);
            let shared = before_list.len().min(new.len());
            for index in start..shared {
                path.push(Value::from(index as i64));
                diff_value(&before_list[index], &new[index], path, operations);
                path.pop();
            }
            if new.len() < before_list.len() {
                operations.push(json!({"op": "truncate", "path": path_value(path), "length": new.len()}));
            } else if new.len() > before_list.len() {
                operations.push(json!({
                    "op": "append",
                    "path": path_value(path),
                    "value": Value::Array(new[before_list.len()..].to_vec()),
                }));
            }
        }
        _ => operations.push(json!({"op": "set", "path": path_value(path), "value": after})),
    }
}

fn unaligned_items(before: &[Value], after: &[Value], offset: usize) -> usize {
    let overlap = (before.len() - offset).min(after.len());
    let changed = (0..overlap).filter(|index| !py_eq(&before[offset + index], &after[*index])).count();
    changed + after.len() - overlap
}

fn evicted_run(before: &[Value], after: &[Value]) -> (usize, usize) {
    let shared = before.len().min(after.len());
    let start = (0..shared).find(|index| !py_eq(&before[*index], &after[*index])).unwrap_or(shared);
    if start == shared {
        return (start, 0);
    }
    let evicted = (1..before.len() - start).find(|offset| py_eq(&before[start + offset], &after[start])).unwrap_or(0);
    if evicted == 0 {
        return (start, 0);
    }
    let (before, after) = (&before[start..], &after[start..]);
    let shifted = evicted + unaligned_items(before, after, evicted);
    (start, if shifted < unaligned_items(before, after, 0) { evicted } else { 0 })
}

/// `build_projection_patch(before, after, base_revision=, target_revision=)`.
pub fn build_projection_patch(before: &Value, after: &Value, base_revision: i64, target_revision: i64) -> Value {
    let previous = match before {
        Value::Object(map) => Value::Object(map.clone()),
        _ => Value::Object(Map::new()),
    };
    let current = match after {
        Value::Object(map) => Value::Object(map.clone()),
        _ => Value::Object(Map::new()),
    };
    let mut operations = Vec::new();
    diff_value(&previous, &current, &mut Vec::new(), &mut operations);
    json!({
        "version": PROJECTION_PATCH_VERSION,
        "baseRevision": base_revision,
        "targetRevision": target_revision,
        "operations": operations,
    })
}

fn strict_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        _ => None,
    }
}

fn update_at_path(
    value: &Value,
    path: &[Value],
    depth: usize,
    update: &dyn Fn(&Value) -> Result<Value, PatchError>,
) -> Result<Value, PatchError> {
    if depth >= path.len() {
        return update(value);
    }
    let part = &path[depth];
    if let Value::Array(items) = value {
        let index = match strict_int(part) {
            Some(index) if index >= 0 && (index as usize) < items.len() => index as usize,
            _ => return Err(PatchError),
        };
        let mut next = items.clone();
        next[index] = update_at_path(&items[index], path, depth + 1, update)?;
        return Ok(Value::Array(next));
    }
    let (Value::Object(map), Value::String(key)) = (value, part) else { return Err(PatchError) };
    let mut next = map.clone();
    let child = update_at_path(map.get(key).unwrap_or(&Value::Null), path, depth + 1, update)?;
    next.insert(key.clone(), child);
    Ok(Value::Object(next))
}

fn remove_at_path(value: &Value, path: &[Value]) -> Result<Value, PatchError> {
    let Some((leaf, parent)) = path.split_last() else { return Err(PatchError) };
    let leaf = leaf.clone();
    update_at_path(value, parent, 0, &move |container: &Value| match container {
        Value::Array(items) => {
            let index = match strict_int(&leaf) {
                Some(index) if index >= 0 && (index as usize) < items.len() => index as usize,
                _ => return Err(PatchError),
            };
            let mut next = items.clone();
            next.remove(index);
            Ok(Value::Array(next))
        }
        Value::Object(map) => {
            let Value::String(key) = &leaf else { return Err(PatchError) };
            let mut next = map.clone();
            next.shift_remove(key);
            Ok(Value::Object(next))
        }
        _ => Err(PatchError),
    })
}

/// `apply_projection_patch(projection, raw_patch)`.
pub fn apply_projection_patch(projection: &Value, raw_patch: &Value) -> Result<Value, PatchError> {
    let Value::Object(patch) = raw_patch else { return Err(PatchError) };
    let version = patch.get("version").and_then(strict_int);
    let Some(Value::Array(operations)) = patch.get("operations") else { return Err(PatchError) };
    if version != Some(PROJECTION_PATCH_VERSION) {
        return Err(PatchError);
    }
    let mut next = match projection {
        Value::Object(map) => Value::Object(map.clone()),
        _ => Value::Object(Map::new()),
    };
    for raw_operation in operations {
        let Value::Object(operation) = raw_operation else { return Err(PatchError) };
        let path = match operation.get("path") {
            Some(Value::Array(path))
                if path.iter().all(|part| part.is_string() || strict_int(part).is_some_and(|value| value >= 0)) =>
            {
                path.clone()
            }
            _ => return Err(PatchError),
        };
        let value = operation.get("value").cloned().unwrap_or(Value::Null);
        next = match operation.get("op").and_then(Value::as_str) {
            Some("set") => update_at_path(&next, &path, 0, &|_| Ok(value.clone()))?,
            Some("remove") => remove_at_path(&next, &path)?,
            Some("append_text") => {
                let Value::String(suffix) = &value else { return Err(PatchError) };
                update_at_path(&next, &path, 0, &|current| match current {
                    Value::String(text) => Ok(Value::String(format!("{text}{suffix}"))),
                    _ => Err(PatchError),
                })?
            }
            Some("append") => {
                let Value::Array(extra) = &value else { return Err(PatchError) };
                update_at_path(&next, &path, 0, &|current| match current {
                    Value::Array(items) => {
                        let mut joined = items.clone();
                        joined.extend(extra.iter().cloned());
                        Ok(Value::Array(joined))
                    }
                    _ => Err(PatchError),
                })?
            }
            Some("truncate") => {
                let length = match operation.get("length").and_then(strict_int) {
                    Some(length) if length >= 0 => length as usize,
                    _ => return Err(PatchError),
                };
                update_at_path(&next, &path, 0, &|current| match current {
                    Value::Array(items) if length <= items.len() => Ok(Value::Array(items[..length].to_vec())),
                    _ => Err(PatchError),
                })?
            }
            _ => return Err(PatchError),
        };
    }
    if !next.is_object() {
        return Err(PatchError);
    }
    Ok(next)
}

/// `is_text_projection_patch(patch)`.
pub fn is_text_projection_patch(patch: &Value) -> bool {
    let Value::Object(patch) = patch else { return false };
    if patch.get("version").and_then(strict_int) != Some(PROJECTION_PATCH_VERSION) {
        return false;
    }
    let Some(Value::Array(operations)) = patch.get("operations") else { return false };
    if operations.len() > 2 {
        return false;
    }
    let mut fields: Vec<&str> = Vec::new();
    for operation in operations {
        let Value::Object(operation) = operation else { return false };
        let Some(Value::Array(path)) = operation.get("path") else { return false };
        if path.len() != 1 {
            return false;
        }
        let Some(field) = path[0].as_str() else { return false };
        if !matches!(field, "content" | "thinking")
            || fields.contains(&field)
            || !matches!(operation.get("op").and_then(Value::as_str), Some("set" | "append_text"))
            || !operation.get("value").is_some_and(Value::is_string)
        {
            return false;
        }
        fields.push(field);
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patches_round_trip_with_appends_and_evictions() {
        let before = json!({"content": "ab", "rows": [1, 2, 3, 4], "gone": 1, "x": {"y": [1]}});
        let after = json!({"content": "abc", "rows": [2, 3, 4, 5], "x": {"y": [1, 2]}, "new": true});
        let patch = build_projection_patch(&before, &after, 1, 2);
        let ops = patch["operations"].as_array().unwrap();
        assert_eq!(ops[0], json!({"op": "remove", "path": ["gone"]}));
        assert!(ops.iter().any(|op| op == &json!({"op": "remove", "path": ["rows", 0]})));
        let applied = apply_projection_patch(&before, &patch).unwrap();
        assert!(py_eq(&applied, &after));
    }
}
