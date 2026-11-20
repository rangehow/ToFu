//! CPython `re` character semantics the hand-written matchers rely on.
//!
//! The kernels replace a handful of fixed Python patterns with explicit
//! scanners. These helpers give them CPython's meaning of `\d`, `\w`, `\b` and
//! `re.IGNORECASE` for ASCII pattern letters (which also match U+0130, U+0131,
//! U+017F and U+212A), using tables exported from the interpreter.

use std::cmp::Ordering;

use crate::chars;
use crate::py_tables::{DECIMAL, IGNORECASE_EXTRA};

/// `re` `\d` for `str` patterns.
pub fn is_decimal(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_digit();
    }
    let cp = c as u32;
    DECIMAL
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

/// `re` `\w` for `str` patterns.
pub fn is_word(c: char) -> bool {
    chars::is_word(c)
}

/// `c` matches the lowercase ASCII pattern letter `letter` under `re.IGNORECASE`.
pub fn letter_ci(c: char, letter: char) -> bool {
    if c.is_ascii() {
        return c.to_ascii_lowercase() == letter;
    }
    IGNORECASE_EXTRA
        .iter()
        .any(|&(cp, l)| cp == c as u32 && l == letter)
}

/// `chars[at..]` starts with the lowercase ASCII `word` under `re.IGNORECASE`.
pub fn starts_with_ci(chars: &[char], at: usize, word: &str) -> bool {
    let n = word.len();
    at + n <= chars.len()
        && word
            .chars()
            .zip(&chars[at..at + n])
            .all(|(l, &c)| letter_ci(c, l))
}

/// `\b` at `pos` in `chars`.
pub fn word_boundary(chars: &[char], pos: usize) -> bool {
    let before = pos > 0 && is_word(chars[pos - 1]);
    let after = pos < chars.len() && is_word(chars[pos]);
    before != after
}

/// `re.sub(r'\s+', ' ', text)`.
pub fn collapse_runs(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut in_space = false;
    for c in text.chars() {
        if chars::is_space(c) {
            if !in_space {
                out.push(' ');
                in_space = true;
            }
        } else {
            out.push(c);
            in_space = false;
        }
    }
    out
}

/// `str.strip()`.
pub fn strip(text: &str) -> &str {
    text.trim_matches(chars::is_space)
}

/// `str.split()` (whitespace separated, no empty items).
pub fn split_whitespace(text: &str) -> impl Iterator<Item = &str> {
    text.split(chars::is_space).filter(|s| !s.is_empty())
}

/// `str.splitlines()` boundaries (without keepends).
pub fn splitlines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut iter = text.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        let is_break = matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if is_break {
            out.push(&text[start..i]);
            let mut next = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(j, '\n')) = iter.peek() {
                    iter.next();
                    next = j + 1;
                }
            }
            start = next;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes() {
        assert!(is_decimal('٣'));
        assert!(!is_decimal('²'));
        assert!(letter_ci('\u{212A}', 'k'));
        assert!(letter_ci('S', 's'));
        assert!(!letter_ci('k', 's'));
        assert_eq!(splitlines("a\r\nb\rc\u{2028}\n"), vec!["a", "b", "c", ""]);
        assert_eq!(collapse_runs(" a \n\t b "), " a b ");
    }
}
