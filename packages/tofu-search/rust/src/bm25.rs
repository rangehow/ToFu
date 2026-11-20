//! Port of the lexical BM25 scorer inside `rerank._rank_candidates`.
//!
//! Python keeps priors, fusion and diversification. Query terms arrive in the
//! caller's iteration order so the floating-point sum is bit-identical.

use rustc_hash::FxHashMap as HashMap;

use crate::lexical::tokenize;

/// `rerank.BM25_K1`.
pub const BM25_K1: f64 = 1.5;
/// `rerank.BM25_B`.
pub const BM25_B: f64 = 0.75;

/// BM25 score of each document text for the given (unique) query terms.
pub fn bm25_scores(query_terms: &[String], doc_texts: &[String]) -> Vec<f64> {
    let index: HashMap<&str, usize> = query_terms
        .iter()
        .enumerate()
        .map(|(i, t)| (t.as_str(), i))
        .collect();
    let mut term_frequencies: Vec<Vec<usize>> = Vec::with_capacity(doc_texts.len());
    let mut doc_lens: Vec<usize> = Vec::with_capacity(doc_texts.len());
    for text in doc_texts {
        let tokens = tokenize(text);
        let mut tf = vec![0usize; query_terms.len()];
        for token in &tokens {
            if let Some(&i) = index.get(token.as_str()) {
                tf[i] += 1;
            }
        }
        doc_lens.push(tokens.len());
        term_frequencies.push(tf);
    }
    let n = doc_texts.len();
    let avg_dl = if n > 0 {
        doc_lens.iter().sum::<usize>() as f64 / n as f64
    } else {
        1.0
    };
    let df: Vec<usize> = (0..query_terms.len())
        .map(|i| term_frequencies.iter().filter(|tf| tf[i] > 0).count())
        .collect();
    term_frequencies
        .iter()
        .zip(&doc_lens)
        .map(|(tf, &dl)| {
            let mut score = 0.0;
            for (i, &count) in tf.iter().enumerate() {
                if count == 0 {
                    continue;
                }
                let d = df[i] as f64;
                let idf = ((n as f64 - d + 0.5) / (d + 0.5) + 1.0).ln();
                let count = count as f64;
                let numerator = count * (BM25_K1 + 1.0);
                let denominator = count + BM25_K1 * (1.0 - BM25_B + BM25_B * dl as f64 / avg_dl);
                score += idf * numerator / denominator;
            }
            score
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matching_document_scores_higher() {
        let terms = vec!["rust".to_owned(), "async".to_owned()];
        let docs = vec![
            "rust async runtime".to_owned(),
            "python".to_owned(),
            String::new(),
        ];
        let scores = bm25_scores(&terms, &docs);
        assert!(scores[0] > 0.0);
        assert_eq!(scores[1], 0.0);
        assert_eq!(scores[2], 0.0);
    }
}
