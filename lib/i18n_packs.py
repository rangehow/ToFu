"""lib/i18n_packs.py — build-time extraction of single-language i18n packs.

WHY THIS EXISTS
---------------
Epic-E sub-part 1 ships ONE dictionary carrying both zh and en (measured:
344.0 KB raw -> 89.8 KB brotli; a zh-only variant is 59.2 KB, i.e. 30.6 KB =
7.6% of the compressed first-paint payload — tests/test_i18n_split_sizing.py).
Slice 1 (landed) made the language server-visible via a cookie. This module is
the ATOM slice 2 needs: turning the one dual-language source into two
single-language packs, provably without losing or altering a single string.

WHY NODE AND NOT A REGEX
------------------------
The obvious implementation — regex the ``{ zh: '…', en: '…' }`` literals — is
wrong, and quietly so. The dictionary contains apostrophes, escaped quotes,
braces inside interpolation placeholders, and 378 comment lines. A regex that
is 99% right silently drops the 1% it cannot parse, and a MISSING key does not
throw: ``t()`` falls back to zh (see i18n.js), so an English UI would just
render Chinese for the dropped keys. That is the exact no-failure-signal
defect class this epic already fixed once at the ``t()`` level.

So the extraction EXECUTES the real file in node and reads the resulting
object. Whatever JavaScript thinks ``_i18n`` is, that is what we serialize —
the parser can never disagree with the runtime.

WHAT THIS MODULE DOES *NOT* DO
------------------------------
It does not touch what is served. Nothing here is wired into
``build_bundle()`` yet. Slice 2 does that, and it must not start until this
extraction is proven exact, because a pack that silently drops keys is
invisible in production.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'SUPPORTED_LANGS',
    'extract_dictionary',
    'build_pack_source',
    'verify_pack_roundtrip',
]

# Kept in lockstep with routes/common.py::_UI_LANGS. A pack is only ever
# emitted for a language the server is willing to select.
SUPPORTED_LANGS = ('zh', 'en')

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_SOURCE = os.path.join(_REPO, 'static', 'js', 'i18n.js')

# Minimal browser surface i18n.js touches at load: it reads localStorage for
# the language, writes a cookie, and _applyI18n walks the DOM. Stubs are inert
# so loading the file has no side effects beyond defining _i18n.
_DOM_STUB = """
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = {
  documentElement: {},
  querySelectorAll: () => [],
  addEventListener: () => {},
  readyState: 'complete',
  get cookie() { return ''; },
  set cookie(v) {},
};
"""


class PackExtractionError(RuntimeError):
    """Raised when the dictionary cannot be extracted or fails verification."""


def _node() -> str:
    exe = shutil.which('node')
    if not exe:
        raise PackExtractionError('node is required to extract i18n packs')
    return exe


def _run_node(script: str, timeout: int = 120) -> str:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([_node(), path], capture_output=True,
                              text=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError as e:
            logger.debug('[i18nPacks] temp cleanup failed: %s', e)
    if proc.returncode != 0:
        raise PackExtractionError(f'node failed: {proc.stderr[:800]}')
    return proc.stdout


def extract_dictionary(source_path: str | None = None) -> dict:
    """Return the real ``_i18n`` object by EXECUTING i18n.js under node.

    Args:
        source_path: Override for the i18n.js path (tests).

    Returns:
        ``{key: {lang: text, ...}, ...}`` exactly as JavaScript sees it.

    Raises:
        PackExtractionError: node missing, file unreadable, or output not JSON.
    """
    path = source_path or I18N_SOURCE
    with open(path, encoding='utf-8') as f:
        src = f.read()

    out = _run_node(_DOM_STUB + src + "\nconsole.log('@@' + JSON.stringify(_i18n));\n")
    marker = [l for l in out.splitlines() if l.startswith('@@')]
    if not marker:
        raise PackExtractionError(f'no dictionary emitted; stdout tail: {out[-300:]}')
    try:
        data = json.loads(marker[-1][2:])
    except json.JSONDecodeError as e:
        raise PackExtractionError(f'dictionary is not valid JSON: {e}') from e
    if not isinstance(data, dict) or not data:
        raise PackExtractionError('extracted dictionary is empty or not an object')
    return data


def build_pack_source(dictionary: dict, lang: str) -> str:
    """Render a single-language pack as JavaScript source.

    The entry SHAPE is preserved — ``{key: {lang: text}}``, not
    ``{key: text}`` — so ``t()``'s ``entry[_i18nLang]`` lookup keeps working
    against a pack with zero changes to the accessor.

    Keys whose entry lacks *lang* are OMITTED rather than filled from another
    language. Omitting is the honest representation of "this pack does not
    have it", and ``t()``'s tripwire (``_reportMissingTranslation``, landed in
    4fbab4fa) makes that omission audible at runtime. Filling silently would
    reintroduce exactly the defect this epic removed.

    Args:
        dictionary: Output of :func:`extract_dictionary`.
        lang: One of :data:`SUPPORTED_LANGS`.

    Returns:
        JavaScript source declaring ``var _i18n = { … };``.
    """
    if lang not in SUPPORTED_LANGS:
        raise PackExtractionError(f'unsupported language {lang!r}')

    lines = [
        f'/* GENERATED — single-language i18n pack ({lang}). Do not edit.',
        ' * Source: static/js/i18n.js (extracted by lib/i18n_packs.py).',
        " * Entry shape is preserved as {key: {lang: text}} so t()'s",
        ' * entry[_i18nLang] lookup is unchanged. Keys missing this language',
        ' * are OMITTED, never filled from another language — the runtime',
        ' * tripwire reports them instead of silently rendering the wrong one.',
        ' */',
        'var _i18n = {',
    ]
    omitted = 0
    for key in dictionary:  # insertion order == source order
        entry = dictionary[key]
        if not isinstance(entry, dict) or lang not in entry:
            omitted += 1
            continue
        lines.append(f'{json.dumps(key)}:{{{lang}:{json.dumps(entry[lang], ensure_ascii=False)}}},')
    lines.append('};')
    if omitted:
        logger.info('[i18nPacks] %s pack omits %d key(s) lacking that language',
                    lang, omitted)
    return '\n'.join(lines) + '\n'


def verify_pack_roundtrip(dictionary: dict, pack_src: str, lang: str) -> dict:
    """Prove a rendered pack reproduces the source strings EXACTLY.

    Executes the generated pack under node and compares every key/value
    against *dictionary*. This is the gate that must pass before slice 2 may
    serve a pack, because the failure mode it guards (a dropped key) produces
    no runtime error — only the wrong language on screen.

    Returns:
        ``{'keys': int, 'missing': [...], 'mismatched': [...], 'extra': [...]}``
        with all three lists empty on success.

    Raises:
        PackExtractionError: the pack is not valid JS / emits no object.
    """
    out = _run_node(pack_src + "\nconsole.log('@@' + JSON.stringify(_i18n));\n")
    marker = [l for l in out.splitlines() if l.startswith('@@')]
    if not marker:
        raise PackExtractionError('generated pack emitted no dictionary')
    got = json.loads(marker[-1][2:])

    expected = {k: v[lang] for k, v in dictionary.items()
                if isinstance(v, dict) and lang in v}

    missing = [k for k in expected if k not in got]
    extra = [k for k in got if k not in expected]
    mismatched = [
        k for k in expected
        if k in got and got[k].get(lang) != expected[k]
    ]
    return {
        'keys': len(expected),
        'missing': missing,
        'mismatched': mismatched,
        'extra': extra,
    }
