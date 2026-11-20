"""Known credential values never reach storage or later model rounds.

Every credential-vault value and every provider key in ``server_config.json``
is replaced by ``[redacted credential: <label>]`` before persistence: raw, after
a ``Bearer`` prefix, and URL-encoded. A secret a command prints without being
injected (``cat`` of a file, an MCP server echoing it) is the case the
injection-seam redaction cannot see. All values here are synthetic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

import pytest

pytestmark = pytest.mark.unit

VAULT_SECRET = 'ghp_KnownVaultValue0123456789abcdefXYZ'
VAULT_MARKER = '[redacted credential: GITHUB_TOKEN]'
PROVIDER_KEY = 'appid/Provider+Key=0123456789abcdef'
PROVIDER_MARKER = '[redacted credential: provider example-corp]'


@pytest.fixture()
def known(tmp_path, monkeypatch):
    import lib.tool_results.credential_redaction as redaction
    import lib.credentials_vault as vault
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    config = tmp_path / 'server_config.json'
    monkeypatch.setattr(redaction, '_server_config_path', lambda: str(config))
    monkeypatch.setattr(redaction, '_cached', {})
    monkeypatch.setattr(redaction, '_model_routing_values', lambda owner: iter(()))
    vault.set_entry('github_token', VAULT_SECRET)
    config.write_text(json.dumps({'providers': [
        {'id': 'default', 'api_keys': [PROVIDER_KEY, 'EMPTY']},
        {'id': 'oauth_codex', 'oauth': True, 'api_keys': ['oauth-managed']},
    ]}))
    return SimpleNamespace(vault=vault, config=config, redaction=redaction,
                           redact=lambda text: redaction.known_credential_redactor().redact(text))


def _leaks(text: str) -> list[str]:
    forms = {VAULT_SECRET, PROVIDER_KEY, quote(PROVIDER_KEY, safe=''),
             quote_plus(PROVIDER_KEY, safe='')}
    return sorted(form for form in forms if form in text)


def _shell_echo(tmp_path) -> str:
    """A real shell command printing secrets it was never injected with."""
    from lib.project_mod.run_command import tool_run_command
    secrets_file = tmp_path / 'leaky.env'
    secrets_file.write_text(
        f'Authorization: Bearer {VAULT_SECRET}\n'
        f'url=https://gw.example.test/v1?key={quote(PROVIDER_KEY, safe="")}\n'
        f'form=key={quote_plus(PROVIDER_KEY, safe="")}\n'
        f'raw={PROVIDER_KEY}\n')
    out = tool_run_command(str(tmp_path), f'cat {secrets_file.name}')
    assert _leaks(out), 'fixture must reproduce the unredacted command output'
    return out


def _v1_client(captured):
    from lib.storage.client import StorageClient
    client = StorageClient.__new__(StorageClient)
    client._call = lambda kind, operation, payload, **kw: captured.append(payload) or {}
    return client


def _v2_client(captured):
    from lib.storage.v2_client import StorageV2Client
    client = StorageV2Client.__new__(StorageV2Client)
    client._owner_id = 1
    client._call = lambda kind, operation, payload, **kw: captured.append(payload) or {}
    return client


@pytest.mark.parametrize('make_client', [_v1_client, _v2_client])
def test_shell_output_is_redacted_before_storage(known, tmp_path, make_client):
    out = _shell_echo(tmp_path)
    captured = []
    payload = {'namespace': 'swarm_run_result', 'key': 'paper-b',
               'value': {'rounds': [{'toolContent': out}], 'tags': ('t', out)}}
    make_client(captured).command('record.put', payload, 'cmd-1')
    stored = json.dumps(captured[0])
    assert _leaks(stored) == []
    assert 'Bearer ' + VAULT_MARKER in stored
    assert stored.count(PROVIDER_MARKER) == 2 * 3  # URL-encoded, form-encoded, raw; twice
    assert payload['value']['rounds'][0]['toolContent'] == out, 'caller data is not mutated'


def test_storage_payload_without_secrets_is_passed_through_unchanged(known):
    captured = []
    payload = {'events': [{'type': 'delta', 'text': 'EMPTY oauth-managed ordinary'}]}
    _v1_client(captured).command('event.append_batch', payload, None)
    assert captured[0] is payload


def _pipeline_task(handler, name):
    from tests.tasks_pkg.test_tool_outcome_finality import task_for
    return task_for(handler, name=name)


def _run_pipeline(task, name):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _, row, _ = _build_tool_round_entry(name, {}, 'call-1', '{}', 1, False)
    task['toolRounds'] = [row]
    call = {'id': 'call-1', 'type': 'function',
            'function': {'name': name, 'arguments': '{}'}}
    messages = []
    execute_tool_pipeline(
        task, [(call, name, 'call-1', {}, 1, row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=0, model='test-model')
    return messages, row


def test_shell_result_is_redacted_for_the_model_and_the_round(known, tmp_path, monkeypatch):
    out = _shell_echo(tmp_path)
    events = []
    monkeypatch.setattr('lib.tasks_pkg.tool_dispatch._pipeline.append_event',
                        lambda task, event: events.append(json.dumps(event, default=str)))

    def handler(task, tc, fn_name, tc_id, fn_args, rn, round_entry, *rest, **kw):
        return tc_id, out, False

    messages, row = _run_pipeline(_pipeline_task(handler, 'run_command'), 'run_command')
    model_facing = json.dumps(messages, default=str)
    assert _leaks(model_facing) == [] and VAULT_MARKER in model_facing
    assert _leaks(json.dumps(row, default=str)) == []
    assert events and _leaks(''.join(events)) == []


def test_mcp_tool_result_is_redacted_for_the_model_and_storage(known, monkeypatch):
    from lib.tasks_pkg.handlers.mcp import handle_mcp_tool
    from tests.mcp_protocol.test_mcp_call_health import _make_bridge_with_server
    bridge, name = _make_bridge_with_server()
    bridge._tool_index[name]['read_only_hint'] = True
    echoed = f'server config: token={VAULT_SECRET} appid={PROVIDER_KEY}'

    async def call_tool(tool_name, arguments=None, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=echoed)],
                               isError=False, structuredContent=None)

    bridge._servers['hope'].session.call_tool = call_tool
    monkeypatch.setattr(bridge, '_run_async',
                        lambda coro, timeout=None, **kw: asyncio.run(coro))
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    task = _pipeline_task(handle_mcp_tool, name)
    messages, row = _run_pipeline(task, name)
    model_facing = json.dumps(messages, default=str)
    assert 'token=' + VAULT_MARKER in model_facing, model_facing
    assert _leaks(model_facing) == []
    captured = []
    _v2_client(captured).command('turn.projection.update', {'rounds': [row]}, 'cmd-2')
    assert _leaks(json.dumps(captured[0], default=str)) == []


def test_known_set_follows_vault_and_server_config_changes(known):
    redact = known.redact
    assert redact(VAULT_SECRET) == VAULT_MARKER
    rotated = 'ghp_RotatedValue9876543210zyxwvuTSR'
    known.vault.set_entry('github_token', rotated)
    known.vault.set_entry('pypi_token', 'pypi-AgEIcHlwaS5vcmcCJGFiY2Rl')
    assert redact(rotated) == VAULT_MARKER
    assert redact('pypi-AgEIcHlwaS5vcmcCJGFiY2Rl') == '[redacted credential: PYPI_TOKEN]'
    assert redact(VAULT_SECRET) == VAULT_SECRET, 'a replaced value is no longer a known credential'
    known.config.write_text(json.dumps({'providers': [], 'mt_provider': {
        'api_key': 'mt-secret-key-0123456789'}}))
    assert redact(PROVIDER_KEY) == PROVIDER_KEY
    assert redact('x mt-secret-key-0123456789 y') == 'x [redacted credential: provider mt_provider] y'


def test_matching_is_exact_on_known_values_only(known):
    redact = known.redact
    text = 'EMPTY oauth-managed ghp_KnownVault sk-unknown-0123456789abcdef'
    assert redact(text) == text
    # The longest known value wins where one contains another.
    known.vault.set_entry('long_token', VAULT_SECRET + '-suffix-extension')
    assert redact(f'a {VAULT_SECRET}-suffix-extension b {VAULT_SECRET}') == (
        f'a [redacted credential: LONG_TOKEN] b {VAULT_MARKER}')


def test_large_output_with_many_known_values(known):
    for index in range(40):
        known.vault.set_entry(f'bulk_{index}', f'bulk-secret-{index:02d}-0123456789abcdef')
    filler = 'line of ordinary build output 0123456789 abcdef ghp_ sk- appid\n' * 120_000
    text = filler + f'Bearer {VAULT_SECRET}\n' + filler + 'bulk-secret-07-0123456789abcdef'
    redacted = known.redact(text)
    assert redacted.endswith('[redacted credential: BULK_7]')
    assert 'Bearer ' + VAULT_MARKER in redacted and VAULT_SECRET not in redacted
    assert redacted.count('[redacted credential: ') == 2
    assert redacted.startswith(filler)


def test_values_are_never_logged(known, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    known.redact(f'{VAULT_SECRET} {PROVIDER_KEY}')
    known.config.write_text('{not json')
    known.redact(VAULT_SECRET)
    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'reload failed' in logged
    assert _leaks(logged) == []
    # The previous set stays active while server_config is unreadable.
    assert known.redact(PROVIDER_KEY) == PROVIDER_MARKER


def test_block_filter_finds_every_occurrence_at_every_alignment(known):
    import random
    import string
    from lib.tool_results.credential_redaction import KnownCredentialRedactor, credential_variants
    rnd = random.Random(20261118)
    alphabet = string.ascii_letters + string.digits + '/+=%-_ '
    for _ in range(200):
        values = [(f'K{index}', ''.join(rnd.choice(alphabet.strip())
                                         for _ in range(rnd.randint(8, 70))))
                  for index in range(rnd.randint(1, 6))]
        redactor = KnownCredentialRedactor.build(values)
        markers = {}
        for label, value in values:
            for variant in credential_variants(value):
                markers.setdefault(variant, f'[redacted credential: {label}]')
        pieces = []
        for _ in range(rnd.randint(1, 8)):
            pieces.append(''.join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 40))))
            pieces.append(rnd.choice(list(markers)))
        text = ''.join(pieces)
        expected = text
        for variant in sorted(markers, key=len, reverse=True):
            expected = expected.replace(variant, markers[variant])
        assert redactor.redact(text) == expected
        # Streaming in random chunks yields the same text as one redaction.
        streamed, pending, cursor = [], '', 0
        while cursor < len(text):
            step = rnd.randint(1, 30)
            out, pending = redactor.redact_stream(pending, text[cursor:cursor + step])
            streamed.append(out)
            cursor += step
        streamed.append(redactor.redact_stream(pending, '', final=True)[0])
        assert ''.join(streamed) == expected


def test_reads_return_stored_records_unchanged(known):
    """Write-time only: existing records are neither rewritten nor masked on read."""
    from lib.storage.client import StorageClient
    client = StorageClient.__new__(StorageClient)
    stored = {'value': {'toolContent': f'legacy {VAULT_SECRET}'}}
    client._call = lambda kind, operation, payload, **kw: stored
    assert client.query('record.get', {'namespace': 'n', 'key': 'k'}) is stored


@pytest.mark.parametrize('matched', [False, True])
def test_shared_immutable_string_is_scanned_once_per_value_walk(monkeypatch, matched):
    import secrets
    from lib.tool_results.credential_redaction import KnownCredentialRedactor
    value = secrets.token_urlsafe(24)
    text = 'ordinary output ' + value if matched else 'ordinary output without a credential'
    redactor = KnownCredentialRedactor.build([('synthetic', value)])
    scans = []
    original = KnownCredentialRedactor._matches

    def matches(self, candidate):
        if candidate is text:
            scans.append(1)
        return original(self, candidate)

    monkeypatch.setattr(KnownCredentialRedactor, '_matches', matches)
    payload = {'first': text, 'second': [text, (text,)]}
    for count in (1, 2):
        result = redactor.redact_value(payload)
        assert len(scans) == count
        expected = 'ordinary output [redacted credential: synthetic]' if matched else text
        assert result == {'first': expected, 'second': [expected, (expected,)]}
        assert (result is payload) is (not matched)
        assert payload['first'] is text
    payload['first'] = 'a changed input'
    assert redactor.redact_value(payload)['first'] == 'a changed input'


def test_string_subclasses_keep_their_independent_redaction_calls(monkeypatch):
    import secrets
    from lib.tool_results.credential_redaction import KnownCredentialRedactor

    class Text(str):
        pass

    text = Text('ordinary output without a credential')
    redactor = KnownCredentialRedactor.build([('synthetic', secrets.token_urlsafe(24))])
    calls = []
    original = KnownCredentialRedactor.redact

    def redact(self, value):
        calls.append(1)
        return original(self, value)

    monkeypatch.setattr(KnownCredentialRedactor, 'redact', redact)
    payload = [text, text]
    assert redactor.redact_value(payload) is payload
    assert len(calls) == 2


def test_string_memo_does_not_cross_snapshots_or_hide_container_mutation():
    import secrets
    from lib.tool_results.credential_redaction import KnownCredentialRedactor
    text = secrets.token_urlsafe(24)
    first = KnownCredentialRedactor.build([('other', secrets.token_urlsafe(24))])
    second = KnownCredentialRedactor.build([('synthetic', text)])
    shared = {'detail': text}
    payload = [shared, shared]
    assert first.redact_value(payload) is payload
    expected = [{'detail': '[redacted credential: synthetic]'}] * 2
    assert second.redact_value(payload) == expected
    shared['detail'] = 'ordinary changed detail'
    assert second.redact_value(payload) is payload
    shared['detail'] = text
    assert second.redact_value(payload) == expected
    assert first.redact_value(payload) is payload


def test_distinct_equal_strings_are_not_identity_memo_hits(monkeypatch):
    import secrets
    from lib.tool_results.credential_redaction import KnownCredentialRedactor
    text = secrets.token_urlsafe(24)
    other = text.encode().decode()
    assert text == other and text is not other
    redactor = KnownCredentialRedactor.build([('synthetic', text)])
    calls = []
    original = KnownCredentialRedactor.redact

    def redact(self, value):
        calls.append(1)
        return original(self, value)

    monkeypatch.setattr(KnownCredentialRedactor, 'redact', redact)
    assert redactor.redact_value([text, other]) == ['[redacted credential: synthetic]'] * 2
    assert len(calls) == 2


def test_failed_walk_does_not_keep_string_results_for_retry(monkeypatch):
    import secrets
    from lib.tool_results.credential_redaction import KnownCredentialRedactor
    text = secrets.token_urlsafe(24)
    redactor = KnownCredentialRedactor.build([('synthetic', text)])
    calls = []
    original = KnownCredentialRedactor.redact

    def redact(self, value):
        if value is text:
            calls.append(1)
        else:
            raise RuntimeError('synthetic failure')
        return original(self, value)

    monkeypatch.setattr(KnownCredentialRedactor, 'redact', redact)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        redactor.redact_value([text, text, 'ordinary other input'])
    assert redactor.redact_value([text, text]) == ['[redacted credential: synthetic]'] * 2
    assert len(calls) == 2
