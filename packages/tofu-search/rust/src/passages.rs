//! Port of `tofu_search/passages.py::select_relevant_passages`.
//!
//! Offsets are code-point indices into the link-free body, exactly as the
//! Python oracle slices `str`. Token sets are interned to ids so similarity
//! and coverage are sorted-vector merges instead of string hashing.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

use rustc_hash::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::chars::{collapse_whitespace, is_space, rstrip, strip};
use crate::lexical::{query_subject, tokenize};

const LINKS_HEADER: &str = "--- Page Links ---";
const GAP: &str = "\n\n[... omitted ...]\n\n";

fn gap_len() -> usize {
    GAP.chars().count()
}

type Span = (usize, usize);

fn merge_spans(spans: &[Span], text: &[char]) -> Vec<Span> {
    let mut sorted = spans.to_vec();
    sorted.sort_unstable();
    let mut merged: Vec<Span> = Vec::with_capacity(sorted.len());
    for (start, end) in sorted {
        if let Some(last) = merged.last_mut() {
            let joins =
                start <= last.1 || (start - last.1 <= 4 && strip(&text[last.1..start]).is_empty());
            if joins {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }
    merged
}

fn rendered_len(spans: &[Span], text: &[char]) -> usize {
    let merged = merge_spans(spans, text);
    let body: usize = merged.iter().map(|(s, e)| e - s).sum();
    body + gap_len() * merged.len().saturating_sub(1)
}

fn render(spans: &[Span], text: &[char]) -> Vec<char> {
    let gap: Vec<char> = GAP.chars().collect();
    let mut out = Vec::new();
    for (index, (start, end)) in merge_spans(spans, text).into_iter().enumerate() {
        if index > 0 {
            out.extend_from_slice(&gap);
        }
        out.extend_from_slice(&text[start..end]);
    }
    out
}

/// `re.finditer(r'\n\s*\n+', text)` spans.
fn paragraph_gaps(text: &[char]) -> Vec<Span> {
    let mut gaps = Vec::new();
    let mut index = 0;
    while index < text.len() {
        if text[index] != '\n' {
            index += 1;
            continue;
        }
        let run_end = index
            + 1
            + text[index + 1..]
                .iter()
                .take_while(|&&c| is_space(c))
                .count();
        match text[index + 1..run_end].iter().rposition(|&c| c == '\n') {
            Some(offset) => {
                let end = index + 1 + offset + 1;
                gaps.push((index, end));
                index = end;
            }
            None => index += 1,
        }
    }
    gaps
}

fn chunk_spans(text: &[char], chunk_chars: usize) -> Vec<Span> {
    let mut paragraphs = Vec::new();
    let mut start = 0;
    for (gap_start, gap_end) in paragraph_gaps(text) {
        paragraphs.push((start, gap_start));
        start = gap_end;
    }
    paragraphs.push((start, text.len()));
    let mut chunks = Vec::new();
    let mut current: Option<Span> = None;
    for (mut start, mut end) in paragraphs {
        while start < end && is_space(text[start]) {
            start += 1;
        }
        while end > start && is_space(text[end - 1]) {
            end -= 1;
        }
        if start == end {
            continue;
        }
        if end - start > chunk_chars {
            if let Some(span) = current.take() {
                chunks.push(span);
            }
            let step = chunk_chars - 120.min(chunk_chars / 8);
            let mut offset = start;
            while offset < end {
                chunks.push((offset, end.min(offset + chunk_chars)));
                if offset + chunk_chars >= end {
                    break;
                }
                offset += step;
            }
        } else if current.is_some_and(|span| end - span.0 <= chunk_chars) {
            current = current.map(|span| (span.0, end));
        } else {
            if let Some(span) = current.take() {
                chunks.push(span);
            }
            current = Some((start, end));
        }
    }
    if let Some(span) = current {
        chunks.push(span);
    }
    chunks
}

fn fallback_excerpt(text: &[char], max_chars: usize, chunk_chars: usize, edges: bool) -> Vec<char> {
    if !edges || max_chars < gap_len() + 2 {
        return rstrip(&text[..max_chars.min(text.len())]).to_vec();
    }
    let edge = chunk_chars.max(80).min((max_chars - gap_len()) / 2);
    let spans = [(0, edge), (text.len() - edge, text.len())];
    rstrip(&render(&spans, text)).to_vec()
}

/// Interned token ids; sets are sorted, deduplicated id vectors.
struct Interner(HashMap<String, u32>);

impl Interner {
    fn id(&mut self, token: String) -> u32 {
        let next = self.0.len() as u32;
        *self.0.entry(token).or_insert(next)
    }

    fn set(&mut self, tokens: Vec<String>) -> Vec<u32> {
        let mut ids: Vec<u32> = tokens.into_iter().map(|t| self.id(t)).collect();
        ids.sort_unstable();
        ids.dedup();
        ids
    }
}

fn intersection_len(a: &[u32], b: &[u32]) -> usize {
    let (mut i, mut j, mut n) = (0, 0, 0);
    while i < a.len() && j < b.len() {
        match a[i].cmp(&b[j]) {
            Ordering::Less => i += 1,
            Ordering::Greater => j += 1,
            Ordering::Equal => {
                n += 1;
                i += 1;
                j += 1;
            }
        }
    }
    n
}

fn intersection(a: &[u32], b: &[u32]) -> Vec<u32> {
    a.iter()
        .copied()
        .filter(|x| b.binary_search(x).is_ok())
        .collect()
}

struct Candidate {
    idx: usize,
    score: f64,
    hits: Vec<u32>,
    terms: Vec<u32>,
}

/// Heap entry ordered like the oracle's `(-priority, idx)` min-heap tuple.
struct Pending {
    neg_priority: f64,
    idx: usize,
    candidate: usize,
}

impl PartialEq for Pending {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Pending {}
impl PartialOrd for Pending {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Pending {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reversed: BinaryHeap is a max-heap. Python compares -0.0 == 0.0.
        let key = self
            .neg_priority
            .partial_cmp(&other.neg_priority)
            .unwrap_or(Ordering::Equal);
        key.then(self.idx.cmp(&other.idx)).reverse()
    }
}

struct Selection<'a> {
    body: &'a [char],
    query_terms: &'a [u32],
    chosen: Vec<Span>,
    covered: HashSet<u32>,
    chosen_terms: Vec<Vec<u32>>,
}

impl Selection<'_> {
    fn priority(&self, candidate: &Candidate) -> f64 {
        let similarity = self
            .chosen_terms
            .iter()
            .map(|previous| {
                let common = intersection_len(&candidate.terms, previous);
                let union = candidate.terms.len() + previous.len() - common;
                common as f64 / union.max(1) as f64
            })
            .fold(None, |best: Option<f64>, value| {
                Some(best.map_or(value, |b| b.max(value)))
            })
            .unwrap_or(0.0);
        let new_hits = candidate
            .hits
            .iter()
            .filter(|h| !self.covered.contains(h))
            .count();
        candidate.score * (1.0 - 0.65 * similarity)
            + (6 * new_hits) as f64 / self.query_terms.len() as f64
    }
}

/// `select_relevant_passages`: pack diverse query evidence into `max_chars`.
pub fn select_relevant_passages(
    text: &str,
    query: &str,
    max_chars: i64,
    chunk_chars: i64,
    include_lead: bool,
    fallback_edges: bool,
) -> String {
    if text.is_empty() || max_chars <= 0 {
        return String::new();
    }
    let max_chars = max_chars as usize;
    let without_links = text.split(LINKS_HEADER).next().unwrap_or("");
    let full: Vec<char> = without_links.chars().collect();
    let body = strip(&full);
    if body.len() <= max_chars {
        return body.iter().collect();
    }
    let query_tokens = tokenize(&query_subject(query));
    if query_tokens.is_empty() {
        let chunk = chunk_chars.max(0) as usize;
        return fallback_excerpt(body, max_chars, chunk, fallback_edges)
            .into_iter()
            .collect();
    }
    let mut interner = Interner(HashMap::default());
    let phrase = query_tokens.join(" ");
    let query_terms = interner.set(query_tokens);

    let chunk_chars = (chunk_chars.min((max_chars / 2) as i64)).max(80) as usize;
    let spans = chunk_spans(body, chunk_chars);
    let mut candidates: Vec<Candidate> = Vec::new();
    let mut seen: HashSet<String> = HashSet::default();
    for (idx, &(start, end)) in spans.iter().enumerate() {
        let chunk = &body[start..end];
        if !seen.insert(collapse_whitespace(chunk)) {
            continue;
        }
        let chunk_text: String = chunk.iter().collect();
        let tokens = tokenize(&chunk_text);
        let token_count = tokens.len();
        let phrase_match = tokens.join(" ").contains(&phrase);
        let mut counts: HashMap<u32, usize> = HashMap::default();
        for token in tokens {
            *counts.entry(interner.id(token)).or_insert(0) += 1;
        }
        let hits: Vec<u32> = query_terms
            .iter()
            .copied()
            .filter(|t| counts.contains_key(t))
            .collect();
        if hits.is_empty() {
            continue;
        }
        let coverage = hits.len() as f64 / query_terms.len() as f64;
        let density_sum: usize = hits.iter().map(|t| counts[t].min(3)).sum();
        let density = density_sum as f64 / token_count.max(1) as f64;
        let heading = chunk_text.split('\n').next().unwrap_or("");
        let heading_set = interner.set(tokenize(heading));
        let heading_hits = intersection_len(&query_terms, &heading_set);
        let score = coverage * 8.0
            + density * 12.0
            + if phrase_match { 2.0 } else { 0.0 }
            + heading_hits as f64 * 0.25;
        let mut terms: Vec<u32> = counts.into_keys().collect();
        terms.sort_unstable();
        candidates.push(Candidate {
            idx,
            score,
            hits,
            terms,
        });
    }
    if candidates.is_empty() {
        return fallback_excerpt(body, max_chars, chunk_chars, fallback_edges)
            .into_iter()
            .collect();
    }

    let mut state = Selection {
        body,
        query_terms: &query_terms,
        chosen: Vec::new(),
        covered: HashSet::default(),
        chosen_terms: Vec::new(),
    };
    if include_lead && max_chars >= 3 {
        let lead_end = spans[0].1.min(max_chars / 3);
        state.chosen.push((0, lead_end));
        let lead: String = body[..lead_end].iter().collect();
        let lead_terms = interner.set(tokenize(&lead));
        state
            .covered
            .extend(intersection(&query_terms, &lead_terms));
    }

    let heap_of = |state: &Selection, live: &[usize]| -> BinaryHeap<Pending> {
        live.iter()
            .map(|&candidate| Pending {
                neg_priority: -state.priority(&candidates[candidate]),
                idx: candidates[candidate].idx,
                candidate,
            })
            .collect()
    };
    let mut pending = heap_of(&state, &(0..candidates.len()).collect::<Vec<_>>());
    let gap = gap_len();
    while let Some(entry) = pending.pop() {
        let candidate = &candidates[entry.candidate];
        let (start, mut end) = spans[candidate.idx];
        let mut trial = state.chosen.clone();
        trial.push((start, end));
        let mut accepted = false;
        if rendered_len(&trial, state.body) <= max_chars {
            state.chosen.push((start, end));
            state.covered.extend(candidate.hits.iter().copied());
            state.chosen_terms.push(candidate.terms.clone());
            accepted = true;
        } else {
            let used = rendered_len(&state.chosen, state.body) as i64;
            let separator = if state.chosen.is_empty() {
                0
            } else {
                gap as i64
            };
            let remaining = max_chars as i64 - used - separator;
            let has_new = candidate.hits.iter().any(|h| !state.covered.contains(h));
            if remaining > 0 && (state.covered.is_empty() || has_new) {
                end = end.min(start + remaining as usize);
                state.chosen.push((start, end));
                let selected: String = body[start..end].iter().collect();
                let selected_terms = interner.set(tokenize(&selected));
                state
                    .covered
                    .extend(intersection(&query_terms, &selected_terms));
                state.chosen_terms.push(selected_terms);
                accepted = true;
            }
        }
        if max_chars as i64 - rendered_len(&state.chosen, state.body) as i64 <= gap as i64 {
            break;
        }
        if accepted {
            let live: Vec<usize> = pending.into_iter().map(|p| p.candidate).collect();
            pending = heap_of(&state, &live);
        }
    }
    let rendered = render(&state.chosen, body);
    rstrip(&rendered[..max_chars.min(rendered.len())])
        .iter()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_body_is_returned_whole_without_links() {
        let text = "  hello world \n--- Page Links ---\n[1] x";
        assert_eq!(
            select_relevant_passages(text, "q", 100, 1200, false, false),
            "hello world"
        );
    }

    #[test]
    fn selects_query_evidence_with_gap_markers() {
        let filler = "lorem ipsum dolor sit amet. ".repeat(20);
        let text = format!("{filler}\n\nrust borrow checker explained\n\n{filler}\n\nmore rust lifetimes\n\n{filler}");
        let out = select_relevant_passages(&text, "rust lifetimes", 200, 1200, false, false);
        assert!(out.contains("rust borrow checker") || out.contains("rust lifetimes"));
        assert!(out.chars().count() <= 200);
    }

    #[test]
    fn no_query_terms_returns_lead() {
        let text = "a".repeat(50) + " " + &"b".repeat(50);
        assert_eq!(
            select_relevant_passages(&text, "the", 10, 1200, false, false),
            "a".repeat(10)
        );
    }
}
