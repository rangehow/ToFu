//! Port of the shingle and grouping kernels of `tofu_search/search/dedup.py`.
//!
//! The Python module keeps request-owned row handling (evidence merges,
//! logging, listing attachment); these functions only decide group membership.

use std::collections::HashSet;

use rustc_hash::FxHashMap;

use crate::chars::{is_space, is_word, lower};

/// The dedup module's CJK class (same code points as the lexical class).
fn is_cjk(c: char) -> bool {
    crate::lexical::is_cjk(c)
}

/// `_text_to_shingles`: word tokens plus CJK character bigrams.
pub fn text_shingles(text: &str) -> HashSet<String> {
    let lowered: Vec<char> = lower(text).chars().collect();
    let mut tokens = HashSet::new();
    let cjk: Vec<char> = lowered.iter().copied().filter(|&c| is_cjk(c)).collect();
    if !cjk.is_empty() {
        for pair in cjk.windows(2) {
            tokens.insert(pair.iter().collect());
        }
        if cjk.len() < 6 {
            tokens.extend(cjk.iter().map(|c| c.to_string()));
        }
    }
    // CJK -> space, then [^\w\s] -> space, then str.split().
    let mut word = String::new();
    let mut word_len = 0usize;
    let mut flush = |word: &mut String, word_len: &mut usize| {
        if *word_len > 1 {
            tokens.insert(std::mem::take(word));
        } else {
            word.clear();
        }
        *word_len = 0;
    };
    for &c in &lowered {
        let separator = is_cjk(c) || is_space(c) || !is_word(c);
        if separator {
            flush(&mut word, &mut word_len);
        } else {
            word.push(c);
            word_len += 1;
        }
    }
    flush(&mut word, &mut word_len);
    tokens
}

/// The word runs and CJK characters `_full_text_shingles` draws trigrams from.
fn full_text_parts(text: &str) -> (Vec<String>, Vec<char>) {
    if text.is_empty() {
        return (Vec::new(), Vec::new());
    }
    let body = match text.find("--- Page Links ---") {
        Some(marker) => &text[..marker],
        None => text,
    };
    let char_count = body.chars().count();
    let bounded: std::borrow::Cow<str> = if char_count > 28_000 {
        let head_end = body
            .char_indices()
            .nth(24_000)
            .map_or(body.len(), |(i, _)| i);
        let tail_start = body
            .char_indices()
            .nth(char_count - 4_000)
            .map_or(body.len(), |(i, _)| i);
        format!("{}\n{}", &body[..head_end], &body[tail_start..]).into()
    } else {
        body.into()
    };
    let lowered = lower(&bounded);
    let mut latin: Vec<String> = Vec::new();
    let mut cjk: Vec<char> = Vec::new();
    let mut run = String::new();
    for c in lowered.chars() {
        if matches!(c, 'a'..='z' | '0'..='9' | '_') {
            run.push(c);
            continue;
        }
        if run.len() >= 2 {
            latin.push(std::mem::take(&mut run));
        } else {
            run.clear();
        }
        if is_cjk(c) {
            cjk.push(c);
        }
    }
    if run.len() >= 2 {
        latin.push(run);
    }
    (latin, cjk)
}

/// `_full_text_shingles`: word and CJK trigrams for near-copy detection.
pub fn full_text_shingles(text: &str) -> HashSet<String> {
    let (latin, cjk) = full_text_parts(text);
    let mut shingles = HashSet::new();
    for window in latin.windows(3) {
        shingles.insert(format!("w:{}", window.join("\x1f")));
    }
    for window in cjk.windows(3) {
        let mut s = String::from("c:");
        s.extend(window);
        shingles.insert(s);
    }
    shingles
}

/// Exact shingle identities as sorted integers. Word runs contain no `\x1f`,
/// so a word trigram is identified by its three interned words; a CJK
/// trigram by its three code points; bit 127 separates the two namespaces.
fn full_text_fingerprint(text: &str, words: &mut FxHashMap<String, u32>) -> Vec<u128> {
    let (latin, cjk) = full_text_parts(text);
    let ids: Vec<u128> = latin
        .into_iter()
        .map(|word| {
            let next = words.len() as u32;
            u128::from(*words.entry(word).or_insert(next))
        })
        .collect();
    let mut fingerprint: Vec<u128> = ids
        .windows(3)
        .map(|w| (w[0] << 64) | (w[1] << 32) | w[2])
        .chain(cjk.windows(3).map(|w| {
            (1u128 << 127) | (u128::from(w[0]) << 64) | (u128::from(w[1]) << 32) | u128::from(w[2])
        }))
        .collect();
    fingerprint.sort_unstable();
    fingerprint.dedup();
    fingerprint
}

fn sorted_jaccard(a: &[u128], b: &[u128]) -> f64 {
    let (mut i, mut j, mut common) = (0, 0, 0usize);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                common += 1;
                i += 1;
                j += 1;
            }
        }
    }
    common as f64 / (a.len() + b.len() - common) as f64
}

fn jaccard(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let (small, large) = if a.len() <= b.len() { (a, b) } else { (b, a) };
    let common = small.iter().filter(|s| large.contains(*s)).count();
    common as f64 / (a.len() + b.len() - common) as f64
}

/// `group_by_content` membership: each input index joins the first group
/// whose representative reaches `threshold`; returns indices per group.
pub fn group_indices(texts: &[String], threshold: f64) -> Vec<Vec<usize>> {
    let shingles: Vec<HashSet<String>> = texts.iter().map(|t| text_shingles(t)).collect();
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for (index, current) in shingles.iter().enumerate() {
        match groups
            .iter_mut()
            .find(|group| jaccard(current, &shingles[group[0]]) >= threshold)
        {
            Some(group) => group.push(index),
            None => groups.push(vec![index]),
        }
    }
    groups
}

/// `dedup_by_full_content` decisions: `None` keeps the row, `Some(k)` names
/// the kept position it duplicates. Empty fingerprints are never duplicates.
pub fn full_content_duplicates(texts: &[String], threshold: f64) -> Vec<Option<usize>> {
    let mut words = FxHashMap::default();
    let fingerprints: Vec<Vec<u128>> = texts
        .iter()
        .map(|t| full_text_fingerprint(t, &mut words))
        .collect();
    let mut kept: Vec<usize> = Vec::new();
    let mut decisions = Vec::with_capacity(texts.len());
    for (index, fingerprint) in fingerprints.iter().enumerate() {
        let duplicate_of = if fingerprint.is_empty() {
            None
        } else {
            kept.iter().position(|&k| {
                !fingerprints[k].is_empty()
                    && sorted_jaccard(fingerprint, &fingerprints[k]) >= threshold
            })
        };
        if duplicate_of.is_none() {
            kept.push(index);
        }
        decisions.push(duplicate_of);
    }
    decisions
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shingles_mix_words_and_cjk() {
        let s = text_shingles("Rust 搜索引擎, a test!");
        for expected in ["rust", "test", "搜索", "索引", "引擎", "搜"] {
            assert!(s.contains(expected), "{expected}");
        }
        assert!(!s.contains("a"));
    }

    #[test]
    fn groups_near_duplicates() {
        let texts = vec![
            "rust async runtime guide".to_owned(),
            "python packaging".to_owned(),
            "rust async runtime guide!".to_owned(),
        ];
        assert_eq!(group_indices(&texts, 0.45), vec![vec![0, 2], vec![1]]);
    }

    #[test]
    fn full_content_mirrors() {
        let body = "alpha beta gamma delta epsilon zeta eta theta".to_owned();
        let texts = vec![
            body.clone(),
            String::new(),
            body,
            "other words here entirely".to_owned(),
        ];
        assert_eq!(
            full_content_duplicates(&texts, 0.78),
            vec![None, None, Some(0), None]
        );
    }
}
