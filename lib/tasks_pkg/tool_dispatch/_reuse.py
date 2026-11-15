"""Reuse previously admitted tool results after current freshness checks.

try_reuse_tool_result preserves cached search/image evidence and settles one
occurrence through the caller's canonical seam. It never executes tools or owns
approval; admission and approval order remain in _pipeline.
"""
from __future__ import annotations

from lib.tasks_pkg.tool_dispatch._flags import (
    _make_cache_key, _project_cache_entry_is_current, _discard_tool_result_cache_entry,
    _unpack_cache_entry, _cache_entry_projection_items, _cache_entry_producer_metadata,
)
from lib.tasks_pkg.tool_dispatch._prefetch import has_prefetch
from lib.tasks_pkg.tool_search_projection import cached_tool_search_projection
from lib.tools.gateway import SEARCH_TOOLS_NAME
from lib.tools.result_projection import (
    TOOL_RESULT_PROJECTION_ITEMS_KEY, TOOL_RESULT_PRODUCER_METADATA_KEY,
)


def publish_cached_result_presentation(
    task, fn_name, fn_args, rn, round_entry, *, content, is_prefetch,
    display=None, engine_breakdown=None, vertical=None, search_diag=None,
    search_projection=None, finalize, build_hit_meta,
):
    """Project a cached/prefetched result without settling model evidence."""
    if search_projection is not None:
        search_results, search_meta = search_projection
        extra = {**search_meta, 'cacheSource': 'prefetch' if is_prefetch else 'cache'}
        round_entry.update(extra)
        finalize(task, rn, round_entry, search_results,
            query_override=str(fn_args.get('query') or fn_name),
            extra_event_fields=extra)
        return
    if fn_name == 'web_search' or (display and fn_name == 'fetch_url'):
        extra = {'cacheSource': 'prefetch' if is_prefetch else 'cache'}
        round_entry.update(extra)
        if engine_breakdown:
            round_entry['engineBreakdown'] = engine_breakdown
            extra['engineBreakdown'] = engine_breakdown
        if vertical:
            if isinstance(vertical, dict) and 'batch' in vertical:
                verticals = vertical.get('batch') or []
            elif isinstance(vertical, list):
                verticals = vertical
            else:
                verticals = None
            if verticals is not None:
                round_entry['verticals'] = verticals
                extra['verticals'] = verticals
            else:
                round_entry['vertical'] = vertical
                extra['vertical'] = vertical
        if fn_name == 'web_search' and not display and search_diag:
            round_entry['searchDiag'] = search_diag
            extra['searchDiag'] = search_diag
        finalize(task, rn, round_entry, display or [],
            query_override=round_entry.get('query', fn_name),
            extra_event_fields=extra)
        return
    finalize(task, rn, round_entry, [build_hit_meta(
        fn_name, fn_args, content, is_prefetch, cached_display=display)],
        query_override=round_entry.get('query', fn_name))


def try_reuse_tool_result(
    task, item, *, cache, idempotent_tools, project_path, round_num, tid,
    tool_results, settle_current, finalize, build_hit_meta, logger,
) -> bool:
    """Return whether a fresh cached receipt completed this occurrence."""
    tc, fn_name, tc_id, fn_args, rn, round_entry, parse_error = item
    if fn_name in idempotent_tools and not task.get('aborted') and not has_prefetch(
            task, fn_name, tc_id, fn_args, round_entry, project_path):
        cache_key = _make_cache_key(fn_name, fn_args)
        cached = cache.get(cache_key)

        if (cached is not None
                and not _project_cache_entry_is_current(fn_name, cached)):
            _discard_tool_result_cache_entry(task, cache_key, cached)
            cached = None
        # ── FreshGate: never serve a STALE cached read ──
        # The streaming pre-exec/dedup cache bypasses the project-tool
        # handler, so a cached read_files/inspect_image result can be
        # arbitrarily older than the disk (sibling edit, git checkout).
        # Serving it hands the model stale bytes AND never re-stamps
        # the write-freshness token — the 're-reads never clear the
        # refusal' loop (). When a covered file moved since
        # this conversation's token, drop the entry and fall through to
        # a REAL read (which re-stamps via the handler seam).
        if cached is not None:
            try:
                from lib.tasks_pkg.handlers._write_freshness_gate import (
                    FILE_READ_TOOLS, cached_read_is_stale,
                )
                if (fn_name in FILE_READ_TOOLS
                        and cached_read_is_stale(task, fn_args,
                                                 project_path)):
                    _discard_tool_result_cache_entry(task, cache_key, cached)
                    cached = None
                    logger.info(
                        '[Task %s] conv=%s FreshGate: %s cache hit '
                        'BYPASSED — covered file changed since cached '
                        'read; re-executing',
                        tid, task.get('convId', ''), fn_name)
            except Exception as _fe:
                logger.debug('[FreshGate] cached-read staleness check '
                             'failed (non-fatal): %s', _fe)
        search_projection = None
        if cached is not None and fn_name == SEARCH_TOOLS_NAME:
            search_projection = cached_tool_search_projection(
                _unpack_cache_entry(cached)[0])
            if search_projection is None:
                # Invalid cache data is not evidence of zero matches.
                # Let the normal handler produce a fresh typed result.
                _discard_tool_result_cache_entry(task, cache_key, cached)
                cached = None
        if cached is not None:
            cached_content, cached_is_search, cached_source, cached_display, cached_engine_bkdn, cached_vertical, cached_search_diag = \
                _unpack_cache_entry(cached)
            cached_projection_items = _cache_entry_projection_items(cached)
            cached_producer_metadata = (
                _cache_entry_producer_metadata(cached))
            is_prefetch = cached_source == 'prefetch'
            # Compute content length for logging without materializing
            # a massive str() for screenshot dicts (which contain base64)
            if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                _log_len = cached_content.get('compressedSize', 0)
                _log_suffix = ' (image)'
            else:
                _log_len = len(str(cached_content))
                _log_suffix = ''
            logger.info(
                '[Task %s] conv=%s %s HIT: %s with same args at round %d — '
                'returning %s result (%d chars%s) instead of re-executing',
                tid, task.get('convId', ''),
                'PREFETCH' if is_prefetch else 'DEDUP',
                fn_name, round_num,
                'prefetched' if is_prefetch else 'cached',
                _log_len, _log_suffix,
            )
            # Preserve __screenshot__ dicts as-is so the post-phase
            # can detect them and convert to image_url blocks.
            # Converting to str() would dump 800K+ of base64 text
            # directly into the context, blowing up the token count.
            if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                dedup_content = cached_content  # keep as dict
            else:
                dedup_content = cached_content if isinstance(cached_content, str) else str(cached_content)
            if round_entry:
                publish_cached_result_presentation(
                    task, fn_name, fn_args, rn, round_entry,
                    content=cached_content, is_prefetch=is_prefetch,
                    display=cached_display,
                    engine_breakdown=cached_engine_bkdn,
                    vertical=cached_vertical, search_diag=cached_search_diag,
                    search_projection=search_projection,
                    finalize=finalize, build_hit_meta=build_hit_meta)
                if cached_projection_items:
                    round_entry[TOOL_RESULT_PROJECTION_ITEMS_KEY] = (
                        cached_projection_items)
                if cached_producer_metadata:
                    round_entry[TOOL_RESULT_PRODUCER_METADATA_KEY] = (
                        cached_producer_metadata)
            tool_results[tc_id] = (dedup_content, cached_is_search)
            # Settle NOW (). A cache/prefetch hit costs ZERO
            #   time: for a streaming-prefetch hit the tool already ran while
            #   the model was still emitting tokens (StreamingToolExecutor
            #   → inject_into_cache). Leaving it to the post-phase made the
            #   FASTEST class of tool in the product the one that waited
            #   longest — measured: tool_complete(cached) landed after
            #   tool_complete(slow-sibling).
            settle_current(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                dedup_content, idempotent_tools=idempotent_tools,
                cache=cache, tid=tid, round_num=round_num)
            return True

    return False
