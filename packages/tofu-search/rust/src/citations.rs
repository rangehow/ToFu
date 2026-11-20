//! Port of `verify/parse.py` and the DOI/arXiv patterns it shares with
//! `search/vertical/{doi,arxiv}.py`.
//!
//! Each regular expression is replaced by a scanner with the same leftmost,
//! greedy-with-backtracking result, over code points (Python string indices).
//! Quirks of the oracle are preserved on purpose, e.g. a double-quoted BibTeX
//! value always reads as empty (`_read_delimited` closes on its own opener).

use std::collections::{HashMap, HashSet};

use crate::chars;
use crate::py_tables::IGNORECASE_EXTRA;
use crate::pyre::{collapse_runs, is_decimal, starts_with_ci, word_boundary};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Citation {
    pub key: String,
    pub title: String,
    pub authors: Vec<String>,
    pub year: String,
    pub doi: String,
    pub arxiv_id: String,
    pub entry_type: String,
    pub raw: String,
}

fn text_of(chars: &[char]) -> String {
    chars.iter().collect()
}

fn strip_chars(chars: &[char]) -> String {
    text_of(chars::strip(chars))
}

fn colon_or_space(c: char) -> bool {
    c == ':' || chars::is_space(c)
}

/// `[a-z]` (and `[A-Z]`) under `re.IGNORECASE`.
fn letter_class_ci(c: char) -> bool {
    c.is_ascii_alphabetic() || IGNORECASE_EXTRA.iter().any(|&(cp, _)| cp == c as u32)
}

fn decimals(chars: &[char], at: usize) -> usize {
    chars[at.min(chars.len())..]
        .iter()
        .take_while(|&&c| is_decimal(c))
        .count()
}

/// `strip_doi`: trailing sentence punctuation.
pub fn strip_doi(doi: &str) -> String {
    doi.trim_end_matches(['.', ',', ';', ':', ')', '>', ']', '\'', '"'])
        .to_owned()
}

/// `10\.\d{4,}/\S+` at `q`; returns the end.
fn doi_body(chars: &[char], q: usize) -> Option<usize> {
    if chars.get(q) != Some(&'1')
        || chars.get(q + 1) != Some(&'0')
        || chars.get(q + 2) != Some(&'.')
    {
        return None;
    }
    let digits = decimals(chars, q + 3);
    if digits < 4 || chars.get(q + 3 + digits) != Some(&'/') {
        return None;
    }
    let start = q + 4 + digits;
    let tail = chars[start.min(chars.len())..]
        .iter()
        .take_while(|&&c| !chars::is_space(c))
        .count();
    (tail > 0).then_some(start + tail)
}

/// Next `_DOI_RE` match at or after `pos`: (match start, group start, end).
fn find_doi(chars: &[char], pos: usize) -> Option<(usize, usize, usize)> {
    (pos..chars.len()).find_map(|q| {
        let end = doi_body(chars, q)?;
        let mut r = q;
        while r > pos && colon_or_space(chars[r - 1]) {
            r -= 1;
        }
        let start = if r < q && r >= pos + 3 && starts_with_ci(chars, r - 3, "doi") {
            r - 3
        } else {
            q
        };
        Some((start, q, end))
    })
}

/// Optional `(?:v\d+)?` after an arXiv number.
fn version_suffix(chars: &[char], at: usize) -> usize {
    if matches!(chars.get(at), Some('v') | Some('V')) {
        let digits = decimals(chars, at + 1);
        if digits > 0 {
            return at + 1 + digits;
        }
    }
    at
}

fn modern_id(chars: &[char], at: usize) -> Option<usize> {
    if decimals(chars, at) < 4 || chars.get(at + 4) != Some(&'.') {
        return None;
    }
    let digits = decimals(chars, at + 5).min(5);
    (digits >= 4).then(|| version_suffix(chars, at + 5 + digits))
}

fn legacy_id(chars: &[char], at: usize) -> Option<usize> {
    let run = chars[at.min(chars.len())..]
        .iter()
        .take_while(|&&c| c == '-' || letter_class_ci(c))
        .count();
    if run == 0 {
        return None;
    }
    let mut e = at + run;
    if chars.get(e) == Some(&'.')
        && chars.get(e + 1).is_some_and(|&c| letter_class_ci(c))
        && chars.get(e + 2).is_some_and(|&c| letter_class_ci(c))
        && chars.get(e + 3) == Some(&'/')
    {
        e += 3;
    }
    if chars.get(e) != Some(&'/') || decimals(chars, e + 1) < 7 {
        return None;
    }
    Some(version_suffix(chars, e + 8))
}

/// `(?:^|arxiv[:\s]+)(ID)` at `p`: (group start, end).
fn arxiv_at(
    chars: &[char],
    p: usize,
    id: fn(&[char], usize) -> Option<usize>,
) -> Option<(usize, usize)> {
    if p == 0 {
        if let Some(end) = id(chars, 0) {
            return Some((0, end));
        }
    }
    if starts_with_ci(chars, p, "arxiv") {
        let run = chars[p + 5..]
            .iter()
            .take_while(|&&c| colon_or_space(c))
            .count();
        if run > 0 {
            if let Some(end) = id(chars, p + 5 + run) {
                return Some((p + 5 + run, end));
            }
        }
    }
    None
}

fn find_arxiv(
    chars: &[char],
    pos: usize,
    id: fn(&[char], usize) -> Option<usize>,
) -> Option<(usize, usize, usize)> {
    (pos..chars.len()).find_map(|p| arxiv_at(chars, p, id).map(|(g, e)| (p, g, e)))
}

/// `extract_identifiers(raw)` → `(doi, arxiv_id)`.
pub fn extract_identifiers(raw: &str) -> (String, String) {
    let chars: Vec<char> = raw.chars().collect();
    let doi = find_doi(&chars, 0)
        .map(|(_, g, e)| strip_doi(&text_of(&chars[g..e])))
        .unwrap_or_default();
    let arxiv = find_arxiv(&chars, 0, modern_id)
        .or_else(|| find_arxiv(&chars, 0, legacy_id))
        .map(|(_, g, e)| text_of(&chars[g..e]))
        .unwrap_or_default();
    (doi, arxiv)
}

// ── BibTeX ──────────────────────────────────────────────────────────────────

/// Next `@(\w+)\s*\{` match: (start, word range, end after the brace).
fn find_entry_start(chars: &[char], pos: usize) -> Option<(usize, (usize, usize), usize)> {
    (pos..chars.len()).find_map(|p| {
        if chars[p] != '@' {
            return None;
        }
        let word = chars[p + 1..]
            .iter()
            .take_while(|&&c| chars::is_word(c))
            .count();
        if word == 0 {
            return None;
        }
        let after = p + 1 + word;
        let spaces = chars[after..]
            .iter()
            .take_while(|&&c| chars::is_space(c))
            .count();
        (chars.get(after + spaces) == Some(&'{')).then_some((p, (p + 1, after), after + spaces + 1))
    })
}

fn extract_braced_body(chars: &[char], open: usize) -> Option<&[char]> {
    if chars.get(open) != Some(&'{') {
        return None;
    }
    let mut depth = 1;
    let mut i = open + 1;
    while i < chars.len() {
        match chars[i] {
            '\\' if i + 1 < chars.len() => {
                i += 2;
                continue;
            }
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&chars[open + 1..i]);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

fn read_delimited(s: &[char], start: usize, open: char, close: char) -> (String, usize) {
    let mut depth = 0;
    let mut i = start;
    let mut buf = String::new();
    while i < s.len() {
        let ch = s[i];
        if ch == '\\' && i + 1 < s.len() {
            buf.push(s[i]);
            buf.push(s[i + 1]);
            i += 2;
            continue;
        }
        if ch == open {
            depth += 1;
            if depth == 1 && open != close {
                i += 1;
                continue;
            }
        }
        if ch == close {
            depth -= 1;
            if depth == 0 {
                return (buf, i + 1);
            }
        }
        buf.push(ch);
        i += 1;
    }
    (buf, i)
}

fn clean_value(value: &str) -> String {
    let value = value.replace(['{', '}'], "").replace("\\&", "&");
    let collapsed = collapse_runs(&value);
    let stripped = collapsed.trim_matches(chars::is_space);
    stripped
        .trim_matches(',')
        .trim_matches(chars::is_space)
        .to_owned()
}

fn parse_fields(body: &[char]) -> HashMap<String, String> {
    let mut fields = HashMap::new();
    let n = body.len();
    let ws = |c: char| matches!(c, ' ' | '\t' | '\r' | '\n');
    let mut i = 0;
    while i < n {
        while i < n && (ws(body[i]) || body[i] == ',') {
            i += 1;
        }
        let mut j = i;
        while j < n && (chars::is_alnum(body[j]) || body[j] == '_' || body[j] == '-') {
            j += 1;
        }
        let name = chars::lower(&strip_chars(&body[i..j]));
        let mut k = j;
        while k < n && ws(body[k]) {
            k += 1;
        }
        if k >= n || body[k] != '=' {
            break;
        }
        k += 1;
        while k < n && ws(body[k]) {
            k += 1;
        }
        if k >= n {
            break;
        }
        let (value, after) = match body[k] {
            '{' => read_delimited(body, k, '{', '}'),
            '"' => read_delimited(body, k, '"', '"'),
            _ => {
                let mut e = k;
                while e < n && body[e] != ',' {
                    e += 1;
                }
                (text_of(&body[k..e]), e)
            }
        };
        if !name.is_empty() {
            fields.insert(name, clean_value(&value));
        }
        i = after;
    }
    fields
}

/// `\b(19|20)\d{2}\b` search.
fn find_year(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    (0..chars.len())
        .find(|&p| {
            p + 4 <= chars.len()
                && word_boundary(&chars, p)
                && matches!((chars[p], chars[p + 1]), ('1', '9') | ('2', '0'))
                && is_decimal(chars[p + 2])
                && is_decimal(chars[p + 3])
                && word_boundary(&chars, p + 4)
        })
        .map(|p| text_of(&chars[p..p + 4]))
        .unwrap_or_default()
}

fn field<'a>(fields: &'a HashMap<String, String>, name: &str) -> &'a str {
    fields.get(name).map_or("", String::as_str)
}

fn arxiv_from_fields(fields: &HashMap<String, String>) -> String {
    let eprint = field(fields, "eprint");
    if chars::lower(field(fields, "archiveprefix")) == "arxiv" && !eprint.is_empty() {
        return eprint.trim_matches(chars::is_space).to_owned();
    }
    let ep = eprint.trim_matches(chars::is_space);
    let ep_chars: Vec<char> = ep.chars().collect();
    if !ep.is_empty()
        && decimals(&ep_chars, 0) >= 4
        && ep_chars.get(4) == Some(&'.')
        && decimals(&ep_chars, 5) >= 4
    {
        return ep.to_owned();
    }
    let haystack = ["url", "note", "journal", "howpublished"]
        .iter()
        .map(|k| field(fields, k))
        .collect::<Vec<_>>()
        .join(" ");
    extract_identifiers(&haystack).1
}

fn doi_from_fields(fields: &HashMap<String, String>) -> String {
    let doi = field(fields, "doi");
    if !doi.is_empty() {
        return strip_doi(doi.trim_matches(chars::is_space));
    }
    let haystack = format!("{} {}", field(fields, "url"), field(fields, "note"));
    extract_identifiers(&haystack).0
}

/// `re.split(r'\s+and\s+', text)`.
fn split_authors(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut pieces = Vec::new();
    let mut last = 0;
    let mut p = 0;
    while p < chars.len() {
        if chars::is_space(chars[p]) {
            let run = chars[p..]
                .iter()
                .take_while(|&&c| chars::is_space(c))
                .count();
            let a = p + run;
            if chars.get(a..a + 3) == Some(&['a', 'n', 'd'][..]) {
                let tail = chars[a + 3..]
                    .iter()
                    .take_while(|&&c| chars::is_space(c))
                    .count();
                if tail > 0 {
                    pieces.push(text_of(&chars[last..p]));
                    last = a + 3 + tail;
                    p = last;
                    continue;
                }
            }
        }
        p += 1;
    }
    pieces.push(text_of(&chars[last..]));
    pieces
}

fn citation_from_fields(
    etype: String,
    key: String,
    fields: &HashMap<String, String>,
    raw: String,
) -> Citation {
    let author = field(fields, "author");
    let authors_raw = if author.is_empty() {
        field(fields, "editor")
    } else {
        author
    };
    let authors = if authors_raw.is_empty() {
        Vec::new()
    } else {
        split_authors(authors_raw)
            .iter()
            .map(|a| a.trim_matches(chars::is_space).to_owned())
            .filter(|a| !a.is_empty())
            .collect()
    };
    let mut year = field(fields, "year").to_owned();
    if year.is_empty() {
        year = find_year(field(fields, "date"));
    }
    Citation {
        key,
        title: field(fields, "title").to_owned(),
        authors,
        year,
        doi: doi_from_fields(fields),
        arxiv_id: arxiv_from_fields(fields),
        entry_type: etype,
        raw,
    }
}

/// `parse_bibtex(text)`.
pub fn parse_bibtex(text: &str) -> Vec<Citation> {
    let chars: Vec<char> = text.chars().collect();
    let mut out = Vec::new();
    let mut pos = 0;
    while let Some((start, (w0, w1), end)) = find_entry_start(&chars, pos) {
        pos = end;
        let etype = chars::lower(&text_of(&chars[w0..w1]));
        if matches!(etype.as_str(), "comment" | "preamble" | "string") {
            continue;
        }
        let Some(body) = extract_braced_body(&chars, end - 1) else {
            continue;
        };
        let comma = body.iter().position(|&c| c == ',');
        let (key, rest) = match comma {
            Some(c) => (&body[..c], &body[c + 1..]),
            None => (body, &body[body.len()..]),
        };
        let raw = format!("{}{{{}}}", text_of(&chars[start..end - 1]), text_of(body));
        out.push(citation_from_fields(
            etype,
            strip_chars(key),
            &parse_fields(rest),
            raw,
        ));
    }
    out
}

// ── Freeform references ──────────────────────────────────────────────────────

fn line_start(chars: &[char], p: usize) -> bool {
    p == 0 || chars[p - 1] == '\n'
}

fn space_run(chars: &[char], p: usize) -> usize {
    chars[p.min(chars.len())..]
        .iter()
        .take_while(|&&c| chars::is_space(c))
        .count()
}

/// `(?m)^\s*\[\d+\]\s+|\s*^\s*\d+\.\s+` at `p`; returns the end.
fn numbered_at(chars: &[char], p: usize) -> Option<usize> {
    let r = p + space_run(chars, p);
    if line_start(chars, p) && chars.get(r) == Some(&'[') {
        let digits = decimals(chars, r + 1);
        if digits > 0 && chars.get(r + 1 + digits) == Some(&']') {
            let after = r + 2 + digits;
            let tail = space_run(chars, after);
            if tail > 0 {
                return Some(after + tail);
            }
        }
    }
    let reaches_line_start = line_start(chars, p) || chars[p..r].contains(&'\n');
    let digits = decimals(chars, r);
    if reaches_line_start && digits > 0 && chars.get(r + digits) == Some(&'.') {
        let after = r + digits + 1;
        let tail = space_run(chars, after);
        if tail > 0 {
            return Some(after + tail);
        }
    }
    None
}

/// `\n\s*\n` at `p`; returns the end.
fn blank_line_at(chars: &[char], p: usize) -> Option<usize> {
    if chars[p] != '\n' {
        return None;
    }
    let r = p + 1 + space_run(chars, p + 1);
    (p + 1..r).rev().find(|&q| chars[q] == '\n').map(|q| q + 1)
}

fn regex_split(chars: &[char], matcher: impl Fn(&[char], usize) -> Option<usize>) -> Vec<String> {
    let mut pieces = Vec::new();
    let mut last = 0;
    let mut p = 0;
    while p < chars.len() {
        if let Some(end) = matcher(chars, p) {
            pieces.push(text_of(&chars[last..p]));
            last = end;
            p = end;
        } else {
            p += 1;
        }
    }
    pieces.push(text_of(&chars[last..]));
    pieces
        .iter()
        .map(|s| s.trim_matches(chars::is_space).to_owned())
        .filter(|s| !s.is_empty())
        .collect()
}

fn split_reference_blob(chars: &[char]) -> Vec<String> {
    if (0..chars.len()).any(|p| numbered_at(chars, p).is_some()) {
        regex_split(chars, numbered_at)
    } else {
        regex_split(chars, blank_line_at)
    }
}

/// `[“"]([^”"]{6,})[”"]` search, group stripped.
fn guess_title(entry: &str) -> String {
    let chars: Vec<char> = entry.chars().collect();
    for p in 0..chars.len() {
        if chars[p] != '“' && chars[p] != '"' {
            continue;
        }
        if let Some(q) = chars[p + 1..].iter().position(|&c| c == '”' || c == '"') {
            if q >= 6 {
                return strip_chars(&chars[p + 1..p + 1 + q]);
            }
        }
    }
    String::new()
}

/// `parse_references(text)`.
pub fn parse_references(text: &str) -> Vec<Citation> {
    let chars: Vec<char> = text.chars().collect();
    split_reference_blob(&chars)
        .into_iter()
        .map(|entry| {
            let (doi, arxiv_id) = extract_identifiers(&entry);
            Citation {
                title: guess_title(&entry),
                year: find_year(&entry),
                doi,
                arxiv_id,
                raw: entry,
                ..Citation::default()
            }
        })
        .collect()
}

/// `extract_citations_from_text(text)`.
pub fn extract_citations_from_text(text: &str) -> Vec<Citation> {
    let chars: Vec<char> = text.chars().collect();
    let window = |start: usize, end: usize| {
        let a = start.saturating_sub(40);
        let b = (end + 40).min(chars.len());
        collapse_runs(&text_of(&chars[a..b]))
            .trim_matches(chars::is_space)
            .to_owned()
    };
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    let mut pos = 0;
    while let Some((start, g, end)) = find_doi(&chars, pos) {
        pos = end;
        let ident = strip_doi(&text_of(&chars[g..end]));
        if seen.insert(format!("doi:{}", chars::lower(&ident))) {
            out.push(Citation {
                doi: ident,
                raw: window(start, end),
                ..Citation::default()
            });
        }
    }
    for id in [modern_id as fn(&[char], usize) -> Option<usize>, legacy_id] {
        let mut pos = 0;
        while let Some((start, g, end)) = find_arxiv(&chars, pos, id) {
            pos = end;
            let ident = text_of(&chars[g..end]);
            if seen.insert(format!("arxiv:{}", chars::lower(&ident))) {
                out.push(Citation {
                    arxiv_id: ident,
                    raw: window(start, end),
                    ..Citation::default()
                });
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers() {
        assert_eq!(
            extract_identifiers("see doi: 10.1145/3292500.3330701). and arXiv:1706.03762v5"),
            ("10.1145/3292500.3330701".into(), "1706.03762v5".into())
        );
        assert_eq!(
            extract_identifiers("arxiv hep-th/9901001").1,
            "hep-th/9901001"
        );
        assert_eq!(extract_identifiers("x hep-th/9901001").1, "");
    }

    #[test]
    fn bibtex() {
        let cits = parse_bibtex(
            "@Article{vaswani,\n title = {Attention {Is} All},\n author = {A Vaswani and N Shazeer},\n year = 2017,\n eprint={1706.03762}}\n@comment{x}",
        );
        assert_eq!(cits.len(), 1);
        let c = &cits[0];
        assert_eq!(c.entry_type, "article");
        assert_eq!(c.title, "Attention Is All");
        assert_eq!(c.authors, vec!["A Vaswani", "N Shazeer"]);
        assert_eq!(c.year, "2017");
        assert_eq!(c.arxiv_id, "1706.03762");
        // Oracle quirk: a double-quoted value reads as empty.
        assert_eq!(parse_bibtex("@misc{k, title = \"Quoted\"}")[0].title, "");
    }

    #[test]
    fn references() {
        let refs =
            parse_references("[1] A. “A Long Title Here,” 2019. doi:10.1000/xyz\n[2] B, 2020");
        assert_eq!(refs.len(), 2);
        assert_eq!(refs[0].title, "A Long Title Here,");
        assert_eq!(refs[0].doi, "10.1000/xyz");
        assert_eq!(refs[1].year, "2020");
        let found = extract_citations_from_text(
            "cite 10.1/x no; 10.1234/A. and 10.1234/a arxiv:2301.00001",
        );
        assert_eq!(found.len(), 2);
    }
}
