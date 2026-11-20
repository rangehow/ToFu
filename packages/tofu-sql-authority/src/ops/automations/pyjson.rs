//! Python `json.dumps(value, separators=(',', ':'))` with either
//! `ensure_ascii` spelling, and a `json.loads` decoder with CPython's
//! acceptance (NaN/Infinity, last duplicate key wins) and `JSONDecodeError`
//! messages.
//!
//! private port of src/ops/artifacts/pyjson.rs (`dumps`), extended with
//! `ensure_ascii=True`; shared owner: artifacts

use serde_json::{Map, Number, Value};

use crate::json::python_float_repr;

fn write_string(out: &mut String, text: &str, ensure_ascii: bool) {
    let escaped = serde_json::to_string(text).expect("string serialization");
    if !ensure_ascii {
        out.push_str(&escaped);
        return;
    }
    for character in escaped.chars() {
        if (character as u32) < 0x7f {
            out.push(character);
        } else {
            let mut units = [0u16; 2];
            for unit in character.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{:04x}", unit));
            }
        }
    }
}

fn write(out: &mut String, value: &Value, sort_keys: bool, ensure_ascii: bool) {
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
        Value::String(text) => write_string(out, text, ensure_ascii),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write(out, item, sort_keys, ensure_ascii);
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
                write_string(out, key, ensure_ascii);
                out.push(':');
                write(out, item, sort_keys, ensure_ascii);
            }
            out.push('}');
        }
    }
}

/// `json.dumps(value, sort_keys=sort_keys, separators=(',', ':'), ensure_ascii=False)`.
pub fn dumps(value: &Value, sort_keys: bool) -> String {
    let mut out = String::new();
    write(&mut out, value, sort_keys, false);
    out
}

/// `json.dumps(value, sort_keys=True, separators=(',', ':'))` (ASCII output).
pub fn dumps_ascii_sorted(value: &Value) -> String {
    let mut out = String::new();
    write(&mut out, value, true, true);
    out
}

/// Why `json.loads` (or the following `allow_nan=False` dump) failed.
#[derive(Debug, PartialEq)]
pub enum LoadError {
    /// `JSONDecodeError` text: `"<msg>: line L column C (char P)"`.
    Decode(String),
    /// A NaN/Infinity value: `json.dumps(allow_nan=False)` raises ValueError
    /// `"Out of range float values are not JSON compliant: <repr>"`.
    NonFinite(String),
    /// A value serde_json cannot hold (integer beyond 64 bits, lone surrogate).
    Unrepresentable,
}

struct Decoder {
    chars: Vec<char>,
    non_finite: Option<String>,
}

fn is_ws(character: char) -> bool {
    matches!(character, ' ' | '\t' | '\n' | '\r')
}

impl Decoder {
    fn error(&self, message: &str, position: usize) -> LoadError {
        let before = &self.chars[..position.min(self.chars.len())];
        let line = before.iter().filter(|c| **c == '\n').count() + 1;
        let column = match before.iter().rposition(|c| *c == '\n') {
            Some(index) => position - index,
            None => position + 1,
        };
        LoadError::Decode(format!("{message}: line {line} column {column} (char {position})"))
    }

    fn skip_ws(&self, mut index: usize) -> usize {
        while index < self.chars.len() && is_ws(self.chars[index]) {
            index += 1;
        }
        index
    }

    fn starts(&self, index: usize, literal: &str) -> bool {
        literal.chars().enumerate().all(|(offset, expected)| self.chars.get(index + offset) == Some(&expected))
    }

    fn non_finite_value(&mut self, repr: &str) -> Value {
        if self.non_finite.is_none() {
            self.non_finite = Some(repr.to_owned());
        }
        Value::Null
    }

    /// `scan_once`; `Ok(None)` is its StopIteration.
    fn scan(&mut self, index: usize) -> Result<Option<(Value, usize)>, LoadError> {
        let Some(&character) = self.chars.get(index) else { return Ok(None) };
        match character {
            '"' => {
                let (text, end) = self.string(index + 1)?;
                Ok(Some((Value::String(text), end)))
            }
            '{' => self.object(index + 1).map(Some),
            '[' => self.array(index + 1).map(Some),
            'n' if self.starts(index, "null") => Ok(Some((Value::Null, index + 4))),
            't' if self.starts(index, "true") => Ok(Some((Value::Bool(true), index + 4))),
            'f' if self.starts(index, "false") => Ok(Some((Value::Bool(false), index + 5))),
            'N' if self.starts(index, "NaN") => Ok(Some((self.non_finite_value("nan"), index + 3))),
            'I' if self.starts(index, "Infinity") => Ok(Some((self.non_finite_value("inf"), index + 8))),
            '-' if self.starts(index, "-Infinity") => Ok(Some((self.non_finite_value("-inf"), index + 9))),
            _ => self.number(index),
        }
    }

    fn number(&mut self, start: usize) -> Result<Option<(Value, usize)>, LoadError> {
        let digit = |chars: &Vec<char>, i: usize| chars.get(i).is_some_and(|c| c.is_ascii_digit());
        let mut index = start;
        if self.chars.get(index) == Some(&'-') {
            index += 1;
        }
        if self.chars.get(index) == Some(&'0') {
            index += 1;
        } else if self.chars.get(index).is_some_and(|c| ('1'..='9').contains(c)) {
            while digit(&self.chars, index) {
                index += 1;
            }
        } else {
            return Ok(None);
        }
        let mut float = false;
        if self.chars.get(index) == Some(&'.') && digit(&self.chars, index + 1) {
            float = true;
            index += 1;
            while digit(&self.chars, index) {
                index += 1;
            }
        }
        if matches!(self.chars.get(index), Some('e' | 'E')) {
            let mut exponent = index + 1;
            if matches!(self.chars.get(exponent), Some('+' | '-')) {
                exponent += 1;
            }
            if digit(&self.chars, exponent) {
                float = true;
                index = exponent;
                while digit(&self.chars, index) {
                    index += 1;
                }
            }
        }
        let literal: String = self.chars[start..index].iter().collect();
        if !float {
            if let Ok(integer) = literal.parse::<i64>() {
                return Ok(Some((Value::from(integer), index)));
            }
            if let Ok(integer) = literal.parse::<u64>() {
                return Ok(Some((Value::from(integer), index)));
            }
            return Err(LoadError::Unrepresentable);
        }
        let parsed: f64 = literal.parse().map_err(|_| LoadError::Unrepresentable)?;
        if !parsed.is_finite() {
            let repr = if parsed > 0.0 { "inf" } else { "-inf" };
            return Ok(Some((self.non_finite_value(repr), index)));
        }
        Ok(Some((Number::from_f64(parsed).map(Value::Number).unwrap_or(Value::Null), index)))
    }

    fn hex4(&self, index: usize) -> Option<u32> {
        let mut value = 0u32;
        for offset in 0..4 {
            value = value * 16 + self.chars.get(index + offset)?.to_digit(16)?;
        }
        Some(value)
    }

    /// `scanstring(s, end, strict=True)`; `end` follows the opening quote.
    fn string(&self, end: usize) -> Result<(String, usize), LoadError> {
        let begin = end - 1;
        let length = self.chars.len();
        let mut out = String::new();
        let mut next = end;
        loop {
            loop {
                if next >= length {
                    return Err(self.error("Unterminated string starting at", begin));
                }
                let character = self.chars[next];
                if character == '"' || character == '\\' {
                    break;
                }
                if (character as u32) <= 0x1f {
                    return Err(self.error("Invalid control character at", next));
                }
                out.push(character);
                next += 1;
            }
            if self.chars[next] == '"' {
                return Ok((out, next + 1));
            }
            next += 1;
            if next == length {
                return Err(self.error("Unterminated string starting at", begin));
            }
            let escape = self.chars[next];
            if escape != 'u' {
                let decoded = match escape {
                    '"' => '"',
                    '\\' => '\\',
                    '/' => '/',
                    'b' => '\u{8}',
                    'f' => '\u{c}',
                    'n' => '\n',
                    'r' => '\r',
                    't' => '\t',
                    _ => return Err(self.error("Invalid \\escape", next - 1)),
                };
                out.push(decoded);
                next += 1;
                continue;
            }
            next += 1;
            if next + 4 >= length {
                return Err(self.error("Invalid \\uXXXX escape", next - 1));
            }
            let Some(mut code) = self.hex4(next) else {
                return Err(self.error("Invalid \\uXXXX escape", next - 1));
            };
            next += 4;
            if (0xd800..=0xdbff).contains(&code)
                && self.chars.get(next) == Some(&'\\')
                && self.chars.get(next + 1) == Some(&'u')
            {
                if let Some(low) = self.hex4(next + 2) {
                    if (0xdc00..=0xdfff).contains(&low) {
                        code = 0x10000 + (((code - 0xd800) << 10) | (low - 0xdc00));
                        next += 6;
                    }
                }
            }
            match char::from_u32(code) {
                Some(decoded) => out.push(decoded),
                None => return Err(LoadError::Unrepresentable),
            }
        }
    }

    fn object(&mut self, start: usize) -> Result<(Value, usize), LoadError> {
        let mut map = Map::new();
        let mut index = self.skip_ws(start);
        if self.chars.get(index) != Some(&'"') {
            if self.chars.get(index) == Some(&'}') {
                return Ok((Value::Object(map), index + 1));
            }
            return Err(self.error("Expecting property name enclosed in double quotes", index));
        }
        loop {
            let (key, after_key) = self.string(index + 1)?;
            index = self.skip_ws(after_key);
            if self.chars.get(index) != Some(&':') {
                return Err(self.error("Expecting ':' delimiter", index));
            }
            index = self.skip_ws(index + 1);
            let Some((value, after_value)) = self.scan(index)? else {
                return Err(self.error("Expecting value", index));
            };
            map.insert(key, value);
            index = self.skip_ws(after_value);
            match self.chars.get(index) {
                Some('}') => return Ok((Value::Object(map), index + 1)),
                Some(',') => {}
                _ => return Err(self.error("Expecting ',' delimiter", index)),
            }
            index = self.skip_ws(index + 1);
            if self.chars.get(index) != Some(&'"') {
                return Err(self.error("Expecting property name enclosed in double quotes", index));
            }
        }
    }

    fn array(&mut self, start: usize) -> Result<(Value, usize), LoadError> {
        let mut items = Vec::new();
        let mut index = self.skip_ws(start);
        if self.chars.get(index) == Some(&']') {
            return Ok((Value::Array(items), index + 1));
        }
        loop {
            let Some((value, after)) = self.scan(index)? else {
                return Err(self.error("Expecting value", index));
            };
            items.push(value);
            index = self.skip_ws(after);
            match self.chars.get(index) {
                Some(']') => return Ok((Value::Array(items), index + 1)),
                Some(',') => {}
                _ => return Err(self.error("Expecting ',' delimiter", index)),
            }
            index = self.skip_ws(index + 1);
        }
    }
}

/// `json.loads(text)` followed by the `allow_nan=False` guard of the
/// caller's next `json.dumps`.
pub fn loads(text: &str) -> Result<Value, LoadError> {
    let mut decoder = Decoder { chars: text.chars().collect(), non_finite: None };
    if decoder.chars.first() == Some(&'\u{feff}') {
        return Err(decoder.error("Unexpected UTF-8 BOM (decode using utf-8-sig)", 0));
    }
    let start = decoder.skip_ws(0);
    let Some((value, end)) = decoder.scan(start)? else {
        return Err(decoder.error("Expecting value", start));
    };
    let end = decoder.skip_ws(end);
    if end != decoder.chars.len() {
        return Err(decoder.error("Extra data", end));
    }
    if let Some(repr) = decoder.non_finite {
        return Err(LoadError::NonFinite(repr));
    }
    Ok(value)
}

impl LoadError {
    /// `str(exc)` of the Python exception.
    pub fn message(&self) -> String {
        match self {
            LoadError::Decode(message) => message.clone(),
            LoadError::NonFinite(repr) => format!("Out of range float values are not JSON compliant: {repr}"),
            LoadError::Unrepresentable => "Unrepresentable JSON value".into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_matches_python() {
        assert_eq!(
            dumps(&json!({"b": 1.0, "a": [1e-5, "é\n\u{1}"]}), true),
            "{\"a\":[1e-05,\"é\\n\\u0001\"],\"b\":1.0}"
        );
        assert_eq!(dumps_ascii_sorted(&json!({"é": "\u{7f}😀"})), "{\"\\u00e9\":\"\\u007f\\ud83d\\ude00\"}");
    }

    #[test]
    fn loads_matches_python_messages() {
        assert_eq!(loads("{\"a\": [1, 2.5, \"\\u00e9\"], \"a\": 3}").unwrap(), json!({"a": 3}));
        assert_eq!(loads("").unwrap_err().message(), "Expecting value: line 1 column 1 (char 0)");
        assert_eq!(loads("{\"a\" 1}").unwrap_err().message(), "Expecting ':' delimiter: line 1 column 6 (char 5)");
        assert_eq!(loads("[1,\n2 x").unwrap_err().message(), "Expecting ',' delimiter: line 2 column 3 (char 6)");
        assert_eq!(loads("{} x").unwrap_err().message(), "Extra data: line 1 column 4 (char 3)");
        assert_eq!(loads("\"ab").unwrap_err().message(), "Unterminated string starting at: line 1 column 1 (char 0)");
        assert_eq!(loads("[NaN]").unwrap_err().message(), "Out of range float values are not JSON compliant: nan");
        assert_eq!(
            loads("{,}").unwrap_err().message(),
            "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
        );
    }
}
