"""tests/test_overleaf_preview.py — Overleaf live-preview pipeline + routes.

Pins the compile-and-locate contract behind the chat PDF panel:

- ``needle_to_line`` maps the agent's inserted text to a 1-based source
  line (exact first, whitespace-tolerant fallback).
- ``parse_locate_result`` normalizes every SyncTeX locate payload shape.
- ``run_preview`` drives the MCP bridge in order (read_file → download_pdf
  → locate_in_pdf), stages a non-empty PDF, and prunes the cache.
- The v1 routes validate the project id, surface PreviewError as 502, and
  serve the staged PDF only after a preview.
"""

import json
import os
import unittest
from unittest import mock

import pytest

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit

from lib import overleaf_preview as op  # noqa: E402

_PID = 'a' * 24
_TEX = '\\section{Intro}\nHello.\n\\section{Method}\n  \\subsection{A}\n'


class FakeBridge:
    """Stands in for the MCP bridge; download_pdf writes bytes to disk."""

    def __init__(self, calls=None, file_text=_TEX,
                 locate_raw=None):
        self.calls = calls if calls is not None else []
        self.file_text = file_text
        self.locate_raw = locate_raw if locate_raw is not None else json.dumps(
            {'page': 3, 'rects': [
                {'page': 3, 'h': 72.0, 'v': 120.0,
                 'width': 300.0, 'height': 14.0}]})

    def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == 'mcp__overleaf__read_file':
            return self.file_text
        if name == 'mcp__overleaf__download_pdf':
            with open(args['output_path'], 'wb') as fh:
                fh.write(b'%PDF-1.7 fake')
            return 'saved'
        if name == 'mcp__overleaf__locate_in_pdf':
            return self.locate_raw
        raise ValueError(f'unknown tool {name}')


class NeedleToLineTest(unittest.TestCase):

    def test_exact_substring_match(self):
        self.assertEqual(op.needle_to_line(_TEX, 'Hello.'), 2)
        self.assertEqual(
            op.needle_to_line(_TEX, '\\section{Method}\n  \\subsection{A}'), 3)

    def test_whitespace_tolerant_first_line(self):
        # Block drifted (file changed since) but the first line still
        # identifies where the edit landed.
        self.assertEqual(op.needle_to_line(
            _TEX, '\\section{Method}\nparagraph the file no longer has'), 3)
        # Agent's new_string lost the leading indent the file carries.
        self.assertEqual(op.needle_to_line(_TEX, '\\subsection{A}\nnew'), 4)

    def test_no_match_returns_none(self):
        self.assertIsNone(op.needle_to_line(_TEX, 'never appears'))
        self.assertIsNone(op.needle_to_line(_TEX, ''))
        self.assertIsNone(op.needle_to_line(_TEX, '   \n  '))


class ParseLocateResultTest(unittest.TestCase):

    def test_envelope_with_rects(self):
        raw = json.dumps({'page': 5, 'rects': [
            {'page': 5, 'h': 1, 'v': 2, 'width': 3, 'height': 4}]})
        out = op.parse_locate_result(raw)
        self.assertEqual(out['page'], 5)
        self.assertEqual(len(out['rects']), 1)

    def test_bare_list_and_single_rect(self):
        rect = {'page': 2, 'h': 1, 'v': 2, 'width': 3, 'height': 4}
        out = op.parse_locate_result(json.dumps([rect]))
        self.assertEqual(out['page'], 2)
        out = op.parse_locate_result(json.dumps(rect))
        self.assertEqual(out['page'], 2)
        self.assertEqual(len(out['rects']), 1)

    def test_garbage_and_missing_keys(self):
        self.assertEqual(op.parse_locate_result('not json'),
                         {'page': None, 'rects': []})
        out = op.parse_locate_result(json.dumps({'rects': [{'h': 1}]}))
        self.assertEqual(out['rects'], [])


class RunPreviewTest(unittest.TestCase):

    def _run(self, bridge, **kwargs):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            return op.run_preview(bridge, _PID, base=tmp, **kwargs), tmp

    def test_bad_project_id_rejected(self):
        with self.assertRaises(op.PreviewError):
            op.run_preview(FakeBridge(), 'not-hex', base='.')

    def test_full_pipeline_order_and_result(self):
        bridge = FakeBridge()
        result, tmp = self._run(bridge, file='main.tex',
                                needle='\\section{Method}')
        self.assertEqual([c[0] for c in bridge.calls], [
            'mcp__overleaf__read_file',
            'mcp__overleaf__download_pdf',
            'mcp__overleaf__locate_in_pdf',
        ])
        self.assertEqual(bridge.calls[2][1]['line'], 3)
        self.assertTrue(result['pdf_path'].endswith(f'{_PID}.pdf'))
        self.assertEqual(result['page'], 3)
        self.assertEqual(len(result['rects']), 1)
        self.assertGreater(result['compiled_at'], 0)

    def test_explicit_line_skips_read(self):
        bridge = FakeBridge()
        self._run(bridge, file='main.tex', line=7)
        self.assertEqual([c[0] for c in bridge.calls], [
            'mcp__overleaf__download_pdf',
            'mcp__overleaf__locate_in_pdf',
        ])
        self.assertEqual(bridge.calls[1][1]['line'], 7)

    def test_no_location_skips_locate(self):
        bridge = FakeBridge()
        result, _ = self._run(bridge)
        self.assertEqual([c[0] for c in bridge.calls],
                         ['mcp__overleaf__download_pdf'])
        self.assertIsNone(result['page'])
        self.assertEqual(result['rects'], [])

    def test_empty_pdf_is_a_preview_error(self):
        class EmptyBridge(FakeBridge):
            def call_tool(self, name, args):
                if name == 'mcp__overleaf__download_pdf':
                    open(args['output_path'], 'wb').close()
                    return 'empty'
                return super().call_tool(name, args)
        with self.assertRaises(op.PreviewError) as ctx:
            self._run(EmptyBridge())
        self.assertIn('no PDF', str(ctx.exception))

    def test_bridge_failure_is_presentable(self):
        class DownBridge:
            def call_tool(self, name, args):
                raise RuntimeError('upstream 500')
        with self.assertRaises(op.PreviewError) as ctx:
            self._run(DownBridge())
        self.assertIn('download_pdf failed', str(ctx.exception))

    def test_cache_pruned_to_keep(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(op._CACHE_KEEP + 3):
                path = os.path.join(tmp, f'{i:024x}.pdf')
                with open(path, 'wb') as fh:
                    fh.write(b'x')
                os.utime(path, (i, i))  # oldest first
            op.run_preview(FakeBridge(), _PID, base=tmp)
            remaining = sorted(os.listdir(tmp))
            self.assertEqual(len(remaining), op._CACHE_KEEP)
            self.assertIn(f'{_PID}.pdf', remaining)


class RouteTest(unittest.TestCase):
    """POST compile+locate, GET the staged PDF, input validation."""

    def setUp(self):
        from lib.api_keys import create_key
        _row, self.token = create_key(
            owner_user_id=1, name='overleaf-route-probe',
            scopes=[], admin=True)
        from quart import Quart
        self.app = Quart(__name__, static_folder=None)
        self.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request)
        self.app.before_request(bearer_auth_before_request)
        self.app.after_request(attach_rate_headers)
        from routes.api_v1.overleaf import api_v1_overleaf_bp
        self.app.register_blueprint(api_v1_overleaf_bp)
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(op, 'cache_dir',
                                    lambda: self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = FakeBridge()
        bridge_patcher = mock.patch('lib.mcp.get_bridge',
                                    lambda: self.bridge)
        bridge_patcher.start()
        self.addCleanup(bridge_patcher.stop)

    def _hdrs(self):
        return {'Authorization': f'Bearer {self.token}'}

    def test_preview_then_serve_pdf(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post('/api/v1/overleaf/preview',
                               headers=self._hdrs(),
                               json={'project_id': _PID,
                                     'file': 'main.tex',
                                     'needle': '\\section{Method}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertEqual(body['page'], 3)
            self.assertEqual(len(body['rects']), 1)
            self.assertIn(f'/api/v1/overleaf/preview/{_PID}/pdf?v=',
                          body['pdf_url'])
            r2 = await cli.get(body['pdf_url'], headers=self._hdrs())
            self.assertEqual(r2.status_code, 200)
            self.assertEqual((await r2.get_data())[:5], b'%PDF-')
        from tests.test_api_v1_integration import _run
        _run(go())

    def test_invalid_project_id_400(self):
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/overleaf/preview', headers=self._hdrs(),
                json={'project_id': '../etc/passwd'})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_bridge_failure_502(self):
        class DownBridge:
            def call_tool(self, name, args):
                raise RuntimeError('overleaf down')
        self.bridge = DownBridge()
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/overleaf/preview', headers=self._hdrs(),
                json={'project_id': _PID})
            self.assertEqual(r.status_code, 502)
            body = await r.get_json()
            self.assertFalse(body['ok'])
        _run(go())

    def test_pdf_404_before_first_preview(self):
        from tests.test_api_v1_integration import _run
        async def go():
            r = await self.app.test_client().get(
                f'/api/v1/overleaf/preview/{_PID}/pdf', headers=self._hdrs())
            self.assertEqual(r.status_code, 404)
        _run(go())


if __name__ == '__main__':
    unittest.main()
