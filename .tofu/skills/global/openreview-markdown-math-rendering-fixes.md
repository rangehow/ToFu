---
name: openreview-markdown-latex-rendering-fixes-and-autofix-script
description: Fix LaTeX math rendering in OpenReview/GitHub Markdown: CommonMark eats backslash before ASCII punctuation, bare * triggers emphasis, bare <letter triggers HTML tag, }_ triggers italic, AND \rbrace/\Vert/\lbrace followed by space+underscore triggers cross-math-block emphasis pairing in marked — comprehensive 23-rule v4 auto-fix script
enabled: true
tags: [markdown, latex, mathjax, openreview, math-rendering, bug-pattern, python, marked, emphasis, rbrace, underscore]
created: 2026-03-27T06:28:08Z
updated: 2026-04-08T08:18:45Z
---

# OpenReview Markdown + LaTeX Math Rendering Fixes (v4)

## Rendering Pipeline
OpenReview uses: `marked` (v15) → `DOMPurify.sanitize()` → `MathJax.typesetPromise()`

**Critical**: `marked` has NO math extension — it processes `$...$` content as ordinary CommonMark text, THEN MathJax post-processes the resulting HTML. This means CommonMark/marked can destroy LaTeX before MathJax sees it.

## Bug Categories (7 total)

### 1. Backslash Eating (CommonMark spec)
CommonMark treats `\<ASCII-punctuation>` as escape sequences, eating the backslash.
- `\,` `\;` `\:` `\!` → use `\mkern{N}mu` equivalents
- `\{` `\}` → use `\lbrace` `\rbrace`
- `\|` → use `\Vert`

### 2. Bare `*` → Markdown Emphasis
- `p_n^*` → `p_n^\ast`

### 3. Bare `<letter` → HTML Tag Parsing
- `i<n` → `i\lt n`
- `a>0` → `a \gt 0`

### 4. `}_` → Italic Trigger
- `\mathbf{x}_n` → `\mathbf{x}\mkern0mu_n`

### 5. `\rbrace _` / `\Vert _` / `\lbrace _` → Cross-Block Emphasis (NEW in v4)
**Root cause**: After rule 12 converts `\}` → `\rbrace `, the original `\}_{sub}` becomes `\rbrace _{sub}`. The SPACE before `_` makes it a **left-flanking delimiter** in marked's emphasis parser. This `_` opens `<em>`, and marked finds the matching closing `_` potentially in a DIFFERENT math block in the same paragraph, destroying everything in between.

**Example**:
```
$\lbrace x_i^t\rbrace _{i\lt n}$ ... $L_{\mathrm{CE}}$
```
marked produces:
```html
$\lbrace x_i^t\rbrace <em>{i\lt n}$ ... $L</em>{\mathrm{CE}}$
```
The `_` after `\rbrace ` opens emphasis, the `_` before `{\mathrm{CE}}` closes it.

**Fix**: `\rbrace _{` → `\rbrace\mkern0mu_{` (remove space, insert zero-width kern)

### 6. `\underbrace{A}_{B}` → restructure to `\underset{B}{\underbrace{A}}`

### 7. `|\mathcal V|` → `\lvert\mathcal V\rvert`

## Fix Script: `fix_openreview_latex.py`

23 rules applied in order (ORDER MATTERS — compound patterns before sub-patterns, delimiter conversion before `}_` fix).

Key rule ordering issue: Rules 11-13 convert `\}` → `\rbrace `, then rules 19-21 must catch the resulting `\rbrace _` pattern. Rule 18 (`}_` → `}\mkern0mu_`) only catches the original `}_` before delimiter conversion.

### Usage
```bash
python fix_openreview_latex.py input.md -o output.md   # fix
python fix_openreview_latex.py --check input.md         # dry-run
python fix_openreview_latex.py --test                    # self-tests
```

### Verification with marked
```bash
npm install marked
node -e "
const { marked } = require('marked');
const html = marked.parse(fs.readFileSync('file.md', 'utf8'));
// Check for <em>/<strong> inside math (between $ delimiters)
// If found → rendering is broken
"
```

## Key Insight
The fix script must be **idempotent** — running it twice produces the same output. All patterns check for already-fixed forms (e.g., `\lbrace` won't be double-converted).
