// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { createMarkdownRenderer } from '../../markdown';
import { createDeliverableOpener } from './open';
import type { LeadDeliverable } from './model';

vi.mock('../../i18n', () => ({ t: (key: string) => key }));
afterEach(() => { document.body.replaceChildren(); });

it('normal report reopen already reuses exact Markdown projection, not dialog DOM or changed copy', async () => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () {
    this.returnValue = ''; this.removeAttribute('open'); this.dispatchEvent(new Event('close'));
  };
  let parses = 0, sanitizations = 0, renderCalls = 0;
  const parser = { parse(source: string) { parses++; return marked.parse(source, { async: false }) as string; } };
  const sanitizer = { sanitize(html: string) { sanitizations++; return DOMPurify.sanitize(html); } };
  const renderer = createMarkdownRenderer({
    maximumCacheEntries: 300,
    getEnvironment: () => ({ document, parser, sanitizer, highlighter: null, mathRenderer: null,
      basePath: '', language: 'en', projectActive: false }), ensureMath: () => {},
  });
  const opener = createDeliverableOpener({ openArtifact: vi.fn(), openTurn: vi.fn(),
    markdown(source) { renderCalls++; return renderer.render(source); } });
  const item: LeadDeliverable = { id: 'report:benign', kind: 'report', title: 'Benign fixture',
    projectPath: '/fixture', createdAt: 1, source: { conversationId: 'fixture', turnId: 'benign' },
    summary: Array.from({ length: 160 }, (_, index) => `Paragraph ${index}: **local benign report**.`).join('\n\n') };
  const outputs: string[] = [];
  const observations: Array<{ parses: number; sanitizations: number; renderCalls: number }> = [];
  try {
    for (let index = 0; index < 3; index++) {
      const before = { parses, sanitizations, renderCalls };
      expect(opener.open(item)).toBe(true);
      const dialog = document.querySelector<HTMLDialogElement>('dialog.lead-deliverable-dialog')!;
      outputs.push(dialog.innerHTML);
      observations.push({ parses: parses - before.parses, sanitizations: sanitizations - before.sanitizations,
        renderCalls: renderCalls - before.renderCalls });
      dialog.close();
      await Promise.resolve();
      expect(document.querySelector('dialog')).toBeNull();
    }
    expect(new Set(outputs).size).toBe(1);
    expect(observations.map(row => row.renderCalls)).toEqual([1, 1, 1]);
    expect(observations.map(row => row.parses)).toEqual([1, 0, 0]);
    expect(observations.slice(1).every(row => row.sanitizations === 0)).toBe(true);
    opener.open({ ...item, summary: 'Changed **same report ID**.' });
    expect(parses).toBe(2);
    expect(document.querySelector('dialog')!.textContent).toContain('Changed same report ID.');
    expect(document.querySelector('dialog')!.textContent).not.toContain('Paragraph 159');
    console.info('normal report opens', JSON.stringify(observations));
  } finally { document.querySelector<HTMLDialogElement>('dialog')?.close(); renderer.dispose(); }
});
