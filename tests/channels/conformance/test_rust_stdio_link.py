"""The Rust core-side stdio link behind Python's AdapterHost (packages/tofu-channel-stdio).

``RustStdioLink`` (lib/channels/protocol/rust_stdio.py) must keep every
promise of ``StdioLink``: the stdio contract tests of
test_protocol_contract.py, and every conformance case of the ``reference-stdio``
driver with exactly the statuses ``baseline.json`` records for it. The link
binary comes from ``TOFU_CHANNEL_STDIO_LINK_BIN``, else Cargo builds it into a
target directory under pytest's temporary root.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lib.channels.protocol.contract import ERRORS, ProtocolError
from tests.channels.conftest import OWNER
from tests.channels.conformance.cases import CASES
from tests.channels.conformance.drivers import ReferenceStdioDriver
from tests.channels.conformance.harness import Env, NotApplicable
from tests.channels.conformance.reference import CHANNEL as REF

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_CRATE = _ROOT / 'packages' / 'tofu-channel-stdio'
_LINK_BIN_ENV = 'TOFU_CHANNEL_STDIO_LINK_BIN'
BASELINE = json.loads(Path(__file__).with_name('baseline.json').read_text(encoding='utf-8'))


def _cargo() -> str | None:
    found = shutil.which('cargo')
    if found:
        return found
    for home in (os.environ.get('CARGO_HOME'), str(Path.home() / '.cargo')):
        if home and (Path(home) / 'bin' / 'cargo').is_file():
            return str(Path(home) / 'bin' / 'cargo')
    return None


@pytest.fixture(scope='session')
def link_binary(tmp_path_factory) -> str:
    given = os.environ.get(_LINK_BIN_ENV)
    if given:
        return given
    cargo = _cargo()
    if cargo is None:
        pytest.skip(f'{_LINK_BIN_ENV} is unset and cargo is unavailable')
    # Shared by xdist workers of one session; Cargo locks the directory itself.
    target = tmp_path_factory.getbasetemp().parent / 'tofu-channel-stdio-target'
    subprocess.run([cargo, 'build', '--bin', 'tofu-channel-stdio-link'], cwd=_CRATE, check=True,
                   env={**os.environ, 'CARGO_TARGET_DIR': str(target)})
    return str(target / 'debug' / 'tofu-channel-stdio-link')


@pytest.fixture(autouse=True)
def rust_link(link_binary, monkeypatch) -> str:
    monkeypatch.setenv(_LINK_BIN_ENV, link_binary)
    return link_binary


def _eventually(predicate, *, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f'timed out waiting for {what}')
        time.sleep(0.01)


# ── the stdio contract tests (test_protocol_contract.py) ─────────────────────

def test_a_rust_link_request_fails_when_the_adapter_process_ends():
    from lib.channels.protocol.rust_stdio import RustStdioLink
    link = RustStdioLink([sys.executable, '-c', 'import sys; sys.stdin.readline()'])
    try:
        with pytest.raises(ProtocolError) as ended:
            link.request('send', {})
        assert ended.value.code == ERRORS['internalError']
        assert str(ended.value) == 'The channel adapter binding closed'
    finally:
        assert link.close(5.0)
    assert link.returncode == 0


_MALFORMED_ADAPTER = r'''
import json, sys
sys.stdout.write('not json\n')
sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'nope', 'params': {}}) + '\n')
sys.stdout.flush()
answers = [json.loads(sys.stdin.readline()) for _ in range(2)]
print(json.dumps(answers), file=sys.stderr)
sys.exit(3)
'''


def test_a_rust_link_answers_malformed_lines_and_unknown_methods():
    from lib.channels.protocol.rust_stdio import RustStdioLink
    link = RustStdioLink([sys.executable, '-c', _MALFORMED_ADAPTER])
    try:
        _eventually(lambda: link.returncode is not None, what='the adapter exiting')
        answers = json.loads(link.stderr_tail)
    finally:
        assert link.close(5.0)
    assert link.returncode == 3
    assert [answer['error']['code'] for answer in answers] == [ERRORS['parseError'], ERRORS['methodNotFound']]
    assert answers[0]['id'] is None
    assert answers[1]['error']['message'] == "The core has no method 'nope'"


@pytest.mark.parametrize('malformed', [
    {'code': 'not-a-number', 'message': 'x'},
    {'code': [1], 'message': 'x'},
    {'code': -32000, 'message': 'x', 'data': 'not-an-object'},
    {'code': -32000, 'message': 'x', 'data': [1, 2]},
])
def test_a_malformed_response_fails_its_rust_link_request_and_the_link_keeps_answering(malformed):
    import threading
    from lib.channels.protocol.rust_stdio import RustStdioLink
    from tests.channels.conformance.test_protocol_contract import _MALFORMED_ADAPTER as ADAPTER
    link = RustStdioLink([sys.executable, '-c', ADAPTER, json.dumps(malformed)])
    outcome: dict = {}

    def call(name):
        try:
            outcome[name] = link.request(name, {})
        except ProtocolError as error:
            outcome[name] = error

    try:
        for name in ('malformed', 'well_formed'):
            caller = threading.Thread(target=call, args=(name,), daemon=True)
            caller.start()
            caller.join(10)
            assert not caller.is_alive(), f'the {name} request never settled'
        assert isinstance(outcome['malformed'], ProtocolError)
        assert outcome['malformed'].code == ERRORS['internalError']
        assert outcome['well_formed'] == {'ok': True}
    finally:
        assert link.close(5.0)


def test_close_reaps_an_adapter_that_ignores_stdin_and_sigterm():
    from lib.channels.protocol.rust_stdio import RustStdioLink
    link = RustStdioLink(['/bin/sh', '-c', 'trap "" TERM; while :; do sleep 1; done'])
    started = time.monotonic()
    assert link.close(0.3)
    assert time.monotonic() - started >= 0.6
    assert link.returncode == -9


# ── every conformance case of the reference-stdio driver ─────────────────────

class RustReferenceStdioDriver(ReferenceStdioDriver):
    """The reference adapter in a child process behind the Rust core-side link."""

    name = 'reference-stdio-rust'

    def _open(self) -> None:
        from lib.channels.protocol.host import AdapterHost
        from lib.channels.protocol.rust_stdio import RustStdioLink
        env = {**os.environ, 'PYTHONPATH': str(_ROOT)}
        self.link = RustStdioLink([sys.executable, '-m', 'tests.channels.conformance.reference'], env=env,
                                  cwd=str(_ROOT))
        self.host = AdapterHost(OWNER, REF, self.link, store=self.store)
        self.host.open()


def _params():
    # The same statuses the Python link records (baseline.json, reference-stdio).
    for case in CASES:
        expected = BASELINE['results']['reference-stdio'][case]
        marks = []
        if expected['status'] == 'xfail':
            marks.append(pytest.mark.xfail(strict=True, reason=expected['cause']))
        yield pytest.param(case, expected, id=f'reference-stdio-rust-{case}', marks=marks)


@pytest.fixture
def env(records, store, router, dispatch, mailbox, monkeypatch, tmp_path, vault):
    return Env(store=store, records=records, dispatch=dispatch, monkeypatch=monkeypatch, tmp_path=tmp_path,
               vault=vault)


@pytest.mark.parametrize('case, expected', list(_params()))
def test_conformance_through_the_rust_link(env, case, expected):
    driver = RustReferenceStdioDriver(env)
    driver.setup()
    try:
        if expected['status'] == 'n/a':
            try:
                CASES[case](driver)
            except NotApplicable:
                pytest.skip(f'n/a: {expected["reason"]}')
            pytest.fail('baseline.json says n/a for reference-stdio, but the case ran through the Rust link')
        try:
            CASES[case](driver)
        except NotApplicable as error:
            pytest.fail(f'the case is not applicable ({error}), but reference-stdio expects {expected["status"]}')
    finally:
        driver.teardown()
