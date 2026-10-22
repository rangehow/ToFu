"""Source-code sanitization (defaults + opensource scrub) and the rg-driven
targeted post-copy sanitize pass (split out of ``export.py``)."""
import os
import re
import subprocess
from pathlib import Path

from export_pkg import _state
from export_pkg._excludes import _is_text_file
from export_pkg._lists import (
    C_END,
    C_YELLOW,
    _ENDPOINTS,
    _INTERNAL_DOMAIN_LITERALS,
    _INTERNAL_EXCLUDED_API_KEYS,
    _INTERNAL_FIXED_CANDIDATES,
    _INTERNAL_IDENTIFIER_REPLACEMENTS,
    _INTERNAL_SANITIZE_TRIGGERS,
    _OPENSOURCE_FIXED_CANDIDATES,
    _RG_MAX_FILESIZE,
    _RG_MAX_FILESIZE_BYTES,
    _SECRETS,
    _have_tool,
    logger,
)
def _sanitize_defaults_for_export(content: str, filepath: str,
                                    version: str = '') -> str:
    """Sanitize default settings for exported projects (internal & opensource).

    Changes the default Thinking Depth from 'medium' to 'off' so exported
    copies start with thinking disabled.  Also stamps the version into
    cache-bust parameters (?v=...) for consistent versioning.

    NOTE: The JS source already removed most ``|| 'medium'`` fallbacks in
    core.js and main.js (they rely on config.defaultThinkingDepth being
    always initialized).  The remaining targets are:
      - core.js: the single initialization line ``= 'medium'``
      - settings.js: fallbacks already use ``|| 'off'`` (no-op here)
      - model_config.py: server-side default ``'medium'``
      - index.html: ``<select>`` default ``selected`` attribute
    """
    # ── Version stamp: replace cache-bust ?v= parameters with the release version ──
    if version and (filepath.endswith('.html') or filepath.endswith('.js')):
        # Replace ?v=YYYYMMDD[a-z] style cache busters → ?v=0.5.0
        content = re.sub(r'\?v=\d{8}[a-z]?', f'?v={version}', content)
        # Also replace _ICON_V = 'YYYYMMDD[a-z]' in settings.js
        if filepath.endswith('settings.js'):
            content = re.sub(
                r"const _ICON_V = '[^']+';",
                f"const _ICON_V = '{version}';",
                content,
            )
    # ── JS: core.js — change the single init default from 'medium' to 'off' ──
    if filepath.endswith('core.js'):
        content = content.replace(
            "config.defaultThinkingDepth = 'medium';",
            "config.defaultThinkingDepth = 'off';",
        )
        # ── core.js — change hardcoded serverModel from opus to gpt-4o ──
        # The JS validation in main.js will auto-correct to an available model
        # on first load, but the initial value should be a reasonable default.
        content = content.replace(
            'let serverModel = "aws.claude-opus-4.7";',
            'let serverModel = "gpt-4o";',
        )

    # ── Python: model_config.py — change server-side default from 'medium' to 'off' ──
    if filepath.endswith('model_config.py'):
        content = content.replace(
            "_default_depth = cfg.get('defaultThinkingDepth', 'medium')",
            "_default_depth = cfg.get('defaultThinkingDepth', 'off')",
        )

    # ── HTML: index.html — change <select> default from medium to off ──
    if filepath.endswith('index.html'):
        # Robust against attribute reordering / extra attrs (e.g. data-i18n)
        # by using regex. Use ``[\w-]`` (NOT ``[a-z\-]``) so attribute names
        # containing hyphens like ``data-i18n`` match.
        # Pattern 1: strip ``selected`` from <option value="medium" …>.
        content = re.sub(
            r'(<option\s+value="medium"(?:\s+[\w-]+="[^"]*")*)\s+selected(\b)',
            r'\1\2',
            content, flags=re.IGNORECASE,
        )
        # Pattern 2: add ``selected`` to <option value="off" …> if not present.
        def _add_selected(m):
            if 'selected' in m.group(0):
                return m.group(0)
            return m.group(0).replace('value="off"', 'value="off" selected', 1)
        content = re.sub(
            r'<option\s+value="off"(?:\s+[\w-]+="[^"]*")*\s*>',
            _add_selected, content, flags=re.IGNORECASE,
        )

    return content


def _sanitize_source_opensource(content: str, filepath: str) -> str:
    """Apply all opensource sanitization to a source file's content.

    Order matters: replace longer strings first to avoid partial matches.
    """
    # 1. Replace hardcoded secrets
    for secret, replacement in _SECRETS.items():
        content = content.replace(secret, replacement)

    # 2. Replace internal endpoints (both code and comments)
    for endpoint, replacement in _ENDPOINTS.items():
        content = re.sub(re.escape(endpoint), replacement, content, flags=re.IGNORECASE)

    # 2b. Also clean bare domain references in comments/strings
    #     e.g. "aigc.sankuai.com" in comments
    content = re.sub(r'aigc\.sankuai\.com', 'your-llm-gateway.example.com',
                     content, flags=re.IGNORECASE)

    # 3. Clean _DEFAULT_KEYS list in lib/__init__.py
    if filepath.endswith('lib/__init__.py') or filepath.endswith('lib' + os.sep + '__init__.py'):
        content = re.sub(
            r"_DEFAULT_KEYS\s*=\s*\[.*?\]",
            "_DEFAULT_KEYS = [\n    # Add your API keys here, or set LLM_API_KEYS env var\n]",
            content,
            flags=re.DOTALL,
        )

    # 4. Clean bootstrap.py default API key (defense-in-depth; source already clean)
    if filepath.endswith('bootstrap.py'):
        content = content.replace(
            "api_keys = [single] if single else ['2003386484270948427']",
            "api_keys = [single] if single else []",
        )

    # 5. Proxy bypass is now fully auto-managed by lib/proxy.py — server.py
    #    no longer has _INTERNAL_DOMAINS hardcoded.  No sanitization needed.

    # 6. Clean model-specific comments in lib/model_info.py
    #    (Was previously in llm_client.py; moved with the 2026-05-21 split.)
    if filepath.endswith('model_info.py'):
        content = content.replace(
            'def is_longcat(model: str) -> bool:\n    """Internal LongCat models (Flash, MoE, etc.).',
            'def is_longcat(model: str) -> bool:\n    """LongCat models (Flash, MoE, etc.).',
        )

    # 6b. Clean _HEADER_MIGRATIONS in dispatcher.py — contains internal
    #     infrastructure routing details (header names + values)
    if 'dispatcher.py' in filepath:
        content = content.replace(
            "'sankuai.com': {'M-TransferContext-INF-CELL': 'gray-release-ai-gpt-test'},",
            "# 'your-domain.com': {'X-Custom-Header': 'value'},  # per-provider custom headers",
        )

    # 7. (migrate.py was removed — no longer needs sanitization)

    # 8. Clean hardcoded absolute paths (/mnt/dolphinfs/...)
    content = re.sub(
        r'/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/INS/ruanjunhao04/[^\s\'"]+',
        '/path/to/your/project',
        content,
    )
    content = re.sub(
        r'/mnt/dolphinfs/ssd_pool/docker/user/hadoop-aipnlp/[^\s\'"]+',
        '/path/to/your/data',
        content,
    )
    # Catch bare /mnt/dolphinfs/... references in docstrings/comments
    content = re.sub(
        r'/mnt/dolphinfs/[^\s\'"`]*',
        '/mnt/your-fs',
        content,
    )

    # 9. Clean FEISHU default project path in _state.py
    if '_state.py' in filepath and 'feishu' in filepath:
        content = content.replace(
            "os.path.expanduser('~/Projects/tofu')",
            "os.path.expanduser('~/tofu')",
        )

    # 10. Clean default provider ID 'sankuai' → 'default' across all files
    #     Defense-in-depth: source already uses 'default' provider ID
    content = content.replace("provider_id='sankuai'", "provider_id='default'")
    content = content.replace("provider_id: str = 'sankuai'", "provider_id: str = 'default'")
    content = content.replace("'id': 'sankuai'", "'id': 'default'")
    content = content.replace("'name': 'Meituan'", "'name': 'Default'")
    content = content.replace("prov_id = provider.get('id', 'sankuai')", "prov_id = provider.get('id', 'default')")
    content = content.replace("m.get('provider_id', 'sankuai')", "m.get('provider_id', 'default')")

    # Maintainer-only range controls are absent from the public settings panel.
    if filepath.endswith('frontend/src/runtime/sections/main/main_input_handling.js'):
        content = re.sub(
            r'(?m)^function _syncRangeOutput\([^\n]*\) \{\n.*?^\}\n',
            '', content, flags=re.DOTALL | re.MULTILINE)
    if filepath.endswith('frontend/src/runtime/sections/_epilogue.js'):
        content = content.replace('  _syncRangeOutput,\n', '')

    # 11. Clean settings.js brand detection and provider name display
    if filepath.endswith('settings.js'):
        content = re.sub(
            r"\[/sankuai\|longcat\|meituan\|三快\|龙猫/i,\s*'meituan'\],",
            "// [/your-org-pattern/i, 'your-org'],  // Add org detection pattern",
            content,
        )
        content = content.replace("meituan:'Meituan'", "default:'Default'")

    if filepath.endswith('main.js'):
        content = content.replace("meituan:'Meituan'", "default:'Default'")

    # 12. Clean .sankuai.com / .meituan.com domain literals in any context
    #     (comments, strings, etc.) — but not in regex patterns already
    #     handled above
    for domain in _INTERNAL_DOMAIN_LITERALS:
        content = re.sub(re.escape(domain), '.internal.example.com',
                         content, flags=re.IGNORECASE)
        content = re.sub(r'(?<![A-Za-z0-9.-])' + re.escape(domain.lstrip('.')) + r'\b',
                         'internal.example.com', content, flags=re.IGNORECASE)

    # 13. Clean plain-substring internal identifiers (usernames, HTTP header /
    #     gray-release tokens that leak via probe scripts + dispatcher header
    #     migrations). Driven from the single-source-of-truth tuple so the scrub
    #     and the file-open trigger list in _opensource_sanitize_triggers can
    #     never drift apart.
    for ident, replacement in _INTERNAL_IDENTIFIER_REPLACEMENTS:
        content = content.replace(ident, replacement)

    # 14. Strip internal-only MCP catalog entries from lib/mcp/registry.py.
    #     Entries inside the '# ── Meituan Internal' block reference
    #     internal-only services (hope CLI, Xuecheng/学城 docs, …) and
    #     must NOT appear in opensource exports. The block runs from the
    #     '# ── Meituan Internal' banner to (but not including) the next
    #     '    # ── ' section header at outer-dict (4-space) indent.
    if filepath.endswith('lib/mcp/registry.py') or filepath.endswith('lib' + os.sep + 'mcp' + os.sep + 'registry.py'):
        content = re.sub(
            r"\n    # ── Meituan Internal.*?─\n\n"
            r"(?:.*?\n)*?"            # any number of lines (entries)…
            r"(?=    # ── )",          # …up to (but not including) the next section header
            "\n",
            content,
        )
        # Belt-and-braces: hard-default the runtime build flag to True so any
        # ``internal_only`` entry that ever escapes the source strip above is
        # still filtered out by get_catalog() at runtime — independent of the
        # env var or entry ordering.
        content = content.replace(
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '').strip().lower() in {",
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '1').strip().lower() in {",
        )

    # 14b. Bake the opensource build flag in lib/settings_panels.py so the
    #     exported tree strips maintainer-only settings tabs at render time
    #     even when TOFU_OPENSOURCE_BUILD is unset — same belt-and-braces
    #     contract as the registry.py bake above. The fragment itself is
    #     excluded via OPENSOURCE_EXTRA_EXCLUDE_FILES ('experiments.html');
    #     this bake is what makes the render-time strip default-on there.
    if filepath.endswith('lib/settings_panels.py'):
        content = content.replace(
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '').strip().lower() in {",
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '1').strip().lower() in {",
        )

    # 15. Clean remaining internal identifiers.
    #     'sankuai' (三快) is an internal corp name — always replace.
    #     Inside an identifier (word char on either side) the hyphenated form
    #     breaks Python/JS syntax — `def test_sankuai_…` became
    #     `def test_example-corp_…` and the exported file no longer parsed
    #     (bitten 2026-08-05, tests/llm_sanitize/test_gateway_sanitize.py). Identifiers get
    #     the underscore form; prose/domains keep the hyphen.
    content = re.sub(r'(?<=[A-Za-z0-9_])sankuai|sankuai(?=[A-Za-z0-9_])',
                     'example_corp', content)
    content = content.replace('sankuai', 'example-corp')
    content = content.replace('三快', 'your-corp')
    content = content.replace('龙猫', 'your-mascot')

    #     'meituan' / 'Meituan' is a public brand (HKEX:3690). Keep in brand
    #     icon registries, CSS selectors, and brand detection arrays — same
    #     category as 'openai', 'google'. Replace elsewhere as provider name.
    _is_brand_asset = (
        filepath.endswith('settings.js')
        or filepath.endswith('main.js')
        or filepath.endswith('.css')
    )
    if not _is_brand_asset:
        content = content.replace('Meituan', 'YourProvider')
        # NOTE: the replacement token MUST be a valid JS identifier. `meituan`
        # historically appeared as an UNQUOTED object key
        # (`meituan: '<svg…>'`) in retained brand registries. A future key of
        # that shape must remain safe too: a hyphenated token (`your-provider`) turns
        # that into `your-provider:` which JS parses as subtraction
        # (`your - provider`) → "Uncaught SyntaxError: Unexpected token '-'",
        # white-screening the whole exported app. `yourprovider` is a bareword
        # key wherever `meituan` was, so the exported JS always parses.
        # (Do NOT gate this on a per-file allowlist — that list drifts on every
        # refactor and is exactly what orphaned the brand registries before.)
        content = content.replace('meituan', 'yourprovider')

    return content


# ═══════════════════════════════════════════════════════════
#  File copy logic
# ═══════════════════════════════════════════════════════════

def _opensource_sanitize_triggers() -> list[str]:
    triggers = list(_INTERNAL_SANITIZE_TRIGGERS)
    for s in _SECRETS:
        triggers.append(re.escape(s))
    for s in _ENDPOINTS:
        triggers.append('(?i:' + re.escape(s) + ')')
    for s in _INTERNAL_DOMAIN_LITERALS:
        triggers.append('(?i:' + re.escape(s) + ')')
    # Internal identifiers that the secret-scan also catches —
    # MUST be in the sanitize trigger list so the rules below run. The
    # plain-substring identifiers come from the single-source-of-truth
    # _INTERNAL_IDENTIFIER_REPLACEMENTS tuple (so scrub + trigger can't drift);
    # the path/domain literals stay explicit here.
    triggers.append(re.escape('/mnt/dolphinfs'))
    for domain in ('sankuai.com', 'meituan.com'):
        triggers.append('(?i:' + re.escape(domain) + ')')
    for ident, _replacement in _INTERNAL_IDENTIFIER_REPLACEMENTS:
        triggers.append(re.escape(ident))
    return triggers


# rg's per-file size ceiling for the secret/endpoint scan. rg SKIPS files
# above this — which is a leak if a large text file carries a secret — so
# _scan_oversized_text_files scans anything above it in-process. Kept as a
# constant so the rg flag and the supplement guard can never drift apart.
def _scan_oversized_text_files(root: Path, combined_rx: str,
                               extra_globs: list[str] | None = None) -> set[str]:
    """Regex-scan TEXT files larger than the rg ``--max-filesize`` cap.

    rg silently skips files above ``_RG_MAX_FILESIZE``; on the opensource
    export that means an oversized text file with a secret / internal hostname
    would ship un-sanitized. This walks ``root`` for text files strictly above
    the byte cap and returns those whose content matches ``combined_rx`` —
    closing the leak rg leaves open. Bounded work: only files ABOVE the cap are
    read (rare in a source tree). ``extra_globs`` are advisory only here (the
    heavy dirs they exclude are almost never text), so we simply skip obvious
    non-text via ``_is_text_file``.
    """
    try:
        rx = re.compile(combined_rx)
    except re.error as e:
        logger.warning('[export] oversized-scan regex compile failed: %s', e)
        return set()
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fpath = Path(dirpath) / fn
            if not _is_text_file(str(fpath)):
                continue
            try:
                if fpath.stat().st_size <= _RG_MAX_FILESIZE_BYTES:
                    continue  # rg already covered it
                content = fpath.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if rx.search(content):
                out.add(str(fpath.relative_to(root)))
                logger.warning('[export] oversized text file matched sanitize '
                               'trigger (rg would have SKIPPED it): %s',
                               fpath.relative_to(root))
    return out
def _rg_files_with_matches(root: Path, patterns: list[str],
                            extra_globs: list[str] | None = None) -> set[str]:
    """Return relative paths under ``root`` that match any of ``patterns``.

    Uses a single ``rg --files-with-matches`` invocation with regex
    alternation. Falls back to a Python walk when rg is unavailable.

    Patterns are joined with ``|``; callers are responsible for ensuring
    each pattern is a valid PCRE2-compatible regex (literals should be
    pre-escaped via ``re.escape``).

    ``extra_globs`` is a list of ``rg --glob`` patterns (``!path`` syntax
    excludes a path) — used to keep rg out of huge dirs like
    ``swebench_workdir/`` that aren't part of the export anyway.
    """
    if not patterns:
        return set()
    combined = '|'.join(f'(?:{p})' for p in patterns)
    if _have_tool('rg'):
        try:
            # --no-ignore (not merely --no-ignore-vcs): the repo's dev-facing
            # .ignore hides generated bundles (app-runtime.js,
            # *.generated.js) from ordinary rg discovery, and rg honours
            # .ignore EVEN WITH --no-ignore-vcs (that flag only disables the
            # gitignore family). The tar copy ships .ignore into dest, so a
            # trigger surviving only inside a generated bundle was invisible
            # here and shipped verbatim — settings-presenters.generated.js
            # leaked the internal gateway host exactly this way until the
            # python publish gate caught it. Ignore files are a discovery
            # aid, never a security boundary: this scan must see every file
            # the tar copy shipped.
            cmd = ['rg', '--files-with-matches', '--no-messages',
                   '--hidden', '--no-ignore',
                   '--max-filesize', _RG_MAX_FILESIZE]
            for g in (extra_globs or []):
                cmd.extend(['--glob', g])
            cmd.extend(['-e', combined, str(root)])
            r = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=180)
            # rg exits 1 when there are no matches — that's not an error.
            if r.returncode in (0, 1):
                hits = {
                    str(Path(p).relative_to(root))
                    for p in r.stdout.splitlines() if p.strip()
                }
                # ── Oversized-text-file leak guard (SECURITY) ──
                # rg silently SKIPS files above --max-filesize, so a >cap text
                # file carrying a secret / internal hostname would never enter
                # the candidate set and would ship UN-sanitized. rg only scans
                # files it did NOT skip, so we must scan the oversized ones
                # ourselves: walk for text files above the cap and regex them
                # in-process (rare, so the cost is bounded). A match adds the
                # file to the candidate set exactly as the rg path would.
                hits |= _scan_oversized_text_files(root, combined, extra_globs)
                return hits
            logger.warning('rg returned rc=%s: %s', r.returncode,
                           (r.stderr or '')[:300])
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('rg search failed: %s', e)

    # Python fallback — slower but never wrong.
    rx = re.compile(combined)
    out: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fpath = Path(dirpath) / fn
            if not _is_text_file(str(fpath)):
                continue
            try:
                content = fpath.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                continue
            if rx.search(content):
                out.add(str(fpath.relative_to(root)))
    return out


def _rg_excluded_globs() -> list[str]:
    """Build a tight ``rg --glob !X`` exclude list for the post-copy scan.

    Kept minimal: the tar copy has already excluded most heavy dirs, so
    we only need to name the few that might still appear in `dest`
    because the re-export preserved them (e.g. the destination's own
    ``data/`` with a pgdata/, ``.git/``, ``uploads/``).  We also skip
    common binary file types that can never match text triggers.
    """
    # Dirs most likely to exist in `dest` (preserved from prior run) and
    # whose contents we never sanitize anyway.
    heavy_dirs = ('.git', 'data', 'uploads', 'logs',
                  '.project_sessions', '.project_indexes', '.chatui',
                  'node_modules', '__pycache__', '.pytest_cache',
                  '.ruff_cache', 'swebench_workdir', 'swebench_full',
                  './paper', 'overleaf_cache', 'sundries')
    globs = [f'!{d}/**' for d in heavy_dirs] + [f'!{d}' for d in heavy_dirs]
    for ext in ('*.png', '*.jpg', '*.jpeg', '*.gif', '*.zip', '*.pdf',
                '*.db', '*.db-*', '*.log', '*.pyc', '*.pyo',
                '*.ttf', '*.woff', '*.woff2'):
        globs.append(f'!{ext}')
    return globs


def _redact_excluded_api_keys(dest: Path) -> bool:
    """Redact personal API-key literals from the exported export_pkg/_lists.py.

    Internal exports ship the toolchain (export_pkg/) so colleagues can
    re-export, but _lists.py carries the exporter's own personal keys in
    ``_INTERNAL_EXCLUDED_API_KEYS`` as the seed-time exclusion list — the
    very literals that list exists to keep OUT of shared trees.  Replace
    them with placeholders in the DEST copy only (source untouched).  The
    dest-side frozenset then holds only placeholders, which can never
    match a cloned repo's config keys, so re-export semantics are
    unchanged.  Personal mode never calls this (self-use backup keeps
    everything); opensource never ships export_pkg at all.
    """
    fpath = dest / 'export_pkg' / '_lists.py'
    if not fpath.is_file():
        return False
    try:
        content = fpath.read_text(encoding='utf-8')
    except OSError as e:
        logger.warning('Could not read %s for key redaction: %s', fpath, e)
        return False
    new = content
    for key in sorted(_INTERNAL_EXCLUDED_API_KEYS):
        new = new.replace(key, '<redacted-personal-key>')
    if new == content:
        return False
    try:
        fpath.write_text(new, encoding='utf-8')
    except OSError as e:
        logger.error('Could not write redacted %s: %s', fpath, e)
        return False
    return True


def _post_copy_sanitize(dest: Path, mode: str) -> dict:
    """Run the sanitizer on the destination tree, in place.

    Strategy:
      * Internal mode — `_sanitize_defaults_for_export` only modifies a
        SHORT, FIXED list of files (core.js, settings.js, model_config.py,
        index.html) plus any .html / .js file that has a ``?v=`` cache
        buster.  We build the candidate list directly (no rg needed).
      * Opensource mode — sanitization can touch secrets anywhere in the
        tree.  Use ``rg --files-with-matches`` to locate trigger files.
        Falls back to a Python walk if rg is absent or slow (e.g.
        pathological FUSE + parallel walker interaction — rg from Python
        subprocess can be dramatically slower than from an interactive
        shell on certain mounts; the Python fallback is predictable).

    Returns ``{'sanitized': n, 'scanned_files': m}``.
    """
    if mode not in ('internal', 'opensource'):
        return {'sanitized': 0, 'scanned_files': 0}

    if mode == 'internal':
        candidates = _collect_internal_candidates(dest)
    else:
        candidates = _collect_opensource_candidates(dest)

    sanitized = 0
    if mode == 'internal' and _redact_excluded_api_keys(dest):
        sanitized += 1
        print(f"  {C_YELLOW}\U0001f511{C_END} export_pkg/_lists.py "
              f"(personal key literals redacted)")
    for relpath in sorted(candidates):
        fpath = dest / relpath
        if not fpath.is_file() or not _is_text_file(str(fpath)):
            continue
        try:
            content = fpath.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            logger.warning('Could not read %s for sanitize: %s', relpath, e)
            continue
        new = _sanitize_defaults_for_export(content, relpath, version=_state._EXPORT_VERSION)
        if mode == 'opensource':
            new = _sanitize_source_opensource(new, relpath)
        if new != content:
            try:
                fpath.write_text(new, encoding='utf-8')
                sanitized += 1
                print(f"  {C_YELLOW}\U0001f527{C_END} {relpath}")
            except OSError as e:
                logger.error('Could not write sanitized %s: %s', relpath, e)
    return {'sanitized': sanitized, 'scanned_files': len(candidates)}


# ── Internal-mode candidate collection (no rg — fixed list) ──
#
# `_sanitize_defaults_for_export` only modifies content that matches one of:
#   - `.html` / `.js` files with a `?v=YYYYMMDD[a-z]` cache-buster substring
#   - the migrated frontend runtime (provider defaults + cache busters)
#   - model_config.py (server-side default thinking depth)
#   - index.html   (<select> default)
#
# The explicit filenames are known; cache-buster patterns live in any HTML/JS
def _collect_internal_candidates(dest: Path) -> set[str]:
    """Return the relative paths of files sanitize might touch in internal mode."""
    out: set[str] = set()
    # 1. Hardcoded whitelist.
    for rel in _INTERNAL_FIXED_CANDIDATES:
        if (dest / rel).is_file():
            out.add(rel)
    # 2. Frontend sources and emitted assets that may contain cache busters.
    for sub in ('frontend/src', 'static'):
        sub_dir = dest / sub
        if not sub_dir.is_dir():
            continue
        for p in sub_dir.rglob('*.js'):
            out.add(str(p.relative_to(dest)))
        for p in sub_dir.rglob('*.ts'):
            out.add(str(p.relative_to(dest)))
        for p in sub_dir.rglob('*.html'):
            out.add(str(p.relative_to(dest)))
    # Top-level HTML files (index.html) — already in fixed set but rglob above
    # misses them since they live at the root.
    for p in dest.glob('*.html'):
        out.add(str(p.relative_to(dest)))
    return out


def _collect_opensource_candidates(dest: Path) -> set[str]:
    """Locate files possibly needing opensource sanitization (secrets/endpoints)."""
    triggers = _opensource_sanitize_triggers()
    out = _rg_files_with_matches(
        dest, triggers, extra_globs=_rg_excluded_globs())
    for rel in _OPENSOURCE_FIXED_CANDIDATES:
        if (dest / rel).is_file():
            out.add(rel)
    return out
