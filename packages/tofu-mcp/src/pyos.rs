//! CPython `os.path`, `str` and `shlex` semantics the ported policies depend on.

use serde_json::Value;
use std::path::{Component, Path, PathBuf};

/// `os.path.join(a, b)` for two POSIX parts.
pub fn join(base: &str, part: &str) -> String {
    if part.starts_with('/') {
        return part.to_string();
    }
    if base.is_empty() || base.ends_with('/') {
        return format!("{base}{part}");
    }
    format!("{base}/{part}")
}

/// `os.path.normpath` (POSIX).
pub fn normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".into();
    }
    let initial = if path.starts_with("//") && !path.starts_with("///") {
        2
    } else if path.starts_with('/') {
        1
    } else {
        0
    };
    let mut parts: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".." || (initial == 0 && parts.is_empty()) || parts.last() == Some(&"..") {
            parts.push(comp);
        } else if !parts.is_empty() {
            parts.pop();
        }
    }
    let joined = parts.join("/");
    let out = format!("{}{}", "/".repeat(initial), joined);
    if out.is_empty() {
        ".".into()
    } else {
        out
    }
}

/// `os.path.realpath(path)` (non-strict): resolve the existing symlink prefix.
pub fn realpath(path: &str) -> String {
    let absolute = if path.starts_with('/') {
        PathBuf::from(path)
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut resolved = PathBuf::from("/");
    let mut pending: Vec<String> = absolute
        .components()
        .filter_map(|c| match c {
            Component::Normal(part) => Some(part.to_string_lossy().into_owned()),
            Component::ParentDir => Some("..".into()),
            _ => None,
        })
        .collect();
    pending.reverse();
    let mut hops = 0;
    while let Some(part) = pending.pop() {
        if part == ".." {
            resolved.pop();
            continue;
        }
        let candidate = resolved.join(&part);
        match std::fs::read_link(&candidate) {
            Ok(target) if hops < 40 => {
                hops += 1;
                let target_text = target.to_string_lossy().into_owned();
                if target_text.starts_with('/') {
                    resolved = PathBuf::from("/");
                }
                let mut parts: Vec<String> = Path::new(&target_text)
                    .components()
                    .filter_map(|c| match c {
                        Component::Normal(p) => Some(p.to_string_lossy().into_owned()),
                        Component::ParentDir => Some("..".into()),
                        _ => None,
                    })
                    .collect();
                parts.reverse();
                pending.extend(parts);
            }
            _ => resolved = candidate,
        }
    }
    resolved.to_string_lossy().into_owned()
}

/// `str.splitlines()`.
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, ch)) = chars.next() {
        let breaks = matches!(
            ch,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !breaks {
            continue;
        }
        lines.push(&text[start..index]);
        let mut end = index + ch.len_utf8();
        if ch == '\r' {
            if let Some((next_index, '\n')) = chars.peek().copied() {
                chars.next();
                end = next_index + 1;
            }
        }
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// `str.split()` (no argument).
pub fn split_whitespace(text: &str) -> Vec<String> {
    text.split(crate::pyjson::py_space)
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect()
}

/// `shlex.split(value)` (POSIX mode, no comments); `Err` where CPython raises ValueError.
pub fn shlex_split(text: &str) -> Result<Vec<String>, &'static str> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut in_word = false;
    let mut chars = text.chars();
    while let Some(ch) = chars.next() {
        match ch {
            ' ' | '\t' | '\r' | '\n' => {
                if in_word {
                    words.push(std::mem::take(&mut word));
                    in_word = false;
                }
            }
            '\\' => {
                in_word = true;
                match chars.next() {
                    Some(next) => word.push(next),
                    None => return Err("No escaped character"),
                }
            }
            '\'' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(c) => word.push(c),
                        None => return Err("No closing quotation"),
                    }
                }
            }
            '"' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(c @ ('"' | '\\')) => word.push(c),
                            Some(c) => {
                                word.push('\\');
                                word.push(c);
                            }
                            None => return Err("No escaped character"),
                        },
                        Some(c) => word.push(c),
                        None => return Err("No closing quotation"),
                    }
                }
            }
            c => {
                in_word = true;
                word.push(c);
            }
        }
    }
    if in_word {
        words.push(word);
    }
    Ok(words)
}

/// Python `str(value)` for a decoded JSON value.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => py_repr_value(other),
    }
}

fn py_repr_value(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => {
            if number.is_f64() {
                crate::pyjson::float_repr(number.as_f64().unwrap_or_default())
            } else {
                number.to_string()
            }
        }
        Value::String(text) => crate::pyjson::py_repr(text),
        Value::Array(items) => {
            format!(
                "[{}]",
                items
                    .iter()
                    .map(py_repr_value)
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", crate::pyjson::py_repr(k), py_repr_value(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Python truthiness of a JSON value.
pub fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(flag)) => *flag,
        Some(Value::Number(number)) => number.as_f64().is_some_and(|n| n != 0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// `os.path.basename`.
pub fn basename(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// CPython's `OSError` subclass name for an errno (PEP 3151).
pub fn os_error_name(error: &std::io::Error) -> &'static str {
    match error.raw_os_error() {
        Some(libc::EACCES) | Some(libc::EPERM) => "PermissionError",
        Some(libc::ENOENT) => "FileNotFoundError",
        Some(libc::EISDIR) => "IsADirectoryError",
        Some(libc::ENOTDIR) => "NotADirectoryError",
        Some(libc::EEXIST) => "FileExistsError",
        Some(libc::EINTR) => "InterruptedError",
        Some(libc::ETIMEDOUT) => "TimeoutError",
        _ => "OSError",
    }
}

/// `os.strerror(errno)`.
pub fn strerror(error: &std::io::Error) -> String {
    match error.raw_os_error() {
        Some(code) => {
            // SAFETY: strerror returns a pointer to a static, NUL-terminated message.
            let text = unsafe { std::ffi::CStr::from_ptr(libc::strerror(code)) };
            text.to_string_lossy().into_owned()
        }
        None => error.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths() {
        assert_eq!(join("/a", "b"), "/a/b");
        assert_eq!(join("/a/", "b"), "/a/b");
        assert_eq!(join("/a", "/b"), "/b");
        assert_eq!(normpath("/a/./b/../c//"), "/a/c");
        assert_eq!(normpath("//a"), "//a");
        assert_eq!(normpath("a/../.."), "..");
        assert_eq!(realpath("/nonexistent/x/../y"), "/nonexistent/y");
    }

    #[test]
    fn lines_and_words() {
        assert_eq!(splitlines("a\r\nb\rc\nd"), vec!["a", "b", "c", "d"]);
        assert_eq!(splitlines("a\n"), vec!["a"]);
        assert_eq!(
            shlex_split("-Dq=x 'a b' \"c\\\"d\" e\\ f").unwrap(),
            vec!["-Dq=x", "a b", "c\"d", "e f"]
        );
        assert!(shlex_split("'open").is_err());
        assert_eq!(split_whitespace("  a  b "), vec!["a", "b"]);
    }
}
