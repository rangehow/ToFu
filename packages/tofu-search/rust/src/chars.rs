//! CPython-identical character classes and case mappings.
//!
//! The Python oracle uses `re` `\w`/`\s` and `str` predicates, which follow
//! the interpreter's bundled Unicode database. Tables are generated from that
//! interpreter (`scripts/gen_unicode_tables.py`), so classification does not
//! drift with the Rust standard library's Unicode version.

use std::cmp::Ordering;

use unicode_normalization::UnicodeNormalization;

use crate::unicode_tables::{
    ALNUM, ALPHA, ASSIGNED, CASED, CASEFOLD, CASE_IGNORABLE, DIGIT, LOWER, SPACE,
};

fn in_table(table: &[(u32, u32)], c: char) -> bool {
    let cp = c as u32;
    table
        .binary_search_by(|&(lo, hi)| {
            if hi < cp {
                Ordering::Less
            } else if lo > cp {
                Ordering::Greater
            } else {
                Ordering::Equal
            }
        })
        .is_ok()
}

fn mapped(table: &'static [(u32, &'static str)], c: char) -> Option<&'static str> {
    let cp = c as u32;
    table
        .binary_search_by(|&(key, _)| key.cmp(&cp))
        .ok()
        .map(|index| table[index].1)
}

/// `str.isalpha` for one character.
pub fn is_alpha(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphabetic();
    }
    in_table(ALPHA, c)
}

/// `str.isalnum` for one character.
pub fn is_alnum(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphanumeric();
    }
    in_table(ALNUM, c)
}

/// `str.isdigit` for one character.
pub fn is_digit(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_digit();
    }
    in_table(DIGIT, c)
}

/// `str.isspace`, identical to `re` `\s` for `str` patterns.
pub fn is_space(c: char) -> bool {
    if c.is_ascii() {
        return matches!(
            c,
            ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c' | '\x1c'..='\x1f'
        );
    }
    in_table(SPACE, c)
}

/// `re` `\w` for `str` patterns.
pub fn is_word(c: char) -> bool {
    c == '_' || is_alnum(c)
}

/// `unicodedata.normalize('NFKC', text)` under the interpreter's Unicode version.
///
/// The normalization crate may know newer characters. A code point unassigned
/// in the interpreter's database has no decomposition and canonical class 0
/// there, so it is an opaque starter that no composition crosses: normalize the
/// segments between such code points and keep them verbatim.
pub fn nfkc(text: &str) -> String {
    if text.is_ascii() {
        return text.to_owned();
    }
    let mut out = String::with_capacity(text.len());
    let mut segment_start = 0;
    for (offset, c) in text.char_indices() {
        if !c.is_ascii() && !in_table(ASSIGNED, c) {
            out.extend(text[segment_start..offset].nfkc());
            out.push(c);
            segment_start = offset + c.len_utf8();
        }
    }
    out.extend(text[segment_start..].nfkc());
    out
}

/// `str.casefold`.
pub fn casefold(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match mapped(CASEFOLD, c) {
            Some(folded) => out.push_str(folded),
            None => out.push(c),
        }
    }
    out
}

/// `str.lower`, including CPython's final-sigma context rule.
pub fn lower(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    for (index, &c) in chars.iter().enumerate() {
        if c == '\u{3A3}' {
            out.push(if final_sigma(&chars, index) {
                '\u{3C2}'
            } else {
                '\u{3C3}'
            });
            continue;
        }
        match mapped(LOWER, c) {
            Some(lowered) => out.push_str(lowered),
            None => out.push(c),
        }
    }
    out
}

fn final_sigma(chars: &[char], index: usize) -> bool {
    let before = chars[..index]
        .iter()
        .rev()
        .find(|&&c| !in_table(CASE_IGNORABLE, c));
    if !before.is_some_and(|&c| in_table(CASED, c)) {
        return false;
    }
    let after = chars[index + 1..]
        .iter()
        .find(|&&c| !in_table(CASE_IGNORABLE, c));
    !after.is_some_and(|&c| in_table(CASED, c))
}

/// `str.strip()` on a char slice, returned as a sub-slice.
pub fn strip(chars: &[char]) -> &[char] {
    let start = chars
        .iter()
        .position(|&c| !is_space(c))
        .unwrap_or(chars.len());
    let end = chars
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(start, |i| i + 1);
    &chars[start..end]
}

/// `str.rstrip()` on a char slice.
pub fn rstrip(chars: &[char]) -> &[char] {
    let end = chars
        .iter()
        .rposition(|&c| !is_space(c))
        .map_or(0, |i| i + 1);
    &chars[..end]
}

/// `' '.join(text.split())`.
pub fn collapse_whitespace(chars: &[char]) -> String {
    let mut out = String::with_capacity(chars.len());
    let mut pending_space = false;
    for &c in chars {
        if is_space(c) {
            pending_space = !out.is_empty();
        } else {
            if pending_space {
                out.push(' ');
                pending_space = false;
            }
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_follow_cpython_not_rust_std() {
        // Combining marks are alphabetic in Rust but not `\w` in CPython.
        assert!('\u{93E}'.is_alphabetic());
        assert!(!is_word('\u{93E}'));
        assert!(is_digit('\u{B2}'));
        assert!(is_space('\x1c'));
        assert!(!is_word('-'));
    }

    #[test]
    fn case_mappings_match_cpython() {
        assert_eq!(casefold("Straße"), "strasse");
        assert_eq!(lower("ΟΔΟΣ ΣΑ"), "οδος σα");
        assert_eq!(lower("İ"), "i\u{307}");
        assert_eq!(nfkc("ﬁ①"), "fi1");
    }

    #[test]
    fn whitespace_helpers() {
        let text: Vec<char> = "  a \n b  ".chars().collect();
        assert_eq!(strip(&text).iter().collect::<String>(), "a \n b");
        assert_eq!(rstrip(&text).iter().collect::<String>(), "  a \n b");
        assert_eq!(collapse_whitespace(&text), "a b");
    }
}
