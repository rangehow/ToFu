"""Existing LaTeX drafts: snapshot, expand, section, edit in place, diff.

Responsibility: everything the pipeline needs to revise a paper that already
exists instead of writing one from scratch.
  * ``snapshot``   copy the draft tree (sources, figures, styles; no build
                   products) into the pipeline workdir; the owner's files are
                   never written.
  * ``expand``     the main file with every ``\\input``/``\\include`` inlined,
                   for reading by the judge and the writer.
  * ``macros`` / ``expand_macros``  the paper's own macro definitions
                   (``\\newcommand`` and keyed ``\\csname`` tables such as
                   ``\\nm{key}``) and the text with them expanded, so judges
                   read numbers rather than unexpanded placeholders.
  * ``sections``   numbered body sections of the expanded text, keyed by a
                   stable slug of their title.
  * ``figures``    every ``\\includegraphics`` with its caption and label.
  * ``apply_edits`` targeted ``old → new`` replacements; each ``old`` must
                   occur exactly once across the draft's ``.tex`` files.
  * ``mark_body_end`` insert the page-limit label before the bibliography,
                   the appendix or the venue's unlimited unnumbered sections.
  * ``unified_patch`` the revision as a patch against the owner's draft.

Entry points: the functions above. Dependencies: standard library only.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import shutil
from pathlib import Path

__all__ = ['snapshot', 'expand', 'sections', 'figures', 'apply_edits',
           'mark_body_end', 'unified_patch', 'tex_files', 'slug', 'reachable_tex',
           'macros', 'expand_macros']

#: Build products and pipeline output never belong to the draft's sources.
_IGNORED = ('*.aux', '*.log', '*.fls', '*.fdb_latexmk', '*.synctex.gz', '*.blg',
            '*.bbl', '*.out', '*.toc', '*.lof', '*.lot', '__pycache__', '.git',
            '.pipeline', 'build')
_INPUT_RE = re.compile(r'\\(?:input|include)\s*\{([^}]+)\}')
_COMMENT_RE = re.compile(r'(?<!\\)%.*$', re.MULTILINE)


def snapshot(paper_dir: Path, destination: Path, *, exclude: tuple[str, ...] = ()) -> dict:
    """Copy the draft tree; return ``{relative path: sha256}``."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(paper_dir, destination,
                    ignore=shutil.ignore_patterns(*_IGNORED, *exclude))
    return {str(path.relative_to(destination)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(destination.rglob('*')) if path.is_file()}


def tex_files(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): path.read_text(encoding='utf-8', errors='replace')
            for path in sorted(root.rglob('*.tex')) if path.is_file()
            and not path.relative_to(root).parts[0].startswith('_official_sample')}


def _resolve(root: Path, base: Path, name: str) -> Path | None:
    for candidate in (base / name, root / name):
        for path in (candidate, candidate.with_name(candidate.name + '.tex')):
            if path.is_file():
                return path
    return None


def expand(root: Path, main: str, *, _seen: frozenset = frozenset()) -> str:
    """Main file text with ``\\input``/``\\include`` inlined (comments kept out)."""
    path = root / main
    text = _COMMENT_RE.sub('', path.read_text(encoding='utf-8', errors='replace'))

    def inline(match):
        target = _resolve(root, path.parent, match.group(1).strip())
        if target is None or target in _seen:
            return match.group(0)
        return expand(root, str(target.relative_to(root)), _seen=_seen | {target})
    return _INPUT_RE.sub(inline, text)


_BRACED = r'\{((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*)\}'
_NEWCOMMAND_RE = re.compile(
    r'\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*'
    r'(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))\s*(?:\[(\d)\])?\s*(\[[^\]]*\])?\s*' + _BRACED)
_DEF_RE = re.compile(r'\\(?:g|e|x)?def\s*\\([A-Za-z@]+)\s*' + _BRACED)
_CSNAME_DEF_RE = re.compile(
    r'\\(?:expandafter\s*)?\\(?:g|e|x)?def\s*\\csname\s*(.*?)\s*\\endcsname\s*' + _BRACED)
_CSNAME_LOOKUP_RE = re.compile(r'\\csname\s*([^\\#]*)#1\s*\\endcsname')


def macros(text: str) -> dict:
    """The document's own macro definitions.

    Returns ``{'commands': {name: (arity, body)}, 'tables': {csname: value},
    'lookups': {name: prefix}}``; a one-argument command whose body looks its
    argument up as ``\\csname <prefix>#1\\endcsname`` (the generated number
    tables of many papers) is a lookup into ``tables``. Commands with an
    optional default argument or a self-reference are not expanded.
    """
    commands, lookups = {}, {}
    for match in _NEWCOMMAND_RE.finditer(text):
        name = match.group(1) or match.group(2)
        arity, default, body = int(match.group(3) or 0), match.group(4), match.group(5)
        lookup = _CSNAME_LOOKUP_RE.search(body) if arity == 1 else None
        if lookup:
            lookups[name] = lookup.group(1)
        elif not default and '\\if' not in body and not re.search(
                r'\\' + name + r'(?![A-Za-z@])', body):
            commands[name] = (arity, body)
    for match in _DEF_RE.finditer(text):
        name, body = match.group(1), match.group(2)
        if '\\if' not in body and not re.search(r'\\' + name + r'(?![A-Za-z@])', body):
            commands.setdefault(name, (0, body))
    tables = {name: value for name, value in _CSNAME_DEF_RE.findall(text)}
    return {'commands': commands, 'tables': tables, 'lookups': lookups}


def expand_macros(text: str, defined: dict) -> str:
    """``text`` with the document's own macros expanded (reading view only).

    Every pass expands one nesting level; with no cyclic definitions the
    nesting depth is at most the number of macros, which bounds the passes.
    A lookup whose key the table lacks is left as written.
    """
    commands, tables, lookups = defined['commands'], defined['tables'], defined['lookups']
    names = sorted(set(commands) | set(lookups), key=len, reverse=True)
    if not names:
        return text
    pattern = re.compile(r'\\(' + '|'.join(map(re.escape, names)) + r')(?![A-Za-z@])'
                         r'((?:\s*\{(?:[^{}]|\{[^{}]*\})*\})*)(\{\})?')

    def replace(match):
        name, raw_args = match.group(1), match.group(2)
        args = re.findall(r'\{((?:[^{}]|\{[^{}]*\})*)\}', raw_args)
        if name in lookups:
            if not args:
                return match.group(0)
            key = lookups[name] + expand_macros(args[0], defined).strip()
            if key not in tables:
                return match.group(0)
            rest = ''.join('{' + arg + '}' for arg in args[1:])
            return tables[key] + rest + (match.group(3) or '')
        arity, body = commands[name]
        if arity == 0 and args and args[0] == '':
            args = args[1:]   # ``\\name{}`` only ends the control word
        if len(args) < arity:
            return match.group(0)
        for index in range(arity):
            body = body.replace(f'#{index + 1}', args[index])
        rest = ''.join('{' + arg + '}' for arg in args[arity:])
        return body + rest
    for _ in range(len(names) + 1):
        expanded = pattern.sub(replace, text)
        if expanded == text:
            break
        text = expanded
    return text


def reachable_tex(root: Path, main: str) -> set[str]:
    """Relative paths of ``main`` and every ``.tex`` it inputs, transitively."""
    seen: set[Path] = set()
    pending = [root / main]
    while pending:
        path = pending.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        text = _COMMENT_RE.sub('', path.read_text(encoding='utf-8', errors='replace'))
        for name in _INPUT_RE.findall(text):
            target = _resolve(root, path.parent, name.strip())
            if target is not None:
                pending.append(target)
    return {str(path.relative_to(root)) for path in seen}


def slug(title: str) -> str:
    text = re.sub(r'\\[a-zA-Z]+\*?(\{[^}]*\})?', ' ', title)
    return re.sub(r'[^a-z0-9]+', '_', text.casefold()).strip('_') or 'section'


_SECTION_RE = re.compile(r'\\section\{((?:[^{}]|\{[^{}]*\})*)\}')
_BODY_END_RE = re.compile(r'\\bibliography\{|\\begin\{thebibliography\}|\\appendix\b|'
                          r'\\printbibliography')


def sections(expanded: str) -> list[dict]:
    """Numbered body sections before the bibliography/appendix, in order."""
    body = expanded[:(_BODY_END_RE.search(expanded) or re.search(r'$', expanded)).start()]
    start = re.search(r'\\begin\{document\}', body)
    begin = start.end() if start else 0
    matches = list(_SECTION_RE.finditer(body, begin))
    rows, seen = [], set()
    abstract = re.search(r'\\begin\{abstract\}(.*?)\\end\{abstract\}', body, re.DOTALL)
    if abstract:
        rows.append({'id': 'abstract', 'title': 'Abstract', 'text': abstract.group(1).strip()})
        seen.add('abstract')
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sid = slug(match.group(1))
        while sid in seen:
            sid += '_'
        seen.add(sid)
        rows.append({'id': sid, 'title': match.group(1).strip(),
                     'text': body[match.end():end].strip()})
    return rows


_FIGURE_RE = re.compile(r'\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}', re.DOTALL)


def figures(expanded: str) -> list[dict]:
    rows = []
    for block in _FIGURE_RE.findall(expanded):
        for file in re.findall(r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', block):
            caption = re.search(r'\\caption\{((?:[^{}]|\{[^{}]*\})*)\}', block)
            label = re.search(r'\\label\{([^}]+)\}', block)
            rows.append({'file': file, 'caption': caption.group(1) if caption else '',
                         'label': label.group(1) if label else ''})
    return rows


def apply_edits(files: dict[str, str], edits: list[dict]) -> tuple[dict, list, list]:
    """Apply ``{old, new}`` edits; return (new files, applied, rejected)."""
    current = dict(files)
    applied, rejected = [], []
    for edit in edits:
        old, new = str(edit.get('old') or ''), str(edit.get('new') or '')
        if not old.strip() or old == new:
            rejected.append({**edit, 'reason': 'empty or unchanged edit'})
            continue
        hits = [(rel, text.count(old)) for rel, text in current.items() if old in text]
        total = sum(count for _rel, count in hits)
        if total != 1:
            rejected.append({**edit, 'reason': f'old text occurs {total} times across the draft '
                                               '(must be exactly once)'})
            continue
        rel = hits[0][0]
        current[rel] = current[rel].replace(old, new, 1)
        applied.append({**edit, 'file': rel})
    return current, applied, rejected


def mark_body_end(main_text: str, label: str, unlimited_sections: tuple[str, ...]) -> str:
    """Insert ``\\label{label}`` where the page-limited body ends."""
    # Exact words at any heading level, in any case: ICLR's official
    # statements are \subsection*{AI use statement}; a renamed statement
    # ("AI Disclosure Statement") is not exempt.
    patterns = [_BODY_END_RE.pattern] + [
        r'(?i:\\(?:sub){0,2}section\*?\{\s*' + re.escape(title) + r'\s*\})'
        for title in unlimited_sections]
    found = [match.start() for pattern in patterns
             for match in [re.search(pattern, main_text)] if match]
    if not found:
        found = [main_text.rfind('\\end{document}')]
    at = min(pos for pos in found if pos >= 0)
    # Attached to the last counted text, the label is set on the page where
    # that text ends; on its own line it would move with the next heading.
    end = len(main_text[:at].rstrip())
    return main_text[:end] + f'\\label{{{label}}}' + main_text[end:]


def unified_patch(original: dict[str, str], revised: dict[str, str]) -> str:
    chunks = []
    for rel in sorted(set(original) | set(revised)):
        before, after = original.get(rel, ''), revised.get(rel, '')
        if before == after:
            continue
        chunks.extend(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f'a/{rel}', tofile=f'b/{rel}'))
    return ''.join(chunks)
