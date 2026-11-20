//! `tofu_search._native` bindings for the URL, authority, citation and HTML
//! extraction kernels (registered by `python.rs`).
//!
//! Functions that return `None` hand the input back to the Python oracle (see
//! `authority.rs`); every other result is final.

use std::collections::{BTreeSet, HashSet};

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::{authority, citations, html, urlkey};

type CitationRow = (
    String,
    String,
    Vec<String>,
    String,
    String,
    String,
    String,
    String,
);

fn rows(items: Vec<citations::Citation>) -> Vec<CitationRow> {
    items
        .into_iter()
        .map(|c| {
            (
                c.key,
                c.title,
                c.authors,
                c.year,
                c.doi,
                c.arxiv_id,
                c.entry_type,
                c.raw,
            )
        })
        .collect()
}

fn to_set(items: BTreeSet<String>) -> HashSet<String> {
    items.into_iter().collect()
}

/// ``search.dedup.canonical_url_key``.
#[pyfunction]
#[pyo3(signature = (url))]
fn canonical_url_key(py: Python<'_>, url: Option<String>) -> String {
    let url = url.unwrap_or_default();
    py.detach(|| urlkey::canonical_url_key(&url))
}

/// ``search.authority.classify_authority``; ``None`` defers to Python.
#[pyfunction]
fn classify_authority(py: Python<'_>, url: String, query: String) -> Option<&'static str> {
    py.detach(|| authority::classify_authority(&url, &query))
}

/// ``search.authority.host_brand_labels``; ``None`` defers to Python.
#[pyfunction]
fn host_brand_labels(py: Python<'_>, url: String) -> Option<HashSet<String>> {
    py.detach(|| authority::host_brand_labels(&url)).map(to_set)
}

/// ``search.authority.brand_tokens``.
#[pyfunction]
#[pyo3(signature = (text))]
fn brand_tokens(py: Python<'_>, text: Option<String>) -> HashSet<String> {
    let text = text.unwrap_or_default();
    to_set(py.detach(|| authority::brand_tokens(&text)))
}

/// ``verify.parse.extract_identifiers`` → ``(doi, arxiv_id)``.
#[pyfunction]
fn extract_identifiers(py: Python<'_>, raw: String) -> (String, String) {
    py.detach(|| citations::extract_identifiers(&raw))
}

/// ``verify.parse.parse_bibtex`` as Citation field tuples.
#[pyfunction]
fn parse_bibtex(py: Python<'_>, text: String) -> Vec<CitationRow> {
    rows(py.detach(|| citations::parse_bibtex(&text)))
}

/// ``verify.parse.parse_references`` as Citation field tuples.
#[pyfunction]
fn parse_references(py: Python<'_>, text: String) -> Vec<CitationRow> {
    rows(py.detach(|| citations::parse_references(&text)))
}

/// ``verify.parse.extract_citations_from_text`` as Citation field tuples.
#[pyfunction]
fn extract_citations_from_text(py: Python<'_>, text: String) -> Vec<CitationRow> {
    rows(py.detach(|| citations::extract_citations_from_text(&text)))
}

/// ``fetch.html_extract`` document assembly after trafilatura:
/// ``(text or None, rendering signals or None)``.
#[pyfunction]
#[pyo3(signature = (html, main_text, url, max_chars, want_rendering))]
fn extract_document<'py>(
    py: Python<'py>,
    html: String,
    main_text: Option<String>,
    url: String,
    max_chars: Option<i64>,
    want_rendering: bool,
) -> PyResult<(Option<String>, Option<Bound<'py, PyDict>>)> {
    let out = py.detach(|| {
        html::extract::extract_document(
            &html,
            main_text.as_deref(),
            &url,
            max_chars,
            want_rendering,
        )
    });
    let rendering = match out.rendering {
        Some(signals) => {
            let dict = PyDict::new(py);
            dict.set_item("application_root", signals.application_root)?;
            dict.set_item("primary_region", signals.primary_region)?;
            dict.set_item("readable_primary", signals.readable_primary)?;
            dict.set_item("requires_javascript", signals.requires_javascript)?;
            Some(dict)
        }
        None => None,
    };
    Ok((out.text, rendering))
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(canonical_url_key, module)?)?;
    module.add_function(wrap_pyfunction!(classify_authority, module)?)?;
    module.add_function(wrap_pyfunction!(host_brand_labels, module)?)?;
    module.add_function(wrap_pyfunction!(brand_tokens, module)?)?;
    module.add_function(wrap_pyfunction!(extract_identifiers, module)?)?;
    module.add_function(wrap_pyfunction!(parse_bibtex, module)?)?;
    module.add_function(wrap_pyfunction!(parse_references, module)?)?;
    module.add_function(wrap_pyfunction!(extract_citations_from_text, module)?)?;
    module.add_function(wrap_pyfunction!(extract_document, module)?)?;
    Ok(())
}
