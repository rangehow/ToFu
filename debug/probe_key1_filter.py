#!/usr/bin/env python3
"""probe_key1_filter.py — Precisely identify content triggering HTTP 450 on key_1.

Key insight from logs: key_0 never gets blocked, key_1 always does on the same content.
This script uses key_1 for binary search, confirms with key_0 as control.

Strategy:
    1. Fetch actual intel content from DB (the most likely trigger)
    2. Confirm: same content passes on key_0, blocked on key_1
    3. Binary search on key_1 down to individual sentences/phrases
    4. For each trigger found, test individual words to find the exact trigger word(s)

Usage:
    python debug/probe_key1_filter.py                  # auto-scan recent intel
    python debug/probe_key1_filter.py --text "test"    # test a specific string
    python debug/probe_key1_filter.py --full-prompt    # test with full sim prompt
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from lib import LLM_API_KEYS, LLM_BASE_URL

# ── Configuration ──
API_URL = f'{LLM_BASE_URL}/chat/completions'
PROBE_MODEL = 'qwen3.6-plus'  # cheap and fast
SLEEP = 0.3     # between requests
MIN_TRIGGER_LEN = 5  # stop binary search when chunk < this
MAX_DEPTH = 12   # max recursion depth

# We need at least 2 keys
assert len(LLM_API_KEYS) >= 2, f'Need ≥2 API keys, got {len(LLM_API_KEYS)}'
KEY_0 = LLM_API_KEYS[0]   # expected: passes
KEY_1 = LLM_API_KEYS[1]   # expected: blocked

_call_count = 0
_cache = {}  # (text_hash, key_index) → bool, avoid redundant calls


def _make_headers(api_key: str) -> dict:
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
    }


def probe(text: str, api_key: str = None, key_label: str = 'key_1') -> bool:
    """Send text to the gateway. Returns True if HTTP 450 (blocked).

    Uses key_1 by default since that's the one with stricter filtering.
    """
    global _call_count
    if api_key is None:
        api_key = KEY_1

    # Cache check
    cache_key = (hash(text), api_key[-8:])
    if cache_key in _cache:
        return _cache[cache_key]

    body = {
        'model': PROBE_MODEL,
        'messages': [{'role': 'user', 'content': text}],
        'max_tokens': 1,
        'temperature': 0,
    }
    time.sleep(SLEEP)
    _call_count += 1
    try:
        resp = requests.post(API_URL, headers=_make_headers(api_key), json=body, timeout=30)
        if resp.status_code == 450:
            _cache[cache_key] = True
            return True
        if resp.status_code == 429:
            print(f'  [429 on {key_label} — waiting 5s]')
            time.sleep(5)
            return probe(text, api_key, key_label)  # retry
        _cache[cache_key] = False
        return False
    except Exception as e:
        print(f'  [Request error on {key_label}: {e}]')
        return False


def probe_both(text: str, label: str = '') -> tuple[bool, bool]:
    """Test text on both keys. Returns (key0_blocked, key1_blocked)."""
    b0 = probe(text, KEY_0, 'key_0')
    b1 = probe(text, KEY_1, 'key_1')
    tag = f' [{label}]' if label else ''
    s0 = '🔴' if b0 else '🟢'
    s1 = '🔴' if b1 else '🟢'
    print(f'  key_0:{s0}  key_1:{s1}  ({len(text)} chars){tag}')
    return b0, b1


def split_sentences(text: str) -> list[str]:
    """Split Chinese/mixed text into sentences."""
    # Split on Chinese/English sentence-ending punctuation
    parts = re.split(r'([。！？；\n.!?;])', text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        sent = parts[i] + parts[i + 1]
        sent = sent.strip()
        if sent:
            sentences.append(sent)
    # Handle trailing text without punctuation
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].strip())
    return sentences


def binary_search_trigger(text: str, depth: int = 0, path: str = '') -> list[str]:
    """Binary search to find minimal triggering substrings.

    Returns list of minimal trigger snippets.
    """
    indent = '  ' * (depth + 1)

    if len(text) < MIN_TRIGGER_LEN:
        return [text]

    if depth >= MAX_DEPTH:
        print(f'{indent}⚠️ Max depth — trigger within: {text[:80]}...')
        return [text]

    # First, try sentence-level splitting (more meaningful than raw char split)
    if depth == 0:
        sentences = split_sentences(text)
        if len(sentences) > 2:
            print(f'{indent}Trying sentence-level split ({len(sentences)} sentences)...')
            blocked_sents = []
            for i, sent in enumerate(sentences):
                if len(sent) < MIN_TRIGGER_LEN:
                    continue
                is_blocked = probe(sent)
                status = '🔴' if is_blocked else '🟢'
                preview = sent[:60].replace('\n', ' ')
                print(f'{indent}  [{i+1}/{len(sentences)}] {status} {preview}...')
                if is_blocked:
                    blocked_sents.append(sent)

            if blocked_sents:
                print(f'{indent}Found {len(blocked_sents)} blocked sentence(s). Drilling deeper...')
                triggers = []
                for sent in blocked_sents:
                    triggers.extend(_drill_into_sentence(sent, depth + 1))
                return triggers
            else:
                print(f'{indent}No individual sentence triggers — checking combinations...')
                # Fall through to binary char split

    # Binary split by characters
    mid = len(text) // 2
    left = text[:mid]
    right = text[mid:]

    left_blocked = probe(left) if len(left) >= MIN_TRIGGER_LEN else False
    right_blocked = probe(right) if len(right) >= MIN_TRIGGER_LEN else False

    triggers = []

    if left_blocked:
        print(f'{indent}🔴 Left ({len(left)} chars) blocked — recursing')
        triggers.extend(binary_search_trigger(left, depth + 1, path + 'L'))
    else:
        print(f'{indent}🟢 Left ({len(left)} chars) OK')

    if right_blocked:
        print(f'{indent}🔴 Right ({len(right)} chars) blocked — recursing')
        triggers.extend(binary_search_trigger(right, depth + 1, path + 'R'))
    else:
        print(f'{indent}🟢 Right ({len(right)} chars) OK')

    if not left_blocked and not right_blocked:
        # Trigger spans the boundary — try overlapping windows
        print(f'{indent}⚠️ Neither half triggers alone — boundary search...')
        for window in [30, 60, 100, 200, 400]:
            w_start = max(0, mid - window)
            w_end = min(len(text), mid + window)
            chunk = text[w_start:w_end]
            if probe(chunk):
                print(f'{indent}🔴 Boundary ±{window} ({len(chunk)} chars) triggers')
                if len(chunk) <= 60:
                    triggers.append(chunk)
                else:
                    triggers.extend(binary_search_trigger(chunk, depth + 1, path + 'B'))
                break
        else:
            print(f'{indent}⚠️ COMBO trigger — needs full {len(text)} chars together')
            triggers.append(f'[COMBO: {len(text)} chars]')

    return triggers


def _drill_into_sentence(sentence: str, depth: int = 0) -> list[str]:
    """For a blocked sentence, find the minimal trigger — could be a single word/phrase."""
    indent = '  ' * (depth + 1)

    if len(sentence) < MIN_TRIGGER_LEN:
        return [sentence]

    # Try word-level split first
    words = sentence.split()
    if len(words) <= 2:
        # Try character-level for very short text
        return _char_level_search(sentence, depth)

    # Test each word individually
    blocked_words = []
    for w in words:
        if len(w) < 2:
            continue
        if probe(w):
            blocked_words.append(w)

    if blocked_words:
        print(f'{indent}🎯 Individual trigger word(s): {blocked_words}')
        return blocked_words

    # No single word triggers — try 2-word windows
    print(f'{indent}No single word triggers — trying 2-word windows...')
    for i in range(len(words) - 1):
        bigram = words[i] + ' ' + words[i + 1]
        if probe(bigram):
            print(f'{indent}🎯 Bigram trigger: "{bigram}"')
            return [bigram]

    # Try 3-word windows
    print(f'{indent}Trying 3-word windows...')
    for i in range(len(words) - 2):
        trigram = ' '.join(words[i:i + 3])
        if probe(trigram):
            print(f'{indent}🎯 Trigram trigger: "{trigram}"')
            return [trigram]

    # Fall back to binary character search
    print(f'{indent}Falling back to binary char search...')
    return binary_search_trigger(sentence, depth + 1)


def _char_level_search(text: str, depth: int) -> list[str]:
    """Binary search at character level for very short strings."""
    if len(text) < MIN_TRIGGER_LEN or depth > MAX_DEPTH:
        return [text]

    mid = len(text) // 2
    left = text[:mid]
    right = text[mid:]

    if len(left) >= MIN_TRIGGER_LEN and probe(left):
        return _char_level_search(left, depth + 1)
    if len(right) >= MIN_TRIGGER_LEN and probe(right):
        return _char_level_search(right, depth + 1)
    return [text]


def get_intel_from_db(limit: int = 50) -> list[dict]:
    """Fetch recent intel articles from the trading DB."""
    from lib.database._core import get_thread_db, DOMAIN_TRADING

    db = get_thread_db(DOMAIN_TRADING)
    rows = db.execute("""
        SELECT id, title, raw_content, summary, category, source_url
        FROM trading_intel_cache
        WHERE raw_content IS NOT NULL AND raw_content != ''
        ORDER BY fetched_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_sim_intel(sim_date: str = '2025-10-09') -> str:
    """Build the intel context that the simulator would use at a given date."""
    try:
        from lib.database._core import get_thread_db, DOMAIN_TRADING
        from lib.trading.intel_timeline import build_intel_context_at

        db = get_thread_db(DOMAIN_TRADING)
        intel_ctx, count = build_intel_context_at(db, sim_date, only_confident_dates=True)
        return intel_ctx or ''
    except Exception as e:
        print(f'Could not build intel context: {e}')
        return ''


def phase1_verify_key_difference():
    """Phase 1: Confirm that the filter is key-specific, not content-specific."""
    print('═══════════════════════════════════════════════════')
    print(' Phase 1: Verify key-level filtering difference')
    print('═══════════════════════════════════════════════════')

    # Simple messages should pass on both
    print('\n1a. Simple message (should pass both):')
    probe_both('你好，今天天气怎么样？', 'simple')

    # Financial keywords
    print('\n1b. Financial keywords:')
    probe_both('分析一下A股市场的投资机会，关注沪深300和创业板指数走势。', 'financial')

    # Longer financial content
    print('\n1c. Extended financial analysis prompt:')
    fin_prompt = ('你是一位资深基金经理，请分析以下市场数据并给出投资建议：\n'
                  '沪深300指数今日下跌2.3%，成交量放大。\n'
                  '美联储加息预期升温，美元走强。\n'
                  '国内GDP增速放缓，CPI维持低位。\n'
                  '请给出资产配置建议。')
    probe_both(fin_prompt, 'fin_analysis')

    # Get some real intel content
    print('\n1d. Real intel content from DB:')
    intel = get_sim_intel('2025-10-09')
    if intel:
        # Test first 2000 chars
        probe_both(intel[:2000], 'intel_2000')
    else:
        print('  (no intel available)')

    return intel


def phase2_section_scan(intel: str):
    """Phase 2: Split intel into sections and find which ones trigger."""
    print('\n═══════════════════════════════════════════════════')
    print(' Phase 2: Section-level scan (key_1 only)')
    print('═══════════════════════════════════════════════════')

    if not intel:
        print('No intel content to scan.')
        return []

    # Split by article boundaries (numbered items or section headers)
    # Intel format is typically: "### N. Title\nContent\n\n### N+1. Title\n..."
    articles = re.split(r'\n(?=###?\s*\d+\.)', intel)
    if len(articles) <= 1:
        # Try splitting by double newlines
        articles = [a.strip() for a in intel.split('\n\n') if a.strip()]

    print(f'Found {len(articles)} article/section(s) in intel.')

    blocked_articles = []
    for i, art in enumerate(articles):
        if len(art) < 10:
            continue
        is_blocked = probe(art)
        status = '🔴 BLOCKED' if is_blocked else '🟢 OK'
        preview = art[:70].replace('\n', ' ')
        print(f'  [{i+1}/{len(articles)}] {status} ({len(art)} chars): {preview}')
        if is_blocked:
            blocked_articles.append((i, art))

    return blocked_articles


def phase3_deep_search(blocked_articles: list[tuple[int, str]]):
    """Phase 3: Binary search within each blocked article to find exact triggers."""
    print('\n═══════════════════════════════════════════════════')
    print(' Phase 3: Deep binary search for exact triggers')
    print('═══════════════════════════════════════════════════')

    all_triggers = []
    for idx, art in blocked_articles:
        print(f'\n--- Article {idx + 1} ({len(art)} chars) ---')
        preview = art[:100].replace('\n', ' ')
        print(f'    Preview: {preview}...')

        triggers = binary_search_trigger(art)
        for t in triggers:
            result = {'article_idx': idx + 1, 'trigger': t, 'article_preview': art[:100]}
            all_triggers.append(result)

    return all_triggers


def phase4_cross_validate(triggers: list[dict]):
    """Phase 4: Verify each trigger on both keys + test minimal variations."""
    print('\n═══════════════════════════════════════════════════')
    print(' Phase 4: Cross-validation (both keys)')
    print('═══════════════════════════════════════════════════')

    for i, t in enumerate(triggers):
        trigger = t['trigger']
        if trigger.startswith('[COMBO'):
            print(f'\n  [{i+1}] {trigger} — skipping (combination trigger)')
            continue

        print(f'\n  [{i+1}] "{trigger[:80]}"')
        b0, b1 = probe_both(trigger, f'trigger-{i+1}')
        t['key_0_blocked'] = b0
        t['key_1_blocked'] = b1

        # Test variations: add innocent prefix/suffix
        if b1 and not b0:
            # Interesting: only key_1 blocks it. This IS the key-level filter.
            disguised = f'关于以下内容的分析：{trigger}'
            b0d = probe(disguised, KEY_0, 'key_0')
            b1d = probe(disguised, KEY_1, 'key_1')
            print(f'    Disguised: key_0:{"🔴" if b0d else "🟢"}  key_1:{"🔴" if b1d else "🟢"}')

    return triggers


def main():
    parser = argparse.ArgumentParser(description='Probe content filter on key_1 vs key_0')
    parser.add_argument('--text', help='Test a specific string')
    parser.add_argument('--sim-date', default='2025-10-09',
                        help='Simulation date for intel context')
    parser.add_argument('--skip-phase1', action='store_true',
                        help='Skip key verification phase')
    parser.add_argument('--db-scan', action='store_true',
                        help='Scan individual DB articles instead of sim intel')
    parser.add_argument('--limit', type=int, default=30,
                        help='Max articles to scan in DB mode')
    args = parser.parse_args()

    print(f'╔═══════════════════════════════════════════════════╗')
    print(f'║  Content Filter Probe: key_0 vs key_1            ║')
    print(f'║  Model: {PROBE_MODEL:<32}       ║')
    print(f'║  Sim date: {args.sim_date:<29}       ║')
    print(f'╚═══════════════════════════════════════════════════╝')
    print()

    # ── Quick text test mode ──
    if args.text:
        print(f'Testing provided text ({len(args.text)} chars)...')
        probe_both(args.text, 'user-text')
        if probe(args.text):
            print('\nBlocked on key_1 — binary searching...')
            triggers = binary_search_trigger(args.text)
            print(f'\n📋 Found {len(triggers)} trigger(s):')
            for i, t in enumerate(triggers, 1):
                print(f'  {i}. {t[:200]}')
        print(f'\nTotal API calls: {_call_count}')
        return

    # ── Phase 1: Verify key-level filtering ──
    if not args.skip_phase1:
        intel = phase1_verify_key_difference()
    else:
        intel = get_sim_intel(args.sim_date)

    if not intel:
        print('\n⚠️ No intel content found. Trying DB scan...')
        args.db_scan = True

    # ── DB article scan mode ──
    if args.db_scan:
        print('\n═══════════════════════════════════════════════════')
        print(' DB Article Scan')
        print('═══════════════════════════════════════════════════')

        articles = get_intel_from_db(args.limit)
        print(f'Found {len(articles)} articles.')

        blocked_texts = []
        for i, art in enumerate(articles):
            title = art.get('title', '')[:60]
            content = art.get('raw_content', '') or art.get('summary', '') or ''
            if len(content) < 20:
                continue

            # Test first 1500 chars of content
            test_text = content[:1500]
            is_blocked = probe(test_text)
            status = '🔴' if is_blocked else '🟢'
            print(f'  [{i+1}/{len(articles)}] {status} {title}')
            if is_blocked:
                blocked_texts.append((i, title, test_text))

        if blocked_texts:
            print(f'\n🔴 {len(blocked_texts)} articles blocked. Deep searching first 3...')
            all_triggers = []
            for idx, title, text in blocked_texts[:3]:
                print(f'\n--- "{title}" ---')
                triggers = binary_search_trigger(text)
                for t in triggers:
                    all_triggers.append({'source': title, 'trigger': t})

            _print_results(all_triggers)
        else:
            print('\n✅ No articles blocked on key_1!')

        print(f'\nTotal API calls: {_call_count}')
        return

    # ── Phase 2: Section-level scan ──
    blocked_articles = phase2_section_scan(intel)

    if not blocked_articles:
        print('\n✅ No individual sections blocked! Testing full intel...')
        full_blocked = probe(intel[:4000])
        if full_blocked:
            print('🔴 Full intel blocked but no section alone triggers — combination effect.')
            # Try sequential accumulation: add sections one by one
            articles = re.split(r'\n(?=###?\s*\d+\.)', intel)
            if len(articles) <= 1:
                articles = [a.strip() for a in intel.split('\n\n') if a.strip()]
            accumulated = ''
            for i, art in enumerate(articles):
                accumulated += art + '\n\n'
                if probe(accumulated):
                    print(f'  🔴 Triggers after adding section {i+1}')
                    # The trigger is in the last-added section or its combo
                    blocked_articles = [(i, art)]
                    break
        else:
            print('🟢 Full intel passes too — filter may be intermittent.')
            print(f'\nTotal API calls: {_call_count}')
            return

    # ── Phase 3: Deep binary search ──
    triggers = phase3_deep_search(blocked_articles)

    if not triggers:
        print('\n⚠️ No triggers isolated.')
        print(f'\nTotal API calls: {_call_count}')
        return

    # ── Phase 4: Cross-validate ──
    triggers = phase4_cross_validate(triggers)

    # ── Results ──
    _print_results(triggers)

    print(f'\nTotal API calls: {_call_count}')


def _print_results(triggers: list[dict]):
    """Print and save results."""
    print(f'\n{"═" * 60}')
    print(f' RESULTS: {len(triggers)} trigger(s) found')
    print(f'{"═" * 60}')

    for i, t in enumerate(triggers, 1):
        trigger = t.get('trigger', str(t))
        print(f'\n  [{i}] "{trigger[:200]}"')
        if 'key_0_blocked' in t:
            print(f'       key_0: {"🔴 BLOCKED" if t["key_0_blocked"] else "🟢 OK"}')
            print(f'       key_1: {"🔴 BLOCKED" if t["key_1_blocked"] else "🟢 OK"}')
        if 'source' in t:
            print(f'       source: {t["source"][:80]}')

    # Save to file
    out_path = 'debug/key1_filter_triggers.json'
    try:
        with open(out_path, 'w') as f:
            json.dump(triggers, f, ensure_ascii=False, indent=2)
        print(f'\n💾 Full results saved to {out_path}')
    except Exception as e:
        print(f'\n⚠️ Could not save results: {e}')


if __name__ == '__main__':
    main()
