//! Native kernels of `tofu-search`.
//!
//! Each module ports one Python owner bit-for-bit and is checked against it by
//! `tests/test_native_parity.py` / `tests/test_native_layers_parity.py`:
//! `lexical` (tokens, matching text, query subjects), `passages` (query-focused
//! excerpts), `dedup` (snippet groups and full-content mirrors), `bm25`
//! (lexical rerank scores), `urlkey` (canonical URL keys), `authority` (URL
//! source hints), `citations` (BibTeX/reference parsing and identifiers) and
//! `html` (`html.parser`/BeautifulSoup tree and the extractor's document
//! assembly). `pyurl`/`pyre` reproduce the CPython library behaviour those
//! owners depend on. The optional `python` feature exposes them as the
//! `tofu_search._native` extension. Kernels are single-threaded and release
//! the GIL; callers own concurrency.

pub mod authority;
pub mod bm25;
pub mod chars;
pub mod citations;
pub mod dedup;
pub mod html;
pub mod lexical;
pub mod passages;
#[rustfmt::skip]
pub mod py_tables;
pub mod pyre;
pub mod pyurl;
#[rustfmt::skip]
pub mod unicode_tables;
pub mod urlkey;

#[cfg(feature = "python")]
mod python;
#[cfg(feature = "python")]
mod python_layers;
