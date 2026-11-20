//! Hand-ported `re` patterns of the in-stream error classifiers
//! (`lib/llm/llm_errors.py`, `lib/model_info/_limits.py`). Matching runs on
//! characters with CPython's Unicode `\s`, `\d` and `re.IGNORECASE` rules
//! (`py::space`, `py::decimal`, `py::ci_eq`).

use std::collections::BTreeSet;

use super::py;

type Chars = [char];

fn chars(text: &str) -> Vec<char> {
    text.chars().collect()
}

fn collect(text: &Chars) -> String {
    text.iter().collect()
}

/// `\s*` from `at`.
fn ws0(text: &Chars, at: usize) -> usize {
    let mut end = at;
    while end < text.len() && py::space(text[end]) {
        end += 1;
    }
    end
}

/// `\s+` from `at`.
fn ws1(text: &Chars, at: usize) -> Option<usize> {
    let end = ws0(text, at);
    (end > at).then_some(end)
}

/// `\d+` from `at`.
fn digits(text: &Chars, at: usize) -> Option<usize> {
    let mut end = at;
    while end < text.len() && py::decimal(text[end]).is_some() {
        end += 1;
    }
    (end > at).then_some(end)
}

fn lit(text: &Chars, at: usize, pattern: &str) -> Option<usize> {
    let mut end = at;
    for expected in pattern.chars() {
        if text.get(end) != Some(&expected) {
            return None;
        }
        end += 1;
    }
    Some(end)
}

/// A case-insensitive literal (ASCII pattern).
fn lit_ci(text: &Chars, at: usize, pattern: &str) -> Option<usize> {
    let mut end = at;
    for expected in pattern.chars() {
        match text.get(end) {
            Some(ch) if py::ci_eq(*ch, expected) => end += 1,
            _ => return None,
        }
    }
    Some(end)
}

/// Words joined by `\s+`, matched case-insensitively.
fn words_ci(text: &Chars, at: usize, words: &[&str]) -> Option<usize> {
    let mut end = at;
    for (position, word) in words.iter().enumerate() {
        if position > 0 {
            end = ws1(text, end)?;
        }
        end = lit_ci(text, end, word)?;
    }
    Some(end)
}

fn one_of(text: &Chars, at: usize, options: &[char]) -> Option<usize> {
    text.get(at)
        .filter(|ch| options.contains(ch))
        .map(|_| at + 1)
}

/// Byte offset of character index `at`.
fn byte_offset(text: &str, at: usize) -> usize {
    text.char_indices()
        .nth(at)
        .map_or(text.len(), |(offset, _)| offset)
}

/// `^API HTTP \d+:\s*` → the text after the prefix (or the text itself).
pub(crate) fn strip_api_http_prefix(text: &str) -> &str {
    let t = chars(text);
    let end = lit(&t, 0, "API HTTP ")
        .and_then(|end| digits(&t, end))
        .and_then(|end| lit(&t, end, ":"))
        .map(|end| ws0(&t, end));
    match end {
        Some(end) => &text[byte_offset(text, end)..],
        None => text,
    }
}

/// The three `\d` characters after the first `needle[:\s]+` (or right after
/// `needle` when `separator` is false), as `re.search(...).group(1)`.
pub(crate) fn three_digits_after(text: &str, needle: &str, separator: bool) -> Option<String> {
    let t = chars(text);
    for start in 0..t.len() {
        let Some(mut end) = lit(&t, start, needle) else {
            continue;
        };
        if separator {
            let after = end;
            while end < t.len() && (t[end] == ':' || py::space(t[end])) {
                end += 1;
            }
            if end == after {
                continue;
            }
        }
        if end + 3 <= t.len() && t[end..end + 3].iter().all(|ch| py::decimal(*ch).is_some()) {
            return Some(collect(&t[end..end + 3]));
        }
    }
    None
}

/// `_WRAPPED_OVERLOAD_RE.search` (case-insensitive).
pub(crate) fn is_wrapped_overload(text: &str) -> bool {
    let t = chars(text);
    let overload = |at: usize| lit(&t, at, "429").or_else(|| lit(&t, at, "529"));
    (0..t.len()).any(|start| {
        let constant = lit_ci(&t, start, "No matching constant for [")
            .and_then(overload)
            .and_then(|end| lit(&t, end, "]"));
        let status_field = lit_ci(&t, start, "\"status\"")
            .and_then(|end| lit(&t, ws0(&t, end), ":"))
            .and_then(|end| overload(ws0(&t, end)));
        let status_code = lit_ci(&t, start, "status").and_then(|mut end| {
            while end < t.len() && (t[end] == '_' || py::space(t[end])) {
                end += 1;
            }
            let mut end = lit_ci(&t, end, "code")?;
            while end < t.len() && (t[end] == '"' || t[end] == ':' || py::space(t[end])) {
                end += 1;
            }
            overload(end)
        });
        constant.is_some() || status_field.is_some() || status_code.is_some()
    })
}

/// `_parse_token_limit_evidence(...).maximum_allowed`.
pub(crate) fn token_limit(text: &str, max_limit: i64) -> Option<i64> {
    let t = chars(text);
    type Tail = fn(&Chars, usize) -> Option<(usize, usize)>;
    let patterns: [(bool, Tail); 5] = [
        (false, range_exclusive),
        (false, less_than),
        (true, bracket_range),
        (true, at_most),
        (true, between),
    ];
    for (inclusive, tail) in patterns {
        let Some((start, end)) = search_token_field(&t, tail) else {
            continue;
        };
        let Some(boundary) = py::decimal_value(&t[start..end], max_limit + 1) else {
            continue;
        };
        let maximum = if inclusive { boundary } else { boundary - 1 };
        if (1..=max_limit).contains(&maximum) {
            return Some(maximum);
        }
    }
    None
}

/// `(?P<field>max_tokens|max_output_tokens|maxOutputTokens).*?<tail>` with
/// `re.IGNORECASE`: the limit group of the leftmost match.
fn search_token_field(
    t: &Chars,
    tail: fn(&Chars, usize) -> Option<(usize, usize)>,
) -> Option<(usize, usize)> {
    for start in 0..t.len() {
        let Some(field_end) = ["max_tokens", "max_output_tokens", "maxOutputTokens"]
            .iter()
            .find_map(|field| lit_ci(t, start, field))
        else {
            continue;
        };
        let mut gap = field_end;
        loop {
            if let Some(limit) = tail(t, gap) {
                return Some(limit);
            }
            if gap >= t.len() || t[gap] == '\n' {
                break;
            }
            gap += 1;
        }
    }
    None
}

/// `range\s+is\s+from\s+\d+\s*\(?\s*inclusive\s*\)?\s+to\s+(\d+)\s*\(?\s*exclusive`.
fn range_exclusive(t: &Chars, at: usize) -> Option<(usize, usize)> {
    let mut end = words_ci(t, at, &["range", "is", "from"])?;
    end = digits(t, ws1(t, end)?)?;
    end = ws0(t, end);
    end = one_of(t, end, &['(']).unwrap_or(end);
    end = lit_ci(t, ws0(t, end), "inclusive")?;
    let spaced = ws0(t, end);
    end = match one_of(t, spaced, &[')']) {
        Some(close) => ws1(t, close)?,
        None if spaced > end => spaced,
        None => return None,
    };
    end = lit_ci(t, end, "to")?;
    let start = ws1(t, end)?;
    let limit_end = digits(t, start)?;
    end = ws0(t, limit_end);
    end = one_of(t, end, &['(']).unwrap_or(end);
    lit_ci(t, ws0(t, end), "exclusive")?;
    Some((start, limit_end))
}

/// `(?:less\s+than|below)\s+(\d+)`.
fn less_than(t: &Chars, at: usize) -> Option<(usize, usize)> {
    let end = words_ci(t, at, &["less", "than"]).or_else(|| lit_ci(t, at, "below"))?;
    let start = ws1(t, end)?;
    Some((start, digits(t, start)?))
}

/// `\[\s*\d+\s*,\s*(\d+)\s*\]`.
fn bracket_range(t: &Chars, at: usize) -> Option<(usize, usize)> {
    let mut end = lit(t, at, "[")?;
    end = digits(t, ws0(t, end))?;
    end = lit(t, ws0(t, end), ",")?;
    let start = ws0(t, end);
    let limit_end = digits(t, start)?;
    lit(t, ws0(t, limit_end), "]")?;
    Some((start, limit_end))
}

/// `(?:at\s+most|...|maximum\s+is)\s+(\d+)`.
fn at_most(t: &Chars, at: usize) -> Option<(usize, usize)> {
    const PHRASES: &[&[&str]] = &[
        &["at", "most"],
        &["less", "than", "or", "equal", "to"],
        &["no", "more", "than"],
        &["cannot", "exceed"],
        &["must", "not", "exceed"],
        &["up", "to"],
        &["maximum", "of"],
        &["maximum", "is"],
    ];
    PHRASES.iter().find_map(|phrase| {
        let start = ws1(t, words_ci(t, at, phrase)?)?;
        Some((start, digits(t, start)?))
    })
}

/// `between\s+\d+\s+and\s+(\d+)`.
fn between(t: &Chars, at: usize) -> Option<(usize, usize)> {
    let mut end = lit_ci(t, at, "between")?;
    end = digits(t, ws1(t, end)?)?;
    end = lit_ci(t, ws1(t, end)?, "and")?;
    let start = ws1(t, end)?;
    Some((start, digits(t, start)?))
}

/// `_THINKING_BINDING_MISMATCH_RE.search`.
pub(crate) fn is_thinking_binding_mismatch(text: &str) -> bool {
    let t = chars(text);
    (0..t.len()).any(|start| {
        let matched = (|| {
            let mut end = digits(&t, lit(&t, start, "messages.")?)?;
            end = digits(&t, lit(&t, end, ".content.")?)?;
            end = lit(&t, end, ":")?;
            end = lit(&t, ws0(&t, end), "Invalid `signature` in `thinking` block.")?;
            lit(
                &t,
                ws0(&t, end),
                "The block is bound to a different conversation",
            )
        })();
        matched.is_some()
    })
}

fn param_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || ch == '_'
}

fn param_body(ch: char) -> bool {
    ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | '[' | ']')
}

/// `_PARAMETER_PATH` from `at`: its end.
fn param_path(t: &Chars, at: usize) -> Option<usize> {
    if !t.get(at).is_some_and(|ch| param_start(*ch)) {
        return None;
    }
    let mut end = at + 1;
    while end < t.len() && param_body(t[end]) {
        end += 1;
    }
    Some(end)
}

const QUOTES: [char; 3] = ['\'', '"', '`'];

/// Every name `_UNSUPPORTED_PARAMETER_PHRASES` capture in `message`.
pub(crate) fn unsupported_parameter_names(message: &str) -> BTreeSet<String> {
    let t = chars(message);
    let mut names = BTreeSet::new();
    for phrase in ["Unsupported parameter:", "Unknown parameter:"] {
        let mut at = 0;
        while at < t.len() {
            let matched = lit(&t, at, phrase).and_then(|end| {
                let end = ws0(&t, end);
                let start = one_of(&t, end, &QUOTES).unwrap_or(end);
                Some((start, param_path(&t, start)?))
            });
            match matched {
                Some((start, end)) => {
                    names.insert(collect(&t[start..end]));
                    at = end;
                }
                None => at += 1,
            }
        }
    }
    let mut at = 0;
    while at < t.len() {
        let behind_ok = at == 0 || !param_body(t[at - 1]);
        let matched = param_path(&t, at).filter(|_| behind_ok).and_then(|end| {
            let colon = lit(&t, end, ":")?;
            Some((
                end,
                lit(&t, ws0(&t, colon), "Extra inputs are not permitted")?,
            ))
        });
        match matched {
            Some((name_end, end)) => {
                names.insert(collect(&t[at..name_end]));
                at = end;
            }
            None => at += 1,
        }
    }
    let mut at = 0;
    while at < t.len() {
        let matched = one_of(&t, at, &QUOTES).and_then(|start| {
            let name_end = param_path(&t, start)?;
            let close = one_of(&t, name_end, &QUOTES)?;
            let end = lit(&t, ws1(&t, close)?, "is not supported with this model")?;
            Some((start, name_end, end))
        });
        match matched {
            Some((start, name_end, end)) => {
                names.insert(collect(&t[start..name_end]));
                at = end;
            }
            None => at += 1,
        }
    }
    names
}

/// `_ERROR_CODE_RE.fullmatch`.
pub(crate) fn is_error_code(text: &str) -> bool {
    let len = py::char_len(text);
    (1..=64).contains(&len)
        && text
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '_' | '.' | ':' | '-'))
}

/// `_DISTRIBUTOR_NO_CHANNEL_RE.search`.
pub(crate) fn has_distributor_no_channel(text: &str) -> bool {
    let t = chars(text);
    (0..t.len()).any(|start| {
        let matched = (|| {
            let mut end = lit(&t, start, "无可用渠道")?;
            end = one_of(&t, ws0(&t, end), &['（', '('])?;
            end = lit_ci(&t, ws0(&t, end), "distributor")?;
            one_of(&t, ws0(&t, end), &['）', ')'])
        })();
        matched.is_some()
    })
}

/// `_REQUEST_ID_SUFFIX_RE.sub('', text)` for stripped text.
pub(crate) fn drop_request_id_suffix(text: &str) -> &str {
    let t = chars(text);
    for start in 0..t.len() {
        let matched = (|| {
            let mut end = one_of(&t, ws0(&t, start), &['（', '('])?;
            end = lit_ci(&t, ws0(&t, end), "request id")?;
            end = one_of(&t, ws0(&t, end), &[':', '：'])?;
            while end < t.len() && !matches!(t[end], ')' | '）') {
                end += 1;
            }
            let end = one_of(&t, end, &[')', '）'])?;
            (ws0(&t, end) == t.len()).then_some(())
        })();
        if matched.is_some() {
            return &text[..byte_offset(text, start)];
        }
    }
    text
}

/// `re.split(r'\s+\(request id:', text, maxsplit=1, flags=re.I)[0]`.
pub(crate) fn before_request_id(text: &str) -> &str {
    let t = chars(text);
    for start in 0..t.len() {
        let matched = ws1(&t, start).and_then(|end| lit_ci(&t, lit(&t, end, "(")?, "request id:"));
        if matched.is_some() {
            return &text[..byte_offset(text, start)];
        }
    }
    text
}

/// `["']<name>["']\s*:\s*(\d+(?:\.\d+)?)` → the captured number as ASCII.
pub(crate) fn quoted_number_field(text: &str, name: &str) -> Option<String> {
    let t = chars(text);
    for start in 0..t.len() {
        let matched = (|| {
            let mut end = one_of(&t, start, &['"', '\''])?;
            end = one_of(&t, lit(&t, end, name)?, &['"', '\''])?;
            let number_start = ws0(&t, lit(&t, ws0(&t, end), ":")?);
            let mut number_end = digits(&t, number_start)?;
            if let Some(fraction) = lit(&t, number_end, ".").and_then(|dot| digits(&t, dot)) {
                number_end = fraction;
            }
            Some((number_start, number_end))
        })();
        if let Some((start, end)) = matched {
            return Some(
                t[start..end]
                    .iter()
                    .map(|ch| match py::decimal(*ch) {
                        Some(value) => char::from_digit(value, 10).unwrap_or('0'),
                        None => *ch,
                    })
                    .collect(),
            );
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_limit_shapes() {
        let limit = |text: &str| token_limit(text, 1_000_000);
        assert_eq!(
            limit("Range of max_tokens should be [1, 65536]"),
            Some(65536)
        );
        assert_eq!(limit("max_tokens must be less than 8193"), Some(8192));
        assert_eq!(
            limit("max_tokens: range is from 1 (inclusive) to 32769 (exclusive)"),
            Some(32768)
        );
        assert_eq!(
            limit("max_tokens range is from 1 inclusive to 9 exclusive"),
            Some(8)
        );
        assert_eq!(
            limit("MAX_TOKENS value must be between 1 and 4000"),
            Some(4000)
        );
        assert_eq!(limit("max_tokens\nmust be at most 5"), None);
        assert_eq!(limit("max_tokens is too large"), None);
    }

    #[test]
    fn parameter_phrases() {
        let names = |text: &str| {
            unsupported_parameter_names(text)
                .into_iter()
                .collect::<Vec<_>>()
        };
        assert_eq!(
            names("Unsupported parameter: 'temperature' is not"),
            ["temperature"]
        );
        assert_eq!(names("top_k: Extra inputs are not permitted"), ["top_k"]);
        assert_eq!(
            names("x.top_k: Extra inputs are not permitted"),
            ["x.top_k"]
        );
        assert_eq!(names("`seed` is not supported with this model"), ["seed"]);
    }

    #[test]
    fn request_id_suffixes() {
        assert_eq!(
            drop_request_id_suffix("no channel (request id: abc)"),
            "no channel"
        );
        assert_eq!(before_request_id("words: x  (Request ID: 1)"), "words: x");
    }
}
