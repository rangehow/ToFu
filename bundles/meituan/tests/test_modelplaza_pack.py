"""ModelPlaza site pack behavior (bundles/meituan/site_packs/modelplaza.json).

Pins the pack-driven pipeline against the exact BrowserPage call sequence the
legacy built-in adapter made, including the best-effort form-fill group and
the compareModels json_payload acceptance tail.
"""

from types import SimpleNamespace

import pytest

from lib.site_packs import load_packs, reset_cache_for_tests
from lib.site_packs._interpreter import operation_capabilities, operation_handler

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_loader_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


@pytest.fixture()
def pack():
    return load_packs()['modelplaza']


def _operation(pack, name):
    return next(op for op in pack.operations if op.name == name)


def _page(url='https://aigc.sankuai.com/ml/modelPlaza/modelInfo', execute_result=None):
    calls = []

    class FakePage:
        current_url = url
        lease = SimpleNamespace(owner_user_id='alice')

        def new_tab(self, u, **kw):
            calls.append(('new_tab', u))

        def fill(self, value, *, selector='', trusted_read=False, **kw):
            calls.append(('fill', value, selector, trusted_read))
            return {}

        def press(self, keys, *, selector='', trusted_read=False, **kw):
            calls.append(('press', keys, selector, trusted_read))
            return {}

        def wait(self, *, selector='', timeout=10, **kw):
            calls.append(('wait', selector, timeout))
            return {}

        def execute(self, expression, *, args=None, trusted_read=False, **kw):
            calls.append(('execute', args, trusted_read))
            return {'result': {'value': execute_result}}

    return FakePage(), calls


def test_search_pipeline_navigates_fills_and_extracts(pack, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_search_records',
        lambda page, query, limit, source_id, source_name, **kw: captured.update(
            query=query, limit=limit, source_id=source_id,
            source_name=source_name, candidates=kw.get('candidates')) or [])
    page, calls = _page(execute_result=[{'title': 'Claude', 'url': '/m/1'}])
    handler = operation_handler(pack, _operation(pack, 'search'))
    handler(page, {'query': 'Claude', 'limit': 10})

    assert calls[0] == ('new_tab',
                        'https://aigc.sankuai.com/ml/modelPlaza/modelInfo')
    fills = [c for c in calls if c[0] == 'fill']
    assert fills == [(
        'fill', 'Claude',
        'input[type="search"],input[placeholder*="搜索"],input[placeholder*="模型"]',
        True)]
    assert ('press', 'Enter',
            'input[type="search"],input[placeholder*="搜索"],input[placeholder*="模型"]',
            True) in calls
    assert ('wait', 'a[href]', 8) in calls
    assert ('execute', {'query': 'Claude'}, True) in calls
    assert captured['candidates'] == [{'title': 'Claude', 'url': '/m/1'}]
    assert captured['source_name'] == 'ModelPlaza'
    assert captured['limit'] == 10


def test_search_form_fill_failure_degrades_to_rendered_list(pack, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        'lib.site_packs._interpreter.read_search_records',
        lambda page, query, limit, source_id, source_name, **kw: captured.update(
            candidates=kw.get('candidates')) or [])

    page, calls = _page(execute_result=[{'title': 'x', 'url': '/m/2'}])
    page.fill = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('no input'))
    handler = operation_handler(pack, _operation(pack, 'search'))
    handler(page, {'query': 'kimi'})  # must not raise
    assert not [c for c in calls if c[0] == 'press']
    assert captured['candidates'] == [{'title': 'x', 'url': '/m/2'}]


def test_model_info_json_payload_drops_secrets_and_bounds_output(pack):
    page = SimpleNamespace(
        current_url='https://aigc.sankuai.com/',
        new_tab=lambda *a: None,
        execute=lambda *a, **k: {'result': {'ok': True, 'data': [
            {'name': 'Claude', 'accessToken': 'secret', 'price': 0}]}})
    handler = operation_handler(pack, _operation(pack, 'model-info'))
    result = handler(page, {'names': ['Claude']})
    import json
    assert 'secret' not in json.dumps(result)
    assert result['data'][0]['price'] == 0
    assert result['coverage']['scope'] == 'requested_model_attributes'


def test_model_info_http_failure_is_typed_and_retryable(pack):
    from lib.browser.adapter_errors import AdapterExecutionError
    page = SimpleNamespace(
        current_url='https://aigc.sankuai.com/',
        new_tab=lambda *a: None,
        execute=lambda *a, **k: {'result': {'ok': False, 'status': 502}})
    handler = operation_handler(pack, _operation(pack, 'model-info'))
    with pytest.raises(AdapterExecutionError) as exc:
        handler(page, {'names': ['Claude']})
    assert exc.value.code == 'fetch_failed'
    assert exc.value.retryable is True


def test_operation_capabilities_match_legacy_manifest(pack):
    assert sorted(operation_capabilities(_operation(pack, 'search'))) == sorted(
        ('tabs', 'execute', 'snapshot', 'read', 'fill', 'press', 'wait'))
    assert sorted(operation_capabilities(_operation(pack, 'detail'))) == sorted(
        ('tabs', 'read', 'snapshot'))
    assert sorted(operation_capabilities(
        _operation(pack, 'model-info'))) == sorted(('tabs', 'execute'))


def test_auth_catalog_projects_the_sso_entry(pack):
    from lib.site_packs import pack_auth_catalog
    rows = {row['domain']: row for row in pack_auth_catalog()}
    entry = rows['sankuai.com']
    assert entry['label'] == 'Meituan internal (SSO)'
    assert entry['access_strategy'] == 'browser_first'
    assert entry['login_url'].endswith('/ml/modelPlaza/modelInfo')
    assert [f['name'] for f in entry['fields']] == [
        'ssoid', 'ssoid_sankuai', 'ssousername', 'ssosession']
