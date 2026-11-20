"""Load both real MV3 packages in Chromium and exercise their popup contract."""
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
from threading import Event, Thread
import zipfile

import pytest
from playwright.sync_api import expect, sync_playwright

from chromium_env import ensure_chromium_env
from tests.support.browser_extension_bundle import build_extension_bundle
from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def proxy_server():
    prefix = '/proxy/15000'
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _reply(self, endpoint, payload):
            requests.append((self.command, self.path))
            if self.path != prefix + endpoint:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply('/api/health', {'ok': True, 'version': 'local-fixture'})

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self._reply('/api/browser/poll', {'commands': []})

    with LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}{prefix}', requests
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def ack_proxy_server():
    prefix = '/proxy//15000'
    armed, dispatched, failed, held, release, empty = (Event() for _ in range(6))
    frames = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != prefix + '/api/health':
                self.send_error(404)
                return
            self.reply(b'{"ok":true,"version":"ack-fixture"}')

        def do_POST(self):
            frame = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            frames.append({'path': self.path, 'frame': frame})
            if self.path != prefix + '/api/browser/poll':
                self.send_error(404)
                return
            if frame['results']:
                if not failed.is_set():
                    failed.set()
                    self.reply(b'not-json')
                    return
                held.set()
                if not release.wait(20):
                    self.send_error(500)
                    return
            elif held.is_set():
                empty.set()
            elif armed.is_set() and not dispatched.is_set():
                dispatched.set()
                self.reply(b'{"commands":[{"id":"fixture-result","type":"list_tabs","params":{}}]}')
                return
            self.reply(b'{"commands":[]}')

    with LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield (f'http://127.0.0.1:{server.server_port}{prefix}',
                   frames, armed, failed, held, release, empty)
        finally:
            release.set()
            server.shutdown()
            thread.join()


def _check_popup_address_forms(page, base):
    field = page.locator('#serverUrl')
    save = page.locator('#saveBtn')
    note = page.locator('#serverNote')
    for pasted, message in [(base, 'Tofu server confirmed.'),
                            (f'  {base}/?view=1#/chat  ', f'Using {base} (Tofu server confirmed).')]:
        field.fill(pasted)
        save.click()
        expect(note).to_have_text(message)
        expect(field).to_have_value(base)
        expect(page.locator('#statusText')).to_have_text('Connected')
        assert page.evaluate("chrome.storage.local.get('serverUrl')") == {'serverUrl': base}

    authority = base.removeprefix('http://')
    for pasted, reason in [(f'ftp://{authority}', 'Unsupported scheme'),
                           (f'http://demo:fixture-only@{authority}', 'user:password@')]:
        field.fill(pasted)
        save.click()
        expect(note).to_contain_text(reason)
        assert 'fixture-only' not in note.inner_text()
        assert page.evaluate("chrome.storage.local.get('serverUrl')") == {'serverUrl': base}


@pytest.mark.parametrize('store', [False, True], ids=['dev', 'store'])
def test_packaged_worker_proxy_result_replays_until_ack(tmp_path, store, ack_proxy_server):
    archive = build_extension_bundle(ROOT, tmp_path / 'checkout', store=store)
    extension = tmp_path / 'extension'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extension)
    base, frames, armed, failed, held, release, empty = ack_proxy_server
    ensure_chromium_env()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(tmp_path / 'profile'), channel='chromium', headless=True,
            args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
        )
        try:
            worker = (browser.service_workers[0] if browser.service_workers
                      else browser.wait_for_event('serviceworker', timeout=15000))
            page = browser.new_page()
            page.goto(f'chrome-extension://{worker.url.split("/")[2]}/popup.html')
            page.wait_for_function("() => document.querySelector('#clientIdText').dataset.full")
            page.locator('#serverUrl').fill(f'  {base}/index.html?view=1#/chat  ')
            page.locator('#saveBtn').click()
            expect(page.locator('#serverNote')).to_have_text(
                f'Using {base} (Tofu server confirmed).')
            page.wait_for_function("async () => (await chrome.runtime.sendMessage({type:'getStatus'})).connected")
            client_id = page.evaluate("chrome.runtime.sendMessage({type:'getStatus'})")['clientId']
            armed.set()
            worker.evaluate('() => bridgeTransport.nudge(bridgeConnection())')
            assert failed.wait(10), frames
            page.wait_for_function("""async () => {
                const s = await chrome.runtime.sendMessage({type:'getStatus'});
                return !s.connected && s.resultQueue === 1;
            }""")
            worker.evaluate('() => bridgeTransport.nudge(bridgeConnection())')
            assert held.wait(10), frames
            assert page.evaluate("chrome.runtime.sendMessage({type:'getStatus'})")['resultQueue'] == 1
            release.set()
            page.wait_for_function("""async () => {
                const s = await chrome.runtime.sendMessage({type:'getStatus'});
                return s.connected && s.resultQueue === 0;
            }""")
            worker.evaluate('() => bridgeTransport.nudge(bridgeConnection())')
            assert empty.wait(10), frames
            worker.evaluate('() => stopPolling()')
            result_frames = [row['frame'] for row in frames if row['frame']['results']]
            assert len(result_frames) == 2
            assert result_frames[0]['results'] == result_frames[1]['results']
            assert len(result_frames[0]['results']) == 1
            result = result_frames[0]['results'][0]
            assert result['id'] == 'fixture-result'
            assert result.get('error') is None
            assert isinstance(result['result'], list)
            status = page.evaluate("chrome.runtime.sendMessage({type:'getStatus'})")
            assert status['commandsExecuted'] == 1
            assert status['commandsFailed'] == 0
            assert all(row['clientId'] == client_id for row in result_frames)
            assert {row['path'] for row in frames} == {'/proxy//15000/api/browser/poll'}
            assert page.evaluate("chrome.storage.local.get('serverUrl')") == {'serverUrl': base}
        finally:
            release.set()
            browser.close()


@pytest.mark.parametrize('store', [False, True], ids=['dev', 'store'])
def test_packaged_worker_registers_and_answers_popup(tmp_path, store, proxy_server):
    archive = build_extension_bundle(ROOT, tmp_path / 'checkout', store=store)
    extension = tmp_path / 'extension'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extension)
    ensure_chromium_env()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(tmp_path / 'profile'), channel='chromium', headless=True,
            args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
        )
        try:
            worker = (browser.service_workers[0] if browser.service_workers
                      else browser.wait_for_event('serviceworker', timeout=15000))
            extension_id = worker.url.split('/')[2]
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'chrome-extension://{extension_id}/popup.html')
            # Chrome storage callbacks finish after synchronous registration.
            # Wait for the public readiness signal, not an arbitrary sleep.
            page.wait_for_function(
                "() => document.querySelector('#clientIdText').dataset.full",
                timeout=10000,
            )
            status = page.evaluate("chrome.runtime.sendMessage({type: 'getStatus'})")
            assert status['inflight'] == status['resultQueue'] == 0
            assert status['commandsExecuted'] == status['commandsFailed'] == 0
            assert status['pollActive'] is False
            assert status['connected'] is False
            assert status['clientId'] == page.locator('#clientIdText').get_attribute('data-full')
            assert page.locator('#repairBtn').count() == 1
            for size in ('16', '48', '128'):
                image = page.locator('body').evaluate(
                    """async (body, size) => {
                        const icon = new Image();
                        icon.src = chrome.runtime.getURL(`icon${size}.png`);
                        await icon.decode();
                        body.append(icon);
                        return [icon.naturalWidth, icon.naturalHeight];
                    }""",
                    size,
                )
                assert image == [int(size), int(size)]
            base, requests = proxy_server
            _check_popup_address_forms(page, base)
            assert requests.count(('GET', '/proxy/15000/api/health')) == 2
            assert {path for method, path in requests if method == 'POST'} == {'/proxy/15000/api/browser/poll'}
            assert errors == []
        finally:
            browser.close()
