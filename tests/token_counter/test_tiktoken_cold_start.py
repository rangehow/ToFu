"""A cold Tier 2 token count never waits on the network (TIKTOKEN-COLD, 2026-11-13).

Before the vocabularies shipped with the install, a cold count could block:
- a missing vocabulary was downloaded by ``tiktoken.get_encoding`` through a
  raw ``requests.get`` with no timeout;
- the download ran under one module-wide encoder lock, which cached hits also
  took;
- with black-holed egress, the first count and every later count in the
  process blocked indefinitely;
- refused egress cost one attempt per encoding per process;
- ``supports()`` loaded cl100k for every model.

These specs pin the shipped-vocabulary contract (docs/modules/
context_engineering.md, *Shipped tokenizer vocabularies*):
- a cold start after a reboot (empty tiktoken cache, no egress) counts exactly
  and connects nowhere;
- concurrent cold callers share one load;
- a load that never returns blocks neither cached hits nor the other encoding.

The seams are implementation-neutral, so the same specs fail on the old
download path: environment proxies pointing at a fake proxy, tiktoken's own
cache directory and ``tiktoken.Encoding`` construction.
"""
from __future__ import annotations

import hashlib
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import tiktoken
import tiktoken.core
import tiktoken.load
import tiktoken.registry
from tiktoken_ext import openai_public

import lib.token_counter.api as token_api
import lib.token_counter.native as native
import lib.token_counter.tiktoken_counter as tc
from lib.http_transport.client import _DEFAULT_TIMEOUT as NETWORK_DEADLINE_S
from tofu_db._loopback import bind_ephemeral_loopback

pytestmark = pytest.mark.unit

VOCABULARY_DIR = Path(tc.__file__).with_name('vocabularies')
ENCODINGS = ('cl100k_base', 'o200k_base')
TEXT = 'Cold token counts never wait on the network: 冷启动计数 🚀.'
MESSAGES = [{'role': 'user', 'content': TEXT}]


def _seed_tiktoken_cache(cache_dir: Path, names) -> None:
    """Place shipped files where tiktoken's own loader finds them offline."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        url = f'https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken'
        shutil.copyfile(VOCABULARY_DIR / f'{name}.tiktoken',
                        cache_dir / hashlib.sha1(url.encode()).hexdigest())


def _refuse_download(blobpath):
    raise AssertionError(f'tiktoken tried to download {blobpath}')


@pytest.fixture(scope='module')
def reference(tmp_path_factory):
    """tiktoken's own encodings, built offline from the shipped files."""
    cache_dir = tmp_path_factory.mktemp('tiktoken-reference-cache')
    _seed_tiktoken_cache(cache_dir, ENCODINGS)
    patch = pytest.MonkeyPatch()
    try:
        patch.setenv('TIKTOKEN_CACHE_DIR', str(cache_dir))
        patch.setattr(tiktoken.load, 'read_file', _refuse_download)
        return {name: tiktoken.Encoding(**openai_public.ENCODING_CONSTRUCTORS[name]())
                for name in ENCODINGS}
    finally:
        patch.undo()


class _FakeEgressProxy:
    """The only egress route: it refuses (403) or black-holes every CONNECT."""

    def __init__(self, mode: str):
        self.mode = mode
        self.connections = 0
        self._held: list[socket.socket] = []
        self._listener = socket.socket()
        bind_ephemeral_loopback(self._listener)
        self._listener.listen(64)
        self.url = 'http://127.0.0.1:%d' % self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self.connections += 1
            if self.mode == 'blackhole':
                self._held.append(connection)
                continue
            with connection:
                connection.recv(65536)
                connection.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')

    def close(self):
        # A blocked old-path download sees EOF and ends, releasing its locks.
        for sock in (self._listener, *self._held):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class _Stall:
    def __init__(self):
        self.entered = threading.Event()
        self.released = threading.Event()


class _ColdTier2:
    """Process-cold Tier 2 with tiktoken's cache and egress under test control."""

    def __init__(self, monkeypatch, tmp_path):
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path
        self.constructions: list[str] = []
        self._stalls: dict[str, _Stall] = {}
        self._proxies: list[_FakeEgressProxy] = []
        real_init = tiktoken.core.Encoding.__init__

        def recording_init(encoding, name, **kwargs):
            self.constructions.append(name)
            stall = self._stalls.get(name)
            if stall is not None:
                stall.entered.set()
                stall.released.wait()
            real_init(encoding, name, **kwargs)

        monkeypatch.setattr(tiktoken.core.Encoding, '__init__', recording_init)

    def no_egress(self, mode: str, *, seed_tiktoken_cache=()) -> _FakeEgressProxy:
        """Route all egress to a refusing or black-holed proxy.

        tiktoken's cache starts empty, as after /tmp is emptied at boot. Seeding
        it lets the pre-fix download path load offline, so a spec can isolate a
        concurrency property from the network.
        """
        proxy = _FakeEgressProxy(mode)
        self._proxies.append(proxy)
        for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            self._monkeypatch.setenv(key, proxy.url)
        for key in ('all_proxy', 'ALL_PROXY', 'no_proxy', 'NO_PROXY', 'DATA_GYM_CACHE_DIR'):
            self._monkeypatch.delenv(key, raising=False)
        cache_dir = self._tmp_path / 'tiktoken-cache'
        _seed_tiktoken_cache(cache_dir, seed_tiktoken_cache)
        self._monkeypatch.setenv('TIKTOKEN_CACHE_DIR', str(cache_dir))
        return proxy

    def stall(self, name: str) -> _Stall:
        """Make every construction of ``name`` wait until released."""
        stall = self._stalls[name] = _Stall()
        return stall

    def close(self):
        for stall in self._stalls.values():
            stall.released.set()
        for proxy in self._proxies:
            proxy.close()


@pytest.fixture()
def cold_tier2(monkeypatch, tmp_path, reference):
    monkeypatch.setattr(native, 'scoped_native_encoder', lambda _name: None)
    monkeypatch.setattr(tc, '_encoders', {})
    monkeypatch.setattr(tc, '_available', None)
    monkeypatch.setattr(tc, '_failed_encodings', {}, raising=False)
    monkeypatch.setattr(token_api, 'MODE', 'auto')
    # tiktoken's process registry would serve a pre-fix load from another test.
    monkeypatch.setattr(tiktoken.registry, 'ENCODINGS', {})
    tc._reset_text_count_cache_for_tests()
    tier2 = _ColdTier2(monkeypatch, tmp_path)
    yield tier2
    tier2.close()
    tc._reset_text_count_cache_for_tests()


def _within_network_deadline(call):
    """Return ``call()``'s value, failing once the shared HTTP client's deadline
    (docs/NETWORK.md) passes without one."""
    outcome = {}

    def target():
        try:
            outcome['value'] = call()
        except BaseException as error:  # re-raised on the test thread
            outcome['error'] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(NETWORK_DEADLINE_S)
    assert not worker.is_alive(), (
        f'count still blocked after the network deadline ({NETWORK_DEADLINE_S:g} s)')
    if 'error' in outcome:
        raise outcome['error']
    return outcome['value']


def _exact(reference, name, text=TEXT):
    return len(reference[name].encode(text, disallowed_special=()))


@pytest.mark.parametrize('egress', ['refuse', 'blackhole'])
def test_a_cold_start_after_reboot_counts_exactly_from_the_shipped_vocabularies(
        cold_tier2, reference, egress):
    proxy = cold_tier2.no_egress(egress)

    counts = _within_network_deadline(lambda: (
        token_api.count_text(TEXT, model='gpt-5'),
        token_api.count_text(TEXT, model='gpt-4'),
        token_api.count_tokens(MESSAGES, model='gpt-4o')['method'],
        token_api.count_tokens(MESSAGES, model='kimi-k3')['method'],
    ))

    assert proxy.connections == 0
    assert counts == (_exact(reference, 'o200k_base'), _exact(reference, 'cl100k_base'),
                      'tiktoken', 'tiktoken')


def test_refused_egress_makes_at_most_one_attempt_per_process(cold_tier2):
    proxy = cold_tier2.no_egress('refuse')
    methods = set()
    for index in range(5):
        text = f'{TEXT} {index}'
        for model in ('gpt-4o', 'kimi-k3'):
            methods.add(token_api.count_tokens(
                [{'role': 'user', 'content': text}], model=model)['method'])
        for model in ('gpt-5', 'gpt-4'):
            token_api.count_text(text, model=model)

    assert proxy.connections <= 1
    assert methods == {'tiktoken'}


def test_forty_concurrent_cold_callers_share_one_load(cold_tier2):
    proxy = cold_tier2.no_egress('refuse', seed_tiktoken_cache=ENCODINGS)
    barrier = threading.Barrier(40)
    results = []

    def caller():
        barrier.wait()
        results.append(token_api.count_tokens(MESSAGES, model='gpt-4o'))

    callers = [threading.Thread(target=caller, daemon=True) for _ in range(40)]
    for thread in callers:
        thread.start()
    deadline = time.monotonic() + NETWORK_DEADLINE_S
    for thread in callers:
        thread.join(max(0.0, deadline - time.monotonic()))

    assert not any(thread.is_alive() for thread in callers)
    assert cold_tier2.constructions == ['o200k_base']
    warm = token_api.count_tokens(MESSAGES, model='gpt-4o')
    assert len(results) == 40
    assert {(result['method'], result['tokens']) for result in results} == {
        ('tiktoken', warm['tokens'])}
    assert proxy.connections == 0


def test_supports_loads_only_the_models_own_encoding(cold_tier2):
    cold_tier2.no_egress('refuse', seed_tiktoken_cache=ENCODINGS)
    counter = tc.TiktokenCounter()

    assert counter.supports('gpt-4o') is True
    assert cold_tier2.constructions == ['o200k_base']
    assert counter.supports('kimi-k3') is True
    assert cold_tier2.constructions == ['o200k_base', 'cl100k_base']


@pytest.mark.parametrize('cl100k_state', ['cached', 'cold'])
def test_a_black_holed_load_of_one_encoding_never_blocks_the_other(
        cold_tier2, reference, cl100k_state):
    """The o200k load never returns, standing in for a black-holed download
    or a stalled filesystem read. cl100k counts proceed whether cl100k is
    already cached or must load now."""
    cold_tier2.no_egress('blackhole', seed_tiktoken_cache=ENCODINGS)
    if cl100k_state == 'cached':
        assert token_api.count_text(TEXT, model='gpt-4') == _exact(reference, 'cl100k_base')
    stall = cold_tier2.stall('o200k_base')
    stalled = {}
    stalled_thread = threading.Thread(
        target=lambda: stalled.setdefault('count', token_api.count_text(TEXT, model='gpt-4o')),
        daemon=True)
    stalled_thread.start()
    assert stall.entered.wait(NETWORK_DEADLINE_S)

    other_text = TEXT + ' (other encoding)'
    assert _within_network_deadline(
        lambda: token_api.count_text(other_text, model='gpt-4')) == _exact(
            reference, 'cl100k_base', other_text)
    assert _within_network_deadline(
        lambda: token_api.count_tokens(MESSAGES, model='kimi-k3'))['method'] == 'tiktoken'

    stall.released.set()
    stalled_thread.join(NETWORK_DEADLINE_S)
    assert stalled == {'count': _exact(reference, 'o200k_base')}
