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
    'js_function_body',
    'playwright_install_invocations',
]

#: Line-comment markers per language family.
#:
#: The ``js`` family also lists ``*`` so that the CONTINUATION lines of a
#: ``/* … */`` banner (conventionally ``   * …``) are blanked as whole-line
#: comments; the block delimiters themselves are handled by the block pass in
#: :func:`strip_comments`.
_LINE_COMMENT_PREFIXES = {
    'shell': ('#',),
    'python': ('#',),
    'js': ('//', '*'),
}

#: Languages whose ``/* … */`` block comments :func:`strip_comments` removes.
#:
#: Opt-in per language, and deliberately WHOLE-LINE only (see the function's
#: docstring): a half-correct block parser that tried to handle ``/*`` inside a
#: string literal or a regex would silently drop real code, which is worse than
#: not stripping at all.
_BLOCK_COMMENT_LANGS = frozenset({'js'})


def strip_comments(text, lang='shell'):
    """Return ``text`` with whole-line comments removed, line count preserved.

    Lines are blanked rather than deleted so that any line-number arithmetic a
    caller does afterwards still lines up with the original file.

    Only WHOLE-LINE comments are stripped — a trailing ``# …`` after real code
    is left alone, because in shell a ``#`` can appear inside a quoted string
    and this module refuses to half-parse quoting. Guards that need to ignore
    trailing comments should match a more specific command shape instead.

    For ``lang='js'`` a ``/* … */`` BLOCK pass runs first, again whole-line
    only: a line is blanked while it sits inside a block that OPENED on its own
    line. A ``/*`` that appears after code on the same line is ignored rather
    than half-parsed, for the same reason trailing ``#`` is left alone — it
    could be inside a string literal or a regex, and dropping real code is a
    worse failure than leaving a comment in.

    Args:
        text: Full source text.
        lang: ``'shell'`` / ``'python'`` (both ``#``), or ``'js'``
            (``//``, ``*`` continuations, and ``/* … */`` blocks).

    Returns:
        The text with comment-only lines replaced by empty lines.
    """
    prefixes = _LINE_COMMENT_PREFIXES.get(lang, ('#',))
    strip_blocks = lang in _BLOCK_COMMENT_LANGS
    out = []
    in_block = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if strip_blocks:
            if in_block:
                # Still inside a block comment: blank the line, and close the
                # block when this line carries the terminator.
                if '*/' in line:
                    in_block = False
                out.append('')
                continue
            if stripped.startswith('/*'):
                # A one-line ``/* … */`` closes immediately; otherwise the
                # block stays open for the lines that follow.
                if '*/' not in stripped[2:]:
                    in_block = True
                out.append('')
                continue
        out.append('' if stripped.startswith(prefixes) else line)
    return '\n'.join(out)


def js_function_body(text, name, lang='js'):
    """Return the FULL body of JS ``function <name>`` by brace matching.

    WHY THIS EXISTS (incident 4 of the family in the module docstring)
    -----------------------------------------------------------------
    Two guards in ``tests/test_conv_state_p6_verdict.py`` extracted a function
    body as a FIXED ``src[start:start + 4000]`` byte slice. That is wrong in
    both directions and silently so:

      * OVERSHOOT — measured on the live tree, the 4000-byte window past
        ``_reconcileStuckActiveTaskPins`` (real body 2999 B) swallowed 1001
        bytes of ``_reconcileIntervalMs`` + ``_crossDeviceReconcile``, and the
        window past ``applyConvStateSnapshot`` (real body 3391 B) swallowed 609
        bytes of the next declaration. A guard asserting "token X must NOT
        appear in this function" therefore fails on a NEIGHBOUR's use of X —
        an accusation pointed at innocent code.
      * TRUNCATION — the mirror failure once a function grows past the window:
        the tail stops being scanned, so a real violation added at the end of a
        long function is silently not seen. That direction is worse, because it
        is invisible: the guard just goes quiet.

    Brace matching removes the constant. Comments and string literals are
    stripped/neutralised BEFORE counting so a ``{`` inside a comment or a
    quoted string cannot unbalance the scan — the same discipline
    :func:`strip_comments` exists to enforce, applied to a different question.

    Args:
        text: Full source text of the file.
        name: Function name, as it appears after the ``function`` keyword.
        lang: Comment family for the pre-strip (default ``'js'``).

    Returns:
        Source of the function from the ``function`` keyword through its
        matching closing brace, comments blanked (line count preserved).

    Raises:
        AssertionError: if the function is absent, or its braces never balance
            (a caller asserting on a body must never silently receive a
            partial one).
    """
    live = strip_comments(text, lang=lang)
    needle = 'function ' + name
    start = live.find(needle)
    assert start != -1, (
        'function %s not found in the scanned source (it may have been '
        'renamed or deleted — a guard must not silently scan nothing)' % name)

    open_at = live.find('{', start)
    assert open_at != -1, 'no opening brace after function %s' % name

    # Neutralise string/template literals so braces inside them do not count.
    # Comments are already blanked by strip_comments above.
    depth = 0
    i = open_at
    quote = None
    while i < len(live):
        ch = live[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'`':
            quote = ch
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return live[start:i + 1]
        i += 1
    raise AssertionError(
        'braces never balanced for function %s — refusing to return a partial '
        'body, which would make any "token not in body" assertion vacuous'
        % name)


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
