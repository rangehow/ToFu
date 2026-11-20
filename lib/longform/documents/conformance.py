"""Template conformance gate: proves a rendered paper uses its venue edition's
official template unmodified and obeys that edition's rules.

Responsibility: ``check_paper`` copies a paper directory to scratch (the
paper itself is never written), marks where the page-limited body ends,
compiles it with a layout probe, and returns one row per check:

  * ``compile``: the scratch build produced a PDF without TeX errors;
  * ``template.official-files``: from the LaTeX recorder (``.fls``), the
    edition's style file was loaded and every venue-owned style file the
    build loaded has the pinned sha256; a style file of another year (by the
    venue's name pattern or by the hash of another pinned edition) fails;
  * ``template.no-overrides``: no source command overrides the kit's
    geometry, fonts or spacing (``\\geometry``, ``\\setlength{\\textheight}``,
    ``\\linespread``, ``savetrees`` ...);
  * ``template.layout``: page size, text block, margins, columns, body font
    size/family and baseline at the start and end of the document equal the
    official sample's, measured by the same probe when the edition was pinned;
  * ``template.mode``: a submission build uses the review mode (no
    camera-ready option or command);
  * ``page-limit``: the body ends within the stage's page limit, exempt
    sections counted by their exact official headings;
  * ``anonymity`` (double-blind submission): the template's anonymous author
    block is printed, no named identity, e-mail address or PDF author
    metadata, no acknowledgements;
  * ``required-sections``: each required heading of the edition, stage and
    track is present by its official words, checklists have no TODO answers.

Entry points: ``check_paper``, ``measure_layout``, ``PROBE_PRETEX``.
Dependencies: ``lib.paper.latex_layout.compile_project`` (toolchain owner),
``lib.longform.documents.venue_registry`` (pinned editions).
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from pathlib import Path

__all__ = ['check_paper', 'measure_layout', 'PROBE_PRETEX', 'BODY_END_MACRO',
           'mark_body_end_in_tree', 'layout_overrides', 'loaded_files']

BODY_END_MACRO = 'TofuConformanceBodyEnd'
PROBE_FILE_SUFFIX = '.tofulayout'
_LAYOUT_KEYS = ('paperwidth', 'paperheight', 'textwidth', 'textheight', 'columnwidth',
                'columnsep', 'oddsidemargin', 'evensidemargin', 'topmargin', 'headheight',
                'headsep', 'footskip', 'hoffset', 'voffset', 'fontsize', 'baselineskip',
                'fontfamily', 'twocolumn')

# Written before \documentclass (latexmk -usepretex). The probe reads the
# effective layout after the whole preamble and again after the last page;
# the body-end macro records the absolute page at shipout.
PROBE_PRETEX = (
    r'\makeatletter'
    r'\newwrite\tofu@probe'
    r'\newif\iftofu@pastbody'
    # Deferred writes travel with standard float boxes to their actual shipout.
    r'\AddToHook{cmd/@endfloatbox/before}{\iftofu@pastbody\else'
    r'\immediate\write\tofu@probe{bodyfloat;}'
    r'\write\tofu@probe{bodyfloatpage=\the\ReadonlyShipoutCounter;}\fi}'
    r'\def\tofu@dump#1{\immediate\write\tofu@probe{#1'
    + ''.join(f' {key}=' + {'fontsize': r'\f@size', 'fontfamily': r'\f@family',
                              'twocolumn': r'\if@twocolumn 1\else 0\fi'}.get(
                                  key, '\\the\\' + key) + ';' for key in _LAYOUT_KEYS)
    + ''.join(' track-' + name + r'=\@ifundefined{if@' + name
              + r'}{unknown}{\csname if@' + name + r'\endcsname 1\else 0\fi};'
              for name in ('main', 'eandd', 'position', 'creativeai', 'education', 'workshop'))
    + r'}}'
    r'\AddToHook{begindocument/end}{\immediate\openout\tofu@probe=\jobname'
    + PROBE_FILE_SUFFIX + r'\relax\tofu@dump{begin}}'
    r'\AddToHook{enddocument/afterlastpage}{\tofu@dump{end}\immediate\closeout\tofu@probe}'
    r'\protected\def\TofuConformanceBodyEnd{\global\tofu@pastbodytrue'
    r'\write\tofu@probe{bodyend='
    r'\the\ReadonlyShipoutCounter;}}'
    r'\makeatother')


# ── source helpers ──────────────────────────────────────────────────────────

_INPUT_RE = re.compile(r'\\(?:input|include)\s*\{([^}]+)\}')


def _mask_comments(text: str) -> str:
    """Same length, comments blanked, so offsets map back to the raw text."""
    return re.sub(r'(?<!\\)%[^\n]*', lambda m: ' ' * len(m.group(0)), text)


def _resolve(root: Path, base: Path, name: str) -> Path | None:
    for candidate in (base / name, root / name):
        for path in (candidate, candidate.with_name(candidate.name + '.tex')):
            if path.is_file():
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f'input {name!r} escapes the scratch build; '
                                     'read-only audit refuses external TeX inputs')
                return path
    return None


def _body_end_pattern(exempt: tuple[str, ...]) -> re.Pattern:
    parts = [r'\\bibliography\{', r'\\begin\{thebibliography\}', r'\\appendix\b',
             r'\\printbibliography'] + [
        r'(?i:\\(?:sub){0,2}section\*?\{\s*' + re.escape(title) + r'\s*\})'
        for title in exempt]
    return re.compile('|'.join(parts))


_PAGE_BREAK = re.compile(r'\\(?:clearpage|cleardoublepage|newpage|pagebreak)(?![A-Za-z@])')
_OPTION_START = re.compile(r'[ \t\r\n]*\[')


def _break_end(text: str, match: re.Match) -> int | None:
    """End of a page-break command, consuming ``\\pagebreak``'s optional argument.

    LaTeX reads that argument as a delimited ``[#1]`` token list: spaces,
    newlines, masked comments and balanced brace groups may sit between the
    brackets, and a control symbol such as ``\\]`` is one token. ``None``
    means the argument's extent cannot be established, so the caller must not
    treat whatever follows as printed body text.
    """
    if match.group(0) != '\\pagebreak':
        return match.end()
    start = _OPTION_START.match(text, match.end())
    if start is None:
        return match.end()
    depth = 0
    pos = start.end() - 1          # at '['
    while pos < len(text):
        char = text[pos]
        if char == '\\':
            pos += 2               # control symbol: one token
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            if depth == 0:
                return None
            depth -= 1
        elif char == ']' and depth == 0:
            return pos + 1
        pos += 1
    return None


def mark_body_end_in_tree(root: Path, main: str, exempt: tuple[str, ...]) -> str:
    """Insert the body-end macro after the last counted text of the tree, in
    whichever input file holds it; returns ``file:offset`` of the insertion."""
    if not (root / main).resolve().is_relative_to(root.resolve()):
        raise ValueError('main must be inside the scratch build')
    pattern = _body_end_pattern(exempt)
    last: list = [None]          # (path, offset just after the last counted text)
    seen: set[Path] = set()
    ambiguous_tail = False

    def walk(path: Path) -> bool:
        nonlocal ambiguous_tail
        if path in seen:
            return False
        seen.add(path)
        raw = path.read_text(encoding='utf-8', errors='replace')
        masked = _mask_comments(raw)
        start = re.search(r'\\begin\{document\}', masked)
        pos = start.end() if start and path == root / main else 0
        while True:
            end = pattern.search(masked, pos)
            include = _INPUT_RE.search(masked, pos)
            stop = min((m.start() for m in (end, include) if m), default=len(masked))
            segment = masked[pos:stop].rstrip()
            if segment.strip():
                breaks = list(_PAGE_BREAK.finditer(segment))
                break_end = _break_end(segment, breaks[-1]) if breaks else None
                if breaks:
                    tail = '' if break_end is None else segment[break_end:].lstrip()
                    ambiguous_tail = (break_end is None or (
                        not tail or tail.startswith(('\\', '{', '}')))
                        and not re.match(r'\\begin\s*\{(?:figure|table)\*?\}', tail))
                elif not segment.lstrip().startswith(('\\', '{', '}')) or re.match(
                        r'\s*\\begin\s*\{(?:figure|table)\*?\}', segment):
                    ambiguous_tail = False
                last[0] = (path, pos + len(segment))
            if end and (not include or end.start() <= include.start()):
                return True
            if not include:
                return False
            target = _resolve(root, path.parent, include.group(1).strip())
            if target is not None and walk(target):
                return True
            pos = include.end()

    found = walk(root / main)
    if not found:
        # No references/appendix/exempt heading: the body runs to \end{document}.
        main_raw = (root / main).read_text(encoding='utf-8', errors='replace')
        at = _mask_comments(main_raw).rfind('\\end{document}')
        if at > 0:
            last[0] = (root / main, len(main_raw[:at].rstrip()))
    # Never trim groups to move a marker before a break: they may be macro arguments.
    if last[0] is None or ambiguous_tail:
        return ''
    path, offset = last[0]
    raw = path.read_text(encoding='utf-8', errors='replace')
    path.write_text(raw[:offset] + '\\' + BODY_END_MACRO + '{}' + raw[offset:],
                    encoding='utf-8')
    return f'{path.relative_to(root)}:{offset}'


def expanded_source(root: Path, main: str) -> str:
    from lib.longform.documents.existing_draft import expand
    return expand(root, main)


_OVERRIDE_RULES = (
    (r'\\usepackage(?:\[(?![^\]]*\bpass\b)[^\]]*\])?\{[^}]*\bgeometry\b[^}]*\}',
     'loads geometry (changes the kit\'s margins)'),
    (r'\\(?:new)?geometry\s*\{', 'sets page geometry'),
    (r'\\(?:setlength|addtolength)\s*\{?\s*\\(textwidth|textheight|oddsidemargin|'
     r'evensidemargin|topmargin|headheight|headsep|footskip|columnsep|hoffset|voffset|'
     r'paperwidth|paperheight|baselineskip|topskip|parskip|abovedisplayskip|'
     r'belowdisplayskip|abovedisplayshortskip|belowdisplayshortskip|abovecaptionskip|'
     r'belowcaptionskip|textfloatsep|floatsep|intextsep|dbltextfloatsep|dblfloatsep)\b',
     'changes a kit dimension'),
    (r'\\(?:textwidth|textheight|oddsidemargin|evensidemargin|topmargin|columnsep|'
     r'baselineskip|textfloatsep|floatsep|intextsep)\s*=?\s*-?[\d.]+\s*(?:pt|in|cm|mm|em)',
     'assigns a kit dimension'),
    (r'\\linespread\s*\{', 'changes line spacing'),
    (r'\\(?:renewcommand\s*\{?\s*|def\s*)\\baselinestretch', 'changes line spacing'),
    (r'\\renewcommand\s*\{?\s*\\(?:normalsize|small|footnotesize)\b',
     'redefines a kit font size'),
    (r'\\usepackage(?:\[[^\]]*\])?\{[^}]*\b(savetrees|fullpage|setspace|titlesec|'
     r'layout|vmargin|anysize|typearea)\b[^}]*\}', 'loads a layout-changing package'),
    (r'\\titlespacing\b', 'changes heading spacing'),
    (r'\\documentclass\s*\[[^\]]*\b(?:9|11|12)pt\b', 'changes the class font size'),
)


def layout_overrides(text: str, allowed: tuple[str, ...] = (),
                     body_end: int | None = None) -> list[str]:
    """Each source command that overrides the kit's geometry, fonts or
    spacing, with its line; ``allowed`` lists exact commands the official
    sample itself uses (e.g. ``\\documentclass[11pt]{article}`` for ACL).
    Commands after ``body_end`` (appendix, back matter) are tagged
    ``after the page-limited body``."""
    found = []
    preamble_end = text.find('\\begin{document}')
    for pattern, why in _OVERRIDE_RULES:
        for match in re.finditer(pattern, text):
            snippet = match.group(0)
            if any(snippet.startswith(ok) or ok.startswith(snippet) for ok in allowed):
                continue
            line = text.count('\n', 0, match.start()) + 1
            where = ('preamble' if preamble_end < 0 or match.start() < preamble_end else
                     'after the page-limited body' if body_end is not None
                     and match.start() >= body_end else 'body')
            found.append(f'{why}: {snippet!r} (expanded source line {line}, {where})')
    preamble = text[:preamble_end] if preamble_end >= 0 else text
    for match in re.finditer(r'\\fontsize\s*\{[^}]*\}\s*\{[^}]*\}\s*\\selectfont', preamble):
        found.append(f'sets the body font size: {match.group(0)!r} (preamble)')
    return found


def _words(text: str) -> str:
    text = re.sub(r'\\[A-Za-z]+\*?', ' ', text)
    return ' '.join(re.sub(r'[^0-9A-Za-z&]+', ' ', text).casefold().split())


def headings(text: str) -> list[str]:
    from lib.paper.latex_layout import iter_section_headings
    return [title.strip() for _, _, title in iter_section_headings(text)]


# ── recorder (.fls) ─────────────────────────────────────────────────────────

def _loaded_paths(fls: Path) -> list[Path]:
    """All distinct loaded style/class paths, including duplicate basenames."""
    pwd, files = fls.parent, []
    for line in fls.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith('PWD '):
            pwd = Path(line[4:].strip())
        elif line.startswith('INPUT '):
            path = Path(line[6:].strip())
            if path.suffix in ('.sty', '.cls'):
                resolved = (path if path.is_absolute() else pwd / path).resolve()
                if resolved not in files:
                    files.append(resolved)
    return files


def loaded_files(fls: Path) -> dict[str, Path]:
    """Basename -> first path; verification also checks every duplicate path."""
    files = {}
    for path in _loaded_paths(fls):
        files.setdefault(path.name, path)
    return files


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── probe ───────────────────────────────────────────────────────────────────

def _parse_probe(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(('begin ', 'end ')):
            phase, rest = line.split(' ', 1)
            out[phase] = dict(item.strip().split('=', 1) for item in rest.split(';')
                              if '=' in item)
        elif line.startswith('bodyend='):
            out['bodyend'] = int(line.split('=', 1)[1].rstrip(';'))
            out['bodyend_count'] = out.get('bodyend_count', 0) + 1
        elif line == 'bodyfloat;':
            out['bodyfloat_count'] = out.get('bodyfloat_count', 0) + 1
        elif match := re.fullmatch(r'bodyfloatpage=(\d+);', line):
            out.setdefault('bodyfloat_pages', []).append(int(match[1]))
    return out


def probe_compile(workdir: Path, main: str, engine: str,
                  texinputs: tuple[str, ...] = ()) -> tuple[dict, dict]:
    """Compile ``workdir/main`` with the layout probe; (compile result, probe)."""
    from lib.paper.latex_layout import compile_project
    result = compile_project(workdir, engine=engine, main=main, pretex=PROBE_PRETEX,
                             texinputs=texinputs)
    probe_path = workdir / (Path(main).stem + PROBE_FILE_SUFFIX)
    probe = _parse_probe(probe_path.read_text(encoding='utf-8', errors='replace')
                         if probe_path.is_file() else '')
    return result, probe


def measure_layout(sample_dir: Path, sample: str, engine: str = 'pdflatex') -> dict:
    """The official sample's layout (for pinning an edition)."""
    result, probe = probe_compile(sample_dir, sample, engine)
    if not probe.get('begin'):
        raise RuntimeError(f'official sample {sample} did not compile with the probe: '
                           f'{result.get("status")} {result.get("errors")}')
    return {phase: {key: value for key, value in (probe.get(phase) or {}).items()
                    if key in _LAYOUT_KEYS} for phase in ('begin', 'end')}


# ── gate ────────────────────────────────────────────────────────────────────

def _row(check: str, ok: bool, detail: str = '', **extra) -> dict:
    return {'check': check, 'ok': bool(ok), 'detail': detail, **extra}


def _identity_tokens(identities) -> list[str]:
    tokens = []
    for name in identities or ():
        name = str(name).strip()
        if name and name.casefold() not in ('anonymous', 'anonymous authors', 'anon'):
            tokens.append(name)
    return tokens


def _anonymity_rows(ed: dict, text: str, pdf: Path, source: str, identities) -> list[dict]:
    from pypdf import PdfReader
    rule = ed['anonymity']
    rows = []
    flat = ' '.join(text.split()).casefold()
    markers = rule.get('pdf_markers') or []
    if markers:
        hit = [m for m in markers if ' '.join(m.split()).casefold() in flat]
        rows.append(_row('anonymity.author-block', bool(hit),
                         f'the anonymous author block {markers} is printed' if hit else
                         f'the template\'s anonymous author block ({" / ".join(markers)}) is '
                         'not in the PDF: the build is not in the review mode or replaced '
                         'the author block'))
    named = [name for name in _identity_tokens(identities) if name.casefold() in flat]
    rows.append(_row('anonymity.identities', not named,
                     f'identifying names in the PDF: {named}' if named else
                     'no listed identity appears in the PDF'))
    emails = sorted({m for m in re.findall(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', text)
                     if not re.search(r'anon|example|domain\.com|email\.com', m, re.I)})
    rows.append(_row('anonymity.emails', not emails,
                     f'e-mail addresses in the PDF: {emails}' if emails else
                     'no e-mail address'))
    # Direct project/personal links are not evidence of anonymous browsing.
    # Refuse for review rather than guessing that a repository is third-party.
    urls = sorted(set(re.findall(r'https?://[^\s{}<>\\]+', source)))
    risky = [u for u in urls if re.search(r'github\.com/[^/]+/|gitlab\.com/[^/]+/|'
                                         r'[^/]+\.github\.io/|dropbox\.com/', u, re.I)
             or any(_words(name).replace(' ', '') in _words(u).replace(' ', '')
                    for name in _identity_tokens(identities))]
    matched = [u for u in risky if any(
        _words(name).replace(' ', '') in _words(u).replace(' ', '')
        for name in _identity_tokens(identities))]
    rows.append(_row('anonymity.urls', not risky,
                     f'URL matches a listed identity: {matched}' if matched else
                     f'manual review required (not a proven identity leak): {risky}' if risky
                     else 'no detected personal/project URL; semantic anonymity still needs review',
                     status='identity_match' if matched else 'manual_review' if risky else 'pass'))
    try:
        info = PdfReader(str(pdf)).metadata or {}
        author = str(info.get('/Author') or '').strip()
    except Exception as exc:  # noqa: BLE001 - unreadable metadata is reported, not fatal
        author = f'(unreadable: {exc})'
    rows.append(_row('anonymity.pdf-metadata', not author or 'anonymous' in author.casefold(),
                     f'PDF /Author metadata is {author!r}' if author else
                     'PDF /Author metadata empty'))
    if rule.get('forbid_acknowledgements'):
        acks = [h for h in headings(source)
                if re.fullmatch(r'acknowledge?ments?', _words(h).replace(' ', ''))]
        rows.append(_row('anonymity.acknowledgements', not acks,
                         f'acknowledgements in a submission: {acks}' if acks else
                         'no acknowledgements section'))
    return rows


def _required_rows(ed: dict, source: str, stage: str, track: str) -> list[dict]:
    rows = []
    present = {_words(h): h for h in headings(source)}
    for req in ed.get('required_sections') or []:
        if stage not in (req.get('stages') or ('submission', 'rebuttal', 'camera_ready')):
            continue
        if req.get('tracks') and track not in req['tracks']:
            continue
        names = [req['heading'], *(req.get('alternatives') or [])]
        hit = next((present[_words(n)] for n in names if _words(n) in present), None)
        if hit:
            todo = [marker for marker in req.get('todo_markers') or []
                    if marker in source[source.find(hit):]]
            rows.append(_row(f'required-sections.{req["heading"]}', not todo,
                             f'{hit!r} present' + (f' but unanswered {todo} remain'
                                                   if todo else '')))
            continue
        wanted = {w for n in names for w in _words(n).split()
                  if w not in ('statement', 'section', 'the', 'and', 'of')}
        near = [h for key, h in present.items() if wanted & set(key.split())]
        rows.append(_row(f'required-sections.{req["heading"]}', False,
                         f'required section {" / ".join(repr(n) for n in names)} is missing '
                         f'({req["source"]["url"]})'
                         + (f'; similar headings found: {near} (use the official words)'
                            if near else '')))
    return rows


def _template_rows(ed: dict, fls: Path, build: Path, catalogue: dict) -> list[dict]:
    from lib.longform.documents import venue_registry as reg
    if not fls.is_file():
        return [_row('template.official-files', False,
                     'no LaTeX recorder file (.fls): the loaded style files cannot be '
                     'verified')]
    loaded = loaded_files(fls)
    owned = set(ed.get('owned_files') or ())
    main_file = ed['main_style_file']
    rows, problems = [], []
    if main_file not in loaded:
        problems.append(f'{main_file} (the {ed["conference"]} style) was not loaded')
    for path in _loaded_paths(fls):
        if (path.name in owned and path.is_file()
                and _sha(path) != ed['files'][path.name] and path != loaded[path.name]):
            problems.append(f'{path.name}: second loaded path {path} differs from the pin')
    for leaf in sorted(owned & set(loaded)):
        path = loaded[leaf]
        if not path.is_file():
            problems.append(f'{leaf}: loaded file {path} no longer exists')
        elif _sha(path) != ed['files'][leaf]:
            problems.append(f'{leaf} differs from the official {ed["conference"]} file '
                            f'(sha256 {_sha(path)[:12]}, pinned {ed["files"][leaf][:12]}): '
                            'modified or another year\'s copy')
    for leaf, pin in ed['files'].items():
        path = loaded.get(leaf)
        if leaf not in owned and path and path.is_file() and build in path.parents \
                and _sha(path) != pin:
            problems.append(f'{leaf} (shipped in the official kit) was modified in the paper')
    index = reg.known_style_hashes(catalogue)
    pattern = ed.get('style_pattern')
    for leaf, path in sorted(loaded.items()):
        if leaf in owned or not path.is_file():
            continue
        others = [e for e in index.get(_sha(path), []) if e != ed['id']
                  and reg.edition(e, catalogue)['venue'] == ed['venue']]
        if others and leaf not in ed['files']:
            problems.append(f'{leaf} is the {others[0]} template file, not '
                            f'{ed["conference"]}\'s')
        elif pattern and re.fullmatch(pattern, Path(leaf).stem):
            problems.append(f'{leaf} is another edition\'s style file; {ed["conference"]} '
                            f'requires {main_file}')
    blg = fls.with_suffix('.blg')
    if blg.is_file():
        used = re.search(r'The style file: (\S+)', blg.read_text(encoding='utf-8',
                                                                    errors='replace'))
        if used:
            bst = used.group(1)
            if bst in owned and (build / bst).is_file() and _sha(build / bst) != ed['files'][bst]:
                problems.append(f'{bst} differs from the official {ed["conference"]} file')
            elif bst not in owned and pattern and re.fullmatch(pattern, Path(bst).stem):
                problems.append(f'{bst} is another edition\'s bibliography style')
    rows.append(_row('template.official-files', not problems,
                     '; '.join(problems) if problems else
                     f'{main_file} and every loaded venue file match the {ed["conference"]} '
                     'pins'))
    return rows


# An appendix may legitimately switch to one column (ICML's example paper
# ends in \onecolumn), so the column state is compared at the start only.
_CONTENT_DEPENDENT_AT_END = ('twocolumn', 'columnwidth')


def _layout_row(ed: dict, probe: dict) -> dict:
    want = ed.get('layout') or {}
    if not probe.get('begin'):
        return _row('template.layout', False, 'the layout probe produced no measurement')
    diffs = []
    for phase in ('begin', 'end'):
        for key, value in (want.get(phase) or {}).items():
            if phase == 'end' and key in _CONTENT_DEPENDENT_AT_END:
                continue
            got = (probe.get(phase) or {}).get(key)
            if got != value:
                diffs.append(f'{key} at document {phase} is {got}, the official sample has '
                             f'{value}')
    return _row('template.layout', not diffs, '; '.join(diffs) if diffs else
                'page size, text block, margins, columns and body font equal the official '
                'sample\'s')


def _style_options(ed: dict, source: str) -> set[str]:
    stem = Path(ed['main_style_file']).stem
    opts_re = (r'\\(?:usepackage|documentclass)\s*\[([^\]]*)\]\s*\{[^}]*\b'
               + re.escape(stem) + r'\b[^}]*\}')
    return {o.strip().split('=')[0] for m in re.finditer(opts_re, source)
            for o in m.group(1).split(',') if o.strip()}


def _track_row(ed: dict, source: str, track: str) -> dict:
    tracks = ed.get('tracks') or {}
    option = (tracks.get(track) or {}).get('option')
    selected = _style_options(ed, source) & {row['option'] for row in tracks.values()
                                           if row.get('option')}
    expected = {option} if option else set()
    ok = selected == expected or (not selected and track == ed.get('default_track'))
    return _row('template.track-source', ok if tracks else True,
                f'requested track {track!r}, loaded track options {sorted(selected)}; '
                f'expected {sorted(expected)}')


def _compiled_track_row(ed: dict, probe: dict, track: str) -> dict:
    """NeurIPS flags measured after the preamble, not inferred from spelling."""
    names = ('main', 'eandd', 'position', 'creativeai', 'education', 'workshop')
    flags = {phase: {name: (probe.get(phase) or {}).get(f'track-{name}', '').strip()
                     for name in names} for phase in ('begin', 'end')}
    expected = {name: ('1' if name == track or name == 'main' else '0') for name in names}
    return _row('template.track', all(row == expected for row in flags.values()),
                f'effective compiled track flags {flags}; requested {track!r}, '
                f'expected {expected}')


def _mode_row(ed: dict, source: str, stage: str) -> dict:
    mode = (ed.get('modes') or {}).get(stage) or {}
    problems = []
    options = _style_options(ed, source)
    for option in mode.get('forbidden_options') or []:
        if option in options:
            problems.append(f'option [{option}] is not allowed for a {stage} build')
    for option in mode.get('required_options') or []:
        if option not in options:
            problems.append(f'option [{option}] is required for a {stage} build')
    for command in mode.get('forbidden_commands') or []:
        if re.search(re.escape(command) + r'(?![A-Za-z])', source):
            problems.append(f'{command} is not allowed for a {stage} build')
    return _row('template.mode', not problems, '; '.join(problems) if problems else
                f'{stage} mode options {sorted(options)}')


def check_paper(paper_dir: str | Path, main: str, ed: dict, *, stage: str = 'submission',
                track: str = '', paper_type: str | None = None, identities=(),
                engine: str | None = None, page_limit: int | None = None,
                submission_ready: bool = True, draft_label: str = '',
                catalogue: dict | None = None, keep: str | Path | None = None,
                texinputs: tuple[str, ...] = (), portfolio_policy: str | None = None,
                commitment_venue: str | None = None, freshness_check=None) -> dict:
    """Run every conformance check on a scratch copy of ``paper_dir``.

    ``texinputs`` are the paper's own search directories as its build sets
    them (``style//``); relative ones resolve inside the scratch copy."""
    from lib.longform.documents import venue_registry as reg
    from lib.longform.documents.existing_draft import snapshot
    from lib.longform.documents.render import pdf_pages, pdf_text
    catalogue = catalogue or reg.load_catalogue()
    terms = reg._terms(ed, paper_type=paper_type, track=track or None, stage=stage)
    limit = min(page_limit or terms['page_limit'], terms['page_limit'])
    scratch_root = Path(keep) if keep else Path(tempfile.mkdtemp(prefix='conformance-'))
    build = scratch_root / 'build'
    snapshot(Path(paper_dir), build, exclude=('*.pdf.part',))
    exempt = tuple((ed.get('exempt_sections') or {}).get('headings') or ())
    marked_at = mark_body_end_in_tree(build, main, exempt)
    # Expansion follows only after the marker walk verified input confinement.
    source = expanded_source(build, main).replace('\\' + BODY_END_MACRO + '{}', '')
    engine = engine or ('xelatex' if re.search(r'\\usepackage(?:\[[^\]]*\])?\{fontspec\}|'
                                               r'!TEX program\s*=\s*xelatex', source)
                        else ed.get('engine') or 'pdflatex')
    search = tuple(entry if Path(entry.rstrip('/')).is_absolute()
                   else f'{build}/{entry}' for entry in texinputs)   # keeps 'style//'
    freshness = (freshness_check or reg.check_freshness)(ed)
    result, probe = probe_compile(build, main, engine, search)
    rows = [_row('template.edition-status', ed.get('status') == 'pinned',
                 f'edition status {ed.get("status")!r}; only a pinned edition can be ready'),
            _row('template.freshness', freshness.get('verdict') == 'current',
                 f'official archive/rules freshness: {freshness.get("verdict")}'),
            _row('compile', result.get('status') == 'success' and bool(result.get('pdf'))
                 and not result.get('errors'),
                 f'{result.get("status")}' + (f': {result.get("errors")[:2]}'
                                              if result.get('errors') else ''))]
    stem = Path(main).stem
    from lib.paper.checks import run_tex_checks
    tex_rows = run_tex_checks(build, main, log=build / f'{stem}.log')
    tex_failures = [r for r in tex_rows if r['severity'] in ('error', 'warning')]
    rows.append(_row('tex-layout', not tex_failures,
                     '; '.join(f'{r["code"]}: {r["message"]}' for r in tex_failures)
                     or 'no TeX length/overfull diagnostics'))
    rows += _template_rows(ed, build / f'{stem}.fls', build.resolve(), catalogue)
    allowed = tuple(ed.get('allowed_layout_commands') or ())
    start = source.find('\\begin{document}')
    end = _body_end_pattern(exempt).search(source, max(start, 0))
    overrides = layout_overrides(source, allowed, end.start() if end else None)
    # An override confined to the appendix or back matter cannot fit a paper
    # into its page limit; it is reported, the preamble and body ones fail.
    blocking = [item for item in overrides if 'after the page-limited body' not in item]
    rows.append(_row('template.no-overrides', not blocking, '; '.join(overrides) or
                     'no geometry, font or spacing override in the sources'))
    forbidden = ed.get('forbidden_packages') or ()
    if forbidden:
        used = {name.strip() for group in re.findall(
            r'\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{([^}]*)\}', source)
            for name in group.split(',')}
        banned = sorted(used & set(forbidden))
        rows.append(_row('template.forbidden-packages', not banned,
                         f'the venue forbids {banned}' if banned else
                         'no package the venue forbids'))
    rows.append(_layout_row(ed, probe))
    rows.append(_mode_row(ed, source, stage))
    if ed['venue'] == 'neurips':
        rows.append(_compiled_track_row(ed, probe, terms['track']))
    else:
        rows.append(_track_row(ed, source, terms['track']))
    pdf = Path(result['pdf']) if result.get('pdf') else None
    body_end = probe.get('bodyend')
    pages = pdf_pages(pdf) if pdf else 0
    float_pages = probe.get('bodyfloat_pages', [])
    if (not marked_at or probe.get('bodyend_count') != 1 or body_end is None
            or not 1 <= body_end <= pages
            or probe.get('bodyfloat_count', 0) != len(float_pages)
            or any(not 1 <= page <= pages for page in float_pages)):
        rows.append(_row('page-limit', False,
                         'body marker placement or float shipout evidence could not be verified',
                         status='unverifiable'))
    else:
        counted_end = max([body_end, *float_pages])
        detail = (f'body ends on page {body_end} of {pages}; body float pages {float_pages}; '
                  f'{stage} limit {limit} ({terms["paper_type"]}); '
                  f'exempt headings {list(exempt)}')
        rows.append(_row('page-limit', counted_end <= limit, detail,
                         status='pass' if counted_end <= limit else 'measured_violation'))
    total = (ed.get('max_total_pages') or {}).get(stage)
    if total and pages:
        rows.append(_row('page-limit.total', pages <= total,
                         f'{pages} pages in total; at most {total} allowed'))
    from .content_rules import content_rule_rows
    rows.extend(content_rule_rows(ed, source, stage=stage, track=terms['track'],
                                  pages=pages, page_limit=limit))
    text = pdf_text(pdf) if pdf else ''
    anonymous = (ed['anonymity'].get('double_blind') and stage == 'submission'
                 and track not in (ed['anonymity'].get('single_blind_tracks') or ()))
    if anonymous and pdf:
        rows += _anonymity_rows(ed, text, pdf, source, identities)
    rows += _required_rows(ed, source, stage, terms['track'])
    from .portfolio_policy import assess
    portfolio = assess(portfolio_policy, ed['venue'], commitment_venue=commitment_venue,
                       paper_type=terms['paper_type'])
    rows.append(_row('portfolio-policy', portfolio['submission_ready'],
                     '; '.join(portfolio['failures']) or portfolio.get('detail')
                     or 'official CCF A evidence verified'))
    passed = all(row['ok'] for row in rows)
    report = {
        'edition': ed['id'], 'conference': ed['conference'], 'stage': stage,
        'track': terms['track'], 'paper_type': terms['paper_type'], 'page_limit': limit,
        'engine': engine, 'body_end_marked_at': marked_at, 'pages': pages,
        'body_pages': body_end, 'body_float_pages': float_pages, 'pdf': str(pdf) if pdf else '',
        'pdf_sha256': _sha(pdf) if pdf else '', 'rows': rows, 'passed': passed,
        'failures': [f'{row["check"]}: {row["detail"]}' for row in rows if not row['ok']],
        'submission_ready': passed and submission_ready, 'draft_label': draft_label,
        'portfolio': portfolio, 'freshness': freshness,
        'limitations': ['Static identity and URL checks are not a guarantee of anonymity.',
                        'Layout is probed at document boundaries, not every local group.',
                        'A passing gate is not a guarantee of venue acceptance.'],
        'policy_conflicts': ed.get('policy_conflicts') or [],
        'manual_review': [row for row in rows if row.get('status') == 'manual_review'],
        'input_main_sha256': _sha(Path(paper_dir) / main),
    }
    if not keep:
        shutil.rmtree(scratch_root, ignore_errors=True)
    return report
