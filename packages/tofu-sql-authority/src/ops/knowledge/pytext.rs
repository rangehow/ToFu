//! CPython text semantics the knowledge operations depend on: `str.casefold`,
//! `str.split()`/`str.strip()` whitespace and `json.loads` acceptance.

/// Characters whose `str.casefold()` differs from `str.lower()` (Unicode 15,
/// CPython 3.12), excluding the Cherokee ranges handled in `casefold_char`.
const CASEFOLD_SPECIAL: &[(char, &str)] = &[
    ('\u{B5}', "\u{3BC}"),
    ('\u{DF}', "\u{73}\u{73}"),
    ('\u{149}', "\u{2BC}\u{6E}"),
    ('\u{17F}', "\u{73}"),
    ('\u{1F0}', "\u{6A}\u{30C}"),
    ('\u{345}', "\u{3B9}"),
    ('\u{390}', "\u{3B9}\u{308}\u{301}"),
    ('\u{3B0}', "\u{3C5}\u{308}\u{301}"),
    ('\u{3C2}', "\u{3C3}"),
    ('\u{3D0}', "\u{3B2}"),
    ('\u{3D1}', "\u{3B8}"),
    ('\u{3D5}', "\u{3C6}"),
    ('\u{3D6}', "\u{3C0}"),
    ('\u{3F0}', "\u{3BA}"),
    ('\u{3F1}', "\u{3C1}"),
    ('\u{3F5}', "\u{3B5}"),
    ('\u{587}', "\u{565}\u{582}"),
    ('\u{1C80}', "\u{432}"),
    ('\u{1C81}', "\u{434}"),
    ('\u{1C82}', "\u{43E}"),
    ('\u{1C83}', "\u{441}"),
    ('\u{1C84}', "\u{442}"),
    ('\u{1C85}', "\u{442}"),
    ('\u{1C86}', "\u{44A}"),
    ('\u{1C87}', "\u{463}"),
    ('\u{1C88}', "\u{A64B}"),
    ('\u{1E96}', "\u{68}\u{331}"),
    ('\u{1E97}', "\u{74}\u{308}"),
    ('\u{1E98}', "\u{77}\u{30A}"),
    ('\u{1E99}', "\u{79}\u{30A}"),
    ('\u{1E9A}', "\u{61}\u{2BE}"),
    ('\u{1E9B}', "\u{1E61}"),
    ('\u{1E9E}', "\u{73}\u{73}"),
    ('\u{1F50}', "\u{3C5}\u{313}"),
    ('\u{1F52}', "\u{3C5}\u{313}\u{300}"),
    ('\u{1F54}', "\u{3C5}\u{313}\u{301}"),
    ('\u{1F56}', "\u{3C5}\u{313}\u{342}"),
    ('\u{1F80}', "\u{1F00}\u{3B9}"),
    ('\u{1F81}', "\u{1F01}\u{3B9}"),
    ('\u{1F82}', "\u{1F02}\u{3B9}"),
    ('\u{1F83}', "\u{1F03}\u{3B9}"),
    ('\u{1F84}', "\u{1F04}\u{3B9}"),
    ('\u{1F85}', "\u{1F05}\u{3B9}"),
    ('\u{1F86}', "\u{1F06}\u{3B9}"),
    ('\u{1F87}', "\u{1F07}\u{3B9}"),
    ('\u{1F88}', "\u{1F00}\u{3B9}"),
    ('\u{1F89}', "\u{1F01}\u{3B9}"),
    ('\u{1F8A}', "\u{1F02}\u{3B9}"),
    ('\u{1F8B}', "\u{1F03}\u{3B9}"),
    ('\u{1F8C}', "\u{1F04}\u{3B9}"),
    ('\u{1F8D}', "\u{1F05}\u{3B9}"),
    ('\u{1F8E}', "\u{1F06}\u{3B9}"),
    ('\u{1F8F}', "\u{1F07}\u{3B9}"),
    ('\u{1F90}', "\u{1F20}\u{3B9}"),
    ('\u{1F91}', "\u{1F21}\u{3B9}"),
    ('\u{1F92}', "\u{1F22}\u{3B9}"),
    ('\u{1F93}', "\u{1F23}\u{3B9}"),
    ('\u{1F94}', "\u{1F24}\u{3B9}"),
    ('\u{1F95}', "\u{1F25}\u{3B9}"),
    ('\u{1F96}', "\u{1F26}\u{3B9}"),
    ('\u{1F97}', "\u{1F27}\u{3B9}"),
    ('\u{1F98}', "\u{1F20}\u{3B9}"),
    ('\u{1F99}', "\u{1F21}\u{3B9}"),
    ('\u{1F9A}', "\u{1F22}\u{3B9}"),
    ('\u{1F9B}', "\u{1F23}\u{3B9}"),
    ('\u{1F9C}', "\u{1F24}\u{3B9}"),
    ('\u{1F9D}', "\u{1F25}\u{3B9}"),
    ('\u{1F9E}', "\u{1F26}\u{3B9}"),
    ('\u{1F9F}', "\u{1F27}\u{3B9}"),
    ('\u{1FA0}', "\u{1F60}\u{3B9}"),
    ('\u{1FA1}', "\u{1F61}\u{3B9}"),
    ('\u{1FA2}', "\u{1F62}\u{3B9}"),
    ('\u{1FA3}', "\u{1F63}\u{3B9}"),
    ('\u{1FA4}', "\u{1F64}\u{3B9}"),
    ('\u{1FA5}', "\u{1F65}\u{3B9}"),
    ('\u{1FA6}', "\u{1F66}\u{3B9}"),
    ('\u{1FA7}', "\u{1F67}\u{3B9}"),
    ('\u{1FA8}', "\u{1F60}\u{3B9}"),
    ('\u{1FA9}', "\u{1F61}\u{3B9}"),
    ('\u{1FAA}', "\u{1F62}\u{3B9}"),
    ('\u{1FAB}', "\u{1F63}\u{3B9}"),
    ('\u{1FAC}', "\u{1F64}\u{3B9}"),
    ('\u{1FAD}', "\u{1F65}\u{3B9}"),
    ('\u{1FAE}', "\u{1F66}\u{3B9}"),
    ('\u{1FAF}', "\u{1F67}\u{3B9}"),
    ('\u{1FB2}', "\u{1F70}\u{3B9}"),
    ('\u{1FB3}', "\u{3B1}\u{3B9}"),
    ('\u{1FB4}', "\u{3AC}\u{3B9}"),
    ('\u{1FB6}', "\u{3B1}\u{342}"),
    ('\u{1FB7}', "\u{3B1}\u{342}\u{3B9}"),
    ('\u{1FBC}', "\u{3B1}\u{3B9}"),
    ('\u{1FBE}', "\u{3B9}"),
    ('\u{1FC2}', "\u{1F74}\u{3B9}"),
    ('\u{1FC3}', "\u{3B7}\u{3B9}"),
    ('\u{1FC4}', "\u{3AE}\u{3B9}"),
    ('\u{1FC6}', "\u{3B7}\u{342}"),
    ('\u{1FC7}', "\u{3B7}\u{342}\u{3B9}"),
    ('\u{1FCC}', "\u{3B7}\u{3B9}"),
    ('\u{1FD2}', "\u{3B9}\u{308}\u{300}"),
    ('\u{1FD3}', "\u{3B9}\u{308}\u{301}"),
    ('\u{1FD6}', "\u{3B9}\u{342}"),
    ('\u{1FD7}', "\u{3B9}\u{308}\u{342}"),
    ('\u{1FE2}', "\u{3C5}\u{308}\u{300}"),
    ('\u{1FE3}', "\u{3C5}\u{308}\u{301}"),
    ('\u{1FE4}', "\u{3C1}\u{313}"),
    ('\u{1FE6}', "\u{3C5}\u{342}"),
    ('\u{1FE7}', "\u{3C5}\u{308}\u{342}"),
    ('\u{1FF2}', "\u{1F7C}\u{3B9}"),
    ('\u{1FF3}', "\u{3C9}\u{3B9}"),
    ('\u{1FF4}', "\u{3CE}\u{3B9}"),
    ('\u{1FF6}', "\u{3C9}\u{342}"),
    ('\u{1FF7}', "\u{3C9}\u{342}\u{3B9}"),
    ('\u{1FFC}', "\u{3C9}\u{3B9}"),
    ('\u{FB00}', "\u{66}\u{66}"),
    ('\u{FB01}', "\u{66}\u{69}"),
    ('\u{FB02}', "\u{66}\u{6C}"),
    ('\u{FB03}', "\u{66}\u{66}\u{69}"),
    ('\u{FB04}', "\u{66}\u{66}\u{6C}"),
    ('\u{FB05}', "\u{73}\u{74}"),
    ('\u{FB06}', "\u{73}\u{74}"),
    ('\u{FB13}', "\u{574}\u{576}"),
    ('\u{FB14}', "\u{574}\u{565}"),
    ('\u{FB15}', "\u{574}\u{56B}"),
    ('\u{FB16}', "\u{57E}\u{576}"),
    ('\u{FB17}', "\u{574}\u{56D}"),
];

fn casefold_char(character: char, out: &mut String) {
    let code = character as u32;
    // Case folding maps Cherokee to its uppercase letters, unlike lower().
    if (0x13A0..=0x13F5).contains(&code) {
        out.push(character);
        return;
    }
    if (0x13F8..=0x13FD).contains(&code) {
        out.push(char::from_u32(code - 8).expect("Cherokee letter"));
        return;
    }
    if (0xAB70..=0xABBF).contains(&code) {
        out.push(char::from_u32(code - 0xAB70 + 0x13A0).expect("Cherokee letter"));
        return;
    }
    if let Ok(index) = CASEFOLD_SPECIAL.binary_search_by(|(key, _)| key.cmp(&character)) {
        out.push_str(CASEFOLD_SPECIAL[index].1);
        return;
    }
    out.extend(character.to_lowercase());
}

/// Python `str.casefold()` (full case folding, per character).
pub fn casefold(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for character in text.chars() {
        casefold_char(character, &mut out);
    }
    out
}

/// Python `str.isspace()` for one character.
pub fn is_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python `str.split()` with no separator.
pub fn split(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_space).filter(|part| !part.is_empty())
}

/// Python `str.strip()` with no argument.
pub fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

#[derive(Debug, PartialEq, Eq)]
pub enum JsonKind {
    Array,
    Object,
    Scalar,
}

#[derive(Debug, PartialEq, Eq)]
pub enum JsonCheck {
    Valid(JsonKind),
    /// `json.JSONDecodeError` (a `ValueError`).
    Invalid,
    /// `RecursionError`: not a `ValueError`, so the caller does not catch it.
    TooDeep,
}

/// CPython 3.12's C recursion budget: `json.loads('[' * 9998 + ...)` raises
/// `RecursionError` while 9997 nested containers decode.
const PYTHON_JSON_NESTING_LIMIT: usize = 9998;

fn skip_ws(bytes: &[u8], index: &mut usize) {
    while matches!(bytes.get(*index), Some(b' ' | b'\t' | b'\n' | b'\r')) {
        *index += 1;
    }
}

fn scan_string(bytes: &[u8], index: &mut usize) -> bool {
    // `index` points at the opening quote.
    *index += 1;
    loop {
        match bytes.get(*index) {
            None => return false,
            Some(b'"') => {
                *index += 1;
                return true;
            }
            Some(b'\\') => match bytes.get(*index + 1) {
                Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => *index += 2,
                Some(b'u') => {
                    let digits = bytes.get(*index + 2..*index + 6);
                    if !digits.is_some_and(|digits| digits.iter().all(u8::is_ascii_hexdigit)) {
                        return false;
                    }
                    *index += 6;
                }
                _ => return false,
            },
            Some(byte) if *byte < 0x20 => return false,
            Some(_) => *index += 1,
        }
    }
}

fn scan_digits(bytes: &[u8], index: &mut usize) -> usize {
    let start = *index;
    while bytes.get(*index).is_some_and(u8::is_ascii_digit) {
        *index += 1;
    }
    *index - start
}

fn scan_number(bytes: &[u8], index: &mut usize) -> bool {
    if bytes.get(*index) == Some(&b'-') {
        *index += 1;
    }
    match bytes.get(*index) {
        Some(b'0') => *index += 1,
        Some(b'1'..=b'9') => {
            scan_digits(bytes, index);
        }
        _ => return false,
    }
    if bytes.get(*index) == Some(&b'.') && bytes.get(*index + 1).is_some_and(u8::is_ascii_digit) {
        *index += 1;
        scan_digits(bytes, index);
    }
    if matches!(bytes.get(*index), Some(b'e' | b'E')) {
        let exponent_start = *index;
        *index += 1;
        if matches!(bytes.get(*index), Some(b'+' | b'-')) {
            *index += 1;
        }
        if scan_digits(bytes, index) == 0 {
            *index = exponent_start;
        }
    }
    true
}

fn literal(bytes: &[u8], index: &mut usize, word: &[u8]) -> bool {
    if bytes[*index..].starts_with(word) {
        *index += word.len();
        true
    } else {
        false
    }
}

/// Python `json.loads(text)` acceptance and the decoded top-level type,
/// including the `NaN`/`Infinity` constants and lone-surrogate escapes that
/// serde_json refuses.
pub fn json_kind(text: &str) -> JsonCheck {
    if text.starts_with('\u{feff}') {
        return JsonCheck::Invalid;
    }
    let bytes = text.as_bytes();
    let mut index = 0usize;
    skip_ws(bytes, &mut index);
    let kind = match bytes.get(index) {
        Some(b'[') => JsonKind::Array,
        Some(b'{') => JsonKind::Object,
        _ => JsonKind::Scalar,
    };
    let mut stack: Vec<u8> = Vec::new();
    'value: loop {
        skip_ws(bytes, &mut index);
        let Some(&byte) = bytes.get(index) else { return JsonCheck::Invalid };
        match byte {
            b'[' | b'{' => {
                stack.push(byte);
                if stack.len() >= PYTHON_JSON_NESTING_LIMIT {
                    return JsonCheck::TooDeep;
                }
                index += 1;
                skip_ws(bytes, &mut index);
                let close = if byte == b'[' { b']' } else { b'}' };
                if bytes.get(index) == Some(&close) {
                    index += 1;
                    stack.pop();
                } else if byte == b'{' {
                    if bytes.get(index) != Some(&b'"') || !scan_string(bytes, &mut index) {
                        return JsonCheck::Invalid;
                    }
                    skip_ws(bytes, &mut index);
                    if bytes.get(index) != Some(&b':') {
                        return JsonCheck::Invalid;
                    }
                    index += 1;
                    continue 'value;
                } else {
                    continue 'value;
                }
            }
            b'"' => {
                if !scan_string(bytes, &mut index) {
                    return JsonCheck::Invalid;
                }
            }
            b'n' | b't' | b'f' | b'N' | b'I' => {
                let word: &[u8] = match byte {
                    b'n' => b"null",
                    b't' => b"true",
                    b'f' => b"false",
                    b'N' => b"NaN",
                    _ => b"Infinity",
                };
                if !literal(bytes, &mut index, word) {
                    return JsonCheck::Invalid;
                }
            }
            b'-' if bytes.get(index + 1) == Some(&b'I') => {
                if !literal(bytes, &mut index, b"-Infinity") {
                    return JsonCheck::Invalid;
                }
            }
            b'-' | b'0'..=b'9' => {
                if !scan_number(bytes, &mut index) {
                    return JsonCheck::Invalid;
                }
            }
            _ => return JsonCheck::Invalid,
        }
        loop {
            skip_ws(bytes, &mut index);
            match stack.last() {
                None => {
                    return if index == bytes.len() { JsonCheck::Valid(kind) } else { JsonCheck::Invalid };
                }
                Some(b'[') => match bytes.get(index) {
                    Some(b',') => {
                        index += 1;
                        continue 'value;
                    }
                    Some(b']') => {
                        index += 1;
                        stack.pop();
                    }
                    _ => return JsonCheck::Invalid,
                },
                Some(_) => match bytes.get(index) {
                    Some(b',') => {
                        index += 1;
                        skip_ws(bytes, &mut index);
                        if bytes.get(index) != Some(&b'"') || !scan_string(bytes, &mut index) {
                            return JsonCheck::Invalid;
                        }
                        skip_ws(bytes, &mut index);
                        if bytes.get(index) != Some(&b':') {
                            return JsonCheck::Invalid;
                        }
                        index += 1;
                        continue 'value;
                    }
                    Some(b'}') => {
                        index += 1;
                        stack.pop();
                    }
                    _ => return JsonCheck::Invalid,
                },
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn casefold_matches_cpython() {
        assert_eq!(casefold("Hello WORLD \u{391}\u{3b2}"), "hello world \u{3b1}\u{3b2}");
        assert_eq!(casefold("Stra\u{df}e"), "strasse");
        assert_eq!(casefold("\u{3c2}\u{13a0}\u{ab70}\u{fb01}"), "\u{3c3}\u{13a0}\u{13a0}fi");
        assert_eq!(casefold("\u{130}"), "i\u{307}");
    }

    #[test]
    fn whitespace_matches_cpython() {
        assert_eq!(split(" a\u{1c}b\u{3000}c\t").collect::<Vec<_>>(), ["a", "b", "c"]);
        assert_eq!(strip("\u{1f} x \u{85}"), "x");
    }

    #[test]
    fn json_acceptance_matches_cpython() {
        use JsonCheck::*;
        assert_eq!(json_kind(" [1, -0.5e+3, NaN, -Infinity, \"\\ud800\"] "), Valid(JsonKind::Array));
        assert_eq!(json_kind("{\"a\": {\"b\": []}, \"a\": null}"), Valid(JsonKind::Object));
        assert_eq!(json_kind("{}"), Valid(JsonKind::Object));
        assert_eq!(json_kind("[]"), Valid(JsonKind::Array));
        assert_eq!(json_kind("\"x\""), Valid(JsonKind::Scalar));
        for invalid in [
            "",
            "[1,]",
            "{\"a\":1,}",
            "[01]",
            "[1.]",
            "[1e]",
            "{1:2}",
            "[\"\\x\"]",
            "[\"\u{1}\"]",
            "[] x",
            "\u{feff}[]",
            "[-]",
            "nul",
        ] {
            assert_eq!(json_kind(invalid), Invalid, "{invalid:?}");
        }
        let deep = |depth: usize| format!("{}{}", "[".repeat(depth), "]".repeat(depth));
        assert_eq!(json_kind(&deep(9997)), Valid(JsonKind::Array));
        assert_eq!(json_kind(&deep(9998)), TooDeep);
    }
}
