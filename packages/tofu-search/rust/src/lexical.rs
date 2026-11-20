//! Port of `tofu_search/lexical.py`: tokens, matching text and query subjects.
//!
//! Positions are code-point indices, as in the Python oracle.

use std::collections::HashSet;

use crate::chars::{casefold, is_alnum, is_alpha, is_digit, is_space, is_word, nfkc};

const COMPLEXITY_NOTATION_CHARS: usize = 128;

const STOP_WORDS: &[&str] = &[
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "has", "have", "he",
    "in", "is", "it", "its", "of", "on", "or", "she", "so", "the", "to", "was", "we", "will",
    "with", "you", "that", "this", "not", "but", "they", "what", "all", "if", "can", "had", "her",
    "his", "how", "may", "no", "our", "out", "too", "use", "when", "where", "which", "who", "why",
    "new", "get", "set", "one", "two", "any", "www", "http", "https", "com", "org", "html",
];

const STRUCTURED_OPERATORS: &[&str] = &[
    "site",
    "filetype",
    "ext",
    "inurl",
    "intitle",
    "intext",
    "allinurl",
    "allintitle",
    "allintext",
    "before",
    "after",
    "daterange",
    "related",
    "cache",
    "source",
    "language",
    "lang",
];

/// The lexical module's CJK class.
pub fn is_cjk(c: char) -> bool {
    matches!(c, '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}' | '\u{3040}'..='\u{30ff}'
        | '\u{ac00}'..='\u{d7af}')
}

fn is_plain_word(c: char) -> bool {
    c != '_' && !is_cjk(c) && is_word(c)
}

/// `_WORD.finditer` spans: `[CJK]+|[^\W_CJK]+`.
fn word_spans(text: &[char]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut index = 0;
    while index < text.len() {
        let c = text[index];
        let class: fn(char) -> bool = if is_cjk(c) {
            is_cjk
        } else if is_plain_word(c) {
            is_plain_word
        } else {
            index += 1;
            continue;
        };
        let start = index;
        while index < text.len() && class(text[index]) {
            index += 1;
        }
        spans.push((start, index));
    }
    spans
}

fn starts_with(text: &[char], at: usize, needle: &str) -> bool {
    (at..)
        .zip(needle.chars())
        .all(|(index, expected)| text.get(index) == Some(&expected))
}

/// `_lexemes`: Unicode words plus bounded symbolic identifiers and notation.
///
/// Every lexeme is a contiguous span of the normalized text (a word, a word
/// with its `++`/`#` suffix, or a notation expression), so spans are returned.
fn lexemes(text: &str) -> (Vec<char>, Vec<(usize, usize)>) {
    let text: Vec<char> = casefold(&nfkc(text)).chars().collect();
    let spans = word_spans(&text);
    if !text.iter().any(|&c| c == '+' || c == '#' || c == '(') {
        return (text, spans);
    }
    let mut out = Vec::new();
    let mut consumed = 0usize;
    for (start, end) in spans {
        if start < consumed {
            continue;
        }
        let word = &text[start..end];
        if word == ['o'] || word == ['ω'] || word == ['θ'] {
            let mut opening = end;
            while opening < text.len() && matches!(text[opening], ' ' | '\t') {
                opening += 1;
            }
            if opening < text.len() && text[opening] == '(' {
                let mut depth: i64 = 0;
                let mut value = false;
                let limit = text.len().min(opening + COMPLEXITY_NOTATION_CHARS);
                for (index, &c) in text.iter().enumerate().take(limit).skip(opening) {
                    if !(is_alnum(c) || "()_+-*/^!., \t".contains(c)) {
                        break;
                    }
                    value = value || is_alnum(c);
                    depth += i64::from(c == '(') - i64::from(c == ')');
                    if depth == 0 {
                        if value {
                            consumed = index + 1;
                            out.push((start, consumed));
                        }
                        break;
                    }
                }
                if consumed > end {
                    continue;
                }
            }
        }
        let suffix: &[char] = if starts_with(&text, end, "++") {
            &['+', '+']
        } else if starts_with(&text, end, "#") {
            &['#']
        } else {
            &[]
        };
        let following = text.get(end + suffix.len()).copied();
        let joins = !suffix.is_empty()
            && !matches!(following, Some('+') | Some('#'))
            && !(suffix == ['#'] && following.is_some_and(|c| is_alpha(c) || c == '_'));
        if joins {
            consumed = end + suffix.len();
            out.push((start, consumed));
        } else {
            out.push((start, end));
        }
    }
    (text, out)
}

fn is_notation_operator(c: char) -> bool {
    matches!(c, '(' | ')' | '+' | '*' | '/' | '^' | '!' | ',' | '-')
}

/// `_canonical_lexeme`: spaces around notation operators are not identity.
fn canonical_lexeme(lexeme: &[char]) -> String {
    if !lexeme.contains(&'(') {
        return lexeme.iter().collect();
    }
    let collapsed: Vec<char> = crate::chars::collapse_whitespace(lexeme).chars().collect();
    let mut out = String::with_capacity(collapsed.len());
    for (index, &c) in collapsed.iter().enumerate() {
        if c == ' ' {
            let after_operator = index > 0 && is_notation_operator(collapsed[index - 1]);
            let before_operator = collapsed
                .get(index + 1)
                .is_some_and(|&n| is_notation_operator(n));
            if after_operator || before_operator {
                continue;
            }
        }
        out.push(c);
    }
    out
}

/// `matching_text`: boundary-preserving subject matching text.
pub fn matching_text(text: &str) -> String {
    let (text, spans) = lexemes(text);
    spans
        .iter()
        .map(|&(start, end)| canonical_lexeme(&text[start..end]))
        .collect::<Vec<_>>()
        .join(" ")
}

fn is_cjk_run(word: &[char]) -> bool {
    !word.is_empty() && word.iter().all(|&c| is_cjk(c))
}

fn is_stop_word(word: &[char]) -> bool {
    word.len() <= 5
        && STOP_WORDS
            .iter()
            .any(|stop| stop.len() == word.len() && stop.chars().zip(word).all(|(a, &b)| a == b))
}

fn push_word(tokens: &mut Vec<String>, word: &[char]) {
    if is_cjk_run(word) {
        for pair in word.windows(2) {
            tokens.push(pair.iter().collect());
        }
        if word.len() <= 2 {
            tokens.extend(word.iter().map(|c| c.to_string()));
        }
    } else if !is_stop_word(word) && (word.len() > 1 || word.iter().all(|&c| is_digit(c))) {
        tokens.push(word.iter().collect());
    }
}

/// `tokenize`: Unicode words and contiguous CJK bigrams.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let (text, spans) = lexemes(text);
    for (start, end) in spans {
        let unit = &text[start..end];
        let technical =
            unit.contains(&'(') || unit.ends_with(&['+', '+']) || unit.ends_with(&['#']);
        if technical {
            tokens.push(canonical_lexeme(unit));
            for (start, end) in word_spans(unit) {
                push_word(&mut tokens, &unit[start..end]);
            }
        } else {
            push_word(&mut tokens, unit);
        }
    }
    tokens
}

/// `subject_terms`: query-subject terms in the `tokenize` space.
pub fn subject_terms(query: &str) -> HashSet<String> {
    let terms: HashSet<String> = tokenize(&query_subject(query)).into_iter().collect();
    let bigram_chars: HashSet<String> = terms
        .iter()
        .filter(|term| {
            let chars: Vec<char> = term.chars().collect();
            chars.len() == 2 && is_cjk_run(&chars)
        })
        .flat_map(|term| term.chars().map(|c| c.to_string()).collect::<Vec<_>>())
        .collect();
    terms
        .into_iter()
        .filter(|term| !bigram_chars.contains(term))
        .collect()
}

/// `(?:[+-]?[A-Za-z]+:|[+-])?"[^"]*"` at `at`; returns the match end.
fn quoted_part_end(text: &[char], at: usize) -> Option<usize> {
    let closing = |open: usize| -> Option<usize> {
        if text.get(open) != Some(&'"') {
            return None;
        }
        text[open + 1..]
            .iter()
            .position(|&c| c == '"')
            .map(|p| open + 1 + p + 1)
    };
    let mut index = at;
    if matches!(text.get(index), Some('+') | Some('-')) {
        index += 1;
    }
    let letters = text[index.min(text.len())..]
        .iter()
        .take_while(|c| c.is_ascii_alphabetic())
        .count();
    if letters > 0 && text.get(index + letters) == Some(&':') {
        if let Some(end) = closing(index + letters + 1) {
            return Some(end);
        }
    }
    if matches!(text.get(at), Some('+') | Some('-')) {
        if let Some(end) = closing(at + 1) {
            return Some(end);
        }
    }
    closing(at)
}

/// `_QUERY_PART.finditer` match spans.
fn query_parts(text: &[char]) -> Vec<(usize, usize)> {
    let mut parts = Vec::new();
    let mut index = 0;
    while index < text.len() {
        if is_space(text[index]) {
            index += 1;
            continue;
        }
        let end = quoted_part_end(text, index)
            .unwrap_or_else(|| index + text[index..].iter().take_while(|&&c| !is_space(c)).count());
        parts.push((index, end));
        index = end;
    }
    parts
}

/// `re.I` equality against an ASCII-lowercase pattern letter.
fn ignorecase_eq(c: char, pattern: char) -> bool {
    c.to_ascii_lowercase() == pattern
        || (pattern == 's' && c == '\u{17F}')
        || (pattern == 'i' && matches!(c, '\u{130}' | '\u{131}'))
        || (pattern == 'k' && c == '\u{212A}')
}

fn is_structured_operator(part: &[char]) -> bool {
    let body = match part.first() {
        Some('+') | Some('-') => &part[1..],
        _ => part,
    };
    STRUCTURED_OPERATORS.iter().any(|name| {
        let length = name.chars().count();
        body.len() > length
            && body[length] == ':'
            && name.chars().zip(body).all(|(p, &c)| ignorecase_eq(c, p))
    })
}

/// `query_subject`: remove retrieval constraints, keep quoted literals.
pub fn query_subject(query: &str) -> String {
    let text: Vec<char> = query.chars().collect();
    let mut subjects: Vec<String> = Vec::new();
    for (start, end) in query_parts(&text) {
        let part = &text[start..end];
        if part[0] == '"' {
            // `part[1:-1]`; an unterminated `"word` reaches here through `\S+`.
            let inner = if part.len() >= 2 {
                &part[1..part.len() - 1]
            } else {
                &[][..]
            };
            subjects.push(inner.iter().collect());
            continue;
        }
        let stripped_both: String = {
            let s = part
                .iter()
                .position(|&c| c != '(' && c != ')')
                .unwrap_or(part.len());
            let e = part
                .iter()
                .rposition(|&c| c != '(' && c != ')')
                .map_or(s, |i| i + 1);
            part[s..e].iter().collect()
        };
        let lstripped = &part[part.iter().position(|&c| c != '(').unwrap_or(part.len())..];
        if !matches!(stripped_both.as_str(), "OR" | "AND" | "NOT")
            && !is_structured_operator(lstripped)
            && lstripped.first() != Some(&'-')
        {
            subjects.push(part.iter().collect());
        }
    }
    subjects.join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenize_words_cjk_and_stop_words() {
        assert_eq!(tokenize("The Rust 2 book"), vec!["rust", "2", "book"]);
        assert_eq!(tokenize("中文搜索"), vec!["中文", "文搜", "搜索"]);
        assert_eq!(tokenize("中文"), vec!["中文", "中", "文"]);
    }

    #[test]
    fn tokenize_technical_notation() {
        assert_eq!(tokenize("C++ and C#"), vec!["c++", "c#"]);
        assert_eq!(tokenize("O(n log n)"), vec!["o(n log n)", "log"]);
        assert_eq!(tokenize("counter++"), vec!["counter++", "counter"]);
        assert_eq!(matching_text("O( n + 1 )"), "o(n+1)");
    }

    #[test]
    fn query_subject_drops_operators() {
        assert_eq!(
            query_subject("rust site:docs.rs \"async trait\" -tokio OR intitle:\"x y\""),
            "rust async trait"
        );
        assert_eq!(query_subject("(AND) foo:bar"), "foo:bar");
        assert_eq!(query_subject("\"unterminated x \""), "unterminated x ");
        assert_eq!(query_subject("a \""), "a ");
    }

    #[test]
    fn subject_terms_drop_chars_of_bigrams() {
        let terms = subject_terms("中文 文");
        assert!(terms.contains("中文"));
        assert!(!terms.contains("文"));
    }
}
