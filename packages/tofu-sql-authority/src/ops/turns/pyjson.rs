//! CPython `json.loads` acceptance for the model-receipt readers of the
//! tool-round replay scan: `lib/tools/result_projection_json.py::
//! load_projection_json` (duplicate keys and NaN/Infinity refused, numbers
//! kept as tokens) and plain `json.loads` (last duplicate wins, constants
//! accepted, `int()` digit limit). Callers only test acceptance and a few
//! shallow fields, so containers below `keep_depth` are validated but not
//! materialized; the scanner is iterative so hostile nesting cannot exhaust
//! the RPC thread stack.

use std::collections::HashSet;

/// CPython 3.12's C scanner raises RecursionError past this container
/// nesting (measured: 9,997 nested arrays or objects decode, 9,998 do not).
const PYTHON_JSON_NESTING_LIMIT: usize = 9_997;
/// `sys.get_int_max_str_digits()` default: `int()` of a longer literal raises.
const PYTHON_INT_MAX_STR_DIGITS: usize = 4_300;

/// A Python `str`: valid Unicode, or code points with a lone surrogate.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum PyStr {
    Text(String),
    Raw(Vec<u32>),
}

impl PyStr {
    pub fn len(&self) -> usize {
        match self {
            PyStr::Text(text) => text.chars().count(),
            PyStr::Raw(points) => points.len(),
        }
    }

    pub fn is(&self, expected: &str) -> bool {
        matches!(self, PyStr::Text(text) if text == expected)
    }
}

#[derive(Debug)]
pub enum PyVal {
    Null,
    Bool(bool),
    Number,
    Str(PyStr),
    List(Vec<PyVal>),
    /// Insertion-ordered pairs; a repeated key keeps its first position.
    Dict(Vec<(PyStr, PyVal)>),
    /// A container nested below the materialized depth.
    Opaque,
}

impl PyVal {
    pub fn get(&self, key: &str) -> Option<&PyVal> {
        match self {
            PyVal::Dict(pairs) => pairs.iter().find(|(name, _)| name.is(key)).map(|(_, value)| value),
            _ => None,
        }
    }

    pub fn contains(&self, key: &str) -> bool {
        self.get(key).is_some()
    }
}

#[derive(Clone, Copy, PartialEq)]
pub enum Mode {
    /// `load_projection_json`.
    Projection,
    /// Plain `json.loads`.
    Plain,
}

enum Container {
    List(Vec<PyVal>),
    Dict { pairs: Vec<(PyStr, PyVal)>, keys: HashSet<PyStr>, key: Option<PyStr> },
}

struct Frame {
    container: Container,
    keep: bool,
}

struct Scanner {
    chars: Vec<char>,
    mode: Mode,
    keep_depth: usize,
}

fn is_ws(character: char) -> bool {
    matches!(character, ' ' | '\t' | '\n' | '\r')
}

impl Scanner {
    fn skip_ws(&self, mut index: usize) -> usize {
        while index < self.chars.len() && is_ws(self.chars[index]) {
            index += 1;
        }
        index
    }

    fn starts(&self, index: usize, literal: &str) -> bool {
        literal.chars().enumerate().all(|(offset, expected)| self.chars.get(index + offset) == Some(&expected))
    }

    fn hex4(&self, index: usize) -> Option<u32> {
        let mut value = 0u32;
        for offset in 0..4 {
            value = value * 16 + self.chars.get(index + offset)?.to_digit(16)?;
        }
        Some(value)
    }

    /// `scanstring(s, end, strict=True)`; `end` follows the opening quote.
    fn string(&self, mut next: usize) -> Option<(PyStr, usize)> {
        let length = self.chars.len();
        let mut points: Vec<u32> = Vec::new();
        loop {
            let character = *self.chars.get(next)?;
            if character == '"' {
                break;
            }
            if character != '\\' {
                if (character as u32) <= 0x1f {
                    return None;
                }
                points.push(character as u32);
                next += 1;
                continue;
            }
            next += 1;
            let escape = *self.chars.get(next)?;
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
                    _ => return None,
                };
                points.push(decoded as u32);
                next += 1;
                continue;
            }
            next += 1;
            if next + 4 >= length {
                return None;
            }
            let mut code = self.hex4(next)?;
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
            points.push(code);
        }
        let text: Option<String> = points.iter().map(|point| char::from_u32(*point)).collect();
        Some((text.map(PyStr::Text).unwrap_or(PyStr::Raw(points)), next + 1))
    }

    fn number(&self, start: usize) -> Option<usize> {
        let digit = |index: usize| self.chars.get(index).is_some_and(char::is_ascii_digit);
        let mut index = start;
        if self.chars.get(index) == Some(&'-') {
            index += 1;
        }
        let integer_start = index;
        if self.chars.get(index) == Some(&'0') {
            index += 1;
        } else if self.chars.get(index).is_some_and(|c| ('1'..='9').contains(c)) {
            while digit(index) {
                index += 1;
            }
        } else {
            return None;
        }
        let integer_digits = index - integer_start;
        let mut float = false;
        if self.chars.get(index) == Some(&'.') && digit(index + 1) {
            float = true;
            index += 1;
            while digit(index) {
                index += 1;
            }
        }
        if matches!(self.chars.get(index), Some('e' | 'E')) {
            let mut exponent = index + 1;
            if matches!(self.chars.get(exponent), Some('+' | '-')) {
                exponent += 1;
            }
            if digit(exponent) {
                float = true;
                index = exponent;
                while digit(index) {
                    index += 1;
                }
            }
        }
        if self.mode == Mode::Plain && !float && integer_digits > PYTHON_INT_MAX_STR_DIGITS {
            return None;
        }
        Some(index)
    }

    /// A scalar at `index`, or `None` for StopIteration / a decode error.
    fn scalar(&self, index: usize) -> Option<(PyVal, usize)> {
        let character = *self.chars.get(index)?;
        let constants = self.mode == Mode::Plain;
        match character {
            '"' => self.string(index + 1).map(|(text, end)| (PyVal::Str(text), end)),
            'n' if self.starts(index, "null") => Some((PyVal::Null, index + 4)),
            't' if self.starts(index, "true") => Some((PyVal::Bool(true), index + 4)),
            'f' if self.starts(index, "false") => Some((PyVal::Bool(false), index + 5)),
            'N' if self.starts(index, "NaN") => constants.then_some((PyVal::Number, index + 3)),
            'I' if self.starts(index, "Infinity") => constants.then_some((PyVal::Number, index + 8)),
            '-' if self.starts(index, "-Infinity") => constants.then_some((PyVal::Number, index + 9)),
            _ => self.number(index).map(|end| (PyVal::Number, end)),
        }
    }

    fn open(&self, stack: &mut Vec<Frame>, dict: bool) -> Option<()> {
        if stack.len() + 1 > PYTHON_JSON_NESTING_LIMIT {
            return None;
        }
        let keep = stack.len() < self.keep_depth;
        let container = if dict {
            Container::Dict { pairs: Vec::new(), keys: HashSet::new(), key: None }
        } else {
            Container::List(Vec::new())
        };
        stack.push(Frame { container, keep });
        Some(())
    }

    /// After `{` or `,` inside an object: the key and its `:`.
    fn key(&self, stack: &mut [Frame], index: usize) -> Option<usize> {
        if self.chars.get(index) != Some(&'"') {
            return None;
        }
        let (key, end) = self.string(index + 1)?;
        let index = self.skip_ws(end);
        if self.chars.get(index) != Some(&':') {
            return None;
        }
        if let Some(Frame { container: Container::Dict { key: slot, .. }, .. }) = stack.last_mut() {
            *slot = Some(key);
        }
        Some(self.skip_ws(index + 1))
    }

    fn close(stack: &mut Vec<Frame>) -> PyVal {
        let frame = stack.pop().expect("open container");
        if !frame.keep {
            return PyVal::Opaque;
        }
        match frame.container {
            Container::List(items) => PyVal::List(items),
            Container::Dict { pairs, .. } => PyVal::Dict(pairs),
        }
    }

    fn attach(&self, stack: &mut [Frame], value: PyVal) -> Option<()> {
        let frame = stack.last_mut().expect("open container");
        let keep = frame.keep;
        match &mut frame.container {
            Container::List(items) => {
                if keep {
                    items.push(value);
                }
            }
            Container::Dict { pairs, keys, key } => {
                let key = key.take().expect("pending key");
                if keys.contains(&key) {
                    if self.mode == Mode::Projection {
                        return None;
                    }
                    if keep {
                        if let Some(slot) = pairs.iter_mut().find(|(name, _)| *name == key) {
                            slot.1 = value;
                        }
                    }
                } else {
                    keys.insert(key.clone());
                    if keep {
                        pairs.push((key, value));
                    }
                }
            }
        }
        Some(())
    }

    fn parse(&self) -> Option<PyVal> {
        if self.chars.first() == Some(&'\u{feff}') {
            return None;
        }
        let mut stack: Vec<Frame> = Vec::new();
        let mut index = self.skip_ws(0);
        loop {
            // A value starts at `index`.
            let (mut value, mut end) = match self.chars.get(index) {
                Some('{') => {
                    self.open(&mut stack, true)?;
                    let inner = self.skip_ws(index + 1);
                    if self.chars.get(inner) == Some(&'}') {
                        (Self::close(&mut stack), inner + 1)
                    } else {
                        index = self.key(&mut stack, inner)?;
                        continue;
                    }
                }
                Some('[') => {
                    self.open(&mut stack, false)?;
                    let inner = self.skip_ws(index + 1);
                    if self.chars.get(inner) == Some(&']') {
                        (Self::close(&mut stack), inner + 1)
                    } else {
                        index = inner;
                        continue;
                    }
                }
                _ => self.scalar(index)?,
            };
            // Attach completed values until one container expects another.
            loop {
                if stack.is_empty() {
                    return (self.skip_ws(end) == self.chars.len()).then_some(value);
                }
                self.attach(&mut stack, value)?;
                let after = self.skip_ws(end);
                let is_dict = matches!(stack.last().map(|frame| &frame.container), Some(Container::Dict { .. }));
                let closing = if is_dict { '}' } else { ']' };
                match self.chars.get(after) {
                    Some(',') => {
                        let next = self.skip_ws(after + 1);
                        index = if is_dict { self.key(&mut stack, next)? } else { next };
                        break;
                    }
                    Some(character) if *character == closing => {
                        value = Self::close(&mut stack);
                        end = after + 1;
                    }
                    _ => return None,
                }
            }
        }
    }
}

/// Decode `text`, materializing containers nested at most `keep_depth`
/// deep (the root container is depth 1); `None` is any raised error.
pub fn loads(text: &str, mode: Mode, keep_depth: usize) -> Option<PyVal> {
    Scanner { chars: text.chars().collect(), mode, keep_depth }.parse()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_mode_refuses_what_load_projection_json_refuses() {
        assert!(loads(r#"{"a":1,"a":2}"#, Mode::Projection, 1).is_none());
        assert!(loads(r#"{"a":NaN}"#, Mode::Projection, 1).is_none());
        assert!(loads(r#"[1e400, 123456789012345678901234567890]"#, Mode::Projection, 1).is_some());
        assert!(loads("[1,]", Mode::Projection, 1).is_none());
        assert!(loads("\u{feff}{}", Mode::Projection, 1).is_none());
        assert!(loads("{\"a\":\"\u{1}\"}", Mode::Projection, 1).is_none());
        let deep = format!("{}{}", "[".repeat(9_997), "]".repeat(9_997));
        assert!(loads(&deep, Mode::Projection, 1).is_some());
        let deeper = format!("{}{}", "[".repeat(9_998), "]".repeat(9_998));
        assert!(loads(&deeper, Mode::Projection, 1).is_none());
    }

    #[test]
    fn plain_mode_keeps_the_last_duplicate_in_its_first_position() {
        let value = loads(r#"{"truncated":false,"x":1,"truncated":true}"#, Mode::Plain, 1).unwrap();
        let PyVal::Dict(pairs) = &value else { panic!("dict") };
        assert!(pairs[0].0.is("truncated"));
        assert!(matches!(pairs[0].1, PyVal::Bool(true)));
        assert!(loads("[NaN, -Infinity]", Mode::Plain, 1).is_some());
        assert!(loads(&format!("[{}]", "1".repeat(4_301)), Mode::Plain, 1).is_none());
        assert!(loads(&format!("[{}]", "1".repeat(4_301)), Mode::Projection, 1).is_some());
    }

    #[test]
    fn lone_surrogates_stay_distinct_code_points() {
        let value = loads(r#"{"a":"\ud800x","b":"\ud83d\ude00"}"#, Mode::Projection, 1).unwrap();
        assert!(matches!(value.get("a"), Some(PyVal::Str(PyStr::Raw(points))) if points.len() == 2));
        assert!(matches!(value.get("b"), Some(PyVal::Str(PyStr::Text(text))) if text == "\u{1f600}"));
    }
}
