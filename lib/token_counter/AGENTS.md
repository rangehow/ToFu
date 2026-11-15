# Token counter ownership

`api.py` owns resolution/fallback and public CountResult metadata; `base.py`
owns request text, structural and image charges. `tiktoken_counter.py` owns
content-free text caches (the shared digest LRU and the owner-held
`RequestTextCounts` memo with its per-projection views, round-scoped identity
ledger, settled-row slots and estimate scope), multiplicity and call-local
identity receipts. Memoized counts must equal a stateless count of the same
request; verify `test_round_text_count_reuse.py` and the request-path neighbors
it names (compaction, body admission, root loop).
Counts here are local units. `serving_units.py` owns conversion to the serving
model's units for every window decision; `usage_cache.py` owns the
content-bound provider counts it anchors on (`RequestLedger` from
`tiktoken_counter.py`); the per-family ratios are maintained facts in
`lib/model_info/token_unit_facts.json`. Never add a ratio, slack or floor knob:
regenerate the facts (`scripts/measure_token_units.py`). Verify
`test_serving_units.py`, `tests/compaction/test_serving_token_units.py` and
`tests/llm_dispatch/test_fallback_token_units.py`; the policy is
docs/CONTEXT_BUDGET_POLICY.md "Serving-unit measurement".
`native.py` borrows the current app NativeHost count-only encoder, preserving
tiktoken surrogate repair; it never owns model/account/storage authority.
`tiktoken_vocabulary.py` owns the shipped vocabularies (`vocabularies/`, pinned
by tiktoken's SHA-256) and their network-free loader; `_get_encoder` owns the
per-encoding single-flight and the per-process failure memo. Verify
`test_tiktoken_cold_start.py`, `test_tiktoken_shipped_vocabulary.py` and
`test_tiktoken_encoder_failure_memo.py`.

Read docs/modules/context_engineering.md and docs/DEVELOPER_RUNTIME.md.
Verify tests/token_counter plus native_host/app_assembly neighbors for lifecycle
changes. Native ABI constants derive from native_token_count_abi_v1.json via
gen_native_token_count_abi.py; never hand-edit generated consumers. Explicit
native selection must not allocate token-ID vectors, spawn a local executor,
or change cache/fallback/confidence semantics.
