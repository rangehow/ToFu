"""Login MIS → launcher SSO session (docs/CHANNELS.md C6): reuse first, one approval otherwise."""
import json
import os
import stat
import subprocess

import pytest

from lib.channels.dx import login
from lib.channels.model import ChannelError

pytestmark = pytest.mark.unit

OWNER = 7


@pytest.fixture
def data(monkeypatch, tmp_path):
    monkeypatch.setattr('lib.runtime.runtime_paths.data_root', lambda: str(tmp_path))
    return tmp_path


def shared(monkeypatch, mis):
    seen = []
    monkeypatch.setattr(login, 'shared_session_mis', lambda environment: seen.append(dict(environment)) or mis)
    return seen


def test_normalize_mis_accepts_only_one_lowercase_account():
    assert login.normalize_mis('  ZhangSan03 ') == 'zhangsan03'
    assert login.normalize_mis('a.b_c-1') == 'a.b_c-1'
    for bad in ('', ' ', 'two words', '_lead', 'x' * 65, None, 42, 'zhang/san', '张三'):
        with pytest.raises(ChannelError) as raised:
            login.normalize_mis(bad)
        assert raised.value.code == 'invalid_update'


def test_login_mis_and_binding_follow_the_chosen_account():
    identity = {'uid': '1', 'mis': 'owner', 'name': 'Owner'}
    assert login.account_login_mis(None) is None
    assert login.account_login_mis({'identity': identity}) == 'owner'
    assert login.account_login_mis({'mis': 'alice', 'identity': identity}) == 'alice'
    assert login.identity_bound({'identity': identity})
    assert login.identity_bound({'mis': 'owner', 'identity': identity})
    assert not login.identity_bound({'mis': 'alice', 'identity': identity})
    assert not login.identity_bound({'mis': 'alice', 'identity': None})


def test_the_shared_session_is_reused_for_its_own_account(monkeypatch, data):
    seen = shared(monkeypatch, 'owner')
    environment = {'PATH': '/bin', 'LLM_MIS': 'someone', 'XUECHENG_MIS': 'else'}
    for chosen in (None, 'owner'):
        session = login.login_session(OWNER, chosen, environment)
        assert session.mis == 'owner'
        # Exactly one MIS target reaches the launcher; the shared session file stays the SDK default.
        assert session.environment == {'PATH': '/bin', 'MTSSO_MIS': 'owner'}
    assert len(seen) == 2
    assert not (data / 'channels').exists()


def test_another_account_logs_in_with_its_own_private_session_file(monkeypatch, data):
    shared(monkeypatch, 'owner')
    session = login.login_session(OWNER, 'alice', {'PATH': '/bin', 'MTSSO_MIS': 'owner'})
    path = data / 'channels' / 'dx' / 'sso' / f'u{OWNER}' / 'alice.json'
    assert session.mis == 'alice'
    assert session.environment == {'PATH': '/bin', 'MTSSO_MIS': 'alice', 'MTSSO_SESSION_TOKEN_FILE': str(path)}
    for directory in (path.parent, path.parent.parent):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    # Only the SDK writes the session file; Tofu creates no file of its own.
    assert not path.exists()


def test_without_a_chosen_mis_or_shared_session_there_is_nothing_to_log_in_with(monkeypatch, data):
    shared(monkeypatch, '')
    session = login.login_session(OWNER, None, {'PATH': '/bin'})
    assert session.mis is None and session.environment == {'PATH': '/bin'}


def test_a_launch_carries_only_the_owners_own_login_request(monkeypatch, data):
    """C6: the SSO adapter's launch asks for a new 大象 approval after an unapproved one only for an
    owner request newer than it (Settings 重新连接); no ambient value ever reaches a launch."""
    shared(monkeypatch, 'owner')
    ambient = {'PATH': '/bin', 'MTSSO_LOGIN_REQUESTED_AT_MS': '99'}
    session = login.login_session(OWNER, 'owner', ambient)
    assert session.environment == {'PATH': '/bin', 'MTSSO_MIS': 'owner'}
    assert login.login_session(OWNER, 'alice', ambient).environment.get('MTSSO_LOGIN_REQUESTED_AT_MS') is None
    shared(monkeypatch, '')
    assert login.login_session(OWNER, None, ambient).environment == {'PATH': '/bin'}
    asked = login.with_login_request(session, {'loginRequestedAtMs': 1_794_500_000_000})
    assert asked.mis == 'owner'
    assert asked.environment == {'PATH': '/bin', 'MTSSO_MIS': 'owner',
                                 'MTSSO_LOGIN_REQUESTED_AT_MS': '1794500000000'}
    for account in (None, {}, {'loginRequestedAtMs': None}, {'loginRequestedAtMs': 'soon'},
                    {'loginRequestedAtMs': True}, {'loginRequestedAtMs': 0}):
        assert login.with_login_request(asked, account).environment == session.environment


def test_the_bridge_and_its_sso_step_run_the_one_verified_node(monkeypatch, data, verified_launcher_node):
    # lib.channels.dx.runtime verifies TOFU_NODE once; an ambient MTSSO_NODE_EXECUTABLE never
    # splits the bridge (DX_NODE) and its SSO step across two binaries.
    from lib.channels.dx.adapter import launcher_session
    seen = shared(monkeypatch, 'owner')
    monkeypatch.setenv('MTSSO_NODE_EXECUTABLE', '/srv/other-node/bin/node')
    session = launcher_session(OWNER, 'owner')
    handed = verified_launcher_node.launcher_environment()
    assert set(handed) == {'DX_NODE', 'MTSSO_NODE_EXECUTABLE', 'MTSSO_NODE_VERIFIED'}
    # The adapter's own question (which MIS the shared session holds) runs on the same Node.
    for environment in (session.environment, seen[-1]):
        assert {key: environment.get(key) for key in handed} == handed


class Adapter:
    """The SSO adapter child process: records each run and answers with a scripted stdout/exit."""

    def __init__(self, monkeypatch, stdout=b'', returncode=0):
        self.runs = []
        self.stdout, self.returncode = stdout, returncode
        monkeypatch.setattr(login.subprocess, 'run', self.run)

    def run(self, argv, *, env, **kwargs):
        self.runs.append((argv, env))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b'OfficialAuthError: redacted')


@pytest.mark.parametrize('state, waits', [('login_required', True), ('pending', True), ('ready', False),
                                          ('blocked', False), ('unknown', False)])
def test_approval_is_required_only_when_the_adapter_says_a_login_waits_for_one(monkeypatch, state, waits):
    adapter = Adapter(monkeypatch, json.dumps({'auth_state': state}).encode())
    session = login.LoginSession('alice', {'MTSSO_MIS': 'alice', 'MTSSO_SESSION_TOKEN_FILE': '/s/alice.json'})
    assert login.approval_required(OWNER, session) is waits
    [(argv, env)] = adapter.runs
    # The adapter is asked for the 大象 audience as this owner and MIS, in the launcher's own SSO environment.
    assert argv[-3:] == [login.sso_owner(OWNER), 'xm-xai', 'alice']
    assert env['MTSSO_SESSION_TOKEN_FILE'] == '/s/alice.json' and env['MTSSO_MIS'] == 'alice'


def test_an_unanswered_approval_question_is_left_to_the_launcher(monkeypatch):
    adapter = Adapter(monkeypatch, returncode=1)
    assert not login.approval_required(OWNER, login.LoginSession('alice', {}))
    adapter.stdout, adapter.returncode = b'not json', 0
    assert not login.approval_required(OWNER, login.LoginSession('alice', {}))
    assert not login.approval_required(OWNER, login.LoginSession(None, {}))
    assert len(adapter.runs) == 2


@pytest.mark.parametrize('state, ready', [('ready', True), ('login_required', False), ('pending', False),
                                          ('blocked', False), ('unknown', False)])
def test_a_login_is_ready_only_when_the_adapter_says_so(monkeypatch, state, ready):
    adapter = Adapter(monkeypatch, json.dumps({'auth_state': state}).encode())
    session = login.LoginSession('alice', {'MTSSO_MIS': 'alice'})
    assert login.login_ready(OWNER, session) is ready
    [(argv, env)] = adapter.runs
    assert argv[-3:] == [login.sso_owner(OWNER), 'xm-xai', 'alice'] and env['MTSSO_MIS'] == 'alice'


def test_an_unanswered_readiness_question_never_counts_as_ready(monkeypatch):
    """A needs_login recheck relaunches only on ``ready``: no answer must not cost a login request."""
    adapter = Adapter(monkeypatch, returncode=1)
    assert not login.login_ready(OWNER, login.LoginSession('alice', {}))
    adapter.stdout, adapter.returncode = b'not json', 0
    assert not login.login_ready(OWNER, login.LoginSession('alice', {}))
    assert not login.login_ready(OWNER, login.LoginSession(None, {}))
    assert len(adapter.runs) == 2
