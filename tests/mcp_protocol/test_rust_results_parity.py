"""The Rust MCP client produces the results Tofu's result and display layers consume.

Every case connects the same recorded MCP session (tests/fixtures/mcp_sessions:
``results_like.json``, ``display_like.json``) once through the Python bridge
and once through the Rust client (``RustMCPBridge`` over ``tofu-mcp-host``),
compares the two outcomes (text, every runtime payload, failure category), and
then feeds BOTH results to the same consumer the oracle file tests (document
decoding, tool_display, canvas links, project-name ingestion, MCP media
capture, the Turn projection, the browser renderer), asserting identical
output and the oracle file's own expectation on the Rust-produced one.

Where an oracle case exercises an SDK attribute spelling or a pure helper, the
Rust owner is driven through the host protocol on the wire-form equivalent
(``project_result``, ``merge_result_parts``, ``extract_structured_content``,
``tool_result_is_error``, ``index_tool``, ``namespaced_names``).
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import CancelledError
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.mcp_protocol.rust_parity_support import (  # noqa: F401 - fixtures
    ROOT, Scene, mcp_host_binary, outcome, rust_host, scene,
)
from tests.support.overlay import requires_yourprovider_overlay
__all__ = ['mcp_host_binary', 'rust_host', 'scene']

pytestmark = pytest.mark.unit

COVERS: dict[str, list[str]] = {
    'test_mcp_result_compat.py': [
        'test_error_flag_spellings_match_python',
        'test_error_flag_over_the_wire_matches_python',
    ],
    'test_mcp_structured_content.py': [
        'test_structured_content_extraction_matches_python',
        'test_merge_result_parts_matches_python',
        'test_structured_only_result_surfaces_on_both',
        'test_text_echo_of_structured_content_is_deduplicated_on_both',
        'test_structured_result_budget_on_both',
    ],
    'test_mcp_documents.py': [
        'test_document_survives_display_truncation_on_both',
        'test_empty_structured_documents_stay_distinct_on_both',
        'test_legacy_errors_or_mixed_display_are_not_documents_on_both',
        'test_exact_legacy_json_and_error_flag_on_both',
        'test_legacy_json_uses_original_bytes_on_both',
        'test_oversized_or_invalid_structured_content_on_both',
        'test_lone_surrogate_structured_content_is_never_a_success_on_both',
        'test_nested_documents_are_bounded_on_both',
        'test_document_cancellation_on_both',
        'test_pre_cancelled_document_never_reaches_the_server_on_both',
        'test_document_transport_loss_is_not_replayed_on_both',
        'test_document_deadline_on_both',
        'test_document_deadline_validation_matches_python',
        'test_document_default_and_long_deadline_on_both',
    ],
    'test_mcp_annotations_output_schema.py': [
        'test_annotation_and_output_schema_rows_match_python',
        'test_recorded_catalog_annotations_match_python',
    ],
    'test_mcp_readonly_hint_spelling.py': [
        'test_read_only_hint_rows_match_python',
        'test_recorded_catalog_annotations_match_python',
    ],
    'test_mcp_provider_safe_tool_names.py': [
        'test_provider_safe_names_match_python',
        'test_recorded_catalog_names_are_provider_safe_on_both',
        'test_call_dispatches_the_raw_tool_name_on_both',
        'test_disabling_the_raw_name_refuses_on_both',
    ],
    'test_mcp_tool_toggle.py': [
        'test_disabled_tool_is_hidden_and_dropped_from_safety_on_both',
        'test_disabled_filter_is_per_server_on_both',
        'test_no_config_row_means_everything_enabled_on_both',
        'test_disabled_tool_is_refused_and_enabled_tool_passes_on_both',
        'test_set_disabled_tools_hot_applies_on_both',
        'test_set_disabled_tools_normalises_input_on_both',
    ],
    'test_mcp_result_images.py': [
        'test_image_rides_out_of_band_on_both',
        'test_adapter_captures_the_rust_image_before_finalizing',
        'test_capture_projects_authoritative_images_from_both',
        'test_capture_rejects_invalid_base64_from_both',
        'test_ingest_uses_owner_scoped_authority_for_both',
    ],
    'test_mcp_media_fidelity.py': [
        'test_mixed_document_bytes_order_and_caption_from_both',
        'test_invalid_media_is_typed_unavailable_from_both',
        'test_retention_failure_does_not_claim_saved_from_both',
        'test_large_error_body_is_canonical_on_both',
        'test_oversized_native_image_is_not_retained_on_both',
        'test_embedded_resource_text_on_both',
        'test_owner_unavailable_from_both',
        'test_exact_decoded_byte_limit_from_both',
    ],
    'test_mcp_image_frontend_projection.py': [
        'test_projected_image_from_both_renders_as_interactive_thumbnail',
    ],
    'test_mcp_display_modifiers.py': [
        'test_display_modifier_titles_from_rust_catalog_names',
        'test_post_build_title_after_a_rust_call',
    ],
    'test_mcp_display_owner_cache.py': [
        'test_cached_project_name_stays_with_its_owner_from_both',
        'test_private_doc_urls_stay_with_owner_from_both',
        'test_ingestion_requires_an_explicit_owner_for_both',
        'test_structured_traversal_is_bounded_from_both',
        'test_unsafe_learned_url_is_not_rendered_from_both',
    ],
    'test_mcp_tool_links.py': [
        'test_overleaf_links_from_rust_catalog_names',
        'test_overleaf_learned_names_and_bases_from_both',
        'test_xuecheng_links_from_both',
        'test_xuecheng_default_base_from_rust_catalog_name',
        'test_non_linkable_and_batch_path_titles_from_rust_catalog_names',
        'test_post_build_title_after_a_rust_call',
        'test_settle_upgrades_from_both',
        'test_read_doc_settle_swaps_id_for_title_from_both',
    ],
    'test_llm_canvas_link_policy.py': [
        'test_canvas_policy_emission_from_rust_catalog_names',
    ],
    'test_llm_canvas_links.py': [
        'test_numeric_start_and_failure_keep_run_links_from_both',
        'test_name_only_resolves_from_successful_experiment_id_from_both',
        'test_fork_new_canvas_is_not_source_from_both',
        'test_untrusted_result_urls_do_not_link_names_from_both',
        'test_run_url_and_other_servers_from_rust_catalog_names',
        'test_progressive_start_uses_underlying_rust_catalog_name',
        'test_capped_untrusted_label_from_both',
        'test_batch_ids_and_dry_run_source_only_from_both',
        'test_settle_removes_stale_name_link_after_rust_failure',
        'test_historical_single_item_fork_wrapper_from_both',
        'test_hostile_wrapper_keeps_only_source_from_both',
        'test_original_retained_source_requires_owning_evidence_from_both',
        'test_producer_envelope_cannot_manufacture_sidecar_from_both',
    ],
}

DISPLAY_SERVERS = ('llm', 'overleaf', 'xuecheng', 'hope', 'github', 'github-batch', 'docs', 'x', 'other')
RESULT_SERVERS = ('papers', 'sheets')
BASE = 'https://model.internal.example.com/user/modelManage/experimentalTaskDetail/'
PID = '6a1e782e9ba0ae3d7727a668'


def _run(call):
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - compared as an outcome
        return exc


def _value(result):
    if isinstance(result, BaseException):
        raise result
    return result


def wire(value, *, error=False):
    """The CallToolResult a server sends for ``value``: JSON text, plus structuredContent for objects."""
    if isinstance(value, str):
        result = {'content': [{'type': 'text', 'text': value}]}
    else:
        result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
        if isinstance(value, dict):
            result['structuredContent'] = value
    if error:
        result['isError'] = True
    return result


class Duo:
    """One recorded session connected under several server names through each client."""

    def __init__(self, scene, host, servers, session):
        from lib.mcp.client import MCPBridge
        from lib.mcp.rust_host import RustMCPBridge
        self.scene, self.host = scene, host
        self.py = MCPBridge(owner_user_id=1)
        self.py._stored_server_config = lambda name, refresh_browser=False: (None, None)
        self.rs = RustMCPBridge(host, 1)
        for server in servers:
            self.py.connect_server(server, {'command': scene.launcher(f'py-{server}', 'replay-mcp', session)})
            self.rs.connect_server(server, {'command': scene.launcher(f'rs-{server}', 'replay-mcp', session)})
        rows = [{(info['server_name'], info['tool_name']): name for name, info, _ in bridge.tool_index_rows()}
                for bridge in (self.py, self.rs)]
        assert rows[0] == rows[1]
        self.names = rows[1]

    def name(self, server, tool):
        """The namespaced name the Rust catalog gives ``server``'s raw ``tool`` (equal to Python's)."""
        return self.names[(server, tool)]

    def call(self, server, tool, arguments=None, *, case=None, result=None, **options):
        arguments = dict(arguments or {})
        if case is not None:
            arguments['case'] = case
        if result is not None:
            arguments['result'] = result
        name = self.name(server, tool)
        py = _run(lambda: self.py.call_tool(name, dict(arguments), **options))
        rs = _run(lambda: self.rs.call_tool(name, dict(arguments), **options))
        assert outcome(lambda: _value(rs)) == outcome(lambda: _value(py)), f'{server}/{tool}'
        return py, rs

    def received(self, which, server, tool=None):
        return [params for params in self.scene.calls(f'{which}-{server}')
                if tool is None or params['name'] == tool]

    def close(self):
        self.py.disconnect_all()
        self.rs.disconnect_all()


def _module_duo(binary, tmp_path_factory, servers, session):
    from lib.mcp.rust_host import RustMCPHost
    monkeypatch = pytest.MonkeyPatch()
    scene = Scene(tmp_path_factory.mktemp(session.split('.')[0]), monkeypatch)
    host = RustMCPHost(binary, env=dict(os.environ))
    host.call('configure', scene.host_settings(), timeout=30)
    duo = Duo(scene, host, servers, session)
    try:
        yield duo
    finally:
        duo.close()
        host.close()
        monkeypatch.undo()


@pytest.fixture(scope='module')
def display(mcp_host_binary, tmp_path_factory):
    yield from _module_duo(mcp_host_binary, tmp_path_factory, DISPLAY_SERVERS, 'display_like.json')


@pytest.fixture(scope='module')
def results(mcp_host_binary, tmp_path_factory):
    yield from _module_duo(mcp_host_binary, tmp_path_factory, RESULT_SERVERS, 'results_like.json')


@pytest.fixture
def toggle(scene, rust_host):
    duo = Duo(scene, rust_host, ('srv', 'a', 'b'), 'results_like.json')
    yield duo
    duo.close()


@pytest.fixture
def names_cache():
    from lib.mcp import project_names
    project_names.clear_cache()
    yield project_names
    project_names.clear_cache()


def _png(color='blue', size=(3, 2)):
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


# ── test_mcp_result_compat.py ──────────────────────────────────────────────

ERROR_FLAGS = [{'is_error': True}, {'is_error': False}, {'isError': True}, {'isError': False}, {},
               {'is_error': False, 'isError': True}, {'is_error': None, 'isError': True}]


@pytest.mark.parametrize('flags', ERROR_FLAGS)
def test_error_flag_spellings_match_python(rust_host, flags):
    from lib.mcp.client._bridge import _tool_result_is_error
    expected = _tool_result_is_error(SimpleNamespace(**flags))
    assert rust_host.call('tool_result_is_error', {'result': flags}, timeout=30) is expected
    projected = rust_host.call('project_result', {'result': {'content': [], **flags}}, timeout=30)
    assert projected['tool_result']['is_error'] is expected


@pytest.mark.parametrize('error', [True, False])
def test_error_flag_over_the_wire_matches_python(results, error):
    py, rs = results.call('papers', 'bundle', result=wire('job not found', error=error))
    assert rs.is_error is error and str(rs).startswith('MCP Error: ') is error


# ── test_mcp_structured_content.py ─────────────────────────────────────────

STRUCTURED = [
    {'structured_content': {'ok': True, 'rows': [{'id': 1}]}},
    {'structuredContent': {'ok': True, 'rows': [{'id': 1}]}},
    {'structured_content': {'v': 'snake'}, 'structuredContent': {'v': 'camel'}},
    {}, {'structured_content': {}}, {'structuredContent': []},
]


@pytest.mark.parametrize('fields', STRUCTURED)
def test_structured_content_extraction_matches_python(rust_host, fields):
    from lib.mcp.client._bridge import MCPBridge
    python = MCPBridge._extract_structured_content(SimpleNamespace(
        content=[], structured_content=fields.get('structured_content'),
        structuredContent=fields.get('structuredContent')))
    assert rust_host.call('extract_structured_content', {'result': fields}, timeout=30) == python


MERGES = [
    ('hello', None), ('hello', ''), ('', '{"rows": [1]}'), ('   ', '{"rows": [1]}'),
    ('done', '{"rows": [1, 2, 3]}'), ('{"rows": [1, 2, 3]}', '{"rows": [1, 2, 3]}'),
    ('{"ok": false, "error": "SSO用户信息为空", "code": 401}', '{"code":401,"error":"SSO\\u7528\\u6237信息为空","ok":false}'),
    ('MCP Error: {"ok": false, "error": "SSO用户信息为空", "code": 401}',
     '{"code":401,"error":"SSO\\u7528\\u6237信息为空","ok":false}'),
    ('MCP Error: {"ok": false}', '{"ok":false,"error_code":"SSO_PERMISSION"}'),
]


@pytest.mark.parametrize('text, structured', MERGES)
def test_merge_result_parts_matches_python(rust_host, text, structured):
    from lib.mcp.client import MCPBridge
    assert rust_host.call('merge_result_parts', {'text': text, 'structured': structured}, timeout=30) == \
        MCPBridge._merge_result_parts(text, structured)


def test_structured_only_result_surfaces_on_both(results):
    _, rs = results.call('papers', 'bundle', result={'content': [], 'structuredContent': {'rows': [1, 2, 3]}})
    assert '[1, 2, 3]' in rs


def test_text_echo_of_structured_content_is_deduplicated_on_both(results):
    from lib.mcp.client._bridge import MCPBridge
    payload = {'rows': [1, 2, 3]}
    serialized = MCPBridge._extract_structured_content(SimpleNamespace(structured_content=payload))
    _, rs = results.call('papers', 'bundle', result={
        'content': [{'type': 'text', 'text': serialized}], 'structuredContent': payload})
    assert rs == serialized


def test_structured_result_budget_on_both(results):
    from lib.mcp.types import MCP_MAX_RESULT_CHARS
    _, rs = results.call('papers', 'bundle', result={
        'content': [], 'structuredContent': {'blob': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}})
    assert len(rs) <= MCP_MAX_RESULT_CHARS + 200 and '[Truncated:' in rs


# ── test_mcp_documents.py ──────────────────────────────────────────────────

def _documents(duo, arguments, timeout=30):
    """``call_tool_document`` on both: Python's bridge, and the Rust host's own decoder."""
    from lib.mcp.documents import MCPDocumentError
    from lib.mcp.rust_host import RustHostError
    name = duo.name('papers', 'bundle')
    try:
        python = ('document', duo.py.call_tool_document(name, dict(arguments), timeout=timeout))
    except MCPDocumentError as exc:
        python = ('invalid', str(exc))
    except Exception as exc:  # noqa: BLE001 - the category is the comparison
        from lib.mcp.bridge_relay import _error_kind
        python = (_error_kind(exc), None)
    try:
        rust = ('document', duo.host.call('call_tool_document', {
            'owner_user_id': 1, 'name': name, 'arguments': dict(arguments), 'timeout': timeout}))
    except RustHostError as exc:
        rust = (exc.kind, str(exc) if exc.kind == 'invalid' else None)
    if python[0] != 'document' and python[1] is None:
        rust = (rust[0], None)
    assert rust == python
    return rust


def test_document_survives_display_truncation_on_both(results):
    from lib.mcp.documents import decode_document
    from lib.mcp.types import MCP_MAX_RESULT_CHARS
    payload = {'chunk': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}
    result = {'content': [{'type': 'text', 'text': 'Build complete'}], 'structuredContent': payload}
    _, rs = results.call('papers', 'bundle', result=result)
    assert 'Build complete' in rs and '[Truncated:' in rs and decode_document(rs) == payload
    assert _documents(results, {'result': result}) == ('document', payload)


@pytest.mark.parametrize('payload', [{}, [], {'ok': True}])
def test_empty_structured_documents_stay_distinct_on_both(results, payload):
    result = {'content': [{'type': 'text', 'text': 'Build complete'}], 'structuredContent': payload}
    if isinstance(payload, list):
        # The SDK-object ``[]`` of the oracle cannot cross the wire: the SDK model
        # (and the Rust session after it) refuses a non-object structuredContent.
        assert _documents(results, {'result': result}) == ('invalid', None)
    else:
        assert _documents(results, {'result': result}) == ('document', payload)


@pytest.mark.parametrize('text', ['Error: compile failed', 'MCP Error: expired',
                                  'Built successfully\n{"build_id":"partial"}', '{"build_id":"partial"'])
def test_legacy_errors_or_mixed_display_are_not_documents_on_both(results, text):
    assert _documents(results, {'result': wire(text)})[0] == 'invalid'


def test_exact_legacy_json_and_error_flag_on_both(results):
    assert _documents(results, {'result': wire('{"ok":true}')}) == ('document', {'ok': True})
    errored = {'content': [{'type': 'text', 'text': 'Build complete'}], 'structuredContent': {'ok': True},
               'isError': True}
    assert _documents(results, {'result': errored}) == ('invalid', 'MCP tool returned an error')


def test_legacy_json_uses_original_bytes_on_both(results):
    from lib.mcp.documents import decode_document
    from lib.mcp.types import MCP_MAX_RESULT_CHARS
    payload = {'chunk': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}
    _, rs = results.call('papers', 'bundle', result=wire(json.dumps(payload)))
    assert '[Truncated:' in rs and decode_document(rs) == payload
    assert _documents(results, {'result': wire(json.dumps(payload))}) == ('document', payload)


def _oversized(kind):
    from lib.mcp.types import MCP_MAX_DOCUMENT_BYTES, MCP_MAX_DOCUMENT_NODES
    return {'ascii': {'blob': 'x' * MCP_MAX_DOCUMENT_BYTES},
            'cjk': {'blob': '\u4e2d' * (MCP_MAX_DOCUMENT_BYTES // 2)},
            'nodes': {'items': [0] * MCP_MAX_DOCUMENT_NODES}}[kind]


@pytest.mark.parametrize('kind', ['ascii', 'cjk', 'nodes'])
def test_oversized_or_invalid_structured_content_on_both(results, kind):
    from lib.mcp.documents import MCPDocumentError, decode_document
    payload = _oversized(kind)
    _, rs = results.call('papers', 'bundle', result={
        'content': [{'type': 'text', 'text': '{"ok":true}'}], 'structuredContent': payload})
    if kind in ('ascii', 'cjk'):
        assert rs.structured_json is not None
    else:
        assert 'Structured result omitted' in rs and rs.structured_json is None
    with pytest.raises(MCPDocumentError):
        decode_document(rs)
    assert _documents(results, {'result': {'content': [{'type': 'text', 'text': '{"ok":true}'}],
                                           'structuredContent': payload}})[0] == 'invalid'


def test_lone_surrogate_structured_content_is_never_a_success_on_both(results):
    # Neither SDK parses a response carrying a lone surrogate: the call ends at its deadline.
    name = results.name('papers', 'bundle')
    for bridge in (results.py, results.rs):
        assert outcome(lambda: bridge.call_tool(name, {'case': 'surrogate_structured'}, timeout_override=1))[:2] == \
            ('raised', 'timeout')
    assert _documents(results, {'result': wire('{"ok":true}')}) == ('document', {'ok': True})


def test_nested_documents_are_bounded_on_both(results):
    assert _documents(results, {'result': wire('[' * 80 + '0' + ']' * 80)})[0] == 'invalid'


def test_document_cancellation_on_both(results):
    name = results.name('papers', 'hang')
    for bridge in (results.py, results.rs):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        with pytest.raises(CancelledError):
            bridge.call_tool_document(name, {}, timeout=30, cancel=cancel)
    assert not results.py._active_calls and not results.py._timeout_streak.get('papers')
    # The Rust side kept no timeout streak either: a document call still runs.
    assert _documents(results, {'result': wire('{"ok":true}')}) == ('document', {'ok': True})


def test_pre_cancelled_document_never_reaches_the_server_on_both(results):
    name = results.name('papers', 'bundle')
    cancel = threading.Event()
    cancel.set()
    before = (len(results.received('py', 'papers')), len(results.received('rs', 'papers')))
    for bridge in (results.py, results.rs):
        with pytest.raises(CancelledError):
            bridge.call_tool_document(name, {'result': wire('{}')}, timeout=30, cancel=cancel)
    assert (len(results.received('py', 'papers')), len(results.received('rs', 'papers'))) == before


def test_document_transport_loss_is_not_replayed_on_both(results):
    name = results.name('papers', 'crash_write')
    for which, bridge in (('py', results.py), ('rs', results.rs)):
        with pytest.raises(Exception) as failure:
            bridge.call_tool_document(name, {}, timeout=30)
        from lib.mcp.bridge_relay import _error_kind
        assert _error_kind(failure.value) == 'transport'
        assert len(results.received(which, 'papers', 'crash_write')) == 1
    # Both reconnected the dead server once; the next document call is answered.
    assert _documents(results, {'result': wire('{"ok":true}')}) == ('document', {'ok': True})


def test_document_deadline_on_both(results):
    name = results.name('papers', 'hang')
    from lib.mcp.bridge_relay import _error_kind
    from lib.mcp.rust_host import RustHostError
    with pytest.raises(Exception) as python:
        results.py.call_tool_document(name, {}, timeout=1)
    with pytest.raises(RustHostError) as rust:
        results.host.call('call_tool_document', {'owner_user_id': 1, 'name': name, 'arguments': {}, 'timeout': 1})
    assert _error_kind(python.value) == rust.value.kind == 'timeout'


@pytest.mark.parametrize('timeout', [True, 0, -1, 1.5])
def test_document_deadline_validation_matches_python(results, timeout):
    from lib.mcp.rust_host import RustHostError
    name = results.name('papers', 'bundle')
    with pytest.raises(ValueError, match='positive integer') as python:
        results.py.call_tool_document(name, {}, timeout=timeout)
    with pytest.raises(RustHostError) as rust:
        results.host.call('call_tool_document', {'owner_user_id': 1, 'name': name, 'arguments': {},
                                                 'timeout': timeout})
    assert rust.value.kind == 'invalid' and str(rust.value) == str(python.value)


@pytest.mark.parametrize('timeout', [None, 420])
def test_document_default_and_long_deadline_on_both(results, timeout):
    result = {'content': [{'type': 'text', 'text': 'Build complete'}], 'structuredContent': {'ok': True}}
    assert _documents(results, {'result': result}, timeout=timeout) == ('document', {'ok': True})


# ── test_mcp_annotations_output_schema.py / test_mcp_readonly_hint_spelling.py ──

def _python_row(tool):
    from lib.mcp.client._bridge import MCPBridge
    bridge = MCPBridge()
    stand_in = SimpleNamespace(name=tool['name'], description=tool.get('description', ''),
                               inputSchema=tool.get('inputSchema'), input_schema=tool.get('inputSchema'),
                               annotations=tool.get('annotations'), meta=None,
                               outputSchema=tool.get('outputSchema'), output_schema=tool.get('output_schema'))
    handle = SimpleNamespace(catalog_fingerprint='', tools=None, catalog_version='')
    bridge._replace_server_catalog('s', handle, [stand_in])
    [info] = bridge._tool_index.values()
    return info


_COMPARED = ('server_name', 'tool_name', 'namespaced_name', 'description', 'input_schema', 'openai_def',
             'read_only_hint', 'annotations', 'output_schema', 'meta', 'catalog_version')

ANNOTATIONS = [
    {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
    {'read_only_hint': True, 'destructive_hint': False, 'idempotent_hint': True, 'open_world_hint': False},
    None, {'readOnlyHint': True, 'destructiveHint': True},
]
OUTPUT_SCHEMAS = [{'outputSchema': {'type': 'object', 'properties': {'a': {'type': 'string'}}}},
                  {'output_schema': {'type': 'object', 'properties': {'a': {'type': 'string'}}}},
                  {}, {'output_schema': 'not a dict'}]


@pytest.mark.parametrize('annotations', ANNOTATIONS)
@pytest.mark.parametrize('schema', OUTPUT_SCHEMAS)
def test_annotation_and_output_schema_rows_match_python(rust_host, annotations, schema):
    tool = {'name': 't', 'description': 'd', 'inputSchema': {'type': 'object', 'properties': {}},
            **({'annotations': annotations} if annotations is not None else {}), **schema}
    python = _python_row(tool)
    rust = rust_host.call('index_tool', {'server': 's', 'tool': tool}, timeout=30)
    for key in _COMPARED:
        assert rust[key] == json.loads(json.dumps(python[key])), key
    assert rust['read_only_hint'] is rust['annotations']['readOnlyHint']


READ_ONLY = [{'readOnlyHint': True}, {'read_only_hint': True}, {'readOnlyHint': False},
             {'read_only_hint': False}, None, {'readonly': True}, {'isReadOnly': True},
             {'readOnlyHint': 'true'}, {'readOnlyHint': 1}, {'readOnlyHint': [1]}, {'readOnlyHint': {}},
             {}, {'readOnlyHint': None, 'read_only_hint': True}]


@pytest.mark.parametrize('annotations', READ_ONLY)
def test_read_only_hint_rows_match_python(rust_host, annotations):
    from lib.mcp.client._coerce import _extract_read_only_hint
    tool = {'name': 't', 'inputSchema': {'type': 'object'}, 'annotations': annotations}
    expected = _extract_read_only_hint(SimpleNamespace(annotations=annotations, name='t'))
    rust = rust_host.call('index_tool', {'server': 's', 'tool': tool}, timeout=30)
    assert rust['read_only_hint'] is expected
    assert rust['annotations'] == _python_row(tool)['annotations']


def test_recorded_catalog_annotations_match_python(results):
    from lib.mcp.client._bridge import catalog_snapshot_row
    py = {name: info for name, info, _ in results.py.tool_index_rows()}
    rs = {name: info for name, info, _ in results.rs.tool_index_rows()}
    assert rs == json.loads(json.dumps(py))
    camel = rs[results.name('papers', 'ann_camel')]
    assert camel['read_only_hint'] is True
    assert camel['annotations'] == {'readOnlyHint': True, 'destructiveHint': False,
                                    'idempotentHint': True, 'openWorldHint': False}
    assert rs[results.name('papers', 'ann_false')]['read_only_hint'] is False
    assert rs[results.name('papers', 'ann_absent')]['annotations'] == dict.fromkeys(camel['annotations'], False)
    assert rs[results.name('papers', 'out_schema')]['output_schema'] == {
        'type': 'object', 'properties': {'a': {'type': 'string'}}}
    assert [catalog_snapshot_row(row) for row in rs.values()] == \
        json.loads(json.dumps(results.py.get_tool_catalog_snapshot()))
    assert results.rs.get_tool_safety() == results.py.get_tool_safety()


# ── test_mcp_provider_safe_tool_names.py ───────────────────────────────────

PROVIDER_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


@pytest.mark.parametrize('server, tools', [
    ('sheets', ['sheet.get_range_data']), ('srv', ['read_doc']), ('hope', ['hope_login']),
    ('amap-maps', ['maps-geo']), ('srv', ['x' * 70 + '.a', 'x' * 70 + '.b']),
    ('srv', ['a_b', 'a.b']), ('srv', ['a.b', 'a_b']),
])
def test_provider_safe_names_match_python(rust_host, server, tools):
    from lib.mcp.types import PROVIDER_TOOL_NAME_MAX, namespaced_tool_names
    rust = rust_host.call('namespaced_names', {'server': server, 'tools': tools}, timeout=30)
    assert rust == namespaced_tool_names(server, tools)
    assert all(PROVIDER_NAME.fullmatch(name) and len(name) <= PROVIDER_TOOL_NAME_MAX for name in rust.values())


def test_recorded_catalog_names_are_provider_safe_on_both(results):
    info = results.rs.get_tool_info('mcp__sheets__sheet_get_range_data')
    assert info == json.loads(json.dumps(results.py.get_tool_info('mcp__sheets__sheet_get_range_data')))
    assert info['tool_name'] == 'sheet.get_range_data'
    assert info['openai_def']['function']['name'] == info['namespaced_name']
    assert results.rs.get_openai_tool_defs() == results.py.get_openai_tool_defs()
    assert all(PROVIDER_NAME.fullmatch(d['function']['name']) for d in results.rs.get_openai_tool_defs())
    assert results.name('sheets', 'a_b') == 'mcp__sheets__a_b'
    assert results.name('sheets', 'a.b').startswith('mcp__sheets__a_b_')


def test_call_dispatches_the_raw_tool_name_on_both(results):
    py, rs = results.call('sheets', 'sheet.get_range_data')
    assert rs == 'ok'
    for which in ('py', 'rs'):
        assert [p['name'] for p in results.received(which, 'sheets')][-1] == 'sheet.get_range_data'


def test_disabling_the_raw_name_refuses_on_both(scene, rust_host):
    duo = Duo(scene, rust_host, ('sheets',), 'results_like.json')
    try:
        for bridge in (duo.py, duo.rs):
            bridge.set_disabled_tools('sheets', ['sheet.get_range_data'])
        for bridge in (duo.py, duo.rs):
            with pytest.raises(ValueError, match='disabled by user'):
                bridge.call_tool('mcp__sheets__sheet_get_range_data', {})
        assert duo.received('py', 'sheets') == duo.received('rs', 'sheets') == []
    finally:
        duo.close()


# ── test_mcp_tool_toggle.py ────────────────────────────────────────────────

def _defs(bridge):
    return [d['function']['name'] for d in bridge.get_openai_tool_defs()]


def test_disabled_tool_is_hidden_and_dropped_from_safety_on_both(toggle):
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('srv', ['drop_tool'])
    assert _defs(toggle.rs) == _defs(toggle.py)
    assert 'mcp__srv__drop_tool' not in _defs(toggle.rs) and 'mcp__srv__keep_tool' in _defs(toggle.rs)
    assert toggle.rs.get_tool_safety() == toggle.py.get_tool_safety()
    assert 'mcp__srv__drop_tool' not in toggle.rs.get_tool_safety()


def test_disabled_filter_is_per_server_on_both(toggle):
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('a', ['search'])
    assert _defs(toggle.rs) == _defs(toggle.py)
    assert 'mcp__a__search' not in _defs(toggle.rs) and 'mcp__b__search' in _defs(toggle.rs)


def test_no_config_row_means_everything_enabled_on_both(toggle):
    assert _defs(toggle.rs) == _defs(toggle.py)
    assert len(_defs(toggle.rs)) == len(toggle.rs.tool_index_rows())


def test_disabled_tool_is_refused_and_enabled_tool_passes_on_both(toggle):
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('srv', ['drop_tool'])
    py, rs = toggle.call('srv', 'drop_tool')
    assert isinstance(rs, ValueError) and 'disabled by user' in str(rs)
    py, rs = toggle.call('srv', 'keep_tool')
    assert rs == 'called keep_tool'
    assert [p['name'] for p in toggle.received('rs', 'srv')] == ['keep_tool']


def test_set_disabled_tools_hot_applies_on_both(toggle):
    before = {id(b): (b.tool_catalog_generation(), _defs(b)) for b in (toggle.py, toggle.rs)}
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('srv', ['b'])
    after = {}
    for bridge in (toggle.py, toggle.rs):
        generation, defs = bridge.tool_catalog_generation(), _defs(bridge)
        assert generation != before[id(bridge)][0]
        assert defs == [d for d in before[id(bridge)][1] if d != 'mcp__srv__b']
        after[id(bridge)] = generation
        bridge.set_disabled_tools('srv', ['b', 'b'])
        assert bridge.tool_catalog_generation() == generation
    rust = toggle.host.call('server_config', {'owner_user_id': 1, 'name': 'srv'})
    assert rust['handle_config']['disabled_tools'] == toggle.py._servers['srv'].config['disabled_tools'] == ['b']
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('srv', [])
        assert _defs(bridge) == before[id(bridge)][1]


def test_set_disabled_tools_normalises_input_on_both(toggle):
    for bridge in (toggle.py, toggle.rs):
        bridge.set_disabled_tools('srv', ['z', 'z', 'a', 42, None])
    rust = toggle.host.call('server_config', {'owner_user_id': 1, 'name': 'srv'})
    assert rust['config']['disabled_tools'] == toggle.py._configs['srv']['disabled_tools'] == ['a', 'z']


# ── test_mcp_result_images.py ──────────────────────────────────────────────

def test_image_rides_out_of_band_on_both(display):
    from lib.mcp.result_content import MCPToolResult
    data = base64.b64encode(b'png-bytes').decode()
    _, rs = display.call('docs', 'read', case='text_and_image')
    assert isinstance(rs, str) and isinstance(rs, MCPToolResult)
    assert 'document body' in rs
    assert rs.image_contents == ({'data': data, 'mimeType': 'image/png', 'sourceOrdinal': 1},)
    assert data not in str(rs)


def test_adapter_captures_the_rust_image_before_finalizing(display, monkeypatch):
    from lib.tasks_pkg.handlers import _adapter
    _, rs = display.call('docs', 'read', case='text_and_image')
    captured, finalized = [], []
    monkeypatch.setattr('lib.tasks_pkg.mcp_result_media.capture_mcp_result_images',
                        lambda task, value, **kwargs: captured.append((task, value, kwargs)) or 1)
    monkeypatch.setattr(_adapter, '_build_simple_meta', lambda *args, **kwargs: {})
    monkeypatch.setattr(_adapter, '_finalize_tool_round',
                        lambda task, rn, round_entry, results: finalized.append(results))
    task = {'id': 'task-1'}
    settled = _adapter.simple_call(task, 'mcp__docs__read', {}, 0, {}, 'call-1',
                                   executor=lambda *_a, **_k: rs, source='MCP')
    assert settled == ('call-1', rs, False)
    assert captured == [(task, rs, {'source_tool': 'mcp__docs__read', 'tool_call_id': 'call-1'})]
    assert finalized == [[{}]]


def _image_result(color='red', size=(2, 3), mime='image/png'):
    return {'content': [{'type': 'text', 'text': '[image content]'},
                        {'type': 'image', 'data': base64.b64encode(_png(color, size)).decode(), 'mimeType': mime}]}


def _image_only():
    return {'content': [{'type': 'image', 'data': base64.b64encode(_png()).decode(), 'mimeType': 'image/png'}]}


def _capture_task(owner=7):
    return {'id': 'task-1', '_userId': owner, '_mcpImages': [], '_mcpImageBytes': 0, 'config': {},
            'content': 'answer', 'thinking': '', 'toolRounds': []}


IMAGE_DOC = {'attachmentId': 'image-doc', 'preview': '/api/v4/media/attachments/image-doc/source',
             'caption': 'Image from mcp__docs__read', 'sizeKB': 12.5, 'mimeType': 'image/png',
             'sourceTool': 'mcp__docs__read', 'toolCallId': 'call-1'}


def _projections(display, monkeypatch, result=None):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    from lib.turn_lifecycle import _task_projection
    calls = []
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image',
                        lambda raw, mime, **kwargs: calls.append((raw, mime, kwargs)) or dict(IMAGE_DOC))
    projections = []
    for value in display.call('docs', 'read', result=result or _image_result()):
        task = _capture_task()
        assert capture_mcp_result_images(task, value, source_tool='mcp__docs__read', tool_call_id='call-1') == 1
        projections.append(_task_projection(task, {}))
    assert projections[1] == projections[0]
    assert calls[0] == calls[1]
    return projections[1], calls


def test_capture_projects_authoritative_images_from_both(display, monkeypatch):
    result = _image_result()
    projection, calls = _projections(display, monkeypatch, result)
    assert calls[1][0] == _png('red', (2, 3))
    assert projection['images'][0]['attachmentId'] == 'image-doc'
    assert result['content'][1]['data'] not in repr(projection)


def test_capture_rejects_invalid_base64_from_both(display, monkeypatch):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image',
                        lambda *a, **k: pytest.fail('invalid base64 must not reach storage'))
    for value in display.call('docs', 'read', case='not_base64'):
        task = {'_userId': 3, '_mcpImages': [], '_mcpImageBytes': 0}
        text = str(value)
        assert capture_mcp_result_images(task, value, source_tool='mcp__x__read', tool_call_id='call-bad') == 0
        assert str(value) == text and task['_mcpImages'] == []


def test_ingest_uses_owner_scoped_authority_for_both(display, monkeypatch):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    seen = {'add': [], 'patch': []}
    monkeypatch.setattr('lib.knowledge.add_document', lambda raw, filename, **kwargs: seen['add'].append(
        (raw, filename, kwargs)) or {'id': 'image-doc', 'name': filename, 'kind': '.png'})
    monkeypatch.setattr('lib.knowledge.patch_media_metadata',
                        lambda document_id, updates, **kwargs: seen['patch'].append((document_id, updates, kwargs)))
    tasks = []
    for value in display.call('docs', 'read', result=_image_result()):
        task = {'_userId': 11, '_mcpImages': [], '_mcpImageBytes': 0}
        assert capture_mcp_result_images(task, value, source_tool='mcp__docs__read', tool_call_id='call-9') == 1
        tasks.append(task)
    (py_raw, _, py_kwargs), (rs_raw, _, rs_kwargs) = seen['add']
    assert rs_raw == py_raw == _png('red', (2, 3))
    assert rs_kwargs['user_id'] == 11 and rs_kwargs['scope'] == 'attachment' and rs_kwargs['defer_image_ocr'] is True
    assert seen['patch'][1][1]['origin'] == 'mcp_tool_result'
    for task in tasks:
        assert task['_mcpImages'][0]['preview'] == '/api/v4/media/attachments/image-doc/source'
        assert task['_mcpImages'][0]['sourceTool'] == 'mcp__docs__read'


# ── test_mcp_media_fidelity.py ─────────────────────────────────────────────

def test_mixed_document_bytes_order_and_caption_from_both(display, monkeypatch):
    import hashlib
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    raw = _png()
    encoded = base64.b64encode(raw).decode()
    text = 'before ' + '中' * 210000
    raw_result = {'content': [
        {'type': 'text', 'text': text},
        {'type': 'image', 'data': encoded, 'mimeType': 'image/png', 'caption': 'original caption'},
        {'type': 'text', 'text': 'after'}], 'structuredContent': {'table': [1, 2]}}
    # Both SDKs (mcp, rmcp) drop the non-protocol ``caption`` from a wire ImageContent ...
    wired = display.call('x', 'read', result=raw_result)
    assert all('caption' not in image for result in wired for image in result.image_contents)
    # ... so the oracle's SDK-object block is projected by each owner directly.
    results = _projected_pair(display.host, raw_result)
    seen = []
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda data, mime, **kwargs: seen.append(data) or {
        'attachmentId': 'synthetic', 'toolCallId': 'c', 'sourceTool': 'mcp__x__read'})
    projected = []
    for result in results:
        task = {'_userId': 7}
        assert capture_mcp_result_images(task, result, source_tool='mcp__x__read', tool_call_id='c') == 1
        assert result.document_text.startswith(text) and result.document_text.endswith('{"table": [1, 2]}')
        assert result.structured_json == '{"table": [1, 2]}'
        assert '[Truncated:' in str(result) and encoded not in str(result)
        assert [b['type'] for b in result.content_blocks] == ['text', 'image', 'text']
        receipt = result.media_refs[0]
        assert receipt['sourceOrdinal'] == 1 and receipt['sha256'] == hashlib.sha256(raw).hexdigest()
        assert receipt['caption'] == 'original caption' and receipt['view'] == 'source'
        assert result._modelReplaySources == ['tofu-media:synthetic']
        assert task['_mcpImages'][0]['caption'] == 'original caption'
        assert capture_mcp_result_images(task, result, source_tool='mcp__x__read', tool_call_id='c') == 0
        projected.append((result.media_refs, task['_mcpImages']))
    assert seen == [raw, raw] and projected[1] == projected[0]


def _projected_pair(host, raw):
    """One raw CallToolResult projected by the Python bridge and by the Rust owner."""
    import asyncio
    from lib.mcp.bridge_relay import _decode_result
    from lib.mcp.client import MCPBridge

    async def call_tool(*args, **kwargs):
        return SimpleNamespace(content=raw['content'], structuredContent=raw.get('structuredContent'),
                               isError=raw.get('isError', False))
    handle = SimpleNamespace(name='fake', sdk_generation=0, session=SimpleNamespace(call_tool=call_tool))
    python = asyncio.run(MCPBridge()._async_call_tool(handle, 'read', {}, None))
    rust = _decode_result(host.call('project_result', {'result': raw}, timeout=60))
    assert outcome(lambda: rust) == outcome(lambda: python)
    return python, rust


@pytest.mark.parametrize('data, mime, reason', [
    ('not base64!', 'image/png', 'invalid_base64'),
    (base64.b64encode(b'not an image').decode(), 'image/png', 'invalid_image'),
    ('png-as-jpeg', 'image/jpeg', 'mime_mismatch'),
    ('eA==', 'image/svg+xml', 'unsupported_mime')])
def test_invalid_media_is_typed_unavailable_from_both(display, monkeypatch, data, mime, reason):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    if data == 'png-as-jpeg':
        data = base64.b64encode(_png()).decode()
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: pytest.fail('invalid input persisted'))
    refs = []
    for result in display.call('x', 'read', result={'content': [{'type': 'image', 'data': data, 'mimeType': mime}]}):
        assert capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c') == 0
        assert result.media_refs[0]['status'] == 'unavailable' and result.media_refs[0]['reason'] == reason
        assert not result._modelReplaySources
        refs.append(result.media_refs)
    assert refs[1] == refs[0]


def test_retention_failure_does_not_claim_saved_from_both(display, monkeypatch, caplog):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images

    def fail(*args, **kwargs):
        raise OSError('SECRET-media-store-detail')
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', fail)
    for result in display.call('x', 'read', result=_image_only()):
        caplog.clear()
        task = {'_userId': 1}
        assert capture_mcp_result_images(task, result, source_tool='mcp__x__r', tool_call_id='c') == 0
        receipt = [ref for ref in result.media_refs if ref.get('reason')][0]
        assert receipt['reason'] == 'retention_failed'
        assert task['_mcpImages'] == [] and not result._modelReplaySources
        assert 'retention failed for image 0' in caplog.text
        assert 'SECRET-media-store-detail' not in caplog.text + str(result.media_refs)


def test_large_error_body_is_canonical_on_both(display):
    full = 'error detail ' * 50000
    _, rs = display.call('x', 'read', result={'content': [{'type': 'text', 'text': full}],
                                              'structuredContent': {'error': 'details'}, 'isError': True})
    assert rs.is_error and full in rs.document_text and len(str(rs)) < len(rs.document_text)
    assert rs.structured_json == '{"error": "details"}'


def test_oversized_native_image_is_not_retained_on_both(display):
    # The oracle shrinks MAX_MCP_IMAGE_BYTES; the Rust owner keeps the contract
    # constant, so the over-limit image is real: one byte past base64(8 MiB).
    from lib.mcp.result_content import MAX_MCP_IMAGE_BYTES
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    data = 'a' * (4 * math.ceil(MAX_MCP_IMAGE_BYTES / 3) + 4)
    for result in display.call('x', 'read', result={'content': [{'type': 'image', 'data': data, 'mimeType': 'image/png'}]}):
        assert 'data' not in result.image_contents[0]
        capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c')
        assert result.media_refs[0]['reason'] == 'image_byte_limit'


def test_embedded_resource_text_on_both(display):
    _, rs = display.call('x', 'read', case='embedded_resources')
    assert 'complete body' in rs.document_text
    assert rs.content_blocks[0]['text'] == 'complete body'
    assert rs.content_blocks[1]['reason'] == 'unsupported_resource_blob'
    assert 'SECRETBASE64' not in str(rs)


def test_owner_unavailable_from_both(display, monkeypatch):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: pytest.fail('no owner'))
    for result in display.call('x', 'read', result=_image_only()):
        capture_mcp_result_images({}, result, source_tool='mcp__x__r', tool_call_id='c')
        assert [ref for ref in result.media_refs if ref.get('reason')][0]['reason'] == 'owner_unavailable'


def test_exact_decoded_byte_limit_from_both(display, monkeypatch):
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    raw = _png()
    monkeypatch.setattr('lib.tasks_pkg.mcp_result_media.MAX_MCP_IMAGE_BYTES', len(raw))
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: {'attachmentId': 'exact'})
    for result in display.call('x', 'read', result={'content': [
            {'type': 'image', 'data': base64.b64encode(raw).decode(), 'mimeType': 'image/png'}]}):
        assert capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c') == 1
        assert result.media_refs[0]['byteSize'] == len(raw)


# ── test_mcp_image_frontend_projection.py ──────────────────────────────────

_RENDER = """
const {{ JSDOM }} = require('jsdom');
const dom = new JSDOM('<main><div id="block"></div></main>');
global.window = dom.window; global.document = dom.window.document; global.Element = dom.window.Element;
const feature = require({bundle});
const source = {{turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1, actor:'assistant',
  kind:'reply', runId:'', status:'completed', currentAttemptId:null, projectionRevision:1,
  settlement:{{outcome:'completed'}}, createdAt:1, updatedAt:1,
  projection:{{content:'Here is the image', images:{images}}}}};
const attachment = feature.selectTurnBlocks(source, 'original').find(block => block.kind === 'attachments');
const node = document.getElementById('block');
feature.createClassicConversationRenderers({{renderSafeMarkdownHtml: value => value}})
  .renderBlock(node, attachment, {{turn:{{source}}}});
const button = node.querySelector('[data-conversation-action="preview-image"]');
const image = node.querySelector('img');
console.log(JSON.stringify({{blockKind: attachment?.kind, imageCount: attachment?.images?.length,
  action: button?.dataset.conversationAction, operation: button?.dataset.operation,
  src: image?.getAttribute('src'), alt: image?.getAttribute('alt')}}));
"""


def test_projected_image_from_both_renders_as_interactive_thumbnail(display, monkeypatch, tmp_path):
    from tests._jsdom import require_node_modules
    projection, _ = _projections(display, monkeypatch)
    [image] = projection['images']
    assert {key: image[key] for key in IMAGE_DOC} == IMAGE_DOC
    require_node_modules('vite')
    require_node_modules('jsdom')
    bundle = tmp_path / 'conversation.cjs'
    built = subprocess.run([str(ROOT / 'scripts' / 'vite_test_bundle.mjs'), str(ROOT / 'frontend/src/conversation/index.ts'),
                            '--bundle', '--format=cjs', '--platform=node', f'--outfile={bundle}'],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    script = _RENDER.format(bundle=json.dumps(str(bundle)), images=json.dumps(projection['images']))
    rendered = subprocess.run([shutil.which('node'), '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert rendered.returncode == 0, rendered.stderr
    assert json.loads(rendered.stdout.strip().splitlines()[-1]) == {
        'blockKind': 'attachments', 'imageCount': 1, 'action': 'preview-image', 'operation': '0',
        'src': '/api/v4/media/attachments/image-doc/source', 'alt': 'Image from mcp__docs__read'}


# ── test_mcp_display_modifiers.py ──────────────────────────────────────────

POD = {'cluster': 'shxs_training_cluster', 'namespace': 'your-user', 'pod_name': 'psxa9idxrr4mwj8c-worker-0-0'}


def test_display_modifier_titles_from_rust_catalog_names(display):
    from lib.tasks_pkg.tool_display import compose_mcp_display

    cases = {'get_log_file': 'tail', 'list_log_files': 'files', 'experiment_update_params': 'ok',
             'experiment_api_serving': 'ok', 'edit_file': 'plain', 'experiment_get': 'resolved'}

    def title(server, tool, args):
        name = display.name(server, tool)
        display.call(server, tool, args, case=cases[tool])  # the arguments reach both servers unchanged
        assert display.received('rs', server)[-1]['arguments'] == display.received('py', server)[-1]['arguments']
        return compose_mcp_display(name, args)[0]

    log = lambda **extra: title('hope', 'get_log_file', {**POD, **extra})  # noqa: E731
    assert log(file_name='stdout').startswith('hope/get_log_file — stdout @ ')
    assert 'shxs_training_cluster/your-user' in log(file_name='stdout')
    assert 'stdout · tail · /SMOKE-RJH-001/' in log(file_name='stdout', method='tail', regex='SMOKE-RJH-001')
    variants = [{'file_name': 'stdout', 'regex': 'SMOKE-RJH-001', 'method': 'tail'},
                {'file_name': 'stdout', 'regex': 'AGENTIX_BRANCH|agentix@', 'method': 'tail'},
                {'file_name': 'stderr', 'regex': 'SMOKE-RJH-001', 'method': 'tail'},
                {'file_name': 'stdout', 'method': 'head'}]
    titles = [log(**variant) for variant in variants]
    assert len(set(titles)) == len(titles)
    assert title('llm', 'experiment_update_params', {'ids': '351488', 'node_id': '2017100', 'field': 'agent',
                                                     'dry_run': True}).count(' · ') == 2
    assert 'dry-run' in title('llm', 'experiment_api_serving', {'experiment_id': '351488', 'dry_run': True})
    assert 'dry-run' not in title('llm', 'experiment_api_serving', {'experiment_id': '351488', 'dry_run': False})
    assert '·' not in log(file_name='stdout', method='stdout')
    assert title('overleaf', 'edit_file', {'project_id': PID, 'file_path': 'acl_latex.tex'}) == \
        'overleaf/edit_file — acl_latex.tex @ 6a1e7…a668'
    assert title('llm', 'experiment_get', {'experiment_id_or_name': '351488'}) == 'llm/experiment_get — 351488'
    pod = title('hope', 'list_log_files', dict(POD))
    assert 'psxa9idx…er-0-0' in pod and 'psxa9idxrr4mwj8c-worker-0-0' not in pod
    assert '351488, 351489' in title('llm', 'experiment_update_params', {'ids': '351488,351489'})
    assert compose_mcp_display(display.name('hope', 'get_log_file'), None)[0] == 'hope/get_log_file'


def _post_build(bridge, fn_name, fn_args, tool_content, round_entry=None, progressive=False):
    """The real handler ``_post_build`` closure, with ``bridge`` as the owner's bridge."""
    import lib.mcp as mcp_pkg
    import lib.tasks_pkg.handlers.mcp as mcp_handler
    captured = {}

    def fake_simple_call(task, fn, args, rn, entry, tc_id, *, executor, source, module_tag, extra,
                         post_build=None, **_kw):
        captured['post_build'] = post_build
        return tc_id, 'ok', False

    entry = round_entry if round_entry is not None else {}
    original_bridge, original_call = mcp_pkg.get_bridge, mcp_handler.simple_call
    mcp_pkg.get_bridge = lambda owner: bridge
    mcp_handler.simple_call = fake_simple_call
    try:
        handler = mcp_handler.handle_progressive_mcp_tool if progressive else mcp_handler.handle_mcp_tool
        handler({'_userId': 7}, {}, fn_name, 't', fn_args, 1, entry, {}, None, False)
        meta = {}
        captured['post_build'](meta, tool_content, fn_args)
        return meta, entry
    finally:
        mcp_pkg.get_bridge, mcp_handler.simple_call = original_bridge, original_call


def _post_build_both(display, server, tool, args, *, case, round_entry=None, progressive=False, cache=None):
    """Call ``server/tool`` on both clients, then settle each result through the real handler."""
    py, rs = display.call(server, tool, case=case)
    name = display.name(server, tool)
    settled = []
    for bridge, result in ((display.py, py), (display.rs, rs)):
        if cache is not None:
            cache.clear_cache()
        fn_name, fn_args = ('call_mcp_read_tool', {'name': name, 'arguments': args}) if progressive else (name, args)
        settled.append(_post_build(bridge, fn_name, fn_args, str(result), json.loads(json.dumps(round_entry)) if
                                   round_entry is not None else None, progressive))
    assert settled[1] == settled[0]
    return settled[1]


def test_post_build_title_after_a_rust_call(display, names_cache):
    meta, _ = _post_build_both(display, 'github-batch', 'batch_commit', {
        'owner': 'rangehow', 'repo': 'ToFu', 'branch': 'main', 'message': 'm',
        'files': [{'path': 'lib/x.py', 'content': '...'}, {'path': 'static/y.js', 'content': '...'}],
        'delete_paths': ['old/z.py']}, case='committed')
    for part in ('lib/x.py', 'static/y.js', '− old/z.py', '3 files'):
        assert part in meta['title']
    assert meta['title'] != 'github-batch/batch_commit — main @ rangehow/ToFu'
    meta, _ = _post_build_both(display, 'github', 'create_issue',
                               {'owner': 'a', 'repo': 'b', 'title': 'Bug', 'issue_number': 5}, case='created')
    assert meta['title'] == 'github/create_issue — Bug @ a/b' and not meta.get('badge')


# ── test_mcp_display_owner_cache.py ────────────────────────────────────────

def _ingest_both(display, server, tool, **call):
    """Both clients' result texts for one call (Python's first), as the handler ingests them."""
    return [str(result) for result in display.call(server, tool, **call)]


def test_cached_project_name_stays_with_its_owner_from_both(display, names_cache):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    name = display.name('overleaf', 'list_files')
    queries = []
    for content in _ingest_both(display, 'overleaf', 'list_projects', case='owner_a'):
        names_cache.clear_cache()
        names_cache.ingest_tool_result(display.name('overleaf', 'list_projects'), {}, content, user_id=1)
        round_for = lambda owner: _build_tool_round_entry(name, {'project_id': PID}, 'call', '{}', 0, False,  # noqa: E731
                                                          task={'_userId': owner})[1]['query']
        queries.append((round_for(2), round_for(1)))
        assert 'Private owner A project' not in queries[-1][0] and 'Private owner A project' in queries[-1][1]
    assert queries[1] == queries[0]


@requires_yourprovider_overlay
def test_private_doc_urls_stay_with_owner_from_both(display, names_cache):
    reads = []
    for content in _ingest_both(display, 'xuecheng', 'read_doc', case='private_doc'):
        names_cache.clear_cache()
        names_cache.ingest_tool_result(display.name('xuecheng', 'read_doc'), {'doc': '1234567'}, content, user_id=1)
        assert names_cache.get_doc_title('1234567', user_id=1) == 'Private document'
        assert names_cache.get_doc_url('1234567', user_id=1) == 'https://private.example/page/1234567'
        assert names_cache.get_doc_url('7654321', user_id=1).startswith('https://private.example/')
        for owner in (None, 2):
            assert names_cache.get_doc_title('1234567', user_id=owner) == ''
            assert names_cache.get_doc_url('1234567', user_id=owner).startswith('https://km.internal.example.com/')
        reads.append(names_cache.get_doc_url('7654321', user_id=1))
    assert reads[1] == reads[0]


def test_ingestion_requires_an_explicit_owner_for_both(display, names_cache):
    for content in _ingest_both(display, 'overleaf', 'list_projects', case='owner_a'):
        for owner in (None, 0, True, -1):
            with pytest.raises(ValueError):
                names_cache.ingest_tool_result(display.name('overleaf', 'list_projects'), {}, content, user_id=owner)


def test_structured_traversal_is_bounded_from_both(display, names_cache):
    beyond = [{'irrelevant': i} for i in range(300)] + [{'id': PID, 'name': 'Beyond traversal budget'}]
    for content in _ingest_both(display, 'overleaf', 'list_projects', result=wire(json.dumps(beyond))):
        names_cache.clear_cache()
        names_cache.ingest_tool_result(display.name('overleaf', 'list_projects'), {}, content, user_id=1)
        assert names_cache.get_project_name(PID, user_id=1) == ''
    for content in _ingest_both(display, 'xuecheng', 'read_doc',
                                result=wire({'id': PID, 'name': 'Wrong namespace'})):
        names_cache.ingest_tool_result(display.name('xuecheng', 'read_doc'), {}, content, user_id=1)
        assert names_cache.get_project_name(PID, user_id=1) == ''


@pytest.mark.parametrize('base', ['https://user:secret@private.example', 'https://private.example:99999',
                                  'https://private.example"onclick="bad'])
def test_unsafe_learned_url_is_not_rendered_from_both(display, names_cache, base):
    for content in _ingest_both(display, 'overleaf', 'create_project',
                                result=wire(f'{base}/project/{PID}')):
        names_cache.clear_cache()
        names_cache.ingest_tool_result(display.name('overleaf', 'create_project'), {}, content, user_id=1)
        assert names_cache.get_project_url(PID, user_id=1) == f'https://www.overleaf.com/project/{PID}'


# ── test_mcp_tool_links.py ─────────────────────────────────────────────────

def test_overleaf_links_from_rust_catalog_names(display, names_cache):
    from lib.tasks_pkg.tool_display import _tool_display_mcp
    assert names_cache.get_project_url(PID, user_id=7) == f'https://www.overleaf.com/project/{PID}'
    assert names_cache.get_project_url('not-a-real-id', user_id=7) == ''
    assert names_cache.get_project_url('', user_id=7) == ''
    disp, extra = _tool_display_mcp(display.name('overleaf', 'edit_file'),
                                    {'project_id': PID, 'file_path': 'acl_latex.tex'}, 't', '{}', user_id=7)
    links = extra.get('_mcpLinks')
    assert links and '@ ' + list(links)[0] in disp
    assert list(links.values())[0] == f'https://www.overleaf.com/project/{PID}'


def _learn_both(display, cache, server, tool, args, case, read):
    """Ingest each client's ``case`` result into a fresh cache and return ``read()`` for both."""
    reads = []
    for content in [str(result) for result in display.call(server, tool, case=case)]:
        cache.clear_cache()
        cache.ingest_tool_result(display.name(server, tool), args, content, user_id=7)
        reads.append(read())
    assert reads[1] == reads[0]
    return reads[1]


def test_overleaf_learned_names_and_bases_from_both(display, names_cache):
    from lib.tasks_pkg.tool_display import _tool_display_mcp
    edit = display.name('overleaf', 'edit_file')

    def label():
        disp, extra = _tool_display_mcp(edit, {'project_id': PID, 'file_path': 'acl_latex.tex'}, 't', '{}', user_id=7)
        return disp, extra['_mcpLinks']
    disp, links = _learn_both(display, names_cache, 'overleaf', 'create_project', {'name': 'My Paper'}, 'my_paper', label)
    assert list(links)[0] in disp
    pid = 'aaaaaaaaaaaaaaaaaaaaaaaa'
    assert _learn_both(display, names_cache, 'overleaf', 'edit_file', {'project_id': pid, 'file_path': 'x.tex'},
                       'self_hosted', lambda: names_cache.get_project_url(pid, user_id=7)) == \
        f'https://overleaf.mycorp.com/project/{pid}'
    self_hosted = f'https://overleaf.corp.example.com/project/{PID}'
    learned = _learn_both(display, names_cache, 'overleaf', 'edit_file', {'project_id': PID, 'file_path': 'acl_latex.tex'},
                          'corp_tag', lambda: (names_cache.get_project_url(PID, user_id=7),
                                               names_cache.get_project_url('b' * 24, user_id=7), label()[1]))
    assert learned == (self_hosted, f'https://overleaf.corp.example.com/project/{"b" * 24}',
                       {list(learned[2])[0]: self_hosted})


def test_xuecheng_links_from_both(display, names_cache):
    from lib.tasks_pkg.tool_display import _tool_display_mcp
    read_doc = display.name('xuecheng', 'read_doc')
    assert _learn_both(display, names_cache, 'xuecheng', 'read_doc', {'doc': '2761323464'}, 'harvest_url',
                       lambda: (names_cache.get_doc_url('2761323464', user_id=7),
                                names_cache.get_doc_url('1111111111', user_id=7))) == (
        'https://km.corp.example.com/page/2761323464', 'https://km.corp.example.com/collabpage/1111111111')

    def after():
        disp, extra = _tool_display_mcp(read_doc, {'doc': '2761323464'}, 't', '{}', user_id=7)
        return names_cache.get_doc_url('2761323464', user_id=7), disp, extra['_mcpLinks']
    url, disp, links = _learn_both(display, names_cache, 'xuecheng', 'read_doc',
                                   {'doc': 'https://km.internal.example.com/collabpage/2761323464'}, 'after_harvest', after)
    assert url == links.get('My Doc') == 'https://km.internal.example.com/collabpage/2761323464' and 'My Doc' in disp


@requires_yourprovider_overlay
def test_xuecheng_default_base_from_rust_catalog_name(display, names_cache):
    from lib.tasks_pkg.tool_display import _tool_display_mcp
    assert names_cache.get_doc_url('2761323464', user_id=7) == 'https://km.internal.example.com/collabpage/2761323464'
    assert names_cache.get_doc_url('not-a-doc-id', user_id=7) == ''
    _disp, extra = _tool_display_mcp(display.name('xuecheng', 'read_doc'), {'doc': '2761323464'}, 't', '{}', user_id=7)
    assert extra.get('_mcpLinks') == {'2761323464': 'https://km.internal.example.com/collabpage/2761323464'}


def test_non_linkable_and_batch_path_titles_from_rust_catalog_names(display, names_cache):
    from lib.tasks_pkg.tool_display import _tool_display_mcp
    _disp, extra = _tool_display_mcp(display.name('github', 'create_issue'),
                                     {'owner': 'torvalds', 'repo': 'linux', 'title': 'bug'}, 't', '{}', user_id=7)
    assert not extra.get('_mcpLinks')
    args = {'owner': 'rangehow', 'repo': 'ToFu', 'branch': 'main', 'message': 'x',
            'files': [{'path': 'lib/tasks_pkg/tool_display.py', 'content': '...'},
                      {'path': 'static/js/ui/tool_rounds.js', 'content': '...'}],
            'delete_paths': ['old/legacy.py']}
    disp, extra = _tool_display_mcp(display.name('github-batch', 'batch_commit'), args, 't', '{}', user_id=7)
    for part in ('lib/tasks_pkg/tool_display.py', 'static/js/ui/tool_rounds.js', '− old/legacy.py', '3 files',
                 '@ rangehow/ToFu'):
        assert part in disp
    assert extra.get('_display_query') == disp and 'main @ rangehow/ToFu' not in disp.split('\n')[0]
    disp, _ = _tool_display_mcp(display.name('github-batch', 'batch_delete'),
                                {'owner': 'a', 'repo': 'b', 'branch': 'main', 'message': 'm', 'paths': ['x.py', 'y.py']},
                                't', '{}', user_id=7)
    assert '− x.py' in disp and '− y.py' in disp and '@ a/b' in disp
    disp, _ = _tool_display_mcp(display.name('github', 'push_files'), {
        'owner': 'a', 'repo': 'b', 'branch': 'main', 'message': 'm',
        'files': [{'path': 'README.md', 'content': '# hi'}]}, 't', '{}', user_id=7)
    assert 'README.md' in disp and '1 file' in disp
    disp, _ = _tool_display_mcp(display.name('github', 'create_issue'),
                                {'owner': 'a', 'repo': 'b', 'title': 'Bug', 'issue_number': 5}, 't', '{}', user_id=7)
    assert '\n' not in disp and 'Bug' in disp


def test_settle_upgrades_from_both(display, names_cache):
    meta, entry = _post_build_both(display, 'xuecheng', 'login', {}, case='approval_timeout', cache=names_cache)
    assert entry.get('status') == 'error' and not meta.get('badge')
    meta, entry = _post_build_both(display, 'xuecheng', 'read_doc', {'doc': '2784112217'}, case='my_doc_short',
                                   progressive=True, cache=names_cache)
    assert not meta.get('badge')
    assert meta['title'] == entry.get('query') == 'xuecheng/read_doc — My Doc'


@requires_yourprovider_overlay
def test_read_doc_settle_swaps_id_for_title_from_both(display, names_cache):
    cid = '2784112217'
    url = f'https://km.internal.example.com/collabpage/{cid}'
    meta, entry = _post_build_both(display, 'xuecheng', 'read_doc', {'doc': cid}, case='my_doc', cache=names_cache,
                                   round_entry={'query': f'xuecheng/read_doc — {cid}', '_mcpLinks': {cid: url}})
    assert entry['query'] == meta['title'] == 'xuecheng/read_doc — My Doc'
    assert entry['_mcpLinks'] == {'My Doc': url} and not meta.get('badge') and entry.get('status') != 'error'


# ── test_llm_canvas_link_policy.py / test_llm_canvas_links.py ─────────────

POLICY = json.loads((ROOT / 'contracts' / 'llm_canvas_links_v1.json').read_text(encoding='utf-8'))


def test_canvas_policy_emission_from_rust_catalog_names(display):
    from urllib.parse import urlsplit
    from lib.tasks_pkg.tool_display._mcp import _mcp_links
    origin = urlsplit(POLICY['origin'])
    assert (origin.scheme, origin.netloc, origin.path) == ('https', 'model.internal.example.com', '')
    assert POLICY['limits'] == dict(serializedChars=65536, referenceChars=512, labelChars=40, inputLinks=2)
    tool = display.name(POLICY['server'], POLICY['toolPrefix'] + 'get')
    url = POLICY['origin'] + POLICY['canvasPathPrefix'] + '381153'
    for key in POLICY['inputKeys']:
        assert _mcp_links({key: '381153'}, fn_name=tool) == {'381153': url}
    assert not _mcp_links({'experiment_id': '381153'}, fn_name=display.name('other', 'experiment_get'))
    base = POLICY['origin'] + POLICY['canvasPathPrefix']
    assert _mcp_links({'experiment_or_url': base + '381153/991/0'}, fn_name=tool) == {'381153': base + '381153'}


def _settle(args, content, tool):
    from lib.tasks_pkg.handlers.mcp import _settle_mcp_round_display
    from lib.tasks_pkg.tool_display._mcp import compose_mcp_display
    display, _ = compose_mcp_display(tool, args, user_id=917)
    row, meta = {'query': display}, {}
    _settle_mcp_round_display(row, meta, tool, args, content, user_id=917)
    assert row['query'] == meta['title']
    for label in row.get('_mcpLinks', {}):
        assert label in row['query']
    return row


def settle(display, args, tool='experiment_get', *, case=None, result=None):
    """Call the llm tool on both clients; settle each result's text; return the Rust-fed row."""
    name = display.name('llm', tool)
    rows = [_settle(args, str(value), name)
            for value in display.call('llm', tool, case=case, result=None if result is None else wire(result))]
    assert rows[1] == rows[0]
    return rows[1]


@pytest.mark.parametrize('key', ['experiment_id_or_name_or_url', 'experiment_or_url', 'experiment_id_or_name',
                                 'experiment_id'])
def test_numeric_start_and_failure_keep_run_links_from_both(display, key):
    from lib.tasks_pkg.tool_display._renderers import _tool_display_mcp
    args = {key: '381153'}
    shown, extra = _tool_display_mcp(display.name('llm', 'experiment_get'), args, '', '', user_id=917)
    assert '381153' in shown and extra['_mcpLinks'] == {'381153': BASE + '381153'}
    for case in ('not_run_error', 'not_run_state'):
        assert settle(display, args, case=case)['_mcpLinks'] == extra['_mcpLinks']


def test_name_only_resolves_from_successful_experiment_id_from_both(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links
    args = {'experiment_id_or_name_or_url': 'canvas name'}
    assert not _mcp_links(args, fn_name=display.name('llm', 'experiment_get'))
    assert settle(display, args, case='resolved')['_mcpLinks'] == {'canvas name': BASE + '381154'}
    for case in ('failed_with_id', 'id_task_id', 'nested_payload', 'bool_id'):
        assert '_mcpLinks' not in settle(display, args, case=case)


@pytest.mark.parametrize('tool', ['experiment_fork', 'experiment_data_fork'])
def test_fork_new_canvas_is_not_source_from_both(display, tool):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links, compose_mcp_display
    name = display.name('llm', tool)
    args = {'base_id': '381153', 'name': 'new canvas'}
    shown, _ = compose_mcp_display(name, args)
    links = _mcp_links(args, fn_name=name)
    assert links == {'source: 381153': BASE + '381153'} and all(label in shown for label in links)
    assert settle(display, args, tool, case='forked')['_mcpLinks'] == {
        'source: 381153': BASE + '381153', 'new: new canvas': BASE + '381154'}
    args['base_id'] = 'source name'
    assert 'source: source name' not in settle(display, args, tool, case='resolved')['_mcpLinks']
    assert '_mcpLinks' not in settle(display, args, tool, case='denied')


@pytest.mark.parametrize('url', ['javascript:alert(1)', BASE + '381154" onclick="bad',
                                 BASE.replace('model.internal.example.com', 'evil.test') + '381154',
                                 BASE + '381154?x=bad', BASE + '381155', 'https://model.internal.example.com@evil.test/x'])
def test_untrusted_result_urls_do_not_link_names_from_both(display, url):
    row = settle(display, {'experiment_id': '<b>name</b>'},
                 result={'ok': True, 'data': {'experimentId': 381154, 'canvasUrl': url}})
    assert '_mcpLinks' not in row


def test_run_url_and_other_servers_from_rust_catalog_names(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links, compose_mcp_display
    tool = display.name('llm', 'experiment_get')
    args = {'experiment_or_url': BASE + '381153/991/0'}
    shown, _ = compose_mcp_display(tool, args)
    links = _mcp_links(args, fn_name=tool)
    assert links == {'381153': BASE + '381153'} and all(label in shown for label in links)
    assert not _mcp_links({'experiment_id': 381153}, fn_name=display.name('other', 'experiment_get'))
    assert not _mcp_links({'experiment_id': 381153}, fn_name=display.name('llm', 'eval_get'))


def test_progressive_start_uses_underlying_rust_catalog_name(display):
    from lib.tasks_pkg.tool_display._renderers import _tool_display_mcp
    shown, extra = _tool_display_mcp('call_mcp_read_tool', {
        'name': display.name('llm', 'experiment_get'), 'arguments': {'experiment_id': 381154}}, '', '', user_id=917)
    assert '381154' in shown and extra['_mcpLinks'] == {'381154': BASE + '381154'}


def test_capped_untrusted_label_from_both(display):
    row = settle(display, {'experiment_id': '<script>&"' + 'x' * 60}, case='resolved')
    label, href = next(iter(row['_mcpLinks'].items()))
    assert len(label) == 40 and label.endswith('…') and label in row['query'] and href == BASE + '381154'


def test_batch_ids_and_dry_run_source_only_from_both(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links, compose_mcp_display
    tool = display.name('llm', 'experiment_run')
    args = {'ids': '381153,381154,381155'}
    display.call('llm', 'experiment_run', args, case='started')
    shown, _ = compose_mcp_display(tool, args)
    assert _mcp_links(args, fn_name=tool) == {'381153': BASE + '381153', '381154': BASE + '381154'}
    assert '+1 more' in shown
    row = settle(display, {'base_id': '381153', 'name': 'copy', 'dry_run': True}, 'experiment_fork', case='dry_run')
    assert row['_mcpLinks'] == {'source: 381153': BASE + '381153'}


def test_settle_removes_stale_name_link_after_rust_failure(display):
    from lib.tasks_pkg.handlers.mcp import _settle_mcp_round_display
    tool = display.name('llm', 'experiment_get')
    rows = []
    for value in display.call('llm', 'experiment_get', case='denied'):
        row = {'query': '', '_mcpLinks': {'unresolved': BASE + '381153'}}
        _settle_mcp_round_display(row, {}, tool, {'experiment_id': 'unresolved'}, str(value), user_id=917)
        rows.append(row)
    assert rows[1] == rows[0] and '_mcpLinks' not in rows[1]


WRAPPED_DATA = {'experimentId': 381491, 'experimentName': 'Fork canvas', 'page': BASE + '381491'}
WRAPPED_ITEM = {'ok': True, 'data': WRAPPED_DATA}
WRAPPED_FORK = {'items': [WRAPPED_ITEM], 'summary': 'Tool returned a structured result.'}


def test_historical_single_item_fork_wrapper_from_both(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links
    tool = display.name('llm', 'experiment_fork')
    args = {'base_id': 379851, 'dry_run': False}
    expected = {'source: 379851': BASE + '379851', 'new: Fork canvas': BASE + '381491'}
    for value in display.call('llm', 'experiment_fork', case='wrapped'):
        assert _mcp_links(args, fn_name=tool, tool_content=str(value)) == expected
    assert settle(display, args, 'experiment_fork', case='wrapped')['_mcpLinks'] == expected
    assert settle(display, {**args, 'dry_run': True}, 'experiment_fork', case='wrapped')['_mcpLinks'] == {
        'source: 379851': BASE + '379851'}


def hostile_wrappers():
    yield {**WRAPPED_FORK, 'items': []}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_ITEM, WRAPPED_ITEM]}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_ITEM, {'ok': False}]}
    yield {**WRAPPED_FORK, 'summary': 'Tool returned 1 items.'}
    yield {'items': [WRAPPED_ITEM]}
    yield {**WRAPPED_FORK, 'items': {'0': WRAPPED_ITEM, 'length': 1}}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_FORK]}
    yield {**WRAPPED_FORK, 'items': [json.dumps(WRAPPED_ITEM)]}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_DATA]}
    yield {**WRAPPED_FORK, 'items': [{'ok': True, 'data': {**WRAPPED_DATA, 'page': BASE + '381492'}}]}
    for extra in ({'ok': False}, {'success': False}, {'error': 'failed'}, {'isError': True},
                  {'status': 'cancelled'}, {'status': 'partial'}, {'truncated': True}, {'dry_run': True}):
        yield {**WRAPPED_FORK, **extra}
        yield {**WRAPPED_FORK, 'items': [{**WRAPPED_ITEM, **extra}]}
        yield {**WRAPPED_FORK, 'items': [{'ok': True, 'data': {**WRAPPED_DATA, **extra}}]}
    yield {**WRAPPED_FORK, 'artifactRef': 'tool-result:partial'}
    yield json.dumps(WRAPPED_FORK)[:-1]
    yield json.dumps(WRAPPED_FORK) + ' truncated'
    yield ' ' * 65537 + json.dumps(WRAPPED_FORK)


@pytest.mark.parametrize('wrapper', list(hostile_wrappers()))
def test_hostile_wrapper_keeps_only_source_from_both(display, wrapper):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links
    tool = display.name('llm', 'experiment_fork')
    for value in display.call('llm', 'experiment_fork', result=wire(wrapper)):
        assert _mcp_links({'base_id': 379851}, fn_name=tool, tool_content=str(value)) == {
            'source: 379851': BASE + '379851'}


ORIGINAL = json.loads((ROOT / 'tests' / 'fixtures' / 'llm_canvas_original_fork.json').read_text())


def test_original_retained_source_requires_owning_evidence_from_both(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links, compose_mcp_display
    tool, args, evidence = display.name('llm', 'experiment_fork'), ORIGINAL['toolArgs'], ORIGINAL['toolResultEvidence']
    assert tool == ORIGINAL['toolName']
    source = {'source: 379851': BASE + '379851'}
    for value in display.call('llm', 'experiment_fork', case='original'):
        content = str(value)
        links = _mcp_links(args, fn_name=tool, tool_content=content, tool_result_evidence=evidence)
        assert list(links.values()) == [BASE + '379851', BASE + '381491']
        title, _ = compose_mcp_display(tool, args, tool_content=content, tool_result_evidence=evidence)
        assert all(label in title for label in links)
        assert _mcp_links(args, fn_name=tool, tool_content=content) == source
        hostile = [None, {}, *[{**evidence, key: None} for key in (
            'contractVersion', 'resultContractVersion', 'status', 'executionStatus',
            'projectionKind', 'retentionCoverage', 'viewCoverage', 'truncated')]]
        hostile += [{**evidence, **extra} for extra in (
            {'status': 'error'}, {'status': 'failed'}, {'status': 'cancelled'}, {'status': 'partial'},
            {'executionStatus': 'failed'}, {'retentionCoverage': 'unknown'}, {'retentionCoverage': 'partial'},
            {'viewCoverage': 'unknown'}, {'viewCoverage': 'partial'}, {'sourceCoverage': 'partial'},
            {'truncated': True}, {'truncated': 0}, {'artifactRef': ''}, {'resultId': ''}, {'evidenceId': ''},
            {'isError': True}, {'error': 'bad'}, {'success': False}, {'ok': False}, {'dry_run': True})]
        for witness in hostile:
            assert _mcp_links(args, fn_name=tool, tool_content=content, tool_result_evidence=witness) == source
        assert _mcp_links({**args, 'dry_run': True}, fn_name=tool, tool_content=content,
                          tool_result_evidence=evidence) == source


def test_producer_envelope_cannot_manufacture_sidecar_from_both(display):
    from lib.tasks_pkg.tool_display._mcp import _mcp_links
    from lib.tools.result_envelope import split_tool_result_delivery
    evidence = ORIGINAL['toolResultEvidence']
    envelope = {**ORIGINAL['toolContent'], **evidence, 'contractVersion': evidence['resultContractVersion']}
    for value in display.call('llm', 'experiment_fork', result=wire(json.dumps(envelope))):
        assert split_tool_result_delivery(str(value)).evidence is None
        assert _mcp_links(ORIGINAL['toolArgs'], fn_name=ORIGINAL['toolName'], tool_content=str(value)) == {
            'source: 379851': BASE + '379851'}
