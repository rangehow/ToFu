"""
fix_openreview_latex.py  v3 — Auto-fix LaTeX-in-Markdown for OpenReview / GitHub / any CommonMark+MathJax/KaTeX.

CommonMark treats  \\<ASCII-punctuation>  as an escape sequence and eats the backslash.
ASCII punctuation = ! " # $ % & ' ( ) * + , - . / : ; < = > ? @ [ \\ ] ^ _ ` { | } ~

Additionally:
  - Bare `*` in math triggers Markdown emphasis/bold
  - Bare `<letter` in math triggers HTML tag parsing
  - Some renderers don't support \\thinspace, \\thickspace etc.

This script rewrites dangerous LaTeX patterns to safe equivalents — ALL inside math delimiters only.

Usage:
    python fix_openreview_latex.py input.md -o output.md   # fix file
    python fix_openreview_latex.py --check input.md         # dry-run check
    python fix_openreview_latex.py --raw                    # fix raw LaTeX from stdin
    python fix_openreview_latex.py --rules                  # show all rules
    python fix_openreview_latex.py --test                   # run self-tests
"""

import re
import sys
import argparse

# ─── Rule registry ────────────────────────────────────────────────────────────

RULES = []


def rule(pattern, repl, desc):
    """Register a (compiled_regex, replacement, description) rule."""
    RULES.append((re.compile(pattern), repl, desc))


# Rule 1: \perp\!\!\!\perp → \perp\mkern-9mu\perp  (independence symbol)
rule(r'\\perp\s*(?:\\[!]\s*){2,4}\\perp', r'\\perp\\mkern-9mu\\perp',
     r'\perp\!\!\!\perp -> \perp\mkern-9mu\perp')

# Rules 2-6: Spacing commands (backslash will be eaten by CommonMark)
rule(r'\\,', r'\\mkern3mu ', r'\, -> \mkern3mu')
rule(r'\\;', r'\\mkern5mu ', r'\; -> \mkern5mu')
rule(r'\\:', r'\\mkern4mu ', r'\: -> \mkern4mu')
rule(r'\\!', r'\\mkern-3mu ', r'\! -> \mkern-3mu')

# Rules 7-12: Named spacing commands (may not be supported by all renderers)
rule(r'\\thinspace\b',     r'\\mkern3mu ',  r'\thinspace  -> \mkern3mu')
rule(r'\\thickspace\b',    r'\\mkern5mu ',  r'\thickspace -> \mkern5mu')
rule(r'\\medspace\b',      r'\\mkern4mu ',  r'\medspace   -> \mkern4mu')
rule(r'\\negthinspace\b',  r'\\mkern-3mu ', r'\negthinspace  -> \mkern-3mu')
rule(r'\\negmedspace\b',   r'\\mkern-4mu ', r'\negmedspace   -> \mkern-4mu')
rule(r'\\negthickspace\b', r'\\mkern-5mu ', r'\negthickspace -> \mkern-5mu')

# Rules 13-15: Delimiter commands (backslash eaten)
rule(r'\\\{', r'\\lbrace ', r'\{ -> \lbrace')
rule(r'\\\}', r'\\rbrace ', r'\} -> \rbrace')
rule(r'\\\|', r'\\Vert ',   r'\| -> \Vert')

# Rules 16-17: Bare angle brackets → HTML tag parsing
rule(r'(?<!\\)(?<!\\left)(?<!\\right)<', r'\\lt ',  r'bare < -> \lt')
rule(r'(?<!\\)(?<!\\left)(?<!\\right)>', r'\\gt ',  r'bare > -> \gt')

# Rule 18: Bare * → Markdown emphasis
rule(r'(?<!\\)\*', r'\\ast ', r'bare * -> \\ast (prevent emphasis)')


# Rule 19: \underbrace{A}_{B} → \underset{B}{\underbrace{A}}
def fix_underbrace(m):
    body = m.group(1)
    label = m.group(2)
    return r'\underset{' + label + r'}{\underbrace{' + body + '}}'


RULES.append((
    re.compile(r'\\underbrace\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}_\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}'),
    fix_underbrace,
    r'\underbrace{A}_{B} -> \underset{B}{\underbrace{A}}',
))

# Rule 20: }_ italic trigger → }\mkern0mu_
rule(r'\}(?=_)', r'}\\mkern0mu', r'}_ italic trigger -> }\mkern0mu_')

# Rules 21-23: \rbrace _ / \Vert _ / \lbrace _ cross-block emphasis prevention
rule(r'\\rbrace\s+(?=_)',  r'\\rbrace\\mkern0mu',  r'\\rbrace _ -> \\rbrace\\mkern0mu_ (prevent emphasis)')
rule(r'\\Vert\s+(?=_)',    r'\\Vert\\mkern0mu',    r'\\Vert _ -> \\Vert\\mkern0mu_ (prevent emphasis)')
rule(r'\\lbrace\s+(?=_)',  r'\\lbrace\\mkern0mu',  r'\\lbrace _ -> \\lbrace\\mkern0mu_ (prevent emphasis)')

# Rule for |mathcal/mathbb/... V| → \lvert ... \rvert
rule(r'\|\\(mathcal|mathbb|mathbf|mathfrak)\s+([A-Za-z])\|',
     r'\\lvert\\\1 \2\\rvert',
     r'|\mathcal V| -> \lvert\mathcal V\rvert')

# ─── Math block extraction ────────────────────────────────────────────────────

# Matches: $$...$$, $...$, \(...\), \[...\]
MATH_PATTERN = re.compile(
    r'(\$\$[\s\S]*?\$\$)'        # display math $$...$$
    r'|(\$(?!\$)(?:[^$\\]|\\.)+?\$)'  # inline math $...$
    r'|(\\\([\s\S]*?\\\))'       # inline math \(...\)
    r'|(\\\[[\s\S]*?\\\])',       # display math \[...\]
    re.DOTALL,
)


# ─── Core functions ───────────────────────────────────────────────────────────

def fix_math_block(latex):
    """Apply all rules to a single math block."""
    for pat, repl, _desc in RULES:
        latex = pat.sub(repl, latex)
    return latex


def fix_markdown(text):
    """Fix all math blocks in a Markdown document."""
    # Ensure display-math $$ is on its own line (helps some parsers)
    text = re.sub(r'([^\n])(\$\$)\s*\n', r'\1\n\2\n', text)
    text = re.sub(r'\n\s*(\$\$)([^\n$])', r'\n\1\n\2', text)

    def replacer(m):
        return fix_math_block(m.group(0))

    result = MATH_PATTERN.sub(replacer, text)
    return result


# ─── Check mode (dry-run) ────────────────────────────────────────────────────

CHECK_PATTERNS = [
    (re.compile(r'\\[,;:!](?![a-zA-Z])'),
     'Spacing cmd (\\, \\; \\: \\!) — backslash will be eaten'),
    (re.compile(r'\\[{}\\|]'),
     'Delimiter (\\{ \\} \\|) — backslash will be eaten'),
    (re.compile(r'\\underbrace\{[^}]*\}_\{'),
     '\\underbrace{A}_{B} — }_ triggers italic'),
    (re.compile(r'(?<!\\text)\}(?=_[^_])'),
     '}_ pattern — potential italic trigger'),
    (re.compile(r'\\perp\s*\\!\s*\\!'),
     '\\perp\\!\\! — \\! will be eaten'),
    (re.compile(r'\\thinspace'),
     '\\thinspace — may not be supported'),
    (re.compile(r'\\thickspace'),
     '\\thickspace — may not be supported'),
    (re.compile(r'\\medspace'),
     '\\medspace — may not be supported'),
    (re.compile(r'\\negthinspace'),
     '\\negthinspace — may not be supported'),
    (re.compile(r'(?<!\\)\*'),
     'Bare * — triggers Markdown emphasis'),
    (re.compile(r'(?<!\\)(?<!\\left)(?<!\\right)<'),
     'Bare < — triggers HTML tag parsing'),
    (re.compile(r'(?<!\\)(?<!\\left)(?<!\\right)>'),
     'Bare > — triggers HTML tag parsing'),
]


def check_markdown(text):
    """Check for issues in math blocks without fixing. Returns list of (line_no, issue)."""
    issues = []
    for i, line in enumerate(text.split('\n'), 1):
        for m_block in MATH_PATTERN.finditer(line):
            block = m_block.group(0)
            for pat, desc in CHECK_PATTERNS:
                if pat.search(block):
                    issues.append((i, desc))
    return issues


# ─── Self-tests ───────────────────────────────────────────────────────────────

def run_tests():
    tests = [
        ('$a\\,b$', '$a\\mkern3mu b$', 'thin space'),
        ('$a\\;b$', '$a\\mkern5mu b$', 'thick space'),
        ('$a\\:b$', '$a\\mkern4mu b$', 'med space'),
        ('$a\\!b$', '$a\\mkern-3mu b$', 'neg thin space'),
        ('$a\\thinspace b$', '$a\\mkern3mu  b$', 'thinspace -> mkern'),
        ('$a\\thickspace b$', '$a\\mkern5mu  b$', 'thickspace -> mkern'),
        ('$a\\medspace b$', '$a\\mkern4mu  b$', 'medspace -> mkern'),
        ('$a\\negthinspace b$', '$a\\mkern-3mu  b$', 'negthinspace -> mkern'),
        ('$\\{x\\}$', '$\\lbrace x\\rbrace $', 'braces'),
        ('$\\|x\\|$', '$\\Vert x\\Vert $', 'double bar'),
        ('$\\underbrace{A+B}_{label}$', '$\\underset{label}{\\underbrace{A+B}}$', 'underbrace'),
        ('$\\mathbf{x}_n$', '$\\mathbf{x}\\mkern0mu_n$', '}_ trigger'),
        ('$\\perp\\!\\!\\!\\perp$', '$\\perp\\mkern-9mu\\perp$', 'independence'),
        ('$|\\mathcal V|$', '$\\lvert\\mathcal V\\rvert$', 'abs value'),
        ('$a \\leq b$', '$a \\leq b$', 'no false positive leq'),
        ('$\\lbrace x\\rbrace$', '$\\lbrace x\\rbrace$', 'no double fix'),
        ('$a\\mkern3mu b$', '$a\\mkern3mu b$', 'mkern passthrough'),
        ('$\\mathbf x_n$', '$\\mathbf x_n$', 'no-brace mathbf ok'),
        ('$\\;\\leq\\;$', '$\\mkern5mu \\leq\\mkern5mu $', 'real spacing'),
        ('$p_n^*$', '$p_n^\\ast $', 'bare star -> ast'),
        ('$w_n^{*}$', '$w_n^{\\ast }$', 'star in braces -> ast'),
        ('$H(p^*, q)$', '$H(p^\\ast , q)$', 'star in expression'),
        ('$i<n$', '$i\\lt n$', 'bare < -> \\lt'),
        ('$a > 0$', '$a \\gt  0$', 'bare > -> \\gt'),
        ('$\\rbrace _{i<n}$', '$\\rbrace\\mkern0mu_{i\\lt n}$', 'rbrace < + _ trigger'),
        ('$\\rbrace _{sub}$', '$\\rbrace\\mkern0mu_{sub}$', 'rbrace space underscore'),
        ('$\\Vert _{sub}$', '$\\Vert\\mkern0mu_{sub}$', 'Vert space underscore'),
        ('$\\lbrace _{sub}$', '$\\lbrace\\mkern0mu_{sub}$', 'lbrace space underscore'),
        ('$a \\leq b$', '$a \\leq b$', 'leq untouched'),
        ('$\\left< x \\right>$', '$\\left< x \\right>$', 'left/right angle'),
        ('$a \\lt b$', '$a \\lt b$', 'existing \\lt ok'),
        ('$a \\gt b$', '$a \\gt b$', 'existing \\gt ok'),
    ]

    passed = 0
    failed = 0
    for inp, expected, name in tests:
        result = fix_markdown(inp)
        if result == expected:
            passed += 1
        else:
            failed += 1
            print(f'  FAIL [{name}]')
            print(f'    input:    {inp}')
            print(f'    expected: {expected}')
            print(f'    got:      {result}')

    print(f'\n  {passed} passed, {failed} failed')
    return failed == 0


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Fix LaTeX-in-Markdown for OpenReview')
    parser.add_argument('input', nargs='?', help='Input Markdown file')
    parser.add_argument('-o', '--output', help='Output file (default: stdout)')
    parser.add_argument('--check', action='store_true',
                        help='Check for issues without fixing')
    parser.add_argument('--raw', action='store_true',
                        help='Fix raw LaTeX from stdin (no $ needed)')
    parser.add_argument('--rules', action='store_true',
                        help='Show all rules')
    parser.add_argument('--test', action='store_true',
                        help='Run self-tests')

    args = parser.parse_args()

    if args.test:
        success = run_tests()
        sys.exit(0 if success else 1)

    if args.rules:
        print(f'Rules ({len(RULES)} total):')
        for i, (_, _, desc) in enumerate(RULES, 1):
            print(f'  {i:2d}  {desc}')
        return

    if args.raw:
        raw = sys.stdin.read()
        wrapped = '$' + raw + '$'
        fixed = fix_markdown(wrapped)
        # Strip the wrapping $ delimiters
        print(fixed[1:-1])
        return

    if not args.input:
        parser.print_help()
        sys.exit(1)

    with open(args.input) as f:
        text = f.read()

    if args.check:
        issues = check_markdown(text)
        if not issues:
            print('No issues found.')
        else:
            for line_no, desc in issues:
                print(f'  line {line_no}: {desc}')
        sys.exit(1 if issues else 0)

    text = fix_markdown(text)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(text)
    else:
        print(text)


if __name__ == '__main__':
    main()
