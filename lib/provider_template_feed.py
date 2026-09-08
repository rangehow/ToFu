"""lib/provider_template_feed.py — Dynamic provider-template catalog feed.

Two auto-update layers exist and must not be conflated:

1. **Template feed (this module).** The onboarding catalog served by
   ``GET /api/v1/providers/templates`` is authored static JSON that rots one
   release behind each vendor.  A background refresh folds the PUBLIC
   OpenRouter model catalog into the picker: every mapped vendor prefix
   becomes candidate offering recipes for its template, cached on disk with
   a TTL.  No credential is involved — the feed is public data, the cache
   holds no secrets, and it inherits the ``data/`` export exclusion.
2. **Provider catalog sync** (``lib.llm_dispatch.provider_catalog_sync``)
   re-lists a CONFIGURED provider's authenticated ``/models`` and appends
   new wire identities as pending offerings.  Templates stay onboarding
   hints; that worker owns post-setup freshness.

Safety rails
------------
* **Authored wins.** A static recipe row is curated (rpm, capabilities,
  retirement notes); a feed row with the same canonical model identity
  (``MiniMax-M3`` vs ``minimax-m3``) is dropped.
* **Retired names stay dead.** A template may author ``retired_model_ids``
  (DeepSeek's 2026-07-24 aliases); the feed never re-adds them.
* **Rolling aliases only.** Dated checkpoint variants (``-0813``,
  ``-20250805``, ``-2026-04-01``) and OpenRouter billing variants
  (``:free`` / ``:batch``) are never imported — the template suggests
  request identities, not frozen checkpoints.
* **Fail static.** Any fetch/parse/cache failure leaves the authored
  catalog exactly as shipped; the picker never blocks on the network — a
  stale cache triggers a one-flight daemon refresh and is served as-is.
* **Opt-out.** ``TOFU_TEMPLATE_FEED=0`` disables refresh and merging.

Only vendor brands whose official API model ids match the OpenRouter
``vendor/`` spelling are mapped.  Deliberately unmapped:
``meta`` (2026 Muse ids ≠ the ``meta-llama`` prefix), ``doubao``
(volcengine ``doubao-seed-*`` ≠ ``bytedance-seed``), ``nvidia`` (NIM serves
other vendors' prefixed ids), ``meituan`` (own sync script), ``openbmb`` (ModelBest ``MiniCPM-*``
ids ≠ OpenRouter spellings; absent from the 2026-09-07 catalog),
``openrouter``
(aggregator), and the serving clouds (groq/cerebras/together/…) which host
other vendors' models.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time

from lib.config_dir import config_path, ensure_config_dir
from lib.http_client import http_get as _default_http_get
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger
from lib.model_registration import ModelRegistrationError, normalize_model_entry
from lib.model_catalog._creator_identity import canonical_key

logger = get_logger(__name__)

__all__ = [
    'VENDOR_PREFIX_BY_TEMPLATE',
    'load_cached_feed',
    'maybe_refresh_async',
    'merge_feed_recipes',
    'recipes_from_catalog',
    'refresh_now',
]

_OPENROUTER_MODELS_URL = 'https://openrouter.ai/api/v1/models'
_FETCH_TIMEOUT_S = 20  # matches the OpenRouter enrichment timeout
_MAX_FEED_ROWS_PER_TEMPLATE = 512

_CACHE_PATH = config_path('provider_template_feed.json')

# template key → OpenRouter vendor prefix (verified against the live
# catalog 2026-09-06; see module docstring for the unmapped keys).
VENDOR_PREFIX_BY_TEMPLATE = {
    'anthropic': 'anthropic',
    'baidu': 'baidu',
    'cohere': 'cohere',
    'deepseek': 'deepseek',
    'gemini': 'google',
    'glm': 'z-ai',
    'kimi': 'moonshotai',
    'minimax': 'minimax',
    'mistral': 'mistralai',
    'openai': 'openai',
    'perplexity': 'perplexity',
    'qwen': 'qwen',
    'stepfun': 'stepfun',
    'tencent': 'tencent',
    'upstage': 'upstage',
    'xai': 'x-ai',
}

# Frozen checkpoint spellings: -0813, -20250805, -2026-04-01.
_DATED_VARIANT_PAT = re.compile(r'-(?:\d{4}|\d{8}|\d{4}-\d{2}-\d{2})$')

_refresh_lock = threading.Lock()
_refresh_in_flight = False


def _disabled() -> bool:
    return os.environ.get('TOFU_TEMPLATE_FEED', '1').strip().lower() in (
        '0', 'false', 'no', 'off')


def _ttl_seconds() -> float:
    try:
        value = float(os.environ.get('TOFU_TEMPLATE_FEED_TTL_S', '86400'))
    except (TypeError, ValueError, OverflowError):
        value = 86400.0
    return min(7.0 * 86400.0, max(3600.0, value))


# ── Catalog → recipes ──────────────────────────────────────────────

def _capabilities_from_row(model_id: str, row: dict) -> set:
    arch = row.get('architecture')
    arch = arch if isinstance(arch, dict) else {}
    inputs = {str(m).lower() for m in (arch.get('input_modalities') or [])}
    outputs = {str(m).lower() for m in (arch.get('output_modalities') or [])}
    params = {str(p).lower() for p in (row.get('supported_parameters') or [])}

    if outputs and outputs <= {'embeddings'}:
        return {'embedding'}
    caps = {'text'}
    if 'image' in outputs:
        caps.add('image_gen')
    if 'reasoning' in params or 'include_reasoning' in params:
        caps.add('thinking')
    if 'image' in inputs:
        caps.add('vision')
    if 'video' in inputs:
        caps.add('video')
    # Union with the name-pattern registry: it encodes verified facts the
    # catalog fields miss (dual-mode families such as GLM-5.x / Qwen3).
    from lib.llm_dispatch.discovery import _infer_capabilities
    caps |= _infer_capabilities(model_id)
    return caps


def _recipe_from_row(template_key: str, row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    full_id = str(row.get('id') or '')
    prefix = VENDOR_PREFIX_BY_TEMPLATE[template_key] + '/'
    if not full_id.startswith(prefix):
        return None
    short = full_id[len(prefix):].strip()
    if not short or ':' in short or '/' in short:
        return None
    if _DATED_VARIANT_PAT.search(short):
        return None
    recipe = {
        'model_id': short,
        'capabilities': sorted(_capabilities_from_row(short, row)),
    }
    context_length = row.get('context_length')
    if (isinstance(context_length, int)
            and not isinstance(context_length, bool) and context_length > 0):
        recipe['context_window'] = context_length
    pricing = row.get('pricing')
    pricing = pricing if isinstance(pricing, dict) else {}
    try:
        prompt = float(pricing.get('prompt') or 0)
        completion = float(pricing.get('completion') or 0)
    except (TypeError, ValueError):
        prompt = completion = 0.0
    if prompt > 0 or completion > 0:
        recipe['pricing'] = {
            'input': round(prompt * 1e6, 4),
            'output': round(completion * 1e6, 4),
            'currency': 'USD',
        }
    try:
        return normalize_model_entry(recipe)
    except ModelRegistrationError as exc:
        logger.debug('[TemplateFeed] dropping %s row %s: %s',
                     template_key, short, exc)
        return None


def recipes_from_catalog(rows: list) -> dict[str, list[dict]]:
    """Convert one OpenRouter ``/models`` payload into per-template recipes."""
    from lib.llm_dispatch.config._pricing import reevaluate_pricing_tags

    out: dict[str, list[dict]] = {}
    for template_key in VENDOR_PREFIX_BY_TEMPLATE:
        built: dict[str, dict] = {}
        for row in rows:
            recipe = _recipe_from_row(template_key, row)
            if recipe is not None and recipe['model_id'] not in built:
                built[recipe['model_id']] = recipe
        if not built:
            continue
        recipes = [built[key] for key in sorted(built)]
        # Tier tags (cheap) come from real pricing rows only — the same
        # authority the OpenRouter enrichment path uses.
        reevaluate_pricing_tags(
            recipes, log_prefix='template-feed:%s' % template_key)
        out[template_key] = recipes
    return out


# ── Cache ──────────────────────────────────────────────────────────

def load_cached_feed() -> dict | None:
    """Return ``{'fetched_at': float, 'templates': {key: [recipe]}}`` or None.

    Cache rows re-pass the registration contract on every read: the file is
    machine-written, but a hand-edit or torn copy must not inject rows the
    authored catalog would reject.
    """
    raw = read_json(_CACHE_PATH, default=None)
    if not isinstance(raw, dict):
        return None
    templates = raw.get('templates')
    if not isinstance(templates, dict):
        return None
    cleaned: dict[str, list[dict]] = {}
    for key, rows in templates.items():
        if key not in VENDOR_PREFIX_BY_TEMPLATE or not isinstance(rows, list):
            continue
        valid = []
        for row in rows[:_MAX_FEED_ROWS_PER_TEMPLATE]:
            try:
                valid.append(normalize_model_entry(row))
            except ModelRegistrationError:
                continue
        if valid:
            cleaned[key] = valid
    if not cleaned:
        return None
    try:
        fetched_at = float(raw.get('fetched_at') or 0)
    except (TypeError, ValueError, OverflowError):
        fetched_at = 0.0
    return {'fetched_at': fetched_at, 'templates': cleaned}


# ── Refresh ────────────────────────────────────────────────────────

def refresh_now(http_get=None) -> dict:
    """Fetch the public catalog and atomically replace the cache.

    Fail-closed: any failure keeps the previous cache and never raises.
    """
    if _disabled():
        return {'ok': False, 'disabled': True}
    get = http_get or _default_http_get
    try:
        resp = get(_OPENROUTER_MODELS_URL, timeout=_FETCH_TIMEOUT_S,
                   headers={'User-Agent': 'Tofu/1.0'})
    except Exception as exc:
        logger.warning('[TemplateFeed] catalog fetch failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    if not getattr(resp, 'ok', False):
        status = getattr(resp, 'status_code', '?')
        logger.warning('[TemplateFeed] catalog fetch HTTP %s', status)
        return {'ok': False, 'error': 'http-%s' % status}
    try:
        rows = resp.json().get('data')
    except (ValueError, AttributeError) as exc:
        logger.warning('[TemplateFeed] catalog payload is not JSON: %s', exc)
        return {'ok': False, 'error': 'bad-json'}
    if not isinstance(rows, list) or not rows:
        logger.warning('[TemplateFeed] catalog payload has no model list')
        return {'ok': False, 'error': 'bad-payload'}
    templates = recipes_from_catalog(rows)
    payload = {
        'version': 1,
        'source': _OPENROUTER_MODELS_URL,
        'fetched_at': time.time(),
        'templates': templates,
    }
    try:
        ensure_config_dir()
        write_json_atomic(_CACHE_PATH, payload)
    except OSError as exc:
        logger.warning('[TemplateFeed] cache write failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    stats = {
        'ok': True,
        'templates': len(templates),
        'models': sum(len(recipes) for recipes in templates.values()),
    }
    logger.info('[TemplateFeed] refreshed: %d templates, %d models',
                stats['templates'], stats['models'])
    return stats


def maybe_refresh_async(feed: dict | None = None, *, now: float | None = None,
                        _spawn=None) -> bool:
    """Kick a one-flight daemon refresh when the loaded cache is stale.

    ``feed`` is the already-loaded :func:`load_cached_feed` result (None =
    missing/invalid, i.e. stale).  Returns True when a refresh was spawned.
    """
    global _refresh_in_flight
    if _disabled():
        return False
    clock_now = time.time() if now is None else float(now)
    if feed is not None and clock_now - feed['fetched_at'] < _ttl_seconds():
        return False
    with _refresh_lock:
        if _refresh_in_flight:
            return False
        _refresh_in_flight = True

    def _run():
        global _refresh_in_flight
        try:
            refresh_now()
        except Exception as exc:  # refresh_now is fail-closed; flag must reset
            logger.debug('[TemplateFeed] refresh raised: %s', exc)
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    spawn = _spawn or (lambda target: threading.Thread(
        target=target, daemon=True, name='provider-template-feed').start())
    try:
        spawn(_run)
    except Exception as exc:
        with _refresh_lock:
            _refresh_in_flight = False
        logger.debug('[TemplateFeed] refresh spawn failed: %s', exc)
        return False
    return True


# ── Merge ──────────────────────────────────────────────────────────

def merge_feed_recipes(templates: list[dict], feed: dict | None) -> list[dict]:
    """Append feed rows the authored catalog does not already cover.

    Authored recipes win every conflict; ``retired_model_ids`` is the
    authored kill-switch the feed can never resurrect.  Templates without a
    vendor-prefix mapping pass through untouched.
    """
    if _disabled() or not isinstance(feed, dict):
        return templates
    feed_templates = feed.get('templates')
    if not isinstance(feed_templates, dict) or not feed_templates:
        return templates
    merged = []
    for template in templates:
        rows = feed_templates.get(template.get('key'))
        if not rows:
            merged.append(template)
            continue
        authored = {canonical_key(row.get('model_id'))
                    for row in (template.get('offering_recipes') or [])}
        retired = {canonical_key(mid)
                   for mid in (template.get('retired_model_ids') or [])}
        additions = [copy.deepcopy(row) for row in rows
                     if canonical_key(row.get('model_id')) not in authored
                     and canonical_key(row.get('model_id')) not in retired]
        if not additions:
            merged.append(template)
            continue
        out = copy.deepcopy(template)
        out['offering_recipes'] = list(out.get('offering_recipes') or []) + additions
        merged.append(out)
    return merged
