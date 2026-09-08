"""World model catalogue sync into the owner model-routing authority.

The Model catalog tab (模型配置) is the registry of models that EXIST, not
models some configured provider currently supplies: a newly shipped model
must appear there even before any provider bundle carries it. This sync
compiles every package provider template (the same recipes the onboarding
picker serves) and inserts the missing Creator/Model rows — idempotent:
provider supply collections are never touched and existing registry rows
(including owner edits) are never overwritten.

One authored exception to insert-only: a template's ``retired_model_ids``
is a kill-switch (the dynamic template feed already never re-adds them),
and rows still carrying a retired identity are pruned here — but only when
the freshly compiled catalogue no longer supplies that identity.

Retirement is global: ``tests/test_world_catalog_sync.py`` pins that no
shipped template authors an offering whose identity any template retires
(deepseek-chat flipped in and out of the registry while deepseek.json
retired it and meituan.json still authored it — the UI's retired chip
advised removal, a full-document PUT wiped the row, and the next boot sync
re-inserted it).  The catalogue guard below remains as defence for
deployment-local override templates the shipped-template invariant cannot
see.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _world_catalog_rows() -> tuple[list[dict], list[dict]]:
    """Union of (creators, models) rows compiled from all known templates."""
    from lib.provider_template_recipes import (
        ProviderTemplateRecipeError, compile_provider_template_bundle,
        load_provider_templates)

    creators: dict[str, dict] = {}
    models: dict[tuple, dict] = {}
    for template in load_provider_templates():
        if template.get('category') == 'local':
            continue
        try:
            bundle = compile_provider_template_bundle(template['key'])
        except ProviderTemplateRecipeError as exc:
            logger.warning('world catalogue: skipping template %s: %s',
                           template['key'], exc)
            continue
        for row in bundle['creators']:
            creators.setdefault(row['creator_id'], row)
        for row in bundle['models']:
            models.setdefault((row['creator_id'], row['model_id']), row)
    return list(creators.values()), list(models.values())


def _retired_model_keys() -> set:
    """Canonical keys of every template-authored ``retired_model_ids`` row."""
    from lib.model_catalog._creator_identity import canonical_key
    from lib.provider_template_recipes import load_provider_templates

    retired = set()
    for template in load_provider_templates():
        for model_id in (template.get('retired_model_ids') or []):
            key = canonical_key(model_id)
            if key:
                retired.add(key)
    return retired


def sync_world_model_catalog(*, boundary=None, repository=None) -> dict:
    """Insert catalogue models missing from the owner authority registry.

    Also prunes registry rows whose identity is retired by any template —
    the feed never re-adds them, so they would otherwise linger forever.
    """
    if boundary is None or repository is None:
        from lib.llm_dispatch.autodiscover_local import _runtime_context
        context = _runtime_context()
        if context is None:
            return {'added': 0, 'creators_added': 0, 'skipped': 'no_owner'}
        boundary, repository = context

    from lib.model_catalog._creator_identity import merge_keys

    creators, models = _world_catalog_rows()
    retired_keys = _retired_model_keys()
    catalog_keys = set()
    for row in models:
        catalog_keys.update(merge_keys(row.get('model_id'),
                                       row.get('display_name')))

    def _prunable(row: dict) -> bool:
        keys = merge_keys(row.get('model_id'), row.get('display_name'))
        return bool(retired_keys & keys) and not (catalog_keys & keys)

    current = repository.get(boundary)
    known_creators = {row['creator_id'] for row in current.document['creators']}
    known_keys = set()
    for row in current.document['models']:
        known_keys.update(merge_keys(row.get('model_id'),
                                     row.get('display_name')))
    new_creators = [row for row in creators
                    if row['creator_id'] not in known_creators]
    new_models = [row for row in models
                  if not (merge_keys(row.get('model_id'),
                                     row.get('display_name')) & known_keys)]
    prunable = [row for row in current.document['models'] if _prunable(row)]
    if not new_creators and not new_models and not prunable:
        return {'added': 0, 'creators_added': 0, 'pruned': 0,
                'revision': current.revision}

    pruned_count = 0

    def _mutate(working: dict):
        nonlocal pruned_count
        pruned_count = 0  # reset per attempt: CAS retries re-run this closure
        changed = False
        if retired_keys:
            kept = []
            for row in working['models']:
                if _prunable(row):
                    pruned_count += 1
                    changed = True
                    continue
                kept.append(row)
            if changed:
                working['models'] = kept
        present_creators = {
            row['creator_id'] for row in working['creators']}
        present_keys = set()
        for row in working['models']:
            present_keys.update(merge_keys(row.get('model_id'),
                                           row.get('display_name')))
        appended = False
        for row in new_creators:
            if row['creator_id'] not in present_creators:
                working['creators'].append(dict(row))
                present_creators.add(row['creator_id'])
                appended = True
        for row in new_models:
            keys = merge_keys(row.get('model_id'), row.get('display_name'))
            if present_keys & keys:
                continue
            working['models'].append(dict(row))
            present_keys.update(keys)
            appended = True
        return working if (appended or changed) else None

    authority = repository.mutate(boundary, _mutate)
    summary = {
        'added': len(new_models),
        'creators_added': len(new_creators),
        'pruned': pruned_count,
        'revision': authority.revision,
    }
    logger.info('world catalogue sync: %s', summary)
    return summary


__all__ = ['sync_world_model_catalog']
