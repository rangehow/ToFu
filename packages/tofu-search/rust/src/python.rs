//! `tofu_search._native`: PyO3 bindings over the pure kernels.
//!
//! Signatures mirror the Python functions they replace, including `None`
//! tolerance where the oracle writes `text or ''`.

use std::collections::HashSet;

use pyo3::prelude::*;

use crate::{bm25, dedup, lexical, passages};

/// Unicode words and contiguous CJK bigrams, with stable normalization.
#[pyfunction]
#[pyo3(signature = (text))]
fn tokenize(py: Python<'_>, text: Option<String>) -> Vec<String> {
    let text = text.unwrap_or_default();
    py.detach(|| lexical::tokenize(&text))
}

/// Boundary-preserving subject matching, including stop words and symbols.
#[pyfunction]
#[pyo3(signature = (text))]
fn matching_text(py: Python<'_>, text: Option<String>) -> String {
    let text = text.unwrap_or_default();
    py.detach(|| lexical::matching_text(&text))
}

/// Query-subject terms for evidence coverage, in the ``tokenize`` space.
#[pyfunction]
#[pyo3(signature = (query))]
fn subject_terms(py: Python<'_>, query: Option<String>) -> HashSet<String> {
    let query = query.unwrap_or_default();
    py.detach(|| lexical::subject_terms(&query))
}

/// Remove retrieval constraints, preserving quoted subject literals.
#[pyfunction]
#[pyo3(signature = (query))]
fn query_subject(py: Python<'_>, query: Option<String>) -> String {
    let query = query.unwrap_or_default();
    py.detach(|| lexical::query_subject(&query))
}

/// Pack diverse query evidence into a hard character budget, in source order.
#[pyfunction]
#[pyo3(signature = (text, query, max_chars, *, chunk_chars = 1_200, include_lead = false, fallback_edges = false))]
fn select_relevant_passages(
    py: Python<'_>,
    text: Option<String>,
    query: Option<String>,
    max_chars: i64,
    chunk_chars: i64,
    include_lead: bool,
    fallback_edges: bool,
) -> String {
    let text = text.unwrap_or_default();
    let query = query.unwrap_or_default();
    py.detach(|| {
        passages::select_relevant_passages(
            &text,
            &query,
            max_chars,
            chunk_chars,
            include_lead,
            fallback_edges,
        )
    })
}

/// Snippet-group membership as index lists (see ``dedup.group_by_content``).
#[pyfunction]
fn group_indices(py: Python<'_>, texts: Vec<String>, threshold: f64) -> Vec<Vec<usize>> {
    py.detach(|| dedup::group_indices(&texts, threshold))
}

/// Full-content mirror decisions (see ``dedup.dedup_by_full_content``).
#[pyfunction]
fn full_content_duplicates(
    py: Python<'_>,
    texts: Vec<String>,
    threshold: f64,
) -> Vec<Option<usize>> {
    py.detach(|| dedup::full_content_duplicates(&texts, threshold))
}

/// BM25 score per document for the given query terms, in caller order.
#[pyfunction]
fn bm25_scores(py: Python<'_>, query_terms: Vec<String>, doc_texts: Vec<String>) -> Vec<f64> {
    py.detach(|| bm25::bm25_scores(&query_terms, &doc_texts))
}

/// Shingles used by snippet grouping (exposed for parity tests).
#[pyfunction]
fn text_shingles(text: String) -> HashSet<String> {
    dedup::text_shingles(&text)
}

/// Shingles used by full-content dedup (exposed for parity tests).
#[pyfunction]
fn full_text_shingles(text: String) -> HashSet<String> {
    dedup::full_text_shingles(&text)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("UNIDATA_VERSION", crate::unicode_tables::UNIDATA_VERSION)?;
    module.add_function(wrap_pyfunction!(tokenize, module)?)?;
    module.add_function(wrap_pyfunction!(matching_text, module)?)?;
    module.add_function(wrap_pyfunction!(subject_terms, module)?)?;
    module.add_function(wrap_pyfunction!(query_subject, module)?)?;
    module.add_function(wrap_pyfunction!(select_relevant_passages, module)?)?;
    module.add_function(wrap_pyfunction!(group_indices, module)?)?;
    module.add_function(wrap_pyfunction!(full_content_duplicates, module)?)?;
    module.add_function(wrap_pyfunction!(bm25_scores, module)?)?;
    module.add_function(wrap_pyfunction!(text_shingles, module)?)?;
    module.add_function(wrap_pyfunction!(full_text_shingles, module)?)?;
    crate::python_layers::register(module)?;
    Ok(())
}
