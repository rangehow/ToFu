"""Executable specification of the pinned venue-template registry, its
freshness check and the template conformance gate.

Registry/freshness tests use the shipped catalogue and fake downloads (no
network). The gate tests compile deliberately non-conforming papers against
a synthetic official kit (``tpl2027.sty``, pinned like a real edition) and
need a TeX toolchain with latexmk; each one fails first on the defect it
plants: an old-year template, a modified style file, an overlong body, a
deanonymised build, a missing checklist, a layout override.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from lib.longform.documents import conformance as cf
from lib.longform.documents import venue_registry as reg

pytestmark = pytest.mark.unit


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── the shipped catalogue ───────────────────────────────────────────────────

def test_shipped_catalogue_is_valid():
    assert reg.validate_catalogue(reg.load_catalogue()) == []


def test_catalogue_covers_the_supported_venues():
    venues = {row['venue'] for row in reg.editions()}
    assert {'icml', 'neurips', 'iclr', 'arr', 'acl', 'emnlp', 'naacl', 'eacl', 'cvpr',
            'iccv', 'eccv', 'colm', 'aaai', 'ijcai', 'kdd', 'cvpr-workshop',
            'eacl-srw', 'icml-workshop'} <= venues
    pinned = {row['id'] for row in reg.editions() if row['status'] == 'pinned'}
    assert {'icml-2027', 'neurips-2026', 'iclr-2027', 'colm-2026', 'arr-rolling',
            'acl-2027', 'naacl-2027', 'eacl-2027', 'cvpr-2027', 'aaai-2027',
            'ijcai-2027'} <= pinned
    unreleased = {row['id'] for row in reg.editions() if row['status'] in ('not_released', 'unverified')}
    assert {'neurips-2027', 'icml-2028', 'colm-2027', 'iclr-2028', 'iccv-2027',
            'eccv-2028'} <= unreleased


def test_every_pinned_edition_cites_official_sources_for_its_rules():
    for ed in reg.editions():
        if ed['status'] != 'pinned':
            continue
        quotes = reg.rule_quotes(ed)
        names = {name for name, _url, _quote in quotes}
        assert {'page limits', 'anonymity', 'archive link'} <= names, ed['id']
        assert all(url.startswith('https://') and quote for _n, url, quote in quotes)
        assert ed['layout']['begin']['paperwidth'], ed['id']
        assert ed['main_style_file'] in ed['owned_files'], ed['id']


def test_validation_names_an_unproven_edition():
    catalogue = {'schema': reg.SCHEMA, 'editions': [
        {'id': 'x-2027', 'venue': 'x', 'conference': 'X 2027', 'status': 'pinned',
         'checked': '2026-11-18', 'archive': {'url': 'http://mirror/x.zip', 'sha256': 'bad'},
         'files': {}},
        {'id': 'x-2028', 'venue': 'x', 'conference': 'X 2028', 'status': 'not_released',
         'checked': '2026-11-18'}]}
    errors = '\n'.join(reg.validate_catalogue(catalogue))
    assert 'archive.url must be the official https download' in errors
    assert 'archive.sha256 must be a sha256' in errors
    assert 'files must map every official style file' in errors
    assert 'page_limits needs a submission limit' in errors
    assert 'not_released needs the official URLs tried' in errors


# ── fetching a pinned kit ───────────────────────────────────────────────────

def _zip(files: dict, comment: bytes = b'') -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        archive.comment = comment
    return buffer.getvalue()


def _fake_edition(data: bytes, files: dict, commit: str = '') -> dict:
    return {'id': 'fake-2027', 'venue': 'fake', 'conference': 'Fake 2027', 'year': 2027,
            'status': 'pinned', 'checked': '2026-11-18', 'kit_dir': '',
            'archive': {'url': 'https://venue.example/kit.zip', 'sha256': _sha(data),
                        'retrieved': '2026-11-18', 'upstream_commit': commit},
            'archive_source': {'source': {'url': 'https://venue.example/cfp', 'quote': 'q'}},
            'files': {leaf: _sha(content.encode()) for leaf, content in files.items()},
            'page_limits': {'regular': {'submission': 8}},
            'page_limits_rule': {'source': {'url': 'https://venue.example/cfp', 'quote': 'q'}},
            'anonymity': {'rule': 'anonymous'}, 'sample': 'sample.tex'}


def test_pinned_kit_is_verified_byte_for_byte(tmp_path):
    files = {'fake2027.sty': 'style', 'fake2027.bst': 'bst'}
    data = _zip({'Kit/fake2027.sty': 'style', 'Kit/fake2027.bst': 'bst',
                 'Kit/sample.tex': 'sample'})
    ed = _fake_edition(data, files)
    kit = reg.fetch_pinned_kit(ed, tmp_path / 'a', download=lambda url: (data, url))
    assert kit['archive_sha256'] == ed['archive']['sha256'] and kit['edition'] == 'fake-2027'
    assert (tmp_path / 'a' / 'fake2027.sty').read_text() == 'style'
    assert (Path(kit['sample_dir']) / 'sample.tex').is_file()
    changed = _zip({'Kit/fake2027.sty': 'style v2', 'Kit/fake2027.bst': 'bst'})
    with pytest.raises(reg.TemplateRefused, match='the official archive changed since it '
                       'was pinned on 2026-11-18'):
        reg.fetch_pinned_kit(ed, tmp_path / 'b', download=lambda url: (changed, url))


def test_regenerated_github_snapshot_is_accepted_only_by_commit_and_files(tmp_path):
    commit = 'c' * 40
    files = {'fake2027.sty': 'style'}
    pinned = _zip({'r/fake2027.sty': 'style'}, commit.encode())
    ed = _fake_edition(pinned, files, commit)
    regenerated = _zip({'r/fake2027.sty': 'style', 'r/README': 'x'}, commit.encode())
    kit = reg.fetch_pinned_kit(ed, tmp_path / 'a', download=lambda url: (regenerated, url))
    assert 'content verified by commit' in kit['verification_note']
    moved = _zip({'r/fake2027.sty': 'style'}, b'd' * 40)
    with pytest.raises(reg.TemplateRefused, match='archive changed'):
        reg.fetch_pinned_kit(ed, tmp_path / 'b', download=lambda url: (moved, url))
    edited = _zip({'r/fake2027.sty': 'edited'}, commit.encode())
    with pytest.raises(reg.TemplateRefused, match=r"official kit files \['fake2027.sty'\]"):
        reg.fetch_pinned_kit(ed, tmp_path / 'c', download=lambda url: (edited, url))


# ── freshness ───────────────────────────────────────────────────────────────

def _freshness_edition(data):
    ed = _fake_edition(data, {'fake2027.sty': 'style'})
    ed['page_limits_rule'] = {'source': {'url': 'https://venue.example/cfp',
                                         'quote': 'Papers are limited to eight pages.'}}
    ed['anonymity'] = {'rule': 'anonymous', 'source': {
        'url': 'https://venue.example/kit.zip', 'archive_member': 'guide.tex',
        'quote': 'Submissions must be anonymous.'}}
    ed['archive_source'] = {'source': {'url': 'https://venue.example/cfp',
                                       'quote': 'Download the Fake 2027 kit'}}
    ed['newer_edition_probes'] = ['https://venue.example/2028/cfp']
    return ed


def _pages(**overrides):
    data = _zip({'k/fake2027.sty': 'style',
                 'k/guide.tex': 'Submissions   must be\nanonymous.'})
    pages = {'https://venue.example/cfp':
             b'<p>Papers are <b>limited</b> to eight pages.</p><a>Download the Fake 2027 '
             b'kit</a>', 'https://venue.example/kit.zip': data}
    pages.update(overrides)

    def download(url):
        if url == 'https://venue.example/2028/cfp' and url not in pages:
            raise RuntimeError(f'{url}: HTTP 404')
        if isinstance(pages[url], tuple):
            return pages[url]
        return pages[url], url
    return data, download


def test_freshness_is_current_when_archive_and_quoted_rules_are_unchanged():
    data, download = _pages()
    report = reg.check_freshness(_freshness_edition(data), download=download)
    assert report['verdict'] == 'current'
    assert all(row['present'] for row in report['rules'])
    assert report['archive']['changed'] is False


def test_freshness_reports_a_changed_archive_rule_or_newer_edition():
    data, _ = _pages()
    ed = _freshness_edition(data)
    _, download = _pages(**{'https://venue.example/kit.zip': _zip({'k/fake2027.sty': 'v2',
                                                                   'k/guide.tex': 'x'})})
    report = reg.check_freshness(ed, download=download)
    assert report['verdict'] == 'changed' and report['archive']['changed']
    assert {row['rule']: row['present'] for row in report['rules']}['anonymity'] is False
    _, download = _pages(**{'https://venue.example/cfp': b'Papers are limited to nine pages.'})
    report = reg.check_freshness(ed, download=download)
    assert report['verdict'] == 'changed'
    _, download = _pages(**{'https://venue.example/2028/cfp': b'Call for papers 2028'})
    report = reg.check_freshness(ed, download=download)
    assert report['verdict'] == 'changed' and report['newer'][0]['answers']
    # aaai.org redirects an unknown year to an old edition: not a newer edition.
    _, download = _pages(**{'https://venue.example/2028/cfp': (
        b'2025 instructions', 'https://venue.example/2025/cfp')})
    assert reg.check_freshness(ed, download=download)['verdict'] == 'unverifiable'


def test_freshness_of_an_unreleased_edition_notices_the_release():
    ed = {'id': 'fake-2028', 'venue': 'fake', 'conference': 'Fake 2028', 'year': 2028,
          'status': 'not_released', 'checked': '2026-11-18', 'not_released_evidence': [
              {'url': 'https://venue.example/2028/cfp', 'result': 'HTTP 404'},
              {'url': 'https://venue.example/w', 'result': 'HTTP 200: soon',
               'quote': 'will be posted here soon'}]}
    pages = {'https://venue.example/w': b'Call will be posted here soon.'}

    def download(url):
        if url not in pages:
            raise RuntimeError(f'{url}: HTTP 404')
        return pages[url], url
    assert reg.check_freshness(ed, download=download)['verdict'] == 'unverifiable'
    pages['https://venue.example/2028/cfp'] = b'Call for papers'
    assert reg.check_freshness(ed, download=download)['verdict'] == 'changed'
    del pages['https://venue.example/2028/cfp']
    pages['https://venue.example/w'] = b'The workshops are listed below.'
    assert reg.check_freshness(ed, download=download)['verdict'] == 'changed'


def test_freshness_is_unverifiable_when_a_source_cannot_be_fetched():
    data, _ = _pages()

    def download(url):
        raise RuntimeError(f'{url}: no route')
    assert reg.check_freshness(_freshness_edition(data),
                               download=download)['verdict'] == 'unverifiable'


# ── source helpers ──────────────────────────────────────────────────────────

def test_layout_overrides_are_named_with_their_line():
    text = ('\\documentclass[11pt]{article}\n\\usepackage{tpl2027}\n'
            '\\usepackage[margin=1cm]{geometry}\n\\setlength{\\textheight}{10in}\n'
            '\\linespread{0.9}\n\\renewcommand{\\baselinestretch}{0.95}\n'
            '\\usepackage{savetrees}\n\\begin{document}\n'
            '\\setlength{\\abovecaptionskip}{-4pt}\n\\end{document}\n')
    found = '\n'.join(cf.layout_overrides(text))
    for expected in ('loads geometry', 'changes a kit dimension', 'changes line spacing',
                     'layout-changing package', 'changes the class font size', 'line 4',
                     'abovecaptionskip'):
        assert expected in found
    assert cf.layout_overrides(text.replace('[margin=1cm]', '[pass]')) != []
    assert not any('geometry' in item for item in cf.layout_overrides(
        '\\usepackage[pass]{geometry}'))
    assert not cf.layout_overrides('\\documentclass[11pt]{article}',
                                   ('\\documentclass[11pt]',))


def test_body_end_marker_lands_after_the_last_counted_text_in_any_input_file(tmp_path):
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n'
        '% \\section*{Ethics Statement}\n\\input{sections/back}\n\\end{document}\n')
    (tmp_path / 'sections').mkdir()
    (tmp_path / 'sections' / 'body.tex').write_text('\\section{Intro}\nLast words.\n\n')
    (tmp_path / 'sections' / 'back.tex').write_text('\\section*{ethics statement}\nE.\n')
    where = cf.mark_body_end_in_tree(tmp_path, 'main.tex', ('Ethics Statement',))
    assert where.startswith('sections/body.tex:')
    assert 'Last words.\\TofuConformanceBodyEnd{}' in (
        tmp_path / 'sections' / 'body.tex').read_text()


def test_recorder_lists_the_loaded_style_files(tmp_path):
    (tmp_path / 'main.fls').write_text(
        f'PWD {tmp_path}\nINPUT /usr/tex/article.cls\nINPUT ./tpl2027.sty\n'
        'INPUT ./main.tex\nOUTPUT main.pdf\n')
    loaded = cf.loaded_files(tmp_path / 'main.fls')
    assert loaded['tpl2027.sty'] == tmp_path / 'tpl2027.sty'
    assert 'article.cls' in loaded and 'main.tex' not in loaded


# ── the gate on compiled papers (needs TeX) ─────────────────────────────────

def _tex_ready() -> bool:
    if not shutil.which('latexmk'):
        return False
    probe = subprocess.run(['kpsewhich', 'lineno.sty', 'times.sty'], capture_output=True,
                           text=True, check=False)
    return probe.stdout.count('.sty') == 2


tex = pytest.mark.skipif(not _tex_ready(), reason='latexmk with lineno/times not installed')

_STY = r"""\ProvidesPackage{%(name)s}
\newif\iftpl@final
\DeclareOption{final}{\tpl@finaltrue}
\ProcessOptions\relax
\RequirePackage{times}
\setlength{\textwidth}{6in}\setlength{\textheight}{%(height)s}
\setlength{\oddsidemargin}{0.25in}\setlength{\topmargin}{0in}
\newcommand{\answerYes}{[Yes]}\newcommand{\answerTODO}{[TODO]}
\renewcommand{\maketitle}{\begin{center}{\Large\@title}\\[1em]
  \iftpl@final\@author\else Anonymous Authors\fi\end{center}}
"""


def _kit_sty(name='tpl2027', height='8in'):
    return _STY % {'name': name, 'height': height}


def _paper(option='', author='Jane Roe', body_paragraphs=3, checklist='Paper Checklist',
           extra_preamble='', back=''):
    para = ('This paragraph states one finding of the paper in plain words and is '
            'repeated to give the body a realistic length on the page. ' * 6)
    return (f'\\documentclass{{article}}\n\\usepackage{option}{{tpl2027}}\n{extra_preamble}'
            f'\\title{{A Conforming Paper}}\\author{{{author}}}\n\\begin{{document}}\n'
            '\\maketitle\n\\section{Introduction}\n'
            + '\n\n'.join(para for _ in range(body_paragraphs))
            + f'\n{back}\\section*{{Ethics Statement}}\nNo concerns.\n'
            f'\\section*{{{checklist}}}\nClaims: \\answerYes\n\\end{{document}}\n')


def _write(directory: Path, main: str, sty: str | None = None, extra: dict | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'main.tex').write_text(main)
    (directory / 'tpl2027.sty').write_text(sty if sty is not None else _kit_sty())
    for name, text in (extra or {}).items():
        (directory / name).write_text(text)
    return directory


@pytest.fixture(scope='module')
def synthetic(tmp_path_factory):
    """A pinned synthetic edition whose layout is measured from its own
    'official sample', exactly as a real edition is pinned."""
    root = tmp_path_factory.mktemp('kit')
    sample = _write(root / 'sample', _paper())
    layout = cf.measure_layout(sample, 'main.tex')
    cite = {'source': {'url': 'https://venue.example/cfp', 'quote': 'q'}}
    ed = {'id': 'tpl-2027', 'venue': 'tpl', 'family': 'tpl', 'conference': 'TPL 2027',
          'year': 2027, 'status': 'pinned', 'checked': '2026-11-18',
          'archive': {'url': 'https://venue.example/kit.zip', 'sha256': 'a' * 64,
                      'retrieved': '2026-11-18'},
          'archive_source': cite, 'files': {'tpl2027.sty': _sha(_kit_sty().encode())},
          'main_style_file': 'tpl2027.sty', 'owned_files': ['tpl2027.sty'],
          'style_pattern': r'tpl\d{4}', 'layout': layout,
          'page_limits': {'regular': {'submission': 1, 'camera_ready': 2}},
          'page_limits_rule': cite,
          'anonymity': {'rule': 'anonymous', 'double_blind': True,
                        'forbid_acknowledgements': True, 'pdf_markers': ['Anonymous Authors'],
                        **cite},
          'required_sections': [{'heading': 'Paper Checklist',
                                 'todo_markers': ['\\answerTODO'], **cite}],
          'exempt_sections': {'headings': ['Ethics Statement', 'Paper Checklist'], **cite},
          'modes': {'submission': {'forbidden_options': ['final']}}}
    old = {'id': 'tpl-2026', 'venue': 'tpl', 'conference': 'TPL 2026', 'year': 2026,
           'status': 'superseded', 'checked': '2026-11-18',
           'archive': {'url': 'https://venue.example/kit26.zip', 'sha256': 'b' * 64,
                       'retrieved': '2025-11-18'},
           'files': {'tpl2026.sty': _sha(_kit_sty('tpl2026').encode())}}
    catalogue = {'schema': reg.SCHEMA, 'editions': [ed, old]}
    return root, ed, catalogue


def _check(synthetic, name, main, **kw):
    root, ed, catalogue = synthetic
    paper = _write(root / name, main, kw.pop('sty', None), kw.pop('extra', None))
    return cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                          freshness_check=lambda ed: {'verdict': 'current'},
                          identities=['Jane Roe'], **kw)


def _failed(report) -> dict:
    return {row['check']: row['detail'] for row in report['rows'] if not row['ok']}


@tex
def test_a_conforming_paper_passes_every_check(synthetic):
    report = _check(synthetic, 'ok', _paper(author='Jane Roe'))
    assert _failed(report) == {}
    assert report['passed'] and report['submission_ready']
    assert report['body_pages'] == 1
    checks = {row['check'] for row in report['rows']}
    assert {'compile', 'template.official-files', 'template.no-overrides', 'template.layout',
            'template.mode', 'page-limit', 'anonymity.author-block',
            'required-sections.Paper Checklist'} <= checks


@tex
def test_the_previous_years_template_fails(synthetic):
    main = _paper().replace('{tpl2027}', '{tpl2026}')
    report = _check(synthetic, 'old', main, extra={'tpl2026.sty': _kit_sty('tpl2026')})
    detail = _failed(report)['template.official-files']
    assert 'tpl2027.sty (the TPL 2027 style) was not loaded' in detail
    assert 'tpl2026.sty is the tpl-2026 template file' in detail


@tex
def test_a_modified_style_file_fails(synthetic):
    report = _check(synthetic, 'modified', _paper(), sty=_kit_sty(height='9in'))
    failed = _failed(report)
    assert 'tpl2027.sty differs from the official TPL 2027 file' in \
        failed['template.official-files']
    assert 'textheight at document begin is 650.43pt' in failed['template.layout']


@tex
def test_an_overlong_body_fails_but_exempt_sections_do_not_count(synthetic):
    report = _check(synthetic, 'long', _paper(body_paragraphs=12))
    assert 'body ends on page 2' in _failed(report)['page-limit']
    camera = _check(synthetic, 'long-camera', _paper(body_paragraphs=12, option='[final]'),
                    stage='camera_ready')
    assert 'page-limit' not in _failed(camera), 'camera-ready allows 2 pages'


@tex
def test_a_deanonymised_submission_fails(synthetic):
    main = _paper(option='[final]', author='Jane Roe \\\\ jane.roe@uni.edu',
                  back='\\section*{Acknowledgments}\nWe thank our lab.\n')
    failed = _failed(_check(synthetic, 'deanon', main))
    assert 'not in the review mode' in failed['anonymity.author-block']
    assert "['Jane Roe']" in failed['anonymity.identities']
    assert 'jane.roe@uni.edu' in failed['anonymity.emails']
    assert 'Acknowledgments' in failed['anonymity.acknowledgements']
    assert 'option [final] is not allowed for a submission build' in failed['template.mode']


@tex
def test_a_missing_or_renamed_checklist_fails(synthetic):
    failed = _failed(_check(synthetic, 'nocheck', _paper(checklist='Checklist')))
    detail = failed['required-sections.Paper Checklist']
    assert "required section 'Paper Checklist' is missing" in detail
    assert "similar headings found: ['Checklist']" in detail
    todo = _paper().replace('Claims: \\answerYes', 'Claims: \\answerTODO')
    assert 'unanswered' in _failed(_check(synthetic, 'todo', todo))[
        'required-sections.Paper Checklist']


@tex
def test_a_preamble_layout_override_fails(synthetic):
    main = _paper(extra_preamble='\\addtolength{\\textheight}{1in}\n')
    failed = _failed(_check(synthetic, 'override', main))
    assert 'changes a kit dimension' in failed['template.no-overrides']
    assert 'textheight' in failed['template.layout']


@tex
def test_an_override_after_the_body_is_reported_but_does_not_fail(synthetic):
    main = _paper().replace('\\section*{Ethics Statement}',
                            '\\appendix\\section{Tables}\\setlength{\\floatsep}{4pt}\n'
                            '\\section*{Ethics Statement}')
    report = _check(synthetic, 'appendix-override', main)
    row = next(r for r in report['rows'] if r['check'] == 'template.no-overrides')
    assert row['ok'] and 'after the page-limited body' in row['detail']


@tex
def test_a_previous_year_draft_is_never_submission_ready(synthetic):
    report = _check(synthetic, 'draft', _paper(), submission_ready=False,
                    draft_label='DRAFT on the TPL 2026 template')
    assert report['passed'] and not report['submission_ready']


@tex
@pytest.mark.parametrize('placement,expected_page', [('!ht', 1), ('p', 2)])
@pytest.mark.parametrize('kind', ['figure', 'table', 'figure*', 'table*'])
def test_body_float_shipout_counts_without_changing_pdf(synthetic, tmp_path, placement, expected_page, kind):
    from pypdf import PdfReader
    from lib.paper.latex_layout import compile_project

    _, ed, catalogue = synthetic
    back = (rf'\begin{{{kind}}}[{placement}]\rule{{1cm}}{{1cm}}'
            rf'\caption{{Body float parity evidence}}\end{{{kind}}}' '\n')
    main = _paper(body_paragraphs=1, back=back)
    baseline = compile_project(_write(tmp_path / 'plain', main))
    paper = _write(tmp_path / 'paper', main)
    original = (paper / 'main.tex').read_bytes()
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            keep=tmp_path / 'kept',
                            freshness_check=lambda _: {'verdict': 'current'})
    def pages(path):
        return [' '.join(page.extract_text().split()) for page in PdfReader(path).pages]
    measured = pages(report['pdf'])
    assert measured == pages(baseline['pdf'])
    assert (paper / 'main.tex').read_bytes() == original
    assert 'Body float parity evidence' in measured[expected_page - 1]
    assert report['body_pages'] == 1
    assert report['body_float_pages'] == [expected_page]
    row = next(row for row in report['rows'] if row['check'] == 'page-limit')
    assert row['ok'] is (expected_page == 1)
    assert report['submission_ready'] is (expected_page == 1), report['failures']


@tex
@pytest.mark.parametrize('tail', [r'\parbox[t]{4cm}{\newpage\label{anchor}}',
                                  r'\newpage\vspace{0pt}',
                                  r'\par\pagebreak[4]\vspace{0pt}',
                                  r'\par\pagebreak [4]\label{anchor}',
                                  r'\par\pagebreak[ 4 ]\vspace{0pt}',
                                  r'\par\pagebreak [ 4 ]\label{anchor}',
                                  '\\par\\pagebreak[\n  4\n]\\label{anchor}',
                                  '\\par\\pagebreak[% strength\n4]\\vspace{0pt}',
                                  r'\newpage\ifnum0=1 hidden\fi',
                                  r'\newpage\unless\iftrue hidden\fi',
                                  r'\newpage\label{before}{\label{after}}'])
@pytest.mark.parametrize('input_cutoff', [False, True])
def test_ambiguous_nonprinting_tail_is_not_measured_overflow(synthetic, tmp_path, tail, input_cutoff):
    from pypdf import PdfReader
    from lib.paper.latex_layout import compile_project

    _, ed, catalogue = synthetic
    main = _paper(body_paragraphs=1, back=tail + '\n')
    extra = {}
    if input_cutoff:
        cutoff = main.index(r'\section*{Ethics Statement}')
        extra['back.tex'] = main[cutoff:main.index(r'\end{document}')]
        main = main[:cutoff] + '\\input{back}\n\\end{document}\n'
    baseline = compile_project(_write(tmp_path / 'plain', main, extra=extra))
    paper = _write(tmp_path / 'paper', main, extra=extra)
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            keep=tmp_path / 'kept',
                            freshness_check=lambda _: {'verdict': 'current'})
    def pages(path):
        return [' '.join(page.extract_text().split()) for page in PdfReader(path).pages]
    assert pages(report['pdf']) == pages(baseline['pdf'])
    assert (tmp_path / 'kept/build/main.tex').read_bytes() == (paper / 'main.tex').read_bytes()
    row = next(row for row in report['rows'] if row['check'] == 'page-limit')
    assert not row['ok'] and row['status'] == 'unverifiable'
    assert not report['submission_ready']


@tex
def test_exempt_float_is_not_counted_as_a_body_float(synthetic, tmp_path):
    from pypdf import PdfReader
    from lib.paper.latex_layout import compile_project

    _, ed, catalogue = synthetic
    main = _paper(body_paragraphs=1).replace('No concerns.',
        r'No concerns.\begin{figure}[p]\rule{1cm}{1cm}'
        r'\caption{Exempt float evidence}\end{figure}')
    baseline = compile_project(_write(tmp_path / 'plain', main))
    report = cf.check_paper(_write(tmp_path / 'paper', main), 'main.tex', ed,
                            catalogue=catalogue, keep=tmp_path / 'kept',
                            freshness_check=lambda _: {'verdict': 'current'})
    measured = [' '.join(page.extract_text().split()) for page in PdfReader(report['pdf']).pages]
    assert measured == [' '.join(page.extract_text().split())
                        for page in PdfReader(baseline['pdf']).pages]
    assert 'Exempt float evidence' in measured[1]
    assert report['body_pages'] == 1 and report['body_float_pages'] == []
    assert report['submission_ready'], report['failures']


@tex
@pytest.mark.parametrize('damage', ['missing-shipout', 'out-of-range', 'repeated-marker'])
def test_incomplete_float_or_marker_evidence_is_unverifiable(synthetic, tmp_path, monkeypatch, damage):
    _, ed, catalogue = synthetic
    parse = cf._parse_probe
    def damaged(text):
        probe = parse(text)
        if damage == 'missing-shipout':
            probe['bodyfloat_pages'] = []
        elif damage == 'out-of-range':
            probe['bodyfloat_pages'] = [0]
        else:
            probe['bodyend_count'] = 2
        return probe
    monkeypatch.setattr(cf, '_parse_probe', damaged)
    main = _paper(body_paragraphs=1, back=r'\begin{figure}[!ht]\rule{1cm}{1cm}'
                  r'\caption{Evidence completeness}\end{figure}' '\n')
    report = cf.check_paper(_write(tmp_path / 'paper', main), 'main.tex', ed,
                            catalogue=catalogue, freshness_check=lambda _: {'verdict': 'current'})
    row = next(row for row in report['rows'] if row['check'] == 'page-limit')
    assert not row['ok'] and row['status'] == 'unverifiable'
    assert not report['submission_ready']


@tex
@pytest.mark.parametrize('late', ['Counted late prose.',
                                 r'\begin{figure}[!ht]\rule{1cm}{1cm}'
                                 r'\caption{Counted late float}\end{figure}'])
@pytest.mark.parametrize('page_break', [r'\newpage', r'\par\pagebreak[4]',
                                       r'\par\pagebreak[ 4 ]',
                                       '\\par\\pagebreak[\n4\n]'])
def test_positive_late_content_remains_measured_violation(synthetic, tmp_path, late, page_break):
    from pypdf import PdfReader
    from lib.paper.latex_layout import compile_project

    _, ed, catalogue = synthetic
    main = _paper(body_paragraphs=1, back=page_break + '\n' + late + '\n')
    baseline = compile_project(_write(tmp_path / 'plain', main))
    report = cf.check_paper(_write(tmp_path / 'paper', main), 'main.tex', ed,
                            catalogue=catalogue, keep=tmp_path / 'kept',
                            freshness_check=lambda _: {'verdict': 'current'})
    measured = [' '.join(page.extract_text().split()) for page in PdfReader(report['pdf']).pages]
    assert measured == [' '.join(page.extract_text().split())
                        for page in PdfReader(baseline['pdf']).pages]
    assert 'Counted late' in measured[1]
    row = next(row for row in report['rows'] if row['check'] == 'page-limit')
    assert not row['ok'] and row['status'] == 'measured_violation'
    assert not report['submission_ready']


def test_pagebreak_optional_argument_is_one_lexical_unit():
    def end(text):
        return cf._break_end(text, cf._PAGE_BREAK.search(text))

    assert end(r'\pagebreak[4]\label{a}') == len(r'\pagebreak[4]')
    assert end('\\pagebreak[ 4 ]\\vspace{0pt}') == len('\\pagebreak[ 4 ]')
    assert end('\\pagebreak[\n  4\n]\\label{a}') == len('\\pagebreak[\n  4\n]')
    assert end(r'\pagebreak[{4}]\label{a}') == len(r'\pagebreak[{4}]')
    assert end(r'\pagebreak\vspace{0pt}') == len(r'\pagebreak')
    # An argument whose extent cannot be established is not guessed at.
    assert end(r'\pagebreak[4') is None
    assert end(r'\pagebreak[4}]\label{a}') is None
    # Commands with no optional argument still end at their control word.
    assert end(r'\newpage[not-an-argument]') == len(r'\newpage')


def test_newer_probe_timeout_is_unverifiable_not_absence():
    data, download = _pages()
    ed = _freshness_edition(data)

    def timeout(url):
        if url.endswith('/2028/cfp'):
            raise RuntimeError('connection timed out')
        return download(url)
    assert reg.check_freshness(ed, download=timeout)['verdict'] == 'unverifiable'


def test_icml_policy_heading_not_template_heading_alone():
    ed = reg.edition('icml-2027')
    good = '\\section*{Impact Statement}I.\\section*{AI Disclosure Statement}A.'
    assert all(r['ok'] for r in cf._required_rows(ed, good, 'submission', ''))
    bad = good.replace('AI Disclosure Statement', 'AI Use Statement')
    assert any(not r['ok'] and 'AI Disclosure Statement' in r['check']
               for r in cf._required_rows(ed, bad, 'submission', ''))


@tex
def test_superseded_direct_api_and_offline_freshness_cannot_be_ready(synthetic):
    from copy import deepcopy
    root, ed, catalogue = synthetic
    ed = deepcopy(ed); ed['status'] = 'superseded'
    paper = _write(root / 'direct-status', _paper())
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            freshness_check=lambda e: {'verdict': 'current'})
    assert not report['submission_ready']
    assert 'template.edition-status' in _failed(report)
    report = cf.check_paper(paper, 'main.tex', synthetic[1], catalogue=catalogue,
                            freshness_check=lambda e: {'verdict': 'unverifiable'})
    assert not report['submission_ready'] and 'template.freshness' in _failed(report)


@tex
def test_owner_policy_composes_with_the_template_gate(synthetic, tmp_path, monkeypatch):
    import json
    from lib.longform.documents import portfolio_policy as pp
    evidence = {'status': 'verified', 'source': {
        'edition': 'test edition', 'edition_date': '2026-01-01', 'retrieved': '2026-11-18',
        'url': 'https://www.ccf.org.cn/test'}, 'venues': {
            'tpl': {'tier': 'A', 'url': 'https://www.ccf.org.cn/test', 'quote': 'TPL'}}}
    evidence['source']['sha256'] = _sha(b'official fixture')
    monkeypatch.setattr(reg, 'http_download', lambda u: (b'official fixture', u))
    path = tmp_path / 'ccf.json'; path.write_text(json.dumps(evidence))
    monkeypatch.setattr(pp, 'EVIDENCE_PATH', path)
    assert _check(synthetic, 'owner-a', _paper(), portfolio_policy=pp.OWNER_CCF_A)[
        'submission_ready']
    broken = _check(synthetic, 'owner-a-old', _paper().replace('{tpl2027}', '{tpl2026}'),
                    extra={'tpl2026.sty': _kit_sty('tpl2026')},
                    portfolio_policy=pp.OWNER_CCF_A)
    assert not broken['submission_ready'] and 'template.official-files' in _failed(broken)
    evidence['status'] = 'unavailable'; path.write_text(json.dumps(evidence))
    assert not _check(synthetic, 'owner-unknown', _paper(),
                      portfolio_policy=pp.OWNER_CCF_A)['submission_ready']


@tex
def test_overfull_build_and_identifying_url_fail(synthetic):
    bad = _paper(back='\\hbox{\\rule{20in}{1pt}}\n'
                      '\\texttt{https://github.com/jane-roe/my-paper}\n')
    report = _check(synthetic, 'overfull-url', bad)
    assert 'tex-layout' in _failed(report)
    assert 'anonymity.urls' in _failed(report)


def test_marker_never_edits_an_external_paper(tmp_path):
    external = tmp_path / 'formal.tex'; external.write_text('Body.\\bibliography{r}')
    root = tmp_path / 'scratch'; root.mkdir()
    (root / 'main.tex').write_text('\\begin{document}\\input{' + str(external) + '}'
                                    '\\end{document}')
    before = external.read_bytes()
    with pytest.raises(ValueError, match='escapes the scratch build'):
        cf.mark_body_end_in_tree(root, 'main.tex', ())
    assert external.read_bytes() == before


@tex
def test_third_party_license_is_manual_review_not_identity_leak(synthetic):
    report = _check(synthetic, 'license-url', _paper(
        back='\\texttt{https://github.com/lucide-icons/lucide/blob/main/LICENSE}\n'))
    row = next(r for r in report['rows'] if r['check'] == 'anonymity.urls')
    assert row['status'] == 'manual_review' and 'not a proven identity leak' in row['detail']
    assert report['manual_review'] and not report['submission_ready']
    identified = _check(synthetic, 'personal-url', _paper(
        back='\\texttt{https://github.com/jane-roe/my-paper}\n'))
    row = next(r for r in identified['rows'] if r['check'] == 'anonymity.urls')
    assert row['status'] == 'identity_match' and not row['ok']


@pytest.mark.parametrize('member', ['../formal.tex', '/absolute.tex', r'..\formal.tex'])
def test_archive_extraction_refuses_escape_before_any_write(tmp_path, member):
    data = _zip({'Kit/fake2027.sty': 'style', member: 'bad'})
    ed = _fake_edition(data, {'fake2027.sty': 'style'})
    with pytest.raises(reg.TemplateRefused, match='unsafe archive member'):
        reg.fetch_pinned_kit(ed, tmp_path / 'kit', download=lambda url: (data, url))
    assert not (tmp_path / 'kit').exists()


def test_every_loaded_copy_of_a_pinned_basename_is_checked(tmp_path):
    a = tmp_path / 'a'; b = tmp_path / 'b'; a.mkdir(); b.mkdir()
    (a / 'tpl2027.sty').write_text('official')
    (b / 'tpl2027.sty').write_text('modified')
    fls = tmp_path / 'main.fls'
    fls.write_text(f'PWD {tmp_path}\nINPUT a/tpl2027.sty\nINPUT b/tpl2027.sty\n')
    ed = {'id': 'tpl-2027', 'conference': 'TPL 2027', 'venue': 'tpl',
          'files': {'tpl2027.sty': _sha(b'official')},
          'owned_files': ['tpl2027.sty'], 'main_style_file': 'tpl2027.sty'}
    rows = cf._template_rows(ed, fls, tmp_path, {'editions': [ed]})
    assert not rows[0]['ok'] and 'second loaded path' in rows[0]['detail']


def test_cvpr_yearless_style_uses_explicit_edition_and_content_pins(tmp_path):
    from copy import deepcopy

    from lib.longform.documents import venues

    terms = venues.venue_terms('cvpr', year=2027)
    assert terms['edition'] == terms['target_edition'] == 'cvpr-2027'
    assert terms['submission_ready'] and not terms['draft_label']
    retained = reg.edition(terms['edition'])
    assert retained['year'] == 2027 and retained['conference'] == 'CVPR 2027'
    assert retained['main_style_file'] == 'cvpr.sty'
    assert retained['archive_source']['source']['quote'] == (
        'Please download the CVPR 2027 Submission Template')

    # Counterpart bytes exercise the retained mapping, not historical vendor bytes.
    counterpart = deepcopy(retained)
    contents = {leaf: f'synthetic CVPR 2027 counterpart: {leaf}\n'.encode()
                for leaf in retained['files']}
    counterpart['files'] = {leaf: _sha(data) for leaf, data in contents.items()}
    catalogue = {'schema': reg.SCHEMA, 'editions': [counterpart]}
    for leaf, data in contents.items():
        (tmp_path / leaf).write_bytes(data)
    fls = tmp_path / 'main.fls'
    fls.write_text(f'PWD {tmp_path}\nINPUT cvpr.sty\n')
    fls.with_suffix('.blg').write_text('The style file: ieeenat_fullname.bst\n')
    assert cf._template_rows(counterpart, fls, tmp_path, catalogue)[0]['ok']

    for leaf in counterpart['owned_files']:
        path = tmp_path / leaf
        path.write_bytes(f'synthetic previous-year counterpart: {leaf}\n'.encode())
        row = cf._template_rows(counterpart, fls, tmp_path, catalogue)[0]
        assert row['check'] == 'template.official-files' and not row['ok']
        assert f'{leaf} differs from the official CVPR 2027 file' in row['detail']
        path.write_bytes(contents[leaf])
    assert cf._template_rows(counterpart, fls, tmp_path, catalogue)[0]['ok']

    for unsupported_year in (2026, 2028):
        with pytest.raises(reg.TemplateRefused,
                           match=f'no cvpr {unsupported_year} template is pinned'):
            venues.venue_terms('cvpr', year=unsupported_year)
    with pytest.raises(reg.TemplateRefused, match='"venue_year" is required'):
        venues.venue_terms('cvpr')


def test_neurips_track_option_must_match_requested_track():
    ed = reg.edition('neurips-2026')
    assert cf._track_row(ed, r'\usepackage{neurips_2026}', 'main')['ok']
    assert not cf._track_row(ed, r'\usepackage{neurips_2026}', 'eandd')['ok']
    assert cf._track_row(ed, r'\usepackage[eandd]{neurips_2026}', 'eandd')['ok']
    assert not cf._track_row(ed, r'\usepackage[eandd]{neurips_2026}', 'main')['ok']


def test_http_failure_alone_cannot_establish_not_released():
    ed = {'id': 'unknown-2028', 'status': 'not_released',
          'not_released_evidence': [{'url': 'https://venue.example/cfp', 'result': 'HTTP 404'}]}
    errors = reg.validate_catalogue({'schema': reg.SCHEMA, 'editions': [ed]})
    assert any('affirmative official' in error for error in errors)


def test_matching_zip_comment_cannot_hide_changed_pinned_style_from_freshness(tmp_path):
    commit = 'c' * 40
    pinned = _zip({'k/fake2027.sty': 'style',
                   'k/guide.tex': 'Submissions must be anonymous.'}, commit.encode())
    ed = _freshness_edition(pinned)
    ed['archive']['upstream_commit'] = commit
    forged = _zip({'k/fake2027.sty': 'ALTERED STYLE',
                   'k/guide.tex': 'Submissions must be anonymous.'}, commit.encode())
    _, download = _pages(**{ed['archive']['url']: forged})
    with pytest.raises(reg.TemplateRefused, match='official kit files'):
        reg.fetch_pinned_kit(ed, tmp_path / 'kit', download=download)
    report = reg.check_freshness(ed, download=download)
    assert report['verdict'] == 'changed'
    assert report['archive']['changed']


@pytest.mark.parametrize('target', ['archive', 'cfp'])
def test_unrecorded_redirect_cannot_be_current_even_with_identical_bytes(tmp_path, target):
    data, _ = _pages()
    ed = _freshness_edition(data)
    url = ed['archive']['url'] if target == 'archive' else 'https://venue.example/cfp'
    payload = data if target == 'archive' else (
        b'Papers are limited to eight pages. Download the Fake 2027 kit')
    _, download = _pages(**{url: (payload, 'https://unrecognised.example/same')})
    assert reg.check_freshness(ed, download=download)['verdict'] == 'unverifiable'
    if target == 'archive':
        with pytest.raises(reg.TemplateRefused, match='redirect'):
            reg.fetch_pinned_kit(ed, tmp_path / 'kit', download=download)


def test_regenerated_snapshot_uses_identical_verifier_for_intake_and_freshness(tmp_path):
    commit = 'c' * 40
    pinned = _zip({'k/fake2027.sty': 'style',
                   'k/guide.tex': 'Submissions must be anonymous.'}, commit.encode())
    ed = _freshness_edition(pinned); ed['archive']['upstream_commit'] = commit
    regenerated = _zip({'k/fake2027.sty': 'style',
                        'k/guide.tex': 'Submissions must be anonymous.',
                        'k/README': 'regenerated'}, commit.encode())
    _, download = _pages(**{ed['archive']['url']: regenerated})
    kit = reg.fetch_pinned_kit(ed, tmp_path / 'kit', download=download)
    report = reg.check_freshness(ed, download=download)
    assert report['verdict'] == 'current'
    assert report['archive']['note'] == kit['verification_note']
    assert 'every pinned file hash' in kit['verification_note']


def _compile_track_fixture(tmp_path, spelling):
    tmp_path.mkdir(parents=True, exist_ok=True)
    flags = ('main', 'eandd', 'position', 'creativeai', 'education', 'workshop')
    style = ('\\ProvidesPackage{fake2027}\n' + ''.join(
        f'\\newif\\if@{name}\\@{name}' + ('true' if name == 'main' else 'false') + '\n'
        for name in flags) + '\\DeclareOption{eandd}{\\@eanddtrue}\n\\ProcessOptions\\relax\n')
    (tmp_path / 'fake2027.sty').write_text(style)
    (tmp_path / 'main.tex').write_text(
        '\\documentclass{article}\n' + spelling
        + '\n\\begin{document}Actual track.\\end{document}\n')
    result, probe = cf.probe_compile(tmp_path, 'main.tex', 'pdflatex')
    assert result['status'] == 'success', result
    return probe


@tex
@pytest.mark.parametrize('spelling, effective', [
    (r'\iffalse\usepackage[eandd]{fake2027}\fi\usepackage{fake2027}', 'main'),
    (r'\PassOptionsToPackage{eandd}{fake2027}\usepackage{fake2027}', 'eandd'),
])
def test_track_gate_uses_compiled_flags_not_source_option_spelling(tmp_path, spelling, effective):
    probe = _compile_track_fixture(tmp_path, spelling)
    assert cf._compiled_track_row({}, probe, effective)['ok']
    wrong = 'eandd' if effective == 'main' else 'main'
    assert not cf._compiled_track_row({}, probe, wrong)['ok']


@tex
@pytest.mark.parametrize('edition_id, back, failed_check', [
    ('ijcai-2027', r'\section*{Contribution Statement}Author contributions.',
     'content.forbidden-sections'),
    ('aaai-2027', r'\appendix\newpage\section{Appendix}Non-reference content.',
     'content.references-only-extra-pages'),
])
def test_compiled_pdf_gate_enforces_edition_content_rule(synthetic, edition_id, back, failed_check):
    from copy import deepcopy
    root, ed, catalogue = synthetic
    ed = deepcopy(ed); ed['id'] = edition_id
    paper = _write(root / edition_id, _paper(body_paragraphs=1, back=back))
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            freshness_check=lambda ed: {'verdict': 'current'})
    assert next(row['ok'] for row in report['rows'] if row['check'] == 'compile')
    assert failed_check in _failed(report)
    assert not report['submission_ready']


@tex
@pytest.mark.parametrize('profile', [None, 'owner-ccf-a'])
def test_actual_inspected_pdf_bytes_bind_delivery_and_reject_another_valid_pdf(synthetic, profile):
    from types import SimpleNamespace
    from pypdf import PdfReader, PdfWriter
    from lib.longform.documents import pipeline
    root, _, _ = synthetic
    suffix = 'owner' if profile else 'generic'
    report = _check(synthetic, f'inspected-{suffix}', _paper(),
                    keep=root / f'kept-{suffix}')
    assert report['passed'], report['failures']
    inspected = Path(report['pdf']).read_bytes()
    delivered = root / f'delivered-{suffix}.pdf'; delivered.write_bytes(inspected)
    assert len(PdfReader(delivered).pages) > 0
    ctx = {'doc_type': SimpleNamespace(needs_venue=True), 'portfolio_policy': profile,
           'artifacts': {'render': {'pdf_sha256': _sha(inspected), 'conformance': report}}}
    art = {'pdf': str(delivered), 'provenance': {'unsourced': []}}
    assert pipeline._gate_deliver(ctx, art) == []
    assert delivered.read_bytes() == inspected
    replacement = PdfWriter()
    replacement.add_blank_page(width=612, height=792)
    with delivered.open('wb') as stream:
        replacement.write(stream)
    assert len(PdfReader(delivered).pages) == 1
    ctx['artifacts']['render']['pdf_sha256'] = _sha(delivered.read_bytes())
    assert pipeline._gate_deliver(ctx, art) == [
        'delivered PDF differs from the template-conformance PDF']


@tex
def test_concurrent_compiled_track_probes_cannot_cross_accept_other_track(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    jobs = [('main', r'\iffalse\usepackage[eandd]{fake2027}\fi\usepackage{fake2027}'),
            ('eandd', r'\PassOptionsToPackage{eandd}{fake2027}\usepackage{fake2027}')]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(track, pool.submit(_compile_track_fixture, tmp_path / track, spelling))
                   for track, spelling in jobs]
        for track, future in futures:
            probe = future.result()
            assert cf._compiled_track_row({}, probe, track)['ok']
            other = 'eandd' if track == 'main' else 'main'
            assert not cf._compiled_track_row({}, probe, other)['ok']


@tex
@pytest.mark.parametrize('heading', [r'\paragraph{Contribution Statement}',
                                    r'\subparagraph*{\textbf{Contribution Statement}}'])
def test_visible_ijcai_paragraph_heading_blocks_compiled_readiness(synthetic, heading):
    from copy import deepcopy
    from lib.longform.documents.render import pdf_text
    root, ed, catalogue = synthetic
    ed = deepcopy(ed); ed['id'] = 'ijcai-2027'
    label = 'subparagraph' if 'subparagraph' in heading else 'paragraph'
    paper = _write(root / f'ijcai-{label}', _paper(body_paragraphs=1, back=heading + 'Author roles.'))
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            keep=root / f'kept-ijcai-{label}',
                            freshness_check=lambda ed: {'verdict': 'current'})
    assert next(row['ok'] for row in report['rows'] if row['check'] == 'compile')
    assert 'Contribution Statement' in ' '.join(pdf_text(report['pdf']).split())
    assert not report['submission_ready']
    assert 'content.forbidden-sections' in _failed(report)


@tex
def test_visible_aaai_results_inside_bibliography_are_not_reference_only(synthetic):
    from copy import deepcopy
    from lib.longform.documents.render import pdf_text
    root, ed, catalogue = synthetic
    ed = deepcopy(ed); ed['id'] = 'aaai-2027'
    hidden = (r'\begin{thebibliography}{9}\bibitem{x}A. Author. Reference.'
              r'\newpage\section{Additional Results}Visible additional results and prose.'
              r'\end{thebibliography}\end{document}')
    paper = _write(root / 'aaai-results-in-bib', _paper(body_paragraphs=1).replace(
        r'\end{document}', hidden))
    report = cf.check_paper(paper, 'main.tex', ed, catalogue=catalogue,
                            keep=root / 'kept-aaai-results-in-bib',
                            freshness_check=lambda ed: {'verdict': 'current'})
    assert next(row['ok'] for row in report['rows'] if row['check'] == 'compile')
    assert report['pages'] > report['page_limit']
    assert 'Additional Results' in ' '.join(pdf_text(report['pdf']).split())
    assert not report['submission_ready']
    row = next(row for row in report['rows'] if row['check'] == 'content.references-only-extra-pages')
    assert not row['ok'] and row['status'] == 'unverifiable'
