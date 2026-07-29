"""Frontend guard: a QR recovered from terminal output must actually RENDER
inside the run_command block, and must not perturb the block when absent.

Renders ``_renderUnifiedToolLine`` (static/js/ui/tool_rounds.js) through node
with the same minimal global surface the wire-parity harness uses.

The invariant that matters: a scan-to-login code must be visible WITHOUT the
user expanding anything. The output ``<pre>`` is collapsed by default AND is
``white-space: pre-wrap`` + ``word-break: break-all`` — the very thing that
destroys block-art QR rows — so the image has to sit outside it. A test that
merely asserted "the html contains the uri" would pass even if the img were
nested inside the collapsed pane, so the position is asserted explicitly.

Run: pytest tests/test_frontend_qr_render.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOL_ROUNDS = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'

_URI = 'data:image/png;base64,QQQQ'


def _render(round_obj: dict) -> str:
    if shutil.which('node') is None:
        pytest.skip('node is required for the QR render guard')
    harness = textwrap.dedent('''
        const fs = require('fs');
        global.escapeHtml = (s) => String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        global.t = (k, d) => (d || k);
        global.Icon = (n, s) => '<ICON>';
        global.renderMarkdown = (s) => s;
        global._shortUrl = (u) => u;
        global.formatNumber = (n) => String(n);
        global.window = { location: { href: 'http://localhost/' },
                          addEventListener() {}, removeEventListener() {} };
        global.document = { addEventListener() {}, removeEventListener() {},
          createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }) };
        // NOTE: with `node -e`, args after `--` start at argv[1] (there is no
        // script-path entry), unlike a script file where they start at argv[2].
        eval(fs.readFileSync(process.argv[1], 'utf8'));
        const round = JSON.parse(process.argv[2]);
        // isSearching mirrors the real dispatcher: an in-flight round routes to
        // the running-state renderer, a settled one to the done renderer.
        const isSearching = round.status === 'searching';
        process.stdout.write(_renderUnifiedToolLine(round, isSearching));
        process.exit(0);
    ''')
    proc = subprocess.run(
        ['node', '-e', harness, '--', str(TOOL_ROUNDS), json.dumps(round_obj)],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f'render harness failed:\n{proc.stderr[:2000]}')
    return proc.stdout


def _cmd_round(**meta_extra) -> dict:
    meta = {'toolName': 'run_command', 'command': 'gh auth login',
            'output': 'Scan the QR below\n<blockart>', 'exitCode': '0'}
    meta.update(meta_extra)
    return {'roundNum': 4, 'toolName': 'run_command', 'status': 'done',
            'query': 'gh auth login', 'results': [meta]}


def _running_round(**round_extra) -> dict:
    """A run_command STILL IN FLIGHT — the state a scan-to-login command sits
    in while it blocks waiting for the scan. Note the descriptors live on the
    ROUND here: tool_progress delivers them before any `results` exists."""
    r = {'roundNum': 4, 'toolName': 'run_command', 'status': 'searching',
         'query': 'gh auth login',
         '_partialOutput': 'Open this QR:\n<blockart>\nWaiting for scan...',
         'results': []}
    r.update(round_extra)
    return r


class TestQrRendersWhileCommandStillRunning:
    """The acceptance criterion for scan-to-login: the code is visible DURING
    the wait. A QR that only appears when the command exits is useless — the
    authorization window has closed by then."""

    def test_running_command_renders_its_qr(self):
        html = _render(_running_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-cmd-running' in html, 'not the running-state block'
        assert 'ptool-qr-strip' in html, (
            'a still-running command did not render its QR — the user cannot '
            'scan it until the command exits, which is too late'
        )
        assert f'src="{_URI}"' in html

    def test_running_qr_precedes_and_stays_out_of_the_live_pane(self):
        """The live pane is `pre-wrap` + `word-break: break-all` — the exact
        styling that destroys block-art QR rows. The image must sit before it,
        not inside it."""
        html = _render(_running_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        qr_at = html.index('ptool-qr-strip')
        live_at = html.index('ptool-cmd-output-live')
        assert qr_at < live_at
        assert 'ptool-qr' not in html[live_at:]

    def test_running_without_qr_is_unchanged(self):
        html = _render(_running_round())
        assert 'ptool-cmd-running' in html
        assert 'ptool-qr' not in html

    def test_qr_shows_before_any_output_has_streamed(self):
        """A CLI can print the QR as its very first bytes; the strip must not
        depend on the live-output pane existing."""
        html = _render(_running_round(_partialOutput='', qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert 'ptool-cmd-output-live' not in html


class TestQrRendersInTerminalBlock:
    def test_qr_image_is_emitted(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert f'src="{_URI}"' in html
        assert '<img' in html

    def test_qr_sits_outside_the_collapsed_output_pane(self):
        """The whole point: the code must be scannable without clicking
        "Show output". ``.ptool-cmd-output`` is ``display:none`` until
        expanded (and re-wraps text), so an img inside it is useless."""
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        qr_at = html.index('ptool-qr-strip')
        out_at = html.index('ptool-cmd-output-wrap')
        assert qr_at < out_at, 'QR must precede the collapsible output wrapper'
        tail = html[html.index('ptool-cmd-output-wrap'):]
        assert 'ptool-qr' not in tail, 'QR must not be nested in the output pane'

    def test_no_qr_field_leaves_no_qr_markup(self):
        html = _render(_cmd_round())
        assert 'ptool-qr' not in html
        assert 'ptool-cmd-block' in html  # the block itself still renders

    def test_descriptor_without_uri_is_skipped(self):
        """The IndexedDB cache strips multi-MB ``uri`` fields on write; a
        descriptor that lost its uri must degrade to "no image", never to a
        broken <img src="">."""
        html = _render(_cmd_round(qrImages=[{'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr' not in html
        assert 'src=""' not in html

    def test_multiple_codes_all_render(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'},
            {'uri': 'data:image/png;base64,WWWW', 'format': 'png',
             'filename': 'qr-2.png'}]))
        assert html.count('<img') == 2

    def test_uri_is_escaped_not_interpolated_raw(self):
        """A data URI reaches the DOM as an attribute — a quote in it must not
        be able to break out and inject markup."""
        html = _render(_cmd_round(qrImages=[
            {'uri': 'data:image/png;base64,A"><script>x</script>',
             'format': 'png', 'filename': 'q.png'}]))
        assert '<script>' not in html
        assert '&quot;' in html or '&gt;' in html

    def test_failed_command_still_shows_its_qr(self):
        """Device-code flows often print the QR and then exit non-zero while
        waiting; the code must still be shown."""
        html = _render(_cmd_round(exitCode='1', qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert 'ptool-cmd-err' in html
