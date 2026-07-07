---
name: js-bundler-allowlist-orphans-scripts
description: JS bundler allowlist trap (5 manifests: _BUNDLE_FILES/_DEFERRED_FILES/_APP_SCRIPTS_RE/_CRITICAL_FILES/_DEFERRED_ENTRY_POINTS) now CLOSED by test_bundle_manifest_parity.py + shared _APP_SCRIPT_SRC_SUBPATTERN
enabled: true
tags: [frontend, bundler, trap, js]
created: 2026-05-11T04:23:36Z
updated: 2026-07-05T08:27:22Z
---

# JS bundler allowlist trap — missing files load silently as no-op

## Symptom
A feature whose JS file exists in `static/js/` and is referenced via
`<script defer src="static/js/foo.js?v=...">` in `index.html` simply never
runs in production. `typeof window.fooFunction === 'undefined'`, no 404, no
console error — the script tag is just *gone* from served HTML.

## Root cause
`routes/common.py` swaps individual app script tags for a single bundle tag on
every `GET /`. `_APP_SCRIPTS_RE` matches **every** `<script defer
src="static/js/…">` that isn't `bundle-…` and removes it. The replacement
bundle concatenates only files in `_BUNDLE_FILES` (core) / `_DEFERRED_FILES`
(deferred). A file not in either manifest is stripped but never re-added.

## STRUCTURAL FIX (2026-07-05) — the class is now guarded, prefer it
There are FIVE declarations that must agree, all now asserted as a closed
system by **`tests/test_bundle_manifest_parity.py`** (9 tests):
`_BUNDLE_FILES`, `_DEFERRED_FILES`, the `_APP_SCRIPTS_RE` strip set,
`_CRITICAL_FILES ⊆ _BUNDLE_FILES`, and `_DEFERRED_ENTRY_POINTS` ↔ fns actually
defined in the deferred sources.

Key mechanism: `routes/common.py` now has ONE constant
`_APP_SCRIPT_SRC_SUBPATTERN = r'static/js/(?!bundle-)[\w./-]+\.js'` that BOTH
the compiled `_APP_SCRIPTS_RE` (3× interpolation) AND the pure predicate
`is_stripped_app_script(src)` consume — so the predicate can't drift into a
6th copy. The parity test feeds every index.html `<script src>` through the
REAL predicate and fails if a stripped tag has no rebuilding manifest.

The one intentional unbundled file `relay-admin.js` (loads only on
`static/admin.html`) is in `_UNBUNDLED_WHITELIST`, and
`test_unbundled_whitelist_is_justified` asserts it's a `<script>` in admin.html
and NOT in index.html — so a new orphan can't hide behind it.

**When you add/rename a top-level JS module, `test_bundle_manifest_parity.py`
now fails loudly if you forget a manifest or the dev-fallback tag** — you no
longer rely on the manual audit. This test SURFACED a real latent orphan on
first run: `push.js` was in `_BUNDLE_FILES` (CRITICAL) but had no dev-fallback
`<script>` tag in index.html (fixed).

Do NOT auto-discover from `os.listdir` at RUNTIME — load order is hand-ordered
(i18n first, main.js last) and exclusions (relay-admin, feature-loader-is-core)
can't be inferred. Keep ordered manifests as the runtime source of truth;
enforce closure in the TEST.

## Fix when adding a new top-level JS module (unchanged mechanics)
1. Add the filename to `_BUNDLE_FILES` (core) or `_DEFERRED_FILES` (deferred)
   in `lib/js_bundler.py` in correct dependency order (i18n.js first;
   main.js last for core).
2. Add the `<script defer src="static/js/foo.js?v=...">` tag to `index.html`
   for the dev-mode fallback.
3. Rebuild: `python3 -c "from lib.js_bundler import build_bundle; print(build_bundle())"`
4. Run `pytest tests/test_bundle_manifest_parity.py` — it catches a forgotten
   manifest entry OR a forgotten dev-fallback tag in either direction.
5. Hard-refresh the browser (content-hash bundle name changes).

## Related tests
- `tests/test_bundle_manifest_parity.py` — the closed-system parity guard (new).
- `tests/test_artifacts_bundle_registration.py` — per-module ordering + index parity.
- `tests/test_bundle_corruption_guard.py` — corrupt/missing-file degradation + `_CRITICAL_FILES`.

