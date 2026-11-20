# Golden-document corpus

The golden-document corpus is the parity gate for the document workers
(`docs/RUST_REWRITE_EVIDENCE.md`, Phase 7). It freezes real input documents
and the outputs the current Python implementation produces from them. A
replacement implementation of a worker capability is accepted only when the
runner reports no difference beyond the tolerance list below.

| What | Where |
|---|---|
| Case list, input digests, output digests | [`tests/fixtures/golden_documents/manifest.json`](../tests/fixtures/golden_documents/manifest.json) |
| Tolerance list (machine-readable) | [`tests/fixtures/golden_documents/tolerances.json`](../tests/fixtures/golden_documents/tolerances.json) |
| Public inputs and expected outputs | `tests/fixtures/golden_documents/{inputs,expected}/` |
| Private inputs and expected outputs | `data/golden_documents/{inputs,expected}/` of the main checkout (gitignored) |
| Runner | [`scripts/golden_documents.py`](../scripts/golden_documents.py) |
| Executable specification | [`tests/golden_documents/test_golden_document_corpus.py`](../tests/golden_documents/test_golden_document_corpus.py) |
| Inert, single-operation candidate checker (not a Rust producer) | [`tests/golden_documents/check_rust_document_candidate.py`](../tests/golden_documents/check_rust_document_candidate.py), [`test_rust_document_candidate.py`](../tests/golden_documents/test_rust_document_candidate.py) |
| How the synthetic inputs were made | [`tests/fixtures/golden_documents/synthetic/build_inputs.py`](../tests/fixtures/golden_documents/synthetic/build_inputs.py) |

## Operations

Each case runs one or more operations. An operation is one public worker entry
point with fixed parameters; its output is `result.json` plus binary files
referenced from it as `{"$blob": "<relative path>", "kind": "<kind>"}`.

| Operation | Python owner | Output |
|---|---|---|
| `doc.extract` | `lib.doc_parser.extract_document_text` (`max_chars`) | the returned dict |
| `pdf.parse` | `lib.pdf_parser.core.parse_pdf` (`text_mode`, `max_pages`, `max_text_chars`, `max_images`, `max_image_width`) | the returned dict; each figure clip's `base64` becomes a `raster` blob |
| `slides.import_pptx` | `lib.slides.import_pptx.import_pptx` (`slide_numbers`) | report pages/theme/skipped and the PPTD tree (YAML parsed, media as `bytes` blobs) |
| `slides.import_html` | `import_pptx` then `render_html.render_page_html(fetch_fonts=False)` | one HTML document per page (attachment preview markup) |
| `slides.pptx_preview` | `lib.slides.pptx_preview.pptx_geometry` and `render_pptx_page` (Chromium) | geometry, per page size, link boxes and a `raster` PNG |
| `slides.inspect_pptx` | `lib.slides.pptx_qa.inspect_pptx_package` | the QA dict |
| `slides.export_pptx` | `lib.slides.export_pptx.export_pptx(embed_fonts=True)` on a PPTD deck | the export summary and the package as an `ooxml` blob |
| `slides.render_html` | `render_html.render_page_html` on a PPTD deck | one HTML document per page |
| `slides.render_png` | `lib.slides.render_png.render_previews` (Chromium) | one `raster` PNG per page |

## Cases

The manifest lists 21 cases (60 operations; 15 public, 6 private). Each case names its features and
the existing tests it mirrors (`mirrors`), so the corpus carries the cases
`tests/slides`, `tests/pdf_parser` and `tests/doc_parser` exercise as frozen
documents rather than as in-test constructions.

- **Synthetic, committed** (project-owned): a DOCX with tables, merged cells,
  CJK and a long body (complete and budgeted extraction); the hostile-markup,
  hyperlink, table, chart and picture PPTX of `test_import_pptx_fidelity.py`;
  the 200-slide PPTX of the truncation tests; the bounded-pages PDF and the
  figure/table-caption PDF of `test_pdf_classic_bounds.py` and
  `test_figure_table_demand.py`; a PPTD deck with gradient, shadow, connector,
  image, table and charts that exports with embedded fonts.
- **Public real-world, committed**: unmodified copies, each with its source
  URL, licence and licence evidence in the manifest's `origin`:
  - PDF: a U.S. Census Bureau brief, public domain, with appendix tables
    that continue over several pages, maps and embedded fonts; a People's Bank
    of China disclosure report (CJK, tables, CID fonts; official document,
    PRC Copyright Law Art. 5); two ACL Anthology papers and a PLOS ONE paper
    (CC BY; two-column layout, tables, figures, math).
  - DOCX: the GSA 18F team-charter template (public domain/CC0, tables) and a
    Ministry of Science and Technology application form (CJK, merged-cell
    table; Art. 5).
  - PPTX: a CC0 Zenodo deck with native tables, and a CC BY 4.0 Zenodo deck
    in Japanese (CJK, tables, native charts, pictures). The Japanese deck
    contains its author's photo and university logo, which the author
    published under that licence. No licence-clean Chinese-language PPTX
    was found, so Chinese PPTX coverage comes from the private decks.
- **Private real-world, digests only**: the owner's own Tofu-exported CJK decks
  (PPTX, their PPTD sources and a PDF rendering) and a 158-page CJK PDF. Their
  bytes stay in `data/golden_documents/`; the manifest commits the input
  sha256 and the sha256 of the frozen expected tree.

Inputs that are directories (PPTD decks) are digested as the sha256 of
`relpath\0sha256(file)\n` lines in path order; expected trees use the same
digest.

## Implementation protocol

```
python3 scripts/golden_documents.py check [--impl-cmd 'CMD {op} {input} {out} {params}'] \
    [--case GLOB] [--out DIR] [--report report.json] [--require-private]
python3 scripts/golden_documents.py produce --impl-cmd ... --out DIR
python3 scripts/golden_documents.py compare --out DIR
python3 scripts/golden_documents.py freeze [--case GLOB]
```

`--impl-cmd` is any command template. The runner substitutes the operation id,
the input path, an empty output directory and the parameters as JSON, runs it
once per operation, and compares the result. Without `--impl-cmd` the Python
implementation runs in-process; `python3 scripts/golden_documents.py
python-impl {op} {input} {out} {params}` is the same implementation behind
the command protocol. A port can also write the output tree itself and run
`compare --out`.

`check` first verifies each case's input digest and stored expected digest
against the manifest. A public case whose files are missing or changed fails.
A private case whose files are absent (any checkout other than the owner's) is
reported as `SKIP … unavailable` and does not fail unless `--require-private`
is given; the parity gate for a port runs with `--require-private` on the
owner's host.

`freeze` re-baselines from the current Python implementation, rewriting the
expected trees and manifest digests. A freeze is a reviewed change: the diff of
`expected/*/result.json` is the evidence of what changed.

### Inert local candidate comparison

For `synthetic-pptx-import-fidelity` / `slides.import_pptx` only:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m tests.golden_documents.check_rust_document_candidate \
    --candidate-dir <already-produced-operation-directory>
```

This test-only checker runs no implementation and accepts no private-case
selector. It verifies this public input and expected tree against the manifest,
then reuses the same comparator and operation tolerances. Exit 0 means a
**comparison-only match with Rust producer/source unverified**, exit 1 is a
difference, and exit 2 rejects missing/invalid output. Candidate JSON must be
finite, its result and blobs locally contained and separate from the corpus,
and blob metadata exactly `$blob`/`kind`. No new masks or tolerances are added.
The fixture-copy and rounding tests exercise the checker, not a producer.

There is no established Rust PPTX-to-PPTD owner or source-matched executable
for this operation in the bounded inspection. See the
[document-worker source map](RUST_REWRITE_EVIDENCE.md#bounded-document-worker-source-map-and-local-comparison-bridge)
for the Python owner, existing command bridge and exact missing Rust producer.
The [source-only metadata tranche](RUST_REWRITE_EVIDENCE.md#native-read-only-import-tranche-source-only-admission-boundary)
names the selected fields, two synthetic operation fixtures, parser/build
availability gaps and differential recipe. It is not an implemented importer
or an extra mode of this full-output checker.
The delivered 60 Python results do not establish Rust parity. This bridge does
not rerun them, load private documents, or qualify a rewrite/release.

## Tolerance list

The runner applies exactly these rules (from `tolerances.json`); anything not
listed is compared exactly. Changing a rule is a reviewed change to this
section and that file together.

| Rule | Applies to | Tolerance | Why |
|---|---|---|---|
| Structure | all JSON | object key sets, array lengths and value types exact | a missing or extra field is a contract change |
| Text | all strings, including extracted text, Markdown and HTML | exact; no whitespace or Unicode normalization | extracted text is the product |
| Numbers | all numbers | absolute 1e-9 | absorbs float formatting of the same arithmetic only |
| Link boxes | `slides.pptx_preview` `/pages/*/links/*` | absolute 0.002 of the page | measured from browser layout; under 3 px at 1280 px |
| Raster | `raster` blobs: PDF figure clips, page previews | equal dimensions; mean per-channel RGBA difference at most 2/255; at most 1% of pixels whose largest RGB difference exceeds 48 | anti-aliasing, resampling and codec differences pass; a moved, missing or recoloured element fails |
| OOXML package | `ooxml` blobs | equal part sets; `.xml`/`.rels` parts by C14N 2.0 with `dcterms:created`/`modified` masked; ZIP order, timestamps and compression ignored; nested packages recurse | python-pptx stamps times; attribute order and namespace placement are not semantic |
| Embedded fonts | `.fntdata` parts | equal family and subfamily names and exact cmap codepoint set | subsetters legitimately differ in table layout and glyph order |
| Bytes | `bytes` blobs, other package parts | sha256 exact | imported media and images are copied, not re-encoded |
| Host paths | all strings | the output directory becomes `{out}`, the input path `{input}` | absolute paths differ per run |
| Font URLs | `slides.import_html`, `slides.render_html` | `@font-face` `url('file://…')` becomes `url('{font-file}')`; family, weight and format stay exact | the host font cache path is not output |
| Ignored fields | `pdf.parse` `/images/*/sizeKB`; `slides.inspect_pptx` `/bytes`; `slides.export_pptx` `/summary/bytes`, `/summary/packageValidation/bytes`, `/summary/embeddedFontBytes` | not compared | encoder and compression sizes; the encoded content is compared by its own rule |

### Environment preconditions

The expected outputs depend on the pinned document libraries (PyMuPDF and
pymupdf4llm, python-pptx, python-docx), the registered design-system fonts
(fixed-size sources from `lib/design_sys/fonts.py`, fetched into the font
cache on first use), and Playwright Chromium for `slides.pptx_preview` and
`slides.render_png`. Untrusted previews (`slides.import_html`,
`slides.pptx_preview`) never download fonts and fall back to host fonts,
which changes layout and link boxes, so the Python adapter first stores every
registered face the imported deck names; a port must render with the same
faces present. A library upgrade that changes output is a freeze, not a
tolerance change.

## Decision record

- *Problem:* Phase 7 ports document-worker capabilities only when a Rust
  implementation "passes the same golden-file corpus"; no corpus existed, and
  the current tests build their documents inline, so a port could not be
  compared against frozen behaviour.
- *Decision:* one manifest-driven corpus with an implementation-neutral
  command protocol, a comparator that owns every normalization and tolerance,
  public inputs committed, private inputs kept in `data/` with committed
  digests.
- *Alternatives considered:* per-test snapshot files beside each Python test
  (rejected: they freeze Python call shapes, not worker outputs, and cannot
  run against another implementation); byte-exact outputs (rejected: ZIP
  timestamps, encoders, subsetters and anti-aliasing differ without any
  behavioural difference); committing private documents (rejected: the
  owner's documents stay out of the repository).
