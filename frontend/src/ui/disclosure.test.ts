// @vitest-environment jsdom
/**
 * App-wide guard: no screen shows a bare disclosure triangle (▶/▸/▾/▼…).
 *
 * 1. No authored source (TS/JS/HTML/locale JSON, CSS declarations) carries a
 *    triangle glyph, except the reviewed non-disclosure uses listed below, and
 *    no stylesheet draws one out of borders (a zero-size box with a
 *    transparent border pair: the 成果 toggle drew its caret that way).
 * 2. Every <summary> an authored source creates opens from the shared chevron
 *    (`ui-disclosure` / createDisclosureSummary), or is a reviewed exemption
 *    whose stylesheet suppresses the native marker and draws its own caret.
 *    Conversation cards are rendered and checked by
 *    conversation/ui/disclosure-chevron.test.ts, so conversation/ is scanned
 *    for glyphs only.
 * 3. The kit's `.ui-disclosure` paints exactly DISCLOSURE_CHEVRON_SVG's path.
 */
import postcss from 'postcss';
import { describe, expect, it, vi } from 'vitest';

import { DISCLOSURE_CHEVRON_SVG, createDisclosureSummary } from './disclosure';

// Test-only Node ports are loaded through Vitest; browser compilation has no Node ambient types.
const { readFileSync, readdirSync } = await vi.importActual<{
  readFileSync(path: string, encoding: 'utf8'): string;
  readdirSync(path: string, options: { recursive: true }): string[];
}>('node:fs');
const { fileURLToPath } = await vi.importActual<{ fileURLToPath(url: string): string }>('node:url');

const SRC = fileURLToPath(import.meta.url).replace(/ui\/[^/]+$/, '');
const FILES = readdirSync(SRC, { recursive: true }).map((path) => path.replace(/\\/g, '/'))
  .filter((path) => /\.(ts|js|html|json|css)$/.test(path))
  .filter((path) => !/(^|\/)[^/]*generated[^/]*$|\.test\.ts$|\.d\.ts$|^runtime\/app-runtime\.js$|\/fixtures\//.test(path));
const read = (path: string) => readFileSync(SRC + path, 'utf8');

const TRIANGLE = /[▶▸►▷▹▾▼▿]|\\(?:u)?25(?:b[6-9a-f]|bc|bd|be|bf)\b|&#(?:965[4-9]|966[0-3]);|&#x25(?:b[6-9a-f]);/i;

/** Reviewed glyphs that are not disclosure controls: `file` + exact line fragment. */
const NON_DISCLOSURE_GLYPHS: ReadonlyArray<{ file: RegExp; line: string; reason: string }> = [
  { file: /^i18n\/locales\/\w+\.json$/, line: '"optimizer.runNow": "▶', reason: 'play glyph on the Run now action' },
  { file: /^i18n\/locales\/\w+\.json$/, line: '"orch.note.start": "▶', reason: 'the Start node symbol in its help note' },
  { file: /^features\/paper\/workbench\.ts$/, line: "tab === 'video' ? '▷'", reason: 'video tool mark' },
  { file: /^runtime\/sections\/paper\/report\.js$/, line: '// ▲ up / ▼ down', reason: 'comment on score-change arrows' },
  { file: /^conversation\/presentation\/disclosure-toggle\.ts$/, line: 'no native ▶ marker', reason: 'comment' },
];

function cssText(sheet: string): string {
  const values: string[] = [];
  postcss.parse(sheet).walkDecls((decl) => { values.push(`${decl.prop}:${decl.value}`); });
  return values.join('\n');
}

interface Rule { readonly selector: string; readonly decls: ReadonlyMap<string, string> }
const SHEETS = FILES.filter((path) => path.endsWith('.css'));
const RULES: Rule[] = SHEETS.flatMap((path) => {
  const found: Rule[] = [];
  postcss.parse(read(path)).walkRules((rule) => {
    const decls = new Map<string, string>();
    rule.walkDecls((decl) => { decls.set(decl.prop.toLowerCase(), decl.value); });
    for (const selector of rule.selectors) found.push({ selector, decls });
  });
  return found;
});

function pseudo(selector: string): [string, string] {
  const match = /::?(after|before|marker|-webkit-details-marker)\s*$/.exec(selector);
  const base = (match ? selector.slice(0, match.index) : selector).replace(/:(hover|focus-visible|focus-within|focus|active)\b/g, '');
  return [base.trim() || '*', match ? match[1] : ''];
}
function matches(element: Element, selector: string): boolean {
  try { return element.matches(selector); } catch { return false; }
}
/** A zero-size box whose solid border meets a transparent one renders a triangle. */
function drawsBorderTriangle(decls: ReadonlyMap<string, string>): boolean {
  const zero = (prop: string) => /^0(px)?$/.test((decls.get(prop) ?? '').trim());
  const borders = [...decls].filter(([prop]) => /^border(-(top|right|bottom|left|block|inline)(-(start|end))?)?$/.test(prop));
  return zero('width') && zero('height')
    && borders.some(([, value]) => /\btransparent\b/.test(value))
    && borders.some(([, value]) => /\bsolid\b/.test(value) && !/\btransparent\b/.test(value));
}
function markerHidden(summary: Element): boolean {
  return RULES.some((rule) => !pseudo(rule.selector)[1]
    && (/\bnone\b/.test(rule.decls.get('list-style') ?? rule.decls.get('list-style-type') ?? '')
      || /^(?!.*list-item)(flex|inline-flex|grid|inline-grid|block|inline-block)\b/.test(rule.decls.get('display') ?? ''))
    && matches(summary, pseudo(rule.selector)[0]));
}

/**
 * Summaries that keep their own (non-triangle) caret. `count` is the number of
 * summary sites in the file; `markup` reproduces their DOM context so the test
 * can prove the native marker is suppressed.
 */
const OWN_CARET: Readonly<Record<string, { count: number; reason: string; markup: string[] }>> = {
  'agent-sidebar-dock.ts': { count: 4, reason: 'background-agent dock rows draw a CSS caret',
    markup: ['<div class="agent-dock"><details class="agent-dock-history"><summary>x</summary></details></div>',
      '<div class="agent-dock"><details class="agent-dock-messages"><summary>x</summary></details></div>',
      '<details class="agent-dock-agent"><summary>x</summary></details>',
      '<div class="agent-dock"><details class="agent-dock-agent-messages"><summary>x</summary></details></div>'] },
  'runtime/sections/background-activity.js': { count: 1, reason: 'command rows draw a ›/⌄ chevron',
    markup: ['<details class="bg-act-cmd"><summary class="bg-act-cmd-head">x</summary></details>'] },
  'runtime/sections/ui/finish_info.js': { count: 1, reason: 'wait block header is an hourglass row',
    markup: ['<details class="wait-block"><summary>x</summary></details>'] },
  'runtime/sections/ui/streaming_swarm_panel.js': { count: 5, reason: 'swarm agent folds draw a CSS caret',
    markup: ['<details class="sw-a-task"><summary>x</summary></details>', '<details class="sw-a-messages"><summary>x</summary></details>',
      '<details class="sw-a-tool-history"><summary>x</summary></details>', '<details class="sw-a-err-details"><summary>x</summary></details>'] },
  'runtime/sections/ui/tool_rounds.js': { count: 6, reason: 'conversation rows: header carets, thinking toggle; one match is a comment',
    markup: ['<div class="ptool-line ptool-rejected"><details class="ptool-rejected-params"><summary>x</summary></details></div>',
      '<details class="thinking-block"><summary class="thinking-header">x</summary></details>',
      '<div class="conversation-surface"><details><summary class="ptool-line ptool-memory-header">x</summary></details></div>'] },
  'settings-panels/database.html': { count: 5, reason: 'database settings summaries draw a CSS caret',
    markup: ['<div id="settingsTab_database"><details class="db-disclosure"><summary>x</summary></details></div>'] },
  'settings-panels/messaging.html': { count: 6, reason: 'messaging platform rows draw a CSS caret',
    markup: ['<details class="msg-platform"><summary class="msg-platform-summary">x</summary></details>'] },
  'features/settings/dx-robot.ts': { count: 2, reason: 'channel-advanced folds draw a CSS caret',
    markup: ['<div class="channel-settings"><details class="channel-advanced"><summary>x</summary></details></div>'] },
  'features/settings/github-channel.ts': { count: 1, reason: 'channel-advanced folds draw a CSS caret',
    markup: ['<div class="channel-settings"><details class="channel-advanced"><summary>x</summary></details></div>'] },
  'features/model-catalog/panel.ts': { count: 2, reason: 'model rows carry an SVG caret; the source row is a labelled card',
    markup: ['<details class="stg-mc-model"><summary class="stg-mc-model-head">x</summary></details>',
      '<details class="stg-mc-aa-source"><summary class="stg-mc-aa-summary">x</summary></details>'] },
};

/** Every <summary> a source creates that does not carry the shared chevron. */
function bareSummarySites(source: string): string[] {
  const sites: string[] = [];
  const tag = /<summary\b[^>]*>/g;
  for (const match of source.matchAll(tag)) if (!match[0].includes('ui-disclosure')) sites.push(match[0]);
  const created = /\b(?:document|doc)\.createElement\(\s*['"]summary['"]\s*\)/g;
  for (const match of source.matchAll(created)) {
    const after = source.slice(match.index! + match[0].length, match.index! + match[0].length + 120);
    if (!/^;\s*\n\s*\w+\.className = ["']ui-disclosure["']/.test(after)) sites.push(match[0]);
  }
  const factory = /\b(?:element|node|el|surfaceNode)\(\s*(?:\w+\s*,\s*)?['"]summary['"]|\b(?:text|coordinationText)\([^()\n]{0,80}(?:\([^()\n]*\))?[^()\n]{0,40},\s*['"]summary['"]\s*\)/g;
  for (const match of source.matchAll(factory)) sites.push(match[0]);
  return sites;
}

describe('the whole app never shows a bare disclosure triangle', () => {
  it('scans every authored source and stylesheet', () => {
    expect(FILES.filter((path) => path.endsWith('.css')).length).toBeGreaterThan(150);
    expect(FILES).toContain('runtime/sections/optimizer.js');
    expect(FILES).toContain('settings-panels/database.html');
    expect(FILES).toContain('i18n/locales/en.json');
  });

  it('has no triangle glyph outside reviewed non-disclosure uses', () => {
    const offenders: string[] = [];
    for (const path of FILES) {
      const source = read(path);
      const text = path.endsWith('.css') ? cssText(source) : source;
      text.split('\n').forEach((line, index) => {
        if (!TRIANGLE.test(line)) return;
        if (NON_DISCLOSURE_GLYPHS.some((allowed) => allowed.file.test(path) && line.includes(allowed.line))) return;
        offenders.push(`${path}:${index + 1}: ${line.trim().slice(0, 120)}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it('draws no triangle out of CSS borders', () => {
    expect(drawsBorderTriangle(new Map([['width', '0'], ['height', '0'],
      ['border-block', '4px solid transparent'], ['border-inline-start', '5px solid var(--text-tertiary)']]))).toBe(true);
    expect(RULES.filter((rule) => drawsBorderTriangle(rule.decls)).map((rule) => rule.selector)).toEqual([]);
  });

  it('opens every <summary> from the shared chevron or a reviewed own caret', () => {
    const offenders: string[] = [];
    for (const path of FILES.filter((file) => /\.(ts|js|html)$/.test(file) && !file.startsWith('conversation/') && file !== 'ui/disclosure.ts')) {
      const sites = bareSummarySites(read(path));
      const own = OWN_CARET[path];
      if (sites.length && sites.length !== own?.count) offenders.push(`${path}: ${sites.length} summaries without the shared chevron`);
    }
    for (const path of Object.keys(OWN_CARET)) expect(FILES, path).toContain(path);
    expect(offenders).toEqual([]);
  });

  it('suppresses the native marker on every reviewed own-caret summary', () => {
    const bare: string[] = [];
    for (const [path, entry] of Object.entries(OWN_CARET)) {
      for (const markup of entry.markup) {
        const host = document.createElement('div');
        host.innerHTML = markup;
        document.body.append(host);
        if (!markerHidden(host.querySelector('summary')!)) bare.push(`${path}: ${markup}`);
        host.remove();
      }
    }
    expect(bare).toEqual([]);
  });

  it('paints the shared chevron path on .ui-disclosure and hides the native marker', () => {
    const summary = createDisclosureSummary(document, 'Label');
    const details = document.createElement('details');
    details.append(summary);
    document.body.append(details);
    expect(markerHidden(summary)).toBe(true);
    const path = /<path d="([^"]+)"[^>]*stroke-width="([^"]+)"/.exec(DISCLOSURE_CHEVRON_SVG)!;
    const chevron = RULES.find((rule) => rule.selector === '.ui-disclosure::before')!;
    for (const prop of ['mask', '-webkit-mask']) {
      const svg = decodeURIComponent(chevron.decls.get(prop) ?? '');
      expect(svg).toContain(`d='${path[1]}'`);
      expect(svg).toContain(`stroke-width='${path[2]}'`);
      expect(svg).toContain("viewBox='0 0 16 16'");
    }
    expect(RULES.some((rule) => /details\[open\]\s*>\s*\.ui-disclosure::before/.test(rule.selector)
      && rule.decls.get('transform') === 'rotate(90deg)')).toBe(true);
    expect(RULES.some((rule) => rule.selector === '.ui-disclosure[aria-expanded="true"]::before')).toBe(true);
    details.remove();
  });
});
