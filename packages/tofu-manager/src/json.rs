//! JSON stores shared with the Python owners.
//!
//! The manager state file is read and written by both managers during the
//! transition, so the writer reproduces `json.dump(indent=2, sort_keys=True,
//! ensure_ascii=False)` plus a trailing newline under the `<path>.lock` file
//! lock that `lib.json_store` writers and readers share. A missing store is
//! decided under that lock (docs/DEFENSIVE_PATTERNS.md, "A missing store is
//! decided under its writers' lock").

use serde_json::{Map, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

pub type Object = Map<String, Value>;

fn lock_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".lock");
    PathBuf::from(name)
}

/// Holds `<path>.lock`; an unopenable lock or a filesystem without advisory
/// locks degrades to no lock, as in `json_store`.
struct StoreLock {
    _file: Option<File>,
}

impl StoreLock {
    fn acquire(path: &Path, exclusive: bool) -> StoreLock {
        let mut options = OpenOptions::new();
        options.read(true).write(true).mode(0o644);
        if exclusive {
            options.create(true);
        }
        let Ok(file) = options.open(lock_path(path)) else {
            return StoreLock { _file: None };
        };
        let operation = if exclusive {
            libc::LOCK_EX
        } else {
            libc::LOCK_SH
        };
        unsafe {
            libc::flock(file.as_raw_fd(), operation);
        }
        StoreLock { _file: Some(file) }
    }
}

pub enum ReadOutcome {
    Present(Object),
    Missing,
}

fn read_present(path: &Path) -> io::Result<ReadOutcome> {
    match fs::read(path) {
        Ok(bytes) => Ok(ReadOutcome::Present(
            match serde_json::from_slice::<Value>(&bytes) {
                Ok(Value::Object(map)) => map,
                _ => Object::new(),
            },
        )),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(ReadOutcome::Missing),
        Err(_) => Ok(ReadOutcome::Present(Object::new())),
    }
}

/// `server_manager._read_json`: `{}` for absent, unreadable or non-object.
pub fn read_object(path: &Path) -> Object {
    if let Ok(ReadOutcome::Present(map)) = read_present(path) {
        return map;
    }
    let _lock = StoreLock::acquire(path, false);
    match read_present(path) {
        Ok(ReadOutcome::Present(map)) => map,
        _ => Object::new(),
    }
}

/// `server_manager._atomic_json`.
pub fn write_atomic(path: &Path, value: &Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = path.with_file_name(format!("{}.{}.tmp", name, std::process::id()));
    let _lock = StoreLock::acquire(path, true);
    let mut text = python_pretty(value);
    text.push('\n');
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o644)
            .open(&temporary)?;
        file.write_all(text.as_bytes())?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// A private (0600) atomic JSON write, as `lib.json_store.write_json_atomic(mode=0o600)`.
pub fn write_private_atomic(path: &Path, value: &Value) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temporary = path.with_file_name(format!(".{}.{}.tmp", name, std::process::id()));
    let _lock = StoreLock::acquire(path, true);
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(python_pretty(value).as_bytes())?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn escape_into(out: &mut String, text: &str) {
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn number_into(out: &mut String, number: &serde_json::Number) {
    if let Some(float) = number.as_f64().filter(|_| number.is_f64()) {
        if float.is_finite() && float == float.trunc() && float.abs() < 1e16 {
            out.push_str(&format!("{:.1}", float));
            return;
        }
    }
    out.push_str(&number.to_string());
}

fn pretty_into(out: &mut String, value: &Value, depth: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(flag) => out.push_str(if *flag { "true" } else { "false" }),
        Value::Number(number) => number_into(out, number),
        Value::String(text) => escape_into(out, text),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('\n');
                out.push_str(&"  ".repeat(depth + 1));
                pretty_into(out, item, depth + 1);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(depth));
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (index, key) in keys.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('\n');
                out.push_str(&"  ".repeat(depth + 1));
                escape_into(out, key);
                out.push_str(": ");
                pretty_into(out, &map[*key], depth + 1);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(depth));
            out.push('}');
        }
    }
}

/// `json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)`.
pub fn python_pretty(value: &Value) -> String {
    let mut out = String::new();
    pretty_into(&mut out, value, 0);
    out
}

/// `json.dumps(value, sort_keys=True)` compact form with Python separators,
/// used where a digest or configuration fence compares serialized state.
pub fn python_compact_sorted(value: &Value) -> String {
    let mut out = String::new();
    compact_into(&mut out, value);
    out
}

fn compact_into(out: &mut String, value: &Value) {
    match value {
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                compact_into(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            out.push('{');
            for (index, key) in keys.iter().enumerate() {
                if index > 0 {
                    out.push_str(", ");
                }
                escape_into(out, key);
                out.push_str(": ");
                compact_into(out, &map[*key]);
            }
            out.push('}');
        }
        other => pretty_into(out, other, 0),
    }
}

/// Typed accessors over a Python-shaped object.
pub trait ObjectExt {
    fn str_of(&self, key: &str) -> &str;
    fn f64_of(&self, key: &str) -> f64;
    fn i64_of(&self, key: &str) -> Option<i64>;
    fn obj_of(&self, key: &str) -> Object;
    fn truthy(&self, key: &str) -> bool;
}

pub fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().map(|n| n != 0.0).unwrap_or(false),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

impl ObjectExt for Object {
    fn str_of(&self, key: &str) -> &str {
        self.get(key).and_then(Value::as_str).unwrap_or("")
    }

    fn f64_of(&self, key: &str) -> f64 {
        match self.get(key) {
            Some(Value::Number(number)) => number.as_f64().unwrap_or(0.0),
            Some(Value::String(text)) => text.trim().parse().unwrap_or(0.0),
            _ => 0.0,
        }
    }

    fn i64_of(&self, key: &str) -> Option<i64> {
        match self.get(key) {
            Some(Value::Number(number)) => number.as_i64(),
            _ => None,
        }
    }

    fn obj_of(&self, key: &str) -> Object {
        match self.get(key) {
            Some(Value::Object(map)) => map.clone(),
            _ => Object::new(),
        }
    }

    fn truthy(&self, key: &str) -> bool {
        truthy(self.get(key))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn pretty_matches_python_json_dump() {
        let value = json!({"b": [1, 2.5, {}], "a": {"z": null, "y": "é\n"}, "c": [], "d": 0.0, "e": 1700000000.0});
        let expected = "{\n  \"a\": {\n    \"y\": \"é\\n\",\n    \"z\": null\n  },\n  \"b\": [\n    1,\n    2.5,\n    {}\n  ],\n  \"c\": [],\n  \"d\": 0.0,\n  \"e\": 1700000000.0\n}";
        assert_eq!(python_pretty(&value), expected);
    }

    #[test]
    fn compact_matches_python_sort_keys_dumps() {
        let value =
            json!({"pid": 5, "desired": "running", "serverArgs": ["--port", "1"], "serverEnv": {}});
        assert_eq!(
            python_compact_sorted(&value),
            "{\"desired\": \"running\", \"pid\": 5, \"serverArgs\": [\"--port\", \"1\"], \"serverEnv\": {}}"
        );
    }

    #[test]
    fn atomic_write_round_trips_and_missing_reads_empty() {
        let directory =
            std::env::temp_dir().join(format!("tofu-manager-json-{}", std::process::id()));
        let path = directory.join("state.json");
        assert!(read_object(&path).is_empty());
        write_atomic(&path, &json!({"version": 1})).unwrap();
        assert_eq!(read_object(&path).i64_of("version"), Some(1));
        let _ = fs::remove_dir_all(&directory);
    }
}
