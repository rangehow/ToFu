"""tests/_source_scan.py — shared source-text scanning primitives for guards.

WHY THIS EXISTS (the rule, learned three times the hard way)
-----------------------------------------------------------
**Any guard that scans SOURCE TEXT must strip comments first.** This is a fixed
precondition for writing that kind of guard, not a patch to apply after it
misfires. Measured history, three separate incidents of the same shape:

  1. The QR batch: a test harness faked ``t()`` with ``(k, d) => (d || k)``,
     inverting the real semantics, so 11 render guards passed on a defect that
     shipped a literal ``project.qrScan`` to users.
  2. The ``--only-shell`` ratchet in ``test_install_uv_fastpath.py``: a comment
     documenting the recovery command ``python -m playwright install chromium``
     was matched as a real invocation, turning a correct tree red.
  3. ``test_chromium_binary_resolution.py`` (the SAME ratchet in a second
     implementation, which is the actual root cause): incident 2 was fixed in
     one file only, so the duplicate kept the bug and went red on the next
     comment that mentioned the command.

Incident 3 is the reason this module exists rather than a second local fix: two
copies of "what counts as a real invocation" will always drift. Both guards now
call :func:`strip_comments` / :func:`playwright_install_invocations`, so the
definition lives once.

A comment must never be able to SATISFY a guard, and must never be able to
VIOLATE one either. Both directions matter: the first makes a guard vacuous, the
second makes it a false alarm that trains people to ignore it.
"""

import re

__all__ = [
    'strip_comments',
    'playwright_install_invocations',
]

#: Line-comment markers per language family. Block comments are deliberately
#: NOT handled: no guard here needs them, and a half-correct block-comment
#: parser is worse than none (it would silently drop real code).
_LINE_COMMENT_PREFIXES = {
    'shell': ('#',),
    'python': ('#',),
}


def strip_comments(text, lang='shell'):
    """Return ``text`` with whole-line comments removed, line count preserved.

    Lines are blanked rather than deleted so that any line-number arithmetic a
    caller does afterwards still lines up with the original file.

    Only WHOLE-LINE comments are stripped — a trailing ``# …`` after real code
    is left alone, because in shell a ``#`` can appear inside a quoted string
    and this module refuses to half-parse quoting. Guards that need to ignore
    trailing comments should match a more specific command shape instead.

    Args:
        text: Full source text.
        lang: ``'shell'`` or ``'python'`` (both use ``#``).

    Returns:
        The text with comment-only lines replaced by empty lines.
    """
    prefixes = _LINE_COMMENT_PREFIXES.get(lang, ('#',))
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        out.append('' if stripped.startswith(prefixes) else line)
    return '\n'.join(out)


#: A real ``playwright install`` browser-download invocation.
#:
#: ``install-deps`` is a DIFFERENT subcommand (it apt-installs system libs and
#: accepts no ``--only-shell``), so it is excluded. Requiring the browser name
#: after optional flags is what separates a command from prose such as the WARN
#: line "playwright install failed; JS-page fetch will degrade".
_PLAYWRIGHT_INSTALL_RE = re.compile(
    r'playwright install(?!-deps)((?:\s+-[^\s|&>]+)*)\s+chromium\b')


def playwright_install_invocations(text, lang='shell'):
    """Return every REAL ``playwright install … chromium`` invocation in ``text``.

    Comments are stripped first (see the module docstring). This is the single
    definition of "a real invocation" shared by the ``--only-shell`` ratchets in
    tests/test_install_uv_fastpath.py and
    tests/test_chromium_binary_resolution.py — previously two hand-written
    copies that drifted, which is how a comment-induced false alarm survived a
    fix applied to only one of them.

    Returns:
        List of matched invocation strings, whitespace-trimmed.
    """
    live = strip_comments(text, lang=lang)
    return [m.group(0).strip() for m in _PLAYWRIGHT_INSTALL_RE.finditer(live)]
