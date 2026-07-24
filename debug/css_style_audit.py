#!/usr/bin/env python3
"""One-shot CSS audit for static/styles.css.

Two measurements, both fully mechanical (no estimates):

(a) Theme-duplication: parse every rule whose selector carries a
    [data-theme="..."] attribute, group declarations by (component, theme),
    and quantify cross-theme duplication: pure color-swap vs structural vs
    identical repeats.

(b) Dead-selector scan: extract every class selector, then check each
    against index.html, static/*.html, static/settings_panels/*.html and
    static/js/**/*.js (generated bundle-*/feature-* files excluded).
    A class is VERIFIED dead only if its literal name appears nowhere in
    the corpus and it doesn't match a dynamically-built prefix found in
    the JS sources. Byte attribution credits a rule's bytes to the dead
    set only when every class in its selector is dead.

Usage: python3 debug/css_style_audit.py
Writes the full dead list to debug/css_dead_selectors.txt.
"""
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT / 'static' / 'styles.css'
DEAD_LIST_OUT = ROOT / 'debug' / 'css_dead_selectors.txt'

COLORISH_PROP_RE = re.compile(
    r'(color|background|border|fill|stroke|shadow|outline|caret|accent|text-decoration)', re.I)
COLOR_TOKEN_RE = re.compile(
    r'(#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|var\(\s*--|color-mix\(|[a-z]+)', re.I)

COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)
THEME_ATTR_RE = re.compile(r'\[\s*data-theme\s*=\s*["\']([^"\']+)["\']\s*\]')
CLASS_RE = re.compile(r'\.(-?[_a-zA-Z]+[_a-zA-Z0-9-]*)')


def strip_comments(css: str) -> str:
    return COMMENT_RE.sub('', css)


def parse_rules(css: str):
    """Yield (selector_text, decls_text, byte_len, media_context) for each rule.

    Tolerant brace-matching parser: walks the comment-stripped source,
    tracking @media nesting one level deep. Skips @keyframes bodies and
    at-rules without declarations.
    """
    i, n = 0, len(css)
    media_stack = []
    while i < n:
        # skip whitespace
        while i < n and css[i].isspace():
            i += 1
        if i >= n:
            break
        if css.startswith('@media', i):
            j = css.index('{', i)
            media_stack.append(css[i:j].strip())
            i = j + 1
            continue
        if css.startswith('@keyframes', i) or css.startswith('@-webkit-keyframes', i):
            # skip whole keyframes block
            j = css.index('{', i)
            depth, k = 1, j + 1
            while k < n and depth:
                if css[k] == '{':
                    depth += 1
                elif css[k] == '}':
                    depth -= 1
                k += 1
            i = k
            continue
        if css[i] == '}':
            if media_stack:
                media_stack.pop()
            i += 1
            continue
        if css[i] == '@':
            # other at-rules (@font-face, @import, @supports…) — @font-face
            # carries declarations we want to skip as a unit
            j = i
            while j < n and css[j] not in '{;':
                j += 1
            if j < n and css[j] == '{':
                depth, k = 1, j + 1
                while k < n and depth:
                    if css[k] == '{':
                        depth += 1
                    elif css[k] == '}':
                        depth -= 1
                    k += 1
                i = k
            else:
                i = j + 1
            continue
        # regular rule: selector up to '{', decls up to matching '}'
        j = css.find('{', i)
        if j == -1:
            break
        selector = css[i:j].strip()
        depth, k = 1, j + 1
        while k < n and depth:
            if css[k] == '{':
                depth += 1
            elif css[k] == '}':
                depth -= 1
            k += 1
        decls = css[j + 1:k - 1]
        byte_len = k - i
        media = media_stack[-1] if media_stack else ''
        i = k
        if selector:
            yield selector, decls, byte_len, media


def parse_decls(decls: str):
    """Return list of (prop, value) preserving order; skips nested garbage."""
    out = []
    for chunk in decls.split(';'):
        if ':' in chunk:
            prop, val = chunk.split(':', 1)
            prop = prop.strip()
            if prop and re.fullmatch(r'[-_a-zA-Z][_a-zA-Z0-9-]*', prop):
                out.append((prop, val.strip()))
    return out


def theme_of(selector: str):
    m = THEME_ATTR_RE.search(selector)
    return m.group(1) if m else None


def component_of(selector: str) -> str:
    """Selector minus the data-theme attribute and any html/body prefix."""
    s = THEME_ATTR_RE.sub('', selector)
    s = re.sub(r'\b(html|body)\b', '', s)
    return re.sub(r'\s+', ' ', s).strip()


def is_colorish(prop: str, val: str) -> bool:
    if COLORISH_PROP_RE.search(prop):
        return True
    return bool(re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|var\(\s*--', val))


def main() -> None:
    raw = CSS_PATH.read_text(encoding='utf-8')
    total_bytes = len(raw.encode('utf-8'))
    css = strip_comments(raw)

    rules = list(parse_rules(css))
    accounted = sum(r[2] for r in rules)
    print(f'== parser sanity ==')
    print(f'file bytes: {total_bytes:,}  rules parsed: {len(rules):,}  '
          f'rule bytes: {accounted:,} ({accounted / total_bytes * 100:.1f}%)')

    # ── (a) theme duplication ────────────────────────────────────────────
    # comp_decls[component][theme] = list of (prop, value)
    comp_decls = defaultdict(lambda: defaultdict(list))
    theme_rule_bytes = 0
    theme_rule_count = 0
    for selector, decls, blen, _media in rules:
        t = theme_of(selector)
        if not t:
            continue
        theme_rule_count += 1
        theme_rule_bytes += blen
        comp = component_of(selector)
        if not comp:
            continue
        for part in selector.split(','):
            pt = theme_of(part)
            if not pt:
                continue
            pc = component_of(part)
            if pc:
                comp_decls[pc][pt].extend(parse_decls(decls))
        if comp not in comp_decls:
            comp_decls[comp][t].extend(parse_decls(decls))

    multi_theme_comps = {c: ts for c, ts in comp_decls.items() if len(ts) >= 2}
    color_swap_decls = 0
    color_swap_bytes = 0
    structural_decls = 0
    structural_bytes = 0
    identical_decls = 0
    identical_bytes = 0
    dup_rows = []
    for comp, ts in multi_theme_comps.items():
        themes = sorted(ts)
        # per-prop: collect values across themes
        prop_vals = defaultdict(dict)
        for th in themes:
            for prop, val in ts[th]:
                prop_vals[prop][th] = val
        comp_dup = 0
        for prop, vals in prop_vals.items():
            if len(vals) < 2:
                continue
            uniq = set(vals.values())
            approx_bytes = (len(prop) + max(len(v) for v in vals.values()) + 4) * (len(vals) - 1)
            if len(uniq) == 1:
                identical_decls += len(vals) - 1
                identical_bytes += approx_bytes
                comp_dup += 1
            elif all(is_colorish(prop, v) for v in uniq):
                color_swap_decls += len(vals) - 1
                color_swap_bytes += approx_bytes
                comp_dup += 1
            else:
                structural_decls += len(vals) - 1
                structural_bytes += approx_bytes
        dup_rows.append((comp_dup, comp, len(themes)))
    dup_rows.sort(reverse=True)

    print(f'\n== (a) theme duplication ==')
    print(f'[data-theme] rules: {theme_rule_count:,}  bytes: {theme_rule_bytes:,} '
          f'({theme_rule_bytes / total_bytes * 100:.1f}% of file)')
    print(f'component selectors themed in >=2 themes: {len(multi_theme_comps):,}')
    print(f'pure color-swap duplicated decls: {color_swap_decls:,}  ~{color_swap_bytes:,} B')
    print(f'identical (copy-paste) duplicated decls: {identical_decls:,}  ~{identical_bytes:,} B')
    print(f'structural duplicated decls: {structural_decls:,}  ~{structural_bytes:,} B')
    reclaim = color_swap_bytes + identical_bytes
    print(f'refactor-reclaimable (color-swap + identical): ~{reclaim:,} B '
          f'({reclaim / total_bytes * 100:.1f}% of file)')
    print('top-10 most-duplicated components:')
    for dup, comp, nth in dup_rows[:10]:
        print(f'  {dup:3d} dup decls across {nth} themes  {comp[:90]}')

    # ── (b) dead-selector scan ───────────────────────────────────────────
    corpus_files = [ROOT / 'index.html']
    corpus_files += list((ROOT / 'static').glob('*.html'))
    corpus_files += list((ROOT / 'static' / 'settings_panels').glob('*.html'))
    corpus_files += [p for p in (ROOT / 'static' / 'js').rglob('*.js')
                     if not p.name.startswith(('bundle-', 'feature-'))]
    corpus = ''
    for p in corpus_files:
        try:
            corpus += p.read_text(encoding='utf-8', errors='ignore') + '\n'
        except OSError:
            continue

    # dynamic class prefixes built in JS: 'foo-' + x / `foo-${...}` / "foo-"+x
    dyn_prefixes = set()
    for m in re.finditer(r'''['"`]([a-zA-Z][a-zA-Z0-9_-]{2,}-)['"`]\s*\+''', corpus):
        dyn_prefixes.add(m.group(1))
    for m in re.finditer(r'`[^`]*?\b([a-zA-Z][a-zA-Z0-9_-]{2,}-)\$\{', corpus):
        dyn_prefixes.add(m.group(1))

    # rule bytes per class: rules where the class is present in selector
    class_rules = defaultdict(list)  # cls -> [(blen, all_classes)]
    all_classes = set()
    for selector, decls, blen, _media in rules:
        cls_in_rule = set(CLASS_RE.findall(selector))
        # drop pseudo-class false positives (e.g. .hover never matches CLASS_RE, safe)
        for c in cls_in_rule:
            all_classes.add(c)
            class_rules[c].append((blen, selector, cls_in_rule))

    dead = []
    dyn_skipped = 0
    for cls in sorted(all_classes):
        if cls in corpus:
            continue
        if any(cls.startswith(p) for p in dyn_prefixes):
            dyn_skipped += 1
            continue
        # byte attribution: only rules whose every class is unreferenced
        rule_bytes = sum(b for b, _sel, classes in class_rules[cls])
        dead.append((cls, len(class_rules[cls]), rule_bytes))

    # second pass for precise byte attribution: a rule counts only if ALL its
    # classes are dead
    dead_set = {d[0] for d in dead}
    precise_dead_bytes = 0
    for cls in dead_set:
        for blen, _sel, classes in class_rules[cls]:
            if classes <= dead_set:
                precise_dead_bytes += blen
    # note: a rule with 2 dead classes gets counted twice above; dedupe by rule id
    seen_rules = set()
    precise_dead_bytes = 0
    for selector, decls, blen, _media in rules:
        cls_in_rule = set(CLASS_RE.findall(selector))
        if cls_in_rule and cls_in_rule <= dead_set:
            key = (selector, id(decls))
            if key not in seen_rules:
                seen_rules.add(key)
                precise_dead_bytes += blen

    dead.sort(key=lambda d: -d[2])
    with DEAD_LIST_OUT.open('w', encoding='utf-8') as fh:
        for cls, nrules, nbytes in dead:
            fh.write(f'{nbytes:8d} B  {nrules:3d} rules  .{cls}\n')

    print(f'\n== (b) dead selectors ==')
    print(f'distinct class selectors: {len(all_classes):,}')
    print(f'dynamic-prefix exclusions (kept alive, conservative): {dyn_skipped:,}')
    print(f'VERIFIED dead classes: {len(dead):,}')
    print(f'dead-rule bytes (rules whose classes are ALL dead): {precise_dead_bytes:,} '
          f'({precise_dead_bytes / total_bytes * 100:.1f}% of file)')
    print(f'full list -> {DEAD_LIST_OUT.relative_to(ROOT)}')
    print('top-20 dead by bytes:')
    for cls, nrules, nbytes in dead[:20]:
        print(f'  {nbytes:8,d} B  {nrules:3d} rules  .{cls}')


if __name__ == '__main__':
    main()
