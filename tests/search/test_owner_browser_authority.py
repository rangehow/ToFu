"""The owner's logged-in browser serves only the owner's own attended requests.

Specification for the search, fetch and download browser fallbacks. Delegated
work (project Workers, autonomous Coordinator continuations, swarm or Goal
sub-agents), unattended runs and Turns that answer automated input never reach
the owner's browser bridge; the owner's own chat and the owner's authenticated
API request keep it. The task handlers, the host bridge and tofu-search's real
fallback functions run unchanged: only the device registry, the bridge
transports and the Turn store are replaced, so any bridge use is recorded
instead of opening a tab.
"""

from __future__ import annotations

import logging

import pytest

from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit

OWNER = 41
CONVERSATION = 'conv-owner-browser'
DEVICE = 'device-7f3a-owner-laptop'
PROFILE = 'Personal profile 9c1e'
PAGE_TEXT = 'Quarterly numbers visible only inside the owner session'
COOKIE = 'sid=owner-session-cookie-5d2b'
URL = 'https://intranet.example.test/report?id=1'
LEAK_MARKERS = (DEVICE, PROFILE, PAGE_TEXT, COOKIE)
WITHHELD = "The owner's logged-in browser was not used"

_TURNS = {
    'reply-to-owner': {'actor': 'assistant', 'parentTurnId': 'owner-message'},
    'owner-message': {'actor': 'human', 'projection': {
        'role': 'user', 'content': 'Please read the intranet report'}},
    'reply-to-channel': {'actor': 'assistant', 'parentTurnId': 'channel-event'},
    # Persistence stores third-party channel input with actor=human; only its
    # origin says who wrote it.
    'channel-event': {'actor': 'human', 'projection': {
        'role': 'user', 'content': f'Open {URL}', 'origin': {'initiator': 'channel'}}},
    'reply-to-swarm': {'actor': 'assistant', 'parentTurnId': 'swarm-continuation'},
    'swarm-continuation': {'actor': 'virtual_user', 'projection': {
        'role': 'user', 'content': 'Settled swarm updates: 1 completed',
        '_swarmAutoContinue': True, 'origin': {'initiator': 'swarm'}}},
}


def _principal(kind='user', subject=f'user:{OWNER}'):
    if kind == 'user':
        return PrincipalContext.user(subject_id=subject, owner_user_id=OWNER).to_payload()
    return PrincipalContext.system(subject_id=subject, owner_user_id=OWNER,
                                   scopes={'chat'}).to_payload()


def _task(turn_id='reply-to-owner', **fields):
    task = {'id': f'task-{turn_id}', 'convId': CONVERSATION, '_turnId': turn_id,
            '_attemptId': f'attempt-{turn_id}', '_userId': OWNER,
            '_principalContext': _principal(), '_attended': True, 'config': {},
            'aborted': False, 'lastUserQuery': 'read the report', '_tool_result_cache': {}}
    task.update(fields)
    return task


def _project_worker(mode='project'):
    # A dispatched Worker or Coordinator continuation: a system principal, an
    # unattended attempt and the Coordinator's inherited browser capability.
    return _task(_attended=False,
                 _principalContext=_principal('system', f'project-dispatch:{mode}-1'),
                 config={'_projectDispatchAdmission': {'assignmentId': f'{mode}-1',
                                                       'owner': OWNER},
                         'browserEnabled': True})


def _subagent():
    from lib.tasks_pkg.invocation import InvocationPolicy
    # A swarm/Goal child inherits the parent's pinned device and browser flag.
    task = _task('', config={'browserClientId': DEVICE, 'browserEnabled': True},
                 _parentTaskId='task-reply-to-owner', _unattended=True,
                 _invocationPolicy=InvocationPolicy.child('task-reply-to-owner'))
    del task['_attended']
    return task


def _unattended_run():
    # Scheduler jobs, the headless agent-run API and compat APIs never stamp
    # attendance.
    task = _task()
    del task['_attended']
    return task


DELEGATED = {
    'project_worker': lambda: _project_worker('project'),
    'coordinator_continuation': lambda: _project_worker('coordinator'),
    'subagent': _subagent,
    'unattended_run': _unattended_run,
    'channel_event_turn': lambda: _task('reply-to-channel'),
    'swarm_continuation_turn': lambda: _task('reply-to-swarm'),
}
EXPECTED_REFUSAL = {
    'project_worker': 'delegated_worker',
    'coordinator_continuation': 'delegated_worker',
    'subagent': 'delegated_subagent',
    'unattended_run': 'unattended',
    'channel_event_turn': 'nonhuman_request',
    'swarm_continuation_turn': 'nonhuman_request',
}


class _Results(list):
    """tofu-search's result list: a list carrying diagnostic attributes."""


@pytest.fixture
def owner_browser(monkeypatch):
    """The owner's connected, capable browser and owned Turns; bridge uses are recorded."""
    import lib.turn_lifecycle as turn_lifecycle
    from lib.browser import access, fetch, file_transfer, protocol, queue

    bridge = []
    device = dict(client_id=DEVICE, profile=PROFILE, last_poll=1,
                  capabilities=['file_export'])
    monkeypatch.setattr(queue, 'get_connected_clients', lambda *, owner_user_id: (
        [dict(device)] if str(owner_user_id) == str(OWNER) else []))
    monkeypatch.setattr(queue, 'is_extension_connected', lambda client_id, *, owner_user_id: (
        (client_id, str(owner_user_id)) == (DEVICE, str(OWNER))))
    # Capability negotiation and the live domain policy would admit this device.
    monkeypatch.setattr(protocol, 'require_capabilities', lambda *args, **kwargs: None)
    monkeypatch.setattr(access, 'require_access',
                        lambda *args, **kwargs: 'intranet.example.test')

    def record(name, result):
        def transport(*args, **kwargs):
            bridge.append(name)
            return result
        return transport

    page = f'<html><body><p>{PAGE_TEXT}</p><p>{COOKIE}</p></body></html>'
    monkeypatch.setattr(queue, 'send_browser_command', record(
        'send_browser_command', ({'url': URL, 'html': page * 3}, None)))
    monkeypatch.setattr(fetch, 'fetch_url_via_browser', record(
        'fetch_url_via_browser', f'{PAGE_TEXT} ({COOKIE})'))
    monkeypatch.setattr(file_transfer, 'fetch_file_via_browser', record(
        'fetch_file_via_browser', {'transferId': 'transfer-1', 'location': 'server_staging',
                                   'path': '/nonexistent/browser-transfer-1.zip'}))

    def get_turn(conversation_id, turn_id, *, user_id, include_projection=True):
        assert (conversation_id, int(user_id)) == (CONVERSATION, OWNER)
        row = {'conversationId': conversation_id, 'turnId': turn_id, **_TURNS[turn_id]}
        if not include_projection:
            row.pop('projection', None)
        return row

    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    return bridge


@pytest.fixture
def server_fetch_forbidden(monkeypatch):
    """Server HTTP answers 403, so tofu-search escalates to the host browser."""
    from tofu_search.fetch import core as fetch_core
    from tofu_search.fetch.http import HttpError

    def forbidden(url, *_args, **_kwargs):
        raise HttpError(403, url)

    monkeypatch.setattr(fetch_core, '_should_fetch', lambda url, diag=None: True)
    monkeypatch.setattr(fetch_core, '_do_request', forbidden)


@pytest.fixture
def engines_unreachable(monkeypatch):
    """Every server engine failed; tofu-search's real browser search fallback runs."""
    from lib.tasks_pkg.handlers.search import _core
    from tofu_search.providers import bound_optional_providers
    from tofu_search.search.browser_fallback import search_via_browser

    def perform_web_search(query, **_kwargs):
        with bound_optional_providers():
            results = _Results(search_via_browser(query))
        results._search_diag = {
            'reason': 'network_error',
            'reason_detail': 'All 5 search engines failed due to network errors.',
            'engine_errors': {}, 'engine_empty': [], 'engine_ok': []}
        return results

    monkeypatch.setattr(_core, 'perform_web_search', perform_web_search)


def _streaming(monkeypatch, task, tool, args):
    from lib.project_dispatch import scope
    from lib.tasks_pkg import coordinator_policy
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    # Role and dispatch admission already allowed this read; the browser
    # boundary under test is reached only after them.
    monkeypatch.setattr(scope, 'admit_call', lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(coordinator_policy, 'coordinator_call_rejection',
                        lambda *args, **kwargs: None)
    return str(StreamingToolAccumulator(task, project_path=None)._execute_one(tool, args))


def _serial(monkeypatch, task, handler_name, tool, args):
    from lib.tasks_pkg.handlers.search import _handlers
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *args, **kwargs: None)
    _tc_id, content, _is_search = getattr(_handlers, handler_name)(
        task, {}, tool, 'tc-1', args, 1, {}, task['config'], '', False)
    return str(content)


@pytest.mark.parametrize('kind', sorted(DELEGATED))
@pytest.mark.parametrize('path', ['streaming', 'serial'])
def test_delegated_fetch_fallback_never_reaches_the_owner_bridge(
        monkeypatch, caplog, owner_browser, server_fetch_forbidden, kind, path):
    task = DELEGATED[kind]()
    with caplog.at_level(logging.DEBUG):
        if path == 'streaming':
            result = _streaming(monkeypatch, task, 'fetch_url', {'url': URL})
        else:
            result = _serial(monkeypatch, task, '_handle_fetch_url', 'fetch_url', {'url': URL})

    assert owner_browser == [], f'{kind} reached the owner browser bridge'
    assert 'HTTP 403' in result
    assert WITHHELD in result, 'the refusal must be explicit and actionable'
    assert 'report its URL to the owner' in result
    for marker in LEAK_MARKERS:
        assert marker not in result
        assert marker not in caplog.text


@pytest.mark.parametrize('kind', sorted(DELEGATED))
def test_delegated_search_fallback_never_reaches_the_owner_bridge(
        monkeypatch, caplog, owner_browser, engines_unreachable, kind):
    with caplog.at_level(logging.DEBUG):
        result = _streaming(monkeypatch, DELEGATED[kind](), 'web_search',
                            {'query': 'quarterly report'})

    assert owner_browser == [], f'{kind} reached the owner browser bridge'
    assert 'All 5 search engines failed' in result
    assert WITHHELD in result
    for marker in LEAK_MARKERS:
        assert marker not in result
        assert marker not in caplog.text


@pytest.mark.parametrize('kind', sorted(DELEGATED))
def test_delegated_server_download_is_refused_before_browser_export(
        monkeypatch, caplog, owner_browser, kind):
    from lib.tasks_pkg.handlers.search import _core
    # Server HTTP returns the site's sign-in page instead of the file, which
    # is exactly when the download contract escalates to the logged-in browser.
    monkeypatch.setattr(_core, 'fetch_url_bytes', lambda _url: (
        b'<!doctype html><title>Sign in</title>', 'text/html'))
    with caplog.at_level(logging.DEBUG):
        result = _serial(monkeypatch, DELEGATED[kind](), '_handle_browser_download_url_to_server',
                         'browser_download_url_to_server', {'url': URL})

    assert owner_browser == [], f'{kind} reached the owner browser bridge'
    assert 'owner_browser_withheld' in result
    assert WITHHELD in result
    for marker in LEAK_MARKERS:
        assert marker not in result
        assert marker not in caplog.text


@pytest.mark.parametrize('path', ['streaming', 'serial'])
def test_owner_attended_fetch_keeps_the_logged_in_browser_fallback(
        monkeypatch, owner_browser, server_fetch_forbidden, path):
    task = _task()
    if path == 'streaming':
        result = _streaming(monkeypatch, task, 'fetch_url', {'url': URL})
    else:
        result = _serial(monkeypatch, task, '_handle_fetch_url', 'fetch_url', {'url': URL})

    assert owner_browser == ['fetch_url_via_browser']
    assert PAGE_TEXT in result
    assert WITHHELD not in result


def test_owner_attended_search_keeps_the_browser_search_fallback(
        monkeypatch, owner_browser, engines_unreachable):
    result = _streaming(monkeypatch, _task(), 'web_search', {'query': 'quarterly report'})

    assert owner_browser == ['send_browser_command']
    assert WITHHELD not in result


@pytest.mark.parametrize('kind', sorted(DELEGATED))
def test_task_selection_records_the_refusal_without_a_device_lookup(
        monkeypatch, owner_browser, kind):
    from lib.browser import queue
    from lib.search.browser_scope import select_search_browser

    monkeypatch.setattr(queue, 'get_connected_clients',
                        lambda **kwargs: pytest.fail('a withheld browser is never looked up'))
    binding = select_search_browser(task=DELEGATED[kind](), client_id=DEVICE)

    assert binding == (str(OWNER), '', '', EXPECTED_REFUSAL[kind])


def test_owner_request_and_owner_attended_task_select_the_owner_device(owner_browser):
    from lib.search.browser_scope import select_search_browser

    assert select_search_browser(task=None, user_id=OWNER) == (str(OWNER), DEVICE, PROFILE, '')
    assert select_search_browser(task=_task()) == (str(OWNER), DEVICE, PROFILE, '')


def test_owner_source_verdict_is_read_once_per_turn(monkeypatch, owner_browser):
    import lib.turn_lifecycle as turn_lifecycle
    from lib.search.browser_scope import select_search_browser

    reads = []
    original = turn_lifecycle.get_turn
    monkeypatch.setattr(turn_lifecycle, 'get_turn', lambda *args, **kwargs: (
        reads.append(args[1]) or original(*args, **kwargs)))
    task = _task()
    for _ in range(3):
        assert select_search_browser(task=task).client_id == DEVICE
    assert reads == ['reply-to-owner', 'owner-message']


def test_unverifiable_owner_request_fails_closed_and_is_retried(monkeypatch, owner_browser):
    import lib.turn_lifecycle as turn_lifecycle
    from lib.search.browser_scope import select_search_browser

    healthy = turn_lifecycle.get_turn

    def unavailable(*_args, **_kwargs):
        raise RuntimeError('storage unavailable')

    task = _task()
    monkeypatch.setattr(turn_lifecycle, 'get_turn', unavailable)
    assert select_search_browser(task=task) == (str(OWNER), '', '', 'unverified_request')
    monkeypatch.setattr(turn_lifecycle, 'get_turn', healthy)
    assert select_search_browser(task=task).client_id == DEVICE


def test_every_browser_selection_states_on_whose_behalf_it_runs(owner_browser):
    from lib.search.browser_scope import bind_search_browser, select_search_browser

    with pytest.raises(TypeError):
        select_search_browser(user_id=OWNER)
    with pytest.raises(TypeError):
        with bind_search_browser(user_id=OWNER):
            pytest.fail('an undeclared caller cannot borrow the owner browser')
    with pytest.raises(TypeError):
        select_search_browser(task=_task(), user_id=OWNER)


def test_refusal_text_names_the_reason_but_no_session_material(owner_browser):
    from lib.search.browser_scope import OWNER_BROWSER_REFUSALS, withheld_browser_notice
    from lib.search.search_bridge import _ChatuiBrowserProvider, bind_search_browser

    for kind, refusal in EXPECTED_REFUSAL.items():
        with bind_search_browser(task=DELEGATED[kind]()):
            assert withheld_browser_notice() == '', 'no browser use was attempted yet'
            assert _ChatuiBrowserProvider().bind().is_connected() is False
            notice = withheld_browser_notice()
        assert OWNER_BROWSER_REFUSALS[refusal] in notice
        assert all(marker not in notice for marker in LEAK_MARKERS)
