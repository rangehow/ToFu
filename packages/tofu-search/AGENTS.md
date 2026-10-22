# Search package guidance

## Scope

This directory owns the independently buildable `tofu-search` distribution:
multi-engine search, bounded fetching/extraction, vertical lookups, citation
verification, and optional MCP delivery. Its public API and provider seams are
the only host integration boundary.

## Editing rules

- Do not import Tofu host `lib`, `routes`, storage, browser, identity, or model
  modules. Host credentials and browser authority arrive through request-owned
  provider interfaces and remain no-ops for standalone callers.
- Keep response bytes, retries, caches, crawl depth,
  browser processes, and optional native dependencies explicitly owned/bounded.
  Independent eligible calls use demand workers; local count gates grant no
  endpoint/billing authority. Explicit request policies keep their owners.
- Public API changes update the package facade, member tests, host adapter,
  compatibility gate, changelog, and version range together.
- The package builds from this directory. Never restore sibling-checkout or
  editable-install assumptions outside `packages/tofu-search/`.

## Verification

`tofu_search/lexical.py` owns subject parsing/tokenization shared by fetch
admission, ranking and excerpts; `passages.py` preserves original source spans
for search context, query-focused reads and the host's extraction windows.
`fetch/core.py` keeps focused projection outside the common raw-fetch pipeline;
verify `tests/test_query_fetch.py` and host `test_fetch_url_full_source.py`.
`fetch/html_fragments.py` selects literal anchors from the sanitized DOM;
`fetch/html_extract.py` owns all text extraction and fidelity rules.
`fetch/pdf_extract.py` owns lazy native/base PDF readers and literal page text
budgets; `fetch/core.py` keeps budget-specific PDF cache entries separate from
ordinary HTML previews. Verify `test_default_pdf.py`, `test_pdf_failure_evidence.py`
and `test_lazy_pdf_dependencies.py`; qualify the native extra separately.
`fetch/html_streaming.py` restores declared received completions before that
visibility boundary; verify `tests/test_html_streaming.py` and its runtime fixture.
`fetch/html_rendering.py` classifies that DOM for recovery; `fetch/rendered_source.py`
owns bounded browser readiness/projection through the same extractor. Verify
`test_html_rendering_admission.py`, `test_rendered_page_source.py`, then opt-in
`TOFU_SEARCH_TEST_BROWSER=1 pytest .../test_rendered_dom_browser.py` with Playwright.
`search/proxy_mode.py::fetch_attempt_plan` owns the HTTP/browser route identity;
`fetch/browser_proxy.py` projects the selected anonymous route into a context.
`fetch/document_response.py` scopes main-document status observations for reads.
Verify `test_browser_proxy.py`, rendered-source tests, then the opt-in
`test_browser_proxy_integration.py` with an owned loopback proxy.
Quality contracts and the offline measurement command live in
`docs/relevance-quality.md`; regression fixtures do not prove live superiority.
Free source admission and metasearch recovery are owned by `docs/free-search.md`.
`fetch/diagnostics.py` owns safe failure facts for delivery adapters; raw fetch
diagnostics and private details must not be forwarded directly to models.

Run `uv run --package tofu-search pytest packages/tofu-search/tests`, then the
root search bridge/runtime and monorepo contract tests for integration changes.

`tofu_search/_worker_executor.py` is generated from `tofu_workers/executor.py`
by `scripts/gen_packaged_demand_workers.py`; edit the kernel and regenerate,
never the private copy. Check generation, demand/startup/cancel/close tests,
then build a wheel and import it in a fresh process outside the checkout.
MCP uses this same kernel rather than AnyIO's count limiter; its CLI teardown
joins published calls. No runtime import reaches outside this distribution.

`tofu_search/generated_local_models.py` is inert model/artifact metadata exported
by `scripts/gen_packaged_local_models.py` after common Model admission. Edit
`lib/model_info/local_model_artifacts.json` and its contract, then regenerate;
never copy researched model facts into a second runtime registry. Verify
`tests/model_routing/test_local_model_artifacts.py`,
`tests/meta/test_packaged_local_models.py` and the package's
`tests/test_local_model_manifest.py`, including standalone wheel import.

`local_rerank.py` owns the optional, explicitly selected local CPU scorer;
`_local_rerank_worker.py` verifies pinned bytes before importing model libraries.
Reuse its owner across related calls, propagate Stop, and retain cleanup debt.
`scripts/gen_packaged_search_runtime.py` exports the shared resource probe from
`tofu_workers/resources.py` and retirement from `lib/project_mod/process_group.py`
as `generated_resources.py` and `generated_process_groups.py`. It also exports
`lib/paper/builds/pdf_validation_limits.py` as `generated_process_lifetime.py`;
the worker binds its parent before input/model loading. Edit their owners,
then regenerate. Verify `test_local_rerank_*.py`, MCP cancellation, host binding,
generation drift and installed-wheel execution. Quality and resource admission
live in `docs/relevance-quality.md` and the root `docs/RESOURCE_CONTROL.md`.
