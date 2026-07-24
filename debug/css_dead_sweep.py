#!/usr/bin/env python3
"""One-shot dead-CSS sweeper: removes rules from static/styles.css whose
selector classes are ALL in the verified dead set (debug/css_dead_selectors.txt)
AND include at least one of the top-N classes by bytes. Conservative by
construction — a rule mixing a dead class with a live one is kept.
Prints per-run stats; re-run debug/css_style_audit.py afterwards to confirm.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'debug'))
from css_style_audit import parse_rules, strip_comments, CLASS_RE  # noqa: E402

TOP_N = 30
CSS = ROOT / 'static' / 'styles.css'
DEAD_LIST = ROOT / 'debug' / 'css_dead_selectors.txt'


def main() -> None:
    dead_entries = []
    for line in DEAD_LIST.read_text(encoding='utf-8').splitlines():
        m = re.search(r'\.([a-zA-Z][\w-]*)\s*$', line)
        if m:
            nbytes = int(line.split('B')[0].strip())
            dead_entries.append((m.group(1), nbytes))
    dead_all = {c for c, _ in dead_entries}
    top_n = {c for c, _ in sorted(dead_entries, key=lambda e: -e[1])[:TOP_N]}
    print(f'verified dead set: {len(dead_all)} classes; deleting rules touching top {len(top_n)}')

    raw = CSS.read_text(encoding='utf-8')
    css = strip_comments(raw)
    # map comment-stripped offsets back to raw: strip_comments only removes
    # /* */ blocks; to keep spans aligned we parse rules on a version where
    # comments are replaced by same-length spaces.
    css_aligned = re.sub(r'/\*.*?\*/', lambda m: ' ' * len(m.group(0)), raw, flags=re.S)

    remove_spans = []
    kept_with_dead = 0
    # parse_rules needs positions in the aligned text; replicate its walk by
    # re-parsing with positions. Easiest: re-run a position-tracking variant.
    i, n = 0, len(css_aligned)
    media_depth = 0
    while i < n:
        while i < n and css_aligned[i].isspace():
            i += 1
        if i >= n:
            break
        if css_aligned.startswith('@media', i):
            j = css_aligned.index('{', i)
            media_depth += 1
            i = j + 1
            continue
        if css_aligned.startswith('@keyframes', i) or css_aligned.startswith('@-webkit-keyframes', i):
            j = css_aligned.index('{', i)
            depth, k = 1, j + 1
            while k < n and depth:
                if css_aligned[k] == '{':
                    depth += 1
                elif css_aligned[k] == '}':
                    depth -= 1
                k += 1
            i = k
            continue
        if css_aligned[i] == '}':
            media_depth = max(0, media_depth - 1)
            i += 1
            continue
        if css_aligned[i] == '@':
            j = i
            while j < n and css_aligned[j] not in '{;':
                j += 1
            if j < n and css_aligned[j] == '{':
                depth, k = 1, j + 1
                while k < n and depth:
                    if css_aligned[k] == '{':
                        depth += 1
                    elif css_aligned[k] == '}':
                        depth -= 1
                    k += 1
                i = k
            else:
                i = j + 1
            continue
        j = css_aligned.find('{', i)
        if j == -1:
            break
        selector = css_aligned[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css_aligned[k] == '{':
                depth += 1
            elif css_aligned[k] == '}':
                depth -= 1
            k += 1
        # extend span to swallow the trailing newline for minified one-liners
        end = k
        if end < n and css_aligned[end:end+1] == '\n':
            end += 1
        if selector:
            classes = set(CLASS_RE.findall(selector))
            if classes and classes <= dead_all and classes & top_n:
                remove_spans.append((i, end, selector[:60]))
            elif classes & (dead_all & top_n):
                kept_with_dead += 1
        i = k

    total_bytes = sum(e - s for s, e, _ in remove_spans)
    print(f'rules to remove: {len(remove_spans)} (~{total_bytes:,} B); '
          f'kept despite dead-class mix: {kept_with_dead}')
    for s, e, sel in remove_spans[:12]:
        print(f'  - {sel}')

    out = raw
    for s, e, _ in sorted(remove_spans, reverse=True):
        out = out[:s] + out[e:]
    out = re.sub(r'\n{3,}', '\n\n', out)
    CSS.write_text(out, encoding='utf-8')

    after = re.sub(r'/\*.*?\*/', '', out, flags=re.S)
    print(f'brace depth after: {after.count("{") - after.count("}")} (must be 0)')
    print(f'new size: {len(out.encode("utf-8")):,} B (was {len(raw.encode("utf-8")):,} B)')


if __name__ == '__main__':
    main()
