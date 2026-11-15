"""A tool result declares that the user must act only through its typed error.

``lib.tools.user_action`` is the task engine's one reader of that declaration
(docs/contracts/TOOL_EXECUTION_POLICY.md, "Errors that need the user"). These
specs pin what counts: a failed MCP result whose structured content says
``user_action_required: true``, or names a model action that asks the user.
Prose, error codes and successful results never count.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from lib.mcp.result_content import MCPToolResult
from lib.tools.result_envelope import tool_result_error
from lib.tools.user_action import (
    AWAITING_USER_ACTION_CODE,
    MODEL_ACTIONS_THAT_ASK_THE_USER,
    awaiting_user_action_result,
    requirement_scope,
    user_action_requirement,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# The shared SSO adapter's envelope for an unapproved 大象 login
# (packages/tofu-mtsso-auth, SSO-SESSION-REUSE-1).
NOT_APPROVED = {
    'ok': False, 'error_code': 'SSO_LOGIN_NOT_APPROVED',
    'error': '大象登录授权未在有效期内获批。', 'auth_source': 'mtsso_shared_session',
    'auth_state': 'login_required', 'login_attempted': False, 'retryable': False,
    'user_action_required': True, 'model_action': 'ask_user_then_request_login',
    'recovery_action': 'request_new_daxiang_login',
}


def mcp_result(payload, *, is_error=None, structured=True):
    text = json.dumps(payload, ensure_ascii=False)
    failed = payload.get('ok') is False if is_error is None else is_error
    return MCPToolResult(('MCP Error: ' if failed else '') + text,
                         structured_json=text if structured else None, is_error=failed)


def test_the_adapter_envelope_declares_a_requirement_for_the_whole_server():
    requirement = user_action_requirement(
        'mcp__xuecheng__create_doc', {'title': 'placeholder'}, mcp_result(NOT_APPROVED))
    assert requirement is not None
    assert requirement.to_dict() == {
        'scope': 'mcp:xuecheng', 'tool': 'mcp__xuecheng__create_doc',
        'errorCode': 'SSO_LOGIN_NOT_APPROVED', 'requiredAction': 'request_new_daxiang_login',
        'modelAction': 'ask_user_then_request_login', 'message': '大象登录授权未在有效期内获批。'}


def test_a_model_action_that_asks_the_user_declares_it_without_the_boolean():
    """学城's own NOT_LOGGED_IN envelope names only its model action."""
    payload = {'ok': False, 'error_code': 'NOT_LOGGED_IN', 'error': '未登录',
               'auth_state': 'login_required', 'model_action': 'ask_user_to_approve_then_retry'}
    requirement = user_action_requirement('mcp__xuecheng__read_doc', {}, mcp_result(payload))
    assert requirement is not None
    assert (requirement.error_code, requirement.required_action) == (
        'NOT_LOGGED_IN', 'ask_user_to_approve_then_retry')


@pytest.mark.parametrize('payload', [
    # A document the account may not read: 学城 answers per document, with no declaration.
    {'ok': False, 'error_code': 619, 'error': '对不起，您没有查看文档的权限'},
    # A configuration fault the user cannot fix by approving anything.
    {'ok': False, 'error_code': 'SSO_NODE_NOT_FOUND', 'auth_state': 'blocked',
     'retryable': False, 'user_action_required': False, 'model_action': 'stop_and_report',
     'recovery_action': 'repair_agent_configuration'},
    {'ok': False, 'error_code': 'INTERNAL_ERROR', 'error': 'internal tool failure'},
], ids=['per-document', 'configuration', 'internal'])
def test_an_error_that_does_not_declare_the_user_is_not_a_requirement(payload):
    assert user_action_requirement('mcp__xuecheng__read_doc', {}, mcp_result(payload)) is None


def test_prose_and_successful_results_never_declare_a_requirement():
    # A status probe answers successfully while the login is still required.
    probe = {'ok': True, 'logged_in': False, 'auth_state': 'login_required',
             'login_request': 'explicit', 'user_action_required': True}
    assert user_action_requirement('mcp__xuecheng__get_login_status', {}, mcp_result(probe)) is None
    # The same envelope without structured content is only display text.
    assert user_action_requirement(
        'mcp__xuecheng__create_doc', {}, mcp_result(NOT_APPROVED, structured=False)) is None
    assert user_action_requirement(
        'mcp__xuecheng__create_doc', {}, 'MCP Error: ' + json.dumps(NOT_APPROVED)) is None
    assert user_action_requirement('run_command', {'command': 'x'}, json.dumps(NOT_APPROVED)) is None


@pytest.mark.parametrize('name, arguments, scope', [
    ('mcp__xuecheng__create_doc', {}, 'mcp:xuecheng'),
    ('call_mcp_write_tool', {'name': 'mcp__xuecheng__create_doc', 'arguments': {}}, 'mcp:xuecheng'),
    ('call_mcp_read_tool', {'name': 'mcp__hope__get_job_logs', 'arguments': {}}, 'mcp:hope'),
    ('call_mcp_read_tool', {'name': 'not-namespaced'}, 'tool:call_mcp_read_tool'),
    ('web_search', {'query': 'x'}, 'tool:web_search'),
])
def test_one_server_shares_one_requirement_scope(name, arguments, scope):
    assert requirement_scope(name, arguments) == scope


def test_a_progressive_wrapper_reports_the_tool_it_called():
    requirement = user_action_requirement(
        'call_mcp_write_tool', {'name': 'mcp__xuecheng__create_doc', 'arguments': {}},
        mcp_result(NOT_APPROVED))
    assert (requirement.scope, requirement.tool) == ('mcp:xuecheng', 'mcp__xuecheng__create_doc')


def test_a_call_not_sent_gets_a_typed_non_retryable_result_naming_the_requirement():
    requirement = user_action_requirement('mcp__xuecheng__create_doc', {}, mcp_result(NOT_APPROVED))
    envelope = awaiting_user_action_result(requirement)
    error = tool_result_error(envelope)
    assert (error.code, error.retryable) == (AWAITING_USER_ACTION_CODE, False)
    assert 'SSO_LOGIN_NOT_APPROVED' in error.message and 'request_new_daxiang_login' in error.message
    assert envelope.items == ({'userAction': requirement.to_dict()},)
    assert tool_result_error(envelope.to_model_text()).code == AWAITING_USER_ACTION_CODE


def test_the_dispatch_pipeline_does_not_load_the_mcp_client_to_read_the_contract():
    """The hot dispatch modules import this reader; the MCP client package loads where it is used."""
    code = ('import sys, lib.tasks_pkg.tool_dispatch._pipeline, lib.tools.user_action; '
            'print("lib.mcp" in sys.modules)')
    loaded = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=300, check=True)
    assert loaded.stdout.strip().splitlines()[-1] == 'False', loaded.stderr[-2000:]


def _adapter(monkeypatch):
    source = ROOT / 'packages' / 'tofu-mtsso-auth' / 'src'
    if not source.is_dir():
        pytest.skip('the company SSO adapter is not shipped in the open-source tree')
    monkeypatch.syspath_prepend(str(source))
    import tofu_mtsso_auth
    return tofu_mtsso_auth


def test_every_adapter_envelope_is_read_as_its_own_user_action_flag(monkeypatch):
    """The adapter is the source of the vocabulary; a new model action that asks the user must join it."""
    adapter = _adapter(monkeypatch)
    codes = sorted(adapter._HUMAN_CODES | adapter._APPROVAL_CODES | adapter._PENDING_CODES
                   | adapter._CONFIGURATION_CODES | {'SSO_EXCHANGE_FAILED'})
    assert {'SSO_LOGIN_NOT_APPROVED', 'SSO_SESSION_REQUIRED', 'SSO_AUTH_REQUEST_PENDING'} <= set(codes)
    for code in codes:
        for user_action in (False, True):
            envelope = adapter.OfficialAuthError(code, 'message', user_action=user_action).envelope()
            if envelope['auth_state'] != 'blocked':
                assert envelope['model_action'] in MODEL_ACTIONS_THAT_ASK_THE_USER, envelope
            requirement = user_action_requirement('mcp__hope__get_job_logs', {}, mcp_result(envelope))
            assert (requirement is not None) is envelope['user_action_required'], envelope
