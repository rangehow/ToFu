//! Source-level citation/reference audit of one frozen build snapshot
//! (`lib/paper/builds/citation_audit.py` with the entry scan of
//! `lib/paper/bib.py`).
//!
//! Cross-references `\cite`/`\nocite` keys with .bib entries and `\ref` keys
//! with `\label` definitions. Advisory: a scan failure never changes the
//! build verdict, and keys the engine log already reported are not repeated.

use super::sources::{read_source, without_comments};
use super::{Fault, Outcome};
use regex::{Regex, RegexBuilder};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::OnceLock;

/// `citation_audit._AUDIT_BUDGET_BYTES` and `_MAX_ROWS`.
const AUDIT_BUDGET_BYTES: u64 = 8 * 1024 * 1024;
const MAX_ROWS: usize = 24;
/// `bib.MAX_ENTRIES`.
const MAX_ENTRIES: usize = 4096;

fn bounded(pattern: &str) -> Regex {
    // The bounded repetitions of the Python patterns compile to large automata.
    RegexBuilder::new(pattern)
        .size_limit(1 << 28)
        .dfa_size_limit(1 << 28)
        .build()
        .expect("regex")
}

fn cite_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        bounded(concat!(
            r"\\(?:cite|citep|citet|citealt|citealp|citeauthor|citeyear|citeyearpar",
            r"|parencite|textcite|autocite|footcite|smartcite|supercite|nocite)",
            r"\s*(?:\[[^\]]{0,200}\])*\s*\{([^}]{1,2000})\}"
        ))
    })
}

fn ref_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        bounded(concat!(
            r"\\(?:ref|eqref|autoref|cref|Cref|vref|vpageref|pageref|nameref)",
            r"\s*(?:\[[^\]]{0,200}\])?\s*\{([^}]{1,512})\}"
        ))
    })
}

fn label_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| bounded(r"\\label\s*\{([^}]{1,512})\}"))
}

fn regex(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("regex"))
}

fn log_citation() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"Citation `([^']+)'")
}

fn log_reference() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"Reference `([^']+)'")
}

fn macro_parameter() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"^#+[1-9]$")
}

fn entry_head() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"@([A-Za-z]+)\s*[{(]\s*")
}

fn bib_key() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r#"^[^\s,{}@#%="()]+$"#)
}

fn keys(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|key| !key.is_empty() && key.chars().count() <= 256)
        .map(str::to_string)
        .collect()
}

fn line_of(text: &str, offset: usize) -> i64 {
    text[..offset].matches('\n').count() as i64 + 1
}

fn row(code: &str, message: &str, file: &str, line: Option<i64>) -> Value {
    json!({
        "severity": "warning",
        "code": code,
        "message": message.chars().take(2000).collect::<String>(),
        "file": file.chars().take(512).collect::<String>(),
        "line": line,
    })
}

/// `bib.parse_entries`: entries `(key, line)` and duplicate keys.
fn parse_entries(text: &str) -> (Vec<(String, i64)>, Vec<String>) {
    let mut entries = Vec::new();
    let mut duplicates: Vec<String> = Vec::new();
    let mut seen = HashSet::new();
    for capture in entry_head().captures_iter(text) {
        let kind = capture[1].to_lowercase();
        if matches!(kind.as_str(), "comment" | "preamble" | "string") {
            continue;
        }
        let end = capture.get(0).map_or(0, |m| m.end());
        let window_end = text[end..]
            .char_indices()
            .nth(300)
            .map_or(text.len(), |(i, _)| end + i);
        let Some(comma) = text[end..window_end].find(',') else {
            continue;
        };
        let key = text[end..end + comma].trim();
        if key.is_empty() || key.chars().count() > 256 || !bib_key().is_match(key) {
            continue;
        }
        let line = line_of(text, capture.get(0).map_or(0, |m| m.start()));
        if seen.contains(key) {
            if !duplicates.iter().any(|d| d == key) {
                duplicates.push(key.to_string());
            }
        } else {
            seen.insert(key.to_string());
            entries.push((key.to_string(), line));
            if entries.len() >= MAX_ENTRIES {
                break;
            }
        }
    }
    (entries, duplicates)
}

fn is_tex(path: &str) -> bool {
    let lower = path.to_lowercase();
    lower.ends_with(".tex") || lower.ends_with(".ltx")
}

pub(crate) fn audit_citations(
    snapshot_root: &Path,
    files: &[String],
    existing: &[Value],
) -> Outcome<Vec<Value>> {
    let tex_paths: Vec<&String> = files.iter().filter(|p| is_tex(p)).collect();
    let bib_paths: Vec<&String> = files
        .iter()
        .filter(|p| p.to_lowercase().ends_with(".bib"))
        .collect();
    if tex_paths.is_empty() && bib_paths.is_empty() {
        return Ok(Vec::new());
    }
    let mut reported = HashSet::new();
    for diagnostic in existing {
        let message = diagnostic["message"].as_str().unwrap_or("");
        for pattern in [log_citation(), log_reference()] {
            if let Some(found) = pattern.captures(message) {
                reported.insert(found[1].to_string());
            }
        }
    }
    let mut budget = 0u64;
    let mut read = |path: &str| -> Outcome<String> {
        if budget > AUDIT_BUDGET_BYTES {
            return Err(Fault::build(
                "audit_limit",
                "Citation audit byte budget reached",
            ));
        }
        let data = read_source(
            snapshot_root,
            &snapshot_root.join(path),
            Some(AUDIT_BUDGET_BYTES - budget + 1),
            None,
        )?;
        budget += data.len() as u64;
        Ok(String::from_utf8_lossy(&data).into_owned())
    };
    let mut cites: BTreeMap<String, (String, i64)> = BTreeMap::new();
    let mut refs: BTreeMap<String, (String, i64)> = BTreeMap::new();
    let mut labels = HashSet::new();
    let mut bib_keys: HashMap<String, (String, i64)> = HashMap::new();
    let mut duplicates: Vec<(String, String, i64)> = Vec::new();
    for path in &tex_paths {
        let text = without_comments(&read(path)?);
        for capture in cite_pattern().captures_iter(&text) {
            let start = capture.get(0).map_or(0, |m| m.start());
            for key in keys(&capture[1]) {
                cites
                    .entry(key)
                    .or_insert_with(|| (path.to_string(), line_of(&text, start)));
            }
        }
        for capture in ref_pattern().captures_iter(&text) {
            let start = capture.get(0).map_or(0, |m| m.start());
            for key in keys(&capture[1]) {
                if macro_parameter().is_match(&key) {
                    continue; // \ref{#1} inside a \newcommand body is not a key
                }
                refs.entry(key)
                    .or_insert_with(|| (path.to_string(), line_of(&text, start)));
            }
        }
        for capture in label_pattern().captures_iter(&text) {
            labels.insert(capture[1].to_string());
        }
    }
    for path in &bib_paths {
        let (entries, repeated) = parse_entries(&read(path)?);
        for (key, line) in entries {
            bib_keys
                .entry(key)
                .or_insert_with(|| (path.to_string(), line));
        }
        for key in repeated {
            let (file, line) = bib_keys
                .get(&key)
                .cloned()
                .unwrap_or_else(|| (path.to_string(), 0));
            duplicates.push((key, file, line));
        }
    }
    if cites.is_empty() && refs.is_empty() && bib_paths.is_empty() {
        return Ok(Vec::new());
    }
    let mut rows = Vec::new();
    if !bib_paths.is_empty() {
        for (key, (path, line)) in &cites {
            if !bib_keys.contains_key(key) && key != "*" && !reported.contains(key) {
                rows.push(row(
                    "undefined_citation",
                    &format!("Citation '{key}' has no entry in any .bib file"),
                    path,
                    Some(*line),
                ));
            }
        }
        for (key, path, line) in duplicates.iter().take(8) {
            rows.push(row(
                "duplicate_bib_key",
                &format!("BibTeX key '{key}' is defined more than once"),
                path,
                (*line != 0).then_some(*line),
            ));
        }
        let unused = if cites.contains_key("*") {
            0
        } else {
            bib_keys
                .keys()
                .filter(|key| !cites.contains_key(*key))
                .count()
        };
        if unused > 0 {
            rows.push(row(
                "unused_bib_entries",
                &format!(
                    "{unused} of {} .bib entries are never cited",
                    bib_keys.len()
                ),
                "",
                None,
            ));
        }
    } else if !cites.is_empty() {
        rows.push(row(
            "missing_bibliography",
            &format!(
                "Sources cite {} key(s) but no .bib file is part of the project",
                cites.len()
            ),
            "",
            None,
        ));
    }
    for (key, (path, line)) in &refs {
        if !labels.contains(key) && !reported.contains(key) {
            rows.push(row(
                "undefined_reference",
                &format!("Reference '{key}' has no matching \\label"),
                path,
                Some(*line),
            ));
        }
    }
    rows.truncate(MAX_ROWS);
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bib_entries_and_duplicates() {
        let (entries, duplicates) =
            parse_entries("@article{a,\n title={x}}\n@misc{b,}\n@string{s = 1}\n@book{a,}\n");
        assert_eq!(entries, vec![("a".to_string(), 1), ("b".to_string(), 3)]);
        assert_eq!(duplicates, vec!["a".to_string()]);
    }
}
