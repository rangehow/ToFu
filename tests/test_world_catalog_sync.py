"""tests/test_world_catalog_sync.py — 模型配置即世界模型注册表.

Pins the 2026-09-06 ruling: the Model catalog tab lists models that EXIST,
not models a configured provider supplies. gpt-6-astra / claude-fable-5-1 /
gemini-3.8-flash were in the package templates and pricing/slot specs but
absent from the owner authority registry (no provider bundle ever carried
them). The startup sync compiles every package template and inserts the
missing Creator/Model rows — insert-only and idempotent.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_world_catalog_sync.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.model_catalog._creator_identity import merge_keys
from lib.model_routing import OwnerBoundary, empty_document
from lib.model_routing.repository import InMemoryModelRoutingRepository
from lib.model_routing.world_catalog import sync_world_model_catalog

pytestmark = pytest.mark.unit

_OWNER = 987654321


def _boundary() -> OwnerBoundary:
    return OwnerBoundary.create(_OWNER)


def _has_model(document: dict, model_id: str) -> bool:
    wanted = merge_keys(model_id)
    return any(
        merge_keys(row.get('model_id'), row.get('display_name')) & wanted
        for row in document['models'])


def test_newly_shipped_models_land_in_empty_authority():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    summary = sync_world_model_catalog(boundary=boundary,
                                       repository=repository)
    assert summary['added'] > 0

    document = repository.get(boundary).document
    assert _has_model(document, 'gpt-6-astra')
    assert _has_model(document, 'gemini-3.8-flash')
    assert _has_model(document, 'claude-fable-5-1')
    assert _has_model(document, 'muse-spark-1.3')
    creators = {row['creator_id'] for row in document['creators']}
    assert {'openai', 'anthropic', 'google', 'meta'} <= creators
    # Registry-only sync: provider supply collections stay untouched.
    assert document['providers'] == []
    assert document['offerings'] == []
    assert document['credentials'] == []


def test_hunyuan_models_land_in_registry_under_tencent_creator():
    # The creator-family taxonomy attributes hy*/hunyuan-* ids to tencent,
    # so the TokenHub/meituan recipes compile official rows the sync can
    # insert — before the family existed they stayed pending_identity and
    # never appeared in the 模型配置 tab.
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    sync_world_model_catalog(boundary=boundary, repository=repository)
    document = repository.get(boundary).document
    assert _has_model(document, 'hy4-preview')
    assert _has_model(document, 'hy3-preview')
    tencent_rows = [row for row in document['models']
                    if row['creator_id'] == 'tencent']
    assert tencent_rows
    assert all('text' in row['capabilities'] for row in tencent_rows)
    creators = {row['creator_id'] for row in document['creators']}
    assert 'tencent' in creators


def test_sync_is_insert_only_and_preserves_owner_edits():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    document = empty_document(revision=0)
    document['creators'].append({'creator_id': 'openai', 'name': 'OpenAI'})
    document['models'].append({
        'creator_id': 'openai',
        'model_id': 'gpt-6-astra',
        'display_name': 'My Astra',
        'capabilities': ['text'],
        'context_window': 1000,
        'quality_rank': 1,
    })
    repository.compare_and_swap(boundary, document, expected_revision=0)

    sync_world_model_catalog(boundary=boundary, repository=repository)

    rows = [row for row in repository.get(boundary).document['models']
            if row['model_id'] == 'gpt-6-astra']
    assert len(rows) == 1
    assert rows[0]['display_name'] == 'My Astra'
    assert rows[0]['context_window'] == 1000


def test_retired_template_models_are_pruned_from_registry():
    """Authored-retired rows stay dead: the feed never re-adds them and the
    sync prunes identities the OpenRouter feed had already inserted."""
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    document = empty_document(revision=0)
    document['creators'].append({'creator_id': 'openai', 'name': 'OpenAI'})
    # Placeholder rows the feed imported before the template retired them.
    for rank, model_id in enumerate(('gpt-6-astra-pro', 'gpt-5.6-sol-pro')):
        document['models'].append({
            'creator_id': 'openai',
            'model_id': model_id,
            'display_name': model_id,
            'capabilities': ['text'],
            'context_window': 1050000,
            'quality_rank': 50 + rank,
        })
    # An owner-customized live model shares the family prefix but must survive.
    document['models'].append({
        'creator_id': 'openai',
        'model_id': 'gpt-6-astra',
        'display_name': 'My Astra',
        'capabilities': ['text'],
        'context_window': 1000,
        'quality_rank': 1,
    })
    repository.compare_and_swap(boundary, document, expected_revision=0)

    summary = sync_world_model_catalog(boundary=boundary, repository=repository)

    assert summary['pruned'] == 2
    document = repository.get(boundary).document
    assert not _has_model(document, 'gpt-6-astra-pro')
    assert not _has_model(document, 'gpt-5.6-sol-pro')
    rows = [row for row in document['models']
            if row['model_id'] == 'gpt-6-astra']
    assert len(rows) == 1
    assert rows[0]['display_name'] == 'My Astra'
    # Pruning is idempotent and retired rows are never re-inserted.
    followup = sync_world_model_catalog(boundary=boundary, repository=repository)
    assert followup['pruned'] == 0
    assert not _has_model(repository.get(boundary).document, 'gpt-6-astra-pro')
    # deepseek-chat is GLOBALLY retired (official 2026-07-24; the stale
    # Meituan-gateway authorship was removed 2026-09-06): never inserted.
    assert not _has_model(repository.get(boundary).document, 'deepseek-chat')


def test_no_template_authors_a_retired_identity():
    """Anti-oscillation invariant: retired is global.

    deepseek-chat/deepseek-reasoner flipped in and out of the registry when
    deepseek.json retired them while meituan.json still authored them — the
    UI's retired chip advised removal, a PUT wiped the rows, and the next
    boot sync re-inserted them.  No shipped template may author an offering
    whose canonical identity any template retires; the sync's catalogue
    guard stays only as defence for deployment-local overrides.
    """
    from lib.model_catalog._creator_identity import canonical_key
    from lib.provider_template_recipes import load_provider_templates

    templates = load_provider_templates()
    retired = set()
    for template in templates:
        for model_id in (template.get('retired_model_ids') or []):
            key = canonical_key(model_id)
            if key:
                retired.add(key)
    offenders = []
    for template in templates:
        for recipe in (template.get('offering_recipes') or []):
            if canonical_key(recipe.get('model_id')) in retired:
                offenders.append((template['key'], recipe['model_id']))
    assert offenders == []


def test_second_run_is_a_no_op():
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    sync_world_model_catalog(boundary=boundary, repository=repository)
    summary = sync_world_model_catalog(boundary=boundary,
                                       repository=repository)
    assert summary['added'] == 0
    assert summary['creators_added'] == 0


def test_without_owner_context_the_sync_skips(monkeypatch):
    import lib.llm_dispatch.autodiscover_local as autodiscover

    monkeypatch.setattr(autodiscover, '_runtime_context', lambda: None)
    assert sync_world_model_catalog() == {
        'added': 0, 'creators_added': 0, 'skipped': 'no_owner'}
