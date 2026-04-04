#!/usr/bin/env python3
"""Binary search for the exact content triggering HTTP 450 on key_1.

Known: intel_context (4000 chars) triggers 450 on key_1 with gemini-2.5-pro.
This script does a precise binary search down to individual sentences/words.
"""

import re
import sys
import os
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import LLM_API_KEYS, LLM_BASE_URL

API_URL = f'{LLM_BASE_URL}/chat/completions'
KEY_1 = LLM_API_KEYS[1]
MODEL = 'gemini-2.5-pro'
SLEEP = 0.4
MIN_LEN = 5

_call_count = 0
_cache = {}


def probe(text: str) -> bool:
    """Returns True if text triggers 450 on key_1."""
    global _call_count
    h = hash(text)
    if h in _cache:
        return _cache[h]

    body = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': text}],
        'max_tokens': 1,
        'temperature': 0,
    }
    time.sleep(SLEEP)
    _call_count += 1
    try:
        resp = requests.post(
            API_URL,
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {KEY_1}'},
            json=body, timeout=20,
        )
        if resp.status_code == 429:
            print('  [429 — waiting 5s]')
            time.sleep(5)
            return probe(text)
        result = resp.status_code == 450
        _cache[h] = result
        return result
    except requests.exceptions.Timeout:
        print('  [timeout — retrying]')
        time.sleep(2)
        return probe(text)
    except Exception as e:
        print(f'  [error: {e}]')
        return False


def split_articles(text: str) -> list[str]:
    """Split intel text by article boundaries (### N. Title)."""
    parts = re.split(r'\n(?=###?\s*)', text)
    return [p.strip() for p in parts if p.strip()]


def split_sentences(text: str) -> list[str]:
    """Split text into sentences."""
    parts = re.split(r'([。！？；\n.!?;])', text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        s = parts[i] + parts[i + 1]
        s = s.strip()
        if s:
            sentences.append(s)
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].strip())
    return sentences


def split_items(text: str) -> list[str]:
    """Split by list items (- lines)."""
    lines = text.split('\n')
    items = []
    current = ''
    for line in lines:
        if line.strip().startswith('- ') and current:
            items.append(current.strip())
            current = line
        else:
            current += '\n' + line
    if current.strip():
        items.append(current.strip())
    return items


def binary_search_chars(text: str, depth: int = 0) -> list[str]:
    """Binary search at character level."""
    indent = '  ' * (depth + 1)

    if len(text) < MIN_LEN:
        return [text]
    if depth > 15:
        return [text[:200]]

    mid = len(text) // 2
    left = text[:mid]
    right = text[mid:]

    triggers = []

    left_blocked = probe(left) if len(left) >= MIN_LEN else False
    right_blocked = probe(right) if len(right) >= MIN_LEN else False

    if left_blocked:
        print(f'{indent}🔴 L ({len(left)} ch)')
        triggers.extend(binary_search_chars(left, depth + 1))

    if right_blocked:
        print(f'{indent}🔴 R ({len(right)} ch)')
        triggers.extend(binary_search_chars(right, depth + 1))

    if not left_blocked and not right_blocked:
        # Boundary trigger
        for w in [30, 60, 100, 200, 400]:
            ws = max(0, mid - w)
            we = min(len(text), mid + w)
            chunk = text[ws:we]
            if probe(chunk):
                print(f'{indent}🔴 Boundary ±{w} ({len(chunk)} ch)')
                if len(chunk) < 60:
                    triggers.append(chunk)
                else:
                    triggers.extend(binary_search_chars(chunk, depth + 1))
                break
        else:
            print(f'{indent}⚠️ COMBO ({len(text)} ch)')
            triggers.append(f'[COMBO:{len(text)}ch] {text[:100]}...')

    return triggers


def main():
    from debug.probe_content_filter import _build_recent_sim_prompt
    sections = _build_recent_sim_prompt('2025-10-09')
    intel = sections['intel_context']

    print(f'╔════════════════════════════════════════════════════╗')
    print(f'║  Binary Search: key_1 + {MODEL:<20}    ║')
    print(f'║  Intel context: {len(intel)} chars                      ║')
    print(f'╚════════════════════════════════════════════════════╝')
    print()

    # Confirm it's blocked
    if not probe(intel):
        print('❌ Intel context NOT blocked! Filter may be intermittent.')
        return

    print('✅ Confirmed: intel_context triggers 450 on key_1')
    print()

    # Phase 1: Split by category sections
    print('═══ Phase 1: Category sections ═══')
    articles = split_articles(intel)
    print(f'Found {len(articles)} sections')

    blocked_sections = []
    for i, art in enumerate(articles):
        blocked = probe(art)
        status = '🔴' if blocked else '🟢'
        preview = art[:70].replace('\n', '|')
        print(f'  [{i+1}] {status} ({len(art):4d} ch) {preview}')
        if blocked:
            blocked_sections.append((i, art))

    if not blocked_sections:
        print()
        print('No individual section triggers alone — testing accumulation...')
        accumulated = ''
        for i, art in enumerate(articles):
            accumulated += art + '\n\n'
            if probe(accumulated):
                print(f'  Triggers after adding section {i+1}')
                # The trigger needs context from prior sections
                blocked_sections.append((i, accumulated))
                break
        if not blocked_sections:
            print('  Still no trigger — full combination needed')
            blocked_sections.append((-1, intel))

    print()

    # Phase 2: For each blocked section, split by items (- lines)
    print('═══ Phase 2: Item-level search ═══')
    blocked_items = []

    for sec_idx, sec_text in blocked_sections:
        print(f'\n--- Section {sec_idx + 1} ({len(sec_text)} chars) ---')
        items = split_items(sec_text)
        if len(items) <= 1:
            # Can't split further by items, go to char-level
            print(f'  Single block, going to char-level binary search...')
            blocked_items.append(sec_text)
            continue

        print(f'  {len(items)} items')
        for j, item in enumerate(items):
            if len(item) < MIN_LEN:
                continue
            blocked = probe(item)
            status = '🔴' if blocked else '🟢'
            preview = item[:65].replace('\n', '|')
            print(f'    [{j+1}] {status} ({len(item):3d} ch) {preview}')
            if blocked:
                blocked_items.append(item)

    if not blocked_items:
        print('  No individual item triggers — binary searching the section...')
        for _, sec_text in blocked_sections:
            blocked_items.append(sec_text)

    print()

    # Phase 3: Character-level binary search within each blocked item
    print('═══ Phase 3: Character-level binary search ═══')
    all_triggers = []

    for item in blocked_items:
        print(f'\n--- Item ({len(item)} chars): {item[:80].replace(chr(10), "|")}... ---')

        if len(item) < 30:
            all_triggers.append(item)
            continue

        triggers = binary_search_chars(item)
        all_triggers.extend(triggers)

    # Phase 4: For short triggers, test individual words
    print()
    print('═══ Phase 4: Word-level isolation ═══')
    final_triggers = []

    for t in all_triggers:
        if t.startswith('[COMBO'):
            final_triggers.append(t)
            continue

        if len(t) > 200:
            # Still too long — report as-is
            final_triggers.append(t[:200] + '...')
            continue

        print(f'\n  Trigger ({len(t)} ch): "{t}"')

        # Test individual words/tokens
        words = t.split()
        if len(words) > 2:
            blocked_words = []
            for w in words:
                if len(w) < 2:
                    continue
                if probe(w):
                    blocked_words.append(w)
                    print(f'    🎯 Word trigger: "{w}"')

            if blocked_words:
                final_triggers.extend(blocked_words)
                continue

            # Test bigrams
            for k in range(len(words) - 1):
                bigram = words[k] + words[k + 1]  # no space (Chinese)
                if probe(bigram):
                    print(f'    🎯 Bigram trigger: "{bigram}"')
                    final_triggers.append(bigram)
                    break
            else:
                final_triggers.append(t)
        else:
            # Very short — this IS the trigger
            final_triggers.append(t)

    # Results
    print()
    print(f'{"═" * 60}')
    print(f' RESULTS: {len(final_triggers)} trigger(s) identified')
    print(f' API calls: {_call_count}')
    print(f'{"═" * 60}')

    for i, t in enumerate(final_triggers, 1):
        print(f'\n  [{i}] "{t}"')

    # Save
    import json
    out = {'triggers': final_triggers, 'api_calls': _call_count, 'model': MODEL, 'key': 'key_1'}
    with open('debug/key1_triggers_result.json', 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\n💾 Saved to debug/key1_triggers_result.json')


if __name__ == '__main__':
    main()
