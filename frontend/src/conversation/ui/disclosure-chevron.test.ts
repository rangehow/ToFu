// @vitest-environment jsdom
/**
 * Guard: no conversation card shows a bare disclosure triangle (▶/▸), neither
 * as text, as a pseudo-element glyph nor as the native <details> marker.
 * Folds open from the shared chevron toggle (presentation/disclosure-toggle);
 * card header rows carry the tool rows' turning caret. Cards are rendered by
 * their real owners and checked against every authored stylesheet, inside
 * the conversation surface and outside it (paper panels).
 */
import postcss from 'postcss';
import { describe, expect, it, vi } from 'vitest';

import type { Translator } from '../../i18n';
import en from '../../i18n/locales/en.json';
import { TOOL_ROUND_CHEVRON_RIGHT_SVG } from '../presentation/tool-round-icons';
import { renderChecklistCard } from './checklist-card/index';
import { programExecutionCardHtml } from './program-execution-card';
import { renderProjectReceipt } from './project-receipt/index';
import transcriptFixture from './tool-cards/fixtures/coordinator-transcript.json';
import schemaShapedFixture from './tool-cards/fixtures/schema-shaped.json';
import { createToolCardPresentation } from './tool-cards/index';
import { renderWorkUnitShell } from './work-unit/shell';

// Test-only Node ports are loaded through Vitest; browser compilation has no Node ambient types.
// (Vitest turns `?raw` CSS imports into empty strings, so sheets are read from disk.)
const { readFileSync, readdirSync } = await vi.importActual<{
  readFileSync(path: string, encoding: 'utf8'): string;
  readdirSync(path: string): string[];
}>('node:fs');
const { fileURLToPath } = await vi.importActual<{ fileURLToPath(url: string): string }>('node:url');

const HERE = fileURLToPath(import.meta.url).replace(/[^/]+$/, '');
const SHEET_PATHS = [
  ...[`${HERE}../../styles/application/`, `${HERE}../../styles/settings/`].flatMap((dir) =>
    readdirSync(dir).filter((name) => name.endsWith('.css')).map((name) => dir + name)),
  `${HERE}conversation-surface.css`,
];
const SHEETS = SHEET_PATHS.map((path) => readFileSync(path, 'utf8'));
const TRIANGLE = /[▶▸►▷▹▾▼▿]/u;
const messages = en as Record<string, string>;
const text = (key: string, fallback?: unknown): string => messages[key] ?? (typeof fallback === 'string' ? fallback : key);
const icon = '<svg viewBox="0 0 16 16" aria-hidden="true"></svg>';

type Round = Record<string, unknown> & { toolName: string };

function cardsHtml(): string[] {
  const translate = ((key: string, params?: Record<string, unknown>) => (messages[key] ?? key)
    .replace(/\{(\w+)\}/g, (match, name: string) => (params?.[name] !== undefined ? String(params[name]) : match))) as Translator;
  const tools = createToolCardPresentation({
    translate, markdown: (source) => `<p>${source}</p>`, conversationTitle: () => undefined, debugEnabled: () => true,
  });
  const rounds = [...transcriptFixture, ...schemaShapedFixture] as unknown as Round[];
  const toolCards = rounds.map((round) => tools.renderCardHtml(round as never, { iconHtml: icon })
    || tools.renderGenericCardHtml(round as never, { iconHtml: icon, titleHtml: round.toolName }));
  const program = (status: string, debug: boolean) => programExecutionCardHtml({
    programStatus: status, programCode: 'return await tools.invoke("read", {});',
    programResult: status === 'error' ? { code: 'tool_failed', message: 'denied' } : { rows: [1] },
    childCallIds: ['child'], childToolNames: ['read'],
    programStats: { outcome: status, tool_calls: 1, successful_tool_calls: 1, result_bytes: 9 },
  }, (key, fallback) => text(key, fallback), false, debug);
  const checklist = renderChecklistCard({ roundNum: 1, results: [{
    todos: [{ status: 'completed', content: 'a' }, { status: 'pending', content: 'b' }],
    todoDisplayHistory: [{ done: 0, total: 2, revision: 1 }, { done: 1, total: 2, revision: 2 }],
  }] }, icon, () => '', (key, fallback) => text(key, fallback)) ?? '';
  const receipt = renderProjectReceipt({ toolName: 'project_board_read', roundNum: 2, toolContent: 'Board', results: [{ source: 'Board' }] },
    icon, '', { text: (key, fallback) => text(key, fallback), markdown: (source) => `<p>${source}</p>`, now: () => 0 });
  const work = renderWorkUnitShell('task', 'slides', 'Deck', 'Slides', (key, fallback) => text(key, fallback), { details: '<p>brief</p>' });
  const cards = [...toolCards, program('completed', true), program('error', true), program('completed', false), checklist, receipt, work];
  expect(cards.every(Boolean)).toBe(true);
  return cards.map((html) => html.replace(/<details(?![^>]*\bopen\b)/g, '<details open'));
}

interface Rule { readonly selector: string; readonly decls: ReadonlyMap<string, string> }

const rules: Rule[] = SHEETS.flatMap((sheet) => {
  const found: Rule[] = [];
  postcss.parse(sheet).walkRules((rule) => {
    const decls = new Map<string, string>();
    rule.walkDecls((decl) => { decls.set(decl.prop.toLowerCase(), decl.value); });
    for (const selector of rule.selectors) found.push({ selector, decls });
  });
  return found;
});

/** `[base, pseudo]`; user-action states are dropped so a hover-only glyph still counts. */
function split(selector: string): [string, string] {
  const pseudo = /::?(after|before|marker|-webkit-details-marker)\s*$/.exec(selector);
  const base = (pseudo ? selector.slice(0, pseudo.index) : selector)
    .replace(/:(hover|focus-visible|focus-within|focus|active)\b/g, '');
  return [base.trim() || '*', pseudo ? pseudo[1] : ''];
}

function matches(element: Element, selector: string): boolean {
  try { return element.matches(selector); } catch { return false; }
}

function hosts(): HTMLElement[] {
  return cardsHtml().flatMap((html) => ['surface', 'plain'].map((kind) => {
    const host = document.createElement('div');
    host.innerHTML = kind === 'surface'
      ? `<div class="conversation-surface"><div class="conversation-block" data-turn-zone="process">${html}</div></div>`
      : html;
    document.body.append(host);
    return host;
  }));
}

const SHARED_CHEVRON = (() => {
  const box = document.createElement('div');
  box.innerHTML = TOOL_ROUND_CHEVRON_RIGHT_SVG;
  return box.firstElementChild!.outerHTML;
})();

describe('conversation cards never show a bare disclosure triangle', () => {
  const rendered = hosts();
  const summaries = rendered.flatMap((host) => [...host.querySelectorAll<HTMLElement>('details > summary')]);

  it('read every authored stylesheet', () => {
    expect(SHEETS.length).toBeGreaterThan(150);
    expect(rules.some((rule) => rule.selector === '.ptool-coord-toggle')).toBe(true);
  });

  it('render folds of every card family', () => {
    expect(summaries.filter((node) => node.classList.contains('ptool-coord-toggle')).length).toBeGreaterThan(10);
    for (const kind of ['.ptc-detail', '.ptool-todo-history', '.ptool-work-receipt', '.ptool-convmeta-block', '.ptool-coord-more']) {
      expect(rendered.some((host) => host.querySelector(kind))).toBe(true);
    }
  });

  it('have no triangle glyph in their text', () => {
    for (const host of rendered) expect(host.textContent).not.toMatch(TRIANGLE);
  });

  it('get no triangle glyph from any stylesheet pseudo-element', () => {
    const glyphRules = rules.filter((rule) => TRIANGLE.test(rule.decls.get('content') ?? '') && split(rule.selector)[1]);
    const offenders = glyphRules.filter((rule) => rendered.some((host) =>
      [...host.querySelectorAll('*')].some((node) => matches(node, split(rule.selector)[0]))));
    expect(offenders.map((rule) => rule.selector)).toEqual([]);
  });

  it('suppress the native <details> marker on every summary', () => {
    const hiding = rules.filter((rule) => !split(rule.selector)[1] && (
      /\bnone\b/.test(rule.decls.get('list-style') ?? rule.decls.get('list-style-type') ?? '')
      || /^(?!.*list-item)(flex|inline-flex|grid|inline-grid|block|inline-block|none)\b/.test(rule.decls.get('display') ?? '')));
    const bare = summaries.filter((summary) => !hiding.some((rule) => matches(summary, split(rule.selector)[0])));
    expect(bare.map((summary) => summary.outerHTML.slice(0, 120))).toEqual([]);
  });

  it('open every fold from the shared chevron toggle', () => {
    for (const summary of summaries.filter((node) => !node.classList.contains('ptool-line'))) {
      expect(summary.classList.contains('ptool-coord-toggle')).toBe(true);
      expect(summary.firstElementChild!.outerHTML).toBe(SHARED_CHEVRON);
    }
  });

  it('give every card header row the turning chevron', () => {
    const carets = rules.filter((rule) => split(rule.selector)[1] === 'after' && /rotate\(-45deg\)/.test(rule.decls.get('transform') ?? ''));
    for (const header of summaries.filter((node) => node.classList.contains('ptool-line'))) {
      const svg = header.querySelector(':scope > .ptc-chevron > svg');
      const label = header.querySelector(':scope > .ptool-text');
      const caret = carets.some((rule) => matches(header, split(rule.selector)[0])
        || (label !== null && matches(label, split(rule.selector)[0])));
      expect(svg?.outerHTML === SHARED_CHEVRON || caret, header.outerHTML.slice(0, 120)).toBe(true);
    }
  });
});
