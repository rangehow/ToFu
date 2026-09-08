"""tests/test_latex_preview.py — LOCAL LaTeX live-preview pipeline + routes.

The local twin of test_overleaf_preview.py: an agent edit to a workspace
``.tex`` file (no Overleaf involved) recompiles the paper with the host TeX
engine and SyncTeX points the chat panel at the edited spot.

Pins:

- ``resolve_tex_path`` maps the model-written path (relative / absolute /
  ``rootname:rel``) onto an in-root ``.tex`` file and refuses escapes.
- ``find_main_document`` picks the compile entry point (self → directory
  candidates → name preference + mtime).
- ``pick_engine`` selects xelatex for Unicode packages, errors when no TeX.
- ``parse_synctex`` reads .synctex.gz WITHOUT a synctex CLI: Input table
  normalization, page tracking via ``{N``/``}N``, nearest-line selection,
  sp→pt conversion, baseline→top rect correction.
- ``run_local_preview`` end-to-end against the real engine (skipUnless):
  compiles into the per-key cache dir, never pollutes the source tree,
  locates the needle in a subfile, prunes the cache.
- The v1 routes validate input, surface PreviewError as 502, and serve the
  staged PDF only after a preview.
"""

import gzip
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pytest

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit

from lib import latex_preview as lp  # noqa: E402

_HAVE_PDFLATEX = shutil.which('pdflatex') is not None

# Minimal hand-built SyncTeX sample (mirrors a real pdflatex probe):
#   tag 2 = /proj/sub.tex; page 1 holds the line-5 hbox + an x record,
#   page 2 holds the line-25 hbox. Coordinates are absolute page sp.
_SYNCTEX_SAMPLE = """SyncTeX Version:1
Input:1:/proj/./main.tex
Input:2:/proj/sub.tex
Output:pdf
Magnification:1000
Unit:1
X Offset:0
Y Offset:0
Content:
!100
{1
[1,1:4736286,46220574:26673152,41484288,0
(2,5:6553600,13107200:14417920,455111,65536
x2,5:6881280,13107200
)
]
!50
}1
{2
[1,10:4736286,46220574:26673152,41484288,0
(2,25:6553600,13107200:14417920,589824,131072
)
]
!40
}2
"""


def _write_synctex(dirpath: str, text: str = _SYNCTEX_SAMPLE) -> str:
    path = os.path.join(dirpath, 'main.synctex.gz')
    with gzip.open(path, 'wt', encoding='utf-8') as fh:
        fh.write(text)
    return path


def _patch_roots(tmp: str):
    """Point the primary root + registry at ``tmp`` for resolve_tex_path."""
    patchers = [
        mock.patch('lib.project_mod.config.get_project_path', lambda: tmp),
        mock.patch('lib.project_mod.config.get_roots', lambda: {}),
        mock.patch('lib.project_mod.config.get_conv_roots',
                   lambda _conv: {}),
    ]
    for p in patchers:
        p.start()
    return patchers


class ResolveTexPathTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.tex = os.path.join(self.root, 'paper.tex')
        with open(self.tex, 'w') as fh:
            fh.write('\\documentclass{article}\n')
        self._patchers = _patch_roots(self.root)
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    def test_relative_resolves_under_primary_root(self):
        self.assertEqual(lp.resolve_tex_path('paper.tex'), self.tex)

    def test_absolute_inside_root_ok(self):
        self.assertEqual(lp.resolve_tex_path(self.tex), self.tex)

    def test_outside_root_refused(self):
        outside = tempfile.NamedTemporaryFile(suffix='.tex', delete=False)
        outside.write(b'x')
        outside.close()
        self.addCleanup(lambda: os.path.exists(outside.name)
                        and os.unlink(outside.name))
        with self.assertRaises(lp.PreviewError) as ctx:
            lp.resolve_tex_path(outside.name)
        self.assertIn('outside the workspace roots', str(ctx.exception))

    def test_non_tex_refused(self):
        other = os.path.join(self.root, 'notes.txt')
        with open(other, 'w') as fh:
            fh.write('x')
        with self.assertRaises(lp.PreviewError):
            lp.resolve_tex_path('notes.txt')

    def test_missing_file_refused(self):
        with self.assertRaises(lp.PreviewError) as ctx:
            lp.resolve_tex_path('nope.tex')
        self.assertIn('file not found', str(ctx.exception))

    def test_namespaced_path_uses_resolve_base(self):
        sub = os.path.join(self.root, 'chapters')
        os.makedirs(sub)
        target = os.path.join(sub, 'intro.tex')
        with open(target, 'w') as fh:
            fh.write('x')
        self.assertEqual(
            lp.resolve_tex_path(f'{os.path.basename(self.root)}:chapters/intro.tex'),
            target)


class FindMainDocumentTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _write(self, name: str, text: str, mtime: float | None = None) -> str:
        path = os.path.join(self.root, name)
        with open(path, 'w') as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_self_with_documentclass_wins(self):
        main = self._write('main.tex', '\\documentclass{article}\n')
        self.assertEqual(lp.find_main_document(main), main)

    def test_subfile_routes_to_main(self):
        main = self._write('main.tex', '\\documentclass{article}\n\\input{sub}\n')
        sub = self._write('sub.tex', 'Just content.\n')
        self.assertEqual(lp.find_main_document(sub), main)

    def test_name_preference_then_mtime(self):
        older = self._write('zzz.tex', '\\documentclass{article}\n', mtime=100)
        main = self._write('main.tex', '\\documentclass{article}\n', mtime=50)
        sub = self._write('sub.tex', 'content\n')
        # 'main' beats a newer non-preferred name.
        self.assertEqual(lp.find_main_document(sub), main)
        os.unlink(main)
        newer = self._write('aaa.tex', '\\documentclass{article}\n', mtime=200)
        self.assertEqual(lp.find_main_document(sub), newer)
        self.assertNotEqual(newer, older)

    def test_no_candidate_falls_back_to_edited(self):
        sub = self._write('lone.tex', 'content\n')
        self.assertEqual(lp.find_main_document(sub), sub)


class PickEngineTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, text: str) -> str:
        path = os.path.join(self._tmp.name, 'main.tex')
        with open(path, 'w') as fh:
            fh.write(text)
        return path

    def test_pdflatex_default(self):
        path = self._write('\\documentclass{article}\n')
        with mock.patch('shutil.which', side_effect=lambda n: f'/bin/{n}'):
            self.assertEqual(lp.pick_engine(path), '/bin/pdflatex')

    def test_xelatex_for_unicode_packages(self):
        for hint in ('\\usepackage{fontspec}\n', '\\usepackage{xeCJK}\n',
                     '\\documentclass{ctexart}\n', '\\usepackage[UTF8]{ctex}\n'):
            path = self._write(hint)
            with mock.patch('shutil.which', side_effect=lambda n: f'/bin/{n}'):
                self.assertEqual(lp.pick_engine(path), '/bin/xelatex', hint)

    def test_no_engine_is_presentable(self):
        path = self._write('\\documentclass{article}\n')
        with mock.patch('shutil.which', return_value=None):
            with self.assertRaises(lp.PreviewError) as ctx:
                lp.pick_engine(path)
            self.assertIn('no local TeX engine', str(ctx.exception))


class ParseSynctexTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gz = _write_synctex(self._tmp.name)

    def test_nearest_line_and_rect_math(self):
        out = lp.parse_synctex(self.gz, target_file='/proj/sub.tex',
                               target_line=6)
        self.assertEqual(out['page'], 1)
        self.assertEqual(len(out['rects']), 1)
        rect = out['rects'][0]
        self.assertEqual(rect['h'], 100.0)
        # v is the box top: baseline 200pt − height (455111+65536)sp.
        self.assertAlmostEqual(rect['v'], 200 - (455111 + 65536) / 65536.0,
                               places=2)
        self.assertAlmostEqual(rect['width'], 220.0, places=2)
        self.assertAlmostEqual(rect['height'], (455111 + 65536) / 65536.0,
                               places=2)

    def test_second_page_record(self):
        out = lp.parse_synctex(self.gz, target_file='/proj/sub.tex',
                               target_line=24)
        self.assertEqual(out['page'], 2)
        self.assertEqual(out['rects'][0]['page'], 2)

    def test_tie_breaks_to_first_record(self):
        out = lp.parse_synctex(self.gz, target_file='/proj/sub.tex',
                               target_line=15)
        self.assertEqual(out['page'], 1)

    def test_input_path_normalized(self):
        # 'Input:1:/proj/./main.tex' must match the resolved absolute path.
        out = lp.parse_synctex(self.gz, target_file='/proj/main.tex',
                               target_line=1)
        self.assertEqual(out['page'], 1)

    def test_unknown_file_and_bad_gzip(self):
        out = lp.parse_synctex(self.gz, target_file='/proj/ghost.tex',
                               target_line=1)
        self.assertEqual(out, {'page': None, 'rects': []})
        bad = os.path.join(self._tmp.name, 'bad.synctex.gz')
        with open(bad, 'wb') as fh:
            fh.write(b'not gzip')
        self.assertEqual(
            lp.parse_synctex(bad, target_file='/proj/sub.tex', target_line=1),
            {'page': None, 'rects': []})


@unittest.skipUnless(_HAVE_PDFLATEX, 'pdflatex not installed')
class RunLocalPreviewE2ETest(unittest.TestCase):
    """Real-engine compile: cache layout, clean source tree, subfile SyncTeX."""

    _MAIN = ('\\documentclass{article}\n\\begin{document}\n'
             'Intro paragraph.\n\n\\input{sub}\n\\end{document}\n')
    _SUB = 'Subfile target sentence MARKER here.\n'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, 'proj')
        os.makedirs(self.root)
        self.main = os.path.join(self.root, 'main.tex')
        self.sub = os.path.join(self.root, 'sub.tex')
        with open(self.main, 'w') as fh:
            fh.write(self._MAIN)
        with open(self.sub, 'w') as fh:
            fh.write(self._SUB)
        self.cache = os.path.join(self._tmp.name, 'cache')
        self._patchers = _patch_roots(self.root)
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    def test_compile_locates_subfile_edit(self):
        result = lp.run_local_preview('sub.tex', needle='MARKER here.',
                                      base=self.cache)
        self.assertTrue(os.path.isfile(result['pdf_path']))
        self.assertTrue(result['pdf_path'].endswith(
            os.path.join(result['pdf_key'], 'current.pdf')))
        self.assertEqual(result['page'], 1)
        self.assertEqual(len(result['rects']), 1)
        rect = result['rects'][0]
        self.assertGreater(rect['v'], 0)
        self.assertGreater(rect['height'], 0)
        # Source tree stays clean: every artifact lives in the cache dir.
        self.assertEqual(sorted(os.listdir(self.root)),
                         ['main.tex', 'sub.tex'])
        # A second run reuses the same key (per-document serialization).
        again = lp.run_local_preview('sub.tex', needle='MARKER here.',
                                     base=self.cache)
        self.assertEqual(again['pdf_key'], result['pdf_key'])
        self.assertEqual(lp.cached_pdf(result['pdf_key'], base=self.cache),
                         result['pdf_path'])

    def test_compile_failure_surfaces_log_tail(self):
        with open(self.main, 'w') as fh:
            fh.write('\\documentclass{article}\n\\begin{document}\n'
                     '\\undefinedcommandboom\n\\end{document}\n')
        with self.assertRaises(lp.PreviewError) as ctx:
            lp.run_local_preview('main.tex', base=self.cache)
        self.assertIn('compile failed', str(ctx.exception))

    def test_cache_pruned_to_keep(self):
        for i in range(lp._CACHE_KEEP + 3):
            d = os.path.join(self.cache, f'stale{i:016x}')
            os.makedirs(d)
            with open(os.path.join(d, 'current.pdf'), 'wb') as fh:
                fh.write(b'x')
            os.utime(d, (i, i))  # oldest first
        lp.run_local_preview('sub.tex', base=self.cache)
        remaining = [n for n in os.listdir(self.cache)
                     if os.path.isdir(os.path.join(self.cache, n))]
        self.assertEqual(len(remaining), lp._CACHE_KEEP)


class RouteTest(unittest.TestCase):
    """POST compile+locate, GET the staged PDF, input validation."""

    def setUp(self):
        from lib.api_keys import create_key
        _row, self.token = create_key(
            owner_user_id=1, name='latex-route-probe', scopes=[], admin=True)
        from quart import Quart
        self.app = Quart(__name__, static_folder=None)
        self.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request)
        self.app.before_request(bearer_auth_before_request)
        self.app.after_request(attach_rate_headers)
        from routes.api_v1.latex import api_v1_latex_bp
        self.app.register_blueprint(api_v1_latex_bp)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key = 'a' * 16
        self.pdf = os.path.join(self._tmp.name, self.key, 'current.pdf')
        os.makedirs(os.path.dirname(self.pdf))
        with open(self.pdf, 'wb') as fh:
            fh.write(b'%PDF-1.7 fake')
        run_patcher = mock.patch(
            'routes.api_v1.latex.run_local_preview',
            lambda *_a, **k: {
                'pdf_path': self.pdf,
                'pdf_key': self.key,
                'compiled_at': 1.0,
                'page': 2,
                'rects': [{'page': 2, 'h': 10.0, 'v': 20.0,
                           'width': 30.0, 'height': 4.0}],
            })
        run_patcher.start()
        self.addCleanup(run_patcher.stop)
        cache_patcher = mock.patch.object(
            lp, 'cache_dir', lambda: self._tmp.name)
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    def _hdrs(self):
        return {'Authorization': f'Bearer {self.token}'}

    def test_preview_then_serve_pdf(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post('/api/v1/latex/preview',
                               headers=self._hdrs(),
                               json={'path': 'main.tex',
                                     'needle': 'MARKER'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(body['pdf_key'], self.key)
            self.assertEqual(body['page'], 2)
            self.assertEqual(len(body['rects']), 1)
            self.assertIn(f'/api/v1/latex/preview/{self.key}/pdf?v=',
                          body['pdf_url'])
            r2 = await cli.get(body['pdf_url'], headers=self._hdrs())
            self.assertEqual(r2.status_code, 200)
            self.assertEqual((await r2.get_data())[:5], b'%PDF-')
        from tests.test_api_v1_integration import _run
        _run(go())

    def test_missing_path_400(self):
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/latex/preview', headers=self._hdrs(), json={})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_bad_line_400(self):
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/latex/preview', headers=self._hdrs(),
                json={'path': 'main.tex', 'line': 0})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_preview_error_502(self):
        def _boom(*_a, **_k):
            raise lp.PreviewError('no local TeX engine found')
        patcher = mock.patch('routes.api_v1.latex.run_local_preview', _boom)
        patcher.start()
        self.addCleanup(patcher.stop)
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/latex/preview', headers=self._hdrs(),
                json={'path': 'main.tex'})
            self.assertEqual(r.status_code, 502)
            body = await r.get_json()
            self.assertFalse(body['ok'])
            self.assertIn('no local TeX engine', str(body))
        _run(go())

    def test_pdf_404_for_unknown_key(self):
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().get(
                f"/api/v1/latex/preview/{'b' * 16}/pdf",
                headers=self._hdrs())
            self.assertEqual(r.status_code, 404)
            # Invalid key shapes never reach the filesystem.
            r2 = await self.app.test_client().get(
                '/api/v1/latex/preview/..%2F..%2Fetc/pdf',
                headers=self._hdrs())
            self.assertIn(r2.status_code, (404, 400))
        _run(go())


if __name__ == '__main__':
    unittest.main()
